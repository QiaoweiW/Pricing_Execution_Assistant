"""Headless render tests for the four-step RO Comparison flow.

Streamlit is stubbed (no browser / server) so the page's render functions can
be called directly.  What these tests protect:

* The **gate**: ▶️ Run RO_Seed is disabled while the upload has a blocking
  problem, and enabled once it is clean.  This is the whole point of the
  pre-flight — the pipeline downstream coerces bad cells to NaN, so a file
  that gets past this gate publishes wrong numbers silently.
* The **removals**: no Save buttons, no post-run Diagnostic, no
  "Regenerate from published" panel.  Auto-save covers all of it.
* The **structure**: four numbered steps, Pipeline at a Glance collapsed.

Context managers use a real class so an exception inside ``with expander:``
propagates instead of being swallowed by a MagicMock ``__exit__``.
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

from data_sources import ro_input_preflight as rpf  # noqa: E402
import pages.demand_planner_analytics_view as page  # noqa: E402


_HEADER = (
    "Month,Format,Customer,Taxonomy,Brand,Item #,Item Desc,Probability,"
    "First Ship Date,Lbs./yr,PC$/yr,Slotting\n"
)
_ROW = (
    "2026-06-01,HTST,Walmart,Retail,DG,340021,Milk Gallon,0.5,"
    "2027-01-01,1000000,50000,0\n"
)


class _Upload:
    """Minimal stand-in for Streamlit's UploadedFile."""

    def __init__(self, data: bytes, name: str = "Distribution_Tracker.csv"):
        self._data = data
        self.name = name
        self.size = len(data)

    def getvalue(self) -> bytes:
        return self._data


@pytest.fixture
def caps(monkeypatch):
    """Reset the streamlit stub per test and capture what got rendered.

    Configures ``page.st`` rather than this module's own ``_ST``: several test
    modules install their own ``sys.modules["streamlit"]`` stub at import time,
    and only the first one wins for a page module that is already imported.
    Binding to the object the page actually calls makes these tests independent
    of collection order.
    """
    c = {"buttons": [], "expanders": [], "markdown": [], "captions": [],
         "download": [], "checkbox": [], "error": [], "warning": [],
         "success": [], "info": []}
    _ST = page.st
    _ST.session_state = {}
    _ST.fragment = lambda f: f
    _ST.columns = lambda spec, **k: [
        _Ctx() for _ in (spec if isinstance(spec, (list, tuple)) else range(spec))
    ]

    def _expander(label="", **k):
        c["expanders"].append((label, k.get("expanded")))
        return _Ctx()

    def _button(label="", **k):
        c["buttons"].append((label, k.get("disabled", False)))
        return False

    def _download(label="", **k):
        c["download"].append(label)
        return False

    def _checkbox(label="", **k):
        c["checkbox"].append(label)
        return False

    _ST.expander = _expander
    _ST.button = _button
    _ST.download_button = _download
    _ST.checkbox = _checkbox
    _ST.markdown = lambda body="", **k: c["markdown"].append(str(body))
    _ST.caption = lambda body="", **k: c["captions"].append(str(body))
    _ST.error = lambda body="", **k: c["error"].append(str(body))
    _ST.warning = lambda body="", **k: c["warning"].append(str(body))
    _ST.success = lambda body="", **k: c["success"].append(str(body))
    _ST.info = lambda body="", **k: c["info"].append(str(body))
    _ST.dataframe = lambda *a, **k: None
    _ST.date_input = lambda *a, **k: k.get("value")
    _ST.file_uploader = lambda *a, **k: c.get("_upload")
    _ST.spinner = lambda *a, **k: _Ctx()
    monkeypatch.setattr(page.fabric_signin_widget, "is_fabric_signed_in",
                        lambda: False)
    return c


def _run_step1(caps, upload, item_master=None):
    caps["_upload"] = upload
    page.st.file_uploader = lambda *a, **k: upload
    page._render_ro_step1_input(item_master, None)
    return caps


