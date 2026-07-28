"""Regression tests for date coercion in demand_plan_comparison.

Focus: ``_vectorised_start_of_month`` must first-of-month-floor EVERY input
shape it can receive — Excel day-serials, date strings, and (the shape that
regressed and zeroed out **Total Actuals / PM Actual**) an already-typed
``datetime64`` column, which is exactly what DuckDB returns for the IBP
Shipments ``Month``.  When that column parsed to all-NaT the actuals frame
was emptied by the downstream ``dropna(subset=["month"])``.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from data_sources.demand_plan_comparison import (
    COL_LAST_PLAN_ACTUALS,
    COL_LAST_PLAN_FORECAST,
    COL_PRIOR_MONTH_ACTUAL,
    COL_PRIOR_MONTH_FORECAST,
    DIAG_COL_LBS,
    DIAG_COL_PMAJ,
    DIAG_COL_SFMT,
    DIAG_UNMAPPED,
    CNC_COL_SHIPPED_M,
    FORECAST_BASE_PLAN,
    FORECAST_R_AND_O,
    NC_COL_ITEM,
    NC_COL_PMAJ,
    TRK_ITEM,
    TRK_ITEM_DESCRIPTION,
    TRK_PMAJ,
    TRK_PMINOR,
    TRK_SFMT,
    ComparisonFilters,
    ComparisonNotCaptured,
    EnrichedSources,
    TemplateRow,
    _compute_leaf_measures,
    build_business_health,
    build_sku_cycle_comparison,
    build_comparison_not_captured,
    build_demand_plan_comparison,
    build_comparison_kpis,
    build_item_dim_frame_from_tracker,
    build_prior_month_shipment_diagnostic,
    list_tracker_dim_values,
    tracker_has_dim_columns,
    _vectorised_start_of_month as som,
)


def test_datetime64_month_floors_not_nat():
    """The regression: datetime64 input must floor to month, never NaT."""
    out = som(pd.Series(pd.to_datetime(["2025-01-15", "2025-07-01"])))
    assert out.tolist() == [dt.date(2025, 1, 1), dt.date(2025, 7, 1)]
    assert out.notna().all()


def test_tz_aware_datetime64_month():
    s = pd.to_datetime(pd.Series(["2025-03-20"])).dt.tz_localize("UTC")
    assert som(s).tolist() == [dt.date(2025, 3, 1)]


def test_excel_serial_month_still_parses():
    # 45658 = 2025-01-01, 45689 = 2025-02-01.
    assert som(pd.Series([45658, 45689])).tolist() == [
        dt.date(2025, 1, 1), dt.date(2025, 2, 1),
    ]


def test_date_strings_and_garbage():
    out = som(pd.Series(["2025-01-10", "not a date", ""]))
    assert out.tolist() == [dt.date(2025, 1, 1), None, None]


def test_contaminated_serial_is_nat_not_crash():
    """An absurd out-of-window magnitude coerces to None, never raises."""
    out = som(pd.Series([1e19, 45658]))
    assert out.tolist() == [None, dt.date(2025, 1, 1)]


# ── Prior Month Forecast uses the PRIOR cycle (not the current cycle) ────────

def _enriched_trk(rows: list[dict]) -> pd.DataFrame:
    cols = ["item_key", "item_desc", "party_site", "month", "pounds",
            "forecast_type", "cycle", "pmaj", "sfmt", "pminor", "brand"]
    return pd.DataFrame(rows, columns=cols)


def _enriched_ibp(rows: list[dict]) -> pd.DataFrame:
    cols = ["item_key", "item_desc", "customer_no", "customer_name", "month",
            "pounds", "pmaj", "sfmt", "pminor", "brand"]
    return pd.DataFrame(rows, columns=cols)


def test_prior_month_forecast_uses_prior_cycle():
    """PM Forecast must sum the PRIOR cycle (C3), never the current (C4)."""
    prior_month = dt.date(2026, 6, 1)
    filters = ComparisonFilters(
        current_cycle="C4", prior_cycle="C3",
        actual_start=dt.date(2026, 4, 1), actual_end=dt.date(2026, 6, 1),
        forecast_start=dt.date(2026, 7, 1), forecast_end=dt.date(2027, 3, 1),
        prior_month=prior_month,
    )
    base = dict(item_key="100", item_desc="X", party_site="1",
                pmaj="", sfmt="", pminor="", brand="", forecast_type=FORECAST_BASE_PLAN)
    trk = _enriched_trk([
        {**base, "month": prior_month, "cycle": "C3", "pounds": 3_000_000.0},  # prior → used
        {**base, "month": prior_month, "cycle": "C4", "pounds": 9_000_000.0},  # current → ignored
    ])
    ibp = _enriched_ibp([
        {"item_key": "100", "item_desc": "X", "customer_no": "1",
         "customer_name": "C", "month": prior_month, "pounds": 5_000_000.0,
         "pmaj": "", "sfmt": "", "pminor": "", "brand": ""},
    ])
    tpl = TemplateRow(row_id="r", label="R", indent=0)  # no dim constraints

    m = _compute_leaf_measures(
        tpl, trk, ibp, filters,
        actual_months={dt.date(2026, 4, 1), dt.date(2026, 5, 1), prior_month},
        forecast_months={dt.date(2026, 7, 1)},
        last_actual_months={dt.date(2026, 4, 1), dt.date(2026, 5, 1)},
        prior_forecast_months={dt.date(2026, 6, 1), dt.date(2026, 7, 1)},
        prior_month=prior_month,
        ro_total_delta_by_path={},
    )
    # Prior-cycle C3 (3.0 M lbs), NOT current-cycle C4 (9.0).
    assert m[COL_PRIOR_MONTH_FORECAST] == 3.0
    assert m[COL_PRIOR_MONTH_ACTUAL] == 5.0


# ── Prior-month shipment diagnostic ──────────────────────────────────────────

def test_shipment_diagnostic_splits_by_pmaj_and_format():
    prior_month = dt.date(2026, 6, 1)
    ibp = _enriched_ibp([
        # ESL splits across formats: LC + Aerosol roll into the ESL line,
        # Aseptic is a SEPARATE line — the diagnostic must show them apart.
        {"item_key": "1", "item_desc": "", "customer_no": "", "customer_name": "",
         "month": prior_month, "pounds": 10_000_000.0,
         "pmaj": "ESL", "sfmt": "Large Carton", "pminor": "", "brand": ""},
        {"item_key": "2", "item_desc": "", "customer_no": "", "customer_name": "",
         "month": prior_month, "pounds": 7_000_000.0,
         "pmaj": "ESL", "sfmt": "Aseptic", "pminor": "", "brand": ""},
        # Item missing from PDH → blank pmaj/sfmt → surfaces as "(unmapped)".
        {"item_key": "3", "item_desc": "", "customer_no": "", "customer_name": "",
         "month": prior_month, "pounds": 1_000_000.0,
         "pmaj": "", "sfmt": "", "pminor": "", "brand": ""},
        # A different month must be excluded.
        {"item_key": "4", "item_desc": "", "customer_no": "", "customer_name": "",
         "month": dt.date(2026, 5, 1), "pounds": 99_000_000.0,
         "pmaj": "ESL", "sfmt": "Large Carton", "pminor": "", "brand": ""},
    ])
    out = build_prior_month_shipment_diagnostic(ibp, prior_month)

    # Only June rows; May excluded.
    assert float(out[DIAG_COL_LBS].sum()) == 18_000_000.0
    lookup = {(r[DIAG_COL_PMAJ], r[DIAG_COL_SFMT]): r[DIAG_COL_LBS]
              for _, r in out.iterrows()}
    assert lookup[("ESL", "Large Carton")] == 10_000_000.0
    assert lookup[("ESL", "Aseptic")] == 7_000_000.0
    assert lookup[(DIAG_UNMAPPED, DIAG_UNMAPPED)] == 1_000_000.0
    # Total ESL (all formats) = 17M, which exceeds the page's ESL line
    # (Large Carton only here, 10M) because Aseptic is broken out — the
    # exact reconciliation gap the diagnostic exists to expose.
    esl = out.loc[out[DIAG_COL_PMAJ] == "ESL", DIAG_COL_LBS].sum()
    assert float(esl) == 17_000_000.0


def test_shipment_diagnostic_empty_when_no_prior_month_rows():
    ibp = _enriched_ibp([
        {"item_key": "1", "item_desc": "", "customer_no": "", "customer_name": "",
         "month": dt.date(2026, 5, 1), "pounds": 5.0,
         "pmaj": "ESL", "sfmt": "Large Carton", "pminor": "", "brand": ""},
    ])
    out = build_prior_month_shipment_diagnostic(ibp, dt.date(2026, 6, 1))
    assert out.empty


# ── Last Plan (independent, one-month-shifted) + Total Delta = Current − Last ─

_APR, _MAY, _JUN, _JUL = (
    dt.date(2026, 4, 1), dt.date(2026, 5, 1),
    dt.date(2026, 6, 1), dt.date(2026, 7, 1),
)


def _filters_apr_jun_actual() -> ComparisonFilters:
    """Actual Apr–Jun, Forecast Jul–Mar, current C4 vs prior C3, PM = Jun."""
    return ComparisonFilters(
        current_cycle="C4", prior_cycle="C3",
        actual_start=_APR, actual_end=_JUN,
        forecast_start=_JUL, forecast_end=dt.date(2027, 3, 1),
        prior_month=_JUN,
    )


def test_last_plan_measures_use_shifted_windows():
    """Last-Plan legs shift one month: actuals drop Jun; prior forecast adds Jun."""
    filters = _filters_apr_jun_actual()
    base = dict(item_key="100", item_desc="X", party_site="1",
                pmaj="", sfmt="", pminor="", brand="", forecast_type=FORECAST_BASE_PLAN)
    trk = _enriched_trk([
        {**base, "month": _JUN, "cycle": "C3", "pounds": 8_000_000.0},  # prior, in prior-fcst window
        {**base, "month": _JUL, "cycle": "C3", "pounds": 9_000_000.0},  # prior, in prior-fcst window
        {**base, "month": _JUL, "cycle": "C4", "pounds": 10_000_000.0},  # current forecast
    ])
    ibp = _enriched_ibp([
        {"item_key": "100", "item_desc": "X", "customer_no": "1", "customer_name": "C",
         "month": m, "pounds": p, "pmaj": "", "sfmt": "", "pminor": "", "brand": ""}
        for m, p in ((_APR, 1_000_000.0), (_MAY, 2_000_000.0), (_JUN, 4_000_000.0))
    ])
    tpl = TemplateRow(row_id="r", label="R", indent=0)  # no dim constraints

    m = _compute_leaf_measures(
        tpl, trk, ibp, filters,
        actual_months={_APR, _MAY, _JUN},
        forecast_months={_JUL},
        last_actual_months={_APR, _MAY},          # Jun dropped
        prior_forecast_months={_JUN, _JUL},        # Jun added (Forecast Start − 1)
        prior_month=_JUN,
        ro_total_delta_by_path={},
    )
    # Last-Plan actuals exclude Jun: 1 + 2 = 3.0 (Jun's 4.0 excluded).
    assert m[COL_LAST_PLAN_ACTUALS] == 3.0
    # Last-Plan forecast = PRIOR cycle (C3) over Jun+Jul: 8 + 9 = 17.0.
    assert m[COL_LAST_PLAN_FORECAST] == 17.0


def _enriched_sources(
    trk: pd.DataFrame, ibp: pd.DataFrame, py: pd.DataFrame | None = None,
) -> EnrichedSources:
    empty = _enriched_ibp([])
    return EnrichedSources(
        tracker=trk, ibp=ibp, ibp_orders=empty, pdh_warning=None,
        ibp_py=py if py is not None else _enriched_ibp([]),
    )


def test_current_last_total_delta_and_total_actuals_removed():
    """End-to-end on the ESL Large Carton / Branded leaf."""
    filters = _filters_apr_jun_actual()
    esl = dict(item_key="100", item_desc="DG Milk", party_site="1",
               pmaj="ESL", sfmt="Large Carton", pminor="", brand="Branded",
               forecast_type=FORECAST_BASE_PLAN)
    trk = _enriched_trk([
        {**esl, "month": _JUL, "cycle": "C4", "pounds": 10_000_000.0},  # current fcst
        {**esl, "month": _JUN, "cycle": "C3", "pounds": 8_000_000.0},   # prior fcst (shifted)
        {**esl, "month": _JUL, "cycle": "C3", "pounds": 9_000_000.0},   # prior fcst
    ])
    ibp = _enriched_ibp([
        {"item_key": "100", "item_desc": "DG Milk", "customer_no": "1",
         "customer_name": "C", "month": m, "pounds": p,
         "pmaj": "ESL", "sfmt": "Large Carton", "pminor": "", "brand": "Branded"}
        for m, p in ((_APR, 1_000_000.0), (_MAY, 2_000_000.0), (_JUN, 4_000_000.0))
    ])
    result = build_demand_plan_comparison(
        None, None, None, filters,
        enriched=_enriched_sources(trk, ibp), ro_total_delta_by_path={},
    )
    table = result.table
    assert "Total Actuals" not in table.columns          # column removed
    assert "Reconciliation" not in table.columns         # column removed
    row = table.loc[table["_row_id"] == "esl_lc_branded"].iloc[0]
    # Current Plan (incl. RO) = actuals(7) + current forecast(10) = 17.
    assert float(row["Current Plan (incl. RO)"]) == 17.0
    # Last Plan (incl. RO) = last-actuals(3) + prior forecast(8+9=17) = 20.
    assert float(row["Last Plan (incl. RO)"]) == 20.0
    # Total Delta = Current − Last = 17 − 20 = −3.
    assert float(row["Total Delta"]) == -3.0
    # Base Plan Var. is the residual = Total Delta − PM Actual Var. − R&O Var.
    #   PM Actual Var. = 4 (Jun ship) − 8 (C3 Jun fcst) = −4;  R&O Var. = 0.
    #   Base Plan Var. = −3 − (−4) − 0 = 1.
    assert float(row["PM Actual Var."]) == -4.0
    assert float(row["Base Plan Var."]) == 1.0
    # Identity holds by construction: three variances sum to Total Delta.
    assert (
        float(row["Base Plan Var."])
        + float(row["PM Actual Var."])
        + float(row["R&O Var."])
    ) == -3.0
    # Base Plan Var % = Base Plan Var. ÷ (Current Plan (Base) − Base Plan Var.).
    #   Current Plan (Base) = 10 (C4 Base 10M lbs); denom = 10 − 1 = 9.
    assert round(float(row["Base Plan Var %"]), 4) == round(1.0 / 9.0, 4)


def test_last_plan_unshifted_window_matches_current_basis():
    """shift_last_plan_window=False → the prior plan uses the SAME window as the
    current plan (the APS section's Prior Plan = the IBP file's plan for that
    cycle, not the one-month-ago snapshot)."""
    filters = _filters_apr_jun_actual()
    esl = dict(item_key="100", item_desc="DG Milk", party_site="1",
               pmaj="ESL", sfmt="Large Carton", pminor="", brand="Branded",
               forecast_type=FORECAST_BASE_PLAN)
    trk = _enriched_trk([
        {**esl, "month": _JUL, "cycle": "C4", "pounds": 10_000_000.0},  # current fcst
        {**esl, "month": _JUN, "cycle": "C3", "pounds": 8_000_000.0},   # prior (shifted-only)
        {**esl, "month": _JUL, "cycle": "C3", "pounds": 9_000_000.0},   # prior fcst
    ])
    ibp = _enriched_ibp([
        {"item_key": "100", "item_desc": "DG Milk", "customer_no": "1",
         "customer_name": "C", "month": m, "pounds": p,
         "pmaj": "ESL", "sfmt": "Large Carton", "pminor": "", "brand": "Branded"}
        for m, p in ((_APR, 1_000_000.0), (_MAY, 2_000_000.0), (_JUN, 4_000_000.0))
    ])
    result = build_demand_plan_comparison(
        None, None, None, filters,
        enriched=_enriched_sources(trk, ibp), ro_total_delta_by_path={},
        shift_last_plan_window=False,
    )
    row = result.table.loc[result.table["_row_id"] == "esl_lc_branded"].iloc[0]
    # Unshifted: last actuals = the FULL actual window (1+2+4=7); prior forecast
    # = C3 over the SAME forecast window as current (Jul only = 9).  Last = 16
    # (vs 20 when shifted — the shifted case is covered by the test above).
    assert float(row["Last Plan (incl. RO)"]) == 16.0
    # Current Plan is unchanged (actuals 7 + C4 Jul 10 = 17); Total Delta = 1.
    assert float(row["Current Plan (incl. RO)"]) == 17.0
    assert float(row["Total Delta"]) == 1.0


def test_ro_var_from_tracker_is_cycle_delta():
    """APS mode (ro_var_from_tracker + unshifted window): R&O Var and Base Plan
    Var are BOTH direct leg deltas (current − prior over the forecast window),
    ignoring the RO Summary lookup, and Base + R&O ≡ Total Delta (no PM-Actual
    term — actuals cancel in the unshifted window)."""
    filters = _filters_apr_jun_actual()
    esl = dict(item_key="100", item_desc="DG Milk", party_site="1",
               pmaj="ESL", sfmt="Large Carton", pminor="", brand="Branded")
    trk = _enriched_trk([
        {**esl, "forecast_type": FORECAST_BASE_PLAN, "month": _JUL, "cycle": "C4",
         "pounds": 10_000_000.0},
        {**esl, "forecast_type": FORECAST_R_AND_O, "month": _JUL, "cycle": "C4",
         "pounds": 5_000_000.0},                        # current R&O
        {**esl, "forecast_type": FORECAST_BASE_PLAN, "month": _JUL, "cycle": "C3",
         "pounds": 9_000_000.0},
        {**esl, "forecast_type": FORECAST_R_AND_O, "month": _JUL, "cycle": "C3",
         "pounds": 3_000_000.0},                        # prior R&O
    ])
    ibp = _enriched_ibp([
        {"item_key": "100", "item_desc": "DG Milk", "customer_no": "1",
         "customer_name": "C", "month": m, "pounds": p,
         "pmaj": "ESL", "sfmt": "Large Carton", "pminor": "", "brand": "Branded"}
        for m, p in ((_APR, 1_000_000.0), (_MAY, 2_000_000.0), (_JUN, 4_000_000.0))
    ])
    result = build_demand_plan_comparison(
        None, None, None, filters,
        enriched=_enriched_sources(trk, ibp),
        ro_total_delta_by_path={},          # deliberately empty — must be ignored
        shift_last_plan_window=False, ro_var_from_tracker=True,
    )
    row = result.table.loc[result.table["_row_id"] == "esl_lc_branded"].iloc[0]
    # R&O Var  = current C4 R&O (5) − prior C3 R&O (3) = 2 (leg delta).
    # Base Var = current C4 Base (10) − prior C3 Base (9) = 1 (leg delta, NOT the
    #            residual — so equal Base plans would give 0).
    assert float(row["R&O Var."]) == 2.0
    assert float(row["Base Plan Var."]) == 1.0
    # APS identity: Base + R&O ≡ Total Delta (PM Actual is not folded in).
    identity = float(row["Base Plan Var."]) + float(row["R&O Var."])
    assert round(identity, 6) == round(float(row["Total Delta"]), 6)


def test_aps_prior_leg_columns_present_only_in_aps_mode():
    """include_prior_legs (driven by ro_var_from_tracker) adds Total Actual +
    prior-cycle Base / R&O columns to the APS frame; the IBP frame is unchanged."""
    filters = _filters_apr_jun_actual()
    esl = dict(item_key="100", item_desc="DG Milk", party_site="1",
               pmaj="ESL", sfmt="Large Carton", pminor="", brand="Branded")
    trk = _enriched_trk([
        {**esl, "forecast_type": FORECAST_BASE_PLAN, "month": _JUL, "cycle": "C4",
         "pounds": 10_000_000.0},
        {**esl, "forecast_type": FORECAST_R_AND_O, "month": _JUL, "cycle": "C4",
         "pounds": 5_000_000.0},
        {**esl, "forecast_type": FORECAST_BASE_PLAN, "month": _JUL, "cycle": "C3",
         "pounds": 9_000_000.0},
        {**esl, "forecast_type": FORECAST_R_AND_O, "month": _JUL, "cycle": "C3",
         "pounds": 3_000_000.0},
    ])
    ibp = _enriched_ibp([
        {"item_key": "100", "item_desc": "DG Milk", "customer_no": "1",
         "customer_name": "C", "month": m, "pounds": p,
         "pmaj": "ESL", "sfmt": "Large Carton", "pminor": "", "brand": "Branded"}
        for m, p in ((_APR, 1_000_000.0), (_MAY, 2_000_000.0), (_JUN, 4_000_000.0))
    ])
    aps = build_demand_plan_comparison(
        None, None, None, filters, enriched=_enriched_sources(trk, ibp),
        ro_total_delta_by_path={}, shift_last_plan_window=False,
        ro_var_from_tracker=True)
    row = aps.table.loc[aps.table["_row_id"] == "esl_lc_branded"].iloc[0]
    assert float(row["Prior Plan (Base)"]) == 9.0    # C3 base over forecast window
    assert float(row["Prior Plan (R&O)"]) == 3.0     # C3 R&O over forecast window
    assert float(row["Total Actual"]) == 7.0         # Apr+May+Jun shipments
    # IBP build (defaults) does NOT carry the APS-only columns.
    ibp_res = build_demand_plan_comparison(
        None, None, None, filters, enriched=_enriched_sources(trk, ibp),
        ro_total_delta_by_path={})
    for col in ("Prior Plan (Base)", "Prior Plan (R&O)", "Total Actual"):
        assert col not in ibp_res.table.columns


def test_business_health_windows_yoy_and_flag():
    """L3M/L6M/L12M Order sums + YAG, Order YoY, momentum Flag, and the Total-B2C
    chart series for BOTH Orders and Shipments."""
    from data_sources.demand_plan_comparison import BH_COL_FLAG, BH_FLAG_RISING
    esl = dict(item_key="100", item_desc="DG Milk", customer_no="1",
               customer_name="C", pmaj="ESL", sfmt="Large Carton",
               pminor="", brand="Branded")
    orders = _enriched_ibp([
        {**esl, "month": dt.date(2026, 6, 1), "pounds": 12e6},   # L3M/L6M/L12M cur
        {**esl, "month": dt.date(2025, 6, 1), "pounds": 6e6},    # matching YAG
        {**esl, "month": dt.date(2025, 7, 1), "pounds": 10e6},   # L12M cur only
        {**esl, "month": dt.date(2024, 7, 1), "pounds": 10e6},   # L12M YAG only
    ])
    shipments = _enriched_ibp([
        {**esl, "month": dt.date(2026, 6, 1), "pounds": 20e6},   # L3M shipments
        {**esl, "month": dt.date(2025, 6, 1), "pounds": 10e6},
    ])
    res = build_business_health(orders, shipments, dt.date(2026, 6, 1))
    row = res.table.loc[res.table["_row_id"] == "esl_lc_branded"].iloc[0]
    assert float(row["L3M Orders"]) == 12.0 and float(row["L3M Orders YAG"]) == 6.0
    assert float(row["L6M Orders"]) == 12.0 and float(row["L6M Orders YAG"]) == 6.0
    assert float(row["L12M Orders"]) == 22.0 and float(row["L12M Orders YAG"]) == 16.0
    assert round(float(row["L3M Order YoY"]), 4) == 1.0        # (12−6)/6
    assert round(float(row["L12M Order YoY"]), 4) == 0.375     # (22−16)/16
    # L3M YoY (1.0) >> L12M YoY (0.375) → accelerating → Rising.
    assert row[BH_COL_FLAG] == BH_FLAG_RISING
    # Total B2C rolls the single leaf up unchanged.
    tot = res.table.loc[res.table["_row_id"] == "total_b2c"].iloc[0]
    assert float(tot["L12M Orders"]) == 22.0
    # Chart series: Orders + Shipments Total-B2C volume + YoY per window.
    assert res.chart_series["Orders"]["L3M"]["vol"] == 12.0
    assert round(res.chart_series["Orders"]["L3M"]["yoy"], 4) == 1.0
    assert res.chart_series["Shipments"]["L3M"]["vol"] == 20.0
    assert round(res.chart_series["Shipments"]["L3M"]["yoy"], 4) == 1.0   # (20−10)/10
    # Window labels drive the legend + the explicit YoY definition.
    assert res.window_labels["L3M"] == ("Apr 2026 – Jun 2026", "Apr 2025 – Jun 2025")
    assert res.window_labels["L12M"] == ("Jul 2025 – Jun 2026", "Jul 2024 – Jun 2025")


def test_sku_cycle_comparison_leg_buildup_and_filter():
    """Per-SKU leg build-up (unshifted / APS): base + R&O legs, actual, plans,
    deltas; dim filter narrows the SKUs."""
    filters = _filters_apr_jun_actual()   # C4 current, C3 prior, Fcst Jul–Mar
    esl = dict(item_key="100", item_desc="DG Milk", pmaj="ESL",
               sfmt="Large Carton", pminor="", brand="Branded")
    cult = dict(item_key="200", item_desc="Sour Cream Tub", pmaj="Cultured",
                sfmt="Large Tub", pminor="Sour Cream", brand="Private")
    trk = _enriched_trk([
        {**esl, "forecast_type": FORECAST_BASE_PLAN, "month": _JUL, "cycle": "C4", "pounds": 10e6},
        {**esl, "forecast_type": FORECAST_R_AND_O, "month": _JUL, "cycle": "C4", "pounds": 4e6},
        {**esl, "forecast_type": FORECAST_BASE_PLAN, "month": _JUL, "cycle": "C3", "pounds": 8e6},
        {**cult, "forecast_type": FORECAST_BASE_PLAN, "month": _JUL, "cycle": "C4", "pounds": 3e6},
    ])
    ibp = _enriched_ibp([
        {"item_key": "100", "item_desc": "DG Milk", "customer_no": "1",
         "customer_name": "C", "month": _APR, "pounds": 2e6,
         "pmaj": "ESL", "sfmt": "Large Carton", "pminor": "", "brand": "Branded"},
    ])
    out = build_sku_cycle_comparison(
        trk, ibp, filters, shift_last_plan_window=False)
    # Two SKUs present (ESL + Cultured), sorted by current plan desc → ESL first.
    assert list(out["SKU"]) == ["DG Milk (100)", "Sour Cream Tub (200)"]
    esl_row = out.iloc[0]
    assert float(esl_row["C4 Base"]) == 10.0 and float(esl_row["C3 Base"]) == 8.0
    assert float(esl_row["Base Δ"]) == 2.0
    assert float(esl_row["C4 R&O"]) == 4.0 and float(esl_row["R&O Δ"]) == 4.0
    assert float(esl_row["Total Actual"]) == 2.0
    # Unshifted: both plans share the actual leg (2).  Current = 2+10+4 = 16;
    # prior = 2+8+0 = 10; Total Δ = 6 = Base Δ(2) + R&O Δ(4) (actuals cancel).
    assert float(esl_row["C4 Plan (incl R&O)"]) == 16.0
    assert float(esl_row["C3 Plan (incl R&O)"]) == 10.0
    assert float(esl_row["Total Δ"]) == 6.0
    # Dim filter narrows to ESL only.
    only_esl = build_sku_cycle_comparison(
        trk, ibp, filters, dim_filter={"pmaj": {"ESL"}}, shift_last_plan_window=False)
    assert list(only_esl["SKU"]) == ["DG Milk (100)"]


def test_current_plan_split_o_pct_and_py_actual():
    """Current Plan (Base)/(R&O) split, O% = R&O/Current Plan, and PY Actual."""
    filters = _filters_apr_jun_actual()
    esl = dict(item_key="100", item_desc="DG Milk", party_site="1",
               pmaj="ESL", sfmt="Large Carton", pminor="", brand="Branded")
    trk = _enriched_trk([
        {**esl, "forecast_type": FORECAST_BASE_PLAN, "month": _JUL, "cycle": "C4",
         "pounds": 10_000_000.0},                       # current Base
        {**esl, "forecast_type": FORECAST_R_AND_O, "month": _JUL, "cycle": "C4",
         "pounds": 2_000_000.0},                        # current R&O
    ])
    ibp = _enriched_ibp([
        {"item_key": "100", "item_desc": "DG Milk", "customer_no": "1",
         "customer_name": "C", "month": m, "pounds": p,
         "pmaj": "ESL", "sfmt": "Large Carton", "pminor": "", "brand": "Branded"}
        for m, p in ((_APR, 1_000_000.0), (_MAY, 2_000_000.0), (_JUN, 4_000_000.0))
    ])
    # Prior-year shipments frame (already scoped to the PY window by the fetch).
    py = _enriched_ibp([
        {"item_key": "100", "item_desc": "DG Milk", "customer_no": "1",
         "customer_name": "C", "month": dt.date(2025, 7, 1), "pounds": 6_000_000.0,
         "pmaj": "ESL", "sfmt": "Large Carton", "pminor": "", "brand": "Branded"},
    ])
    result = build_demand_plan_comparison(
        None, None, None, filters,
        enriched=_enriched_sources(trk, ibp, py), ro_total_delta_by_path={},
    )
    row = result.table.loc[result.table["_row_id"] == "esl_lc_branded"].iloc[0]
    assert float(row["Current Plan (Base)"]) == 10.0
    assert float(row["Current Plan (R&O)"]) == 2.0
    # Current Plan (incl. RO) = actuals(7) + Base(10) + R&O(2) = 19.
    assert float(row["Current Plan (incl. RO)"]) == 19.0
    # O% of Current Plan = R&O(2) / 19.
    assert round(float(row["O% of Current Plan"]), 4) == round(2.0 / 19.0, 4)
    assert float(row["PY Actual"]) == 6.0
    assert "Current Plan (Forecast)" not in result.table.columns


def test_build_comparison_kpis():
    """T3M/T6M YoY from trailing shipments; Full-Year YoY + RO% from the table."""
    filters = _filters_apr_jun_actual()  # actual Apr–Jun 2026 → T-anchor = Jun 2026
    esl = dict(item_key="100", item_desc="DG Milk", party_site="1",
               pmaj="ESL", sfmt="Large Carton", pminor="", brand="Branded")
    trk = _enriched_trk([
        {**esl, "forecast_type": FORECAST_BASE_PLAN, "month": _JUL, "cycle": "C4",
         "pounds": 10_000_000.0},
        {**esl, "forecast_type": FORECAST_R_AND_O, "month": _JUL, "cycle": "C4",
         "pounds": 2_000_000.0},
    ])
    ship = lambda m, p: {  # noqa: E731
        "item_key": "100", "item_desc": "DG Milk", "customer_no": "1",
        "customer_name": "C", "month": m, "pounds": p,
        "pmaj": "ESL", "sfmt": "Large Carton", "pminor": "", "brand": "Branded"}
    ibp = _enriched_ibp([ship(_APR, 1e6), ship(_MAY, 2e6), ship(_JUN, 4e6)])  # actuals=7
    py = _enriched_ibp([ship(dt.date(2025, 7, 1), 6e6)])                      # PY Actual=6
    result = build_demand_plan_comparison(
        None, None, None, filters,
        enriched=_enriched_sources(trk, ibp, py), ro_total_delta_by_path={},
    )

    # Trailing-6-month shipments ending Jun 2026 (T3M = Apr–Jun).
    def _m(y, mo):
        return dt.date(y, mo, 1)
    recent = _enriched_ibp([ship(_m(2026, mo), 4e6) for mo in (4, 5, 6)]      # T3M cur=12
                           + [ship(_m(2026, mo), 2e6) for mo in (1, 2, 3)])   # +6 → T6M cur=18
    recent_py = _enriched_ibp([ship(_m(2025, mo), 3e6) for mo in (4, 5, 6)]   # T3M py=9
                              + [ship(_m(2025, mo), 1e6) for mo in (1, 2, 3)])  # +3 → T6M py=12

    kpis = build_comparison_kpis(result.table, recent, recent_py, filters)
    assert round(kpis.t3m_yoy, 4) == round((12 - 9) / 9, 4)     # +33.3%
    assert round(kpis.t6m_yoy, 4) == round((18 - 12) / 12, 4)   # +50%
    # Current Plan = actuals(7)+Base(10)+R&O(2)=19; Base plan = 19−R&O(2)=17;
    # Full-Year Base vs PY% = (Base − PY) / PY = (17 − 6) / 6.
    assert round(kpis.full_year_base_vs_py, 4) == round((17 - 6) / 6, 4)
    assert round(kpis.ro_pct, 4) == round(2 / 19, 4)
    # Walk-tile values must tie to the assembled Total B2C cells so the
    # KPI strip and the table always reconcile.
    cp = "Current Plan (incl. RO)"
    lp = "Last Plan (incl. RO)"
    tot = result.table.loc[result.table["_row_id"] == "total_b2c"].iloc[0]
    assert kpis.current_plan_total == float(tot[cp])
    assert kpis.last_plan_total == float(tot[lp])
    assert kpis.pm_actual_var == float(tot["PM Actual Var."])
    assert kpis.base_plan_var == float(tot["Base Plan Var."])
    # R&O Var. tile must equal the R&O Var. table cell (which is the
    # RO Summary Report's FY27 Probabilized | Total Δ) — guards the
    # RO_Summary_Report ↔ table ↔ tile chain from silent drift.
    assert kpis.ro_var == float(tot["R&O Var."])


def test_kpis_none_when_denominator_zero():
    filters = _filters_apr_jun_actual()
    empty = _enriched_ibp([])
    result = build_demand_plan_comparison(
        None, None, None, filters,
        enriched=_enriched_sources(_enriched_trk([]), empty), ro_total_delta_by_path={},
    )
    kpis = build_comparison_kpis(result.table, empty, empty, filters)
    assert kpis.t3m_yoy is None and kpis.t6m_yoy is None
    assert kpis.full_year_base_vs_py is None   # PY Actual = 0 → undefined


def test_pmaj_filter_narrows_rollup():
    """Selecting only ESL zeroes the Cultured branch (and Total B2C reflects it)."""
    filters = ComparisonFilters(
        current_cycle="C4", prior_cycle="C3",
        actual_start=_APR, actual_end=_JUN,
        forecast_start=_JUL, forecast_end=dt.date(2027, 3, 1),
        prior_month=_JUN, pmaj_filter=frozenset({"ESL"}),
    )
    trk = _enriched_trk([
        {"item_key": "1", "item_desc": "", "party_site": "1", "pmaj": "ESL",
         "sfmt": "Large Carton", "pminor": "", "brand": "Branded",
         "forecast_type": FORECAST_BASE_PLAN, "month": _JUL, "cycle": "C4",
         "pounds": 10_000_000.0},
        {"item_key": "2", "item_desc": "", "party_site": "1", "pmaj": "Cultured",
         "sfmt": "Large Tub", "pminor": "", "brand": "",
         "forecast_type": FORECAST_BASE_PLAN, "month": _JUL, "cycle": "C4",
         "pounds": 5_000_000.0},
    ])
    result = build_demand_plan_comparison(
        None, None, None, filters,
        enriched=_enriched_sources(trk, _enriched_ibp([])), ro_total_delta_by_path={},
    )
    t = result.table
    cp = "Current Plan (incl. RO)"
    esl = float(t.loc[t["_row_id"] == "esl_lc_branded", cp].iloc[0])
    cult = float(t.loc[t["_row_id"] == "cult_large_tub", cp].iloc[0])
    b2c = float(t.loc[t["_row_id"] == "total_b2c", cp].iloc[0])
    assert esl == 10.0
    assert cult == 0.0            # Cultured filtered out
    assert b2c == 10.0            # Total B2C reflects only the ESL slice


def test_dim_frame_from_tracker_reads_columns_and_derives_brand():
    trk = pd.DataFrame({
        TRK_ITEM: ["100", "200"],
        TRK_ITEM_DESCRIPTION: ["DG Whole Milk", "Store Brand Milk"],
        TRK_PMAJ: ["ESL", "Cultured"],
        TRK_SFMT: ["Large Carton", "Large Tub"],
        "Portfolio Minor": ["", "Cottage Cheese"],
    })
    dim = build_item_dim_frame_from_tracker(trk)
    by_item = {r["__item_key"]: r for _, r in dim.iterrows()}
    assert by_item["100"]["pmaj"] == "ESL" and by_item["100"]["sfmt"] == "Large Carton"
    assert by_item["100"]["brand"] == "Branded"      # "DG ..." → Branded
    assert by_item["200"]["brand"] == "Private"       # no "DG" prefix
    assert by_item["200"]["pminor"] == "Cottage Cheese"
    # Options helper surfaces the distinct dims for the widgets (raw tracker).
    pmajs, sfmts = list_tracker_dim_values(trk)
    assert pmajs == ["Cultured", "ESL"]
    assert sfmts == ["Large Carton", "Large Tub"]


def test_dim_frame_from_tracker_empty_signals_fallback():
    """A legacy tracker with no dim columns returns empty → caller falls back."""
    trk = pd.DataFrame({TRK_ITEM: ["100"], TRK_ITEM_DESCRIPTION: ["x"]})
    assert build_item_dim_frame_from_tracker(trk).empty


# ── Packaged-Butter budget from the static base file (Branded/Private × SFmt) ─

def test_packaged_butter_budget_from_base_file():
    """build_packaged_butter_budget sums Static_Budget_Base_Lbs.csv rows scoped
    to Portfolio Minor = 'Packaged Butter', keyed by normalised Brand × SFmt.
    Non-packaged Butter rows are excluded; the loose SFmt key drops trailing
    's' so the plan's plural formats match."""
    from data_sources.demand_summary import build_packaged_butter_budget
    base = pd.DataFrame([
        {"Portfolio Major": "Butter", "Supply Format": "Elgin Solid",
         "Brand Name": "Darigold", "Portfolio Minor": "Packaged Butter",
         "Demand Plan Pounds": 11_000_000.0},
        {"Portfolio Major": "Butter", "Supply Format": "Western Quarters",
         "Brand Name": "Private", "Portfolio Minor": "Packaged Butter",
         "Demand Plan Pounds": 60_000_000.0},
        {"Portfolio Major": "Butter", "Supply Format": "Elgin Quarters",
         "Brand Name": "Private", "Portfolio Minor": "Packaged Butter",
         "Demand Plan Pounds": 22_000_000.0},
        # Bulk / ingredient butter shares the Butter PMaj but is NOT Packaged
        # Butter — the Portfolio Minor scope must drop it.
        {"Portfolio Major": "Butter", "Supply Format": "Bulk",
         "Brand Name": "Darigold", "Portfolio Minor": "Bulk Butter",
         "Demand Plan Pounds": 999_000_000.0},
    ])
    bb = build_packaged_butter_budget(base)
    assert bb.has_data
    assert round(bb.total_m, 3) == 93.0                       # 11 + 60 + 22 (bulk excluded)
    assert round(bb.by_brand_sfmt[("Branded", "elgin solid")], 3) == 11.0
    assert round(bb.by_brand_sfmt[("Private", "western quarter")], 3) == 60.0
    # "Darigold" → Branded; "Elgin Quarters" loose-keys to "elgin quarter".
    assert round(bb.by_brand_sfmt[("Private", "elgin quarter")], 3) == 22.0


