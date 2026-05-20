"""
USDA milk-price PDF helpers.

Two responsibilities live here, kept side-by-side because they share the same
HTTP I/O and ``pdfplumber`` text-extraction primitives:

1. **Change detection** — has the source PDF changed since we last looked?
   We use HTTP ``HEAD`` first (free, returns ``ETag`` + ``Last-Modified``
   when the CDN provides them) and fall back to a content SHA-256 over a
   ``GET`` body when the headers are absent or inconclusive.

2. **Parsing** — extract advance-price data from ``dymadvancedprices.pdf``
   and Class II Butterfat figures from page 2 of ``dymclassprices.pdf``.

   Two parser tiers are exposed:

   * **Headline parsers** (``parse_advanced_prices``) — pull the small
     "ADVANCED PRICES FOR <MONTH YYYY>" summary block at the top of
     page 1.  Used for the Class I ESL Adjustment, which is announced
     only for the upcoming month and is *not* republished in the
     per-year history.
   * **History parsers** (``parse_advanced_prices_history``,
     ``parse_class_ii_butterfat_history``, ``parse_announced_month``) —
     parse the per-year monthly tables.  These are the AUTHORITATIVE
     source for any monthly reconciliation: the headline is just the
     newest row of the table, rendered for emphasis, while the table
     itself carries the explicit ``(year, month)`` label and every
     value USDA has ever published.  Driving the orchestrator off the
     history table eliminates the entire class of "PDF page-1 lags the
     candidate month" mis-labelling bugs.

Why pdfplumber and not pypdf2/pdfminer/AI?
    The USDA PDFs are text-native (verified empirically — see the parse
    fixture in tests/), each label is unique, and pdfplumber returns the
    extracted text in a deterministic per-page order. AI parsing was ruled
    out for the default path so we don't introduce a paid API key.
    Layout drift is mitigated by precise label regex + a unit test on a
    captured fixture; on a parse failure ``maybe_update_from_pdfs`` surfaces
    a banner instead of silently corrupting the DB.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from typing import Optional

import pdfplumber
import requests


# Public URLs of the source PDFs. Centralised so callers can mock them in tests
# and so we don't repeat string literals across modules.
ADVANCED_PRICES_URL: str = "https://www.ams.usda.gov/mnreports/dymadvancedprices.pdf"
CLASS_PRICES_URL:    str = "https://www.ams.usda.gov/mnreports/dymclassprices.pdf"

# Conservative timeout — the USDA CDN normally responds in <1s, but we don't
# want a hung HEAD to block a Streamlit render forever.
_HTTP_TIMEOUT_SECONDS: float = 15.0

# A polite, identifying User-Agent. Some CDNs reject default urllib UAs.
_USER_AGENT: str = "DarigoldPricingAssistant/1.0 (milk-mover auto-update)"


# ── Change detection ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Fingerprint:
    """HTTP-level fingerprint we persist to detect future changes."""
    etag:           Optional[str]
    last_modified:  Optional[str]
    content_sha256: Optional[str]


def _head(url: str) -> _Fingerprint:
    """Issue a HEAD and return only the change-detection headers we care about."""
    resp = requests.head(
        url,
        timeout=_HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": _USER_AGENT},
        allow_redirects=True,
    )
    resp.raise_for_status()
    return _Fingerprint(
        etag=resp.headers.get("ETag"),
        last_modified=resp.headers.get("Last-Modified"),
        content_sha256=None,
    )


def fetch_pdf_bytes(url: str) -> tuple[bytes, _Fingerprint]:
    """Download the PDF and compute a content fingerprint in one pass.

    Returns ``(body_bytes, fingerprint)`` where the fingerprint includes the
    response's ``ETag`` / ``Last-Modified`` headers (when present) plus a
    SHA-256 of the body. The SHA serves as the durable signal when the CDN
    omits the other two — without it we'd never detect changes.
    """
    resp = requests.get(
        url,
        timeout=_HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": _USER_AGENT},
        allow_redirects=True,
    )
    resp.raise_for_status()
    body = resp.content
    return body, _Fingerprint(
        etag=resp.headers.get("ETag"),
        last_modified=resp.headers.get("Last-Modified"),
        content_sha256=hashlib.sha256(body).hexdigest(),
    )


def has_pdf_changed(url: str, previous: Optional[dict]) -> tuple[bool, _Fingerprint]:
    """Return ``(changed, fingerprint)`` for ``url`` against the cached state.

    Strategy
    --------
    * If we have a previous ``ETag`` / ``Last-Modified``, do a cheap HEAD and
      compare. Identical → not changed. Different → changed.
    * If the HEAD lacks both headers, OR if we have no previous fingerprint,
      we GET the body and compare SHA-256s.

    The returned fingerprint always reflects the *latest* observation so
    callers can persist it via ``upsert_pdf_state``.
    """
    if previous and (previous.get("etag") or previous.get("last_modified")):
        try:
            head_fp = _head(url)
        except requests.RequestException:
            head_fp = _Fingerprint(None, None, None)
        if head_fp.etag and previous.get("etag"):
            return head_fp.etag != previous["etag"], head_fp
        if head_fp.last_modified and previous.get("last_modified"):
            return head_fp.last_modified != previous["last_modified"], head_fp

    # Fall back to body hash. This always pulls the bytes, so callers that
    # care about minimising bandwidth should rely on the HEAD path above.
    _, body_fp = fetch_pdf_bytes(url)
    prev_sha = (previous or {}).get("content_sha256")
    return prev_sha != body_fp.content_sha256, body_fp


# ── Advanced Prices parser (dymadvancedprices.pdf) ──────────────────────────

# Three-letter month abbreviation → 1-based month number.  Used by every
# history parser in this module so the per-month dict shape stays consistent
# across parsers (advance-prices history + class-prices history) and there
# is exactly one canonical mapping to maintain.
_MONTH_NAME_TO_INT: dict[str, int] = {
    "Jan": 1, "Feb": 2,  "Mar": 3,  "Apr": 4,  "May": 5,  "Jun": 6,
    "Jul": 7, "Aug": 8,  "Sep": 9,  "Oct": 10, "Nov": 11, "Dec": 12,
}

# Each pattern targets a unique label that appears on page 1 of the PDF. The
# ``[^\d\$\(\-]*`` filler tolerates the footnote-marker characters
# ("²", "1", a literal superscript byte that pdfplumber renders as the
# Unicode replacement char, etc.) and the trailing colon. Numbers can be
# wrapped in parentheses to indicate a negative value (e.g. the ESL
# adjustment: ``($0.49)``).
_VALUE_PATTERN = r"\(?\$?(-?\d+(?:\.\d+)?)\)?"
_LABEL_PATTERNS: dict[str, re.Pattern[str]] = {
    "class_i_skim_raw":     re.compile(
        r"Base Skim Milk Price for Class I[^:\n]*:\s*" + _VALUE_PATTERN
    ),
    "advanced_butterfat":   re.compile(
        r"Advanced Butterfat Pricing Factor[^:\n]*:\s*" + _VALUE_PATTERN
    ),
    # Negative lookbehind so we don't match "Advanced Class III Skim Milk ..."
    # or any other "Advanced Class X" prefix.
    "class_ii_skim_raw":    re.compile(
        r"(?<!Advanced )Class II Skim Milk Price[^:\n]*:\s*" + _VALUE_PATTERN
    ),
    "class_i_esl_adj_raw":  re.compile(
        r"Class I ESL Adjustment[^:\n]*:\s*" + _VALUE_PATTERN
    ),
    # Class II Nonfat Solids Price ($/lb).  Source for the headline (current
    # advance month).  For every OTHER month this value comes from the
    # per-year history table — see :func:`parse_advanced_prices_history`.
    "class_ii_nonfat_solids": re.compile(
        r"Class II Nonfat Solids Price[^:\n]*:\s*" + _VALUE_PATTERN
    ),
}


def _is_negative(text: str, match: re.Match[str]) -> bool:
    """Detect the ``($0.49)`` paren-wrapped negative convention USDA uses."""
    span = text[max(0, match.start() - 1): match.end() + 1]
    return "(" in span and ")" in span


def parse_advanced_prices(pdf_bytes: bytes) -> dict[str, float]:
    """Parse the five HEADLINE values from ``dymadvancedprices.pdf``.

    These are the values printed in the "ADVANCED PRICES FOR <MONTH YYYY>"
    summary block at the top of page 1 — i.e. the announcement for the
    UPCOMING month only.  Every other month's value is published in the
    per-year history table further down on page 1 and should be read via
    :func:`parse_advanced_prices_history`.

    The only field returned here that does NOT appear in the history
    table is ``class_i_esl_adj_raw`` (the Class I ESL Adjustment, which
    USDA announces month-by-month and never republishes).  The other
    four headline values duplicate the latest row of the history table
    and are returned to support legacy cross-checks; new code should
    prefer the history parser for any non-announced month.

    Returns a dict with keys:

    * ``class_i_skim_raw``       — "Base Skim Milk Price for Class I" ($/cwt)
    * ``advanced_butterfat``     — "Advanced Butterfat Pricing Factor" ($/lb)
    * ``class_ii_skim_raw``      — "Class II Skim Milk Price" ($/cwt)
    * ``class_i_esl_adj_raw``    — "Class I ESL Adjustment" ($/cwt, signed)
    * ``class_ii_nonfat_solids`` — "Class II Nonfat Solids Price" ($/lb).
                                    Doubles as the Cottage Cheese
                                    Protein Rate AND Other Solids Rate.

    Raises ``ValueError`` listing every missing label so the orchestrator can
    surface a single actionable error message instead of failing silently.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # Page 1 carries every label we care about; reading all pages costs
        # only a few ms but makes the regex resilient if USDA ever shifts the
        # label across a page break.
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)

    out: dict[str, float] = {}
    missing: list[str] = []
    for key, pattern in _LABEL_PATTERNS.items():
        m = pattern.search(text)
        if not m:
            missing.append(key)
            continue
        value = float(m.group(1))
        if key == "class_i_esl_adj_raw" and _is_negative(text, m):
            value = -abs(value)
        out[key] = value

    if missing:
        raise ValueError(
            "Could not locate the following labels in dymadvancedprices.pdf: "
            + ", ".join(missing)
            + ". The USDA PDF layout may have changed."
        )
    return out


