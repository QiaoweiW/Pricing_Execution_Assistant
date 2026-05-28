"""Early-Start-Date Programs — drilldown of the published RO comparison.

This module owns the small "programs starting before a cutoff" table
that renders between the per-Format driver table and the RO Summary
Report on the Demand Planner Analytics page.  Like
:mod:`data_sources.ro_summary_report`, it is a *pure consumer* of the
published ``RO_Comparison_Output.csv`` blob in Microsoft Fabric — it
does NOT touch the in-memory comparison frame.  This means the
section always reflects the planner's last *saved* baseline (their
"approved" view), independent of any unsaved edits in the editor
above.

What it shows
-------------
One row per RO whose **LE First Ship Date** falls strictly before a
planner-chosen cutoff date.  The table is intentionally narrow so a
planner can eyeball the list of programs scheduled to start "by"
some date — typical use case: *"which programs are supposed to have
shipped before end-of-quarter?  Why don't I see them in the shipment
data yet?"*

Programs at **LE Probability == 100 %** are always excluded — they're
locked-in commitments and don't belong in a watchlist of items still
in flux.  Rows with missing / unparseable probabilities are kept
(they're "uncertain", not "locked in").

Output columns (4)
------------------
* ``Format``                       — pass-through of the upstream
                                     ``Format`` value from RO_History.
                                     Same column the RO Comparison
                                     editor and the per-Format driver
                                     table key off, so the planner
                                     can cross-reference at a glance.
* ``Program``                      — single-line composite identifier:
                                     ``"{Customer} — {Item #}
                                     {Description} — Prob
                                     {LE Probability}"``.  Blank
                                     components are dropped (em-dash
                                     separator matches the per-Format
                                     driver-cell pattern in
                                     :func:`ro_comparison._format_driver_cell`).
* ``LE Annual Opportunity (lbs)``  — numeric, sortable in the table
                                     (click the column header) and
                                     filterable through the
                                     ``min_le_annual_opp`` argument
                                     (page widget: number input).
                                     Aliased from
                                     :data:`ro_comparison.ANNUAL_OPP_LE`
                                     so the column name / format
                                     match the rest of the page.
* ``Start Date``                   — ``LE First Ship Date`` coerced
                                     to a Python ``date`` for
                                     ordering and ``DateColumn``
                                     display.

Rows are sorted ``(Start Date asc, Format asc, Item # asc)`` so the
earliest-shipping programs surface first.

Filter semantics
----------------
* ``formats_filter``     (page widget: multiselect, empty = no constraint).
* ``before_date``        (page widget: date picker, default = today).
                         Rows with ``LE First Ship Date >= before_date``
                         OR a missing/unparseable Start Date are
                         dropped — they don't belong in a "programs
                         with a start date before X" report.
* ``min_le_annual_opp``  (page widget: number input, default = 0).
                         Drops rows whose ``LE Annual Opportunity
                         (lbs)`` is below the threshold.  Missing /
                         unparseable values are treated as 0 so a
                         positive threshold filters them out
                         alongside genuinely small programs.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Iterable, Optional

import pandas as pd

from data_sources.ro_comparison import ANNUAL_OPP_LE


logger = logging.getLogger(__name__)


# ── Output column identifiers ────────────────────────────────────────────────

COL_FORMAT: str         = "Format"
COL_PROGRAM: str        = "Program"
# Aliased to the canonical constant from ``ro_comparison`` so the
# output frame's column name matches the rest of the codebase (one
# canonical spelling per concept).
COL_LE_ANNUAL_OPP: str  = ANNUAL_OPP_LE
COL_START_DATE: str     = "Start Date"

OUTPUT_COLUMNS: tuple[str, ...] = (
    COL_FORMAT, COL_PROGRAM, COL_LE_ANNUAL_OPP, COL_START_DATE,
)


# ── Input column identifiers (subset of RO_Comparison_Output.csv) ────────────
#
# Kept as module-private constants so a downstream rename in
# ``ro_comparison.OUTPUT_COLUMNS`` doesn't silently break this report
# — a grep for ``_INPUT_*`` from this module surfaces every coupling
# in one pass.  ``ANNUAL_OPP_LE`` is imported above (re-exported as
# :data:`COL_LE_ANNUAL_OPP`) and used both for filtering and as the
# output column name.
_INPUT_FORMAT: str        = "Format"
_INPUT_CUSTOMER: str      = "Customer"
_INPUT_ITEM_NUM: str      = "Item #"
_INPUT_DESCRIPTION: str   = "Description"
_INPUT_LE_PROB: str       = "LE Probability"
_INPUT_LE_FIRST_SHIP: str = "LE First Ship Date"

# Probability values are rounded to 2dp upstream in
# ``ro_comparison._recompute_derived_columns``, so a strict
# ``>= 1.0`` test reliably catches "100 %" without floating-point
# false negatives.  ``NaN >= 1.0`` is False, so rows with missing
# probabilities survive the filter (they're "uncertain", not
# "locked in" — see module docstring).
_PROB_LOCKED_THRESHOLD: float = 1.0


# ── Pure helpers — no I/O, no Streamlit ──────────────────────────────────────

def _stringify(value: object) -> str:
    """Return *value* as a stripped string ('' for null / blank cells).

    Tolerates the three forms a CSV reader emits for blanks:
    ``None`` (from explicit nullable types), ``float('nan')`` (the
    pandas default for empty numeric cells), and the literal string
    ``"nan"`` (from object columns that got cast).
    """
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _format_probability(value: object) -> str:
    """Return ``"Prob {p:.2f}"`` for numeric *value*, or '' for missing.

    Two-decimal precision matches the rest of the RO section (see
    ``_ro_column_config`` in the page) so the planner reads the
    same number here and in the editor above.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    if pd.isna(f):
        return ""
    return f"Prob {f:.2f}"


