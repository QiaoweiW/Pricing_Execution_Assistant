"""Unit tests for the order-book fill-rate transforms.

Covers the parts that would silently corrupt the chart if they broke:
dimension dedupe (a fan-out multiplies pounds), scope selection, the
timezone-safe week anchor, the Portfolio alias mapping that keeps the
section's filters usable, and the completed-lines-only fill rate.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from data_sources import orders_fill_rate as ofr
from data_sources.shipments_velocity import (
    COL_BUSINESS_UNIT,
    COL_CORP_GROUP,
    COL_CUSTOMER,
    COL_PORTFOLIO,
    COL_PRODUCT_DESC,
    COL_PRODUCT_FORMAT,
    COL_PRODUCT_MINOR,
    COL_SHIP_TO,
    WEEK_START,
)

MASTER = "Darigold Item Master Organization"


def _orders(**over) -> pd.DataFrame:
    """Two closed sales-order lines, one per item, both fully shipped."""
    base = {
        "Ordered Date": pd.to_datetime(
            ["2026-06-03 08:00:00+00:00", "2026-06-04 08:00:00+00:00"], utc=True),
        "Line Type Code": ["ORA_BUY", "ORA_BUY"],
        "Category Code": ["ORDER", "ORDER"],
        "Line Status": ["Closed", "Closed"],
        "Original Ordered Quantity Pounds": [100.0, 200.0],
        "Canceled Quantity Pounds": [0.0, 0.0],
        "Ordered Quantity Pounds": [100.0, 200.0],
        "Shipped Quantity Pounds": [100.0, 200.0],
        "Inventory Item ID": ["ITEM1", "ITEM2"],
        "Ship To Party Site ID": ["SITE1", "SITE1"],
    }
    base.update(over)
    return pd.DataFrame(base)


def _products(**over) -> pd.DataFrame:
    base = {
        "Inventory Item ID": ["ITEM1", "ITEM2"],
        "Organization Name": [MASTER, MASTER],
        "Item No": ["310001", "310002"],
        "Item Description": ["DG Homo Qt UP", "DG HH Qt UP"],
        "Portfolio Major": ["ESL", "HTST"],
        "Portfolio Minor": ["Classic Milk", "Classic Milk"],
        "Supply Format": ["Small Carton", "Large Carton"],
        "Business Unit": ["B2C", "B2C"],
    }
    base.update(over)
    return pd.DataFrame(base)


def _customers(**over) -> pd.DataFrame:
    base = {
        "Party Site ID": ["SITE1"],
        "Site Purpose": ["Ship to"],
        "Organization Name": ["United Natural Foods"],
        "Site Number": ["72398"],
    }
    base.update(over)
    return pd.DataFrame(base)


# ── dedupe ───────────────────────────────────────────────────────────────────

def test_dedupe_products_prefers_master_org():
    """A non-master row must never win — its dims can disagree."""
    p = pd.concat([
        _products(),
        _products(Organization_Name=None).assign(
            **{"Organization Name": ["Boise - Market St (GXO)"] * 2,
               "Portfolio Major": ["WRONG", "WRONG"]}),
    ], ignore_index=True)
    out = ofr.dedupe_products(p)
    assert out["Inventory Item ID"].is_unique
    assert set(out["Portfolio Major"]) == {"ESL", "HTST"}


def test_dedupe_products_keeps_items_absent_from_master_org():
    p = _products(**{"Organization Name": ["Some Plant", "Some Plant"]})
    out = ofr.dedupe_products(p)
    assert len(out) == 2
    assert out["Inventory Item ID"].is_unique


def test_dedupe_customers_prefers_ship_to():
    c = pd.DataFrame({
        "Party Site ID": ["SITE1", "SITE1", "SITE2"],
        "Site Purpose": ["Bill to", "Ship to", "Plan To"],
        "Organization Name": ["WRONG", "United Natural Foods", "Other"],
        "Site Number": ["999", "72398", "111"],
    })
    out = ofr.dedupe_customers(c)
    assert out["Party Site ID"].is_unique
    row = out[out["Party Site ID"] == "SITE1"].iloc[0]
    assert row["Organization Name"] == "United Natural Foods"
    # A site with no ship-to row still survives.
    assert "SITE2" in set(out["Party Site ID"])


# ── scope ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line_type,category,status,kept", [
    ("ORA_BUY", "ORDER", "Closed", True),
    ("ORA_BUY", "ORDER", "Awaiting Shipping", True),   # open, still in scope
    ("ORA_BUY", "ORDER", "Canceled", False),           # net of cancelled
    ("ORA_CREDIT_ONLY", "ORDER", "Closed", False),
    ("XX_BILL_ONLY", "ORDER", "Closed", False),
    ("ORA_BUY", "RETURN", "Closed", False),
])
def test_scope_sales_orders(line_type, category, status, kept):
    o = _orders(**{"Line Type Code": [line_type] * 2,
                   "Category Code": [category] * 2,
                   "Line Status": [status] * 2})
    assert (len(ofr.scope_sales_orders(o)) == 2) is kept


def test_scope_is_case_and_whitespace_tolerant():
    o = _orders(**{"Line Type Code": [" ora_buy ", "ORA_BUY"],
                   "Category Code": ["order", " ORDER"]})
    assert len(ofr.scope_sales_orders(o)) == 2


# ── joins ────────────────────────────────────────────────────────────────────

def test_join_dimensions_does_not_fan_out():
    o, p, c = _orders(), _products(), _customers()
    out = ofr.join_dimensions(o, p, c)
    assert len(out) == len(o)
    assert out["Ordered Quantity Pounds"].sum() == pytest.approx(300.0)
    assert set(out["Portfolio Major"]) == {"ESL", "HTST"}
    assert set(out["Organization Name"]) == {"United Natural Foods"}


def test_join_dimensions_raises_if_the_dedupe_regresses(monkeypatch):
    """The guard is the point: neutralise the dedupe and it must fail loudly.

    ``join_dimensions`` dedupes internally, so duplicate input alone can never
    reach the merge.  Stubbing the dedupe to a pass-through simulates exactly
    the regression the guard exists to catch — duplicate join keys silently
    multiplying pounds.
    """
    dupes = pd.concat([_products(), _products()], ignore_index=True)
    monkeypatch.setattr(ofr, "dedupe_products",
                        lambda p: dupes.drop(columns=["Organization Name"]))
    with pytest.raises(ofr.OrdersFillRateError,
                       match="changed the data|duplicate join keys"):
        ofr.join_dimensions(_orders(), _products(), _customers())


def test_join_dimensions_guard_catches_customer_fan_out(monkeypatch):
    two_purposes = pd.concat([_customers(), _customers()], ignore_index=True)
    monkeypatch.setattr(ofr, "dedupe_customers",
                        lambda c: two_purposes.drop(columns=["Site Purpose"]))
    with pytest.raises(ofr.OrdersFillRateError, match="changed the data"):
        ofr.join_dimensions(_orders(), _products(), _customers())


def test_unmatched_dimensions_survive_as_blanks():
    """An item with no Products row must keep its pounds, not vanish."""
    o = _orders(**{"Inventory Item ID": ["ITEM1", "UNKNOWN"]})
    out = ofr.tidy_orders(o, _products(), _customers())
    assert out[ofr.COL_ORDERED_LBS].sum() == pytest.approx(300.0)
    assert "" in set(out[COL_PORTFOLIO])


# ── week anchoring ───────────────────────────────────────────────────────────

def test_week_start_anchors_monday_without_tz_shift():
    """A Monday order must stay in its own week, not slip back a week."""
    s = pd.Series(pd.to_datetime(
        ["2026-06-01 02:00:00+00:00",   # Monday
         "2026-06-07 23:00:00+00:00"],  # Sunday, same week
        utc=True))
    out = ofr.week_start(s)
    assert list(out) == [pd.Timestamp("2026-06-01")] * 2
    assert out.dt.tz is None


# ── portfolio alias ──────────────────────────────────────────────────────────

def test_portfolio_aliases_match_the_shipments_vocabulary():
    """The section's dropdown says 'Extended Shelf Life', Products says 'ESL'."""
    out = ofr.tidy_orders(_orders(), _products(), _customers())
    assert set(out[COL_PORTFOLIO]) == {"Extended Shelf Life", "Fresh Milk"}


