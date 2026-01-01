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
    
    - **New Price Quote**: Generate rapid, on-demand price quotes for HTST products
    - **Market Barometer**: Monitor market trends and pricing indicators
    - **Pricing Execution Automation**: Generate VBCS files for Oracle upload
    
    Select a page from the sidebar to get started.
    """)
    
    st.markdown("---")
    
    # Quick links or overview cards
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("""
        #### 🚀 Quick Start
        - New to the platform? Start with **New Price Quote**
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


