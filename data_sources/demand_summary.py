"""Demand Summary connector — Microsoft Fabric Lakehouse Files I/O.

Reads the two CSV files that back the *Demand Summary* section on the
Demand Planner Analytics page:

* ``Files/RO Tracking/Demand Plan/qry_mgmt_plan_full.csv``
* ``Files/RO Tracking/Demand Plan/qry_total_item_level_demand.csv``

Both blobs live in the same OneLake lakehouse used by every other
RO Tracking surface (see ``ro_comparison._SECRETS_SECTION``).  We
piggyback on the shared ``[fabric_htst]`` secrets block — no per-feature
credentials, no separate sign-in — so any user already authenticated for
RO Comparison / RO Summary Report can read these tables with zero extra
latency.

Public surface
--------------
* :class:`DemandSummaryError`       — domain-specific exception.
* :class:`DemandSummarySnapshot`    — value object: ``(df, etag, size,
                                       last_modified, blob_path)``.
* :func:`fetch_mgmt_plan_full`     — full ``qry_mgmt_plan_full.csv``.
* :func:`fetch_total_item_level_demand` — full
                                       ``qry_total_item_level_demand.csv``.
* :func:`clear_demand_summary_cache` — single-call cache invalidation
                                       (wired to the section's "Refresh"
                                       button).

Cache model
-----------
Both fetchers are wrapped in ``@st.cache_data`` with a 15-minute TTL —
identical to the RO Comparison / IBP cadence so the planner has one
mental model for "how fresh is the data on this page".  Cache keys are
trivial sentinel strings (no signature) because there is exactly one
canonical blob per file; the public wrappers accept ``force_refresh`` to
bypass.

Errors
------
Underlying :class:`LakehouseIOError` is wrapped into
:class:`DemandSummaryError` so the page can render one consistent error
banner without leaking storage-SDK diagnostics into the section body.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st

from data_sources.fabric_lakehouse_io import (
    LakehouseIOError,
    get_file_properties,
    read_bytes,
    read_csv,
)


logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────────
#
# We piggyback on the ``[fabric_htst]`` secrets block — same pattern as
# every other RO Tracking connector (see ``ro_comparison.py``).  The
# block must provide ``workspace`` and ``lakehouse`` (display names or
# GUIDs).  See ``fabric_lakehouse_io._read_lakehouse_config`` for the
# inheritance rules.
_SECRETS_SECTION: str = "fabric_htst"

# Source blob paths under ``Files/`` — POSIX-style, no leading slash.
# Hard-coded as module-level constants because they are the canonical
# locations on the Fabric portal, not user-configurable inputs.
_MGMT_PLAN_FULL_BLOB_PATH: str = (
    "RO Tracking/Demand Plan/qry_mgmt_plan_full.csv"
)
_TOTAL_ITEM_LEVEL_DEMAND_BLOB_PATH: str = (
    "RO Tracking/Demand Plan/qry_total_item_level_demand.csv"
)
# Primary source for the per-item Supply Format lookup used by the
# Demand Pivot Summary.  Joined on Item.  When a row is missing here,
# we fall back to RO_Item_Master.csv (read via the existing
# ``ro_comparison.fetch_ro_item_master_df`` connector — see
# :func:`build_supply_format_lookup`).
_PDH_BLOB_PATH: str = (
    "RO Tracking/Demand Plan/qry_pdh.csv"
)

# Annual budget sources for the Demand Pivot Summary's "Total Budget"
# column + the Base+RO chart's dotted budget line.  Both live in the
# top-level ``Files/RO Tracking/`` folder (NOT inside ``Demand Plan/``
# — these are static planning artefacts published once per cycle).
#
# Schemas (per the planner's confirmation):
#
#   Static_Budget_Base_Lbs.csv
#   --------------------------
#   Portfolio Major, Supply Format, Brand Name, Portfolio Minor,
#   Sum of consensus_forecast   ← annual lbs for that (PMaj, SFmt)
#
#   Static_Budget_RO_Lbs.csv
#   ------------------------
#   Format, Customer, Taxonomy, Brand, Item No, Item Desc, Probability,
#   First Ship Date, Lbs./yr, Lbs./yr Exp, Days in Year,
#   FY Lbs. Total, FY Lbs. Exp, Portfolio Major
#                              ↑ "Format" is the Supply-Format equivalent
#                              ↑ ``FY Lbs. Total`` is the annual
#                                budget figure we attribute to the
#                                (PMaj, R&O, SFmt) leaf
_STATIC_BUDGET_BASE_BLOB_PATH: str = (
    "RO Tracking/Static_Budget_Base_Lbs.csv"
)
_STATIC_BUDGET_RO_BLOB_PATH: str = (
    "RO Tracking/Static_Budget_RO_Lbs.csv"
)

# 15-minute Streamlit cache TTL.  Mirrors the RO Comparison / IBP /
# Summary Report cadence so the whole Demand Planner Analytics page has
# one consistent freshness window.
_CACHE_TTL_SECONDS: int = 15 * 60


# ── Public types ─────────────────────────────────────────────────────────────

class DemandSummaryError(RuntimeError):
    """Raised on any Demand Summary I/O or parse failure.

    Wraps the lower-level :class:`LakehouseIOError` so the page renders
    a single, scope-aware banner without leaking the storage SDK's
    chain-of-exceptions into the section body.
    """


@dataclass(frozen=True)
class DemandSummarySnapshot:
    """Identity + payload for a single Demand Summary CSV snapshot.

    Attributes
    ----------
    df
        The parsed DataFrame.  Never ``None`` — an empty CSV degrades
        to ``pd.DataFrame()`` (callers can branch on ``df.empty``).
    etag
        Fabric ETag of the blob at read time.  ``None`` if the storage
        SDK didn't surface one (rare).
    size
        Blob size in bytes (best-effort, ``None`` if unavailable).
    last_modified
        Best-effort UTC timestamp of the most recent Fabric write.
        ``None`` if the storage SDK didn't surface one.
    blob_path
        POSIX path under ``Files/`` so the UI can echo "Source:
        Files/RO Tracking/Demand Plan/qry_mgmt_plan_full.csv".
    """
    df: pd.DataFrame
    etag: Optional[str]
    size: Optional[int]
    last_modified: Optional[datetime]
    blob_path: str

    @property
    def row_count(self) -> int:
        """Convenience for the UI caption — never raises on empty frames."""
        return int(len(self.df))

    @property
    def column_count(self) -> int:
        """Convenience for the UI caption."""
        return int(len(self.df.columns))


# ── Internal cached readers ──────────────────────────────────────────────────
#
# We split each public ``fetch_*`` from its ``@st.cache_data`` impl so
# that the public wrapper can:
#   1. Accept a ``force_refresh=True`` flag without spilling the
#      Streamlit-specific ``.clear()`` API onto callers.
#   2. Always return a strongly-typed :class:`DemandSummarySnapshot`.
#
# The cached impl's ``_signature`` argument is the documented Streamlit
# pattern for an explicit cache key — it participates in cache identity
# but its contents are never hashed by us.

@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch(blob_path: str, _signature: str) -> DemandSummarySnapshot:
    """Cached read of a single Demand Summary CSV blob.

    Centralised so both top-level fetchers share one implementation —
    add a new file by introducing a single thin wrapper, not a parallel
    cached function.  Raises :class:`DemandSummaryError` on any
    underlying read / parse failure.
    """
    # 1. Lightweight metadata (etag, size, last_modified) for the UI
    #    caption.  Failure here is non-fatal — we fall through to the
    #    body read with empty metadata rather than blocking the page.
    last_modified: Optional[datetime] = None
    size: Optional[int] = None
    try:
        props = get_file_properties(_SECRETS_SECTION, blob_path)
    except LakehouseIOError as exc:
        # Properties-fetch is a "nice to have" header pull; if it
        # blows up we still try the body read and surface a useful
        # error there if needed.
        logger.info(
            "get_file_properties failed for 'Files/%s' (non-fatal): %s",
            blob_path, exc,
        )
        props = None

    if props is not None:
        size = props.size
        if props.last_modified:
            # ``last_modified`` is stored as a string for portability
            # across SDK versions; parse defensively here so the
            # snapshot exposes a real ``datetime`` to the UI.
            try:
                last_modified = pd.to_datetime(
                    props.last_modified, utc=True,
                ).to_pydatetime()
            except (TypeError, ValueError) as exc:
                logger.info(
                    "Could not parse last_modified=%r for 'Files/%s' "
                    "(non-fatal): %s",
                    props.last_modified, blob_path, exc,
                )

    # 2. Authoritative body read.  Any I/O failure surfaces as a
    #    domain-specific error so the page can render one clean banner.
    try:
        df, etag = read_csv(_SECRETS_SECTION, blob_path)
    except LakehouseIOError as exc:
        raise DemandSummaryError(
            f"Could not read 'Files/{blob_path}' from Microsoft Fabric: {exc}"
        ) from exc

    if df is None:
        raise DemandSummaryError(
            f"OneLake blob 'Files/{blob_path}' does not exist.  Verify "
            "that the upstream pipeline has published the file and that "
            "your account has Read access to the lakehouse."
        )

    logger.info(
        "Loaded Demand Summary CSV 'Files/%s': %s rows, %s columns.",
        blob_path, len(df), len(df.columns),
    )

    return DemandSummarySnapshot(
        df=df,
        etag=etag,
        size=size,
        # ``last_modified`` may be UTC-aware from the SDK; we keep it
        # as-is — UI converts to a display string when needed.
        last_modified=last_modified.astimezone(timezone.utc) if last_modified else None,
        blob_path=blob_path,
    )


# ── Public fetch helpers ─────────────────────────────────────────────────────

def fetch_mgmt_plan_full(*, force_refresh: bool = False) -> DemandSummarySnapshot:
    """Return the latest ``qry_mgmt_plan_full.csv`` as a snapshot.

    Parameters
    ----------
    force_refresh
        When True, clears this connector's cache slot before reading so
        the next call hits Fabric.  Wire this to a "Refresh from Fabric"
        button in the UI.
    """
    if force_refresh:
        _cached_fetch.clear()
    return _cached_fetch(_MGMT_PLAN_FULL_BLOB_PATH, "default")


def fetch_total_item_level_demand(
    *, force_refresh: bool = False,
) -> DemandSummarySnapshot:
    """Return the latest ``qry_total_item_level_demand.csv`` as a snapshot.

    The returned snapshot's ``df`` has the raw ``Start of Month`` column
    (Excel-serial integers from the source CSV) converted into proper
    ``datetime64[ns]`` so the preview table renders human-readable
    dates instead of opaque numbers.  The conversion is lossless —
    every parseable value becomes a date; unparseables stay as
    ``NaT`` so we don't silently coerce bad input.  The ``raw bytes``
    download path is unaffected: planners who hit the ⬇️ button still
    get a byte-for-byte copy of what's in Fabric.

    See :func:`fetch_mgmt_plan_full` for the ``force_refresh`` contract.
    """
    if force_refresh:
        _cached_fetch.clear()
    snapshot = _cached_fetch(_TOTAL_ITEM_LEVEL_DEMAND_BLOB_PATH, "default")
    return _coerce_demand_dates_for_display(snapshot)


def fetch_pdh(*, force_refresh: bool = False) -> DemandSummarySnapshot:
    """Return the latest ``qry_pdh.csv`` as a snapshot.

    Primary source for the per-item Supply Format lookup consumed by
    the Demand Pivot Summary (see :func:`build_supply_format_lookup`).
    Joined on the item-number column inside ``qry_pdh.csv`` (auto-
    detected from a small whitelist of likely names, see
    :data:`_PDH_ITEM_KEY_CANDIDATES`).

    See :func:`fetch_mgmt_plan_full` for the ``force_refresh`` contract.
    """
    if force_refresh:
        _cached_fetch.clear()
    return _cached_fetch(_PDH_BLOB_PATH, "default")


def fetch_static_budget_base(
    *, force_refresh: bool = False,
) -> DemandSummarySnapshot:
    """Return the latest ``Static_Budget_Base_Lbs.csv`` as a snapshot.

    Backs the "Total Budget" column on every Base-Plan leaf of the
    Demand Pivot Summary.  The pivot builder consumes this via
    :func:`build_budget_lookup`, which sums ``Sum of consensus_forecast``
    per ``(Portfolio Major, Supply Format)`` bucket.

    See :func:`fetch_mgmt_plan_full` for the ``force_refresh`` contract.
    """
    if force_refresh:
        _cached_fetch.clear()
    return _cached_fetch(_STATIC_BUDGET_BASE_BLOB_PATH, "default")


def fetch_static_budget_ro(
    *, force_refresh: bool = False,
) -> DemandSummarySnapshot:
    """Return the latest ``Static_Budget_RO_Lbs.csv`` as a snapshot.

    Backs the "Total Budget" column on every R&O leaf of the Demand
    Pivot Summary.  The pivot builder consumes this via
    :func:`build_budget_lookup`, which sums ``FY Lbs. Total`` per
    ``(Portfolio Major, Format)`` bucket (the per-program ``Format``
    column in this CSV is the same dimension as ``Supply Format``
    elsewhere in the model).

    See :func:`fetch_mgmt_plan_full` for the ``force_refresh`` contract.
    """
    if force_refresh:
        _cached_fetch.clear()
    return _cached_fetch(_STATIC_BUDGET_RO_BLOB_PATH, "default")


# ── Display-side date coercion ──────────────────────────────────────────────
#
# The source CSV stores ``Start of Month`` as an Excel serial integer.
# Showing those raw in the preview table is unfriendly; the pivot
# logic below already parses them via ``_coerce_start_of_month``, so
# the preview frame can use the same primitive to convert in place
# without changing what the raw download path serves.

def _coerce_demand_dates_for_display(
    snapshot: DemandSummarySnapshot,
) -> DemandSummarySnapshot:
    """Return *snapshot* with ``Start of Month`` coerced to ``datetime64``.

    Pure: returns a new snapshot (the underlying DataFrame is
    shallow-copied and the affected column overwritten).  Idempotent
    — calling twice is a no-op once the column is already a datetime
    dtype.  Tolerates the column being absent (some files might not
    have it, in which case the caller still gets a valid snapshot
    back, with a debug log entry).
    """
    df = snapshot.df
    if COL_START_OF_MONTH not in df.columns:
        return snapshot
    if pd.api.types.is_datetime64_any_dtype(df[COL_START_OF_MONTH]):
        return snapshot  # Already a date dtype — nothing to do.

    out = df.copy()
    out[COL_START_OF_MONTH] = (
        out[COL_START_OF_MONTH]
        .map(_coerce_start_of_month)
        .map(lambda d: pd.Timestamp(d) if d is not None else pd.NaT)
    )
    # Coerce the whole column to ``datetime64[ns]`` so Streamlit's
    # ``st.dataframe`` renders it via DateColumn defaults (YYYY-MM-DD).
    out[COL_START_OF_MONTH] = pd.to_datetime(
        out[COL_START_OF_MONTH], errors="coerce",
    )
    return DemandSummarySnapshot(
        df=out,
        etag=snapshot.etag,
        size=snapshot.size,
        last_modified=snapshot.last_modified,
        blob_path=snapshot.blob_path,
    )


# ── Raw-bytes download path ──────────────────────────────────────────────────
#
# The CSV the planner downloads from this section must be a byte-for-
# byte copy of what is sitting in OneLake — NOT a re-serialisation of
# our parsed DataFrame.  Re-serialising would drop trailing newlines,
# coerce numeric types, rewrite quoting, etc., and the planner's
# downstream tools (Excel pivot tables) are unforgiving about those
# changes.  The fetchers above return a parsed frame for preview /
# inspection; downloads go through this raw path instead.

def fetch_raw_bytes(blob_path: str) -> bytes:
    """Return the raw bytes of a Demand Summary blob, untouched.

    Raises :class:`DemandSummaryError` when the blob is missing or the
    storage SDK errors out.  Used by the Streamlit download buttons so
    the user gets an unmodified copy of the source CSV.
    """
    try:
        raw, _etag = read_bytes(_SECRETS_SECTION, blob_path)
    except LakehouseIOError as exc:
        raise DemandSummaryError(
            f"Could not download 'Files/{blob_path}' from Microsoft Fabric: "
            f"{exc}"
        ) from exc
    if raw is None:
        raise DemandSummaryError(
            f"OneLake blob 'Files/{blob_path}' does not exist."
        )
    return raw


def mgmt_plan_full_blob_path() -> str:
    """Return the POSIX path of the management-plan-full CSV under ``Files/``."""
    return _MGMT_PLAN_FULL_BLOB_PATH


def total_item_level_demand_blob_path() -> str:
    """Return the POSIX path of the total-item-level-demand CSV under ``Files/``."""
    return _TOTAL_ITEM_LEVEL_DEMAND_BLOB_PATH


# ── Cache management ─────────────────────────────────────────────────────────

def clear_demand_summary_cache() -> None:
    """Invalidate the cached snapshots for EVERY Demand Summary CSV.

    Covers ``qry_mgmt_plan_full.csv``,
    ``qry_total_item_level_demand.csv``, and ``qry_pdh.csv`` — they
    share a single ``@st.cache_data`` slot (the cached impl is the
    same function, keyed by blob path), so one ``clear()`` call
    invalidates the whole family.

    Wired to the section's "🔄 Refresh from Fabric" button so a single
    click forces fresh reads of every file on the next render.  Exposed
    as a public function (rather than reaching into the cached impl from
    the page) so the page doesn't need to know about Streamlit's
    ``.clear()`` decorator API.
    """
    _cached_fetch.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Demand Pivot Summary
# ─────────────────────────────────────────────────────────────────────────────
#
# A hierarchical roll-up of ``qry_total_item_level_demand.csv`` that
# mirrors the planner's Excel pivot (see screenshot in the chat).
#
# Row hierarchy
# -------------
# ``Portfolio Major`` (level 0)
#   → ``Forecast Type`` ∈ {"Base Plan", "R&O"} (level 1)
#       → ``Supply Format`` (level 2)
#
# A row whose Portfolio Major is blank is rolled up under the literal
# label ``"(blank)"`` — matches Excel's convention.  Rows whose every
# month value is zero are dropped from the output frame (the planner
# never looks at empty rows in the source pivot).
#
# Column shape
# ------------
# One column per distinct month (e.g., ``2026-04-01``, ``2026-05-01``,
# …) plus a trailing ``Total`` column.  Values are in **millions of
# pounds**, rounded to one decimal place — matches the screenshot's
# display precision and keeps the on-screen widths readable.
#
# Source columns (from the planner's confirmation)
# ------------------------------------------------
# ``Start of Month``      — Excel serial (e.g., 46174 = 2026-05-01)
# ``Portfolio Major``     — text (e.g., "Butter", "Cultured", …)
# ``Supply Format``       — text (e.g., "Large Carton", "Tanker", …)
# ``Forecast Type``       — text ∈ {"Base Plan", "R&O", …}
# ``Demand Plan Pounds``  — numeric (raw lbs; converted to millions)

# Source column names.  Pinned as module constants so they appear in
# one place — change here if the upstream schema ever drifts.
COL_START_OF_MONTH: str   = "Start of Month"
COL_ITEM: str             = "Item"
COL_PORTFOLIO_MAJOR: str  = "Portfolio Major"
COL_SUPPLY_FORMAT: str    = "Supply Format"
COL_FORECAST_TYPE: str    = "Forecast Type"
COL_DEMAND_LBS: str       = "Demand Plan Pounds"

# Column-name CANDIDATES for the two lookup CSVs.  ``qry_pdh`` and
# ``RO_Item_Master`` are owned by different upstream teams and have
# historically used slightly different column spellings — we probe
# the most likely names instead of hard-failing on a single literal.
# First match wins (the lists are intentionally ordered most-likely
# first).  Add new spellings here rather than special-casing in the
# join logic.
_PDH_ITEM_KEY_CANDIDATES: tuple[str, ...] = (
    "Item No", "Item", "Item Number", "Item #", "ItemNo", "Item_No",
)
_PDH_SFMT_CANDIDATES: tuple[str, ...] = (
    "Supply Format", "Supply_Format", "SupplyFormat", "SFmt",
)
_ITEM_MASTER_ITEM_KEY_CANDIDATES: tuple[str, ...] = (
    "Item #", "Item No", "Item", "Item Number", "ItemNo",
)
_ITEM_MASTER_SFMT_CANDIDATES: tuple[str, ...] = (
    "Supply Format", "Supply_Format", "SupplyFormat", "SFmt",
)

# Canonical labels for the Forecast Type bucket.  Anything that does
# not normalise to one of these two falls into "Base Plan" by default
# (the planner's spec treats unknown / unclassified rows as base).
FORECAST_BASE_PLAN: str   = "Base Plan"
FORECAST_R_AND_O: str     = "R&O"

# Excel's date epoch — 1899-12-30 corrects for the Lotus-1-2-3 leap-
# year bug that Excel inherited.  Same value used by the RO Comparison
# connector for its Excel-serial parsing.
_EXCEL_EPOCH: pd.Timestamp = pd.Timestamp("1899-12-30")

# Sentinel label used when a row's Portfolio Major is blank — mirrors
# the literal "(blank)" Excel displays in the pivot.
PMAJ_BLANK_LABEL: str = "(blank)"

# Display column appended to the right of every month column in the
# pivot output.  Held here so the page and the saved CSV agree on the
# spelling.
TOTAL_COLUMN_LABEL: str = "Total"

# "Total Budget" column appended to the right of the Total column —
# carries the per-row annual budget (in millions of lbs) sourced from
# the two static-budget CSVs.  Constant so the pivot frame, the
# downloaded CSV, the dynamic-subtotal frame, and the page renderer
# all agree on the same spelling.
TOTAL_BUDGET_COLUMN_LABEL: str = "Total Budget"

# Number of pounds in one million — extracted as a constant so the
# units conversion is grep-able in one place.
_LBS_PER_MILLION: float = 1_000_000.0

# Tolerance for the "row is entirely empty" check, expressed in
# millions of pounds.  Half the display precision (which is 0.1 M)
# so a row that rounds to 0.0 in every month but holds tiny FP noise
# is correctly considered empty.
_EMPTY_ROW_TOLERANCE_M: float = 0.05


class DemandPivotError(RuntimeError):
    """Raised on any Demand Pivot build / parse failure.

    Distinct from :class:`DemandSummaryError` so the page can show a
    pivot-specific error banner without losing the connector's own
    error path (the connector reads the source CSV; the pivot
    transforms it — two failure modes deserve two banners).
    """


@dataclass(frozen=True)
class DemandPivotFilters:
    """User-selected filters that narrow the pivot before roll-up.

    All four fields are optional — passing ``None`` means "no filter
    on this dimension" (every value contributes).  The pivot builder
    applies all four conjunctively (AND), which matches the planner's
    mental model when picking through Excel slicers.

    Attributes
    ----------
    portfolio_majors
        Whitelist of Portfolio Major values to include.  Pass the
        literal ``PMAJ_BLANK_LABEL`` to keep blank-PMaj rows.
    supply_formats
        Whitelist of Supply Format values to include.
    start_month, end_month
        Inclusive bounds for the ``Start of Month`` column.  Passing
        ``None`` for either bound leaves that side open.
    """
    portfolio_majors: Optional[tuple[str, ...]] = None
    supply_formats: Optional[tuple[str, ...]] = None
    start_month: Optional[date] = None
    end_month: Optional[date] = None


@dataclass(frozen=True)
class DemandPivotResult:
    """Output of :func:`build_demand_pivot`.

    Attributes
    ----------
    pivot
        Wide-format DataFrame with one row per (PMaj, ForecastType,
        SFmt or subtotal) and one column per month + a trailing
        ``Total`` column + a final ``Total Budget`` column.  Monthly
        + Total values are in **millions of pounds** rounded to 1
        decimal; the Total Budget column holds the per-row annual
        budget (in millions) sourced from the two static-budget
        CSVs.  Internal metadata lives in three hidden columns:
        ``_row_id``, ``_indent``, ``_is_subtotal``.
    month_columns
        Ordered list of the month-column names actually present in
        ``pivot`` (excludes ``Total`` and ``Total Budget``).  Use
        this to drive the column_config of the page renderer without
        re-parsing.
    base_plan_totals, r_and_o_totals
        Two single-row DataFrames keyed by ``month_columns`` + the
        trailing ``Total`` + ``Total Budget`` columns, holding the
        dynamic per-month totals.  Rendered as the table's footer.
        Always present even when the pivot is empty — saves the page
        a guard clause.
    budget_totals
        Single-row DataFrame bundling Base + R&O annual budgets into
        one number under the ``"Total Budget (Base + R&O)"`` label.
        Lives in the same shape as ``base_plan_totals`` /
        ``r_and_o_totals`` so the page can stack all three in one
        footer table.  Monthly cells are blank (NaN) because budget
        is annual and has no monthly slice.
    budget_total_m
        Bundled annual budget (Base + R&O, post-filter) in millions
        of lbs.  Drives the chart's dotted budget line — the chart
        renderer slices this by ``len(month_columns)`` to plot a
        per-month equivalent.
    has_budget_data
        ``True`` iff at least one budget leaf contributed to the
        visible pivot.  ``False`` when both source CSVs are empty /
        unavailable / have no rows that survive the user filters;
        the page hides the budget column + line in that case.
    chart_long
        Long-form DataFrame ``(Month, Forecast Type, Pounds_M)`` ready
        to hand to Plotly for the stacked area chart.  Contains only
        the rows that passed the user filters; the chart caption can
        derive its date range from this frame.
    """
    pivot: pd.DataFrame
    month_columns: tuple[str, ...]
    base_plan_totals: pd.DataFrame
    r_and_o_totals: pd.DataFrame
    budget_totals: pd.DataFrame
    budget_total_m: float
    has_budget_data: bool
    chart_long: pd.DataFrame


# Internal hidden-column names — mirror the convention used by
# ``ro_summary_report`` so anyone reading both modules sees the same
# pattern for "structural metadata, not displayed to the user".
_COL_ROW_ID: str       = "_row_id"
_COL_INDENT: str       = "_indent"
_COL_IS_SUBTOTAL: str  = "_is_subtotal"
_HIDDEN_COLS: tuple[str, ...] = (_COL_ROW_ID, _COL_INDENT, _COL_IS_SUBTOTAL)

# Indentation unit used when rendering the label column.  Two
# non-breaking spaces per indent level — that's what reads cleanly in
# Streamlit's data viewer at typical font sizes.  NBSP (\u00a0) is
# used instead of ASCII space because Streamlit's table widget
# collapses runs of regular spaces.
_INDENT_UNIT: str = "\u00a0\u00a0\u00a0\u00a0"


def _coerce_start_of_month(value) -> Optional[date]:
    """Best-effort coerce *value* into a Python :class:`date`.

    Accepts every shape ``Start of Month`` has historically taken:

    * Excel serial (int or float, e.g. ``46174``)
    * ISO / MDY / YMD string (``"2026-05-01"``, ``"5/1/2026"``)
    * Already-typed :class:`datetime` / :class:`pd.Timestamp` / :class:`date`

    Returns ``None`` on blanks, NaT, and unparseable input — never
    raises so callers can ``.map(_coerce_start_of_month)`` safely.
    The returned value is always anchored at the FIRST day of the
    month so month-bound comparisons line up.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(value, datetime):
        return value.date().replace(day=1)
    if isinstance(value, pd.Timestamp):
        return value.date().replace(day=1)
    if isinstance(value, date):
        return value.replace(day=1)

    # Excel serial path — accept numeric or numeric-string.
    try:
        as_float = float(value)
        # ``> 1`` guards against zero / boolean-ish inputs being read
        # as 1899-12-31.  Real Demand-Plan serials are always > 1.
        if as_float > 1:
            ts = _EXCEL_EPOCH + pd.Timedelta(days=int(as_float))
            return ts.date().replace(day=1)
    except (TypeError, ValueError):
        pass

    # Generic textual parser — pandas handles ISO, MDY, YMD, etc.
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if pd.notna(ts):
            return ts.date().replace(day=1)
    except (TypeError, ValueError):
        pass

    return None


