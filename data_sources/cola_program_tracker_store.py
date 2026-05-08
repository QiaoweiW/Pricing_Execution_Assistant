"""
OneLake-backed store for the **Annual COLA Program Tracker**.

Single-blob CSV store living at::

    Files/Monthly_Pricing_Execution/COLA_Program_Tracker.csv

Read on every render of the Market Barometer's *Annual COLA Movers*
section so the user always sees the latest committed table; overwritten
authoritatively when the user clicks **Refresh** to push their edits.

Why a dedicated module?
-----------------------
The Cost-Of-Living-Adjustment workflow is conceptually independent
from the resin / milk / freight pipelines that share
``monthly_pricing_execution_store``: that store is *write-only* (it
publishes pipeline outputs), while this one is read-modify-write
(operators edit the table and push it back).  Keeping the read path
out of the publisher avoids accidentally tying the COLA cache
invalidation to every Monthly-Movers Refresh.

We deliberately reuse the ``[fabric_monthly_pricing_execution]``
secrets section so deployments don't need to duplicate workspace /
lakehouse config — both stores read from the same OneLake folder.

Public API
----------
* :func:`read_table`              → live ``pd.DataFrame`` (or empty when blob absent)
* :func:`replace_table`           → overwrite the blob (no ETag guard)
* :func:`invalidate_read_cache`   → drop ``@st.cache_data`` + per-session bypass
* :func:`get_blob_label`          → display string for status captions
* :data:`COLA_PROGRAM_TRACKER_BLOB_PATH`
* :class:`ColaProgramTrackerStoreError`
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io


logger = logging.getLogger(__name__)


# ── Public errors ────────────────────────────────────────────────────────────


class ColaProgramTrackerStoreError(RuntimeError):
    """Raised on any configuration / auth / I/O failure for this store.

    Wraps :class:`fabric_lakehouse_io.LakehouseIOError` so the page
    renders a single clean error banner instead of leaking SDK or
    Streamlit stack traces.
    """


# ── Constants ────────────────────────────────────────────────────────────────

# Same secrets section as ``monthly_pricing_execution_store`` — the
# COLA blob shares the workspace / lakehouse config with every other
# Monthly_Pricing_Execution artefact.
_SECRETS_SECTION: str = "fabric_monthly_pricing_execution"

# Canonical blob path (matches the OneLake URL the user shared in the
# May-2026-late spec).
COLA_PROGRAM_TRACKER_BLOB_PATH: str = (
    "Monthly_Pricing_Execution/COLA_Program_Tracker.csv"
)

# Per-session "we already bypassed an empty cached result once" flag.
# Mirrors the pattern in ``resin_cost_tracker_store`` and
# ``milk_mover_store`` — without it a freshly-uploaded OneLake file
# would be invisible for up to 5 minutes after a stale @st.cache_data
# pinning, and a genuinely-empty store would re-hit OneLake on every
# rerun (wasteful).
_SS_BYPASS_KEY: str = "cola_program_tracker_store_bypass_done"

# Cache TTL on the read path.  Short enough that a manual edit committed
# from another browser tab shows up within a few minutes; long enough
# that a single page render doesn't pound OneLake.
_READ_CACHE_TTL_SECONDS: int = 5 * 60


# ── Internal cached read path ────────────────────────────────────────────────


@st.cache_data(ttl=_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _read_csv_cached() -> Optional[pd.DataFrame]:
    """OneLake-backed read of the COLA tracker blob.

    Returns ``None`` when the blob does not exist (cold-start), an
    empty DataFrame when it exists but contains no rows (rare —
    operator deleted every row but kept the file), or the parsed
    DataFrame otherwise.

    NOTE: Streamlit's ``@st.cache_data`` caches BOTH happy and empty
    results.  Callers therefore go through :func:`_read_with_fallback`
    so a transiently-empty answer doesn't pin them out of fresh data
    for the full TTL — see :data:`_SS_BYPASS_KEY` for the rationale.
    """
    try:
        df, _etag = _io.read_csv(_SECRETS_SECTION, COLA_PROGRAM_TRACKER_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        # Re-raise as the domain error so the page renders a clean
        # banner without leaking the SDK message stack.
        raise ColaProgramTrackerStoreError(
            f"OneLake read failed for COLA_Program_Tracker: {exc}"
        ) from exc
    return df


def _read_with_fallback() -> Optional[pd.DataFrame]:
    """Cached read with a one-shot per-session bypass on empty results.

    First call in a session returns whatever Streamlit has cached.  If
    that is ``None`` or empty, we drop the cache exactly once per
    session (gated by :data:`_SS_BYPASS_KEY`) and re-read OneLake
    directly so a freshly-uploaded blob shows up immediately.  All
    subsequent empty cache hits short-circuit and return the cached
    answer to avoid OneLake retry storms in a genuinely empty state.
    """
    df = _read_csv_cached()
    if df is not None and not df.empty:
        return df
    if st.session_state.get(_SS_BYPASS_KEY):
        return df
    st.session_state[_SS_BYPASS_KEY] = True
    _read_csv_cached.clear()
    return _read_csv_cached()


# ── Public API: reads ────────────────────────────────────────────────────────


def read_table() -> pd.DataFrame:
    """Return the live COLA Program Tracker as a DataFrame.

    Returns an empty DataFrame (with no columns) when the blob does
    not yet exist — this lets the UI render an empty editor that
    seeds the file on first save.

    Raises
    ------
    ColaProgramTrackerStoreError
        On any configuration / auth / I/O failure.  The page catches
        this and renders a clean error banner.
    """
    df = _read_with_fallback()
    if df is None:
        return pd.DataFrame()
    return df


# ── Public API: writes ───────────────────────────────────────────────────────


def replace_table(df: pd.DataFrame) -> None:
    """Overwrite the COLA tracker blob authoritatively.

    No ETag guard: the editor that drives this is single-tenant by
    design (only the Pricing team uses it) and the user's intent is
    "publish exactly what's on screen".  An ETag-conflict retry path
    here would silently merge concurrent edits from a second user,
    which is worse UX than the current "last-write-wins" semantic.

    Empty DataFrames are written verbatim — that's how the operator
    legitimately clears the table when a program ends.

    Side effect: invalidates the read cache so the very next render
    (or the very next ``read_table`` call inside the same request)
    sees the freshly-published rows.

    Raises
    ------
    ColaProgramTrackerStoreError
        On any configuration / auth / I/O failure.
    """
    try:
        _io.write_csv(
            _SECRETS_SECTION,
            COLA_PROGRAM_TRACKER_BLOB_PATH,
            df,
            etag=None,
        )
    except _io.LakehouseIOError as exc:
        raise ColaProgramTrackerStoreError(
            f"OneLake write failed for COLA_Program_Tracker: {exc}"
        ) from exc
    invalidate_read_cache()


# ── Public API: cache invalidation ───────────────────────────────────────────


def invalidate_read_cache() -> None:
    """Drop the @st.cache_data result AND the per-session bypass flag.

    Mirrors the pattern in ``resin_cost_tracker_store`` and
    ``milk_mover_store`` so callers (e.g. ``_recover_after_fabric_signin``
    in pages) can sweep every Fabric-backed cache with a single
    consistent call.
    """
    try:
        _read_csv_cached.clear()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass
    st.session_state.pop(_SS_BYPASS_KEY, None)


# ── Public API: identity captions ────────────────────────────────────────────


def get_blob_label() -> str:
    """Short human-readable label for the COLA tracker blob (UI captions)."""
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=COLA_PROGRAM_TRACKER_BLOB_PATH,
    ).display


__all__ = [
    "ColaProgramTrackerStoreError",
    "COLA_PROGRAM_TRACKER_BLOB_PATH",
    "read_table",
    "replace_table",
    "invalidate_read_cache",
    "get_blob_label",
]
