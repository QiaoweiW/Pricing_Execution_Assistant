"""Customer dimension connector — ``dbo.dp_dimcustomernames``.

The Product Line Review pipeline needs **one** customer-side dimension
table, stored as a Delta table in the same Fabric Lakehouse as the IBP
tables (one sign-in, one DuckDB pool — see :mod:`data_sources.fabric_auth`):

* **dp_dimcustomernames**
      ``customer_num │ customer_name │ corporate_group``

  This table is the single source of truth for Corporate Group:

  * **Actual rows** (synthesised from ``IBP Shipments``) — exact join on
    ``shipments.Customer No`` → ``customer_num`` → ``corporate_group``.
  * **Base Plan rows** (from ``qry_demand_item_customer_detail.csv``) —
    ``party_site_number`` is first translated to ``customer_num`` via
    :mod:`data_sources.ship_to_sites` (``dbo.dp_dimshiptosites``), then
    looked up the same way as the Actual rows.
  * **R&O rows** — fuzzy match on ``Customer Name`` against
    ``customer_name`` (party-site numbers are not meaningful for R&O,
    and Customer No is left blank on these rows).
  * **IBP Orders** (run-rate / PY columns of the PLR table) — exact
    join on ``orders.Customer No`` → ``customer_num`` →
    ``corporate_group``.

The legacy ``dp_dimcorporategroup`` table is no longer used: every
Customer No → Corporate Group lookup now hits ``dp_dimcustomernames``
so the planner has ONE table to maintain for corporate-group ownership
(planner spec, June 2026 cycle).

Why a separate module
---------------------
Mirror of :mod:`data_sources.ship_to_sites`: a slow-moving dimension
table that lives next to the IBP fact tables, shares the same auth +
DuckDB plumbing, and is read end-to-end with a 15-minute Streamlit
cache.  Keeping it out of the PLR builder file lets the builder stay
pure-pandas and lets any future caller reuse the dim fetcher verbatim.

Caching note
------------
``_cached_customer_names_fetch`` returns ONLY native values (DataFrame +
plain scalar URI) — never a custom dataclass — to keep Streamlit's
pickle round-trip robust against file-watcher reloads that rebind the
class object.  Same contract as :mod:`data_sources.ship_to_sites` /
:mod:`ibp_official`.
"""
from __future__ import annotations

import logging

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
_TABLE_CUSTOMER_NAMES = "dp_dimcustomernames"

# 15-minute cache TTL — slow-moving planner reference data; match the
# IBP / ship-to-sites cadence.
_CACHE_TTL_SECONDS = 15 * 60


# ── Column-name candidates ────────────────────────────────────────────────────
#
# Probed so a one-line spelling drift upstream is a one-line fix here
# (rather than a silent join failure).  Same pattern used everywhere else
# the codebase touches Fabric Delta tables.

CUSTOMER_NUM_CANDIDATES: tuple[str, ...] = (
    "customer_num", "customer_number", "Customer Num", "CustomerNum",
    "Customer No", "customer_no",
)
CUSTOMER_NAME_CANDIDATES: tuple[str, ...] = (
    "customer_name", "Customer Name", "CustomerName", "customer",
)
CORPORATE_GROUP_CANDIDATES: tuple[str, ...] = (
    "corporate_group", "Corporate Group", "CorporateGroup", "corp_group",
)


# ── Errors ────────────────────────────────────────────────────────────────────

class CustomerDimsError(RuntimeError):
    """Raised on any failure to read ``dbo.dp_dimcustomernames``.

    Wraps the underlying exception so callers can render a single clean
    error path without leaking deltalake / DuckDB stack traces into the
    Streamlit banner.
    """


# ── Internal helpers ──────────────────────────────────────────────────────────

def _build_table_uri(table: str) -> str:
    """Construct the OneLake ``abfss://`` URI for one dim table."""
    return (
        f"abfss://{_WORKSPACE_GUID}@onelake.dfs.fabric.microsoft.com/"
        f"{_LAKEHOUSE_GUID}/Tables/{_SCHEMA}/{table}"
    )


def _read_full_table(table: str) -> pd.DataFrame:
    """Token-bound full-table scan via DuckDB.  Raises :class:`CustomerDimsError`."""
    table_uri = _build_table_uri(table)
    try:
        token = acquire_storage_token()
    except FabricAuthError as exc:
        raise CustomerDimsError(str(exc)) from exc
    # Honor [fabric_htst] ca_cert_file / ssl_verify for the OneLake TLS
    # handshake (corporate MITM proxy → else DuckDB libcurl "SSL connect error").
    ssl_verify = prepare_duckdb_tls()
    try:
        con = get_duckdb_connection()
        with duckdb_lock():
            bind_storage_token(con, token, ssl_verify=ssl_verify)
            df = con.execute(f"SELECT * FROM delta_scan('{table_uri}')").df()
    except FabricAuthError as exc:
        raise CustomerDimsError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise CustomerDimsError(
            f"Could not read Delta table via DuckDB at {table_uri}.  "
            f"Verify the lakehouse identifiers and your Read access.  "
            f"Underlying error: {exc}{_tls_hint(exc)}"
        ) from exc
    logger.info("Loaded %s (%s rows) from %s", table, len(df), table_uri)
    return df


# ── Streamlit-cached fetch ────────────────────────────────────────────────────

@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_customer_names_fetch(_cache_token: str) -> pd.DataFrame:
    """Streamlit-cached read of ``dbo.dp_dimcustomernames``."""
    return _read_full_table(_TABLE_CUSTOMER_NAMES)


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_dp_dimcustomernames_df(*, force_refresh: bool = False) -> pd.DataFrame:
    """Return the ``dbo.dp_dimcustomernames`` dimension as a DataFrame.

    Schema (per planner spec, columns are probed via the ``*_CANDIDATES``
    constants above so harmless casing / spacing drift is tolerated)::

        customer_num │ customer_name │ corporate_group

    Used by the PLR enrichment pipeline as the single Corporate Group
    lookup target for every row of the unified frame AND for the IBP
    Orders run-rate columns.

    Raises :class:`CustomerDimsError` on any read failure.
    """
    if force_refresh:
        _cached_customer_names_fetch.clear()
    return _cached_customer_names_fetch("default")


__all__ = [
    "CustomerDimsError",
    "fetch_dp_dimcustomernames_df",
    "CUSTOMER_NUM_CANDIDATES",
    "CUSTOMER_NAME_CANDIDATES",
    "CORPORATE_GROUP_CANDIDATES",
]