def test_packaged_butter_budget_empty_when_no_packaged_rows():
    """No Packaged-Butter rows → has_data False so the comparison keeps the
    workbook fallback."""
    from data_sources.demand_summary import build_packaged_butter_budget
    base = pd.DataFrame([
        {"Portfolio Major": "ESL", "Supply Format": "Large Carton",
         "Brand Name": "Darigold", "Portfolio Minor": "Whipping Cream",
         "Demand Plan Pounds": 5_000_000.0},
    ])
    assert not build_packaged_butter_budget(base).has_data


def test_butter_budget_overrides_workbook_in_comparison():
    """A CSV-sourced PackagedButterBudget overrides the FY27 workbook's single
    'butter' figure: the parent 'Packaged Butter' row shows the CSV total and
    each Branded/Private → SFmt detail row its own budget (both IBP and APS)."""
    from data_sources.demand_summary import PackagedButterBudget
    filters = _filters_apr_jun_actual()
    branded = dict(item_key="b1", item_desc="DG Elgin", party_site="1",
                   pmaj="Butter", sfmt="Elgin Solid", pminor="Packaged Butter",
                   brand="Branded", forecast_type=FORECAST_BASE_PLAN)
    private = dict(item_key="b2", item_desc="PL WQ", party_site="1",
                   pmaj="Butter", sfmt="Western Quarters", pminor="Packaged Butter",
                   brand="Private", forecast_type=FORECAST_BASE_PLAN)
    trk = _enriched_trk([
        {**branded, "month": _JUL, "cycle": "C4", "pounds": 5e6},
        {**private, "month": _JUL, "cycle": "C4", "pounds": 3e6},
    ])
    ibp = _enriched_ibp([])
    bb = PackagedButterBudget(
        by_brand_sfmt={("Branded", "elgin solid"): 11.0,
                       ("Private", "western quarter"): 60.0},
        combos=(("Branded", "Elgin Solid", 11.0),
                ("Private", "Western Quarters", 60.0)),
        total_m=71.0, has_data=True)

    def _bud(table, rid):
        r = table.loc[table["_row_id"] == rid]
        return round(float(r.iloc[0]["Budget"]), 3)

    for kw in ({}, {"shift_last_plan_window": False, "ro_var_from_tracker": True}):
        res = build_demand_plan_comparison(
            None, None, None, filters, enriched=_enriched_sources(trk, ibp),
            ro_total_delta_by_path={}, budget_by_row_id={"butter": 7.5},
            butter_budget=bb, **kw)
        t = res.table
        assert _bud(t, "butter") == 71.0            # overrode the workbook's 7.5
        assert _bud(t, "butter_branded_sfmt_elgin_solid") == 11.0
        assert _bud(t, "butter_private_sfmt_western_quarters") == 60.0
        assert _bud(t, "butter_private") == 60.0    # subtotal rolls up its child

    # Regression guard: without butter_budget the workbook value stands.
    res2 = build_demand_plan_comparison(
        None, None, None, filters, enriched=_enriched_sources(trk, ibp),
        ro_total_delta_by_path={}, budget_by_row_id={"butter": 7.5})
    assert _bud(res2.table, "butter") == 7.5


