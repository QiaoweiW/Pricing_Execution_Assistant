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
   FY27 in-year probabilized lbs per Portfolio Major × Supply Format, split
   into first-ship-date urgency buckets (``< 30 days`` / ``30–90 days`` /
   ``> 90 days``) → a horizontal stacked bar sorted by total desc.

3. **High-urgency programs** (:func:`build_high_urgency_programs` +
   :func:`apply_watchlist_filters`)
   The top-quartile-urgency programs (urgency = ``volume × (1 − prob) ×
   exp(−max(0, days)/90)``, big + unlikely + soon = urgent) with a blank
   Action for the planner to fill in, a Days-to-Ship column, and optional
   portfolio / volume / ship-window / probability filters.  Replaces the old
   "Programs with Early Start Date" watchlist.

4. **Pipeline build-up** (:func:`build_pipeline_buildup`)
   Per Portfolio Major × Supply Format, a stacked bar from FY27 probabilized
   (solid) up to the unweighted Gross Pipeline via a Year-effect and a Risk
   (probability-headroom) increment — so the reader sees the upside of lifting
   win probability.
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
_IN_SUPPLY_FORMAT: str = "Supply Format"


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

# Fiscal-year display labels — the SINGLE source of truth for the "FY__" text
# across the pipeline tiles / charts (page reads these).  At fiscal rollover,
# bump these two here instead of hunting literals through the UI.  (Kept in sync
# with the RO Summary's group labels, which carry the same convention.)
FY_CURRENT_LABEL: str = "FY27"    # current fiscal year (in-year, "Current Fiscal")
FY_NEXT_LABEL: str    = "FY28"    # next fiscal year (full-year, "Year1")

# First-ship-date urgency buckets (days from as-of to LE First Ship Date).
# "Overdue" (should already have shipped) is split out from "< 30 days" so a
# past-due program doesn't hide among merely-imminent ones.
SHIP_BUCKET_OVERDUE: str = "Overdue"
SHIP_BUCKET_NEAR: str = "< 30 days"
SHIP_BUCKET_MID: str  = "30–90 days"
SHIP_BUCKET_FAR: str  = "> 90 days"
SHIP_BUCKETS: tuple[str, ...] = (
    SHIP_BUCKET_OVERDUE, SHIP_BUCKET_NEAR, SHIP_BUCKET_MID, SHIP_BUCKET_FAR,
)

# Pipeline build-up segments (per Portfolio × Format): a solid in-year base plus
# the two increments that build it up to the unweighted Gross Pipeline —
#   In-Year Probabilized = Σ CUR_FISCAL_PROB_LE          (risk-adjusted in-year)
#   Year-effect          = Σ YEAR1_PROB_LE − Σ CUR_FISCAL (probabilized volume
#                                                          beyond the fiscal year)
#   Risk                 = Σ ANNUAL_OPP_LE − Σ YEAR1_PROB (probability headroom:
#                                                          upside if prob → 100%)
# The three sum to Gross, so the reader sees how much lifting probability x→y
# could recover.
SEG_FY27: str        = f"{FY_CURRENT_LABEL} Probabilized"
SEG_YEAR_EFFECT: str = "Year-effect"
SEG_RISK: str        = "Probability Headroom"
BUILDUP_SEGMENTS: tuple[str, ...] = (SEG_FY27, SEG_YEAR_EFFECT, SEG_RISK)

# Prob-tier action guidance surfaced in the UI (the planner fills Action in by
# hand; these are the reference bands, not an auto-assignment).
ACTION_PROTECT: str  = "Protect"
ACTION_CHASE: str    = "Chase"
ACTION_QUALIFY: str  = "Qualify"
ACTION_KILL: str     = "Kill"
ACTION_OPTIONS: tuple[str, ...] = (
    "", ACTION_PROTECT, ACTION_CHASE, ACTION_QUALIFY, ACTION_KILL,
)

# Output column identifiers for the high-urgency watchlist.  ``In-Year`` is the
# same FY27 probabilized quantity the urgency chart plots — carried here so the
# table and the chart beside it read on a common volume basis.
COL_PROGRAM: str       = "Program"
COL_PORTFOLIO: str     = "Portfolio Major"
COL_ANNUAL_VOLUME: str = "Annual Volume (lbs)"
COL_IN_YEAR: str       = "In-Year Probabilized (lbs)"
COL_FIRST_SHIP: str    = "First Ship Date"
COL_DAYS_TO_SHIP: str  = "Days-to-Ship"
COL_PROBABILITY: str   = "Probability"
COL_URGENCY: str       = "Urgency"
COL_ACTION: str        = "Action"

