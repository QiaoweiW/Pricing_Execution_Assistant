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
from dataclasses import asdict
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
    demand_plan_comparison_blob_path,
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
    ComparisonNotCaptured,
    DIAG_COL_LBS,
    DIAG_COL_MLBS,
    DIAG_COL_PMAJ,
    build_prior_month_actual_vs_fcst_table,
    build_prior_month_shipment_diagnostic,
    build_pm_actual_driver_table,
    comparison_to_csv_bytes,
    compute_demand_driver_items,
    driver_table_to_csv_bytes,
    enrich_ibp_orders_df,
    list_driver_buckets_for_group,
    fetch_ro_summary_total_delta_by_path,
    list_tracker_cycles,
    list_tracker_dim_values,
    list_tracker_months,
    tracker_has_dim_columns,
    validate_filters,
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
    FY_MONTH_LABELS,
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
)
from data_sources.holistic_demand_plan_aps import (
    HolisticDemandPlanError,
    MATCH_COL_CORP,
    MATCH_COL_CUSTOMER,
    MATCH_COL_STATUS,
    MATCH_EXACT,
    MATCH_FUZZY,
    MATCH_OVERRIDE,
    MATCH_UNMAPPED,
    apply_customer_corp_overrides,
    filter_needs_review,
    generate_holistic_demand_plan_aps,
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
    save_ro_comparison_output,
)
from data_sources.ro_early_start_programs import (
    COL_FORMAT as ESP_COL_FORMAT,
    COL_LE_ANNUAL_OPP as ESP_COL_LE_ANNUAL_OPP,
    COL_PROGRAM as ESP_COL_PROGRAM,
    COL_START_DATE as ESP_COL_START_DATE,
    build_early_start_programs_table,
    list_available_formats as list_esp_formats,
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
    fetch_ro_comparison_output_df,
    recompute_subtotals,
    save_ro_summary_report,
    summary_to_csv_bytes,
)
from data_sources.ro_seed_pipeline import (
    DELETE_TARGETS,
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

# Early-Start-Date Programs section.
#
# Sourcing model: reads ``RO_Comparison_Output.csv`` directly from
# Fabric (via the cached :func:`fetch_ro_comparison_output_df`
# connector) — the SAME cache slot the Summary Report uses, so the
# "Refresh from Fabric" button at the top of the section invalidates
# both with a single click and our fragment never pays a second
# round-trip per render.  Keys persist the Format multiselect and
# the cutoff-date picker selections so a fragment rerun (caused by
# any other widget on the page) doesn't clear the planner's filters.
_SS_ESP_FORMAT_FILTER     = "_ro_esp_format_filter"
_SS_ESP_DATE_AFTER_FILTER = "_ro_esp_date_after_filter"
_SS_ESP_DATE_FILTER       = "_ro_esp_date_filter"
_SS_ESP_MIN_OPP_FILTER    = "_ro_esp_min_opp_filter"

# Sentinel "no effective lower bound" for the Start-Date-After widget.
# Predates any real Darigold program data, so by default the widget
# is rendered but filters nothing.  Pinned at module scope so we get
# one canonical spelling shared between the default-value, the
# is-it-the-default test, and the help text.
_ESP_AFTER_DATE_SENTINEL: date = date(1900, 1, 1)

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

            # Early-Start-Date Programs — drilldown of the published
            # ``RO_Comparison_Output.csv``.  Reads from Fabric directly
            # (not from ``_SS_SUMMARY_DF``) so it reflects the last
            # *saved* baseline.  Own fragment for widget isolation.
            st.markdown("---")
            _render_early_start_programs_section()

        # 11. RO Summary Report — hierarchical roll-up of the
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


# ── Early-Start-Date Programs (Fabric drilldown) ────────────────────────────

def _render_early_start_programs_section() -> None:
    """Render the header + delegate to the Early-Start-Date fragment.

    Header (title + caption) lives OUTSIDE the fragment because it's
    static text — wrapping it inside would add a stack frame to every
    widget rerun for no benefit.  Matches the
    :func:`_render_summary_report_section` shape so the two sections
    read identically when skimming the page.
    """
    st.markdown("### 🗓️ Programs with Early Start Date")
    st.caption(
        "Programs from the **published** RO comparison whose "
        "**LE First Ship Date** falls before a chosen cutoff.  "
        "Independent of the field filters and month pickers above — "
        "reads `Files/RO Tracking/RO_Reporting/RO_Comparison_Output.csv` "
        "from Microsoft Fabric directly, so the table reflects the last "
        "**saved** baseline rather than any unsaved edits in the editor."
    )
    _render_early_start_programs_fragment()


@st.fragment
def _render_early_start_programs_fragment() -> None:
    """Render the Format / cutoff-date filters and the (Format, Program,
    Start Date) table.

    Wrapped in a fragment so changing the Format multiselect or the
    cutoff-date picker reruns ONLY this block — no Fabric round-trip
    (the comparison-output frame is served from the shared
    ``@st.cache_data`` slot owned by
    :mod:`ro_summary_report`), no comparison rebuild, no warnings
    banner re-render.

    Error model
    -----------
    A genuine Fabric I/O failure is surfaced as an inline error and
    the fragment returns early — the rest of the page (Summary
    Report below, editor above) is unaffected.  An empty / missing
    published CSV degrades to an info banner with a hint to publish
    the comparison first.
    """
    try:
        comp_df = fetch_ro_comparison_output_df()
    except RoSummaryReportError as exc:
        # ``fetch_ro_comparison_output_df`` lives in
        # ``ro_summary_report`` and raises that module's error type.
        # We share the connector deliberately (one cache slot, one
        # Fabric round-trip per refresh window) and therefore share
        # the error type as well — no need to introduce a parallel
        # exception just for this section.
        st.error(
            "❌ Could not read RO_Comparison_Output.csv from "
            f"Microsoft Fabric.\n\n{exc}"
        )
        return

    if comp_df is None or comp_df.empty:
        st.info(
            "ℹ️ `RO_Comparison_Output.csv` is empty or has not been "
            "published yet.  It is **auto-published** by this app the "
            "next time `RO_History_Tracker.csv` changes in Fabric — "
            "wait a few minutes for Fabric to ingest your upload and "
            "reload this page."
        )
        return

    # ── Filter widgets ────────────────────────────────────────────
    # Four side-by-side widgets at typical browser widths; stack on
    # mobile.  Format gets the lion's share of horizontal space
    # because its selected-chip list grows; the date / min-opp
    # inputs are all single-value widgets.  Reading order is the
    # natural English phrasing of a range filter:
    #   Format | after  | before | Min Opp
    available_formats = list_esp_formats(comp_df)
    fcol, acol, dcol, ocol = st.columns([3, 1.2, 1.2, 1.2])
    with fcol:
        selected_formats = st.multiselect(
            "Format",
            options=available_formats,
            key=_SS_ESP_FORMAT_FILTER,
            help=(
                "Limit to programs whose Format matches one of the "
                "selected values.  Leave empty to include every Format."
            ),
        )
    with acol:
        # Default to the 1900-01-01 sentinel: widget is always-on so
        # the planner sees it on first render, but it filters nothing
        # by default (no real program data predates 1900).  Picking a
        # later date narrows the report to a range.  ``min_value``
        # lines up with the sentinel so the widget never complains
        # "value out of range" on initial render.
        after_cutoff: date = st.date_input(
            "Start date after",
            value=st.session_state.get(
                _SS_ESP_DATE_AFTER_FILTER, _ESP_AFTER_DATE_SENTINEL,
            ),
            min_value=_ESP_AFTER_DATE_SENTINEL,
            key=_SS_ESP_DATE_AFTER_FILTER,
            help=(
                "Show only programs whose LE First Ship Date is "
                "**strictly later** than this date.  Defaults to "
                f"{_ESP_AFTER_DATE_SENTINEL:%Y-%m-%d} (no effective "
                "lower bound).  Pair with the 'Start date before' "
                "picker on the right to narrow to a window."
            ),
        )
    with dcol:
        # Default the cutoff to today on first render — common case
        # is "what's supposed to be shipping by now?".  After the
        # planner picks a value Streamlit persists it in session_state
        # under our key, so subsequent reruns preserve their choice.
        before_cutoff: date = st.date_input(
            "Start date before",
            value=st.session_state.get(_SS_ESP_DATE_FILTER, date.today()),
            key=_SS_ESP_DATE_FILTER,
            help=(
                "Show programs whose LE First Ship Date is **strictly "
                "earlier** than this date.  Defaults to today."
            ),
        )
    with ocol:
        # Step = 100,000 lbs so the +/- buttons increment in
        # planner-meaningful chunks.  The planner can always type an
        # exact threshold; the step only governs the up-down arrows.
        # ``int`` everywhere so the widget renders a whole-pound input
        # (matches the accounting format of the column itself).
        min_le_annual_opp: int = int(st.number_input(
            "Min LE Annual Opp (lbs)",
            min_value=0,
            value=int(st.session_state.get(_SS_ESP_MIN_OPP_FILTER, 0)),
            step=100_000,
            key=_SS_ESP_MIN_OPP_FILTER,
            help=(
                "Show only programs whose LE Annual Opportunity (lbs) "
                "is ≥ this value.  0 (default) includes every program."
            ),
        ))

    # ── Bounds sanity check ──────────────────────────────────────
    # Strict bounds (``after < d < before``) mean ``after >= before``
    # is *guaranteed* to produce zero rows.  Warn the planner so
    # they don't read an empty result as "no data" when it's
    # actually a self-inflicted impossible range.
    after_is_active = after_cutoff > _ESP_AFTER_DATE_SENTINEL
    if after_is_active and after_cutoff >= before_cutoff:
        st.warning(
            "⚠️ **Start date after** "
            f"(`{after_cutoff:%Y-%m-%d}`) is on or after "
            f"**Start date before** (`{before_cutoff:%Y-%m-%d}`).  "
            "The range is empty by construction — pick an earlier "
            "*after* date or a later *before* date."
        )

    # ── Compute + render ──────────────────────────────────────────
    table = build_early_start_programs_table(
        comp_df,
        formats_filter=selected_formats or None,
        # Pass ``None`` when the widget is at the sentinel so the
        # function short-circuits the lower-bound check rather than
        # comparing every row against 1900-01-01.  Saves a vectorised
        # pass on big frames and keeps test inputs deterministic.
        after_date=after_cutoff if after_is_active else None,
        before_date=before_cutoff,
        min_le_annual_opp=min_le_annual_opp if min_le_annual_opp > 0 else None,
    )

    if table.empty:
        st.info(
            "No programs match the current Format / date range / Min "
            "Opp selection.  Try expanding the Format filter, "
            "widening the date range, or lowering the Min Opp threshold."
        )
        return

    # Caption tells the planner exactly which filters are active so
    # they don't mistake a small list for missing data.  The Start
    # Date bit shows the active range — single-bound when ``after``
    # is at the sentinel, full range when both bounds are live.
    if after_is_active:
        date_bit = (
            f"**{after_cutoff:%Y-%m-%d}** < Start Date < "
            f"**{before_cutoff:%Y-%m-%d}**"
        )
    else:
        date_bit = f"Start Date < **{before_cutoff:%Y-%m-%d}**"
    filter_bits = [date_bit]
    if min_le_annual_opp > 0:
        filter_bits.append(f"LE Annual Opp ≥ **{min_le_annual_opp:,} lbs**")
    st.caption(
        f"Showing **{len(table):,}** program(s) — "
        f"{' · '.join(filter_bits)}.  "
        "_Programs at LE Probability = 100 % are always excluded._  "
        "Click any column header to sort."
    )

    cc = st.column_config
    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        # Header (38) + per-row (36) sized to fit a typical short
        # list without scrolling, capped so a huge list (hundreds of
        # programs at a far-future cutoff) doesn't dominate the page.
        height=min(36 * (len(table) + 1) + 38, 480),
        column_config={
            ESP_COL_FORMAT:        cc.TextColumn("Format",  width="small"),
            ESP_COL_PROGRAM:       cc.TextColumn("Program", width="large"),
            # ``accounting`` format = comma thousands, no decimals,
            # negatives in parentheses — matches every other Lbs
            # column on this page so the planner reads them
            # uniformly.  Numeric column → clickable header sorts
            # numerically (not lexically).
            ESP_COL_LE_ANNUAL_OPP: cc.NumberColumn(
                "LE Annual Opp (lbs)", format="accounting", width="medium",
            ),
            ESP_COL_START_DATE:    cc.DateColumn(
                "Start Date", format="YYYY-MM-DD", width="small",
            ),
        },
    )


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
        _render_ro_summary_html(display_df)

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
    ("Year 1 Probabilized", (
        (SR_COL_Y1_PRIOR, "Prior"),
        (SR_COL_Y1_CHANGE, "Change"),
        (SR_COL_Y1_LATEST, "Latest"),
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
  font-size:0.82rem; background:#ffffff; color:#1a1a1a;}
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

    # ── Header: group band + sub-column row ───────────────────────
    band = ['<th></th>']
    for group_name, cols in _RO_SR_GROUPS:
        band.append(
            f'<th class="grp" colspan="{len(cols)}">{_esc_html(group_name)}</th>'
        )
    subhead = ['<th class="lbl">Millions of lbs.</th>']
    for _grp, cols in _RO_SR_GROUPS:
        for col, label in cols:
            cls = ' class="grp"' if col in _RO_SR_GROUP_START_COLS else ""
            subhead.append(f'<th{cls}>{_esc_html(label)}</th>')

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


# Session key holding the last-built Holistic Demand Plan result so download /
# preview interactions don't re-run the heavy Fabric build.
_HDP_APS_RESULT_KEY: str = "holistic_demand_plan_aps_result"
# Planner's manual Customer → Corporate Group overrides for the R&O leg
# ({stripped Customer -> corp}).  Applied in-memory to every R&O row of that
# Customer; survives reruns and Fabric rebuilds until explicitly cleared.
_HDP_APS_OVERRIDES_KEY: str = "holistic_demand_plan_aps_overrides"
# Bumped whenever the underlying data changes (fresh Generate) or the planner
# clears overrides, so the keyed mapping editor re-initialises cleanly instead
# of replaying stale cell edits onto a different row set.
_HDP_APS_EDITOR_NONCE_KEY: str = "holistic_demand_plan_aps_editor_nonce"


def _render_demand_summary_aps() -> None:
    """Render the Demand Summary (APS) section — the Holistic Demand Plan build.

    A foldable, self-contained section: one **Generate Holistic Demand
    Plan** button pulls ``dp_factscurrentaps`` (APS Base Plan) + expands
    ``RO_Seed.csv`` into the R&O portion, merges them into
    ``qry_mgmt_plan_full_aps.csv`` (Month · Item · Corporate Group ·
    Demand Plan Pounds · Forecast Type), and offers it as a **download**
    (never written back to Fabric).  The built result is cached in
    ``st.session_state`` so download / preview clicks don't rebuild.
    """
    with st.expander("📈 Demand Summary (APS)", expanded=False):
        st.caption(
            "**Holistic Demand Plan** — merges the **APS Base Plan** "
            "(`dbo.dp_factscurrentaps`, consensus plan tagged *APS Base "
            "Plan*) with the **R&O** portion expanded from `RO_Seed.csv` "
            "into one file **`qry_mgmt_plan_full_aps.csv`** "
            "(Month · Item · Corporate Group · Demand Plan Pounds · "
            "Forecast Type).  R&O rows are attributed to a Corporate Group "
            "by fuzzy-matching RO_Seed's Customer name to "
            "`dp_dimcustomernames`.  Download-only — nothing is written "
            "back to Fabric."
        )

        # Auth gate — match every other Fabric-backed section here.
        if not fabric_signin_widget.is_fabric_signed_in():
            st.warning(
                "🔒 **Microsoft Fabric is not connected.**  Sign in via "
                "**Home & Fabric Sign-in** in the sidebar, then return here."
            )
            return

        generate_clicked = st.button(
            "▶️ Generate Holistic Demand Plan",
            key="hdp_aps_generate",
            type="primary",
            use_container_width=True,
            help=(
                "Pulls dp_factscurrentaps + RO_Seed + supporting dims from "
                "Fabric and builds qry_mgmt_plan_full_aps.csv.  Heavy (full "
                "APS scan + 36-month RO expansion) — runs only on click."
            ),
        )

        result = st.session_state.get(_HDP_APS_RESULT_KEY)
        if generate_clicked:
            try:
                with st.spinner("Building Holistic Demand Plan (APS) from Microsoft Fabric…"):
                    result = generate_holistic_demand_plan_aps()
                st.session_state[_HDP_APS_RESULT_KEY] = result
                # Fresh data → reset the mapping editor's widget state.
                st.session_state[_HDP_APS_EDITOR_NONCE_KEY] = (
                    st.session_state.get(_HDP_APS_EDITOR_NONCE_KEY, 0) + 1
                )
            except (
                HolisticDemandPlanError, PlanLiftError, CustomerDimsError,
                LakehouseIOError, ValueError,
            ) as exc:
                st.session_state.pop(_HDP_APS_RESULT_KEY, None)
                st.error(f"❌ Could not build the Holistic Demand Plan.\n\n{exc}")
                return

        if result is None:
            st.caption(
                "_Click **Generate Holistic Demand Plan** to build the merged "
                "APS + R&O file.  It reads the current Fabric data (cached ~15 "
                "min); nothing is written back._"
            )
            return

        # Editable Customer → Corporate Group mapping FIRST (top of the
        # results) so the planner can fix mis-matches before trusting /
        # downloading the file.  Returns the merged overrides after applying
        # any inline edits; the download below reflects them immediately.
        overrides = _render_hdp_match_editor(result)
        effective = apply_customer_corp_overrides(result, overrides)

        frame = effective.frame
        if frame.empty:
            st.info(
                "The build produced no rows — check that `dp_factscurrentaps` "
                "and `RO_Seed.csv` are populated in Fabric."
            )
        else:
            st.success(
                f"✅ Built **{len(frame):,}** rows — "
                f"{effective.aps_rows:,} APS Base Plan + {effective.ro_rows:,} R&O."
            )
            today = pd.Timestamp.utcnow().strftime("%Y%m%d")
            st.download_button(
                label="⬇️ Download `qry_mgmt_plan_full_aps.csv`",
                data=frame.to_csv(index=False).encode("utf-8"),
                file_name=f"qry_mgmt_plan_full_aps_{today}.csv",
                mime="text/csv",
                key="hdp_aps_download",
                type="primary",
                use_container_width=True,
                help=(
                    "Reflects your Corporate Group edits above — each override "
                    "is applied to every R&O row of that Customer."
                ),
            )
            with st.expander("👁️ Preview (first 100 rows)", expanded=False):
                st.dataframe(frame.head(100), use_container_width=True, hide_index=True)

        # Rebuild = drop the cached result + Fabric caches, then re-fetch
        # fresh.  Manual overrides are KEPT (re-applied to the new data) —
        # use the "Clear overrides" button in the editor to discard them.
        if st.button("🔄 Rebuild (refresh from Fabric)", key="hdp_aps_rebuild"):
            st.session_state.pop(_HDP_APS_RESULT_KEY, None)
            st.cache_data.clear()
            st.rerun()


def _render_hdp_match_editor(result) -> dict[str, str]:
    """Render the editable R&O Customer → Corporate Group mapping + match log.

    Foldable, at the top of the Holistic Demand Plan results.  Shows — by
    default — only the rows that need a look (Fuzzy / Unmapped / blank
    Corporate Group / already-overridden); a toggle reveals every Customer.
    The **Corporate Group** cell is editable: typing a value overrides that
    Customer's group for **all** its R&O rows in the downloaded file.
    Auto-expands when there is anything to review.

    Returns the merged ``{stripped Customer -> corp}`` override dict (also
    persisted to session) so the caller can re-apply it to the frame.
    """
    overrides: dict[str, str] = dict(st.session_state.get(_HDP_APS_OVERRIDES_KEY, {}))
    base_log = result.customer_match_log
    if base_log is None or base_log.empty:
        return overrides

    # Original resolution per Customer — the yardstick for "is this an edit?".
    base_corp = {
        str(c).strip(): str(g)
        for c, g in zip(base_log[MATCH_COL_CUSTOMER], base_log[MATCH_COL_CORP])
    }
    status = base_log[MATCH_COL_STATUS]
    n_unmapped = int((status == MATCH_UNMAPPED).sum())
    n_fuzzy = int((status == MATCH_FUZZY).sum())
    n_exact = int((status == MATCH_EXACT).sum())

    with st.expander(
        f"🔗 R&O Customer → Corporate Group mapping — "
        f"{n_unmapped} unmapped · {n_fuzzy} fuzzy · {n_exact} exact"
        + (f" · {len(overrides)} edited" if overrides else ""),
        expanded=(n_unmapped > 0 or n_fuzzy > 0 or bool(overrides)),
    ):
        st.caption(
            "How each **R&O** Customer resolved to a Corporate Group (APS "
            "rows use `dp_factscurrentaps`'s own `corporate_group_code`).  "
            "**Edit the Corporate Group cell to fix a mapping** — the change "
            "applies to *every* R&O row of that Customer in the downloaded "
            "`qry_mgmt_plan_full_aps.csv`.  By default this shows only rows "
            "needing review (Fuzzy / Unmapped / blank); edited rows are "
            "tagged **Override**."
        )

        top_l, top_r = st.columns([3, 1.4])
        with top_l:
            show_all = st.toggle(
                "Show all customers",
                value=False,
                key="hdp_aps_show_all",
                help=(
                    "Off (default): only rows needing review.  On: every "
                    "distinct R&O Customer — use it to re-map a Customer that "
                    "matched confidently but to the wrong group."
                ),
            )
        with top_r:
            if overrides and st.button(
                "↩️ Clear overrides",
                key="hdp_aps_clear_overrides",
                use_container_width=True,
                help="Discard all manual Corporate Group edits.",
            ):
                st.session_state.pop(_HDP_APS_OVERRIDES_KEY, None)
                st.session_state[_HDP_APS_EDITOR_NONCE_KEY] = (
                    st.session_state.get(_HDP_APS_EDITOR_NONCE_KEY, 0) + 1
                )
                st.rerun()

        display_log = base_log if show_all else filter_needs_review(base_log)

        if display_log is None or display_log.empty:
            st.success(
                "✅ Every R&O Customer resolved to a real Corporate Group — "
                "nothing to review.  Tick **Show all customers** to re-map one "
                "anyway."
            )
        else:
            # Seed the editable frame with the CURRENT effective group
            # (override if set, else the resolved group; blanks for Unmapped
            # so the planner types into an empty cell).
            def _display_corp(cust: object) -> str:
                key = str(cust).strip()
                val = overrides.get(key, base_corp.get(key, ""))
                return "" if val == CORP_GROUP_UNMAPPED else str(val)

            view = display_log[[MATCH_COL_CUSTOMER, MATCH_COL_STATUS]].copy()
            view[MATCH_COL_CORP] = display_log[MATCH_COL_CUSTOMER].map(_display_corp)

            editor_nonce = st.session_state.get(_HDP_APS_EDITOR_NONCE_KEY, 0)
            edited = st.data_editor(
                view,
                hide_index=True,
                use_container_width=True,
                num_rows="fixed",
                # Key varies with the row set (show_all) and a reset nonce so
                # stale cell edits never replay onto a different set of rows.
                key=f"hdp_aps_match_editor_{int(show_all)}_{editor_nonce}",
                column_config={
                    MATCH_COL_CUSTOMER: st.column_config.TextColumn(
                        "Customer", disabled=True),
                    MATCH_COL_STATUS: st.column_config.TextColumn(
                        "Match", disabled=True, width="small"),
                    MATCH_COL_CORP: st.column_config.TextColumn(
                        "Corporate Group",
                        help=(
                            "Type to override — applied to ALL R&O rows of "
                            "this Customer. Leave blank to keep it unmapped."
                        ),
                    ),
                },
                column_order=[MATCH_COL_CUSTOMER, MATCH_COL_STATUS, MATCH_COL_CORP],
            )

            # Fold the visible edits into the override set: a cell that now
            # differs from the original resolution is an override; one reset
            # back to the original (or blanked) drops any prior override.
            for _, r in edited.iterrows():
                cust = str(r[MATCH_COL_CUSTOMER]).strip()
                val = str(r[MATCH_COL_CORP]).strip()
                base = str(base_corp.get(cust, "")).strip()
                # A real value that differs from the original resolution is an
                # override; blank / "(Unmapped)" / unchanged drops any prior one.
                if val and val != CORP_GROUP_UNMAPPED and val != base:
                    overrides[cust] = val
                else:
                    overrides.pop(cust, None)

        st.session_state[_HDP_APS_OVERRIDES_KEY] = overrides

        # Download the mapping AS APPLIED (overrides folded in) for the record.
        applied_log = apply_customer_corp_overrides(result, overrides).customer_match_log
        today = pd.Timestamp.utcnow().strftime("%Y%m%d")
        st.download_button(
            label="⬇️ Download mapping log (CSV)",
            data=applied_log.to_csv(index=False).encode("utf-8"),
            file_name=f"holistic_demand_plan_match_log_{today}.csv",
            mime="text/csv",
            key="hdp_aps_match_log_download",
        )
    return overrides


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
        _months_in_range_local(filters.actual_start, filters.actual_end)
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
    # Spell out exactly how each column is built — planners kept asking what
    # "Current Plan" vs "Last Plan" (a.k.a. Prior Plan) mean.  Foldable so the
    # reference text doesn't crowd the metrics + table (collapsed by default).
    with st.expander("ℹ️ How the columns are built", expanded=False):
        st.markdown(
            "_(Actual window = `[Actual Start … Actual End]`, "
        "Forecast window = `[Forecast Start … Forecast End]`)_\n"
        "- **Current Plan (Base)** / **Current Plan (R&O)** = the "
        "**current-cycle** forecast over the Forecast window, split by "
        "Forecast Type (Base Plan vs R&O).\n"
        "- **Current Plan** = **actual shipments** over the Actual window "
        "**＋** Current Plan (Base) **＋** Current Plan (R&O).\n"
        "- **O% of Current Plan** = Current Plan (R&O) ÷ Current Plan _(the "
        "R&O / opportunity share of the plan)_.\n"
        "- **PY Actual** = **prior-year shipments** over the plan's full "
        "horizon shifted back **12 months** — window "
        "`[Actual Start − 1yr … Forecast End − 1yr]`.  E.g. Actual begins "
        "Apr 2026 and Forecast ends Mar 2027 → PY = **Apr 2025 … Mar 2026**.\n"
        "- **Last Plan** _(the prior / one-month-ago estimate)_ = **actual "
        "shipments** over the Actual window shifted back one month "
        "`[Actual Start … Actual End − 1]` **＋** the **prior-cycle** "
        "forecast over the Forecast window shifted back one month "
        "`[Forecast Start − 1 … Forecast End]` — i.e. the month that just "
        "closed is still a prior-cycle forecast here.\n"
        "- **Total Delta** = Current Plan − Last Plan.\n"
        "- **Base Plan** = Total Delta − PM Actual − R&O _(residual, so "
        "Base Plan + PM Actual + R&O = Total Delta)_.\n"
        "- **PM Actual** = Prior-Month actual shipments − prior-cycle "
        "forecast for the selected Prior Month.\n"
        "- **R&O** = *FY27 Total Delta* from the saved RO Summary Report."
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
# When False, hide Total Actuals … Current Plan (Forecast) in the table.
_DPC_SHOW_DETAIL_COLS_KEY: str = "demand_plan_comparison_show_detail_cols"
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


@st.cache_data(ttl=_CACHE_TTL_SECONDS_OUTPUTS, show_spinner=False)
def _cached_demand_plan_comparison_payload(
    sig_key: tuple,
    filters: ComparisonFilters,
    ro_lookup_key: tuple,
    budget_lookup_key: tuple,
    _enriched: EnrichedSources,
    _ro_lookup: dict,
    _budget_by_row_id: dict[str, float],
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

    # Portfolio Major / Supply Format options come straight off the tracker
    # file (it now carries them) so the filter widgets render pre-enrichment.
    pmaj_options, sfmt_options = list_tracker_dim_values(tracker_df)
    # Foldable filters — wrapping the call renders every picker inside the
    # expander.  Expanded by default so the active window is visible.
    with st.expander("🔍 Filters", expanded=True):
        filters = _render_demand_comparison_filters(
            cycles, months, actual_months, pmaj_options, sfmt_options)
    errors = validate_filters(filters)
    if errors:
        for msg in errors:
            st.error(f"❌ {msg}")
        return

    # 3. Generate gate.  The heavy build (dim-enrich the 356k-row tracker +
    #    IBP slim read + RO Summary read + build passes) runs only when the
    #    planner clicks Generate, and stays live across reruns so picker /
    #    filter changes refine the view without re-clicking.
    enabled = st.session_state.get(_DPC_ENABLED_KEY, False)
    if not enabled:
        st.markdown(
            "**Generate Demand Plan Comparison Summary** — what this does:\n"
            "1. Reads the **latest `qry_mgmt_plan_history_tracker.csv`** from "
            "the lakehouse.\n"
            "2. If it's missing **Portfolio Major / Portfolio Minor / Supply "
            "Format**, adds those columns and fills them (PDH → "
            "RO_Item_Master), **archiving the previous file first**, then "
            "saves — so categorisation lives on the file itself.\n"
            "3. Builds the comparison table, the headline KPI tiles, and the "
            "**not-captured** reconciliation logs (prior cycle · current "
            "cycle · actual shipments).\n\n"
            "_Tip: **Save the RO Summary Report above first** so the **R&O** "
            "column is populated._\n\n"
            "_The other way to refresh this data is to upload a new Base Plan "
            "in **Demand Plan Generation** — that regenerates "
            "`qry_mgmt_plan_full`, the item-level query and the history "
            "tracker (all with the categorisation dims) from source._"
        )
        if st.button(
            "▶ Generate Demand Plan Comparison Summary",
            key="demand_plan_comparison_enable",
            type="primary",
            width="stretch",
            help=(
                "Pulls the latest tracker (adding the categorisation columns "
                "if missing) and builds the comparison, KPIs and not-captured "
                "logs.  Stays live for the session; picker / filter changes "
                "then refine the view."
            ),
        ):
            _dpc_generate(tracker_df)
        return

    # One-shot backfill confirmation (set by _dpc_generate, survives the rerun).
    backfill_banner = st.session_state.pop(_DPC_BACKFILL_BANNER_KEY, None)
    if backfill_banner:
        st.success(backfill_banner)

    # Regenerate — re-pull the latest tracker (and re-check / backfill the
    # categorisation columns), then rebuild.
    if st.button(
        "🔄 Regenerate (re-pull latest tracker)",
        key="demand_plan_comparison_regenerate",
        help="Re-reads the latest qry_mgmt_plan_history_tracker.csv and rebuilds.",
    ):
        _dpc_generate(tracker_df)

    # 4. Heavy supporting sources — loaded only post opt-in.  Each
    #    loader is independent so a single failing source doesn't
    #    short-circuit the others; the builder degrades gracefully.
    pdh_df = _load_demand_comparison_pdh()
    item_master_df = _load_mom_item_master()   # RO_Item_Master fallback dims
    actual_months = _months_in_range_local(
        filters.actual_start, filters.actual_end)
    prior_month_set = {filters.prior_month.replace(day=1)}
    ibp_month_filter = tuple(sorted(actual_months | prior_month_set))
    ibp_df, ibp_warning = _load_demand_comparison_ibp(months=ibp_month_filter)
    ibp_orders_df, ibp_orders_warning = _load_demand_comparison_ibp_orders(
        months=tuple(sorted(prior_month_set)),
    )
    # Prior-Year Actual window: the plan's full horizon (actual start →
    # forecast end) shifted back 12 months — e.g. actual Apr 2026 … forecast
    # Mar 2027 → PY Apr 2025 … Mar 2026.  Shipments over that window feed the
    # PY Actual column.
    py_window = tuple(sorted(_months_in_range_local(
        _shift_year_back(filters.actual_start),
        _shift_year_back(filters.forecast_end),
    )))
    ibp_py_df, _ = _load_demand_comparison_ibp(months=py_window)
    # Trailing-6-month shipments (current + prior year), anchored on the Actual
    # end month, for the T3M / T6M YoY KPI tiles.  T3M = last 3 of these 6.
    recent_cur = _last_n_months_local(filters.actual_end, 6)
    recent_window = tuple(sorted(recent_cur))
    recent_py_window = tuple(sorted(_shift_year_back(m) for m in recent_cur))
    ibp_recent_df, _ = _load_demand_comparison_ibp(months=recent_window)
    ibp_recent_py_df, _ = _load_demand_comparison_ibp(months=recent_py_window)
    ro_lookup = fetch_ro_summary_total_delta_by_path()
    dim_df, dim_warning = _load_demand_comparison_dim()

    # 4b. FY27 Budget workbook (leaf budgets for the Budget column).
    budget_etag = _fy27_budget_workbook_etag()
    budget_by_row_id, budget_warnings = _cached_fy27_budget_by_row_id(budget_etag)
    budget_lookup_key = tuple(sorted(budget_by_row_id.items()))

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
    #     carried on the tracker (filled from PDH → RO_Item_Master).
    _render_comparison_not_captured_logs(
        build_comparison_not_captured(
            enriched.tracker, filters, ibp_enriched=enriched.ibp),
    )

    # 6c. Executive KPI strip (headline metrics) — sits directly above the
    #     table so the reader gets the top-line story before the detail.
    _render_comparison_kpis(
        build_comparison_kpis(
            result.table, enriched.ibp_recent, enriched.ibp_recent_py, filters,
        )
    )

    # 7. Render the comparison table + download / save.
    _render_demand_comparison_table(result)

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


def _shift_year_back(d: date) -> date:
    """Return the same month one year earlier (first-of-month)."""
    return date(d.year - 1, d.month, 1)


def _last_n_months_local(end: date, n: int) -> set[date]:
    """Return the set of *n* first-of-month dates ending at *end* (inclusive)."""
    months: set[date] = set()
    cur = end.replace(day=1)
    for _ in range(max(0, n)):
        months.add(cur)
        cur = cur.replace(year=cur.year - 1, month=12) if cur.month == 1 \
            else cur.replace(month=cur.month - 1)
    return months


def _months_in_range_local(start: date, end: date) -> set[date]:
    """Return the inclusive first-of-month set covered by [start, end]."""
    out: set[date] = set()
    cur = start.replace(day=1)
    end_norm = end.replace(day=1)
    while cur <= end_norm:
        out.add(cur)
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return out


def _render_comparison_not_captured_logs(nc: ComparisonNotCaptured) -> None:
    """Render the three 'SKUs not captured' logs (prior · current · actuals).

    A SKU is *not captured* when its Portfolio Major / Supply Format / Brand /
    Portfolio Minor match no comparison-template family, so its pounds never
    reach a row — the exact gap between a raw source total and the table.
    One foldable, clearly-labelled section per leg, each with a jump link to
    RO_Item_Master.csv and a CSV download.
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
        key_suffix="prior",
    )
    _render_one_comparison_not_captured(
        nc.current_cycle,
        title=f"Current cycle ({nc.current_cycle_label}) — forecast SKUs",
        empty_note=f"Every **current-cycle ({nc.current_cycle_label})** forecast SKU is captured.",
        key_suffix="current",
    )
    _render_one_comparison_not_captured(
        nc.actuals,
        title=f"Actual shipments ({nc.actual_window_label}) — shipped SKUs",
        empty_note=f"Every **actual-shipment ({nc.actual_window_label})** SKU is captured.",
        key_suffix="actuals",
    )


def _render_one_comparison_not_captured(
    df: pd.DataFrame, *, title: str, empty_note: str, key_suffix: str,
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
                key=f"cmp_not_captured_dl_{key_suffix}",
                use_container_width=True,
            )


def _render_demand_comparison_filters(
    cycles: list[str], months: list[date], actual_months: list[date],
    pmaj_options: list[str] | None = None,
    sfmt_options: list[str] | None = None,
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
    default_current = cycles[-1]
    default_prior = cycles[-2] if len(cycles) >= 2 else cycles[-1]
    if default_prior == default_current:  # guard very small cycle lists
        default_prior = next(
            (c for c in reversed(cycles) if c != default_current), cycles[0]
        )

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

    actual_start_idx = _actual_idx(date(2026, 4, 1), 0)
    actual_end_idx = _actual_idx(date(2026, 5, 1), last_actual_idx)
    fc_start_idx = _month_idx(date(2026, 6, 1), min(prior_fallback_idx + 1, last_idx))
    fc_end_idx = _month_idx(date(2027, 3, 1), last_idx)
    prior_default_idx = _month_idx(date(2026, 5, 1), prior_fallback_idx)

    fmt_cycle = lambda c: c  # noqa: E731 — trivial identity for clarity
    # Spell the month out ("Apr 2026") so the selected value is easy to
    # read in the dropdown (the bare "4/2026" form was hard to parse).
    fmt_month = lambda d: d.strftime("%b %Y")  # noqa: E731

    row1 = st.columns(2)
    with row1[0]:
        current_cycle = st.selectbox(
            "Current cycle", options=cycles,
            index=cycles.index(default_current),
            key="dpc_current_cycle", format_func=fmt_cycle,
            help="The cycle whose plan you're evaluating.",
        )
    with row1[1]:
        prior_cycle = st.selectbox(
            "Prior cycle", options=cycles,
            index=cycles.index(default_prior),
            key="dpc_prior_cycle", format_func=fmt_cycle,
            help="The earlier cycle to compare against (drives Base Plan).",
        )

    st.markdown("**Actual month range** (IBP Shipments + current-cycle actuals)")
    row2 = st.columns(2)
    with row2[0]:
        actual_start = st.selectbox(
            "Actual — beginning month", options=actual_months, index=actual_start_idx,
            key="dpc_actual_start", format_func=fmt_month,
        )
    with row2[1]:
        actual_end = st.selectbox(
            "Actual — end month", options=actual_months, index=actual_end_idx,
            key="dpc_actual_end", format_func=fmt_month,
        )

    st.markdown("**Forecast month range** (must not overlap the actual range)")
    row3 = st.columns(2)
    with row3[0]:
        forecast_start = st.selectbox(
            "Forecast — beginning month", options=months, index=fc_start_idx,
            key="dpc_forecast_start", format_func=fmt_month,
        )
    with row3[1]:
        forecast_end = st.selectbox(
            "Forecast — end month", options=months, index=fc_end_idx,
            key="dpc_forecast_end", format_func=fmt_month,
        )

    prior_month = st.selectbox(
        "Prior Month (for PM Actual / Prior Month Forecast)",
        options=months, index=prior_default_idx,
        key="dpc_prior_month", format_func=fmt_month,
        help="The single month used for the Prior-Month columns.",
    )

    # ── Portfolio Major / Supply Format filter ──────────────────────────
    # Multiselects default to EVERYTHING selected; deselecting narrows the
    # whole summary (incl. Total B2C) to the chosen slice.  A full (or empty)
    # selection means "no filter" so the default view is unchanged.
    pmaj_options = pmaj_options or []
    sfmt_options = sfmt_options or []
    pmaj_filter: frozenset = frozenset()
    sfmt_filter: frozenset = frozenset()
    if pmaj_options or sfmt_options:
        st.markdown("**Filter by Portfolio Major / Supply Format** "
                    "_(all selected = no filter; deselect to remove)_")
        frow = st.columns(2)
        with frow[0]:
            pmaj_sel = st.multiselect(
                "Portfolio Major", options=pmaj_options, default=pmaj_options,
                key="dpc_pmaj_filter",
                help="Rows outside the selected Portfolio Majors are removed "
                     "and the subtotals recompute.",
            )
        with frow[1]:
            sfmt_sel = st.multiselect(
                "Supply Format", options=sfmt_options, default=sfmt_options,
                key="dpc_sfmt_filter",
                help="Rows outside the selected Supply Formats are removed "
                     "and the subtotals recompute.",
            )
        # Only narrow on a strict, non-empty subset.
        if pmaj_sel and set(pmaj_sel) != set(pmaj_options):
            pmaj_filter = frozenset(pmaj_sel)
        if sfmt_sel and set(sfmt_sel) != set(sfmt_options):
            sfmt_filter = frozenset(sfmt_sel)

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
        pmaj_filter=pmaj_filter,
        sfmt_filter=sfmt_filter,
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


# Hierarchy table row highlights (planner-facing).
_ROW_STYLE_HIGHLIGHT_ORANGE_BOLD = "background-color: #ffcc80; font-weight: 700"
_ROW_STYLE_SUBTOTAL = "background-color: #fde9d9; font-weight: 600"
_ROW_STYLE_MEMO = "font-style: italic; color: #555555"
_ROW_STYLE_PLR_CUSTOMER = "background-color: #e3f2fd"

_DPC_HIGHLIGHT_ROW_IDS: frozenset[str] = frozenset({"butter"})
_PLR_HIGHLIGHT_LABELS: frozenset[str] = frozenset({"Branded", "Private", "Grand Total"})


def _normalize_table_label(label: object) -> str:
    """Strip hierarchy indent (NBSP) and surrounding whitespace."""
    return str(label).replace("\u00a0", " ").strip()


def _is_butter_highlight_row(
    idx: int,
    *,
    row_ids: list[str] | None,
    labels: list[str] | None,
    highlight_row_ids: frozenset[str],
) -> bool:
    if row_ids is not None and idx < len(row_ids) and row_ids[idx] in highlight_row_ids:
        return True
    if labels is not None and idx < len(labels):
        return _normalize_table_label(labels[idx]) == "Butter"
    return False


def _style_comparison_hierarchy_row(
    row: pd.Series,
    *,
    subtotal_flags: list[bool],
    memo_flags: list[bool],
    row_ids: list[str] | None,
    labels: list[str] | None,
) -> list[str]:
    """Orange+bold Butter row; peach subtotals; italic memo rows."""
    idx = int(row.name)
    n = len(row)
    if _is_butter_highlight_row(
        idx,
        row_ids=row_ids,
        labels=labels,
        highlight_row_ids=_DPC_HIGHLIGHT_ROW_IDS,
    ):
        return [_ROW_STYLE_HIGHLIGHT_ORANGE_BOLD] * n
    if idx < len(subtotal_flags) and subtotal_flags[idx]:
        return [_ROW_STYLE_SUBTOTAL] * n
    if idx < len(memo_flags) and memo_flags[idx]:
        return [_ROW_STYLE_MEMO] * n
    return [""] * n


def _demand_comparison_column_config(percent_labels: list[str]) -> dict:
    """Return the ``column_config`` for the comparison table.

    The row-label column is pinned and widened so the indented hierarchy
    stays readable; metric columns format as one-decimal millions, and
    the two ratio columns format as one-decimal percentages.
    """
    config: dict = {
        DPC_COL_LABEL: st.column_config.TextColumn(
            DPC_COL_LABEL, width="large", pinned=True,
        ),
    }
    for col_id in DPC_DISPLAY_ORDER:
        label = DPC_DISPLAY_LABELS[col_id]
        if label in percent_labels:
            config[label] = st.column_config.NumberColumn(label, format="%.1f%%")
        else:
            config[label] = st.column_config.NumberColumn(label, format="%.2f")
    return config


# Executive KPI strip shown above the Demand Plan Comparison table.  Four
# tiles read left→right as a narrative: current reality → recent trend → the
# plan's full-year assumption → the R&O aspiration baked into the plan.
_DPC_KPI_CSS = """
<style>
.dpc-kpis {display:flex; gap:14px; flex-wrap:wrap; margin:.15rem 0 1rem;}
.dpc-kpi {flex:1 1 180px; min-width:165px; background:#ffffff;
  border:1px solid #e4e0d8; border-top:3px solid #1f4e79; border-radius:10px;
  padding:12px 16px 11px; box-shadow:0 1px 3px rgba(40,50,70,.07);}
.dpc-kpi .k-label {font-size:.72rem; font-weight:700; letter-spacing:.04em;
  text-transform:uppercase; color:#5a6472;}
.dpc-kpi .k-value {font-size:1.85rem; font-weight:800; line-height:1.15;
  margin:.12rem 0 .12rem; color:#1f4e79;}
.dpc-kpi .k-value.up {color:#1b7f3a;}
.dpc-kpi .k-value.down {color:#c0392b;}
.dpc-kpi .k-value.flat {color:#5a6472;}
.dpc-kpi .k-desc {font-size:.7rem; font-style:italic; color:#8a8f98;}
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


def _render_comparison_kpis(kpis: ComparisonKpis) -> None:
    """Render the four headline KPI tiles above the comparison table."""
    t3m = _fmt_yoy(kpis.t3m_yoy)
    t6m = _fmt_yoy(kpis.t6m_yoy)
    fy = _fmt_yoy(kpis.full_year_yoy)
    ro = _fmt_share(kpis.ro_pct)
    # (label, (value_text, css_class), descriptor) — narrative left→right.
    tiles = (
        ("T3M YoY", t3m, "Current reality"),
        ("T6M YoY", t6m, "Recent trend"),
        ("Full-Year YoY", fy, "Plan assumption"),
        ("R&O % of Current Plan", ro, "Aspiration"),
    )
    cards = "".join(
        f'<div class="dpc-kpi"><div class="k-label">{_esc_html(label)}</div>'
        f'<div class="k-value {cls}">{_esc_html(text)}</div>'
        f'<div class="k-desc">{_esc_html(desc)}</div></div>'
        for label, (text, cls), desc in tiles
    )
    st.markdown(
        f'{_DPC_KPI_CSS}<div class="dpc-kpis">{cards}</div>',
        unsafe_allow_html=True,
    )


def _render_demand_comparison_table(result) -> None:
    """Render the comparison table (styled) + a CSV download button.

    Subtotal rows are shaded + bold; memo rows (Cottage Cheese / Sour
    Cream) are italicised.  Percentage columns are scaled to whole
    percents for display.  The download serves the on-screen frame
    (internal metadata columns stripped).
    """
    table = result.table
    if table is None or table.empty:
        st.info("No comparison rows to display.")
        return

    # Clean frame (no internal metadata cols) — shared by the download
    # and the Fabric save so both emit exactly what's on screen.
    save_df = table.drop(
        columns=[c for c in ("_row_id", "_indent", "_is_subtotal", "_is_memo")
                 if c in table.columns]
    ).reset_index(drop=True)

    # ── Download + Save to Fabric (above the table — easy to find) ────
    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    dl_col, save_col = st.columns([1, 1])
    with dl_col:
        st.download_button(
            label="⬇️ Download Demand Plan Comparison Summary (CSV)",
            data=comparison_to_csv_bytes(result),
            file_name=f"demand_plan_comparison_summary_{today}.csv",
            mime="text/csv",
            key="demand_plan_comparison_download",
            type="primary",
            width="stretch",
            help=(
                "Downloads the comparison as shown — preserves the indented "
                "row hierarchy and every metric column."
            ),
        )
    with save_col:
        if st.button(
            "💾 Save to Fabric (overwrite)",
            key="demand_plan_comparison_save",
            type="primary",
            width="stretch",
            help=(
                "Overwrites `Files/RO Tracking/Demand Plan/"
                "qry_demand_plan_comparison_summary.csv` with the table as "
                "shown, so the comparison can be consumed without recomputing."
            ),
        ):
            try:
                with st.spinner("Saving Demand Plan Comparison Summary to Microsoft Fabric…"):
                    blob_path = save_demand_plan_comparison(save_df)
            except DemandSummaryError as exc:
                st.error(f"❌ Save failed.\n\n{exc}")
            else:
                st.success(f"✅ Saved to `Files/{blob_path}` ({len(save_df)} rows).")

    # ── Build the display frame ───────────────────────────────────────
    # Percent ids → display labels; the stored values are fractions, so
    # multiply by 100 for a whole-percent display.
    percent_labels = [DPC_DISPLAY_LABELS[c] for c in DPC_PERCENT_COLS]

    # Row-type flags (positional) for styling, captured before we drop
    # the internal metadata columns.
    subtotal_flags = table["_is_subtotal"].tolist()
    memo_flags = table["_is_memo"].tolist()
    row_ids = (
        table["_row_id"].tolist() if "_row_id" in table.columns else None
    )
    label_flags = (
        table[DPC_COL_LABEL].tolist() if DPC_COL_LABEL in table.columns else None
    )

    display_df = table.drop(
        columns=[c for c in ("_row_id", "_indent", "_is_subtotal", "_is_memo")
                 if c in table.columns]
    ).reset_index(drop=True)
    for label in percent_labels:
        if label in display_df.columns:
            display_df[label] = display_df[label] * 100.0

    # Optional detail columns (hidden by default per planner spec).
    hidden_detail_labels = {
        DPC_DISPLAY_LABELS[c] for c in DPC_COLS_HIDDEN_BY_DEFAULT
    }
    show_detail_cols = st.checkbox(
        "Show prior-month & current-actual detail columns "
        "(Prior Month Actual / Forecast, Current Plan (Actual))",
        value=False,
        key=_DPC_SHOW_DETAIL_COLS_KEY,
        help=(
            "When unchecked, the table shows Last Plan through Budget only. "
            "Download and Save to Fabric still include every column."
        ),
    )
    visible_metric_cols = [
        DPC_DISPLAY_LABELS[c] for c in DPC_DISPLAY_ORDER
        if show_detail_cols or DPC_DISPLAY_LABELS[c] not in hidden_detail_labels
    ]
    column_order = [DPC_COL_LABEL, *visible_metric_cols]

    def _style_row(row: pd.Series) -> list[str]:
        return _style_comparison_hierarchy_row(
            row,
            subtotal_flags=subtotal_flags,
            memo_flags=memo_flags,
            row_ids=row_ids,
            labels=label_flags,
        )

    styled = display_df.style.apply(_style_row, axis=1)

    table_height = min(35 * (len(display_df) + 1) + 38, 900)
    st.dataframe(
        styled,
        width="stretch",
        hide_index=True,
        height=table_height,
        column_order=column_order,
        column_config=_demand_comparison_column_config(percent_labels),
    )

    if not result.ro_summary_available:
        st.caption(
            "_R&O is zero because the RO Summary Report could not be read. "
            "Save the RO Summary Report above to populate it._"
        )


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
    filtered = filtered.sort_values(
        by=[DRV_COL_PMAJ, DRV_COL_SFMT, DRV_COL_BRAND],
        ascending=[True, True, True],
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
_PMAF_NAVY = "#1f4e79"
_PMAF_CSS = f"""
<style>
.pmaf {{overflow-x:auto; margin:.25rem 0 .75rem;}}
.pmaf table {{border-collapse:collapse; width:100%; font-size:.8rem;
  background:#ffffff; color:#1a1a1a;}}
.pmaf th, .pmaf td {{padding:4px 10px; white-space:nowrap;}}
.pmaf thead th {{background:{_PMAF_NAVY}; color:#ffffff; font-weight:700;
  text-align:right; border:1px solid #2f5f8f;}}
.pmaf thead th.lbl {{text-align:left;}}
.pmaf tbody td {{border-bottom:1px solid #e8e8e8; text-align:right;
  background:#ffffff;}}
.pmaf td.lbl {{text-align:left;}}
.pmaf .grp {{border-left:2px solid {_PMAF_NAVY};}}
.pmaf tr.total td {{background:#dce6f1; font-weight:700;}}
.pmaf tr.section td {{background:#f8cbad; font-weight:700;}}
.pmaf tr.sub td {{font-weight:600;}}
.pmaf tr.memo td {{font-style:italic; color:#555555;}}
</style>
"""


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
    """Render Prior Month Actual vs Fcst as the screenshot-styled HTML table.

    Two-row banded header (Total {forecast, Ordered, Shipped} · Difference
    {Ordered, Shipped} · % {Ordered, Shipped}), dark-blue header, light-blue
    Total B2C, light-orange Portfolio-Major section rows, bold subtotals and
    italic memo rows.  Read-only (edits/exports happen via the CSV download).
    """
    fcst_label = f"{prior_cycle} Forecast"
    # (column id, header label, value formatter) grouped under the band names.
    groups: tuple[tuple[str, tuple[tuple[str, str, object], ...]], ...] = (
        ("Total", (
            (PMAF_COL_PRIOR_PLAN, fcst_label, _fmt_pmaf_lbs),
            (PMAF_COL_ORDERED, "Ordered", _fmt_pmaf_lbs),
            (PMAF_COL_SHIPPED, "Shipped", _fmt_pmaf_lbs),
        )),
        ("Difference", (
            (PMAF_COL_ORDERED_DIFF, "Ordered", _fmt_pmaf_diff),
            (PMAF_COL_SHIPPED_DIFF, "Shipped", _fmt_pmaf_diff),
        )),
        ("%", (
            (PMAF_COL_ORDERED_PCT, "Ordered", _fmt_pmaf_pct),
            (PMAF_COL_SHIPPED_PCT, "Shipped", _fmt_pmaf_pct),
        )),
    )
    cols = [(cid, fmt) for _g, gcols in groups for (cid, _lbl, fmt) in gcols]
    group_start = {gcols[0][0] for _g, gcols in groups}  # first col of each band

    # ── Header: band row + sub-column row ─────────────────────────
    band = [f'<th class="lbl">{_esc_html(prior_month.strftime("%B %Y"))}</th>']
    for gname, gcols in groups:
        band.append(f'<th class="grp" colspan="{len(gcols)}">{_esc_html(gname)}</th>')
    sub = ['<th class="lbl">Millions of lbs.</th>']
    for _g, gcols in groups:
        for cid, lbl, _fmt in gcols:
            cls = ' class="grp"' if cid in group_start else ""
            sub.append(f'<th{cls}>{_esc_html(lbl)}</th>')

    # ── Body rows ─────────────────────────────────────────────────
    body: list[str] = []
    for _idx, row in table.iterrows():
        row_id = str(row.get("_row_id", ""))
        indent = int(row.get("_indent", 0) or 0)
        is_sub = bool(row.get("_is_subtotal", False))
        is_memo = bool(row.get("_is_memo", False))
        if row_id == "total_b2c":
            cls = "total"
        elif indent == 1:
            # Every indent-1 row is a Portfolio Major section (orange + bold),
            # whether it's a subtotal (ESL, Cultured, …) or a leaf (Butter).
            cls = "section"
        elif is_sub:
            cls = "sub"
        elif is_memo:
            cls = "memo"
        else:
            cls = ""
        tr = f'<tr class="{cls}">' if cls else "<tr>"
        cells = [f'<td class="lbl">{_esc_html(row.get(DPC_COL_LABEL, ""))}</td>']
        for cid, fmt in cols:
            grp = ' class="grp"' if cid in group_start else ""
            cells.append(f'<td{grp}>{fmt(row.get(cid))}</td>')
        body.append(tr + "".join(cells) + "</tr>")

    html = (
        f'{_PMAF_CSS}<div class="pmaf"><table>'
        f'<thead><tr>{"".join(band)}</tr><tr>{"".join(sub)}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )
    st.markdown(html, unsafe_allow_html=True)


def _render_prior_month_actual_vs_fcst_table(
    table: pd.DataFrame, *, prior_cycle: str, prior_month: date,
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
        key="dpc_prior_month_vs_fcst_download",
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


def _render_demand_comparison_driver_tables_cached(
    enrich_sig: tuple,
    filters: ComparisonFilters,
    dim_sig: tuple,
    enriched: EnrichedSources,
    dim_df: Optional[pd.DataFrame],
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

    st.markdown(
        f"**PM Actual drivers**  —  _driver = Customer Name – Customer No "
        f"(prior month: {filters.prior_month.strftime('%b %Y')})_"
    )
    _render_one_driver_table(pm_result, DRV_PM_ACTUAL_VALUE, "pm_actual_drivers")

    st.markdown(
        "**Base Plan drivers**  —  _driver = Customer – Party Site No "
        "(current vs prior cycle, forecast months)_"
    )
    _render_one_driver_table(bp_result, DRV_BASE_PLAN_VALUE, "base_plan_drivers")


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
        cy_actual_months,
        _detail_df=detail_df,
        _ibp_orders_df=ibp_orders_df,
        _ibp_shipments_df=ibp_shipments_df,
        _pdh_df=pdh_df,
        _customer_names_dim=customer_names_dim,
        _ship_to_sites_dim=ship_to_sites_dim,
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
    cy_actual_months: tuple,
    *,
    _detail_df: Optional[pd.DataFrame],
    _ibp_orders_df: Optional[pd.DataFrame],
    _ibp_shipments_df: Optional[pd.DataFrame],
    _pdh_df: Optional[pd.DataFrame],
    _customer_names_dim: Optional[pd.DataFrame],
    _ship_to_sites_dim: Optional[pd.DataFrame],
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
    #           dp_dimshiptosites
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


# ── 3. Entry point ────────────────────────────────────────────────────────────


def render() -> None:
    """Render the Demand Planner Analytics page.

    Flow
    ----
    1. Page header + Instructions
    2. IBP Cadence and Supporting files (📅, collapsible, collapsed)
    3. Plan Lift Analysis           (📈, collapsible, collapsed; above RO)
    4. RO Comparison                (collapsible, expanded by default)
    5. Demand Summary               (collapsible, collapsed by default)
    6. Product Line Review          (collapsible, collapsed by default)
    7. Sales Distribution Tracker   (🚚, collapsible, collapsed)
    8. Demand Planning BI Dashboard (collapsible, last — heavy iframe)
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

    _render_ro_comparison()
    st.markdown("---")

    _render_demand_summary()
    st.markdown("---")

    _render_demand_summary_aps()
    st.markdown("---")

    _render_product_line_review()
