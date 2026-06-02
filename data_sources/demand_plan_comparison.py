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
Every value is in **millions of pounds**, rounded to one decimal place
for display — matching the planner's Excel and the Demand MOM Summary
above it.
"""
from __future__ import annotations

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
from data_sources.fabric_lakehouse_io import LakehouseIOError, read_csv


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

# RO Summary Report (RO_Reporting/RO_Summary_Report.csv).
_RO_SUMMARY_REPORT_BLOB_PATH: str = (
    "RO Tracking/RO_Reporting/RO_Summary_Report.csv"
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


# ════════════════════════════════════════════════════════════════════════════
# BUDGET — hard-coded per-leaf budget in MILLIONS of lbs.
# ════════════════════════════════════════════════════════════════════════════
#
#   >>> FILL THESE IN <<<
#
# The planner specified Budget as a fixed number per row.  Populate each
# LEAF below with its budget (in millions of lbs); subtotals are summed
# automatically from their non-memo children, so do NOT set subtotal
# budgets here.  Memo rows (Cottage Cheese / Sour Cream) may carry their
# own budget for display but never roll up.
#
# Values default to 0.0 until provided.
_BUDGET_BY_LEAF_M: dict[str, float] = {
    # ESL
    "esl_lc_branded": 0.0,
    "esl_lc_private": 0.0,
    "esl_sc_branded": 0.0,
    "esl_sc_private": 0.0,
    "esl_aerosol":    0.0,
    # Aseptic
    "asep_branded":   0.0,
    "asep_private":   0.0,
    # Cultured (Supply Format breakdown — these roll up)
    "cult_large_tub": 0.0,
    "cult_small_tub": 0.0,
    "cult_pail":      0.0,
    # Cultured (memo Portfolio-Minor breakdown — do NOT roll up)
    "cult_cottage_cheese": 0.0,
    "cult_sour_cream":     0.0,
    # Fresh Milk
    "fm_gallon_jug":  0.0,
    "fm_caseless_jug": 0.0,
    "fm_mini_carton": 0.0,
    "fm_hg_jug":      0.0,
    "fm_bossy":       0.0,
    "fm_totes":       0.0,
    "fm_tanker":      0.0,
    # Butter
    "butter":         0.0,
}


def _budget(leaf_id: str) -> float:
    """Return the hard-coded budget (millions) for *leaf_id* (0.0 if unset)."""
    return float(_BUDGET_BY_LEAF_M.get(leaf_id, 0.0))


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
          ro_summary_path=(_RO_TOTAL, _RO_ESL, "Large Carton", _RO_BRANDED),
          budget_m=_budget("esl_lc_branded")),
    _leaf("esl_lc_private", "Private", 3,
          pmaj_match=_ESL, sfmt_match="Large Carton", brand_match=BRAND_PRIVATE,
          ro_summary_path=(_RO_TOTAL, _RO_ESL, "Large Carton", _RO_PRIVATE),
          budget_m=_budget("esl_lc_private")),
    _subtotal("esl_sc", "Small Carton", 2, ("esl_sc_branded", "esl_sc_private")),
    _leaf("esl_sc_branded", "Branded", 3,
          pmaj_match=_ESL, sfmt_match="Small Carton", brand_match=BRAND_BRANDED,
          ro_summary_path=(_RO_TOTAL, _RO_ESL, "Small Carton", _RO_BRANDED),
          budget_m=_budget("esl_sc_branded")),
    _leaf("esl_sc_private", "Private", 3,
          pmaj_match=_ESL, sfmt_match="Small Carton", brand_match=BRAND_PRIVATE,
          ro_summary_path=(_RO_TOTAL, _RO_ESL, "Small Carton", _RO_PRIVATE),
          budget_m=_budget("esl_sc_private")),
    _leaf("esl_aerosol", "Aerosol Can", 2,
          pmaj_match=_ESL, sfmt_match="Aerosol Can",
          budget_m=_budget("esl_aerosol")),

    # ── Aseptic ─────────────────────────────────────────────────────
    _subtotal("aseptic", "Aseptic", 1, ("asep_branded", "asep_private")),
    _leaf("asep_branded", "Branded", 2,
          pmaj_match=_ESL, sfmt_match="Aseptic", brand_match=BRAND_BRANDED,
          ro_summary_path=(_RO_TOTAL, _RO_ASEPTIC, _RO_BRANDED),
          budget_m=_budget("asep_branded")),
    _leaf("asep_private", "Private", 2,
          pmaj_match=_ESL, sfmt_match="Aseptic", brand_match=BRAND_PRIVATE,
          ro_summary_path=(_RO_TOTAL, _RO_ASEPTIC, _RO_PRIVATE),
          budget_m=_budget("asep_private")),

    # ── Cultured ────────────────────────────────────────────────────
    # Subtotal = Large Tub + Small Tub + Pail (the Supply Format split).
    # Cottage Cheese / Sour Cream are MEMO rows (Portfolio Minor split of
    # the same total) and are deliberately NOT children of the subtotal.
    _subtotal("cultured", "Cultured", 1, ("cult_large_tub", "cult_small_tub", "cult_pail")),
    _leaf("cult_large_tub", "Large Tub", 2,
          pmaj_match=_CULTURED, sfmt_match="Large Tub",
          ro_summary_path=(_RO_TOTAL, _RO_CULTURED, "Large Tub"),
          budget_m=_budget("cult_large_tub")),
    _leaf("cult_small_tub", "Small Tub", 2,
          pmaj_match=_CULTURED, sfmt_match="Small Tub",
          ro_summary_path=(_RO_TOTAL, _RO_CULTURED, "Small Tub"),
          budget_m=_budget("cult_small_tub")),
    _leaf("cult_pail", "Pail", 2,
          pmaj_match=_CULTURED, sfmt_match="Pail",
          budget_m=_budget("cult_pail")),
    _leaf("cult_cottage_cheese", "Cottage Cheese", 2,
          pmaj_match=_CULTURED, pminor_match="Cottage Cheese", is_memo=True,
          budget_m=_budget("cult_cottage_cheese")),
    _leaf("cult_sour_cream", "Sour Cream", 2,
          pmaj_match=_CULTURED, pminor_match="Sour Cream", is_memo=True,
          budget_m=_budget("cult_sour_cream")),

    # ── Fresh Milk ──────────────────────────────────────────────────
    # Subtotal = Gallon Jug … Tanker.
    _subtotal("fresh_milk", "Fresh Milk", 1,
              ("fm_gallon_jug", "fm_caseless_jug", "fm_mini_carton",
               "fm_hg_jug", "fm_bossy", "fm_totes", "fm_tanker")),
    _leaf("fm_gallon_jug", "Gallon Jug", 2,
          pmaj_match=_FRESH_MILK, sfmt_match="Gallon Jug",
          ro_summary_path=(_RO_TOTAL, _RO_FRESH_MILK, "Gallon Jug"),
          budget_m=_budget("fm_gallon_jug")),
    _leaf("fm_caseless_jug", "Caseless Jug", 2,
          pmaj_match=_FRESH_MILK, sfmt_match="Caseless Jug",
          ro_summary_path=(_RO_TOTAL, _RO_FRESH_MILK, "Caseless Jug"),
          budget_m=_budget("fm_caseless_jug")),
    _leaf("fm_mini_carton", "Mini Carton", 2,
          pmaj_match=_FRESH_MILK, sfmt_match="Mini Carton",
          ro_summary_path=(_RO_TOTAL, _RO_FRESH_MILK, "Mini Carton"),
          budget_m=_budget("fm_mini_carton")),
    _leaf("fm_hg_jug", "HG Jug", 2,
          pmaj_match=_FRESH_MILK, sfmt_match="HG Jug",
          ro_summary_path=(_RO_TOTAL, _RO_FRESH_MILK, "HG Jug"),
          budget_m=_budget("fm_hg_jug")),
    _leaf("fm_bossy", "Bossy", 2,
          pmaj_match=_FRESH_MILK, sfmt_match="Bossy",
          ro_summary_path=(_RO_TOTAL, _RO_FRESH_MILK, "Bossy"),
          budget_m=_budget("fm_bossy")),
    # "Totes" under Fresh Milk matches Supply Format = "Dispenser" (per
    # the planner's rule), and maps to the RO Summary "Totes" leaf.
    _leaf("fm_totes", "Totes", 2,
          pmaj_match=_FRESH_MILK, sfmt_match="Dispenser",
          ro_summary_path=(_RO_TOTAL, _RO_FRESH_MILK, "Totes"),
          budget_m=_budget("fm_totes")),
    # Tanker rows live under "Fresh Milk or Bulk Fluid HTST"; no RO
    # Summary counterpart, so R&O stays 0.
    _leaf("fm_tanker", "Tanker", 2,
          pmaj_match=_FRESH_MILK_TANKER, sfmt_match="Tanker",
          budget_m=_budget("fm_tanker")),

    # ── Butter ──────────────────────────────────────────────────────
    # Per planner direction (2026-06), Butter is now scoped by BOTH
    # Portfolio Major = Butter AND Portfolio Minor = "Packaged Butter"
    # (e.g. excludes bulk/ingredient butter that shares the Butter PMaj).
    # Both values come from PDH; the mask ANDs them in _leaf_mask.
    _leaf("butter", "Butter", 1,
          pmaj_match=_BUTTER, pminor_match=_BUTTER_PMINOR,
          ro_summary_path=(_RO_TOTAL, "Butter"),
          budget_m=_budget("butter")),
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

_LBS_PER_MILLION: float = 1_000_000.0


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

    Parses ``Start of Month`` with the shared coercion primitive so
    Excel serials and string dates both normalise to first-of-month
    :class:`datetime.date` values.
    """
    if tracker_df is None or TRK_START_OF_MONTH not in tracker_df.columns:
        return []
    months = (
        tracker_df[TRK_START_OF_MONTH]
        .map(_coerce_start_of_month)
        .dropna()
    )
    return sorted({m for m in months if m is not None})


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


