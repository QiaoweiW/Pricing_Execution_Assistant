"""
Distribution Tracker → RO_History_Tracker pipeline (Streamlit-native).

This is the Streamlit/OneLake port of the two Microsoft Fabric notebook cells
that planners previously ran by hand:

  1. **Merge** the freshly-uploaded ``Distribution_Tracker.csv`` into
     ``Distribution_Tracker_History.csv`` — date-overlap cleanup, schema align,
     dedup, type/customer-name normalisation — then **build** ``RO_Seed.csv``
     (filter ``Reflected in APS = no`` / not ``declined`` / ``Probability > 0``,
     except **R&O risk lines** — see :mod:`data_sources.ro_risk` — which bypass
     the ``declined`` gate; aggregate duplicate source rows).
  2. **Expand** RO_Seed (7 computed columns, stable RO Key assignment, ``Month``)
     and **merge** it into ``RO_History_Tracker.csv`` (replace matching-Month
     rows, append, dedup).

Why a dedicated module
----------------------
The notebook used direct lakehouse filesystem I/O (``/lakehouse/default/…``),
which only works *inside* Fabric.  Streamlit reaches OneLake through the ADLS
connector layer (:mod:`data_sources.fabric_lakehouse_io`), so the logic is
re-expressed against ``read_csv`` / ``write_csv`` / ``delete_blob``.

Two deliberate departures from the notebook, both for safety in a live app:

* **Compute-then-write.**  Every output frame is built fully in memory *before*
  any Fabric write happens, so a parsing/logic error can never leave the three
  output files in a half-updated, mutually-inconsistent state.
* **Structured logging.**  Each ``print(...)`` / warning from the notebook
  becomes a :class:`LogEntry` with a level, so the UI can render the run report
  (and surface warnings prominently) instead of dumping text to a console.

The function is pure-ish: its only side effects are the explicit Fabric writes
at the end and the source-file delete.  It never touches Streamlit, so it is
unit-testable and safe to call from anywhere.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd
from pandas.tseries.offsets import MonthEnd

from .ro_risk import risk_mask
from .ro_rules_config import RoRulesConfig
from .fabric_lakehouse_io import (
    LakehouseIOError,
    archive_bytes,
    delete_blob,
    read_bytes,
    read_csv,
    write_csv,
)

# ── OneLake locations (relative to "Files/") ─────────────────────────────────
_SECRETS_SECTION: str = "fabric_htst"
_SOURCE_BLOB_PATH: str = "RO Tracking/Append_New_History/Distribution_Tracker.csv"
# Every uploaded Distribution_Tracker is archived here (timestamped) before the
# run, so prior inputs are recoverable.
_RO_INPUT_ARCHIVE_DIR: str = "RO Tracking/Append_New_History/Archive"
_DIST_HISTORY_BLOB_PATH: str = "RO Tracking/Distribution_Tracker_History.csv"
_RO_SEED_BLOB_PATH: str = "RO Tracking/RO_Seed.csv"
_RO_HISTORY_TRACKER_BLOB_PATH: str = "RO Tracking/RO_History_Tracker.csv"

# Read every RO CSV as raw strings with blanks preserved — the pipeline does its
# own typing, exactly like the notebook.
_STR_READ_KW: dict = {"dtype": str, "keep_default_na": False}

# The snapshot/date column. Older exports call it "Date"; we standardise to "Month".
_DATE_COLUMN = "Month"

# Header rename map applied to both the new file and history.
_RENAME_MAP = {
    "Anticipated Annual Lbs. Vol": "Lbs./yr",
    "Annual PC $": "PC$/yr",
    "Total Anticipated Slotting Costs": "Slotting",
}

# RO_Seed business columns (group keys + summed metrics + final column order).
_AGG_KEYS = ["Format", "Customer", "Taxonomy", "Brand", "Item #",
             "Item Desc", "Probability", "First Ship Date"]
_SUM_COLS = ["Lbs./yr", "PC$/yr", "Slotting"]
_RO_SEED_COLS = ["Format", "Customer", "Taxonomy", "Brand", "Item #",
                 "Item Desc", "Probability", "First Ship Date", "Lbs./yr",
                 "PC$/yr", "Slotting"]

# RO expansion (stage 2) columns.
_MATCH_COLS = ["Format", "Customer", "Taxonomy", "Brand", "Item #"]
_BUSINESS_COLS = ["Format", "Customer", "Taxonomy", "Brand", "Item #",
                  "Item Desc", "Probability", "First Ship Date",
                  "Lbs./yr", "PC$/yr", "Slotting"]
_EXPANSION_COLS = ["First Ship Round", "Lbs./yr Exp", "Days in Year",
                   "FY Lbs. Total", "FY Lbs. Exp", "RO Key", "Month"]


# ── Structured run log ───────────────────────────────────────────────────────

@dataclass
class LogEntry:
    """One line of the run report. ``level`` ∈ info | success | warning | error."""
    level: str
    text: str


@dataclass
class PipelineResult:
    """Outcome of one pipeline run — the UI renders this verbatim."""
    ok: bool
    log: list[LogEntry] = field(default_factory=list)
    # Headline stats (None until the relevant stage completes).
    snapshot_months: list[str] = field(default_factory=list)
    dist_history_rows: Optional[int] = None
    ro_seed_rows: Optional[int] = None
    ro_history_rows: Optional[int] = None
    new_ro_keys: Optional[int] = None
    ro_seed_total_lbs: Optional[float] = None

    @property
    def warnings(self) -> list[str]:
        return [e.text for e in self.log if e.level == "warning"]

    @property
    def errors(self) -> list[str]:
        return [e.text for e in self.log if e.level == "error"]


class _Log:
    """Tiny accumulator so each stage reads like the notebook's print()s."""

    def __init__(self) -> None:
        self.entries: list[LogEntry] = []

    def info(self, text: str) -> None:
        self.entries.append(LogEntry("info", text))

    def ok(self, text: str) -> None:
        self.entries.append(LogEntry("success", text))

    def warn(self, text: str) -> None:
        self.entries.append(LogEntry("warning", text))

    def err(self, text: str) -> None:
        self.entries.append(LogEntry("error", text))


