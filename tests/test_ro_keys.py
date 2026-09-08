"""Unit tests for :mod:`data_sources.ro_keys` — the R&O business-key canonicaliser.

This module exists because the stages spell the same key differently
(``RO_Seed`` keeps ``Brand = "PL"``, ``RO_Comparison_Output`` normalises it to
``"Private"``), which made the reconciliation report one line as missing from
both files at once.  These tests pin the canonicalisation both sides now share.
"""
from __future__ import annotations

import pytest

from data_sources import ro_keys as rk


# ── Brand ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "PL", "pl", "Pl", " PL ", "Private Label", "private label",
    "private-label", "PrivateLabel", "  Private-Label  ",
])
def test_every_private_label_spelling_maps_to_the_canonical_form(raw):
    assert rk.canonical_brand(raw) == rk.PRIVATE_LABEL == "Private"


@pytest.mark.parametrize("raw", ["Darigold", "Branded", "DG", "Organic"])
def test_other_brands_pass_through_untouched(raw):
    assert rk.canonical_brand(raw) == raw


def test_surrounding_whitespace_is_stripped():
    assert rk.canonical_brand("  Darigold  ") == "Darigold"


@pytest.mark.parametrize("blank", [None, "", "   ", float("nan")])
def test_a_blank_brand_stays_blank_rather_than_being_invented(blank):
    """Callers surface a "fill me in" warning; guessing would hide the gap."""
    assert rk.canonical_brand(blank) == ""


def test_private_is_idempotent():
    once = rk.canonical_brand("PL")
    assert rk.canonical_brand(once) == once


# ── Generic cells ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (58, "58"),
    ("58", "58"),
    (58.0, "58"),
    ("58.0", "58"),
    (" 58 ", "58"),
    ("-58.0", "-58"),
])
def test_item_numbers_collapse_across_dtypes(raw, expected):
    """A NaN elsewhere in the column coerces ints to floats; both must match."""
    assert rk.canonical_cell(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", float("nan")])
def test_blank_cells_become_empty_string(raw):
    assert rk.canonical_cell(raw) == ""


def test_a_genuine_decimal_is_not_truncated():
    assert rk.canonical_cell("58.5") == "58.5"
    assert rk.canonical_cell("1.10") == "1.10"


def test_text_is_left_alone_apart_from_stripping():
    assert rk.canonical_cell("  ESL Whipping Cream ") == "ESL Whipping Cream"


# ── Per-column dispatch ──────────────────────────────────────────────────────

def test_only_the_brand_column_gets_the_brand_treatment():
    assert rk.canonical_key_cell("Brand", "PL") == "Private"
    # A customer legitimately called "PL" must not become "Private".
    assert rk.canonical_key_cell("Customer", "PL") == "PL"
    assert rk.canonical_key_cell("Taxonomy", "PL") == "PL"


def test_the_key_columns_are_the_five_the_pipeline_uses():
    assert rk.BUSINESS_KEY_COLS == (
        "Format", "Customer", "Taxonomy", "Brand", "Item #",
    )


def test_reconcile_reexports_the_same_key_definition():
    """One definition — the diagnostic must not drift from the pipeline."""
    from data_sources.ro_risk_reconcile import BUSINESS_KEY_COLS
    assert BUSINESS_KEY_COLS == rk.BUSINESS_KEY_COLS


def test_the_comparison_builder_shares_the_brand_map():
    """ro_comparison's own normaliser must be the same function underneath."""
    from data_sources.ro_comparison import _normalize_brand
    for raw in ("PL", "private label", "Darigold", "", None):
        assert _normalize_brand(raw) == rk.canonical_brand(raw)


def test_canonical_cell_matches_the_dim_cascade_key():
    """The pre-flight looks items up in the cascade's ``__item_key`` index.

    ``build_item_dim_frame`` builds that key with ``_vectorised_item_key``; the
    validator builds its side with :func:`canonical_cell`.  If the two ever
    diverge the check reports items that classify perfectly well, so pin the
    equality here rather than relying on the two docstrings agreeing.
    """
    import pandas as pd
    from data_sources.demand_plan_comparison import _vectorised_item_key

    for value in ["370072.0", " 58 ", "P-37.0", "0340021", "340021",
                  58, 58.0, "", "-58.0"]:
        cascade = _vectorised_item_key(pd.Series([value])).iloc[0]
        assert rk.canonical_cell(value) == cascade, value
