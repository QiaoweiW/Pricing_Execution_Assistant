"""Velocity Analysis — Portfolio Minor via PRODUCTDESC → PDH.

dbo.Shipments carries only a Product Description (no item number), so Portfolio
Minor is derived by matching PRODUCTDESC to PDH's Item Description.  Streamlit is
stubbed (with pass-through cache/fragment decorators) so the page module imports
without a running server.
"""
import sys
from unittest.mock import MagicMock

import pandas as pd


class _Ctx:
    def __enter__(self): return self
    def __exit__(self, *a): return False


_ST = MagicMock()
_ST.session_state = {}
_ST.cache_data = lambda *a, **k: (lambda fn: fn)
_ST.cache_resource = lambda *a, **k: (lambda fn: fn)
_ST.fragment = lambda fn: fn
_ST.expander = lambda *a, **k: _Ctx()
_ST.columns = lambda spec, **k: [_Ctx() for _ in (spec if isinstance(spec, (list, tuple)) else range(spec))]
sys.modules["streamlit"] = _ST
sys.modules.setdefault("streamlit.components", MagicMock())
sys.modules.setdefault("streamlit.components.v1", MagicMock())

import pages.demand_planner_analytics_view as page   # noqa: E402
import data_sources.shipments_velocity as vel         # noqa: E402


def _patch_pdh(monkeypatch, dim: pd.DataFrame) -> None:
    monkeypatch.setattr(page, "_load_demand_comparison_pdh", lambda: object())
    monkeypatch.setattr(page, "build_item_dim_frame", lambda _pdh: dim)


def test_attach_pminor_by_product_desc(monkeypatch):
    dim = pd.DataFrame({
        "__item_key": ["1", "2"],
        "desc": ["DG Milk Gallon", "DG Btr Qtr 1Lb"],
        "pminor": ["Fluid Milk", "Packaged Butter"],
    })
    _patch_pdh(monkeypatch, dim)
    # Normalisation: extra spaces + case must still match; unknowns → "".
    ship = pd.DataFrame({vel.COL_PRODUCT_DESC: ["dg milk  gallon ", "DG BTR QTR 1LB", "Mystery"]})
    out = page._velocity_attach_portfolio_minor(ship)
    assert list(out[vel.COL_PRODUCT_MINOR]) == ["Fluid Milk", "Packaged Butter", ""]


def test_attach_pminor_noop_when_already_present():
    ship = pd.DataFrame({vel.COL_PRODUCT_MINOR: ["X"], vel.COL_PRODUCT_DESC: ["y"]})
    assert page._velocity_attach_portfolio_minor(ship) is ship   # untouched


def test_norm_desc_collapses_and_casefolds():
    s = page._velocity_norm_desc(pd.Series(["  DG   Milk Gallon "]))
    assert s.iloc[0] == "dg milk gallon"
