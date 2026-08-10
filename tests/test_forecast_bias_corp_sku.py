"""Tests for the Forecast Bias Corporate × SKU drivers layer.

Covers the pure helpers (accuracy math, corp-group two-hop lookups, corp
resolution + fallbacks, segment-leaf collection) and one end-to-end build on
synthetic data engineered so the party_site → customer_num → corporate_group
chain actually joins — proving the builder is correct when the dimension keys
reconcile (the production blocker is a dim-data gap, not this code).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from data_sources.demand_plan_comparison import (
    CORP_UNATTRIBUTED,
    ComparisonFilters,
    CorpSkuDriversResult,
    BIAS_COL_WMAPE,
    BIAS_COL_IMPACT,
    TRK_CYCLE,
    TRK_DEMAND_LBS,
    TRK_FORECAST_TYPE,
    TRK_ITEM,
    TRK_ITEM_DESCRIPTION,
    TRK_PARTY_SITE,
    TRK_PMAJ,
    TRK_PMINOR,
    TRK_SFMT,
    TRK_START_OF_MONTH,
    build_corp_group_lookups,
    build_forecast_bias_corp_sku_drivers,
    _accuracy_stats,
    _resolve_corp_actual,
    _resolve_corp_forecast,
    _segment_leaf_rows,
    COMPARISON_TEMPLATE,
)


# ── _accuracy_stats ──────────────────────────────────────────────────────────

def test_accuracy_stats_basic():
    """WMAPE / bias / FVA / impact on a hand-checked series."""
    f = [10.0, 12.0, 8.0]
    a = [9.0, 13.0, 10.0]
    nv = [0.0, 0.0, 0.0]            # naive = 0 → naive error = Σ|a| → wmape_naive = 1
    s = _accuracy_stats(f, a, nv, covered=[0, 1, 2], total_volume=64.0)
    # Σ|a-f| = 1 + 1 + 2 = 4 ; Σ|a| = 32
    assert s.abs_error == pytest.approx(4.0)
    assert s.volume == pytest.approx(32.0)
    assert s.wmape == pytest.approx(4.0 / 32.0)
    assert s.impact == pytest.approx(4.0 / 64.0)
    assert s.fva == pytest.approx(1.0 - 4.0 / 32.0)   # naive wmape 1.0 − forecast wmape
    # avg of per-month bias (f-a)/a
    assert s.avg_bias == pytest.approx(((1 / 9) + (-1 / 13) + (-2 / 10)) / 3)


def test_accuracy_stats_uncovered_month_ignored():
    """A month outside ``covered`` contributes to neither numerator nor denom."""
    s = _accuracy_stats([10, 999], [10, 1], [0, 0], covered=[0], total_volume=10.0)
    assert s.abs_error == pytest.approx(0.0)      # month 1 (the 999 miss) excluded
    assert s.wmape == pytest.approx(0.0)


# ── build_corp_group_lookups ─────────────────────────────────────────────────

def _shiptosites():
    # party_site_code → plan_to_code.  customer_num here is the WRONG key space
    # (a dead end) — included to prove the builder ignores it.
    return pd.DataFrame({
        "party_site_code": ["PS1", "PS2", "PS3"],
        "plan_to_code": ["PL1", "PL2", "PL3"],
        "customer_num": ["WRONG1", "WRONG2", "WRONG3"],
    })


def _plantosites():
    # plan_to_code → customer_num (the key space that matches customernames).
    # corporate_group here is deliberately a TYPO form to prove we route through
    # dp_dimcustomernames (not this denormalised column).
    return pd.DataFrame({
        "plan_to_code": ["PL1", "PL2"],
        "site_name": ["Acme Site", "Beta Site"],
        "customer_num": ["CUST1", "CUST2"],
        "corporate_group": ["Acme TYPO", "Beta TYPO"],
    })


def _customernames():
    return pd.DataFrame({
        "customer_num": ["CUST1", "CUST2", "CUST4"],
        "customer_name": ["Acme Foods", "Beta Dairy", "Gamma"],
        # casing drift on Acme + a Blank that must be dropped
        "corporate_group": ["ACME", "Beta", "Blank"],
    })


def test_build_corp_group_lookups_full_chain():
    party2corp, cust2corp = build_corp_group_lookups(
        _shiptosites(), _plantosites(), _customernames())
    # party → plan → customer_num → customernames.corporate_group.
    # PS3→PL3 has no plantosites row → dropped.  Corp comes from customernames
    # (NOT plantosites' typo column).
    assert party2corp == {"PS1": "ACME", "PS2": "Beta"}
    assert cust2corp == {"CUST1": "ACME", "CUST2": "Beta"}


def test_build_corp_group_lookups_canonicalises_casing():
    names = pd.DataFrame({
        "customer_num": ["1", "2", "3"],
        "customer_name": ["a", "b", "c"],
        "corporate_group": ["Acme Foods", "ACME FOODS", "acme foods"],
    })
    _party, cust2corp = build_corp_group_lookups(None, None, names)
    # All three collapse to ONE surface form (they tie 1-1-1 → longest wins).
    assert len(set(cust2corp.values())) == 1


# ── corp resolution + fallbacks ──────────────────────────────────────────────

def test_resolve_corp_forecast_unattributed_when_no_bridge():
    trk = pd.DataFrame({"party_site": ["PS1", "PSX"]})
    cg = _resolve_corp_forecast(trk, {"PS1": "Acme"})
    assert cg.tolist() == ["Acme", CORP_UNATTRIBUTED]


def test_resolve_corp_actual_soft_name_fallback():
    act = pd.DataFrame({
        "customer_no": ["CUST1", "CUSTX", ""],
        "customer_name": ["Acme", "Small Shop", ""],
    })
    cg, soft = _resolve_corp_actual(act, {"CUST1": "Acme"})
    assert cg.tolist() == ["Acme", "Small Shop", CORP_UNATTRIBUTED]
    assert soft.tolist() == [False, True, False]   # name fallback flagged soft


# ── _segment_leaf_rows ───────────────────────────────────────────────────────

def test_segment_leaf_rows_total_excludes_memo():
    leaves = _segment_leaf_rows("total_b2c", list(COMPARISON_TEMPLATE))
    ids = {lf.row_id for lf in leaves}
    assert "esl_lc_branded" in ids and "fm_gallon_jug" in ids and "butter" in ids
    # Cottage Cheese / Sour Cream are memo rows → excluded from the slice.
    assert "cult_cottage_cheese" not in ids and "cult_sour_cream" not in ids
    assert all(not lf.is_subtotal for lf in leaves)


def test_segment_leaf_rows_leaf_returns_itself():
    leaves = _segment_leaf_rows("butter", list(COMPARISON_TEMPLATE))
    assert [lf.row_id for lf in leaves] == ["butter"]


# ── end-to-end builder (synthetic data that DOES join) ───────────────────────

_MONTHS = [dt.date(2026, m, 1) for m in range(1, 7)]   # Jan..Jun 2026


def _tracker():
    """Base Plan, cycle C1, Fresh Milk / Gallon Jug; two customers."""
    rows = []
    for m in _MONTHS:
        rows.append({
            TRK_START_OF_MONTH: m.isoformat(), TRK_ITEM: "100",
            TRK_ITEM_DESCRIPTION: "DG Gallon Jug", TRK_PARTY_SITE: "PS1",
            TRK_DEMAND_LBS: "1000000", TRK_FORECAST_TYPE: "Base Plan",
            TRK_CYCLE: "C1", TRK_PMAJ: "Fresh Milk", TRK_SFMT: "Gallon Jug",
            TRK_PMINOR: "",
        })
        rows.append({
            TRK_START_OF_MONTH: m.isoformat(), TRK_ITEM: "200",
            TRK_ITEM_DESCRIPTION: "DG Gallon Jug B", TRK_PARTY_SITE: "PS2",
            TRK_DEMAND_LBS: "100000", TRK_FORECAST_TYPE: "Base Plan",
            TRK_CYCLE: "C1", TRK_PMAJ: "Fresh Milk", TRK_SFMT: "Gallon Jug",
            TRK_PMINOR: "",
        })
    return pd.DataFrame(rows)


def _orders():
    rows = []
    for m in _MONTHS:
        rows.append({"Item No": "100", "Month": m.isoformat(),
                     "Ordered Qty lbs": "1200000", "Customer No": "CUST1",
                     "Customer Name": "Acme Foods"})
        rows.append({"Item No": "200", "Month": m.isoformat(),
                     "Ordered Qty lbs": "500000", "Customer No": "CUST2",
                     "Customer Name": "Beta Dairy"})
    return pd.DataFrame(rows)


def _filters():
    return ComparisonFilters(
        current_cycle="C1", prior_cycle="C1",
        actual_start=dt.date(2026, 1, 1), actual_end=dt.date(2026, 6, 1),
        forecast_start=dt.date(2026, 7, 1), forecast_end=dt.date(2026, 12, 1),
        prior_month=dt.date(2026, 6, 1))


def test_corp_sku_drivers_join_and_rank():
    res = build_forecast_bias_corp_sku_drivers(
        _tracker(), _orders(), None, None, _filters(),
        segment_row_id="fresh_milk",
        shiptosites_df=_shiptosites(), plantosites_df=_plantosites(),
        customernames_df=_customernames(), top_n=3)
    assert isinstance(res, CorpSkuDriversResult) and res.available
    # Both legs attributed cleanly.
    assert res.forecast_attributed_share == pytest.approx(1.0)
    assert res.attributed_share == pytest.approx(1.0)
    d = res.drivers
    assert len(d) == 2
    # Beta: |0.1−0.5|×6 = 2.4M error > Acme: |1.0−1.2|×6 = 1.2M → Beta ranks first.
    assert d.iloc[0]["corp_group"] == "Beta" and d.iloc[0]["item_key"] == "200"
    assert d.iloc[1]["corp_group"] == "ACME" and d.iloc[1]["item_key"] == "100"
    assert not d["unattributed"].any() and not d["soft"].any()
    acme = d[d["item_key"] == "100"].iloc[0]
    assert acme[BIAS_COL_WMAPE] == pytest.approx(0.2 / 1.2, abs=1e-3)   # 16.7%
    assert acme[BIAS_COL_IMPACT] == pytest.approx(1.2 / 10.2, abs=1e-3)


def test_corp_sku_drivers_top_n_zero_returns_every_cell():
    """``top_n=0`` = no cut — the filtered-list view needs the full universe to
    build its Brand / Corporate group / SKU options from."""
    kwargs = dict(
        segment_row_id="fresh_milk",
        shiptosites_df=_shiptosites(), plantosites_df=_plantosites(),
        customernames_df=_customernames())
    capped = build_forecast_bias_corp_sku_drivers(
        _tracker(), _orders(), None, None, _filters(), top_n=1, **kwargs)
    full = build_forecast_bias_corp_sku_drivers(
        _tracker(), _orders(), None, None, _filters(), top_n=0, **kwargs)
    assert len(capped.drivers) == 1 and len(full.drivers) == 2
    # Still impact-ranked, and the cap is just the head of the full list.
    assert (full.drivers.iloc[0]["_driver_id"]
            == capped.drivers.iloc[0]["_driver_id"])


def test_corp_sku_drivers_carry_brand():
    """Brand rides along the SKU leg so the list can filter Branded/Private."""
    res = build_forecast_bias_corp_sku_drivers(
        _tracker(), _orders(), None, None, _filters(),
        segment_row_id="fresh_milk",
        shiptosites_df=_shiptosites(), plantosites_df=_plantosites(),
        customernames_df=_customernames(), top_n=0)
    # Both fixture items are "DG …" descriptions → Branded.
    assert set(res.drivers["brand"]) == {"Branded"}


def test_corp_sku_drivers_no_bridge_flags_zero_forecast_attribution():
    """Without the dp_dimplantosites bridge the forecast can't reach a corp
    group — the guard signal (forecast_attributed_share≈0) must fire so the UI
    blocks, even though ship-to-sites and customer-names are present."""
    res = build_forecast_bias_corp_sku_drivers(
        _tracker(), _orders(), None, None, _filters(),
        segment_row_id="fresh_milk",
        shiptosites_df=_shiptosites(), plantosites_df=None,
        customernames_df=_customernames(), top_n=3)
    assert res.forecast_attributed_share == pytest.approx(0.0)
    # Actual side still maps (customer_no → customernames), coverage stays high.
    assert res.attributed_share == pytest.approx(1.0)
