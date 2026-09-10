"""Upload-driven APS demand-plan pipeline.

Turns a planner-uploaded **APS bulk export** CSV (one row per party-site × item
× month, e.g. ``FY27_C5_APS_bulk_export_per_month_YYYYMMDD.csv``) plus a chosen
**Cycle** and **Fiscal Year** into the cycle-stamped APS plan and appends it to
a rolling history tracker — the APS analogue of the IBP
``qry_mgmt_plan_history_tracker.csv``.

Flow — two independent legs, each from its own upload
-----------------------------------------------------
The **Base Plan** leg and the **R&O** leg are built and persisted separately, so
a re-upload replaces only that (Cycle, FY, Forecast Type) slice and the other
leg is left intact.

* :func:`generate_base_plan_from_upload` — an **APS bulk export** →
  the **APS Base Plan** leg: ``month`` → Start-of-Month Excel serial,
  ``item_code`` → Item, dims (Portfolio Major/Minor · Supply Format) resolved
  **by item code** via PDH → RO_Item_Master, ``sales_forecast`` /
  ``consensus_forecast`` → the two pound measures, Corporate Group re-derived via
  the deterministic ``plan_to_code → dp_dimplantosites → dp_dimcustomernames``
  bridge (native ``corporate_group_code`` as fallback), Forecast Type =
  ``APS Base Plan``.
* :func:`generate_ro_from_seed` — an uploaded **R&O seed** → the **R&O** leg
  (reusing the holistic builder's expansion + Customer-name fuzzy
  corporate-group match), Forecast Type = ``R&O``.

Both stamp Cycle / FY / Inclusion Date (today, as an Excel serial), refresh
``qry_mgmt_plan_full_aps.csv`` (the whole current-cycle slice) and **upsert**
their leg into ``qry_mgmt_plan_full_aps_history.csv`` via
:func:`replace_cycle_fy_forecast_slice` (idempotent per Cycle + FY + Forecast
Type).  :func:`delete_history_slice` removes a (Cycle, FY[, Forecast Type])
slice.

Corporate groups resolve themselves
-----------------------------------
An R&O customer that no dimension could match used to land as ``(Unmapped)``
and wait for a planner to curate it through a download / edit / re-upload
"review + patch" tool.  That tool is retired: :func:`apply_customer_corp_default`
now gives those rows the customer's own name — which is what the planner typed
almost every time — on both the leg being built and the merged history file, so
older rows heal themselves on the next upload.  Fuzzy matches are deliberately
left alone; see that function for why overwriting them breaks roll-ups.

Reuse (no duplication): the RO_Seed expansion + fuzzy corp match come from
:mod:`data_sources.holistic_demand_plan_aps`; dims + date coercion + the
plan-to bridge from :mod:`data_sources.demand_plan_comparison`; all Fabric I/O
from :mod:`data_sources.fabric_lakehouse_io`.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, replace
from datetime import date
from typing import Optional

import pandas as pd

from data_sources.demand_plan_pipeline import (
    _DEFAULT_ANCHOR_MONTH,
    _PDH_BLOB,
    _RO_ITEMS_BLOB,
    _SECRETS_SECTION,
    _TBL_MONTHS_BLOB,
)
from data_sources.holistic_demand_plan_aps import (
    CORP_GROUP_UNMAPPED,
    FORECAST_APS_BASE_PLAN,
    FORECAST_R_AND_O,
    MATCH_COL_CUSTOMER,
    MATCH_COL_STATUS,
    _build_name_to_corp,
    _build_ro_leg,
    _corp_by_customer,
    _filter_b2c,
)
from data_sources.demand_plan_comparison import (
    build_item_dim_frame_cascade,
    build_plan_to_corp_group,
    order_cycles_by_horizon,
    _vectorised_clean_str,
    _vectorised_item_key,
    _vectorised_start_of_month,
)
from data_sources.customer_dims import fetch_dp_dimcustomernames_df
from data_sources.ship_to_sites import fetch_dp_dimplantosites_df
from data_sources.fabric_lakehouse_io import read_csv, update_csv, write_bytes, write_csv

logger = logging.getLogger(__name__)


# ── Output contract (history-tracker schema — adds Sales Forecast / Cycle / FY
#    / Inclusion Date vs the IBP qry_mgmt_plan_full, drops Business Unit) ───────
COL_MONTH: str        = "Start of Month"
COL_ITEM: str         = "Item"
COL_ITEM_DESC: str    = "Item Description"
COL_PARTY: str        = "Party Site Number"
COL_SALES_LBS: str    = "Sales Forecast Pounds"
COL_DEMAND_LBS: str   = "Demand Plan Pounds"
COL_FORECAST: str     = "Forecast Type"
COL_PMAJ: str         = "Portfolio Major"
COL_PMIN: str         = "Portfolio Minor"
COL_SFMT: str         = "Supply Format"
COL_CORP: str         = "Corporate Group"
COL_CUSTOMER: str     = "Customer"      # R&O rows only — the RO_Seed customer name
COL_CORP_SRC: str     = "Corp Source"   # how Corporate Group was set (see below)
COL_CYCLE: str        = "Cycle"
COL_FY: str           = "FY"
COL_INCLUSION: str    = "Inclusion Date"
APS_HIST_COLUMNS: tuple[str, ...] = (
    COL_MONTH, COL_ITEM, COL_ITEM_DESC, COL_PARTY, COL_SALES_LBS, COL_DEMAND_LBS,
    COL_FORECAST, COL_PMAJ, COL_PMIN, COL_SFMT, COL_CORP, COL_CUSTOMER, COL_CORP_SRC,
    COL_CYCLE, COL_FY, COL_INCLUSION,
)
# How a row's Corporate Group was resolved.  Only ``unmapped`` means "nothing
# resolved it" — see :func:`apply_customer_corp_default`, which fills those in
# automatically so no one has to curate them by hand.
CORP_SRC_BRIDGE: str   = "bridge"     # APS: plan_to_code → dp_dimplantosites → corp
CORP_SRC_NATIVE: str   = "native"     # APS: fell back to the file's corporate_group_code
CORP_SRC_EXACT: str    = "exact"      # R&O: exact customer-name match
CORP_SRC_FUZZY: str    = "fuzzy"      # R&O: fuzzy customer-name match
CORP_SRC_UNMAPPED: str = "unmapped"   # neither leg resolved a real group
CORP_SRC_CUSTOMER: str = "customer"   # R&O: defaulted to the Customer name (automatic)
# Historical only: rows written by the retired manual "review + patch" tool.
# Nothing produces this any more, but it is still present in the history file
# and must keep being treated as resolved.
CORP_SRC_OVERRIDE: str = "override"
# The per-run stamp columns (added last; stripped before a re-stamp).
_STAMP_COLS: tuple[str, ...] = (COL_CYCLE, COL_FY, COL_INCLUSION)

# Fabric locations for the two APS outputs + the raw-upload landing folder.
_APS_FULL_BLOB: str    = "RO Tracking/APS/qry_mgmt_plan_full_aps.csv"
_APS_HISTORY_BLOB: str = "RO Tracking/APS/qry_mgmt_plan_full_aps_history.csv"
_APS_UPLOAD_DIR: str   = "RO Tracking/APS/Append_New_File"

# Uploaded APS bulk-export column names (candidate lists tolerate spelling drift).
_UP_MONTH: tuple[str, ...]     = ("month", "Month", "Start of Month")
_UP_PARTY: tuple[str, ...]     = ("party_site_code", "Party Site Code", "party_site_number")
_UP_PLAN_TO: tuple[str, ...]   = ("plan_to_code", "Plan To Code", "PlanToCode")
_UP_ITEM: tuple[str, ...]      = ("item_code", "Item Code", "Item No", "Item")
_UP_ITEM_DESC: tuple[str, ...] = ("item_description", "Item Description", "ItemDescription")
_UP_SALES: tuple[str, ...]     = ("sales_forecast", "Sales Forecast", "sales_forecast_lbs")
_UP_CONSENSUS: tuple[str, ...] = (
    "consensus_forecast", "Consensus Forecast", "consensus_plan_lbs", "plan_lbs")
_UP_CORP: tuple[str, ...]      = ("corporate_group_code", "Corporate Group", "corporate_group")

# Excel/Lotus day-serial epoch — same anchor the coercion helpers parse FROM.
_EXCEL_EPOCH = pd.Timestamp("1899-12-30")

# Valid dropdown domains (surfaced by the page; validated here too).
CYCLES: tuple[str, ...] = tuple(f"C{i}" for i in range(1, 13))
FISCAL_YEARS: tuple[int, ...] = tuple(range(2027, 2038))


class ApsUploadError(RuntimeError):
    """Raised when the uploaded APS export or a required source is unusable."""


@dataclass(frozen=True)
class ApsUploadResult:
    """Outcome of one upload → transform → append run."""
    rows: pd.DataFrame          # the new cycle's rows (history schema)
    aps_rows: int
    ro_rows: int
    history_rows: int           # total rows in the history file after upsert
    corp_coverage: float        # share of APS-leg pounds mapped to a real corp group
    match_log: pd.DataFrame     # R&O Customer → Corporate Group fuzzy log
    corp_defaulted: int         # R&O rows given their Customer name as the group
    cycle: str
    fy: int


# ── serial helpers ───────────────────────────────────────────────────────────
def _to_excel_serial(value: object) -> object:
    """First-of-month date / Timestamp → Excel day-serial int (NaN passes through)."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or pd.isna(value):
        return pd.NA
    return int((pd.Timestamp(value) - _EXCEL_EPOCH).days)


