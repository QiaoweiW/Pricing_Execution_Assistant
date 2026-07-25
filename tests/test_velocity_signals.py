"""Unit tests for the velocity leading-signal analytics (pure)."""
import numpy as np
import pandas as pd

import data_sources.velocity_signals as vsig


def test_cross_correlation_lag_recovers_shift():
    # A follower that is the leader shifted right by 2 weeks → lag ≈ 2.
    base = pd.Series([0, 5, 12, 20, 12, 5, 0, 4, 11, 19, 12, 6, 0, 5, 12, 20], dtype=float)
    follower = base.shift(2)
    lag, corr = vsig.cross_correlation_lag(base, follower, max_lag=5)
    assert lag == 2 and corr > 0.5


def test_cross_correlation_lag_none_when_unrelated():
    rng = np.arange(20, dtype=float)
    lag, _ = vsig.cross_correlation_lag(pd.Series(rng), pd.Series(rng[::-1]), max_lag=5)
    assert lag is None                                # anti-correlated → no lag


def _flat(v, n=8):
    return pd.Series([v] * n, dtype=float)


def test_signal_standing_gap_shelf_steady_demand_low():
    sig = vsig.divergence_signal(_flat(101), _flat(34), _flat(38), _flat(0.99))
    assert sig["level"] == vsig.LEVEL_WATCH
    assert "below its norm" in sig["headline"]


def test_signal_shelf_accelerating_ahead_of_orders():
    shelf = pd.Series(np.linspace(100, 130, 8))       # rising fast
    demand = _flat(100)                               # flat
    sig = vsig.divergence_signal(shelf, demand, _flat(100), _flat(0.99))
    assert sig["level"] == vsig.LEVEL_WATCH
    assert "ahead of orders" in sig["headline"]


def test_signal_shipping_into_slowdown_is_alert():
    shelf = pd.Series(np.linspace(120, 90, 8))        # falling
    supply = _flat(120)                               # hot vs its own norm
    sig = vsig.divergence_signal(shelf, _flat(100), supply, _flat(0.99))
    assert sig["level"] == vsig.LEVEL_ALERT
    assert "slowing shelf" in sig["headline"]


def test_signal_aligned():
    sig = vsig.divergence_signal(_flat(100), _flat(101), _flat(99), _flat(0.99))
    assert sig["level"] == vsig.LEVEL_ALIGNED
