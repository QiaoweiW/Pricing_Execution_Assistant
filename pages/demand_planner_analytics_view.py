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

from datetime import date, datetime
from typing import Callable

import pandas as pd
import streamlit as st

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
    compute_history_fingerprint,
    compute_per_format_summary,
    detect_history_change,
    fetch_dimitems_df,
    fetch_ro_history_df,
    fetch_ro_item_master_df,
    list_months,
    regenerate_comparison_output,
    save_ro_comparison_output,
    upload_customer_input,
    write_history_fingerprint,
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
#                                    ro_comparison module.
# 3. Month filters                 — Prior + LE month dropdowns sourced from
#                                    the distinct values in RO_History "Month".
# 4. Build comparison              — pure transform, cached on (Prior, LE) in
#                                    session_state to keep filter / edit
#                                    interactions zero-IO.
# 5. Warnings banner               — every recoverable issue collected by
#                                    build_ro_comparison surfaced in one place.
# 6. Field filters                 — multiselects above the table; restrict
#                                    both the visible rows and the subtotal.
# 7. Editable table                — st.data_editor with computed columns
#                                    disabled; edits to inputs trigger a live
#                                    recompute of Change / Probability / Driver.
# 8. Subtotal row                  — recomputes from the post-edit filtered
#                                    view on every rerun.
# 9. Save button                   — overwrites
#                                    Files/RO Tracking/RO_Reporting/RO_Comparison_Output.csv.

# Session-state keys.  Centralised so we never typo a key elsewhere.
_SS_SUMMARY_DF      = "_ro_cmp_summary_df"
_SS_MONTHS_SIG      = "_ro_cmp_months_sig"
_SS_WARNINGS        = "_ro_cmp_warnings"
_SS_DIMITEMS_ERROR  = "_ro_cmp_dimitems_error"
# History fingerprint of the just-fetched RO_History_Tracker.csv —
# computed once per page render and stashed so the manual Save handler
# can anchor the sidecar without re-fetching History.  Keeps "saved
# CSV in sync with this History snapshot" invariant for both auto and
# manual saves.
_SS_HISTORY_FP      = "_ro_cmp_history_fp"
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

        # 1b. Consolidated "Refresh from Fabric" button.  Click → drop
        #     every cached connector + every RO Comparison / Summary
        #     Report session key + auto-regenerate the comparison
        #     output → next render rebuilds end-to-end from the latest
        #     Fabric snapshot.  This is the ONE button to use when a
        #     planner suspects the auto-regen didn't fire (e.g.,
        #     fingerprint sidecar drift, mid-session History push).
        if st.button(
            "🔄 Refresh from Fabric",
            key="ro_cmp_refresh_from_fabric",
            help=(
                "Re-read RO_History_Tracker.csv, dp_dimitems, and "
                "RO_Item_Master.csv from Microsoft Fabric, force-rebuild "
                "the comparison + driver tables, and force-republish "
                "RO_Comparison_Output.csv.  Use this if you suspect the "
                "auto-refresh didn't pick up a new RO_History push."
            ),
        ):
            _force_refresh_from_fabric()
            st.rerun()

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

        # Cache the History fingerprint for the lifetime of this render
        # so both the auto-regen orchestrator (later in this function)
        # and the manual Save handler (in the editor fragment) anchor
        # the sidecar to the SAME snapshot.  Computed once — sub-100ms
        # for realistic History sizes.
        st.session_state[_SS_HISTORY_FP] = compute_history_fingerprint(history_df)

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
            "is left untouched; you can reload to retry, or use the "
            "**Refresh from Fabric** button at the top of the section.\n\n"
            f"Details: {exc}"
        )
        return

    # Stash the banner payload — rendered ONCE by
    # ``_render_auto_regen_banner_once`` later in the page flow.  No
    # downstream cache invalidation needed: the in-memory comparison
    # frame is rebuilt by ``_ensure_summary_in_session`` on the same
    # render, and the Summary Report consumes that in-memory frame
    # directly (see ``_render_summary_report_fragment``).
    st.session_state[_SS_AUTO_REGEN_BANNER] = result


