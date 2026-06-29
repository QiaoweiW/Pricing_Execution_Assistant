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
from datetime import date, datetime, timezone
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

# What the cache stores + why it is a tuple of NATIVE values
# -----------------------------------------------------------
# ``st.cache_data`` pickles every cached return value.  Returning a
# custom class such as :class:`IBPSnapshotMeta` makes pickle look the
# class up by qualified name on the way back in — and when Streamlit's
# file-watcher has re-executed this module (a normal occurrence during
# development and multi-page navigation) the *re-defined* class is "not
# the same object" as the one the cached instance references, so pickle
# raises and the whole fetch fails (``UnserializableReturnValueError``).
# Caching only DataFrames + plain scalars sidesteps that entirely;
# :func:`_fetch` rebuilds the :class:`IBPSnapshotMeta` outside the cache.
@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch(
    table_name: str, _cache_token: str,
) -> tuple[pd.DataFrame, int, Optional[datetime], int, str]:
    """Streamlit-cached fetch keyed by ``(table_name, opaque token)``.

    Returns native values — ``(df, version, last_modified, row_count,
    source_uri)`` — never the :class:`IBPSnapshotMeta` class (see the
    note above).  :func:`_fetch` wraps these back into the meta object.

    The leading-underscore arg name is the documented Streamlit
    convention for "include in the cache key but do not hash the
    contents".  Ideal for a manual cache-busting flag.
    """
    table_uri = _build_table_uri(table_name)
    token = _acquire_storage_token()
    df, version, last_modified = _read_delta_table(table_uri, token)
    logger.info(
        "Loaded IBP snapshot table=%s v%s (%s rows) from %s",
        table_name, version, len(df), table_uri,
    )
    return df, version, last_modified, len(df), table_uri


def _fetch(table_name: str, *, force_refresh: bool) -> tuple[pd.DataFrame, IBPSnapshotMeta]:
    """Internal helper — fetch *table_name*, optionally bypassing the cache.

    Assembles the :class:`IBPSnapshotMeta` *outside* the cache from the
    native values returned by :func:`_cached_fetch`, so the cached layer
    never has to (de)serialise a custom class.
    """
    if force_refresh:
        # ``cache_data.clear()`` purges every cached entry of the
        # decorated function regardless of args.  That is the desired
        # behaviour for the IBP page's single "Refresh" button — both
        # tables are pulled together, so dropping both cached snapshots
        # at once keeps them in sync.
        _cached_fetch.clear()
    df, version, last_modified, row_count, source_uri = _cached_fetch(table_name, "default")
    meta = IBPSnapshotMeta(
        table=table_name,
        version=version,
        last_modified=last_modified,
        row_count=row_count,
        source_uri=source_uri,
    )
    return df, meta


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


# ── Slim, projected fetcher (perf-sensitive callers) ─────────────────────────
#
# Consumers that need only a handful of columns + a narrow date window
# (e.g. the Demand Plan Comparison stack) should use these slim helpers.
# They push column projection and a month predicate into DuckDB so OneLake
# returns just the rows we actually use.
#
# Keep separate default column sets per table, but route through one generic
# implementation (single code path, no duplicated SQL/caching logic).
_SLIM_SHIPMENTS_COLUMNS: tuple[str, ...] = (
    "Item No", "Customer No", "Customer Name", "Month", "Shipped Qty lbs",
)
_SLIM_ORDERS_COLUMNS: tuple[str, ...] = (
    "Item No", "Customer No", "Customer Name", "Month", "Ordered Qty lbs",
)


def _quote_ident(col: str) -> str:
    """Quote an identifier for DuckDB (escapes embedded double quotes)."""
    return '"' + col.replace('"', '""') + '"'


def _build_slim_table_sql(
    table_uri: str,
    columns: tuple[str, ...],
    months: Optional[tuple[date, ...]],
) -> str:
    """Return a slim ``SELECT`` query with optional month predicate.

    *columns* are quoted defensively (Fabric column names contain spaces);
    *months* — when supplied — becomes an inclusive
    ``Month IN (DATE 'yyyy-mm-dd', …)`` predicate so DuckDB can prune
    the Delta scan instead of materialising the full table.
    """
    select = ", ".join(_quote_ident(c) for c in columns)
    sql = f"SELECT {select} FROM delta_scan('{table_uri}')"
    if months:
        # Inclusive list — first-of-month dates, matching how Fabric
        # stores the Month column for Shipments.
        literals = ", ".join(f"DATE '{m.isoformat()}'" for m in months)
        sql += f' WHERE "Month" IN ({literals})'
    return sql


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch_slim_table(
    table_name: str,
    columns_key: tuple[str, ...],
    months_key: Optional[tuple[date, ...]],
    _cache_token: str,
) -> tuple[pd.DataFrame, str]:
    """Streamlit-cached slim read for one IBP table.

    Keyed on ``(table_name, columns, months)`` so distinct slices share
    distinct cache slots.
    """
    table_uri = _build_table_uri(table_name)
    token = _acquire_storage_token()
    sql = _build_slim_table_sql(table_uri, columns_key, months_key)
    try:
        con = get_duckdb_connection()
        with duckdb_lock():
            bind_storage_token(con, token)
            df = con.execute(sql).df()
    except FabricAuthError as exc:
        raise IBPOfficialSourceError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise IBPOfficialSourceError(
            f"Could not read slim Delta projection for '{table_name}' at {table_uri}.  "
            f"Underlying error: {exc}"
        ) from exc
    logger.info(
        "Loaded slim IBP table=%s (%s rows, cols=%s, months=%s).",
        table_name, len(df), columns_key, months_key,
    )
    return df, table_uri


