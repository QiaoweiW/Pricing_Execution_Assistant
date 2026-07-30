"""Canonical definition of an R&O *risk* line — the single source of truth.

A **risk** is a demand **loss the planner is likely to see**.  A line qualifies
only when it clears ALL THREE conditions:

    1. Reflected in APS = "NO"        — not yet baked into the APS base plan,
                                         so it is still *incremental* R&O.
    2. Anticipated annual volume < 0  — a negative volume is a loss / de-list.
    3. Probability ≥ 50%              — likely enough to plan around.

The 50% threshold is the planner default; the RO rules panel in the Demand
Planner Analytics view lets it be overridden at runtime (see
``data_sources/ro_rules_config.py``).

The identical rule is applied everywhere R&O is captured, so the stages stay
reconciled:

  * :mod:`data_sources.ro_seed_pipeline` — a risk bypasses the pipeline-status
    gate so it still lands in ``RO_Seed`` (and therefore the RO history, the
    demand plan, and the mgmt-plan history tracker);
  * :mod:`data_sources.ro_summary_report` — the RO Summary's "Risk" delta column;
  * the demand-plan comparison reports read those outputs, so they inherit it.

Conventions
-----------
* Probability is a **0–1 fraction** (``1.0`` == 100%) at every stage.
* "Reflected in APS" is only a column *upstream* (Distribution Tracker /
  RO_Seed).  By the time R&O reaches ``RO_Comparison_Output`` the non-reflected
  filter has already been applied, so callers there pass ``reflected_col=None``
  and condition 1 is treated as already satisfied.

Kept dependency-light (pandas only) so both the RO-seed pipeline and the RO
summary can import it without a cycle.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

# Probability (0–1 fraction) a loss must clear to count as a *likely* risk.
# 0.5 = 50%.  The Demand Planner Analytics rules panel can raise/lower this
# per-session by passing an explicit ``min_probability`` to :func:`risk_mask`.
RISK_PROBABILITY: float = 0.5
# The "Reflected in APS" value that marks a line as still incremental R&O.
_REFLECTED_NOT_IN_APS: str = "no"


def _numeric(series: pd.Series) -> pd.Series:
    """Coerce a possibly comma-formatted column to float (bad values → NaN)."""
    return pd.to_numeric(
        series.astype(str).str.replace(",", "", regex=False), errors="coerce",
    )


def risk_mask(
    df: pd.DataFrame,
    *,
    volume_col: str,
    probability_col: str,
    reflected_col: Optional[str] = None,
    min_probability: Optional[float] = None,
    require_negative_volume: bool = True,
) -> pd.Series:
    """Return a boolean Series (aligned to ``df.index``) of the R&O risk rows.

    A row is a risk when **volume < 0** (unless ``require_negative_volume`` is
    disabled) AND **probability ≥ min_probability** AND — when a
    ``reflected_col`` is supplied — **Reflected in APS == "no"**.  A missing
    volume or probability column yields an all-``False`` mask (a risk we can't
    confirm is not a risk), so partial or synthetic frames never raise.

    Parameters
    ----------
    volume_col
        Anticipated annual volume column (negative = loss).
    probability_col
        Probability column, as a 0–1 fraction (``1.0`` == 100%).
    reflected_col
        "Reflected in APS" column.  ``None`` when it has already been filtered
        upstream (e.g. in ``RO_Comparison_Output``) — condition 1 is then taken
        as satisfied.
    min_probability
        Threshold that probability must clear.  ``None`` → :data:`RISK_PROBABILITY`
        (the planner default, 0.5 == 50%).  The Demand Planner Analytics rules
        panel passes an explicit value here to override at runtime without
        mutating the module-level default.
    require_negative_volume
        When ``True`` (default) the volume-is-negative gate applies.  Kept
        exposed so a user rule can widen the definition of Risk to any
        probable line, not only losses.
    """
    if volume_col not in df.columns or probability_col not in df.columns:
        return pd.Series(False, index=df.index)

    threshold = RISK_PROBABILITY if min_probability is None else float(min_probability)
    volume = _numeric(df[volume_col]).fillna(0.0)
    probability = _numeric(df[probability_col]).fillna(0.0)
    mask = probability >= threshold
    if require_negative_volume:
        mask = mask & (volume < 0)

    if reflected_col is not None and reflected_col in df.columns:
        reflected = (
            df[reflected_col].astype(str).str.strip().str.lower()
            == _REFLECTED_NOT_IN_APS
        )
        mask = mask & reflected
    return mask
