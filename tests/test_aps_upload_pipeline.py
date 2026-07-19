"""Tests for the upload-driven APS pipeline (data layer, no Fabric)."""
from __future__ import annotations

import pandas as pd
import pytest

import datetime as dt

from data_sources.aps_upload_pipeline import (
    APS_HIST_COLUMNS,
    ApsUploadResult,
    CORP_SRC_EXACT,
    CORP_SRC_FUZZY,
    CORP_SRC_OVERRIDE,
    CORP_SRC_UNMAPPED,
    COL_CORP,
    COL_CORP_SRC,
    COL_CUSTOMER,
    COL_CYCLE,
    COL_DEMAND_LBS,
    COL_FORECAST,
    COL_FY,
    COL_INCLUSION,
    COL_ITEM,
    COL_MONTH,
    COL_PARTY,
    COL_SALES_LBS,
    _finalize,
    _shape_ro_history,
    _to_excel_serial,
    build_aps_history_rows,
    build_corp_review,
    parse_corp_override_csv,
    patch_history_corp,
    replace_cycle_fy_slice,
    today_inclusion_serial,
)


# ── serial helpers ───────────────────────────────────────────────────────────

def test_excel_serial_matches_known_anchors():
    assert _to_excel_serial(pd.Timestamp("2026-07-01")) == 46204
    assert _to_excel_serial(pd.Timestamp("2026-07-17")) == 46220
    assert pd.isna(_to_excel_serial(None))


def test_today_inclusion_serial_round_trips():
    serial = today_inclusion_serial()
    back = (pd.Timestamp("1899-12-30") + pd.Timedelta(days=serial)).date()
    assert back == pd.Timestamp.today().date()


# ── fixtures ─────────────────────────────────────────────────────────────────

def _pdh():
    # PDH: item code -> dims + Business Unit (B2C gate).  310180 is B2C butter;
    # 999999 is non-B2C and must be filtered out.
    return pd.DataFrame({
        "Item No": ["310180", "999999"],
        "Item Description": ["DG Btr Qtr 1Lb 30cs", "Bulk Raw Milk"],
        "Portfolio Major": ["Butter", "Bulk Fluid"],
        "Portfolio Minor": ["Packaged Butter", "Fresh Milk"],
        "Supply Format": ["Western Quarters", "Tanker"],
        "Business Unit": ["B2C", "Bulk"],
    })


def _plantosites():
    # plan_to_code -> customer_num ; PL1 bridges, PL_MISS does not.
    return pd.DataFrame({
        "plan_to_code": ["PL1"],
        "customer_num": ["CUST1"],
        "corporate_group": ["ignored"],
    })


def _customernames():
    return pd.DataFrame({
        "customer_num": ["CUST1"],
        "customer_name": ["Acme"],
        "corporate_group": ["Acme Foods"],
    })


def _upload():
    # Two B2C rows (bridge hit + native fallback) and one non-B2C row (dropped).
    return pd.DataFrame({
        "month": ["7/1/2026", "7/1/2026", "7/1/2026"],
        "party_site_code": ["10036", "10244", "55555"],
        "plan_to_code": ["PL1", "PL_MISS", "PL1"],
        "item_code": ["310180", "310180", "999999"],
        "item_description": ["DG Btr Qtr 1Lb 30cs", "DG Btr Qtr 1Lb 30cs", "Bulk Raw Milk"],
        "sales_forecast": ["1000", "500", "9999"],
        "consensus_forecast": ["1200", "600", "9999"],
        "corporate_group_code": ["NATIVE_A", "NATIVE_B", "NATIVE_X"],
    })


def _build(upload=None):
    rows, res = build_aps_history_rows(
        upload if upload is not None else _upload(),
        ro_seed_df=None, tbl_months_df=None,   # no R&O leg in the unit test
        pdh_df=_pdh(), ro_master_df=None,
        customer_names_df=_customernames(), plantosites_df=_plantosites(),
        cycle="C5", fy=2027, inclusion_serial=46220,
    )
    return rows, res


# ── transform ────────────────────────────────────────────────────────────────

