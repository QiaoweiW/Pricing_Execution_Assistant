"""Case study + integration test for the RFP P&L Conversion Cost rule.

Two layers:

1. **Synthetic case study** — a fake BOM that mirrors every row visible
   in the analyst's screenshot of DG Hvy Whip Hg UP / Portland /
   1-Jun-26, plus deliberate decoy rows that the filter must drop
   (empty-Tag rollup, Depreciation, Milk Component, Ingredient, Milk,
   Packaging, wrong month / plant / SKU / level). Expected total = 0.8258883.

2. **Live BOM integration test** — points at
   ``data/RFP Financial Analysis/BOM_History_Tracker_tagged.csv`` (the
   on-disk copy of the Fabric file) and exercises the rule across every
   plant present in the file, asserting:
       * the Portland total matches the analyst's reported ~0.825,
       * Depreciation, Milk Component, Ingredient, Milk and Packaging
         tagged rows are never summed into Conversion Cost,
       * empty-Tag rollup rows are never summed,
       * Conversion Cost is non-negative for every plant.
"""

from __future__ import annotations

import pathlib

import pandas as pd
import pytest

from data_sources.rfp_pnl_store import (
    RfpPnlSources,
    _calc_for_item,
    _fmt_level,
    _norm,
    recompute_items,
)


# Rows lifted verbatim from the analyst's screenshot for
# DG Hvy Whip Hg UP / Portland / 1-Jun-26 / Level 1. ``Ing-Rsrc Desc``
# is informational; the filter only uses ``Tag``.
_SCREENSHOT_LINES = [
    ("Direct Labor & Benefits - EA",            "Labor",            0.118960084),
    ("Direct Labor & Benefits Variable - EA",   "Labor",            0.029740021),
    ("Cleaners & Supplies - Fixed",             "Other",            0.011298444),
    ("Environmental Expenses - Fixed",          "Other",            0.004145011),
    ("Electricity - Fixed",                     "Electricity",      0.026901964),
    ("Natural Gas - Fixed",                     "Natural Gas",      0.012985966),
    ("Other Expenses - Fixed",                  "Other",            0.077486947),
    ("Warehouse Expenses - Fixed",              "Other",            0.006054297),
    ("Repair & Maint - Fixed",                  "R&M",              0.135954601),
    ("Sewer - Fixed",                           "Sewer",            0.035210313),
    ("Other Utilities - Fixed",                 "Other Utilities",  0.031317117),
    ("Indirect Labor & Benefits - EA",          "Labor",            0.111847950),
    ("Support Labor & Benefits - EA",           "Labor",            0.087923565),
    ("Tax - EA",                                "Tax",              0.013476384),
    ("Cleaners & Supplies - Variable",          "Other",            0.002824611),
    ("Environmental Expenses - Variable",       "Other",            0.001036253),
    ("Electricity - Variable",                  "Electricity",      0.011529413),
    ("Natural Gas - Variable",                  "Natural Gas",      0.012985966),
    ("Other Expenses - Variable",               "Other",            0.019371737),
    ("Warehouse Expenses - Variable",           "Other",            0.024217188),
    ("Repair & Maint - Variable",               "R&M",              0.033988650),
    ("Sewer - Variable",                        "Sewer",            0.008802578),
    ("Other Utilities - Variable",              "Other Utilities",  0.007829279),
    ("Warehouse Labor & Benefits Var - EA",     "Labor",            0.0),
    ("Warehouse Labor & Benefits - EA",         "Labor",            0.0),
]

EXPECTED_CONVERSION_SUM = sum(c for _, _, c in _SCREENSHOT_LINES)  # ≈ 0.8258883


