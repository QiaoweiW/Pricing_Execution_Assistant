"""Demand Plan Comparison Summary — cycle-over-cycle roll-up builder.

This module owns the *Demand Plan Comparison Summary* that renders
beneath the *Demand MOM Summary* on the Demand Planner Analytics page.
It is a **pure-logic** module — no Streamlit, no Fabric I/O.  The page
hands it already-loaded DataFrames (tracker, IBP Shipments, PDH, and the
saved RO Summary Report) plus a :class:`ComparisonFilters` selection, and
gets back a fully-shaped, display-ready :class:`ComparisonResult`.

Why a separate module
---------------------
* Keeps the page thin (it only wires widgets → filters → this builder →
  a styled table).
* Makes the column math unit-testable without a Streamlit session.
* Mirrors the separation already used by ``ro_summary_report.py`` (a
  hardcoded business-reporting template + a pure recompute pass).

Data sources (all passed in by the page)
-----------------------------------------
* **Tracker** — ``qry_mgmt_plan_history_tracker.csv``.  One row per
  Item × Party Site × Month × Cycle × Forecast Type::

      Start of Month, Item, Item Description, Party Site Number,
      Demand Plan Pounds, Forecast Type, Business Unit, Cycle

  Supplies every *plan* number (current / prior cycle, actual / forecast
  / prior-month buckets).  Months are taken from ``Start of Month``.
* **IBP Shipments** — ``dbo.IBP Shipments``.  Supplies every *actual*
  number via ``Shipped Qty lbs``; its ``Month`` column is already
  first-of-month so it aligns directly with the tracker's
  ``Start of Month``.
* **PDH** — ``qry_pdh.csv``.  Per-item dimension lookup (Portfolio
  Major / Supply Format / Portfolio Minor) joined on ``Item No``.  Also
  the source of truth for **Brand**: the first two characters of the
  PDH ``Item Description`` — ``"DG"`` → *Branded*, otherwise *Private*.
* **RO Summary Report** — ``RO_Reporting/RO_Summary_Report.csv``.  The
  **R&O** column is its ``FY27 Probabilized | Total Delta`` value,
  matched to each comparison row by hierarchy/label path.

Row hierarchy (the reporting contract — see :data:`COMPARISON_TEMPLATE`)
-----------------------------------------------------------------------
    Total B2C
      ESL
        Large Carton            (Branded / Private)
        Small Carton            (Branded / Private)
        Aerosol Can
      Aseptic                   (Branded / Private)
      Cultured
        Large Tub
        Small Tub
        Pail
        Cottage Cheese  (memo — Portfolio Minor split, not summed)
        Sour Cream      (memo — Portfolio Minor split, not summed)
      Fresh Milk
        Gallon Jug / Caseless Jug / Mini Carton / HG Jug /
        Bossy / Totes (Dispenser) / Tanker
      Butter

Subtotals are the sum of their *non-memo* children.  Cottage Cheese /
Sour Cream are **memo** rows: they show an alternate Portfolio-Minor
breakdown of the Cultured total and are therefore excluded from the
Cultured subtotal to avoid double-counting.

Units
-----
Every value is in **millions of pounds**, rounded to two decimal places
for display.

Budget column
-------------
Leaf-row **Budget** values are read from
``Files/RO Tracking/Demand Plan/FY27_Budget_Demand_Plan_Summary.xlsx``
(see :func:`fetch_fy27_budget_by_row_id`).  Subtotals sum their children.
"""
from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

# Reuse the battle-tested coercion / join primitives from the Demand
# Summary connector rather than re-implementing them — one definition,
# one behaviour, for Start-of-Month parsing, item-key normalisation, and
# Forecast Type bucketing across the whole Demand Planner page.
from data_sources.demand_summary import (
    _coerce_start_of_month,
    _normalise_forecast_type,
    _normalise_item_key,
    _resolve_column,
    FORECAST_BASE_PLAN,
    FORECAST_R_AND_O,
    # Shared pivot-assembly primitives — the Demand MOM Summary reuses the
    # exact hierarchy walk / footer / chart shaping the classic Demand
    # Pivot uses, so the two views stay pixel-identical in layout.
    BudgetLookup,
    MonthlyBudgetLookup,
    PMAJ_BLANK_LABEL,
    assemble_hierarchical_pivot,
    build_month_wide,
    chart_long_from_grouped,
    footer_row_frame,
    footer_wide_from_grouped,
    forecast_month_grouped,
    monthly_budget_footer,
    _format_month_label,
    _COL_FORECAST_DIM,
    _COL_PMAJ_DIM,
    _COL_SFMT_DIM,
)
from data_sources.fabric_lakehouse_io import LakehouseIOError, read_bytes, read_csv


logger = logging.getLogger(__name__)


# ── Errors ───────────────────────────────────────────────────────────────────

class DemandPlanComparisonError(RuntimeError):
    """Raised on any Demand Plan Comparison build / parse failure.

    Distinct from the connector errors so the page can render a
    comparison-specific banner without masking the upstream Fabric read
    errors (which have their own, separate error types).
    """


# ─────────────────────────────────────────────────────────────────────────────
# Source schema — column-name constants
# ─────────────────────────────────────────────────────────────────────────────
#
# Pinned in one place so a schema drift upstream is a one-line fix here.

# Tracker (qry_mgmt_plan_history_tracker.csv).
TRK_START_OF_MONTH: str = "Start of Month"
TRK_ITEM: str           = "Item"
TRK_PARTY_SITE: str     = "Party Site Number"
TRK_DEMAND_LBS: str     = "Demand Plan Pounds"
TRK_FORECAST_TYPE: str  = "Forecast Type"
TRK_BUSINESS_UNIT: str  = "Business Unit"
TRK_CYCLE: str          = "Cycle"
# Categorisation dims now carried ON the tracker file (written by the Demand
# Plan pipeline).  When present the comparison reads them straight off the
# file instead of re-joining PDH / RO_Item_Master; Brand is NOT a column — it
# is derived from the Item Description exactly as before.
TRK_PMAJ: str           = "Portfolio Major"
TRK_SFMT: str           = "Supply Format"
TRK_PMINOR: str         = "Portfolio Minor"
_TRK_DIM_COLS: tuple[str, ...] = (TRK_PMAJ, TRK_SFMT, TRK_PMINOR)

# IBP Shipments (dbo.IBP Shipments).  Column names probed from a small
# whitelist because the Delta table's spelling has historically wobbled
# between exports ("Item No" vs "Item Number", etc.).
_IBP_ITEM_CANDIDATES: tuple[str, ...] = (
    "Item No", "Item", "Item Number", "Item #", "ItemNo",
)
_IBP_MONTH_CANDIDATES: tuple[str, ...] = (
    "Month", "Start of Month", "Ship Month", "Shipment Month",
)
_IBP_QTY_CANDIDATES: tuple[str, ...] = (
    "Shipped Qty lbs", "Shipped Qty Lbs", "Shipped Qty", "Shipped Quantity Lbs",
    "Shipped_Qty_lbs",
)
_IBP_ORDERED_QTY_CANDIDATES: tuple[str, ...] = (
    "Ordered Qty lbs", "Ordered Qty Lbs", "Ordered Qty", "Ordered Quantity Lbs",
    "Ordered_Qty_lbs",
)
_IBP_CUSTOMER_NO_CANDIDATES: tuple[str, ...] = (
    "Customer No", "Customer Number", "Customer No.", "CustomerNo", "Customer_No",
)
_IBP_CUSTOMER_NAME_CANDIDATES: tuple[str, ...] = (
    "Customer Name", "CustomerName", "Customer_Name",
)

# Tracker (qry_mgmt_plan_history_tracker.csv) — Item Description column.
# Used as a fallback only; driver tables prefer the PDH description so
# item naming is consistent across plan + actuals.
TRK_ITEM_DESCRIPTION: str = "Item Description"

# PDH (qry_pdh.csv).
_PDH_ITEM_CANDIDATES: tuple[str, ...] = (
    "Item No", "Item", "Item Number", "Item #", "ItemNo", "Item_No",
)
_PDH_DESC_CANDIDATES: tuple[str, ...] = (
    "Item Description", "Item Desc", "Description", "ItemDescription",
)
_PDH_PMAJ_CANDIDATES: tuple[str, ...] = (
    "Portfolio Major", "Portfolio_Major", "PortfolioMajor",
)
_PDH_PMINOR_CANDIDATES: tuple[str, ...] = (
    "Portfolio Minor", "Portfolio_Minor", "PortfolioMinor",
)
_PDH_SFMT_CANDIDATES: tuple[str, ...] = (
    "Supply Format", "Supply_Format", "SupplyFormat", "SFmt",
)

# dp_dimshiptosites (dbo) — ship-to-site dimension used by the driver
# tables to translate a Party Site Number into a customer.  Probed from
# candidate names so an upstream rename is a one-line fix here.
_DIM_PARTY_SITE_CANDIDATES: tuple[str, ...] = (
    "party_site_code", "party_site_number", "PartySiteCode", "Party Site Code",
)
_DIM_CUSTOMER_NUM_CANDIDATES: tuple[str, ...] = (
    "customer_num", "customer_number", "CustomerNum", "Customer Num",
)
_DIM_ACCOUNT_DESC_CANDIDATES: tuple[str, ...] = (
    "account_description", "account_desc", "AccountDescription",
)

# RO Summary Report (RO_Reporting/RO_Summary_Report.csv).
_RO_SUMMARY_REPORT_BLOB_PATH: str = (
    "RO Tracking/RO_Reporting/RO_Summary_Report.csv"
)
# FY27 leaf budgets (millions of lbs) — row labels mirror COMPARISON_TEMPLATE.
_FY27_BUDGET_BLOB_PATH: str = (
    "RO Tracking/Demand Plan/FY27_Budget_Demand_Plan_Summary.xlsx"
)
_RO_SUMMARY_SECRETS_SECTION: str = "fabric_htst"
# The label (tree) column and the metric column we read.  Primary names
# match the labels produced by
# ``ro_summary_report.prepare_summary_for_export``; the extra candidates
# tolerate minor header drift (trailing-dot, spacing) so a slightly
# different export still populates R&O instead of silently zeroing it.
_RO_SUMMARY_LABEL_CANDIDATES: tuple[str, ...] = (
    "Millions of lbs.", "Millions of lbs", "Millions of Lbs.",
    "Row Label", "Label",
)
_RO_SUMMARY_TOTAL_DELTA_CANDIDATES: tuple[str, ...] = (
    "FY27 Probabilized | Total Delta",
    "FY27 Probabilized|Total Delta",
    "FY27 Probabilized  | Total Delta",
)
# Non-breaking space used by the RO Summary exporter to indent the tree.
_NBSP: str = "\u00A0"


# ── Brand derivation ─────────────────────────────────────────────────────────

BRAND_BRANDED: str = "Branded"
BRAND_PRIVATE: str = "Private"


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio Major synonym sets
# ─────────────────────────────────────────────────────────────────────────────
#
# Upstream dimension tables disagree on the literal PMaj string for the
# same business family (e.g. fresh milk shows up as both "Fresh Milk" and
# "HTST").  Matching uses these frozensets so leaves roll up correctly
# regardless of which spelling PDH carries.  Display labels are separate
# (they always use the screenshot's wording).

_ESL: frozenset = frozenset({"ESL", "Extended Shelf Life"})
_CULTURED: frozenset = frozenset({"Cultured"})
_FRESH_MILK: frozenset = frozenset({"Fresh Milk", "HTST"})
# Tanker fresh-milk rows live under the bulk-fluid PMaj per the planner's
# rule ("Fresh Milk or Bulk Fluid HTST").
_FRESH_MILK_TANKER: frozenset = frozenset({"Fresh Milk", "Bulk Fluid HTST", "HTST"})
_BUTTER: frozenset = frozenset({"Butter"})
# Butter is further restricted to a single Portfolio Minor so bulk /
# ingredient butter sharing the "Butter" PMaj is excluded (planner rule,
# 2026-06).  Matched case-insensitively against PDH's Portfolio Minor.
_BUTTER_PMINOR: str = "Packaged Butter"


# ─────────────────────────────────────────────────────────────────────────────
# Row template
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TemplateRow:
    """One row of the Demand Plan Comparison hierarchy.

    Attributes
    ----------
    row_id
        Stable internal id (used for subtotal child references).
    label
        Display text for the indented row-label column.
    indent
        Tree depth (0 = Total B2C) — drives the NBSP indent.
    is_subtotal
        ``True`` → value columns are the sum of *children*; ``False`` →
        a leaf computed directly from the source data.
    is_memo
        ``True`` → an informational row (Cottage Cheese / Sour Cream)
        that is computed and displayed but **excluded** from its
        parent's subtotal to avoid double-counting.
    children
        Child ``row_id``s for subtotals (empty for leaves).
    pmaj_match / sfmt_match / brand_match / pminor_match
        Leaf filter criteria against the enriched (PDH-joined) frames.
        ``None`` means "no constraint on this dimension".
    ro_summary_path
        Hierarchy/label path into the saved RO Summary Report used to
        read this row's **R&O** value (its ``FY27 Total Delta``).
        ``None`` → no RO counterpart, R&O = 0.
    budget_m
        Fixed, hard-coded **Budget** for this leaf in millions of lbs.
        Subtotals ignore this and sum their children instead.
    """
    row_id: str
    label: str
    indent: int
    is_subtotal: bool = False
    is_memo: bool = False
    children: tuple[str, ...] = ()
    pmaj_match: Optional[frozenset] = None
    sfmt_match: Optional[str] = None
    brand_match: Optional[str] = None
    pminor_match: Optional[str] = None
    ro_summary_path: Optional[tuple[str, ...]] = None
    budget_m: float = 0.0


def _subtotal(
    row_id: str, label: str, indent: int, children: tuple[str, ...],
) -> TemplateRow:
    """Shorthand for a subtotal row (value = sum of *children*)."""
    return TemplateRow(
        row_id=row_id, label=label, indent=indent,
        is_subtotal=True, children=children,
    )


def _leaf(
    row_id: str, label: str, indent: int, *,
    pmaj_match: Optional[frozenset] = None,
    sfmt_match: Optional[str] = None,
    brand_match: Optional[str] = None,
    pminor_match: Optional[str] = None,
    ro_summary_path: Optional[tuple[str, ...]] = None,
    is_memo: bool = False,
    budget_m: float = 0.0,
) -> TemplateRow:
    """Shorthand for a leaf row (value computed directly from source)."""
    return TemplateRow(
        row_id=row_id, label=label, indent=indent,
        is_subtotal=False, is_memo=is_memo, children=(),
        pmaj_match=pmaj_match, sfmt_match=sfmt_match,
        brand_match=brand_match, pminor_match=pminor_match,
        ro_summary_path=ro_summary_path, budget_m=budget_m,
    )


# RO Summary Report path prefixes (its exact label wording).  Hoisted so
# the leaf declarations below stay readable and the RO label spellings
# live in one place.
_RO_TOTAL = "Total B2C"
_RO_ESL = "Extended Shelf Life"   # RO Summary labels ESL as "Extended Shelf Life"
_RO_ASEPTIC = "Aseptic"
_RO_CULTURED = "Cultured"
_RO_FRESH_MILK = "Fresh Milk"
_RO_BRANDED = "Branded"
_RO_PRIVATE = "Private Label"      # RO Summary labels Private as "Private Label"


# The reporting template.  Declaration order == display order.  Subtotals
# are declared before their children (top-down, screenshot order); the
# recompute pass walks children via the id graph so declaration order has
# no effect on the math.
COMPARISON_TEMPLATE: tuple[TemplateRow, ...] = (
    _subtotal("total_b2c", "Total B2C", 0, ("esl", "aseptic", "cultured", "fresh_milk", "butter")),

    # ── ESL ──────────────────────────────────────────────────────────
    # ESL = Large Carton + Small Carton + Aerosol Can.
    _subtotal("esl", "ESL", 1, ("esl_lc", "esl_sc", "esl_aerosol")),
    _subtotal("esl_lc", "Large Carton", 2, ("esl_lc_branded", "esl_lc_private")),
    _leaf("esl_lc_branded", "Branded", 3,
          pmaj_match=_ESL, sfmt_match="Large Carton", brand_match=BRAND_BRANDED,
          ro_summary_path=(_RO_TOTAL, _RO_ESL, "Large Carton", _RO_BRANDED)),
    _leaf("esl_lc_private", "Private", 3,
          pmaj_match=_ESL, sfmt_match="Large Carton", brand_match=BRAND_PRIVATE,
          ro_summary_path=(_RO_TOTAL, _RO_ESL, "Large Carton", _RO_PRIVATE)),
    _subtotal("esl_sc", "Small Carton", 2, ("esl_sc_branded", "esl_sc_private")),
    _leaf("esl_sc_branded", "Branded", 3,
          pmaj_match=_ESL, sfmt_match="Small Carton", brand_match=BRAND_BRANDED,
          ro_summary_path=(_RO_TOTAL, _RO_ESL, "Small Carton", _RO_BRANDED)),
    _leaf("esl_sc_private", "Private", 3,
          pmaj_match=_ESL, sfmt_match="Small Carton", brand_match=BRAND_PRIVATE,
          ro_summary_path=(_RO_TOTAL, _RO_ESL, "Small Carton", _RO_PRIVATE)),
    _leaf("esl_aerosol", "Aerosol Can", 2,
          pmaj_match=_ESL, sfmt_match="Aerosol Can"),

    # ── Aseptic ─────────────────────────────────────────────────────
    _subtotal("aseptic", "Aseptic", 1, ("asep_branded", "asep_private")),
    _leaf("asep_branded", "Branded", 2,
          pmaj_match=_ESL, sfmt_match="Aseptic", brand_match=BRAND_BRANDED,
          ro_summary_path=(_RO_TOTAL, _RO_ASEPTIC, _RO_BRANDED)),
    _leaf("asep_private", "Private", 2,
          pmaj_match=_ESL, sfmt_match="Aseptic", brand_match=BRAND_PRIVATE,
          ro_summary_path=(_RO_TOTAL, _RO_ASEPTIC, _RO_PRIVATE)),

    # ── Cultured ────────────────────────────────────────────────────
    # Subtotal = Large Tub + Small Tub + Pail (the Supply Format split).
    # Cottage Cheese / Sour Cream are MEMO rows (Portfolio Minor split of
    # the same total) and are deliberately NOT children of the subtotal.
    _subtotal("cultured", "Cultured", 1, ("cult_large_tub", "cult_small_tub", "cult_pail")),
    _leaf("cult_large_tub", "Large Tub", 2,
          pmaj_match=_CULTURED, sfmt_match="Large Tub",
          ro_summary_path=(_RO_TOTAL, _RO_CULTURED, "Large Tub")),
    _leaf("cult_small_tub", "Small Tub", 2,
          pmaj_match=_CULTURED, sfmt_match="Small Tub",
          ro_summary_path=(_RO_TOTAL, _RO_CULTURED, "Small Tub")),
    _leaf("cult_pail", "Pail", 2,
          pmaj_match=_CULTURED, sfmt_match="Pail"),
    _leaf("cult_cottage_cheese", "Cottage Cheese", 2,
          pmaj_match=_CULTURED, pminor_match="Cottage Cheese", is_memo=True),
    _leaf("cult_sour_cream", "Sour Cream", 2,
          pmaj_match=_CULTURED, pminor_match="Sour Cream", is_memo=True),

    # ── Fresh Milk ──────────────────────────────────────────────────
    # Subtotal = Gallon Jug … Tanker.
    _subtotal("fresh_milk", "Fresh Milk", 1,
              ("fm_gallon_jug", "fm_caseless_jug", "fm_mini_carton",
               "fm_hg_jug", "fm_bossy", "fm_totes", "fm_tanker")),
    _leaf("fm_gallon_jug", "Gallon Jug", 2,
          pmaj_match=_FRESH_MILK, sfmt_match="Gallon Jug",
          ro_summary_path=(_RO_TOTAL, _RO_FRESH_MILK, "Gallon Jug")),
    _leaf("fm_caseless_jug", "Caseless Jug", 2,
          pmaj_match=_FRESH_MILK, sfmt_match="Caseless Jug",
          ro_summary_path=(_RO_TOTAL, _RO_FRESH_MILK, "Caseless Jug")),
    _leaf("fm_mini_carton", "Mini Carton", 2,
          pmaj_match=_FRESH_MILK, sfmt_match="Mini Carton",
          ro_summary_path=(_RO_TOTAL, _RO_FRESH_MILK, "Mini Carton")),
    _leaf("fm_hg_jug", "HG Jug", 2,
          pmaj_match=_FRESH_MILK, sfmt_match="HG Jug",
          ro_summary_path=(_RO_TOTAL, _RO_FRESH_MILK, "HG Jug")),
    _leaf("fm_bossy", "Bossy", 2,
          pmaj_match=_FRESH_MILK, sfmt_match="Bossy",
          ro_summary_path=(_RO_TOTAL, _RO_FRESH_MILK, "Bossy")),
    # "Totes" under Fresh Milk matches Supply Format = "Dispenser" (per
    # the planner's rule), and maps to the RO Summary "Totes" leaf.
    _leaf("fm_totes", "Totes", 2,
          pmaj_match=_FRESH_MILK, sfmt_match="Dispenser",
          ro_summary_path=(_RO_TOTAL, _RO_FRESH_MILK, "Totes")),
    # Tanker rows live under "Fresh Milk or Bulk Fluid HTST"; no RO
    # Summary counterpart, so R&O stays 0.
    _leaf("fm_tanker", "Tanker", 2,
          pmaj_match=_FRESH_MILK_TANKER, sfmt_match="Tanker"),

    # ── Butter ──────────────────────────────────────────────────────
    # Per planner direction (2026-06), Butter is now scoped by BOTH
    # Portfolio Major = Butter AND Portfolio Minor = "Packaged Butter"
    # (e.g. excludes bulk/ingredient butter that shares the Butter PMaj).
    # Both values come from PDH; the mask ANDs them in _leaf_mask.
    _leaf("butter", "Butter", 1,
          pmaj_match=_BUTTER, pminor_match=_BUTTER_PMINOR,
          ro_summary_path=(_RO_TOTAL, "Butter")),
)

TEMPLATE_BY_ID: dict[str, TemplateRow] = {row.row_id: row for row in COMPARISON_TEMPLATE}


# ─────────────────────────────────────────────────────────────────────────────
# Output column identifiers + display labels
# ─────────────────────────────────────────────────────────────────────────────
#
# Internal ids drive the math; display labels (mirroring screenshot 1)
# are applied at the very end.  Splitting the two lets us reorder / rename
# the on-screen table without touching the computation.

# Additive base measures — these roll up as the sum of children.
COL_TOTAL_ACTUALS: str          = "total_actuals"
COL_PRIOR_MONTH_ACTUAL: str     = "prior_month_actual"
COL_PRIOR_MONTH_FORECAST: str   = "prior_month_forecast"
COL_CURRENT_PLAN_ACTUAL: str    = "current_plan_actual"
# Current-cycle forecast over the forecast window, SPLIT by Forecast Type into
# its Base Plan and R&O legs (replaces the old single "Current Plan (Forecast)"
# column).  Current Plan = actuals + Base + R&O.
COL_CURRENT_PLAN_BASE: str      = "current_plan_base"
COL_CURRENT_PLAN_RO: str        = "current_plan_ro"
# Prior-Year Actual: shipments over the plan's full horizon shifted back 12
# months — [actual_start − 1yr … forecast_end − 1yr].  Additive.
COL_PY_ACTUAL: str              = "py_actual"
# Last Plan's two independent legs — the "one-month-ago" snapshot:
#   * actuals over [actual_start … actual_end − 1 month]
#   * PRIOR-cycle forecast over [forecast_start − 1 month … forecast_end]
# so Last Plan = last_plan_actuals + last_plan_forecast (see _assemble_table).
COL_LAST_PLAN_ACTUALS: str      = "last_plan_actuals"
COL_LAST_PLAN_FORECAST: str     = "last_plan_forecast"
COL_BASE_PLAN: str              = "base_plan"
COL_R_AND_O: str                = "r_and_o"
COL_BUDGET: str                 = "budget"

# Derived columns — linear combinations of the additive measures (so
# computing them post-roll-up equals summing them; we compute per row).
COL_CURRENT_PLAN: str           = "current_plan"
COL_PM_ACTUAL: str              = "pm_actual"
COL_TOTAL_DELTA: str            = "total_delta"
COL_LAST_PLAN: str              = "last_plan"
COL_V_BUDGET: str               = "v_budget"
# NB: COL_BASE_PLAN (declared above with the additive names for display
# ordering) is a DERIVED residual — Total Delta − PM Actual − R&O — computed
# in _assemble_table, NOT an independently-summed measure.  Defining it this
# way makes the columns always reconcile: Base Plan + PM Actual + R&O ≡
# Total Delta, so Base Plan absorbs any gap between the Current/Last plan
# walk and the PM-Actual + R&O components.

# Ratio columns — NOT additive; computed from each row's own derived
# values after roll-up.
COL_TOTAL_DELTA_PCT: str        = "total_delta_pct"
COL_PCT: str                    = "pct"
# Base Plan Var % — Base Plan Var. as a share of the PRIOR-cycle baseline
# forecast the current-cycle Base leg walked away from.  Denominator is
# ``current_plan_base − base_plan_var`` (i.e. what Base was before the walk).
# Blank when the denominator is zero (undefined baseline).
COL_BASE_PLAN_VAR_PCT: str      = "base_plan_var_pct"
# O% of Current Plan = Current Plan (R&O) ÷ Current Plan (the whole current
# plan: actuals + Base + R&O).  The R&O ("opportunity") share of the plan.
COL_O_PCT: str                  = "o_pct"

# Trailing-window shipment sums (millions of lbs) — additive leaf measures
# that roll up like the rest.  ``*_cur`` = the trailing 3-/6-month actuals
# ending at the Actual window's end month; ``*_py`` = the same months a year
# earlier.  Only the derived YoY ratios below are shown.
COL_T3M_CUR: str                = "t3m_cur"
COL_T3M_PY: str                 = "t3m_py"
COL_T6M_CUR: str                = "t6m_cur"
COL_T6M_PY: str                 = "t6m_py"
# Per-row trailing YoY ratios (derived from the sums above, ratio-of-sums).
COL_T3M_YOY: str                = "t3m_yoy"
COL_T6M_YOY: str                = "t6m_yoy"

# The set of measures summed during subtotal roll-up.  Base Plan is
# intentionally absent — it's a derived residual (see _assemble_table),
# linear in these additive measures, so it rolls up correctly without
# being summed independently.  The trailing-window sums roll up so a
# subtotal's T3M/T6M YoY is a correct ratio-of-sums over its leaves.
_ADDITIVE_COLS: tuple[str, ...] = (
    COL_TOTAL_ACTUALS, COL_PY_ACTUAL,
    COL_PRIOR_MONTH_ACTUAL, COL_PRIOR_MONTH_FORECAST,
    COL_CURRENT_PLAN_ACTUAL, COL_CURRENT_PLAN_BASE, COL_CURRENT_PLAN_RO,
    COL_LAST_PLAN_ACTUALS, COL_LAST_PLAN_FORECAST,
    COL_R_AND_O, COL_BUDGET,
    COL_T3M_CUR, COL_T3M_PY, COL_T6M_CUR, COL_T6M_PY,
)

# Internal structural columns kept alongside the metrics for the page.
COL_LABEL: str       = "Millions of lbs."
COL_ROW_ID: str      = "_row_id"
COL_INDENT: str      = "_indent"
COL_IS_SUBTOTAL: str = "_is_subtotal"
COL_IS_MEMO: str     = "_is_memo"
_META_COLS: tuple[str, ...] = (COL_ROW_ID, COL_INDENT, COL_IS_SUBTOTAL, COL_IS_MEMO)

# Shared display column names for the "not captured" reconciliation logs —
# used by BOTH the Demand MOM Summary log and the Demand Plan Comparison log,
# so a planner reads one schema across the app.  Defined here (early) because
# both consumers live later in the module.
NC_COL_ITEM: str    = "Item"
NC_COL_DESC: str    = "Item Description"
NC_COL_PMAJ: str    = "Portfolio Major"
NC_COL_SFMT: str    = "Supply Format"

