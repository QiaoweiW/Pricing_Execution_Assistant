"""Bridge the demand-plan INPUTS to the published ``qry_*`` outputs.

Motivation
----------
``qry_mgmt_plan_full.csv`` / ``qry_total_item_level_demand.csv`` are built from
``ibp_base_plan_current.csv`` (the Base Plan leg) and ``RO_Seed.csv`` (the R&O
leg).  Along the way the pipeline drops rows for four legitimate reasons — see
:data:`data_sources.demand_plan_pipeline.ROW_GATES` — and until now it did so
silently.  A planner comparing the upload against the outputs saw a gap with no
way to attribute it, and no way to tell a correct exclusion (a genuine B2B SKU)
from a data-quality problem (a SKU missing from RO_Item_Master).

This module makes that gap addressable.  It answers three questions:

1. **Where did the pounds go?**  A waterfall from input lbs to output lbs, one
   step per gate.
2. **Which SKUs, and why?**  Per-item detail for every dropped row, carrying
   the gate's reason and the concrete fix.
3. **Does the published file still match its inputs?**  Recomputing from the
   current inputs and diffing against what is actually on the lakehouse catches
   the other failure mode — a stale or partially-written output.

A second, independent bridge reconciles the R&O leg against the RO Summary's
``FY27 Probabilized | Current Plan``, which travels through a different
pipeline (``RO_Comparison_Output.csv``) and so can disagree for reasons the
gate ledger cannot explain.

Design
------
* **No I/O, no Streamlit.**  Callers hand in already-loaded DataFrames — same
  contract as :mod:`data_sources.ro_risk_reconcile`, so this is unit-testable
  and reusable from a notebook or a CLI.
* **No second copy of the rules.**  The bridge does not re-implement the
  pipeline's filters; it *runs the pipeline* (:func:`demand_plan_pipeline.
  _build_mgmt_plan_and_detail`) and reads the gate ledger that run produces.
  A rule change therefore updates the bridge automatically, and the two can
  never disagree about why a SKU vanished.
* **Item-level grain.**  Drops are reported per (Item, gate) — the grain a
  planner acts on — with row counts and lbs so a big-ticket exclusion is
  obvious next to a rounding-scale one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

# Private pipeline helpers are imported deliberately: this module must run the
# SAME code the pipeline runs, not a copy of it.  ``aps_upload_pipeline`` and
# ``holistic_demand_plan_aps`` already reach into these for the same reason.
from .demand_plan_pipeline import (
    GATE_COL,
    ROW_GATES,
    ROW_GATES_BY_ID,
    _build_mgmt_plan_and_detail,
    _build_tbl_ro_input,
    _DEFAULT_ANCHOR_MONTH,
    _Log,
    _N_MONTHS,
    _norm_item,
    _SEED_COLUMNS,
)
from .ro_comparison import CUR_FISCAL_PROB_LE


# ── Column contracts ─────────────────────────────────────────────────────────
#
# Output column names for the two detail frames.  Module constants so the view
# renders them by name without restating string literals.
COL_ITEM: str = "Item"
COL_DESC: str = "Item Description"
COL_FORECAST: str = "Forecast Type"
COL_GATE: str = "Dropped by"
COL_ROWS: str = "Rows"
COL_LBS: str = "Lbs"
COL_REASON: str = "Why"
COL_FIX: str = "How to fix"

DROP_DETAIL_COLUMNS: tuple[str, ...] = (
    COL_ITEM, COL_DESC, COL_FORECAST, COL_GATE, COL_ROWS, COL_LBS,
    COL_REASON, COL_FIX,
)

# RO bridge columns.
COL_RO_SUMMARY_LBS: str = "RO Summary FY lbs"
COL_PLAN_LBS: str = "Plan R&O FY lbs"
COL_DELTA: str = "Delta"
COL_STATUS: str = "Status"

RO_BRIDGE_COLUMNS: tuple[str, ...] = (
    COL_ITEM, COL_RO_SUMMARY_LBS, COL_PLAN_LBS, COL_DELTA, COL_STATUS, COL_FIX,
)

# Lbs below which a delta is treated as pro-ration / rounding noise rather than
# a missing SKU.  The RO Summary stores values rounded to 0.1 M, so anything
# under a few thousand pounds cannot be meaningfully attributed.
_RO_NOISE_LBS: float = 5_000.0


# ── Waterfall ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BridgeStep:
    """One rung of the input → output waterfall."""
    gate_id: str
    label: str
    rows: int
    lbs: float
    items: int
    reason: str
    fix: str


@dataclass(frozen=True)
class DemandPlanBridge:
    """Input → published-output reconciliation for one demand-plan build.

    ``input_*`` are the Base Plan + R&O rows as they enter the row gates;
    ``output_*`` are what reached ``qry_mgmt_plan_full``.  ``steps`` accounts
    for every pound of the difference, so::

        input_lbs - sum(step.lbs for step in steps) == output_lbs

    ``published_lbs`` is what is actually on the lakehouse right now.
    ``drift_lbs`` (published − recomputed) is non-zero only when the file on
    disk no longer matches its own inputs — a stale or partial write, which the
    gate ledger cannot explain and which needs a re-run rather than a data fix.
    ``published_lbs`` is ``None`` when the caller had no published file to
    compare against (e.g. it was withdrawn).
    """
    input_rows: int
    input_lbs: float
    output_rows: int
    output_lbs: float
    steps: tuple[BridgeStep, ...]
    dropped_detail: pd.DataFrame
    #: The freshly rebuilt plan.  Carried so the R&O bridge can reconcile
    #: against it when nothing is published (e.g. after a withdraw).
    rebuilt: pd.DataFrame
    published_rows: Optional[int] = None
    published_lbs: Optional[float] = None

    @property
    def dropped_lbs(self) -> float:
        """Total pounds removed by the gates."""
        return float(sum(s.lbs for s in self.steps))

    @property
    def drift_lbs(self) -> Optional[float]:
        """Published − recomputed lbs; ``None`` when nothing was published."""
        if self.published_lbs is None:
            return None
        return float(self.published_lbs) - float(self.output_lbs)

    @property
    def ties(self) -> bool:
        """True when the published file matches what its inputs rebuild to."""
        drift = self.drift_lbs
        return drift is not None and abs(drift) <= 1.0     # 1 lb float noise


def _lbs(series: pd.Series) -> pd.Series:
    """Coerce a pounds column to float (blank / bad values → 0.0)."""
    return pd.to_numeric(
        series.astype(str).str.replace(",", "", regex=False), errors="coerce",
    ).fillna(0.0)


def _sum_lbs(df: Optional[pd.DataFrame], column: str = "Demand Plan Pounds") -> float:
    """Total pounds in *df*, or 0.0 for a missing / empty frame."""
    if df is None or df.empty or column not in df.columns:
        return 0.0
    return float(_lbs(df[column]).sum())


def build_demand_plan_bridge(
    base_plan: pd.DataFrame,
    ro_seed: pd.DataFrame,
    tbl_months: pd.DataFrame,
    pdh: pd.DataFrame,
    ro_master: pd.DataFrame,
    *,
    window_end: pd.Timestamp,
    anchor_month: date = _DEFAULT_ANCHOR_MONTH,
    published_mgmt_full: Optional[pd.DataFrame] = None,
) -> DemandPlanBridge:
    """Rebuild the demand plan from *inputs* and explain every pound it lost.

    Runs the real pipeline stages (so the answer is by construction the same one
    the next upload will produce) and turns the gate ledger into a waterfall
    plus per-SKU detail.  The R&O leg is expanded from *ro_seed* through the
    pipeline's own Format×Month helper, so callers hand in raw source files and
    no intermediate is built twice.  *published_mgmt_full* is optional: supply
    the file currently on the lakehouse to also detect drift between it and its
    inputs.
    """
    log = _Log()
    tbl_ro_input = _expand_ro_seed(ro_seed, anchor_month, log)
    mgmt_full, _detail, ledger = _build_mgmt_plan_and_detail(
        base_plan, tbl_ro_input, tbl_months, pdh, ro_master,
        window_end=window_end, log=log,
    )

    output_lbs = _sum_lbs(mgmt_full)
    dropped_lbs = _sum_lbs(ledger)

    steps = tuple(
        _step(gate.id, ledger) for gate in ROW_GATES
    )
    return DemandPlanBridge(
        # Inputs are reconstructed as output + everything the gates removed:
        # the gates see exactly the combined Base Plan + R&O frame, so this is
        # the true input total without re-deriving it a second way.
        input_rows=len(mgmt_full) + len(ledger),
        input_lbs=output_lbs + dropped_lbs,
        output_rows=len(mgmt_full),
        output_lbs=output_lbs,
        steps=steps,
        dropped_detail=_drop_detail(ledger),
        rebuilt=mgmt_full,
        published_rows=None if published_mgmt_full is None else len(published_mgmt_full),
        published_lbs=None if published_mgmt_full is None else _sum_lbs(published_mgmt_full),
    )


def _expand_ro_seed(
    ro_seed: Optional[pd.DataFrame], anchor_month: date, log: _Log,
) -> pd.DataFrame:
    """Expand RO_Seed to the wide Format×Month frame, tolerating an empty seed.

    The pipeline requires a populated ``RO_Seed.csv`` and fails loudly without
    one — correct for a build, wrong for a diagnostic.  A planner opens this
    bridge precisely when the files are in a bad state, so an absent or empty
    seed degrades to "no R&O leg" and the Base Plan leg still reconciles.
    """
    if ro_seed is not None and not ro_seed.empty:
        return _build_tbl_ro_input(ro_seed, anchor_month, log)
    log.warn("RO_Seed is missing or empty — reconciling the Base Plan leg only.")
    return pd.DataFrame(columns=[
        *_SEED_COLUMNS, "Prob. Lbs/m", *(f"Month {n}" for n in range(1, _N_MONTHS + 1)),
    ])


def _step(gate_id: str, ledger: pd.DataFrame) -> BridgeStep:
    """Summarise one gate's drops from the ledger."""
    gate = ROW_GATES_BY_ID[gate_id]
    rows = (ledger.loc[ledger[GATE_COL] == gate_id]
            if not ledger.empty else ledger)
    return BridgeStep(
        gate_id=gate_id,
        label=gate.label,
        rows=len(rows),
        lbs=_sum_lbs(rows),
        items=int(rows["Item"].nunique()) if not rows.empty else 0,
        reason=gate.reason,
        fix=gate.fix,
    )


