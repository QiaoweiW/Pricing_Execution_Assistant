"""Unit tests for the rewired Milk-Mover new-month gate.

Covers:
* ``usda_milk_pdf.parse_advanced_prices_month`` — reading the page-1
  "ADVANCED PRICES FOR <MONTH YYYY>" banner (pdfplumber stubbed; no network).
* ``milk_mover_autoupdate._new_month_skip_reason`` — the pure gate: only append
  ``file_max + 1`` when the PDF announces the month already latest in the file.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

import data_sources.usda_milk_pdf as upm
import data_sources.milk_mover_autoupdate as au


# ── parse_advanced_prices_month ─────────────────────────────────────────────

class _FakePage:
    def __init__(self, text: str):
        self._t = text

    def extract_text(self):
        return self._t


class _FakePDF:
    def __init__(self, text: str):
        self.pages = [_FakePage(text)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_pdf(monkeypatch, text: str):
    monkeypatch.setattr(upm.pdfplumber, "open", lambda _b: _FakePDF(text))


def test_parse_month_full_name(monkeypatch):
    _stub_pdf(monkeypatch, "USDA ...\nADVANCED PRICES FOR JULY 2026\nBase Skim ...")
    assert upm.parse_advanced_prices_month(b"x") == date(2026, 7, 1)


def test_parse_month_case_and_abbrev(monkeypatch):
    _stub_pdf(monkeypatch, "advanced prices for sep 2027")
    assert upm.parse_advanced_prices_month(b"x") == date(2027, 9, 1)


def test_parse_month_missing_banner(monkeypatch):
    _stub_pdf(monkeypatch, "no banner here")
    assert upm.parse_advanced_prices_month(b"x") is None


def test_parse_month_unrecognised_word(monkeypatch):
    _stub_pdf(monkeypatch, "ADVANCED PRICES FOR SMARCH 2026")
    assert upm.parse_advanced_prices_month(b"x") is None


# ── _new_month_skip_reason (the gate) ───────────────────────────────────────

_JUL = pd.Timestamp(2026, 7, 1)
_AUG = pd.Timestamp(2026, 8, 1)


def test_gate_writes_when_announced_equals_file_max():
    # File latest = Jul AND PDF announces Jul → write Aug (no skip reason).
    assert au._new_month_skip_reason(_JUL, date(2026, 7, 1)) is None


def test_gate_blocks_when_announced_behind_file_max():
    # File latest already = Aug, PDF still announces Jul → do NOT write Sep.
    reason = au._new_month_skip_reason(_AUG, date(2026, 7, 1))
    assert reason is not None
    assert "Jul 2026" in reason and "Aug 2026" in reason


def test_gate_blocks_when_month_unreadable():
    reason = au._new_month_skip_reason(_JUL, None)
    assert reason is not None
    assert "Could not read" in reason


def test_gate_allows_bootstrap_when_file_empty():
    # Empty pre-seed file (file_max None) → bootstrap write proceeds.
    assert au._new_month_skip_reason(None, date(2026, 7, 1)) is None
    assert au._new_month_skip_reason(None, None) is None
