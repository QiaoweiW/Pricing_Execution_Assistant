"""Holistic Demand Plan (APS) builder.

Produces ``qry_mgmt_plan_full_aps.csv`` — an APS-based analogue of
``qry_mgmt_plan_full.csv`` — by merging two legs, then enriching to the SAME
column structure as the IBP file with **Corporate Group appended last**::

    Start of Month | Item | Item Description | Party Site Number |
    Demand Plan Pounds | Forecast Type | Business Unit |
    Portfolio Major | Portfolio Minor | Supply Format | Corporate Group

Item Description + the Portfolio/Supply dims are resolved PDH-primary →
RO_Item_Master-fallback (the same coalesce the IBP pipeline uses); Party Site
Number is intentionally blank and Business Unit is ``"B2C"`` for every row.

Legs
----
* **APS Base Plan** — ``dbo.dp_factscurrentaps`` (via
  :func:`data_sources.plan_lift.fetch_factscurrentaps_holistic_df`):
  ``month → Start of Month``, ``item_code → Item``, native
  ``corporate_group_code → Corporate Group``, ``consensus_plan_lbs → Demand
  Plan Pounds``, ``Forecast Type = "APS Base Plan"``.
* **R&O** — ``RO_Seed.csv`` expanded through the *existing* pipeline
  automation (:func:`demand_plan_pipeline._build_tbl_ro_input` — the
  Format×Month 36-month expansion), then melted to long **keeping the
  seed's Customer** so each row can be attributed to a Corporate Group by
  a fuzzy match of Customer → ``dp_dimcustomernames`` (aligning R&O to the
  same Corporate Group vocabulary APS uses).  B2C-filtered exactly like the
  IBP ``qry_mgmt_plan_full`` R&O portion.  ``Forecast Type = "R&O"``.

This module is **pure-logic** for the builder (frames in → frame out, unit
testable) with a thin :func:`generate_holistic_demand_plan_aps` orchestrator
that reads the sources from Fabric.  The finished plan is **persisted** to
``Files/RO Tracking/APS/qry_mgmt_plan_full_aps.csv`` (see
:func:`save_aps_plan`); once it exists the page loads it via
:func:`load_persisted_aps_plan` instead of regenerating, so hand-applied
Corporate Group fixes survive.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

# Reuse the canonical RO expansion + helpers + OneLake locations from the
# IBP pipeline so the R&O leg matches qry_mgmt_plan_full byte-for-byte in
# spirit (one definition of the Format×Month math, no duplication).
from data_sources.demand_plan_pipeline import (
    _DEFAULT_ANCHOR_MONTH,
    _PDH_BLOB,
    _RO_ITEMS_BLOB,
    _RO_SEED_BLOB,
    _SECRETS_SECTION,
    _TBL_MONTHS_BLOB,
    _attach_item_attrs,
    _build_tbl_ro_input,
    _norm_item,
    _parse_dates,
    _ro_input_to_long,
)
from data_sources.customer_dims import (
    CORPORATE_GROUP_CANDIDATES,
    CUSTOMER_NAME_CANDIDATES,
    fetch_dp_dimcustomernames_df,
)
from data_sources.fabric_lakehouse_io import read_csv, write_csv
from data_sources.plan_lift import (
    COL_CORP_GROUP,
    COL_ITEM_CODE,
    COL_MONTH,
    COL_PLAN_LBS,
    CORP_GROUP_UNMAPPED,
    fetch_factscurrentaps_holistic_df,
)

logger = logging.getLogger(__name__)


# ── Output contract ──────────────────────────────────────────────────────────
# Mirrors the IBP qry_mgmt_plan_full.csv structure with Corporate Group LAST.
HDP_COL_MONTH: str      = "Start of Month"
HDP_COL_ITEM: str       = "Item"
HDP_COL_ITEM_DESC: str  = "Item Description"
HDP_COL_PARTY: str      = "Party Site Number"
HDP_COL_POUNDS: str     = "Demand Plan Pounds"
HDP_COL_FORECAST: str   = "Forecast Type"
HDP_COL_BU: str         = "Business Unit"
HDP_COL_PMAJ: str       = "Portfolio Major"
HDP_COL_PMIN: str       = "Portfolio Minor"
HDP_COL_SFMT: str       = "Supply Format"
HDP_COL_CORP: str       = "Corporate Group"
HDP_COLUMNS: tuple[str, ...] = (
    HDP_COL_MONTH, HDP_COL_ITEM, HDP_COL_ITEM_DESC, HDP_COL_PARTY,
    HDP_COL_POUNDS, HDP_COL_FORECAST, HDP_COL_BU,
    HDP_COL_PMAJ, HDP_COL_PMIN, HDP_COL_SFMT, HDP_COL_CORP,
)
# Working schema for the two legs BEFORE item-attribute enrichment: the
# grouping keys + Corporate Group + pounds.  Item Description / Portfolio dims
# / Party Site / Business Unit are attached AFTER grouping since each is a pure
# function of Item (or a constant), so they can't change group cardinality.
_CORE_COLUMNS: tuple[str, ...] = (
    HDP_COL_MONTH, HDP_COL_ITEM, HDP_COL_CORP, HDP_COL_POUNDS, HDP_COL_FORECAST,
)
HDP_BUSINESS_UNIT: str = "B2C"

# Persisted output location (OneLake Files/…) — fixed name so the existence
# check can find it and skip regeneration once it's written.
_APS_OUTPUT_BLOB: str = "RO Tracking/APS/qry_mgmt_plan_full_aps.csv"
APS_OUTPUT_NAME: str  = "qry_mgmt_plan_full_aps.csv"

FORECAST_APS_BASE_PLAN: str = "APS Base Plan"
FORECAST_R_AND_O: str       = "R&O"

# Customer → Corporate Group fuzzy-match log (one row per distinct RO_Seed
# Customer in the R&O output).  Surfaced so the planner can eyeball what
# matched and hand-fix the Unmapped ones in the downloaded qry_aps file.
MATCH_COL_CUSTOMER: str = "Customer"
MATCH_COL_CORP: str     = "Corporate Group"
MATCH_COL_STATUS: str   = "Match"
MATCH_COLUMNS: tuple[str, ...] = (MATCH_COL_CUSTOMER, MATCH_COL_CORP, MATCH_COL_STATUS)

MATCH_EXACT: str     = "Exact"
MATCH_FUZZY: str     = "Fuzzy"
MATCH_UNMAPPED: str  = "Unmapped"
MATCH_OVERRIDE: str  = "Override"  # planner set the Corporate Group by hand
# Sort order for the log — most actionable (needs a manual fix) first.
# Overrides sort with the confident Exact rows (the planner already fixed them).
_MATCH_RANK: dict[str, int] = {
    MATCH_UNMAPPED: 0, MATCH_FUZZY: 1, MATCH_OVERRIDE: 2, MATCH_EXACT: 3,
}
# Statuses that always warrant a manual look in the match log.
_REVIEW_STATUSES: frozenset[str] = frozenset({MATCH_FUZZY, MATCH_UNMAPPED, MATCH_OVERRIDE})

# Read source CSVs as raw strings (pipeline parity — we do our own typing).
_STR_READ_KW: dict = {"dtype": str, "keep_default_na": False}


class HolisticDemandPlanError(RuntimeError):
    """Raised when a source needed for the Holistic Demand Plan is missing."""


@dataclass(frozen=True)
class HolisticPlanResult:
    """Output of the Holistic Demand Plan (APS) build.

    Attributes
    ----------
    frame
        The unified :data:`HDP_COLUMNS` DataFrame (APS Base Plan + R&O).
    customer_match_log
        One row per distinct RO_Seed Customer in the R&O output
        (:data:`MATCH_COLUMNS`): the Corporate Group it resolved to and the
        match status (Exact / Fuzzy / Unmapped / Override), most-actionable
        first.
    aps_rows, ro_rows
        Row counts per leg (post-aggregation) for the run summary.
    aps_leg
        The grouped APS Base Plan leg, kept so a Customer→Corporate Group
        override can be re-applied to the R&O leg and re-merged in-memory
        (no Fabric re-fetch) — see :func:`apply_customer_corp_overrides`.
    ro_detail
        The B2C-filtered R&O rows *before* Corporate Group attribution and
        grouping (``Month | Item | Customer | Demand Plan Pounds``).  The
        override re-map keys off ``Customer`` here.
    item_attrs
        Distinct-Item lookup (``Item | Item Description | Portfolio Major |
        Portfolio Minor | Supply Format``) used to enrich the core legs into
        the full output shape.  Stored so an override re-map can re-assemble
        the frame in-memory without re-reading PDH / RO_Item_Master.
    """
    frame: pd.DataFrame
    customer_match_log: pd.DataFrame
    aps_rows: int
    ro_rows: int
    aps_leg: pd.DataFrame
    ro_detail: pd.DataFrame
    item_attrs: pd.DataFrame

    @property
    def unmapped_customers(self) -> tuple[str, ...]:
        """RO_Seed Customer names that did not resolve to a Corporate Group."""
        log = self.customer_match_log
        if log is None or log.empty:
            return ()
        unmapped = log.loc[log[MATCH_COL_STATUS] == MATCH_UNMAPPED, MATCH_COL_CUSTOMER]
        return tuple(unmapped.astype(str))


# ── Small helpers ────────────────────────────────────────────────────────────

class _NullLog:
    """No-op logger so :func:`_build_tbl_ro_input` can be reused headless."""

    def ok(self, *_a, **_k) -> None: ...
    def info(self, *_a, **_k) -> None: ...
    def warn(self, *_a, **_k) -> None: ...
    def err(self, *_a, **_k) -> None: ...


def _pick(df: Optional[pd.DataFrame], candidates: tuple[str, ...]) -> Optional[str]:
    """First candidate column present in *df* (or None)."""
    if df is None or df.empty:
        return None
    return next((c for c in candidates if c in df.columns), None)


def _empty_core() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_CORE_COLUMNS))


def _empty_output() -> pd.DataFrame:
    return pd.DataFrame(columns=list(HDP_COLUMNS))


def _concat_core(aps_leg: pd.DataFrame, ro_leg: pd.DataFrame) -> pd.DataFrame:
    """Concatenate the two core legs, skipping empties (no all-NA concat warn)."""
    legs = [leg for leg in (aps_leg, ro_leg) if leg is not None and not leg.empty]
    if not legs:
        return _empty_core()
    return pd.concat(legs, ignore_index=True)[list(_CORE_COLUMNS)]


def _group(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate a shaped leg to one core row per (Month, Item, Corp, Forecast)."""
    if df.empty:
        return _empty_core()
    out = (
        df.groupby(
            [HDP_COL_MONTH, HDP_COL_ITEM, HDP_COL_CORP, HDP_COL_FORECAST],
            as_index=False, dropna=False,
        )[HDP_COL_POUNDS].sum()
    )
    return out[list(_CORE_COLUMNS)]


