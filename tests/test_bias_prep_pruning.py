"""The bias prep prunes the tracker — without moving a single number.

``_prepare_bias_inputs`` used to enrich all 1,552,423 tracker rows to read the
~18,600 inside the bias window (1.2%).  It now resolves cycle horizons from a
cheap raw projection, prunes to the window, and enriches only that.

Measured on live data: the bias table went 113.2s → 25.2s and the Corporate ×
SKU drivers 38.4s → 13.9s, with both outputs byte-identical to the pre-change
baseline.  These tests pin the two invariants that make the prune safe, both
of which I got wrong on the first attempt:

1. Horizons must be read BEFORE the prune and from the full tracker — a
   cycle's horizon start is normally outside the bias window.
2. The month parse must be the canonical one.  A bare ``pd.to_datetime``
   silently NaT'd six of nine cycles (the tracker mixes Excel serials with
   M/D/YYYY, and pandas infers one format per column), which dropped them off
   the lag-1 timeline and changed 83% of the bias values.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import data_sources.demand_plan_comparison as dpc


def _tracker(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=[
        "Start of Month", "Item", "Item Description", "Party Site Number",
        "Demand Plan Pounds", "Forecast Type", "Cycle",
    ])


_UNSET = object()


def _row(month, cycle, item="340021", lbs="1000", ftype=_UNSET):
    """One tracker row.  ``ftype`` defaults to Base Plan but accepts "" / None
    verbatim — an ``or`` default would swallow exactly the cases under test."""
    return [month, item, "Milk", "1001", lbs,
            dpc.FORECAST_BASE_PLAN if ftype is _UNSET else ftype, cycle]


# ── Horizons: full history, mixed formats ────────────────────────────────────

def test_horizons_come_from_the_whole_tracker_not_the_bias_window():
    """A cycle's horizon START is normally outside the window being analysed."""
    trk = _tracker([
        _row("2026-01-01", "C1"),      # horizon start, well before the window
        _row("2026-07-01", "C1"),
    ])
    starts = dpc._cycle_horizon_starts_raw(trk)
    assert starts["C1"][0] == date(2026, 1, 1)


def test_serials_and_text_together_do_not_lose_cycles():
    """The regression that changed 83% of the numbers.

    The live tracker carries Excel serials alongside M/D/YYYY text.  A bare
    ``pd.to_datetime`` locks onto one shape and NaTs the rest, which dropped
    six of nine cycles off the lag-1 timeline.  The canonical parser handles
    this pairing, which is the one the real file actually contains.
    """
    trk = _tracker([
        _row("3/1/2026", "C1"),        # US text
        _row("4/1/2026", "C2"),        # US text
        _row(46173, "C3"),             # Excel serial = 2026-05-01
    ])
    starts = dpc._cycle_horizon_starts_raw(trk)
    assert set(starts) == {"C1", "C2", "C3"}, (
        f"a cycle was lost to date parsing: {sorted(starts)}"
    )
    assert starts["C1"][0] == date(2026, 3, 1)
    assert starts["C2"][0] == date(2026, 4, 1)
    assert starts["C3"][0] == date(2026, 5, 1)


@pytest.mark.xfail(
    reason="Latent bug in _vectorised_start_of_month, not introduced here: "
           "its string fallback is a single pd.to_datetime, so a column mixing "
           "US *and* ISO text loses whichever style pandas did not infer. The "
           "live tracker pairs serials with one text style, so it parses "
           "cleanly today (proven by the byte-identical baseline) — but the "
           "day a tracker carries both text styles, rows would vanish "
           "silently. Fix is the format='mixed' retry used in ro_dates.",
    strict=True,
)
def test_two_text_date_formats_in_one_column_currently_lose_a_cycle():
    trk = _tracker([
        _row("3/1/2026", "C1"),        # US text
        _row("2026-04-01", "C2"),      # ISO text — silently NaT'd today
    ])
    assert set(dpc._cycle_horizon_starts_raw(trk)) == {"C1", "C2"}


