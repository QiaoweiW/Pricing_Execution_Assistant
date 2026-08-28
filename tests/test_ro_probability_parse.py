"""Unit tests for ``_parse_probability`` — the shared Probability scale parser.

Probability multiplies every probabilized volume in the RO chain
(``Lbs./yr Exp`` -> ``FY Lbs. Exp`` -> ``FY27 Probabilized | Current Plan``), so
a scale slip here silently rescales the whole R&O pipeline.  Before the fix the
two consumers failed in OPPOSITE directions on the same ``"50%"`` cell:
``ro_seed_pipeline`` stripped it to ``50.0`` (50x overstatement) while
``demand_plan_pipeline`` coerced it to ``NaN`` (row silently dropped).

Pure in-memory frames — no Fabric, no Streamlit.
"""
from __future__ import annotations

import math

import pandas as pd

import data_sources.ro_seed_pipeline as rsp


def _parse(values: list[str], log: rsp._Log | None = None) -> list[float]:
    return rsp._parse_probability(pd.Series(values, dtype="object"), log).tolist()


# ── Scale handling ───────────────────────────────────────────────────────────

def test_fraction_passes_through_unchanged():
    assert _parse(["0.5", "0.75", "0"]) == [0.5, 0.75, 0.0]


def test_percent_marker_is_divided_by_100():
    # The regression: "50%" must be 0.5, not 50.0.
    assert _parse(["50%", " 75 % ", "100%"]) == [0.5, 0.75, 1.0]


def test_bare_value_above_one_is_treated_as_percent():
    assert _parse(["50", "75"]) == [0.5, 0.75]


def test_certainty_is_not_rescaled():
    # 1 is a probability of 1.0 (certainty), NOT 1%.  The >1 guard must exclude it.
    assert _parse(["1", "1.0"]) == [1.0, 1.0]


# ── Unreadable / out-of-range input ──────────────────────────────────────────

def test_blank_and_text_become_nan():
    out = _parse(["", "abc", "n/a"])
    assert all(math.isnan(v) for v in out)


def test_out_of_range_becomes_nan_not_zero():
    # NaN (missing data), never 0.0 — a silent zero reads as a real
    # zero-volume opportunity and hides the bad input.
    out = _parse(["150", "-0.2"])
    assert all(math.isnan(v) for v in out)


# ── Logging ──────────────────────────────────────────────────────────────────

def test_bare_percent_is_warned_not_silently_normalised():
    log = rsp._Log()
    _parse(["50", "0.5"], log)
    warnings = [e.text for e in log.entries if e.level == "warning"]
    assert any("no '%' marker" in w for w in warnings)


def test_out_of_range_is_warned():
    log = rsp._Log()
    _parse(["150"], log)
    warnings = [e.text for e in log.entries if e.level == "warning"]
    assert any("outside [0, 1]" in w for w in warnings)


def test_clean_fractions_log_nothing():
    log = rsp._Log()
    _parse(["0.5", "1.0", "0"], log)
    assert [e for e in log.entries if e.level == "warning"] == []


def test_log_is_optional():
    assert _parse(["50%"], None) == [0.5]


# ── Both consumers agree ─────────────────────────────────────────────────────

def test_both_pipelines_share_one_parser():
    # demand_plan_pipeline imports the SAME function — the two can no longer
    # disagree on scale for any input.
    import data_sources.demand_plan_pipeline as dpp

    assert dpp._parse_probability is rsp._parse_probability