def _normalise_forecast_type(value) -> str:
    """Normalise a raw Forecast Type cell into ``"Base Plan"`` or ``"R&O"``.

    Comparison is case-insensitive and tolerant of common spelling /
    spacing variants (``"r&o"``, ``"R & O"``, ``"ro"``).  Anything
    that does not match the R&O patterns falls into Base Plan — per
    the planner's spec that treats unclassified / unknown rows as
    the default ("Base Plan" is the conservative bucket for "we
    plan to ship this").
    """
    if value is None:
        return FORECAST_BASE_PLAN
    try:
        if pd.isna(value):
            return FORECAST_BASE_PLAN
    except (TypeError, ValueError):
        pass
    s = str(value).strip().lower().replace(" ", "")
    if s in {"r&o", "ro", "r+o", "r/o", "risksandopportunities"}:
        return FORECAST_R_AND_O
    return FORECAST_BASE_PLAN


def _ensure_required_columns(df: pd.DataFrame) -> None:
    """Raise :class:`DemandPivotError` when a required source column is missing.

    Centralising the schema check keeps the build pipeline below
    short and gives the planner a single, actionable error when the
    upstream CSV schema drifts.  ``Supply Format`` is NOT required
    here — it's enriched from a separate lookup (see
    :func:`build_supply_format_lookup`) so the demand CSV is allowed
    to omit it.
    """
    required = (
        COL_START_OF_MONTH, COL_ITEM, COL_PORTFOLIO_MAJOR,
        COL_FORECAST_TYPE, COL_DEMAND_LBS,
    )
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise DemandPivotError(
            f"qry_total_item_level_demand.csv is missing required column(s): "
            f"{missing!r}.  Available columns: "
            f"{list(df.columns)!r}.  Check the upstream Fabric query — "
            "the pivot needs at minimum: "
            f"{list(required)!r}.  Supply Format is enriched from a "
            "separate lookup (qry_pdh / RO_Item_Master)."
        )