def _drop_detail(ledger: pd.DataFrame) -> pd.DataFrame:
    """Per (Item, Forecast Type, gate) drop detail, biggest loss first.

    Aggregated to the grain a planner acts on: one line per SKU per reason,
    with the row count and pounds behind it.  The reason / fix text comes
    straight off the gate definition, so the guidance shown here is the same
    guidance the pipeline's own documentation carries.
    """
    if ledger is None or ledger.empty:
        return pd.DataFrame(columns=list(DROP_DETAIL_COLUMNS))

    work = ledger.copy()
    work["Item"] = _norm_item(work["Item"])
    work["__lbs"] = _lbs(work["Demand Plan Pounds"])
    desc = (work["Item Description"].astype(str)
            if "Item Description" in work.columns else "")

    grouped = (
        work.assign(**{COL_DESC: desc})
        .groupby(["Item", COL_DESC, "Forecast Type", GATE_COL],
                 as_index=False, dropna=False)
        .agg(**{COL_ROWS: ("__lbs", "size"), COL_LBS: ("__lbs", "sum")})
    )
    grouped[COL_GATE] = grouped[GATE_COL].map(
        lambda g: ROW_GATES_BY_ID[g].label if g in ROW_GATES_BY_ID else str(g))
    grouped[COL_REASON] = grouped[GATE_COL].map(
        lambda g: ROW_GATES_BY_ID[g].reason if g in ROW_GATES_BY_ID else "")
    grouped[COL_FIX] = grouped[GATE_COL].map(
        lambda g: ROW_GATES_BY_ID[g].fix if g in ROW_GATES_BY_ID else "")
    grouped = grouped.rename(columns={
        "Item": COL_ITEM, "Forecast Type": COL_FORECAST})

    # Largest absolute loss first — a de-list shows as negative lbs and matters
    # just as much as a missing gain.
    grouped = grouped.reindex(
        grouped[COL_LBS].abs().sort_values(ascending=False).index)
    return grouped[list(DROP_DETAIL_COLUMNS)].reset_index(drop=True)