def test_butter_budget_parent_respects_brand_filter():
    """The parent 'Packaged Butter' budget re-filters the CSV combos to the
    active selection, so a Branded-only filter drops Private from the total."""
    from data_sources.demand_summary import PackagedButterBudget
    filters = ComparisonFilters(
        current_cycle="C4", prior_cycle="C3",
        actual_start=_APR, actual_end=_JUN,
        forecast_start=_JUL, forecast_end=dt.date(2027, 3, 1),
        prior_month=_JUN, brand_filter=("Branded",))
    branded = dict(item_key="b1", item_desc="DG Elgin", party_site="1",
                   pmaj="Butter", sfmt="Elgin Solid", pminor="Packaged Butter",
                   brand="Branded", forecast_type=FORECAST_BASE_PLAN)
    trk = _enriched_trk([{**branded, "month": _JUL, "cycle": "C4", "pounds": 5e6}])
    bb = PackagedButterBudget(
        by_brand_sfmt={("Branded", "elgin solid"): 11.0,
                       ("Private", "western quarter"): 60.0},
        combos=(("Branded", "Elgin Solid", 11.0),
                ("Private", "Western Quarters", 60.0)),
        total_m=71.0, has_data=True)
    res = build_demand_plan_comparison(
        None, None, None, filters, enriched=_enriched_sources(trk, _enriched_ibp([])),
        ro_total_delta_by_path={}, budget_by_row_id={"butter": 7.5}, butter_budget=bb)
    t = res.table
    row = t.loc[t["_row_id"] == "butter"].iloc[0]
    assert round(float(row["Budget"]), 3) == 11.0   # Private (60) filtered out


