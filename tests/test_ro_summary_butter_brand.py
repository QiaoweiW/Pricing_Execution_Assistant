"""RO Summary Report: Butter is segmented Branded vs Private Label per format.

The Branded / Private Label split keys on the derived Brand Category (← the
comparison ``Brand`` column, sourced from the distribution tracker's Brand — NOT
any Item-Description "DG" heuristic).  Fixture is a synthetic
``RO_Comparison_Output.csv``-shaped frame — no Fabric.
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
    COL_LABEL,
    COL_ROW_ID,
    build_summary_report,
)


def _comp_df() -> pd.DataFrame:
    """Butter / Sticks with a Branded row and a Private Label row.

    Brand column drives the split: 'DG Sticks' is a branded name (→ Branded),
    The comparison output carries the normalised Brand ('Private' for private
    label; a real brand name otherwise).  Description is deliberately the SAME on
    both rows so a DG-description heuristic could not distinguish them — only the
    Brand column can.
    """
    return pd.DataFrame({
        "Portfolio Major": ["Butter", "Butter"],
        "Supply Format":   ["Sticks", "Sticks"],
        "Portfolio Minor": ["", ""],
        "Brand":           ["Land O Lakes", "Private"],
        "Description":     ["BUTTER STICKS 1LB", "BUTTER STICKS 1LB"],
        "Driver":          ["New", "New"],
        ANNUAL_OPP_LE:          [10_000_000, 4_000_000],
        CUR_FISCAL_PROB_LE:     [6_000_000, 2_000_000],   # Branded 6.0M, PL 2.0M
        CUR_FISCAL_PROB_CHANGE: [6_000_000, 2_000_000],
        YEAR1_PROB_PRIOR:       [0, 0],
        YEAR1_PROB_LE:          [7_000_000, 3_000_000],
    })


def _row(df: pd.DataFrame, row_id: str) -> pd.Series:
    return df.loc[df[COL_ROW_ID] == row_id].iloc[0]


def test_butter_split_into_branded_and_private_label():
    df, _warnings, _template = build_summary_report(_comp_df())
    ids = set(df[COL_ROW_ID])

    # One Butter format subtotal (Sticks) with a Branded + Private Label leaf.
    fmt_ids = [r for r in ids if r.startswith("but_sfmt_")
               and not (r.endswith("_br") or r.endswith("_pv"))]
    assert len(fmt_ids) == 1
    fid = fmt_ids[0]
    assert f"{fid}_br" in ids and f"{fid}_pv" in ids

    # Leaf labels read "Branded" / "Private Label".
    assert _row(df, f"{fid}_br")[COL_LABEL].strip().endswith("Branded")
    assert _row(df, f"{fid}_pv")[COL_LABEL].strip().endswith("Private Label")

    # Split keyed on the Brand column: branded 6.0M, private 2.0M (millions).
    assert round(float(_row(df, f"{fid}_br")[COL_CURRENT_PLAN]), 1) == 6.0
    assert round(float(_row(df, f"{fid}_pv")[COL_CURRENT_PLAN]), 1) == 2.0

    # Format subtotal + Butter + Total B2C all roll up to 8.0M.
    assert round(float(_row(df, fid)[COL_CURRENT_PLAN]), 1) == 8.0
    assert round(float(_row(df, "but")[COL_CURRENT_PLAN]), 1) == 8.0
    assert round(float(_row(df, "total_b2c")[COL_CURRENT_PLAN]), 1) == 8.0
