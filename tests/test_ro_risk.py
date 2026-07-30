"""Unit tests for the shared R&O risk rule (data_sources.ro_risk).

Canonical rule: Reflected-in-APS = "no" AND anticipated volume < 0 AND
probability ≥ 50%.  The Streamlit rules panel can override the probability
threshold and the volume gate at runtime via keyword args.
"""
from __future__ import annotations

import pandas as pd

from data_sources.ro_risk import RISK_PROBABILITY, risk_mask


def _df():
    return pd.DataFrame({
        "vol":  [-100.0, -100.0, -100.0, 100.0, -100.0, -100.0],
        "prob": [1.0,    0.49,   0.50,   1.0,   1.0,    0.75],
        "refl": ["no",   "no",   "no",   "no",  "yes",  "NO"],
    })


def test_three_conditions_all_required_at_default_threshold():
    """Default 50% threshold: probability ≥ 0.5 counts as risk."""
    m = risk_mask(_df(), volume_col="vol", probability_col="prob",
                  reflected_col="refl")
    # row 0: neg + 100% + no        -> risk
    # row 1: neg + 49%  + no        -> NOT (below threshold)
    # row 2: neg + 50%  + no        -> risk (>= threshold)
    # row 3: POS + 100% + no        -> NOT (volume not negative)
    # row 4: neg + 100% + reflected -> NOT (APS-reflected)
    # row 5: neg + 75%  + "NO"      -> risk (case-insensitive)
    assert list(m) == [True, False, True, False, False, True]


def test_reflected_optional_when_already_filtered_upstream():
    # No reflected_col → condition 1 treated as satisfied (downstream stages).
    m = risk_mask(_df(), volume_col="vol", probability_col="prob")
    assert list(m) == [True, False, True, False, True, True]


def test_min_probability_override_tightens_the_rule():
    """A user setting the threshold to 100% collapses to yesterday's tight rule."""
    m = risk_mask(_df(), volume_col="vol", probability_col="prob",
                  reflected_col="refl", min_probability=1.0)
    # Only 100%-prob rows survive: 0 and 4-and-5 (case-insensitive).
    assert list(m) == [True, False, False, False, False, False]


def test_require_negative_volume_can_be_disabled():
    """A user widening Risk to any probable line drops the negative-vol gate."""
    m = risk_mask(_df(), volume_col="vol", probability_col="prob",
                  reflected_col="refl", require_negative_volume=False)
    # Now row 3 (POS + 100% + no) qualifies too.
    assert list(m) == [True, False, True, True, False, True]


def test_missing_columns_never_raise():
    df = pd.DataFrame({"vol": [-1.0]})
    assert not risk_mask(df, volume_col="vol", probability_col="prob").any()
    assert not risk_mask(df, volume_col="nope", probability_col="prob").any()


def test_risk_probability_default_is_fifty_percent():
    assert RISK_PROBABILITY == 0.5
