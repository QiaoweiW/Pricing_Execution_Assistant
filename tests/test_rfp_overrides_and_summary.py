"""Tests for the cost-override precedence pattern and the
``summarize_scenarios`` Multi-Scenario Summary builder.

Both features share one design constraint: the displayed scenario table
must always reflect the live calc engine. Overrides are stored in
dedicated ``<Component> Override`` columns so saved values can never
silently mask a recomputation, and the summary builder always pipes
each scenario through ``recompute_items`` before aggregating.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pandas as pd
import pytest

from data_sources.rfp_pnl_store import (
    COST_OVERRIDE_FOR,
    METRIC_COLS,
    REQUIRED_INPUT_FIELDS,
    STRICT_CALC_METRICS,
    SUMMARY_PER_ITEM_METRICS,
    SUMMARY_TOTAL_LABEL,
    SUMMARY_TOTAL_METRICS,
    _calc_for_item,
    _norm,
    bom_search,
    bom_search_item_options,
    find_missing_required_inputs,
    recompute_items,
    reference_sku_options,
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
        (df["Item"] == SUMMARY_TOTAL_LABEL) & (df["Metric"] == "Volume (pounds)"), "S"
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
            & (only_a["Metric"] == "Volume (pounds)"), "S"
        ].iloc[0]
        != full.loc[
            (full["Item"] == SUMMARY_TOTAL_LABEL)
            & (full["Metric"] == "Volume (pounds)"), "S"
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


# ─── Retail-side metrics (Delivered Price + Retailer's Margin%) ──────────────

@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_delivered_price_equals_fob_plus_freight():
    """Delivered Price = FOB + Freight; rounding-tolerant equality."""
    sources = _live_sources()
    row = _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                      target_lbs=4.30, ref_lbs=4.30)
    row["PCM $/lbs"] = "0.26"
    row["Freight Cost"] = "0.50"
    metrics = _calc_for_item(row, sources)
    assert metrics["FOB Price"] is not None
    assert metrics["Delivered Price"] == pytest.approx(metrics["FOB Price"] + 0.50)


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_delivered_price_treats_blank_freight_as_zero():
    """Blank Freight → Delivered Price equals FOB exactly (no NaN leak)."""
    sources = _live_sources()
    row = _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                      target_lbs=4.30, ref_lbs=4.30)
    row["PCM $/lbs"] = "0.26"
    row["Freight Cost"] = ""  # blank
    metrics = _calc_for_item(row, sources)
    assert metrics["Delivered Price"] == pytest.approx(metrics["FOB Price"])


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_retailer_margin_formula():
    """Retailer's Margin% = (Retail - Delivered) / Retail."""
    sources = _live_sources()
    row = _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                      target_lbs=4.30, ref_lbs=4.30)
    row["PCM $/lbs"] = "0.26"
    row["Freight Cost"] = "0.50"
    row["Retail Price"] = "8.00"
    metrics = _calc_for_item(row, sources)
    expected = (8.00 - metrics["Delivered Price"]) / 8.00
    assert metrics["Retailer's Margin%"] == pytest.approx(expected)


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_retailer_margin_blank_when_retail_missing_or_zero():
    """No Retail Price (or Retail = 0) → Retailer's Margin% is None."""
    sources = _live_sources()
    row = _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                      target_lbs=4.30, ref_lbs=4.30)
    row["PCM $/lbs"] = "0.26"
    row["Retail Price"] = ""  # blank
    metrics_blank = _calc_for_item(row, sources)
    assert metrics_blank["Retailer's Margin%"] is None
    # Delivered Price is still well-defined.
    assert metrics_blank["Delivered Price"] is not None

    row["Retail Price"] = "0"  # zero divisor
    metrics_zero = _calc_for_item(row, sources)
    assert metrics_zero["Retailer's Margin%"] is None


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_delivered_and_margin_blank_when_fob_missing():
    """If FOB can't be computed (e.g. PCM $/lbs blank), the retail-side
    metrics propagate as None rather than partial / NaN values.
    """
    sources = _live_sources()
    row = _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                      target_lbs=4.30, ref_lbs=4.30)
    row["PCM $/lbs"] = ""  # blanks FOB
    row["Freight Cost"] = "0.50"
    row["Retail Price"] = "8.00"
    metrics = _calc_for_item(row, sources)
    assert metrics["FOB Price"] is None
    assert metrics["Delivered Price"] is None
    assert metrics["Retailer's Margin%"] is None


def test_retail_side_metrics_are_strict():
    """Both calculated retail-side metrics live in STRICT_CALC_METRICS so
    a stale persisted value can never mask a recomputation (same fix
    pattern as the cost cells).
    """
    assert "Delivered Price" in STRICT_CALC_METRICS
    assert "Retailer's Margin%" in STRICT_CALC_METRICS