def _master(*items) -> pd.DataFrame:
    """A fully classified RO_Item_Master — the "nothing to report" case."""
    n = len(items)
    return pd.DataFrame({
        "Item #": list(items),
        "Item Desc": ["x"] * n,
        "Portfolio Major": ["HTST"] * n,
        "Portfolio Minor": ["Gallon Jug"] * n,
        "Brand Category": ["Branded"] * n,
    })


def _run_button(caps):
    hits = [b for b in caps["buttons"] if "Run RO_Seed" in b[0]]
    assert hits, f"no Run RO_Seed button rendered; got {caps['buttons']}"
    return hits[0]


# ── The gate ─────────────────────────────────────────────────────────────────

def test_run_disabled_with_no_upload(caps):
    _run_step1(caps, None)
    assert _run_button(caps)[1] is True


def test_run_enabled_for_a_clean_file(caps):
    _run_step1(caps, _Upload((_HEADER + _ROW).encode()), _master(340021))
    label, disabled = _run_button(caps)
    assert disabled is False, "a clean file must enable the run"
    assert any("Checked and ready" in s for s in caps["success"])


def test_run_disabled_when_the_month_column_is_missing(caps):
    header = _HEADER.replace("Month,", "")
    row = _ROW.replace("2026-06-01,", "")
    _run_step1(caps, _Upload((header + row).encode()), _master(340021))
    assert _run_button(caps)[1] is True
    assert any("must be fixed" in e for e in caps["error"])


def test_run_disabled_when_a_numeric_cell_holds_an_excel_error(caps):
    """The silent-zero case — the single most important thing the gate stops."""
    row = _ROW.replace(",1000000,", ",#N/A,")
    _run_step1(caps, _Upload((_HEADER + row).encode()), _master(340021))
    assert _run_button(caps)[1] is True


def test_unlinked_item_leaves_run_disabled_until_acknowledged(caps):
    """Structurally fine, so it is offered — but only behind an explicit tick."""
    _run_step1(caps, _Upload((_HEADER + _ROW).encode()), _master(999999))
    # The stubbed checkbox returns False → not acknowledged → still disabled.
    assert _run_button(caps)[1] is True
    assert any("run anyway" in cb for cb in caps["checkbox"])
    assert any("need your attention" in w for w in caps["warning"])


def test_acknowledging_an_unlinked_item_enables_the_run(caps):
    page.st.checkbox = lambda label="", **k: True
    _run_step1(caps, _Upload((_HEADER + _ROW).encode()), _master(999999))
    assert _run_button(caps)[1] is False


# ── Step 1 guidance ──────────────────────────────────────────────────────────

def test_step1_points_at_the_reference_file_and_offers_it_as_a_template(caps):
    _run_step1(caps, None)
    assert any("template" in d.lower() for d in caps["download"])
    rendered = " ".join(caps["markdown"] + caps["captions"])
    # Named, linked, and framed as the thing to compare against.
    assert "Distribution_Tracker_20260831_164436.csv" in rendered
    assert "Append_New_History%2FArchive" in rendered
    assert "should look like the table below" in rendered


def test_step1_names_only_the_failure_causing_checks(caps):
    """Guidance must not read as "audit every column"."""
    _run_step1(caps, None)
    rendered = " ".join(caps["info"])
    assert "do not need to audit every column" in rendered
    for cause in ("Month", "Lbs./yr", "Probability", "RO_Item_Master.csv"):
        assert cause in rendered, cause


def test_step1_offers_the_item_master_download(caps, monkeypatch):
    """Point 4 of the checks is the only Fabric fix — the file sits beside it."""
    calls = []
    monkeypatch.setattr(page, "_render_ro_item_master_download_button",
                        lambda **k: calls.append(k))
    _run_step1(caps, None)
    assert calls, "Step 1 must offer RO_Item_Master.csv"
    # A distinct widget key, or it collides with the same button in Step 4c.
    assert calls[0].get("key_suffix"), calls[0]


