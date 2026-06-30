"""Plan Lift Analysis — YoY Lift% data layer.

Backs the *Plan Lift Analysis* section on the Demand Planner Analytics
page.  The single metric this module exists to compute is **YoY Lift%**,
evaluated at the *series* grain (a ratio of sums — never the mean of
per-item lifts):

    For each (filter) × month:
        plan_sum  = SUM(consensus_plan_lbs)          # dp_factscurrentaps
        ship_sum  = SUM(Shipped Qty lbs)             # IBP Shipments
        numerator = plan_sum if plan_sum > 0 else ship_sum
        prior_year = ship_sum shifted +12 months     # SHIPMENTS only
        lift       = numerator / prior_year - 1      # NaN when prior_year <= 0

The prior year is **always the actual shipped volume** 12 months back —
it never uses APS / plan data, even where a plan existed in that month.
The current month may be plan-driven ("plan / actual" right of today;
"actual / actual" to the left), but its year-ago baseline is the real
shipments, so the lift answers "vs. what we actually shipped a year ago".

Module layout
-------------
1. Source identity + typed error               (constants, ``PlanLiftError``)
2. Fabric connectors (new Delta tables)         (``fetch_factscurrentaps_slim_df``,
                                                  ``fetch_dimcalendar_df``)
3. Pre-aggregation builder                      (``build_plan_lift_base``)
4. The metric                                   (``compute_yoy_lift``)
5. Small reuse-friendly helpers / constants

What is reused vs. new
----------------------
* Auth + DuckDB plumbing — :mod:`data_sources.fabric_auth` (shared token,
  shared connection with ``azure`` + ``delta`` pre-loaded).
* IBP Shipments — :func:`data_sources.ibp_official.fetch_ibp_shipments_slim_df`
  (column-projected, month-predicate-pushed).
* Item dimensions — :func:`data_sources.ro_comparison.fetch_dimitems_df`
  (the ``dp_dimitems`` Delta table).
* Corporate Group — the *single source of truth* is
  ``dp_dimcustomernames`` via
  :func:`data_sources.demand_item_customer._build_customer_num_to_corp_group_lookup`.
  BOTH sides resolve through it: shipments by ``Customer No`` and the plan
  by ``party_site_code`` (each → ``customer_num`` → ``corporate_group``).
  The native ``dp_factscurrentaps.corporate_group_code`` is intentionally
  NOT used, so the two sides can never disagree at the today boundary.
* Item-key normalisation — :func:`...demand_plan_comparison._vectorised_item_key`
  (strip → drop trailing ``.0`` → blank), the same coercion every other
  item join in this codebase uses, so ``Item No`` (shipments) and
  ``item_code`` (plan / dims) land on one key.

The builder and the metric are PURE functions (no Streamlit) so they are
unit-testable; the page owns the ``st.cache_data`` wrapping (mirroring how
the Product Line Review section caches its prepared inputs).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Mapping, Optional, Sequence

import numpy as np
import pandas as pd
import streamlit as st

from data_sources.customer_dims import fetch_dp_dimcustomernames_df
from data_sources.demand_item_customer import (
    _build_customer_num_to_corp_group_lookup,
)
from data_sources.demand_plan_comparison import _vectorised_item_key
from data_sources.demand_summary import _resolve_column
from data_sources.fabric_auth import (
    FabricAuthError,
    acquire_storage_token,
    bind_storage_token,
    duckdb_lock,
    get_duckdb_connection,
)
from data_sources.fabric_lakehouse_io import (
    LakehouseIOError,
    list_files,
    read_csv,
)


logger = logging.getLogger(__name__)


# ── 1. Source identity + error ────────────────────────────────────────────────
#
# Same workspace + lakehouse GUID pair as every other connector against
# this lakehouse (IBP, dp_dimitems, dp_dimcustomernames).  GUIDs are
# stable across display-name renames and leak no data on their own —
# see the long-form rationale in ``data_sources.ibp_official``.
_WORKSPACE_GUID = "bb11c51d-03c8-4f1b-938c-e20657a8f31d"
_LAKEHOUSE_GUID = "a01f513d-eee7-41eb-8c15-670bc40e7fc8"
_SCHEMA = "dbo"

_TABLE_FACTS = "dp_factscurrentaps"
_TABLE_CALENDAR = "dp_dimcalendar"

# Dim tables move slowly; the current-plan fact refreshes on the IBP
# cadence.  Match the 15-minute TTL used by the sibling connectors.
_CACHE_TTL_SECONDS = 15 * 60

# Sentinel applied when a customer/party-site code does not resolve to a
# Corporate Group via dp_dimcustomernames.  Kept visible (rather than
# dropped) so unmapped volume is auditable rather than silently missing.
CORP_GROUP_UNMAPPED = "(Unmapped)"
# Sentinel applied when an item is absent from dp_dimitems — its dims are
# unknown but its volume still belongs to any company-wide combo series.
DIM_UNKNOWN = "(Unknown)"


class PlanLiftError(RuntimeError):
    """Raised on any failure to read a Plan-Lift Delta source.

    Wraps the underlying deltalake / DuckDB / azure-identity exception so
    the page renders one clean error path instead of a stack trace.
    """


# ── Column-name candidates ────────────────────────────────────────────────────
#
# Probed via :func:`_resolve_column` so a one-line upstream spelling /
# casing drift is a one-line fix here rather than a silent join failure.
# Screenshots show the lowercase forms; the alternates guard against the
# usual Fabric export variations.
_APS_MONTH_CANDIDATES = ("month", "Month", "Start of Month")
_APS_PARTY_SITE_CANDIDATES = (
    "party_site_code", "Party Site Code", "PartySiteCode",
    "party_site_number", "Party Site Number",
)
_APS_ITEM_CANDIDATES = ("item_code", "Item Code", "ItemCode", "Item No", "Item No.")
_APS_PLAN_CANDIDATES = (
    "consensus_plan_lbs", "Consensus Plan Lbs", "ConsensusPlanLbs",
)

# dp_dimitems — item dimension columns surfaced as combo slicers.  ``key``
# is our internal column name; ``candidates`` are the upstream spellings.
_DIM_ITEM_CANDIDATES = ("item_code", "Item Code", "ItemCode", "Item No")
_DIM_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("portfolio_major", ("portfolio_major", "Portfolio Major", "PortfolioMajor", "Portfolio_Major")),
    ("portfolio_minor", ("portfolio_minor", "Portfolio Minor", "PortfolioMinor", "Portfolio_Minor")),
    ("supply_format", ("supply_format", "Supply Format", "SupplyFormat", "Supply_Format")),
    ("size", ("size", "Size")),
    ("taxonomy", ("taxonomy", "Taxonomy")),
    ("brand_category", ("brand_category", "Brand Category", "BrandCategory", "brand_cat")),
    ("brand_name", ("brand_name", "Brand Name", "BrandName", "brand")),
    ("milk_type", ("milk_type", "Milk Type", "MilkType")),
    ("business_unit", ("business_unit", "Business Unit", "BusinessUnit")),
    ("item_description", ("item_description", "Item Description", "ItemDescription", "item_descr")),
)

# IBP Shipments slim columns (as projected by ``fetch_ibp_shipments_slim_df``).
_SHIP_ITEM_CANDIDATES = ("Item No", "Item No.", "ItemNo", "item_code")
_SHIP_CUST_NO_CANDIDATES = ("Customer No", "Customer No.", "CustomerNo", "customer_num")
_SHIP_MONTH_CANDIDATES = ("Month", "month", "Start of Month")
_SHIP_QTY_CANDIDATES = ("Shipped Qty lbs", "Shipped Qty Lbs", "Shipped Qty", "shipped_qty_lbs")

# dp_dimcalendar — used only for the optional fiscal hover label.
_CAL_DATE_CANDIDATES = ("date", "Date")
_CAL_FY_QTR_CANDIDATES = ("fy_and_qtr", "FY and Qtr", "fy_qtr", "fiscal_qtr")

# Internal, stable column names produced by the builder.
COL_MONTH = "month"
COL_ITEM_KEY = "item_key"
COL_CORP_GROUP = "corporate_group"
COL_PLAN_LBS = "plan_lbs"
COL_SHIP_LBS = "ship_lbs"
COL_ITEM_CODE = "item_code"

# The set of columns a combo slicer may filter on (internal names).
SLICER_DIMS: tuple[str, ...] = (
    COL_CORP_GROUP,
    "portfolio_major",
    "supply_format",
    "size",
    "taxonomy",
    "brand_category",
    "brand_name",
    "portfolio_minor",
    COL_ITEM_CODE,
)


# ── 2. Fabric connectors (new Delta tables) ───────────────────────────────────

def _build_table_uri(table: str) -> str:
    """Construct the OneLake ``abfss://`` URI for one table in this lakehouse."""
    return (
        f"abfss://{_WORKSPACE_GUID}@onelake.dfs.fabric.microsoft.com/"
        f"{_LAKEHOUSE_GUID}/Tables/{_SCHEMA}/{table}"
    )


