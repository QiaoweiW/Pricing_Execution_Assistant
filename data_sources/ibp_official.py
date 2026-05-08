"""IBP Official connector — Microsoft Fabric Lakehouse Delta tables.

Reads the two IBP (Integrated Business Planning) Delta tables that back
the *Current Plan Overview* section on the Demand Planner Analytics page:

* ``dbo.IBP Orders``     — current-plan order book.
* ``dbo.IBP Shipments``  — current-plan shipments.

Both tables live in a single Fabric Lakehouse, identified by its workspace
+ lakehouse GUID pair (encoded in the Fabric portal URLs that prompted
this connector).  GUIDs are hard-coded as module-level constants because:

* They are stable across renames of the workspace / lakehouse display
  names (a recurring source of breakage on display-name URLs).
* They are public identifiers — they leak no data on their own and are
  identical for every Darigold employee.  Storing them in source rather
  than secrets keeps the connector zero-config for end users.
* The credentials needed to *read* the data are still supplied via
  azure-identity (interactive sign-in or service principal), so anyone
  without lakehouse Read access still cannot fetch rows.

Auth + read pipeline
--------------------
This connector now uses the SHARED auth + DuckDB plumbing in
:mod:`data_sources.fabric_auth` — the same credential and the same
DuckDB connection (with the ``azure`` + ``delta`` extensions
pre-loaded) that ``htst_shipment`` uses. Practical effects:

* A user who has already signed in for HTST Shipment / Milk Mover does
  not see a second browser sign-in when opening Demand Planner Analytics.
* The ``LOAD azure`` / ``LOAD delta`` cost is paid once per process
  rather than once per connector cold fetch.
* TLS / CA-bundle handling lives in one place (``fabric_auth``) instead
  of two slightly-different copies.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st

from data_sources.fabric_auth import (
    FabricAuthError,
    acquire_storage_token,
    bind_storage_token,
    duckdb_lock,
    get_duckdb_connection,
)


logger = logging.getLogger(__name__)


# ── Source identity (extracted from the Fabric portal URLs) ───────────────────
# https://app.fabric.microsoft.com/groups/<WORKSPACE_GUID>/lakehouses/<LAKEHOUSE_GUID>
#   ?...&selectedPath=dbo%2FIBP+Orders
#   ?...&selectedPath=dbo%2FIBP+Shipments
_WORKSPACE_GUID = "bb11c51d-03c8-4f1b-938c-e20657a8f31d"
_LAKEHOUSE_GUID = "a01f513d-eee7-41eb-8c15-670bc40e7fc8"
_SCHEMA = "dbo"

# Table NAMES contain a literal space (e.g. ``IBP Orders``).  OneLake supports
# this — the path segment must be URL-encoded when handed to delta-rs/DuckDB
# but ``abfss://`` parsers in both libraries percent-encode for us, so we
# keep the human-readable form here.
_TABLE_ORDERS = "IBP Orders"
_TABLE_SHIPMENTS = "IBP Shipments"

# 15-minute Streamlit cache TTL — same as htst_shipment.  Fabric dataflows
# typically refresh hourly or daily, so 15 min is conservative without
# being chatty.
_CACHE_TTL_SECONDS = 15 * 60


# ── Public types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IBPSnapshotMeta:
    """Identity of one IBP Delta-table snapshot, for caching + UI display."""

    table: str
    version: int
    last_modified: Optional[datetime]
    row_count: int
    source_uri: str

    @property
    def cache_key(self) -> str:
        """Stable string usable as a downstream cache signature."""
        return f"{self.table}:v{self.version}@{self.source_uri}"


class IBPOfficialSourceError(RuntimeError):
    """Raised on any failure to read an IBP Official Delta table.

    Wraps the underlying exception so the page can render a single, clean
    error path without leaking deltalake / azure-identity / DuckDB stack
    traces to end users.
    """


# ── URI building ──────────────────────────────────────────────────────────────

def _build_table_uri(table: str) -> str:
    """Construct the OneLake ``abfss://`` URI for a single IBP table.

    Uses the GUID form of the workspace + lakehouse path.  In the GUID
    form the ``.Lakehouse`` suffix is omitted and the path goes directly
    to ``Tables/<schema>/<table>``.  See the docstring of
    ``data_sources.htst_shipment._build_table_uri`` for the reasoning
    behind preferring GUIDs to display names.
    """
    return (
        f"abfss://{_WORKSPACE_GUID}@onelake.dfs.fabric.microsoft.com/"
        f"{_LAKEHOUSE_GUID}/Tables/{_SCHEMA}/{table}"
    )


# ── Credential acquisition ────────────────────────────────────────────────────

def _acquire_storage_token() -> str:
    """Acquire a bearer token for OneLake (Azure Storage scope).

    Thin wrapper around :func:`fabric_auth.acquire_storage_token` that
    translates :class:`FabricAuthError` into our domain-specific error.
    Uses the process-shared default cache name so a sign-in in any
    other Fabric-backed page also satisfies this connector.
    """
    try:
        return acquire_storage_token()
    except FabricAuthError as exc:
        raise IBPOfficialSourceError(str(exc)) from exc


# ── Core read path ────────────────────────────────────────────────────────────

def _read_delta_table(table_uri: str, token: str) -> tuple[pd.DataFrame, int, Optional[datetime]]:
    """Materialise a Fabric Lakehouse Delta table into an in-memory DataFrame.

    Returns ``(df, version, last_modified_utc)``.  See the long-form
    explanation in :func:`data_sources.htst_shipment._read_delta_table`
    for *why* the read goes through DuckDB rather than delta-rs's native
    ``to_pandas()``: Fabric writes Delta protocol reader v2 (with
    timestampNtz), which delta-rs rejects; DuckDB's delta extension uses
    the Databricks ``delta-kernel-rs`` crate and handles every Fabric
    protocol level cleanly.

    Uses the SHARED DuckDB connection (extensions pre-loaded) — see
    :func:`fabric_auth.get_duckdb_connection`.
    """
    # ── Best-effort metadata via delta-rs (non-fatal) ────────────────────────
    # The actual protocol-version check is performed inside delta-rs; we
    # tolerate failure here because the row data itself comes from DuckDB.
    version: int = -1
    last_modified: Optional[datetime] = None
    try:
        from deltalake import DeltaTable

        dt = DeltaTable(
            table_uri,
            storage_options={
                "bearer_token":        token,
                "use_fabric_endpoint": "true",
                "account_name":        "onelake",
            },
        )
        version = dt.version()
        history = dt.history(limit=1)
        if history:
            ts = history[0].get("timestamp")
            if isinstance(ts, (int, float)):
                last_modified = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
            elif isinstance(ts, str):
                last_modified = pd.to_datetime(ts, utc=True).to_pydatetime()
    except Exception as meta_exc:  # noqa: BLE001
        logger.info(
            "delta-rs could not open %s for metadata (often expected on "
            "Fabric tables at protocol v2) — proceeding via DuckDB.  "
            "Reason: %s",
            table_uri, meta_exc,
        )

    # ── Authoritative read via the shared DuckDB connection ────────────────
    try:
        con = get_duckdb_connection()
        with duckdb_lock():
            bind_storage_token(con, token)
            df = con.execute(f"SELECT * FROM delta_scan('{table_uri}')").df()
    except FabricAuthError as exc:
        raise IBPOfficialSourceError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise IBPOfficialSourceError(
            f"Could not read Delta table via DuckDB at {table_uri}.  "
            f"Verify (1) the workspace + lakehouse + table identifiers, "
            f"(2) your account has Read access to the lakehouse, and "
            f"(3) the dataflow refresh has actually populated the table.  "
            f"Underlying error: {exc}"
        ) from exc

    return df, version, last_modified


# ── Cached fetch helpers ──────────────────────────────────────────────────────

@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch(table_name: str, _cache_token: str) -> tuple[pd.DataFrame, IBPSnapshotMeta]:
    """Streamlit-cached fetch keyed by ``(table_name, opaque token)``.

    The leading-underscore arg name is the documented Streamlit
    convention for "include in the cache key but do not hash the
    contents".  Ideal for a manual cache-busting flag.
    """
    table_uri = _build_table_uri(table_name)
    token = _acquire_storage_token()
    df, version, last_modified = _read_delta_table(table_uri, token)
    meta = IBPSnapshotMeta(
        table=table_name,
        version=version,
        last_modified=last_modified,
        row_count=len(df),
        source_uri=table_uri,
    )
    logger.info(
        "Loaded IBP snapshot table=%s v%s (%s rows) from %s",
        table_name, version, len(df), table_uri,
    )
    return df, meta


def _fetch(table_name: str, *, force_refresh: bool) -> tuple[pd.DataFrame, IBPSnapshotMeta]:
    """Internal helper — fetch *table_name*, optionally bypassing the cache."""
    if force_refresh:
        # ``cache_data.clear()`` purges every cached entry of the
        # decorated function regardless of args.  That is the desired
        # behaviour for the IBP page's single "Refresh" button — both
        # tables are pulled together, so dropping both cached snapshots
        # at once keeps them in sync.
        _cached_fetch.clear()
    return _cached_fetch(table_name, "default")


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_ibp_orders_df(*, force_refresh: bool = False) -> tuple[pd.DataFrame, IBPSnapshotMeta]:
    """Return the latest snapshot of the ``dbo.IBP Orders`` Delta table.

    Parameters
    ----------
    force_refresh : bool, default False
        When True, clears the connector's Streamlit cache before reading.
        Wired to the page's "Refresh from Fabric" button.

    Raises
    ------
    IBPOfficialSourceError
        On any auth, configuration, or storage failure.  The page
        catches this and surfaces a single error block.
    """
    return _fetch(_TABLE_ORDERS, force_refresh=force_refresh)


def fetch_ibp_shipments_df(*, force_refresh: bool = False) -> tuple[pd.DataFrame, IBPSnapshotMeta]:
    """Return the latest snapshot of the ``dbo.IBP Shipments`` Delta table.

    Same contract as :func:`fetch_ibp_orders_df` — see that function's
    docstring for parameter and exception details.
    """
    return _fetch(_TABLE_SHIPMENTS, force_refresh=force_refresh)


__all__ = [
    "IBPSnapshotMeta",
    "IBPOfficialSourceError",
    "fetch_ibp_orders_df",
    "fetch_ibp_shipments_df",
]
