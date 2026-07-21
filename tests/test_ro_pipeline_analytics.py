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
    """Five programs spanning ship buckets, prob tiers, and a locked win."""
    return pd.DataFrame({
        "Customer":            ["Acme", "Beta", "Cee", "Dee", "Eve"],
        "Item #":              ["1", "2", "3", "4", "5"],
        "Description":         ["A", "B", "C", "D", "E"],
        "Portfolio Major":     ["Butter", "Butter", "Cheese", "Cheese", "Butter"],
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


# ── Urgency ranking ──────────────────────────────────────────────────────────

def test_urgency_ranking_buckets_and_sort():
    wide = rpa.build_urgency_ranking(_sample(), as_of=AS_OF)
    assert list(wide.columns) == list(rpa.SHIP_BUCKETS)
    # Sorted by row total desc: Butter (2350) then Cheese (400).
    assert list(wide.index) == ["Butter", "Cheese"]
    # Butter: <30 = Acme 800 + Eve 50 = 850; 30–90 = Beta 1500; >90 = 0.
    assert wide.loc["Butter", rpa.SHIP_BUCKET_NEAR] == pytest.approx(850.0)
    assert wide.loc["Butter", rpa.SHIP_BUCKET_MID] == pytest.approx(1500.0)
    assert wide.loc["Butter", rpa.SHIP_BUCKET_FAR] == pytest.approx(0.0)
    # Cheese: Cee 400 at >90; Dee has 0 in-year lbs so it drops out.
    assert wide.loc["Cheese", rpa.SHIP_BUCKET_FAR] == pytest.approx(400.0)


# ── High-urgency watchlist ───────────────────────────────────────────────────

def test_action_tiers():
    assert rpa._action_for_prob(1.0) is None          # locked
    assert rpa._action_for_prob(float("nan")) is None
    assert rpa._action_for_prob(0.999) == rpa.ACTION_PROTECT
    assert rpa._action_for_prob(0.80) == rpa.ACTION_PROTECT
    assert rpa._action_for_prob(0.79) == rpa.ACTION_CHASE
    assert rpa._action_for_prob(0.50) == rpa.ACTION_CHASE
    assert rpa._action_for_prob(0.49) == rpa.ACTION_QUALIFY
    assert rpa._action_for_prob(0.20) == rpa.ACTION_QUALIFY
    assert rpa._action_for_prob(0.19) == rpa.ACTION_KILL
    assert rpa._action_for_prob(0.0) == rpa.ACTION_KILL


def test_high_urgency_excludes_locked_and_ranks():
    # quantile=0.5 keeps the upper half so we can assert ordering + actions.
    high = rpa.build_high_urgency_programs(_sample(), as_of=AS_OF, quantile=0.5)
    assert list(high.columns) == list(rpa.WATCHLIST_COLUMNS)
    # Dee (prob 1.0, locked) must never appear.
    assert not high[rpa.COL_PROGRAM].str.contains("Dee").any()
    # Urgency = vol×(1−prob)×exp(−max(0,days)/90). Beta dominates, Acme next.
    beta = 2000.0 * 0.7 * exp(-41.0 / 90.0)
    acme = 1000.0 * 0.10 * exp(-5.0 / 90.0)
    assert list(high[rpa.COL_PROGRAM].str[:4]) == ["Beta", "Acme"]
    assert high.iloc[0][rpa.COL_URGENCY] == pytest.approx(beta, rel=1e-6)
    assert high.iloc[1][rpa.COL_URGENCY] == pytest.approx(acme, rel=1e-6)
    # Actions are prob-tiered: Beta 0.30 → Qualify; Acme 0.90 → Protect.
    assert high.iloc[0][rpa.COL_ACTION] == rpa.ACTION_QUALIFY
    assert high.iloc[1][rpa.COL_ACTION] == rpa.ACTION_PROTECT


def test_high_urgency_empty_when_all_locked():
    df = _sample()
    df["LE Probability"] = 1.0
    assert rpa.build_high_urgency_programs(df, as_of=AS_OF).empty


# ── Probability buckets ──────────────────────────────────────────────────────

def test_probability_buckets():
    wide = rpa.build_probability_buckets(_sample())
    assert list(wide.columns) == list(rpa.PROB_BUCKETS)
    # Gross (unweighted) lbs. Cheese total 3500 > Butter 3100 → Cheese first.
    assert list(wide.index) == ["Cheese", "Butter"]
    # Butter: Dead = Eve 100; In-play = Beta 2000; Committed = Acme 1000.
    assert wide.loc["Butter", rpa.PROB_BUCKET_DEAD] == pytest.approx(100.0)
    assert wide.loc["Butter", rpa.PROB_BUCKET_INPLAY] == pytest.approx(2000.0)
    assert wide.loc["Butter", rpa.PROB_BUCKET_COMMITTED] == pytest.approx(1000.0)
    # Cheese: Committed = Cee 500 + Dee 3000 = 3500.
    assert wide.loc["Cheese", rpa.PROB_BUCKET_COMMITTED] == pytest.approx(3500.0)


def test_isclose_guard_sanity():
    # Sanity: concentration math is a plain ratio (guards against unit drift).
    m = rpa.compute_pipeline_metrics(_sample())
    assert isclose(m.committed_concentration, m.committed_lbs / m.in_year_lbs)
