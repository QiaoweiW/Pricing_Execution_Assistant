"""Demand Planner Analytics page view.

Sections
--------
1. Source URLs                    (module-level constants)
2. Section renderers              (_render_instructions,
                                   _render_demand_planning_dashboard,
                                   _render_distribution_tracker,
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
from datetime import date, datetime
from typing import Callable, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


logger = logging.getLogger(__name__)

from data_sources.demand_summary import (
    BudgetLookup,
    DemandPivotError,
    DemandPivotFilters,
    DemandPivotResult,
    DemandSummaryError,
    DemandSummarySnapshot,
    FORECAST_BASE_PLAN,
    FORECAST_R_AND_O,
    MonthlyBudgetLookup,
    TOTAL_BUDGET_COLUMN_LABEL,
    TOTAL_COLUMN_LABEL,
    build_budget_lookup,
    build_demand_pivot,
    build_monthly_budget_lookup,
    build_supply_format_lookup,
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
    list_available_filter_values,
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
    build_base_plan_driver_table,
    build_demand_plan_comparison,
    build_enriched_sources,
    build_prior_month_actual_vs_fcst_table,
    build_pm_actual_driver_table,
    comparison_to_csv_bytes,
    compute_demand_driver_items,
    driver_table_to_csv_bytes,
    enrich_ibp_orders_df,
    list_driver_buckets_for_group,
    fetch_ro_summary_total_delta_by_path,
    list_tracker_cycles,
    list_tracker_months,
    validate_filters,
)
from data_sources.ibp_official import (
    IBPOfficialSourceError,
    fetch_ibp_orders_slim_df,
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
from data_sources.ship_to_sites import (
    ShipToSitesSourceError,
    fetch_dimshiptosites_df,
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
    COL_DIM_BCAT as SR_COL_DIM_BCAT,
    COL_DIM_PMAJ as SR_COL_DIM_PMAJ,
    COL_DIM_PMINOR as SR_COL_DIM_PMINOR,
    COL_DIM_SFMT as SR_COL_DIM_SFMT,
    COL_LABEL as SR_COL_LABEL,
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
    run_demand_plan_pipeline,
)
from data_sources.fabric_lakehouse_io import LakehouseIOError
from utils import fabric_signin_widget
from utils.embed_helpers import (
    render_embedded_resource,
    to_sharepoint_excel_embed_url,
)
from utils.ui_helpers import apply_custom_css


# ── 1. Source URLs ────────────────────────────────────────────────────────────
#
# Kept as module-level constants — these are the canonical, share-link URLs
# the user pastes from SharePoint / OneDrive.  They are *not* secrets;
# access is gated by SharePoint's own permission model.

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


def _render_demand_planning_dashboard() -> None:
    """Embed the SharePoint Power BI desktop (``.pbix``) file as a live preview.

    SharePoint's Power BI viewer renders ``.pbix`` files in a read-only
    web preview when the URL is loaded directly.  We pass the canonical
    share-link URL through unchanged because the Power BI online
    viewer does the right thing with the ``:u:/r/`` resource prefix —
    no embed-mode rewrite is necessary.
    """
    with st.expander("📊 Demand Planning BI Dashboard", expanded=False):
        render_embedded_resource(
            url=_DEMAND_PLANNING_PBIX_URL,
            title="Demand Planning Data Model v3",
            # No URL transform: SharePoint's Power BI preview handler
            # accepts the share-link form as-is.
            embed_url=None,
            height=820,
            fallback_note=(
                "This is the live Demand Planning Power BI model hosted in "
                "SharePoint. If the embed below is blank, your browser "
                "session may not be signed in to the Darigold tenant — "
                "use the button below to open it in a new tab."
            ),
        )


def _render_distribution_tracker() -> None:
    """Embed the SharePoint Excel workbook (Office-Online read-mode).

    Icon: 🚚 — distribution / route-to-market; distinct from 📊 (BI
    dashboard) and 📋 (Product Line Review) elsewhere on this page.
    """
    with st.expander("🚚 Sales Distribution Tracker (RO Details)", expanded=False):
        render_embedded_resource(
            url=_DISTRIBUTION_TRACKER_URL,
            title="Sales Distribution Tracker (RO Details)",
            # Rewrite ``action=default`` → ``action=embedview`` so Office
            # Online renders a chrome-less, read-only embed.
            embed_url=to_sharepoint_excel_embed_url(_DISTRIBUTION_TRACKER_URL),
            height=820,
            fallback_note=(
                "This is the live Sales Distribution Tracker (RO Details) "
                "workbook hosted in SharePoint, rendered through Office "
                "Online in read-only embed mode. Use the button below to "
                "open it in Excel Online with full editing rights."
            ),
        )


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
    # end-to-end from the fresh History snapshot:
    #
    #   * ``_SS_SUMMARY_DF`` / ``_SS_MONTHS_SIG`` — drop so
    #     ``_ensure_summary_in_session`` (called immediately below)
    #     rebuilds the in-memory comparison frame instead of serving
    #     the stale pre-regen copy that still matches the (Prior, LE)
    #     signature.  Without this drop the editor + per-Format driver
    #     table + Subtotal would silently render OLD numbers even though
    #     the saved CSV is fresh.
    #   * ``_SS_SUMMARY_REPORT_*`` — pop so the Summary Report
    #     fragment's "rebuild iff signature drifted" guard sees a fresh
    #     signature and recomputes from the new ``_SS_SUMMARY_DF``.
    #   * ``clear_comparison_output_cache()`` — invalidates the shared
    #     ``@st.cache_data`` slot read by the Early-Start-Date Programs
    #     section AND any other consumer of the published
    #     ``RO_Comparison_Output.csv``.  Without this clear, the
    #     drilldown would keep serving the previous baseline from cache
    #     for up to 15 minutes after the underlying CSV was overwritten.
    #
    # Net effect: a single source-CSV update on Fabric → next page
    # render → auto-regen → editor / drivers / Subtotal / Early-Start-
    # Date table / Summary Report all reflect the new baseline on the
    # SAME render.  No manual refresh click required.
    for key in (
        _SS_SUMMARY_DF, _SS_MONTHS_SIG, _SS_WARNINGS, _SS_DIMITEMS_ERROR,
        _SS_SUMMARY_REPORT_DF, _SS_SUMMARY_REPORT_LOADED_AT,
        _SS_SUMMARY_REPORT_WARNINGS, _SS_SUMMARY_REPORT_RAW_DF,
        _SS_SUMMARY_REPORT_SIG, _SS_SUMMARY_REPORT_TEMPLATE,
    ):
        st.session_state.pop(key, None)
    clear_comparison_output_cache()

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
        "Brand Category.  Independent of the field filters above; "
        "automatically rebuilds when you change the Prior / LE month "
        "pickers.  All-zero rows are hidden by default — tick **Show "
        "empty rows** to enter values into a row that currently has none."
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
                "Tick to reveal them — useful if you want to enter a "
                "value into a row that has no upstream match."
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

    # ── Render the editor ────────────────────────────────────────
    view_df = full_df if show_empty else drop_all_zero_rows(full_df)
    if view_df.empty:
        # No visible rows, but the full template may still exist in
        # session — offer download/save of the all-zero shape.
        export_ready = recompute_subtotals(
            full_df,
            st.session_state.get(_SS_SUMMARY_REPORT_TEMPLATE),
        )
        _render_summary_report_actions(export_ready)
        st.info(
            "Every row is zero — nothing to display.  Tick **Show empty "
            "rows** to edit the template directly, or change the Prior / "
            "LE pickers above to a month pair with comparison data."
        )
        return

    editor_cols = _summary_report_column_order()
    editor_df = view_df.loc[:, [c for c in editor_cols if c in view_df.columns]]
    sr_row_ids = (
        view_df[SR_COL_ROW_ID].tolist()
        if SR_COL_ROW_ID in view_df.columns
        else []
    )
    sr_labels = (
        view_df[SR_COL_LABEL].tolist()
        if SR_COL_LABEL in view_df.columns
        else []
    )

    def _style_ro_row(row: pd.Series) -> list[str]:
        return _style_ro_summary_editor_row(
            row, row_ids=sr_row_ids, labels=sr_labels,
        )

    styled_editor_df = editor_df.style.apply(_style_ro_row, axis=1)
    edited_view = st.data_editor(
        styled_editor_df,
        key="ro_sr_editor",
        num_rows="fixed",
        use_container_width=True,
        # Header (38) + per-row (35) sized to fit the whole template
        # without scrolling for the common ~10-30 visible row range,
        # capped so a huge template doesn't dominate the page.
        height=min(35 * (len(editor_df) + 1) + 38, 900),
        column_config=_summary_report_column_config(),
        column_order=editor_cols,
        hide_index=True,
    )

    # Merge the (possibly-edited) view back into the full template
    # frame, then ALWAYS recompute subtotals so the displayed totals
    # and the saved CSV match — even if a planner edited a subtotal
    # cell directly (Streamlit's data_editor can't disable per-cell,
    # only per-column, so subtotal edits are technically allowed).
    merged = full_df.copy()
    overlap = [c for c in edited_view.columns if c in merged.columns]
    merged.loc[edited_view.index, overlap] = edited_view[overlap].values
    merged = recompute_subtotals(
        merged,
        st.session_state.get(_SS_SUMMARY_REPORT_TEMPLATE),
    )
    st.session_state[_SS_SUMMARY_REPORT_DF] = merged

    # ── Download + Save (after editor — reflects live edits) ─────
    _render_summary_report_actions(merged)


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


def _summary_report_column_order() -> list[str]:
    """Return the editor's left-to-right column order.

    Per planner direction we **hide the four dimension columns**
    (Portfolio Major, Supply Format, Portfolio Minor, Brand Category)
    in the editor — they're internal match keys that just clutter the
    on-screen table.  The dim values still live in the underlying
    template DataFrame (and are therefore saved to
    ``RO_Summary_Report.csv``); they're simply omitted from
    ``column_order``, which Streamlit interprets as "don't render".

    The *🔬 Diagnostic* expander remains the planner's one-click way
    to inspect the raw dim literals when troubleshooting matches.
    """
    return [
        SR_COL_LABEL,
        SR_COL_CURRENT_PLAN, SR_COL_TOTAL_DELTA,
        SR_COL_DELTA_NEW, SR_COL_DELTA_EXIT, SR_COL_DELTA_CHANGE,
        SR_COL_Y1_PRIOR, SR_COL_Y1_CHANGE, SR_COL_Y1_LATEST,
    ]


def _summary_report_column_config() -> dict:
    """Return the ``column_config`` mapping for the RO Summary Report editor.

    Display labels match the planner's screenshot column groups; the
    "Change" collision (which appears under both Delta Breakdown and
    Year 1 Probabilized) is disambiguated by prefixing the Year 1
    instance with ``Y1`` so the editor renders a unique label per
    column.

    Internal metadata columns (``_row_id``, ``_indent``, ``_is_subtotal``)
    are hidden via ``column_order`` (omitted from the visible list).

    Data columns use ``format="%.1f"`` (NOT accounting) because the
    saved-CSV values are in MILLIONS already — accounting would render
    "47" instead of "47.8" and strip the decimal precision the planner
    cares about at this scale.  Negatives in parentheses are mimicked
    via the planner reading the leading minus sign; Streamlit's
    Number format string doesn't have an "accounting-with-decimals"
    preset.
    """
    cc = st.column_config
    money_fmt = "%.1f"

    return {
        # Dimension columns — read-only display strings.
        SR_COL_DIM_PMAJ:   cc.TextColumn("Portfolio Major", width="small", disabled=True),
        SR_COL_DIM_SFMT:   cc.TextColumn("Supply Format",   width="small", disabled=True),
        SR_COL_DIM_PMINOR: cc.TextColumn("Portfolio Minor", width="small", disabled=True),
        SR_COL_DIM_BCAT:   cc.TextColumn("Brand Category",  width="small", disabled=True),
        # Indented hierarchy label — read-only.
        SR_COL_LABEL:      cc.TextColumn("Millions of lbs.", width="medium", disabled=True),
        # FY27 Probabilized.
        SR_COL_CURRENT_PLAN: cc.NumberColumn("FY27 Current Plan", format=money_fmt),
        SR_COL_TOTAL_DELTA:  cc.NumberColumn("FY27 Total Δ",      format=money_fmt),
        # Delta Breakdown.
        SR_COL_DELTA_NEW:    cc.NumberColumn("Δ: New",    format=money_fmt),
        SR_COL_DELTA_EXIT:   cc.NumberColumn("Δ: Exit",   format=money_fmt),
        SR_COL_DELTA_CHANGE: cc.NumberColumn("Δ: Change", format=money_fmt),
        # Year 1 Probabilized — "Y1" prefix disambiguates the Change collision.
        SR_COL_Y1_PRIOR:  cc.NumberColumn("Y1 Prior",  format=money_fmt),
        SR_COL_Y1_CHANGE: cc.NumberColumn("Y1 Δ",      format=money_fmt),
        SR_COL_Y1_LATEST: cc.NumberColumn("Y1 Latest", format=money_fmt),
    }


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
    with st.expander("📈 Demand Summary", expanded=False):
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

_SS_DP_PMAJ_FILTER:  str = "ro_dp_pmaj_filter"
_SS_DP_SFMT_FILTER:  str = "ro_dp_sfmt_filter"
_SS_DP_START_FILTER: str = "ro_dp_start_month_filter"
_SS_DP_END_FILTER:   str = "ro_dp_end_month_filter"

# Hidden columns owned by the pivot builder — kept in sync with
# ``data_sources/demand_summary._HIDDEN_COLS``.  Listed locally so the
# page doesn't need to reach into the private module surface to know
# which columns to suppress from the editor.
_DP_HIDDEN_COLS: tuple[str, ...] = ("_row_id", "_indent", "_is_subtotal")


def _build_demand_pivot_supply_format_lookup() -> dict[str, str]:
    """Return the per-item Supply Format lookup for the Demand Pivot Summary.

    Composes two Fabric reads — ``qry_pdh.csv`` (primary) and
    ``RO_Item_Master.csv`` (fallback) — into a single
    ``{item -> supply_format}`` dict via
    :func:`build_supply_format_lookup`.  Both reads are wrapped in
    try/except so a transient outage on either source degrades the
    cascade gracefully:

    * ``qry_pdh.csv`` down → fallback alone populates the lookup.
    * Both down                       → returns an empty dict (every item
      shows up under the ``(blank)`` Supply Format sentinel — visible
      to the planner but not blocking).

    Failure logs are intentionally INFO-level (not warnings) because a
    blank Supply Format column is recoverable and the user has plenty
    of other visual cues in the pivot.
    """
    # Primary tier: qry_pdh.csv via the Demand Summary connector.
    try:
        pdh_snapshot = fetch_pdh()
        pdh_df = pdh_snapshot.df
    except DemandSummaryError as exc:
        # Non-fatal — fallback will carry the lookup on its own.
        logger.info(
            "qry_pdh.csv unavailable for Demand Pivot Supply Format "
            "lookup (will fall back to RO_Item_Master.csv only): %s",
            exc,
        )
        pdh_df = None

    # Fallback tier: RO_Item_Master.csv via the RO Comparison connector.
    # Using the existing connector avoids a duplicate read + cache slot
    # — Item Master is already loaded on the RO Comparison render path,
    # so this is a cache hit in the common case.
    try:
        item_master_df = fetch_ro_item_master_df()
    except RoComparisonError as exc:
        # Non-fatal — pivot still renders with primary-tier-only or
        # empty-lookup Supply Format.
        logger.info(
            "RO_Item_Master.csv unavailable for Demand Pivot Supply "
            "Format fallback (continuing with primary tier only): %s",
            exc,
        )
        item_master_df = None

    return build_supply_format_lookup(pdh_df, item_master_df)


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
    """Render the Demand Pivot Summary header + delegate to the fragment.

    The header (title + caption) stays OUTSIDE the fragment because
    it's static text — the fragment owns only the interactive widgets
    + table + chart so filter / date-range changes rerun only the
    pivot view, not the surrounding headers.
    """
    st.markdown("### 📊 Demand MOM Summary")
    st.caption(
        "Hierarchical roll-up of **`qry_total_item_level_demand.csv`** "
        "from Microsoft Fabric — Portfolio Major → Forecast Type → "
        "Supply Format — with monthly columns in **millions of pounds**.  "
        "Auto-refreshes whenever the source file changes in Fabric (same "
        "freshness model as the raw preview above).  Use the filters to "
        "narrow by Portfolio Major / Supply Format / month range; the "
        "footer subtotals and the Base + RO chart below update live."
    )
    _render_demand_pivot_fragment()


@st.fragment
def _render_demand_pivot_fragment() -> None:
    """Render the filters, hierarchical pivot table, footer subtotals, and chart.

    Why this is a ``@st.fragment``
    -------------------------------
    The widget set (PMaj / SFmt multiselects + month-range pickers)
    is interactive, but the surrounding sections of the page have
    nothing to do with the pivot.  Wrapping this block in a
    fragment scopes each filter change to a rerun of just this
    function — no upstream Fabric reads, no RO Comparison rebuild.

    Sourcing model
    --------------
    Reads the cached ``DemandSummarySnapshot`` from
    :func:`fetch_total_item_level_demand`.  That fetcher is keyed by
    blob etag/size/last_modified (see the connector), so any update
    to the source CSV invalidates the cache and the pivot reflects
    the fresh data on the same render — no manual refresh click
    needed.
    """
    # 1. Load the source frame (or surface an error + bail).
    try:
        with st.spinner("Reading qry_total_item_level_demand.csv from Microsoft Fabric…"):
            snapshot = fetch_total_item_level_demand()
    except DemandSummaryError as exc:
        st.error(
            "❌ Could not load **qry_total_item_level_demand.csv** for "
            f"the pivot.\n\n{exc}"
        )
        return

    if snapshot.df.empty:
        st.info(
            "ℹ️ `qry_total_item_level_demand.csv` is empty — nothing to "
            "roll up.  Check the upstream Fabric pipeline."
        )
        return

    # 2. Build the per-item Supply Format lookup with the two-tier
    #    cascade (qry_pdh primary, RO_Item_Master fallback).  Both
    #    sources are non-fatal — a missing or empty tier just drops
    #    out of the cascade, and items absent from BOTH tiers
    #    surface in the pivot under the "(blank)" Supply Format
    #    sentinel where the planner can spot them.
    supply_format_lookup = _build_demand_pivot_supply_format_lookup()

    # 2b. Annual budget → pivot table Total Budget column (unchanged).
    budget_lookup = _build_demand_pivot_budget_lookup()
    # 2c. Monthly budget → footer Total Budget row + chart only.
    monthly_budget = _load_demand_pivot_monthly_budget()

    # 3. Discover the available filter values from the FULL frame so
    #    a selection in one filter doesn't shrink the option list of
    #    another (matches the planner's expectation when slicing).
    try:
        filter_values = list_available_filter_values(
            snapshot.df, supply_format_lookup=supply_format_lookup,
        )
    except DemandPivotError as exc:
        st.error(f"❌ Pivot source has an unexpected schema.\n\n{exc}")
        return

    # 4. Render the filter row.
    filters = _render_demand_pivot_filters(filter_values)

    # 5. Build the pivot.
    try:
        with st.spinner("Building Demand Pivot Summary…"):
            result = build_demand_pivot(
                snapshot.df, filters,
                supply_format_lookup=supply_format_lookup,
                budget_lookup=budget_lookup,
                monthly_budget=monthly_budget,
            )
    except DemandPivotError as exc:
        st.error(f"❌ Could not build the Demand Pivot Summary.\n\n{exc}")
        return

    # 5. Empty-after-filter case — show a hint, don't render an empty
    #    table or a degenerate empty chart.
    if result.pivot.empty:
        st.info(
            "No rows match the current Portfolio Major / Supply Format / "
            "month range selection.  Widen one of the filters above to "
            "see data."
        )
        return

    # 6. Render the pivot + footer totals + download button.
    _render_demand_pivot_table(result)

    # 7. Stacked area chart of monthly Base Plan + R&O.
    st.markdown("---")
    _render_base_ro_summary_chart(result)


def _render_demand_pivot_filters(
    filter_values: dict[str, list],
) -> DemandPivotFilters:
    """Render the four filter widgets and return a :class:`DemandPivotFilters`.

    Layout
    ------
    Four columns at typical browser widths — PMaj and SFmt get the
    wider slots because their selected-chip lists grow with each
    pick, while the month-range pickers are single-value widgets.

    Defaults
    --------
    * PMaj / SFmt multiselects start EMPTY, which the pivot builder
      interprets as "include every value" (matches the screenshot
      where no slicer is active by default).
    * Month range defaults to the full available window so the table
      shows the same dataset the Excel pivot does when first opened.
    """
    pmaj_opts: list[str] = filter_values["portfolio_majors"]
    sfmt_opts: list[str] = filter_values["supply_formats"]
    month_opts: list[date] = filter_values["months"]

    # Sensible default month bounds — full available window.
    min_month: Optional[date] = month_opts[0]  if month_opts else None
    max_month: Optional[date] = month_opts[-1] if month_opts else None

    with st.expander("🔍 Filters", expanded=True):
        cols = st.columns([2, 2, 1.2, 1.2])
        with cols[0]:
            selected_pmaj = st.multiselect(
                "Portfolio Major",
                options=pmaj_opts,
                key=_SS_DP_PMAJ_FILTER,
                help=(
                    "Limit the pivot to specific Portfolio Major "
                    "value(s).  Empty = include every Portfolio Major."
                ),
            )
        with cols[1]:
            selected_sfmt = st.multiselect(
                "Supply Format",
                options=sfmt_opts,
                key=_SS_DP_SFMT_FILTER,
                help=(
                    "Limit the pivot to specific Supply Format "
                    "value(s).  Empty = include every Supply Format."
                ),
            )
        with cols[2]:
            start_month = st.date_input(
                "Month range — start",
                value=st.session_state.get(_SS_DP_START_FILTER, min_month),
                min_value=min_month,
                max_value=max_month,
                key=_SS_DP_START_FILTER,
                help=(
                    "Inclusive lower bound on `Start of Month`.  Defaults "
                    "to the earliest month available in the source CSV."
                ),
            )
        with cols[3]:
            end_month = st.date_input(
                "Month range — end",
                value=st.session_state.get(_SS_DP_END_FILTER, max_month),
                min_value=min_month,
                max_value=max_month,
                key=_SS_DP_END_FILTER,
                help=(
                    "Inclusive upper bound on `Start of Month`.  Defaults "
                    "to the latest month available in the source CSV."
                ),
            )

    # Warn (don't block) on an inverted range — the pivot builder will
    # simply return zero rows, which is correct but unhelpful without
    # an explanation.
    if (
        isinstance(start_month, date)
        and isinstance(end_month, date)
        and start_month > end_month
    ):
        st.warning(
            f"⚠️ Month-range start (`{start_month:%Y-%m}`) is after "
            f"end (`{end_month:%Y-%m}`).  The pivot will be empty — "
            "swap the two dates or widen the range."
        )

    return DemandPivotFilters(
        portfolio_majors=tuple(selected_pmaj) or None,
        supply_formats=tuple(selected_sfmt) or None,
        # Pickers are always-on and always return a date, so we forward
        # them straight through — the builder treats Python ``date``
        # values inclusively on both ends.
        start_month=start_month if isinstance(start_month, date) else None,
        end_month=end_month     if isinstance(end_month, date)   else None,
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


def _render_demand_pivot_table(result: DemandPivotResult) -> None:
    """Render the pivot table + dynamic footer subtotals + download button.

    The pivot itself is rendered with :func:`st.dataframe` (not
    :func:`st.data_editor`) because the planner doesn't edit this
    view — it's a pure roll-up of the source CSV.  ``column_order``
    pins the Row Label first, then every month in ascending order,
    then the Total column, then Total Budget when annual budget data
    is available (same layout as before the monthly-budget change).
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

    # Pivot table.  Height sized so the typical 10-30 row range fits
    # without scrolling, capped so a wide filter doesn't dominate the
    # page.  Each row is ~35 px tall; header ~38 px.
    table_height = min(35 * (len(result.pivot) + 1) + 38, 720)
    st.dataframe(
        result.pivot,
        use_container_width=True,
        hide_index=True,
        height=table_height,
        column_order=column_order,
        column_config=pivot_column_config,
    )

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
    st.markdown("**Dynamic subtotals** (live: reflects current filters)")
    footer_parts = [result.base_plan_totals, result.r_and_o_totals]
    if result.has_budget_data:
        footer_parts.append(result.budget_totals)
    footer_df = pd.concat(footer_parts, ignore_index=True)
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
            "Base + RO Summary chart below)."
        )