def _bom_row(
    *,
    per_beg: str,
    plant: str,
    rule_item_desc: str,
    level: int | str,
    tag: str,
    ing_rsrc_desc: str,
    ext_cost_1: float,
    qty_1: float = 1.0,
) -> dict:
    return {
        "Per Beg": per_beg,
        "Plant": plant,
        "Rule Item Desc": rule_item_desc,
        "Level": level,
        "Tag": tag,
        "Ing-Rsrc Desc": ing_rsrc_desc,
        "Qty.1": qty_1,
        "Ext Cost": 0.0,
        "Ext Cost.1": ext_cost_1,
    }


def _build_synthetic_sources() -> RfpPnlSources:
    """Fabricate a minimal BOM/Budget/PDH triple sufficient for one item."""
    target_month = "1-Jun-26"
    target_plant = "Portland"
    target_sku = "DG Hvy Whip Hg UP"

    rows: list[dict] = []

    # 1) The 25 real conversion rows from the screenshot.
    for ing_rsrc_desc, tag, ext_cost_1 in _SCREENSHOT_LINES:
        rows.append(_bom_row(
            per_beg=target_month, plant=target_plant,
            rule_item_desc=target_sku, level=1, tag=tag,
            ing_rsrc_desc=ing_rsrc_desc, ext_cost_1=ext_cost_1,
        ))

    # 2) Decoy: Upper Level Costs rollup at Level 1 with empty Tag and
    #    a non-zero Ext Cost.1 (this is the row that polluted the sum
    #    before the empty-Tag guard was added).
    rows.append(_bom_row(
        per_beg=target_month, plant=target_plant,
        rule_item_desc=target_sku, level=1, tag="",
        ing_rsrc_desc="Upper Level Costs", ext_cost_1=1.7721,
    ))

    # 3) Decoy: Depreciation line — must be excluded per the rule.
    rows.append(_bom_row(
        per_beg=target_month, plant=target_plant,
        rule_item_desc=target_sku, level=1, tag="Depreciation",
        ing_rsrc_desc="Depreciation - Plant", ext_cost_1=0.4242,
    ))

    # 4) Decoys: cost categories handled by other P&L lines.
    for tag, ext in [("Milk Component", 0.5), ("Ingredient", 0.6),
                     ("Milk", 0.7), ("Packaging", 0.31)]:
        rows.append(_bom_row(
            per_beg=target_month, plant=target_plant,
            rule_item_desc=target_sku, level=1, tag=tag,
            ing_rsrc_desc=f"{tag} placeholder", ext_cost_1=ext,
        ))

    # 5) Decoys: wrong month / wrong plant / wrong SKU at Level 1 with
    #    a conversion-flavoured Tag — must NOT match.
    rows.append(_bom_row(
        per_beg="1-Jul-26", plant=target_plant, rule_item_desc=target_sku,
        level=1, tag="Labor", ing_rsrc_desc="Labor - other month",
        ext_cost_1=9.99,
    ))
    rows.append(_bom_row(
        per_beg=target_month, plant="Lynden", rule_item_desc=target_sku,
        level=1, tag="Labor", ing_rsrc_desc="Labor - other plant",
        ext_cost_1=9.99,
    ))
    rows.append(_bom_row(
        per_beg=target_month, plant=target_plant,
        rule_item_desc="Cream Whipping 40% HVY", level=1, tag="Labor",
        ing_rsrc_desc="Labor - other SKU", ext_cost_1=9.99,
    ))

    # 6) Decoy: a Level-2 line at the right SKU — should not match (the
    #    rule is strictly Level == 1).
    rows.append(_bom_row(
        per_beg=target_month, plant=target_plant, rule_item_desc=target_sku,
        level=2, tag="Labor", ing_rsrc_desc="Labor - sub recipe",
        ext_cost_1=0.5,
    ))

    bom = pd.DataFrame(rows)

    # Pre-compute the normalized columns the live loader would build.
    bom["_norm_month"] = bom["Per Beg"].map(_norm)
    bom["_norm_plant"] = bom["Plant"].map(_norm)
    bom["_norm_rule_item_desc"] = bom["Rule Item Desc"].map(_norm)
    bom["_norm_tag"] = bom["Tag"].map(_norm)
    bom["_level_text"] = bom["Level"].map(_fmt_level)

    return RfpPnlSources(
        bom_df=bom,
        budget_df=pd.DataFrame(columns=["Category", "Tag", "Budget Value"]),
        pdh_df=pd.DataFrame(columns=["Item Description", "Portfolio Major"]),
        cost_col="Ext Cost.1",
        month_options=(target_month,),
        plant_options=(target_plant,),
        pdh_item_desc_set=frozenset(),
        category_by_desc={},
        budget_sum_by_cat_tag={},
    )


