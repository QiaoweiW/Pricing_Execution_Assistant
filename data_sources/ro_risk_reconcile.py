"""Reconcile the risks in ``RO_Seed.csv`` with the risks the RO Summary shows.

Motivation
----------
``qry_mgmt_plan_full.csv`` / ``qry_total_item_level_demand.csv`` are built from
``RO_Seed.csv`` (see :mod:`data_sources.demand_plan_pipeline`).  The RO Summary
Report is built from ``RO_Comparison_Output.csv`` (see
:mod:`data_sources.ro_summary_report`).  Both are supposed to reflect the same
underlying R&O risk set, but the two files travel through independent pipelines
and can drift for legitimate reasons:

* The RO Comparison editor can save an edited ``RO_Comparison_Output.csv``
  without touching ``Distribution_Tracker_History.csv`` (planner reclassified a
  line directly in the editor).
* The Distribution Tracker upload rebuilds ``RO_Seed.csv`` for the *current*
  snapshot month only; a risk carried over in ``RO_History_Tracker.csv`` from a
  prior snapshot can still show in the summary while being absent from the
  current seed.
* The user tunes :class:`data_sources.ro_rules_config.RoRulesConfig` at runtime;
  the seed on disk was built under an earlier setting.

When any of these drift, the mgmt-plan files silently under- or over-report the
demand plan.  This module is the diagnostic side of the fix: pure, dependency-
light functions that take the two frames plus the current rules config and
report exactly which risks are in one file but not the other.

Design
------
* **No I/O, no Streamlit.**  Callers hand in already-loaded DataFrames.  Keeps
  the module trivially unit-testable and reusable from a CLI, a notebook, or
  the Streamlit page.
* **One rule primitive.**  We call :func:`data_sources.ro_risk.risk_mask` on
  both frames with the appropriate column names, so the definition of *risk*
  is identical to what :mod:`data_sources.ro_seed_pipeline` and
  :mod:`data_sources.ro_summary_report` already apply.  There is no second
  copy of the rule to keep in sync.
* **Business key = ``(Format, Customer, Taxonomy, Brand, Item #)``.**  Same
  five-column key ``ro_seed_pipeline`` uses for RO Key assignment; guarantees a
  divergence report joins on the same identity the pipeline itself uses.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import pandas as pd

from .ro_risk import risk_mask
from .ro_rules_config import RoRulesConfig


# ── Column contracts ─────────────────────────────────────────────────────────
#
# Kept as module constants (rather than free strings scattered through the
# code) so a rename in either upstream schema is a one-line fix.  Names mirror
# what ``ro_seed_pipeline._build_ro_seed`` reads on the seed side and
# ``ro_summary_report._compute_leaf_values`` reads on the summary side.

# Business key — must match ``ro_seed_pipeline._MATCH_COLS``.
BUSINESS_KEY_COLS: tuple[str, ...] = (
    "Format", "Customer", "Taxonomy", "Brand", "Item #",
)

# RO_Seed side — raw Distribution-Tracker-lineage columns.
SEED_VOLUME_COL: str = "Lbs./yr"
SEED_PROBABILITY_COL: str = "Probability"
SEED_REFLECTED_COL: str = "Reflected in APS"

# RO_Comparison_Output side — the LE (Latest Estimate) columns are what the
# Summary Report classifies as risk.
SUMMARY_VOLUME_COL: str = "LE Annual Opportunity (lbs)"
SUMMARY_PROBABILITY_COL: str = "LE Probability"

# Extra columns we surface in the divergence detail frames so the planner can
# scan a row and understand it without opening the source file.  Only kept
# when present — a missing column is silently skipped.
_SEED_DETAIL_COLS: tuple[str, ...] = (
    SEED_VOLUME_COL, SEED_PROBABILITY_COL, "Item Desc", "Month",
)
_SUMMARY_DETAIL_COLS: tuple[str, ...] = (
    SUMMARY_VOLUME_COL, SUMMARY_PROBABILITY_COL, "Description", "Driver",
    "Portfolio Major", "Supply Format",
)


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RiskReconciliationResult:
    """Outcome of :func:`reconcile_ro_seed_vs_summary`.

    Attributes
    ----------
    missing_from_seed
        Rows the RO Summary classifies as risk but that have no matching
        business key in the RO_Seed risk set — the actionable failure mode
        (``qry_mgmt_plan_full`` won't reflect them until the seed is rebuilt
        with these rows in Distribution_Tracker_History).
    missing_from_summary
        Rows RO_Seed considers a risk but the RO Summary does not — usually a
        stale ``RO_Comparison_Output.csv`` (regenerate it and this shrinks to
        zero) or a rules-config change that hasn't been applied downstream.
    matched
        Rows present as risk in BOTH files.  Handed back for a "healthy row
        count" caption in the UI.
    seed_risk_count, summary_risk_count
        Total risk-mask hits on each side (may differ from the detail-frame
        row counts when the same business key appears more than once in the
        source file — the detail frames deduplicate on business key).
    """
    missing_from_seed: pd.DataFrame
    missing_from_summary: pd.DataFrame
    matched: pd.DataFrame
    seed_risk_count: int
    summary_risk_count: int

    @property
    def is_aligned(self) -> bool:
        """True when neither divergence side has any rows."""
        return self.missing_from_seed.empty and self.missing_from_summary.empty

    @property
    def total_divergence(self) -> int:
        """Total number of rows that appear on exactly one side."""
        return int(len(self.missing_from_seed) + len(self.missing_from_summary))


# ── Internal helpers ─────────────────────────────────────────────────────────

def _normalise_cell(value) -> str:
    """Canonicalise a business-key cell for cross-file comparison.

    Strips whitespace, coerces NaN/None to ``""``, and drops the trailing
    ``.0`` from floats-that-are-integers (``"380574.0"`` → ``"380574"``) so
    an ``Int64``-typed Item # on one side matches a string-typed Item # on
    the other.  Mirrors ``ro_seed_pipeline._norm_item``'s intent.
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].lstrip("-").isdigit():
        text = text[:-2]
    return text


