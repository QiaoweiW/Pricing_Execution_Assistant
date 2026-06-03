"""Product Line Review — per-Portfolio-Major hierarchical table + chart.

Pure build logic for the Demand Planner Analytics page.  Consumes:

* ``dbo.IBP Orders``                            (``Ordered Qty lbs``)
* ``ibp_base_plan_current.csv``                 (``Total`` by ``Start of Month``)
* Saved ``RO_Summary_Report.csv``               (``FY27 Probabilized | Current Plan``)
* ``qry_total_item_level_demand.csv``           (Full-Year chart)
* ``qry_pdh.csv``                               (item-level dims for every join)

Layout
------
The Demand Planner Analytics page renders **one table + one chart per
Portfolio Major** (looped from PDH).  This module owns:

* The filter dataclasses (common picks + per-PM sub-filters).
* The hierarchical table builder (brand → pminor → sfmt → customers).
* The Full-Year chart data builder (CY FY vs NY FY).
* The dynamic display-group spec (column labels echo the active filters).

Volumes are returned in **millions of lbs**, 1 decimal in display; percent
columns are whole-number percents; invalid ratios → ``nm``; missing → ``–``.
The Full-Year chart, by planner request, reports **raw lbs** (matches the
``qry_total_item_level_demand`` viewer the chart mock-up was taken from).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

from data_sources.demand_plan_comparison import (
    BRAND_BRANDED,
    BRAND_PRIVATE,
    _attach_dims,
    _IBP_ITEM_CANDIDATES,
    _LBS_PER_MILLION,
    _vectorised_clean_str,
    _vectorised_item_key,
    _vectorised_start_of_month,
    build_item_dim_frame,
    enrich_ibp_orders_df,
    months_in_range,
    resolve_ro_summary_path,
)
from data_sources.demand_summary import (
    COL_DEMAND_LBS,
    COL_ITEM,
    COL_START_OF_MONTH,
    _resolve_column,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Display column keys (internal IDs; UI maps to dynamic labels)
# ─────────────────────────────────────────────────────────────────────────────

COL_ROW_LABEL = "Row Label"
COL_INDENT = "_indent"
COL_IS_CUSTOMER = "_is_customer"

# Current-Month group.
COL_CM_PY = "cm_py"
COL_CM_CY = "cm_cy"
COL_CM_PCT = "cm_pct"
COL_CM_LBS = "cm_lbs"

# Year-To-Go group.
COL_YTG_PY = "ytg_py"
COL_YTG_CY = "ytg_cy"
COL_YTG_PCT = "ytg_pct"
COL_YTG_LBS = "ytg_lbs"

# Annualized Run Rate group.
COL_RR_L3 = "rr_l3"
COL_RR_L6 = "rr_l6"
COL_RR_L12 = "rr_l12"

# Full-Year group.
COL_FY_PY = "fy_py"
COL_FY_LE = "fy_le"
COL_FY_PCT = "fy_pct"
COL_FY_LBS = "fy_lbs"
COL_FY_RO = "fy_ro"
COL_FY_TOTAL = "fy_total"

# Ordered tuple of (group, ((col_key, label_template), ...)) used to drive
# the rename + column-order pipeline.  Labels containing ``{py}`` / ``{cy}``
# placeholders are formatted from the active filters at display time via
# :func:`build_display_groups`.
_DISPLAY_GROUPS_TEMPLATE: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Current Month", (
        (COL_CM_PY,  "Orders – {py}"),
        (COL_CM_CY,  "Base Plan – {cy}"),
        (COL_CM_PCT, "Base Plan vs PY Orders (%)"),
        (COL_CM_LBS, "Base Plan vs PY Orders (Mlbs)"),
    )),
    ("Year-to-Go", (
        (COL_YTG_PY,  "PY Orders – YTG"),
        (COL_YTG_CY,  "Base Plan – CY YTG"),
        (COL_YTG_PCT, "Base Plan vs PY Orders – YTG (%)"),
        (COL_YTG_LBS, "Base Plan vs PY Orders – YTG (Mlbs)"),
    )),
    ("Annualized Run Rate", (
        (COL_RR_L3,  "Annualized Run Rate L3 (Orders)"),
        (COL_RR_L6,  "Annualized Run Rate L6 (Orders)"),
        (COL_RR_L12, "Annualized Run Rate L12 (Orders)"),
    )),
    ("Full Year", (
        (COL_FY_PY,    "PFY Orders – Full Year"),
        (COL_FY_LE,    "CFY Base Plan – Full Year"),
        (COL_FY_PCT,   "CFY Base Plan vs PFY Orders (%)"),
        (COL_FY_LBS,   "CFY Base Plan vs PFY Orders (Mlbs)"),
        (COL_FY_RO,    "Full Year R&O (Mlbs)"),
        (COL_FY_TOTAL, "Full Year Forecast Total (Base + R&O)"),
    )),
)


# ─────────────────────────────────────────────────────────────────────────────
# Schema candidates — column names probed from each source CSV
# ─────────────────────────────────────────────────────────────────────────────

# Base-plan CSV (``ibp_base_plan_current.csv``).
_BP_MONTH_CANDIDATES: tuple[str, ...] = ("Start of Month", "Start Of Month", "Month")
_BP_ITEM_CANDIDATES: tuple[str, ...] = _IBP_ITEM_CANDIDATES
_BP_TOTAL_CANDIDATES: tuple[str, ...] = ("Total", "Demand Plan Pounds")
_BP_PORTFOLIO_CANDIDATES: tuple[str, ...] = (
    "Portfolio", "Portfolio Major", "Portfolio_Major",
)
_BP_SFMT_CANDIDATES: tuple[str, ...] = (
    "Product Format", "Product_Format", "Supply Format", "Supply_Format",
)
_BP_BRAND_CANDIDATES: tuple[str, ...] = (
    "Brand Category", "Brand_Category", "BrandCategory",
)
_BP_CUSTOMER_CANDIDATES: tuple[str, ...] = (
    "Plan To Name", "Plan To", "PlanToName", "Customer Name",
)
_BP_CYCLE_CANDIDATES: tuple[str, ...] = ("Cycle",)

# Fiscal-year month labels — Apr is FY month 1, Mar is FY month 12.
FY_MONTH_LABELS: tuple[str, ...] = (
    "Apr", "May", "Jun", "Jul", "Aug", "Sep",
    "Oct", "Nov", "Dec", "Jan", "Feb", "Mar",
)


# ─────────────────────────────────────────────────────────────────────────────
# Filter dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProductLineReviewCommonFilters:
    """Filter values shared across every Portfolio Major table.

    The user picks four monthly dates in the page header; PY counterparts
    are derived (CY − 12) and never picked.
    """
    cy_month: date
    cy_begin_month: date
    cy_ytg_start: date
    cy_ytg_end: date


@dataclass(frozen=True)
class ProductLineReviewSubFilters:
    """Per-PM picker state (multi-select; empty tuple = include all)."""
    supply_formats: tuple[str, ...] = ()
    brands: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductLineReviewFilters:
    """Fully resolved filter set for a single Portfolio Major build.

    Combines a :class:`ProductLineReviewCommonFilters` with the PM
    label + per-PM sub-filters, and pre-computes the PY-aligned month
    bounds (CY − 12) so the builders don't reason about derivation.
    """
    common: ProductLineReviewCommonFilters
    portfolio_major: str
    sub: ProductLineReviewSubFilters

    # Derived (CY − 12 months); populated by :func:`resolve_filters`.
    py_month: date = field(init=False, default=date(1900, 1, 1))
    py_ytg_start: date = field(init=False, default=date(1900, 1, 1))
    py_ytg_end: date = field(init=False, default=date(1900, 1, 1))

    def __post_init__(self) -> None:
        # ``frozen=True`` blocks normal assignment; ``object.__setattr__`` is
        # the documented escape hatch for post-init computed fields.
        cy_m = self.common.cy_month.replace(day=1)
        cy_ytg_s = self.common.cy_ytg_start.replace(day=1)
        cy_ytg_e = self.common.cy_ytg_end.replace(day=1)
        object.__setattr__(self, "py_month", add_months(cy_m, -12))
        object.__setattr__(self, "py_ytg_start", add_months(cy_ytg_s, -12))
        object.__setattr__(self, "py_ytg_end", add_months(cy_ytg_e, -12))


def resolve_filters(
    common: ProductLineReviewCommonFilters,
    portfolio_major: str,
    sub: ProductLineReviewSubFilters,
) -> ProductLineReviewFilters:
    """Convenience constructor — keeps callers symmetric across PMs."""
    return ProductLineReviewFilters(
        common=common,
        portfolio_major=portfolio_major,
        sub=sub,
    )


@dataclass(frozen=True)
class ProductLineReviewResult:
    """Formatted hierarchical table + soft warnings for one PM build."""
    table: pd.DataFrame
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _Measures:
    """Numeric measures for one row (millions of lbs except percents)."""
    cm_py: float = 0.0
    cm_cy: float = 0.0
    ytg_py: float = 0.0
    ytg_cy: float = 0.0
    rr_l3: float = 0.0
    rr_l6: float = 0.0
    rr_l12: float = 0.0
    fy_py: float = 0.0
    fy_le: float = 0.0
    fy_ro: Optional[float] = None  # ``None`` → display em-dash (customer rows)


# ─────────────────────────────────────────────────────────────────────────────
# Month arithmetic
# ─────────────────────────────────────────────────────────────────────────────

def add_months(anchor: date, delta: int) -> date:
    """Return the first-of-month *delta* months from *anchor* (signed)."""
    y, m = anchor.year, anchor.month
    m += delta
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return date(y, m, 1)


def trailing_months_end_at(end: date, count: int) -> set[date]:
    """Trailing *count* first-of-month dates ending at *end* (inclusive)."""
    end_norm = end.replace(day=1)
    start = add_months(end_norm, -(count - 1))
    return months_in_range(start, end_norm)


def eligible_cy_begin_months(cy_month: date) -> list[date]:
    """Return every first-of-month the user may pick as CY Begin.

    Planner rule: 12 months ending at and including CY Month —
    i.e. ``[CY Month − 11, CY Month]``.  Built arithmetically.
    """
    cy_m = cy_month.replace(day=1)
    earliest = add_months(cy_m, -11)
    return sorted(months_in_range(earliest, cy_m))


def cy_full_year_months(cy_begin: date) -> set[date]:
    """CY fiscal year: CY Begin through CY Begin + 11 months."""
    start = cy_begin.replace(day=1)
    return months_in_range(start, add_months(start, 11))


def py_full_year_months(cy_begin: date) -> set[date]:
    """PY fiscal year: CY window shifted back 12 months."""
    start = add_months(cy_begin.replace(day=1), -12)
    return months_in_range(start, add_months(start, 11))


def ny_full_year_months(cy_begin: date) -> set[date]:
    """Next-Year fiscal year: 12 months immediately AFTER the CY FY window."""
    start = add_months(cy_begin.replace(day=1), 12)
    return months_in_range(start, add_months(start, 11))


def collect_ibp_months(filters: ProductLineReviewFilters) -> tuple[date, ...]:
    """Union of every month an IBP-Orders pull needs for *filters*."""
    return collect_ibp_months_for_common(filters.common)


def collect_ibp_months_for_common(
    common: ProductLineReviewCommonFilters,
) -> tuple[date, ...]:
    """Union of every month an IBP-Orders pull needs for *common* filters.

    Identical to :func:`collect_ibp_months` but takes the smaller dataclass —
    the page-side loader fetches IBP Orders once for ALL Portfolio Majors,
    and the months depend only on the common picks (not on the per-PM
    sub-filters).
    """
    cy_m = common.cy_month.replace(day=1)
    py_m = add_months(cy_m, -12)
    py_ytg_start = add_months(common.cy_ytg_start.replace(day=1), -12)
    py_ytg_end = add_months(common.cy_ytg_end.replace(day=1), -12)
    months: set[date] = {cy_m, py_m}
    months |= trailing_months_end_at(cy_m, 12)
    months |= months_in_range(py_ytg_start, py_ytg_end)
    months |= cy_full_year_months(common.cy_begin_month)
    months |= py_full_year_months(common.cy_begin_month)
    return tuple(sorted(months))


def collect_chart_months(common: ProductLineReviewCommonFilters) -> tuple[date, ...]:
    """Union of every month the Full-Year chart needs (CY FY + NY FY)."""
    months = cy_full_year_months(common.cy_begin_month) | ny_full_year_months(
        common.cy_begin_month,
    )
    return tuple(sorted(months))


# ─────────────────────────────────────────────────────────────────────────────
# Base-plan normalisation (kept identical to the prior implementation —
# proven correct, no reason to disturb it)
# ─────────────────────────────────────────────────────────────────────────────

def prepare_ibp_base_plan_long(
    raw: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Tidy base-plan rows for Product Line Review joins.

    Output columns: ``month, pounds, pmaj, sfmt, pminor, brand, customer``.

    Index discipline
    ----------------
    The cycle filter (``work = work.loc[…]``) leaves *work* with a NON-
    contiguous index.  Mixing those Series with the merged frame's
    fresh RangeIndex inside a ``pd.DataFrame({…})`` constructor causes
    pandas to align on the index UNION and NaN-fill the rest — silently
    producing an all-blank ``pmaj/sfmt/brand`` frame and every base-plan
    sum collapsing to 0.  Two safeguards prevent that here:

      * ``work = work.reset_index(drop=True)`` AFTER the cycle filter
        normalises every downstream Series to RangeIndex 0..n-1.
      * Every Series passed to the final ``out`` constructor is
        ``.to_numpy()`` first so the result is laid out positionally
        rather than via index alignment.  Matches the contract already
        used by :func:`_enrich_ibp` in ``demand_plan_comparison``.
    """
    empty_cols = ["month", "pounds", "pmaj", "sfmt", "pminor", "brand", "customer"]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=empty_cols)

    month_col = _resolve_column(raw, _BP_MONTH_CANDIDATES)
    item_col = _resolve_column(raw, _BP_ITEM_CANDIDATES)
    total_col = _resolve_column(raw, _BP_TOTAL_CANDIDATES)
    port_col = _resolve_column(raw, _BP_PORTFOLIO_CANDIDATES)
    sfmt_col = _resolve_column(raw, _BP_SFMT_CANDIDATES)
    brand_col = _resolve_column(raw, _BP_BRAND_CANDIDATES)
    cust_col = _resolve_column(raw, _BP_CUSTOMER_CANDIDATES)
    cycle_col = _resolve_column(raw, _BP_CYCLE_CANDIDATES)

    if not (month_col and item_col and total_col):
        logger.warning(
            "ibp_base_plan_current missing required columns "
            "(month=%r, item=%r, total=%r).",
            month_col, item_col, total_col,
        )
        return pd.DataFrame(columns=empty_cols)

    work = raw.copy()
    if cycle_col:
        cycles = work[cycle_col].dropna().astype(str).str.strip()
        if not cycles.empty:
            work = work.loc[
                work[cycle_col].astype(str).str.strip() == cycles.max()
            ]
    # CRITICAL — see docstring.  After this point every per-column Series
    # is positionally aligned with the others.
    work = work.reset_index(drop=True)

    n = len(work)
    blank = pd.Series([""] * n, dtype="object")
    item_keys = _vectorised_item_key(work[item_col])
    pounds = pd.to_numeric(
        work[total_col].astype("string").str.replace(",", "", regex=False),
        errors="coerce",
    ).fillna(0.0)

    slim = pd.DataFrame({
        "item_key": item_keys.to_numpy(),
        "month": _vectorised_start_of_month(work[month_col]).to_numpy(),
        "pounds": pounds.to_numpy(),
        "customer": (
            _vectorised_clean_str(work[cust_col]).to_numpy()
            if cust_col else blank.to_numpy()
        ),
    })
    dim_frame = build_item_dim_frame(pdh_df)
    merged = _attach_dims(slim, slim["item_key"], dim_frame)

    # Portfolio / Product Format / Brand Category on the CSV win for those
    # dims; PDH supplies Portfolio Minor (and fills blanks for the rest).
    # ``.to_numpy()`` everywhere so the constructor stays positional.
    out = pd.DataFrame({
        "month": merged["month"].to_numpy(),
        "pounds": merged["pounds"].to_numpy(),
        "pmaj": (
            _vectorised_clean_str(work[port_col]).to_numpy()
            if port_col else merged["pmaj"].to_numpy()
        ),
        "sfmt": (
            _vectorised_clean_str(work[sfmt_col]).to_numpy()
            if sfmt_col else merged["sfmt"].to_numpy()
        ),
        "pminor": merged["pminor"].to_numpy(),
        "brand": (
            _vectorised_clean_str(work[brand_col]).to_numpy()
            if brand_col else merged["brand"].to_numpy()
        ),
        "customer": merged["customer"].to_numpy(),
    })
    return out.dropna(subset=["month"]).reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Dim-filter masking (PM + multi-select SFmt + multi-select Brand)