# ── R&O ↔ RO Summary bridge ──────────────────────────────────────────────────

@dataclass(frozen=True)
class RoFiscalBridge:
    """Item-level reconciliation of RO Summary FY lbs vs the plan's R&O lbs.

    Both sides are probabilized pounds for the same fiscal year, but they reach
    the number by different routes — the RO Summary sums
    ``LE Current Fiscal Probabilized Lbs`` off ``RO_Comparison_Output.csv``
    (one annual figure per RO line, pro-rated from its First Ship Date to the
    fiscal year-end), while the demand plan expands ``RO_Seed.csv`` month by
    month and then applies the row gates.  ``detail`` lists every item where the
    two disagree by more than rounding, newest gap first.
    """
    fiscal_start: date
    fiscal_end: date
    ro_summary_lbs: float
    plan_lbs: float
    detail: pd.DataFrame

    @property
    def delta_lbs(self) -> float:
        return float(self.ro_summary_lbs) - float(self.plan_lbs)


# Status labels — also the join key for the fix text below.
_STATUS_MISSING_FROM_PLAN: str = "In RO Summary, absent from the plan"
_STATUS_MISSING_FROM_RO: str = "In the plan, absent from RO Summary"
_STATUS_SHORTFALL: str = "Both, but the plan carries less"
_STATUS_EXCESS: str = "Both, but the plan carries more"