def test_not_captured_flags_items_outside_the_template():
    """A 'Whey/Bag' SKU (no template family) is logged for both cycles."""
    filters = ComparisonFilters(
        current_cycle="C4", prior_cycle="C3",
        actual_start=_APR, actual_end=_JUN,
        forecast_start=_JUL, forecast_end=dt.date(2026, 8, 1),
        prior_month=_JUN,
    )
    captured = dict(item_key="100", item_desc="DG Milk", party_site="1",
                    pmaj="ESL", sfmt="Large Carton", pminor="", brand="Branded",
                    forecast_type=FORECAST_BASE_PLAN)
    uncaptured = dict(item_key="900", item_desc="Whey Powder", party_site="9",
                      pmaj="Whey", sfmt="Bag", pminor="", brand="",
                      forecast_type=FORECAST_BASE_PLAN)
    trk = _enriched_trk([
        {**captured, "month": _JUL, "cycle": "C4", "pounds": 5_000_000.0},
        {**captured, "month": _JUN, "cycle": "C3", "pounds": 3_000_000.0},
        {**uncaptured, "month": _JUL, "cycle": "C4", "pounds": 2_000_000.0},  # current window
        {**uncaptured, "month": _JUN, "cycle": "C3", "pounds": 1_000_000.0},  # prior window (Jul−1)
    ])
    nc: ComparisonNotCaptured = build_comparison_not_captured(trk, filters)

    cur_items = set(nc.current_cycle[NC_COL_ITEM].astype(str))
    prior_items = set(nc.prior_cycle[NC_COL_ITEM].astype(str))
    assert "900" in cur_items and "900" in prior_items   # uncaptured, both cycles
    assert "100" not in cur_items and "100" not in prior_items  # captured
    # Categorised via the (cascade-resolved) dims.
    whey_row = nc.current_cycle.loc[
        nc.current_cycle[NC_COL_ITEM].astype(str) == "900"].iloc[0]
    assert whey_row[NC_COL_PMAJ] == "Whey"
    assert nc.current_cycle_label == "C4" and nc.prior_cycle_label == "C3"
    assert nc.actuals.empty              # no ibp passed → actuals leg empty


