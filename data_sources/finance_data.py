"""
OneLake-backed reader for the monthly **Finance** extract.

The Finance team publishes a dated snapshot under
``Files/Finance/finance_data_YYYYMMDD.csv`` in the HTST lakehouse — a wide
GL-level table (Net Sales, COGS, margin, …) at Item × Customer × month
granularity.  Business Health consumes the **Net Sales** column to chart a
Net-Sales-YoY momentum line beside its Order/Shipment volume lines.

Responsibilities
----------------
* Discover the **latest** ``finance_data_*.csv`` in ``Files/Finance/`` (so the
  app follows next month's file automatically — no code change per drop).
* Read it once per cache slot and hand back a plain ``DataFrame`` + the source
  file name for the UI caption.

The taxonomy → Business-Health-category mapping is NOT done here; it is done in
``demand_plan_comparison.enrich_finance_df`` by joining ``Item No.`` to PDH, so
Net Sales is classified by the *same* rules as the volume lines.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io

_SECRETS_SECTION: str = "fabric_htst"
_FOLDER: str = "Finance"
_FILE_PREFIX: str = "finance_data_"
_CACHE_TTL_SECONDS: int = 15 * 60


class FinanceDataError(RuntimeError):
    """Raised on configuration / auth / I-O failures for the Finance extract."""


@dataclass(frozen=True)
class FinanceDataSnapshot:
    """The latest Finance extract plus the source file name (for the caption)."""
    df: pd.DataFrame
    file_name: str

    @property
    def row_count(self) -> int:
        return int(len(self.df))


def _latest_finance_path() -> tuple[str, str]:
    """Return ``(full_path, leaf_name)`` of the newest ``finance_data_*.csv``.

    "Newest" is decided by the file *name* (the ``YYYYMMDD`` stamp sorts
    lexically), falling back to ``last_modified`` for ties / off-pattern names.
    Raises :class:`FinanceDataError` when the folder holds no matching file.
    """
    try:
        files = _io.list_files(_SECRETS_SECTION, _FOLDER, suffix=".csv")
    except _io.LakehouseIOError as exc:
        raise FinanceDataError(
            f"Could not list 'Files/{_FOLDER}' in Microsoft Fabric: {exc}"
        ) from exc

    candidates = [f for f in files if f.name.startswith(_FILE_PREFIX)]
    if not candidates:
        raise FinanceDataError(
            f"No '{_FILE_PREFIX}*.csv' file found under 'Files/{_FOLDER}'.  "
            "Verify the Finance pipeline has published a snapshot and that "
            "your account has Read access to the lakehouse."
        )
    newest = max(candidates, key=lambda f: (f.name, f.last_modified or ""))
    return newest.full_path, newest.name


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch() -> tuple[pd.DataFrame, str]:
    """Cached read of the latest Finance CSV → ``(df, file_name)`` (native types)."""
    full_path, leaf = _latest_finance_path()
    try:
        df, _etag = _io.read_csv(
            _SECRETS_SECTION, full_path,
            # The extract mixes typed/blank cells across a few columns; read as
            # a stable frame and let the enricher coerce only what it needs.
            read_csv_kwargs={"low_memory": False},
        )
    except _io.LakehouseIOError as exc:
        raise FinanceDataError(
            f"Could not read 'Files/{full_path}' from Microsoft Fabric: {exc}"
        ) from exc
    if df is None:
        raise FinanceDataError(f"File not found in OneLake: Files/{full_path}")
    return df, leaf


def fetch_finance_data(*, force_refresh: bool = False) -> FinanceDataSnapshot:
    """Return the latest ``finance_data_*.csv`` as a snapshot.

    ``force_refresh`` clears this connector's cache slot so the next call hits
    Fabric (wire to a "Refresh from Fabric" control).
    """
    if force_refresh:
        _cached_fetch.clear()
    df, leaf = _cached_fetch()
    return FinanceDataSnapshot(df=df, file_name=leaf)