# ── Advanced Prices page-1 monthly history (per-year tables) ────────────────
#
# The page-1 monthly tables on ``dymadvancedprices.pdf`` are titled
# "Federal Milk Order Class I and Class II Advanced Prices and Pricing
# Factors, <year>" — one per year (current + prior calendar year). Each
# data row follows the same fixed column layout:
#
#     <MonAbbr> <Class I Base> <Base Skim Class I> <Adv Class III Skim>
#               <Adv Class IV Skim> <Adv Butterfat> <Class II Skim>
#               <Class II Nonfat Solids>
#
# pdfplumber's text extraction collapses each row into a single line with
# the 7 numbers in column order. We anchor on the year header (so unrelated
# numeric tables on the page can't false-match) and capture every value we
# need from each month row. The "Avg" row is intentionally NOT included
# because it doesn't map onto a (year, month) key.

_ADV_HISTORY_ROW_RE = re.compile(
    r"^\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(-?\d+\.\d+)\s+"   # group 2: Class I Base Price ($/cwt) — unused (derived)
    r"(-?\d+\.\d+)\s+"   # group 3: Base Skim Milk Price for Class I ($/cwt)
    r"(-?\d+\.\d+)\s+"   # group 4: Advanced Class III Skim Milk Pricing Factor — unused
    r"(-?\d+\.\d+)\s+"   # group 5: Advanced Class IV  Skim Milk Pricing Factor — unused
    r"(-?\d+\.\d+)\s+"   # group 6: Advanced Butterfat Pricing Factor ($/lb)
    r"(-?\d+\.\d+)\s+"   # group 7: Class II Skim Milk Price ($/cwt)
    r"(-?\d+\.\d+)\s*$", # group 8: Class II Nonfat Solids Price ($/lb)
    re.MULTILINE,
)

