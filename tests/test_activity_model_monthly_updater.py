"""Unit tests for the Activity_Model monthly updater's currency-aware rules.

Regression guard for the "$X.XX base overwritten by the delta" bug: the money
columns are stored as dollar strings ("$0.82"), and the Delivery / PPPI rules
must ADD the mover on top of the parsed base — not treat the base as 0.
Fabric I/O (archive + write) is monkeypatched, so no network is involved.
"""
from __future__ import annotations

import pandas as pd
import pytest

import data_sources.activity_model_monthly_updater as au


TARGET = pd.Timestamp(2026, 8, 1)


@pytest.fixture
def captured_write(monkeypatch):
    """Capture the DataFrame written by each rule; stub the archive step."""
    box: dict[str, pd.DataFrame] = {}

    def _fake_write(_secrets, _blob, df, etag=None):  # noqa: ANN001
        box["df"] = df.copy()
        return "etag-xyz"

    monkeypatch.setattr(au._io, "write_csv", _fake_write)
    monkeypatch.setattr(au, "_archive_existing_blob", lambda **k: None)
    return box


# ── Currency helpers ─────────────────────────────────────────────────────────

def test_parse_currency():
    assert au._parse_currency("$0.82") == pytest.approx(0.82)
    assert au._parse_currency("1,234.5") == pytest.approx(1234.5)
    assert au._parse_currency(0.82) == pytest.approx(0.82)
    assert au._parse_currency("") == 0.0
    assert au._parse_currency(None) == 0.0
    assert au._parse_currency("garbage") == 0.0
    assert au._parse_currency(float("nan")) == 0.0


def test_format_currency():
    assert au._format_currency(0.87) == "$0.87"
    assert au._format_currency(0.0) == "$0.00"


# ── Delivery rule: incorporate (base + delta), don't overwrite ───────────────

def test_delivery_rule_adds_delta_to_dollar_string_base(captured_write):
    df = pd.DataFrame({
        au._DELIVERY_COL_MILEAGE_TIER: ["A. >= 1000 mi", "N/A"],
        au._DELIVERY_COL_CHARGE: ["$0.82", "$0.00"],
    })
    res = au._apply_delivery_rule(df, etag=None, target_month=TARGET, rest_freight=0.05)
    assert res.ok
    out = captured_write["df"]
    # Base $0.82 + $0.05 delta = $0.87 (NOT overwritten to $0.05).
    assert out[au._DELIVERY_COL_CHARGE].iloc[0] == "$0.87"
    # N/A tier stays $0.00.
    assert out[au._DELIVERY_COL_CHARGE].iloc[1] == "$0.00"


# ── PPPI rule: matched = base + resin mover; unmatched untouched ─────────────

def test_pppi_rule_adds_mover_to_dollar_string_packaging(captured_write):
    df = pd.DataFrame({
        au._PPPI_COL_ITEM: ["100", "200"],
        au._PPPI_COL_PACKAGING: ["$1.20", "$2.00"],
    })
    fg = pd.DataFrame({
        au._FG_COL_PRODUCT_ID: ["100"],
        au._FG_COL_MOVER: ["$0.30"],   # dollar-string mover → parsed defensively
    })
    res = au._apply_pppi_rule(df, etag=None, target_month=TARGET, fg_df=fg)
    assert res.ok
    out = captured_write["df"]
    # Matched: $1.20 base + $0.30 mover = $1.50.
    assert out[au._PPPI_COL_PACKAGING].iloc[0] == "$1.50"
    # Unmatched row untouched.
    assert out[au._PPPI_COL_PACKAGING].iloc[1] == "$2.00"
    assert res.rows_changed == 1
