"""
Parser for USDA's National Dairy Products Sales Report PDF.

URL:  https://www.ams.usda.gov/mnreports/dywdairyproductssales.pdf

Layout (as of May 2026)
-----------------------
The PDF carries five week-ending columns per product, named "DD-MMM"
(e.g. ``9-May``). Each product block looks like::

    <Product> Prices and Sales
    United States 11-Apr 18-Apr 25-Apr 2-May 9-May
    (dollars per pound)
    Weighted Price 0.6458 *0.6504 0.6342 *0.6356 0.6415
    (pounds)
    Sales 7,127,795 *8,337,556 11,271,048 *9,555,050 9,027,787
    *Revised

The leading ``*`` flags a "revised" figure — USDA revises the prior
four weeks as new data arrives. We strip the asterisk and treat the
value as authoritative for that week (USDA's own convention).

The report header carries the full date range::

    National Dairy Products Sales Report for Weeks Ending: 4/11/2026 - 5/9/2026

We anchor on the END date of that range to pin the year — the "DD-MMM"
values inside the blocks are otherwise year-ambiguous around year
boundaries.

Public entry points
-------------------
* :func:`fetch_pdf_bytes` — thin wrapper over ``requests.get`` with the
  same fingerprint pattern as ``cme_spot_call_scraper.fetch_recap_pdf_bytes``.
* :func:`parse_dairy_products_sales(pdf_bytes)` — returns a
  :class:`DairyProductsRecap` carrying the date range plus per-product
  weekly rows.

Only Dry Whey + Nonfat Dry Milk are the user-facing products today
(``USER_FACING_PRODUCTS``). The parser still extracts Butter and
Cheddar Cheese as a courtesy so we can pivot to them later without
re-implementing the parser — :class:`DairyProductsRecap` carries the
full set.
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


# ── Constants ────────────────────────────────────────────────────────────────

USDA_DAIRY_PRODUCTS_URL: str = (
    "https://www.ams.usda.gov/mnreports/dywdairyproductssales.pdf"
)

# HTTP-client polish — generous timeout because USDA's CDN can stall
# briefly on Tuesday-publication windows.
_HTTP_TIMEOUT_SECONDS: float = 20.0
_USER_AGENT: str = "DarigoldPricingAssistant/1.0 (dairy-products-sales)"


# Canonical product labels — the UI calls these out by name. We use
# "Cheddar Cheese" rather than just "Cheese" because that's how USDA
# titles the block; the parallel CME report uses "Cheese" so we keep
# the names distinct to avoid implicit data mixing in the UI.
PRODUCT_BUTTER:            str = "Butter"
PRODUCT_CHEDDAR_CHEESE:    str = "Cheddar Cheese"
PRODUCT_DRY_WHEY:          str = "Dry Whey"
PRODUCT_NONFAT_DRY_MILK:   str = "Nonfat Dry Milk"

# Products the UI surfaces today (per the May-2026 spec).
USER_FACING_PRODUCTS: tuple[str, ...] = (PRODUCT_DRY_WHEY, PRODUCT_NONFAT_DRY_MILK)

# Map a product label → the regex that finds its block start. Each
# block header is unique enough that we don't need anchoring text
# beyond the product name itself.
_PRODUCT_HEADER_RE: dict[str, re.Pattern[str]] = {
    PRODUCT_BUTTER:           re.compile(r"Butter\s+Prices\s+and\s+Sales"),
    PRODUCT_CHEDDAR_CHEESE:   re.compile(r"40-Pound\s+Block\s+Cheddar\s+Cheese\s+Prices\s+and\s+Sales"),
    PRODUCT_DRY_WHEY:         re.compile(r"Dry\s+Whey\s+Prices\s+and\s+Sales"),
    PRODUCT_NONFAT_DRY_MILK:  re.compile(r"Nonfat\s+Dry\s+Milk\s+Prices\s+and\s+Sales"),
}

# Report header: "Weeks Ending: 4/11/2026 - 5/9/2026". The end date is the
# most-recent week-ending in the table; we use it to disambiguate the
# DD-MMM column headers.
_REPORT_DATE_RANGE_RE = re.compile(
    r"Weeks\s+Ending\s*[:\-]?\s*"
    r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})"
)

# "United States 11-Apr 18-Apr 25-Apr 2-May 9-May" — the five DD-MMM
# week-ending column headers. Captured greedily; we then split on
# whitespace.
_WEEKLY_DATE_HEADER_RE = re.compile(
    r"United\s+States\s+((?:\d{1,2}-[A-Za-z]{3}\s*){5})"
)

# "Weighted Price *1.7767 1.7494 1.7450 *1.7118 1.6799" — five values,
# each optionally prefixed with ``*``. We capture them as one big group
# then split.
_WEIGHTED_PRICE_RE = re.compile(
    r"Weighted\s+Price\s+((?:\*?\d+\.\d+\s*){5})"
)


# ── HTTP fingerprint ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DairyProductsFingerprint:
    """HTTP-level fingerprint persisted alongside each fetch."""
    etag:           Optional[str]
    last_modified:  Optional[str]
    content_sha256: Optional[str]


def fetch_pdf_bytes(
    url: str = USDA_DAIRY_PRODUCTS_URL,
) -> tuple[bytes, DairyProductsFingerprint]:
    """Download the PDF and compute its fingerprint.

    Same shape as :func:`cme_spot_call_scraper.fetch_recap_pdf_bytes`
    — kept distinct so the two PDFs can have independent fingerprint
    state files in OneLake.
    """
    resp = requests.get(
        url,
        timeout=_HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": _USER_AGENT},
        allow_redirects=True,
    )
    resp.raise_for_status()
    body = resp.content
    return body, DairyProductsFingerprint(
        etag=resp.headers.get("ETag"),
        last_modified=resp.headers.get("Last-Modified"),
        content_sha256=hashlib.sha256(body).hexdigest(),
    )


# ── Parser result types ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class WeeklyRow:
    """One (week-ending, weighted price) datum for a single product.

    ``revised`` reflects USDA's own asterisk-marker; the UI surfaces it
    as a tooltip but the chart treats revised and non-revised values
    identically.
    """
    week_ending:    datetime
    weighted_price: float
    revised:        bool


@dataclass(frozen=True)
class DairyProductsRecap:
    """Parsed result of a single USDA Dairy Products Sales PDF.

    Attributes
    ----------
    date_range_start, date_range_end
        Span of week-endings covered by the PDF (5 weeks). Pulled from
        the report header.
    rows_by_product
        ``{product → tuple[WeeklyRow, ...]}`` with up to five entries
        per product, sorted oldest-to-newest by ``week_ending``.
    missing_products
        Product names from :data:`_PRODUCT_HEADER_RE` that could NOT
        be located in the PDF text. Empty in steady state.
    """
    date_range_start: datetime
    date_range_end:   datetime
    rows_by_product:  dict[str, tuple[WeeklyRow, ...]]
    missing_products: tuple[str, ...]


def parse_dairy_products_sales(pdf_bytes: bytes) -> DairyProductsRecap:
    """Parse the PDF into a :class:`DairyProductsRecap`.

    Raises ``ValueError`` only when the report-date-range header is
    missing — without that the per-week dates are year-ambiguous.
    Individual missing product blocks are surfaced via
    :attr:`DairyProductsRecap.missing_products` so a partial-coverage
    PDF still yields usable data for the products that DID parse.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)

    # 1. Report date range — anchors the year.
    range_match = _REPORT_DATE_RANGE_RE.search(text)
    if not range_match:
        raise ValueError(
            "Could not locate the report date range (e.g. 'Weeks Ending: "
            "4/11/2026 - 5/9/2026') in the USDA Dairy Products Sales "
            "PDF. The publication layout may have changed."
        )
    try:
        date_range_start = datetime.strptime(range_match.group(1), "%m/%d/%Y")
        date_range_end   = datetime.strptime(range_match.group(2), "%m/%d/%Y")
    except ValueError as exc:
        raise ValueError(
            f"Could not parse report date range "
            f"{range_match.group(0)!r}: {exc}"
        ) from exc

    rows_by_product: dict[str, tuple[WeeklyRow, ...]] = {}
    missing: list[str] = []
    for product, header_re in _PRODUCT_HEADER_RE.items():
        rows = _parse_product_block(
            text, header_re, date_range_start, date_range_end,
        )
        if not rows:
            missing.append(product)
            continue
        rows_by_product[product] = rows

    return DairyProductsRecap(
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        rows_by_product=rows_by_product,
        missing_products=tuple(missing),
    )


