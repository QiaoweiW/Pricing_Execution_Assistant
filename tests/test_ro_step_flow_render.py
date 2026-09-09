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
    """Stands in for a column / container.

    Carries the handful of element methods the page calls *on* a column
    (rather than on ``st``), so a real exception inside the block still
    propagates instead of being swallowed by a MagicMock.
    """

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def metric(self, *a, **k):
        return None

    def markdown(self, *a, **k):
        return None

    def caption(self, *a, **k):
        return None


_ST = MagicMock()
_ST.session_state = {}
_ST.fragment = lambda f: f
# Pass-through caching decorators.  They are applied at IMPORT time, so a
# MagicMock here would replace the decorated function itself — the page's
# cached helpers would return a Mock instead of their value.
_ST.cache_data = lambda *a, **k: (lambda fn: fn)
_ST.cache_resource = lambda *a, **k: (lambda fn: fn)
_ST.columns = lambda spec, **k: [
    _Ctx() for _ in (spec if isinstance(spec, (list, tuple)) else range(spec))
]
_ST.expander = lambda *a, **k: _Ctx()
_ST.container = lambda *a, **k: _Ctx()
_ST.popover = lambda *a, **k: _Ctx()
sys.modules["streamlit"] = _ST
sys.modules.setdefault("streamlit.components", MagicMock())
sys.modules.setdefault("streamlit.components.v1", MagicMock())

from data_sources import ro_input_preflight as rpf
from data_sources.demand_plan_comparison import build_item_dim_frame_cascade  # noqa: E402
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
    # Step 1 derives the PDH -> RO_Item_Master cascade lazily (it costs a
    # Fabric read), so serve whatever the test injected instead.
    monkeypatch.setattr(page, "_load_ro_item_dims",
                        lambda _master=None: c.get("_item_dims"))
    return c


def _run_step1(caps, upload, item_master=None):
    caps["_upload"] = upload
    caps["_item_dims"] = item_master
    page.st.file_uploader = lambda *a, **k: upload
    page._render_ro_step1_input(item_master, None)
    return caps


def _master(*items) -> pd.DataFrame:
    """A fully classified dim cascade — the "nothing to report" case."""
    n = len(items)
    return build_item_dim_frame_cascade(None, pd.DataFrame({
        "Item #": list(items),
        "Item Desc": ["x"] * n,
        "Portfolio Major": ["HTST"] * n,
        "Portfolio Minor": ["Gallon Jug"] * n,
        "Brand Category": ["Branded"] * n,
    }))


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
    assert any("go ahead anyway" in cb for cb in caps["checkbox"])
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
    # Assert against the constant, not a literal: the reference file gets
    # rotated, and a hard-coded name turns that into a test failure.
    assert page._RO_INPUT_EXAMPLE_NAME in rendered
    assert page._RO_INPUT_EXAMPLE_URL in rendered
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


# ── Step 1 · replacing RO_Item_Master in place ───────────────────────────────

def test_step1_offers_an_item_master_upload(caps, monkeypatch):
    """Both halves of the item fix — get the file, put it back — live together."""
    calls = []
    monkeypatch.setattr(page, "_render_ro_item_master_uploader",
                        lambda: calls.append(1))
    _run_step1(caps, None)
    assert calls, "Step 1 must offer a way to upload a corrected item master"


def test_the_item_master_uploader_is_inert_when_signed_out(caps, monkeypatch):
    """No Fabric session → no read, no write, and no scary banner."""
    monkeypatch.setattr(page.fabric_signin_widget, "is_fabric_signed_in",
                        lambda: False)
    page._render_ro_item_master_uploader()
    assert not any("Replace" in b[0] for b in caps["buttons"])
    assert caps["error"] == []


def test_the_item_master_guidance_warns_about_replacing_the_whole_list(caps,
                                                                      monkeypatch):
    monkeypatch.setattr(page.fabric_signin_widget, "is_fabric_signed_in",
                        lambda: True)
    page.st.file_uploader = lambda *a, **k: None
    page._render_ro_item_master_uploader()
    guidance = " ".join(caps["info"] + caps["markdown"])
    assert "replaces the whole list" in guidance
    assert "CSV UTF-8" in guidance
    # The three column names a planner must not rename.
    for col in ("Item #", "Portfolio Major", "Portfolio Minor"):
        assert col in guidance, col
    assert "archive" in guidance.lower()