def _fetch_ibp_table_slim_df(
    table_name: str,
    *,
    months: Optional[tuple[date, ...]],
    columns: tuple[str, ...],
    force_refresh: bool,
) -> pd.DataFrame:
    """Return a thin, predicate-pushed slice of one IBP table.

    Parameters
    ----------
    months
        Optional inclusive whitelist of first-of-month dates.  When set,
        DuckDB filters at scan time (the Delta-kernel reader prunes files
        whose statistics fall outside the predicate), so OneLake returns
        far less data than the full table read.  When ``None`` every
        month is returned (still projected).
    columns
        Subset of columns to project.  Defaults to
        Requested projection for the table.
    force_refresh
        When True, drops every cached slim slice before reading.

    Returns
    -------
    pandas.DataFrame
        The projected, optionally-filtered Shipments frame.  Always
        carries exactly the requested column set, in the requested order.

    Raises
    ------
    IBPOfficialSourceError
        On any auth, configuration, or storage failure.
    """
    if force_refresh:
        _cached_fetch_slim_table.clear()
    # Normalise months → sorted tuple so cache keys collapse across
    # callers that pass the same set in a different order.
    months_key = tuple(sorted(set(months))) if months else None
    df, _uri = _cached_fetch_slim_table(
        table_name, tuple(columns), months_key, "default",
    )
    return df


def fetch_ibp_shipments_slim_df(
    *, months: Optional[tuple[date, ...]] = None,
    columns: tuple[str, ...] = _SLIM_SHIPMENTS_COLUMNS,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Return a thin, predicate-pushed slice of ``dbo.IBP Shipments``."""
    return _fetch_ibp_table_slim_df(
        _TABLE_SHIPMENTS,
        months=months,
        columns=columns,
        force_refresh=force_refresh,
    )


def fetch_ibp_orders_slim_df(
    *, months: Optional[tuple[date, ...]] = None,
    columns: tuple[str, ...] = _SLIM_ORDERS_COLUMNS,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Return a thin, predicate-pushed slice of ``dbo.IBP Orders``."""
    return _fetch_ibp_table_slim_df(
        _TABLE_ORDERS,
        months=months,
        columns=columns,
        force_refresh=force_refresh,
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch_shipments_months(_cache_token: str) -> tuple[date, ...]:
    """Streamlit-cached ``SELECT DISTINCT "Month"`` over IBP Shipments."""
    table_uri = _build_table_uri(_TABLE_SHIPMENTS)
    token = _acquire_storage_token()
    sql = f'SELECT DISTINCT "Month" FROM delta_scan(\'{table_uri}\')'
    try:
        con = get_duckdb_connection()
        with duckdb_lock():
            bind_storage_token(con, token)
            df = con.execute(sql).df()
    except FabricAuthError as exc:
        raise IBPOfficialSourceError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise IBPOfficialSourceError(
            f"Could not read distinct Months from '{_TABLE_SHIPMENTS}' at "
            f"{table_uri}.  Underlying error: {exc}"
        ) from exc
    # Coerce to first-of-month dates; drop anything unparseable.
    parsed = pd.to_datetime(df["Month"], errors="coerce").dropna()
    months = {ts.to_period("M").to_timestamp().date() for ts in parsed}
    return tuple(sorted(months))


def fetch_ibp_shipments_months(*, force_refresh: bool = False) -> tuple[date, ...]:
    """Return the sorted distinct first-of-month dates in ``dbo.IBP Shipments``.

    Cheap, projection-only scan (``SELECT DISTINCT "Month"``) used to
    populate the Demand Plan Comparison *Actual* month-range pickers from
    the actuals' true source rather than from the plan-history tracker.
    """
    if force_refresh:
        _cached_fetch_shipments_months.clear()
    return _cached_fetch_shipments_months("default")


__all__ = [
    "IBPSnapshotMeta",
    "IBPOfficialSourceError",
    "fetch_ibp_orders_df",
    "fetch_ibp_shipments_df",
    "fetch_ibp_shipments_slim_df",
    "fetch_ibp_orders_slim_df",
    "fetch_ibp_shipments_months",
]
