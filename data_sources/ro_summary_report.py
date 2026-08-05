"""RO Summary Report — hierarchical roll-up of the published RO Comparison.

This module owns the small downstream report that renders below the
RO Comparison editor on the Demand Planner Analytics page.  It is a
*pure consumer* of the published ``RO_Comparison_Output.csv`` blob in
Microsoft Fabric — it does NOT touch the in-memory comparison frame,
the dp_dimitems table, or the RO_History feed.  This means the
section can be refreshed independently of the editor and always
shows what was last *saved* (planner's mental model of "approved" data).

Layout (mirrors the planner's existing Excel screenshot)
--------------------------------------------------------
A fixed 30-row template, indented like a tree:

    Total B2C
      Extended Shelf Life
        Large Carton
          Branded
          Private Label
        Small Carton
          Branded
          Private Label
      Aseptic
        Branded
        Private Label
      Cultured
        Large Tub
          Cottage Cheese
          Sour Cream
        Small Tub
          Cottage Cheese
          Sour Cream
        Totes
          Cottage Cheese
          Sour Cream
      Fresh Milk
        Gallon Jug
        Caseless Jug
        Mini Carton
        HG Jug
        Bossy
        Totes
        QT Jug
      Butter
        <Supply Format>   ← dynamic leaf per format present in data
        …

Butter is a **subtotal** whose children are Supply Format leaves
discovered at build time from rows with ``Portfolio Major == "Butter"``.
Only formats that actually appear in the comparison frame are emitted
(planner request, 2026-06) — no fixed format list and no Brand tier.

The template is hardcoded because it represents a business reporting
contract — the planner expects these exact rows to appear in this
exact order every month regardless of what data exists.  Rows that
end up all-zero after computation are HIDDEN from the editor view
(via :func:`drop_all_zero_rows`) but the saved CSV keeps every row
so downstream consumers get a stable column / row shape.

Data columns (8)
----------------
* ``FY27 Probabilized | Current Plan``     — Σ ``LE Current Fiscal Probabilized Lbs``
* ``FY27 Probabilized | Total Delta``      — ``New + Exit + Change``  (recomputed)
* ``Delta Breakdown   | New``              — Σ ``Change Current Fiscal Probabilized Lbs`` where Driver = "New"
* ``Delta Breakdown   | Exit``             — Σ same, Driver = "Exit"
* ``Delta Breakdown   | Change``           — Σ same, Driver = "Change"
* ``FY28 Probabilized | Prior``            — Σ ``Prior Year1 Probabilized Lbs``
* ``FY28 Probabilized | Change``           — ``Latest − Prior``  (recomputed)
* ``FY28 Probabilized | Latest``           — Σ ``LE Year1 Probabilized Lbs``
* ``FY28 Delta Breakdown | New/Exit/Change/Risk`` — same Risk-first bucketing
  as the FY27 Delta Breakdown, but on the ANNUALIZED (Year-1) delta
  ``LE Year1 − Prior Year1`` per row.  Sums to ``FY28 Probabilized | Change``.

Numbers are stored in **millions of lbs** (raw ÷ 1,000,000, rounded
to 1 decimal) — both for display and for the saved CSV — so the
planner edits the same scale they see, and saved files don't need
out-of-band scale documentation.

Subtotals
---------
* Subtotal rows recompute from their declared children on every
  rerun (see :func:`recompute_subtotals`).  This means even if the
  planner edits a subtotal cell directly, the next render
  overwrites it with the rolled-up sum — preventing a saved CSV
  whose totals don't reconcile.
* The page is expected to make data columns editable across the
  whole table (Streamlit's column_config can't disable per-cell,
  only per-column); the recompute pass enforces consistency.

Brand Category derivation
-------------------------
The screenshot's "Brand Category" dimension is derived from our
existing normalised ``Brand`` column per the planner's spec
(``Brand == "Private"`` → category ``Private`` displayed as "Private
Label"; everything else → ``Branded``).  We do NOT pull the
``Brand Category`` column from RO_Item_Master to keep this module's
input contract narrow (only RO_Comparison_Output.csv).

Portfolio Major normalisation
-----------------------------
Some portfolio families use synonym frozensets in ``pmaj_match`` so
leaves roll up correctly when upstream dim tables disagree on the
literal string (matching only — display columns keep screenshot
values):

* **ESL** — ``"ESL"`` and ``"Extended Shelf Life"``
* **Fresh Milk** — ``"Fresh Milk"`` and ``"HTST"`` (RO Comparison /
  dp_dimitems often labels HTST fresh-milk SKUs as PMaj=HTST)

See :data:`_ESL` and :data:`_FRESH_MILK` in :data:`RO_SUMMARY_TEMPLATE`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import streamlit as st

from data_sources.fabric_lakehouse_io import LakehouseIOError, read_csv, write_csv
from data_sources.ro_comparison import (
    ANNUAL_OPP_LE,
    CUR_FISCAL_PROB_CHANGE,
    CUR_FISCAL_PROB_LE,
    YEAR1_PROB_LE,
    YEAR1_PROB_PRIOR,
)
from data_sources.ro_risk import risk_mask
from data_sources.ro_rules_config import RoRulesConfig


logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────────
#
# Same secrets block as the rest of the section — see ``ro_comparison.py``.
_SECRETS_SECTION: str = "fabric_htst"

# Sources / sinks.
_RO_COMPARISON_OUTPUT_BLOB_PATH: str = (
    "RO Tracking/RO_Reporting/RO_Comparison_Output.csv"
)
_RO_SUMMARY_REPORT_BLOB_PATH: str = (
    "RO Tracking/RO_Reporting/RO_Summary_Report.csv"
)

# Cache TTL: 15 min, matching RO_History_Tracker (same publication
# cadence and the same staleness tolerance for the planner).
_COMPARISON_OUTPUT_CACHE_TTL_SECONDS: int = 15 * 60


# ── Internal column identifiers ──────────────────────────────────────────────
#
# We deliberately use distinct internal IDs for the 8 data columns
# because the planner's labels have a collision ("Change" appears
# under both Delta Breakdown and FY28 Probabilized).  pandas
# DataFrames don't tolerate duplicate column names; the column_config
# in the page maps these IDs to clean display labels.

COL_PRIOR_PLAN: str   = "_fy27_prior_plan"
COL_CURRENT_PLAN: str = "_fy27_current_plan"
COL_TOTAL_DELTA: str  = "_fy27_total_delta"
COL_DELTA_NEW: str    = "_delta_new"
COL_DELTA_EXIT: str   = "_delta_exit"
COL_DELTA_CHANGE: str = "_delta_change"
COL_DELTA_RISK: str   = "_delta_risk"
COL_Y1_PRIOR: str     = "_y1_prior"
COL_Y1_CHANGE: str    = "_y1_change"
COL_Y1_LATEST: str    = "_y1_latest"
# FY28 Delta Breakdown — same New / Exit / Change / Risk buckets as the
# FY27 Delta Breakdown, but bucketed on the ANNUALIZED (Year-1) probabilized
# delta (``YEAR1_PROB_LE − YEAR1_PROB_PRIOR``) instead of the pro-rated FY27
# change.  Gives the planner attribution symmetry across the two horizons:
# how much of the FY28 swing is committed risk vs new opportunities vs
# volume changes, at the same run-rate scale.  New + Exit + Change + Risk
# reconciles exactly to :data:`COL_Y1_CHANGE` (Latest − Prior) by construction.
COL_Y1_DELTA_NEW: str    = "_y1_delta_new"
COL_Y1_DELTA_EXIT: str   = "_y1_delta_exit"
COL_Y1_DELTA_CHANGE: str = "_y1_delta_change"
COL_Y1_DELTA_RISK: str   = "_y1_delta_risk"

DATA_COLS: tuple[str, ...] = (
    COL_PRIOR_PLAN, COL_CURRENT_PLAN, COL_TOTAL_DELTA,
    COL_DELTA_NEW, COL_DELTA_EXIT, COL_DELTA_CHANGE, COL_DELTA_RISK,
    COL_Y1_PRIOR, COL_Y1_CHANGE, COL_Y1_LATEST,
    COL_Y1_DELTA_NEW, COL_Y1_DELTA_EXIT, COL_Y1_DELTA_CHANGE, COL_Y1_DELTA_RISK,
)

# Display labels for the saved CSV — the planner / downstream
# consumers see these (grouped form), not the internal IDs.
SAVED_COLUMN_LABELS: dict[str, str] = {
    COL_PRIOR_PLAN:   "FY27 Probabilized | Prior Plan",
    COL_CURRENT_PLAN: "FY27 Probabilized | Current Plan",
    COL_TOTAL_DELTA:  "FY27 Probabilized | Total Delta",
    COL_DELTA_NEW:    "Delta Breakdown | New",
    COL_DELTA_EXIT:   "Delta Breakdown | Exit",
    COL_DELTA_CHANGE: "Delta Breakdown | Change",
    COL_DELTA_RISK:   "Delta Breakdown | Risk",
    COL_Y1_PRIOR:     "FY28 Probabilized | Prior",
    COL_Y1_CHANGE:    "FY28 Probabilized | Change",
    COL_Y1_LATEST:    "FY28 Probabilized | Latest",
    COL_Y1_DELTA_NEW:    "FY28 Delta Breakdown | New",
    COL_Y1_DELTA_EXIT:   "FY28 Delta Breakdown | Exit",
    COL_Y1_DELTA_CHANGE: "FY28 Delta Breakdown | Change",
    COL_Y1_DELTA_RISK:   "FY28 Delta Breakdown | Risk",
}

# Structural / display columns kept alongside the data columns.
COL_LABEL: str           = "Millions of lbs."
COL_DIM_PMAJ: str        = "Portfolio Major"
COL_DIM_SFMT: str        = "Supply Format"
COL_DIM_PMINOR: str      = "Portfolio Minor"
COL_DIM_BCAT: str        = "Brand Category"
DIM_COLS: tuple[str, ...] = (COL_DIM_PMAJ, COL_DIM_SFMT, COL_DIM_PMINOR, COL_DIM_BCAT)

# Internal-only structural metadata columns the page hides from the
# editor.  Used to drive subtotal recomputation and row styling.
COL_ROW_ID: str       = "_row_id"
COL_INDENT: str       = "_indent"
COL_IS_SUBTOTAL: str  = "_is_subtotal"
META_COLS: tuple[str, ...] = (COL_ROW_ID, COL_INDENT, COL_IS_SUBTOTAL)


# ── Errors ───────────────────────────────────────────────────────────────────

class RoSummaryReportError(RuntimeError):
    """Raised on any RO_Summary_Report I/O or compute failure.

    Wraps the lower-level :class:`LakehouseIOError` so the page
    renders a single, scope-aware banner without leaking storage
    diagnostics into the section body.
    """


# ── Template ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TemplateRow:
    """One row in the RO Summary Report template — leaf or subtotal.

    Attributes
    ----------
    row_id
        Globally unique stable identifier.  Used to look up rows
        across edits / reruns and to drive the subtotal-rollup graph.
    label
        Display string (e.g., "Large Carton") shown in the indented
        ``Millions of lbs.`` column.  NOT unique — "Cottage Cheese"
        appears multiple times under different Supply Formats.
    indent
        Nesting depth, 0 = root.  Used for the leading-space indent
        in the label and (optionally) row styling.
    portfolio_major, supply_format, portfolio_minor, brand_category
        Dimension display values for the screenshot's leftmost four
        columns.  Blank for subtotal rows; literal screenshot values
        for leaves (e.g., "ESL" for some leaves, "Extended Shelf
        Life" for others — preserved verbatim).
    is_subtotal
        True for rolled-up rows; False for leaves that source values
        from the RO Comparison output.
    children
        Tuple of child ``row_id`` values for subtotal rows; empty for
        leaves.
    pmaj_match, sfmt_match, pminor_match, brand_match
        Match criteria used by :func:`_filter_for_leaf` to slice the
        comparison frame.  ``None`` on any dim means "no constraint"
        (any value matches).  ``pmaj_match`` is a frozenset to support
        the ESL ↔ Extended Shelf Life synonym pair; the other three
        are scalar strings.
    """
    row_id: str
    label: str
    indent: int
    portfolio_major: str
    supply_format: str
    portfolio_minor: str
    brand_category: str
    is_subtotal: bool
    children: tuple[str, ...]
    pmaj_match: Optional[frozenset]
    sfmt_match: Optional[str]
    pminor_match: Optional[str]
    brand_match: Optional[str]


# Portfolio Major synonym sets — one frozenset per family that needs
# more than one upstream literal.  Defined at module scope so every
# leaf reuses the same object (mirrors :data:`_ESL`).
_ESL: frozenset = frozenset({"ESL", "Extended Shelf Life"})
_CULTURED: frozenset = frozenset({"Cultured"})
# Fresh Milk leaves aggregate rows whose PMaj is either the report
# label or the HTST code used in dp_dimitems / RO_Item_Master.
_FRESH_MILK: frozenset = frozenset({"Fresh Milk", "HTST"})
_BUTTER: frozenset = frozenset({"Butter"})

# Reusable helper to keep the TemplateRow literals readable.
def _subtotal(
    row_id: str, label: str, indent: int, children: tuple[str, ...],
) -> TemplateRow:
    """Shorthand: build a subtotal row (blank dims, no match criteria)."""
    return TemplateRow(
        row_id=row_id, label=label, indent=indent,
        portfolio_major="", supply_format="", portfolio_minor="", brand_category="",
        is_subtotal=True, children=children,
        pmaj_match=None, sfmt_match=None, pminor_match=None, brand_match=None,
    )


def _leaf(
    row_id: str, label: str, indent: int,
    *, pmaj_disp: str, sfmt: str = "", pminor: str = "", bcat: str = "",
    pmaj_match: frozenset, brand_match: Optional[str] = None,
    pminor_match: Optional[str] = None,
) -> TemplateRow:
    """Shorthand: build a leaf row.

    *pmaj_disp* / *sfmt* / *pminor* / *bcat* populate the display
    dimension columns verbatim (mirroring the screenshot — which
    has ESL in some rows and "Extended Shelf Life" in others).

    *pmaj_match* / *sfmt* / *pminor_match* / *brand_match* drive the
    actual data filter.  *sfmt* doubles as both display value and
    filter criterion (most leaves match on the same string they
    display).  Pass ``sfmt=""`` (or omit) to disable the Supply
    Format filter — used by **Butter**, which per the data-pulling
    rules aggregates every SFmt under PMaj=Butter.
    """
    return TemplateRow(
        row_id=row_id, label=label, indent=indent,
        portfolio_major=pmaj_disp, supply_format=sfmt,
        portfolio_minor=pminor, brand_category=bcat,
        is_subtotal=False, children=(),
        pmaj_match=pmaj_match,
        # Empty *sfmt* (no display value) collapses to "no SFmt
        # filter" so the legacy call sites stay terse.
        sfmt_match=sfmt or None,
        pminor_match=pminor_match, brand_match=brand_match,
    )


# The 30-row template.  Order here = display order in the editor.
# Subtotals are declared BEFORE their children (top-down screenshot
# order); the recompute pass walks children-first via the graph.
RO_SUMMARY_TEMPLATE: tuple[TemplateRow, ...] = (
    _subtotal("total_b2c", "Total B2C", 0,
              ("esl", "asep", "cult", "fm", "but")),

    # ── Extended Shelf Life ───────────────────────────────────────
    _subtotal("esl", "Extended Shelf Life", 1, ("esl_lc", "esl_sc")),
    _subtotal("esl_lc", "Large Carton", 2, ("esl_lc_br", "esl_lc_pv")),
    _leaf("esl_lc_br", "Branded", 3,
          pmaj_disp="ESL", sfmt="Large Carton", bcat="Branded",
          pmaj_match=_ESL, brand_match="Branded"),
    _leaf("esl_lc_pv", "Private Label", 3,
          pmaj_disp="Extended Shelf Life", sfmt="Large Carton", bcat="Private",
          pmaj_match=_ESL, brand_match="Private"),
    _subtotal("esl_sc", "Small Carton", 2, ("esl_sc_br", "esl_sc_pv")),
    _leaf("esl_sc_br", "Branded", 3,
          pmaj_disp="Extended Shelf Life", sfmt="Small Carton", bcat="Branded",
          pmaj_match=_ESL, brand_match="Branded"),
    _leaf("esl_sc_pv", "Private Label", 3,
          pmaj_disp="Extended Shelf Life", sfmt="Small Carton", bcat="Private",
          pmaj_match=_ESL, brand_match="Private"),

    # ── Aseptic (sibling of ESL — data still lives under PMaj=ESL) ─
    _subtotal("asep", "Aseptic", 1, ("asep_br", "asep_pv")),
    _leaf("asep_br", "Branded", 2,
          pmaj_disp="ESL", sfmt="Aseptic", bcat="Branded",
          pmaj_match=_ESL, brand_match="Branded"),
    _leaf("asep_pv", "Private Label", 2,
          pmaj_disp="Extended Shelf Life", sfmt="Aseptic", bcat="Private",
          pmaj_match=_ESL, brand_match="Private"),

    # ── Cultured ──────────────────────────────────────────────────
    _subtotal("cult", "Cultured", 1, ("cult_lt", "cult_st", "cult_to")),
    _subtotal("cult_lt", "Large Tub", 2, ("cult_lt_cc", "cult_lt_sc")),
    _leaf("cult_lt_cc", "Cottage Cheese", 3,
          pmaj_disp="Cultured", sfmt="Large Tub", pminor="Cottage Cheese",
          pmaj_match=_CULTURED, pminor_match="Cottage Cheese"),
    _leaf("cult_lt_sc", "Sour Cream", 3,
          pmaj_disp="Cultured", sfmt="Large Tub", pminor="Sour Cream",
          pmaj_match=_CULTURED, pminor_match="Sour Cream"),
    _subtotal("cult_st", "Small Tub", 2, ("cult_st_cc", "cult_st_sc")),
    _leaf("cult_st_cc", "Cottage Cheese", 3,
          pmaj_disp="Cultured", sfmt="Small Tub", pminor="Cottage Cheese",
          pmaj_match=_CULTURED, pminor_match="Cottage Cheese"),
    _leaf("cult_st_sc", "Sour Cream", 3,
          pmaj_disp="Cultured", sfmt="Small Tub", pminor="Sour Cream",
          pmaj_match=_CULTURED, pminor_match="Sour Cream"),
    _subtotal("cult_to", "Totes", 2, ("cult_to_cc", "cult_to_sc")),
    # Cultured Totes leaves match SFmt = "Tote" (singular) per the
    # screenshot's Supply Format column.
    _leaf("cult_to_cc", "Cottage Cheese", 3,
          pmaj_disp="Cultured", sfmt="Tote", pminor="Cottage Cheese",
          pmaj_match=_CULTURED, pminor_match="Cottage Cheese"),
    _leaf("cult_to_sc", "Sour Cream", 3,
          pmaj_disp="Cultured", sfmt="Tote", pminor="Sour Cream",
          pmaj_match=_CULTURED, pminor_match="Sour Cream"),

    # ── Fresh Milk ────────────────────────────────────────────────
    # All leaves use :data:`_FRESH_MILK` (Fresh Milk | HTST) for PMaj
    # filtering; display column stays "Fresh Milk" per the screenshot.
    _subtotal("fm", "Fresh Milk", 1,
              ("fm_gj", "fm_cj", "fm_mc", "fm_hg", "fm_bo", "fm_to", "fm_qt")),
    _leaf("fm_gj", "Gallon Jug", 2,
          pmaj_disp="Fresh Milk", sfmt="Gallon Jug",
          pmaj_match=_FRESH_MILK),
    _leaf("fm_cj", "Caseless Jug", 2,
          pmaj_disp="Fresh Milk", sfmt="Caseless Jug",
          pmaj_match=_FRESH_MILK),
    _leaf("fm_mc", "Mini Carton", 2,
          pmaj_disp="Fresh Milk", sfmt="Mini Carton",
          pmaj_match=_FRESH_MILK),
    _leaf("fm_hg", "HG Jug", 2,
          pmaj_disp="Fresh Milk", sfmt="HG Jug",
          pmaj_match=_FRESH_MILK),
    _leaf("fm_bo", "Bossy", 2,
          pmaj_disp="Fresh Milk", sfmt="Bossy",
          pmaj_match=_FRESH_MILK),
    # Per planner clarification: ONE row labeled "Totes" under Fresh
    # Milk that matches SFmt = "Dispenser" (NOT "Totes" / "Tote").
    _leaf("fm_to", "Totes", 2,
          pmaj_disp="Fresh Milk", sfmt="Dispenser",
          pmaj_match=_FRESH_MILK),
    _leaf("fm_qt", "QT Jug", 2,
          pmaj_disp="Fresh Milk", sfmt="QT Jug",
          pmaj_match=_FRESH_MILK),

    # ── Butter ────────────────────────────────────────────────────
    # Static placeholder — :func:`_build_runtime_template` injects
    # one Supply Format leaf per format present in the source frame
    # and wires those ids into this subtotal's ``children`` tuple.
    _subtotal("but", "Butter", 1, ()),
)

# Lookup index — id → TemplateRow.  Used everywhere we need to fetch
# a row by id (subtotal recompute, save-time validation, etc.).
TEMPLATE_BY_ID: dict[str, TemplateRow] = {
    row.row_id: row for row in RO_SUMMARY_TEMPLATE
}


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


def _collect_butter_supply_formats(df: pd.DataFrame) -> list[str]:
    """Return sorted distinct Supply Formats for rows in the Butter PMaj."""
    if df is None or df.empty or COL_DIM_PMAJ not in df.columns:
        return []
    pmaj = df[COL_DIM_PMAJ].astype(str).str.strip()
    mask = pmaj.isin(_BUTTER)
    if COL_DIM_SFMT not in df.columns:
        return []
    sfmts = (
        df.loc[mask, COL_DIM_SFMT]
        .astype(str).str.strip()
        .replace({"nan": "", "None": ""})
    )
    return sorted({s for s in sfmts.tolist() if s})


def _butter_brand_leaves(fmt_id: str, sfmt: str) -> tuple[TemplateRow, TemplateRow]:
    """The Branded / Private Label leaf pair under one Butter Supply Format.

    Branded vs Private Label is keyed on the derived Brand Category
    (``_brand_cat`` ← the comparison ``Brand`` column, itself sourced from the
    distribution tracker's Brand — NOT any Item-Description "DG" heuristic), so
    it matches the same rule the ESL / Aseptic leaves already use.
    """
    return (
        _leaf(f"{fmt_id}_br", "Branded", 3,
              pmaj_disp="Butter", sfmt=sfmt, bcat="Branded",
              pmaj_match=_BUTTER, brand_match="Branded"),
        _leaf(f"{fmt_id}_pv", "Private Label", 3,
              pmaj_disp="Butter", sfmt=sfmt, bcat="Private",
              pmaj_match=_BUTTER, brand_match="Private"),
    )


def _build_dynamic_butter_rows(formats: list[str]) -> tuple[TemplateRow, ...]:
    """Build Butter Supply Format rows: each format is a SUBTOTAL over its
    Branded / Private Label leaves (indent 2 subtotal → indent 3 leaves)."""
    if not formats:
        return ()
    fmt_ids = _stable_unique_row_ids("but", formats)
    rows: list[TemplateRow] = []
    for sfmt, fmt_id in zip(formats, fmt_ids):
        br, pv = _butter_brand_leaves(fmt_id, sfmt)
        rows.append(_subtotal(fmt_id, sfmt, 2, (br.row_id, pv.row_id)))
        rows.extend((br, pv))
    return tuple(rows)


def _build_runtime_template(source_df: pd.DataFrame) -> tuple[TemplateRow, ...]:
    """Return the report template with dynamic Butter format children."""
    butter_formats = _collect_butter_supply_formats(source_df)
    butter_rows = _build_dynamic_butter_rows(butter_formats)
    # Direct children of the Butter subtotal = the per-format subtotals (indent
    # 2); their Branded / Private Label leaves (indent 3) roll up beneath them.
    butter_child_ids = tuple(r.row_id for r in butter_rows if r.indent == 2)

    out: list[TemplateRow] = []
    for tpl in RO_SUMMARY_TEMPLATE:
        if tpl.row_id == "but":
            out.append(_subtotal("but", "Butter", 1, butter_child_ids))
            out.extend(butter_rows)
        else:
            out.append(tpl)
    return tuple(out)


def _template_by_id(template: tuple[TemplateRow, ...]) -> dict[str, TemplateRow]:
    """Return ``{row_id -> TemplateRow}`` for a runtime template."""
    return {row.row_id: row for row in template}


def _template_from_report_df(df: pd.DataFrame) -> tuple[TemplateRow, ...]:
    """Reconstruct a runtime template from an existing report frame.

    Used by :func:`recompute_subtotals` when the caller does not pass
    an explicit template. Dynamic Butter format subtotals are recovered
    from rows whose ``_row_id`` starts with ``but_sfmt_`` (excluding the
    ``_br`` / ``_pv`` Branded / Private Label leaves, which are synthesised
    deterministically beneath each format).
    """
    if df is None or df.empty or COL_ROW_ID not in df.columns:
        return RO_SUMMARY_TEMPLATE

    fmt_ids = [
        str(rid) for rid in df[COL_ROW_ID].tolist()
        if str(rid).startswith("but_sfmt_")
        and not (str(rid).endswith("_br") or str(rid).endswith("_pv"))
    ]
    if not fmt_ids:
        return RO_SUMMARY_TEMPLATE

    butter_rows: list[TemplateRow] = []
    for fid in fmt_ids:
        row = df.loc[df[COL_ROW_ID] == fid].iloc[0]
        # Format name lives in the (NBSP-indented) label; subtotal dims blank.
        label = str(row.get(COL_LABEL, "")).lstrip("\u00a0 ").strip()
        sfmt = label or str(row.get(COL_DIM_SFMT, "")).strip()
        br, pv = _butter_brand_leaves(fid, sfmt)
        butter_rows.append(_subtotal(fid, sfmt, 2, (br.row_id, pv.row_id)))
        butter_rows.extend((br, pv))

    butter_child_ids = tuple(r.row_id for r in butter_rows if r.indent == 2)
    out: list[TemplateRow] = []
    for tpl in RO_SUMMARY_TEMPLATE:
        if tpl.row_id == "but":
            out.append(_subtotal("but", "Butter", 1, butter_child_ids))
            out.extend(butter_rows)
        else:
            out.append(tpl)
    return tuple(out)


# ── Pure helpers — no I/O, no Streamlit ──────────────────────────────────────

def _derive_brand_category(brand: object) -> str:
    """Map our normalised ``Brand`` value to the screenshot's Brand Category.

    Rule per planner spec:
      * ``"Private"`` (the canonical form after PL/Pl normalisation in
        ``ro_comparison._normalize_brand``) → ``"Private"``
      * Everything else (including blanks) → ``"Branded"``
    """
    if str(brand).strip() == "Private":
        return "Private"
    return "Branded"


def _make_indented_label(label: str, indent: int) -> str:
    """Return the indented display string for the ``Millions of lbs.`` column.

    Uses non-breaking spaces (U+00A0) so Streamlit / browser whitespace
    collapsing doesn't flatten the hierarchy.  Two NBSPs per indent
    level — visually distinct without becoming oppressive on small
    screens.
    """
    return ("\u00A0\u00A0" * indent) + label


def _filter_for_leaf(df: pd.DataFrame, tpl: TemplateRow) -> pd.DataFrame:
    """Slice *df* down to the rows that satisfy *tpl*'s match criteria.

    Every constraint is optional (``None`` = "any value matches").
    Thin wrapper around :func:`_filter_for_leaf_with_trace` that
    discards the diagnostic trace — exposed because some callers
    (and tests) only need the filtered slice.
    """
    sub, _trace = _filter_for_leaf_with_trace(df, tpl)
    return sub


def _filter_for_leaf_with_trace(
    df: pd.DataFrame, tpl: TemplateRow,
) -> tuple[pd.DataFrame, list[tuple[str, int]]]:
    """Apply each match step incrementally and return ``(sub, trace)``.

    The trace is a list of ``(step_label, surviving_row_count)`` pairs
    in the order the filters were applied.  ``"start"`` reports the
    full input frame; subsequent entries report the row count AFTER
    each constraint, so a sudden drop pinpoints which dim is the
    upstream spelling mismatch.

    Filter order is intentional: PMaj → SFmt → PMinor → BrandCat.
    PMaj first because it's the broadest scope and a hit there means
    we're at least in the right portfolio family; SFmt next because
    it's the dim most prone to spelling drift between dp_dimitems and
    RO_History's ``Format`` fallback; the other two refine.
    """
    trace: list[tuple[str, int]] = [("start", len(df))]
    mask = pd.Series(True, index=df.index)

    if tpl.pmaj_match is not None:
        mask &= df[COL_DIM_PMAJ].isin(tpl.pmaj_match)
        trace.append((f"PMaj∈{sorted(tpl.pmaj_match)}", int(mask.sum())))
    if tpl.sfmt_match is not None:
        mask &= df[COL_DIM_SFMT] == tpl.sfmt_match
        trace.append((f"SFmt=='{tpl.sfmt_match}'", int(mask.sum())))
    if tpl.pminor_match is not None:
        mask &= df[COL_DIM_PMINOR] == tpl.pminor_match
        trace.append((f"PMinor=='{tpl.pminor_match}'", int(mask.sum())))
    if tpl.brand_match is not None:
        mask &= df["_brand_cat"] == tpl.brand_match
        trace.append((f"BrandCat=='{tpl.brand_match}'", int(mask.sum())))

    return df.loc[mask], trace


def _format_zero_match_warning(
    tpl: TemplateRow, trace: list[tuple[str, int]],
) -> str:
    """Build the human-readable warning string for a zero-match leaf.

    Uses the trace produced by :func:`_filter_for_leaf_with_trace` to
    pinpoint exactly which filter step dropped the row count to zero,
    so the planner can act on the actionable dim instead of guessing.
    """
    steps_str = " → ".join(f"{label}: {count}" for label, count in trace)

    # Identify the filter that dropped to zero (the first 0 after the
    # initial "start" entry); fall back to "start" if every step is 0.
    drop_step = next(
        (label for label, count in trace[1:] if count == 0),
        trace[0][0] if trace else "start",
    )
    return (
        f"**{tpl.row_id}** ({tpl.label}) matched 0 rows. "
        f"Filter trace: {steps_str}. "
        f"Most likely culprit: **{drop_step}** — confirm the literal "
        f"spelling in `RO_Comparison_Output.csv` for this combination "
        f"(check casing / trailing whitespace / alternate names)."
    )


def diag_dim_summary(comp_output_df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Return diagnostic frames showing what dim values the CSV contains.

    Used by the Diagnostic expander on the page so a planner can see
    the literal values present in ``RO_Comparison_Output.csv`` and
    reconcile them against the template's match criteria.

    Output keys:
      * ``"unique_pmaj"``   — Portfolio Major value → row count
      * ``"unique_sfmt"``   — Supply Format value → row count
      * ``"unique_pminor"`` — Portfolio Minor value → row count
      * ``"unique_brand"``  — Brand value → row count
      * ``"combo_pmaj_sfmt"`` — (PMaj, SFmt) → row count, sorted desc
      * ``"combo_full"``    — (PMaj, SFmt, PMinor, _brand_cat) → row count

    Empty / missing columns degrade to empty frames rather than
    raising — the diagnostic should always render something useful.
    """
    if comp_output_df is None or comp_output_df.empty:
        empty = pd.DataFrame(columns=["value", "rows"])
        return {
            "unique_pmaj":      empty.copy(),
            "unique_sfmt":      empty.copy(),
            "unique_pminor":    empty.copy(),
            "unique_brand":     empty.copy(),
            "combo_pmaj_sfmt":  pd.DataFrame(columns=[COL_DIM_PMAJ, COL_DIM_SFMT, "rows"]),
            "combo_full":       pd.DataFrame(
                columns=[COL_DIM_PMAJ, COL_DIM_SFMT, COL_DIM_PMINOR, "Brand Category", "rows"],
            ),
        }

    df = comp_output_df.copy()
    for col in (COL_DIM_PMAJ, COL_DIM_SFMT, COL_DIM_PMINOR):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].astype(str).where(df[col].notna(), "")
    if "Brand" not in df.columns:
        df["Brand"] = ""
    df["_brand_cat"] = df["Brand"].map(_derive_brand_category)

    def _value_counts(series: pd.Series) -> pd.DataFrame:
        """Frequency table sorted desc, blank values floated to top for visibility."""
        counts = series.value_counts(dropna=False).rename_axis("value").reset_index(name="rows")
        # Surface blanks at the top — they're the most common cause of "0 matches".
        counts["__is_blank"] = counts["value"].map(
            lambda v: 1 if (pd.isna(v) or str(v).strip() == "") else 0
        )
        counts = counts.sort_values(
            by=["__is_blank", "rows"], ascending=[False, False],
        ).drop(columns="__is_blank").reset_index(drop=True)
        return counts

    combo_pmaj_sfmt = (
        df.groupby([COL_DIM_PMAJ, COL_DIM_SFMT], dropna=False)
        .size().reset_index(name="rows")
        .sort_values(by="rows", ascending=False)
        .reset_index(drop=True)
    )
    combo_full = (
        df.groupby(
            [COL_DIM_PMAJ, COL_DIM_SFMT, COL_DIM_PMINOR, "_brand_cat"],
            dropna=False,
        )
        .size().reset_index(name="rows")
        .rename(columns={"_brand_cat": "Brand Category"})
        .sort_values(by="rows", ascending=False)
        .reset_index(drop=True)
    )

    return {
        "unique_pmaj":     _value_counts(df[COL_DIM_PMAJ]),
        "unique_sfmt":     _value_counts(df[COL_DIM_SFMT]),
        "unique_pminor":   _value_counts(df[COL_DIM_PMINOR]),
        "unique_brand":    _value_counts(df["Brand"]),
        "combo_pmaj_sfmt": combo_pmaj_sfmt,
        "combo_full":      combo_full,
    }


def _zero_values() -> dict[str, float]:
    """Return a dict of {data_col: 0.0} for every DATA_COL."""
    return {col: 0.0 for col in DATA_COLS}


def _compute_leaf_values(
    sub: pd.DataFrame,
    *,
    config: Optional[RoRulesConfig] = None,
) -> dict[str, float]:
    """Compute the 8 data column values for one already-filtered leaf slice.

    Pure aggregation — no template knowledge, no scaling to millions
    (that happens in :func:`build_summary_report` as a final pass so
    we don't accidentally double-scale subtotals).

    ``config`` (defaults to canonical rules) tunes the Risk carve-out only:
    Reflected-in-APS is already guaranteed upstream (RO_Seed filtered it),
    so the reflected column is not re-applied here.
    """
    if sub.empty:
        return _zero_values()

    # "Risk" = a probable demand loss — one canonical rule in data_sources.ro_risk:
    # LE Annual Opportunity < 0 AND LE Probability ≥ threshold.  Whatever its
    # New/Exit/Change Driver, a risk line's probabilized change is reported
    # under Risk instead, so New/Exit/Change EXCLUDE risk lines and
    # New + Exit + Change + Risk == Total Delta (unchanged).  Prior/Current
    # Plan and Total Delta are untouched; only the Delta Breakdown split shifts
    # with the user's Risk threshold.
    cfg = config
    is_risk = risk_mask(
        sub, volume_col=ANNUAL_OPP_LE, probability_col="LE Probability",
        min_probability=cfg.min_risk_probability if cfg is not None else None,
        require_negative_volume=(
            cfg.risk_requires_negative_volume if cfg is not None else True
        ),
    )
    risk_val   = float(sub.loc[is_risk, CUR_FISCAL_PROB_CHANGE].sum())
    new_val    = float(sub.loc[(sub["Driver"] == "New")    & ~is_risk, CUR_FISCAL_PROB_CHANGE].sum())
    exit_val   = float(sub.loc[(sub["Driver"] == "Exit")   & ~is_risk, CUR_FISCAL_PROB_CHANGE].sum())
    change_val = float(sub.loc[(sub["Driver"] == "Change") & ~is_risk, CUR_FISCAL_PROB_CHANGE].sum())

    prior_y1  = float(sub[YEAR1_PROB_PRIOR].sum())
    latest_y1 = float(sub[YEAR1_PROB_LE].sum())

    # FY28 Delta Breakdown — same Risk-first bucketing as FY27, but on the
    # ANNUALIZED delta (LE Year-1 Probabilized − Prior Year-1 Probabilized).
    # Every row lands in exactly one bucket, so
    # ``y1_new + y1_exit + y1_change + y1_risk == latest_y1 − prior_y1`` by
    # construction — the FY28 Change column stays consistent with its
    # breakdown across subtotals just like FY27's does.
    y1_delta = sub[YEAR1_PROB_LE] - sub[YEAR1_PROB_PRIOR]
    y1_risk_val   = float(y1_delta[is_risk].sum())
    y1_new_val    = float(y1_delta[(sub["Driver"] == "New")    & ~is_risk].sum())
    y1_exit_val   = float(y1_delta[(sub["Driver"] == "Exit")   & ~is_risk].sum())
    y1_change_val = float(y1_delta[(sub["Driver"] == "Change") & ~is_risk].sum())

    current_plan = float(sub[CUR_FISCAL_PROB_LE].sum())
    total_delta  = new_val + exit_val + change_val + risk_val

    return {
        # Prior Plan is the FY27 plan before this cycle's deltas.  Total
        # Delta = Current − Prior, so Prior = Current − Total Delta —
        # derived (no separate source column) and additive, so it rolls
        # up through subtotals exactly like the other columns.
        COL_PRIOR_PLAN:   current_plan - total_delta,
        COL_CURRENT_PLAN: current_plan,
        COL_TOTAL_DELTA:  total_delta,
        COL_DELTA_NEW:    new_val,
        COL_DELTA_EXIT:   exit_val,
        COL_DELTA_CHANGE: change_val,
        COL_DELTA_RISK:   risk_val,
        COL_Y1_PRIOR:     prior_y1,
        COL_Y1_CHANGE:    latest_y1 - prior_y1,
        COL_Y1_LATEST:    latest_y1,
        COL_Y1_DELTA_NEW:    y1_new_val,
        COL_Y1_DELTA_EXIT:   y1_exit_val,
        COL_Y1_DELTA_CHANGE: y1_change_val,
        COL_Y1_DELTA_RISK:   y1_risk_val,
    }


# ── Public pure transform ────────────────────────────────────────────────────

def build_summary_report(
    comp_output_df: pd.DataFrame,
    *,
    config: Optional[RoRulesConfig] = None,
) -> tuple[pd.DataFrame, list[str], tuple[TemplateRow, ...]]:
    """Build the summary report DataFrame from RO_Comparison_Output.

    Parameters
    ----------
    comp_output_df
        Raw ``RO_Comparison_Output.csv`` content as returned by
        :func:`fetch_ro_comparison_output_df`.  Must contain the
        column names produced by
        :data:`ro_comparison.OUTPUT_COLUMNS`.
    config
        Optional :class:`RoRulesConfig` overriding the canonical Risk
        classification rules for the ``Delta Breakdown | Risk`` column.
        ``None`` → planner defaults.  This is a **view-time filter**: the
        Opportunity gate (Reflected-in-APS, Pipeline Status, probability
        threshold) already applied upstream when RO_Seed was built, so the
        only thing tunable at this stage is the Risk carve-out.

    Returns
    -------
    df
        DataFrame in runtime template order.  Columns:
          * Internal meta (3): ``_row_id``, ``_indent``, ``_is_subtotal``
          * Dim display (4): ``Portfolio Major``, ``Supply Format``,
            ``Portfolio Minor``, ``Brand Category``
          * Label (1): ``Millions of lbs.`` (indented)
          * Data (8): the ``DATA_COLS`` (in millions, rounded to 1dp)
        Every row is present — even all-zero rows.  Callers that want
        to hide them must call :func:`drop_all_zero_rows`.
    warnings
        List of human-readable warning strings the page can surface
        above the table (e.g., "Column X missing from
        RO_Comparison_Output.csv — treated as 0").
    template
        Runtime template tuple (static rows + dynamic Butter format
        leaves).  Pass this to :func:`recompute_subtotals` so
        subtotal roll-ups stay consistent after planner edits.
    """
    warnings: list[str] = []

    if comp_output_df is None or comp_output_df.empty:
        return _empty_template_frame(), [
            "RO_Comparison_Output.csv is empty — nothing to roll up.",
        ], RO_SUMMARY_TEMPLATE

    runtime_template = _build_runtime_template(comp_output_df)
    template_by_id = _template_by_id(runtime_template)

    # ── Pre-process: derive Brand Category + coerce numerics ──────
    df = comp_output_df.copy()

    # Ensure dim columns are strings (CSV reader may return NaN cells).
    for col in DIM_COLS:
        if col in df.columns:
            df[col] = df[col].astype(str).where(df[col].notna(), "")
        else:
            # Tolerate a missing dim column rather than KeyError-ing —
            # surfaces as a leaf with 0 matches and produces a warning
            # via the matched-zero check below.
            df[col] = ""

    if "Driver" not in df.columns:
        df["Driver"] = ""
    df["Driver"] = df["Driver"].astype(str).where(df["Driver"].notna(), "")

    if "Brand" not in df.columns:
        df["Brand"] = ""
    df["_brand_cat"] = df["Brand"].map(_derive_brand_category)

    for col in (CUR_FISCAL_PROB_LE, CUR_FISCAL_PROB_CHANGE,
                YEAR1_PROB_PRIOR, YEAR1_PROB_LE):
        if col not in df.columns:
            df[col] = 0.0
            warnings.append(
                f"Column '{col}' missing from RO_Comparison_Output.csv — "
                "treated as 0."
            )
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # ── Compute leaf values once ──────────────────────────────────
    #
    # We deliberately do NOT emit a warning when a leaf matches 0
    # rows — those rows are hidden by :func:`drop_all_zero_rows`
    # before display, which is the planner's stated intent.  The
    # **🔬 Diagnostic** expander on the page surfaces the literal
    # dim values present in the data so a planner can spot real
    # spelling drift without wading through dozens of "no data this
    # month" notes.  The trace helpers
    # (:func:`_filter_for_leaf_with_trace` /
    # :func:`_format_zero_match_warning`) are kept module-private
    # for ad-hoc debugging — they may be re-wired into the page
    # later if the planner decides they want the per-leaf trace
    # behind a "verbose" toggle.
    leaf_vals: dict[str, dict[str, float]] = {}
    for tpl in runtime_template:
        if tpl.is_subtotal:
            continue
        sub = _filter_for_leaf(df, tpl)
        leaf_vals[tpl.row_id] = _compute_leaf_values(sub, config=config)

    # ── Roll up subtotals (memoised recursion) ────────────────────
    rollup: dict[str, dict[str, float]] = dict(leaf_vals)

    def _resolve(row_id: str) -> dict[str, float]:
        if row_id in rollup:
            return rollup[row_id]
        tpl = template_by_id[row_id]
        # Subtotal: sum across children's data columns.
        kids = [_resolve(c) for c in tpl.children]
        summed = {col: sum(k[col] for k in kids) for col in DATA_COLS}
        rollup[row_id] = summed
        return summed

    for tpl in runtime_template:
        _resolve(tpl.row_id)

    # ── Assemble output frame in template order ───────────────────
    rows: list[dict] = []
    for tpl in runtime_template:
        record = {
            COL_ROW_ID:      tpl.row_id,
            COL_INDENT:      tpl.indent,
            COL_IS_SUBTOTAL: tpl.is_subtotal,
            COL_DIM_PMAJ:    tpl.portfolio_major,
            COL_DIM_SFMT:    tpl.supply_format,
            COL_DIM_PMINOR:  tpl.portfolio_minor,
            COL_DIM_BCAT:    tpl.brand_category,
            COL_LABEL:       _make_indented_label(tpl.label, tpl.indent),
        }
        record.update(rollup[tpl.row_id])
        rows.append(record)

    out = pd.DataFrame(rows, columns=[
        *META_COLS, *DIM_COLS, COL_LABEL, *DATA_COLS,
    ])

    # ── Scale to millions + round to 1dp ──────────────────────────
    for col in DATA_COLS:
        out[col] = (out[col] / 1_000_000).round(1)

    return out, warnings, runtime_template


def _empty_template_frame(
    template: tuple[TemplateRow, ...] | None = None,
) -> pd.DataFrame:
    """Return a template frame with every data column zeroed.

    Used as the canonical "nothing loaded yet" fallback so the page
    can always render a stable shape even before the first fetch.
    """
    tpl_rows = template or RO_SUMMARY_TEMPLATE
    rows = []
    for tpl in tpl_rows:
        record = {
            COL_ROW_ID:      tpl.row_id,
            COL_INDENT:      tpl.indent,
            COL_IS_SUBTOTAL: tpl.is_subtotal,
            COL_DIM_PMAJ:    tpl.portfolio_major,
            COL_DIM_SFMT:    tpl.supply_format,
            COL_DIM_PMINOR:  tpl.portfolio_minor,
            COL_DIM_BCAT:    tpl.brand_category,
            COL_LABEL:       _make_indented_label(tpl.label, tpl.indent),
        }
        for col in DATA_COLS:
            record[col] = 0.0
        rows.append(record)
    return pd.DataFrame(rows, columns=[
        *META_COLS, *DIM_COLS, COL_LABEL, *DATA_COLS,
    ])


def recompute_subtotals(
    df: pd.DataFrame,
    template: tuple[TemplateRow, ...] | None = None,
) -> pd.DataFrame:
    """Recompute every subtotal row's data columns from its children.

    Walks the template hierarchy in dependency order (children before
    parents).  Called after the planner edits leaf cells in the
    ``st.data_editor`` so the displayed and saved subtotals always
    equal the sum of their (possibly user-overridden) children.

    Idempotent: calling this twice in a row leaves the frame unchanged.

    Parameters
    ----------
    df
        DataFrame produced by :func:`build_summary_report`, possibly
        with edited data-column values.  Must contain ``_row_id`` and
        all DATA_COLS.
    template
        Runtime template returned by :func:`build_summary_report`.
        When omitted, inferred from dynamic Butter rows present in
        *df* via :func:`_template_from_report_df`.

    Returns
    -------
    A new DataFrame (defensive copy) with the subtotal rows
    refreshed.  Leaf rows are untouched (planner edits preserved).
    """
    runtime_template = template or _template_from_report_df(df)
    template_by_id = _template_by_id(runtime_template)
    out = df.copy()
    # row_id → row index lookup.  We may end up with row_ids that are
    # missing if the caller dropped all-zero rows — those subtotals
    # simply have nothing to write back to, which is fine.
    idx_by_id: dict[str, int] = {
        row_id: idx for idx, row_id in zip(out.index, out[COL_ROW_ID])
    }

    def _resolve(row_id: str) -> dict[str, float]:
        tpl = template_by_id[row_id]
        idx = idx_by_id.get(row_id)
        if not tpl.is_subtotal:
            # Leaf: take whatever's currently in the row (may have been
            # edited by the planner).  Default to 0 when the row is
            # missing from the visible frame.
            if idx is None:
                return _zero_values()
            return {col: float(out.at[idx, col]) for col in DATA_COLS}
        # Subtotal: sum children and write back.
        kids = [_resolve(c) for c in tpl.children]
        summed = {col: sum(k[col] for k in kids) for col in DATA_COLS}
        if idx is not None:
            for col, val in summed.items():
                out.at[idx, col] = round(val, 1)
        return summed

    for tpl in runtime_template:
        if tpl.is_subtotal:
            _resolve(tpl.row_id)
    return out


def drop_all_zero_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Return *df* with rows whose every DATA_COL is zero removed.

    Comparison uses an absolute tolerance of 0.05 (half of the 1-decimal
    display precision) so a row that rounds to 0.0 but holds tiny
    floating-point noise is still considered zero.
    """
    if df.empty:
        return df
    nonzero_mask = (df[list(DATA_COLS)].abs() > 0.05).any(axis=1)
    return df.loc[nonzero_mask].copy()


# ── Streamlit-cached Fabric I/O ──────────────────────────────────────────────

@st.cache_data(ttl=_COMPARISON_OUTPUT_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_comparison_output_df(_signature: str) -> pd.DataFrame:
    """Cached read of ``RO_Comparison_Output.csv`` (the published comparison).

    Returns an empty DataFrame when the blob doesn't exist yet — the
    summary report can still render its template (all zeros, hidden by
    :func:`drop_all_zero_rows`) and surface a "publish the comparison
    first" hint.

    Raises :class:`RoSummaryReportError` only on a true I/O failure.
    """
    try:
        df, _etag = read_csv(_SECRETS_SECTION, _RO_COMPARISON_OUTPUT_BLOB_PATH)
    except LakehouseIOError as exc:
        raise RoSummaryReportError(
            "Could not read RO_Comparison_Output.csv from Microsoft Fabric: "
            f"{exc}"
        ) from exc

    if df is None:
        logger.info(
            "RO_Comparison_Output.csv does not exist yet at Files/%s — "
            "summary report will render empty template.",
            _RO_COMPARISON_OUTPUT_BLOB_PATH,
        )
        return pd.DataFrame()

    logger.info(
        "Loaded RO_Comparison_Output.csv: %s rows, %s columns.",
        len(df), len(df.columns),
    )
    return df


def fetch_ro_comparison_output_df(*, force_refresh: bool = False) -> pd.DataFrame:
    """Return the latest published ``RO_Comparison_Output.csv``.

    Parameters
    ----------
    force_refresh : bool, default False
        When True, clears this connector's Streamlit cache before
        reading.  Wired to the section's "🔄 Refresh from Fabric"
        button so the planner can re-pull immediately after saving
        the comparison editor (without waiting for the 15-min TTL).
    """
    if force_refresh:
        _cached_comparison_output_df.clear()
    return _cached_comparison_output_df("default")


def clear_comparison_output_cache() -> None:
    """Invalidate the cached ``RO_Comparison_Output.csv`` snapshot.

    Exposed as a public function (rather than reaching into the
    cached impl from the page) so callers don't need to know that
    :func:`fetch_ro_comparison_output_df` is a thin wrapper around a
    ``@st.cache_data``-decorated inner function.  The next call to
    :func:`fetch_ro_comparison_output_df` (or the next render of the
    summary report fragment after its session entry is popped) will
    re-read from Fabric.

    Called by the RO Comparison Save handler so the summary report
    automatically reflects the just-saved data on its next refresh.
    """
    _cached_comparison_output_df.clear()


# ── Export / download shape (shared with Fabric save) ────────────────────────
#
# The page's ``st.download_button`` and :func:`save_ro_summary_report`
# must emit the *same* column set, order, and header labels so a
# planner who downloads locally and later hits Save publishes an
# identical file.  All shaping lives here — the view only calls
# :func:`summary_to_csv_bytes`.

# Left-to-right order for ``RO_Summary_Report.csv`` (dims → label → metrics).
_EXPORT_COLUMN_ORDER: tuple[str, ...] = (
    *DIM_COLS,
    COL_LABEL,
    *DATA_COLS,
)


def prepare_summary_for_export(df: pd.DataFrame) -> pd.DataFrame:
    """Return *df* shaped for Fabric save or local CSV download.

    Transformations (applied in order)
    --------------------------------
    1. Drop internal metadata columns (:data:`META_COLS`) — the page
       uses these for subtotal rollup and indent styling only.
    2. Rename internal data-column IDs to the grouped display labels
       in :data:`SAVED_COLUMN_LABELS` (matches the planner's Excel
       screenshot and the published Fabric file).
    3. Reorder columns to :data:`_EXPORT_COLUMN_ORDER` so every
       export has a stable shape regardless of pandas column order.

    The returned frame is the FULL template (all 30 rows when built
    from the standard template), including all-zero leaves — same
    contract as :func:`save_ro_summary_report`.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    drop_cols = [c for c in META_COLS if c in out.columns]
    if drop_cols:
        out = out.drop(columns=drop_cols)

    out = out.rename(columns=SAVED_COLUMN_LABELS)

    # After rename, dim + label columns keep their display names; only
    # the eight metric columns pick up the grouped ``|`` labels.
    ordered_labels: list[str] = [
        SAVED_COLUMN_LABELS.get(col, col) for col in _EXPORT_COLUMN_ORDER
    ]
    present = [c for c in ordered_labels if c in out.columns]
    extra = [c for c in out.columns if c not in present]
    return out.loc[:, present + extra].copy()


def summary_for_download(df: pd.DataFrame) -> pd.DataFrame:
    """Public entry: prepare the in-memory report for a CSV download.

    Thin alias over :func:`prepare_summary_for_export` so the page
    mirrors the ``pivot_for_download`` pattern in ``demand_summary.py``.
    """
    return prepare_summary_for_export(df)


def summary_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Serialise the export-shaped report to UTF-8 CSV bytes.

    Intended for ``st.download_button`` on the Demand Planner
    Analytics page — HTTP download, not WebSocket, so there is no
    row-count cap beyond what the browser can accept (the template
    is fixed at ~30 rows).
    """
    export_df = prepare_summary_for_export(df)
    if export_df.empty:
        return b""
    return export_df.to_csv(index=False).encode("utf-8")


def save_ro_summary_report(df: pd.DataFrame) -> str:
    """Overwrite ``Files/RO Tracking/RO_Reporting/RO_Summary_Report.csv``.

    The saved CSV contains the FULL 30-row template (subtotals + every
    leaf, including all-zero rows) so downstream consumers get a
    stable shape every month.  Shaping is delegated to
    :func:`prepare_summary_for_export` so the Fabric file matches a
    local download byte-for-byte (modulo line-ending normalisation by
    the storage layer).

    Returns the destination blob path.  Raises
    :class:`RoSummaryReportError` on any underlying write failure.
    """
    out = prepare_summary_for_export(df)

    try:
        write_csv(
            _SECRETS_SECTION, _RO_SUMMARY_REPORT_BLOB_PATH, out, etag=None,
        )
    except LakehouseIOError as exc:
        raise RoSummaryReportError(
            "Could not save RO_Summary_Report.csv to "
            f"'Files/{_RO_SUMMARY_REPORT_BLOB_PATH}': {exc}"
        ) from exc
    return _RO_SUMMARY_REPORT_BLOB_PATH


# ── Re-export contract ──────────────────────────────────────────────────────

__all__ = [
    # Errors
    "RoSummaryReportError",
    # Template + columns
    "RO_SUMMARY_TEMPLATE",
    "TemplateRow",
    "TEMPLATE_BY_ID",
    "DATA_COLS",
    "DIM_COLS",
    "META_COLS",
    "SAVED_COLUMN_LABELS",
    "COL_PRIOR_PLAN", "COL_CURRENT_PLAN", "COL_TOTAL_DELTA",
    "COL_DELTA_NEW", "COL_DELTA_EXIT", "COL_DELTA_CHANGE",
    "COL_Y1_PRIOR", "COL_Y1_CHANGE", "COL_Y1_LATEST",
    "COL_Y1_DELTA_NEW", "COL_Y1_DELTA_EXIT",
    "COL_Y1_DELTA_CHANGE", "COL_Y1_DELTA_RISK",
    "COL_LABEL",
    "COL_DIM_PMAJ", "COL_DIM_SFMT", "COL_DIM_PMINOR", "COL_DIM_BCAT",
    "COL_ROW_ID", "COL_INDENT", "COL_IS_SUBTOTAL",
    # Pure transforms
    "build_summary_report",
    "recompute_subtotals",
    "drop_all_zero_rows",
    # Diagnostics (used by the page's Diagnostic expander)
    "diag_dim_summary",
    # Export (download + Fabric save share the same shape)
    "prepare_summary_for_export",
    "summary_for_download",
    "summary_to_csv_bytes",
    # Fabric I/O
    "clear_comparison_output_cache",
    "fetch_ro_comparison_output_df",
    "save_ro_summary_report",
]
