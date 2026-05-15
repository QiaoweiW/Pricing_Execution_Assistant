"""
CME Group cash-dairy weekly-average scraper.

Why USDA's mirror and not cmegroup.com directly?
------------------------------------------------
The CME Group spot-call page at
``cmegroup.com/trading/agricultural/spot-call-data.html`` is a fully
client-rendered SPA — its tables are populated by JavaScript at runtime
and are NOT present in the HTML body returned by ``requests``. Reliably
scraping it would require a headless browser (Playwright / Selenium),
which is a heavy runtime dependency we want to avoid on Streamlit Cloud.

Fortunately USDA's Agricultural Marketing Service publishes the **same**
underlying CME-cash-trading data as a stable, text-native PDF every
Friday at 12:30 p.m. Central (or the last trading day of the week):

    https://www.ams.usda.gov/mnreports/ams_1602.pdf   (Slug ID 1602,
                                                       "CME Group –
                                                       Weekly Recap")

The PDF carries daily closes plus the canonical Weekly Average for all
four products (Cheese 40-LB Blocks, Butter Grade AA, Dry Whey Extra
Grade, Nonfat Dry Milk Grade A) and a Cheese Barrels series we ignore.
It's the same data the CME page would render — sourced from CME, simply
republished by USDA in a parseable form. The PDF gets a fresh report
number and a new "Report N, <Date>" header on every weekly release, so
the standard HTTP-fingerprint (ETag / Last-Modified / SHA-256) pattern
from :mod:`usda_milk_pdf` works unchanged.

Public entry points
-------------------
* :func:`fetch_recap_pdf_bytes(url=...)` — thin wrapper over the same
  ``requests.get`` we already use for USDA PDFs; returns ``(body,
  fingerprint)``.
* :func:`parse_weekly_recap(pdf_bytes)` — returns a
  :class:`WeeklyRecap` with the report's week-ending date and a
  ``{product → weekly_average}`` map for the four products the UI cares
  about.
* :func:`is_friday_after_9am_local(now)` — pure helper used by the
  scheduler gate; broken out so the store can call it without
  importing ``datetime``-formatting logic.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pdfplumber
import requests


# Public URL of the USDA mirror PDF. Centralised so callers can mock it
# in tests and so we don't repeat the string literal across modules.
CME_WEEKLY_RECAP_URL: str = "https://www.ams.usda.gov/mnreports/ams_1602.pdf"

# Live CME page — exposed via the UI's "source link" caption so the
# operator can verify the upstream data directly. The data we ingest
# comes from the USDA mirror (see module docstring), but the operator's
# mental model is "CME spot call".
CME_LIVE_PAGE_URL: str = (
    "https://www.cmegroup.com/trading/agricultural/spot-call-data.html"
)

# HTTP timing budget. Generous because the USDA CDN can be slow during
# heavy publishing windows (12:30 p.m. Central on Fridays).
_HTTP_TIMEOUT_SECONDS: float = 20.0

# Identifying User-Agent — politeness on shared CDNs.
_USER_AGENT: str = "DarigoldPricingAssistant/1.0 (cme-spot-call recap)"


# ── HTTP-level fingerprint (mirror of usda_milk_pdf._Fingerprint) ───────────

@dataclass(frozen=True)
class CMEFingerprint:
    """Change-detection fingerprint persisted alongside each fetch.

    Mirrors :class:`usda_milk_pdf._Fingerprint`. Kept as a separate
    public dataclass so the CME store can persist/compare it without
    importing internals of an unrelated module.
    """
    etag:           Optional[str]
    last_modified:  Optional[str]
    content_sha256: Optional[str]


def fetch_recap_pdf_bytes(
    url: str = CME_WEEKLY_RECAP_URL,
) -> tuple[bytes, CMEFingerprint]:
    """Download the CME Weekly Recap PDF + compute its fingerprint.

    Returns ``(body_bytes, fingerprint)``. The fingerprint includes the
    response's ``ETag`` / ``Last-Modified`` headers (when present) and
    a SHA-256 of the body so the store can detect changes even when
    USDA omits the cache headers.
    """
    resp = requests.get(
        url,
        timeout=_HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": _USER_AGENT},
        allow_redirects=True,
    )
    resp.raise_for_status()
    body = resp.content
    return body, CMEFingerprint(
        etag=resp.headers.get("ETag"),
        last_modified=resp.headers.get("Last-Modified"),
        content_sha256=hashlib.sha256(body).hexdigest(),
    )


# ── PDF parser ───────────────────────────────────────────────────────────────

# Report header anchor. pdfplumber's text-extraction lays the header out as
# three logical lines:
#
#     Agricultural Marketing Service Report 20
#     Dairy Market News
#     May 15, 2026
#     MMN Slug ID 1602 / Slug Name: MD_DA998
#
# We anchor on the date immediately PRECEDING the "MMN Slug ID" marker —
# that pair is uniquely identifying (no other date in the PDF body can
# precede a "MMN Slug ID" line). This also robustly survives USDA tweaking
# the spacing or interleaving "Dairy Market News" differently.
_REPORT_HEADER_RE = re.compile(
    r"([A-Z][a-z]+\s+\d{1,2},\s*\d{4})\s*\n\s*MMN\s+Slug\s+ID",
)

# Product blocks. Each pattern captures the Weekly Average $/lb value
# from the line "Weekly Average $<price> (<change>)" that closes the
# product's section. ``[\s\S]*?`` (non-greedy) lets the daily-closes
# table separate the heading from its Weekly Average without dragging
# us into the next product's block.
_VALUE_RE = r"\$(\d+\.\d+)"

_PRODUCT_LABEL_PATTERNS: dict[str, re.Pattern[str]] = {
    # We treat the user's "Cheese" as the canonical 40-LB Blocks series —
    # the dairy-industry benchmark. The CHEESE BARRELS block is captured
    # by a separate pattern below if a future caller wants it, but it's
    # NOT returned from ``parse_weekly_recap`` by default.
    "Cheese": re.compile(
        r"CHEESE 40 POUND BLOCKS[\s\S]*?Weekly\s+Average\s+" + _VALUE_RE
    ),
    "Butter": re.compile(
        r"BUTTER GRADE AA[\s\S]*?Weekly\s+Average\s+" + _VALUE_RE
    ),
    "Nonfat Dry Milk": re.compile(
        r"NONFAT DRY MILK GRADE A[\s\S]*?Weekly\s+Average\s+" + _VALUE_RE
    ),
    "Dry Whey": re.compile(
        r"DRY WHEY EXTRA GRADE[\s\S]*?Weekly\s+Average\s+" + _VALUE_RE
    ),
}

# Canonical product order — used when rendering chart series so traces
# always appear in the same legend order regardless of dict insertion.
PRODUCT_ORDER: tuple[str, ...] = ("Cheese", "Butter", "Nonfat Dry Milk", "Dry Whey")


@dataclass(frozen=True)
class WeeklyRecap:
    """Parsed result of a single CME Weekly Recap PDF.

    Attributes
    ----------
    week_ending
        Friday-of-publication date as a ``datetime``. Used as the
        time-series key for the historical store (dedup on
        ``(week_ending, product)``).
    weekly_averages
        ``{product → $/lb}`` for the four products in :data:`PRODUCT_ORDER`.
        A product is included only when the corresponding label was
        located on the PDF — see :attr:`missing_products`.
    missing_products
        Any product names from :data:`PRODUCT_ORDER` that could NOT be
        located in the PDF text. Empty in steady state. Surfaced to the
        UI so an operator can investigate without diving into logs.
    """
    week_ending:     datetime
    weekly_averages: dict[str, float]
    missing_products: tuple[str, ...]


def parse_weekly_recap(pdf_bytes: bytes) -> WeeklyRecap:
    """Parse a CME Weekly Recap PDF into a :class:`WeeklyRecap`.

    Raises ``ValueError`` ONLY when the report-header (week-ending) date
    is unparseable — without that we have no time key to bind values
    to. Individual missing product labels are surfaced via
    :attr:`WeeklyRecap.missing_products` so the caller can decide
    whether to record a partial week or skip.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # Reading every page (typically 1) lets the regex still find
        # the report header even if USDA paginates differently in the
        # future. The body of the report is small (~1 KB of text).
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)

    header_match = _REPORT_HEADER_RE.search(text)
    if not header_match:
        raise ValueError(
            "Could not locate the report header (e.g. 'Report 20 "
            "May 15, 2026') in the CME Weekly Recap PDF. The USDA "
            "publication layout may have changed."
        )

    try:
        # USDA's format is consistent: "%B %d, %Y" (e.g. "May 15, 2026").
        week_ending = datetime.strptime(
            header_match.group(1).strip().replace("  ", " "),
            "%B %d, %Y",
        )
    except ValueError as exc:
        raise ValueError(
            f"Could not parse the CME Weekly Recap header date "
            f"{header_match.group(1)!r}: {exc}"
        ) from exc

    weekly_averages: dict[str, float] = {}
    missing: list[str] = []
    for product, pattern in _PRODUCT_LABEL_PATTERNS.items():
        m = pattern.search(text)
        if not m:
            missing.append(product)
            continue
        try:
            weekly_averages[product] = float(m.group(1))
        except (TypeError, ValueError):
            missing.append(product)

    return WeeklyRecap(
        week_ending=week_ending,
        weekly_averages=weekly_averages,
        missing_products=tuple(missing),
    )


