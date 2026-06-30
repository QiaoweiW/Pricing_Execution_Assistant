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