def test_the_raw_and_enriched_horizon_readers_agree():
    """The cheap path must return exactly what the expensive one did."""
    trk = _tracker([
        _row("3/1/2026", "C1"), _row("2026-08-01", "C1"),
        _row("5/1/2026", "C2"), _row(46265, "C2"),          # 2026-08-01
    ])
    dim, _ = dpc._build_augmented_dim_frame(trk, None, None)
    enriched = dpc._enrich_tracker(trk, dim)
    assert dpc._cycle_horizon_starts_raw(trk) == dpc._cycle_horizon_starts(enriched)


@pytest.mark.parametrize("ftype", ["Order", "Actual", "", None])
def test_only_plan_rows_define_a_horizon(ftype):
    trk = _tracker([_row("2026-01-01", "C1", ftype=ftype),
                    _row("2026-05-01", "C1")])
    assert dpc._cycle_horizon_starts_raw(trk)["C1"][0] == date(2026, 5, 1)


def test_a_tracker_without_the_columns_yields_no_horizons():
    assert dpc._cycle_horizon_starts_raw(pd.DataFrame()) == {}
    assert dpc._cycle_horizon_starts_raw(None) == {}
    assert dpc._cycle_horizon_starts_raw(pd.DataFrame({"x": [1]})) == {}


# ── The prune itself ─────────────────────────────────────────────────────────

def test_the_slice_keeps_exactly_the_window():
    trk = _tracker([
        _row("2026-03-01", "C1"), _row("2026-04-01", "C1"),
        _row("2026-09-01", "C1"), _row("2027-01-01", "C1"),
    ])
    kept = dpc._slice_tracker_to_months(trk, [date(2026, 3, 1), date(2026, 4, 1)])
    assert len(kept) == 2
    assert set(pd.to_datetime(kept["Start of Month"]).dt.date) == {
        date(2026, 3, 1), date(2026, 4, 1)}


def test_the_slice_handles_every_date_format_the_tracker_uses():
    trk = _tracker([_row("3/1/2026", "C1"),      # US text
                    _row(46082, "C2"),           # Excel serial = 2026-03-01
                    _row("9/1/2026", "C3")])     # outside the window
    kept = dpc._slice_tracker_to_months(trk, [date(2026, 3, 1)])
    assert set(kept["Cycle"]) == {"C1", "C2"}, "a date format was missed"


def test_a_tracker_without_a_month_column_passes_through_untouched():
    """Emptying it would be a far worse failure than being slow."""
    trk = pd.DataFrame({"Item": ["1"], "Cycle": ["C1"]})
    assert len(dpc._slice_tracker_to_months(trk, [date(2026, 3, 1)])) == 1


def test_an_empty_window_keeps_nothing():
    trk = _tracker([_row("2026-03-01", "C1")])
    assert dpc._slice_tracker_to_months(trk, []).empty


# ── End to end: the prep still resolves the same cycles ──────────────────────

def test_the_prep_maps_the_same_lag1_cycles_as_an_unpruned_read():
    """Guards the ordering: horizons before prune, prune before enrich."""
    trk = _tracker([
        # C1 starts in Mar and runs forward; C2 starts in Apr.
        _row("2026-03-01", "C1"), _row("2026-04-01", "C1"), _row("2026-05-01", "C1"),
        _row("2026-04-01", "C2"), _row("2026-05-01", "C2"),
    ])
    f = dpc.ComparisonFilters(
        current_cycle="C2", prior_cycle="C1",
        actual_start=date(2026, 3, 1), actual_end=date(2026, 5, 1),
        forecast_start=date(2026, 6, 1), forecast_end=date(2026, 7, 1),
        prior_month=date(2026, 5, 1))
    bi = dpc._prepare_bias_inputs(trk, None, None, None, f, n_months=3)

    expected = dpc._map_lag1_cycles(
        dpc._months_back(date(2026, 5, 1), 3),
        dpc._cycle_horizon_starts_raw(trk))
    assert bi.month_cycles == [c for c, _l, _fb in expected]
    assert "C1" in bi.month_cycles, "the earliest cycle must survive the prune"
    # And the enriched frame really is pruned to the window.
    assert set(bi.trk["month"]) <= set(bi.bias_months)
