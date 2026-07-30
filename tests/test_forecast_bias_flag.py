"""Forecast-accuracy Trend + Flag helpers (page-level, pure string builders).

Covers the redesigned Forecast Accuracy section: the accuracy Trend read
(Improving / Worsening / Flat over the monthly bias) and the trend-sentence
Flag that names the Corporate × SKU driver.
"""
from __future__ import annotations

from data_sources.demand_plan_comparison import BIAS_FLAG_PRIORITY, BIAS_FLAG_MONITOR


def _page():
    """Lazy import of the page module.

    Imported inside the tests (not at module scope) on purpose: these are pure
    string helpers that never touch streamlit, and importing the page at
    collection time would bind the REAL streamlit to it before
    ``test_ro_pipeline_render`` installs its fake-streamlit stub — defeating
    that module's chart-capture (it assumes it is the first to import the page).
    """
    import pages.demand_planner_analytics_view as p
    return p


def test_bias_trend_direction():
    p = _page()
    # Error shrinking over the window → Improving (green).
    arrow, word, color = p._bias_trend([0.20, 0.18, 0.16, 0.06, 0.04, 0.02])
    assert word == "Improving" and arrow == "↘" and color == "#1b7f3a"
    # Error growing → Worsening (red).
    _a, word, color = p._bias_trend([0.02, 0.03, 0.05, 0.15, 0.18, 0.22])
    assert word == "Worsening" and color == "#c0392b"
    # Flat within the band → Flat.
    assert p._bias_trend([0.10] * 6)[1] == "Flat"
    # Fewer than 4 real months → Flat (not enough to call a direction).
    assert p._bias_trend([0.1, 0.2])[1] == "Flat"


def test_bias_flag_sentence_leads_with_trend_and_names_driver():
    p = _page()
    html = p._bias_flag_html(
        BIAS_FLAG_PRIORITY, "Improving", "Under", "Costco × KS Butter 1lb")
    assert 'chip pri' in html                       # Priority chip kept
    assert "Improving accuracy" in html             # trend leads
    assert "under-forecast" in html                 # direction
    assert "driven by Costco × KS Butter 1lb" in html   # named driver


def test_bias_flag_sentence_without_driver_or_severity():
    p = _page()
    # Unflagged, balanced, no driver → just the trend sentence, no chip.
    html = p._bias_flag_html("", "Flat", "Balanced", None)
    assert html == '<span class="flagmsg">Flat accuracy</span>'
    # Monitor keeps its chip; no driver clause when driver is None.
    html2 = p._bias_flag_html(BIAS_FLAG_MONITOR, "Worsening", "Over", None)
    assert "chip mon" in html2 and "Worsening accuracy, over-forecast" in html2
    assert "driven by" not in html2