WATCHLIST_COLUMNS: tuple[str, ...] = (
    COL_PROGRAM, COL_PORTFOLIO, COL_ANNUAL_VOLUME, COL_IN_YEAR, COL_FIRST_SHIP,
    COL_DAYS_TO_SHIP, COL_PROBABILITY, COL_URGENCY, COL_ACTION,
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
    """Map days-to-ship → urgency bucket label.

    Past-due rows (days < 0) are their own ``Overdue`` bucket — the most urgent —
    so they don't hide among merely-imminent (< 30-day) programs.  NaN (no date)
    is treated as ``> 90 days`` so undated rows don't inflate urgency.
    """
    out = pd.Series(SHIP_BUCKET_FAR, index=days.index, dtype=object)
    out[(days >= 0) & (days < 30)] = SHIP_BUCKET_NEAR
    out[(days >= 30) & (days <= 90)] = SHIP_BUCKET_MID
    out[days < 0] = SHIP_BUCKET_OVERDUE
    out[days.isna()] = SHIP_BUCKET_FAR
    return out


def _clean_str(comp_df: pd.DataFrame, col: str) -> pd.Series:
    """Cleaned string view of *col*, blanks → "Unclassified"."""
    if col not in comp_df.columns:
        return pd.Series("Unclassified", index=comp_df.index, dtype=object)
    s = comp_df[col].astype(str).str.strip()
    return s.mask(s.isin(("", "nan", "None")), "Unclassified")


def _portfolio(comp_df: pd.DataFrame) -> pd.Series:
    """Portfolio Major as a cleaned string, blanks → "Unclassified"."""
    return _clean_str(comp_df, _IN_PORTFOLIO)


def _supply_format(comp_df: pd.DataFrame) -> pd.Series:
    """Supply Format as a cleaned string, blanks → "Unclassified"."""
    return _clean_str(comp_df, _IN_SUPPLY_FORMAT)


# Category label joining Portfolio Major and Supply Format — shared by the
# urgency and build-up charts so each portfolio splits by its format mix.
_PMAJOR_FORMAT_CAT: str = "Portfolio × Format"


def _pmajor_format(comp_df: pd.DataFrame) -> pd.Series:
    """Combined ``"{Portfolio Major} · {Supply Format}"`` category label."""
    return _portfolio(comp_df).str.cat(_supply_format(comp_df), sep=" · ")


# ── Urgency ranking (Portfolio Major × Supply Format, by ship bucket) ────────

def build_urgency_ranking(comp_df: pd.DataFrame, *, as_of: date) -> pd.DataFrame:
    """FY27 in-year probabilized lbs per Portfolio Major × Supply Format.

    Returns a wide frame indexed by a ``"{Portfolio} · {Supply Format}"`` label
    (sorted by row total desc), one column per :data:`SHIP_BUCKETS` label (raw
    lbs).  Feeds a horizontal stacked bar.  Empty input → empty frame.
    """
    empty = pd.DataFrame(columns=list(SHIP_BUCKETS))
    empty.index.name = _PMAJOR_FORMAT_CAT
    if comp_df is None or comp_df.empty:
        return empty

    work = pd.DataFrame({
        _PMAJOR_FORMAT_CAT: _pmajor_format(comp_df),
        "_bucket": _ship_bucket(_days_to_ship(comp_df, as_of)),
        "_vol": _num(comp_df, _IN_IN_YEAR),
    })
    work = work[work["_vol"] != 0.0]
    if work.empty:
        return empty

    wide = (work.pivot_table(index=_PMAJOR_FORMAT_CAT, columns="_bucket",
                             values="_vol", aggfunc="sum", fill_value=0.0)
                .reindex(columns=list(SHIP_BUCKETS), fill_value=0.0))
    wide = wide.loc[wide.sum(axis=1).sort_values(ascending=False).index]
    wide.columns.name = None
    return wide


# ── High-urgency program watchlist ───────────────────────────────────────────

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
    """Top-quartile-urgency programs, with a blank Action for the planner to fill.

    Urgency = ``annual_volume × (1 − prob) × exp(−max(0, days)/90)``.  Rows at
    ``LE Probability >= 1.0`` (locked-in wins) are excluded — no action to take.
    Rows are kept when their urgency is at / above the *quantile*-th percentile
    of all eligible rows, then sorted by urgency desc.  ``Action`` is left blank
    (the planner assigns it in the editor); ``Days-to-Ship`` is whole days from
    *as_of*.  Columns: :data:`WATCHLIST_COLUMNS`.  Empty input → empty frame.
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
        COL_IN_YEAR: _num(comp_df, _IN_IN_YEAR),
        COL_FIRST_SHIP: pd.to_datetime(
            comp_df.get(_IN_FIRST_SHIP), errors="coerce"),
        COL_DAYS_TO_SHIP: days,
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
    high[COL_DAYS_TO_SHIP] = high[COL_DAYS_TO_SHIP].astype("Int64")
    high[COL_ACTION] = ""                       # blank — planner fills it in
    high = (high.sort_values(COL_URGENCY, ascending=False)
                .drop(columns="_prob_raw")
                .reset_index(drop=True))
    return high[list(WATCHLIST_COLUMNS)]


def apply_watchlist_filters(
    df: pd.DataFrame, *,
    portfolios: Optional[list[str]] = None,
    min_volume: Optional[float] = None,
    ship_buckets: Optional[list[str]] = None,
    prob_range: Optional[tuple[float, float]] = None,
) -> pd.DataFrame:
    """Narrow a built watchlist frame by the planner's filter selections.

    * ``portfolios``   — keep rows whose Portfolio Major is in the list (empty /
      ``None`` = all).
    * ``min_volume``   — keep rows with Annual Volume ≥ this many lbs.
    * ``ship_buckets`` — keep rows whose Days-to-Ship falls in one of the given
      :data:`SHIP_BUCKETS` labels.
    * ``prob_range``   — ``(lo, hi)`` inclusive probability band (0-1 fractions).
    """
    if df is None or df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    if portfolios:
        mask &= df[COL_PORTFOLIO].isin(portfolios)
    if min_volume is not None:
        mask &= pd.to_numeric(df[COL_ANNUAL_VOLUME], errors="coerce").fillna(0.0) >= min_volume
    if ship_buckets:
        buckets = _ship_bucket(pd.to_numeric(df[COL_DAYS_TO_SHIP], errors="coerce"))
        mask &= buckets.isin(ship_buckets)
    if prob_range is not None:
        lo, hi = prob_range
        p = pd.to_numeric(df[COL_PROBABILITY], errors="coerce")
        mask &= p.between(lo, hi) | p.isna()
    return df.loc[mask].reset_index(drop=True)


# ── Pipeline build-up (per Portfolio Major: FY27 → Gross) ────────────────────

def build_pipeline_buildup(comp_df: pd.DataFrame) -> pd.DataFrame:
    """Build-up of FY27 probabilized lbs up to Gross, per Portfolio × Format.

    Returns a wide frame indexed by a ``"{Portfolio} · {Supply Format}"`` label
    (sorted by Gross desc), with one column per :data:`BUILDUP_SEGMENTS` label
    (raw lbs).  The three segments sum to the unweighted Gross Pipeline;
    increments are clamped at 0 so a stray row where probabilized exceeds
    unweighted can't produce a negative slice.  Empty input → empty frame.
    """
    empty = pd.DataFrame(columns=list(BUILDUP_SEGMENTS))
    empty.index.name = _PMAJOR_FORMAT_CAT
    if comp_df is None or comp_df.empty:
        return empty

    work = pd.DataFrame({
        _PMAJOR_FORMAT_CAT: _pmajor_format(comp_df),
        "_fy27": _num(comp_df, _IN_IN_YEAR),
        "_year1": _num(comp_df, _IN_FULL_YEAR),
        "_gross": _num(comp_df, _IN_ANNUAL_OPP),
    })
    agg = work.groupby(_PMAJOR_FORMAT_CAT).sum()
    agg = agg[agg["_gross"] != 0.0]
    if agg.empty:
        return empty

    out = pd.DataFrame({
        SEG_FY27: agg["_fy27"].clip(lower=0.0),
        SEG_YEAR_EFFECT: (agg["_year1"] - agg["_fy27"]).clip(lower=0.0),
        SEG_RISK: (agg["_gross"] - agg["_year1"]).clip(lower=0.0),
    })
    out = out.loc[agg["_gross"].sort_values(ascending=False).index]
    out.index.name = _PMAJOR_FORMAT_CAT
    return out