def _parse_product_block(
    full_text:        str,
    header_re:        re.Pattern[str],
    range_start:      datetime,
    range_end:        datetime,
) -> tuple[WeeklyRow, ...]:
    """Locate a product block and return its WeeklyRows (or ``()``).

    Strategy:
    1. Find the product header anchor in the full text.
    2. Scan forward within a bounded window (next 600 chars — comfortably
       larger than a 5-row block) for both the "United States <dates>"
       header line and the "Weighted Price <values>" line.
    3. Pair the dates with the values; map "DD-MMM" tokens to full
       :class:`datetime` instances using the PDF's date range to pin
       the year (handling Dec-to-Jan year rollovers transparently).
    """
    header_match = header_re.search(full_text)
    if not header_match:
        return ()
    window = full_text[header_match.end(): header_match.end() + 600]

    date_match  = _WEEKLY_DATE_HEADER_RE.search(window)
    price_match = _WEIGHTED_PRICE_RE.search(window)
    if not date_match or not price_match:
        return ()

    raw_dates  = date_match.group(1).split()       # e.g. ['11-Apr', '18-Apr', ...]
    raw_prices = price_match.group(1).split()      # e.g. ['*1.7767', '1.7494', ...]
    if len(raw_dates) != len(raw_prices):
        return ()

    week_dates = _resolve_year_for_dd_mmm(raw_dates, range_start, range_end)
    if week_dates is None:
        return ()

    rows: list[WeeklyRow] = []
    for week_end, raw_price in zip(week_dates, raw_prices):
        revised = raw_price.startswith("*")
        try:
            value = float(raw_price.lstrip("*"))
        except ValueError:
            # A single un-parseable value shouldn't tank the whole block;
            # we still emit the OTHER weeks. Caller dedupes upstream.
            continue
        rows.append(WeeklyRow(
            week_ending=week_end,
            weighted_price=value,
            revised=revised,
        ))
    return tuple(sorted(rows, key=lambda r: r.week_ending))


