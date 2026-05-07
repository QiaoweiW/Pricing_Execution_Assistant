"""HTST Shipment Report connector — Microsoft Fabric Dataflow Gen2 / OneLake.

The HTST Activity Monitor previously consumed a user-uploaded "HTST Shipment
Report_<date>.csv" file.  This module replaces that input by reading the same
data from a Fabric Lakehouse Delta table — the destination of the
"HTST_Shipment_Report" Dataflow Gen2 — over OneLake's abfss endpoint.

Public API
----------
fetch_htst_shipment_df(force_refresh=False) -> tuple[DataFrame, SnapshotMeta]
    Returns the latest Delta-table snapshot as an in-memory DataFrame, along
    with metadata (Delta version, last-modified UTC timestamp, row count,
    source URI) used by the page for cache-keying and the "data as of"
    caption.  Streamlit-cached for 15 minutes; force_refresh=True bypasses
    the cache for the explicit "Refresh from Dataflow" button.

Configuration (read from st.secrets["fabric_htst"])
---------------------------------------------------
Required:
  workspace      — Fabric workspace NAME or GUID (both accepted by OneLake)
  lakehouse      — Fabric lakehouse NAME without ".Lakehouse" suffix
  table          — Delta table name (default: "htst_shipment")

Optional:
  schema         — Lakehouse schema name (e.g. "dbo") for schema-enabled
                   lakehouses.  When omitted the path is Tables/<table>;
                   when present the path becomes Tables/<schema>/<table>.

Optional (only needed if you have a service principal):
  tenant_id      — Entra tenant GUID
  client_id      — Service principal application (client) ID
  client_secret  — Service principal client secret VALUE

Auth model
----------
Uses a small ChainedTokenCredential built on azure-identity:
  1. EnvironmentCredential          — picks up AZURE_TENANT_ID / AZURE_CLIENT_ID /
                                      AZURE_CLIENT_SECRET if exported.  Populated
                                      from secrets.toml when the optional SP keys
                                      above are present (the production path).
  2. InteractiveBrowserCredential   — opens a browser the first time, with
                                      persistent token caching to disk.  After
                                      the initial sign-in the cached refresh
                                      token is used silently for ~90 days.
                                      No admin rights, no installs — works on
                                      locked-down corporate workstations.

The token is acquired for the Azure Storage scope (which OneLake honors) and
passed to delta-rs via the `bearer_token` storage option.

The in-memory credential is cached via @st.cache_resource so it survives
Streamlit's page-rerun cycle and the browser only opens once per session.
The on-disk MSAL cache then survives across server restarts.

Dependencies
------------
deltalake>=0.17     (binary wheel; ships its own Azure storage backend)
azure-identity>=1.15 (credential chain + token acquisition)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd
import streamlit as st

from data_sources.fabric_auth import (
    GUID_RE,
    FabricAuthError,
    acquire_storage_token,
    promote_sp_secrets_to_env,
    read_section,
)


# deltalake is imported lazily inside _read_delta_table() so that this module
# can still be imported (and the page can render its error UI) even if the
# package is not yet installed in the environment.

logger = logging.getLogger(__name__)


# ── Public types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SnapshotMeta:
    """Identity of one Delta-table snapshot, used for caching and UI display.

    Attributes
    ----------
    version       : Delta-log commit version (monotonically increasing int).
    last_modified : UTC timestamp of the commit that produced this version.
    row_count     : Number of rows in the materialised DataFrame.
    source_uri    : abfss:// URI the table was read from (for diagnostics).
    """
    version: int
    last_modified: Optional[datetime]
    row_count: int
    source_uri: str

    @property
    def cache_key(self) -> str:
        """Stable string suitable for use as a downstream cache signature."""
        return f"v{self.version}@{self.source_uri}"


class HTSTShipmentSourceError(RuntimeError):
    """Raised on any failure to read the HTST Shipment Delta table.

    Wraps the underlying exception so the page can render a single, clean
    error path without leaking deltalake / azure-identity stack traces.
    """


# ── Configuration ─────────────────────────────────────────────────────────────

_DEFAULT_TABLE = "htst_shipment"
_CACHE_TTL_SECONDS = 15 * 60  # 15 minutes — balances freshness vs Fabric API load


def _read_config() -> dict[str, str]:
    """Pull and validate the Fabric secrets block for HTST.

    Thin wrapper around :func:`fabric_auth.read_section` that translates
    its :class:`FabricAuthError` into our domain-specific error so the
    page renders a clean message in the existing error path.
    """
    try:
        return read_section(
            "fabric_htst",
            required=("workspace", "lakehouse"),
            defaults={"table": _DEFAULT_TABLE},
        )
    except FabricAuthError as exc:
        raise HTSTShipmentSourceError(str(exc)) from exc


def _build_table_uri(workspace: str, lakehouse: str, table: str, schema: Optional[str] = None) -> str:
    """Construct the OneLake abfss URI for a Lakehouse Delta table.

    OneLake exposes lakehouse tables under TWO equivalent URL conventions:

      * Display-name form:
          abfss://<workspace_name>@onelake.dfs.fabric.microsoft.com/
                 <lakehouse_name>.Lakehouse/Tables[/<schema>]/<table>

      * GUID form (canonical, what Fabric uses internally):
          abfss://<workspace_guid>@onelake.dfs.fabric.microsoft.com/
                 <lakehouse_guid>/Tables[/<schema>]/<table>

    Two important differences:
      1. The ".Lakehouse" suffix is REQUIRED for the display-name form and
         MUST NOT be present for the GUID form.
      2. Display-name URLs with embedded spaces (e.g. "B2C Pricing") are
         passed verbatim by delta-rs and parsed inconsistently by some
         downstream Azure libraries — GUID form is immune to this entire
         class of bug.

    GUIDs are therefore the recommended identifiers in secrets.toml; this
    function transparently supports either by regex-detecting the form.

    Schema-enabled lakehouses (a newer Fabric feature, default for fresh
    lakehouses created in 2025+) place tables under a schema namespace such
    as "dbo".  Older lakehouses store tables flat directly under Tables/.
    """
    lakehouse_part = lakehouse if GUID_RE.match(lakehouse) else f"{lakehouse}.Lakehouse"
    table_path = f"Tables/{schema}/{table}" if schema else f"Tables/{table}"
    return (
        f"abfss://{workspace}@onelake.dfs.fabric.microsoft.com/"
        f"{lakehouse_part}/{table_path}"
    )


_TOKEN_CACHE_NAME = "streamlit_htst_shipment"


def _acquire_storage_token() -> str:
    """Acquire a bearer token for Azure Storage / OneLake.

    Thin wrapper around :func:`fabric_auth.acquire_storage_token` that
    translates its :class:`FabricAuthError` into our domain-specific
    error so the page renders a clean, actionable message.
    """
    try:
        return acquire_storage_token(_TOKEN_CACHE_NAME)
    except FabricAuthError as exc:
        raise HTSTShipmentSourceError(str(exc)) from exc


# ── Core read path ────────────────────────────────────────────────────────────

def _read_delta_table(table_uri: str, token: str) -> tuple[pd.DataFrame, int, Optional[datetime]]:
    """Materialise the Delta table at *table_uri* into a pandas DataFrame.

    Returns (df, version, last_modified_utc).  Version is taken from delta-rs
    when it can open the table; last_modified_utc may be None when the
    history is unreadable (does not block the read).

    Why DuckDB and not delta-rs's to_pandas()
    -----------------------------------------
    Microsoft Fabric writes Lakehouse Delta tables at protocol reader-version
    2 with the timestampNtz feature.  delta-rs (the engine behind the
    `deltalake` Python package) only handles reader-version 1 or 3 — v2 is a
    transitional protocol level it explicitly rejects.  DuckDB's Delta
    extension uses a separate Rust crate (delta-kernel-rs) that ships from
    Databricks and handles all Fabric protocol variants cleanly.

    Read path
    ---------
    1. duckdb's azure extension is configured with the pre-acquired bearer
       token via a CREATE SECRET statement (PROVIDER ACCESS_TOKEN).
    2. duckdb's delta extension does delta_scan() → arrow → pandas in a
       single SQL query.  Column projection / filter pushdown can be added
       later by parameterising the SELECT.
    3. delta-rs is still used opportunistically for metadata (version,
       commit timestamp).  When delta-rs cannot open the table at all (e.g.
       protocol v2 reject), we fall back to version=-1 and last_modified=None
       — the data is still returned, only the audit caption degrades.

    Errors anywhere in the chain are wrapped in HTSTShipmentSourceError so
    the page renders one clean message instead of leaking SQL/Rust traces.
    """
    try:
        import duckdb  # noqa: WPS433  (lazy import keeps module import cheap)
    except ImportError as exc:
        raise HTSTShipmentSourceError(
            "Python package 'duckdb' is not installed.  Run: "
            "pip install -r requirements.txt"
        ) from exc

    # ── Best-effort metadata via delta-rs (non-fatal) ────────────────────────
    # Fabric tables are at reader v2 which delta-rs rejects.  We still try
    # because (a) the rejection is fast and (b) future delta-rs releases or
    # tables created by other tools may succeed — and the version + commit
    # timestamp are useful in the UI when available.
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
            "delta-rs could not open %s for metadata (often expected on Fabric "
            "tables at protocol v2) — proceeding via DuckDB.  Reason: %s",
            table_uri, meta_exc,
        )

    # ── Authoritative read via DuckDB delta_scan ────────────────────────────
    try:
        con = duckdb.connect(":memory:")
        # Extensions auto-install on first use; subsequent runs hit the cache
        # in %USERPROFILE%\.duckdb\extensions and load in <50ms.
        con.execute("INSTALL azure")
        con.execute("LOAD azure")
        con.execute("INSTALL delta")
        con.execute("LOAD delta")
        # Stash the bearer token in a duckdb SECRET so it never appears in
        # the SQL log of subsequent queries.  CREATE OR REPLACE makes this
        # idempotent across reruns.
        con.execute(
            "CREATE OR REPLACE SECRET onelake_token ("
            "TYPE AZURE, PROVIDER ACCESS_TOKEN, "
            f"ACCESS_TOKEN '{token}', ACCOUNT_NAME 'onelake')"
        )
        df = con.execute(f"SELECT * FROM delta_scan('{table_uri}')").df()
    except Exception as exc:  # noqa: BLE001
        raise HTSTShipmentSourceError(
            f"Could not read Delta table via DuckDB at {table_uri}.  "
            f"Verify (1) the workspace + lakehouse + table identifiers, "
            f"(2) your account has Read access to the lakehouse, and "
            f"(3) the dataflow refresh has actually populated the table.  "
            f"Underlying error: {exc}"
        ) from exc

    return df, version, last_modified


# ── Public API ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch(_cache_token: str) -> tuple[pd.DataFrame, SnapshotMeta]:
    """Streamlit-cached fetch keyed by an opaque token.

    The leading-underscore arg name is the documented Streamlit convention
    for "include in cache key but do not hash the contents" — perfect for
    a string token whose only job is to bust the cache when force_refresh
    is True.
    """
    cfg = _read_config()
    promote_sp_secrets_to_env(cfg)
    table_uri = _build_table_uri(
        cfg["workspace"],
        cfg["lakehouse"],
        cfg["table"],
        schema=cfg.get("schema"),
    )
    token = _acquire_storage_token()
    df, version, last_modified = _read_delta_table(table_uri, token)
    meta = SnapshotMeta(
        version=version,
        last_modified=last_modified,
        row_count=len(df),
        source_uri=table_uri,
    )
    logger.info("Loaded HTST Shipment snapshot v%s (%s rows) from %s", version, len(df), table_uri)
    return df, meta


def fetch_htst_shipment_df(*, force_refresh: bool = False) -> tuple[pd.DataFrame, SnapshotMeta]:
    """Return the latest HTST Shipment snapshot from the Fabric dataflow.

    Parameters
    ----------
    force_refresh : bool, default False
        When True, clears the Streamlit cache for this connector before
        reading.  Wired to the "Refresh from Dataflow" button on the page.

    Returns
    -------
    (df, meta) : tuple[pd.DataFrame, SnapshotMeta]

    Raises
    ------
    HTSTShipmentSourceError
        On any configuration, auth, or storage failure.  The page catches
        this and surfaces a single error block + the manual-upload fallback.
    """
    if force_refresh:
        _cached_fetch.clear()
    # The cache token is "default" in steady state; the cache is invalidated
    # via .clear() rather than via key rotation, so a constant token is fine.
    return _cached_fetch("default")


__all__ = ["SnapshotMeta", "HTSTShipmentSourceError", "fetch_htst_shipment_df"]