def _normalise_item_key(value) -> str:
    """Return a canonical string form of an item identifier.

    Joins between the three CSVs are unreliable when one side stores
    item numbers as numeric (e.g. ``370072`` int / ``370072.0`` float)
    and the other as text (``"370072"``).  Coercing to "string with
    trailing-``.0``-stripped" normalises every shape we have observed:

    * ``370072`` int     → ``"370072"``
    * ``370072.0`` float → ``"370072"``
    * ``" 370072 "`` str → ``"370072"``
    * ``"P-370072"`` str → ``"P-370072"`` (unchanged)
    * ``NaN`` / ``None`` → ``""`` (empty key — won't match anything)
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    if not s:
        return ""
    # Strip the trailing ``.0`` from floats-that-are-integers so the
    # join key matches the int-typed side without surprise.
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        s = s[:-2]
    return s


def _resolve_column(
    df: pd.DataFrame, candidates: tuple[str, ...],
) -> Optional[str]:
    """Return the first column name in *candidates* that exists in *df*.

    Case-sensitive — Fabric column names are usually exact-cased and
    a fuzzy match risks picking up an unrelated column on a name
    collision.  Returns ``None`` when no candidate matches; callers
    treat that as "this source contributes nothing" rather than an
    error so the cascade can degrade gracefully.
    """
    if df is None or df.empty:
        return None
    for c in candidates:
        if c in df.columns:
            return c
    return None


def build_supply_format_lookup(
    pdh_df: Optional[pd.DataFrame],
    item_master_df: Optional[pd.DataFrame],
) -> dict[str, str]:
    """Return a ``{item_key -> supply_format}`` lookup with a two-tier cascade.

    Cascade order (per planner spec)
    ---------------------------------
    1. **Primary** — ``qry_pdh.csv`` keyed on its item-number column
       (auto-detected from :data:`_PDH_ITEM_KEY_CANDIDATES`).
    2. **Fallback** — ``RO_Item_Master.csv`` keyed on its item-number
       column (auto-detected from :data:`_ITEM_MASTER_ITEM_KEY_CANDIDATES`).
       Only items missing from the primary tier consult the fallback.

    Returns
    -------
    dict
        ``{normalised_item_key -> supply_format_string}``.  Keys are
        normalised via :func:`_normalise_item_key` so the caller can
        look up using whatever native dtype the demand frame carries
        (int / float / str) by normalising the same way at the call
        site.  Returns an empty dict when both inputs are unusable
        — callers treat that as "every item's Supply Format is blank".

    Robustness
    ----------
    * Either input may be ``None``, empty, or missing one of the
      required columns; we silently skip that tier rather than
      raising, so the pivot keeps working when (e.g.) ``qry_pdh.csv``
      is mid-publish.
    * Multiple rows per item on the same tier: last row wins (matches
      ``dict`` insertion semantics).  Pivot consumers only need a
      stable answer per item; the planner can audit the source CSV
      directly if a multi-row item is surprising.
    """
    lookup: dict[str, str] = {}

    # ── Fallback tier first ──────────────────────────────────────
    #
    # We populate the fallback tier first and then OVERWRITE with
    # the primary tier on top.  Net effect: any item present in the
    # primary tier takes its value from there (one ``dict.update``
    # at the end is the most idiomatic implementation of the spec's
    # "primary wins, fallback covers gaps" rule).
    fb_item_col = _resolve_column(item_master_df, _ITEM_MASTER_ITEM_KEY_CANDIDATES)
    fb_sfmt_col = _resolve_column(item_master_df, _ITEM_MASTER_SFMT_CANDIDATES)
    if (
        item_master_df is not None and not item_master_df.empty
        and fb_item_col and fb_sfmt_col
    ):
        for raw_item, raw_sfmt in zip(
            item_master_df[fb_item_col], item_master_df[fb_sfmt_col],
        ):
            key = _normalise_item_key(raw_item)
            if not key:
                continue
            try:
                if pd.isna(raw_sfmt):
                    continue
            except (TypeError, ValueError):
                pass
            sfmt = str(raw_sfmt).strip()
            if sfmt:
                lookup[key] = sfmt

    # ── Primary tier — overwrites the fallback on collisions ──────
    p_item_col = _resolve_column(pdh_df, _PDH_ITEM_KEY_CANDIDATES)
    p_sfmt_col = _resolve_column(pdh_df, _PDH_SFMT_CANDIDATES)
    if (
        pdh_df is not None and not pdh_df.empty
        and p_item_col and p_sfmt_col
    ):
        primary: dict[str, str] = {}
        for raw_item, raw_sfmt in zip(pdh_df[p_item_col], pdh_df[p_sfmt_col]):
            key = _normalise_item_key(raw_item)
            if not key:
                continue
            try:
                if pd.isna(raw_sfmt):
                    continue
            except (TypeError, ValueError):
                pass
            sfmt = str(raw_sfmt).strip()
            if sfmt:
                primary[key] = sfmt
        lookup.update(primary)

    logger.info(
        "Supply Format lookup built: %s items (primary: %s, fallback: %s).",
        len(lookup),
        "yes" if p_item_col and p_sfmt_col else "skipped",
        "yes" if fb_item_col and fb_sfmt_col else "skipped",
    )
    return lookup


# ── Budget lookup (Static_Budget_Base_Lbs + Static_Budget_RO_Lbs) ───────────

# Column-name CANDIDATES per static-budget CSV.  Probing for variants
# keeps the join robust to minor schema drift (we have observed two
# different spellings of the same column across publish cycles).
# First match wins; intentionally ordered most-likely first.
_BUDGET_BASE_PMAJ_CANDIDATES: tuple[str, ...] = (
    "Portfolio Major", "PortfolioMajor", "Portfolio_Major",
)
_BUDGET_BASE_SFMT_CANDIDATES: tuple[str, ...] = (
    "Supply Format", "Supply_Format", "SupplyFormat", "SFmt", "Format",
)
_BUDGET_BASE_VALUE_CANDIDATES: tuple[str, ...] = (
    "Sum of consensus_forecast", "Consensus Forecast", "consensus_forecast",
    "Budget Lbs", "Lbs",
)
_BUDGET_RO_PMAJ_CANDIDATES: tuple[str, ...] = (
    "Portfolio Major", "PortfolioMajor", "Portfolio_Major",
)
_BUDGET_RO_SFMT_CANDIDATES: tuple[str, ...] = (
    "Format", "Supply Format", "Supply_Format", "SupplyFormat", "SFmt",
)
_BUDGET_RO_VALUE_CANDIDATES: tuple[str, ...] = (
    "FY Lbs. Total", "FY Lbs Total", "FY_Lbs_Total", "FY Total Lbs",
    "FY Lbs",  # fall-through legacy spelling
)


@dataclass(frozen=True)
class BudgetLookup:
    """Per-(PMaj, ForecastType, SFmt) annual budget in millions of lbs.

    Built by :func:`build_budget_lookup` from the two static-budget
    CSVs.  Returned by the connector layer so the pivot builder can
    join it onto every leaf without re-reading Fabric.

    Attributes
    ----------
    by_leaf
        ``{(pmaj, forecast_type, sfmt) -> annual lbs in millions}``.
        Keys use the same normalisation the pivot does (blank values
        replaced with :data:`PMAJ_BLANK_LABEL`) so a leaf lookup is a
        straight ``dict.get`` with no extra coercion.
    has_data
        ``True`` iff at least one budget row could be parsed.  The
        page renderer uses this to decide whether to surface the
        "Total Budget" column / chart line at all — when both source
        CSVs are unavailable, hiding the column keeps the pivot from
        showing a misleading all-zero budget line.

    The class is intentionally tiny — a single dict + a helper.  Any
    aggregations the pivot needs (subtotals, per-Forecast slices) are
    computed by the pivot builder itself so this struct stays
    side-effect-free and trivially memoisable.
    """
    by_leaf: dict[tuple[str, str, str], float]
    has_data: bool

    @property
    def total_m(self) -> float:
        """Sum of every leaf budget (millions of lbs)."""
        return float(sum(self.by_leaf.values()))

    def lookup_leaf(self, pmaj: str, forecast: str, sfmt: str) -> float:
        """Return the annual budget for one (PMaj, ForecastType, SFmt) leaf."""
        return float(self.by_leaf.get((pmaj, forecast, sfmt), 0.0))

    def slice_total(
        self,
        *,
        forecast: Optional[str] = None,
        pmaj_whitelist: Optional[set[str]] = None,
        sfmt_whitelist: Optional[set[str]] = None,
    ) -> float:
        """Return the annual budget summed over a subset of leaves.

        Used by the pivot builder to roll up subtotals (per Forecast
        Type, per Portfolio Major, etc.) and to drive the chart's
        dotted budget line.  Filter arguments are ANDed conjunctively;
        passing ``None`` means "include every value on that
        dimension".
        """
        total = 0.0
        for (pmaj, fc, sfmt), value in self.by_leaf.items():
            if forecast is not None and fc != forecast:
                continue
            if pmaj_whitelist is not None and pmaj not in pmaj_whitelist:
                continue
            if sfmt_whitelist is not None and sfmt not in sfmt_whitelist:
                continue
            total += value
        return total


def _norm_pmaj(value) -> str:
    """Return a Portfolio-Major value normalised to the pivot's bucket key."""
    if value is None:
        return PMAJ_BLANK_LABEL
    try:
        if pd.isna(value):
            return PMAJ_BLANK_LABEL
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    return s if s else PMAJ_BLANK_LABEL