# Month-abbreviation → number lookup. Local constant rather than a
# ``time.strptime`` call because strptime is locale-sensitive on
# Windows.
_MONTH_ABBR_TO_INT: dict[str, int] = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _resolve_year_for_dd_mmm(
    raw_dates:    list[str],
    range_start:  datetime,
    range_end:    datetime,
) -> Optional[list[datetime]]:
    """Map ``["11-Apr", "18-Apr", "25-Apr", "2-May", "9-May"]`` to
    a list of full :class:`datetime` instances.

    We assume the 5 week-endings are consecutive Saturdays (USDA's
    convention) and that the END date of the report header is the
    last entry. We walk BACKWARDS from ``range_end`` so a December →
    January year crossover resolves correctly even when the chronology
    flips at index 4.
    """
    if len(raw_dates) != 5:
        return None

    resolved: list[Optional[datetime]] = [None] * 5
    last_year = range_end.year
    expected_end_token = f"{range_end.day}-{range_end.strftime('%b')}"
    if raw_dates[-1] != expected_end_token:
        # USDA layout has changed — bail rather than guess.
        return None
    resolved[-1] = range_end

    # Walk earlier weeks. Each step moves 7 days back; if the resulting
    # (day, month) doesn't match the PDF's token, fall back to a
    # year-aware "best-fit" lookup against the start of the range.
    for idx in range(3, -1, -1):
        candidate = resolved[idx + 1] - _timedelta_days(7)
        token = raw_dates[idx]
        parsed = _try_parse_token_with_year(token, candidate.year)
        if parsed and parsed.day == candidate.day and parsed.month == candidate.month:
            resolved[idx] = parsed
            continue
        # Year-rollover: try the previous calendar year.
        parsed_prev = _try_parse_token_with_year(token, candidate.year - 1)
        if parsed_prev and parsed_prev.day == candidate.day and parsed_prev.month == candidate.month:
            resolved[idx] = parsed_prev
            continue
        # Anything else means the assumed weekly cadence is wrong;
        # bail rather than emit a wrong-year row.
        return None
    return [d for d in resolved if d is not None]


def _try_parse_token_with_year(token: str, year: int) -> Optional[datetime]:
    """Parse ``"11-Apr"`` + ``2026`` → ``datetime(2026, 4, 11)`` or None."""
    try:
        day_str, mon_str = token.split("-", 1)
        day = int(day_str)
        month = _MONTH_ABBR_TO_INT.get(mon_str)
        if month is None:
            return None
        return datetime(year, month, day)
    except (ValueError, IndexError):
        return None


def _timedelta_days(days: int):
    """Tiny helper — same as in cme_spot_call_scraper to avoid a stray import."""
    from datetime import timedelta
    return timedelta(days=days)


__all__ = [
    "USDA_DAIRY_PRODUCTS_URL",
    "PRODUCT_BUTTER",
    "PRODUCT_CHEDDAR_CHEESE",
    "PRODUCT_DRY_WHEY",
    "PRODUCT_NONFAT_DRY_MILK",
    "USER_FACING_PRODUCTS",
    "DairyProductsFingerprint",
    "DairyProductsRecap",
    "WeeklyRow",
    "fetch_pdf_bytes",
    "parse_dairy_products_sales",
]
