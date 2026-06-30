"""Unit tests for the Plan Lift Analysis data layer.

Synthetic fixtures only — exercises the pure-pandas logic in
:mod:`data_sources.plan_lift` (the pre-agg builder + the YoY-Lift metric)
without touching Microsoft Fabric.  Coverage, in order:

1. ``build_plan_lift_base`` — outer-merge of shipments + plan to the
   (month, item, corporate-group) grain, dim attach, and the single
   Corporate Group source of truth (``dp_dimcustomernames`` for BOTH
   sides: shipments via Customer No, plan via party_site_code).
2. ``compute_yoy_lift`` — the contract that matters most:
       * ratio of SUMS, never the mean of per-item lifts;
       * numerator = plan if plan > 0 else shipments;
       * prior_year = the numerator self-shifted +12 months;
       * "n.m." (NaN) when prior_year <= 0;
       * volume-floor suppression;
       * empty filter selection = no constraint.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from data_sources.plan_lift import (
    CORP_GROUP_UNMAPPED,
    DIM_UNKNOWN,
    IRI_FILTER_COLUMNS,
    build_plan_lift_base,
    compute_iri_unit_lift,
    compute_yoy_lift,
    iri_file_label,
    list_iri_filter_options,
    list_minor_products,
    list_portfolios,
    list_slicer_options_for_portfolio,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _shipments() -> pd.DataFrame:
    """IBP Shipments slim shape: Item No, Customer No, Month, Shipped Qty lbs."""
    rows = []
    # 12 months of history (2025) for item 100 and item 200, two customers.
    for m in range(1, 13):
        month = f"2025-{m:02d}-01"
        rows.append(("100", "6514", month, 100.0))   # cust 6514 -> URM
        rows.append(("200", "9999", month, 50.0))    # cust 9999 -> COSTCO
    # Current-year actuals (Jan-Jun 2026) before they switch to plan.
    for m in range(1, 7):
        month = f"2026-{m:02d}-01"
        rows.append(("100", "6514", month, 110.0))
        rows.append(("200", "9999", month, 40.0))
    return pd.DataFrame(
        rows, columns=["Item No", "Customer No", "Month", "Shipped Qty lbs"],
    )


def _plan() -> pd.DataFrame:
    """Connector-shaped plan: month, party_site_code, item_code, plan_lbs."""
    rows = []
    # Forward plan Jul-Dec 2026 (plan > 0 => numerator uses plan).
    for m in range(7, 13):
        month = f"2026-{m:02d}-01"
        rows.append((month, "6514", "100", 130.0))   # party 6514 -> URM
        rows.append((month, "9999", "200", 30.0))
    return pd.DataFrame(
        rows, columns=["month", "party_site_code", "item_code", "plan_lbs"],
    )


def _dimitems() -> pd.DataFrame:
    return pd.DataFrame({
        "item_code": ["100", "200"],
        "portfolio_major": ["Cultured", "Fluid Milk"],
        "portfolio_minor": ["Sour Cream", "Whole"],
        "supply_format": ["Cup", "Gallon"],
        "size": ["16 oz", "1 GA"],
        "taxonomy": ["Cultured", "Milk"],
        "brand_category": ["Branded", "Branded"],
        "brand_name": ["Darigold", "Darigold"],
        "milk_type": ["Conventional", "Conventional"],
        "business_unit": ["B2C", "B2C"],
        "item_description": ["DG Sour Cream", "DG Whole Milk"],
    })


def _customers() -> pd.DataFrame:
    return pd.DataFrame({
        "customer_num": ["6514", "9999"],
        "customer_name": ["URM Stores", "Costco Wholesale"],
        "corporate_group": ["URM", "COSTCO"],
    })


def _build():
    return build_plan_lift_base(
        shipments_df=_shipments(),
        plan_df=_plan(),
        dimitems_df=_dimitems(),
        customer_names_df=_customers(),
    )


# ── build_plan_lift_base ────────────────────────────────────────────────────

def test_base_grain_and_corp_group_both_sides():
    base, stats = _build()
    # Grain columns present.
    for col in ("month", "item_key", "corporate_group", "plan_lbs", "ship_lbs"):
        assert col in base.columns
    # Corporate Group resolved on BOTH legs (shipments via Customer No,
    # plan via party_site_code) — never the unmapped sentinel here.
    assert set(base["corporate_group"]) == {"URM", "COSTCO"}
    assert stats.corp_unmapped_ship_pct == 0.0
    assert stats.corp_unmapped_plan_pct == 0.0
    # Dims attached by item key.
    assert set(base.loc[base["item_key"] == "100", "portfolio_major"]) == {"Cultured"}


def test_base_outer_merge_keeps_plan_only_months():
    base, _ = _build()
    # Jul-Dec 2026 exist only on the plan side; they must survive the merge.
    jul = base[(base["month"] == pd.Timestamp("2026-07-01")) & (base["item_key"] == "100")]
    assert len(jul) == 1
    assert float(jul["plan_lbs"].iloc[0]) == 130.0
    assert float(jul["ship_lbs"].iloc[0]) == 0.0


def test_unmapped_customer_falls_back_to_sentinel():
    ship = _shipments()
    ship.loc[len(ship)] = ("100", "0000", "2025-01-01", 999.0)  # 0000 not in dim
    base, stats = build_plan_lift_base(
        shipments_df=ship, plan_df=_plan(),
        dimitems_df=_dimitems(), customer_names_df=_customers(),
    )
    assert CORP_GROUP_UNMAPPED in set(base["corporate_group"])
    assert stats.corp_unmapped_ship_pct > 0.0


def test_item_missing_in_dim_marked_unknown():
    ship = _shipments()
    ship.loc[len(ship)] = ("777", "6514", "2025-01-01", 10.0)  # 777 not in dim
    base, stats = build_plan_lift_base(
        shipments_df=ship, plan_df=_plan(),
        dimitems_df=_dimitems(), customer_names_df=_customers(),
    )
    unknown = base[base["item_key"] == "777"]
    assert set(unknown["portfolio_major"]) == {DIM_UNKNOWN}
    assert stats.item_unmatched_pct > 0.0


def test_list_portfolios_excludes_unknown():
    base, _ = _build()
    assert list_portfolios(base) == ["Cultured", "Fluid Milk"]


# ── compute_yoy_lift ─────────────────────────────────────────────────────────

def test_numerator_coalesces_plan_over_shipments():
    base, _ = _build()
    res = compute_yoy_lift(base, {"portfolio_major": ["Cultured"]})
    f = res.frame.set_index("month")
    # History month: no plan -> numerator = shipments (100).
    assert f.loc[pd.Timestamp("2025-03-01"), "numerator"] == 100.0
    # Future month: plan present -> numerator = plan (130), not shipments.
    assert f.loc[pd.Timestamp("2026-08-01"), "numerator"] == 130.0


def test_prior_year_uses_shipments_only():
    base, _ = _build()
    res = compute_yoy_lift(base, {"portfolio_major": ["Cultured"]})
    f = res.frame.set_index("month")
    # Aug-2026 numerator = 130 (plan); its prior_year = Aug-2025 SHIPMENTS
    # = 100.  Lift = 130/100 - 1 = 0.30.
    assert f.loc[pd.Timestamp("2026-08-01"), "prior_year"] == 100.0
    assert math.isclose(f.loc[pd.Timestamp("2026-08-01"), "lift"], 0.30, abs_tol=1e-9)
    # First 12 months have no prior year -> NaN ("n.m.").
    assert np.isnan(f.loc[pd.Timestamp("2025-01-01"), "lift"])


def test_prior_year_ignores_historical_plan():
    """Prior year is shipments only — a plan in the year-ago month is ignored."""
    base = pd.DataFrame({
        "month": [pd.Timestamp("2025-08-01"), pd.Timestamp("2026-08-01")],
        "item_key": ["100", "100"],
        "corporate_group": ["URM", "URM"],
        # Aug-2025 carries BOTH a 999 plan and 100 shipped; Aug-2026 is plan.
        "ship_lbs": [100.0, 0.0],
        "plan_lbs": [999.0, 130.0],
        "portfolio_major": ["Cultured", "Cultured"],
        "item_code": ["100", "100"],
    })
    f = compute_yoy_lift(base, {}).frame.set_index("month")
    aug26 = pd.Timestamp("2026-08-01")
    # PY must be Aug-2025 SHIPPED (100), NOT the Aug-2025 plan (999).
    assert f.loc[aug26, "prior_year"] == 100.0
    assert math.isclose(f.loc[aug26, "lift"], 0.30, abs_tol=1e-9)  # 130/100-1


def test_ratio_of_sums_not_mean_of_item_lifts():
    """Two items with very different per-item lifts must combine as Σ/Σ."""
    base, _ = _build()
    # Whole-company series (no filter) over Aug: plan = 130 (item100) + 30
    # (item200) = 160; prior_year = Aug-2025 ship = 100 + 50 = 150.
    res = compute_yoy_lift(base, {})
    f = res.frame.set_index("month")
    aug_lift = f.loc[pd.Timestamp("2026-08-01"), "lift"]
    assert math.isclose(aug_lift, 160.0 / 150.0 - 1.0, abs_tol=1e-9)
    # Mean of per-item lifts would be ((130/100-1)+(30/50-1))/2 = -0.05,
    # which must NOT equal the ratio-of-sums result.
    assert not math.isclose(aug_lift, -0.05, abs_tol=1e-6)


def test_prior_year_zero_is_nan():
    base = pd.DataFrame({
        "month": [pd.Timestamp("2026-01-01")],
        "item_key": ["100"], "corporate_group": ["URM"],
        "plan_lbs": [10.0], "ship_lbs": [0.0], "portfolio_major": ["Cultured"],
        "item_code": ["100"],
    })
    res = compute_yoy_lift(base, {})
    assert np.isnan(res.frame["lift"].iloc[0])  # no prior-year slot at all


def test_volume_floor_suppresses_small_base():
    base, _ = _build()
    # COSTCO/item200 prior-year base (Aug-2025 ship = 50) is below a 100 lb
    # floor, so its Aug lift is suppressed to NaN + flagged below_floor.
    res = compute_yoy_lift(base, {"portfolio_major": ["Fluid Milk"]}, volume_floor=100.0)
    f = res.frame.set_index("month")
    assert np.isnan(f.loc[pd.Timestamp("2026-08-01"), "lift"])
    assert bool(f.loc[pd.Timestamp("2026-08-01"), "below_floor"])


def test_empty_filter_selection_is_no_constraint():
    base, _ = _build()
    full = compute_yoy_lift(base, {})
    # An empty list for a dim must behave identically to omitting it.
    same = compute_yoy_lift(base, {"portfolio_major": []})
    pd.testing.assert_frame_equal(full.frame, same.frame)


# ── Portfolio-scoped option helpers ──────────────────────────────────────────

def test_list_minor_products_scoped_to_portfolio():
    base, _ = _build()
    assert list_minor_products(base, "Cultured") == ["Sour Cream"]
    assert list_minor_products(base, "Fluid Milk") == ["Whole"]


def test_scoped_slicer_options_limited_to_portfolio():
    base, _ = _build()
    opts = list_slicer_options_for_portfolio(base, "Cultured")
    # Only item 100 (Sour Cream / Darigold) lives in Cultured.
    assert opts["brand_name"] == ["Darigold"]
    assert opts["portfolio_minor"] == ["Sour Cream"]
    assert "200" not in opts["item_code"]


# ── IRI overlay ──────────────────────────────────────────────────────────────

def _iri() -> pd.DataFrame:
    return pd.DataFrame({
        "Product": ["A", "A", "B"],
        "Geography": ["G1", "G1", "G1"],
        "Custom Major Brand": ["Private Label"] * 3,
        "Tag": ["Prior Year Weekly Performance"] * 3,
        "Week": ["2025-06-03", "2025-06-10", "2025-07-01"],
        "Base Units": ["100", "300", "50"],
        "Incremental Units": ["20", "30", "5"],
        "Unit Lift %": ["0.2", "0.1", "0.1"],
    })


def test_iri_unit_lift_is_ratio_of_sums_by_month():
    res = compute_iri_unit_lift(_iri(), {})
    f = res.frame.set_index("month")
    # June: (20+30)/(100+300) = 0.125 ; July: 5/50 = 0.10 (ratio of sums,
    # NOT the mean of the weekly 0.2/0.1 figures).
    assert math.isclose(f.loc[pd.Timestamp("2025-06-01"), "unit_lift"], 0.125, abs_tol=1e-9)
    assert math.isclose(f.loc[pd.Timestamp("2025-07-01"), "unit_lift"], 0.10, abs_tol=1e-9)


def test_iri_filter_applies():
    res = compute_iri_unit_lift(_iri(), {"Product": ["A"]})
    months = set(res.frame["month"])
    assert pd.Timestamp("2025-07-01") not in months   # product B dropped
    f = res.frame.set_index("month")
    assert math.isclose(f.loc[pd.Timestamp("2025-06-01"), "unit_lift"], 0.125, abs_tol=1e-9)


def test_iri_week_excel_serial_parses_to_month():
    df = _iri()
    df["Week"] = ["45809", "45816", "45840"]   # Excel day-serials
    res = compute_iri_unit_lift(df, {})
    expected = pd.to_datetime(45809, origin="1899-12-30", unit="D").to_period("M").to_timestamp()
    assert expected in set(res.frame["month"])


def test_iri_base_zero_is_nan():
    df = _iri()
    df["Base Units"] = ["0", "0", "0"]
    res = compute_iri_unit_lift(df, {})
    assert res.frame["unit_lift"].isna().all()


def test_iri_filter_options_lists_distinct_values():
    opts = list_iri_filter_options(_iri())
    assert opts["Product"] == ["A", "B"]
    assert opts["Tag"] == ["Prior Year Weekly Performance"]


def test_iri_filter_options_handles_missing_df():
    assert list_iri_filter_options(None) == {c: [] for c in IRI_FILTER_COLUMNS}


def test_iri_geography_alias_resolves_source_typo():
    """The source spells it 'Georgraphy' — the Geography filter must still work."""
    df = _iri().rename(columns={"Geography": "Georgraphy"})
    opts = list_iri_filter_options(df)
    assert opts["Geography"] == ["G1"]            # options populate, not empty
    res = compute_iri_unit_lift(df, {"Geography": ["G1"]})
    assert not res.frame.empty                    # and filtering applies


def test_iri_file_label_is_basename():
    assert iri_file_label("RO Tracking/IRI/IRI_Food & Cream_Volume Lift_6.14.26.csv") == \
        "IRI_Food & Cream_Volume Lift_6.14.26.csv"
