"""
OneLake-backed store for ``Milk_Usage_Stable.csv``.

The Monthly Movers section of the Market Barometer page used to require
the user to upload ``Milk_Usage_Stable.csv`` on every visit. The file is
slow-changing reference data — the per-item Skim/Butterfat usage and
Class/Category mapping — so it belongs in Fabric, alongside the
already-migrated FMMO tracker.

Storage layout
--------------
``Files/Milk_cost_tracker/Milk_Usage_Stable.csv``  — the table, in its
canonical CSV shape (``Item, Item Description, Class, Category,
Skim Usage, Butterfat Usage, Protein Usage, Other Solids Usage``).
Co-located with ``fmmo_tracker.json`` and
``base_milk_cost_monthly_tracker.csv`` under the single
``Milk_cost_tracker`` folder so every milk-cost artefact lives in one
place a user can open in OneLake explorer.

``Protein Usage`` and ``Other Solids Usage`` were introduced for the
Culture category (formerly "Cottage Cheese", renamed May-2026-late).
HTST/ESL items carry ``0`` for both so the additive cost formula in
``_build_milk_usage_with_movers`` collapses back to the legacy
Skim+Butterfat behaviour for non-Culture items — the new schema is
fully backward-compatible for cost arithmetic.

Why CSV and not JSON?
    * The file is 35 KB / ~685 rows — CSV stays human-readable in
      OneLake's Files explorer for the rare manual edit.
    * Downstream code (``_build_milk_usage_with_movers``) consumes a
      pandas DataFrame, so JSON would gain us nothing.
    * The future "download as CSV / upload to replace" workflow on the
      New Price Quote page expects CSV anyway.

Public API (deliberately tiny — same shape as the milk-mover store helpers
the page already calls):
    read_milk_usage_stable_df()       -> pd.DataFrame
    seed_from_csv_if_empty(csv_path)  -> int   (rows seeded; 0 if already present)
    write_milk_usage_stable_df(df)    -> None  (full overwrite, ETag-guarded)
    get_store_label()                 -> str   (for UI captions)

This module reuses the [fabric_htst] secrets block by default; an
optional [fabric_milk_usage_stable] block in secrets.toml may override
``workspace`` / ``lakehouse`` if the milk inputs ever need to live in a
different lakehouse from HTST. See ``fabric_lakehouse_io._read_lakehouse_config``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io


logger = logging.getLogger(__name__)


# ── Public errors ────────────────────────────────────────────────────────────

class MilkUsageStableStoreError(RuntimeError):
    """Raised on any configuration / auth / I/O failure for this store.

    Wraps :class:`fabric_lakehouse_io.LakehouseIOError` so the page
    error path can surface a single domain-specific message rather than
    leaking generic OneLake stack traces.
    """


# ── Constants ────────────────────────────────────────────────────────────────

# Same lakehouse as ``fmmo_tracker.json`` by default. Change the
# secrets section name if you ever want this to live elsewhere.
_SECRETS_SECTION: str = "fabric_milk_usage_stable"

# Path inside the lakehouse Files/ folder. Lives under the same
# ``Milk_cost_tracker/`` subfolder as the FMMO tracker JSON and the
# append-only base_milk_cost_monthly_tracker CSV — see
# ``data_sources/milk_mover_store.py`` for the rationale.
_BLOB_PATH: str = "Milk_cost_tracker/Milk_Usage_Stable.csv"

# Streamlit-cache TTL for blob reads. The file is touched maybe once a
# month — caching for 5 minutes is a fine balance between freshness and
# avoiding redundant OneLake round-trips on rapid Streamlit reruns.
_READ_CACHE_TTL_SECONDS: int = 300

# Required column set — strict. Downstream
# ``_build_milk_usage_with_movers`` references each name by literal
# string, so a silent column-drift bug is much worse than a loud
# "missing column" error at read time. ``Protein Usage`` and
# ``Other Solids Usage`` were added in May-2026 for the Culture
# category (formerly "Cottage Cheese", renamed May-2026-late);
# legacy HTST/ESL items must carry ``0`` for both to preserve
# their existing cost.
_REQUIRED_COLUMNS: tuple[str, ...] = (
    "Item",
    "Item Description",
    "Class",
    "Category",
    "Skim Usage",
    "Butterfat Usage",
    "Protein Usage",
    "Other Solids Usage",
)

# Default seed CSV — used to bootstrap a fresh OneLake blob on first ever
# render against an empty lakehouse.
_DEFAULT_SEED_CSV: Path = (
    Path(__file__).resolve().parent.parent
    / "data" / "Market Barometer" / "Montly Movers" / "Milk_Usage_Stable.csv"
)


# ── Internal helpers ─────────────────────────────────────────────────────────

@st.cache_data(ttl=_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _read_csv_cached() -> Optional[pd.DataFrame]:
    """Cached fetch of the raw blob. Returns ``None`` when absent."""
    try:
        df, _etag = _io.read_csv(_SECRETS_SECTION, _BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise MilkUsageStableStoreError(str(exc)) from exc
    if df is None:
        return None
    return df


def _invalidate_read_cache() -> None:
    """Drop the cached frame after a write so the next read sees fresh data."""
    _read_csv_cached.clear()


def invalidate_read_cache() -> None:
    """Public alias of :func:`_invalidate_read_cache`.

    Pages call this after recovering from a Fabric auth failure so the
    cached "absent" answer (which can persist for up to
    ``_READ_CACHE_TTL_SECONDS``) is dropped before the next read.
    Mirrors the same-named helper on the milk-mover and resin stores
    for cross-store consistency.
    """
    _invalidate_read_cache()


def _validate_columns(df: pd.DataFrame) -> None:
    """Raise if the DataFrame is missing any of the required columns.

    Strictness rationale: every downstream consumer
    (``_build_milk_usage_with_movers`` and the lookups built on it)
    references these column names by literal string. A silent mismatch
    becomes a hard-to-diagnose "milk impact is always blank" bug — much
    nicer to fail loudly here.
    """
    missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        # Detailed, action-oriented error so the operator can fix in
        # one round-trip: lists missing columns, the canonical schema,
        # AND the columns we DID see — making rename / case-drift bugs
        # diagnosable from the message alone.
        raise MilkUsageStableStoreError(
            f"Milk_Usage_Stable is missing required columns {missing!r}. "
            f"Expected exactly: {list(_REQUIRED_COLUMNS)}. "
            f"Got columns: {list(df.columns)!r}. "
            "Re-upload the file with the canonical schema "
            "(HTST/ESL rows must use 0 for Protein Usage and "
            "Other Solids Usage; Culture rows carry the real "
            "per-item usage values)."
        )


# ── Public API ───────────────────────────────────────────────────────────────

def read_milk_usage_stable_df() -> pd.DataFrame:
    """Return the Milk_Usage_Stable table as a DataFrame.

    Returns an empty DataFrame (with the correct columns) when the blob
    is absent — callers handle that gracefully via the existing
    "no milk impact" branch in ``_build_milk_usage_with_movers``.
    """
    df = _read_csv_cached()
    if df is None or df.empty:
        return pd.DataFrame(columns=list(_REQUIRED_COLUMNS))
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    _validate_columns(df)
    return df


def seed_from_csv_if_empty(csv_path: Optional[Path] = None) -> int:
    """Bootstrap the OneLake blob from ``csv_path`` when absent.

    Idempotent and safe to call from a render path: when the blob
    already exists the function is a no-op (returns 0).

    Returns the number of rows seeded.
    """
    try:
        existing, _etag = _io.read_csv(_SECRETS_SECTION, _BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise MilkUsageStableStoreError(str(exc)) from exc
    if existing is not None:
        return 0  # never overwrite existing data — even if the seed CSV is newer

    csv = csv_path or _DEFAULT_SEED_CSV
    if not csv.exists():
        return 0

    df = pd.read_csv(csv)
    df.columns = [str(c).strip() for c in df.columns]
    _validate_columns(df)

    try:
        _io.write_csv(_SECRETS_SECTION, _BLOB_PATH, df, etag=None)
    except _io.LakehouseIOError as exc:
        raise MilkUsageStableStoreError(str(exc)) from exc

    _invalidate_read_cache()
    return len(df)


def write_milk_usage_stable_df(df: pd.DataFrame) -> None:
    """Replace the blob with ``df``. Intended for the New Price Quote
    "Upload to replace" UX in case Milk_Usage_Stable ever surfaces there.

    For now this is unused but kept here so the public API matches the
    other CSV-shaped stores in this folder, making it cheap to expose
    later without another round of refactoring.
    """
    _validate_columns(df)
    try:
        # Read current ETag so we can guard against concurrent writers.
        _, etag = _io.read_csv(_SECRETS_SECTION, _BLOB_PATH)
        _io.write_csv(_SECRETS_SECTION, _BLOB_PATH, df, etag=etag)
    except _io.LakehouseIOError as exc:
        raise MilkUsageStableStoreError(str(exc)) from exc
    _invalidate_read_cache()


def get_store_label() -> str:
    """Return a short human-readable label of where the data lives.

    Used in the upload panel and any "where does this come from?"
    captions on the page. Never raises — falls back to a generic
    string when secrets are missing so the caption always renders.
    """
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=_BLOB_PATH,
    ).display


__all__ = [
    "MilkUsageStableStoreError",
    "read_milk_usage_stable_df",
    "seed_from_csv_if_empty",
    "write_milk_usage_stable_df",
    "get_store_label",
]
