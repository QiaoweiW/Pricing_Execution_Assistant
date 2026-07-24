"""Unit tests for the Business Health per-category four-lever deep-dive:
Lever A (gap read), B (Net-Sales decomposition), C (Vol×Margin), D (concentration),
plus the Finance enrichment that feeds B/C."""

from datetime import date

import pandas as pd
import pytest

from data_sources.demand_plan_comparison import (
    BH_CATEGORY_ROWS,
    BH_CATEGORY_LABELS,
    BH_GAP_BUILDING,
    BH_GAP_DRAINING,
    BH_GAP_BALANCED,
    BH_MOVE_EARNED,
    BH_MOVE_GIVEN_BACK,
    BH_MOVE_BOUGHT,
    BH_MOVE_LOST,
    BH_VM_HEALTHY,
    BH_VM_RENTING,
    BH_VM_SHEDDING,
    BH_VM_BLEED,
    _bh_gap_flag,
    _bh_decomp_sowhat,
    _bh_vol_margin_reading,
    build_business_health_categories,
    enrich_finance_df,
)


PRIOR = date(2026, 6, 1)   # single-month fixtures land in every window ending here


# ── Pure-helper tests (exhaustive over the branches) ─────────────────────────

def test_gap_flag_branches():
    assert _bh_gap_flag(0.30, 0.20) == BH_GAP_BUILDING     # orders outpace ships
    assert _bh_gap_flag(0.10, 0.30) == BH_GAP_DRAINING     # ships outpace orders
    assert _bh_gap_flag(0.20, 0.20) == BH_GAP_BALANCED
    assert _bh_gap_flag(None, 0.2) == "" and _bh_gap_flag(0.2, None) == ""


def test_decomp_sowhat_branches():
    # Positive move, price/mix dominates → earned; volume dominates → bought.
    assert _bh_decomp_sowhat(0.25, 0.05) == BH_MOVE_EARNED     # price_mix +20 vs vol +5
    assert _bh_decomp_sowhat(0.25, 0.20) == BH_MOVE_BOUGHT     # price_mix +5 vs vol +20
    # Negative move, price/mix dominates → given back; volume dominates → lost.
    assert _bh_decomp_sowhat(-0.25, -0.05) == BH_MOVE_GIVEN_BACK
    assert _bh_decomp_sowhat(-0.25, -0.20) == BH_MOVE_LOST
    assert _bh_decomp_sowhat(None, 0.1) == ""


def test_vol_margin_reading_quadrants():
    assert _bh_vol_margin_reading(0.10, 0.20, 0.18) == BH_VM_HEALTHY    # vol↑ margin↑
    assert _bh_vol_margin_reading(0.10, 0.16, 0.18) == BH_VM_RENTING    # vol↑ margin↓
    assert _bh_vol_margin_reading(-0.08, 0.19, 0.18) == BH_VM_SHEDDING  # vol↓ margin↑
    assert _bh_vol_margin_reading(-0.08, 0.16, 0.18) == BH_VM_BLEED     # vol↓ margin↓
    assert _bh_vol_margin_reading(None, 0.2, 0.2) == ""


# ── Fixtures: a Butter deep-dive with clean, single-month numbers ────────────

def _orders() -> pd.DataFrame:
    """Butter orders — several Customer×SKU lines for Lever D, incl. an
    order-only account (MT Food Bank) that has NO shipment (the RCA case)."""
    rows = [
        # Growers.
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Costco",  "KS Btr Qtr", "2026-06-01", 2_000_000),
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Costco",  "KS Btr Qtr", "2025-06-01", 1_000_000),  # +1.0
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Kroger",  "KRO Salted", "2026-06-01",   800_000),
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Kroger",  "KRO Salted", "2025-06-01",   300_000),  # +0.5
        # Decliners.
        ("Butter", "Sticks", "Branded", "Packaged Butter", "MT Food Bank", "DG Btr", "2025-06-01", 1_400_000),  # -1.4 (order-only)
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Baugh",   "WhF Btr",  "2026-06-01",   100_000),
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Baugh",   "WhF Btr",  "2025-06-01",   900_000),  # -0.8
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Golden State (McD)", "DG Btr", "2026-06-01", 200_000),
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Golden State (McD)", "DG Btr", "2025-06-01", 800_000),  # -0.6
    ]
    cols = ["pmaj", "sfmt", "brand", "pminor", "customer_name", "item_desc", "month", "pounds"]
    df = pd.DataFrame(rows, columns=cols)
    df["month"] = pd.to_datetime(df["month"]).dt.date
    df["item_key"] = df["item_desc"]
    return df


