"""Unit tests for the demand × item × customer enrichment pipeline.

Synthetic fixtures only — exercises the pure-pandas + rapidfuzz logic
in :mod:`data_sources.demand_item_customer` without touching Microsoft
Fabric.  We cover, in order:

1. ``compute_cy_actual_months`` (the discriminator that drives every
   downstream operation).
2. ``_normalise_for_fuzzy`` + the helpers behind the per-forecast-type
   Corporate Group dispatcher.
3. ``attach_customer_no_from_ship_to_sites`` — Base Plan rows only.
4. ``attach_corporate_group_by_forecast_type`` — the three branches:
   exact customer_num for Actual + Base Plan, fuzzy Customer Name for
   R&O, universal fallback to Customer Name on misses.
5. ``attach_corporate_group_to_orders`` — exact lookup against
   ``dp_dimcustomernames`` with fallback to Customer Name when the
   dim row is missing or blank.
6. ``build_demand_order_item_customer`` end-to-end:
       * CY-Actual rows in the detail CSV are dropped.
       * IBP-Shipments rows are appended with PDH dims and Customer No.
       * Customer No is back-filled on Base Plan rows via the
         ship-to-sites dim.
       * Corporate Group is resolved per row by Forecast Type.
       * Column order matches OUTPUT_COLUMNS (with Customer No BEFORE
         Customer Name, per planner spec).
7. ``prepare_demand_long_for_plr`` produces the long shape the table
   builder consumes (including the corporate-group column).
8. ``list_filter_values_from_demand`` cascades correctly.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from data_sources.demand_item_customer import (
    COL_CORPORATE_GROUP,
    COL_CUSTOMER_NAME,
    COL_CUSTOMER_NO,
    COL_DEMAND_LBS,
    COL_FORECAST_TYPE,
    COL_ITEM,
    COL_ITEM_DESC,
    COL_PARTY_SITE_NUMBER,
    COL_PORTFOLIO_MAJOR,
    COL_PORTFOLIO_MINOR,
    COL_START_OF_MONTH,
    COL_SUPPLY_FORMAT,
    FORECAST_TYPE_ACTUAL,
    FORECAST_TYPE_BASE_PLAN,
    OUTPUT_COLUMNS,
    _normalise_for_fuzzy,
    apply_corp_group_canonical_map,
    attach_corporate_group_by_forecast_type,
    attach_corporate_group_to_orders,
    attach_customer_no_from_ship_to_sites,
    build_corp_group_canonical_map,
    build_demand_order_item_customer,
    compute_cy_actual_months,
    list_filter_values_for_pmaj_from_demand,
    list_filter_values_from_demand,
    prepare_demand_long_for_plr,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures (kept tiny + synthetic — every row is documented)
# ─────────────────────────────────────────────────────────────────────────────

def _pdh() -> pd.DataFrame:
    """Minimum PDH frame for the synthesised Actual rows."""
    return pd.DataFrame([
        {
            "Item No": "311042",
            "Item Description": "DG Test Butter Quarter",
            "Portfolio Major": "Butter",
            "Portfolio Minor": "Packaged Butter",
            "Supply Format": "Bundled Elgin Quarter",
        },
        {
            "Item No": "310180",
            "Item Description": "DG Btr Qtr 1Lb 30cs",
            "Portfolio Major": "Butter",
            "Portfolio Minor": "Packaged Butter",
            "Supply Format": "Western Quarters",
        },
    ])


def _detail() -> pd.DataFrame:
    """Detail CSV: Apr 2026 Base Plan (CY-Actual → dropped),
    Sep 2026 Base Plan (kept), Sep 2026 R&O (kept).
    No Customer No published — we'll back-fill via ship-to-sites.
    """
    return pd.DataFrame([
        {
            COL_START_OF_MONTH: "2026-04-01",
            COL_ITEM: "311042",
            COL_ITEM_DESC: "DG Test Butter Quarter",
            COL_CUSTOMER_NAME: "ALBERTSONS SAFEWAY",
            COL_PARTY_SITE_NUMBER: "10244",
            COL_DEMAND_LBS: "10,626",
            COL_FORECAST_TYPE: "Base Plan",
            COL_PORTFOLIO_MAJOR: "Butter",
            COL_PORTFOLIO_MINOR: "Packaged Butter",
            COL_SUPPLY_FORMAT: "Bundled Elgin Quarter",
        },
        {
            COL_START_OF_MONTH: "2026-09-01",
            COL_ITEM: "310180",
            COL_ITEM_DESC: "DG Btr Qtr 1Lb 30cs",
            COL_CUSTOMER_NAME: "KROGER",
            COL_PARTY_SITE_NUMBER: "5862",
            COL_DEMAND_LBS: "20000",
            COL_FORECAST_TYPE: "Base Plan",
            COL_PORTFOLIO_MAJOR: "Butter",
            COL_PORTFOLIO_MINOR: "Packaged Butter",
            COL_SUPPLY_FORMAT: "Western Quarters",
        },
        {
            COL_START_OF_MONTH: "2026-09-01",
            COL_ITEM: "310180",
            COL_ITEM_DESC: "DG Btr Qtr 1Lb 30cs",
            COL_CUSTOMER_NAME: "FAR WEST DISTR INC",  # R&O — fuzzy branch.
            COL_PARTY_SITE_NUMBER: "",                # R&O carries no PS#.
            COL_DEMAND_LBS: "1500",
            COL_FORECAST_TYPE: "RO",                  # not Actual / Base Plan
            COL_PORTFOLIO_MAJOR: "Butter",
            COL_PORTFOLIO_MINOR: "Packaged Butter",
            COL_SUPPLY_FORMAT: "Western Quarters",
        },
    ])


def _shipments() -> pd.DataFrame:
    """IBP Shipments: an Apr 2026 row REPLACES the detail Apr row, and a
    Sep 2026 row that must be IGNORED (Sep is YTG, not CY Actual)."""
    return pd.DataFrame([
        {
            "Item No": "311042",
            "Customer No": "6058",
            "Customer Name": "FAR WEST DISTR INC",
            "Month": "2026-04-01",
            "Shipped Qty lbs": 41336.25,
        },
        # Sep is YTG (NOT in CY Actual) — must be IGNORED.
        {
            "Item No": "310180",
            "Customer No": "5862",
            "Customer Name": "KROGER",
            "Month": "2026-09-01",
            "Shipped Qty lbs": 999_999.0,
        },
    ])


def _customer_names_dim() -> pd.DataFrame:
    """Customer-num + customer-name + corporate-group dim rows.

    Mix of:
      * Albertsons row matches by customer_num (Base Plan flow) AND by
        name (fuzzy flow);
      * a Blank corporate_group entry that exercises the fallback;
      * a Far-West row only reachable via fuzzy match (R&O path);
      * an unrelated bakery entry to test no-match fallback.
    """
    return pd.DataFrame([
        # Customer No 7777 is used by an Actual row in our test.
        {"customer_num": "7777", "customer_name": "FOO BAR FOODS",
         "corporate_group": "Foo Bar Group"},
        # Base Plan: party-site 10244 → customer_num 8001 in ship-to-sites
        # → corporate_group below.
        {"customer_num": "8001", "customer_name": "Albertsons Safeway, Inc.",
         "corporate_group": "Albertsons-Safeway"},
        # Far West fuzzy match — corporate_group is "Blank" → fallback.
        {"customer_num": "9999", "customer_name": "FAR WEST DISTRIBUTORS",
         "corporate_group": "Blank"},
        # Sep KROGER party-site 5862 → customer_num 8002 → "Kroger Co"
        {"customer_num": "8002", "customer_name": "KROGER CO.",
         "corporate_group": "Kroger Co"},
    ])


def _ship_to_sites_dim() -> pd.DataFrame:
    """Party Site Number → customer_num translation table.
    Resolves the Apr/Sep Base Plan rows to dim-customer-nums above."""
    return pd.DataFrame([
        {"party_site_code": "10244", "customer_num": "8001",
         "account_description": "Albertsons Safeway"},
        {"party_site_code": "5862", "customer_num": "8002",
         "account_description": "Kroger"},
    ])


# ─────────────────────────────────────────────────────────────────────────────
# 1) CY Actual Months arithmetic
# ─────────────────────────────────────────────────────────────────────────────

def test_compute_cy_actual_months_excludes_ytg_window():
    """CY FY = Apr 2026 – Mar 2027; YTG = May 2026 – Mar 2027; Actual = Apr 2026."""
    cy_fy = sorted({date(2026, m, 1) for m in range(4, 13)} | {
        date(2027, m, 1) for m in (1, 2, 3)
    })
    actual = compute_cy_actual_months(
        cy_full_year_months=cy_fy,
        cy_ytg_start=date(2026, 5, 1),
        cy_ytg_end=date(2027, 3, 1),
    )
    assert actual == (date(2026, 4, 1),)


def test_compute_cy_actual_months_handles_full_ytg_window():
    """If YTG covers the entire CY FY, there are no actual months."""
    cy_fy = sorted({date(2026, m, 1) for m in range(4, 13)} | {
        date(2027, m, 1) for m in (1, 2, 3)
    })
    actual = compute_cy_actual_months(
        cy_full_year_months=cy_fy,
        cy_ytg_start=date(2026, 4, 1),
        cy_ytg_end=date(2027, 3, 1),
    )
    assert actual == ()


# ─────────────────────────────────────────────────────────────────────────────
# 2) Fuzzy normalisation
# ─────────────────────────────────────────────────────────────────────────────

def test_normalise_for_fuzzy_strips_suffixes_and_punctuation():
    """Legal suffixes + punctuation must collapse to a comparable form."""
    assert _normalise_for_fuzzy("Albertsons Safeway, Inc.") == "ALBERTSONS SAFEWAY"
    assert _normalise_for_fuzzy("FAR WEST DISTR INC") == "FAR WEST DISTR"
    assert _normalise_for_fuzzy("  the   COMPANY  ") == ""


# ─────────────────────────────────────────────────────────────────────────────
# 3) Customer No back-fill (Base Plan via ship-to-sites)
# ─────────────────────────────────────────────────────────────────────────────

def test_attach_customer_no_from_ship_to_sites_fills_base_plan_only():
    """The lookup only writes Customer No on Base Plan rows.  Actual /
    R&O rows are left untouched (planner spec — Actual already has a
    Customer No from shipments; R&O has none and the universal
    fallback resolves Corporate Group to Customer Name)."""
    unified = pd.DataFrame({
        COL_FORECAST_TYPE: ["Base Plan", "Actual", "RO"],
        COL_PARTY_SITE_NUMBER: ["10244", "10244", "10244"],
        COL_CUSTOMER_NO: ["", "6058", ""],
        COL_CUSTOMER_NAME: ["Albertsons", "Far West", "RO Customer"],
    })
    out = attach_customer_no_from_ship_to_sites(unified, _ship_to_sites_dim())
    # Base Plan: filled from the dim.
    assert out.loc[out[COL_FORECAST_TYPE] == "Base Plan", COL_CUSTOMER_NO].iloc[0] == "8001"
    # Actual: existing Customer No preserved (not overwritten).
    assert out.loc[out[COL_FORECAST_TYPE] == "Actual", COL_CUSTOMER_NO].iloc[0] == "6058"
    # R&O: left blank.
    assert out.loc[out[COL_FORECAST_TYPE] == "RO", COL_CUSTOMER_NO].iloc[0] == ""


def test_attach_customer_no_handles_missing_dim_table():
    """No dim table → frame returned unchanged (no exception)."""
    unified = pd.DataFrame({
        COL_FORECAST_TYPE: ["Base Plan"],
        COL_PARTY_SITE_NUMBER: ["10244"],
        COL_CUSTOMER_NO: [""],
        COL_CUSTOMER_NAME: ["Albertsons"],
    })
    out = attach_customer_no_from_ship_to_sites(unified, None)
    assert out[COL_CUSTOMER_NO].iloc[0] == ""


def test_attach_customer_no_handles_party_site_miss():
    """Per Q4 (planner): a Base Plan row whose Party Site Number is NOT
    in the dim keeps a blank Customer No — the universal Corporate
    Group fallback (= Customer Name) handles those rows downstream."""
    unified = pd.DataFrame({
        COL_FORECAST_TYPE: ["Base Plan"],
        COL_PARTY_SITE_NUMBER: ["UNLISTED_PS#"],
        COL_CUSTOMER_NO: [""],
        COL_CUSTOMER_NAME: ["Unknown Customer"],
    })
    out = attach_customer_no_from_ship_to_sites(unified, _ship_to_sites_dim())
    assert out[COL_CUSTOMER_NO].iloc[0] == ""


# ─────────────────────────────────────────────────────────────────────────────
# 4) Forecast-type-aware Corporate Group dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def _unified_three_rows() -> pd.DataFrame:
    """Three rows — one per branch — with the Customer No values that
    map directly to dim rows in :func:`_customer_names_dim`."""
    return pd.DataFrame({
        # Branch B: Base Plan → customer_num exact join.
        # Branch A: Actual    → customer_num exact join.
        # Branch C: R&O       → fuzzy on Customer Name.
        COL_FORECAST_TYPE: ["Base Plan", "Actual", "RO"],
        COL_CUSTOMER_NO: ["8001", "7777", ""],
        COL_CUSTOMER_NAME: [
            "Albertsons LLC",        # exact via customer_num
            "Foo Bar Foods, Inc.",   # exact via customer_num
            "FAR WEST DISTR INC",    # fuzzy → "Blank" → fallback
        ],
    })


def test_dispatch_assigns_exact_corp_group_for_actual_and_base_plan():
    out, stats = attach_corporate_group_by_forecast_type(
        _unified_three_rows(), _customer_names_dim(),
    )
    # Base Plan and Actual rows use customer_num → corporate_group.
    bp = out.loc[out[COL_FORECAST_TYPE] == "Base Plan"].iloc[0]
    ac = out.loc[out[COL_FORECAST_TYPE] == "Actual"].iloc[0]
    assert bp[COL_CORPORATE_GROUP] == "Albertsons-Safeway"
    assert ac[COL_CORPORATE_GROUP] == "Foo Bar Group"
    # Audit counts: two exact rows, both matched.
    assert stats["n_exact"] == 2
    assert stats["n_exact_matched"] == 2
    # One R&O row, dim row had "Blank" → counts as "matched=0" via the
    # universal fallback path.
    assert stats["n_fuzzy"] == 1
    assert stats["n_fuzzy_matched"] == 0


def test_dispatch_uses_fuzzy_for_ro_rows():
    """R&O rows fuzzy-match on Customer Name; when a non-blank dim row
    matches the normalised name, its corporate_group wins.  We use a
    candidate name that normalises to the SAME token set as the input
    so the token_set_ratio clears the (strict) default threshold."""
    # Reuse the standard dim but REMOVE the "Blank" Far West entry so
    # the only Far-West-ish candidate is the one with a real group.
    cn_dim = _customer_names_dim().copy()
    cn_dim = cn_dim.loc[
        cn_dim["corporate_group"].astype(str).str.casefold() != "blank"
    ].reset_index(drop=True)
    # Add a dim row whose normalised form is a superset of the input's
    # ("FAR WEST DISTR") — token_set_ratio returns 100 in that case.
    cn_dim = pd.concat([cn_dim, pd.DataFrame([{
        "customer_num": "10001",
        "customer_name": "Far West Distr Holdings Inc",
        "corporate_group": "Far West Holdings",
    }])], ignore_index=True)
    out, stats = attach_corporate_group_by_forecast_type(
        _unified_three_rows(), cn_dim,
    )
    ro = out.loc[out[COL_FORECAST_TYPE] == "RO"].iloc[0]
    assert ro[COL_CORPORATE_GROUP] == "Far West Holdings"
    assert stats["n_fuzzy_matched"] == 1


def test_dispatch_falls_back_to_customer_name_on_misses():
    """No dim table → every row falls back to Customer Name verbatim."""
    unified = _unified_three_rows()
    out, stats = attach_corporate_group_by_forecast_type(unified, None)
    assert out[COL_CORPORATE_GROUP].tolist() == [
        "Albertsons LLC", "Foo Bar Foods, Inc.", "FAR WEST DISTR INC",
    ]
    assert stats["n_exact_matched"] == 0
    assert stats["n_fuzzy_matched"] == 0


def test_dispatch_does_not_fuzzy_match_actual_and_base_plan_rows():
    """Key behaviour: Actual / Base Plan rows must NOT run through the
    fuzzy branch.  If their customer_num doesn't match, they fall back
    to Customer Name verbatim — never to a fuzzy approximation."""
    # Strip every customer_num from the dim → exact branch will miss
    # for both Base Plan and Actual.  Add a row whose customer_name
    # would fuzzy-match Albertsons strongly — to prove we do NOT use it.
    dim = pd.DataFrame([
        {"customer_num": "different_num", "customer_name": "Albertsons Inc.",
         "corporate_group": "Albertsons-Inc-Group"},
    ])
    out, stats = attach_corporate_group_by_forecast_type(
        _unified_three_rows(), dim,
    )
    bp = out.loc[out[COL_FORECAST_TYPE] == "Base Plan"].iloc[0]
    # MUST be the Customer Name fallback, NOT "Albertsons-Inc-Group".
    assert bp[COL_CORPORATE_GROUP] == "Albertsons LLC"
    assert stats["n_exact"] == 2
    assert stats["n_exact_matched"] == 0


def test_dispatch_handles_empty_frame():
    """Empty frame returns an empty Corporate Group column + zero stats."""
    out, stats = attach_corporate_group_by_forecast_type(
        pd.DataFrame(columns=[COL_FORECAST_TYPE, COL_CUSTOMER_NO, COL_CUSTOMER_NAME]),
        _customer_names_dim(),
    )
    assert COL_CORPORATE_GROUP in out.columns
    assert stats == {
        "n_total": 0, "n_exact": 0, "n_exact_matched": 0,
        "n_fuzzy": 0, "n_fuzzy_matched": 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5) Orders-side corporate-group attach (now via dp_dimcustomernames)
# ─────────────────────────────────────────────────────────────────────────────

def test_attach_corporate_group_to_orders_uses_customer_names_dim():
    """The orders side now joins on dp_dimcustomernames (planner
    retired dp_dimcorporategroup as of June 2026)."""
    orders = pd.DataFrame([
        {"Customer No": "7777", "Customer Name": "FOO BAR FOODS",
         "Item No": "311042", "Month": "2026-04-01", "Ordered Qty lbs": 100.0},
        {"Customer No": "9999", "Customer Name": "FAR WEST",
         "Item No": "310180", "Month": "2026-09-01", "Ordered Qty lbs": 200.0},
        {"Customer No": "8888", "Customer Name": "UNLISTED",
         "Item No": "310180", "Month": "2026-09-01", "Ordered Qty lbs": 300.0},
    ])
    out = attach_corporate_group_to_orders(orders, _customer_names_dim())
    cg = out["customer_corp_group"].tolist()
    assert cg[0] == "Foo Bar Group"   # exact via customer_num
    assert cg[1] == "FAR WEST"        # dim said "Blank" → fallback to Customer Name
    assert cg[2] == "UNLISTED"        # no dim row → fallback


def test_attach_corporate_group_handles_post_enrichment_column_names():
    """Accepts both the raw projection (``Customer No``) AND the
    post-enrichment shape (``customer_no``) — the page may call this
    before or after the PDH-dim merge."""
    enriched_orders = pd.DataFrame([
        {"customer_no": "7777", "customer_name": "FOO BAR FOODS",
         "item_key": "311042", "month": date(2026, 4, 1), "pounds": 100.0},
    ])
    out = attach_corporate_group_to_orders(enriched_orders, _customer_names_dim())
    assert out["customer_corp_group"].iloc[0] == "Foo Bar Group"


def test_attach_corporate_group_to_orders_handles_missing_dim():
    """No dim → every row falls back to Customer Name verbatim."""
    orders = pd.DataFrame([{
        "Customer No": "7777", "Customer Name": "Foo Bar",
        "Month": "2026-04-01", "Ordered Qty lbs": 1.0,
    }])
    out = attach_corporate_group_to_orders(orders, None)
    assert out["customer_corp_group"].iloc[0] == "Foo Bar"


# ─────────────────────────────────────────────────────────────────────────────
# 6) End-to-end enrichment
# ─────────────────────────────────────────────────────────────────────────────

def test_build_demand_order_item_customer_end_to_end():
    """Drop CY-Actual rows → append IBP-Shipments rows → back-fill
    Customer No → resolve Corporate Group per forecast type.

    With CY Actual = {Apr 2026}, the detail CSV's Apr row must be
    removed and the IBP Shipments Apr row appended (Forecast Type =
    "Actual" with shipments' Customer No preserved).  The Sep rows
    in the detail CSV survive; the Sep IBP-Shipments row is IGNORED
    (not in CY Actual).
    """
    build = build_demand_order_item_customer(
        detail_df=_detail(),
        shipments_df=_shipments(),
        pdh_df=_pdh(),
        customer_names_dim=_customer_names_dim(),
        ship_to_sites_dim=_ship_to_sites_dim(),
        cy_actual_months=(date(2026, 4, 1),),
    )

    df = build.df
    # Schema: every output column in canonical order — Customer No is
    # right BEFORE Customer Name (planner spec).
    assert list(df.columns) == list(OUTPUT_COLUMNS)
    idx = df.columns.get_loc
    assert idx(COL_CUSTOMER_NO) == idx(COL_CUSTOMER_NAME) - 1

    # Row count: 2 (Sep detail) + 1 (Apr synthesised Actual) = 3.
    assert len(df) == 3

    # The Apr 2026 row must now be Forecast Type = "Actual" — sourced
    # from IBP Shipments, NOT the original Base Plan row.  Customer
    # No must come straight from shipments.
    apr = df.loc[df[COL_START_OF_MONTH] == date(2026, 4, 1)].iloc[0]
    assert apr[COL_FORECAST_TYPE] == FORECAST_TYPE_ACTUAL
    assert apr[COL_CUSTOMER_NAME] == "FAR WEST DISTR INC"
    assert apr[COL_CUSTOMER_NO] == "6058"   # from shipments
    assert apr[COL_DEMAND_LBS] == 41336.25
    # PDH dims came from the join.
    assert apr[COL_PORTFOLIO_MAJOR] == "Butter"
    assert apr[COL_SUPPLY_FORMAT] == "Bundled Elgin Quarter"
    # Party Site Number left blank per planner spec.
    assert apr[COL_PARTY_SITE_NUMBER] == ""
    # Actual row's Customer No isn't in the dim → fallback.
    assert apr[COL_CORPORATE_GROUP] == "FAR WEST DISTR INC"

    # Sep 2026 Base Plan KROGER row → Customer No filled via ship-to-sites
    # → corporate_group resolved via dp_dimcustomernames.
    sep_bp = df.loc[
        (df[COL_START_OF_MONTH] == date(2026, 9, 1))
        & (df[COL_CUSTOMER_NAME] == "KROGER")
    ].iloc[0]
    assert sep_bp[COL_FORECAST_TYPE] == "Base Plan"
    assert sep_bp[COL_CUSTOMER_NO] == "8002"
    assert sep_bp[COL_CORPORATE_GROUP] == "Kroger Co"

    # Sep 2026 R&O row → no Customer No → fuzzy match.  The dim row
    # ("FAR WEST DISTRIBUTORS" → "Blank") yields the Customer Name
    # fallback per planner spec.
    sep_ro = df.loc[df[COL_FORECAST_TYPE] == "RO"].iloc[0]
    assert sep_ro[COL_CUSTOMER_NO] == ""
    assert sep_ro[COL_CORPORATE_GROUP] == "FAR WEST DISTR INC"


def test_build_handles_empty_shipments():
    """No CY-Actual shipments → keep every detail row outside CY Actual."""
    build = build_demand_order_item_customer(
        detail_df=_detail(),
        shipments_df=pd.DataFrame(),
        pdh_df=_pdh(),
        customer_names_dim=_customer_names_dim(),
        ship_to_sites_dim=_ship_to_sites_dim(),
        cy_actual_months=(date(2026, 4, 1),),
    )
    # Apr row was DROPPED (CY Actual) and not replaced — only the
    # two Sep rows survive.
    assert len(build.df) == 2
    assert set(build.df[COL_START_OF_MONTH]) == {date(2026, 9, 1)}


def test_build_skips_replacement_when_cy_actual_empty():
    """No CY Actual months → detail CSV passes through unchanged."""
    build = build_demand_order_item_customer(
        detail_df=_detail(),
        shipments_df=_shipments(),
        pdh_df=_pdh(),
        customer_names_dim=_customer_names_dim(),
        ship_to_sites_dim=_ship_to_sites_dim(),
        cy_actual_months=(),
    )
    assert len(build.df) == 3
    assert FORECAST_TYPE_ACTUAL not in set(build.df[COL_FORECAST_TYPE])


def test_build_uses_orders_qty_column_as_fallback():
    """When IBP Shipments is presented with the orders-naming column
    instead (planner has discussed renaming once shipments-as-actual
    goes live), the synthesis still works via the candidate fallback."""
    shipments = pd.DataFrame([{
        "Item No": "311042", "Customer No": "6058",
        "Customer Name": "FAR WEST DISTR INC",
        "Month": "2026-04-01",
        "Ordered Qty lbs": 41336.25,    # NOTE: orders-naming column.
    }])
    build = build_demand_order_item_customer(
        detail_df=_detail(),
        shipments_df=shipments,
        pdh_df=_pdh(),
        customer_names_dim=_customer_names_dim(),
        ship_to_sites_dim=_ship_to_sites_dim(),
        cy_actual_months=(date(2026, 4, 1),),
    )
    apr = build.df.loc[build.df[COL_FORECAST_TYPE] == FORECAST_TYPE_ACTUAL].iloc[0]
    assert apr[COL_DEMAND_LBS] == 41336.25


def test_build_warns_when_customer_names_dim_missing():
    """A missing dim table surfaces a soft warning so the page can show it."""
    build = build_demand_order_item_customer(
        detail_df=_detail(),
        shipments_df=pd.DataFrame(),
        pdh_df=_pdh(),
        customer_names_dim=None,
        ship_to_sites_dim=_ship_to_sites_dim(),
        cy_actual_months=(),
    )
    assert any("dp_dimcustomernames" in w for w in build.warnings)


def test_build_warns_when_ship_to_sites_dim_missing():
    """A missing ship-to-sites dim surfaces a warning too — Base Plan
    rows can't get Customer No back-filled."""
    build = build_demand_order_item_customer(
        detail_df=_detail(),
        shipments_df=pd.DataFrame(),
        pdh_df=_pdh(),
        customer_names_dim=_customer_names_dim(),
        ship_to_sites_dim=None,
        cy_actual_months=(),
    )
    assert any("dp_dimshiptosites" in w for w in build.warnings)


# ─────────────────────────────────────────────────────────────────────────────
# 7) Long-format prep + filter discovery
# ─────────────────────────────────────────────────────────────────────────────

def test_prepare_demand_long_for_plr_produces_expected_columns():
    """The long frame the PLR builder consumes must carry pmaj/sfmt/brand/
    pminor/customer/customer_corp_group/month/pounds/forecast_type."""
    build = build_demand_order_item_customer(
        detail_df=_detail(),
        shipments_df=_shipments(),
        pdh_df=_pdh(),
        customer_names_dim=_customer_names_dim(),
        ship_to_sites_dim=_ship_to_sites_dim(),
        cy_actual_months=(date(2026, 4, 1),),
    )
    long = prepare_demand_long_for_plr(build.df, _pdh())
    expected = {
        "month", "pounds", "pmaj", "sfmt", "pminor", "brand",
        "customer", "customer_corp_group", "forecast_type",
    }
    assert expected.issubset(set(long.columns))
    # The Apr row's brand should be "Branded" (Item Description starts with "DG").
    apr = long.loc[long["month"] == date(2026, 4, 1)].iloc[0]
    assert apr["brand"] == "Branded"


def test_list_filter_values_from_demand_returns_uniques():
    df = pd.DataFrame({
        COL_PORTFOLIO_MAJOR: ["Butter", "Butter", "Cultured"],
        COL_SUPPLY_FORMAT: ["Print", "Quarters", "Cup"],
        COL_PORTFOLIO_MINOR: ["Packaged", "Packaged", "Sour Cream"],
    })
    fv = list_filter_values_from_demand(df)
    assert fv["portfolio_major"] == ["Butter", "Cultured"]
    assert fv["supply_format"] == ["Cup", "Print", "Quarters"]


def test_list_filter_values_for_pmaj_from_demand_cascades():
    df = pd.DataFrame({
        COL_PORTFOLIO_MAJOR: ["Butter", "Butter", "Cultured"],
        COL_SUPPLY_FORMAT: ["Print", "Quarters", "Cup"],
        COL_PORTFOLIO_MINOR: ["P1", "P2", "P3"],
    })
    fv = list_filter_values_for_pmaj_from_demand(df, "Butter")
    assert fv["supply_format"] == ["Print", "Quarters"]
    assert fv["portfolio_minor"] == ["P1", "P2"]
    # Cup belongs only to Cultured — must NOT appear under Butter.
    assert "Cup" not in fv["supply_format"]


# ─────────────────────────────────────────────────────────────────────────────
# Sanity: FORECAST_TYPE_BASE_PLAN constant is exported + matches detail CSV.
# ─────────────────────────────────────────────────────────────────────────────

def test_forecast_type_base_plan_constant_matches_detail_csv():
    """The detail CSV uses the literal "Base Plan" string; the module
    constant must match (case-sensitively) so the dispatch's
    case-insensitive comparison still works on the canonical form."""
    assert FORECAST_TYPE_BASE_PLAN == "Base Plan"


# ─────────────────────────────────────────────────────────────────────────────
# 8) Corporate-group canonicalisation (casing-drift de-duplication)
# ─────────────────────────────────────────────────────────────────────────────

def test_build_corp_group_canonical_map_picks_most_frequent_casing():
    """Multiple casings under the same casefold key → most-frequent wins.
    Ties broken by longest then first-seen.  A warning is emitted."""
    dim = pd.DataFrame({
        "customer_num": ["1", "2", "3", "4"],
        "customer_name": ["A", "B", "C", "D"],
        # 3× "Associated Foods" vs 1× "ASSOCIATED FOODS"  → mixed-case wins.
        "corporate_group": [
            "Associated Foods", "Associated Foods", "Associated Foods",
            "ASSOCIATED FOODS",
        ],
    })
    canonical, warnings = build_corp_group_canonical_map(dim)
    assert canonical["associated foods"] == "Associated Foods"
    assert warnings, "casing drift in the dim must surface a warning"
    assert "Associated Foods" in warnings[0]


def test_build_corp_group_canonical_map_clean_dim_yields_no_warnings():
    """A clean dim (1 surface form per casefold key) → no warning."""
    dim = pd.DataFrame({
        "customer_num": ["1", "2"],
        "customer_name": ["A", "B"],
        "corporate_group": ["Albertsons Safeway", "Costco"],
    })
    canonical, warnings = build_corp_group_canonical_map(dim)
    assert canonical == {
        "albertsons safeway": "Albertsons Safeway",
        "costco": "Costco",
    }
    assert warnings == ()


def test_build_corp_group_canonical_map_handles_missing_dim():
    """Returns an empty map + no warnings when the dim is unusable."""
    assert build_corp_group_canonical_map(None) == ({}, ())
    assert build_corp_group_canonical_map(pd.DataFrame()) == ({}, ())


def test_apply_corp_group_canonical_map_collapses_unknown_casefold_keys():
    """When ``extend_with_unknowns=True``, the first-seen surface form of
    a casefold key not already in the map wins for the entire Series.
    Subsequent casefold-equivalent values rewrite onto that first-seen
    form — which is exactly how the unified frame collapses fallback
    values that came from raw Customer Name."""
    s = pd.Series([
        "Far West Distributing",
        "FAR WEST DISTRIBUTING",
        "far west distributing",
    ])
    canonical = {}
    out, ext = apply_corp_group_canonical_map(
        s, canonical, extend_with_unknowns=True,
    )
    assert out.nunique() == 1
    assert out.iloc[0] == "Far West Distributing"
    assert ext["far west distributing"] == "Far West Distributing"


def test_apply_corp_group_canonical_map_strict_mode_leaves_unknowns_alone():
    """``extend_with_unknowns=False`` short-circuits — unknown casefold
    keys stay verbatim.  Useful for the page wanting to enforce ONLY
    the dim-seeded map without growing it further."""
    s = pd.Series(["UNKNOWN", "Unknown"])
    out, _ = apply_corp_group_canonical_map(
        s, {}, extend_with_unknowns=False,
    )
    assert out.tolist() == ["UNKNOWN", "Unknown"]


def test_build_emits_canonical_map_that_collapses_unified_frame_casings():
    """Mixed casing across detail (Base Plan) + shipments (Actual) must
    collapse to a single surface form in the unified frame so the PLR
    table's customer-row iteration stops double-counting."""
    # Detail has "Albertsons Safeway"; shipments has "ALBERTSONS SAFEWAY"
    # for the same customer_num.  Dim entry uses "Albertsons Safeway"
    # → that's the canonical winner.
    detail = pd.DataFrame([{
        COL_START_OF_MONTH: "2026-09-01", COL_ITEM: "310180",
        COL_ITEM_DESC: "DG Btr Qtr 1Lb 30cs",
        COL_CUSTOMER_NO: "", COL_CUSTOMER_NAME: "Albertsons Safeway",
        COL_PARTY_SITE_NUMBER: "10244", COL_DEMAND_LBS: 100.0,
        COL_FORECAST_TYPE: FORECAST_TYPE_BASE_PLAN,
        COL_PORTFOLIO_MAJOR: "Butter", COL_PORTFOLIO_MINOR: "Packaged Butter",
        COL_SUPPLY_FORMAT: "Western Quarters",
    }])
    shipments = pd.DataFrame([{
        "Item No": "310180", "Customer No": "C42",
        "Customer Name": "ALBERTSONS SAFEWAY",      # all-caps drift.
        "Month": "2026-04-01", "Shipped Qty lbs": 250.0,
    }])
    customer_dim = pd.DataFrame({
        "customer_num": ["C42"], "customer_name": ["Albertsons Safeway"],
        "corporate_group": ["Albertsons Safeway"],
    })
    ship_to = pd.DataFrame({
        "party_site_code": ["10244"], "customer_num": ["C42"],
    })
    build = build_demand_order_item_customer(
        detail_df=detail, shipments_df=shipments, pdh_df=_pdh(),
        customer_names_dim=customer_dim, ship_to_sites_dim=ship_to,
        cy_actual_months=(date(2026, 4, 1),),
    )
    # Both rows now carry the single canonical surface form.
    assert set(build.df[COL_CORPORATE_GROUP]) == {"Albertsons Safeway"}
    # The canonical map is exposed so the page can hand it to the
    # orders-side attach.
    assert build.canonical_corp_group_map["albertsons safeway"] == "Albertsons Safeway"


def test_attach_corporate_group_to_orders_applies_canonical_map():
    """The orders-side attach must adopt the same surface form the
    unified frame uses for the same casefold key — otherwise the PLR
    customer-row mask still splits the parent into duplicate rows."""
    orders = pd.DataFrame({
        "customer_no": ["C1", "C2"],
        "customer_name": ["KROGER COMPANY", "C&S WHOLESALE"],
    })
    dim = pd.DataFrame({
        "customer_num": ["C1"],
        "customer_name": ["Kroger Company"],
        "corporate_group": ["KROGER COMPANY"],     # dim happens to be UPPER.
    })
    canonical_map = {"kroger company": "Kroger Company"}  # unified-CSV's choice.
    out = attach_corporate_group_to_orders(
        orders, dim, canonical_map=canonical_map,
    )
    # First row: matched dim → rewritten by the canonical map.
    # Second row: dim miss → fallback to Customer Name → canonical
    # extension fixes a casing on first seen, but since it's a brand
    # new casefold key there's nothing to collapse to — verbatim.
    assert out["customer_corp_group"].tolist() == [
        "Kroger Company", "C&S WHOLESALE",
    ]


def test_attach_corporate_group_to_orders_without_map_falls_back_to_dim_casing():
    """Backwards-compat: when no canonical map is supplied (e.g. tests),
    the attach behaves as it did before the canonicalisation refactor —
    it adopts the dim's surface form verbatim."""
    orders = pd.DataFrame({
        "customer_no": ["C1"], "customer_name": ["whatever"],
    })
    dim = pd.DataFrame({
        "customer_num": ["C1"],
        "customer_name": ["Kroger Company"],
        "corporate_group": ["KROGER COMPANY"],
    })
    out = attach_corporate_group_to_orders(orders, dim, canonical_map=None)
    assert out["customer_corp_group"].iloc[0] == "KROGER COMPANY"
