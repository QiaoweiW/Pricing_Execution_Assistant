"""Every ``@st.cache_data`` function must be able to notice new data.

The bug this file exists to prevent
------------------------------------
Streamlit excludes any parameter whose name starts with an underscore from the
cache key.  ``_cached_aps_comparison_options`` was written with a shape
signature *specifically* so it would recompute when the APS history changed::

    def _cached_aps_comparison_options(
        _aps_sig, _ibp_sig, _pdh_sig, _aps_hist, _ibp_tracker, _pdh):

Every name is underscored, so nothing was hashed, so the key was constant.  The
function served the first cycle list of the hour forever.  A planner uploaded
cycle C6, the rows landed correctly in Fabric, and C6 simply was not in the
"Current cycle (APS)" picker — no error, no warning, just a stale menu and a
comparison quietly run against the previous cycle.

Its own docstring claimed it was "keyed on the source shape signatures".  Only
a machine reading the parameter names catches the contradiction, so:

A cached function is safe if EITHER
  * at least one parameter is hashed (no leading underscore), so new inputs
    produce a new key; or
  * something calls ``<fn>.clear()`` somewhere, so a write can invalidate it.

A function with neither can only be refreshed by waiting out its TTL, which is
exactly the failure above.  Zero-argument functions are fine — they take no
inputs to key on and are always paired with an explicit clear.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent
_DIRS = ("pages", "data_sources")


def _python_files() -> list[Path]:
    return sorted(
        f for d in _DIRS for f in (_ROOT / d).glob("*.py") if f.name != "__init__.py"
    )


def _is_streamlit_cache(decorator: ast.expr) -> bool:
    """True for ``@st.cache_data`` / ``@st.cache_resource``, called or bare."""
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    return isinstance(node, ast.Attribute) and node.attr in {
        "cache_data", "cache_resource"}


def _cached_functions() -> list[tuple[str, str, int, tuple[str, ...]]]:
    """``(module, fn_name, lineno, params)`` for every cached function."""
    out = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_is_streamlit_cache(d) for d in node.decorator_list):
                continue
            a = node.args
            params = tuple(
                p.arg for p in a.posonlyargs + a.args + a.kwonlyargs)
            out.append((path.name, node.name, node.lineno, params))
    return out


_CACHED = _cached_functions()
#: One concatenated blob of every source file — enough to answer "does anything
#: anywhere call ``<fn>.clear()``?" without re-reading per test.
_ALL_SOURCE = "\n".join(p.read_text(encoding="utf-8") for p in _python_files())


def test_the_scan_finds_the_cached_functions():
    """Guard the guard: a broken matcher would make every test below vacuous."""
    assert len(_CACHED) > 40, f"only found {len(_CACHED)} cached functions"
    names = {n for _m, n, _l, _p in _CACHED}
    assert "_cached_aps_comparison_options" in names


@pytest.mark.parametrize(
    "module,name,lineno,params", _CACHED,
    ids=[f"{m}:{n}" for m, n, _l, _p in _CACHED],
)
def test_every_cache_can_be_invalidated(module, name, lineno, params):
    if not params:
        return                                  # nothing to key on by design
    if any(not p.startswith("_") for p in params):
        return                                  # has a real cache key
    assert f"{name}.clear()" in _ALL_SOURCE, (
        f"{module}:{lineno} {name}({', '.join(params)}) has a CONSTANT cache "
        f"key — Streamlit ignores underscore-prefixed parameters — and nothing "
        f"calls {name}.clear(), so it can only go stale until its TTL lapses. "
        f"Drop the underscore from whichever parameter is meant to be the key "
        f"(keep it on unhashable payloads like DataFrames), or clear it on the "
        f"write path."
    )


def test_the_aps_options_signature_stays_hashed():
    """The exact regression: the C6-missing-from-the-picker bug.

    Pinned by name rather than by the generic rule above, because this one is
    also covered by a ``.clear()`` now — so the generic rule alone would let
    the underscores creep back.
    """
    match = [c for c in _CACHED if c[1] == "_cached_aps_comparison_options"]
    assert match, "_cached_aps_comparison_options not found"
    _m, _n, _l, params = match[0]
    hashed = [p for p in params if not p.startswith("_")]
    assert hashed == ["aps_sig", "ibp_sig", "pdh_sig"], (
        f"the shape signatures must stay hashed (found {hashed}) — underscoring "
        f"them is what hid a freshly uploaded cycle from the picker"
    )


def test_every_aps_write_path_flushes_through_one_helper():
    """APS writes must go through ``_flush_aps_caches``, not ad-hoc clears.

    The C6 bug was a *missed* clear: the build cleared two caches by hand and
    silently left the third.  Funnelling every write through one helper is what
    stops the next cache from being half-wired the same way.
    """
    page = (_ROOT / "pages" / "demand_planner_analytics_view.py").read_text(
        encoding="utf-8")
    tree = ast.parse(page)
    helper = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_flush_aps_caches"),
        None)
    assert helper is not None, "_flush_aps_caches is gone"

    cleared = {
        node.func.value.id
        for node in ast.walk(helper)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "clear" and isinstance(node.func.value, ast.Name)
    }
    assert cleared == {
        "_cached_persisted_aps_plan", "_cached_aps_history",
        "_cached_aps_history_summary", "_cached_aps_comparison_options",
    }, f"the APS flush set changed: {sorted(cleared)}"
    # Both write paths — the build and the delete — must call it.
    assert page.count("_flush_aps_caches()") >= 3, (
        "expected the definition plus a call from each APS write path"
    )
