"""
Distribute Price Book — pure logic, lookups, and email transport.

Lives next to the other ``data_sources`` modules because every public
function in here is a data-shape transform (parquet read, cascading
filter, VBCS lookup) or a transport primitive (SMTP send).  The
Streamlit UI lives in ``pages/pricing_execution_automation_view.py``
and only ever calls into here — no Streamlit primitives leak into
this module so the logic remains unit-testable from a notebook or a
script.

Data sources
------------
1.  ``Files/FG_Pricing_History/B2C_Pricing_History.parquet``
    The history of B2C pricing events.  Drives the **rows** of the
    Price Book: the cascading filter UI narrows this frame down to a
    subset, and every surviving row becomes one output row.

2.  ``Files/Monthly_Pricing_Execution/VBCS_refrehable/*.csv``
    The latest published VBCS files (one per tool / customer group),
    refreshed automatically every time a "Run … Generation" succeeds
    on the Pricing Execution Automation page.  Drives the **New
    Price** column of the Price Book: for every output row we scan
    every VBCS file for a row matching ``(item, ship-to, UOM, +1
    calendar month)`` and copy its ``Adjustmentamount`` in.

Output schema
-------------
Eight columns, in this order:

* ``Darigold Item Number``
* ``Item Description``
* ``Pricing UOM``
* ``Old Price``
* ``New Price``
* ``Price Change``  (``New Price - Old Price``, blank when New is blank)
* ``Price Start Date``
* ``Price End Date``

Why a separate module?
----------------------
Concentrating every transform here keeps the page module thin (UI
plumbing only) and makes the contract testable without spinning up
Streamlit.  The parquet read, the cascade math, the VBCS join, and
the SMTP send are independent enough that an integration bug in any
one of them shouldn't bring the others down — each public function
returns plain Python / pandas values the next stage can compose.

Email transport
---------------
Reuses the SMTP credentials already configured under
``[task_manager_email]`` in ``.streamlit/secrets.toml`` (the same
section ``utils.notification_helpers`` uses for task reminders).
We deliberately don't add a new secrets section — keeping a single
SMTP identity reduces the chance of password drift between sections.
A future "Distribute Price Book from a different sender" requirement
should add a ``[price_book_email]`` section to ``secrets.toml`` and
gate this module on its presence; today's footprint is small enough
not to justify the extra config surface.
"""
from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Iterable, Optional

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io
from data_sources import vbcs_refrehable_store as _vbcs_store


logger = logging.getLogger(__name__)


# ── Errors ───────────────────────────────────────────────────────────────────


class PriceBookError(RuntimeError):
    """Raised on any data-load, lookup, or transport failure."""


# ── Lakehouse coordinates ────────────────────────────────────────────────────

# Dedicated secrets section so a deployment can route the FG Pricing
# History parquet at a different lakehouse if it ever needs to (e.g. a
# separate "B2C" lakehouse).  Falls back to ``[fabric_htst]`` via
# ``fabric_lakehouse_io._read_lakehouse_config`` when the block is
# absent, so today's single-lakehouse setup needs zero extra config.
_PARQUET_SECRETS_SECTION: str = "fabric_fg_pricing_history"
_PARQUET_BLOB_PATH:       str = "FG_Pricing_History/B2C_Pricing_History.parquet"

# Read cache TTL (in seconds).  The parquet is small enough (~tens of
# MB at worst) to round-trip in <1 s, but a Streamlit selectbox interaction
# fires a full rerun, so we cache for 5 minutes to keep the cascading
# typing feel snappy.  Operators clicking "Reload from lakehouse" call
# ``invalidate_parquet_cache`` to bypass the cache.
_PARQUET_READ_TTL_SECONDS: int = 300


# ── Parquet schema constants ─────────────────────────────────────────────────
#
# Centralised here so a future column-rename in the upstream pipeline
# only needs to update these literals.  Comparison sites strip + match
# case-insensitively so a stray ``"item"`` vs ``"Item"`` drift would
# still surface a precise error message (see :func:`_resolve_column`).