def test_not_captured_actual_shipments_leg():
    """Uncaptured SHIPPED SKUs over the actual window populate the actuals leg."""
    filters = ComparisonFilters(
        current_cycle="C4", prior_cycle="C3",
        actual_start=_APR, actual_end=_JUN,
        forecast_start=_JUL, forecast_end=dt.date(2026, 8, 1),
        prior_month=_JUN,
    )
    ibp = _enriched_ibp([
        # Captured (ESL LC Branded) — should NOT appear.
        {"item_key": "100", "item_desc": "DG Milk", "customer_no": "1",
         "customer_name": "C", "month": _MAY, "pounds": 5e6,
         "pmaj": "ESL", "sfmt": "Large Carton", "pminor": "", "brand": "Branded"},
        # Uncaptured (Whey/Bag) in the actual window — SHOULD appear.
        {"item_key": "900", "item_desc": "Whey Powder", "customer_no": "9",
         "customer_name": "W", "month": _JUN, "pounds": 2e6,
         "pmaj": "Whey", "sfmt": "Bag", "pminor": "", "brand": ""},
        # Uncaptured but OUTSIDE the actual window — excluded.
        {"item_key": "901", "item_desc": "Whey Two", "customer_no": "9",
         "customer_name": "W", "month": _JUL, "pounds": 9e6,
         "pmaj": "Whey", "sfmt": "Bag", "pminor": "", "brand": ""},
    ])
    nc = build_comparison_not_captured(_enriched_trk([]), filters, ibp_enriched=ibp)
    items = set(nc.actuals[NC_COL_ITEM].astype(str))
    assert items == {"900"}                        # captured + out-of-window excluded
    assert CNC_COL_SHIPPED_M in nc.actuals.columns  # shipped measure, not forecast
    assert nc.actual_window_label == "Apr 2026 – Jun 2026"


