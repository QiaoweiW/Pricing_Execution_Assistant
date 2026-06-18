"""Tests for the Milk Commodity Cost summary table builder.

``_build_milk_commodity_summary`` derives the five FMMO advanced-price
components (Class I skim by Category, Class I butterfat, Class II skim /
butterfat) for the two most-recent months in the milk (HTST) JSON, plus the
month-over-month change. The fixtures mirror the operator's reference
snapshot so a regression in the lookup / fallback / change arithmetic is
caught immediately.
"""
from __future__ import annotations

import pandas as pd
import pytest

from pages.monthly_resin_freight_mover_tracker import (
    _MILK_SUMMARY_COLS,
    _build_milk_commodity_summary,
)

_LABEL, _CUR, _LAST, _CHG = _MILK_SUMMARY_COLS


def _two_month_milk_df() -> pd.DataFrame:
    """Two months of HTST/ESL Class I + Class II rows, snapshot values."""
    rows: list[dict] = []
    # (Month, HTST Class I skim, ESL Class I skim, Class I bfat,
    #  Class II skim, Class II bfat)
    for month, htst_skim, esl_skim, c1_bf, c2_skim, c2_bf in (
        ("2026-05-01", 0.1200, 0.1175, 2.0221, 0.1270, 1.7864),
        ("2026-06-01", 0.1412, 0.1363, 1.8649, 0.1482, 2.0290),
    ):
        rows.append({"Category": "HTST", "Month": month, "Class": "Class I",
                     "Skim Rate": htst_skim, "Butterfat Rate": c1_bf})
        rows.append({"Category": "ESL", "Month": month, "Class": "Class I",
                     "Skim Rate": esl_skim, "Butterfat Rate": c1_bf})
        rows.append({"Category": "ESL", "Month": month, "Class": "Class II",
                     "Skim Rate": c2_skim, "Butterfat Rate": c2_bf})
    return pd.DataFrame(rows)


def _rate(summary: pd.DataFrame, label: str, col: str):
    return summary.loc[summary[_LABEL] == label, col].iloc[0]


def test_summary_matches_reference_snapshot():
    """All five rows reconcile to the reference snapshot, latest month first."""
    summary, current_month, last_month = _build_milk_commodity_summary(
        _two_month_milk_df()
    )
    assert current_month == pd.Timestamp("2026-06-01")
    assert last_month == pd.Timestamp("2026-05-01")

    # (label, current, last, change)
    expected = [
        ("Class I Skim HTST",  0.1412, 0.1200, 0.0212),
        ("Class I Skim ESL",   0.1363, 0.1175, 0.0188),
        ("Class I Butterfat",  1.8649, 2.0221, -0.1572),
        ("Class II Skim",      0.1482, 0.1270, 0.0212),
        ("Class II Butterfat", 2.0290, 1.7864, 0.2426),
    ]
    assert list(summary[_LABEL]) == [e[0] for e in expected]
    for label, cur, last, chg in expected:
        assert _rate(summary, label, _CUR) == pytest.approx(cur)
        assert _rate(summary, label, _LAST) == pytest.approx(last)
        assert _rate(summary, label, _CHG) == pytest.approx(chg)


def test_class_i_skim_distinguishes_htst_from_esl():
    """The HTST vs ESL Class I skim rows must not collapse to one value."""
    summary, _, _ = _build_milk_commodity_summary(_two_month_milk_df())
    assert _rate(summary, "Class I Skim HTST", _CUR) != _rate(
        summary, "Class I Skim ESL", _CUR
    )


def test_category_fallback_when_preferred_family_absent():
    """A missing preferred Category falls back to any family with that rate.

    Here only HTST rows exist, yet ``Class I Skim ESL`` (preferred ESL)
    still resolves — to the HTST value — rather than blanking out.
    """
    df = pd.DataFrame([
        {"Category": "HTST", "Month": "2026-06-01", "Class": "Class I",
         "Skim Rate": 0.1412, "Butterfat Rate": 1.8649},
    ])
    summary, current_month, last_month = _build_milk_commodity_summary(df)
    assert last_month is None  # single month → no prior comparison
    assert _rate(summary, "Class I Skim ESL", _CUR) == pytest.approx(0.1412)
    # No prior month → change is blank (None).
    assert _rate(summary, "Class I Skim ESL", _CHG) is None


def test_empty_or_malformed_frame_yields_header_only():
    """An empty / column-less frame returns a header-only table and no months."""
    summary, current_month, last_month = _build_milk_commodity_summary(
        pd.DataFrame()
    )
    assert list(summary.columns) == list(_MILK_SUMMARY_COLS)
    assert summary.empty
    assert current_month is None and last_month is None
