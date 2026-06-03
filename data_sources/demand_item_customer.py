"""Demand × Item × Customer enrichment pipeline (Product Line Review source).

Replaces the per-row Base-Plan lookup the Product Line Review section used
to do directly against ``ibp_base_plan_current.csv``.  Why the change:

* The planner wants the CY Actual months (months inside CY Full Year but
  OUTSIDE the CY YTG range) to reflect **actual shipments** rather than
  a stale Base Plan placeholder.
* They also want a **Corporate Group** dimension on every row so the
  customer-detail rows roll up by parent (e.g. "Albertsons / Safeway / …"
  → "Albertsons-Safeway"), not by raw Plan-To name.
* And they want a single CSV — ``demand_order_item_customer.csv`` —
  published to Fabric so other consumers (Power BI, downstream notebooks)
  see the same view the PLR table renders from.

Inputs
------
1. ``Files/RO Tracking/Demand Plan/qry_demand_item_customer_detail.csv``
       Authoritative source for Base Plan / R&O / placeholder rows by
       month × item × customer × forecast type.  Columns (Customer No
       is added by THIS pipeline — the upstream CSV does not carry it)::

           Start of Month │ Item │ Item Description │ Customer Name │
           Party Site Number │ Demand Plan Pounds │ Forecast Type │
           Portfolio Major │ Portfolio Minor │ Supply Format

2. ``dbo.IBP Shipments`` (Delta)
       Source for the CY Actual months.  We pull the slim projection
       (``Item No, Customer No, Customer Name, Month, Shipped Qty lbs``)
       and use it to REPLACE every detail-CSV row whose ``Start of Month``
       falls inside the CY Actual Months window — emitted as
       ``Forecast Type = "Actual"`` rows with the shipments' ``Customer
       No`` preserved.

3. ``qry_pdh.csv``
       Item-level enrichment for the synthesised "Actual" rows
       (``Item Description``, ``Portfolio Major / Minor / Supply Format``).

4. ``dbo.dp_dimshiptosites`` (Delta)
       Translation table for the Base-Plan rows:
       ``party_site_code`` → ``customer_num``.  Used to back-fill
       ``Customer No`` on Base-Plan rows (the upstream detail CSV ships
       only Party Site Number).

5. ``dbo.dp_dimcustomernames`` (Delta)
       Single source of truth for Corporate Group:
       ``customer_num │ customer_name │ corporate_group``.

       *Base Plan + Actual* rows resolve via the exact
       ``customer_num`` join above; *R&O* rows resolve via a fuzzy
       ``Customer Name`` match (R&O rows have no useful Customer No).

Output
------
``Files/RO Tracking/Demand Plan/demand_order_item_customer.csv`` — same
column order as the input CSV plus two new columns:

* ``Customer No`` — inserted RIGHT BEFORE ``Customer Name``
  (planner spec).  Sourced from shipments for Actual rows, from the
  party-site → customer_num lookup for Base Plan rows, and left blank
  for R&O rows.
* ``Corporate Group`` — appended at the end.  Falls back to the row's
  Customer Name when no usable corporate-group lookup result exists.

Per-forecast-type Corporate Group resolution
--------------------------------------------
========== ============================== ===========================
Forecast   Lookup                          Customer No source
Type
========== ============================== ===========================
Actual     customer_num → dimcustomernames shipments.Customer No
Base Plan  customer_num → dimcustomernames party_site → dimshiptosites
R&O        fuzzy Customer Name              (blank)
========== ============================== ===========================

Every branch shares the same universal fallback: when the matched
``corporate_group`` is empty / NaN / literal "Blank", or no match was
found at all, ``Corporate Group = Customer Name`` for that row.

Fuzzy match feasibility
-----------------------
We use :mod:`rapidfuzz` (already added to ``requirements.txt``), which
is a drop-in for ``fuzzywuzzy`` with a fully-C scorer.  Strategy for
the R&O branch only:

* normalise both sides ONCE (uppercase, drop punctuation, drop common
  legal suffixes like "LLC", "INC", "& CO");
* extract the DISTINCT R&O customer names from the unified frame (a
  few hundred at most), match each ONCE against the dim table
  (~tens of thousands of rows) with ``token_set_ratio`` at a default
  cutoff of 88;
* memoise inside the page's ``st.cache_data`` wrapper keyed by input
  shape signatures so repeat renders are instant.

Performance contract
--------------------
* Every fetcher caches with the usual 15-minute TTL.
* The enrichment is wrapped in an ``st.cache_data`` call (keyed on
  shape signatures) so re-renders that don't change the underlying
  data short-circuit to a microsecond cache hit.
* The Fabric write is gated by a session-scoped signature guard
  (planner spec: "save whenever a new file is created") — so the same
  rendered frame never round-trips to Fabric twice in the same session.

The PLR builder consumes :func:`prepare_demand_long_for_plr`, which
turns the wide saved-CSV shape into the long (month, pounds, dim…)
shape the table builder expects — same contract as
``prepare_ibp_base_plan_long``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

import numpy as np
import pandas as pd
import streamlit as st

from data_sources.demand_plan_comparison import (
    BRAND_BRANDED,
    BRAND_PRIVATE,
    _IBP_CUSTOMER_NAME_CANDIDATES,
    _IBP_CUSTOMER_NO_CANDIDATES,
    _IBP_ITEM_CANDIDATES,
    _IBP_MONTH_CANDIDATES,
    _IBP_ORDERED_QTY_CANDIDATES,
    _IBP_QTY_CANDIDATES,
    _attach_dims,
    _vectorised_brand,
    _vectorised_clean_str,
    _vectorised_item_key,
    _vectorised_start_of_month,
    build_item_dim_frame,
)
from data_sources.customer_dims import (
    CORPORATE_GROUP_CANDIDATES,
    CUSTOMER_NAME_CANDIDATES,
    CUSTOMER_NUM_CANDIDATES,
)
from data_sources.demand_summary import _resolve_column
from data_sources.fabric_lakehouse_io import LakehouseIOError, read_csv, write_csv
from data_sources.ship_to_sites import (
    CUSTOMER_NUM_CANDIDATES as _STS_CUSTOMER_NUM_CANDIDATES,
    PARTY_SITE_CANDIDATES as _STS_PARTY_SITE_CANDIDATES,
)


logger = logging.getLogger(__name__)


# ── Fabric paths + secrets section (same as the rest of RO Tracking) ──────────

_SECRETS_SECTION: str = "fabric_htst"

DETAIL_BLOB_PATH: str = (
    "RO Tracking/Demand Plan/qry_demand_item_customer_detail.csv"
)
DEMAND_ORDER_ITEM_CUSTOMER_BLOB_PATH: str = (
    "RO Tracking/Demand Plan/demand_order_item_customer.csv"
)

# 60-minute TTL — matches the Demand Summary cadence (the detail CSV is
# refreshed daily upstream; a shorter TTL just forces repeated 5-MB cold
# reads without any freshness gain).  Planners who genuinely need an
# immediate re-read still have the "🔄 Refresh from Fabric" button.
_CACHE_TTL_SECONDS: int = 60 * 60


# ── Canonical output column names ────────────────────────────────────────────
#
# These match the source CSV's schema exactly (the planner provided the
# example row in their spec).  ``CORPORATE_GROUP`` is appended at the end.

COL_START_OF_MONTH: str = "Start of Month"
COL_ITEM: str = "Item"
COL_ITEM_DESC: str = "Item Description"
# Inserted BEFORE Customer Name (planner spec, June 2026 cycle): the
# detail CSV does NOT publish Customer No; we back-fill it from the
# ship-to-sites dim for Base Plan rows and from IBP Shipments for
# Actual rows.  R&O rows leave it blank.
COL_CUSTOMER_NO: str = "Customer No"
COL_CUSTOMER_NAME: str = "Customer Name"
COL_PARTY_SITE_NUMBER: str = "Party Site Number"
COL_DEMAND_LBS: str = "Demand Plan Pounds"
COL_FORECAST_TYPE: str = "Forecast Type"
COL_PORTFOLIO_MAJOR: str = "Portfolio Major"
COL_PORTFOLIO_MINOR: str = "Portfolio Minor"
COL_SUPPLY_FORMAT: str = "Supply Format"
COL_CORPORATE_GROUP: str = "Corporate Group"

OUTPUT_COLUMNS: tuple[str, ...] = (
    COL_START_OF_MONTH, COL_ITEM, COL_ITEM_DESC,
    COL_CUSTOMER_NO, COL_CUSTOMER_NAME,
    COL_PARTY_SITE_NUMBER, COL_DEMAND_LBS, COL_FORECAST_TYPE,
    COL_PORTFOLIO_MAJOR, COL_PORTFOLIO_MINOR, COL_SUPPLY_FORMAT,
    COL_CORPORATE_GROUP,
)

# Markers the dispatch logic uses to choose between the exact
# customer_num lookup (Actual + Base Plan) and the fuzzy Customer Name
# lookup (R&O).  Compared case-insensitively / whitespace-stripped, so
# minor casing drift from the upstream CSV is harmless.
FORECAST_TYPE_ACTUAL: str = "Actual"
FORECAST_TYPE_BASE_PLAN: str = "Base Plan"
# Set of forecast-type values that resolve their Corporate Group via
# the exact ``customer_num`` join (post-normalise: casefold + strip).
_EXACT_FORECAST_TYPES_CF: frozenset[str] = frozenset({
    FORECAST_TYPE_ACTUAL.casefold(),
    FORECAST_TYPE_BASE_PLAN.casefold(),
})

# Default fuzzy cutoff.  88 was the planner-acceptable threshold from
# manual spot-checks of the dp_dimcustomernames sample on file.  Lower
# values picked up too many cross-region near-matches (e.g.
# "PALACE INDUSTRIES … ROGUE" ↔ "ROGUE CREAMERY"); higher values dropped
# routine spelling drift ("ALBERTSON'S" vs "ALBERTSONS").
DEFAULT_FUZZY_THRESHOLD: int = 88

# Words stripped during fuzzy normalisation (case-insensitive, on word
# boundaries only).  Keep them ordered by likelihood for trivial speed.
_STOPLIST: frozenset[str] = frozenset({
    "INC", "LLC", "CORP", "CO", "LTD", "LP", "LLP",
    "COMPANY", "CORPORATION", "INTL", "INTERNATIONAL",
    "THE",
})


# ── Errors ────────────────────────────────────────────────────────────────────

class DemandItemCustomerError(RuntimeError):
    """Raised on any failure to read / build / save the enriched CSV.

    Wraps the lower-level :class:`LakehouseIOError` so the page renders a
    single, scope-aware banner.
    """


# ── Public result type ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DemandOrderItemCustomerBuild:
    """Output of :func:`build_demand_order_item_customer`.

    ``df``         — the saved-CSV-shape frame (one row per detail row
                     OR synthesised Actual row), with ``Corporate Group``
                     attached.
    ``warnings``   — soft warnings to surface in the page (e.g. dim
                     table missing).  Empty tuple means everything went
                     fine.
    ``stats``      — small dict for the page's debug caption.
    """
    df: pd.DataFrame
    warnings: tuple[str, ...]
    stats: dict[str, int]


# ─────────────────────────────────────────────────────────────────────────────
# CY Actual months — the discriminator that drives the whole pipeline
# ─────────────────────────────────────────────────────────────────────────────

def compute_cy_actual_months(
    *,
    cy_full_year_months: Iterable[date],
    cy_ytg_start: date,
    cy_ytg_end: date,
) -> tuple[date, ...]:
    """Return the months inside CY Full Year but OUTSIDE the CY YTG window.

    The planner spec defines these as the "Actual" months — periods that
    have already shipped and therefore should be sourced from
    ``IBP Orders`` rather than the Base Plan / R&O placeholder rows.

    *cy_full_year_months* is taken as-is (the PLR module already produces
    a normalised first-of-month set).  *cy_ytg_start* / *cy_ytg_end* are
    normalised to first-of-month here so callers don't have to.
    """
    ytg_start = cy_ytg_start.replace(day=1)
    ytg_end = cy_ytg_end.replace(day=1)
    fy_months = {m.replace(day=1) if hasattr(m, "replace") else m for m in cy_full_year_months}
    return tuple(sorted(
        m for m in fy_months if not (ytg_start <= m <= ytg_end)
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Detail CSV fetch + save
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_detail_fetch(_signature: str) -> tuple[pd.DataFrame, Optional[str]]:
    """Cached read of ``qry_demand_item_customer_detail.csv``.

    Returns ``(df, etag)`` — native values only (Streamlit cache
    contract; same pattern as :mod:`data_sources.demand_summary`).  A
    missing blob raises :class:`DemandItemCustomerError`.
    """
    try:
        df, etag = read_csv(_SECRETS_SECTION, DETAIL_BLOB_PATH)
    except LakehouseIOError as exc:
        raise DemandItemCustomerError(
            f"Could not read 'Files/{DETAIL_BLOB_PATH}' from "
            f"Microsoft Fabric: {exc}"
        ) from exc
    if df is None:
        df = pd.DataFrame()
    return df, etag


def fetch_demand_item_customer_detail(
    *, force_refresh: bool = False,
) -> pd.DataFrame:
    """Return the latest ``qry_demand_item_customer_detail.csv`` as a DataFrame.

    Sized at ~tens of thousands of rows; cached for 60 minutes.  Pass
    ``force_refresh=True`` to bypass the cache (the page exposes this
    behind its "🔄 Refresh from Fabric" button).
    """
    if force_refresh:
        _cached_detail_fetch.clear()
    df, _etag = _cached_detail_fetch("default")
    return df


def save_demand_order_item_customer(df: pd.DataFrame) -> str:
    """Overwrite the saved ``demand_order_item_customer.csv`` in Fabric.

    Writes *df* to ``Files/RO Tracking/Demand Plan/
    demand_order_item_customer.csv`` (create-or-overwrite, no ETag
    guard) — same contract as :func:`save_demand_plan_comparison`.

    Raises :class:`DemandItemCustomerError` on any write failure so the
    page can render one clean banner.  Returns the destination blob
    path (for the auto-save success log).
    """
    if df is None or df.empty:
        raise DemandItemCustomerError(
            "Nothing to save — the enriched frame is empty.  This usually "
            "means both the detail CSV and IBP Orders returned no rows "
            "for the selected filters."
        )
    try:
        write_csv(
            _SECRETS_SECTION, DEMAND_ORDER_ITEM_CUSTOMER_BLOB_PATH, df,
            etag=None,
        )
    except LakehouseIOError as exc:
        raise DemandItemCustomerError(
            f"Could not save 'Files/{DEMAND_ORDER_ITEM_CUSTOMER_BLOB_PATH}': "
            f"{exc}"
        ) from exc
    return DEMAND_ORDER_ITEM_CUSTOMER_BLOB_PATH


# ─────────────────────────────────────────────────────────────────────────────
# Detail-CSV normalisation
# ─────────────────────────────────────────────────────────────────────────────
#
# The CSV occasionally arrives with thousands-separators in
# ``Demand Plan Pounds`` and mixed date encodings in ``Start of Month``
# (string + Excel serial), so coerce both columns up-front to the same
# canonical types we use everywhere else (date object + float lbs).

def _normalise_detail(df: pd.DataFrame) -> pd.DataFrame:
    """Trim + coerce the detail CSV in place-ish (returns a new frame).

    Required columns are probed defensively — a missing column logs a
    WARNING and the whole frame falls through with the missing column
    appearing as ``NaN`` so the downstream concat doesn't blow up.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=list(OUTPUT_COLUMNS[:-1]))

    out = pd.DataFrame()
    out[COL_START_OF_MONTH] = (
        _vectorised_start_of_month(df[COL_START_OF_MONTH])
        if COL_START_OF_MONTH in df.columns else pd.Series([None] * len(df))
    )
    out[COL_ITEM] = (
        _vectorised_item_key(df[COL_ITEM])
        if COL_ITEM in df.columns else ""
    )
    out[COL_ITEM_DESC] = (
        _vectorised_clean_str(df[COL_ITEM_DESC])
        if COL_ITEM_DESC in df.columns else ""
    )
    # Customer No is NOT published by the upstream CSV today; we keep
    # the column shape consistent and back-fill from the ship-to-sites
    # dim (Base Plan rows) or shipments (Actual rows) downstream.  When
    # a future CSV revision starts including the column we surface it
    # verbatim instead of overwriting.
    out[COL_CUSTOMER_NO] = (
        _vectorised_clean_str(df[COL_CUSTOMER_NO])
        if COL_CUSTOMER_NO in df.columns else ""
    )
    out[COL_CUSTOMER_NAME] = (
        _vectorised_clean_str(df[COL_CUSTOMER_NAME])
        if COL_CUSTOMER_NAME in df.columns else ""
    )
    out[COL_PARTY_SITE_NUMBER] = (
        _vectorised_clean_str(df[COL_PARTY_SITE_NUMBER])
        if COL_PARTY_SITE_NUMBER in df.columns else ""
    )
    if COL_DEMAND_LBS in df.columns:
        out[COL_DEMAND_LBS] = pd.to_numeric(
            df[COL_DEMAND_LBS].astype("string").str.replace(",", "", regex=False),
            errors="coerce",
        ).fillna(0.0)
    else:
        out[COL_DEMAND_LBS] = 0.0
    out[COL_FORECAST_TYPE] = (
        _vectorised_clean_str(df[COL_FORECAST_TYPE])
        if COL_FORECAST_TYPE in df.columns else ""
    )
    out[COL_PORTFOLIO_MAJOR] = (
        _vectorised_clean_str(df[COL_PORTFOLIO_MAJOR])
        if COL_PORTFOLIO_MAJOR in df.columns else ""
    )
    out[COL_PORTFOLIO_MINOR] = (
        _vectorised_clean_str(df[COL_PORTFOLIO_MINOR])
        if COL_PORTFOLIO_MINOR in df.columns else ""
    )
    out[COL_SUPPLY_FORMAT] = (
        _vectorised_clean_str(df[COL_SUPPLY_FORMAT])
        if COL_SUPPLY_FORMAT in df.columns else ""
    )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CY-Actual replacement: drop original rows + synthesise from IBP Orders
