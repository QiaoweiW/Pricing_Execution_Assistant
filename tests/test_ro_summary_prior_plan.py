"""Unit test for the RO Summary Report's derived FY27 'Prior Plan' column.

Prior Plan = Current Plan − Total Delta (no separate source column), so it
must roll up additively through the subtotals exactly like the other metrics.
Fixture is a synthetic ``RO_Comparison_Output.csv``-shaped frame — no Fabric.
"""
from __future__ import annotations

import pandas as pd

from data_sources.ro_comparison import (
    CUR_FISCAL_PROB_CHANGE,
    CUR_FISCAL_PROB_LE,
    YEAR1_PROB_LE,
    YEAR1_PROB_PRIOR,
)
from data_sources.ro_summary_report import (
    COL_CURRENT_PLAN,
    COL_PRIOR_PLAN,
    COL_ROW_ID,
    COL_TOTAL_DELTA,
    DATA_COLS,
    SAVED_COLUMN_LABELS,
    build_summary_report,
    prepare_summary_for_export,
)


def _comp_df() -> pd.DataFrame:
    """Two Fresh Milk / Gallon Jug rows (New + Change drivers)."""
    return pd.DataFrame({
        "Portfolio Major": ["Fresh Milk", "Fresh Milk"],
        "Supply Format":   ["Gallon Jug", "Gallon Jug"],
        "Portfolio Minor": ["", ""],
        "Brand":           ["Branded", "Branded"],
        "Driver":          ["New", "Change"],
        CUR_FISCAL_PROB_LE:     [5_000_000, 3_000_000],   # Current = 8.0M
        CUR_FISCAL_PROB_CHANGE: [5_000_000, -1_000_000],  # Δ = 4.0M
        YEAR1_PROB_PRIOR:       [1_000_000, 1_000_000],
        YEAR1_PROB_LE:          [2_000_000, 2_000_000],
    })


def test_prior_plan_registered_in_schema():
    assert COL_PRIOR_PLAN == DATA_COLS[0]  # leftmost FY27 column
    assert SAVED_COLUMN_LABELS[COL_PRIOR_PLAN] == "FY27 Probabilized | Prior Plan"


def test_prior_plan_equals_current_minus_total_delta():
    df, _warnings, _template = build_summary_report(_comp_df())
    total = df.loc[df[COL_ROW_ID] == "total_b2c"].iloc[0]
    # Current = 8.0, Total Δ = 4.0  →  Prior = 4.0.
    assert round(float(total[COL_CURRENT_PLAN]), 1) == 8.0
    assert round(float(total[COL_TOTAL_DELTA]), 1) == 4.0
    assert round(float(total[COL_PRIOR_PLAN]), 1) == 4.0
    # Identity holds on every row.
    assert (
        (df[COL_PRIOR_PLAN] - (df[COL_CURRENT_PLAN] - df[COL_TOTAL_DELTA]))
        .abs().max() < 0.05
    )


def test_prior_plan_label_present_in_export():
    df, _w, _t = build_summary_report(_comp_df())
    export = prepare_summary_for_export(df)
    assert "FY27 Probabilized | Prior Plan" in export.columns