def test_schema_and_stamps():
    rows, res = _build()
    assert list(rows.columns) == list(APS_HIST_COLUMNS)
    assert (rows[COL_CYCLE] == "C5").all()
    assert (rows[COL_FY] == 2027).all()
    assert (rows[COL_INCLUSION] == 46220).all()
    assert (rows[COL_FORECAST] == "APS Base Plan").all()
    # month "7/1/2026" -> first-of-month serial 46204.
    assert (rows[COL_MONTH] == 46204).all()


def test_b2c_filter_drops_non_b2c():
    rows, res = _build()
    # The non-B2C item (999999) is filtered out; only 310180 survives.
    assert set(rows[COL_ITEM]) == {"310180"}
    assert res.aps_rows == 2   # two B2C party-site rows


def test_corp_bridge_primary_native_fallback():
    rows, _ = _build()
    by_party = dict(zip(rows["Party Site Number"], rows[COL_CORP]))
    # 10036 -> PL1 -> CUST1 -> customernames "Acme Foods" (bridge wins).
    assert by_party["10036"] == "Acme Foods"
    # 10244 -> PL_MISS (no bridge) -> native code "NATIVE_B".
    assert by_party["10244"] == "NATIVE_B"


def test_pounds_and_dims_carry_through():
    rows, _ = _build()
    r = rows[rows["Party Site Number"] == "10036"].iloc[0]
    assert r[COL_SALES_LBS] == 1000.0 and r[COL_DEMAND_LBS] == 1200.0
    assert r["Portfolio Major"] == "Butter" and r["Supply Format"] == "Western Quarters"


def test_empty_upload_yields_no_rows():
    rows, res = _build(pd.DataFrame(columns=_upload().columns))
    assert rows.empty and res.aps_rows == 0


# ── upsert ───────────────────────────────────────────────────────────────────

def test_replace_cycle_fy_slice_is_idempotent():
    rows, _ = _build()
    # First insert.
    hist = replace_cycle_fy_slice(None, rows, "C5", 2027)
    assert len(hist) == len(rows)
    # Re-upload same cycle -> replaces, not doubles.
    hist2 = replace_cycle_fy_slice(hist, rows, "C5", 2027)
    assert len(hist2) == len(rows)
    # A different cycle appends.
    other = rows.assign(**{COL_CYCLE: "C6"})
    hist3 = replace_cycle_fy_slice(hist2, other, "C6", 2027)
    assert len(hist3) == len(rows) * 2
    assert set(hist3[COL_CYCLE]) == {"C5", "C6"}


# ── R&O corporate-group review + patch ───────────────────────────────────────

def _shape_ro_leg():
    """Shape a two-customer R&O leg: URM fuzzy, Kroger exact, 7-Eleven unmapped."""
    ro_detail = pd.DataFrame({
        "Start of Month": [dt.date(2026, 7, 1)] * 3,
        "Item": ["310180", "310180", "310180"],
        "Customer": ["URM", "Kroger", "7-Eleven"],
        "Demand Plan Pounds": [100.0, 200.0, 50.0],
    })
    cust_corp = {"URM": "DFS Gormet", "Kroger": "Kroger"}   # 7-Eleven unmapped
    cust_src = {"URM": CORP_SRC_FUZZY, "Kroger": CORP_SRC_EXACT,
                "7-Eleven": CORP_SRC_UNMAPPED}
    dim_maps = {"pmaj": {"310180": "Butter"}, "sfmt": {"310180": "Western Quarters"},
                "pminor": {"310180": "Packaged Butter"}, "desc": {"310180": "DG Btr"}}
    return _shape_ro_history(ro_detail, cust_corp, cust_src, dim_maps)