def _key_series(df: pd.DataFrame) -> pd.Series:
    """Return a Series of ``(f, c, t, b, i)`` business-key tuples for *df*.

    Missing columns are treated as blank so a frame that predates one of the
    key columns still hashes cleanly (avoids raising during a schema drift).
    """
    if df.empty:
        return pd.Series([], dtype=object)
    cols: list[pd.Series] = []
    for col in BUSINESS_KEY_COLS:
        series = df[col] if col in df.columns else pd.Series("", index=df.index)
        cols.append(series.map(_normalise_cell))
    # Materialise the zip into a list so pd.Series constructs cleanly on every
    # pandas version (some releases pull once from generators, then hand back
    # an empty Series when reset).  Object dtype keeps the tuples intact.
    return pd.Series(list(zip(*cols)), index=df.index, dtype=object)


def _mask_seed_risk(df: pd.DataFrame, cfg: RoRulesConfig) -> pd.Series:
    """Risk mask for the RO_Seed side.

    Uses the raw Distribution-Tracker columns.  Reflected-in-APS is enforced
    only when the config asks for it AND the column is present — a legacy seed
    file without the column falls through cleanly instead of raising.
    """
    if df.empty:
        return pd.Series([], dtype=bool)
    reflected_col = (
        SEED_REFLECTED_COL
        if cfg.reflected_in_aps_only and SEED_REFLECTED_COL in df.columns
        else None
    )
    return risk_mask(
        df,
        volume_col=SEED_VOLUME_COL,
        probability_col=SEED_PROBABILITY_COL,
        reflected_col=reflected_col,
        min_probability=cfg.min_risk_probability,
        require_negative_volume=cfg.risk_requires_negative_volume,
    )


def _mask_summary_risk(df: pd.DataFrame, cfg: RoRulesConfig) -> pd.Series:
    """Risk mask for the RO Summary (``RO_Comparison_Output.csv``) side.

    Matches ``ro_summary_report._compute_leaf_values``: no Reflected filter
    (already applied upstream when RO_Seed was built), classifies on the LE
    columns.
    """
    if df.empty:
        return pd.Series([], dtype=bool)
    return risk_mask(
        df,
        volume_col=SUMMARY_VOLUME_COL,
        probability_col=SUMMARY_PROBABILITY_COL,
        reflected_col=None,
        min_probability=cfg.min_risk_probability,
        require_negative_volume=cfg.risk_requires_negative_volume,
    )


