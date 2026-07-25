"""
Leading-signal analytics for the Velocity Analysis chart — pure, testable.

Turns three indexed weekly series (consumer sell-through, our retailer-order
"demand" velocity, our shipment "supply" velocity) plus fill rate into:

* a **lead/lag estimate** — how many weeks our orders follow consumer
  sell-through (cross-correlation of week-over-week *changes*, so it keys on
  turning points, not on shared trend/seasonality); and
* a **divergence signal** — a traffic-light so-what a demand planner can act on:
  demand running ahead of orders (upside / stockout risk), shipping into a
  slowdown (retail inventory build / bullwhip), or a service slip (fill down).

All functions are Streamlit-/IO-free.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

# Signal levels (traffic light).
LEVEL_ALIGNED: str = "aligned"
LEVEL_WATCH: str = "watch"
LEVEL_ALERT: str = "alert"

# Tuning (index points unless noted).  Kept explicit so a planner can reason
# about what trips each warning.  IMPORTANT: each series is rebased to its OWN
# median, so we NEVER compare index levels across series — only each series'
# level vs its own 100, and cross-series *direction/timing*.
_SLOPE_MEANINGFUL: float = 1.0   # index-pts/week to call a real trend
_STEADY_BAND: float = 12.0       # within ±this of 100 ⇒ "at its normal"
_LEVEL_GAP: float = 20.0         # a series this far from its own 100 ⇒ notable
_FILL_WARN: float = 0.97         # fill rate below this while demand is up → risk


def cross_correlation_lag(
    leader: pd.Series, follower: pd.Series, *,
    max_lag: int = 8, min_overlap: int = 6, min_corr: float = 0.2,
) -> tuple[Optional[int], Optional[float]]:
    """Weeks the *follower* lags the *leader*, by max correlation of week-over-week
    changes.  Returns ``(lag, corr)`` — ``(None, corr?)`` when no lag clears
    ``min_corr`` or there isn't enough overlap.  Δ-based so it tracks *turning
    points*, not a common trend."""
    a = pd.to_numeric(pd.Series(leader).reset_index(drop=True), errors="coerce").diff()
    b = pd.to_numeric(pd.Series(follower).reset_index(drop=True), errors="coerce").diff()
    n = min(len(a), len(b))
    best_lag, best_corr = None, -2.0
    for lag in range(0, max_lag + 1):
        x = a.iloc[:n - lag] if lag else a.iloc[:n]
        y = b.iloc[lag:n]
        pair = pd.concat([x.reset_index(drop=True), y.reset_index(drop=True)],
                         axis=1).dropna()
        if len(pair) < min_overlap or pair.iloc[:, 0].std() == 0 or pair.iloc[:, 1].std() == 0:
            continue                              # need variance on both sides
        c = pair.iloc[:, 0].corr(pair.iloc[:, 1])
        if pd.notna(c) and c > best_corr:
            best_corr, best_lag = float(c), lag
    if best_lag is None or best_corr < min_corr:
        return None, (best_corr if best_lag is not None else None)
    return best_lag, best_corr


def _recent_slope(series: pd.Series, n: int) -> Optional[float]:
    """Least-squares slope (per week) over the last *n* non-null points."""
    s = pd.to_numeric(pd.Series(series), errors="coerce").dropna().tail(n)
    if len(s) < 3:
        return None
    x = np.arange(len(s), dtype=float)
    return float(np.polyfit(x, s.to_numpy(dtype=float), 1)[0])


def _last(series: pd.Series) -> Optional[float]:
    s = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
    return float(s.iloc[-1]) if not s.empty else None


def divergence_signal(
    sell_through_index: pd.Series,
    demand_index: pd.Series,
    supply_index: pd.Series,
    fill_rate: Optional[pd.Series] = None,
    *,
    recent_weeks: int = 6,
) -> dict:
    """Traffic-light divergence read on the last *recent_weeks*.

    Returns ``{"level", "icon", "headline", "detail"}``.  Every comparison is
    baseline-safe: each series vs its OWN 100 (level), and sell-through vs orders
    by *direction* (slope).  Priority: shipping into a slowdown → demand ahead of
    orders → standing gap (our velocity off its norm while the shelf holds) →
    aligned; a fill-rate slip is appended when demand is up.
    """
    iri_slope = _recent_slope(sell_through_index, recent_weeks)
    dem_slope = _recent_slope(demand_index, recent_weeks)
    sup_slope = _recent_slope(supply_index, recent_weeks)
    iri_last = _last(sell_through_index)
    dem_last = _last(demand_index)
    sup_last = _last(supply_index)
    fill_last = _last(fill_rate) if fill_rate is not None else None

    if iri_slope is None or iri_last is None:
        return {"level": LEVEL_ALIGNED, "icon": "⚪",
                "headline": "Not enough overlapping weeks to read a signal.",
                "detail": "Widen the Week window or the filters so sell-through and "
                          "our orders share weeks."}

    def pct(v):
        return f"{v:.0f}% of normal" if v is not None else "n/a"

    fill_note = ""
    if fill_last is not None and fill_last < _FILL_WARN and iri_slope > 0:
        fill_note = (f"  Fill rate is {fill_last * 100:.0f}% while sell-through rises "
                     "— watch service.")

    # 1) Shipping into a slowing shelf: consumer cooling, our supply still hot vs
    #    ITS OWN norm (within-series level) or still climbing.
    if (iri_slope < -_SLOPE_MEANINGFUL and sup_last is not None
            and (sup_last > 100 + _STEADY_BAND or (sup_slope is not None and sup_slope > _SLOPE_MEANINGFUL))):
        return {
            "level": LEVEL_ALERT, "icon": "🔴",
            "headline": "Shipping into a slowing shelf — retail inventory-build / bullwhip risk.",
            "detail": (f"Sell-through is falling ({iri_slope:+.1f} idx-pts/wk) while our supply "
                       f"velocity is {pct(sup_last)} and not easing.  Confirm downstream "
                       "inventory before shipping to plan." + fill_note),
        }

    # 2) Consumer demand accelerating ahead of our orders (direction, not level).
    if iri_slope > _SLOPE_MEANINGFUL and (dem_slope is None or dem_slope < iri_slope * 0.5):
        return {
            "level": LEVEL_WATCH, "icon": "🟠",
            "headline": "Sell-through accelerating ahead of orders — upside / stock-out risk.",
            "detail": (f"Consumer sell-through is rising ({iri_slope:+.1f} idx-pts/wk) but our "
                       "orders aren't following yet"
                       + (f" ({dem_slope:+.1f}/wk)" if dem_slope is not None else "")
                       + ".  Check for under-ordering / replenishment lag." + fill_note),
        }

    # 3) Standing gap: the shelf sits at its normal but our velocity is well off
    #    ITS OWN norm (both statements are within-series, so this is valid).
    steady_shelf = abs(iri_last - 100) <= _STEADY_BAND
    if steady_shelf and dem_last is not None and dem_last <= 100 - _LEVEL_GAP:
        return {
            "level": LEVEL_WATCH, "icon": "🟠",
            "headline": "Our sell-in is running below its norm while the shelf holds — investigate.",
            "detail": (f"Category sell-through is steady ({pct(iri_last)}) but our order velocity "
                       f"is {pct(dem_last)}.  Check share, distribution, mix or scope — a "
                       "persistent gap compounds." + fill_note),
        }
    if steady_shelf and dem_last is not None and dem_last >= 100 + _LEVEL_GAP:
        return {
            "level": LEVEL_WATCH, "icon": "🟠",
            "headline": "Our sell-in is running hot while the shelf is only normal — confirm it's real.",
            "detail": (f"Category sell-through is steady ({pct(iri_last)}) but our order velocity "
                       f"is {pct(dem_last)}.  Confirm the demand is genuine (not forward-buy / "
                       "pipeline fill) before committing supply." + fill_note),
        }

    return {
        "level": LEVEL_ALIGNED, "icon": "🟢",
        "headline": "Sell-through, orders and shipments are moving together.",
        "detail": (f"Shelf {pct(iri_last)}, orders {pct(dem_last)} — no material divergence."
                   + (fill_note or "  Fill rate healthy.")),
    }