# ── Demand Summary · the four-step flow ──────────────────────────────────────

def test_demand_summary_is_four_numbered_steps_in_order():
    src = open(page.__file__, encoding="utf-8").read()
    steps = [
        "**Step 1 · Upload a new Base Plan**",
        "**Step 2 · Download the plan files**",
        "**Step 3 · Compare this plan with the last one**",
        "**Step 4 · Withdraw a Base Plan upload**",
    ]
    positions = []
    for step in steps:
        assert step in src, step
        positions.append(src.index(step))
    assert positions == sorted(positions), "the steps must render in order"


def test_the_destructive_withdraw_comes_after_the_uploader():
    """It used to render FIRST — before the uploader it undoes."""
    src = open(page.__file__, encoding="utf-8").read()
    assert (src.index("_render_base_plan_uploader()")
            < src.index("_render_withdraw_cycle_tool()"))


def test_the_refresh_from_fabric_button_is_gone():
    src = open(page.__file__, encoding="utf-8").read()
    assert "demand_summary_refresh_from_fabric" not in src


def test_reconciliation_runs_automatically_not_behind_a_button():
    src = open(page.__file__, encoding="utf-8").read()
    assert "_render_demand_reconciliation_autocheck()" in src
    assert "▶️ Run reconciliation" not in src
    assert "demand_reconcile_run" not in src
    # It must still be re-runnable after a fix.
    assert "demand_reconcile_recheck" in src


def test_a_clean_reconciliation_reads_as_safe_to_proceed(caps, monkeypatch):
    class _Bridge:
        published_lbs = 92_200_000.0
        drift_lbs = 0.0
        ties = True
        dropped_detail = pd.DataFrame()

    monkeypatch.setattr(page, "_build_reconciliation", lambda: {"bridge": _Bridge()})
    # The verdict is what this test is about; the waterfall it tucks into
    # "Show me the working" has its own coverage.
    monkeypatch.setattr(page, "_render_reconciliation_result", lambda p: None)
    page.st.session_state.clear()
    page._render_demand_reconciliation_autocheck()
    assert any("the numbers add up" in s for s in caps["success"])
    assert caps["warning"] == []


def test_a_broken_reconciliation_tells_the_planner_to_act(caps, monkeypatch):
    class _Bridge:
        published_lbs = 92_200_000.0
        drift_lbs = -2_000_000.0
        ties = False
        dropped_detail = pd.DataFrame()

    shown = []
    monkeypatch.setattr(page, "_build_reconciliation", lambda: {"bridge": _Bridge()})
    monkeypatch.setattr(page, "_render_reconciliation_result", lambda p: shown.append(p))
    page.st.session_state.clear()
    page._render_demand_reconciliation_autocheck()
    assert any("needs a look" in w for w in caps["warning"])
    assert len(shown) == 1


def test_the_bridge_runs_once_per_session(caps, monkeypatch):
    """It rebuilds the whole plan and reads six files — never on a plain rerun."""
    calls = []

    class _Bridge:
        published_lbs = 1.0
        drift_lbs = 0.0
        ties = True
        dropped_detail = pd.DataFrame()

    monkeypatch.setattr(page, "_build_reconciliation",
                        lambda: (calls.append(1), {"bridge": _Bridge()})[1])
    monkeypatch.setattr(page, "_render_reconciliation_result", lambda p: None)
    page.st.session_state.clear()
    page._render_demand_reconciliation_autocheck()
    page._render_demand_reconciliation_autocheck()
    assert len(calls) == 1


def test_an_input_error_is_surfaced_not_swallowed(caps, monkeypatch):
    monkeypatch.setattr(page, "_build_reconciliation",
                        lambda: {"error": "Could not read the inputs."})
    shown = []
    monkeypatch.setattr(page, "_render_reconciliation_result", lambda p: shown.append(p))
    page.st.session_state.clear()
    page._render_demand_reconciliation_autocheck()
    assert len(shown) == 1
    assert caps["success"] == []


# ── The reference example is the real file, not an invented one ──────────────

