"""
New Price Quote page view.

This module provides the UI for querying and viewing pricing data.
Features:
- Multi-item search by item number or description (semicolon-separated)
- Filtering by plant, volume brackets, pallet, mileage, and drop size
- CSV export of filtered results
- Database upload and processing interface
"""
import streamlit as st
import pandas as pd
import sys
import subprocess
import tempfile
import io
from pathlib import Path
import datetime
from utils.ui_helpers import apply_custom_css

# Required CSV files for database creation
REQUIRED_FILES = [
    "Product_Class_Plant.csv",
    "Plant_Class_Plant Fees.csv",
    "Product_Milk Base Cost.csv",
    "Product_Processing_Pkg_Ing.csv",
    "Sell-to_Volume Bracket_Fee.csv",
    "Custom Label_Volume Bracket_Fee.csv",
    "Pallet_Fee.csv",
    "Delivery_Miles Tier_Drop Size Tier_Fee.csv",
    "Product_UOM.csv"
]


def load_pricing_data():
    """
    Load pricing data from parquet file or session state.
    
    Returns:
        tuple: (DataFrame or None, record_count, file_size_mb, file_mod_time)
    """
    temp_dir = Path(tempfile.gettempdir())
    parquet_path = temp_dir / "pricing_data.parquet"
    
    if parquet_path.exists():
        try:
            df = pd.read_parquet(parquet_path)
            record_count = len(df)
            file_size_mb = parquet_path.stat().st_size / (1024 * 1024)
            file_mod_time = datetime.datetime.fromtimestamp(parquet_path.stat().st_mtime)
            return df, record_count, file_size_mb, file_mod_time
        except Exception as e:
            st.error(f"Error loading parquet file: {e}")
            return None, 0, 0, None
    
    # Try to load from session state if available
    if hasattr(st.session_state, 'processed_df') and st.session_state.processed_df is not None:
        df = st.session_state.processed_df
        record_count = len(df)
        file_size_mb = 0  # Unknown
        file_mod_time = datetime.datetime.now()
        return df, record_count, file_size_mb, file_mod_time
    
    return None, 0, 0, None


def display_database_status(record_count, file_size_mb, file_mod_time):
    """
    Display database connection status in a 4-column layout.
    
    Args:
        record_count: Number of records in database
        file_size_mb: File size in MB
        file_mod_time: File modification time
    """
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.success("✅ Database Connected")
    with col2:
        st.info(f"📊 {record_count:,} records")
    with col3:
        st.info(f"📁 {file_size_mb:.1f} MB")
    with col4:
        st.info(f"🕒 {file_mod_time.strftime('%Y-%m-%d %H:%M')}")


def initialize_filter_session_state(available_options):
    """
    Initialize session state for all filters with default values (all options selected).
    
    Args:
        available_options: Dictionary mapping filter names to available option lists
    """
    for filter_name, options in available_options.items():
        session_key = f'filter_{filter_name}'
        if session_key not in st.session_state:
            st.session_state[session_key] = options
    
    # Initialize item search separately
    if 'filter_item_search' not in st.session_state:
        st.session_state.filter_item_search = ""
    # Initialize item description search separately
    if 'filter_item_description_search' not in st.session_state:
        st.session_state.filter_item_description_search = ""


def apply_item_search_filter(df, search_text):
    """
    Apply item number search filter supporting multiple items separated by ';'.
    Searches only in the Item column.
    
    Args:
        df: DataFrame to filter
        search_text: Search text (can contain multiple item numbers separated by ';')
        
    Returns:
        Filtered DataFrame
    """
    if not search_text:
        return df
    
    search_terms = [term.strip() for term in search_text.split(';') if term.strip()]
    if not search_terms:
        return df
    
    # Create mask for matching any of the search terms in Item column
    item_mask = pd.Series([False] * len(df), index=df.index)
    
    for term in search_terms:
        # Search in Item column only
        item_match = df['Item'].astype(str).str.contains(term, case=False, na=False, regex=False)
        item_mask = item_mask | item_match
    
    return df[item_mask]