def _norm_sfmt(value) -> str:
    """Return a Supply-Format value normalised to the pivot's leaf key."""
    # Same normalisation as PMaj — the pivot uses ``PMAJ_BLANK_LABEL``
    # for every "(blank)" dimension regardless of column name.
    return _norm_pmaj(value)


def build_budget_lookup(
    base_df: Optional[pd.DataFrame],
    ro_df: Optional[pd.DataFrame],
) -> BudgetLookup:
    """Return a :class:`BudgetLookup` built from the two static-budget CSVs.

    Aggregation
    -----------
    * **Base tier** (``Static_Budget_Base_Lbs.csv``): sum
      ``Sum of consensus_forecast`` per
      ``(Portfolio Major, Supply Format)``.  Emits one leaf per
      group, keyed under :data:`FORECAST_BASE_PLAN`.
    * **R&O tier** (``Static_Budget_RO_Lbs.csv``): sum
      ``FY Lbs. Total`` per ``(Portfolio Major, Format)``.  Emits
      one leaf per group, keyed under :data:`FORECAST_R_AND_O`.

    Values are converted from raw lbs to **millions of lbs** so the
    Total Budget column lines up unit-for-unit with the rest of the
    pivot (which is also in millions).

    Robustness
    ----------
    Either source frame may be ``None`` / empty / missing one of its
    required columns — in those cases that tier silently contributes
    nothing, the cascade keeps building from the other tier, and
    :attr:`BudgetLookup.has_data` flips to ``False`` only when BOTH
    tiers degenerate.  Same "graceful-degradation" pattern used by
    :func:`build_supply_format_lookup`.
    """
    by_leaf: dict[tuple[str, str, str], float] = {}

    # ── Base tier ────────────────────────────────────────────────
    base_pmaj_col = _resolve_column(base_df, _BUDGET_BASE_PMAJ_CANDIDATES)
    base_sfmt_col = _resolve_column(base_df, _BUDGET_BASE_SFMT_CANDIDATES)
    base_val_col  = _resolve_column(base_df, _BUDGET_BASE_VALUE_CANDIDATES)
    if (
        base_df is not None and not base_df.empty
        and base_pmaj_col and base_sfmt_col and base_val_col
    ):
        # Vectorised aggregation: groupby then iterate, far faster
        # than the equivalent per-row loop on large budget frames.
        base_work = base_df[[base_pmaj_col, base_sfmt_col, base_val_col]].copy()
        base_work[base_pmaj_col] = base_work[base_pmaj_col].map(_norm_pmaj)
        base_work[base_sfmt_col] = base_work[base_sfmt_col].map(_norm_sfmt)
        base_work[base_val_col]  = pd.to_numeric(
            base_work[base_val_col], errors="coerce",
        ).fillna(0.0)
        grouped = (
            base_work.groupby([base_pmaj_col, base_sfmt_col], dropna=False)
                     [base_val_col].sum()
        )
        for (pmaj, sfmt), lbs in grouped.items():
            key = (str(pmaj), FORECAST_BASE_PLAN, str(sfmt))
            by_leaf[key] = by_leaf.get(key, 0.0) + float(lbs) / _LBS_PER_MILLION

    # ── R&O tier ─────────────────────────────────────────────────
    ro_pmaj_col = _resolve_column(ro_df, _BUDGET_RO_PMAJ_CANDIDATES)
    ro_sfmt_col = _resolve_column(ro_df, _BUDGET_RO_SFMT_CANDIDATES)
    ro_val_col  = _resolve_column(ro_df, _BUDGET_RO_VALUE_CANDIDATES)
    if (
        ro_df is not None and not ro_df.empty
        and ro_pmaj_col and ro_sfmt_col and ro_val_col
    ):
        ro_work = ro_df[[ro_pmaj_col, ro_sfmt_col, ro_val_col]].copy()
        ro_work[ro_pmaj_col] = ro_work[ro_pmaj_col].map(_norm_pmaj)
        ro_work[ro_sfmt_col] = ro_work[ro_sfmt_col].map(_norm_sfmt)
        ro_work[ro_val_col]  = pd.to_numeric(
            ro_work[ro_val_col], errors="coerce",
        ).fillna(0.0)
        grouped = (
            ro_work.groupby([ro_pmaj_col, ro_sfmt_col], dropna=False)
                   [ro_val_col].sum()
        )
        for (pmaj, sfmt), lbs in grouped.items():
            key = (str(pmaj), FORECAST_R_AND_O, str(sfmt))
            by_leaf[key] = by_leaf.get(key, 0.0) + float(lbs) / _LBS_PER_MILLION

    has_data = bool(by_leaf)
    logger.info(
        "Budget lookup built: %s leaf(s) (base: %s, ro: %s).",
        len(by_leaf),
        "yes" if (base_pmaj_col and base_sfmt_col and base_val_col) else "skipped",
        "yes" if (ro_pmaj_col and ro_sfmt_col and ro_val_col) else "skipped",
    )
    return BudgetLookup(by_leaf=by_leaf, has_data=has_data)


