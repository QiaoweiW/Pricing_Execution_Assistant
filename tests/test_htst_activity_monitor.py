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
import data_sources.htst_shipment as shipment_mod     # noqa: E402
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


# ── Column projection contract ───────────────────────────────────────────────

def test_analysis_columns_cover_pipeline_inputs():
    """The connector projects the lakehouse read down to ANALYSIS_COLUMNS.

    That projection is what keeps the page inside a Streamlit Cloud memory
    budget (1 409 MB -> 202 MB measured), but it means a column the pipeline
    needs and the connector does not request is simply absent at runtime —
    a silent blank metric rather than a loud failure.

    _shipment() is the minimal frame the whole enrichment + requote pipeline
    is proven to run on by the tests below, so it doubles as the authoritative
    statement of what the page consumes.  If someone teaches the page to read
    a new source column, this test fails until the connector asks for it too.
    """
    needed = set(_shipment().columns)
    projected = set(shipment_mod.ANALYSIS_COLUMNS)
    assert needed <= projected, (
        f"pipeline reads columns the connector never projects: "
        f"{sorted(needed - projected)}"
    )


def test_analysis_columns_include_the_filter_predicate():
    """PRODUCTGROUP must survive projection — it is the pushdown predicate
    and the page's own safety-net re-filter both depend on it."""
    assert "PRODUCTGROUP" in shipment_mod.ANALYSIS_COLUMNS


def test_resolve_projection_skips_columns_absent_upstream():
    """A renamed/dropped upstream column degrades to a warning, not a crash."""
    class _Con:
        def execute(self, sql, *a):
            assert "LIMIT 0" in sql
            return SimpleNamespace(description=[
                ("PRODUCTGROUP",), ("SHIPTONAME",), ("Ordered LBS",)])

    select_list, missing = shipment_mod._resolve_projection(_Con(), "abfss://x")
    assert select_list == '"PRODUCTGROUP", "SHIPTONAME", "Ordered LBS"'
    assert "Customer" in missing and "Order Date" in missing


def test_resolve_projection_falls_back_to_star_when_probe_fails():
    class _Con:
        def execute(self, sql, *a):
            raise RuntimeError("delta_scan exploded")

    select_list, missing = shipment_mod._resolve_projection(_Con(), "abfss://x")
    assert select_list == "*" and missing == []


def test_quote_ident_escapes_embedded_quotes():
    """Column names carry spaces and dots ('Ordered LBS', 'Savannah.Key');
    quoting them is mandatory and must not be defeatable."""
    assert shipment_mod._quote_ident("Ordered LBS") == '"Ordered LBS"'
    assert shipment_mod._quote_ident('we"ird') == '"we""ird"'


# ── Lazy CSV export identity ─────────────────────────────────────────────────

def test_frame_identity_tracks_filter_changes():
    """A prepared CSV must not be offered after the filter moves under it."""
    enr = _enriched()
    base = page._frame_identity(enr)
    assert page._frame_identity(enr) == base                    # stable
    subset = enr[enr["SHIPTONAME"] == "S1"]
    assert page._frame_identity(subset) != base                 # rows changed
    dropped = enr.drop(columns=["Mileage"])
    assert page._frame_identity(dropped) != base                # columns changed


def test_frame_identity_handles_empty_frame():
    enr = _enriched()
    assert page._frame_identity(enr.iloc[0:0])                  # must not raise


def test_lazy_download_caches_payload_and_expires_it_on_identity_change():
    """The prepare→download handshake must never serve a stale CSV.

    Rebuild once on the prepare click, reuse it for free while the data is
    unchanged, and withdraw the download button the moment the underlying
    selection moves — otherwise a user downloads a file that disagrees with
    the table they are looking at.
    """
    calls = []

    def _build():
        calls.append(1)
        return b"col\n1\n"

    _ST.session_state.clear()
    _ST.download_button.reset_mock()

    # 1. First render: nothing prepared, user clicks "Prepare" -> builds once.
    _ST.button.return_value = True
    page._lazy_csv_download(label="X", key="k", file_name="f.csv",
                            identity="id-1", build=_build)
    assert calls == [1]
    assert _ST.download_button.call_count == 0        # revealed on the rerun

    # 2. Rerun with the same data: served from session_state, no rebuild.
    _ST.button.return_value = False
    page._lazy_csv_download(label="X", key="k", file_name="f.csv",
                            identity="id-1", build=_build)
    assert calls == [1]
    assert _ST.download_button.call_count == 1
    assert _ST.download_button.call_args.kwargs["data"] == b"col\n1\n"

    # 3. Filter moved: the stale payload must NOT be offered for download.
    page._lazy_csv_download(label="X", key="k", file_name="f.csv",
                            identity="id-2", build=_build)
    assert calls == [1]
    assert _ST.download_button.call_count == 1        # still just the one
    _ST.button.return_value = False


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


def test_build_customer_deepdive_fee_bridge_and_metrics():
    enr = _enriched()
    w = page._trailing_windows(enr["Order Date"])
    # threshold=0 so the synthetic S1 drift (≈$9k/yr) qualifies for the test.
    dd = page._build_customer_deepdive(enr, w, None, None, None, None, monthly_threshold=0.0)
    assert dd, "expected at least one deep-dive"
    top = dd[0]
    assert top["customer"] == "C1"                       # the drifting customer
    assert top["annual_impact"] > 0 and top["total_delta"] > 0
    # Fee bridge is internally consistent.
    assert len(top["components"]) == 4
    assert sum(c["delta"] for c in top["components"]) == pytest.approx(top["total_delta"], rel=1e-6)
    assert top["annual_impact"] == pytest.approx(top["total_delta"] * top["annual_volume"], rel=1e-6)
    # Drop size moved (shrank), so it is charted; stable metrics are not.
    charted = {m["key"] for m in top["metrics"]}
    assert "drop_size" in charted
    assert all(m["chart"] in ("bar", "line") for m in top["metrics"])


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
