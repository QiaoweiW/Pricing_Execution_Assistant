"""Demand Planner Analytics page view.

Sections
--------
1. Source URLs                    (module-level constants)
2. Section renderers              (_render_instructions,
                                   _render_ibp_supporting_files,
                                   _render_ro_comparison,
                                   _render_demand_summary,
                                   _render_product_line_review)
3. Entry point                    (render)

Page layout
-----------
1. Page header + Instructions block.
2. ── divider ──
3. Foldable: "RO Comparison" — month pickers + nested foldable (collapsed)
   "RO Comparison & Drivers & Start Date Validation" (Customer Input
   upload, editor, drivers, Early-Start programs) + RO Summary Report.
4. ── divider ──
5. Foldable: "Demand Summary" — Demand Plan CSV previews + hierarchical
   Demand Pivot Summary + Demand Plan Comparison (drivers in nested
   foldable) + Base + RO chart.
6. ── divider ──
7. Foldable: "Product Line Review" — one table + chart per Portfolio
   Major (Bulk Fluid / Cheese / Milk Powders / Whey Powders last).
8. ── divider ──
9. Foldable: "🚚 Sales Distribution Tracker (RO Details)" — SharePoint
   Excel workbook in Office-Online read-mode; sits after PLR and before
   the BI dashboard so operational Fabric sections load first.
10. ── divider ──
11. Foldable: "Demand Planning BI Dashboard" — last on the page; embeds
    the SharePoint ``.pbix`` file so the user can interact with the live
    model without slowing the operational sections above.

Why every external resource gets its own foldable section
---------------------------------------------------------
The two external dashboards each render a heavy ``<iframe>`` that
forces an HTTP round trip when expanded.  Wrapping each in
``st.expander(expanded=False)`` lets the user load only the panels
they actually want, keeps initial page load fast, and avoids hammering
the SharePoint / Fabric services every time the page reruns on a
widget interaction.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from typing import Callable, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


logger = logging.getLogger(__name__)

from data_sources.demand_summary import (
    BudgetLookup,
    DemandSummaryError,
    DemandSummarySnapshot,
    FORECAST_BASE_PLAN,
    FORECAST_R_AND_O,
    MonthlyBudgetLookup,
    TOTAL_BUDGET_COLUMN_LABEL,
    TOTAL_COLUMN_LABEL,
    build_budget_lookup,
    build_monthly_budget_lookup,
    fetch_mgmt_plan_full,
    fetch_mgmt_plan_history_tracker,
    fetch_pdh,
    fetch_raw_bytes as fetch_demand_summary_raw_bytes,
    save_demand_plan_comparison,
    fetch_static_budget_base,
    fetch_static_budget_monthly,
    fetch_static_budget_ro,
    fetch_total_item_level_demand,
    mgmt_plan_full_blob_path,
    pivot_for_download,
    total_item_level_demand_blob_path,
)
from data_sources.demand_plan_comparison import (
    ComparisonFilters,
    ComparisonResult,
    DISPLAY_LABELS as DPC_DISPLAY_LABELS,
    DISPLAY_ORDER as DPC_DISPLAY_ORDER,
    COLS_HIDDEN_BY_DEFAULT as DPC_COLS_HIDDEN_BY_DEFAULT,
    PERCENT_COLS as DPC_PERCENT_COLS,
    fetch_fy27_budget_by_row_id,
    fy27_budget_blob_path,
    COL_LABEL as DPC_COL_LABEL,
    COL_LAST_PLAN as DPC_COL_LAST_PLAN,
    COL_CURRENT_PLAN as DPC_COL_CURRENT_PLAN,
    COL_CURRENT_PLAN_RO as DPC_COL_CURRENT_PLAN_RO,
    COL_PM_ACTUAL as DPC_COL_PM_ACTUAL,
    COL_BASE_PLAN as DPC_COL_BASE_PLAN,
    COL_R_AND_O as DPC_COL_R_AND_O,
    COL_CURRENT_PLAN_BASE as DPC_COL_CURRENT_PLAN_BASE,
    COL_TOTAL_ACTUALS as DPC_COL_TOTAL_ACTUALS,
    COL_PY_ACTUAL as DPC_COL_PY_ACTUAL,
    COL_O_PCT as DPC_COL_O_PCT,
    COL_BUDGET as DPC_COL_BUDGET,
    COL_PCT as DPC_COL_PCT,
    COL_ROW_ID as DPC_COL_ROW_ID,
    DRV_COL_PMAJ,
    DRV_COL_SFMT,
    DRV_COL_BRAND,
    DRV_BASE_PLAN_VALUE,
    DRV_PM_ACTUAL_VALUE,
    DRV_DRIVER_COLS,
    DRV_ITEM_COL_ITEM,
    DRV_ITEM_COL_DESC,
    DRV_ITEM_COL_BRAND,
    DRV_ITEM_COL_CUSTOMER,
    DRV_ITEM_COL_CUSTOMER_ID,
    DRV_ITEM_COL_DELTA,
    DriverTableResult,
    PMAF_COL_PRIOR_PLAN,
    PMAF_COL_ORDERED,
    PMAF_COL_SHIPPED,
    PMAF_COL_ORDERED_DIFF,
    PMAF_COL_SHIPPED_DIFF,
    PMAF_COL_ORDERED_PCT,
    PMAF_COL_SHIPPED_PCT,
    EnrichedSources,
    ComparisonKpis,
    build_base_plan_driver_table,
    build_comparison_kpis,
    build_comparison_not_captured,
    build_demand_plan_comparison,
    build_enriched_sources,
    build_item_dim_frame_cascade,
    ComparisonNotCaptured,
    DIAG_COL_LBS,
    DIAG_COL_MLBS,
    DIAG_COL_PMAJ,
    build_prior_month_actual_vs_fcst_table,
    build_prior_month_shipment_diagnostic,
    build_pm_actual_driver_table,
    build_business_health,
    build_business_health_sku,
    build_sku_cycle_comparison,
    BusinessHealthResult,
    BH_COL_CATEGORY,
    BH_COL_FLAG,
    BH_DISPLAY_ORDER,
    BH_PERCENT_COLS,
    BH_LEVEL_LABELS,
    BH_YOY_LABELS,
    BH_FLAG_RISING,
    BH_FLAG_FALLING,
    comparison_to_csv_bytes,
    compute_demand_driver_items,
    driver_table_to_csv_bytes,
    enrich_ibp_orders_df,
    enrich_ibp_shipments_df,
    list_driver_buckets_for_group,
    fetch_ro_summary_total_delta_by_path,
    list_tracker_cycles,
    list_comparison_combos,
    build_forecast_bias_table,
    build_forecast_bias_corp_sku_drivers,
    BIAS_COL_AVG,
    BIAS_COL_WMAPE,
    BIAS_COL_FVA,
    BIAS_COL_VOLUME,
    BIAS_COL_IMPACT,
    BIAS_COL_FLAG_DIR,
    BIAS_COL_FLAG_SEV,
    BIAS_COL_FLAG_FVA,
    BIAS_FLAG_PRIORITY,
    BIAS_FLAG_MONITOR,
    BIAS_FVA_BELOW,
    list_tracker_months,
    tracker_has_dim_columns,
    validate_filters,
    # Shared month-window helpers (one definition, in the data layer).
    months_in_range,
    shift_year_back,
    last_n_months,
    # Demand MOM Summary (actuals-stitched-onto-forecast pivot).
    DemandMomFilters,
    DemandMomResult,
    DemandPlanComparisonError,
    SERIES_ACTUAL,
    MOM_ROW_PMAJ,
    MOM_ROW_FORECAST,
    MOM_ROW_SFMT,
    NC_COL_ITEM,
    build_demand_mom_pivot,
    list_mom_filter_values,
    validate_mom_filters,
)
from data_sources.ibp_official import (
    IBPOfficialSourceError,
    fetch_ibp_orders_slim_df,
    fetch_ibp_shipments_months,
    fetch_ibp_shipments_slim_df,
)
from data_sources.product_line_review import (
    BRAND_BRANDED,
    BRAND_PRIVATE,
    COL_INDENT,
    COL_IS_CUSTOMER,
    COL_ROW_LABEL,
    FullYearChartData,
    ProductLineReviewCommonFilters,
    ProductLineReviewFilters,
    ProductLineReviewResult,
    ProductLineReviewSubFilters,
    add_months,
    aggregate_base_plan_for_plr,
    aggregate_orders_for_plr,
    aggregate_total_demand_for_plr,
    build_display_groups,
    build_full_year_chart_data,
    build_product_line_review_table,
    collect_ibp_months_for_common,
    eligible_cy_begin_months,
    list_pdh_filter_values_for_pmaj,
    resolve_filters,
    validate_common_filters,
)
from data_sources.demand_item_customer import (
    DemandItemCustomerError,
    attach_corporate_group_to_orders,
    build_demand_order_item_customer,
    compute_cy_actual_months,
    fetch_demand_item_customer_detail,
    list_filter_values_for_pmaj_from_demand,
    list_filter_values_from_demand,
    prepare_demand_long_for_plr,
    save_demand_order_item_customer,
)
from data_sources.customer_dims import (
    CustomerDimsError,
    fetch_dp_dimcustomernames_df,
)
from data_sources.plan_lift import (
    COL_MONTH as PL_COL_MONTH,
    CORP_GROUP_UNMAPPED,
    DIM_UNKNOWN,
    IRI_FILTER_COLUMNS,
    IRIResult,
    PlanLiftBuildStats,
    PlanLiftError,
    SLICER_DIMS,
    YoYLiftResult,
    build_month_fiscal_labels,
    build_plan_lift_base,
    compute_iri_unit_lift,
    compute_yoy_lift,
    fetch_dimcalendar_df,
    fetch_factscurrentaps_slim_df,
    fetch_iri_df,
    iri_file_label,
    list_iri_files,
    list_iri_filter_options,
    list_minor_products,
    list_portfolios,
    list_slicer_options_for_portfolio,
    today_month_begin,
)
from data_sources.ship_to_sites import (
    ShipToSitesSourceError,
    fetch_dimshiptosites_df,
    fetch_dp_dimplantosites_df,
)
from data_sources.holistic_demand_plan_aps import (
    APS_OUTPUT_NAME,
    HolisticDemandPlanError,
    load_persisted_aps_plan,
)
from data_sources.aps_upload_pipeline import (
    ApsUploadError,
    CYCLES as APS_CYCLES,
    FISCAL_YEARS as APS_FISCAL_YEARS,
    FORECAST_APS_BASE_PLAN as APS_FCST_BASE_PLAN,
    FORECAST_R_AND_O as APS_FCST_R_AND_O,
    aps_history_path,
    build_corp_review,
    delete_history_slice,
    fetch_aps_history_df,
    generate_base_plan_from_upload,
    generate_ro_from_seed,
    list_aps_history_cycles,
    parse_corp_override_csv,
    patch_history_corp,
)
from data_sources.product_line_review import (
    cy_full_year_months as _plr_cy_full_year_months,
)
from data_sources.ro_comparison import (
    ANNUAL_OPP_CHANGE,
    ANNUAL_OPP_LE,
    ANNUAL_OPP_PRIOR,
    CUR_FISCAL_PROB_CHANGE,
    CUR_FISCAL_PROB_LE,
    CUR_FISCAL_PROB_PRIOR,
    PER_FORMAT_DELTA_COL,
    PER_FORMAT_DRIVER_COLS,
    PER_FORMAT_DRIVER_BLANK_LABEL,
    PER_FORMAT_FORMAT_COL,
    PER_FORMAT_TOTAL_LABEL,
    SUBTOTAL_COLUMNS,
    YEAR1_PROB_CHANGE,
    YEAR1_PROB_LE,
    YEAR1_PROB_PRIOR,
    AutoRegenResult,
    ComparisonWarnings,
    RoComparisonError,
    _recompute_derived_columns,
    build_ro_comparison,
    compute_driver_items,
    compute_per_format_summary,
    detect_history_change,
    fetch_dimitems_df,
    fetch_ro_history_df,
    fetch_ro_item_master_df,
    fetch_ro_item_master_raw_bytes,
    list_months,
    regenerate_comparison_output,
    ro_item_master_blob_path,
    list_pipeline_review_snapshots,
    save_pipeline_review_snapshot,
    save_ro_comparison_output,
)
from data_sources.ro_summary_report import (
    COL_DELTA_CHANGE as SR_COL_DELTA_CHANGE,
    COL_DELTA_EXIT as SR_COL_DELTA_EXIT,
    COL_DELTA_NEW as SR_COL_DELTA_NEW,
    COL_CURRENT_PLAN as SR_COL_CURRENT_PLAN,
    COL_INDENT as SR_COL_INDENT,
    COL_IS_SUBTOTAL as SR_COL_IS_SUBTOTAL,
    COL_LABEL as SR_COL_LABEL,
    COL_PRIOR_PLAN as SR_COL_PRIOR_PLAN,
    COL_ROW_ID as SR_COL_ROW_ID,
    COL_TOTAL_DELTA as SR_COL_TOTAL_DELTA,
    COL_Y1_CHANGE as SR_COL_Y1_CHANGE,
    COL_Y1_LATEST as SR_COL_Y1_LATEST,
    COL_Y1_PRIOR as SR_COL_Y1_PRIOR,
    RoSummaryReportError,
    build_summary_report,
    clear_comparison_output_cache,
    diag_dim_summary,
    drop_all_zero_rows,
    recompute_subtotals,
    save_ro_summary_report,
    summary_to_csv_bytes,
)
from data_sources import ro_pipeline_analytics as rpa
from data_sources import shipments_velocity as vel
from data_sources.ro_seed_pipeline import (
    PipelineResult,
    delete_history_rows_for_month,
    fetch_ro_seed_raw_bytes,
    ro_seed_blob_path,
    run_distribution_tracker_pipeline,
)
from data_sources.demand_plan_pipeline import (
    DemandPlanResult,
    backfill_plan_attribute_columns,
    run_demand_plan_pipeline,
)
from data_sources.fabric_lakehouse_io import LakehouseIOError
from utils import fabric_signin_widget
from utils.ui_helpers import apply_custom_css


# ── 1. Source URLs ────────────────────────────────────────────────────────────
#
# Kept as module-level constants — these are the canonical, share-link URLs
# the user pastes from SharePoint / OneDrive.  They are *not* secrets;
# access is gated by SharePoint's own permission model.

# SharePoint-hosted RFP Tracker workbook.  Pasted verbatim (an
# AccessDenied redirect that carries the real file in its Source= param and
# auto-redirects once the user is signed in to the tenant).
_RFP_TRACKER_URL = (
    "https://darigold1com.sharepoint.com/sites/ChannelsDevelopment/"
    "_layouts/15/AccessDenied.aspx?Source=https%3A%2F%2Fdarigold1com."
    "sharepoint.com%2F%3Ax%3A%2Fr%2Fsites%2FChannelsDevelopment%2FShared"
    "+Documents%2FGeneral%2F1-+Weekly+Update+and+RFP+Tracker%2FRFP+Tracker"
    ".xlsx%3Fd%3Dw3d0cc19e7a474dd0be606d7713a242b9%26csf%3D1%26web%3D1%26e"
    "%3DRc9urc%26OR%3DTEAMS-WEB.undefined_ns.rwc%26wdExp%3DTEAMS-TREATMENT"
    "%26CT%3D1783616217454%26web%3D1%26TeamsCID%3D7dde02c1-b4f7-49ef-838e-"
    "d6871a3e5d8a%26linkOpenTime%3D1783616217487&correlation=c79f25a2-d0dc-"
    "0000-df1c-a05bccb0b039&Type=item&name=76866e00-dfc5-46eb-a6b4-"
    "b570035af43a&listItemId=828&listItemUniqueId=3d0cc19e-7a47-4dd0-be60-"
    "6d7713a242b9&allowautoredirecttosource=true"
)

# SharePoint-hosted Power BI desktop file (.pbix) — Demand Planning data model.
_DEMAND_PLANNING_PBIX_URL = (
    "https://darigold1com.sharepoint.com/:u:/r/sites/BrandedPricing/"
    "Shared%20Documents/B2C%20Demand%20Planning/2.%20Areas/"
    "Latest%20Demand%20Plan%20v3/Demand%20Planning%20Data%20Model%20v3.pbix"
    "?csf=1&web=1&e=cOv634"
)

# SharePoint-hosted Excel workbook — Sales Distribution Tracker (RO Details).
_DISTRIBUTION_TRACKER_URL = (
    "https://darigold1com.sharepoint.com/:x:/r/sites/CategoryCMM/"
    "_layouts/15/Doc.aspx?sourcedoc=%7B327C9520-28F4-41E7-A08A-7FD616FABB99%7D"
    "&file=New%20Distribution%20Tracker%20Corporate%20Group.xlsx"
    "&fromShare=true&action=default&mobileredirect=true"
)

# ── 2. Section renderers ──────────────────────────────────────────────────────


def _render_instructions() -> None:
    """Render the static instructions block at the top of the page."""
    st.markdown("### 📋 Instructions")
    st.markdown("Enable real-time demand insights")


# IBP cadence + supporting workbooks / reports — SharePoint share links and
# Power BI report links surfaced as a quick-reference list above RO
# Comparison.  (label, url) pairs; each opens in a new tab (auth is gated by
# SharePoint / Power BI / Fabric themselves — these are not secrets).
_IBP_SUPPORTING_FILES: tuple[tuple[str, str], ...] = (
    (
        "IBP Monthly Checklist and Calendar.xlsx",
        "https://darigold1com.sharepoint.com/:x:/r/sites/"
        "IntegratedBusinessPlanning-DarigoldDataModel/_layouts/15/Doc.aspx?"
        "sourcedoc=%7B3180D0A0-F357-4A8D-9DEF-7108DE49D7A6%7D&"
        "file=IBP%20Monthly%20Checklist%20and%20Calendar.xlsx&"
        "action=default&mobileredirect=true&"
        "TeamsCID=067d1c73-9056-42f2-b270-1fb9aa258683",
    ),
    (
        "Baseline Plan Change Journal.xlsx",
        "https://darigold1com.sharepoint.com/sites/BrandedPricing/"
        "Shared%20Documents/B2C%20Demand%20Planning/"
        "Baseline%20Plan%20Change%20Journal.xlsx?web=1",
    ),
    (
        "Tiger Report",
        "https://app.fabric.microsoft.com/groups/"
        "2a12208d-127f-4f59-b062-3f44876388dc/reports/"
        "f2627a71-11c0-4cdc-85be-05780079c71b/88798e0648107555d421"
        "?experience=fabric-developer",
    ),
    (
        "IBP Demand Planning Report (Power BI)",
        "https://app.powerbi.com/groups/"
        "2a12208d-127f-4f59-b062-3f44876388dc/reports/"
        "3068bf60-96eb-493f-8610-630c725940d7/6f2c04f0ad6811196392"
        "?experience=power-bi&clientSideAuth=0",
    ),
    (
        "SLT Dashboard",
        "https://app.fabric.microsoft.com/groups/me/reports/"
        "7d03fb42-73c9-48c3-8ebf-b2dffceed69d/8e711805f364f2606729"
        "?ctid=c9a55ced-3b88-408c-ab99-8db8b9b90286&experience=power-bi",
    ),
    ("RFP tracker", _RFP_TRACKER_URL),
    ("Demand Planning BI Dashboard", _DEMAND_PLANNING_PBIX_URL),
    ("Sales Distribution Tracker", _DISTRIBUTION_TRACKER_URL),
)


def _render_ibp_supporting_files() -> None:
    """Render the 'IBP Cadence and Supporting files' link list.

    A collapsed expander above RO Comparison listing the IBP cadence /
    baseline workbooks as clickable SharePoint links.
    """
    with st.expander("📅 IBP Cadence and Supporting files", expanded=False):
        for label, url in _IBP_SUPPORTING_FILES:
            st.markdown(f"- [{label}]({url})")


# ── RO Comparison ────────────────────────────────────────────────────────────
#
# Section flow
# ------------
# 1. Upload control                — drops a local "Customer Input" CSV into
#                                    Files/RO Tracking/Append_New_History/.
# 2. Load RO_History + dp_dimitems — Fabric reads, both cached by the
#                                    ro_comparison module.  Cache keys
#                                    are ETag-driven, so a fresh upstream
#                                    publish auto-invalidates on the
#                                    next render — no manual Refresh
#                                    button required (one was removed
#                                    in May 2026 once the ETag cache
#                                    key replaced the TTL-only flow).
# 3. Filters                       — ONE "🔍 Filters" section holding the
#                                    Prior + LE month pickers (sourced from the
#                                    distinct RO_History "Month" values; they
#                                    drive the build) plus the per-field
#                                    multiselects beneath them.
# 4. Auto-regen                    — when the RO_History ETag advances,
#                                    `RO_Comparison_Output.csv` is silently
#                                    regenerated + republished to Fabric;
#                                    every downstream section (Early-Start
#                                    Programs, Summary Report) reads from
#                                    that same in-memory frame so the
#                                    cascade is automatic.
# 5. Editable table + Subtotal + per-Format drivers
#                                  — wrapped in a single @st.fragment for
#                                    sub-second edit / Save interactions.  The
#                                    field filters (step 3) live above it and
#                                    are applied here from session_state.

# Session-state keys.  Centralised so we never typo a key elsewhere.
_SS_SUMMARY_DF      = "_ro_cmp_summary_df"
_SS_MONTHS_SIG      = "_ro_cmp_months_sig"
_SS_WARNINGS        = "_ro_cmp_warnings"
_SS_DIMITEMS_ERROR  = "_ro_cmp_dimitems_error"
# Tracks the (history-fingerprint, prior, le) signature of the LAST
# auto-regen we ran in this session — guards against re-running the
# same regen on every fragment rerun within a single page session.
_SS_AUTO_REGEN_SIG  = "_ro_cmp_auto_regen_sig"
# Banner payload for the most recent auto-regen — popped after one
# render so the planner sees it once, not on every interaction.
_SS_AUTO_REGEN_BANNER = "_ro_cmp_auto_regen_banner"
# Result of the last Distribution Tracker pipeline run (PipelineResult). Held in
# session_state so the RO Summary foldable survives reruns until the next run.
_SS_PIPELINE_RESULT = "_ro_pipeline_result"
# Results (list[MonthDeleteResult]) of the last month-cleanup action — held so
# the outcome survives the rerun the delete button triggers.
_SS_CLEANUP_RESULT = "_ro_cleanup_result"

# RO Summary Report (separate fragment).
#
# Sourcing model: the report is built from the IN-MEMORY comparison
# frame at ``_SS_SUMMARY_DF`` (NOT a fresh Fabric read).  This is what
# the planner asked for — picker changes propagate to the report on
# the same render, no extra Fabric round-trip per interaction.  The
# top-level "Refresh from Fabric" button still forces a full reload
# of every connector cache + auto-regen, which transitively refreshes
# the report's source frame.
_SS_SUMMARY_REPORT_DF        = "_ro_sr_df"
_SS_SUMMARY_REPORT_LOADED_AT = "_ro_sr_loaded_at"
_SS_SUMMARY_REPORT_WARNINGS  = "_ro_sr_warnings"
_SS_SUMMARY_REPORT_SHOW_ZERO = "_ro_sr_show_zero"
# Raw comparison snapshot used by the Diagnostic expander.  Mirrors
# ``_SS_SUMMARY_DF`` at the moment the report was last built so the
# diagnostic stays consistent with the table even if the planner
# subsequently edits comparison cells without saving.
_SS_SUMMARY_REPORT_RAW_DF    = "_ro_sr_raw_df"
# Signature of the comparison frame the report was last built from
# (= the comparison's ``_SS_MONTHS_SIG`` at build time).  Used by
# the report fragment to decide "do I need to rebuild?" — i.e.,
# rebuild when the picker changes; preserve planner edits when a
# widget INSIDE the report fragment reruns.
_SS_SUMMARY_REPORT_SIG       = "_ro_sr_sig"
_SS_SUMMARY_REPORT_TEMPLATE  = "_ro_sr_template"

# Filterable columns for the field-filter row above the editable table.
# ``Driver`` is one of {"New", "Exit", "Change", "No Change"} (see
# :func:`ro_comparison._compute_driver`) — letting the planner narrow
# the view to e.g. just "New" programs is a frequent use case when
# they want to audit what drove the FY27 Δ in either direction.
_RO_FILTER_COLUMNS: tuple[str, ...] = (
    "Format", "Customer", "Taxonomy", "Brand", "Item #", "Description",
    "Portfolio Major", "Portfolio Minor", "Supply Format", "Driver",
)

# Computed columns that must be read-only in the editor so a planner
# cannot drift them out of sync with the underlying inputs.
_RO_DISABLED_COLUMNS: tuple[str, ...] = (
    "Prior RO Key", "LE RO Key", "Driver",
    ANNUAL_OPP_CHANGE, YEAR1_PROB_CHANGE, CUR_FISCAL_PROB_CHANGE,
    "Prior Probability", "LE Probability", "Change Probability",
    "Change (Days)", "Existing SKUs", "Item #",
)


# ── Business Health (trailing-window order momentum) ─────────────────────────
# A read-only view above RO Comparison: pick a Prior Month, then see L3M / L6M /
# L12M order volume + YoY vs the same windows a year ago, plus a per-category
# table with a momentum Flag.  Reuses the comparison template + trailing-window
# helpers + the shared column-picker / tree helpers; nothing here writes Fabric.

# Curated hierarchy shown in the Business Health table (matches the executive
# summary set): Total B2C → majors, with ESL's carton subtotals and Cultured's
# Cottage Cheese / Sour Cream memos.  Row ids resolve against COMPARISON_TEMPLATE.
_BH_CURATED_ROWS: tuple[str, ...] = (
    "total_b2c", "esl", "esl_lc", "esl_sc", "aseptic", "cultured",
    "cult_cottage_cheese", "cult_sour_cream", "fresh_milk", "butter",
)
# Business-Health-only restyle: render Cultured's Cottage Cheese / Sour Cream
# like ESL's Large / Small Carton sub-rows (indent-2 subtotal look — no memo
# bullet, bold) instead of the template's memo styling.  Scoped to this section
# so the shared COMPARISON_TEMPLATE (used by RO / demand summary) is untouched.
_BH_SUBTOTAL_STYLE_ROWS: frozenset = frozenset(
    {"cult_cottage_cheese", "cult_sour_cream"})
_BH_MEMO_BULLET: str = "• "   # bullet prefix baked into memo labels
_BH_TABLE_COLS_KEY: str = "business_health_table_cols"
# Executive names for the trailing windows (chart + legend): the newest window
# reads as near-term "Momentum", the mid window as the "Trajectory", and the
# full year as the structural "Run-Rate".  The L-code is kept in parentheses so
# the chart still ties cleanly to the L3M / L6M / L12M table columns.
_BH_WINDOW_NICE: dict[str, str] = {
    "L3M": "Momentum", "L6M": "Trajectory", "L12M": "Run-Rate",
}


def _bh_window_display(code: str) -> str:
    """Catchy window name with its L-code, e.g. ``"Momentum (L3M)"``."""
    return f"{_BH_WINDOW_NICE.get(code, code)} ({code})"


# Chart font palette — dark gray, sized up from Plotly's light-gray defaults for
# executive legibility.
_BH_FONT_COLOR: str = "#3a3a3a"
# Green/red for signed YoY + Flag, layered onto the shared tree styling.
# Flag → lite-table colour class: Rising green, Falling red, Flat neutral.
_BH_FLAG_CSS: dict[str, str] = {
    BH_FLAG_RISING: "pos", BH_FLAG_FALLING: "neg",
}


def _render_business_health() -> None:
    """Business Health — trailing-window (L3M/L6M/L12M) order momentum on IBP Orders.

    Pick a **Prior Month**; the L3M / L6M / L12M windows END at it (inclusive)
    and each is compared YoY against the same span a year earlier.  A dual-axis
    chart (order volume bars + a dotted YoY line) sits above a per-category table
    with a momentum **Flag**.  Read-only; sourced entirely from IBP Orders.
    """
    with st.expander("🩺 Business Health", expanded=False):
        st.caption(
            "Trailing-window **order momentum** from **IBP Orders**.  Pick a "
            "**Prior Month**; the **L3M / L6M / L12M** windows end at that month "
            "(inclusive) and are compared **YoY** against the same span one year "
            "earlier (YAG).  The chart shows order **volume** (bars) and **YoY %** "
            "(dotted line); the table breaks it down by category with a momentum "
            "**Flag**."
        )
        if not fabric_signin_widget.is_fabric_signed_in():
            st.warning("🔒 **Microsoft Fabric is not connected.**  Sign in first.")
            return

        try:
            months = sorted(fetch_ibp_shipments_months())
        except IBPOfficialSourceError:
            months = []
        if not months:
            st.info("ℹ️ No IBP months available yet.")
            return
        prior_month = st.selectbox(
            "Prior Month (for PM Actual / Prior Month Forecast)",
            options=months, index=len(months) - 1, key="bh_prior_month",
            format_func=lambda d: d.strftime("%b %Y"),
            help="The L3M / L6M / L12M windows end at this month (inclusive).",
        )

        # Load exactly the 24 months the L12M current + year-ago windows need.
        cur12 = last_n_months(prior_month, 12)
        window = tuple(sorted(cur12 | {shift_year_back(m) for m in cur12}))
        try:
            with st.spinner("Loading IBP Orders + Shipments…"):
                pdh_df = _load_demand_comparison_pdh()
                orders_df, orders_warn = _load_demand_comparison_ibp_orders(months=window)
                orders_enriched = enrich_ibp_orders_df(orders_df, pdh_df)
                ship_df, ship_warn = _load_demand_comparison_ibp(months=window)
                shipments_enriched = enrich_ibp_shipments_df(ship_df, pdh_df)
        except (LakehouseIOError, ValueError) as exc:
            st.error(f"❌ Could not load IBP data for Business Health.\n\n{exc}")
            return
        for warn in (orders_warn, ship_warn):
            if warn:
                st.caption(f"⚠️ {warn}")

        # "Hide combinations" filter — applied to BOTH orders + shipments before
        # rolling up, so chart + table exclude the same combos.
        combo_exclude = _render_business_health_dim_filters(orders_enriched)
        orders_enriched = _bh_apply_combo_exclude(orders_enriched, combo_exclude)
        shipments_enriched = _bh_apply_combo_exclude(shipments_enriched, combo_exclude)
        result = build_business_health(orders_enriched, shipments_enriched, prior_month)

        _render_business_health_legend(result)
        _render_business_health_chart(result)
        st.caption(
            "**YoY %** = (orders in the window ÷ orders in the **same window one "
            "year earlier**) − 1.  e.g. **L3M YoY** compares "
            f"**{result.window_labels['L3M'][0]}** vs "
            f"**{result.window_labels['L3M'][1]}**.  The **Flag** reads momentum: "
            "**Rising** when recent (L3M) YoY is accelerating vs the trailing year "
            "(L12M), **Falling** when decelerating, else **Flat**."
        )
        _render_business_health_table(result)
        _render_business_health_sku_drilldown(orders_enriched, prior_month)


def _bh_order_combos(
    orders: Optional[pd.DataFrame],
) -> list[tuple[str, str, str]]:
    """Sorted distinct ``(pmaj, sfmt, brand)`` present in the enriched orders.

    The orders are PDH-enriched (``enrich_ibp_orders_df`` attaches pmaj / sfmt /
    the Branded-vs-Private ``brand`` rule), so these ARE the categorised combos
    in the loaded window — the same three dimensions the comparison sections'
    "Hide combinations" filter uses.
    """
    cols = ("pmaj", "sfmt", "brand")
    if orders is None or orders.empty or not set(cols).issubset(orders.columns):
        return []
    sub = orders[list(cols)]
    seen = {
        (str(p).strip(), str(s).strip(), str(b).strip())
        for p, s, b in zip(sub["pmaj"], sub["sfmt"], sub["brand"])
    }
    return sorted(c for c in seen if c[0] and c[1])


def _render_business_health_dim_filters(orders: Optional[pd.DataFrame]) -> frozenset:
    """One "Hide combinations — Portfolio Major · Supply Format · Brand" filter.

    Identical in design to the comparison / APS sections' filter: a single
    search-to-hide multiselect over concatenated ``PMaj · SFmt · Brand`` combos
    (empty = show all), sourced from the enriched orders.  Returns the set of
    excluded ``(pmaj, sfmt, brand)`` combos — the caller applies it to BOTH the
    orders and the shipments frames via :func:`_bh_apply_combo_exclude`.
    """
    labels_to_combo = {
        f"{_DPC_PMAJ_DISPLAY.get(p, p)} · {s} · {b}": (p, s, b)
        for p, s, b in _bh_order_combos(orders)
    }
    all_labels = sorted(labels_to_combo)
    if not all_labels:
        st.caption(
            "ℹ️ The Portfolio Major · Supply Format · Brand filter appears once "
            "IBP Orders load."
        )
        return frozenset()
    st.markdown(
        "**Hide combinations — Portfolio Major · Supply Format · Brand** "
        "_(empty = show all; search a name and pick it to remove — e.g. type "
        "**butter private** to drop those rows)_"
    )
    hidden = st.multiselect(
        "Hide combinations", options=all_labels, key="bh_combo_exclude",
        label_visibility="collapsed",
        placeholder="Search to hide, e.g. “butter private”…",
        help="Type to search; each pick is REMOVED from the chart + table.",
    )
    return frozenset(labels_to_combo[h] for h in hidden if h in labels_to_combo)


def _bh_apply_combo_exclude(
    frame: Optional[pd.DataFrame], combo_exclude: frozenset,
) -> Optional[pd.DataFrame]:
    """Drop rows whose ``(pmaj, sfmt, brand)`` is in *combo_exclude*.

    Same ``"␟"``-joined match key the comparison's ``_apply_dim_filter`` uses,
    so orders + shipments filter identically.
    """
    if (frame is None or frame.empty or not combo_exclude
            or not {"pmaj", "sfmt", "brand"}.issubset(frame.columns)):
        return frame
    combo = (
        frame["pmaj"].astype(str).str.strip() + "␟"
        + frame["sfmt"].astype(str).str.strip() + "␟"
        + frame["brand"].astype(str).str.strip()
    )
    drop = {f"{p}␟{s}␟{b}" for p, s, b in combo_exclude}
    return frame[~combo.isin(drop)]


def _render_business_health_legend(result: "BusinessHealthResult") -> None:
    """One-line legend mapping each catchy window name → exact month range."""
    wl = result.window_labels
    parts = " · ".join(
        f"**{_bh_window_display(w)}** {wl[w][0]}  _(vs {wl[w][1]})_"
        for w in ("L3M", "L6M", "L12M"))
    st.markdown(
        f"📅 **Windows** ending **{result.prior_month:%b %Y}** — {parts}")


# Business Health chart palette — Orders vs Shipments YoY lines kept visually
# distinct: a red dotted line for Order YoY, a teal dotted line for Shipment YoY.
_BH_CHART_STYLE: dict[str, dict] = {
    "Orders":    {"line": "#c0392b"},
    "Shipments": {"line": "#137d78"},
}


def _render_business_health_chart(result: "BusinessHealthResult") -> None:
    """YoY-focused chart: dotted Order-YoY + Shipment-YoY lines (no volume bars).

    Single axis (YoY %, zero-based).  X axis = L12M → L6M → L3M (widest →
    narrowest).  Values are the Total B2C row for each source, so the chart ties
    to the table's top row.
    """
    order = ["L12M", "L6M", "L3M"]                    # widest → narrowest
    labels = [_bh_window_display(w) for w in order]   # Run-Rate (L12M) …
    axis_title = dict(size=15, color=_BH_FONT_COLOR)
    tick_font = dict(size=14, color=_BH_FONT_COLOR)
    label_font = dict(size=15, color=_BH_FONT_COLOR)

    # Which YoY lines to draw — lets the planner remove a line (e.g. Shipments).
    shown = st.multiselect(
        "Show lines", options=["Orders", "Shipments"],
        default=["Orders", "Shipments"], key="bh_chart_lines",
        help="Untick a source to remove its YoY line from the chart.",
    )
    if not shown:
        st.info("Pick at least one line to show.")
        return

    fig = go.Figure()
    for src in shown:
        if src not in _BH_CHART_STYLE:      # guard stale/unknown selections
            continue
        series = result.chart_series.get(src, {})
        yoys = [
            (None if series.get(w, {}).get("yoy") is None
             else float(series[w]["yoy"]) * 100.0)
            for w in order
        ]
        color = _BH_CHART_STYLE[src]["line"]
        fig.add_scatter(
            x=labels, y=yoys, name=f"{src} YoY %", mode="lines+markers+text",
            line=dict(color=color, dash="dot", width=3),
            marker=dict(color=color, size=11, line=dict(color="white", width=1.5)),
            text=[("—" if y is None else f"{y:+.1f}%") for y in yoys],
            textposition="top center", textfont=dict(size=14, color=color),
            hovertemplate=f"{src} %{{x}} YoY: %{{y:+.1f}}%<extra></extra>",
        )
    fig.update_layout(
        height=340, margin=dict(l=10, r=10, t=44, b=10),
        font=dict(color=_BH_FONT_COLOR, size=14),   # base font: dark gray, bigger
        xaxis=dict(tickfont=label_font),
        # Single YoY axis, zero-based so 0 is the reference line.
        yaxis=dict(title=dict(text="YoY %", font=axis_title), ticksuffix="%",
                   rangemode="tozero", zeroline=True, zerolinecolor="#9ca3af",
                   showgrid=True, gridcolor="#eeeeee", tickfont=tick_font),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0,
                    font=dict(size=15, color=_BH_FONT_COLOR)),
        plot_bgcolor="white",
    )
    st.plotly_chart(fig, use_container_width=True, key="business_health_chart")


def _bh_plain_label(indented: str) -> str:
    """Strip the NBSP indent + memo bullet from a Category label → the raw name."""
    return str(indented).replace(" ", "").replace("•", "").strip()


def _bh_name_overrides(tdf: pd.DataFrame) -> dict[str, str]:
    """Read the session-only category renames → ``{row_id: new_name}``.

    Read-only (no widgets), so the table can apply the overrides even though the
    rename controls render BELOW it — the text boxes write ``bh_name_<row_id>``
    to session_state, which this reads on the next rerun.
    """
    overrides: dict[str, str] = {}
    for rid, cat in zip(tdf["_row_id"].tolist(), tdf[BH_COL_CATEGORY].tolist()):
        plain = _bh_plain_label(cat)
        val = str(st.session_state.get(f"bh_name_{rid}", "") or "").strip()
        if val and val != plain:
            overrides[rid] = val
    return overrides


def _render_business_health_rename(tdf: pd.DataFrame) -> None:
    """Session-only per-row rename controls, rendered BELOW the table.

    A collapsed expander with one text box per category (default = its current
    name).  Edits live in ``st.session_state`` for the session only — nothing is
    written to Fabric — and are picked up by :func:`_bh_name_overrides` on the
    next rerun, so the table above reflects the rename.
    """
    pairs = list(zip(tdf["_row_id"].tolist(),
                     [_bh_plain_label(c) for c in tdf[BH_COL_CATEGORY].tolist()]))
    with st.expander("✏️ Rename categories (this view only)", expanded=False):
        st.caption(
            "Rename any category for **this view only** — applies to the table "
            "above; not saved to Fabric."
        )
        cols = st.columns(2)
        for i, (rid, plain) in enumerate(pairs):
            with cols[i % 2]:
                st.text_input(plain, value=plain, key=f"bh_name_{rid}")


def _render_business_health_table(result: "BusinessHealthResult") -> None:
    """Per-category table: L3M/L6M/L12M + YAG + YoY (green/red) + Flag.

    Curated executive hierarchy (Total B2C → majors + ESL cartons + Cultured
    memos).  Level columns carry their exact month range as a 2nd header line;
    columns are hideable / reorderable via the shared picker.
    """
    table = result.table
    if table is None or table.empty:
        st.info("No Business Health rows to display.")
        return

    order_ix = {rid: i for i, rid in enumerate(_BH_CURATED_ROWS)}
    tdf = table[table["_row_id"].isin(order_ix)].copy()
    tdf = (tdf.assign(_ord=tdf["_row_id"].map(order_ix))
              .sort_values("_ord").drop(columns="_ord").reset_index(drop=True))

    # Restyle Cottage Cheese / Sour Cream to match ESL's Large / Small Carton
    # sub-rows (indent-2 subtotal look, no memo bullet) — Business-Health only.
    _restyle = tdf["_row_id"].isin(_BH_SUBTOTAL_STYLE_ROWS)
    if _restyle.any():
        tdf.loc[_restyle, "_is_memo"] = False
        tdf.loc[_restyle, "_is_subtotal"] = True
        tdf.loc[_restyle, BH_COL_CATEGORY] = (
            tdf.loc[_restyle, BH_COL_CATEGORY]
            .str.replace(_BH_MEMO_BULLET, "", regex=False))

    all_labels = list(BH_DISPLAY_ORDER)
    cols = _picked_columns(_BH_TABLE_COLS_KEY, all_labels)
    # Session-only category renames — read here (applied to the table), but the
    # edit controls render BELOW the table (see the call at the end).
    name_overrides = _bh_name_overrides(tdf)

    # 2nd header line: each level column's exact month range (drives clarity).
    wl = result.window_labels
    periods: dict[str, str] = {}
    for w, (cur_lbl, yag_lbl) in BH_LEVEL_LABELS.items():
        periods[cur_lbl] = f"({wl[w][0]})"
        periods[yag_lbl] = f"({wl[w][1]})"

    percent_set = set(BH_PERCENT_COLS)
    row_ids = tdf["_row_id"].tolist()
    subtotal_flags = tdf["_is_subtotal"].tolist()
    memo_flags = tdf["_is_memo"].tolist()
    indent_flags = tdf["_indent"].tolist()

    def _cell_html(col: str, val: object) -> str:
        if col == BH_COL_FLAG:
            flag = "" if val is None else str(val)
            cls = _BH_FLAG_CSS.get(flag, "")
            body = _esc_html(flag or "—")
            return f'<td class="{cls}">{body}</td>' if cls else f"<td>{body}</td>"
        if col in percent_set:
            return _summary_cell_html(
                "pct_signed", None if val is None or pd.isna(val) else float(val))
        return _summary_cell_html("m", val)

    def _th(col: str) -> str:
        period = periods.get(col)
        sub = f'<br><span class="per">{_esc_html(period)}</span>' if period else ""
        return f"<th>{_esc_html(col)}{sub}</th>"

    def _label_html(i: int) -> str:
        """Indented category label, applying a session rename if present."""
        original = str(tdf.iloc[i][BH_COL_CATEGORY])
        name = name_overrides.get(row_ids[i])
        if not name:
            return _esc_html(original)
        indent, is_memo = int(indent_flags[i]), bool(memo_flags[i])
        prefix = "  " * indent + ("• " if is_memo else "")
        return _esc_html(prefix + name)

    # Clean "lite" table (white, hairline rules, bold Total/section rows, green/
    # red YoY) — the same polished look as the comparison summary table.
    head_html = ('<th class="lbl">' + _esc_html(BH_COL_CATEGORY) + "</th>"
                 + "".join(_th(c) for c in cols))
    body_rows: list[str] = []
    for i in range(len(tdf)):
        cls = _dpc_cmp_row_class(
            row_id=row_ids[i], is_subtotal=bool(subtotal_flags[i]),
            is_memo=bool(memo_flags[i]), indent=int(indent_flags[i]))
        row = tdf.iloc[i]
        cells = (f'<td class="lbl">{_label_html(i)}</td>'
                 + "".join(_cell_html(c, row[c]) for c in cols))
        body_rows.append(f'<tr class="{cls}">{cells}</tr>')
    st.markdown(
        _DPC_LITE_CSS
        + '<div class="dpc-lite"><table><thead><tr>' + head_html
        + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table></div>",
        unsafe_allow_html=True,
    )
    _render_column_picker(
        _BH_TABLE_COLS_KEY, all_labels,
        help_suffix="  The Category column always shows.")
    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    st.download_button(
        "⬇️ Download Business Health (CSV)",
        data=tdf.drop(columns=[c for c in ("_row_id", "_indent", "_is_subtotal",
                                           "_is_memo") if c in tdf.columns]
                      ).to_csv(index=False).encode("utf-8"),
        file_name=f"business_health_{today}.csv", mime="text/csv",
        key="business_health_download", use_container_width=True)
    # Rename controls render BELOW the table (edits apply on the next rerun).
    _render_business_health_rename(tdf)


def _render_business_health_sku_drilldown(
    orders_enriched: Optional[pd.DataFrame], prior_month: date,
) -> None:
    """Foldable SKU-level build-up of the Business Health order table.

    Mirrors the APS / IBP cycle drill-in design: Portfolio Major / Portfolio
    Minor / Brand / Supply Format search filters (empty = all) over the SKUs
    that compose the roll-up rows, in a native ``st.dataframe`` (sort / hide /
    resize for free).  Values are the L12M / L6M / L3M **ordered lbs** (+ YAG +
    Order-YoY) in millions, sorted by L12M orders.
    """
    with st.expander("🔬 SKU-level order drill-in", expanded=False):
        st.caption(
            "The individual **SKUs** behind the Business Health rows above — "
            "L12M / L6M / L3M **ordered lbs** (+ year-ago and Order-YoY), in "
            "millions.  Filter by **Portfolio Major / Portfolio Minor / Brand / "
            "Supply Format** (empty = all); sorted by L12M orders."
        )
        cols = st.columns(4)
        dim_filter: dict[str, set] = {}
        for (key, label), slot in zip(
            (("pmaj", "Portfolio Major"), ("pminor", "Portfolio Minor"),
             ("brand", "Brand"), ("sfmt", "Supply Format")), cols):
            with slot:
                sel = st.multiselect(
                    label, options=_sku_dim_options(orders_enriched, key),
                    key=f"bh_sku_{key}", placeholder="All")
            if sel:
                dim_filter[key] = set(sel)

        sku_df = build_business_health_sku(
            orders_enriched, prior_month, dim_filter=dim_filter)
        if sku_df.empty:
            st.info("No SKUs match the current filters.")
            return
        st.caption(f"**{len(sku_df):,}** SKU(s)")

        # Millions with 2dp for the level columns; the three Order-YoY columns as
        # signed percentages (fractions → %).
        cc = st.column_config
        level_cols = [lbl for pair in BH_LEVEL_LABELS.values() for lbl in pair]
        colcfg: dict[str, object] = {
            "SKU": cc.TextColumn("SKU", width="large"),
            **{c: cc.NumberColumn(c, format="%.2f") for c in level_cols},
            **{c: cc.NumberColumn(c, format="percent") for c in BH_YOY_LABELS.values()},
        }
        st.dataframe(
            sku_df, use_container_width=True, hide_index=True,
            height=min(35 * (len(sku_df) + 1) + 38, 640), column_config=colcfg)
        today = pd.Timestamp.utcnow().strftime("%Y%m%d")
        st.download_button(
            "⬇️ Download SKU order drill-in (CSV)",
            data=sku_df.to_csv(index=False).encode("utf-8"),
            file_name=f"business_health_sku_{today}.csv", mime="text/csv",
            key="business_health_sku_download", use_container_width=True)


def _render_ro_comparison() -> None:
    """Render the RO Comparison section end-to-end inside a foldable expander.

    The section is a self-contained workflow: upload the latest
    Customer Input CSV → pick the two months to compare → review the
    enriched summary → edit any cells that need attention → publish
    back to ``RO_Reporting/RO_Comparison_Output.csv``.

    Wrapped in ``st.expander(expanded=True)`` to match the foldable
    pattern used by the other dashboards on this page while still
    showing the summary by default — collapse to hide everything,
    expand to see the full workflow.
    """
    with st.expander("🔁 RO Comparison", expanded=True):
        st.caption(
            "Compare two RO_History snapshots month-over-month, enrich with the "
            "dp_dimitems portfolio dimensions, edit any cells that need attention, "
            "and publish the result to the RO_Reporting folder in Microsoft Fabric."
        )

        _render_ro_item_master_download_button()
        _render_ro_seed_download_button()

        # 1b. "How to see your changes after upload" guidance.
        #     Replaces the old "🔄 Refresh from Fabric" button.  The
        #     auto-refresh path is now ETag-driven (see
        #     ``ro_comparison._compute_history_blob_signature``), so a
        #     manual button no longer adds any capability — but planners
        #     still need to know WHAT to do when they don't see their
        #     change.  Surface that flow explicitly here.
        _render_post_upload_guidance()
        st.markdown("")  # vertical breathing room before the comparison block.

        # 2. Fabric auth gate — match the pattern used by every other
        #    Fabric-backed page in this app (see
        #    pages/bid_asset_intelligence_view.py).  Without this gate
        #    the auto-fetch below would re-trigger the credential chain
        #    on every render, which on a broken auth chain blocks the
        #    page for tens of seconds and floods the log with retries.
        if not fabric_signin_widget.is_fabric_signed_in():
            st.warning(
                "🔒 **Microsoft Fabric is not connected.**\n\n"
                "Please visit **Home & Fabric Sign-in** in the sidebar to "
                "sign in.  Once signed in, return here — the RO Comparison "
                "summary will load automatically."
            )
            return

        # 3. Load both Fabric sources.  RO_History is required; dimitems
        #    is treated as a soft dependency so the section degrades
        #    gracefully rather than blocking the planner on a transient
        #    auth blip.
        try:
            with st.spinner("Reading RO_History_Tracker.csv from Microsoft Fabric…"):
                history_df = fetch_ro_history_df()
        except RoComparisonError as exc:
            st.error(f"❌ Could not load RO_History_Tracker.csv: {exc}")
            return

        try:
            with st.spinner("Reading dp_dimitems from Microsoft Fabric…"):
                dimitems_df = fetch_dimitems_df()
                dimitems_err: str | None = None
        except RoComparisonError as exc:
            dimitems_df = None
            dimitems_err = str(exc)

        # 2b. RO_Item_Master.csv — middle tier of the Portfolio cascade.
        #     Missing / empty blob is NOT fatal; cascade silently falls
        #     through to the RO_History "Format" fallback for SFmt and
        #     leaves Portfolio columns blank for items not in dp_dimitems.
        try:
            with st.spinner("Reading RO_Item_Master.csv from Microsoft Fabric…"):
                item_master_df = fetch_ro_item_master_df()
                item_master_err: str | None = None
        except RoComparisonError as exc:
            item_master_df = None
            item_master_err = str(exc)

        # 3. Filters section — Prior/LE month pickers + the per-field filters,
        #    grouped in ONE "🔍 Filters" expander.  The expander is used as a
        #    deferred container: the month pickers render first (they drive the
        #    comparison build below), then the field filters render into the
        #    SAME expander once the summary frame exists to supply their options
        #    (see step 5b).
        months = list_months(history_df)
        if len(months) < 2:
            st.warning(
                "RO_History_Tracker.csv has fewer than 2 distinct Month values "
                f"({len(months)} found) — need at least 2 to build a comparison."
            )
            return
        filters_exp = st.expander("🔍 Filters", expanded=True)
        with filters_exp:
            prior_month, le_month = _render_month_filters(months)

        if prior_month == le_month:
            st.info("Pick two different months to see a comparison.")
            return

        # 3b. Auto-regenerate `RO_Comparison_Output.csv` if RO_History
        #     has changed since the last save.  Per planner spec:
        #     silent overwrite, picker pair, planner edits intentionally
        #     dropped (they re-edit on top of the new baseline).  See
        #     ``_maybe_auto_regenerate_comparison_output`` for the
        #     two-phase implementation that keeps the picker
        #     interactive on the common no-op render.
        _maybe_auto_regenerate_comparison_output(
            history_df, dimitems_df, item_master_df, prior_month, le_month,
        )

        # 3c. Explicit, on-demand regenerate button.  Force-reads the latest
        #     RO_History from Fabric and rebuilds regardless of fingerprint
        #     state (the auto-regen above only fires on a detected change,
        #     and only if the cached read already surfaced fresh bytes).  It
        #     reruns after writing, so the refreshed source flows through the
        #     top-of-section fetch + ``_ensure_summary_in_session`` rebuild.
        _render_ro_comparison_generate_button(prior_month, le_month)

        # 4. Build (and cache in session_state) the comparison frame.
        _ensure_summary_in_session(
            history_df, dimitems_df, item_master_df,
            prior_month, le_month, dimitems_err, item_master_err,
        )
        warnings: ComparisonWarnings = st.session_state[_SS_WARNINGS]

        # 4b. Surface the auto-regen banner ONCE, post-build.  Sized
        #     after the warnings banner so the most recent action is
        #     visually closest to the table.
        _render_auto_regen_banner_once()

        # 5. Warnings.
        _render_warnings_banner(warnings)

        # 5b. Field filters — rendered into the SAME "🔍 Filters" expander as
        #     the month pickers, now that the summary frame exists to supply
        #     each multiselect's options.  Selections persist in session_state;
        #     the editor fragment applies them via `_apply_filters`.  These
        #     widgets live OUTSIDE the editor fragment, so a field-filter change
        #     is a full rerun — cheap, because the Fabric reads and the
        #     comparison build above are signature-guarded and short-circuit
        #     when nothing upstream changed.
        with filters_exp:
            st.divider()
            _render_field_filter_widgets(st.session_state[_SS_SUMMARY_DF])

        # 6-10. Customer Input upload, RO_Comparison_Output editor,
        #       per-Format drivers, and Early-Start programs — grouped in
        #       one foldable subsection (collapsed by default) so month
        #       pickers + warnings stay visible above.
        with st.expander(
            "📋 RO Comparison & Drivers & Start Date Validation",
            expanded=False,
        ):
            # Distribution_Tracker.csv upload — top of the validation
            # block so the ingest path sits next to the tables it feeds.
            _render_customer_input_uploader()
            st.markdown("")  # breathing room before the editor

            # Filters + editor + subtotal + per-Format summary + Save.
            # Wrapped in a single ``@st.fragment`` so a filter change /
            # cell edit / Save click re-runs ONLY this block — no Fabric
            # I/O, no comparison rebuild, no warnings banner re-render.
            _render_filtered_editor_fragment(prior_month, le_month)

        # 11. Pipeline at a Glance — headline metric tiles + urgency /
        #     probability charts over the same in-memory comparison frame
        #     (``_SS_SUMMARY_DF``), rendered ABOVE the RO Summary roll-up.
        st.markdown("---")
        _render_ro_pipeline_analytics_section()

        # 12. RO Summary Report — hierarchical roll-up of the
        #     **in-memory** comparison frame (``_SS_SUMMARY_DF``).
        #     Lives in its own ``@st.fragment`` so editing a leaf cell
        #     or hitting Save doesn't trigger any work above; on a
        #     month-picker change (full page rerun) the fragment
        #     rebuilds because its signature key drifts from the
        #     comparison's ``_SS_MONTHS_SIG``.  Tied to the in-memory
        #     frame on purpose: the planner asked for picker changes
        #     to update the report instantly without a Fabric round-
        #     trip.  The top-of-section "🔄 Refresh from Fabric"
        #     button is the escape hatch for re-reading the published
        #     CSV when needed.
        st.markdown("---")
        _render_summary_report_section()


# ── RO Comparison helpers ───────────────────────────────────────────────────

def _render_ro_item_master_download_button() -> None:
    """Render a red download button for ``RO_Item_Master.csv`` from Fabric."""
    if not fabric_signin_widget.is_fabric_signed_in():
        st.caption(
            "_Sign in via **Home & Fabric Sign-in** to download "
            "`RO_Item_Master.csv`._"
        )
        return

    blob_path = ro_item_master_blob_path()
    try:
        raw_bytes = fetch_ro_item_master_raw_bytes()
        item_master_df = fetch_ro_item_master_df()
        row_count = len(item_master_df) if item_master_df is not None else 0
    except RoComparisonError as exc:
        st.warning(
            f"⚠️ Could not prepare the download for `Files/{blob_path}`."
            f"\n\nDetails: {exc}"
        )
        return

    # Scope red styling to this download via a marker + sibling selector.
    st.markdown(
        '<span id="ro-item-master-dl-marker"></span>',
        unsafe_allow_html=True,
    )
    st.markdown(
        """
        <style>
        #ro-item-master-dl-marker ~ div [data-testid="stDownloadButton"] button {
            background-color: #d32f2f !important;
            color: #ffffff !important;
            border: 1px solid #b71c1c !important;
        }
        #ro-item-master-dl-marker ~ div [data-testid="stDownloadButton"] button:hover {
            background-color: #b71c1c !important;
            border-color: #8b0000 !important;
            color: #ffffff !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    st.download_button(
        label=(
            f"⬇️ Download RO_Item_Master.csv "
            f"({row_count:,} rows from Fabric)"
        ),
        data=raw_bytes,
        file_name=f"RO_Item_Master_{today}.csv",
        mime="text/csv",
        key="ro_cmp_dl_item_master",
        type="primary",
        help=(
            f"Downloads a byte-for-byte copy of `Files/{blob_path}` "
            "from Microsoft Fabric — no re-serialisation."
        ),
    )


def _render_ro_seed_download_button() -> None:
    """Download the current ``RO_Seed.csv`` — the input the Demand Plan ETL reads.

    Lets the planner verify the seed's currency before uploading a new Base Plan
    (the demand pipeline assumes RO_Seed is the right cycle; see decision #3).
    """
    if not fabric_signin_widget.is_fabric_signed_in():
        return  # the item-master button above already prompts to sign in.

    blob_path = ro_seed_blob_path()
    try:
        raw_bytes = fetch_ro_seed_raw_bytes()
    except LakehouseIOError as exc:
        st.caption(f"_RO_Seed.csv unavailable for download: {exc}_")
        return

    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    st.download_button(
        label="⬇️ Download RO_Seed.csv (current, from Fabric)",
        data=raw_bytes,
        file_name=f"RO_Seed_{today}.csv",
        mime="text/csv",
        key="ro_cmp_dl_ro_seed",
        help=(
            f"Byte-for-byte copy of `Files/{blob_path}` — the R&O source the "
            "Demand Plan pipeline expands into `tbl_ro_input`."
        ),
    )


def _render_customer_input_uploader() -> None:
    """Render the Distribution Tracker upload → run-pipeline → RO Summary block.

    On **Run & Save to Fabric** the entire former Fabric-notebook pipeline runs
    in-app (see :func:`run_distribution_tracker_pipeline`): merge the upload into
    ``Distribution_Tracker_History.csv`` → build ``RO_Seed.csv`` → expand + merge
    into ``RO_History_Tracker.csv``, then delete the staged source. The run
    report (with warnings surfaced prominently) renders in a foldable RO Summary
    directly beneath the button.

    Independence guarantee: this block is fully self-contained — the comparison
    table below renders whether or not anything has been uploaded.
    """
    st.markdown(
        "<h4 style='font-size:1.35rem; margin-top:0.25rem;'>"
        "📤 Upload &quot;Distribution_Tracker.csv&quot; to run the RO pipeline"
        "</h4>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Export the 'Customer Input' table as **Distribution_Tracker.csv** with a "
        "**Month** column set to the first day of the current month (e.g. "
        "2026-06-01). On **Run & Save to Fabric** the app merges it into the "
        "distribution history, rebuilds RO_Seed, and updates RO_History_Tracker — "
        "then the RO Comparison below refreshes automatically. No Fabric notebook "
        "needed."
    )
    uploaded = st.file_uploader(
        "Upload Distribution_Tracker.csv",
        type=["csv"],
        key="ro_cmp_customer_input_upload",
        label_visibility="collapsed",
    )

    # Fiscal year-end anchor (Analysis!$B$3) — drives the "Days in Year"
    # expansion maths. Defaults to the notebook's value; planner can override.
    c1, c2 = st.columns([1, 2])
    with c1:
        anchor = st.date_input(
            "Fiscal year-end anchor",
            value=date(2027, 3, 31),
            format="YYYY-MM-DD",
            key="ro_cmp_anchor_date",
            help="Analysis!$B$3 — the fiscal year-end used to compute 'Days in "
                 "Year'. Defaults to 3/31/2027; change it when the fiscal year rolls.",
        )

    run_clicked = st.button(
        "▶️ Run & Save to Fabric",
        key="ro_cmp_customer_input_save",
        type="primary",
        disabled=uploaded is None,
        help="Runs the full pipeline and writes Distribution_Tracker_History.csv, "
             "RO_Seed.csv and RO_History_Tracker.csv to Microsoft Fabric.",
    )

    if run_clicked and uploaded is not None:
        with st.spinner(
            "Running the RO pipeline (merge history → build RO_Seed → "
            "update RO_History_Tracker)…"
        ):
            result = run_distribution_tracker_pipeline(
                uploaded.getvalue(), anchor_date=anchor,
            )
        st.session_state[_SS_PIPELINE_RESULT] = result
        if result.ok:
            # The pipeline just wrote new history to Fabric. The comparison and
            # month pickers above were rendered BEFORE this write (so they still
            # show the pre-upload months), and the month selectboxes persist
            # their prior value by key (so they'd stay on the old "latest" even
            # after a refresh). Drop the cached history read AND reset the
            # picker keys, then rerun — so the just-absorbed month appears and
            # the LE picker defaults to it immediately.
            fetch_ro_history_df(force_refresh=True)
            st.session_state.pop("ro_cmp_prior_month", None)
            st.session_state.pop("ro_cmp_le_month", None)
            st.rerun()

    # RO Summary — persists across reruns until the next run.
    result: Optional[PipelineResult] = st.session_state.get(_SS_PIPELINE_RESULT)
    if result is not None:
        _render_ro_pipeline_summary(result)

    # Maintenance tool: delete a mislabeled month from the history files.
    _render_month_cleanup()


# Icons for each run-log level, used by the RO Summary renderer.
_LOG_LEVEL_ICON = {"info": "•", "success": "✅", "warning": "⚠️", "error": "❌"}


def _render_ro_pipeline_summary(result: PipelineResult) -> None:
    """Render the foldable 'RO Summary' for the last pipeline run.

    Warnings are pulled to the top and shown as a Streamlit warning box so they
    can't be missed; the full run log (every step) is available beneath the
    headline so the planner can audit exactly what changed.
    """
    title = "📊 RO Summary — last run " + ("✅ success" if result.ok else "❌ failed")
    with st.expander(title, expanded=True):
        # Headline outcome + instructions.
        if result.ok:
            st.success(
                "Pipeline completed. **Distribution_Tracker_History.csv**, "
                "**RO_Seed.csv** and **RO_History_Tracker.csv** were updated in "
                "Fabric. The RO Comparison below refreshes automatically on the "
                "next interaction (or change a month picker to force it now)."
            )
        else:
            st.error(
                "Pipeline did **not** complete — no files were written unless a "
                "specific write is listed in the log below. Fix the issue(s) and "
                "re-run. See the errors and warnings below."
            )

        # Errors first (only on failure), then warnings — both impossible to miss.
        for err in result.errors:
            st.error(f"❌ {err}")
        if result.warnings:
            st.warning(
                "**Please review these warnings:**\n\n"
                + "\n".join(f"- {w}" for w in result.warnings)
            )

        # Headline metrics (present once the relevant stages ran).
        if result.ok:
            months = ", ".join(result.snapshot_months) or "—"
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("RO_Seed rows", f"{result.ro_seed_rows:,}"
                      if result.ro_seed_rows is not None else "—")
            m2.metric("New RO Keys", f"{result.new_ro_keys:,}"
                      if result.new_ro_keys is not None else "—")
            m3.metric("RO_History rows", f"{result.ro_history_rows:,}"
                      if result.ro_history_rows is not None else "—")
            m4.metric("RO_Seed total Lbs./yr", f"{result.ro_seed_total_lbs:,.0f}"
                      if result.ro_seed_total_lbs is not None else "—")
            st.caption(f"Snapshot month(s) processed: **{months}**")

        # Full step-by-step run log (audit trail).
        with st.expander("Full run log", expanded=not result.ok):
            for entry in result.log:
                icon = _LOG_LEVEL_ICON.get(entry.level, "•")
                st.markdown(f"{icon} {entry.text}")


# Display label (shown in the multiselect) → DELETE_TARGETS key.
_CLEANUP_LABEL_TO_KEY = {
    "RO_History_Tracker.csv": "ro_history",
    "Distribution_Tracker_History.csv": "distribution_history",
}


def _render_month_cleanup() -> None:
    """Render the destructive 'delete a month' maintenance tool.

    Removes every row for a chosen calendar month from one or both history files
    — the recovery path for a mislabeled pipeline run. Guarded behind an
    explicit file selection AND a confirmation checkbox so it can't fire by
    accident, and collapsed by default so it stays out of the normal workflow.
    """
    with st.expander("🧹 Maintenance — delete a month's rows (careful)", expanded=False):
        st.caption(
            "Permanently removes **every row for the chosen month** from the "
            "selected Fabric file(s) — matched by calendar month, regardless of "
            "the stored date format. Use this to undo a mislabeled run. "
            "**This cannot be undone.**"
        )

        c1, c2 = st.columns([1, 2])
        with c1:
            cleanup_month = st.date_input(
                "Month to delete",
                value=date.today().replace(day=1),
                format="YYYY-MM-DD",
                key="ro_cleanup_month",
                help="Any row whose Month falls in this calendar month is removed.",
            )
        with c2:
            targets = st.multiselect(
                "File(s) to clean",
                options=list(_CLEANUP_LABEL_TO_KEY.keys()),
                key="ro_cleanup_targets",
                help="Pick one or both history files.",
            )

        confirm = st.checkbox(
            f"Yes — permanently delete all **{cleanup_month:%B %Y}** rows from "
            "the selected file(s).",
            key="ro_cleanup_confirm",
        )
        do_delete = st.button(
            "🗑️ Delete month",
            type="primary",
            disabled=(not targets or not confirm),
            key="ro_cleanup_btn",
            help="Enabled once you select at least one file and tick the confirmation.",
        )

        if do_delete:
            with st.spinner(f"Deleting {cleanup_month:%B %Y} rows from Fabric…"):
                st.session_state[_SS_CLEANUP_RESULT] = [
                    delete_history_rows_for_month(_CLEANUP_LABEL_TO_KEY[lbl], cleanup_month)
                    for lbl in targets
                ]

        # Results persist across the rerun the button triggers.
        results = st.session_state.get(_SS_CLEANUP_RESULT)
        if results:
            renderer = {"success": st.success, "warning": st.warning, "error": st.error}
            for r in results:
                renderer.get(r.level, st.info)(r.message)
            st.caption(
                "Re-read the section (or change a month picker) to see the "
                "comparison refreshed against the updated history."
            )


def _render_month_filters(months: list) -> tuple:
    """Render Prior + LE month dropdowns and return the planner's choices.

    Defaults: LE = latest month available, Prior = next-to-latest.
    """
    options = sorted(months)
    labels = {m: m.strftime("%B %Y") for m in options}

    cols = st.columns(2)
    with cols[0]:
        prior = st.selectbox(
            "Prior Month",
            options=options,
            index=max(0, len(options) - 2),
            format_func=lambda m: labels[m],
            key="ro_cmp_prior_month",
            help="The earlier of the two snapshots to compare.",
        )
    with cols[1]:
        le = st.selectbox(
            "LE Month (latest)",
            options=options,
            index=len(options) - 1,
            format_func=lambda m: labels[m],
            key="ro_cmp_le_month",
            help="The latest-estimate snapshot to compare against Prior.",
        )
    return prior, le


def _invalidate_ro_comparison_downstream() -> None:
    """Drop cached comparison state so the next build rebuilds from source.

    Shared by the fingerprint-driven auto-regen and the explicit
    "Generate" button — after ``RO_Comparison_Output.csv`` is rewritten,
    everything derived from it must be recomputed:

      * ``_SS_SUMMARY_DF`` / ``_SS_MONTHS_SIG`` — drop so
        :func:`_ensure_summary_in_session` rebuilds the in-memory
        comparison frame instead of serving the stale pre-regen copy that
        still matches the (Prior, LE) signature.  Without this the editor
        + per-Format driver table + Subtotal would render OLD numbers even
        though the saved CSV is fresh.
      * ``_SS_SUMMARY_REPORT_*`` — pop so the Summary Report fragment's
        "rebuild iff signature drifted" guard recomputes from the new
        ``_SS_SUMMARY_DF``.
      * :func:`clear_comparison_output_cache` — invalidates the shared
        ``@st.cache_data`` slot read by the Early-Start-Date Programs
        section (and any other consumer of the published CSV), which would
        otherwise serve the previous baseline for up to 15 minutes.
    """
    for key in (
        _SS_SUMMARY_DF, _SS_MONTHS_SIG, _SS_WARNINGS, _SS_DIMITEMS_ERROR,
        _SS_SUMMARY_REPORT_DF, _SS_SUMMARY_REPORT_LOADED_AT,
        _SS_SUMMARY_REPORT_WARNINGS, _SS_SUMMARY_REPORT_RAW_DF,
        _SS_SUMMARY_REPORT_SIG, _SS_SUMMARY_REPORT_TEMPLATE,
    ):
        st.session_state.pop(key, None)
    clear_comparison_output_cache()


def _maybe_auto_regenerate_comparison_output(
    history_df: pd.DataFrame,
    dimitems_df: pd.DataFrame | None,
    item_master_df: pd.DataFrame | None,
    prior_month,
    le_month,
) -> None:
    """Auto-regenerate ``RO_Comparison_Output.csv`` iff History changed.

    Two-phase implementation so the **common no-op render does not
    show a spinner** (the spinner was the cause of the picker
    appearing greyed-out on every page load):

      1. **Cheap detect.**  :func:`detect_history_change` only hashes
         the in-memory History frame and reads the small fingerprint
         sidecar — sub-second, zero spinner, leaves every widget
         interactive.  Returns ``None`` when no regen is needed,
         which is the dominant case on routine reruns.
      2. **Heavy regen.**  When (and only when) the detect step
         returns a fingerprint, we wrap
         :func:`regenerate_comparison_output` in a spinner — that
         path does the build + Fabric write + sidecar update and
         legitimately takes seconds.

    Idempotent within a session: once a regen has fired for a given
    (History-fingerprint, prior, le) triple, the session guard
    short-circuits on subsequent reruns.  The sidecar remains the
    cross-session source of truth; the session guard just avoids
    re-hashing on every page render in the same browser tab.

    Failure handling: regen may raise :class:`RoComparisonError` on
    Fabric write failures.  We surface a non-blocking warning so the
    planner can still inspect the (stale) saved CSV — they can retry
    by reloading once the underlying issue clears.
    """
    if history_df is None or history_df.empty:
        return

    # Session-level cheap guard — once we've decided "in sync" for
    # this (history, prior, le) triple, don't re-check on subsequent
    # reruns within the same session.  The sidecar fingerprint is the
    # cross-session source of truth; this guard exists purely to
    # avoid the per-rerun hash + sidecar read.
    sig = (id(history_df), prior_month.isoformat(), le_month.isoformat())
    if st.session_state.get(_SS_AUTO_REGEN_SIG) == sig:
        return

    # ── Phase 1: cheap detect (no spinner) ────────────────────────
    fp = detect_history_change(history_df)
    st.session_state[_SS_AUTO_REGEN_SIG] = sig  # stamp regardless of outcome
    if fp is None:
        return  # In sync — leave the picker fully interactive.

    # ── Phase 2: heavy regen (spinner only here) ──────────────────
    try:
        with st.spinner(
            "RO_History_Tracker.csv changed — regenerating "
            "RO_Comparison_Output.csv…"
        ):
            result = regenerate_comparison_output(
                history_df, dimitems_df, item_master_df,
                prior_month, le_month,
                history_fingerprint=fp,
            )
    except RoComparisonError as exc:
        st.warning(
            "⚠️ Could not auto-regenerate `RO_Comparison_Output.csv` after "
            "detecting an `RO_History_Tracker.csv` refresh. The saved file "
            "is left untouched; the auto-refresh will retry on the next "
            "render, or you can hard-refresh the browser (see the "
            "guidance at the top of the section).\n\n"
            f"Details: {exc}"
        )
        return

    # Invalidate the entire downstream cascade so this render rebuilds
    # end-to-end from the fresh History snapshot (see
    # :func:`_invalidate_ro_comparison_downstream` for the per-key rationale).
    _invalidate_ro_comparison_downstream()

    # Stash the banner payload — rendered ONCE by
    # ``_render_auto_regen_banner_once`` later in the page flow.
    st.session_state[_SS_AUTO_REGEN_BANNER] = result


def _render_post_upload_guidance() -> None:
    """Tell the planner how to see their changes after uploading a new file.

    Replaces the deprecated "🔄 Refresh from Fabric" button.  Two
    things changed since that button was useful:

    1. ``RO_History_Tracker.csv`` cache keys are now ETag-driven (see
       :func:`data_sources.ro_comparison._compute_history_blob_signature`),
       so every render does a sub-100ms HEAD-equivalent to see if
       Fabric has a fresh version.  When it does, the comparison
       output + every downstream table auto-regenerate in the same
       render — no button click required.
    2. The "Upload Customer Input" path also writes into the same
       Fabric pipeline.  Once Fabric materialises the new history
       (typically minutes after upload — outside this app's control),
       the next render here picks it up automatically.

    The remaining failure mode is *browser-side staleness* — a
    proxy / browser cache pinning the previous Streamlit asset
    bundle.  We surface the canonical hard-refresh shortcuts so the
    planner can self-serve in those rare cases without us needing a
    code-side "force refresh" hack.

    Collapsed by default so the upload control stays above the fold;
    expand when troubleshooting stale data after an upload.
    """
    with st.expander(
        "ℹ️ Changes auto-refresh after upload — troubleshooting",
        expanded=False,
    ):
        st.markdown(
            "✅ **Changes auto-refresh.**  Once Fabric ingests your upload "
            "(usually within a few minutes), this page detects the new "
            "`RO_History_Tracker.csv` ETag on the next render and "
            "automatically rebuilds the RO Comparison table, the driver "
            "breakdown, the Early-Start-Date Programs table, and the "
            "RO Summary Report — no button click required.\n\n"
            "**If you still don't see your changes after a few minutes:**\n"
            "1. Wait one more minute, then reload the page tab.\n"
            "2. If the table is still stale, do a **hard refresh** to clear "
            "your browser's local cache:\n"
            "   - **Windows / Linux:** `Ctrl` + `Shift` + `R` (or `Ctrl` + `F5`)\n"
            "   - **macOS:** `⌘` + `Shift` + `R`\n"
            "3. As a last resort, sign out of Microsoft Fabric (top of the "
            "sidebar) and sign back in — this drops every cached token and "
            "forces a fresh read."
        )


def _render_auto_regen_banner_once() -> None:
    """Pop and render the most recent auto-regen banner if present.

    One-shot display: after the planner sees the banner once, it's
    cleared from session state so subsequent reruns within the same
    visit don't keep flashing it.  A genuine re-detection (new
    History fingerprint) will repopulate the banner.
    """
    result: AutoRegenResult | None = st.session_state.pop(
        _SS_AUTO_REGEN_BANNER, None,
    )
    if result is None:
        return

    st.success(
        "📥 **`RO_History_Tracker.csv` was refreshed since the last save.**  \n"
        f"Auto-regenerated `Files/{result.blob_path}` for "
        f"**{result.prior_month:%B %Y}** vs **{result.le_month:%B %Y}** "
        f"({result.rows_saved:,} rows).  Any unsaved planner edits to the "
        "previous version were intentionally discarded — review the new "
        "baseline below and re-save if you want to publish further edits."
    )


def _ensure_summary_in_session(
    history_df: pd.DataFrame,
    dimitems_df: pd.DataFrame | None,
    item_master_df: pd.DataFrame | None,
    prior_month,
    le_month,
    dimitems_err: str | None,
    item_master_err: str | None,
) -> None:
    """Build the comparison summary once per (Prior, LE) and cache it.

    Caching in ``st.session_state`` (not ``@st.cache_data``) is
    deliberate: the frame is mutable — planners edit individual cells —
    so a process-wide cache would corrupt other sessions, while a
    session-scoped cache survives filter clicks and editor reruns
    without losing in-progress edits.
    """
    months_sig = (prior_month, le_month)
    if st.session_state.get(_SS_MONTHS_SIG) == months_sig:
        return

    summary_df, warnings = build_ro_comparison(
        history_df, dimitems_df, prior_month, le_month,
        item_master_df=item_master_df,
    )
    if dimitems_err:
        warnings.dimitems_unavailable = True
        warnings.extras.append(f"dp_dimitems unavailable: {dimitems_err}")
    if item_master_err:
        warnings.extras.append(f"RO_Item_Master.csv unavailable: {item_master_err}")

    st.session_state[_SS_SUMMARY_DF] = summary_df
    st.session_state[_SS_WARNINGS] = warnings
    st.session_state[_SS_MONTHS_SIG] = months_sig
    st.session_state[_SS_DIMITEMS_ERROR] = dimitems_err
    # Republish the freshly built comparison to Fabric.  This keeps the
    # downstream consumers (Summary Report, PLR R&O, drill-downs) reading
    # the CURRENT (Prior, LE) view rather than whatever the last
    # ``RO_History_Tracker.csv`` change-detect path wrote.  Idempotent via
    # signature guard — repeat reruns within the same session don't write.
    _maybe_autosave_ro_comparison_output(trigger="comparison rebuild")


def _render_ro_comparison_save_button(summary_df: pd.DataFrame) -> None:
    """Render the manual "Save to Fabric" button for RO_Comparison_Output.csv.

    Bypasses the signature-guard in :func:`_maybe_autosave_ro_comparison_output`
    so an explicit click ALWAYS writes, even when an identical save already
    happened in this session (the planner may have just edited a cell —
    the editor edits land in :data:`_SS_SUMMARY_DF` but don't change the
    Prior/LE signature, so the auto-save guard would otherwise skip them).
    """
    if summary_df is None or summary_df.empty:
        return

    if not st.button(
        "💾 Save `RO_Comparison_Output.csv` to Fabric",
        key="ro_cmp_output_save",
        type="primary",
        help=(
            "Republishes the current comparison view (including any in-tab "
            "cell edits) to "
            "`Files/RO Tracking/RO_Reporting/RO_Comparison_Output.csv`. "
            "The Auto-save path covers Prior/LE month changes — use this "
            "button after editing cells in the table above."
        ),
    ):
        return

    try:
        with st.spinner("Saving RO_Comparison_Output.csv to Microsoft Fabric…"):
            blob_path = save_ro_comparison_output(summary_df)
    except RoComparisonError as exc:
        st.error(f"❌ Save failed.\n\n{exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error saving RO_Comparison_Output.csv.")
        st.error(
            "❌ Save failed unexpectedly — the file was not written.\n\n"
            f"{type(exc).__name__}: {exc}"
        )
        return

    # Stamp the guard so the auto-save flow doesn't re-write the same frame
    # on the very next rerun.  Mirror the key the auto-save path uses.
    st.session_state[_SS_AUTOSAVE_RO_CMP_SIG] = (
        st.session_state.get(_SS_MONTHS_SIG),
        _signature_for(summary_df),
    )
    st.success(f"✅ Saved to `Files/{blob_path}` ({len(summary_df):,} rows).")


def _render_ro_comparison_generate_button(
    prior_month: date,
    le_month: date,
) -> None:
    """Render an explicit "regenerate from source" button.

    Distinct from the other two write paths:

      * **Auto-regen** only fires when ``RO_History_Tracker.csv``'s
        fingerprint drifts from the last-saved output — and only if the
        cached read already surfaced the fresh bytes.
      * **💾 Save** republishes the in-memory frame *including* any
        hand-edits made in the table.

    This button **force-reads the latest `RO_History_Tracker.csv`
    (+ dp_dimitems / RO_Item_Master dims) straight from Fabric**, rebuilds
    the comparison for the selected Prior/LE months, and overwrites
    ``RO_Comparison_Output.csv`` — **discarding** any manual cell edits.

    The force-refresh is the crux: without it the button would regenerate
    from whatever ``fetch_ro_history_df`` last cached, so a just-updated
    ``RO_History_Tracker.csv`` could produce an identical table (the bug
    this fixes).  After writing, it drops the cached comparison + the
    auto-regen session guard and **reruns**, so the page re-reads the now-
    fresh source at the top and every downstream table rebuilds from the
    new baseline.
    """
    clicked = st.button(
        "🔁 Generate `RO_Comparison_Output.csv` from current RO_History",
        key="ro_cmp_generate_from_history",
        use_container_width=True,
        help=(
            "Force-reads the latest `RO_History_Tracker.csv` from Fabric "
            "(which already reflects `Distribution_Tracker_History.csv`), "
            "rebuilds the comparison for the selected Prior/LE months, and "
            "overwrites `Files/RO Tracking/RO_Reporting/RO_Comparison_Output.csv`. "
            "Ignores any in-tab cell edits — use **💾 Save** to keep those."
        ),
    )
    if not clicked:
        return

    # 1. Force a FRESH read of the required source, bypassing the ETag /
    #    TTL cache entirely.  A stale cached frame is exactly why a
    #    just-updated RO_History could regenerate an identical table.
    try:
        with st.spinner(
            "Reading the latest RO_History_Tracker.csv from Microsoft Fabric…"
        ):
            history_df = fetch_ro_history_df(force_refresh=True)
    except RoComparisonError as exc:
        st.error(f"❌ Could not read the latest RO_History_Tracker.csv.\n\n{exc}")
        return
    if history_df is None or history_df.empty:
        st.warning("RO_History_Tracker.csv is empty — nothing to generate from.")
        return

    # 2. Best-effort fresh read of the soft dimension sources; a failure
    #    just leaves the Portfolio / Supply-Format cascade to its fallback.
    def _refresh_soft(fetcher) -> pd.DataFrame | None:
        try:
            return fetcher(force_refresh=True)
        except RoComparisonError:
            return None

    dimitems_df = _refresh_soft(fetch_dimitems_df)
    item_master_df = _refresh_soft(fetch_ro_item_master_df)

    # 3. Rebuild + save unconditionally from the fresh frames.
    try:
        with st.spinner(
            "Generating RO_Comparison_Output.csv from current RO_History…"
        ):
            result = regenerate_comparison_output(
                history_df, dimitems_df, item_master_df, prior_month, le_month,
            )
    except RoComparisonError as exc:
        st.error(f"❌ Could not generate RO_Comparison_Output.csv.\n\n{exc}")
        return

    # 4. Drop the cached comparison + the auto-regen session guard, stash the
    #    one-shot banner, and rerun.  On the rerun the top-of-section
    #    ``fetch_ro_history_df()`` returns the now-fresh cache and
    #    ``_ensure_summary_in_session`` rebuilds the on-screen table from it.
    _invalidate_ro_comparison_downstream()
    st.session_state.pop(_SS_AUTO_REGEN_SIG, None)
    st.session_state[_SS_AUTO_REGEN_BANNER] = result
    st.rerun()


def _render_warnings_banner(w: ComparisonWarnings) -> None:
    """Render the consolidated warnings banner above the table.

    Foldable so a planner staring at a clean run doesn't have a
    multi-line orange block dominating the page header — the banner
    is collapsed by default and the title text gives the count.
    Listing item IDs (capped at 30 per category to keep the body
    legible once expanded) lets the planner ctrl-F them in the table
    immediately instead of scrolling.
    """
    if not w.has_any():
        return

    def _format_items(items: list[str]) -> str:
        if not items:
            return ""
        head = ", ".join(items[:30])
        tail = f"… (+{len(items) - 30} more)" if len(items) > 30 else ""
        return f"{head}{tail}"

    lines: list[str] = []
    if w.missing_brand:
        lines.append(
            f"**Missing Brand** ({len(w.missing_brand)} item(s) — please fill): "
            f"{_format_items(w.missing_brand)}"
        )
    if w.missing_portfolio:
        lines.append(
            f"**Missing Portfolio Major/Minor** ({len(w.missing_portfolio)} item(s) "
            f"not found in dp_dimitems — please fill): {_format_items(w.missing_portfolio)}"
        )
    if w.missing_supply_format:
        lines.append(
            f"**Missing Supply Format** ({len(w.missing_supply_format)} item(s) "
            f"with no value from either dp_dimitems or RO_History Format): "
            f"{_format_items(w.missing_supply_format)}"
        )
    if w.unparseable_dates:
        lines.append(
            f"**Unparseable dates** ({len(w.unparseable_dates)} item(s)): "
            f"{_format_items(w.unparseable_dates)}"
        )
    if w.unparseable_numerics:
        lines.append(
            f"**Unparseable numeric cells** ({len(w.unparseable_numerics)} item(s)): "
            f"{_format_items(w.unparseable_numerics)}"
        )
    if w.dimitems_unavailable:
        lines.append(
            "**dp_dimitems unavailable** — Portfolio Major/Minor are blank and "
            "Supply Format falls back to RO_History Format for every row. "
            "Fix Fabric sign-in and reload."
        )
    for note in w.extras:
        lines.append(f"**Other:** {note}")

    # Foldable container — collapsed by default.  The expander label
    # surfaces the *count* so the planner can decide at a glance
    # whether to expand without reading the body.  Body uses markdown
    # bullets so the existing **bold** category labels render
    # consistently with the previous flat ``st.warning`` layout.
    with st.expander(
        f"⚠️ {len(lines)} note(s) — please review and fix before saving",
        expanded=False,
    ):
        for line in lines:
            st.markdown(f"- {line}")


@st.fragment
def _render_filtered_editor_fragment(prior_month, le_month) -> None:
    """Render the editor + subtotal + per-Format summary + Save.

    Why this is a fragment
    ----------------------
    Cell-edit / Save interactions used to trigger a full page rerun, which
    re-executed the upload control, re-fetched RO_History + dp_dimitems from
    Fabric (cache-hit but still pays the cache key check), re-ran
    ``build_ro_comparison``, and re-rendered the warnings banner — every time
    the planner edited a cell or hit Save.  Wrapping the editable part in a
    fragment (Streamlit ≥ 1.33) scopes the rerun to just this function:
    editing a cell / clicking Save only re-renders this block.

    Filters live OUTSIDE this fragment, in the shared "🔍 Filters" section
    (month pickers + field filters).  Changing one is a full rerun, but a
    cheap one — the Fabric reads and the comparison build are signature-guarded
    and short-circuit when nothing upstream changed.  We read the planner's
    saved field-filter selections from session_state and apply them here.

    Why not @st.cache_data
    ----------------------
    The comparison frame is *mutable* — planners edit cells and
    the frame round-trips through ``_recompute_derived_columns``
    on every fragment rerun.  ``cache_data`` would cache a stale
    pre-edit copy and corrupt other sessions on a multi-tab Cloud
    deployment.  ``st.session_state`` (still the owner of the
    canonical frame) gives us per-session mutability without that
    risk.
    """
    summary_df: pd.DataFrame = st.session_state[_SS_SUMMARY_DF]

    # Field filters are rendered up in the shared "🔍 Filters" section; here we
    # just apply the planner's saved selections to the (possibly edited) frame.
    filtered_df = _apply_filters(summary_df, _read_filter_state_from_session())
    st.caption(
        f"Showing {len(filtered_df):,} of {len(summary_df):,} rows · "
        f"Prior: {prior_month.strftime('%B %Y')} · "
        f"LE: {le_month.strftime('%B %Y')}"
    )

    # Editable table.  Key the editor on (prior, le) so a month change
    # (which happens OUTSIDE this fragment and triggers a full page
    # rerun) resets the widget's internal state cleanly.
    editor_key = (
        f"ro_cmp_editor_{prior_month.isoformat()}_{le_month.isoformat()}"
    )
    edited_filtered = st.data_editor(
        filtered_df,
        key=editor_key,
        num_rows="fixed",
        use_container_width=True,
        height=520,
        column_config=_ro_column_config(),
    )

    # Merge edits back into the master frame, then recompute derived
    # columns so the next render shows fresh Change / Probability /
    # Driver / Days values.
    if not edited_filtered.empty:
        summary_df.loc[edited_filtered.index, edited_filtered.columns] = edited_filtered
    summary_df = _recompute_derived_columns(summary_df)
    st.session_state[_SS_SUMMARY_DF] = summary_df

    # Subtotal mirrors the FILTERED view (it's "subtotal of what's on
    # screen") so it reflects every filter pick + every cell edit.
    post_recompute_view = _apply_filters(
        summary_df, _read_filter_state_from_session(),
    )
    _render_subtotal(post_recompute_view)

    # Per-Format driver summary is a portfolio-wide diagnostic — it
    # MUST ignore the field filters above (per planner spec) so the
    # totals are meaningful regardless of what's currently selected.
    # Reads from the (now-recomputed) full master frame.  The drill-
    # down expander beneath it also reads the full frame so the
    # planner can chase ANY driver bucket without re-narrowing the
    # filters at the top of the section.
    _render_per_format_summary(summary_df)

    # Manual Save button — republishes the in-memory comparison frame
    # (including any planner cell edits) to
    # ``Files/RO Tracking/RO_Reporting/RO_Comparison_Output.csv``.  Sits
    # alongside the existing auto-save hooks:
    #   * Auto-regen path (history-fingerprint change)  → upstream pipeline.
    #   * Auto-save on every Prior/LE rebuild           → see
    #     :func:`_maybe_autosave_ro_comparison_output` in
    #     :func:`_ensure_summary_in_session`.
    #   * Auto-save at the end of every PLR render     → see PLR fragment.
    # The button is the explicit escape hatch for planner edits, which the
    # other paths cannot detect on their own.
    _render_ro_comparison_save_button(summary_df)


# Sentinel option surfaced inside every filter multiselect — picking
# it narrows the view to rows where the column is blank (NaN, empty
# string, or whitespace-only).  Centralised so the multiselect option
# list, the help text, and the apply-filter path all stay in lockstep.
# Spelled with parentheses + lowercase to avoid colliding with any
# real value in any of the filterable columns.
_BLANK_FILTER_SENTINEL: str = "(blank)"


def _render_field_filter_widgets(summary_df: pd.DataFrame) -> None:
    """Render the per-column filter multiselects (widgets only — no apply).

    Selections persist in ``st.session_state`` under stable keys (see
    :func:`_filter_widget_key`) so a rerun doesn't clear the planner's picks;
    the editor fragment reads them back with
    :func:`_read_filter_state_from_session` and narrows the frame via
    :func:`_apply_filters`. There is no ``st.expander`` here — these widgets
    are rendered inside the shared "🔍 Filters" section, directly beneath the
    Prior/LE month pickers.

    Blank-cell handling
    -------------------
    Every column whose source frame has at least one blank cell
    (NaN / empty string / whitespace) exposes a special ``"(blank)"`` option
    at the top of its multiselect.  Picking it narrows the view to *exactly
    those blank rows* — the canonical way for a planner to find e.g. items
    missing a Portfolio Minor that need triaging.  See
    :data:`_BLANK_FILTER_SENTINEL`.
    """
    st.caption(
        "Tip: pick **(blank)** in any filter to surface rows where "
        "that column is empty (handy for finding items missing a "
        "Portfolio Minor, Supply Format, etc.)."
    )
    cols = st.columns(3)
    for i, col_name in enumerate(_RO_FILTER_COLUMNS):
        with cols[i % 3]:
            options = _filter_options_for_column(summary_df[col_name])
            st.multiselect(
                col_name,
                options=options,
                key=_filter_widget_key(col_name),
            )


def _filter_options_for_column(series: pd.Series) -> list[str]:
    """Return the sorted dropdown options for a single filter column.

    Output layout:

    * ``"(blank)"`` first (only when the column actually has at least
      one blank value — otherwise the option is omitted to avoid
      cluttering filters with non-actionable choices).
    * Real non-blank values sorted ascending, string-cast for stable
      ordering across mixed dtypes (Item # stored as int vs str).
    """
    str_series = series.astype("string")
    has_blank = bool(
        str_series.isna().any()
        or (str_series.fillna("").str.strip() == "").any()
    )
    real_values = sorted({
        str(v).strip() for v in str_series.dropna() if str(v).strip()
    })
    return ([_BLANK_FILTER_SENTINEL] if has_blank else []) + real_values


def _filter_widget_key(col_name: str) -> str:
    """Return the stable session-state key for a single filter widget."""
    # Replace characters that can collide with Streamlit's key encoding.
    safe = col_name.replace(" ", "_").replace("#", "num").replace(".", "")
    return f"ro_cmp_filter_{safe}"


def _read_filter_state_from_session() -> dict:
    """Snapshot the current filter selections from session_state."""
    return {
        col: st.session_state.get(_filter_widget_key(col), [])
        for col in _RO_FILTER_COLUMNS
    }


def _apply_filters(df: pd.DataFrame, selections: dict) -> pd.DataFrame:
    """Return *df* narrowed by the per-column selections.

    Comparison is string-cast so a multiselect of ``["370072"]`` matches
    ``Item #`` cells stored as int or float without surprising the user
    with dtype-driven mismatches.

    The :data:`_BLANK_FILTER_SENTINEL` value is handled specially: when
    present in a column's selection, it adds "row is blank in this
    column" (NaN / empty / whitespace-only) as an additional accepted
    value.  Mixed selections (e.g. ``["(blank)", "Costco"]``) match
    rows that satisfy EITHER condition — same as the user's mental
    model for a normal multiselect with an extra option.
    """
    mask = pd.Series(True, index=df.index)
    for col, values in selections.items():
        if not values:
            continue
        col_str = df[col].astype("string")
        is_blank = col_str.isna() | (col_str.fillna("").str.strip() == "")
        wants_blank = _BLANK_FILTER_SENTINEL in values
        non_blank_values = [
            str(v) for v in values if v != _BLANK_FILTER_SENTINEL
        ]
        col_mask = col_str.fillna("").str.strip().isin(non_blank_values)
        if wants_blank:
            col_mask = col_mask | is_blank
        mask &= col_mask
    return df.loc[mask]


def _ro_column_config() -> dict:
    """Return the ``column_config`` mapping for the RO Comparison editor.

    * Computed columns are read-only so a planner cannot drift them out
      of sync with their inputs.
    * Every Lbs column uses ``format="accounting"`` per spec — comma
      thousands separators, no decimals, no currency sign, negatives
      in parentheses.  This is a Streamlit ≥ 1.36 feature; we are on
      1.49 (see ``requirements.txt``) so the format is always available.
    * Probability columns stay at 2-decimal precision (``"%.2f"``) per
      explicit user direction — keeps the 0.25-style display the
      planner is used to.
    * Ship-date columns render via ``DateColumn`` so the time component
      of the underlying ``datetime64`` value is hidden.
    """
    cc = st.column_config

    money_fmt = "accounting"  # comma thousands, no decimals, no $ — per spec
    prob_fmt  = "%.2f"
    date_fmt  = "YYYY-MM-DD"
    int_fmt   = "%d"

    config: dict = {
        # Read-only IDs / driver / Existing SKUs.
        "Item #":             cc.TextColumn(disabled=True),
        "Prior RO Key":       cc.NumberColumn(format=int_fmt, disabled=True),
        "LE RO Key":          cc.NumberColumn(format=int_fmt, disabled=True),
        "Driver":             cc.TextColumn(disabled=True),
        "Existing SKUs":      cc.TextColumn(disabled=True),
        # Editable Lbs inputs — Prior / LE for all three metric pairs.
        ANNUAL_OPP_PRIOR:      cc.NumberColumn(format=money_fmt),
        ANNUAL_OPP_LE:         cc.NumberColumn(format=money_fmt),
        YEAR1_PROB_PRIOR:      cc.NumberColumn(format=money_fmt),
        YEAR1_PROB_LE:         cc.NumberColumn(format=money_fmt),
        CUR_FISCAL_PROB_PRIOR: cc.NumberColumn(format=money_fmt),
        CUR_FISCAL_PROB_LE:    cc.NumberColumn(format=money_fmt),
        # Read-only derived Change columns.
        ANNUAL_OPP_CHANGE:      cc.NumberColumn(format=money_fmt, disabled=True),
        YEAR1_PROB_CHANGE:      cc.NumberColumn(format=money_fmt, disabled=True),
        CUR_FISCAL_PROB_CHANGE: cc.NumberColumn(format=money_fmt, disabled=True),
        # Read-only derived probabilities (per IFERROR(Exp/Lbs, 0) spec).
        "Prior Probability":  cc.NumberColumn(format=prob_fmt, disabled=True),
        "LE Probability":     cc.NumberColumn(format=prob_fmt, disabled=True),
        "Change Probability": cc.NumberColumn(format=prob_fmt, disabled=True),
        # Editable ship dates rendered as dates (no time component).
        "Prior First Ship Date": cc.DateColumn(format=date_fmt),
        "LE First Ship Date":    cc.DateColumn(format=date_fmt),
        # Read-only derived day-delta.
        "Change (Days)":      cc.NumberColumn(format=int_fmt, disabled=True),
    }
    return config


def _render_subtotal(view_df: pd.DataFrame) -> None:
    """Render a one-row subtotal directly beneath the editable table.

    Sums are computed from the filtered + recomputed view, so they
    react to every filter change AND every cell edit on the next
    fragment rerun.  Rendered as a separate compact dataframe rather
    than appended to the editor so it stays visually distinct.

    Every numeric column uses ``format="accounting"`` for the same
    reason the editor does — comma thousands, no decimals, no $.
    """
    if view_df.empty:
        st.caption("_No rows match the current filters — subtotal is 0._")
        return

    totals = {
        col: round(float(pd.to_numeric(view_df[col], errors="coerce").fillna(0).sum()), 1)
        for col in SUBTOTAL_COLUMNS
    }
    totals_df = pd.DataFrame([totals], index=["Subtotal"])

    cc = st.column_config
    subtotal_config = {
        col: cc.NumberColumn(format="accounting", disabled=True)
        for col in SUBTOTAL_COLUMNS
    }

    st.markdown("**Subtotal** (live: reflects current filters + edits)")
    st.dataframe(
        totals_df,
        use_container_width=True,
        height=80,
        column_config=subtotal_config,
    )


def _render_per_format_summary(view_df: pd.DataFrame) -> None:
    """Render the per-Format Δ Current Fiscal Probabilized Lbs summary.

    One row per Format with the net Δ (LE − Prior of "Current Fiscal
    Probabilized Lbs") and the top 3 driver buckets — each bucket is
    one ``(Customer, Portfolio Minor)`` combination — displayed
    inline.  A TOTAL footer row reconciles to the section subtotal.

    Beneath the summary, an "🔬 Drill into items" expander lets the
    planner pick any (Format, Customer, Portfolio Minor) bucket and
    see the item-level rows that compose it — the bridge from
    "where's the money moving?" (this table) to "which SKUs?" (the
    drill-down).

    Visual treatment
    ----------------
    * Negative Δ values in the **Δ** column are colored red, positive
      green, via ``pandas.Styler.map`` — the planner can spot the
      bleeders at a glance.
    * The TOTAL row is rendered in bold to set it apart from the
      individual-Format rows.
    * The Δ column uses ``format="accounting"`` like the rest of the
      section.  Driver cells are pre-formatted text
      ``"{Customer} — {Portfolio Minor}  (±value)"`` rendered as
      plain strings.

    Lives INSIDE the same ``@st.fragment`` as the editor + subtotal,
    so it recomputes instantly on every filter change or cell edit
    without re-touching Fabric.
    """
    summary = compute_per_format_summary(view_df)
    if summary.empty:
        return

    st.markdown(
        "**Δ Current Fiscal Probabilized Lbs — by Format**  \n"
        "_Net change and top 3 driver buckets per Format.  Each driver "
        "bucket is one **(Customer, Portfolio Minor)** combo — the table "
        "above shows the items behind every bucket.  Use the drill-down "
        "expander below to inspect the SKUs in any one bucket._"
    )

    def _color_signed(v):
        """Return a CSS color for a signed numeric value (red / green / none)."""
        try:
            x = float(v)
        except (TypeError, ValueError):
            return ""
        if x > 0:
            return "color: #1b7f3a; font-weight: 600"
        if x < 0:
            return "color: #c0392b; font-weight: 600"
        return ""

    def _bold_total(row):
        """Bold the TOTAL footer row across every column."""
        if row[PER_FORMAT_FORMAT_COL] == PER_FORMAT_TOTAL_LABEL:
            return ["font-weight: 700"] * len(row)
        return [""] * len(row)

    styler = (
        summary.style
        .map(_color_signed, subset=[PER_FORMAT_DELTA_COL])
        .apply(_bold_total, axis=1)
    )

    cc = st.column_config
    column_config = {
        PER_FORMAT_FORMAT_COL: cc.TextColumn("Format", width="small"),
        PER_FORMAT_DELTA_COL:  cc.NumberColumn(format="accounting"),
        PER_FORMAT_DRIVER_COLS[0]: cc.TextColumn(PER_FORMAT_DRIVER_COLS[0], width="large"),
        PER_FORMAT_DRIVER_COLS[1]: cc.TextColumn(PER_FORMAT_DRIVER_COLS[1], width="large"),
        PER_FORMAT_DRIVER_COLS[2]: cc.TextColumn(PER_FORMAT_DRIVER_COLS[2], width="large"),
    }

    st.dataframe(
        styler,
        use_container_width=True,
        # Auto-fit-ish height: 36 px / row + 38 px header, capped so it
        # never crowds the page when there are many Formats.
        height=min(36 * (len(summary) + 1) + 38, 480),
        hide_index=True,
        column_config=column_config,
    )

    # ── Drill-down: items behind a single driver bucket ───────────────
    # Reads the FULL master frame (passed in as *view_df*) so the
    # planner can chase any bucket regardless of the field filters
    # at the top of the section — same "portfolio-wide diagnostic"
    # contract as the summary table itself.
    _render_driver_drill_down(view_df, summary)


def _render_driver_drill_down(
    view_df: pd.DataFrame, summary: pd.DataFrame,
) -> None:
    """Render the (Format → Customer/PMinor → Items) drill-down expander.

    Wiring
    ------
    * The Format selector is populated from the per-Format summary
      (excluding the TOTAL footer row) so it lists exactly the
      Formats the planner can see in the table above.
    * The (Customer, PMinor) selector is populated from
      ``compute_per_format_summary``'s underlying buckets — recomputed
      for the chosen Format only so the planner never picks a bucket
      that doesn't exist.  Blank values flow through as the
      :data:`PER_FORMAT_DRIVER_BLANK_LABEL` sentinel (matching the
      table's behaviour).
    * The item table is computed via :func:`compute_driver_items` and
      sorted by ``|Δ Current Fiscal Probabilized Lbs|`` desc so the
      biggest movers within the bucket land at the top.

    Visual treatment matches the rest of the section: accounting
    formatting on the Lbs columns + a small height cap so the
    drilldown never dominates the page.
    """
    # Format selector — exclude the synthetic TOTAL row.
    format_options = [
        f for f in summary[PER_FORMAT_FORMAT_COL].tolist()
        if f != PER_FORMAT_TOTAL_LABEL
    ]
    if not format_options:
        return  # Nothing to drill into.

    with st.expander(
        "🔬 Drill into items — pick a driver bucket to see the SKUs behind it",
        expanded=False,
    ):
        sel_format = st.selectbox(
            "Format",
            options=format_options,
            index=0,
            key="ro_cmp_driver_drill_format",
            help=(
                "Pick the Format whose driver bucket you want to inspect. "
                "The list mirrors the per-Format summary table above."
            ),
        )

        # Build the (Customer, PMinor) options for the chosen Format
        # from the SAME normalised bucket keys the summary uses, so
        # blank-customer / blank-PMinor buckets are reachable.
        bucket_options = _list_driver_buckets_for_format(view_df, sel_format)
        if not bucket_options:
            st.info("No driver buckets for this Format on the current view.")
            return

        bucket_labels = {
            (c, p): _bucket_display_label(c, p)
            for (c, p) in bucket_options
        }
        sel_bucket = st.selectbox(
            "Driver bucket (Customer — Portfolio Minor)",
            options=bucket_options,
            index=0,
            format_func=lambda key: bucket_labels[key],
            key="ro_cmp_driver_drill_bucket",
            help=(
                "Lists every (Customer, Portfolio Minor) combo present in "
                "the chosen Format, sorted by absolute net Δ (largest "
                "movers first).  Pick \"(blank)\" entries to find rows "
                "missing a Customer or Portfolio Minor."
            ),
        )

        sel_customer, sel_pminor = sel_bucket
        items_df = compute_driver_items(
            view_df, sel_format, sel_customer, sel_pminor,
        )
        if items_df.empty:
            st.caption(
                "_No items match the current driver bucket — "
                "this can happen if a cell edit just emptied a "
                "Format/Customer/PMinor combination._"
            )
            return

        cc = st.column_config
        column_config = {
            "Item #":          cc.TextColumn("Item #", width="small"),
            "Description":     cc.TextColumn("Description", width="medium"),
            "Brand":           cc.TextColumn("Brand", width="small"),
            "Driver":          cc.TextColumn("Driver", width="small"),
            CUR_FISCAL_PROB_PRIOR: cc.NumberColumn(format="accounting"),
            CUR_FISCAL_PROB_LE:    cc.NumberColumn(format="accounting"),
            CUR_FISCAL_PROB_CHANGE: cc.NumberColumn(
                "Δ Lbs", format="accounting",
            ),
        }
        st.caption(
            f"**{len(items_df):,} item(s)** in **{sel_format} → "
            f"{bucket_labels[sel_bucket]}**"
        )
        st.dataframe(
            items_df,
            use_container_width=True,
            height=min(36 * (len(items_df) + 1) + 38, 420),
            hide_index=True,
            column_config=column_config,
        )


def _list_driver_buckets_for_format(
    view_df: pd.DataFrame, format_name: str,
) -> list[tuple[str, str]]:
    """Return ``(Customer, Portfolio Minor)`` buckets for *format_name*.

    Buckets are sorted by ``|net Δ|`` desc so the planner sees the
    same ordering they'd expect from the per-Format summary table.
    Blank values are normalised to :data:`PER_FORMAT_DRIVER_BLANK_LABEL`
    so a blank-Customer / blank-PMinor combo is selectable instead of
    vanishing under a NaN groupby key.

    Pure helper — no IO, no Streamlit dependencies — extracted from
    :func:`_render_driver_drill_down` so the selector logic is
    independently inspectable / unit-testable.
    """
    if view_df is None or view_df.empty:
        return []

    work = view_df.copy()
    work["_fmt"] = (
        work[PER_FORMAT_FORMAT_COL].astype(str).str.strip()
        .replace({"": "(Unspecified)", "nan": "(Unspecified)"})
    )
    work = work.loc[work["_fmt"].eq((format_name or "").strip() or "(Unspecified)")]
    if work.empty:
        return []

    # Mirror the bucket-key normalisation used by ``compute_per_format_summary``
    # so a "(blank)" Customer in the summary table maps to the same option
    # offered by the drill-down selector.
    customers = (
        work["Customer"].astype("string").fillna("").str.strip()
        .where(lambda s: s.ne(""), PER_FORMAT_DRIVER_BLANK_LABEL)
    )
    pminors = (
        work.get("Portfolio Minor", pd.Series(dtype="string"))
            .astype("string").fillna("").str.strip()
            .where(lambda s: s.ne(""), PER_FORMAT_DRIVER_BLANK_LABEL)
    )
    deltas = pd.to_numeric(
        work[CUR_FISCAL_PROB_CHANGE], errors="coerce",
    ).fillna(0.0).abs()

    keys = pd.DataFrame({
        "customer": customers.values,
        "pminor": pminors.values,
        "_abs": deltas.values,
    })
    # Sum |Δ| per bucket for the ranking — the SIGN of Δ is irrelevant
    # to "which buckets matter most" — both growers and shrinkers
    # should be reachable from the top of the list.
    ranked = (
        keys.groupby(["customer", "pminor"], dropna=False)["_abs"]
            .sum().reset_index()
            .sort_values(by=["_abs", "customer", "pminor"],
                         ascending=[False, True, True],
                         kind="mergesort")
    )
    return [(str(r.customer), str(r.pminor)) for r in ranked.itertuples()]


def _bucket_display_label(customer: str, pminor: str) -> str:
    """Render a ``(Customer, PMinor)`` tuple as a human-friendly select option."""
    customer_disp = customer or PER_FORMAT_DRIVER_BLANK_LABEL
    pminor_disp = pminor or PER_FORMAT_DRIVER_BLANK_LABEL
    return f"{customer_disp} — {pminor_disp}"


# ── RO Pipeline Analytics (tiles + charts, above the RO Summary) ─────────────

def _ro_pipeline_comp_df() -> Optional[pd.DataFrame]:
    """In-memory RO Comparison per-program frame, narrowed by active filters.

    Returns the same frame the RO Summary rolls up (``_SS_SUMMARY_DF``), with
    the field filters applied so the tiles / charts react to them exactly like
    the summary below.  ``None`` when the comparison hasn't built yet.
    """
    comp = st.session_state.get(_SS_SUMMARY_DF)
    if comp is None or comp.empty:
        return None
    filter_state = _read_filter_state_from_session()
    if any(sel for sel in filter_state.values()):
        return _apply_filters(comp, filter_state)
    return comp


def _fmt_m_lbs(lbs: float) -> str:
    """Raw lbs → compact millions string for a KPI tile, e.g. ``"45.2M"``."""
    return f"{(lbs or 0.0) / 1e6:,.1f}M"


def _fmt_m(millions: float) -> str:
    """A value already in millions → compact tile string, e.g. ``"45.2M"``."""
    return f"{(millions or 0.0):,.1f}M"


def _ro_total_b2c_totals(comp_df: pd.DataFrame) -> tuple[Optional[float], Optional[float]]:
    """Canonical (In-Year FY27, Full-Year FY28) Total B2C totals, in millions.

    Reads them from the SAME roll-up the RO Summary shows (``build_summary_report``
    → Total B2C row) so the two headline tiles are identical to the summary by
    construction — not a parallel per-program sum that can drift when a row's
    portfolio / format falls outside the summary taxonomy.  ``(None, None)`` when
    the roll-up can't be built or has no Total B2C row.
    """
    try:
        report_df, _warn, _tpl = build_summary_report(comp_df)
    except RoSummaryReportError:
        return None, None
    row = report_df.loc[report_df[SR_COL_ROW_ID] == "total_b2c"]
    if row.empty:
        return None, None
    return float(row.iloc[0][SR_COL_CURRENT_PLAN]), float(row.iloc[0][SR_COL_Y1_LATEST])


def _render_ro_pipeline_tiles(
    comp_df: pd.DataFrame, *,
    in_year_m: Optional[float] = None, full_year_m: Optional[float] = None,
) -> None:
    """Four headline pipeline metrics as KPI tiles (millions of lbs).

    Gross (unweighted) · Full-Year risk-adjusted (FY28) · In-Year risk-adjusted
    (FY27) · Committed (in-year probabilized at ≥95%, with its concentration of
    the in-year pipeline).  When *in_year_m* / *full_year_m* are supplied (the
    canonical RO Summary Total B2C totals), those two tiles use them so they
    match the summary exactly; otherwise they fall back to the per-program sum.
    A caption flags any gap between the per-program in-year sum and Total B2C
    (rows outside the summary taxonomy).
    """
    m = rpa.compute_pipeline_metrics(comp_df)
    pp_in_year_m = m.in_year_lbs / 1e6                 # per-program (all rows)
    in_year_disp = in_year_m if in_year_m is not None else pp_in_year_m
    full_year_disp = full_year_m if full_year_m is not None else m.full_year_lbs / 1e6
    # Committed % is the share of the (per-program) in-year pipeline that
    # committed is a subset of — its true denominator.
    conc = m.committed_concentration
    committed_val = (
        f"{_fmt_m_lbs(m.committed_lbs)}, {conc * 100:.0f}%"
        if conc is not None else _fmt_m_lbs(m.committed_lbs)
    )
    tiles = (
        ("Gross Pipeline", _fmt_m_lbs(m.gross_lbs),
         "unweighted annual opportunity, all programs"),
        ("Full-Year, Risk-adjusted", _fmt_m(full_year_disp),
         f"{rpa.FY_NEXT_LABEL} probabilized — Total B2C"),
        ("In-Year, Risk-adjusted", _fmt_m(in_year_disp),
         f"{rpa.FY_CURRENT_LABEL} probabilized — Total B2C"),
        ("Committed", committed_val,
         "in-year probabilized at ≥95% · M lbs & % of in-year"),
    )
    cards = "".join(
        f'<div class="dpc-kpi dpc-kpi--walk">'
        f'<div class="k-label">{_esc_html(label)}</div>'
        f'<div class="k-value">{_esc_html(value)}</div>'
        f'<span class="k-sub">{_esc_html(sub)}</span></div>'
        for label, value, sub in tiles
    )
    st.markdown(
        f'{_DPC_KPI_CSS}<div class="dpc-kpis">{cards}</div>',
        unsafe_allow_html=True,
    )
    # Reconciliation guard: surface any in-year volume that the per-program sum
    # sees but the RO Summary Total B2C doesn't (rows whose portfolio / format
    # fall outside the summary taxonomy), so the headline never silently
    # under-/over-states without explanation.
    if in_year_m is not None and abs(pp_in_year_m - in_year_m) > 0.1:
        gap = pp_in_year_m - in_year_m
        st.caption(
            f"⚠️ {gap:+,.1f}M lbs of in-year pipeline is outside the RO Summary "
            "**Total B2C** taxonomy (unmapped portfolio / supply format).  The "
            "In-Year / Full-Year tiles use Total B2C; Gross and the charts below "
            "include every program."
        )


# First-ship-date urgency-bucket colours: dark red = overdue → red = soon →
# amber → blue = slack.
_RO_SHIP_BUCKET_COLORS: dict[str, str] = {
    rpa.SHIP_BUCKET_OVERDUE: "#7b241c",
    rpa.SHIP_BUCKET_NEAR: "#c0392b",
    rpa.SHIP_BUCKET_MID:  "#e59866",
    rpa.SHIP_BUCKET_FAR:  "#5dade2",
}
_RO_CHART_FONT: str = _BH_FONT_COLOR


def _render_ro_urgency_chart(comp_df: pd.DataFrame) -> None:
    """Horizontal stacked bar: FY27 in-year probabilized lbs per Portfolio × Format.

    One row per Portfolio Major × Supply Format, stacked by first-ship-date
    urgency window (< 30 / 30–90 / > 90 days), sorted by total volume desc
    (largest on top).  Values in millions of lbs.
    """
    wide = rpa.build_urgency_ranking(comp_df, as_of=date.today())
    if wide.empty:
        st.info("No in-year probabilized volume to rank under the current filters.")
        return
    cats = [str(c) for c in wide.index]
    fig = go.Figure()
    for bucket in rpa.SHIP_BUCKETS:
        fig.add_bar(
            y=cats, x=(wide[bucket] / 1e6).tolist(), name=bucket,
            orientation="h", marker_color=_RO_SHIP_BUCKET_COLORS[bucket],
            hovertemplate=f"%{{y}} · {bucket}: %{{x:,.1f}}M lbs<extra></extra>",
        )
    fig.update_layout(
        barmode="stack", height=max(260, 30 * len(cats) + 100),
        margin=dict(l=10, r=10, t=40, b=10),
        font=dict(color=_RO_CHART_FONT, size=12),
        xaxis=dict(title=dict(text=f"{rpa.FY_CURRENT_LABEL} In-Year Probabilized (M lbs)"),
                   showgrid=True, gridcolor="#eeeeee", rangemode="tozero"),
        yaxis=dict(autorange="reversed", tickfont=dict(size=11)),  # largest on top
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        plot_bgcolor="white",
    )
    st.plotly_chart(fig, use_container_width=True, key="ro_urgency_chart")


# Session store for planner Action edits, keyed by the stable Program string so
# edits survive reruns / filter changes (the watchlist frame is rebuilt each run).
_SS_WL_ACTIONS = "ro_wl_actions"


def _render_ro_high_urgency_table(comp_df: pd.DataFrame) -> None:
    """Editable high-urgency watchlist with filters + a Fabric-archive button.

    Top-quartile programs by urgency = volume × (1 − prob) × deadline-decay
    (locked-in 100% wins excluded).  Every cell is editable; the **Action**
    column is a Protect/Chase/Qualify/Kill dropdown whose picks persist across
    reruns / filter changes (keyed by Program in session_state).  The Urgency
    column is hidden (it only drives the ranking).  Filters narrow by portfolio
    / annual volume / ship window / probability.  "🔄 Refresh & archive" writes
    the currently-shown (filtered + edited) rows to Fabric as a timestamped CSV.
    """
    st.markdown("#### 🚨 High-Urgency Programs")
    st.caption(
        "Top-quartile programs by **urgency** = annual volume × (1 − win "
        "probability) × deadline-decay (`exp(−days-to-ship / 90)`), so large, "
        "uncertain, soon-to-ship programs rise first.  Locked-in wins "
        "(probability = 100%) are excluded.  Edit any cell; pick an **Action**, "
        "then **Refresh & archive** to snapshot it to Fabric."
    )
    tbl = rpa.build_high_urgency_programs(comp_df, as_of=date.today())
    if tbl.empty:
        st.info(
            "No unlocked programs to prioritize under the current filters — "
            "widen the field filters or check that the comparison has ROs."
        )
        return

    # Seed the Action column from the per-Program session store so prior picks
    # survive reruns and filter changes (the frame itself is rebuilt each run).
    actions: dict = st.session_state.setdefault(_SS_WL_ACTIONS, {})
    tbl[rpa.COL_ACTION] = tbl[rpa.COL_PROGRAM].map(actions).fillna("")

    # ── Watchlist filters (a second, local layer on top of the section's
    #    field filters above) ───────────────────────────────────────
    st.caption("**Watchlist filters** — further narrow the table below only:")
    f1, f2, f3, f4 = st.columns([2, 1.3, 1.5, 1.7])
    with f1:
        portfolios = st.multiselect(
            "Portfolio", options=sorted(tbl[rpa.COL_PORTFOLIO].unique()),
            key="ro_wl_portfolio", help="Leave empty to include every portfolio.")
    with f2:
        min_vol = int(st.number_input(
            "Annual Volume ≥ (lbs)", min_value=0, value=0, step=100_000,
            key="ro_wl_min_vol", help="Keep programs at / above this annual volume."))
    with f3:
        buckets = st.multiselect(
            "Days-to-Ship", options=list(rpa.SHIP_BUCKETS), key="ro_wl_ship",
            help="Filter by how soon each program ships (vs today).")
    with f4:
        lo, hi = st.slider(
            "Probability (%)", min_value=0, max_value=100, value=(0, 100),
            step=5, key="ro_wl_prob", help="Keep programs whose win probability is in this band.")

    filtered = rpa.apply_watchlist_filters(
        tbl, portfolios=portfolios or None,
        min_volume=min_vol or None, ship_buckets=buckets or None,
        prob_range=(lo / 100.0, hi / 100.0))
    if filtered.empty:
        st.info("No programs match the current filters.")
        return

    # Urgency is hidden from the view (and the archive) — it only ranks the rows.
    # No widget key on the editor: persistence is driven by the Program-keyed
    # session store below, which is robust to the row set changing under filters.
    show = filtered.drop(columns=[rpa.COL_URGENCY])
    cc = st.column_config
    edited = st.data_editor(
        show, use_container_width=True, hide_index=True, num_rows="fixed",
        height=min(36 * (len(show) + 1) + 38, 480),
        column_config={
            rpa.COL_PROGRAM:       cc.TextColumn("Program", width="large"),
            rpa.COL_PORTFOLIO:     cc.TextColumn("Portfolio", width="small"),
            rpa.COL_ANNUAL_VOLUME: cc.NumberColumn(
                "Annual Volume (lbs)", format="accounting", width="medium"),
            rpa.COL_IN_YEAR:       cc.NumberColumn(
                f"In-Year ({rpa.FY_CURRENT_LABEL}, lbs)", format="accounting",
                width="medium",
                help=f"{rpa.FY_CURRENT_LABEL} in-year probabilized — the same "
                     "basis the urgency chart plots."),
            rpa.COL_FIRST_SHIP:    cc.DateColumn(
                "First Ship Date", format="YYYY-MM-DD", width="small"),
            rpa.COL_DAYS_TO_SHIP:  cc.NumberColumn(
                "Days-to-Ship", format="%d", width="small"),
            rpa.COL_PROBABILITY:   cc.NumberColumn(
                "Prob", format="percent", width="small"),
            rpa.COL_ACTION:        cc.SelectboxColumn(
                "Action", width="small", options=list(rpa.ACTION_OPTIONS),
                help="Protect / Chase / Qualify / Kill; blank = undecided."),
        },
    )

    # Persist Action edits back to the session store (keyed by Program) so they
    # survive the next rerun / filter change.  Only the shown rows are touched.
    for prog, act in zip(edited[rpa.COL_PROGRAM], edited[rpa.COL_ACTION]):
        val = "" if act is None or pd.isna(act) else str(act).strip()
        if val:
            actions[str(prog)] = val
        else:
            actions.pop(str(prog), None)

    # ── Refresh & archive to Fabric ──────────────────────────────
    st.caption(
        f"Archives the **{len(edited)}** row(s) currently shown (after filters "
        "+ your edits).  Clear the filters above to archive the full list."
    )
    if not fabric_signin_widget.is_fabric_signed_in():
        # Gate on sign-in like the other RO Fabric actions — the write would
        # otherwise only fail at click time.
        st.caption(
            "_Sign in via **Home & Fabric Sign-in** to archive a snapshot._"
        )
    elif st.button("🔄 Refresh & archive snapshot to Fabric",
                   key="ro_wl_archive", use_container_width=True):
        try:
            # UTC timestamp so filenames sort/read consistently regardless of
            # the server's local timezone.
            ts = pd.Timestamp.utcnow().strftime("%Y%m%d_%H%M%S")
            path = save_pipeline_review_snapshot(edited, timestamp=ts)
            st.success(f"✅ Archived {len(edited)} row(s) to `Files/{path}`.")
        except RoComparisonError as exc:
            st.error(f"❌ Could not archive the snapshot to Fabric.\n\n{exc}")

    _render_ro_pipeline_review_archive()


def _render_ro_pipeline_review_archive() -> None:
    """Collapsed list of archived RO Pipeline Review snapshots (audit trail).

    Read-back for the write-only archive — surfaces the timestamped CSVs the
    Refresh button writes so the trail is reachable from the app.  Requires
    Fabric sign-in; degrades to a hint / info banner otherwise.
    """
    with st.expander("🗂️ Archived review snapshots", expanded=False):
        if not fabric_signin_widget.is_fabric_signed_in():
            st.caption("_Sign in to list archived snapshots._")
            return
        try:
            files = list_pipeline_review_snapshots()
        except RoComparisonError as exc:
            st.warning(f"Could not list the archive: {exc}")
            return
        if not files:
            st.info("No snapshots archived yet — use **Refresh & archive** above.")
            return
        # Newest first (already sorted by the data source); show the recent few.
        st.caption(f"{len(files)} snapshot(s) — most recent first.")
        st.dataframe(
            pd.DataFrame(
                [{"File": f.name,
                  "Last modified (UTC)": f.last_modified or "—",
                  "Size (KB)": round((f.size or 0) / 1024, 1)}
                 for f in files[:25]]),
            use_container_width=True, hide_index=True)


# Build-up segment styling: solid green base (expected in-year) → lighter timing
# increment → patterned amber headroom (recoverable if probability lifts).
_RO_BUILDUP_STYLE: dict[str, dict] = {
    rpa.SEG_FY27:        {"color": "#1b7f3a", "pattern": ""},
    rpa.SEG_YEAR_EFFECT: {"color": "#7dc47f", "pattern": "/"},
    rpa.SEG_RISK:        {"color": "#e0b64a", "pattern": "x"},
}


def _render_ro_buildup_chart(comp_df: pd.DataFrame) -> None:
    """Stacked bar per Portfolio × Format: FY27 probabilized → Gross Pipeline.

    Solid FY27 Probabilized base + a Year-effect increment (probabilized volume
    beyond the fiscal year) + a Risk / probability-headroom increment (the upside
    recoverable if win probability rose to 100%).  The three sum to the
    unweighted Gross Pipeline.  Millions of lbs.
    """
    wide = rpa.build_pipeline_buildup(comp_df)
    if wide.empty:
        st.info("No gross pipeline volume to build up under the current filters.")
        return
    cats = [str(x) for x in wide.index]
    fig = go.Figure()
    for seg in rpa.BUILDUP_SEGMENTS:
        style = _RO_BUILDUP_STYLE[seg]
        fig.add_bar(
            x=cats, y=(wide[seg] / 1e6).tolist(), name=seg,
            marker=dict(color=style["color"],
                        pattern=dict(shape=style["pattern"])),
            hovertemplate=f"%{{x}} · {seg}: %{{y:,.1f}}M lbs<extra></extra>",
        )
    fig.update_layout(
        barmode="stack", height=max(260, 30 * len(cats) + 100),
        margin=dict(l=10, r=10, t=40, b=10),
        font=dict(color=_RO_CHART_FONT, size=12),
        xaxis=dict(tickfont=dict(size=11)),
        yaxis=dict(title=dict(text="Pipeline (M lbs)"),
                   showgrid=True, gridcolor="#eeeeee", rangemode="tozero"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        plot_bgcolor="white",
    )
    st.plotly_chart(fig, use_container_width=True, key="ro_buildup_chart")


def _render_ro_pipeline_analytics_section() -> None:
    """RO pipeline headline metrics + charts, rendered ABOVE the RO Summary.

    Reads the in-memory per-program comparison frame (reacts to the field
    filters like the summary below).  Bails quietly when the comparison has
    not built yet — the RO Summary section prints the "not built" hint.  The
    body is fragment-isolated so the watchlist filters / editor rerun only this
    block, not the whole page.
    """
    comp = _ro_pipeline_comp_df()
    if comp is None:
        return
    _render_ro_pipeline_analytics_fragment(comp)


@st.fragment
def _render_ro_pipeline_analytics_fragment(comp: pd.DataFrame) -> None:
    """Fragment body of Pipeline at a Glance (tiles + charts + watchlist).

    Isolated in a fragment so a watchlist filter change / cell edit / archive
    click reruns only this section — no full-page rerun, no re-walk of the
    upstream Fabric reads.
    """
    st.markdown("### 🎯 Pipeline at a Glance")
    st.caption(
        "Headline read on the **current in-memory** RO pipeline — reacts to "
        "the field filters above.  _Risk-adjusted_ = probability-weighted "
        "(expected value); _Gross_ = unweighted annual opportunity."
    )
    # Filter-layer legibility: this whole section already reflects the section's
    # field filters; the watchlist below adds a SECOND, local filter layer.  Flag
    # when the field filters are active so a shrunk view isn't read as "no data".
    if any(sel for sel in _read_filter_state_from_session().values()):
        st.caption(
            "🔎 The **field filters above** are narrowing this section (tiles, "
            "charts, and watchlist).  The watchlist has its own filters on top."
        )
    # Canonical FY27 / FY28 Total B2C from the same roll-up the summary shows,
    # so the two headline tiles match the summary by construction.
    in_year_m, full_year_m = _ro_total_b2c_totals(comp)
    _render_ro_pipeline_tiles(comp, in_year_m=in_year_m, full_year_m=full_year_m)

    # Two charts side by side: urgency (left) + FY27→Gross build-up (right).
    left, right = st.columns(2)
    with left:
        st.markdown("#### 🔺 Urgency by Portfolio × Format & Ship Window")
        st.caption(
            f"{rpa.FY_CURRENT_LABEL} in-year probabilized volume per Portfolio "
            "Major × Supply Format, split by how soon each program is due to "
            "ship (Overdue → > 90 days)."
        )
        _render_ro_urgency_chart(comp)
    with right:
        st.markdown(f"#### 🧱 Pipeline Build-up ({rpa.FY_CURRENT_LABEL} → Gross)")
        st.caption(
            f"Per Portfolio Major × Supply Format: solid {rpa.FY_CURRENT_LABEL} "
            "probabilized, plus the Year-effect and Risk (probability-headroom) "
            "increments that build up to the unweighted Gross Pipeline."
        )
        _render_ro_buildup_chart(comp)

    # The paired high-urgency watchlist, full width below the charts.
    _render_ro_high_urgency_table(comp)


# ── RO Summary Report (hierarchical roll-up) ────────────────────────────────

def _render_summary_report_section() -> None:
    """Render the RO Summary Report header + delegate to the fragment.

    The header (title + caption) is kept OUTSIDE the fragment because
    it's static text — wrapping it inside would just add another stack
    frame to every fragment rerun for no benefit.  The fragment owns
    only the interactive widgets and the table itself.
    """
    st.markdown("### 📊 RO Summary Report")
    st.caption(
        "Hierarchical roll-up of the **current in-memory comparison** — "
        "FY27 Probabilized Lbs in millions, by Portfolio / Supply Format / "
        "Brand Category.  **Reacts to the field filters and the Prior / LE "
        "month pickers above** — narrow the filters and the table below "
        "recomputes.  Read-only presentation (edit values in the RO "
        "Comparison editor above); all-zero rows are hidden by default.  "
        "The Download / Save actions always publish the **full, unfiltered** "
        "30-row report so downstream consumers keep a stable shape."
    )
    _render_summary_report_fragment()


@st.fragment
def _render_summary_report_fragment() -> None:
    """Render the RO Summary Report editor + Save button.

    Sourcing model
    --------------
    The fragment consumes the **in-memory** comparison frame at
    ``_SS_SUMMARY_DF`` (NOT a fresh Fabric read).  This is what
    the planner asked for — picker changes propagate to the report
    on the same render with no extra Fabric round-trip.  When the
    planner explicitly wants to re-read what was last published,
    the consolidated **🔄 Refresh from Fabric** button at the top
    of the section clears every cache + rebuilds end-to-end.

    Rebuild trigger (cheap)
    -----------------------
    The fragment compares the comparison's ``_SS_MONTHS_SIG`` against
    its own last-built signature.  When they drift (e.g., on a month-
    picker change → full page rerun → fragment rerun), the report is
    rebuilt from scratch.  When a widget INSIDE the fragment reruns
    (cell edit, "show empty" toggle, Save button), the cached
    template is reused so planner edits are preserved.

    State model
    -----------
    * ``_SS_SUMMARY_REPORT_DF`` — full 30-row template (subtotals +
      leaves, possibly with planner edits).
    * ``_SS_SUMMARY_REPORT_RAW_DF`` — snapshot of the comparison
      frame the report was last built from (drives the Diagnostic
      expander).
    * ``_SS_SUMMARY_REPORT_SIG`` — the comparison signature that
      built the cached template (used to decide rebuild-vs-reuse).
    * Saved CSV always contains the full template (downstream
      consumers expect a stable shape).
    """
    summary_df: pd.DataFrame | None = st.session_state.get(_SS_SUMMARY_DF)
    if summary_df is None or summary_df.empty:
        # The comparison editor above hasn't built yet — nothing to
        # roll up.  Bail quietly; on the next render (typically the
        # same one, after ``_ensure_summary_in_session`` populates
        # the frame) we'll have the data.
        st.info(
            "ℹ️ The comparison table above has not built yet — once it "
            "loads, this section will populate automatically."
        )
        return

    months_sig = st.session_state.get(_SS_MONTHS_SIG)
    cached_sig = st.session_state.get(_SS_SUMMARY_REPORT_SIG)

    # ── Rebuild iff the source comparison signature drifted ───────
    needs_rebuild = (
        cached_sig != months_sig
        or _SS_SUMMARY_REPORT_DF not in st.session_state
    )
    if needs_rebuild:
        try:
            report_df, report_warnings, runtime_template = build_summary_report(summary_df)
        except RoSummaryReportError as exc:
            st.error(f"❌ Could not build the RO Summary Report.\n\n{exc}")
            return
        st.session_state[_SS_SUMMARY_REPORT_DF]        = report_df
        st.session_state[_SS_SUMMARY_REPORT_WARNINGS]  = report_warnings
        st.session_state[_SS_SUMMARY_REPORT_TEMPLATE]  = runtime_template
        st.session_state[_SS_SUMMARY_REPORT_LOADED_AT] = datetime.now()
        st.session_state[_SS_SUMMARY_REPORT_RAW_DF]    = summary_df.copy()
        st.session_state[_SS_SUMMARY_REPORT_SIG]       = months_sig
        # Republish the freshly built template to Fabric.  The manual Save
        # button below remains available; this auto-save is the planner's
        # safety net so downstream consumers (PLR R&O, Demand Plan
        # Comparison) always read the CURRENT in-memory view.
        _maybe_autosave_ro_summary_report(trigger="RO Summary rebuild")

    # ── Toolbar: status + Show-empty-rows toggle ──────────────────
    show_empty = st.session_state.setdefault(_SS_SUMMARY_REPORT_SHOW_ZERO, False)
    tb_status, tb_show = st.columns([5, 1.6])
    with tb_status:
        loaded_at = st.session_state.get(_SS_SUMMARY_REPORT_LOADED_AT)
        if loaded_at is not None:
            st.caption(
                f"Built from in-memory comparison at "
                f"**{loaded_at:%Y-%m-%d %H:%M:%S}**.  Click **🔄 Refresh "
                "from Fabric** at the top of the section to re-read the "
                "published CSV from scratch."
            )
    with tb_show:
        show_empty = st.checkbox(
            "Show empty rows",
            value=show_empty,
            key="ro_sr_show_empty_toggle",
            help=(
                "By default, rows whose every column is zero are hidden. "
                "Tick to reveal them — useful to see a row that currently "
                "has no upstream match under the active filters."
            ),
        )

    full_df: pd.DataFrame = st.session_state[_SS_SUMMARY_REPORT_DF]
    raw_df:  pd.DataFrame = st.session_state.get(
        _SS_SUMMARY_REPORT_RAW_DF, pd.DataFrame(),
    )

    # ── Diagnostic expander (always before warnings) ─────────────
    _render_summary_report_diagnostic(raw_df)

    # ── Optional warnings ────────────────────────────────────────
    report_warnings: list[str] = st.session_state.get(_SS_SUMMARY_REPORT_WARNINGS, [])
    _render_summary_report_warnings(report_warnings)

    # ── Dynamic display frame — react to the RO Comparison filters ─
    #
    # The persisted report (full_df) is ALWAYS built from the unfiltered
    # comparison so the Download/Save shape stays stable for downstream
    # consumers.  For the on-screen table only, we re-roll the report over
    # the field-filtered comparison so the planner sees exactly the slice
    # their filters select.  Cheap (≤30-row template) so it's fine to do
    # every render; skipped entirely when no filter is active.
    filter_state = _read_filter_state_from_session()
    filters_active = any(sel for sel in filter_state.values())
    if filters_active:
        try:
            display_full, _fw, _ft = build_summary_report(
                _apply_filters(summary_df, filter_state)
            )
        except RoSummaryReportError as exc:
            st.warning(
                "Could not apply the field filters to the report "
                f"({exc}); showing the unfiltered roll-up."
            )
            display_full = full_df
    else:
        display_full = full_df

    display_df = display_full if show_empty else drop_all_zero_rows(display_full)

    if filters_active:
        st.caption(
            f"🔎 Filtered by the field filters above — showing "
            f"**{len(drop_all_zero_rows(display_full))}** non-empty rows."
        )

    # ── Render the read-only, screenshot-styled report ────────────
    if display_df.empty:
        st.info(
            "No rows match the current filters / month pair.  Clear a "
            "field filter above, tick **Show empty rows**, or change the "
            "Prior / LE pickers to a month pair with comparison data."
        )
    else:
        _render_ro_summary_header_editor()
        _render_ro_summary_html(display_df)
        with st.expander("ℹ️ What the FY28 Probabilized columns mean", expanded=False):
            st.markdown(
                "**FY28 Probabilized** is the risk-adjusted demand plan for the "
                "**next fiscal year** (the first full year beyond the current "
                "one).  *Probabilized* means every line is weighted by how likely "
                "it is to land — an expected-value view, not a best-case wish "
                "list — so it's a sober read on where next year is trending.\n\n"
                "**Why it's here:** the FY27 columns tell you what this cycle did "
                "to the *current* year; these three tell you what it did to "
                "**next** year — the early-warning signal so a shift shows up a "
                "year out, while there's still time to act on capacity, pricing, "
                "and customer commitments.\n\n"
                "**How to read it:** **Prior** = where next year stood before this "
                "cycle; **Latest** = where it stands now; **Change** = the swing "
                "(Latest − Prior).  A large **Change** — especially concentrated "
                "in one segment — is the line to investigate: it's this cycle "
                "quietly re-shaping the forward year."
            )

    # ── Download + Save — always the FULL, unfiltered report ─────
    #
    # Read-only display, so nothing is merged back; recompute subtotals
    # defensively (idempotent) so a session-reloaded frame still balances.
    export_ready = recompute_subtotals(
        full_df,
        st.session_state.get(_SS_SUMMARY_REPORT_TEMPLATE),
    )
    _render_summary_report_actions(export_ready)


def _render_summary_report_actions(export_df: pd.DataFrame) -> None:
    """Render Download + Save controls for the RO Summary Report.

    Placed **after** the data editor so both actions reflect the
    planner's latest in-session edits (merged back into the full
    template + subtotals recomputed).  Layout matches other editable
    sections on this page and across the app: ``⬇️ Download … (CSV)``
    label, ``text/csv`` mime, ``YYYYMMDD`` filename suffix, primary
    styling on the download action.

    Parameters
    ----------
    export_df
        Full 30-row template with subtotals reconciled — the same
        frame passed to :func:`save_ro_summary_report`.
    """
    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    row_count = len(export_df)

    dl_col, save_col = st.columns([1, 1])
    with dl_col:
        st.download_button(
            label="⬇️ Download RO Summary Report (CSV)",
            data=summary_to_csv_bytes(export_df),
            file_name=f"RO_Summary_Report_{today}.csv",
            mime="text/csv",
            key="ro_sr_download",
            type="primary",
            use_container_width=True,
            help=(
                "Downloads the current report as a CSV — same column "
                "headers and row shape as "
                "`Files/RO Tracking/RO_Reporting/RO_Summary_Report.csv` "
                "(full template, all rows including zeros).  Includes "
                "any edits you just made in the table above."
            ),
        )
    with save_col:
        if st.button(
            "💾 Save RO_Summary_Report.csv (overwrite)",
            key="ro_sr_save",
            type="primary",
            use_container_width=True,
            help=(
                "Overwrites `Files/RO Tracking/RO_Reporting/RO_Summary_Report.csv` "
                "with the FULL 30-row template (subtotals + every leaf, including "
                "all-zero rows) so downstream consumers get a stable shape."
            ),
        ):
            try:
                with st.spinner("Saving RO_Summary_Report.csv to Microsoft Fabric…"):
                    blob_path = save_ro_summary_report(export_df)
            except RoSummaryReportError as exc:
                st.error(f"❌ Save failed.\n\n{exc}")
            except Exception as exc:  # noqa: BLE001 — surface any other failure
                logger.exception("Unexpected error saving RO_Summary_Report.csv")
                st.error(
                    "❌ Save failed unexpectedly — the file was not written.\n\n"
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                st.success(f"✅ Saved to `Files/{blob_path}` ({row_count} rows).")


def _render_summary_report_warnings(warnings: list[str]) -> None:
    """Render any genuine roll-up warnings as a foldable expander.

    Per planner direction the per-leaf "0 rows matched" notes are NOT
    emitted by :func:`build_summary_report` — those rows are simply
    hidden by :func:`drop_all_zero_rows` before display, and the
    **🔬 Diagnostic** expander already surfaces the literal dim
    values so a planner can spot real spelling drift.  This function
    therefore only renders something when ``build_summary_report``
    emits a *real* warning (e.g., a missing required column or a
    coercion failure).
    """
    if not warnings:
        return
    with st.expander(
        f"⚠️ {len(warnings)} note(s) from the last roll-up",
        expanded=False,
    ):
        for note in warnings:
            st.markdown(f"- {note}")


def _render_summary_report_diagnostic(raw_df: pd.DataFrame) -> None:
    """Render the read-only Diagnostic expander above the warnings list.

    Shows the unique values + row counts for every dim column in the
    just-loaded ``RO_Comparison_Output.csv`` so a planner can
    self-diagnose "0 matches" warnings: if my template expects
    ``SFmt = "Gallon Jug"`` but the CSV has ``"1 Gallon Jug"``,
    the diagnostic will surface the actual literal in 2 seconds.

    All compute is delegated to
    :func:`ro_summary_report.diag_dim_summary` so this function is
    only responsible for layout.  Default-collapsed because most
    runs have nothing wrong.
    """
    if raw_df is None or raw_df.empty:
        return

    diag = diag_dim_summary(raw_df)

    with st.expander(
        "🔬 Diagnostic — unique dim values in `RO_Comparison_Output.csv`",
        expanded=False,
    ):
        st.caption(
            f"**{len(raw_df):,} total rows** in the loaded CSV.  Compare the "
            "literal strings below against the template's match criteria — "
            "any whitespace / casing / synonym mismatch is the most common "
            "cause of a *0 rows matched* warning."
        )

        # Four side-by-side value-count tables (PMaj / SFmt / PMinor / Brand).
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Portfolio Major** values (rows)")
            _render_diag_value_table(diag["unique_pmaj"])
            st.markdown("**Portfolio Minor** values (rows)")
            _render_diag_value_table(diag["unique_pminor"])
        with c2:
            st.markdown("**Supply Format** values (rows)")
            _render_diag_value_table(diag["unique_sfmt"])
            st.markdown("**Brand** values (rows) → derived `Brand Category`")
            _render_diag_value_table(diag["unique_brand"])

        st.markdown(
            "**(Portfolio Major, Supply Format, Portfolio Minor, Brand Category) "
            "combinations** — what the template actually filters against:"
        )
        st.dataframe(
            diag["combo_full"],
            use_container_width=True,
            height=min(35 * (len(diag["combo_full"]) + 1) + 38, 360),
            hide_index=True,
        )


def _render_diag_value_table(df: pd.DataFrame) -> None:
    """Render one diagnostic value-count frame as a compact table."""
    if df.empty:
        st.caption("_(no rows)_")
        return
    st.dataframe(
        df,
        use_container_width=True,
        height=min(35 * (len(df) + 1) + 38, 240),
        hide_index=True,
    )


# ── RO Summary Report — read-only screenshot-styled presentation ─────────────
#
# Column groups exactly as the planner's screenshot: three banded groups over
# nine metric columns, plus the indented hierarchy label.  A native
# st.dataframe / st.data_editor cannot render a two-row grouped header band or
# the vertical dividers bracketing the Delta Breakdown, so the table is
# emitted as a small, self-contained HTML block (read-only by nature — edits
# happen in the RO Comparison editor above, which feeds this roll-up).
_RO_SR_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("FY27 Probabilized", (
        (SR_COL_PRIOR_PLAN, "Prior Plan"),
        (SR_COL_CURRENT_PLAN, "Current Plan"),
        (SR_COL_TOTAL_DELTA, "Total Δ"),
    )),
    ("Delta Breakdown", (
        (SR_COL_DELTA_NEW, "New"),
        (SR_COL_DELTA_EXIT, "Exit"),
        (SR_COL_DELTA_CHANGE, "Change"),
    )),
    # Prior → Latest → Change (planner-requested order: where it started, where
    # it landed, then the swing).  Group renamed FY28 Probabilized (see below).
    ("FY28 Probabilized", (
        (SR_COL_Y1_PRIOR, "Prior"),
        (SR_COL_Y1_LATEST, "Latest"),
        (SR_COL_Y1_CHANGE, "Change"),
    )),
)
# First metric column of each group carries the vertical divider.
_RO_SR_GROUP_START_COLS: frozenset[str] = frozenset(
    cols[0][0] for _grp, cols in _RO_SR_GROUPS
)
# Scoped CSS — navy header band, light-blue Total B2C, orange section rows,
# bold subtotals, group dividers.  Explicit light-surface colours so the
# table reads the same in either Streamlit theme.
_RO_SR_CSS: str = """
<style>
.ro-sr {overflow-x:auto; margin:0.25rem 0 0.75rem;}
.ro-sr table {border-collapse:collapse; width:100%;
  font-size:0.95rem; background:#ffffff; color:#1a1a1a;}
.ro-sr th, .ro-sr td {padding:4px 10px; white-space:nowrap;}
.ro-sr thead th {background:#1f3864; color:#ffffff; font-weight:700;
  text-align:center; border:1px solid #2f4a7a;}
.ro-sr tbody td {border-bottom:1px solid #e8e8e8; text-align:right;}
.ro-sr td.lbl {text-align:left;}
.ro-sr .grp {border-left:2px solid #1f3864;}
.ro-sr tr.total td {background:#dce6f1; font-weight:700;}
.ro-sr tr.section td {background:#f8cbad; font-weight:700;}
.ro-sr tr.subtotal td {font-weight:700;}
</style>
"""


# ── Editable headers (session-scoped) ────────────────────────────────────────
# The group + sub-column labels above are the DEFAULTS.  A planner can rename
# any header via the editor expander; the override lives in session_state
# (keyed per group index / per column id) and the table reads it back.  Scope is
# the current session — a full page reload restores the defaults.
def _ro_sr_group_label(i: int) -> str:
    """Effective group-band label for group *i* (session override or default)."""
    return st.session_state.get(f"ro_sr_grp_{i}") or _RO_SR_GROUPS[i][0]


def _ro_sr_sub_default(col_id: str) -> str:
    """The code-default sub-column label for *col_id*."""
    for _grp, cols in _RO_SR_GROUPS:
        for col, label in cols:
            if col == col_id:
                return label
    return col_id


def _ro_sr_sub_label(col_id: str) -> str:
    """Effective sub-column label for *col_id* (session override or default)."""
    return st.session_state.get(f"ro_sr_sub_{col_id}") or _ro_sr_sub_default(col_id)


def _render_ro_summary_header_editor() -> None:
    """Foldable editor to rename any RO-Summary header (session-scoped)."""
    with st.expander("✏️ Edit report headers", expanded=False):
        st.caption(
            "Rename any column header — changes apply to the table below for "
            "this session (a full page reload restores the defaults)."
        )
        editor_cols = st.columns(len(_RO_SR_GROUPS))
        for i, (grp_name, sub_cols) in enumerate(_RO_SR_GROUPS):
            with editor_cols[i]:
                gkey = f"ro_sr_grp_{i}"
                st.session_state.setdefault(gkey, grp_name)
                st.text_input("Group band", key=gkey)
                for col_id, label in sub_cols:
                    skey = f"ro_sr_sub_{col_id}"
                    st.session_state.setdefault(skey, label)
                    # Widget label = the default, so the planner can see which
                    # column each input renames even after editing its value.
                    st.text_input(f"↳ {label}", key=skey)


def _fmt_ro_sr_num(value: object) -> str:
    """Format a millions value like the screenshot: '-' / '46.1' / '(17.8)'."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    if pd.isna(num) or abs(num) < 0.05:  # rounds to zero at 1-dp display
        return "-"
    return f"({abs(num):.1f})" if num < 0 else f"{num:.1f}"


def _esc_html(text: object) -> str:
    """Minimal HTML escape (NBSP indent in labels is preserved verbatim)."""
    return (
        str(text)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _ro_sr_row_class(row: pd.Series) -> str:
    """CSS class for one report row: Total B2C / section / subtotal / leaf."""
    if str(row.get(SR_COL_ROW_ID, "")) == "total_b2c":
        return "total"
    is_subtotal = bool(row.get(SR_COL_IS_SUBTOTAL, False))
    if is_subtotal and int(row.get(SR_COL_INDENT, 0) or 0) == 1:
        return "section"  # ESL / Aseptic / Cultured / Fresh Milk / Butter
    return "subtotal" if is_subtotal else ""


def _render_ro_summary_html(df: pd.DataFrame) -> None:
    """Render the RO Summary Report as the screenshot-styled, read-only table.

    Two-row banded header (FY27 Probabilized · Delta Breakdown · Year 1
    Probabilized) over the nine metric columns, vertical dividers between
    groups, a light-blue Total B2C row, orange section rows, and bold
    subtotal numbers.  Values are millions, formatted '-' / '46.1' / '(17.8)'.
    """
    data_cols = [col for _grp, cols in _RO_SR_GROUPS for col, _lbl in cols]

    # ── Header: group band + sub-column row (labels are session-editable) ──
    band = ['<th></th>']
    for i, (_group_name, cols) in enumerate(_RO_SR_GROUPS):
        band.append(
            f'<th class="grp" colspan="{len(cols)}">'
            f'{_esc_html(_ro_sr_group_label(i))}</th>'
        )
    subhead = ['<th class="lbl">Millions of lbs.</th>']
    for _grp, cols in _RO_SR_GROUPS:
        for col, _label in cols:
            cls = ' class="grp"' if col in _RO_SR_GROUP_START_COLS else ""
            subhead.append(f'<th{cls}>{_esc_html(_ro_sr_sub_label(col))}</th>')

    # ── Body rows ─────────────────────────────────────────────────
    body: list[str] = []
    for _idx, row in df.iterrows():
        row_cls = _ro_sr_row_class(row)
        tr_open = f'<tr class="{row_cls}">' if row_cls else "<tr>"
        cells = [f'<td class="lbl">{_esc_html(row.get(SR_COL_LABEL, ""))}</td>']
        for col in data_cols:
            grp = " grp" if col in _RO_SR_GROUP_START_COLS else ""
            cells.append(f'<td class="{grp.strip()}">{_fmt_ro_sr_num(row.get(col))}</td>')
        body.append(tr_open + "".join(cells) + "</tr>")

    html = (
        f'{_RO_SR_CSS}<div class="ro-sr"><table>'
        f'<thead><tr>{"".join(band)}</tr><tr>{"".join(subhead)}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )
    st.markdown(html, unsafe_allow_html=True)


# ── Demand Summary (Fabric CSV preview + download) ──────────────────────────
#
# Section flow
# ------------
# 1. Single foldable expander wrapping the whole section so the planner
#    can collapse the two preview tables when not in use — same
#    pattern as the "🔁 RO Comparison" expander above.
# 2. Single "🔄 Refresh from Fabric" button.  One click invalidates BOTH
#    cached snapshots so the planner gets a consistent re-read pair —
#    matches the consolidated refresh model the RO Comparison section
#    uses.
# 3. Two side-by-side sub-sections (one per file).  Each one renders:
#       a. Metadata caption (rows × cols, blob path, last_modified UTC).
#       b. Prominent ⬇️ download button BEFORE the table — keeps the
#          export action in the "above the fold" area, exactly where
#          the planner is told to look.
#       c. Top-20-row preview rendered via ``st.dataframe`` (read-only,
#          no editor — these tables are pure outputs, not inputs).
#
# Section keys (Streamlit widget identity)
# ----------------------------------------
# Kept distinct from any other download / refresh key on the page so a
# rerun doesn't accidentally re-trigger the wrong button.

# Number of rows shown in each preview.  Pinned at 5 per the planner's
# direction — the preview is an EXAMPLE EXTRACT only.  Anyone who
# wants the full dataset clicks the prominent download button above
# the preview, which streams the byte-for-byte source CSV from Fabric.
# Keeping the preview tiny also makes the section render instantly
# even on the largest sources (millions of rows).
_DEMAND_SUMMARY_PREVIEW_ROWS: int = 5


def _format_last_modified_utc(ts) -> str:
    """Return a planner-friendly ``"YYYY-MM-DD HH:MM UTC"`` for a snapshot ts.

    Handles ``datetime`` (timezone-aware or naive), ``pandas.Timestamp``,
    and ``None``.  Naive timestamps are assumed to be UTC — same
    convention the rest of the page uses.
    """
    if ts is None:
        return "unknown"
    try:
        # ``pd.to_datetime`` normalises every flavour we accept.
        normalised = pd.to_datetime(ts, utc=True)
    except (TypeError, ValueError):
        return "unknown"
    return normalised.strftime("%Y-%m-%d %H:%M UTC")


def _demand_preview_column_config(df: pd.DataFrame) -> dict:
    """Return a ``column_config`` mapping that formats date columns as dates.

    Auto-detects every ``datetime64[*]`` column in *df* and assigns
    ``st.column_config.DateColumn(format="YYYY-MM-DD")`` so the
    preview shows the date without the misleading ``00:00:00`` time
    component pandas attaches when round-tripping a date through
    ``datetime64[ns]``.  Non-date columns are left to Streamlit's
    auto-detection.
    """
    cc = st.column_config
    config: dict = {}
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            config[col] = cc.DateColumn(col, format="YYYY-MM-DD")
    return config


def _render_demand_summary_table(
    *,
    title: str,
    icon: str,
    snapshot: DemandSummarySnapshot,
    download_basename: str,
    download_button_key: str,
) -> None:
    """Render one Demand Summary file: metadata caption + ⬇️ + tiny example extract.

    Parameters
    ----------
    title
        Human-readable section heading (e.g., "qry_mgmt_plan_full").
    icon
        Single emoji used as a leading badge in the heading.
    snapshot
        :class:`DemandSummarySnapshot` returned by the connector.
    download_basename
        Filename stem used for the CSV download (no extension, no
        date — appended below).  Pinned to the source filename for
        traceability.
    download_button_key
        Stable Streamlit widget key — must be unique across the page.

    UX rationale
    ------------
    The preview on this page is an **example extract** (first
    :data:`_DEMAND_SUMMARY_PREVIEW_ROWS` rows), not a working table.
    Planners who need to inspect, slice, or hand off the data go
    through the prominent download button at the top of the section —
    its label and the caption above the preview both spell out that
    the displayed rows are illustrative only.  Keeping the preview
    tiny is intentional: the source CSVs can carry millions of rows
    and pushing that into the browser would freeze the tab.
    """
    st.markdown(f"#### {icon} {title}")
    last_mod = _format_last_modified_utc(snapshot.last_modified)
    size_bit = (
        f" · **{snapshot.size:,}** bytes" if snapshot.size is not None else ""
    )
    st.caption(
        f"🛰️ Source: `Files/{snapshot.blob_path}` · "
        f"Last modified **{last_mod}**{size_bit} · "
        f"**{snapshot.row_count:,}** rows · "
        f"**{snapshot.column_count}** columns"
    )

    # ── Download button (above the preview — the canonical path) ────
    #
    # We hand the user the RAW bytes from OneLake (via
    # ``fetch_raw_bytes``) instead of a re-serialised
    # ``snapshot.df.to_csv(...)``.  Re-serialising would drop trailing
    # newlines, coerce numeric types (e.g. integer item codes → floats),
    # rewrite quoting style, and locale-format dates — every one of
    # which is a recurring class of breakage for downstream Excel /
    # pivot-table consumers.  Raw bytes preserve byte-for-byte fidelity
    # with what's in Fabric, which is what the planner expects when
    # they hand the file to someone else.
    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    try:
        raw_bytes = fetch_demand_summary_raw_bytes(snapshot.blob_path)
    except DemandSummaryError as exc:
        # Don't block the preview on a download-bytes failure — surface
        # an inline warning so the planner can still inspect the data.
        st.warning(
            f"⚠️ Could not prepare the download for `{snapshot.blob_path}`."
            f"\n\nDetails: {exc}"
        )
    else:
        st.download_button(
            label=(
                f"⬇️ Download FULL `{download_basename}.csv` "
                f"({snapshot.row_count:,} rows from Fabric)"
            ),
            data=raw_bytes,
            file_name=f"{download_basename}_{today}.csv",
            mime="text/csv",
            key=download_button_key,
            type="primary",
            help=(
                f"Downloads a byte-for-byte copy of "
                f"`Files/{snapshot.blob_path}` straight from Microsoft "
                "Fabric — no re-serialisation, so numeric types / "
                "quoting / line endings match the source exactly. "
                "This is the canonical path to the data; the preview "
                "below is just an example extract."
            ),
        )

    # ── Tiny example extract (NOT a working table) ──────────────────
    #
    # Read-only preview only — the planner doesn't edit these tables.
    # ``st.dataframe`` (not ``st.data_editor``) keeps the widget light
    # and renders the data with built-in column sorting.  Pinning the
    # row count to ``_DEMAND_SUMMARY_PREVIEW_ROWS`` keeps the section
    # vertically bounded regardless of how big the underlying file is
    # — the download button above is the path to the full data.
    n_shown = min(_DEMAND_SUMMARY_PREVIEW_ROWS, snapshot.row_count)
    st.markdown(
        f"**📄 Example extract** — first **{n_shown:,}** of "
        f"**{snapshot.row_count:,}** rows.  Use the download button "
        "above to get the full file."
    )
    preview = snapshot.df.head(_DEMAND_SUMMARY_PREVIEW_ROWS)
    # Compact height — 5 rows + header fits in <200 px so the section
    # never crowds the page; download button stays inside the viewport.
    preview_height = 36 * (len(preview) + 1) + 38
    st.dataframe(
        preview,
        width="stretch",
        height=preview_height,
        # Render any datetime-typed column as a clean ``YYYY-MM-DD``
        # date instead of Streamlit's default ``YYYY-MM-DD HH:MM:SS``
        # display.  The connector already coerces ``Start of Month``
        # from its raw Excel-serial form to ``datetime64`` (see
        # ``_coerce_demand_dates_for_display``); this config makes the
        # output look like the planner expects.  Auto-detected so the
        # same renderer works for any other date column either file
        # might add in the future.
        column_config=_demand_preview_column_config(preview),
    )


# Session-state key for the last Demand Plan pipeline run (survives reruns).
_SS_DEMAND_PIPELINE_RESULT = "_demand_plan_pipeline_result"


def _render_base_plan_uploader() -> None:
    """Upload a new Base Plan → run the in-app Demand Plan ETL → auto-refresh.

    Mirrors the RO uploader (:func:`_render_customer_input_uploader`): on **Run**
    the former Fabric-notebook pipeline runs in-app
    (:func:`run_demand_plan_pipeline`) — rebuilding ``tbl_ro_input``,
    ``qry_mgmt_plan_full``, ``qry_demand_item_customer_detail`` and
    ``qry_total_item_level_demand``, and appending the cycle-over-cycle history
    tracker with the upload's authored ``Cycle``. On success it flushes the
    section's caches and reruns, so the tables below reflect the new plan with no
    extra click.
    """
    with st.expander("⬆️ Upload a new Base Plan (run the Demand Plan pipeline)",
                     expanded=False):
        st.caption(
            "Upload the new **`ibp_base_plan_current`** export. It must include "
            "the usual plan columns plus a **`month`** column (the demand-review "
            "meeting month) and a **`Cycle`** column (e.g. `C5`). On **Run** the "
            "app archives the upload, rebuilds every demand-plan file, appends "
            "the history tracker as that cycle, and refreshes this section — no "
            "Fabric notebook needed. RO_Seed is used as-is (download it in the "
            "RO Comparison section to verify it's current)."
        )
        uploaded = st.file_uploader(
            "Upload ibp_base_plan_current.csv",
            type=["csv"],
            key="demand_base_plan_upload",
            label_visibility="collapsed",
        )

        c1, c2 = st.columns(2)
        with c1:
            anchor_month = st.date_input(
                "RO calendar anchor (Month 1)",
                value=date(2026, 4, 1),
                format="YYYY-MM-DD",
                key="demand_base_plan_anchor",
                help="First month of the R&O 36-month calendar (was the "
                     "notebook's ANCHOR_MONTH). Defaults to 2026-04-01.",
            )
        with c2:
            window_months = st.number_input(
                "Forward window (months)",
                min_value=1, max_value=60, value=24, step=1,
                key="demand_base_plan_window",
                help="Rows beyond (meeting month + this many months) are dropped. "
                     "No lower bound — historical months are always kept.",
            )

        override_on = st.checkbox(
            "Override forward-window month (default: use the upload's `month`)",
            value=False, key="demand_base_plan_override_on",
        )
        meeting_override = None
        if override_on:
            meeting_override = st.date_input(
                "Forward-window month",
                value=date.today().replace(day=1),
                format="YYYY-MM-DD",
                key="demand_base_plan_override_month",
            )

        run_clicked = st.button(
            "▶️ Run Demand Plan pipeline & Save to Fabric",
            key="demand_base_plan_run",
            type="primary",
            disabled=uploaded is None,
            help="Builds tbl_ro_input, qry_mgmt_plan_full, the item×customer "
                 "detail and total item-level demand, then appends the history "
                 "tracker — all written to Microsoft Fabric.",
        )

        if run_clicked and uploaded is not None:
            with st.spinner(
                "Running the Demand Plan pipeline (RO_Seed → tbl_ro_input → "
                "mgmt_plan_full → detail → total item-level → history)…"
            ):
                result = run_demand_plan_pipeline(
                    uploaded.getvalue(),
                    anchor_month=anchor_month,
                    forward_window_months=int(window_months),
                    meeting_month_override=meeting_override,
                )
            st.session_state[_SS_DEMAND_PIPELINE_RESULT] = result
            if result.ok:
                # The pipeline just rewrote every demand-plan file. Flush ALL
                # @st.cache_data so the Demand Summary, Comparison (shape-signature
                # build caches) and PLR re-read fresh, then rerun.
                st.cache_data.clear()
                st.rerun()

        result: Optional[DemandPlanResult] = st.session_state.get(
            _SS_DEMAND_PIPELINE_RESULT)
        if result is not None:
            _render_demand_plan_pipeline_summary(result)


def _render_demand_plan_pipeline_summary(result: DemandPlanResult) -> None:
    """Render the foldable summary for the last Demand Plan pipeline run."""
    title = ("📦 Demand Plan pipeline — last run "
             + ("✅ success" if result.ok else "❌ failed"))
    with st.expander(title, expanded=True):
        if result.ok:
            st.success(
                f"Pipeline completed for cycle **{result.cycle}**. "
                "tbl_ro_input, qry_mgmt_plan_full, the item×customer detail, "
                "total item-level demand, and the history tracker were updated "
                "in Fabric. This section refreshed automatically."
            )
        else:
            st.error(
                "Pipeline did **not** complete — no files were written unless a "
                "specific write is listed in the log below. Fix the issue(s) "
                "and re-run."
            )

        for err in result.errors:
            st.error(f"❌ {err}")
        if result.warnings:
            st.warning("**Please review these warnings:**\n\n"
                       + "\n".join(f"- {w}" for w in result.warnings))

        if result.ok:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("mgmt_plan_full rows", f"{result.mgmt_full_rows:,}"
                      if result.mgmt_full_rows is not None else "—")
            m2.metric("detail rows", f"{result.detail_rows:,}"
                      if result.detail_rows is not None else "—")
            m3.metric("history rows", f"{result.history_rows:,}"
                      if result.history_rows is not None else "—")
            m4.metric("total Demand Plan lbs", f"{result.mgmt_total_lbs:,.0f}"
                      if result.mgmt_total_lbs is not None else "—")
            if result.meeting_month and result.window_end:
                st.caption(
                    f"Cycle **{result.cycle}** · meeting month "
                    f"**{result.meeting_month:%Y-%m-%d}** · forward window "
                    f"< **{result.window_end:%Y-%m-%d}**"
                )

        with st.expander("Full run log", expanded=not result.ok):
            for entry in result.log:
                icon = _LOG_LEVEL_ICON.get(entry.level, "•")
                st.markdown(f"{icon} {entry.text}")


def _render_demand_summary() -> None:
    """Render the Demand Summary section end-to-end inside a foldable expander.

    Loads both Demand Plan CSVs from Microsoft Fabric (with the same
    15-minute TTL cache the rest of the page uses), shows a tiny
    example extract of each, and exposes prominent download buttons
    for the full files.

    Wrapped in ``st.expander(expanded=False)`` to match the foldable
    pattern used by the other dashboards on this page — collapsed by
    default to keep the page lightweight; expanding triggers the
    Fabric reads (or hits the cache on a hot rerun).

    Error handling
    --------------
    A genuine Fabric I/O failure is surfaced as an inline error and
    the section returns early — every OTHER section on the page is
    unaffected (we render the whole thing inside a self-contained
    expander).  An auth gate matches the RO Comparison gate so the
    planner sees the same "Sign in via Home" hint instead of an
    exception stack.
    """
    with st.expander("📈 Demand Summary (IBP)", expanded=False):
        st.caption(
            "Latest **Demand Plan** CSV exports from Microsoft Fabric.  "
            f"Each table below is just an **example extract** "
            f"(first {_DEMAND_SUMMARY_PREVIEW_ROWS} rows) — use the "
            "prominent download button above each preview to grab the "
            "full file.  Reads `Files/RO Tracking/Demand Plan/` and "
            "inherits the same 15-min cache cadence as the RO Comparison "
            "section."
        )

        # Fabric auth gate — match the RO Comparison gate so the planner
        # sees one consistent "sign-in needed" message across the page
        # rather than two slightly-different banners.
        if not fabric_signin_widget.is_fabric_signed_in():
            st.warning(
                "🔒 **Microsoft Fabric is not connected.**\n\n"
                "Please visit **Home & Fabric Sign-in** in the sidebar to "
                "sign in.  Once signed in, return here — the Demand Summary "
                "tables will load automatically."
            )
            return

        # Upload a new Base Plan → run the in-app Demand Plan pipeline.
        # The history tracker is appended (with the upload's authored Cycle)
        # by that pipeline, so this Refresh button only re-reads.
        _render_base_plan_uploader()

        # Consolidated "Refresh from Fabric" button.  One click re-reads the
        # ENTIRE section from the lakehouse — not just the two demand summary
        # CSVs, but the Demand Plan Comparison summary and the Product Line
        # Review below it.
        #
        # Why a full ``st.cache_data.clear()`` and not just
        # ``clear_demand_summary_cache()``: the comparison + PLR pull
        # several *other* Fabric sources (IBP Shipments/Orders, the PDH /
        # customer / ship-to dims, the RO Summary delta, the FY27 budget
        # workbook) behind their own caches, and their build outputs are
        # keyed on a cheap ``(rows, cols)`` shape signature — so a content
        # change that leaves the shape intact would otherwise serve a
        # stale build even after the raw reads refresh.  Flushing every
        # ``@st.cache_data`` slot (the same primitive the Market Barometer
        # "Refresh from Fabric" uses) is the only way to guarantee all
        # three sub-sections move together on one click.
        if st.button(
            "🔄 Refresh from Fabric",
            key="demand_summary_refresh_from_fabric",
            help=(
                "Re-read this whole section from Microsoft Fabric — the Demand "
                "Summary CSVs, the Demand Plan Comparison summary, and the "
                "Product Line Review — bypassing every data cache."
            ),
        ):
            st.cache_data.clear()
            st.rerun()

        # Load both files.  We catch errors PER FILE so a failure on
        # one source doesn't hide the other (common case: one of the
        # upstream queries is still running and its CSV is missing,
        # while the other is already published).
        st.markdown("")  # vertical breathing room before the first table.
        _render_demand_summary_file(
            title="Management Plan (Full)",
            icon="📋",
            fetch_fn=fetch_mgmt_plan_full,
            blob_path_fn=mgmt_plan_full_blob_path,
            download_basename="qry_mgmt_plan_full",
            download_button_key="demand_summary_dl_mgmt_plan_full",
        )

        st.markdown("---")
        _render_demand_summary_file(
            title="Total Item-Level Demand",
            icon="📦",
            fetch_fn=fetch_total_item_level_demand,
            blob_path_fn=total_item_level_demand_blob_path,
            download_basename="qry_total_item_level_demand",
            download_button_key="demand_summary_dl_total_item_level_demand",
        )

        # ── Demand Pivot Summary (hierarchical roll-up) ─────────────
        #
        # Lives below the two raw-CSV previews because it's a derived
        # view of the second file (qry_total_item_level_demand.csv).
        # The horizontal rule keeps the visual separation crisp so
        # planners don't conflate the raw preview with the roll-up.
        st.markdown("---")
        _render_demand_pivot_section()

        # ── Demand Plan Comparison Summary (cycle-over-cycle) ───────
        #
        # Sits directly below the MOM roll-up.  Pulls plan numbers from
        # the plan-history tracker, actuals from IBP Shipments, and
        # dimensions/brand from PDH — see ``demand_plan_comparison``.
        st.markdown("---")
        _render_demand_plan_comparison_section()


# Session key holding the last upload-build result so download / preview
# interactions don't re-run the transform.
_APS_UPLOAD_RESULT_KEY: str = "aps_upload_result"
# Bumped after a successful build so the file_uploader widget resets (a re-run
# doesn't re-ingest the same file).
_APS_UPLOAD_NONCE_KEY: str = "aps_upload_nonce"
# Bumped after a successful corp-group patch so the patch uploader resets.
_APS_PATCH_NONCE_KEY: str = "aps_patch_nonce"


@st.cache_data(ttl=900, show_spinner=False)
def _cached_persisted_aps_plan() -> Optional[pd.DataFrame]:
    """Cached read of the persisted APS plan from Fabric (``None`` if absent).

    Cached so the existence-check + preview/download don't re-read the (large)
    CSV on every rerun; cleared explicitly after a successful build so the fresh
    file is picked up.
    """
    df, _etag = load_persisted_aps_plan()
    return df


def _render_aps_corp_review(history: Optional[pd.DataFrame]) -> None:
    """R&O Corporate Group review + patch — read/written straight on the history file.

    The APS base-plan leg is attributed deterministically (plan-to bridge +
    native code), so only the R&O leg's **fuzzy / Unmapped** customers warrant a
    look.  Works off the already-loaded ``qry_mgmt_plan_full_aps_history.csv``
    (*history*, read once by the caller): **download** the list, correct the
    **Corporate Group** cells, **upload** it back, and **Apply patch** rewrites
    *only* those still-reviewable R&O rows on the history file (exact matches and
    prior patches are left untouched, so earlier fixes never need redoing).
    """
    if not fabric_signin_widget.is_fabric_signed_in():
        return
    with st.expander("🧾 R&O Corporate Group review + patch", expanded=False):
        st.caption(
            "R&O **Customers** whose Corporate Group is still **Fuzzy** or "
            "**Unmapped** on the APS history tracker (Unmapped first).  To fix: "
            "**download** the list, correct the **Corporate Group** cells, "
            "**upload** it back, and click **Apply patch** — that rewrites *only* "
            "those still-reviewable R&O rows directly on the history file "
            "(exact matches and earlier patches are left alone)."
        )
        if history is None or history.empty:
            st.info(
                "ℹ️ No APS history yet — build a cycle in **Demand Summary (APS / Oracle)** "
                "above; the review lights up once the history file exists."
            )
            return
        review = build_corp_review(history)
        n_review = len(review)
        if n_review:
            st.warning(
                f"⚠️ **{n_review}** R&O customer(s) need a Corporate Group review."
            )
        if review.empty:
            st.success("✅ Every R&O customer already has a resolved Corporate Group.")
        else:
            st.dataframe(review, use_container_width=True, hide_index=True)
        st.download_button(
            label="⬇️ Download review list (CSV)",
            data=review.to_csv(index=False).encode("utf-8"),
            file_name="aps_ro_corp_group_match_log.csv",
            mime="text/csv",
            key="aps_corp_review_download",
            disabled=review.empty,
        )

        st.markdown("**Apply a fixed review list**")
        patch_nonce = st.session_state.get(_APS_PATCH_NONCE_KEY, 0)
        patch = st.file_uploader(
            "Upload fixed review list (CSV)", type=["csv"],
            key=f"aps_patch_upload_{patch_nonce}",
            help="The downloaded list with corrected Corporate Group values "
                 "(Customer + Corporate Group columns).",
        )
        if st.button(
            "✅ Apply patch to history", key="aps_patch_apply",
            disabled=patch is None,
        ) and patch is not None:
            try:
                overrides = parse_corp_override_csv(patch.getvalue())
                if not overrides:
                    st.warning(
                        "No usable Customer → Corporate Group rows found "
                        "(blank / (Unmapped) values are skipped)."
                    )
                    return
                with st.spinner("Patching the APS history tracker…"):
                    patched, total = patch_history_corp(overrides)
                _cached_persisted_aps_plan.clear()
                _cached_aps_history.clear()   # comparison picks up the patched history
                st.session_state[_APS_PATCH_NONCE_KEY] = patch_nonce + 1
                if patched:
                    st.success(
                        f"✅ Patched **{patched:,}** R&O row(s) across "
                        f"**{len(overrides)}** customer(s); history now "
                        f"**{total:,}** rows."
                    )
                else:
                    st.info(
                        "No reviewable rows matched — those customers may already "
                        "be resolved (exact / previously patched)."
                    )
                st.rerun()
            except (ApsUploadError, LakehouseIOError, ValueError) as exc:
                st.error(f"❌ Could not apply the patch.\n\n{exc}")


def _render_demand_summary_aps() -> None:
    """Render the Demand Summary (APS / Oracle) section — the upload-driven APS plan.

    One foldable section containing, top → bottom: ① **upload & manage** (build
    the Base Plan leg from an APS bulk export **or** the R&O leg from an R&O
    seed, and a delete tool for a Cycle / FY / Forecast Type slice), ② the
    **R&O Corporate Group review + patch** sub-section, and ③ the **APS Demand
    Plan Comparison Summary** sub-section — the last two are native (nested)
    expanders available whenever the history file exists.  B2C-only.
    """
    with st.expander("📈 Demand Summary (APS / Oracle)", expanded=False):
        st.caption(
            "**APS / Oracle demand plan.**  Upload an **APS bulk export** (builds the "
            "**Base Plan** leg) **or** an **R&O seed** (builds the **R&O** leg), "
            "pick the **Cycle** + **Fiscal Year**, and the rows are shaped to the "
            "history schema (Portfolio / Supply by **item code** via PDH → "
            "RO_Item_Master; Corporate Group via the `plan_to_code → "
            "dp_dimplantosites → dp_dimcustomernames` bridge, native code as "
            f"fallback) and upserted into **`{aps_history_path()}`** — replacing "
            "only that (Cycle, FY, Forecast Type) leg.  Use the delete tool to "
            "clear a slice.  Review / patch corporate groups and build the "
            "comparison in the sub-sections below."
        )

        # Auth gate — match every other Fabric-backed section here.
        if not fabric_signin_widget.is_fabric_signed_in():
            st.warning(
                "🔒 **Microsoft Fabric is not connected.**  Sign in via "
                "**Home & Fabric Sign-in** in the sidebar, then return here."
            )
            return

        _render_aps_upload_manage()

        # ② + ③ share ONE history read (the ≈1M-row tracker is the section's
        # heaviest source).  Reading it here — before either sub-section — means
        # neither blocks the other on its own read, so both render together and
        # are independent.  The read is cached (see _cached_aps_history), so the
        # upload/manage step above and a warm rerun don't pay for it again.
        try:
            aps_history = _cached_aps_history()
        except (LakehouseIOError, ValueError) as exc:
            aps_history = None
            st.warning(f"Could not read the APS history tracker: {exc}")

        # ② + ③ — native (nested) foldable sub-sections, always available.
        _render_aps_corp_review(aps_history)
        _render_aps_comparison_section(aps_history)


def _render_aps_upload_manage() -> None:
    """① Upload & build one leg (Base Plan or R&O) + delete a history slice."""
    pick = st.columns(2)
    with pick[0]:
        cycle = st.selectbox(
            "Cycle", options=APS_CYCLES, key="aps_upload_cycle",
            help="The planning cycle this upload represents.",
        )
    with pick[1]:
        fy = st.selectbox(
            "Fiscal Year", options=APS_FISCAL_YEARS, key="aps_upload_fy",
            help="The fiscal year this upload represents.",
        )
    kind = st.radio(
        "What are you uploading?",
        options=("APS bulk export → Base Plan leg", "R&O seed → R&O leg"),
        horizontal=True, key="aps_upload_kind",
        help="An APS export replaces only the Base Plan rows for this Cycle + FY; "
             "an R&O seed replaces only the R&O rows — the other leg is left intact.",
    )
    is_base = kind.startswith("APS bulk export")
    nonce = st.session_state.get(_APS_UPLOAD_NONCE_KEY, 0)
    uploaded = st.file_uploader(
        "Upload APS bulk export (CSV)" if is_base else "Upload R&O seed (CSV)",
        type=["csv"], key=f"aps_upload_file_{nonce}",
        help=("e.g. FY27_C5_APS_bulk_export_per_month_YYYYMMDD.csv — one row per "
              "party-site × item × month." if is_base else
              "The RO_Seed export (one row per Customer × item × format) used to "
              "expand the R&O leg."),
    )
    build_clicked = st.button(
        "▶️ Build & append to history", key="aps_upload_build", type="primary",
        use_container_width=True, disabled=uploaded is None,
        help="Transforms the upload and upserts the matching leg into the APS "
             "history tracker (replacing only this Cycle + FY + Forecast Type).",
    )

    res_new: Optional[object] = None
    if build_clicked and uploaded is not None:
        try:
            with st.spinner(
                "Transforming the upload, resolving corporate groups, and "
                "upserting into the APS history tracker…"
            ):
                if is_base:
                    res_new = generate_base_plan_from_upload(
                        uploaded.getvalue(), filename=uploaded.name,
                        cycle=str(cycle), fy=int(fy))
                else:
                    res_new = generate_ro_from_seed(
                        uploaded.getvalue(), filename=uploaded.name,
                        cycle=str(cycle), fy=int(fy))
            st.session_state[_APS_UPLOAD_NONCE_KEY] = nonce + 1
        except (
            ApsUploadError, HolisticDemandPlanError, PlanLiftError,
            CustomerDimsError, ShipToSitesSourceError, LakehouseIOError,
            ValueError,
        ) as exc:
            st.session_state.pop(_APS_UPLOAD_RESULT_KEY, None)
            st.error(f"❌ Could not build the APS plan.\n\n{exc}")
        if res_new is not None:
            _cached_persisted_aps_plan.clear()
            _cached_aps_history.clear()   # so the comparison + review see the change
            st.session_state[_APS_UPLOAD_RESULT_KEY] = res_new

    # Build outcome (or persisted / empty state), then the delete tool.
    res = st.session_state.get(_APS_UPLOAD_RESULT_KEY)
    if res is not None:
        cov = "—" if pd.isna(res.corp_coverage) else f"{res.corp_coverage:.0%}"
        leg = (f"{res.aps_rows:,} Base Plan" if res.aps_rows
               else f"{res.ro_rows:,} R&O")
        st.success(
            f"✅ Built **{len(res.rows):,}** rows ({leg}) for **{res.cycle} / "
            f"FY{res.fy}** — corporate-group coverage **{cov}**.  History tracker "
            f"now **{res.history_rows:,}** rows."
        )
        _render_aps_download_preview(res.rows, key_prefix="aps_upload_live")
    else:
        try:
            persisted = _cached_persisted_aps_plan()
        except (LakehouseIOError, ValueError) as exc:
            persisted = None
            st.warning(f"Could not read the saved APS file: {exc}")
        if persisted is not None and not persisted.empty:
            st.success(
                f"✅ Loaded the last-built **{APS_OUTPUT_NAME}** from Fabric "
                f"({len(persisted):,} rows)."
            )
            _render_aps_download_preview(persisted, key_prefix="aps_upload_saved")
        else:
            st.caption(
                "_Pick a **Cycle** + **Fiscal Year**, choose what you're uploading, "
                "then click **Build & append to history**._"
            )

    _render_aps_delete_tool()


def _render_aps_delete_tool() -> None:
    """Delete a (Cycle, FY, Forecast Type) slice from the APS history tracker."""
    with st.container(border=True):
        st.markdown("**🗑️ Delete rows from the APS history tracker**")
        st.caption(
            "Remove a slice by **Cycle + Fiscal Year + Forecast Type** — e.g. "
            "clear a bad R&O leg before re-uploading its seed.  Permanent."
        )
        dc = st.columns(3)
        with dc[0]:
            del_cycle = st.selectbox("Cycle", APS_CYCLES, key="aps_del_cycle")
        with dc[1]:
            del_fy = st.selectbox("Fiscal Year", APS_FISCAL_YEARS, key="aps_del_fy")
        with dc[2]:
            del_type = st.selectbox(
                "Forecast Type", ("All", APS_FCST_BASE_PLAN, APS_FCST_R_AND_O),
                key="aps_del_type")
        confirm = st.checkbox(
            f"Yes, permanently delete **{del_type}** rows for "
            f"**{del_cycle} / FY{del_fy}**", key="aps_del_confirm")
        if st.button(
            "🗑️ Delete matching rows", key="aps_del_btn", disabled=not confirm,
        ) and confirm:
            types = None if del_type == "All" else (del_type,)
            try:
                with st.spinner("Deleting from the APS history tracker…"):
                    deleted, total = delete_history_slice(
                        str(del_cycle), int(del_fy), types)
                # Clear caches so the review + comparison sub-sections re-read the
                # post-delete history later in THIS run (no rerun needed, so the
                # confirmation banner stays visible).
                _cached_persisted_aps_plan.clear()
                _cached_aps_history.clear()
                if deleted:
                    st.success(
                        f"🗑️ Deleted **{deleted:,}** row(s); history tracker now "
                        f"**{total:,}** rows."
                    )
                else:
                    st.info(
                        "No rows matched that Cycle / Fiscal Year / Forecast Type."
                    )
            except (LakehouseIOError, ValueError) as exc:
                st.error(f"❌ Could not delete the slice.\n\n{exc}")


def _render_aps_download_preview(frame: pd.DataFrame, *, key_prefix: str) -> None:
    """Download button (fixed filename) + a first-100-rows preview expander."""
    st.download_button(
        label=f"⬇️ Download `{APS_OUTPUT_NAME}`",
        data=frame.to_csv(index=False).encode("utf-8"),
        file_name=APS_OUTPUT_NAME,
        mime="text/csv",
        key=f"{key_prefix}_download",
        type="primary",
        use_container_width=True,
        help="Exactly the file saved in Fabric (reflects any applied "
             "Corporate Group patch).",
    )
    with st.expander("👁️ Preview (first 100 rows)", expanded=False):
        st.dataframe(frame.head(100), use_container_width=True, hide_index=True)


def _render_demand_summary_file(
    *,
    title: str,
    icon: str,
    fetch_fn: Callable[..., DemandSummarySnapshot],
    blob_path_fn: Callable[[], str],
    download_basename: str,
    download_button_key: str,
) -> None:
    """Fetch + render one Demand Summary file with per-file error containment.

    Parameters
    ----------
    title, icon, download_basename, download_button_key
        Forwarded verbatim to :func:`_render_demand_summary_table`.
    fetch_fn
        One of :func:`fetch_mgmt_plan_full` or
        :func:`fetch_total_item_level_demand` — the connector function
        that returns a :class:`DemandSummarySnapshot`.
    blob_path_fn
        Returns the POSIX blob path; used in the error message so the
        planner sees exactly which file failed when the load errors.

    Per-file error containment keeps the two file sub-sections
    independent: a transient read failure on one file (e.g. upstream
    pipeline mid-write, permissions blip) does NOT prevent the other
    file from rendering on the same page load.
    """
    try:
        with st.spinner(f"Reading {title} from Microsoft Fabric…"):
            snapshot = fetch_fn()
    except DemandSummaryError as exc:
        st.error(
            f"❌ Could not load **{title}** "
            f"(`{blob_path_fn()}`).\n\n{exc}"
        )
        return

    _render_demand_summary_table(
        title=title,
        icon=icon,
        snapshot=snapshot,
        download_basename=download_basename,
        download_button_key=download_button_key,
    )


# ── Demand Pivot Summary (hierarchical roll-up) ─────────────────────────────
#
# Mirrors the Excel pivot the planner pasted in the chat:
#   Rows:  Portfolio Major → Forecast Type ("Base Plan" / "R&O") → Supply Format
#   Cols:  one per month + a trailing "Total" column
#   Vals:  Sum of Demand Plan Pounds, in millions, rounded to 1 decimal
#
# Source: ``Files/RO Tracking/Demand Plan/qry_total_item_level_demand.csv``
# (loaded via the same cached fetcher the raw preview above uses, so the
# pivot benefits from the same freshness guarantees — a refresh of the
# source CSV in Fabric automatically propagates to this section on the
# next page render).
#
# Filter widget keys — separated from the rest of the page's keys with a
# distinct ``ro_dp_`` prefix so a key collision can't accidentally
# reset a planner's selection in another section.

_SS_DP_PMAJ_FILTER:   str = "ro_dp_pmaj_filter"
_SS_DP_SFMT_FILTER:   str = "ro_dp_sfmt_filter"
_SS_DP_CYCLE_FILTER:  str = "ro_dp_cycle_filter"
# Forecast window (tracker) and actual window (IBP Shipments) pickers.
# Distinct key strings from the retired single date-range so a stale
# session value from an older build can't collide with the new widgets.
_SS_DP_FC_START_FILTER:  str = "ro_dp_forecast_start_filter"
_SS_DP_FC_END_FILTER:    str = "ro_dp_forecast_end_filter"
_SS_DP_ACT_START_FILTER: str = "ro_dp_actual_start_filter"
_SS_DP_ACT_END_FILTER:   str = "ro_dp_actual_end_filter"

# Hidden columns owned by the pivot builder — kept in sync with
# ``data_sources/demand_summary._HIDDEN_COLS``.  Listed locally so the
# page doesn't need to reach into the private module surface to know
# which columns to suppress from the editor.
_DP_HIDDEN_COLS: tuple[str, ...] = ("_row_id", "_indent", "_is_subtotal")


def _build_demand_pivot_budget_lookup() -> BudgetLookup:
    """Annual leaf budget for the hierarchical pivot table only."""
    try:
        base_df = fetch_static_budget_base().df
    except DemandSummaryError as exc:
        logger.info(
            "Static_Budget_Base_Lbs.csv unavailable (pivot Total Budget): %s",
            exc,
        )
        base_df = None
    try:
        ro_df = fetch_static_budget_ro().df
    except DemandSummaryError as exc:
        logger.info(
            "Static_Budget_RO_Lbs.csv unavailable (pivot Total Budget): %s",
            exc,
        )
        ro_df = None
    return build_budget_lookup(base_df, ro_df)


def _load_demand_pivot_monthly_budget() -> MonthlyBudgetLookup:
    """Load bundled monthly budget for footer Total Budget + chart.

    Reads ``Static_Budget_Base&RO_by_Month.csv`` from Fabric and parses
    it via :func:`build_monthly_budget_lookup`.  A missing file yields
    an empty lookup; the page suppresses the footer Total Budget row
    and the chart overlay without blocking the rest of the pivot.
    """
    try:
        snapshot = fetch_static_budget_monthly()
        budget_df = snapshot.df
    except DemandSummaryError as exc:
        logger.info(
            "Static_Budget_Base&RO_by_Month.csv unavailable for Demand "
            "Pivot budget (footer + chart suppressed): %s",
            exc,
        )
        budget_df = None

    return build_monthly_budget_lookup(budget_df)


def _render_demand_pivot_section() -> None:
    """Render the Demand MOM Summary header + delegate to the fragment.

    The header (title + caption) stays OUTSIDE the fragment because
    it's static text — the fragment owns only the interactive widgets
    + table + chart so filter / date-range changes rerun only the
    MOM view, not the surrounding headers.
    """
    st.markdown("### 📊 Demand MOM Summary")
    st.caption(
        "Month-over-month roll-up (Portfolio Major → Forecast Type → "
        "Supply Format), monthly columns in **millions of pounds**.  The "
        "**Actual month range** pulls actuals from **`dbo.IBP Shipments`**; "
        "the remaining **Forecast month range** pulls the plan for the "
        "selected **Cycle** from **`qry_mgmt_plan_history_tracker.csv`** — "
        "stitched onto one month axis (actuals for the closed months, "
        "forecast beyond).  Auto-refreshes when the sources change in "
        "Fabric.  The footer subtotals and the chart below update live; "
        "**double-click a pivot row to drill into its SKUs**."
    )
    _render_demand_pivot_fragment()


@st.fragment
def _render_demand_pivot_fragment() -> None:
    """Render the filters, not-captured log, MOM pivot, footer, and chart.

    Why this is a ``@st.fragment``
    -------------------------------
    The widget set (Cycle + PMaj / SFmt multiselects + the two month-
    range pickers + the pivot's row-select) is interactive, but the
    surrounding page sections have nothing to do with the MOM Summary.
    Wrapping this block in a fragment scopes each interaction to a rerun
    of just this function — no upstream Fabric reads for the rest of the
    page, no RO Comparison rebuild.

    Sourcing model
    --------------
    * **Forecast** — ``qry_mgmt_plan_history_tracker.csv`` via
      :func:`fetch_mgmt_plan_history_tracker` (cached, blob-keyed).
    * **Actuals**  — ``dbo.IBP Shipments`` via
      :func:`fetch_ibp_shipments_slim_df`, projected to just the actual
      window so OneLake returns the minimum rows.
    * **Dims**     — ``qry_pdh.csv`` supplies Portfolio Major / Supply
      Format (the tracker + shipments carry neither).
    Any refresh of a source in Fabric invalidates its cache and the MOM
    view reflects the fresh data on the next render — no manual refresh.
    """
    # 1. Forecast source (tracker) + dims (PDH) + budgets.
    try:
        with st.spinner("Reading qry_mgmt_plan_history_tracker.csv from Microsoft Fabric…"):
            tracker = fetch_mgmt_plan_history_tracker()
    except DemandSummaryError as exc:
        st.error(
            "❌ Could not load **qry_mgmt_plan_history_tracker.csv** for "
            f"the Demand MOM Summary.\n\n{exc}"
        )
        return
    if tracker.df.empty:
        st.info(
            "ℹ️ `qry_mgmt_plan_history_tracker.csv` is empty — nothing to "
            "roll up.  Check the upstream Fabric pipeline."
        )
        return

    pdh_df = _load_demand_comparison_pdh()          # primary dims (non-fatal)
    item_master_df = _load_mom_item_master()        # fallback dims (non-fatal)
    budget_lookup = _build_demand_pivot_budget_lookup()
    monthly_budget = _load_demand_pivot_monthly_budget()

    # 2. Discover filter option lists.
    cycles = list_tracker_cycles(tracker.df)
    if not cycles:
        st.warning(
            "⚠️ The tracker carries no `Cycle` values — cannot pick a "
            "forecast cycle.  Check the upstream export."
        )
        return
    tracker_months = list_tracker_months(tracker.df)
    try:
        actual_months = list(fetch_ibp_shipments_months())
    except IBPOfficialSourceError as exc:
        actual_months = []
        st.warning(
            "⚠️ Could not read the IBP Shipments month list "
            f"({exc}) — falling back to the tracker's months for the "
            "actual-range picker."
        )
    if not actual_months:
        actual_months = tracker_months

    field_options = list_mom_filter_values(tracker.df, pdh_df, item_master_df)

    # 3. Render the filters (Cycle + Actual/Forecast ranges + PMaj/SFmt).
    filters = _render_demand_mom_filters(
        cycles, tracker_months, actual_months, field_options,
    )

    # 4. Validate the windows (disjoint + start ≤ end) before any read.
    errors = validate_mom_filters(filters)
    if errors:
        for err in errors:
            st.warning(f"⚠️ {err}")
        return

    # 5. Load actuals for the actual window only (predicate-pushed slim read).
    actual_window = tuple(sorted(
        months_in_range(filters.actual_start, filters.actual_end)
    ))
    ibp_df: Optional[pd.DataFrame] = None
    try:
        with st.spinner("Reading dbo.IBP Shipments actuals from Microsoft Fabric…"):
            if actual_window:
                ibp_df = fetch_ibp_shipments_slim_df(months=actual_window)
    except IBPOfficialSourceError as exc:
        st.warning(
            "⚠️ Could not read IBP Shipments actuals "
            f"({exc}) — the MOM view will show forecast months only."
        )

    # 6. Build the stitched MOM pivot.
    try:
        with st.spinner("Building Demand MOM Summary…"):
            result = build_demand_mom_pivot(
                tracker.df, ibp_df, pdh_df, filters,
                item_master_df=item_master_df,
                budget_lookup=budget_lookup,
                monthly_budget=monthly_budget,
            )
    except DemandPlanComparisonError as exc:
        st.error(f"❌ Could not build the Demand MOM Summary.\n\n{exc}")
        return

    # 7. Reconciliation log — surfaced at the TOP of the results so a
    #    planner sees "what's missing" before reading the numbers.
    _render_mom_not_captured_log(result.not_captured_items)

    # 8. Empty-after-filter case — hint instead of a degenerate table/chart.
    if result.pivot.empty:
        st.info(
            "No rows match the current Cycle / Portfolio Major / Supply "
            "Format / month-range selection.  Widen one of the filters "
            "above to see data."
        )
        return

    # 9. Pivot table + footer totals + download + SKU drill-down.
    _render_demand_pivot_table(result)

    # 10. Stitched month-over-month chart (Actual → Base + R&O).
    st.markdown("---")
    _render_base_ro_summary_chart(result)


def _render_demand_mom_filters(
    cycles: list[str],
    tracker_months: list[date],
    actual_months: list[date],
    field_options: dict[str, list[str]],
) -> DemandMomFilters:
    """Render the MOM filter widgets and return a :class:`DemandMomFilters`.

    Layout
    ------
    Row 1 — Portfolio Major / Supply Format multiselects + the Cycle
    picker.  Row 2 — the **Actual month range** (IBP Shipments).  Row 3 —
    the **Forecast month range** (tracker, selected Cycle).  Month pickers
    are ``selectbox``es over the discrete month lists (mirrors the sibling
    Demand Plan Comparison section rather than free-form date inputs).

    Defaults
    --------
    * PMaj / SFmt start EMPTY → "include every value".
    * Cycle defaults to the newest (``cycles`` is in natural order).
    * The month windows default to the planner's usual split — recent
      closed months as actuals, the following months as forecast — and
      fall back to a computed disjoint split when those exact months are
      absent, so the two ranges never overlap on first render.
    """
    pmaj_opts: list[str] = field_options.get("portfolio_majors", [])
    sfmt_opts: list[str] = field_options.get("supply_formats", [])

    # Spell the month out ("Apr 2026") — the bare "4/2026" form is hard to
    # scan.  Identity formatter for the cycle labels.
    fmt_month = lambda d: d.strftime("%b %Y")  # noqa: E731
    fmt_cycle = lambda c: c                     # noqa: E731

    # ── Default indices ────────────────────────────────────────────────
    n_fc = len(tracker_months)
    last_fc_idx = max(0, n_fc - 1)

    def _fc_idx(target: date, fallback: int) -> int:
        return tracker_months.index(target) if target in tracker_months else fallback

    def _act_idx(target: date, fallback: int) -> int:
        return actual_months.index(target) if target in actual_months else fallback

    last_actual_idx = max(0, len(actual_months) - 1)
    # Preferred windows (match the sibling comparison section); fall back
    # to a self-adjusting disjoint split when those months are absent.
    fc_fallback_start = min(max(0, n_fc // 2), last_fc_idx)
    act_start_idx = _act_idx(date(2026, 4, 1), 0)
    act_end_idx = _act_idx(date(2026, 5, 1), last_actual_idx)
    fc_start_idx = _fc_idx(date(2026, 6, 1), fc_fallback_start)
    fc_end_idx = _fc_idx(date(2027, 3, 1), last_fc_idx)

    with st.expander("🔍 Filters", expanded=True):
        row1 = st.columns([2, 2, 1.4])
        with row1[0]:
            selected_pmaj = st.multiselect(
                "Portfolio Major", options=pmaj_opts, key=_SS_DP_PMAJ_FILTER,
                help=(
                    "Limit the pivot to specific Portfolio Major value(s).  "
                    "Empty = include every Portfolio Major."
                ),
            )
        with row1[1]:
            selected_sfmt = st.multiselect(
                "Supply Format", options=sfmt_opts, key=_SS_DP_SFMT_FILTER,
                help=(
                    "Limit the pivot to specific Supply Format value(s).  "
                    "Empty = include every Supply Format."
                ),
            )
        with row1[2]:
            cycle = st.selectbox(
                "Cycle (forecast)", options=cycles,
                index=len(cycles) - 1, key=_SS_DP_CYCLE_FILTER,
                format_func=fmt_cycle,
                help=(
                    "Planning cycle whose forecast the MOM Summary pulls "
                    "from `qry_mgmt_plan_history_tracker.csv`."
                ),
            )

        st.markdown("**Actual month range** (pulled from `dbo.IBP Shipments`)")
        row2 = st.columns(2)
        with row2[0]:
            actual_start = st.selectbox(
                "Actual month — start", options=actual_months,
                index=act_start_idx, key=_SS_DP_ACT_START_FILTER,
                format_func=fmt_month,
            )
        with row2[1]:
            actual_end = st.selectbox(
                "Actual month — end", options=actual_months,
                index=act_end_idx, key=_SS_DP_ACT_END_FILTER,
                format_func=fmt_month,
            )

        st.markdown(
            "**Forecast month range** (pulled from the tracker; must not "
            "overlap the actual range)"
        )
        row3 = st.columns(2)
        with row3[0]:
            forecast_start = st.selectbox(
                "Forecast month — start", options=tracker_months,
                index=fc_start_idx, key=_SS_DP_FC_START_FILTER,
                format_func=fmt_month,
            )
        with row3[1]:
            forecast_end = st.selectbox(
                "Forecast month — end", options=tracker_months,
                index=fc_end_idx, key=_SS_DP_FC_END_FILTER,
                format_func=fmt_month,
            )

        st.caption(
            f"📌 **Actual** {fmt_month(actual_start)} – {fmt_month(actual_end)} "
            f"(IBP Shipments)  ·  **Forecast** {fmt_month(forecast_start)} – "
            f"{fmt_month(forecast_end)} (tracker, cycle **{cycle}**)"
        )

    return DemandMomFilters(
        cycle=cycle,
        actual_start=actual_start,
        actual_end=actual_end,
        forecast_start=forecast_start,
        forecast_end=forecast_end,
        portfolio_majors=tuple(selected_pmaj) or None,
        supply_formats=tuple(selected_sfmt) or None,
    )


# Deep link to the RO_Item_Master.csv file in the Fabric lakehouse — the
# authoritative place to look up / add a Portfolio Major + Supply Format for
# items the MOM dim cascade (PDH → RO_Item_Master) still can't classify.
_RO_ITEM_MASTER_FABRIC_URL: str = (
    "https://app.fabric.microsoft.com/groups/"
    "bb11c51d-03c8-4f1b-938c-e20657a8f31d/lakehouses/"
    "a01f513d-eee7-41eb-8c15-670bc40e7fc8"
    "?experience=fabric-developer"
    "&selectedPath=Files%2FRO%20Tracking%2FRO_Item_Master.csv"
)


def _render_mom_not_captured_log(not_captured: pd.DataFrame) -> None:
    """Render the "in tracker but not captured in the MOM Summary" log.

    Sits at the top of the results.  When every tracker item (selected
    cycle, forecast window) made it into the pivot, a compact success
    note confirms the reconciliation ran; otherwise an expanded warning
    lists the missing items + reason, a CSV download, a one-click copy of
    the item numbers, and a jump link to ``RO_Item_Master.csv`` in Fabric
    so the planner can classify the stragglers at source.
    """
    if not_captured is None or not_captured.empty:
        st.caption(
            "✅ Every tracker item in the forecast window is captured in "
            "the MOM Summary below."
        )
        return

    n = len(not_captured)
    with st.expander(
        f"⚠️ {n:,} tracker item(s) NOT captured in the MOM Summary",
        expanded=True,
    ):
        st.caption(
            "Items present in `qry_mgmt_plan_history_tracker.csv` for the "
            "selected cycle + forecast window that do **not** appear in the "
            "pivot below — with the reason (no Portfolio Major / Supply "
            "Format mapping in **PDH or RO_Item_Master**, excluded by an "
            "active filter, or zero forecast pounds).  Reconcile these "
            "before trusting the totals."
        )
        st.dataframe(not_captured, use_container_width=True, hide_index=True)

        # Reconciliation aids: jump to the fallback source in Fabric, copy
        # the item list to paste into it, and download the full log.
        col_link, col_dl = st.columns([1, 1])
        with col_link:
            st.link_button(
                "🔎 Open RO_Item_Master.csv in Fabric",
                _RO_ITEM_MASTER_FABRIC_URL,
                use_container_width=True,
                help=(
                    "Add a Portfolio Major + Supply Format for these items "
                    "here; the MOM Summary picks them up on the next refresh."
                ),
            )
        with col_dl:
            today = pd.Timestamp.utcnow().strftime("%Y%m%d")
            st.download_button(
                label="⬇️ Download not-captured items (CSV)",
                data=not_captured.to_csv(index=False).encode("utf-8"),
                file_name=f"demand_mom_not_captured_{today}.csv",
                mime="text/csv",
                key="demand_mom_not_captured_download",
                use_container_width=True,
            )

        # ``st.code`` renders a built-in copy button — the fastest way to
        # lift the item numbers and search for them in RO_Item_Master.
        items = (
            not_captured[NC_COL_ITEM].astype(str).str.strip().tolist()
            if NC_COL_ITEM in not_captured.columns else []
        )
        if items:
            st.caption("Item numbers to look up (copy →):")
            st.code(", ".join(items), language="text")


def _render_mom_sku_drilldown(result: DemandMomResult, row: pd.Series) -> None:
    """Render the SKU-level detail behind a selected pivot row + download.

    Resolves the clicked row to its (Portfolio Major, Forecast Type,
    Supply Format) breadcrumb via the hidden dimension columns and asks
    the result for the matching item-level slice.  A Grand Total / header
    row (blank breadcrumb) drills into everything beneath it.
    """
    pmaj = str(row.get(MOM_ROW_PMAJ, "") or "")
    forecast = str(row.get(MOM_ROW_FORECAST, "") or "")
    sfmt = str(row.get(MOM_ROW_SFMT, "") or "")
    label = str(row.get("Row Label", "")).strip() or "selection"

    sku = result.sku_detail_for(pmaj, forecast, sfmt)
    st.markdown(f"**🔬 SKU detail — {label}**")
    if sku.empty:
        st.info("No SKU-level rows for this selection.")
        return
    st.dataframe(sku, use_container_width=True, hide_index=True)
    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    st.download_button(
        label="⬇️ Download SKU detail (CSV)",
        data=sku.to_csv(index=False).encode("utf-8"),
        file_name=f"demand_mom_sku_detail_{today}.csv",
        mime="text/csv",
        key="demand_mom_sku_detail_download",
    )
    st.caption(
        "Values in millions of pounds.  Select a different row to drill "
        "into it."
    )


def _demand_pivot_column_config(
    month_columns: tuple[str, ...], *, include_budget: bool,
) -> dict:
    """Return the ``column_config`` mapping for the pivot editor.

    All month columns + the Total column use ``format="%.1f"`` to
    match the screenshot's "47.8 M lb" display precision (one
    decimal place).  ``Row Label`` is wide enough to fit the deepest
    indent without truncation.  The Total Budget column is included
    only when *include_budget* is True (the page passes ``False`` if
    both static-budget CSVs were unavailable, so the column doesn't
    render as a misleading all-zero strip).
    """
    cc = st.column_config
    num_fmt = "%.1f"

    config: dict = {
        "Row Label": cc.TextColumn("Row Labels", width="large", disabled=True),
    }
    for c in month_columns:
        config[c] = cc.NumberColumn(c, format=num_fmt, disabled=True)
    config[TOTAL_COLUMN_LABEL] = cc.NumberColumn(
        TOTAL_COLUMN_LABEL, format=num_fmt, disabled=True,
    )
    if include_budget:
        config[TOTAL_BUDGET_COLUMN_LABEL] = cc.NumberColumn(
            TOTAL_BUDGET_COLUMN_LABEL,
            format=num_fmt,
            disabled=True,
            help=(
                "Annual budget (millions of lbs) for this pivot row, "
                "from `Static_Budget_Base_Lbs.csv` / "
                "`Static_Budget_RO_Lbs.csv`.  The footer Total Budget "
                "row uses monthly data from "
                "`Static_Budget_Base&RO_by_Month.csv`."
            ),
        )
    return config


def _fmt_mom_num(value: object) -> str:
    """Format a millions value to 1 dp; blank/NaN → '-'."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    if pd.isna(num):
        return "-"
    return f"{num:,.1f}"


def _render_mom_pivot_html(result: DemandMomResult, *, show_budget: bool) -> None:
    """Render the MOM pivot as a foldable, dark-styled, read-only HTML table.

    Matches the planner's target layout: a dark-gray/white header, dark-gray
    layer-1 (Portfolio Major) rows, and dark-gray Total + Total Budget columns
    on every row.  Layer-1 and layer-2 (Forecast Type) rows are
    ``<details>/<summary>`` disclosures so the planner can fold a whole
    Portfolio Major — or one Forecast-Type branch — down to a single row.
    Columns are a fixed CSS grid so every row (summary or leaf) stays aligned.
    """
    pivot = result.pivot
    months = list(result.month_columns)
    data_cols = [*months, TOTAL_COLUMN_LABEL]
    if show_budget:
        data_cols.append(TOTAL_BUDGET_COLUMN_LABEL)
    # Fixed column widths → identical across every <details> block, so the
    # grid lines up even though rows live in separate disclosure elements.
    # Months are 66px so a full "2026-04" label fits without truncation;
    # Total Budget is 96px so its header reads in full.
    _month_w, _tot_w, _bud_w = 66, 62, 96
    grid = ("230px " + " ".join([f"{_month_w}px"] * len(months))
            + f" {_tot_w}px" + (f" {_bud_w}px" if show_budget else ""))
    min_w = 230 + _month_w * len(months) + _tot_w + (_bud_w if show_budget else 0)

    tot_cols = {TOTAL_COLUMN_LABEL, TOTAL_BUDGET_COLUMN_LABEL}

    def _cells(row: pd.Series, *, indent: int, foldable: bool) -> str:
        label = str(row.get("Row Label", "")).replace(" ", "").strip()
        tri = '<span class="tri"></span>' if foldable else ""
        pad = 8 + indent * 16
        out = [f'<div class="lbl" style="padding-left:{pad}px">{tri}{_esc_html(label)}</div>']
        for c in data_cols:
            klass = "cell tot" if c in tot_cols else "cell"
            out.append(f'<div class="{klass}">{_fmt_mom_num(row.get(c))}</div>')
        return "".join(out)

    def _norm(label: object) -> str:
        return str(label).replace(" ", " ").strip()

    # Header row.
    head = [f'<div class="lbl">Row Labels</div>']
    for c in data_cols:
        head.append(f'<div>{_esc_html(c)}</div>')
    parts = [f'<div class="r hdr">{"".join(head)}</div>']

    rows = [r for _, r in pivot.iterrows()]
    n = len(rows)
    i = 0
    while i < n:
        row = rows[i]
        indent = int(row.get("_indent", 0) or 0)
        is_sub = bool(row.get("_is_subtotal", False))
        if _norm(row.get("Row Label")) == "Grand Total":
            parts.append(f'<div class="r grand">{_cells(row, indent=0, foldable=False)}</div>')
            i += 1
            continue
        if indent == 0 and is_sub:
            # Layer-1 Portfolio Major → foldable, dark row.
            parts.append(f'<details class="g1" open><summary class="r l1">'
                         f'{_cells(row, indent=0, foldable=True)}</summary>')
            i += 1
            while i < n and int(rows[i].get("_indent", 0) or 0) > 0 \
                    and _norm(rows[i].get("Row Label")) != "Grand Total":
                r2 = rows[i]
                if int(r2.get("_indent", 0) or 0) == 1 and bool(r2.get("_is_subtotal", False)):
                    # Layer-2 Forecast Type → foldable.
                    parts.append(f'<details class="g2" open><summary class="r l2">'
                                 f'{_cells(r2, indent=1, foldable=True)}</summary>')
                    i += 1
                    while i < n and int(rows[i].get("_indent", 0) or 0) >= 2:
                        parts.append(f'<div class="r">{_cells(rows[i], indent=2, foldable=False)}</div>')
                        i += 1
                    parts.append("</details>")
                else:
                    # A layer-1 with no forecast-type subtotal (rare) → plain row.
                    parts.append(f'<div class="r">{_cells(r2, indent=1, foldable=False)}</div>')
                    i += 1
            parts.append("</details>")
        else:
            parts.append(f'<div class="r">{_cells(row, indent=indent, foldable=False)}</div>')
            i += 1

    css = f"""
<style>
.mom {{overflow-x:auto; margin:.25rem 0 .5rem; font-size:.8rem;
  color:#1a1a1a;}}
.mom .tbl {{min-width:{min_w}px;}}
.mom .r {{display:grid; grid-template-columns:{grid}; align-items:center;}}
.mom .r > div {{padding:3px 6px; white-space:nowrap; text-align:right;
  border-bottom:1px solid #ededed; overflow:hidden; text-overflow:ellipsis;
  background:#ffffff;}}
.mom .r > .lbl {{text-align:left;}}
.mom .r > .tot {{background:#404040; color:#ffffff;}}
.mom .hdr > div {{background:#404040; color:#ffffff; font-weight:700;
  border-bottom:1px solid #2b2b2b;
  white-space:normal; overflow:visible; text-overflow:clip; line-height:1.15;}}
.mom .l1 > div {{background:#404040; color:#ffffff; font-weight:700;}}
.mom .l2 > div {{font-weight:600;}}
.mom .grand > div {{background:#2b2b2b; color:#ffffff; font-weight:700;}}
.mom details {{border:0; margin:0;}}
.mom summary {{list-style:none;}}
.mom summary.r {{cursor:pointer;}}
.mom summary::-webkit-details-marker {{display:none;}}
.mom .tri::before {{content:'\\25B8\\00a0';}}
.mom details[open] > summary .tri::before {{content:'\\25BE\\00a0';}}
</style>
"""
    html = css + f'<div class="mom"><div class="tbl">{"".join(parts)}</div></div>'
    st.markdown(html, unsafe_allow_html=True)


def _render_demand_pivot_table(result: DemandMomResult) -> None:
    """Render the MOM pivot + dynamic footer subtotals + download + drill-down.

    The pivot is rendered with :func:`st.dataframe` in single-row
    ``on_select`` mode: it's read-only (a pure roll-up, not an editor),
    but selecting a row reveals the SKU-level detail behind it.
    ``column_order`` pins the Row Label first, then every month in
    ascending order, then the Total column, then Total Budget when annual
    budget data is available.
    """
    # ── Download button (above the table — "easy to find") ──────────
    #
    # Hand the planner a clean CSV (internal _row_id / _indent /
    # _is_subtotal columns stripped) so the file they hand off
    # downstream looks identical to what's on screen.
    download_df = pivot_for_download(result.pivot)
    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    st.download_button(
        label="⬇️ Download Demand Pivot Summary (CSV)",
        data=download_df.to_csv(index=False).encode("utf-8"),
        file_name=f"demand_pivot_summary_{today}.csv",
        mime="text/csv",
        key="demand_pivot_summary_download",
        type="primary",
        help=(
            "Downloads the current pivot view as a CSV — preserves "
            "the indented Row Label hierarchy so the file mirrors the "
            "on-screen layout, including the Total Budget column when "
            "annual budget data is available.  Honours your active filters."
        ),
    )

    column_order: list[str] = [
        "Row Label", *result.month_columns, TOTAL_COLUMN_LABEL,
    ]
    show_budget_col = result.has_pivot_budget_data or result.has_budget_data
    if show_budget_col:
        column_order.append(TOTAL_BUDGET_COLUMN_LABEL)

    pivot_column_config = _demand_pivot_column_config(
        result.month_columns, include_budget=result.has_pivot_budget_data,
    )
    # Footer uses the same columns; monthly Total Budget row fills month cells.
    footer_column_config = pivot_column_config

    # Pivot table — a read-only, FOLDABLE, dark-styled HTML table (Streamlit's
    # st.dataframe can render neither a dark header/row band nor collapsible
    # hierarchy).  Layer-1 (Portfolio Major) and layer-2 (Forecast Type) rows
    # are <details>/<summary> disclosures, so a click folds e.g. Butter — or
    # Butter → Actual — down to a single row.
    _render_mom_pivot_html(result, show_budget=show_budget_col)

    # ── Dynamic subtotals (Base Plan / R&O / bundled Total Budget) ───
    #
    # Per the planner's spec, these are SEPARATE from the Grand Total
    # row inside the pivot above — they make the Base/R&O split
    # explicit at a glance, regardless of which Portfolio Major
    # rows ended up visible (e.g. R&O total is non-zero even when the
    # planner filtered to PMaj that have only Base-Plan leaves).
    #
    # The bundled Total Budget row is appended at the bottom so a
    # single glance answers "what's the budget across both Base and
    # R&O for the current filter window?" — same number that drives
    # the dotted line on the chart below.
    # Footer order mirrors the row hierarchy: Actual first, then the two
    # forecast branches, then the static bundled budget.
    st.markdown("**Dynamic subtotals** (live: reflects current filters)")
    footer_parts = [
        result.actual_totals, result.base_plan_totals, result.r_and_o_totals,
    ]
    if result.has_budget_data:
        footer_parts.append(result.budget_totals)
    footer_df = pd.concat(
        [p for p in footer_parts if p is not None and not p.empty],
        ignore_index=True,
    )
    st.dataframe(
        footer_df,
        use_container_width=True,
        hide_index=True,
        height=35 * (len(footer_df) + 1) + 38,
        column_order=column_order,
        column_config=footer_column_config,
    )
    if result.has_budget_data:
        st.caption(
            f"💰 **Bundled Total Budget (Base + R&O):** "
            f"**{result.budget_total_m:,.1f} M lbs** "
            "for the visible month window (from "
            "`Static_Budget_Base&RO_by_Month.csv`; green line on the "
            "chart below)."
        )

    # ── SKU drill-down (picker) ─────────────────────────────────────────
    # The foldable HTML table can't post a row click back to Python, so the
    # drill-down is driven by a compact picker of the pivot's rows.
    pivot = result.pivot
    if not pivot.empty:
        def _breadcrumb(idx: int) -> str:
            r = pivot.iloc[idx]
            parts = [str(r.get(c, "") or "").strip()
                     for c in (MOM_ROW_PMAJ, MOM_ROW_FORECAST, MOM_ROW_SFMT)]
            parts = [p for p in parts if p]
            return " › ".join(parts) or str(r.get("Row Label", "")).strip() or "Grand Total"

        options = [-1, *range(len(pivot))]
        sel = st.selectbox(
            "🔬 Drill into a row (SKU-level detail)",
            options=options,
            index=0,
            format_func=lambda i: "— none —" if i < 0 else _breadcrumb(i),
            key="ro_dp_pivot_drill_select",
            help="Pick a Portfolio Major / Forecast Type / Supply Format row to "
                 "see the SKUs behind it.",
        )
        if sel is not None and sel >= 0:
            _render_mom_sku_drilldown(result, pivot.iloc[sel])
        else:
            st.caption("💡 Pick a row above to drill into its SKU-level values.")


def _render_base_ro_summary_chart(result: DemandMomResult) -> None:
    """Render the stitched month-over-month chart (Actual → Base + R&O).

    Actual months (IBP Shipments) render as the ``Actual`` area; the
    forecast months render as the Base Plan (dark blue) + R&O (orange)
    stack — and because the two windows are disjoint on the month axis,
    each month is populated by exactly one branch, giving one continuous
    month-over-month view.  When monthly budget data is available (see
    :attr:`DemandMomResult.has_budget_data`), a green dotted line plots
    the bundled budget per month from ``Static_Budget_Base&RO_by_Month.csv``.

    Why Plotly (not ``st.area_chart``)
    -----------------------------------
    ``st.area_chart`` produces a stacked area by default but does
    NOT expose colour overrides per series, and cannot overlay an
    arbitrary reference line.  The planner explicitly wants the
    Base/R&O colours from the reference screenshot (dark blue /
    orange) AND the dotted budget line, so we drive the chart
    directly through a Plotly figure.  This module already lists
    ``plotly`` as a required dependency (see ``requirements.txt``).
    """
    chart_df = result.chart_long
    if chart_df.empty:
        # Already handled upstream (empty pivot returns early), but
        # double-guard so the chart helper is safe to call from
        # anywhere in the future.
        return

    st.markdown("**Month-over-Month: Actual → Forecast**")
    if result.has_budget_data:
        st.caption(
            "Stitched monthly area in millions of pounds — **Actual** "
            "(IBP Shipments) for the actual months, **Base Plan** + **R&O** "
            "(tracker) for the forecast months — with a **green** dotted "
            "**Total Budget** line from `Static_Budget_Base&RO_by_Month.csv`.  "
            "Updates live with the filters above; the budget line is static "
            "per month."
        )
    else:
        st.caption(
            "Stitched monthly area in millions of pounds — **Actual** "
            "(IBP Shipments) for the actual months, **Base Plan** + **R&O** "
            "(tracker) for the forecast months.  Updates live with the "
            "filters above.  _Total Budget line unavailable — "
            "`Static_Budget_Base&RO_by_Month.csv` could not be read "
            "from Fabric._"
        )

    # Pivot the long frame into one column per series so each ``go.Scatter``
    # trace can read its full y-vector in one indexer call — cleaner than
    # filtering the long frame twice.
    wide = chart_df.pivot_table(
        index="Month",
        columns="Forecast Type",
        values="Pounds_M",
        aggfunc="sum",
        fill_value=0.0,
        observed=True,
    ).sort_index()

    # Format x-axis ticks as ``M/YY`` — the screenshot uses that short
    # form so the labels don't crowd at typical chart widths.  We build
    # the label manually (rather than via the POSIX-only ``%-m``
    # strftime token) so the formatter works on Windows AND POSIX
    # without an OS-specific branch.
    x_labels = [
        f"{pd.Timestamp(d).month}/{pd.Timestamp(d).strftime('%y')}"
        for d in wide.index
    ]

    fig = go.Figure()

    # Actual trace — teal, drawn first (bottom of the stack).  Actual
    # months carry shipment pounds while Base/R&O are zero there (and
    # vice-versa for forecast months), so sharing ``stackgroup="one"``
    # across all three series yields a single continuous month-over-month
    # area: actuals on the left, forecast on the right, no double-count.
    if SERIES_ACTUAL in wide.columns:
        fig.add_trace(go.Scatter(
            x=x_labels,
            y=wide[SERIES_ACTUAL].tolist(),
            name=SERIES_ACTUAL,
            mode="lines",
            stackgroup="one",
            fillcolor="#2ca6a4",         # teal — distinct from Base/R&O
            line=dict(width=0.5, color="#2ca6a4"),
            hovertemplate=(
                "<b>%{x}</b><br>"
                f"{SERIES_ACTUAL} (Shipments): "
                "%{y:.1f} M lbs<extra></extra>"
            ),
        ))

    # Base Plan trace — dark blue, drawn next.  ``stackgroup`` ties traces
    # into the same stack; sharing one group across the series gives us
    # the stitched stacked-area look the planner expects.
    if FORECAST_BASE_PLAN in wide.columns:
        fig.add_trace(go.Scatter(
            x=x_labels,
            y=wide[FORECAST_BASE_PLAN].tolist(),
            name=FORECAST_BASE_PLAN,
            mode="lines",
            stackgroup="one",
            fillcolor="#1f4e79",         # dark blue (matches screenshot)
            line=dict(width=0.5, color="#1f4e79"),
            hovertemplate=(
                "<b>%{x}</b><br>"
                f"{FORECAST_BASE_PLAN}: "
                "%{y:.1f} M lbs<extra></extra>"
            ),
        ))

    # R&O trace — orange, drawn on top of Base Plan.
    if FORECAST_R_AND_O in wide.columns:
        fig.add_trace(go.Scatter(
            x=x_labels,
            y=wide[FORECAST_R_AND_O].tolist(),
            name=FORECAST_R_AND_O,
            mode="lines",
            stackgroup="one",
            fillcolor="#ed7d31",         # orange (matches screenshot)
            line=dict(width=0.5, color="#ed7d31"),
            hovertemplate=(
                "<b>%{x}</b><br>"
                f"{FORECAST_R_AND_O}: "
                "%{y:.1f} M lbs<extra></extra>"
            ),
        ))

    # Total Budget overlay — green dotted line per month from Fabric.
    if result.has_budget_data and len(wide.index) > 0:
        budget_y: list[float] = []
        for month_date in wide.index:
            # Keys match pivot month columns (%Y-%m).
            month_label = pd.Timestamp(month_date).strftime("%Y-%m")
            budget_y.append(float(result.budget_by_month.get(month_label, float("nan"))))

        fig.add_trace(go.Scatter(
            x=x_labels,
            y=budget_y,
            name="Total Budget (Base + R&O)",
            mode="lines",
            line=dict(color="#2ca02c", width=3, dash="dot"),
            hovertemplate=(
                "<b>%{x}</b><br>"
                "Total Budget: %{y:.1f} M lbs<extra></extra>"
            ),
        ))

    fig.update_layout(
        height=360,
        margin=dict(l=40, r=20, t=10, b=40),
        legend=dict(
            orientation="h",
            yanchor="bottom", y=-0.25,
            xanchor="center", x=0.5,
        ),
        xaxis=dict(title=None, tickangle=0),
        yaxis=dict(title="Millions of lbs.", rangemode="tozero"),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True, theme=None)


# ─────────────────────────────────────────────────────────────────────────────
# Demand Plan Comparison Summary (cycle-over-cycle)
# ─────────────────────────────────────────────────────────────────────────────
#
# Renders below the Demand MOM Summary.  Pulls plan numbers from
# ``qry_mgmt_plan_history_tracker.csv``, actuals from ``dbo.IBP
# Shipments``, dimensions/brand from ``qry_pdh.csv``, and the R&O column
# from the saved RO Summary Report.  All heavy lifting lives in
# ``data_sources.demand_plan_comparison``; this layer only wires widgets
# → filters → builder → a styled table.


def _render_demand_plan_comparison_section() -> None:
    """Render the Demand Plan Comparison Summary header + fragment.

    The static header lives outside the fragment so cycle / month-range
    changes rerun only the interactive block, not the surrounding text.
    """
    st.markdown("### 🔀 Demand Plan Comparison Summary")
    st.caption(
        "Cycle-over-cycle comparison from "
        "**`qry_mgmt_plan_history_tracker.csv`** (plan — now carrying "
        "Portfolio Major / Supply Format / Portfolio Minor) and **`dbo.IBP "
        "Shipments`** (actuals).  Pick a current vs prior cycle, the actual "
        "and forecast month ranges (which must not overlap), the month "
        "treated as *Prior Month*, and optionally filter by Portfolio Major "
        "/ Supply Format.  All values are in **millions of pounds**."
    )
    # Spell out — in plain English — what each KPI tile and each table column
    # means, in the same left→right order the planner reads them on screen.
    # Foldable so the reference text doesn't crowd the metrics + table
    # (collapsed by default).  "prior cycle" / "current cycle" mirror the two
    # cycles selected in the filter — the KPI tiles print the exact codes.
    with st.expander("ℹ️ How the metrics and columns are built", expanded=False):
        st.markdown(
            "_(Actual window = `[Actual Start … Actual End]`, "
            "Forecast window = `[Forecast Start … Forecast End]`.  "
            "\"prior cycle\" and \"current cycle\" follow the filter above.)_\n"
            "\n"
            "**KPI strip — Row 1 (cycle-over-cycle walk):**\n"
            "- **Last Plan** — the prior cycle's total forecast (baseline + "
            "R&O), i.e. what we thought the plan was last cycle.\n"
            "- **PM Actual Var.** — how prior-month shipments came in vs the "
            "prior cycle's forecast for that same month.\n"
            "- **Base Plan Var.** — how the baseline plan moved from the "
            "prior cycle to the current cycle.\n"
            "- **R&O Var.** — how the R&O plan moved from the prior cycle "
            "to the current cycle.  Same cell as the table's **R&O Var.** "
            "column: sourced from *FY27 Probabilized | Total Δ* on the saved "
            "**RO Summary Report**, so it ties by construction.\n"
            "- **Current Plan** — the current cycle's total forecast "
            "(baseline + R&O), i.e. this cycle's view of the plan.\n"
            "- Identity: **Base Plan Var. + PM Actual Var. + R&O Var. = "
            "Total Delta = Current Plan − Last Plan.**\n"
            "\n"
            "**KPI strip — Row 2 (YoY / share context):**\n"
            "- **T3M / T6M YoY** — trailing 3- / 6-month shipments vs the "
            "same months a year ago.\n"
            "- **Full-Year Base vs PY%** — Base plan (Current Plan − R&O) vs "
            "Prior-Year Actual over the full plan horizon.\n"
            "- **R&O % of Current Plan** — R&O share of the current plan.\n"
            "- **Total B2C Plan vs Budget %** — Current Plan vs Budget.\n"
            "\n"
            "**Summary table (Base vs PY %) + the metric tiles read the "
            "SAME filtered data as the detailed table** — the Portfolio "
            "Major · Supply Format · Brand filter above narrows the whole "
            "section, so removing e.g. Butter · … · Private updates the "
            "summary's Butter row and the tiles to match the detailed table.\n"
            "\n"
            "**Table columns:**\n"
            "- **Current Plan (Base)** / **Current Plan (R&O)** — the "
            "current-cycle forecast over the Forecast window, split by "
            "Forecast Type (Base Plan vs R&O).\n"
            "- **Current Plan (incl. RO)** — actual shipments over the "
            "Actual window ＋ Current Plan (Base) ＋ Current Plan (R&O).\n"
            "- **Last Plan (incl. RO)** — one-month-ago estimate: actuals "
            "over `[Actual Start … Actual End − 1]` ＋ the prior-cycle "
            "forecast over `[Forecast Start − 1 … Forecast End]`.\n"
            "- **O% of Current Plan** — Current Plan (R&O) ÷ Current Plan "
            "(incl. RO).\n"
            "- **PY Actual** — prior-year shipments over the plan's full "
            "horizon shifted back 12 months.\n"
            "- **Total Delta** = Current Plan (incl. RO) − Last Plan (incl. RO).\n"
            "- **PM Actual Var.** — Prior-Month actual shipments − "
            "prior-cycle forecast for the selected Prior Month.\n"
            "- **Base Plan Var.** — the residual Total Delta − PM Actual "
            "Var. − R&O Var., so the three variances always add to Total "
            "Delta.\n"
            "- **Base Plan Var %** — Base Plan Var. as a share of the "
            "prior cycle's baseline forecast (i.e. Base Plan Var. ÷ "
            "(Current Plan (Base) − Base Plan Var.)).\n"
            "- **R&O Var.** — wired to the **RO Comparison** table: it reads "
            "the *FY27 Probabilized | Total Δ* by hierarchy path from the saved "
            "**RO Summary Report** (the output of the 🔁 RO Comparison section "
            "above), matched to each row. It is an external opportunity figure — "
            "**not** a cycle-over-cycle delta of the tracker's R&O rows — so use "
            "the RO Comparison table as its source of truth / sanity check. "
            "(The APS section computes its R&O Var differently: the current − "
            "prior cycle delta of the tracker's own R&O rows.)"
        )
    _render_demand_plan_comparison_fragment()


# ── Cached build layer ───────────────────────────────────────────────────────
#
# Streamlit reruns the entire fragment body on every widget event.  The
# Fabric *reads* are cached, but the *builds* (PDH-enrich → comparison →
# drivers) used to recompute every time, costing the bulk of the
# fragment latency.  The helpers below wrap each build in
# ``@st.cache_data`` keyed on a lightweight **signature** (etag /
# row-count tuple + the filters), so two reruns with the same data +
# selection hit the cache.  The actual DataFrames go through
# underscore-prefixed args, which Streamlit excludes from hashing — we
# never pay the cost of hashing a 356k-row tracker.
#
# Why ``@st.cache_data`` and not ``@st.cache_resource``: each output is
# an immutable value (DataFrame + DataFrames) we want copied on read.
# That's exactly the cache_data contract; cache_resource would risk
# downstream mutation leaking back into the cached slot.

# Session-state key for the user's "Load comparison" opt-in.  Persisted
# across reruns so the planner doesn't have to re-click on every
# interaction once they've expanded the section.
_DPC_ENABLED_KEY: str = "demand_plan_comparison_enabled"
# Planner's chosen visible metric columns for the detailed table (the ⚙️
# Columns picker below the table).  Defaults to everything except the
# extra-detail set; every column is individually hidable.
_DPC_DETAIL_COLS_KEY: str = "demand_plan_comparison_detail_cols"
# One-shot flag: force the tracker re-read on the next fragment run (set by
# the Generate / Regenerate action so it always pulls the LATEST file).
_DPC_FORCE_TRACKER_REFRESH_KEY: str = "demand_plan_comparison_force_tracker"
# One-shot success banner shown after a backfill (survives the rerun).
_DPC_BACKFILL_BANNER_KEY: str = "demand_plan_comparison_backfill_banner"


def _dpc_generate(tracker_df: pd.DataFrame) -> None:
    """Run the *Generate Demand Plan Comparison Summary* action, then rerun.

    1. If the history tracker is missing Portfolio Major / Supply Format /
       Portfolio Minor, backfill them (archive the previous files, fill from
       PDH → RO_Item_Master, save) via
       :func:`backfill_plan_attribute_columns` — persisted so the comparison
       reads the dims straight off the file thereafter.
    2. Force a fresh tracker read on the coming rerun (pull the LATEST file,
       incl. any columns just written) and mark the section enabled.

    Shared by the first-time Generate button and the Regenerate button so the
    backfill-then-build logic lives in exactly one place.
    """
    if not tracker_has_dim_columns(tracker_df):
        with st.spinner(
            "Adding Portfolio Major / Minor / Supply Format to the tracker "
            "(PDH → RO_Item_Master) and archiving the previous file…"
        ):
            res = backfill_plan_attribute_columns()
        if res.ok:
            st.session_state[_DPC_BACKFILL_BANNER_KEY] = (
                "✅ Added Portfolio Major / Minor / Supply Format to "
                "`qry_mgmt_plan_history_tracker.csv` and `qry_mgmt_plan_full.csv` "
                "(previous copies archived)."
            )
        else:
            st.error(
                "❌ Could not add the categorisation columns to the tracker.\n\n"
                + "\n".join(res.errors)
            )
            return
    st.session_state[_DPC_FORCE_TRACKER_REFRESH_KEY] = True
    st.session_state[_DPC_ENABLED_KEY] = True
    st.rerun(scope="fragment")

# TTL for the derived (build) outputs.  60 minutes — matches the bumped
# raw-CSV TTL so the build cache never out-lives its inputs.  The "🔄
# Refresh from Fabric" button clears the raw cache (and the page reruns,
# so the build cache misses cleanly on the next render).
_CACHE_TTL_SECONDS_OUTPUTS: int = 60 * 60


def _fy27_budget_workbook_etag() -> str:
    """Cheap cache-bust key for the FY27 budget xlsx in Fabric."""
    try:
        from data_sources.fabric_lakehouse_io import get_file_properties
        props = get_file_properties("fabric_htst", fy27_budget_blob_path())
        return str(getattr(props, "etag", "") or "")
    except Exception:
        return ""


def _signature_for(df: Optional[pd.DataFrame]) -> tuple[int, int]:
    """Return a cheap ``(rows, cols)`` signature for a DataFrame.

    Skips full hashing — a row-count + column-count change is the
    overwhelming majority of "data changed" signals on these slow-moving
    Fabric exports.  The TTL on the underlying cached read covers the
    long-tail freshness case, and the explicit "Refresh from Fabric"
    button still busts everything.
    """
    if df is None or df.empty:
        return (0, 0)
    return (int(len(df)), int(len(df.columns)))


# NOTE on the cache decorator choice
# -----------------------------------
# * ``@st.cache_resource`` for the **shared enrichment bundle** —
#   we want the SAME object reference handed to all three builders so
#   downstream consumers re-use the in-memory DataFrames; pickling would
#   defeat the share.  ``cache_resource`` also sidesteps the
#   ``UnserializableReturnValueError`` previously seen on dataclass
#   return values (the cached object is held by reference, never
#   round-tripped through pickle).
# * ``@st.cache_data`` for the **per-filter build outputs** — each
#   cached return is a value (DataFrame or native tuple) safe to copy
#   on read.  We return a native tuple from the comparison build and
#   reconstruct ``ComparisonResult`` outside the cache, mirroring the
#   defensive pattern already used in ``demand_summary._cached_fetch``.

@st.cache_resource(ttl=_CACHE_TTL_SECONDS_OUTPUTS, show_spinner=False)
def _cached_enriched_sources(
    tracker_sig: tuple, ibp_sig: tuple, ibp_orders_sig: tuple, ibp_py_sig: tuple,
    ibp_recent_sig: tuple, ibp_recent_py_sig: tuple,
    pdh_sig: tuple, item_master_sig: tuple,
    _tracker_df: pd.DataFrame,
    _ibp_df: Optional[pd.DataFrame],
    _ibp_orders_df: Optional[pd.DataFrame],
    _ibp_py_df: Optional[pd.DataFrame],
    _ibp_recent_df: Optional[pd.DataFrame],
    _ibp_recent_py_df: Optional[pd.DataFrame],
    _pdh_df: Optional[pd.DataFrame],
    _item_master_df: Optional[pd.DataFrame],
) -> EnrichedSources:
    """Cache the shared dim-joined tracker + IBP frames (by reference).

    Built once per unique ``(tracker, ibp, pdh, RO_Item_Master)`` signature,
    shared by the comparison builder and BOTH driver builders.  Dimensions
    resolve through the PDH → RO_Item_Master cascade.  Leading-underscore
    arg names tell Streamlit to skip hashing the DataFrames themselves
    (we already key on their signature tuples).
    """
    return build_enriched_sources(
        _tracker_df, _ibp_df, _ibp_orders_df, _pdh_df,
        item_master_df=_item_master_df, ibp_py_df=_ibp_py_df,
        ibp_recent_df=_ibp_recent_df, ibp_recent_py_df=_ibp_recent_py_df,
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS_OUTPUTS, show_spinner=False)
def _cached_fy27_budget_by_row_id(budget_etag: str) -> tuple[dict[str, float], tuple[str, ...]]:
    """Cache FY27 leaf budgets keyed by comparison ``row_id``."""
    result = fetch_fy27_budget_by_row_id()
    return dict(result.by_row_id), tuple(result.warnings)


@dataclass(frozen=True)
class _ComparisonSupportingSources:
    """The IBP / PDH / PY / trailing-window + FY27-budget sources shared by the
    IBP and APS comparison render paths (see
    :func:`_load_comparison_supporting_sources`)."""
    pdh_df: pd.DataFrame
    item_master_df: pd.DataFrame
    ibp_df: pd.DataFrame
    ibp_warning: Optional[str]
    ibp_orders_df: pd.DataFrame
    ibp_orders_warning: Optional[str]
    ibp_py_df: pd.DataFrame
    ibp_recent_df: pd.DataFrame
    ibp_recent_py_df: pd.DataFrame
    budget_by_row_id: dict
    budget_warnings: tuple
    budget_lookup_key: tuple


@st.cache_data(ttl=_CACHE_TTL_SECONDS_OUTPUTS, show_spinner=False)
def _cached_comparison_order_yoy(
    prior_month: date,
) -> tuple[dict[str, dict[str, Optional[float]]], dict[str, tuple[str, str]]]:
    """Per-row **L3M / L6M Order YoY** + window labels, anchored on *prior_month*.

    Loads the 24 months of IBP Orders the L12M current + year-ago windows need,
    enriches them, and reuses :func:`build_business_health` (orders only) so the
    comparison's L3M/L6M Order YoY reconcile with the Business Health section by
    construction.  Returns ``({row_id: {"L3M": frac|None, "L6M": frac|None}},
    window_labels)``; a failed Orders read degrades to empty (blank YoY).
    """
    cur12 = last_n_months(prior_month, 12)
    window = tuple(sorted(cur12 | {shift_year_back(m) for m in cur12}))
    try:
        orders_df, _ = _load_demand_comparison_ibp_orders(months=window)
        enriched = enrich_ibp_orders_df(orders_df, _load_demand_comparison_pdh())
    except (LakehouseIOError, ValueError):
        enriched = None
    res = build_business_health(enriched, None, prior_month)
    yoy = {
        str(r[DPC_COL_ROW_ID]): {
            "L3M": r[BH_YOY_LABELS["L3M"]], "L6M": r[BH_YOY_LABELS["L6M"]]}
        for _, r in res.table.iterrows()
    }
    return yoy, res.window_labels


def _comparison_period_labels(
    filters: ComparisonFilters, order_labels: dict[str, tuple[str, str]],
) -> dict[str, str]:
    """Month-range 2nd-row strings for the YoY summary columns / tiles.

    Keyed by the summary column HEADER so both the table header and the metric
    tiles can look each range up.  Base Plan spells out its two legs explicitly
    (actuals over the Actual window + baseline over the Forecast window).
    """
    def _rng(a: date, b: date) -> str:
        return f"{a:%b%y}–{b:%b%y}"

    actual = _rng(filters.actual_start, filters.actual_end)
    forecast = _rng(filters.forecast_start, filters.forecast_end)
    py = _rng(shift_year_back(filters.actual_start), shift_year_back(filters.forecast_end))
    return {
        "Base plan": f"{actual} Actual + {forecast} Fcst",
        "PY": py,
        "R&O vol": forecast,
        "L3M Order YoY": order_labels.get("L3M", ("", ""))[0],
        "L6M Order YoY": order_labels.get("L6M", ("", ""))[0],
    }


def _load_comparison_supporting_sources(
    filters: ComparisonFilters,
) -> _ComparisonSupportingSources:
    """Load every supporting source the comparison build needs (post opt-in).

    Shared verbatim by the IBP fragment and the APS comparison section so the
    window arithmetic + Fabric reads live in ONE place.  Each loader is
    independent, so a single failing source degrades to empty/zeros rather than
    short-circuiting the others.  Windows:
      * ``ibp_df`` — actuals + prior-month shipments (for PM Actual).
      * ``ibp_py_df`` — the plan's full horizon shifted back 12 months (PY Actual).
      * ``ibp_recent_df`` / ``ibp_recent_py_df`` — trailing 6 months (T3M/T6M YoY).
      * FY27 budget workbook, keyed by comparison ``row_id`` (the Budget column).
    """
    pdh_df = _load_demand_comparison_pdh()
    item_master_df = _load_mom_item_master()   # RO_Item_Master fallback dims
    actual_months = months_in_range(filters.actual_start, filters.actual_end)
    prior_month_set = {filters.prior_month.replace(day=1)}
    ibp_df, ibp_warning = _load_demand_comparison_ibp(
        months=tuple(sorted(actual_months | prior_month_set)))
    ibp_orders_df, ibp_orders_warning = _load_demand_comparison_ibp_orders(
        months=tuple(sorted(prior_month_set)))
    py_window = tuple(sorted(months_in_range(
        shift_year_back(filters.actual_start), shift_year_back(filters.forecast_end))))
    ibp_py_df, _ = _load_demand_comparison_ibp(months=py_window)
    recent_cur = last_n_months(filters.actual_end, 6)
    ibp_recent_df, _ = _load_demand_comparison_ibp(months=tuple(sorted(recent_cur)))
    ibp_recent_py_df, _ = _load_demand_comparison_ibp(
        months=tuple(sorted(shift_year_back(m) for m in recent_cur)))
    budget_etag = _fy27_budget_workbook_etag()
    budget_by_row_id, budget_warnings = _cached_fy27_budget_by_row_id(budget_etag)
    budget_lookup_key = tuple(sorted(budget_by_row_id.items()))
    return _ComparisonSupportingSources(
        pdh_df=pdh_df, item_master_df=item_master_df,
        ibp_df=ibp_df, ibp_warning=ibp_warning,
        ibp_orders_df=ibp_orders_df, ibp_orders_warning=ibp_orders_warning,
        ibp_py_df=ibp_py_df, ibp_recent_df=ibp_recent_df,
        ibp_recent_py_df=ibp_recent_py_df,
        budget_by_row_id=budget_by_row_id, budget_warnings=budget_warnings,
        budget_lookup_key=budget_lookup_key)


@st.cache_data(ttl=_CACHE_TTL_SECONDS_OUTPUTS, show_spinner=False)
def _cached_demand_plan_comparison_payload(
    sig_key: tuple,
    filters: ComparisonFilters,
    ro_lookup_key: tuple,
    budget_lookup_key: tuple,
    _enriched: EnrichedSources,
    _ro_lookup: dict,
    _budget_by_row_id: dict[str, float],
    shift_last_plan_window: bool = True,
    ro_var_from_tracker: bool = False,
) -> tuple[pd.DataFrame, tuple[str, ...], bool]:
    """Cache the comparison build, returning a NATIVE tuple.

    Returns ``(table, warnings_tuple, ro_summary_available)`` so the
    cache only stores DataFrames + tuples + bool — avoiding the
    pickle-class-identity hazard with custom dataclasses.  The caller
    rebuilds the :class:`ComparisonResult` outside the cache.

    ``sig_key`` rolls the enriched-sources signature so a fresh tracker
    / IBP / PDH read invalidates the cached result; ``ro_lookup_key`` is
    a tuple summary of the RO Summary lookup so editing the RO Summary
    Report also invalidates the cached comparison.
    """
    result = build_demand_plan_comparison(
        None, None, None, filters,
        ro_total_delta_by_path=_ro_lookup,
        enriched=_enriched,
        budget_by_row_id=_budget_by_row_id,
        shift_last_plan_window=shift_last_plan_window,
        ro_var_from_tracker=ro_var_from_tracker,
    )
    return result.table, tuple(result.warnings), bool(result.ro_summary_available)


@st.cache_data(ttl=_CACHE_TTL_SECONDS_OUTPUTS, show_spinner=False)
def _cached_prior_month_actual_vs_fcst_table(
    sig_key: tuple,
    filters: ComparisonFilters,
    _enriched: EnrichedSources,
) -> pd.DataFrame:
    """Cache the Prior Month Actual vs Fcst table per data/filter signature."""
    return build_prior_month_actual_vs_fcst_table(
        None, None, None, None, filters, enriched=_enriched,
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS_OUTPUTS, show_spinner=False)
def _cached_base_plan_driver_table(
    sig_key: tuple, filters: ComparisonFilters, dim_sig: tuple,
    _enriched: EnrichedSources, _dim_df: Optional[pd.DataFrame],
) -> DriverTableResult:
    """Cache the Base Plan driver build per ``(data signature, filters, dim)``."""
    return build_base_plan_driver_table(
        None, None, _dim_df, filters, enriched=_enriched,
    )


@st.cache_data(ttl=_CACHE_TTL_SECONDS_OUTPUTS, show_spinner=False)
def _cached_pm_actual_driver_table(
    sig_key: tuple, filters: ComparisonFilters, dim_sig: tuple,
    _enriched: EnrichedSources, _dim_df: Optional[pd.DataFrame],
) -> DriverTableResult:
    """Cache the PM Actual driver build per ``(data signature, filters, dim)``."""
    return build_pm_actual_driver_table(
        None, None, None, _dim_df, filters, enriched=_enriched,
    )


def _ro_lookup_signature(ro_lookup: dict) -> tuple:
    """Return a cheap, hashable summary of the RO Summary lookup.

    Captures total-row count + sum of values — enough to detect any
    edit/refresh of the underlying RO Summary Report without re-hashing
    the entire dict on every cached call.
    """
    if not ro_lookup:
        return (0, 0.0)
    try:
        return (len(ro_lookup), round(float(sum(ro_lookup.values())), 4))
    except (TypeError, ValueError):
        return (len(ro_lookup), 0.0)


@st.fragment
def _render_demand_plan_comparison_fragment() -> None:
    """Load sources, render the pickers, build + render the comparison.

    Render flow
    -----------
    1. Tracker (the spine — used for the pickers too) is loaded first.
    2. Pickers + validation render before any expensive supporting
       sources are touched, so an invalid selection short-circuits
       without an IBP/PDH read.
    3. The expensive comparison + driver-table builds are gated behind
       an opt-in toggle stored in :data:`st.session_state` — collapsing
       the expander or first-paint no longer forces a build.
    4. PDH / IBP / RO Summary / dim are loaded only when the user opted
       in; each loader degrades gracefully.
    5. Shared :class:`EnrichedSources` is cached and reused by all three
       builders so the PDH-merge cost is paid exactly once per signature.
    """
    # 1. Tracker (cheap CSV read, lives behind the 60-min cache).  The
    #    Generate button sets a one-shot force-refresh flag so a click always
    #    pulls the LATEST tracker (and picks up columns just written by a
    #    backfill).
    force_tracker = bool(st.session_state.pop(_DPC_FORCE_TRACKER_REFRESH_KEY, False))
    try:
        with st.spinner("Reading qry_mgmt_plan_history_tracker.csv from Microsoft Fabric…"):
            tracker_snapshot = fetch_mgmt_plan_history_tracker(force_refresh=force_tracker)
    except DemandSummaryError as exc:
        st.error(
            "❌ Could not load **qry_mgmt_plan_history_tracker.csv** for "
            f"the comparison.\n\n{exc}"
        )
        return
    tracker_df = tracker_snapshot.df
    if tracker_df is None or tracker_df.empty:
        st.info(
            "ℹ️ `qry_mgmt_plan_history_tracker.csv` is empty — nothing to "
            "compare.  Check the upstream Fabric pipeline."
        )
        return

    # 2. Picker data + selection.
    cycles = list_tracker_cycles(tracker_df)
    months = list_tracker_months(tracker_df)
    if len(cycles) < 2:
        st.warning(
            "Need at least two distinct **Cycle** values in the tracker to "
            f"compare — found: {cycles or 'none'}."
        )
        return
    if not months:
        st.warning(
            "No parseable **Start of Month** values in the tracker — cannot "
            "build the month-range pickers."
        )
        return

    # Actual-range months come from IBP Shipments (the actuals' true
    # source), so the planner can begin the actual window at a month the
    # tracker doesn't carry (e.g. April 2026).  Cheap DISTINCT "Month"
    # scan; on failure fall back to the tracker months.
    try:
        actual_months = list(fetch_ibp_shipments_months())
    except IBPOfficialSourceError as exc:
        logger.info("IBP Shipments months unavailable for Actual pickers: %s", exc)
        actual_months = []
    if not actual_months:
        actual_months = months

    # Concatenated Portfolio Major · Supply Format · Brand combos for the
    # filter: the tracker's planned B2C combos (ESL / Fresh Milk / Cultured /
    # Butter — no Powders / Cheese / Bulk Fluid) UNION the Butter item catalog
    # (so Private / Chips / Elgin Solid are selectable).  PDH is a small, cached
    # read; on failure it degrades to tracker-only combos.
    combo_options = list_comparison_combos(tracker_df, _load_demand_comparison_pdh())
    # Foldable filters — wrapping the call renders every picker inside the
    # expander.  Expanded by default so the active window is visible.
    with st.expander("🔍 Filters", expanded=True):
        filters = _render_demand_comparison_filters(
            cycles, months, actual_months, combo_options)
    errors = validate_filters(filters)
    if errors:
        for msg in errors:
            st.error(f"❌ {msg}")
        return

    # 3. Generate control — ALWAYS visible at the top of the section (both
    #    first build and re-generate use this one button), separate from the
    #    Base-Plan "append new history" uploader.  The heavy build runs only
    #    after a click and stays live across reruns so picker / filter changes
    #    refine the view without re-clicking.
    enabled = st.session_state.get(_DPC_ENABLED_KEY, False)

    # One-shot backfill confirmation (set by _dpc_generate, survives the rerun).
    backfill_banner = st.session_state.pop(_DPC_BACKFILL_BANNER_KEY, None)
    if backfill_banner:
        st.success(backfill_banner)

    with st.expander("ℹ️ What the Generate button does", expanded=not enabled):
        st.markdown(
            "Builds this Comparison Summary **from the existing "
            "`qry_mgmt_plan_history_tracker.csv`** (does NOT append a new "
            "cycle — that's the Base-Plan uploader in Demand Summary):\n"
            "1. Reads the **latest** history tracker from the lakehouse.\n"
            "2. If it's missing **Portfolio Major / Portfolio Minor / Supply "
            "Format**, adds those columns and fills them (PDH → "
            "RO_Item_Master), **archiving the previous file first**, then "
            "saves — so categorisation lives on the file itself.\n"
            "3. Builds the comparison table, the headline KPI tiles, and the "
            "**not-captured** logs (prior cycle · current cycle · actual "
            "shipments).\n\n"
            "_Tip: **Save the RO Summary Report above first** so the **R&O** "
            "column is populated._"
        )

    if st.button(
        ("🔄 Regenerate Demand Plan Comparison Summary (re-pull latest tracker)"
         if enabled else "▶ Generate Demand Plan Comparison Summary"),
        key="demand_plan_comparison_generate",
        type="primary",
        width="stretch",
        help=(
            "Uses the EXISTING qry_mgmt_plan_history_tracker.csv — adds the "
            "Portfolio Major / Minor / Supply Format columns if missing, then "
            "builds the comparison, KPIs and not-captured logs."
        ),
    ):
        _dpc_generate(tracker_df)

    if not enabled:
        st.info(
            "👆 Click **Generate Demand Plan Comparison Summary** to build the "
            "table from the latest history tracker."
        )
        return

    # 4. Heavy supporting sources — loaded only post opt-in, via the shared
    #    loader (identical windows + FY27 budget as the APS comparison section).
    _src = _load_comparison_supporting_sources(filters)
    pdh_df = _src.pdh_df
    item_master_df = _src.item_master_df
    ibp_df, ibp_warning = _src.ibp_df, _src.ibp_warning
    ibp_orders_df, ibp_orders_warning = _src.ibp_orders_df, _src.ibp_orders_warning
    ibp_py_df = _src.ibp_py_df
    ibp_recent_df = _src.ibp_recent_df
    ibp_recent_py_df = _src.ibp_recent_py_df
    budget_by_row_id = _src.budget_by_row_id
    budget_warnings = _src.budget_warnings
    budget_lookup_key = _src.budget_lookup_key
    # IBP-only supporting sources (the APS section carries no RO Summary; it
    # loads its dim frame in the driver section).
    ro_lookup = fetch_ro_summary_total_delta_by_path()
    dim_df, dim_warning = _load_demand_comparison_dim()

    # 5. Build the shared enrichment ONCE, then reuse it across all
    #    three builders.  Cached on data signatures so repeated reruns
    #    with the same data + filters are free.
    tracker_sig = _signature_for(tracker_df)
    ibp_sig = _signature_for(ibp_df)
    ibp_orders_sig = _signature_for(ibp_orders_df)
    ibp_py_sig = _signature_for(ibp_py_df)
    ibp_recent_sig = _signature_for(ibp_recent_df)
    ibp_recent_py_sig = _signature_for(ibp_recent_py_df)
    pdh_sig = _signature_for(pdh_df)
    item_master_sig = _signature_for(item_master_df)
    dim_sig = _signature_for(dim_df)
    ro_sig = _ro_lookup_signature(ro_lookup)
    enrich_sig = (
        tracker_sig, ibp_sig, ibp_orders_sig, ibp_py_sig,
        ibp_recent_sig, ibp_recent_py_sig, pdh_sig, item_master_sig)

    with st.spinner("Building Demand Plan Comparison Summary…"):
        enriched = _cached_enriched_sources(
            tracker_sig, ibp_sig, ibp_orders_sig, ibp_py_sig,
            ibp_recent_sig, ibp_recent_py_sig, pdh_sig, item_master_sig,
            tracker_df, ibp_df, ibp_orders_df, ibp_py_df,
            ibp_recent_df, ibp_recent_py_df, pdh_df, item_master_df,
        )
        table, build_warnings, ro_available = _cached_demand_plan_comparison_payload(
            enrich_sig + (ro_sig, budget_lookup_key),
            filters,
            ro_sig,
            budget_lookup_key,
            enriched,
            ro_lookup,
            budget_by_row_id,
        )
        prior_month_vs_fcst = _cached_prior_month_actual_vs_fcst_table(
            enrich_sig + (filters.prior_month,), filters, enriched,
        )
    # Reconstruct the dataclass OUTSIDE the cache (the cache stores
    # native values only — see the decorator-choice note above).
    result = ComparisonResult(
        table=table, warnings=build_warnings, ro_summary_available=ro_available,
    )

    # 6. Surface advisories (missing IBP column, empty PDH, no RO Summary, no dim).
    warnings = list(build_warnings)
    if ibp_warning:
        warnings.insert(0, ibp_warning)
    if ibp_orders_warning:
        warnings.append(ibp_orders_warning)
    if dim_warning:
        warnings.append(dim_warning)
    warnings.extend(budget_warnings)
    for msg in warnings:
        st.warning(f"⚠️ {msg}")

    # 6b. "SKUs not captured" reconciliation logs (prior cycle · current
    #     cycle · actual shipments), surfaced above the table so the planner
    #     reconciles before trusting the totals.  Categorised off the dims
    #     carried on the tracker (filled from PDH → RO_Item_Master); the
    #     same PDH → RO_Item_Master cascade is passed through so blank
    #     Item Description / Portfolio Major / Supply Format on unclassified
    #     items get filled from those sources rather than rendering empty.
    _not_captured_dim = build_item_dim_frame_cascade(pdh_df, item_master_df)
    _render_comparison_not_captured_logs(
        build_comparison_not_captured(
            enriched.tracker, filters,
            ibp_enriched=enriched.ibp, dim_frame=_not_captured_dim,
        ),
    )

    # 6c. Executive story, top → bottom:
    #     (0) the summary table's Columns picker (sits ABOVE the metrics),
    #     (1) YoY / share metrics row (incl. Total B2C Plan vs Budget %),
    #     (2) the condensed current-plan summary table (screenshot 2),
    #     (3) a divider clearly splitting the two sections,
    #     (4) the cycle-walk ("Prior Plan") metrics row, then
    #     (5) the full restyled comparison table.
    # Everything here reads the FILTERED ``result`` / ``kpis`` — the Portfolio
    # Major · Supply Format · Brand filter narrows the summary table AND the
    # metric tiles, not just the detailed table below.
    st.markdown("#### 📈 YoY Comparison")
    _render_comparison_summary_col_picker()
    kpis = build_comparison_kpis(
        result.table, enriched.ibp_recent, enriched.ibp_recent_py, filters,
    )
    # L3M/L6M **Order** YoY (trailing IBP Orders, anchored on the Prior Month) +
    # the month-range 2nd rows / tile sub-labels.
    _order_yoy, _order_labels = _cached_comparison_order_yoy(filters.prior_month)
    _periods = _comparison_period_labels(filters, _order_labels)
    _render_comparison_kpis_yoy(
        kpis, order_yoy_total=_order_yoy.get("total_b2c", {}), periods=_periods)
    _render_comparison_summary_table(
        result, order_yoy_by_row=_order_yoy, periods=_periods)
    _render_comparison_mix_table(result)

    st.markdown("---")

    # 6d. Forecast Bias (rolling lag-1) by Segment × Month — its own section,
    #     divider-bracketed between the YoY block and the cycle-over-cycle table.
    _render_forecast_bias_section(tracker_df, pdh_df, item_master_df, filters)

    st.markdown("---")

    st.markdown("#### 🔄 Cycle over Cycle Comparison")
    _render_comparison_kpis_walk(kpis, filters)
    # 7. Render the comparison table + download / save (plan columns anchored
    #    on the chosen prior / current cycles).
    _render_demand_comparison_table(result, filters)

    # 7b. SKU-level build-up of the cycle-over-cycle rows (foldable, filterable).
    _render_sku_cycle_drilldown(enriched, filters, ns="", shift_last_plan_window=True)

    # 8. Prior Month Actual vs Fcst summary (between comparison and drivers).
    _render_prior_month_actual_vs_fcst_table(
        prior_month_vs_fcst,
        prior_cycle=filters.prior_cycle,
        prior_month=filters.prior_month,
    )

    # 8b. Prior-Month Shipment Diagnostic (reconciliation) — foldable and
    #     read-only.  Reuses the already-built enriched shipments
    #     (``enriched.ibp``), so it adds no Fabric read and NO new cache
    #     layer (sidesteps the cache-serialisation pitfall entirely) and
    #     cannot affect any other section.
    _render_prior_month_shipment_diagnostic(enriched.ibp, filters.prior_month)

    # 9. Driver tables — share the same EnrichedSources + dim signature.
    #    Foldable so the comparison + Prior-Month tables stay visible
    #    while the heavy driver drill-downs stay tucked away until needed.
    with st.expander(
        "📋 Demand Plan Comparison & Drivers Validation",
        expanded=False,
    ):
        _render_demand_comparison_driver_tables_cached(
            enrich_sig, filters, dim_sig, enriched, dim_df,
        )


def _render_comparison_not_captured_logs(
    nc: ComparisonNotCaptured, *, ns: str = "",
) -> None:
    """Render the three 'SKUs not captured' logs (prior · current · actuals).

    A SKU is *not captured* when its Portfolio Major / Supply Format / Brand /
    Portfolio Minor match no comparison-template family, so its pounds never
    reach a row — the exact gap between a raw source total and the table.
    One foldable, clearly-labelled section per leg, each with a jump link to
    RO_Item_Master.csv and a CSV download.  *ns* namespaces the widget keys so
    the APS mirror can coexist with the IBP one on the same page.
    """
    st.markdown("**🧾 SKUs not captured in the comparison**")
    st.caption(
        "Why a SKU is *not captured*: its **Portfolio Major / Supply Format / "
        "Portfolio Minor / Brand** (carried on the tracker, filled from PDH → "
        "RO_Item_Master) match **no** comparison-template family, so its "
        "forecast (cycles) or shipped (actuals) pounds never roll into a row. "
        "Classify these items in RO_Item_Master — or fix their PDH dims — to "
        "fold them in.  Each leg is listed explicitly below."
    )
    _render_one_comparison_not_captured(
        nc.prior_cycle,
        title=f"Prior cycle ({nc.prior_cycle_label}) — forecast SKUs",
        empty_note=f"Every **prior-cycle ({nc.prior_cycle_label})** forecast SKU is captured.",
        key_suffix="prior", ns=ns,
    )
    _render_one_comparison_not_captured(
        nc.current_cycle,
        title=f"Current cycle ({nc.current_cycle_label}) — forecast SKUs",
        empty_note=f"Every **current-cycle ({nc.current_cycle_label})** forecast SKU is captured.",
        key_suffix="current", ns=ns,
    )
    _render_one_comparison_not_captured(
        nc.actuals,
        title=f"Actual shipments ({nc.actual_window_label}) — shipped SKUs",
        empty_note=f"Every **actual-shipment ({nc.actual_window_label})** SKU is captured.",
        key_suffix="actuals", ns=ns,
    )


def _render_one_comparison_not_captured(
    df: pd.DataFrame, *, title: str, empty_note: str, key_suffix: str, ns: str = "",
) -> None:
    """Render one not-captured leg (success note when empty)."""
    if df is None or df.empty:
        st.caption(f"✅ {empty_note}")
        return

    with st.expander(f"⚠️ {len(df):,} {title} NOT captured", expanded=False):
        st.dataframe(df, use_container_width=True, hide_index=True)
        col_link, col_dl = st.columns(2)
        with col_link:
            st.link_button(
                "🔎 Open RO_Item_Master.csv in Fabric",
                _RO_ITEM_MASTER_FABRIC_URL,
                use_container_width=True,
            )
        with col_dl:
            today = pd.Timestamp.utcnow().strftime("%Y%m%d")
            st.download_button(
                label="⬇️ Download not-captured items (CSV)",
                data=df.to_csv(index=False).encode("utf-8"),
                file_name=f"comparison_not_captured_{key_suffix}_{today}.csv",
                mime="text/csv",
                key=_ns_key(f"cmp_not_captured_dl_{key_suffix}", ns),
                use_container_width=True,
            )


# Friendly Portfolio-Major labels for the combination filter (the tracker
# tags Fresh Milk as "HTST"); combos still filter on the raw value.
_DPC_PMAJ_DISPLAY: dict[str, str] = {"HTST": "Fresh Milk"}

# Planner-preferred default windows for the comparison filters (FY26–27 cycle).
# Named here — not inline in the widget renderer — so rolling to a new fiscal
# year is a one-line edit.  Each falls back to first/last available month when
# the preferred month isn't in the picker's options.
_DPC_DEFAULT_ACTUAL_START: date = date(2026, 4, 1)
_DPC_DEFAULT_ACTUAL_END: date = date(2026, 5, 1)
_DPC_DEFAULT_FORECAST_START: date = date(2026, 6, 1)
_DPC_DEFAULT_FORECAST_END: date = date(2027, 3, 1)
_DPC_DEFAULT_PRIOR_MONTH: date = date(2026, 5, 1)


def _render_demand_comparison_filters(
    cycles: list[str], months: list[date], actual_months: list[date],
    combo_options: list[tuple[str, str, str]] | None = None,
    *,
    prior_cycles: list[str] | None = None,
    ns: str = "",
    current_label: str = "Current cycle",
    prior_label: str = "Prior cycle",
) -> ComparisonFilters:
    """Render the cycle + month-range pickers; return a filter selection.

    Defaults are chosen to be immediately useful: the two most recent
    cycles (current vs prior), the latest month as *Prior Month*, the
    actual window as everything up to and including it, and the forecast
    window as everything after it (so the two windows start out
    disjoint).

    ``months`` (tracker ``Start of Month`` values) drives the Forecast +
    Prior-Month pickers; ``actual_months`` (IBP Shipments months — the
    actuals' true source) drives the *Actual* range pickers so the actual
    window can begin at a month the tracker doesn't carry (e.g. Apr 2026).
    """
    # Sensible default indices.
    # ── Cycle defaults: anchor on cycle ORDER, never a hard-coded label.
    #    ``cycles`` is in natural order (C1, C2, C3 …; see
    #    list_tracker_cycles), so the newest cycle is "current" and the one
    #    before it is "prior".  This stays correct as new cycles land —
    #    e.g. when C4 arrives it becomes the default current with C3 prior —
    #    rather than pinning to a specific label that goes stale each cycle.
    # Prior cycle may come from a DIFFERENT list than current (the APS mirror
    # picks current from the APS tracker, prior from the IBP tracker).  Default
    # prior = newest of its own list when the lists differ; the "one-before-
    # current" logic only applies when both share one list (the IBP section).
    prior_cycles = list(prior_cycles) if prior_cycles is not None else list(cycles)
    default_current = cycles[-1]
    if prior_cycles == list(cycles):
        default_prior = cycles[-2] if len(cycles) >= 2 else cycles[-1]
        if default_prior == default_current:  # guard very small cycle lists
            default_prior = next(
                (c for c in reversed(cycles) if c != default_current), cycles[0]
            )
    else:
        default_prior = prior_cycles[-1]

    # ── Month defaults ─────────────────────────────────────────────────
    # The computed disjoint split is the FALLBACK; the planner's preferred
    # windows (Apr–May actuals, Jun 2026–Mar 2027 forecast, May prior
    # month) override it whenever those exact months exist — actuals in the
    # IBP month list, forecast/prior in the tracker month list.
    n_months = len(months)
    last_idx = n_months - 1
    prior_fallback_idx = max(0, min(n_months // 2, n_months - 2)) if n_months >= 2 else 0

    def _month_idx(target: date, fallback: int) -> int:
        """Index of *target* in the tracker month list, or *fallback*."""
        return months.index(target) if target in months else fallback

    # Actual pickers index into ``actual_months`` (IBP Shipments), not the
    # tracker months.  Default the actual start to Apr 2026 (else earliest).
    last_actual_idx = max(0, len(actual_months) - 1)

    def _actual_idx(target: date, fallback: int) -> int:
        """Index of *target* in the IBP month list, or *fallback*."""
        return actual_months.index(target) if target in actual_months else fallback

    actual_start_idx = _actual_idx(_DPC_DEFAULT_ACTUAL_START, 0)
    actual_end_idx = _actual_idx(_DPC_DEFAULT_ACTUAL_END, last_actual_idx)
    fc_start_idx = _month_idx(
        _DPC_DEFAULT_FORECAST_START, min(prior_fallback_idx + 1, last_idx))
    fc_end_idx = _month_idx(_DPC_DEFAULT_FORECAST_END, last_idx)
    prior_default_idx = _month_idx(_DPC_DEFAULT_PRIOR_MONTH, prior_fallback_idx)

    fmt_cycle = lambda c: c  # noqa: E731 — trivial identity for clarity
    # Spell the month out ("Apr 2026") so the selected value is easy to
    # read in the dropdown (the bare "4/2026" form was hard to parse).
    fmt_month = lambda d: d.strftime("%b %Y")  # noqa: E731

    row1 = st.columns(2)
    with row1[0]:
        current_cycle = st.selectbox(
            current_label, options=cycles,
            index=cycles.index(default_current),
            key=_ns_key("dpc_current_cycle", ns), format_func=fmt_cycle,
            help="The cycle whose plan you're evaluating.",
        )
    with row1[1]:
        prior_cycle = st.selectbox(
            prior_label, options=prior_cycles,
            index=prior_cycles.index(default_prior),
            key=_ns_key("dpc_prior_cycle", ns), format_func=fmt_cycle,
            help="The earlier cycle to compare against (drives Base Plan).",
        )

    st.markdown("**Actual month range** (IBP Shipments + current-cycle actuals)")
    row2 = st.columns(2)
    with row2[0]:
        actual_start = st.selectbox(
            "Actual — beginning month", options=actual_months, index=actual_start_idx,
            key=_ns_key("dpc_actual_start", ns), format_func=fmt_month,
        )
    with row2[1]:
        actual_end = st.selectbox(
            "Actual — end month", options=actual_months, index=actual_end_idx,
            key=_ns_key("dpc_actual_end", ns), format_func=fmt_month,
        )

    st.markdown("**Forecast month range** (must not overlap the actual range)")
    row3 = st.columns(2)
    with row3[0]:
        forecast_start = st.selectbox(
            "Forecast — beginning month", options=months, index=fc_start_idx,
            key=_ns_key("dpc_forecast_start", ns), format_func=fmt_month,
        )
    with row3[1]:
        forecast_end = st.selectbox(
            "Forecast — end month", options=months, index=fc_end_idx,
            key=_ns_key("dpc_forecast_end", ns), format_func=fmt_month,
        )

    prior_month = st.selectbox(
        "Prior Month (for PM Actual / Prior Month Forecast)",
        options=months, index=prior_default_idx,
        key=_ns_key("dpc_prior_month", ns), format_func=fmt_month,
        help="The single month used for the Prior-Month columns.",
    )

    # ── Search-to-HIDE Portfolio Major · Supply Format · Brand filter ───
    # ONE multiselect that starts EMPTY (everything shown); type to search a
    # combination and pick it to REMOVE it — e.g. type "butter private" and
    # drop those rows while keeping Butter · … · Branded.  Much easier than
    # deselecting from ~60 chips.  Empty = no filter.
    combo_exclude: frozenset = frozenset()
    combos = list(combo_options or [])
    # HTST is the Fresh Milk portfolio major — show the friendly name.
    labels_to_combo = {
        f"{_DPC_PMAJ_DISPLAY.get(p, p)} · {s} · {b}": (p, s, b)
        for p, s, b in combos
    }
    all_labels = sorted(labels_to_combo)
    if all_labels:
        st.markdown(
            "**Hide combinations — Portfolio Major · Supply Format · Brand** "
            "_(empty = show all; search a name and pick it to remove — e.g. "
            "type **butter private** to drop those rows)_"
        )
        hidden = st.multiselect(
            "Hide combinations", options=all_labels,
            key=_ns_key("dpc_combo_exclude", ns), label_visibility="collapsed",
            placeholder="Search to hide, e.g. “butter private”…",
            help="Type to search; each pick is REMOVED from every table in "
                 "this section.  Powders / Cheese / Bulk Fluid aren't listed "
                 "(non-B2C); Butter includes catalog combinations (Private / "
                 "Chips / Elgin Solid) even with no plan.",
        )
        combo_exclude = frozenset(
            labels_to_combo[h] for h in hidden if h in labels_to_combo)
    else:
        st.caption(
            "ℹ️ **The Portfolio Major · Supply Format · Brand filter will "
            "appear here** once the tracker carries the categorisation columns."
        )

    # Plain-language echo of the current selection — makes the active
    # window obvious at a glance regardless of dropdown contrast.
    #
    # Date ranges are shown per source: **Shipments** (the comparison's
    # actuals) span the actual window, while **Orders** are pulled for the
    # prior month only (they feed the Prior-Month Actual-vs-Forecast table's
    # Ordered column — see the fragment's ibp_orders load).  The Prior-Month
    # clause spells out both legs of PM Actual: the actual is prior-month
    # SHIPMENTS (not orders — see _compute_leaf_measures.prior_month_actual,
    # summed off the IBP Shipments frame), and the forecast is the PRIOR
    # cycle's plan for that month (summed under ``prior_cycle``).  So
    # PM Actual = prior-month shipments − prior-cycle forecast.
    st.caption(
        f"📌 Comparing **{current_cycle}** (current) vs **{prior_cycle}** (prior)  ·  "
        f"Shipments (actuals) **{fmt_month(actual_start)} – {fmt_month(actual_end)}**  ·  "
        f"Orders **{fmt_month(prior_month)}** (prior month only)  ·  "
        f"Forecast **{fmt_month(forecast_start)} – {fmt_month(forecast_end)}**  ·  "
        f"Prior month **{fmt_month(prior_month)}** "
        f"(its **actual = shipments**, not orders; its forecast = the "
        f"**{prior_cycle}** prior-cycle plan)"
    )

    return ComparisonFilters(
        current_cycle=current_cycle,
        prior_cycle=prior_cycle,
        actual_start=actual_start,
        actual_end=actual_end,
        forecast_start=forecast_start,
        forecast_end=forecast_end,
        prior_month=prior_month,
        combo_exclude=combo_exclude,
    )


def _load_demand_comparison_pdh() -> Optional[pd.DataFrame]:
    """Return the PDH frame for dimension/brand enrichment, or ``None``.

    Non-fatal: a PDH failure leaves dimensions blank (the builder then
    surfaces its own "PDH empty" warning) rather than breaking the
    section.
    """
    try:
        return fetch_pdh().df
    except DemandSummaryError as exc:
        logger.info("qry_pdh.csv unavailable for Demand Plan Comparison: %s", exc)
        return None


def _load_mom_item_master() -> Optional[pd.DataFrame]:
    """Return ``RO_Item_Master.csv`` for the MOM dim fallback, or ``None``.

    Non-fatal: if RO_Item_Master can't be read the Demand MOM Summary just
    degrades to PDH-only dimensions (more items land in the not-captured
    log), rather than breaking the section.
    """
    try:
        return fetch_ro_item_master_df()
    except RoComparisonError as exc:
        logger.info(
            "RO_Item_Master.csv unavailable for Demand MOM dim fallback: %s", exc,
        )
        return None


def _load_demand_comparison_ibp(
    months: Optional[tuple[date, ...]] = None,
) -> tuple[Optional[pd.DataFrame], Optional[str]]:
    """Return ``(slim IBP Shipments frame, warning)``.

    Calls the *slim* fetcher with a month predicate pushed into DuckDB
    so OneLake only returns the few months the comparison actually
    consumes (was: full table scan, every render).  Non-fatal: an IBP
    failure yields an empty-actuals build plus a user-facing warning.
    """
    try:
        df = fetch_ibp_shipments_slim_df(months=months)
        return df, None
    except IBPOfficialSourceError as exc:
        logger.info("IBP Shipments unavailable for Demand Plan Comparison: %s", exc)
        return None, (
            "IBP Shipments could not be read, so the Actuals columns are "
            f"zero.  ({exc})"
        )


def _load_demand_comparison_ibp_orders(
    months: Optional[tuple[date, ...]] = None,
) -> tuple[Optional[pd.DataFrame], Optional[str]]:
    """Return ``(slim IBP Orders frame, warning)`` for prior-month summary."""
    try:
        df = fetch_ibp_orders_slim_df(months=months)
        return df, None
    except IBPOfficialSourceError as exc:
        logger.info("IBP Orders unavailable for Prior Month summary: %s", exc)
        return None, (
            "IBP Orders could not be read, so the Ordered column is zero.  "
            f"({exc})"
        )


def _load_demand_comparison_dim() -> tuple[Optional[pd.DataFrame], Optional[str]]:
    """Return ``(dp_dimshiptosites frame, warning)`` for the driver tables.

    Non-fatal: a dim failure leaves the driver-table customer names blank
    (Party Site Number + Item Description still resolve) plus a warning.
    """
    try:
        return fetch_dimshiptosites_df(), None
    except ShipToSitesSourceError as exc:
        logger.info("dp_dimshiptosites unavailable for driver tables: %s", exc)
        return None, (
            "Ship-to-site dimension (dp_dimshiptosites) could not be read, so "
            f"driver customer names may be blank.  ({exc})"
        )


# Row-highlight styles for the Product Line Review Styler tables (the Demand
# Plan Comparison tables render as HTML \u2014 see _DPC_TREE_CSS / _DPC_LITE_CSS \u2014
# and no longer use these pandas-Styler strings).
_ROW_STYLE_HIGHLIGHT_ORANGE_BOLD = "background-color: #ffcc80; font-weight: 700"
_ROW_STYLE_PLR_CUSTOMER = "background-color: #e3f2fd"
_PLR_HIGHLIGHT_LABELS: frozenset[str] = frozenset({"Branded", "Private", "Grand Total"})


# Executive KPI strip shown above the Demand Plan Comparison table.  Four
# tiles read left→right as a narrative: current reality → recent trend → the
# plan's full-year assumption → the R&O aspiration baked into the plan.
_DPC_KPI_CSS = """
<style>
.dpc-kpis {display:flex; gap:14px; flex-wrap:wrap; margin:.15rem 0 1rem;}
.dpc-kpis + .dpc-kpis {margin-top:-.35rem;}  /* tighten walk → YoY gap */
.dpc-kpi {flex:1 1 180px; min-width:165px; background:#ffffff;
  border:1px solid #e4e0d8; border-top:3px solid #1f4e79; border-radius:10px;
  padding:12px 16px 11px; box-shadow:0 1px 3px rgba(40,50,70,.07);}
/* Walk-row tiles wear a slightly heavier accent so the plan-vs-plan story
   reads first; totals use the deep accent, variances share it. */
.dpc-kpi--walk {border-top-color:#0f3d63;}
.dpc-kpi .k-label {font-size:.72rem; font-weight:700; letter-spacing:.04em;
  text-transform:uppercase; color:#5a6472;}
.dpc-kpi .k-value {font-size:1.85rem; font-weight:800; line-height:1.15;
  margin:.12rem 0 .12rem; color:#1f4e79;}
.dpc-kpi .k-value.up {color:#1b7f3a;}
.dpc-kpi .k-value.down {color:#c0392b;}
.dpc-kpi .k-value.flat {color:#5a6472;}
.dpc-kpi .k-desc {font-size:.7rem; font-style:italic; color:#8a8f98;}
/* Sub-label sits under the value on the walk tiles — cycle labels + a
   plain-English descriptor.  Small enough not to compete with the value. */
.dpc-kpi .k-sub {display:block; font-size:.68rem; font-style:italic;
  color:#8a8f98; margin-top:.1rem; line-height:1.25;}
</style>
"""


def _fmt_yoy(value: Optional[float]) -> tuple[str, str]:
    """Signed whole-ish percent for a YoY fraction → (text, css-class)."""
    if value is None or pd.isna(value):
        return "—", "flat"
    cls = "up" if value > 0 else "down" if value < 0 else "flat"
    return f"{value * 100:+.1f}%", cls


def _fmt_share(value: Optional[float]) -> tuple[str, str]:
    """Unsigned percent for a share fraction → (text, neutral class)."""
    if value is None or pd.isna(value):
        return "—", "flat"
    return f"{value * 100:.1f}%", ""


def _fmt_millions(value: Optional[float], *, signed: bool) -> tuple[str, str]:
    """Format a millions-of-lbs value → (text, css-class).

    ``signed=True`` renders a leading ``+``/``−`` and colours the tile
    up/down/flat like the YoY formatter (used for the three ``Var.`` tiles).
    ``signed=False`` is unsigned + neutral colour (used for the two total
    tiles that anchor the walk).  Missing / NaN → ``"—"``.
    """
    if value is None or pd.isna(value):
        return "—", "flat"
    if signed:
        cls = "up" if value > 0 else "down" if value < 0 else "flat"
        return f"{value:+.2f}", cls
    return f"{value:.2f}", ""


def _fmt_pct_walk(frac: Optional[float]) -> tuple[str, str]:
    """Format a signed fraction → ("+0.9%", css-class) for a walk KPI tile.

    Mirrors :func:`_fmt_millions` (up/down/flat colouring) so the Total Delta %
    tile reads consistently with the signed ``Var.`` tiles beside it.
    """
    if frac is None or pd.isna(frac):
        return "—", "flat"
    pct = frac * 100.0
    cls = "up" if pct > 0 else "down" if pct < 0 else "flat"
    return f"{pct:+.1f}%", cls


def _fmt_pct_share(frac: Optional[float]) -> tuple[str, str]:
    """Format an unsigned share fraction → ("45.2%", "") for a KPI tile.

    Neutral (no +/− sign, no up/down colour) — used for the APS "Actl% of
    Current Plan" tile, which is a share of the plan, not a signed variance.
    """
    if frac is None or pd.isna(frac):
        return "—", "flat"
    return f"{frac * 100.0:.1f}%", ""


def _render_comparison_kpis_yoy(
    kpis: ComparisonKpis, *,
    order_yoy_total: Optional[dict] = None,
    periods: Optional[dict[str, str]] = None,
) -> None:
    """Render the YoY / share KPI row (top of the section).

    L3M Order YoY · L6M Order YoY · Full-Year Base vs PY% · R&O % of Current
    Plan · **Total B2C Plan vs Budget %**.  ``order_yoy_total`` supplies the
    Total-B2C trailing IBP-Order YoY (same framing as Business Health);
    ``periods`` adds the covered month range under the relevant tiles.
    """
    order_yoy_total = order_yoy_total or {}
    per = periods or {}
    l3m = _fmt_yoy(order_yoy_total.get("L3M"))
    l6m = _fmt_yoy(order_yoy_total.get("L6M"))
    fy = _fmt_yoy(kpis.full_year_base_vs_py)
    ro = _fmt_share(kpis.ro_pct)
    budget = _fmt_yoy(kpis.budget_pct)

    def _desc(base: str, header: str) -> str:
        rng = per.get(header)
        return f"{base} · {rng}" if rng else base

    yoy = (
        ("L3M Order YoY", l3m, _desc("Current reality", "L3M Order YoY")),
        ("L6M Order YoY", l6m, _desc("Recent trend", "L6M Order YoY")),
        ("Full-Year Base vs PY%", fy, _desc("Plan assumption", "PY")),
        ("R&O % of Current Plan", ro, "Aspiration"),
        ("Total B2C Plan vs Budget %", budget, "Plan vs budget"),
    )
    yoy_cards = "".join(
        f'<div class="dpc-kpi"><div class="k-label">{_esc_html(label)}</div>'
        f'<div class="k-value {cls}">{_esc_html(text)}</div>'
        f'<div class="k-desc">{_esc_html(desc)}</div></div>'
        for label, (text, cls), desc in yoy
    )
    st.markdown(
        f'{_DPC_KPI_CSS}<div class="dpc-kpis">{yoy_cards}</div>',
        unsafe_allow_html=True,
    )


def _render_comparison_kpis_walk(
    kpis: ComparisonKpis, filters: ComparisonFilters,
) -> None:
    """Render the cycle-over-cycle walk KPI row (the "Prior Plan" metrics).

    Last Plan (prior cycle) → PM Actual Var. → Base Plan Var. → R&O Var. →
    Current Plan (current cycle).  Cycle labels are dynamic (pulled from
    ``filters``); values are read straight off the Total B2C row of the
    assembled table so tile ↔ table numbers reconcile by construction.
    """
    prior_cy = filters.prior_cycle
    current_cy = filters.current_cycle
    # Total Delta % = (Current − Last) ÷ Last Plan — the cycle-over-cycle % move
    # of the whole plan.  Computed from the same Total B2C plan totals the tiles
    # show, so it reconciles with the table's Total Delta % by construction.
    total_delta = kpis.current_plan_total - kpis.last_plan_total
    total_delta_pct = (
        total_delta / kpis.last_plan_total if kpis.last_plan_total else None)
    walk = (
        (
            "Last Plan",
            _fmt_millions(kpis.last_plan_total, signed=False),
            f"{prior_cy} total forecast incl. R&O",
        ),
        (
            "PM Actual Var.",
            _fmt_millions(kpis.pm_actual_var, signed=True),
            f"prior month actual vs {prior_cy} forecast",
        ),
        (
            "Base Plan Var.",
            _fmt_millions(kpis.base_plan_var, signed=True),
            f"{current_cy} vs {prior_cy} baseline forecast",
        ),
        (
            "R&O Var.",
            _fmt_millions(kpis.ro_var, signed=True),
            f"{current_cy} vs {prior_cy} R&O forecast",
        ),
        (
            "Current Plan",
            _fmt_millions(kpis.current_plan_total, signed=False),
            f"{current_cy} total forecast incl. R&O",
        ),
        (
            "Total Delta %",
            _fmt_pct_walk(total_delta_pct),
            f"{current_cy} vs {prior_cy}: total plan % change",
        ),
    )
    walk_cards = "".join(
        f'<div class="dpc-kpi dpc-kpi--walk">'
        f'<div class="k-label">{_esc_html(label)}</div>'
        f'<div class="k-value {cls}">{_esc_html(text)}</div>'
        f'<span class="k-sub">{_esc_html(sub)}</span></div>'
        for label, (text, cls), sub in walk
    )
    st.markdown(
        f'{_DPC_KPI_CSS}<div class="dpc-kpis">{walk_cards}</div>',
        unsafe_allow_html=True,
    )


# ── Demand Plan Comparison — screenshot-styled HTML table ────────────────────
# Mirrors the sibling RO Summary / Prior-Month tables (navy header + white
# font, light-blue Total B2C, orange #f8cbad Portfolio-Major section rows) so
# the comparison reads as one system with the rest of this UI.  st.dataframe
# can't style the header band, hence the hand-built table.
_DPC_TREE_CSS: str = """
<style>
.dpc-tree {overflow-x:auto; margin:0.35rem 0 0.75rem;}
.dpc-tree-in {min-width:920px; background:#ffffff; color:#1a1a1a; font-size:0.95rem;}
.dpc-tree details {margin:0;}
.dpc-tree .rw {display:flex; align-items:center; border-bottom:1px solid #e8e8e8;}
.dpc-tree .rw > span {flex:1 1 70px; padding:4px 10px; white-space:nowrap;
  text-align:right; overflow:hidden; text-overflow:ellipsis;}
.dpc-tree .rw > span.lbl {flex:0 0 230px; text-align:left;
  display:flex; align-items:center; gap:2px;}
.dpc-tree .hdr {background:#1f3864; color:#ffffff; font-weight:700;
  position:sticky; top:0; z-index:1;}
.dpc-tree .hdr > span {text-align:center;}
.dpc-tree .hdr > span.lbl {text-align:left;}
/* Period sub-label (2nd header line): same dark-blue bg (inherited) + white
   bold font, a touch smaller so it reads as the window the variance covers. */
.dpc-tree .hdr .per {display:block; font-weight:700; font-size:0.82em;}
/* Native disclosure: the summary IS the row; hide the default marker and use
   our own triangle that rotates when the node is open. */
.dpc-tree summary.rw {cursor:pointer; list-style:none;}
.dpc-tree summary.rw::-webkit-details-marker {display:none;}
.dpc-tree .rw.total {background:#dce6f1; font-weight:700;}
.dpc-tree .rw.section {background:#f8cbad; font-weight:700;}
.dpc-tree .rw.subtotal {font-weight:700;}
.dpc-tree .rw.memo {font-style:italic; color:#555555;}
.dpc-tree .tri {flex:0 0 auto; display:inline-block; width:0.85em;
  color:#5a6472; transition:transform .12s ease;}
.dpc-tree details[open] > summary.rw .tri {transform:rotate(90deg);}
</style>
"""


# ── Lightweight table + mix-visual styling (executive summary section) ───────
# A clean, low-chrome look (white background, hairline separators, bold section
# rows, no fills) for the two top summary tables + the volume-mix bar — kept
# separate from the detailed table's heavier navy/orange treatment.
_DPC_LITE_CSS: str = """
<style>
.dpc-lite {overflow-x:auto; margin:0.3rem 0 0.8rem;}
.dpc-lite table {border-collapse:collapse; width:100%; background:transparent;
  color:#1f2430; font-size:0.98rem;}
.dpc-lite th, .dpc-lite td {padding:7px 14px; white-space:nowrap; text-align:right;}
.dpc-lite thead th {color:#6b7280; font-weight:600; font-size:0.9rem;
  border-bottom:2px solid #e5e7eb;}
/* 2nd header line: the month range a column covers. */
.dpc-lite thead th .per {display:block; font-weight:400; font-size:0.8em;
  color:#9ca3af; white-space:nowrap;}
.dpc-lite th.lbl, .dpc-lite td.lbl {text-align:left;}
.dpc-lite tbody td {border-bottom:1px solid #f1f2f4;}
.dpc-lite tr.section td {font-weight:700;}
.dpc-lite tr.memo td {font-style:italic; color:#6b7280;}
/* Total row reads as an emphasised band whether it sits on top or bottom. */
.dpc-lite tr.total td {font-weight:700; border-top:2px solid #d1d5db;
  border-bottom:2px solid #d1d5db;}
.dpc-lite td.pos {color:#1b7f3a;}   /* positive variance — green */
.dpc-lite td.neg {color:#c0392b;}   /* negative variance — red   */

/* Current-plan volume-mix stacked bar + legend. */
.dpc-mixbar {display:flex; width:100%; height:26px; border-radius:5px;
  overflow:hidden; margin:0.15rem 0 0.5rem;}
.dpc-mixbar .seg {display:flex; align-items:center; justify-content:center;
  color:#ffffff; font-size:0.72rem; font-weight:700; min-width:0;}
.dpc-mixlegend {display:flex; flex-wrap:wrap; gap:14px; margin:0 0 0.6rem;
  font-size:0.76rem; color:#4b5563;}
.dpc-mixlegend span {display:inline-flex; align-items:center; gap:6px;}
.dpc-mixlegend i {width:11px; height:11px; border-radius:2px; display:inline-block;}
</style>
"""


def _dpc_cmp_row_class(
    *, row_id: object, is_subtotal: bool, is_memo: bool, indent: int,
) -> str:
    """CSS class for one comparison row: total / section / subtotal / memo / leaf.

    Every indent-1 row is a Portfolio Major (ESL / Aseptic / Cultured / Fresh
    Milk / **Butter**) and gets the orange ``section`` fill — including Butter,
    which is a leaf in the static template but still a top-level major.
    """
    if str(row_id) == "total_b2c":
        return "total"
    if is_memo:
        return "memo"
    if indent == 1:
        return "section"
    return "subtotal" if is_subtotal else ""


def _dpc_fmt_cell(value: object, *, is_percent: bool) -> str:
    """Format one metric cell: '1,129.00' / '6.0%' / '—' for blank/NaN.

    Percent values arrive pre-scaled to whole percents; millions keep two
    decimals with thousands separators (matches the prior NumberColumn view).
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if pd.isna(num):
        return "—"
    return f"{num:.1f}%" if is_percent else f"{num:,.2f}"


def _foldable_tree_body(
    n: int, indent_flags: list[int],
    make_row: "Callable[[int, bool], tuple[str, str]]",
) -> list[str]:
    """Return the ``<details>`` / ``<div>`` row parts for a foldable tree.

    Parent/child nesting is inferred from ``indent_flags``: a row is a foldable
    ``<details><summary>`` when the next row is deeper, else a leaf ``<div>``.
    ``make_row(i, foldable)`` returns ``(css_class, inner_cells_html)``.  Shared
    by the comparison / Prior-Month / bias trees so the fold mechanics live in
    one place.
    """
    def _ind(i: int) -> int:
        return int(indent_flags[i]) if i < len(indent_flags) else 0

    parts: list[str] = []
    open_stack: list[int] = []      # indents of currently-open <details>
    for i in range(n):
        ind = _ind(i)
        while open_stack and open_stack[-1] >= ind:   # close non-ancestors
            parts.append("</details>")
            open_stack.pop()
        foldable = (i + 1 < n) and _ind(i + 1) > ind
        cls, cells = make_row(i, foldable)
        if foldable:
            parts.append(
                f'<details open class="node"><summary class="rw {cls}">{cells}</summary>')
            open_stack.append(ind)
        else:
            parts.append(f'<div class="rw {cls}">{cells}</div>')
    parts.extend("</details>" for _ in open_stack)
    return parts


def _tri_span(foldable: bool) -> str:
    """Rotating disclosure triangle for foldable rows (spacer when a leaf)."""
    return '<span class="tri">▸</span>' if foldable else '<span class="tri"></span>'


def _render_comparison_tree(
    display_df: pd.DataFrame,
    *,
    label_col: str,
    metric_cols: list[str],
    percent_labels: list[str],
    row_ids: list[str] | None,
    subtotal_flags: list[bool],
    memo_flags: list[bool],
    indent_flags: list[int],
    header_labels: dict[str, str] | None = None,
    period_labels: dict[str, str] | None = None,
) -> None:
    """Render the comparison rows as a natively-foldable ``<details>`` tree.

    All nodes start expanded; clicking a parent row collapses its subtree with
    no server round-trip.  The NBSP hierarchy indent in each label is preserved
    and a rotating triangle marks foldable rows.  *header_labels* optionally
    overrides the DISPLAYED text of a column header (keyed by the metric-col /
    df-column name) without changing the underlying data key — used to anchor
    the plan columns on the chosen cycles (e.g. "C3 Plan (incl. R&O)").
    *period_labels* optionally adds a 2nd header line (same dark-blue bg / white
    bold font) under a column — used to stamp each variance with its window.
    """
    percent_set = set(percent_labels)
    hdr = header_labels or {}
    periods = period_labels or {}

    def _make_row(i: int, foldable: bool) -> tuple[str, str]:
        cls = _dpc_cmp_row_class(
            row_id=row_ids[i] if row_ids is not None and i < len(row_ids) else "",
            is_subtotal=bool(subtotal_flags[i]) if i < len(subtotal_flags) else False,
            is_memo=bool(memo_flags[i]) if i < len(memo_flags) else False,
            indent=int(indent_flags[i]) if i < len(indent_flags) else 0,
        )
        row = display_df.iloc[i]
        cells = f'<span class="lbl">{_tri_span(foldable)}{_esc_html(row[label_col])}</span>' + "".join(
            f'<span>{_esc_html(_dpc_fmt_cell(row[c], is_percent=c in percent_set))}</span>'
            for c in metric_cols
        )
        return cls, cells

    def _hdr_cell(c: str) -> str:
        main = _esc_html(hdr.get(c, c))
        period = periods.get(c)
        # 2nd line inherits the header's dark-blue bg + white bold font; the
        # <span class="per"> just relaxes the size a touch so it reads as a
        # sub-label rather than a second header.
        if period:
            return f'<span>{main}<br><span class="per">{_esc_html(period)}</span></span>'
        return f"<span>{main}</span>"

    header = (
        '<div class="rw hdr"><span class="lbl">' + _esc_html(label_col) + "</span>"
        + "".join(_hdr_cell(c) for c in metric_cols)
        + "</div>"
    )
    parts = [header] + _foldable_tree_body(len(display_df), indent_flags, _make_row)

    st.markdown(
        _DPC_TREE_CSS
        + '<div class="dpc-tree"><div class="dpc-tree-in">'
        + "".join(parts)
        + "</div></div>",
        unsafe_allow_html=True,
    )


# Curated executive current-plan summary: each entry is (row_id, css-class).
# Total B2C anchors the TOP; only ESL is split into its carton subtotals and
# only Cultured shows Cottage Cheese / Sour Cream.  Those two render with the
# same weight as the carton sub-rows (plain "subtotal", not italic "memo").
_DPC_SUMMARY_ROWS: tuple[tuple[str, str], ...] = (
    ("total_b2c", "total"),
    ("esl", "section"),
    ("esl_lc", "subtotal"),
    ("esl_sc", "subtotal"),
    ("aseptic", "section"),
    ("cultured", "section"),
    ("cult_cottage_cheese", "subtotal"),
    ("cult_sour_cream", "subtotal"),
    ("fresh_milk", "section"),
    ("butter", "section"),
)

# Summary-table columns, in display order.  Each entry is (key, header, kind)
# where ``kind`` ∈ {"m" millions, "pct_signed" green/red %, "pct_plain" %}.
# RO% sits between R&O vol and Total plan; T3M/T6M YoY follow Base vs PY %.
# Every column here is individually hidable via the popover; Category is fixed.
_DPC_SUMMARY_COLS: tuple[tuple[str, str, str], ...] = (
    ("base_plan",      "Base plan",      "m"),
    ("py",             "PY",             "m"),
    ("base_vs_py",     "Base vs PY %",   "pct_signed"),
    # L3M/L6M Order YoY (trailing IBP Orders, anchored on the Prior Month) —
    # same framing as Business Health; values injected from the order lookup.
    ("l3m_order_yoy",  "L3M Order YoY",  "pct_signed"),
    ("l6m_order_yoy",  "L6M Order YoY",  "pct_signed"),
    ("ro_vol",         "R&O vol",        "m"),
    ("ro_pct",         "RO%",            "pct_plain"),
    ("total_plan",     "Total plan",     "m"),
    ("budget",         "Budget",         "m"),
    ("vs_budget",      "% vs Budget",    "pct_signed"),
)
_DPC_SUMMARY_COLS_KEY: str = "demand_plan_comparison_summary_cols"


def _dpc_num(row: pd.Series, col_id: str) -> Optional[float]:
    """Numeric cell from a comparison-table row by column id (None if missing)."""
    val = row.get(DPC_DISPLAY_LABELS.get(col_id, col_id))
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(num) else num


def _dpc_fmt_m(value: Optional[float]) -> str:
    """Millions, one decimal + thousands separators (screenshot style)."""
    return "—" if value is None else f"{value:,.1f}"


def _dpc_fmt_pct(
    frac: Optional[float], *, signed: bool, decimals: int = 1,
) -> tuple[str, str]:
    """Fraction → (percent text, css-class) — green/red when *signed*.

    Rounds to *decimals* places; a value that rounds to zero renders as a
    neutral ``"0%"`` (never ``"-0%"``) with no colour class.
    """
    if frac is None:
        return "—", ""
    pct = frac * 100.0
    if abs(round(pct, decimals)) == 0.0:           # avoid "-0%" / spurious colour
        return f"{0:.{decimals}f}%", ""
    cls = ("pos" if pct > 0 else "neg") if signed else ""
    txt = f"{pct:+.{decimals}f}%" if signed else f"{pct:.{decimals}f}%"
    return txt, cls


def _dpc_fmt_pp(delta_frac: Optional[float], *, decimals: int = 1) -> tuple[str, str]:
    """Fraction difference → (``"+1.8pp"`` text, css-class), green/red signed."""
    if delta_frac is None:
        return "—", ""
    pp = delta_frac * 100.0
    if abs(round(pp, decimals)) == 0.0:
        return f"{0:.{decimals}f}pp", ""
    cls = "pos" if pp > 0 else "neg"
    return f"{pp:+.{decimals}f}pp", cls


def _summary_row_values(r: pd.Series) -> dict[str, Optional[float]]:
    """Project one comparison-table row into the summary table's value keys.

    All keys map to :data:`_DPC_SUMMARY_COLS`.  The only derived values are
    ``base_plan`` (Current Plan − R&O) and ``base_vs_py`` ((Base − PY) / PY);
    T3M/T6M YoY are read straight off the pre-computed table columns.
    """
    current_plan = _dpc_num(r, DPC_COL_CURRENT_PLAN)
    ro_vol = _dpc_num(r, DPC_COL_CURRENT_PLAN_RO)
    py = _dpc_num(r, DPC_COL_PY_ACTUAL)
    base_plan = None if current_plan is None else current_plan - (ro_vol or 0.0)
    base_vs_py = (
        (base_plan - py) / py
        if base_plan is not None and py not in (None, 0.0) else None
    )
    return {
        "base_plan": base_plan,
        "py": py,
        "base_vs_py": base_vs_py,
        # L3M/L6M Order YoY are merged in by the renderer from the order lookup
        # (they are NOT trailing shipments any more).
        "l3m_order_yoy": None,
        "l6m_order_yoy": None,
        "ro_vol": ro_vol,
        "ro_pct": _dpc_num(r, DPC_COL_O_PCT),
        "total_plan": current_plan,
        "budget": _dpc_num(r, DPC_COL_BUDGET),
        "vs_budget": _dpc_num(r, DPC_COL_PCT),
    }


def _summary_cell_html(kind: str, value: Optional[float]) -> str:
    """Format one summary cell (whole-percent, green/red for signed) as ``<td>``."""
    if kind == "m":
        return f"<td>{_esc_html(_dpc_fmt_m(value))}</td>"
    signed = kind == "pct_signed"
    txt, cls = _dpc_fmt_pct(value, signed=signed, decimals=0)
    return f'<td class="{cls}">{_esc_html(txt)}</td>' if cls else f"<td>{_esc_html(txt)}</td>"


def _summary_clean_label(r: pd.Series) -> str:
    """Row label with the memo bullet dropped so Cottage Cheese / Sour Cream
    render exactly like the Large/Small Carton sub-rows (indent preserved)."""
    return str(r.get(DPC_COL_LABEL, "")).replace("• ", "")


# ── Shared column picker: hide + reorder, one widget, used by every table ────
# A single ``st.multiselect`` drives BOTH visibility and order: unticking hides
# a column, and Streamlit preserves selection order, so the sequence a planner
# ticks columns in IS their left-to-right order.  Keeping one helper means the
# ⚙️ Columns popover behaves identically on every table (no per-table variants).
def _render_column_picker(
    key: str, options: list[str], *,
    help_suffix: str = "", label_overrides: Optional[dict[str, str]] = None,
) -> None:
    """Render the ⚙️ Columns popover for one table (binds to ``session_state[key]``).

    Options default to *all shown, canonical order* on first render.  The label
    column is fixed by the caller and never appears here.  *label_overrides*
    maps an option to the text shown for it (the stored value stays the option),
    so the picker can mirror cycle-anchored table headers.
    """
    ov = label_overrides or {}
    st.session_state.setdefault(key, list(options))
    with st.popover("⚙️ Columns", use_container_width=False):
        st.multiselect(
            "Show columns", options=options, key=key,
            format_func=lambda o: ov.get(o, o),
            help="Untick to hide a column; re-tick columns in the order you want "
                 "them to appear (selection order = left-to-right order)."
                 + help_suffix,
        )


def _picked_columns(key: str, options: list[str]) -> list[str]:
    """Visible columns in the planner's chosen order (all *options* if unset).

    Filters stale picks no longer in *options* so a changed schema can't crash
    the render; falls back to the full canonical list when nothing is selected.
    """
    picked = st.session_state.get(key) or options
    valid = set(options)
    ordered = [c for c in picked if c in valid]
    return ordered or list(options)


def _ns_key(base: str, ns: str) -> str:
    """Namespace a widget/session key so a second section can't collide.

    ``ns=""`` returns *base* unchanged (the IBP section keeps its exact keys);
    a non-empty ns (e.g. ``"aps"``) prefixes it.
    """
    return base if not ns else f"{ns}_{base}"


def _render_comparison_summary_col_picker(ns: str = "") -> None:
    """⚙️ Columns popover for the summary table — rendered ABOVE the metrics row.

    Kept separate from :func:`_render_comparison_summary_table` so it can sit at
    the very top of the section; the table reads its selection from
    session_state (Category is always shown, every metric column is hidable).
    *ns* namespaces the widget key so an APS mirror can coexist with the IBP one.
    """
    all_headers = [header for _key, header, _kind in _DPC_SUMMARY_COLS]
    _render_column_picker(
        _ns_key(_DPC_SUMMARY_COLS_KEY, ns), all_headers,
        help_suffix="  The Category column always shows.",
    )


def _render_comparison_summary_table(
    result, ns: str = "", *,
    order_yoy_by_row: Optional[dict] = None,
    periods: Optional[dict[str, str]] = None,
) -> None:
    """Render the executive current-plan summary table (screenshot 1).

    A condensed, current-plan-anchored projection of the SAME (filtered)
    comparison data (Base plan = Current Plan − R&O; Base vs PY %; R&O; Budget),
    so the Portfolio Major · Supply Format · Brand filter flows through here too.
    ``order_yoy_by_row`` injects per-row **L3M / L6M Order YoY** (trailing IBP
    Orders); ``periods`` adds a 2nd header line (month range) under Base Plan /
    PY / R&O / L3M / L6M.  Visible columns come from the col picker popover.
    """
    table = getattr(result, "table", None)
    if table is None or table.empty or DPC_COL_ROW_ID not in table.columns:
        return
    by_id = {str(r[DPC_COL_ROW_ID]): r for _, r in table.iterrows()}
    order_yoy = order_yoy_by_row or {}
    per = periods or {}

    # Visible columns (Category always shown) in the planner's chosen ORDER —
    # selection + ordering live in the picker rendered above the metrics.
    all_headers = [header for _key, header, _kind in _DPC_SUMMARY_COLS]
    col_by_header = {spec[1]: spec for spec in _DPC_SUMMARY_COLS}
    cols = [col_by_header[h]
            for h in _picked_columns(_ns_key(_DPC_SUMMARY_COLS_KEY, ns), all_headers)]

    def _th(header: str) -> str:
        rng = per.get(header)
        sub = f'<br><span class="per">{_esc_html(rng)}</span>' if rng else ""
        return f"<th>{_esc_html(header)}{sub}</th>"

    head_html = '<th class="lbl">Category</th>' + "".join(
        _th(header) for _key, header, _kind in cols)

    body_rows: list[str] = []
    for row_id, cls in _DPC_SUMMARY_ROWS:
        r = by_id.get(row_id)
        if r is None:
            continue
        vals = _summary_row_values(r)
        row_orders = order_yoy.get(row_id, {})
        vals["l3m_order_yoy"] = row_orders.get("L3M")
        vals["l6m_order_yoy"] = row_orders.get("L6M")
        cells = f'<td class="lbl">{_esc_html(_summary_clean_label(r))}</td>' + "".join(
            _summary_cell_html(kind, vals[key]) for key, _header, kind in cols
        )
        body_rows.append(f'<tr class="{cls}">{cells}</tr>')

    st.markdown(
        _DPC_LITE_CSS
        + '<div class="dpc-lite"><table><thead><tr>' + head_html
        + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table></div>",
        unsafe_allow_html=True,
    )


# ── Current-plan volume-mix / mix-shift view (screenshot 2) ──────────────────
# Portfolio-Major rows only (direct children of Total B2C), ordered by current
# plan volume so the biggest mover reads first.  Each major gets a stable
# colour for the stacked bar + legend.
_MIX_MAJOR_IDS: tuple[str, ...] = ("esl", "aseptic", "cultured", "fresh_milk", "butter")
_MIX_COLORS: dict[str, str] = {
    "fresh_milk": "#3b82f6",   # blue
    "esl":        "#10b981",   # green
    "cultured":   "#f59e0b",   # amber
    "aseptic":    "#8b5cf6",   # purple
    "butter":     "#ef4444",   # red
}
_MIX_FALLBACK_COLOR: str = "#9ca3af"


def _build_mix_rows(result) -> tuple[list[dict], Optional[dict]]:
    """Return ``(major_rows_sorted_desc, total_row)`` for the mix view.

    Each dict carries: ``row_id``, ``label``, ``plan`` (current plan vol),
    ``py``, ``mix`` / ``py_mix`` (fractions of the Total B2C plan / PY),
    ``shift`` (mix − py_mix), ``yoy`` ((plan − py) / py).  ``total_row`` is the
    Total B2C anchor (100 % mix).  Returns ``([], None)`` when unavailable.
    """
    table = getattr(result, "table", None)
    if table is None or table.empty or DPC_COL_ROW_ID not in table.columns:
        return [], None
    by_id = {str(r[DPC_COL_ROW_ID]): r for _, r in table.iterrows()}
    total = by_id.get("total_b2c")
    if total is None:
        return [], None
    total_plan = _dpc_num(total, DPC_COL_CURRENT_PLAN) or 0.0
    total_py = _dpc_num(total, DPC_COL_PY_ACTUAL) or 0.0
    if total_plan <= 0:
        return [], None

    def _mk(r, row_id) -> dict:
        plan = _dpc_num(r, DPC_COL_CURRENT_PLAN) or 0.0
        py = _dpc_num(r, DPC_COL_PY_ACTUAL) or 0.0
        mix = plan / total_plan if total_plan else None
        py_mix = py / total_py if total_py else None
        return {
            "row_id": row_id,
            "label": _dpc_clean_label(r.get(DPC_COL_LABEL, row_id)),
            "plan": plan, "py": py, "mix": mix, "py_mix": py_mix,
            "shift": (mix - py_mix) if (mix is not None and py_mix is not None) else None,
            "yoy": ((plan - py) / py) if py else None,
        }

    majors = [_mk(by_id[i], i) for i in _MIX_MAJOR_IDS if i in by_id]
    majors.sort(key=lambda d: d["plan"], reverse=True)
    total_row = {
        "label": _dpc_clean_label(total.get(DPC_COL_LABEL, "Total B2C")),
        "plan": total_plan, "mix": 1.0, "py_mix": 1.0 if total_py else None,
        "shift": None,
        "yoy": ((total_plan - total_py) / total_py) if total_py else None,
    }
    return majors, total_row


def _dpc_clean_label(label: object) -> str:
    """Strip the NBSP hierarchy indent so a top-level label reads flush-left."""
    return str(label).replace(" ", "").strip()


def _render_comparison_mix_table(result) -> None:
    """Render the current-plan volume-mix + mix-shift view (screenshot 2).

    Two lightweight pieces: a stacked **volume-mix bar** (+ legend), then a
    table of Plan vol · Mix % · PY mix % · Mix shift (pp) · YOY growth per
    Portfolio Major, anchored by Total B2C.  All values are re-projected from
    the SAME comparison data (current plan vs prior-year), so it reconciles
    with the summary table above.
    """
    majors, total_row = _build_mix_rows(result)
    if not majors or total_row is None:
        return

    # 1) Stacked mix bar — one segment per major (mix % width), label shown
    #    only when the segment is wide enough to fit it.
    segments, legend = [], []
    for m in majors:
        if not m["mix"]:
            continue
        color = _MIX_COLORS.get(m["row_id"], _MIX_FALLBACK_COLOR)
        pct = m["mix"] * 100.0
        label = f"{pct:.0f}%" if pct >= 6 else ""
        segments.append(
            f'<div class="seg" style="width:{pct:.4f}%;background:{color};" '
            f'title="{_esc_html(m["label"])} {pct:.1f}%">{label}</div>'
        )
        legend.append(
            f'<span><i style="background:{color}"></i>'
            f'{_esc_html(m["label"])} {pct:.0f}%</span>'
        )

    # 2) Mix table.
    headers = ["Category", "Plan vol", "Mix %", "PY mix %", "Mix shift", "YOY growth"]
    head_html = "".join(
        f'<th class="lbl">{_esc_html(h)}</th>' if i == 0
        else f'<th>{_esc_html(h)}</th>'
        for i, h in enumerate(headers)
    )

    def _mix_row(m: dict, *, cls: str) -> str:
        mix_txt, _ = _dpc_fmt_pct(m["mix"], signed=False, decimals=1)
        pymix_txt, _ = _dpc_fmt_pct(m["py_mix"], signed=False, decimals=1)
        shift_txt, shift_cls = _dpc_fmt_pp(m["shift"])
        yoy_txt, yoy_cls = _dpc_fmt_pct(m["yoy"], signed=True, decimals=0)
        cells = [
            f'<td class="lbl">{_esc_html(m["label"])}</td>',
            f'<td>{_esc_html(_dpc_fmt_m(m["plan"]))}</td>',
            f'<td>{_esc_html(mix_txt)}</td>',
            f'<td>{_esc_html(pymix_txt)}</td>',
            f'<td class="{shift_cls}">{_esc_html(shift_txt)}</td>',
            f'<td class="{yoy_cls}">{_esc_html(yoy_txt)}</td>',
        ]
        return f'<tr class="{cls}">' + "".join(cells) + "</tr>"

    body_rows = [_mix_row(m, cls="section") for m in majors]
    body_rows.append(_mix_row(total_row, cls="total"))

    st.markdown(
        _DPC_LITE_CSS
        + f'<div class="dpc-mixbar">{"".join(segments)}</div>'
        + f'<div class="dpc-mixlegend">{"".join(legend)}</div>'
        + '<div class="dpc-lite"><table><thead><tr>' + head_html
        + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table></div>",
        unsafe_allow_html=True,
    )


# ── Forecast Bias (Lag 1) section ────────────────────────────────────────────
# Lightweight foldable tree: monthly Bias% (green over / red under), a trend
# sparkline, 6-month average, WMAPE, FVA and an over/under + priority Flag.
_BIAS_CSS: str = """
<style>
.bias {overflow-x:auto; margin:.3rem 0 .6rem;}
.bias-in {min-width:960px; background:#ffffff; color:#1f2430; font-size:0.9rem;}
.bias details {margin:0;}
.bias .rw {display:flex; align-items:center; border-bottom:1px solid #f1f2f4;}
.bias .rw > span {flex:1 1 60px; padding:6px 10px; white-space:nowrap;
  text-align:right; overflow:hidden; text-overflow:ellipsis;}
.bias .rw > span.lbl {flex:0 0 240px; text-align:left; display:flex;
  align-items:center; gap:2px;}
.bias .rw > span.wide {flex:1.4 1 74px;}
.bias .hdr {background:#fafafa; color:#6b7280; font-weight:600; font-size:0.72rem;
  text-transform:uppercase; letter-spacing:.02em; border-bottom:2px solid #e5e7eb;}
.bias .hdr > span {text-align:right;}
.bias .hdr > span.lbl {text-align:left;}
.bias summary.rw {cursor:pointer; list-style:none;}
.bias summary.rw::-webkit-details-marker {display:none;}
.bias .rw.total {background:#eef3fb; font-weight:700;}
.bias .rw.section {background:#fbeee6; font-weight:700;}
.bias .rw.subtotal {font-weight:600;}
.bias .rw.memo {font-style:italic; color:#6b7280;}
.bias .tri {flex:0 0 auto; display:inline-block; width:.85em; color:#5a6472;
  transition:transform .12s ease;}
.bias details[open] > summary.rw .tri {transform:rotate(90deg);}
.bias .pos {color:#1b7f3a;}
.bias .neg {color:#c0392b;}
.bias .spark {display:inline-flex; align-items:flex-end; gap:1px; height:16px;
  justify-content:flex-end;}
.bias .spark i {width:4px; display:inline-block; border-radius:1px;}
.bias .chip {display:inline-block; padding:1px 7px; border-radius:9px;
  font-size:.7rem; font-weight:700;}
.bias .chip.pri {background:#fde2e1; color:#c0392b;}
.bias .chip.mon {background:#fdf0d5; color:#9a6a00;}
.bias .chip.dir {background:#eef1f4; color:#55606e; font-weight:600; margin-left:4px;}
.bias .chip.fva {background:#fce8d5; color:#9a4b00; margin-left:4px;}
/* WMAPE font echoes the severity chip colour: pink = Priority, amber = Monitor. */
.bias .wmape-pri {color:#c0392b; font-weight:700;}
.bias .wmape-mon {color:#9a6a00; font-weight:700;}
</style>
"""


def _bias_fmt_pct(frac: object) -> tuple[str, str]:
    """Signed Bias% → (text, css-class): negative in parens + red, positive green."""
    if frac is None or pd.isna(frac):
        return "—", ""
    pct = float(frac) * 100.0
    if abs(round(pct)) == 0:
        return "0%", ""
    return (f"({abs(pct):.0f}%)", "neg") if pct < 0 else (f"{pct:.0f}%", "pos")


def _bias_wmape_txt(frac: object) -> str:
    """Unsigned WMAPE as a whole percent."""
    return "—" if frac is None or pd.isna(frac) else f"{float(frac) * 100:.0f}%"


def _bias_pp(frac: object) -> tuple[str, str]:
    """Percentage-point value (FVA) → (``+2.1pp``, css-class)."""
    if frac is None or pd.isna(frac):
        return "—", ""
    pp = float(frac) * 100.0
    cls = "pos" if pp > 0 else "neg" if pp < 0 else ""
    return f"{pp:+.1f}pp", cls


def _bias_spark(values: list) -> str:
    """Tiny bar sparkline of the monthly Bias% (green over / red under)."""
    nums = [float(v) for v in values if v is not None and not pd.isna(v)]
    if not nums:
        return ""
    scale = max(0.05, max(abs(v) for v in nums))
    bars = []
    for v in values:
        if v is None or pd.isna(v):
            bars.append('<i style="height:2px;background:#d1d5db"></i>')
            continue
        h = max(2, min(16, round(abs(float(v)) / scale * 16)))
        color = "#1b7f3a" if v > 0 else "#c0392b" if v < 0 else "#9ca3af"
        bars.append(f'<i style="height:{h}px;background:{color}"></i>')
    return f'<span class="spark">{"".join(bars)}</span>'


def _wmape_severity_cls(severity: object) -> str:
    """CSS class colouring the WMAPE cell to match its severity chip."""
    if severity == BIAS_FLAG_PRIORITY:
        return "wmape-pri"
    if severity == BIAS_FLAG_MONITOR:
        return "wmape-mon"
    return ""


def _bias_flag_html(direction: object, severity: object, fva_verdict: object) -> str:
    """Flag cell: severity chip (Priority/Monitor) + Over/Under + FVA verdict.

    The FVA chip only appears in its actionable state (``Below naive`` — the
    plan lost to a same-month-last-year guess); positive FVA is the expected
    good case and stays unlabelled to keep the cell readable.
    """
    chips: list[str] = []
    if severity == BIAS_FLAG_PRIORITY:
        chips.append('<span class="chip pri">Priority</span>')
    elif severity == BIAS_FLAG_MONITOR:
        chips.append('<span class="chip mon">Monitor</span>')
    if direction and str(direction) not in ("", "Balanced"):
        chips.append(f'<span class="chip dir">{_esc_html(direction)}</span>')
    if fva_verdict == BIAS_FVA_BELOW:
        chips.append(f'<span class="chip fva">{_esc_html(BIAS_FVA_BELOW)}</span>')
    return "".join(chips) if chips else "—"


def _render_bias_tiles(total: pd.Series | None) -> None:
    """Section KPI tiles: Total B2C WMAPE (6-mo), Bias 6-mo avg, FVA vs naive."""
    wmape = None if total is None else _dpc_num(total, BIAS_COL_WMAPE)
    avg = None if total is None else _dpc_num(total, BIAS_COL_AVG)
    fva = None if total is None else _dpc_num(total, BIAS_COL_FVA)
    avg_txt, avg_cls = _fmt_yoy(avg)          # signed %, up/down/flat colour
    # FVA keeps the "+X.Xpp" text from _bias_pp but must use the KPI-tile CSS
    # vocab (up/down/flat) — the tile stylesheet has no pos/neg rule.
    fva_txt, _fva_pp_cls = _bias_pp(fva)
    fva_cls = {"pos": "up", "neg": "down"}.get(_fva_pp_cls, "flat")
    tiles = (
        ("Total B2C WMAPE — 6-Mo", _bias_wmape_txt(wmape), "",
         "Volume-weighted error: Σ|Actual−Forecast| ÷ Σ|Actual| over the last "
         "6 months. Lower = more accurate."),
        ("Bias — 6-Mo Avg", avg_txt, avg_cls,
         "Average monthly (Forecast−Actual)/Actual. Negative = chronic "
         "under-forecast; positive = over-forecast."),
        ("FVA vs Seasonal-Naive", fva_txt, fva_cls,
         "WMAPE points better than a same-month-last-year guess. "
         "Positive = the plan adds value over the naive benchmark."),
    )
    cards = "".join(
        f'<div class="dpc-kpi"><div class="k-label">{_esc_html(label)}</div>'
        f'<div class="k-value {cls}">{_esc_html(val)}</div>'
        f'<div class="k-desc">{_esc_html(desc)}</div></div>'
        for label, val, cls, desc in tiles
    )
    st.markdown(f'{_DPC_KPI_CSS}<div class="dpc-kpis">{cards}</div>', unsafe_allow_html=True)


def _render_bias_instructions(month_meta: tuple) -> None:
    """Definitions + formulas for the bias section (rolling lag-1)."""
    mapping = ", ".join(
        f"{k}←{cyc}" + (f" (lag {lag})*" if fb else "")
        for k, cyc, lag, fb in month_meta if cyc
    )
    with st.expander("ℹ️ How the metrics and columns are built", expanded=False):
        st.markdown(
            "**Rolling lag-1 forecast.**  For each actual month, the forecast is "
            "taken from the planning cycle whose horizon **starts that month** — "
            "the freshest *one-month-ahead* view — rather than a single fixed "
            "cycle.  This is the industry-standard lag-1 accuracy series: every "
            "column is the same 1-step-ahead horizon, so months are comparable.\n"
            + (f"\n_This run's source per month: {mapping}._\n" if mapping else "")
            + "\n"
            "- **Forecast** = that month's lag-1 cycle **Base Plan** (R&O "
            "excluded — this tracks the committed base plan vs orders).\n"
            "- **Actual** = **IBP Orders (ordered lbs)** — bias measures the "
            "plan vs what customers actually ordered.\n"
            "- The six columns are the months **ending at (and including) the "
            "Prior Month**.  A month with no cycle at exactly lag-1 is "
            "**backfilled** from the nearest earlier cycle (a longer lag), "
            "marked with an asterisk (*).\n\n"
            "**Formulas**\n"
            "- **Bias %** = (Forecast − Actual) ÷ Actual.  Negative = "
            "**under-forecast** (ordered more than planned); positive = "
            "over-forecast.\n"
            "- **6-Mo Avg Bias** = simple average of the monthly Bias %.\n"
            "- **WMAPE** (volume-weighted MAPE) = Σ|Actual − Forecast| ÷ "
            "Σ|Actual| across the shown months.  0% = perfect; it weights big "
            "SKUs more than a plain average of percentages, so one tiny line "
            "can't dominate — the most representative single accuracy number.  "
            "Coloured **pink** when Priority, **amber** when Monitor.\n"
            "- **FVA vs Seasonal-Naive** = WMAPE(same-month-last-year orders) − "
            "WMAPE(forecast), in percentage points.  Positive = the plan beats "
            "just repeating last year (adds value); ≤ 0 = it doesn't.\n"
            "- **Impact (materiality)** = the segment's absolute pound-error ÷ "
            "the **whole B2C** volume (equivalently WMAPE × the segment's share "
            "of volume).  It converts a percentage error into what it actually "
            "**costs the total business**, so a big % miss on a tiny line ranks "
            "below a smaller % miss on a large one.\n\n"
            "**Flag — sized by business impact, not % error alone**\n"
            "Two questions decide the flag, so a small line's large percentage "
            "can't outrank a big line's costlier one:\n"
            "- **Priority** — the forecast is inaccurate (**WMAPE ≥ 10%**) *and* "
            "the error is **material** (Impact ≥ 1% of total B2C volume).  Big "
            "business, badly forecast → fix now.\n"
            "- **Monitor** — a real accuracy gap (**WMAPE ≥ 10%**) but on a "
            "**small** business (Impact < 1%).  Worth watching, not urgent — "
            "e.g. Aerosol can miss ~18% yet barely move the total, so it stays "
            "Monitor, never Priority.\n"
            "- **Blank** — **WMAPE < 10%**: accurate enough, whatever the size "
            "(a large, well-forecast line is not flagged just for being large).\n"
            "- **Direction** (Over > +2pp / Under < −2pp / Balanced) comes from "
            "the 6-Mo Avg Bias; a **Below naive** tag is added when FVA < −0.5pp "
            "(the plan lost to a seasonal-naive guess — simplify it).\n"
            "- **WMAPE colour** echoes the flag: **pink** = Priority, "
            "**amber** = Monitor.\n\n"
            "_The search-to-hide filter (Portfolio Major · Supply Format · "
            "Brand) at the top narrows this section too._"
        )


def _render_bias_tree(
    table: pd.DataFrame, months: tuple[str, ...], month_meta: tuple,
) -> None:
    """Render the foldable Bias-by-segment×month tree (fallback months marked *)."""
    rows = table.reset_index(drop=True)
    indent_flags = rows["_indent"].tolist() if "_indent" in rows.columns else []
    # month_key -> (cycle, lag, is_fallback)
    meta_by_key = {k: (cyc, lag, fb) for k, cyc, lag, fb in month_meta}

    def _cls(row: pd.Series) -> str:
        if str(row.get("_row_id", "")) == "total_b2c":
            return "total"
        if int(row.get("_indent", 0) or 0) == 1:
            return "section"
        if bool(row.get("_is_subtotal", False)):
            return "subtotal"
        if bool(row.get("_is_memo", False)):
            return "memo"
        return ""

    def _make_row(i: int, foldable: bool) -> tuple[str, str]:
        row = rows.iloc[i]
        parts = [f'<span class="lbl">{_tri_span(foldable)}{_esc_html(row.get(DPC_COL_LABEL, ""))}</span>']
        for mk in months:
            txt, c = _bias_fmt_pct(row.get(mk))
            parts.append(f'<span class="{c}">{_esc_html(txt)}</span>')
        parts.append(f'<span class="wide">{_bias_spark([row.get(mk) for mk in months])}</span>')
        avg_txt, avg_c = _bias_fmt_pct(row.get(BIAS_COL_AVG))
        parts.append(f'<span class="{avg_c}">{_esc_html(avg_txt)}</span>')
        wmape_cls = _wmape_severity_cls(row.get(BIAS_COL_FLAG_SEV))
        parts.append(
            f'<span class="{wmape_cls}">'
            f'{_esc_html(_bias_wmape_txt(row.get(BIAS_COL_WMAPE)))}</span>')
        fva_txt, fva_c = _bias_pp(row.get(BIAS_COL_FVA))
        parts.append(f'<span class="{fva_c}">{_esc_html(fva_txt)}</span>')
        flag_html = _bias_flag_html(
            row.get(BIAS_COL_FLAG_DIR), row.get(BIAS_COL_FLAG_SEV),
            row.get(BIAS_COL_FLAG_FVA))
        parts.append(f'<span class="wide">{flag_html}</span>')
        return _cls(row), "".join(parts)

    head = ['<span class="lbl">Segment</span>']
    head += [
        f'<span>{_esc_html(mk)}{"*" if meta_by_key.get(mk, ("", 0, False))[2] else ""}</span>'
        for mk in months
    ]
    head += ['<span class="wide">Trend</span>', "<span>6-Mo Avg Bias</span>",
             "<span>WMAPE</span>", "<span>FVA</span>", '<span class="wide">Flag</span>']
    header = '<div class="rw hdr">' + "".join(head) + "</div>"
    body = _foldable_tree_body(len(rows), indent_flags, _make_row)
    st.markdown(
        _BIAS_CSS + '<div class="bias"><div class="bias-in">'
        + header + "".join(body) + "</div></div>",
        unsafe_allow_html=True,
    )
    # Footnote: which cycle fed each month's lag-1 forecast (+ backfill marks).
    srcs = ", ".join(
        f"{k}←{cyc}" + (f" (lag {lag})*" if fb else "")
        for k, cyc, lag, fb in month_meta if cyc
    )
    if srcs:
        st.caption(
            f"Lag-1 source per month: {srcs}.  \\* = no cycle forecast that "
            "month one-month-ahead, so the nearest earlier cycle is used "
            "(longer lag)."
        )


@st.cache_data(ttl=_CACHE_TTL_SECONDS_OUTPUTS, show_spinner=False)
def _cached_forecast_bias(
    sig_key: tuple,
    filters: ComparisonFilters,
    _tracker_df: pd.DataFrame,
    _ibp_actuals_df: Optional[pd.DataFrame],
    _ibp_naive_df: Optional[pd.DataFrame],
    _pdh_df: Optional[pd.DataFrame],
    _item_master_df: Optional[pd.DataFrame],
) -> tuple[pd.DataFrame, tuple[str, ...], bool, tuple]:
    """Cache the forecast-bias build (native tuple to dodge the dataclass-pickle hazard)."""
    res = build_forecast_bias_table(
        _tracker_df, _ibp_actuals_df, _ibp_naive_df, _pdh_df, filters,
        item_master_df=_item_master_df,
    )
    return res.table, res.months, res.available, res.month_meta


def _render_forecast_bias_section(
    tracker_df: pd.DataFrame,
    pdh_df: Optional[pd.DataFrame],
    item_master_df: Optional[pd.DataFrame],
    filters: ComparisonFilters,
) -> None:
    """Render the Forecast Bias (rolling Lag 1) section — tiles + foldable tree."""
    st.markdown("#### 🎯 Forecast Bias (excl. R&O) — Rolling Lag 1 by Segment × Month")

    # "Actual" = IBP ORDERS for the 6 bias months + the seasonal-naive
    # benchmark (the same months a year earlier).
    bias_cur = last_n_months(filters.prior_month, 6)
    ibp_actuals_df, _ = _load_demand_comparison_ibp_orders(months=tuple(sorted(bias_cur)))
    ibp_naive_df, _ = _load_demand_comparison_ibp_orders(
        months=tuple(sorted(shift_year_back(m) for m in bias_cur)))

    # Rolling lag-1 depends on the tracker's cycle horizons (captured by the
    # tracker signature) + the Prior Month + the search-to-hide filter.
    sig = (
        _signature_for(tracker_df), _signature_for(ibp_actuals_df),
        _signature_for(ibp_naive_df), _signature_for(pdh_df),
        _signature_for(item_master_df),
        filters.prior_month.isoformat(),
        tuple(sorted(filters.combo_exclude)),
    )
    table, months, available, month_meta = _cached_forecast_bias(
        sig, filters, tracker_df, ibp_actuals_df, ibp_naive_df, pdh_df, item_master_df)

    if table is None or table.empty or not available:
        st.info(
            "ℹ️ No data to compute forecast bias for the selected Prior Month — "
            "check that IBP Orders and the tracker's prior cycles are populated."
        )
        return

    # Instructions first (definitions before the numbers), then the headline
    # tiles, then the segment tree.
    _render_bias_instructions(month_meta)
    by_id = {str(r["_row_id"]): r for _, r in table.iterrows()}
    _render_bias_tiles(by_id.get("total_b2c"))
    _render_bias_tree(table, months, month_meta)

    # Opt-in drill: Corporate group × SKU drivers of the segment miss.
    _render_bias_corp_sku_drivers(
        tracker_df, pdh_df, item_master_df, filters,
        ibp_actuals_df, ibp_naive_df, table)


# ── Corporate group × SKU drivers (opt-in drill under the bias tree) ─────────
_BIAS_DRIVERS_LOADED_KEY: str = "bias_corp_sku_drivers_loaded"
_BIAS_DRIVERS_SEG_KEY: str = "bias_corp_sku_drivers_segment"
_BIAS_DRIVER_PICK_KEY: str = "bias_corp_sku_driver_pick"
# Below this forecast-side attribution the party_site→corporate_group join
# isn't reconciling, so a corp split would be misleading — show a diagnostic
# instead of fabricated drivers (the view auto-enables once the dims are fixed).
_BIAS_DRIVERS_MIN_FCST_ATTR: float = 0.30


@st.cache_data(ttl=_CACHE_TTL_SECONDS_OUTPUTS, show_spinner=False)
def _cached_corp_sku_drivers(
    sig_key: tuple,
    filters: ComparisonFilters,
    segment_row_id: str,
    _tracker_df: pd.DataFrame,
    _actuals_df: Optional[pd.DataFrame],
    _naive_df: Optional[pd.DataFrame],
    _pdh_df: Optional[pd.DataFrame],
    _item_master_df: Optional[pd.DataFrame],
    _shiptosites_df: Optional[pd.DataFrame],
    _plantosites_df: Optional[pd.DataFrame],
    _customernames_df: Optional[pd.DataFrame],
) -> tuple:
    """Cache the corp×SKU driver build (native tuple → dodges the pickle hazard)."""
    res = build_forecast_bias_corp_sku_drivers(
        _tracker_df, _actuals_df, _naive_df, _pdh_df, filters,
        segment_row_id=segment_row_id,
        shiptosites_df=_shiptosites_df, plantosites_df=_plantosites_df,
        customernames_df=_customernames_df, item_master_df=_item_master_df, top_n=3)
    return (res.drivers, res.months, res.segment_label, res.segment_volume,
            res.attributed_share, res.forecast_attributed_share, res.available)


def _load_corp_group_dims() -> tuple[
    Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame], str
]:
    """Fetch the three corp-group dims (cached).

    Returns ``(shiptosites, plantosites, customernames, warning)`` — the chain
    party_site → plan_to_code → customer_num → corporate_group.
    """
    try:
        sts = fetch_dimshiptosites_df()
        pts = fetch_dp_dimplantosites_df()
    except (ShipToSitesSourceError, Exception) as exc:  # noqa: BLE001
        return None, None, None, f"ship-to / plan-to dims — {exc}"
    try:
        names = fetch_dp_dimcustomernames_df()
    except (CustomerDimsError, Exception) as exc:  # noqa: BLE001
        return sts, pts, None, f"customer-names dim — {exc}"
    return sts, pts, names, ""


def _bias_default_segment(table: pd.DataFrame) -> str:
    """Worst-flagged segment (highest impact among flagged), else Total B2C."""
    if BIAS_COL_FLAG_SEV in table.columns:
        flagged = table[table[BIAS_COL_FLAG_SEV].astype(str).str.len() > 0]
    else:
        flagged = table.iloc[0:0]
    pool = flagged if not flagged.empty else table
    if BIAS_COL_IMPACT in pool.columns and not pool.empty:
        pool = pool.sort_values(BIAS_COL_IMPACT, ascending=False)
    return str(pool.iloc[0]["_row_id"]) if not pool.empty else "total_b2c"


def _corp_driver_label(row: pd.Series) -> str:
    """Corporate-group display, marking soft (~name fallback) / Unattributed."""
    corp = str(row.get("corp_group", ""))
    if bool(row.get("soft")):
        return f"~{corp}"      # customer-name fallback → softer attribution
    return corp


def _render_bias_corp_sku_drivers(
    tracker_df: pd.DataFrame,
    pdh_df: Optional[pd.DataFrame],
    item_master_df: Optional[pd.DataFrame],
    filters: ComparisonFilters,
    ibp_actuals_df: Optional[pd.DataFrame],
    ibp_naive_df: Optional[pd.DataFrame],
    bias_table: pd.DataFrame,
) -> None:
    """Foldable, opt-in Corporate × SKU driver drill for the bias section.

    Lazy by design: nothing computes and no corp-group dims are read until the
    planner clicks *Load*, so the high-level report above never pays for it.
    """
    with st.expander("🔎 Forecast Bias Corporate × SKU Drivers", expanded=False):
        st.caption(
            "Drill into the **Corporate group × SKU** cells driving a segment's "
            "lag-1 base-plan miss — ranked by **pounds of error (impact)**, the "
            "cells actually moving the number.  Loads on demand so it never "
            "slows the report above."
        )
        with st.expander("ℹ️ How corporate group is derived", expanded=False):
            st.markdown(
                "Corporate group resolves to the **same** table on both sides, so "
                "forecast and orders land in identical buckets:\n"
                "- **Forecast (tracker)** — the tracker only carries a **Party "
                "Site Number**, so it's resolved through a bridge:\n"
                "  `party_site → dp_dimshiptosites.plan_to_code → "
                "dp_dimplantosites.customer_num → dp_dimcustomernames."
                "corporate_group`.\n"
                "- **Orders (actual)** — carry a Customer No directly: "
                "`customer_no → dp_dimcustomernames.corporate_group`.\n\n"
                "**Why `plan_to_code` is the bridge key (not `customer_num`):** "
                "`dp_dimshiptosites`'s own `customer_num` is a *different key "
                "space* that doesn't match `dp_dimcustomernames` (≈0% overlap), "
                "so it's a dead end.  Its **`plan_to_code`** maps through "
                "`dp_dimplantosites` to a `customer_num` that matches "
                "`dp_dimcustomernames` **1:1** — the working path (~98% of the "
                "forecast).  Rows that can't be mapped show as **Unattributed** "
                "(and `~name` marks a softer customer-name fallback on the orders "
                "side)."
            )
        if not st.session_state.get(_BIAS_DRIVERS_LOADED_KEY):
            if st.button("📥 Load / refresh drivers", key="bias_drivers_load_btn"):
                st.session_state[_BIAS_DRIVERS_LOADED_KEY] = True
                st.rerun()
            return

        sts, pts, names, dim_warn = _load_corp_group_dims()
        if sts is None or pts is None or names is None:
            st.warning(f"⚠️ Could not load the corporate-group dimensions ({dim_warn}).")
            return

        seg_ids = [str(r["_row_id"]) for _, r in bias_table.iterrows()]
        labels = {
            str(r["_row_id"]): str(r.get(DPC_COL_LABEL, r["_row_id"]))
            .replace(" ", "").replace("• ", "").strip()
            for _, r in bias_table.iterrows()
        }
        st.session_state.setdefault(_BIAS_DRIVERS_SEG_KEY, _bias_default_segment(bias_table))
        seg = st.selectbox(
            "Segment", options=seg_ids, key=_BIAS_DRIVERS_SEG_KEY,
            format_func=lambda s: labels.get(s, s),
            help="Pick a node from the accuracy table; its top-3 Corporate × SKU "
                 "drivers show below.",
        )

        sig = (
            _signature_for(tracker_df), _signature_for(ibp_actuals_df),
            _signature_for(ibp_naive_df), _signature_for(pdh_df),
            _signature_for(item_master_df), _signature_for(sts),
            _signature_for(pts), _signature_for(names),
            filters.prior_month.isoformat(), tuple(sorted(filters.combo_exclude)), seg,
        )
        (drivers, months, seg_label, seg_vol, attr, fcst_attr, avail
         ) = _cached_corp_sku_drivers(
            sig, filters, seg, tracker_df, ibp_actuals_df, ibp_naive_df,
            pdh_df, item_master_df, sts, pts, names)

        if not avail or drivers is None or drivers.empty:
            st.info("No drivers for this segment / prior month.")
            return

        # Guard: if the forecast couldn't be attributed to corporate groups, the
        # corp split is meaningless — show exactly what to fix, and auto-enable
        # once the upstream dim keys reconcile.
        if pd.isna(fcst_attr) or fcst_attr < _BIAS_DRIVERS_MIN_FCST_ATTR:
            shown = 0.0 if pd.isna(fcst_attr) else fcst_attr
            st.warning(
                f"⚠️ **Corporate attribution low — only {shown:.0%} of "
                f"{seg_label}'s base-plan forecast mapped to a corporate group.**  "
                "The forecast chain is `party_site → dp_dimshiptosites.plan_to_code "
                "→ dp_dimplantosites.customer_num → dp_dimcustomernames."
                "corporate_group`; a low rate means party sites are missing a "
                "`plan_to_code` (in dp_dimshiptosites) or their `plan_to_code` "
                "isn't in dp_dimplantosites.  Fix those rows upstream and this view "
                "fills in automatically."
            )
            return

        _render_corp_sku_driver_rows(drivers, months, seg_label, seg_vol, attr, fcst_attr)


def _render_corp_sku_driver_rows(
    drivers: pd.DataFrame, months: tuple, seg_label: str,
    seg_vol: float, attr: float, fcst_attr: float,
) -> None:
    """Top-3 driver rows (sparkline + severity-coloured WMAPE) + a drill chart."""
    def _make_row(i: int, _foldable: bool) -> tuple[str, str]:
        row = drivers.iloc[i]
        corp = _corp_driver_label(row)
        sku = f"{row.get('item_key', '')} · {row.get('item_desc', '')}".strip(" ·")
        parts = [f'<span class="lbl">{_esc_html(corp)}</span>',
                 f'<span class="wide">{_esc_html(sku)}</span>',
                 f'<span>{_esc_html(_dpc_fmt_m(row.get(BIAS_COL_VOLUME)))}</span>']
        wcls = _wmape_severity_cls(row.get(BIAS_COL_FLAG_SEV))
        parts.append(f'<span class="{wcls}">{_esc_html(_bias_wmape_txt(row.get(BIAS_COL_WMAPE)))}</span>')
        avg_txt, avg_c = _bias_fmt_pct(row.get(BIAS_COL_AVG))
        parts.append(f'<span class="{avg_c}">{_esc_html(avg_txt)}</span>')
        parts.append(f'<span>{row.get(BIAS_COL_IMPACT, float("nan"))*100:.1f}%</span>'
                     if not pd.isna(row.get(BIAS_COL_IMPACT)) else "<span>—</span>")
        parts.append(f'<span class="wide">{_bias_spark([row.get(m) for m in months])}</span>')
        return "", "".join(parts)

    head = ('<div class="rw hdr"><span class="lbl">Corporate group</span>'
            '<span class="wide">SKU</span><span>Vol (M)</span><span>WMAPE</span>'
            '<span>Avg Bias</span><span>Impact</span><span class="wide">Trend</span></div>')
    body = "".join(
        f'<div class="rw">{_make_row(i, False)[1]}</div>' for i in range(len(drivers)))
    st.markdown(
        _BIAS_CSS + '<div class="bias"><div class="bias-in">' + head + body + "</div></div>",
        unsafe_allow_html=True,
    )
    st.caption(
        f"Ranked by pound-error within **{seg_label}** (segment orders "
        f"{seg_vol:,.1f}M lbs).  Forecast attributed {fcst_attr:.0%} · orders "
        f"attributed {attr:.0%} to a corporate group.  `~name` = softer "
        "customer-name fallback; **Unattributed** = unmapped pounds."
    )

    # Drill: pick one driver → monthly forecast-vs-actual + bias% chart.
    idx = list(range(len(drivers)))
    st.session_state.setdefault(_BIAS_DRIVER_PICK_KEY, 0)
    pick = st.selectbox(
        "Monthly trend for driver", options=idx, key=_BIAS_DRIVER_PICK_KEY,
        format_func=lambda i: f"{_corp_driver_label(drivers.iloc[i])} · "
                              f"{drivers.iloc[i].get('item_desc') or drivers.iloc[i].get('item_key')}",
    )
    _render_corp_sku_driver_chart(drivers.iloc[pick], months)


def _render_corp_sku_driver_chart(row: pd.Series, months: tuple) -> None:
    """Grouped monthly base-plan vs orders bars + a bias% line (secondary axis)."""
    fcst = list(row.get("_fcst", ())) or [0.0] * len(months)
    act = list(row.get("_act", ())) or [0.0] * len(months)
    bias_pct = [
        (row.get(m) * 100.0 if row.get(m) is not None and not pd.isna(row.get(m)) else None)
        for m in months
    ]
    fig = go.Figure()
    fig.add_bar(name="Base plan", x=list(months), y=fcst, marker_color="#9aa7b8")
    fig.add_bar(name="Orders (actual)", x=list(months), y=act, marker_color="#2f5d8a")
    fig.add_trace(go.Scatter(
        name="Bias %", x=list(months), y=bias_pct, yaxis="y2",
        mode="lines+markers", line=dict(color="#c0392b", width=2)))
    fig.update_layout(
        barmode="group", height=300, margin=dict(l=10, r=10, t=30, b=10),
        yaxis=dict(title="M lbs"),
        yaxis2=dict(title="Bias %", overlaying="y", side="right", zeroline=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0.0),
    )
    st.plotly_chart(fig, use_container_width=True, theme=None)


def _render_demand_comparison_table(
    result, filters: Optional[ComparisonFilters] = None,
    *, ns: str = "", allow_save: bool = True,
    extra_cols: Optional[list[str]] = None,
    header_overrides: Optional[dict[str, str]] = None,
    period_overrides: Optional[dict[str, str]] = None,
    ro_hint: bool = True,
) -> None:
    """Render the detailed comparison table (foldable tree) + controls + exports.

    Layout: the tree first, then (below it) the detail-column toggle, then the
    Download / Save-to-Fabric buttons.  Folding is per-row and native (click a
    parent row) — there is no global fold control.  The column toggle lives
    below the table but its state is read up-front via session_state.  When
    *filters* is given, the two total-plan column headers are anchored on the
    chosen cycles (e.g. "C3 Plan (incl. R&O)" / "C4 Plan (incl. R&O)").  *ns*
    namespaces the widget keys (so an APS mirror coexists with the IBP one);
    *allow_save* gates the Save-to-Fabric button (off for the APS view, which
    must not overwrite the IBP comparison output).

    ``extra_cols`` appends APS-only frame columns to the picker universe (hidden
    by default, so the default view still matches IBP); ``header_overrides`` /
    ``period_overrides`` merge into the header text / 2nd-line month ranges (used
    by the APS view to surface "YTD Actuals" + "C5 YTG Fcst w.o. RO").  Set
    ``ro_hint=False`` to suppress the "R&O is zero — save the RO Summary" caption
    (the APS view derives R&O from the tracker legs, not the RO Summary lookup,
    so that hint would be stale there).
    """
    extra_cols = extra_cols or []
    table = result.table
    if table is None or table.empty:
        st.info("No comparison rows to display.")
        return

    # Full frame (all rows + cols, metadata stripped) for Download / Fabric
    # save — always the complete data, independent of the on-screen column
    # toggle (row folding is client-side only, so every row is already here).
    save_df = table.drop(
        columns=[c for c in ("_row_id", "_indent", "_is_subtotal", "_is_memo")
                 if c in table.columns]
    ).reset_index(drop=True)

    # Column-visibility state is read here (the picker is rendered BELOW the
    # table); session_state carries it across the rerun the widget triggers.
    # Every metric column is individually hidable; the default-visible set is
    # everything except COLS_HIDDEN_BY_DEFAULT (the screenshot-3 column set).
    # Seed the default BEFORE the table reads it (the picker renders below the
    # table but its state is read up-front via session_state).
    detail_cols_key = _ns_key(_DPC_DETAIL_COLS_KEY, ns)
    # Extra (APS-only) columns are added to the picker universe but hidden by
    # default, so the DEFAULT view still matches the IBP table exactly.
    _extra = [c for c in extra_cols if c in table.columns]
    all_detail_labels = [DPC_DISPLAY_LABELS[c] for c in DPC_DISPLAY_ORDER] + _extra
    _hidden_default = {DPC_DISPLAY_LABELS[c] for c in DPC_COLS_HIDDEN_BY_DEFAULT} | set(_extra)
    default_visible = [lbl for lbl in all_detail_labels if lbl not in _hidden_default]
    st.session_state.setdefault(detail_cols_key, default_visible)

    # Percent ids → display labels; stored values are fractions, ×100 to show.
    percent_labels = [DPC_DISPLAY_LABELS[c] for c in DPC_PERCENT_COLS]

    # Row-type flags (positional) captured before dropping the metadata cols.
    # The full row set is rendered — the tree folds levels client-side.
    subtotal_flags = table["_is_subtotal"].tolist()
    memo_flags = table["_is_memo"].tolist()
    row_ids = table["_row_id"].tolist() if "_row_id" in table.columns else None
    indent_flags = table["_indent"].tolist() if "_indent" in table.columns else []

    display_df = table.drop(
        columns=[c for c in ("_row_id", "_indent", "_is_subtotal", "_is_memo")
                 if c in table.columns]
    ).reset_index(drop=True)
    for label in percent_labels:
        if label in display_df.columns:
            display_df[label] = display_df[label] * 100.0

    # Visible metric columns = the planner's picks, in the ORDER they were
    # ticked (from the Columns popover below); Category stays fixed at the left.
    visible_metric_cols = _picked_columns(detail_cols_key, all_detail_labels)

    # Cycle-anchored header text for the two total-plan columns (display only —
    # the df column keys stay the DISPLAY_LABELS names so all lookups hold).
    header_labels = (
        {
            DPC_DISPLAY_LABELS[DPC_COL_LAST_PLAN]:
                f"{filters.prior_cycle} Plan (incl. R&O)",
            DPC_DISPLAY_LABELS[DPC_COL_CURRENT_PLAN]:
                f"{filters.current_cycle} Plan (incl. R&O)",
        }
        if filters is not None else {}
    )

    # Period sub-labels rendered as a 2nd header line on the three variance
    # columns, so each variance reads with the window it covers: PM Actual Var.
    # is anchored on the selected prior month; Base Plan Var. and R&O Var. on the
    # selected forecast window.
    period_labels = (
        {
            DPC_DISPLAY_LABELS[DPC_COL_PM_ACTUAL]: f"({filters.prior_month:%b%y})",
            DPC_DISPLAY_LABELS[DPC_COL_BASE_PLAN]:
                f"({filters.forecast_start:%b%y}-{filters.forecast_end:%b%y})",
            DPC_DISPLAY_LABELS[DPC_COL_R_AND_O]:
                f"({filters.forecast_start:%b%y}-{filters.forecast_end:%b%y})",
        }
        if filters is not None else {}
    )

    # APS-view overrides (e.g. "YTD Actuals", "C5 YTG Fcst w.o. RO" + windows).
    if header_overrides:
        header_labels = {**header_labels, **header_overrides}
    if period_overrides:
        period_labels = {**period_labels, **period_overrides}

    # Foldable tree — navy header + white font, light-blue Total B2C, orange
    # (#f8cbad) Portfolio-Major rows (incl. Butter); click a parent row to fold.
    _render_comparison_tree(
        display_df,
        label_col=DPC_COL_LABEL,
        metric_cols=visible_metric_cols,
        percent_labels=percent_labels,
        row_ids=row_ids,
        subtotal_flags=subtotal_flags,
        memo_flags=memo_flags,
        indent_flags=indent_flags,
        header_labels=header_labels,
        period_labels=period_labels,
    )

    if ro_hint and not result.ro_summary_available:
        st.caption(
            "_R&O is zero because the RO Summary Report could not be read. "
            "Save the RO Summary Report above to populate it._"
        )

    # Column picker + exports, below the table (per planner layout).
    _render_comparison_table_controls(all_detail_labels, header_labels, ns=ns)
    _render_comparison_table_exports(result, save_df, ns=ns, allow_save=allow_save)


def _render_comparison_table_controls(
    all_labels: list[str], label_overrides: Optional[dict[str, str]] = None,
    *, ns: str = "",
) -> None:
    """⚙️ Columns picker for the detailed table, rendered below it.

    Lists EVERY metric column so the planner can show/hide/reorder any of them
    (incl. the extra-detail columns that start hidden), via the shared
    :func:`_render_column_picker`.  *label_overrides* keeps the picker's visible
    names in sync with the table's cycle-anchored headers.  *ns* namespaces the
    widget key.  Category always shows; Download / Save always include every
    column.
    """
    _render_column_picker(
        _ns_key(_DPC_DETAIL_COLS_KEY, ns), all_labels, label_overrides=label_overrides,
        help_suffix="  The Category column always shows; Download and Save to "
                    "Fabric always include every column.",
    )


def _render_comparison_table_exports(
    result, save_df: pd.DataFrame, *, ns: str = "", allow_save: bool = True,
) -> None:
    """Download (+ optional Save-to-Fabric) buttons, rendered at the very bottom.

    Both act on the FULL frame (``save_df`` / ``result``), never the folded /
    column-trimmed on-screen view.  *ns* namespaces the widget keys; *allow_save*
    gates the Save button (the APS view is download-only — it must not overwrite
    the IBP comparison output file).
    """
    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    cols = st.columns(2) if allow_save else st.columns(1)
    with cols[0]:
        st.download_button(
            label="⬇️ Download Demand Plan Comparison Summary (CSV)",
            data=comparison_to_csv_bytes(result),
            file_name=f"demand_plan_comparison_summary_{today}.csv",
            mime="text/csv",
            key=_ns_key("demand_plan_comparison_download", ns),
            type="primary",
            width="stretch",
            help=(
                "Downloads the full comparison — every row and metric column, "
                "regardless of the on-screen fold level."
            ),
        )
    if not allow_save:
        return
    with cols[1]:
        if st.button(
            "💾 Save to Fabric (overwrite)",
            key=_ns_key("demand_plan_comparison_save", ns),
            type="primary",
            width="stretch",
            help=(
                "Overwrites `Files/RO Tracking/Demand Plan/"
                "qry_demand_plan_comparison_summary.csv` with the full table so "
                "the comparison can be consumed without recomputing."
            ),
        ):
            try:
                with st.spinner("Saving Demand Plan Comparison Summary to Microsoft Fabric…"):
                    blob_path = save_demand_plan_comparison(save_df)
            except DemandSummaryError as exc:
                st.error(f"❌ Save failed.\n\n{exc}")
            else:
                st.success(f"✅ Saved to `Files/{blob_path}` ({len(save_df)} rows).")


# ── APS / Oracle Demand Plan Comparison Summary (mirror of the IBP section) ───────────
_APS_CMP_ENABLED_KEY: str = "aps_comparison_enabled"


@st.cache_data(ttl=900, show_spinner="Loading APS history tracker…")
def _cached_aps_history() -> Optional[pd.DataFrame]:
    """Cached read of the APS history tracker (``None`` until a cycle is built).

    The tracker is a large (≈1M-row) OneLake CSV, so a cold read (e.g. right
    after a build clears this cache) takes a while — ``show_spinner`` surfaces
    that so the sections below don't look like they vanished.
    """
    return fetch_aps_history_df()


def _build_aps_merged_tracker(
    aps_hist: pd.DataFrame, ibp_tracker: pd.DataFrame,
    current_cycle: str, prior_cycle: str,
) -> tuple[pd.DataFrame, str]:
    """Merge APS[current cycle] (current) with IBP[prior cycle] (prior baseline).

    Returns ``(merged_raw_tracker, effective_prior_label)``.  If the two cycle
    labels collide, the IBP prior rows are relabelled so the comparison's cycle
    filter can distinguish current from prior.
    """
    aps_cur = aps_hist[
        aps_hist["Cycle"].astype(str).str.strip() == str(current_cycle)].copy()
    ibp_prior = ibp_tracker[
        ibp_tracker["Cycle"].astype(str).str.strip() == str(prior_cycle)].copy()
    prior_label = str(prior_cycle)
    if str(current_cycle) == prior_label:
        prior_label = f"{prior_cycle} (IBP prior)"
        ibp_prior["Cycle"] = prior_label
    merged = pd.concat([aps_cur, ibp_prior], ignore_index=True, sort=False)
    return merged, prior_label


@st.cache_data(ttl=_CACHE_TTL_SECONDS_OUTPUTS, show_spinner=False)
def _cached_aps_comparison_options(
    _aps_sig: tuple, _ibp_sig: tuple, _pdh_sig: tuple,
    _aps_hist: pd.DataFrame,
    _ibp_tracker: Optional[pd.DataFrame],
    _pdh: Optional[pd.DataFrame],
) -> tuple[list, list, list, list]:
    """Cache the APS-comparison filter-option discovery.

    ``list_aps_history_cycles`` / ``list_tracker_months`` / ``list_comparison_
    combos`` each scan the ≈1M-row tracker; without this they re-ran on every
    rerun (each filter tweak).  Keyed on the source shape signatures, so the
    scans recompute only when the underlying data actually changes.
    """
    aps_cycles = list_aps_history_cycles(_aps_hist)
    ibp_cycles = list_tracker_cycles(_ibp_tracker) if _ibp_tracker is not None else []
    months = sorted(
        set(list_tracker_months(_ibp_tracker)) | set(list_tracker_months(_aps_hist)))
    combos = list_comparison_combos(_aps_hist, _pdh)
    return aps_cycles, ibp_cycles, months, combos


def _render_aps_cycle_kpis(kpis: ComparisonKpis, filters: ComparisonFilters) -> None:
    """APS cycle-over-cycle top metrics (its own tile set, distinct from IBP's).

    Base Plan Var. · R&O Var. · Total Delta · Total Delta % · Actl% of Current
    Plan.  Values are read off the Total B2C row (via ``kpis``) so they tie to
    the table by construction.  Actl% = Total Actual ÷ Current Plan (incl. R&O).
    """
    prior_cy, current_cy = filters.prior_cycle, filters.current_cycle
    total_delta = (
        kpis.current_plan_total - kpis.last_plan_total
        if kpis.current_plan_total is not None and kpis.last_plan_total is not None
        else None)
    total_delta_pct = (
        total_delta / kpis.last_plan_total
        if total_delta is not None and kpis.last_plan_total else None)
    actl_pct = (
        kpis.total_actual_total / kpis.current_plan_total
        if kpis.total_actual_total is not None and kpis.current_plan_total else None)
    tiles = (
        ("Base Plan Var.", _fmt_millions(kpis.base_plan_var, signed=True),
         f"{current_cy} vs {prior_cy} baseline forecast"),
        ("R&O Var.", _fmt_millions(kpis.ro_var, signed=True),
         f"{current_cy} vs {prior_cy} R&O forecast"),
        ("Total Delta", _fmt_millions(total_delta, signed=True),
         f"{current_cy} vs {prior_cy} total plan move"),
        ("Total Delta %", _fmt_pct_walk(total_delta_pct),
         f"{current_cy} vs {prior_cy}: total plan % change"),
        ("Actl% of Current Plan", _fmt_pct_share(actl_pct),
         f"actual shipments ÷ {current_cy} plan (incl. R&O)"),
    )
    cards = "".join(
        f'<div class="dpc-kpi dpc-kpi--walk">'
        f'<div class="k-label">{_esc_html(label)}</div>'
        f'<div class="k-value {cls}">{_esc_html(text)}</div>'
        f'<span class="k-sub">{_esc_html(sub)}</span></div>'
        for label, (text, cls), sub in tiles
    )
    st.markdown(
        f'{_DPC_KPI_CSS}<div class="dpc-kpis">{cards}</div>',
        unsafe_allow_html=True,
    )


def _render_aps_comparison_section(aps_hist: Optional[pd.DataFrame]) -> None:
    """APS mirror of Demand Plan Comparison Summary (no bias; incl. Prior Month).

    Current cycle = the APS history tracker (*aps_hist*, read once by the
    caller); prior baseline = a chosen cycle of the IBP
    ``qry_mgmt_plan_history_tracker.csv``.  Same filters + tables as the IBP
    section (reused via an ``"aps"`` widget-key namespace so both coexist).
    RO-Summary is zeroed (APS has none); Budget is matched identically to the
    IBP section (FY27 workbook by row-id); actuals reuse IBP Orders /
    Shipments.  All controls appear whenever the APS history file exists.
    """
    with st.expander("🧭 APS / Oracle Demand Plan Comparison Summary", expanded=False):
        st.caption(
            "Compares the **APS / Oracle plan** (current cycle, from the APS history "
            "tracker) against a chosen **prior cycle of the IBP tracker** "
            "(`qry_mgmt_plan_history_tracker.csv`).  Same filters + YoY / summary "
            "/ mix / cycle-over-cycle / Prior-Month tables as the IBP section; "
            "actuals reuse IBP Orders / Shipments.  (No forecast-accuracy section.)  "
            "**R&O Var.** here is the cycle-over-cycle delta of the tracker's own "
            "R&O rows — current-cycle APS R&O minus prior-cycle IBP R&O over the "
            "forecast window (unlike the IBP section, which reads the RO Summary "
            "Report).  Cross-check it against the 🔁 RO Comparison table.  "
            "**Base Plan Var. + R&O Var. = Total Delta** (both are current − prior "
            "leg deltas over the forecast window)."
        )
        if not fabric_signin_widget.is_fabric_signed_in():
            st.warning("🔒 **Microsoft Fabric is not connected.**  Sign in first.")
            return

        if aps_hist is None or aps_hist.empty:
            st.info(
                "ℹ️ No APS history yet — build a cycle in **Demand Summary (APS / Oracle)** "
                "above; this comparison lights up once "
                "`qry_mgmt_plan_full_aps_history.csv` exists."
            )
            return
        try:
            ibp_tracker = fetch_mgmt_plan_history_tracker().df
        except DemandSummaryError as exc:
            st.error(f"❌ Could not load the IBP tracker for the prior baseline.\n\n{exc}")
            return
        # Filter-option discovery (cycles / months / combos) scans the whole
        # tracker; cache it on the source shapes so it doesn't re-run per rerun.
        pdh_df = _load_demand_comparison_pdh()
        aps_cycles, ibp_cycles, months, combo_options = _cached_aps_comparison_options(
            _signature_for(aps_hist), _signature_for(ibp_tracker), _signature_for(pdh_df),
            aps_hist, ibp_tracker, pdh_df)
        if not aps_cycles:
            st.warning("The APS history has no Cycle values yet.")
            return
        if not ibp_cycles:
            st.warning("The IBP tracker has no Cycle values for the prior baseline.")
            return

        # Actual-window months come from IBP Shipments (falls back to the trackers').
        try:
            actual_months = list(fetch_ibp_shipments_months())
        except IBPOfficialSourceError:
            actual_months = months
        if not actual_months:
            actual_months = months

        # Same filter widget as the IBP section (current cycles = APS, prior = IBP).
        with st.expander("🔍 Filters", expanded=True):
            filters = _render_demand_comparison_filters(
                aps_cycles, months, actual_months, combo_options,
                prior_cycles=ibp_cycles, ns="aps",
                current_label="Current cycle (APS)",
                prior_label="Prior cycle (IBP tracker)")

        enabled = st.session_state.get(_APS_CMP_ENABLED_KEY, False)
        if st.button(
            "▶️ Generate Demand Plan Comparison Summary (APS / Oracle)",
            key="aps_cmp_generate", type="primary", use_container_width=True,
        ):
            enabled = True
            st.session_state[_APS_CMP_ENABLED_KEY] = True
        if not enabled:
            st.info("👆 Click **Generate Demand Plan Comparison Summary (APS / Oracle)** to build the tables.")
            return

        # Merge APS[current] + IBP[prior]; relabel prior on a label collision so
        # the comparison's cycle filter can tell them apart, then align filters.
        merged, prior_label = _build_aps_merged_tracker(
            aps_hist, ibp_tracker, filters.current_cycle, filters.prior_cycle)
        if merged.empty:
            st.warning("No rows for the chosen cycle pair.")
            return
        filters = replace(filters, prior_cycle=prior_label)
        errors = validate_filters(filters)
        if errors:
            for msg in errors:
                st.error(f"❌ {msg}")
            return

        # Supporting sources via the SAME loader the IBP fragment uses (dims +
        # IBP actuals + PY / trailing windows + FY27 budget).  RO-Summary is
        # zeroed (APS has none); Budget is matched identically to IBP.
        _src = _load_comparison_supporting_sources(filters)
        pdh_df, item_master_df = _src.pdh_df, _src.item_master_df
        ibp_df, ibp_orders_df, ibp_py_df = _src.ibp_df, _src.ibp_orders_df, _src.ibp_py_df
        ibp_recent_df, ibp_recent_py_df = _src.ibp_recent_df, _src.ibp_recent_py_df
        budget_by_row_id = _src.budget_by_row_id
        budget_warnings = _src.budget_warnings
        budget_lookup_key = _src.budget_lookup_key

        tracker_sig = _signature_for(merged)
        enrich_sig = (
            tracker_sig, _signature_for(ibp_df), _signature_for(ibp_orders_df),
            _signature_for(ibp_py_df), _signature_for(ibp_recent_df),
            _signature_for(ibp_recent_py_df), _signature_for(pdh_df),
            _signature_for(item_master_df))
        empty_ro_sig = (0, 0.0)   # APS R&O Var comes from the tracker, not RO Summary

        with st.spinner("Building APS Demand Plan Comparison…"):
            enriched = _cached_enriched_sources(
                *enrich_sig, merged, ibp_df, ibp_orders_df, ibp_py_df,
                ibp_recent_df, ibp_recent_py_df, pdh_df, item_master_df)
            # shift_last_plan_window=False → the Prior Plan (an IBP cycle) uses
            # the same window as the current plan (= the IBP file's plan for that
            # cycle).  ro_var_from_tracker=True → R&O Var is the cycle-over-cycle
            # delta of the tracker's R&O rows (current APS − prior IBP), so it
            # ties to the tracker and reconciles with the RO Comparison table.
            table, build_warnings, ro_available = _cached_demand_plan_comparison_payload(
                enrich_sig + (empty_ro_sig, budget_lookup_key, "noshift", "rotrk"),
                filters, empty_ro_sig, budget_lookup_key, enriched, {}, budget_by_row_id,
                shift_last_plan_window=False, ro_var_from_tracker=True)
            prior_month_vs_fcst = _cached_prior_month_actual_vs_fcst_table(
                enrich_sig + (filters.prior_month,), filters, enriched)
        result = ComparisonResult(
            table=table, warnings=build_warnings, ro_summary_available=ro_available)

        # APS derives R&O from the tracker (not the RO Summary Report), so the
        # builder's "RO Summary unavailable → R&O is zero" advisory does NOT
        # apply here — drop it so it doesn't read as a failed generation.
        for msg in list(build_warnings) + list(budget_warnings):
            if "RO Summary Report" in msg:
                continue
            st.warning(f"⚠️ {msg}")

        # Not-captured log (same design as the IBP section): rows that meet the
        # filters but roll into NO comparison row — with the reason (their
        # Portfolio Major / Supply Format / Brand / Portfolio Minor match no
        # comparison-template family) — surfaced ABOVE the tables so the planner
        # reconciles before trusting the totals.
        _aps_nc_dim = build_item_dim_frame_cascade(pdh_df, item_master_df)
        _render_comparison_not_captured_logs(
            build_comparison_not_captured(
                enriched.tracker, filters,
                ibp_enriched=enriched.ibp, dim_frame=_aps_nc_dim),
            ns="aps")

        st.markdown("#### 📈 YoY Comparison")
        _render_comparison_summary_col_picker(ns="aps")
        kpis = build_comparison_kpis(
            result.table, enriched.ibp_recent, enriched.ibp_recent_py, filters)
        _aps_order_yoy, _aps_order_labels = _cached_comparison_order_yoy(filters.prior_month)
        _aps_periods = _comparison_period_labels(filters, _aps_order_labels)
        _render_comparison_kpis_yoy(
            kpis, order_yoy_total=_aps_order_yoy.get("total_b2c", {}), periods=_aps_periods)
        _render_comparison_summary_table(
            result, ns="aps", order_yoy_by_row=_aps_order_yoy, periods=_aps_periods)
        _render_comparison_mix_table(result)

        st.markdown("---")
        st.markdown("#### 🔄 Cycle over Cycle Comparison")
        # APS uses its OWN top metrics + table (prior-vs-current leg layout),
        # distinct from the IBP walk/table.
        _render_aps_cycle_kpis(kpis, filters)
        # Same detailed table as the IBP section (dark-blue/orange, column
        # rearrange/hide), plus two APS columns (hidden by default): "YTD
        # Actuals" (actuals window) and "{cycle} YTG Fcst w.o. RO" (current-cycle
        # base forecast over the forecast window).
        _fwin = f"({filters.forecast_start:%b%y}-{filters.forecast_end:%b%y})"
        _awin = f"({filters.actual_start:%b%y}-{filters.actual_end:%b%y})"
        _render_demand_comparison_table(
            result, filters, ns="aps", allow_save=False,
            extra_cols=[DPC_DISPLAY_LABELS[DPC_COL_TOTAL_ACTUALS]],
            header_overrides={
                DPC_DISPLAY_LABELS[DPC_COL_TOTAL_ACTUALS]: "YTD Actuals",
                DPC_DISPLAY_LABELS[DPC_COL_CURRENT_PLAN_BASE]:
                    f"{filters.current_cycle} YTG Fcst w.o. RO",
            },
            period_overrides={
                DPC_DISPLAY_LABELS[DPC_COL_TOTAL_ACTUALS]: _awin,
                DPC_DISPLAY_LABELS[DPC_COL_CURRENT_PLAN_BASE]: _fwin,
            },
            # APS derives R&O from the tracker legs (not the RO Summary lookup),
            # so the "save the RO Summary" hint would be stale here.
            ro_hint=False)
        # SKU-level build-up of the APS cycle-over-cycle rows (unshifted window).
        _render_sku_cycle_drilldown(
            enriched, filters, ns="aps", shift_last_plan_window=False)
        _render_prior_month_actual_vs_fcst_table(
            prior_month_vs_fcst, prior_cycle=filters.prior_cycle,
            prior_month=filters.prior_month, ns="aps")

        # Variance drill-in — same driver tables as the IBP section (Top-5 movers
        # behind PM Actual + Base Plan by Portfolio Major × Supply Format × Brand),
        # sharing this section's EnrichedSources so it adds no extra Fabric read.
        dim_df, dim_warning = _load_demand_comparison_dim()
        if dim_warning:
            st.caption(f"⚠️ {dim_warning}")
        dim_sig = _signature_for(dim_df)
        with st.expander(
            "📋 APS / Oracle Demand Plan Comparison & Drivers Validation", expanded=False,
        ):
            _render_demand_comparison_driver_tables_cached(
                enrich_sig, filters, dim_sig, enriched, dim_df, ns="aps")


def _render_one_driver_table(
    result: DriverTableResult,
    value_col: str,
    key_prefix: str,
) -> None:
    """Render one driver table with filters, drill-down, and download."""
    table = result.table
    buckets = result.buckets
    if table is None or table.empty:
        st.info("No driver rows to display for the current selection.")
        return

    filtered = _render_driver_filters(table, buckets, key_prefix)
    # Order by IMPACT: the largest-magnitude variance rows sit on top, so the
    # biggest movers behind this metric read first.  Dimension keys break ties
    # for a stable, readable secondary order.
    filtered = filtered.sort_values(
        by=[value_col, DRV_COL_PMAJ, DRV_COL_SFMT, DRV_COL_BRAND],
        ascending=[False, True, True, True],
        key=lambda s: s.abs() if s.name == value_col else s,
        kind="mergesort",
    ).reset_index(drop=True)

    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    st.download_button(
        label=f"⬇️ Download {value_col} drivers (CSV)",
        data=driver_table_to_csv_bytes(filtered),
        file_name=f"{key_prefix}_{today}.csv",
        mime="text/csv",
        key=f"{key_prefix}_download",
        width="stretch",
    )

    column_config = {
        DRV_COL_PMAJ: st.column_config.TextColumn(DRV_COL_PMAJ, width="small"),
        DRV_COL_SFMT: st.column_config.TextColumn(DRV_COL_SFMT, width="small"),
        DRV_COL_BRAND: st.column_config.TextColumn(DRV_COL_BRAND, width="small"),
        value_col: st.column_config.NumberColumn(value_col, format="%.2f"),
    }
    for col in DRV_DRIVER_COLS:
        column_config[col] = st.column_config.TextColumn(col, width="large")

    table_height = min(35 * (len(filtered) + 1) + 38, 720)
    st.dataframe(
        filtered,
        width="stretch",
        hide_index=True,
        height=table_height,
        column_config=column_config,
    )
    _render_demand_driver_drill_down(buckets, filtered, key_prefix)


def _render_driver_filters(
    table: pd.DataFrame,
    buckets: pd.DataFrame,
    key_prefix: str,
) -> pd.DataFrame:
    """Render driver-table filters and return the filtered frame.

    Search semantics:
    - Portfolio Major / Supply Format / Brand / Portfolio Minor are
      exact-dimension filters (PMinor uses the item-level bucket frame).
    - Item Description / Customer are case-insensitive substring matches
      against the five driver text cells.
    """
    pmaj_values = sorted(v for v in table[DRV_COL_PMAJ].dropna().astype(str).str.strip().unique() if v)
    sfmt_values = sorted(v for v in table[DRV_COL_SFMT].dropna().astype(str).str.strip().unique() if v)
    brand_values = sorted(v for v in table[DRV_COL_BRAND].dropna().astype(str).str.strip().unique() if v)
    pminor_values: list[str] = []
    if buckets is not None and not buckets.empty and "pminor" in buckets.columns:
        pminor_values = sorted(
            v for v in buckets["pminor"].dropna().astype(str).str.strip().unique() if v
        )

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        selected_pmaj = st.multiselect(
            "Portfolio Major",
            options=pmaj_values,
            key=f"{key_prefix}_flt_pmaj",
            placeholder="All",
        )
    with c2:
        selected_sfmt = st.multiselect(
            "Supply Format",
            options=sfmt_values,
            key=f"{key_prefix}_flt_sfmt",
            placeholder="All",
        )
    with c3:
        selected_brand = st.multiselect(
            "Brand",
            options=brand_values,
            key=f"{key_prefix}_flt_brand",
            placeholder="All",
        )
    with c4:
        selected_pminor = st.multiselect(
            "Portfolio Minor",
            options=pminor_values,
            key=f"{key_prefix}_flt_pminor",
            placeholder="All",
        )

    q1, q2 = st.columns(2)
    with q1:
        item_query = st.text_input(
            "Item Description contains",
            key=f"{key_prefix}_flt_item",
            placeholder="e.g. Qtr 1Lb",
        ).strip().casefold()
    with q2:
        customer_query = st.text_input(
            "Customer contains",
            key=f"{key_prefix}_flt_customer",
            placeholder="e.g. Walmart",
        ).strip().casefold()

    out = table.copy()
    if selected_pmaj:
        out = out.loc[out[DRV_COL_PMAJ].isin(selected_pmaj)]
    if selected_sfmt:
        out = out.loc[out[DRV_COL_SFMT].isin(selected_sfmt)]
    if selected_brand:
        out = out.loc[out[DRV_COL_BRAND].isin(selected_brand)]

    if selected_pminor and buckets is not None and not buckets.empty:
        bucket_groups = (
            buckets.loc[buckets["pminor"].isin(selected_pminor)]
            .groupby(["pmaj", "sfmt", "brand"], dropna=False)
            .size()
            .reset_index()[["pmaj", "sfmt", "brand"]]
        )
        if bucket_groups.empty:
            out = out.iloc[0:0]
        else:
            out = out.merge(
                bucket_groups,
                left_on=[DRV_COL_PMAJ, DRV_COL_SFMT, DRV_COL_BRAND],
                right_on=["pmaj", "sfmt", "brand"],
                how="inner",
            ).drop(columns=["pmaj", "sfmt", "brand"])

    if item_query and buckets is not None and not buckets.empty:
        item_groups = (
            buckets.loc[
                buckets["item_desc"].astype(str).str.casefold().str.contains(
                    item_query, regex=False,
                )
            ]
            .groupby(["pmaj", "sfmt", "brand"], dropna=False)
            .size()
            .reset_index()[["pmaj", "sfmt", "brand"]]
        )
        if item_groups.empty:
            out = out.iloc[0:0]
        else:
            out = out.merge(
                item_groups,
                left_on=[DRV_COL_PMAJ, DRV_COL_SFMT, DRV_COL_BRAND],
                right_on=["pmaj", "sfmt", "brand"],
                how="inner",
            ).drop(columns=["pmaj", "sfmt", "brand"])

    if customer_query:
        text_blob = (
            out[list(DRV_DRIVER_COLS)]
            .fillna("")
            .astype(str)
            .agg(" | ".join, axis=1)
            .str.casefold()
        )
        out = out.loc[text_blob.str.contains(customer_query, regex=False)]

    if out.empty:
        st.caption("No rows match the current driver filters.")
    return out.reset_index(drop=True)


def _render_demand_driver_drill_down(
    buckets: pd.DataFrame,
    driver_table: pd.DataFrame,
    key_prefix: str,
) -> None:
    """Render drill-down expander for one demand-comparison driver table."""
    if buckets is None or buckets.empty or driver_table is None or driver_table.empty:
        return

    group_options = [
        (row[DRV_COL_PMAJ], row[DRV_COL_SFMT], row[DRV_COL_BRAND])
        for _, row in driver_table.iterrows()
    ]
    if not group_options:
        return

    group_labels = {
        g: f"{g[0]} → {g[1]} → {g[2]}"
        for g in group_options
    }

    with st.expander(
        "🔬 Drill into items — pick a driver bucket to see the SKUs behind it",
        expanded=False,
    ):
        sel_group = st.selectbox(
            "Group (Portfolio Major → Supply Format → Brand)",
            options=group_options,
            index=0,
            format_func=lambda g: group_labels[g],
            key=f"{key_prefix}_drill_group",
        )
        sel_pmaj, sel_sfmt, sel_brand = sel_group

        bucket_options = list_driver_buckets_for_group(
            buckets, sel_pmaj, sel_sfmt, sel_brand,
        )
        if not bucket_options:
            st.info("No driver buckets for this group on the current view.")
            return

        sel_bucket = st.selectbox(
            "Driver bucket (Customer — Account / Customer No)",
            options=bucket_options,
            index=0,
            key=f"{key_prefix}_drill_bucket",
        )

        items_df = compute_demand_driver_items(
            buckets, sel_pmaj, sel_sfmt, sel_brand, sel_bucket,
        )
        if items_df.empty:
            st.caption(
                "_No items match the current driver bucket — "
                "try widening the filters above._"
            )
            return

        cc = st.column_config
        column_config = {
            DRV_ITEM_COL_ITEM: cc.TextColumn("Item #", width="small"),
            DRV_ITEM_COL_DESC: cc.TextColumn("Description", width="medium"),
            DRV_ITEM_COL_BRAND: cc.TextColumn("Brand", width="small"),
            DRV_ITEM_COL_CUSTOMER: cc.TextColumn("Customer", width="medium"),
            DRV_ITEM_COL_CUSTOMER_ID: cc.TextColumn(
                "Customer / Account No", width="small",
            ),
            DRV_ITEM_COL_DELTA: cc.NumberColumn(format="%.2f"),
        }
        st.caption(
            f"**{len(items_df):,} item(s)** in **{group_labels[sel_group]} → "
            f"{sel_bucket}**"
        )
        st.dataframe(
            items_df,
            width="stretch",
            height=min(36 * (len(items_df) + 1) + 38, 420),
            hide_index=True,
            column_config=column_config,
        )


# Prior Month Actual vs Fcst — screenshot-styled presentation.
#   * "same dark blue on UI" = #1f4e79 (the stitched MoM chart's dark blue,
#     used just below this table), light-blue Total B2C, light-orange section
#     rows — a banded Total / Difference / % header, like the source workbook.
def _fmt_pmaf_lbs(value: object) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    return "-" if pd.isna(num) else f"{num:,.1f}"


def _fmt_pmaf_diff(value: object) -> str:
    """Accounting style: negatives in parentheses, 1 dp; NaN → '-'."""
    try:
        num = round(float(value), 1)
    except (TypeError, ValueError):
        return "-"
    if pd.isna(num):
        return "-"
    return f"({abs(num):.1f})" if num < 0 else f"{num:.1f}"


def _fmt_pmaf_pct(value: object) -> str:
    """Fraction → whole-percent (e.g. 0.03 → '3%', -0.13 → '-13%'); NaN → '-'."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    return "-" if pd.isna(num) else f"{round(num * 100):.0f}%"


def _render_pmaf_html(table: pd.DataFrame, *, prior_cycle: str, prior_month: date) -> None:
    """Render Prior Month Actual vs Fcst as a natively-foldable ``<details>`` tree.

    Same row hierarchy + folding as the detailed comparison table (click a
    Portfolio-Major row to collapse its children).  Columns: the prior-cycle
    forecast, Ordered / Shipped, their differences, and %.  Read-only (edits /
    exports happen via the CSV download).
    """
    fcst_label = f"{prior_cycle} Forecast"
    # (column id, header label, value formatter) — one flat header row (the
    # labels are self-describing, matching the detailed table's single header).
    cols: tuple[tuple[str, str, object], ...] = (
        (PMAF_COL_PRIOR_PLAN, fcst_label, _fmt_pmaf_lbs),
        (PMAF_COL_ORDERED, "Ordered", _fmt_pmaf_lbs),
        (PMAF_COL_SHIPPED, "Shipped", _fmt_pmaf_lbs),
        (PMAF_COL_ORDERED_DIFF, "Ordered Diff.", _fmt_pmaf_diff),
        (PMAF_COL_SHIPPED_DIFF, "Shipped Diff.", _fmt_pmaf_diff),
        (PMAF_COL_ORDERED_PCT, "Ordered %", _fmt_pmaf_pct),
        (PMAF_COL_SHIPPED_PCT, "Shipped %", _fmt_pmaf_pct),
    )
    rows = table.reset_index(drop=True)
    indent_flags = rows["_indent"].tolist() if "_indent" in rows.columns else []

    def _cls(row: pd.Series) -> str:
        if str(row.get("_row_id", "")) == "total_b2c":
            return "total"
        if int(row.get("_indent", 0) or 0) == 1:      # Portfolio Major (incl. Butter)
            return "section"
        if bool(row.get("_is_subtotal", False)):
            return "subtotal"
        if bool(row.get("_is_memo", False)):
            return "memo"
        return ""

    def _make_row(i: int, foldable: bool) -> tuple[str, str]:
        row = rows.iloc[i]
        cells = f'<span class="lbl">{_tri_span(foldable)}{_esc_html(row.get(DPC_COL_LABEL, ""))}</span>' + "".join(
            # formatters may emit safe HTML (coloured spans) → not escaped.
            f"<span>{fmt(row.get(cid))}</span>" for cid, _lbl, fmt in cols
        )
        return _cls(row), cells

    header = (
        f'<div class="rw hdr"><span class="lbl">{_esc_html(prior_month.strftime("%B %Y"))}</span>'
        + "".join(f"<span>{_esc_html(lbl)}</span>" for _cid, lbl, _fmt in cols)
        + "</div>"
    )
    parts = [header] + _foldable_tree_body(len(rows), indent_flags, _make_row)
    st.markdown(
        _DPC_TREE_CSS + '<div class="dpc-tree"><div class="dpc-tree-in">'
        + "".join(parts) + "</div></div>",
        unsafe_allow_html=True,
    )


def _render_prior_month_actual_vs_fcst_table(
    table: pd.DataFrame, *, prior_cycle: str, prior_month: date, ns: str = "",
) -> None:
    """Render the *Prior Month Actual vs Fcst* summary table.

    This sits below Demand Plan Comparison Summary and above the driver
    tables.  It reuses the exact same row hierarchy/indent metadata as
    the comparison table (including dynamic Butter detail rows), and is
    presented as a screenshot-styled HTML table (dark-blue banded header,
    light-blue Total B2C, light-orange section rows).
    """
    st.markdown("#### 📌 Prior Month Actual vs Fcst")
    st.caption(
        f"**{prior_cycle} Forecast** = the prior-cycle plan for the prior month, "
        "Ordered = IBP Orders (Ordered Qty lbs), "
        "Shipped = **Prior Month Actual = prior-month IBP Shipments (not orders)**.  "
        "All values are in millions of lbs."
    )
    if table is None or table.empty:
        st.info("No rows available for Prior Month Actual vs Fcst.")
        return

    # Download mirrors the on-screen table; the forecast column header carries
    # the specific cycle name (e.g. "C3 Forecast") like the display.
    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    out_df = table.drop(
        columns=[c for c in ("_row_id", "_indent", "_is_subtotal", "_is_memo")
                 if c in table.columns]
    ).rename(columns={PMAF_COL_PRIOR_PLAN: f"{prior_cycle} Forecast"}).reset_index(drop=True)
    st.download_button(
        label="⬇️ Download Prior Month Actual vs Fcst (CSV)",
        data=out_df.to_csv(index=False).encode("utf-8"),
        file_name=f"prior_month_actual_vs_fcst_{today}.csv",
        mime="text/csv",
        key=_ns_key("dpc_prior_month_vs_fcst_download", ns),
        width="stretch",
    )

    _render_pmaf_html(table, prior_cycle=prior_cycle, prior_month=prior_month)


def _render_prior_month_shipment_diagnostic(
    ibp: Optional[pd.DataFrame], prior_month: date,
) -> None:
    """Foldable, read-only prior-month shipment reconciliation.

    Decomposes the selected prior month's IBP Shipments by Portfolio Major
    × Supply Format so a raw portfolio total (e.g. "Total ESL") can be
    reconciled against the hierarchy lines above.  Self-contained: it reads
    the enriched shipments already in memory and writes nothing, so it can't
    affect any other section; no new cache layer is introduced.
    """
    with st.expander("🔎 Prior-Month Shipment Diagnostic (reconciliation)", expanded=False):
        st.caption(
            "Breaks the selected prior month's **IBP Shipments** down by "
            "**Portfolio Major × Supply Format** so you can reconcile a raw "
            "portfolio total against the hierarchy lines above.  Reminder: the "
            "**ESL** line = ESL × {Large Carton, Small Carton, Aerosol Can}; "
            "**Aseptic** (ESL × Aseptic) is a *separate* line; rows with a "
            "blank/other Supply Format or an `(unmapped)` Portfolio Major "
            "(item not in PDH) are **not** counted in any hierarchy line.  "
            "Read-only — independent of the tables above."
        )
        detail = build_prior_month_shipment_diagnostic(ibp, prior_month)
        if detail.empty:
            st.info(f"No IBP Shipments rows for {prior_month:%b %Y}.")
            return

        total_lbs = float(detail[DIAG_COL_LBS].sum())
        st.metric(
            f"Total {prior_month:%b %Y} shipments (all items)",
            f"{total_lbs:,.0f} lbs · {total_lbs / 1e6:,.2f} M",
        )

        num_lbs = st.column_config.NumberColumn(DIAG_COL_LBS, format="%.0f")
        num_mlbs = st.column_config.NumberColumn(DIAG_COL_MLBS, format="%.2f")

        rollup = (
            detail.groupby(DIAG_COL_PMAJ, as_index=False)[DIAG_COL_LBS].sum()
            .sort_values(DIAG_COL_LBS, ascending=False, ignore_index=True)
        )
        rollup[DIAG_COL_MLBS] = rollup[DIAG_COL_LBS] / 1e6
        st.markdown(
            "**By Portfolio Major** — compare a portfolio total here (e.g. Total "
            "ESL across every format) against the corresponding line above."
        )
        st.dataframe(
            rollup, use_container_width=True, hide_index=True,
            column_config={DIAG_COL_LBS: num_lbs, DIAG_COL_MLBS: num_mlbs},
        )

        st.markdown("**By Portfolio Major × Supply Format**")
        st.dataframe(
            detail, use_container_width=True, hide_index=True,
            column_config={DIAG_COL_LBS: num_lbs, DIAG_COL_MLBS: num_mlbs},
        )

        today = pd.Timestamp.utcnow().strftime("%Y%m%d")
        st.download_button(
            "⬇️ Download diagnostic (CSV)",
            data=detail.to_csv(index=False).encode("utf-8"),
            file_name=f"prior_month_shipment_diagnostic_{prior_month:%Y%m}_{today}.csv",
            mime="text/csv",
            key="dpc_prior_month_diag_dl",
        )


def _sku_dim_options(trk: Optional[pd.DataFrame], col: str) -> list[str]:
    """Sorted distinct non-blank values of a dimension column on the tracker."""
    if trk is None or getattr(trk, "empty", True) or col not in trk.columns:
        return []
    vals = trk[col].astype(str).str.strip()
    return sorted({v for v in vals.tolist() if v and v.lower() != "nan"})


def _render_sku_cycle_drilldown(
    enriched, filters: ComparisonFilters, *,
    ns: str = "", shift_last_plan_window: bool = True,
) -> None:
    """Foldable SKU-level build-up of the cycle-over-cycle table above.

    Dimension search filters (Portfolio Major / Minor / Brand / Supply Format,
    empty = all) narrow to the SKUs that compose the roll-up rows; the table is
    the leg build-up (Base + R&O legs, prior vs current cycle, + actuals → plans
    + deltas), sorted by current-cycle plan.  Native ``st.dataframe`` so columns
    sort / hide / resize for free.  ``shift_last_plan_window`` matches the
    section (IBP shifted, APS unshifted).
    """
    trk = getattr(enriched, "tracker", None)
    ibp = getattr(enriched, "ibp", None)
    with st.expander("🔬 SKU-level cycle-over-cycle drill-in", expanded=False):
        st.caption(
            "The individual **SKUs** that build up to the cycle-over-cycle rows "
            "above.  Filter by **Portfolio Major / Portfolio Minor / Brand / "
            "Supply Format** (empty = all); sorted by current-cycle plan.  Leg "
            "build-up in **millions of lbs** — Budget / RO-Summary R&O are per "
            "hierarchy row, not per SKU, so they aren't shown here."
        )
        cols = st.columns(4)
        dim_filter: dict[str, set] = {}
        for (key, label), slot in zip(
            (("pmaj", "Portfolio Major"), ("pminor", "Portfolio Minor"),
             ("brand", "Brand"), ("sfmt", "Supply Format")), cols):
            with slot:
                sel = st.multiselect(
                    label, options=_sku_dim_options(trk, key),
                    key=_ns_key(f"sku_{key}", ns), placeholder="All")
            if sel:
                dim_filter[key] = set(sel)
        sku_df = build_sku_cycle_comparison(
            trk, ibp, filters, dim_filter=dim_filter,
            shift_last_plan_window=shift_last_plan_window)
        if sku_df.empty:
            st.info("No SKUs match the current filters.")
            return
        st.caption(f"**{len(sku_df):,}** SKU(s)")
        st.dataframe(
            sku_df, use_container_width=True, hide_index=True,
            height=min(35 * (len(sku_df) + 1) + 38, 640))
        today = pd.Timestamp.utcnow().strftime("%Y%m%d")
        st.download_button(
            "⬇️ Download SKU drill-in (CSV)",
            data=sku_df.to_csv(index=False).encode("utf-8"),
            file_name=f"sku_cycle_over_cycle_{ns or 'ibp'}_{today}.csv",
            mime="text/csv", key=_ns_key("sku_cycle_dl", ns))


def _render_demand_comparison_driver_tables_cached(
    enrich_sig: tuple,
    filters: ComparisonFilters,
    dim_sig: tuple,
    enriched: EnrichedSources,
    dim_df: Optional[pd.DataFrame],
    *,
    ns: str = "",
) -> None:
    """Render the PM Actual + Base Plan driver tables (shared enrichment, cached).

    Called from inside the foldable
    ``Demand Plan Comparison & Drivers Validation`` expander in
    :func:`_render_demand_plan_comparison_fragment`.

    Both builders consume the same :class:`EnrichedSources` bundle —
    the PDH-merge happened once upstream — and each build itself is
    wrapped in :func:`_cached_demand_plan_comparison`-style cache slots,
    so repeated reruns at the same selection are essentially free.

    Each table breaks the comparison's metric down by
    (Portfolio Major × Supply Format × Brand) and surfaces the top-5
    customer/account drivers (signed, in millions of lbs).  Values are
    *deltas*, matching the comparison's PM Actual / Base Plan columns.
    """
    st.caption(
        "Top-5 movers behind **PM Actual** and **Base Plan**, by "
        "Portfolio Major × Supply Format × Brand.  Each driver shows "
        "Customer – Account/Customer No and its signed contribution "
        "in millions of lbs."
    )

    with st.spinner("Building driver tables…"):
        pm_result = _cached_pm_actual_driver_table(
            enrich_sig, filters, dim_sig, enriched, dim_df,
        )
        bp_result = _cached_base_plan_driver_table(
            enrich_sig, filters, dim_sig, enriched, dim_df,
        )

    prefix = f"{ns}_" if ns else ""
    # Base Plan variance analysis FIRST (the planner reviews the baseline move
    # before the prior-month actual variance), each ordered by impact.
    st.markdown(
        "**Base Plan drivers**  —  _driver = Customer – Party Site No "
        "(current vs prior cycle, forecast months)_"
    )
    _render_one_driver_table(bp_result, DRV_BASE_PLAN_VALUE, f"{prefix}base_plan_drivers")

    st.markdown(
        f"**PM Actual drivers**  —  _driver = Customer Name – Customer No "
        f"(prior month: {filters.prior_month.strftime('%b %Y')})_"
    )
    _render_one_driver_table(pm_result, DRV_PM_ACTUAL_VALUE, f"{prefix}pm_actual_drivers")


# ── Auto-save hooks (RO Summary + RO Comparison Output) ──────────────────────
#
# Two soft, idempotent helpers that republish the in-memory RO Summary
# Report and RO Comparison Output to Fabric.  Wired into BOTH the relevant
# build sites (Summary Report fragment rebuild, ``_ensure_summary_in_session``
# rebuild) AND the Product Line Review fragment, so the saved CSVs always
# reflect what the planner is currently seeing.  Idempotent — the signature
# guard skips the Fabric write when the in-memory frame hasn't changed.
#
# Each helper soft-fails (warning log, no UI banner) so the manual Save
# buttons in the respective sections remain the user-facing fallback when
# Fabric is temporarily unreachable.

# Session keys for the auto-save signature guards.  Kept distinct from the
# manual-Save flow so a manual click never short-circuits a later auto-save.
_SS_AUTOSAVE_RO_SR_SIG: str = "_autosave_ro_summary_sig"
_SS_AUTOSAVE_RO_CMP_SIG: str = "_autosave_ro_comparison_sig"


def _maybe_autosave_ro_summary_report(*, trigger: str) -> None:
    """Save the in-memory RO Summary Report frame to Fabric (idempotent).

    Reads :data:`_SS_SUMMARY_REPORT_DF` — the full 30-row template the
    Summary Report fragment builds in-place.  Signature guard combines
    the build-time signature with the current frame shape so planner
    edits also re-trigger a save.

    Called from:
      * :func:`_render_summary_report_fragment` — right after each rebuild.
      * :func:`_render_product_line_review_fragment` — at the end of every
        PLR render, so PLR's ``R&O`` column reads the *current* report.
    """
    report_df: pd.DataFrame | None = st.session_state.get(_SS_SUMMARY_REPORT_DF)
    if report_df is None or report_df.empty:
        return
    sig = (
        st.session_state.get(_SS_SUMMARY_REPORT_SIG),
        _signature_for(report_df),
    )
    if st.session_state.get(_SS_AUTOSAVE_RO_SR_SIG) == sig:
        return

    try:
        with st.spinner(
            f"Auto-saving `RO_Summary_Report.csv` to Microsoft Fabric ({trigger})…"
        ):
            blob_path = save_ro_summary_report(report_df)
    except RoSummaryReportError as exc:
        # Soft fail — log + leave the in-session manual Save button as the
        # explicit fallback.  We deliberately do NOT throw a red banner so
        # transient Fabric blips don't make the rest of the page look broken.
        logger.warning("Auto-save of RO_Summary_Report failed: %s", exc)
        return
    except Exception as exc:  # noqa: BLE001 — last-resort safety net
        logger.exception("Unexpected error auto-saving RO_Summary_Report.")
        return

    st.session_state[_SS_AUTOSAVE_RO_SR_SIG] = sig
    logger.info(
        "Auto-saved RO_Summary_Report.csv → Files/%s (trigger=%s)",
        blob_path, trigger,
    )


def _maybe_autosave_ro_comparison_output(*, trigger: str) -> None:
    """Save the in-memory comparison frame to Fabric (idempotent).

    Reads :data:`_SS_SUMMARY_DF` — the comparison summary the editor /
    Summary Report / PLR all consume.  The history fingerprint sidecar is
    deliberately NOT touched here (only :func:`regenerate_comparison_output`
    writes that): this hook republishes the **current view**, which may
    differ from RO_History after a Prior/LE month change.

    Called from:
      * :func:`_ensure_summary_in_session` — right after each rebuild.
      * :func:`_render_product_line_review_fragment` — at the end of every
        PLR render, so downstream consumers see the same frame.
      * The manual "💾 Save" button in the RO Comparison section.
    """
    summary_df: pd.DataFrame | None = st.session_state.get(_SS_SUMMARY_DF)
    if summary_df is None or summary_df.empty:
        return
    sig = (
        st.session_state.get(_SS_MONTHS_SIG),
        _signature_for(summary_df),
    )
    if st.session_state.get(_SS_AUTOSAVE_RO_CMP_SIG) == sig:
        return

    try:
        with st.spinner(
            f"Auto-saving `RO_Comparison_Output.csv` to Microsoft Fabric "
            f"({trigger})…"
        ):
            blob_path = save_ro_comparison_output(summary_df)
    except RoComparisonError as exc:
        logger.warning("Auto-save of RO_Comparison_Output failed: %s", exc)
        return
    except Exception as exc:  # noqa: BLE001 — last-resort safety net
        logger.exception("Unexpected error auto-saving RO_Comparison_Output.")
        return

    st.session_state[_SS_AUTOSAVE_RO_CMP_SIG] = sig
    logger.info(
        "Auto-saved RO_Comparison_Output.csv → Files/%s (trigger=%s)",
        blob_path, trigger,
    )


# ── Product Line Review ───────────────────────────────────────────────────────
#
# Portfolio Majors listed here render last (tables + charts) so the
# higher-traffic Butter / Cultured / etc. sections stay near the top.
_PLR_PORTFOLIO_MAJOR_LAST: frozenset[str] = frozenset({
    "Bulk Fluid",
    "Cheese",
    "Milk Powders",
    "Whey Powders",
})


def _sort_plr_portfolio_majors(pmaj_options: list[str]) -> list[str]:
    """Return *pmaj_options* with deprioritized Portfolio Majors at the end."""
    primary = sorted(p for p in pmaj_options if p not in _PLR_PORTFOLIO_MAJOR_LAST)
    trailing = sorted(p for p in pmaj_options if p in _PLR_PORTFOLIO_MAJOR_LAST)
    return primary + trailing


# Per-Portfolio-Major hierarchical table + Full-Year chart.  The section is
# available as soon as the underlying Fabric sources exist — there is **no**
# Generate gate and **no** dependency on the Demand Summary load above.  The
# planner picks four common date filters once, then each Portfolio Major
# table gets its own (cascaded, multi-select) Supply Format / Brand picker.
#
# Auto-save hooks live further up the file:
#   * RO Summary Report             — :func:`_maybe_autosave_ro_summary_report`
#   * RO Comparison Output          — :func:`_maybe_autosave_ro_comparison_output`
# Both are called whenever their corresponding "table" is rebuilt; both keep
# their existing manual Save buttons + warnings (planner request).

_PLR_FMT_MONTH = lambda d: d.strftime("%b %Y")  # noqa: E731
_PLR_CHART_HEIGHT = 320
_PLR_CY_BEGIN_KEY = "plr_cy_begin_month"


def _render_product_line_review() -> None:
    """Foldable Product Line Review section (bottom of page)."""
    with st.expander("📋 Product Line Review", expanded=False):
        st.caption(
            "**One table + one chart per Portfolio Major.**  "
            "📌 **Current Cycle Plan = Base Plan + R&O** — both are included "
            "(closed current-fiscal-year months are shown as **actual "
            "shipments** instead).  Hierarchical "
            "**Brand → Portfolio Minor → Supply Format** roll-up with "
            "customer-detail rows (light blue) aggregated by **Corporate "
            "Group**; volumes in **millions of lbs**.  Run-rate columns "
            "still use IBP **Orders** trailing windows ending at **CY "
            "Month**.  The unified source `demand_order_item_customer.csv` "
            "is rebuilt on every render by (1) keeping the Base Plan + "
            "R&O rows from `qry_demand_item_customer_detail.csv` outside "
            "the CY-Actual months, (2) replacing those CY-Actual months "
            "with IBP **Shipments** as `Forecast Type = \"Actual\"`, and "
            "(3) stamping `Customer No` + `Corporate Group` per row: "
            "Base Plan via `dp_dimshiptosites` → `dp_dimcustomernames`, "
            "Actual via shipments' Customer No → `dp_dimcustomernames`, "
            "R&O via fuzzy match against `dp_dimcustomernames`.  The "
            "rebuilt CSV is re-published to Fabric on every render."
        )
        if not fabric_signin_widget.is_fabric_signed_in():
            fabric_signin_widget.render()
            return
        _render_product_line_review_fragment()


# Session keys for the demand-order-item-customer auto-save signature guard.
_SS_AUTOSAVE_DOIC_SIG: str = "_autosave_demand_order_item_customer_sig"


def _maybe_autosave_demand_order_item_customer(
    df: pd.DataFrame, *, trigger: str,
) -> None:
    """Save ``demand_order_item_customer.csv`` to Fabric (idempotent).

    Mirrors :func:`_maybe_autosave_ro_summary_report` — the signature
    guard short-circuits repeated writes of the same enriched frame so
    a normal filter-click rerun never re-uploads to Fabric.  The save
    only fires when the in-memory frame's shape signature changes
    (planner spec: "save whenever a new file is created").
    """
    if df is None or df.empty:
        return
    sig = _signature_for(df)
    if st.session_state.get(_SS_AUTOSAVE_DOIC_SIG) == sig:
        return
    try:
        with st.spinner(
            "Auto-saving `demand_order_item_customer.csv` to Microsoft "
            f"Fabric ({trigger})…"
        ):
            blob_path = save_demand_order_item_customer(df)
    except DemandItemCustomerError as exc:
        # Soft fail — same playbook as the other PLR auto-saves so a
        # transient Fabric blip doesn't make the rest of the page look
        # broken.  Planners can re-trigger by changing a filter.
        logger.warning(
            "Auto-save of demand_order_item_customer.csv failed: %s", exc,
        )
        return
    except Exception:  # noqa: BLE001 — last-resort safety net
        logger.exception(
            "Unexpected error auto-saving demand_order_item_customer.csv."
        )
        return

    st.session_state[_SS_AUTOSAVE_DOIC_SIG] = sig
    logger.info(
        "Auto-saved demand_order_item_customer.csv → Files/%s (trigger=%s)",
        blob_path, trigger,
    )


@st.fragment
def _render_product_line_review_fragment() -> None:
    """Eagerly load Fabric sources + render one table+chart per PM.

    Sourcing model (planner spec, June 2026 cycle)
    ----------------------------------------------
    1. ``qry_demand_item_customer_detail.csv``  — wide month × item ×
       customer detail (Base Plan / R&O / placeholder rows).
    2. ``qry_pdh.csv``                          — dim attribution
       (joined on Item No).
    3. ``dbo.IBP Shipments``                    — Shipped Qty lbs;
       months derived from the CY Actual Months (months in CY Full
       Year but outside CY YTG).  These rows REPLACE the detail-CSV
       rows in the same months and are emitted as
       ``Forecast Type = "Actual"``.
    4. ``dbo.IBP Orders``                       — Ordered Qty lbs;
       months derived from the common filters; feeds the run-rate /
       PY columns ONLY (not the unified CSV).
    5. ``dbo.dp_dimshiptosites``                — translates a Base
       Plan row's Party Site Number into a ``customer_num`` so the
       Corporate Group attach can hit ``dp_dimcustomernames``.
    6. ``dbo.dp_dimcustomernames``              — single source of
       truth for Corporate Group.  Exact ``customer_num`` join for
       Actual + Base Plan rows; fuzzy ``Customer Name`` match for
       R&O rows; exact ``customer_num`` join for the IBP Orders
       run-rate side.

    Output
    ------
    The enriched frame is saved to
    ``Files/RO Tracking/Demand Plan/demand_order_item_customer.csv``
    on every render (idempotent — only writes when the in-memory frame
    signature changes).  All filter dropdowns, hierarchy leaves and
    chart series read from that same in-memory frame.

    Performance contract
    --------------------
    * Each fetcher caches at the source layer (TTL + ETag); the repeated
      calls here are cheap.
    * Enrichment + fuzzy join + aggregation happen ONCE per render via
      :func:`_cached_prepare_plr_inputs`, keyed on shape signatures so
      filter clicks that don't touch the underlying data short-circuit
      to a microsecond cache hit.
    * Each Portfolio Major section renders inside its OWN ``@st.fragment``
      (:func:`_render_plr_pm_section`) so a Supply Format / Brand pick
      on one PM no longer rebuilds every other PM.
    """
    # 1) Load raw sources up-front.  Each fetcher caches at the source
    #    layer (60 min for CSVs, 15 min for dim tables) so repeat fragment
    #    runs reuse the cached payloads.
    try:
        with st.spinner("Reading PLR sources from Microsoft Fabric…"):
            detail_df = fetch_demand_item_customer_detail()
    except DemandItemCustomerError as exc:
        st.error(f"❌ Could not load Product Line Review sources.\n\n{exc}")
        return

    if detail_df is None or detail_df.empty:
        st.info(
            "ℹ️ `qry_demand_item_customer_detail.csv` is empty — nothing "
            "to render."
        )
        return

    pdh_df = _load_demand_comparison_pdh()

    # Customer-names dim is the single source of truth for Corporate
    # Group as of the June 2026 planner spec.  Ship-to-sites is the
    # bridge that lets Base Plan rows (Party Site Number only) reach
    # that lookup.  Both fetchers are independently cached; failures
    # are non-fatal — the build helpers fall back to Customer Name
    # so a temporary auth blip never bricks the section.
    try:
        customer_names_dim = fetch_dp_dimcustomernames_df()
    except CustomerDimsError as exc:
        logger.warning("dp_dimcustomernames load failed: %s", exc)
        customer_names_dim = None
    try:
        ship_to_sites_dim = fetch_dimshiptosites_df()
    except ShipToSitesSourceError as exc:
        logger.warning("dp_dimshiptosites load failed: %s", exc)
        ship_to_sites_dim = None
    # dp_dimplantosites bridges party_site's plan_to_code → the customer_num
    # that actually matches dp_dimcustomernames (dp_dimshiptosites' own
    # customer_num does not), so Base Plan rows resolve to a real corp group.
    try:
        plantosites_dim = fetch_dp_dimplantosites_df()
    except ShipToSitesSourceError as exc:
        logger.warning("dp_dimplantosites load failed: %s", exc)
        plantosites_dim = None

    # 2) Common filters (apply to every PM table & chart).  Months come
    #    from the detail CSV's ``Start of Month`` so the planner can only
    #    pick months that actually have rows.
    common = _render_plr_common_filters(detail_df)
    errors = validate_common_filters(common)
    if errors:
        for msg in errors:
            st.error(f"❌ {msg}")
        return

    # 3) IBP pulls — month union covers BOTH the enrichment (CY Actual
    #    months sourced from SHIPMENTS) AND the table's PY / run-rate /
    #    FY columns (sourced from ORDERS).  Done ONCE per render;
    #    cached per month set in the slim fetcher.
    ibp_months = collect_ibp_months_for_common(common)
    ibp_orders_df, _warn_o = _load_demand_comparison_ibp_orders(months=ibp_months)
    ibp_shipments_df, _warn_s = _load_demand_comparison_ibp(months=ibp_months)

    # 4) CY Actual Months (months in CY Full Year but outside CY YTG)
    #    drive the enrichment swap — these get sourced from IBP
    #    Shipments instead of the detail CSV.
    cy_actual_months = compute_cy_actual_months(
        cy_full_year_months=_plr_cy_full_year_months(common.cy_begin_month),
        cy_ytg_start=common.cy_ytg_start,
        cy_ytg_end=common.cy_ytg_end,
    )

    # 5) Enrich + aggregate the four PLR input frames.  Cached on
    #    shape-signatures + the CY Actual Months tuple so filter reruns
    #    that don't touch the underlying data short-circuit to a cache
    #    hit instead of re-running the fuzzy join.
    (
        enriched_df, orders_agg, demand_agg, chart_agg,
        orders_stats, plr_warnings,
    ) = _cached_prepare_plr_inputs(
        _signature_for(detail_df),
        _signature_for(ibp_orders_df),
        _signature_for(ibp_shipments_df),
        _signature_for(pdh_df),
        _signature_for(customer_names_dim),
        _signature_for(ship_to_sites_dim),
        _signature_for(plantosites_dim),
        cy_actual_months,
        _detail_df=detail_df,
        _ibp_orders_df=ibp_orders_df,
        _ibp_shipments_df=ibp_shipments_df,
        _pdh_df=pdh_df,
        _customer_names_dim=customer_names_dim,
        _ship_to_sites_dim=ship_to_sites_dim,
        _plantosites_dim=plantosites_dim,
    )

    for msg in plr_warnings:
        st.warning(msg)

    # Planner spec: always show how many IBP Orders rows were dropped
    # during PDH enrichment (variable mismatch / unparseable month) so
    # data-quality regressions surface immediately.  The banner is
    # informational when zero rows dropped, otherwise a yellow warning.
    _render_orders_drop_banner(orders_stats)

    # 6) Auto-save the enriched CSV to Fabric (idempotent — guarded by
    #    the in-memory frame signature).  Planner spec: "the output
    #    should always be automatically saved whenever a new file is
    #    created".
    _maybe_autosave_demand_order_item_customer(enriched_df, trigger="PLR build")

    # 7) PM dropdown options come from the saved CSV (planner spec).
    fv = list_filter_values_from_demand(enriched_df)
    pmaj_options = fv.get("portfolio_major", [])
    if not pmaj_options:
        st.warning(
            "No Portfolio Major values found in "
            "`demand_order_item_customer.csv` — cannot build any "
            "Product Line Review section."
        )
        return

    # 8) Per-PM render.  Each section is its OWN @st.fragment so changes
    #    to one PM's sub-filters do not rerun the others.  Bulk Fluid,
    #    Cheese, Milk Powders, and Whey Powders render last (tables +
    #    charts) per planner preference.
    for pmaj in _sort_plr_portfolio_majors(pmaj_options):
        _render_plr_pm_section(
            pmaj=pmaj,
            common=common,
            pdh_df=pdh_df,
            enriched_df=enriched_df,
            orders_agg=orders_agg,
            demand_agg=demand_agg,
            chart_agg=chart_agg,
        )

    # 9) Auto-save the in-memory RO Summary + RO Comparison snapshots to
    #    Fabric so the freshly rendered PLR tables reference what's
    #    currently saved.  Idempotent within a session.
    _maybe_autosave_ro_summary_report(trigger="PLR build")
    _maybe_autosave_ro_comparison_output(trigger="PLR build")


# ─────────────────────────────────────────────────────────────────────────────
# PLR input prep (enrichment + dim-grain aggregation) — cached
# ─────────────────────────────────────────────────────────────────────────────
#
# The four output frames below are the per-PM loop's only inputs.
# Caching on shape-signatures means filter clicks (which don't change
# the underlying data) hit the cache instead of re-running the fuzzy
# join across the whole frame.  The aggregation step collapses to dim-
# grain so each per-PM mask + groupby is bound by dim cardinality, not
# raw shape.

@st.cache_data(ttl=_CACHE_TTL_SECONDS_OUTPUTS, show_spinner=False)
def _cached_prepare_plr_inputs(
    detail_sig: tuple,
    orders_sig: tuple,
    shipments_sig: tuple,
    pdh_sig: tuple,
    customer_names_sig: tuple,
    ship_to_sites_sig: tuple,
    plantosites_sig: tuple,
    cy_actual_months: tuple,
    *,
    _detail_df: Optional[pd.DataFrame],
    _ibp_orders_df: Optional[pd.DataFrame],
    _ibp_shipments_df: Optional[pd.DataFrame],
    _pdh_df: Optional[pd.DataFrame],
    _customer_names_dim: Optional[pd.DataFrame],
    _ship_to_sites_dim: Optional[pd.DataFrame],
    _plantosites_dim: Optional[pd.DataFrame],
) -> tuple[
    pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame,
    tuple[int, int], tuple[str, ...],
]:
    """Build the enriched demand frame + every aggregated frame the PLR uses.

    Returns
    -------
    enriched_df
        Saved-CSV-shape frame (one row per detail row OR synthesised
        Actual row), with the per-forecast-type-resolved ``Corporate
        Group`` column attached.  This is the file written to Fabric.
    orders_agg
        IBP Orders + PDH dims + Corporate Group, used for the
        PY / run-rate columns on the table.  Already grouped to
        dim-grain.
    demand_agg
        The enriched CSV in long shape, grouped to the dim-grain the
        table builder consumes for the Current-Cycle-Plan columns.
    chart_agg
        The same long frame aggregated to ``(pmaj, sfmt, brand, month)``
        for the Full-Year chart.
    orders_stats
        ``(n_orders_in, n_orders_enriched)`` — surfaced as a visible
        banner so the planner sees drops due to variable mismatches
        in the IBP Orders enrichment immediately.
    warnings
        Soft warnings produced by the enrichment (dim table missing,
        no fuzzy matches, etc.) — surfaced as captions on the page
        outside the cached call.

    A plain tuple is returned (not a dataclass) for the same reason
    the rest of the page uses tuples in ``@st.cache_data`` callers:
    the cache value is pickled on round-trip, and a custom class can
    get bound to a stale class object when Streamlit's file watcher
    reloads the module.
    """
    # 1. Enrich the saved-CSV frame.  Flow (planner spec, June 2026
    #    cycle):
    #       filter detail (drop CY-Actual months)
    #         ⊕ synthesise Actual rows from IBP SHIPMENTS
    #         → back-fill Customer No on Base Plan rows via
    #           dp_dimshiptosites.plan_to_code → dp_dimplantosites.customer_num
    #         → resolve Corporate Group per row by Forecast Type
    #           (exact customer_num for Actual + Base Plan; fuzzy
    #           Customer Name for R&O).
    #    This is the file written to Fabric.
    build = build_demand_order_item_customer(
        detail_df=_detail_df,
        shipments_df=_ibp_shipments_df,
        pdh_df=_pdh_df,
        customer_names_dim=_customer_names_dim,
        ship_to_sites_dim=_ship_to_sites_dim,
        plantosites_df=_plantosites_dim,
        cy_actual_months=cy_actual_months,
    )

    # 2. Orders side: PDH-enrich + attach Corporate Group via the
    #    same dp_dimcustomernames table (planner spec retired the
    #    legacy dp_dimcorporategroup).  We track row counts before
    #    and after enrichment so the page can render a visible drop
    #    banner — `enrich_ibp_orders_df` silently drops rows whose
    #    Month is unparseable or whose required columns are missing.
    #
    #    The canonical map produced by the unified-CSV build is
    #    passed through so the Orders side ends up with the SAME
    #    spelling per Corporate Group casefold key as the unified
    #    frame.  This is what stops the table from splitting
    #    "Associated Foods" and "ASSOCIATED FOODS" into two customer
    #    rows that each double-count the same casefold-equivalent
    #    Orders pounds.
    n_orders_in = int(len(_ibp_orders_df)) if _ibp_orders_df is not None else 0
    orders_enriched = enrich_ibp_orders_df(_ibp_orders_df, _pdh_df)
    n_orders_enriched = int(len(orders_enriched))
    orders_with_cg = attach_corporate_group_to_orders(
        orders_enriched, _customer_names_dim,
        canonical_map=dict(build.canonical_corp_group_map),
    )

    # 3. Long-format saved-CSV frame for the table builder.
    demand_long = prepare_demand_long_for_plr(build.df, _pdh_df)

    return (
        build.df,
        aggregate_orders_for_plr(orders_with_cg),
        aggregate_base_plan_for_plr(demand_long),
        aggregate_total_demand_for_plr(demand_long),
        (n_orders_in, n_orders_enriched),
        build.warnings,
    )


def _render_orders_drop_banner(orders_stats: tuple[int, int]) -> None:
    """Render the always-visible IBP Orders enrichment drop banner.

    *orders_stats* = ``(n_in, n_enriched)``.  Per planner spec (Q3),
    the banner is shown unconditionally so data-quality regressions
    surface immediately — yellow when drops > 0, informational caption
    otherwise.  The most common drop reasons (per the inner
    ``_enrich_ibp`` helper) are: missing required columns (Item No /
    Month / Ordered Qty lbs), or rows with an unparseable Month value.
    """
    n_in, n_enriched = orders_stats
    n_dropped = max(0, n_in - n_enriched)
    if n_in == 0:
        st.caption("ℹ️ IBP Orders not pulled — run-rate columns will be zero.")
        return
    if n_dropped == 0:
        st.caption(
            f"ℹ️ IBP Orders enrichment: {n_enriched:,} of {n_in:,} rows "
            "kept (0 dropped — clean variable match)."
        )
        return
    pct = (n_dropped / n_in) * 100.0 if n_in else 0.0
    st.warning(
        f"⚠️ IBP Orders enrichment dropped **{n_dropped:,} of {n_in:,}** rows "
        f"({pct:.1f}%) due to variable mismatch.  Most common causes: missing "
        f"`Item No` / `Month` / `Ordered Qty lbs`, or an unparseable `Month` "
        f"value.  The remaining {n_enriched:,} rows feed the Orders columns."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Per-PM section — its own fragment so sub-filter clicks scope tightly
# ─────────────────────────────────────────────────────────────────────────────

@st.fragment
def _render_plr_pm_section(
    *,
    pmaj: str,
    common: ProductLineReviewCommonFilters,
    pdh_df: Optional[pd.DataFrame],
    enriched_df: pd.DataFrame,
    orders_agg: pd.DataFrame,
    demand_agg: pd.DataFrame,
    chart_agg: pd.DataFrame,
) -> None:
    """Render one Portfolio Major's header + sub-filters + table + chart.

    Wrapping this in ``@st.fragment`` means a planner toggling the
    Supply Format multiselect under "Butter" only re-runs **this** call —
    the other PMs keep their existing renders + filter state.
    """
    st.markdown("---")
    st.markdown(f"### Portfolio Major: {pmaj}")
    sub = _render_plr_pm_sub_filters(enriched_df, pdh_df, pmaj)
    filters = resolve_filters(common, pmaj, sub)

    result = build_product_line_review_table(
        orders_enriched=orders_agg,
        base_long=demand_agg,
        filters=filters,
    )
    for msg in result.warnings:
        st.warning(msg)
    _render_plr_table_for_pm(result, filters)

    chart_data = build_full_year_chart_data(
        total_demand_long=chart_agg,
        portfolio_major=pmaj,
        sub=sub,
        cy_begin_month=common.cy_begin_month,
    )
    _render_plr_chart_for_pm(chart_data, pmaj)


# ── PLR common filters ────────────────────────────────────────────────────────

def _render_plr_common_filters(
    detail_df: pd.DataFrame,
) -> ProductLineReviewCommonFilters:
    """Render the four filters shared across every Portfolio Major table.

    *detail_df* is consulted only to seed the dropdown month list — every
    distinct ``Start of Month`` in the detail CSV is offered, so the
    planner can pick any month that actually has rows.  PY counterparts
    are always derived (CY − 12) and never picked.
    """
    from data_sources.demand_plan_comparison import _vectorised_start_of_month
    from data_sources.demand_summary import _resolve_column

    month_col = _resolve_column(
        detail_df, ("Start of Month", "Start Of Month", "Month"),
    )
    if not month_col:
        st.warning(
            "Detail file has no parseable **Start of Month** column — "
            "falling back to today's month for every picker."
        )
        return ProductLineReviewCommonFilters(
            cy_month=date.today().replace(day=1),
            cy_begin_month=date.today().replace(day=1),
            cy_ytg_start=date.today().replace(day=1),
            cy_ytg_end=date.today().replace(day=1),
        )

    months = sorted({
        m for m in _vectorised_start_of_month(detail_df[month_col]).tolist()
        if m is not None
    })
    if not months:
        st.warning("No parseable months in `qry_demand_item_customer_detail.csv`.")
        return ProductLineReviewCommonFilters(
            cy_month=date.today().replace(day=1),
            cy_begin_month=date.today().replace(day=1),
            cy_ytg_start=date.today().replace(day=1),
            cy_ytg_end=date.today().replace(day=1),
        )

    def _idx(target: date, fallback: int) -> int:
        return months.index(target) if target in months else fallback

    last = len(months) - 1
    cy_m_idx = _idx(date(2026, 5, 1), last)
    cy_ytg_s_idx = _idx(date(2026, 5, 1), cy_m_idx)
    cy_ytg_e_idx = _idx(date(2027, 3, 1), last)

    st.markdown("**Common filters** _(apply to every Portfolio Major table below)_")

    row_a = st.columns(2)
    with row_a[0]:
        cy_month = st.selectbox(
            "CY Month", options=months, index=cy_m_idx,
            key="plr_cy_month", format_func=_PLR_FMT_MONTH,
            help="PY Month is derived automatically (CY Month − 12 months).",
        )

    # CY Begin options derive from CY Month — a 12-month arithmetic window
    # ``[CY Month − 11, CY Month]`` so the dropdown always has 12 entries.
    cy_begin_options = eligible_cy_begin_months(cy_month)
    if (
        _PLR_CY_BEGIN_KEY in st.session_state
        and st.session_state[_PLR_CY_BEGIN_KEY] not in cy_begin_options
    ):
        # Planner's previous pick fell out of the new window — reset rather
        # than throw a Streamlit "default not in options" warning.
        del st.session_state[_PLR_CY_BEGIN_KEY]
    with row_a[1]:
        cy_begin = st.selectbox(
            "CY Begin Month", options=cy_begin_options,
            index=0,  # oldest month in the window → forward-looking FY view
            key=_PLR_CY_BEGIN_KEY, format_func=_PLR_FMT_MONTH,
            help=(
                f"First month of the 12-month Full Year window — selectable "
                f"between {_PLR_FMT_MONTH(cy_begin_options[0])} and "
                f"{_PLR_FMT_MONTH(cy_begin_options[-1])}."
            ),
        )

    st.caption("**CY YTG** (base plan; PY YTG is automatically CY YTG − 12 months)")
    row_b = st.columns(2)
    with row_b[0]:
        cy_ytg_start = st.selectbox(
            "CY YTG Begin", options=months, index=cy_ytg_s_idx,
            key="plr_cy_ytg_start", format_func=_PLR_FMT_MONTH,
        )
    with row_b[1]:
        cy_ytg_end = st.selectbox(
            "CY YTG End", options=months, index=cy_ytg_e_idx,
            key="plr_cy_ytg_end", format_func=_PLR_FMT_MONTH,
        )

    common = ProductLineReviewCommonFilters(
        cy_month=cy_month,
        cy_begin_month=cy_begin,
        cy_ytg_start=cy_ytg_start,
        cy_ytg_end=cy_ytg_end,
    )
    # Derived caption — gives the planner a one-line confirmation of the
    # PY windows in play (since they're not pickable).
    py_month = add_months(cy_month, -12)
    py_ytg_start = add_months(cy_ytg_start, -12)
    py_ytg_end = add_months(cy_ytg_end, -12)
    st.caption(
        f"📌 Derived · PY Month **{_PLR_FMT_MONTH(py_month)}**  ·  "
        f"PY YTG **{_PLR_FMT_MONTH(py_ytg_start)} – {_PLR_FMT_MONTH(py_ytg_end)}**"
    )
    return common


# ── PLR per-PM sub-filters (Supply Format + Brand, multi-select) ─────────────

def _render_plr_pm_sub_filters(
    enriched_df: pd.DataFrame,
    pdh_df: Optional[pd.DataFrame],
    portfolio_major: str,
) -> ProductLineReviewSubFilters:
    """Render the two per-PM multiselects.  Empty selection = include all.

    Sources (planner spec, June 2026):
    * **Supply Format** options come from the enriched
      ``demand_order_item_customer`` frame, cascaded on Portfolio
      Major — so the planner only sees formats that actually have rows
      in the current cycle.
    * **Brand** options come from PDH (the saved CSV has no Brand
      column).  Same item-description Branded/Private rule the rest of
      the page uses.
    """
    sfmt_options = list_filter_values_for_pmaj_from_demand(
        enriched_df, portfolio_major,
    ).get("supply_format", [])
    brand_options = (
        list_pdh_filter_values_for_pmaj(pdh_df, portfolio_major).get("brand", [])
        or [BRAND_BRANDED, BRAND_PRIVATE]
    )

    # Per-PM widget keys so each section keeps its OWN selection — picking
    # "Print" under Butter must not bleed into the Cultured table.
    pm_key = portfolio_major.lower().replace(" ", "_")

    cols = st.columns(2)
    with cols[0]:
        supply_formats = tuple(st.multiselect(
            "Supply Format",
            options=sfmt_options,
            default=[],
            key=f"plr_sfmt_{pm_key}",
            help=(
                "Leave empty to include every Supply Format in this "
                "Portfolio Major.  Options come from "
                "`demand_order_item_customer.csv` filtered to "
                f"**{portfolio_major}**."
            ),
        ))
    with cols[1]:
        brands = tuple(st.multiselect(
            "Brand",
            options=brand_options,
            default=[],
            key=f"plr_brand_{pm_key}",
            help=(
                "Leave empty to include both Branded and Private.  "
                "Source: first two characters of `Item Description` "
                "in `qry_pdh.csv`."
            ),
        ))

    return ProductLineReviewSubFilters(
        supply_formats=supply_formats, brands=brands,
    )


# ── PLR table renderer ────────────────────────────────────────────────────────

def _render_plr_table_for_pm(
    result: ProductLineReviewResult,
    filters: ProductLineReviewFilters,
) -> None:
    """Render one PM's hierarchical table with dynamic column headers."""
    table = result.table
    if table is None or table.empty:
        st.info(
            f"No rows for Portfolio Major **{filters.portfolio_major}** "
            "under the current Supply Format / Brand selection."
        )
        return

    indents = table[COL_INDENT].tolist()
    labels = table[COL_ROW_LABEL].tolist()
    is_customer = table[COL_IS_CUSTOMER].tolist()

    display = table.drop(columns=[COL_INDENT, COL_IS_CUSTOMER]).copy()
    display[COL_ROW_LABEL] = [
        ("\u00a0\u00a0" * indent) + label
        for indent, label in zip(indents, labels)
    ]

    # Build the rename map FROM the dynamic display groups so the CM headers
    # echo the active PY / CY month (e.g. ``Orders – May 2025``).
    display_groups = build_display_groups(filters)
    rename: dict[str, str] = {COL_ROW_LABEL: "Pounds in millions"}
    col_order: list[str] = ["Pounds in millions"]
    for _group, cols in display_groups:
        for col_id, label in cols:
            rename[col_id] = label
            col_order.append(label)
    display = display.rename(columns=rename)
    display = display.loc[:, [c for c in col_order if c in display.columns]]
    display = display.reset_index(drop=True)

    def _style_row(row: pd.Series) -> list[str]:
        idx = int(row.name)
        if labels[idx] in _PLR_HIGHLIGHT_LABELS:
            return [_ROW_STYLE_HIGHLIGHT_ORANGE_BOLD] * len(row)
        if is_customer[idx]:
            return [_ROW_STYLE_PLR_CUSTOMER] * len(row)
        return [""] * len(row)

    st.dataframe(
        display.style.apply(_style_row, axis=1),
        width="stretch",
        hide_index=True,
    )


# ── PLR Full-Year chart renderer ─────────────────────────────────────────────

def _render_plr_chart_for_pm(
    data: FullYearChartData, portfolio_major: str,
) -> None:
    """Render the CY FY + NY FY Plotly line chart for one Portfolio Major.

    Y-axis units are **raw lbs** (per planner request — matches the
    ``qry_total_item_level_demand`` viewer the screenshot was taken from).
    X-axis is the fiscal-year month position (Apr = 1 … Mar = 12) shared
    by both series so the planner can compare same-month-of-FY YoY.
    """
    if not data.series:
        st.caption(
            f"_No `qry_total_item_level_demand` rows for "
            f"**{portfolio_major}** under the current sub-filters._"
        )
        return

    fig = go.Figure()
    # Two-series palette — same dark-blue / orange pairing as the rest of
    # the Demand Summary chart so the page reads cohesively.
    palette = ("#1f4e79", "#ed7d31")
    for series, colour in zip(data.series, palette):
        # Decorate the legend label with the actual calendar span so the
        # planner can tell the two fiscal years apart at a glance
        # (otherwise both legends are just "FY 2027" / "FY 2028").
        span = ""
        if series.months:
            span = (
                f"  ({series.months[0]:%b %Y} – "
                f"{series.months[-1]:%b %Y})"
            )
        legend_label = f"{series.label}{span}"
        fig.add_trace(go.Scatter(
            x=list(data.fy_month_labels),
            y=list(series.values_lbs),
            name=legend_label,
            mode="lines+markers",
            line=dict(color=colour, width=2.5),
            marker=dict(size=7),
            hovertemplate=(
                "<b>%{x}</b><br>" + series.label
                + ": %{y:,.0f} lbs<extra></extra>"
            ),
        ))

    fig.update_layout(
        title=dict(
            text="<b>Total Plan LE in Lbs</b>",
            x=0.02, xanchor="left", font=dict(size=14),
        ),
        # Larger top margin so the legend sits cleanly ABOVE the plot
        # area where it's always visible (the prior below-chart position
        # got clipped by the surrounding st.expander on smaller screens).
        height=_PLR_CHART_HEIGHT,
        margin=dict(l=50, r=20, t=70, b=40),
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.02,   # sits above the plotting area
            xanchor="left", x=0.0,
            font=dict(size=13),         # bumped from default ~10
            bgcolor="rgba(255,255,255,0.85)",
            bordercolor="#cccccc",
            borderwidth=1,
        ),
        xaxis=dict(title=None, tickangle=0, tickfont=dict(size=12)),
        yaxis=dict(
            title="Lbs", rangemode="tozero",
            tickformat=",", tickfont=dict(size=11),
        ),
        hovermode="x unified",
    )
    st.plotly_chart(
        fig, use_container_width=True, theme=None,
        # Stable key so re-renders don't churn the chart widget.
        key=f"plr_chart_{portfolio_major}",
    )


# ── Plan Lift Analysis ────────────────────────────────────────────────────────
#
# Independent of every RO calculation: this section reads its own Fabric
# sources (IBP Shipments, dp_factscurrentaps, dp_dimitems,
# dp_dimcustomernames, dp_dimcalendar), computes "YoY Lift%" entirely in
# Streamlit, and renders ABOVE RO Comparison so it shares no session
# state with the RO workflow.  The metric itself lives in
# :mod:`data_sources.plan_lift`; this layer only orchestrates caching,
# slicers and charting.

_PLAN_LIFT_CHART_HEIGHT = 380

# Combo-slicer display order + human labels.  Keys are the internal
# dim-column names produced by the plan_lift builder (see ``SLICER_DIMS``).
_PLAN_LIFT_SLICER_ORDER: tuple[str, ...] = (
    "corporate_group", "portfolio_major", "supply_format", "size",
    "taxonomy", "brand_category", "brand_name", "portfolio_minor",
    "item_code",
)
_PLAN_LIFT_SLICER_LABELS: dict[str, str] = {
    "corporate_group": "Corporate Group",
    "portfolio_major": "Portfolio",
    "supply_format": "Supply Format",
    "size": "Size",
    "taxonomy": "Taxonomy",
    "brand_category": "Brand Category",
    "brand_name": "Brand",
    "portfolio_minor": "Minor Product",
    "item_code": "Item",
}
# Sanity guard — keep the view's slicer order in lock-step with the
# builder's canonical dim set so a new dim can't silently drop off the UI.
assert set(_PLAN_LIFT_SLICER_ORDER) == set(SLICER_DIMS)

# The three combo builders offered inside every Portfolio section.
_PLAN_LIFT_COMBO_IDS: tuple[str, ...] = ("A", "B", "C")
# Combo slicer dims = every dim EXCEPT Portfolio (the combo is already
# scoped to its enclosing Portfolio, so a Portfolio picker would be moot).
_PLAN_LIFT_COMBO_DIMS: tuple[str, ...] = tuple(
    d for d in _PLAN_LIFT_SLICER_ORDER if d != "portfolio_major"
)
# Categorical palette for the many lines a Portfolio chart can carry
# (one per Minor Product + applied combos + IRI overlays).
_PLAN_LIFT_PALETTE: tuple[str, ...] = (
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
)


@st.cache_data(show_spinner=False)
def _cached_plan_lift_base(
    _ship_sig: tuple[int, int],
    _plan_sig: tuple[int, int],
    _dim_sig: tuple[int, int],
    _cust_sig: tuple[int, int],
    *,
    _shipments_df: pd.DataFrame,
    _plan_df: pd.DataFrame,
    _dimitems_df: Optional[pd.DataFrame],
    _customer_names_df: Optional[pd.DataFrame],
) -> tuple[pd.DataFrame, dict]:
    """Build + cache the item×month×dims base, keyed on source signatures.

    Mirrors the PLR caching contract: the signatures (rows, cols) form the
    cache key while the underscore-prefixed frames are passed for the
    actual build but excluded from hashing.  The build's
    :class:`PlanLiftBuildStats` is returned as a plain ``dict`` so the
    cached value is a builtin (avoids the pickle class-identity hazard
    documented in ``data_sources.ibp_official``).
    """
    base, stats = build_plan_lift_base(
        shipments_df=_shipments_df,
        plan_df=_plan_df,
        dimitems_df=_dimitems_df,
        customer_names_df=_customer_names_df,
    )
    return base, asdict(stats)


def _render_plan_lift_analysis() -> None:
    """Foldable 'Plan Lift Analysis' section (above RO, independent of it)."""
    with st.expander("📈 Plan Lift Analysis", expanded=False):
        st.caption(
            "**YoY Lift%** per Portfolio, computed entirely here from IBP "
            "**Shipments** + the **current plan** (`dp_factscurrentaps`).  "
            "At each month a series' numerator is its **plan** lbs when "
            "planned, else its **shipped** lbs; the prior year is that same "
            "numerator shifted back 12 months.  Lift is the **ratio of "
            "sums** (`Σnumerator / Σprior-year − 1`), so left of today reads "
            "actual-vs-actual and right of today reads plan-vs-actual.  "
            "Corporate Group is resolved for both sides through "
            "`dp_dimcustomernames`."
        )
        if not fabric_signin_widget.is_fabric_signed_in():
            fabric_signin_widget.render()
            return
        _render_plan_lift_fragment()


@st.fragment
def _render_plan_lift_fragment() -> None:
    """Load sources, build the base once, render per-portfolio charts.

    A ``@st.fragment`` so slicer / button interactions rerun only this
    section — never the RO or Demand Summary blocks above/below.
    """
    _render_plan_lift_instructions()

    # 1) Required sources.  Shipments + current plan are hard dependencies;
    #    a failure here means there is nothing to chart.
    try:
        with st.spinner("Reading Plan Lift sources from Microsoft Fabric…"):
            shipments_df = fetch_ibp_shipments_slim_df()
            plan_df = fetch_factscurrentaps_slim_df()
    except (IBPOfficialSourceError, PlanLiftError) as exc:
        st.error(f"❌ Could not load the Plan Lift sources.\n\n{exc}")
        return

    # 2) Soft dependencies — dims + the Corporate Group lookup degrade
    #    gracefully (blank dims / "(Unmapped)" group) rather than blocking.
    try:
        dimitems_df = fetch_dimitems_df()
    except RoComparisonError as exc:
        logger.warning("dp_dimitems load failed for Plan Lift: %s", exc)
        dimitems_df = None
    try:
        customer_names_df = fetch_dp_dimcustomernames_df()
    except CustomerDimsError as exc:
        logger.warning("dp_dimcustomernames load failed for Plan Lift: %s", exc)
        customer_names_df = None
    try:
        calendar_df = fetch_dimcalendar_df()
    except PlanLiftError as exc:
        logger.warning("dp_dimcalendar load failed for Plan Lift: %s", exc)
        calendar_df = None

    # 3) Build (cached) the item×month×dims base frame.
    base, stats_dict = _cached_plan_lift_base(
        _signature_for(shipments_df),
        _signature_for(plan_df),
        _signature_for(dimitems_df),
        _signature_for(customer_names_df),
        _shipments_df=shipments_df,
        _plan_df=plan_df,
        _dimitems_df=dimitems_df,
        _customer_names_df=customer_names_df,
    )
    if base is None or base.empty:
        st.info("ℹ️ No overlapping shipment / plan rows to chart yet.")
        return

    _render_plan_lift_coverage(PlanLiftBuildStats(**stats_dict))

    fiscal_labels = build_month_fiscal_labels(calendar_df)
    today = today_month_begin(date.today())

    portfolios = list_portfolios(base)
    if not portfolios:
        st.info("ℹ️ No `portfolio_major` values found in `dp_dimitems`.")
        return

    # 4) Section-level control: prior-year volume floor (one for the whole
    #    section).  The IRI dataset is picked PER PORTFOLIO below.
    volume_floor = st.number_input(
        "Prior-year volume floor (lbs)",
        min_value=0.0, value=0.0, step=1000.0,
        key="plan_lift_volume_floor",
        help=(
            "Suppress (show 'n.m.') any series-month whose prior-year base "
            "is positive but below this many pounds, so a tiny denominator "
            "can't produce an explosive lift.  0 disables the floor."
        ),
    )

    # List the available IRI files ONCE (one cheap round-trip) and hand the
    # paths to every Portfolio; each Portfolio renders its own picker + does
    # its own lazy load, so files differ per Portfolio without N listings.
    try:
        iri_paths = list_iri_files()
    except PlanLiftError as exc:
        logger.warning("Could not list IRI files for Plan Lift: %s", exc)
        iri_paths = []

    # 5) One lazy expander per Portfolio: Minor-Product lines by default,
    #    plus combos / IRI overlays the planner adds via the button.
    for idx, pmaj in enumerate(portfolios):
        if idx:
            st.divider()
        with st.expander(f"📦 {pmaj}", expanded=False):
            _render_plan_lift_portfolio(
                base, pmaj, volume_floor, iri_paths, today, fiscal_labels,
            )


def _render_plan_lift_instructions() -> None:
    """Plain-English 'how this section works' block at the top."""
    with st.expander("📖 Instructions — what this shows & how it's built", expanded=False):
        st.markdown(
            "**What it shows.**  For every Portfolio, a line per **Minor "
            "Product** tracking **YoY Lift %** — how this year's volume "
            "compares to the same month a year earlier.\n\n"
            "**How a line is built (ratio of sums).**  For each month we add "
            "up the relevant volume, then divide by the same total from 12 "
            "months earlier and subtract 1:\n"
            "- **This month's volume** = the **current plan** "
            "(`consensus_plan_lbs`) when a plan exists, otherwise actual "
            "**shipped** lbs.  So *left of the 'today' line* reads "
            "actual-vs-actual; *right of it* reads plan-vs-actual (shaded).\n"
            "- **Prior year** = the **actual shipped** volume from 12 months "
            "back — **always shipments, never the plan/APS**, even for future "
            "months.  Every line answers \"vs. what we actually shipped a "
            "year ago\".\n"
            "- If there's no prior-year shipped volume to divide by, the "
            "point shows **'n.m.'** (not meaningful).\n\n"
            "**Combos (A / B / C).**  Build your own line inside any "
            "Portfolio by picking Corporate Group / Supply Format / Brand / "
            "Item etc. (choices are limited to that Portfolio).  Lines appear "
            "only after you press **➕ Add to chart**.\n\n"
            "**IRI overlay.**  Tick **Include IRI data** in a combo to add "
            "syndicated **Unit Lift %** (`Incremental ÷ Base Units`, summed "
            "to the month) from the IRI file you pick in that Portfolio's own "
            "section.  It's a **promotional** lift — a different idea from "
            "YoY — so it's drawn **dashed on the right-hand axis**.\n\n"
            "**Data sources.**  IBP Shipments + `dp_factscurrentaps` "
            "(plan) → the lift; `dp_dimitems` → Portfolio / Minor Product / "
            "Brand etc.; `dp_dimcustomernames` → Corporate Group (for both "
            "shipments and plan); `dp_dimcalendar` → fiscal labels; "
            "`Files/RO Tracking/IRI/` → the IRI overlay."
        )


def _render_plan_lift_coverage(stats: PlanLiftBuildStats) -> None:
    """Surface build warnings + a one-line mapping-coverage caption."""
    for msg in stats.warnings:
        st.warning(f"⚠️ {msg}")
    notes: list[str] = []
    if stats.corp_unmapped_plan_pct >= 1.0:
        notes.append(f"{stats.corp_unmapped_plan_pct:.0f}% of plan lbs unmapped")
    if stats.corp_unmapped_ship_pct >= 1.0:
        notes.append(f"{stats.corp_unmapped_ship_pct:.0f}% of shipment lbs unmapped")
    if stats.item_unmatched_pct >= 1.0:
        notes.append(f"{stats.item_unmatched_pct:.0f}% of lbs from items absent in dp_dimitems")
    if notes:
        st.caption(
            "Coverage — " + "; ".join(notes)
            + f". Those land in **{CORP_GROUP_UNMAPPED}** / **{DIM_UNKNOWN}** "
            "and are still counted in any series they belong to."
        )


def _render_plan_lift_iri_picker(
    pmaj: str, iri_paths: list[str],
) -> tuple[Optional[pd.DataFrame], dict[str, list[str]]]:
    """Render one Portfolio's IRI dataset picker + lazy load.

    The IRI CSV (tens of MB) is fetched only once a combo IN THIS
    PORTFOLIO has its "Include IRI data" box ticked — detected via the
    per-combo checkbox keys in ``st.session_state`` — so a Portfolio the
    planner never overlays IRI on pays nothing.  Two Portfolios that pick
    the same file share one cached read.

    Returns ``(iri_df_or_None, iri_filter_options)``.
    """
    if not iri_paths:
        st.caption("_No IRI files found in `Files/RO Tracking/IRI/`._")
        return None, {}

    selected = st.selectbox(
        "IRI dataset (for this Portfolio's combo overlays)",
        options=iri_paths,
        format_func=iri_file_label,
        key=f"plan_lift_iri_file_{pmaj}",
        help="Pick which IRI export this Portfolio's combos overlay; each "
             "combo reads its IRI filters from this file.",
    )

    iri_wanted = any(
        bool(st.session_state.get(f"plan_lift_{pmaj}_{c}_iri"))
        for c in _PLAN_LIFT_COMBO_IDS
    )
    if not iri_wanted:
        st.caption("_Tick **Include IRI data** in a combo below to load this dataset._")
        return None, {}

    try:
        with st.spinner(f"Reading IRI dataset '{iri_file_label(selected)}'…"):
            iri_df = fetch_iri_df(selected)
        return iri_df, list_iri_filter_options(iri_df)
    except PlanLiftError as exc:
        logger.warning("IRI load failed: %s", exc)
        st.warning(f"⚠️ Could not load the selected IRI dataset: {exc}")
        return None, {}


def _plan_lift_combo_label(combo_id: str, selections: dict[str, list[str]]) -> str:
    """Build a compact, human label for a combo line from its filters."""
    if not selections:
        return f"Combo {combo_id}"
    parts = [
        f"{_PLAN_LIFT_SLICER_LABELS[dim]}: {', '.join(vals)}"
        for dim, vals in selections.items()
    ]
    summary = " · ".join(parts)
    if len(summary) > 80:  # keep the legend readable
        summary = summary[:77] + "…"
    return f"Combo {combo_id} ({summary})"


def _render_plan_lift_combo(
    pmaj: str,
    combo_id: str,
    scoped_options: dict[str, list[str]],
    iri_options: dict[str, list[str]],
) -> dict:
    """Render one combo builder (slicers + optional IRI) inside a Portfolio.

    Returns a *pending* spec ``{label, plan_filters, iri_enabled,
    iri_filters}`` reflecting the current widget state — the caller
    snapshots it into applied state on the "Add to chart" click.  All
    widget keys are namespaced by ``(portfolio, combo)`` so the three
    combos and every Portfolio stay independent.
    """
    with st.expander(f"Combo {combo_id}", expanded=False):
        selections: dict[str, list[str]] = {}
        cols = st.columns(3)
        for i, dim in enumerate(_PLAN_LIFT_COMBO_DIMS):
            with cols[i % 3]:
                chosen = st.multiselect(
                    _PLAN_LIFT_SLICER_LABELS[dim],
                    scoped_options.get(dim, []),
                    key=f"plan_lift_{pmaj}_{combo_id}_{dim}",
                )
            if chosen:
                selections[dim] = chosen

        iri_enabled = st.checkbox(
            "Include IRI data",
            key=f"plan_lift_{pmaj}_{combo_id}_iri",
            help="Overlay IRI promotional Unit Lift % (right axis) for the "
                 "filters below, from the IRI dataset chosen at the top.",
        )
        iri_filters: dict[str, list[str]] = {}
        if iri_enabled:
            if not iri_options:
                st.caption("_Select an IRI dataset at the top of the section to filter it._")
            icols = st.columns(3)
            for i, col in enumerate(IRI_FILTER_COLUMNS):
                with icols[i % 3]:
                    chosen = st.multiselect(
                        col, iri_options.get(col, []),
                        key=f"plan_lift_{pmaj}_{combo_id}_iri_{col}",
                    )
                if chosen:
                    iri_filters[col] = chosen

    return {
        "label": _plan_lift_combo_label(combo_id, selections),
        "plan_filters": selections,
        "iri_enabled": bool(iri_enabled),
        "iri_filters": iri_filters,
    }


def _render_plan_lift_portfolio(
    base: pd.DataFrame,
    pmaj: str,
    volume_floor: float,
    iri_paths: list[str],
    today: pd.Timestamp,
    fiscal_labels: dict,
) -> None:
    """Render one Portfolio: Minor-Product lines + combo/IRI overlays + download."""
    # Default lines: one YoY-lift line per Minor Product in this Portfolio.
    minors = list_minor_products(base, pmaj)
    minor_lines = [
        compute_yoy_lift(
            base, {"portfolio_major": [pmaj], "portfolio_minor": [m]},
            label=m, volume_floor=volume_floor,
        )
        for m in minors
    ]
    if not minor_lines:
        st.caption(f"_No Minor Products found for {pmaj}._")

    # IRI dataset for THIS Portfolio (lazy-loaded when a combo wants it).
    iri_df, iri_options = _render_plan_lift_iri_picker(pmaj, iri_paths)

    # Combo builders A/B/C — slicer options scoped to THIS Portfolio.
    scoped_options = list_slicer_options_for_portfolio(base, pmaj)
    st.markdown("**Build custom lines** (choices limited to this Portfolio):")
    pending = [
        _render_plan_lift_combo(pmaj, cid, scoped_options, iri_options)
        for cid in _PLAN_LIFT_COMBO_IDS
    ]

    # Deferred apply: combos / IRI only reach the chart on the button click.
    applied_key = f"plan_lift_applied_{pmaj}"
    c1, c2, _ = st.columns([1, 1, 2])
    with c1:
        if st.button("➕ Add to chart", key=f"plan_lift_add_{pmaj}", type="primary"):
            st.session_state[applied_key] = [
                s for s in pending if s["plan_filters"] or s["iri_enabled"]
            ]
    with c2:
        if st.button("Clear added", key=f"plan_lift_clear_{pmaj}"):
            st.session_state.pop(applied_key, None)
    applied: list[dict] = st.session_state.get(applied_key, [])

    # Compute the applied combo lift lines + IRI overlays.
    combo_lines: list[YoYLiftResult] = []
    iri_lines: list[IRIResult] = []
    for spec in applied:
        if spec["plan_filters"]:
            combo_lines.append(compute_yoy_lift(
                base, {"portfolio_major": [pmaj], **spec["plan_filters"]},
                label=spec["label"], volume_floor=volume_floor,
            ))
        if spec["iri_enabled"] and iri_df is not None:
            iri_lines.append(compute_iri_unit_lift(
                iri_df, spec["iri_filters"], label=f"{spec['label']} · IRI",
            ))

    # Line-removal control — untick any series (Minor Product, combo or IRI)
    # to drop it from the chart.  State-backed so the choice survives the
    # fragment reruns that Plotly's own legend-click hiding would reset.
    visible = _plan_lift_visible_lines(pmaj, minor_lines, combo_lines, iri_lines)
    minor_v = [ln for ln in minor_lines if ln.label in visible]
    combo_v = [ln for ln in combo_lines if ln.label in visible]
    iri_v = [ln for ln in iri_lines if ln.label in visible]

    fig = _build_plan_lift_figure(minor_v, combo_v, iri_v, today, fiscal_labels)
    st.plotly_chart(
        fig, use_container_width=True, theme=None,
        key=f"plan_lift_chart_{pmaj}",
    )
    if not applied:
        st.caption("ℹ️ Build Combo A/B/C above and press **➕ Add to chart** to overlay custom lines.")

    download = _plan_lift_download_frame(minor_v, combo_v, iri_v)
    st.download_button(
        "⬇️ Download data",
        data=download.to_csv(index=False).encode("utf-8"),
        file_name=f"plan_lift_{pmaj.replace(' ', '_').lower()}.csv",
        mime="text/csv",
        key=f"plan_lift_dl_{pmaj}",
        help="Every series currently shown on the chart (Minor Products + combos + IRI).",
    )


def _plan_lift_visible_lines(
    pmaj: str,
    minor_lines: list[YoYLiftResult],
    combo_lines: list[YoYLiftResult],
    iri_lines: list[IRIResult],
) -> set[str]:
    """Render the 'Lines to display' control; return the labels to keep.

    Every series starts visible.  Unticking a label removes that line and
    the choice persists across reruns (unlike Plotly's legend-click, which
    resets each time the fragment rebuilds the figure).  Newly added series
    (e.g. a combo the planner just applied) auto-appear; previously removed
    ones stay removed.
    """
    labels: list[str] = []
    for line in (*minor_lines, *combo_lines, *iri_lines):
        if line.label not in labels:   # dedupe, preserve order
            labels.append(line.label)
    if not labels:
        return set()

    sel_key = f"plan_lift_visible_{pmaj}"
    known_key = f"plan_lift_visible_known_{pmaj}"
    known = st.session_state.get(known_key)
    if sel_key not in st.session_state or known is None:
        st.session_state[sel_key] = list(labels)               # first render: all on
    else:
        new = [lbl for lbl in labels if lbl not in known]      # auto-show new series
        st.session_state[sel_key] = [
            lbl for lbl in st.session_state[sel_key] if lbl in labels
        ] + new
    st.session_state[known_key] = list(labels)

    chosen = st.multiselect(
        "Lines to display",
        options=labels,
        key=sel_key,
        help="Untick a series to remove its line from the chart.",
    )
    return set(chosen)


def _build_plan_lift_figure(
    minor_lines: list[YoYLiftResult],
    combo_lines: list[YoYLiftResult],
    iri_lines: list[IRIResult],
    today: pd.Timestamp,
    fiscal_labels: dict,
) -> go.Figure:
    """Build the multi-line %-axis chart (today marker, future shading, IRI y2).

    Minor-Product + combo YoY-lift lines share the left % axis; IRI Unit
    Lift % lines are dashed on a secondary right % axis because they
    measure a different (promotional) concept.
    """
    fig = go.Figure()
    months_all: list[pd.Timestamp] = []
    colour_idx = 0

    def _add_lift_line(line: YoYLiftResult, width: float) -> None:
        nonlocal colour_idx
        colour = _PLAN_LIFT_PALETTE[colour_idx % len(_PLAN_LIFT_PALETTE)]
        colour_idx += 1
        frame = line.frame
        if frame.empty:
            return
        months_all.extend(frame[PL_COL_MONTH].tolist())
        customdata = [
            [num, py, fiscal_labels.get(m, "")]
            for m, num, py in zip(
                frame[PL_COL_MONTH], frame["numerator"], frame["prior_year"],
            )
        ]
        fig.add_trace(go.Scatter(
            x=frame[PL_COL_MONTH].tolist(),
            y=frame["lift"].tolist(),
            name=line.label,
            mode="lines+markers",
            connectgaps=False,  # a NaN ("n.m.") leaves a real gap
            line=dict(color=colour, width=width),
            marker=dict(size=5),
            customdata=customdata,
            hovertemplate=(
                "<b>%{x|%b %Y}</b> %{customdata[2]}<br>"
                + line.label
                + ": %{y:.1%}<br>"
                "numerator %{customdata[0]:,.0f} lbs · "
                "prior yr %{customdata[1]:,.0f} lbs<extra></extra>"
            ),
        ))

    for line in minor_lines:           # default Minor-Product lines
        _add_lift_line(line, width=2.0)
    for line in combo_lines:           # applied combos — thicker to stand out
        _add_lift_line(line, width=3.0)

    for line in iri_lines:             # IRI overlays on the secondary axis
        colour = _PLAN_LIFT_PALETTE[colour_idx % len(_PLAN_LIFT_PALETTE)]
        colour_idx += 1
        frame = line.frame
        if frame.empty:
            continue
        fig.add_trace(go.Scatter(
            x=frame["month"].tolist(),
            y=frame["unit_lift"].tolist(),
            name=line.label,
            mode="lines+markers",
            connectgaps=False,
            line=dict(color=colour, width=2.0, dash="dot"),
            marker=dict(size=5, symbol="diamond"),
            yaxis="y2",
            customdata=[
                [inc, bs] for inc, bs in zip(frame["incremental"], frame["base"])
            ],
            hovertemplate=(
                "<b>%{x|%b %Y}</b><br>" + line.label
                + ": %{y:.1%}<br>"
                "incremental %{customdata[0]:,.0f} / base %{customdata[1]:,.0f}"
                "<extra></extra>"
            ),
        ))

    fig.add_hline(y=0, line=dict(color="#cccccc", width=1))
    if months_all:
        xmax = max(months_all)
        # Shade current-month-onward (plan-influenced) and mark "today".
        if xmax >= today:
            fig.add_vrect(
                x0=today.to_pydatetime(), x1=xmax.to_pydatetime(),
                fillcolor="#fbeec1", opacity=0.30, line_width=0, layer="below",
                annotation_text="plan / actual",
                annotation_position="top right", annotation_font_size=10,
            )
        fig.add_vline(
            x=today.to_pydatetime(),
            line=dict(color="#888888", width=1.5, dash="dash"),
        )

    layout = dict(
        height=_PLAN_LIFT_CHART_HEIGHT,
        margin=dict(l=55, r=60, t=60, b=40),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0.0,
            font=dict(size=11), bgcolor="rgba(255,255,255,0.85)",
            bordercolor="#cccccc", borderwidth=1,
        ),
        xaxis=dict(title=None, tickfont=dict(size=11)),
        yaxis=dict(
            title="YoY Lift %", tickformat=".0%",
            tickfont=dict(size=11), zeroline=False,
        ),
        hovermode="x unified",
    )
    if iri_lines:
        layout["yaxis2"] = dict(
            title="IRI Unit Lift %", tickformat=".0%",
            overlaying="y", side="right", showgrid=False,
            zeroline=False, tickfont=dict(size=11),
        )
    fig.update_layout(**layout)
    return fig


def _plan_lift_download_frame(
    minor_lines: list[YoYLiftResult],
    combo_lines: list[YoYLiftResult],
    iri_lines: list[IRIResult],
) -> pd.DataFrame:
    """Concatenate every plotted series into one tidy, labelled download frame."""
    lift_cols = [
        "series", "series_type", "filters_applied", PL_COL_MONTH, "plan_sum",
        "ship_sum", "numerator", "prior_year", "lift", "below_floor",
    ]
    parts: list[pd.DataFrame] = []
    for kind, lines in (("Minor Product", minor_lines), ("Combo", combo_lines)):
        for line in lines:
            if line.frame.empty:
                continue
            frame = line.frame.copy()
            frame.insert(0, "series", line.label)
            frame.insert(1, "series_type", kind)
            frame["filters_applied"] = _plan_lift_describe_filters(line.filters)
            parts.append(frame[lift_cols])

    frames: list[pd.DataFrame] = []
    if parts:
        frames.append(pd.concat(parts, ignore_index=True))
    for line in iri_lines:
        if line.frame.empty:
            continue
        frame = line.frame.rename(columns={
            "incremental": "iri_incremental_units",
            "base": "iri_base_units",
            "unit_lift": "iri_unit_lift",
        }).copy()
        frame.insert(0, "series", line.label)
        frame.insert(1, "series_type", "IRI Unit Lift")
        frame["filters_applied"] = _plan_lift_describe_filters(line.filters)
        frames.append(frame)

    if not frames:
        return pd.DataFrame(columns=lift_cols)
    # Union of columns across plan-lift + IRI shapes (missing → NaN).
    return pd.concat(frames, ignore_index=True)


def _plan_lift_describe_filters(filters) -> str:
    """Render an applied-filter dict as a compact, human-readable string."""
    if not filters:
        return "(whole company)"
    return "; ".join(
        f"{_PLAN_LIFT_SLICER_LABELS.get(dim, dim)}={', '.join(map(str, vals))}"
        for dim, vals in filters.items()
    )


def _render_promotion_lift_analysis() -> None:
    """Foldable 'Promotion Lift Analysis' section.

    A collapsed ``st.expander`` matching the other page sections, separated
    from its neighbours by the same ``st.markdown("---")`` divider line.
    """
    with st.expander("🎯 Promotion Lift Analysis", expanded=False):
        st.caption(
            "Incremental volume lift attributable to promotions — how much "
            "extra was shipped during promoted periods versus the non-promoted "
            "baseline."
        )
        st.info("ℹ️ Promotion Lift Analysis is coming soon.")


def _render_velocity_analysis() -> None:
    """Foldable 'Velocity Analysis' section — weekly ordered vs shipped lbs.

    A collapsed ``st.expander`` matching the other page sections.  Reads
    ``dbo.Shipments`` (OneLake); the interactive body is fragment-isolated so
    filter changes rerun only this block.
    """
    with st.expander("🚀 Velocity Analysis", expanded=False):
        st.caption(
            "Week-over-week **ordered vs shipped lbs** from `dbo.Shipments`, "
            "sliced by Portfolio, Product Description, Customer, Business Unit "
            "and Product Format over a chosen order-date range."
        )
        if not fabric_signin_widget.is_fabric_signed_in():
            st.info(
                "ℹ️ Sign in via **Home & Fabric Sign-in** to load shipments."
            )
            return
        _render_velocity_analysis_body()


# Velocity chart styling: ordered = dotted red, shipped = solid teal.
_VELOCITY_STYLE: dict[str, dict] = {
    "Ordered": {"color": "#c0392b", "dash": "dot"},
    "Shipped": {"color": "#137d78", "dash": "solid"},
}


@st.fragment
def _render_velocity_analysis_body() -> None:
    """Filters + KPI tiles + weekly ordered/shipped line chart (fragment)."""
    try:
        df = vel.fetch_shipments_df()
    except vel.ShipmentsVelocityError as exc:
        st.error(f"❌ Could not load `dbo.Shipments`.\n\n{exc}")
        return
    if df is None or df.empty or vel.COL_ORDER_DATE not in df.columns:
        st.info("No shipment rows available.")
        return

    # ── Filters — one multiselect per dimension that resolved in the table ──
    order_dt = pd.to_datetime(df[vel.COL_ORDER_DATE], errors="coerce")
    min_d, max_d = order_dt.min(), order_dt.max()
    if pd.isna(min_d) or pd.isna(max_d):
        st.info("Shipment rows carry no parseable order dates.")
        return
    min_d, max_d = min_d.date(), max_d.date()

    # Business Unit first so its default (B2C) is prominent; then the rest.
    dim_specs = [
        (vel.COL_BUSINESS_UNIT, "Business Unit"),
        (vel.COL_PORTFOLIO,     "Portfolio"),
        (vel.COL_PRODUCT_FORMAT, "Product Format"),
        (vel.COL_PRODUCT_DESC,  "Product Description"),
        (vel.COL_CUSTOMER,      "Customer"),
    ]
    selections: dict[str, list[str]] = {}
    cols = st.columns(3)
    for i, (canonical, label) in enumerate(dim_specs):
        options = vel.distinct_values(df, canonical)
        if not options:
            continue                              # dimension absent in this table
        # Business Unit defaults to B2C (when present); others default to all.
        default = (
            [vel.DEFAULT_BUSINESS_UNIT]
            if canonical == vel.COL_BUSINESS_UNIT
            and vel.DEFAULT_BUSINESS_UNIT in options
            else []
        )
        with cols[i % 3]:
            selections[canonical] = st.multiselect(
                label, options=options, default=default,
                key=f"velocity_{canonical}",
                help="Leave empty to include all." if default == [] else None,
            )

    date_range = st.slider(
        "Order date range", min_value=min_d, max_value=max_d,
        value=(min_d, max_d), format="YYYY-MM-DD", key="velocity_date_range",
    )

    result = vel.build_weekly_velocity(
        df,
        portfolios=selections.get(vel.COL_PORTFOLIO) or None,
        product_descs=selections.get(vel.COL_PRODUCT_DESC) or None,
        customers=selections.get(vel.COL_CUSTOMER) or None,
        business_units=selections.get(vel.COL_BUSINESS_UNIT) or None,
        product_formats=selections.get(vel.COL_PRODUCT_FORMAT) or None,
        date_range=date_range,
    )

    # ── Summary metric tiles (respond to every filter) ──────────────────────
    tiles = (
        ("Total Ordered", _fmt_m_lbs(result.total_ordered), "lbs, all filters applied"),
        ("Total Shipped", _fmt_m_lbs(result.total_shipped), "lbs, all filters applied"),
    )
    cards = "".join(
        f'<div class="dpc-kpi dpc-kpi--walk">'
        f'<div class="k-label">{_esc_html(label)}</div>'
        f'<div class="k-value">{_esc_html(value)}</div>'
        f'<span class="k-sub">{_esc_html(sub)}</span></div>'
        for label, value, sub in tiles
    )
    st.markdown(
        f'{_DPC_KPI_CSS}<div class="dpc-kpis">{cards}</div>',
        unsafe_allow_html=True,
    )

    if result.weekly.empty:
        st.info("No shipments match the current filters / date range.")
        return

    # ── Weekly line chart: ordered (dotted) vs shipped (solid) ──────────────
    weeks = result.weekly[vel.WEEK_START]
    fig = go.Figure()
    for name, col in (("Ordered", vel.COL_ORDERED_LBS), ("Shipped", vel.COL_SHIPPED_LBS)):
        style = _VELOCITY_STYLE[name]
        fig.add_scatter(
            x=weeks, y=result.weekly[col], name=f"{name} lbs",
            mode="lines+markers",
            line=dict(color=style["color"], dash=style["dash"], width=2.5),
            marker=dict(color=style["color"], size=7),
            hovertemplate=f"{name}: %{{y:,.0f}} lbs<br>week of %{{x|%Y-%m-%d}}<extra></extra>",
        )
    fig.update_layout(
        height=380, margin=dict(l=10, r=10, t=40, b=10),
        font=dict(color=_BH_FONT_COLOR, size=13),
        xaxis=dict(title=dict(text="Week (of order date)"),
                   showgrid=True, gridcolor="#eeeeee"),
        yaxis=dict(title=dict(text="Lbs"), rangemode="tozero",
                   showgrid=True, gridcolor="#eeeeee"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        plot_bgcolor="white",
    )
    st.plotly_chart(fig, use_container_width=True, key="velocity_chart")

    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    st.download_button(
        "⬇️ Download weekly velocity (CSV)",
        data=result.weekly.to_csv(index=False).encode("utf-8"),
        file_name=f"velocity_weekly_{today}.csv", mime="text/csv",
        key="velocity_download", use_container_width=True,
    )


# ── 3. Entry point ────────────────────────────────────────────────────────────


def render() -> None:
    """Render the Demand Planner Analytics page.

    Flow
    ----
    1. Page header + Instructions
    2. IBP Cadence and Supporting files (📅, collapsible, collapsed)
    3. Plan Lift Analysis           (📈, collapsible, collapsed; above RO)
    4. Promotion Lift Analysis      (🎯, collapsible, collapsed)
    5. Velocity Analysis           (🚀, collapsible, collapsed)
    6. RO Comparison                (collapsible, expanded by default)
    7. Demand Summary               (collapsible, collapsed by default)
    8. Product Line Review          (collapsible, collapsed by default)
    9. Sales Distribution Tracker   (🚚, collapsible, collapsed)
    10. Demand Planning BI Dashboard (collapsible, last — heavy iframe)
    """
    apply_custom_css()
    st.markdown(
        '<h1 class="main-header">Demand Planner Analytics</h1>',
        unsafe_allow_html=True,
    )

    _render_instructions()
    st.markdown("---")

    _render_ibp_supporting_files()
    st.markdown("---")

    # Plan Lift Analysis sits ABOVE RO Comparison and reads its own Fabric
    # sources, so it stays fully independent of the RO calculations below.
    _render_plan_lift_analysis()
    st.markdown("---")

    _render_promotion_lift_analysis()
    st.markdown("---")

    _render_velocity_analysis()
    st.markdown("---")

    # Business Health (trailing-window order momentum) sits directly ABOVE RO
    # Comparison, separated by a divider — a self-contained read-only view.
    _render_business_health()
    st.markdown("---")

    _render_ro_comparison()
    st.markdown("---")

    # APS above IBP.  The APS section is self-contained: upload & manage, then
    # the corp-group review + patch and the demand-summary comparison + variance
    # drill-in are nested foldable sub-sections inside it.
    _render_demand_summary_aps()
    st.markdown("---")

    _render_demand_summary()
    st.markdown("---")

    _render_product_line_review()
