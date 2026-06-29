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
_RO_SUMMARY_CURRENT_PLAN_CANDIDATES: tuple[str, ...] = (
    "FY27 Probabilized | Current Plan",
    "FY27 Probabilized|Current Plan",
    "FY27 Probabilized  | Current Plan",
)
# Non-breaking space used by the RO Summary exporter to indent the tree.
_NBSP: str = "\u00A0"


# ── Brand derivation ─────────────────────────────────────────────────────────

BRAND_BRANDED: str = "Branded"
BRAND_PRIVATE: str = "Private"


def derive_brand(item_description: object) -> str:
    """Return ``"Branded"`` / ``"Private"`` from a PDH item description.

    Planner rule: look at the **first two characters** of the PDH
    ``Item Description``.  ``"DG"`` (case-insensitive) → *Branded*
    (Darigold's own brand); anything else → *Private* (private label /
    co-pack).  Blank / missing descriptions fall to *Private* — the
    conservative default (a Darigold-branded SKU would carry the ``DG``
    prefix, so an unlabelled row is treated as not-branded).
    """
    if item_description is None:
        return BRAND_PRIVATE
    try:
        if pd.isna(item_description):
            return BRAND_PRIVATE
    except (TypeError, ValueError):
        pass
    text = str(item_description).strip()
    if text[:2].upper() == "DG":
        return BRAND_BRANDED
    return BRAND_PRIVATE


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
COL_CURRENT_PLAN_FORECAST: str  = "current_plan_forecast"
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

# Ratio columns — NOT additive; computed from each row's own derived
# values after roll-up.
COL_TOTAL_DELTA_PCT: str        = "total_delta_pct"
COL_PCT: str                    = "pct"

# The set of measures summed during subtotal roll-up.
_ADDITIVE_COLS: tuple[str, ...] = (
    COL_TOTAL_ACTUALS, COL_PRIOR_MONTH_ACTUAL, COL_PRIOR_MONTH_FORECAST,
    COL_CURRENT_PLAN_ACTUAL, COL_CURRENT_PLAN_FORECAST,
    COL_BASE_PLAN, COL_R_AND_O, COL_BUDGET,
)

# Internal structural columns kept alongside the metrics for the page.
COL_LABEL: str       = "Millions of lbs."
COL_ROW_ID: str      = "_row_id"
COL_INDENT: str      = "_indent"
COL_IS_SUBTOTAL: str = "_is_subtotal"
COL_IS_MEMO: str     = "_is_memo"
_META_COLS: tuple[str, ...] = (COL_ROW_ID, COL_INDENT, COL_IS_SUBTOTAL, COL_IS_MEMO)

# Display order + labels (left → right, mirroring screenshot 1).
DISPLAY_LABELS: dict[str, str] = {
    COL_TOTAL_ACTUALS:         "Total Actuals",
    COL_PRIOR_MONTH_ACTUAL:    "Prior Month Actual",
    COL_PRIOR_MONTH_FORECAST:  "Prior Month Forecast",
    COL_CURRENT_PLAN_ACTUAL:   "Current Plan (Actual)",
    COL_CURRENT_PLAN_FORECAST: "Current Plan (Forecast)",
    COL_LAST_PLAN:             "Last Plan",
    COL_CURRENT_PLAN:          "Current Plan",
    COL_PM_ACTUAL:             "PM Actual",
    COL_TOTAL_DELTA:           "Total Delta",
    COL_TOTAL_DELTA_PCT:       "Total Delta %",
    COL_BASE_PLAN:             "Base Plan",
    COL_R_AND_O:               "R&O",
    COL_V_BUDGET:              "v. Budget",
    COL_PCT:                   "%",
    COL_BUDGET:                "Budget",
}
# Left → right order of the metric columns in the rendered table.
DISPLAY_ORDER: tuple[str, ...] = (
    COL_TOTAL_ACTUALS, COL_PRIOR_MONTH_ACTUAL, COL_PRIOR_MONTH_FORECAST,
    COL_CURRENT_PLAN_ACTUAL, COL_CURRENT_PLAN_FORECAST, COL_LAST_PLAN,
    COL_CURRENT_PLAN, COL_PM_ACTUAL, COL_TOTAL_DELTA, COL_TOTAL_DELTA_PCT,
    COL_BASE_PLAN, COL_R_AND_O, COL_V_BUDGET, COL_PCT, COL_BUDGET,
)
# Columns rendered as percentages (the rest are millions of lbs).
PERCENT_COLS: frozenset = frozenset({COL_TOTAL_DELTA_PCT, COL_PCT})