def build_item_dim_lookup(pdh_df: Optional[pd.DataFrame]) -> dict[str, _ItemDims]:
    """Return ``{normalised_item_key -> _ItemDims}`` from PDH.

    * Joined on the PDH item-number column (auto-detected).
    * Brand is derived from the PDH ``Item Description`` (the planner's
      ``DG`` rule) — always taken from PDH, never the tracker, so the
      brand split is consistent across plan and actuals.
    * Last row wins on duplicate items (a planner can audit PDH directly
      if a multi-row item is surprising — same contract as
      ``build_supply_format_lookup``).

    Returns an empty dict when PDH is unusable; downstream every item
    then resolves to blank dimensions and simply won't match any leaf.
    """
    lookup: dict[str, _ItemDims] = {}
    if pdh_df is None or pdh_df.empty:
        return lookup

    item_col = _resolve_column(pdh_df, _PDH_ITEM_CANDIDATES)
    if not item_col:
        return lookup
    desc_col = _resolve_column(pdh_df, _PDH_DESC_CANDIDATES)
    pmaj_col = _resolve_column(pdh_df, _PDH_PMAJ_CANDIDATES)
    pminor_col = _resolve_column(pdh_df, _PDH_PMINOR_CANDIDATES)
    sfmt_col = _resolve_column(pdh_df, _PDH_SFMT_CANDIDATES)

    for _, row in pdh_df.iterrows():
        key = _normalise_item_key(row.get(item_col))
        if not key:
            continue
        lookup[key] = _ItemDims(
            pmaj=_clean_str(row.get(pmaj_col)) if pmaj_col else "",
            sfmt=_clean_str(row.get(sfmt_col)) if sfmt_col else "",
            pminor=_clean_str(row.get(pminor_col)) if pminor_col else "",
            brand=derive_brand(row.get(desc_col)) if desc_col else BRAND_PRIVATE,
        )
    return lookup


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


