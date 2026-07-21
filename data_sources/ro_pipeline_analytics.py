"""RO pipeline analytics — metric tiles + urgency / probability views.

Pure, Streamlit-free transforms over the per-program **RO Comparison
Output** frame (the 28-column frame produced by
:data:`data_sources.ro_comparison.OUTPUT_COLUMNS`, held in-memory on the
Demand Planner page under ``_SS_SUMMARY_DF``).  Everything here is a
plain ``DataFrame``-in / ``DataFrame``-or-``dataclass``-out function so
it unit-tests without a Fabric round-trip or a Streamlit session.

What this module powers (all rendered ABOVE the RO Summary table)
-----------------------------------------------------------------
1. **Metric tiles** (:func:`compute_pipeline_metrics`)
   * Gross Pipeline (unweighted)          — Σ LE Annual Opportunity
   * Full-Year, Risk-adjusted Pipeline    — Σ LE Year1 Probabilized (FY28)
   * In-Year, Risk-adjusted Pipeline      — Σ LE Current Fiscal Probabilized (FY27)
   * Committed                            — Σ In-Year Probabilized on rows at
                                            ``LE Probability >= 0.95`` (+ its
                                            concentration of the In-Year total)
   Because the RO Comparison output is entirely B2C, a sum over every
   row equals the RO Summary "Total B2C" roll-up row by construction —
   so these tiles reconcile with the summary table below them.  Values
   are RAW LBS (the page scales to millions for display, matching the
   rest of the section).

2. **Urgency ranking** (:func:`build_urgency_ranking`)
   FY27 in-year probabilized lbs per Portfolio Major, split into first-
   ship-date urgency buckets (``< 30 days`` / ``30–90 days`` /
   ``> 90 days``) → a horizontal stacked bar sorted by total desc.

3. **High-urgency programs** (:func:`build_high_urgency_programs`)
   The top-quartile-urgency programs with a prob-tiered recommended
   Action.  Urgency = ``volume × (1 − prob) × exp(−max(0, days)/90)``
   (big + unlikely + soon = urgent).  Replaces the old "Programs with
   Early Start Date" watchlist.

4. **Probability buckets** (:func:`build_probability_buckets`)
   Gross (unweighted) lbs per probability bucket (``Dead`` 0–20% /
   ``In-play`` 20–80% / ``Committed`` >80%), split by Portfolio Major →
   a stacked bar (one bar per bucket).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from data_sources.ro_comparison import (
    ANNUAL_OPP_LE,
    CUR_FISCAL_PROB_LE,
    YEAR1_PROB_LE,
)


# ── Input column identifiers (subset of RO_Comparison_Output.csv) ────────────
#
# Kept as module-private constants (mirrors ``ro_early_start_programs``) so a
# downstream rename in ``ro_comparison.OUTPUT_COLUMNS`` surfaces in one grep.
# ``ANNUAL_OPP_LE`` / ``CUR_FISCAL_PROB_LE`` / ``YEAR1_PROB_LE`` are imported
# canonically above (there ARE named constants for those three); the rest are
# bare literals in ``OUTPUT_COLUMNS`` with no exported constant.
_IN_ANNUAL_OPP: str    = ANNUAL_OPP_LE          # unweighted volume (lbs)
_IN_IN_YEAR: str       = CUR_FISCAL_PROB_LE     # FY27 probabilized (lbs)
_IN_FULL_YEAR: str     = YEAR1_PROB_LE          # FY28 probabilized (lbs)
_IN_PROB: str          = "LE Probability"       # 0-1 fraction
_IN_FIRST_SHIP: str    = "LE First Ship Date"
_IN_PORTFOLIO: str     = "Portfolio Major"
_IN_CUSTOMER: str      = "Customer"
_IN_ITEM_NUM: str      = "Item #"
_IN_DESCRIPTION: str   = "Description"


# ── Tunables (confirmed with the planner) ────────────────────────────────────

# Committed tile: share of the In-Year risk-adjusted pipeline at near-certain
# probability.  A separate, stricter bar than the >80% "Committed" prob bucket.
COMMITTED_PROB: float = 0.95

# Locked-in commitments (prob == 100%) are excluded from the action watchlist —
# there is no action to take on something already won.
LOCKED_PROB: float = 1.0

# Deadline-decay time constant (days).  urgency weight = exp(-max(0,days)/90):
# a program shipping today (or overdue) weighs 1.0; ~90 days out ≈ 0.37.
DEADLINE_DECAY_DAYS: float = 90.0

# High-urgency watchlist = the top quartile by urgency score.
HIGH_URGENCY_QUANTILE: float = 0.75

# First-ship-date urgency buckets (days from as-of to LE First Ship Date).
SHIP_BUCKET_NEAR: str = "< 30 days"
SHIP_BUCKET_MID: str  = "30–90 days"
SHIP_BUCKET_FAR: str  = "> 90 days"
SHIP_BUCKETS: tuple[str, ...] = (SHIP_BUCKET_NEAR, SHIP_BUCKET_MID, SHIP_BUCKET_FAR)

# Probability buckets for the gross-pipeline breakdown.
PROB_BUCKET_DEAD: str      = "Dead (0–20%)"
PROB_BUCKET_INPLAY: str    = "In-play (20–80%)"
PROB_BUCKET_COMMITTED: str = "Committed (>80%)"
PROB_BUCKETS: tuple[str, ...] = (
    PROB_BUCKET_DEAD, PROB_BUCKET_INPLAY, PROB_BUCKET_COMMITTED,
)

# Prob-tiered recommended actions (upper bound exclusive, lower inclusive).
ACTION_PROTECT: str  = "Protect"
ACTION_CHASE: str    = "Chase"
ACTION_QUALIFY: str  = "Qualify"
ACTION_KILL: str     = "Kill"

# Rationale copy laid out in the UI beside the watchlist.
ACTION_RATIONALE: tuple[tuple[str, str], ...] = (
    (ACTION_PROTECT, "≥ 80% — nearly won; protect the win, lock supply & timing."),
    (ACTION_CHASE,   "50–80% — winnable; chase to close the remaining gap."),
    (ACTION_QUALIFY, "20–50% — uncertain; qualify the opportunity before investing."),
    (ACTION_KILL,    "< 20% — long shot; kill or de-prioritize to free up focus."),
)

# Output column identifiers for the high-urgency watchlist.
COL_PROGRAM: str       = "Program"
COL_PORTFOLIO: str     = "Portfolio Major"
COL_ANNUAL_VOLUME: str = "Annual Volume (lbs)"
COL_FIRST_SHIP: str    = "First Ship Date"
COL_PROBABILITY: str   = "Probability"
COL_URGENCY: str       = "Urgency"
COL_ACTION: str        = "Action"

WATCHLIST_COLUMNS: tuple[str, ...] = (
    COL_PROGRAM, COL_PORTFOLIO, COL_ANNUAL_VOLUME, COL_FIRST_SHIP,
    COL_PROBABILITY, COL_URGENCY, COL_ACTION,
)


# ── Metric tiles ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PipelineMetrics:
    """The four RO-pipeline headline metrics, all in RAW LBS.

    ``committed_concentration`` is a 0-1 fraction (``committed_lbs`` ÷
    ``in_year_lbs``); ``None`` when the In-Year pipeline is zero.  The page
    scales the lbs values to millions for display.
    """

    gross_lbs: float
    full_year_lbs: float
    in_year_lbs: float
    committed_lbs: float
    committed_concentration: Optional[float]


def _num(comp_df: pd.DataFrame, col: str) -> pd.Series:
    """Numeric view of *col* (missing column → all-zero, NaN → 0)."""
    if col not in comp_df.columns:
        return pd.Series(0.0, index=comp_df.index)
    return pd.to_numeric(comp_df[col], errors="coerce").fillna(0.0)


def _prob(comp_df: pd.DataFrame) -> pd.Series:
    """Numeric LE Probability (0-1); missing column → all-NaN (unknown)."""
    if _IN_PROB not in comp_df.columns:
        return pd.Series(np.nan, index=comp_df.index)
    return pd.to_numeric(comp_df[_IN_PROB], errors="coerce")


def compute_pipeline_metrics(comp_df: pd.DataFrame) -> PipelineMetrics:
    """Compute the four headline pipeline metrics from the per-program frame.

    A sum over every (B2C) row → reconciles with the RO Summary Total B2C row.
    """
    if comp_df is None or comp_df.empty:
        return PipelineMetrics(0.0, 0.0, 0.0, 0.0, None)

    gross = float(_num(comp_df, _IN_ANNUAL_OPP).sum())
    full_year = float(_num(comp_df, _IN_FULL_YEAR).sum())
    in_year_series = _num(comp_df, _IN_IN_YEAR)
    in_year = float(in_year_series.sum())

    prob = _prob(comp_df)
    committed = float(in_year_series[prob >= COMMITTED_PROB].sum())
    concentration = (committed / in_year) if in_year else None

    return PipelineMetrics(
        gross_lbs=gross, full_year_lbs=full_year, in_year_lbs=in_year,
        committed_lbs=committed, committed_concentration=concentration,
    )


# ── Shared date/bucket helpers ───────────────────────────────────────────────

def _days_to_ship(comp_df: pd.DataFrame, as_of: date) -> pd.Series:
    """Whole days from *as_of* to each LE First Ship Date (NaN when unparseable)."""
    if _IN_FIRST_SHIP not in comp_df.columns:
        return pd.Series(np.nan, index=comp_df.index)
    ship = pd.to_datetime(comp_df[_IN_FIRST_SHIP], errors="coerce")
    delta = ship - pd.Timestamp(as_of)
    return delta.dt.days.astype("float64")


def _ship_bucket(days: pd.Series) -> pd.Series:
    """Map days-to-ship → urgency bucket label (NaN days → far, treated as slack)."""
    # Overdue / very soon rows (< 30, incl. negative) are the most urgent; NaN
    # (no date) is treated as "far" so undated rows don't inflate urgency.
    out = pd.Series(SHIP_BUCKET_FAR, index=days.index, dtype=object)
    out[days < 30] = SHIP_BUCKET_NEAR
    out[(days >= 30) & (days <= 90)] = SHIP_BUCKET_MID
    out[days.isna()] = SHIP_BUCKET_FAR
    return out


def _prob_bucket(prob: pd.Series) -> pd.Series:
    """Map LE Probability (0-1) → probability bucket label (NaN → Dead)."""
    out = pd.Series(PROB_BUCKET_INPLAY, index=prob.index, dtype=object)
    out[prob < 0.20] = PROB_BUCKET_DEAD
    out[prob > 0.80] = PROB_BUCKET_COMMITTED
    out[prob.isna()] = PROB_BUCKET_DEAD          # unknown prob ⇒ not committed
    return out


def _portfolio(comp_df: pd.DataFrame) -> pd.Series:
    """Portfolio Major as a cleaned string, blanks → "Unclassified"."""
    if _IN_PORTFOLIO not in comp_df.columns:
        return pd.Series("Unclassified", index=comp_df.index, dtype=object)
    s = comp_df[_IN_PORTFOLIO].astype(str).str.strip()
    return s.mask(s.isin(("", "nan", "None")), "Unclassified")


# ── Urgency ranking (Portfolio Major × ship bucket) ──────────────────────────

def build_urgency_ranking(comp_df: pd.DataFrame, *, as_of: date) -> pd.DataFrame:
    """FY27 in-year probabilized lbs per Portfolio Major × ship-date bucket.

    Returns a wide frame indexed by Portfolio Major (sorted by row total desc),
    one column per :data:`SHIP_BUCKETS` label (raw lbs).  Feeds a horizontal
    stacked bar.  Empty input → empty (but correctly-shaped) frame.
    """
    empty = pd.DataFrame(columns=list(SHIP_BUCKETS))
    empty.index.name = _IN_PORTFOLIO
    if comp_df is None or comp_df.empty:
        return empty

    work = pd.DataFrame({
        _IN_PORTFOLIO: _portfolio(comp_df),
        "_bucket": _ship_bucket(_days_to_ship(comp_df, as_of)),
        "_vol": _num(comp_df, _IN_IN_YEAR),
    })
    work = work[work["_vol"] != 0.0]
    if work.empty:
        return empty

    wide = (work.pivot_table(index=_IN_PORTFOLIO, columns="_bucket",
                             values="_vol", aggfunc="sum", fill_value=0.0)
                .reindex(columns=list(SHIP_BUCKETS), fill_value=0.0))
    wide = wide.loc[wide.sum(axis=1).sort_values(ascending=False).index]
    wide.columns.name = None
    return wide


# ── High-urgency program watchlist ───────────────────────────────────────────

def _action_for_prob(p: float) -> Optional[str]:
    """Prob-tiered recommended action; ``None`` for locked (>=100%) / NaN rows."""
    if p is None or pd.isna(p) or p >= LOCKED_PROB:
        return None
    if p >= 0.80:
        return ACTION_PROTECT
    if p >= 0.50:
        return ACTION_CHASE
    if p >= 0.20:
        return ACTION_QUALIFY
    return ACTION_KILL


def _compose_program(row: pd.Series) -> str:
    """Single-line ``Program`` identifier: ``Customer — Item# Description``."""
    def s(v: object) -> str:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        return str(v).strip()
    customer = s(row.get(_IN_CUSTOMER))
    item = f"{s(row.get(_IN_ITEM_NUM))} {s(row.get(_IN_DESCRIPTION))}".strip()
    parts = [p for p in (customer, item) if p]
    return " — ".join(parts) if parts else "?"