def test_tracker_has_dim_columns():
    assert tracker_has_dim_columns(pd.DataFrame({
        TRK_ITEM: ["1"], TRK_PMAJ: ["ESL"], TRK_SFMT: ["Large Carton"],
        TRK_PMINOR: [""],
    }))
    assert not tracker_has_dim_columns(pd.DataFrame({TRK_ITEM: ["1"]}))  # legacy
    assert not tracker_has_dim_columns(None)


# ── R&O Var reconciles to the RO Summary Report (IBP) ─────────────────────────

def test_ibp_ro_var_subtotals_mirror_ro_summary_report():
    """IBP R&O Var reads the RO Summary Report's OWN subtotal rows — not a sum of
    its per-leaf values — so Total B2C / family subtotals reconcile to the RO
    Summary section headline-for-headline even when the report's subtotal
    disagrees with the sum of its (0.1M-rounded) leaves.

    Regression for the -1.3 (leaf-sum) vs -1.2 (report subtotal) Total B2C gap.
    """
    filters = _filters_apr_jun_actual()
    esl = dict(item_key="100", item_desc="DG Milk", party_site="1",
               pmaj="ESL", sfmt="Large Carton", pminor="", brand="Branded",
               forecast_type=FORECAST_BASE_PLAN)
    trk = _enriched_trk([{**esl, "month": _JUL, "cycle": "C4", "pounds": 1e6}])
    # Report where the SUBTOTAL rows deliberately differ from the sum of their
    # rounded leaves (LC leaves -0.3 + -0.1 = -0.4, but the LC subtotal reads
    # -0.3; ESL -0.3; Total B2C -0.2) — exactly the rounding drift in the file.
    ro = {
        ("Total B2C",): -0.2,
        ("Total B2C", "Extended Shelf Life"): -0.3,
        ("Total B2C", "Extended Shelf Life", "Large Carton"): -0.3,
        ("Total B2C", "Extended Shelf Life", "Large Carton", "Branded"): -0.3,
        ("Total B2C", "Extended Shelf Life", "Large Carton", "Private Label"): -0.1,
    }
    res = build_demand_plan_comparison(
        None, None, None, filters, enriched=_enriched_sources(trk, _enriched_ibp([])),
        ro_total_delta_by_path=ro)
    t = res.table

    def rov(rid):
        return round(float(t.loc[t["_row_id"] == rid].iloc[0]["R&O Var."]), 3)

    # Subtotals read the report's OWN rows (NOT the -0.4 leaf sum).
    assert rov("total_b2c") == -0.2
    assert rov("esl") == -0.3
    assert rov("esl_lc") == -0.3
    # Leaves still read their own path values.
    assert rov("esl_lc_branded") == -0.3
    assert rov("esl_lc_private") == -0.1


