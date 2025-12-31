"""
Shared UI helper functions and utilities for Streamlit app views.
"""
import streamlit as st
from pathlib import Path
import pandas as pd
from datetime import datetime


def apply_custom_css():
    """Apply custom CSS styling to the Streamlit app."""
    st.markdown("""
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    """, unsafe_allow_html=True)
    
    st.markdown("""
    <style>
        .main-header {
            font-size: 2.5rem;
            color: #d32f2f;
            text-align: center;
            margin-bottom: 2rem;
            font-weight: 700;
            letter-spacing: 0.5px;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        .script-section {
            background-color: #f0f2f6;
            padding: 1rem;
            border-radius: 0.5rem;
            margin: 1rem 0;
        }
        .success-box {
            background-color: #ffebee;
            border: 1px solid #ffcdd2;
            border-radius: 0.25rem;
            padding: 1rem;
            margin: 1rem 0;
        }
        .error-box {
            background-color: #ffebee;
            border: 1px solid #ffcdd2;
            border-radius: 0.25rem;
            padding: 1rem;
            margin: 1rem 0;
        }
        .warning-box {
            background-color: #fff3e0;
            border: 1px solid #ffcc02;
            border-radius: 0.25rem;
            padding: 1rem;
            margin: 1rem 0;
        }
        
        /* Sidebar styling */
        .css-1d391kg {
            background-color: #fafafa;
        }
        
        /* Sidebar button styling */
        .stButton > button {
            background-color: #d32f2f;
            color: white;
            border: none;
            border-radius: 0.5rem;
            padding: 0.75rem 1rem;
            font-weight: 600;
            transition: all 0.3s ease;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        
        .stButton > button:hover {
            background-color: #b71c1c;
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.2);
        }
        
        /* Sidebar header styling */
        .sidebar .stMarkdown h1 {
            color: #d32f2f;
            font-size: 1.8rem;
            margin-bottom: 1rem;
            text-align: center;
            font-weight: 700;
            letter-spacing: 0.5px;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        
        .sidebar .stMarkdown h3 {
            color: #d32f2f;
            font-size: 1.2rem;
            margin-bottom: 1rem;
        }
        
        /* Make sidebar more intuitive */
        .stSidebar .stButton {
            margin-bottom: 0.5rem;
        }
        
        .stSidebar .stButton > button {
            width: 100%;
            text-align: left;
            justify-content: flex-start;
            padding: 1rem;
            font-size: 1rem;
            line-height: 1.4;
        }
        
        /* Hide default Streamlit sidebar elements above custom content */
        /* Hide the "streamlit app" header/search bar and page navigation */
        .stSidebar [data-testid="stSidebarNav"],
        .stSidebar [data-testid="stSidebarNavLinks"],
        .stSidebar section[data-testid="stSidebarNav"] {
            display: none !important;
        }
        
        /* Hide any elements with "streamlit app" text or search functionality */
        .stSidebar [class*="search"],
        .stSidebar [placeholder*="Search"],
        .stSidebar input[type="search"] {
            display: none !important;
        }
        
        /* IMPORTANT: Ensure horizontal rules (hr) and dividers are ALWAYS visible */
        .stSidebar hr,
        .stSidebar [class*="divider"],
        .stSidebar .stMarkdown hr,
        .stSidebar div[data-testid="stMarkdownContainer"] hr {
            display: block !important;
            visibility: visible !important;
            border-top: 1px solid #ccc !important;
            margin: 1rem 0 !important;
            opacity: 1 !important;
        }
        
        /* Ensure all markdown content in sidebar is visible (including our custom content) */
        .stSidebar .stMarkdown:has(h1),
        .stSidebar .stMarkdown:has(hr),
        .stSidebar .stMarkdown:has(div) {
            display: block !important;
            visibility: visible !important;
        }
    </style>
    """, unsafe_allow_html=True)


def create_consistent_container(content, container_type="default", min_height=None):
    """
    Create a consistent container with fixed dimensions regardless of zoom level.
    
    Args:
        content: The content to display in the container
        container_type: Type of container ("default", "metric", "upload", "button")
        min_height: Custom minimum height in pixels
    
    Returns:
        HTML container with consistent styling
    """
    height_map = {
        "default": "200px",
        "metric": "80px", 
        "upload": "100px",
        "button": "120px"
    }
    
    height = min_height or height_map.get(container_type, "200px")
    
    container_html = f"""
    <div class="fixed-container" style="min-height: {height};">
        {content}
    </div>
    """
    return container_html


def create_metric_box(title, value, subtitle=""):
    """
    Create a consistent metric display box.
    
    Args:
        title: Main metric title
        value: Metric value
        subtitle: Optional subtitle
    
    Returns:
        HTML for metric box
    """
    metric_html = f"""
    <div class="metric-container">
        <div style="font-size: 14px; color: #666; margin-bottom: 4px;">{title}</div>
        <div style="font-size: 24px; font-weight: bold; color: #1f77b4;">{value}</div>
        {f'<div style="font-size: 12px; color: #888;">{subtitle}</div>' if subtitle else ''}
    </div>
    """
    return metric_html


def render_footer():
    """Render the footer for all pages."""
    st.markdown("---")
    st.markdown("""
    <div style='text-align: center; color: #666; padding: 20px;'>
        <p>Pricing Execution Agent - Streamlit Application</p>
        <p>For support or questions, please contact the development team.</p>
    </div>
    """, unsafe_allow_html=True)


def safe_error_message(message):
    """
    Safely convert error message to ASCII-safe string to avoid encoding issues.
    Avoids calling str() directly which can fail with UnicodeEncodeError on Windows.
    
    Args:
        message: Error message (can be string, exception, or any object)
    
    Returns:
        ASCII-safe string representation
    """
    # Handle different input types
    if message is None:
        return "Error occurred (no error message provided)"
    
    # If it's already a string, encode it safely
    if isinstance(message, str):
        try:
            # Try to encode to UTF-8 first, then to ASCII
            # This avoids the 'charmap' codec issue on Windows
            utf8_bytes = message.encode('utf-8', errors='replace')
            return utf8_bytes.decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
        except Exception:
            return "Error occurred (message contains non-ASCII characters that could not be converted)"
    
    # If it's bytes, decode it safely
    if isinstance(message, bytes):
        try:
            decoded = message.decode('utf-8', errors='replace')
            return decoded.encode('ascii', errors='replace').decode('ascii')
        except Exception:
            return "Error occurred (message bytes could not be decoded)"
    
    # If it's an exception, use repr() which is usually safer
    if isinstance(message, Exception):
        try:
            if hasattr(message, 'args') and message.args and len(message.args) > 0:
                arg = message.args[0]
                if isinstance(arg, str):
                    utf8_bytes = arg.encode('utf-8', errors='replace')
                    return utf8_bytes.decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
                elif isinstance(arg, bytes):
                    return arg.decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
            # Fallback to repr()
            return repr(message).encode('ascii', errors='replace').decode('ascii')
        except Exception:
            return f"{type(message).__name__}: Error occurred (could not convert error message to ASCII)"
    
    # For other types, try repr() first (safer than str())
    try:
        msg_repr = repr(message)
        return msg_repr.encode('ascii', errors='replace').decode('ascii')
    except Exception:
        # Last resort: try str() with safe encoding
        try:
            msg_str = str(message)
            return msg_str.encode('utf-8', errors='replace').decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
        except Exception:
            return "Error occurred (message contains non-ASCII characters that could not be converted)"