# ── Date canonicalisation (shared by both stages) ────────────────────────────

def _canon_date(series: pd.Series) -> pd.Series:
    """Parse mixed date representations (mm/dd/yyyy, ISO, Excel serial) → datetime."""
    s = series.astype(str).str.strip()
    s = s.replace(["nan", "NaN", "NaT", "None", "NULL", ""], pd.NA)
    is_serial = s.str.fullmatch(r"\d+(\.0+)?", na=False)  # pure numeric ⇒ Excel serial
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    if is_serial.any():
        out.loc[is_serial] = pd.to_datetime(
            pd.to_numeric(s[is_serial]), origin="1899-12-30", unit="D")
    out.loc[~is_serial] = pd.to_datetime(s[~is_serial], errors="coerce")
    return out


def _canon_date_str(series: pd.Series) -> pd.Series:
    """Canonical date → ``mm/dd/yyyy`` text (blank for unparseable)."""
    return _canon_date(series).dt.strftime("%m/%d/%Y").fillna("")


def _scrub_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Strip BOM, collapse whitespace, trim — then standardise Date→Month."""
    df = df.copy()
    df.columns = (
        df.columns.str.replace("﻿", "", regex=False)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )
    if "Month" not in df.columns and "Date" in df.columns:
        df = df.rename(columns={"Date": "Month"})
    return df.rename(columns=_RENAME_MAP)


def _dedupe_identical_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop fully-identical rows, trimming text-cell whitespace first.

    A naive ``drop_duplicates`` misses rows that are logically identical
    but differ by a stray leading/trailing space or a CSV round-trip
    artefact in a text cell — the usual reason duplicates survive into the
    written history.  We strip every object (text) column's edge
    whitespace BEFORE comparing so the dedupe is clean, and the trimmed
    values carry through to the written file.  Numeric / Int64 columns are
    left untouched.

    Returns ``(deduped_df, n_removed)``.
    """
    if df is None or df.empty:
        return df, 0
    out = df.copy()
    for col in out.select_dtypes(include="object").columns:
        out[col] = out[col].astype(str).str.strip()
    before = len(out)
    out = out.drop_duplicates(ignore_index=True)
    return out, before - len(out)


# ── Stage 1: merge history + build RO_Seed ───────────────────────────────────

def _merge_history_and_build_seed(
    df_new: pd.DataFrame,
    df_history: pd.DataFrame,
    log: _Log,
    *,
    config: Optional[RoRulesConfig] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Return ``(combined_history, ro_seed_filtered, snapshot_months)``.

    ``config`` (defaults to the canonical rules when ``None``) is threaded
    into :func:`_build_ro_seed` so a user rules override propagates through
    the pipeline entrypoint.
    """
    df_new = _scrub_headers(df_new)
    df_history = _scrub_headers(df_history)
    log.info(f"New file: {df_new.shape[0]:,} rows, {df_new.shape[1]} cols")
    log.info(f"History file: {df_history.shape[0]:,} rows, {df_history.shape[1]} cols")

    new_months = (
        set(df_new[_DATE_COLUMN].dropna().unique())
        if _DATE_COLUMN in df_new.columns else set()
    )
    snapshot_months = sorted(str(m) for m in new_months)
    log.info(f"Snapshot date(s) in new file: {snapshot_months or 'NONE FOUND'}")

    # 1. Remove overlapping snapshot dates from history (avoid stacking).
    if _DATE_COLUMN in df_new.columns and _DATE_COLUMN in df_history.columns:
        if new_months:
            before = len(df_history)
            df_history = df_history[~df_history[_DATE_COLUMN].isin(new_months)]
            removed = before - len(df_history)
            if removed > 0:
                log.info(f"Date overlap: removed {removed:,} existing history rows "
                         f"for dates {snapshot_months}")
            else:
                log.ok(f"No date overlap in history for dates {snapshot_months}")
    else:
        log.warn(f"Date column '{_DATE_COLUMN}' missing from one or both files — "
                 "skipping date-based cleanup.")

    # 2. Schema align — reorder/extend the new file to match history.
    if list(df_new.columns) != list(df_history.columns) and len(df_history.columns):
        only_new = set(df_new.columns) - set(df_history.columns)
        only_hist = set(df_history.columns) - set(df_new.columns)
        log.warn("Column mismatch detected between new file and history. "
                 f"Only in new: {sorted(only_new) or '—'}; "
                 f"only in history: {sorted(only_hist) or '—'}. "
                 "Aligned the new file to the history schema (missing cols blank).")
        df_new = df_new.reindex(columns=list(df_history.columns), fill_value="")
    else:
        log.ok("Schemas match")

    # 3. Append, then normalise types/text, THEN de-duplicate.  Dedup must
    #    run AFTER cleanup: raw rows that differ only in date format
    #    (7/1/2026 vs 07/01/2026), Item # formatting (380574 vs 380574.0)
    #    or Customer casing are distinct strings until cleanup canonicalises
    #    them — deduping first would leave those identical-after-cleanup
    #    rows in the written history (the reported duplicate-rows bug).
    df_combined = pd.concat([df_history, df_new], ignore_index=True)
    df_combined = _clean_combined_types(df_combined)
    log.ok("Formatting and cleanup applied")

    before = len(df_combined)
    df_combined, removed = _dedupe_identical_rows(df_combined)
    log.info(f"De-duplicated history (post-cleanup): {before:,} → "
             f"{len(df_combined):,} rows ({removed:,} identical rows removed)")

    # 5. Build RO_Seed from the current snapshot.
    ro_seed = _build_ro_seed(df_combined, new_months, log, config=config)
    return df_combined, ro_seed, snapshot_months


def _clean_combined_types(df: pd.DataFrame) -> pd.DataFrame:
    """Port of the notebook's type/text normalisation (section 5)."""
    df = df.copy()

    # 5a. Text fields → str, drop literal 'nan'.
    for col in ["Format", "Customer", "Taxonomy", "Brand", "Item Desc"]:
        if col in df.columns:
            df[col] = df[col].astype(str).replace("nan", "")

    # 5a-i. Customer case-collision fix — prefer a non-UPPERCASE spelling,
    #       else the most common variant, for each case-insensitive group.
    if "Customer" in df.columns:
        groups: dict[str, list[str]] = {}
        for name in df["Customer"].value_counts().index:
            groups.setdefault(str(name).lower().strip(), []).append(name)
        canonical: dict[str, str] = {}
        for key, variants in groups.items():
            non_upper = [v for v in variants if not str(v).isupper()]
            canonical[key] = non_upper[0] if non_upper else variants[0]
        df["Customer"] = df["Customer"].map(
            lambda x: canonical.get(str(x).lower().strip(), x))

    # 5b. Item # → nullable Int64.
    if "Item #" in df.columns:
        df["Item #"] = pd.to_numeric(
            df["Item #"].astype(str).str.replace(r"[^\d.-]", "", regex=True),
            errors="coerce").astype("Int64")

    # 5c. First Ship Date → mm/dd/yyyy (Excel serials + text).
    if "First Ship Date" in df.columns:
        df["First Ship Date"] = _canon_date_str(df["First Ship Date"])

    # 5d. Decimal numerics (incl. Probability for the >0 filter later).
    for col in ["Lbs./yr", "PC$/yr", "Slotting", "Probability"]:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(r"[^\d.-]", "", regex=True),
                errors="coerce").astype(float)
    return df


