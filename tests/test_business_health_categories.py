"""Unit tests for build_business_health_categories (per-category YoY + drivers)."""

from datetime import date

import pandas as pd
import pytest

from data_sources.demand_plan_comparison import (
    BH_CATEGORY_ROWS,
    BH_CATEGORY_LABELS,
    BH_FLAG_RISING,
    build_business_health_categories,
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


def test_categories_shape_and_order():
    cats = build_business_health_categories(_orders(), _shipments(), PRIOR)
    assert [c.row_id for c in cats] == list(BH_CATEGORY_ROWS)
    assert [c.label for c in cats] == [BH_CATEGORY_LABELS[r] for r in BH_CATEGORY_ROWS]
    for c in cats:
        assert set(c.order_series) == {"L3M", "L6M", "L12M"}
        assert set(c.ship_series) == {"L3M", "L6M", "L12M"}


def test_butter_flag_and_customer_sku_drivers():
    cats = {c.row_id: c for c in build_business_health_categories(_orders(), _shipments(), PRIOR)}
    butter = cats["butter"]
    # L3M orders 4.0M vs YAG 3.5M (+14%), L12M dragged down by the 2024-08 base
    # → recent momentum accelerating → Rising.
    assert butter.flag == BH_FLAG_RISING
    assert butter.order_series["L3M"]["yoy"] == pytest.approx((4.0 - 3.5) / 3.5, rel=1e-3)
    # Drivers are Customer × SKU pairs, ranked by L3M lbs Δ (Rising → top grower).
    assert len(butter.drivers) == 2
    top = butter.drivers[0]
    assert (top.customer, top.sku) == ("Costco", "Butter Stick")
    assert top.delta_m == pytest.approx(1.0)          # 3.0M − 2.0M
    assert butter.drivers[1].delta_m == pytest.approx(-0.5)   # WinCo Bulk 1.0 − 1.5


def test_shipments_series_present_for_butter():
    cats = {c.row_id: c for c in build_business_health_categories(_orders(), _shipments(), PRIOR)}
    # Butter shipped only in the L3M current window (2.8M), no year-ago → YoY None.
    assert cats["butter"].ship_series["L3M"]["vol"] == pytest.approx(2.8)
    assert cats["butter"].ship_series["L3M"]["yoy"] is None


def test_empty_inputs_degrade():
    cats = build_business_health_categories(None, None, PRIOR)
    assert [c.row_id for c in cats] == list(BH_CATEGORY_ROWS)
    assert all(c.drivers == () for c in cats)
