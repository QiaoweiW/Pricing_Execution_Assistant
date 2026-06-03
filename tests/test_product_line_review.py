"""Unit tests for Product Line Review build logic (synthetic fixtures).

The tests cover four areas:

1. Pure math / date arithmetic — ``add_months``, ``trailing_months_end_at``,
   ``eligible_cy_begin_months``, ``cy/py/ny_full_year_months``.
2. Filter discovery + validation — ``list_pdh_filter_values*``,
   ``validate_common_filters``.
3. End-to-end table build — including PY L3 multipliers, customer rows,
   multi-select SFmt/Brand filtering, and R&O lookup.
4. Chart data builder — CY FY / NY FY series from
   ``qry_total_item_level_demand``.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from data_sources.demand_plan_comparison import (
    enrich_ibp_orders_df,
    resolve_ro_summary_path,
)
from data_sources.product_line_review import (
    FY_MONTH_LABELS,
    ProductLineReviewCommonFilters,
    ProductLineReviewSubFilters,
    add_months,
    aggregate_base_plan_for_plr,
    aggregate_orders_for_plr,
    aggregate_total_demand_for_plr,
    build_display_groups,
    build_full_year_chart_data,
    build_product_line_review_table,
    collect_chart_months,
    collect_ibp_months,
    eligible_cy_begin_months,
    list_pdh_filter_values,
    list_pdh_filter_values_for_pmaj,
    ny_full_year_months,
    prepare_ibp_base_plan_long,
    prepare_total_item_level_demand_long,
    resolve_filters,
    trailing_months_end_at,
    validate_common_filters,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _common(**overrides) -> ProductLineReviewCommonFilters:
    base = dict(
        cy_month=date(2026, 5, 1),
        cy_begin_month=date(2026, 4, 1),
        cy_ytg_start=date(2026, 5, 1),
        cy_ytg_end=date(2027, 3, 1),
    )
    base.update(overrides)
    return ProductLineReviewCommonFilters(**base)


def _filters(
    *,
    pmaj: str = "Butter",
    supply_formats: tuple[str, ...] = (),
    brands: tuple[str, ...] = ("Branded",),
    **common_overrides,
):
    return resolve_filters(
        _common(**common_overrides),
        pmaj,
        ProductLineReviewSubFilters(
            supply_formats=supply_formats,
            brands=brands,
        ),
    )


def _pdh(extra_rows: list[dict] | None = None) -> pd.DataFrame:
    rows = [{
        "Item No": "311042",
        "Item Description": "DG Test Butter",
        "Portfolio Major": "Butter",
        "Portfolio Minor": "Packaged Butter",
        "Supply Format": "Bundled Elgin Quarter",
    }]
    if extra_rows:
        rows.extend(extra_rows)
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 1) Pure math / date arithmetic
# ─────────────────────────────────────────────────────────────────────────────

def test_resolve_py_month_and_ytg():
    f = _filters()
    assert f.py_month == date(2025, 5, 1)
    assert f.common.cy_month == date(2026, 5, 1)
    assert f.py_ytg_start == date(2025, 5, 1)
    assert f.py_ytg_end == date(2026, 3, 1)


def test_add_months_negative_wrap():
    assert add_months(date(2026, 5, 1), -12) == date(2025, 5, 1)
    assert add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)


def test_eligible_cy_begin_months_window():
    """CY May 2026 → 12 months ending at May 2026 (Jun 2025 … May 2026)."""
    cy = date(2026, 5, 1)
    allowed = eligible_cy_begin_months(cy)
    assert len(allowed) == 12
    assert allowed[0] == date(2025, 6, 1)
    assert allowed[-1] == date(2026, 5, 1)
    assert date(2025, 5, 1) not in allowed


def test_trailing_months_anchor_cy_month():
    """PY L12 ends at CY Month = 5/1/2026 → starts 6/1/2025."""
    cy = date(2026, 5, 1)
    assert trailing_months_end_at(cy, 12) == {
        date(2025, 6, 1), date(2025, 7, 1), date(2025, 8, 1),
        date(2025, 9, 1), date(2025, 10, 1), date(2025, 11, 1),
        date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1),
        date(2026, 3, 1), date(2026, 4, 1), date(2026, 5, 1),
    }
    assert trailing_months_end_at(cy, 6) == {
        date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1),
        date(2026, 3, 1), date(2026, 4, 1), date(2026, 5, 1),
    }
    assert trailing_months_end_at(cy, 3) == {
        date(2026, 3, 1), date(2026, 4, 1), date(2026, 5, 1),
    }


def test_ny_full_year_window():
    """NY FY = 12 months immediately AFTER the CY FY window."""
    months = sorted(ny_full_year_months(date(2026, 4, 1)))
    assert months[0] == date(2027, 4, 1)
    assert months[-1] == date(2028, 3, 1)
    assert len(months) == 12


def test_collect_ibp_months_includes_run_rate_and_fy():
    f = _filters()
    months = collect_ibp_months(f)
    assert date(2026, 5, 1) in months
    assert date(2025, 5, 1) in months  # derived PY month
    assert date(2025, 6, 1) in months  # PY L12 start
    assert date(2025, 4, 1) in months  # PY FY start (CY begin − 12)


def test_collect_chart_months_covers_both_years():
    months = collect_chart_months(_common())
    assert date(2026, 4, 1) in months   # CY FY start
    assert date(2027, 3, 1) in months   # CY FY end
    assert date(2027, 4, 1) in months   # NY FY start
    assert date(2028, 3, 1) in months   # NY FY end


# ─────────────────────────────────────────────────────────────────────────────
# 2) Filter discovery + validation
# ─────────────────────────────────────────────────────────────────────────────

def test_list_pdh_filter_values():
    fv = list_pdh_filter_values(_pdh())
    assert "Butter" in fv["portfolio_major"]
    assert "Bundled Elgin Quarter" in fv["supply_format"]
    assert "Branded" in fv["brand"]


def test_list_pdh_filter_values_for_pmaj_cascades():
    """Only the formats / brands for the chosen PM appear in the cascade."""
    pdh = _pdh(extra_rows=[{
        "Item No": "999999",
        "Item Description": "PRIVATE Sour Cream",
        "Portfolio Major": "Cultured",
        "Portfolio Minor": "Cup",
        "Supply Format": "16 oz Cup",
    }])
    fv = list_pdh_filter_values_for_pmaj(pdh, "Butter")
    assert fv["supply_format"] == ["Bundled Elgin Quarter"]
    assert "16 oz Cup" not in fv["supply_format"]


def test_validate_cy_ytg_range_flags_inverted_window():
    errors = validate_common_filters(_common(
        cy_ytg_start=date(2027, 1, 1),
        cy_ytg_end=date(2026, 1, 1),
    ))
    assert errors


def test_validate_cy_begin_out_of_range():
    """CY Begin Month outside [CY−11, CY] must fail validation."""
    errors = validate_common_filters(_common(
        cy_begin_month=date(2025, 5, 1),
    ))
    assert any("CY Begin Month" in e for e in errors)


# ─────────────────────────────────────────────────────────────────────────────
# 3) Table build (end-to-end via the enrichment pipeline)
# ─────────────────────────────────────────────────────────────────────────────

def _enrich(orders_raw, base_raw, pdh):
    """Mimic the page's enrichment pipeline so build_table sees tidy frames."""
    return (
        enrich_ibp_orders_df(orders_raw, pdh),
        prepare_ibp_base_plan_long(base_raw, pdh),
    )