def _compose_program(row: pd.Series) -> str:
    """Build the single-line ``Program`` identifier for one comparison row.

    Shape mirrors the per-Format driver-cell pattern in
    :func:`ro_comparison._format_driver_cell` — em-dash joined parts,
    blank parts dropped so partial rows still produce a readable
    line.  When every component is blank the cell falls back to
    ``"?"`` so the row is still discoverable in the table.

    LE Annual Opportunity (lbs) is intentionally NOT part of this
    string — it lives in its own sortable / filterable numeric column
    so the planner can rank programs by size without parsing text.
    """
    customer = _stringify(row.get(_INPUT_CUSTOMER))
    item_num = _stringify(row.get(_INPUT_ITEM_NUM))
    description = _stringify(row.get(_INPUT_DESCRIPTION))
    item_part = f"{item_num} {description}".strip()
    prob_part = _format_probability(row.get(_INPUT_LE_PROB))

    parts = [p for p in (customer, item_part, prob_part) if p]
    return " — ".join(parts) if parts else "?"


def _empty_output_frame() -> pd.DataFrame:
    """Return a zero-row DataFrame with the canonical column shape.

    Used as the "nothing to display" fallback so the page can always
    render a stable table — Streamlit's ``column_config`` lookup is
    column-order-sensitive and would silently elide a column if the
    empty frame had a different shape.
    """
    return pd.DataFrame(columns=list(OUTPUT_COLUMNS))


# ── Public pure transforms ───────────────────────────────────────────────────

def list_available_formats(comp_df: pd.DataFrame) -> list[str]:
    """Return sorted unique non-blank ``Format`` values from *comp_df*.

    Used by the page to populate the Format multiselect.  Tolerant
    of a missing / empty input — returns ``[]`` so the widget
    degrades to "no options" rather than raising.
    """
    if comp_df is None or comp_df.empty or _INPUT_FORMAT not in comp_df.columns:
        return []
    values = {_stringify(v) for v in comp_df[_INPUT_FORMAT].dropna().tolist()}
    values.discard("")
    return sorted(values)