def test_retail_side_columns_in_metric_cols():
    for col in ("Retail Price", "Freight Cost",
                "Delivered Price", "Retailer's Margin%"):
        assert col in METRIC_COLS, f"{col!r} missing from METRIC_COLS"


# ─── Required-input prompt helper ────────────────────────────────────────────

def test_find_missing_required_inputs_flags_milk_lbs_and_pcm():
    """Both Milk Reference SKU lbs/Each and PCM $/lbs are required."""
    assert "Milk Reference SKU lbs per Each" in REQUIRED_INPUT_FIELDS
    assert "PCM $/lbs" in REQUIRED_INPUT_FIELDS

    items = pd.DataFrame([{
        "Target SKU Name": "Item A",
        "Milk Reference SKU lbs per Each": "",
        "PCM $/lbs": "0.26",
    }, {
        "Target SKU Name": "",
        "Milk Reference SKU lbs per Each": "2.15",
        "PCM $/lbs": "",
    }])
    issues = find_missing_required_inputs(items)
    assert len(issues) == 2
    label_a, missing_a = issues[0]
    label_b, missing_b = issues[1]
    assert label_a == "Item A"
    assert "Milk Reference SKU lbs per Each" in missing_a
    assert "PCM $/lbs" not in missing_a
    # Blank Target SKU Name → falls back to "Item N" (1-indexed).
    assert label_b == "Item 2"
    assert missing_b == ["PCM $/lbs"]


def test_find_missing_required_inputs_returns_empty_when_all_filled():
    items = pd.DataFrame([{
        "Target SKU Name": "Item A",
        "Milk Reference SKU lbs per Each": "2.15",
        "PCM $/lbs": "0.26",
    }])
    assert find_missing_required_inputs(items) == []


def test_find_missing_required_inputs_handles_nan_floats():
    """pandas-loaded blanks come back as NaN floats — must still flag."""
    import math
    items = pd.DataFrame([{
        "Target SKU Name": "Item A",
        "Milk Reference SKU lbs per Each": math.nan,
        "PCM $/lbs": math.nan,
    }])
    issues = find_missing_required_inputs(items)
    assert len(issues) == 1
    label, missing = issues[0]
    assert set(missing) == {"Milk Reference SKU lbs per Each", "PCM $/lbs"}


# ─── Summary "Revenue" → "FOB Revenue" rename ────────────────────────────────

def test_summary_uses_fob_revenue_label():
    """Both per-item and Total metric tuples expose 'FOB Revenue', not 'Revenue'."""
    assert "FOB Revenue" in SUMMARY_PER_ITEM_METRICS
    assert "FOB Revenue" in SUMMARY_TOTAL_METRICS
    assert "Revenue" not in SUMMARY_PER_ITEM_METRICS
    assert "Revenue" not in SUMMARY_TOTAL_METRICS


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_summary_emits_fob_revenue_rows():
    sources = _live_sources()
    df = summarize_scenarios({"S": _scenario_with_two_items()}, sources)
    metric_values = set(df["Metric"])
    assert "FOB Revenue" in metric_values
    assert "Revenue" not in metric_values


# ─── Volume (pounds) rename ──────────────────────────────────────────────────

def test_summary_metric_tuples_use_volume_pounds_label():
    assert "Volume (pounds)" in SUMMARY_PER_ITEM_METRICS
    assert "Volume (pounds)" in SUMMARY_TOTAL_METRICS
    assert "Volume" not in SUMMARY_PER_ITEM_METRICS
    assert "Volume" not in SUMMARY_TOTAL_METRICS


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_summary_emits_volume_pounds_rows():
    sources = _live_sources()
    df = summarize_scenarios({"S": _scenario_with_two_items()}, sources)
    metric_values = set(df["Metric"])
    assert "Volume (pounds)" in metric_values
    assert "Volume" not in metric_values


# ─── Total GP = Σ (GP $/EA × units), not Σ per-EA ────────────────────────────