def _enrich_tracker(
    tracker_df: pd.DataFrame, dims: dict[str, _ItemDims],
) -> pd.DataFrame:
    """Return a tidy, enriched tracker frame ready for leaf filtering.

    Output columns (one row per source row):
    ``item_key, month (date), pounds (float), forecast_type (Base/R&O),
    cycle (str), pmaj, sfmt, pminor, brand``.

    Rows with an unparseable month are dropped (they can't be bucketed).
    """
    if tracker_df is None or tracker_df.empty:
        return _empty_enriched()

    df = tracker_df.copy()
    item_keys = df[TRK_ITEM].map(_normalise_item_key)
    resolved = item_keys.map(lambda k: dims.get(k))

    out = pd.DataFrame({
        "item_key": item_keys,
        "month": df[TRK_START_OF_MONTH].map(_coerce_start_of_month),
        "pounds": pd.to_numeric(
            df[TRK_DEMAND_LBS].astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        ),
        "forecast_type": df[TRK_FORECAST_TYPE].map(_normalise_forecast_type),
        "cycle": df[TRK_CYCLE].map(_clean_str),
        "pmaj": resolved.map(lambda d: d.pmaj if d else ""),
        "sfmt": resolved.map(lambda d: d.sfmt if d else ""),
        "pminor": resolved.map(lambda d: d.pminor if d else ""),
        "brand": resolved.map(lambda d: d.brand if d else BRAND_PRIVATE),
    })
    out["pounds"] = out["pounds"].fillna(0.0)
    return out.dropna(subset=["month"]).reset_index(drop=True)


