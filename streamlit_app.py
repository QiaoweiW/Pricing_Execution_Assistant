# streamlit_app.py - Darigold Pricing VBCS Generation Tool
# UPDATED: 2025-01-27 - Dynamic view discovery from pages directory
"""
Main Streamlit application entry point.
This file handles routing and page navigation only.
All page-specific UI code is in the pages/ directory.
"""
import streamlit as st
print("=" * 80)
print("STREAMLIT_APP.PY IS BEING EXECUTED - FILE LOADED!")
print("=" * 80)
import warnings
warnings.filterwarnings('ignore')

import os
import importlib
from pathlib import Path

# Import shared utilities
from utils.ui_helpers import apply_custom_css, render_footer

# Dynamic view discovery - only load views that exist in pages directory
PAGES_DIR = Path(__file__).parent / "pages"

# Mapping of view file names to display names.
# Only views listed here are eligible for sidebar navigation.
VIEW_NAME_MAPPING = {
    "home_view":                          "Home",
    "bid_asset_intelligence_view":        "Bid Asset Intelligence",
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
                print(f"Loaded view: {display_name} ({view_name})")
            else:
                print(f"Warning: {view_name} does not have a render() function")
        except Exception as e:
            print(f"Error loading {view_name}: {e}")

print(f"Total views loaded: {len(AVAILABLE_VIEWS)}")
print("=" * 80)

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
                print(f"Fabric warm-up failed (non-fatal): {exc}")
            except Exception as exc:  # noqa: BLE001  defensive top-level guard
                warm_status.update(
                    label=f"Microsoft Fabric warm-up errored ({exc}).",
                    state="error",
                    expanded=False,
                )
                print(f"Fabric warm-up errored (non-fatal): {exc}")
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
    if "Home" in sorted_views:
        sorted_views.remove("Home")
        sorted_views.insert(0, "Home")
    
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
    # Default to Home if available, otherwise first available view
    if "Home" in PAGE_ROUTER:
        st.session_state.selected_page = "Home"
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
    # Special debug logging for Pricing Execution Automation
    if st.session_state.selected_page == "Pricing Execution Automation":
        print("=" * 80)
        print("ROUTER: About to call pricing_execution_automation_view.render()")
        print("=" * 80)
    render_function()
else:
    # Default to Home if unknown page (shouldn't happen, but safety check)
    st.warning(f"⚠️ Unknown page: {st.session_state.selected_page}. Redirecting to Home.")
    if "Home" in PAGE_ROUTER:
        st.session_state.selected_page = "Home"
        PAGE_ROUTER["Home"]()
    elif PAGE_ROUTER:
        # Fallback to first available view
        first_view = list(PAGE_ROUTER.keys())[0]
        st.session_state.selected_page = first_view
        PAGE_ROUTER[first_view]()
    else:
        st.error("No views available!")

# Footer
render_footer()
