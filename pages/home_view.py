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
tools for pricing analysis, market monitoring, and execution automation.

| Page | What it does |
|------|-------------|
| **Market Barometer** | Real-time view of key market indices (FRED, EIA) with a 24-month probabilistic forecast. Also hosts the **Walmart Fresh Tracker** — upload Walmart HTST monthly files to review how Walmart tracks fuel and resin costs for the fresh business. |
| **Bid Asset Intelligence** | Evaluate customer bid opportunities — volume, pricing method, delivery charges, pallet economics, and custom-label fees — to support competitive and profitable bid decisions. |
| **HTST Activity Monitor** | Upload actual HTST shipment data to refresh site-level activity metrics, delivery charges, pallet status, and volume bracket classifications across your customer-site portfolio. |
| **New Price Quote** | Generate rapid, on-demand price quotes for HTST products. |
| **Pricing Execution Automation** | Generate VBCS files for Oracle upload. |

Select a page from the sidebar to get started.
""")

    st.markdown("---")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("""
#### 🚀 Quick Start

- **Monitoring market trends or Walmart fuel/resin costs?**
  → **Market Barometer** (Walmart Fresh Tracker is inside)
- **Evaluating a customer bid?**
  → **Bid Asset Intelligence**
- **Refreshing shipment activity & charges?**
  → **HTST Activity Monitor**
- **Need a quick price quote?**
  → **New Price Quote**
- **Generating VBCS files for Oracle?**
  → **Pricing Execution Automation**
""")

    with col2:
        st.markdown("""
#### 📚 Tips & Resources

- All CSV uploads should be **UTF-8 encoded**.
- In the **Market Barometer**, the Walmart Fresh Tracker section is collapsed
  by default — click it to expand and upload your files.
- Once Walmart files are processed, the upload panel is hidden automatically.
  Use the **Change files** button to re-upload.
- Market index data auto-refreshes every **15 days** when valid API keys
  are present.
""")

    st.markdown("---")
    st.info("💡 **Tip**: Use the sidebar navigation to switch between tools.")
