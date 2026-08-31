"""Unit tests for the demand-plan reconciliation bridge.

Two properties matter most and are asserted directly:

* **The waterfall closes.**  input − Σ(drops) == output, always.  A bridge that
  doesn't add up is worse than no bridge.
* **The bridge cannot drift from the pipeline.**  It runs the pipeline's own
  gates rather than re-implementing them, so a row the pipeline drops is the
  row the bridge explains — asserted by rebuilding both from one input.

Pure in-memory frames — no Fabric, no Streamlit.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import data_sources.demand_plan_pipeline as dpp
from data_sources.demand_plan_reconcile import (
    COL_DELTA,
    COL_GATE,
    COL_ITEM,
    COL_LBS,
    COL_ROWS,
    COL_PLAN_LBS,
    COL_RO_SUMMARY_LBS,
    COL_STATUS,
    build_demand_plan_bridge,
    build_ro_fiscal_bridge,
)
from data_sources.ro_comparison import CUR_FISCAL_PROB_LE


WINDOW_END = pd.Timestamp("2028-09-01")


# ── Fixtures: the smallest inputs that exercise each gate ────────────────────

def _base_plan(*rows: dict) -> pd.DataFrame:
    """ibp_base_plan_current-shaped upload."""
    return pd.DataFrame([{
        "Start of Month": r.get("month", "9/1/2026"),
        "Item": r["item"],
        "Item Description": r.get("desc", f"desc {r['item']}"),
        "Value": r.get("site", "7516"),
        "Total": r["lbs"],
        "Corporate Group": r.get("corp", "COSTCO"),
        "month": "9/1/2026",
        "Cycle": "C6",
    } for r in rows])


def _seed(*rows: dict) -> pd.DataFrame:
    """RO_Seed-shaped frame; empty but correctly shaped when given no rows."""
    if not rows:
        return pd.DataFrame(columns=list(dpp._SEED_COLUMNS))
    return pd.DataFrame([{
        "Format": "F", "Customer": r.get("cust", "C"), "Taxonomy": "T",
        "Brand": "B", "Item #": r["item"], "Item Desc": f"desc {r['item']}",
        "Probability": r.get("prob", "0.5"), "First Ship Date": r.get("fsd", "10/1/2026"),
        "Lbs./yr": r["lbs_yr"], "PC$/yr": "0", "Slotting": "0",
    } for r in rows])


def _months() -> pd.DataFrame:
    start = pd.Timestamp("2026-04-01")
    return pd.DataFrame([{
        "Month Number": f"Month {n}",
        "Start of Month": (start + pd.DateOffset(months=n - 1)).strftime("%m/%d/%Y"),
    } for n in range(1, 37)])


def _pdh(*items: tuple[str, str]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Item No": i, "Business Unit": bu, "Portfolio Major": "ESL",
        "Portfolio Minor": "White Milk", "Supply Format": "Large Carton",
    } for i, bu in items])


def _ro_master() -> pd.DataFrame:
    return pd.DataFrame(columns=["Item #", "Business Unit", "Portfolio Major",
                                 "Portfolio Minor", "Supply Format"])


def _bridge(base_plan, seed=None, pdh=None, **kw):
    return build_demand_plan_bridge(
        base_plan,
        seed if seed is not None else _seed(),
        _months(),
        pdh if pdh is not None else _pdh(("1", "B2C")),
        _ro_master(),
        window_end=kw.pop("window_end", WINDOW_END),
        **kw,
    )


# ── The waterfall closes ─────────────────────────────────────────────────────

def test_waterfall_adds_up():
    bridge = _bridge(_base_plan(
        {"item": "1", "lbs": "100"},              # kept
        {"item": "1", "lbs": "0"},                # zero
        {"item": "1", "lbs": "50", "month": "9/1/2030"},   # beyond window
    ))
    assert bridge.input_lbs - bridge.dropped_lbs == pytest.approx(bridge.output_lbs)
    assert bridge.output_lbs == pytest.approx(100.0)


def test_every_input_row_is_either_kept_or_explained():
    # The invariant behind the whole bridge: a row cannot vanish unrecorded.
    # An NA Business Unit (item in neither PDH nor RO_Item_Master) used to be
    # falsy in both directions and fell out of the kept set AND the ledger.
    bridge = _bridge(_base_plan(
        {"item": "1", "lbs": "100"},        # kept
        {"item": "999", "lbs": "700"},      # no BU anywhere
        {"item": "1", "lbs": "0"},          # zero
    ))
    assert bridge.input_rows == 3
    assert bridge.output_rows + int(bridge.dropped_detail[COL_ROWS].sum()) == 3
    assert bridge.input_lbs - bridge.dropped_lbs == pytest.approx(bridge.output_lbs)


def test_a_clean_input_drops_nothing():
    bridge = _bridge(_base_plan({"item": "1", "lbs": "100"}))
    assert bridge.dropped_detail.empty
    assert all(s.rows == 0 for s in bridge.steps)


# ── Each gate is attributed correctly ────────────────────────────────────────

def _gate_labels(bridge) -> set[str]:
    return set(bridge.dropped_detail[COL_GATE])


def test_zero_rows_are_attributed_to_the_demand_sign_gate():
    bridge = _bridge(_base_plan({"item": "1", "lbs": "0"}))
    assert _gate_labels(bridge) == {dpp.ROW_GATES_BY_ID["demand_sign"].label}


def test_negative_base_plan_rows_are_dropped_and_explained():
    bridge = _bridge(_base_plan({"item": "1", "lbs": "-500"}))
    assert _gate_labels(bridge) == {dpp.ROW_GATES_BY_ID["demand_sign"].label}
    assert bridge.output_lbs == pytest.approx(0.0)


def test_undated_rows_are_attributed_to_the_undated_gate():
    bridge = _bridge(_base_plan({"item": "1", "lbs": "100", "month": "not a date"}))
    assert _gate_labels(bridge) == {dpp.ROW_GATES_BY_ID["undated"].label}


def test_far_horizon_rows_are_attributed_to_the_window_gate():
    bridge = _bridge(_base_plan({"item": "1", "lbs": "100", "month": "9/1/2030"}))
    assert _gate_labels(bridge) == {dpp.ROW_GATES_BY_ID["forward_window"].label}


def test_non_b2c_rows_are_attributed_to_the_business_unit_gate():
    bridge = _bridge(_base_plan({"item": "1", "lbs": "100"}), pdh=_pdh(("1", "B2B")))
    assert _gate_labels(bridge) == {dpp.ROW_GATES_BY_ID["business_unit"].label}


def test_an_item_in_neither_pdh_nor_ro_master_is_dropped_as_non_b2c():
    # The silent-loss case the bridge exists to surface.
    bridge = _bridge(_base_plan({"item": "999", "lbs": "100"}), pdh=_pdh(("1", "B2C")))
    assert _gate_labels(bridge) == {dpp.ROW_GATES_BY_ID["business_unit"].label}
    assert bridge.dropped_detail[COL_ITEM].tolist() == ["999"]


def test_only_the_first_failing_gate_is_charged():
    # A zero-lbs row far beyond the window must be counted ONCE, by the gate
    # that actually removed it, or the waterfall would double-count.
    bridge = _bridge(_base_plan({"item": "1", "lbs": "0", "month": "9/1/2030"}))
    assert len(bridge.dropped_detail) == 1
    assert _gate_labels(bridge) == {dpp.ROW_GATES_BY_ID["demand_sign"].label}


# ── Detail content ───────────────────────────────────────────────────────────

def test_detail_carries_a_reason_and_a_fix_for_every_row():
    bridge = _bridge(_base_plan(
        {"item": "1", "lbs": "0"},
        {"item": "1", "lbs": "100", "month": "9/1/2030"},
        {"item": "999", "lbs": "100"},
    ))
    from data_sources.demand_plan_reconcile import COL_FIX, COL_REASON
    assert (bridge.dropped_detail[COL_REASON].str.len() > 0).all()
    assert (bridge.dropped_detail[COL_FIX].str.len() > 0).all()


def test_detail_is_sorted_by_biggest_loss_first():
    bridge = _bridge(_base_plan(
        {"item": "1", "lbs": "100", "month": "9/1/2030"},
        {"item": "2", "lbs": "900", "month": "9/1/2030"},
    ), pdh=_pdh(("1", "B2C"), ("2", "B2C")))
    assert bridge.dropped_detail[COL_ITEM].tolist() == ["2", "1"]
    assert bridge.dropped_detail[COL_LBS].tolist() == [900.0, 100.0]


# ── Drift against the published file ─────────────────────────────────────────

def test_no_published_file_means_no_drift_verdict():
    bridge = _bridge(_base_plan({"item": "1", "lbs": "100"}))
    assert bridge.drift_lbs is None
    assert bridge.ties is False


def test_a_matching_published_file_ties():
    plan = _base_plan({"item": "1", "lbs": "100"})
    published = pd.DataFrame({"Demand Plan Pounds": ["100"]})
    assert _bridge(plan, published_mgmt_full=published).ties


def test_a_stale_published_file_is_flagged():
    plan = _base_plan({"item": "1", "lbs": "100"})
    published = pd.DataFrame({"Demand Plan Pounds": ["250"]})
    bridge = _bridge(plan, published_mgmt_full=published)
    assert not bridge.ties
    assert bridge.drift_lbs == pytest.approx(150.0)


# ── R&O ↔ RO Summary bridge ──────────────────────────────────────────────────

FY = {"fiscal_start": date(2026, 4, 1), "fiscal_end": date(2027, 3, 1)}


def _plan_ro(item: str, lbs: float, month: str = "9/1/2026") -> pd.DataFrame:
    return pd.DataFrame([{
        "Item": item, "Start of Month": month,
        "Demand Plan Pounds": str(lbs), "Forecast Type": "R&O",
    }])


def _ro_out(item: str, lbs: float) -> pd.DataFrame:
    return pd.DataFrame([{"Item #": item, CUR_FISCAL_PROB_LE: str(lbs)}])


def test_matching_sides_report_no_detail():
    ro = build_ro_fiscal_bridge(_plan_ro("1", 100_000), _ro_out("1", 100_000), **FY)
    assert ro.detail.empty
    assert ro.delta_lbs == pytest.approx(0.0)


def test_item_missing_from_the_plan_is_named():
    ro = build_ro_fiscal_bridge(_plan_ro("1", 0), _ro_out("2", 500_000), **FY)
    row = ro.detail.iloc[0]
    assert row[COL_ITEM] == "2"
    assert "absent from the plan" in row[COL_STATUS]
    assert row[COL_DELTA] == pytest.approx(500_000.0)


def test_item_missing_from_ro_summary_is_named():
    ro = build_ro_fiscal_bridge(_plan_ro("1", 500_000), _ro_out("2", 0), **FY)
    statuses = set(ro.detail[COL_STATUS])
    assert any("absent from RO Summary" in s for s in statuses)


def test_partial_coverage_is_labelled_a_shortfall():
    ro = build_ro_fiscal_bridge(_plan_ro("1", 100_000), _ro_out("1", 500_000), **FY)
    assert "plan carries less" in ro.detail.iloc[0][COL_STATUS]


def test_rows_outside_the_fiscal_window_are_not_counted():
    # Sep 2028 is outside FY27 — the plan side must read zero for it.
    ro = build_ro_fiscal_bridge(_plan_ro("1", 900_000, "9/1/2028"), _ro_out("1", 0), **FY)
    assert ro.plan_lbs == pytest.approx(0.0)


def test_base_plan_rows_are_ignored_by_the_ro_bridge():
    plan = _plan_ro("1", 100_000).assign(**{"Forecast Type": "Base Plan"})
    assert build_ro_fiscal_bridge(plan, _ro_out("1", 0), **FY).plan_lbs == 0.0


def test_rounding_noise_is_not_reported_as_a_gap():
    ro = build_ro_fiscal_bridge(_plan_ro("1", 100_000), _ro_out("1", 101_000), **FY)
    assert ro.detail.empty          # 1,000 lbs < the noise floor


def test_a_missing_ro_file_degrades_instead_of_raising():
    ro = build_ro_fiscal_bridge(_plan_ro("1", 100_000), None, **FY)
    assert ro.ro_summary_lbs == 0.0
    assert ro.plan_lbs == pytest.approx(100_000.0)


def test_an_ro_file_without_the_probabilized_column_degrades():
    junk = pd.DataFrame({"Item #": ["1"], "Something Else": ["5"]})
    assert build_ro_fiscal_bridge(_plan_ro("1", 1), junk, **FY).ro_summary_lbs == 0.0
