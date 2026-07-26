"""Unit tests for data_sources.demand_quality (pure base/promo analytics)."""
import numpy as np
import pandas as pd
import pytest

import data_sources.iri_velocity as iri
import data_sources.demand_quality as dq


def _raw_quality() -> pd.DataFrame:
    cols = [iri.COL_GEOGRAPHY, iri.COL_BRAND, iri.COL_SUBTYPE, iri.COL_PROCESS,
            iri.COL_SIZE, iri.COL_WEEK, iri.COL_U_SALES, iri.COL_UNITS_PER_STORE,
            dq.COL_BASE_UNITS, dq.COL_INC_UNITS, dq.COL_DOLLAR, dq.COL_BASE_PRICE, dq.COL_ACV]
    rows = [
        # geo, brand, sub, proc, size, week, U, UPSS, base, inc, $, basePrice, ACV
        ["A", "DARIGOLD", "REGULAR", "ESL", "S", "1/5/2025", 300, 30, 300, 0, 1500, 5.0, 0],
        ["A", "DARIGOLD", "REGULAR", "ESL", "S", "1/12/2025", 400, 40, 300, 100, 1800, 5.0, 20],
        ["A", "DARIGOLD", "REGULAR", "ESL", "S", "1/19/2025", 500, 50, 250, 250, 2000, 5.0, 30],
    ]
    return pd.DataFrame(rows, columns=cols)


def test_build_iri_quality_base_incremental_split():
    q = dq.build_iri_quality(_raw_quality())
    w = q.weekly
    # stores = 10 each week → base/total velocity exact.
    assert list(w[dq.TOTAL_VEL].round(1)) == [30.0, 40.0, 50.0]
    assert list(w[dq.BASE_VEL].round(1)) == [30.0, 30.0, 25.0]
    assert list(w[dq.LIFT_PCT].round(1)) == [0.0, 33.3, 100.0]
    assert list(w[dq.DEPTH_PCT].round(1)) == [0.0, 10.0, 20.0]
    # Fixed baselines: base median 30, total median 40.
    assert q.base_baseline == pytest.approx(30.0)
    assert q.total_baseline == pytest.approx(40.0)
    assert list(w[dq.BASE_INDEX].round(1)) == [100.0, 100.0, 83.3]
    # Efficiency NaN when ACV<2 (week1), else lift/ACV.
    assert np.isnan(w[dq.EFFICIENCY].iloc[0])
    assert w[dq.EFFICIENCY].iloc[1] == pytest.approx(33.3 / 20, rel=1e-2)
    # New columns: stores (10 each), distribution index (flat 100), abs base units.
    assert list(w[dq.STORES].round(1)) == [10.0, 10.0, 10.0]
    assert list(w[dq.DIST_INDEX].round(1)) == [100.0, 100.0, 100.0]
    assert list(w[dq.BASE_UNITS]) == [300, 300, 250]


def _weekly(base_index, total_index, base_vel=None, stores=None) -> pd.DataFrame:
    n = len(base_index)
    d = {iri.WEEK_START: [_mon(k) for k in range(n)],
         dq.BASE_INDEX: base_index, dq.TOTAL_INDEX: total_index}
    if base_vel is not None:
        d[dq.BASE_VEL] = base_vel
    if stores is not None:
        d[dq.STORES] = stores
    return pd.DataFrame(d)


def test_base_erosion_significant_masked_and_rate_driven():
    n = 10
    w = _weekly(list(np.linspace(100, 85, n)), [110.0] * n,
                base_vel=list(np.linspace(20, 17, n)), stores=[100.0] * n)
    sig = dq.base_erosion_signal(w)
    assert "ERODING" in sig["headline"]
    assert sig["significant"] and sig["level"] == dq.LEVEL_ALERT   # total holds → masked
    assert sig["base_slope"] < 0 and sig["base_r2"] > 0.99
    assert "rate-of-sale" in sig["detail"]                         # stores flat → velocity-driven


def test_base_erosion_not_significant_reads_holding():
    w = _weekly([100, 102, 98, 101, 99, 100, 101, 99, 100, 102], [110.0] * 10)
    sig = dq.base_erosion_signal(w)
    assert "HOLDING" in sig["headline"] and not sig["significant"]


def test_base_erosion_min_sample_guard():
    w = _weekly([100, 96, 92], [110, 110, 110])       # only 3 weeks < min points
    sig = dq.base_erosion_signal(w)
    assert "Not enough weeks" in sig["headline"] and sig["base_slope"] is None


def test_promo_response_curve():
    n = 12
    depth = list(np.linspace(2, 20, n))
    w = pd.DataFrame({dq.DEPTH_PCT: depth, dq.LIFT_PCT: [2 * x + 5 for x in depth]})
    rc = dq.promo_response_curve(w)
    assert rc["available"] and rc["n"] == n
    assert rc["marginal"] == pytest.approx(2.0, rel=1e-6)          # lift = 2·depth + 5
    assert rc["r2"] == pytest.approx(1.0, abs=1e-9)


def test_promo_economics_signal():
    q = dq.build_iri_quality(_raw_quality())
    sig = dq.promo_economics_signal(q.weekly)
    assert sig["lift_recent"] == pytest.approx((0 + 33.3 + 100) / 3, rel=1e-2)
    assert sig["level"] == dq.LEVEL_WATCH          # recent lift ≥ 30%


# ── Promo onsets + cohort ────────────────────────────────────────────────────

def _mon(w):
    return pd.Timestamp("2025-01-06") + pd.Timedelta(days=7 * w)


def test_promo_onsets():
    # Consecutive weeks → one onset; a gap starts a new onset.
    weeks = [_mon(0), _mon(1), _mon(2), _mon(5), _mon(6)]
    onsets = dq.promo_onsets(set(weeks))
    assert onsets == [_mon(0), _mon(5)]


def _cohort_weekly() -> pd.DataFrame:
    base = np.full(20, 100.0)
    total = np.full(20, 100.0)
    for w0 in (4, 14):                     # two well-separated onsets
        total[w0:w0 + 3] = 130.0           # in-promo spike (offset 0..2)
        base[w0 + 3:w0 + 6] = 88.0         # base dips after (borrow)
        total[w0 + 3:w0 + 6] = 85.0        # total dips below baseline after
    return pd.DataFrame({
        iri.WEEK_START: [_mon(w) for w in range(20)],
        dq.BASE_INDEX: base, dq.TOTAL_INDEX: total,
    })


def test_promo_cohort_detects_borrow():
    coh = dq.build_promo_cohort(_cohort_weekly(), [_mon(4), _mon(14)],
                                k_pre=2, k_post=5, min_events=2)
    assert coh.n_events == 2
    assert coh.summary["base_shift_pct"] == pytest.approx(-12.0)   # post 88 − pre 100
    assert coh.summary["pull_forward_ratio"] == pytest.approx(0.5)  # deficit 15 / lift 30
    sig = dq.promo_cohort_signal(coh)
    assert "BORROW" in sig["headline"] and sig["level"] == dq.LEVEL_ALERT


def test_promo_cohort_too_few_events():
    coh = dq.build_promo_cohort(_cohort_weekly(), [_mon(4)], min_events=3)
    assert coh.n_events < 3 and coh.curve.empty
    assert "Not enough" in dq.promo_cohort_signal(coh)["headline"]
