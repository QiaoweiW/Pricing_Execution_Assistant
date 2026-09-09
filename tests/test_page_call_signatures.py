"""Every data-source call in the page must match the function it calls.

A live TypeError motivated this file.  ``build_business_health`` lost two
parameters when Business Health became orders-only; two of its three call
sites were updated and the third — ``_cached_comparison_order_yoy``, far away
in the Demand Plan Comparison section — was not.  Nothing caught it, because
that path only runs when a planner opens Step 3, and it failed there with a
redacted error on Streamlit Cloud::

    res = build_business_health(enriched, None, prior_month)
    TypeError: takes 2 positional arguments but 3 were given

Static analysis catches this class in a second, for every call site at once,
without needing a fixture per code path.  Pyflakes does not — an arity
mismatch across modules is outside its remit.

Imports nothing at module scope, deliberately
---------------------------------------------
The page can only be imported once per session, and the first test module to
do it decides what ``page.st`` is for every module after it.  Two render-test
modules each install their own Streamlit stub, so a third importer that
arrives first (this file sorts before both) silently breaks their fixtures.
So the AST work reads the file by path, and the page is imported lazily inside
the tests — by which point collection has finished and the render modules have
already installed their stub.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


_PAGE_PATH: Path = (
    Path(__file__).resolve().parent.parent
    / "pages" / "demand_planner_analytics_view.py"
)

#: Functions whose signatures the page must track.  Anything imported from a
#: data source and called positionally is a candidate; these are the ones whose
#: signatures have actually churned.
_WATCHED: tuple = (
    "build_business_health",
    "build_business_health_categories",
    "build_business_health_sku",
    "build_demand_plan_bridge",
    "build_ro_fiscal_bridge",
    "analyse_ro_delta",
    "check_distribution_tracker",
    "check_ro_item_master",
    "seed_scope_mask",
    "build_item_dim_frame_cascade",
    "save_ro_item_master",
    "run_distribution_tracker_pipeline",
    "run_demand_plan_pipeline",
)


def _calls_in_page() -> list:
    """Every call to a watched function: ``(name, positional, keywords, line)``.

    Pure AST over the file — no import, so collection stays side-effect free.
    """
    tree = ast.parse(_PAGE_PATH.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None)
        if name not in _WATCHED:
            continue
        starred = any(isinstance(a, ast.Starred) for a in node.args)
        kwargs = [kw.arg for kw in node.keywords]
        out.append((name, len(node.args), tuple(kwargs), node.lineno, starred))
    return out


_CALLS = _calls_in_page()


def _page():
    """Import the page lazily — see the module docstring."""
    import pages.demand_planner_analytics_view as page
    return page


def test_the_watched_functions_are_actually_called():
    """Guard the guard: a typo in _WATCHED would make this file pass vacuously."""
    assert _CALLS, "no watched calls found — is _WATCHED stale?"
    assert "build_business_health" in {c[0] for c in _CALLS}


@pytest.mark.parametrize(
    "name,n_pos,kwargs,line,starred", _CALLS,
    ids=[f"{c[0]}:L{c[3]}" for c in _CALLS],
)
def test_every_call_matches_its_signature(name, n_pos, kwargs, line, starred):
    """Bind each call's arguments against the real signature.

    ``Signature.bind`` reproduces what Python does at call time, so a wrong
    positional count, an unknown keyword or a missing required argument all
    fail here — reported at the line the traceback would have shown.
    """
    if starred or None in kwargs:
        pytest.skip("*args / **kwargs unpacking — arity is not statically known")

    target = getattr(_page(), name, None)
    if target is None or not callable(target):
        pytest.skip(f"{name} is not bound in the page module")

    sig = inspect.signature(target)
    try:
        sig.bind(*([object()] * n_pos), **{k: object() for k in kwargs})
    except TypeError as exc:
        raise AssertionError(
            f"{_PAGE_PATH.name}:{line} calls {name} with {n_pos} positional "
            f"arg(s) and {sorted(kwargs)}, but the signature is "
            f"{name}{sig} — {exc}"
        ) from None


def test_business_health_is_called_orders_only():
    """The specific regression: no third positional argument survives."""
    for name, n_pos, _kw, line, _s in _CALLS:
        if name == "build_business_health":
            assert n_pos <= 2, (
                f"L{line}: build_business_health lost its shipments and finance "
                f"parameters — pass (orders_enriched, prior_month)"
            )


# ── The failing path, exercised ──────────────────────────────────────────────

def test_the_order_yoy_helper_runs_end_to_end(monkeypatch):
    """``_cached_comparison_order_yoy`` is what blew up on Streamlit Cloud.

    Static binding proves the call is *shaped* right; this proves it runs.
    Fabric is stubbed, so it exercises the wiring rather than the data.
    """
    import pandas as pd
    from datetime import date

    page = _page()
    monkeypatch.setattr(page, "_load_demand_comparison_ibp_orders",
                        lambda months=None: (pd.DataFrame(), None))
    monkeypatch.setattr(page, "_load_demand_comparison_pdh", lambda: None)
    monkeypatch.setattr(page, "enrich_ibp_orders_df", lambda o, p: None)

    fn = page._cached_comparison_order_yoy
    fn = getattr(fn, "__wrapped__", fn)        # bypass st.cache_data
    yoy, labels = fn(date(2026, 8, 1), frozenset())

    assert isinstance(yoy, dict) and yoy, "one entry per comparison row"
    assert set(labels) == {"L12M", "L6M", "L3M"}


# ── Step 2 offers all three plan files ───────────────────────────────────────

def test_step_2_offers_three_downloadable_plan_files():
    """Management Plan, Total Item-Level Demand, and Item-Customer Detail.

    Pinned by widget key: each ``_render_demand_summary_file`` call is one red
    primary download button, and dropping one is otherwise a silent loss.
    """
    src = _PAGE_PATH.read_text(encoding="utf-8")
    for key in ("demand_summary_dl_mgmt_plan_full",
                "demand_summary_dl_total_item_level_demand",
                "demand_summary_dl_item_customer_detail"):
        assert src.count(f'download_button_key="{key}"') == 1, key


def test_the_item_customer_detail_is_wired_to_its_own_source():
    from data_sources.demand_summary import demand_item_customer_detail_blob_path

    assert (demand_item_customer_detail_blob_path()
            == "RO Tracking/Demand Plan/qry_demand_item_customer_detail.csv")


def test_every_cached_demand_summary_blob_is_registered_for_the_cache_bound():
    """``_CACHED_BLOB_PATHS`` sizes the cache; a blob missing from it evicts early.

    The module says so itself — the list exists "so the cache bound is derived
    from reality instead of a magic number that silently goes stale the day a
    ninth source is added". Adding a fetcher without registering its blob is
    exactly that failure, and it shows up only as mysterious cache misses.
    """
    import data_sources.demand_summary as ds

    registered = set(ds._CACHED_BLOB_PATHS)
    for name in dir(ds):
        if name.endswith("_BLOB_PATH") and name.startswith("_"):
            path = getattr(ds, name)
            if isinstance(path, str) and path.endswith(".csv"):
                assert path in registered, (
                    f"{name} is fetched but missing from _CACHED_BLOB_PATHS"
                )