# ─────────────────────────────────────────────────────────────────────────────

def _synthesise_actual_rows(
    shipments_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    cy_actual_months: Iterable[date],
) -> pd.DataFrame:
    """Build the "Actual" replacement rows from IBP Shipments.

    Filters shipments to *cy_actual_months*, attaches PDH dims via the
    standard item-key join, then renames into the same wide schema the
    detail CSV uses.  Forecast Type is hard-coded to ``"Actual"`` and
    ``Party Site Number`` is left blank (planner spec).  ``Customer
    No`` is preserved verbatim from the shipments row so the
    downstream Corporate Group attach can join on it directly.

    Returns an empty frame with the correct columns when there is no
    overlap — so the concat downstream is always safe.

    The probe order for the quantity column is shipments-first
    (``Shipped Qty lbs``) with the orders-naming family as a back-stop;
    that way a partial schema unification upstream (planner has
    discussed renaming the column once shipments-as-Actual goes live)
    keeps working without a code change here.
    """
    months = tuple(sorted(set(cy_actual_months)))
    empty = pd.DataFrame(columns=list(OUTPUT_COLUMNS[:-1]))
    if shipments_df is None or shipments_df.empty or not months:
        return empty

    item_col = _resolve_column(shipments_df, _IBP_ITEM_CANDIDATES)
    month_col = _resolve_column(shipments_df, _IBP_MONTH_CANDIDATES)
    qty_col = (
        _resolve_column(shipments_df, _IBP_QTY_CANDIDATES)
        or _resolve_column(shipments_df, _IBP_ORDERED_QTY_CANDIDATES)
    )
    name_col = _resolve_column(shipments_df, _IBP_CUSTOMER_NAME_CANDIDATES)
    no_col = _resolve_column(shipments_df, _IBP_CUSTOMER_NO_CANDIDATES)
    if not (item_col and month_col and qty_col):
        logger.warning(
            "IBP Shipments missing required columns "
            "(item=%r, month=%r, qty=%r); synthesised Actual rows = 0.",
            item_col, month_col, qty_col,
        )
        return empty

    months_set = set(months)

    # Normalise month to date and filter — DO NOT reorder columns yet.
    work = shipments_df.copy()
    work["__month"] = _vectorised_start_of_month(work[month_col])
    work = work.loc[work["__month"].isin(months_set)].reset_index(drop=True)
    if work.empty:
        return empty

    # PDH dims via the canonical join (Item Description / PMaj / PMinor /
    # SFmt all come from PDH for the synthesised rows — shipments carries
    # no dims of its own).
    item_keys = _vectorised_item_key(work[item_col])
    slim = pd.DataFrame({
        "__item_key_in": item_keys.to_numpy(),
        "month": work["__month"].to_numpy(),
        "pounds": pd.to_numeric(work[qty_col], errors="coerce").fillna(0.0).to_numpy(),
        "customer_name": (
            _vectorised_clean_str(work[name_col]).to_numpy()
            if name_col else np.array([""] * len(work), dtype=object)
        ),
        "customer_no": (
            # Item-key normalisation (strip + zero-pad-stripped) is the
            # right tool here too — Customer No is treated everywhere
            # else (IBP Orders enrichment) as an item-key-style identifier.
            _vectorised_item_key(work[no_col]).to_numpy()
            if no_col else np.array([""] * len(work), dtype=object)
        ),
    })
    dim_frame = build_item_dim_frame(pdh_df)
    merged = _attach_dims(slim, slim["__item_key_in"], dim_frame)

    n = len(merged)
    out = pd.DataFrame({
        COL_START_OF_MONTH: merged["month"].to_numpy(),
        COL_ITEM: merged["__item_key_in"].to_numpy(),
        COL_ITEM_DESC: merged["desc"].to_numpy(),
        COL_CUSTOMER_NO: merged["customer_no"].to_numpy(),
        COL_CUSTOMER_NAME: merged["customer_name"].to_numpy(),
        COL_PARTY_SITE_NUMBER: [""] * n,
        COL_DEMAND_LBS: merged["pounds"].to_numpy(),
        COL_FORECAST_TYPE: [FORECAST_TYPE_ACTUAL] * n,
        COL_PORTFOLIO_MAJOR: merged["pmaj"].to_numpy(),
        COL_PORTFOLIO_MINOR: merged["pminor"].to_numpy(),
        COL_SUPPLY_FORMAT: merged["sfmt"].to_numpy(),
    })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Fuzzy match: Customer Name -> Corporate Group
