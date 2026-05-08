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


# ── Public API: writes ───────────────────────────────────────────────────────


def replace_one(role: str, df: pd.DataFrame) -> bool:
    """Overwrite a single CSV in the Monthly_Pricing_Execution folder.

    Parameters
    ----------
    role
        One of ``"rest_fg"``, ``"topco_fg"``, ``"milk_mover"``.
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
    "get_folder_label",
    "get_blob_label",
]
