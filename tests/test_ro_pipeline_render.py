"""Headless render tests for the RO "Pipeline at a Glance" section.

Streamlit is stubbed (no browser / server) so we can call the page's render
functions directly and assert their invariants — 2 charts, a single editable
watchlist with Urgency hidden, a blank Action column, and a graceful bail when
the comparison hasn't built.  Context managers use a real class so exceptions
inside ``with col:`` / ``with expander:`` propagate (a MagicMock __exit__ would
otherwise swallow them and defeat the point of the test).
"""

import sys
from unittest.mock import MagicMock

import pandas as pd
import pytest


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── Install a fake streamlit BEFORE importing the page ───────────────────────
_ST = MagicMock()
_ST.session_state = {}
_ST.fragment = lambda f: f
_ST.columns = lambda spec, **k: [
    _Ctx() for _ in (spec if isinstance(spec, (list, tuple)) else range(spec))
]
_ST.expander = lambda *a, **k: _Ctx()
_ST.container = lambda *a, **k: _Ctx()
_ST.popover = lambda *a, **k: _Ctx()
sys.modules["streamlit"] = _ST
sys.modules.setdefault("streamlit.components", MagicMock())
sys.modules.setdefault("streamlit.components.v1", MagicMock())

from data_sources.ro_comparison import (  # noqa: E402
    ANNUAL_OPP_LE, CUR_FISCAL_PROB_LE, YEAR1_PROB_LE,
)
import data_sources.ro_pipeline_analytics as rpa  # noqa: E402
import pages.demand_planner_analytics_view as page  # noqa: E402


@pytest.fixture
def caps(monkeypatch):
    """Fresh streamlit stub behaviours per test; returns a capture dict."""
    c = {"plotly": [], "editor": []}
    _ST.session_state = {}
    _ST.fragment = lambda f: f
    _ST.columns = lambda spec, **k: [
        _Ctx() for _ in (spec if isinstance(spec, (list, tuple)) else range(spec))
    ]
    _ST.expander = lambda *a, **k: _Ctx()
    _ST.multiselect = lambda *a, **k: k.get("default", [])
    _ST.number_input = lambda *a, **k: k.get("value", 0)
    _ST.slider = lambda *a, **k: k.get("value", (0, 100))
    _ST.button = lambda *a, **k: False
    _ST.checkbox = lambda *a, **k: k.get("value", False)
    _ST.text_input = lambda *a, **k: k.get("value", "")
    _ST.markdown = lambda *a, **k: None
    _ST.caption = lambda *a, **k: None
    _ST.info = lambda *a, **k: None
    _ST.success = lambda *a, **k: None
    _ST.warning = lambda *a, **k: None
    _ST.error = lambda *a, **k: None
    _ST.dataframe = lambda *a, **k: None
    _ST.plotly_chart = lambda fig, **k: c["plotly"].append(k.get("key"))

    def _editor(df, **k):
        c["editor"].append(df)
        return df

    _ST.data_editor = _editor
    # No Fabric in tests: signed-in + empty archive.
    monkeypatch.setattr(page.fabric_signin_widget, "is_fabric_signed_in",
                        lambda: True)
    monkeypatch.setattr(page, "list_pipeline_review_snapshots", lambda: [])
    return c


def _comp() -> pd.DataFrame:
    """Butter (maps into Total B2C) + Cheese (unmapped) per-program frame."""
    return pd.DataFrame({
        "Portfolio Major":    ["Butter", "Butter", "Cheese"],
        "Supply Format":      ["Sticks", "Bulk", "Aseptic"],
        "Customer":           ["Acme", "Beta", "Cee"],
        "Item #":             ["1", "2", "3"],
        "Description":        ["A", "B", "C"],
        "Driver":             ["New", "Change", "New"],
        ANNUAL_OPP_LE:        [45e6, 12e6, 8e6],
        CUR_FISCAL_PROB_LE:   [30e6, 5e6, 4e6],
        YEAR1_PROB_LE:        [40e6, 6e6, 5e6],
        "LE Probability":     [0.97, 0.30, 0.10],
        "LE First Ship Date": ["2026-08-01", "2026-09-15", "2026-07-01"],
    })


def test_section_renders_two_charts_and_watchlist(caps):
    _ST.session_state[page._SS_SUMMARY_DF] = _comp()
    page._render_ro_pipeline_analytics_section()

    assert caps["plotly"] == ["ro_urgency_chart", "ro_buildup_chart"]
    assert len(caps["editor"]) == 1
    ed = caps["editor"][0]
    # Urgency hidden; In-Year + Days-to-Ship shown; Action blank by default.
    assert rpa.COL_URGENCY not in ed.columns
    assert rpa.COL_IN_YEAR in ed.columns
    assert rpa.COL_DAYS_TO_SHIP in ed.columns
    assert (ed[rpa.COL_ACTION] == "").all()
    # The per-Program action store is created.
    assert page._SS_WL_ACTIONS in _ST.session_state


def test_section_bails_when_comparison_not_built(caps):
    # No _SS_SUMMARY_DF in session → render should no-op without raising.
    page._render_ro_pipeline_analytics_section()
    assert caps["plotly"] == []
    assert caps["editor"] == []
