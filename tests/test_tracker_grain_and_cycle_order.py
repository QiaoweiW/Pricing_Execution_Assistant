"""Unit tests for the history-tracker grain collapse and cycle ordering.

Two regressions, both silent:

* ``_append_history_tracker`` used ``drop_duplicates`` on a schema that omits
  Corporate Group, so two customers' identical plan lines collapsed to one and
  the cycle lost real volume (C6: 1.03 M lbs over 76 rows).
* Cycles were ranked by LABEL.  The live tracker's labels wrap at the fiscal
  year — ``C11``/``C12`` are prior-year cycles whose horizons start before
  ``C1``'s — so no label ordering can identify the newest cycle.  Ranking by
  plan horizon does.

Pure in-memory frames — no Fabric, no Streamlit.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import data_sources.demand_plan_pipeline as dpp
from data_sources.demand_plan_comparison import (
    cycle_sort_key,
    list_tracker_cycles,
    order_cycles_by_horizon,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _mgmt_row(item: str, month: str, site: str, lbs: float, **over) -> dict:
    row = {
        "Start of Month": month, "Item": item, "Item Description": f"desc {item}",
        "Party Site Number": site, "Demand Plan Pounds": lbs,
        "Forecast Type": "Base Plan", "Business Unit": "B2C",
        "Portfolio Major": "ESL", "Portfolio Minor": "White Milk",
        "Supply Format": "Large Carton",
    }
    row.update(over)
    return row


def _mgmt(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows))[dpp._MGMT_FULL_COLUMNS]


def _tracker_frame(*rows: dict) -> pd.DataFrame:
    """A frame already in tracker shape (all-text, Cycle stamped)."""
    return pd.DataFrame(list(rows))[dpp._TRACKER_COLUMNS].astype(str)


# ── _collapse_to_grain ───────────────────────────────────────────────────────

def test_identical_rows_are_summed_not_dropped():
    # The C6 / item 342065 regression: two Costco lines, same item, month,
    # site and quantity.  Real additive demand — must total 176,286.
    rows = _tracker_frame(
        _mgmt_row("342065", "9/1/2026", "7516", "88143", Cycle="C6"),
        _mgmt_row("342065", "9/1/2026", "7516", "88143", Cycle="C6"),
    )
    out, absorbed = dpp._collapse_to_grain(rows)
    assert len(out) == 1
    assert absorbed == 1
    assert out["Demand Plan Pounds"].iloc[0] == "176286"


def test_distinct_rows_are_left_alone():
    rows = _tracker_frame(
        _mgmt_row("342065", "9/1/2026", "7516", "88143", Cycle="C6"),
        _mgmt_row("342065", "9/1/2026", "7517", "88143", Cycle="C6"),   # other site
        _mgmt_row("342065", "10/1/2026", "7516", "88143", Cycle="C6"),  # other month
    )
    out, absorbed = dpp._collapse_to_grain(rows)
    assert len(out) == 3
    assert absorbed == 0


def test_collapse_preserves_total_volume():
    rows = _tracker_frame(*[
        _mgmt_row("310531", "9/1/2026", "7439", "4244", Cycle="C6") for _ in range(5)
    ])
    out, _ = dpp._collapse_to_grain(rows)
    assert pd.to_numeric(out["Demand Plan Pounds"]).sum() == 4244 * 5


def test_fractional_pounds_survive_without_float_noise():
    rows = _tracker_frame(
        _mgmt_row("370065", "9/1/2026", "77556", "0.1", Cycle="C6"),
        _mgmt_row("370065", "9/1/2026", "77556", "0.2", Cycle="C6"),
    )
    out, _ = dpp._collapse_to_grain(rows)
    assert out["Demand Plan Pounds"].iloc[0] == "0.3"   # not 0.30000000000000004


def test_whole_pounds_carry_no_trailing_decimal():
    rows = _tracker_frame(_mgmt_row("342065", "9/1/2026", "7516", "88143.0", Cycle="C6"))
    out, _ = dpp._collapse_to_grain(rows)
    assert out["Demand Plan Pounds"].iloc[0] == "88143"


def test_grain_comparison_ignores_surrounding_whitespace():
    rows = _tracker_frame(
        _mgmt_row("342065", "9/1/2026", "7516", "100", Cycle="C6"),
        _mgmt_row("342065", "9/1/2026", " 7516 ", "100", Cycle="C6"),
    )
    out, absorbed = dpp._collapse_to_grain(dpp._tracker_text(rows))
    assert absorbed == 1
    assert out["Demand Plan Pounds"].iloc[0] == "200"


def test_collapse_of_empty_frame_is_a_noop():
    empty = pd.DataFrame(columns=dpp._TRACKER_COLUMNS)
    out, absorbed = dpp._collapse_to_grain(empty)
    assert out.empty and absorbed == 0


# ── _append_history_tracker ──────────────────────────────────────────────────

def _append(monkeypatch, mgmt: pd.DataFrame, cycle: str, existing):
    monkeypatch.setattr(dpp, "read_csv", lambda *a, **k: (existing, None))
    return dpp._append_history_tracker(mgmt, cycle, dpp._Log())


def test_append_preserves_duplicate_grain_volume(monkeypatch):
    mgmt = _mgmt(
        _mgmt_row("342065", "2026-09-01", "7516", 88143.0),
        _mgmt_row("342065", "2026-09-01", "7516", 88143.0),
    )
    out = _append(monkeypatch, mgmt, "C6", None)
    assert len(out) == 1
    assert pd.to_numeric(out["Demand Plan Pounds"]).sum() == 176286


def test_append_drops_exact_duplicates_in_previously_published_cycles(monkeypatch):
    # File-level accumulation in an OLD cycle is hygiene — safe to remove,
    # because a re-run of that cycle drops its rows wholesale first.
    existing = _tracker_frame(
        _mgmt_row("310530", "9/1/2026", "7514", "8340", Cycle="C5"),
        _mgmt_row("310530", "9/1/2026", "7514", "8340", Cycle="C5"),
    )
    mgmt = _mgmt(_mgmt_row("342065", "2026-09-01", "7516", 88143.0))
    out = _append(monkeypatch, mgmt, "C6", existing)
    assert len(out.loc[out["Cycle"] == "C5"]) == 1        # duplicate removed
    assert len(out.loc[out["Cycle"] == "C6"]) == 1


def test_append_replaces_the_rerun_cycle_wholesale(monkeypatch):
    existing = _tracker_frame(
        _mgmt_row("342065", "9/1/2026", "7516", "999", Cycle="C6"),
        _mgmt_row("310530", "9/1/2026", "7514", "8340", Cycle="C5"),
    )
    mgmt = _mgmt(_mgmt_row("342065", "2026-09-01", "7516", 88143.0))
    out = _append(monkeypatch, mgmt, "C6", existing)
    c6 = out.loc[out["Cycle"] == "C6"]
    assert len(c6) == 1
    assert c6["Demand Plan Pounds"].iloc[0] == "88143"    # stale 999 gone
    assert len(out.loc[out["Cycle"] == "C5"]) == 1        # other cycle untouched


def test_append_is_idempotent_across_reruns(monkeypatch):
    mgmt = _mgmt(
        _mgmt_row("342065", "2026-09-01", "7516", 88143.0),
        _mgmt_row("342065", "2026-09-01", "7516", 88143.0),
    )
    first = _append(monkeypatch, mgmt, "C6", None)
    second = _append(monkeypatch, mgmt, "C6", first)
    # Re-running must not double the volume, and must not halve it either.
    assert len(second) == len(first) == 1
    assert pd.to_numeric(second["Demand Plan Pounds"]).sum() == 176286


def test_append_raises_when_volume_does_not_reconcile(monkeypatch):
    mgmt = _mgmt(_mgmt_row("342065", "2026-09-01", "7516", 88143.0))
    monkeypatch.setattr(dpp, "read_csv", lambda *a, **k: (None, None))
    # Simulate any future collapse that loses volume — the invariant must fire
    # rather than let a mismatched tracker reach Fabric.
    monkeypatch.setattr(
        dpp, "_collapse_to_grain",
        lambda rows: (rows.iloc[0:0], len(rows)),
    )
    with pytest.raises(ValueError, match="does not reconcile"):
        dpp._append_history_tracker(mgmt, "C6", dpp._Log())


# ── cycle_sort_key ───────────────────────────────────────────────────────────

def test_cycles_sort_naturally_past_single_digits():
    labels = ["C1", "C11", "C12", "C2", "C3"]
    assert sorted(labels, key=cycle_sort_key) == ["C1", "C2", "C3", "C11", "C12"]


def test_sort_key_handles_labels_without_digits():
    labels = ["Draft", "C2", "C10", "Final"]
    assert sorted(labels, key=cycle_sort_key) == ["C2", "C10", "Draft", "Final"]


def test_sort_key_is_whitespace_and_case_insensitive_on_text():
    assert cycle_sort_key(" C6 ") == cycle_sort_key("C6")
    assert cycle_sort_key("c6") == cycle_sort_key("C6")


# ── order_cycles_by_horizon ──────────────────────────────────────────────────
#
# The live tracker's real shape: labels WRAP at the fiscal-year boundary, so
# C11/C12 are prior-year cycles whose horizons start BEFORE C1's.  Neither
# lexicographic nor natural label ordering can get this right.

_LIVE_HORIZONS = {
    "C11": date(2026, 1, 1), "C12": date(2026, 2, 1), "C1": date(2026, 3, 1),
    "C2": date(2026, 5, 1), "C3": date(2026, 5, 1), "C4": date(2026, 6, 1),
    "C5": date(2026, 7, 1), "C6": date(2026, 8, 1),
}


def _horizon_frame(horizons: dict[str, date]) -> pd.DataFrame:
    """One row per cycle at its horizon start, plus a later filler month."""
    rows = [{"Cycle": c, "Start of Month": m} for c, m in horizons.items()]
    rows += [{"Cycle": c, "Start of Month": date(2028, 1, 1)} for c in horizons]
    return pd.DataFrame(rows)


def test_horizon_order_beats_the_label_wrap():
    df = _horizon_frame(_LIVE_HORIZONS)
    assert order_cycles_by_horizon(df["Cycle"], df["Start of Month"]) == [
        "C11", "C12", "C1", "C2", "C3", "C4", "C5", "C6",
    ]


def test_newest_cycle_is_last_despite_the_wrap():
    df = _horizon_frame(_LIVE_HORIZONS)
    ordered = order_cycles_by_horizon(df["Cycle"], df["Start of Month"])
    assert ordered[-1] == "C6"       # the live answer
    assert ordered[-2] == "C5"       # prior-cycle default
    # Both label orderings get this wrong on the same input.
    assert sorted(_LIVE_HORIZONS)[-1] == "C6"                       # lucky
    assert sorted(_LIVE_HORIZONS, key=cycle_sort_key)[-1] == "C12"  # stale


def test_ties_on_horizon_fall_back_to_the_label():
    # C2 and C3 genuinely share a May 2026 horizon in the live tracker.
    df = _horizon_frame({"C3": date(2026, 5, 1), "C2": date(2026, 5, 1)})
    assert order_cycles_by_horizon(df["Cycle"], df["Start of Month"]) == ["C2", "C3"]


def test_undated_cycles_sort_first_and_never_look_newest():
    df = pd.DataFrame({
        "Cycle": ["C6", "C6", "Scratch"],
        "Start of Month": [date(2026, 8, 1), date(2028, 1, 1), None],
    })
    ordered = order_cycles_by_horizon(df["Cycle"], df["Start of Month"])
    assert ordered == ["Scratch", "C6"]


def test_order_without_months_falls_back_to_label_order():
    s = pd.Series(["C10", "C2", "C1"])
    assert order_cycles_by_horizon(s, None) == ["C1", "C2", "C10"]


def test_blank_cycles_are_dropped():
    df = pd.DataFrame({
        "Cycle": ["C1", "", "  ", None, "C1"],
        "Start of Month": [date(2026, 3, 1)] * 5,
    })
    assert order_cycles_by_horizon(df["Cycle"], df["Start of Month"]) == ["C1"]


def test_list_tracker_cycles_uses_the_horizon():
    df = _horizon_frame(_LIVE_HORIZONS)
    assert list_tracker_cycles(df)[-1] == "C6"


def test_list_tracker_cycles_without_a_month_column_still_works():
    assert list_tracker_cycles(pd.DataFrame({"Cycle": ["C2", "C1"]})) == ["C1", "C2"]