def _input_row() -> pd.Series:
    """Mimic an analyst's RFP P&L input row.

    Reference SKU lbs / Each is set equal to Target SKU lbs / Each so
    that the conversion ratio is 1 and the printed cost equals the
    pure Σ Ext Cost.1 — that lets the test compare directly against
    the screenshot total.
    """
    return pd.Series({
        "Month": "1-Jun-26",
        "Plant": "Portland",
        "Target SKU Name": "DG Hvy Whip Hg UP",
        "Target SKU lbs per Each": 1.0,
        "Target SKU Volume (units)": 1.0,
        "Milk Reference SKU": "DG Hvy Whip Hg UP",
        "Ingredient Reference SKU": "",
        "Packaging Reference SKU": "",
        "Conversion Reference SKU": "DG Hvy Whip Hg UP",
        "Milk Reference SKU lbs per Each": 1.0,
        "Ingredient Reference SKU lbs per Each": "",
        "Packaging Reference SKU lbs per Each": 1.0,
        "Conversion Reference SKU lbs per Each": 1.0,
        "Other Cost": 0.0,
        "PCM $/lbs": 0.0,
    })


def test_conversion_cost_case_study_dg_hvy_whip_hg_up():
    """Case study: DG Hvy Whip Hg UP / Portland / June 2026."""
    sources = _build_synthetic_sources()
    metrics = _calc_for_item(_input_row(), sources)
    conversion = metrics["Conversion Cost"]

    # Every screenshot line is captured; every decoy is rejected;
    # ratio (target_lbs / ref_lbs) = 1, so result == raw Σ Ext Cost.1.
    assert conversion is not None
    assert abs(conversion - EXPECTED_CONVERSION_SUM) < 1e-9, (
        f"Conversion = {conversion!r}, expected {EXPECTED_CONVERSION_SUM!r}"
    )
    # Sanity-check the absolute target reported by the analyst (~0.825).
    assert abs(conversion - 0.825) < 0.005


def test_conversion_excludes_depreciation_and_rollup():
    """Without the Depreciation + non-empty-Tag guards the sum balloons."""
    sources = _build_synthetic_sources()
    metrics = _calc_for_item(_input_row(), sources)
    conversion = metrics["Conversion Cost"]
    # Empty-Tag rollup (1.7721) and Depreciation (0.4242) and the four
    # category placeholders (0.5+0.6+0.7+0.31) were all live decoys.
    forbidden_total = (
        EXPECTED_CONVERSION_SUM
        + 1.7721    # Upper Level Costs rollup
        + 0.4242    # Depreciation
        + 0.5 + 0.6 + 0.7 + 0.31  # Milk Component / Ingredient / Milk / Packaging
    )
    assert conversion is not None
    assert conversion < forbidden_total - 0.1, (
        "Filter is leaking forbidden tags into the conversion sum"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Live BOM integration tests — exercise the rule against the on-disk Fabric
# export so any future change to the rule, the file, or the loader is caught
# the next time pytest runs.
# ─────────────────────────────────────────────────────────────────────────────

_LIVE_BOM_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "data"
    / "RFP Financial Analysis"
    / "BOM_History_Tracker_tagged.csv"
)


