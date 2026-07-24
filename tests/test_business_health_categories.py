"""Unit tests for build_business_health_categories — per-category YoY (Order /
Shipment / Net Sales), top-5 growers + decliners, and the finance enrichment."""

from datetime import date

import pandas as pd
import pytest

from data_sources.demand_plan_comparison import (
    BH_CATEGORY_ROWS,
    BH_CATEGORY_LABELS,
    BH_FLAG_RISING,
    build_business_health,
    build_business_health_categories,
    enrich_finance_df,
)


PRIOR = date(2026, 6, 1)   # L3M = Apr–Jun 2026; YAG L3M = Apr–Jun 2025


def _orders() -> pd.DataFrame:
    """Enriched orders: Butter (rising, two Customer×SKU movers) + a Cultured
    Cottage Cheese row so that category is non-empty too."""
    # Butter is scoped to Portfolio Minor "Packaged Butter" (planner rule).
    rows = [
        # Butter — Costco×Stick grows (+1.0M L3M), WinCo×Bulk declines (-0.5M).
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Costco", "Butter Stick", "2026-06-01", 3_000_000),
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Costco", "Butter Stick", "2025-06-01", 2_000_000),
        ("Butter", "Bulk",   "Branded", "Packaged Butter", "WinCo",  "Butter Bulk",  "2026-06-01", 1_000_000),
        ("Butter", "Bulk",   "Branded", "Packaged Butter", "WinCo",  "Butter Bulk",  "2025-06-01", 1_500_000),
        # L12M-only year-ago weight so L12M YoY < L3M YoY → flag = Rising.
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Costco", "Butter Stick", "2024-08-01", 2_000_000),
        # Cultured Cottage Cheese (keeps that category non-empty).
        ("Cultured", "Large Tub", "Branded", "Cottage Cheese", "Kroger", "CC 16oz", "2026-05-01", 500_000),
    ]
    cols = ["pmaj", "sfmt", "brand", "pminor", "customer_name", "item_desc", "month", "pounds"]
    df = pd.DataFrame(rows, columns=cols)
    df["month"] = pd.to_datetime(df["month"]).dt.date
    df["item_key"] = df["item_desc"]
    return df


def _shipments() -> pd.DataFrame:
    df = pd.DataFrame([
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Costco", "Butter Stick", "2026-06-01", 2_800_000),
    ], columns=["pmaj", "sfmt", "brand", "pminor", "customer_name", "item_desc", "month", "pounds"])
    df["month"] = pd.to_datetime(df["month"]).dt.date
    df["item_key"] = df["item_desc"]
    return df


def _finance_enriched() -> pd.DataFrame:
    """Enriched Finance frame for Butter — Net Sales L3M $3.0M vs YAG $2.4M
    (+25%); Gross Profit L3M $1.0M vs YAG $1.25M (−20%)."""
    rows = [
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Costco", "Butter Stick", "2026-06-01", 3_000_000.0, 1_000_000.0),
        ("Butter", "Sticks", "Branded", "Packaged Butter", "Costco", "Butter Stick", "2025-06-01", 2_400_000.0, 1_250_000.0),
    ]
    cols = ["pmaj", "sfmt", "brand", "pminor", "customer_name", "item_desc",
            "month", "net_sales", "gross_profit"]
    df = pd.DataFrame(rows, columns=cols)
    df["month"] = pd.to_datetime(df["month"]).dt.date
    df["item_key"] = df["item_desc"]
    return df


def test_categories_shape_and_order():
    cats = build_business_health_categories(_orders(), _shipments(), PRIOR)
    assert [c.row_id for c in cats] == list(BH_CATEGORY_ROWS)
    assert [c.label for c in cats] == [BH_CATEGORY_LABELS[r] for r in BH_CATEGORY_ROWS]
    for c in cats:
        assert set(c.order_series) == {"L3M", "L6M", "L12M"}
        assert set(c.ship_series) == {"L3M", "L6M", "L12M"}
        assert c.finance_series == {}      # no finance frame supplied → empty


def test_butter_flag_and_grower_decliner_drivers():
    cats = {c.row_id: c for c in build_business_health_categories(_orders(), _shipments(), PRIOR)}
    butter = cats["butter"]
    # L3M orders 4.0M vs YAG 3.5M (+14%), L12M dragged down by the 2024-08 base
    # → recent momentum accelerating → Rising.
    assert butter.flag == BH_FLAG_RISING
    assert butter.order_series["L3M"]["yoy"] == pytest.approx((4.0 - 3.5) / 3.5, rel=1e-3)
    # Growers = biggest positive Δ; decliners = biggest negative Δ — BOTH shown.
    assert len(butter.growers) == 1 and len(butter.decliners) == 1
    grow = butter.growers[0]
    assert (grow.customer, grow.sku) == ("Costco", "Butter Stick")
    assert grow.delta_m == pytest.approx(1.0)                 # 3.0M − 2.0M
    assert grow.yoy_pct == pytest.approx((3.0 - 2.0) / 2.0)   # +50%
    dec = butter.decliners[0]
    assert (dec.customer, dec.sku) == ("WinCo", "Butter Bulk")
    assert dec.delta_m == pytest.approx(-0.5)                 # 1.0M − 1.5M
    assert dec.yoy_pct == pytest.approx((1.0 - 1.5) / 1.5)    # −33.3%


def test_finance_series_per_category_and_total():
    fin = _finance_enriched()
    cats = {c.row_id: c
            for c in build_business_health_categories(
                _orders(), _shipments(), PRIOR, finance_enriched=fin)}
    butter = cats["butter"]
    # Both finance lines present per category, keyed by line name.
    assert set(butter.finance_series) == {"Net Sales", "Gross Profit"}
    # Net Sales L3M $3.0M vs YAG $2.4M → +25%.
    assert butter.finance_series["Net Sales"]["L3M"]["yoy"] == pytest.approx((3.0 - 2.4) / 2.4, rel=1e-3)
    # Gross Profit L3M $1.0M vs YAG $1.25M → −20%.
    assert butter.finance_series["Gross Profit"]["L3M"]["yoy"] == pytest.approx((1.0 - 1.25) / 1.25, rel=1e-3)
    # Total-B2C chart also carries BOTH finance series when finance supplied.
    result = build_business_health(_orders(), _shipments(), PRIOR, finance_enriched=fin)
    assert "Net Sales" in result.chart_series and "Gross Profit" in result.chart_series
    assert result.chart_series["Gross Profit"]["L3M"]["yoy"] == pytest.approx((1.0 - 1.25) / 1.25, rel=1e-3)


def test_build_business_health_without_finance_has_no_finance_lines():
    result = build_business_health(_orders(), _shipments(), PRIOR)
    assert "Net Sales" not in result.chart_series
    assert "Gross Profit" not in result.chart_series


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


def test_empty_inputs_degrade():
    cats = build_business_health_categories(None, None, PRIOR)
    assert [c.row_id for c in cats] == list(BH_CATEGORY_ROWS)
    assert all(c.growers == () and c.decliners == () for c in cats)


def test_enrich_finance_df_empty():
    assert enrich_finance_df(None, None).empty
    assert list(enrich_finance_df(None, None).columns) == [
        "item_key", "item_desc", "customer_name", "month",
        "net_sales", "gross_profit", "pmaj", "sfmt", "pminor", "brand",
    ]
