"""Unit tests for build_business_health_sku (per-SKU trailing-window orders)."""

from datetime import date

import pandas as pd
import pytest

from data_sources.demand_plan_comparison import (
    BH_LEVEL_LABELS,
    BH_YOY_LABELS,
    BH_SKU_COLUMNS,
    build_business_health_sku,
)


PRIOR = date(2026, 6, 1)   # L3M = Apr–Jun 2026; YAG L3M = Apr–Jun 2025


def _orders() -> pd.DataFrame:
    """Enriched-orders-shaped frame (item_key/item_desc/month/pounds/dims)."""
    return pd.DataFrame({
        "item_key":  ["A", "A", "B"],
        "item_desc": ["Cottage 16oz", "Cottage 16oz", "Sour 16oz"],
        "month":     [date(2026, 6, 1), date(2025, 6, 1), date(2026, 5, 1)],
        "pounds":    [3_000_000.0, 2_000_000.0, 1_000_000.0],
        "pmaj":      ["Cultured", "Cultured", "Cultured"],
        "pminor":    ["Cottage Cheese", "Cottage Cheese", "Sour Cream"],
        "brand":     ["Branded", "Branded", "Private"],
        "sfmt":      ["Large Tub", "Large Tub", "Small Tub"],
    })


def test_sku_windows_yoy_and_sort():
    df = build_business_health_sku(_orders(), PRIOR)
    assert list(df.columns) == list(BH_SKU_COLUMNS)
    # Sorted by L12M Orders desc → A (3.0M) before B (1.0M).
    assert list(df["SKU"]) == ["Cottage 16oz", "Sour 16oz"]

    a = df.iloc[0]
    l3m_cur, l3m_yag = BH_LEVEL_LABELS["L3M"]
    assert a[l3m_cur] == pytest.approx(3.0)     # Jun-2026 → 3.0M
    assert a[l3m_yag] == pytest.approx(2.0)     # Jun-2025 (YAG) → 2.0M
    assert a[BH_YOY_LABELS["L3M"]] == pytest.approx(0.5)   # (3-2)/2
    assert a[BH_LEVEL_LABELS["L12M"][0]] == pytest.approx(3.0)

    b = df.iloc[1]
    assert b[l3m_cur] == pytest.approx(1.0)
    assert b[l3m_yag] == pytest.approx(0.0)
    # No year-ago base → YoY is NaN (not a divide-by-zero explosion).
    assert pd.isna(b[BH_YOY_LABELS["L3M"]])


def test_sku_dim_filter():
    df = build_business_health_sku(
        _orders(), PRIOR, dim_filter={"pminor": {"Cottage Cheese"}})
    assert list(df["SKU"]) == ["Cottage 16oz"]


def test_sku_empty_input():
    assert build_business_health_sku(pd.DataFrame(), PRIOR).empty
    assert build_business_health_sku(None, PRIOR).empty
