"""Ship-to-sites dimension connector — ``dbo.dp_dimshiptosites``.

Reads the ship-to-site dimension Delta table that maps a **party site**
to its **customer** and **site** descriptors:

    customer_num │ account_description │ party_site_code │ site_name │ site │ plan_to_code

It lives in the SAME Fabric Lakehouse as the IBP tables, so this module
reuses the shared auth + DuckDB plumbing in :mod:`data_sources.fabric_auth`
(one sign-in, one connection, ``azure`` + ``delta`` extensions pre-loaded).

The Demand Plan Comparison *driver tables* use it to translate the
tracker's ``Party Site Number`` into a customer name (``account_description``)
and into a ``customer_num`` that matches IBP Shipments' ``Customer No``.

Caching note
------------
``_cached_fetch`` returns ONLY native values (DataFrame + plain scalars).
Returning a custom dataclass makes ``st.cache_data``'s pickle round-trip
fail with ``UnserializableReturnValueError`` whenever Streamlit's file
watcher re-executes this module (the cached instance's class is then
"not the same object" as the redefined class).  See the identical note
in :mod:`data_sources.ibp_official`.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import streamlit as st

from data_sources.fabric_auth import (
    FabricAuthError,
    acquire_storage_token,
    bind_storage_token,
    duckdb_lock,
    get_duckdb_connection,
    prepare_duckdb_tls,
)
from data_sources.fabric_tls import tls_error_hint as _tls_hint


logger = logging.getLogger(__name__)


# ── Source identity (same lakehouse as IBP; dbo schema) ───────────────────────
_WORKSPACE_GUID = "bb11c51d-03c8-4f1b-938c-e20657a8f31d"
_LAKEHOUSE_GUID = "a01f513d-eee7-41eb-8c15-670bc40e7fc8"
_SCHEMA = "dbo"
_TABLE = "dp_dimshiptosites"
# Plan-to-sites bridge: plan_to_code → customer_num (→ corporate_group).  It is
# the working Hop-2 key space for the forecast-side corporate-group chain
# (dp_dimshiptosites' own customer_num does NOT match dp_dimcustomernames).
_TABLE_PLAN_TO_SITES = "dp_dimplantosites"

# 15-minute cache TTL — a slow-moving dimension; match the IBP connector.
_CACHE_TTL_SECONDS = 15 * 60

# Column-name candidates (probed so a spelling drift upstream is a
# one-line fix here rather than a silent join failure).
PARTY_SITE_CANDIDATES: tuple[str, ...] = (
    "party_site_code", "party_site_number", "PartySiteCode", "Party Site Code",
)
CUSTOMER_NUM_CANDIDATES: tuple[str, ...] = (
    "customer_num", "customer_number", "CustomerNum", "Customer Num",
)
# The bridge key into dp_dimplantosites.  Shared by every consumer that resolves
# corporate group off a Party Site Number, so the join spelling lives in ONE
# place (both dp_dimshiptosites and dp_dimplantosites carry ``plan_to_code``).
PLAN_TO_CANDIDATES: tuple[str, ...] = (
    "plan_to_code", "PlanToCode", "Plan To Code", "plan_to",
)
ACCOUNT_DESC_CANDIDATES: tuple[str, ...] = (
    "account_description", "account_desc", "AccountDescription",
)
SITE_NAME_CANDIDATES: tuple[str, ...] = (
    "site_name", "SiteName", "Site Name",
)


class ShipToSitesSourceError(RuntimeError):
    """Raised on any failure to read ``dbo.dp_dimshiptosites``.

    Wraps the underlying exception so callers can render a single clean
    error path without leaking deltalake / DuckDB stack traces.
    """


def _build_table_uri(table: str) -> str:
    """Construct the OneLake ``abfss://`` URI for a dimension table."""
    return (
        f"abfss://{_WORKSPACE_GUID}@onelake.dfs.fabric.microsoft.com/"
        f"{_LAKEHOUSE_GUID}/Tables/{_SCHEMA}/{table}"
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch(table: str) -> tuple[pd.DataFrame, str]:
    """Streamlit-cached full-table read of *table* (native values only).

    Returns ``(df, source_uri)`` — no custom class (see module docstring).  The
    table name doubles as the cache key, so each dim caches independently.
    """
    table_uri = _build_table_uri(table)
    try:
        token = acquire_storage_token()
    except FabricAuthError as exc:
        raise ShipToSitesSourceError(str(exc)) from exc

    # Honor [fabric_htst] ca_cert_file / ssl_verify for the OneLake TLS
    # handshake (corporate MITM proxy → else DuckDB libcurl "SSL connect error").
    ssl_verify = prepare_duckdb_tls()
    try:
        con = get_duckdb_connection()
        with duckdb_lock():
            bind_storage_token(con, token, ssl_verify=ssl_verify)
            df = con.execute(f"SELECT * FROM delta_scan('{table_uri}')").df()
    except FabricAuthError as exc:
        raise ShipToSitesSourceError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise ShipToSitesSourceError(
            f"Could not read Delta table via DuckDB at {table_uri}.  "
            f"Verify the lakehouse identifiers and your Read access.  "
            f"Underlying error: {exc}{_tls_hint(exc)}"
        ) from exc

    logger.info("Loaded %s (%s rows) from %s", table, len(df), table_uri)
    return df, table_uri


def fetch_dimshiptosites_df(*, force_refresh: bool = False) -> pd.DataFrame:
    """Return the ship-to-sites dimension as a DataFrame.

    Raises :class:`ShipToSitesSourceError` on any read failure.
    """
    if force_refresh:
        _cached_fetch.clear()
    df, _uri = _cached_fetch(_TABLE)
    return df


def fetch_dp_dimplantosites_df(*, force_refresh: bool = False) -> pd.DataFrame:
    """Return the plan-to-sites dimension (``dbo.dp_dimplantosites``).

    Schema: ``plan_to_code │ site_name │ customer_num │ corporate_group``.  It
    bridges ``dp_dimshiptosites.plan_to_code`` → a ``customer_num`` that matches
    ``dp_dimcustomernames`` — the working Hop-2 for forecast-side corporate
    group.  Raises :class:`ShipToSitesSourceError` on any read failure.
    """
    if force_refresh:
        _cached_fetch.clear()
    df, _uri = _cached_fetch(_TABLE_PLAN_TO_SITES)
    return df


__all__ = [
    "ShipToSitesSourceError",
    "fetch_dimshiptosites_df",
    "fetch_dp_dimplantosites_df",
    "PARTY_SITE_CANDIDATES",
    "CUSTOMER_NUM_CANDIDATES",
    "PLAN_TO_CANDIDATES",
    "ACCOUNT_DESC_CANDIDATES",
    "SITE_NAME_CANDIDATES",
]