# ─────────────────────────────────────────────────────────────────────────────

def _apply_dim_filters(
    df: pd.DataFrame, filters: ProductLineReviewFilters,
) -> pd.Series:
    """Mask *df* by PM (single) + supply formats (multi) + brands (multi).

    Empty multi-select tuple ⇒ no constraint on that dimension (matches
    Streamlit's empty-multiselect semantics that the planner expects).
    """
    if df.empty:
        return pd.Series([], dtype=bool)
    mask = pd.Series(True, index=df.index)

    pmaj = filters.portfolio_major.strip()
    if pmaj:
        mask &= (
            df["pmaj"].astype(str).str.strip().str.casefold() == pmaj.casefold()
        )

    if filters.sub.supply_formats:
        wanted = {s.strip().casefold() for s in filters.sub.supply_formats if s.strip()}
        mask &= (
            df["sfmt"].astype(str).str.strip().str.casefold().isin(wanted)
        )

    if filters.sub.brands:
        wanted = {b.strip() for b in filters.sub.brands if b.strip()}
        mask &= df["brand"].astype(str).str.strip().isin(wanted)

    return mask


def _sum_millions(df: pd.DataFrame, mask: pd.Series) -> float:
    """Σ pounds (in millions) over *df* rows where *mask* is True."""
    if df.empty or not mask.any():
        return 0.0
    return float(df.loc[mask, "pounds"].sum()) / _LBS_PER_MILLION


