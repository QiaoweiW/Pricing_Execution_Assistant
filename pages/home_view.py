"""
Home page view — landing page for the Darigold Pricing Intelligence platform.
"""
import streamlit as st
from utils.ui_helpers import apply_custom_css


def render() -> None:
    """Render the Home page."""
    apply_custom_css()

    st.markdown(
        '<h1 class="main-header">Darigold Pricing Intelligence</h1>',
        unsafe_allow_html=True,
    )

    st.markdown("""
### Welcome

Welcome to the Darigold Pricing Intelligence platform. This application provides
tools for pricing analysis, market monitoring, demand insight, and execution
automation.

| Page | What it does |
|------|-------------|
| **Market Barometer** | Real-time view of key market indices (FRED, EIA) with a 24-month probabilistic forecast. Hosts the **Monthly Milk, Resin & Freight Movers** workflow (with a slicer- and Category/Class-filterable Milk Commodity Cost chart) and the **Walmart Fresh Tracker** for Walmart HTST fuel & resin cost reviews. |
| **Bid Asset Intelligence** | Evaluate customer bid opportunities — volume, pricing method, delivery charges, pallet economics, and custom-label fees — to support competitive and profitable bid decisions. |
| **Shipment Monitor & HTST Requote** | Upload actual HTST shipment data to refresh site-level activity metrics, delivery charges, pallet status, and volume bracket classifications across your customer-site portfolio. |
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
#### 🚀 Quick Start

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
#### 📚 Tips & Resources

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