def test_aps_ro_var_ignores_ro_summary_subtotal_override():
    """APS (ro_var_from_tracker) keeps its tracker cycle-delta roll-up — the RO
    Summary subtotal override is IBP-only."""
    filters = _filters_apr_jun_actual()
    esl = dict(item_key="100", item_desc="DG Milk", party_site="1",
               pmaj="ESL", sfmt="Large Carton", pminor="", brand="Branded")
    trk = _enriched_trk([
        {**esl, "forecast_type": FORECAST_R_AND_O, "month": _JUL, "cycle": "C4", "pounds": 5e6},
        {**esl, "forecast_type": FORECAST_R_AND_O, "month": _JUL, "cycle": "C3", "pounds": 3e6},
    ])
    ro = {  # absurd values that MUST be ignored in APS mode
        ("Total B2C",): -99.0,
        ("Total B2C", "Extended Shelf Life"): -99.0,
        ("Total B2C", "Extended Shelf Life", "Large Carton"): -99.0,
    }
    res = build_demand_plan_comparison(
        None, None, None, filters, enriched=_enriched_sources(trk, _enriched_ibp([])),
        ro_total_delta_by_path=ro, shift_last_plan_window=False, ro_var_from_tracker=True)
    t = res.table
    # Tracker leg delta = C4 R&O (5) − C3 R&O (3) = 2 — NOT the -99 report value.
    assert round(float(t.loc[t["_row_id"] == "esl_lc_branded"].iloc[0]["R&O Var."]), 3) == 2.0
    assert round(float(t.loc[t["_row_id"] == "total_b2c"].iloc[0]["R&O Var."]), 3) == 2.0