def _shipments() -> pd.DataFrame:
    """Butter shipments — total L3M 3.0M vs YAG 2.5M (+20%).  Note MT Food Bank
    has NO shipment row, so a shipment-based Lever D would miss it."""
    rows = [
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Costco", "KS Btr Qtr", "2026-06-01", 3_000_000),
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Costco", "KS Btr Qtr", "2025-06-01", 2_500_000),
    ]
    cols = ["pmaj", "sfmt", "brand", "pminor", "customer_name", "item_desc", "month", "pounds"]
    df = pd.DataFrame(rows, columns=cols)
    df["month"] = pd.to_datetime(df["month"]).dt.date
    df["item_key"] = df["item_desc"]
    return df


def _finance_enriched() -> pd.DataFrame:
    """Butter finance — Net Sales L3M $3.0M vs YAG $2.4M (+25%); Gross Profit
    $0.6M both sides → margin 20% current."""
    rows = [
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Costco", "Butter Stick", "2026-06-01", 3_000_000.0, 600_000.0),
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Costco", "Butter Stick", "2025-06-01", 2_400_000.0, 600_000.0),
    ]
    cols = ["pmaj", "sfmt", "brand", "pminor", "customer_name", "item_desc",
            "month", "net_sales", "gross_profit"]
    df = pd.DataFrame(rows, columns=cols)
    df["month"] = pd.to_datetime(df["month"]).dt.date
    df["item_key"] = df["item_desc"]
    return df


def _butter(**kw):
    cats = {c.row_id: c for c in build_business_health_categories(
        _orders(), _shipments(), PRIOR, **kw)}
    return cats["butter"]


# ── Builder wiring ───────────────────────────────────────────────────────────

def test_categories_shape_and_order():
    cats = build_business_health_categories(_orders(), _shipments(), PRIOR)
    assert [c.row_id for c in cats] == list(BH_CATEGORY_ROWS)
    assert [c.label for c in cats] == [BH_CATEGORY_LABELS[r] for r in BH_CATEGORY_ROWS]


def test_lever_a_gap_read():
    # Orders −29.5% vs Shipments +20% → shipments outpacing orders → draining.
    assert _butter().gap_flag == BH_GAP_DRAINING


def test_lever_b_decomposition_and_sowhat():
    butter = _butter(finance_enriched=_finance_enriched())
    d = butter.decomp["L3M"]
    assert d["volume"] == pytest.approx(0.20, rel=1e-3)      # shipped-lbs YoY
    assert d["net_sales"] == pytest.approx(0.25, rel=1e-3)   # finance net sales YoY
    assert d["price_mix"] == pytest.approx(0.05, rel=1e-3)   # residual 25 − 20
    # Volume (+20) dominates the +25 net move → bought (volume).
    assert butter.decomp_sowhat == BH_MOVE_BOUGHT
    # All three periods carry the decomposition keys.
    for p in ("L12M", "L6M", "L3M"):
        assert set(butter.decomp[p]) == {"volume", "price_mix", "net_sales"}


def test_lever_b_without_finance_is_blank():
    butter = _butter()
    assert butter.decomp["L3M"]["net_sales"] is None
    assert butter.decomp["L3M"]["price_mix"] is None
    assert butter.decomp_sowhat == ""


def test_lever_c_margin_and_reading():
    butter = _butter(finance_enriched=_finance_enriched())
    assert butter.margin_pct["L3M"] == pytest.approx(0.20, rel=1e-3)   # 0.6 / 3.0
    # Vol ▲ (+20%) and margin flat/▲ across periods → healthy growth.
    assert butter.vol_margin_reading == BH_VM_HEALTHY


def test_lever_d_concentration_order_based_captures_mt_food_bank():
    """Lever D ranks by ORDER-lbs Δ, so MT Food Bank (order-only, no shipment)
    is captured — the RCA fix — as the #1 decliner."""
    conc = _butter().concentration
    # Order deltas: Costco +1.0, Kroger +0.5, MT Food Bank −1.4, Baugh −0.8,
    # Golden State −0.6 → Σ|Δ| = 4.3M.
    assert conc.total_abs_m == pytest.approx(4.3, rel=1e-3)
    # Two growers (only two positive), three decliners.
    assert [g.label for g in conc.growers] == ["Costco × KS Btr Qtr", "Kroger × KRO Salted"]
    assert conc.growers[0].delta_m == pytest.approx(1.0, rel=1e-3)
    assert conc.growers[0].share == pytest.approx(1.0 / 4.3, rel=1e-3)
    top_decliner = conc.decliners[0]
    assert top_decliner.label == "MT Food Bank × DG Btr"      # captured, ranked #1
    assert top_decliner.kind == "decliner"
    assert top_decliner.delta_m == pytest.approx(-1.4, rel=1e-3)
    assert top_decliner.yoy_pct == pytest.approx(-1.0, rel=1e-3)   # 0 vs 1.4M → −100%
    assert [d.label for d in conc.decliners] == [
        "MT Food Bank × DG Btr", "Baugh × WhF Btr", "Golden State (McD) × DG Btr"]
    # Ranking is by absolute pounds — never YoY% (MT Food Bank's −100% doesn't
    # jump it above a bigger-pound line; it wins here on 1.4M lbs, not the %).
    assert all(g.kind == "grower" for g in conc.growers)


