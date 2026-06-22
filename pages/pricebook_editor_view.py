"""
Pricebook Editor view (integrated into the main app router).

Exposes ``render()`` so streamlit_app.py can route to it like every other
``*_view.py``. All data access is via ORDS REST — the Oracle DB sits behind
ORDS and no DB DSN is available to clients.

Architecture (single REST path):
  * READ  -> GET  {base}/{resource}/?q=...   (filtered, paged)
  * WRITE -> POST / PUT (runs the same server-side validations and triggers
             the Excel add-in relies on)

The ORDS base URL comes from .streamlit/secrets.toml -> [ords].base_url.

The page is fixed to **Price Adjustments**. The other XXICS pricebook tables
can be re-exposed behind a selector once their schema + read functions exist
(see pricebook/schemas.py -> TABLES and ords_client.read_*).
"""
import streamlit as st

from pricebook import ords_client
from pricebook.editor import render_editor
from pricebook.schemas import TABLES

# The editor is locked to Price Adjustments.
_TABLE_KEY = "priceadjs"


def render():
    st.markdown(
        '<h1 class="main-header">Pricebook Editor</h1>', unsafe_allow_html=True)
    st.caption("Read & write via ORDS REST — server-side validations and "
               "triggers run exactly as in the Excel add-in.")

    render_editor(
        TABLES[_TABLE_KEY],
        fetch_fn=ords_client.read_price_adjustments,
        # Market dropdown options sourced from distinct ORDS values (cached).
        filter_options={"market": ords_client.distinct_markets},
    )
