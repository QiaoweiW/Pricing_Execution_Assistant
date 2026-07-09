"""Holistic Demand Plan (APS) builder.

Produces the downloadable ``qry_mgmt_plan_full_aps.csv`` — an APS-based
analogue of ``qry_mgmt_plan_full.csv`` — by merging two legs into ONE
unified 5-column frame::

    Month | Item | Corporate Group | Demand Plan Pounds | Forecast Type

Legs
----
* **APS Base Plan** — ``dbo.dp_factscurrentaps`` (via
  :func:`data_sources.plan_lift.fetch_factscurrentaps_holistic_df`):
  ``month → Month``, ``item_code → Item``, native ``corporate_group_code →
  Corporate Group``, ``consensus_plan_lbs → Demand Plan Pounds``,
  ``Forecast Type = "APS Base Plan"``.
* **R&O** — ``RO_Seed.csv`` expanded through the *existing* pipeline
  automation (:func:`demand_plan_pipeline._build_tbl_ro_input` — the
  Format×Month 36-month expansion), then melted to long **keeping the
  seed's Customer** so each row can be attributed to a Corporate Group by
  a fuzzy match of Customer → ``dp_dimcustomernames`` (aligning R&O to the
  same Corporate Group vocabulary APS uses).  B2C-filtered exactly like the
  IBP ``qry_mgmt_plan_full`` R&O portion.  ``Forecast Type = "R&O"``.

This module is **pure-logic** for the builder (frames in → frame out, unit
testable) with a thin :func:`generate_holistic_demand_plan_aps` orchestrator
that reads the sources from Fabric.  It never writes to Fabric — the page
offers the result as a download only.
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
from data_sources.fabric_lakehouse_io import read_csv
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
HDP_COL_MONTH: str    = "Month"
HDP_COL_ITEM: str     = "Item"
HDP_COL_CORP: str     = "Corporate Group"
HDP_COL_POUNDS: str   = "Demand Plan Pounds"
HDP_COL_FORECAST: str = "Forecast Type"
HDP_COLUMNS: tuple[str, ...] = (
    HDP_COL_MONTH, HDP_COL_ITEM, HDP_COL_CORP, HDP_COL_POUNDS, HDP_COL_FORECAST,
)

FORECAST_APS_BASE_PLAN: str = "APS Base Plan"
FORECAST_R_AND_O: str       = "R&O"

# Customer → Corporate Group fuzzy-match log (one row per distinct RO_Seed
# Customer in the R&O output).  Surfaced so the planner can eyeball what
# matched and hand-fix the Unmapped ones in the downloaded qry_aps file.
MATCH_COL_CUSTOMER: str = "Customer"
MATCH_COL_CORP: str     = "Corporate Group"
MATCH_COL_STATUS: str   = "Match"
MATCH_COLUMNS: tuple[str, ...] = (MATCH_COL_CUSTOMER, MATCH_COL_CORP, MATCH_COL_STATUS)

MATCH_EXACT: str    = "Exact"
MATCH_FUZZY: str     = "Fuzzy"
MATCH_UNMAPPED: str = "Unmapped"
# Sort order for the log — most actionable (needs a manual fix) first.
_MATCH_RANK: dict[str, int] = {MATCH_UNMAPPED: 0, MATCH_FUZZY: 1, MATCH_EXACT: 2}

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
        match status (Exact / Fuzzy / Unmapped), most-actionable first.
    aps_rows, ro_rows
        Row counts per leg (post-aggregation) for the run summary.
    """
    frame: pd.DataFrame
    customer_match_log: pd.DataFrame
    aps_rows: int
    ro_rows: int

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


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(HDP_COLUMNS))


def _group(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate a shaped leg to one row per (Month, Item, Corp, Forecast)."""
    if df.empty:
        return _empty_frame()
    out = (
        df.groupby(
            [HDP_COL_MONTH, HDP_COL_ITEM, HDP_COL_CORP, HDP_COL_FORECAST],
            as_index=False, dropna=False,
        )[HDP_COL_POUNDS].sum()
    )
    return out[list(HDP_COLUMNS)]


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
    """Shape dp_factscurrentaps into the unified schema ("APS Base Plan")."""
    if aps_df is None or aps_df.empty:
        return _empty_frame()
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


def _build_ro_leg(
    ro_seed_df: Optional[pd.DataFrame],
    tbl_months_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    ro_master_df: Optional[pd.DataFrame],
    name_to_corp: dict[str, str],
    anchor_month: date,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Expand RO_Seed → long R&O rows, corp-group by Customer, B2C-filtered.

    Returns ``(grouped R&O frame, customer match log)``.
    """
    if ro_seed_df is None or ro_seed_df.empty or tbl_months_df is None:
        return _empty_frame(), _empty_match_log()

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
        return _empty_frame(), _empty_match_log()

    ro_long = _filter_b2c(ro_long, pdh_df, ro_master_df)
    if ro_long.empty:
        return _empty_frame(), _empty_match_log()

    corp, match_log = _resolve_customer_corp(ro_long["Customer"], name_to_corp)
    ro_long = ro_long.assign(**{
        HDP_COL_CORP: corp.values,
        HDP_COL_FORECAST: FORECAST_R_AND_O,
    })
    ro_long[HDP_COL_MONTH] = ro_long[HDP_COL_MONTH].dt.normalize()
    return _group(ro_long), match_log


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
    """Merge the APS Base Plan + R&O legs into the unified plan (pure)."""
    name_to_corp = _build_name_to_corp(customer_names_df)
    aps_leg = _build_aps_leg(aps_df)
    ro_leg, match_log = _build_ro_leg(
        ro_seed_df, tbl_months_df, pdh_df, ro_master_df, name_to_corp, anchor_month,
    )
    frame = pd.concat([aps_leg, ro_leg], ignore_index=True)[list(HDP_COLUMNS)]
    return HolisticPlanResult(
        frame=frame,
        customer_match_log=match_log,
        aps_rows=len(aps_leg),
        ro_rows=len(ro_leg),
    )


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


__all__ = [
    "HDP_COLUMNS", "HDP_COL_MONTH", "HDP_COL_ITEM", "HDP_COL_CORP",
    "HDP_COL_POUNDS", "HDP_COL_FORECAST",
    "FORECAST_APS_BASE_PLAN", "FORECAST_R_AND_O",
    "MATCH_COLUMNS", "MATCH_COL_CUSTOMER", "MATCH_COL_CORP", "MATCH_COL_STATUS",
    "MATCH_EXACT", "MATCH_FUZZY", "MATCH_UNMAPPED",
    "HolisticDemandPlanError", "HolisticPlanResult",
    "build_holistic_demand_plan_aps", "generate_holistic_demand_plan_aps",
]