def _prepare_long_frame(
    df: pd.DataFrame,
    supply_format_lookup: Optional[dict[str, str]] = None,
) -> pd.DataFrame:
    """Return a tidy long frame ready for the pivot pipeline.

    Steps:
    1. Validate the required schema (raises on drift — Supply Format
       is NOT required because it's enriched, not sourced).
    2. Coerce ``Start of Month`` to ``date`` and drop unparseables —
       a row with no parseable date can't go into a month-bucketed
       pivot, so it's dropped with a warning logged.
    3. Coerce ``Demand Plan Pounds`` to numeric (non-numeric → 0).
    4. Normalise ``Forecast Type`` into the two canonical buckets.
    5. Stringify Portfolio Major and replace blanks with ``(blank)``.
    6. Enrich each row with Supply Format from the lookup; rows whose
       item has no Supply Format entry get the ``(blank)`` sentinel
       so they still appear in the pivot under a clearly-labelled
       "unknown format" bucket rather than vanishing.
    """
    _ensure_required_columns(df)

    keep_cols = [
        COL_START_OF_MONTH, COL_ITEM, COL_PORTFOLIO_MAJOR,
        COL_FORECAST_TYPE, COL_DEMAND_LBS,
    ]
    # Keep an in-source Supply Format column if the upstream CSV ever
    # starts carrying one — it'll override the lookup in that case
    # (single source of truth: trust the row over the join).
    if COL_SUPPLY_FORMAT in df.columns:
        keep_cols.append(COL_SUPPLY_FORMAT)
    out = df.loc[:, keep_cols].copy()

    out["__month"] = out[COL_START_OF_MONTH].map(_coerce_start_of_month)
    unparseable = int(out["__month"].isna().sum())
    if unparseable:
        logger.info(
            "Demand pivot: dropping %s row(s) with unparseable '%s' values.",
            unparseable, COL_START_OF_MONTH,
        )
        out = out.loc[out["__month"].notna()].copy()

    out["__lbs"] = pd.to_numeric(out[COL_DEMAND_LBS], errors="coerce").fillna(0.0)
    out["__forecast"] = out[COL_FORECAST_TYPE].map(_normalise_forecast_type)

    # Dimension normalisation — strip whitespace, coerce NaN/None to
    # empty string so the blank-PMaj sentinel logic is uniform.
    out["__pmaj"] = (
        out[COL_PORTFOLIO_MAJOR]
        .astype("string").fillna("").str.strip()
        .replace("", PMAJ_BLANK_LABEL)
    )

    # Supply Format enrichment via the two-tier lookup, with the
    # in-source column (when present) as an opportunistic upgrade
    # path for items the lookup doesn't know.
    lookup = supply_format_lookup or {}
    item_keys = out[COL_ITEM].map(_normalise_item_key)
    looked_up = item_keys.map(lookup).fillna("")
    if COL_SUPPLY_FORMAT in out.columns:
        # In-source values take precedence over the lookup when
        # non-blank — the demand CSV is authoritative for its own
        # rows.  This is currently a no-op (the upstream CSV doesn't
        # carry the column) but keeps the code future-proof.
        in_source = out[COL_SUPPLY_FORMAT].astype("string").fillna("").str.strip()
        sfmt_series = in_source.where(in_source.ne(""), looked_up)
    else:
        sfmt_series = looked_up
    out["__sfmt"] = (
        sfmt_series.astype("string").fillna("").str.strip()
        .replace("", PMAJ_BLANK_LABEL)
    )

    return out


