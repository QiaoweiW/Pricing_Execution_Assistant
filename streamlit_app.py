# streamlit_app.py - Darigold Pricing VBCS Generation Tool
"""
Main Streamlit application entry point.
This file handles routing and page navigation only.
All page-specific UI code is in the pages/ directory.

Diagnostics
-----------
Everything in this module logs via the stdlib ``logging`` module — never
``print()``.  On Windows, ``print()`` raises ``OSError: [Errno 22] Invalid
argument`` whenever stdout is closed/redirected (Python bug 35754 — the
Windows equivalent of ``BrokenPipeError``).  Because Streamlit reruns
the script on every widget interaction, a single misbehaving ``print``
in the router can surface as a confusing red traceback for the user on
every filter click.  ``logging`` writes to stderr (managed by Streamlit's
own log handler), so the same diagnostics flow into the same terminal /
``streamlit.log`` without the stdout fragility.
"""
import logging
import warnings

import streamlit as st

warnings.filterwarnings('ignore')

import importlib
from pathlib import Path

from utils.ui_helpers import apply_custom_css, render_footer


logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    # Streamlit installs its own root handler in normal runs, but when the
    # module is imported by a unit test or a CLI utility the root logger
    # may be empty — surface our diagnostics in that case too.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Dynamic view discovery - only load views that exist in pages directory
PAGES_DIR = Path(__file__).parent / "pages"

# Mapping of view file names to display names.
# Only views listed here are eligible for sidebar navigation.
VIEW_NAME_MAPPING = {
    "home_view":                          "Home & Fabric Sign-in",
    "bid_asset_intelligence_view":        "Bid Assistant",
    "htst_activity_monitor_view":         "Shipment Monitor & HTST Requote",
    "new_price_quote_view":               "New Price Quote",
    "market_barometer_view":              "Market Barometer",
    "pricing_execution_automation_view":  "Pricing Execution Automation",
    "pricing_granularity_view":           "Pricing Granularity",
    "unit_economics_view":                "Unit Economics",
    "demand_view":                        "Demand Insight",
    "demand_planner_analytics_view":      "Demand Planner Analytics",
    "rfp_financial_analysis_view":        "RFP Financial Analysis",
}

# Views temporarily hidden from the sidebar without being deleted.
# Add a view's file-stem here to suppress its navigation button while keeping
# the module available for import by other pages.
HIDDEN_VIEWS: set = set()

# Discover available views dynamically
AVAILABLE_VIEWS = {}
PAGE_ROUTER = {}

for view_file in PAGES_DIR.glob("*_view.py"):
    view_name = view_file.stem  # e.g., "home_view"

    if view_name in VIEW_NAME_MAPPING and view_name not in HIDDEN_VIEWS:
        try:
            # Dynamically import the view module
            module = importlib.import_module(f"pages.{view_name}")
            
            # Check if it has a render function
            if hasattr(module, 'render'):
                display_name = VIEW_NAME_MAPPING[view_name]
                AVAILABLE_VIEWS[display_name] = view_name
                PAGE_ROUTER[display_name] = module.render
                logger.info("Loaded view: %s (%s)", display_name, view_name)
            else:
                logger.warning("View %s does not expose a render() function", view_name)
        except Exception as exc:  # noqa: BLE001  defensive top-level guard
            logger.exception("Error loading view %s: %s", view_name, exc)

logger.info("Total views loaded: %d", len(AVAILABLE_VIEWS))

# Page configuration
st.set_page_config(
    page_title="Darigold Pricing Tools",
    page_icon="🔧",
    layout="wide",
    initial_sidebar_state="expanded"
)


# Apply custom CSS
apply_custom_css()


