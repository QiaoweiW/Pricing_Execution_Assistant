"""Unit tests for the Holistic Demand Plan (APS) builder.

`build_holistic_demand_plan_aps` merges the APS Base Plan leg
(dp_factscurrentaps) with the R&O leg (RO_Seed expansion) into one unified
frame: Month | Item | Corporate Group | Demand Plan Pounds | Forecast Type.
Fixtures are synthetic DataFrames — no Fabric/Streamlit session needed.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from data_sources.holistic_demand_plan_aps import (
    FORECAST_APS_BASE_PLAN,
    FORECAST_R_AND_O,
    HDP_COL_CORP,
    HDP_COL_FORECAST,
    HDP_COL_ITEM,
    HDP_COL_MONTH,
    HDP_COL_POUNDS,
    HDP_COLUMNS,
    MATCH_COL_CORP,
    MATCH_COL_CUSTOMER,
    MATCH_COL_STATUS,
    MATCH_EXACT,
    MATCH_FUZZY,
    MATCH_OVERRIDE,
    MATCH_UNMAPPED,
    apply_customer_corp_overrides,
    build_holistic_demand_plan_aps,
    filter_needs_review,
)

_ANCHOR = dt.date(2026, 4, 1)


def _aps_df() -> pd.DataFrame:
    """dp_factscurrentaps projection (internal names) — mirrors screenshot 1."""
    return pd.DataFrame({
        "month": ["2026-07-01", "2026-07-01"],
        "item_code": ["380574", "380574"],           # same key → should sum
        "plan_lbs": ["23", "2"],
        "corporate_group": ["URM", "URM"],
    })


def _ro_seed_df() -> pd.DataFrame:
    """RO_Seed rows: Winco (maps), Albertsons Safeway (fuzzy), Mystery Mart (unmapped)."""
    base = dict(
        Taxonomy="Packaged Butter", Brand="Branded", Item_Desc="X",
        Probability="1.0", First_Ship_Date="2026-01-01",
        Lbs_yr="365000", PC_yr="0", Slotting="0",
    )
    rows = [
        {"Format": "Western Quarters", "Customer": "Winco", "Item #": "310180"},
        {"Format": "Aseptic", "Customer": "Albertsons Safeway", "Item #": "66"},
        {"Format": "Aseptic", "Customer": "Mystery Mart", "Item #": "999"},
    ]
    return pd.DataFrame([{
        "Format": r["Format"], "Customer": r["Customer"], "Taxonomy": base["Taxonomy"],
        "Brand": base["Brand"], "Item #": r["Item #"], "Item Desc": base["Item_Desc"],
        "Probability": base["Probability"], "First Ship Date": base["First_Ship_Date"],
        "Lbs./yr": base["Lbs_yr"], "PC$/yr": base["PC_yr"], "Slotting": base["Slotting"],
    } for r in rows])


def _tbl_months_df() -> pd.DataFrame:
    # NB: the pipeline melts the wide columns to "Month 1".."Month 36" and
    # merges that against tblMonths' "Month Number", so Month Number carries
    # the "Month N" form (not a bare integer).
    rows = []
    for n in range(1, 37):
        month = _ANCHOR + pd.DateOffset(months=n - 1)
        rows.append({"Month Number": f"Month {n}",
                     "Start of Month": month.strftime("%Y-%m-%d")})
    return pd.DataFrame(rows)


def _pdh_df() -> pd.DataFrame:
    return pd.DataFrame({
        "Item No": ["310180", "66", "999"],
        "Business Unit": ["B2C", "B2C", "B2C"],
    })


def _ro_master_df() -> pd.DataFrame:
    return pd.DataFrame({"Item #": ["310180", "66", "999"]})


def _customer_names_df() -> pd.DataFrame:
    return pd.DataFrame({
        "customer_num": ["1", "2"],
        # "Winco Foods" → seed "Winco" resolves by CONTAINMENT (Fuzzy);
        # "Albertsons/Safeway" → seed "Albertsons Safeway" is EXACT once
        # punctuation is normalised to a space.
        "customer_name": ["Winco Foods", "Albertsons/Safeway"],
        "corporate_group": ["WINCO", "ALBERTSONS"],
    })


def _build(**overrides):
    kwargs = dict(
        aps_df=_aps_df(), ro_seed_df=_ro_seed_df(), tbl_months_df=_tbl_months_df(),
        pdh_df=_pdh_df(), ro_master_df=_ro_master_df(),
        customer_names_df=_customer_names_df(), anchor_month=_ANCHOR,
    )
    kwargs.update(overrides)
    return build_holistic_demand_plan_aps(**kwargs)


def test_output_schema_and_forecast_types():
    res = _build()
    assert list(res.frame.columns) == list(HDP_COLUMNS)
    assert set(res.frame[HDP_COL_FORECAST]) == {FORECAST_APS_BASE_PLAN, FORECAST_R_AND_O}


def test_aps_leg_sums_and_tags():
    res = _build()
    aps = res.frame[res.frame[HDP_COL_FORECAST] == FORECAST_APS_BASE_PLAN]
    assert len(aps) == 1                       # two source rows summed to one
    row = aps.iloc[0]
    assert str(row[HDP_COL_ITEM]) == "380574"
    assert row[HDP_COL_CORP] == "URM"          # native corporate_group_code kept
    assert float(row[HDP_COL_POUNDS]) == 25.0  # 23 + 2
    assert pd.Timestamp(row[HDP_COL_MONTH]) == pd.Timestamp("2026-07-01")


def test_ro_leg_corp_group_fuzzy_and_pounds():
    res = _build()
    ro = res.frame[res.frame[HDP_COL_FORECAST] == FORECAST_R_AND_O]
    corp_by_item = dict(zip(ro[HDP_COL_ITEM].astype(str), ro[HDP_COL_CORP]))
    assert corp_by_item["310180"] == "WINCO"          # fuzzy: "Winco" ⊂ "Winco Foods"
    assert corp_by_item["66"] == "ALBERTSONS"          # exact after normalising "/"→" "
    assert corp_by_item["999"] == "(Unmapped)"         # no match
    # April 2026 (Month 1) Winco/310180 = 1.0 * 365000 / 365 * 30 days = 30000.
    apr = ro[(ro[HDP_COL_ITEM].astype(str) == "310180")
             & (pd.to_datetime(ro[HDP_COL_MONTH]) == pd.Timestamp("2026-04-01"))]
    assert float(apr[HDP_COL_POUNDS].iloc[0]) == 30000.0


def test_unmapped_customers_reported():
    res = _build()
    assert res.unmapped_customers == ("Mystery Mart",)


def test_customer_match_log_classifies_and_sorts():
    res = _build()
    log = res.customer_match_log
    status = dict(zip(log[MATCH_COL_CUSTOMER], log[MATCH_COL_STATUS]))
    corp = dict(zip(log[MATCH_COL_CUSTOMER], log[MATCH_COL_CORP]))
    assert status["Winco"] == MATCH_FUZZY and corp["Winco"] == "WINCO"
    assert status["Albertsons Safeway"] == MATCH_EXACT
    assert corp["Albertsons Safeway"] == "ALBERTSONS"
    assert status["Mystery Mart"] == MATCH_UNMAPPED
    assert corp["Mystery Mart"] == "(Unmapped)"
    # Most-actionable first: the Unmapped row leads the log.
    assert log.iloc[0][MATCH_COL_STATUS] == MATCH_UNMAPPED


def test_non_b2c_items_dropped():
    # Item 66 loses its PDH B2C flag AND is absent from RO_Item_Master → dropped.
    pdh = pd.DataFrame({"Item No": ["310180", "999"], "Business Unit": ["B2C", "B2C"]})
    ro_master = pd.DataFrame({"Item #": ["310180", "999"]})
    res = _build(pdh_df=pdh, ro_master_df=ro_master)
    ro_items = set(
        res.frame.loc[res.frame[HDP_COL_FORECAST] == FORECAST_R_AND_O, HDP_COL_ITEM]
        .astype(str))
    assert "66" not in ro_items
    assert {"310180", "999"} <= ro_items


def test_empty_aps_still_builds_ro():
    res = _build(aps_df=pd.DataFrame(columns=["month", "item_code", "plan_lbs", "corporate_group"]))
    assert res.aps_rows == 0
    assert res.ro_rows > 0
    assert set(res.frame[HDP_COL_FORECAST]) == {FORECAST_R_AND_O}


def _ro_corp_by_item(res) -> dict:
    ro = res.frame[res.frame[HDP_COL_FORECAST] == FORECAST_R_AND_O]
    return dict(zip(ro[HDP_COL_ITEM].astype(str), ro[HDP_COL_CORP]))


def test_filter_needs_review_hides_confident_exacts():
    res = _build()
    review = filter_needs_review(res.customer_match_log)
    custs = set(review[MATCH_COL_CUSTOMER])
    assert "Winco" in custs             # Fuzzy → needs review
    assert "Mystery Mart" in custs      # Unmapped → needs review
    assert "Albertsons Safeway" not in custs  # Exact w/ real corp → hidden


def test_apply_overrides_remaps_customer_and_relabels_log():
    res = _build()
    out = apply_customer_corp_overrides(res, {"Winco": "WINCO GROUP"})
    # Every R&O row of Winco's item picks up the override.
    assert _ro_corp_by_item(out)["310180"] == "WINCO GROUP"
    # Log row is re-tagged Override with the new group.
    row = out.customer_match_log.set_index(MATCH_COL_CUSTOMER).loc["Winco"]
    assert row[MATCH_COL_STATUS] == MATCH_OVERRIDE
    assert row[MATCH_COL_CORP] == "WINCO GROUP"
    # APS leg is untouched by an R&O override.
    aps = out.frame[out.frame[HDP_COL_FORECAST] == FORECAST_APS_BASE_PLAN]
    assert float(aps.iloc[0][HDP_COL_POUNDS]) == 25.0
    # Original result is not mutated.
    assert _ro_corp_by_item(res)["310180"] == "WINCO"


def test_apply_overrides_can_map_a_previously_unmapped_customer():
    res = _build()
    assert res.unmapped_customers == ("Mystery Mart",)
    out = apply_customer_corp_overrides(res, {"Mystery Mart": "MYSTERY CORP"})
    assert _ro_corp_by_item(out)["999"] == "MYSTERY CORP"
    assert out.unmapped_customers == ()


def test_apply_overrides_is_noop_for_empty_or_blank():
    res = _build()
    assert apply_customer_corp_overrides(res, {}) is res
    assert apply_customer_corp_overrides(res, {"Winco": "  "}) is res