def _force_refresh_from_fabric() -> None:
    """Clear every cache & session state tied to the RO Comparison flow.

    Called by the consolidated **🔄 Refresh from Fabric** button at
    the top of the RO Comparison section.  After this returns, the
    next render will:

      * Re-fetch ``RO_History_Tracker.csv``, ``dp_dimitems``,
        ``RO_Item_Master.csv`` (because their ``@st.cache_data`` is
        cleared upstream of this call where relevant — see button
        wiring).
      * Rebuild the in-memory comparison frame from scratch (because
        ``_SS_SUMMARY_DF`` is gone).
      * Auto-regen ``RO_Comparison_Output.csv`` if the History
        fingerprint differs (it will, because the sidecar is read
        fresh).
      * Rebuild the Summary Report from the fresh in-memory frame
        (because ``_SS_SUMMARY_REPORT_*`` keys are popped).

    Pure state-mutation — no UI rendering — so it's safe to call
    inside an ``if button_clicked:`` block followed by ``st.rerun()``.
    """
    # Clear connector caches so the next render hits Fabric.
    clear_comparison_output_cache()
    fetch_ro_history_df.clear()                                  # type: ignore[attr-defined]
    fetch_dimitems_df.clear()                                    # type: ignore[attr-defined]
    fetch_ro_item_master_df.clear()                              # type: ignore[attr-defined]

    # Drop every RO Comparison + RO Summary Report session key so the
    # next render rebuilds from scratch.  Use a tuple to keep the
    # set explicit and grep-able — DO NOT replace with a wildcard
    # iteration over session_state keys: that would also nuke other
    # pages' state on a multi-page session.
    for key in (
        # Comparison editor state
        _SS_SUMMARY_DF,
        _SS_MONTHS_SIG,
        _SS_WARNINGS,
        _SS_HISTORY_FP,
        _SS_AUTO_REGEN_SIG,
        _SS_AUTO_REGEN_BANNER,
        # Summary Report state
        _SS_SUMMARY_REPORT_DF,
        _SS_SUMMARY_REPORT_LOADED_AT,
        _SS_SUMMARY_REPORT_WARNINGS,
        _SS_SUMMARY_REPORT_RAW_DF,
        _SS_SUMMARY_REPORT_SIG,
    ):
        st.session_state.pop(key, None)


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
    # Reads from the (now-recomputed) full master frame.
    _render_per_format_summary(summary_df)

    if st.button(
        "💾 Save to RO_Reporting (overwrite)",
        key="ro_cmp_save_to_reporting",
        type="primary",
        help=(
            "Overwrites Files/RO Tracking/RO_Reporting/RO_Comparison_Output.csv "
            "with the current edited table (full table, not just the filtered view)."
        ),
    ):
        try:
            with st.spinner("Saving RO_Comparison_Output.csv to Microsoft Fabric…"):
                blob_path = save_ro_comparison_output(summary_df)
        except RoComparisonError as exc:
            st.error(f"❌ Save failed.\n\n{exc}")
        else:
            # Anchor the History fingerprint sidecar so the next page
            # load doesn't auto-regen-and-overwrite the just-saved
            # edits.  Tolerated to fail softly: a sidecar miss merely
            # triggers an auto-regen on the next reload, which is the
            # planner's stated intent for History changes.
            history_fp = st.session_state.get(_SS_HISTORY_FP, "")
            if history_fp:
                try:
                    write_history_fingerprint(history_fp)
                except RoComparisonError as exc:
                    st.warning(
                        "Saved the comparison successfully, but failed "
                        "to update the History fingerprint sidecar. The "
                        "next page reload may auto-regenerate over your "
                        f"edits.\n\nDetails: {exc}"
                    )

            st.success(f"✅ Saved to `Files/{blob_path}`.")
            # NOTE: no need to invalidate the Summary Report cache —
            # the report fragment now reads from the in-memory
            # ``_SS_SUMMARY_DF`` (which was just edited / saved) and
            # rebuilds whenever the comparison signature changes.
            # The connector cache for ``RO_Comparison_Output.csv`` is
            # cleared centrally by the top-of-section "🔄 Refresh
            # from Fabric" button when the planner explicitly wants
            # to re-read what was just published.
            clear_comparison_output_cache()


def _render_field_filters_and_apply(summary_df: pd.DataFrame) -> pd.DataFrame:
    """Render the filter multiselects and return *summary_df* narrowed.

    Empty multiselects mean "no filter on this column".  Filter state
    persists in ``st.session_state`` under stable keys so a rerun
    (caused by an edit or month change) doesn't clear the planner's
    selections.
    """
    with st.expander("🔍 Filters", expanded=False):
        cols = st.columns(3)
        for i, col_name in enumerate(_RO_FILTER_COLUMNS):
            with cols[i % 3]:
                options = sorted({
                    str(v) for v in summary_df[col_name].dropna()
                    if str(v).strip()
                })
                st.multiselect(
                    col_name,
                    options=options,
                    key=_filter_widget_key(col_name),
                )

    return _apply_filters(summary_df, _read_filter_state_from_session())


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
    """
    mask = pd.Series(True, index=df.index)
    for col, values in selections.items():
        if not values:
            continue
        mask &= df[col].astype(str).isin([str(v) for v in values])
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
    Probabilized Lbs") and the top 3 drivers by ``|Δ|`` displayed
    inline.  A TOTAL footer row reconciles to the section subtotal.

    Visual treatment
    ----------------
    * Negative Δ values in the **Δ** column are colored red, positive
      green, via ``pandas.Styler.map`` — the planner can spot the
      bleeders at a glance.
    * The TOTAL row is rendered in bold to set it apart from the
      individual-Format rows.
    * The Δ column uses ``format="accounting"`` like the rest of the
      section.  Driver cells are pre-formatted text (Item # — Desc
      (±value)), rendered as plain strings.

    Lives INSIDE the same ``@st.fragment`` as the editor + subtotal,
    so it recomputes instantly on every filter change or cell edit
    without re-touching Fabric.
    """
    summary = compute_per_format_summary(view_df)
    if summary.empty:
        return

    st.markdown(
        "**Δ Current Fiscal Probabilized Lbs — by Format**  \n"
        "_Net change and top 3 drivers per Format (drivers ranked by absolute "
        "magnitude; TOTAL row reconciles to the subtotal above)._"
    )

    is_total = summary[PER_FORMAT_FORMAT_COL].eq(PER_FORMAT_TOTAL_LABEL)

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
            "published yet.  Use **💾 Save to RO_Reporting (overwrite)** "
            "above to publish the current comparison first."
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
    4. Current Plan Overview (IBP Official) — opt-in via checkbox
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

    _render_current_plan_overview()
