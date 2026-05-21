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
    caption.  The DataFrame is pre-filtered to ``PRODUCTGROUP == 'HTST'``
    via DuckDB predicate pushdown so callers do not pay the network +
    Parquet-decode cost for rows they are about to drop in pandas anyway.
    Streamlit-cached for 60 minutes; force_refresh=True bypasses the cache
    for the explicit "Refresh from Lakehouse" button.

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

Auth + DuckDB model
-------------------
Auth and the DuckDB Delta-scan engine are owned by
:mod:`data_sources.fabric_auth`. This module is a thin Delta-table
adapter that:

* Asks ``fabric_auth.acquire_storage_token`` for a bearer token (the
  process-shared MSAL cache means a sign-in for any other Fabric page
  also satisfies this one).
* Pulls the process-shared DuckDB connection from
  ``fabric_auth.get_duckdb_connection`` (extensions pre-loaded once
  per session) and runs ``delta_scan`` against the table URI.
* Owns the corporate-network TLS / CA-bundle plumbing the bundled
  libcurl needs (see :func:`_resolve_ca_cert_file` for the why).

Dependencies
------------
deltalake>=0.17     (binary wheel; ships its own Azure storage backend)
azure-identity>=1.15 (credential chain + token acquisition)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st

from data_sources.fabric_auth import (
    GUID_RE,
    FabricAuthError,
    acquire_storage_token,
    bind_storage_token,
    duckdb_lock,
    get_duckdb_connection,
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

# Fabric Dataflow Gen2 pipelines that feed the HTST lakehouse table refresh on
# a multi-hour cadence (typically once per business morning); a 60-minute cache
# strikes a much better network/freshness balance than the original 15-minute
# value, and the in-page "🔄 Refresh from Lakehouse" button always bypasses
# this cache for explicit reloads (force_refresh=True clears it).
_CACHE_TTL_SECONDS = 60 * 60

# Hard-coded PRODUCTGROUP value the page filters to.  Duplicated here (the
# canonical copy lives in pages/htst_activity_monitor_view.py:_PRODUCT_GROUP_FILTER)
# so the connector can push the row filter into DuckDB without growing an
# upward import dependency on pages/.  The page still applies its own filter
# after the read — which is now an idempotent no-op for healthy data and a
# safety net for the rare case where pushdown fell back to a full read.
_PRODUCT_GROUP_FILTER: str = "HTST"

# Timeout for the best-effort delta-rs metadata fetch.  Runs in parallel with
# the (much slower) DuckDB read, so this only adds visible latency if the data
# read finishes FIRST and we have to wait on the metadata thread — which only
# happens on an unusually fast Fabric pull or a stalled _delta_log probe.
_METADATA_TAIL_TIMEOUT_SECONDS: float = 0.5


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


def _acquire_storage_token() -> str:
    """Acquire a bearer token for Azure Storage / OneLake.

    Thin wrapper around :func:`fabric_auth.acquire_storage_token` that
    translates its :class:`FabricAuthError` into our domain-specific
    error so the page renders a clean, actionable message. Uses the
    process-shared ``DEFAULT_CACHE_NAME`` so this connector signs in
    only once across the whole app — see ``fabric_auth`` for the
    rationale.
    """
    try:
        return acquire_storage_token()
    except FabricAuthError as exc:
        raise HTSTShipmentSourceError(str(exc)) from exc


# ── TLS / CA-bundle plumbing for the bundled libcurl ─────────────────────────

def _resolve_ca_cert_file(cfg: dict[str, str]) -> Optional[str]:
    """Return a filesystem path to a CA bundle libcurl can use, or None.

    Resolution order (first hit wins):
      1. ``[fabric_htst].ca_cert_file`` from secrets — explicit override the
         operator can set to a corporate root-CA bundle (e.g. when Zscaler /
         Netskope / a forward-proxy is doing TLS inspection on
         ``*.fabric.microsoft.com``).
      2. ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE`` env vars — these are
         already the de-facto standard for Python TLS clients on locked-down
         corporate workstations, so we honor them transparently.
      3. ``certifi.where()`` — the Mozilla root-CA bundle ships with
         ``certifi`` (transitive dep of ``requests``), so it is virtually
         always available.  Sufficient for any non-MITM connection.

    Returning a real path lets the caller export ``CURL_CA_INFO`` /
    ``CURL_CA_BUNDLE`` *before* DuckDB's azure extension initialises libcurl.
    """
    explicit = cfg.get("ca_cert_file")
    if explicit and os.path.isfile(explicit):
        return explicit

    for env_name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        env_val = os.environ.get(env_name)
        if env_val and os.path.isfile(env_val):
            return env_val

    try:
        import certifi  # transitive dep of requests / azure-* — always present
    except ImportError:
        return None
    bundle = certifi.where()
    return bundle if os.path.isfile(bundle) else None


def _ssl_verify_enabled(cfg: dict[str, str]) -> bool:
    """Return True unless secrets opt out via ``ssl_verify = false``.

    Last-resort escape hatch for situations where (a) a corporate MITM
    proxy is in play, (b) the operator cannot get the corporate root CA
    file, and (c) the alternative is the page being completely unusable.
    Logged loudly so it is impossible to leave on by accident.
    """
    raw = str(cfg.get("ssl_verify", "true")).strip().lower()
    return raw not in ("false", "0", "no", "off")


# ── Core read path ────────────────────────────────────────────────────────────

@dataclass
class _MetadataResult:
    """Out-parameter for the background delta-rs metadata thread.

    Kept as a small dataclass instead of a tuple so the worker thread can
    write fields independently (version may succeed even when history
    parsing fails) without juggling thread-local state.
    """
    version: int = -1
    last_modified: Optional[datetime] = None


def _start_metadata_thread(table_uri: str, token: str) -> tuple[_MetadataResult, threading.Event]:
    """Kick off a daemon thread that fetches Delta version + commit timestamp.

    Returns the (result, done) pair so the main thread can decide how long
    to wait for completion.  The worker captures ``table_uri`` and ``token``
    by closure; both are short-lived strings the caller already has on
    hand.

    Why a background thread
    -----------------------
    Fabric Lakehouse tables are written at Delta protocol reader-version 2
    with the timestampNtz feature, which delta-rs explicitly rejects.  The
    rejection itself is fast in CPU terms but ``DeltaTable(...)`` still
    issues 2–3 small HTTP requests against ``_delta_log/`` before it can
    decide it cannot proceed — on a corporate-proxied network that's
    typically 1–3 seconds the user previously paid serially before the
    main data read could begin.

    The DuckDB read against the same Delta table also reads ``_delta_log``,
    but it is the dominant cost (seconds to tens of seconds for a multi-
    hundred-thousand-row table).  Running the metadata fetch in parallel
    with the DuckDB scan therefore hides the metadata round-trips behind
    the read entirely on the common path, while a short tail-wait timeout
    (see :data:`_METADATA_TAIL_TIMEOUT_SECONDS`) ensures we never extend
    the visible spinner waiting on a stalled metadata probe.
    """
    out = _MetadataResult()
    done = threading.Event()

    def _worker() -> None:
        t0 = time.monotonic()
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
            out.version = dt.version()
            history = dt.history(limit=1)
            if history:
                ts = history[0].get("timestamp")
                if isinstance(ts, (int, float)):
                    out.last_modified = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
                elif isinstance(ts, str):
                    out.last_modified = pd.to_datetime(ts, utc=True).to_pydatetime()
            logger.info(
                "delta-rs metadata for %s: v%s in %.2f s",
                table_uri, out.version, time.monotonic() - t0,
            )
        except Exception as meta_exc:  # noqa: BLE001 — intentionally broad; non-fatal path
            logger.info(
                "delta-rs could not open %s for metadata after %.2f s "
                "(often expected on Fabric tables at protocol v2) — "
                "proceeding without version/timestamp.  Reason: %s",
                table_uri, time.monotonic() - t0, meta_exc,
            )
        finally:
            done.set()

    threading.Thread(
        target=_worker,
        name="htst-shipment-metadata",
        daemon=True,
    ).start()
    return out, done


def _scan_htst_subset(con, table_uri: str) -> pd.DataFrame:
    """Read the HTST-filtered subset of *table_uri* via DuckDB's Delta extension.

    Pushes the ``PRODUCTGROUP == 'HTST'`` predicate into ``delta_scan`` so
    DuckDB never materialises the ~6/7 of rows the page is going to drop in
    pandas anyway.  The whitespace + case-insensitive form
    (``upper(trim(...))``) preserves the exact semantic of the historical
    pandas filter in :func:`pages.htst_activity_monitor_view._filter_to_htst`
    so the connector behaves identically on data that has stray casing or
    padding — a real concern with Fabric Dataflows that occasionally inherit
    raw values from upstream ERP exports.

    On any failure in the pushdown path (most plausibly a column-name
    mismatch — e.g., the lakehouse schema renames PRODUCTGROUP) we
    transparently fall back to ``SELECT *``.  The page's downstream
    :func:`_filter_to_htst` then applies the same filter in pandas, so the
    user observes only a perf regression, not a functional one.

    Caller is responsible for holding :func:`duckdb_lock` around the call.
    """
    # The literal value is hard-coded in this codebase (see
    # _PRODUCT_GROUP_FILTER), so the parameterized query is purely a
    # style/safety nicety; the URL itself is inlined because DuckDB's
    # delta_scan takes a literal path, not a parameter.
    pushdown_sql = (
        f"SELECT * FROM delta_scan('{table_uri}') "
        f"WHERE upper(trim(CAST(\"PRODUCTGROUP\" AS VARCHAR))) = ?"
    )
    t0 = time.monotonic()
    try:
        df = con.execute(pushdown_sql, [_PRODUCT_GROUP_FILTER]).df()
    except Exception as pushdown_exc:  # noqa: BLE001 — fall back, not error path
        logger.warning(
            "HTST predicate pushdown failed (%s) — falling back to "
            "SELECT * and applying the filter in pandas downstream.  "
            "Verify the Delta table exposes a 'PRODUCTGROUP' column.",
            pushdown_exc,
        )
        df = con.execute(f"SELECT * FROM delta_scan('{table_uri}')").df()
        logger.info(
            "DuckDB SELECT * delta_scan returned %d rows in %.2f s",
            len(df), time.monotonic() - t0,
        )
        return df

    logger.info(
        "DuckDB pushdown delta_scan returned %d HTST rows in %.2f s",
        len(df), time.monotonic() - t0,
    )
    return df


def _read_delta_table(table_uri: str, token: str, cfg: dict[str, str]) -> tuple[pd.DataFrame, int, Optional[datetime]]:
    """Materialise the HTST subset of *table_uri* into a pandas DataFrame.

    Returns (df, version, last_modified_utc).  The DataFrame is already
    filtered to ``PRODUCTGROUP == 'HTST'`` via predicate pushdown — callers
    that re-apply the page-side filter see it as a no-op.  Version and
    last_modified come from a parallel delta-rs metadata probe; both
    degrade gracefully (-1 / None) when delta-rs cannot read the table.

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
    1. A daemon thread starts a delta-rs metadata probe in parallel — its
       round-trips to ``_delta_log/`` overlap with the DuckDB scan instead
       of stacking serially in front of it (see :func:`_start_metadata_thread`).
    2. The process-shared DuckDB connection (``fabric_auth.get_duckdb_connection``)
       has the ``azure`` and ``delta`` extensions already loaded — we no
       longer pay ``LOAD azure`` / ``LOAD delta`` (~300 ms) per fetch.
    3. The bearer token is rebound on every call via
       :func:`bind_storage_token` (``CREATE OR REPLACE SECRET``), so the
       connection always sees a fresh, non-expired token.
    4. DuckDB's delta extension does delta_scan → arrow → pandas in a single
       SQL query, with the HTST filter pushed into the scan (see
       :func:`_scan_htst_subset`).
    5. After the scan returns, we wait up to
       :data:`_METADATA_TAIL_TIMEOUT_SECONDS` for the metadata thread.
       The metadata is informational (powering the caption); we refuse to
       extend the visible spinner waiting on it.

    Errors anywhere in the chain are wrapped in HTSTShipmentSourceError so
    the page renders one clean message instead of leaking SQL/Rust traces.
    """
    # ── Kick off delta-rs metadata in parallel with the data read ──────────
    meta, meta_done = _start_metadata_thread(table_uri, token)

    # ── Pre-flight: TLS / CA bundle plumbing for the bundled libcurl ────────
    # DuckDB's azure extension statically links the Azure SDK for C++ which
    # uses libcurl as one of its transport adapters.  That bundled libcurl
    # has no notion of the Windows certificate store and on Linux it looks
    # for the CA bundle at the RHEL path (/etc/pki/tls/certs/ca-bundle.crt)
    # which doesn't exist on Debian/Ubuntu.  When the bundle isn't found,
    # the TLS handshake to onelake.blob.fabric.microsoft.com fails with
    # 'Problem with the SSL CA cert (path? access rights?)' — which has
    # nothing to do with the bearer token, the workspace identifiers, or
    # network reachability.  See https://github.com/duckdb/duckdb_azure/issues/8
    #
    # Fix: resolve a real CA-bundle path via _resolve_ca_cert_file()
    # (operator override → standard env vars → certifi) and export the
    # libcurl-recognised env vars BEFORE the extension loads, then force
    # the curl transport so the same code path is exercised on every OS.
    ca_cert_file = _resolve_ca_cert_file(cfg)
    ssl_verify = _ssl_verify_enabled(cfg)
    if ca_cert_file:
        os.environ.setdefault("CURL_CA_INFO", ca_cert_file)
        os.environ.setdefault("CURL_CA_BUNDLE", ca_cert_file)
        os.environ.setdefault("SSL_CERT_FILE", ca_cert_file)

    # ── Authoritative read via the shared DuckDB connection ────────────────
    if not ssl_verify:
        # Loud, opt-in only.  Logged at WARNING because the operator
        # explicitly asked for it via secrets.toml.
        logger.warning(
            "TLS certificate verification is DISABLED for OneLake reads "
            "([fabric_htst].ssl_verify = false).  This is a corporate-"
            "MITM workaround — restore verification by removing the flag "
            "(or setting it to true) and providing a proper "
            "[fabric_htst].ca_cert_file path."
        )

    scan_start = time.monotonic()
    try:
        con = get_duckdb_connection()
        with duckdb_lock():
            bind_storage_token(con, token, ssl_verify=ssl_verify)
            df = _scan_htst_subset(con, table_uri)
    except FabricAuthError as exc:
        raise HTSTShipmentSourceError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        ssl_hint = ""
        if "SSL CA cert" in msg or "certificate" in msg.lower() or "ssl" in msg.lower():
            ssl_hint = (
                "\n\nThis looks like a TLS / CA-certificate failure inside "
                "DuckDB's bundled libcurl, NOT a problem with your bearer "
                "token, workspace identifiers, or lakehouse permissions.  "
                "Two ways to fix it:\n"
                "  (a) [PREFERRED] Point the connector at a CA bundle that "
                "your environment trusts.  Add to .streamlit/secrets.toml "
                "under [fabric_htst]:\n"
                "        ca_cert_file = \"C:/path/to/your/corporate_ca.pem\"\n"
                "      If your machine has no MITM proxy, the certifi bundle "
                "(shipped with Python's 'requests' package) is auto-detected "
                "— the most likely root cause is then a corporate firewall "
                "rewriting *.fabric.microsoft.com certificates.\n"
                "  (b) [LAST RESORT] Disable TLS verification by adding to "
                "[fabric_htst]:\n"
                "        ssl_verify = false\n"
                "      Insecure — only use on a trusted network and revert "
                "as soon as you have a proper CA bundle."
            )
        raise HTSTShipmentSourceError(
            f"Could not read Delta table via DuckDB at {table_uri}.  "
            f"Verify (1) the workspace + lakehouse + table identifiers, "
            f"(2) your account has Read access to the lakehouse, and "
            f"(3) the dataflow refresh has actually populated the table.  "
            f"Underlying error: {exc}{ssl_hint}"
        ) from exc

    scan_elapsed = time.monotonic() - scan_start

    # ── Wait briefly for the metadata thread, but don't block forever ───────
    # If delta-rs is being slow today (rare; corporate-proxy / TLS pathology),
    # we'd rather show "Delta version v-1 / unknown" in the caption than
    # extend the visible spinner.  The thread is daemon so it dies with the
    # process if it doesn't finish — its result fields stay at their defaults.
    if not meta_done.wait(timeout=_METADATA_TAIL_TIMEOUT_SECONDS):
        logger.info(
            "delta-rs metadata still running %.2fs after DuckDB scan finished "
            "(scan took %.2fs); proceeding with v-1 / unknown timestamp.",
            _METADATA_TAIL_TIMEOUT_SECONDS, scan_elapsed,
        )

    return df, meta.version, meta.last_modified


# ── Public API ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch(_cache_token: str) -> tuple[pd.DataFrame, SnapshotMeta]:
    """Streamlit-cached fetch keyed by an opaque token.

    The leading-underscore arg name is the documented Streamlit convention
    for "include in cache key but do not hash the contents" — perfect for
    a string token whose only job is to bust the cache when force_refresh
    is True.

    Each invocation is bookended by wall-clock timing logs so operators
    can diagnose cold-start latency (token acquisition + Delta scan)
    independently of the hot path; cache hits never reach this function at
    all, so its mere appearance in the log is a cache miss.
    """
    fetch_start = time.monotonic()
    cfg = _read_config()
    promote_sp_secrets_to_env(cfg)
    table_uri = _build_table_uri(
        cfg["workspace"],
        cfg["lakehouse"],
        cfg["table"],
        schema=cfg.get("schema"),
    )

    auth_start = time.monotonic()
    token = _acquire_storage_token()
    auth_elapsed = time.monotonic() - auth_start

    df, version, last_modified = _read_delta_table(table_uri, token, cfg)
    meta = SnapshotMeta(
        version=version,
        last_modified=last_modified,
        row_count=len(df),
        source_uri=table_uri,
    )
    logger.info(
        "Loaded HTST Shipment snapshot v%s (%s rows) from %s "
        "(auth %.2fs, total %.2fs)",
        version, len(df), table_uri,
        auth_elapsed, time.monotonic() - fetch_start,
    )
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