# ── Fuzzy Customer → Corporate Group ─────────────────────────────────────────
#
# RO_Seed carries a Customer NAME (no party site), so R&O rows are attributed
# to a Corporate Group by matching that name against dp_dimcustomernames
# (customer_name → corporate_group).  We normalise both sides (casefold, strip
# punctuation, collapse whitespace) for an exact hit, then fall back to a
# containment match so "Albertsons Safeway" ≈ "Albertsons/Safeway", etc.

_NORM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_name(value: object) -> str:
    return _NORM_RE.sub(" ", str(value).casefold().strip()).strip()


def _build_name_to_corp(customer_names_df: Optional[pd.DataFrame]) -> dict[str, str]:
    """Return ``{normalised customer_name -> corporate_group}`` from the dim."""
    name_col = _pick(customer_names_df, CUSTOMER_NAME_CANDIDATES)
    corp_col = _pick(customer_names_df, CORPORATE_GROUP_CANDIDATES)
    if not name_col or not corp_col:
        return {}
    lookup: dict[str, str] = {}
    for raw_name, raw_corp in zip(
        customer_names_df[name_col], customer_names_df[corp_col],
    ):
        key = _normalize_name(raw_name)
        corp = str(raw_corp).strip()
        if key and corp:
            lookup[key] = corp  # last non-blank wins
    return lookup


