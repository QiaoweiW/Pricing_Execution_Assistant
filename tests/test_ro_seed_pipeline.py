"""Unit tests for duplicate-row removal in the RO seed pipeline.

Covers the fix for duplicate rows leaking into ``Distribution_Tracker_History.csv``
and ``RO_History_Tracker.csv``.  Pure in-memory frames — no Fabric, no Streamlit.
"""
from __future__ import annotations

import pandas as pd

import data_sources.ro_seed_pipeline as rsp


def _log() -> rsp._Log:
    return rsp._Log()


# ── _dedupe_identical_rows ───────────────────────────────────────────────────

def test_dedupe_strips_whitespace_then_drops_identicals():
    df = pd.DataFrame({
        "Customer": ["URM", "URM ", " URM"],   # same after strip
        "Item #": ["1", "1", "1"],
    })
    out, removed = rsp._dedupe_identical_rows(df)
    assert removed == 2
    assert len(out) == 1
    assert out["Customer"].iloc[0] == "URM"     # trimmed value is written


def test_dedupe_keeps_genuinely_distinct_rows():
    df = pd.DataFrame({"Customer": ["A", "B"], "Item #": ["1", "2"]})
    out, removed = rsp._dedupe_identical_rows(df)
    assert removed == 0 and len(out) == 2


# ── Distribution_Tracker_History: dedupe AFTER cleanup ───────────────────────

def _dist_row(**over) -> dict:
    base = {
        "Month": "07/01/2026", "Format": "F", "Customer": "URM",
        "Taxonomy": "T", "Brand": "B", "Item #": "380574", "Item Desc": "DG X",
        "First Ship Date": "04/01/2026", "Lbs./yr": "100", "PC$/yr": "10",
        "Slotting": "0", "Probability": "1",
        "Reflected in APS": "no", "Pipeline Status": "open",
    }
    base.update(over)
    return base


def test_history_dedupes_rows_identical_only_after_cleanup():
    """Item# 380574 vs 380574.0 and Customer URM vs urm collapse post-cleanup."""
    df_new = pd.DataFrame([
        _dist_row(),
        _dist_row(**{"Item #": "380574.0", "Customer": "urm"}),  # same after cleanup
    ])
    # Fresh history, matching how the pipeline seeds it (empty BUT with the
    # upload's columns) — isolates the within-file dedupe.
    df_history = pd.DataFrame(columns=df_new.columns)

    combined, seed, months = rsp._merge_history_and_build_seed(
        df_new, df_history, _log(),
    )
    # The two source rows are identical after type/text normalisation.
    assert len(combined) == 1
    # And the seed built from the de-duplicated history is a single row whose
    # metric is NOT double-counted.
    assert len(seed) == 1
    assert float(seed["Lbs./yr"].iloc[0]) == 100.0


def test_history_keeps_distinct_business_rows():
    df_new = pd.DataFrame([_dist_row(), _dist_row(**{"Item #": "999999"})])
    combined, _seed, _m = rsp._merge_history_and_build_seed(
        df_new, pd.DataFrame(columns=df_new.columns), _log(),
    )
    assert len(combined) == 2


# ── RO_History_Tracker: final dedupe is whitespace-robust ────────────────────

def test_ro_history_merge_removes_identical_rows():
    seed = pd.DataFrame({
        "Format": ["F"], "Customer": ["URM"], "Item #": ["380574"],
        "Month": ["07/01/2026"],
    })
    # Two carried-over history rows for a non-seed month, identical bar a
    # trailing space — must collapse to one in the rebuilt history.
    df_hist = pd.DataFrame({
        "Format": ["F", "F"], "Customer": ["ACME", "ACME "],
        "Item #": ["111", "111"], "Month": ["06/01/2026", "06/01/2026"],
    })
    out = rsp._merge_into_ro_history(seed, df_hist, _log())
    # 1 unique carried-over row + 1 appended seed row.
    assert len(out) == 2
    assert set(out["Month"]) == {"06/01/2026", "07/01/2026"}