def _enrich_ibp(
    ibp_df: pd.DataFrame, dims: dict[str, _ItemDims],
) -> pd.DataFrame:
    """Return a tidy, enriched IBP Shipments frame for leaf filtering.

    Output columns: ``item_key, month (date), pounds (float), pmaj,
    sfmt, pminor, brand``.  Column names are auto-detected from the
    candidate whitelists so a spelling drift in the Delta table is a
    one-line fix in the constants above.
    """
    if ibp_df is None or ibp_df.empty:
        return _empty_enriched(actuals=True)

    item_col = _resolve_column(ibp_df, _IBP_ITEM_CANDIDATES)
    month_col = _resolve_column(ibp_df, _IBP_MONTH_CANDIDATES)
    qty_col = _resolve_column(ibp_df, _IBP_QTY_CANDIDATES)
    if not (item_col and month_col and qty_col):
        # Missing a required column → no actuals contribute.  The caller
        # surfaces a warning; we degrade to an empty frame rather than
        # raising so the (plan-only) columns still render.
        logger.warning(
            "IBP Shipments missing a required column "
            "(item=%r, month=%r, qty=%r); actuals will be zero.",
            item_col, month_col, qty_col,
        )
        return _empty_enriched(actuals=True)

    df = ibp_df.copy()
    item_keys = df[item_col].map(_normalise_item_key)
    resolved = item_keys.map(lambda k: dims.get(k))

    out = pd.DataFrame({
        "item_key": item_keys,
        "month": df[month_col].map(_coerce_start_of_month),
        "pounds": pd.to_numeric(
            df[qty_col].astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        ),
        "pmaj": resolved.map(lambda d: d.pmaj if d else ""),
        "sfmt": resolved.map(lambda d: d.sfmt if d else ""),
        "pminor": resolved.map(lambda d: d.pminor if d else ""),
        "brand": resolved.map(lambda d: d.brand if d else BRAND_PRIVATE),
    })
    out["pounds"] = out["pounds"].fillna(0.0)
    return out.dropna(subset=["month"]).reset_index(drop=True)


