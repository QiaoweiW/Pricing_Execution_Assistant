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

from typing import Callable

import pandas as pd
import streamlit as st

from data_sources.ibp_official import (
    IBPOfficialSourceError,
    IBPSnapshotMeta,
    fetch_ibp_orders_df,
    fetch_ibp_shipments_df,
)
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

    _render_current_plan_overview()
