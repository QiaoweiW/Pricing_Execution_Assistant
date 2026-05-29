"""Demand Summary connector — Microsoft Fabric Lakehouse Files I/O.

Reads the two CSV files that back the *Demand Summary* section on the
Demand Planner Analytics page:

* ``Files/RO Tracking/Demand Plan/qry_mgmt_plan_full.csv``
* ``Files/RO Tracking/Demand Plan/qry_total_item_level_demand.csv``

Both blobs live in the same OneLake lakehouse used by every other
RO Tracking surface (see ``ro_comparison._SECRETS_SECTION``).  We
piggyback on the shared ``[fabric_htst]`` secrets block — no per-feature
credentials, no separate sign-in — so any user already authenticated for
RO Comparison / RO Summary Report can read these tables with zero extra
latency.

Public surface
--------------
* :class:`DemandSummaryError`       — domain-specific exception.
* :class:`DemandSummarySnapshot`    — value object: ``(df, etag, size,
                                       last_modified, blob_path)``.
* :func:`fetch_mgmt_plan_full`     — full ``qry_mgmt_plan_full.csv``.
* :func:`fetch_total_item_level_demand` — full
                                       ``qry_total_item_level_demand.csv``.
* :func:`clear_demand_summary_cache` — single-call cache invalidation
                                       (wired to the section's "Refresh"
                                       button).

Cache model
-----------
Both fetchers are wrapped in ``@st.cache_data`` with a 15-minute TTL —
identical to the RO Comparison / IBP cadence so the planner has one
mental model for "how fresh is the data on this page".  Cache keys are
trivial sentinel strings (no signature) because there is exactly one
canonical blob per file; the public wrappers accept ``force_refresh`` to
bypass.

Errors
------
Underlying :class:`LakehouseIOError` is wrapped into
:class:`DemandSummaryError` so the page can render one consistent error
banner without leaking storage-SDK diagnostics into the section body.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st

from data_sources.fabric_lakehouse_io import (
    LakehouseIOError,
    get_file_properties,
    read_bytes,
    read_csv,
)


logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────────
#
# We piggyback on the ``[fabric_htst]`` secrets block — same pattern as
# every other RO Tracking connector (see ``ro_comparison.py``).  The
# block must provide ``workspace`` and ``lakehouse`` (display names or
# GUIDs).  See ``fabric_lakehouse_io._read_lakehouse_config`` for the
# inheritance rules.
_SECRETS_SECTION: str = "fabric_htst"

# Source blob paths under ``Files/`` — POSIX-style, no leading slash.
# Hard-coded as module-level constants because they are the canonical
# locations on the Fabric portal, not user-configurable inputs.
_MGMT_PLAN_FULL_BLOB_PATH: str = (
    "RO Tracking/Demand Plan/qry_mgmt_plan_full.csv"
)
_TOTAL_ITEM_LEVEL_DEMAND_BLOB_PATH: str = (
    "RO Tracking/Demand Plan/qry_total_item_level_demand.csv"
)

# 15-minute Streamlit cache TTL.  Mirrors the RO Comparison / IBP /
# Summary Report cadence so the whole Demand Planner Analytics page has
# one consistent freshness window.
_CACHE_TTL_SECONDS: int = 15 * 60


# ── Public types ─────────────────────────────────────────────────────────────

class DemandSummaryError(RuntimeError):
    """Raised on any Demand Summary I/O or parse failure.

    Wraps the lower-level :class:`LakehouseIOError` so the page renders
    a single, scope-aware banner without leaking the storage SDK's
    chain-of-exceptions into the section body.
    """


@dataclass(frozen=True)
class DemandSummarySnapshot:
    """Identity + payload for a single Demand Summary CSV snapshot.

    Attributes
    ----------
    df
        The parsed DataFrame.  Never ``None`` — an empty CSV degrades
        to ``pd.DataFrame()`` (callers can branch on ``df.empty``).
    etag
        Fabric ETag of the blob at read time.  ``None`` if the storage
        SDK didn't surface one (rare).
    size
        Blob size in bytes (best-effort, ``None`` if unavailable).
    last_modified
        Best-effort UTC timestamp of the most recent Fabric write.
        ``None`` if the storage SDK didn't surface one.
    blob_path
        POSIX path under ``Files/`` so the UI can echo "Source:
        Files/RO Tracking/Demand Plan/qry_mgmt_plan_full.csv".
    """
    df: pd.DataFrame
    etag: Optional[str]
    size: Optional[int]
    last_modified: Optional[datetime]
    blob_path: str

    @property
    def row_count(self) -> int:
        """Convenience for the UI caption — never raises on empty frames."""
        return int(len(self.df))

    @property
    def column_count(self) -> int:
        """Convenience for the UI caption."""
        return int(len(self.df.columns))


# ── Internal cached readers ──────────────────────────────────────────────────
#
# We split each public ``fetch_*`` from its ``@st.cache_data`` impl so
# that the public wrapper can:
#   1. Accept a ``force_refresh=True`` flag without spilling the
#      Streamlit-specific ``.clear()`` API onto callers.
#   2. Always return a strongly-typed :class:`DemandSummarySnapshot`.
#
# The cached impl's ``_signature`` argument is the documented Streamlit
# pattern for an explicit cache key — it participates in cache identity
# but its contents are never hashed by us.

@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch(blob_path: str, _signature: str) -> DemandSummarySnapshot:
    """Cached read of a single Demand Summary CSV blob.

    Centralised so both top-level fetchers share one implementation —
    add a new file by introducing a single thin wrapper, not a parallel
    cached function.  Raises :class:`DemandSummaryError` on any
    underlying read / parse failure.
    """
    # 1. Lightweight metadata (etag, size, last_modified) for the UI
    #    caption.  Failure here is non-fatal — we fall through to the
    #    body read with empty metadata rather than blocking the page.
    last_modified: Optional[datetime] = None
    size: Optional[int] = None
    try:
        props = get_file_properties(_SECRETS_SECTION, blob_path)
    except LakehouseIOError as exc:
        # Properties-fetch is a "nice to have" header pull; if it
        # blows up we still try the body read and surface a useful
        # error there if needed.
        logger.info(
            "get_file_properties failed for 'Files/%s' (non-fatal): %s",
            blob_path, exc,
        )
        props = None

    if props is not None:
        size = props.size
        if props.last_modified:
            # ``last_modified`` is stored as a string for portability
            # across SDK versions; parse defensively here so the
            # snapshot exposes a real ``datetime`` to the UI.
            try:
                last_modified = pd.to_datetime(
                    props.last_modified, utc=True,
                ).to_pydatetime()
            except (TypeError, ValueError) as exc:
                logger.info(
                    "Could not parse last_modified=%r for 'Files/%s' "
                    "(non-fatal): %s",
                    props.last_modified, blob_path, exc,
                )

    # 2. Authoritative body read.  Any I/O failure surfaces as a
    #    domain-specific error so the page can render one clean banner.
    try:
        df, etag = read_csv(_SECRETS_SECTION, blob_path)
    except LakehouseIOError as exc:
        raise DemandSummaryError(
            f"Could not read 'Files/{blob_path}' from Microsoft Fabric: {exc}"
        ) from exc

    if df is None:
        raise DemandSummaryError(
            f"OneLake blob 'Files/{blob_path}' does not exist.  Verify "
            "that the upstream pipeline has published the file and that "
            "your account has Read access to the lakehouse."
        )

    logger.info(
        "Loaded Demand Summary CSV 'Files/%s': %s rows, %s columns.",
        blob_path, len(df), len(df.columns),
    )

    return DemandSummarySnapshot(
        df=df,
        etag=etag,
        size=size,
        # ``last_modified`` may be UTC-aware from the SDK; we keep it
        # as-is — UI converts to a display string when needed.
        last_modified=last_modified.astimezone(timezone.utc) if last_modified else None,
        blob_path=blob_path,
    )


# ── Public fetch helpers ─────────────────────────────────────────────────────

def fetch_mgmt_plan_full(*, force_refresh: bool = False) -> DemandSummarySnapshot:
    """Return the latest ``qry_mgmt_plan_full.csv`` as a snapshot.

    Parameters
    ----------
    force_refresh
        When True, clears this connector's cache slot before reading so
        the next call hits Fabric.  Wire this to a "Refresh from Fabric"
        button in the UI.
    """
    if force_refresh:
        _cached_fetch.clear()
    return _cached_fetch(_MGMT_PLAN_FULL_BLOB_PATH, "default")


def fetch_total_item_level_demand(
    *, force_refresh: bool = False,
) -> DemandSummarySnapshot:
    """Return the latest ``qry_total_item_level_demand.csv`` as a snapshot.

    See :func:`fetch_mgmt_plan_full` for the ``force_refresh`` contract.
    """
    if force_refresh:
        _cached_fetch.clear()
    return _cached_fetch(_TOTAL_ITEM_LEVEL_DEMAND_BLOB_PATH, "default")


# ── Raw-bytes download path ──────────────────────────────────────────────────
#
# The CSV the planner downloads from this section must be a byte-for-
# byte copy of what is sitting in OneLake — NOT a re-serialisation of
# our parsed DataFrame.  Re-serialising would drop trailing newlines,
# coerce numeric types, rewrite quoting, etc., and the planner's
# downstream tools (Excel pivot tables) are unforgiving about those
# changes.  The fetchers above return a parsed frame for preview /
# inspection; downloads go through this raw path instead.

def fetch_raw_bytes(blob_path: str) -> bytes:
    """Return the raw bytes of a Demand Summary blob, untouched.

    Raises :class:`DemandSummaryError` when the blob is missing or the
    storage SDK errors out.  Used by the Streamlit download buttons so
    the user gets an unmodified copy of the source CSV.
    """
    try:
        raw, _etag = read_bytes(_SECRETS_SECTION, blob_path)
    except LakehouseIOError as exc:
        raise DemandSummaryError(
            f"Could not download 'Files/{blob_path}' from Microsoft Fabric: "
            f"{exc}"
        ) from exc
    if raw is None:
        raise DemandSummaryError(
            f"OneLake blob 'Files/{blob_path}' does not exist."
        )
    return raw


def mgmt_plan_full_blob_path() -> str:
    """Return the POSIX path of the management-plan-full CSV under ``Files/``."""
    return _MGMT_PLAN_FULL_BLOB_PATH


def total_item_level_demand_blob_path() -> str:
    """Return the POSIX path of the total-item-level-demand CSV under ``Files/``."""
    return _TOTAL_ITEM_LEVEL_DEMAND_BLOB_PATH


# ── Cache management ─────────────────────────────────────────────────────────

def clear_demand_summary_cache() -> None:
    """Invalidate the cached snapshots for BOTH Demand Summary CSVs.

    Wired to the section's "🔄 Refresh from Fabric" button so a single
    click forces fresh reads of both files on the next render.  Exposed
    as a public function (rather than reaching into the cached impl from
    the page) so the page doesn't need to know about Streamlit's
    ``.clear()`` decorator API.
    """
    _cached_fetch.clear()


__all__ = [
    "DemandSummaryError",
    "DemandSummarySnapshot",
    "fetch_mgmt_plan_full",
    "fetch_total_item_level_demand",
    "fetch_raw_bytes",
    "mgmt_plan_full_blob_path",
    "total_item_level_demand_blob_path",
    "clear_demand_summary_cache",
]
