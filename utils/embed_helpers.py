"""Embed helpers for live SharePoint / Power BI / Office-Online resources.

This module is intentionally tiny and side-effect free.  It exists so that
multiple page views can render externally-hosted dashboards (Power BI,
SharePoint Excel, SharePoint Power BI ``.pbix`` files) with a *consistent*
look, the same fallback behaviour, and the same URL-transformation rules —
without duplicating boilerplate across views.

Why a fallback panel is always rendered
---------------------------------------
SharePoint Online and Power BI both serve their pages with
``X-Frame-Options: SAMEORIGIN`` (or a Content-Security-Policy
``frame-ancestors`` directive) for any tenant that has not explicitly
allow-listed an external embed host.  In practice that means an ``<iframe>``
embedded inside Streamlit will:

* Render normally for users whose browser session is already authenticated
  to the same tenant (the common case for Darigold employees on a managed
  laptop).
* Render *blank* for users on web / unauthenticated sessions, or whose
  browser blocks third-party cookies.

Rather than detect this at runtime (which would require client-side JS that
Streamlit does not natively support), every embed helper here ALSO renders
a clearly-labelled "Open in new tab" link so the resource is always
reachable.  This is a deliberate, conservative UX choice.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

import streamlit as st


# ── URL transformers ──────────────────────────────────────────────────────────
#
# The transformers below convert "browser-friendly" URLs that the user
# pastes from their address bar into the embed-friendly variants that
# Office Online / Power BI expect inside an iframe.  Each transformer is
# pure (no side effects, no I/O) and falls back to returning the input
# URL unchanged when it cannot recognise the format — never raises.


def to_sharepoint_excel_embed_url(url: str) -> str:
    """Return an Office-Online ``action=embedview`` URL for a SharePoint Excel link.

    Accepts the "Doc.aspx?sourcedoc={GUID}&file=...&action=default&..." form
    that SharePoint produces from the *Share → Copy link* menu and rewrites
    the ``action`` parameter to ``embedview`` so Office Online renders the
    workbook in read-only embed mode (no toolbar, no full-screen chrome).

    Falls through unchanged if the URL is not a recognised Doc.aspx link.
    """
    if "Doc.aspx" not in url:
        return url
    # Replace any existing ``action=...`` value with ``action=embedview``,
    # case-insensitively.  Preserves all other query parameters (sourcedoc,
    # file, etc.) so the embed continues to point at the correct workbook.
    rewritten = re.sub(r"action=[^&]+", "action=embedview", url, flags=re.IGNORECASE)
    if "action=" not in rewritten:
        # No action parameter at all — append one.
        sep = "&" if "?" in rewritten else "?"
        rewritten = f"{rewritten}{sep}action=embedview"
    return rewritten


# Report-viewer hosts we can rewrite into an embed URL.  Fabric and Power BI
# serve the SAME report objects off the same ``/groups/<g>/reports/<id>/<page>``
# path shape — a report opened from the Fabric portal carries an identical
# reportId — so both rewrite to ``app.powerbi.com/reportEmbed``, which is the
# only host that serves an embeddable frame.
_REPORT_VIEWER_HOSTS: frozenset = frozenset({
    "app.powerbi.com",
    "app.fabric.microsoft.com",
})


def to_powerbi_embed_url(url: str) -> Optional[str]:
    """Return a ``reportEmbed`` URL for a Power BI / Fabric report viewer link.

    Converts URLs of the form
    ``https://app.powerbi.com/groups/<group>/reports/<reportId>/<pageName>?ctid=<tenantId>...``
    — or the Fabric-portal equivalent
    ``https://app.fabric.microsoft.com/groups/<group>/reports/<reportId>/<pageName>?experience=fabric-developer``
    — into the embed-mode equivalent
    ``https://app.powerbi.com/reportEmbed?reportId=<reportId>&pageName=<pageName>&ctid=<tenantId>&autoAuth=true``.

    The ``autoAuth=true`` flag tells the Power BI service to silently
    redirect through Entra-ID SSO for users who already have an active
    tenant session — no second sign-in prompt.  Users who are *not* signed
    in will see Power BI's own sign-in page inside the frame.

    ``ctid`` is only appended when the source URL carries one; Fabric portal
    links usually don't, and omitting it lets the service resolve the tenant
    from the browser session instead of pinning the wrong one.

    Returns ``None`` if *url* does not match the expected viewer pattern,
    so the caller can fall back to rendering the raw link.
    """
    parsed = urlparse(url)
    if parsed.netloc.lower() not in _REPORT_VIEWER_HOSTS:
        return None

    # Path looks like: /groups/<group>/reports/<reportId>/<pageName>
    parts = [p for p in parsed.path.split("/") if p]
    try:
        reports_idx = parts.index("reports")
        report_id = parts[reports_idx + 1]
    except (ValueError, IndexError):
        return None
    page_name = parts[reports_idx + 2] if len(parts) > reports_idx + 2 else None

    qs = parse_qs(parsed.query)
    ctid = qs.get("ctid", [None])[0]

    embed = f"https://app.powerbi.com/reportEmbed?reportId={quote(report_id)}"
    if page_name:
        embed += f"&pageName={quote(page_name)}"
    if ctid:
        embed += f"&ctid={quote(ctid)}"
    embed += "&autoAuth=true"
    return embed


# ── Render helpers ────────────────────────────────────────────────────────────


def _render_open_in_new_tab(url: str, label: str = "Open in new tab") -> None:
    """Render a styled anchor that opens *url* in a new browser tab.

    Implemented as a raw ``<a target="_blank" rel="noopener noreferrer">``
    so the link survives Streamlit's link-rewriting and so the host page
    cannot be reverse-tabnabbed by the embedded resource.
    """
    st.markdown(
        f'''
        <div style="margin: 0.5rem 0 1rem 0;">
          <a href="{url}" target="_blank" rel="noopener noreferrer"
             style="display:inline-block;
                    background:#d32f2f; color:#fff;
                    padding:0.55rem 1.1rem; border-radius:0.5rem;
                    text-decoration:none; font-weight:600;
                    font-family:'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    box-shadow:0 2px 4px rgba(0,0,0,0.12);">
            🔗 {label}
          </a>
        </div>
        ''',
        unsafe_allow_html=True,
    )


def render_embedded_resource(
    *,
    url: str,
    title: str,
    embed_url: Optional[str] = None,
    height: int = 820,
    fallback_note: Optional[str] = None,
) -> None:
    """Render a live, interactive embed of an external resource.

    Layout (top → bottom):

      1. Optional small note explaining what the user is looking at and
         what to do if the embed is blank.
      2. A red "Open in new tab" button (always present).
      3. The ``<iframe>`` itself, sized to *height* pixels.

    Parameters
    ----------
    url
        The canonical, user-shareable URL (used by the "Open in new tab"
        button and shown in the fallback note).
    title
        Human-readable title used as the iframe ``title`` attribute for
        accessibility.
    embed_url
        Optional URL to embed inside the iframe.  When omitted, the
        canonical *url* is embedded directly.  Pass the output of
        :func:`to_sharepoint_excel_embed_url` or
        :func:`to_powerbi_embed_url` here to force read-only embed mode.
    height
        Iframe height in pixels.  Defaults to a value that comfortably
        accommodates a typical Power BI report at 1080p.
    fallback_note
        Optional override for the small italicised note rendered above
        the button.  When ``None``, a sensible default is used.
    """
    note = fallback_note or (
        "If the embedded view below appears blank, your browser session "
        "may not be authenticated to the host tenant. Use the button "
        "below to open the resource directly in a new tab."
    )
    st.markdown(
        f'<div style="font-size:0.85rem; color:#555; margin-bottom:0.25rem;">'
        f'<em>{note}</em></div>',
        unsafe_allow_html=True,
    )
    _render_open_in_new_tab(url)

    # st.components.v1.iframe is preferred over a raw <iframe> because it
    # adds a sandboxed wrapper and handles HiDPI sizing on Streamlit Cloud.
    import streamlit.components.v1 as components  # local import: rarely used

    components.iframe(
        src=embed_url or url,
        height=height,
        scrolling=True,
    )


__all__ = [
    "to_sharepoint_excel_embed_url",
    "to_powerbi_embed_url",
    "render_embedded_resource",
]