def test_build_table_current_month_and_run_rate():
    pdh = _pdh()
    orders_raw = pd.DataFrame([
        {"Item No": "311042", "Month": "2025-05-01", "Ordered Qty lbs": 1_000_000},
        {"Item No": "311042", "Month": "2026-03-01", "Ordered Qty lbs": 500_000},
        {"Item No": "311042", "Month": "2026-04-01", "Ordered Qty lbs": 500_000},
        {"Item No": "311042", "Month": "2026-05-01", "Ordered Qty lbs": 500_000},
    ])
    base_raw = pd.DataFrame([{
        "Start of Month": "2026-05-01",
        "Portfolio": "Butter",
        "Product Format": "Bundled Elgin Quarter",
        "Brand Category": "Branded",
        "Item": "311042",
        "Plan To Name": "Costco",
        "Total": "2,000,000",
        "Cycle": "C1",
    }])
    ro_lookup = {("Total B2C", "Butter", "Bundled Elgin Quarter"): 0.5}

    orders, base = _enrich(orders_raw, base_raw, pdh)
    result = build_product_line_review_table(
        orders_enriched=orders, base_long=base,
        ro_current_plan_by_path=ro_lookup,
        filters=_filters(),
    )
    assert not result.table.empty

    branded = result.table.loc[
        result.table["Row Label"] == "Bundled Elgin Quarter"
    ].iloc[0]
    assert branded["cm_py"] == "1.0"
    assert branded["cm_cy"] == "2.0"
    assert branded["cm_pct"] == "100%"
    assert branded["rr_l3"] == "6.0"           # 1.5 Mlbs × 4
    assert branded["fy_ro"] == "0.5"
    assert branded["fy_total"] == "2.5"

    costco = result.table.loc[result.table["Row Label"] == "Costco"].iloc[0]
    assert costco["fy_ro"] == "–"
    assert costco["fy_total"] == "–"