def _apply_pivot_filters(
    long_df: pd.DataFrame, filters: DemandPivotFilters,
) -> pd.DataFrame:
    """Return *long_df* narrowed by *filters* (conjunctive)."""
    out = long_df
    if filters.portfolio_majors:
        out = out.loc[out["__pmaj"].isin(filters.portfolio_majors)]
    if filters.supply_formats:
        out = out.loc[out["__sfmt"].isin(filters.supply_formats)]
    if filters.start_month is not None:
        out = out.loc[out["__month"] >= filters.start_month]
    if filters.end_month is not None:
        out = out.loc[out["__month"] <= filters.end_month]
    return out


def _format_month_label(d: date) -> str:
    """Return the display label for a month column (e.g., ``2026-05``).

    Anchored at the FIRST of the month and printed without a day
    component because every month column represents a full calendar
    month — including the "01" would just clutter the header.
    """
    return d.strftime("%Y-%m")


def _build_indented_label(label: str, indent: int) -> str:
    """Return ``label`` with leading NBSPs to render the indent in Streamlit."""
    return f"{_INDENT_UNIT * indent}{label}"


def list_available_filter_values(
    df: pd.DataFrame,
    supply_format_lookup: Optional[dict[str, str]] = None,
) -> dict[str, list]:
    """Return the sort-stable distinct values for every filter widget.

    Exposed publicly so the page renderer can populate its multiselect
    widgets without re-implementing the same enumeration logic.  Pulls
    from the RAW source frame (not the post-filter frame) — selecting
    a value in one filter should NOT narrow the option list of another
    filter (matches the planner's mental model when comparing across
    formats).

    Parameters
    ----------
    df
        Raw ``qry_total_item_level_demand.csv`` frame.
    supply_format_lookup
        Output of :func:`build_supply_format_lookup` — used to enrich
        the demand frame with Supply Format values before enumerating
        the distinct list.  Pass ``None`` (the default) to skip
        enrichment; the Supply Format option list then collapses to
        just the ``(blank)`` sentinel (and any in-source values, if
        the upstream CSV ever starts carrying the column).

    Returns
    -------
    dict
        ``{"portfolio_majors": [...], "supply_formats": [...],
           "months": [date(...), ...]}``
    """
    long_df = _prepare_long_frame(df, supply_format_lookup=supply_format_lookup)
    return {
        "portfolio_majors": sorted(long_df["__pmaj"].dropna().unique().tolist()),
        "supply_formats":   sorted(long_df["__sfmt"].dropna().unique().tolist()),
        "months":           sorted(long_df["__month"].dropna().unique().tolist()),
    }