def today_inclusion_serial() -> int:
    """Today's date as an Excel day-serial (the Inclusion Date stamp)."""
    return int((pd.Timestamp(date.today()) - _EXCEL_EPOCH).days)


def _resolve(df: pd.DataFrame, candidates: tuple[str, ...]) -> Optional[str]:
    """First candidate column present in *df* (case-sensitive as uploaded)."""
    return next((c for c in candidates if c in df.columns), None)


def _num(series: pd.Series) -> pd.Series:
    """Coerce a possibly-string numeric column to float (blank/NaN → 0)."""
    return pd.to_numeric(
        series.astype("string").str.replace(",", "", regex=False), errors="coerce",
    ).fillna(0.0)


# ── leg builders ─────────────────────────────────────────────────────────────
def _dim_maps(dim_frame: pd.DataFrame) -> dict[str, dict[str, str]]:
    """``{field -> {item_key -> value}}`` for pmaj / sfmt / pminor / desc."""
    fields = ("pmaj", "sfmt", "pminor", "desc")
    if dim_frame is None or dim_frame.empty:
        return {f: {} for f in fields}
    keys = dim_frame["__item_key"].astype(str)
    return {
        field: dict(zip(keys, dim_frame[field].astype("string").fillna("")))
        for field in fields if field in dim_frame.columns
    }