def _quote_ident(col: str) -> str:
    """Quote a SQL identifier for DuckDB (escapes embedded double quotes)."""
    return '"' + col.replace('"', '""') + '"'


def _scan(sql: str, table_uri: str) -> pd.DataFrame:
    """Run one DuckDB ``delta_scan`` query against OneLake.

    Centralises the token-bind + locked-execute boilerplate shared by both
    connectors below.  Raises :class:`PlanLiftError` on any failure.
    """
    try:
        token = acquire_storage_token()
    except FabricAuthError as exc:
        raise PlanLiftError(str(exc)) from exc
    try:
        con = get_duckdb_connection()
        with duckdb_lock():
            bind_storage_token(con, token)
            return con.execute(sql).df()
    except FabricAuthError as exc:
        raise PlanLiftError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise PlanLiftError(
            f"Could not read Delta table via DuckDB at {table_uri}.  Verify "
            f"the lakehouse identifiers, your Read access, and that the "
            f"dataflow has populated the table.  Underlying error: {exc}"
        ) from exc


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_factscurrentaps(_cache_token: str) -> pd.DataFrame:
    """Streamlit-cached slim read of ``dbo.dp_factscurrentaps``.

    Projects only the four columns the metric needs (month, party site,
    item, consensus plan lbs).  Column names are resolved from a cheap
    ``DESCRIBE`` probe so an upstream casing drift degrades to a clear
    error rather than a wrong-column silent join.
    """
    table_uri = _build_table_uri(_TABLE_FACTS)
    # Cheap schema probe — DESCRIBE reads Delta metadata only, no row scan.
    schema = _scan(f"DESCRIBE SELECT * FROM delta_scan('{table_uri}')", table_uri)
    # Resolve against the column LIST directly: ``_resolve_column`` short-
    # circuits to None on a zero-row frame, so a column-only shim would
    # spuriously report every column missing.
    available = tuple(schema["column_name"].astype(str))
    available_set = set(available)

    def _pick(candidates: tuple[str, ...]) -> Optional[str]:
        return next((c for c in candidates if c in available_set), None)

    month_col = _pick(_APS_MONTH_CANDIDATES)
    party_col = _pick(_APS_PARTY_SITE_CANDIDATES)
    item_col = _pick(_APS_ITEM_CANDIDATES)
    plan_col = _pick(_APS_PLAN_CANDIDATES)
    missing = [
        name for name, col in (
            ("month", month_col), ("party_site_code", party_col),
            ("item_code", item_col), ("consensus_plan_lbs", plan_col),
        ) if col is None
    ]
    if missing:
        raise PlanLiftError(
            f"{_TABLE_FACTS} is missing expected column(s): {', '.join(missing)}. "
            f"Available columns: {', '.join(available)}."
        )

    select = ", ".join(
        f"{_quote_ident(src)} AS {_quote_ident(dst)}"
        for src, dst in (
            (month_col, COL_MONTH), (party_col, "party_site_code"),
            (item_col, COL_ITEM_CODE), (plan_col, COL_PLAN_LBS),
        )
    )
    df = _scan(f"SELECT {select} FROM delta_scan('{table_uri}')", table_uri)
    logger.info("Loaded %s slim: %s rows.", _TABLE_FACTS, len(df))
    return df


