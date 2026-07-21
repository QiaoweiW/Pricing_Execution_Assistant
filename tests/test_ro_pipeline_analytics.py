"""Unit tests for data_sources.ro_pipeline_analytics (pure, no Streamlit)."""

from datetime import date
from math import exp, isclose

import pandas as pd
import pytest

from data_sources.ro_comparison import (
    ANNUAL_OPP_LE,
    CUR_FISCAL_PROB_LE,
    YEAR1_PROB_LE,
)
import data_sources.ro_pipeline_analytics as rpa


AS_OF = date(2026, 7, 20)


def _sample() -> pd.DataFrame:
    """Five programs spanning ship buckets, prob tiers, formats, and a locked win."""
    return pd.DataFrame({
        "Customer":            ["Acme", "Beta", "Cee", "Dee", "Eve"],
        "Item #":              ["1", "2", "3", "4", "5"],
        "Description":         ["A", "B", "C", "D", "E"],
        "Portfolio Major":     ["Butter", "Butter", "Cheese", "Cheese", "Butter"],
        "Supply Format":       ["Cultured", "Extended", "Aseptic", "Cultured", "Cultured"],
        ANNUAL_OPP_LE:         [1000.0, 2000.0, 500.0, 3000.0, 100.0],
        CUR_FISCAL_PROB_LE:    [800.0, 1500.0, 400.0, 0.0, 50.0],
        YEAR1_PROB_LE:         [900.0, 1600.0, 450.0, 100.0, 60.0],
        "LE Probability":      [0.90, 0.30, 0.97, 1.00, 0.10],
        "LE First Ship Date":  ["2026-07-25", "2026-08-30", "2026-12-01",
                                 "2026-07-22", "2026-07-01"],
    })


# ── Metric tiles ─────────────────────────────────────────────────────────────

def test_pipeline_metrics_totals_and_committed():
    m = rpa.compute_pipeline_metrics(_sample())
    assert m.gross_lbs == pytest.approx(6600.0)
    assert m.full_year_lbs == pytest.approx(3110.0)
    assert m.in_year_lbs == pytest.approx(2750.0)
    # Only Cee (prob 0.97 ≥ 0.95) carries in-year lbs; Dee is 0.
    assert m.committed_lbs == pytest.approx(400.0)
    assert m.committed_concentration == pytest.approx(400.0 / 2750.0)


def test_pipeline_metrics_empty():
    m = rpa.compute_pipeline_metrics(pd.DataFrame())
    assert (m.gross_lbs, m.full_year_lbs, m.in_year_lbs, m.committed_lbs) == \
        (0.0, 0.0, 0.0, 0.0)
    assert m.committed_concentration is None


# ── Urgency ranking (Portfolio × Format) ─────────────────────────────────────

def test_urgency_ranking_by_portfolio_and_format():
    wide = rpa.build_urgency_ranking(_sample(), as_of=AS_OF)
    assert list(wide.columns) == list(rpa.SHIP_BUCKETS)
    # Rows are Portfolio × Format, sorted by total desc.
    assert list(wide.index) == [
        "Butter · Extended", "Butter · Cultured", "Cheese · Aseptic"]
    # Butter · Cultured: Acme 800 + Eve 50 at <30.
    assert wide.loc["Butter · Cultured", rpa.SHIP_BUCKET_NEAR] == pytest.approx(850.0)
    # Butter · Extended: Beta 1500 at 30–90.
    assert wide.loc["Butter · Extended", rpa.SHIP_BUCKET_MID] == pytest.approx(1500.0)
    # Cheese · Aseptic: Cee 400 at >90 (Dee dropped — 0 in-year lbs).
    assert wide.loc["Cheese · Aseptic", rpa.SHIP_BUCKET_FAR] == pytest.approx(400.0)


# ── High-urgency watchlist ───────────────────────────────────────────────────

def test_high_urgency_blank_action_days_and_ranking():
    high = rpa.build_high_urgency_programs(_sample(), as_of=AS_OF, quantile=0.5)
    assert list(high.columns) == list(rpa.WATCHLIST_COLUMNS)
    assert rpa.COL_DAYS_TO_SHIP in high.columns
    # Dee (prob 1.0, locked) must never appear.
    assert not high[rpa.COL_PROGRAM].str.contains("Dee").any()
    # Beta dominates, Acme next (urgency = vol×(1−prob)×exp(−max(0,days)/90)).
    beta = 2000.0 * 0.7 * exp(-41.0 / 90.0)
    acme = 1000.0 * 0.10 * exp(-5.0 / 90.0)
    assert list(high[rpa.COL_PROGRAM].str[:4]) == ["Beta", "Acme"]
    assert high.iloc[0][rpa.COL_URGENCY] == pytest.approx(beta, rel=1e-6)
    assert high.iloc[1][rpa.COL_URGENCY] == pytest.approx(acme, rel=1e-6)
    # Action is blank for the planner to fill in.
    assert (high[rpa.COL_ACTION] == "").all()
    # Days-to-Ship is whole days from as_of.
    assert int(high.iloc[0][rpa.COL_DAYS_TO_SHIP]) == 41   # Beta
    assert int(high.iloc[1][rpa.COL_DAYS_TO_SHIP]) == 5    # Acme


