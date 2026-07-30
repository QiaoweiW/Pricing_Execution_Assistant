"""Canonical definition of an R&O *risk* line — the single source of truth.

A **risk** is a demand **loss the planner is committed to**.  A line qualifies
only when it clears ALL THREE conditions:

    1. Reflected in APS = "NO"        — not yet baked into the APS base plan,
                                         so it is still *incremental* R&O.
    2. Anticipated annual volume < 0  — a negative volume is a loss / de-list.
    3. Probability = 100%             — a committed loss, not a maybe.

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

# Probability (0–1 fraction) a loss must carry to count as a *committed* risk.
RISK_PROBABILITY: float = 1.0
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
) -> pd.Series:
    """Return a boolean Series (aligned to ``df.index``) of the R&O risk rows.

    A row is a risk when **volume < 0** AND **probability ≥ 100%** AND — when a
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
    """
    if volume_col not in df.columns or probability_col not in df.columns:
        return pd.Series(False, index=df.index)

    volume = _numeric(df[volume_col]).fillna(0.0)
    probability = _numeric(df[probability_col]).fillna(0.0)
    mask = (volume < 0) & (probability >= RISK_PROBABILITY)

    if reflected_col is not None and reflected_col in df.columns:
        reflected = (
            df[reflected_col].astype(str).str.strip().str.lower()
            == _REFLECTED_NOT_IN_APS
        )
        mask = mask & reflected
    return mask