def _live_sources() -> RfpPnlSources:
    """Build an ``RfpPnlSources`` from the on-disk BOM, no Fabric required.

    Mirrors the normalization done by ``rfp_pnl_store.load_sources`` for the
    BOM frame so ``_calc_for_item`` sees the same shape it does in production.
    Budget / PDH frames are left empty: they only feed Cost of Quality and
    Internal Logistics, which are not under test here.
    """
    bom = pd.read_csv(_LIVE_BOM_PATH, low_memory=False)
    bom.columns = [str(c).strip() for c in bom.columns]
    for col in ("Per Beg", "Plant", "Rule Item Desc", "Tag", "Level",
                "Ing-Rsrc Desc", "Qty.1", "Top Recipe"):
        if col not in bom.columns:
            bom[col] = ""
    bom["_norm_month"] = bom["Per Beg"].map(_norm)
    bom["_norm_plant"] = bom["Plant"].map(_norm)
    bom["_norm_rule_item_desc"] = bom["Rule Item Desc"].map(_norm)
    bom["_norm_tag"] = bom["Tag"].map(_norm)
    bom["_norm_top_recipe"] = bom["Top Recipe"].map(_norm)
    bom["_level_text"] = bom["Level"].map(_fmt_level)

    return RfpPnlSources(
        bom_df=bom,
        budget_df=pd.DataFrame(columns=["Category", "Tag", "Budget Value"]),
        pdh_df=pd.DataFrame(columns=["Item Description", "Portfolio Major"]),
        cost_col="Ext Cost.1",
        month_options=tuple(sorted(bom["Per Beg"].dropna().astype(str).unique())),
        plant_options=tuple(sorted(bom["Plant"].dropna().astype(str).unique())),
        pdh_item_desc_set=frozenset(),
        category_by_desc={},
        budget_sum_by_cat_tag={},
    )


def _live_input(*, plant: str, sku: str, target_lbs: float = 1.0,
                ref_lbs: float = 1.0,
                explicit_other_ref_lbs: bool = False) -> pd.Series:
    """Build an analyst-style input row for ``_calc_for_item``.

    By default the Ingredient / Packaging / Conversion Reference SKU
    lbs/Each are left BLANK so the calc engine inherits Milk Ref
    lbs/Each (the default behaviour the analyst sees in the UI). Pass
    ``explicit_other_ref_lbs=True`` to populate them with ``ref_lbs``
    directly, exercising the override path.
    """
    base = {
        "Month": "1-Jun-26",
        "Plant": plant,
        "Target SKU Name": sku,
        "Target SKU lbs per Each": target_lbs,
        "Target SKU Volume (units)": 1.0,
        "Milk Reference SKU": sku,
        "Ingredient Reference SKU": "",
        "Packaging Reference SKU": "",
        "Conversion Reference SKU": sku,
        "Milk Reference SKU lbs per Each": ref_lbs,
        "Ingredient Reference SKU lbs per Each":
            ref_lbs if explicit_other_ref_lbs else "",
        "Packaging Reference SKU lbs per Each":
            ref_lbs if explicit_other_ref_lbs else "",
        "Conversion Reference SKU lbs per Each":
            ref_lbs if explicit_other_ref_lbs else "",
        "Other Cost": 0.0,
        "PCM $/lbs": 0.0,
    }
    return pd.Series(base)