def _detail_frame(
    df: pd.DataFrame,
    mask: pd.Series,
    extra_cols: Iterable[str],
) -> pd.DataFrame:
    """Return a display-ready, deduped detail frame for a risk slice.

    * Selects rows where *mask* is True.
    * Normalises the business-key columns so downstream set-arithmetic joins
      cleanly on identity, not on dtype/whitespace incidentals.
    * Deduplicates on the business key (multiple source rows per RO — e.g.
      the same Item # split across ship-date buckets — collapse to one entry
      in the report).
    * Keeps only the business-key columns plus whichever *extra_cols* the
      caller asked for and the source frame actually has.
    """
    empty_cols = list(BUSINESS_KEY_COLS) + [c for c in extra_cols]
    if df.empty or not bool(mask.any()):
        return pd.DataFrame(columns=empty_cols)

    slice_ = df.loc[mask].copy()
    for col in BUSINESS_KEY_COLS:
        if col not in slice_.columns:
            slice_[col] = ""
        slice_[col] = slice_[col].map(_normalise_cell)

    keep_cols = list(BUSINESS_KEY_COLS) + [c for c in extra_cols if c in slice_.columns]
    slice_ = slice_.loc[:, keep_cols]
    # Deduplicate on the business key — the report is per-RO, not per-row.
    return slice_.drop_duplicates(subset=list(BUSINESS_KEY_COLS)).reset_index(drop=True)


# ── Public entry point ───────────────────────────────────────────────────────

def reconcile_ro_seed_vs_summary(
    seed_df: Optional[pd.DataFrame],
    comparison_output_df: Optional[pd.DataFrame],
    *,
    config: Optional[RoRulesConfig] = None,
) -> RiskReconciliationResult:
    """Compare risk classifications between RO_Seed and RO_Comparison_Output.

    Parameters
    ----------
    seed_df
        Content of ``RO_Seed.csv`` (as read by
        :func:`data_sources.ro_seed_pipeline.fetch_ro_seed_raw_bytes` → pandas).
        ``None`` or an empty frame is treated as "no risks on the seed side".
    comparison_output_df
        Content of ``RO_Comparison_Output.csv`` (as returned by
        :func:`data_sources.ro_comparison.fetch_ro_comparison_output_df`).
        ``None`` / empty is treated as "no risks on the summary side".
    config
        Optional :class:`RoRulesConfig`.  Defaults to
        :meth:`RoRulesConfig.default`.  Threaded through so the reconciliation
        uses the SAME threshold the user is looking at in the RO Summary
        rules panel — a mismatch there was itself a common divergence cause.

    Returns
    -------
    RiskReconciliationResult
        Divergence detail + counts.  Never raises — a missing column on either
        side degrades to "no risks classified on that side", surfacing as
        divergence rather than an exception (the UI can then guide the user
        to fix the source file).
    """
    cfg = config or RoRulesConfig.default()
    seed_df = seed_df if seed_df is not None else pd.DataFrame()
    summary_df = comparison_output_df if comparison_output_df is not None else pd.DataFrame()

    seed_mask = _mask_seed_risk(seed_df, cfg)
    summary_mask = _mask_summary_risk(summary_df, cfg)

    seed_detail = _detail_frame(seed_df, seed_mask, _SEED_DETAIL_COLS)
    summary_detail = _detail_frame(summary_df, summary_mask, _SUMMARY_DETAIL_COLS)

    seed_keys = set(_key_series(seed_detail))
    summary_keys = set(_key_series(summary_detail))

    def _filter_by_keys(detail: pd.DataFrame, keep: set) -> pd.DataFrame:
        """Return *detail* rows whose business key is in *keep*."""
        if detail.empty:
            return detail
        keys = _key_series(detail)
        return detail.loc[keys.isin(keep)].reset_index(drop=True)

    missing_from_seed = _filter_by_keys(summary_detail, summary_keys - seed_keys)
    missing_from_summary = _filter_by_keys(seed_detail, seed_keys - summary_keys)
    matched = _filter_by_keys(seed_detail, seed_keys & summary_keys)

    return RiskReconciliationResult(
        missing_from_seed=missing_from_seed,
        missing_from_summary=missing_from_summary,
        matched=matched,
        seed_risk_count=int(seed_mask.sum()),
        summary_risk_count=int(summary_mask.sum()),
    )


__all__ = [
    "BUSINESS_KEY_COLS",
    "SEED_VOLUME_COL",
    "SEED_PROBABILITY_COL",
    "SEED_REFLECTED_COL",
    "SUMMARY_VOLUME_COL",
    "SUMMARY_PROBABILITY_COL",
    "RiskReconciliationResult",
    "reconcile_ro_seed_vs_summary",
]