def test_the_example_is_the_real_archived_tracker():
    """Copied verbatim from the lakehouse: header + first three rows.

    An invented example is worse than none — a planner matches her export
    against it column-for-column, so a made-up shape sends her to "fix" a file
    that was already correct.
    """
    df, raw = page._ro_input_example()
    assert df is not None, "the example CSV must ship with the repo"
    assert len(df) == 3, "three rows, as the guidance says"
    # The real tracker's own spellings — NOT the pipeline's internal names.
    for col in ("Sales Manager", "Customer", "Last Updated", "Add / Delete",
                "Taxonomy", "Item Desc", "Item #", "Pipeline Status",
                "Reflected in APS", "Anticipated Annual Lbs. Vol",
                "Annual PC $", "Total Anticipated Slotting Costs", "Month"):
        assert col in df.columns, col
    assert raw, "the template download needs the bytes"


def test_the_example_carries_every_column_the_checks_need():
    """Whatever the validator can block on must be visible in the example."""
    from data_sources import ro_input_preflight as pf

    df, _ = page._ro_input_example()
    renamed = df.rename(columns=pf.HEADER_ALIASES)
    for col in pf.CRITICAL_COLUMNS:
        assert col in renamed.columns, col
    assert pf.MONTH_COLUMN in renamed.columns


def test_the_template_is_byte_identical_to_the_example():
    """One file serves both, so the table and the download cannot drift."""
    df, raw = page._ro_input_example()
    import io as _io
    import pandas as _pd
    assert _pd.read_csv(_io.BytesIO(raw), dtype=str,
                        keep_default_na=False).equals(df)


def test_the_template_passes_its_own_validator():
    """A template that the gate would reject is a trap."""
    from data_sources import ro_input_preflight as pf

    _df, raw = page._ro_input_example()
    assert pf.check_distribution_tracker(raw).ok_to_run


def test_a_missing_example_file_does_not_break_step_1(caps, monkeypatch):
    """The guidance stands on its own; the table is a bonus."""
    monkeypatch.setattr(page, "_ro_input_example", lambda: (None, b""))
    _run_step1(caps, None)
    rendered = " ".join(caps["markdown"] + caps["captions"])
    assert page._RO_INPUT_EXAMPLE_URL in rendered
    assert not any("template" in d.lower() for d in caps["download"])


# ── Dropped SKUs are a decision, not an error ────────────────────────────────

def _actionable(*items) -> pd.DataFrame:
    from data_sources.demand_plan_reconcile import (
        COL_ACTION, COL_DESC, COL_FORECAST, COL_GATE, COL_ITEM, COL_LBS,
        COL_LINK, COL_ROWS,
    )
    return pd.DataFrame([{
        COL_ITEM: i, COL_DESC: f"desc {i}", COL_FORECAST: "Base Plan",
        COL_GATE: "Unclassified — not in PDH", COL_ROWS: 2,
        COL_LBS: 792_000.0, COL_ACTION: "Classify it in RO_Item_Master.csv.",
        COL_LINK: "",
    } for i in items])


def _radio(caps, answer):
    """Stub st.radio to return *answer*, recording the options offered."""
    def _r(label="", options=(), **k):
        caps["radio"] = list(options)
        return answer
    page.st.radio = _r


def test_nothing_dropped_reads_as_nothing_to_decide(caps):
    page._render_dropped_sku_decision(pd.DataFrame())
    assert any("Nothing to decide" in s for s in caps["success"])


def test_the_planner_is_asked_to_decide_not_told_to_fix(caps):
    _radio(caps, None)
    page._render_dropped_sku_decision(_actionable("830109", "830108"))
    heading = " ".join(caps["markdown"])
    assert "your call" in heading
    assert caps["radio"] == [page._DROP_APPROVE, page._DROP_REJECT]
    # Undecided is not an error state.
    assert any("not an error" in i for i in caps["info"])
    assert caps["error"] == []


def test_approving_closes_it_out_with_nothing_further_to_do(caps):
    _radio(caps, page._DROP_APPROVE)
    page._render_dropped_sku_decision(_actionable("830109"))
    assert any("nothing more to do" in s.lower() for s in caps["success"])
    assert caps["warning"] == []


def test_declining_names_both_files_and_offers_the_rows(caps):
    _radio(caps, page._DROP_REJECT)
    page._render_dropped_sku_decision(_actionable("830109", "830108"))
    assert any("BOTH files" in w for w in caps["warning"])
    rendered = " ".join(caps["markdown"])
    assert "qry_mgmt_plan_full.csv" in rendered
    assert "qry_total_item_level_demand.csv" in rendered
    assert any("add back" in d.lower() for d in caps["download"])


