"""
OneLake-backed publisher for the **VBCS_refrehable** drop-zone.

Every successful "Run … Generation" click on the Pricing Execution
Automation page now publishes the produced VBCS CSV files to a single
canonical OneLake folder so the downstream Price Book Distribution
workflow can resolve the "New Price" for any item/site/UOM combination
by enumerating every CSV in that folder.

Storage layout
--------------
``Files/Monthly_Pricing_Execution/VBCS_refrehable/<filename>.csv``

The four VBCS tools (Fixed, KS, Variable, Combine) each emit one or
more named CSVs (e.g. ``urm_vbcs.csv``, ``winco_vbcs.csv``,
``batch_vbcs.csv``, ``fixed_vbcs.csv``, ``ks_htst_vbcs.csv``,
``combined_vbcs.csv``).  This store treats those filenames as the
authoritative key — the latest publish for a given filename overwrites
the prior copy.  We deliberately do NOT version-stamp the filenames:
the user's contract is "always overwrite the old copy", which keeps
the New-Price lookup deterministic (one row per ``(item, site, uom,
month)`` per file).

Public API
----------
* :func:`publish_one`     — push a single CSV (overwrite-on-success).
* :func:`publish_many`    — push every CSV in a ``{filename: df}`` map.
* :func:`list_files`      — enumerate every CSV currently in the folder.
* :func:`read_one`        — read one CSV by filename (used by Price Book).
* :func:`read_all`        — read every CSV (cached) — used by the Price
                            Book lookup loop.
* :func:`invalidate_read_cache` — drop the local read cache after a
                                  publish or on demand.
* :func:`get_folder_label`/:func:`get_blob_path` — short labels for UI.

Why a dedicated store (vs. extending ``monthly_pricing_execution_store``)?
-------------------------------------------------------------------------
The existing store binds four FIXED roles to four FIXED blob paths
(``rest_htst_resin_mover_fg.csv``, etc.).  The VBCS drop-zone is
unbounded in cardinality — any tool can drop a new filename and the
Price Book lookup must enumerate the live set.  Folding both contracts
into one module would force ``replace_one`` to accept either a known
role OR an arbitrary filename, which is exactly the kind of
overloaded interface that grows bugs.  A separate module keeps each
store's contract honest.

Semantics
---------
"Publish" is an unconditional overwrite — same as
:func:`monthly_pricing_execution_store.replace_one`.  Empty / ``None``
frames are a silent no-op so a partial pipeline (e.g. Variable Pricing
generated no Winco rows that month) doesn't blow away a prior good
copy.
"""
from __future__ import annotations

import logging
from typing import Iterable, Mapping, Optional

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io


logger = logging.getLogger(__name__)


class VbcsRefrehableStoreError(RuntimeError):
    """Raised by any public function on a lakehouse I/O error."""


# Re-use the same Fabric Lakehouse secrets section as the existing
# Monthly_Pricing_Execution store — the folder is rooted under the same
# lakehouse, so a single set of credentials covers both stores.
_SECRETS_SECTION: str = "fabric_monthly_pricing_execution"

# Folder layout: ``Files/Monthly_Pricing_Execution/VBCS_refrehable/``.
# Keeping the typo ``refrehable`` matches the user-facing folder name in
# OneLake exactly — renaming the folder would force a one-time data
# migration with zero functional benefit.
_FOLDER_PREFIX: str = "Monthly_Pricing_Execution/VBCS_refrehable"

# Streamlit-cache TTL for the per-filename CSV reads.  60 s is enough
# to absorb a Refresh-bursts on the Distribute Price Book screen while
# still picking up out-of-band edits within one minute.
_READ_CACHE_TTL_SECONDS: int = 60


def _blob_path_for(file_name: str) -> str:
    """Return the canonical blob path for ``file_name`` (no leading slash).

    Forbids slashes in ``file_name`` because we never publish nested
    sub-folders here — every CSV lives directly under
    ``VBCS_refrehable/``.  An accidental ``a/b.csv`` would silently
    create a sub-folder that ``list_files`` (non-recursive) would
    never see, so we fail loudly instead.
    """
    leaf = file_name.strip()
    if not leaf:
        raise VbcsRefrehableStoreError("file_name must be a non-empty string")
    if "/" in leaf or "\\" in leaf:
        raise VbcsRefrehableStoreError(
            f"file_name {leaf!r} contains a path separator; "
            "publish_one expects a leaf filename only."
        )
    if not leaf.lower().endswith(".csv"):
        # Tolerate missing extension — but normalise so the lakehouse
        # blob always carries the ``.csv`` suffix, matching the other
        # CSVs in the folder.
        leaf = f"{leaf}.csv"
    return f"{_FOLDER_PREFIX}/{leaf}"


# ── Public API: writes ───────────────────────────────────────────────────────


