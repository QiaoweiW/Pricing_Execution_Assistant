"""
OneLake-backed store for the USDA Dairy Products Sales weekly
**weighted-price** time-series.

Storage layout
--------------
Two blobs under ``Files/Milk_cost_tracker/`` (same folder as the rest of
the milk-cost artefacts so the OneLake explorer view stays tidy):

* ``usda_dairy_products_weighted.csv`` — long-format CSV with one row
  per ``(Week Ending, Product)`` pair:

      ``Week Ending,Product,Weighted Price,Revised``
      ``2026-05-09,Dry Whey,0.6415,False``
      ``2026-05-09,Nonfat Dry Milk,2.0231,False``
      ``...``

* ``usda_dairy_products_state.json`` — fingerprint of the most-recent
  PDF we ingested + the timestamp of the last successful pull, so the
  TTL guard on the UI side can short-circuit redundant fetches.

Design parallels :mod:`cme_spot_call_store` almost exactly. The only
material difference is the additional ``Revised`` column which carries
USDA's own asterisk-flag forward into OneLake. The chart treats
revised/non-revised values identically; the flag is purely an audit
breadcrumb.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io


# ── Storage locations ────────────────────────────────────────────────────────

_SECRETS_SECTION:   str = "fabric_usda_dairy_products"
_FOLDER_PREFIX:     str = "Milk_cost_tracker"
_TABLE_BLOB_PATH:   str = f"{_FOLDER_PREFIX}/usda_dairy_products_weighted.csv"
_STATE_BLOB_PATH:   str = f"{_FOLDER_PREFIX}/usda_dairy_products_state.json"


# ── Canonical columns ────────────────────────────────────────────────────────

COL_WEEK_ENDING:    str = "Week Ending"
COL_PRODUCT:        str = "Product"
COL_WEIGHTED_PRICE: str = "Weighted Price"
COL_REVISED:        str = "Revised"

ALL_COLUMNS: tuple[str, ...] = (
    COL_WEEK_ENDING, COL_PRODUCT, COL_WEIGHTED_PRICE, COL_REVISED,
)

_READ_CACHE_TTL_SECONDS: int = 60


# ── Exception ────────────────────────────────────────────────────────────────

class USDADairyProductsStoreError(RuntimeError):
    """Anything that goes wrong reading/writing this store."""


# ── Public read API ──────────────────────────────────────────────────────────

@st.cache_data(ttl=_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _read_df_cached() -> pd.DataFrame:
    """Cached fetch of the long-format CSV. Empty DataFrame on first run."""
    try:
        df, _etag = _io.read_csv(_SECRETS_SECTION, _TABLE_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        msg = str(exc).lower()
        if "not found" in msg or "does not exist" in msg:
            return pd.DataFrame(columns=list(ALL_COLUMNS))
        raise USDADairyProductsStoreError(str(exc)) from exc
    return _coerce_df(df)


def invalidate_read_cache() -> None:
    """Drop the cached DataFrame after a write."""
    _read_df_cached.clear()


def read_df() -> pd.DataFrame:
    """Return the long-format USDA weighted-price DataFrame.

    Empty on first load. Always carries the four canonical columns in
    :data:`ALL_COLUMNS` order with ``Week Ending`` as a
    ``datetime64[ns]`` and ``Weighted Price`` as ``float64``.
    """
    return _read_df_cached()


def _coerce_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise a raw read to the canonical schema/types.

    Handles the legacy file gracefully — a freshly-bootstrapped store
    may lack the ``Revised`` column; we default it to False in that
    case rather than erroring.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=list(ALL_COLUMNS))
    for c in ALL_COLUMNS:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[list(ALL_COLUMNS)].copy()
    df[COL_WEEK_ENDING] = pd.to_datetime(df[COL_WEEK_ENDING], errors="coerce")
    df[COL_WEIGHTED_PRICE] = pd.to_numeric(df[COL_WEIGHTED_PRICE], errors="coerce")
    df[COL_PRODUCT] = df[COL_PRODUCT].astype(str).str.strip()
    # ``Revised`` round-trips as the string "True"/"False" through CSV;
    # coerce back to bool so downstream filters work naturally.
    df[COL_REVISED] = df[COL_REVISED].apply(_coerce_bool)
    df = df.dropna(subset=[COL_WEEK_ENDING, COL_WEIGHTED_PRICE])
    return df.sort_values([COL_WEEK_ENDING, COL_PRODUCT]).reset_index(drop=True)


def _coerce_bool(value: Any) -> bool:
    """Resilient bool coercion — accepts ``True``/``"True"``/``"true"``/``1``."""
    if isinstance(value, bool):
        return value
    if value is None or pd.isna(value):
        return False
    s = str(value).strip().lower()
    return s in {"true", "1", "yes", "y", "revised"}


# ── Public write API ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AppendResult:
    """Outcome of a :func:`dedup_append_rows` call."""
    inserted:    int
    skipped:     int
    revised:     int   # rows whose key existed but value differs from what's stored
    total_after: int


def dedup_append_rows(
    rows: Iterable[dict[str, Any]],
    *,
    source: str = "auto-update",
    update_revised: bool = True,
) -> AppendResult:
    """Append rows, deduping by ``(Week Ending, Product)``.

    Behaviour
    ---------
    * NEW keys → inserted.
    * EXISTING keys with the same value → skipped.
    * EXISTING keys with a different value AND ``update_revised`` →
      the stored row is overwritten (USDA revises the prior four weeks
      and we want the latest figure to win, not the first one we saw).
      Counted under ``revised`` in the result.
    * EXISTING keys with a different value AND NOT ``update_revised``
      → skipped, with a debug log entry.

    Defaulting ``update_revised`` to True matches USDA's own convention
    (the most-recent PDF is authoritative for any week-ending it covers).
    """
    existing = read_df()
    existing_index: dict[tuple[pd.Timestamp, str], int] = {}
    if not existing.empty:
        for idx, row in existing[[COL_WEEK_ENDING, COL_PRODUCT]].iterrows():
            existing_index[(pd.Timestamp(row[COL_WEEK_ENDING]),
                            str(row[COL_PRODUCT]).strip())] = idx

    # Materialise the input rows for indexed access.
    incoming: list[dict[str, Any]] = []
    skipped = 0
    for raw in rows:
        try:
            week = pd.to_datetime(raw[COL_WEEK_ENDING]).normalize()
        except (KeyError, ValueError, TypeError):
            skipped += 1
            continue
        product = str(raw.get(COL_PRODUCT, "")).strip()
        try:
            value = float(raw[COL_WEIGHTED_PRICE])
        except (KeyError, ValueError, TypeError):
            skipped += 1
            continue
        if not product:
            skipped += 1
            continue
        incoming.append({
            COL_WEEK_ENDING:    week,
            COL_PRODUCT:        product,
            COL_WEIGHTED_PRICE: value,
            COL_REVISED:        bool(raw.get(COL_REVISED, False)),
        })

    if not incoming:
        return AppendResult(inserted=0, skipped=skipped, revised=0, total_after=len(existing))

    # Apply updates / inserts.
    inserted = 0
    revised  = 0
    df = existing.copy()
    new_rows: list[dict[str, Any]] = []
    for r in incoming:
        key = (r[COL_WEEK_ENDING], r[COL_PRODUCT])
        if key in existing_index:
            idx = existing_index[key]
            stored_value   = float(df.at[idx, COL_WEIGHTED_PRICE])
            stored_revised = bool(df.at[idx, COL_REVISED])
            if abs(stored_value - r[COL_WEIGHTED_PRICE]) < 1e-9 \
               and stored_revised == r[COL_REVISED]:
                skipped += 1
                continue
            if update_revised:
                df.at[idx, COL_WEIGHTED_PRICE] = r[COL_WEIGHTED_PRICE]
                df.at[idx, COL_REVISED]        = r[COL_REVISED]
                if "_source" in df.columns:
                    df.at[idx, "_source"] = source
                if "_inserted_at" in df.columns:
                    df.at[idx, "_inserted_at"] = (
                        datetime.utcnow().isoformat(timespec="seconds") + "Z"
                    )
                revised += 1
            else:
                skipped += 1
        else:
            new_rows.append({
                **r,
                "_source":      source,
                "_inserted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            })
            inserted += 1

    if not new_rows and revised == 0:
        return AppendResult(inserted=0, skipped=skipped, revised=0, total_after=len(existing))

    if new_rows:
        df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True, sort=False)

    leading = [c for c in ALL_COLUMNS if c in df.columns]
    trailing = [c for c in df.columns if c not in ALL_COLUMNS]
    df = df[leading + trailing]
    df = df.sort_values([COL_WEEK_ENDING, COL_PRODUCT]).reset_index(drop=True)

    _write_df(df)
    invalidate_read_cache()
    return AppendResult(
        inserted=inserted,
        skipped=skipped,
        revised=revised,
        total_after=len(df),
    )


def _write_df(df: pd.DataFrame) -> None:
    """Internal: persist ``df`` to the OneLake CSV."""
    try:
        _io.write_csv(_SECRETS_SECTION, _TABLE_BLOB_PATH, df, etag=None)
    except _io.LakehouseIOError as exc:
        raise USDADairyProductsStoreError(
            f"Failed writing {_TABLE_BLOB_PATH} to OneLake: {exc}"
        ) from exc


# ── PDF-fingerprint state ────────────────────────────────────────────────────

def get_pdf_state(url: str) -> Optional[dict]:
    """Return the cached fingerprint for ``url`` (or ``None``)."""
    try:
        state, _etag = _io.read_json(_SECRETS_SECTION, _STATE_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        msg = str(exc).lower()
        if "not found" in msg or "does not exist" in msg:
            return None
        raise USDADairyProductsStoreError(str(exc)) from exc
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
    """Persist the fingerprint for ``url`` after a check/ingest."""

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
        raise USDADairyProductsStoreError(
            f"Failed updating {_STATE_BLOB_PATH}: {exc}"
        ) from exc


# ── Identity helpers ─────────────────────────────────────────────────────────

def get_store_label() -> str:
    """Short label of where the table lives, for status captions."""
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=_TABLE_BLOB_PATH,
    ).display


def get_table_blob_path() -> str:
    """Return the relative Files/-rooted path to the USDA CSV."""
    return _TABLE_BLOB_PATH


__all__ = [
    "COL_WEEK_ENDING",
    "COL_PRODUCT",
    "COL_WEIGHTED_PRICE",
    "COL_REVISED",
    "ALL_COLUMNS",
    "AppendResult",
    "USDADairyProductsStoreError",
    "dedup_append_rows",
    "get_pdf_state",
    "get_store_label",
    "get_table_blob_path",
    "invalidate_read_cache",
    "read_df",
    "upsert_pdf_state",
]