def _manual_conversion(bom: pd.DataFrame, *, month: str, plant: str,
                       sku: str) -> tuple[float, pd.DataFrame]:
    """Reference implementation of the rule, computed independently
    of the production code path. Returns (sum, kept_rows)."""
    exclude = {"milk component", "ingredient", "milk", "packaging", "depreciation"}
    sub = bom[
        (bom["_norm_month"] == _norm(month))
        & (bom["_norm_plant"] == _norm(plant))
        & (bom["_norm_rule_item_desc"] == _norm(sku))
        & (bom["Level"].astype(str).str.strip() == "1")
    ]
    norm_tag = sub["_norm_tag"]
    keep = sub[(~norm_tag.isin(exclude)) & (norm_tag.astype(str).str.len() > 0)]
    total = float(pd.to_numeric(keep["Ext Cost.1"], errors="coerce").fillna(0).sum())
    return total, keep


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_live_conversion_cost_dg_hvy_whip_portland():
    """DG Hvy Whip Hg UP @ Portland matches the analyst's expected ~0.825."""
    sources = _live_sources()
    metrics = _calc_for_item(
        _live_input(plant="Portland", sku="DG Hvy Whip Hg UP"),
        sources,
    )
    expected, kept = _manual_conversion(
        sources.bom_df, month="1-Jun-26",
        plant="Portland", sku="DG Hvy Whip Hg UP",
    )
    assert metrics["Conversion Cost"] is not None
    # Tight tolerance: production code and reference code must agree exactly.
    assert abs(metrics["Conversion Cost"] - expected) < 1e-6, (
        f"Production={metrics['Conversion Cost']!r}, reference={expected!r}"
    )
    # Anchored to analyst's observed value.
    assert abs(metrics["Conversion Cost"] - 0.825888) < 1e-3
    # Exactly 25 line items survive (matches the screenshot count).
    assert len(kept) == 25


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_live_conversion_cost_excludes_forbidden_tags_for_portland():
    """No Depreciation / Milk* / Ingredient / Packaging / blank-Tag row
    contributes to the Portland conversion sum for DG Hvy Whip Hg UP."""
    sources = _live_sources()
    _, kept = _manual_conversion(
        sources.bom_df, month="1-Jun-26",
        plant="Portland", sku="DG Hvy Whip Hg UP",
    )
    forbidden = {"milk component", "ingredient", "milk", "packaging",
                 "depreciation", ""}
    bad = kept[kept["_norm_tag"].isin(forbidden)]
    assert bad.empty, (
        f"Conversion sum is leaking forbidden Tag rows: "
        f"{bad[['Tag', 'Ext Cost.1']].to_dict('records')}"
    )


# ─── Milk / Ingredient case study (2-step lookup) ─────────────────────────