def _carry_forward_historical_risks(
    prior_df: pd.DataFrame,
    current_snapshot_df: pd.DataFrame,
    cfg: RoRulesConfig,
    log: _Log,
) -> pd.DataFrame:
    """Return risk rows from earlier snapshots not yet in the current snapshot.

    Used by :func:`_build_ro_seed` to guarantee RO_Seed reconciles with the RO
    Summary Report: a risk carried in RO_History_Tracker across snapshots must
    still be a first-class row in RO_Seed even when the latest Distribution
    Tracker upload dropped it — otherwise the demand-plan ETL under-reports
    R&O relative to what the Summary Report shows.

    Selection rule
    --------------
    1. Apply the SAME :func:`data_sources.ro_risk.risk_mask` rule the RO
       Summary uses to *prior_df* under the given *cfg*.
    2. Deduplicate to the LATEST ``Month`` per business key
       ``(Format, Customer, Taxonomy, Brand, Item #)`` so a re-run never
       stacks multiple historical snapshots of the same RO into RO_Seed.
    3. Drop business keys whose row is already present in the current
       snapshot — the fresh Tracker value wins over a stale carried-over one.

    Empty inputs (or a prior slice with no qualifying risks) return an empty
    frame so the caller's ``concat`` is a no-op.
    """
    if prior_df is None or prior_df.empty:
        return prior_df.iloc[0:0].copy() if prior_df is not None else pd.DataFrame()

    reflected_col = (
        "Reflected in APS"
        if cfg.reflected_in_aps_only and "Reflected in APS" in prior_df.columns
        else None
    )
    is_risk = risk_mask(
        prior_df,
        volume_col="Lbs./yr",
        probability_col="Probability",
        reflected_col=reflected_col,
        min_probability=cfg.min_risk_probability,
        require_negative_volume=cfg.risk_requires_negative_volume,
    )
    candidates = prior_df.loc[is_risk]
    if candidates.empty:
        return candidates

    # Pick the latest-Month row per business key.  Parsing Month here (rather
    # than string-sorting) tolerates the mixed date shapes _canon_date already
    # copes with elsewhere in the pipeline.
    working = candidates.copy()
    working["__month_ts"] = _canon_date(working[_DATE_COLUMN])
    working = working.sort_values(
        by="__month_ts", kind="stable", na_position="first",
    )
    latest = (
        working.drop_duplicates(subset=_MATCH_COLS, keep="last")
        .drop(columns="__month_ts")
    )

    # Exclude business keys already covered by the current snapshot — the
    # fresh Tracker row is authoritative for that key.
    if not current_snapshot_df.empty:
        snapshot_keys = set(
            current_snapshot_df[_MATCH_COLS]
            .astype(str).apply(tuple, axis=1)
        )
        latest_keys = latest[_MATCH_COLS].astype(str).apply(tuple, axis=1)
        latest = latest.loc[~latest_keys.isin(snapshot_keys)]

    if not latest.empty:
        log.info(
            f"Carried forward {len(latest):,} historical risk row(s) from prior "
            f"snapshots (latest-Month copy per business key, current-snapshot "
            f"rows take precedence)."
        )
    return latest