# Display order + labels (left → right, mirroring screenshot 1).
DISPLAY_LABELS: dict[str, str] = {
    COL_PRIOR_MONTH_ACTUAL:    "Prior Month Actual",
    COL_PRIOR_MONTH_FORECAST:  "Prior Month Forecast",
    COL_CURRENT_PLAN_ACTUAL:   "Current Plan (Actual)",
    COL_CURRENT_PLAN_BASE:     "Current Plan (Base)",
    COL_CURRENT_PLAN_RO:       "Current Plan (R&O)",
    COL_PY_ACTUAL:             "PY Actual",
    # Total-plan columns explicitly flag that they include the R&O leg
    # (matches the KPI-strip walk tiles' "incl. R&O" wording).
    COL_LAST_PLAN:             "Last Plan (incl. RO)",
    COL_CURRENT_PLAN:          "Current Plan (incl. RO)",
    COL_O_PCT:                 "O% of Current Plan",
    # "…Var." suffix disambiguates variance vs level (a planner can now
    # tell PM Actual — the shipped pounds — from PM Actual Var. — the
    # variance vs the prior-cycle plan for that month).
    COL_PM_ACTUAL:             "PM Actual Var.",
    COL_TOTAL_DELTA:           "Total Delta",
    COL_TOTAL_DELTA_PCT:       "Total Delta %",
    COL_BASE_PLAN:             "Base Plan Var.",
    COL_BASE_PLAN_VAR_PCT:     "Base Plan Var %",
    COL_R_AND_O:               "R&O Var.",
    COL_V_BUDGET:              "v. Budget",
    COL_PCT:                   "%",
    COL_BUDGET:                "Budget",
    # Trailing YoY ratios — surfaced only in the lightweight summary table
    # (deliberately NOT in DISPLAY_ORDER, so the detailed table is unchanged).
    COL_T3M_YOY:               "T3M YoY",
    COL_T6M_YOY:               "T6M YoY",
}
# Left → right order of the metric columns in the rendered table.  The two
# Current Plan legs (Base / R&O) sit where the single forecast column used to;
# O% follows Current Plan (it is R&O ÷ Current Plan); PY Actual sits with the
# actual-side measures.
DISPLAY_ORDER: tuple[str, ...] = (
    COL_PRIOR_MONTH_ACTUAL, COL_PRIOR_MONTH_FORECAST,
    COL_CURRENT_PLAN_ACTUAL, COL_CURRENT_PLAN_BASE, COL_CURRENT_PLAN_RO,
    COL_PY_ACTUAL, COL_LAST_PLAN, COL_CURRENT_PLAN, COL_O_PCT,
    # Delta-breakdown legs (PM Actual · Base Plan · R&O) come BEFORE the
    # Total Delta they sum into, so the walk reads left→right into the total.
    COL_PM_ACTUAL, COL_BASE_PLAN, COL_BASE_PLAN_VAR_PCT, COL_R_AND_O,
    COL_TOTAL_DELTA, COL_TOTAL_DELTA_PCT,
    COL_V_BUDGET, COL_PCT, COL_BUDGET,
)
# Columns rendered as percentages (the rest are millions of lbs).
PERCENT_COLS: frozenset = frozenset({
    COL_TOTAL_DELTA_PCT, COL_PCT, COL_O_PCT, COL_BASE_PLAN_VAR_PCT,
    COL_T3M_YOY, COL_T6M_YOY,
})

# Metric columns hidden by default in the Streamlit table — planners can
# expand them via a checkbox on the page.  The default-visible set matches the
# executive layout (Prior Plan · Current-vs-Prior Delta breakdown · Current
# Plan · Total Delta % · v. Budget · % · Budget); everything else — the raw
# prior-month + current-actual legs, the Current Plan Base/R&O split, PY Actual,
# O% and Base Plan Var % — starts hidden.
COLS_HIDDEN_BY_DEFAULT: tuple[str, ...] = (
    COL_PRIOR_MONTH_ACTUAL,
    COL_PRIOR_MONTH_FORECAST,
    COL_CURRENT_PLAN_ACTUAL,
    COL_CURRENT_PLAN_BASE,
    COL_CURRENT_PLAN_RO,
    COL_PY_ACTUAL,
    COL_O_PCT,
    COL_BASE_PLAN_VAR_PCT,
)

_LBS_PER_MILLION: float = 1_000_000.0

# Millions-of-lbs display precision for every metric column in this table
# (and the Prior Month Actual vs Fcst companion table).
_MILLIONS_DISPLAY_DECIMALS: int = 2

# Prior-Month summary column labels (display contract).
PMAF_COL_PRIOR_PLAN: str = "Prior Plan"
PMAF_COL_ORDERED: str = "Ordered"
PMAF_COL_SHIPPED: str = "Shipped"
PMAF_COL_ORDERED_DIFF: str = "Ordered Diff."
PMAF_COL_SHIPPED_DIFF: str = "Shipped Diff."
PMAF_COL_ORDERED_PCT: str = "Ordered%"
PMAF_COL_SHIPPED_PCT: str = "Shipped%"


# ─────────────────────────────────────────────────────────────────────────────
# Filters
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ComparisonFilters:
    """User selection driving the comparison.

    Attributes
    ----------
    current_cycle / prior_cycle
        Cycle labels from the tracker's ``Cycle`` column — whichever the
        planner selects as the current vs. the prior cycle (defaults anchor
        on cycle order: newest = current, the one before it = prior).
        Prior Month Forecast is summed from ``prior_cycle``.
    actual_start / actual_end
        Inclusive month bounds (first-of-month dates) for the **actual**
        window (IBP shipments + current-cycle "actual" plan).
    forecast_start / forecast_end
        Inclusive month bounds for the **forecast** window.  Must not
        overlap the actual window.
    prior_month
        The single month treated as "Prior Month" for the PM Actual /
        Prior Month Forecast columns.
    pmaj_filter / sfmt_filter / brand_filter
        Optional whitelists of Portfolio Major / Supply Format / Brand
        (``Branded`` / ``Private``) values to include (each AND-ed).  Empty =
        include everything.  Kept for programmatic callers / tests; the page
        UI now drives ``combo_filter`` instead.
    combo_filter
        Optional whitelist of exact ``(portfolio_major, supply_format, brand)``
        tuples to INCLUDE.  Kept for programmatic callers; the UI now drives
        ``combo_exclude`` instead.
    combo_exclude
        Optional set of exact ``(portfolio_major, supply_format, brand)`` tuples
        to DROP (everything else shown).  This is the search-to-hide filter —
        e.g. add ``Butter · … · Private`` combos to remove them while keeping
        ``Butter · … · Branded``.  Matched on the enriched ``pmaj`` / ``sfmt`` /
        ``brand`` columns; empty = no filter.
    """
    current_cycle: str
    prior_cycle: str
    actual_start: date
    actual_end: date
    forecast_start: date
    forecast_end: date
    prior_month: date
    pmaj_filter: frozenset = frozenset()
    sfmt_filter: frozenset = frozenset()
    brand_filter: frozenset = frozenset()
    combo_filter: frozenset = frozenset()
    combo_exclude: frozenset = frozenset()


@dataclass(frozen=True)
class ComparisonResult:
    """Output of :func:`build_demand_plan_comparison`.

    Attributes
    ----------
    table
        Display-ready DataFrame: indented ``Millions of lbs.`` label
        column + the 15 metric columns (display labels), one row per
        template entry, in template order.
    warnings
        Non-fatal advisories surfaced to the planner (e.g. RO Summary
        Report unavailable → R&O is zero).
    ro_summary_available
        ``True`` when the RO Summary Report was read and contributed
        the R&O column.
    """
    table: pd.DataFrame
    warnings: tuple[str, ...] = ()
    ro_summary_available: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Filter discovery + validation (consumed by the page widgets)
# ─────────────────────────────────────────────────────────────────────────────

def list_tracker_cycles(tracker_df: pd.DataFrame) -> list[str]:
    """Return the distinct, sorted ``Cycle`` values in the tracker.

    Empty / NaN cycles are dropped.  Sorting is lexicographic, which
    keeps ``C1, C2, C3 …`` in natural order for the typical labelling.
    """
    if tracker_df is None or TRK_CYCLE not in tracker_df.columns:
        return []
    cycles = (
        tracker_df[TRK_CYCLE]
        .dropna().astype(str).str.strip()
    )
    return sorted({c for c in cycles if c})


def list_tracker_months(tracker_df: pd.DataFrame) -> list[date]:
    """Return the distinct, sorted first-of-month dates in the tracker.

    Uses the vectorised coercion path so this scales cleanly on the
    full 356k-row tracker (used to be the slowest filter-discovery call
    on a cold render).
    """
    if tracker_df is None or TRK_START_OF_MONTH not in tracker_df.columns:
        return []
    months = _vectorised_start_of_month(tracker_df[TRK_START_OF_MONTH])
    return sorted({m for m in months.tolist() if m is not None})


def validate_filters(filters: ComparisonFilters) -> list[str]:
    """Return a list of human-readable validation errors (empty = OK).

    Enforces the planner's hard rule that the **actual** and
    **forecast** windows must not overlap, plus basic start ≤ end
    sanity on each window.  Returned strings are safe to surface
    directly in a Streamlit error / warning banner.
    """
    errors: list[str] = []

    if filters.current_cycle == filters.prior_cycle:
        errors.append(
            "Current cycle and prior cycle must be different — pick two "
            "distinct cycles to compare."
        )
    if filters.actual_start > filters.actual_end:
        errors.append("Actual range: the beginning month is after the end month.")
    if filters.forecast_start > filters.forecast_end:
        errors.append("Forecast range: the beginning month is after the end month.")

    # Overlap check — two inclusive intervals overlap iff each starts on
    # or before the other ends.
    overlap = (
        filters.actual_start <= filters.forecast_end
        and filters.forecast_start <= filters.actual_end
    )
    if overlap:
        errors.append(
            "Actual and forecast month ranges overlap.  Per the planner's "
            "rule they must be disjoint — adjust one of the ranges."
        )
    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Enrichment — PDH join + dimension/brand attachment
# ─────────────────────────────────────────────────────────────────────────────

def build_item_dim_frame(pdh_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Return a vectorised PDH lookup as a DataFrame.

    Columns: ``__item_key, pmaj, sfmt, pminor, brand, desc``.  The
    ``__item_key`` column is the canonical join key produced by
    :func:`_vectorised_item_key` so it matches the same coercion used on
    the tracker / IBP sides.  Last row wins on duplicate items.

    Replacing the ``iterrows`` lookup with a vectorised frame turns the
    PDH preparation from O(n) Python calls into a handful of pandas
    column operations — the hottest part of the cold-build budget per
    the diagnose report.
    """
    if pdh_df is None or pdh_df.empty:
        return pd.DataFrame(columns=["__item_key", "pmaj", "sfmt", "pminor", "brand", "desc"])

    item_col = _resolve_column(pdh_df, _PDH_ITEM_CANDIDATES)
    if not item_col:
        return pd.DataFrame(columns=["__item_key", "pmaj", "sfmt", "pminor", "brand", "desc"])
    desc_col = _resolve_column(pdh_df, _PDH_DESC_CANDIDATES)
    pmaj_col = _resolve_column(pdh_df, _PDH_PMAJ_CANDIDATES)
    pminor_col = _resolve_column(pdh_df, _PDH_PMINOR_CANDIDATES)
    sfmt_col = _resolve_column(pdh_df, _PDH_SFMT_CANDIDATES)

    n = len(pdh_df)
    blank = pd.Series([""] * n, index=pdh_df.index, dtype="object")
    desc_series = _vectorised_clean_str(pdh_df[desc_col]) if desc_col else blank
    out = pd.DataFrame({
        "__item_key": _vectorised_item_key(pdh_df[item_col]),
        "pmaj": _vectorised_clean_str(pdh_df[pmaj_col]) if pmaj_col else blank,
        "sfmt": _vectorised_clean_str(pdh_df[sfmt_col]) if sfmt_col else blank,
        "pminor": _vectorised_clean_str(pdh_df[pminor_col]) if pminor_col else blank,
        "brand": _vectorised_brand(desc_series) if desc_col else pd.Series(
            [BRAND_PRIVATE] * n, index=pdh_df.index, dtype="object"),
        "desc": desc_series,
    })
    # Drop rows with no item key (would never match anything), then
    # collapse duplicates keeping the last row (legacy contract).
    out = out.loc[out["__item_key"] != ""]
    return out.drop_duplicates(subset="__item_key", keep="last").reset_index(drop=True)


# Dimension fields recovered from the fallback tier (the dim frame's columns
# minus the join key).  Named once so the cascade merge and any future
# consumer agree on exactly which fields cascade.
_DIM_CASCADE_FIELDS: tuple[str, ...] = ("pmaj", "sfmt", "pminor", "brand", "desc")


def build_item_dim_frame_cascade(
    pdh_df: Optional[pd.DataFrame],
    item_master_df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Return a per-item dim frame with a PDH → RO_Item_Master cascade.

    Builds the vectorised dim frame from **both** ``qry_pdh.csv`` (primary)
    and ``RO_Item_Master.csv`` (fallback) via :func:`build_item_dim_frame`
    — whose candidate column lists already resolve either schema — then
    coalesces them **per field**: PDH wins whenever it carries a non-blank
    value, and RO_Item_Master fills the gaps (both a wholly-missing item and
    an item PDH knows but left a dimension blank).  This is the Demand-MOM
    analogue of the RO Comparison's ``dp_dimitems → RO_Item_Master`` cascade
    and the classic pivot's Supply-Format fallback, extended to Portfolio
    Major so items absent from PDH stop collapsing into the ``(blank)``
    bucket when RO_Item_Master can classify them.

    Columns: ``__item_key, pmaj, sfmt, pminor, brand, desc``.  A missing /
    empty tier returns the other unchanged, so this degrades gracefully to
    today's PDH-only behaviour when RO_Item_Master is unavailable.
    """
    primary = build_item_dim_frame(pdh_df)
    fallback = build_item_dim_frame(item_master_df)
    if fallback.empty:
        return primary
    if primary.empty:
        return fallback

    # Outer-join on the shared key so items in either tier survive; the
    # fallback columns arrive under a ``_fb`` suffix for the coalesce.
    merged = primary.merge(
        fallback, on="__item_key", how="outer", suffixes=("", "_fb"),
    )
    for col in _DIM_CASCADE_FIELDS:
        primary_vals = merged[col].astype("string").fillna("").str.strip()
        fallback_vals = merged[f"{col}_fb"].astype("string").fillna("").str.strip()
        # PDH value when present, else the RO_Item_Master value.
        merged[col] = primary_vals.where(primary_vals != "", fallback_vals)

    return merged[["__item_key", *_DIM_CASCADE_FIELDS]].reset_index(drop=True)


def build_item_dim_frame_from_tracker(tracker_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Return the per-item dim frame read STRAIGHT off the tracker file.

    The Demand Plan pipeline now writes Portfolio Major / Supply Format /
    Portfolio Minor onto ``qry_mgmt_plan_history_tracker.csv``, so the
    comparison can categorise from the file itself — no PDH / RO_Item_Master
    join.  Brand is derived from the Item Description (``_vectorised_brand``),
    identical to the PDH path, so the leaf Brand splits are unchanged.

    Returns the same ``__item_key, pmaj, sfmt, pminor, brand, desc`` shape as
    :func:`build_item_dim_frame`.  Returns an EMPTY frame when the tracker
    carries NONE of the dim columns (a legacy file that predates them) — the
    caller then falls back to the PDH → RO_Item_Master cascade.
    """
    empty = pd.DataFrame(columns=["__item_key", "pmaj", "sfmt", "pminor", "brand", "desc"])
    if tracker_df is None or tracker_df.empty or TRK_ITEM not in tracker_df.columns:
        return empty
    if not any(c in tracker_df.columns for c in _TRK_DIM_COLS):
        return empty  # unmigrated tracker → signal fallback

    n = len(tracker_df)
    blank = pd.Series([""] * n, index=tracker_df.index, dtype="object")

    def _col(name: str) -> pd.Series:
        return _vectorised_clean_str(tracker_df[name]) if name in tracker_df.columns else blank

    desc = _col(TRK_ITEM_DESCRIPTION)
    out = pd.DataFrame({
        "__item_key": _vectorised_item_key(tracker_df[TRK_ITEM]),
        "pmaj":   _col(TRK_PMAJ),
        "sfmt":   _col(TRK_SFMT),
        "pminor": _col(TRK_PMINOR),
        "brand":  _vectorised_brand(desc),
        "desc":   desc,
    })
    out = out.loc[out["__item_key"] != ""]
    # One row per item; last non-empty dims win (the pipeline writes them
    # consistently per item, so any row is representative).
    return out.drop_duplicates(subset="__item_key", keep="last").reset_index(drop=True)


def _apply_dim_filter(df: pd.DataFrame, filters: "ComparisonFilters") -> pd.DataFrame:
    """Narrow an enriched frame to the selected Portfolio Major / Supply Format.

    Empty whitelists = keep everything (default).  Operates on the enriched
    ``pmaj`` / ``sfmt`` / ``brand`` columns, so it works identically for
    tracker + actuals.  ``combo_filter`` (exact PMaj·SFmt·Brand tuples) is
    AND-ed with the independent whitelists.
    """
    if df is None or df.empty or not (
        filters.pmaj_filter or filters.sfmt_filter or filters.brand_filter
        or filters.combo_filter or filters.combo_exclude
    ):
        return df
    mask = pd.Series(True, index=df.index)
    if filters.pmaj_filter and "pmaj" in df.columns:
        mask &= df["pmaj"].isin(filters.pmaj_filter)
    if filters.sfmt_filter and "sfmt" in df.columns:
        mask &= df["sfmt"].isin(filters.sfmt_filter)
    if filters.brand_filter and "brand" in df.columns:
        mask &= df["brand"].astype(str).str.strip().isin(filters.brand_filter)
    if (filters.combo_filter or filters.combo_exclude) and \
            {"pmaj", "sfmt", "brand"}.issubset(df.columns):
        combo = (
            df["pmaj"].astype(str).str.strip() + "␟"
            + df["sfmt"].astype(str).str.strip() + "␟"
            + df["brand"].astype(str).str.strip()
        )
        if filters.combo_filter:      # include whitelist (programmatic)
            wanted = {f"{p}␟{s}␟{b}" for p, s, b in filters.combo_filter}
            mask &= combo.isin(wanted)
        if filters.combo_exclude:     # search-to-hide exclude list (UI)
            drop = {f"{p}␟{s}␟{b}" for p, s, b in filters.combo_exclude}
            mask &= ~combo.isin(drop)
    return df.loc[mask]


def list_tracker_dim_values(
    tracker_df: pd.DataFrame,
) -> tuple[list[str], list[str]]:
    """Return ``(portfolio_majors, supply_formats)`` for the filter widgets.

    Reads the RAW tracker's ``Portfolio Major`` / ``Supply Format`` columns
    (present since the pipeline now writes them), so the page can populate the
    multiselects BEFORE the enrichment pass runs.  Sorted distinct non-blank
    values; empty lists for a legacy tracker that lacks the columns.
    """
    def _distinct(col: str) -> list[str]:
        if tracker_df is None or tracker_df.empty or col not in tracker_df.columns:
            return []
        vals = tracker_df[col].astype("string").str.strip()
        return sorted({v for v in vals.dropna() if v})
    return _distinct(TRK_PMAJ), _distinct(TRK_SFMT)


# ── Catalog (PDH / RO_Item_Master) dim helpers ──────────────────────────────
# The plan/tracker only carries the Supply Format × Brand combinations that
# have demand planned against them.  These helpers read the fuller ITEM CATALOG
# so the Butter breakdown + the filters can also surface combinations that
# exist as items but have no plan yet (e.g. Private butter, Chips, Elgin Solid).

def butter_catalog_combos(
    pdh_df: Optional[pd.DataFrame],
    item_master_df: Optional[pd.DataFrame] = None,
) -> tuple[tuple[str, str], ...]:
    """Distinct ``(brand, supply_format)`` for **Packaged Butter** in the catalog.

    Union of PDH (``qry_pdh.csv``) + RO_Item_Master; brand is derived from the
    Item Description (``DG`` prefix → Branded).  Used to seed the Butter detail
    with every catalogued format, even those with zero plan.
    """
    butter_names = {s.casefold() for s in _BUTTER}
    pminor = _BUTTER_PMINOR.casefold()
    combos: set[tuple[str, str]] = set()
    for df, desc_c in ((pdh_df, "Item Description"), (item_master_df, "Item Desc")):
        if df is None or df.empty:
            continue
        if not {desc_c, "Portfolio Major", "Portfolio Minor", "Supply Format"}.issubset(df.columns):
            continue
        mask = (
            df["Portfolio Major"].astype(str).str.strip().str.casefold().isin(butter_names)
            & (df["Portfolio Minor"].astype(str).str.strip().str.casefold() == pminor)
        )
        sub = df.loc[mask]
        if sub.empty:
            continue
        for brand, sfmt in zip(_vectorised_brand(sub[desc_c]),
                               sub["Supply Format"].astype(str).str.strip()):
            if sfmt:
                combos.add((str(brand), sfmt))
    return tuple(sorted(combos))


# Portfolio Majors that belong in the B2C comparison filter — everything the
# template rolls up (ESL / Fresh Milk(=HTST) / Cultured / Butter).  Powders,
# Cheese, Bulk Fluid & other non-B2C catalog majors are deliberately excluded.
_FILTER_ALLOWED_PMAJ: frozenset[str] = frozenset({"esl", "htst", "cultured", "butter"})


def _combo_key(pmaj: str, sfmt: str, brand: str) -> tuple[str, str, str]:
    """Loose dedup key so e.g. 'Elgin Quarter' folds into 'Elgin Quarters'."""
    return (pmaj.casefold(), sfmt.strip().casefold().rstrip("s"), brand)


def list_comparison_combos(
    tracker_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame] = None,
    item_master_df: Optional[pd.DataFrame] = None,
) -> list[tuple[str, str, str]]:
    """Return the ``(portfolio_major, supply_format, brand)`` combos for the filter.

    Union of the tracker's planned combos (restricted to the B2C majors —
    ESL / HTST / Cultured / Butter, so Powders / Cheese / Bulk Fluid never
    appear) and the Butter item catalog (so Private / Chips / Elgin Solid are
    selectable even with no plan).  Deduped loosely, tracker names winning.
    """
    out: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    if (tracker_df is not None and not tracker_df.empty
            and {"Portfolio Major", "Supply Format", "Item Description"}.issubset(tracker_df.columns)):
        t = tracker_df[["Portfolio Major", "Supply Format", "Item Description"]].copy()
        t["pmaj"] = t["Portfolio Major"].astype(str).str.strip()
        t["sfmt"] = t["Supply Format"].astype(str).str.strip()
        t = t[t["pmaj"].str.casefold().isin(_FILTER_ALLOWED_PMAJ)
              & (t["sfmt"] != "")].drop_duplicates(["pmaj", "sfmt", "Item Description"])
        t["brand"] = _vectorised_brand(t["Item Description"])
        for pmaj, sfmt, brand in zip(t["pmaj"], t["sfmt"], t["brand"]):
            out.setdefault(_combo_key(pmaj, sfmt, str(brand)), (pmaj, sfmt, str(brand)))
    # Butter catalog (adds Private / Chips / Elgin Solid …); tracker wins on clash.
    for brand, sfmt in butter_catalog_combos(pdh_df, item_master_df):
        out.setdefault(_combo_key("Butter", sfmt, brand), ("Butter", sfmt, brand))
    return sorted(out.values())


# ── Vectorised primitives (used by the enrichment helpers) ──────────────────
#
# These are pure functions over ``pd.Series`` — no per-row Python calls,
# no ``iterrows``.  Keep them tiny + composable so the enrichment paths
# stay readable: each helper does ONE coercion.

def _vectorised_clean_str(series: pd.Series) -> pd.Series:
    """Vectorised analogue of :func:`_clean_str` (trim + None/NaN → '')."""
    s = series.astype("string").str.strip()
    return s.fillna("").astype("object")


def _vectorised_item_key(series: pd.Series) -> pd.Series:
    """Vectorised analogue of :func:`_normalise_item_key`.

    Mirrors the contract: strip → drop trailing ``.0`` → blank for
    NaN/None.  Implemented entirely with pandas string ops so 356k-row
    tracker columns coerce in milliseconds instead of seconds.
    """
    s = series.astype("string").str.strip()
    # Drop a trailing ``.0`` ONLY when the value is an int-like float
    # literal (matches ``370072.0`` → ``370072`` but leaves ``"P-37.0"``
    # alone).  Regex anchors so we don't strip mid-string dots.
    s = s.str.replace(r"^(-?\d+)\.0+$", r"\1", regex=True)
    return s.fillna("").astype("object")


def _vectorised_brand(desc_series: pd.Series) -> pd.Series:
    """Brand from a PDH item description: first 2 chars ``DG`` → Branded, else
    Private (the planner rule; blank/missing descriptions fall to Private)."""
    s = desc_series.astype("string").str.strip()
    is_branded = s.str[:2].str.upper() == "DG"
    return is_branded.map({True: BRAND_BRANDED, False: BRAND_PRIVATE}).astype("object")


# Origin for the Excel/Lotus day-serial fast path — the source CSVs store
# ``Start of Month`` as an integer day count from this epoch.
_SERIAL_DAY_ORIGIN = pd.Timestamp("1899-12-30")

# Inclusive day-serial window that pandas can represent as ns-resolution
# Timestamps.  Derived once from pandas' own Timestamp limits (via plain
# ``date`` components so the subtraction can't overflow pandas' Timedelta,
# and to dodge the nanosecond-discard warning), with a 1-day buffer so the
# partially-representable boundary days are excluded.
#
# Why clamp to this window BEFORE converting: ``pd.to_datetime(unit="D")``
# scales days→nanoseconds inside a ``np.errstate(over="raise")`` block, so a
# contaminated/overflowing month cell (an absurd magnitude or ±inf) overflows
# float64 there and raises ``FloatingPointError`` — which ``errors="coerce"``
# does NOT trap (it only suppresses date-parse failures, not numpy FP errors).
# Nulling out-of-range values first keeps the intended "unparseable → NaT"
# contract instead of crashing the whole column on one bad cell.
_SERIAL_DAY_MIN = (
    date(pd.Timestamp.min.year, pd.Timestamp.min.month, pd.Timestamp.min.day)
    - _SERIAL_DAY_ORIGIN.date()
).days + 1
_SERIAL_DAY_MAX = (
    date(pd.Timestamp.max.year, pd.Timestamp.max.month, pd.Timestamp.max.day)
    - _SERIAL_DAY_ORIGIN.date()
).days - 1


def _vectorised_start_of_month(series: pd.Series) -> pd.Series:
    """Vectorised first-of-month coercion (Excel serials + strings).

    Tries integer-serial parsing first (the source CSV's native shape)
    then falls back to pandas' generic string parser for any non-numeric
    survivors.  Anything that still cannot be parsed becomes ``NaT``.
    The output dtype is ``object`` carrying :class:`datetime.date`
    values, matching :func:`_coerce_start_of_month` so downstream code
    that compares against ``date`` objects keeps working unchanged.
    """
    s = series
    # 0. Already-typed datetime columns (e.g. IBP Shipments "Month" read via
    #    DuckDB, which returns a TIMESTAMP → datetime64) need no serial /
    #    string parsing — floor straight to first-of-month.  This MUST come
    #    first: ``pd.to_numeric(datetime64)`` yields huge nanosecond integers
    #    (NOT NaN), which fall outside the serial window AND dodge the
    #    string fallback below, leaving every row NaT — which empties the
    #    actuals frame downstream (Total Actuals / PM Actual all zero).
    if pd.api.types.is_datetime64_any_dtype(s):
        dt = s.dt.tz_localize(None) if getattr(s.dtype, "tz", None) is not None else s
        floored = dt.dt.to_period("M").dt.to_timestamp()
        return floored.dt.date.where(floored.notna(), other=None)
    # 1. Numeric/serial fast path.  Convert ONLY the in-window finite serials
    #    and hand pandas nothing else: ``between`` yields False for NaN and
    #    ±inf, so the converted subset is guaranteed clean.  This matters
    #    because ``pd.to_datetime(unit="D")`` scales days→nanoseconds via
    #    ``np.round`` under ``np.errstate(over="raise", invalid="raise")``
    #    (pandas ≥2.3) — so an overflowing magnitude trips ``over`` AND a
    #    plain NaN in the array trips ``invalid`` on some numpy builds, both
    #    raising ``FloatingPointError`` which ``errors="coerce"`` does NOT
    #    trap.  Feeding only finite in-range values sidesteps both flags.
    as_num = pd.to_numeric(s, errors="coerce")
    in_window = as_num.between(_SERIAL_DAY_MIN, _SERIAL_DAY_MAX)
    serials_ts = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    if in_window.any():
        serials_ts.loc[in_window] = pd.to_datetime(
            as_num[in_window], unit="D", origin=_SERIAL_DAY_ORIGIN,
            errors="coerce")
    # 2. String fallback — only for cells that aren't numeric at all (genuine
    #    date strings).  A numeric value that merely fell outside the serial
    #    window is a contaminated serial, not a date string: leave it NaT
    #    rather than re-parsing it (which would route an absurd magnitude back
    #    through the same overflow-prone ns-unit cast).
    needs_str = serials_ts.isna() & as_num.isna()
    if needs_str.any():
        str_ts = pd.to_datetime(s[needs_str], errors="coerce")
        serials_ts.loc[needs_str] = str_ts
    # Snap to first-of-month, return as a Series of ``date`` (object).
    out = serials_ts.dt.to_period("M").dt.to_timestamp()
    return out.dt.date.where(out.notna(), other=None)


def _vectorised_forecast_type(series: pd.Series) -> pd.Series:
    """Vectorised forecast-type bucketing (Base Plan / R&O / passthrough)."""
    s = series.astype("string").str.strip()
    folded = s.str.casefold()
    out = s.fillna("").astype("object").copy()
    out[folded == "base plan"] = FORECAST_BASE_PLAN
    out[folded.isin({"r&o", "r and o", "ro", "r & o", "r_and_o"})] = FORECAST_R_AND_O
    return out


def _clean_str(value: object) -> str:
    """Return a trimmed string, mapping NaN/None to ``""``."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _attach_dims(
    base: pd.DataFrame, item_key_series: pd.Series, dim_frame: pd.DataFrame,
) -> pd.DataFrame:
    """Left-join *base* + the four PDH dim columns on a normalised item key.

    Vectorised pandas ``merge`` — replaces the per-row ``map(dims.get)``
    lookup that was the dominant cost on 356k-row tracker enrichment.
    Missing items keep blank dims (and Brand = Private, matching the
    legacy fallback in the deleted ``_enrich_*`` helpers).
    """
    base = base.copy()
    base["__item_key"] = item_key_series.values
    if dim_frame is None or dim_frame.empty:
        for col in ("pmaj", "sfmt", "pminor", "desc"):
            base[col] = ""
        base["brand"] = BRAND_PRIVATE
        return base
    merged = base.merge(
        dim_frame[["__item_key", "pmaj", "sfmt", "pminor", "brand", "desc"]],
        on="__item_key", how="left",
    )
    for col in ("pmaj", "sfmt", "pminor", "desc"):
        merged[col] = merged[col].fillna("")
    merged["brand"] = merged["brand"].fillna(BRAND_PRIVATE)
    return merged


def _enrich_tracker(
    tracker_df: pd.DataFrame, dims_or_frame,
) -> pd.DataFrame:
    """Return a tidy, enriched tracker frame ready for leaf filtering.

    Output columns: ``item_key, item_desc, party_site, month (date),
    pounds (float), forecast_type, cycle, pmaj, sfmt, pminor, brand``.
    Rows with an unparseable month are dropped.  *dims_or_frame* is the
    vectorised dim frame from :func:`build_item_dim_frame`.
    """
    if tracker_df is None or tracker_df.empty:
        return _empty_enriched()

    dim_frame = _coerce_dims_to_frame(dims_or_frame)
    n = len(tracker_df)
    item_keys = _vectorised_item_key(tracker_df[TRK_ITEM])

    pounds = pd.to_numeric(
        tracker_df[TRK_DEMAND_LBS].astype("string").str.replace(",", "", regex=False),
        errors="coerce",
    ).fillna(0.0)

    party_site = (
        _vectorised_clean_str(tracker_df[TRK_PARTY_SITE])
        if TRK_PARTY_SITE in tracker_df.columns
        else pd.Series([""] * n, index=tracker_df.index, dtype="object")
    )
    trk_desc = (
        _vectorised_clean_str(tracker_df[TRK_ITEM_DESCRIPTION])
        if TRK_ITEM_DESCRIPTION in tracker_df.columns
        else pd.Series([""] * n, index=tracker_df.index, dtype="object")
    )

    base = pd.DataFrame({
        "item_key": item_keys.values,
        "party_site": party_site.values,
        "__trk_desc": trk_desc.values,
        "month": _vectorised_start_of_month(tracker_df[TRK_START_OF_MONTH]).values,
        "pounds": pounds.values,
        "forecast_type": _vectorised_forecast_type(tracker_df[TRK_FORECAST_TYPE]).values,
        "cycle": _vectorised_clean_str(tracker_df[TRK_CYCLE]).values,
    })
    enriched = _attach_dims(base, base["item_key"], dim_frame)

    # Item Description: prefer PDH; fall back to tracker's own column.
    pdh_desc = enriched["desc"]
    enriched["item_desc"] = pdh_desc.where(pdh_desc.astype(bool), enriched["__trk_desc"])

    out = enriched[[
        "item_key", "item_desc", "party_site", "month", "pounds",
        "forecast_type", "cycle", "pmaj", "sfmt", "pminor", "brand",
    ]]
    return out.dropna(subset=["month"]).reset_index(drop=True)


def _enrich_ibp(
    ibp_df: pd.DataFrame,
    dims_or_frame,
    *,
    qty_candidates: tuple[str, ...] = _IBP_QTY_CANDIDATES,
) -> pd.DataFrame:
    """Return a tidy, enriched IBP Shipments frame for leaf filtering.

    Output columns: ``item_key, item_desc, customer_no, customer_name,
    month (date), pounds (float), pmaj, sfmt, pminor, brand``.  Source
    column names are auto-detected from the candidate whitelists.
    *dims_or_frame* is the vectorised dim frame from
    :func:`build_item_dim_frame`.
    """
    if ibp_df is None or ibp_df.empty:
        return _empty_enriched(actuals=True)

    item_col = _resolve_column(ibp_df, _IBP_ITEM_CANDIDATES)
    month_col = _resolve_column(ibp_df, _IBP_MONTH_CANDIDATES)
    qty_col = _resolve_column(ibp_df, qty_candidates)
    if not (item_col and month_col and qty_col):
        logger.warning(
            "IBP Shipments missing a required column "
            "(item=%r, month=%r, qty=%r); actuals will be zero.",
            item_col, month_col, qty_col,
        )
        return _empty_enriched(actuals=True)

    cust_no_col = _resolve_column(ibp_df, _IBP_CUSTOMER_NO_CANDIDATES)
    cust_name_col = _resolve_column(ibp_df, _IBP_CUSTOMER_NAME_CANDIDATES)

    dim_frame = _coerce_dims_to_frame(dims_or_frame)
    n = len(ibp_df)
    item_keys = _vectorised_item_key(ibp_df[item_col])
    pounds = pd.to_numeric(
        ibp_df[qty_col].astype("string").str.replace(",", "", regex=False),
        errors="coerce",
    ).fillna(0.0)

    cust_no = (
        _vectorised_item_key(ibp_df[cust_no_col])
        if cust_no_col else pd.Series([""] * n, index=ibp_df.index, dtype="object")
    )
    cust_name = (
        _vectorised_clean_str(ibp_df[cust_name_col])
        if cust_name_col else pd.Series([""] * n, index=ibp_df.index, dtype="object")
    )

    base = pd.DataFrame({
        "item_key": item_keys.values,
        "customer_no": cust_no.values,
        "customer_name": cust_name.values,
        "month": _vectorised_start_of_month(ibp_df[month_col]).values,
        "pounds": pounds.values,
    })
    enriched = _attach_dims(base, base["item_key"], dim_frame)
    enriched["item_desc"] = enriched["desc"]

    out = enriched[[
        "item_key", "item_desc", "customer_no", "customer_name", "month",
        "pounds", "pmaj", "sfmt", "pminor", "brand",
    ]]
    return out.dropna(subset=["month"]).reset_index(drop=True)


def _coerce_dims_to_frame(dims_or_frame) -> pd.DataFrame:
    """Normalise the dim argument to a DataFrame (None → empty schema)."""
    if isinstance(dims_or_frame, pd.DataFrame):
        return dims_or_frame
    return pd.DataFrame(columns=["__item_key", "pmaj", "sfmt", "pminor", "brand", "desc"])


def _empty_enriched(actuals: bool = False) -> pd.DataFrame:
    """Return an empty enriched frame with the right columns."""
    if actuals:
        cols = ["item_key", "item_desc", "customer_no", "customer_name",
                "month", "pounds", "pmaj", "sfmt", "pminor", "brand"]
    else:
        cols = ["item_key", "item_desc", "party_site", "month", "pounds",
                "forecast_type", "cycle", "pmaj", "sfmt", "pminor", "brand"]
    return pd.DataFrame(columns=cols)


# ─────────────────────────────────────────────────────────────────────────────
# Leaf masking
# ─────────────────────────────────────────────────────────────────────────────

def _leaf_mask(df: pd.DataFrame, tpl: TemplateRow) -> pd.Series:
    """Return a boolean mask selecting *df* rows that satisfy *tpl*.

    Each dimension constraint is optional; an absent constraint
    (``None``) imposes no filter.  PMaj matches against the leaf's
    synonym frozenset; SFmt / brand / Portfolio Minor match a single
    string (case-insensitive, trimmed) so upstream casing wobble
    doesn't drop rows.
    """
    if df.empty:
        return pd.Series([], dtype=bool)

    mask = pd.Series(True, index=df.index)
    if tpl.pmaj_match is not None:
        wanted = {s.casefold() for s in tpl.pmaj_match}
        mask &= df["pmaj"].astype(str).str.strip().str.casefold().isin(wanted)
    if tpl.sfmt_match is not None:
        mask &= (
            df["sfmt"].astype(str).str.strip().str.casefold()
            == tpl.sfmt_match.casefold()
        )
    if tpl.brand_match is not None:
        mask &= df["brand"].astype(str).str.strip() == tpl.brand_match
    if tpl.pminor_match is not None:
        mask &= (
            df["pminor"].astype(str).str.strip().str.casefold()
            == tpl.pminor_match.casefold()
        )
    return mask


def _prev_month(d: date) -> date:
    """Return the first day of the calendar month before *d*.

    Used to shift the Last-Plan windows back one month (the
    "one-month-ago snapshot"): its actual window ends one month earlier
    and its prior-cycle forecast window starts one month earlier.
    """
    first = d.replace(day=1)
    if first.month == 1:
        return first.replace(year=first.year - 1, month=12)
    return first.replace(month=first.month - 1)


def _months_in_range(start: date, end: date) -> set[date]:
    """Return the set of first-of-month dates from *start* to *end* inclusive."""
    months: set[date] = set()
    cur = start.replace(day=1)
    end = end.replace(day=1)
    while cur <= end:
        months.add(cur)
        # Advance one month without dateutil.
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return months


def _shift_year_back(d: date) -> date:
    """Return the same month one calendar year earlier (first-of-month)."""
    return date(d.year - 1, d.month, 1)


def _last_n_months(end: date, n: int) -> set[date]:
    """Return the set of *n* first-of-month dates ending at *end* (inclusive)."""
    cur = end.replace(day=1)
    months: set[date] = set()
    for _ in range(max(0, n)):
        months.add(cur)
        cur = _prev_month(cur)
    return months


def _sum_millions(df: pd.DataFrame, mask: pd.Series) -> float:
    """Return Σ pounds (in millions) over *df* rows where *mask* is True."""
    if df.empty or not mask.any():
        return 0.0
    return float(df.loc[mask, "pounds"].sum()) / _LBS_PER_MILLION


# ─────────────────────────────────────────────────────────────────────────────
# RO Summary Report — R&O lookup (FY27 Total Delta, matched by path)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_ro_summary_metric_by_path(
    metric_candidates: tuple[str, ...],
) -> dict[tuple[str, ...], float]:
    """Return ``{label_path -> metric}`` from the saved RO Summary Report.

    Shared parser for any single metric column (Total Delta, Current
    Plan, etc.).  Returns an empty dict when the file is missing /
    unreadable / lacks the expected columns — callers treat that as zero
    and surface a soft warning.  Never raises on a missing file.
    """
    try:
        df, _etag = read_csv(_RO_SUMMARY_SECRETS_SECTION, _RO_SUMMARY_REPORT_BLOB_PATH)
    except LakehouseIOError as exc:
        logger.info("RO Summary Report read failed: %s", exc)
        return {}

    if df is None or df.empty:
        logger.info("RO Summary Report is missing or empty.")
        return {}

    label_col = _resolve_column(df, _RO_SUMMARY_LABEL_CANDIDATES)
    metric_col = _resolve_column(df, metric_candidates)
    if not label_col or not metric_col:
        logger.info(
            "RO Summary Report present but expected columns not found "
            "(label=%r, metric=%r).  Available columns: %r.",
            label_col, metric_col, list(df.columns),
        )
        return {}

    labels = df[label_col].astype("string").fillna("")
    indents = (labels.str.len() - labels.str.lstrip(_NBSP).str.len()) // 2
    cleans = labels.str.replace(_NBSP, "", regex=False).str.strip()
    values = pd.to_numeric(df[metric_col], errors="coerce").fillna(0.0)

    by_path: dict[tuple[str, ...], float] = {}
    stack: list[str] = []
    for label_text, indent, clean, value in zip(
        labels.tolist(), indents.tolist(), cleans.tolist(), values.tolist(),
    ):
        if not label_text or not clean:
            continue
        del stack[int(indent):]
        stack.append(clean)
        by_path[tuple(stack)] = float(value)
    return by_path