def test_ibp_butter_ro_var_rolls_up_from_detail_rows():
    """The dynamic Branded/Private -> format detail rows read the RO Summary
    Report's OWN Butter (format, brand) detail, so R&O Var -- and the Base Plan
    Var residual -- foot at the parent 'Packaged Butter' row (regression for the
    parent 0.70 vs children 0.00 gap in the Cycle-over-Cycle table)."""
    filters = _filters_apr_jun_actual()

    def bt(brand, sfmt):
        return dict(item_key="x", item_desc="x", party_site="1", month=_JUL,
                    pounds=2e6, forecast_type=FORECAST_BASE_PLAN, cycle="C4",
                    pmaj="Butter", sfmt=sfmt, pminor="Packaged Butter", brand=brand)

    trk = _enriched_trk([
        bt("Branded", "Western Quarters"),
        bt("Private", "Western Quarters"),
        bt("Branded", "Elgin Solid"),
    ])
    ro = {
        ("Total B2C", "Butter"): 0.7,
        ("Total B2C", "Butter", "Western Quarters"): 0.7,
        ("Total B2C", "Butter", "Western Quarters", "Branded"): -0.2,
        ("Total B2C", "Butter", "Western Quarters", "Private Label"): 0.9,
    }
    res = build_demand_plan_comparison(
        None, None, None, filters, enriched=_enriched_sources(trk, _enriched_ibp([])),
        ro_total_delta_by_path=ro)
    t = res.table

    def rov(rid):
        return round(float(t.loc[t["_row_id"] == rid].iloc[0]["R&O Var."]), 3)

    def base(rid):
        return round(float(t.loc[t["_row_id"] == rid].iloc[0]["Base Plan Var."]), 3)

    # Detail brand subtotals read the report's Butter (format, brand) detail;
    # Elgin Solid contributes 0 (absent from the report -> loose miss).
    assert rov("butter_branded") == -0.2
    assert rov("butter_private") == 0.9
    # Parent 'Packaged Butter' = Branded + Private (foots -- was 0.70 vs 0.00).
    assert rov("butter") == 0.7
    # Base Plan Var (residual) foots too, because R&O now foots.
    assert base("butter") == round(base("butter_branded") + base("butter_private"), 3)


def test_aps_butter_ro_var_uses_tracker_not_ro_summary():
    """APS butter R&O comes from the tracker cycle delta (not the RO Summary
    detail), and still foots parent = Branded + Private."""
    filters = _filters_apr_jun_actual()

    def bt(brand, ft, cyc, lbs):
        return dict(item_key="x", item_desc="x", party_site="1", month=_JUL,
                    pounds=lbs, forecast_type=ft, cycle=cyc, pmaj="Butter",
                    sfmt="Western Quarters", pminor="Packaged Butter", brand=brand)

    trk = _enriched_trk([
        bt("Branded", FORECAST_R_AND_O, "C4", 5e6),
        bt("Branded", FORECAST_R_AND_O, "C3", 3e6),
        bt("Private", FORECAST_R_AND_O, "C4", 4e6),
    ])
    ro = {("Total B2C", "Butter", "Western Quarters", "Branded"): -99.0}  # ignored in APS
    res = build_demand_plan_comparison(
        None, None, None, filters, enriched=_enriched_sources(trk, _enriched_ibp([])),
        ro_total_delta_by_path=ro, shift_last_plan_window=False, ro_var_from_tracker=True)
    t = res.table

    def rov(rid):
        return round(float(t.loc[t["_row_id"] == rid].iloc[0]["R&O Var."]), 3)

    # Branded = C4 R&O (5) - C3 R&O (3) = 2; Private = 4 - 0 = 4 -- tracker, not -99.
    assert rov("butter_branded") == 2.0
    assert rov("butter_private") == 4.0
    assert rov("butter") == 6.0     # foots parent = Branded + Private