def test_high_urgency_empty_when_all_locked():
    df = _sample()
    df["LE Probability"] = 1.0
    assert rpa.build_high_urgency_programs(df, as_of=AS_OF).empty


def test_apply_watchlist_filters():
    tbl = rpa.build_high_urgency_programs(_sample(), as_of=AS_OF, quantile=0.5)
    # Both survivors are Butter.
    assert set(rpa.apply_watchlist_filters(tbl, portfolios=["Butter"])
               [rpa.COL_PORTFOLIO]) == {"Butter"}
    assert rpa.apply_watchlist_filters(tbl, portfolios=["Cheese"]).empty
    # Volume ≥ 1500 keeps only Beta (2000); drops Acme (1000).
    vol = rpa.apply_watchlist_filters(tbl, min_volume=1500.0)
    assert list(vol[rpa.COL_PROGRAM].str[:4]) == ["Beta"]
    # Ship window 30–90 keeps only Beta (41 days).
    ship = rpa.apply_watchlist_filters(tbl, ship_buckets=[rpa.SHIP_BUCKET_MID])
    assert list(ship[rpa.COL_PROGRAM].str[:4]) == ["Beta"]
    # Probability 20–50% keeps only Beta (0.30); drops Acme (0.90).
    prob = rpa.apply_watchlist_filters(tbl, prob_range=(0.20, 0.50))
    assert list(prob[rpa.COL_PROGRAM].str[:4]) == ["Beta"]


# ── Pipeline build-up ────────────────────────────────────────────────────────

def test_pipeline_buildup_segments_sum_to_gross():
    wide = rpa.build_pipeline_buildup(_sample())
    assert list(wide.columns) == list(rpa.BUILDUP_SEGMENTS)
    # Rows are Portfolio × Format, sorted by Gross desc:
    #   Cheese·Cultured (Dee 3000), Butter·Extended (Beta 2000),
    #   Butter·Cultured (Acme 1000 + Eve 100 = 1100), Cheese·Aseptic (Cee 500).
    assert list(wide.index) == [
        "Cheese · Cultured", "Butter · Extended",
        "Butter · Cultured", "Cheese · Aseptic"]
    # Butter · Cultured: FY27 = Acme 800 + Eve 50 = 850; Year1 = 900 + 60 = 960;
    # Gross = 1000 + 100 = 1100 → Year-effect 110, Risk 140.
    bc = wide.loc["Butter · Cultured"]
    assert bc[rpa.SEG_FY27] == pytest.approx(850.0)
    assert bc[rpa.SEG_YEAR_EFFECT] == pytest.approx(110.0)
    assert bc[rpa.SEG_RISK] == pytest.approx(140.0)
    assert bc.sum() == pytest.approx(1100.0)
    # Cheese · Cultured is Dee alone: FY27 0, Year1 100, Gross 3000.
    cc = wide.loc["Cheese · Cultured"]
    assert cc[rpa.SEG_FY27] == pytest.approx(0.0)
    assert cc[rpa.SEG_YEAR_EFFECT] == pytest.approx(100.0)
    assert cc[rpa.SEG_RISK] == pytest.approx(2900.0)
    # Every row's three segments sum to that combo's Gross Pipeline.
    assert cc.sum() == pytest.approx(3000.0)


def test_isclose_guard_sanity():
    # Sanity: concentration math is a plain ratio (guards against unit drift).
    m = rpa.compute_pipeline_metrics(_sample())
    assert isclose(m.committed_concentration, m.committed_lbs / m.in_year_lbs)


# ── Reconciliation with the RO Summary Total B2C row (item 19) ───────────────

def _butter_sample() -> pd.DataFrame:
    """All-Butter rows — every row maps into the RO Summary Total B2C subtotal
    (Butter leaves match PMaj=Butter for any non-blank Supply Format), so the
    per-program tile totals must equal the roll-up Total B2C exactly."""
    return pd.DataFrame({
        "Portfolio Major":       ["Butter", "Butter", "Butter"],
        "Supply Format":         ["Sticks", "Bulk", "Print"],
        "Driver":                ["New", "Change", "New"],
        ANNUAL_OPP_LE:           [15_000_000.0, 8_000_000.0, 5_000_000.0],
        CUR_FISCAL_PROB_LE:      [10_000_000.0, 5_000_000.0, 3_000_000.0],
        "Change Current Fiscal Probabilized Lbs": [0.0, 0.0, 0.0],
        "Prior Year1 Probabilized Lbs":           [0.0, 0.0, 0.0],
        YEAR1_PROB_LE:           [12_000_000.0, 6_000_000.0, 4_000_000.0],
    })


def test_tiles_reconcile_with_summary_total_b2c():
    from data_sources.ro_summary_report import (
        build_summary_report, COL_CURRENT_PLAN, COL_Y1_LATEST, COL_ROW_ID,
    )
    comp = _butter_sample()
    m = rpa.compute_pipeline_metrics(comp)
    report, _warn, _tpl = build_summary_report(comp)
    total = report.loc[report[COL_ROW_ID] == "total_b2c"].iloc[0]
    # Roll-up is in millions (rounded 1dp); per-program tiles are raw lbs.
    assert m.in_year_lbs / 1e6 == pytest.approx(total[COL_CURRENT_PLAN], abs=0.05)
    assert m.full_year_lbs / 1e6 == pytest.approx(total[COL_Y1_LATEST], abs=0.05)
