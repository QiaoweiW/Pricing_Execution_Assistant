"""Demand Planner Analytics page view.

Sections
--------
1. Source URLs                    (module-level constants)
2. Section renderers              (_render_instructions,
                                   _render_demand_planning_dashboard,
                                   _render_distribution_tracker,
                                   _render_current_plan_overview,
                                   _render_ibp_table)
3. Entry point                    (render)

Page layout
-----------
1. Page header + Instructions block (placeholder content "TBD").
2. ── divider ──
3. Foldable: "Demand Planning BI Dashboard" — embeds the SharePoint
   ``.pbix`` file so the user can interact with the live model.
4. ── divider ──
5. Foldable: "New Distribution Tracker" — embeds the SharePoint Excel
   workbook in Office-Online read-mode.
6. ── divider ──
7. "Current Plan Overview (IBP Official)" — opt-in checkbox that, when
   ticked, pulls the dbo.IBP Orders and dbo.IBP Shipments Delta tables
   from the Microsoft Fabric Lakehouse.

Why every external resource gets its own foldable section
---------------------------------------------------------
The three external dashboards each render a heavy ``<iframe>`` that
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
    TOTAL_BUDGET_COLUMN_LABEL,
    TOTAL_COLUMN_LABEL,
    build_budget_lookup,
    build_demand_pivot,
    build_supply_format_lookup,
    clear_demand_summary_cache,
    fetch_mgmt_plan_full,
    fetch_pdh,
    fetch_raw_bytes as fetch_demand_summary_raw_bytes,
    fetch_static_budget_base,
    fetch_static_budget_ro,
    fetch_total_item_level_demand,
    list_available_filter_values,
    mgmt_plan_full_blob_path,
    pivot_for_download,
    total_item_level_demand_blob_path,
)
from data_sources.ibp_official import (
    IBPOfficialSourceError,
    IBPSnapshotMeta,
    fetch_ibp_orders_df,
    fetch_ibp_shipments_df,
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
    list_months,
    regenerate_comparison_output,
    upload_customer_input,
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
)
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

# SharePoint-hosted Excel workbook — New Distribution Tracker.
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
    st.markdown("TBD")


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
    """Embed the SharePoint Excel workbook (Office-Online read-mode)."""
    with st.expander("📦 New Distribution Tracker (Corporate Group)", expanded=False):
        render_embedded_resource(
            url=_DISTRIBUTION_TRACKER_URL,
            title="New Distribution Tracker — Corporate Group",
            # Rewrite ``action=default`` → ``action=embedview`` so Office
            # Online renders a chrome-less, read-only embed.
            embed_url=to_sharepoint_excel_embed_url(_DISTRIBUTION_TRACKER_URL),
            height=820,
            fallback_note=(
                "This is the live New Distribution Tracker workbook hosted "
                "in SharePoint, rendered through Office Online in read-only "
                "embed mode. Use the button below to open it in Excel "
                "Online with full editing rights."
            ),
        )


def _render_ibp_table(
    title: str,
    icon: str,
    fetch_fn: Callable[..., tuple[pd.DataFrame, IBPSnapshotMeta]],
    *,
    force_refresh: bool,
    download_basename: str,
) -> None:
    """Render a single IBP table fetched from the Fabric Lakehouse.

    Parameters
    ----------
    title
        Human-readable heading shown above the data preview (e.g.
        "IBP Orders").
    icon
        Single emoji used as a leading badge in the heading.
    fetch_fn
        One of :func:`fetch_ibp_orders_df` or
        :func:`fetch_ibp_shipments_df` — the connector function that
        returns ``(df, meta)``.
    force_refresh
        Forwarded straight through to *fetch_fn* — true when the user
        clicked the "Refresh from Fabric" button on this rerun.
    download_basename
        Filename stem used for the CSV download button (no extension,
        no date — the function appends both).
    """
    st.markdown(f"#### {icon} {title}")
    try:
        with st.spinner(f"Reading {title} from Microsoft Fabric…"):
            df, meta = fetch_fn(force_refresh=force_refresh)
    except IBPOfficialSourceError as exc:
        st.error(f"❌ Could not load {title} from the Fabric Lakehouse.\n\n{exc}")
        return

    last_mod = (
        meta.last_modified.strftime("%Y-%m-%d %H:%M UTC")
        if meta.last_modified else "unknown"
    )
    st.caption(
        f"🛰️ {title} **as of {last_mod}** · "
        f"Delta version **v{meta.version}** · "
        f"**{meta.row_count:,}** rows · **{len(df.columns)}** columns"
    )

    # Preview the first N rows only — full DataFrames can be tens of
    # millions of rows; pushing them all to the browser would freeze
    # the tab.  Users who want everything can hit the download button.
    preview_rows = 200
    st.dataframe(df.head(preview_rows), width="stretch", height=320)
    if len(df) > preview_rows:
        st.caption(
            f"_Showing the first {preview_rows:,} rows. "
            f"Use the download button below for the full dataset._"
        )

    # CSV download streams over HTTP, not the WebSocket — there is no
    # client-side cap on payload size, so this works for arbitrarily
    # large snapshots (memory permitting on the server side).
    today = pd.Timestamp.utcnow().strftime("%Y%m%d")
    st.download_button(
        label=f"⬇️ Download {title} (CSV)",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=f"{download_basename}_{today}.csv",
        mime="text/csv",
        key=f"ibp_{download_basename}_csv_download",
        help=f"Full {title} snapshot from the Fabric Lakehouse Delta table.",
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
# 3. Month filters                 — Prior + LE month dropdowns sourced from
#                                    the distinct values in RO_History "Month".
# 4. Auto-regen                    — when the RO_History ETag advances,
#                                    `RO_Comparison_Output.csv` is silently
#                                    regenerated + republished to Fabric;
#                                    every downstream section (Early-Start
#                                    Programs, Summary Report) reads from
#                                    that same in-memory frame so the
#                                    cascade is automatic.
# 5. Field filters + Editable table + Subtotal + per-Format drivers
#                                  — wrapped in a single @st.fragment for
#                                    sub-second filter / edit interactions.

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

        # 1. Upload control — fully independent of the comparison flow
        #    below so a planner can drop a new Customer Input CSV even
        #    if there is a downstream Fabric outage.  The Save action
        #    itself surfaces its own auth error if sign-in is missing.
        _render_customer_input_uploader()

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

        # 3. Month filters.
        months = list_months(history_df)
        if len(months) < 2:
            st.warning(
                "RO_History_Tracker.csv has fewer than 2 distinct Month values "
                f"({len(months)} found) — need at least 2 to build a comparison."
            )
            return
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

        # 6-9. Filters + editor + subtotal + per-Format summary + Save.
        #      All wrapped in a single ``@st.fragment`` so a filter
        #      change / cell edit / Save click re-runs ONLY this block
        #      — no Fabric I/O, no comparison rebuild, no warnings
        #      banner re-render.  Filter latency drops from O(seconds)
        #      to sub-second.
        _render_filtered_editor_fragment(prior_month, le_month)

        # 10. Early-Start-Date Programs — drilldown of the published
        #     ``RO_Comparison_Output.csv``.  Sits between the per-
        #     Format driver table (last thing in the editor fragment)
        #     and the RO Summary Report below.  Reads from Fabric
        #     directly (not from ``_SS_SUMMARY_DF``) so it always
        #     reflects the planner's last *saved* baseline — the
        #     same "approved view" semantics the Summary Report uses
        #     for its Fabric snapshot.  Wrapped in its own fragment
        #     so the Format / cutoff-date widgets rerun only this
        #     section.
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

def _render_customer_input_uploader() -> None:
    """Render the "Upload Customer Input CSV" control row.

    The uploaded file is saved verbatim (no transformation) to
    ``Files/RO Tracking/Append_New_History/<original-filename>`` so the
    downstream Fabric pipeline can ingest it on its usual cadence.

    Independence guarantee: this block is fully self-contained — the
    summary table below renders whether or not anything has been
    uploaded.  See the caption on the heading.
    """
    # Bigger-than-h4 heading per UX direction.  Using inline HTML so we
    # can hit a 1.35rem size that sits clearly above the rest of the
    # section's body text but below the section's h3 heading.
    st.markdown(
        "<h4 style='font-size:1.35rem; margin-top:0.25rem;'>"
        "📤 Save the 'Customer Input' table in the \"Distribution Tracker New\" "
        "as csv file to Upload here"
        "</h4>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Optional — uploading here only affects the Append_New_History drop "
        "zone.  The summary table below loads from RO_History_Tracker.csv "
        "independently and is always visible."
    )
    uploaded = st.file_uploader(
        "Save the 'Customer Input' table in the \"Distribution Tracker New\" as csv file to Upload here",
        type=["csv"],
        key="ro_cmp_customer_input_upload",
        label_visibility="collapsed",
        help=(
            "Pick the local CSV (typically named "
            "\"Distribution Tracker New 'Customer Input'.csv\"). "
            "On Save it lands in Files/RO Tracking/Append_New_History/ "
            "under its original filename — Fabric never sees the file "
            "until you click Save."
        ),
    )

    save_clicked = st.button(
        "💾 Save to Fabric",
        key="ro_cmp_customer_input_save",
        type="primary",
        disabled=uploaded is None,
        help="Uploads the selected CSV to the Append_New_History folder in Fabric.",
    )

    if uploaded is None or not save_clicked:
        return

    try:
        with st.spinner(f"Uploading '{uploaded.name}' to Microsoft Fabric…"):
            blob_path = upload_customer_input(uploaded.name, uploaded.getvalue())
    except RoComparisonError as exc:
        st.error(f"❌ Upload failed.\n\n{exc}")
        return

    st.success(f"✅ Uploaded `{uploaded.name}` to `Files/{blob_path}`.")


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
        _SS_SUMMARY_REPORT_SIG,
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
    """
    st.info(
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
        "forces a fresh read.",
        icon="ℹ️",
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
    """Render the filter widgets + editor + subtotal + per-Format summary + Save.

    Why this is a fragment
    ----------------------
    Filter / edit / Save interactions used to trigger a full page
    rerun, which re-executed the upload control, re-fetched
    RO_History + dp_dimitems from Fabric (cache-hit but still pays
    the cache key check), re-ran ``build_ro_comparison``, and
    re-rendered the warnings banner — every time the planner
    clicked anything in this block.  Wrapping the interactive part
    in a fragment (Streamlit ≥ 1.33) scopes the rerun to just this
    function: changing a multiselect / editing a cell / clicking
    Save only re-renders the widgets contained here.  Anything
    OUTSIDE the fragment (upload, month pickers, warnings) only
    reruns on its own widget changes, which is exactly what we want.

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

    filtered_df = _render_field_filters_and_apply(summary_df)
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

    # No manual Save button: the auto-regen path republishes
    # ``RO_Comparison_Output.csv`` to Fabric automatically whenever
    # the upstream ``RO_History_Tracker.csv`` ETag advances (see
    # ``_maybe_auto_regenerate_comparison_output``).  Planner edits
    # in the table above are session-scoped — useful for in-tab
    # exploration / driver inspection, intentionally not persisted
    # to Fabric (that's the upstream pipeline's job).


# Sentinel option surfaced inside every filter multiselect — picking
# it narrows the view to rows where the column is blank (NaN, empty
# string, or whitespace-only).  Centralised so the multiselect option
# list, the help text, and the apply-filter path all stay in lockstep.
# Spelled with parentheses + lowercase to avoid colliding with any
# real value in any of the filterable columns.
_BLANK_FILTER_SENTINEL: str = "(blank)"


def _render_field_filters_and_apply(summary_df: pd.DataFrame) -> pd.DataFrame:
    """Render the filter multiselects and return *summary_df* narrowed.

    Empty multiselects mean "no filter on this column".  Filter state
    persists in ``st.session_state`` under stable keys so a rerun
    (caused by an edit or month change) doesn't clear the planner's
    selections.

    Blank-cell handling
    -------------------
    Every column whose source frame has at least one blank cell
    (NaN / empty string / whitespace) exposes a special
    ``"(blank)"`` option at the top of its multiselect.  Picking it
    narrows the view to *exactly those blank rows* — the canonical
    way for a planner to find e.g. items missing a Portfolio Minor
    that need triaging.  See :data:`_BLANK_FILTER_SENTINEL`.
    """
    with st.expander("🔍 Filters", expanded=False):
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

    return _apply_filters(summary_df, _read_filter_state_from_session())


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
            report_df, report_warnings = build_summary_report(summary_df)
        except RoSummaryReportError as exc:
            st.error(f"❌ Could not build the RO Summary Report.\n\n{exc}")
            return
        st.session_state[_SS_SUMMARY_REPORT_DF]        = report_df
        st.session_state[_SS_SUMMARY_REPORT_WARNINGS]  = report_warnings
        st.session_state[_SS_SUMMARY_REPORT_LOADED_AT] = datetime.now()
        st.session_state[_SS_SUMMARY_REPORT_RAW_DF]    = summary_df.copy()
        st.session_state[_SS_SUMMARY_REPORT_SIG]       = months_sig

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
        st.info(
            "Every row is zero — nothing to display.  Tick **Show empty "
            "rows** to edit the template directly, or change the Prior / "
            "LE pickers above to a month pair with comparison data."
        )
        return

    edited_view = st.data_editor(
        view_df,
        key="ro_sr_editor",
        num_rows="fixed",
        use_container_width=True,
        # Header (38) + per-row (35) sized to fit the whole template
        # without scrolling for the common ~10-30 visible row range,
        # capped so a huge template doesn't dominate the page.
        height=min(35 * (len(view_df) + 1) + 38, 900),
        column_config=_summary_report_column_config(),
        column_order=_summary_report_column_order(),
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
    merged = recompute_subtotals(merged)
    st.session_state[_SS_SUMMARY_REPORT_DF] = merged

    # ── Save button ──────────────────────────────────────────────
    if st.button(
        "💾 Save RO_Summary_Report.csv (overwrite)",
        key="ro_sr_save",
        type="primary",
        help=(
            "Overwrites `Files/RO Tracking/RO_Reporting/RO_Summary_Report.csv` "
            "with the FULL 30-row template (subtotals + every leaf, including "
            "all-zero rows) so downstream consumers get a stable shape."
        ),
    ):
        try:
            with st.spinner("Saving RO_Summary_Report.csv to Microsoft Fabric…"):
                blob_path = save_ro_summary_report(merged)
        except RoSummaryReportError as exc:
            st.error(f"❌ Save failed.\n\n{exc}")
        else:
            st.success(f"✅ Saved to `Files/{blob_path}`.")


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
    convention the rest of the page uses (see
    :func:`_render_ibp_table`).
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

        # Consolidated "Refresh from Fabric" button.  One click clears
        # BOTH cached snapshots so the planner gets a consistent re-read
        # pair — matches the refresh model the RO Comparison section
        # uses ("🔄 Refresh from Fabric") so the two sections feel
        # uniform.
        if st.button(
            "🔄 Refresh from Fabric",
            key="demand_summary_refresh_from_fabric",
            help=(
                "Re-read both `qry_mgmt_plan_full.csv` and "
                "`qry_total_item_level_demand.csv` from Microsoft Fabric "
                "now — bypasses the 15-minute cache."
            ),
        ):
            clear_demand_summary_cache()
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
    """Return the annual-budget lookup for the Demand Pivot Summary.

    Composes the two static-budget CSVs — ``Static_Budget_Base_Lbs.csv``
    and ``Static_Budget_RO_Lbs.csv`` — into a single
    :class:`BudgetLookup` via :func:`build_budget_lookup`.  Both reads
    are wrapped in try/except so a transient outage on either source
    degrades the cascade gracefully:

    * Base unavailable → R&O budget alone fills the Total Budget column.
    * RO unavailable   → Base budget alone fills the Total Budget column.
    * Both unavailable → empty lookup; :attr:`BudgetLookup.has_data`
      reports ``False`` and the page suppresses both the column and
      the chart's dotted budget line.

    Failure logs stay INFO-level (not warnings) because a missing
    static-budget file is recoverable — every other section on the
    page keeps working, and the planner has plenty of other visual
    cues that the budget is unavailable (an absent column, an absent
    chart line).
    """
    # Base tier.
    try:
        base_snapshot = fetch_static_budget_base()
        base_df = base_snapshot.df
    except DemandSummaryError as exc:
        logger.info(
            "Static_Budget_Base_Lbs.csv unavailable for the Demand Pivot "
            "budget lookup (will fall back to RO-only budget): %s",
            exc,
        )
        base_df = None

    # R&O tier.
    try:
        ro_snapshot = fetch_static_budget_ro()
        ro_df = ro_snapshot.df
    except DemandSummaryError as exc:
        logger.info(
            "Static_Budget_RO_Lbs.csv unavailable for the Demand Pivot "
            "budget lookup (will fall back to Base-only budget): %s",
            exc,
        )
        ro_df = None

    return build_budget_lookup(base_df, ro_df)


def _render_demand_pivot_section() -> None:
    """Render the Demand Pivot Summary header + delegate to the fragment.

    The header (title + caption) stays OUTSIDE the fragment because
    it's static text — the fragment owns only the interactive widgets
    + table + chart so filter / date-range changes rerun only the
    pivot view, not the surrounding headers.
    """
    st.markdown("### 📊 Demand Pivot Summary")
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

    # 2b. Build the per-leaf annual budget lookup from the two
    #     static-budget CSVs (Base + R&O).  Same graceful-degradation
    #     story as the Supply Format cascade — a missing source
    #     simply leaves that tier blank in the Total Budget column.
    budget_lookup = _build_demand_pivot_budget_lookup()

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
                "Annual budget for this row (in millions of lbs), "
                "sourced from `Static_Budget_Base_Lbs.csv` (Base Plan "
                "rows) and `Static_Budget_RO_Lbs.csv` (R&O rows). "
                "Aggregated at the row's natural level — leaves show "
                "the per-(PMaj, SFmt) budget; subtotals sum their "
                "children; Grand Total bundles every visible row."
            ),
        )
    return config


def _render_demand_pivot_table(result: DemandPivotResult) -> None:
    """Render the pivot table + dynamic footer subtotals + download button.

    The pivot itself is rendered with :func:`st.dataframe` (not
    :func:`st.data_editor`) because the planner doesn't edit this
    view — it's a pure roll-up of the source CSV.  ``column_order``
    pins the Row Label first, then every month in ascending order,
    then the Total column, then the Total Budget column on the
    far right (mirrors the Excel screenshot column layout exactly,
    with the budget appended per the planner's direction).
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
            "on-screen layout, including the Total Budget column. "
            "Honours your active filters."
        ),
    )

    # ── Column layout: Row Label · months ascending · Total · Total Budget ─
    column_order: list[str] = [
        "Row Label", *result.month_columns, TOTAL_COLUMN_LABEL,
    ]
    if result.has_budget_data:
        column_order.append(TOTAL_BUDGET_COLUMN_LABEL)

    column_config = _demand_pivot_column_config(
        result.month_columns, include_budget=result.has_budget_data,
    )

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
        column_config=column_config,
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
        column_config=column_config,
    )
    if result.has_budget_data:
        st.caption(
            f"💰 **Bundled Total Budget (Base + R&O):** "
            f"**{result.budget_total_m:,.1f} M lbs** "
            "(annual; same figure shown as the dotted line on the "
            "Base + RO Summary chart below)."
        )


