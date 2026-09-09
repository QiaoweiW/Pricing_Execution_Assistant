"""Root-cause analysis of the RO Summary ↔ plan R&O gap.

The per-item bridge answers "which items differ".  A planner looking at a
multi-million-pound gap needs the next question answered: what is causing it,
and which cause is worth chasing first.  These tests pin that attribution —
above all that the causes sum to the whole gap, which is what makes the table
trustworthy rather than merely suggestive.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from data_sources.demand_plan_reconcile import (
    COL_DELTA, COL_DESC, COL_GATE, COL_ITEM, COL_LBS, COL_PLAN_LBS,
    COL_RO_SUMMARY_LBS, COL_STATUS, RO_CAUSE_COLUMNS, RoFiscalBridge,
    analyse_ro_delta,
)

_MISSING_FROM_PLAN = "In RO Summary, absent from the plan"
_MISSING_FROM_RO = "In the plan, absent from RO Summary"
_SHORTFALL = "Both, but the plan carries less"
_EXCESS = "Both, but the plan carries more"


def _detail(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [{COL_ITEM: i, COL_RO_SUMMARY_LBS: ro, COL_PLAN_LBS: pl,
          COL_DELTA: ro - pl, COL_STATUS: st} for i, ro, pl, st in rows]
    )


def _bridge(detail: pd.DataFrame) -> RoFiscalBridge:
    ro = float(detail[COL_RO_SUMMARY_LBS].sum()) if not detail.empty else 0.0
    plan = float(detail[COL_PLAN_LBS].sum()) if not detail.empty else 0.0
    return RoFiscalBridge(fiscal_start=date(2026, 4, 1), fiscal_end=date(2027, 3, 31),
                          ro_summary_lbs=ro, plan_lbs=plan, detail=detail)


def _drops(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [{COL_ITEM: i, COL_DESC: d, COL_GATE: g, COL_LBS: lbs}
         for i, d, g, lbs in rows]
    )


# ── The property that makes it trustworthy ───────────────────────────────────

def test_the_causes_account_for_the_whole_gap():
    detail = _detail(
        ("830109", 792_000.0, 0.0, _MISSING_FROM_PLAN),
        ("700001", 0.0, 250_000.0, _MISSING_FROM_RO),
        ("700002", 100_000.0, 60_000.0, _SHORTFALL),
        ("700003", 50_000.0, 90_000.0, _EXCESS),
    )
    bridge = _bridge(detail)
    analysis = analyse_ro_delta(bridge)
    assert round(analysis.explained_lbs) == round(bridge.delta_lbs)
    assert abs(analysis.causes["Share of gap"].sum() - 1.0) < 1e-9


def test_causes_are_ranked_worst_first():
    detail = _detail(
        ("a", 10_000.0, 0.0, _SHORTFALL),
        ("b", 900_000.0, 0.0, _MISSING_FROM_PLAN),
    )
    causes = analyse_ro_delta(_bridge(detail)).causes
    assert causes.iloc[0]["Cause"] == _MISSING_FROM_PLAN
    assert list(causes["M lbs"].abs()) == sorted(causes["M lbs"].abs(), reverse=True)


def test_the_frame_has_the_documented_columns():
    detail = _detail(("a", 1.0, 0.0, _SHORTFALL))
    assert list(analyse_ro_delta(_bridge(detail)).causes.columns) == list(RO_CAUSE_COLUMNS)


# ── The actual root cause, not just the symptom ──────────────────────────────

def test_a_missing_line_is_traced_to_the_gate_that_dropped_it():
    """The reported case: two SKUs absent because PDH does not classify them."""
    detail = _detail(
        ("830109", 792_000.0, 0.0, _MISSING_FROM_PLAN),
        ("830108", 620_000.0, 0.0, _MISSING_FROM_PLAN),
    )
    drops = _drops(
        ("830109", "2200lb SMP LH CDX", "Unclassified — not in PDH", 792_000.0),
        ("830108", "25kg SMP LH CDX", "Unclassified — not in PDH", 620_000.0),
    )
    root = analyse_ro_delta(_bridge(detail), drops).causes.iloc[0]["Root cause"]
    assert "Unclassified — not in PDH" in root
    assert "Dropped by the plan build" in root


def test_the_dominant_gate_is_named_and_the_rest_counted():
    detail = _detail(
        ("1", 900_000.0, 0.0, _MISSING_FROM_PLAN),
        ("2", 10_000.0, 0.0, _MISSING_FROM_PLAN),
    )
    drops = _drops(
        ("1", "big", "Unclassified — not in PDH", 900_000.0),
        ("2", "small", "Not B2C", 10_000.0),
    )
    root = analyse_ro_delta(_bridge(detail), drops).causes.iloc[0]["Root cause"]
    assert "Unclassified — not in PDH" in root
    assert "1 other reason" in root


def test_a_line_absent_from_the_drop_ledger_says_so():
    """Not dropped by the build → it was never seeded, which is a different fix."""
    detail = _detail(("999", 500_000.0, 0.0, _MISSING_FROM_PLAN))
    root = analyse_ro_delta(_bridge(detail), _drops()).causes.iloc[0]["Root cause"]
    assert "never seeded" in root or "RO_Seed" in root


def test_the_other_statuses_get_a_structural_root_cause():
    for status, expect in [
        (_MISSING_FROM_RO, "different snapshots"),
        (_SHORTFALL, "forward window"),
        (_EXCESS, "pro-ration"),
    ]:
        detail = _detail(("a", 100_000.0, 0.0, status))
        root = analyse_ro_delta(_bridge(detail)).causes.iloc[0]["Root cause"]
        assert expect in root, status


def test_every_cause_carries_an_action():
    detail = _detail(
        ("a", 1.0, 0.0, _MISSING_FROM_PLAN),
        ("b", 0.0, 1.0, _MISSING_FROM_RO),
    )
    causes = analyse_ro_delta(_bridge(detail)).causes
    assert causes["What to do"].str.len().gt(0).all()


# ── Headline + materiality ───────────────────────────────────────────────────

def test_the_headline_names_the_biggest_cause_with_its_share():
    detail = _detail(
        ("a", 900_000.0, 0.0, _MISSING_FROM_PLAN),
        ("b", 100_000.0, 0.0, _SHORTFALL),
    )
    headline = analyse_ro_delta(_bridge(detail)).headline
    assert "90%" in headline
    assert "absent from the plan" in headline


def test_a_tiny_gap_is_flagged_immaterial():
    detail = _detail(("a", 1_000.0, 0.0, _SHORTFALL))
    assert not analyse_ro_delta(_bridge(detail)).is_material


def test_a_large_gap_is_material():
    detail = _detail(("a", 900_000.0, 0.0, _MISSING_FROM_PLAN))
    assert analyse_ro_delta(_bridge(detail)).is_material


# ── Degenerate inputs ────────────────────────────────────────────────────────

def test_a_tying_bridge_has_nothing_to_explain():
    analysis = analyse_ro_delta(_bridge(pd.DataFrame()))
    assert analysis.causes.empty
    assert "ties" in analysis.headline


def test_no_drop_ledger_is_not_an_error():
    detail = _detail(("a", 100_000.0, 0.0, _MISSING_FROM_PLAN))
    assert not analyse_ro_delta(_bridge(detail), None).causes.empty


def test_no_ledger_and_an_empty_ledger_say_different_things():
    """Absent ledger = we cannot say. Empty ledger = it was never dropped."""
    detail = _detail(("999", 500_000.0, 0.0, _MISSING_FROM_PLAN))
    unavailable = analyse_ro_delta(_bridge(detail), None).causes.iloc[0]["Root cause"]
    empty_ledger = analyse_ro_delta(_bridge(detail), _drops()).causes.iloc[0]["Root cause"]
    assert unavailable != empty_ledger
    assert "drop ledger" in unavailable
    assert "never seeded" in empty_ledger