def _render_base_ro_summary_chart(result: DemandPivotResult) -> None:
    """Render the Base + RO Summary stacked area chart + Total Budget line.

    Mirrors the screenshot the planner shared: Base Plan stacked on
    the bottom (dark blue), R&O on top (orange), x-axis = month,
    y-axis = millions of pounds.  When monthly budget data is
    available (see :attr:`DemandPivotResult.has_budget_data`), a
    green dotted line plots the bundled budget per month from
    ``Static_Budget_Base&RO_by_Month.csv``.

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

    st.markdown("**Base + RO Summary**")
    if result.has_budget_data:
        st.caption(
            "Stacked area of monthly Base Plan + R&O totals in millions "
            "of pounds, with a **green** dotted **Total Budget** line "
            "from `Static_Budget_Base&RO_by_Month.csv`.  Demand subtotals "
            "above update with filters; the budget line is static per month."
        )
    else:
        st.caption(
            "Stacked area of monthly Base Plan + R&O totals in millions "
            "of pounds.  Updates live with the filter selections above. "
            "_Total Budget line unavailable — "
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

    # Base Plan trace — dark blue, drawn first so it sits on the
    # bottom of the stack.  ``stackgroup`` ties traces into the same
    # stack; sharing one group across both series gives us the stacked-
    # area look the planner expects.
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
        "**`qry_mgmt_plan_history_tracker.csv`** (plan), **`dbo.IBP "
        "Shipments`** (actuals), and **`qry_pdh.csv`** (Portfolio Major / "
        "Supply Format / Brand).  Pick a current vs prior cycle, the "
        "actual and forecast month ranges (which must not overlap), and "
        "the month treated as *Prior Month*.  The **R&O** column reads "
        "*FY27 Total Delta* from the saved RO Summary Report.  All values "
        "are in **millions of pounds**."
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
    tracker_sig: tuple, ibp_sig: tuple, ibp_orders_sig: tuple, pdh_sig: tuple,
    _tracker_df: pd.DataFrame,
    _ibp_df: Optional[pd.DataFrame],
    _ibp_orders_df: Optional[pd.DataFrame],
    _pdh_df: Optional[pd.DataFrame],
) -> EnrichedSources:
    """Cache the shared PDH-joined tracker + IBP frames (by reference).

    Built once per unique ``(tracker, ibp, pdh)`` signature, shared by
    the comparison builder and BOTH driver builders.  Leading-underscore
    arg names tell Streamlit to skip hashing the DataFrames themselves
    (we already key on their signature tuples).
    """
    return build_enriched_sources(_tracker_df, _ibp_df, _ibp_orders_df, _pdh_df)


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
    # 1. Tracker (cheap CSV read, lives behind the 60-min cache).
    try:
        with st.spinner("Reading qry_mgmt_plan_history_tracker.csv from Microsoft Fabric…"):
            tracker_snapshot = fetch_mgmt_plan_history_tracker()
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

    filters = _render_demand_comparison_filters(cycles, months)
    errors = validate_filters(filters)
    if errors:
        for msg in errors:
            st.error(f"❌ {msg}")
        return

    # 3. Opt-in gate.  Expensive builds (PDH-merge over the 356k-row
    #    tracker + IBP slim read + RO Summary read + 3 build passes) only
    #    happen once the planner explicitly asks for them — and stay
    #    enabled across reruns so picker changes don't require re-clicking.
    enabled = st.session_state.get(_DPC_ENABLED_KEY, False)
    if not enabled:
        st.warning(
            "Hit the saving the RO_Summary_Report Button ABOVE Before "
            "Generate this Summary"
        )
        if st.button(
            "▶ Load Demand Plan Comparison (uses tracker + IBP + PDH)",
            key="demand_plan_comparison_enable",
            type="primary",
            width="stretch",
            help=(
                "Pulls the supporting sources and builds the comparison + "
                "driver tables.  Once loaded, the section stays live for "
                "the rest of this session — picker changes rebuild only "
                "the comparison, not the underlying enrichment."
            ),
        ):
            st.session_state[_DPC_ENABLED_KEY] = True
            st.rerun(scope="fragment")
        st.caption(
            "_The Demand Plan Comparison + driver tables are heavy "
            "(joins the 356k-row tracker against PDH and the IBP Delta "
            "table).  They're loaded on demand to keep the rest of the "
            "Demand Summary section snappy._"
        )
        return

    # 4. Heavy supporting sources — loaded only post opt-in.  Each
    #    loader is independent so a single failing source doesn't
    #    short-circuit the others; the builder degrades gracefully.
    pdh_df = _load_demand_comparison_pdh()
    actual_months = _months_in_range_local(
        filters.actual_start, filters.actual_end)
    prior_month_set = {filters.prior_month.replace(day=1)}
    ibp_month_filter = tuple(sorted(actual_months | prior_month_set))
    ibp_df, ibp_warning = _load_demand_comparison_ibp(months=ibp_month_filter)
    ibp_orders_df, ibp_orders_warning = _load_demand_comparison_ibp_orders(
        months=tuple(sorted(prior_month_set)),
    )
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
    pdh_sig = _signature_for(pdh_df)
    dim_sig = _signature_for(dim_df)
    ro_sig = _ro_lookup_signature(ro_lookup)
    enrich_sig = (tracker_sig, ibp_sig, ibp_orders_sig, pdh_sig)

    with st.spinner("Building Demand Plan Comparison Summary…"):
        enriched = _cached_enriched_sources(
            tracker_sig, ibp_sig, ibp_orders_sig, pdh_sig,
            tracker_df, ibp_df, ibp_orders_df, pdh_df,
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

    # 7. Render the comparison table + download / save.
    _render_demand_comparison_table(result)

    # 8. Prior Month Actual vs Fcst summary (between comparison and drivers).
    _render_prior_month_actual_vs_fcst_table(prior_month_vs_fcst)

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


def _render_demand_comparison_filters(
    cycles: list[str], months: list[date],
) -> ComparisonFilters:
    """Render the cycle + month-range pickers; return a filter selection.

    Defaults are chosen to be immediately useful: the two most recent
    cycles (current vs prior), the latest month as *Prior Month*, the
    actual window as everything up to and including it, and the forecast
    window as everything after it (so the two windows start out
    disjoint).
    """
    # Sensible default indices.
    # ── Cycle defaults: prefer C3 (current) vs C2 (prior) ──────────────
    default_current = "C3" if "C3" in cycles else cycles[-1]
    default_prior = "C2" if "C2" in cycles else cycles[-2]
    if default_prior == default_current:  # guard very small cycle lists
        default_prior = next(
            (c for c in reversed(cycles) if c != default_current), cycles[0]
        )

    # ── Month defaults ─────────────────────────────────────────────────
    # The computed disjoint split is the FALLBACK; the planner's preferred
    # windows (Mar–May actuals, Jun 2026–Mar 2027 forecast, May prior
    # month) override it whenever those exact months exist in the tracker.
    n_months = len(months)
    last_idx = n_months - 1
    prior_fallback_idx = max(0, min(n_months // 2, n_months - 2)) if n_months >= 2 else 0

    def _month_idx(target: date, fallback: int) -> int:
        """Index of *target* in the month list, or *fallback* if absent."""
        return months.index(target) if target in months else fallback

    actual_start_idx = _month_idx(date(2026, 3, 1), 0)
    actual_end_idx = _month_idx(date(2026, 5, 1), prior_fallback_idx)
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
            "Actual — beginning month", options=months, index=actual_start_idx,
            key="dpc_actual_start", format_func=fmt_month,
        )
    with row2[1]:
        actual_end = st.selectbox(
            "Actual — end month", options=months, index=actual_end_idx,
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

    # Plain-language echo of the current selection — makes the active
    # window obvious at a glance regardless of dropdown contrast.
    st.caption(
        f"📌 Comparing **{current_cycle}** (current) vs **{prior_cycle}** (prior)  ·  "
        f"Actuals **{fmt_month(actual_start)} – {fmt_month(actual_end)}**  ·  "
        f"Forecast **{fmt_month(forecast_start)} – {fmt_month(forecast_end)}**  ·  "
        f"Prior month **{fmt_month(prior_month)}**"
    )

    return ComparisonFilters(
        current_cycle=current_cycle,
        prior_cycle=prior_cycle,
        actual_start=actual_start,
        actual_end=actual_end,
        forecast_start=forecast_start,
        forecast_end=forecast_end,
        prior_month=prior_month,
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
_RO_HIGHLIGHT_ROW_IDS: frozenset[str] = frozenset({"butter", "but"})
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


def _style_ro_summary_editor_row(
    row: pd.Series,
    *,
    row_ids: list[str],
    labels: list[str],
) -> list[str]:
    """Highlight the Butter row in the RO Summary editor (match DPC by name)."""
    idx = int(row.name)
    n = len(row)
    if _is_butter_highlight_row(
        idx,
        row_ids=row_ids,
        labels=labels,
        highlight_row_ids=_RO_HIGHLIGHT_ROW_IDS,
    ):
        return [_ROW_STYLE_HIGHLIGHT_ORANGE_BOLD] * n
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
        "Show actuals & month detail columns "
        "(Total Actuals through Current Plan (Forecast))",
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


def _render_prior_month_actual_vs_fcst_table(table: pd.DataFrame) -> None:
    """Render the *Prior Month Actual vs Fcst* summary table.

    This sits below Demand Plan Comparison Summary and above the driver
    tables.  It reuses the exact same row hierarchy/indent metadata as
    the comparison table (including dynamic Butter detail rows).
    """
    st.markdown("#### 📌 Prior Month Actual vs Fcst")
    st.caption(
        "Prior Plan = Prior Month Forecast, Ordered = IBP Orders (Ordered Qty lbs), "
        "Shipped = Prior Month Actual.  All values are in millions of lbs."
    )
    if table is None or table.empty:
        st.info("No rows available for Prior Month Actual vs Fcst.")
        return

    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    out_df = table.drop(
        columns=[c for c in ("_row_id", "_indent", "_is_subtotal", "_is_memo")
                 if c in table.columns]
    ).reset_index(drop=True)
    st.download_button(
        label="⬇️ Download Prior Month Actual vs Fcst (CSV)",
        data=out_df.to_csv(index=False).encode("utf-8"),
        file_name=f"prior_month_actual_vs_fcst_{today}.csv",
        mime="text/csv",
        key="dpc_prior_month_vs_fcst_download",
        width="stretch",
    )

    subtotal_flags = table["_is_subtotal"].tolist() if "_is_subtotal" in table.columns else []
    memo_flags = table["_is_memo"].tolist() if "_is_memo" in table.columns else []
    row_ids = (
        table["_row_id"].tolist() if "_row_id" in table.columns else None
    )
    label_flags = (
        table[DPC_COL_LABEL].tolist() if DPC_COL_LABEL in table.columns else None
    )
    for pct_col in (PMAF_COL_ORDERED_PCT, PMAF_COL_SHIPPED_PCT):
        if pct_col in out_df.columns:
            out_df[pct_col] = out_df[pct_col] * 100.0

    def _style_row(row: pd.Series) -> list[str]:
        return _style_comparison_hierarchy_row(
            row,
            subtotal_flags=subtotal_flags,
            memo_flags=memo_flags,
            row_ids=row_ids,
            labels=label_flags,
        )

    styled = out_df.style.apply(_style_row, axis=1)
    column_order = [
        DPC_COL_LABEL,
        PMAF_COL_PRIOR_PLAN,
        PMAF_COL_ORDERED,
        PMAF_COL_SHIPPED,
        PMAF_COL_ORDERED_DIFF,
        PMAF_COL_SHIPPED_DIFF,
        PMAF_COL_ORDERED_PCT,
        PMAF_COL_SHIPPED_PCT,
    ]
    column_config = {
        DPC_COL_LABEL: st.column_config.TextColumn(DPC_COL_LABEL, width="large", pinned=True),
        PMAF_COL_PRIOR_PLAN: st.column_config.NumberColumn(PMAF_COL_PRIOR_PLAN, format="%.2f"),
        PMAF_COL_ORDERED: st.column_config.NumberColumn(PMAF_COL_ORDERED, format="%.2f"),
        PMAF_COL_SHIPPED: st.column_config.NumberColumn(PMAF_COL_SHIPPED, format="%.2f"),
        PMAF_COL_ORDERED_DIFF: st.column_config.NumberColumn(PMAF_COL_ORDERED_DIFF, format="%.2f"),
        PMAF_COL_SHIPPED_DIFF: st.column_config.NumberColumn(PMAF_COL_SHIPPED_DIFF, format="%.2f"),
        PMAF_COL_ORDERED_PCT: st.column_config.NumberColumn(PMAF_COL_ORDERED_PCT, format="%.1f%%"),
        PMAF_COL_SHIPPED_PCT: st.column_config.NumberColumn(PMAF_COL_SHIPPED_PCT, format="%.1f%%"),
    }
    table_height = min(35 * (len(out_df) + 1) + 38, 860)
    st.dataframe(
        styled,
        width="stretch",
        hide_index=True,
        height=table_height,
        column_order=[c for c in column_order if c in out_df.columns],
        column_config=column_config,
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
            "**One table + one chart per Portfolio Major.**  Hierarchical "
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


# ── 3. Entry point ────────────────────────────────────────────────────────────


def render() -> None:
    """Render the Demand Planner Analytics page.

    Flow
    ----
    1. Page header + Instructions
    2. RO Comparison                (collapsible, expanded by default)
    3. Demand Summary               (collapsible, collapsed by default)
    4. Product Line Review          (collapsible, collapsed by default)
    5. Sales Distribution Tracker   (🚚, collapsible, collapsed)
    6. Demand Planning BI Dashboard (collapsible, last — heavy iframe)
    """
    apply_custom_css()
    st.markdown(
        '<h1 class="main-header">Demand Planner Analytics</h1>',
        unsafe_allow_html=True,
    )

    _render_instructions()
    st.markdown("---")

    _render_ro_comparison()
    st.markdown("---")

    _render_demand_summary()
    st.markdown("---")

    _render_product_line_review()
    st.markdown("---")

    _render_distribution_tracker()
    st.markdown("---")

    _render_demand_planning_dashboard()
