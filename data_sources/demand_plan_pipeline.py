"""
Demand Plan ETL — Base Plan upload → demand-plan CSVs (in-app, no Fabric notebook).

This is the in-app port of the four Fabric-notebook cells that build the demand
plan from an uploaded ``ibp_base_plan_current.csv``.  It mirrors
:mod:`data_sources.ro_seed_pipeline`: a pure, Streamlit-free module that reads
and writes the OneLake lakehouse through :mod:`data_sources.fabric_lakehouse_io`
and returns a structured result the page renders verbatim.

Pipeline (all computed in memory, written only once every stage succeeds)
------------------------------------------------------------------------
1. ``tbl_ro_input.csv``                 ← ``RO_Seed.csv`` expanded Format→Month 36
2. ``qry_mgmt_plan_full.csv``           ← Base Plan + R&O, B2C-filtered
   ``qry_demand_item_customer_detail.csv`` (built alongside, shares intermediates)
3. ``qry_total_item_level_demand.csv``  ← mgmt-plan-full + Portfolio Major
4. ``qry_mgmt_plan_history_tracker.csv``← append this run's mgmt-plan-full stamped
   with the upload's **user-authored ``Cycle``** (upsert: re-running a cycle
   replaces that cycle's rows — idempotent)

Differences vs. the original notebook (by design, agreed with the planner)
--------------------------------------------------------------------------
* **Cycle** comes from the upload's ``Cycle`` column (e.g. ``C5``) — never
  auto-numbered.  It is the sole identity key for the history tracker.
* The forward-window cutoff is anchored on the upload's ``month`` column (the
  demand-review month), not wall-clock ``today()`` — so a cycle's output is
  reproducible.  An explicit override is accepted.
* The RO 36-month calendar anchor (was hard-coded ``2026-04-01``) is a caller
  argument, defaulting to that same value.
* File I/O goes through the connector layer; ``display()``/``print()`` become a
  structured run log.

Paths are duplicated as module constants (rather than imported from
:mod:`data_sources.demand_summary`) on purpose: that module carries Streamlit
cache decorators, and keeping this pipeline import-light preserves its
testability — the same trade-off ``ro_seed_pipeline`` already makes.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

# Reuse the RO pipeline's trivial run-log types — no duplicate boilerplate.
from .ro_seed_pipeline import LogEntry, _Log
from .fabric_lakehouse_io import (
    LakehouseIOError,
    archive_bytes,
    read_csv,
    write_bytes,
    write_csv,
)


# ── OneLake locations (relative to "Files/") ─────────────────────────────────
_SECRETS_SECTION: str = "fabric_htst"
_BASE = "RO Tracking/Demand Plan"
_RO = "RO Tracking"

_BASE_PLAN_BLOB: str = f"{_BASE}/Append New Plan/ibp_base_plan_current.csv"
_BASE_PLAN_ARCHIVE_DIR: str = f"{_BASE}/Append New Plan/Archive"
_TBL_RO_INPUT_BLOB: str = f"{_BASE}/tbl_ro_input.csv"
_TBL_MONTHS_BLOB: str = f"{_BASE}/tblMonths.csv"
_PDH_BLOB: str = f"{_BASE}/qry_pdh.csv"
_RO_ITEMS_BLOB: str = f"{_RO}/RO_Item_Master.csv"
_RO_SEED_BLOB: str = f"{_RO}/RO_Seed.csv"
_MGMT_PLAN_FULL_BLOB: str = f"{_BASE}/qry_mgmt_plan_full.csv"
_DETAIL_BLOB: str = f"{_BASE}/qry_demand_item_customer_detail.csv"
_TOTAL_ITEM_BLOB: str = f"{_BASE}/qry_total_item_level_demand.csv"
_HISTORY_TRACKER_BLOB: str = f"{_BASE}/qry_mgmt_plan_history_tracker.csv"

# Read every CSV as raw strings, blanks preserved — the pipeline does its own
# typing (notebook parity).
_STR_READ_KW: dict = {"dtype": str, "keep_default_na": False}

# Defaults (planner-overridable from the UI).
_DEFAULT_ANCHOR_MONTH: date = date(2026, 4, 1)   # was ANCHOR_MONTH in cell 1
_N_MONTHS: int = 36                               # RO horizon
_DEFAULT_FORWARD_WINDOW_MONTHS: int = 24          # was FORWARD_WINDOW_MONTHS in cell 2

# RO_Seed columns required by stage 1 (Format→Month 36 expansion).
_SEED_COLUMNS = [
    "Format", "Customer", "Taxonomy", "Brand", "Item #", "Item Desc",
    "Probability", "First Ship Date", "Lbs./yr", "PC$/yr", "Slotting",
]
# Base-plan columns the ETL consumes (+ the two new metadata columns).
_BASE_PLAN_REQUIRED = [
    "Start of Month", "Item", "Item Description", "Value", "Total",
    "Corporate Group", "month", "Cycle",
]
# qry_mgmt_plan_full.csv schema (also the history tracker minus Cycle).
_MGMT_FULL_COLUMNS = [
    "Start of Month", "Item", "Item Description", "Party Site Number",
    "Demand Plan Pounds", "Forecast Type", "Business Unit",
]
_TRACKER_COLUMNS = _MGMT_FULL_COLUMNS + ["Cycle"]
# Item-level attributes resolved PDH-first, RO-master-fallback (per field).
_ATTR_COLS = ["Portfolio Major", "Portfolio Minor", "Supply Format"]


@dataclass
class DemandPlanResult:
    """Outcome of one Demand Plan pipeline run — the UI renders this verbatim."""
    ok: bool
    log: list[LogEntry] = field(default_factory=list)
    cycle: Optional[str] = None
    meeting_month: Optional[date] = None
    window_end: Optional[date] = None
    tbl_ro_input_rows: Optional[int] = None
    mgmt_full_rows: Optional[int] = None
    detail_rows: Optional[int] = None
    total_item_rows: Optional[int] = None
    history_rows: Optional[int] = None
    mgmt_total_lbs: Optional[float] = None

    @property
    def warnings(self) -> list[str]:
        return [e.text for e in self.log if e.level == "warning"]

    @property
    def errors(self) -> list[str]:
        return [e.text for e in self.log if e.level == "error"]


# ── Shared ETL helpers (the notebook's reusable functions) ───────────────────

def _parse_dates(series: pd.Series) -> pd.Series:
    """Parse a column mixing Excel serials (``46266``) and date strings.

    Per row: try the serial interpretation first, fall back to a string parse —
    so neither format is silently dropped.
    """
    as_num = pd.to_numeric(series, errors="coerce")
    from_serial = pd.to_datetime(as_num, unit="D", origin="1899-12-30", errors="coerce")
    from_string = pd.to_datetime(series, errors="coerce")
    return from_serial.combine_first(from_string)


def _norm_item(series: pd.Series) -> pd.Series:
    """Item join key: string, trimmed, trailing ``.0`` dropped (``370086.0``→``370086``)."""
    return series.astype("string").str.strip().str.replace(r"\.0$", "", regex=True)


def _lbs(series: pd.Series) -> pd.Series:
    """Coerce a possibly comma-formatted pounds column to numeric."""
    return pd.to_numeric(
        series.astype(str).str.replace(",", "", regex=False), errors="coerce")


def _single_value(series: pd.Series, label: str) -> str:
    """Return the one distinct non-blank value in ``series`` or raise ValueError."""
    vals = sorted({v.strip() for v in series.astype(str) if v and v.strip()})
    if len(vals) != 1:
        raise ValueError(
            f"The upload must carry exactly one '{label}' value; found: "
            f"{vals or 'none'}.")
    return vals[0]


# ── Stage 1: RO_Seed → tbl_ro_input (Format → Month 36) ──────────────────────

def _build_tbl_ro_input(
    seed_df: pd.DataFrame, anchor_month: date, log: _Log,
) -> pd.DataFrame:
    """Expand RO_Seed into the wide 36-month R&O input (mirrors the 'RO Input' tab).

    Monthly lbs = Probability × Lbs/yr ÷ 365 × DaysInMonth, gated so a month
    only contributes once it is on/after the row's First Ship Date.
    """
    missing = [c for c in _SEED_COLUMNS if c not in seed_df.columns]
    if missing:
        raise ValueError(f"RO_Seed.csv is missing required column(s): {missing}")

    s = seed_df.copy()
    for c in ("Probability", "Lbs./yr", "PC$/yr", "Slotting"):
        s[c] = pd.to_numeric(s[c], errors="coerce")
    s["First Ship Date"] = pd.to_datetime(s["First Ship Date"], errors="coerce").dt.normalize()
    s["Prob. Lbs/m"] = s["Lbs./yr"] * s["Probability"] / 12

    # Build the anchored month calendar (pandas-native — no dateutil/calendar dep).
    anchor_ts = pd.Timestamp(anchor_month).normalize().replace(day=1)
    cal = pd.DataFrame([
        {
            "ColumnName": f"Month {n}",
            "MonthStart": (anchor_ts + pd.DateOffset(months=n - 1)).normalize(),
            "DaysInMonth": (anchor_ts + pd.DateOffset(months=n - 1)).days_in_month,
        }
        for n in range(1, _N_MONTHS + 1)
    ])

    # Cross-join seed × calendar, gate by First Ship Date, pivot to wide.
    s["_k"] = 1
    cal["_k"] = 1
    long = s.merge(cal, on="_k").drop(columns="_k")
    gate = long["MonthStart"] >= long["First Ship Date"]
    long["MonthlyLbs"] = (
        long["Probability"] * long["Lbs./yr"] / 365 * long["DaysInMonth"]
    ).where(gate, 0.0)

    wide = long.pivot_table(
        index=_SEED_COLUMNS + ["Prob. Lbs/m"],
        columns="ColumnName", values="MonthlyLbs", aggfunc="sum",
    ).reset_index()
    wide.columns.name = None
    month_cols = [f"Month {n}" for n in range(1, _N_MONTHS + 1)]
    wide = wide[_SEED_COLUMNS + ["Prob. Lbs/m"] + month_cols]
    log.ok(f"tbl_ro_input built — {len(wide):,} rows × {len(wide.columns)} cols")
    return wide


def _ro_input_to_long(
    ro_input: pd.DataFrame,
    qry_months: pd.DataFrame,
    *,
    extra_id_cols: tuple[str, ...] = (),
) -> pd.DataFrame:
    """Melt the wide ``tbl_ro_input`` (Month 1..36) into long R&O rows.

    Shared by the IBP mgmt-plan build (``extra_id_cols=()`` — collapses
    across customers, one row per Item × month) and the APS Holistic Demand
    Plan build (``extra_id_cols=("Customer",)`` — keeps per-customer rows so
    each can carry its own Corporate Group).  ``qry_months`` supplies the
    ``Month Number → Start of Month`` mapping (parsed by the caller).

    Returns columns ``[*extra_id_cols, Item, Item Description,
    Start of Month, Demand Plan Pounds]``.
    """
    month_cols = [f"Month {i}" for i in range(1, _N_MONTHS + 1)]
    id_cols = ["Item #", "Item Desc", *extra_id_cols]
    return (
        ro_input[id_cols + month_cols]
        .melt(id_vars=id_cols, value_vars=month_cols,
              var_name="Attribute", value_name="Value")
        .assign(
            Attribute=lambda d: d["Attribute"].astype(str).str.strip(),
            Value=lambda d: pd.to_numeric(
                d["Value"].astype(str).str.replace(",", "", regex=False)
                          .str.replace("-", "0", regex=False).str.strip(),
                errors="coerce").fillna(0),
        )
        .groupby(id_cols + ["Attribute"], as_index=False).agg(Pounds=("Value", "sum"))
        .merge(qry_months, left_on="Attribute", right_on="Month Number", how="left")
        .rename(columns={"Item #": "Item", "Item Desc": "Item Description",
                         "Pounds": "Demand Plan Pounds"})
        .assign(**{"Item": lambda d: _norm_item(d["Item"])})
        [[*extra_id_cols, "Item", "Item Description",
          "Start of Month", "Demand Plan Pounds"]]
    )


# ── Stage 2: Base Plan + R&O → mgmt_plan_full + item×customer detail ──────────

def _build_mgmt_plan_and_detail(
    base_plan: pd.DataFrame,
    ro_input: pd.DataFrame,
    tbl_months: pd.DataFrame,
    pdh: pd.DataFrame,
    ro_master: pd.DataFrame,
    *,
    window_end: pd.Timestamp,
    log: _Log,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build ``qry_mgmt_plan_full`` and ``qry_demand_item_customer_detail``.

    Both outputs share one enrichment pass (B2C resolution + the filtered
    base-plan branch), exactly like the source notebook cell — so the heavy
    work is done once. Returns ``(mgmt_full, detail)``.
    """
    # Still needed by the detail branch (_build_detail) below.
    month_cols = [f"Month {i}" for i in range(1, _N_MONTHS + 1)]

    # Month-number → Start-of-Month lookup.
    qry_months = tbl_months.copy()
    qry_months["Start of Month"] = _parse_dates(qry_months["Start of Month"])
    qry_months["Month Number"] = qry_months["Month Number"].astype(str).str.strip()

    # Tag base-plan rows so the detail branch can attach Corporate Group later.
    base_plan = base_plan.reset_index(drop=True).copy()
    base_plan["_ibp_row_id"] = base_plan.index

    # --- Base-plan branch → canonical layout ---------------------------------
    base_long = (
        base_plan[["_ibp_row_id", "Start of Month", "Item",
                   "Item Description", "Value", "Total"]]
        .rename(columns={"Value": "Party Site Number", "Total": "Demand Plan Pounds"})
        .assign(**{
            "Start of Month":     lambda d: _parse_dates(d["Start of Month"]),
            "Item":               lambda d: _norm_item(d["Item"]),
            "Item Description":   lambda d: d["Item Description"].astype(str),
            "Party Site Number":  lambda d: d["Party Site Number"].astype(str),
            "Demand Plan Pounds": lambda d: _lbs(d["Demand Plan Pounds"]),
            "Forecast Type":      "Base Plan",
        })
    )

    # --- R&O branch → unpivot Month 1..36 to long (shared helper) ------------
    ro_long = (
        _ro_input_to_long(ro_input, qry_months)
        .assign(**{"Forecast Type": "R&O", "_ibp_row_id": pd.NA})
        [["_ibp_row_id", "Item", "Item Description", "Start of Month",
          "Forecast Type", "Demand Plan Pounds"]]
    )

    # --- Combine + row filters (positive demand, dated, forward window) -------
    combined = pd.concat([base_long, ro_long], ignore_index=True)
    combined["Party Site Number"] = (
        combined["Party Site Number"].fillna("NA")
        if "Party Site Number" in combined.columns else "NA")
    combined = combined[combined["Demand Plan Pounds"] > 0]
    combined = combined[combined["Start of Month"].notna()]
    combined = combined[combined["Start of Month"] < window_end].reset_index(drop=True)
    combined["Start of Month"] = combined["Start of Month"].dt.normalize()
    combined["Item"] = _norm_item(combined["Item"])

    # --- Business Unit (PDH primary, RO-master → B2C fallback) ---------------
    pdh_bu = (
        pdh[["Item No", "Business Unit"]].dropna(subset=["Item No"])
        .assign(**{"Item No": lambda d: _norm_item(d["Item No"]),
                   "Business Unit": lambda d: d["Business Unit"].astype("string").str.strip()})
        .drop_duplicates(subset=["Item No"], keep="first")
        .rename(columns={"Item No": "Item", "Business Unit": "Business Unit_pdh"})
    )
    ro_items_set = set(_norm_item(ro_master["Item #"]).dropna())

    combined = combined.merge(pdh_bu, on="Item", how="left")
    combined["Business Unit"] = combined["Business Unit_pdh"]
    fb = combined["Business Unit"].isna() & combined["Item"].isin(ro_items_set)
    combined.loc[fb, "Business Unit"] = "B2C"
    combined = combined.drop(columns=["Business Unit_pdh"])
    combined = combined[combined["Business Unit"] == "B2C"].reset_index(drop=True)

    mgmt_full = combined[_MGMT_FULL_COLUMNS].copy()
    assert mgmt_full["Forecast Type"].notna().all(), "Forecast Type has nulls!"
    assert (mgmt_full["Business Unit"] == "B2C").all(), "Non-B2C rows leaked through!"
    log.ok(f"qry_mgmt_plan_full built — {len(mgmt_full):,} rows")

    # --- Detail (Item × Customer grain) --------------------------------------
    detail = _build_detail(
        combined, base_plan, ro_input, qry_months, pdh, ro_master,
        ro_items_set=ro_items_set, window_end=window_end, month_cols=month_cols)
    log.ok(f"qry_demand_item_customer_detail built — {len(detail):,} rows")
    return mgmt_full, detail