def test_reconcile_is_silent_when_the_files_agree(caps, monkeypatch):
    """A clean background check must render nothing at all."""
    class _Aligned:
        is_aligned = True

    monkeypatch.setattr(page.fabric_signin_widget, "is_fabric_signed_in",
                        lambda: True)
    monkeypatch.setattr(page, "_run_ro_seed_summary_reconcile", lambda: _Aligned())
    page.st.session_state.clear()
    page._render_ro_reconcile_autocheck()
    assert caps["warning"] == [] and caps["success"] == [] and caps["error"] == []
    assert caps["expanders"] == []


def test_reconcile_speaks_up_when_the_files_diverge(caps, monkeypatch):
    class _Diverged:
        is_aligned = False

    shown = []
    monkeypatch.setattr(page.fabric_signin_widget, "is_fabric_signed_in",
                        lambda: True)
    monkeypatch.setattr(page, "_run_ro_seed_summary_reconcile", lambda: _Diverged())
    monkeypatch.setattr(page, "_render_ro_seed_summary_reconcile_result",
                        lambda r: shown.append(r))
    page.st.session_state.clear()
    page._render_ro_reconcile_autocheck()
    assert len(shown) == 1


def test_reconcile_stays_silent_when_signed_out(caps, monkeypatch):
    """No Fabric session, no reads — and no scary banner either."""
    calls = []
    monkeypatch.setattr(page, "_run_ro_seed_summary_reconcile",
                        lambda: calls.append(1))
    page.st.session_state.clear()
    page._render_ro_reconcile_autocheck()
    assert calls == []
    assert caps["warning"] == [] and caps["error"] == []


def test_reconcile_runs_once_per_session(caps, monkeypatch):
    """Two Fabric reads — it must not fire on every rerun."""
    calls = []

    class _Aligned:
        is_aligned = True

    monkeypatch.setattr(page.fabric_signin_widget, "is_fabric_signed_in",
                        lambda: True)
    monkeypatch.setattr(page, "_run_ro_seed_summary_reconcile",
                        lambda: (calls.append(1), _Aligned())[1])
    page.st.session_state.clear()
    page._render_ro_reconcile_autocheck()
    page._render_ro_reconcile_autocheck()
    assert len(calls) == 1


def test_step1_folds_in_the_excel_backtestable_method(caps):
    _run_step1(caps, None)
    labels = " ".join(lbl for lbl, _ in caps["expanders"])
    assert "Run RO_Seed" in labels and "Excel" in labels


def test_the_method_doc_carries_the_real_formulas():
    """Guards against the doc drifting away from ro_seed_pipeline's maths."""
    md = page._RO_SEED_METHOD_MD
    for fragment in (
        "Probability * Lbs./yr",
        "Days in Year / 365",
        "EOMONTH(First Ship Date, 0) + 1",
        "MIN(365, MAX(0,",
    ):
        assert fragment in md, fragment


def test_step1_opens_itself_when_nothing_has_run(caps):
    _run_step1(caps, None)
    step1 = [e for e in caps["expanders"] if "Step 1" in e[0]]
    assert step1 and step1[0][1] is True


# ── Fix guidance is actionable, not just descriptive ─────────────────────────

def test_a_spreadsheet_problem_names_the_row_and_the_fix(caps):
    row = _ROW.replace(",1000000,", ",#REF!,")
    _run_step1(caps, _Upload((_HEADER + row).encode()), _master(340021))
    # The "where do I fix this?" verdict rides on the expander label.
    labels = " ".join(lbl for lbl, _ in caps["expanders"]).lower()
    assert "spreadsheet" in labels
    assert any("fix list" in d.lower() for d in caps["download"])
    # And the finding itself must say what it costs if ignored.
    assert any("zero volume" in e for e in caps["error"] + caps["markdown"])


def test_a_fabric_problem_links_to_the_file(caps):
    _run_step1(caps, _Upload((_HEADER + _ROW).encode()), _master(999999))
    rendered = " ".join(caps["markdown"])
    assert "Fabric" in rendered
    assert "RO_Item_Master.csv" in rendered


