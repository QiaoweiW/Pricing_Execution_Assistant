"""
OneLake-backed publisher for the **Monthly Pricing Execution** drop-zone.

Every successful "Refresh" click on the Market Barometer's Monthly Movers
section now publishes the four driver CSVs the downstream pricing
pipelines consume to a single canonical OneLake folder:

* ``rest_htst_resin_mover_fg.csv``  — Rest HTST Resin Mover FG output.
* ``topco_resin_mover_fg.csv``      — TOPCO Resin Mover FG output.
* ``milk_mover.csv``                — slicer-driven Milk Usage with movers.
* ``Movers_Non_Milk_Tracker.csv``   — the editable Movers Non-Milk
  tracker as-of-Refresh, so downstream auditors / pipelines see the
  exact mover values that drove the published FG outputs.

Storage layout
--------------
``Files/Monthly_Pricing_Execution/<filename>.csv``

Public API
----------
* :data:`REST_FG_BLOB_PATH`, :data:`TOPCO_FG_BLOB_PATH`,
  :data:`MILK_MOVER_BLOB_PATH`, :data:`MOVERS_NON_MILK_TRACKER_BLOB_PATH`
  — canonical paths.
* :func:`replace_files`                — push every CSV in one call.
* :func:`replace_one`                  — push a single CSV (lower-level).
* :func:`read_movers_non_milk_tracker_df` — read-back of the editable
                                           tracker (used by the page on
                                           cold start so the in-UI table
                                           reflects whatever the last
                                           successful Refresh published).
* :func:`invalidate_read_cache`        — drop the local read cache; the
                                         publish path calls this after
                                         every successful write so a
                                         same-session re-read sees the
                                         freshly-published bytes.
* :func:`get_folder_label`             — short caption for the UI.
* :func:`get_blob_label`               — per-file caption for the UI.

Semantics
---------
"Refresh" is an authoritative replace operation: callers always have the
latest in-memory frame and the lakehouse copy should reflect that frame
*as-of-now*.  We therefore unconditionally overwrite the blob (no ETag
guard) — there's no read-modify-write contract to defend against here.

Idempotent: re-clicking Refresh with the same data simply rewrites
identical bytes.  Empty / missing frames are silently skipped so a
partial Refresh (e.g. milk slicer not yet selected) doesn't blow away a
prior good copy.

Read-back
---------
``Movers_Non_Milk_Tracker.csv`` is dual-purpose: the page publishes it
on every Refresh AND seeds the in-memory editable tracker from it on
cold start.  :func:`read_movers_non_milk_tracker_df` is the
read-side entry point.  A small ``@st.cache_data`` TTL guards repeated
calls inside one session; :func:`invalidate_read_cache` (called from
:func:`replace_one` after a successful write) keeps reads consistent
with the just-published bytes.

Configuration
-------------
Reads ``workspace`` and ``lakehouse`` from
``[fabric_monthly_pricing_execution]`` when present, falling back to
``[fabric_htst]`` so deployments don't need to duplicate config when
every artefact lives in the same Pricing_Lakehouse.
"""
from __future__ import annotations

import logging
from typing import Iterable, Mapping, Optional

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io


logger = logging.getLogger(__name__)


# ── Public errors ────────────────────────────────────────────────────────────


class MonthlyPricingExecutionStoreError(RuntimeError):
    """Raised on configuration / auth / I/O failure for this store.

    Wraps :class:`fabric_lakehouse_io.LakehouseIOError` so the page
    renders a single clean error path instead of leaking SDK / Streamlit
    stack traces.
    """


# ── Constants ────────────────────────────────────────────────────────────────

# Single secrets-section name with [fabric_htst] inheritance.  Every blob
# in this folder shares one config — see ``fabric_lakehouse_io._read_lakehouse_config``.
_SECRETS_SECTION: str = "fabric_monthly_pricing_execution"

# Canonical folder under Files/.  The full path matches the OneLake URL
# the user shared in the May-2026 spec.
_FOLDER_PREFIX: str = "Monthly_Pricing_Execution"