def fetch_ro_summary_total_delta_by_path() -> dict[tuple[str, ...], float]:
    """Return ``{label_path -> FY27 Total Delta}`` from the saved RO Summary."""
    return _fetch_ro_summary_metric_by_path(_RO_SUMMARY_TOTAL_DELTA_CANDIDATES)


def months_in_range(start: date, end: date) -> set[date]:
    """Inclusive first-of-month set from *start* through *end*."""
    return _months_in_range(start, end)


def shift_year_back(d: date) -> date:
    """Return the same month one calendar year earlier (first-of-month)."""
    return _shift_year_back(d)


def last_n_months(end: date, n: int) -> set[date]:
    """Return the set of *n* first-of-month dates ending at *end* (inclusive)."""
    return _last_n_months(end, n)


def enrich_ibp_orders_df(
    ibp_orders_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Return tidy IBP Orders with PDH dims attached (pmaj, sfmt, pminor, brand)."""
    dim_frame = build_item_dim_frame(pdh_df)
    if ibp_orders_df is None or ibp_orders_df.empty:
        return _empty_enriched(actuals=True)
    return _enrich_ibp(
        ibp_orders_df, dim_frame, qty_candidates=_IBP_ORDERED_QTY_CANDIDATES,
    )


def resolve_ro_summary_path(
    *,
    pmaj: str,
    sfmt: str,
    brand: str,
    pminor: str = "",
) -> Optional[tuple[str, ...]]:
    """Map PDH dimensions to an RO Summary label path for metric lookup.

    Mirrors the ``ro_summary_path`` wiring on the Demand Plan Comparison
    template so Product Line Review reads the same saved report leaves.
    Returns ``None`` when no RO counterpart exists for the slice.
    """
    pm = pmaj.strip().casefold()
    sf = sfmt.strip()
    br = brand.strip()
    pmin_cf = pminor.strip().casefold()

    if pm in {x.casefold() for x in _BUTTER}:
        if pmin_cf and pmin_cf != _BUTTER_PMINOR.casefold():
            return None
        if not sf:
            return (_RO_TOTAL, "Butter")
        return (_RO_TOTAL, "Butter", sf)

    if pm in {x.casefold() for x in _ESL}:
        if sf.casefold() == "aseptic":
            if br == BRAND_BRANDED:
                return (_RO_TOTAL, _RO_ASEPTIC, _RO_BRANDED)
            if br == BRAND_PRIVATE:
                return (_RO_TOTAL, _RO_ASEPTIC, _RO_PRIVATE)
            return None
        if br == BRAND_BRANDED:
            return (_RO_TOTAL, _RO_ESL, sf, _RO_BRANDED)
        if br == BRAND_PRIVATE:
            return (_RO_TOTAL, _RO_ESL, sf, _RO_PRIVATE)
        return None

    if pm in {x.casefold() for x in _CULTURED}:
        if not sf:
            return None
        return (_RO_TOTAL, _RO_CULTURED, sf)

    if pm in {x.casefold() for x in _FRESH_MILK}:
        if not sf:
            return None
        ro_sfmt = "Totes" if sf.casefold() == "dispenser" else sf
        return (_RO_TOTAL, _RO_FRESH_MILK, ro_sfmt)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Shared enrichment bundle (built ONCE per render, reused by all builders)
# ─────────────────────────────────────────────────────────────────────────────
#
# The comparison + the two driver tables all need the same PDH-joined
# tracker + IBP frames.  Building them three times was the dominant
# per-render cost; this bundle exists so the page can build them once
# and inject the result into all three builders.

@dataclass(frozen=True)
class EnrichedSources:
    """Frozen bundle of pre-enriched frames shared across builders.

    Attributes
    ----------
    tracker
        Output of :func:`_enrich_tracker` (tidy, dims attached).
    ibp
        Output of :func:`_enrich_ibp` (tidy, dims attached).
    ibp_orders
        Output of :func:`_enrich_ibp` over IBP Orders (same tidy shape as
        ``ibp``, but the ``pounds`` column carries *ordered* lbs).
    pdh_warning
        Non-fatal advisory when PDH was empty / unusable; the caller
        surfaces this to the planner.
    """
    tracker: pd.DataFrame
    ibp: pd.DataFrame
    ibp_orders: pd.DataFrame
    pdh_warning: Optional[str] = None
    # Prior-year shipments (same tidy shape as ``ibp``) for the PY Actual
    # column; empty when the caller doesn't request it.
    ibp_py: pd.DataFrame = field(default_factory=lambda: _empty_enriched(actuals=True))
    # Trailing-6-month shipments (current + prior-year) for the T3M/T6M YoY
    # KPI tiles; empty when the caller doesn't request them.
    ibp_recent: pd.DataFrame = field(default_factory=lambda: _empty_enriched(actuals=True))
    ibp_recent_py: pd.DataFrame = field(default_factory=lambda: _empty_enriched(actuals=True))
    # Packaged-Butter (brand, supply_format) combos from the item catalog
    # (PDH + RO_Item_Master) — seeds the Butter detail with every catalogued
    # format, incl. ones with no plan (Private / Chips / Elgin Solid …).
    butter_catalog: tuple[tuple[str, str], ...] = ()


def _build_augmented_dim_frame(
    tracker_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    item_master_df: Optional[pd.DataFrame],
) -> tuple[pd.DataFrame, Optional[str]]:
    """Return ``(item→dim frame, pdh_warning)`` used to classify every source.

    Dims come straight off the tracker (planned items).  For a legacy tracker
    without the dim columns we fall back entirely to the PDH → RO_Item_Master
    cascade.  Otherwise we AUGMENT the tracker map with catalog dims for items
    absent from the tracker — so UNPLANNED SKUs that ship/order (e.g. Butter
    Chips / Elgin Solid / Private) classify into the right leaf instead of
    vanishing into the "not captured" log.  Tracker dims always win on a clash.
    """
    dim_frame = build_item_dim_frame_from_tracker(tracker_df)
    pdh_warning: Optional[str] = None
    if dim_frame.empty:
        dim_frame = build_item_dim_frame_cascade(pdh_df, item_master_df)
        if dim_frame.empty:
            pdh_warning = (
                "The tracker carries no Portfolio Major / Supply Format / "
                "Portfolio Minor columns and neither PDH (qry_pdh.csv) nor "
                "RO_Item_Master.csv could resolve any item — categorisation "
                "is blank, so every row is zero.  Regenerate the Demand Plan "
                "files (they now carry these columns) or check the PDH / "
                "RO_Item_Master exports."
            )
        return dim_frame, pdh_warning
    catalog = build_item_dim_frame_cascade(pdh_df, item_master_df)
    if not catalog.empty:
        missing = catalog[~catalog["__item_key"].isin(dim_frame["__item_key"])]
        if not missing.empty:
            dim_frame = pd.concat([dim_frame, missing], ignore_index=True)
    return dim_frame, pdh_warning


def build_enriched_sources(
    tracker_df: pd.DataFrame,
    ibp_df: Optional[pd.DataFrame],
    ibp_orders_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    *,
    item_master_df: Optional[pd.DataFrame] = None,
    ibp_py_df: Optional[pd.DataFrame] = None,
    ibp_recent_df: Optional[pd.DataFrame] = None,
    ibp_recent_py_df: Optional[pd.DataFrame] = None,
) -> EnrichedSources:
    """Build the shared enrichment bundle exactly once.

    Performs the dimension frame build + vectorised tracker and IBP
    enrichment in a single pass, so all three downstream builders
    (comparison + PM Actual drivers + Base Plan drivers) can reuse the
    output without redoing the work.

    Dimensions come **straight off the tracker file** — the Demand Plan
    pipeline writes Portfolio Major / Supply Format / Portfolio Minor onto
    ``qry_mgmt_plan_history_tracker.csv`` (Brand is derived from the Item
    Description), so no PDH / RO_Item_Master join is needed.  For a LEGACY
    tracker that predates those columns we fall back to the old
    **PDH → RO_Item_Master cascade** so the section keeps working until the
    files are regenerated.  ``pdh_df`` / ``item_master_df`` are therefore only
    consulted on that fallback path.
    """
    dim_frame, pdh_warning = _build_augmented_dim_frame(
        tracker_df, pdh_df, item_master_df)
    trk = _enrich_tracker(tracker_df, dim_frame) if tracker_df is not None else _empty_enriched()
    ibp = _enrich_ibp(ibp_df, dim_frame) if ibp_df is not None else _empty_enriched(actuals=True)
    ibp_orders = (
        _enrich_ibp(ibp_orders_df, dim_frame, qty_candidates=_IBP_ORDERED_QTY_CANDIDATES)
        if ibp_orders_df is not None
        else _empty_enriched(actuals=True)
    )
    # Prior-year + trailing-6-month shipments share the SAME dim frame → an
    # item categorises to the same leaf across every actuals window.
    def _enrich_opt(df: Optional[pd.DataFrame]) -> pd.DataFrame:
        return _enrich_ibp(df, dim_frame) if df is not None else _empty_enriched(actuals=True)

    return EnrichedSources(
        tracker=trk, ibp=ibp, ibp_orders=ibp_orders, pdh_warning=pdh_warning,
        ibp_py=_enrich_opt(ibp_py_df),
        ibp_recent=_enrich_opt(ibp_recent_df),
        ibp_recent_py=_enrich_opt(ibp_recent_py_df),
        butter_catalog=butter_catalog_combos(pdh_df, item_master_df),
    )


# ─────────────────────────────────────────────────────────────────────────────
# FY27 Budget workbook (Fabric xlsx → leaf row_id lookup)
# ─────────────────────────────────────────────────────────────────────────────

# Map each comparison leaf ``row_id`` to the label path used in
# ``FY27_Budget_Demand_Plan_Summary.xlsx`` (screenshot hierarchy).
_BUDGET_LABEL_PATH_BY_ROW_ID: dict[str, tuple[str, ...]] = {
    "esl_lc_branded": ("ESL", "Large Carton", "Branded"),
    "esl_lc_private": ("ESL", "Large Carton", "Private"),
    "esl_sc_branded": ("ESL", "Small Carton", "Branded"),
    "esl_sc_private": ("ESL", "Small Carton", "Private"),
    "esl_aerosol": ("ESL", "Aerosol Can"),
    "asep_branded": ("Aseptic", "Branded"),
    "asep_private": ("Aseptic", "Private"),
    "cult_large_tub": ("Cultured", "Large Tub"),
    "cult_small_tub": ("Cultured", "Small Tub"),
    "cult_pail": ("Cultured", "Pail"),
    "cult_cottage_cheese": ("Cultured", "Cottage Cheese"),
    "cult_sour_cream": ("Cultured", "Sour Cream"),
    "fm_gallon_jug": ("Fresh Milk", "Gallon Jug"),
    "fm_caseless_jug": ("Fresh Milk", "Caseless Jug"),
    "fm_mini_carton": ("Fresh Milk", "Mini Carton"),
    "fm_hg_jug": ("Fresh Milk", "HG Jug"),
    "fm_bossy": ("Fresh Milk", "Bossy"),
    "fm_totes": ("Fresh Milk", "Totes"),
    "fm_tanker": ("Fresh Milk", "Tanker"),
    "butter": ("Butter",),
}

_BUDGET_MAJOR_LABELS: frozenset[str] = frozenset({
    "ESL", "Aseptic", "Cultured", "Fresh Milk", "Butter",
})
_BUDGET_ESL_SFMT_LABELS: frozenset[str] = frozenset({
    "Large Carton", "Small Carton", "Aerosol Can",
})
_BUDGET_CULTURED_CHILD_LABELS: frozenset[str] = frozenset({
    "Large Tub", "Small Tub", "Pail", "Cottage Cheese", "Sour Cream",
})
_BUDGET_FRESH_MILK_SFMT_LABELS: frozenset[str] = frozenset({
    "Gallon Jug", "Caseless Jug", "Mini Carton", "HG Jug",
    "Bossy", "Totes", "Tanker",
})
_BUDGET_BRAND_LABELS: frozenset[str] = frozenset({"Branded", "Private"})
_BUDGET_SKIP_LABELS: frozenset[str] = frozenset({
    "Millions of lbs.", "Millions of lbs", "Budget", "",
})


@dataclass(frozen=True)
class Fy27BudgetLoadResult:
    """Parsed FY27 budget workbook keyed by comparison ``row_id``."""
    by_row_id: dict[str, float]
    warnings: tuple[str, ...] = ()


def fy27_budget_blob_path() -> str:
    """Return the Fabric blob path for the FY27 budget workbook."""
    return _FY27_BUDGET_BLOB_PATH


def _normalise_budget_label(raw: object) -> str:
    """Collapse whitespace in a workbook label cell."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    return " ".join(str(raw).split()).strip()


def _update_budget_context(label: str) -> list[str]:
    """Return the hierarchy path after reading *label* from the workbook."""
    if label.startswith("Total B2C"):
        return [label]
    if label in _BUDGET_MAJOR_LABELS:
        return [label]
    if label in _BUDGET_ESL_SFMT_LABELS:
        return ["ESL", label]
    if label in _BUDGET_CULTURED_CHILD_LABELS:
        return ["Cultured", label]
    if label in _BUDGET_FRESH_MILK_SFMT_LABELS:
        return ["Fresh Milk", label]
    if label in _BUDGET_BRAND_LABELS:
        # Branded / Private appear under ESL (LC/SC) or Aseptic only.
        return []  # caller resolves using prior context
    return []


def parse_fy27_budget_workbook(raw: bytes) -> dict[tuple[str, ...], float]:
    """Parse the FY27 budget xlsx into ``label_path → millions``."""
    frame = pd.read_excel(io.BytesIO(raw), header=None, engine="openpyxl")
    if frame.empty or frame.shape[1] < 2:
        return {}

    values_by_path: dict[tuple[str, ...], float] = {}
    context: list[str] = []

    for _, row in frame.iterrows():
        label = _normalise_budget_label(row.iloc[0])
        if not label or label in _BUDGET_SKIP_LABELS:
            continue
        amount = pd.to_numeric(row.iloc[1], errors="coerce")
        if pd.isna(amount):
            continue

        if label in _BUDGET_BRAND_LABELS:
            if len(context) >= 2 and context[0] == "ESL":
                path = (context[0], context[1], label)
            elif context and context[-1] == "Aseptic":
                path = ("Aseptic", label)
            else:
                continue
        else:
            context = _update_budget_context(label)
            if not context:
                continue
            path = tuple(context)

        values_by_path[path] = float(amount)

    return values_by_path


def budget_by_row_id_from_workbook(
    values_by_path: dict[tuple[str, ...], float],
) -> dict[str, float]:
    """Translate workbook label paths to comparison ``row_id`` budgets."""
    out: dict[str, float] = {}
    for row_id, path in _BUDGET_LABEL_PATH_BY_ROW_ID.items():
        if path in values_by_path:
            out[row_id] = float(values_by_path[path])
    return out


def load_fy27_budget_by_row_id(raw: Optional[bytes]) -> Fy27BudgetLoadResult:
    """Parse raw xlsx bytes into per-leaf budgets (millions of lbs)."""
    if not raw:
        return Fy27BudgetLoadResult(
            {}, ("FY27 budget workbook is empty.",),
        )
    try:
        paths = parse_fy27_budget_workbook(raw)
    except Exception as exc:
        return Fy27BudgetLoadResult(
            {}, (f"Could not parse FY27 budget workbook: {exc}",),
        )
    by_row_id = budget_by_row_id_from_workbook(paths)
    warnings: list[str] = []
    missing = [
        row_id for row_id in _BUDGET_LABEL_PATH_BY_ROW_ID
        if row_id not in by_row_id
    ]
    if missing:
        warnings.append(
            f"FY27 budget workbook: {len(missing)} leaf row(s) had no "
            f"matching label path (Budget will be 0 for those rows)."
        )
    return Fy27BudgetLoadResult(by_row_id=by_row_id, warnings=tuple(warnings))


def fetch_fy27_budget_by_row_id() -> Fy27BudgetLoadResult:
    """Read ``FY27_Budget_Demand_Plan_Summary.xlsx`` from Fabric."""
    try:
        raw, _etag = read_bytes(_RO_SUMMARY_SECRETS_SECTION, _FY27_BUDGET_BLOB_PATH)
    except LakehouseIOError as exc:
        return Fy27BudgetLoadResult(
            {}, (
                f"Could not read `Files/{_FY27_BUDGET_BLOB_PATH}` from "
                f"Microsoft Fabric: {exc}",
            ),
        )
    return load_fy27_budget_by_row_id(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────

def build_demand_plan_comparison(
    tracker_df: Optional[pd.DataFrame],
    ibp_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    filters: ComparisonFilters,
    *,
    item_master_df: Optional[pd.DataFrame] = None,
    ro_total_delta_by_path: Optional[dict[tuple[str, ...], float]] = None,
    enriched: Optional[EnrichedSources] = None,
    budget_by_row_id: Optional[dict[str, float]] = None,
    ibp_py_df: Optional[pd.DataFrame] = None,
) -> ComparisonResult:
    """Build the Demand Plan Comparison Summary table.

    Parameters
    ----------
    tracker_df, ibp_df, pdh_df
        Raw source frames (the page loads these from Fabric).
    filters
        Validated :class:`ComparisonFilters` selection.
    ro_total_delta_by_path
        Optional precomputed RO Summary lookup (see
        :func:`fetch_ro_summary_total_delta_by_path`).  When ``None``
        the builder fetches it itself; pass it in to avoid a duplicate
        Fabric read or to inject a stub in tests.

    Algorithm
    ---------
    1. Build the per-item PDH dimension lookup (PMaj / SFmt / PMinor /
       Brand) and enrich both source frames.
    2. For every template leaf, compute the eight **additive** measures
       (actuals + plan buckets + base-plan delta + R&O + budget).
    3. Roll subtotals up from their non-memo children.
    4. Compute the **derived** columns (Current Plan, PM Actual, Total
       Delta, Last Plan, v. Budget) per row, then the two ratio columns.
    5. Shape into the display frame (indented labels + display column
       order + millions rounding).

    Never raises on missing optional inputs — degrades to zeros and a
    warning so the planner always sees the template shape.

    Parameters
    ----------
    enriched
        Optional pre-built :class:`EnrichedSources`.  Pass this when
        sharing enrichment with the driver-table builders to avoid
        re-PDH-joining the tracker / IBP frames (the hottest step in a
        cold build).  When ``None`` the bundle is built internally.
    """
    artifacts = _build_runtime_artifacts(
        tracker_df, ibp_df, pdh_df, filters,
        item_master_df=item_master_df,
        ro_total_delta_by_path=ro_total_delta_by_path,
        enriched=enriched,
        budget_by_row_id=budget_by_row_id,
        ibp_py_df=ibp_py_df,
    )

    # 4 + 5. Assemble the display frame.
    table = _assemble_table(artifacts.measures, artifacts.template)
    return ComparisonResult(
        table=table,
        warnings=artifacts.warnings,
        ro_summary_available=artifacts.ro_summary_available,
    )


@dataclass(frozen=True)
class _RuntimeBuildArtifacts:
    """Shared intermediate state for comparison-adjacent tables.

    This captures the expensive once-per-selection work (template
    realisation + per-row additive measures) so multiple render targets
    can reuse it without recomputing:

    - Demand Plan Comparison Summary
    - Prior Month Actual vs Fcst summary
    """
    template: tuple[TemplateRow, ...]
    template_by_id: dict[str, TemplateRow]
    measures: dict[str, dict[str, float]]
    warnings: tuple[str, ...]
    ro_summary_available: bool
    enriched: EnrichedSources
    prior_month: date


def _build_runtime_artifacts(
    tracker_df: Optional[pd.DataFrame],
    ibp_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    filters: ComparisonFilters,
    *,
    item_master_df: Optional[pd.DataFrame] = None,
    ro_total_delta_by_path: Optional[dict[tuple[str, ...], float]],
    enriched: Optional[EnrichedSources],
    budget_by_row_id: Optional[dict[str, float]] = None,
    ibp_py_df: Optional[pd.DataFrame] = None,
) -> _RuntimeBuildArtifacts:
    """Build reusable runtime artifacts for comparison-style rollups."""
    warnings: list[str] = []

    if enriched is None:
        enriched = build_enriched_sources(
            tracker_df, ibp_df, None, pdh_df, item_master_df=item_master_df,
            ibp_py_df=ibp_py_df,
        )
    if enriched.pdh_warning:
        warnings.append(enriched.pdh_warning)
    # Portfolio Major / Supply Format filter — narrow every source frame up
    # front so the template roll-up (incl. Total B2C) reflects only the
    # selected slice.  No-op when both whitelists are empty (the default).
    trk = _apply_dim_filter(enriched.tracker, filters)
    ibp = _apply_dim_filter(enriched.ibp, filters)
    ibp_py = _apply_dim_filter(enriched.ibp_py, filters)
    # Trailing-window shipment frames for the per-row T3M/T6M YoY columns.
    # Empty (degrades to blank YoY) when the caller built ``enriched`` without
    # the recent frames, e.g. a standalone/test build.
    ibp_recent = _apply_dim_filter(enriched.ibp_recent, filters)
    ibp_recent_py = _apply_dim_filter(enriched.ibp_recent_py, filters)

    if ro_total_delta_by_path is None:
        ro_total_delta_by_path = fetch_ro_summary_total_delta_by_path()
    ro_available = bool(ro_total_delta_by_path)
    if not ro_available:
        warnings.append(
            "RO Summary Report (RO_Summary_Report.csv) was unavailable, so "
            "the R&O column is zero.  Save the RO Summary Report above to "
            "populate it."
        )

    actual_months = _months_in_range(filters.actual_start, filters.actual_end)
    forecast_months = _months_in_range(filters.forecast_start, filters.forecast_end)
    prior_month = filters.prior_month.replace(day=1)

    # Trailing 3-/6-month YoY windows, anchored on the Actual window's end
    # month (same convention as build_comparison_kpis).  Computed once and
    # reused for every leaf's T3M/T6M shipment sums below.
    _t_end = filters.actual_end.replace(day=1)
    m3_cur = _last_n_months(_t_end, 3)
    m6_cur = _last_n_months(_t_end, 6)
    m3_py = {_shift_year_back(m) for m in m3_cur}
    m6_py = {_shift_year_back(m) for m in m6_cur}

    # Last Plan = the "one-month-ago" snapshot, shifted back one calendar
    # month on both legs (see COL_LAST_PLAN_ACTUALS / COL_LAST_PLAN_FORECAST):
    #   * actuals drop the final realised month  → [actual_start … actual_end − 1]
    #   * the just-closed month reverts to a prior-cycle forecast
    #                                            → [forecast_start − 1 … forecast_end]
    # An empty last-actual window (actual_start == actual_end) yields 0 —
    # _months_in_range returns {} when start > end.
    last_actual_months = _months_in_range(
        filters.actual_start, _prev_month(filters.actual_end))
    prior_forecast_months = _months_in_range(
        _prev_month(filters.forecast_start), filters.forecast_end)

    # Guard: Prior Month Forecast benchmarks against the PRIOR cycle's plan
    # for the prior month.  If that cycle carries no Base/R&O rows for the
    # prior month the forecast is 0 and PM Actual would silently show the
    # full actual as if it were a beat — surface that instead of hiding it.
    if not trk.empty:
        has_prior_forecast = bool((
            (trk["cycle"] == filters.prior_cycle)
            & trk["forecast_type"].isin((FORECAST_BASE_PLAN, FORECAST_R_AND_O))
            & (trk["month"] == prior_month)
        ).any())
        if not has_prior_forecast:
            warnings.append(
                f"Prior cycle {filters.prior_cycle} has no Base/R&O plan rows "
                f"for the prior month ({prior_month:%b %Y}), so Prior Month "
                "Forecast is 0 and PM Actual reflects the full actual.  "
                "Verify the tracker or choose a different prior cycle."
            )

    runtime_template = _build_runtime_template_for_filters(
        trk, ibp, filters,
        actual_months=actual_months,
        forecast_months=forecast_months,
        prior_month=prior_month,
        butter_catalog=enriched.butter_catalog,
    )
    runtime_template_by_id = {row.row_id: row for row in runtime_template}

    measures: dict[str, dict[str, float]] = {}
    for tpl in runtime_template:
        if tpl.is_subtotal:
            continue
        measures[tpl.row_id] = _compute_leaf_measures(
            tpl, trk, ibp, filters,
            actual_months=actual_months,
            forecast_months=forecast_months,
            last_actual_months=last_actual_months,
            prior_forecast_months=prior_forecast_months,
            prior_month=prior_month,
            ro_total_delta_by_path=ro_total_delta_by_path,
            ibp_py=ibp_py,
            ibp_recent=ibp_recent, ibp_recent_py=ibp_recent_py,
            m3_cur=m3_cur, m6_cur=m6_cur, m3_py=m3_py, m6_py=m6_py,
        )
        if budget_by_row_id and tpl.row_id in budget_by_row_id:
            measures[tpl.row_id][COL_BUDGET] = float(
                budget_by_row_id[tpl.row_id],
            )
    for tpl in runtime_template:
        if tpl.is_subtotal:
            measures[tpl.row_id] = _rollup_subtotal(tpl, measures, runtime_template_by_id)

    return _RuntimeBuildArtifacts(
        template=runtime_template,
        template_by_id=runtime_template_by_id,
        measures=measures,
        warnings=tuple(warnings),
        ro_summary_available=ro_available,
        enriched=enriched,
        prior_month=prior_month,
    )


def _compute_leaf_measures(
    tpl: TemplateRow,
    trk: pd.DataFrame,
    ibp: pd.DataFrame,
    filters: ComparisonFilters,
    *,
    actual_months: set[date],
    forecast_months: set[date],
    last_actual_months: set[date],
    prior_forecast_months: set[date],
    prior_month: date,
    ro_total_delta_by_path: dict[tuple[str, ...], float],
    ibp_py: Optional[pd.DataFrame] = None,
    ibp_recent: Optional[pd.DataFrame] = None,
    ibp_recent_py: Optional[pd.DataFrame] = None,
    m3_cur: Optional[set[date]] = None,
    m6_cur: Optional[set[date]] = None,
    m3_py: Optional[set[date]] = None,
    m6_py: Optional[set[date]] = None,
) -> dict[str, float]:
    """Compute the additive measures for a single leaf row.

    All sums are in millions of lbs.  See the module docstring's column
    table for the exact business definition of each measure.

    ``last_actual_months`` / ``prior_forecast_months`` are the
    one-month-shifted Last-Plan windows (actuals minus the final month;
    prior-cycle forecast starting one month earlier).  ``ibp_py`` is the
    prior-year shipments frame (already scoped to the PY window by the
    caller's fetch), summed whole for the PY Actual column.

    ``ibp_recent`` / ``ibp_recent_py`` + the ``m3_*`` / ``m6_*`` month sets
    drive the trailing 3-/6-month shipment sums (this year vs a year ago),
    from which the per-row T3M/T6M YoY ratios are derived after roll-up.
    """
    # Dimension masks (computed once, reused across month/cycle slices).
    trk_mask = _leaf_mask(trk, tpl)
    ibp_mask = _leaf_mask(ibp, tpl)

    # Month membership masks.
    trk_month = trk["month"] if not trk.empty else pd.Series([], dtype=object)
    ibp_month = ibp["month"] if not ibp.empty else pd.Series([], dtype=object)
    trk_in_actual = trk_month.isin(actual_months) if not trk.empty else pd.Series([], dtype=bool)
    trk_in_forecast = trk_month.isin(forecast_months) if not trk.empty else pd.Series([], dtype=bool)
    trk_in_prior = (trk_month == prior_month) if not trk.empty else pd.Series([], dtype=bool)
    trk_in_prior_forecast = (
        trk_month.isin(prior_forecast_months) if not trk.empty else pd.Series([], dtype=bool))
    ibp_in_actual = ibp_month.isin(actual_months) if not ibp.empty else pd.Series([], dtype=bool)
    ibp_in_prior = (ibp_month == prior_month) if not ibp.empty else pd.Series([], dtype=bool)
    ibp_in_last_actual = (
        ibp_month.isin(last_actual_months) if not ibp.empty else pd.Series([], dtype=bool))

    # Cycle + forecast-type masks on the tracker.
    if not trk.empty:
        cur_cycle = trk["cycle"] == filters.current_cycle
        prior_cycle = trk["cycle"] == filters.prior_cycle
        is_base_or_ro = trk["forecast_type"].isin((FORECAST_BASE_PLAN, FORECAST_R_AND_O))
    else:
        cur_cycle = prior_cycle = is_base_or_ro = pd.Series([], dtype=bool)

    # ── Actuals (IBP Shipments — no forecast type) ───────────────────
    total_actuals = _sum_millions(ibp, ibp_mask & ibp_in_actual)
    prior_month_actual = _sum_millions(ibp, ibp_mask & ibp_in_prior)

    # Prior-Year Actual: the whole PY frame is already the shifted-back
    # window, so sum every row that matches this leaf.
    if ibp_py is not None and not ibp_py.empty:
        py_actual = _sum_millions(ibp_py, _leaf_mask(ibp_py, tpl))
    else:
        py_actual = 0.0

    # ── Plan buckets (tracker, Base + R&O) ───────────────────────────
    # Prior Month Forecast benchmarks the prior-month actual against the
    # forecast that was live BEFORE the month closed — i.e. the PRIOR
    # cycle's plan for that month — so PM Actual reads as a genuine
    # forecast-vs-actual variance.  ("Prior" here means the prior cycle,
    # not just the prior month.)  The current cycle's view of the actual
    # window is already captured by Current Plan (Actual) below.
    prior_month_forecast = _sum_millions(
        trk, trk_mask & prior_cycle & is_base_or_ro & trk_in_prior)
    current_plan_actual = _sum_millions(
        trk, trk_mask & cur_cycle & is_base_or_ro & trk_in_actual)
    # Current-cycle forecast SPLIT into its Base Plan and R&O legs (was one
    # "Current Plan (Forecast)" column).  Current Plan = actuals + Base + R&O.
    if not trk.empty:
        is_base = trk["forecast_type"] == FORECAST_BASE_PLAN
        is_ro = trk["forecast_type"] == FORECAST_R_AND_O
    else:
        is_base = is_ro = pd.Series([], dtype=bool)
    current_plan_base = _sum_millions(
        trk, trk_mask & cur_cycle & is_base & trk_in_forecast)
    current_plan_ro = _sum_millions(
        trk, trk_mask & cur_cycle & is_ro & trk_in_forecast)

    # ── Last Plan legs (the one-month-ago snapshot) ──────────────────
    # Actuals over the shifted-back actual window; PRIOR-cycle forecast
    # over the shifted-back forecast window (so the just-closed month is
    # still a prior-cycle forecast here).  Last Plan itself is assembled
    # as the sum of these two in _assemble_table.
    last_plan_actuals = _sum_millions(ibp, ibp_mask & ibp_in_last_actual)
    last_plan_forecast = _sum_millions(
        trk, trk_mask & prior_cycle & is_base_or_ro & trk_in_prior_forecast)

    # ── R&O (RO Summary FY27 Total Delta, matched by label path) ─────
    r_and_o = 0.0
    if tpl.ro_summary_path is not None:
        r_and_o = float(ro_total_delta_by_path.get(tpl.ro_summary_path, 0.0))

    # ── Trailing 3-/6-month shipments (this leaf, this year vs a year ago).
    # Sums (not ratios) so they roll up additively; the YoY ratio is derived
    # per row after roll-up.  Degrades to 0 when the recent frames weren't
    # supplied (e.g. a standalone build).
    t3m_cur, t3m_py, t6m_cur, t6m_py = _leaf_trailing_shipments(
        tpl, ibp_recent, ibp_recent_py, m3_cur, m6_cur, m3_py, m6_py)

    # NOTE: Base Plan is NOT computed here.  Per the planner's spec it is a
    # DERIVED residual — Total Delta − PM Actual − R&O — assembled in
    # _assemble_table so the columns always sum to Total Delta.
    return {
        COL_TOTAL_ACTUALS: total_actuals,
        COL_PY_ACTUAL: py_actual,
        COL_PRIOR_MONTH_ACTUAL: prior_month_actual,
        COL_PRIOR_MONTH_FORECAST: prior_month_forecast,
        COL_CURRENT_PLAN_ACTUAL: current_plan_actual,
        COL_CURRENT_PLAN_BASE: current_plan_base,
        COL_CURRENT_PLAN_RO: current_plan_ro,
        COL_LAST_PLAN_ACTUALS: last_plan_actuals,
        COL_LAST_PLAN_FORECAST: last_plan_forecast,
        COL_R_AND_O: r_and_o,
        COL_BUDGET: tpl.budget_m,
        COL_T3M_CUR: t3m_cur, COL_T3M_PY: t3m_py,
        COL_T6M_CUR: t6m_cur, COL_T6M_PY: t6m_py,
    }


def _leaf_trailing_shipments(
    tpl: TemplateRow,
    ibp_recent: Optional[pd.DataFrame],
    ibp_recent_py: Optional[pd.DataFrame],
    m3_cur: Optional[set[date]],
    m6_cur: Optional[set[date]],
    m3_py: Optional[set[date]],
    m6_py: Optional[set[date]],
) -> tuple[float, float, float, float]:
    """Return ``(t3m_cur, t3m_py, t6m_cur, t6m_py)`` shipment sums for a leaf.

    Each sum applies the leaf's dimension mask to the relevant trailing-window
    frame; missing frames / windows yield 0.0 so the caller degrades cleanly.
    """
    def _sum(df: Optional[pd.DataFrame], months: Optional[set[date]]) -> float:
        if df is None or df.empty or not months:
            return 0.0
        return _sum_millions(df, _leaf_mask(df, tpl) & df["month"].isin(months))

    return (
        _sum(ibp_recent, m3_cur), _sum(ibp_recent_py, m3_py),
        _sum(ibp_recent, m6_cur), _sum(ibp_recent_py, m6_py),
    )


def _rollup_subtotal(
    tpl: TemplateRow,
    measures: dict[str, dict[str, float]],
    template_by_id: dict[str, TemplateRow],
) -> dict[str, float]:
    """Return a subtotal's additive measures = Σ of its children.

    Children are resolved recursively through the id graph, so a
    subtotal-of-subtotals (e.g. ESL → Large Carton → Branded) sums
    correctly.  Memo rows are never declared as children, so they are
    inherently excluded from every subtotal.
    """
    totals = {col: 0.0 for col in _ADDITIVE_COLS}
    for child_id in tpl.children:
        child_tpl = template_by_id[child_id]
        child = (
            _rollup_subtotal(child_tpl, measures, template_by_id)
            if child_tpl.is_subtotal
            else measures[child_id]
        )
        for col in _ADDITIVE_COLS:
            totals[col] += child.get(col, 0.0)
    return totals


def _assemble_table(
    measures: dict[str, dict[str, float]],
    template: tuple[TemplateRow, ...],
) -> pd.DataFrame:
    """Return the display-ready table from per-row additive measures.

    Adds the derived + ratio columns, the indented label, the internal
    metadata columns (for the page's row styling), rounds metric values
    (two decimals for millions, four for ratios), and applies the display
    column order + labels.
    """
    records: list[dict] = []
    for tpl in template:
        m = measures[tpl.row_id]

        # Derived (linear) columns.
        #   Current Plan = actuals over the actual window + current-cycle
        #                  forecast over the forecast window.
        #   Last Plan    = the two independent one-month-ago legs.
        #   Total Delta  = the walk between them (Current − Last).
        #   Base Plan    = the DERIVED residual Total Delta − PM Actual − R&O,
        #                  so Base Plan + PM Actual + R&O ≡ Total Delta.
        #   Current Plan = actuals over the actual window + the current-cycle
        #                  forecast (Base + R&O) over the forecast window.
        current_plan_base = m[COL_CURRENT_PLAN_BASE]
        current_plan_ro = m[COL_CURRENT_PLAN_RO]
        current_plan = m[COL_TOTAL_ACTUALS] + current_plan_base + current_plan_ro
        last_plan = m[COL_LAST_PLAN_ACTUALS] + m[COL_LAST_PLAN_FORECAST]
        pm_actual = m[COL_PRIOR_MONTH_ACTUAL] - m[COL_PRIOR_MONTH_FORECAST]
        total_delta = current_plan - last_plan
        r_and_o = m[COL_R_AND_O]
        base_plan = total_delta - pm_actual - r_and_o
        v_budget = current_plan - m[COL_BUDGET]

        # Ratio columns — guard divide-by-zero (blank when undefined).
        #   Total Delta %       = Total Delta as a share of Last Plan (MoM move).
        #   %                   = v. Budget as a share of Current Plan.
        #   O% of Current Plan  = Current Plan (R&O) as a share of Current Plan.
        #   Base Plan Var %     = Base Plan Var. ÷ prior-cycle baseline
        #                         (= current_plan_base − base_plan_var).  Reads
        #                         as "how much the baseline moved, relative to
        #                         what it was before the walk".
        total_delta_pct = _safe_ratio(total_delta, last_plan)
        pct = _safe_ratio(v_budget, current_plan)
        o_pct = _safe_ratio(current_plan_ro, current_plan)
        base_plan_var_pct = _safe_ratio(base_plan, current_plan_base - base_plan)

        # Trailing YoY (ratio-of-sums over the rolled-up trailing windows).
        # NaN — rendered as "—" — when there's no prior-year base to divide by.
        t3m_py, t6m_py = m.get(COL_T3M_PY, 0.0), m.get(COL_T6M_PY, 0.0)
        t3m_yoy = (_safe_ratio(m.get(COL_T3M_CUR, 0.0) - t3m_py, t3m_py)
                   if abs(t3m_py) > 1e-9 else float("nan"))
        t6m_yoy = (_safe_ratio(m.get(COL_T6M_CUR, 0.0) - t6m_py, t6m_py)
                   if abs(t6m_py) > 1e-9 else float("nan"))

        row = {
            COL_ROW_ID: tpl.row_id,
            COL_INDENT: tpl.indent,
            COL_IS_SUBTOTAL: tpl.is_subtotal,
            COL_IS_MEMO: tpl.is_memo,
            COL_LABEL: _make_indented_label(tpl.label, tpl.indent, tpl.is_memo),
            COL_PRIOR_MONTH_ACTUAL: m[COL_PRIOR_MONTH_ACTUAL],
            COL_PRIOR_MONTH_FORECAST: m[COL_PRIOR_MONTH_FORECAST],
            COL_CURRENT_PLAN_ACTUAL: m[COL_CURRENT_PLAN_ACTUAL],
            COL_CURRENT_PLAN_BASE: current_plan_base,
            COL_CURRENT_PLAN_RO: current_plan_ro,
            COL_PY_ACTUAL: m[COL_PY_ACTUAL],
            COL_LAST_PLAN: last_plan,
            COL_CURRENT_PLAN: current_plan,
            COL_O_PCT: o_pct,
            COL_PM_ACTUAL: pm_actual,
            COL_TOTAL_DELTA: total_delta,
            COL_TOTAL_DELTA_PCT: total_delta_pct,
            COL_BASE_PLAN: base_plan,
            COL_BASE_PLAN_VAR_PCT: base_plan_var_pct,
            COL_R_AND_O: r_and_o,
            COL_V_BUDGET: v_budget,
            COL_PCT: pct,
            COL_BUDGET: m[COL_BUDGET],
            # Trailing YoY (summary-table only; kept out of DISPLAY_ORDER).
            COL_T3M_YOY: t3m_yoy,
            COL_T6M_YOY: t6m_yoy,
        }
        records.append(row)

    df = pd.DataFrame.from_records(records)

    # Round metric columns: 2 dp for millions, 4 dp for ratios (the page
    # formats ratios as percentages — keeping 4 dp preserves e.g. 6.3%).
    for col in DISPLAY_ORDER:
        if col in PERCENT_COLS:
            df[col] = df[col].round(4)
        else:
            df[col] = df[col].round(_MILLIONS_DISPLAY_DECIMALS)
    # The trailing-YoY ratios ride alongside (not in DISPLAY_ORDER).
    for col in (COL_T3M_YOY, COL_T6M_YOY):
        df[col] = df[col].round(4)

    # Final column order: metadata + label + metrics (display order) + the
    # trailing-YoY extras, renamed to the screenshot labels.
    ordered = [*_META_COLS, COL_LABEL, *DISPLAY_ORDER, COL_T3M_YOY, COL_T6M_YOY]
    df = df.loc[:, ordered]
    return df.rename(columns=DISPLAY_LABELS)


# ─────────────────────────────────────────────────────────────────────────────
# "SKUs not captured in the comparison table" reconciliation log
# ─────────────────────────────────────────────────────────────────────────────
#
# The comparison rolls up into a FIXED template (Total B2C → ESL / Cultured /
# Fresh Milk / Butter → …), so a tracker SKU only appears if its dims match one
# of the template leaves.  An item whose (Portfolio Major / Supply Format /
# Brand / Portfolio Minor) — resolved through the PDH → RO_Item_Master cascade
# — matches NO leaf silently drops out of every row.  This log surfaces those
# items, per cycle, so the planner can reconcile the totals.

# Extra display columns (Item / Description / PMaj / SFmt reuse the MOM log's
# NC_COL_* constants so both reconciliation logs speak the same schema).  The
# measure column differs by leg: forecast pounds for the two cycles, shipped
# pounds for the actuals leg.
CNC_COL_FORECAST_M: str = "Forecast (M lbs)"
CNC_COL_SHIPPED_M: str = "Shipped (M lbs)"


def _not_captured_columns(measure_label: str) -> tuple[str, ...]:
    return (NC_COL_ITEM, NC_COL_DESC, NC_COL_PMAJ, NC_COL_SFMT, measure_label)


@dataclass(frozen=True)
class ComparisonNotCaptured:
    """"Not captured in the comparison" reconciliation logs (three legs).

    Each frame lists the SKUs whose dims match no template leaf, so their
    pounds never reach a comparison row:

    * ``prior_cycle`` / ``current_cycle`` — each cycle's FORECAST SKUs
      (measure = :data:`CNC_COL_FORECAST_M`).
    * ``actuals`` — prior-month..actual-window SHIPPED SKUs
      (measure = :data:`CNC_COL_SHIPPED_M`).

    The ``*_label`` fields echo the cycle names / actual window for headers.
    """
    prior_cycle: pd.DataFrame
    current_cycle: pd.DataFrame
    actuals: pd.DataFrame
    prior_cycle_label: str
    current_cycle_label: str
    actual_window_label: str


def _comparison_captured_mask(trk: pd.DataFrame) -> pd.Series:
    """Return a per-row mask: True where the row matches ANY template leaf.

    ORs every non-subtotal leaf's :func:`_leaf_mask` (memo leaves included —
    they still appear in the table).  Subtotals are skipped: their masks are
    unconstrained and would mark every row captured.  Works on any enriched
    frame (tracker or IBP) — both carry the same ``pmaj/sfmt/pminor/brand``.
    """
    if trk is None or trk.empty:
        return pd.Series([], dtype=bool)
    captured = pd.Series(False, index=trk.index)
    for tpl in COMPARISON_TEMPLATE:
        if tpl.is_subtotal:
            continue
        captured |= _leaf_mask(trk, tpl)
    return captured


def _coalesce_from_dim_frame(
    grouped: pd.DataFrame, dim_frame: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Back-fill blank ``item_desc / pmaj / sfmt`` from the PDH → RO cascade.

    An item lands in the not-captured log precisely because its dims did NOT
    match any template leaf — often because the tracker/IBP row carried empty
    Portfolio Major / Supply Format / Description strings (unclassified item,
    or the pipeline hadn't back-filled them yet).  Screenshot 2 shows exactly
    this: every row has an Item but the description / PMaj / SFmt columns are
    blank.

    The PDH → RO_Item_Master cascade frame (built once by the caller via
    :func:`build_item_dim_frame_cascade`) knows the same dimensions the
    template uses to classify, so we prefer values already on the enriched
    row, and fall back to the cascade for anything still blank.  Downstream
    ``_norm_dim`` still renders stubborn blanks as ``(blank)``.

    Called INSIDE :func:`_aggregate_not_captured` so both forecast legs and
    the actual-shipments leg use the same enrichment (no duplication).
    """
    if dim_frame is None or dim_frame.empty or grouped.empty:
        return grouped

    lookup = dim_frame.loc[:, ["__item_key", "desc", "pmaj", "sfmt"]].rename(
        columns={"__item_key": "item_key", "desc": "_fb_desc",
                 "pmaj": "_fb_pmaj", "sfmt": "_fb_sfmt"},
    )
    merged = grouped.merge(lookup, on="item_key", how="left")
    # Prefer the existing (tracker/IBP) value when non-blank; fall back to
    # cascade otherwise.  Treat NaN as blank for the same reason _norm_dim
    # does — the cascade may not carry the item.
    for src, fb in (("item_desc", "_fb_desc"),
                    ("pmaj", "_fb_pmaj"),
                    ("sfmt", "_fb_sfmt")):
        primary = merged[src].astype("string").fillna("").str.strip()
        fallback = merged[fb].astype("string").fillna("").str.strip()
        merged[src] = primary.where(primary != "", fallback)
    return merged.drop(columns=["_fb_desc", "_fb_pmaj", "_fb_sfmt"])


def _aggregate_not_captured(
    df: pd.DataFrame, mask: pd.Series, *, measure_label: str,
    dim_frame: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Aggregate the *mask*-selected rows of *df* into a not-captured table.

    Groups by item, sums pounds → millions under *measure_label*, back-fills
    Item Description / Portfolio Major / Supply Format from the PDH →
    RO_Item_Master cascade (``dim_frame``) when the enriched row is blank,
    and sorts heaviest-first (that's what moves the totals most).  Shared by
    every leg (forecast + actuals) so the shaping — and the fallback — lives
    in one place.
    """
    cols = _not_captured_columns(measure_label)
    empty = pd.DataFrame(columns=list(cols))
    if df is None or df.empty or mask is None or not bool(mask.any()):
        return empty
    sub = df.loc[mask]
    grouped = sub.groupby("item_key", as_index=False).agg(
        item_desc=("item_desc", "first"),
        pmaj=("pmaj", "first"),
        sfmt=("sfmt", "first"),
        pounds=("pounds", "sum"),
    )
    grouped = _coalesce_from_dim_frame(grouped, dim_frame)
    grouped[NC_COL_PMAJ] = grouped["pmaj"].map(_norm_dim)
    grouped[NC_COL_SFMT] = grouped["sfmt"].map(_norm_dim)
    grouped[measure_label] = (grouped["pounds"] / _LBS_PER_MILLION).round(3)
    out = grouped.rename(columns={"item_key": NC_COL_ITEM, "item_desc": NC_COL_DESC})
    out = out.sort_values(measure_label, ascending=False).reset_index(drop=True)
    return out[list(cols)]


def build_comparison_not_captured(
    trk_enriched: pd.DataFrame,
    filters: ComparisonFilters,
    *,
    ibp_enriched: Optional[pd.DataFrame] = None,
    dim_frame: Optional[pd.DataFrame] = None,
) -> ComparisonNotCaptured:
    """Return the not-captured reconciliation logs (prior, current, actuals).

    An item is *not captured* when its dims (Portfolio Major / Supply Format /
    Portfolio Minor / Brand — carried on the tracker, filled from PDH →
    RO_Item_Master) match no template leaf, so its pounds never reach a
    comparison row.  ``dim_frame`` (a PDH → RO_Item_Master cascade, e.g. from
    :func:`build_item_dim_frame_cascade`) is used to back-fill blank Item
    Description / Portfolio Major / Supply Format in the surfaced rows so
    planners see the item's classification even when the tracker/IBP row
    carried empty strings.  ``None`` degrades gracefully to the pre-fallback
    behaviour.  Windows:

      * **current cycle** forecast → ``[forecast_start … forecast_end]``
      * **prior cycle**   forecast → ``[forecast_start − 1 month … forecast_end]``
      * **actual shipments** (``ibp_enriched``) → ``[actual_start … actual_end]``
    """
    captured_trk = _comparison_captured_mask(trk_enriched)

    def _trk_leg(cycle: str, window: set[date]) -> pd.DataFrame:
        if trk_enriched is None or trk_enriched.empty or not window:
            return pd.DataFrame(columns=list(_not_captured_columns(CNC_COL_FORECAST_M)))
        base_or_ro = trk_enriched["forecast_type"].isin(
            (FORECAST_BASE_PLAN, FORECAST_R_AND_O))
        mask = (
            (trk_enriched["cycle"] == cycle)
            & base_or_ro
            & trk_enriched["month"].isin(window)
            & (~captured_trk)
        )
        return _aggregate_not_captured(
            trk_enriched, mask,
            measure_label=CNC_COL_FORECAST_M, dim_frame=dim_frame,
        )

    current_window = _months_in_range(filters.forecast_start, filters.forecast_end)
    prior_window = _months_in_range(
        _prev_month(filters.forecast_start), filters.forecast_end)

    # Actual-shipments leg: shipped SKUs over the actual window that match no leaf.
    actual_window = _months_in_range(filters.actual_start, filters.actual_end)
    actuals = pd.DataFrame(columns=list(_not_captured_columns(CNC_COL_SHIPPED_M)))
    if ibp_enriched is not None and not ibp_enriched.empty and actual_window:
        captured_ibp = _comparison_captured_mask(ibp_enriched)
        amask = ibp_enriched["month"].isin(actual_window) & (~captured_ibp)
        actuals = _aggregate_not_captured(
            ibp_enriched, amask,
            measure_label=CNC_COL_SHIPPED_M, dim_frame=dim_frame,
        )

    return ComparisonNotCaptured(
        prior_cycle=_trk_leg(filters.prior_cycle, prior_window),
        current_cycle=_trk_leg(filters.current_cycle, current_window),
        actuals=actuals,
        prior_cycle_label=filters.prior_cycle,
        current_cycle_label=filters.current_cycle,
        actual_window_label=f"{filters.actual_start:%b %Y} – {filters.actual_end:%b %Y}",
    )


def tracker_has_dim_columns(tracker_df: Optional[pd.DataFrame]) -> bool:
    """True when the tracker already carries all three categorisation dims.

    Drives the *Generate* button's decision to backfill: a tracker missing
    Portfolio Major / Supply Format / Portfolio Minor must be enriched (and
    persisted) before the comparison can categorise straight off the file.
    """
    if tracker_df is None or tracker_df.empty:
        return False
    return all(c in tracker_df.columns for c in (TRK_PMAJ, TRK_SFMT, TRK_PMINOR))


# ─────────────────────────────────────────────────────────────────────────────
# Executive KPI tiles (headline metrics above the comparison table)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ComparisonKpis:
    """Headline metrics for the KPI strip above the Demand Plan Comparison.

    Two logical groupings, both scoped to the **Total B2C** row and honouring
    the section's Portfolio Major / Supply Format filter:

    * **YoY / share ratios** — FRACTIONs (0.03 = +3%) or ``None`` when the
      denominator is missing/zero:
      ``t3m_yoy`` / ``t6m_yoy`` — trailing 3- / 6-month actual shipments vs
      the same months a year ago (anchored on the Actual-window end month);
      ``full_year_base_vs_py`` — (Base plan − PY Actual) / PY Actual, where
      Base plan = Current Plan − R&O (i.e. the plan excluding R&O, vs PY);
      ``ro_pct`` — Current Plan (R&O) / Current Plan (R&O share of plan).

    * **Cycle-over-cycle walk (millions of lbs)** — mirrors the assembled
      table's Total B2C row so the tile and the table cell tie by construction
      (no independent math).  ``None`` when the source cell is missing:
      ``last_plan_total`` / ``current_plan_total`` — total plan (incl. R&O)
      for the prior / current cycle;
      ``pm_actual_var`` — Prior-Month actual − prior-cycle forecast;
      ``base_plan_var`` — Base Plan Var. (residual);
      ``ro_var`` — R&O Var. (the same ``FY27 Probabilized | Total Δ`` cell
      from ``RO_Summary_Report.csv`` the table's ``R&O Var.`` column reads).
    """
    t3m_yoy: Optional[float]
    t6m_yoy: Optional[float]
    full_year_base_vs_py: Optional[float]
    ro_pct: Optional[float]
    # ``budget_pct`` — Total B2C ``%`` cell (Current Plan vs Budget %), shown
    # as the last tile on the YoY/share row.
    budget_pct: Optional[float] = None
    # Walk-row values (M lbs) — read directly off the Total B2C row of the
    # assembled table so tile ↔ table reconciliation is trivial.
    last_plan_total: Optional[float] = None
    current_plan_total: Optional[float] = None
    pm_actual_var: Optional[float] = None
    base_plan_var: Optional[float] = None
    ro_var: Optional[float] = None


def _b2c_shipments_millions(
    enriched_ibp: pd.DataFrame, months: set[date], filters: "ComparisonFilters",
) -> float:
    """Σ (millions) of Total-B2C shipments in *months* — captured leaves only.

    Applies the section's PMaj/SFmt filter first so the KPI matches the
    (possibly filtered) table, then the template capture mask so only B2C
    rows count, then the month window.
    """
    if enriched_ibp is None or enriched_ibp.empty or not months:
        return 0.0
    df = _apply_dim_filter(enriched_ibp, filters)
    if df.empty:
        return 0.0
    mask = _comparison_captured_mask(df) & df["month"].isin(months)
    return _sum_millions(df, mask)


def _cell(row: pd.Series, col_id: str) -> Optional[float]:
    """Return the Total-B2C row's numeric cell for a comparison column id.

    Resolves the display label from :data:`DISPLAY_LABELS` and coerces the
    value to ``float``.  Returns ``None`` for missing / NaN cells so
    downstream tiles render "—" instead of a bogus 0.  Used by the KPI
    builder to lift walk values straight off the assembled table.
    """
    label = DISPLAY_LABELS.get(col_id, col_id)
    val = row.get(label)
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def build_comparison_kpis(
    table: pd.DataFrame,
    ibp_recent: pd.DataFrame,
    ibp_recent_py: pd.DataFrame,
    filters: ComparisonFilters,
) -> ComparisonKpis:
    """Compute the four headline KPIs shown above the comparison table.

    ``table`` is the assembled comparison frame (supplies Current Plan, PY
    Actual and O% at the Total B2C row).  ``ibp_recent`` / ``ibp_recent_py``
    are the enriched trailing-6-month shipments (current + prior year); the
    trailing-3-month figures are the last three of those six months.  The
    windows anchor on ``filters.actual_end``:
    e.g. Actual ends Jun 2026 → T3M = Apr–Jun 2026 vs Apr–Jun 2025.
    """
    # ── Trailing-window YoY (from shipments) ─────────────────────────
    t_end = filters.actual_end.replace(day=1)
    m3_cur = _last_n_months(t_end, 3)
    m6_cur = _last_n_months(t_end, 6)
    m3_py = {_shift_year_back(m) for m in m3_cur}
    m6_py = {_shift_year_back(m) for m in m6_cur}

    t3m_cur = _b2c_shipments_millions(ibp_recent, m3_cur, filters)
    t3m_py = _b2c_shipments_millions(ibp_recent_py, m3_py, filters)
    t6m_cur = _b2c_shipments_millions(ibp_recent, m6_cur, filters)
    t6m_py = _b2c_shipments_millions(ibp_recent_py, m6_py, filters)
    t3m_yoy = _safe_ratio(t3m_cur - t3m_py, t3m_py) if t3m_py else None
    t6m_yoy = _safe_ratio(t6m_cur - t6m_py, t6m_py) if t6m_py else None

    # ── Plan-level KPIs + cycle-walk values (from Total B2C row) ─────
    # Every walk value is the SAME cell rendered in the table's Total B2C
    # row.  Reading them here (rather than recomputing) guarantees the tile
    # and the table always agree — including R&O Var., which stays tied to
    # the RO Summary Report by construction.
    full_year_base_vs_py: Optional[float] = None
    ro_pct: Optional[float] = None
    budget_pct: Optional[float] = None
    last_plan_total: Optional[float] = None
    current_plan_total: Optional[float] = None
    pm_actual_var: Optional[float] = None
    base_plan_var: Optional[float] = None
    ro_var: Optional[float] = None

    if table is not None and not table.empty and COL_ROW_ID in table.columns:
        tot = table.loc[table[COL_ROW_ID] == "total_b2c"]
        if not tot.empty:
            row = tot.iloc[0]
            cur_plan = _cell(row, COL_CURRENT_PLAN)
            py_actual = _cell(row, COL_PY_ACTUAL)
            ro_vol = _cell(row, COL_CURRENT_PLAN_RO)
            # Full-Year "Base vs PY %": Base plan (= Current Plan − R&O) vs PY.
            base_plan = (
                cur_plan - ro_vol
                if cur_plan is not None and ro_vol is not None else None
            )
            full_year_base_vs_py = (
                _safe_ratio(base_plan - py_actual, py_actual) if py_actual else None
            ) if base_plan is not None and py_actual is not None else None
            ro_pct = _cell(row, COL_O_PCT)
            budget_pct = _cell(row, COL_PCT)
            current_plan_total = cur_plan
            last_plan_total = _cell(row, COL_LAST_PLAN)
            pm_actual_var = _cell(row, COL_PM_ACTUAL)
            base_plan_var = _cell(row, COL_BASE_PLAN)
            ro_var = _cell(row, COL_R_AND_O)

    return ComparisonKpis(
        t3m_yoy=t3m_yoy, t6m_yoy=t6m_yoy,
        full_year_base_vs_py=full_year_base_vs_py, ro_pct=ro_pct,
        budget_pct=budget_pct,
        last_plan_total=last_plan_total,
        current_plan_total=current_plan_total,
        pm_actual_var=pm_actual_var,
        base_plan_var=base_plan_var,
        ro_var=ro_var,
    )


def build_prior_month_actual_vs_fcst_table(
    tracker_df: Optional[pd.DataFrame],
    ibp_shipments_df: Optional[pd.DataFrame],
    ibp_orders_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    filters: ComparisonFilters,
    *,
    enriched: Optional[EnrichedSources] = None,
) -> pd.DataFrame:
    """Build the *Prior Month Actual vs Fcst* summary table.

    Column definitions (all in millions of lbs):
    - Prior Plan = Prior Month Forecast (from comparison measures)
    - Ordered = prior-month IBP Orders ordered lbs
    - Shipped = Prior Month Actual (from comparison measures)
    - Ordered Diff. = Ordered - Prior Plan
    - Shipped Diff. = Shipped - Prior Plan
    - Ordered% = Ordered / Prior Plan - 1
    - Shipped% = Shipped / Prior Plan - 1

    Row hierarchy intentionally reuses the SAME runtime template as the
    Demand Plan Comparison Summary (including dynamic Butter detail rows),
    guaranteeing row-name/indent parity.
    """
    if enriched is None:
        enriched = build_enriched_sources(
            tracker_df, ibp_shipments_df, ibp_orders_df, pdh_df,
        )

    # No RO dependency for this table; pass an empty lookup so we can
    # reuse the exact comparison measure pipeline without extra I/O.
    artifacts = _build_runtime_artifacts(
        tracker_df, ibp_shipments_df, pdh_df, filters,
        ro_total_delta_by_path={},
        enriched=enriched,
    )
    ordered_by_row = _compute_prior_month_ordered_measures(
        artifacts.template,
        artifacts.template_by_id,
        artifacts.enriched.ibp_orders,
        artifacts.prior_month,
    )
    return _assemble_prior_month_actual_vs_fcst_table(
        artifacts.template,
        artifacts.measures,
        ordered_by_row,
    )


def _compute_prior_month_ordered_measures(
    template: tuple[TemplateRow, ...],
    template_by_id: dict[str, TemplateRow],
    ibp_orders: pd.DataFrame,
    prior_month: date,
) -> dict[str, float]:
    """Return ``{row_id -> Ordered (M lbs)}`` for prior month."""
    ordered_measures: dict[str, float] = {}
    for tpl in template:
        if tpl.is_subtotal:
            continue
        if ibp_orders is None or ibp_orders.empty:
            ordered_measures[tpl.row_id] = 0.0
            continue
        mask = _leaf_mask(ibp_orders, tpl) & (ibp_orders["month"] == prior_month)
        ordered_measures[tpl.row_id] = _sum_millions(ibp_orders, mask)

    # Subtotals use the same child graph as the comparison template so
    # roll-up semantics stay identical (memo rows excluded by design).
    for tpl in template:
        if not tpl.is_subtotal:
            continue
        ordered_measures[tpl.row_id] = _rollup_subtotal_scalar(
            tpl, ordered_measures, template_by_id,
        )
    return ordered_measures


def _rollup_subtotal_scalar(
    tpl: TemplateRow,
    values_by_row: dict[str, float],
    template_by_id: dict[str, TemplateRow],
) -> float:
    """Return a scalar subtotal via recursive child traversal."""
    subtotal = 0.0
    for child_id in tpl.children:
        child_tpl = template_by_id[child_id]
        if child_tpl.is_subtotal:
            subtotal += _rollup_subtotal_scalar(
                child_tpl, values_by_row, template_by_id,
            )
        else:
            subtotal += float(values_by_row.get(child_id, 0.0))
    return subtotal


def _assemble_prior_month_actual_vs_fcst_table(
    template: tuple[TemplateRow, ...],
    measures: dict[str, dict[str, float]],
    ordered_by_row: dict[str, float],
) -> pd.DataFrame:
    """Assemble the display-ready prior-month summary table."""
    records: list[dict] = []
    for tpl in template:
        m = measures[tpl.row_id]
        prior_plan = float(m[COL_PRIOR_MONTH_FORECAST])
        shipped = float(m[COL_PRIOR_MONTH_ACTUAL])
        ordered = float(ordered_by_row.get(tpl.row_id, 0.0))
        ordered_diff = ordered - prior_plan
        shipped_diff = shipped - prior_plan
        ordered_pct = _safe_ratio(ordered_diff, prior_plan)
        shipped_pct = _safe_ratio(shipped_diff, prior_plan)

        records.append({
            COL_ROW_ID: tpl.row_id,
            COL_INDENT: tpl.indent,
            COL_IS_SUBTOTAL: tpl.is_subtotal,
            COL_IS_MEMO: tpl.is_memo,
            COL_LABEL: _make_indented_label(tpl.label, tpl.indent, tpl.is_memo),
            PMAF_COL_PRIOR_PLAN: prior_plan,
            PMAF_COL_ORDERED: ordered,
            PMAF_COL_SHIPPED: shipped,
            PMAF_COL_ORDERED_DIFF: ordered_diff,
            PMAF_COL_SHIPPED_DIFF: shipped_diff,
            PMAF_COL_ORDERED_PCT: ordered_pct,
            PMAF_COL_SHIPPED_PCT: shipped_pct,
        })

    out = pd.DataFrame.from_records(records)
    value_cols = [
        PMAF_COL_PRIOR_PLAN, PMAF_COL_ORDERED, PMAF_COL_SHIPPED,
        PMAF_COL_ORDERED_DIFF, PMAF_COL_SHIPPED_DIFF,
    ]
    pct_cols = [PMAF_COL_ORDERED_PCT, PMAF_COL_SHIPPED_PCT]
    for c in value_cols:
        out[c] = out[c].round(_MILLIONS_DISPLAY_DECIMALS)
    for c in pct_cols:
        out[c] = out[c].round(4)

    ordered_cols = [
        *_META_COLS,
        COL_LABEL,
        PMAF_COL_PRIOR_PLAN,
        PMAF_COL_ORDERED,
        PMAF_COL_SHIPPED,
        PMAF_COL_ORDERED_DIFF,
        PMAF_COL_SHIPPED_DIFF,
        PMAF_COL_ORDERED_PCT,
        PMAF_COL_SHIPPED_PCT,
    ]
    return out.loc[:, ordered_cols].rename(columns={COL_LABEL: "Millions of lbs."})


# ─────────────────────────────────────────────────────────────────────────────
# Forecast Bias (Lag 1) by Segment × Month
# ─────────────────────────────────────────────────────────────────────────────
#
# Compares each segment's ACTUAL orders (IBP Orders) against the rolling lag-1
# forecast — **Base Plan only** (R&O excluded: this measures the committed base
# plan vs what customers ordered) — for the six months ending at (and
# including) the Prior Month.  Only months a lag-1 cycle actually forecast
# (its horizon overlaps them) are scored; earlier months are blank.  Surfaces
# monthly Bias%, a 6-month average, WMAPE, and Forecast Value Added vs a
# seasonal-naive (same-month-last-year) benchmark.
#
#   Bias%(m)   = (Forecast(m) − Actual(m)) / Actual(m)   (− = under-forecast)
#   6-Mo Avg   = simple mean of the monthly Bias%
#   WMAPE      = Σ|Actual − Forecast| / Σ|Actual|         (volume-weighted; 0 best)
#   FVA vs SN  = WMAPE(seasonal-naive) − WMAPE(forecast)  (pp; + = beats naive)
#   Impact     = Σ|Actual − Forecast| / Total-B2C volume  (= WMAPE × vol share)
# Severity is impact-aware: Priority = WMAPE ≥ floor AND material (Impact ≥
# threshold); Monitor = an accuracy gap on a small business; blank otherwise —
# so a tiny line's large % error can't outrank a large line's costlier one.

BIAS_COL_AVG: str        = "_avg_bias"
BIAS_COL_WMAPE: str      = "_wmape"
BIAS_COL_FVA: str        = "_fva"
BIAS_COL_VOLUME: str     = "_volume"   # segment Σ|Actual| over covered months (M lbs)
BIAS_COL_IMPACT: str     = "_impact"   # segment |error| ÷ Total-B2C volume (share of total miss)
BIAS_COL_FLAG_DIR: str   = "_flag_dir"
BIAS_COL_FLAG_SEV: str   = "_flag_sev"
BIAS_COL_FLAG_FVA: str   = "_flag_fva"

# Flag thresholds.  Severity now blends ACCURACY and BUSINESS SIZE so a tiny
# segment with a big % error can't outrank a large segment's smaller % error:
#   • WMAPE floor  — below it the forecast is accurate enough; never flag.
#   • Impact       — the segment's absolute pound-error as a share of the WHOLE
#                    B2C business (= WMAPE × the segment's volume share).  It is
#                    what a high % error actually COSTS at the total level.
#   Priority = an accuracy gap that is ALSO material (impact ≥ threshold);
#   Monitor  = an accuracy gap that is immaterial to the total (small business).
_BIAS_WMAPE_FLOOR: float    = 0.10   # ≥10% WMAPE = a real accuracy gap worth a flag
_BIAS_IMPACT_PRIORITY: float = 0.01  # segment causes ≥1pp of total-B2C volume error
_BIAS_DIR_BAND: float       = 0.02   # ±2 pp around zero reads as "Balanced"
# FVA best practice (IBF / Gilliland): a forecast should beat the seasonal-naive
# benchmark.  FVA below −0.5 pp means the naive guess was more accurate, so the
# planning effort is destroying value — the actionable "Below naive" flag.
_BIAS_FVA_BAND: float       = 0.005

BIAS_FLAG_PRIORITY: str = "Priority"
BIAS_FLAG_MONITOR: str  = "Monitor"
BIAS_DIR_OVER: str      = "Over"
BIAS_DIR_UNDER: str     = "Under"
BIAS_DIR_BALANCED: str  = "Balanced"
BIAS_FVA_BELOW: str     = "Below naive"   # negative FVA → naive would beat us


@dataclass(frozen=True)
class ForecastBiasResult:
    """Output of :func:`build_forecast_bias_table`.

    ``table`` carries the metadata columns (``_row_id`` / ``_indent`` /
    ``_is_subtotal`` / ``_is_memo`` / ``Segment``), one Bias% column per month
    (keyed ``YYYY-MM``), and the derived ``_avg_bias`` / ``_wmape`` / ``_fva`` /
    flag columns.  ``months`` lists the six month keys oldest→newest.
    """
    table: pd.DataFrame
    months: tuple[str, ...]
    available: bool = False
    # Per-month lag-1 provenance: ``(month_key, cycle, lag, is_fallback)`` —
    # which cycle supplied each month's forecast, its lag, and whether it was a
    # backfill (no cycle forecast that month 1-ahead).
    month_meta: tuple[tuple[str, str, int, bool], ...] = ()


def _add_months(d: date, k: int) -> date:
    """First-of-month *d* shifted by *k* calendar months (k may be negative)."""
    idx = (d.year * 12 + (d.month - 1)) + k
    return date(idx // 12, idx % 12 + 1, 1)


def _month_diff(a: date, b: date) -> int:
    """Whole calendar months from *a* to *b* (b − a)."""
    return (b.year * 12 + b.month) - (a.year * 12 + a.month)


def _months_back(anchor: date, n: int) -> list[date]:
    """``n`` first-of-month dates ending at (and including) *anchor*, oldest→newest."""
    a = anchor.replace(day=1)
    return [_add_months(a, -(n - 1 - i)) for i in range(n)]


def _cycle_horizon_starts(trk: pd.DataFrame) -> dict[str, tuple[date, date]]:
    """Return ``{cycle: (first_month, last_month)}`` over its Base/R&O rows.

    A cycle's ``first_month`` is its horizon start — the month it forecasts
    "1-month-ahead" — used to place it on the rolling lag-1 timeline.
    """
    if trk is None or trk.empty or "cycle" not in trk.columns:
        return {}
    m = trk[trk["forecast_type"].isin((FORECAST_BASE_PLAN, FORECAST_R_AND_O))]
    m = m[m["month"].notna()]
    if m.empty:
        return {}
    agg = m.groupby(m["cycle"].astype(str).str.strip())["month"].agg(["min", "max"])
    return {c: (r["min"], r["max"]) for c, r in agg.iterrows() if c}


def _map_lag1_cycles(
    bias_months: list[date], cyc_range: dict[str, tuple[date, date]],
) -> list[tuple[Optional[str], Optional[int], bool]]:
    """Map each actual month to its lag-1 cycle: ``(cycle, lag, is_fallback)``.

    Rolling lag-1: the forecast for month M comes from the cycle whose horizon
    STARTS at M (the freshest 1-month-ahead view).  Ties on the start month are
    broken by the lexically-smallest cycle label.  When no cycle starts at M
    (a gap month), backfill with the nearest EARLIER cycle that still covers M
    — a longer-lag fallback, flagged so the UI can mark it.  ``(None, None,
    False)`` when even a backfill is impossible (M predates every cycle).
    """
    out: list[tuple[Optional[str], Optional[int], bool]] = []
    for month in bias_months:
        exact = sorted(c for c, (start, _mx) in cyc_range.items() if start == month)
        if exact:
            out.append((exact[0], 1, False))
            continue
        earlier = [(c, s) for c, (s, mx) in cyc_range.items() if s < month <= mx]
        if earlier:
            cyc, start = max(earlier, key=lambda cs: (cs[1], cs[0]))
            out.append((cyc, _month_diff(start, month) + 1, True))
        else:
            out.append((None, None, False))
    return out


def _is_nan(x: object) -> bool:
    """True for None or float NaN (guards the flag inputs uniformly)."""
    return x is None or (isinstance(x, float) and math.isnan(x))


def _bias_direction(avg_bias: float) -> str:
    """Over / Under / Balanced from the 6-month average bias (±2 pp dead-band)."""
    if _is_nan(avg_bias):
        return ""
    if avg_bias > _BIAS_DIR_BAND:
        return BIAS_DIR_OVER
    if avg_bias < -_BIAS_DIR_BAND:
        return BIAS_DIR_UNDER
    return BIAS_DIR_BALANCED


def _bias_severity(wmape: float, impact: float) -> str:
    """Severity that blends accuracy (WMAPE) with business size (impact).

    A forecast that is accurate enough (WMAPE below the floor) never flags,
    however big the segment.  Among segments WITH an accuracy gap, the ones
    whose error is material to the whole business (``impact`` ≥ threshold) are
    **Priority**; the rest — a real % miss but on a small business — are
    **Monitor**.  This stops a tiny line's big percentage from outranking a
    large line's smaller, costlier percentage.
    """
    if _is_nan(wmape) or wmape < _BIAS_WMAPE_FLOOR:
        return ""
    if not _is_nan(impact) and impact >= _BIAS_IMPACT_PRIORITY:
        return BIAS_FLAG_PRIORITY
    return BIAS_FLAG_MONITOR


def _bias_fva_verdict(fva: float) -> str:
    """``"Below naive"`` when FVA < −0.5 pp (naive would beat the plan), else "".

    Positive FVA is the expected good case and stays unlabelled to avoid chip
    clutter.
    """
    return "" if _is_nan(fva) or fva >= -_BIAS_FVA_BAND else BIAS_FVA_BELOW


def _round_or_nan(x: float, ndigits: int = 4) -> float:
    """Round *x* to *ndigits*, passing NaN through untouched."""
    return x if (isinstance(x, float) and math.isnan(x)) else round(x, ndigits)


@dataclass(frozen=True)
class _AccuracyStats:
    """Lag-1 accuracy for one Forecast/Actual/Naive triple over covered months."""
    bias: tuple[float, ...]   # per-month (Forecast−Actual)/Actual; NaN if uncovered
    avg_bias: float
    volume: float             # Σ|Actual|
    abs_error: float          # Σ|Actual − Forecast|
    wmape: float
    fva: float                # WMAPE(naive) − WMAPE(forecast)
    impact: float             # abs_error ÷ total_volume (share of the total miss)


def _accuracy_stats(
    forecast: list[float], actual: list[float], naive: list[float],
    covered, total_volume: float,
) -> _AccuracyStats:
    """Compute Bias / WMAPE / FVA / Impact for one forecast/actual/naive triple.

    The single source of truth for the accuracy arithmetic — used by BOTH the
    segment table and the Corporate × SKU driver cells so the two can never
    drift.  ``covered`` = month indices that had a lag-1 cycle forecast;
    ``total_volume`` = the denominator for Impact (the parent's Σ|Actual|).
    """
    n = len(actual)
    cov = set(covered)
    bias = [
        ((forecast[i] - actual[i]) / actual[i])
        if (i in cov and abs(actual[i]) > 1e-9) else float("nan")
        for i in range(n)
    ]
    valid = [b for i, b in enumerate(bias) if i in cov and not math.isnan(b)]
    avg_bias = (sum(valid) / len(valid)) if valid else float("nan")
    volume = sum(abs(actual[i]) for i in cov)
    abs_error = sum(abs(actual[i] - forecast[i]) for i in cov)
    wmape = (abs_error / volume) if volume > 1e-9 else float("nan")
    wmape_naive = (sum(abs(actual[i] - naive[i]) for i in cov) / volume
                   if volume > 1e-9 else float("nan"))
    fva = (wmape_naive - wmape
           if not (math.isnan(wmape) or math.isnan(wmape_naive)) else float("nan"))
    impact = (abs_error / total_volume) if total_volume > 1e-9 else float("nan")
    return _AccuracyStats(tuple(bias), avg_bias, volume, abs_error, wmape, fva, impact)


def _bias_leaf_series(
    tpl: TemplateRow,
    trk: pd.DataFrame,
    actuals: pd.DataFrame,
    naive: pd.DataFrame,
    month_cycles: list[Optional[str]],
    bias_months: list[date],
    naive_months: list[date],
) -> tuple[list[float], list[float], list[float]]:
    """Return ``(forecast[], actual[], naive[])`` M-lb series for one leaf.

    Forecast for each month = the ROLLING lag-1 cycle for that month
    (``month_cycles[i]``) **Base Plan only** (R&O excluded — the bias measures
    the committed base plan vs orders); actual / naive are orders in the bias
    window and the same-month-a-year-ago window respectively.
    """
    if not trk.empty:
        base = (
            _leaf_mask(trk, tpl)
            & (trk["forecast_type"] == FORECAST_BASE_PLAN)
        )
        trk_cyc = trk["cycle"].astype(str).str.strip()
        f = [
            _sum_millions(trk, base & (trk_cyc == cyc) & (trk["month"] == m))
            if cyc is not None else 0.0
            for cyc, m in zip(month_cycles, bias_months)
        ]
    else:
        f = [0.0] * len(bias_months)
    if actuals is not None and not actuals.empty:
        amask = _leaf_mask(actuals, tpl)
        a = [_sum_millions(actuals, amask & (actuals["month"] == m)) for m in bias_months]
    else:
        a = [0.0] * len(bias_months)
    if naive is not None and not naive.empty:
        nmask = _leaf_mask(naive, tpl)
        nvals = [_sum_millions(naive, nmask & (naive["month"] == m)) for m in naive_months]
    else:
        nvals = [0.0] * len(naive_months)
    return f, a, nvals


def _sum_leaf_series(
    tpl: TemplateRow,
    series_by_row: dict[str, list[float]],
    template_by_id: dict[str, TemplateRow],
    n: int,
) -> list[float]:
    """Element-wise sum of a subtotal's children series (recursive)."""
    total = [0.0] * n
    for child_id in tpl.children:
        child = template_by_id[child_id]
        vals = (
            _sum_leaf_series(child, series_by_row, template_by_id, n)
            if child.is_subtotal else series_by_row[child_id]
        )
        for i in range(n):
            total[i] += vals[i]
    return total


@dataclass(frozen=True)
class _BiasInputs:
    """Shared, enriched inputs for the bias builders (segment table + drivers)."""
    trk: pd.DataFrame
    actuals: pd.DataFrame
    naive: pd.DataFrame
    template: list
    template_by_id: dict
    month_cycles: list
    bias_months: list
    naive_months: list
    month_keys: tuple
    month_meta: tuple
    prior_month: date


def _prepare_bias_inputs(
    tracker_df: Optional[pd.DataFrame],
    ibp_actuals_df: Optional[pd.DataFrame],
    ibp_naive_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    filters: ComparisonFilters,
    *,
    item_master_df: Optional[pd.DataFrame] = None,
    n_months: int = 6,
) -> _BiasInputs:
    """Enrich + filter the sources and resolve the rolling lag-1 month→cycle map.

    Factored out so the Segment table and the Corporate × SKU drivers share ONE
    enrichment + lag-1 setup (the mapping is grain-agnostic — computed once from
    the whole tracker's cycle horizons).  "Actual" is IBP **Orders**.
    """
    prior_month = filters.prior_month.replace(day=1)
    bias_months = _months_back(prior_month, n_months)
    naive_months = [_add_months(m, -12) for m in bias_months]
    month_keys = tuple(m.strftime("%Y-%m") for m in bias_months)

    dim_frame, _warn = _build_augmented_dim_frame(tracker_df, pdh_df, item_master_df)
    trk = _apply_dim_filter(
        _enrich_tracker(tracker_df, dim_frame) if tracker_df is not None else _empty_enriched(),
        filters,
    )
    # Rolling lag-1 cycle per month (freshest 1-month-ahead view; gaps backfill).
    lag1_map = _map_lag1_cycles(bias_months, _cycle_horizon_starts(trk))
    month_cycles = [cyc for cyc, _lag, _fb in lag1_map]
    month_meta = tuple(
        (key, cyc or "", lag or 0, fb)
        for key, (cyc, lag, fb) in zip(month_keys, lag1_map)
    )
    actuals = _apply_dim_filter(
        _enrich_ibp(ibp_actuals_df, dim_frame, qty_candidates=_IBP_ORDERED_QTY_CANDIDATES)
        if ibp_actuals_df is not None else _empty_enriched(actuals=True),
        filters,
    )
    naive = _apply_dim_filter(
        _enrich_ibp(ibp_naive_df, dim_frame, qty_candidates=_IBP_ORDERED_QTY_CANDIDATES)
        if ibp_naive_df is not None else _empty_enriched(actuals=True),
        filters,
    )
    template = _build_runtime_template_for_filters(
        trk, actuals, filters,
        actual_months=set(bias_months), forecast_months=set(), prior_month=prior_month,
        butter_catalog=butter_catalog_combos(pdh_df, item_master_df),
    )
    return _BiasInputs(
        trk=trk, actuals=actuals, naive=naive,
        template=template, template_by_id={t.row_id: t for t in template},
        month_cycles=month_cycles, bias_months=bias_months, naive_months=naive_months,
        month_keys=month_keys, month_meta=month_meta, prior_month=prior_month,
    )


def build_forecast_bias_table(
    tracker_df: Optional[pd.DataFrame],
    ibp_actuals_df: Optional[pd.DataFrame],
    ibp_naive_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    filters: ComparisonFilters,
    *,
    item_master_df: Optional[pd.DataFrame] = None,
    n_months: int = 6,
) -> ForecastBiasResult:
    """Build the Forecast Bias (rolling Lag 1) by Segment × Month table.

    **Rolling lag-1**: each month's forecast comes from the cycle whose horizon
    starts that month (the freshest 1-month-ahead view) — NOT a single fixed
    cycle.  Gap months backfill to the nearest earlier cycle (longer lag,
    flagged).  "Actual" is **IBP Orders** (ordered lbs); bias/WMAPE compare the
    lag-1 forecast to what customers ordered.  *ibp_actuals_df* must be the
    ORDERS covering the six months ending at ``filters.prior_month``;
    *ibp_naive_df* the ORDERS for the same months a year earlier (seasonal-naive
    benchmark).  Segments reuse the comparison hierarchy (incl. dynamic Butter
    detail) and honour the section's Portfolio Major · Supply Format · Brand
    filter.
    """
    bi = _prepare_bias_inputs(
        tracker_df, ibp_actuals_df, ibp_naive_df, pdh_df, filters,
        item_master_df=item_master_df, n_months=n_months)
    trk, actuals, naive = bi.trk, bi.actuals, bi.naive
    template, template_by_id = bi.template, bi.template_by_id
    month_cycles, bias_months, naive_months = bi.month_cycles, bi.bias_months, bi.naive_months
    month_keys, month_meta = bi.month_keys, bi.month_meta

    # Per-leaf forecast / actual / naive series, then roll subtotals up.
    fser: dict[str, list[float]] = {}
    aser: dict[str, list[float]] = {}
    nser: dict[str, list[float]] = {}
    for tpl in template:
        if tpl.is_subtotal:
            continue
        fser[tpl.row_id], aser[tpl.row_id], nser[tpl.row_id] = _bias_leaf_series(
            tpl, trk, actuals, naive, month_cycles, bias_months, naive_months)
    for tpl in template:
        if tpl.is_subtotal:
            fser[tpl.row_id] = _sum_leaf_series(tpl, fser, template_by_id, n_months)
            aser[tpl.row_id] = _sum_leaf_series(tpl, aser, template_by_id, n_months)
            nser[tpl.row_id] = _sum_leaf_series(tpl, nser, template_by_id, n_months)

    # A month is only "covered" when a lag-1 (or backfill) cycle supplied a
    # forecast for it.  Uncovered months (no cycle at all) render blank (NaN),
    # NOT a zero forecast — which would read as a bogus −100% bias.  Coverage is
    # judged on the Total B2C forecast so a segment that genuinely planned zero
    # in a covered month still shows its real (−100%) bias.
    total_f = fser.get("total_b2c", [0.0] * n_months)
    covered = [
        i for i in range(n_months)
        if month_cycles[i] is not None and abs(total_f[i]) > 1e-9
    ]

    # Total-B2C volume anchors the "impact" (materiality) of every segment's
    # error — computed here so the per-row loop can normalise against it.
    total_a = aser.get("total_b2c", [0.0] * n_months)
    total_volume = sum(abs(total_a[i]) for i in covered)

    records: list[dict] = []
    for tpl in template:
        s = _accuracy_stats(
            fser[tpl.row_id], aser[tpl.row_id], nser[tpl.row_id], covered, total_volume)
        rec = {
            COL_ROW_ID: tpl.row_id,
            COL_INDENT: tpl.indent,
            COL_IS_SUBTOTAL: tpl.is_subtotal,
            COL_IS_MEMO: tpl.is_memo,
            COL_LABEL: _make_indented_label(tpl.label, tpl.indent, tpl.is_memo),
            BIAS_COL_AVG: _round_or_nan(s.avg_bias),
            BIAS_COL_WMAPE: _round_or_nan(s.wmape),
            BIAS_COL_FVA: _round_or_nan(s.fva),
            BIAS_COL_VOLUME: _round_or_nan(s.volume),
            BIAS_COL_IMPACT: _round_or_nan(s.impact),
            BIAS_COL_FLAG_DIR: _bias_direction(s.avg_bias),
            BIAS_COL_FLAG_SEV: _bias_severity(s.wmape, s.impact),
            BIAS_COL_FLAG_FVA: _bias_fva_verdict(s.fva),
        }
        for key, b in zip(month_keys, s.bias):
            rec[key] = _round_or_nan(b)
        records.append(rec)

    table = pd.DataFrame.from_records(records)
    available = bool(len(table)) and (trk is not None and not trk.empty)
    return ForecastBiasResult(
        table=table, months=month_keys, available=available, month_meta=month_meta)


# ─────────────────────────────────────────────────────────────────────────────
# Forecast Bias — Corporate group × SKU drivers (lazy, opt-in drill)
# ─────────────────────────────────────────────────────────────────────────────
#
# Corporate group resolves to the SAME canonical table on both legs:
#   forecast (tracker):  Party Site Number → dp_dimshiptosites.plan_to_code
#                        → dp_dimplantosites.customer_num
#                        → dp_dimcustomernames.corporate_group
#   actual  (IBP orders): Customer No       → dp_dimcustomernames.corporate_group
# Both legs END at dp_dimcustomernames.corporate_group, so forecast and actual
# land in IDENTICAL corporate-group buckets at the (corporate group × SKU)
# grain.  plan_to_code is the bridge key because dp_dimshiptosites' own
# customer_num is a different key space that does NOT match dp_dimcustomernames
# (overlap ~0), whereas dp_dimplantosites.customer_num matches it 1:1.  The dim
# frames are passed in by the page (which owns the cached Fabric fetch); column
# spellings mirror ship_to_sites.py / customer_dims.py, kept local so this
# pure-pandas builder needs no Fabric-fetch import.
_STS_PARTY_SITE_CANDIDATES: tuple[str, ...] = (
    "party_site_code", "party_site_number", "PartySiteCode", "Party Site Code",
    "Party Site Number",
)
# The bridge key: dp_dimshiptosites' OWN customer_num is a dead-end (different
# key space from dp_dimcustomernames, overlap ~0); its plan_to_code resolves
# through dp_dimplantosites to a customer_num that matches dp_dimcustomernames.
# The SAME ``plan_to_code`` spelling appears on both dp_dimshiptosites and
# dp_dimplantosites, so one constant serves both sides of the bridge.
_PLAN_TO_CANDIDATES: tuple[str, ...] = (
    "plan_to_code", "PlanToCode", "Plan To Code", "plan_to",
)
_PTS_CUSTOMER_NUM_CANDIDATES: tuple[str, ...] = (
    "customer_num", "customer_number", "CustomerNum", "Customer Num",
)
_CN_CUSTOMER_NUM_CANDIDATES: tuple[str, ...] = (
    "customer_num", "customer_number", "Customer Num", "CustomerNum",
    "Customer No", "customer_no",
)
_CN_CORP_GROUP_CANDIDATES: tuple[str, ...] = (
    "corporate_group", "Corporate Group", "CorporateGroup", "corp_group",
)
_CG_BLANK_TOKENS: frozenset = frozenset({"", "blank", "nan", "none", "null"})
CORP_UNATTRIBUTED: str = "Unattributed"


@dataclass(frozen=True)
class CorpSkuDriversResult:
    """Top-N Corporate × SKU drivers of a segment's lag-1 forecast error.

    ``drivers`` — one row per driver (ranked by pound-error), carrying the
    accuracy metrics, per-month Bias% columns (``YYYY-MM``, for the sparkline),
    ``_fcst`` / ``_act`` per-month tuples (M lbs, for the drill chart), and
    ``soft`` / ``unattributed`` attribution flags.  ``attributed_share`` is the
    fraction of the segment's actual volume that mapped to a real corporate
    group (the rest is soft/unattributed) — surfaced so ops can judge trust.
    """
    drivers: pd.DataFrame
    months: tuple[str, ...]
    segment_label: str
    segment_volume: float
    attributed_share: float           # actual-side: mapped orders ÷ segment orders
    forecast_attributed_share: float  # forecast-side: mapped base plan ÷ segment base plan
    available: bool
    month_meta: tuple = ()


def _canonical_corp_form(values) -> dict[str, str]:
    """Map each corporate-group casefold key → its most common surface form.

    Collapses casing / spacing drift ("ASSOCIATED FOODS" vs "Associated Foods")
    to one display string (winner: most frequent, then longest, then first) —
    the same idea as the PLR canonical map, kept compact and dependency-free.
    """
    counts: dict[str, dict[str, int]] = {}
    for v in values:
        s = str(v).strip()
        if s.casefold() in _CG_BLANK_TOKENS:
            continue
        counts.setdefault(s.casefold(), {})
        counts[s.casefold()][s] = counts[s.casefold()].get(s, 0) + 1
    return {
        key: max(forms.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        for key, forms in counts.items()
    }


def build_corp_group_lookups(
    shiptosites_df: Optional[pd.DataFrame],
    plantosites_df: Optional[pd.DataFrame],
    customernames_df: Optional[pd.DataFrame],
) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(party_site → corporate_group, customer_num → corporate_group)``.

    * **Forecast leg** (``party_site → corporate_group``) composes the full
      three-table chain: ``dp_dimshiptosites`` (party_site_code → plan_to_code)
      → ``dp_dimplantosites`` (plan_to_code → customer_num) →
      ``dp_dimcustomernames`` (customer_num → corporate_group).
    * **Actual leg** (``customer_num → corporate_group``) is the direct
      ``dp_dimcustomernames`` lookup the orders side uses.

    Both maps END at ``dp_dimcustomernames.corporate_group`` so the two legs
    agree.  Customer numbers use the shared item-key normalisation so the joins
    align; corporate groups are canonicalised to one surface form per casefold
    key and blanks dropped.
    """
    # Shared endpoint: customer_num → canonical corporate_group.
    cust2corp: dict[str, str] = {}
    if customernames_df is not None and not customernames_df.empty:
        cn_col = _resolve_column(customernames_df, _CN_CUSTOMER_NUM_CANDIDATES)
        cg_col = _resolve_column(customernames_df, _CN_CORP_GROUP_CANDIDATES)
        if cn_col and cg_col:
            canon = _canonical_corp_form(customernames_df[cg_col])
            tmp = pd.DataFrame({
                "cn": _vectorised_item_key(customernames_df[cn_col]),
                "cg": _vectorised_clean_str(customernames_df[cg_col]),
            })
            tmp = tmp[~tmp["cg"].str.casefold().isin(_CG_BLANK_TOKENS)]
            tmp = tmp.drop_duplicates("cn", keep="last")
            tmp["cg"] = tmp["cg"].map(lambda s: canon.get(s.casefold(), s))
            cust2corp = dict(zip(tmp["cn"], tmp["cg"]))

    # Bridge: plan_to_code → customer_num (dp_dimplantosites).
    plan2cust: dict[str, str] = {}
    if plantosites_df is not None and not plantosites_df.empty:
        p_col = _resolve_column(plantosites_df, _PLAN_TO_CANDIDATES)
        c_col = _resolve_column(plantosites_df, _PTS_CUSTOMER_NUM_CANDIDATES)
        if p_col and c_col:
            tmp = pd.DataFrame({
                "p": _vectorised_clean_str(plantosites_df[p_col]),
                "c": _vectorised_item_key(plantosites_df[c_col]),
            })
            tmp = tmp[tmp["p"].astype(bool)].drop_duplicates("p", keep="last")
            plan2cust = dict(zip(tmp["p"], tmp["c"]))

    # Forecast leg: party_site → plan_to_code → customer_num → corporate_group.
    party2corp: dict[str, str] = {}
    if shiptosites_df is not None and not shiptosites_df.empty:
        ps_col = _resolve_column(shiptosites_df, _STS_PARTY_SITE_CANDIDATES)
        plan_col = _resolve_column(shiptosites_df, _PLAN_TO_CANDIDATES)
        if ps_col and plan_col:
            tmp = pd.DataFrame({
                "ps": _vectorised_clean_str(shiptosites_df[ps_col]),
                "plan": _vectorised_clean_str(shiptosites_df[plan_col]),
            })
            tmp = tmp[tmp["ps"].astype(bool)].drop_duplicates("ps", keep="last")
            for ps, plan in zip(tmp["ps"], tmp["plan"]):
                corp = cust2corp.get(plan2cust.get(plan, ""), "")
                if corp:
                    party2corp[ps] = corp
    return party2corp, cust2corp


def _resolve_corp_forecast(
    trk: pd.DataFrame, party2corp: dict[str, str],
) -> pd.Series:
    """Corporate group per tracker row via the party_site chain (else Unattributed)."""
    if trk.empty:
        return pd.Series([], dtype="object")
    cg = trk["party_site"].map(party2corp).fillna("")
    return cg.where(cg.astype(bool), CORP_UNATTRIBUTED)


def _resolve_corp_actual(
    act: pd.DataFrame, cn2cg: dict[str, str],
) -> tuple[pd.Series, pd.Series]:
    """Corporate group per orders row via customer_no→group.

    Unmapped rows fall back to the Customer Name (soft attribution), else
    Unattributed.  Returns ``(corp_group, is_soft)`` — soft marks the
    customer-name fallback so the UI can label it as weaker attribution.
    """
    if act.empty:
        return pd.Series([], dtype="object"), pd.Series([], dtype=bool)
    cg = act["customer_no"].map(cn2cg).fillna("")
    mapped = cg.astype(bool)
    name = act["customer_name"].astype(str).str.strip()
    soft = (~mapped) & name.astype(bool)
    cg = cg.where(mapped, name.where(name.astype(bool), CORP_UNATTRIBUTED))
    return cg, soft


def _segment_leaf_rows(node_id: str, template: list) -> list:
    """All non-memo leaf TemplateRows at or under *node_id* (deduped).

    Subtotals carry no dimension mask, so a segment slice is the OR of its leaf
    descendants' masks.  Memo leaves (Cottage Cheese / Sour Cream) overlap the
    supply-format leaves and are excluded to avoid double-counting the slice.
    """
    by_id = {t.row_id: t for t in template}
    node = by_id.get(node_id)
    if node is None:
        return []
    out, seen, stack = [], set(), [node]
    while stack:
        t = stack.pop()
        if t.is_subtotal:
            stack.extend(by_id[c] for c in t.children if c in by_id)
        elif not t.is_memo and t.row_id not in seen:
            seen.add(t.row_id)
            out.append(t)
    return out


def _slice_to_segment(df: pd.DataFrame, leaves: list) -> pd.Series:
    """Boolean mask = union of the segment leaves' dimension masks."""
    if df.empty or not leaves:
        return pd.Series([False] * len(df), index=df.index, dtype=bool)
    mask = pd.Series(False, index=df.index)
    for lf in leaves:
        mask |= _leaf_mask(df, lf)
    return mask


def _corp_sku_month_pivot(
    df: pd.DataFrame, months: list, month_keys: tuple, covered: list,
) -> pd.DataFrame:
    """Pivot (corp, item_key) × month_key of summed pounds (M lbs), covered only.

    *months* aligns positionally with *month_keys*; naive uses the year-ago
    months but the SAME month_key labels so forecast/actual/naive align.
    """
    parts = []
    for i in covered:
        sub = df[df["month"] == months[i]]
        if sub.empty:
            continue
        g = sub.groupby(["corp", "item_key"])["pounds"].sum() / _LBS_PER_MILLION
        parts.append(g.rename(month_keys[i]))
    return pd.concat(parts, axis=1) if parts else pd.DataFrame()


def build_forecast_bias_corp_sku_drivers(
    tracker_df: Optional[pd.DataFrame],
    ibp_actuals_df: Optional[pd.DataFrame],
    ibp_naive_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    filters: ComparisonFilters,
    *,
    segment_row_id: str,
    shiptosites_df: Optional[pd.DataFrame] = None,
    plantosites_df: Optional[pd.DataFrame] = None,
    customernames_df: Optional[pd.DataFrame] = None,
    item_master_df: Optional[pd.DataFrame] = None,
    n_months: int = 6,
    top_n: int = 3,
) -> CorpSkuDriversResult:
    """Top-*top_n* Corporate × SKU drivers of *segment_row_id*'s lag-1 error.

    Reuses the shared bias inputs (same enrichment + rolling lag-1 map), slices
    to the chosen segment's leaves, resolves corporate group on both legs, then
    groups by (corporate group × SKU) and ranks cells by **pound-error**
    (Impact) — the materiality-consistent "what's actually moving the miss".
    """
    bi = _prepare_bias_inputs(
        tracker_df, ibp_actuals_df, ibp_naive_df, pdh_df, filters,
        item_master_df=item_master_df, n_months=n_months)
    month_keys, month_cycles = bi.month_keys, bi.month_cycles
    bias_months, naive_months = bi.bias_months, bi.naive_months
    segment_label = bi.template_by_id.get(
        segment_row_id, TemplateRow(segment_row_id, segment_row_id, 0)).label

    party2corp, cust2corp = build_corp_group_lookups(
        shiptosites_df, plantosites_df, customernames_df)
    leaves = _segment_leaf_rows(segment_row_id, bi.template)

    # Slice each source to the segment; forecast is Base Plan only (excl. R&O).
    trk = bi.trk[
        _slice_to_segment(bi.trk, leaves)
        & (bi.trk["forecast_type"] == FORECAST_BASE_PLAN)
    ].copy()
    act = bi.actuals[_slice_to_segment(bi.actuals, leaves)].copy()
    nai = bi.naive[_slice_to_segment(bi.naive, leaves)].copy()

    empty = CorpSkuDriversResult(
        pd.DataFrame(), month_keys, segment_label, 0.0, float("nan"), float("nan"),
        False, bi.month_meta)
    if trk.empty and act.empty:
        return empty

    trk["corp"] = _resolve_corp_forecast(trk, party2corp)
    act["corp"], act_soft = _resolve_corp_actual(act, cust2corp)
    act["_soft"] = act_soft
    nai["corp"], _ = _resolve_corp_actual(nai, cust2corp)

    # Covered = months with a lag-1 cycle AND a non-zero forecast in this slice.
    covered = [
        i for i, (cyc, m) in enumerate(zip(month_cycles, bias_months))
        if cyc is not None
        and trk[(trk["cycle"] == cyc) & (trk["month"] == m)]["pounds"].sum() > 1e-3
    ]
    if not covered:
        return empty

    # Forecast uses each covered month's lag-1 cycle; actual/naive use the month.
    f_parts = []
    for i in covered:
        sub = trk[(trk["cycle"] == month_cycles[i]) & (trk["month"] == bias_months[i])]
        if not sub.empty:
            g = sub.groupby(["corp", "item_key"])["pounds"].sum() / _LBS_PER_MILLION
            f_parts.append(g.rename(month_keys[i]))
    f_piv = pd.concat(f_parts, axis=1) if f_parts else pd.DataFrame()
    a_piv = _corp_sku_month_pivot(act, bias_months, month_keys, covered)
    n_piv = _corp_sku_month_pivot(nai, naive_months, month_keys, covered)

    cells = f_piv.index.union(a_piv.index) if not (f_piv.empty and a_piv.empty) else []
    # Segment actual volume (the Impact denominator) + attribution coverage.
    act_cov = act[act["month"].isin([bias_months[i] for i in covered])]
    seg_volume = float(act_cov["pounds"].abs().sum()) / _LBS_PER_MILLION
    mapped_vol = float(
        act_cov.loc[~act_cov["_soft"] & (act_cov["corp"] != CORP_UNATTRIBUTED),
                    "pounds"].abs().sum()) / _LBS_PER_MILLION
    attributed_share = (mapped_vol / seg_volume) if seg_volume > 1e-9 else float("nan")
    # Forecast-side coverage: the fraction of the (covered-month) base plan that
    # reached a real corporate group — the leg that party_site→corp must resolve.
    # This gates the UI: a near-zero share means the dim keys don't reconcile.
    trk_cov = trk[trk["month"].isin([bias_months[i] for i in covered])]
    fcst_total = float(trk_cov["pounds"].abs().sum())
    fcst_mapped = float(
        trk_cov.loc[trk_cov["corp"] != CORP_UNATTRIBUTED, "pounds"].abs().sum())
    forecast_attributed_share = (
        (fcst_mapped / fcst_total) if fcst_total > 1e-9 else float("nan"))

    pos = {mk: i for i, mk in enumerate(month_keys)}

    def _series(piv: pd.DataFrame, key) -> list:
        arr = [0.0] * n_months
        if not piv.empty and key in piv.index:
            row = piv.loc[key]
            for mk in piv.columns:
                v = row[mk]
                arr[pos[mk]] = float(v) if not pd.isna(v) else 0.0
        return arr

    desc_map: dict[str, str] = {}
    for src in (trk, act):
        if "item_desc" in src.columns:
            for k, d in zip(src["item_key"], src["item_desc"]):
                if k and d and k not in desc_map:
                    desc_map[k] = d
    soft_corps = set(act.loc[act["_soft"], "corp"])

    records = []
    for corp, item_key in cells:
        f = _series(f_piv, (corp, item_key))
        a = _series(a_piv, (corp, item_key))
        nv = _series(n_piv, (corp, item_key))
        s = _accuracy_stats(f, a, nv, covered, seg_volume)
        if s.volume <= 1e-9 and s.abs_error <= 1e-9:
            continue
        rec = {
            "corp_group": corp,
            "item_key": item_key,
            "item_desc": desc_map.get(item_key, ""),
            "soft": (corp in soft_corps) and (corp != CORP_UNATTRIBUTED),
            "unattributed": corp == CORP_UNATTRIBUTED,
            "_driver_id": f"{corp}␟{item_key}",
            "_abs_error": s.abs_error,
            BIAS_COL_VOLUME: _round_or_nan(s.volume),
            BIAS_COL_WMAPE: _round_or_nan(s.wmape),
            BIAS_COL_AVG: _round_or_nan(s.avg_bias),
            BIAS_COL_FVA: _round_or_nan(s.fva),
            BIAS_COL_IMPACT: _round_or_nan(s.impact),
            BIAS_COL_FLAG_SEV: _bias_severity(s.wmape, s.impact),
            "_fcst": tuple(round(x, 4) for x in f),
            "_act": tuple(round(x, 4) for x in a),
        }
        for mk, b in zip(month_keys, s.bias):
            rec[mk] = _round_or_nan(b)
        records.append(rec)

    if not records:
        return empty
    drivers = (
        pd.DataFrame.from_records(records)
        .sort_values("_abs_error", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )
    return CorpSkuDriversResult(
        drivers=drivers, months=month_keys, segment_label=segment_label,
        segment_volume=seg_volume, attributed_share=attributed_share,
        forecast_attributed_share=forecast_attributed_share,
        available=bool(len(drivers)), month_meta=bi.month_meta)


# ─────────────────────────────────────────────────────────────────────────────
# Prior-month shipment diagnostic (reconciliation aid)
# ─────────────────────────────────────────────────────────────────────────────

# Label for a dimension that PDH left blank (item absent from PDH, or no
# Supply Format) — surfaced so "lost" pounds are visible, not silently gone.
DIAG_UNMAPPED: str = "(unmapped)"

# Diagnostic frame column names (kept as constants so the view never
# string-types them by hand).
DIAG_COL_PMAJ: str = "Portfolio Major"
DIAG_COL_SFMT: str = "Supply Format"
DIAG_COL_LBS: str = "Shipped Lbs"
DIAG_COL_MLBS: str = "Shipped (M lbs)"


def build_prior_month_shipment_diagnostic(
    ibp: Optional[pd.DataFrame], prior_month: date,
) -> pd.DataFrame:
    """Return prior-month IBP Shipments broken down by Portfolio Major × Supply Format.

    A pure reconciliation aid: it sums the **already-enriched** shipments
    (PDH dims attached) for the prior month and groups by
    ``(Portfolio Major, Supply Format)`` so a planner can see how a raw
    prior-month shipment total decomposes — in particular which ESL
    Supply Formats roll into the *ESL* hierarchy line (Large/Small Carton,
    Aerosol Can), which land in the separate *Aseptic* line, and which fall
    outside the reporting hierarchy entirely.  Blank dims surface as
    :data:`DIAG_UNMAPPED` (item not in PDH / no format).

    Returns a plain DataFrame — columns ``Portfolio Major, Supply Format,
    Shipped Lbs, Shipped (M lbs)`` — sorted by pounds desc.  No Streamlit,
    no I/O, no custom types: safe to cache or render directly.  Empty frame
    when there are no prior-month rows.
    """
    cols = [DIAG_COL_PMAJ, DIAG_COL_SFMT, DIAG_COL_LBS, DIAG_COL_MLBS]
    if ibp is None or ibp.empty:
        return pd.DataFrame(columns=cols)

    sub = ibp.loc[ibp["month"] == prior_month]
    if sub.empty:
        return pd.DataFrame(columns=cols)

    grouped = pd.DataFrame({
        DIAG_COL_PMAJ: sub["pmaj"].astype(str).str.strip().replace("", DIAG_UNMAPPED),
        DIAG_COL_SFMT: sub["sfmt"].astype(str).str.strip().replace("", DIAG_UNMAPPED),
        DIAG_COL_LBS: pd.to_numeric(sub["pounds"], errors="coerce").fillna(0.0),
    })
    out = (
        grouped.groupby([DIAG_COL_PMAJ, DIAG_COL_SFMT], as_index=False)[DIAG_COL_LBS]
        .sum()
        .sort_values(DIAG_COL_LBS, ascending=False, ignore_index=True)
    )
    out[DIAG_COL_MLBS] = out[DIAG_COL_LBS] / _LBS_PER_MILLION
    return out[cols]


def _build_runtime_template_for_filters(
    trk: pd.DataFrame,
    ibp: pd.DataFrame,
    filters: ComparisonFilters,
    *,
    actual_months: set[date],
    forecast_months: set[date],
    prior_month: date,
    butter_catalog: tuple[tuple[str, str], ...] = (),
) -> tuple[TemplateRow, ...]:
    """Return the table template with dynamic Butter detail rows.

    Planner rule: under the static Butter row, show additional detail rows
    in this hierarchy:

        Butter
          Branded
            <Supply Format 1>
            <Supply Format 2>
          Private
            <Supply Format ...>

    The detail rows are generated dynamically from rows that can
    contribute to the current selection (actual/forecast/prior windows +
    selected cycles), keeping the section "clean" (no zero-only stale
    formats from unrelated periods).
    """
    dynamic_rows = _build_dynamic_butter_rows(
        trk, ibp, filters,
        actual_months=actual_months,
        forecast_months=forecast_months,
        prior_month=prior_month,
        butter_catalog=butter_catalog,
    )
    if not dynamic_rows:
        return COMPARISON_TEMPLATE

    out: list[TemplateRow] = []
    for tpl in COMPARISON_TEMPLATE:
        out.append(tpl)
        if tpl.row_id == "butter":
            out.extend(dynamic_rows)
    return tuple(out)


def _build_dynamic_butter_rows(
    trk: pd.DataFrame,
    ibp: pd.DataFrame,
    filters: ComparisonFilters,
    *,
    actual_months: set[date],
    forecast_months: set[date],
    prior_month: date,
    butter_catalog: tuple[tuple[str, str], ...] = (),
) -> tuple[TemplateRow, ...]:
    """Build the Butter detail rows (Branded / Private → Supply Format leaves).

    Combos come from BOTH the plan data (real names + numbers, already
    dim-filtered by the caller) AND the item *catalog* (``butter_catalog``), so
    every catalogued Packaged-Butter format shows — including ones with no plan
    (Private / Chips / Elgin Solid …), which render as zeros.  Catalog combos
    are deduped against the data combos by a loose key (so PDH's "Elgin Quarter"
    doesn't double the plan's "Elgin Quarters"), and the whole set honours the
    active Portfolio Major / Supply Format / Brand filter.
    """
    def _collect_from_tracker() -> pd.DataFrame:
        if trk.empty:
            return pd.DataFrame(columns=["brand", "sfmt"])
        trk_months = trk["month"]
        contributes = (
            trk_months.isin(actual_months | forecast_months | {prior_month})
            & trk["cycle"].isin((filters.current_cycle, filters.prior_cycle))
            & trk["forecast_type"].isin((FORECAST_BASE_PLAN, FORECAST_R_AND_O))
        )
        mask = _leaf_mask(trk, TemplateRow(
            row_id="__butter_probe",
            label="__butter_probe",
            indent=0,
            pmaj_match=_BUTTER,
            pminor_match=_BUTTER_PMINOR,
        ))
        return trk.loc[contributes & mask, ["brand", "sfmt"]].copy()

    def _collect_from_ibp() -> pd.DataFrame:
        if ibp.empty:
            return pd.DataFrame(columns=["brand", "sfmt"])
        ibp_months = ibp["month"]
        contributes = ibp_months.isin(actual_months | {prior_month})
        mask = _leaf_mask(ibp, TemplateRow(
            row_id="__butter_probe",
            label="__butter_probe",
            indent=0,
            pmaj_match=_BUTTER,
            pminor_match=_BUTTER_PMINOR,
        ))
        return ibp.loc[contributes & mask, ["brand", "sfmt"]].copy()

    candidates = pd.concat([_collect_from_tracker(), _collect_from_ibp()], ignore_index=True)
    candidates["brand"] = candidates["brand"].astype(str).str.strip()
    candidates["sfmt"] = candidates["sfmt"].astype(str).str.strip()
    candidates = candidates.loc[(candidates["brand"] != "") & (candidates["sfmt"] != "")]

    # Merge the plan combos with the catalog combos.  Data names win on a loose
    # key clash (casefold + trailing-'s') so "Elgin Quarter"/"Elgin Quarters"
    # collapse to one row keyed on the plan's display name.
    def _key(brand: str, sfmt: str) -> tuple[str, str]:
        return (brand.casefold(), sfmt.strip().casefold().rstrip("s"))

    combos: dict[tuple[str, str], tuple[str, str]] = {}
    for brand, sfmt in zip(candidates["brand"], candidates["sfmt"]):
        combos[_key(brand, sfmt)] = (brand, sfmt)
    for brand, sfmt in butter_catalog:
        brand, sfmt = str(brand).strip(), str(sfmt).strip()
        if brand and sfmt:
            combos.setdefault(_key(brand, sfmt), (brand, sfmt))

    merged = _filter_butter_combos(list(combos.values()), filters)
    if not merged:
        return ()

    rows: list[TemplateRow] = []
    for brand in (BRAND_BRANDED, BRAND_PRIVATE):
        brand_formats = sorted({s for b, s in merged if b == brand})
        if not brand_formats:
            continue
        brand_id = f"butter_{brand.casefold()}"
        child_ids = tuple(_stable_unique_row_ids(brand_id, brand_formats))
        rows.append(_subtotal(brand_id, brand, 2, child_ids))
        for sfmt, child_id in zip(brand_formats, child_ids):
            rows.append(_leaf(
                child_id, sfmt, 3,
                pmaj_match=_BUTTER,
                pminor_match=_BUTTER_PMINOR,
                brand_match=brand,
                sfmt_match=sfmt,
            ))
    return tuple(rows)


def _filter_butter_combos(
    combos: list[tuple[str, str]], filters: ComparisonFilters,
) -> list[tuple[str, str]]:
    """Narrow Butter ``(brand, sfmt)`` combos to the active PMaj/SFmt/Brand filter.

    Butter's Portfolio Major is always "Butter", so a Portfolio-Major whitelist
    that excludes it drops every combo; the Supply-Format / Brand whitelists
    filter the leaves (matched case-insensitively for SFmt).
    """
    if filters.pmaj_filter and not (
        {p.casefold() for p in _BUTTER} & {p.casefold() for p in filters.pmaj_filter}
    ):
        return []
    sfmt_wl = {s.casefold() for s in filters.sfmt_filter} if filters.sfmt_filter else None
    brand_wl = set(filters.brand_filter) if filters.brand_filter else None
    # Concatenated combo filters (Butter combos only): include whitelist and/or
    # search-to-hide exclude set, keyed on (sfmt casefold, brand).
    butter_names = {x.casefold() for x in _BUTTER}
    combo_wl = (
        {(s.casefold(), b) for p, s, b in filters.combo_filter
         if p.casefold() in butter_names}
        if filters.combo_filter else None
    )
    combo_drop = {
        (s.casefold(), b) for p, s, b in filters.combo_exclude
        if p.casefold() in butter_names
    }
    out: list[tuple[str, str]] = []
    for brand, sfmt in combos:
        if brand_wl and brand not in brand_wl:
            continue
        if sfmt_wl and sfmt.casefold() not in sfmt_wl:
            continue
        if combo_wl is not None and (sfmt.casefold(), brand) not in combo_wl:
            continue
        if (sfmt.casefold(), brand) in combo_drop:
            continue
        out.append((brand, sfmt))
    return out


def _slugify_for_row_id(text: str) -> str:
    """Return a safe row-id slug from display text."""
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in text.strip())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned or "blank"


def _stable_unique_row_ids(prefix: str, labels: list[str]) -> list[str]:
    """Return deterministic, collision-safe row ids for dynamic labels."""
    seen: dict[str, int] = {}
    ids: list[str] = []
    for label in labels:
        base = f"{prefix}_sfmt_{_slugify_for_row_id(label)}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        ids.append(base if n == 0 else f"{base}_{n+1}")
    return ids


def _safe_ratio(numerator: float, denominator: float) -> float:
    """Return numerator / denominator, or 0.0 when the denominator is ~0."""
    if abs(denominator) < 1e-9:
        return 0.0
    return numerator / denominator


def _make_indented_label(label: str, indent: int, is_memo: bool) -> str:
    """Return the indented row label.

    Two NBSPs per indent level (matching the RO Summary / MOM tables) so
    browser whitespace-collapsing doesn't flatten the hierarchy.  Memo
    rows get a leading bullet so the planner can tell them apart from the
    summing children at the same depth.
    """
    prefix = "\u00A0\u00A0" * indent
    if is_memo:
        return f"{prefix}• {label}"
    return prefix + label


# ─────────────────────────────────────────────────────────────────────────────
# Download helper
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Driver tables (PM Actual + Base Plan)
# ─────────────────────────────────────────────────────────────────────────────
#
# Two diagnostic tables that "explain" the comparison's PM Actual and
# Base Plan columns.  Each is grouped by the raw (Portfolio Major,
# Supply Format, Brand) dimensions present in the data (NOT the template
# hierarchy) and, per group, surfaces the top-5 drivers — the buckets
# whose signed contribution moved the group's value the most.
#
#   Portfolio Major │ Supply Format │ Brand │ <metric> │ #1 … #5
#
# Driver cells show ``Customer – Account/Customer No  (+amount)`` (no
# item description — that lives in the drill-down).  Both metrics are
# *deltas* (signed), so a driver's sign tells the planner whether that
# customer/account pushed the number up or down.

DRV_COL_PMAJ: str = "Portfolio Major"
DRV_COL_SFMT: str = "Supply Format"
DRV_COL_BRAND: str = "Brand"
DRV_BASE_PLAN_VALUE: str = "Base Plan"
DRV_PM_ACTUAL_VALUE: str = "PM Actual"
DRV_DRIVER_COLS: tuple[str, ...] = (
    "#1 Driver", "#2 Driver", "#3 Driver", "#4 Driver", "#5 Driver",
)
_DRV_BLANK: str = "(blank)"
_DRV_TOP_N: int = 5

# Item-level bucket frame — powers Portfolio Minor filtering and the
# drill-down expander beneath each driver table.
_DRV_BUCKET_COLS: tuple[str, ...] = (
    "pmaj", "sfmt", "brand", "pminor", "bucket_label",
    "item_key", "item_desc", "customer_name", "customer_id", "delta",
)

# Drill-down display columns (page maps these to column_config).
DRV_ITEM_COL_ITEM: str = "Item #"
DRV_ITEM_COL_DESC: str = "Description"
DRV_ITEM_COL_BRAND: str = "Brand"
DRV_ITEM_COL_CUSTOMER: str = "Customer"
DRV_ITEM_COL_CUSTOMER_ID: str = "Customer / Account No"
DRV_ITEM_COL_DELTA: str = "Δ (millions lbs)"


@dataclass(frozen=True)
class DriverTableResult:
    """Aggregated driver table plus item-level bucket detail."""

    table: pd.DataFrame
    buckets: pd.DataFrame


def _format_millions_signed(value_m: float) -> str:
    """Return a signed, comma-grouped millions string (1 dp), e.g. ``+1.2``."""
    sign = "+" if value_m >= 0 else "-"
    return f"{sign}{abs(value_m):,.1f}"


def _driver_cell(label: str, value_m: float) -> str:
    """Render a driver cell: ``"{label}  (+1.2)"`` (value in millions)."""
    return f"{label}  ({_format_millions_signed(value_m)})"


def _join_label(parts: list[str]) -> str:
    """Join driver-label parts with a spaced en-dash separator."""
    return " – ".join(p if p else _DRV_BLANK for p in parts)


def _empty_driver_table(value_col: str) -> pd.DataFrame:
    """Return an empty driver table with the canonical column order."""
    return pd.DataFrame(columns=[
        DRV_COL_PMAJ, DRV_COL_SFMT, DRV_COL_BRAND, value_col, *DRV_DRIVER_COLS,
    ])


def _empty_driver_buckets() -> pd.DataFrame:
    """Return an empty item-level bucket frame."""
    return pd.DataFrame(columns=list(_DRV_BUCKET_COLS))


def _empty_driver_result(value_col: str) -> DriverTableResult:
    return DriverTableResult(
        table=_empty_driver_table(value_col),
        buckets=_empty_driver_buckets(),
    )


def _normalise_driver_group_value(value: object) -> str:
    """Normalise a dimension value for driver grouping / filtering."""
    s = _clean_str(value)
    return s if s else "(Unspecified)"


def _build_driver_table(buckets: pd.DataFrame, value_col: str) -> pd.DataFrame:
    """Group *buckets* by (PMaj, SFmt, Brand) → value + top-5 drivers.

    *buckets* must carry columns ``pmaj, sfmt, brand, bucket_label,
    delta`` where ``delta`` is the signed contribution **in pounds**.
    Buckets sharing a ``bucket_label`` within a group are summed first
    (so one customer/account collapses into a single driver), then the
    top five by ``|Σ delta|`` are reported.  The group value is the net
    Σ delta in millions, rounded to 1 dp.

    Output rows are sorted alphabetically by Portfolio Major →
    Supply Format → Brand (planner request, 2026-06).
    """
    cols = [DRV_COL_PMAJ, DRV_COL_SFMT, DRV_COL_BRAND, value_col, *DRV_DRIVER_COLS]
    if buckets is None or buckets.empty:
        return _empty_driver_table(value_col)

    work = buckets.copy()
    for c in ("pmaj", "sfmt", "brand"):
        work[c] = work[c].map(_normalise_driver_group_value)
    work["bucket_label"] = work["bucket_label"].astype(str)
    work["delta"] = pd.to_numeric(work["delta"], errors="coerce").fillna(0.0)

    rows: list[dict] = []
    for (pmaj, sfmt, brand), group in work.groupby(
        ["pmaj", "sfmt", "brand"], sort=False,
    ):
        net_m = float(group["delta"].sum()) / _LBS_PER_MILLION

        agg = (
            group.groupby("bucket_label", dropna=False)["delta"]
            .sum().reset_index()
        )
        ranked = agg.assign(_abs=agg["delta"].abs()).sort_values(
            by=["_abs", "bucket_label"], ascending=[False, True],
            kind="mergesort",
        )
        drivers: list[str] = [
            _driver_cell(str(r["bucket_label"]), float(r["delta"]) / _LBS_PER_MILLION)
            for _, r in ranked.head(_DRV_TOP_N).iterrows()
        ]
        while len(drivers) < _DRV_TOP_N:
            drivers.append("")

        row_data = {
            DRV_COL_PMAJ: pmaj, DRV_COL_SFMT: sfmt, DRV_COL_BRAND: brand,
            value_col: round(net_m, _MILLIONS_DISPLAY_DECIMALS),
        }
        for col_name, cell in zip(DRV_DRIVER_COLS, drivers):
            row_data[col_name] = cell
        rows.append(row_data)

    out = pd.DataFrame(rows, columns=cols)
    out = out.sort_values(
        by=[DRV_COL_PMAJ, DRV_COL_SFMT, DRV_COL_BRAND],
        ascending=[True, True, True],
        kind="mergesort",
    ).reset_index(drop=True)
    return out


def _prepare_driver_bucket_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalise an item-level bucket frame for aggregation + drill-down."""
    if raw is None or raw.empty:
        return _empty_driver_buckets()
    out = raw.copy()
    for col in _DRV_BUCKET_COLS:
        if col not in out.columns:
            out[col] = ""
    for c in ("pmaj", "sfmt", "brand"):
        out[c] = out[c].map(_normalise_driver_group_value)
    out["pminor"] = out["pminor"].map(_clean_str).replace({"": _DRV_BLANK})
    out["bucket_label"] = out["bucket_label"].astype(str)
    out["item_key"] = out["item_key"].map(_clean_str)
    out["item_desc"] = out["item_desc"].map(_clean_str)
    out["customer_name"] = out["customer_name"].map(_clean_str).replace({"": _DRV_BLANK})
    out["customer_id"] = out["customer_id"].map(_clean_str).replace({"": _DRV_BLANK})
    out["delta"] = pd.to_numeric(out["delta"], errors="coerce").fillna(0.0)
    return out[list(_DRV_BUCKET_COLS)]


def _assemble_driver_result(
    buckets: pd.DataFrame, value_col: str,
) -> DriverTableResult:
    """Build the aggregated table + normalised bucket detail."""
    prepared = _prepare_driver_bucket_frame(buckets)
    table = _build_driver_table(prepared, value_col)
    return DriverTableResult(table=table, buckets=prepared)


# ─────────────────────────────────────────────────────────────────────────────
# Demand MOM Summary (month-over-month: actuals stitched onto the forecast)
# ─────────────────────────────────────────────────────────────────────────────
#
# The Demand MOM Summary renders the SAME hierarchical pivot as the classic
# Demand Pivot (Portfolio Major → Forecast Type → Supply Format, monthly
# columns in millions of pounds) but stitches two sources onto one month
# axis:
#
#   * ACTUAL months  → ``dbo.IBP Shipments`` (Shipped Qty lbs).  Surfaced
#     under a third Forecast Type branch, ``"Actual"``, since shipments
#     carry no Base/R&O split.
#   * FORECAST months → ``qry_mgmt_plan_history_tracker.csv`` for the
#     selected ``Cycle`` (Base Plan / R&O).
#
# The two windows are disjoint (see :func:`validate_mom_filters`), so on the
# stitched month axis the Actual branch populates only actual-window columns
# and the Base/R&O branches only forecast-window columns — one continuous
# month-over-month view.  All hierarchy / footer / chart shaping is delegated
# to the shared primitives in ``demand_summary`` so the MOM table is
# pixel-identical in layout to the classic pivot.

# The synthetic Forecast Type branch that carries IBP Shipments actuals.
SERIES_ACTUAL: str = "Actual"

# Row order beneath each Portfolio Major: actuals first (chronologically the
# left-most months), then the two forecast branches.  Only the forecast
# branches carry an annual Total Budget.
MOM_FORECAST_ORDER: tuple[str, ...] = (
    SERIES_ACTUAL, FORECAST_BASE_PLAN, FORECAST_R_AND_O,
)
_MOM_BUDGETED_FORECASTS: set[str] = {FORECAST_BASE_PLAN, FORECAST_R_AND_O}

# Pounds → millions (grep-able single definition, matches demand_summary).
_MOM_LBS_PER_MILLION: float = 1_000_000.0
# "Row rounds to zero" tolerance in millions — mirrors demand_summary's
# _EMPTY_ROW_TOLERANCE_M so "captured" here agrees with what the pivot draws.
_MOM_ZERO_TOL_M: float = 0.05

# Hidden dimension-breadcrumb columns on the assembled pivot (re-exported
# from demand_summary so the page can resolve a clicked row → its
# (Portfolio Major, Forecast Type, Supply Format) leaf for the drill-down).
MOM_ROW_PMAJ: str     = _COL_PMAJ_DIM
MOM_ROW_FORECAST: str = _COL_FORECAST_DIM
MOM_ROW_SFMT: str     = _COL_SFMT_DIM

# MOM-log-specific reason column (Item / Description / PMaj / SFmt are the
# shared NC_COL_* constants defined earlier in the module).
NC_COL_REASON: str  = "Reason"
_NC_COLUMNS: tuple[str, ...] = (
    NC_COL_ITEM, NC_COL_DESC, NC_COL_PMAJ, NC_COL_SFMT, NC_COL_REASON,
)

# The reasons a tracker item can be missing from the MOM Summary, most
# actionable (data-quality) first.
_NC_REASON_NO_MAPPING: str = (
    "No Portfolio Major / Supply Format mapping (PDH and RO_Item_Master)"
)
_NC_REASON_FILTERED: str   = "Excluded by the active Portfolio Major / Supply Format filter"
_NC_REASON_ZERO: str       = "Zero forecast pounds in the selected window"

# SKU drill-down display column names.
SKU_COL_ITEM: str = "Item"
SKU_COL_DESC: str = "Item Description"
SKU_COL_TOTAL: str = "Total"


@dataclass(frozen=True)
class DemandMomFilters:
    """User selection driving the Demand MOM Summary.

    Attributes
    ----------
    cycle
        The tracker ``Cycle`` supplying the forecast months.
    actual_start / actual_end
        Inclusive first-of-month bounds for the **actual** window — pulled
        from IBP Shipments.
    forecast_start / forecast_end
        Inclusive first-of-month bounds for the **forecast** window —
        pulled from the tracker for *cycle*.  Must not overlap the actual
        window (see :func:`validate_mom_filters`).
    portfolio_majors / supply_formats
        Optional whitelists (``None`` = include every value), applied
        conjunctively — same semantics as the classic pivot's filters.
    """
    cycle: str
    actual_start: date
    actual_end: date
    forecast_start: date
    forecast_end: date
    portfolio_majors: Optional[tuple[str, ...]] = None
    supply_formats: Optional[tuple[str, ...]] = None


@dataclass(frozen=True)
class DemandMomResult:
    """Output of :func:`build_demand_mom_pivot`.

    Carries the same display artifacts as
    :class:`demand_summary.DemandPivotResult` (so the page's pivot-table
    and chart renderers consume them unchanged) plus three MOM-specific
    additions: the ``Actual`` footer row, the item-level ``item_detail``
    frame behind the drill-down, and the ``not_captured_items``
    reconciliation log.
    """
    pivot: pd.DataFrame
    month_columns: tuple[str, ...]
    actual_month_columns: tuple[str, ...]
    forecast_month_columns: tuple[str, ...]
    actual_totals: pd.DataFrame
    base_plan_totals: pd.DataFrame
    r_and_o_totals: pd.DataFrame
    budget_totals: pd.DataFrame
    budget_by_month: dict[str, float]
    budget_total_m: float
    has_pivot_budget_data: bool
    has_budget_data: bool
    chart_long: pd.DataFrame
    item_detail: pd.DataFrame
    not_captured_items: pd.DataFrame
    has_actuals: bool

    def sku_detail_for(
        self, pmaj: str = "", forecast: str = "", sfmt: str = "",
    ) -> pd.DataFrame:
        """Return the SKU-level detail behind a clicked pivot row.

        Filters :attr:`item_detail` to the (Portfolio Major, Forecast
        Type, Supply Format) breadcrumb of the selected row — an empty
        string on any level means "don't pin that level" (so clicking a
        Portfolio Major header returns every item under it, a Forecast
        Type subtotal every item in that branch, a Supply Format leaf
        just that leaf).  The result is pivoted to one row per Item ×
        month + a ``Total`` column, ready to display and download.
        """
        detail = self.item_detail
        if detail.empty:
            return pd.DataFrame()

        mask = pd.Series(True, index=detail.index)
        if pmaj:
            mask &= detail["__pmaj"] == pmaj
        if forecast:
            mask &= detail["__forecast"] == forecast
        if sfmt:
            mask &= detail["__sfmt"] == sfmt
        sub = detail.loc[mask]
        if sub.empty:
            return pd.DataFrame()

        wide = sub.pivot_table(
            index=["item_key", "item_desc"],
            columns="__month_label",
            values="__lbs_m",
            aggfunc="sum",
            fill_value=0.0,
        )
        month_cols = list(wide.columns)
        wide = wide.reset_index().rename(
            columns={"item_key": SKU_COL_ITEM, "item_desc": SKU_COL_DESC},
        )
        wide[SKU_COL_TOTAL] = wide[month_cols].sum(axis=1).round(1)
        for c in month_cols:
            wide[c] = wide[c].round(1)
        # Heaviest items first — the planner scans top-down for movers.
        wide = wide.sort_values(SKU_COL_TOTAL, ascending=False).reset_index(drop=True)
        return wide[[SKU_COL_ITEM, SKU_COL_DESC, *month_cols, SKU_COL_TOTAL]]


def validate_mom_filters(filters: DemandMomFilters) -> list[str]:
    """Return human-readable validation errors for a MOM selection (empty = OK).

    Enforces start ≤ end on each window and the planner's rule that the
    actual and forecast windows must be disjoint (otherwise a month would
    be sourced from both IBP Shipments and the tracker at once).
    """
    errors: list[str] = []
    if filters.actual_start > filters.actual_end:
        errors.append("Actual range: the beginning month is after the end month.")
    if filters.forecast_start > filters.forecast_end:
        errors.append("Forecast range: the beginning month is after the end month.")
    overlap = (
        filters.actual_start <= filters.forecast_end
        and filters.forecast_start <= filters.actual_end
    )
    if overlap:
        errors.append(
            "Actual and forecast month ranges overlap.  A month can be an "
            "actual OR a forecast, not both — adjust one of the ranges so "
            "they are disjoint."
        )
    return errors


def _norm_dim(value) -> str:
    """Normalise a dimension value to the pivot's bucket key.

    Trims whitespace and maps NaN / None / empty → the ``(blank)``
    sentinel so unmapped items land in a clearly-labelled bucket rather
    than a ``NaN`` group (matches demand_summary's ``_prepare_long_frame``).
    """
    try:
        if value is None or pd.isna(value):
            return PMAJ_BLANK_LABEL
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    return s if s else PMAJ_BLANK_LABEL


def list_mom_filter_values(
    tracker_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    item_master_df: Optional[pd.DataFrame] = None,
) -> dict[str, list[str]]:
    """Return the Portfolio Major / Supply Format options for the MOM filters.

    Scoped to the items the tracker actually carries (joined to the same
    PDH → RO_Item_Master dim cascade the pivot uses) so the dropdowns list
    exactly the values that can appear in the table.  Tracker items unknown
    to **both** sources contribute the ``(blank)`` sentinel so they remain
    selectable.  Returns ``{"portfolio_majors": [...], "supply_formats":
    [...]}`` (sorted, ``(blank)`` last).
    """
    empty = {"portfolio_majors": [], "supply_formats": []}
    dim = build_item_dim_frame_cascade(pdh_df, item_master_df)
    if tracker_df is None or tracker_df.empty or TRK_ITEM not in tracker_df.columns:
        return empty

    keys = set(_vectorised_item_key(tracker_df[TRK_ITEM]).tolist()) - {""}
    if not keys:
        return empty

    if dim.empty:
        # No dims at all — every item lands in the blank bucket.
        return {
            "portfolio_majors": [PMAJ_BLANK_LABEL],
            "supply_formats": [PMAJ_BLANK_LABEL],
        }

    sub = dim.loc[dim["__item_key"].isin(keys)]
    pmajs = {_norm_dim(v) for v in sub["pmaj"]}
    sfmts = {_norm_dim(v) for v in sub["sfmt"]}
    # Any tracker item missing from PDH → surface the blank bucket option.
    if keys - set(dim["__item_key"]):
        pmajs.add(PMAJ_BLANK_LABEL)
        sfmts.add(PMAJ_BLANK_LABEL)

    def _sorted_blank_last(values: set[str]) -> list[str]:
        ordered = sorted(v for v in values if v != PMAJ_BLANK_LABEL)
        if PMAJ_BLANK_LABEL in values:
            ordered.append(PMAJ_BLANK_LABEL)
        return ordered

    return {
        "portfolio_majors": _sorted_blank_last(pmajs),
        "supply_formats": _sorted_blank_last(sfmts),
    }


def _mom_detail_columns() -> list[str]:
    """The unified item-level detail column order (tracker + shipments)."""
    return [
        "item_key", "item_desc", "customer_name",
        "__pmaj", "__sfmt", "__forecast", "month", "pounds",
    ]


def _empty_mom_result() -> DemandMomResult:
    """A fully-shaped zero-row result so the page can bail without guards."""
    empty = pd.DataFrame()
    return DemandMomResult(
        pivot=pd.DataFrame(),
        month_columns=(),
        actual_month_columns=(),
        forecast_month_columns=(),
        actual_totals=empty,
        base_plan_totals=empty,
        r_and_o_totals=empty,
        budget_totals=empty,
        budget_by_month={},
        budget_total_m=0.0,
        has_pivot_budget_data=False,
        has_budget_data=False,
        chart_long=pd.DataFrame(columns=["Month", "Forecast Type", "Pounds_M"]),
        item_detail=pd.DataFrame(),
        not_captured_items=pd.DataFrame(columns=list(_NC_COLUMNS)),
        has_actuals=False,
    )


def _build_mom_not_captured(
    trk_forecast_window: pd.DataFrame,
    captured_keys: set[str],
    filters: DemandMomFilters,
) -> pd.DataFrame:
    """Return the "in tracker but not captured in the MOM Summary" log.

    Reference set = every distinct Item the tracker carries for the
    selected ``Cycle`` inside the forecast window.  An item is *captured*
    when it contributes non-zero forecast pounds to a real (non-``(blank)``)
    Portfolio Major row that survives the active filters — i.e. it's
    visible in the rendered pivot.  Everything else is reported here with a
    reason, most-actionable (missing PDH mapping) first, so the planner can
    reconcile the pivot back to the tracker at a glance.
    """
    if trk_forecast_window.empty:
        return pd.DataFrame(columns=list(_NC_COLUMNS))

    # One reference row per item: its dims (first seen) + total window pounds.
    per_item = (
        trk_forecast_window.groupby("item_key", as_index=False)
        .agg(
            item_desc=("item_desc", "first"),
            pmaj=("__pmaj", "first"),
            sfmt=("__sfmt", "first"),
            pounds_m=("pounds", lambda s: float(s.sum()) / _MOM_LBS_PER_MILLION),
        )
    )

    pmaj_filter = set(filters.portfolio_majors) if filters.portfolio_majors else None
    sfmt_filter = set(filters.supply_formats) if filters.supply_formats else None

    def _reason(row) -> Optional[str]:
        if row["item_key"] in captured_keys:
            return None  # Captured — not a miss.
        if row["pmaj"] == PMAJ_BLANK_LABEL or row["sfmt"] == PMAJ_BLANK_LABEL:
            return _NC_REASON_NO_MAPPING
        if pmaj_filter is not None and row["pmaj"] not in pmaj_filter:
            return _NC_REASON_FILTERED
        if sfmt_filter is not None and row["sfmt"] not in sfmt_filter:
            return _NC_REASON_FILTERED
        if abs(row["pounds_m"]) <= _MOM_ZERO_TOL_M:
            return _NC_REASON_ZERO
        # In-window, mapped, passes filters, non-zero — but still absent.
        # Should be rare; surface it rather than hide it.
        return _NC_REASON_ZERO

    per_item["__reason"] = per_item.apply(_reason, axis=1)
    missed = per_item.loc[per_item["__reason"].notna()].copy()
    if missed.empty:
        return pd.DataFrame(columns=list(_NC_COLUMNS))

    out = missed.rename(columns={
        "item_key": NC_COL_ITEM,
        "item_desc": NC_COL_DESC,
        "pmaj": NC_COL_PMAJ,
        "sfmt": NC_COL_SFMT,
        "__reason": NC_COL_REASON,
    })
    # Group by reason (data-quality first), then item, for a stable read.
    reason_rank = {
        _NC_REASON_NO_MAPPING: 0, _NC_REASON_FILTERED: 1, _NC_REASON_ZERO: 2,
    }
    out["__rank"] = out[NC_COL_REASON].map(reason_rank).fillna(9)
    out = out.sort_values(["__rank", NC_COL_ITEM]).reset_index(drop=True)
    return out[list(_NC_COLUMNS)]


def build_demand_mom_pivot(
    tracker_df: Optional[pd.DataFrame],
    ibp_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    filters: DemandMomFilters,
    *,
    item_master_df: Optional[pd.DataFrame] = None,
    budget_lookup: Optional[BudgetLookup] = None,
    monthly_budget: Optional[MonthlyBudgetLookup] = None,
) -> DemandMomResult:
    """Build the stitched actual-plus-forecast Demand MOM Summary.

    Parameters
    ----------
    tracker_df
        Raw ``qry_mgmt_plan_history_tracker.csv`` (the forecast source).
    ibp_df
        A slice of ``dbo.IBP Shipments`` (the actuals source) — ideally
        already projected to the actual window by the caller.
    pdh_df
        ``qry_pdh.csv`` — primary source of Portfolio Major / Supply
        Format per item (the tracker + shipments carry neither).
    filters
        The :class:`DemandMomFilters` selection.
    item_master_df
        Optional ``RO_Item_Master.csv`` — the **fallback** dimension tier.
        Items PDH can't classify (or left blank) are recovered from here
        via :func:`build_item_dim_frame_cascade`, so they stop collapsing
        into the ``(blank)`` bucket / the not-captured log.  ``None``
        preserves the PDH-only behaviour.
    budget_lookup / monthly_budget
        Same optional budget inputs as the classic pivot — the annual
        leaf budget feeds the ``Total Budget`` column (forecast branches
        only), the monthly budget the footer row + chart reference line.

    Returns
    -------
    :class:`DemandMomResult`
    """
    annual = budget_lookup if budget_lookup is not None else BudgetLookup(
        by_leaf={}, has_data=False,
    )
    monthly = monthly_budget if monthly_budget is not None else MonthlyBudgetLookup(
        by_month={}, has_data=False,
    )

    # ── Enrich both sources with dims (PDH primary, RO_Item_Master
    #    fallback) — one shared, cascaded dim frame ──────────────────────
    dim_frame = build_item_dim_frame_cascade(pdh_df, item_master_df)
    trk = _enrich_tracker(tracker_df, dim_frame)
    ibp = _enrich_ibp(ibp_df, dim_frame)

    # ── Forecast side (tracker, selected cycle, forecast window) ────────
    if not trk.empty:
        trk = trk.assign(
            __pmaj=trk["pmaj"].map(_norm_dim),
            __sfmt=trk["sfmt"].map(_norm_dim),
            # Collapse the tracker's Forecast Type into the two canonical
            # buckets (unknown/blank → Base Plan, matching the classic pivot).
            __forecast=trk["forecast_type"].eq(FORECAST_R_AND_O).map(
                {True: FORECAST_R_AND_O, False: FORECAST_BASE_PLAN}
            ),
            customer_name="",  # tracker rows carry a party site, not a customer
        )
        trk_cycle = trk.loc[trk["cycle"] == filters.cycle]
        fc_mask = (
            (trk_cycle["month"] >= filters.forecast_start)
            & (trk_cycle["month"] <= filters.forecast_end)
        )
        trk_forecast = trk_cycle.loc[fc_mask]
    else:
        trk_cycle = trk
        trk_forecast = trk

    # ── Actual side (IBP Shipments, actual window) ──────────────────────
    if not ibp.empty:
        ibp = ibp.assign(
            __pmaj=ibp["pmaj"].map(_norm_dim),
            __sfmt=ibp["sfmt"].map(_norm_dim),
            __forecast=SERIES_ACTUAL,
        )
        act_mask = (
            (ibp["month"] >= filters.actual_start)
            & (ibp["month"] <= filters.actual_end)
        )
        ibp_actual = ibp.loc[act_mask]
    else:
        ibp_actual = ibp

    # ── Unified item-level detail (pre user PMaj/SFmt filter) ───────────
    cols = _mom_detail_columns()
    parts = [
        p[cols] for p in (trk_forecast, ibp_actual)
        if p is not None and not p.empty
    ]
    detail_all = (
        pd.concat(parts, ignore_index=True) if parts
        else pd.DataFrame(columns=cols)
    )

    # Apply the user's Portfolio Major / Supply Format whitelists (AND).
    detail = detail_all
    if filters.portfolio_majors:
        detail = detail.loc[detail["__pmaj"].isin(filters.portfolio_majors)]
    if filters.supply_formats:
        detail = detail.loc[detail["__sfmt"].isin(filters.supply_formats)]

    if detail.empty:
        return _empty_mom_result()

    # ── Long frame for the shared assembler ─────────────────────────────
    long_df = detail.rename(columns={"month": "__month"}).assign(
        __lbs_m=lambda d: d["pounds"] / _MOM_LBS_PER_MILLION,
    )

    wide, month_col_labels = build_month_wide(long_df)
    assembled = assemble_hierarchical_pivot(
        wide, month_col_labels, annual, MOM_FORECAST_ORDER,
        budgeted_forecasts=_MOM_BUDGETED_FORECASTS,
    )

    # ── Footer subtotals (Actual / Base Plan / R&O) ─────────────────────
    grouped = forecast_month_grouped(long_df)
    footer_wide = footer_wide_from_grouped(grouped, MOM_FORECAST_ORDER)
    grand_base_m = assembled.grand_budget_by_forecast.get(FORECAST_BASE_PLAN, 0.0)
    grand_ro_m = assembled.grand_budget_by_forecast.get(FORECAST_R_AND_O, 0.0)
    include_budget_col = annual.has_data or monthly.has_data

    actual_totals = footer_row_frame(
        "Total Actual (Shipments)", footer_wide.loc[SERIES_ACTUAL],
        month_col_labels, include_budget_col=include_budget_col,
        budget_col_value=float("nan"),  # actuals carry no budget
    )
    base_plan_totals = footer_row_frame(
        "Total Base Plan", footer_wide.loc[FORECAST_BASE_PLAN],
        month_col_labels, include_budget_col=include_budget_col,
        budget_col_value=(
            round(float(grand_base_m), 1) if annual.has_data else float("nan")
        ),
    )
    r_and_o_totals = footer_row_frame(
        "Total R&O", footer_wide.loc[FORECAST_R_AND_O],
        month_col_labels, include_budget_col=include_budget_col,
        budget_col_value=(
            round(float(grand_ro_m), 1) if annual.has_data else float("nan")
        ),
    )
    budget_totals, budget_by_month, budget_total_m = monthly_budget_footer(
        monthly, month_col_labels,
    )

    chart_long = chart_long_from_grouped(grouped, MOM_FORECAST_ORDER)

    # ── Classify each visible month column as actual vs forecast ────────
    actual_months = {
        _format_month_label(m)
        for m in _months_in_range(filters.actual_start, filters.actual_end)
    }
    forecast_months = {
        _format_month_label(m)
        for m in _months_in_range(filters.forecast_start, filters.forecast_end)
    }
    actual_month_columns = tuple(
        c for c in month_col_labels if c in actual_months
    )
    forecast_month_columns = tuple(
        c for c in month_col_labels if c in forecast_months
    )

    # ── Item-level detail for the drill-down ────────────────────────────
    item_detail = (
        long_df.assign(__month_label=long_df["__month"].map(_format_month_label))
        .groupby(
            ["__pmaj", "__forecast", "__sfmt", "item_key", "item_desc",
             "__month_label"],
            as_index=False,
        )["__lbs_m"].sum()
    )

    # ── "Not captured" reconciliation log ───────────────────────────────
    # Captured = contributes non-zero forecast pounds to a real (non-blank)
    # Portfolio Major that survived the filters (i.e. drawn in the pivot).
    forecast_detail = detail.loc[detail["__forecast"] != SERIES_ACTUAL]
    captured_keys: set[str] = set()
    if not forecast_detail.empty:
        real = forecast_detail.loc[forecast_detail["__pmaj"] != PMAJ_BLANK_LABEL]
        if not real.empty:
            per_item_m = (
                real.groupby("item_key")["pounds"].sum() / _MOM_LBS_PER_MILLION
            )
            captured_keys = set(
                per_item_m.loc[per_item_m.abs() > _MOM_ZERO_TOL_M].index
            )
    not_captured_items = _build_mom_not_captured(
        trk_forecast, captured_keys, filters,
    )

    return DemandMomResult(
        pivot=assembled.pivot,
        month_columns=month_col_labels,
        actual_month_columns=actual_month_columns,
        forecast_month_columns=forecast_month_columns,
        actual_totals=actual_totals,
        base_plan_totals=base_plan_totals,
        r_and_o_totals=r_and_o_totals,
        budget_totals=budget_totals,
        budget_by_month={
            k: round(float(v), 1)
            for k, v in budget_by_month.items() if pd.notna(v)
        },
        budget_total_m=float(budget_total_m),
        has_pivot_budget_data=bool(
            annual.has_data and assembled.grand_budget_total_m > 0
        ),
        has_budget_data=bool(monthly.has_data and budget_total_m > 0),
        chart_long=chart_long,
        item_detail=item_detail,
        not_captured_items=not_captured_items,
        has_actuals=not ibp_actual.empty if ibp_actual is not None else False,
    )


def list_driver_buckets_for_group(
    buckets: pd.DataFrame,
    pmaj: str,
    sfmt: str,
    brand: str,
) -> list[str]:
    """Return driver bucket labels for one (PMaj, SFmt, Brand) group.

    Sorted by absolute net delta descending so the drill-down selector
    mirrors the ranking logic in :func:`_build_driver_table`.
    """
    if buckets is None or buckets.empty:
        return []
    work = buckets.copy()
    for c in ("pmaj", "sfmt", "brand"):
        work[c] = work[c].map(_normalise_driver_group_value)
    mask = (
        work["pmaj"].eq(_normalise_driver_group_value(pmaj))
        & work["sfmt"].eq(_normalise_driver_group_value(sfmt))
        & work["brand"].eq(_normalise_driver_group_value(brand))
    )
    sub = work.loc[mask]
    if sub.empty:
        return []
    ranked = (
        sub.groupby("bucket_label", dropna=False)["delta"]
        .sum().reset_index()
        .assign(_abs=lambda d: d["delta"].abs())
        .sort_values(by=["_abs", "bucket_label"], ascending=[False, True],
                     kind="mergesort")
    )
    return [str(r.bucket_label) for r in ranked.itertuples()]


def compute_demand_driver_items(
    buckets: pd.DataFrame,
    pmaj: str,
    sfmt: str,
    brand: str,
    bucket_label: str,
) -> pd.DataFrame:
    """Return item-level rows composing one driver bucket (drill-down).

    Pure function — no I/O, no Streamlit.  Output sorted by ``|Δ|`` desc.
    """
    output_cols = [
        DRV_ITEM_COL_ITEM, DRV_ITEM_COL_DESC, DRV_ITEM_COL_BRAND,
        DRV_ITEM_COL_CUSTOMER, DRV_ITEM_COL_CUSTOMER_ID, DRV_ITEM_COL_DELTA,
    ]
    if buckets is None or buckets.empty:
        return pd.DataFrame(columns=output_cols)

    work = _prepare_driver_bucket_frame(buckets)
    mask = (
        work["pmaj"].eq(_normalise_driver_group_value(pmaj))
        & work["sfmt"].eq(_normalise_driver_group_value(sfmt))
        & work["brand"].eq(_normalise_driver_group_value(brand))
        & work["bucket_label"].eq(str(bucket_label))
    )
    filtered = work.loc[mask]
    if filtered.empty:
        return pd.DataFrame(columns=output_cols)

    filtered = filtered.assign(
        _abs=filtered["delta"].abs(),
    ).sort_values(
        by=["_abs", "item_key"],
        ascending=[False, True],
        kind="mergesort",
    )
    out = pd.DataFrame({
        DRV_ITEM_COL_ITEM: filtered["item_key"],
        DRV_ITEM_COL_DESC: filtered["item_desc"],
        DRV_ITEM_COL_BRAND: filtered["brand"],
        DRV_ITEM_COL_CUSTOMER: filtered["customer_name"],
        DRV_ITEM_COL_CUSTOMER_ID: filtered["customer_id"],
        DRV_ITEM_COL_DELTA: (
            filtered["delta"] / _LBS_PER_MILLION
        ).round(_MILLIONS_DISPLAY_DECIMALS),
    })
    return out[output_cols].reset_index(drop=True)


def _party_site_lookup(
    dim_df: Optional[pd.DataFrame], value_candidates: tuple[str, ...],
) -> dict[str, str]:
    """Return ``{normalised party_site -> value}`` from the dim table.

    *value_candidates* selects which dim column to map to (account
    description or customer number).  Keys are normalised with
    :func:`_normalise_item_key` so a numeric/text mismatch between the
    tracker's Party Site Number and the dim's ``party_site_code`` never
    silently drops a join.  Last row wins on duplicate party sites.
    """
    lookup: dict[str, str] = {}
    if dim_df is None or dim_df.empty:
        return lookup
    ps_col = _resolve_column(dim_df, _DIM_PARTY_SITE_CANDIDATES)
    val_col = _resolve_column(dim_df, value_candidates)
    if not ps_col or not val_col:
        logger.info(
            "dp_dimshiptosites missing a column (party_site=%r, value=%r); "
            "driver customer names will be blank.", ps_col, val_col,
        )
        return lookup
    for ps, val in zip(dim_df[ps_col], dim_df[val_col]):
        key = _normalise_item_key(ps)
        if key:
            lookup[key] = _clean_str(val)
    return lookup


def _customer_num_to_name(ibp_enriched: pd.DataFrame) -> dict[str, str]:
    """Return ``{normalised customer_no -> Customer Name}`` from enriched IBP."""
    lookup: dict[str, str] = {}
    if ibp_enriched is None or ibp_enriched.empty:
        return lookup
    if "customer_no" not in ibp_enriched.columns:
        return lookup
    for no, name in zip(ibp_enriched["customer_no"], ibp_enriched["customer_name"]):
        key = str(no).strip()
        if key:
            lookup[key] = str(name).strip()
    return lookup


def build_base_plan_driver_table(
    tracker_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    dim_df: Optional[pd.DataFrame],
    filters: ComparisonFilters,
    *,
    enriched: Optional[EnrichedSources] = None,
) -> DriverTableResult:
    """Return the Base Plan driver table + item-level bucket detail.

    Value + driver magnitude = **Base Plan delta** = current-cycle minus
    prior-cycle ``Base Plan`` pounds over the forecast months (the same
    quantity as the comparison's *Base Plan* column).  Driver buckets are
    ``(account_description – Party Site Number)`` — customer name resolved
    via ``Party Site Number → party_site_code``.

    Pass *enriched* to share PDH enrichment with the comparison and the
    PM Actual driver builder (cuts the dominant cold-build cost).
    """
    if enriched is None:
        enriched = build_enriched_sources(tracker_df, None, None, pdh_df)
    trk = enriched.tracker
    if trk.empty:
        return _empty_driver_result(DRV_BASE_PLAN_VALUE)

    forecast_months = _months_in_range(filters.forecast_start, filters.forecast_end)
    sub = trk.loc[
        (trk["forecast_type"] == FORECAST_BASE_PLAN)
        & (trk["month"].isin(forecast_months))
        & (trk["cycle"].isin([filters.current_cycle, filters.prior_cycle]))
    ].copy()
    if sub.empty:
        return _empty_driver_result(DRV_BASE_PLAN_VALUE)

    # Signed pounds: + for current cycle, − for prior cycle → delta.
    sub["delta"] = sub["pounds"].where(
        sub["cycle"] == filters.current_cycle, -sub["pounds"])

    ps_to_acct = _party_site_lookup(dim_df, _DIM_ACCOUNT_DESC_CANDIDATES)
    cust = _vectorised_item_key(sub["party_site"]).map(
        lambda k: ps_to_acct.get(k, ""))
    party_site = sub["party_site"].map(_clean_str)
    sub["customer_name"] = cust.map(_clean_str).replace({"": _DRV_BLANK})
    sub["customer_id"] = party_site.replace({"": _DRV_BLANK})
    sub["bucket_label"] = [
        _join_label([c, ps])
        for c, ps in zip(sub["customer_name"], sub["customer_id"])
    ]
    bucket_cols = [
        "pmaj", "sfmt", "brand", "pminor", "bucket_label",
        "item_key", "item_desc", "customer_name", "customer_id", "delta",
    ]
    sub["item_key"] = sub["item_key"].map(_clean_str)
    return _assemble_driver_result(sub[bucket_cols], DRV_BASE_PLAN_VALUE)


def build_pm_actual_driver_table(
    tracker_df: Optional[pd.DataFrame],
    ibp_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    dim_df: Optional[pd.DataFrame],
    filters: ComparisonFilters,
    *,
    enriched: Optional[EnrichedSources] = None,
) -> DriverTableResult:
    """Return the PM Actual driver table + item-level bucket detail.

    Value + driver magnitude = **PM Actual delta** = prior-month IBP
    actual minus prior-month PRIOR-cycle (Base + R&O) tracker forecast
    (the same quantity as the comparison's *PM Actual* column).  Driver
    buckets are ``(Customer Name – Customer No)``: the actual side keys on
    IBP's own ``Customer Name`` / ``Customer No``; the forecast side maps
    ``Party Site Number → customer_num`` so both sides net within the
    same bucket.

    Pass *enriched* to share PDH enrichment with the comparison and the
    Base Plan driver builder (cuts the dominant cold-build cost).
    """
    if enriched is None:
        enriched = build_enriched_sources(tracker_df, ibp_df, None, pdh_df)
    trk = enriched.tracker
    ibp = enriched.ibp
    prior_month = filters.prior_month.replace(day=1)

    parts: list[pd.DataFrame] = []
    bucket_cols = [
        "pmaj", "sfmt", "brand", "pminor", "bucket_label",
        "item_key", "item_desc", "customer_name", "customer_id", "delta",
    ]

    # ── Actual side (IBP, prior month): +pounds ──────────────────────
    if not ibp.empty:
        act = ibp.loc[ibp["month"] == prior_month].copy()
        if not act.empty:
            act["delta"] = act["pounds"]
            act["customer_name"] = act["customer_name"].map(_clean_str).replace({"": _DRV_BLANK})
            act["customer_id"] = act["customer_no"].map(_clean_str).replace({"": _DRV_BLANK})
            act["customer_name"] = act["customer_name"].where(
                act["customer_name"].ne(_DRV_BLANK), act["customer_id"],
            )
            act["bucket_label"] = [
                _join_label([c, cid])
                for c, cid in zip(act["customer_name"], act["customer_id"])
            ]
            act["item_key"] = act["item_key"].map(_clean_str)
            parts.append(act[bucket_cols])

    # ── Forecast side (tracker, PRIOR cycle, Base+R&O, prior month): −pounds ─
    #    Prior cycle (not current): PM Actual measures actuals vs. the
    #    forecast that was live before the month closed (see the comparison's
    #    prior_month_forecast in _compute_leaf_measures).
    if not trk.empty:
        fc = trk.loc[
            (trk["cycle"] == filters.prior_cycle)
            & (trk["forecast_type"].isin((FORECAST_BASE_PLAN, FORECAST_R_AND_O)))
            & (trk["month"] == prior_month)
        ].copy()
        if not fc.empty:
            fc["delta"] = -fc["pounds"]
            ps_to_num = _party_site_lookup(dim_df, _DIM_CUSTOMER_NUM_CANDIDATES)
            num_to_name = _customer_num_to_name(ibp)
            cust_num = _vectorised_item_key(fc["party_site"]).map(
                lambda k: ps_to_num.get(k, ""))
            fc["customer_id"] = cust_num.map(_clean_str).replace({"": _DRV_BLANK})
            fc["customer_name"] = fc["customer_id"].map(
                lambda n: num_to_name.get(n, n) if n else _DRV_BLANK,
            )
            fc["bucket_label"] = [
                _join_label([c, cid])
                for c, cid in zip(fc["customer_name"], fc["customer_id"])
            ]
            fc["item_key"] = fc["item_key"].map(_clean_str)
            parts.append(fc[bucket_cols])

    if not parts:
        return _empty_driver_result(DRV_PM_ACTUAL_VALUE)
    return _assemble_driver_result(pd.concat(parts, ignore_index=True), DRV_PM_ACTUAL_VALUE)


def driver_table_to_csv_bytes(table: pd.DataFrame) -> bytes:
    """Serialise a driver table to UTF-8 CSV bytes for download."""
    if table is None or table.empty:
        return b""
    return table.to_csv(index=False).encode("utf-8")


def comparison_to_csv_bytes(result: ComparisonResult) -> bytes:
    """Serialise the comparison table to UTF-8 CSV bytes for download.

    Drops the internal metadata columns so the downloaded file matches
    exactly what the planner sees on screen (indented label + metrics).
    """
    if result.table is None or result.table.empty:
        return b""
    drop = [c for c in _META_COLS if c in result.table.columns]
    out = result.table.drop(columns=drop)
    return out.to_csv(index=False).encode("utf-8")


__all__ = [
    "DemandPlanComparisonError",
    "ComparisonFilters",
    "ComparisonResult",
    "COMPARISON_TEMPLATE",
    "TemplateRow",
    "TEMPLATE_BY_ID",
    "DISPLAY_LABELS",
    "DISPLAY_ORDER",
    "COLS_HIDDEN_BY_DEFAULT",
    "PERCENT_COLS",
    "fy27_budget_blob_path",
    "fetch_fy27_budget_by_row_id",
    "load_fy27_budget_by_row_id",
    "parse_fy27_budget_workbook",
    "budget_by_row_id_from_workbook",
    "Fy27BudgetLoadResult",
    "COL_LABEL",
    "list_tracker_cycles",
    "list_tracker_months",
    "validate_filters",
    "fetch_ro_summary_total_delta_by_path",
    "months_in_range",
    "shift_year_back",
    "last_n_months",
    "enrich_ibp_orders_df",
    "resolve_ro_summary_path",
    "build_enriched_sources",
    "EnrichedSources",
    "build_item_dim_frame",
    "build_item_dim_frame_cascade",
    "build_demand_plan_comparison",
    "build_comparison_kpis",
    "ComparisonKpis",
    "tracker_has_dim_columns",
    "build_prior_month_actual_vs_fcst_table",
    "build_prior_month_shipment_diagnostic",
    "DIAG_UNMAPPED",
    "DIAG_COL_PMAJ",
    "DIAG_COL_SFMT",
    "DIAG_COL_LBS",
    "DIAG_COL_MLBS",
    "comparison_to_csv_bytes",
    "PMAF_COL_PRIOR_PLAN",
    "PMAF_COL_ORDERED",
    "PMAF_COL_SHIPPED",
    "PMAF_COL_ORDERED_DIFF",
    "PMAF_COL_SHIPPED_DIFF",
    "PMAF_COL_ORDERED_PCT",
    "PMAF_COL_SHIPPED_PCT",
    "build_base_plan_driver_table",
    "build_pm_actual_driver_table",
    "driver_table_to_csv_bytes",
    "DriverTableResult",
    "compute_demand_driver_items",
    "list_driver_buckets_for_group",
    "DRV_COL_PMAJ",
    "DRV_COL_SFMT",
    "DRV_COL_BRAND",
    "DRV_BASE_PLAN_VALUE",
    "DRV_PM_ACTUAL_VALUE",
    "DRV_DRIVER_COLS",
    "DRV_ITEM_COL_ITEM",
    "DRV_ITEM_COL_DESC",
    "DRV_ITEM_COL_BRAND",
    "DRV_ITEM_COL_CUSTOMER",
    "DRV_ITEM_COL_CUSTOMER_ID",
    "DRV_ITEM_COL_DELTA",
]