def _empty_enriched(actuals: bool = False) -> pd.DataFrame:
    """Return an empty enriched frame with the right columns."""
    cols = ["item_key", "month", "pounds", "pmaj", "sfmt", "pminor", "brand"]
    if not actuals:
        cols = ["item_key", "month", "pounds", "forecast_type", "cycle",
                "pmaj", "sfmt", "pminor", "brand"]
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

def fetch_ro_summary_total_delta_by_path() -> dict[tuple[str, ...], float]:
    """Return ``{label_path -> FY27 Total Delta}`` from the saved RO Summary.

    The RO Summary Report CSV stores an indented tree in its
    ``Millions of lbs.`` column (two NBSPs per level) plus the metric
    column ``FY27 Probabilized | Total Delta``.  We rebuild each row's
    full label path (e.g. ``("Total B2C", "Cultured", "Large Tub")``)
    by tracking the most-recent label seen at each shallower indent, and
    map it to the row's Total Delta.

    Returns an empty dict when the file is missing / unreadable / lacks
    the expected columns — the caller treats that as "R&O = 0 for every
    row" and surfaces a soft warning.  Never raises on a missing file.
    """
    try:
        df, _etag = read_csv(_RO_SUMMARY_SECRETS_SECTION, _RO_SUMMARY_REPORT_BLOB_PATH)
    except LakehouseIOError as exc:
        logger.info("RO Summary Report read failed (R&O will be 0): %s", exc)
        return {}

    if df is None or df.empty:
        logger.info("RO Summary Report is missing or empty (R&O will be 0).")
        return {}

    label_col = _resolve_column(df, _RO_SUMMARY_LABEL_CANDIDATES)
    delta_col = _resolve_column(df, _RO_SUMMARY_TOTAL_DELTA_CANDIDATES)
    if not label_col or not delta_col:
        logger.info(
            "RO Summary Report present but expected columns not found "
            "(label=%r, total_delta=%r).  Available columns: %r.  R&O will be 0.",
            label_col, delta_col, list(df.columns),
        )
        return {}

    by_path: dict[tuple[str, ...], float] = {}
    stack: list[str] = []  # stack[i] = label at indent depth i
    for _, row in df.iterrows():
        raw_label = row.get(label_col)
        if raw_label is None or (isinstance(raw_label, float) and pd.isna(raw_label)):
            continue
        label_text = str(raw_label)
        indent = _indent_depth(label_text)
        clean = label_text.replace(_NBSP, "").strip()
        if not clean:
            continue
        # Maintain the path stack: truncate to this row's depth, then set.
        del stack[indent:]
        stack.append(clean)
        value = pd.to_numeric(row.get(delta_col), errors="coerce")
        by_path[tuple(stack)] = float(value) if pd.notna(value) else 0.0
    return by_path


def _indent_depth(indented_label: str) -> int:
    """Return the tree depth encoded in an RO-Summary indented label.

    The RO Summary exporter prefixes ``"\\u00A0\\u00A0" * indent`` (two
    NBSPs per level).  We count leading NBSPs and divide by two.
    """
    leading = len(indented_label) - len(indented_label.lstrip(_NBSP))
    return leading // 2


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────

