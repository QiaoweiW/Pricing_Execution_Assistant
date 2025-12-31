"""
Data loading and processing helper functions.
"""
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta


def load_existing_data():
    """Load existing VBCS data files if they exist"""
    data_files = {}
    
    # Try multiple possible output directory locations
    possible_output_dirs = [
        Path("data"),          # Local data directory (for Streamlit Cloud)
        Path("../../Output"),  # From current directory
        Path("../Output"),     # Alternative path
        Path("Output"),        # If in root
        Path("./Output"),      # Current directory
    ]
    
    output_dir = None
    for dir_path in possible_output_dirs:
        if dir_path.exists():
            output_dir = dir_path
            break
    
    if output_dir is None:
        # Create data directory if it doesn't exist
        output_dir = Path("data")
        output_dir.mkdir(exist_ok=True)
    
    # Clean up old input files (older than 5 minutes) on data load
    # This ensures uploaded files don't persist indefinitely
    _cleanup_old_input_files(output_dir, max_age_minutes=5)
    
    # Load data files (only output VBCS files, not input files)
    output_file_names = ["fixed_vbcs.csv", "ks_htst_vbcs.csv", "urm_vbcs.csv", "winco_vbcs.csv", "batch_vbcs.csv", "combined_all_vbcs.csv"]
    for file_name in output_file_names:
        file_path = output_dir / file_name
        if file_path.exists():
            try:
                data_files[file_name] = pd.read_csv(file_path)
            except Exception as e:
                # Safely handle exception message encoding
                try:
                    error_msg = str(e)
                except UnicodeEncodeError:
                    error_msg = repr(e)
                print(f"Error loading {file_name}: {error_msg}")
    
    return data_files, output_dir


def _cleanup_old_input_files(directory, max_age_minutes=5):
    """
    Clean up input files older than max_age_minutes in the given directory.
    Only removes input files, not output VBCS files.
    
    Args:
        directory: Path to directory to clean
        max_age_minutes: Maximum age in minutes before files are deleted (default: 5)
    """
    if not directory or not directory.exists():
        return
    
    try:
        max_age = timedelta(minutes=max_age_minutes)
        cutoff_time = datetime.now() - max_age
        
        # List of known input file names that should be cleaned up
        input_file_patterns = [
            "Execution_final.csv",
            "HTST Pricing_UOMS_v1.csv",
            "Milk_Market_Index.csv",
            "Effective_Date_Assumptions.csv",
            "Customer_Extract_Report.csv",
            "Old_Price_Build.csv",
            "Costco_HTST_Pricing.csv",
            "Costco_HTST_Region_Lookup.csv"
        ]
        
        csv_files = list(directory.glob("*.csv"))
        files_deleted = 0
        
        for file_path in csv_files:
            try:
                # Only delete if it's an input file (matches known input patterns)
                is_input_file = any(pattern in file_path.name for pattern in input_file_patterns)
                
                # Also check if it's NOT an output file
                is_output_file = any(name in file_path.name for name in [
                    "urm_vbcs.csv", "winco_vbcs.csv", "batch_vbcs.csv",
                    "fixed_vbcs.csv", "ks_htst_vbcs.csv", "combined_all_vbcs.csv"
                ])
                
                # Delete if it's an input file (or unknown file that's not an output) and is old
                if (is_input_file or (not is_output_file)) and file_path.exists():
                    file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                    if file_mtime < cutoff_time:
                        file_path.unlink()
                        files_deleted += 1
                        print(f"Cleaned up old input file: {file_path.name} (age: {datetime.now() - file_mtime})")
            except Exception as e:
                # Continue with other files if one fails
                try:
                    error_msg = str(e)
                except UnicodeEncodeError:
                    error_msg = repr(e)
                print(f"Error cleaning up {file_path.name}: {error_msg}")
        
        if files_deleted > 0:
            print(f"Cleanup: Removed {files_deleted} old input files from {directory}")
            
    except Exception as e:
        try:
            error_msg = str(e)
        except UnicodeEncodeError:
            error_msg = repr(e)
        print(f"Error during input file cleanup: {error_msg}")


def display_data_summary(data_files, output_dir):
    """Display summary of loaded data files"""
    import streamlit as st
    
    if not data_files:
        st.warning("No data files found. Please ensure the processing scripts have been run first.")
        return
    
    st.subheader("📊 Data Summary")
    
    # Create summary DataFrame
    summary_data = []
    for file_name, df in data_files.items():
        # Try to get the file timestamp from the correct directory
        try:
            file_path = output_dir / file_name
            if file_path.exists():
                last_updated = datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            else:
                last_updated = "Unknown"
        except:
            last_updated = "Unknown"
            
        summary_data.append({
            "File": file_name,
            "Records": len(df),
            "Columns": len(df.columns),
            "Last Updated": last_updated
        })
    
    summary_df = pd.DataFrame(summary_data)
    st.dataframe(summary_df, width='stretch')


def display_data_preview(data_files, selected_file):
    """Display preview of selected data file"""
    import streamlit as st
    from utils.ui_helpers import create_metric_box
    
    if selected_file in data_files:
        df = data_files[selected_file]
        st.subheader(f"📋 Preview: {selected_file}")
        
        # Display basic info with consistent sizing
        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown(create_metric_box("Total Records", str(len(df))), unsafe_allow_html=True)
        with col2:
            st.markdown(create_metric_box("Total Columns", str(len(df.columns))), unsafe_allow_html=True)
        with col3:
            st.markdown(create_metric_box("Memory Usage", f"{df.memory_usage(deep=True).sum() / 1024:.1f} KB"), unsafe_allow_html=True)
        
        # Display data preview
        st.dataframe(df.head(10), width='stretch')
        
        # Display column info
        with st.expander("📝 Column Information"):
            col_info = pd.DataFrame({
                'Column': df.columns,
                'Type': df.dtypes,
                'Non-Null Count': df.count(),
                'Null Count': df.isnull().sum()
            })
            st.dataframe(col_info, width='stretch')