# ─────────────────────────────────────────────────────────────────────────────

# A small set of substrings that signal "no usable corporate group" —
# applied AFTER stripping.  Matches the screenshot's "Blank" sentinel.
_CG_BLANK_TOKENS: frozenset[str] = frozenset({"", "blank", "nan", "none", "null"})


def _normalise_for_fuzzy(name: object) -> str:
    """Normalise a customer name for fuzzy comparison.

    Steps:
        1. uppercase + strip;
        2. replace any non-alphanumeric run with a single space;
        3. drop legal-suffix stoplist tokens on word boundaries;
        4. collapse whitespace.

    The same function is applied to BOTH sides of the fuzzy match so the
    comparison sees the same normal form regardless of input casing /
    punctuation / suffix drift.
    """
    if name is None:
        return ""
    try:
        if pd.isna(name):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(name).upper().strip()
    if not s:
        return ""
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    tokens = [t for t in s.split() if t and t not in _STOPLIST]
    return " ".join(tokens)


def _looks_blank(value: str) -> bool:
    """Return True when *value* is the literal blank sentinel."""
    return value.strip().casefold() in _CG_BLANK_TOKENS


def _build_customer_num_to_corp_group_lookup(
    customer_names_dim: Optional[pd.DataFrame],
) -> dict[str, str]:
    """Return ``customer_num → corporate_group`` from ``dp_dimcustomernames``.

    The single map is shared by every "exact" branch — Base Plan rows
    (after Party Site → customer_num translation), Actual rows, and the
    IBP Orders run-rate columns.  Keys are stripped strings; values
    that the dim flags as "Blank"/NaN/empty are dropped so a ``.map``
    fall-through naturally lands on the Customer Name fallback.
    """
    if customer_names_dim is None or customer_names_dim.empty:
        return {}
    num_col = _resolve_column(customer_names_dim, CUSTOMER_NUM_CANDIDATES)
    cg_col = _resolve_column(customer_names_dim, CORPORATE_GROUP_CANDIDATES)
    if not (num_col and cg_col):
        logger.warning(
            "dp_dimcustomernames missing required columns "
            "(customer_num=%r, corporate_group=%r); customer_num "
            "lookups will fall back to Customer Name.",
            num_col, cg_col,
        )
        return {}
    keys = customer_names_dim[num_col].astype(str).str.strip()
    vals = customer_names_dim[cg_col].astype(str).str.strip()
    keep = (keys != "") & ~vals.str.casefold().isin(_CG_BLANK_TOKENS)
    return (
        pd.DataFrame({"k": keys[keep], "v": vals[keep]})
        .drop_duplicates(subset="k", keep="last")
        .set_index("k")["v"]
        .to_dict()
    )