def test_lever_d_ships_only_would_miss_mt_food_bank():
    """Regression guard: a shipment-based rank misses the order-only account
    (there is no MT Food Bank shipment row) — hence Lever D uses orders."""
    from data_sources.demand_plan_comparison import (
        _category_concentration, COMPARISON_TEMPLATE, _last_n_months, _shift_year_back,
    )
    tby = {r.row_id: r for r in COMPARISON_TEMPLATE}
    cur = _last_n_months(PRIOR, 3)
    yag = {_shift_year_back(m) for m in cur}
    ship_conc = _category_concentration(_shipments(), "butter", cur, yag, tby)
    labels = [s.label for s in (*ship_conc.growers, *ship_conc.decliners)]
    assert not any("MT Food Bank" in lbl for lbl in labels)


def test_empty_inputs_degrade():
    cats = build_business_health_categories(None, None, PRIOR)
    assert [c.row_id for c in cats] == list(BH_CATEGORY_ROWS)
    for c in cats:
        assert c.gap_flag == "" and c.decomp_sowhat == "" and c.vol_margin_reading == ""
        assert c.concentration.growers == () and c.concentration.decliners == ()


# ── Finance enrichment (feeds Levers B & C) ──────────────────────────────────

def test_enrich_finance_df_actual_only_and_pdh_dims():
    """enrich_finance_df keeps only Actual rows and attaches PDH dims by Item No."""
    pdh = pd.DataFrame({
        "Item No": ["310180"],
        "Item Description": ["DG Btr Qtr 1Lb 30cs"],
        "Portfolio Major": ["Butter"],
        "Supply Format": ["Sticks"],
        "Portfolio Minor": ["Packaged Butter"],
        "Brand": ["Branded"],
    })
    finance = pd.DataFrame({
        "Budget/Actual": ["Actual", "Budget", "Actual"],
        "Item No.": ["310180", "310180", "310180"],
        "Item Description": ["DG Btr Qtr 1Lb 30cs"] * 3,
        "Customer": ["Costco", "Costco", "Kroger"],
        "GLMonth": [45778, 45778, 45778],   # 2025-05-01 (excel serial)
        "Net Sales": ["1,000.50", "9999", "500"],
        "Gross Profit": ["400.25", "8888", "100"],
    })
    out = enrich_finance_df(finance, pdh)
    assert list(out.columns) == [
        "item_key", "item_desc", "customer_name", "month",
        "net_sales", "gross_profit", "pmaj", "sfmt", "pminor", "brand",
    ]
    assert len(out) == 2                       # Budget row dropped
    assert out["net_sales"].sum() == pytest.approx(1500.50)   # comma parsed
    assert out["gross_profit"].sum() == pytest.approx(500.25)
    assert set(out["pmaj"]) == {"Butter"}      # PDH dims attached via Item No.
    assert out["month"].iloc[0] == date(2025, 5, 1)


def test_enrich_finance_df_missing_metric_column_degrades_to_zero():
    """A finance metric whose source column is absent → all-zeros, not a crash."""
    finance = pd.DataFrame({
        "Budget/Actual": ["Actual"],
        "Item No.": ["310180"],
        "Customer": ["Costco"],
        "GLMonth": [45778],
        "Net Sales": ["1000"],
        # No "Gross Profit" column present.
    })
    out = enrich_finance_df(finance, None)
    assert (out["gross_profit"] == 0.0).all()
    assert out["net_sales"].sum() == pytest.approx(1000.0)


def test_enrich_finance_df_empty():
    assert enrich_finance_df(None, None).empty
    assert list(enrich_finance_df(None, None).columns) == [
        "item_key", "item_desc", "customer_name", "month",
        "net_sales", "gross_profit", "pmaj", "sfmt", "pminor", "brand",
    ]
