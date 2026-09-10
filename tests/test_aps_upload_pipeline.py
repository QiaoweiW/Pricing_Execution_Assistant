"""Tests for the upload-driven APS pipeline (data layer, no Fabric)."""
from __future__ import annotations

import pandas as pd
import pytest

import datetime as dt

from data_sources.aps_upload_pipeline import (
    APS_HIST_COLUMNS,
    CORP_SRC_CUSTOMER,
    CORP_SRC_EXACT,
    CORP_SRC_FUZZY,
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
    FORECAST_APS_BASE_PLAN,
    FORECAST_R_AND_O,
    apply_customer_corp_default,
    build_aps_history_rows,
    list_aps_history_forecast_types,
    replace_cycle_fy_forecast_slice,
    summarise_history,
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

def test_replace_cycle_fy_forecast_slice_is_leg_scoped():
    rows, _ = _build()   # Base Plan rows only (ro_seed_df=None)
    types = (FORECAST_APS_BASE_PLAN,)
    # First insert.
    hist = replace_cycle_fy_forecast_slice(None, rows, "C5", 2027, types)
    assert len(hist) == len(rows)
    # Re-upload the same leg -> replaces, not doubles.
    hist2 = replace_cycle_fy_forecast_slice(hist, rows, "C5", 2027, types)
    assert len(hist2) == len(rows)
    # An R&O leg for the SAME cycle appends (different Forecast Type, left intact).
    ro = rows.assign(**{COL_FORECAST: FORECAST_R_AND_O}).head(1)
    hist3 = replace_cycle_fy_forecast_slice(hist2, ro, "C5", 2027, (FORECAST_R_AND_O,))
    assert len(hist3) == len(rows) + 1
    assert set(hist3[COL_FORECAST]) == {FORECAST_APS_BASE_PLAN, FORECAST_R_AND_O}
    # Re-uploading the Base Plan leg does NOT drop the R&O rows.
    hist4 = replace_cycle_fy_forecast_slice(hist3, rows, "C5", 2027, types)
    assert (hist4[COL_FORECAST] == FORECAST_R_AND_O).sum() == 1
    # A different cycle appends.
    other = rows.assign(**{COL_CYCLE: "C6"})
    hist5 = replace_cycle_fy_forecast_slice(hist4, other, "C6", 2027, types)
    assert set(hist5[COL_CYCLE]) == {"C5", "C6"}


def test_delete_history_slice_targets_cycle_fy_forecast(monkeypatch):
    import data_sources.aps_upload_pipeline as aps
    rows, _ = _build()   # Base Plan rows for C5/2027
    ro = rows.assign(**{COL_FORECAST: FORECAST_R_AND_O}).head(1)
    c6 = rows.assign(**{COL_CYCLE: "C6"})
    hist = pd.concat([rows, ro, c6], ignore_index=True)

    def fake_update_csv(section, blob, mutator, *, initial_default=None, verify=True):
        return mutator(hist)

    monkeypatch.setattr(aps, "update_csv", fake_update_csv)
    # Delete only the C5/2027 R&O leg.
    deleted, total = aps.delete_history_slice("C5", 2027, (FORECAST_R_AND_O,))
    assert deleted == 1 and total == len(hist) - 1
    # Delete ALL of C5/2027 (forecast_types=None) — C6 rows survive.
    deleted2, total2 = aps.delete_history_slice("C5", 2027, None)
    assert deleted2 == len(rows) + 1 and total2 == len(c6)


def test_list_aps_history_forecast_types():
    rows, _ = _build()
    ro = rows.assign(**{COL_FORECAST: FORECAST_R_AND_O}).head(1)
    hist = pd.concat([rows, ro], ignore_index=True)
    assert list_aps_history_forecast_types(hist) == sorted(
        {FORECAST_APS_BASE_PLAN, FORECAST_R_AND_O})
    assert list_aps_history_forecast_types(None) == []


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
    # An unmapped customer no longer lands as "(Unmapped)" waiting for a human:
    # it takes its own name as the group, stamped so we know how it got there.
    assert by_cust["7-Eleven"] == ("7-Eleven", CORP_SRC_CUSTOMER)
    # R&O rows carry no party site / sales forecast, and dims backfill from dims.
    assert (leg[COL_PARTY] == "").all() and (leg[COL_SALES_LBS] == 0.0).all()
    assert (leg["Portfolio Major"] == "Butter").all()


# ── Corporate groups resolve themselves (replaces the manual review+patch) ───

def test_unmapped_ro_rows_take_the_customer_name():
    out, n = apply_customer_corp_default(_history_with_ro())
    by_cust = dict(zip(out[COL_CUSTOMER], zip(out[COL_CORP], out[COL_CORP_SRC])))
    # _shape_ro_history already filled 7-Eleven, so the frame arrives clean.
    assert n == 0
    assert by_cust["7-Eleven"] == ("7-Eleven", CORP_SRC_CUSTOMER)


def test_a_legacy_unmapped_row_is_healed_in_place():
    """Rows written before this rule existed are fixed on the next write."""
    legacy = pd.DataFrame({
        COL_FORECAST: [FORECAST_R_AND_O],
        COL_CORP: ["(Unmapped)"], COL_CUSTOMER: ["Walgreens"],
        COL_CORP_SRC: [CORP_SRC_UNMAPPED],
    })
    out, n = apply_customer_corp_default(legacy)
    assert n == 1
    assert out.loc[0, COL_CORP] == "Walgreens"
    assert out.loc[0, COL_CORP_SRC] == CORP_SRC_CUSTOMER


def test_fuzzy_matches_are_left_alone():
    """A fuzzy row already holds the spelling the BASE PLAN uses.

    Overwriting "SMART AND FINAL" with the seed's "Smart & Final" would split
    one corporate group into two and break the roll-up — measured on the live
    file, doing so would have broken 2 groups to fix 1.  So fuzzy is untouched.
    """
    fuzzy = pd.DataFrame({
        COL_FORECAST: [FORECAST_R_AND_O],
        COL_CORP: ["SMART AND FINAL"], COL_CUSTOMER: ["Smart & Final"],
        COL_CORP_SRC: [CORP_SRC_FUZZY],
    })
    out, n = apply_customer_corp_default(fuzzy)
    assert n == 0 and out.loc[0, COL_CORP] == "SMART AND FINAL"


def test_base_plan_rows_are_never_touched():
    """Base-plan rows carry no Customer at all — there is nothing to inherit."""
    base = pd.DataFrame({
        COL_FORECAST: [FORECAST_APS_BASE_PLAN],
        COL_CORP: ["(Unmapped)"], COL_CUSTOMER: [""],
        COL_CORP_SRC: [CORP_SRC_UNMAPPED],
    })
    out, n = apply_customer_corp_default(base)
    assert n == 0 and out.loc[0, COL_CORP] == "(Unmapped)"


def test_a_blank_customer_has_nothing_to_paste():
    """Better an honest (Unmapped) than an empty-string corporate group."""
    blank = pd.DataFrame({
        COL_FORECAST: [FORECAST_R_AND_O],
        COL_CORP: ["(Unmapped)"], COL_CUSTOMER: ["   "],
        COL_CORP_SRC: [CORP_SRC_UNMAPPED],
    })
    out, n = apply_customer_corp_default(blank)
    assert n == 0 and out.loc[0, COL_CORP] == "(Unmapped)"


def test_the_fill_is_idempotent():
    """Runs on every upload, so a second pass must be a no-op."""
    legacy = pd.DataFrame({
        COL_FORECAST: [FORECAST_R_AND_O], COL_CORP: ["(Unmapped)"],
        COL_CUSTOMER: ["Raley's"], COL_CORP_SRC: [CORP_SRC_UNMAPPED],
    })
    once, n1 = apply_customer_corp_default(legacy)
    twice, n2 = apply_customer_corp_default(once)
    assert (n1, n2) == (1, 0)
    pd.testing.assert_frame_equal(once, twice)


@pytest.mark.parametrize("frame", [None, pd.DataFrame(),
                                   pd.DataFrame({"Item": ["1"]})])
def test_the_fill_survives_junk_input(frame):
    out, n = apply_customer_corp_default(frame)
    assert n == 0 and out is not None


def test_the_fill_does_not_copy_when_nothing_matches():
    """A no-op must not clone a million-row frame."""
    clean = pd.DataFrame({
        COL_FORECAST: [FORECAST_R_AND_O], COL_CORP: ["Kroger"],
        COL_CUSTOMER: ["Kroger"], COL_CORP_SRC: [CORP_SRC_EXACT],
    })
    out, n = apply_customer_corp_default(clean)
    assert n == 0 and out is clean


# ── Step 2's "what landed" summary ──────────────────────────────────────────

def test_history_summary_counts_each_leg_and_the_span():
    summary = summarise_history(_history_with_ro())
    assert list(summary.columns) == [
        "Cycle", "Base Plan rows", "R&O rows", "Total rows", "Plan covers"]
    row = summary.iloc[0]
    assert row["Cycle"] == "C5"
    assert row["Base Plan rows"] == 1
    assert row["R&O rows"] == 3
    assert row["Total rows"] == 4
    assert row["Plan covers"] == "Jul 2026 → Jul 2026"


def test_history_summary_puts_the_newest_cycle_first():
    """The planner has just uploaded the newest cycle; show it at the top."""
    old = _finalize([_shape_ro_leg()], "C4", 2027, 46220)
    new = _finalize([_shape_ro_leg()], "C5", 2027, 46220)
    new[COL_MONTH] = 46234                       # a later horizon than C4's
    summary = summarise_history(pd.concat([old, new], ignore_index=True))
    assert list(summary["Cycle"]) == ["C5", "C4"]


def test_history_summary_is_empty_without_history():
    for empty in (None, pd.DataFrame()):
        out = summarise_history(empty)
        assert out.empty and "Cycle" in out.columns