# ── Scheduler gate (Fridays @ 09:00 local) ───────────────────────────────────

def is_friday_after_9am_local(now: Optional[datetime] = None) -> bool:
    """Return True when ``now`` is on a Friday and local time ≥ 09:00.

    Pure / side-effect-free so it can be unit-tested without the
    fixture-laden ``freezegun``. The CME recap normally drops at
    12:30 p.m. Central; pulling at 09:00 local catches at least the
    Pacific-time release window for west-coast operators without
    over-fetching.

    Parameters
    ----------
    now
        Override for the current local time. Defaults to
        :func:`datetime.now` so callers don't have to construct one in
        production.
    """
    now = now or datetime.now()
    return now.weekday() == 4 and now.hour >= 9  # weekday(): Mon=0 … Fri=4


def most_recent_friday_9am_local(now: Optional[datetime] = None) -> datetime:
    """Return the most-recent Friday-09:00 strictly at or before ``now``.

    Used by the store's TTL guard so we can answer "have we successfully
    pulled since the last Friday-09:00 boundary?" Friday-after-9am
    returns today; any other moment returns last Friday (or today if
    today is Friday before 09:00 → returns last Friday too, by design).
    """
    now = now or datetime.now()
    # Number of days back to the most-recent Friday — wraps cleanly via
    # modulo. Saturday (5) → 1 day back; Friday (4) → 0 days; Thursday
    # (3) → 6 days. If today IS Friday but it's before 09:00, the
    # comparison ``now.hour >= 9`` is False, so we fall to "last Friday".
    today_is_fri = now.weekday() == 4
    days_back = (now.weekday() - 4) % 7
    if today_is_fri and now.hour < 9:
        days_back = 7
    base = now - _timedelta_days(days_back)
    return base.replace(hour=9, minute=0, second=0, microsecond=0)


def _timedelta_days(days: int):
    """Tiny helper so the file doesn't grow a ``datetime`` re-import."""
    from datetime import timedelta
    return timedelta(days=days)


__all__ = [
    "CMEFingerprint",
    "WeeklyRecap",
    "CME_WEEKLY_RECAP_URL",
    "CME_LIVE_PAGE_URL",
    "PRODUCT_ORDER",
    "fetch_recap_pdf_bytes",
    "parse_weekly_recap",
    "is_friday_after_9am_local",
    "most_recent_friday_9am_local",
]