def _build_ro_seed(
    df_combined: pd.DataFrame,
    new_months: set,
    log: _Log,
    *,
    config: Optional[RoRulesConfig] = None,
) -> pd.DataFrame:
    """Filter the combined history to the seed extract (section 7).

    ``config`` (canonical defaults when ``None``) drives every user-tunable
    gate — the Reflected-in-APS whitelist, the Pipeline Status excludes and
    the Opportunity probability threshold — and the Risk carve-out that
    bypasses them.  All three gates are logged so a planner reviewing the run
    can see exactly which rules the seed was built under.
    """
    cfg = config or RoRulesConfig.default()
    if _DATE_COLUMN in df_combined.columns and new_months:
        in_snapshot = df_combined[_DATE_COLUMN].isin(new_months)
        df = df_combined.loc[in_snapshot].copy()
        log.info(f"RO_Seed snapshot rows (Month in {sorted(str(m) for m in new_months)}): "
                 f"{len(df):,}")
        # Carry forward risks captured in EARLIER snapshots.
        #
        # A row that still satisfies the risk criteria today but was captured
        # in a prior snapshot (and dropped from this cycle's Distribution
        # Tracker upload) would otherwise vanish from RO_Seed — even though it
        # persists in RO_History_Tracker → RO_Comparison_Output and shows up
        # in the RO Summary Report.  That silent divergence is the reason
        # qry_mgmt_plan_full can under-report R&O compared to the Summary
        # (see data_sources.ro_risk_reconcile for the diagnostic view).
        #
        # We only bring forward RISK rows (a positive Opportunity from a stale
        # cycle is properly forgotten — the current snapshot is the source of
        # truth for Opportunities), and only ONE copy per business key: the
        # most-recent Month wins so we never stack multiple historical
        # snapshots of the same RO into RO_Seed.  Rows whose business key is
        # already in the current snapshot are skipped — the fresh Tracker
        # value always beats a carried-over one.
        carried = _carry_forward_historical_risks(
            df_combined.loc[~in_snapshot], df, cfg, log,
        )
        if not carried.empty:
            df = pd.concat([df, carried], ignore_index=True)
    else:
        df = df_combined.copy()
        log.warn(f"Date column '{_DATE_COLUMN}' missing or no snapshot dates — "
                 f"seeding from all {len(df):,} combined rows.")

    # R&O "risk" lines (see data_sources.ro_risk for the one canonical rule:
    # Reflected-in-APS = no AND Lbs./yr < 0 AND Probability ≥ threshold) bypass
    # the Pipeline-Status + Probability gates so a probable loss still reaches
    # RO_Seed → the mgmt plan / history / APS even when its status is declined
    # or its probability sits below the Opportunity threshold.
    is_risk = risk_mask(
        df, volume_col="Lbs./yr", probability_col="Probability",
        reflected_col="Reflected in APS" if cfg.reflected_in_aps_only else None,
        min_probability=cfg.min_risk_probability,
        require_negative_volume=cfg.risk_requires_negative_volume,
    )
    if cfg.reflected_in_aps_only and "Reflected in APS" in df.columns:
        df = df[df["Reflected in APS"].astype(str).str.strip().str.lower() == "no"]
        is_risk = is_risk.loc[df.index]
        log.info(f"After 'Reflected in APS = no': {len(df):,} rows")

    excludes = cfg.normalised_excludes()
    if excludes and "Pipeline Status" in df.columns:
        status_l = df["Pipeline Status"].astype(str).str.lower()
        drop_mask = pd.Series(False, index=df.index)
        for tok in excludes:
            drop_mask = drop_mask | status_l.str.contains(tok, na=False)
        # Risk lines bypass the Pipeline Status gate (committed loss > status).
        df = df[(~drop_mask) | is_risk]
        is_risk = is_risk.loc[df.index]
        log.info(
            f"After 'Pipeline Status ∉ {list(cfg.pipeline_status_excludes)}' "
            f"(risk-exempt): {len(df):,} rows"
        )

    if "Probability" in df.columns:
        prob_threshold = float(cfg.min_opp_probability)
        ok = pd.to_numeric(df["Probability"], errors="coerce").fillna(0.0) > prob_threshold
        df = df[ok | is_risk]
        log.info(
            f"After 'Probability > {prob_threshold:g}' (risk-exempt): {len(df):,} rows"
        )

    # Aggregate duplicate source rows: sum the metric columns per business key.
    agg_keys = [c for c in _AGG_KEYS if c in df.columns]
    sum_cols = [c for c in _SUM_COLS if c in df.columns]
    for c in sum_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    before = len(df)
    if agg_keys and sum_cols:
        df = df.groupby(agg_keys, dropna=False, as_index=False)[sum_cols].sum(min_count=1)
        log.info(f"After aggregation (sum dup rows): {len(df):,} rows "
                 f"({before - len(df):,} duplicates collapsed)")

    # Enforce the RO_Seed column set/order; create any missing as blank.
    missing = [c for c in _RO_SEED_COLS if c not in df.columns]
    if missing:
        log.warn(f"RO_Seed missing columns created blank: {missing}")
        for c in missing:
            df[c] = None
    return df[_RO_SEED_COLS].copy()


# ── Stage 2: expand RO_Seed + merge into RO_History_Tracker ──────────────────

def _resolve_seed_month(snapshot_months: list[str], log: _Log) -> str:
    """Pick the canonical ``Month`` label to stamp on the expanded seed.

    Uses the **snapshot month from the uploaded file** (what the planner put in
    the ``Month`` column) — NOT ``date.today()`` — so a July snapshot uploaded
    in June is still recorded as July. The original notebook used today's date,
    which silently mislabels the run whenever it isn't executed during the
    snapshot's own month. Falls back to the current month only when the upload
    carries no usable Month at all.
    """
    parsed = (_canon_date(pd.Series(snapshot_months))
              if snapshot_months else pd.Series([], dtype="datetime64[ns]"))
    valid = sorted(d for d in parsed if pd.notna(d))
    if not valid:
        fallback = pd.Timestamp(date.today()).replace(day=1)
        log.warn(f"Upload has no usable snapshot Month — stamping RO_History with "
                 f"the current month {fallback:%m/%d/%Y} instead.")
        return fallback.strftime("%m/%d/%Y")
    chosen = valid[-1]
    if len(valid) > 1:
        spanned = ", ".join(d.strftime("%m/%d/%Y") for d in valid)
        log.warn(f"Upload spans multiple snapshot months ({spanned}) — stamping "
                 f"RO_History with the latest, {chosen:%m/%d/%Y}.")
    return pd.Timestamp(chosen).strftime("%m/%d/%Y")


