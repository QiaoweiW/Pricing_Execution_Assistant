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
    _N_MONTHS,
    _PDH_BLOB,
    _RO_ITEMS_BLOB,
    _RO_SEED_BLOB,
    _SECRETS_SECTION,
    _TBL_MONTHS_BLOB,
    _build_tbl_ro_input,
    _norm_item,
    _parse_dates,
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
    unmapped_customers
        RO_Seed Customer names that fuzzy-matching could not resolve to a
        Corporate Group (surfaced so the planner can reconcile them).
    aps_rows, ro_rows
        Row counts per leg (post-aggregation) for the run summary.
    """
    frame: pd.DataFrame
    unmapped_customers: tuple[str, ...]
    aps_rows: int
    ro_rows: int


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


def _map_customers_to_corp(
    customers: pd.Series, name_to_corp: dict[str, str],
) -> tuple[pd.Series, set[str]]:
    """Map a Customer-name series → Corporate Group; collect unmapped names."""
    # Longest keys first so a containment match prefers the most specific name.
    keys_by_len = sorted(name_to_corp, key=len, reverse=True)
    resolved_cache: dict[str, str] = {}
    unmapped: set[str] = set()

    def _resolve(raw: object) -> str:
        norm = _normalize_name(raw)
        if not norm:
            return CORP_GROUP_UNMAPPED
        if norm in resolved_cache:
            return resolved_cache[norm]
        corp = name_to_corp.get(norm)
        if corp is None:  # containment fallback (either direction)
            for key in keys_by_len:
                if key in norm or norm in key:
                    corp = name_to_corp[key]
                    break
        result = corp or CORP_GROUP_UNMAPPED
        resolved_cache[norm] = result
        if result == CORP_GROUP_UNMAPPED:
            unmapped.add(str(raw).strip())
        return result

    return customers.map(_resolve), unmapped


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


def _build_ro_leg(
    ro_seed_df: Optional[pd.DataFrame],
    tbl_months_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    ro_master_df: Optional[pd.DataFrame],
    name_to_corp: dict[str, str],
    anchor_month: date,
) -> tuple[pd.DataFrame, set[str]]:
    """Expand RO_Seed → long R&O rows, corp-group by Customer, B2C-filtered."""
    if ro_seed_df is None or ro_seed_df.empty or tbl_months_df is None:
        return _empty_frame(), set()

    # Reuse the pipeline's Format×Month 36-month expansion (keeps Customer).
    ro_input = _build_tbl_ro_input(ro_seed_df, anchor_month, _NullLog())
    month_cols = [f"Month {i}" for i in range(1, _N_MONTHS + 1)]

    qry_months = tbl_months_df.copy()
    qry_months["Start of Month"] = _parse_dates(qry_months["Start of Month"])
    qry_months["Month Number"] = qry_months["Month Number"].astype(str).str.strip()

    ro_long = (
        ro_input[["Item #", "Item Desc", "Customer"] + month_cols]
        .melt(
            id_vars=["Item #", "Item Desc", "Customer"], value_vars=month_cols,
            var_name="Attribute", value_name="Value",
        )
        .assign(
            Attribute=lambda d: d["Attribute"].astype(str).str.strip(),
            Value=lambda d: pd.to_numeric(
                d["Value"].astype(str).str.replace(",", "", regex=False)
                          .str.replace("-", "0", regex=False).str.strip(),
                errors="coerce").fillna(0),
        )
        # Grain KEEPS Customer (unlike the IBP portion, which collapses it) so
        # each R&O row can carry its own Corporate Group.
        .groupby(["Item #", "Item Desc", "Customer", "Attribute"], as_index=False)
        .agg(Pounds=("Value", "sum"))
        .merge(qry_months, left_on="Attribute", right_on="Month Number", how="left")
        .rename(columns={
            "Item #": HDP_COL_ITEM, "Pounds": HDP_COL_POUNDS,
            "Start of Month": HDP_COL_MONTH,
        })
        .assign(**{HDP_COL_ITEM: lambda d: _norm_item(d[HDP_COL_ITEM])})
    )
    ro_long = ro_long[(ro_long[HDP_COL_POUNDS] > 0) & ro_long[HDP_COL_MONTH].notna()]
    if ro_long.empty:
        return _empty_frame(), set()

    ro_long = _filter_b2c(ro_long, pdh_df, ro_master_df)
    if ro_long.empty:
        return _empty_frame(), set()

    corp, unmapped = _map_customers_to_corp(ro_long["Customer"], name_to_corp)
    ro_long = ro_long.assign(**{
        HDP_COL_CORP: corp.values,
        HDP_COL_FORECAST: FORECAST_R_AND_O,
    })
    ro_long[HDP_COL_MONTH] = ro_long[HDP_COL_MONTH].dt.normalize()
    return _group(ro_long), unmapped


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
    ro_leg, unmapped = _build_ro_leg(
        ro_seed_df, tbl_months_df, pdh_df, ro_master_df, name_to_corp, anchor_month,
    )
    frame = pd.concat([aps_leg, ro_leg], ignore_index=True)[list(HDP_COLUMNS)]
    return HolisticPlanResult(
        frame=frame,
        unmapped_customers=tuple(sorted(unmapped)),
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
    "HolisticDemandPlanError", "HolisticPlanResult",
    "build_holistic_demand_plan_aps", "generate_holistic_demand_plan_aps",
]