PARQUET_COL_ITEM:           str = "Item"
PARQUET_COL_ITEM_DESC:      str = "Item Description"
PARQUET_COL_SHIP_TO:        str = "Ship to Site Name"
PARQUET_COL_UOM:            str = "Pricing UOM"
PARQUET_COL_OLD_PRICE:      str = "Total Price per Pricing UOM"
PARQUET_COL_START_DATE:     str = "Pricing Adjustment Start Date"
PARQUET_COL_END_DATE:       str = "Pricing Adjustment End Date"
PARQUET_COL_ITEM_CATEGORY:  str = "Item Category"
PARQUET_COL_CUSTOMER:       str = "Customer"

# Cascading filter order (left → right).  Each step narrows the
# parquet to the rows that survive the upstream selections; the
# downstream selectbox then offers only the distinct values present
# in the surviving slice.
CASCADING_FILTERS: tuple[str, ...] = (
    PARQUET_COL_ITEM_CATEGORY,
    PARQUET_COL_CUSTOMER,
    PARQUET_COL_SHIP_TO,
    PARQUET_COL_START_DATE,
    PARQUET_COL_UOM,
)


# ── VBCS schema constants ────────────────────────────────────────────────────

VBCS_COL_ITEM:        str = "item_name"
VBCS_COL_UOM:         str = "pricinguom"
VBCS_COL_SHIP_TO:     str = "Shiptoname"
VBCS_COL_AMOUNT:      str = "Adjustmentamount"
VBCS_COL_START_DATE:  str = "Adjustmentstartdate"


# ── Output schema constants ──────────────────────────────────────────────────

OUT_COL_ITEM:    str = "Darigold Item Number"
OUT_COL_DESC:    str = "Item Description"
OUT_COL_UOM:     str = "Pricing UOM"
OUT_COL_OLD:     str = "Old Price"
OUT_COL_NEW:     str = "New Price"
OUT_COL_CHANGE:  str = "Price Change"
OUT_COL_START:   str = "Price Start Date"
OUT_COL_END:     str = "Price End Date"

OUTPUT_COLUMNS: tuple[str, ...] = (
    OUT_COL_ITEM, OUT_COL_DESC, OUT_COL_UOM,
    OUT_COL_OLD, OUT_COL_NEW, OUT_COL_CHANGE,
    OUT_COL_START, OUT_COL_END,
)


# ── Parquet loader ───────────────────────────────────────────────────────────