def publish_one(file_name: str, df: Optional[pd.DataFrame]) -> bool:
    """Overwrite ``file_name`` in the VBCS_refrehable folder.

    Parameters
    ----------
    file_name
        Leaf filename (``urm_vbcs.csv``, ``fixed_vbcs.csv``, etc.).
        The ``.csv`` suffix is auto-appended if missing.
    df
        DataFrame to publish.  Empty / ``None`` frames are a silent
        no-op so partial pipeline runs don't accidentally clobber a
        good prior copy.

    Returns
    -------
    True when bytes were written, False when the call was skipped
    (empty DataFrame).
    """
    if df is None or df.empty:
        return False
    blob_path = _blob_path_for(file_name)
    try:
        # ``etag=None`` means create-or-overwrite unconditionally — the
        # whole point of this store is to publish authoritative
        # replacements, mirroring monthly_pricing_execution_store.
        _io.write_csv(_SECRETS_SECTION, blob_path, df, etag=None)
    except _io.LakehouseIOError as exc:
        raise VbcsRefrehableStoreError(str(exc)) from exc
    invalidate_read_cache()
    return True


def publish_many(files: Mapping[str, Optional[pd.DataFrame]]) -> dict[str, bool]:
    """Publish every supplied DataFrame to its canonical blob path.

    Parameters
    ----------
    files
        ``{filename: DataFrame}`` map.  Filenames without ``.csv`` get
        the suffix appended automatically.

    Returns
    -------
    ``{filename: bool}`` indicating whether each file's bytes were
    written.  Missing / skipped files map to ``False``.  An exception
    on any single file aborts the loop — earlier successful writes
    are NOT rolled back (each blob is independently authoritative).
    """
    result: dict[str, bool] = {}
    for file_name, df in files.items():
        result[file_name] = publish_one(file_name, df)
    return result


# ── Public API: reads ────────────────────────────────────────────────────────


@st.cache_data(ttl=_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _read_one_cached(blob_path: str) -> Optional[pd.DataFrame]:
    """Cached fetch of one CSV.  Cleared by every write helper.

    Returns ``None`` when the blob is absent (cold-bootstrap state).
    """
    try:
        df, _etag = _io.read_csv(_SECRETS_SECTION, blob_path)
    except _io.LakehouseIOError as exc:
        # Surface the error to the caller; the page treats it as a
        # missing-file condition and falls back to a blank New Price.
        raise VbcsRefrehableStoreError(str(exc)) from exc
    return df


def read_one(file_name: str) -> Optional[pd.DataFrame]:
    """Return the CSV body for ``file_name`` or ``None`` when absent."""
    return _read_one_cached(_blob_path_for(file_name))


def list_files() -> list[_io.LakehouseFile]:
    """Return every CSV currently in the VBCS_refrehable folder.

    Non-recursive enumeration — sub-folders (if any) are ignored on
    purpose (see :func:`_blob_path_for` for the publish-side check
    that forbids creating them).
    """
    try:
        return _io.list_files(
            _SECRETS_SECTION, _FOLDER_PREFIX, suffix=".csv",
        )
    except _io.LakehouseIOError as exc:
        raise VbcsRefrehableStoreError(str(exc)) from exc


def read_all() -> list[tuple[str, pd.DataFrame]]:
    """Return ``[(filename, df), …]`` for every CSV in the folder.

    Used by the Distribute Price Book "New Price" lookup loop to scan
    every published VBCS file for a matching ``(item, site, uom,
    month)`` tuple.  Files that read back empty are skipped so the
    caller never has to special-case zero-row CSVs.
    """
    out: list[tuple[str, pd.DataFrame]] = []
    for entry in list_files():
        try:
            df, _etag = _io.read_csv(_SECRETS_SECTION, entry.full_path)
        except _io.LakehouseIOError as exc:
            # Non-fatal — surface to the logger and keep walking.  A
            # corrupt VBCS file shouldn't block the Price Book from
            # answering "no match" on its rows.
            logger.warning(
                "Could not read VBCS file %s: %s — skipping", entry.full_path, exc,
            )
            continue
        if df is None or df.empty:
            continue
        out.append((entry.name, df))
    return out


def invalidate_read_cache() -> None:
    """Drop the local per-file read cache.

    Called from :func:`publish_one` so any same-session reader sees
    the freshly-published bytes.  Operators can also call this from a
    "Reload from lakehouse" button if one is ever added.
    """
    _read_one_cached.clear()


# ── Public API: labels for the UI ────────────────────────────────────────────


def get_folder_label() -> str:
    """Short caption for the folder, suitable for ``st.caption``."""
    return f"OneLake folder: `Files/{_FOLDER_PREFIX}/`"


def get_blob_path(file_name: str) -> str:
    """Return the full ``Files/...`` path for ``file_name`` (for captions)."""
    return f"Files/{_blob_path_for(file_name)}"


__all__ = [
    "VbcsRefrehableStoreError",
    "publish_one",
    "publish_many",
    "list_files",
    "read_one",
    "read_all",
    "invalidate_read_cache",
    "get_folder_label",
    "get_blob_path",
]