def _expand_seed(
    ro_seed: pd.DataFrame,
    df_ro_hist: pd.DataFrame,
    anchor_ts: pd.Timestamp,
    seed_month: str,
    log: _Log,
) -> tuple[pd.DataFrame, int]:
    """Return ``(expanded_seed, new_ro_key_count)``."""
    df_seed = ro_seed.copy()

    # Idempotency guard: drop any pre-existing expansion columns, canonicalise
    # the ship date, and collapse exact-duplicate business rows.
    df_seed = df_seed.drop(columns=[c for c in _EXPANSION_COLS if c in df_seed.columns])
    df_seed["First Ship Date"] = _canon_date_str(df_seed["First Ship Date"])
    before = len(df_seed)
    df_seed = df_seed.drop_duplicates(subset=_BUSINESS_COLS, keep="first").reset_index(drop=True)
    if before != len(df_seed):
        log.info(f"Expansion: removed {before - len(df_seed):,} exact-duplicate seed rows "
                 f"({before:,} → {len(df_seed):,})")

    # Numeric conversions for the maths.
    for col in ["Probability", "Lbs./yr", "PC$/yr", "Slotting"]:
        df_seed[col] = pd.to_numeric(
            df_seed[col].astype(str).str.replace(r"[^\d.-]", "", regex=True),
            errors="coerce").astype(float)
    df_seed["Item #"] = pd.to_numeric(
        df_seed["Item #"].astype(str).str.replace(r"[^\d.-]", "", regex=True),
        errors="coerce").astype("Int64")

    fsd = _canon_date(df_seed["First Ship Date"])

    # First Ship Round = IF(DAY>1, EOMONTH+1, d) — round up to next month start.
    fsr = pd.Series(pd.to_datetime(
        np.where(fsd.dt.day > 1, (fsd + MonthEnd(0)) + pd.Timedelta(days=1), fsd)
    ), index=df_seed.index)

    df_seed["Lbs./yr Exp"] = df_seed["Probability"] * df_seed["Lbs./yr"]
    days = ((anchor_ts - fsr).dt.days + 1).fillna(0)
    df_seed["Days in Year"] = days.clip(lower=0, upper=365).astype(int)
    df_seed["FY Lbs. Total"] = df_seed["Lbs./yr"] * df_seed["Days in Year"] / 365
    df_seed["FY Lbs. Exp"] = df_seed["Lbs./yr Exp"] * df_seed["Days in Year"] / 365
    df_seed["First Ship Round"] = fsr.dt.strftime("%m/%d/%Y").fillna("")

    # RO Key — stable per (Format, Customer, Taxonomy, Brand, Item #) across
    # history; new combos get the next integer after the existing max.
    key_map: dict[tuple, int] = {}
    max_key = 0
    if "RO Key" in df_ro_hist.columns and len(df_ro_hist) > 0:
        hist = df_ro_hist.copy()
        hist["Item #"] = pd.to_numeric(
            hist["Item #"].astype(str).str.replace(r"[^\d.-]", "", regex=True),
            errors="coerce").astype("Int64")
        hist["RO Key"] = pd.to_numeric(hist["RO Key"], errors="coerce").astype("Int64")
        lk = hist.dropna(subset=["RO Key"]).drop_duplicates(subset=_MATCH_COLS, keep="first")
        key_map = {tuple(combo): k for combo, k in
                   zip(lk[_MATCH_COLS].astype(str).values.tolist(), lk["RO Key"])}
        if hist["RO Key"].notna().any():
            max_key = int(hist["RO Key"].dropna().max())
    log.info(f"Max existing RO Key in history: {max_key}")

    combos = df_seed[_MATCH_COLS].astype(str).apply(tuple, axis=1)
    next_key, new_assigned = max_key, 0
    for c in combos:  # first-appearance order
        if c not in key_map:
            next_key += 1
            key_map[c] = next_key
            new_assigned += 1
    df_seed["RO Key"] = combos.map(key_map).astype("Int64")
    log.info(f"Assigned {new_assigned:,} new RO Keys" if new_assigned
             else "All seed rows matched existing RO Keys in history.")

    # Month = the snapshot month from the uploaded file (NOT today's date), so a
    # snapshot is recorded under its own month regardless of when it's run.
    df_seed["Month"] = seed_month

    final_cols = _BUSINESS_COLS + ["First Ship Round", "Lbs./yr Exp", "Days in Year",
                                   "FY Lbs. Total", "FY Lbs. Exp", "RO Key", "Month"]
    missing = [c for c in final_cols if c not in df_seed.columns]
    if missing:
        raise LakehouseIOError(f"Missing expected columns after expansion: {missing}")
    df_seed = df_seed[final_cols]
    log.ok(f"RO_Seed expanded to {df_seed.shape[1]} columns, {len(df_seed):,} rows")
    return df_seed, new_assigned