def apply_item_description_search_filter(df, search_text):
    """
    Apply item description search filter supporting multiple descriptions separated by ';'.
    Searches only in the Item Description column.
    
    Args:
        df: DataFrame to filter
        search_text: Search text (can contain multiple descriptions separated by ';')
        
    Returns:
        Filtered DataFrame
    """
    if not search_text:
        return df
    
    # Check if Item Description column exists
    if 'Item Description' not in df.columns:
        return df
    
    search_terms = [term.strip() for term in search_text.split(';') if term.strip()]
    if not search_terms:
        return df
    
    # Create mask for matching any of the search terms in Item Description column
    description_mask = pd.Series([False] * len(df), index=df.index)
    
    for term in search_terms:
        # Search in Item Description column
        desc_match = df['Item Description'].astype(str).str.contains(term, case=False, na=False, regex=False)
        description_mask = description_mask | desc_match
    
    return df[description_mask]


def format_numeric_columns(df, decimal_places=4):
    """
    Format numeric columns for display with specified decimal places.
    
    Args:
        df: DataFrame to format
        decimal_places: Number of decimal places to display
        
    Returns:
        DataFrame with formatted numeric columns
    """
    display_df = df.copy()
    numeric_columns = [
        col for col in display_df.columns
        if '($/Gal)' in col or '($/gal)' in col or 'per Gallon' in col or 'per Each' in col or 'per Case' in col
    ]
    
    for col in numeric_columns:
        if col in display_df.columns:
            display_df[col] = display_df[col].apply(
                lambda x: f"{x:.{decimal_places}f}" if pd.notna(x) and isinstance(x, (int, float)) else x
            )
    
    return display_df