# Header anchor for the per-year Advanced-Prices table. The wording is
# "Federal Milk Order Class I and Class II Advanced Prices and Pricing
# Factors, <year>". We accept any 4-digit year and segment the page-1
# text into per-year blocks using the next-header position as a bound.
_ADV_YEAR_HEADER_RE = re.compile(
    r"Federal Milk Order Class I[^,]*and\s*Class II[^,]*Advanced Prices "
    r"and Pricing Factors[^,]*,\s*(\d{4})"
)


@dataclass(frozen=True)
class AdvancedPriceSample:
    """One month's reconcilable advance-price values from the page-1 history.

    Carries the four advance-price values USDA publishes per month, in the
    raw form that appears on the PDF (no rescaling).  Callers that need
    Skim-per-pound divide ``class_*_skim_raw`` by 100 explicitly — that
    rescaling is left to the orchestrator so the parser stays a pure
    extraction layer with no derived semantics.
    """
    year:                   int
    month:                  int
    class_i_skim_raw:       float  # Base Skim Milk Price for Class I  ($/cwt)
    advanced_butterfat:     float  # Advanced Butterfat Pricing Factor ($/lb)
    class_ii_skim_raw:      float  # Class II Skim Milk Price          ($/cwt)
    class_ii_nonfat_solids: float  # Class II Nonfat Solids Price      ($/lb)


