"""Tests for the cost-override precedence pattern and the
``summarize_scenarios`` Multi-Scenario Summary builder.

Both features share one design constraint: the displayed scenario table
must always reflect the live calc engine. Overrides are stored in
dedicated ``<Component> Override`` columns so saved values can never
silently mask a recomputation, and the summary builder always pipes
each scenario through ``recompute_items`` before aggregating.
"""

from __future__ import annotations

import pathlib

import pandas as pd
import pytest

from data_sources.rfp_pnl_store import (
    COST_OVERRIDE_FOR,
    METRIC_COLS,
    SUMMARY_PER_ITEM_METRICS,
    SUMMARY_TOTAL_LABEL,
    SUMMARY_TOTAL_METRICS,
    _calc_for_item,
    recompute_items,
    summarize_scenarios,
)
from tests.test_rfp_conversion_cost import (
    _LIVE_BOM_PATH,
    _live_input,
    _live_sources,
)


# ─── Override pattern ─────────────────────────────────────────────────────────

@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_override_columns_are_persisted_in_metric_cols():
    """Every cost component that supports an override has its override
    column in the canonical schema, so saved CSVs round-trip correctly.
    """
    for component, override_col in COST_OVERRIDE_FOR.items():
        assert override_col in METRIC_COLS, (
            f"{override_col!r} missing from METRIC_COLS"
        )
        assert component in METRIC_COLS, (
            f"{component!r} missing from METRIC_COLS"
        )


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_override_blank_falls_back_to_calc():
    """Blank override → display equals BOM/Budget calc default."""
    sources = _live_sources()
    row = _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                      target_lbs=4.30, ref_lbs=4.30)
    # All overrides blank by default in _live_input.
    metrics = _calc_for_item(row, sources)
    # Sanity — the calc should produce a real number, not None.
    assert metrics["Milk"] is not None
    assert metrics["Milk"] > 0.0


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_override_nonblank_takes_precedence_over_calc():
    """Non-blank parseable override → display equals override value."""
    sources = _live_sources()
    row = _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                      target_lbs=4.30, ref_lbs=4.30)
    row["Milk Override"] = "9.99"
    row["Ingredient Override"] = "0.5"
    row["Packaging Override"] = "1.25"
    row["Conversion Cost Override"] = "2.00"
    row["Cost of Quality Override"] = "0.10"
    row["Internal Logistics (Shuttling & WHSE) Override"] = "0.20"

    metrics = _calc_for_item(row, sources)
    assert metrics["Milk"] == pytest.approx(9.99)
    assert metrics["Ingredient"] == pytest.approx(0.5)
    assert metrics["Packaging"] == pytest.approx(1.25)
    assert metrics["Conversion Cost"] == pytest.approx(2.00)
    assert metrics["Cost of Quality"] == pytest.approx(0.10)
    assert metrics["Internal Logistics (Shuttling & WHSE)"] == pytest.approx(0.20)


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_override_garbage_value_falls_back_to_calc():
    """Unparseable override (e.g. typo'd 'abc') falls back to the calc
    default rather than silently returning None and zeroing the cell.
    """
    sources = _live_sources()
    row = _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                      target_lbs=4.30, ref_lbs=4.30)
    row["Milk Override"] = "not a number"
    metrics = _calc_for_item(row, sources)
    assert metrics["Milk"] is not None
    assert metrics["Milk"] > 0.0


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_override_survives_recompute_round_trip():
    """Overrides are inputs, not derived metrics: ``recompute_items``
    must never overwrite them, even when the override conflicts with
    the BOM/Budget default.
    """
    sources = _live_sources()
    row = _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                      target_lbs=4.30, ref_lbs=4.30)
    row["Item"] = "Item 1"
    row["Milk Override"] = "9.99"
    items = pd.DataFrame([row])

    refreshed = recompute_items(items, sources)
    out = refreshed.iloc[0]
    assert str(out["Milk Override"]).strip() == "9.99"
    assert float(out["Milk"]) == pytest.approx(9.99)


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_recompute_purges_stale_cost_when_override_blank():
    """Regression: a stale ``Milk = 0`` written into a saved CSV must be
    overwritten by Refresh when the override column is blank, even
    though the legacy code path treated the cost cell as "do not
    overwrite if non-blank" defaultable. This nails down the fix that
    promoted cost cells into STRICT_CALC_METRICS.
    """
    sources = _live_sources()
    row = _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                      target_lbs=4.30, ref_lbs=4.30)
    row["Item"] = "Item 1"
    row["Milk"] = 0.0  # stale value from a pre-fix saved scenario
    row["Ingredient"] = 0.0
    row["Packaging"] = 0.0
    row["Conversion Cost"] = 0.0
    items = pd.DataFrame([row])

    refreshed = recompute_items(items, sources)
    out = refreshed.iloc[0]
    for col in ("Milk", "Ingredient", "Packaging", "Conversion Cost"):
        assert out[col] != 0.0, f"Stale {col!r} survived Refresh"