def test_build_table_respects_multi_select_supply_format():
    """Supply Format multi-select narrows the universe; empty tuple = all."""
    pdh = _pdh(extra_rows=[{
        "Item No": "311043",
        "Item Description": "DG Test Butter B",
        "Portfolio Major": "Butter",
        "Portfolio Minor": "Packaged Butter",
        "Supply Format": "Print",
    }])
    orders_raw = pd.DataFrame([
        {"Item No": "311042", "Month": "2025-05-01", "Ordered Qty lbs": 1_000_000},
        {"Item No": "311043", "Month": "2025-05-01", "Ordered Qty lbs": 9_000_000},
    ])
    base_raw = pd.DataFrame([
        {"Start of Month": "2026-05-01", "Portfolio": "Butter",
         "Product Format": "Bundled Elgin Quarter", "Brand Category": "Branded",
         "Item": "311042", "Plan To Name": "A", "Total": "1000000", "Cycle": "C1"},
        {"Start of Month": "2026-05-01", "Portfolio": "Butter",
         "Product Format": "Print", "Brand Category": "Branded",
         "Item": "311043", "Plan To Name": "B", "Total": "1000000", "Cycle": "C1"},
    ])
    orders, base = _enrich(orders_raw, base_raw, pdh)

    # Narrow to ONLY the "Print" format — the other leaf must disappear.
    result = build_product_line_review_table(
        orders_enriched=orders, base_long=base,
        ro_current_plan_by_path={},
        filters=_filters(supply_formats=("Print",)),
    )
    labels = set(result.table["Row Label"])
    assert "Print" in labels
    assert "Bundled Elgin Quarter" not in labels


def test_customer_row_uses_plan_to_name():
    pdh = _pdh()
    orders_raw = pd.DataFrame([{
        "Item No": "311042", "Customer Name": "Costco",
        "Month": "2025-05-01", "Ordered Qty lbs": 2_000_000,
    }])
    base_raw = pd.DataFrame([{
        "Start of Month": "2026-05-01", "Portfolio": "Butter",
        "Product Format": "Bundled Elgin Quarter", "Brand Category": "Branded",
        "Item": "311042", "Plan To Name": "Costco",
        "Total": "1,000,000", "Cycle": "C1",
    }])
    orders, base = _enrich(orders_raw, base_raw, pdh)
    result = build_product_line_review_table(
        orders_enriched=orders, base_long=base,
        ro_current_plan_by_path={}, filters=_filters(),
    )
    costco = result.table.loc[result.table["Row Label"] == "Costco"].iloc[0]
    assert costco["cm_py"] == "2.0"
    assert costco["cm_cy"] == "1.0"


