"""Tests for the Milk Commodity Cost summary table builder.

``_build_milk_commodity_summary`` derives the five FMMO advanced-price
components (Class I skim by Category, Class I butterfat, Class II skim /
butterfat) for two **explicit** months.  The table leads with the chart
slicer's **End Month** (the :data:`_MILK_COL_END` column) and follows with its
**Start Month** (:data:`_MILK_COL_START`), plus the change between them
(``End − Start``).  The fixtures mirror the operator's reference snapshot so a
regression in the lookup / fallback / change arithmetic is caught immediately.
"""
from __future__ import annotations

import pandas as pd
import pytest

from pages.monthly_resin_freight_mover_tracker import (
    _MILK_COL_END,
    _MILK_COL_START,
    _MILK_SUMMARY_COLS,
    _build_milk_commodity_summary,
)

_LABEL, _END, _START, _CHG = _MILK_SUMMARY_COLS


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


def test_value_columns_lead_with_end_then_start():
    """The frame's two value columns are ordered End-month then Start-month."""
    assert _MILK_SUMMARY_COLS[1] == _MILK_COL_END
    assert _MILK_SUMMARY_COLS[2] == _MILK_COL_START


def test_summary_matches_reference_snapshot():
    """All five rows reconcile to the reference snapshot for the chosen months.

    End Month = June (leading column), Start Month = May (following column);
    the builder resolves rates for exactly the months passed and the change is
    ``End − Start``.
    """
    summary = _build_milk_commodity_summary(
        _two_month_milk_df(),
        end_month=pd.Timestamp("2026-06-01"),
        start_month=pd.Timestamp("2026-05-01"),
    )

    # (label, end (June), start (May), change = End − Start)
    expected = [
        ("Class I Skim HTST",  0.1412, 0.1200, 0.0212),
        ("Class I Skim ESL",   0.1363, 0.1175, 0.0188),
        ("Class I Butterfat",  1.8649, 2.0221, -0.1572),
        ("Class II Skim",      0.1482, 0.1270, 0.0212),
        ("Class II Butterfat", 2.0290, 1.7864, 0.2426),
    ]
    assert list(summary[_LABEL]) == [e[0] for e in expected]
    for label, end_rate, start_rate, chg in expected:
        assert _rate(summary, label, _END) == pytest.approx(end_rate)
        assert _rate(summary, label, _START) == pytest.approx(start_rate)
        assert _rate(summary, label, _CHG) == pytest.approx(chg)


def test_start_end_months_drive_the_columns():
    """Swapping the two months swaps the End/Start columns accordingly."""
    df = _two_month_milk_df()
    swapped = _build_milk_commodity_summary(
        df,
        end_month=pd.Timestamp("2026-05-01"),    # End = May
        start_month=pd.Timestamp("2026-06-01"),  # Start = June
    )
    # End column now reflects May; Start column reflects June; change = End−Start.
    assert _rate(swapped, "Class I Skim HTST", _END) == pytest.approx(0.1200)
    assert _rate(swapped, "Class I Skim HTST", _START) == pytest.approx(0.1412)
    assert _rate(swapped, "Class I Skim HTST", _CHG) == pytest.approx(-0.0212)


def test_class_i_skim_distinguishes_htst_from_esl():
    """The HTST vs ESL Class I skim rows must not collapse to one value."""
    summary = _build_milk_commodity_summary(
        _two_month_milk_df(),
        end_month=pd.Timestamp("2026-06-01"),
        start_month=pd.Timestamp("2026-05-01"),
    )
    assert _rate(summary, "Class I Skim HTST", _END) != _rate(
        summary, "Class I Skim ESL", _END
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
    # Start Month has no rows → its column and the change blank out.
    summary = _build_milk_commodity_summary(
        df,
        end_month=pd.Timestamp("2026-06-01"),
        start_month=None,
    )
    assert _rate(summary, "Class I Skim ESL", _END) == pytest.approx(0.1412)
    # No start month → change is blank (None).
    assert _rate(summary, "Class I Skim ESL", _CHG) is None


def test_empty_or_malformed_frame_yields_header_only():
    """An empty / column-less frame returns a header-only table."""
    summary = _build_milk_commodity_summary(
        pd.DataFrame(),
        end_month=pd.Timestamp("2026-06-01"),
        start_month=pd.Timestamp("2026-05-01"),
    )
    assert list(summary.columns) == list(_MILK_SUMMARY_COLS)
    assert summary.empty