def build_high_urgency_programs(
    comp_df: pd.DataFrame, *, as_of: date,
    quantile: float = HIGH_URGENCY_QUANTILE,
) -> pd.DataFrame:
    """Top-quartile-urgency programs with a prob-tiered recommended Action.

    Urgency = ``annual_volume × (1 − prob) × exp(−max(0, days)/90)``.  Rows at
    ``LE Probability >= 1.0`` (locked-in wins) are excluded — no action to take.
    Rows are kept when their urgency is at / above the *quantile*-th percentile
    of all eligible rows, then sorted by urgency desc.  Columns:
    :data:`WATCHLIST_COLUMNS`.  Empty / no-eligible input → empty frame.
    """
    empty = pd.DataFrame(columns=list(WATCHLIST_COLUMNS))
    if comp_df is None or comp_df.empty:
        return empty

    prob = _prob(comp_df)
    volume = _num(comp_df, _IN_ANNUAL_OPP)
    days = _days_to_ship(comp_df, as_of)
    decay = np.exp(-np.clip(days.fillna(days.max() if days.notna().any() else 0.0),
                            a_min=0.0, a_max=None) / DEADLINE_DECAY_DAYS)
    urgency = volume * (1.0 - prob.fillna(0.0)) * decay

    work = pd.DataFrame({
        COL_PROGRAM: comp_df.apply(_compose_program, axis=1),
        COL_PORTFOLIO: _portfolio(comp_df),
        COL_ANNUAL_VOLUME: volume,
        COL_FIRST_SHIP: pd.to_datetime(
            comp_df.get(_IN_FIRST_SHIP), errors="coerce"),
        COL_PROBABILITY: prob,
        COL_URGENCY: urgency,
        "_prob_raw": prob,
    })
    # Eligible = unlocked (prob < 100%) with a real, positive urgency score.
    eligible = work[(work["_prob_raw"] < LOCKED_PROB) & (work[COL_URGENCY] > 0.0)]
    if eligible.empty:
        return empty

    threshold = eligible[COL_URGENCY].quantile(quantile)
    high = eligible[eligible[COL_URGENCY] >= threshold].copy()
    high[COL_ACTION] = high["_prob_raw"].map(_action_for_prob)
    high = (high.sort_values(COL_URGENCY, ascending=False)
                .drop(columns="_prob_raw")
                .reset_index(drop=True))
    return high[list(WATCHLIST_COLUMNS)]


