"""
IRI / Circana weekly **consumer sell-through** velocity, indexed to 100.

Source: ``Files/RO Tracking/IRI/IRI_Weekly_Units_*.csv`` in the HTST lakehouse —
category POS data (multiple brands incl. competitors, many retail geographies) at
``Geography × Major Brand × Custom Subtype × Custom Process × Custom Size × Week``.
Circana weeks are labelled by their **ending Sunday**; we anchor every week to the
Monday of that Mon–Sun span so it lines up with the shipments velocity grid.

The demand-planning use is a **leading indicator**: consumer sell-through leads,
retailer orders follow, our shipments lag.  To compare shapes across series with
different units, each is rebased to an index:

    velocity   = Σ Units ÷ Σ (stores selling)         (a true blended per-store
                 rate — reconstruct stores = U Sales ÷ Units-per-Store-Selling,
                 then aggregate numerator/denominator; never average the ratio)
    baseline   = MEDIAN velocity over weeks with U Sales > 0   (FULL history)
    index      = velocity ÷ baseline × 100

The baseline is fixed over full history, so slicing the Week window only zooms —
the index always reads as "vs a typical week".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io

_SECRETS_SECTION: str = "fabric_htst"
_FOLDER: str = "RO Tracking/IRI"
_FILE_PREFIX: str = "IRI_Weekly_Units_"
_CACHE_TTL_SECONDS: int = 60 * 60

# Source columns.
COL_GEOGRAPHY: str = "Geography"
COL_BRAND: str = "Major Brand"
COL_SUBTYPE: str = "Custom Subtype"
COL_PROCESS: str = "Custom Process"
COL_SIZE: str = "Custom Size"
COL_WEEK: str = "Week"
COL_U_SALES: str = "U Sales"
COL_UNITS_PER_STORE: str = "Units per Store Selling"

# The five filterable IRI dimensions, in display order.
FILTER_COLS: tuple[str, ...] = (COL_GEOGRAPHY, COL_BRAND, COL_SUBTYPE, COL_PROCESS, COL_SIZE)

# Output (tidy weekly) columns.
WEEK_START: str = "week_start"
SELL_THROUGH_VELOCITY: str = "sell_through_velocity"
SELL_THROUGH_INDEX: str = "sell_through_index"
U_SALES_WK: str = "u_sales"


class IRIVelocityError(RuntimeError):
    """Raised on configuration / auth / I-O failures for the IRI extract."""


@dataclass(frozen=True)
class IRIWeekly:
    """Weekly IRI sell-through velocity + index for the current filter.

    ``weekly`` has ``week_start`` (Monday), ``sell_through_velocity`` (units per
    store selling, blended), ``sell_through_index`` (÷ baseline × 100) and
    ``u_sales``.  ``baseline`` is the full-history median velocity (the "100").
    ``file_name`` is the source snapshot for the caption.
    """
    weekly: pd.DataFrame
    baseline: Optional[float]
    file_name: str


def _latest_iri_path() -> tuple[str, str]:
    """``(full_path, name)`` of the newest ``IRI_Weekly_Units_*.csv``."""
    try:
        files = _io.list_files(_SECRETS_SECTION, _FOLDER, suffix=".csv")
    except _io.LakehouseIOError as exc:
        raise IRIVelocityError(f"Could not list 'Files/{_FOLDER}': {exc}") from exc
    cands = [f for f in files if f.name.startswith(_FILE_PREFIX)]
    if not cands:
        raise IRIVelocityError(
            f"No '{_FILE_PREFIX}*.csv' under 'Files/{_FOLDER}'.")
    newest = max(cands, key=lambda f: (f.name, f.last_modified or ""))
    return newest.full_path, newest.name


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch() -> tuple[pd.DataFrame, str]:
    full_path, leaf = _latest_iri_path()
    try:
        df, _etag = _io.read_csv(_SECRETS_SECTION, full_path,
                                 read_csv_kwargs={"low_memory": False})
    except _io.LakehouseIOError as exc:
        raise IRIVelocityError(
            f"Could not read 'Files/{full_path}': {exc}") from exc
    if df is None:
        raise IRIVelocityError(f"File not found in OneLake: Files/{full_path}")
    df.columns = df.columns.str.strip()
    return df, leaf


def fetch_iri_df(*, force_refresh: bool = False) -> tuple[pd.DataFrame, str]:
    """Return ``(raw IRI dataframe, source file name)``.  ``force_refresh``
    clears the cache slot before reading."""
    if force_refresh:
        _cached_fetch.clear()
    return _cached_fetch()


def _week_start(week: pd.Series) -> pd.Series:
    """Monday of each Circana week (labelled by its ending Sunday)."""
    dt = pd.to_datetime(week, errors="coerce")
    return (dt - pd.to_timedelta(dt.dt.weekday, unit="D")).dt.normalize()


def distinct_values(df: pd.DataFrame, col: str) -> list[str]:
    """Sorted distinct non-blank values of *col* (``[]`` when absent)."""
    if df is None or df.empty or col not in df.columns:
        return []
    s = df[col].dropna().astype(str).str.strip()
    return sorted({v for v in s if v and v.lower() not in ("nan", "none")})


def build_iri_weekly(
    df: pd.DataFrame, *,
    geographies: Optional[list[str]] = None,
    brands: Optional[list[str]] = None,
    subtypes: Optional[list[str]] = None,
    processes: Optional[list[str]] = None,
    sizes: Optional[list[str]] = None,
) -> IRIWeekly:
    """Filter the IRI frame and build weekly sell-through velocity + index.

    Aggregates correctly across the selection: reconstructs stores-selling per
    row (``U Sales ÷ Units-per-Store-Selling``), sums Units and stores per week,
    then ``velocity = Σ Units ÷ Σ stores``.  Baseline = median velocity over
    weeks with U Sales > 0 (full history), so the index is comparable across
    slices.  Empty / column-less input → an empty, well-shaped result.
    """
    cols = [WEEK_START, SELL_THROUGH_VELOCITY, SELL_THROUGH_INDEX, U_SALES_WK]
    empty = IRIWeekly(pd.DataFrame(columns=cols), None, "")
    if df is None or df.empty or COL_WEEK not in df.columns:
        return empty

    work = df
    dim_filters = {
        COL_GEOGRAPHY: geographies, COL_BRAND: brands, COL_SUBTYPE: subtypes,
        COL_PROCESS: processes, COL_SIZE: sizes,
    }
    mask = pd.Series(True, index=work.index)
    for col, values in dim_filters.items():
        if values and col in work.columns:
            mask &= work[col].astype(str).str.strip().isin([str(v) for v in values])
    work = work.loc[mask]
    if work.empty:
        return empty

    units = pd.to_numeric(work.get(COL_U_SALES), errors="coerce").fillna(0.0)
    upss = pd.to_numeric(work.get(COL_UNITS_PER_STORE), errors="coerce")
    # stores selling = units ÷ (units per store); 0 when the ratio is missing/0.
    stores = np.where((upss > 0) & units.notna(), units / upss.replace(0, np.nan), 0.0)
    grouped = pd.DataFrame({
        WEEK_START: _week_start(work[COL_WEEK]).to_numpy(),
        "_units": units.to_numpy(),
        "_stores": np.nan_to_num(stores),
    }).dropna(subset=[WEEK_START])
    if grouped.empty:
        return empty

    weekly = (grouped.groupby(WEEK_START, as_index=False)
              .agg(_units=("_units", "sum"), _stores=("_stores", "sum"))
              .sort_values(WEEK_START).reset_index(drop=True))
    denom = weekly["_stores"].where(weekly["_stores"] > 0)
    weekly[SELL_THROUGH_VELOCITY] = weekly["_units"] / denom
    weekly[U_SALES_WK] = weekly["_units"]

    active = weekly.loc[weekly["_units"] > 0, SELL_THROUGH_VELOCITY].dropna()
    baseline = float(active.median()) if not active.empty and active.median() > 0 else None
    weekly[SELL_THROUGH_INDEX] = (
        weekly[SELL_THROUGH_VELOCITY] / baseline * 100.0 if baseline else np.nan)

    return IRIWeekly(
        weekly=weekly[[WEEK_START, SELL_THROUGH_VELOCITY, SELL_THROUGH_INDEX, U_SALES_WK]],
        baseline=baseline, file_name="")