def build_demand_pivot(
    df: pd.DataFrame,
    filters: Optional[DemandPivotFilters] = None,
    *,
    supply_format_lookup: Optional[dict[str, str]] = None,
    budget_lookup: Optional[BudgetLookup] = None,
) -> DemandPivotResult:
    """Build the hierarchical Demand Pivot Summary.

    Parameters
    ----------
    df
        Raw ``qry_total_item_level_demand.csv`` frame (one row per
        Item × Month).  Must contain the columns listed in
        :func:`_ensure_required_columns`.
    filters
        Optional :class:`DemandPivotFilters` — defaults to "no
        filters" (every value contributes).
    supply_format_lookup
        Output of :func:`build_supply_format_lookup`.  Used to enrich
        each demand row with its Supply Format (the source CSV does
        not carry the column).
    budget_lookup
        Optional :class:`BudgetLookup` (from
        :func:`build_budget_lookup`).  When provided, every row in
        the pivot gains a ``Total Budget`` column populated from the
        lookup at the row's natural level (leaf, subtotal, grand-
        total).  When omitted (or empty), the column is still
        present but zero-valued, and
        :attr:`DemandPivotResult.has_budget_data` reports ``False``
        so the page can hide the column / chart line.

    Returns
    -------
    :class:`DemandPivotResult`
        Pivot frame + month-column list + footer-total frames +
        budget bundle frame + chart data.

    Algorithm
    ---------
    1. Validate + normalise the source via :func:`_prepare_long_frame`.
    2. Apply user filters (PMaj / SFmt / month range).
    3. Convert pounds to millions in one vectorised pass.
    4. Group by ``(PMaj, ForecastType, SFmt, Month)`` and sum the
       millions column → wide-form pivot (one column per month).
    5. Walk the PMaj × ForecastType groups in screenshot order to
       build the indented row sequence (leaf rows + subtotals).
       Each row's Total Budget cell is computed from
       *budget_lookup* at the row's natural aggregation level.
    6. Drop rows where every month value rounds to 0 (within
       :data:`_EMPTY_ROW_TOLERANCE_M`).  Subtotal rows are kept iff
       at least one of their leaves survives, so the pivot never
       shows a subtotal whose children all vanished.
    7. Append a ``Total`` column + a ``Total Budget`` column to every row.
    8. Build the footer totals (Base Plan / R&O / Grand Total) and
       the long-form chart frame from the **post-filter, pre-roll-up**
       millions frame — those two outputs reflect the planner's
       filter selection exactly.
    """
    filters = filters or DemandPivotFilters()
    budget = budget_lookup if budget_lookup is not None else BudgetLookup(
        by_leaf={}, has_data=False,
    )

    long_df = _prepare_long_frame(df, supply_format_lookup=supply_format_lookup)
    long_df = _apply_pivot_filters(long_df, filters)

    # Empty post-filter frame — return a fully-shaped zero-row result
    # so the page can render its "no rows match" notice without a
    # nested guard.
    if long_df.empty:
        empty_pivot = pd.DataFrame(columns=[
            "Row Label", *_HIDDEN_COLS, TOTAL_COLUMN_LABEL, TOTAL_BUDGET_COLUMN_LABEL,
        ])
        empty_chart = pd.DataFrame(columns=["Month", "Forecast Type", "Pounds_M"])
        empty_footer = pd.DataFrame()
        return DemandPivotResult(
            pivot=empty_pivot,
            month_columns=(),
            base_plan_totals=empty_footer,
            r_and_o_totals=empty_footer,
            budget_totals=empty_footer,
            budget_total_m=0.0,
            has_budget_data=False,
            chart_long=empty_chart,
        )

    # ── Convert to millions in one vectorised pass ─────────────────
    long_df = long_df.assign(__lbs_m=lambda d: d["__lbs"] / _LBS_PER_MILLION)

    # ── Wide pivot: rows = (PMaj, ForecastType, SFmt), cols = Month ─
    grouped = (
        long_df.groupby(["__pmaj", "__forecast", "__sfmt", "__month"],
                        observed=True, dropna=False)["__lbs_m"]
        .sum().reset_index()
    )
    wide = grouped.pivot_table(
        index=["__pmaj", "__forecast", "__sfmt"],
        columns="__month",
        values="__lbs_m",
        aggfunc="sum",
        fill_value=0.0,
        observed=True,
    )
    # ``pivot_table`` returns a DataFrame whose columns are date
    # objects in ascending order — exactly what we want.  Rename to
    # the display labels.
    month_dates: list[date] = list(wide.columns)
    wide.columns = pd.Index([_format_month_label(d) for d in month_dates])
    month_col_labels = tuple(wide.columns.tolist())

    # Pre-compute the post-filter PMaj / SFmt whitelists for budget
    # slicing — the chart's dotted budget line + the footer "Total
    # Budget" subtotal must reflect EXACTLY the buckets that survived
    # the filters (otherwise the line would drift to a value the
    # planner can't tie back to the pivot rows).
    visible_pmajs = {str(p) for p in wide.index.get_level_values(0).unique()}
    visible_sfmts = {str(s) for s in wide.index.get_level_values(2).unique()}

    # ── Walk the hierarchy to assemble the display rows ───────────
    #
    # PMaj order: alphabetical, with ``(blank)`` always last (matches
    # the Excel screenshot).  Within each PMaj: Base Plan first then
    # R&O.  Within each (PMaj, Forecast Type): SFmt alphabetical.
    pmaj_values = sorted({p for p in wide.index.get_level_values(0)})
    if PMAJ_BLANK_LABEL in pmaj_values:
        pmaj_values = [p for p in pmaj_values if p != PMAJ_BLANK_LABEL]
        pmaj_values.append(PMAJ_BLANK_LABEL)

    forecast_order = (FORECAST_BASE_PLAN, FORECAST_R_AND_O)

    output_rows: list[dict] = []
    next_row_id = 0

    def _make_row(
        label: str,
        indent: int,
        values: dict[str, float],
        is_subtotal: bool,
        budget_m: float,
    ) -> dict:
        nonlocal next_row_id
        row = {
            "Row Label": _build_indented_label(label, indent),
            _COL_ROW_ID: next_row_id,
            _COL_INDENT: indent,
            _COL_IS_SUBTOTAL: is_subtotal,
        }
        next_row_id += 1
        for c in month_col_labels:
            row[c] = round(float(values.get(c, 0.0)), 1)
        row[TOTAL_COLUMN_LABEL] = round(
            float(sum(values.get(c, 0.0) for c in month_col_labels)), 1,
        )
        row[TOTAL_BUDGET_COLUMN_LABEL] = round(float(budget_m), 1)
        return row

    def _is_empty_values(values: dict[str, float]) -> bool:
        return all(
            abs(float(values.get(c, 0.0))) <= _EMPTY_ROW_TOLERANCE_M
            for c in month_col_labels
        )

    for pmaj in pmaj_values:
        # PMaj-level subtotal (sum across both Forecast Types + every SFmt).
        try:
            pmaj_slice = wide.xs(pmaj, level=0, drop_level=False)
        except KeyError:
            continue
        pmaj_totals = pmaj_slice.sum(axis=0).to_dict()
        if _is_empty_values(pmaj_totals):
            # Skip the whole PMaj branch — saves rendering empty
            # subtotals AND every empty leaf below.
            continue

        # PMaj-level budget: sum every leaf for this PMaj across both
        # Forecast Types, restricted to visible SFmts so the column
        # reconciles with the on-screen leaves.
        pmaj_budget_m = budget.slice_total(
            pmaj_whitelist={pmaj}, sfmt_whitelist=visible_sfmts,
        )
        pmaj_idx = len(output_rows)
        output_rows.append(_make_row(
            pmaj, 0, pmaj_totals, is_subtotal=True, budget_m=pmaj_budget_m,
        ))

        pmaj_has_visible_child = False
        for forecast in forecast_order:
            # Forecast-Type subtotal (sum across every SFmt in this PMaj).
            try:
                f_slice = wide.xs((pmaj, forecast), level=(0, 1), drop_level=False)
            except KeyError:
                continue
            f_totals = f_slice.sum(axis=0).to_dict()
            if _is_empty_values(f_totals):
                continue

            # Forecast-Type budget at the (PMaj, ForecastType) level.
            f_budget_m = budget.slice_total(
                forecast=forecast,
                pmaj_whitelist={pmaj},
                sfmt_whitelist=visible_sfmts,
            )
            output_rows.append(
                _make_row(forecast, 1, f_totals, is_subtotal=True, budget_m=f_budget_m),
            )

            f_has_visible_leaf = False
            sfmts = sorted({s for s in f_slice.index.get_level_values(2)})
            if PMAJ_BLANK_LABEL in sfmts:
                sfmts = [s for s in sfmts if s != PMAJ_BLANK_LABEL]
                sfmts.append(PMAJ_BLANK_LABEL)
            for sfmt in sfmts:
                leaf_values = f_slice.xs(sfmt, level=2).iloc[0].to_dict()
                if _is_empty_values(leaf_values):
                    continue
                leaf_budget_m = budget.lookup_leaf(pmaj, forecast, sfmt)
                output_rows.append(
                    _make_row(
                        sfmt, 2, leaf_values, is_subtotal=False,
                        budget_m=leaf_budget_m,
                    ),
                )
                f_has_visible_leaf = True

            if not f_has_visible_leaf:
                # Forecast-Type subtotal is non-zero but every leaf
                # rounded to zero — keep the subtotal anyway (we
                # already vetted ``f_totals`` is non-empty).
                pass
            pmaj_has_visible_child = True

        if not pmaj_has_visible_child:
            # All forecast-type branches empty — pop the PMaj header
            # we just added to keep the table tidy.  Roll back next_row_id
            # so subsequent IDs stay densely packed.
            output_rows.pop(pmaj_idx)
            next_row_id -= 1

    # ── Grand Total row + visible budget totals ──────────────────
    grand_budget_base_m = budget.slice_total(
        forecast=FORECAST_BASE_PLAN,
        pmaj_whitelist=visible_pmajs,
        sfmt_whitelist=visible_sfmts,
    )
    grand_budget_ro_m = budget.slice_total(
        forecast=FORECAST_R_AND_O,
        pmaj_whitelist=visible_pmajs,
        sfmt_whitelist=visible_sfmts,
    )
    grand_budget_total_m = grand_budget_base_m + grand_budget_ro_m

    if output_rows:
        grand_totals = wide.sum(axis=0).to_dict()
        output_rows.append(
            _make_row(
                "Grand Total", 0, grand_totals, is_subtotal=True,
                budget_m=grand_budget_total_m,
            ),
        )

    pivot = pd.DataFrame(output_rows)

    # ── Footer totals (Base Plan / R&O / Total Budget — dynamic per filter) ──
    #
    # Built from the post-filter ``long_df`` so they always reconcile
    # to whatever is currently on screen.  Stored as a single-row
    # frame so the page can render them via ``st.dataframe`` with
    # the exact same column_config as the pivot.
    footer_grouped = (
        long_df.groupby(["__forecast", "__month"], observed=True)["__lbs_m"]
        .sum().reset_index()
    )
    footer_wide = footer_grouped.pivot_table(
        index="__forecast", columns="__month",
        values="__lbs_m", aggfunc="sum", fill_value=0.0,
    )
    footer_wide.columns = pd.Index(
        [_format_month_label(d) for d in footer_wide.columns]
    )
    # Ensure both rows exist (a filter narrowing to one type would
    # otherwise drop the other from the footer entirely).
    for forecast in (FORECAST_BASE_PLAN, FORECAST_R_AND_O):
        if forecast not in footer_wide.index:
            footer_wide.loc[forecast] = 0.0

    def _row_to_footer_df(
        label: str, series: pd.Series, budget_m: float,
    ) -> pd.DataFrame:
        """Return a single-row footer frame with rounded values + Total + Total Budget."""
        values = {c: round(float(series.get(c, 0.0)), 1) for c in month_col_labels}
        values[TOTAL_COLUMN_LABEL] = round(
            float(sum(series.get(c, 0.0) for c in month_col_labels)), 1,
        )
        values[TOTAL_BUDGET_COLUMN_LABEL] = round(float(budget_m), 1)
        # The label uses the "Row Label" column name so the footer
        # frame slots underneath the pivot with identical schema.
        return pd.DataFrame([{"Row Label": label, **values}])

    base_plan_totals = _row_to_footer_df(
        "Total Base Plan",
        footer_wide.loc[FORECAST_BASE_PLAN],
        grand_budget_base_m,
    )
    r_and_o_totals = _row_to_footer_df(
        "Total R&O",
        footer_wide.loc[FORECAST_R_AND_O],
        grand_budget_ro_m,
    )

    # Bundled Budget row — what the planner asked for ("include a
    # Total Budget in the dynamic subtotals (bundle budget base and
    # budget RO)").  Monthly cells are intentionally blank / NaN
    # because budget is an annual figure with no monthly slice — we
    # don't want to imply a 1/12-style decomposition the source data
    # doesn't support.  The Total Budget column carries the bundled
    # annual figure.
    budget_label = f"{TOTAL_BUDGET_COLUMN_LABEL} (Base + R&O)"
    budget_totals_values: dict[str, float] = {
        c: float("nan") for c in month_col_labels
    }
    budget_totals_values[TOTAL_COLUMN_LABEL] = float("nan")
    budget_totals_values[TOTAL_BUDGET_COLUMN_LABEL] = round(
        float(grand_budget_total_m), 1,
    )
    budget_totals = pd.DataFrame(
        [{"Row Label": budget_label, **budget_totals_values}]
    )

    # ── Long-form chart frame (Month × Forecast Type × Pounds_M) ──
    #
    # The chart in the screenshot is a stacked area chart of Base
    # Plan + R&O monthly totals.  Plotly Express's ``area`` consumes
    # tidy / long-form data with one column per visual encoding, so
    # we emit that shape directly here to avoid a second reshape in
    # the page renderer.
    chart_long = footer_grouped.rename(
        columns={"__forecast": "Forecast Type",
                 "__month":    "Month",
                 "__lbs_m":    "Pounds_M"},
    )
    chart_long["Pounds_M"] = chart_long["Pounds_M"].round(1)
    # Preserve a stable category order so Base Plan stacks on the
    # bottom (largest series) and R&O sits on top — matches the
    # screenshot's visual hierarchy.
    chart_long["Forecast Type"] = pd.Categorical(
        chart_long["Forecast Type"],
        categories=[FORECAST_BASE_PLAN, FORECAST_R_AND_O],
        ordered=True,
    )
    chart_long = chart_long.sort_values(["Month", "Forecast Type"]).reset_index(drop=True)

    return DemandPivotResult(
        pivot=pivot,
        month_columns=month_col_labels,
        base_plan_totals=base_plan_totals,
        r_and_o_totals=r_and_o_totals,
        budget_totals=budget_totals,
        budget_total_m=float(grand_budget_total_m),
        has_budget_data=bool(budget.has_data and grand_budget_total_m > 0),
        chart_long=chart_long,
    )