def _build_aps_leg(
    upload_df: pd.DataFrame,
    dim_frame: pd.DataFrame,
    plan_to_corp: dict[str, str],
    pdh_df: Optional[pd.DataFrame],
    ro_master_df: Optional[pd.DataFrame],
) -> tuple[pd.DataFrame, float]:
    """Shape the uploaded APS export to the history schema (B2C-scoped).

    Corporate Group is **bridge-primary, native-fallback**: the plan_to_code →
    dp_dimplantosites → corporate_group bridge wins; where it misses, the
    upload's own ``corporate_group_code`` fills in; still blank → Unmapped.
    Returns ``(leg, corp_coverage)`` — the pound-weighted share of the B2C leg
    attributed to a real corporate group.
    """
    if upload_df is None or upload_df.empty:
        return pd.DataFrame(columns=list(APS_HIST_COLUMNS)), float("nan")
    m_col, i_col = _resolve(upload_df, _UP_MONTH), _resolve(upload_df, _UP_ITEM)
    plan_col, cons_col = _resolve(upload_df, _UP_PLAN_TO), _resolve(upload_df, _UP_CONSENSUS)
    if not (m_col and i_col and cons_col):
        raise ApsUploadError(
            f"Uploaded file is missing required columns "
            f"(month={m_col!r}, item={i_col!r}, consensus_forecast={cons_col!r}); "
            f"found {list(upload_df.columns)}."
        )
    party_col = _resolve(upload_df, _UP_PARTY)
    desc_col = _resolve(upload_df, _UP_ITEM_DESC)
    sales_col = _resolve(upload_df, _UP_SALES)
    corp_col = _resolve(upload_df, _UP_CORP)
    n = len(upload_df)

    # Corporate Group: bridge (plan_to → corp) primary, native code fallback.
    # Track the SOURCE per row so the file records how each group was set.
    bridge = (
        _vectorised_clean_str(upload_df[plan_col]).map(plan_to_corp).fillna("")
        if plan_col else pd.Series([""] * n)
    ).reset_index(drop=True)
    native = (
        _vectorised_clean_str(upload_df[corp_col]) if corp_col else pd.Series([""] * n)
    ).reset_index(drop=True)
    corp = bridge.where(bridge.astype(bool), native)
    corp = corp.where(corp.astype(bool), CORP_GROUP_UNMAPPED)
    source = pd.Series(
        [CORP_SRC_BRIDGE if b else (CORP_SRC_NATIVE if nv else CORP_SRC_UNMAPPED)
         for b, nv in zip(bridge.astype(bool), native.astype(bool))]
    )

    shaped = pd.DataFrame({
        COL_MONTH: _vectorised_start_of_month(upload_df[m_col]).map(_to_excel_serial).values,
        COL_ITEM: _vectorised_item_key(upload_df[i_col]).values,
        COL_ITEM_DESC: _vectorised_clean_str(upload_df[desc_col]).values if desc_col else "",
        COL_PARTY: _vectorised_clean_str(upload_df[party_col]).values if party_col else "",
        COL_SALES_LBS: _num(upload_df[sales_col]).values if sales_col else 0.0,
        COL_DEMAND_LBS: _num(upload_df[cons_col]).values,
        COL_CORP: corp.values,
        COL_CORP_SRC: source.values,
    })
    # Aggregate to one row per (month, item, party site, corp); pounds summed.
    grouped = (
        shaped.groupby([COL_MONTH, COL_ITEM, COL_PARTY, COL_CORP], as_index=False, dropna=False)
        .agg({COL_SALES_LBS: "sum", COL_DEMAND_LBS: "sum",
              COL_ITEM_DESC: "first", COL_CORP_SRC: "first"})
    )
    grouped = _filter_b2c(grouped, pdh_df, ro_master_df).reset_index(drop=True)  # B2C-only
    if grouped.empty:
        return pd.DataFrame(columns=list(APS_HIST_COLUMNS)), float("nan")

    dims = _dim_maps(dim_frame)
    grouped[COL_FORECAST] = FORECAST_APS_BASE_PLAN
    grouped[COL_CUSTOMER] = ""   # APS base rows have a party site, not a customer
    grouped[COL_PMAJ] = grouped[COL_ITEM].map(dims.get("pmaj", {})).fillna("")
    grouped[COL_SFMT] = grouped[COL_ITEM].map(dims.get("sfmt", {})).fillna("")
    grouped[COL_PMIN] = grouped[COL_ITEM].map(dims.get("pminor", {})).fillna("")
    # Backfill Item Description from the dim catalog where the source lacked it
    # (e.g. the Fabric SQL fact, which carries no description column).
    dim_desc = grouped[COL_ITEM].map(dims.get("desc", {})).fillna("")
    grouped[COL_ITEM_DESC] = grouped[COL_ITEM_DESC].where(
        grouped[COL_ITEM_DESC].astype(str).str.strip().astype(bool), dim_desc)

    total = float(grouped[COL_DEMAND_LBS].abs().sum())
    mapped = float(
        grouped.loc[grouped[COL_CORP] != CORP_GROUP_UNMAPPED, COL_DEMAND_LBS].abs().sum())
    coverage = (mapped / total) if total > 1e-9 else float("nan")
    return grouped, coverage


