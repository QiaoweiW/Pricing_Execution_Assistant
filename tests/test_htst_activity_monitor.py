"""Unit tests for the Shipment Monitor & HTST Requote dashboard.

Covers the enrichment pipeline (Format / Pallet / Pricing), the trailing-window
momentum metrics, the requote-candidate detection, the dynamic delivery-fee map,
and the lakehouse lookup fetcher (io mocked).  Streamlit is stubbed so the page
module imports without a running server; ``cache_data`` is made a pass-through
so the cached fetcher runs the real function under test.
"""
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest


class _Ctx:
    def __enter__(self): return self
    def __exit__(self, *a): return False


_ST = MagicMock()
_ST.session_state = {}
_ST.columns = lambda spec, **k: [MagicMock() for _ in (spec if isinstance(spec, (list, tuple)) else range(spec))]
_ST.expander = lambda *a, **k: _Ctx()
_ST.cache_data = lambda *a, **k: (lambda f: f)      # pass-through decorator
_ST.cache_resource = lambda *a, **k: (lambda f: f)
sys.modules["streamlit"] = _ST
sys.modules.setdefault("streamlit.components", MagicMock())
sys.modules.setdefault("streamlit.components.v1", MagicMock())

import pages.htst_activity_monitor_view as page       # noqa: E402
import data_sources.htst_shipment_lookups as lookups_mod  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _shipment() -> pd.DataFrame:
    """Site S1 drops shrink over the year (requote); S2 stays large (stable)."""
    rows = []

    def add(cust, site, desc, psn, date, orders, lbs_each):
        for i in range(orders):
            rows.append({
                "PRODUCTGROUP": "HTST", "Customer": cust, "SHIPTONAME": site,
                "Shipping Warehouse": "WH1", "PRODUCTDESC": desc,
                "Party Site Number": psn, "Order Number": f"{site}-{date}-{i}",
                "Ordered LBS": lbs_each, "Ordered Secondary QTY": lbs_each / 10.0,
                "Order Date": date,
            })

    old = [f"2025-{m:02d}-15" for m in range(7, 13)] + [f"2026-{m:02d}-15" for m in range(1, 4)]
    recent = ["2026-04-15", "2026-05-15", "2026-06-15"]
    for d in old:
        add("C1", "S1", "DG Milk Gallon", "PS1", d, 1, 100_000)   # big drops
    for d in recent:
        add("C1", "S1", "DG Milk Gallon", "PS1", d, 1, 5_000)     # shrunk drops
    for d in old + recent:
        add("C2", "S2", "DG Milk Half Gallon", "PS2", d, 1, 40_000)
    return pd.DataFrame(rows)


def _lookups() -> dict:
    return {
        "plant_tracker": pd.DataFrame({"Shipping Warehouse": ["WH1"], "Plant": ["P1"]}),
        "mileage_tracker": pd.DataFrame({
            "Sourcing Plant": ["P1", "P1"], "SHIPTONAME": ["S1", "S2"], "Mileage": [250, 250]}),
        "demantra": pd.DataFrame({
            "Item Description": ["DG Milk Gallon", "DG Milk Half Gallon"],
            "Total Each Per Pallet": [100, 100], "Unit Net Weight": [10, 10],
            "Product Size": ["Gallon", "64 Ounce"],
            "Unit Pkg Type": ["Plastic Jug", "Paper Carton"]}),
        "pricing_tracker": pd.DataFrame({
            "Item Description": ["DG Milk Gallon", "DG Milk Half Gallon"],
            "Party Site Number": ["PS1", "PS2"], "Pricing Method": [1, 1]}),
    }


def _enriched() -> pd.DataFrame:
    enr = page._process_shipment_data(_shipment(), _lookups())
    enr["Order Date"] = pd.to_datetime(enr["Order Date"])
    return enr


# ── Enrichment ───────────────────────────────────────────────────────────────

def test_enrichment_adds_format_pallet_pricing():
    enr = page._process_shipment_data(_shipment(), _lookups())
    assert enr is not None
    assert set(enr["Format"].unique()) == {"Gallon Plastic Jug", "64 Ounce Paper Carton"}
    # Full-pallet: order 100k or 5k vs full-pallet lbs 1000 → Pallet% ≥ 0.9 → Full.
    assert set(enr["Pallet Status"].unique()) == {"Full"}
    assert (enr["Pricing Method"] == 1).all()          # delivered, not FOB


# ── Trailing windows + momentum ──────────────────────────────────────────────

def test_trailing_windows_anchor_and_span():
    w = page._trailing_windows(_enriched()["Order Date"])
    assert set(w) == {"L12M", "L6M", "L3M"}
    assert w["L3M"][1].date().isoformat() == "2026-06-15"     # anchor = latest
    assert w["L12M"][2] > w["L6M"][2] > w["L3M"][2]           # spans widen


