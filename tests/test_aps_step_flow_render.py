"""Headless render tests for the four-step Demand Summary (APS / Oracle) flow.

Streamlit is stubbed (no browser / server) so the page's render functions can
be called directly.  What these tests protect:

* The **structure**: four numbered steps in the IBP module's order — upload,
  check, compare, delete — with the destructive step last and out of the
  happy path.
* The **removal**: no R&O Corporate Group review, no patch uploader, no
  "Apply patch" button.  Unresolved groups now fill themselves in.
* The **stale-picker fix**: a build and a delete both flush every APS cache
  through one helper, so a cycle that was just uploaded is immediately
  selectable in Step 3 (and one just deleted stops being offered).
* The **download cost**: the 47 MB CSV is serialised through a cache rather
  than rebuilt on every rerun of the fragment.

Context managers use a real class so an exception inside ``with expander:``
propagates instead of being swallowed by a MagicMock ``__exit__``.
"""
from unittest.mock import MagicMock

import pandas as pd
import pytest


from tests.streamlit_stub import Ctx as _Ctx, bind, install

_ST = install()

import data_sources.aps_upload_pipeline as ap          # noqa: E402
import pages.demand_planner_analytics_view as page     # noqa: E402


@pytest.fixture
def caps(monkeypatch):
    """Fresh stub behaviours per test; returns a capture dict."""
    c = {"expanders": [], "captions": [], "markdown": [], "downloads": [],
         "frames": [], "success": [], "info": [], "buttons": [],
         "uploaders": []}
    global _ST
    _ST = bind(page)        # configure what the page actually holds

    def _expander(label="", **k):
        c["expanders"].append(label)
        return _Ctx()

    def _uploader(label="", **k):
        c["uploaders"].append(label)
        return None

    def _button(label="", **k):
        c["buttons"].append(label)
        return False

    def _download(label="", **k):
        c["downloads"].append((label, k.get("data")))
        return False

    _ST.expander = _expander
    _ST.file_uploader = _uploader
    _ST.button = _button
    _ST.download_button = _download
    _ST.selectbox = lambda label, options, **k: list(options)[0]
    _ST.radio = lambda label, options, **k: list(options)[0]
    _ST.checkbox = lambda *a, **k: k.get("value", False)
    _ST.caption = lambda t="", **k: c["captions"].append(str(t))
    _ST.markdown = lambda t="", **k: c["markdown"].append(str(t))
    _ST.dataframe = lambda df=None, **k: c["frames"].append(df)
    _ST.success = lambda t="", **k: c["success"].append(str(t))
    _ST.info = lambda t="", **k: c["info"].append(str(t))
    _ST.warning = lambda *a, **k: None
    _ST.error = lambda *a, **k: None
    monkeypatch.setattr(page.fabric_signin_widget, "is_fabric_signed_in",
                        lambda: True)
    return c


def _history() -> pd.DataFrame:
    """A two-cycle history: C5 complete, C6 missing its R&O leg."""
    rows = []
    for cycle, month, n_base, n_ro in (("C5", 46204, 3, 2), ("C6", 46234, 4, 0)):
        for _ in range(n_base):
            rows.append((month, cycle, ap.FORECAST_APS_BASE_PLAN))
        for _ in range(n_ro):
            rows.append((month, cycle, ap.FORECAST_R_AND_O))
    return pd.DataFrame(rows, columns=[ap.COL_MONTH, ap.COL_CYCLE, ap.COL_FORECAST])


# ── Structure ───────────────────────────────────────────────────────────────

def test_the_four_steps_are_numbered_and_in_order(caps, monkeypatch):
    monkeypatch.setattr(page, "_section_load_gate", lambda *a, **k: True)
    monkeypatch.setattr(page, "_cached_aps_history", lambda: _history())
    monkeypatch.setattr(page, "_render_aps_comparison_section",
                        lambda hist: _ST.expander(
                            "**Step 3 · Compare this cycle with the last one**"))
    page._render_demand_summary_aps()

    steps = [e for e in caps["expanders"] if str(e).startswith("**Step")]
    assert len(steps) == 4, caps["expanders"]
    assert [s.split("·")[1].split("**")[0].strip() for s in steps] == [
        "Upload this cycle's plan",
        "Check what landed",
        "Compare this cycle with the last one",
        "Delete a cycle",
    ]


def test_the_destructive_step_is_last(caps, monkeypatch):
    """A first-time planner reading top-to-bottom must meet upload before delete."""
    monkeypatch.setattr(page, "_section_load_gate", lambda *a, **k: True)
    monkeypatch.setattr(page, "_cached_aps_history", lambda: _history())
    monkeypatch.setattr(page, "_render_aps_comparison_section", lambda hist: None)
    page._render_demand_summary_aps()
    steps = [e for e in caps["expanders"] if str(e).startswith("**Step")]
    assert "Delete" in steps[-1] and "Upload" in steps[0]