def _build_detail(
    combined: pd.DataFrame,
    base_plan: pd.DataFrame,
    ro_input: pd.DataFrame,
    qry_months: pd.DataFrame,
    pdh: pd.DataFrame,
    ro_master: pd.DataFrame,
    *,
    ro_items_set: set,
    window_end: pd.Timestamp,
    month_cols: list[str],
) -> pd.DataFrame:
    """Item × Customer detail: reuse the B2C base-plan branch, re-melt R&O w/ Customer."""

    def _keep_b2c(df: pd.DataFrame) -> pd.DataFrame:
        d = df.merge(
            pdh[["Item No", "Business Unit"]].assign(
                **{"Item No": lambda x: _norm_item(x["Item No"])}
            ).drop_duplicates("Item No").rename(columns={"Item No": "Item"}),
            on="Item", how="left")
        bu = d["Business Unit"]
        bu = bu.where(bu.notna(), other=pd.NA)
        fb = bu.isna() & d["Item"].isin(ro_items_set)
        d.loc[fb, "Business Unit"] = "B2C"
        return d[d["Business Unit"] == "B2C"].drop(columns=["Business Unit"]).reset_index(drop=True)

    def _attach_attrs(df: pd.DataFrame) -> pd.DataFrame:
        pdh_attr = (
            pdh[["Item No"] + _ATTR_COLS].dropna(subset=["Item No"])
            .assign(**{"Item No": lambda d: _norm_item(d["Item No"])})
            .drop_duplicates("Item No", keep="first")
            .rename(columns={"Item No": "Item", **{c: f"{c}_pdh" for c in _ATTR_COLS}}))
        ro_attr = (
            ro_master[["Item #"] + _ATTR_COLS].dropna(subset=["Item #"])
            .assign(**{"Item #": lambda d: _norm_item(d["Item #"])})
            .drop_duplicates("Item #", keep="first")
            .rename(columns={"Item #": "Item", **{c: f"{c}_ro" for c in _ATTR_COLS}}))
        d = df.merge(pdh_attr, on="Item", how="left").merge(ro_attr, on="Item", how="left")
        for c in _ATTR_COLS:
            d[c] = (d[f"{c}_pdh"].astype("string").str.strip()
                      .combine_first(d[f"{c}_ro"].astype("string").str.strip()))
        return d.drop(columns=[f"{c}_pdh" for c in _ATTR_COLS]
                              + [f"{c}_ro" for c in _ATTR_COLS])

    # A. Base-plan detail — Customer Name = Corporate Group via _ibp_row_id.
    ibp_cust = base_plan[["_ibp_row_id", "Corporate Group"]].copy()
    ibp_cust["Corporate Group"] = ibp_cust["Corporate Group"].astype("string").str.strip()
    base_detail = (
        combined.loc[combined["Forecast Type"] == "Base Plan",
                     ["_ibp_row_id", "Start of Month", "Item", "Item Description",
                      "Party Site Number", "Demand Plan Pounds", "Forecast Type"]]
        .assign(_ibp_row_id=lambda d: d["_ibp_row_id"].astype("int64"))
        .merge(ibp_cust, on="_ibp_row_id", how="left")
        .rename(columns={"Corporate Group": "Customer Name"})
        .drop(columns=["_ibp_row_id"])
    )

    # B. R&O detail — re-melt keeping Customer, re-apply filters + B2C.
    ro_detail = (
        ro_input[["Item #", "Item Desc", "Customer"] + month_cols]
        .melt(id_vars=["Item #", "Item Desc", "Customer"], value_vars=month_cols,
              var_name="Attribute", value_name="Value")
        .assign(
            Attribute=lambda d: d["Attribute"].astype(str).str.strip(),
            Value=lambda d: pd.to_numeric(
                d["Value"].astype(str).str.replace(",", "", regex=False)
                          .str.replace("-", "0", regex=False).str.strip(),
                errors="coerce").fillna(0),
        )
        .groupby(["Item #", "Item Desc", "Customer", "Attribute"], as_index=False)
        .agg(**{"Demand Plan Pounds": ("Value", "sum")})
        .merge(qry_months[["Month Number", "Start of Month"]],
               left_on="Attribute", right_on="Month Number", how="left")
        .drop(columns=["Attribute", "Month Number"])
        .rename(columns={"Item #": "Item", "Item Desc": "Item Description",
                         "Customer": "Customer Name"})
        .assign(**{"Item": lambda d: _norm_item(d["Item"]),
                   "Customer Name": lambda d: d["Customer Name"].astype("string").str.strip(),
                   "Party Site Number": "NA", "Forecast Type": "R&O"})
    )
    ro_detail = ro_detail[ro_detail["Demand Plan Pounds"] > 0]
    ro_detail = ro_detail[ro_detail["Start of Month"].notna()]
    ro_detail = ro_detail[ro_detail["Start of Month"] < window_end].reset_index(drop=True)
    ro_detail = _keep_b2c(ro_detail)
    ro_detail = ro_detail[["Start of Month", "Item", "Item Description",
                           "Party Site Number", "Demand Plan Pounds",
                           "Forecast Type", "Customer Name"]]

    # C. Combine, attach attributes once, aggregate to grain.
    detail = pd.concat([base_detail, ro_detail], ignore_index=True)
    detail["Customer Name"] = (detail["Customer Name"].astype("string").str.strip()
                                     .replace("", pd.NA).fillna("NA"))
    detail["Start of Month"] = pd.to_datetime(detail["Start of Month"]).dt.normalize()
    detail = _attach_attrs(detail)

    group_keys = ["Start of Month", "Item", "Item Description", "Customer Name",
                  "Party Site Number", "Forecast Type"] + _ATTR_COLS
    detail = (detail.groupby(group_keys, as_index=False, dropna=False)
                    .agg(**{"Demand Plan Pounds": ("Demand Plan Pounds", "sum")}))
    return detail[["Start of Month", "Item", "Item Description", "Customer Name",
                   "Party Site Number", "Demand Plan Pounds", "Forecast Type"] + _ATTR_COLS]


