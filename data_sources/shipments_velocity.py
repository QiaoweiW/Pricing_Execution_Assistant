"""Velocity Analysis data source — ``dbo.Shipments`` (OneLake Delta).

Powers the Demand Planner Analytics page's **Velocity Analysis** section: a
week-over-week view of **ordered lbs** vs **shipped lbs**, filterable by
Portfolio, Product Description, Customer, Business Unit (default B2C) and
Product Format, with an order-date time slicer.

Design notes
------------
* **Read path** mirrors :mod:`data_sources.customer_dims`: a token-bound DuckDB
  ``delta_scan`` of the ``Tables/dbo/Shipments`` Delta table in the shared IBP
  lakehouse.  We first read the header (``LIMIT 0``) to discover the live
  columns, resolve each logical field against a candidate whitelist, then
  ``SELECT`` only the (narrow) resolved columns — so a wide/large transaction
  table isn't pulled column-for-column.
* **Column resolution is tolerant.**  The Delta export's spelling has
  historically wobbled ("Ordered Qty lbs" vs "Ordered Lbs", etc.), so every
  logical field is probed case-insensitively against :data:`_CANDIDATES`.  The
  three *required* fields (order date, ordered lbs, shipped lbs) raise a
  :class:`ShipmentsVelocityError` that lists the ACTUAL columns when unresolved,
  so a spelling drift is a one-line fix here rather than a silent empty chart.
  The five *filter* dimensions are optional — a missing one simply disables its
  filter.
* **Pure transforms** (:func:`resolve_columns`, :func:`build_weekly_velocity`,
  :func:`distinct_values`) are Streamlit-/IO-free and unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd
import streamlit as st

from data_sources.fabric_auth import (
    FabricAuthError,
    acquire_storage_token,
    bind_storage_token,
    duckdb_lock,
    get_duckdb_connection,
    prepare_duckdb_tls,
)
from data_sources.fabric_tls import tls_error_hint as _tls_hint


# ── Source identity (same lakehouse as IBP; dbo schema) ──────────────────────
_WORKSPACE_GUID = "bb11c51d-03c8-4f1b-938c-e20657a8f31d"
_LAKEHOUSE_GUID = "a01f513d-eee7-41eb-8c15-670bc40e7fc8"
_SCHEMA = "dbo"
_TABLE_SHIPMENTS = "Shipments"

# 15-minute cache TTL — matches the IBP / customer-dims read cadence.
_CACHE_TTL_SECONDS = 15 * 60

# Default Business Unit selection for the section.
DEFAULT_BUSINESS_UNIT = "B2C"


# ── Canonical (logical) column names of the tidy frame we return ─────────────
COL_ORDER_DATE:     str = "order_date"
COL_ORDERED_LBS:    str = "ordered_lbs"
COL_SHIPPED_LBS:    str = "shipped_lbs"
COL_PORTFOLIO:      str = "portfolio"
COL_PRODUCT_MINOR:  str = "product_minor"
COL_PRODUCT_DESC:   str = "product_desc"
COL_CUSTOMER:       str = "customer"
COL_BUSINESS_UNIT:  str = "business_unit"
COL_PRODUCT_FORMAT: str = "product_format"
# Non-filter helper columns: item (for a PDH Portfolio-Minor fallback join when
# the shipments table lacks Portfolio Minor) and ship-to (for Shipped Velocity).
COL_ITEM:           str = "item"
COL_SHIP_TO:        str = "ship_to"

# Required (the chart can't be built without these) vs optional filter dims vs
# extra helper columns (selected when present, not offered as filters).
REQUIRED_FIELDS: tuple[str, ...] = (COL_ORDER_DATE, COL_ORDERED_LBS, COL_SHIPPED_LBS)
FILTER_FIELDS:   tuple[str, ...] = (
    COL_PORTFOLIO, COL_PRODUCT_MINOR, COL_PRODUCT_DESC, COL_CUSTOMER,
    COL_BUSINESS_UNIT, COL_PRODUCT_FORMAT,
)
EXTRA_FIELDS:    tuple[str, ...] = (COL_ITEM, COL_SHIP_TO)

# Candidate source-column spellings per logical field (probed case-insensitively,
# first match wins).  Seeded from the conventions already used for
# ``dbo.IBP Shipments`` in :mod:`data_sources.demand_plan_comparison`.
_CANDIDATES: dict[str, tuple[str, ...]] = {
    COL_ORDER_DATE: (
        "Order Date", "OrderDate", "Order_Date", "Ordered Date", "Order Dt",
        "Order Date Key", "OrderDateKey",
    ),
    COL_ORDERED_LBS: (
        "Ordered Qty lbs", "Ordered Qty Lbs", "Ordered Quantity Lbs",
        "Ordered_Qty_lbs", "Ordered Lbs", "Ordered Pounds", "OrderedLbs",
        "Order Lbs", "Ordered Qty",
    ),
    COL_SHIPPED_LBS: (
        "Shipped Qty lbs", "Shipped Qty Lbs", "Shipped Quantity Lbs",
        "Shipped_Qty_lbs", "Shipped Lbs", "Shipped Pounds", "ShippedLbs",
        "Ship Lbs", "Shipped Qty",
    ),
    COL_PORTFOLIO: (
        "Portfolio", "Portfolio Major", "PortfolioMajor", "Portfolio_Major",
    ),
    COL_PRODUCT_MINOR: (
        "Portfolio Minor", "PortfolioMinor", "Portfolio_Minor", "productminor",
        "Product Minor",
    ),
    COL_ITEM: (
        "Item", "Item No", "Item Number", "Item #", "ItemNo", "Item Num",
        "ItemNum", "Item Code", "ItemCode", "Item ID", "ItemID", "SKU",
        "productcode", "Product Code", "Product ID", "ProductID", "Product",
        "Material", "Material Number", "itemnumber", "item_no", "item",
    ),
    COL_SHIP_TO: (
        "Ship To", "ShipTo", "Ship-To", "Ship To Number", "ShipToNumber",
        "Ship To Location", "Ship To Site", "Ship To Party", "Party Site Number",
        "Party Site", "Location",
    ),
    COL_PRODUCT_DESC: (
        "productdesc", "Product Desc", "Product Description", "ProductDescription",
        "ProductDesc", "Item Description", "Description",
    ),
    COL_CUSTOMER: (
        "Customer", "Customer Name", "CustomerName", "Customer_Name",
        "Customer No", "Customer Number",
    ),
    COL_BUSINESS_UNIT: (
        "Business Unit", "BusinessUnit", "Business_Unit", "BU",
    ),
    COL_PRODUCT_FORMAT: (
        "Product Format", "ProductFormat", "Product_Format", "Supply Format",
        "SupplyFormat", "Format",
    ),
}


class ShipmentsVelocityError(RuntimeError):
    """Raised on any failure to read / resolve ``dbo.Shipments``.

    Wraps the underlying exception so the page can render one clean banner
    without leaking DuckDB / deltalake stack traces.
    """


# ── Column resolution (pure) ─────────────────────────────────────────────────

@dataclass(frozen=True)
class ResolvedColumns:
    """Map of logical field → actual source column (``None`` when absent).

    ``available`` keeps the raw column list so the page can show a "columns
    detected" diagnostic when a required field can't be resolved.
    """

    mapping: dict[str, Optional[str]]
    available: tuple[str, ...]

    def actual(self, logical: str) -> Optional[str]:
        return self.mapping.get(logical)

    def missing_required(self) -> list[str]:
        return [f for f in REQUIRED_FIELDS if not self.mapping.get(f)]

    def present_fields(self) -> list[str]:
        """Logical fields (required + filter + extra) that resolved to a column."""
        return [f for f in (*REQUIRED_FIELDS, *FILTER_FIELDS, *EXTRA_FIELDS)
                if self.mapping.get(f)]

    def select_sql(self) -> str:
        """``"Actual" AS logical`` list for the resolved fields (SQL-quoted)."""
        parts = [
            f'"{self.mapping[f]}" AS {f}'
            for f in self.present_fields()
        ]
        return ", ".join(parts)


def resolve_columns(available: list[str]) -> ResolvedColumns:
    """Resolve each logical field against :data:`_CANDIDATES` (case-insensitive).

    First matching candidate wins; unresolved fields map to ``None``.
    """
    by_ci = {str(c).strip().casefold(): c for c in available}
    mapping: dict[str, Optional[str]] = {}
    for logical, candidates in _CANDIDATES.items():
        hit: Optional[str] = None
        for cand in candidates:
            actual = by_ci.get(cand.strip().casefold())
            if actual is not None:
                hit = actual
                break
        mapping[logical] = hit
    return ResolvedColumns(mapping=mapping, available=tuple(available))


# ── Weekly velocity transform (pure) ─────────────────────────────────────────

@dataclass(frozen=True)
class WeeklyVelocity:
    """Result of :func:`build_weekly_velocity`.

    * ``weekly`` — one row per ISO week (Mon-anchored ``week_start``) with
      ``ordered_lbs`` and ``shipped_lbs`` sums, sorted ascending.  When the
      shipments table carries a ship-to column it also has ``ship_to_count``
      (distinct ship-to locations that week), ``shipped_velocity``
      (``shipped_lbs ÷ ship_to_count``) and ``order_velocity``
      (``ordered_lbs ÷ ship_to_count``) — average lbs per ship-to.
    * ``total_ordered`` / ``total_shipped`` — filtered grand totals (lbs).
    * ``has_velocity`` — whether the ship-to-derived velocity lines are present.
    """

    weekly: pd.DataFrame
    total_ordered: float
    total_shipped: float
    has_velocity: bool = False
    # Fixed full-history median velocities (the "100" of the demand/supply index).
    baseline_demand: Optional[float] = None
    baseline_supply: Optional[float] = None
    # Overall shipped ÷ ordered for the displayed window (service / fill rate).
    fill_rate: Optional[float] = None


WEEK_START: str = "week_start"
SHIP_TO_COUNT: str = "ship_to_count"
SHIPPED_VELOCITY: str = "shipped_velocity"
ORDER_VELOCITY: str = "order_velocity"
# Rebased-to-100 indices (velocity ÷ fixed full-history median × 100) + fill rate.
DEMAND_INDEX: str = "demand_index"
SUPPLY_INDEX: str = "supply_index"
FILL_RATE: str = "fill_rate"


def _week_start(order_dt: pd.Series) -> pd.Series:
    """Monday-of-week (normalized) for each order date."""
    dt = pd.to_datetime(order_dt, errors="coerce")
    return (dt - pd.to_timedelta(dt.dt.weekday, unit="D")).dt.normalize()


def distinct_values(df: pd.DataFrame, logical_col: str) -> list[str]:
    """Sorted distinct non-blank string values of *logical_col* (``[]`` if absent)."""
    if df is None or df.empty or logical_col not in df.columns:
        return []
    s = df[logical_col].dropna().astype(str).str.strip()
    return sorted({v for v in s if v and v.lower() not in ("nan", "none")})


def build_weekly_velocity(
    df: pd.DataFrame, *,
    portfolios:      Optional[list[str]] = None,
    product_minors:  Optional[list[str]] = None,
    product_descs:   Optional[list[str]] = None,
    customers:       Optional[list[str]] = None,
    business_units:  Optional[list[str]] = None,
    product_formats: Optional[list[str]] = None,
    date_range:      Optional[tuple[date, date]] = None,
) -> WeeklyVelocity:
    """Filter the tidy shipments frame and aggregate ordered/shipped lbs by week.

    Each dimension filter applies only when a value list is given AND the column
    is present.  ``date_range`` is an inclusive ``(start, end)`` bound on the
    order date.  When a ship-to column is present, each week also gets its
    distinct ship-to count and **Shipped Velocity** (shipped lbs per ship-to) —
    both reactive to the same filters.  Empty input / no surviving rows → an
    empty (well-shaped) result.
    """
    empty = WeeklyVelocity(
        weekly=pd.DataFrame(columns=[WEEK_START, COL_ORDERED_LBS, COL_SHIPPED_LBS]),
        total_ordered=0.0, total_shipped=0.0, has_velocity=False,
    )
    if df is None or df.empty or COL_ORDER_DATE not in df.columns:
        return empty

    work = df.copy()
    work[COL_ORDER_DATE] = pd.to_datetime(work[COL_ORDER_DATE], errors="coerce")

    _dim_filters = {
        COL_PORTFOLIO: portfolios, COL_PRODUCT_MINOR: product_minors,
        COL_PRODUCT_DESC: product_descs, COL_CUSTOMER: customers,
        COL_BUSINESS_UNIT: business_units, COL_PRODUCT_FORMAT: product_formats,
    }
    # Dimension filters only — the date window is applied AFTER indexing so the
    # baseline stays a fixed full-history reference (the index means "vs normal").
    mask = work[COL_ORDER_DATE].notna()
    for col, values in _dim_filters.items():
        if values and col in work.columns:
            mask &= work[col].astype(str).str.strip().isin([str(v) for v in values])
    work = work.loc[mask]
    if work.empty:
        return empty

    grouped = pd.DataFrame({
        WEEK_START: _week_start(work[COL_ORDER_DATE]),
        COL_ORDERED_LBS: pd.to_numeric(work.get(COL_ORDERED_LBS), errors="coerce").fillna(0.0).to_numpy(),
        COL_SHIPPED_LBS: pd.to_numeric(work.get(COL_SHIPPED_LBS), errors="coerce").fillna(0.0).to_numpy(),
    })
    has_velocity = COL_SHIP_TO in work.columns
    if has_velocity:
        grouped[COL_SHIP_TO] = work[COL_SHIP_TO].astype(str).str.strip().to_numpy()

    agg = {COL_ORDERED_LBS: "sum", COL_SHIPPED_LBS: "sum"}
    if has_velocity:
        agg[COL_SHIP_TO] = "nunique"
    # Full weekly series across ALL weeks (baseline reference).
    full = (grouped.groupby(WEEK_START, as_index=False).agg(agg)
                   .sort_values(WEEK_START).reset_index(drop=True))
    # Weekly fill rate = shipped ÷ ordered (service level).
    full[FILL_RATE] = np.where(full[COL_ORDERED_LBS] > 0,
                               full[COL_SHIPPED_LBS] / full[COL_ORDERED_LBS], np.nan)

    baseline_demand = baseline_supply = None
    if has_velocity:
        full = full.rename(columns={COL_SHIP_TO: SHIP_TO_COUNT})
        # Average lbs per ship-to location; NaN (not ∞) when a week has none.
        _denom = full[SHIP_TO_COUNT].where(full[SHIP_TO_COUNT] > 0)
        full[SHIPPED_VELOCITY] = full[COL_SHIPPED_LBS] / _denom
        full[ORDER_VELOCITY] = full[COL_ORDERED_LBS] / _denom
        # Fixed baseline = median velocity over weeks with activity (full history).
        d_active = full.loc[full[COL_ORDERED_LBS] > 0, ORDER_VELOCITY].dropna()
        s_active = full.loc[full[COL_SHIPPED_LBS] > 0, SHIPPED_VELOCITY].dropna()
        baseline_demand = float(d_active.median()) if not d_active.empty and d_active.median() > 0 else None
        baseline_supply = float(s_active.median()) if not s_active.empty and s_active.median() > 0 else None
        full[DEMAND_INDEX] = (full[ORDER_VELOCITY] / baseline_demand * 100.0
                              if baseline_demand else np.nan)
        full[SUPPLY_INDEX] = (full[SHIPPED_VELOCITY] / baseline_supply * 100.0
                              if baseline_supply else np.nan)

    # Slice to the display window — the index is already rebased to the fixed
    # full-history baseline, so slicing only zooms the view.
    weekly = full
    if date_range is not None:
        lo, hi = date_range
        weekly = full[full[WEEK_START].dt.date.between(lo, hi)].reset_index(drop=True)
    if weekly.empty:
        return empty

    _to = float(weekly[COL_ORDERED_LBS].sum())
    _ts = float(weekly[COL_SHIPPED_LBS].sum())
    return WeeklyVelocity(
        weekly=weekly,
        total_ordered=_to,
        total_shipped=_ts,
        has_velocity=has_velocity,
        baseline_demand=baseline_demand,
        baseline_supply=baseline_supply,
        fill_rate=(_ts / _to) if _to else None,
    )


# ── I/O: token-bound DuckDB read of dbo.Shipments ───────────────────────────

def _table_uri() -> str:
    return (
        f"abfss://{_WORKSPACE_GUID}@onelake.dfs.fabric.microsoft.com/"
        f"{_LAKEHOUSE_GUID}/Tables/{_SCHEMA}/{_TABLE_SHIPMENTS}"
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_shipments_fetch(_cache_token: str) -> pd.DataFrame:
    """Read + tidy ``dbo.Shipments``: resolve columns, then SELECT only those.

    Returns a frame whose columns are the CANONICAL logical names (a subset of
    :data:`REQUIRED_FIELDS` + :data:`FILTER_FIELDS`, whichever resolved).
    """
    uri = _table_uri()
    try:
        token = acquire_storage_token()
    except FabricAuthError as exc:
        raise ShipmentsVelocityError(str(exc)) from exc

    ssl_verify = prepare_duckdb_tls()
    try:
        con = get_duckdb_connection()
        with duckdb_lock():
            bind_storage_token(con, token, ssl_verify=ssl_verify)
            header = con.execute(
                f"SELECT * FROM delta_scan('{uri}') LIMIT 0"
            ).df()
            resolved = resolve_columns(list(header.columns))
            missing = resolved.missing_required()
            if missing:
                raise ShipmentsVelocityError(
                    "dbo.Shipments is missing required column(s) for "
                    f"{missing}.  Columns present: {list(header.columns)}."
                )
            df = con.execute(
                f"SELECT {resolved.select_sql()} FROM delta_scan('{uri}')"
            ).df()
    except ShipmentsVelocityError:
        raise
    except FabricAuthError as exc:
        raise ShipmentsVelocityError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise ShipmentsVelocityError(
            f"Could not read Delta table via DuckDB at {uri}.  Verify the "
            f"lakehouse identifiers and your Read access.  Underlying error: "
            f"{exc}{_tls_hint(exc)}"
        ) from exc
    return df


def fetch_shipments_df(*, force_refresh: bool = False) -> pd.DataFrame:
    """Return the tidy ``dbo.Shipments`` frame (canonical column names).

    Raises :class:`ShipmentsVelocityError` on any read / resolution failure.
    """
    if force_refresh:
        _cached_shipments_fetch.clear()
    return _cached_shipments_fetch("default")


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_source_columns(_cache_token: str) -> list[str]:
    """Raw ``dbo.Shipments`` column names — a cheap LIMIT-0 header read."""
    uri = _table_uri()
    try:
        token = acquire_storage_token()
    except FabricAuthError as exc:
        raise ShipmentsVelocityError(str(exc)) from exc
    ssl_verify = prepare_duckdb_tls()
    try:
        con = get_duckdb_connection()
        with duckdb_lock():
            bind_storage_token(con, token, ssl_verify=ssl_verify)
            header = con.execute(
                f"SELECT * FROM delta_scan('{uri}') LIMIT 0").df()
    except Exception as exc:  # noqa: BLE001
        raise ShipmentsVelocityError(str(exc)) from exc
    return list(header.columns)


def fetch_source_columns() -> list[str]:
    """Raw ``dbo.Shipments`` column names for the UI diagnostic; ``[]`` on any
    read failure (a diagnostic must never break the section)."""
    try:
        return _cached_source_columns("default")
    except ShipmentsVelocityError:
        return []


__all__ = [
    "ShipmentsVelocityError",
    "ResolvedColumns",
    "WeeklyVelocity",
    "DEFAULT_BUSINESS_UNIT",
    "WEEK_START", "SHIP_TO_COUNT", "SHIPPED_VELOCITY", "ORDER_VELOCITY",
    "DEMAND_INDEX", "SUPPLY_INDEX", "FILL_RATE",
    "COL_ORDER_DATE", "COL_ORDERED_LBS", "COL_SHIPPED_LBS", "COL_PORTFOLIO",
    "COL_PRODUCT_MINOR", "COL_PRODUCT_DESC", "COL_CUSTOMER", "COL_BUSINESS_UNIT",
    "COL_PRODUCT_FORMAT", "COL_ITEM", "COL_SHIP_TO",
    "REQUIRED_FIELDS", "FILTER_FIELDS", "EXTRA_FIELDS",
    "resolve_columns", "build_weekly_velocity", "distinct_values",
    "fetch_shipments_df", "fetch_source_columns",
]