def _render_base_ro_summary_chart(result: DemandPivotResult) -> None:
    """Render the Base + RO Summary stacked area chart + Total Budget line.

    Mirrors the screenshot the planner shared: Base Plan stacked on
    the bottom (dark blue), R&O on top (orange), x-axis = month,
    y-axis = millions of pounds.  When a Total Budget value is
    available (see :attr:`DemandPivotResult.has_budget_data`), a
    bright dotted line is overlaid at the **monthly** budget level
    — that's the annual bundled (Base + R&O) budget divided by the
    number of months in the filter window — so a planner can see at
    a glance whether each month's actual demand is tracking above or
    below the budget target.

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
            "of pounds, with a dotted line at the **monthly Total "
            "Budget** target (annual Base + R&O budget ÷ months in "
            "the filter window).  Updates live with the filter "
            "selections above."
        )
    else:
        st.caption(
            "Stacked area of monthly Base Plan + R&O totals in millions "
            "of pounds.  Updates live with the filter selections above. "
            "_Total Budget line unavailable — the static-budget CSVs "
            "could not be read from Fabric._"
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

    # Total Budget overlay — bright dotted line at the monthly budget
    # level (annual ÷ months).  NOT inside the stackgroup so it sits
    # on top of the areas instead of summing into them.  Coloured a
    # high-contrast magenta + 3 px stroke + dot pattern so it stays
    # readable against the dark blue Base Plan fill underneath.
    n_months = len(wide.index)
    if result.has_budget_data and n_months > 0:
        monthly_budget_m = float(result.budget_total_m) / float(n_months)
        fig.add_trace(go.Scatter(
            x=x_labels,
            y=[monthly_budget_m] * n_months,
            name=(
                f"Total Budget (monthly): {monthly_budget_m:,.1f} M lbs · "
                f"annual: {result.budget_total_m:,.1f} M lbs"
            ),
            mode="lines",
            # No stackgroup → drawn as an overlay reference line.
            line=dict(
                color="#d62728",            # bright red — high contrast
                width=3,
                dash="dot",
            ),
            hovertemplate=(
                "<b>%{x}</b><br>"
                "Total Budget (monthly): %{y:.1f} M lbs<extra></extra>"
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


def _render_current_plan_overview() -> None:
    """Render the Current Plan Overview (IBP Official) opt-in section.

    The IBP Official path requires interactive Azure sign-in (browser
    popup) to acquire a OneLake storage token.  Streamlit Cloud is
    headless — the popup cannot open — so this is gated behind an opt-
    in checkbox with a loud warning to keep web users away from it.
    The same gating pattern is used by the HTST Activity Monitor
    page; keeping the wording consistent here helps users build a
    single mental model for the toggle.
    """
    st.markdown("### 📈 Current Plan Overview (IBP Official)")

    use_dataflow = st.checkbox(
        "Use Microsoft Fabric Dataflow as the source — "
        "**DO NOT CLICK IF YOU ARE A WEB USER**",
        value=False,
        key="demand_planner_use_fabric",
        help=(
            "Default (unchecked): no data is fetched. The Current Plan "
            "Overview remains collapsed.\n\n"
            "Checked: pull the dbo.IBP Orders and dbo.IBP Shipments "
            "Delta tables directly from the Microsoft Fabric Lakehouse. "
            "Requires interactive Azure sign-in or a service principal — "
            "only works in a local desktop session, not on a headless "
            "Streamlit Cloud server."
        ),
    )

    if not use_dataflow:
        # Nothing to do until the user opts in — show a brief hint and
        # return early so we don't accidentally trigger an auth prompt.
        st.info(
            "Tick the checkbox above to load the latest IBP Orders and "
            "IBP Shipments tables from the Microsoft Fabric Lakehouse."
        )
        return

    # The refresh button must be rendered BEFORE the connector calls so
    # that a click on this rerun clears the cache for both tables in one
    # pass (otherwise the Orders table would be served from cache while
    # only Shipments is refreshed, leaving the two desynchronised).
    refresh_clicked = st.button(
        "🔄 Refresh from Fabric",
        key="ibp_official_refresh",
        help="Bypass the 15-minute cache and re-read both Delta tables now.",
    )

    _render_ibp_table(
        title="IBP Orders",
        icon="📦",
        fetch_fn=fetch_ibp_orders_df,
        force_refresh=refresh_clicked,
        download_basename="IBP_Orders",
    )
    st.markdown("")  # vertical breathing room between the two tables
    _render_ibp_table(
        title="IBP Shipments",
        icon="🚚",
        fetch_fn=fetch_ibp_shipments_df,
        # The Orders fetch above already cleared the cache for both
        # tables when refresh_clicked was True — passing False here
        # avoids a redundant clear() on a fresh cache slot.
        force_refresh=False,
        download_basename="IBP_Shipments",
    )


# ── 3. Entry point ────────────────────────────────────────────────────────────


def render() -> None:
    """Render the Demand Planner Analytics page.

    Flow
    ----
    1. Page header + Instructions
    2. Demand Planning BI Dashboard (collapsible, collapsed by default)
    3. New Distribution Tracker     (collapsible, collapsed by default)
    4. RO Comparison                (collapsible, expanded by default)
    5. Demand Summary               (collapsible, collapsed by default)
    6. Current Plan Overview (IBP Official) — opt-in via checkbox
    """
    apply_custom_css()
    st.markdown(
        '<h1 class="main-header">Demand Planner Analytics</h1>',
        unsafe_allow_html=True,
    )

    _render_instructions()
    st.markdown("---")

    _render_demand_planning_dashboard()
    st.markdown("---")

    _render_distribution_tracker()
    st.markdown("---")

    _render_ro_comparison()
    st.markdown("---")

    _render_demand_summary()
    st.markdown("---")

    _render_current_plan_overview()