def apply_customer_corp_default(
    df: Optional[pd.DataFrame],
) -> tuple[pd.DataFrame, int]:
    """Unresolved R&O rows inherit their Customer name as the Corporate Group.

    Replaces the retired "review + patch" tool.  A planner used to download the
    unresolved customers, type a Corporate Group into each one and upload the
    sheet back; in practice the value they typed was the customer's own name
    almost every time, so the app now just does that.

    Scope is deliberately narrow — only rows where **nothing** resolved a group:

    * **R&O rows only.**  Base-plan rows carry no Customer at all (verified: all
      1,342,282 of them are blank), so there is nothing to inherit.
    * **``unmapped`` only** — never ``fuzzy``.  A fuzzy row already matched a
      real group out of the customer-names dimension, and that group is usually
      the spelling the *base plan* uses ("SMART AND FINAL" where the R&O seed
      says "Smart & Final").  Overwriting it with the customer name would split
      one corporate group into two and quietly break the roll-up — measured on
      the live file, it would have broken 2 groups to fix 1.
    * **Non-blank Customer only.**  A blank one has nothing to paste, so it
      stays ``(Unmapped)`` rather than becoming an empty group.

    Idempotent: filled rows are stamped ``Corp Source = customer``, which is not
    ``unmapped``, so a second pass matches nothing.  Returns
    ``(frame, rows_filled)`` — the input frame unchanged when nothing matched
    (no defensive copy of a million-row frame for a no-op).
    """
    if df is None or df.empty:
        return (df if df is not None else pd.DataFrame(columns=list(APS_HIST_COLUMNS))), 0
    for col in (COL_FORECAST, COL_CORP_SRC, COL_CUSTOMER, COL_CORP):
        if col not in df.columns:
            return df, 0
    customer = df[COL_CUSTOMER].astype(str).str.strip()
    target = (
        (df[COL_FORECAST].astype(str).str.strip() == FORECAST_R_AND_O)
        & (df[COL_CORP_SRC].astype(str).str.strip().str.lower() == CORP_SRC_UNMAPPED)
        & customer.astype(bool)
    )
    n = int(target.sum())
    if not n:
        return df, 0
    out = df.copy()
    out.loc[target, COL_CORP] = customer[target]
    out.loc[target, COL_CORP_SRC] = CORP_SRC_CUSTOMER
    return out, n


