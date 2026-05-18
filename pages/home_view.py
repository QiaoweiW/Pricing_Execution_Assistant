"""
Home & Fabric Sign-in page view.

This is the single centralized location for Microsoft Fabric authentication.
Users sign in here ONCE; every Fabric-backed view (Monthly Milk/Resin/Freight
Movers, Bid Asset Intelligence, Shipment Monitor, Butter Movers, etc.) will
then work without further sign-in prompts.
"""
import streamlit as st

from utils.ui_helpers import apply_custom_css
from utils import fabric_signin_widget


def render() -> None:
    """Render the Home & Fabric Sign-in page."""
    apply_custom_css()

    st.markdown(
        '<h1 class="main-header">Darigold Pricing Intelligence</h1>',
        unsafe_allow_html=True,
    )

    # ── Microsoft Fabric Sign-in (prominent, top of page) ────────────────────
    #
    # Placed here — above the navigation table — so it is the first thing a
    # user sees on app launch.  A successful sign-in on this page silently
    # unlocks every Fabric-backed view in the app; no per-page re-authentication
    # is ever required.
    st.markdown("---")
    fabric_signin_widget.render_fabric_signin_section()
    st.markdown("---")

    # ── Welcome & navigation table ────────────────────────────────────────────
    st.markdown("""
### Welcome

Welcome to the Darigold Pricing Intelligence platform. This application provides
tools for pricing analysis, market monitoring, demand insight, and execution
automation.

| Page | What it does |
|------|-------------|
| **Home & Fabric Sign-in** *(this page)* | Centralized Microsoft Fabric sign-in. Sign in once here to unlock all Fabric-backed views. |
| **Market Barometer** | Real-time view of key market indices (FRED, EIA) with a 24-month probabilistic forecast. Hosts the **Monthly Milk, Resin & Freight Movers** workflow and the **Walmart Fresh Tracker** for Walmart HTST fuel & resin cost reviews. |
| **Bid Asset Intelligence** | Evaluate customer bid opportunities — volume, pricing method, delivery charges, pallet economics, and custom-label fees. Data is pulled automatically from the Fabric Lakehouse. |
| **Shipment Monitor & HTST Requote** | Refresh site-level activity metrics, delivery charges, pallet status, and volume bracket classifications. Pulls shipment data from the Fabric Lakehouse automatically. |
| **New Price Quote** | Generate rapid, on-demand price quotes for HTST products. |
| **Pricing Execution Automation** | Generate VBCS files for Oracle upload. |
| **Demand Planner Analytics** | Embedded Demand Planning BI dashboard, New Distribution Tracker, and an opt-in pull of the IBP Orders / Shipments Delta tables from the Microsoft Fabric Lakehouse. |
| **RFP Financial Analysis** | Embedded Finance P&L Power BI report for interactive RFP financial review. |

Select a page from the sidebar to get started.
""")

    st.markdown("---")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("""
#### Quick Start

- **Need to connect to Microsoft Fabric?**
  → Sign in using the panel above (this page)
- **Monitoring market trends or running the monthly Milk/Resin/Freight movers?**
  → **Market Barometer** (Walmart Fresh Tracker is inside)
- **Evaluating a customer bid?**
  → **Bid Asset Intelligence**
- **Refreshing shipment activity & charges?**
  → **Shipment Monitor & HTST Requote**
- **Need a quick price quote?**
  → **New Price Quote**
- **Generating VBCS files for Oracle?**
  → **Pricing Execution Automation**
- **Reviewing demand plans or IBP Orders/Shipments?**
  → **Demand Planner Analytics**
- **Looking at the Finance P&L for an RFP?**
  → **RFP Financial Analysis**
""")

    with col2:
        st.markdown("""
#### Tips & Resources

- **Sign in to Microsoft Fabric once** (using the panel above) to unlock
  Bid Asset Intelligence, Monthly Movers, Shipment Monitor, Butter Movers,
  and any future Fabric-backed feature — no per-page re-authentication needed.
- All CSV uploads should be **UTF-8 encoded**.
- In the **Market Barometer**, the Monthly Milk, Resin & Freight Movers and
  Walmart Fresh Tracker sections are collapsed by default — click to expand
  and upload your files.
- The Milk Commodity Cost chart supports independent **Category (HTST vs ESL)**
  and **Class (I vs II)** filters; both are chart-only and never affect the
  Milk Mover calculation or the mover_details_table.
- Once files are processed, the upload panel is hidden automatically. Use the
  **Change files** button to re-upload.
- Market index data auto-refreshes every **15 days** when valid API keys
  are present.
""")

    st.markdown("---")
    st.info("💡 **Tip**: Use the sidebar navigation to switch between tools.")
