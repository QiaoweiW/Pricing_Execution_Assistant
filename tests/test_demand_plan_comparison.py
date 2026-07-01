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

from data_sources.demand_plan_comparison import _vectorised_start_of_month as som


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
