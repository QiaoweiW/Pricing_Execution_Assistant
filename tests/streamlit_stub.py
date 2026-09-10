"""One shared headless Streamlit stub for the render-test modules.

Why this has to be shared
-------------------------
The page module can only be imported once per test session, and the first test
module to import it binds ``page.st`` to whatever ``sys.modules["streamlit"]``
held at that moment — permanently.  Each render-test module used to build its
own ``MagicMock`` and assign it, so the winner was decided by pytest's
alphabetical collection order.  Any module that lost simply configured an
object the page no longer referenced, and its fixtures had no effect: adding
``test_aps_step_flow_render.py`` (which sorts first) silently broke
``test_ro_pipeline_render.py``'s captures without touching that file.

:func:`install` removes the race by returning the *same* stub to every caller —
whoever imports first creates it, everyone after reuses it.  Order stops
mattering, because there is only ever one object.

Each module still owns its own per-test behaviour: configure the returned stub
inside a fixture (see the ``caps`` fixtures), which runs long after import.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock


class Ctx:
    """Stands in for a column / container / expander.

    A real class, not a ``MagicMock``: an exception raised inside
    ``with col:`` must propagate, and a mock's ``__exit__`` returns a truthy
    Mock, which would swallow it and hollow out every test in the file.
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


def _columns(spec, **_k):
    return [Ctx() for _ in (spec if isinstance(spec, (list, tuple)) else range(spec))]


def install() -> MagicMock:
    """Return the shared streamlit stub, installing it on first call."""
    existing = sys.modules.get("streamlit")
    if isinstance(existing, MagicMock):
        return existing

    st = MagicMock()
    st.session_state = {}
    st.fragment = lambda f: f
    # Pass-through caching decorators.  These are applied at IMPORT time, so a
    # bare MagicMock would replace each decorated function itself and every
    # cached helper on the page would return a Mock instead of its value.
    st.cache_data = lambda *a, **k: (lambda fn: fn)
    st.cache_resource = lambda *a, **k: (lambda fn: fn)
    st.columns = _columns
    st.expander = lambda *a, **k: Ctx()
    st.container = lambda *a, **k: Ctx()
    st.popover = lambda *a, **k: Ctx()
    sys.modules["streamlit"] = st
    sys.modules.setdefault("streamlit.components", MagicMock())
    sys.modules.setdefault("streamlit.components.v1", MagicMock())
    return st


def reset(st: MagicMock) -> None:
    """Restore the import-time defaults before a test configures the stub.

    The stub is shared, so one module's fixture must not inherit the element
    behaviours another module's fixture last assigned.
    """
    st.session_state = {}
    st.fragment = lambda f: f
    st.columns = _columns
    st.expander = lambda *a, **k: Ctx()
    st.container = lambda *a, **k: Ctx()
    st.popover = lambda *a, **k: Ctx()


def bind(page_module) -> MagicMock:
    """Return the streamlit object *the page is actually bound to*, reset.

    :func:`install` makes every module share one stub, but that only settles
    the common case.  The page keeps whatever ``import streamlit as st`` gave
    it at import time, and that binding is fixed for the session — so a fixture
    that configures ``sys.modules["streamlit"]`` is configuring the right
    object only as long as nothing else got there first.

    Configuring ``page.st`` instead is true by construction: it is definitionally
    the object the render functions will call.  Fixtures use this rather than
    the module-level stub, which is what keeps these files order-independent.
    """
    st = page_module.st
    reset(st)
    return st
