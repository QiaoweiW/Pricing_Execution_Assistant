"""
OneLake-backed store for the FMMO (Federal Milk Marketing Order) tracker.

Two JSON blobs live in the same Microsoft Fabric Lakehouse the HTST
Activity Monitor already reads from — no new vendor, no new auth flow,
persistent across Streamlit Cloud redeploys.

Storage layout
--------------
``Files/Milk_cost_tracker/fmmo_tracker.json``    — the table, as a JSON array of row dicts.
``Files/Milk_cost_tracker/milk_mover_state.json`` — PDF source-state cache (one entry per URL).

Both blobs live alongside ``Milk_Usage_Stable.csv`` and
``base_milk_cost_monthly_tracker.csv`` under
``Files/Milk_cost_tracker/``, so every artefact that backs the Milk
Commodity Cost view sits in one folder a user can open in OneLake
explorer for auditing.

Why two blobs and not one?
    1. A user opening ``fmmo_tracker.json`` in OneLake explorer to
       audit/edit a row should not see machine-managed PDF fingerprints
       alongside their data.
    2. Writes to one don't invalidate the ETag of the other, so the
       advanced-prices change-detection cycle can update its
       bookkeeping without conflicting with a concurrent manual edit
       to the table.

Public API (mirrors the legacy SQLite module so the autoupdate
orchestrator and the view need only minimal changes):
    read_milk_mover_df()             -> pd.DataFrame
    latest_month()                   -> Optional[pd.Timestamp]
    insert_rows(rows)                -> int  (number of rows actually added)
    has_rows_for_month(target_month) -> bool
    seed_from_csv_if_empty(csv_path) -> int  (number of rows seeded; 0 if non-empty)
    get_pdf_state(url)               -> Optional[dict]
    upsert_pdf_state(url, **fields)  -> None
    get_store_label()                -> str  (human-readable for status captions)
    get_table_blob_path()            -> str  (live path for UI captions)

Concurrency model
-----------------
Every write goes through :func:`fabric_lakehouse_io.update_json`, which
implements ETag-based optimistic concurrency with bounded retries. We
no longer maintain our own copy of that logic.

Configuration
-------------
The store reads ``workspace`` and ``lakehouse`` from
``[fabric_milk_mover]`` when present, falling back to ``[fabric_htst]``
(the existing HTST block) so deployments don't need to duplicate
config. Service-principal keys are honoured by the shared ``fabric_auth``
chain — no module-local credential plumbing.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io


# ── Public errors ────────────────────────────────────────────────────────────

class MilkMoverStoreError(RuntimeError):
    """Raised on any configuration, auth, or storage failure for the store.

    Wrapped so the page renders a single clean error path instead of
    leaking Azure SDK / Streamlit stack traces.
    """


# ── Constants ────────────────────────────────────────────────────────────────

# Secrets-section name. Inherits workspace/lakehouse from [fabric_htst]
# when the dedicated [fabric_milk_mover] block is absent — see
# ``fabric_lakehouse_io._read_lakehouse_config``.
_SECRETS_SECTION: str = "fabric_milk_mover"

# Subfolder inside the lakehouse Files/ root that holds every milk-cost
# artefact (FMMO tracker, PDF state cache, Milk_Usage_Stable, and the
# append-only base_milk_cost_monthly_tracker). Co-locating them under
# one folder keeps the lakehouse explorer view tidy and makes
# per-feature permissions / lifecycle policies easy to apply.
_FOLDER_PREFIX: str = "Milk_cost_tracker"

# Blob paths (POSIX-style, relative to the lakehouse Files/ root). The
# table file is named ``fmmo_tracker.json`` because it stores the four
# Federal Milk Marketing Order rows (HTST/ESL × Class I/II) derived from
# USDA's advanced-prices PDF; the previous flat-root name
# ``milk_mover_tracker.json`` was retired when the folder was introduced.
_TABLE_BLOB_PATH: str = f"{_FOLDER_PREFIX}/fmmo_tracker.json"
_STATE_BLOB_PATH: str = f"{_FOLDER_PREFIX}/milk_mover_state.json"

# Canonical column names — same shape as the legacy CSV / SQLite schema
# so every downstream consumer of read_milk_mover_df() keeps working
# unchanged.
COL_CATEGORY  = "Category"
COL_MONTH     = "Month"
COL_CLASS     = "Class"
COL_SKIM      = "Skim Rate"
COL_BUTTERFAT = "Butterfat Rate"
ALL_COLUMNS: tuple[str, ...] = (COL_CATEGORY, COL_MONTH, COL_CLASS, COL_SKIM, COL_BUTTERFAT)

# Streamlit-cache TTL for blob reads. OneLake reads cost ~50–200 ms;
# caching for one minute makes the page feel instant on rapid reruns
# while still picking up out-of-band edits within ~60 s.
_READ_CACHE_TTL_SECONDS: int = 60

# Default seed CSV used to bootstrap a fresh OneLake table — same path
# the legacy SQLite implementation used.
_DEFAULT_SEED_CSV: Path = (
    Path(__file__).resolve().parent.parent
    / "data" / "Market Barometer" / "Montly Movers" / "Milk_Mover_Tracker.csv"
)


# ── Row serialisation helpers ────────────────────────────────────────────────

def _row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Return the composite primary key for a row, used for INSERT-OR-IGNORE."""
    return (
        str(row[COL_CATEGORY]).strip(),
        str(row[COL_MONTH]),
        str(row[COL_CLASS]).strip(),
    )


