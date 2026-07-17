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
from dataclasses import dataclass, field, replace
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
    MATCH_COL_CORP,
    MATCH_COL_CUSTOMER,
    MATCH_COL_STATUS,
    MATCH_OVERRIDE,
    _build_name_to_corp,
    _build_ro_leg,
    _clean_overrides,
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
    """Outcome of one upload → transform → append run.

    ``ro_detail`` (Month/Item/Customer/Pounds) + ``ro_dims`` (item→dim maps) +
    ``base_corp`` (Customer→Corporate Group as first resolved) are retained so a
    planner's Customer→Corporate Group override can be re-applied to the R&O leg
    **in memory** (no re-transform / re-fetch) — see
    :func:`apply_ro_corp_overrides`.
    """
    rows: pd.DataFrame          # the new cycle's rows (history schema)
    aps_rows: int
    ro_rows: int
    history_rows: int           # total rows in the history file after upsert
    corp_coverage: float        # share of APS-leg pounds mapped to a real corp group
    match_log: pd.DataFrame     # R&O Customer → Corporate Group fuzzy log
    cycle: str
    fy: int
    ro_detail: pd.DataFrame = field(default_factory=pd.DataFrame)
    ro_dims: dict = field(default_factory=dict)
    base_corp: dict = field(default_factory=dict)


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


def _shape_ro_history(ro_core: pd.DataFrame, dim_maps: dict) -> pd.DataFrame:
    """Shape a grouped R&O core (Month/Item/Corp/Pounds) → history-schema rows.

    R&O rows carry no party site and no sales forecast (a plan-only leg),
    Forecast Type = ``R&O``.  Pure — reused by the initial build and the
    override re-apply.  Unstamped (Cycle/FY/Inclusion added by :func:`_finalize`).
    """
    if ro_core is None or ro_core.empty:
        return pd.DataFrame(columns=list(APS_HIST_COLUMNS))
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
    leg[COL_PMAJ] = leg[COL_ITEM].map(dim_maps["pmaj"]).fillna("")
    leg[COL_SFMT] = leg[COL_ITEM].map(dim_maps["sfmt"]).fillna("")
    leg[COL_PMIN] = leg[COL_ITEM].map(dim_maps["pminor"]).fillna("")
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

    # R&O leg via the holistic expansion + fuzzy corp; keep ro_detail + base
    # attribution so a planner override can re-map in memory.
    ro_detail, match_log = _build_ro_leg(
        ro_seed_df, tbl_months_df, pdh_df, ro_master_df,
        _build_name_to_corp(customer_names_df), anchor_month)
    base_corp = _corp_by_customer(match_log)
    ro_leg = _shape_ro_history(_ro_frame(ro_detail, base_corp), dim_maps)

    combined = _finalize([aps_leg, ro_leg], cycle, fy, inclusion_serial)
    result = ApsUploadResult(
        rows=combined, aps_rows=len(aps_leg), ro_rows=len(ro_leg),
        history_rows=0, corp_coverage=coverage, match_log=match_log,
        cycle=str(cycle), fy=int(fy),
        ro_detail=ro_detail, ro_dims=dim_maps, base_corp=base_corp)
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


def _persist_and_upsert(rows: pd.DataFrame, cycle: str, fy: int) -> int:
    """Write the this-cycle snapshot (overwrite) + upsert the history; return total."""
    write_csv(_SECRETS_SECTION, _APS_FULL_BLOB, rows)
    return upsert_aps_history(rows, cycle, fy)


# ── R&O Corporate-Group override (planner-uploaded fixed match log) ──────────
def parse_corp_override_csv(data: bytes) -> dict[str, str]:
    """Parse a fixed match-log CSV → ``{Customer: Corporate Group}`` overrides.

    Accepts the downloaded match-log shape (``Customer`` / ``Corporate Group``
    columns, case-insensitive); rows with a blank / ``(Unmapped)`` Corporate
    Group are skipped, so a half-filled sheet only patches the customers the
    planner actually completed.
    """
    try:
        raw = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
    except Exception as exc:  # noqa: BLE001
        raise ApsUploadError(f"Could not read the override CSV: {exc}") from exc
    cols = {c.strip().lower(): c for c in raw.columns}
    cust_col = cols.get(MATCH_COL_CUSTOMER.lower())
    corp_col = cols.get(MATCH_COL_CORP.lower())
    if not cust_col or not corp_col:
        raise ApsUploadError(
            f"The override CSV must have '{MATCH_COL_CUSTOMER}' and "
            f"'{MATCH_COL_CORP}' columns (found {list(raw.columns)})."
        )
    out: dict[str, str] = {}
    for cust, corp in zip(raw[cust_col], raw[corp_col]):
        k, v = str(cust).strip(), str(corp).strip()
        if k and v and v != CORP_GROUP_UNMAPPED:
            out[k] = v
    return out