def render():
    """Render the New Price Quote page."""
    apply_custom_css()
    
    st.markdown('<h1 class="main-header">New Price Quote</h1>', unsafe_allow_html=True)
    
    st.markdown("""
    ### Welcome
    
    Designed to speed up sales cycle, this tool provides rapid, on-demand new price quote for HTST products. If you need a quote for a non-HTST product, please continue following current Smartsheet process or contact RGM team directly.
    """)
    
    # Load pricing data
    df, record_count, file_size_mb, file_mod_time = load_pricing_data()
    
    # Display database status if data is loaded
    if df is not None:
        display_database_status(record_count, file_size_mb, file_mod_time)
    else:
        st.warning("⚠️ No pricing database found. Please upload CSV files to create the database.")

    # Sample database display
    if df is not None:
        st.markdown("---")
        st.markdown("### 📋 Sample Database (Top 50 Records)")
        
        sample_df = df.head(50)
        
        st.dataframe(
            sample_df,
            width='stretch',
            height=400
        )
        
        # Query section
        st.markdown("---")
        st.markdown("### 🔍 Query Database")
        
        # Get available filter options
        try:
            available_plants = sorted([str(x) for x in df['Plant'].unique().tolist()])
            available_volumes = sorted([str(x) for x in df['Sell-to Volume Bracket'].unique().tolist()])
            available_custom_volumes = sorted([str(x) for x in df['Custom Label Bracket'].unique().tolist()])
            available_pallets = sorted([str(x) for x in df['Pallet'].unique().tolist()])
            available_mileages = sorted([str(x) for x in df['Mileage Fee Tier (Mi)'].unique().tolist()])
            available_drops = sorted([str(x) for x in df['Drop Fee Tier (lbs/Drop)'].unique().tolist()])
            
            # Create filter interface
            st.markdown("#### Check Volume Tier (Optional)")
            st.markdown("If you're quoting for an existing customer, you can view their current order list and annual volume [here](https://darigold1com.sharepoint.com/:x:/r/sites/CPPricing2/Shared%20Documents/General/HTST_Activity_Model_Fundamental_Data/Volume%20Tier%20Monitor/Volume%20Tier%20Monitor.xlsx?d=wc8cd9b703b9f44949ef6ece10098326a&csf=1&web=1&e=wnaNjA). Use this information, along with the new volume, to set your filters.")
            
            st.markdown("#### Filter Options")
            
            # Initialize session state for filters
            initialize_filter_session_state({
                'plants': available_plants,
                'volumes': available_volumes,
                'custom_volumes': available_custom_volumes,
                'pallets': available_pallets,
                'mileages': available_mileages,
                'drops': available_drops
            })
            
            # Get sample items for default placeholder
            sample_items = []
            if df is not None and 'Item' in df.columns:
                unique_items = df['Item'].astype(str).unique()
                sample_items = sorted([item for item in unique_items if item and item != 'nan'])[:3]
            
            # Create default placeholder text for item search
            default_item_placeholder = "e.g., " + ";".join(sample_items) if sample_items else "Enter item numbers separated by ';' (e.g., 340776;340013)"
            
            # Get sample descriptions for default placeholder
            sample_descriptions = []
            if df is not None and 'Item Description' in df.columns:
                unique_descriptions = df['Item Description'].astype(str).unique()
                sample_descriptions = [desc for desc in unique_descriptions if desc and desc != 'nan' and len(desc) > 10][:2]
            
            # Create default placeholder text for description search
            default_desc_placeholder = "e.g., " + ";".join([d[:20] + "..." if len(d) > 20 else d for d in sample_descriptions]) if sample_descriptions else "Enter item descriptions separated by ';' (e.g., description1;description2)"
            
            col1, col2, col3 = st.columns(3)
            
            with col1:
                item_search = st.text_input("Item Search", 
                                          value=st.session_state.filter_item_search,
                                          placeholder=default_item_placeholder,
                                          help="Search by item number(s) separated by ';' (e.g., 340776;340013)",
                                          key="item_search_input")
                if item_search != st.session_state.filter_item_search:
                    st.session_state.filter_item_search = item_search
                
                item_description_search = st.text_input("Item Description Search", 
                                                      value=st.session_state.filter_item_description_search,
                                                      placeholder=default_desc_placeholder,
                                                      help="Search by item description(s) separated by ';'. Multiple descriptions can be searched at once.",
                                                      key="item_description_search_input")
                if item_description_search != st.session_state.filter_item_description_search:
                    st.session_state.filter_item_description_search = item_description_search
                
                selected_plants = st.multiselect("Plant", 
                                               available_plants, 
                                               default=st.session_state.filter_plants,
                                               key="plants_select")
                if selected_plants != st.session_state.filter_plants:
                    st.session_state.filter_plants = selected_plants
                
                selected_volumes = st.multiselect("Sell-to Volume (Gal/yr)", 
                                                available_volumes, 
                                                default=st.session_state.filter_volumes,
                                                key="volumes_select")
                if selected_volumes != st.session_state.filter_volumes:
                    st.session_state.filter_volumes = selected_volumes
            
            with col2:
                selected_custom_volumes = st.multiselect("Custom-label Volume (Gal/Yr)", 
                                                       available_custom_volumes, 
                                                       default=st.session_state.filter_custom_volumes,
                                                       key="custom_volumes_select")
                if selected_custom_volumes != st.session_state.filter_custom_volumes:
                    st.session_state.filter_custom_volumes = selected_custom_volumes
                
                selected_pallets = st.multiselect("Pallet", 
                                                available_pallets, 
                                                default=st.session_state.filter_pallets,
                                                key="pallets_select")
                if selected_pallets != st.session_state.filter_pallets:
                    st.session_state.filter_pallets = selected_pallets
                
                selected_mileages = st.multiselect("Mileage", 
                                                 available_mileages, 
                                                 default=st.session_state.filter_mileages,
                                                 key="mileages_select")
                if selected_mileages != st.session_state.filter_mileages:
                    st.session_state.filter_mileages = selected_mileages
            
            with col3:
                selected_drops = st.multiselect("Drop Size (Lb/drop)", 
                                              available_drops, 
                                              default=st.session_state.filter_drops,
                                              key="drops_select")
                if selected_drops != st.session_state.filter_drops:
                    st.session_state.filter_drops = selected_drops
            
            # Query button
            if st.button("🔍 Query Database", type="primary", use_container_width=True):
                st.session_state.query_executed = True
            
            # Apply filters only after query button is pressed
            if hasattr(st.session_state, 'query_executed') and st.session_state.query_executed:
                filtered_df = df.copy()
                
                # Apply item search filter (supports multiple items separated by ";")
                filtered_df = apply_item_search_filter(filtered_df, st.session_state.filter_item_search)
                
                # Apply item description search filter (supports multiple descriptions separated by ";")
                filtered_df = apply_item_description_search_filter(filtered_df, st.session_state.filter_item_description_search)
                
                # Plant filter
                filtered_df = filtered_df[filtered_df['Plant'].astype(str).isin(st.session_state.filter_plants)]
                
                # Volume filters
                filtered_df = filtered_df[filtered_df['Sell-to Volume Bracket'].astype(str).isin(st.session_state.filter_volumes)]
                filtered_df = filtered_df[filtered_df['Custom Label Bracket'].astype(str).isin(st.session_state.filter_custom_volumes)]
                
                # Other filters
                filtered_df = filtered_df[filtered_df['Pallet'].astype(str).isin(st.session_state.filter_pallets)]
                filtered_df = filtered_df[filtered_df['Mileage Fee Tier (Mi)'].astype(str).isin(st.session_state.filter_mileages)]
                filtered_df = filtered_df[filtered_df['Drop Fee Tier (lbs/Drop)'].astype(str).isin(st.session_state.filter_drops)]
                
                # Display results
                st.markdown(f"#### Query Results ({len(filtered_df):,} records)")
                
                if len(filtered_df) > 0:
                    # Format numeric columns for display
                    display_df = format_numeric_columns(filtered_df, decimal_places=4)
                    
                    st.dataframe(
                        display_df,
                        width='stretch',
                        height=400
                    )
                    
                    # Download button
                    csv_data = display_df.to_csv(index=False)
                    st.download_button(
                        label="📥 Download Filtered Data as CSV",
                        data=csv_data,
                        file_name=f"filtered_pricing_data_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                        mime="text/csv"
                    )
                else:
                    st.info("No records match the selected filters.")
                
        except Exception as e:
            st.error(f"Error setting up filters: {e}")
    
    # Upload section - always accessible to allow database refresh
    st.markdown("---")
    
    # Initialize uploaded_files variable
    uploaded_files = None
    
    # Use expander if data exists, otherwise show directly
    if df is not None:
        with st.expander("📤 Upload CSV Files to Refresh/Regenerate Database", expanded=False):
            st.markdown("Upload all the csv files located in this [RGM teams folder](https://darigold1com.sharepoint.com/:f:/r/sites/CPPricing2/Shared%20Documents/General/HTST_Activity_Model_Fundamental_Data/Model%20Input%20(csv%20files)?csf=1&web=1&e=1tp04m)")
            st.info("⚠️ **Warning**: Uploading new files will replace the existing database. Make sure you have the latest CSV files.")
            
            # File upload
            uploaded_files = st.file_uploader(
                "Choose CSV files",
                type=['csv'],
                accept_multiple_files=True,
                help="Upload the 9 required CSV files to refresh the pricing database",
                key="file_uploader_refresh"
            )
    else:
        st.markdown("### 📤 Upload CSV Files to Refresh Database")
        st.markdown("Upload all the csv files located in this [RGM teams folder](https://darigold1com.sharepoint.com/:f:/r/sites/CPPricing2/Shared%20Documents/General/HTST_Activity_Model_Fundamental_Data/Model%20Input%20(csv%20files)?csf=1&web=1&e=1tp04m)")
        
        # File upload
        uploaded_files = st.file_uploader(
            "Choose CSV files",
            type=['csv'],
            accept_multiple_files=True,
            help="Upload the 9 required CSV files to create the pricing database",
            key="file_uploader_initial"
        )
    
    # Process uploaded files (works for both initial upload and refresh)
    if uploaded_files:
            # Check if we have all required files
            uploaded_filenames = [f.name for f in uploaded_files]
            missing_files = [f for f in REQUIRED_FILES if f not in uploaded_filenames]
            
            if missing_files:
                st.error(f"❌ Missing required files: {', '.join(missing_files)}")
                st.info(f"📋 Required files: {', '.join(REQUIRED_FILES)}")
            else:
                st.success(f"✅ All required files uploaded ({len(uploaded_files)} files)")
                
                # Process button
                if st.button("🚀 Process Files", type="primary", use_container_width=True):
                    try:
                        # Save uploaded files temporarily
                        temp_dir = Path(tempfile.gettempdir())
                        temp_dir.mkdir(exist_ok=True)
                        
                        # Save all uploaded files
                        for uploaded_file in uploaded_files:
                            file_path = temp_dir / uploaded_file.name
                            with open(file_path, "wb") as f:
                                f.write(uploaded_file.getbuffer())
                        
                        # Run the processing script
                        progress_bar = st.progress(0)
                        status_text = st.empty()
                        
                        status_text.text("🔄 Processing files...")
                        progress_bar.progress(20)
                        
                        # Run the processing script
                        try:
                            # UPDATED 2025-01-26: Script moved to processing/ directory
                            script_path = Path("processing/new_pricing_processor.py")
                            if script_path.exists():
                                result = subprocess.run(
                                    [sys.executable, str(script_path)],
                                    cwd=str(Path.cwd()),
                                    capture_output=True,
                                    text=True,
                                    timeout=300
                                )
                                
                                if result.returncode == 0:
                                    progress_bar.progress(100)
                                    status_text.text("✅ Processing completed successfully!")
                                    
                                    # Load the processed data
                                    parquet_path = temp_dir / "pricing_data.parquet"
                                    
                                    if parquet_path.exists():
                                        test_load = pd.read_parquet(parquet_path)
                                        st.session_state.processed_df = test_load
                                        
                                        # Clear filter states to start fresh with new data
                                        if 'query_executed' in st.session_state:
                                            del st.session_state.query_executed
                                        if 'filter_item_search' in st.session_state:
                                            st.session_state.filter_item_search = ""
                                        if 'filter_item_description_search' in st.session_state:
                                            st.session_state.filter_item_description_search = ""
                                        
                                        st.success(f"✅ Successfully loaded pricing: {test_load.shape[0]:,} records")
                                        st.info("🔄 Database refreshed! Filters have been reset. You can now query the new database.")
                                        
                                        # Force page refresh to show query interface
                                        st.rerun()
                                    else:
                                        st.error("❌ Parquet file not found after processing")
                                else:
                                    st.error(f"❌ Processing failed: {result.stderr}")
                            else:
                                st.error("❌ Processing script not found")
                                
                        except subprocess.TimeoutExpired:
                            st.error("❌ Processing timed out")
                        except Exception as e:
                            st.error(f"❌ Error during processing: {e}")
                        
                    except Exception as e:
                        st.error(f"❌ Error: {e}")
    
    # Finalize Quote Section
    if df is not None:
        st.markdown("---")
        st.markdown("### 📋 Finalize Quote")
        
        st.markdown("""
        - **If you are Sales**: Please collaborate with RGM team to finalize quote by confirming key input such as trade%, first order date, whether shuttling is involved, demand plan.
        
        - **If you are RGM**: Please use this [standard template](https://darigold1com.sharepoint.com/:x:/r/sites/CPPricing2/Shared%20Documents/General/HTST_Activity_Model_Fundamental_Data/Template/HTST_New%20Business%20Bid_Price%20Build%20Template.xlsx?d=w8f7a5e94128743aca27a89f268ab68da&csf=1&web=1&e=Qef248) saved in RGM teams folder to complete the quote.
        """)