def parse_advanced_prices_history(
    pdf_bytes: bytes,
) -> dict[tuple[int, int], AdvancedPriceSample]:
    """Return ``{(year, month) → AdvancedPriceSample}`` from page-1 history.

    The per-year monthly tables on page 1 of ``dymadvancedprices.pdf`` are
    the AUTHORITATIVE source for any monthly reconciliation.  Each row is
    explicitly labelled with its month so the orchestrator can never
    mis-label a value (unlike the page-1 headline, which references only
    the upcoming month).

    Coverage is the current calendar year (months published so far) and
    the previous full calendar year — typically ~24 rows.  Months older
    than the PDF's history window are simply absent from the returned
    dict; callers handle the missing-key case themselves.

    Never raises on layout drift — returns whatever rows it could parse.
    The orchestrator's "skip when empty" guard then falls back to the
    last good state in the store.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # Year tables live on page 1; reading all pages is cheap and
        # keeps us resilient against future paginations of the same
        # report layout.
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)

    headers = list(_ADV_YEAR_HEADER_RE.finditer(text))
    if not headers:
        return {}

    out: dict[tuple[int, int], AdvancedPriceSample] = {}
    for i, m in enumerate(headers):
        block_year = int(m.group(1))
        start = m.end()
        end   = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[start:end]
        for row in _ADV_HISTORY_ROW_RE.finditer(block):
            month_int = _MONTH_NAME_TO_INT.get(row.group(1))
            if month_int is None:
                continue
            try:
                out[(block_year, month_int)] = AdvancedPriceSample(
                    year=block_year,
                    month=month_int,
                    class_i_skim_raw       = float(row.group(3)),
                    advanced_butterfat     = float(row.group(6)),
                    class_ii_skim_raw      = float(row.group(7)),
                    class_ii_nonfat_solids = float(row.group(8)),
                )
            except (TypeError, ValueError):
                # Unparseable cell — silently skip rather than poison
                # the dict; the caller handles missing keys.
                continue
    return out


def parse_announced_month(pdf_bytes: bytes) -> Optional[tuple[int, int]]:
    """Return ``(year, month)`` of the upcoming-month advance USDA just published.

    Derived from the LATEST row present in the page-1 history table —
    that row IS the announcement.  This is more reliable than text-mining
    the "ADVANCED PRICES FOR …" headline, which has variable casing and
    occasional layout drift across revisions and would silently mis-match
    on a single PDF re-issue.

    Returns ``None`` when the page-1 history table is empty or
    unparseable — at which point the orchestrator skips any new write
    rather than guessing a candidate month.  This is the structural fix
    for the historical "PDF page-1 lags ``_next_month(latest)``" bug
    where stale headline values were written under the wrong month.
    """
    history = parse_advanced_prices_history(pdf_bytes)
    if not history:
        return None
    return max(history.keys())


# ── Class Prices parser (dymclassprices.pdf, page 2) ────────────────────────

# The page-2 year tables don't have visible borders so pdfplumber's table
# extractor returns empty rows. The text layout is, however, perfectly
# predictable: each data row is "<MonAbbr> <ClassII> <ClassIIBfat> <ClassIII>
# <ClassIIISkim> <ClassIV> <ClassIVSkim>". We anchor on the year header and
# capture the next 12 (or fewer for partial years) such rows.
_CLASS_PRICES_ROW_RE = re.compile(
    r"^\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(-?\d+\.\d+)\s+"   # group 2: Class II        ($/cwt) — unused (Skim used instead)
    r"(-?\d+\.\d+)\s+"   # group 3: Class II Bfat   ($/lb)
    r"(-?\d+\.\d+)\s+"   # group 4: Class III       — unused
    r"(-?\d+\.\d+)\s+"   # group 5: Class III Skim  — unused
    r"(-?\d+\.\d+)\s+"   # group 6: Class IV        — unused
    r"(-?\d+\.\d+)\s*$", # group 7: Class IV Skim   — unused
    re.MULTILINE,
)

# Header anchor: e.g. "Federal Milk Order Class II, Class III, and Class IV
# Milk Prices, 2026". We accept any 4-digit year and use the captured one to
# segment the page text into per-year sections.
_CLASS_YEAR_HEADER_RE = re.compile(
    r"Federal Milk Order Class II[^,]*,\s*Class III[^,]*,\s*and\s*Class IV[^,]*,\s*(\d{4})"
)


def parse_class_ii_butterfat_history(
    pdf_bytes: bytes,
) -> dict[tuple[int, int], float]:
    """Return ``{(year, month) → Class II Butterfat Price}`` for every month.

    Parses every monthly row from the per-year tables on page 2 of
    ``dymclassprices.pdf``.  The class-prices PDF publishes ~1 month
    behind the advance-prices PDF (USDA cadence), so the latest one or
    two months in the advance-prices history may not yet have a Class
    II Butterfat row available — those keys are simply absent from the
    returned dict and the orchestrator preserves the stored value for
    those months.

    Never raises on layout drift — returns whatever rows it could
    parse.  Used by the reconciliation pass to drive HTST II / ESL II /
    CC II Butterfat across every month USDA exposes, replacing the
    previous one-month-at-a-time lookup whose fallback path masked
    parser errors.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if len(pdf.pages) < 2:
            return {}
        page2_text = pdf.pages[1].extract_text() or ""

    headers = list(_CLASS_YEAR_HEADER_RE.finditer(page2_text))
    if not headers:
        return {}

    out: dict[tuple[int, int], float] = {}
    for i, m in enumerate(headers):
        block_year = int(m.group(1))
        start = m.end()
        end   = headers[i + 1].start() if i + 1 < len(headers) else len(page2_text)
        block = page2_text[start:end]
        for row in _CLASS_PRICES_ROW_RE.finditer(block):
            month_int = _MONTH_NAME_TO_INT.get(row.group(1))
            if month_int is None:
                continue
            try:
                # Group 3 is the Class II Butterfat column — see
                # _CLASS_PRICES_ROW_RE comments for the column order.
                out[(block_year, month_int)] = float(row.group(3))
            except (TypeError, ValueError):
                continue
    return out
