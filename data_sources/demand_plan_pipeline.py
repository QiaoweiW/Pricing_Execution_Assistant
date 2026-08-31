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

Withdraw (the inverse of a run)
-------------------------------
:func:`withdraw_cycles` undoes an upload so a planner can start over: it drops
the chosen cycles' rows from the history tracker and deletes the four
single-cycle snapshots (2, 3 and the raw upload).  Everything is archived first.
``tbl_ro_input.csv`` is untouched — it derives from ``RO_Seed.csv``, not from
the base-plan upload.

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

# Reuse the RO pipeline's trivial run-log types + the shared Probability parser
# (that pipeline writes RO_Seed.csv, so it owns the column's contract).
from .ro_seed_pipeline import LogEntry, _Log, _parse_probability
from .fabric_lakehouse_io import (
    LakehouseIOError,
    archive_bytes,
    delete_blob,
    read_bytes,
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
# Timestamped copies of the plan CSVs are dropped here before each overwrite
# (audit / rollback), mirroring the base-plan upload archive above.
_DEMAND_PLAN_ARCHIVE_DIR: str = f"{_BASE}/Archive"

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
# Item-level attributes resolved PDH-first, RO-master-fallback (per field).
_ATTR_COLS = ["Portfolio Major", "Portfolio Minor", "Supply Format"]
# Portfolio Minor that marks the consumer (B2C) butter category.  PDH tags most
# packaged-butter SKUs B2B (they ship through retailer / distributor accounts),
# so the B2C filter forces this Portfolio Minor to B2C — matching the Demand
# Plan Comparison, which scopes Butter to "Packaged Butter".  Bulk / ingredient
# butter carries Portfolio Minor "Bulk Butter" and correctly stays B2B.
_PACKAGED_BUTTER_PMINOR = "Packaged Butter"
# Carried on qry_mgmt_plan_full + the history tracker so the Demand Plan
# Comparison reads its categorisation dims straight off the file instead of
# re-joining PDH + RO_Item_Master.  All three PDH dims travel; Brand is NOT
# carried — the comparison derives it from the Item Description (already on the
# file), so there is no need to persist it.
_MGMT_ATTR_COLS = list(_ATTR_COLS)
# qry_mgmt_plan_full.csv schema (also the history tracker minus Cycle).  The
# two attribute columns are appended LAST so any legacy reader that indexes by
# name still works and a plain column-order diff stays readable.
_MGMT_FULL_COLUMNS = [
    "Start of Month", "Item", "Item Description", "Party Site Number",
    "Demand Plan Pounds", "Forecast Type", "Business Unit",
] + _MGMT_ATTR_COLS
_TRACKER_COLUMNS = _MGMT_FULL_COLUMNS + ["Cycle"]


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


def _attach_item_attrs(
    df: pd.DataFrame,
    pdh: pd.DataFrame,
    ro_master: pd.DataFrame,
    *,
    cols: list[str] = _ATTR_COLS,
) -> pd.DataFrame:
    """Attach item attributes to *df* on ``Item``, PDH-primary → RO-master fallback.

    Per-field coalesce (PDH wins, RO_Item_Master fills blanks) exactly as the
    detail branch has always done — extracted to module scope so both the
    ``qry_mgmt_plan_full`` slice and the ``qry_demand_item_customer_detail``
    branch share ONE definition (no duplicated merge logic).  *cols* selects
    which attributes to bring across (e.g. just Portfolio Major + Supply Format
    for the mgmt-plan/tracker files).  Missing source columns degrade to blank.
    """
    def _lookup(src: pd.DataFrame, item_col: str, suffix: str) -> pd.DataFrame:
        present = [c for c in cols if c in src.columns]
        if item_col not in src.columns or not present:
            return pd.DataFrame(columns=["Item", *[f"{c}_{suffix}" for c in cols]])
        out = (
            src[[item_col, *present]].dropna(subset=[item_col])
            .assign(**{item_col: lambda d: _norm_item(d[item_col])})
            .drop_duplicates(item_col, keep="first")
            .rename(columns={item_col: "Item", **{c: f"{c}_{suffix}" for c in present}})
        )
        for c in cols:                       # keep a stable, complete column set
            if f"{c}_{suffix}" not in out.columns:
                out[f"{c}_{suffix}"] = pd.NA
        return out

    d = (
        df.merge(_lookup(pdh, "Item No", "pdh"), on="Item", how="left")
          .merge(_lookup(ro_master, "Item #", "ro"), on="Item", how="left")
    )
    for c in cols:
        d[c] = (d[f"{c}_pdh"].astype("string").str.strip()
                  .combine_first(d[f"{c}_ro"].astype("string").str.strip()))
    return d.drop(columns=[f"{c}_pdh" for c in cols] + [f"{c}_ro" for c in cols])


def _resolve_b2c_business_unit(
    df: pd.DataFrame,
    pdh: pd.DataFrame,
    ro_master: pd.DataFrame,
    ro_items_set: set,
) -> pd.Series:
    """Resolve each row's Business Unit for the B2C filter (aligned to ``df.index``).

    Resolution order:
      1. PDH ``Business Unit`` (primary), matched on the normalised Item.
      2. Items PDH doesn't classify (blank/missing) fall back to ``B2C`` when
         they appear in ``RO_Item_Master`` — the planner's curated B2C list.
      3. **Packaged Butter is forced to B2C** regardless of PDH.  PDH tags most
         packaged-butter SKUs ``B2B`` (they move through retailer / distributor
         accounts), so without this the demand plan kept only the handful PDH
         happens to mark B2C — in practice a few **Western Quarters** items —
         and silently dropped Elgin Solid / Elgin Quarter / Chips / …  This
         matches the Demand Plan Comparison, which already scopes Butter to
         Portfolio Minor = "Packaged Butter" as a B2C category.  Bulk /
         ingredient butter ("Bulk Butter") is untouched and stays B2B.

    Portfolio Minor for step 3 uses the SAME PDH-primary → RO-master cascade as
    :func:`_attach_item_attrs`, so the classification is consistent with the
    dims the file ultimately carries.
    """
    items = _norm_item(df["Item"])
    pdh_bu = (
        pdh[["Item No", "Business Unit"]].dropna(subset=["Item No"])
        .assign(**{"Item No": lambda d: _norm_item(d["Item No"]),
                   "Business Unit": lambda d: d["Business Unit"].astype("string").str.strip()})
        .drop_duplicates("Item No", keep="first")
        .set_index("Item No")["Business Unit"]
    )
    bu = items.map(pdh_bu)
    bu = bu.where(bu.ne(""), other=pd.NA)          # blank PDH BU == unclassified
    fb = bu.isna() & items.isin(ro_items_set)
    bu = bu.mask(fb, "B2C")
    # Packaged Butter → B2C (positional mask: _attach_item_attrs resets index).
    pminor = _attach_item_attrs(
        pd.DataFrame({"Item": items.to_numpy()}), pdh, ro_master,
        cols=["Portfolio Minor"],
    )["Portfolio Minor"]
    is_pkg_butter = (
        pminor.astype("string").str.strip().str.casefold()
        == _PACKAGED_BUTTER_PMINOR.casefold()
    ).fillna(False).to_numpy(dtype=bool)
    return bu.mask(is_pkg_butter, "B2C")


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
    for c in ("Lbs./yr", "PC$/yr", "Slotting"):
        s[c] = pd.to_numeric(s[c], errors="coerce")
    # Probability shares ONE parser with the RO seed pipeline (which writes this
    # column) so the two never disagree on scale.  A bare to_numeric here turned
    # "50%" into NaN → the row's R&O silently vanished from the demand plan,
    # while ro_seed_pipeline's [^\d.-] strip turned the same cell into 50.0 →
    # a 50x overstatement.  Same input, opposite failures.
    s["Probability"] = _parse_probability(s["Probability"], log)
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
            # A blank cell ("-") coerces to NaN → 0; a genuine negative
            # ("-51697") is PRESERVED — it's a demand risk / de-list.  Do NOT
            # strip "-", which turned "-51697" into "051697" (+51697), silently
            # flipping a risk into an opportunity.
            Value=lambda d: pd.to_numeric(
                d["Value"].astype(str).str.replace(",", "", regex=False).str.strip(),
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


# ── Row gates: the four filters that decide what reaches the demand plan ─────
#
# Every pound that leaves the Base Plan / R&O inputs and never arrives in
# qry_mgmt_plan_full was removed by exactly one of these, in this order.  The
# metadata below is the *explanation* half (surfaced by
# :mod:`data_sources.demand_plan_reconcile`); :func:`apply_row_gates` is the
# *behaviour* half and is the only place the filtering is written, so a bridge
# can never disagree with the pipeline about why a SKU vanished.

#: Ledger column naming the gate that dropped a row.
GATE_COL: str = "_gate"


@dataclass(frozen=True)
class RowGate:
    """One drop rule, with the planner-facing explanation attached."""
    id: str
    label: str
    reason: str
    fix: str


ROW_GATES: tuple[RowGate, ...] = (
    RowGate(
        id="demand_sign",
        label="Zero pounds, or a negative Base Plan row",
        reason=(
            "The row carries 0 lbs, or it is a Base Plan row with negative lbs. "
            "Zeros are always dropped; only R&O may be negative (a demand risk "
            "or de-list)."
        ),
        fix=(
            "Zero rows are normal padding and need no action.  A NEGATIVE Base "
            "Plan row is a data error — fix the sign in the upload, or move the "
            "line into the R&O seed if it really is a loss."
        ),
    ),
    RowGate(
        id="undated",
        label="Unparseable or blank Start of Month",
        reason=(
            "Start of Month could not be read as a date (blank, text, or an "
            "out-of-range Excel serial), so the row cannot be placed on the "
            "month axis."
        ),
        fix=(
            "Correct Start of Month in ibp_base_plan_current.csv (or tblMonths"
            ".csv for an R&O row) and re-upload."
        ),
    ),
    RowGate(
        id="forward_window",
        label="Beyond the forward window",
        reason=(
            "Start of Month falls on or after the cut-off (meeting month + the "
            "forward-window months set on the upload form, 24 by default).  "
            "Note there is NO lower bound — earlier months are always kept."
        ),
        fix=(
            "Expected for far-horizon rows.  Raise 'Forward window (months)' on "
            "the upload form if the plan genuinely needs to reach further out."
        ),
    ),
    RowGate(
        id="business_unit",
        label="Not B2C",
        reason=(
            "The item resolved to a non-B2C Business Unit, or to none at all.  "
            "Resolution order: PDH Business Unit → present in RO_Item_Master → "
            "B2C, with Packaged Butter forced to B2C.  An item in NEITHER PDH "
            "nor RO_Item_Master has no Business Unit and is dropped here."
        ),
        fix=(
            "If the SKU belongs in the B2C demand plan, add it to "
            "RO_Item_Master.csv (or correct its Business Unit / Portfolio Minor "
            "in PDH).  Genuinely B2B items are correctly excluded."
        ),
    ),
)

ROW_GATES_BY_ID: dict[str, RowGate] = {g.id: g for g in ROW_GATES}


@dataclass(frozen=True)
class GateResult:
    """Rows that survived every gate, plus a ledger of everything dropped.

    ``ledger`` carries the dropped rows verbatim with one extra column
    (:data:`GATE_COL`) naming the gate that removed them — the FIRST gate a row
    fails, since the gates run in sequence exactly as the pipeline applies them.
    Empty when nothing was dropped.
    """
    kept: pd.DataFrame
    ledger: pd.DataFrame


def apply_row_gates(
    combined: pd.DataFrame,
    *,
    window_end: pd.Timestamp,
    business_unit_fn,
) -> GateResult:
    """Apply the four row gates in pipeline order, recording every drop.

    This is the pipeline's own filter chain — :func:`_build_mgmt_plan_and_detail`
    calls it and uses ``kept``.  The reconciliation bridge calls the same
    function and reads ``ledger``, so the two can never drift.

    *business_unit_fn* resolves the Business Unit column and is invoked on the
    already-narrowed frame, immediately before the B2C gate — the same point
    (and therefore the same cost and the same result) as the original inline
    chain.
    """
    ledger: list[pd.DataFrame] = []

    def _gate(df: pd.DataFrame, keep: pd.Series, gate_id: str) -> pd.DataFrame:
        # Normalise the mask before splitting.  A comparison against a column
        # holding pd.NA — Business Unit for an item in neither PDH nor
        # RO_Item_Master — yields NA, which is falsy in BOTH ``keep`` and
        # ``~keep``: the row would drop out of the kept set AND the ledger, so
        # the very SKUs this ledger exists to surface would go unrecorded and
        # the waterfall would silently fail to add up.  NA means "not B2C",
        # i.e. do not keep.
        keep = keep.fillna(False).astype(bool)
        dropped = df.loc[~keep]
        if not dropped.empty:
            ledger.append(dropped.assign(**{GATE_COL: gate_id}))
        return df.loc[keep]

    # 1. Keep positive demand; ALSO keep NEGATIVE R&O (a demand risk / de-list)
    #    so it flows into qry_mgmt_plan_full, the history tracker and the APS
    #    mirror.  Base-plan rows stay strictly positive; zeros always go.
    pounds = combined["Demand Plan Pounds"]
    combined = _gate(
        combined,
        (pounds > 0) | ((pounds < 0) & (combined["Forecast Type"] == "R&O")),
        "demand_sign",
    )
    # 2 + 3. Dated, and inside the forward window (upper bound only).
    combined = _gate(combined, combined["Start of Month"].notna(), "undated")
    combined = _gate(
        combined, combined["Start of Month"] < window_end, "forward_window",
    ).reset_index(drop=True)

    combined["Start of Month"] = combined["Start of Month"].dt.normalize()
    combined["Item"] = _norm_item(combined["Item"])

    # 4. B2C only (PDH primary, RO-master fallback, Packaged Butter forced).
    combined["Business Unit"] = business_unit_fn(combined)
    combined = _gate(
        combined, combined["Business Unit"] == "B2C", "business_unit",
    ).reset_index(drop=True)

    return GateResult(
        kept=combined,
        ledger=(pd.concat(ledger, ignore_index=True) if ledger
                else combined.iloc[0:0].assign(**{GATE_COL: pd.Series(dtype=object)})),
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
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build ``qry_mgmt_plan_full`` and ``qry_demand_item_customer_detail``.

    Both outputs share one enrichment pass (B2C resolution + the filtered
    base-plan branch), exactly like the source notebook cell — so the heavy
    work is done once.

    Returns ``(mgmt_full, detail, gate_ledger)``.  The ledger is the by-product
    of :func:`apply_row_gates` — every input row that did NOT reach
    ``mgmt_full``, tagged with the gate that removed it.  The pipeline ignores
    it; :mod:`data_sources.demand_plan_reconcile` turns it into the planner's
    bridge.  Building it costs nothing extra: the rows are already in hand at
    the moment they are filtered out.
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

    # --- Combine + row filters (demand, dated, forward window) ---------------
    combined = pd.concat([base_long, ro_long], ignore_index=True)
    combined["Party Site Number"] = (
        combined["Party Site Number"].fillna("NA")
        if "Party Site Number" in combined.columns else "NA")
    # --- Row gates (demand sign, dated, forward window, B2C) -----------------
    # One shared implementation — see apply_row_gates.  ``gated.ledger`` names
    # the gate that removed each dropped row and is what the reconciliation
    # bridge reports; the pipeline itself only needs ``kept``.
    ro_items_set = set(_norm_item(ro_master["Item #"]).dropna())
    gated = apply_row_gates(
        combined,
        window_end=window_end,
        business_unit_fn=lambda d: _resolve_b2c_business_unit(
            d, pdh, ro_master, ro_items_set),
    )
    combined = gated.kept
    for gate in ROW_GATES:
        n = int((gated.ledger[GATE_COL] == gate.id).sum()) if not gated.ledger.empty else 0
        if n:
            log.info(f"Row gate '{gate.label}' dropped {n:,} row(s).")

    # Attach Portfolio Major + Supply Format so the file is self-describing and
    # the Demand Plan Comparison no longer re-joins PDH/RO_Item_Master.
    combined = _attach_item_attrs(combined, pdh, ro_master, cols=_MGMT_ATTR_COLS)

    mgmt_full = combined[_MGMT_FULL_COLUMNS].copy()
    assert mgmt_full["Forecast Type"].notna().all(), "Forecast Type has nulls!"
    assert (mgmt_full["Business Unit"] == "B2C").all(), "Non-B2C rows leaked through!"
    log.ok(f"qry_mgmt_plan_full built — {len(mgmt_full):,} rows")

    # --- Detail (Item × Customer grain) --------------------------------------
    detail = _build_detail(
        combined, base_plan, ro_input, qry_months, pdh, ro_master,
        ro_items_set=ro_items_set, window_end=window_end, month_cols=month_cols)
    log.ok(f"qry_demand_item_customer_detail built — {len(detail):,} rows")
    return mgmt_full, detail, gated.ledger


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
        # Same resolution as the mgmt-plan branch — incl. Packaged Butter = B2C.
        d = df.copy()
        d["Business Unit"] = _resolve_b2c_business_unit(
            d, pdh, ro_master, ro_items_set)
        return d[d["Business Unit"] == "B2C"].drop(columns=["Business Unit"]).reset_index(drop=True)

    def _attach_attrs(df: pd.DataFrame) -> pd.DataFrame:
        # Full attribute set (incl. Portfolio Minor) for the item×customer detail.
        return _attach_item_attrs(df, pdh, ro_master, cols=_ATTR_COLS)

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
            # A blank cell ("-") coerces to NaN → 0; a genuine negative
            # ("-51697") is PRESERVED — it's a demand risk / de-list.  Do NOT
            # strip "-", which turned "-51697" into "051697" (+51697), silently
            # flipping a risk into an opportunity.
            Value=lambda d: pd.to_numeric(
                d["Value"].astype(str).str.replace(",", "", regex=False).str.strip(),
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
    # R&O detail: keep negatives too (demand risk), drop only zeros.
    ro_detail = ro_detail[ro_detail["Demand Plan Pounds"] != 0]
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

# Tracker grain: every column EXCEPT the measure.  Two rows that agree on all of
# these are the same plan line as far as the tracker can express, so their
# pounds ADD — see _collapse_to_grain.
_TRACKER_GRAIN: list[str] = [c for c in _TRACKER_COLUMNS if c != "Demand Plan Pounds"]


def _tracker_text(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise an all-text tracker frame: trim every cell, ``<NA>`` → ``""``.

    The tracker is written and read as raw strings, so grain comparisons are
    only meaningful after this pass (``"7516"`` and ``" 7516"`` are one site).
    """
    return df.apply(lambda c: c.astype("string").str.strip().fillna(""))


def _collapse_to_grain(rows: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Collapse *rows* to :data:`_TRACKER_GRAIN`, **summing** Demand Plan Pounds.

    Returns ``(collapsed, rows_absorbed)``.

    Why sum rather than drop
    ------------------------
    ``qry_mgmt_plan_full`` is keyed on Item × Month × Party Site, but the base
    plan's true grain also includes **Corporate Group**, which the mgmt-plan
    schema does not carry.  Two genuinely distinct plan lines — different
    customers, same item, month, site and quantity — therefore arrive here as
    byte-identical rows.  The previous ``drop_duplicates`` read that as a data
    artefact and deleted one, silently losing real demand: cycle C6 lost
    1.03 M lbs across 76 rows, 0.62 M of it a single Costco line on item 342065
    that repeated in all seven forecast months.

    Summing is the only volume-preserving reading, and it is safe for the case
    the old dedupe was actually defending against — a re-run of the same cycle
    — because the caller drops that cycle's rows wholesale before appending, so
    a re-run never sees its own prior output.

    The trade-off is explicit: a genuine upstream repeat inside ONE cycle's
    upload is now added rather than discarded.  That is the correct default
    (a demand plan must not lose volume to a schema limitation), and the
    caller's reconciliation check makes any such repeat visible as a mismatch
    against ``qry_mgmt_plan_full`` rather than a silent adjustment.
    """
    if rows.empty:
        return rows, 0
    before = len(rows)
    lbs = pd.to_numeric(rows["Demand Plan Pounds"], errors="coerce").fillna(0.0)
    collapsed = (
        rows.assign(**{"Demand Plan Pounds": lbs})
            .groupby(_TRACKER_GRAIN, as_index=False, dropna=False, sort=False)
            .agg(**{"Demand Plan Pounds": ("Demand Plan Pounds", "sum")})
    )
    # Back to the tracker's on-disk text style (whole pounds carry no ".0").
    collapsed["Demand Plan Pounds"] = (
        collapsed["Demand Plan Pounds"].map(_fmt_pounds).astype("string"))
    return collapsed[_TRACKER_COLUMNS], before - len(collapsed)


def _fmt_pounds(value: float) -> str:
    """Format a pounds figure the way the tracker stores it (no trailing ``.0``).

    Rounds to 4dp first so a float sum can't leak ``…0000000004`` into the CSV,
    then trims trailing zeros — ``88143.0 → "88143"``, ``3950.5 → "3950.5"``.
    (``:g`` is deliberately not used: it switches to scientific notation past
    six significant figures, which these volumes routinely exceed.)
    """
    if pd.isna(value):
        return ""
    return f"{round(float(value), 4):.4f}".rstrip("0").rstrip(".") or "0"


def _append_history_tracker(
    mgmt_full: pd.DataFrame, cycle_label: str, log: _Log,
) -> pd.DataFrame:
    """Upsert ``mgmt_full`` into the history tracker stamped with ``cycle_label``.

    Existing rows for ``cycle_label`` are replaced (idempotent re-runs); all
    other cycles are preserved verbatim. New rows match the tracker's on-disk
    text style: Start of Month ``M/D/YYYY``, a trailing ``.0`` stripped from
    whole pounds.

    Duplicate handling differs by provenance, because the two cases are not the
    same thing:

    * **This cycle's rows** are freshly computed demand — collapsed to the
      tracker grain with their pounds **summed** (:func:`_collapse_to_grain`),
      so no volume is lost to the missing Corporate Group column.
    * **Other cycles' rows** are read back off disk and are already at grain;
      fully-identical rows there can only be file-level accumulation, so they
      are dropped as hygiene.  This cannot touch the incoming cycle.

    Raises ``ValueError`` when the cycle's tracker volume does not tie back to
    ``mgmt_full`` — see the reconciliation check below.
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
        # Left as-is here; _collapse_to_grain re-formats via _fmt_pounds after
        # summing, so this is the only place the on-disk number style is set.
        "Demand Plan Pounds": mgmt_full["Demand Plan Pounds"].astype("string").to_numpy(),
        "Forecast Type":      mgmt_full["Forecast Type"].astype("string").str.strip().to_numpy(),
        "Business Unit":      mgmt_full["Business Unit"].astype("string").str.strip().to_numpy(),
        # Portfolio Major / Supply Format carried through from mgmt_full so the
        # tracker is self-describing (comparison reads them directly).  Tolerant
        # of a legacy mgmt_full that predates these columns → blank.
        **{c: (mgmt_full[c] if c in mgmt_full.columns
               else pd.Series("", index=mgmt_full.index))
              .astype("string").str.strip().fillna("").to_numpy()
           for c in _MGMT_ATTR_COLS},
        "Cycle":              cycle_label,
    })[_TRACKER_COLUMNS]

    # ── This cycle: collapse to grain, SUMMING pounds (never dropping) ──────
    new_rows, absorbed = _collapse_to_grain(_tracker_text(new_rows))
    if absorbed:
        log.info(f"History tracker: {absorbed:,} row(s) for cycle {cycle_label} shared "
                 "the tracker grain and were summed (Corporate Group is not carried "
                 "on the mgmt-plan schema, so same-item/month/site lines collide).")

    # ── Other cycles: replace this cycle wholesale, de-dupe the rest ────────
    existing = _tracker_text(existing)
    keep = existing["Cycle"] != cycle_label
    replaced = int((~keep).sum())
    if replaced:
        log.info(f"History tracker: replacing {replaced:,} existing rows for cycle {cycle_label}.")
    prior = existing[keep]
    before = len(prior)
    # Fully-identical rows in already-published cycles are file-level
    # accumulation (a re-run can't cause them — that cycle is dropped above),
    # so removing them is safe and keeps the CSV from growing without bound.
    prior = prior.drop_duplicates(ignore_index=True)
    stale = before - len(prior)
    if stale:
        log.info(f"History tracker: removed {stale:,} duplicate row(s) from "
                 "previously-published cycles.")

    # Skip empty legs: concatenating an all-NA frame lets pandas infer dtypes
    # from it (deprecated, and it would demote these text columns to object).
    legs = [f for f in (prior, new_rows) if not f.empty]
    combined = (
        pd.concat(legs, ignore_index=True)[_TRACKER_COLUMNS] if legs
        else pd.DataFrame(columns=_TRACKER_COLUMNS)
    )

    # ── Reconciliation: the cycle must tie to mgmt_full, to the pound ───────
    # This is the invariant the old dedupe broke.  Asserting it here makes the
    # whole class of bug impossible to ship: the pipeline writes nothing (the
    # caller computes every output before any write) rather than publishing a
    # tracker that silently disagrees with the plan it was built from.
    src_lbs = float(pd.to_numeric(
        mgmt_full["Demand Plan Pounds"], errors="coerce").fillna(0.0).sum())
    trk_lbs = float(pd.to_numeric(
        new_rows["Demand Plan Pounds"], errors="coerce").fillna(0.0).sum())
    if abs(src_lbs - trk_lbs) > 1.0:           # 1 lb — float noise only
        raise ValueError(
            f"History tracker does not reconcile to qry_mgmt_plan_full for cycle "
            f"{cycle_label}: plan {src_lbs:,.0f} lbs vs tracker {trk_lbs:,.0f} lbs "
            f"(gap {src_lbs - trk_lbs:+,.0f}).  Nothing was written."
        )

    log.ok(f"qry_mgmt_plan_history_tracker → cycle {cycle_label} "
           f"(+{len(new_rows):,} rows, {len(combined):,} total, "
           f"{trk_lbs / 1_000_000:,.1f} M lbs — ties to the plan)")
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
        mgmt_full, detail, _gate_ledger = _build_mgmt_plan_and_detail(
            base_plan, tbl_ro_input, tbl_months, pdh, ro_master,
            window_end=window_end, log=log)
        if mgmt_full.empty:
            log.err("Pipeline produced 0 mgmt-plan rows — nothing written "
                    "(check the upload, the forward window, and RO_Seed).")
            return result
        total_item = _build_total_item_level_demand(mgmt_full, pdh, ro_master, log)
        history_combined = _append_history_tracker(mgmt_full, cycle, log)

        # ---- Archive the CURRENT plan CSVs before overwriting them ----------
        # A timestamped copy of each file about to be replaced lands in the
        # Archive folder (audit / rollback), same contract as the base-plan
        # upload archive above.  Best-effort: a missing file or archive hiccup
        # is logged, never fatal.
        for blob, leaf in ((_MGMT_PLAN_FULL_BLOB, "qry_mgmt_plan_full.csv"),
                           (_HISTORY_TRACKER_BLOB, "qry_mgmt_plan_history_tracker.csv")):
            try:
                prev, _etag = read_bytes(_SECRETS_SECTION, blob)
                if prev is not None:
                    dest = archive_bytes(_SECRETS_SECTION, _DEMAND_PLAN_ARCHIVE_DIR, leaf, prev)
                    log.info(f"Archived previous '{leaf}' → 'Files/{dest}'.")
            except LakehouseIOError as exc:
                log.warn(f"Could not archive previous '{leaf}' (continuing): {exc}")

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


@dataclass
class BackfillResult:
    """Outcome of :func:`backfill_plan_attribute_columns` (per file)."""
    ok: bool
    log: list[LogEntry] = field(default_factory=list)
    mgmt_full_rows: Optional[int] = None
    tracker_rows: Optional[int] = None
    mgmt_full_archived: Optional[str] = None
    tracker_archived: Optional[str] = None


def _backfill_one(
    df: pd.DataFrame, columns: list[str], pdh: pd.DataFrame, ro_master: pd.DataFrame,
) -> pd.DataFrame:
    """Return *df* with Portfolio Major / Supply Format present + populated.

    Existing non-blank values are preserved (manual fixes win); blanks and
    missing columns are filled from the PDH → RO_Item_Master cascade.  Column
    order is normalised to *columns* (mgmt-full or tracker schema).
    """
    out = df.copy()
    out["Item"] = _norm_item(out["Item"])
    cascade = _attach_item_attrs(
        out[["Item"]].copy(), pdh, ro_master, cols=_MGMT_ATTR_COLS)
    for c in _MGMT_ATTR_COLS:
        existing = (out[c].astype("string").str.strip()
                    if c in out.columns else pd.Series(pd.NA, index=out.index, dtype="string"))
        filled = cascade[c].astype("string").str.strip()
        out[c] = existing.where(existing.fillna("") != "", filled).fillna("")
    for c in columns:                     # tolerate any other legacy-missing column
        if c not in out.columns:
            out[c] = ""
    return out[columns]


def backfill_plan_attribute_columns() -> BackfillResult:
    """One-shot: add Portfolio Major + Supply Format to the two live plan CSVs.

    For migrating the EXISTING ``qry_mgmt_plan_full.csv`` and
    ``qry_mgmt_plan_history_tracker.csv`` in place (no base-plan re-run needed).
    Steps, per file: archive a timestamped copy to the Archive folder, enrich
    with the two attribute columns (PDH-primary → RO_Item_Master fallback,
    preserving any existing values), and write it back with the columns in the
    canonical schema order.  Idempotent — safe to re-run.  Never raises;
    failures come back as ``ok=False`` with an error log entry.
    """
    log = _Log()
    result = BackfillResult(ok=False, log=log.entries)
    try:
        pdh, _ = read_csv(_SECRETS_SECTION, _PDH_BLOB, read_csv_kwargs=_STR_READ_KW)
        if pdh is None:
            pdh = pd.DataFrame(columns=["Item No"] + _ATTR_COLS)
            log.warn(f"'Files/{_PDH_BLOB}' not found — Portfolio Major/Supply Format "
                     "will fill only from RO_Item_Master.")
        ro_master, _ = read_csv(_SECRETS_SECTION, _RO_ITEMS_BLOB, read_csv_kwargs=_STR_READ_KW)
        if ro_master is None:
            ro_master = pd.DataFrame(columns=["Item #"] + _ATTR_COLS)
            log.warn(f"'Files/{_RO_ITEMS_BLOB}' not found — RO_Item_Master fallback unavailable.")

        for blob, leaf, columns, is_tracker in (
            (_MGMT_PLAN_FULL_BLOB, "qry_mgmt_plan_full.csv", _MGMT_FULL_COLUMNS, False),
            (_HISTORY_TRACKER_BLOB, "qry_mgmt_plan_history_tracker.csv", _TRACKER_COLUMNS, True),
        ):
            df, _ = read_csv(_SECRETS_SECTION, blob, read_csv_kwargs=_STR_READ_KW)
            if df is None or df.empty:
                log.warn(f"'Files/{leaf}' is missing or empty — skipped.")
                continue
            prev_bytes, _ = read_bytes(_SECRETS_SECTION, blob)
            if prev_bytes is not None:
                dest = archive_bytes(_SECRETS_SECTION, _DEMAND_PLAN_ARCHIVE_DIR, leaf, prev_bytes)
                log.info(f"Archived '{leaf}' → 'Files/{dest}'.")
                if is_tracker:
                    result.tracker_archived = dest
                else:
                    result.mgmt_full_archived = dest
            enriched = _backfill_one(df, columns, pdh, ro_master)
            # Preserve on-disk text style (already strings) — no date reformat.
            write_csv(_SECRETS_SECTION, blob, enriched, etag=None)
            filled = int((enriched[_MGMT_ATTR_COLS].apply(
                lambda s: s.astype(str).str.strip() != "").any(axis=1)).sum())
            log.ok(f"{leaf}: wrote {len(enriched):,} rows, {filled:,} with a "
                   f"Portfolio Major/Supply Format value.")
            if is_tracker:
                result.tracker_rows = len(enriched)
            else:
                result.mgmt_full_rows = len(enriched)

        result.ok = True
        return result
    except LakehouseIOError as exc:
        log.err(f"Fabric I/O error during backfill: {exc}")
        return result
    except Exception as exc:  # noqa: BLE001
        log.err(f"Backfill failed: {exc}")
        return result


# ── Reconciliation inputs ────────────────────────────────────────────────────

@dataclass
class ReconciliationInputs:
    """Everything the demand-plan bridge needs, read in one pass.

    Lives here rather than in :mod:`data_sources.demand_plan_reconcile` because
    this module already owns every one of these blob paths and the read
    conventions that go with them — the reconciler stays pure (frames in,
    findings out) and no path is spelled twice.

    Any member can be ``None``: the withdraw tool deletes the base plan and the
    published outputs, and the bridge is exactly the tool a planner reaches for
    in that state, so a missing file must degrade rather than raise.
    """
    base_plan: Optional[pd.DataFrame] = None
    ro_seed: Optional[pd.DataFrame] = None
    tbl_months: Optional[pd.DataFrame] = None
    pdh: Optional[pd.DataFrame] = None
    ro_master: Optional[pd.DataFrame] = None
    published_mgmt_full: Optional[pd.DataFrame] = None
    missing: tuple[str, ...] = ()

    @property
    def can_rebuild(self) -> bool:
        """True when the four inputs the pipeline needs are all present."""
        return all(f is not None and not f.empty for f in (
            self.base_plan, self.ro_seed, self.tbl_months))


def load_reconciliation_inputs() -> ReconciliationInputs:
    """Read the demand-plan inputs + the published output for the bridge.

    Uses the SAME blob constants and read keywords as
    :func:`run_demand_plan_pipeline`, so the bridge always reconciles the files
    the pipeline actually consumes.  Missing blobs are collected in ``missing``
    for the UI to report instead of failing the whole panel.
    """
    wanted = (
        ("base_plan", _BASE_PLAN_BLOB),
        ("ro_seed", _RO_SEED_BLOB),
        ("tbl_months", _TBL_MONTHS_BLOB),
        ("pdh", _PDH_BLOB),
        ("ro_master", _RO_ITEMS_BLOB),
        ("published_mgmt_full", _MGMT_PLAN_FULL_BLOB),
    )
    frames: dict[str, Optional[pd.DataFrame]] = {}
    missing: list[str] = []
    for attr, blob in wanted:
        try:
            df, _etag = read_csv(_SECRETS_SECTION, blob, read_csv_kwargs=_STR_READ_KW)
        except LakehouseIOError:
            df = None
        if df is None or df.empty:
            missing.append(blob)
            df = None
        frames[attr] = df
    return ReconciliationInputs(missing=tuple(missing), **frames)


def meeting_month_of(base_plan: Optional[pd.DataFrame]) -> Optional[pd.Timestamp]:
    """First-of-month demand-review month carried on a base-plan upload.

    The upload's ``month`` column is the pipeline's forward-window anchor, so
    the bridge reads it the same way rather than guessing from the data.
    """
    if base_plan is None or base_plan.empty or "month" not in base_plan.columns:
        return None
    stamp = pd.to_datetime(base_plan["month"].iloc[0], errors="coerce")
    return None if pd.isna(stamp) else stamp.normalize().replace(day=1)


# ── Withdraw: undo a base-plan upload's effect on the demand plan ────────────
#
# The pipeline writes ONE cycle into the history tracker and overwrites four
# single-cycle snapshots.  Withdrawing therefore has two halves: drop the
# chosen cycles' rows from the (multi-cycle) tracker, and delete the four
# snapshots outright — they only ever describe the most recent run, so once a
# cycle is pulled they are stale by definition and the next upload rebuilds
# them.  Everything is archived first, so a withdraw is recoverable.

# Snapshot blobs cleared by a withdraw, as (blob path, archive dir).  The base
# plan archives beside its own uploads; the three derived files share the plan
# archive — matching exactly where run_demand_plan_pipeline puts them.
_WITHDRAW_SNAPSHOTS: tuple[tuple[str, str], ...] = (
    (_MGMT_PLAN_FULL_BLOB, _DEMAND_PLAN_ARCHIVE_DIR),
    (_DETAIL_BLOB,         _DEMAND_PLAN_ARCHIVE_DIR),
    (_TOTAL_ITEM_BLOB,     _DEMAND_PLAN_ARCHIVE_DIR),
    (_BASE_PLAN_BLOB,      _BASE_PLAN_ARCHIVE_DIR),
)


@dataclass
class WithdrawResult:
    """Outcome of one withdraw — the UI renders this verbatim."""
    ok: bool
    log: list[LogEntry] = field(default_factory=list)
    cycles: tuple[str, ...] = ()
    rows_removed: int = 0
    rows_remaining: int = 0
    files_deleted: tuple[str, ...] = ()
    files_absent: tuple[str, ...] = ()

    @property
    def warnings(self) -> list[str]:
        return [e.text for e in self.log if e.level == "warning"]

    @property
    def errors(self) -> list[str]:
        return [e.text for e in self.log if e.level == "error"]


def list_history_tracker_cycles() -> list[str]:
    """Cycle labels currently in the history tracker, oldest → newest.

    Reads the tracker directly (no Streamlit cache) so the withdraw picker
    always reflects what is actually on the lakehouse right now — offering a
    cycle that a colleague already withdrew would be worse than a slow read.

    Delegates to :func:`~data_sources.demand_plan_comparison.list_tracker_cycles`
    rather than re-deriving the order here, so the picker cannot drift from the
    cycle dropdowns elsewhere on the page.  That matters: the tracker stores
    ``Start of Month`` as a mix of ``M/D/YYYY`` text and Excel day-serials, and
    only that module's tolerant parser reads both — a plain ``to_datetime``
    silently mis-dates the serial rows and scrambles the horizon order.

    The import is local to dodge a cycle: ``demand_plan_comparison`` is a
    consumer of this module's file layout, not the other way round.
    """
    from data_sources.demand_plan_comparison import list_tracker_cycles

    df, _etag = read_csv(
        _SECRETS_SECTION, _HISTORY_TRACKER_BLOB, read_csv_kwargs=_STR_READ_KW)
    return list_tracker_cycles(df)


def withdraw_cycles(cycles: list[str]) -> WithdrawResult:
    """Remove *cycles* from the history tracker and clear the four snapshots.

    Undoes the effect of one or more ``ibp_base_plan_current.csv`` uploads so a
    planner can re-upload from a clean slate.

    What it touches
    ---------------
    * ``qry_mgmt_plan_history_tracker.csv`` — rows whose ``Cycle`` is in
      *cycles* are dropped; every other cycle is preserved byte-for-byte.
    * ``qry_mgmt_plan_full.csv``, ``qry_demand_item_customer_detail.csv``,
      ``qry_total_item_level_demand.csv``, ``Append New Plan/
      ibp_base_plan_current.csv`` — deleted.  These are single-cycle snapshots
      of the latest run, so they are cleared regardless of which cycle was
      withdrawn; the next upload regenerates all four.

    ``tbl_ro_input.csv`` is deliberately NOT touched: it is derived from
    ``RO_Seed.csv`` (the RO pipeline's output), not from the base-plan upload,
    so it is not part of this upload's footprint.

    Safety
    ------
    Every file is archived before it is overwritten or deleted — the tracker
    and the three derived files into ``Demand Plan/Archive``, the base plan
    into its own ``Append New Plan/Archive`` — so a withdraw is recoverable by
    re-uploading the archived copy.  The new tracker is computed and validated
    in memory before any write, mirroring
    :func:`run_demand_plan_pipeline`'s never-leave-partial-state contract.

    Never raises: failures come back as ``ok=False`` with an error log entry.
    """
    log = _Log()
    result = WithdrawResult(ok=False, log=log.entries)

    wanted = [c for c in (str(c).strip() for c in (cycles or [])) if c]
    if not wanted:
        log.err("Pick at least one cycle to withdraw.")
        return result
    result.cycles = tuple(wanted)

    try:
        # ---- Compute the new tracker in memory (no writes yet) -------------
        existing, _etag = read_csv(
            _SECRETS_SECTION, _HISTORY_TRACKER_BLOB, read_csv_kwargs=_STR_READ_KW)
        if existing is None or existing.empty:
            log.warn(f"'Files/{_HISTORY_TRACKER_BLOB}' is missing or empty — "
                     "nothing to remove from the tracker.")
            remaining = None
        else:
            if "Cycle" not in existing.columns:
                log.err(f"'Files/{_HISTORY_TRACKER_BLOB}' has no 'Cycle' column — "
                        "cannot withdraw by cycle.")
                return result
            labels = existing["Cycle"].astype(str).str.strip()
            drop = labels.isin(set(wanted))
            result.rows_removed = int(drop.sum())
            if not result.rows_removed:
                log.warn("No tracker rows matched "
                         f"{', '.join(wanted)} — the tracker is unchanged.")
                remaining = None
            else:
                remaining = existing.loc[~drop].reset_index(drop=True)
                result.rows_remaining = len(remaining)
                left = sorted(set(labels[~drop]) - {""})
                log.info(f"Tracker: removing {result.rows_removed:,} row(s) for "
                         f"{', '.join(wanted)}; {result.rows_remaining:,} row(s) "
                         f"remain across {len(left)} cycle(s): "
                         f"{', '.join(left) if left else 'none'}.")

        # ---- Archive + rewrite the tracker ---------------------------------
        if remaining is not None:
            _archive_existing(_HISTORY_TRACKER_BLOB, _DEMAND_PLAN_ARCHIVE_DIR, log)
            write_csv(_SECRETS_SECTION, _HISTORY_TRACKER_BLOB, remaining, etag=None)
            log.ok(f"qry_mgmt_plan_history_tracker → {result.rows_remaining:,} row(s).")

        # ---- Archive + delete the four single-cycle snapshots ---------------
        deleted: list[str] = []
        absent: list[str] = []
        for blob, archive_dir in _WITHDRAW_SNAPSHOTS:
            leaf = blob.rsplit("/", 1)[-1]
            _archive_existing(blob, archive_dir, log)
            if delete_blob(_SECRETS_SECTION, blob):
                deleted.append(leaf)
            else:
                absent.append(leaf)
        result.files_deleted = tuple(deleted)
        result.files_absent = tuple(absent)
        if deleted:
            log.ok(f"Deleted {len(deleted)} snapshot file(s): {', '.join(deleted)}.")
        if absent:
            log.info(f"Already absent (nothing to delete): {', '.join(absent)}.")

        result.ok = True
        return result

    except LakehouseIOError as exc:
        log.err(f"Fabric I/O error during withdraw: {exc}")
        return result
    except Exception as exc:  # noqa: BLE001 — surface any unexpected failure
        log.err(f"Withdraw failed: {exc}")
        return result


def _archive_existing(blob: str, archive_dir: str, log: _Log) -> None:
    """Best-effort timestamped copy of *blob* into *archive_dir* before it changes.

    Mirrors the archive step in :func:`run_demand_plan_pipeline`: a missing file
    or an archive hiccup is logged, never fatal — losing the audit copy must not
    block the operation the planner asked for.
    """
    leaf = blob.rsplit("/", 1)[-1]
    try:
        prev, _etag = read_bytes(_SECRETS_SECTION, blob)
        if prev is not None:
            dest = archive_bytes(_SECRETS_SECTION, archive_dir, leaf, prev)
            log.info(f"Archived '{leaf}' → 'Files/{dest}'.")
    except LakehouseIOError as exc:
        log.warn(f"Could not archive '{leaf}' (continuing): {exc}")


__all__ = [
    "DemandPlanResult", "run_demand_plan_pipeline",
    "BackfillResult", "backfill_plan_attribute_columns",
    "WithdrawResult", "withdraw_cycles", "list_history_tracker_cycles",
    "ReconciliationInputs", "load_reconciliation_inputs", "meeting_month_of",
    "RowGate", "ROW_GATES", "ROW_GATES_BY_ID", "GateResult", "apply_row_gates",
    "GATE_COL",
]