# ── Step 4 ───────────────────────────────────────────────────────────────────

def test_step4_explains_delete_then_reupload(caps, monkeypatch):
    for fn in ("_render_month_cleanup", "_render_ro_rules_panel",
               "_render_ro_seed_download_button",
               "_render_ro_item_master_download_button"):
        monkeypatch.setattr(page, fn, lambda *a, **k: None)
    page._render_ro_step4_rerun()
    rendered = " ".join(caps["info"] + caps["markdown"])
    assert "delete" in rendered.lower()
    assert "Step 1" in rendered
    step4 = [e for e in caps["expanders"] if "Step 4" in e[0]]
    assert step4 and step4[0][1] is False, "Step 4 must start collapsed"


# ── The removals stay removed ────────────────────────────────────────────────

def test_step4c_holds_only_the_two_reference_downloads(caps, monkeypatch):
    """4c is a download shelf now — no regenerate, no reconcile."""
    for fn in ("_render_month_cleanup", "_render_ro_rules_panel"):
        monkeypatch.setattr(page, fn, lambda *a, **k: None)
    monkeypatch.setattr(page.fabric_signin_widget, "is_fabric_signed_in",
                        lambda: False)
    page._render_ro_step4_rerun()
    rendered = " ".join(caps["markdown"])
    assert "Download the reference files" in rendered
    assert "Generate" not in rendered
    assert not any("Reconcile" in b[0] for b in caps["buttons"])


def test_both_reference_downloads_are_primary_styled():
    """Red, per the design: these are the two files a planner actually takes."""
    src = open(page.__file__, encoding="utf-8").read()
    for key in ("ro_cmp_dl_ro_seed", "ro_cmp_dl_item_master"):
        i = src.index(key)
        window = src[i - 400:i + 400]
        assert 'type="primary"' in window, key
    # The old marker-plus-CSS red hack is gone.
    assert "ro-item-master-dl-marker" not in src


def test_no_save_buttons_or_removed_panels_remain():
    src = open(page.__file__, encoding="utf-8").read()
    for gone in (
        "Save RO_Summary_Report.csv",
        "Save `RO_Comparison_Output.csv` to Fabric",
        "Regenerate from published RO_Comparison_Output.csv",
        "_render_summary_report_diagnostic",
        "_render_ro_comparison_save_button",
        "_render_ro_regen_from_published",
        "_render_ro_comparison_generate_button",
        "_render_warnings_banner",
        "_render_post_upload_guidance",
        "_render_ro_pipeline_review_archive",
        "_render_ro_seed_summary_reconcile_button",
        "please review and fix before saving",
        "Archived review snapshots",
        "Regenerate RO_Seed with current rules",
    ):
        assert gone not in src, f"{gone} should have been removed"


def test_summary_report_offers_download_and_says_saving_is_automatic(caps):
    page._render_summary_report_actions(
        pd.DataFrame({"Millions of lbs.": ["Total B2C"], "Prior Plan": [23.9]})
    )
    assert any("Download RO Summary Report" in d for d in caps["download"])
    assert not any("Save" in b[0] for b in caps["buttons"])
    assert any("automatic" in c.lower() for c in caps["captions"])


def test_pipeline_at_a_glance_is_collapsed_and_the_flow_is_four_steps():
    src = open(page.__file__, encoding="utf-8").read()
    for step in ("Step 1 · Upload", "Step 2 · RO Output",
                 "Step 3 · Drivers", "Step 4 · Re-upload"):
        assert step in src, step
    # Pipeline at a Glance renders last, after Step 4.
    assert (src.index("_render_ro_step4_rerun()")
            < src.index("_render_ro_pipeline_analytics_section()"))


def test_rule_changes_point_at_the_step_1_upload():
    """The regenerate button is gone, so the copy must name its replacement."""
    src = open(page.__file__, encoding="utf-8").read()
    assert "rebuild_ro_seed_from_published_history" not in src
    assert "take effect the next time you upload in Step 1" in src or            "effect the next time" in src