# ── Microsoft Fabric warm-up ─────────────────────────────────────────────────
#
# Eagerly acquire the OneLake bearer token + initialise the shared DuckDB
# connection ONCE per Streamlit session. Two reasons this lives here, not
# inside each page:
#
#   1. Auth is consolidated to a single moment with explicit progress UI,
#      so the rare browser sign-in happens up front — not hidden behind a
#      mid-session navigation click.
#   2. The DuckDB ``LOAD azure`` / ``LOAD delta`` cost (~300 ms) is paid
#      exactly once per process; every subsequent Delta-table fetch on
#      any page is hot.
#
# Best-effort: failures DO NOT block the app. A user without Fabric
# access can still use the local-only pages (Home, Pricing Granularity,
# etc.); pages that need Fabric will surface their own contextual error
# when the user navigates to them.
if "fabric_warm" not in st.session_state:
    try:
        from data_sources import fabric_auth
        with st.status("Connecting to Microsoft Fabric…", expanded=False) as warm_status:
            try:
                fabric_auth.warmup()
                warm_status.update(
                    label="Microsoft Fabric ready.",
                    state="complete",
                    expanded=False,
                )
            except fabric_auth.FabricAuthError as exc:
                # Pages that need Fabric will render their own contextual
                # error when the user opens them; we don't gate the app.
                warm_status.update(
                    label=(
                        "Microsoft Fabric not connected — pages that need it "
                        "will prompt for sign-in on first use."
                    ),
                    state="error",
                    expanded=False,
                )
                logger.warning("Fabric warm-up failed (non-fatal): %s", exc)
            except Exception as exc:  # noqa: BLE001  defensive top-level guard
                warm_status.update(
                    label=f"Microsoft Fabric warm-up errored ({exc}).",
                    state="error",
                    expanded=False,
                )
                logger.exception("Fabric warm-up errored (non-fatal): %s", exc)
    finally:
        # Set the flag regardless of outcome — we only ever try ONCE per
        # session. A failed warm-up does NOT permanently disable Fabric;
        # any later page that calls into a Fabric connector will simply
        # acquire the token lazily (the same legacy behaviour) and pop
        # the sign-in then.
        st.session_state["fabric_warm"] = True

# --- Sidebar Navigation ---
with st.sidebar:
    st.markdown("""
    <div style="text-align: center; padding: 1.5rem 0;">
        <h1 style="color: #d32f2f; margin: 0; font-size: 1.8rem; font-weight: 700; letter-spacing: 0.5px; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;">Darigold Pricing Intelligence</h1>
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("---")
    
    # Page selection buttons - dynamically generated from available views
    # Sort views to ensure Home is first, then alphabetical order
    sorted_views = sorted(AVAILABLE_VIEWS.keys())
    if "Home & Fabric Sign-in" in sorted_views:
        sorted_views.remove("Home & Fabric Sign-in")
        sorted_views.insert(0, "Home & Fabric Sign-in")
    
    for display_name in sorted_views:
        # Create a safe key from the display name
        key_safe = display_name.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("-", "_")
        button_key = f"{key_safe}_btn"
        
        # Special handling for Pricing Execution Automation button text
        button_text = display_name
        if display_name == "Pricing Execution Automation":
            button_text = "**Pricing Execution Automation (for RGM)**"
        else:
            button_text = f"**{display_name}**"
        
        if st.button(button_text, width='stretch', type="primary", key=button_key):
            st.session_state.selected_page = display_name
    
    st.markdown("---")
    
    # Add some additional info
    st.markdown("""
    <div style="font-size: 0.8rem; color: #666; text-align: center; margin-top: 2rem;">
        <p>Darigold Pricing Team</p>
        <p>Version 2.0</p>
    </div>
    """, unsafe_allow_html=True)

# Initialize session state if not exists
if 'selected_page' not in st.session_state:
    # Default to Home & Fabric Sign-in if available, otherwise first available view
    if "Home & Fabric Sign-in" in PAGE_ROUTER:
        st.session_state.selected_page = "Home & Fabric Sign-in"
    elif PAGE_ROUTER:
        st.session_state.selected_page = list(PAGE_ROUTER.keys())[0]
    else:
        st.error("No views available! Please ensure at least one view file exists in the pages/ directory.")
        st.stop()

# --- Main Content Routing ---
# Route to the appropriate page view based on selection
# Get the render function for the selected page
render_function = PAGE_ROUTER.get(st.session_state.selected_page)

if render_function:
    # NB: any per-page diagnostics belong in the page's own render(), not
    # in the router.  Earlier versions printed a debug banner here, which
    # ran on every Streamlit rerun (every widget click) and surfaced as
    # ``OSError: [Errno 22]`` on Windows whenever stdout was closed or
    # redirected — masking the real exception from the user.  If you need
    # to trace routing, add a single ``logger.debug(...)`` here instead.
    render_function()
else:
    # Default to Home if unknown page (shouldn't happen, but safety check)
    st.warning(f"⚠️ Unknown page: {st.session_state.selected_page}. Redirecting to Home.")
    if "Home & Fabric Sign-in" in PAGE_ROUTER:
        st.session_state.selected_page = "Home & Fabric Sign-in"
        PAGE_ROUTER["Home & Fabric Sign-in"]()
    elif PAGE_ROUTER:
        # Fallback to first available view
        first_view = list(PAGE_ROUTER.keys())[0]
        st.session_state.selected_page = first_view
        PAGE_ROUTER[first_view]()
    else:
        st.error("No views available!")

# Footer
render_footer()