def _merge_into_ro_history(
    df_seed_expanded: pd.DataFrame,
    df_ro_hist: pd.DataFrame,
    log: _Log,
) -> pd.DataFrame:
    """Replace matching-Month rows in RO_History_Tracker, then append the seed."""
    # Round-trip the expanded seed through CSV so it is all-strings, exactly
    # matching the on-disk RO_Seed.csv we publish (the history is all-strings).
    buf = io.StringIO()
    df_seed_expanded.to_csv(buf, index=False)
    df_seed_str = pd.read_csv(io.StringIO(buf.getvalue()), **_STR_READ_KW)
    df_seed_str.columns = df_seed_str.columns.str.replace(r"\s+", " ", regex=True).str.strip()

    df_hist = df_ro_hist
    if len(df_hist) > 0:
        if list(df_hist.columns) != list(df_seed_str.columns):
            log.warn("RO_History_Tracker column order differs — reindexing to match seed.")
            df_hist = df_hist.reindex(columns=df_seed_str.columns, fill_value="")
        df_hist = df_hist.copy()
        df_hist["Month"] = _canon_date_str(df_hist["Month"])

    df_seed_str["Month"] = _canon_date_str(df_seed_str["Month"])
    seed_months = set(df_seed_str["Month"].unique())
    log.info(f"Months in new seed: {sorted(seed_months)}")

    before = len(df_hist)
    removed = 0
    if "Month" in df_hist.columns and len(df_hist) > 0:
        mask = df_hist["Month"].isin(seed_months)
        removed = int(mask.sum())
        df_hist = df_hist.loc[~mask].reset_index(drop=True)
    log.info(f"Removed {removed:,} existing RO_History rows matching seed Month(s)")

    combined = pd.concat([df_hist, df_seed_str], ignore_index=True)
    combined, deduped = _dedupe_identical_rows(combined)
    log.info(f"RO_History rebuild: {before:,} − {removed:,} removed + {len(df_seed_str):,} "
             f"appended − {deduped:,} identical = {len(combined):,} final rows")
    return combined


# ── Public entry point ───────────────────────────────────────────────────────

def run_distribution_tracker_pipeline(
    new_file_bytes: bytes,
    *,
    anchor_date: date,
    config: Optional[RoRulesConfig] = None,
) -> PipelineResult:
    """Run the full Distribution Tracker → RO_History_Tracker pipeline.

    Parameters
    ----------
    new_file_bytes:
        Raw bytes of the uploaded ``Distribution_Tracker.csv``.
    anchor_date:
        Fiscal year-end anchor (``Analysis!$B$3``) driving ``Days in Year``.
    config:
        Optional :class:`RoRulesConfig` overriding the canonical Opportunity /
        Risk rules for this run (``None`` → planner defaults).  The rules
        panel in the Streamlit view passes the user's current selection here
        so a "Regenerate RO_Seed with current rules" click actually applies
        the on-screen configuration.

    All three Fabric outputs are computed in memory first and only written once
    every stage succeeds, so a logic error never leaves a partial update. On a
    successful run the staged source file is deleted (notebook parity).

    Never raises: any failure is captured as an ``error`` log entry and returned
    with ``ok=False`` so the caller can render it.
    """
    log = _Log()
    cfg = config or RoRulesConfig.default()
    result = PipelineResult(ok=False, log=log.entries)
    anchor_ts = pd.Timestamp(anchor_date)
    log.info(f"Fiscal year-end anchor: {anchor_ts:%m/%d/%Y}")

    try:
        # ---- Read inputs (no writes yet) ------------------------------------
        try:
            df_new = pd.read_csv(io.BytesIO(new_file_bytes), **_STR_READ_KW)
        except Exception as exc:  # noqa: BLE001
            log.err(f"Could not read the uploaded CSV: {exc}")
            return result
        if df_new.empty:
            log.err("The uploaded Distribution_Tracker.csv has no rows.")
            return result

        # Archive the raw upload (timestamped) before doing anything else, so a
        # bad input can always be recovered. Non-fatal: an archive failure must
        # not block the run.
        try:
            archived = archive_bytes(
                _SECRETS_SECTION, _RO_INPUT_ARCHIVE_DIR,
                "Distribution_Tracker.csv", new_file_bytes)
            log.info(f"Archived upload → 'Files/{archived}'.")
        except LakehouseIOError as exc:
            log.warn(f"Could not archive the upload (continuing): {exc}")

        df_history, _ = read_csv(_SECRETS_SECTION, _DIST_HISTORY_BLOB_PATH,
                                 read_csv_kwargs=_STR_READ_KW)
        if df_history is None:
            log.warn(f"'Files/{_DIST_HISTORY_BLOB_PATH}' not found — starting a fresh "
                     "distribution history from this upload.")
            df_history = pd.DataFrame(columns=df_new.columns)

        df_ro_hist, _ = read_csv(_SECRETS_SECTION, _RO_HISTORY_TRACKER_BLOB_PATH,
                                 read_csv_kwargs=_STR_READ_KW)
        if df_ro_hist is None:
            log.warn(f"'Files/{_RO_HISTORY_TRACKER_BLOB_PATH}' not found — it will be "
                     "created from this run's seed.")
            df_ro_hist = pd.DataFrame()
        else:
            df_ro_hist = df_ro_hist.copy()
            df_ro_hist.columns = (
                df_ro_hist.columns.str.replace(r"\s+", " ", regex=True).str.strip())

        # ---- Compute (all in memory) ----------------------------------------
        log.info(
            "RO rules — "
            f"APS-only={cfg.reflected_in_aps_only}, "
            f"Pipeline excludes={list(cfg.pipeline_status_excludes)}, "
            f"Opp Prob>{cfg.min_opp_probability:g}, "
            f"Risk Prob≥{cfg.min_risk_probability:g}, "
            f"Risk requires Vol<0={cfg.risk_requires_negative_volume}"
        )
        dist_history, ro_seed, snapshot_months = _merge_history_and_build_seed(
            df_new, df_history, log, config=cfg)
        result.snapshot_months = snapshot_months

        # Stamp the run with the upload's snapshot month (not today's date).
        seed_month = _resolve_seed_month(snapshot_months, log)
        log.info(f"RO_History Month stamp: {seed_month}")

        seed_expanded, new_keys = _expand_seed(
            ro_seed, df_ro_hist, anchor_ts, seed_month, log)
        ro_history_combined = _merge_into_ro_history(seed_expanded, df_ro_hist, log)

        # ---- Write outputs (only after every compute step succeeded) --------
        write_csv(_SECRETS_SECTION, _DIST_HISTORY_BLOB_PATH, dist_history, etag=None)
        log.ok(f"Distribution_Tracker_History.csv updated — {len(dist_history):,} rows")

        write_csv(_SECRETS_SECTION, _RO_SEED_BLOB_PATH, seed_expanded, etag=None)
        log.ok(f"RO_Seed.csv generated — {len(seed_expanded):,} rows")

        write_csv(_SECRETS_SECTION, _RO_HISTORY_TRACKER_BLOB_PATH,
                  ro_history_combined, etag=None)
        log.ok(f"RO_History_Tracker.csv updated — {len(ro_history_combined):,} rows")

        # ---- Cleanup: delete the staged source (notebook parity) ------------
        try:
            if delete_blob(_SECRETS_SECTION, _SOURCE_BLOB_PATH):
                log.info(f"Cleanup: deleted staged source 'Files/{_SOURCE_BLOB_PATH}'.")
            else:
                log.info("Cleanup: no staged source file to delete.")
        except LakehouseIOError as exc:
            # Non-fatal: the run succeeded; a stale source file is harmless.
            log.warn(f"Cleanup skipped — could not delete staged source: {exc}")

        # ---- Headline stats -------------------------------------------------
        result.dist_history_rows = len(dist_history)
        result.ro_seed_rows = len(seed_expanded)
        result.ro_history_rows = len(ro_history_combined)
        result.new_ro_keys = new_keys
        if "Lbs./yr" in seed_expanded.columns:
            result.ro_seed_total_lbs = float(
                pd.to_numeric(seed_expanded["Lbs./yr"], errors="coerce").fillna(0).sum())
        result.ok = True
        return result

    except LakehouseIOError as exc:
        log.err(f"Fabric I/O error — no partial state written before this point "
                f"unless a write is named above. Details: {exc}")
        return result
    except Exception as exc:  # noqa: BLE001 - surface any unexpected failure
        log.err(f"Pipeline failed: {exc}")
        return result