def test_other_portfolios_pass_through_unchanged():
    p = _products(**{"Portfolio Major": ["Butter", "Cheese"]})
    out = ofr.tidy_orders(_orders(), p, _customers())
    assert set(out[COL_PORTFOLIO]) == {"Butter", "Cheese"}


# ── weekly aggregation + fill rate ───────────────────────────────────────────

def test_tidy_orders_preserves_totals_and_dims():
    out = ofr.tidy_orders(_orders(), _products(), _customers())
    assert out[ofr.COL_ORDERED_LBS].sum() == pytest.approx(300.0)
    for col in (WEEK_START, COL_PORTFOLIO, COL_PRODUCT_MINOR, COL_PRODUCT_DESC,
                COL_PRODUCT_FORMAT, COL_BUSINESS_UNIT, COL_CUSTOMER,
                COL_SHIP_TO):
        assert col in out.columns
    assert set(out[COL_SHIP_TO]) == {"72398"}


def test_fill_rate_counts_cuts_as_a_miss():
    """80 shipped of 100 originally ordered = 80%, not 100%."""
    o = _orders(**{
        "Original Ordered Quantity Pounds": [100.0, 100.0],
        "Canceled Quantity Pounds": [20.0, 0.0],
        "Ordered Quantity Pounds": [80.0, 100.0],
        "Shipped Quantity Pounds": [80.0, 100.0],
    })
    r = ofr.build_weekly_fill_rate(ofr.tidy_orders(o, _products(), _customers()))
    assert r.total_cut == pytest.approx(20.0)
    assert r.fill_rate_gross == pytest.approx(180.0 / 200.0)   # 90%
    assert r.fill_rate_net == pytest.approx(1.0)               # nothing short-shipped
    assert r.cut_rate == pytest.approx(0.10)