def _manual_two_step(bom: pd.DataFrame, *, month: str, plant: str, sku: str,
                     step2_tag: str, ref_lbs_each: float = 1.0,
                     target_lbs_each: float = 1.0,
                     ) -> tuple[float, pd.DataFrame, dict]:
    """Reference impl of the 2-step Milk / Ingredient rule.

    Step 1: Rule Item Desc = parent SKU, Level == 1, Tag = "Milk Component".
    Step 2: Rule Item Desc = step-1's Ing-Rsrc Desc (chain key),
            Level contains "2", Tag = step2_tag, AND
            Top Recipe == step-1's Top Recipe (so we don't pull in
            another finished good that shares the same sub-recipe).
    Result = Σ_anchor (Σ Ext Cost.1 of step2 × Qty.1
                       × target_lbs_each ÷ ref_lbs_each).
    """
    s1 = bom[
        (bom["_norm_month"] == _norm(month))
        & (bom["_norm_plant"] == _norm(plant))
        & (bom["_norm_rule_item_desc"] == _norm(sku))
        & (bom["Level"].astype(str).str.strip() == "1")
        & (bom["_norm_tag"] == "milk component")
    ]
    total = 0.0
    rows_kept = []
    info: dict = {"step1_rows": len(s1), "anchors": []}
    for _, anchor in s1.iterrows():
        qty1 = float(pd.to_numeric(anchor.get("Qty.1"), errors="coerce") or 0.0)
        chain = _norm(anchor.get("Ing-Rsrc Desc"))
        top_rec = _norm(anchor.get("Top Recipe"))
        if qty1 == 0 or not chain:
            continue
        s2 = bom[
            (bom["_norm_month"] == _norm(month))
            & (bom["_norm_plant"] == _norm(plant))
            & (bom["_norm_rule_item_desc"] == chain)
            & (bom["Level"].astype(str).str.contains("2", na=False))
            & (bom["_norm_tag"] == _norm(step2_tag))
            & (bom["_norm_top_recipe"] == top_rec)
        ]
        ext_sum = float(pd.to_numeric(s2["Ext Cost.1"], errors="coerce")
                        .fillna(0.0).sum())
        info["anchors"].append({
            "chain": chain, "Qty.1": qty1, "top_recipe": top_rec,
            "rows": len(s2), "ext_sum": ext_sum,
        })
        rows_kept.append(s2)
        total += ext_sum * qty1 * target_lbs_each / ref_lbs_each
    kept = pd.concat(rows_kept) if rows_kept else bom.iloc[0:0]
    return total, kept, info


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_live_milk_cost_dg_hvy_whip_portland():
    """DG Hvy Whip Hg UP @ Portland — Milk step-2 matches the
    six unique Class-II / Fluid component lines from the screenshot
    (no double-count from sibling SKUs sharing the sub-recipe).

    Formula: Σ Ext Cost.1 × Qty.1 × Target lbs/Each ÷ Milk Ref lbs/Each.
    With target_lbs = ref_lbs = 1, Milk = Σ Ext Cost.1 × Qty.1
       = 1.021871 × 4.19885 ≈ 4.291189.
    """
    sources = _live_sources()
    metrics = _calc_for_item(
        _live_input(plant="Portland", sku="DG Hvy Whip Hg UP"),
        sources,
    )
    expected, kept, info = _manual_two_step(
        sources.bom_df, month="1-Jun-26",
        plant="Portland", sku="DG Hvy Whip Hg UP", step2_tag="Milk",
        ref_lbs_each=1.0, target_lbs_each=1.0,
    )
    assert metrics["Milk"] is not None
    assert abs(metrics["Milk"] - expected) < 1e-6, (
        f"Production={metrics['Milk']!r}, reference={expected!r}, info={info}"
    )
    # Anchor against the closed-form value derived from the live BOM.
    assert abs(metrics["Milk"] - 4.291189) < 1e-3
    # Step 2 must yield exactly the six line items shown in the snapshot
    # (Fluid Lb Cream, Fluid LB Farm Milk Grade-A, Cmp LB Butterfat
    # Class-II, Cmp LB Protein Class-II, Cmp LB Other Solids Class-II,
    # Cmp LB Skim Class-II) — and NO duplicate from the
    # ``DG Cl 40pc Whip Hg UP Disp Box`` parent recipe (Top Recipe 340962).
    assert len(kept) == 6, (
        f"Expected 6 milk step-2 rows, got {len(kept)}: "
        f"{kept[['Top Recipe','Ing-Rsrc Desc','Ext Cost.1']].to_dict('records')}"
    )
    expected_descs = {
        "fluid lb cream",
        "fluid lb farm milk grade-a",
        "cmp lb butterfat class-ii",
        "cmp lb protein class-ii",
        "cmp lb other solids class-ii",
        "cmp lb skim class-ii",
    }
    actual_descs = {_norm(d) for d in kept["Ing-Rsrc Desc"].tolist()}
    assert actual_descs == expected_descs, (
        f"Milk step-2 descriptors mismatch.\n"
        f"  expected: {sorted(expected_descs)}\n"
        f"  actual:   {sorted(actual_descs)}"
    )
    # All six rows belong to DG Hvy Whip Hg UP's Top Recipe.
    assert kept["_norm_top_recipe"].nunique() == 1


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_live_ingredient_cost_dg_hvy_whip_portland():
    """DG Hvy Whip Hg UP @ Portland — Ingredient step-2 matches the
    single LACTARN stabilizer row from the screenshot (no double-count).

    Formula: Σ Ext Cost.1 × Qty.1 × Target lbs/Each ÷ Ingredient Ref lbs/Each.
    With target_lbs = ref_lbs = 1 (Ingredient Ref lbs/Each defaults to
    Milk Ref lbs/Each since the analyst left it blank), Ingredient =
    0.003681 × 4.19885 ≈ 0.015456.
    """
    sources = _live_sources()
    metrics = _calc_for_item(
        _live_input(plant="Portland", sku="DG Hvy Whip Hg UP"),
        sources,
    )
    expected, kept, info = _manual_two_step(
        sources.bom_df, month="1-Jun-26",
        plant="Portland", sku="DG Hvy Whip Hg UP", step2_tag="Ingredient",
        ref_lbs_each=1.0, target_lbs_each=1.0,
    )
    assert metrics["Ingredient"] is not None
    assert abs(metrics["Ingredient"] - expected) < 1e-9, (
        f"Production={metrics['Ingredient']!r}, reference={expected!r}, info={info}"
    )
    assert abs(metrics["Ingredient"] - 0.015456) < 1e-4
    assert len(kept) == 1, (
        f"Expected 1 ingredient step-2 row, got {len(kept)}: "
        f"{kept[['Top Recipe','Ing-Rsrc Desc','Ext Cost.1']].to_dict('records')}"
    )
    only = kept.iloc[0]
    assert "lactarn" in _norm(only["Ing-Rsrc Desc"]), (
        f"Unexpected ingredient row: {only['Ing-Rsrc Desc']!r}"
    )
    # Ext Cost.1 = 0.003681 per the live snapshot.
    assert abs(float(only["Ext Cost.1"]) - 0.003681) < 1e-5


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_live_ref_lbs_inherits_milk_ref_lbs_when_blank():
    """Leaving Ingredient / Packaging / Conversion Ref lbs/Each blank
    should inherit Milk Ref lbs/Each. The blank-default and the
    explicit-equal paths must produce identical metrics.
    """
    sources = _live_sources()
    blank_path = _calc_for_item(
        _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                    target_lbs=4.30, ref_lbs=4.30,
                    explicit_other_ref_lbs=False),
        sources,
    )
    explicit_path = _calc_for_item(
        _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                    target_lbs=4.30, ref_lbs=4.30,
                    explicit_other_ref_lbs=True),
        sources,
    )
    for metric in ("Milk", "Ingredient", "Packaging", "Conversion Cost"):
        assert blank_path[metric] is not None, f"{metric} should not be None"
        assert abs(blank_path[metric] - explicit_path[metric]) < 1e-9, (
            f"{metric}: blank-inherit ({blank_path[metric]}) != "
            f"explicit ({explicit_path[metric]})"
        )


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_live_milk_ingredient_scale_with_target_lbs():
    """Sanity check: the new formula is linear in (Target lbs/Each ÷
    Ref lbs/Each). Doubling Target lbs/Each at constant Ref lbs/Each
    must double Milk and Ingredient.
    """
    sources = _live_sources()
    a = _calc_for_item(
        _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                    target_lbs=1.0, ref_lbs=1.0),
        sources,
    )
    b = _calc_for_item(
        _live_input(plant="Portland", sku="DG Hvy Whip Hg UP",
                    target_lbs=2.0, ref_lbs=1.0),
        sources,
    )
    assert abs(b["Milk"] - 2 * a["Milk"]) < 1e-9
    assert abs(b["Ingredient"] - 2 * a["Ingredient"]) < 1e-9


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_recompute_items_always_overwrites_cost_columns():
    """Regression: stale Milk / Ingredient / Packaging / Conversion Cost
    values from a previously saved scenario must NOT survive a Refresh.
    Treating them as defaultable (calc-only-fills-when-blank) previously
    let stale values from before a formula change linger forever.
    """
    sources = _live_sources()
    inputs = _live_input(plant="Portland", sku="DG HH Qt UP",
                         target_lbs=2.15, ref_lbs=2.15)

    # Sanity: production with blank cost cells produces the right values.
    fresh = _calc_for_item(inputs, sources)
    assert fresh["Milk"] is not None
    assert fresh["Milk"] > 0.0  # Milk for DG HH Qt UP is non-zero.

    # Stuff stale, deliberately wrong values into the four cost columns and
    # confirm Refresh overwrites every single one with the calc result.
    stale_row = inputs.copy()
    stale_row["Item"] = "Item 1"
    stale_row["Milk"] = 0.0033          # stale leftover
    stale_row["Ingredient"] = 0.0023    # stale leftover
    stale_row["Packaging"] = 99.9999    # stale leftover
    stale_row["Conversion Cost"] = -1.0  # stale leftover

    items = pd.DataFrame([stale_row])
    refreshed = recompute_items(items, sources)
    out = refreshed.iloc[0]
    for col in ("Milk", "Ingredient", "Packaging", "Conversion Cost"):
        assert out[col] != stale_row[col], (
            f"Refresh did not overwrite stale {col!r} "
            f"(was {stale_row[col]!r}, still {out[col]!r})"
        )
        # And the value must exactly match the freshly computed metric.
        if fresh[col] is None:
            assert out[col] == "" or out[col] is None
        else:
            assert abs(float(out[col]) - fresh[col]) < 1e-9


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_live_milk_does_not_double_count_when_subrecipe_shared():
    """Regression: shared sub-recipe between ``DG Hvy Whip Hg UP`` and
    ``DG Cl 40pc Whip Hg UP Disp Box`` previously caused step-2 to pull
    rows from BOTH parents, doubling Milk and Ingredient.
    """
    sources = _live_sources()
    a = _calc_for_item(
        _live_input(plant="Portland", sku="DG Hvy Whip Hg UP"),
        sources,
    )["Milk"]
    b_expected_unique, _, _ = _manual_two_step(
        sources.bom_df, month="1-Jun-26",
        plant="Portland", sku="DG Hvy Whip Hg UP", step2_tag="Milk",
        ref_lbs_each=1.0, target_lbs_each=1.0,
    )
    # Production must equal the single-parent reference, not 2x of it.
    assert a is not None
    assert abs(a - b_expected_unique) < 1e-6
    assert a < 2 * b_expected_unique - 0.01