# ─────────────────────────────────────────────────────────────────────────────
# Per-row measurement computation (unchanged math — the planner has already
# signed off on PY L3/L6/L12, YTG, FY definitions in earlier iterations)
# ─────────────────────────────────────────────────────────────────────────────

def _compute_measures(
    orders: pd.DataFrame,
    base: pd.DataFrame,
    filters: ProductLineReviewFilters,
    *,
    brand: str,
    pminor: str,
    sfmt: str,
    customer: Optional[str],
    month_sets: dict[str, set[date]],
    ro_lookup: dict[tuple[str, ...], float],
) -> _Measures:
    """Compute the 9 numeric measures for one (brand, pminor, sfmt) leaf."""
    def _slice_mask(df: pd.DataFrame) -> pd.Series:
        m = _apply_dim_filters(df, filters)
        m &= df["brand"].astype(str).str.strip() == brand.strip()
        m &= (
            df["pminor"].astype(str).str.strip().str.casefold()
            == pminor.strip().casefold()
        )
        m &= (
            df["sfmt"].astype(str).str.strip().str.casefold()
            == sfmt.strip().casefold()
        )
        if customer is not None:
            m &= df["customer"].astype(str).str.strip() == customer.strip()
        return m

    om = _slice_mask(orders)
    bm = _slice_mask(base)

    cy_m = filters.common.cy_month.replace(day=1)
    py_m = filters.py_month.replace(day=1)

    cm_py = _sum_millions(orders, om & (orders["month"] == py_m))
    cm_cy = _sum_millions(base, bm & (base["month"] == cy_m))
    ytg_py = _sum_millions(
        orders, om & orders["month"].isin(month_sets["py_ytg"]),
    )
    ytg_cy = _sum_millions(
        base, bm & base["month"].isin(month_sets["cy_ytg"]),
    )

    l3 = _sum_millions(orders, om & orders["month"].isin(month_sets["l3"]))
    l6 = _sum_millions(orders, om & orders["month"].isin(month_sets["l6"]))
    l12 = _sum_millions(orders, om & orders["month"].isin(month_sets["l12"]))

    fy_py = _sum_millions(orders, om & orders["month"].isin(month_sets["py_fy"]))
    fy_le = _sum_millions(base, bm & base["month"].isin(month_sets["cy_fy"]))

    fy_ro: Optional[float] = None
    if customer is None:
        path = resolve_ro_summary_path(
            pmaj=filters.portfolio_major,
            sfmt=sfmt, brand=brand, pminor=pminor,
        )
        if path is not None:
            fy_ro = float(ro_lookup.get(path, 0.0))

    return _Measures(
        cm_py=cm_py, cm_cy=cm_cy,
        ytg_py=ytg_py, ytg_cy=ytg_cy,
        rr_l3=l3 * 4.0,
        rr_l6=l6 * 2.0,
        rr_l12=l12,
        fy_py=fy_py, fy_le=fy_le, fy_ro=fy_ro,
    )