def _shape_ro_history(
    ro_detail: pd.DataFrame, cust_corp: dict[str, str], cust_src: dict[str, str],
    dim_maps: dict,
) -> pd.DataFrame:
    """Shape RO_Seed detail (Month/Item/Customer/Pounds) → history-schema rows.

    Grouped to one row per (Month, Item, **Customer**) — the Customer is KEPT so
    the corporate-group patch can target R&O rows directly on the history file.
    Each row records its ``Corporate Group`` + ``Corp Source`` (exact / fuzzy /
    unmapped) from the fuzzy match.  R&O rows carry no party site and no sales
    forecast.  Unstamped (Cycle/FY/Inclusion added by :func:`_finalize`).
    """
    if ro_detail is None or ro_detail.empty:
        return pd.DataFrame(columns=list(APS_HIST_COLUMNS))
    tmp = ro_detail.copy()
    tmp["__item"] = _vectorised_item_key(tmp["Item"]).values
    tmp["__cust"] = tmp["Customer"].astype(str).str.strip()
    tmp["__month"] = pd.Series(tmp["Start of Month"]).map(_to_excel_serial).values
    grouped = (
        tmp.groupby(["__month", "__item", "__cust"], as_index=False, dropna=False)
        ["Demand Plan Pounds"].sum()
    )
    corp = grouped["__cust"].map(cust_corp).fillna("")
    corp = corp.where(corp.astype(bool), CORP_GROUP_UNMAPPED)
    leg = pd.DataFrame({
        COL_MONTH: grouped["__month"].values,
        COL_ITEM: grouped["__item"].values,
        COL_ITEM_DESC: "",
        COL_PARTY: "",
        COL_SALES_LBS: 0.0,
        COL_DEMAND_LBS: grouped["Demand Plan Pounds"].astype(float).values,
        COL_FORECAST: FORECAST_R_AND_O,
        COL_CORP: corp.values,
        COL_CUSTOMER: grouped["__cust"].values,
        COL_CORP_SRC: grouped["__cust"].map(cust_src).fillna(CORP_SRC_UNMAPPED).values,
    })
    leg[COL_PMAJ] = leg[COL_ITEM].map(dim_maps.get("pmaj", {})).fillna("")
    leg[COL_SFMT] = leg[COL_ITEM].map(dim_maps.get("sfmt", {})).fillna("")
    leg[COL_PMIN] = leg[COL_ITEM].map(dim_maps.get("pminor", {})).fillna("")
    dim_desc = leg[COL_ITEM].map(dim_maps.get("desc", {})).fillna("")
    leg[COL_ITEM_DESC] = dim_desc
    # Fill unresolved groups here, at the source, so the rows the planner
    # previews and downloads match what lands in the history file.
    leg, _filled = apply_customer_corp_default(leg)
    return leg


def _finalize(
    legs: list[pd.DataFrame], cycle: str, fy: int, inclusion_serial: int,
) -> pd.DataFrame:
    """Concat the (unstamped) legs, stamp Cycle / FY / Inclusion, order columns."""
    present = [leg for leg in legs if leg is not None and not leg.empty]
    combined = (
        pd.concat(present, ignore_index=True) if present
        else pd.DataFrame(columns=list(APS_HIST_COLUMNS))
    )
    combined[COL_CYCLE] = str(cycle)
    combined[COL_FY] = int(fy)
    combined[COL_INCLUSION] = int(inclusion_serial)
    for col in APS_HIST_COLUMNS:
        if col not in combined.columns:
            combined[col] = ""
    return combined[list(APS_HIST_COLUMNS)].reset_index(drop=True)


def build_aps_history_rows(
    upload_df: pd.DataFrame,
    ro_seed_df: Optional[pd.DataFrame],
    tbl_months_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    ro_master_df: Optional[pd.DataFrame],
    customer_names_df: Optional[pd.DataFrame],
    plantosites_df: Optional[pd.DataFrame],
    *,
    cycle: str,
    fy: int,
    inclusion_serial: int,
    anchor_month: date = _DEFAULT_ANCHOR_MONTH,
) -> tuple[pd.DataFrame, ApsUploadResult]:
    """Pure builder: uploaded export + RO_Seed → history-schema rows for one cycle.

    Returns ``(rows, partial_result)`` — *partial_result* has the counts /
    coverage / match log + the R&O re-apply state, but ``history_rows=0``
    (filled by the upsert step).
    """
    dim_frame = build_item_dim_frame_cascade(pdh_df, ro_master_df)
    dim_maps = _dim_maps(dim_frame)
    plan_to_corp = build_plan_to_corp_group(plantosites_df, customer_names_df)

    aps_leg, coverage = _build_aps_leg(
        upload_df, dim_frame, plan_to_corp, pdh_df, ro_master_df)

    # R&O leg via the holistic expansion + fuzzy corp; the Customer + match
    # status are carried onto the rows so a later patch can target them on file.
    ro_detail, match_log = _build_ro_leg(
        ro_seed_df, tbl_months_df, pdh_df, ro_master_df,
        _build_name_to_corp(customer_names_df), anchor_month)
    cust_corp = _corp_by_customer(match_log)
    cust_src = {
        str(c).strip(): str(m).strip().lower()
        for c, m in zip(match_log[MATCH_COL_CUSTOMER], match_log[MATCH_COL_STATUS])
    } if match_log is not None and not match_log.empty else {}
    ro_leg = _shape_ro_history(ro_detail, cust_corp, cust_src, dim_maps)

    combined = _finalize([aps_leg, ro_leg], cycle, fy, inclusion_serial)
    result = ApsUploadResult(
        rows=combined, aps_rows=len(aps_leg), ro_rows=len(ro_leg),
        history_rows=0, corp_coverage=coverage, match_log=match_log,
        corp_defaulted=0,
        cycle=str(cycle), fy=int(fy))
    return combined, result