def _resolve_one(
    raw: object, name_to_corp: dict[str, str], keys_by_len: list[str],
) -> tuple[str, str]:
    """Resolve one Customer name → ``(corporate_group, match_status)``.

    Exact match on the normalised name first; then a containment fallback
    (a dim name contained in the customer or vice-versa, longest dim name
    preferred); else ``(Unmapped)``.
    """
    norm = _normalize_name(raw)
    if not norm:
        return CORP_GROUP_UNMAPPED, MATCH_UNMAPPED
    corp = name_to_corp.get(norm)
    if corp is not None:
        return corp, MATCH_EXACT
    for key in keys_by_len:  # longest first → most specific containment wins
        if key in norm or norm in key:
            return name_to_corp[key], MATCH_FUZZY
    return CORP_GROUP_UNMAPPED, MATCH_UNMAPPED


def _resolve_customer_corp(
    customers: pd.Series, name_to_corp: dict[str, str],
) -> tuple[pd.Series, pd.DataFrame]:
    """Return ``(corp-group series aligned to *customers*, distinct match log)``.

    Resolves each distinct Customer once (cached), so the row-level mapping
    and the audit log stay perfectly consistent.  The log has one row per
    distinct Customer (:data:`MATCH_COLUMNS`), sorted most-actionable first.
    """
    keys_by_len = sorted(name_to_corp, key=len, reverse=True)
    # stripped customer -> (corp, status)
    resolved: dict[str, tuple[str, str]] = {}

    def _lookup(raw: object) -> str:
        cust = str(raw).strip()
        if cust not in resolved:
            resolved[cust] = _resolve_one(raw, name_to_corp, keys_by_len)
        return resolved[cust][0]

    corp_series = customers.map(_lookup)
    log = pd.DataFrame(
        [
            {MATCH_COL_CUSTOMER: cust, MATCH_COL_CORP: corp, MATCH_COL_STATUS: status}
            for cust, (corp, status) in resolved.items()
        ],
        columns=list(MATCH_COLUMNS),
    )
    if not log.empty:
        log = (
            log.assign(_rank=log[MATCH_COL_STATUS].map(_MATCH_RANK).fillna(9))
            .sort_values(["_rank", MATCH_COL_CUSTOMER])
            .drop(columns="_rank")
            .reset_index(drop=True)
        )
    return corp_series, log


