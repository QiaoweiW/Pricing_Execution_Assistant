"""Which rows the upload validator is allowed to scrutinise.

The pre-flight must not block a run over rows the pipeline is about to
discard — an item already reflected in APS, a declined programme, a
zero-probability line.  ``ro_risk.seed_scope_mask`` decides that, and it is
deliberately a **superset** of what ``ro_seed_pipeline`` keeps: a row whose
gate cell is unreadable stays in scope, because that is the row most likely
to vanish from the plan by accident rather than by decision.

The load-bearing test here is :func:`test_pipeline_output_is_contained_in_scope`,
which asserts that containment against the REAL pipeline.  If someone later
tightens one predicate without the other, that test fails rather than the
validator silently skipping rows that matter.
"""
from __future__ import annotations

import pandas as pd
import pytest

from data_sources.ro_risk import seed_scope_mask
from data_sources.ro_rules_config import RoRulesConfig
from data_sources.ro_seed_pipeline import _build_ro_seed, _Log


_CFG = RoRulesConfig.default()


def _row(**over) -> dict:
    row = {
        "Month": "2026-06-01", "Format": "HTST", "Customer": "Walmart",
        "Taxonomy": "Retail", "Brand": "PL", "Item #": "340021",
        "Item Desc": "Milk Gallon", "Probability": "0.5",
        "First Ship Date": "2027-01-01", "Lbs./yr": "1000000",
        "PC$/yr": "0", "Slotting": "0",
        "Reflected in APS": "no", "Pipeline Status": "Presented",
    }
    row.update(over)
    return row


def _scope(*rows, config: RoRulesConfig = _CFG) -> list:
    df = pd.DataFrame(list(rows))
    return list(seed_scope_mask(df, config=config))


# ── Out of scope: a stated business decision ─────────────────────────────────

def test_reflected_in_aps_yes_is_out_of_scope():
    """The planner has said this is already in the base plan — leave it alone."""
    assert _scope(_row(**{"Reflected in APS": "yes"})) == [False]


@pytest.mark.parametrize("status", ["Declined", "declined", "Closed", "CLOSED"])
def test_declined_or_closed_is_out_of_scope(status):
    assert _scope(_row(**{"Pipeline Status": status})) == [False]


@pytest.mark.parametrize("prob", ["0", "0.0", "0.00"])
def test_zero_probability_is_out_of_scope(prob):
    assert _scope(_row(Probability=prob)) == [False]


# ── In scope: we cannot tell, so we check ────────────────────────────────────

@pytest.mark.parametrize("reflected", ["", "   ", "nan"])
def test_a_blank_reflected_in_aps_stays_in_scope(reflected):
    """The pipeline would drop it; we still check it, because that drop is silent."""
    assert _scope(_row(**{"Reflected in APS": reflected})) == [True]


@pytest.mark.parametrize("prob", ["", "#N/A", "high", "TBD"])
def test_an_unreadable_probability_stays_in_scope(prob):
    assert _scope(_row(Probability=prob)) == [True]


def test_a_negative_probability_stays_in_scope():
    """It parses, but no probability is below zero — a broken cell, not a zero."""
    assert _scope(_row(Probability="-0.2")) == [True]


def test_an_ordinary_opportunity_is_in_scope():
    assert _scope(_row()) == [True]


# ── Risk lines bypass the gates, exactly as the pipeline lets them ───────────

def test_a_risk_line_stays_in_scope_despite_being_declined():
    risk = _row(**{"Pipeline Status": "Declined", "Lbs./yr": "-500000",
                   "Probability": "1"})
    assert _scope(risk) == [True]


def test_a_risk_line_stays_in_scope_despite_low_probability_gate():
    """Risk clears its own threshold, so the Opportunity floor must not drop it."""
    cfg = RoRulesConfig.default().with_updates(min_opp_probability=0.9)
    risk = _row(**{"Lbs./yr": "-500000", "Probability": "0.6"})
    assert seed_scope_mask(pd.DataFrame([risk]), config=cfg).tolist() == [True]


def test_reflected_in_aps_yes_beats_the_risk_exemption():
    """A risk needs Reflected-in-APS = no, so a Yes is out regardless."""
    risk = _row(**{"Reflected in APS": "yes", "Lbs./yr": "-500000",
                   "Probability": "1"})
    assert _scope(risk) == [False]


# ── The rules the planner sets are the rules that scope ─────────────────────

def test_raising_the_opportunity_floor_narrows_the_scope():
    cfg = RoRulesConfig.default().with_updates(min_opp_probability=0.6)
    row = _row(Probability="0.5")
    assert seed_scope_mask(pd.DataFrame([row]), config=_CFG).tolist() == [True]
    assert seed_scope_mask(pd.DataFrame([row]), config=cfg).tolist() == [False]


def test_disabling_the_aps_gate_widens_the_scope():
    cfg = RoRulesConfig.default().with_updates(reflected_in_aps_only=False)
    row = _row(**{"Reflected in APS": "yes"})
    assert seed_scope_mask(pd.DataFrame([row]), config=cfg).tolist() == [True]


# ── Degenerate inputs ───────────────────────────────────────────────────────

def test_an_empty_frame_yields_an_empty_mask():
    assert list(seed_scope_mask(pd.DataFrame(), config=_CFG)) == []


def test_a_frame_without_the_gate_columns_is_all_in_scope():
    """No columns to judge by → check everything rather than nothing."""
    df = pd.DataFrame([{"Format": "HTST", "Item #": "1"}])
    assert seed_scope_mask(df, config=_CFG).tolist() == [True]


# ── The invariant that stops the two predicates drifting ────────────────────

def test_pipeline_output_is_contained_in_scope():
    """Every row the real pipeline keeps must be one the validator checked.

    This is the property that makes scoping safe.  If it ever fails, the
    validator is skipping rows that reach the report — the exact silent-wrong-
    number class the pre-flight exists to prevent.
    """
    rows = [
        _row(Item="a"),                                             # ordinary
        _row(**{"Reflected in APS": "yes"}),                        # in APS
        _row(**{"Pipeline Status": "Declined"}),                    # declined
        _row(**{"Pipeline Status": "Closed"}),                      # closed
        _row(Probability="0"),                                      # zero prob
        _row(Probability=""),                                       # unreadable
        _row(**{"Reflected in APS": ""}),                           # blank gate
        _row(Probability="-0.2"),                                   # impossible
        _row(**{"Lbs./yr": "-500000", "Probability": "1",
               "Pipeline Status": "Declined"}),                     # risk
        _row(**{"Lbs./yr": "#N/A"}),                                # broken vol
    ]
    # Distinct items so the seed's aggregation cannot merge two cases together.
    for i, r in enumerate(rows):
        r["Item #"] = str(340000 + i)
        r.pop("Item", None)
    df = pd.DataFrame(rows)

    kept = _build_ro_seed(df, {"2026-06-01"}, _Log(), config=_CFG)
    scoped_items = set(
        df.loc[seed_scope_mask(df, config=_CFG), "Item #"].astype(str)
    )
    kept_items = set(kept["Item #"].astype(str)) if not kept.empty else set()

    assert kept_items, "fixture built no seed rows — the test proves nothing"
    assert kept_items <= scoped_items, (
        f"pipeline keeps rows the validator never checked: "
        f"{sorted(kept_items - scoped_items)}"
    )