def test_resolve_ro_summary_path_butter_format():
    """Round-trip the path lookup used to attach R&O to a (PM, SFmt, Brand)."""
    path = resolve_ro_summary_path(
        pmaj="Butter", sfmt="Bundled Elgin Quarter",
        brand="Branded", pminor="Packaged Butter",
    )
    assert path == ("Total B2C", "Butter", "Bundled Elgin Quarter")


# ─────────────────────────────────────────────────────────────────────────────
# 4) Display labels (dynamic header substitution)
# ─────────────────────────────────────────────────────────────────────────────

def test_display_groups_inject_dates_and_stay_unique():
    """``Orders – {Mon YYYY}`` / ``Base Plan – {Mon YYYY}`` are dynamic; the
    full set of column labels must remain globally unique so ``st.dataframe``
    can apply per-row styling without colliding on duplicate headers."""
    f = _filters()  # CY=May 2026 → PY=May 2025
    groups = build_display_groups(f)
    labels = [label for _g, cols in groups for _k, label in cols]
    assert "Orders – May 2025" in labels
    assert "Base Plan – May 2026" in labels
    assert len(labels) == len(set(labels))


# ─────────────────────────────────────────────────────────────────────────────
# 5) Full-Year chart
# ─────────────────────────────────────────────────────────────────────────────

def test_chart_axis_uses_fiscal_year_month_order():
    """X-axis labels are Apr → Mar regardless of which month CY Begin falls on."""
    assert FY_MONTH_LABELS == (
        "Apr", "May", "Jun", "Jul", "Aug", "Sep",
        "Oct", "Nov", "Dec", "Jan", "Feb", "Mar",
    )


def test_chart_sums_demand_pounds_across_forecast_types():
    """One row per (item, month, Forecast Type) → series is the SUM."""
    pdh = _pdh()
    qry_df = pd.DataFrame([
        {"Item": "311042", "Start of Month": "2026-04-01",
         "Forecast Type": "Base Plan", "Demand Plan Pounds": "1000"},
        {"Item": "311042", "Start of Month": "2026-04-01",
         "Forecast Type": "R&O",       "Demand Plan Pounds": "500"},
        {"Item": "311042", "Start of Month": "2027-04-01",
         "Forecast Type": "Base Plan", "Demand Plan Pounds": "200"},
    ])
    total_long = prepare_total_item_level_demand_long(qry_df, pdh)
    data = build_full_year_chart_data(
        total_demand_long=total_long,
        portfolio_major="Butter",
        sub=ProductLineReviewSubFilters(),
        cy_begin_month=date(2026, 4, 1),
    )
    assert len(data.series) == 2
    cy_series = data.series[0]
    assert cy_series.label == "FY 2027"
    # FY position 1 = Apr (CY FY starts at Apr 2026) → 1000 + 500 = 1500.
    assert cy_series.values_lbs[0] == 1500.0
    ny_series = data.series[1]
    assert ny_series.label == "FY 2028"
    assert ny_series.values_lbs[0] == 200.0


def test_chart_applies_sub_filters():
    """Multi-select Brand filter restricts the items contributing to the chart."""
    pdh = _pdh(extra_rows=[{
        "Item No": "999999",
        "Item Description": "PRIVATE Test Butter",
        "Portfolio Major": "Butter",
        "Portfolio Minor": "Packaged Butter",
        "Supply Format": "Bundled Elgin Quarter",
    }])
    qry_df = pd.DataFrame([
        {"Item": "311042", "Start of Month": "2026-04-01",
         "Forecast Type": "Base Plan", "Demand Plan Pounds": "1000"},
        {"Item": "999999", "Start of Month": "2026-04-01",
         "Forecast Type": "Base Plan", "Demand Plan Pounds": "9000"},
    ])
    total_long = prepare_total_item_level_demand_long(qry_df, pdh)
    data = build_full_year_chart_data(
        total_demand_long=total_long,
        portfolio_major="Butter",
        sub=ProductLineReviewSubFilters(brands=("Branded",)),
        cy_begin_month=date(2026, 4, 1),
    )
    # Only the Branded item should contribute (1000); Private is dropped.
    cy_series = data.series[0]
    assert cy_series.values_lbs[0] == 1000.0