# ── Rebuild-without-upload: reapply current rules to published history ──────

def rebuild_ro_seed_from_published_history(
    *,
    anchor_date: date,
    config: RoRulesConfig,
) -> PipelineResult:
    """Rebuild ``RO_Seed.csv`` (+ downstream) from published history + rules.

    Same output contract as :func:`run_distribution_tracker_pipeline` — three
    files are written to Fabric in-memory-first — but the *input* is the
    already-published ``Distribution_Tracker_History.csv`` rather than a new
    upload.  Used by the Streamlit rules panel's **Regenerate RO_Seed with
    current rules** button so a planner tuning the Opportunity / Risk gates
    doesn't need to re-upload the distribution tracker to see the pipeline
    react.

    The snapshot month(s) used to select seed rows are the latest month(s)
    present in the history — matches the notebook's "seed from the most
    recent snapshot" contract.
    """
    log = _Log()
    result = PipelineResult(ok=False, log=log.entries)
    anchor_ts = pd.Timestamp(anchor_date)
    log.info(f"Fiscal year-end anchor: {anchor_ts:%m/%d/%Y}")
    log.info(
        "RO rules — "
        f"APS-only={config.reflected_in_aps_only}, "
        f"Pipeline excludes={list(config.pipeline_status_excludes)}, "
        f"Opp Prob>{config.min_opp_probability:g}, "
        f"Risk Prob≥{config.min_risk_probability:g}, "
        f"Risk requires Vol<0={config.risk_requires_negative_volume}"
    )

    try:
        df_history, _ = read_csv(_SECRETS_SECTION, _DIST_HISTORY_BLOB_PATH,
                                 read_csv_kwargs=_STR_READ_KW)
        if df_history is None or df_history.empty:
            log.err(f"'Files/{_DIST_HISTORY_BLOB_PATH}' not found or empty — "
                    "cannot rebuild the seed without a published history.")
            return result
        df_history = _scrub_headers(df_history)
        df_history = _clean_combined_types(df_history)

        df_ro_hist, _ = read_csv(_SECRETS_SECTION, _RO_HISTORY_TRACKER_BLOB_PATH,
                                 read_csv_kwargs=_STR_READ_KW)
        if df_ro_hist is None:
            log.warn(f"'Files/{_RO_HISTORY_TRACKER_BLOB_PATH}' not found — it will be "
                     "created from this rebuild's seed.")
            df_ro_hist = pd.DataFrame()
        else:
            df_ro_hist = df_ro_hist.copy()
            df_ro_hist.columns = (
                df_ro_hist.columns.str.replace(r"\s+", " ", regex=True).str.strip())

        # Seed off the latest snapshot month(s) already in history — matches the
        # notebook's contract and the upload path's "seed = current snapshot".
        if _DATE_COLUMN in df_history.columns:
            months = sorted(str(m) for m in df_history[_DATE_COLUMN].dropna().unique())
            new_months = {months[-1]} if months else set()
        else:
            new_months = set()
        snapshot_months = sorted(str(m) for m in new_months)
        result.snapshot_months = snapshot_months
        log.info(f"Seeding from latest snapshot month(s) in history: "
                 f"{snapshot_months or 'NONE FOUND'}")

        ro_seed = _build_ro_seed(df_history, new_months, log, config=config)
        seed_month = _resolve_seed_month(snapshot_months, log)
        log.info(f"RO_History Month stamp: {seed_month}")
        seed_expanded, new_keys = _expand_seed(
            ro_seed, df_ro_hist, anchor_ts, seed_month, log)
        ro_history_combined = _merge_into_ro_history(seed_expanded, df_ro_hist, log)

        write_csv(_SECRETS_SECTION, _RO_SEED_BLOB_PATH, seed_expanded, etag=None)
        log.ok(f"RO_Seed.csv rebuilt — {len(seed_expanded):,} rows")
        write_csv(_SECRETS_SECTION, _RO_HISTORY_TRACKER_BLOB_PATH,
                  ro_history_combined, etag=None)
        log.ok(f"RO_History_Tracker.csv updated — {len(ro_history_combined):,} rows")

        result.ro_seed_rows = len(seed_expanded)
        result.ro_history_rows = len(ro_history_combined)
        result.new_ro_keys = new_keys
        if "Lbs./yr" in seed_expanded.columns:
            result.ro_seed_total_lbs = float(
                pd.to_numeric(seed_expanded["Lbs./yr"], errors="coerce").fillna(0).sum())
        result.ok = True
        return result
    except LakehouseIOError as exc:
        log.err(f"Fabric I/O error during rebuild — {exc}")
        return result
    except Exception as exc:  # noqa: BLE001
        log.err(f"Rebuild failed: {exc}")
        return result