def test_ro_history_merge_replaces_matching_month_without_duplicating():
    seed = pd.DataFrame({
        "Format": ["F"], "Customer": ["URM"], "Item #": ["380574"],
        "Month": ["07/01/2026"],
    })
    # Pre-existing July rows must be replaced by the seed, not stacked.
    df_hist = pd.DataFrame({
        "Format": ["F"], "Customer": ["URM"], "Item #": ["380574"],
        "Month": ["07/01/2026"],
    })
    out = rsp._merge_into_ro_history(seed, df_hist, _log())
    assert len(out) == 1


def _seed_row(item, lbs, prob, status, reflected="no"):
    """Factory for a single Distribution Tracker row (post-cleanup dtypes)."""
    return {
        "Format": "F", "Customer": "C", "Taxonomy": "T", "Brand": "B",
        "Item #": item, "Item Desc": "D", "Probability": prob,
        "First Ship Date": "2026-04-01", "Lbs./yr": lbs, "PC$/yr": 0.0,
        "Slotting": 0.0, "Reflected in APS": reflected,
        "Pipeline Status": status, "Month": "2026-06",
    }


def test_build_ro_seed_default_rules_50pct_risk_and_declined_plus_closed_excluded():
    """Default rules: Risk = neg vol + prob ≥ 50%; Opportunity excludes Declined
    and Closed; Reflected-in-APS-only whitelist applies."""
    df = pd.DataFrame([
        _seed_row("100", 1000.0, 1.0, "Open"),           # normal opportunity → kept
        _seed_row("200", -5000.0, 0.75, "Declined"),     # RISK (neg + 75% + no) → kept
        _seed_row("300", -4000.0, 0.30, "Declined"),     # neg but 30% → not risk → dropped
        _seed_row("400", -3000.0, 1.0, "Declined", reflected="yes"),  # reflected → dropped
        _seed_row("500", 2000.0, 0.5, "Closed"),         # Closed → dropped
        _seed_row("600", 2000.0, 0.5, "Closed Won"),     # substring match: closed → dropped
    ])
    seed = rsp._build_ro_seed(df, {"2026-06"}, _log())
    items = set(seed["Item #"].astype(str))
    assert "100" in items          # normal opportunity
    assert "200" in items          # 75%-prob negative — Risk bypasses Declined
    assert "300" not in items      # 30% prob — below Risk threshold, still declined
    assert "400" not in items      # Reflected in APS — not incremental R&O
    assert "500" not in items      # Closed status now excluded
    assert "600" not in items      # substring "closed" still matches


def test_build_ro_seed_config_override_tightens_risk_to_hundred_percent():
    """Passing a config with 100% Risk threshold recovers yesterday's tight rule."""
    from data_sources.ro_rules_config import RoRulesConfig
    df = pd.DataFrame([
        _seed_row("A", -5000.0, 0.75, "Declined"),  # was Risk at 50%, not at 100%
        _seed_row("B", -5000.0, 1.0,  "Declined"),  # still Risk at 100%
    ])
    cfg = RoRulesConfig.default().with_updates(min_risk_probability=1.0)
    seed = rsp._build_ro_seed(df, {"2026-06"}, _log(), config=cfg)
    items = set(seed["Item #"].astype(str))
    assert "A" not in items
    assert "B" in items