def test_chart_consumes_pre_aggregated_frame():
    """build_full_year_chart_data must work directly on the aggregator output."""
    pdh = _pdh()
    qry_df = pd.DataFrame([
        {"Item": "311042", "Start of Month": "2026-04-01",
         "Forecast Type": "Base Plan", "Demand Plan Pounds": "100"},
        {"Item": "311042", "Start of Month": "2026-04-01",
         "Forecast Type": "R&O",       "Demand Plan Pounds": "50"},
    ])
    total_long = prepare_total_item_level_demand_long(qry_df, pdh)
    total_agg = aggregate_total_demand_for_plr(total_long)
    # Aggregation must collapse the two Forecast Types into one row.
    assert len(total_agg) == 1
    assert float(total_agg["pounds"].iloc[0]) == 150.0

    data = build_full_year_chart_data(
        total_demand_long=total_agg,
        portfolio_major="Butter",
        sub=ProductLineReviewSubFilters(),
        cy_begin_month=date(2026, 4, 1),
    )
    assert data.series[0].values_lbs[0] == 150.0


# ─────────────────────────────────────────────────────────────────────────────
# 6) Regression: index-mismatch bug in prepare_ibp_base_plan_long
# ─────────────────────────────────────────────────────────────────────────────

def test_base_plan_survives_cycle_filter_non_contiguous_index():
    """Regression for the "Base Plan = 0" bug.

    Before the fix, the cycle filter left ``work`` with a non-contiguous
    index, and the final ``pd.DataFrame({…})`` constructor aligned the
    PDH-merged columns against ``work``'s scattered Series indexes — so
    every ``pmaj/sfmt/brand`` cell became NaN and downstream sums
    collapsed to 0.  This test seeds an old (C1) and new (C2) cycle so
    the filter actually drops rows, then asserts the kept rows still
    carry their dims after the prep.
    """
    pdh = _pdh()
    # 4 rows, 2 cycles → after cycle filter (C2 wins) we keep rows at
    # indexes 1, 3 (non-contiguous before reset_index).
    base_raw = pd.DataFrame([
        {"Start of Month": "2026-05-01", "Portfolio": "Butter",
         "Product Format": "Bundled Elgin Quarter", "Brand Category": "Branded",
         "Item": "311042", "Plan To Name": "OldA",
         "Total": "1", "Cycle": "C1"},
        {"Start of Month": "2026-05-01", "Portfolio": "Butter",
         "Product Format": "Bundled Elgin Quarter", "Brand Category": "Branded",
         "Item": "311042", "Plan To Name": "Costco",
         "Total": "1000000", "Cycle": "C2"},
        {"Start of Month": "2026-05-01", "Portfolio": "Butter",
         "Product Format": "Bundled Elgin Quarter", "Brand Category": "Branded",
         "Item": "311042", "Plan To Name": "OldB",
         "Total": "1", "Cycle": "C1"},
        {"Start of Month": "2026-05-01", "Portfolio": "Butter",
         "Product Format": "Bundled Elgin Quarter", "Brand Category": "Branded",
         "Item": "311042", "Plan To Name": "Sysco",
         "Total": "2000000", "Total ": None, "Cycle": "C2"},
    ])
    base_long = prepare_ibp_base_plan_long(base_raw, pdh)

    # Cycle filter keeps only the two C2 rows.
    assert len(base_long) == 2
    # The dim columns MUST survive the index dance.
    assert set(base_long["pmaj"]) == {"Butter"}
    assert set(base_long["sfmt"]) == {"Bundled Elgin Quarter"}
    assert set(base_long["brand"]) == {"Branded"}
    assert set(base_long["customer"]) == {"Costco", "Sysco"}
    # And pounds add up to the C2-only total (3 M lbs).
    assert float(base_long["pounds"].sum()) == 3_000_000.0