# ─── Multi-Scenario Summary builder ──────────────────────────────────────────

def _scenario_with_two_items() -> pd.DataFrame:
    """Synthetic scenario with two items and pre-baked metric values.

    We bypass the BOM lookup by setting overrides directly, so the
    summary math is fully deterministic and not coupled to the BOM
    file's contents.
    """
    common_blank_metrics = {col: "" for col in METRIC_COLS}
    item_a = {
        **common_blank_metrics,
        "Month": "1-Jun-26", "Plant": "Portland",
        "Target SKU Name": "Item A", "Category": "ESL",
        "Target SKU lbs per Each": 2.0,
        "Target SKU Volume (units)": 100.0,
        "Milk Reference SKU": "DG HH Qt UP",
        "Milk Reference SKU lbs per Each": 2.0,
        "Ingredient Reference SKU lbs per Each": 2.0,
        "Packaging Reference SKU lbs per Each": 2.0,
        "Conversion Reference SKU lbs per Each": 2.0,
        "PCM $/lbs": 0.50,
        "Milk Override": 1.00,
        "Ingredient Override": 0.10,
        "Packaging Override": 0.20,
        "Conversion Cost Override": 0.30,
        "Cost of Quality Override": 0.05,
        "Internal Logistics (Shuttling & WHSE) Override": 0.05,
        "Other Cost": 0.0,
    }
    item_b = {
        **common_blank_metrics,
        "Month": "1-Jun-26", "Plant": "Portland",
        "Target SKU Name": "Item B", "Category": "Cultured",
        "Target SKU lbs per Each": 1.0,
        "Target SKU Volume (units)": 200.0,
        "Milk Reference SKU": "DG HH Qt UP",
        "Milk Reference SKU lbs per Each": 1.0,
        "Ingredient Reference SKU lbs per Each": 1.0,
        "Packaging Reference SKU lbs per Each": 1.0,
        "Conversion Reference SKU lbs per Each": 1.0,
        "PCM $/lbs": 0.30,
        "Milk Override": 0.50,
        "Ingredient Override": 0.05,
        "Packaging Override": 0.10,
        "Conversion Cost Override": 0.10,
        "Cost of Quality Override": 0.02,
        "Internal Logistics (Shuttling & WHSE) Override": 0.03,
        "Other Cost": 0.0,
    }
    return pd.DataFrame([
        {"Item": "Item 1", **item_a},
        {"Item": "Item 2", **item_b},
    ])


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_summary_shape_and_metrics():
    """One scenario, two items: summary has 2 × 5 per-item rows + 5 Total rows."""
    sources = _live_sources()
    df = summarize_scenarios(
        {"Sysco_FY26": _scenario_with_two_items()},
        sources,
    )

    assert list(df.columns) == ["Item", "Category", "Metric", "Sysco_FY26"]
    expected_rows = 2 * len(SUMMARY_PER_ITEM_METRICS) + len(SUMMARY_TOTAL_METRICS)
    assert len(df) == expected_rows

    # Per-item rows come first.
    per_item = df[df["Item"] != SUMMARY_TOTAL_LABEL]
    totals = df[df["Item"] == SUMMARY_TOTAL_LABEL]
    assert set(per_item["Item"]) == {"Item A", "Item B"}
    assert set(per_item["Metric"]) == set(SUMMARY_PER_ITEM_METRICS)
    assert set(totals["Metric"]) == set(SUMMARY_TOTAL_METRICS)

    # Each per-item metric appears exactly once per item.
    for item in ("Item A", "Item B"):
        sub = per_item[per_item["Item"] == item]
        assert list(sub["Metric"]) == list(SUMMARY_PER_ITEM_METRICS)


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_summary_totals_use_volume_weighting():
    """Total Volume = Σ(units × lbs/Each); Total Revenue = Σ(FOB × units);
    Total GP = Σ(GP$); PCM% / GP% are volume-weighted (lbs).
    """
    sources = _live_sources()
    df = summarize_scenarios(
        {"S": _scenario_with_two_items()}, sources,
    )

    # Item A: lbs/EA=2, units=100 → Volume_lbs = 200
    # Item B: lbs/EA=1, units=200 → Volume_lbs = 200
    total_volume = df.loc[
        (df["Item"] == SUMMARY_TOTAL_LABEL) & (df["Metric"] == "Volume"), "S"
    ].iloc[0]
    assert total_volume == pytest.approx(400.0)

    # Each item has the same volume in lbs, so PCM% / GP% in the Total
    # row should be the simple arithmetic mean of the two items'
    # percentages (volume-weighting collapses to equal weighting when
    # weights are equal).
    pcm_a = df.loc[(df["Item"] == "Item A") & (df["Metric"] == "PCM%"), "S"].iloc[0]
    pcm_b = df.loc[(df["Item"] == "Item B") & (df["Metric"] == "PCM%"), "S"].iloc[0]
    total_pcm = df.loc[
        (df["Item"] == SUMMARY_TOTAL_LABEL) & (df["Metric"] == "PCM%"), "S"
    ].iloc[0]
    assert total_pcm == pytest.approx((pcm_a + pcm_b) / 2)


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_summary_filters_by_item_and_category():
    """Empty filter → include all; non-empty → restrict and re-aggregate Totals."""
    sources = _live_sources()
    full = summarize_scenarios(
        {"S": _scenario_with_two_items()}, sources,
    )
    only_a = summarize_scenarios(
        {"S": _scenario_with_two_items()}, sources,
        items_filter=["Item A"],
    )
    only_esl = summarize_scenarios(
        {"S": _scenario_with_two_items()}, sources,
        categories_filter=["ESL"],
    )

    # Filtering by Item A yields exactly one item set in the per-item rows.
    per_item_only_a = only_a[only_a["Item"] != SUMMARY_TOTAL_LABEL]
    assert set(per_item_only_a["Item"]) == {"Item A"}

    # Total row Volume must reflect the filter, not the un-filtered superset.
    assert (
        only_a.loc[
            (only_a["Item"] == SUMMARY_TOTAL_LABEL)
            & (only_a["Metric"] == "Volume"), "S"
        ].iloc[0]
        != full.loc[
            (full["Item"] == SUMMARY_TOTAL_LABEL)
            & (full["Metric"] == "Volume"), "S"
        ].iloc[0]
    )

    # Category filter behaves the same way.
    per_item_only_esl = only_esl[only_esl["Item"] != SUMMARY_TOTAL_LABEL]
    assert set(per_item_only_esl["Category"]) == {"ESL"}


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_summary_handles_empty_input():
    sources = _live_sources()
    df = summarize_scenarios({}, sources)
    assert df.empty