# ── Stage 3: mgmt_plan_full → total item-level demand ────────────────────────

def _build_total_item_level_demand(
    mgmt_full: pd.DataFrame, pdh: pd.DataFrame, ro_master: pd.DataFrame, log: _Log,
) -> pd.DataFrame:
    """Add Portfolio Major (PDH primary, RO-master fallback) + IBP Product Group.

    Uses the in-memory ``mgmt_full`` (not a re-read) and normalised join keys, so
    item numbers like ``370072.0`` still match ``370072`` in PDH.
    """
    out = mgmt_full.copy()
    out["Item"] = _norm_item(out["Item"])

    pdh_lk = (
        pdh[["Item No", "Portfolio Major"]].dropna(subset=["Item No"])
        .assign(**{"Item No": lambda d: _norm_item(d["Item No"])})
        .drop_duplicates("Item No", keep="first")
        .rename(columns={"Item No": "Item", "Portfolio Major": "Portfolio Major_pdh"}))
    ro_lk = (
        ro_master[["Item #", "Portfolio Major"]].dropna(subset=["Item #"])
        .assign(**{"Item #": lambda d: _norm_item(d["Item #"])})
        .drop_duplicates("Item #", keep="first")
        .rename(columns={"Item #": "Item", "Portfolio Major": "Portfolio Major_ro"}))

    out = out.merge(pdh_lk, on="Item", how="left").merge(ro_lk, on="Item", how="left")
    out["Portfolio Major"] = out["Portfolio Major_pdh"].fillna(out["Portfolio Major_ro"])
    out["IBP Product Group"] = out["Portfolio Major"]
    out = out[["Start of Month", "Item", "Item Description", "Demand Plan Pounds",
               "Forecast Type", "Business Unit", "Portfolio Major", "IBP Product Group"]]

    missing_pm = int(out["Portfolio Major"].isna().sum())
    if missing_pm:
        log.warn(f"{missing_pm:,} row(s) had no Portfolio Major in PDH or RO_Item_Master.")
    log.ok(f"qry_total_item_level_demand built — {len(out):,} rows")
    return out


