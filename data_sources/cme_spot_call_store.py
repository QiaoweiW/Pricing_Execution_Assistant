"""
OneLake-backed store for the CME spot-call **weekly average** time-series.

Storage layout
--------------
Two blobs co-located with the rest of the milk-cost artefacts under
``Files/Milk_cost_tracker/``:

* ``cme_spot_call_weekly.csv`` — long-format CSV with one row per
  ``(Week Ending, Product)`` pair:

      ``Week Ending,Product,Weekly Average``
      ``2026-05-15,Cheese,1.6035``
      ``2026-05-15,Butter,1.6385``
      ``...``

  Long format chosen so:
    - Adding a future product (e.g. Cheese Barrels) requires zero schema
      changes.
    - The UI does ``df.groupby("Product")`` to emit one plotly trace per
      product without any pivot acrobatics.
    - CSV diffs in OneLake explorer show "added 4 rows this week" instead
      of "all cells changed".

* ``cme_spot_call_state.json`` — companion state blob persisting the
  fingerprint (ETag / Last-Modified / SHA-256) of the most-recent USDA
  PDF we successfully ingested, plus the timestamp of the last
  successful pull. Used by the auto-refresh gate so a tab-switch back
  to the Market Barometer page doesn't trigger redundant network I/O.

Behaviour guarantees
--------------------
1. ``dedup_append_rows`` is idempotent — re-running with the same
   ``(Week Ending, Product)`` tuples is a no-op (the existing row wins,
   any caller-supplied "Weekly Average" is ignored when the key already
   exists).
2. The state blob is always written AFTER a successful CSV write so a
   half-finished update can never advance the cursor.
3. Reads are cached with a short TTL so rapid Streamlit reruns don't
   pound OneLake; writes invalidate the cache automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io


# ── Storage locations ────────────────────────────────────────────────────────

# Shared with milk_mover_store / milk_usage_stable_store etc. — all
# milk-cost artefacts live under one folder. The secrets section
# ``[fabric_cme_spot_call]`` inherits workspace/lakehouse from
# ``[fabric_htst]`` (see fabric_lakehouse_io._read_lakehouse_config), so
# no extra secret block is required for deployment.
_SECRETS_SECTION:   str = "fabric_cme_spot_call"
_FOLDER_PREFIX:     str = "Milk_cost_tracker"
_TABLE_BLOB_PATH:   str = f"{_FOLDER_PREFIX}/cme_spot_call_weekly.csv"
_STATE_BLOB_PATH:   str = f"{_FOLDER_PREFIX}/cme_spot_call_state.json"


# ── Canonical column names ───────────────────────────────────────────────────

COL_WEEK_ENDING:    str = "Week Ending"
COL_PRODUCT:        str = "Product"
COL_WEEKLY_AVERAGE: str = "Weekly Average"

ALL_COLUMNS: tuple[str, ...] = (COL_WEEK_ENDING, COL_PRODUCT, COL_WEEKLY_AVERAGE)


# Short read cache so rapid reruns (slider drag, filter click) don't hammer
# OneLake. 60 s matches the FMMO tracker's cache TTL — same UX target.
_READ_CACHE_TTL_SECONDS: int = 60


# ── Exceptions ───────────────────────────────────────────────────────────────

class CMESpotCallStoreError(RuntimeError):
    """Anything that goes wrong reading/writing the CME store."""


# ── Public read API ──────────────────────────────────────────────────────────

@st.cache_data(ttl=_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _read_df_cached() -> pd.DataFrame:
    """Cached fetch of the CME long-format CSV. Empty DataFrame on first run."""
    try:
        df, _etag = _io.read_csv(_SECRETS_SECTION, _TABLE_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        # An empty / not-yet-created file is the normal first-load state.
        msg = str(exc).lower()
        if "not found" in msg or "does not exist" in msg:
            return pd.DataFrame(columns=list(ALL_COLUMNS))
        raise CMESpotCallStoreError(str(exc)) from exc
    return _coerce_df(df)


def invalidate_read_cache() -> None:
    """Drop the cached DataFrame after a write. Call sites are the
    write helpers below; UI code should never need to call this directly.
    """
    _read_df_cached.clear()


def read_df() -> pd.DataFrame:
    """Return the long-format CME weekly-average DataFrame.

    Empty DataFrame on first load (file not yet created). Always has
    the three canonical columns in :data:`ALL_COLUMNS` order with
    ``Week Ending`` as a ``datetime64[ns]`` and ``Weekly Average`` as
    ``float64``.
    """
    return _read_df_cached()


def _coerce_df(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a raw read into the canonical schema/types.

    * Adds missing canonical columns (rare — only on a manually-edited
      file).
    * Drops extra columns silently.
    * Parses ``Week Ending`` to datetime64; rows with un-parseable
      dates are dropped (corrupt rows shouldn't poison the chart).
    * Coerces ``Weekly Average`` to numeric; NaN rows are dropped.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=list(ALL_COLUMNS))
    for c in ALL_COLUMNS:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[list(ALL_COLUMNS)].copy()
    df[COL_WEEK_ENDING] = pd.to_datetime(df[COL_WEEK_ENDING], errors="coerce")
    df[COL_WEEKLY_AVERAGE] = pd.to_numeric(df[COL_WEEKLY_AVERAGE], errors="coerce")
    df = df.dropna(subset=[COL_WEEK_ENDING, COL_WEEKLY_AVERAGE])
    df[COL_PRODUCT] = df[COL_PRODUCT].astype(str).str.strip()
    return df.sort_values([COL_WEEK_ENDING, COL_PRODUCT]).reset_index(drop=True)


# ── Public write API ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AppendResult:
    """Outcome of a :func:`dedup_append_rows` call."""
    inserted: int
    skipped:  int
    total_after: int


def dedup_append_rows(
    rows: Iterable[dict[str, Any]],
    *,
    source: str = "auto-update",
) -> AppendResult:
    """Append the given rows to the OneLake CSV, skipping duplicates.

    Parameters
    ----------
    rows
        Iterable of dicts each carrying ``Week Ending`` (date-like),
        ``Product`` (str), and ``Weekly Average`` (float). Extra keys
        are ignored. ``Week Ending`` is normalised to midnight.
    source
        Free-text label written to the optional ``_source`` column
        when the row is inserted. Defaults to ``"auto-update"``;
        manual uploads pass ``"manual-upload"``.

    Returns
    -------
    AppendResult
        ``inserted`` = number of rows that were NOT already in the
        table (dedup key ``(Week Ending, Product)``). ``skipped`` =
        rows whose key already existed (older value wins — we never
        retroactively rewrite history). ``total_after`` = total row
        count of the table after the write.
    """
    # Read existing table (cached).
    existing = read_df()
    existing_keys: set[tuple[pd.Timestamp, str]] = set(
        zip(existing[COL_WEEK_ENDING], existing[COL_PRODUCT])
    ) if not existing.empty else set()

    inserted_rows: list[dict[str, Any]] = []
    skipped = 0
    for raw in rows:
        try:
            week = pd.to_datetime(raw[COL_WEEK_ENDING]).normalize()
        except (KeyError, ValueError, TypeError):
            # Skip un-parseable inputs rather than crashing the pipeline.
            skipped += 1
            continue
        product = str(raw.get(COL_PRODUCT, "")).strip()
        if not product:
            skipped += 1
            continue
        try:
            value = float(raw[COL_WEEKLY_AVERAGE])
        except (KeyError, ValueError, TypeError):
            skipped += 1
            continue
        key = (week, product)
        if key in existing_keys:
            skipped += 1
            continue
        existing_keys.add(key)
        inserted_rows.append({
            COL_WEEK_ENDING:    week,
            COL_PRODUCT:        product,
            COL_WEEKLY_AVERAGE: value,
            "_source":          source,
            "_inserted_at":     datetime.utcnow().isoformat(timespec="seconds") + "Z",
        })

    if not inserted_rows:
        return AppendResult(inserted=0, skipped=skipped, total_after=len(existing))

    # Build the post-write frame. We preserve any audit columns
    # (``_source``, ``_inserted_at``) that already live in the file
    # while keeping the three canonical columns leading.
    new_df = pd.DataFrame(inserted_rows)
    combined = pd.concat([existing, new_df], ignore_index=True, sort=False)
    # Canonical column order first; any audit columns trail.
    leading = [c for c in ALL_COLUMNS if c in combined.columns]
    trailing = [c for c in combined.columns if c not in ALL_COLUMNS]
    combined = combined[leading + trailing]
    combined = combined.sort_values([COL_WEEK_ENDING, COL_PRODUCT]).reset_index(drop=True)

    _write_df(combined)
    invalidate_read_cache()
    return AppendResult(
        inserted=len(inserted_rows),
        skipped=skipped,
        total_after=len(combined),
    )


def _write_df(df: pd.DataFrame) -> None:
    """Internal: serialise ``df`` to the OneLake CSV.

    Wrapped in a single try / except so the caller sees one well-shaped
    :class:`CMESpotCallStoreError` instead of an SDK exception.
    """
    try:
        _io.write_csv(_SECRETS_SECTION, _TABLE_BLOB_PATH, df, etag=None)
    except _io.LakehouseIOError as exc:
        raise CMESpotCallStoreError(
            f"Failed writing {_TABLE_BLOB_PATH} to OneLake: {exc}"
        ) from exc


# ── Public PDF-fingerprint state ─────────────────────────────────────────────

def get_pdf_state(url: str) -> Optional[dict]:
    """Return the cached fingerprint for ``url`` (or ``None``).

    Same shape as :func:`milk_mover_store.get_pdf_state` so callers can
    reuse familiar idioms.
    """
    try:
        state, _etag = _io.read_json(_SECRETS_SECTION, _STATE_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        msg = str(exc).lower()
        if "not found" in msg or "does not exist" in msg:
            return None
        raise CMESpotCallStoreError(str(exc)) from exc
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
    last_success_at: Optional[datetime] = None,
) -> None:
    """Insert or update the cached PDF fingerprint for ``url``.

    ``last_success_at`` is the auxiliary field this store cares about
    that ``milk_mover_store`` doesn't: it's the timestamp of the most
    recent SUCCESSFUL ingest (an unchanged-content check sets
    ``checked_at`` but NOT ``last_success_at``). The Friday-9am gate
    uses it to decide whether we've already pulled this week.
    """

    def _mutate(current: Any) -> dict[str, Any]:
        if not isinstance(current, dict):
            current = {}
        previous = current.get(url, {}) if isinstance(current.get(url), dict) else {}
        current[url] = {
            "etag": etag,
            "last_modified": last_modified,
            "content_sha256": content_sha256 or previous.get("content_sha256"),
            "checked_at": checked_at.isoformat(),
            "last_success_at": (
                last_success_at.isoformat() if last_success_at is not None
                else previous.get("last_success_at")
            ),
        }
        return current

    try:
        _io.update_json(_SECRETS_SECTION, _STATE_BLOB_PATH, _mutate, initial_default={})
    except _io.LakehouseIOError as exc:
        raise CMESpotCallStoreError(
            f"Failed updating {_STATE_BLOB_PATH}: {exc}"
        ) from exc


# ── Identity helpers (for UI captions) ───────────────────────────────────────

def get_store_label() -> str:
    """Short human-readable label of where the table lives in OneLake."""
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=_TABLE_BLOB_PATH,
    ).display


def get_table_blob_path() -> str:
    """Return the relative Files/-rooted path to the CME CSV."""
    return _TABLE_BLOB_PATH


__all__ = [
    "COL_WEEK_ENDING",
    "COL_PRODUCT",
    "COL_WEEKLY_AVERAGE",
    "ALL_COLUMNS",
    "AppendResult",
    "CMESpotCallStoreError",
    "dedup_append_rows",
    "get_pdf_state",
    "get_store_label",
    "get_table_blob_path",
    "invalidate_read_cache",
    "read_df",
    "upsert_pdf_state",
]