REST_FG_BLOB_PATH:                  str = f"{_FOLDER_PREFIX}/rest_htst_resin_mover_fg.csv"
TOPCO_FG_BLOB_PATH:                 str = f"{_FOLDER_PREFIX}/topco_resin_mover_fg.csv"
MILK_MOVER_BLOB_PATH:               str = f"{_FOLDER_PREFIX}/milk_mover.csv"
MOVERS_NON_MILK_TRACKER_BLOB_PATH:  str = f"{_FOLDER_PREFIX}/Movers_Non_Milk_Tracker.csv"

# Logical role → blob path lookup.  Lets callers say
# ``replace_files({"rest_fg": df_rest, "topco_fg": df_topco, ...})``
# without having to know the exact filename of each artefact.
_ROLE_TO_BLOB: Mapping[str, str] = {
    "rest_fg":                 REST_FG_BLOB_PATH,
    "topco_fg":                TOPCO_FG_BLOB_PATH,
    "milk_mover":              MILK_MOVER_BLOB_PATH,
    "movers_non_milk_tracker": MOVERS_NON_MILK_TRACKER_BLOB_PATH,
}

# Order roles are written in.  Stable ordering keeps the user-facing
# success caption deterministic across runs and makes log scraping easy.
# Tracker last so downstream readers see the FG outputs first when a
# polling job lands on the folder mid-publish.
_REPLACE_ORDER: tuple[str, ...] = (
    "rest_fg",
    "topco_fg",
    "milk_mover",
    "movers_non_milk_tracker",
)


# ── Internal: cached read ────────────────────────────────────────────────────

# Streamlit-cache TTL for blob reads.  Five minutes is generous: out-of-band
# updates to Movers_Non_Milk_Tracker.csv are rare (the page is the canonical
# writer); the publish path :func:`replace_one` invalidates the cache
# immediately so same-session reads after Refresh always see fresh bytes.
_READ_CACHE_TTL_SECONDS: int = 300