# Metric columns hidden by default in the Streamlit table — planners can
# expand them via a checkbox on the page.  Spans Total Actuals through
# Current Plan (Forecast), inclusive.
COLS_HIDDEN_BY_DEFAULT: tuple[str, ...] = (
    COL_TOTAL_ACTUALS,
    COL_PRIOR_MONTH_ACTUAL,
    COL_PRIOR_MONTH_FORECAST,
    COL_CURRENT_PLAN_ACTUAL,
    COL_CURRENT_PLAN_FORECAST,
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
        Cycle labels from the tracker's ``Cycle`` column (e.g. ``"C3"``
        / ``"C2"``).
    actual_start / actual_end
        Inclusive month bounds (first-of-month dates) for the **actual**
        window (IBP shipments + current-cycle "actual" plan).
    forecast_start / forecast_end
        Inclusive month bounds for the **forecast** window.  Must not
        overlap the actual window.
    prior_month
        The single month treated as "Prior Month" for the PM Actual /
        Prior Month Forecast columns.
    """
    current_cycle: str
    prior_cycle: str
    actual_start: date
    actual_end: date
    forecast_start: date
    forecast_end: date
    prior_month: date


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

@dataclass(frozen=True)
class _ItemDims:
    """Per-item dimensions resolved from PDH (the join payload)."""
    pmaj: str
    sfmt: str
    pminor: str
    brand: str
    desc: str = ""


def build_item_dim_lookup(pdh_df: Optional[pd.DataFrame]) -> dict[str, _ItemDims]:
    """Return ``{normalised_item_key -> _ItemDims}`` from PDH.

    Kept for backward compatibility with any caller that needs a plain
    dict; the comparison + driver builders themselves use the faster
    DataFrame-shaped lookup returned by :func:`build_item_dim_frame`.
    """
    frame = build_item_dim_frame(pdh_df)
    if frame.empty:
        return {}
    return {
        row["__item_key"]: _ItemDims(
            pmaj=row["pmaj"], sfmt=row["sfmt"], pminor=row["pminor"],
            brand=row["brand"], desc=row["desc"],
        )
        for row in frame.to_dict(orient="records")
    }


def build_item_dim_frame(pdh_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Return a vectorised PDH lookup as a DataFrame.

    Columns: ``__item_key, pmaj, sfmt, pminor, brand, desc``.  The
    ``__item_key`` column is the canonical join key produced by
    :func:`_vectorised_item_key` so it matches the same coercion used on
    the tracker / IBP sides.  Last row wins on duplicate items (matches
    the legacy ``build_item_dim_lookup`` contract).

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
    """Vectorised analogue of :func:`derive_brand` (first 2 chars ``DG`` → Branded)."""
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
    # 1. Numeric/serial fast path.  Restrict to the representable serial
    #    window first (``between`` yields False for NaN and ±inf, so those
    #    are nulled too) — this turns garbage/overflowing cells into NaT
    #    rather than letting them overflow inside pandas' day→ns scaling.
    as_num = pd.to_numeric(s, errors="coerce")
    as_num = as_num.where(as_num.between(_SERIAL_DAY_MIN, _SERIAL_DAY_MAX))
    serials_ts = pd.to_datetime(
        as_num, unit="D", origin=_SERIAL_DAY_ORIGIN, errors="coerce")
    # 2. String fallback for survivors.
    needs_str = serials_ts.isna()
    if needs_str.any():
        str_ts = pd.to_datetime(s[needs_str], errors="coerce")
        serials_ts = serials_ts.copy()
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
    Rows with an unparseable month are dropped.

    Accepts either the new vectorised dim frame (preferred) or the
    legacy ``dict`` lookup (back-compat for external callers).
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

    Accepts either the new vectorised dim frame (preferred) or the
    legacy ``dict`` lookup (back-compat for external callers).
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
    """Accept either a legacy ``dict[str, _ItemDims]`` or a dim frame.

    Lets the enrichment helpers stay back-compatible with any caller
    that still passes :func:`build_item_dim_lookup`'s dict — we lift it
    into the new frame shape on the fly.  No-op when already a frame.
    """
    if dims_or_frame is None:
        return pd.DataFrame(columns=["__item_key", "pmaj", "sfmt", "pminor", "brand", "desc"])
    if isinstance(dims_or_frame, pd.DataFrame):
        return dims_or_frame
    if isinstance(dims_or_frame, dict):
        if not dims_or_frame:
            return pd.DataFrame(
                columns=["__item_key", "pmaj", "sfmt", "pminor", "brand", "desc"])
        return pd.DataFrame.from_records([
            {
                "__item_key": k, "pmaj": d.pmaj, "sfmt": d.sfmt,
                "pminor": d.pminor, "brand": d.brand, "desc": d.desc,
            }
            for k, d in dims_or_frame.items()
        ])
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


def fetch_ro_summary_current_plan_by_path() -> dict[tuple[str, ...], float]:
    """Return ``{label_path -> FY27 Current Plan}`` from the saved RO Summary.

    Used by Product Line Review for the **R&O FY** column (millions of
    lbs, already on the report's display scale).
    """
    return _fetch_ro_summary_metric_by_path(_RO_SUMMARY_CURRENT_PLAN_CANDIDATES)


def months_in_range(start: date, end: date) -> set[date]:
    """Inclusive first-of-month set from *start* through *end*."""
    return _months_in_range(start, end)


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


def _indent_depth(indented_label: str) -> int:
    """Return the tree depth encoded in an RO-Summary indented label.

    The RO Summary exporter prefixes ``"\\u00A0\\u00A0" * indent`` (two
    NBSPs per level).  We count leading NBSPs and divide by two.
    """
    leading = len(indented_label) - len(indented_label.lstrip(_NBSP))
    return leading // 2


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


def build_enriched_sources(
    tracker_df: pd.DataFrame,
    ibp_df: Optional[pd.DataFrame],
    ibp_orders_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
) -> EnrichedSources:
    """Build the shared enrichment bundle exactly once.

    Performs the PDH dim frame build + vectorised tracker and IBP
    enrichment in a single pass, so all three downstream builders
    (comparison + PM Actual drivers + Base Plan drivers) can reuse the
    output without redoing the work.
    """
    dim_frame = build_item_dim_frame(pdh_df)
    pdh_warning: Optional[str] = None
    if dim_frame.empty:
        pdh_warning = (
            "PDH (qry_pdh.csv) was empty or missing its Item No column — "
            "Portfolio Major / Supply Format / Brand could not be resolved, "
            "so every row is zero.  Check the upstream PDH export."
        )
    trk = _enrich_tracker(tracker_df, dim_frame) if tracker_df is not None else _empty_enriched()
    ibp = _enrich_ibp(ibp_df, dim_frame) if ibp_df is not None else _empty_enriched(actuals=True)
    ibp_orders = (
        _enrich_ibp(ibp_orders_df, dim_frame, qty_candidates=_IBP_ORDERED_QTY_CANDIDATES)
        if ibp_orders_df is not None
        else _empty_enriched(actuals=True)
    )
    return EnrichedSources(
        tracker=trk, ibp=ibp, ibp_orders=ibp_orders, pdh_warning=pdh_warning,
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
    ro_total_delta_by_path: Optional[dict[tuple[str, ...], float]] = None,
    enriched: Optional[EnrichedSources] = None,
    budget_by_row_id: Optional[dict[str, float]] = None,
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
        ro_total_delta_by_path=ro_total_delta_by_path,
        enriched=enriched,
        budget_by_row_id=budget_by_row_id,
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
    ro_total_delta_by_path: Optional[dict[tuple[str, ...], float]],
    enriched: Optional[EnrichedSources],
    budget_by_row_id: Optional[dict[str, float]] = None,
) -> _RuntimeBuildArtifacts:
    """Build reusable runtime artifacts for comparison-style rollups."""
    warnings: list[str] = []

    if enriched is None:
        enriched = build_enriched_sources(tracker_df, ibp_df, None, pdh_df)
    if enriched.pdh_warning:
        warnings.append(enriched.pdh_warning)
    trk = enriched.tracker
    ibp = enriched.ibp

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
    runtime_template = _build_runtime_template_for_filters(
        trk, ibp, filters,
        actual_months=actual_months,
        forecast_months=forecast_months,
        prior_month=prior_month,
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
            prior_month=prior_month,
            ro_total_delta_by_path=ro_total_delta_by_path,
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
    prior_month: date,
    ro_total_delta_by_path: dict[tuple[str, ...], float],
) -> dict[str, float]:
    """Compute the eight additive measures for a single leaf row.

    All sums are in millions of lbs.  See the module docstring's column
    table for the exact business definition of each measure.
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
    ibp_in_actual = ibp_month.isin(actual_months) if not ibp.empty else pd.Series([], dtype=bool)
    ibp_in_prior = (ibp_month == prior_month) if not ibp.empty else pd.Series([], dtype=bool)

    # Cycle + forecast-type masks on the tracker.
    if not trk.empty:
        cur_cycle = trk["cycle"] == filters.current_cycle
        prior_cycle = trk["cycle"] == filters.prior_cycle
        is_base = trk["forecast_type"] == FORECAST_BASE_PLAN
        is_base_or_ro = trk["forecast_type"].isin((FORECAST_BASE_PLAN, FORECAST_R_AND_O))
    else:
        cur_cycle = prior_cycle = is_base = is_base_or_ro = pd.Series([], dtype=bool)

    # ── Actuals (IBP Shipments — no forecast type) ───────────────────
    total_actuals = _sum_millions(ibp, ibp_mask & ibp_in_actual)
    prior_month_actual = _sum_millions(ibp, ibp_mask & ibp_in_prior)

    # ── Plan buckets (tracker, current cycle, Base + R&O) ────────────
    prior_month_forecast = _sum_millions(
        trk, trk_mask & cur_cycle & is_base_or_ro & trk_in_prior)
    current_plan_actual = _sum_millions(
        trk, trk_mask & cur_cycle & is_base_or_ro & trk_in_actual)
    current_plan_forecast = _sum_millions(
        trk, trk_mask & cur_cycle & is_base_or_ro & trk_in_forecast)

    # ── Base Plan delta (Base Plan type only, forecast months) ───────
    # current cycle base-plan forecast − prior cycle base-plan forecast.
    base_plan = (
        _sum_millions(trk, trk_mask & cur_cycle & is_base & trk_in_forecast)
        - _sum_millions(trk, trk_mask & prior_cycle & is_base & trk_in_forecast)
    )

    # ── R&O (RO Summary FY27 Total Delta, matched by label path) ─────
    r_and_o = 0.0
    if tpl.ro_summary_path is not None:
        r_and_o = float(ro_total_delta_by_path.get(tpl.ro_summary_path, 0.0))

    return {
        COL_TOTAL_ACTUALS: total_actuals,
        COL_PRIOR_MONTH_ACTUAL: prior_month_actual,
        COL_PRIOR_MONTH_FORECAST: prior_month_forecast,
        COL_CURRENT_PLAN_ACTUAL: current_plan_actual,
        COL_CURRENT_PLAN_FORECAST: current_plan_forecast,
        COL_BASE_PLAN: base_plan,
        COL_R_AND_O: r_and_o,
        COL_BUDGET: tpl.budget_m,
    }


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
    to one decimal, and applies the display column order + labels.
    """
    records: list[dict] = []
    for tpl in template:
        m = measures[tpl.row_id]

        # Derived (linear) columns.
        current_plan = m[COL_TOTAL_ACTUALS] + m[COL_CURRENT_PLAN_FORECAST]
        pm_actual = m[COL_PRIOR_MONTH_ACTUAL] - m[COL_PRIOR_MONTH_FORECAST]
        total_delta = m[COL_BASE_PLAN] + m[COL_R_AND_O] + pm_actual
        last_plan = current_plan - total_delta
        v_budget = current_plan - m[COL_BUDGET]

        # Ratio columns — guard divide-by-zero (blank when undefined).
        total_delta_pct = _safe_ratio(v_budget, current_plan)
        pct = _safe_ratio(v_budget, current_plan)

        row = {
            COL_ROW_ID: tpl.row_id,
            COL_INDENT: tpl.indent,
            COL_IS_SUBTOTAL: tpl.is_subtotal,
            COL_IS_MEMO: tpl.is_memo,
            COL_LABEL: _make_indented_label(tpl.label, tpl.indent, tpl.is_memo),
            COL_TOTAL_ACTUALS: m[COL_TOTAL_ACTUALS],
            COL_PRIOR_MONTH_ACTUAL: m[COL_PRIOR_MONTH_ACTUAL],
            COL_PRIOR_MONTH_FORECAST: m[COL_PRIOR_MONTH_FORECAST],
            COL_CURRENT_PLAN_ACTUAL: m[COL_CURRENT_PLAN_ACTUAL],
            COL_CURRENT_PLAN_FORECAST: m[COL_CURRENT_PLAN_FORECAST],
            COL_LAST_PLAN: last_plan,
            COL_CURRENT_PLAN: current_plan,
            COL_PM_ACTUAL: pm_actual,
            COL_TOTAL_DELTA: total_delta,
            COL_TOTAL_DELTA_PCT: total_delta_pct,
            COL_BASE_PLAN: m[COL_BASE_PLAN],
            COL_R_AND_O: m[COL_R_AND_O],
            COL_V_BUDGET: v_budget,
            COL_PCT: pct,
            COL_BUDGET: m[COL_BUDGET],
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

    # Final column order: metadata + label + metrics (display order),
    # renamed to the screenshot labels.
    ordered = [*_META_COLS, COL_LABEL, *DISPLAY_ORDER]
    df = df.loc[:, ordered]
    return df.rename(columns=DISPLAY_LABELS)


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


def _build_runtime_template_for_filters(
    trk: pd.DataFrame,
    ibp: pd.DataFrame,
    filters: ComparisonFilters,
    *,
    actual_months: set[date],
    forecast_months: set[date],
    prior_month: date,
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
) -> tuple[TemplateRow, ...]:
    """Build dynamic Butter detail rows for the current filter selection."""
    if trk.empty and ibp.empty:
        return ()

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
    if candidates.empty:
        return ()

    candidates["brand"] = candidates["brand"].astype(str).str.strip()
    candidates["sfmt"] = candidates["sfmt"].astype(str).str.strip()
    candidates = candidates.loc[(candidates["brand"] != "") & (candidates["sfmt"] != "")]
    if candidates.empty:
        return ()

    rows: list[TemplateRow] = []
    for brand in (BRAND_BRANDED, BRAND_PRIVATE):
        brand_formats = sorted({
            sf for sf in candidates.loc[candidates["brand"] == brand, "sfmt"].tolist() if sf
        })
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
    actual minus prior-month current-cycle (Base + R&O) tracker forecast
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

    # ── Forecast side (tracker, current cycle, Base+R&O, prior month): −pounds ─
    if not trk.empty:
        fc = trk.loc[
            (trk["cycle"] == filters.current_cycle)
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
    "derive_brand",
    "build_item_dim_lookup",
    "list_tracker_cycles",
    "list_tracker_months",
    "validate_filters",
    "fetch_ro_summary_total_delta_by_path",
    "fetch_ro_summary_current_plan_by_path",
    "months_in_range",
    "enrich_ibp_orders_df",
    "resolve_ro_summary_path",
    "build_enriched_sources",
    "EnrichedSources",
    "build_item_dim_frame",
    "build_demand_plan_comparison",
    "build_prior_month_actual_vs_fcst_table",
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