@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_total_gp_is_dollar_sum_not_per_each_sum():
    """Total GP must scale with units, not with item count.

    Build a 1-item scenario, then a 2-item duplicate of it. Per-EA GP
    is identical in both, but Total GP $ must double when units double.
    """
    sources = _live_sources()
    one = _scenario_with_two_items().iloc[[0]].copy()
    two = pd.concat([one, one.copy()], ignore_index=True)
    two.loc[1, "Item"] = "Item 2"
    two.loc[1, "Target SKU Name"] = "Item A copy"

    df_one = summarize_scenarios({"S": one}, sources)
    df_two = summarize_scenarios({"S": two}, sources)

    total_gp_one = df_one.loc[
        (df_one["Item"] == SUMMARY_TOTAL_LABEL) & (df_one["Metric"] == "GP"), "S"
    ].iloc[0]
    total_gp_two = df_two.loc[
        (df_two["Item"] == SUMMARY_TOTAL_LABEL) & (df_two["Metric"] == "GP"), "S"
    ].iloc[0]
    assert total_gp_two == pytest.approx(2 * total_gp_one)


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_total_gp_matches_manual_dollar_sum():
    """Total GP equals Σ (per-item GP $/EA × per-item units)."""
    sources = _live_sources()
    items = _scenario_with_two_items()
    refreshed = recompute_items(items, sources)

    expected = 0.0
    for _, row in refreshed.iterrows():
        gp = float(row["GP"])
        units = float(row["Target SKU Volume (units)"])
        expected += gp * units

    df = summarize_scenarios({"S": items}, sources)
    total_gp = df.loc[
        (df["Item"] == SUMMARY_TOTAL_LABEL) & (df["Metric"] == "GP"), "S"
    ].iloc[0]
    assert total_gp == pytest.approx(expected)


# ─── BOM Search + Reference SKU dropdown sourcing ─────────────────────────────
#
# The Reference SKU dropdowns and the BOM Search panel both source their
# options from the BOM (Level-1 ``Rule Item Desc`` for a Month + Plant); the
# Reference SKU list is additionally gated on PDH membership. ``bom_search``
# returns the matching Level-1 rows plus their chained Level-2 sub-recipe rows.

_KNOWN_MONTH = "1-Jun-26"
_KNOWN_PLANT = "Portland"
_KNOWN_SKU = "DG Hvy Whip Hg UP"


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_bom_search_item_options_are_level1_for_month_plant():
    """Item Description options are unique, sorted Level-1 descs for the pair."""
    sources = _live_sources()
    opts = bom_search_item_options(
        sources, month=_KNOWN_MONTH, plant=_KNOWN_PLANT
    )
    assert _KNOWN_SKU in opts
    bom = sources.bom_df
    for desc in opts:
        match = bom[
            (bom["_norm_month"] == _norm(_KNOWN_MONTH))
            & (bom["_norm_plant"] == _norm(_KNOWN_PLANT))
            & (bom["_norm_rule_item_desc"] == _norm(desc))
            & (bom["Level"].astype(str).str.strip() == "1")
        ]
        assert not match.empty, f"{desc} has no Level-1 row for the pair"
    # Deduped and sorted case-insensitively.
    assert list(opts) == sorted(set(opts), key=lambda x: x.casefold())


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_item_options_empty_without_both_parents():
    """A blank Month or Plant collapses the cascading dropdowns to empty."""
    sources = _live_sources()
    assert bom_search_item_options(sources, month="", plant=_KNOWN_PLANT) == ()
    assert bom_search_item_options(sources, month=_KNOWN_MONTH, plant="") == ()
    assert reference_sku_options(sources, month="", plant=_KNOWN_PLANT) == ()
    assert reference_sku_options(sources, month=_KNOWN_MONTH, plant="") == ()


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_bom_search_returns_level1_and_chained_level2():
    """Level-1 rows match the filters; Level-2 rows are chained sub-recipes."""
    sources = _live_sources()
    level1, level2 = bom_search(
        sources, month=_KNOWN_MONTH, plant=_KNOWN_PLANT, item_desc=_KNOWN_SKU,
    )
    assert not level1.empty
    assert (level1["Level"].astype(str).str.strip() == "1").all()
    assert (level1["Rule Item Desc"].map(_norm) == _norm(_KNOWN_SKU)).all()
    if not level2.empty:
        assert level2["Level"].astype(str).str.contains("2").all()
    # The synthetic ``Step`` column belonged to the retired extractor; the
    # BOM-search output must carry only native BOM columns.
    assert "Step" not in level1.columns
    assert "Step" not in level2.columns


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_bom_search_blank_filters_yield_header_only_frames():
    """Missing filters return empty frames that still carry column headers."""
    sources = _live_sources()
    level1, level2 = bom_search(sources, month="", plant="", item_desc="")
    assert level1.empty and level2.empty
    assert len(level1.columns) > 0 and len(level2.columns) > 0


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_reference_sku_options_gate_on_pdh_membership():
    """Reference SKU options are Level-1 BOM descs intersected with PDH."""
    sources = _live_sources()
    all_level1 = bom_search_item_options(
        sources, month=_KNOWN_MONTH, plant=_KNOWN_PLANT
    )
    assert len(all_level1) >= 2
    # Admit only the first two Level-1 descs into a synthetic PDH set; the
    # Reference SKU dropdown must return exactly that intersection.
    admitted = all_level1[:2]
    gated = dataclasses.replace(
        sources, pdh_item_desc_set=frozenset(_norm(d) for d in admitted)
    )
    opts = reference_sku_options(gated, month=_KNOWN_MONTH, plant=_KNOWN_PLANT)
    assert set(opts) == set(admitted)
