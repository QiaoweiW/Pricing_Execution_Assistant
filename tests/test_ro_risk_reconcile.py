"""Unit tests for :mod:`data_sources.ro_risk_reconcile`.

Cover the four cases the RO Summary vs RO_Seed reconciliation button
surfaces to the planner:

1. Both files agree — no divergence.
2. A risk exists in the RO Summary (via RO_Comparison_Output) but not in
   RO_Seed — the actionable "mgmt-plan under-reports R&O" case.
3. A risk exists in RO_Seed but not in the RO Summary — usually a stale
   RO_Comparison_Output.csv.
4. The current :class:`RoRulesConfig` threshold flips a borderline row.

Pure in-memory DataFrames — no Fabric, no Streamlit.
"""
from __future__ import annotations

import pandas as pd

from data_sources.ro_risk_reconcile import (
    BUSINESS_KEY_COLS,
    SEED_PROBABILITY_COL,
    SEED_REFLECTED_COL,
    SEED_VOLUME_COL,
    SUMMARY_PROBABILITY_COL,
    SUMMARY_VOLUME_COL,
    reconcile_ro_seed_vs_summary,
)
from data_sources.ro_rules_config import RoRulesConfig


# ── Row factories ────────────────────────────────────────────────────────────


def _seed_row(item: str, lbs: float, prob: float, *, reflected: str = "no",
              customer: str = "URM", **over) -> dict:
    """Construct a RO_Seed-shaped row (columns as read from Distribution_Tracker_History)."""
    row = {
        "Format": "F", "Customer": customer, "Taxonomy": "T",
        "Brand": "B", "Item #": item, "Item Desc": f"D{item}",
        SEED_VOLUME_COL: lbs, SEED_PROBABILITY_COL: prob,
        SEED_REFLECTED_COL: reflected, "Month": "2026-07",
    }
    row.update(over)
    return row


def _summary_row(item: str, le_lbs: float, le_prob: float, *,
                 customer: str = "URM", driver: str = "Change",
                 **over) -> dict:
    """Construct an RO_Comparison_Output-shaped row (only the columns the reconcile needs)."""
    row = {
        "Format": "F", "Customer": customer, "Taxonomy": "T",
        "Brand": "B", "Item #": item, "Description": f"D{item}",
        SUMMARY_VOLUME_COL: le_lbs, SUMMARY_PROBABILITY_COL: le_prob,
        "Driver": driver,
    }
    row.update(over)
    return row


# ── Aligned case ─────────────────────────────────────────────────────────────


def test_aligned_when_same_risk_lines_in_both_files():
    """One risk in each file, matching business keys → no divergence."""
    seed = pd.DataFrame([
        _seed_row("100", -5000.0, 1.0),           # RISK
        _seed_row("200", 1000.0, 0.9),            # Opportunity (positive)
    ])
    summary = pd.DataFrame([
        _summary_row("100", -5_000_000, 1.0, driver="Change"),  # RISK
        _summary_row("200", 1_000_000, 0.9),                    # Opportunity
    ])
    result = reconcile_ro_seed_vs_summary(seed, summary)
    assert result.is_aligned
    assert result.seed_risk_count == 1
    assert result.summary_risk_count == 1
    assert len(result.matched) == 1
    assert result.total_divergence == 0


# ── The bug the user reported ────────────────────────────────────────────────


def test_missing_from_seed_when_summary_has_risk_seed_does_not():
    """RO Summary shows a risk; RO_Seed does not carry that Item # at all."""
    seed = pd.DataFrame([_seed_row("200", 1000.0, 0.9)])
    summary = pd.DataFrame([
        _summary_row("999", -1_000_000, 1.0, driver="Change"),  # only in summary
        _summary_row("200", 1_000_000, 0.9),
    ])
    result = reconcile_ro_seed_vs_summary(seed, summary)
    assert not result.is_aligned
    assert len(result.missing_from_seed) == 1
    assert result.missing_from_seed.iloc[0]["Item #"] == "999"
    assert result.missing_from_summary.empty
    # Detail frame carries the summary-side columns so the planner can scan it.
    assert SUMMARY_VOLUME_COL in result.missing_from_seed.columns
    assert result.missing_from_seed.iloc[0]["Driver"] == "Change"