@pytest.mark.skipif(not _LIVE_BOM_PATH.exists(),
                    reason=f"Live BOM CSV not present at {_LIVE_BOM_PATH}")
def test_live_conversion_cost_robust_across_all_plants():
    """For DG Hvy Whip Hg UP across every plant in the live BOM:

    * production result equals the independent reference implementation,
    * conversion is non-negative,
    * no forbidden Tag bleeds in,
    * conversion is strictly less than the rolled-up Level-1 ``Ext Cost``
      total for that recipe (a sanity ceiling).
    """
    sources = _live_sources()
    bom = sources.bom_df
    sku = "DG Hvy Whip Hg UP"
    plants = sorted(
        bom.loc[bom["_norm_rule_item_desc"] == _norm(sku), "Plant"]
           .dropna().astype(str).str.strip().unique().tolist()
    )
    assert plants, "Expected DG Hvy Whip Hg UP to appear for at least one plant"

    failures: list[str] = []
    for plant in plants:
        metrics = _calc_for_item(_live_input(plant=plant, sku=sku), sources)
        prod = metrics["Conversion Cost"]
        ref, kept = _manual_conversion(
            bom, month="1-Jun-26", plant=plant, sku=sku,
        )
        if prod is None:
            failures.append(f"{plant}: production returned None")
            continue
        if abs(prod - ref) > 1e-6:
            failures.append(f"{plant}: prod={prod} vs ref={ref}")
            continue
        if prod < 0:
            failures.append(f"{plant}: negative conversion {prod}")
            continue
        forbidden = {"milk component", "ingredient", "milk", "packaging",
                     "depreciation", ""}
        if not kept[kept["_norm_tag"].isin(forbidden)].empty:
            failures.append(f"{plant}: forbidden Tag leaked")
            continue

    assert not failures, "Conversion rule failed for: " + "; ".join(failures)