# ── RO_Seed download helpers (for the RO section's download button) ──────────

def ro_seed_blob_path() -> str:
    """POSIX path (under ``Files/``) of the canonical ``RO_Seed.csv``."""
    return _RO_SEED_BLOB_PATH


def fetch_ro_seed_raw_bytes() -> bytes:
    """Return ``RO_Seed.csv`` as raw bytes for a byte-for-byte download.

    Raises :class:`LakehouseIOError` if the blob is missing/unreadable so the
    caller can surface a clear message.
    """
    raw, _etag = read_bytes(_SECRETS_SECTION, _RO_SEED_BLOB_PATH)
    if raw is None:
        raise LakehouseIOError(f"'Files/{_RO_SEED_BLOB_PATH}' not found.")
    return raw


# ── Maintenance: delete a calendar month's rows from a history file ──────────

# UI-facing target key → (blob path, friendly label). Keeps the view from
# hard-coding OneLake paths.
DELETE_TARGETS: dict[str, tuple[str, str]] = {
    "ro_history": (_RO_HISTORY_TRACKER_BLOB_PATH, "RO_History_Tracker.csv"),
    "distribution_history": (_DIST_HISTORY_BLOB_PATH, "Distribution_Tracker_History.csv"),
}


@dataclass
class MonthDeleteResult:
    """Outcome of one delete-a-month maintenance action."""
    ok: bool
    target_label: str
    level: str = "info"          # success | warning | error
    message: str = ""
    removed: Optional[int] = None
    remaining: Optional[int] = None


def delete_history_rows_for_month(target: str, month: date) -> MonthDeleteResult:
    """Delete every row falling in ``month``'s calendar month from a history file.

    ``target`` is a key of :data:`DELETE_TARGETS`. Matching is by **(year,
    month)** of a canonicalised ``Month`` (or legacy ``Date``) column, so it is
    robust to the various stored formats (``7/1/2026``, ``07/01/2026``,
    ``2026-07-01``, Excel serials) and removes the whole month regardless of day.

    Destructive but safe-by-construction: reads, filters in memory, and only
    writes back when at least one row actually matched. Never raises — failures
    come back as ``ok=False`` with an explanatory message.
    """
    if target not in DELETE_TARGETS:
        return MonthDeleteResult(False, target, "error", f"Unknown target '{target}'.")
    blob_path, label = DELETE_TARGETS[target]
    month_name = pd.Timestamp(month).strftime("%B %Y")

    try:
        df, _etag = read_csv(_SECRETS_SECTION, blob_path, read_csv_kwargs=_STR_READ_KW)
    except LakehouseIOError as exc:
        return MonthDeleteResult(False, label, "error", f"Could not read {label}: {exc}")
    if df is None:
        return MonthDeleteResult(False, label, "error",
                                 f"{label} not found in Fabric — nothing to delete.")

    df = df.copy()
    df.columns = df.columns.str.replace(r"\s+", " ", regex=True).str.strip()
    month_col = "Month" if "Month" in df.columns else ("Date" if "Date" in df.columns else None)
    if month_col is None:
        return MonthDeleteResult(False, label, "error",
                                 f"{label} has no 'Month' or 'Date' column to match on.")

    col_dt = _canon_date(df[month_col])
    tgt = pd.Timestamp(month)
    mask = (col_dt.dt.year == tgt.year) & (col_dt.dt.month == tgt.month)
    removed = int(mask.sum())
    if removed == 0:
        return MonthDeleteResult(True, label, "warning",
                                 f"No {month_name} rows found in {label} — nothing deleted.",
                                 removed=0, remaining=len(df))

    kept = df.loc[~mask].reset_index(drop=True)
    try:
        write_csv(_SECRETS_SECTION, blob_path, kept, etag=None)
    except LakehouseIOError as exc:
        return MonthDeleteResult(False, label, "error",
                                 f"Matched {removed:,} {month_name} rows in {label} but the "
                                 f"write-back failed (no rows deleted): {exc}")
    return MonthDeleteResult(True, label, "success",
                             f"Deleted {removed:,} {month_name} row(s) from {label} — "
                             f"{len(kept):,} rows remain.",
                             removed=removed, remaining=len(kept))


__all__ = [
    "LogEntry",
    "PipelineResult",
    "run_distribution_tracker_pipeline",
    "DELETE_TARGETS",
    "MonthDeleteResult",
    "delete_history_rows_for_month",
]