# ── Leg builders ─────────────────────────────────────────────────────────────

def _build_aps_leg(aps_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Shape dp_factscurrentaps into the core schema ("APS Base Plan")."""
    if aps_df is None or aps_df.empty:
        return _empty_core()
    corp = aps_df[COL_CORP_GROUP].astype("string").str.strip()
    shaped = pd.DataFrame({
        HDP_COL_MONTH:    pd.to_datetime(aps_df[COL_MONTH], errors="coerce"),
        HDP_COL_ITEM:     _norm_item(aps_df[COL_ITEM_CODE]),
        HDP_COL_CORP:     corp.where(corp.astype(bool), CORP_GROUP_UNMAPPED)
                              .fillna(CORP_GROUP_UNMAPPED).astype(str),
        HDP_COL_POUNDS:   pd.to_numeric(
            aps_df[COL_PLAN_LBS].astype(str).str.replace(",", "", regex=False),
            errors="coerce"),
        HDP_COL_FORECAST: FORECAST_APS_BASE_PLAN,
    })
    shaped = shaped[(shaped[HDP_COL_POUNDS] > 0) & shaped[HDP_COL_MONTH].notna()]
    shaped[HDP_COL_MONTH] = shaped[HDP_COL_MONTH].dt.normalize()
    return _group(shaped)


def _filter_b2c(
    df: pd.DataFrame, pdh_df: Optional[pd.DataFrame], ro_master_df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Keep only B2C items — PDH Business Unit primary, RO_Item_Master → B2C.

    Mirrors the Business-Unit gate in ``demand_plan_pipeline`` so the R&O leg
    scopes identically to the IBP qry_mgmt_plan_full R&O portion.
    """
    bu = pd.Series(pd.NA, index=df.index, dtype="object")
    if pdh_df is not None and {"Item No", "Business Unit"}.issubset(pdh_df.columns):
        pdh_bu = (
            pdh_df[["Item No", "Business Unit"]].dropna(subset=["Item No"])
            .assign(**{
                "Item No": lambda d: _norm_item(d["Item No"]),
                "Business Unit": lambda d: d["Business Unit"].astype("string").str.strip(),
            })
            .drop_duplicates(subset=["Item No"], keep="first")
            .set_index("Item No")["Business Unit"]
        )
        bu = df[HDP_COL_ITEM].map(pdh_bu)
    # Fallback: items present in RO_Item_Master with no PDH BU are B2C.
    if ro_master_df is not None and "Item #" in ro_master_df.columns:
        ro_items = set(_norm_item(ro_master_df["Item #"]).dropna())
        fb = bu.isna() & df[HDP_COL_ITEM].isin(ro_items)
        bu = bu.where(~fb, "B2C")
    return df[bu == "B2C"]


def _empty_match_log() -> pd.DataFrame:
    return pd.DataFrame(columns=list(MATCH_COLUMNS))


# R&O detail schema (pre-grouping, Customer retained for corp attribution).
_RO_DETAIL_COLS: tuple[str, ...] = (
    HDP_COL_MONTH, HDP_COL_ITEM, "Customer", HDP_COL_POUNDS,
)


def _empty_ro_detail() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_RO_DETAIL_COLS))


