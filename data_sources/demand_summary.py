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

    See :func:`fetch_mgmt_plan_full` for the ``force_refresh`` contract.
    """
    if force_refresh:
        _cached_fetch.clear()
    return _cached_fetch(_TOTAL_ITEM_LEVEL_DEMAND_BLOB_PATH, "default")


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
    """Invalidate the cached snapshots for BOTH Demand Summary CSVs.

    Wired to the section's "🔄 Refresh from Fabric" button so a single
    click forces fresh reads of both files on the next render.  Exposed
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
COL_PORTFOLIO_MAJOR: str  = "Portfolio Major"
COL_SUPPLY_FORMAT: str    = "Supply Format"
COL_FORECAST_TYPE: str    = "Forecast Type"
COL_DEMAND_LBS: str       = "Demand Plan Pounds"

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
        ``Total`` column.  Values are in **millions of pounds** rounded
        to 1 decimal.  Internal metadata lives in three hidden columns:
        ``_row_id``, ``_indent``, ``_is_subtotal``.
    month_columns
        Ordered list of the month-column names actually present in
        ``pivot`` (excludes ``Total``).  Use this to drive the
        column_config of the page renderer without re-parsing.
    base_plan_totals, r_and_o_totals
        Two single-row DataFrames keyed by ``month_columns`` (plus the
        trailing ``Total``) holding the dynamic per-month totals.
        Rendered as the table's footer.  Always present even when the
        pivot is empty — saves the page a guard clause.
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
    upstream CSV schema drifts.
    """
    required = (
        COL_START_OF_MONTH, COL_PORTFOLIO_MAJOR,
        COL_SUPPLY_FORMAT, COL_FORECAST_TYPE, COL_DEMAND_LBS,
    )
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise DemandPivotError(
            f"qry_total_item_level_demand.csv is missing required column(s): "
            f"{missing!r}.  Available columns: "
            f"{list(df.columns)!r}.  Check the upstream Fabric query — "
            "the pivot needs all five of: "
            f"{list(required)!r}."
        )


def _prepare_long_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return a tidy long frame ready for the pivot pipeline.

    Steps:
    1. Validate the required schema (raises on drift).
    2. Coerce ``Start of Month`` to ``date`` and drop unparseables —
       a row with no parseable date can't go into a month-bucketed
       pivot, so it's dropped with a warning logged.
    3. Coerce ``Demand Plan Pounds`` to numeric (non-numeric → 0).
    4. Normalise ``Forecast Type`` into the two canonical buckets.
    5. Stringify the dimension columns and replace blank PMaj with
       the ``(blank)`` sentinel.
    """
    _ensure_required_columns(df)

    out = df.loc[:, [
        COL_START_OF_MONTH, COL_PORTFOLIO_MAJOR,
        COL_SUPPLY_FORMAT, COL_FORECAST_TYPE, COL_DEMAND_LBS,
    ]].copy()

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
    out["__sfmt"] = (
        out[COL_SUPPLY_FORMAT]
        .astype("string").fillna("").str.strip()
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


def list_available_filter_values(df: pd.DataFrame) -> dict[str, list]:
    """Return the sort-stable distinct values for every filter widget.

    Exposed publicly so the page renderer can populate its multiselect
    widgets without re-implementing the same enumeration logic.  Pulls
    from the RAW source frame (not the post-filter frame) — selecting
    a value in one filter should NOT narrow the option list of another
    filter (matches the planner's mental model when comparing across
    formats).

    Returns
    -------
    dict
        ``{"portfolio_majors": [...], "supply_formats": [...],
           "months": [date(...), ...]}``
    """
    long_df = _prepare_long_frame(df)
    return {
        "portfolio_majors": sorted(long_df["__pmaj"].dropna().unique().tolist()),
        "supply_formats":   sorted(long_df["__sfmt"].dropna().unique().tolist()),
        "months":           sorted(long_df["__month"].dropna().unique().tolist()),
    }


def build_demand_pivot(
    df: pd.DataFrame,
    filters: Optional[DemandPivotFilters] = None,
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

    Returns
    -------
    :class:`DemandPivotResult`
        Pivot frame + month-column list + footer-total frames +
        long-form chart data.

    Algorithm
    ---------
    1. Validate + normalise the source via :func:`_prepare_long_frame`.
    2. Apply user filters (PMaj / SFmt / month range).
    3. Convert pounds to millions in one vectorised pass.
    4. Group by ``(PMaj, ForecastType, SFmt, Month)`` and sum the
       millions column → wide-form pivot (one column per month).
    5. Walk the PMaj × ForecastType groups in screenshot order to
       build the indented row sequence (leaf rows + subtotals).
    6. Drop rows where every month value rounds to 0 (within
       :data:`_EMPTY_ROW_TOLERANCE_M`).  Subtotal rows are kept iff
       at least one of their leaves survives, so the pivot never
       shows a subtotal whose children all vanished.
    7. Append a ``Total`` column to every row.
    8. Build the footer totals (Base Plan / R&O / Grand Total) and
       the long-form chart frame from the **post-filter, pre-roll-up**
       millions frame — those two outputs reflect the planner's
       filter selection exactly.
    """
    filters = filters or DemandPivotFilters()

    long_df = _prepare_long_frame(df)
    long_df = _apply_pivot_filters(long_df, filters)

    # Empty post-filter frame — return a fully-shaped zero-row result
    # so the page can render its "no rows match" notice without a
    # nested guard.
    if long_df.empty:
        empty_pivot = pd.DataFrame(columns=[
            "Row Label", *_HIDDEN_COLS, TOTAL_COLUMN_LABEL,
        ])
        empty_chart = pd.DataFrame(columns=["Month", "Forecast Type", "Pounds_M"])
        empty_footer = pd.DataFrame()
        return DemandPivotResult(
            pivot=empty_pivot,
            month_columns=(),
            base_plan_totals=empty_footer,
            r_and_o_totals=empty_footer,
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
        label: str, indent: int, values: dict[str, float], is_subtotal: bool,
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

        pmaj_idx = len(output_rows)
        output_rows.append(_make_row(pmaj, 0, pmaj_totals, is_subtotal=True))

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

            f_idx = len(output_rows)
            output_rows.append(
                _make_row(forecast, 1, f_totals, is_subtotal=True),
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
                output_rows.append(
                    _make_row(sfmt, 2, leaf_values, is_subtotal=False),
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

    # ── Grand Total row ───────────────────────────────────────────
    if output_rows:
        grand_totals = wide.sum(axis=0).to_dict()
        output_rows.append(
            _make_row("Grand Total", 0, grand_totals, is_subtotal=True),
        )

    pivot = pd.DataFrame(output_rows)

    # ── Footer totals (Base Plan / R&O — dynamic per filter) ──────
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

    def _row_to_footer_df(label: str, series: pd.Series) -> pd.DataFrame:
        """Return a single-row footer frame with rounded values + Total."""
        values = {c: round(float(series.get(c, 0.0)), 1) for c in month_col_labels}
        values[TOTAL_COLUMN_LABEL] = round(
            float(sum(series.get(c, 0.0) for c in month_col_labels)), 1,
        )
        # The label uses the "Row Label" column name so the footer
        # frame slots underneath the pivot with identical schema.
        return pd.DataFrame([{"Row Label": label, **values}])

    base_plan_totals = _row_to_footer_df(
        "Total Base Plan", footer_wide.loc[FORECAST_BASE_PLAN],
    )
    r_and_o_totals = _row_to_footer_df(
        "Total R&O", footer_wide.loc[FORECAST_R_AND_O],
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
    "fetch_raw_bytes",
    "mgmt_plan_full_blob_path",
    "total_item_level_demand_blob_path",
    "clear_demand_summary_cache",
    # Pivot surface.
    "COL_START_OF_MONTH", "COL_PORTFOLIO_MAJOR", "COL_SUPPLY_FORMAT",
    "COL_FORECAST_TYPE", "COL_DEMAND_LBS",
    "FORECAST_BASE_PLAN", "FORECAST_R_AND_O",
    "PMAJ_BLANK_LABEL", "TOTAL_COLUMN_LABEL",
    "DemandPivotError", "DemandPivotFilters", "DemandPivotResult",
    "build_demand_pivot",
    "list_available_filter_values",
    "pivot_for_download",
]