def fetch_factscurrentaps_slim_df(*, force_refresh: bool = False) -> pd.DataFrame:
    """Return ``dbo.dp_factscurrentaps`` projected to the metric's columns.

    Columns (renamed to internal names): ``month``, ``party_site_code``,
    ``item_code``, ``plan_lbs`` (= ``consensus_plan_lbs``).

    Raises :class:`PlanLiftError` on any read failure.
    """
    if force_refresh:
        _cached_factscurrentaps.clear()
    return _cached_factscurrentaps("default")


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_dimcalendar(_cache_token: str) -> pd.DataFrame:
    """Streamlit-cached full read of ``dbo.dp_dimcalendar``.

    Small reference table; read in full (no projection) and used only to
    derive an optional ``month → fiscal-year/quarter`` hover label.
    """
    table_uri = _build_table_uri(_TABLE_CALENDAR)
    df = _scan(f"SELECT * FROM delta_scan('{table_uri}')", table_uri)
    logger.info("Loaded %s: %s rows.", _TABLE_CALENDAR, len(df))
    return df


def fetch_dimcalendar_df(*, force_refresh: bool = False) -> pd.DataFrame:
    """Return the ``dbo.dp_dimcalendar`` dimension as a DataFrame.

    Raises :class:`PlanLiftError` on any read failure.
    """
    if force_refresh:
        _cached_dimcalendar.clear()
    return _cached_dimcalendar("default")


def build_month_fiscal_labels(calendar_df: Optional[pd.DataFrame]) -> dict[pd.Timestamp, str]:
    """Return a ``month-begin Timestamp → "FY27 Q2"`` style label map.

    Best-effort: returns ``{}`` when the calendar is unavailable or lacks
    the expected columns, in which case the chart simply omits the fiscal
    annotation.  Keeps the calendar a soft dependency of the section.
    """
    if calendar_df is None or calendar_df.empty:
        return {}
    date_col = _resolve_column(calendar_df, _CAL_DATE_CANDIDATES)
    label_col = _resolve_column(calendar_df, _CAL_FY_QTR_CANDIDATES)
    if not (date_col and label_col):
        return {}
    months = _to_month_begin(calendar_df[date_col])
    labels = calendar_df[label_col].astype("string").str.strip()
    pairs = pd.DataFrame({"m": months, "lbl": labels}).dropna(subset=["m"])
    # First non-null label per month wins (the calendar repeats a label
    # across every week of the month).
    return (
        pairs.dropna(subset=["lbl"])
        .drop_duplicates(subset="m", keep="first")
        .set_index("m")["lbl"]
        .to_dict()
    )


# ── 3. Pre-aggregation builder ────────────────────────────────────────────────

@dataclass(frozen=True)
class PlanLiftBuildStats:
    """Coverage diagnostics surfaced to the planner after a build.

    Attributes
    ----------
    ship_rows, plan_rows
        Row counts of the two source frames after month/key coercion.
    corp_unmapped_ship_pct, corp_unmapped_plan_pct
        Share of shipment / plan POUNDS whose customer/party-site code did
        not resolve to a Corporate Group (landed in :data:`CORP_GROUP_UNMAPPED`).
    item_unmatched_pct
        Share of total pounds whose item is absent from ``dp_dimitems``
        (dims = :data:`DIM_UNKNOWN`).  Still counted in company-wide series.
    warnings
        Human-readable advisories for the page banner.
    """

    ship_rows: int = 0
    plan_rows: int = 0
    corp_unmapped_ship_pct: float = 0.0
    corp_unmapped_plan_pct: float = 0.0
    item_unmatched_pct: float = 0.0
    warnings: tuple[str, ...] = ()


def _to_month_begin(series: pd.Series) -> pd.Series:
    """Coerce a date-ish column to first-of-month ``datetime64`` (NaT on fail).

    Parses with ``utc=True`` so mixed tz-aware inputs (e.g. the plan's
    ``2026-07-01T00:00:00.000Z``) and tz-naive shipment timestamps both
    coerce cleanly, then drops the tz so month-begin arithmetic and
    plotting stay tz-naive.
    """
    parsed = pd.to_datetime(series, errors="coerce", utc=True)
    return parsed.dt.tz_localize(None).dt.to_period("M").dt.to_timestamp()