def pivot_for_download(pivot: pd.DataFrame) -> pd.DataFrame:
    """Return *pivot* with the internal metadata columns stripped.

    The on-screen pivot carries ``_row_id`` / ``_indent`` /
    ``_is_subtotal`` so the page can style it correctly.  The CSV
    the planner downloads should be clean — only the columns a human
    expects to see.  Indentation in the ``Row Label`` column is
    preserved (it's the NBSP-prefixed display string), giving the
    downloaded file the same hierarchical look as the screen.
    """
    if pivot is None or pivot.empty:
        return pd.DataFrame()
    keep = [c for c in pivot.columns if c not in _HIDDEN_COLS]
    return pivot.loc[:, keep].copy()


__all__ = [
    "DemandSummaryError",
    "DemandSummarySnapshot",
    "fetch_mgmt_plan_full",
    "fetch_total_item_level_demand",
    "fetch_pdh",
    "fetch_static_budget_base",
    "fetch_static_budget_ro",
    "fetch_raw_bytes",
    "mgmt_plan_full_blob_path",
    "total_item_level_demand_blob_path",
    "clear_demand_summary_cache",
    # Pivot surface.
    "COL_START_OF_MONTH", "COL_ITEM", "COL_PORTFOLIO_MAJOR",
    "COL_SUPPLY_FORMAT", "COL_FORECAST_TYPE", "COL_DEMAND_LBS",
    "FORECAST_BASE_PLAN", "FORECAST_R_AND_O",
    "PMAJ_BLANK_LABEL", "TOTAL_COLUMN_LABEL", "TOTAL_BUDGET_COLUMN_LABEL",
    "DemandPivotError", "DemandPivotFilters", "DemandPivotResult",
    "BudgetLookup", "build_budget_lookup",
    "build_demand_pivot",
    "build_supply_format_lookup",
    "list_available_filter_values",
    "pivot_for_download",
]