def test_declining_warns_that_one_file_alone_desynchronises_them(caps):
    _radio(caps, page._DROP_REJECT)
    page._render_dropped_sku_decision(_actionable("830109"))
    assert any("only one file" in w for w in caps["warning"])


def test_both_management_files_are_linked_into_fabric():
    for _label, path in page._MGMT_FILES:
        url = page._lakehouse_file_url(path)
        assert url.startswith("https://app.fabric.microsoft.com/")
        assert "%20" in url and " " not in url.split("selectedPath=")[-1]


def test_the_source_fix_is_still_reachable_after_declining(caps):
    """Adding rows by hand fixes this cycle; the source fix stops it recurring."""
    _radio(caps, page._DROP_REJECT)
    page._render_dropped_sku_decision(_actionable("830109"))
    labels = " ".join(lbl for lbl, _ in caps["expanders"])
    assert "come in on their own next time" in labels


# ── Step 2 reads as three numbered tasks ─────────────────────────────────────


def _bridge(published=None, rebuilt=None, ties=True, drops=None):
    """A DemandPlanBridge stand-in — only the fields the renderer reads."""
    class _B:
        drift_lbs = None if published is None else (published - rebuilt)
        published_lbs = published
        output_lbs = rebuilt
        dropped_detail = pd.DataFrame() if drops is None else drops
    _B.ties = ties
    return _B()


def _ro(delta=0.0, detail=None):
    class _R:
        ro_summary_lbs = 1_000_000.0
        plan_lbs = 1_000_000.0 - delta
        delta_lbs = delta
    _R.detail = pd.DataFrame() if detail is None else detail
    return _R()


def _payload(**over):
    base = {"bridge": _bridge(2_576_480_000.0, 2_576_080_000.0, ties=False),
            "ro_bridge": _ro(), "ro_available": True}
    base.update(over)
    return base


def test_step_2_renders_exactly_three_numbered_tasks(caps, monkeypatch):
    monkeypatch.setattr(page, "_render_dropped_sku_decision", lambda a: None)
    monkeypatch.setattr(page, "_render_ro_item_detail", lambda r: None)
    page.st.session_state.clear()
    page._render_reconciliation_result(_payload())
    headings = [m for m in caps["markdown"] if m.startswith("##### ")]
    assert len(headings) == 3, headings
    assert headings[0].startswith("##### 1 · SKUs the plan left out")
    assert headings[1].startswith("##### 2 · The full drop list")
    assert headings[2].startswith("##### 3 · Are these files current?")


def test_the_decision_comes_first(caps, monkeypatch):
    """The planner's call leads; everything else is downstream of it."""
    order = []
    monkeypatch.setattr(page, "_render_dropped_sku_decision",
                        lambda a: order.append("decision"))
    monkeypatch.setattr(page, "_render_drop_list", lambda d: order.append("drops"))
    monkeypatch.setattr(page, "_render_plan_freshness",
                        lambda *a, **k: order.append("freshness"))
    page.st.session_state.clear()
    page._render_reconciliation_result(_payload())
    assert order == ["decision", "drops", "freshness"]


def test_the_waterfall_and_dropped_by_design_are_gone():
    src = open(page.__file__, encoding="utf-8").read()
    assert "Input — Base Plan + R&O" not in src
    assert "Dropped by design" not in src
    assert "= Demand plan" not in src


def test_the_headline_names_which_tasks_need_the_planner(caps, monkeypatch):
    monkeypatch.setattr(page, "_render_dropped_sku_decision", lambda a: None)
    monkeypatch.setattr(page, "_render_ro_item_detail", lambda r: None)
    page.st.session_state.clear()
    page._render_reconciliation_result(
        _payload(bridge=_bridge(100.0, 90.0, ties=False, drops=_actionable("1")))
    )
    warn = " ".join(caps["warning"])
    assert "2 of the 3 checks below need you" in warn
    assert "a decision" in warn and "out of date" in warn


def test_all_clear_says_nothing_needs_doing(caps, monkeypatch):
    monkeypatch.setattr(page, "_render_dropped_sku_decision", lambda a: None)
    monkeypatch.setattr(page, "_render_ro_item_detail", lambda r: None)
    page.st.session_state.clear()
    page._render_reconciliation_result(
        _payload(bridge=_bridge(100.0, 100.0, ties=True))
    )
    assert any("Nothing needs doing" in s for s in caps["success"])