def test_base_plan_pipeline_produces_non_zero_cm_cy():
    """End-to-end: a multi-cycle base CSV must yield non-zero cm_cy.

    Direct integration test for the user-visible symptom — `Base Plan –
    May 2026` (and every other base column) returns 0 when the prep
    silently drops dim attribution.  After the fix it should report the
    pre-filter total in millions.
    """
    pdh = _pdh()
    base_raw = pd.DataFrame([
        # C1 rows (will be dropped by cycle filter).
        {"Start of Month": "2026-05-01", "Portfolio": "Butter",
         "Product Format": "Bundled Elgin Quarter", "Brand Category": "Branded",
         "Item": "311042", "Plan To Name": "X",
         "Total": "99999999", "Cycle": "C1"},
        # C2 rows (kept; force non-contiguous indexes).
        {"Start of Month": "2026-05-01", "Portfolio": "Butter",
         "Product Format": "Bundled Elgin Quarter", "Brand Category": "Branded",
         "Item": "311042", "Plan To Name": "Costco",
         "Total": "2000000", "Cycle": "C2"},
        {"Start of Month": "2025-05-01", "Portfolio": "Butter",
         "Product Format": "Bundled Elgin Quarter", "Brand Category": "Branded",
         "Item": "311042", "Plan To Name": "X",
         "Total": "99999999", "Cycle": "C1"},
        {"Start of Month": "2026-05-01", "Portfolio": "Butter",
         "Product Format": "Bundled Elgin Quarter", "Brand Category": "Branded",
         "Item": "311042", "Plan To Name": "Sysco",
         "Total": "1000000", "Cycle": "C2"},
    ])
    orders, base = _enrich(pd.DataFrame(), base_raw, pdh)

    result = build_product_line_review_table(
        orders_enriched=orders, base_long=base,
        ro_current_plan_by_path={},
        filters=_filters(),
    )
    branded = result.table.loc[
        result.table["Row Label"] == "Bundled Elgin Quarter"
    ].iloc[0]
    # 3 M lbs total across the two C2 rows → 3.0 in display units.
    assert branded["cm_cy"] == "3.0"


# ─────────────────────────────────────────────────────────────────────────────
# 7) Pre-aggregation helpers
# ─────────────────────────────────────────────────────────────────────────────

def test_aggregate_base_plan_for_plr_collapses_duplicates():
    """Multiple rows for the same dim-tuple must collapse to a single sum."""
    df = pd.DataFrame({
        "month": [date(2026, 5, 1), date(2026, 5, 1)],
        "pounds": [10.0, 25.0],
        "pmaj": ["Butter", "Butter"],
        "sfmt": ["BEQ", "BEQ"],
        "pminor": ["Packaged Butter", "Packaged Butter"],
        "brand": ["Branded", "Branded"],
        "customer": ["Costco", "Costco"],
    })
    agg = aggregate_base_plan_for_plr(df)
    assert len(agg) == 1
    assert float(agg["pounds"].iloc[0]) == 35.0


def test_aggregate_orders_for_plr_preserves_customer_name():
    """Aggregator must use ``customer_name`` (IBP Orders convention)."""
    df = pd.DataFrame({
        "month": [date(2025, 5, 1)],
        "pounds": [100.0],
        "pmaj": ["Butter"], "sfmt": ["BEQ"], "pminor": ["Packaged Butter"],
        "brand": ["Branded"], "customer_name": ["Costco"],
    })
    agg = aggregate_orders_for_plr(df)
    assert "customer_name" in agg.columns
    assert agg["customer_name"].iloc[0] == "Costco"
