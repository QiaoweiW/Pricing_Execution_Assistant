"""Unit tests for the Demand MOM Summary builder + the shared pivot assembler.

Covers the behaviour the Demand Planner Analytics page relies on:

* ``build_demand_mom_pivot`` stitches IBP-Shipments **actuals** (actual
  window) and tracker **forecast** (selected cycle, forecast window) onto a
  single month axis under an ``Actual`` / ``Base Plan`` / ``R&O`` hierarchy.
* The Cycle filter and the disjoint actual/forecast windows are honoured.
* The "not captured" reconciliation log flags tracker items missing from the
  pivot (here: an item with no PDH mapping).
* ``sku_detail_for`` returns the item-level slice behind a pivot row.
* ``validate_mom_filters`` rejects overlapping windows.
* The refactored classic ``build_demand_pivot`` still produces a sane pivot
  (regression guard for the shared-assembler extraction).

All fixtures are synthetic DataFrames — no Fabric / Streamlit session needed.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from data_sources.demand_plan_comparison import (
    SERIES_ACTUAL,
    DemandMomFilters,
    build_demand_mom_pivot,
    build_item_dim_frame_cascade,
    list_mom_filter_values,
    validate_mom_filters,
)
from data_sources.demand_summary import (
    FORECAST_BASE_PLAN,
    FORECAST_R_AND_O,
    PMAJ_BLANK_LABEL,
    DemandPivotFilters,
    build_demand_pivot,
    build_supply_format_lookup,
)


# ── Synthetic sources ───────────────────────────────────────────────────────

def _pdh() -> pd.DataFrame:
    """PDH dims — item 300 is deliberately absent (→ unmapped)."""
    return pd.DataFrame({
        "Item No": [100, 200],
        "Item Description": ["DG Milk Gallon", "Sour Cream Tub"],
        "Portfolio Major": ["ESL", "Cultured"],
        "Supply Format": ["Large Carton", "Small Tub"],
        "Portfolio Minor": ["Milk", "Cultured"],
    })


def _ro_item_master() -> pd.DataFrame:
    """RO_Item_Master fallback — classifies item 300 (absent from PDH).

    Also carries a deliberately-wrong mapping for item 100 to prove PDH
    wins the cascade.  Uses RO_Item_Master's native column spellings.
    """
    return pd.DataFrame({
        "Item #": [100, 300],
        "Item Desc": ["WRONG DESC", "Mystery SKU"],
        "Brand Category": ["Private", "Private"],
        "Portfolio Major": ["WRONG PMAJ", "Cultured"],
        "Supply Format": ["WRONG SFMT", "Pail"],
        "Portfolio Minor": ["WRONG", "Cultured"],
    })


def _tracker() -> pd.DataFrame:
    """Tracker forecast rows across two cycles + in/out-of-window months."""
    return pd.DataFrame({
        "Start of Month": [
            "2026-06-01", "2026-07-01", "2026-06-01",  # C3 in-window
            "2026-06-01",                               # C3 unmapped item 300
            "2026-06-01",                               # C2 (wrong cycle)
            "2026-09-01",                               # C3 out-of-window
        ],
        "Item": [100, 100, 200, 300, 100, 100],
        "Item Description": ["DG Milk Gallon", "DG Milk Gallon", "Sour Cream Tub",
                             "Mystery SKU", "DG Milk Gallon", "DG Milk Gallon"],
        "Party Site Number": [10, 10, 20, 30, 10, 10],
        "Demand Plan Pounds": [2_000_000, 1_000_000, 500_000,
                               300_000, 9_999_999, 7_000_000],
        "Forecast Type": ["Base Plan", "R&O", "Base Plan",
                          "Base Plan", "Base Plan", "Base Plan"],
        "Business Unit": ["B2C"] * 6,
        "Cycle": ["C3", "C3", "C3", "C3", "C2", "C3"],
    })


def _ibp() -> pd.DataFrame:
    """IBP Shipments actuals — Month is datetime64 (as DuckDB returns it)."""
    return pd.DataFrame({
        "Item No": [100, 100, 200],
        "Customer No": [1, 1, 2],
        "Customer Name": ["Acme", "Acme", "Beta"],
        "Month": pd.to_datetime(["2026-04-01", "2026-05-01", "2026-05-01"]),
        "Shipped Qty lbs": [1_500_000, 1_600_000, 400_000],
    })


def _filters(**overrides) -> DemandMomFilters:
    base = dict(
        cycle="C3",
        actual_start=dt.date(2026, 4, 1),
        actual_end=dt.date(2026, 5, 1),
        forecast_start=dt.date(2026, 6, 1),
        forecast_end=dt.date(2026, 7, 1),
        portfolio_majors=None,
        supply_formats=None,
    )
    base.update(overrides)
    return DemandMomFilters(**base)


# ── build_demand_mom_pivot ──────────────────────────────────────────────────

def test_month_axis_is_actual_then_forecast():
    res = build_demand_mom_pivot(_tracker(), _ibp(), _pdh(), _filters())
    assert res.month_columns == ("2026-04", "2026-05", "2026-06", "2026-07")
    assert res.actual_month_columns == ("2026-04", "2026-05")
    assert res.forecast_month_columns == ("2026-06", "2026-07")
    # Out-of-window (2026-09) and wrong-cycle rows never reach the axis.
    assert "2026-09" not in res.month_columns


def test_actual_branch_only_in_actual_months():
    """Stitch invariant: Actual populates only actual months; forecast only forecast."""
    res = build_demand_mom_pivot(_tracker(), _ibp(), _pdh(), _filters())
    cl = res.chart_long
    series = set(cl["Forecast Type"].astype(str))
    assert {SERIES_ACTUAL, FORECAST_BASE_PLAN, FORECAST_R_AND_O} <= series

    def _pounds(series_name, month):
        m = (cl["Forecast Type"].astype(str) == series_name) & (
            cl["Month"] == dt.date.fromisoformat(month + "-01"))
        return float(cl.loc[m, "Pounds_M"].sum())

    # Actuals land in Apr/May, nothing in Jun/Jul.
    assert _pounds(SERIES_ACTUAL, "2026-04") == 1.5
    assert _pounds(SERIES_ACTUAL, "2026-06") == 0.0
    # Forecast lands in Jun/Jul, nothing in the actual months.
    assert _pounds(FORECAST_BASE_PLAN, "2026-04") == 0.0
    assert _pounds(FORECAST_R_AND_O, "2026-07") == 1.0
    # Jun Base Plan = item100 (2.0) + item200 (0.5) + unmapped item300 (0.3).
    assert _pounds(FORECAST_BASE_PLAN, "2026-06") == 2.8


def test_cycle_filter_excludes_other_cycles():
    """The C2 row (9,999,999 lbs) must never contribute under a C3 selection."""
    res = build_demand_mom_pivot(_tracker(), _ibp(), _pdh(), _filters(cycle="C3"))
    jun_base = res.chart_long[
        (res.chart_long["Forecast Type"].astype(str) == FORECAST_BASE_PLAN)
        & (res.chart_long["Month"] == dt.date(2026, 6, 1))
    ]["Pounds_M"].sum()
    assert jun_base == 2.8  # 10.0-ish would mean the C2 row leaked in


def test_not_captured_flags_unmapped_item():
    res = build_demand_mom_pivot(_tracker(), _ibp(), _pdh(), _filters())
    nc = res.not_captured_items
    assert not nc.empty
    items = set(nc["Item"].astype(str))
    assert "300" in items          # unmapped tracker item is surfaced
    assert "100" not in items      # fully-captured items are not
    assert "200" not in items


def test_sku_detail_for_leaf():
    res = build_demand_mom_pivot(_tracker(), _ibp(), _pdh(), _filters())
    sku = res.sku_detail_for("ESL", FORECAST_BASE_PLAN, "Large Carton")
    assert not sku.empty
    assert "100" in set(sku["Item"].astype(str))
    # Item 100 Base Plan in Jun = 2.0 M.
    row = sku.loc[sku["Item"].astype(str) == "100"].iloc[0]
    assert float(row["2026-06"]) == 2.0


def test_portfolio_filter_narrows_and_reports_excluded():
    res = build_demand_mom_pivot(
        _tracker(), _ibp(), _pdh(), _filters(portfolio_majors=("ESL",)),
    )
    # Only ESL rows survive → Cultured item 200 is excluded, and reported.
    pmajs = set(res.pivot["_pmaj"].astype(str)) - {""}
    assert pmajs <= {"ESL"}
    assert "200" in set(res.not_captured_items["Item"].astype(str))


def test_forecast_only_when_no_actuals():
    """A None IBP frame yields a forecast-only view (no Actual series)."""
    res = build_demand_mom_pivot(_tracker(), None, _pdh(), _filters())
    assert res.has_actuals is False
    assert res.actual_month_columns == ()
    assert SERIES_ACTUAL not in set(res.chart_long["Forecast Type"].astype(str))


def test_empty_when_nothing_in_window():
    res = build_demand_mom_pivot(
        _tracker(), _ibp(), _pdh(),
        _filters(forecast_start=dt.date(2030, 1, 1),
                 forecast_end=dt.date(2030, 2, 1),
                 actual_start=dt.date(2030, 3, 1),
                 actual_end=dt.date(2030, 4, 1)),
    )
    assert res.pivot.empty


# ── list_mom_filter_values ──────────────────────────────────────────────────

def test_list_mom_filter_values_scopes_to_tracker_and_flags_blank():
    opts = list_mom_filter_values(_tracker(), _pdh())
    assert "ESL" in opts["portfolio_majors"]
    assert "Cultured" in opts["portfolio_majors"]
    # Item 300 is in the tracker but unmapped → (blank) offered, listed last.
    assert opts["portfolio_majors"][-1] == PMAJ_BLANK_LABEL


def test_list_mom_filter_values_gains_recovered_pmaj_from_item_master():
    """With RO_Item_Master, item 300's recovered PMaj shows as an option."""
    opts = list_mom_filter_values(_tracker(), _pdh(), _ro_item_master())
    assert "Cultured" in opts["portfolio_majors"]
    # Item 300 is now classified → the (blank) option should not be forced
    # by that item any longer (every tracker item now maps).
    assert PMAJ_BLANK_LABEL not in opts["portfolio_majors"]


