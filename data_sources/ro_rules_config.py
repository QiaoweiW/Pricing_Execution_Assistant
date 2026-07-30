"""User-tunable business rules for the RO Seed → Comparison → Summary pipeline.

The planner-visible defaults below are the *canonical* rules — the RO seed
pipeline uses them out of the box, and the Streamlit rules panel writes any
overrides into ``session_state[SESSION_KEY]`` at runtime.  Two consumers:

1. **View-time filter** — the RO Summary Report renders its ``Delta Breakdown |
   Risk`` column by re-classifying rows off ``RO_Comparison_Output.csv``.
   Passing the current :class:`RoRulesConfig` into
   :func:`data_sources.ro_risk.risk_mask` lets the user retune the Risk
   threshold and see the table update without touching Fabric.
2. **Regeneration** — the "Regenerate RO_Seed with current rules" button in
   the RO section runs :func:`data_sources.ro_seed_pipeline
   .run_distribution_tracker_pipeline` with the same config.  The Opportunity
   gate (Pipeline Status excludes + probability threshold + Reflected-in-APS)
   sits *upstream* of the persisted CSVs, so changing it requires rewriting
   ``RO_Seed.csv`` / ``RO_History_Tracker.csv``.

Kept dependency-light (stdlib + a tiny dataclass) so both the pure pipeline
module and the Streamlit view can import it without cycles.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional


# ── Defaults (aligned with the planner-approved rules, Jul 2026) ────────────

# Opportunity inclusion — a Distribution Tracker row lands in RO_Seed only when
# it clears ALL of these gates (a Risk line bypasses the Pipeline-Status +
# probability gates, since a committed loss is still material even if declined).
DEFAULT_REFLECTED_IN_APS_ONLY: bool = True
DEFAULT_PIPELINE_STATUS_EXCLUDES: tuple[str, ...] = ("Declined", "Closed")
# Opportunity probability threshold — a row must clear ``probability > this``.
# 0.0 → the historical ">0" rule.
DEFAULT_MIN_OPP_PROBABILITY: float = 0.0

# Risk carve-out — Delta Breakdown | Risk column and the RO_Seed risk exemption.
DEFAULT_MIN_RISK_PROBABILITY: float = 0.5   # 50%
DEFAULT_RISK_REQUIRES_NEGATIVE_VOLUME: bool = True


@dataclass(frozen=True)
class RoRulesConfig:
    """Single source of truth for the tunable RO rules.

    All fields carry planner defaults so :meth:`default` is the canonical
    starting point and every field is independently overridable — the rules
    panel writes only the widgets the user touched and inherits the rest.

    * ``reflected_in_aps_only`` — restrict Opportunity + Risk to rows tagged
      "Reflected in APS = no".  Turn off to include APS-reflected wins/losses.
    * ``pipeline_status_excludes`` — case-insensitive substrings dropped from
      the Opportunity inclusion.  A row is dropped when its Pipeline Status
      contains ANY listed token (Risk lines bypass this gate).
    * ``min_opp_probability`` — Opportunity clears when probability strictly
      exceeds this.  ``0.0`` == "any non-zero" (the historical rule).
    * ``min_risk_probability`` — Risk mask threshold; probability must be
      **≥** this fraction to count.
    * ``risk_requires_negative_volume`` — when ``True`` (default) Risk requires
      a negative Anticipated Annual Vol; disable to widen Risk to any probable
      line.
    """

    reflected_in_aps_only: bool = DEFAULT_REFLECTED_IN_APS_ONLY
    pipeline_status_excludes: tuple[str, ...] = field(
        default_factory=lambda: tuple(DEFAULT_PIPELINE_STATUS_EXCLUDES),
    )
    min_opp_probability: float = DEFAULT_MIN_OPP_PROBABILITY
    min_risk_probability: float = DEFAULT_MIN_RISK_PROBABILITY
    risk_requires_negative_volume: bool = DEFAULT_RISK_REQUIRES_NEGATIVE_VOLUME

    # ── Convenience constructors + views ────────────────────────────────

    @classmethod
    def default(cls) -> "RoRulesConfig":
        """Return the planner-approved default rules (no user overrides)."""
        return cls()

    def with_updates(self, **updates) -> "RoRulesConfig":
        """Return a copy with ``updates`` applied (frozen dataclass helper)."""
        return replace(self, **updates)

    # ── Rule application helpers (shared by pipeline + view) ─────────────

    def normalised_excludes(self) -> tuple[str, ...]:
        """Return case-normalised, non-empty Pipeline-Status exclude tokens."""
        return tuple(
            t.strip().lower() for t in self.pipeline_status_excludes if t and t.strip()
        )

    def signature(self) -> tuple:
        """Return a hashable snapshot of the rules for cache keys / diffing."""
        return (
            self.reflected_in_aps_only,
            tuple(self.pipeline_status_excludes),
            float(self.min_opp_probability),
            float(self.min_risk_probability),
            bool(self.risk_requires_negative_volume),
        )


# ── Streamlit session-state key (shared by the panel + every consumer) ──────

# ``st.session_state[SESSION_KEY]`` holds the current :class:`RoRulesConfig`.
# The panel writes here; readers pull the config or fall back to defaults.
SESSION_KEY: str = "_ro_rules_config"


def config_from_session(session_state) -> RoRulesConfig:
    """Read the current rules from ``st.session_state``, defaulting on miss.

    Kept out of the Streamlit view so pure-Python callers (the seed pipeline,
    the RO summary builder, the tests) can hand in any mapping-like object —
    including a plain ``dict`` — without importing Streamlit.
    """
    if session_state is None:
        return RoRulesConfig.default()
    try:
        current = session_state.get(SESSION_KEY)
    except AttributeError:  # bare dict-like without .get
        current = session_state[SESSION_KEY] if SESSION_KEY in session_state else None
    if isinstance(current, RoRulesConfig):
        return current
    return RoRulesConfig.default()


def pipeline_status_excluded(value: object, config: Optional[RoRulesConfig]) -> bool:
    """Return True when ``value`` (a Pipeline Status cell) is excluded.

    Case-insensitive substring match against the config's exclude tokens.
    A ``None`` config is treated as the default rules — matches the seed
    pipeline's contract of falling back to canonical rules when no config
    is threaded through.
    """
    cfg = config or RoRulesConfig.default()
    tokens = cfg.normalised_excludes()
    if not tokens:
        return False
    text = str(value or "").strip().lower()
    return any(tok in text for tok in tokens)