def _history_with_ro():
    """A finalized history frame: one APS Base Plan row + the shaped R&O leg."""
    aps_leg = pd.DataFrame({
        COL_MONTH: [46204], COL_ITEM: ["310180"], "Item Description": ["DG Btr"],
        COL_PARTY: ["10036"], COL_SALES_LBS: [50.0], COL_DEMAND_LBS: [60.0],
        COL_FORECAST: ["APS Base Plan"], "Portfolio Major": ["Butter"],
        "Portfolio Minor": ["Packaged Butter"], "Supply Format": ["Western Quarters"],
        COL_CORP: ["Costco"], COL_CUSTOMER: [""], COL_CORP_SRC: ["bridge"],
    })
    return _finalize([aps_leg, _shape_ro_leg()], "C5", 2027, 46220)


def test_shape_ro_history_keeps_customer_and_source():
    leg = _shape_ro_leg()
    assert (leg[COL_FORECAST] == "R&O").all()
    by_cust = dict(zip(leg[COL_CUSTOMER], zip(leg[COL_CORP], leg[COL_CORP_SRC])))
    assert by_cust["URM"] == ("DFS Gormet", CORP_SRC_FUZZY)
    assert by_cust["Kroger"] == ("Kroger", CORP_SRC_EXACT)
    # Unmapped customer falls back to the (Unmapped) sentinel corp group.
    assert by_cust["7-Eleven"][1] == CORP_SRC_UNMAPPED
    # R&O rows carry no party site / sales forecast, and dims backfill from dims.
    assert (leg[COL_PARTY] == "").all() and (leg[COL_SALES_LBS] == 0.0).all()
    assert (leg["Portfolio Major"] == "Butter").all()


def test_parse_corp_override_csv():
    csv = (
        "Customer,Corporate Group,Match\n"
        "URM,URM,Fuzzy\n"
        "Costco,,Exact\n"            # blank -> skipped
        "HEB,(Unmapped),Exact\n"     # Unmapped -> skipped
        "Kroger,Kroger Co,Exact\n"
    ).encode("utf-8")
    assert parse_corp_override_csv(csv) == {"URM": "URM", "Kroger": "Kroger Co"}


def test_build_corp_review_lists_only_reviewable_unmapped_first():
    review = build_corp_review(_history_with_ro())
    # Kroger (exact) is NOT reviewable; URM (fuzzy) + 7-Eleven (unmapped) are.
    assert list(review.columns) == ["Customer", "Corporate Group", "Match"]
    assert list(review["Customer"]) == ["7-Eleven", "URM"]   # Unmapped first
    assert list(review["Match"]) == ["Unmapped", "Fuzzy"]


def test_build_corp_review_empty_without_columns():
    # A legacy history frame missing Customer / Corp Source yields nothing to review.
    bare = pd.DataFrame({COL_FORECAST: ["R&O"], COL_CORP: ["X"]})
    assert build_corp_review(bare).empty
    assert build_corp_review(None).empty


def test_patch_history_corp_targets_reviewable_ro_only(monkeypatch):
    """The mutator patches only reviewable R&O rows; exact / prior rows untouched."""
    import data_sources.aps_upload_pipeline as aps

    captured = {}

    def fake_update_csv(section, blob, mutator, *, initial_default=None, verify=True):
        out = mutator(_history_with_ro())
        captured["out"] = out
        return out

    monkeypatch.setattr(aps, "update_csv", fake_update_csv)
    patched, total = aps.patch_history_corp({"URM": "URM Inc", "7-Eleven": "7-Eleven",
                                             "Kroger": "SHOULD NOT APPLY"})
    out = captured["out"]
    by_cust = dict(zip(out[COL_CUSTOMER], zip(out[COL_CORP], out[COL_CORP_SRC])))
    assert by_cust["URM"] == ("URM Inc", CORP_SRC_OVERRIDE)         # fuzzy -> patched
    assert by_cust["7-Eleven"] == ("7-Eleven", CORP_SRC_OVERRIDE)   # unmapped -> patched
    assert by_cust["Kroger"] == ("Kroger", CORP_SRC_EXACT)          # exact -> untouched
    assert patched == 2 and total == len(out)


def test_patch_history_corp_noop_without_overrides():
    # No clean overrides -> returns early without touching Fabric.
    assert patch_history_corp({}) == (0, 0)
    assert patch_history_corp(None) == (0, 0)