# ── RO_Item_Master dimension fallback (PDH → RO_Item_Master cascade) ─────────

def test_cascade_pdh_wins_and_item_master_fills_gaps():
    casc = build_item_dim_frame_cascade(_pdh(), _ro_item_master())
    row100 = casc.loc[casc["__item_key"] == "100"].iloc[0]
    # Item 100 is in BOTH → PDH wins over RO_Item_Master's wrong values.
    assert row100["pmaj"] == "ESL"
    assert row100["sfmt"] == "Large Carton"
    # Item 300 is only in RO_Item_Master → recovered from the fallback.
    row300 = casc.loc[casc["__item_key"] == "300"].iloc[0]
    assert row300["pmaj"] == "Cultured"
    assert row300["sfmt"] == "Pail"


def test_cascade_none_fallback_is_pdh_only():
    """A ``None`` fallback degrades to today's PDH-only behaviour."""
    casc = build_item_dim_frame_cascade(_pdh(), None)
    assert set(casc["__item_key"]) == {"100", "200"}  # no item 300


def test_item_master_fallback_recovers_unmapped_item():
    """Passing RO_Item_Master captures item 300 that PDH alone dropped."""
    res = build_demand_mom_pivot(
        _tracker(), _ibp(), _pdh(), _filters(),
        item_master_df=_ro_item_master(),
    )
    # No longer in the not-captured log …
    assert "300" not in set(res.not_captured_items["Item"].astype(str))
    # … and its forecast pounds now land under Cultured / Pail (Jun Base Plan).
    sku = res.sku_detail_for("Cultured", FORECAST_BASE_PLAN, "Pail")
    assert "300" in set(sku["Item"].astype(str))
    assert float(sku.loc[sku["Item"].astype(str) == "300"].iloc[0]["2026-06"]) == 0.3


