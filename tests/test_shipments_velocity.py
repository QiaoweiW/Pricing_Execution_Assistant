"""Unit tests for data_sources.shipments_velocity (pure, no Fabric/Streamlit)."""

from datetime import date

import pandas as pd
import pytest

import data_sources.shipments_velocity as sv


# ── Column resolution ────────────────────────────────────────────────────────

def test_resolve_columns_all_present_and_case_insensitive():
    cols = ["order date", "ORDERED QTY LBS", "Shipped Qty lbs", "Portfolio",
            "productdesc", "Customer", "Business Unit", "Product Format", "junk"]
    r = sv.resolve_columns(cols)
    assert r.actual(sv.COL_ORDER_DATE) == "order date"
    assert r.actual(sv.COL_ORDERED_LBS) == "ORDERED QTY LBS"
    assert r.actual(sv.COL_SHIPPED_LBS) == "Shipped Qty lbs"
    assert r.actual(sv.COL_PORTFOLIO) == "Portfolio"
    assert r.actual(sv.COL_BUSINESS_UNIT) == "Business Unit"
    assert r.missing_required() == []


def test_resolve_columns_missing_required():
    r = sv.resolve_columns(["Order Date", "Ordered Lbs"])  # no shipped column
    assert sv.COL_SHIPPED_LBS in r.missing_required()
    # Optional filter dims simply resolve to None (no crash).
    assert r.actual(sv.COL_PORTFOLIO) is None


def test_resolve_columns_select_sql_quotes_and_aliases():
    r = sv.resolve_columns(["Order Date", "Ordered Qty lbs", "Shipped Qty lbs"])
    sql = r.select_sql()
    assert '"Order Date" AS order_date' in sql
    assert '"Ordered Qty lbs" AS ordered_lbs' in sql
    assert '"Shipped Qty lbs" AS shipped_lbs' in sql
    # No optional dims present → not in the select list.
    assert "portfolio" not in sql


# ── Weekly velocity ──────────────────────────────────────────────────────────

def _tidy() -> pd.DataFrame:
    """Canonical-column shipments frame spanning two Mon-anchored weeks."""
    return pd.DataFrame({
        sv.COL_ORDER_DATE: pd.to_datetime([
            "2026-07-06", "2026-07-08", "2026-07-13", "2026-07-08"]),
        sv.COL_ORDERED_LBS: [100.0, 50.0, 200.0, 999.0],
        sv.COL_SHIPPED_LBS: [90.0, 40.0, 180.0, 999.0],
        sv.COL_PORTFOLIO: ["Butter", "Cheese", "Butter", "Butter"],
        sv.COL_BUSINESS_UNIT: ["B2C", "B2C", "B2C", "B2B"],
    })


def test_weekly_velocity_business_unit_filter_and_weeks():
    res = sv.build_weekly_velocity(_tidy(), business_units=["B2C"])
    # B2B row dropped; two weeks.
    assert list(res.weekly[sv.WEEK_START].dt.strftime("%Y-%m-%d")) == \
        ["2026-07-06", "2026-07-13"]
    wk1, wk2 = res.weekly.iloc[0], res.weekly.iloc[1]
    assert wk1[sv.COL_ORDERED_LBS] == pytest.approx(150.0)   # 100 + 50
    assert wk1[sv.COL_SHIPPED_LBS] == pytest.approx(130.0)   # 90 + 40
    assert wk2[sv.COL_ORDERED_LBS] == pytest.approx(200.0)
    assert res.total_ordered == pytest.approx(350.0)
    assert res.total_shipped == pytest.approx(310.0)


def test_weekly_velocity_portfolio_filter():
    res = sv.build_weekly_velocity(_tidy(), portfolios=["Butter"], business_units=["B2C"])
    assert res.total_ordered == pytest.approx(300.0)   # 100 (wk1) + 200 (wk2)
    assert res.total_shipped == pytest.approx(270.0)


def test_weekly_velocity_date_range():
    res = sv.build_weekly_velocity(
        _tidy(), business_units=["B2C"],
        date_range=(date(2026, 7, 6), date(2026, 7, 10)))
    assert len(res.weekly) == 1                        # only the first week
    assert res.total_ordered == pytest.approx(150.0)


def test_weekly_velocity_empty_input():
    res = sv.build_weekly_velocity(pd.DataFrame())
    assert res.weekly.empty
    assert (res.total_ordered, res.total_shipped) == (0.0, 0.0)


def test_distinct_values():
    assert sv.distinct_values(_tidy(), sv.COL_PORTFOLIO) == ["Butter", "Cheese"]
    assert sv.distinct_values(_tidy(), sv.COL_PRODUCT_FORMAT) == []  # column absent


def test_weekly_velocity_no_shipto_column():
    # No ship-to column → no velocity series.
    res = sv.build_weekly_velocity(_tidy(), business_units=["B2C"])
    assert res.has_velocity is False
    assert sv.SHIPPED_VELOCITY not in res.weekly.columns


# ── Shipped Velocity + Portfolio Minor ───────────────────────────────────────

def _tidy_shipto() -> pd.DataFrame:
    """Two Mon-weeks with a ship-to column + Portfolio Minor (all B2C)."""
    return pd.DataFrame({
        sv.COL_ORDER_DATE: pd.to_datetime([
            "2026-07-06", "2026-07-08", "2026-07-08", "2026-07-13"]),
        sv.COL_ORDERED_LBS: [100.0, 50.0, 20.0, 220.0],
        sv.COL_SHIPPED_LBS: [90.0, 40.0, 10.0, 200.0],
        sv.COL_BUSINESS_UNIT: ["B2C", "B2C", "B2C", "B2C"],
        sv.COL_PRODUCT_MINOR: ["Cottage Cheese", "Sour Cream", "Cottage Cheese",
                               "Cottage Cheese"],
        sv.COL_SHIP_TO: ["A", "B", "A", "C"],
    })


def test_shipped_velocity_per_week():
    res = sv.build_weekly_velocity(_tidy_shipto())
    assert res.has_velocity is True
    wk1, wk2 = res.weekly.iloc[0], res.weekly.iloc[1]
    # Week 1: shipped 90+40+10=140 over ship-tos {A,B}=2 → 70 lbs/ship-to.
    assert wk1[sv.SHIP_TO_COUNT] == 2
    assert wk1[sv.SHIPPED_VELOCITY] == pytest.approx(70.0)
    # Week 2: shipped 200 over {C}=1 → 200.
    assert wk2[sv.SHIP_TO_COUNT] == 1
    assert wk2[sv.SHIPPED_VELOCITY] == pytest.approx(200.0)


def test_product_minor_filter_reacts_in_velocity():
    res = sv.build_weekly_velocity(
        _tidy_shipto(), product_minors=["Cottage Cheese"])
    # Sour Cream row dropped → week 1 shipped 90+10=100 over {A}=1 → velocity 100.
    wk1 = res.weekly.iloc[0]
    assert wk1[sv.COL_SHIPPED_LBS] == pytest.approx(100.0)
    assert wk1[sv.SHIP_TO_COUNT] == 1
    assert wk1[sv.SHIPPED_VELOCITY] == pytest.approx(100.0)
