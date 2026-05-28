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

Output columns (3)
------------------
* ``Format``       — pass-through of the upstream ``Format`` value
                     from RO_History.  Same column the RO Comparison
                     editor and the per-Format driver table key off,
                     so the planner can cross-reference at a glance.
* ``Program``      — single-line composite identifier:
                     ``"{Customer} — {Item #} {Description} —
                     Prob {LE Probability} — LE Annual Opp
                     {LE Annual Opportunity (lbs)} lbs"``.  Blank
                     components are dropped (em-dash separator
                     matches the per-Format driver-cell pattern in
                     :func:`ro_comparison._format_driver_cell`).
* ``Start Date``   — ``LE First Ship Date`` coerced to a Python
                     ``date`` for ordering and ``DateColumn`` display.

Rows are sorted ``(Start Date asc, Format asc, Item # asc)`` so the
earliest-shipping programs surface first.

Filter semantics
----------------
* ``formats_filter`` (page widget: multiselect, empty = no constraint).
* ``before_date``    (page widget: date picker, default = today).
                     Rows with ``LE First Ship Date >= before_date``
                     OR a missing/unparseable Start Date are dropped
                     — they don't belong in a "programs with a start
                     date before X" report.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Iterable, Optional

import pandas as pd

from data_sources.ro_comparison import ANNUAL_OPP_LE


logger = logging.getLogger(__name__)


# ── Output column identifiers ────────────────────────────────────────────────

COL_FORMAT: str     = "Format"
COL_PROGRAM: str    = "Program"
COL_START_DATE: str = "Start Date"

OUTPUT_COLUMNS: tuple[str, ...] = (COL_FORMAT, COL_PROGRAM, COL_START_DATE)


# ── Input column identifiers (subset of RO_Comparison_Output.csv) ────────────
#
# Kept as module-private constants so a downstream rename in
# ``ro_comparison.OUTPUT_COLUMNS`` doesn't silently break this report
# — a grep for ``_INPUT_*`` from this module surfaces every coupling
# in one pass.
_INPUT_FORMAT: str        = "Format"
_INPUT_CUSTOMER: str      = "Customer"
_INPUT_ITEM_NUM: str      = "Item #"
_INPUT_DESCRIPTION: str   = "Description"
_INPUT_LE_PROB: str       = "LE Probability"
_INPUT_LE_FIRST_SHIP: str = "LE First Ship Date"
# ``ANNUAL_OPP_LE`` is re-used as the source for the program's LE
# Annual Opportunity (lbs) component.  Imported from ro_comparison
# above so we stay in lock-step with the canonical column name.


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


def _format_annual_opp(value: object) -> str:
    """Return ``"LE Annual Opp {n:,} lbs"`` for numeric *value*, or ''.

    Whole-pound display (no decimals) + thousands separators matches
    the ``format="accounting"`` style used by every other Lbs column
    on this page.  Negative values are tolerated and rendered with
    a leading minus sign.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    if pd.isna(f):
        return ""
    return f"LE Annual Opp {int(round(f)):,} lbs"


def _compose_program(row: pd.Series) -> str:
    """Build the single-line ``Program`` identifier for one comparison row.

    Shape mirrors the per-Format driver-cell pattern in
    :func:`ro_comparison._format_driver_cell` — em-dash joined parts,
    blank parts dropped so partial rows still produce a readable
    line.  When every component is blank the cell falls back to
    ``"?"`` so the row is still discoverable in the table.
    """
    customer = _stringify(row.get(_INPUT_CUSTOMER))
    item_num = _stringify(row.get(_INPUT_ITEM_NUM))
    description = _stringify(row.get(_INPUT_DESCRIPTION))
    item_part = f"{item_num} {description}".strip()
    prob_part = _format_probability(row.get(_INPUT_LE_PROB))
    opp_part  = _format_annual_opp(row.get(ANNUAL_OPP_LE))

    parts = [p for p in (customer, item_part, prob_part, opp_part) if p]
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
) -> pd.DataFrame:
    """Return a ``(Format, Program, Start Date)`` table from *comp_df*.

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

    Returns
    -------
    pandas.DataFrame
        Three-column frame sorted ``(Start Date asc, Format asc,
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

    # Combined "row survives" mask, AND-ed step by step so a future
    # debugger can drop a print before each line to pinpoint a drop.
    keep_mask = start_dt.notna()

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

    surviving = comp_df.loc[keep_mask]
    if surviving.empty:
        return _empty_output_frame()

    # Build the three output columns.  We assemble them in a fresh
    # DataFrame (rather than mutating ``surviving``) so we never
    # accidentally leak a heavier copy of the source frame downstream.
    out = pd.DataFrame(index=surviving.index)
    out[COL_FORMAT]     = surviving[_INPUT_FORMAT].astype(str).map(_stringify)
    out[COL_PROGRAM]    = surviving.apply(_compose_program, axis=1)
    out[COL_START_DATE] = start_dt.loc[surviving.index].dt.date

    # Stable sort: earliest Start Date first; ties → Format asc →
    # Item # asc.  ``mergesort`` is stable and O(n log n).
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
    "COL_FORMAT", "COL_PROGRAM", "COL_START_DATE",
    "OUTPUT_COLUMNS",
    "build_early_start_programs_table",
    "list_available_formats",
]
