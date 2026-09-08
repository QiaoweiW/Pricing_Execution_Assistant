"""Canonical R&O inclusion rules — what counts as a risk, and what reaches RO_Seed.

A **risk** is a demand **loss the planner is likely to see**.  A line qualifies
only when it clears ALL THREE conditions:

    1. Reflected in APS = "NO"        — not yet baked into the APS base plan,
                                         so it is still *incremental* R&O.
    2. Anticipated annual volume < 0  — a negative volume is a loss / de-list.
    3. Probability ≥ 50%              — likely enough to plan around.

All three are planner defaults that the RO rules panel in the Demand Planner
Analytics view can override at runtime (see ``data_sources/ro_rules_config.py``
— ``risk_requires_not_reflected_in_aps``, ``risk_requires_negative_volume`` and
``min_risk_probability`` respectively).  Condition 1 is gated per call site by
``RoRulesConfig.risk_reflected_col``, which resolves both the planner's choice
and whether the frame even carries the column.

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

Two predicates live here:

  * :func:`risk_mask` — is this line a risk?  The rule above.
  * :func:`seed_scope_mask` — could this line reach ``RO_Seed`` at all?  Used
    by the upload validator so it never blocks a run over rows the pipeline
    was going to discard anyway (an item already reflected in APS, a declined
    programme, a zero-probability line).

Kept dependency-light (pandas only) so the RO-seed pipeline, the RO summary and
the upload pre-flight can all import it without a cycle.
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


def seed_scope_mask(
    df: pd.DataFrame,
    *,
    config,
    volume_col: str = "Lbs./yr",
    probability_col: str = "Probability",
    reflected_col: str = "Reflected in APS",
    status_col: str = "Pipeline Status",
) -> pd.Series:
    """Rows that *could* reach ``RO_Seed`` — the validation superset.

    ``ro_seed_pipeline._build_ro_seed`` answers "does this row belong in the
    seed?".  This answers a deliberately weaker question: "could this row
    matter?"  The difference is how each treats an **unreadable** gate cell.

    The pipeline is strict, because it has to choose: a blank
    ``Reflected in APS`` is not the literal ``"no"``, so the row is dropped; a
    blank ``Probability`` coerces to ``0.0`` and fails the threshold, so the
    row is dropped.  Either way the line silently disappears from the plan.

    A validator must not inherit that strictness, or it would skip checking
    precisely the rows whose gate cells are broken — the ones most likely to
    vanish by accident rather than by decision.  So a row here is out of scope
    only when a gate is **definitively** satisfied:

    * ``Reflected in APS`` holds a real value that is not ``"no"`` (i.e. Yes) —
      the planner has said this is already in the base plan;
    * ``Pipeline Status`` matches an exclude token (Declined / Closed);
    * ``Probability`` parses cleanly and sits at or below the Opportunity
      threshold.

    Blank or unparseable cells leave the row **in** scope.  Risk lines are
    always in scope: they bypass the status and probability gates in the
    pipeline too.

    The result is therefore a superset of what the pipeline keeps —
    ``pipeline_kept ⊆ seed_scope_mask`` — which is the property that makes it
    safe to use for validation.  ``tests/test_ro_seed_scope.py`` asserts that
    containment against the real pipeline so the two cannot drift apart.

    Parameters
    ----------
    config
        A :class:`data_sources.ro_rules_config.RoRulesConfig`.  Read for
        ``reflected_in_aps_only``, ``normalised_excludes()``,
        ``min_opp_probability`` and the Risk parameters, so the scope always
        reflects the planner's current rules.
    """
    if df.empty:
        return pd.Series([], dtype=bool)

    is_risk = risk_mask(
        df,
        volume_col=volume_col,
        probability_col=probability_col,
        reflected_col=config.risk_reflected_col(df.columns, reflected_col),
        min_probability=config.min_risk_probability,
        require_negative_volume=config.risk_requires_negative_volume,
    )

    out = pd.Series(False, index=df.index)

    # Reflected in APS = Yes — a decision, not an accident.  A blank stays in
    # scope: we cannot tell what the planner meant, so we check it.
    if config.reflected_in_aps_only and reflected_col in df.columns:
        text = df[reflected_col].astype(str).str.strip().str.lower()
        stated = text.ne("") & text.ne("nan")
        out = out | (stated & text.ne(_REFLECTED_NOT_IN_APS))

    # Declined / Closed — again a stated status.  Risk lines are exempt.
    excludes = config.normalised_excludes()
    if excludes and status_col in df.columns:
        status = df[status_col].astype(str).str.lower()
        dropped = pd.Series(False, index=df.index)
        for token in excludes:
            dropped = dropped | status.str.contains(token, na=False)
        out = out | (dropped & ~is_risk)

    # A probability that reads cleanly, is a possible probability, and does not
    # clear the bar — a business zero, so the row is genuinely out of play.
    #
    # A NEGATIVE value is a different animal: it parses, but no probability can
    # be below zero, so it is a broken cell rather than a decision.  Those stay
    # in scope and get reported, because the row vanishes from the plan by
    # accident.  Same reasoning as an unreadable cell.
    if probability_col in df.columns:
        probability = _numeric(df[probability_col])
        stated_low = (
            probability.notna()
            & (probability >= 0.0)
            & (probability <= float(config.min_opp_probability))
        )
        out = out | (stated_low & ~is_risk)

    return ~out