_RO_FIXES: dict[str, str] = {
    _STATUS_MISSING_FROM_PLAN: (
        "The RO line never reached the demand plan.  Check the SKU-level drop "
        "table above — most often it is not B2C (add it to RO_Item_Master.csv) "
        "— then confirm the line is in the CURRENT RO_Seed.csv: the seed is "
        "rebuilt for the latest snapshot month only, so a line carried forward "
        "in RO_History_Tracker.csv can show in the Summary without being "
        "re-seeded.  Regenerate RO_Seed to pull it back in."
    ),
    _STATUS_MISSING_FROM_RO: (
        "The plan carries R&O the Summary does not.  Usually the RO Comparison "
        "editor saved an edit to RO_Comparison_Output.csv without a matching "
        "Distribution Tracker upload.  Re-run the RO Comparison, or re-upload "
        "the tracker so both sides come from the same snapshot."
    ),
    _STATUS_SHORTFALL: (
        "Partial coverage — some months of this line were dropped.  The usual "
        "cause is the forward window cutting the tail off a line that starts "
        "late in the fiscal year; the drop table above shows which months went."
    ),
    _STATUS_EXCESS: (
        "The plan expands more months than the Summary's annual pro-ration "
        "covers.  Expected where a line starts before the fiscal year and the "
        "Summary caps its Days-in-Year at 365; only investigate a large gap."
    ),
}


def build_ro_fiscal_bridge(
    mgmt_full: pd.DataFrame,
    ro_comparison_output: Optional[pd.DataFrame],
    *,
    fiscal_start: date,
    fiscal_end: date,
) -> RoFiscalBridge:
    """Reconcile RO Summary fiscal-year probabilized lbs to the plan's R&O rows.

    *mgmt_full* is the demand plan (published or freshly rebuilt); only its
    ``Forecast Type == "R&O"`` rows inside the fiscal window are counted.
    *ro_comparison_output* is the published RO comparison; ``None`` (or a frame
    without the probabilized column) yields an empty bridge rather than raising,
    so the panel degrades to "RO side unavailable" instead of breaking.
    """
    plan_by_item = _plan_ro_by_item(mgmt_full, fiscal_start, fiscal_end)
    ro_by_item = _ro_summary_by_item(ro_comparison_output)

    joined = ro_by_item.join(plan_by_item, how="outer").fillna(0.0)
    joined[COL_DELTA] = joined[COL_RO_SUMMARY_LBS] - joined[COL_PLAN_LBS]

    detail = joined.loc[joined[COL_DELTA].abs() > _RO_NOISE_LBS].copy()
    if not detail.empty:
        detail[COL_STATUS] = [
            _classify_ro_gap(ro, plan)
            for ro, plan in zip(detail[COL_RO_SUMMARY_LBS], detail[COL_PLAN_LBS])
        ]
        detail[COL_FIX] = detail[COL_STATUS].map(_RO_FIXES)
        detail = detail.reindex(
            detail[COL_DELTA].abs().sort_values(ascending=False).index)
        detail = detail.reset_index().rename(columns={"index": COL_ITEM})
        detail = detail[list(RO_BRIDGE_COLUMNS)]
    else:
        detail = pd.DataFrame(columns=list(RO_BRIDGE_COLUMNS))

    return RoFiscalBridge(
        fiscal_start=fiscal_start,
        fiscal_end=fiscal_end,
        ro_summary_lbs=float(joined[COL_RO_SUMMARY_LBS].sum()),
        plan_lbs=float(joined[COL_PLAN_LBS].sum()),
        detail=detail,
    )