def _normalise_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coerce every row to the canonical column set & types.

    * Month is normalised to ``YYYY-MM-DD`` so JSON sorting is chronological.
    * NaNs become ``None`` (i.e. JSON null) so a future re-read by pandas
      yields ``NaN`` consistently.
    """
    out: list[dict[str, Any]] = []
    for raw in rows:
        cleaned = {c: raw.get(c) for c in ALL_COLUMNS}
        if cleaned[COL_MONTH] is not None:
            ts = pd.to_datetime(cleaned[COL_MONTH], errors="coerce")
            if pd.isna(ts):
                # Skip rows with un-parseable months — never crash the pipeline.
                continue
            cleaned[COL_MONTH] = ts.normalize().strftime("%Y-%m-%d")
        for c in (COL_SKIM, COL_BUTTERFAT):
            v = cleaned[c]
            cleaned[c] = None if (v is None or pd.isna(v)) else float(v)
        cleaned[COL_CATEGORY] = str(cleaned[COL_CATEGORY]).strip()
        cleaned[COL_CLASS]    = str(cleaned[COL_CLASS]).strip()
        out.append(cleaned)
    return out


# ── Internal: raw-rows access (cached) ───────────────────────────────────────

@st.cache_data(ttl=_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _read_rows_cached() -> list[dict[str, Any]]:
    """Cached fetch of the raw row list. Cleared by every write helper.

    NOTE: callers are expected to bypass this cache (via
    :func:`invalidate_read_cache`) when they observe an empty list on a
    cold read — otherwise an "absent blob" answer pins for up to
    ``_READ_CACHE_TTL_SECONDS`` even after the auto-updater (or a manual
    out-of-band write) populates the blob in OneLake.
    """
    try:
        rows, _etag = _io.read_json(_SECRETS_SECTION, _TABLE_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc
    return list(rows or [])


def _invalidate_read_cache() -> None:
    """Drop the cached row list after a write so the next read sees fresh data."""
    _read_rows_cached.clear()


def invalidate_read_cache() -> None:
    """Public alias of :func:`_invalidate_read_cache`.

    Page code calls this immediately before re-reading the FMMO table
    after triggering the auto-updater (or after the user clicks
    "USDA refresh"), so a cached "blob empty" answer is dropped before
    the next read.
    """
    _invalidate_read_cache()


def _read_rows_with_fallback() -> list[dict[str, Any]]:
    """Read rows with a one-shot retry that bypasses the local cache.

    Mirrors the resin-store ``_read_with_fallback`` helper.  Plain
    :func:`_read_rows_cached` keeps even an empty list for up to
    ``_READ_CACHE_TTL_SECONDS``; that's normally fine but breaks the
    cold-start UX when the auto-updater seeds the blob a few hundred ms
    after we've already cached "absent".  This helper invalidates the
    cache once and re-reads directly when the cached answer is empty,
    paying at most one extra HTTPS round-trip per page render.
    """
    rows = _read_rows_cached()
    if rows:
        return rows
    _invalidate_read_cache()
    try:
        rows, _etag = _io.read_json(_SECRETS_SECTION, _TABLE_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc
    return list(rows or [])


# ── Public API: data table ───────────────────────────────────────────────────

def read_milk_mover_df() -> pd.DataFrame:
    """Return the FMMO table as a DataFrame matching the legacy CSV shape.

    Output columns (in order): ``Category, Month, Class, Skim Rate,
    Butterfat Rate``. Months are returned as ``M/D/YYYY`` strings (no
    zero-padding) — the same format the legacy CSV used — so existing
    parsers (``_parse_month``) accept them unchanged.

    Uses :func:`_read_rows_with_fallback` so a cached empty answer from
    a prior cold render does not mask a freshly-seeded OneLake blob.
    """
    rows = _read_rows_with_fallback()
    if not rows:
        return pd.DataFrame(columns=list(ALL_COLUMNS))

    df = pd.DataFrame(rows, columns=list(ALL_COLUMNS))
    months = pd.to_datetime(df[COL_MONTH])
    df[COL_MONTH] = months.apply(lambda d: f"{d.month}/{d.day}/{d.year}")
    return df


def latest_month() -> Optional[pd.Timestamp]:
    """Return the most-recent month in the table, or ``None`` when empty."""
    rows = _read_rows_cached()
    if not rows:
        return None
    months = pd.to_datetime([r[COL_MONTH] for r in rows], errors="coerce")
    months = months.dropna() if hasattr(months, "dropna") else pd.Series(months).dropna()
    if len(months) == 0:
        return None
    return pd.Timestamp(max(months)).normalize().replace(day=1)


def has_rows_for_month(target_month: pd.Timestamp) -> bool:
    """Return True when at least one row exists for ``target_month``."""
    target_str = pd.Timestamp(target_month).normalize().strftime("%Y-%m-%d")
    return any(r.get(COL_MONTH) == target_str for r in _read_rows_cached())


def insert_rows(rows: Iterable[dict[str, Any]], *, source: str = "auto-update") -> int:
    """Append rows to the table, skipping duplicates by ``(Category, Month, Class)``.

    Returns the number of rows actually inserted. This is the OneLake
    equivalent of SQLite's ``INSERT OR IGNORE`` — re-running the
    auto-update pipeline is a safe no-op when the rows already exist.

    The ``source`` parameter is informational only (recorded as a
    top-level field in each row so future tooling can distinguish
    seed/auto-update/manual rows); it has no effect on dedup or read
    behaviour.
    """
    incoming = _normalise_rows(rows)
    if not incoming:
        return 0

    inserted_count = 0

    def _mutate(current: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        nonlocal inserted_count
        rows_now = list(current) if current else []
        existing_keys = {_row_key(r) for r in rows_now}
        appended: list[dict[str, Any]] = []
        for r in incoming:
            if _row_key(r) in existing_keys:
                continue
            appended.append({**r, "_source": source, "_inserted_at": datetime.utcnow().isoformat()})
            existing_keys.add(_row_key(r))
        inserted_count = len(appended)
        return rows_now + appended if appended else rows_now

    try:
        _io.update_json(_SECRETS_SECTION, _TABLE_BLOB_PATH, _mutate, initial_default=[])
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc

    if inserted_count > 0:
        _invalidate_read_cache()
    return inserted_count


def seed_from_csv_if_empty(csv_path: Optional[Path] = None) -> int:
    """Bootstrap the OneLake table from ``csv_path`` when the blob is absent.

    Returns the number of rows seeded (0 when the blob already has
    content or the CSV is missing). Invoked once on first page render
    after a fresh OneLake setup.
    """
    try:
        existing, _etag = _io.read_json(_SECRETS_SECTION, _TABLE_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc
    if existing:
        return 0  # already populated; never overwrite real data

    csv = csv_path or _DEFAULT_SEED_CSV
    if not csv.exists():
        return 0

    df = pd.read_csv(csv)
    df.columns = [str(c).strip() for c in df.columns]
    seed_rows = _normalise_rows(df.to_dict(orient="records"))
    if not seed_rows:
        return 0

    payload = [
        {**r, "_source": "seed", "_inserted_at": datetime.utcnow().isoformat()}
        for r in seed_rows
    ]
    try:
        _io.write_json(_SECRETS_SECTION, _TABLE_BLOB_PATH, payload, etag=None)
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc

    _invalidate_read_cache()
    return len(payload)


# ── Public API: PDF source-state cache ───────────────────────────────────────

def get_pdf_state(url: str) -> Optional[dict]:
    """Return the cached fingerprint for ``url`` or ``None`` when never checked."""
    try:
        state, _etag = _io.read_json(_SECRETS_SECTION, _STATE_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc
    if not isinstance(state, dict):
        return None
    return state.get(url)


def upsert_pdf_state(
    url: str,
    *,
    etag: Optional[str],
    last_modified: Optional[str],
    content_sha256: Optional[str],
    checked_at: datetime,
    last_change_at: Optional[datetime] = None,
) -> None:
    """Insert or update the cached PDF fingerprint for ``url``."""

    def _mutate(current: Any) -> dict[str, Any]:
        if not isinstance(current, dict):
            current = {}
        previous = current.get(url, {}) if isinstance(current.get(url), dict) else {}
        current[url] = {
            "etag": etag,
            "last_modified": last_modified,
            "content_sha256": content_sha256 or previous.get("content_sha256"),
            "checked_at": checked_at.isoformat(),
            "last_change_at": (
                last_change_at.isoformat() if last_change_at is not None
                else previous.get("last_change_at")
            ),
        }
        return current

    try:
        _io.update_json(_SECRETS_SECTION, _STATE_BLOB_PATH, _mutate, initial_default={})
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc


# ── Public API: store identity (for status captions) ─────────────────────────

def get_store_label() -> str:
    """Return a short human-readable label of where the data lives.

    Used by the page's auto-update status caption. Never raises —
    falls back to a generic string when secrets are missing so the
    caption always renders.
    """
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=_TABLE_BLOB_PATH,
    ).display


def get_table_blob_path() -> str:
    """Return the relative ``Files/<...>`` path of the FMMO tracker JSON.

    Exposed so UI captions can render the live path without duplicating
    the constant — keeps the docstring example and the actual path in
    lock-step if the layout ever moves again.
    """
    return _TABLE_BLOB_PATH


__all__ = [
    "MilkMoverStoreError",
    "COL_CATEGORY",
    "COL_MONTH",
    "COL_CLASS",
    "COL_SKIM",
    "COL_BUTTERFAT",
    "ALL_COLUMNS",
    "read_milk_mover_df",
    "invalidate_read_cache",
    "latest_month",
    "has_rows_for_month",
    "insert_rows",
    "seed_from_csv_if_empty",
    "get_pdf_state",
    "upsert_pdf_state",
    "get_store_label",
    "get_table_blob_path",
]
