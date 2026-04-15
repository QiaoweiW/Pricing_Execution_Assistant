"""
Home page view for the Streamlit app.
"""
import streamlit as st
from utils.ui_helpers import apply_custom_css


def render():
    """Render the Home page."""
    apply_custom_css()
    
    st.markdown('<h1 class="main-header">Darigold Pricing Intelligence</h1>', unsafe_allow_html=True)
    
    st.markdown("""
    ### Welcome
    
    Welcome to the Darigold Pricing Intelligence platform. This application provides tools for:
    
    - **Bid Asset Intelligence**: Analyze customer bid opportunities — evaluate volume,
      pricing method, delivery charges, pallet economics, and custom-label fees to
      support competitive and profitable bid decisions.
    - **HTST Activity Monitor**: Upload actual HTST shipment data to refresh
      site-level activity metrics, delivery charges, pallet status, and volume
      bracket classifications across your customer-site portfolio.
    - **Customer Data Barometer**: Upload consolidated Walmart HTST monthly files to
      review how Walmart tracks fuel and resin costs for the fresh business.
    - **New Price Quote**: Generate rapid, on-demand price quotes for HTST products.
    - **Market Barometer**: Monitor market trends and pricing indicators.
    - **Pricing Execution Automation**: Generate VBCS files for Oracle upload.
    
    Select a page from the sidebar to get started.
    """)
    
    st.markdown("---")
    
    # Quick links or overview cards
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("""
        #### 🚀 Quick Start
        - Evaluating a customer bid? Go to **Bid Asset Intelligence**
        - Refreshing shipment activity & charges? Use **HTST Activity Monitor**
        - Reviewing Walmart fuel & resin cost tracking? Use **Customer Data Barometer**
        - Need a quick price quote? Go to **New Price Quote**
        - Need to generate VBCS files? Use **Pricing Execution Automation**
        - Want to analyze market trends? Check **Market Barometer**
        """)
    
    with col2:
        st.markdown("""
        #### 📚 Resources
        - All CSV files should be in UTF-8 format
        """)
    
    st.markdown("---")
    
    st.info("💡 **Tip**: Use the sidebar navigation to switch between different tools and pages.")