# ── validate_mom_filters ────────────────────────────────────────────────────

def test_validate_rejects_overlap():
    errs = validate_mom_filters(_filters(actual_end=dt.date(2026, 6, 1)))
    assert any("overlap" in e.lower() for e in errs)


def test_validate_accepts_disjoint():
    assert validate_mom_filters(_filters()) == []


def test_validate_rejects_inverted_range():
    errs = validate_mom_filters(
        _filters(forecast_start=dt.date(2026, 8, 1),
                 forecast_end=dt.date(2026, 6, 1)),
    )
    assert any("after the end month" in e for e in errs)


# ── Regression: classic build_demand_pivot still works post-refactor ─────────

def test_classic_build_demand_pivot_regression():
    demand = pd.DataFrame({
        "Start of Month": ["2026-06-01", "2026-06-01", "2026-07-01"],
        "Item": [100, 200, 100],
        "Portfolio Major": ["ESL", "Cultured", "ESL"],
        "Forecast Type": ["Base Plan", "R&O", "Base Plan"],
        "Demand Plan Pounds": [1_000_000, 2_000_000, 3_000_000],
    })
    lookup = build_supply_format_lookup(_pdh(), None)
    res = build_demand_pivot(
        demand, DemandPivotFilters(), supply_format_lookup=lookup,
    )
    assert not res.pivot.empty
    assert res.month_columns == ("2026-06", "2026-07")
    assert "Grand Total" in res.pivot["Row Label"].str.strip().tolist()
    # Grand total across all months = 6.0 M.
    grand = res.pivot.loc[
        res.pivot["Row Label"].str.strip() == "Grand Total"
    ].iloc[0]
    assert float(grand["Total"]) == 6.0