# ── The stale-summary case ───────────────────────────────────────────────────


def test_missing_from_summary_when_seed_has_risk_summary_does_not():
    """RO_Seed carries a risk that hasn't propagated into RO_Comparison_Output yet."""
    seed = pd.DataFrame([_seed_row("100", -5000.0, 1.0)])   # RISK on seed side
    summary = pd.DataFrame(columns=list(BUSINESS_KEY_COLS) + [
        SUMMARY_VOLUME_COL, SUMMARY_PROBABILITY_COL, "Description", "Driver",
    ])
    result = reconcile_ro_seed_vs_summary(seed, summary)
    assert not result.is_aligned
    assert result.missing_from_seed.empty
    assert len(result.missing_from_summary) == 1
    assert result.missing_from_summary.iloc[0]["Item #"] == "100"


# ── Rules-config affects both sides identically ──────────────────────────────


def test_config_override_tightens_risk_threshold_symmetrically():
    """A 100% risk threshold drops the same borderline row from both sides."""
    seed = pd.DataFrame([_seed_row("A", -5000.0, 0.75)])   # 75%
    summary = pd.DataFrame([_summary_row("A", -5_000_000, 0.75, driver="Change")])
    # Default 50% threshold → both flag it as risk, they agree.
    assert reconcile_ro_seed_vs_summary(seed, summary).is_aligned
    # 100% threshold → NEITHER side sees it as a risk, they still agree.
    cfg = RoRulesConfig.default().with_updates(min_risk_probability=1.0)
    r = reconcile_ro_seed_vs_summary(seed, summary, config=cfg)
    assert r.is_aligned
    assert r.seed_risk_count == 0
    assert r.summary_risk_count == 0


# ── Dtype / whitespace robustness ────────────────────────────────────────────


def test_item_id_dtype_and_whitespace_are_normalised():
    """Item # ``"380574"`` on one side must match ``380574.0`` (float-int) on the other."""
    seed = pd.DataFrame([_seed_row("380574", -5000.0, 1.0)])
    summary = pd.DataFrame([
        _summary_row("380574.0", -5_000_000, 1.0, driver="Change", customer=" URM "),
    ])
    result = reconcile_ro_seed_vs_summary(seed, summary)
    assert result.is_aligned, (
        f"Expected match, got missing_from_seed=\n{result.missing_from_seed}\n"
        f"missing_from_summary=\n{result.missing_from_summary}"
    )


# ── Same business key on multiple rows collapses in the detail frame ────────


def test_duplicate_rows_per_business_key_collapse_in_detail():
    """A summary that repeats the same RO across ship-date buckets → one row."""
    seed = pd.DataFrame(columns=list(BUSINESS_KEY_COLS) + [
        SEED_VOLUME_COL, SEED_PROBABILITY_COL, SEED_REFLECTED_COL, "Item Desc", "Month",
    ])
    summary = pd.DataFrame([
        _summary_row("999", -600_000, 1.0, driver="Change"),
        _summary_row("999", -400_000, 1.0, driver="Change"),  # duplicate business key
    ])
    result = reconcile_ro_seed_vs_summary(seed, summary)
    # Two summary rows classify as risk, but the detail dedupes to one entry.
    assert result.summary_risk_count == 2
    assert len(result.missing_from_seed) == 1


# ── Empty / missing inputs never raise ───────────────────────────────────────


def test_empty_inputs_are_handled():
    r = reconcile_ro_seed_vs_summary(None, None)
    assert r.is_aligned
    assert r.seed_risk_count == 0
    assert r.summary_risk_count == 0
    assert r.matched.empty


def test_missing_required_column_yields_no_risk_not_an_exception():
    """A frame without the volume column can't be classified — treat as no risks."""
    bad = pd.DataFrame([{"Item #": "1", "Format": "F", "Customer": "C",
                         "Taxonomy": "T", "Brand": "B"}])
    r = reconcile_ro_seed_vs_summary(bad, bad)
    assert r.seed_risk_count == 0 and r.summary_risk_count == 0
