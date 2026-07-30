"""RO Summary Report: the Delta Breakdown "Risk" column.

A line whose latest anticipated annual volume is negative (LE Annual
Opportunity < 0) AND whose LE Probability clears the Risk threshold (default
50%) is reported under Risk instead of New/Exit/Change, whatever its Driver.
New + Exit + Change + Risk must still equal Total Delta, and Prior Plan /
Current Plan / Total Delta (the columns other sections read) must be unchanged
by the split.  Synthetic RO_Comparison_Output-shaped frame — no Fabric.
"""
from __future__ import annotations

import pandas as pd

from data_sources.ro_comparison import (
    ANNUAL_OPP_LE,
    CUR_FISCAL_PROB_CHANGE,
    CUR_FISCAL_PROB_LE,
    YEAR1_PROB_LE,
    YEAR1_PROB_PRIOR,
)
from data_sources.ro_summary_report import (
    COL_CURRENT_PLAN,
    COL_DELTA_CHANGE,
    COL_DELTA_EXIT,
    COL_DELTA_NEW,
    COL_DELTA_RISK,
    COL_ROW_ID,
    COL_TOTAL_DELTA,
    build_summary_report,
)


def _comp_df() -> pd.DataFrame:
    """Three ESL Large Carton lines; the middle one is a Risk — negative LE vol
    AND probability at or above the 50% Risk threshold (Reflected-in-APS is
    already filtered upstream)."""
    return pd.DataFrame({
        "Portfolio Major": ["ESL", "ESL", "ESL"],
        "Supply Format":   ["Large Carton", "Large Carton", "Large Carton"],
        "Portfolio Minor": ["", "", ""],
        "Brand":           ["DG", "DG", "DG"],
        "Description":     ["A", "B", "C"],
        "Driver":          ["Change", "Change", "New"],
        # Line 2 is a Risk: NEGATIVE annual opportunity AND 75% probability
        # (clears the 50% default Risk threshold), even though Driver="Change".
        ANNUAL_OPP_LE:          [10_000_000, -8_000_000, 4_000_000],
        "LE Probability":       [0.9, 0.75, 0.9],
        CUR_FISCAL_PROB_LE:     [5_000_000, 1_000_000, 2_000_000],
        CUR_FISCAL_PROB_CHANGE: [5_000_000, -3_000_000, 2_000_000],
        YEAR1_PROB_PRIOR:       [0, 0, 0],
        YEAR1_PROB_LE:          [0, 0, 0],
    })


def _total(df: pd.DataFrame, col: str) -> float:
    return round(float(df.loc[df[COL_ROW_ID] == "total_b2c"].iloc[0][col]), 1)


def test_risk_column_carves_negative_volume_out_of_the_breakdown():
    df, _warnings, _template = build_summary_report(_comp_df())

    # Risk = the negative-LE-volume line's probabilized change (-3.0M).
    assert _total(df, COL_DELTA_RISK) == -3.0
    # New keeps the New line (2.0M); Change keeps ONLY the non-risk change line
    # (5.0M) — the -3.0M risk line is carved out; Exit is 0.
    assert _total(df, COL_DELTA_NEW) == 2.0
    assert _total(df, COL_DELTA_CHANGE) == 5.0
    assert _total(df, COL_DELTA_EXIT) == 0.0
    # New + Exit + Change + Risk == Total Delta (unchanged by the split).
    assert _total(df, COL_TOTAL_DELTA) == 4.0
    assert (
        _total(df, COL_DELTA_NEW) + _total(df, COL_DELTA_EXIT)
        + _total(df, COL_DELTA_CHANGE) + _total(df, COL_DELTA_RISK)
        == _total(df, COL_TOTAL_DELTA)
    )
    # Current Plan is untouched (still sums every line): 5 + 1 + 2 = 8.0M.
    assert _total(df, COL_CURRENT_PLAN) == 8.0


def test_no_risk_when_no_negative_volume():
    comp = _comp_df()
    comp[ANNUAL_OPP_LE] = [10_000_000, 8_000_000, 4_000_000]   # all positive
    df, _warnings, _template = build_summary_report(comp)
    assert _total(df, COL_DELTA_RISK) == 0.0
    # Change now keeps both change lines (5.0 + -3.0 = 2.0); total still 4.0.
    assert _total(df, COL_DELTA_CHANGE) == 2.0
    assert _total(df, COL_TOTAL_DELTA) == 4.0


def test_negative_below_default_50pct_probability_is_not_risk():
    """A negative line under the 50% Risk threshold stays in Change."""
    comp = _comp_df()
    comp["LE Probability"] = [0.9, 0.49, 0.9]   # negative line drops to 49%
    df, _warnings, _template = build_summary_report(comp)
    assert _total(df, COL_DELTA_RISK) == 0.0          # below threshold
    assert _total(df, COL_DELTA_CHANGE) == 2.0        # -3.0 stays in Change (5.0 - 3.0)
    assert _total(df, COL_TOTAL_DELTA) == 4.0         # total unchanged


def test_user_config_override_can_tighten_risk_threshold_to_100pct():
    """Passing a config raises the Risk threshold; the 75%-prob line drops out."""
    from data_sources.ro_rules_config import RoRulesConfig
    cfg = RoRulesConfig.default().with_updates(min_risk_probability=1.0)
    df, _warnings, _template = build_summary_report(_comp_df(), config=cfg)
    # 75% no longer clears the 100% threshold → line falls back into Change.
    assert _total(df, COL_DELTA_RISK) == 0.0
    assert _total(df, COL_DELTA_CHANGE) == 2.0        # 5.0 + -3.0
    assert _total(df, COL_TOTAL_DELTA) == 4.0