@st.cache_data(ttl=_PARQUET_READ_TTL_SECONDS, show_spinner=False)
def _load_b2c_pricing_history_cached() -> pd.DataFrame:
    """Cached load of the B2C Pricing History parquet.

    Returns an empty DataFrame when the blob is absent (cold-bootstrap
    deployments) so the page can surface a clear "Upload parquet"
    message instead of crashing.
    """
    try:
        df, _etag = _io.read_parquet(_PARQUET_SECRETS_SECTION, _PARQUET_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise PriceBookError(
            f"Could not read OneLake blob 'Files/{_PARQUET_BLOB_PATH}': {exc}. "
            "Verify the parquet exists and that the lakehouse "
            "credentials in .streamlit/secrets.toml are valid."
        ) from exc
    if df is None:
        return pd.DataFrame()
    # Always strip column whitespace once at load time — protects every
    # downstream lookup from a stray trailing space in the parquet header.
    df = df.rename(columns=lambda c: c.strip() if isinstance(c, str) else c)
    return df


def load_b2c_pricing_history() -> pd.DataFrame:
    """Return the B2C Pricing History DataFrame (cached, whitespace-stripped)."""
    return _load_b2c_pricing_history_cached()


def invalidate_parquet_cache() -> None:
    """Drop the parquet read cache so the next call re-reads from OneLake.

    Wired to an explicit "Reload from lakehouse" button in the page so
    operators can force a refresh after an upstream pipeline publish.
    """
    _load_b2c_pricing_history_cached.clear()


# ── Column resolution ────────────────────────────────────────────────────────


def _resolve_column(df: pd.DataFrame, expected: str) -> str:
    """Return the actual column name in ``df`` matching ``expected``.

    Matching is case-insensitive + whitespace-tolerant so a stray
    ``"item"`` / ``" Item "`` in the upstream parquet still resolves
    cleanly.  Raises :class:`PriceBookError` listing every column the
    parquet *does* expose so the operator knows exactly what's
    available without spelunking in OneLake explorer.
    """
    if df.empty:
        raise PriceBookError(
            f"The B2C Pricing History parquet is empty — cannot resolve column "
            f"{expected!r}.  Verify the upstream pipeline has published rows."
        )
    norm = {c.strip().casefold(): c for c in df.columns}
    hit = norm.get(expected.strip().casefold())
    if hit is None:
        raise PriceBookError(
            f"Required column {expected!r} not found in the B2C Pricing "
            f"History parquet.  Available columns: {sorted(df.columns)!r}"
        )
    return hit


def validate_parquet_schema(df: pd.DataFrame) -> dict[str, str]:
    """Return ``{logical_name: actual_column_name}`` for every required field.

    Verifies the parquet exposes every column the cascading filters and
    the row-mapping code below depend on.  Returns the resolved-name
    map for downstream callers (so they don't have to ``_resolve_column``
    on every row).  Raises a single aggregated error when any column
    is missing.
    """
    required_logical = (
        PARQUET_COL_ITEM,
        PARQUET_COL_ITEM_DESC,
        PARQUET_COL_SHIP_TO,
        PARQUET_COL_UOM,
        PARQUET_COL_OLD_PRICE,
        PARQUET_COL_START_DATE,
        PARQUET_COL_END_DATE,
        PARQUET_COL_ITEM_CATEGORY,
        PARQUET_COL_CUSTOMER,
    )
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for logical in required_logical:
        try:
            resolved[logical] = _resolve_column(df, logical)
        except PriceBookError:
            missing.append(logical)
    if missing:
        raise PriceBookError(
            "B2C Pricing History parquet is missing required column(s): "
            f"{missing!r}.  Available columns: {sorted(df.columns)!r}.  "
            "Fix the upstream pipeline or contact the data steward."
        )
    return resolved


# ── Cascading filter helpers ─────────────────────────────────────────────────


def apply_filters(
    df: pd.DataFrame,
    selections: dict[str, list[Any]],
    column_map: dict[str, str],
) -> pd.DataFrame:
    """Return the rows of ``df`` that survive every non-empty selection.

    ``selections`` maps logical filter names (from
    :data:`CASCADING_FILTERS`) to the user's multi-select picks.  An
    empty list means "no filter on that axis".

    Comparison policy:
      * Date columns (Price Adjustment Start / End Date) compare on
        the normalised first-of-the-second YYYY-MM-DD form so a
        ``Timestamp("2026-05-01")`` pick matches a parquet cell of
        ``2026-05-01`` regardless of whether the column stores
        date-only or full datetime values.
      * Every other column compares on whitespace-trimmed str
        equality — the dropdown labels are the source of truth and
        the values flow through unchanged.
    """
    if df.empty:
        return df
    out = df
    for logical in CASCADING_FILTERS:
        picks = selections.get(logical) or []
        if not picks:
            continue
        actual_col = column_map.get(logical)
        if actual_col is None:
            continue
        if logical in (PARQUET_COL_START_DATE, PARQUET_COL_END_DATE):
            # Normalise both sides to YYYY-MM-DD so date-only and full
            # datetime representations compare equal.
            col_norm = pd.to_datetime(out[actual_col], errors="coerce").dt.normalize()
            pick_keys = {
                pd.Timestamp(p).normalize() for p in picks
            }
            out = out[col_norm.isin(pick_keys)]
        else:
            out = out[
                out[actual_col].astype(str).str.strip().isin([str(p).strip() for p in picks])
            ]
        if out.empty:
            break
    return out


def distinct_options(
    df: pd.DataFrame,
    logical_col: str,
    column_map: dict[str, str],
) -> list[Any]:
    """Return sorted distinct values of ``logical_col`` in ``df``.

    Used by the cascading dropdowns to compute their option lists.
    Dates are returned as ``pd.Timestamp`` so the page can format them
    consistently (``YYYY-MM-DD``).  All other types are coerced to
    their natural Python type via pandas's ``.unique()``.

    Returns ``[]`` when the column is missing or the frame is empty —
    the page renders an empty dropdown rather than crashing.
    """
    actual_col = column_map.get(logical_col)
    if actual_col is None or df.empty or actual_col not in df.columns:
        return []
    series = df[actual_col]
    # For date-shaped columns coerce to Timestamp so options sort
    # chronologically AND the page can format them as YYYY-MM-DD.
    if logical_col in (PARQUET_COL_START_DATE, PARQUET_COL_END_DATE):
        coerced = pd.to_datetime(series, errors="coerce").dropna()
        return sorted(coerced.dt.normalize().unique().tolist())
    # Drop NaN so the dropdown doesn't expose a "nan" sentinel.
    cleaned = series.dropna()
    try:
        return sorted(cleaned.unique().tolist())
    except TypeError:
        # Mixed types — fall back to string-sorted unique values.
        return sorted({str(v) for v in cleaned.unique().tolist()})


# ── New-Price lookup against VBCS_refrehable/ ────────────────────────────────


@dataclass(frozen=True)
class _VbcsLookupKey:
    """Composite key for the VBCS-side join."""

    item: str
    uom:  str
    site: str


@dataclass(frozen=True)
class _VbcsLookupHit:
    """One match returned by the VBCS scan."""

    amount: float
    source_file: str


@dataclass
class NewPriceConflict:
    """Two or more VBCS files disagree on Adjustmentamount for the same key."""

    item:   str
    site:   str
    uom:    str
    values: list[tuple[str, float]]  # [(source_file, amount), …]


def _next_calendar_month(month: pd.Timestamp) -> pd.Timestamp:
    """Return ``month + 1`` calendar month, normalised to first-of-month."""
    base = pd.Timestamp(month).normalize().replace(day=1)
    return base + pd.DateOffset(months=1)


def _norm_str(v: Any) -> str:
    """Whitespace-strip + cast to str; treat ``NaN`` as ''."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def build_vbcs_lookup(
    vbcs_files: Iterable[tuple[str, pd.DataFrame]],
    target_month: pd.Timestamp,
) -> tuple[dict[_VbcsLookupKey, _VbcsLookupHit], list[NewPriceConflict]]:
    """Build a ``(item, uom, site) → amount`` lookup for ``target_month``.

    Scans every VBCS DataFrame and keeps only the rows whose
    ``Adjustmentstartdate`` falls in the SAME calendar (year, month)
    as ``target_month``.  Conflicting rows (two files disagree on
    Adjustmentamount for the same key) are flagged as
    :class:`NewPriceConflict` so the page can surface a per-row warning
    AND mark the New Price blank for the affected row.

    Files missing any of the four required VBCS columns are silently
    skipped — VBCS schemas are well-defined upstream but defensive
    behaviour means a partially-baked file doesn't poison the whole
    Price Book.
    """
    lookup: dict[_VbcsLookupKey, _VbcsLookupHit] = {}
    conflicts: dict[_VbcsLookupKey, NewPriceConflict] = {}
    target_year  = int(target_month.year)
    target_month_no = int(target_month.month)

    for source_file, df in vbcs_files:
        if df is None or df.empty:
            continue
        required = (VBCS_COL_ITEM, VBCS_COL_UOM, VBCS_COL_SHIP_TO, VBCS_COL_AMOUNT, VBCS_COL_START_DATE)
        norm_cols = {c.strip().casefold(): c for c in df.columns}
        col_map: dict[str, Optional[str]] = {
            req: norm_cols.get(req.strip().casefold()) for req in required
        }
        if any(v is None for v in col_map.values()):
            logger.warning(
                "VBCS file %s is missing required column(s); skipping. "
                "Required: %r, present: %r",
                source_file, list(required), list(df.columns),
            )
            continue

        # Date-coerce once per file (vectorised) so the per-row month
        # comparison below is a cheap int compare.
        starts = pd.to_datetime(df[col_map[VBCS_COL_START_DATE]], errors="coerce")
        amounts = pd.to_numeric(df[col_map[VBCS_COL_AMOUNT]], errors="coerce")
        items = df[col_map[VBCS_COL_ITEM]].astype(str).str.strip()
        uoms  = df[col_map[VBCS_COL_UOM]].astype(str).str.strip()
        sites = df[col_map[VBCS_COL_SHIP_TO]].astype(str).str.strip()

        # Mask rows that fall in the target (year, month).
        mask = (
            starts.dt.year.eq(target_year)
            & starts.dt.month.eq(target_month_no)
            & amounts.notna()
        )
        sub = df.loc[mask]
        if sub.empty:
            continue

        for idx in sub.index:
            key = _VbcsLookupKey(
                item=items.at[idx],
                uom=uoms.at[idx],
                site=sites.at[idx],
            )
            amount = float(amounts.at[idx])
            existing = lookup.get(key)
            if existing is None:
                lookup[key] = _VbcsLookupHit(amount=amount, source_file=source_file)
                continue
            # Same key already seen — only escalate to a conflict when
            # the amounts actually differ (a duplicate-but-identical
            # row from the same file is benign).
            if _amounts_differ(existing.amount, amount):
                conflict = conflicts.get(key)
                if conflict is None:
                    conflict = NewPriceConflict(
                        item=key.item,
                        site=key.site,
                        uom=key.uom,
                        values=[(existing.source_file, existing.amount)],
                    )
                    conflicts[key] = conflict
                conflict.values.append((source_file, amount))

    # Strip conflicted keys from the lookup so the row-level fill below
    # leaves their New Price blank rather than emit a misleading match.
    for key in conflicts:
        lookup.pop(key, None)

    return lookup, list(conflicts.values())


def _amounts_differ(a: float, b: float, *, abs_tol: float = 1e-4) -> bool:
    """Treat sub-cent rounding noise (≤0.0001) as equal."""
    import math
    return not math.isclose(a, b, abs_tol=abs_tol, rel_tol=1e-9)


# ── Price Book builder ───────────────────────────────────────────────────────


@dataclass
class PriceBookResult:
    """Result of :func:`build_price_book` — DataFrame plus per-row diagnostics."""

    df: pd.DataFrame
    target_month: Optional[pd.Timestamp] = None
    matched_rows:  int = 0
    unmatched_rows: int = 0
    conflicts: list[NewPriceConflict] = field(default_factory=list)


def build_price_book(
    parquet_df: pd.DataFrame,
    selections: dict[str, list[Any]],
    column_map: dict[str, str],
    *,
    topmost_start_date: pd.Timestamp,
    vbcs_files: Iterable[tuple[str, pd.DataFrame]],
) -> PriceBookResult:
    """Compose the Price Book DataFrame for the chosen filter slice.

    Steps
    -----
    1.  Filter ``parquet_df`` by every non-empty selection.
    2.  Project the surviving rows into the canonical output schema
        (``OUTPUT_COLUMNS``) — Item, Description, UOM, Old Price,
        Start / End dates carry over directly.
    3.  Compute ``target_month = topmost_start_date + 1 calendar month``
        and look up ``New Price`` for every row from
        :func:`build_vbcs_lookup`.
    4.  Compute ``Price Change = New − Old`` (blank when New is blank).

    Parameters
    ----------
    parquet_df
        The full B2C Pricing History as returned by
        :func:`load_b2c_pricing_history`.
    selections
        ``{logical_filter_name: [picks…]}`` map.  Empty / missing
        entries mean "no filter on that axis".
    column_map
        ``{logical → actual}`` map from :func:`validate_parquet_schema`.
    topmost_start_date
        The earliest Price Adjustment Start Date the user selected
        (per the May-2026-late operator contract: when several dates
        are picked, use the earliest as the +1-month reference).
    vbcs_files
        ``[(filename, df), …]`` as returned by
        :func:`vbcs_refrehable_store.read_all`.
    """
    filtered = apply_filters(parquet_df, selections, column_map)
    if filtered.empty:
        return PriceBookResult(df=pd.DataFrame(columns=OUTPUT_COLUMNS))

    target_month = _next_calendar_month(topmost_start_date)
    lookup, conflicts = build_vbcs_lookup(vbcs_files, target_month)

    # Snapshot the columns we project from so the row build stays
    # readable.  Each ``.get`` defaults to the logical name to maintain
    # the contract that resolved columns are case/whitespace-tolerant.
    col_item   = column_map[PARQUET_COL_ITEM]
    col_desc   = column_map[PARQUET_COL_ITEM_DESC]
    col_uom    = column_map[PARQUET_COL_UOM]
    col_old    = column_map[PARQUET_COL_OLD_PRICE]
    col_start  = column_map[PARQUET_COL_START_DATE]
    col_end    = column_map[PARQUET_COL_END_DATE]
    col_site   = column_map[PARQUET_COL_SHIP_TO]

    item_series  = filtered[col_item].astype(str).str.strip()
    desc_series  = filtered[col_desc].astype(str).str.strip()
    uom_series   = filtered[col_uom].astype(str).str.strip()
    site_series  = filtered[col_site].astype(str).str.strip()
    old_series   = pd.to_numeric(filtered[col_old], errors="coerce")
    start_series = pd.to_datetime(filtered[col_start], errors="coerce")
    end_series   = pd.to_datetime(filtered[col_end], errors="coerce")

    new_prices: list[Optional[float]] = []
    for idx in filtered.index:
        key = _VbcsLookupKey(
            item=item_series.at[idx],
            uom=uom_series.at[idx],
            site=site_series.at[idx],
        )
        hit = lookup.get(key)
        new_prices.append(round(hit.amount, 4) if hit is not None else None)

    old_rounded = old_series.round(4)
    # Build Price Change as None when New is None — otherwise the
    # subtraction would silently propagate NaN even when both sides
    # were declared blank.
    changes: list[Optional[float]] = []
    for new_v, old_v in zip(new_prices, old_rounded.tolist()):
        if new_v is None or pd.isna(old_v):
            changes.append(None)
        else:
            changes.append(round(float(new_v) - float(old_v), 4))

    out = pd.DataFrame({
        OUT_COL_ITEM:   item_series.values,
        OUT_COL_DESC:   desc_series.values,
        OUT_COL_UOM:    uom_series.values,
        OUT_COL_OLD:    old_rounded.values,
        OUT_COL_NEW:    new_prices,
        OUT_COL_CHANGE: changes,
        OUT_COL_START:  start_series.dt.strftime("%Y-%m-%d").where(start_series.notna(), ""),
        OUT_COL_END:    end_series.dt.strftime("%Y-%m-%d").where(end_series.notna(), ""),
    })
    out = out[list(OUTPUT_COLUMNS)].reset_index(drop=True)

    matched = sum(1 for v in new_prices if v is not None)
    unmatched = len(new_prices) - matched
    return PriceBookResult(
        df=out,
        target_month=target_month,
        matched_rows=matched,
        unmatched_rows=unmatched,
        conflicts=conflicts,
    )


# ── Convenience: read every VBCS CSV from the lakehouse ──────────────────────


def read_all_vbcs_files() -> list[tuple[str, pd.DataFrame]]:
    """Return every published VBCS CSV.

    Thin wrapper around :func:`vbcs_refrehable_store.read_all` — kept
    here so the page only ever imports ``price_book_distribution`` for
    everything Price-Book-related.
    """
    return _vbcs_store.read_all()


# ── Email distribution ───────────────────────────────────────────────────────


_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
)


@dataclass
class EmailDeliveryResult:
    """Per-recipient send outcome."""

    recipient: str
    success:   bool
    error:     Optional[str] = None


def parse_recipients(raw: str) -> list[str]:
    """Split a free-form recipient string into normalised email addresses.

    Accepts comma, semicolon, or newline separators (and any mix).
    Whitespace is stripped, empty tokens are dropped, and duplicates
    are collapsed.  Does NOT validate addresses — call
    :func:`validate_recipients` on the result when you need that.
    """
    if not raw:
        return []
    tokens = [t.strip() for t in re.split(r"[,\n;]+", raw)]
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if not t:
            continue
        key = t.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def validate_recipients(addresses: list[str]) -> tuple[list[str], list[str]]:
    """Split ``addresses`` into ``(valid, invalid)`` lists.

    Lightweight regex check — not RFC 5322 strict but rejects the
    common typos (missing @, missing TLD, embedded spaces).
    """
    valid:   list[str] = []
    invalid: list[str] = []
    for addr in addresses:
        if _EMAIL_RE.match(addr):
            valid.append(addr)
        else:
            invalid.append(addr)
    return valid, invalid


def _read_smtp_config() -> dict[str, Any]:
    """Read the shared ``[task_manager_email]`` SMTP config from secrets.

    Mirrors ``utils.notification_helpers._read_email_config`` exactly
    so a missing-key error message reads the same in both flows.
    """
    section = "task_manager_email"
    if section not in st.secrets:
        raise PriceBookError(
            "Missing [task_manager_email] in .streamlit/secrets.toml — "
            "the same SMTP credentials used for task reminders are reused "
            "here.  Add the section with smtp_host, smtp_port, "
            "smtp_username, smtp_password, from_email."
        )
    cfg = dict(st.secrets[section])
    required = ("smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise PriceBookError(
            f"[task_manager_email] missing required keys: {', '.join(missing)}"
        )
    cfg.setdefault("use_tls", True)
    return cfg


def send_price_book(
    *,
    recipients: list[str],
    csv_bytes:  bytes,
    file_name:  str,
    summary_lines: Optional[list[str]] = None,
) -> list[EmailDeliveryResult]:
    """Send ``csv_bytes`` as a CSV attachment to every recipient.

    One SMTP connection per send call — recipients are batched into a
    single ``send_message`` round-trip (the SMTP server handles the
    per-recipient delivery internally).  Returns one
    :class:`EmailDeliveryResult` per address so the UI can render a
    per-address ✅ / ❌ list.

    Failures are LOCALISED: an authentication or connection error
    fails EVERY recipient with the same error string; an SMTP
    per-recipient refusal (e.g. ``5.7.1 Relay denied``) fails only the
    refused address while the others land.
    """
    if not recipients:
        return []
    cfg = _read_smtp_config()

    msg = EmailMessage()
    msg["From"]    = str(cfg["from_email"])
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = (
        f"Darigold Price Book — generated {pd.Timestamp.now():%Y-%m-%d %H:%M}"
    )
    body_lines = [
        "The attached CSV is the Price Book generated from the Pricing "
        "Execution > Variable Pricing > Distribute Price Book section.",
        "",
        f"Filename: {file_name}",
    ]
    if summary_lines:
        body_lines.append("")
        body_lines.extend(summary_lines)
    msg.set_content("\n".join(body_lines))
    msg.add_attachment(
        csv_bytes,
        maintype="text",
        subtype="csv",
        filename=file_name,
    )

    results: list[EmailDeliveryResult] = []
    try:
        server = smtplib.SMTP(str(cfg["smtp_host"]), int(cfg["smtp_port"]), timeout=30)
        try:
            if bool(cfg.get("use_tls", True)):
                server.starttls()
            server.login(str(cfg["smtp_username"]), str(cfg["smtp_password"]))
            refused = server.send_message(msg)
        finally:
            server.quit()
    except Exception as exc:  # noqa: BLE001 — surface every SMTP error
        # Transport-wide failure — every recipient gets the same error.
        for r in recipients:
            results.append(EmailDeliveryResult(
                recipient=r, success=False, error=f"{type(exc).__name__}: {exc}",
            ))
        return results

    # ``send_message`` returns ``{recipient: (code, message)}`` for the
    # refused subset (and an empty dict on full success).
    for r in recipients:
        if r in refused:
            code, smtp_msg = refused[r]
            err_text = smtp_msg.decode() if isinstance(smtp_msg, (bytes, bytearray)) else str(smtp_msg)
            results.append(EmailDeliveryResult(
                recipient=r, success=False, error=f"SMTP {code}: {err_text}",
            ))
        else:
            results.append(EmailDeliveryResult(recipient=r, success=True))
    return results


# ── Public API export ────────────────────────────────────────────────────────


__all__ = [
    "PriceBookError",
    "PriceBookResult",
    "NewPriceConflict",
    "EmailDeliveryResult",
    "OUT_COL_ITEM", "OUT_COL_DESC", "OUT_COL_UOM",
    "OUT_COL_OLD",  "OUT_COL_NEW",  "OUT_COL_CHANGE",
    "OUT_COL_START", "OUT_COL_END",
    "OUTPUT_COLUMNS",
    "CASCADING_FILTERS",
    "PARQUET_COL_ITEM", "PARQUET_COL_ITEM_DESC",
    "PARQUET_COL_SHIP_TO", "PARQUET_COL_UOM",
    "PARQUET_COL_OLD_PRICE", "PARQUET_COL_START_DATE",
    "PARQUET_COL_END_DATE", "PARQUET_COL_ITEM_CATEGORY",
    "PARQUET_COL_CUSTOMER",
    "load_b2c_pricing_history",
    "invalidate_parquet_cache",
    "validate_parquet_schema",
    "apply_filters",
    "distinct_options",
    "build_vbcs_lookup",
    "build_price_book",
    "read_all_vbcs_files",
    "parse_recipients",
    "validate_recipients",
    "send_price_book",
]
