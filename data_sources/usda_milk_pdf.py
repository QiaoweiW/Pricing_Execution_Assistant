"""
USDA milk-price PDF helpers.

Two responsibilities live here, kept side-by-side because they share the same
HTTP I/O and ``pdfplumber`` text-extraction primitives:

1. **Change detection** — has the source PDF changed since we last looked?
   We use HTTP ``HEAD`` first (free, returns ``ETag`` + ``Last-Modified``
   when the CDN provides them) and fall back to a content SHA-256 over a
   ``GET`` body when the headers are absent or inconclusive.

2. **Parsing** — extract the four advanced-prices fields from
   ``dymadvancedprices.pdf`` and the Class II Butterfat figure from page 2 of
   ``dymclassprices.pdf``.

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
}


def _is_negative(text: str, match: re.Match[str]) -> bool:
    """Detect the ``($0.49)`` paren-wrapped negative convention USDA uses."""
    span = text[max(0, match.start() - 1): match.end() + 1]
    return "(" in span and ")" in span


def parse_advanced_prices(pdf_bytes: bytes) -> dict[str, float]:
    """Parse the four headline values from ``dymadvancedprices.pdf``.

    Returns a dict with keys:

    * ``class_i_skim_raw``      — "Base Skim Milk Price for Class I" ($/cwt)
    * ``advanced_butterfat``    — "Advanced Butterfat Pricing Factor" ($/lb)
    * ``class_ii_skim_raw``     — "Class II Skim Milk Price" ($/cwt)
    * ``class_i_esl_adj_raw``   — "Class I ESL Adjustment" ($/cwt, signed)

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


# ── Class Prices parser (dymclassprices.pdf, page 2) ────────────────────────

# The page-2 year tables don't have visible borders so pdfplumber's table
# extractor returns empty rows. The text layout is, however, perfectly
# predictable: each data row is "<MonAbbr> <ClassII> <ClassIIBfat> <ClassIII>
# <ClassIIISkim> <ClassIV> <ClassIVSkim>". We anchor on the year header and
# capture the next 12 (or fewer for partial years) such rows.
_MONTH_ROW_RE = re.compile(
    r"^\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+"
    r"(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s*$",
    re.MULTILINE,
)

# Header anchor: e.g. "Federal Milk Order Class II, Class III, and Class IV
# Milk Prices, 2026". We accept any 4-digit year and use the captured one to
# segment the page text into per-year sections.
_YEAR_HEADER_RE = re.compile(
    r"Federal Milk Order Class II[^,]*,\s*Class III[^,]*,\s*and\s*Class IV[^,]*,\s*(\d{4})"
)

# pdfplumber's per-page text uses the 3-letter month abbreviations above.
_MONTH_INDEX: dict[int, str] = {
    1:  "Jan", 2:  "Feb", 3:  "Mar", 4:  "Apr", 5:  "May", 6:  "Jun",
    7:  "Jul", 8:  "Aug", 9:  "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


@dataclass(frozen=True)
class ClassIIBfatLookup:
    """Result of a Class II Butterfat lookup, useful for status banners."""
    year:  int
    month: int
    value: float


def parse_class_ii_butterfat(
    pdf_bytes: bytes,
    *,
    target_year:  int,
    target_month: int,
) -> ClassIIBfatLookup:
    """Return the Class II Butterfat price for ``(target_year, target_month)``.

    Looks up the value on page 2 of ``dymclassprices.pdf``, which lists the
    six Class II/III/IV columns by month for each year (current year on top,
    prior year below).

    Raises ``ValueError`` when either the year section or the requested
    month's row is absent — typically because the requested month hasn't been
    published yet (USDA classprices are published ~end of the following
    month).
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if len(pdf.pages) < 2:
            raise ValueError(
                "dymclassprices.pdf is shorter than expected (no page 2)."
            )
        page2_text = pdf.pages[1].extract_text() or ""

    # Segment the page into (year → block) chunks using the year-header anchors.
    # We take everything between this anchor and the next anchor as the
    # year's data, which is robust to additional blank lines USDA sometimes
    # injects between tables.
    headers = list(_YEAR_HEADER_RE.finditer(page2_text))
    if not headers:
        raise ValueError(
            "Could not locate any year header on page 2 of dymclassprices.pdf."
        )

    year_blocks: dict[int, str] = {}
    for i, m in enumerate(headers):
        block_year = int(m.group(1))
        start = m.end()
        end   = headers[i + 1].start() if i + 1 < len(headers) else len(page2_text)
        year_blocks[block_year] = page2_text[start:end]

    if target_year not in year_blocks:
        raise ValueError(
            f"Year {target_year} table not found on page 2 of dymclassprices.pdf. "
            f"Available years: {sorted(year_blocks)}."
        )

    target_month_abbr = _MONTH_INDEX[target_month]
    block = year_blocks[target_year]
    for row in _MONTH_ROW_RE.finditer(block):
        if row.group(1) != target_month_abbr:
            continue
        # Group order matches the column order — see _MONTH_ROW_RE definition.
        # Class II Butterfat is the 2nd captured number after the month label.
        return ClassIIBfatLookup(
            year=target_year,
            month=target_month,
            value=float(row.group(3)),
        )

    raise ValueError(
        f"{target_month_abbr} {target_year} row not present in the year-{target_year} "
        f"Class II/III/IV table yet — USDA may not have published it. "
        f"Available rows: {[m.group(1) for m in _MONTH_ROW_RE.finditer(block)]}."
    )
