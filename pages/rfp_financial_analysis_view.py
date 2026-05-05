"""RFP Financial Analysis page view.

Sections
--------
1. Source URLs           (module-level constant)
2. Section renderers     (_render_instructions,
                          _render_finance_pnl_dashboard)
3. Entry point           (render)

Page layout
-----------
1. Page header + Instructions block (placeholder content "TBD").
2. ── divider ──
3. Foldable: "Finance P&L" — embeds the live Power BI report so the
   user can interact with it (slicers, drill-down, etc.) without
   leaving the app.
"""
from __future__ import annotations

import streamlit as st

from utils.embed_helpers import (
    render_embedded_resource,
    to_powerbi_embed_url,
)
from utils.ui_helpers import apply_custom_css


# ── 1. Source URLs ────────────────────────────────────────────────────────────
#
# Canonical, share-friendly Power BI viewer URL.  Kept as a module-level
# constant so it can be updated in one place if Finance ever publishes
# the report under a new ID.

_FINANCE_PNL_REPORT_URL = (
    "https://app.powerbi.com/groups/me/reports/"
    "ff2d4ea3-d3e4-4a14-945d-998bb7a7f03d/ef0f92c30868546c301b"
    "?ctid=c9a55ced-3b88-408c-ab99-8db8b9b90286&experience=power-bi"
)


# ── 2. Section renderers ──────────────────────────────────────────────────────


def _render_instructions() -> None:
    """Render the static instructions block at the top of the page."""
    st.markdown("### 📋 Instructions")
    st.markdown("TBD")


def _render_finance_pnl_dashboard() -> None:
    """Embed the Finance P&L Power BI report.

    The Power BI service serves its viewer URL with an X-Frame-Options
    header that blocks embedding from arbitrary origins.  To bypass
    that we rewrite the URL into the ``reportEmbed`` form via
    :func:`utils.embed_helpers.to_powerbi_embed_url`, which appends
    ``autoAuth=true`` so the embedded frame silently completes the
    Entra-ID handshake for users with an active tenant session.
    """
    with st.expander("💰 Finance P&L", expanded=False):
        embed_url = to_powerbi_embed_url(_FINANCE_PNL_REPORT_URL)
        render_embedded_resource(
            url=_FINANCE_PNL_REPORT_URL,
            title="Finance P&L (Power BI)",
            embed_url=embed_url,
            height=900,
            fallback_note=(
                "This is the live Finance P&L Power BI report. The frame "
                "below uses Power BI's embed mode with Entra-ID auto-auth, "
                "but tenant SSO policy may still require an interactive "
                "sign-in. If the frame is blank, use the button below to "
                "open the report directly in Power BI."
            ),
        )


# ── 3. Entry point ────────────────────────────────────────────────────────────


def render() -> None:
    """Render the RFP Financial Analysis page.

    Flow
    ----
    1. Page header + Instructions
    2. Finance P&L Power BI dashboard (collapsible, collapsed by default)
    """
    apply_custom_css()
    st.markdown(
        '<h1 class="main-header">RFP Financial Analysis</h1>',
        unsafe_allow_html=True,
    )

    _render_instructions()
    st.markdown("---")

    _render_finance_pnl_dashboard()
