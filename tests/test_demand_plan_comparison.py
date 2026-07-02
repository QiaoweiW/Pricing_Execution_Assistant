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
    COL_PRIOR_MONTH_ACTUAL,
    COL_PRIOR_MONTH_FORECAST,
    DIAG_COL_LBS,
    DIAG_COL_PMAJ,
    DIAG_COL_SFMT,
    DIAG_UNMAPPED,
    FORECAST_BASE_PLAN,
    ComparisonFilters,
    TemplateRow,
    _compute_leaf_measures,
    build_prior_month_shipment_diagnostic,
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
