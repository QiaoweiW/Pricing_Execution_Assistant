"""Unit tests for the Risk-side 'Reflected in APS = No' rule.

Risk now carries its OWN reflected-in-APS gate
(``risk_requires_not_reflected_in_aps``), independent of the Opportunity gate
(``reflected_in_aps_only``).  The two answer different questions:

  * Opportunity gate — what reaches ``RO_Seed`` at all.
  * Risk gate       — what earns the Risk exemption from the Pipeline-Status
                      and Opportunity-probability gates.

Pure in-memory frames — no Fabric, no Streamlit.
"""
from __future__ import annotations

import pandas as pd

from data_sources.ro_risk import risk_mask
from data_sources.ro_rules_config import (
    REFLECTED_IN_APS_COLUMN,
    RoRulesConfig,
)
import data_sources.ro_risk_reconcile as rrr
import data_sources.ro_seed_pipeline as rsp


# ── Defaults ─────────────────────────────────────────────────────────────────

def test_rule_is_on_by_default():
    assert RoRulesConfig.default().risk_requires_not_reflected_in_aps is True


def test_rule_is_part_of_the_cache_signature():
    base = RoRulesConfig.default()
    flipped = base.with_updates(risk_requires_not_reflected_in_aps=False)
    assert base.signature() != flipped.signature()


# ── risk_reflected_col — the single place the gate is resolved ───────────────

_COLS = [REFLECTED_IN_APS_COLUMN, "Lbs./yr", "Probability"]


def test_column_is_returned_when_the_rule_is_on():
    cfg = RoRulesConfig.default()
    assert cfg.risk_reflected_col(_COLS) == REFLECTED_IN_APS_COLUMN


def test_none_when_the_planner_turns_the_rule_off():
    cfg = RoRulesConfig.default().with_updates(
        risk_requires_not_reflected_in_aps=False)
    assert cfg.risk_reflected_col(_COLS) is None


def test_none_when_the_frame_has_no_such_column():
    # RO_Comparison_Output and legacy seed files don't carry it — the gate must
    # fall through cleanly rather than raising.
    cfg = RoRulesConfig.default()
    assert cfg.risk_reflected_col(["Lbs./yr", "Probability"]) is None


def test_the_gate_is_independent_of_the_opportunity_toggle():
    # This is the whole point of the new rule: turning the Opportunity gate off
    # must NOT silently switch the Risk gate off too (the old coupled behaviour).
    cfg = RoRulesConfig.default().with_updates(reflected_in_aps_only=False)
    assert cfg.risk_reflected_col(_COLS) == REFLECTED_IN_APS_COLUMN


def test_a_caller_may_name_its_own_column():
    cfg = RoRulesConfig.default()
    assert cfg.risk_reflected_col(["APS?"], "APS?") == "APS?"


# ── End-to-end through risk_mask ─────────────────────────────────────────────

def _rows() -> pd.DataFrame:
    """Two identical probable losses; only the APS flag differs."""
    return pd.DataFrame({
        REFLECTED_IN_APS_COLUMN: ["no", "yes"],
        "Lbs./yr": [-1000, -1000],
        "Probability": [0.9, 0.9],
    })


def _mask(cfg: RoRulesConfig, df: pd.DataFrame) -> pd.Series:
    return risk_mask(
        df, volume_col="Lbs./yr", probability_col="Probability",
        reflected_col=cfg.risk_reflected_col(df.columns),
        min_probability=cfg.min_risk_probability,
        require_negative_volume=cfg.risk_requires_negative_volume,
    )


def test_reflected_loss_is_excluded_by_default():
    assert _mask(RoRulesConfig.default(), _rows()).tolist() == [True, False]


def test_reflected_loss_counts_once_the_rule_is_off():
    cfg = RoRulesConfig.default().with_updates(
        risk_requires_not_reflected_in_aps=False)
    assert _mask(cfg, _rows()).tolist() == [True, True]


def test_the_flag_is_matched_case_and_whitespace_insensitively():
    df = _rows()
    df[REFLECTED_IN_APS_COLUMN] = [" No ", "NO"]
    assert _mask(RoRulesConfig.default(), df).tolist() == [True, True]


def test_the_other_two_risk_conditions_still_apply():
    df = pd.DataFrame({
        REFLECTED_IN_APS_COLUMN: ["no", "no", "no"],
        "Lbs./yr": [-1000, 1000, -1000],   # gain in the middle
        "Probability": [0.9, 0.9, 0.1],    # unlikely at the end
    })
    assert _mask(RoRulesConfig.default(), df).tolist() == [True, False, False]


# ── The seed pipeline honours it ─────────────────────────────────────────────

def _tracker(reflected: str, status: str = "Declined") -> pd.DataFrame:
    """One probable loss whose Pipeline Status would normally exclude it.

    Only the Risk exemption can keep it, so the frame isolates the new rule.
    """
    return pd.DataFrame({
        "Format": ["F"], "Customer": ["C"], "Taxonomy": ["T"], "Brand": ["B"],
        "Item #": ["1"], "Item Desc": ["d"], "First Ship Date": ["1/1/2027"],
        "PC$/yr": [0], "Slotting": [0],
        REFLECTED_IN_APS_COLUMN: [reflected],
        "Pipeline Status": [status],
        "Lbs./yr": [-1000],
        "Probability": [0.9],
    })


def _seed(df: pd.DataFrame, cfg: RoRulesConfig) -> pd.DataFrame:
    # No Month column → the builder seeds from all combined rows, which keeps
    # this test on the gate logic rather than snapshot selection.
    return rsp._build_ro_seed(df, set(), rsp._Log(), config=cfg)


def test_declined_loss_keeps_its_risk_exemption_when_not_reflected():
    cfg = RoRulesConfig.default().with_updates(reflected_in_aps_only=False)
    assert len(_seed(_tracker("no"), cfg)) == 1


def test_declined_loss_loses_the_exemption_once_reflected_in_aps():
    # Reflected → not Risk → the Declined status gate now drops it.
    cfg = RoRulesConfig.default().with_updates(reflected_in_aps_only=False)
    assert len(_seed(_tracker("yes"), cfg)) == 0


def test_turning_the_rule_off_restores_the_exemption():
    cfg = RoRulesConfig.default().with_updates(
        reflected_in_aps_only=False, risk_requires_not_reflected_in_aps=False)
    assert len(_seed(_tracker("yes"), cfg)) == 1


# ── The reconciliation diagnostic mirrors the pipeline ───────────────────────

def test_reconcile_seed_mask_follows_the_same_rule():
    df = _rows().rename(columns={
        "Lbs./yr": rrr.SEED_VOLUME_COL, "Probability": rrr.SEED_PROBABILITY_COL,
    })
    assert rrr._mask_seed_risk(df, RoRulesConfig.default()).tolist() == [True, False]

    off = RoRulesConfig.default().with_updates(
        risk_requires_not_reflected_in_aps=False)
    assert rrr._mask_seed_risk(df, off).tolist() == [True, True]


def test_reconcile_summary_mask_is_unaffected():
    # RO_Comparison_Output carries no APS column, so the summary side must keep
    # classifying on volume + probability alone.
    df = pd.DataFrame({
        rrr.SUMMARY_VOLUME_COL: [-1000],
        rrr.SUMMARY_PROBABILITY_COL: [0.9],
    })
    assert rrr._mask_summary_risk(df, RoRulesConfig.default()).tolist() == [True]
