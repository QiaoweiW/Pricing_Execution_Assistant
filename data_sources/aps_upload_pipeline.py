"""Upload-driven APS demand-plan pipeline.

Turns a planner-uploaded **APS bulk export** CSV (one row per party-site × item
× month, e.g. ``FY27_C5_APS_bulk_export_per_month_YYYYMMDD.csv``) plus a chosen
**Cycle** and **Fiscal Year** into the cycle-stamped APS plan and appends it to
a rolling history tracker — the APS analogue of the IBP
``qry_mgmt_plan_history_tracker.csv``.

Flow (see :func:`generate_aps_from_upload`)
------------------------------------------
1. Parse the uploaded export; archive the raw bytes to
   ``Files/RO Tracking/APS/Append_New_File/``.
2. Shape the **APS Base Plan** leg to the history schema:
   ``month`` → Start-of-Month Excel serial, ``item_code`` → Item, dims
   (Portfolio Major/Minor · Supply Format) resolved **by item code** via
   PDH → RO_Item_Master, ``sales_forecast`` / ``consensus_forecast`` → the two
   pound measures, Corporate Group re-derived via the deterministic
   ``plan_to_code → dp_dimplantosites → dp_dimcustomernames`` bridge
   ("bridge everything"), Forecast Type = ``APS Base Plan``.
3. Append the current **RO_Seed** R&O leg (reusing the holistic builder's
   expansion + Customer-name fuzzy corporate-group match).
4. Stamp Cycle / FY / Inclusion Date (today, as an Excel serial), write
   ``qry_mgmt_plan_full_aps.csv`` (this cycle), and **upsert** the rows into
   ``qry_mgmt_plan_full_aps_history.csv`` — re-uploading a (Cycle, FY) replaces
   that slice, so re-runs are idempotent.

Reuse (no duplication): the RO_Seed expansion + fuzzy corp match come from
:mod:`data_sources.holistic_demand_plan_aps`; dims + date coercion + the
plan-to bridge from :mod:`data_sources.demand_plan_comparison`; all Fabric I/O
from :mod:`data_sources.fabric_lakehouse_io`.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

from data_sources.demand_plan_pipeline import (
    _DEFAULT_ANCHOR_MONTH,
    _PDH_BLOB,
    _RO_ITEMS_BLOB,
    _RO_SEED_BLOB,
    _SECRETS_SECTION,
    _TBL_MONTHS_BLOB,
)
from data_sources.holistic_demand_plan_aps import (
    CORP_GROUP_UNMAPPED,
    FORECAST_APS_BASE_PLAN,
    FORECAST_R_AND_O,
    _build_name_to_corp,
    _build_ro_leg,
    _corp_by_customer,
    _filter_b2c,
    _ro_frame,
    _read_seed_csv,
)
from data_sources.demand_plan_comparison import (
    build_item_dim_frame_cascade,
    build_plan_to_corp_group,
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
COL_CYCLE: str        = "Cycle"
COL_FY: str           = "FY"
COL_INCLUSION: str    = "Inclusion Date"
APS_HIST_COLUMNS: tuple[str, ...] = (
    COL_MONTH, COL_ITEM, COL_ITEM_DESC, COL_PARTY, COL_SALES_LBS, COL_DEMAND_LBS,
    COL_FORECAST, COL_PMAJ, COL_PMIN, COL_SFMT, COL_CORP, COL_CYCLE, COL_FY,
    COL_INCLUSION,
)

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
_UP_CONSENSUS: tuple[str, ...] = ("consensus_forecast", "Consensus Forecast", "consensus_plan_lbs")
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
    """``{field -> {item_key -> value}}`` for pmaj / sfmt / pminor from the cascade."""
    if dim_frame is None or dim_frame.empty:
        return {"pmaj": {}, "sfmt": {}, "pminor": {}}
    keys = dim_frame["__item_key"].astype(str)
    return {
        field: dict(zip(keys, dim_frame[field].astype("string").fillna("")))
        for field in ("pmaj", "sfmt", "pminor")
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
    bridge = (
        _vectorised_clean_str(upload_df[plan_col]).map(plan_to_corp).fillna("")
        if plan_col else pd.Series([""] * n)
    )
    native = _vectorised_clean_str(upload_df[corp_col]) if corp_col else pd.Series([""] * n)
    corp = bridge.where(bridge.astype(bool), native.reset_index(drop=True))
    corp = corp.where(corp.astype(bool), CORP_GROUP_UNMAPPED)

    shaped = pd.DataFrame({
        COL_MONTH: _vectorised_start_of_month(upload_df[m_col]).map(_to_excel_serial).values,
        COL_ITEM: _vectorised_item_key(upload_df[i_col]).values,
        COL_ITEM_DESC: _vectorised_clean_str(upload_df[desc_col]).values if desc_col else "",
        COL_PARTY: _vectorised_clean_str(upload_df[party_col]).values if party_col else "",
        COL_SALES_LBS: _num(upload_df[sales_col]).values if sales_col else 0.0,
        COL_DEMAND_LBS: _num(upload_df[cons_col]).values,
        COL_CORP: corp.values,
    })
    # Aggregate to one row per (month, item, party site, corp); pounds summed.
    grouped = (
        shaped.groupby([COL_MONTH, COL_ITEM, COL_PARTY, COL_CORP], as_index=False, dropna=False)
        .agg({COL_SALES_LBS: "sum", COL_DEMAND_LBS: "sum", COL_ITEM_DESC: "first"})
    )
    grouped = _filter_b2c(grouped, pdh_df, ro_master_df).reset_index(drop=True)  # B2C-only
    if grouped.empty:
        return pd.DataFrame(columns=list(APS_HIST_COLUMNS)), float("nan")

    dims = _dim_maps(dim_frame)
    grouped[COL_FORECAST] = FORECAST_APS_BASE_PLAN
    grouped[COL_PMAJ] = grouped[COL_ITEM].map(dims["pmaj"]).fillna("")
    grouped[COL_SFMT] = grouped[COL_ITEM].map(dims["sfmt"]).fillna("")
    grouped[COL_PMIN] = grouped[COL_ITEM].map(dims["pminor"]).fillna("")

    total = float(grouped[COL_DEMAND_LBS].abs().sum())
    mapped = float(
        grouped.loc[grouped[COL_CORP] != CORP_GROUP_UNMAPPED, COL_DEMAND_LBS].abs().sum())
    coverage = (mapped / total) if total > 1e-9 else float("nan")
    return grouped, coverage


def _build_ro_leg_history(
    ro_seed_df: Optional[pd.DataFrame],
    tbl_months_df: Optional[pd.DataFrame],
    pdh_df: Optional[pd.DataFrame],
    ro_master_df: Optional[pd.DataFrame],
    customer_names_df: Optional[pd.DataFrame],
    dim_frame: pd.DataFrame,
    anchor_month: date,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Reuse the holistic RO_Seed expansion + fuzzy corp; shape to history schema.

    Returns ``(leg, match_log)``.  R&O rows carry no party site and no sales
    forecast (a plan-only leg), Forecast Type = ``R&O``.
    """
    name_to_corp = _build_name_to_corp(customer_names_df)
    ro_detail, match_log = _build_ro_leg(
        ro_seed_df, tbl_months_df, pdh_df, ro_master_df, name_to_corp, anchor_month)
    ro_core = _ro_frame(ro_detail, _corp_by_customer(match_log))  # Month/Item/Corp/Pounds/Forecast
    if ro_core.empty:
        return pd.DataFrame(columns=list(APS_HIST_COLUMNS)), match_log

    dims = _dim_maps(dim_frame)
    item_key = _vectorised_item_key(ro_core["Item"])
    leg = pd.DataFrame({
        COL_MONTH: pd.Series(ro_core["Start of Month"]).map(_to_excel_serial).values,
        COL_ITEM: item_key.values,
        COL_ITEM_DESC: "",
        COL_PARTY: "",
        COL_SALES_LBS: 0.0,
        COL_DEMAND_LBS: ro_core["Demand Plan Pounds"].astype(float).values,
        COL_FORECAST: FORECAST_R_AND_O,
        COL_CORP: ro_core["Corporate Group"].astype(str).values,
    })
    leg[COL_PMAJ] = leg[COL_ITEM].map(dims["pmaj"]).fillna("")
    leg[COL_SFMT] = leg[COL_ITEM].map(dims["sfmt"]).fillna("")
    leg[COL_PMIN] = leg[COL_ITEM].map(dims["pminor"]).fillna("")
    return leg, match_log


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
    coverage / match log but ``history_rows=0`` (filled by the upsert step).
    """
    dim_frame = build_item_dim_frame_cascade(pdh_df, ro_master_df)
    plan_to_corp = build_plan_to_corp_group(plantosites_df, customer_names_df)

    aps_leg, coverage = _build_aps_leg(
        upload_df, dim_frame, plan_to_corp, pdh_df, ro_master_df)
    ro_leg, match_log = _build_ro_leg_history(
        ro_seed_df, tbl_months_df, pdh_df, ro_master_df, customer_names_df,
        dim_frame, anchor_month)

    legs = [leg for leg in (aps_leg, ro_leg) if not leg.empty]
    combined = (
        pd.concat(legs, ignore_index=True) if legs
        else pd.DataFrame(columns=list(APS_HIST_COLUMNS))
    )
    combined[COL_CYCLE] = str(cycle)
    combined[COL_FY] = int(fy)
    combined[COL_INCLUSION] = int(inclusion_serial)
    for col in APS_HIST_COLUMNS:
        if col not in combined.columns:
            combined[col] = ""
    combined = combined[list(APS_HIST_COLUMNS)].reset_index(drop=True)

    result = ApsUploadResult(
        rows=combined, aps_rows=len(aps_leg), ro_rows=len(ro_leg),
        history_rows=0, corp_coverage=coverage, match_log=match_log,
        cycle=str(cycle), fy=int(fy))
    return combined, result


def replace_cycle_fy_slice(
    current: Optional[pd.DataFrame], new_rows: pd.DataFrame, cycle: str, fy: int,
) -> pd.DataFrame:
    """Return *current* with its (Cycle, FY) slice replaced by *new_rows* (pure).

    Idempotent upsert semantics: any existing rows for the uploaded (Cycle, FY)
    are dropped before the new rows are appended, so re-uploading a cycle
    corrects it without stale or doubled rows.
    """
    if current is None or current.empty:
        return new_rows.copy()
    keep = ~(
        (current[COL_CYCLE].astype(str) == str(cycle))
        & (current[COL_FY].astype(str) == str(fy))
    )
    return pd.concat([current[keep], new_rows], ignore_index=True)


def upsert_aps_history(new_rows: pd.DataFrame, cycle: str, fy: int) -> int:
    """Replace the (Cycle, FY) slice of the history file with *new_rows*; return total.

    Read-modify-write via :func:`update_csv` (ETag-retry) — safe against a
    concurrent save.
    """
    merged = update_csv(
        _SECRETS_SECTION, _APS_HISTORY_BLOB,
        lambda current: replace_cycle_fy_slice(current, new_rows, cycle, fy),
        initial_default=pd.DataFrame(columns=list(APS_HIST_COLUMNS)),
    )
    return len(merged)


def _archive_raw_upload(upload_bytes: bytes, filename: str) -> None:
    """Drop the raw uploaded file into the Append_New_File landing folder."""
    safe = (filename or "aps_upload.csv").replace("/", "_").replace("\\", "_")
    try:
        write_bytes(_SECRETS_SECTION, f"{_APS_UPLOAD_DIR}/{safe}", upload_bytes)
    except Exception as exc:  # noqa: BLE001 — archival is best-effort, never fatal
        logger.warning("Could not archive raw APS upload %s: %s", safe, exc)


def generate_aps_from_upload(
    upload_bytes: bytes,
    *,
    filename: str,
    cycle: str,
    fy: int,
    anchor_month: date = _DEFAULT_ANCHOR_MONTH,
) -> ApsUploadResult:
    """Full orchestrator: parse upload → build → write full_aps → upsert history.

    Reads RO_Seed / tblMonths / PDH / RO_Item_Master / customer-names /
    plan-to-sites from Fabric, writes ``qry_mgmt_plan_full_aps.csv`` (this
    cycle) and appends to ``qry_mgmt_plan_full_aps_history.csv``.
    """
    if cycle not in CYCLES:
        raise ApsUploadError(f"Cycle must be one of {CYCLES}, got {cycle!r}.")
    if int(fy) not in FISCAL_YEARS:
        raise ApsUploadError(f"Fiscal Year must be in {FISCAL_YEARS}, got {fy!r}.")

    try:
        upload_df = pd.read_csv(io.BytesIO(upload_bytes), dtype=str, keep_default_na=False)
    except Exception as exc:  # noqa: BLE001
        raise ApsUploadError(f"Could not read the uploaded CSV: {exc}") from exc
    if upload_df.empty:
        raise ApsUploadError("The uploaded APS export is empty.")

    _archive_raw_upload(upload_bytes, filename)

    ro_seed_df = _read_seed_csv(_RO_SEED_BLOB)
    tbl_months_df, _ = read_csv(_SECRETS_SECTION, _TBL_MONTHS_BLOB,
                                read_csv_kwargs={"dtype": str, "keep_default_na": False})
    pdh_df, _ = read_csv(_SECRETS_SECTION, _PDH_BLOB,
                         read_csv_kwargs={"dtype": str, "keep_default_na": False})
    ro_master_df, _ = read_csv(_SECRETS_SECTION, _RO_ITEMS_BLOB,
                               read_csv_kwargs={"dtype": str, "keep_default_na": False})
    customer_names_df = fetch_dp_dimcustomernames_df()
    plantosites_df = fetch_dp_dimplantosites_df()

    rows, partial = build_aps_history_rows(
        upload_df, ro_seed_df, tbl_months_df, pdh_df, ro_master_df,
        customer_names_df, plantosites_df,
        cycle=cycle, fy=fy, inclusion_serial=today_inclusion_serial(),
        anchor_month=anchor_month)
    if rows.empty:
        raise ApsUploadError(
            "Transform produced no rows — check the upload's columns and that "
            "RO_Seed is populated.")

    # This-cycle snapshot (overwrite) + rolling history (upsert by Cycle/FY).
    write_csv(_SECRETS_SECTION, _APS_FULL_BLOB, rows)
    history_rows = upsert_aps_history(rows, cycle, fy)

    return ApsUploadResult(
        rows=rows, aps_rows=partial.aps_rows, ro_rows=partial.ro_rows,
        history_rows=history_rows, corp_coverage=partial.corp_coverage,
        match_log=partial.match_log, cycle=str(cycle), fy=int(fy))


def aps_full_path() -> str:
    """OneLake path of the this-cycle APS plan (for UI messages)."""
    return f"Files/{_APS_FULL_BLOB}"


def aps_history_path() -> str:
    """OneLake path of the rolling APS history tracker (for UI messages)."""
    return f"Files/{_APS_HISTORY_BLOB}"