def _rollup_measures(children: list[_Measures]) -> _Measures:
    """Aggregate child measures.  R&O sums only defined (non-``None``) values."""
    if not children:
        return _Measures()
    fy_ro_vals = [c.fy_ro for c in children if c.fy_ro is not None]
    fy_ro = sum(fy_ro_vals) if fy_ro_vals else None
    return _Measures(
        cm_py=sum(c.cm_py for c in children),
        cm_cy=sum(c.cm_cy for c in children),
        ytg_py=sum(c.ytg_py for c in children),
        ytg_cy=sum(c.ytg_cy for c in children),
        rr_l3=sum(c.rr_l3 for c in children),
        rr_l6=sum(c.rr_l6 for c in children),
        rr_l12=sum(c.rr_l12 for c in children),
        fy_py=sum(c.fy_py for c in children),
        fy_le=sum(c.fy_le for c in children),
        fy_ro=fy_ro,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Display formatting
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_millions(value: float) -> str:
    return f"{value:.1f}"


def _fmt_pct(cy: float, py: float) -> str:
    if py == 0.0:
        return "nm"
    return f"{round((cy / py - 1.0) * 100)}%"


def _fmt_delta(cy: float, py: float) -> str:
    return _fmt_millions(cy - py)


def _fmt_optional_millions(value: Optional[float]) -> str:
    if value is None:
        return "–"
    return _fmt_millions(value)


def _measures_to_display_row(
    label: str,
    indent: int,
    m: _Measures,
    *,
    is_customer: bool,
) -> dict[str, object]:
    """Turn numeric measures into formatted strings for the UI table."""
    # Customer rows show ``–`` for R&O and Full-Year Total (planner rule:
    # R&O is only meaningful at the aggregate / template level).
    fy_ro_disp: Optional[float] = None if is_customer else m.fy_ro
    if is_customer:
        fy_total: Optional[float] = None
    elif m.fy_ro is None and m.fy_le == 0.0:
        fy_total = None
    else:
        fy_total = m.fy_le + (m.fy_ro or 0.0)

    return {
        COL_ROW_LABEL: label,
        COL_INDENT: indent,
        COL_IS_CUSTOMER: is_customer,
        COL_CM_PY: _fmt_millions(m.cm_py),
        COL_CM_CY: _fmt_millions(m.cm_cy),
        COL_CM_PCT: _fmt_pct(m.cm_cy, m.cm_py),
        COL_CM_LBS: _fmt_delta(m.cm_cy, m.cm_py),
        COL_YTG_PY: _fmt_millions(m.ytg_py),
        COL_YTG_CY: _fmt_millions(m.ytg_cy),
        COL_YTG_PCT: _fmt_pct(m.ytg_cy, m.ytg_py),
        COL_YTG_LBS: _fmt_delta(m.ytg_cy, m.ytg_py),
        COL_RR_L3: _fmt_millions(m.rr_l3),
        COL_RR_L6: _fmt_millions(m.rr_l6),
        COL_RR_L12: _fmt_millions(m.rr_l12),
        COL_FY_PY: _fmt_millions(m.fy_py),
        COL_FY_LE: _fmt_millions(m.fy_le),
        COL_FY_PCT: _fmt_pct(m.fy_le, m.fy_py),
        COL_FY_LBS: _fmt_delta(m.fy_le, m.fy_py),
        COL_FY_RO: _fmt_optional_millions(fy_ro_disp),
        COL_FY_TOTAL: _fmt_optional_millions(fy_total),
    }


def build_display_groups(
    filters: ProductLineReviewFilters,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Return ``((group, ((col_key, dynamic_label), ...)), ...)``.

    The CM ``Orders – {Mon YYYY}`` / ``Base Plan – {Mon YYYY}`` labels are
    interpolated from the active filters so the displayed header always
    echoes the active PY / CY month.  All other labels are static.
    """
    py = filters.py_month.strftime("%b %Y")
    cy = filters.common.cy_month.strftime("%b %Y")
    out: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for group, cols in _DISPLAY_GROUPS_TEMPLATE:
        formatted = tuple(
            (key, template.format(py=py, cy=cy))
            for key, template in cols
        )
        out.append((group, formatted))
    return tuple(out)


def flatten_display_columns(
    display_groups: tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
) -> tuple[tuple[str, str], ...]:
    """Return ``((col_key, label), ...)`` in display order — convenience helper."""
    flat: list[tuple[str, str]] = []
    for _group, cols in display_groups:
        flat.extend(cols)
    return tuple(flat)


# ─────────────────────────────────────────────────────────────────────────────
# Filter-discovery helpers
# ─────────────────────────────────────────────────────────────────────────────

def list_pdh_filter_values(
    pdh_df: Optional[pd.DataFrame],
) -> dict[str, list[str]]:
    """Distinct Portfolio Major / Supply Format / Brand from ``qry_pdh.csv``.

    Brand follows the same PDH rule as Demand Plan Comparison (first two
    characters of ``Item Description`` → Branded vs Private).
    """
    frame = build_item_dim_frame(pdh_df)
    if frame.empty:
        return {"portfolio_major": [], "supply_format": [], "brand": []}
    return {
        "portfolio_major": sorted({
            v for v in frame["pmaj"].astype(str).str.strip() if v
        }),
        "supply_format": sorted({
            v for v in frame["sfmt"].astype(str).str.strip() if v
        }),
        "brand": sorted({
            v for v in frame["brand"].astype(str).str.strip() if v
        }),
    }


def list_pdh_filter_values_for_pmaj(
    pdh_df: Optional[pd.DataFrame],
    portfolio_major: str,
) -> dict[str, list[str]]:
    """Return Supply Format + Brand values **restricted to one PM**.

    Drives the per-PM sub-filter dropdowns so the planner never sees a
    format that doesn't apply to the section they're looking at.
    """
    frame = build_item_dim_frame(pdh_df)
    if frame.empty or not portfolio_major.strip():
        return {"supply_format": [], "brand": []}
    pm_cf = portfolio_major.strip().casefold()
    sub = frame.loc[
        frame["pmaj"].astype(str).str.strip().str.casefold() == pm_cf
    ]
    return {
        "supply_format": sorted({
            v for v in sub["sfmt"].astype(str).str.strip() if v
        }),
        "brand": sorted({
            v for v in sub["brand"].astype(str).str.strip() if v
        }),
    }


def validate_common_filters(
    common: ProductLineReviewCommonFilters,
) -> list[str]:
    """Human-readable validation errors for the four common pickers."""
    errors: list[str] = []
    if common.cy_ytg_start.replace(day=1) > common.cy_ytg_end.replace(day=1):
        errors.append("CY YTG: beginning month is after the end month.")
    cy_begin = common.cy_begin_month.replace(day=1)
    allowed = eligible_cy_begin_months(common.cy_month)
    if cy_begin not in allowed:
        errors.append(
            f"CY Begin Month must be between {allowed[0]:%b %Y} and "
            f"{allowed[-1]:%b %Y} (12 months including CY Month)."
        )
    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Hierarchical table builder (per Portfolio Major)
# ─────────────────────────────────────────────────────────────────────────────

def build_product_line_review_table(
    *,
    orders_enriched: pd.DataFrame,
    base_long: pd.DataFrame,
    ro_current_plan_by_path: dict[tuple[str, ...], float],
    filters: ProductLineReviewFilters,
) -> ProductLineReviewResult:
    """Build one Portfolio Major's hierarchical table.

    Inputs are **already enriched** — :func:`enrich_ibp_orders_df` for
    orders and :func:`prepare_ibp_base_plan_long` for base plan — so the
    per-PM loop in the page can enrich once and reuse the frames.

    Parameters
    ----------
    orders_enriched
        IBP Orders frame with PDH dims attached.
    base_long
        Base-plan frame in the long shape produced by
        :func:`prepare_ibp_base_plan_long`.
    ro_current_plan_by_path
        ``{label_path → FY27 Current Plan Mlbs}`` map from the saved
        RO Summary Report.  Empty map → R&O FY = 0 with a soft warning.
    filters
        Per-PM filter set — PM label, common windows, per-PM sub-filters.
    """
    warnings: list[str] = []
    pmaj = filters.portfolio_major.strip()
    if not pmaj:
        return ProductLineReviewResult(
            table=pd.DataFrame(),
            warnings=("No Portfolio Major selected — nothing to build.",),
        )

    if orders_enriched.empty and base_long.empty:
        return ProductLineReviewResult(
            table=pd.DataFrame(),
            warnings=(f"No data for Portfolio Major '{pmaj}'.",),
        )

    if not ro_current_plan_by_path:
        warnings.append(
            "RO Summary Report is missing or lacks "
            "'FY27 Probabilized | Current Plan' — R&O FY will be zero."
        )

    cy_m = filters.common.cy_month.replace(day=1)
    month_sets: dict[str, set[date]] = {
        "l3":  trailing_months_end_at(cy_m, 3),
        "l6":  trailing_months_end_at(cy_m, 6),
        "l12": trailing_months_end_at(cy_m, 12),
        "py_ytg": months_in_range(
            filters.py_ytg_start.replace(day=1),
            filters.py_ytg_end.replace(day=1),
        ),
        "cy_ytg": months_in_range(
            filters.common.cy_ytg_start.replace(day=1),
            filters.common.cy_ytg_end.replace(day=1),
        ),
        "py_fy": py_full_year_months(filters.common.cy_begin_month),
        "cy_fy": cy_full_year_months(filters.common.cy_begin_month),
    }

    dim_mask_orders = _apply_dim_filters(orders_enriched, filters)
    dim_mask_base = _apply_dim_filters(base_long, filters)

    # Hierarchy keys present in either source (post-filter).
    keys: set[tuple[str, str, str]] = set()
    for df, mask in (
        (orders_enriched, dim_mask_orders),
        (base_long, dim_mask_base),
    ):
        if df.empty:
            continue
        sub = df.loc[mask]
        for br, pmn, sf in zip(
            sub["brand"].astype(str).str.strip(),
            sub["pminor"].astype(str).str.strip(),
            sub["sfmt"].astype(str).str.strip(),
        ):
            if br and sf:
                keys.add((br, pmn, sf))

    if not keys:
        return ProductLineReviewResult(
            table=pd.DataFrame(),
            warnings=tuple(warnings) + (
                f"No rows for Portfolio Major '{pmaj}' under the current "
                "Supply Format / Brand selection.",
            ),
        )

    # tree[brand][pminor] = [sfmt, ...]
    tree: dict[str, dict[str, list[str]]] = {}
    for brand, pminor, sfmt in sorted(keys, key=lambda t: (t[0], t[1], t[2])):
        tree.setdefault(brand, {}).setdefault(pminor, []).append(sfmt)

    rows: list[dict[str, object]] = []
    brand_rollups: list[_Measures] = []

    for brand in sorted(tree.keys()):
        pminor_rollups: list[_Measures] = []
        child_rows: list[tuple[str, _Measures, int]] = []
        for pminor in sorted(tree[brand].keys()):
            sfmt_rollups: list[_Measures] = []
            sfmt_rows: list[tuple[str, _Measures, int]] = []
            for sfmt in sorted(tree[brand][pminor]):
                m = _compute_measures(
                    orders_enriched, base_long, filters,
                    brand=brand, pminor=pminor, sfmt=sfmt,
                    customer=None,
                    month_sets=month_sets,
                    ro_lookup=ro_current_plan_by_path,
                )
                sfmt_rollups.append(m)
                sfmt_rows.append((sfmt, m, 2))
            pm = _rollup_measures(sfmt_rollups)
            pminor_rollups.append(pm)
            child_rows.append((pminor or "—", pm, 1))
            child_rows.extend(sfmt_rows)
        bm = _rollup_measures(pminor_rollups)
        brand_rollups.append(bm)
        rows.append(
            _measures_to_display_row(brand, indent=0, m=bm, is_customer=False),
        )
        for label, meas, indent in child_rows:
            rows.append(_measures_to_display_row(
                label, indent=indent, m=meas, is_customer=False,
            ))

    grand = _rollup_measures(brand_rollups)
    rows.append(_measures_to_display_row(
        "Grand Total", indent=0, m=grand, is_customer=False,
    ))

    # Customers from base plan (Plan To Name), sorted asc.
    if not base_long.empty:
        cust_sub = base_long.loc[dim_mask_base]
        customers = sorted({
            c for c in cust_sub["customer"].astype(str).str.strip() if c
        })
        for customer in customers:
            m = _compute_customer_measures(
                orders_enriched, base_long, filters, customer, month_sets,
            )
            rows.append(_measures_to_display_row(
                customer, indent=0, m=m, is_customer=True,
            ))

    return ProductLineReviewResult(
        table=pd.DataFrame(rows),
        warnings=tuple(warnings),
    )


def _compute_customer_measures(
    orders: pd.DataFrame,
    base: pd.DataFrame,
    filters: ProductLineReviewFilters,
    customer: str,
    month_sets: dict[str, set[date]],
) -> _Measures:
    """Aggregate all rows for one ``Plan To Name`` (ignores pminor/sfmt).

    Base plan matches on ``customer``; IBP Orders matches on
    ``customer_name`` (same label when the upstream export aligns).
    """
    cust_cf = customer.strip().casefold()

    def _cust_mask_base(df: pd.DataFrame) -> pd.Series:
        m = _apply_dim_filters(df, filters)
        m &= df["customer"].astype(str).str.strip().str.casefold() == cust_cf
        return m

    def _cust_mask_orders(df: pd.DataFrame) -> pd.Series:
        m = _apply_dim_filters(df, filters)
        name_col = "customer_name" if "customer_name" in df.columns else "customer"
        m &= df[name_col].astype(str).str.strip().str.casefold() == cust_cf
        return m

    cy_m = filters.common.cy_month.replace(day=1)
    py_m = filters.py_month.replace(day=1)
    om, bm = _cust_mask_orders(orders), _cust_mask_base(base)

    return _Measures(
        cm_py=_sum_millions(orders, om & (orders["month"] == py_m)),
        cm_cy=_sum_millions(base, bm & (base["month"] == cy_m)),
        ytg_py=_sum_millions(orders, om & orders["month"].isin(month_sets["py_ytg"])),
        ytg_cy=_sum_millions(base, bm & base["month"].isin(month_sets["cy_ytg"])),
        rr_l3=_sum_millions(orders, om & orders["month"].isin(month_sets["l3"])) * 4.0,
        rr_l6=_sum_millions(orders, om & orders["month"].isin(month_sets["l6"])) * 2.0,
        rr_l12=_sum_millions(orders, om & orders["month"].isin(month_sets["l12"])),
        fy_py=_sum_millions(orders, om & orders["month"].isin(month_sets["py_fy"])),
        fy_le=_sum_millions(base, bm & base["month"].isin(month_sets["cy_fy"])),
        fy_ro=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Full-Year chart — CY FY + NY FY series from qry_total_item_level_demand
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FullYearChartSeries:
    """One line on the per-PM Full-Year chart (12 monthly totals, raw lbs)."""
    label: str                       # legend label (e.g. "FY 2026")
    months: tuple[date, ...]         # actual calendar months (length 12)
    values_lbs: tuple[float, ...]    # length-12, indexed by FY position 1..12


@dataclass(frozen=True)
class FullYearChartData:
    """Chart payload: x-axis labels + 0..2 series for CY FY / NY FY."""
    fy_month_labels: tuple[str, ...]  # ("Apr","May",…,"Mar")
    series: tuple[FullYearChartSeries, ...]


def prepare_total_item_level_demand_long(
    raw: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Return a tidy ``qry_total_item_level_demand`` frame with PDH dims.

    Columns: ``month, pounds, pmaj, sfmt, brand``.  Items missing from
    PDH fall through with blank dims (the PM filter naturally drops them).

    Public + named symmetrically with :func:`prepare_ibp_base_plan_long`
    so the page can enrich this CSV ONCE before the per-PM chart loop —
    avoiding O(PM) full-frame enrichments which dominated the prior
    section's wall-clock latency.

    Same index-discipline contract as :func:`prepare_ibp_base_plan_long`:
    every Series is converted to a numpy array before the final
    ``pd.DataFrame`` constructor.
    """
    empty = pd.DataFrame(columns=["month", "pounds", "pmaj", "sfmt", "brand"])
    if raw is None or raw.empty:
        return empty

    # Use the constants exported by demand_summary so we never drift from
    # the canonical column names this CSV is published with.
    item_col = COL_ITEM if COL_ITEM in raw.columns else None
    month_col = COL_START_OF_MONTH if COL_START_OF_MONTH in raw.columns else None
    lbs_col = COL_DEMAND_LBS if COL_DEMAND_LBS in raw.columns else None
    if not (item_col and month_col and lbs_col):
        logger.warning(
            "qry_total_item_level_demand missing required columns "
            "(item=%r, month=%r, lbs=%r); chart will be empty.",
            item_col, month_col, lbs_col,
        )
        return empty

    work = raw.reset_index(drop=True)
    item_keys = _vectorised_item_key(work[item_col])
    pounds = pd.to_numeric(
        work[lbs_col].astype("string").str.replace(",", "", regex=False),
        errors="coerce",
    ).fillna(0.0)
    months = _vectorised_start_of_month(work[month_col])

    slim = pd.DataFrame({
        "item_key": item_keys.to_numpy(),
        "month": months.to_numpy(),
        "pounds": pounds.to_numpy(),
    })
    dim_frame = build_item_dim_frame(pdh_df)
    merged = _attach_dims(slim, slim["item_key"], dim_frame)

    out = pd.DataFrame({
        "month": merged["month"].to_numpy(),
        "pounds": merged["pounds"].to_numpy(),
        "pmaj": merged["pmaj"].to_numpy(),
        "sfmt": merged["sfmt"].to_numpy(),
        "brand": merged["brand"].to_numpy(),
    })
    return out.dropna(subset=["month"]).reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Pre-aggregation helpers — collapse to the minimum-distinct-dim grain
# the PLR builder + chart actually consume.  Done ONCE in the page (before
# the per-PM loop) so each PM's mask-and-sum work is essentially instant.
# ─────────────────────────────────────────────────────────────────────────────

def aggregate_orders_for_plr(orders_enriched: pd.DataFrame) -> pd.DataFrame:
    """Group enriched IBP Orders by every dim used downstream and sum lbs.

    Columns kept: ``pmaj, sfmt, brand, pminor, customer_name, month, pounds``.
    Reduces a typical 350k-row enriched frame to a few thousand rows so the
    per-PM mask + groupby loop is bound by dim cardinality, not raw shape.
    """
    if orders_enriched is None or orders_enriched.empty:
        return pd.DataFrame(columns=[
            "pmaj", "sfmt", "brand", "pminor", "customer_name", "month", "pounds",
        ])
    # ``customer_name`` is the field IBP Orders carries; the customer mask
    # below probes for it (with ``customer`` as fallback).  Keep both
    # available so the per-PM customer rows still match cleanly.
    cust_col = "customer_name" if "customer_name" in orders_enriched.columns else "customer"
    keep = ["pmaj", "sfmt", "brand", "pminor", cust_col, "month"]
    grouped = (
        orders_enriched
        .groupby(keep, as_index=False, dropna=False)["pounds"]
        .sum()
    )
    if cust_col != "customer_name":
        grouped = grouped.rename(columns={cust_col: "customer_name"})
    return grouped


def aggregate_base_plan_for_plr(base_long: pd.DataFrame) -> pd.DataFrame:
    """Group base-plan rows by every dim used downstream and sum lbs.

    Columns kept: ``pmaj, sfmt, brand, pminor, customer, month, pounds``.
    """
    if base_long is None or base_long.empty:
        return pd.DataFrame(columns=[
            "pmaj", "sfmt", "brand", "pminor", "customer", "month", "pounds",
        ])
    keep = ["pmaj", "sfmt", "brand", "pminor", "customer", "month"]
    return (
        base_long
        .groupby(keep, as_index=False, dropna=False)["pounds"]
        .sum()
    )


def aggregate_total_demand_for_plr(
    total_demand_long: pd.DataFrame,
) -> pd.DataFrame:
    """Group qry_total_item_level_demand rows for the Full-Year chart.

    The chart only filters on ``pmaj / sfmt / brand`` and reports by
    ``month`` — collapsing to that grain drops every per-item /
    per-Forecast-Type row from the masking path.
    """
    if total_demand_long is None or total_demand_long.empty:
        return pd.DataFrame(columns=["pmaj", "sfmt", "brand", "month", "pounds"])
    return (
        total_demand_long
        .groupby(["pmaj", "sfmt", "brand", "month"], as_index=False, dropna=False)
        ["pounds"]
        .sum()
    )


# ─────────────────────────────────────────────────────────────────────────────
# Chart payload assembly (consumes the ALREADY-ENRICHED long frame)
# ─────────────────────────────────────────────────────────────────────────────

def build_full_year_chart_data(
    *,
    total_demand_long: pd.DataFrame,
    portfolio_major: str,
    sub: ProductLineReviewSubFilters,
    cy_begin_month: date,
) -> FullYearChartData:
    """Build two fiscal-year series (CY FY + NY FY) for one PM.

    * **CY FY** spans ``cy_begin_month`` → ``cy_begin_month + 11``
    * **NY FY** spans ``cy_begin_month + 12`` → ``cy_begin_month + 23``

    Both lines share the **same** fiscal-month axis (Apr = position 1,
    … Mar = position 12) so the planner can visually compare the same
    month-of-fiscal-year across years.

    The chart respects the per-PM Supply Format + Brand sub-filters.
    Empty multi-select tuples mean "include all".

    Performance contract
    --------------------
    *total_demand_long* MUST come from
    :func:`prepare_total_item_level_demand_long` (and ideally
    :func:`aggregate_total_demand_for_plr`).  The page does that once
    BEFORE the per-PM loop so the chart's per-PM cost is just a small
    mask + groupby (no item-level enrichment).
    """
    cy_window = sorted(cy_full_year_months(cy_begin_month))
    ny_window = sorted(ny_full_year_months(cy_begin_month))

    if total_demand_long is None or total_demand_long.empty:
        return FullYearChartData(
            fy_month_labels=FY_MONTH_LABELS, series=(),
        )

    # Single mask once (PM + sub-filters); reuse for both windows.
    pm_cf = portfolio_major.strip().casefold()
    mask = (
        total_demand_long["pmaj"]
        .astype(str).str.strip().str.casefold() == pm_cf
    )
    if sub.supply_formats:
        wanted = {s.strip().casefold() for s in sub.supply_formats if s.strip()}
        mask &= (
            total_demand_long["sfmt"]
            .astype(str).str.strip().str.casefold().isin(wanted)
        )
    if sub.brands:
        wanted_b = {b.strip() for b in sub.brands if b.strip()}
        mask &= (
            total_demand_long["brand"].astype(str).str.strip().isin(wanted_b)
        )

    sliced = total_demand_long.loc[mask, ["month", "pounds"]]
    if sliced.empty:
        return FullYearChartData(
            fy_month_labels=FY_MONTH_LABELS, series=(),
        )

    by_month = sliced.groupby("month", as_index=True)["pounds"].sum()

    def _series_for(window: list[date], label: str) -> FullYearChartSeries:
        values = tuple(float(by_month.get(m, 0.0)) for m in window)
        return FullYearChartSeries(
            label=label, months=tuple(window), values_lbs=values,
        )

    # Label each series by its fiscal-year designation (year of the LAST
    # month in the window — March is FY position 12, so a window that
    # ends in March 2027 is FY 2027 by Darigold's convention).
    cy_label = f"FY {cy_window[-1].year}"
    ny_label = f"FY {ny_window[-1].year}"
    return FullYearChartData(
        fy_month_labels=FY_MONTH_LABELS,
        series=(_series_for(cy_window, cy_label), _series_for(ny_window, ny_label)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public surface
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    # Filter dataclasses.
    "ProductLineReviewCommonFilters",
    "ProductLineReviewSubFilters",
    "ProductLineReviewFilters",
    "ProductLineReviewResult",
    "resolve_filters",
    # Validation + filter discovery.
    "validate_common_filters",
    "list_pdh_filter_values",
    "list_pdh_filter_values_for_pmaj",
    # Month arithmetic.
    "add_months",
    "eligible_cy_begin_months",
    "trailing_months_end_at",
    "cy_full_year_months",
    "py_full_year_months",
    "ny_full_year_months",
    "collect_ibp_months",
    "collect_ibp_months_for_common",
    "collect_chart_months",
    # Source normalisation + builders.
    "prepare_ibp_base_plan_long",
    "prepare_total_item_level_demand_long",
    "aggregate_orders_for_plr",
    "aggregate_base_plan_for_plr",
    "aggregate_total_demand_for_plr",
    "build_product_line_review_table",
    "build_full_year_chart_data",
    # Display surface.
    "COL_ROW_LABEL",
    "COL_INDENT",
    "COL_IS_CUSTOMER",
    "FY_MONTH_LABELS",
    "build_display_groups",
    "flatten_display_columns",
    # Chart payload types.
    "FullYearChartSeries",
    "FullYearChartData",
    # Re-exports (so the page never reaches into demand_plan_comparison).
    "BRAND_BRANDED",
    "BRAND_PRIVATE",
]