def test_the_section_stops_at_the_load_gate(caps, monkeypatch):
    """Nothing heavy may run before the planner asks for it."""
    calls = []
    monkeypatch.setattr(page, "_section_load_gate", lambda *a, **k: False)
    monkeypatch.setattr(page, "_cached_aps_history",
                        lambda: calls.append("read") or _history())
    page._render_demand_summary_aps()
    assert calls == [], "the 1.4M-row history was read behind a closed gate"
    assert not [e for e in caps["expanders"] if str(e).startswith("**Step")]


# ── The R&O Corporate Group review is gone ──────────────────────────────────

def test_no_corporate_group_review_or_patch_anywhere_in_the_page():
    """Retired: unresolved groups take the customer name automatically."""
    src = page.__file__
    text = open(src, encoding="utf-8").read()
    for gone in ("_render_aps_corp_review", "build_corp_review",
                 "patch_history_corp", "parse_corp_override_csv",
                 "aps_patch_upload", "aps_patch_apply", "_APS_PATCH_NONCE_KEY"):
        assert gone not in text, f"{gone} survived the removal"


def test_step2_offers_no_patch_uploader(caps, monkeypatch):
    monkeypatch.setattr(page, "_cached_persisted_aps_plan", lambda: None)
    page._render_aps_step2_check(_history())
    assert caps["uploaders"] == [], "Step 2 must not ask for an upload"


# ── Step 2 answers "did my upload land?" ────────────────────────────────────

def test_step2_lists_every_cycle_newest_first(caps, monkeypatch):
    monkeypatch.setattr(page, "_cached_persisted_aps_plan", lambda: None)
    page._render_aps_step2_check(_history())
    assert caps["frames"], "Step 2 renders no table"
    summary = caps["frames"][0]
    assert list(summary["Cycle"]) == ["C6", "C5"]
    assert list(summary["Base Plan rows"]) == [4, 3]
    assert list(summary["R&O rows"]) == [0, 2]


def test_step2_names_the_leg_that_is_missing(caps, monkeypatch):
    """C6 has no R&O rows — say so, and say which file fixes it."""
    monkeypatch.setattr(page, "_cached_persisted_aps_plan", lambda: None)
    page._render_aps_step2_check(_history())
    warned = [c for c in caps["captions"] if "missing" in c]
    assert warned and "C6" in warned[0] and "R&O seed" in warned[0]
    assert "C5" not in warned[0], "the complete cycle must not be flagged"


def test_step2_is_calm_when_there_is_no_history(caps, monkeypatch):
    monkeypatch.setattr(page, "_cached_persisted_aps_plan", lambda: None)
    page._render_aps_step2_check(None)
    assert caps["info"] and "Step 1" in caps["info"][0]
    assert caps["frames"] == []


# ── The stale-picker fix ────────────────────────────────────────────────────

def test_the_flush_helper_clears_the_comparison_options(monkeypatch):
    """The C6 bug: the cycle picker's cache was the one nobody cleared."""
    cleared = []
    for name in ("_cached_persisted_aps_plan", "_cached_aps_history",
                 "_cached_aps_history_summary", "_cached_aps_comparison_options"):
        stub = MagicMock()
        stub.clear = (lambda n=name: cleared.append(n))
        monkeypatch.setattr(page, name, stub)
    page._flush_aps_caches()
    assert sorted(cleared) == sorted([
        "_cached_aps_comparison_options", "_cached_aps_history",
        "_cached_aps_history_summary", "_cached_persisted_aps_plan"])


def test_a_delete_flushes_the_same_caches_as_a_build(caps, monkeypatch):
    """A deleted cycle must stop being offered in Step 3 straight away."""
    flushed = []
    monkeypatch.setattr(page, "_flush_aps_caches", lambda: flushed.append(1))
    monkeypatch.setattr(page, "delete_history_slice", lambda *a, **k: (5, 10))
    _ST.checkbox = lambda *a, **k: True
    _ST.button = lambda *a, **k: True
    page._render_aps_step4_delete()
    assert flushed == [1]


# ── The 47 MB download ──────────────────────────────────────────────────────

def test_the_download_goes_through_the_bytes_cache(caps, monkeypatch):
    """``to_csv`` on the live 382,738-row plan costs 47 MB and 1.17s a time.

    Inline in the download button, that ran on EVERY rerun of the fragment.
    The stub's ``cache_data`` is a pass-through, so this asserts the *wiring* —
    that the bytes come from the cached helper, keyed on a shape signature —
    rather than re-testing Streamlit's own caching.
    """
    seen = []

    def _fake(sig, frame):
        seen.append(sig)
        return b"csv-bytes"

    monkeypatch.setattr(page, "_cached_csv_bytes", _fake)
    frame = pd.DataFrame({"a": [1, 2, 3]})
    page._render_aps_download_preview(frame, key_prefix="t")

    assert seen == [(3, 1)], "the CSV must be keyed on the frame's shape"
    assert caps["downloads"][0][1] == b"csv-bytes"


def test_no_inline_to_csv_survives_in_the_aps_download():
    """Static backstop: the inline serialisation must not creep back."""
    import inspect
    src = inspect.getsource(page._render_aps_download_preview)
    assert "_cached_csv_bytes" in src
    assert ".to_csv(" not in src, "inline to_csv re-serialises on every rerun"