# ── Probability-bucket breakdown (gross lbs) ─────────────────────────────────

def build_probability_buckets(comp_df: pd.DataFrame) -> pd.DataFrame:
    """Gross (unweighted) lbs per Portfolio Major × probability bucket.

    Returns a wide frame indexed by Portfolio Major, one column per
    :data:`PROB_BUCKETS` label (raw lbs).  Feeds a stacked bar with one bar per
    bucket, each stacked by Portfolio Major.  Empty input → empty frame.
    """
    empty = pd.DataFrame(columns=list(PROB_BUCKETS))
    empty.index.name = _IN_PORTFOLIO
    if comp_df is None or comp_df.empty:
        return empty

    work = pd.DataFrame({
        _IN_PORTFOLIO: _portfolio(comp_df),
        "_bucket": _prob_bucket(_prob(comp_df)),
        "_vol": _num(comp_df, _IN_ANNUAL_OPP),
    })
    work = work[work["_vol"] != 0.0]
    if work.empty:
        return empty

    wide = (work.pivot_table(index=_IN_PORTFOLIO, columns="_bucket",
                             values="_vol", aggfunc="sum", fill_value=0.0)
                .reindex(columns=list(PROB_BUCKETS), fill_value=0.0))
    wide = wide.loc[wide.sum(axis=1).sort_values(ascending=False).index]
    wide.columns.name = None
    return wide
