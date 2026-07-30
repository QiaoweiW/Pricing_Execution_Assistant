"""Unit tests for the shared R&O risk rule (data_sources.ro_risk).

A risk = Reflected-in-APS=no AND anticipated volume < 0 AND probability = 100%.
"""
from __future__ import annotations

import pandas as pd

from data_sources.ro_risk import RISK_PROBABILITY, risk_mask


def _df():
    return pd.DataFrame({
        "vol":  [-100.0, -100.0, -100.0, 100.0, -100.0],
        "prob": [1.0,    0.99,   1.0,    1.0,   1.0],
        "refl": ["no",   "no",   "yes",  "no",  "NO"],
    })


def test_three_conditions_all_required():
    m = risk_mask(_df(), volume_col="vol", probability_col="prob",
                  reflected_col="refl")
    # row 0: neg + 100% + no        -> risk
    # row 1: neg + 99%  + no        -> NOT (probability < 100%)
    # row 2: neg + 100% + reflected -> NOT (reflected in APS)
    # row 3: POS + 100% + no        -> NOT (volume not negative)
    # row 4: neg + 100% + "NO"      -> risk (case-insensitive)
    assert list(m) == [True, False, False, False, True]


def test_reflected_optional_when_already_filtered_upstream():
    # No reflected_col → condition 1 treated as satisfied (downstream stages).
    m = risk_mask(_df(), volume_col="vol", probability_col="prob")
    assert list(m) == [True, False, True, False, True]


def test_missing_columns_never_raise():
    df = pd.DataFrame({"vol": [-1.0]})
    assert not risk_mask(df, volume_col="vol", probability_col="prob").any()
    assert not risk_mask(df, volume_col="nope", probability_col="prob").any()


def test_risk_probability_is_one():
    assert RISK_PROBABILITY == 1.0