def _build_fuzzy_name_to_corp_group_lookup(
    customer_names_dim: Optional[pd.DataFrame],
    distinct_names: Iterable[str],
    *,
    threshold: int = DEFAULT_FUZZY_THRESHOLD,
) -> dict[str, str]:
    """Build a ``Customer Name → corporate_group`` map via fuzzy match.

    The expensive ``rapidfuzz`` work runs only against the DISTINCT
    input names supplied by the caller — typically just the R&O rows'
    Customer Names, which is a few hundred values at most.  Repeat
    look-ups inside the dispatcher then become O(1) dict hits.

    Returns an empty dict whenever the dim table is unusable / lacks
    the expected columns; the dispatcher falls back to the row's
    Customer Name verbatim in that case.
    """
    distinct = [n for n in (str(n).strip() for n in distinct_names) if n]
    if not distinct or customer_names_dim is None or customer_names_dim.empty:
        return {}

    name_col = _resolve_column(customer_names_dim, CUSTOMER_NAME_CANDIDATES)
    cg_col = _resolve_column(customer_names_dim, CORPORATE_GROUP_CANDIDATES)
    if not (name_col and cg_col):
        logger.warning(
            "dp_dimcustomernames missing required columns "
            "(customer_name=%r, corporate_group=%r); fuzzy lookups "
            "will fall back to Customer Name.",
            name_col, cg_col,
        )
        return {}

    # Normalised-name → corporate_group candidate index built ONCE.
    dim_names_raw = customer_names_dim[name_col].astype(str).tolist()
    dim_groups_raw = customer_names_dim[cg_col].astype(str).tolist()
    norm_to_group: dict[str, str] = {}
    for raw_name, raw_group in zip(dim_names_raw, dim_groups_raw):
        norm = _normalise_for_fuzzy(raw_name)
        if not norm or norm in norm_to_group:
            # ``in`` keeps the FIRST occurrence — dim row order wins on dupes.
            continue
        norm_to_group[norm] = "" if _looks_blank(raw_group) else raw_group.strip()
    if not norm_to_group:
        return {}

    candidates = list(norm_to_group.keys())

    # Lazy import keeps the module importable in environments that don't
    # have rapidfuzz installed (e.g. a stripped CI container).
    from rapidfuzz import fuzz, process

    out: dict[str, str] = {}
    for raw in distinct:
        norm = _normalise_for_fuzzy(raw)
        if not norm:
            out[raw] = ""
            continue
        match = process.extractOne(
            norm, candidates,
            scorer=fuzz.token_set_ratio,
            score_cutoff=threshold,
        )
        if match is None:
            out[raw] = ""
            continue
        out[raw] = norm_to_group.get(match[0], "")
    return out