# ── Stage 4: append this run into the cycle-over-cycle history tracker ────────

def _append_history_tracker(
    mgmt_full: pd.DataFrame, cycle_label: str, log: _Log,
) -> pd.DataFrame:
    """Upsert ``mgmt_full`` into the history tracker stamped with ``cycle_label``.

    Existing rows for ``cycle_label`` are replaced (idempotent re-runs); all
    other cycles are preserved verbatim. New rows match the tracker's on-disk
    text style: Start of Month ``M/D/YYYY``, a trailing ``.0`` stripped from
    whole pounds.
    """
    existing, _ = read_csv(_SECRETS_SECTION, _HISTORY_TRACKER_BLOB, read_csv_kwargs=_STR_READ_KW)
    if existing is None:
        existing = pd.DataFrame(columns=_TRACKER_COLUMNS)
    for col in _TRACKER_COLUMNS:               # tolerate a legacy/missing column
        if col not in existing.columns:
            existing[col] = ""
    existing = existing[_TRACKER_COLUMNS]

    dt = pd.to_datetime(mgmt_full["Start of Month"], errors="coerce")
    new_rows = pd.DataFrame({
        "Start of Month":     dt.map(lambda x: f"{x.month}/{x.day}/{x.year}" if pd.notna(x) else "").to_numpy(),
        "Item":               _norm_item(mgmt_full["Item"]).to_numpy(),
        "Item Description":   mgmt_full["Item Description"].astype("string").to_numpy(),
        "Party Site Number":  mgmt_full["Party Site Number"].astype("string").str.strip().to_numpy(),
        "Demand Plan Pounds": mgmt_full["Demand Plan Pounds"].astype("string").str.replace(r"\.0$", "", regex=True).to_numpy(),
        "Forecast Type":      mgmt_full["Forecast Type"].astype("string").str.strip().to_numpy(),
        "Business Unit":      mgmt_full["Business Unit"].astype("string").str.strip().to_numpy(),
        "Cycle":              cycle_label,
    })[_TRACKER_COLUMNS]

    keep = existing["Cycle"].astype(str).str.strip() != cycle_label
    replaced = int((~keep).sum())
    if replaced:
        log.info(f"History tracker: replacing {replaced:,} existing rows for cycle {cycle_label}.")
    combined = pd.concat([existing[keep], new_rows], ignore_index=True)[_TRACKER_COLUMNS]

    # De-duplicate: the tracker is an all-text frame, so normalise every
    # column (trim whitespace, <NA> → "") and drop fully-identical rows.
    # This stops duplicates accumulating in the published CSV from repeated
    # runs, upstream repeats, or a prior file that already carried them.
    combined = combined.apply(lambda c: c.astype("string").str.strip().fillna(""))
    before = len(combined)
    combined = combined.drop_duplicates(ignore_index=True)[_TRACKER_COLUMNS]
    deduped = before - len(combined)
    if deduped:
        log.info(f"History tracker: removed {deduped:,} duplicate row(s).")
    log.ok(f"qry_mgmt_plan_history_tracker → cycle {cycle_label} "
           f"(+{len(new_rows):,} rows, {len(combined):,} total)")
    return combined