def _classify_ro_gap(ro_lbs: float, plan_lbs: float) -> str:
    """Label one item's gap by which side is missing / short."""
    if abs(plan_lbs) <= _RO_NOISE_LBS:
        return _STATUS_MISSING_FROM_PLAN
    if abs(ro_lbs) <= _RO_NOISE_LBS:
        return _STATUS_MISSING_FROM_RO
    return _STATUS_SHORTFALL if ro_lbs > plan_lbs else _STATUS_EXCESS


def _plan_ro_by_item(
    mgmt_full: Optional[pd.DataFrame], fiscal_start: date, fiscal_end: date,
) -> pd.DataFrame:
    """R&O pounds per item inside the fiscal window (indexed by Item)."""
    empty = pd.DataFrame({COL_PLAN_LBS: pd.Series(dtype=float)})
    empty.index.name = COL_ITEM
    if mgmt_full is None or mgmt_full.empty:
        return empty
    needed = {"Forecast Type", "Start of Month", "Item", "Demand Plan Pounds"}
    if not needed.issubset(mgmt_full.columns):
        return empty

    work = mgmt_full.loc[mgmt_full["Forecast Type"].astype(str).str.strip() == "R&O"].copy()
    if work.empty:
        return empty
    month = pd.to_datetime(work["Start of Month"], errors="coerce")
    in_window = (month >= pd.Timestamp(fiscal_start)) & (month <= pd.Timestamp(fiscal_end))
    work = work.loc[in_window]
    if work.empty:
        return empty

    out = (
        work.assign(**{COL_ITEM: _norm_item(work["Item"]),
                       COL_PLAN_LBS: _lbs(work["Demand Plan Pounds"])})
        .groupby(COL_ITEM)[COL_PLAN_LBS].sum().to_frame()
    )
    return out


def _ro_summary_by_item(ro_comparison_output: Optional[pd.DataFrame]) -> pd.DataFrame:
    """RO Summary fiscal probabilized lbs per item (indexed by Item).

    Reads the SAME column the RO Summary Report totals — see
    ``ro_summary_report``'s ``FY27 Probabilized | Current Plan`` — so the two
    cannot drift apart.
    """
    empty = pd.DataFrame({COL_RO_SUMMARY_LBS: pd.Series(dtype=float)})
    empty.index.name = COL_ITEM
    if ro_comparison_output is None or ro_comparison_output.empty:
        return empty
    if not {"Item #", CUR_FISCAL_PROB_LE}.issubset(ro_comparison_output.columns):
        return empty

    work = ro_comparison_output
    return (
        work.assign(**{COL_ITEM: _norm_item(work["Item #"]),
                       COL_RO_SUMMARY_LBS: _lbs(work[CUR_FISCAL_PROB_LE])})
        .groupby(COL_ITEM)[COL_RO_SUMMARY_LBS].sum().to_frame()
    )


__all__ = [
    "BridgeStep", "DemandPlanBridge", "build_demand_plan_bridge",
    "RoFiscalBridge", "build_ro_fiscal_bridge",
    "DROP_DETAIL_COLUMNS", "RO_BRIDGE_COLUMNS",
]