def attach_corporate_group_by_forecast_type(
    unified_df: pd.DataFrame,
    customer_names_dim: Optional[pd.DataFrame],
    *,
    fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Resolve Corporate Group per row using a forecast-type-aware lookup.

    Planner spec (June 2026 cycle):

    * **Actual** + **Base Plan** rows → exact join
      ``Customer No`` → ``dp_dimcustomernames.customer_num`` →
      ``corporate_group``.
    * **R&O** rows (anything that is neither "Actual" nor "Base Plan")
      → fuzzy ``Customer Name`` match.  R&O rows have no usable
      Customer No.

    Universal fallback: when the matched ``corporate_group`` is empty /
    NaN / literal "Blank" or no match is found, ``Corporate Group =
    Customer Name`` for that row.

    Returns ``(annotated_df, stats)`` where *stats* contains row counts
    per branch + match counts so the caller can render an audit
    caption.
    """
    out = unified_df.copy()
    n = len(out)
    if n == 0:
        out[COL_CORPORATE_GROUP] = []
        return out, {
            "n_total": 0, "n_exact": 0, "n_exact_matched": 0,
            "n_fuzzy": 0, "n_fuzzy_matched": 0,
        }

    # Universal fallback values (typed object for downstream np.where).
    fallback_names = (
        out[COL_CUSTOMER_NAME].astype("string").fillna("").str.strip()
    )

    # Forecast-type dispatch mask.  Case-insensitive + whitespace
    # tolerant so harmless drift (e.g. "BASE PLAN" upstream) keeps
    # working without a code change.
    forecast_cf = (
        out[COL_FORECAST_TYPE].astype("string").fillna("").str.strip().str.casefold()
    )
    exact_mask = forecast_cf.isin(_EXACT_FORECAST_TYPES_CF).to_numpy()
    fuzzy_mask = ~exact_mask

    # ── Exact branch ────────────────────────────────────────────────
    exact_lookup = _build_customer_num_to_corp_group_lookup(customer_names_dim)
    customer_no_keys = (
        out[COL_CUSTOMER_NO].astype("string").fillna("").str.strip()
    )
    exact_resolved = customer_no_keys.map(exact_lookup).fillna("")
    n_exact = int(exact_mask.sum())
    n_exact_matched = int(
        (pd.Series(exact_mask) & (exact_resolved.str.strip() != "")).sum()
    )

    # ── Fuzzy branch ────────────────────────────────────────────────
    fuzzy_names = out.loc[fuzzy_mask, COL_CUSTOMER_NAME].astype(str).unique().tolist()
    fuzzy_lookup = _build_fuzzy_name_to_corp_group_lookup(
        customer_names_dim, fuzzy_names, threshold=fuzzy_threshold,
    )
    fuzzy_resolved = fallback_names.map(fuzzy_lookup).fillna("")
    n_fuzzy = int(fuzzy_mask.sum())
    n_fuzzy_matched = int(
        (pd.Series(fuzzy_mask) & (fuzzy_resolved.str.strip() != "")).sum()
    )

    # ── Stitch ──────────────────────────────────────────────────────
    candidate = np.where(
        exact_mask, exact_resolved.to_numpy(), fuzzy_resolved.to_numpy(),
    )
    final = np.where(
        pd.Series(candidate).fillna("").astype(str).str.strip().to_numpy() == "",
        fallback_names.to_numpy(),
        candidate,
    )
    out[COL_CORPORATE_GROUP] = final
    return out, {
        "n_total": n,
        "n_exact": n_exact,
        "n_exact_matched": n_exact_matched,
        "n_fuzzy": n_fuzzy,
        "n_fuzzy_matched": n_fuzzy_matched,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Customer No back-fill via the ship-to-sites dim (Base Plan rows only)
# ─────────────────────────────────────────────────────────────────────────────

def attach_customer_no_from_ship_to_sites(
    unified_df: pd.DataFrame,
    ship_to_sites_dim: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Back-fill ``Customer No`` on Base Plan rows from the ship-to-sites dim.

    The upstream ``qry_demand_item_customer_detail.csv`` does NOT
    publish ``Customer No``; planners only get ``Party Site Number``
    there.  We translate that to the ``customer_num`` the rest of the
    Fabric tables already use:

        unified.Party Site Number  →  dp_dimshiptosites.party_site_code
                                  →  dp_dimshiptosites.customer_num
                                  →  unified.Customer No

    Only Base Plan rows are touched — Actual rows already carry
    ``Customer No`` from the shipments source, and R&O rows are left
    blank (planner spec: Customer No is N/A for R&O).

    A row whose Party Site Number is missing from the dim keeps its
    existing (blank) ``Customer No``; the universal fallback in
    :func:`attach_corporate_group_by_forecast_type` then resolves its
    Corporate Group to the row's Customer Name.
    """
    if unified_df is None or unified_df.empty:
        return unified_df.copy() if unified_df is not None else pd.DataFrame()

    out = unified_df.copy()
    if ship_to_sites_dim is None or ship_to_sites_dim.empty:
        # Nothing to fill — leave Customer No as-is (blank for Base
        # Plan rows; the universal fallback handles the rest).
        return out

    ps_col = _resolve_column(ship_to_sites_dim, _STS_PARTY_SITE_CANDIDATES)
    cn_col = _resolve_column(ship_to_sites_dim, _STS_CUSTOMER_NUM_CANDIDATES)
    if not (ps_col and cn_col):
        logger.warning(
            "dp_dimshiptosites missing required columns "
            "(party_site=%r, customer_num=%r); Base Plan rows will "
            "keep blank Customer No.",
            ps_col, cn_col,
        )
        return out

    # Build the party-site → customer_num lookup once.
    keys = ship_to_sites_dim[ps_col].astype(str).str.strip()
    vals = ship_to_sites_dim[cn_col].astype(str).str.strip()
    keep = (keys != "") & (vals != "") & (vals.str.casefold() != "nan")
    lookup = (
        pd.DataFrame({"k": keys[keep], "v": vals[keep]})
        .drop_duplicates(subset="k", keep="last")
        .set_index("k")["v"]
        .to_dict()
    )

    # Only Base Plan rows opt in.  We mask the assignment so an Actual
    # row that already carries a Customer No is never overwritten with
    # a stale lookup hit.
    forecast_cf = (
        out[COL_FORECAST_TYPE].astype("string").fillna("").str.strip().str.casefold()
    )
    base_plan_mask = forecast_cf.eq(FORECAST_TYPE_BASE_PLAN.casefold())

    party_keys = (
        out[COL_PARTY_SITE_NUMBER].astype("string").fillna("").str.strip()
    )
    resolved = party_keys.map(lookup).fillna("").astype("object")
    existing = out[COL_CUSTOMER_NO].astype("string").fillna("").to_numpy()
    out[COL_CUSTOMER_NO] = np.where(
        base_plan_mask.to_numpy() & (resolved.to_numpy() != ""),
        resolved.to_numpy(),
        existing,
    )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Orders-side corporate-group attach (exact join on Customer No)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OrdersEnrichmentStats:
    """Audit counts surfaced to the page when enriching IBP Orders.

    ``n_in`` is the row count of the slim Orders frame as fetched from
    Fabric; ``n_enriched`` is the row count after the PDH + dim enrich
    pipeline.  Any difference is a "drop due to variable mismatch" —
    planner-specified visible warning trigger.
    """
    n_in: int
    n_enriched: int

    @property
    def n_dropped(self) -> int:
        return max(0, self.n_in - self.n_enriched)


def attach_corporate_group_to_orders(
    orders_df: Optional[pd.DataFrame],
    customer_names_dim: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Add ``Corporate Group`` to an enriched IBP Orders frame.

    Look-up: ``orders.Customer No`` → ``dp_dimcustomernames.customer_num``
    → ``corporate_group``.  Blank / NaN / "Blank" matches fall back to
    the customer name (planner spec — never write a literal "Blank"
    into the table).

    Works on EITHER the raw slim Orders projection (columns
    ``Customer No`` / ``Customer Name``) OR the already-enriched frame
    produced by :func:`enrich_ibp_orders_df` (columns ``customer_no`` /
    ``customer_name``).  This way the page can attach the corporate
    group either before or after the PDH-dim enrichment without
    juggling intermediate frames.

    Returns a NEW frame with the original columns plus
    ``customer_corp_group``.  When *orders_df* already carries that
    column it is overwritten so repeated calls are idempotent.
    """
    if orders_df is None or orders_df.empty:
        return orders_df.copy() if orders_df is not None else pd.DataFrame()

    out = orders_df.copy()
    cust_no_col = _resolve_column(out, _IBP_CUSTOMER_NO_CANDIDATES)
    if cust_no_col is None and "customer_no" in out.columns:
        cust_no_col = "customer_no"
    cust_name_col = _resolve_column(out, _IBP_CUSTOMER_NAME_CANDIDATES)
    # Match the column name the rest of the pipeline already uses
    # internally for orders (``customer_name``, set up by ``_enrich_ibp``).
    if cust_name_col is None and "customer_name" in out.columns:
        cust_name_col = "customer_name"

    fallback = (
        _vectorised_clean_str(out[cust_name_col])
        if cust_name_col else pd.Series([""] * len(out), dtype="object")
    )

    if (
        cust_no_col is None
        or customer_names_dim is None
        or customer_names_dim.empty
    ):
        out["customer_corp_group"] = fallback.to_numpy()
        return out

    # Build the customer_num → corporate_group lookup from
    # dp_dimcustomernames (the single source of truth for Corporate
    # Group as of the June 2026 planner spec).  Sharing the helper
    # used by the unified frame keeps the two attach paths in lock-step.
    lookup = _build_customer_num_to_corp_group_lookup(customer_names_dim)
    if not lookup:
        out["customer_corp_group"] = fallback.to_numpy()
        return out

    keys = out[cust_no_col].astype(str).str.strip()
    found = keys.map(lookup).fillna("").astype("object")
    final = np.where(found.to_numpy() == "", fallback.to_numpy(), found.to_numpy())
    out["customer_corp_group"] = final
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Top-level enrichment
# ─────────────────────────────────────────────────────────────────────────────

def build_demand_order_item_customer(
    *,
    detail_df: Optional[pd.DataFrame],
    shipments_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    customer_names_dim: Optional[pd.DataFrame],
    ship_to_sites_dim: Optional[pd.DataFrame],
    cy_actual_months: Iterable[date],
    fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD,
) -> DemandOrderItemCustomerBuild:
    """Run the full enrichment pipeline.

    Steps (matches the planner's spec verbatim — June 2026 cycle):

        1. Normalise the detail CSV (coerce dates + lbs).
        2. Drop every detail row whose ``Start of Month`` falls inside
           the CY Actual Months window.
        3. Pull IBP **Shipments** for those same months, attach PDH
           dims, and emit them as ``Forecast Type = "Actual"`` rows —
           shipments' ``Customer No`` is preserved verbatim.
        4. Concat (filtered detail) ⊕ (Actual rows).
        5. Back-fill ``Customer No`` on Base Plan rows via the
           ``Party Site Number`` → ``dp_dimshiptosites`` →
           ``customer_num`` lookup.
        6. Resolve ``Corporate Group`` per row using the forecast-type
           dispatcher (exact ``customer_num`` join for Actual + Base
           Plan; fuzzy ``Customer Name`` match for R&O).  Blanks fall
           back to the Customer Name itself.

    The returned :class:`DemandOrderItemCustomerBuild` carries the
    unified frame in *saved-CSV column order*, plus stats + soft
    warnings the page can render as captions.
    """
    warnings: list[str] = []

    detail_norm = _normalise_detail(detail_df)
    actual_set = set(cy_actual_months)
    if actual_set and not detail_norm.empty:
        kept_mask = ~detail_norm[COL_START_OF_MONTH].isin(actual_set)
        detail_kept = detail_norm.loc[kept_mask].reset_index(drop=True)
        n_dropped = int((~kept_mask).sum())
    else:
        detail_kept = detail_norm
        n_dropped = 0

    actual_rows = _synthesise_actual_rows(shipments_df, pdh_df, actual_set)
    n_actual = len(actual_rows)

    # Drop empty frames before concat to avoid the pandas "concat with
    # all-NA columns" FutureWarning (and to make the dtype of the
    # surviving frame the one that wins).
    pieces = [f for f in (detail_kept, actual_rows) if f is not None and not f.empty]
    if pieces:
        unified = pd.concat(pieces, ignore_index=True, sort=False)
    else:
        unified = pd.DataFrame(columns=list(OUTPUT_COLUMNS[:-1]))
    # Re-order to the canonical schema (minus Corporate Group, added
    # below) so we never accidentally write columns out of order.
    for col in OUTPUT_COLUMNS[:-1]:
        if col not in unified.columns:
            unified[col] = ""
    unified = unified.loc[:, list(OUTPUT_COLUMNS[:-1])]

    # Back-fill Customer No on Base Plan rows (upstream CSV doesn't
    # publish it) BEFORE the corp-group attach — the dispatcher's
    # exact branch needs it populated to score the Base Plan rows.
    unified = attach_customer_no_from_ship_to_sites(unified, ship_to_sites_dim)
    n_base_plan_filled_customer_no = (
        int(
            (
                unified[COL_FORECAST_TYPE].astype(str).str.strip().str.casefold()
                == FORECAST_TYPE_BASE_PLAN.casefold()
            ).sum()
            and (unified[COL_CUSTOMER_NO].astype(str).str.strip() != "").sum()
        )
        if not unified.empty else 0
    )

    annotated, attach_stats = attach_corporate_group_by_forecast_type(
        unified, customer_names_dim, fuzzy_threshold=fuzzy_threshold,
    )

    # Soft warnings the page renders verbatim under the section header.
    if customer_names_dim is None or customer_names_dim.empty:
        warnings.append(
            "Corporate Group resolution skipped — `dbo.dp_dimcustomernames` "
            "is unavailable.  Every row's Corporate Group falls back to "
            "Customer Name."
        )
    else:
        if attach_stats["n_exact"] and attach_stats["n_exact_matched"] == 0:
            warnings.append(
                f"0 of {attach_stats['n_exact']} Actual / Base Plan rows "
                "matched any `dp_dimcustomernames.customer_num`.  Their "
                "Corporate Group falls back to Customer Name."
            )
        if attach_stats["n_fuzzy"] and attach_stats["n_fuzzy_matched"] == 0:
            warnings.append(
                f"0 of {attach_stats['n_fuzzy']} R&O rows matched any "
                "`dp_dimcustomernames.customer_name` (fuzzy threshold "
                f"{fuzzy_threshold}).  Their Corporate Group falls back "
                "to Customer Name."
            )
    if ship_to_sites_dim is None or ship_to_sites_dim.empty:
        warnings.append(
            "Customer No back-fill skipped — `dbo.dp_dimshiptosites` is "
            "unavailable.  Base Plan rows will have a blank Customer No "
            "and their Corporate Group falls back to Customer Name."
        )

    annotated = annotated.loc[:, list(OUTPUT_COLUMNS)].reset_index(drop=True)
    stats = {
        "n_detail_in": int(len(detail_norm)),
        "n_detail_kept": int(len(detail_kept)),
        "n_detail_dropped_in_cy_actual": n_dropped,
        "n_actual_synthesised": n_actual,
        "n_total_output": int(len(annotated)),
        "n_base_plan_customer_no_filled": n_base_plan_filled_customer_no,
        **attach_stats,
    }
    return DemandOrderItemCustomerBuild(
        df=annotated, warnings=tuple(warnings), stats=stats,
    )


# ─────────────────────────────────────────────────────────────────────────────
# PLR consumer: convert saved-CSV shape -> long shape for the table builder
# ─────────────────────────────────────────────────────────────────────────────

def prepare_demand_long_for_plr(
    raw: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Return a tidy long-format frame the Product Line Review builder consumes.

    Output columns::

        month │ pounds │ pmaj │ sfmt │ pminor │ brand │
        customer │ customer_corp_group │ forecast_type

    Brand is derived from PDH via Item join (first two chars of
    ``Item Description`` → ``DG`` ⇒ Branded, else Private — same rule the
    rest of the page uses).  ``customer_corp_group`` is taken verbatim
    from the saved CSV's ``Corporate Group`` column, which has already
    been fuzzy-resolved by :func:`build_demand_order_item_customer`.

    Index discipline mirrors :func:`prepare_ibp_base_plan_long`: every
    Series is converted to a numpy array before the final
    ``pd.DataFrame`` constructor so a non-contiguous source index
    cannot silently NaN-fill dim columns via index alignment.
    """
    empty_cols = [
        "month", "pounds", "pmaj", "sfmt", "pminor", "brand",
        "customer", "customer_corp_group", "forecast_type",
    ]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=empty_cols)

    work = raw.reset_index(drop=True)

    months = (
        _vectorised_start_of_month(work[COL_START_OF_MONTH])
        if COL_START_OF_MONTH in work.columns
        else pd.Series([None] * len(work))
    )
    pounds = (
        pd.to_numeric(
            work[COL_DEMAND_LBS].astype("string").str.replace(",", "", regex=False),
            errors="coerce",
        ).fillna(0.0)
        if COL_DEMAND_LBS in work.columns
        else pd.Series([0.0] * len(work))
    )
    pmaj = (
        _vectorised_clean_str(work[COL_PORTFOLIO_MAJOR])
        if COL_PORTFOLIO_MAJOR in work.columns
        else pd.Series([""] * len(work), dtype="object")
    )
    sfmt = (
        _vectorised_clean_str(work[COL_SUPPLY_FORMAT])
        if COL_SUPPLY_FORMAT in work.columns
        else pd.Series([""] * len(work), dtype="object")
    )
    pminor = (
        _vectorised_clean_str(work[COL_PORTFOLIO_MINOR])
        if COL_PORTFOLIO_MINOR in work.columns
        else pd.Series([""] * len(work), dtype="object")
    )
    desc = (
        _vectorised_clean_str(work[COL_ITEM_DESC])
        if COL_ITEM_DESC in work.columns
        else pd.Series([""] * len(work), dtype="object")
    )
    customer = (
        _vectorised_clean_str(work[COL_CUSTOMER_NAME])
        if COL_CUSTOMER_NAME in work.columns
        else pd.Series([""] * len(work), dtype="object")
    )
    customer_corp_group = (
        _vectorised_clean_str(work[COL_CORPORATE_GROUP])
        if COL_CORPORATE_GROUP in work.columns
        else customer
    )
    forecast_type = (
        _vectorised_clean_str(work[COL_FORECAST_TYPE])
        if COL_FORECAST_TYPE in work.columns
        else pd.Series([""] * len(work), dtype="object")
    )

    # Brand: trust the description on the row when available; fall back
    # to a PDH-by-item lookup so old rows that came in without an
    # ``Item Description`` still get a brand.  The PDH lookup is the
    # same vectorised join the rest of the page uses.
    brand_from_desc = _vectorised_brand(desc)
    if COL_ITEM in work.columns:
        item_keys = _vectorised_item_key(work[COL_ITEM])
        dim_frame = build_item_dim_frame(pdh_df)
        merged = _attach_dims(
            pd.DataFrame({"__item_key_in": item_keys.to_numpy()}),
            item_keys, dim_frame,
        )
        brand_from_pdh = merged["brand"].to_numpy()
        # When the row's own description gave us "Private" (i.e. the
        # description was blank), prefer the PDH-derived brand — that's
        # the only meaningful signal we have.
        desc_nonempty = desc.astype(str).str.strip() != ""
        brand_final = np.where(
            desc_nonempty.to_numpy(),
            brand_from_desc.to_numpy(),
            brand_from_pdh,
        )
    else:
        brand_final = brand_from_desc.to_numpy()

    # ``customer_corp_group`` falls back to Customer Name verbatim if
    # the saved CSV had a blank in that cell (defensive — the build
    # function already populates this column non-empty, but we re-apply
    # the rule so legacy CSVs round-trip cleanly).
    ccg_arr = customer_corp_group.to_numpy()
    cust_arr = customer.to_numpy()
    ccg_final = np.where(
        pd.Series(ccg_arr).fillna("").astype(str).str.strip().to_numpy() == "",
        cust_arr,
        ccg_arr,
    )

    out = pd.DataFrame({
        "month": months.to_numpy(),
        "pounds": pounds.to_numpy(),
        "pmaj": pmaj.to_numpy(),
        "sfmt": sfmt.to_numpy(),
        "pminor": pminor.to_numpy(),
        "brand": brand_final,
        "customer": cust_arr,
        "customer_corp_group": ccg_final,
        "forecast_type": forecast_type.to_numpy(),
    })
    return out.dropna(subset=["month"]).reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Filter discovery — driven by the SAVED CSV (planner spec)
# ─────────────────────────────────────────────────────────────────────────────

def list_filter_values_from_demand(
    df: Optional[pd.DataFrame],
) -> dict[str, list[str]]:
    """Return distinct Portfolio Major / Supply Format / Portfolio Minor.

    The planner asked that the PM dropdown, per-PM SFmt cascade, and row
    leaves all reference the unique values found in
    ``demand_order_item_customer.csv`` (not PDH).  Brands are still
    derived from PDH via Item Description — the saved CSV doesn't carry
    a Brand column.
    """
    if df is None or df.empty:
        return {"portfolio_major": [], "supply_format": [], "portfolio_minor": []}

    def _uniques(col: str) -> list[str]:
        if col not in df.columns:
            return []
        return sorted({
            v for v in df[col].astype(str).str.strip().tolist()
            if v
        })

    return {
        "portfolio_major": _uniques(COL_PORTFOLIO_MAJOR),
        "supply_format": _uniques(COL_SUPPLY_FORMAT),
        "portfolio_minor": _uniques(COL_PORTFOLIO_MINOR),
    }


def list_filter_values_for_pmaj_from_demand(
    df: Optional[pd.DataFrame],
    portfolio_major: str,
) -> dict[str, list[str]]:
    """Return Supply Format + Portfolio Minor restricted to *portfolio_major*.

    Mirrors :func:`product_line_review.list_pdh_filter_values_for_pmaj`
    but sourced from the saved CSV so the cascade matches whatever rows
    the planner is actually looking at.
    """
    if (
        df is None or df.empty
        or COL_PORTFOLIO_MAJOR not in df.columns
        or not portfolio_major.strip()
    ):
        return {"supply_format": [], "portfolio_minor": []}

    pm_cf = portfolio_major.strip().casefold()
    sub = df.loc[
        df[COL_PORTFOLIO_MAJOR].astype(str).str.strip().str.casefold() == pm_cf
    ]
    return {
        "supply_format": sorted({
            v for v in sub[COL_SUPPLY_FORMAT].astype(str).str.strip().tolist()
            if v
        }),
        "portfolio_minor": sorted({
            v for v in sub[COL_PORTFOLIO_MINOR].astype(str).str.strip().tolist()
            if v
        }),
    }


__all__ = [
    # Errors + types.
    "DemandItemCustomerError",
    "DemandOrderItemCustomerBuild",
    "OrdersEnrichmentStats",
    # Constants the page imports for headers / paths.
    "DETAIL_BLOB_PATH",
    "DEMAND_ORDER_ITEM_CUSTOMER_BLOB_PATH",
    "FORECAST_TYPE_ACTUAL",
    "FORECAST_TYPE_BASE_PLAN",
    "DEFAULT_FUZZY_THRESHOLD",
    "OUTPUT_COLUMNS",
    "COL_START_OF_MONTH",
    "COL_ITEM",
    "COL_ITEM_DESC",
    "COL_CUSTOMER_NO",
    "COL_CUSTOMER_NAME",
    "COL_PARTY_SITE_NUMBER",
    "COL_DEMAND_LBS",
    "COL_FORECAST_TYPE",
    "COL_PORTFOLIO_MAJOR",
    "COL_PORTFOLIO_MINOR",
    "COL_SUPPLY_FORMAT",
    "COL_CORPORATE_GROUP",
    # Fetch + save.
    "fetch_demand_item_customer_detail",
    "save_demand_order_item_customer",
    # Pipeline.
    "compute_cy_actual_months",
    "build_demand_order_item_customer",
    "attach_corporate_group_by_forecast_type",
    "attach_customer_no_from_ship_to_sites",
    "attach_corporate_group_to_orders",
    # PLR consumer.
    "prepare_demand_long_for_plr",
    "list_filter_values_from_demand",
    "list_filter_values_for_pmaj_from_demand",
    # Brand re-exports (so the page never reaches into demand_plan_comparison
    # just for these two constants — they ARE the canonical Brand labels).
    "BRAND_BRANDED",
    "BRAND_PRIVATE",
]