def _forecast_type_mask(df: pd.DataFrame, forecast_types) -> pd.Series:
    """Boolean mask over *df* rows whose Forecast Type ∈ *forecast_types* (stripped)."""
    types = {str(t).strip() for t in forecast_types}
    col = (df[COL_FORECAST] if COL_FORECAST in df.columns
           else pd.Series([""] * len(df), index=df.index))
    return col.astype(str).str.strip().isin(types)


def replace_cycle_fy_forecast_slice(
    current: Optional[pd.DataFrame], new_rows: pd.DataFrame,
    cycle: str, fy: int, forecast_types,
) -> pd.DataFrame:
    """Return *current* with the (Cycle, FY, Forecast Type ∈ types) slice replaced.

    Leg-scoped idempotent upsert: only the rows for this (Cycle, FY) **and one of
    the given Forecast Types** are dropped before *new_rows* are appended.  So a
    re-uploaded APS export replaces only the Base Plan rows and an R&O seed only
    the R&O rows — the other leg for that cycle is left intact.
    """
    if current is None or current.empty:
        return new_rows.copy()
    drop = (
        (current[COL_CYCLE].astype(str).str.strip() == str(cycle))
        & (current[COL_FY].astype(str).str.strip() == str(fy))
        & _forecast_type_mask(current, forecast_types)
    )
    return pd.concat([current[~drop], new_rows], ignore_index=True)


def upsert_aps_history_leg(
    new_rows: pd.DataFrame, cycle: str, fy: int, forecast_types,
) -> tuple[pd.DataFrame, int]:
    """Replace the (Cycle, FY, Forecast Type) slice of the history file; return it merged.

    Read-modify-write via :func:`update_csv` (ETag-retry) — safe against a
    concurrent save.  Returns ``(merged_history, rows_corp_defaulted)``.

    The merged frame passes through :func:`apply_customer_corp_default` before
    it is written.  ``_shape_ro_history`` already cleans the leg being built, so
    this second pass exists for rows *already* in the file from an earlier
    upload — they are healed in place the next time anything is built, at no
    extra I/O (we are reading and rewriting the file regardless).
    """
    filled = {"n": 0}

    def _merge(current: Optional[pd.DataFrame]) -> pd.DataFrame:
        merged = replace_cycle_fy_forecast_slice(
            current, new_rows, cycle, fy, forecast_types)
        merged, filled["n"] = apply_customer_corp_default(merged)
        return merged

    merged = update_csv(
        _SECRETS_SECTION, _APS_HISTORY_BLOB, _merge,
        initial_default=pd.DataFrame(columns=list(APS_HIST_COLUMNS)),
        verify=False,   # large blob — skip the full-file header re-read
    )
    return merged, filled["n"]


def delete_history_slice(cycle: str, fy: int, forecast_types=None) -> tuple[int, int]:
    """Delete the (Cycle, FY[, Forecast Type]) slice from the history file.

    *forecast_types* ``None`` deletes every Forecast Type for that (Cycle, FY);
    otherwise only the listed types.  Read-modify-write (verify=False; large
    blob).  Returns ``(rows_deleted, total_history_rows_after)``.
    """
    counter = {"n": 0}

    def _mutate(current: Optional[pd.DataFrame]) -> pd.DataFrame:
        if current is None or current.empty:
            return pd.DataFrame(columns=list(APS_HIST_COLUMNS))
        df = current.copy()
        match = (
            (df[COL_CYCLE].astype(str).str.strip() == str(cycle))
            & (df[COL_FY].astype(str).str.strip() == str(fy))
        )
        if forecast_types is not None:
            match &= _forecast_type_mask(df, forecast_types)
        counter["n"] = int(match.sum())
        return df[~match].reset_index(drop=True)

    merged = update_csv(
        _SECRETS_SECTION, _APS_HISTORY_BLOB, _mutate,
        initial_default=pd.DataFrame(columns=list(APS_HIST_COLUMNS)), verify=False)
    return counter["n"], len(merged)


def _persist_leg_and_upsert(
    rows: pd.DataFrame, cycle: str, fy: int, forecast_types,
) -> tuple[int, int]:
    """Upsert one leg into the history + refresh the this-cycle snapshot.

    ``verify=False`` on the writes: these frames are large (hundreds of thousands
    of rows) and the post-write header re-read is pure overhead (the header
    contract is pinned).  The ``qry_mgmt_plan_full_aps.csv`` snapshot is rewritten
    to the *whole* (Cycle, FY) slice of the merged history (both legs), so it
    always reflects the current cycle regardless of which leg was just built.

    Returns ``(total_history_rows, rows_corp_defaulted)``.
    """
    merged, corp_defaulted = upsert_aps_history_leg(rows, cycle, fy, forecast_types)
    slice_df = merged[
        (merged[COL_CYCLE].astype(str).str.strip() == str(cycle))
        & (merged[COL_FY].astype(str).str.strip() == str(fy))
    ]
    write_csv(_SECRETS_SECTION, _APS_FULL_BLOB, slice_df, verify=False)
    return len(merged), corp_defaulted