# ── Task 2 · the drop list is a download, not a wall of rows ─────────────────

def test_the_drop_list_is_offered_as_a_download_only(caps):
    from data_sources.demand_plan_reconcile import COL_ITEM, COL_GATE, COL_ACTION
    detail = pd.DataFrame({COL_ITEM: ["1", "2"], COL_GATE: ["Not B2C"] * 2,
                           COL_ACTION: ["", ""]})
    page._render_drop_list(detail)
    assert any("Full SKU-level drop list" in d for d in caps["download"])
    # No table: the by-design drops are the bulk and are not actionable.
    assert any("audit" in c for c in caps["captions"])


def test_no_drops_means_no_list(caps):
    page._render_drop_list(pd.DataFrame())
    assert caps["download"] == []


# ── Task 3 · the regenerate instruction ──────────────────────────────────────

def test_a_stale_plan_says_regenerate_and_names_the_one_action(caps):
    page._render_plan_freshness(
        _bridge(2_576_480_000.0, 2_576_080_000.0, ties=False), _ro(), False)
    assert any("out of date" in e for e in caps["error"])
    said = " ".join(caps["markdown"])
    assert "Step 1 · Upload a new Base Plan" in said
    assert "RO_Seed.csv" in said
    # Both outputs are named, so nobody regenerates one and forgets the other.
    assert "qry_mgmt_plan_full.csv" in said
    assert "qry_total_item_level_demand.csv" in said
    # And the wrong fix is ruled out explicitly.
    assert "not** edit the `qry` files by hand" in said


def test_a_current_plan_says_so_without_instructions(caps):
    page._render_plan_freshness(_bridge(100.0, 100.0, ties=True), _ro(), False)
    assert any("Up to date" in s for s in caps["success"])
    assert not any("Step 1" in m for m in caps["markdown"])


def test_nothing_published_yet_is_not_an_error(caps):
    page._render_plan_freshness(_bridge(None, 100.0, ties=True), _ro(), False)
    assert caps["error"] == []
    assert any("Nothing is published yet" in i for i in caps["info"])


def test_the_ro_section_shows_the_item_detail_directly(caps, monkeypatch):
    """No fold, no cause table — the item list is what the section is for."""
    from data_sources.demand_plan_reconcile import (
        COL_DELTA, COL_ITEM, COL_PLAN_LBS, COL_RO_SUMMARY_LBS, COL_STATUS,
    )
    shown = []
    monkeypatch.setattr(page, "_render_ro_item_detail", lambda r: shown.append(r))
    detail = pd.DataFrame([{
        COL_ITEM: "370089", COL_RO_SUMMARY_LBS: 249_736.0, COL_PLAN_LBS: 301_007.0,
        COL_DELTA: -51_271.0, COL_STATUS: "Both, but the plan carries more",
    }])
    page._render_plan_freshness(_bridge(100.0, 100.0, ties=True),
                                _ro(-51_271.0, detail), True, pd.DataFrame())
    assert len(shown) == 1, "the item-by-item detail must render, unfolded"
    assert not any("Item-by-item detail" in lbl for lbl, _ in caps["expanders"])


def test_the_ro_gap_is_explained_in_one_line(caps, monkeypatch):
    """The root-cause headline survives as a caption, not a second table."""
    from data_sources.demand_plan_reconcile import (
        COL_DELTA, COL_ITEM, COL_PLAN_LBS, COL_RO_SUMMARY_LBS, COL_STATUS,
    )
    monkeypatch.setattr(page, "_render_ro_item_detail", lambda r: None)
    detail = pd.DataFrame([{
        COL_ITEM: "830109", COL_RO_SUMMARY_LBS: 792_000.0, COL_PLAN_LBS: 0.0,
        COL_DELTA: 792_000.0, COL_STATUS: "In RO Summary, absent from the plan",
    }])
    page._render_plan_freshness(_bridge(100.0, 100.0, ties=True),
                                _ro(792_000.0, detail), True, pd.DataFrame())
    caption = " ".join(caps["captions"])
    assert "gap is" in caption
    assert "older RO_Seed" in caption


def test_a_missing_ro_summary_is_a_caption_not_a_scare(caps):
    page._render_plan_freshness(_bridge(100.0, 100.0, ties=True), _ro(), False)
    assert caps["warning"] == []
    assert any("RO_Comparison_Output.csv" in c for c in caps["captions"])