def test_build_ro_seed_carries_forward_risk_from_prior_snapshot():
    """A risk captured in an EARLIER snapshot must still land in RO_Seed even
    when this cycle's Distribution Tracker upload doesn't include it — the
    fix that keeps RO_Seed reconciled with the RO Summary Report.

    Prior-month row (Item 900) is a Risk; the current snapshot (2026-07) only
    carries an unrelated Opportunity.  The seed builder must carry Item 900
    forward using its prior-month values.
    """
    df = pd.DataFrame([
        _seed_row("100", 1000.0, 1.0, "Open"),               # current opportunity
        _seed_row("900", -8000.0, 1.0, "Declined"),          # prior RISK
    ])
    # Stamp explicit Months: row 0 is in this cycle (2026-07); row 1 is in
    # the earlier snapshot (2026-06). The factory's default Month is 2026-06,
    # so the current-snapshot row must be overridden.
    df.loc[0, "Month"] = "2026-07"
    df.loc[1, "Month"] = "2026-06"
    seed = rsp._build_ro_seed(df, {"2026-07"}, _log())
    items = set(seed["Item #"].astype(str))
    assert "100" in items, "current snapshot opportunity should be present"
    assert "900" in items, "prior-snapshot risk should be carried forward"


def test_build_ro_seed_carry_forward_keeps_latest_snapshot_only():
    """When the same risk business key appears in multiple prior snapshots,
    only the LATEST-Month copy is carried into RO_Seed (no double-counting)."""
    df = pd.DataFrame([
        _seed_row("900", -8000.0, 1.0, "Declined"),  # 2026-04 (older)
        _seed_row("900", -9000.0, 1.0, "Declined"),  # 2026-05 (newer)
    ])
    df.loc[0, "Month"] = "2026-04"
    df.loc[1, "Month"] = "2026-05"
    seed = rsp._build_ro_seed(df, {"2026-07"}, _log())
    subset = seed.loc[seed["Item #"].astype(str) == "900"]
    assert len(subset) == 1, "expected one carried-forward row per business key"
    assert float(subset["Lbs./yr"].iloc[0]) == -9000.0, "newest snapshot wins"


def test_build_ro_seed_current_snapshot_wins_over_carry_forward():
    """When the current snapshot ALREADY has a row for a business key, the
    fresh Tracker value is authoritative — no carried-over copy is added."""
    df = pd.DataFrame([
        _seed_row("900", -1000.0, 1.0, "Declined"),  # current snapshot value
        _seed_row("900", -8000.0, 1.0, "Declined"),  # prior snapshot (must not stack)
    ])
    df.loc[0, "Month"] = "2026-07"
    df.loc[1, "Month"] = "2026-06"
    seed = rsp._build_ro_seed(df, {"2026-07"}, _log())
    subset = seed.loc[seed["Item #"].astype(str) == "900"]
    assert len(subset) == 1
    assert float(subset["Lbs./yr"].iloc[0]) == -1000.0, "current snapshot wins"


def test_build_ro_seed_does_not_carry_forward_prior_opportunities():
    """Opportunities are NOT carried forward — the current snapshot is the
    source of truth for positive lines.  Only risks travel across cycles."""
    df = pd.DataFrame([
        _seed_row("100", 1000.0, 1.0, "Open"),   # current snapshot opportunity
        _seed_row("800", 4000.0, 1.0, "Open"),   # prior snapshot opportunity — must NOT carry
    ])
    # Factory default Month is 2026-06 — pin row 0 to the current snapshot.
    df.loc[0, "Month"] = "2026-07"
    df.loc[1, "Month"] = "2026-06"
    seed = rsp._build_ro_seed(df, {"2026-07"}, _log())
    items = set(seed["Item #"].astype(str))
    assert "100" in items
    assert "800" not in items, "stale opportunity from prior snapshot must not carry forward"


def test_build_ro_seed_config_can_widen_opportunity_probability_threshold():
    """Setting min_opp_probability = 0.5 drops all lines below 50%."""
    from data_sources.ro_rules_config import RoRulesConfig
    df = pd.DataFrame([
        _seed_row("LOW",  1000.0, 0.30, "Open"),   # below new 50% threshold → dropped
        _seed_row("HIGH", 1000.0, 0.60, "Open"),   # above 50% → kept
    ])
    cfg = RoRulesConfig.default().with_updates(min_opp_probability=0.5)
    seed = rsp._build_ro_seed(df, {"2026-06"}, _log(), config=cfg)
    items = set(seed["Item #"].astype(str))
    assert "LOW" not in items
    assert "HIGH" in items