def test_window_metrics_drop_and_mix():
    enr = _enriched()
    w = page._trailing_windows(enr["Order Date"])
    s1 = enr[enr["SHIPTONAME"] == "S1"]
    m = page._window_metrics(page._window_slice(s1, w["L3M"]), w["L3M"][2])
    assert m["mixed_pct"] == 0.0                              # all Full
    assert m["drop_size"] == pytest.approx(5_000.0)          # 3 recent 5k orders
    assert m["mileage"] == pytest.approx(250.0)


def test_build_momentum_shape():
    enr = _enriched()
    w = page._trailing_windows(enr["Order Date"])
    mom = page._build_momentum(enr, w)
    assert list(mom["Metric"]) == [name for name, _, _ in page._MOMENTUM_METRICS]
    assert {"L12M", "L6M", "L3M"}.issubset(mom.columns)


# ── Requote detection ────────────────────────────────────────────────────────

def test_build_requote_flags_shrinking_drops():
    enr = _enriched()
    w = page._trailing_windows(enr["Order Date"])
    req = page._build_requote(enr, w, None, None, None, None)
    assert not req.empty
    top = req.iloc[0]
    assert top["SHIPTONAME"] == "S1"                          # the drifting site
    assert top["Δ Total Activity Fee"] > 0                    # fee rose
    assert top["$ Impact (Δfee × L3M vol)"] > 0
    assert "delivery tier worse" in top["Requote Drivers (L12M→L3M)"]


def test_summary_handles_category_dtype_keys():
    """The lakehouse shipment frame stores identity columns as `category`;
    grouping must stay on observed combinations, not explode to the Cartesian
    product of levels (regression for a 5M-row blow-up on real data)."""
    enr = _enriched()
    for c in ("Customer", "SHIPTONAME", "PRODUCTDESC", "PRODUCTGROUP", "Format"):
        if c in enr.columns:
            enr[c] = enr[c].astype("category")
    w = page._trailing_windows(enr["Order Date"])
    req = page._build_requote(enr, w, None, None, None, None)     # must not raise
    assert not req.empty and req.iloc[0]["SHIPTONAME"] == "S1"


# ── Dynamic delivery-fee map ─────────────────────────────────────────────────

def test_delivery_fee_map_dynamic_and_fallback():
    dv = pd.DataFrame({
        "Mileage Fee Tier (Mi)": ["A. >= 1000 mi"],
        "Drop Fee Tier (lbs/Drop Size)": ["A. >= 30k lbs"],
        "Delivery Charge ($/Gal)": [" $0.99 "],
    })
    assert page._delivery_fee_map(dv)[("A. >= 1000 mi", "A. >= 30k lbs")] == pytest.approx(0.99)
    assert page._delivery_fee_map(None) == dict(page._DELIVERY_FEES)   # fallback


# ── Lookup fetcher (io mocked) ───────────────────────────────────────────────

def _fake_files(folder):
    names = {
        "Activity_Model/Shipment Report": [
            "Shipment_Plant_Tracker_20260409.csv",
            "Ship_Route_Mileage_Tracker_20260409.csv",
            "Demantra_04102026.csv",
            "Delivered vs FOB_Tracker_20260410.csv",
        ],
        "Activity_Model": [
            "Sell-to_Volume Bracket_Fee.csv",
            "Custom Label_Volume Bracket_Fee.csv",
            "Pallet_Fee.csv",
            "Delivery_Miles Tier_Drop Size Tier_Fee.csv",
        ],
    }.get(folder, [])
    return [SimpleNamespace(name=n, full_path=f"{folder}/{n}", last_modified="2026-07-25")
            for n in names]


def test_fetch_htst_lookups_bundle(monkeypatch):
    monkeypatch.setattr(lookups_mod._io, "list_files", lambda s, f, suffix=None: _fake_files(f))
    monkeypatch.setattr(lookups_mod._io, "read_csv", lambda s, p, **k: (pd.DataFrame({"a": [1]}), "etag"))
    b = lookups_mod.fetch_htst_lookups()
    assert set(b.frames) == {
        "plant_tracker", "mileage_tracker", "demantra", "pricing_tracker",
        "sell_to", "custom_label", "pallet_fee", "delivery"}
    assert len(b.files) == 8


def test_fetch_htst_lookups_missing_required_raises(monkeypatch):
    # Drop the Shipment Report folder → required enrichment lookup missing.
    monkeypatch.setattr(lookups_mod._io, "list_files",
                        lambda s, f, suffix=None: _fake_files(f) if f == "Activity_Model" else [])
    monkeypatch.setattr(lookups_mod._io, "read_csv", lambda s, p, **k: (pd.DataFrame({"a": [1]}), "etag"))
    with pytest.raises(lookups_mod.HTSTLookupError):
        lookups_mod.fetch_htst_lookups()