def _mark_overrides(match_log: pd.DataFrame, clean_overrides: dict[str, str]) -> pd.DataFrame:
    """Return the match log with overridden customers re-tagged ``Override``."""
    if match_log is None or match_log.empty or not clean_overrides:
        return match_log
    log = match_log.copy()
    touched = log[MATCH_COL_CUSTOMER].astype(str).str.strip().isin(clean_overrides)
    log.loc[touched, MATCH_COL_CORP] = (
        log.loc[touched, MATCH_COL_CUSTOMER].astype(str).str.strip().map(clean_overrides))
    log.loc[touched, MATCH_COL_STATUS] = MATCH_OVERRIDE
    return log.reset_index(drop=True)


def apply_ro_corp_overrides(
    result: ApsUploadResult, overrides: Optional[dict[str, str]],
    *, inclusion_serial: Optional[int] = None,
) -> ApsUploadResult:
    """Re-attribute R&O Corporate Group per planner overrides; re-shape rows (pure).

    *overrides* maps a Customer name → the Corporate Group to use for **all** its
    R&O rows.  The R&O leg is re-derived from the retained ``ro_detail`` (no
    re-fetch, no re-transform); the APS leg is untouched.  Returns *result*
    unchanged when there is nothing to apply.
    """
    clean = _clean_overrides(overrides)
    if not clean or result.ro_detail is None or result.ro_detail.empty:
        return result
    incl = inclusion_serial if inclusion_serial is not None else today_inclusion_serial()
    effective = {**result.base_corp, **clean}
    ro_leg = _shape_ro_history(_ro_frame(result.ro_detail, effective), result.ro_dims)
    aps_unstamped = (
        result.rows[result.rows[COL_FORECAST] == FORECAST_APS_BASE_PLAN]
        .drop(columns=list(_STAMP_COLS))
    )
    combined = _finalize([aps_unstamped, ro_leg], result.cycle, result.fy, incl)
    return replace(
        result, rows=combined, ro_rows=len(ro_leg),
        match_log=_mark_overrides(result.match_log, clean))


def save_aps_override(
    result: ApsUploadResult, overrides: Optional[dict[str, str]],
) -> ApsUploadResult:
    """Apply R&O corp overrides, re-write full_aps + re-upsert history; new result."""
    patched = apply_ro_corp_overrides(result, overrides)
    history_rows = _persist_and_upsert(patched.rows, patched.cycle, patched.fy)
    return replace(patched, history_rows=history_rows)


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

    history_rows = _persist_and_upsert(rows, cycle, fy)
    return replace(partial, history_rows=history_rows)


def fetch_aps_history_df() -> Optional[pd.DataFrame]:
    """Read the rolling APS history tracker (``None`` if it doesn't exist yet)."""
    df, _etag = read_csv(
        _SECRETS_SECTION, _APS_HISTORY_BLOB,
        read_csv_kwargs={"dtype": str, "keep_default_na": False})
    return df


def list_aps_history_cycles(history_df: Optional[pd.DataFrame]) -> list[str]:
    """Distinct Cycle labels present in the APS history tracker (sorted)."""
    if history_df is None or history_df.empty or COL_CYCLE not in history_df.columns:
        return []
    return sorted(history_df[COL_CYCLE].astype(str).str.strip().replace("", pd.NA).dropna().unique())


def aps_full_path() -> str:
    """OneLake path of the this-cycle APS plan (for UI messages)."""
    return f"Files/{_APS_FULL_BLOB}"


def aps_history_path() -> str:
    """OneLake path of the rolling APS history tracker (for UI messages)."""
    return f"Files/{_APS_HISTORY_BLOB}"