def _normalise_codes(series: pd.Series) -> pd.Series:
    """Normalise a numeric-ish code column to a clean string key.

    Reuses the item-key coercion (strip → drop trailing ``.0`` → blank)
    so a ``Customer No`` read as ``6514.0`` matches a ``customer_num`` of
    ``"6514"`` in ``dp_dimcustomernames``.
    """
    return _vectorised_item_key(series)


def _attach_corp_group(codes: pd.Series, lookup: Mapping[str, str]) -> pd.Series:
    """Map a normalised code Series to Corporate Group via the dim lookup.

    Unmatched / blank codes land in :data:`CORP_GROUP_UNMAPPED`.
    """
    mapped = codes.map(lookup) if lookup else pd.Series(index=codes.index, dtype="object")
    # ``valid`` is a plain bool Series (fillna removes the <NA> a failed
    # .map leaves behind) so ``.where`` never sees a nullable condition.
    valid = mapped.notna() & mapped.astype("string").str.strip().fillna("").ne("")
    return mapped.where(valid, CORP_GROUP_UNMAPPED)


def _build_item_dim_frame(dimitems_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Return a deduped ``item_key + dims`` lookup frame from ``dp_dimitems``.

    Columns: ``item_key`` plus every internal dim name in
    :data:`_DIM_FIELDS`.  Last row wins on duplicate item keys (matches the
    contract used by the RO Comparison / PLR dim joins).
    """
    dim_cols = [name for name, _ in _DIM_FIELDS]
    if dimitems_df is None or dimitems_df.empty:
        return pd.DataFrame(columns=[COL_ITEM_KEY, *dim_cols])

    item_col = _resolve_column(dimitems_df, _DIM_ITEM_CANDIDATES)
    if not item_col:
        return pd.DataFrame(columns=[COL_ITEM_KEY, *dim_cols])

    out = {COL_ITEM_KEY: _vectorised_item_key(dimitems_df[item_col])}
    blank = pd.Series([""] * len(dimitems_df), index=dimitems_df.index, dtype="object")
    for name, candidates in _DIM_FIELDS:
        col = _resolve_column(dimitems_df, candidates)
        out[name] = (
            dimitems_df[col].astype("string").str.strip().fillna("").astype("object")
            if col else blank
        )
    frame = pd.DataFrame(out)
    frame = frame.loc[frame[COL_ITEM_KEY] != ""]
    return frame.drop_duplicates(subset=COL_ITEM_KEY, keep="last").reset_index(drop=True)


def _aggregate_side(
    df: pd.DataFrame,
    *,
    item_col: str,
    code_col: str,
    month_col: str,
    qty_col: str,
    out_qty: str,
    corp_lookup: Mapping[str, str],
) -> tuple[pd.DataFrame, float]:
    """Aggregate one source (shipments or plan) to (month, item_key, corp).

    Returns ``(agg_frame, unmapped_pounds_pct)`` where the frame carries
    ``month, item_key, corporate_group, <out_qty>`` and the pct is the
    share of pounds that fell into :data:`CORP_GROUP_UNMAPPED`.
    """
    tidy = pd.DataFrame({
        COL_MONTH: _to_month_begin(df[month_col]),
        COL_ITEM_KEY: _vectorised_item_key(df[item_col]),
        COL_CORP_GROUP: _attach_corp_group(_normalise_codes(df[code_col]), corp_lookup),
        out_qty: pd.to_numeric(df[qty_col], errors="coerce").fillna(0.0),
    })
    tidy = tidy.loc[tidy[COL_MONTH].notna() & (tidy[COL_ITEM_KEY] != "")]

    total_lbs = float(tidy[out_qty].sum())
    unmapped_lbs = float(tidy.loc[tidy[COL_CORP_GROUP] == CORP_GROUP_UNMAPPED, out_qty].sum())
    unmapped_pct = (unmapped_lbs / total_lbs * 100.0) if total_lbs else 0.0

    agg = (
        tidy.groupby([COL_MONTH, COL_ITEM_KEY, COL_CORP_GROUP], as_index=False)[out_qty]
        .sum()
    )
    return agg, unmapped_pct


def build_plan_lift_base(
    *,
    shipments_df: Optional[pd.DataFrame],
    plan_df: Optional[pd.DataFrame],
    dimitems_df: Optional[pd.DataFrame],
    customer_names_df: Optional[pd.DataFrame],
) -> tuple[pd.DataFrame, PlanLiftBuildStats]:
    """Build the cached item × month × dims base frame for the metric.

    Grain
    -----
    One row per ``(month, item_key, corporate_group)`` carrying both
    ``plan_lbs`` (from dp_factscurrentaps) and ``ship_lbs`` (from IBP
    Shipments) — a full outer combination so a series-month present in
    only one source still appears (the missing side is 0).  Item
    dimensions are joined by ``item_key``; the display ``item_code`` is
    kept for the Item slicer.

    Both sources resolve Corporate Group through the SAME
    ``dp_dimcustomernames`` lookup (shipments via ``Customer No``, plan via
    ``party_site_code``) so the grouping is identical on both legs.

    Returns ``(base_df, stats)``.  Pure — no Streamlit, no I/O.
    """
    warnings: list[str] = []
    corp_lookup = _build_customer_num_to_corp_group_lookup(customer_names_df)
    if not corp_lookup:
        warnings.append(
            "dp_dimcustomernames did not yield a customer→Corporate Group "
            "lookup — every row is grouped as " + CORP_GROUP_UNMAPPED + "."
        )

    # ── Shipments side ────────────────────────────────────────────────
    ship_agg = _empty_side(COL_SHIP_LBS)
    ship_unmapped = 0.0
    ship_rows = 0
    if shipments_df is not None and not shipments_df.empty:
        item_col = _resolve_column(shipments_df, _SHIP_ITEM_CANDIDATES)
        code_col = _resolve_column(shipments_df, _SHIP_CUST_NO_CANDIDATES)
        month_col = _resolve_column(shipments_df, _SHIP_MONTH_CANDIDATES)
        qty_col = _resolve_column(shipments_df, _SHIP_QTY_CANDIDATES)
        if all((item_col, code_col, month_col, qty_col)):
            ship_agg, ship_unmapped = _aggregate_side(
                shipments_df, item_col=item_col, code_col=code_col,
                month_col=month_col, qty_col=qty_col, out_qty=COL_SHIP_LBS,
                corp_lookup=corp_lookup,
            )
            ship_rows = len(shipments_df)
        else:
            warnings.append("IBP Shipments is missing an expected column — shipments ignored.")

    # ── Plan side ─────────────────────────────────────────────────────
    plan_agg = _empty_side(COL_PLAN_LBS)
    plan_unmapped = 0.0
    plan_rows = 0
    if plan_df is not None and not plan_df.empty:
        # plan_df arrives already projected/renamed by the connector.
        plan_agg, plan_unmapped = _aggregate_side(
            plan_df, item_col=COL_ITEM_CODE, code_col="party_site_code",
            month_col=COL_MONTH, qty_col=COL_PLAN_LBS, out_qty=COL_PLAN_LBS,
            corp_lookup=corp_lookup,
        )
        plan_rows = len(plan_df)

    # ── Outer-merge the two legs on the shared grain ──────────────────
    base = ship_agg.merge(
        plan_agg, on=[COL_MONTH, COL_ITEM_KEY, COL_CORP_GROUP], how="outer",
    )
    base[COL_SHIP_LBS] = base[COL_SHIP_LBS].fillna(0.0)
    base[COL_PLAN_LBS] = base[COL_PLAN_LBS].fillna(0.0)

    # ── Attach item dimensions by item_key ────────────────────────────
    dim_frame = _build_item_dim_frame(dimitems_df)
    dim_cols = [name for name, _ in _DIM_FIELDS]
    if dim_frame.empty:
        warnings.append("dp_dimitems unavailable — item dimensions are blank.")
        for name in dim_cols:
            base[name] = DIM_UNKNOWN
        item_unmatched_pct = 100.0 if not base.empty else 0.0
    else:
        base = base.merge(dim_frame, on=COL_ITEM_KEY, how="left")
        unmatched_mask = base["portfolio_major"].isna()
        total_lbs = float((base[COL_PLAN_LBS] + base[COL_SHIP_LBS]).sum())
        unmatched_lbs = float(
            (base.loc[unmatched_mask, COL_PLAN_LBS]
             + base.loc[unmatched_mask, COL_SHIP_LBS]).sum()
        )
        item_unmatched_pct = (unmatched_lbs / total_lbs * 100.0) if total_lbs else 0.0
        # Items not in dp_dimitems keep their volume but get visible
        # "(Unknown)" dims so they still roll into company-wide combos.
        for name in dim_cols:
            base[name] = base[name].replace("", np.nan).fillna(DIM_UNKNOWN)

    # The display item_code mirrors item_key (the normalised numeric key).
    base[COL_ITEM_CODE] = base[COL_ITEM_KEY]

    stats = PlanLiftBuildStats(
        ship_rows=ship_rows,
        plan_rows=plan_rows,
        corp_unmapped_ship_pct=ship_unmapped,
        corp_unmapped_plan_pct=plan_unmapped,
        item_unmatched_pct=item_unmatched_pct,
        warnings=tuple(warnings),
    )
    return base.reset_index(drop=True), stats


def _empty_side(qty_col: str) -> pd.DataFrame:
    """Return an empty aggregate frame with the canonical grain columns."""
    return pd.DataFrame(columns=[COL_MONTH, COL_ITEM_KEY, COL_CORP_GROUP, qty_col])


# ── 4. The metric ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class YoYLiftResult:
    """The tidy series behind one chart line.

    ``frame`` columns: ``month, plan_sum, ship_sum, numerator, prior_year,
    lift, below_floor``.  ``lift`` is NaN where the prior-year base is
    non-positive or below the volume floor ("n.m." in the UI).
    """

    label: str
    filters: Mapping[str, Sequence[str]]
    frame: pd.DataFrame

    @property
    def is_empty(self) -> bool:
        """True when no month carries a defined lift (all "n.m.")."""
        return self.frame.empty or bool(self.frame["lift"].isna().all())


def compute_yoy_lift(
    base: pd.DataFrame,
    filters: Optional[Mapping[str, Sequence[str]]] = None,
    *,
    label: str = "",
    volume_floor: float = 0.0,
) -> YoYLiftResult:
    """Compute YoY Lift% at series grain for one filter set.

    THE function the whole section is built on — every chart line (each
    portfolio total and the free-form combo) calls this with a different
    ``filters`` dict.  The contract is a **ratio of sums**:

        numerator(m)  = SUM(plan) if SUM(plan) > 0 else SUM(ship)
        prior_year(m) = SUM(ship)(m - 12 months)        # SHIPMENTS only
        lift(m)       = numerator(m) / prior_year(m) - 1

    The prior year is the actual **shipped** volume 12 months back — it
    NEVER uses APS / plan data, even if a plan existed in that month.  So
    the lift always answers "vs. what we actually shipped a year ago".

    Parameters
    ----------
    base
        The full pre-agg frame from :func:`build_plan_lift_base`.  The
        month grid for the 12-month shift is taken from the FULL frame
        (not the filtered subset) so the shift aligns on the true
        calendar even when a filter leaves gaps.
    filters
        ``{dim -> allowed values}``.  A dim that is absent or maps to an
        empty collection imposes no constraint.  Unknown dim names are
        ignored.
    volume_floor
        Series-months whose prior-year base is positive but below this
        many pounds are flagged ``below_floor`` and their ``lift`` is
        suppressed to NaN — guards against explosive ratios off a tiny
        base.  ``0.0`` disables the floor (only ``py <= 0`` is suppressed).

    Returns
    -------
    YoYLiftResult
    """
    filters = filters or {}
    # Full calendar grid drives the self-shift; empty base → empty result.
    if base is None or base.empty:
        return YoYLiftResult(label=label, filters=dict(filters), frame=_empty_lift_frame())

    # CONTIGUOUS monthly grid (min..max of the whole base) so the −12-month
    # self-shift always lands on a real slot — a month with no rows is a
    # genuine zero, not a hole that would otherwise read as "n.m.".
    present = pd.to_datetime(base[COL_MONTH].dropna().unique())
    months = pd.date_range(present.min(), present.max(), freq="MS")

    # ── Apply filters (AND across dims, OR within a dim) ──────────────
    mask = pd.Series(True, index=base.index)
    for dim, allowed in filters.items():
        if dim not in base.columns:
            continue
        wanted = {str(v) for v in (allowed or []) if str(v) != ""}
        if not wanted:
            continue  # empty selection = no constraint on this dim
        mask &= base[dim].astype(str).isin(wanted)
    sub = base.loc[mask]

    # ── Sum to month, reindex onto the full grid (0-fill) ─────────────
    sums = (
        sub.groupby(COL_MONTH)[[COL_PLAN_LBS, COL_SHIP_LBS]].sum()
        if not sub.empty
        else pd.DataFrame(columns=[COL_PLAN_LBS, COL_SHIP_LBS])
    )
    sums = sums.reindex(months, fill_value=0.0)
    plan_sum = sums[COL_PLAN_LBS].to_numpy(dtype=float)
    ship_sum = sums[COL_SHIP_LBS].to_numpy(dtype=float)

    # numerator = plan where plan > 0 else shipments (current-month rule).
    numerator = np.where(plan_sum > 0.0, plan_sum, ship_sum)

    # prior_year = SHIPMENTS only, 12 calendar months back — never the plan.
    # The current month may be plan-driven, but its year-ago baseline is
    # always the actual shipped volume from that month, so the lift answers
    # "how does this compare to what we actually shipped a year ago?".
    # Period arithmetic makes the shift robust to any month gaps in the grid.
    periods = months.to_period("M")
    ship_by_period = pd.Series(ship_sum, index=periods)
    prior_year = ship_by_period.reindex(periods - 12).to_numpy(dtype=float)

    with np.errstate(divide="ignore", invalid="ignore"):
        lift = numerator / prior_year - 1.0

    py_positive = prior_year > 0.0
    below_floor = py_positive & (prior_year < float(volume_floor))
    suppressed = (~py_positive) | below_floor
    lift = np.where(suppressed, np.nan, lift)

    frame = pd.DataFrame({
        COL_MONTH: months,
        "plan_sum": plan_sum,
        "ship_sum": ship_sum,
        "numerator": numerator,
        "prior_year": prior_year,
        "lift": lift,
        "below_floor": below_floor,
    })
    return YoYLiftResult(label=label, filters=dict(filters), frame=frame)


def _empty_lift_frame() -> pd.DataFrame:
    """Return an empty metric frame with the canonical columns."""
    return pd.DataFrame(
        columns=[
            COL_MONTH, "plan_sum", "ship_sum", "numerator",
            "prior_year", "lift", "below_floor",
        ]
    )


def list_slicer_options(base: pd.DataFrame) -> dict[str, list[str]]:
    """Return sorted distinct non-blank values for every combo slicer dim.

    Used by the page to populate the multiselects.  Returns ``{}``-safe
    empty lists for dims absent from *base*.
    """
    options: dict[str, list[str]] = {}
    for dim in SLICER_DIMS:
        if base is None or base.empty or dim not in base.columns:
            options[dim] = []
            continue
        vals = (
            base[dim].astype(str).str.strip()
            .loc[lambda s: s != ""].unique().tolist()
        )
        options[dim] = sorted(vals)
    return options


def list_portfolios(base: pd.DataFrame) -> list[str]:
    """Return the sorted distinct, non-blank ``portfolio_major`` values."""
    if base is None or base.empty or "portfolio_major" not in base.columns:
        return []
    vals = (
        base["portfolio_major"].astype(str).str.strip()
        .loc[lambda s: (s != "") & (s != DIM_UNKNOWN)].unique().tolist()
    )
    return sorted(vals)


def today_month_begin(today: Optional[date] = None) -> pd.Timestamp:
    """Return the first-of-month Timestamp for *today* (defaults to now).

    The Plan Lift charts shade months strictly after this as "future"
    (plan-driven) and draw a marker at the boundary.  Parameterised for
    deterministic tests.
    """
    ts = pd.Timestamp(today) if today is not None else pd.Timestamp.today()
    return ts.to_period("M").to_timestamp()


# ── Portfolio-scoped option helpers ───────────────────────────────────────────

def list_minor_products(base: pd.DataFrame, portfolio: str) -> list[str]:
    """Return the sorted distinct ``portfolio_minor`` values within *portfolio*.

    Drives the per-portfolio default chart (one YoY-lift line per Minor
    Product).  Blank minors are dropped; the ``(Unknown)`` bucket is kept
    so items missing from ``dp_dimitems`` remain visible as their own line.
    """
    if base is None or base.empty or "portfolio_minor" not in base.columns:
        return []
    sub = base.loc[base["portfolio_major"].astype(str) == str(portfolio)]
    vals = (
        sub["portfolio_minor"].astype(str).str.strip()
        .loc[lambda s: s != ""].unique().tolist()
    )
    return sorted(vals)


def list_slicer_options_for_portfolio(
    base: pd.DataFrame, portfolio: str,
) -> dict[str, list[str]]:
    """Return combo-slicer options scoped to one portfolio.

    Same shape as :func:`list_slicer_options` but computed on the
    portfolio's slice only, so a combo inside the "Cultured" section lists
    just the brands / items / customers that actually exist in Cultured.
    Cheap — a single boolean mask over the already-cached base frame.
    """
    if base is None or base.empty:
        return {dim: [] for dim in SLICER_DIMS}
    sub = base.loc[base["portfolio_major"].astype(str) == str(portfolio)]
    return list_slicer_options(sub)


# ── 5. IRI overlay (syndicated weekly volume-lift data) ───────────────────────
#
# The IRI files are syndicated CSVs the planner drops into
# ``Files/RO Tracking/IRI/`` (one per refresh cycle, same schema).  They
# are NOT a Delta table, so they go through the Files-based connector
# (:mod:`data_sources.fabric_lakehouse_io`) rather than DuckDB/delta_scan.
#
# The chart overlay is the **promotional Unit Lift %** —
# ``Σ(Incremental Units) / Σ(Base Units)`` per month — drawn on a SECONDARY
# axis because it measures a different thing than the plan YoY lift.

_IRI_SECRETS_SECTION = "fabric_htst"
_IRI_FOLDER = "RO Tracking/IRI"

# Categorical columns the planner filters IRI on (display order).
IRI_FILTER_COLUMNS: tuple[str, ...] = (
    "Product", "Geography", "Custom Major Brand", "Custom Sub Category",
    "Custom Type Value", "Custom Size", "Tag",
)
# Columns behind the Unit Lift % rollup.
_IRI_WEEK_COL = "Week"
_IRI_INCREMENTAL_COL = "Incremental Units"
_IRI_BASE_COL = "Base Units"

# Display name → accepted source spellings.  The IRI export ships a couple
# of upstream typos / variants (notably ``Georgraphy`` for Geography) that
# would otherwise resolve to nothing and leave a filter unusable.  Anything
# not listed resolves by its own name (exact, then case/space-insensitive).
_IRI_COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "Geography": ("Geography", "Georgraphy", "Geo"),
    "Custom Major Brand": ("Custom Major Brand", "Custom Major Brands"),
    "Custom Sub Category": ("Custom Sub Category", "Custom Subcategory"),
    "Custom Type Value": ("Custom Type Value", "Custom Type"),
    _IRI_INCREMENTAL_COL: ("Incremental Units", "Incremental Unit"),
    _IRI_BASE_COL: ("Base Units", "Base Unit"),
}


def list_iri_files() -> list[str]:
    """Return IRI CSV blob paths under ``Files/RO Tracking/IRI``, newest first.

    Newest-first by last-modified so the most recent refresh is the
    default selection, while every prior file stays available for the
    planner to pick.  Returns ``[]`` when the folder is empty / absent.

    Raises :class:`PlanLiftError` on a hard listing failure.
    """
    try:
        files = list_files(_IRI_SECRETS_SECTION, _IRI_FOLDER, suffix=".csv")
    except LakehouseIOError as exc:
        raise PlanLiftError(str(exc)) from exc
    files.sort(key=lambda f: (f.last_modified or ""), reverse=True)
    return [f.full_path for f in files]


def iri_file_label(blob_path: str) -> str:
    """Return the bare filename for an IRI blob path (for the file picker)."""
    return blob_path.rsplit("/", 1)[-1]


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_iri(blob_path: str) -> pd.DataFrame:
    """Streamlit-cached read of one IRI CSV (keyed by its blob path)."""
    try:
        df, _etag = read_csv(_IRI_SECRETS_SECTION, blob_path)
    except LakehouseIOError as exc:
        raise PlanLiftError(str(exc)) from exc
    if df is None:
        raise PlanLiftError(f"IRI file not found: Files/{blob_path}")
    logger.info("Loaded IRI file %s: %s rows.", blob_path, len(df))
    return df


def fetch_iri_df(blob_path: str, *, force_refresh: bool = False) -> pd.DataFrame:
    """Return one IRI CSV as a DataFrame (cached per blob path).

    Raises :class:`PlanLiftError` on any read failure.
    """
    if force_refresh:
        _cached_iri.clear()
    return _cached_iri(blob_path)


def _resolve_iri_column(df: pd.DataFrame, name: str) -> Optional[str]:
    """Resolve an IRI column tolerant of casing / whitespace / known typos.

    Tries every accepted spelling for *name* (see
    :data:`_IRI_COLUMN_CANDIDATES`) exact-first, then case/space-insensitive.
    This is why e.g. a "Geography" filter still works when the source file
    actually spells the column ``Georgraphy``.
    """
    candidates = _IRI_COLUMN_CANDIDATES.get(name, (name,))
    lower = {str(c).lower().strip(): c for c in df.columns}
    for cand in candidates:
        if cand in df.columns:
            return cand
        hit = lower.get(cand.lower().strip())
        if hit is not None:
            return hit
    return None


def list_iri_filter_options(df: Optional[pd.DataFrame]) -> dict[str, list[str]]:
    """Return sorted distinct values per IRI filter column (``{}``-safe)."""
    options: dict[str, list[str]] = {}
    for col in IRI_FILTER_COLUMNS:
        actual = _resolve_iri_column(df, col) if df is not None else None
        if actual is None:
            options[col] = []
            continue
        options[col] = sorted(
            df[actual].astype(str).str.strip().loc[lambda s: s != ""].unique().tolist()
        )
    return options


def _iri_week_to_month(series: pd.Series) -> pd.Series:
    """Coerce the IRI ``Week`` column (Excel serial or date) → month-begin.

    Mirrors the Excel-serial handling in
    :func:`ro_seed_pipeline._canon_date` so a numeric week index parses to
    the correct calendar week, then floors to first-of-month.
    """
    s = series.astype(str).str.strip()
    is_serial = s.str.fullmatch(r"\d+(\.0+)?", na=False)
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    if is_serial.any():
        out.loc[is_serial] = pd.to_datetime(
            pd.to_numeric(s[is_serial]), origin="1899-12-30", unit="D",
        )
    if (~is_serial).any():
        out.loc[~is_serial] = pd.to_datetime(s[~is_serial], errors="coerce")
    return out.dt.to_period("M").dt.to_timestamp()


@dataclass(frozen=True)
class IRIResult:
    """The tidy monthly Unit-Lift series behind one IRI overlay line.

    ``frame`` columns: ``month, incremental, base, unit_lift`` where
    ``unit_lift = incremental / base`` (NaN when base <= 0).
    """

    label: str
    filters: Mapping[str, Sequence[str]]
    frame: pd.DataFrame

    @property
    def is_empty(self) -> bool:
        return self.frame.empty or bool(self.frame["unit_lift"].isna().all())


def _empty_iri_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["month", "incremental", "base", "unit_lift"])


def compute_iri_unit_lift(
    df: Optional[pd.DataFrame],
    filters: Optional[Mapping[str, Sequence[str]]] = None,
    *,
    label: str = "",
) -> IRIResult:
    """Aggregate IRI Unit Lift % to month for one filter set (ratio of sums).

    ``Unit Lift %(month) = Σ(Incremental Units) / Σ(Base Units)`` over the
    weeks falling in that month, after applying *filters* (AND across
    columns, OR within a column; an empty / missing selection is no
    constraint).  Weekly percentages are NEVER averaged — we sum the
    underlying unit counts first, consistent with the plan metric.
    """
    filters = filters or {}
    if df is None or df.empty:
        return IRIResult(label=label, filters=dict(filters), frame=_empty_iri_frame())

    inc_col = _resolve_iri_column(df, _IRI_INCREMENTAL_COL)
    base_col = _resolve_iri_column(df, _IRI_BASE_COL)
    week_col = _resolve_iri_column(df, _IRI_WEEK_COL)
    if not (inc_col and base_col and week_col):
        return IRIResult(label=label, filters=dict(filters), frame=_empty_iri_frame())

    mask = pd.Series(True, index=df.index)
    for dim, allowed in filters.items():
        actual = _resolve_iri_column(df, dim)
        wanted = {str(v) for v in (allowed or []) if str(v) != ""}
        if actual is None or not wanted:
            continue
        mask &= df[actual].astype(str).str.strip().isin(wanted)
    sub = df.loc[mask]
    if sub.empty:
        return IRIResult(label=label, filters=dict(filters), frame=_empty_iri_frame())

    tidy = pd.DataFrame({
        "month": _iri_week_to_month(sub[week_col]),
        "incremental": pd.to_numeric(sub[inc_col], errors="coerce").fillna(0.0),
        "base": pd.to_numeric(sub[base_col], errors="coerce").fillna(0.0),
    }).dropna(subset=["month"])
    if tidy.empty:
        return IRIResult(label=label, filters=dict(filters), frame=_empty_iri_frame())

    agg = tidy.groupby("month", as_index=False)[["incremental", "base"]].sum()
    with np.errstate(divide="ignore", invalid="ignore"):
        agg["unit_lift"] = np.where(
            agg["base"] > 0.0, agg["incremental"] / agg["base"], np.nan,
        )
    return IRIResult(label=label, filters=dict(filters), frame=agg)


__all__ = [
    "PlanLiftError",
    "PlanLiftBuildStats",
    "YoYLiftResult",
    "IRIResult",
    "CORP_GROUP_UNMAPPED",
    "DIM_UNKNOWN",
    "SLICER_DIMS",
    "IRI_FILTER_COLUMNS",
    "COL_MONTH",
    "COL_ITEM_CODE",
    "COL_CORP_GROUP",
    "fetch_factscurrentaps_slim_df",
    "fetch_dimcalendar_df",
    "build_month_fiscal_labels",
    "build_plan_lift_base",
    "compute_yoy_lift",
    "list_slicer_options",
    "list_slicer_options_for_portfolio",
    "list_minor_products",
    "list_portfolios",
    "today_month_begin",
    "list_iri_files",
    "iri_file_label",
    "fetch_iri_df",
    "list_iri_filter_options",
    "compute_iri_unit_lift",
]
