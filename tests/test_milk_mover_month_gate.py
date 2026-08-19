"""Unit tests for the Milk-Mover new-month gate.

Covers:
* ``usda_milk_pdf.parse_advanced_prices_month`` — reading the page-1 banner
  that names the announced month (pdfplumber stubbed; no network).
* ``milk_mover_autoupdate._new_month_skip_reason`` — the pure gate: append the
  announced month only when it is newer than the file's latest month.
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


# Verbatim page-1 excerpt from the live PDF (ADV-0926, 19 Aug 2026) — the
# wording the parser must handle.  Note that "ADVANCED PRICES FOR <MONTH>"
# appears NOWHERE in the real document; assuming it did was the bug that
# stalled every ingest.
_LIVE_PAGE_1 = """Announcement of Advanced
Prices and Pricing Factors
ADV - 0926 August 19, 2026
September 2026 Highlights
Base Class I Price was $17.04 per hundredweight for the month of September 2026.
Announcement of Advanced Prices and Pricing Factors for September 2026
Base Class I Price: $17.04 (per hundredweight)
Federal Milk Order Class I and Class II Advanced Prices and Pricing Factors, 2026
"""


def test_parse_month_live_pdf_wording(monkeypatch):
    _stub_pdf(monkeypatch, _LIVE_PAGE_1)
    assert upm.parse_advanced_prices_month(b"x") == date(2026, 9, 1)


def test_parse_month_highlights_heading_only(monkeypatch):
    # Announcement line reworded / missing → the "<Month> <Year> Highlights"
    # heading still carries the answer.
    _stub_pdf(monkeypatch, "ADV - 0926\nSeptember 2026 Highlights\nBase ...")
    assert upm.parse_advanced_prices_month(b"x") == date(2026, 9, 1)


def test_parse_month_ignores_history_table_header(monkeypatch):
    # The per-year table header has no " for <Month> <Year>" — it must not be
    # mistaken for the announcement banner.
    _stub_pdf(
        monkeypatch,
        "Federal Milk Order Class I and Class II Advanced Prices and "
        "Pricing Factors, 2026\nJan 1.00 2.00\n",
    )
    assert upm.parse_advanced_prices_month(b"x") is None


def test_parse_month_legacy_wording(monkeypatch):
    _stub_pdf(monkeypatch, "USDA ...\nADVANCED PRICES FOR JULY 2026\nBase Skim ...")
    assert upm.parse_advanced_prices_month(b"x") == date(2026, 7, 1)


def test_parse_month_case_and_abbrev(monkeypatch):
    _stub_pdf(monkeypatch, "advanced prices and pricing factors for sep 2027")
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


def test_gate_writes_when_announced_is_next_month():
    # File latest = Aug AND PDF announces Sep → write Sep (no skip reason).
    assert au._new_month_skip_reason(_AUG, date(2026, 9, 1)) is None


def test_gate_blocks_when_announced_already_stored():
    # Re-tick on the same publication: file latest = Aug, PDF announces Aug →
    # nothing to append (this is the dedup guard).
    reason = au._new_month_skip_reason(_AUG, date(2026, 8, 1))
    assert reason is not None
    assert "Aug 2026" in reason


def test_gate_blocks_when_announced_behind_file_max():
    # File latest already = Aug, PDF still announces Jul → do NOT write.
    reason = au._new_month_skip_reason(_AUG, date(2026, 7, 1))
    assert reason is not None
    assert "Jul 2026" in reason and "Aug 2026" in reason


def test_gate_writes_across_a_gap():
    # Two publications missed (file latest = Jul, PDF announces Oct): the
    # announced month is still written; the orchestrator warns about the hole.
    assert au._new_month_skip_reason(_JUL, date(2026, 10, 1)) is None


def test_gate_blocks_when_month_unreadable():
    # Without the banner we cannot tell which month the headline values
    # describe, so nothing is written — even on an empty file.
    for file_max in (_JUL, None):
        reason = au._new_month_skip_reason(file_max, None)
        assert reason is not None
        assert "Could not read" in reason


def test_gate_allows_bootstrap_when_file_empty():
    # Empty pre-seed file (file_max None) → bootstrap write proceeds.
    assert au._new_month_skip_reason(None, date(2026, 7, 1)) is None
