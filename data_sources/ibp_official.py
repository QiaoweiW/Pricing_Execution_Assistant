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

The auth + read pipeline mirrors :mod:`data_sources.htst_shipment` —
ChainedTokenCredential → DuckDB ``delta_scan`` over OneLake — so the
two modules share the same operational semantics, the same failure
modes, and the same on-disk MSAL token cache (sign in once, both
connectors stay authenticated).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st


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

# OneLake honors any token issued for the Azure Storage scope.
_STORAGE_SCOPE = "https://storage.azure.com/.default"

# Persisted MSAL cache name.  Distinct from the htst_shipment cache so
# that signing out of one connector does not invalidate the other —
# they share the same underlying refresh tokens at the OS level but
# have independent cache slots.
_TOKEN_CACHE_NAME = "streamlit_ibp_official"


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

@st.cache_resource(show_spinner=False)
def _get_credential():
    """Build a credential chain reused across every fetch in a session.

    Order of precedence (first that succeeds wins):

    1. ``EnvironmentCredential`` — picks up ``AZURE_TENANT_ID`` /
       ``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET`` if they are set in
       the process environment (the headless service-principal path).
    2. ``InteractiveBrowserCredential`` — opens a browser the first
       time, with persistent token caching to ``%LOCALAPPDATA%`` so
       subsequent runs are silent for ~90 days.

    Cached at module level via ``@st.cache_resource`` so the
    ``ChainedTokenCredential`` survives Streamlit reruns inside a
    session; on-disk persistence handles cross-restart survival.
    """
    try:
        from azure.identity import (
            ChainedTokenCredential,
            EnvironmentCredential,
            InteractiveBrowserCredential,
            TokenCachePersistenceOptions,
        )
    except ImportError as exc:
        raise IBPOfficialSourceError(
            "Python package 'azure-identity' is not installed.  Run: "
            "pip install azure-identity"
        ) from exc

    persistence = TokenCachePersistenceOptions(
        name=_TOKEN_CACHE_NAME,
        # Required on locked-down corporate workstations that lack a
        # managed keyring — the cache file is stored under the user
        # profile and needs no admin rights.
        allow_unencrypted_storage=True,
    )

    return ChainedTokenCredential(
        EnvironmentCredential(),
        InteractiveBrowserCredential(
            cache_persistence_options=persistence,
        ),
    )


def _acquire_storage_token() -> str:
    """Acquire a bearer token for OneLake (Azure Storage scope).

    First call typically triggers a browser sign-in; subsequent calls are
    silent.  Wraps any underlying credential error in our typed exception
    so the page renders a clean, actionable message.
    """
    credential = _get_credential()
    try:
        return credential.get_token(_STORAGE_SCOPE).token
    except Exception as exc:  # noqa: BLE001
        raise IBPOfficialSourceError(
            "Could not acquire an Azure Storage token for OneLake.  "
            "If a sign-in window opened, complete it and reload this page.  "
            "If no window appeared, your browser may have blocked the popup "
            "or your account is restricted from interactive auth.  "
            f"Underlying error: {exc}"
        ) from exc


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
    """
    try:
        import duckdb  # noqa: WPS433  (lazy import keeps module import cheap)
    except ImportError as exc:
        raise IBPOfficialSourceError(
            "Python package 'duckdb' is not installed.  Run: "
            "pip install -r requirements.txt"
        ) from exc

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

    # ── Authoritative read via DuckDB delta_scan ────────────────────────────
    try:
        con = duckdb.connect(":memory:")
        # Extensions auto-install on first use; subsequent runs hit the
        # local extension cache and load in <50ms.
        con.execute("INSTALL azure")
        con.execute("LOAD azure")
        con.execute("INSTALL delta")
        con.execute("LOAD delta")
        # Stash the bearer token in a duckdb SECRET so it never appears
        # in the SQL log of subsequent queries.  CREATE OR REPLACE makes
        # this idempotent across reruns.
        con.execute(
            "CREATE OR REPLACE SECRET onelake_token ("
            "TYPE AZURE, PROVIDER ACCESS_TOKEN, "
            f"ACCESS_TOKEN '{token}', ACCOUNT_NAME 'onelake')"
        )
        df = con.execute(f"SELECT * FROM delta_scan('{table_uri}')").df()
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