# ── Orchestration ────────────────────────────────────────────────────────────

def run_demand_plan_pipeline(
    base_plan_bytes: bytes,
    *,
    anchor_month: date = _DEFAULT_ANCHOR_MONTH,
    forward_window_months: int = _DEFAULT_FORWARD_WINDOW_MONTHS,
    meeting_month_override: Optional[date] = None,
) -> DemandPlanResult:
    """Run the full Base Plan → demand-plan ETL and write every output to Fabric.

    Parameters
    ----------
    base_plan_bytes:
        Raw bytes of the uploaded ``ibp_base_plan_current.csv`` (must carry the
        ``month`` and ``Cycle`` columns in addition to the plan columns).
    anchor_month:
        First month of the RO 36-month calendar (stage 1). Default 2026-04-01.
    forward_window_months:
        Rows with ``Start of Month >= meeting_month + this`` are dropped.
    meeting_month_override:
        Forces the forward-window base month; when ``None`` the upload's
        ``month`` column is used.

    Everything is computed in memory and validated before any write, so a logic
    error never leaves a partial update. Never raises: failures are returned as
    ``ok=False`` with an ``error`` log entry.
    """
    log = _Log()
    result = DemandPlanResult(ok=False, log=log.entries)
    try:
        # ---- Read + validate the upload (no writes yet) ---------------------
        try:
            base_plan = pd.read_csv(io.BytesIO(base_plan_bytes), **_STR_READ_KW)
        except Exception as exc:  # noqa: BLE001
            log.err(f"Could not read the uploaded CSV: {exc}")
            return result
        if base_plan.empty:
            log.err("The uploaded ibp_base_plan_current.csv has no rows.")
            return result

        missing = [c for c in _BASE_PLAN_REQUIRED if c not in base_plan.columns]
        if missing:
            log.err(f"Upload is missing required column(s): {missing}")
            return result

        try:
            cycle = _single_value(base_plan["Cycle"], "Cycle")
        except ValueError as exc:
            log.err(str(exc))
            return result
        result.cycle = cycle

        if meeting_month_override is not None:
            meeting_ts = pd.Timestamp(meeting_month_override).normalize().replace(day=1)
        else:
            try:
                raw_month = _single_value(base_plan["month"], "month")
            except ValueError as exc:
                log.err(str(exc))
                return result
            meeting_ts = pd.to_datetime(raw_month, errors="coerce")
            if pd.isna(meeting_ts):
                log.err(f"Could not parse the upload's 'month' value: {raw_month!r}.")
                return result
            meeting_ts = meeting_ts.normalize().replace(day=1)

        window_end = meeting_ts + pd.DateOffset(months=forward_window_months)
        result.meeting_month = meeting_ts.date()
        result.window_end = window_end.date()
        log.info(f"Cycle: {cycle} · meeting month: {meeting_ts:%Y-%m-%d} · "
                 f"forward window < {window_end:%Y-%m-%d} · RO anchor: {anchor_month:%Y-%m-%d}")

        # ---- Archive the raw upload (audit / rollback) before anything else --
        try:
            archived = archive_bytes(
                _SECRETS_SECTION, _BASE_PLAN_ARCHIVE_DIR,
                "ibp_base_plan_current.csv", base_plan_bytes)
            log.info(f"Archived upload → 'Files/{archived}'.")
        except LakehouseIOError as exc:
            log.warn(f"Could not archive the upload (continuing): {exc}")

        # ---- Read supporting sources ----------------------------------------
        seed_df, _ = read_csv(_SECRETS_SECTION, _RO_SEED_BLOB, read_csv_kwargs=_STR_READ_KW)
        if seed_df is None or seed_df.empty:
            log.err(f"'Files/{_RO_SEED_BLOB}' is missing or empty — run the RO pipeline "
                    "first (Download/refresh RO_Seed in the RO section).")
            return result
        tbl_months, _ = read_csv(_SECRETS_SECTION, _TBL_MONTHS_BLOB, read_csv_kwargs=_STR_READ_KW)
        if tbl_months is None or tbl_months.empty:
            log.err(f"'Files/{_TBL_MONTHS_BLOB}' is missing or empty.")
            return result
        pdh, _ = read_csv(_SECRETS_SECTION, _PDH_BLOB, read_csv_kwargs=_STR_READ_KW)
        if pdh is None:
            pdh = pd.DataFrame(columns=["Item No", "Business Unit"] + _ATTR_COLS)
            log.warn(f"'Files/{_PDH_BLOB}' not found — dimensions/BU degrade to fallback.")
        ro_master, _ = read_csv(_SECRETS_SECTION, _RO_ITEMS_BLOB, read_csv_kwargs=_STR_READ_KW)
        if ro_master is None:
            ro_master = pd.DataFrame(columns=["Item #", "Business Unit"] + _ATTR_COLS)
            log.warn(f"'Files/{_RO_ITEMS_BLOB}' not found — RO→B2C fallback unavailable.")

        # ---- Compute everything in memory -----------------------------------
        tbl_ro_input = _build_tbl_ro_input(seed_df, anchor_month, log)
        mgmt_full, detail = _build_mgmt_plan_and_detail(
            base_plan, tbl_ro_input, tbl_months, pdh, ro_master,
            window_end=window_end, log=log)
        if mgmt_full.empty:
            log.err("Pipeline produced 0 mgmt-plan rows — nothing written "
                    "(check the upload, the forward window, and RO_Seed).")
            return result
        total_item = _build_total_item_level_demand(mgmt_full, pdh, ro_master, log)
        history_combined = _append_history_tracker(mgmt_full, cycle, log)

        # ---- Write outputs (only after every compute step succeeded) --------
        iso = {"date_format": "%Y-%m-%d"}
        write_csv(_SECRETS_SECTION, _TBL_RO_INPUT_BLOB, tbl_ro_input, etag=None, to_csv_kwargs=iso)
        write_csv(_SECRETS_SECTION, _MGMT_PLAN_FULL_BLOB, mgmt_full, etag=None, to_csv_kwargs=iso)
        write_csv(_SECRETS_SECTION, _DETAIL_BLOB, detail, etag=None, to_csv_kwargs=iso)
        write_csv(_SECRETS_SECTION, _TOTAL_ITEM_BLOB, total_item, etag=None, to_csv_kwargs=iso)
        write_csv(_SECRETS_SECTION, _HISTORY_TRACKER_BLOB, history_combined, etag=None)
        # Promote the upload to the live copy last (byte-for-byte fidelity).
        write_bytes(_SECRETS_SECTION, _BASE_PLAN_BLOB, base_plan_bytes)
        log.ok("All demand-plan files written to Fabric.")

        # ---- Headline stats -------------------------------------------------
        result.tbl_ro_input_rows = len(tbl_ro_input)
        result.mgmt_full_rows = len(mgmt_full)
        result.detail_rows = len(detail)
        result.total_item_rows = len(total_item)
        result.history_rows = len(history_combined)
        result.mgmt_total_lbs = float(
            pd.to_numeric(mgmt_full["Demand Plan Pounds"], errors="coerce").fillna(0).sum())
        result.ok = True
        return result

    except LakehouseIOError as exc:
        log.err(f"Fabric I/O error — no partial state written unless a write is "
                f"named above. Details: {exc}")
        return result
    except Exception as exc:  # noqa: BLE001 - surface any unexpected failure
        log.err(f"Pipeline failed: {exc}")
        return result


__all__ = ["DemandPlanResult", "run_demand_plan_pipeline"]