@st.cache_data(ttl=_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _read_csv_cached(blob_path: str) -> Optional[pd.DataFrame]:
    """Cached fetch of one CSV blob.  Returns ``None`` when absent.

    Wraps any underlying ``LakehouseIOError`` into the public store
    error type so callers render one clean error path.
    """
    try:
        df, _etag = _io.read_csv(_SECRETS_SECTION, blob_path)
    except _io.LakehouseIOError as exc:
        raise MonthlyPricingExecutionStoreError(str(exc)) from exc
    return df


def invalidate_read_cache() -> None:
    """Drop the cached read frames so the next read hits OneLake directly.

    Called from :func:`replace_one` after a successful write so a
    same-session re-read of the just-published blob returns the new
    bytes instead of a stale cached copy.  Also exposed as a public
    helper so pages can force a re-pull on demand (e.g. an explicit
    "Reload from lakehouse" button).
    """
    _read_csv_cached.clear()


# ── Public API: writes ───────────────────────────────────────────────────────


def replace_one(role: str, df: pd.DataFrame) -> bool:
    """Overwrite a single CSV in the Monthly_Pricing_Execution folder.

    Parameters
    ----------
    role
        One of ``"rest_fg"``, ``"topco_fg"``, ``"milk_mover"``,
        ``"movers_non_milk_tracker"``.
    df
        DataFrame to publish.  Empty / ``None`` frames are a silent
        no-op so partial pipeline runs don't accidentally clobber a
        good prior copy.

    Returns
    -------
    True when bytes were written, False when the call was skipped
    (empty DataFrame, unknown role, etc.).
    """
    if df is None or df.empty:
        return False
    blob_path = _ROLE_TO_BLOB.get(role)
    if blob_path is None:
        raise MonthlyPricingExecutionStoreError(
            f"Unknown role {role!r}.  Expected one of "
            f"{sorted(_ROLE_TO_BLOB)!r}."
        )
    try:
        # ``etag=None`` means create-or-overwrite unconditionally — the
        # whole point of this store is to publish authoritative replacements.
        _io.write_csv(_SECRETS_SECTION, blob_path, df, etag=None)
    except _io.LakehouseIOError as exc:
        raise MonthlyPricingExecutionStoreError(str(exc)) from exc
    # Invalidate the local read cache so any same-session reader sees
    # the freshly-published bytes (matters for the editable Movers
    # Non-Milk Tracker — its seed reads from this store on cold start).
    invalidate_read_cache()
    return True


def replace_files(
    files: Mapping[str, Optional[pd.DataFrame]],
    *,
    only: Optional[Iterable[str]] = None,
) -> dict[str, bool]:
    """Publish every supplied DataFrame to its canonical blob path.

    Parameters
    ----------
    files
        ``{role: DataFrame}`` map.  Roles not in :data:`_ROLE_TO_BLOB`
        are ignored (forward-compat: extra keys in ``files`` are
        tolerated so callers can pass through their full ``outputs``
        dict without filtering).
    only
        Optional iterable of role names.  When set, only those roles
        are pushed even if other roles are present in ``files``.

    Returns
    -------
    ``{role: bool}`` indicating whether each role's bytes were written.
    Missing / skipped roles map to ``False``.

    Raises
    ------
    MonthlyPricingExecutionStoreError
        On any underlying lakehouse I/O failure.  The exception fires
        on the FIRST failing role; earlier successful writes are NOT
        rolled back (each blob is independently authoritative).
    """
    allowed = set(only) if only is not None else None
    result: dict[str, bool] = {role: False for role in _ROLE_TO_BLOB}
    for role in _REPLACE_ORDER:
        if allowed is not None and role not in allowed:
            continue
        df = files.get(role)
        result[role] = replace_one(role, df)
    return result


# ── Public API: reads ────────────────────────────────────────────────────────


def read_movers_non_milk_tracker_df() -> Optional[pd.DataFrame]:
    """Return the lakehouse copy of ``Movers_Non_Milk_Tracker.csv`` or ``None``.

    Used by the Market Barometer page to seed its in-memory editable
    tracker on cold start so the UI reflects the last successful
    Refresh.  Returns ``None`` when the blob is absent (cold-bootstrap
    deployments) or the read backing-store raises a recoverable error
    we've already logged — callers fall back to the hard-coded seed in
    that case.

    No coercion is performed here — the page applies its own header
    migration / dtype coercion so this reader stays a pure "give me the
    bytes" entry point.
    """
    try:
        df = _read_csv_cached(MOVERS_NON_MILK_TRACKER_BLOB_PATH)
    except MonthlyPricingExecutionStoreError as exc:
        # I/O failures are logged so the operator can debug, but we
        # return ``None`` instead of bubbling — the page falls back to
        # the hard-coded seed and continues to render.  This matches
        # the resilience model used by the resin store readers.
        logger.warning(
            "read_movers_non_milk_tracker_df: %s — falling back.", exc,
        )
        return None
    if df is None or df.empty:
        return None
    return df


# ── Public API: identity captions ────────────────────────────────────────────


def get_folder_label() -> str:
    """Short human-readable label for the destination folder."""
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=f"{_FOLDER_PREFIX}/",
    ).display


def get_blob_label(role: str) -> str:
    """Short human-readable label for a single blob in this store."""
    blob_path = _ROLE_TO_BLOB.get(role)
    if blob_path is None:
        raise MonthlyPricingExecutionStoreError(
            f"Unknown role {role!r}.  Expected one of "
            f"{sorted(_ROLE_TO_BLOB)!r}."
        )
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=blob_path,
    ).display


__all__ = [
    "MonthlyPricingExecutionStoreError",
    "REST_FG_BLOB_PATH",
    "TOPCO_FG_BLOB_PATH",
    "MILK_MOVER_BLOB_PATH",
    "MOVERS_NON_MILK_TRACKER_BLOB_PATH",
    "replace_one",
    "replace_files",
    "read_movers_non_milk_tracker_df",
    "invalidate_read_cache",
    "get_folder_label",
    "get_blob_label",
]