def _archive_raw_upload(upload_bytes: bytes, filename: str) -> None:
    """Drop the raw uploaded file into the Append_New_File landing folder."""
    safe = (filename or "aps_upload.csv").replace("/", "_").replace("\\", "_")
    try:
        write_bytes(_SECRETS_SECTION, f"{_APS_UPLOAD_DIR}/{safe}", upload_bytes)
    except Exception as exc:  # noqa: BLE001 — archival is best-effort, never fatal
        logger.warning("Could not archive raw APS upload %s: %s", safe, exc)


def _validate_cycle_fy(cycle: str, fy: int) -> None:
    if cycle not in CYCLES:
        raise ApsUploadError(f"Cycle must be one of {CYCLES}, got {cycle!r}.")
    if int(fy) not in FISCAL_YEARS:
        raise ApsUploadError(f"Fiscal Year must be in {FISCAL_YEARS}, got {fy!r}.")


def _load_aps_dim_sources() -> tuple:
    """Read months / PDH / RO_Item_Master + customer / plan-to dims (NO RO_Seed).

    The two legs now come from planner uploads (an APS bulk export or an R&O
    seed), so the reference plumbing no longer reads the Fabric RO_Seed blob.
    Returns ``(tbl_months_df, pdh_df, ro_master_df, customer_names_df,
    plantosites_df)``.
    """
    kw = {"dtype": str, "keep_default_na": False}
    tbl_months_df, _ = read_csv(_SECRETS_SECTION, _TBL_MONTHS_BLOB, read_csv_kwargs=kw)
    pdh_df, _ = read_csv(_SECRETS_SECTION, _PDH_BLOB, read_csv_kwargs=kw)
    ro_master_df, _ = read_csv(_SECRETS_SECTION, _RO_ITEMS_BLOB, read_csv_kwargs=kw)
    customer_names_df = fetch_dp_dimcustomernames_df()
    plantosites_df = fetch_dp_dimplantosites_df()
    return (tbl_months_df, pdh_df, ro_master_df, customer_names_df, plantosites_df)


def _parse_upload_csv(data: bytes, what: str) -> pd.DataFrame:
    """Parse uploaded CSV bytes → DataFrame (str dtype); raise on empty / unreadable."""
    try:
        df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
    except Exception as exc:  # noqa: BLE001
        raise ApsUploadError(f"Could not read the uploaded {what}: {exc}") from exc
    if df.empty:
        raise ApsUploadError(f"The uploaded {what} is empty.")
    return df


def generate_base_plan_from_upload(
    upload_bytes: bytes,
    *,
    filename: str,
    cycle: str,
    fy: int,
    anchor_month: date = _DEFAULT_ANCHOR_MONTH,
) -> ApsUploadResult:
    """Orchestrator: APS bulk export → **Base Plan leg only** → upsert history.

    Replaces only the ``APS Base Plan`` rows for this (Cycle, FY); the R&O leg
    (built separately from an R&O seed) is left untouched.  Refreshes the
    ``qry_mgmt_plan_full_aps.csv`` snapshot and archives the raw upload.
    """
    _validate_cycle_fy(cycle, fy)
    upload_df = _parse_upload_csv(upload_bytes, "APS export")
    _archive_raw_upload(upload_bytes, filename)
    tbl_months_df, pdh_df, ro_master_df, cust_df, plantosites_df = _load_aps_dim_sources()
    rows, partial = build_aps_history_rows(
        upload_df, None, tbl_months_df, pdh_df, ro_master_df, cust_df, plantosites_df,
        cycle=cycle, fy=fy, inclusion_serial=today_inclusion_serial(),
        anchor_month=anchor_month)
    if rows.empty:
        raise ApsUploadError(
            "Transform produced no Base Plan rows — check the export's columns "
            "(month / item_code / consensus_forecast) and B2C mapping.")
    history_rows, corp_defaulted = _persist_leg_and_upsert(
        rows, cycle, fy, (FORECAST_APS_BASE_PLAN,))
    return replace(partial, history_rows=history_rows, corp_defaulted=corp_defaulted)