def _build_ro_leg(
    ro_seed_df: Optional[pd.DataFrame],
    tbl_months_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    ro_master_df: Optional[pd.DataFrame],
    name_to_corp: dict[str, str],
    anchor_month: date,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Expand RO_Seed → long, B2C-filtered R&O detail + the match log.

    Returns ``(ro_detail, match_log)`` where *ro_detail* has
    :data:`_RO_DETAIL_COLS` (Customer retained, Corporate Group NOT yet
    applied and NOT grouped) so the caller — and later a planner override —
    can attribute the Corporate Group and aggregate via :func:`_ro_frame`.
    """
    if ro_seed_df is None or ro_seed_df.empty or tbl_months_df is None:
        return _empty_ro_detail(), _empty_match_log()

    # Reuse the pipeline's Format×Month 36-month expansion (keeps Customer)
    # AND its melt→month-map reshape, requesting the extra Customer id column
    # so each R&O row can carry its own Corporate Group.
    ro_input = _build_tbl_ro_input(ro_seed_df, anchor_month, _NullLog())
    qry_months = tbl_months_df.copy()
    qry_months["Start of Month"] = _parse_dates(qry_months["Start of Month"])
    qry_months["Month Number"] = qry_months["Month Number"].astype(str).str.strip()

    ro_long = _ro_input_to_long(
        ro_input, qry_months, extra_id_cols=("Customer",),
    ).rename(columns={"Start of Month": HDP_COL_MONTH})
    ro_long = ro_long[(ro_long[HDP_COL_POUNDS] > 0) & ro_long[HDP_COL_MONTH].notna()]
    if ro_long.empty:
        return _empty_ro_detail(), _empty_match_log()

    ro_long = _filter_b2c(ro_long, pdh_df, ro_master_df)
    if ro_long.empty:
        return _empty_ro_detail(), _empty_match_log()

    ro_long[HDP_COL_MONTH] = ro_long[HDP_COL_MONTH].dt.normalize()
    # Match log is computed once here (against the un-overridden dim) so the
    # audit reflects how each Customer resolved before any manual fix.
    _corp, match_log = _resolve_customer_corp(ro_long["Customer"], name_to_corp)
    return ro_long[list(_RO_DETAIL_COLS)].copy(), match_log


def _ro_frame(
    ro_detail: pd.DataFrame, corp_by_customer: dict[str, str],
) -> pd.DataFrame:
    """Attribute a Corporate Group per Customer and group into the schema.

    *corp_by_customer* maps a **stripped** Customer name → Corporate Group.
    Customers absent from the map fall back to :data:`CORP_GROUP_UNMAPPED`.
    Pure + in-memory, so it serves both the initial build and a re-map with
    planner overrides.
    """
    if ro_detail is None or ro_detail.empty:
        return _empty_core()
    corp = (
        ro_detail["Customer"].astype(str).str.strip()
        .map(lambda c: corp_by_customer.get(c) or CORP_GROUP_UNMAPPED)
    )
    shaped = ro_detail.assign(**{
        HDP_COL_CORP: corp.values,
        HDP_COL_FORECAST: FORECAST_R_AND_O,
    })
    return _group(shaped)


# ── Item-attribute enrichment (Item Description + Portfolio/Supply dims) ──────
#
# The core legs carry only the grouping keys + pounds.  Everything the IBP
# qry_mgmt_plan_full.csv adds on top (Item Description, Portfolio Major/Minor,
# Supply Format) is a pure function of Item, resolved PDH-primary →
# RO_Item_Master-fallback exactly like the IBP pipeline — so it is attached
# AFTER grouping and never affects group cardinality.

def _build_desc_map(
    pdh_df: Optional[pd.DataFrame], ro_master_df: Optional[pd.DataFrame],
) -> dict[str, str]:
    """``{normalised Item -> Item Description}``, PDH-primary → RO fallback.

    RO_Item_Master's ``Item Desc`` is loaded first (fallback), then PDH's
    ``Item Description`` overwrites it (primary wins) — mirroring the per-field
    coalesce used for the Portfolio/Supply dims.
    """
    out: dict[str, str] = {}
    for df, item_col, desc_col in (
        (ro_master_df, "Item #", "Item Desc"),
        (pdh_df, "Item No", "Item Description"),
    ):
        if df is None or df.empty or not {item_col, desc_col}.issubset(df.columns):
            continue
        keys = _norm_item(df[item_col])
        vals = df[desc_col].astype("string").str.strip()
        for k, v in zip(keys, vals):
            if pd.notna(k) and str(k) and pd.notna(v) and str(v):
                out[str(k)] = str(v)
    return out


_ATTRS_COLS: tuple[str, ...] = (
    HDP_COL_ITEM, HDP_COL_ITEM_DESC, HDP_COL_PMAJ, HDP_COL_PMIN, HDP_COL_SFMT,
)


def _build_item_attrs(
    items: pd.Series,
    pdh_df: Optional[pd.DataFrame],
    ro_master_df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """One row per distinct Item: Item Description + Portfolio Major/Minor/Supply.

    Portfolio dims come from the shared :func:`_attach_item_attrs` coalesce
    (PDH → RO_Item_Master); Item Description from :func:`_build_desc_map`.
    Missing values degrade to ``""`` so the output never carries NaN.
    """
    distinct = pd.Series(
        pd.unique(_norm_item(items)), name=HDP_COL_ITEM,
    ).dropna()
    if distinct.empty:
        return pd.DataFrame(columns=list(_ATTRS_COLS))
    base = pd.DataFrame({HDP_COL_ITEM: distinct.astype(str)})
    enriched = _attach_item_attrs(
        base,
        pdh_df if pdh_df is not None else pd.DataFrame(),
        ro_master_df if ro_master_df is not None else pd.DataFrame(),
    )
    desc = _build_desc_map(pdh_df, ro_master_df)
    enriched[HDP_COL_ITEM_DESC] = (
        enriched[HDP_COL_ITEM].astype(str).map(desc)
    )
    for c in (HDP_COL_ITEM_DESC, HDP_COL_PMAJ, HDP_COL_PMIN, HDP_COL_SFMT):
        if c not in enriched.columns:
            enriched[c] = ""
        enriched[c] = enriched[c].astype("string").fillna("").astype(str)
    return enriched[list(_ATTRS_COLS)]


def _assemble(core: pd.DataFrame, item_attrs: pd.DataFrame) -> pd.DataFrame:
    """Attach item attributes + the constant columns → the full HDP frame.

    ``Party Site Number`` is left blank and ``Business Unit`` is ``"B2C"`` for
    every row, per the file spec; the result is column-ordered to
    :data:`HDP_COLUMNS` (Corporate Group last).
    """
    if core is None or core.empty:
        return _empty_output()
    out = core.copy()
    out[HDP_COL_ITEM] = _norm_item(out[HDP_COL_ITEM]).astype(str)
    out = out.merge(item_attrs, on=HDP_COL_ITEM, how="left")
    for c in (HDP_COL_ITEM_DESC, HDP_COL_PMAJ, HDP_COL_PMIN, HDP_COL_SFMT):
        out[c] = out[c].fillna("") if c in out.columns else ""
    out[HDP_COL_PARTY] = ""                 # kept blank per spec
    out[HDP_COL_BU] = HDP_BUSINESS_UNIT     # every row is B2C
    return out[list(HDP_COLUMNS)].reset_index(drop=True)


# ── Public builder + orchestrator ────────────────────────────────────────────

def build_holistic_demand_plan_aps(
    aps_df: Optional[pd.DataFrame],
    ro_seed_df: Optional[pd.DataFrame],
    tbl_months_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    ro_master_df: Optional[pd.DataFrame],
    customer_names_df: Optional[pd.DataFrame],
    *,
    anchor_month: date = _DEFAULT_ANCHOR_MONTH,
) -> HolisticPlanResult:
    """Merge the APS Base Plan + R&O legs, enrich to the full plan (pure)."""
    name_to_corp = _build_name_to_corp(customer_names_df)
    aps_leg = _build_aps_leg(aps_df)
    ro_detail, match_log = _build_ro_leg(
        ro_seed_df, tbl_months_df, pdh_df, ro_master_df, name_to_corp, anchor_month,
    )
    # Default Corporate Group per Customer = how the match log resolved it.
    base_corp = _corp_by_customer(match_log)
    ro_leg = _ro_frame(ro_detail, base_corp)
    core = _concat_core(aps_leg, ro_leg)
    item_attrs = _build_item_attrs(core[HDP_COL_ITEM], pdh_df, ro_master_df)
    frame = _assemble(core, item_attrs)
    return HolisticPlanResult(
        frame=frame,
        customer_match_log=match_log,
        aps_rows=len(aps_leg),
        ro_rows=len(ro_leg),
        aps_leg=aps_leg,
        ro_detail=ro_detail,
        item_attrs=item_attrs,
    )


def _corp_by_customer(match_log: pd.DataFrame) -> dict[str, str]:
    """``{stripped Customer -> Corporate Group}`` from a match log."""
    if match_log is None or match_log.empty:
        return {}
    return {
        str(cust).strip(): str(corp)
        for cust, corp in zip(
            match_log[MATCH_COL_CUSTOMER], match_log[MATCH_COL_CORP],
        )
    }


def _clean_overrides(overrides: Optional[dict[str, str]]) -> dict[str, str]:
    """Normalise a raw override dict → ``{stripped Customer -> non-blank corp}``."""
    if not overrides:
        return {}
    cleaned: dict[str, str] = {}
    for cust, corp in overrides.items():
        key = str(cust).strip()
        val = str(corp).strip()
        if key and val and val != CORP_GROUP_UNMAPPED:
            cleaned[key] = val
    return cleaned


def apply_customer_corp_overrides(
    result: HolisticPlanResult, overrides: Optional[dict[str, str]],
) -> HolisticPlanResult:
    """Return a new result with manual Customer→Corporate Group overrides applied.

    *overrides* maps a Customer name → the Corporate Group the planner wants
    for **every** R&O row of that Customer.  The R&O leg is re-attributed and
    re-grouped in-memory (no Fabric re-fetch); overridden Customers are
    re-tagged :data:`MATCH_OVERRIDE` in the match log with their new group.
    Blank / ``(Unmapped)`` override values are ignored (treated as "leave as
    resolved").  Returns *result* unchanged when there is nothing to apply.
    """
    clean = _clean_overrides(overrides)
    if not clean:
        return result

    effective = {**_corp_by_customer(result.customer_match_log), **clean}
    ro_leg = _ro_frame(result.ro_detail, effective)
    core = _concat_core(result.aps_leg, ro_leg)
    frame = _assemble(core, result.item_attrs)

    log = result.customer_match_log.copy()
    if not log.empty:
        touched = log[MATCH_COL_CUSTOMER].astype(str).str.strip().isin(clean)
        log.loc[touched, MATCH_COL_CORP] = (
            log.loc[touched, MATCH_COL_CUSTOMER].astype(str).str.strip().map(clean)
        )
        log.loc[touched, MATCH_COL_STATUS] = MATCH_OVERRIDE
        log = (
            log.assign(_rank=log[MATCH_COL_STATUS].map(_MATCH_RANK).fillna(9))
            .sort_values(["_rank", MATCH_COL_CUSTOMER])
            .drop(columns="_rank")
            .reset_index(drop=True)
        )
    return HolisticPlanResult(
        frame=frame,
        customer_match_log=log,
        aps_rows=len(result.aps_leg),
        ro_rows=len(ro_leg),
        aps_leg=result.aps_leg,
        ro_detail=result.ro_detail,
        item_attrs=result.item_attrs,
    )


def filter_needs_review(match_log: pd.DataFrame) -> pd.DataFrame:
    """Return only the match-log rows that warrant a manual look.

    A row needs review when its status is Fuzzy / Unmapped / Override, OR its
    Corporate Group is blank / ``(Unmapped)`` (the "matched but no group" case
    the planner asked to surface).  Confident Exact rows that resolved to a
    real group are hidden.
    """
    if match_log is None or match_log.empty:
        return match_log
    corp = match_log[MATCH_COL_CORP].astype(str).str.strip()
    blank = corp.eq("") | corp.eq(CORP_GROUP_UNMAPPED)
    review = match_log[MATCH_COL_STATUS].isin(_REVIEW_STATUSES) | blank
    return match_log[review].reset_index(drop=True)


def _read_seed_csv(blob_path: str) -> pd.DataFrame:
    df, _etag = read_csv(_SECRETS_SECTION, blob_path, read_csv_kwargs=_STR_READ_KW)
    if df is None:
        raise HolisticDemandPlanError(
            f"Required source 'Files/{blob_path}' was not found in Fabric."
        )
    return df


def generate_holistic_demand_plan_aps(
    *, anchor_month: date = _DEFAULT_ANCHOR_MONTH,
) -> HolisticPlanResult:
    """Read every source from Fabric and build the holistic plan.

    Raises the underlying source error (``PlanLiftError`` /
    ``CustomerDimsError`` / ``LakehouseIOError`` / ``HolisticDemandPlanError``
    / ``ValueError``) so the page can render one clean error path.
    """
    aps_df = fetch_factscurrentaps_holistic_df()
    ro_seed_df = _read_seed_csv(_RO_SEED_BLOB)
    tbl_months_df = _read_seed_csv(_TBL_MONTHS_BLOB)
    pdh_df = _read_seed_csv(_PDH_BLOB)
    ro_master_df = _read_seed_csv(_RO_ITEMS_BLOB)
    customer_names_df = fetch_dp_dimcustomernames_df()
    return build_holistic_demand_plan_aps(
        aps_df, ro_seed_df, tbl_months_df, pdh_df, ro_master_df, customer_names_df,
        anchor_month=anchor_month,
    )


# ── Fabric persistence (existence-check load + save) ──────────────────────────
#
# The finished plan is written to Files/RO Tracking/APS/qry_mgmt_plan_full_aps.csv
# under the fixed name so the page can (a) skip regeneration when it already
# exists and (b) keep hand-applied Corporate Group fixes across sessions.

def load_persisted_aps_plan() -> tuple[Optional[pd.DataFrame], Optional[str]]:
    """Return ``(frame, etag)`` for the persisted APS plan, or ``(None, None)``.

    ``keep_default_na=False`` so blank Party Site Number / Corporate Group stay
    empty strings (not NaN) and round-trip identically on the next save.
    """
    return read_csv(
        _SECRETS_SECTION, _APS_OUTPUT_BLOB,
        read_csv_kwargs={"keep_default_na": False},
    )


def save_aps_plan(frame: pd.DataFrame) -> str:
    """Persist *frame* to ``Files/RO Tracking/APS/…`` and return the new ETag."""
    if frame is None:
        raise HolisticDemandPlanError("Refusing to save a null Holistic Demand Plan.")
    return write_csv(_SECRETS_SECTION, _APS_OUTPUT_BLOB, frame)


def aps_output_path() -> str:
    """The OneLake-relative output path, e.g. for user-facing messages."""
    return f"Files/{_APS_OUTPUT_BLOB}"


__all__ = [
    "HDP_COLUMNS", "HDP_COL_MONTH", "HDP_COL_ITEM", "HDP_COL_ITEM_DESC",
    "HDP_COL_PARTY", "HDP_COL_POUNDS", "HDP_COL_FORECAST", "HDP_COL_BU",
    "HDP_COL_PMAJ", "HDP_COL_PMIN", "HDP_COL_SFMT", "HDP_COL_CORP",
    "HDP_BUSINESS_UNIT", "APS_OUTPUT_NAME",
    "FORECAST_APS_BASE_PLAN", "FORECAST_R_AND_O",
    "MATCH_COLUMNS", "MATCH_COL_CUSTOMER", "MATCH_COL_CORP", "MATCH_COL_STATUS",
    "MATCH_EXACT", "MATCH_FUZZY", "MATCH_UNMAPPED", "MATCH_OVERRIDE",
    "HolisticDemandPlanError", "HolisticPlanResult",
    "build_holistic_demand_plan_aps", "generate_holistic_demand_plan_aps",
    "apply_customer_corp_overrides", "filter_needs_review",
    "load_persisted_aps_plan", "save_aps_plan", "aps_output_path",
]