def test_open_lines_excluded_from_fill_rate_but_not_from_volume():
    """The newest week must not read as a collapse just for being in flight."""
    o = _orders(**{
        "Line Status": ["Closed", "Awaiting Shipping"],
        "Shipped Quantity Pounds": [100.0, 0.0],
    })
    r = ofr.build_weekly_fill_rate(ofr.tidy_orders(o, _products(), _customers()))
    assert r.total_ordered == pytest.approx(300.0)      # both lines in the bars
    assert r.completed_original == pytest.approx(100.0)  # only the closed one
    assert r.fill_rate_gross == pytest.approx(1.0)      # 100%, not 33%
    assert r.open_lines == 1


def test_fill_rate_is_none_when_nothing_has_completed():
    o = _orders(**{"Line Status": ["Awaiting Shipping"] * 2,
                   "Shipped Quantity Pounds": [0.0, 0.0]})
    r = ofr.build_weekly_fill_rate(ofr.tidy_orders(o, _products(), _customers()))
    assert r.fill_rate_gross is None
    assert r.total_ordered == pytest.approx(300.0)


def test_weekly_rows_are_one_per_week_ascending():
    o = _orders(**{"Ordered Date": pd.to_datetime(
        ["2026-06-10 08:00:00+00:00", "2026-06-03 08:00:00+00:00"], utc=True)})
    r = ofr.build_weekly_fill_rate(ofr.tidy_orders(o, _products(), _customers()))
    assert list(r.weekly[WEEK_START]) == [pd.Timestamp("2026-06-01"),
                                          pd.Timestamp("2026-06-08")]


# ── filters ──────────────────────────────────────────────────────────────────

def test_filters_use_the_same_contract_as_shipments_velocity():
    tidy = ofr.tidy_orders(_orders(), _products(), _customers())
    everything = ofr.build_weekly_fill_rate(tidy)
    assert everything.total_ordered == pytest.approx(300.0)

    esl = ofr.build_weekly_fill_rate(tidy, portfolios=["Extended Shelf Life"])
    assert esl.total_ordered == pytest.approx(100.0)

    fmt = ofr.build_weekly_fill_rate(tidy, product_formats=["Small Carton"])
    assert fmt.total_ordered == pytest.approx(100.0)

    cust = ofr.build_weekly_fill_rate(tidy, customers=["United Natural Foods"])
    assert cust.total_ordered == pytest.approx(300.0)

    # Empty list means "all", matching build_weekly_velocity.
    assert ofr.build_weekly_fill_rate(
        tidy, portfolios=None).total_ordered == pytest.approx(300.0)


def test_unmatched_filter_yields_empty_not_error():
    tidy = ofr.tidy_orders(_orders(), _products(), _customers())
    r = ofr.build_weekly_fill_rate(tidy, customers=["NOBODY"])
    assert r.weekly.empty
    assert r.fill_rate_gross is None


def test_corporate_group_filter_applies_when_the_page_attached_it():
    """The page adds COL_CORP_GROUP after fetch; the filter must honour it."""
    tidy = ofr.tidy_orders(_orders(), _products(), _customers())
    tidy[COL_CORP_GROUP] = ["United Natural Foods"] * len(tidy)
    hit = ofr.build_weekly_fill_rate(
        tidy, corporate_groups=["United Natural Foods"])
    miss = ofr.build_weekly_fill_rate(tidy, corporate_groups=["Kroger"])
    assert hit.total_ordered == pytest.approx(300.0)
    assert miss.weekly.empty


def test_date_range_slices_the_window():
    o = _orders(**{"Ordered Date": pd.to_datetime(
        ["2026-06-03 08:00:00+00:00", "2026-06-10 08:00:00+00:00"], utc=True)})
    tidy = ofr.tidy_orders(o, _products(), _customers())
    import datetime as _dt
    r = ofr.build_weekly_fill_rate(
        tidy, date_range=(_dt.date(2026, 6, 8), _dt.date(2026, 6, 30)))
    assert len(r.weekly) == 1
    assert r.total_ordered == pytest.approx(200.0)


def test_empty_input_is_well_shaped():
    r = ofr.build_weekly_fill_rate(pd.DataFrame())
    assert r.weekly.empty
    assert WEEK_START in r.weekly.columns
    assert r.total_ordered == 0.0
    assert r.fill_rate_gross is None


def test_over_shipment_is_not_clipped():
    """Shipping more than ordered is real in this data; don't hide it."""
    o = _orders(**{"Shipped Quantity Pounds": [110.0, 200.0]})
    r = ofr.build_weekly_fill_rate(ofr.tidy_orders(o, _products(), _customers()))
    assert r.fill_rate_gross > 1.0
    assert not np.isnan(r.weekly[ofr.FILL_RATE_GROSS]).all()