def build_early_start_programs_table(
    comp_df: pd.DataFrame,
    *,
    formats_filter: Optional[Iterable[str]] = None,
    before_date: Optional[date] = None,
    min_le_annual_opp: Optional[float] = None,
) -> pd.DataFrame:
    """Return a ``(Format, Program, LE Annual Opportunity (lbs), Start Date)``
    table from *comp_df*.

    Parameters
    ----------
    comp_df
        Published RO comparison frame (the same shape as
        :data:`ro_comparison.OUTPUT_COLUMNS`).  Pass an empty / None
        frame for the "no data published yet" case — returns a
        zero-row frame with the canonical column shape so the page
        can still render a stable empty table.
    formats_filter
        Iterable of Format strings.  When ``None`` or empty, no
        Format constraint applies.  Whitespace-stripped comparison;
        empty strings inside the iterable are ignored.
    before_date
        Cutoff date.  Only rows with ``LE First Ship Date <
        before_date`` are kept.  Rows with a missing / unparseable
        Start Date are ALWAYS dropped — they don't belong in a
        "programs with a start date before X" report.  Passing
        ``None`` disables the date filter (useful for tests).
    min_le_annual_opp
        Optional minimum LE Annual Opportunity (lbs).  Rows whose
        value is **below** the threshold (or missing / unparseable,
        which we treat as 0) are dropped.  ``None`` or 0 disables
        the filter and every row passes regardless of Opp value.

    Always-on filter
    ----------------
    Rows with ``LE Probability >= 1.0`` (= "100 %" — locked-in
    commitments) are dropped unconditionally.  Rows with a
    missing / unparseable probability are kept — ``NaN >= 1.0`` is
    False, so the mask treats them as "uncertain, not locked-in".

    Returns
    -------
    pandas.DataFrame
        Four-column frame sorted ``(Start Date asc, Format asc,
        Item # asc)`` and re-indexed 0..N-1 so it renders cleanly
        in ``st.dataframe``.
    """
    if comp_df is None or comp_df.empty:
        return _empty_output_frame()

    if _INPUT_LE_FIRST_SHIP not in comp_df.columns:
        # Defensive — should never happen for a real comparison output
        # blob, but a manually-crafted CSV could be missing the column.
        # Log + return empty rather than KeyError-ing the page.
        logger.warning(
            "RO_Comparison_Output.csv is missing the '%s' column — "
            "Early-Start-Date Programs table will be empty.",
            _INPUT_LE_FIRST_SHIP,
        )
        return _empty_output_frame()

    # ── Parse the Start Date column (accept datetime, date, or the
    #    YYYY-MM-DD string emitted by ``save_ro_comparison_output``)
    start_dt = pd.to_datetime(comp_df[_INPUT_LE_FIRST_SHIP], errors="coerce")

    # ── Coerce LE Annual Opportunity (lbs) once.  Used both for the
    #    optional ``min_le_annual_opp`` filter AND as the value of
    #    the output column, so doing it here keeps the two perfectly
    #    in sync (any future tweak to coercion only touches one line).
    opp_numeric = pd.to_numeric(
        comp_df.get(ANNUAL_OPP_LE, pd.Series(0.0, index=comp_df.index)),
        errors="coerce",
    ).fillna(0.0)

    # ── Coerce LE Probability for the always-on "drop 100 %" filter.
    if _INPUT_LE_PROB in comp_df.columns:
        prob_numeric = pd.to_numeric(comp_df[_INPUT_LE_PROB], errors="coerce")
    else:
        # No probability column → can't filter; behave as if every
        # row's probability were unknown (kept).
        prob_numeric = pd.Series(float("nan"), index=comp_df.index)

    # Combined "row survives" mask, AND-ed step by step so a future
    # debugger can drop a print before each line to pinpoint a drop.
    keep_mask = start_dt.notna()
    # ``NaN >= threshold`` is False → ~False = True → NaN rows survive.
    keep_mask &= ~(prob_numeric >= _PROB_LOCKED_THRESHOLD)

    if before_date is not None:
        before_ts = pd.Timestamp(before_date)
        keep_mask &= start_dt < before_ts

    if formats_filter:
        wanted = {str(f).strip() for f in formats_filter if str(f).strip()}
        if wanted:
            fmt_series = (
                comp_df.get(_INPUT_FORMAT, pd.Series("", index=comp_df.index))
                .astype(str).str.strip()
            )
            keep_mask &= fmt_series.isin(wanted)

    if min_le_annual_opp is not None and min_le_annual_opp > 0:
        keep_mask &= opp_numeric >= float(min_le_annual_opp)

    surviving = comp_df.loc[keep_mask]
    if surviving.empty:
        return _empty_output_frame()

    # Build the four output columns.  We assemble them in a fresh
    # DataFrame (rather than mutating ``surviving``) so we never
    # accidentally leak a heavier copy of the source frame downstream.
    out = pd.DataFrame(index=surviving.index)
    out[COL_FORMAT]         = surviving[_INPUT_FORMAT].astype(str).map(_stringify)
    out[COL_PROGRAM]        = surviving.apply(_compose_program, axis=1)
    out[COL_LE_ANNUAL_OPP]  = opp_numeric.loc[surviving.index]
    out[COL_START_DATE]     = start_dt.loc[surviving.index].dt.date

    # Stable sort: earliest Start Date first; ties → Format asc →
    # Item # asc.  ``mergesort`` is stable and O(n log n).  Note the
    # planner can re-sort interactively in ``st.dataframe`` (e.g.,
    # click the LE Annual Opp header for largest-first); this is
    # the *initial* order on render.
    sort_helper = pd.DataFrame({
        "_d":    start_dt.loc[surviving.index].values,
        "_fmt":  out[COL_FORMAT].values,
        "_item": surviving.get(_INPUT_ITEM_NUM, "").astype(str).values,
    }, index=surviving.index)
    order = (
        sort_helper.sort_values(by=["_d", "_fmt", "_item"], kind="mergesort").index
    )
    return out.loc[order].reset_index(drop=True)


# ── Re-export contract ──────────────────────────────────────────────────────

__all__ = [
    "COL_FORMAT", "COL_PROGRAM", "COL_LE_ANNUAL_OPP", "COL_START_DATE",
    "OUTPUT_COLUMNS",
    "build_early_start_programs_table",
    "list_available_formats",
]