def generate_ro_from_seed(
    seed_bytes: bytes,
    *,
    filename: str,
    cycle: str,
    fy: int,
    anchor_month: date = _DEFAULT_ANCHOR_MONTH,
) -> ApsUploadResult:
    """Orchestrator: uploaded R&O seed → **R&O leg only** → upsert history.

    Replaces only the ``R&O`` rows for this (Cycle, FY); the Base Plan leg is
    left untouched.  Refreshes the ``qry_mgmt_plan_full_aps.csv`` snapshot and
    archives the raw upload.
    """
    _validate_cycle_fy(cycle, fy)
    seed_df = _parse_upload_csv(seed_bytes, "R&O seed")
    _archive_raw_upload(seed_bytes, filename)
    tbl_months_df, pdh_df, ro_master_df, cust_df, plantosites_df = _load_aps_dim_sources()
    rows, partial = build_aps_history_rows(
        pd.DataFrame(), seed_df, tbl_months_df, pdh_df, ro_master_df, cust_df, plantosites_df,
        cycle=cycle, fy=fy, inclusion_serial=today_inclusion_serial(),
        anchor_month=anchor_month)
    if rows.empty:
        raise ApsUploadError(
            "Transform produced no R&O rows — check the R&O seed's columns and "
            "that it holds B2C rows for this horizon.")
    history_rows, corp_defaulted = _persist_leg_and_upsert(
        rows, cycle, fy, (FORECAST_R_AND_O,))
    return replace(partial, history_rows=history_rows, corp_defaulted=corp_defaulted)


def fetch_aps_history_df() -> Optional[pd.DataFrame]:
    """Read the rolling APS history tracker (``None`` if it doesn't exist yet)."""
    df, _etag = read_csv(
        _SECRETS_SECTION, _APS_HISTORY_BLOB,
        read_csv_kwargs={"dtype": str, "keep_default_na": False})
    return df


def list_aps_history_cycles(history_df: Optional[pd.DataFrame]) -> list[str]:
    """Distinct Cycle labels in the APS history tracker, oldest → newest.

    Shares :func:`~data_sources.demand_plan_comparison.order_cycles_by_horizon`
    with the IBP tracker so both sections agree on which cycle is newest — the
    page pairs the two lists in one filter widget (APS = current, IBP = prior).
    """
    if history_df is None or history_df.empty or COL_CYCLE not in history_df.columns:
        return []
    months = (
        _vectorised_start_of_month(history_df[COL_MONTH])
        if COL_MONTH in history_df.columns else None
    )
    return order_cycles_by_horizon(history_df[COL_CYCLE], months)


#: Column order of :func:`summarise_history` — the page renders it as-is.
HISTORY_SUMMARY_COLUMNS: tuple[str, ...] = (
    "Cycle", "Base Plan rows", "R&O rows", "Total rows", "Plan covers",
)


def summarise_history(history_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """One row per stored cycle: rows in each leg, and the months it covers.

    Answers the only question a planner has straight after an upload — *did my
    cycle land, and is it complete?*  A cycle needs both legs (an APS bulk
    export and an R&O seed) before the comparison can read it properly, and a
    zero in either column says which file is still missing.

    Newest cycle first, ordered by :func:`list_aps_history_cycles` (plan
    horizon, not label) and reversed, so the cycle just uploaded is at the top.
    Empty frame — with the right columns — when there is no history yet.
    """
    if history_df is None or history_df.empty or COL_CYCLE not in history_df.columns:
        return pd.DataFrame(columns=list(HISTORY_SUMMARY_COLUMNS))
    cycles = history_df[COL_CYCLE].astype(str).str.strip()
    legs = (
        history_df[COL_FORECAST].astype(str).str.strip()
        if COL_FORECAST in history_df.columns
        else pd.Series("", index=history_df.index)
    )
    months = (
        pd.Series(_vectorised_start_of_month(history_df[COL_MONTH]),
                  index=history_df.index)
        if COL_MONTH in history_df.columns
        else pd.Series(pd.NA, index=history_df.index)
    )
    rows = []
    for cycle in list_aps_history_cycles(history_df):
        in_cycle = cycles == cycle
        span = months[in_cycle].dropna()
        rows.append({
            "Cycle": cycle,
            "Base Plan rows": int((in_cycle & (legs == FORECAST_APS_BASE_PLAN)).sum()),
            "R&O rows": int((in_cycle & (legs == FORECAST_R_AND_O)).sum()),
            "Total rows": int(in_cycle.sum()),
            "Plan covers": (
                f"{min(span):%b %Y} → {max(span):%b %Y}" if len(span) else "—"),
        })
    out = pd.DataFrame(rows, columns=list(HISTORY_SUMMARY_COLUMNS))
    return out.iloc[::-1].reset_index(drop=True)


def list_aps_history_forecast_types(history_df: Optional[pd.DataFrame]) -> list[str]:
    """Distinct Forecast Type labels present in the APS history tracker (sorted)."""
    if history_df is None or history_df.empty or COL_FORECAST not in history_df.columns:
        return []
    return sorted(
        history_df[COL_FORECAST].astype(str).str.strip().replace("", pd.NA).dropna().unique())


def aps_full_path() -> str:
    """OneLake path of the this-cycle APS plan (for UI messages)."""
    return f"Files/{_APS_FULL_BLOB}"


def aps_history_path() -> str:
    """OneLake path of the rolling APS history tracker (for UI messages)."""
    return f"Files/{_APS_HISTORY_BLOB}"