def build_demand_plan_comparison(
    tracker_df: pd.DataFrame,
    ibp_df: pd.DataFrame,
    pdh_df: pd.DataFrame,
    filters: ComparisonFilters,
    *,
    ro_total_delta_by_path: Optional[dict[tuple[str, ...], float]] = None,
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
    """
    warnings: list[str] = []

    # 1. Dimensions + enrichment.
    dims = build_item_dim_lookup(pdh_df)
    if not dims:
        warnings.append(
            "PDH (qry_pdh.csv) was empty or missing its Item No column — "
            "Portfolio Major / Supply Format / Brand could not be resolved, "
            "so every row is zero.  Check the upstream PDH export."
        )
    trk = _enrich_tracker(tracker_df, dims)
    ibp = _enrich_ibp(ibp_df, dims)

    # 1b. R&O source (FY27 Total Delta by label path).
    if ro_total_delta_by_path is None:
        ro_total_delta_by_path = fetch_ro_summary_total_delta_by_path()
    ro_available = bool(ro_total_delta_by_path)
    if not ro_available:
        warnings.append(
            "RO Summary Report (RO_Summary_Report.csv) was unavailable, so "
            "the R&O column is zero.  Save the RO Summary Report above to "
            "populate it."
        )

    # Precompute the month buckets once (cheap set membership downstream).
    actual_months = _months_in_range(filters.actual_start, filters.actual_end)
    forecast_months = _months_in_range(filters.forecast_start, filters.forecast_end)
    prior_month = filters.prior_month.replace(day=1)

    # 2. Per-leaf additive measures.
    measures: dict[str, dict[str, float]] = {}
    for tpl in COMPARISON_TEMPLATE:
        if tpl.is_subtotal:
            continue
        measures[tpl.row_id] = _compute_leaf_measures(
            tpl, trk, ibp, filters,
            actual_months=actual_months,
            forecast_months=forecast_months,
            prior_month=prior_month,
            ro_total_delta_by_path=ro_total_delta_by_path,
        )

    # 3. Subtotal roll-up (children-first via the id graph).
    for tpl in COMPARISON_TEMPLATE:
        if tpl.is_subtotal:
            measures[tpl.row_id] = _rollup_subtotal(tpl, measures)

    # 4 + 5. Assemble the display frame.
    table = _assemble_table(measures)
    return ComparisonResult(
        table=table,
        warnings=tuple(warnings),
        ro_summary_available=ro_available,
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
    tpl: TemplateRow, measures: dict[str, dict[str, float]],
) -> dict[str, float]:
    """Return a subtotal's additive measures = Σ of its children.

    Children are resolved recursively through the id graph, so a
    subtotal-of-subtotals (e.g. ESL → Large Carton → Branded) sums
    correctly.  Memo rows are never declared as children, so they are
    inherently excluded from every subtotal.
    """
    totals = {col: 0.0 for col in _ADDITIVE_COLS}
    for child_id in tpl.children:
        child_tpl = TEMPLATE_BY_ID[child_id]
        child = (
            _rollup_subtotal(child_tpl, measures)
            if child_tpl.is_subtotal
            else measures[child_id]
        )
        for col in _ADDITIVE_COLS:
            totals[col] += child.get(col, 0.0)
    return totals


def _assemble_table(measures: dict[str, dict[str, float]]) -> pd.DataFrame:
    """Return the display-ready table from per-row additive measures.

    Adds the derived + ratio columns, the indented label, the internal
    metadata columns (for the page's row styling), rounds metric values
    to one decimal, and applies the display column order + labels.
    """
    records: list[dict] = []
    for tpl in COMPARISON_TEMPLATE:
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

    # Round metric columns: 1 dp for millions, 4 dp for ratios (the page
    # formats ratios as percentages — keeping 4 dp preserves e.g. 6.3%).
    for col in DISPLAY_ORDER:
        if col in PERCENT_COLS:
            df[col] = df[col].round(4)
        else:
            df[col] = df[col].round(1)

    # Final column order: metadata + label + metrics (display order),
    # renamed to the screenshot labels.
    ordered = [*_META_COLS, COL_LABEL, *DISPLAY_ORDER]
    df = df.loc[:, ordered]
    return df.rename(columns=DISPLAY_LABELS)


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
    "PERCENT_COLS",
    "COL_LABEL",
    "derive_brand",
    "build_item_dim_lookup",
    "list_tracker_cycles",
    "list_tracker_months",
    "validate_filters",
    "fetch_ro_summary_total_delta_by_path",
    "build_demand_plan_comparison",
    "comparison_to_csv_bytes",
]