def test_to_float_treats_nan_as_missing():
    """``_to_float(float('nan'))`` must return ``None``, not NaN.

    pandas' ``read_csv`` represents blank cells as float NaN. If
    ``_to_float`` propagated those NaNs, ``_apply_override`` would
    accept NaN as a "valid" override (since ``NaN is not None``), which
    blows up the entire cost stack on every reloaded scenario.
    """
    import math
    from data_sources.rfp_pnl_store import _to_float
    assert _to_float(float("nan")) is None
    assert _to_float(math.nan) is None
    # Sanity — real values still parse.
    assert _to_float("2.15") == pytest.approx(2.15)
    assert _to_float(2.15) == pytest.approx(2.15)
    assert _to_float("") is None
    assert _to_float(None) is None


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_summary_round_trip_through_csv_does_not_leak_nan(tmp_path):
    """End-to-end regression for the ``$nan`` / ``nan%`` summary bug.

    A scenario built in memory with ``""`` override cells, when written
    to CSV and re-read by pandas, comes back with float-NaN override
    cells. Pre-fix this caused ``summarize_scenarios`` to produce NaN
    FOB / Revenue / PCM% / GP%. Post-fix every leakage path is plugged
    by ``_to_float``'s NaN guard.
    """
    sources = _live_sources()
    items = _scenario_with_two_items()
    csv_path = tmp_path / "scenario.csv"
    items.to_csv(csv_path, index=False)
    reloaded = pd.read_csv(csv_path)

    summary = summarize_scenarios({"S": reloaded}, sources)
    # Every numeric scenario column cell must be finite or None — never NaN.
    leaked = [
        (row["Item"], row["Metric"], row["S"])
        for _, row in summary.iterrows()
        if isinstance(row["S"], float) and pd.isna(row["S"])
    ]
    assert not leaked, f"NaN leaked into summary cells: {leaked}"


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_summary_recomputes_each_scenario():
    """Stale costs in a saved scenario must NOT bleed into the summary.

    We craft a scenario where the persisted ``Milk = 0`` value would
    misrepresent the FOB if the summary trusted it. Because
    ``summarize_scenarios`` always pipes through ``recompute_items``,
    the displayed FOB / Revenue come from the override-driven calc.
    """
    sources = _live_sources()
    items = _scenario_with_two_items()
    items.loc[0, "Milk"] = 0.0
    items.loc[0, "FOB Price"] = 0.0  # stale ahead-of-recompute

    df = summarize_scenarios({"S": items}, sources)
    fob_a = df.loc[(df["Item"] == "Item A") & (df["Metric"] == "FOB Price"), "S"].iloc[0]
    # Item A: Milk=1.00 + Ing=0.10 + Pkg=0.20 + PCM=PCM$/lb*lbs/EA=0.50*2=1.00
    # → FOB = 1.00 + 0.10 + 0.20 + 1.00 = 2.30
    assert fob_a == pytest.approx(2.30)
