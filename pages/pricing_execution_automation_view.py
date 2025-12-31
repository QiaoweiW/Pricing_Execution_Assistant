"""
Pricing Execution Automation (VBCS Generator) page view.

This module provides the Streamlit UI for the VBCS (Value-Based Cost Structure) generator.
It handles four main functions:
1. Fixed Pricing - Generate VBCS files for fixed and quarterly pricing items
2. KS Pricing - Generate VBCS files for Kirkland Signature items
3. Variable Pricing - Generate VBCS files for variable pricing items with Excel automation
4. Combine VBCS - Combine multiple VBCS files into a single file

Key Features:
- File upload and validation
- Processing script execution via subprocess
- Output file caching in session state (5-minute TTL)
- Excel automation error handling and display
- Download functionality for generated VBCS files

Author: Pricing Execution Agent Team
Last Updated: 2025-01-27
"""
import streamlit as st
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
from collections import Counter
import traceback
from datetime import datetime, timedelta

from utils.ui_helpers import apply_custom_css, create_metric_box, safe_error_message
from utils.data_helpers import load_existing_data
from utils.processing_helpers import run_processing_script


def _store_vbcs_in_cache(output_dataframes):
    """
    Store VBCS output files in session state cache with timestamps.
    
    Args:
        output_dataframes: Dictionary mapping filename to DataFrame
    """
    if 'vbcs_cache' not in st.session_state:
        st.session_state.vbcs_cache = {}
    if 'vbcs_cache_timestamps' not in st.session_state:
        st.session_state.vbcs_cache_timestamps = {}
    
    current_time = datetime.now()
    
    # Store each output file in cache with timestamp
    for filename, df in output_dataframes.items():
        st.session_state.vbcs_cache[filename] = df
        st.session_state.vbcs_cache_timestamps[filename] = current_time


def _cleanup_vbcs_cache():
    """
    Clean up VBCS cache entries older than 5 minutes.
    
    This ensures that output files don't persist indefinitely in session state.
    Files are automatically removed after 5 minutes or when new files are generated.
    """
    if 'vbcs_cache' in st.session_state and 'vbcs_cache_timestamps' in st.session_state:
        current_time = datetime.now()
        cutoff_time = current_time - timedelta(minutes=5)
        files_to_remove = [
            filename for filename, timestamp in st.session_state.vbcs_cache_timestamps.items()
            if timestamp < cutoff_time
        ]
        for filename in files_to_remove:
            if filename in st.session_state.vbcs_cache:
                del st.session_state.vbcs_cache[filename]
            if filename in st.session_state.vbcs_cache_timestamps:
                del st.session_state.vbcs_cache_timestamps[filename]


def render():
    """
    Render the main Pricing Execution Automation page.
    
    This function:
    - Sets up the page layout and styling
    - Manages session state for tool selection and data caching
    - Handles cache cleanup (removes entries older than 5 minutes)
    - Routes to appropriate tool functions based on user selection
    """
    apply_custom_css()
    
    st.markdown('<h1 class="main-header">Oracle Data Preparation Tool (VBCS Generator)</h1>', unsafe_allow_html=True)
    
    st.markdown("""
    ### Welcome

    This application helps you generate HTST & ESL Private Label VBCS files for Oracle upload. 
    The tool provides four main functions: **Fixed Pricing**, **KS Pricing**, **Variable Pricing**, and **Combine VBCS**.

    **Note**: What's not covered by this tool - custom model such as Bulk Milk (totes & tankers) and KS Organic milk.

    **How to navigate**: Click on any of the buttons below to switch between different tools. 
    Each tool will guide you through the process of uploading required files, running VBCS generation, and downloading the results (in CSV format).
    """)

    # Security notice as a separate, prominent element
    st.warning("🔒 **Security Notice:** For security reasons, all upload and download files will be automatically removed after each processing run.")

    # Load existing data from session state cache (not from persistent disk)
    # Clean up old cache entries first (older than 5 minutes)
    _cleanup_vbcs_cache()
    
    # Load from session state cache
    data_files = st.session_state.get('vbcs_cache', {})
    output_dir = Path("data")  # Keep for compatibility, but files are in cache

    # Tool selection at the top
    st.markdown("---")
    st.markdown('<h2 style="font-size: 1.8rem; color: #1f77b4; margin-bottom: 1rem;">Select Tool</h2>', unsafe_allow_html=True)

    # Create columns for tool selection with better visuals
    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        if st.button("**Fixed Pricing**\n\nClick to generate VBCS files for Fixed and Quarterly pricing items", width='stretch', type="primary", key="fixed_btn"):
            st.session_state.selected_tool = "Fixed Pricing"

    with col2:
        if st.button("**KS Pricing**\n\nClick to generate VBCS files for Kirkland Signature items", width='stretch', type="primary", key="ks_btn"):
            st.session_state.selected_tool = "KS Pricing"

    with col3:
        if st.button("**Variable Pricing**\n\nClick to generate VBCS files for variable pricing items", width='stretch', type="primary", key="var_btn"):
            st.session_state.selected_tool = "Variable Pricing"

    with col4:
        if st.button("**Combine VBCS**\n\nClick to combine all VBCS files into one", width='stretch', type="primary", key="combine_btn"):
            st.session_state.selected_tool = "Combine VBCS"

    with col5:
        if st.button("**Pricing Update Validation (Testing)**\n\nClick to analyze pricing discrepancies", width='stretch', type="primary", key="validation_btn"):
            st.session_state.selected_tool = "Pricing Update Validation"

    # Initialize session state if not exists
    if 'selected_tool' not in st.session_state:
        st.session_state.selected_tool = "Fixed Pricing"

    st.markdown("---")

    # Main content area
    if st.session_state.selected_tool == "Fixed Pricing":
        run_fixed_pricing(data_files)
    elif st.session_state.selected_tool == "KS Pricing":
        run_ks_pricing(data_files)
    elif st.session_state.selected_tool == "Variable Pricing":
        run_variable_pricing(data_files)
    elif st.session_state.selected_tool == "Combine VBCS":
        run_combine_vbcs(data_files)
    elif st.session_state.selected_tool == "Pricing Update Validation":
        run_pricing_validation(data_files)


def run_fixed_pricing(data_files):
    """Run Fixed Pricing VBCS Generation"""
    
    # Monthly update type selection
    st.subheader("Select Month Type")
    update_type = st.radio(
        "Select the current month type:",
        ["Quarterly Update Month", "Non-Quarterly Update Month"],
        help="Choose whether this is a quarterly update month or not. This affects which rows are included in the output."
    )
    
    # Show reminder message for quarterly updates
    if update_type == "Quarterly Update Month":
        # Reminder message - no emoji, plain text
        st.markdown("**Reminder**: As a reminder, the VBCS generation for quarterly pricing is managed in Excel. Please locate the correct file (e.g., \"KS Organic Price Build\") and navigate to the \"VBCS\" tab there.")
    
    # How to Use section
    st.subheader("How to Use")
    st.markdown("""
    This tool generates VBCS files for fixed pricing items based on market index and quarterly pricing.
    
    **What it does:**
    - Filters items with 'Fixed' or 'Quarterly' market index names
    - Excludes items starting with 'DG'
    - Applies effective dates from assumptions file
    - Generates VBCS format output for Oracle upload
    
    **Reminder - all csv files should be UTF-8 format**
    """)
    
    # Upload & Run section
    st.subheader("Upload & Run")
    
    col1, col2 = st.columns(2)
    
    with col1:
        price_build_file = st.file_uploader(
            "Upload Old_Price_Build.csv",
            type=['csv'],
            help="Price Build Report file"
        )
    
    with col2:
        assumptions_file = st.file_uploader(
            "Upload Effective_Date_Assumptions.csv",
            type=['csv'],
            help="Effective Date Assumptions file"
        )
    
    if st.button("Run Fixed Pricing Generation", type="primary"):
        if price_build_file is not None and assumptions_file is not None:
            with st.spinner("Processing Fixed Pricing data..."):
                # Prepare uploaded files
                uploaded_files = {
                    "Old_Price_Build.csv": price_build_file.getvalue(),
                    "Effective_Date_Assumptions.csv": assumptions_file.getvalue()
                }
                
                # Create output directory
                output_dir = Path("data")
                output_dir.mkdir(exist_ok=True)
                
                # Run the processing script
                success, message, output_dataframes = run_processing_script("Fixed_Pricing_VBCS", uploaded_files, output_dir)
                # Store output in cache if available (for consistency, though Fixed Pricing may not use cache)
                if success and output_dataframes:
                    if 'vbcs_cache' not in st.session_state:
                        st.session_state.vbcs_cache = {}
                    if 'vbcs_cache_timestamps' not in st.session_state:
                        st.session_state.vbcs_cache_timestamps = {}
                    from datetime import datetime
                    current_time = datetime.now()
                    for filename, df in output_dataframes.items():
                        st.session_state.vbcs_cache[filename] = df
                        st.session_state.vbcs_cache_timestamps[filename] = current_time
                
                if success:
                    st.success(f"Success: {message}")
                    st.rerun()  # Refresh the page to show new data
                else:
                    # Safely display error message (handle Unicode encoding issues)
                    safe_msg = safe_error_message(message)
                    st.error(f"Error: {safe_msg}")
        else:
            st.warning("Please upload both required files before running the generation.")
    
    # Download Output section
    st.subheader("Download Output")
    
    if "fixed_vbcs.csv" in data_files:
        df = data_files["fixed_vbcs.csv"]
        
        # Apply filtering based on update type
        if update_type == "Quarterly Update Month":
            # Remove rows where Market column contains "Quarterly"
            if 'Market' in df.columns:
                original_count = len(df)
                df_filtered = df[~df['Market'].str.contains('Quarterly', na=False)]
                filtered_count = len(df_filtered)
                st.info(f"📊 Quarterly Update Month: Removed {original_count - filtered_count} rows containing 'Quarterly' in Market column. {filtered_count} rows remaining.")
                df = df_filtered
            else:
                st.warning("'Market' column not found in data. No filtering applied.")
        else:
            # Non-Quarterly Update Month - keep all data as is
            st.info("Non-Quarterly Update Month: All data included (no filtering applied).")
        
        # Download button
        csv = df.to_csv(index=False)
        st.download_button(
            label="Download Fixed Pricing VBCS",
            data=csv,
            file_name="fixed_vbcs.csv",
            mime="text/csv"
        )
    else:
        st.info("No data available for download. Please run the generation first.")


def run_ks_pricing(data_files):
    """Run KS Pricing VBCS Generation"""
    
    # How to Use section
    st.subheader("How to Use")
    st.markdown("""
    This tool generates VBCS files for KS (Kirkland Signature) items with Costco-specific pricing.
    
    **What it does:**
    - Filters KS items from Price Build Report
    - Matches with Costco region-specific pricing
    - Applies CLASS market index filtering
    - Generates VBCS format for both EA and CA UOMs
    
    **Reminder - all csv files should be UTF-8 format**
    """)
    
    # Upload & Run section
    st.subheader("Upload & Run")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        costco_prices_file = st.file_uploader(
            "Upload Costco_HTST_Pricing.csv",
            type=['csv'],
            help="Costco pricing data file"
        )
    
    with col2:
        price_build_file = st.file_uploader(
            "Upload Old_Price_Build.csv",
            type=['csv'],
            help="Price Build Report file"
        )
    
    with col3:
        regions_file = st.file_uploader(
            "Upload Costco_HTST_Region_Lookup.csv",
            type=['csv'],
            help="Costco regions lookup file"
        )
    
    with col4:
        assumptions_file = st.file_uploader(
            "Upload Effective_Date_Assumptions.csv",
            type=['csv'],
            help="Effective Date Assumptions file"
        )
    
    if st.button("Run KS Pricing Generation", type="primary"):
        if costco_prices_file is not None and price_build_file is not None and regions_file is not None and assumptions_file is not None:
            with st.spinner("Processing KS Pricing data..."):
                # Prepare uploaded files
                uploaded_files = {
                    "Costco_HTST_Pricing.csv": costco_prices_file.getvalue(),
                    "Old_Price_Build.csv": price_build_file.getvalue(),
                    "Costco_HTST_Region_Lookup.csv": regions_file.getvalue(),
                    "Effective_Date_Assumptions.csv": assumptions_file.getvalue()
                }
                
                # Create output directory
                output_dir = Path("data")
                output_dir.mkdir(exist_ok=True)
                
                # Run the processing script
                success, message, output_dataframes = run_processing_script("KS_Pricing_VBCS", uploaded_files, output_dir)
                # Store output in cache if available (for consistency, though KS Pricing may not use cache)
                if success and output_dataframes:
                    if 'vbcs_cache' not in st.session_state:
                        st.session_state.vbcs_cache = {}
                    if 'vbcs_cache_timestamps' not in st.session_state:
                        st.session_state.vbcs_cache_timestamps = {}
                    from datetime import datetime
                    current_time = datetime.now()
                    for filename, df in output_dataframes.items():
                        st.session_state.vbcs_cache[filename] = df
                        st.session_state.vbcs_cache_timestamps[filename] = current_time
                
                if success:
                    st.success(f"Success: {message}")
                    st.rerun()  # Refresh the page to show new data
                else:
                    # Safely display error message (handle Unicode encoding issues)
                    safe_msg = safe_error_message(message)
                    st.error(f"Error: {safe_msg}")
        else:
            st.warning("Please upload all 4 required files before running the generation.")
    
    # Download Output section
    st.subheader("Download Output")
    
    if "ks_htst_vbcs.csv" in data_files:
        df = data_files["ks_htst_vbcs.csv"]
        
        # Download button
        csv = df.to_csv(index=False)
        st.download_button(
            label="Download KS Pricing VBCS",
            data=csv,
            file_name="ks_htst_vbcs.csv",
            mime="text/csv"
        )
    else:
        st.info("No data available for download. Please run the generation first.")


def run_variable_pricing(data_files):
    """
    Run Variable Pricing VBCS Generation.
    
    This function:
    - Handles file uploads for Variable Pricing processing
    - Executes the Variable_Pricing_VBCS processing script
    - Stores output files in session state cache (5-minute TTL)
    - Displays Excel automation status and errors
    - Provides download functionality for generated VBCS files
    
    Args:
        data_files: Dictionary of existing data files (for compatibility, not used for Variable Pricing)
    """
    
    # How to Use section
    st.subheader("How to Use")
    st.markdown("""
    This tool generates VBCS files for variable pricing items with dynamic market-based calculations.
    
    **What it does:**
    - Processes execution data with UOM calculations
    - Applies effective dates and market classifications
    - Handles special cross-dock logic for Winco and URM customers
    - Generates separate VBCS files for different customer groups
    
    **Reminder - all csv files should be UTF-8 format**
    """)
    
    # Upload & Run section
    st.subheader("Upload & Run")
    
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        execution_file = st.file_uploader(
            "Upload Execution_final.csv",
            type=['csv'],
            help="Execution data file"
        )
    
    with col2:
        uom_file = st.file_uploader(
            "Upload HTST Pricing_UOMS_v1.csv",
            type=['csv'],
            help="UOM data file"
        )
    
    with col3:
        market_file = st.file_uploader(
            "Upload Milk_Market_Index.csv",
            type=['csv'],
            help="Market index file"
        )
    
    with col4:
        dates_file = st.file_uploader(
            "Upload Effective_Date_Assumptions.csv",
            type=['csv'],
            help="Effective dates file"
        )
    
    with col5:
        customer_report_file = st.file_uploader(
            "Upload Customer_Extract_Report.csv",
            type=['csv'],
            help="Customer report file for cross-dock logic"
        )
    
    if st.button("Run Variable Pricing Generation", type="primary"):
        if execution_file is not None and uom_file is not None and dates_file is not None and market_file is not None and customer_report_file is not None:
            with st.spinner("Processing Variable Pricing data..."):
                # Prepare uploaded files
                uploaded_files = {
                    "Execution_final.csv": execution_file.getvalue(),
                    "HTST Pricing_UOMS_v1.csv": uom_file.getvalue(),
                    "Effective_Date_Assumptions.csv": dates_file.getvalue(),
                    "Milk_Market_Index.csv": market_file.getvalue(),
                    "Customer_Extract_Report.csv": customer_report_file.getvalue()
                }
                
                # Create output directory
                output_dir = Path("data")
                output_dir.mkdir(exist_ok=True)
                
                # Run the processing script (generates CSV files first, then runs Excel automation automatically)
                success, message, output_dataframes = run_processing_script("Variable_Pricing_VBCS", uploaded_files, output_dir)
                
                if success:
                    # Store output files in session state immediately after CSV generation
                    # CSV files are generated first in the script, so they're available right away
                    if output_dataframes:
                        _store_vbcs_in_cache(output_dataframes)
                        _cleanup_vbcs_cache()
                    
                    # Check for Excel automation errors in the message (from stdout/stderr)
                    has_excel_error = ("ERROR: Failed to process URM custom sheet" in message or 
                                      "ERROR: Failed to process Winco custom sheet" in message or 
                                      "Excel automation error" in message or 
                                      "Failed to process" in message or
                                      "⚠️ Excel Automation Warnings" in message)
                    
                    # Check if macros completed but email might not have been sent
                    has_email_warning = ("NOTE: If you did not receive an email" in message or
                                        "INFO: Email should have been sent" in message)
                    
                    if has_excel_error:
                        st.success("✅ VBCS files generated successfully and available for download!")
                        st.warning("⚠️ Excel automation encountered issues. VBCS files are available for download, but email automation did not complete.")
                        with st.expander("🔍 Excel Automation Debug Information", expanded=True):
                            # Extract error details from message
                            error_lines = [line for line in message.split('\n') if 'ERROR' in line.upper() or 'Excel' in line or 'macro' in line.lower() or 'Failed' in line]
                            if error_lines:
                                st.text("Error Details:")
                                for line in error_lines:
                                    st.text(line)
                            else:
                                st.text("Full output:")
                                st.text(message)
                            st.markdown("**Common issues and solutions:**")
                            st.markdown("""
                            1. **Excel template file not found**: Verify the file exists at the expected location
                            2. **Macros disabled**: Enable macros in Excel security settings
                            3. **Macros not present**: 
                               - URM: Ensure macros Step1_UpdateData, Step2_SaveNewMonthasValues, Step3_SendPreparedEmail exist
                               - Winco: Ensure macros Step1_RollForwardData, Step2_ExportCleanVersion, Step3_EmailPriceList exist
                            4. **pywin32 not installed**: The script will attempt to auto-install, but you may need to install manually: `pip install pywin32`
                            5. **Excel file open**: Close the Excel file if it's open in another application
                            6. **Email not configured**: Check that email settings are configured in the Excel macros
                            7. **Outlook not running**: Ensure Outlook is installed, configured, and running
                            8. **Email in spam/junk**: Check your spam/junk folder for the email
                            """)
                    elif has_email_warning:
                        st.success("✅ VBCS files generated successfully and available for download!")
                        st.info("📧 Excel automation completed. If you did not receive an email, please check:")
                        st.markdown("""
                        - **Outlook is running**: The email macros require Outlook to be open and configured
                        - **Check Sent Items**: Verify the email was sent by checking Outlook's Sent Items folder
                        - **Email address**: Ensure your email address is in the macro's recipient list
                        - **Spam folder**: Check your spam/junk folder
                        - **Email settings**: Verify email settings in the Excel macros are correct
                        """)
                        # Show the success message with email note
                        st.success(f"Success: {message.split('NOTE:')[0].strip()}")
                    else:
                        st.success("✅ VBCS files generated successfully and available for download!")
                        st.success(f"Success: {message}")
                    
                    st.rerun()  # Refresh the page to show new data
                else:
                    # Safely display error message (handle Unicode encoding issues)
                    safe_msg = safe_error_message(message)
                    st.error(f"Error: {safe_msg}")
                    # Show detailed error information in the browser
                    with st.expander("🔍 Debug Information", expanded=True):
                        st.text(f"Error Details: {message}")
                        st.text("This error usually indicates an encoding issue with one of your CSV files.")
                        st.text("Please check that all your CSV files are saved with proper encoding (UTF-8 recommended).")
                        st.text("The Customer_Extract_Report.csv file was detected as having special characters.")
        else:
            st.warning("Please upload all 5 required files before running the generation.")
    
    # Download Output section
    st.subheader("Download Output")
    
    # Check for variable pricing files in session state cache
    variable_files = {
        "URM/TOPCO": "urm_vbcs.csv",
        "Winco": "winco_vbcs.csv",
        "Batch": "batch_vbcs.csv"
    }
    
    # Load from session state cache (not from disk)
    cache_data_files = st.session_state.get('vbcs_cache', {})
    available_files = {name: file_name for name, file_name in variable_files.items() if file_name in cache_data_files}
    
    if available_files:
        # Display download buttons for each file from cache
        for name, file_name in available_files.items():
            try:
                df = cache_data_files[file_name]
                csv = df.to_csv(index=False)
                st.download_button(
                    label=f"Download {name} VBCS",
                    data=csv,
                    file_name=f"{name.lower()}_vbcs.csv",
                    mime="text/csv"
                )
            except Exception as e:
                safe_msg = safe_error_message(e)
                st.error(f"Error loading {name} data: {safe_msg}")
        
        # New button for Excel automation (only show if URM or Winco files exist)
        st.markdown("---")
        st.markdown("### 📧 Receive Emails about URM & Winco DSD Sheets")
        
        has_urm_or_winco = "urm_vbcs.csv" in cache_data_files or "winco_vbcs.csv" in cache_data_files
        
        if has_urm_or_winco:
            if st.button("Receive URM & Winco DSD sheets for Customer Distribution", type="primary"):
                with st.spinner("Running Excel automation and sending email notifications..."):
                    # Create output directory
                    output_dir = Path("data")
                    output_dir.mkdir(exist_ok=True)
                    
                    # Run Excel automation script
                    # Use empty uploaded_files since we only need to run Excel automation
                    success, message, _ = run_processing_script("Variable_Pricing_VBCS", {}, output_dir, excel_automation=True)
                    
                    if success:
                        # Check for Excel automation errors
                        has_excel_error = ("ERROR: Failed to process URM custom sheet" in message or 
                                          "ERROR: Failed to process Winco custom sheet" in message or 
                                          "Excel automation error" in message or 
                                          "Failed to process" in message)
                        
                        has_email_warning = ("NOTE: If you did not receive an email" in message or
                                            "INFO: Email should have been sent" in message)
                        
                        if has_excel_error:
                            st.warning("⚠️ Excel automation encountered issues.")
                            with st.expander("🔍 Excel Automation Debug Information", expanded=True):
                                error_lines = [line for line in message.split('\n') if 'ERROR' in line.upper() or 'Excel' in line or 'macro' in line.lower() or 'Failed' in line]
                                if error_lines:
                                    st.text("Error Details:")
                                    for line in error_lines:
                                        st.text(line)
                                else:
                                    st.text("Full output:")
                                    st.text(message)
                                st.markdown("**Common issues and solutions:**")
                                st.markdown("""
                                1. **Excel template file not found**: Verify the file exists at the expected location
                                2. **Macros disabled**: Enable macros in Excel security settings
                                3. **Macros not present**: 
                                   - URM: Ensure macros Step1_UpdateData, Step2_SaveNewMonthasValues, Step3_SendPreparedEmail exist
                                   - Winco: Ensure macros Step1_RollForwardData, Step2_ExportCleanVersion, Step3_EmailPriceList exist
                                4. **pywin32 not installed**: The script will attempt to auto-install, but you may need to install manually: `pip install pywin32`
                                5. **Excel file open**: Close the Excel file if it's open in another application
                                6. **Email not configured**: Check that email settings are configured in the Excel macros
                                7. **Outlook not running**: Ensure Outlook is installed, configured, and running
                                8. **Email in spam/junk**: Check your spam/junk folder for the email
                                """)
                        elif has_email_warning:
                            st.success("✅ Excel automation completed!")
                            st.info("📧 If you did not receive an email, please check:")
                            st.markdown("""
                            - **Outlook is running**: The email macros require Outlook to be open and configured
                            - **Check Sent Items**: Verify the email was sent by checking Outlook's Sent Items folder
                            - **Email address**: Ensure your email address is in the macro's recipient list
                            - **Spam folder**: Check your spam/junk folder
                            - **Email settings**: Verify email settings in the Excel macros are correct
                            """)
                        else:
                            st.success("✅ Excel automation completed and emails sent successfully!")
                            st.info("📧 Please check your inbox for the URM and Winco DSD sheets.")
                        
                        st.info("""
                        **Next Steps:**
                        - Review the custom sheets with a focus on comparing $unit price changes against the mover file.
                        - If no further changes are needed, proceed with sending these files out to customers.
                        - Customer contact information can be found here: [Customer Contact Information](https://darigold1com.sharepoint.com/:t:/r/sites/CPPricing2/Shared%20Documents/General/Monthly%20and%20Quarterly%20Price%20Updates/03%20Custom%20Pricing%20Models/Customer%20Contact_URM%20%26%20Winco.txt?csf=1&web=1&e=Bel9Gr).
                        - If revisions are required, please first update execution_final to ensure correct VBCS formats are generated.
                        """)
                    else:
                        safe_msg = safe_error_message(message)
                        st.error(f"Error running Excel automation: {safe_msg}")
                        with st.expander("🔍 Debug Information", expanded=True):
                            st.text(f"Error Details: {message}")
        else:
            st.info("URM or Winco VBCS files are required to run Excel automation. Please ensure CSV files are generated first.")
    else:
        st.info("No data available for download. Please run the generation first.")


def run_combine_vbcs(data_files):
    """Run VBCS File Combination"""
    
    # How to Use section
    st.subheader("How to Use")
    st.markdown("""
    This tool combines all generated VBCS files into a single comprehensive file to enable efficient validation of upload success. The VBCS files that can be uploaded include: 1) fixed_vbcs, 2) ks_htst_vbcs, 3) batch_vbcs, 4) urm_topco_vbcs, 5) winco_vbcs, 6) bulk_vbcs, 7) walmart_vbcs, 8) us_foods_vbcs, 9) ks_organic_vbcs. All csv files should be UTF-8 format.
    """)
    
    # Upload & Run section
    st.subheader("Upload & Run")
    
    # Multiple file uploader
    uploaded_files = st.file_uploader(
        "Upload VBCS files to combine",
        type=['csv'],
        accept_multiple_files=True,
        help="Upload one or more VBCS CSV files. Supported files: fixed_vbcs, ks_htst_vbcs, batch_vbcs, urm_topco_vbcs, winco_vbcs, bulk_vbcs, walmart_vbcs, us_foods_vbcs, ks_organic_vbcs"
    )
    
    if st.button("Run Combine VBCS Generation", type="primary"):
        if uploaded_files and len(uploaded_files) > 0:
            with st.spinner("Combining VBCS files..."):
                try:
                    # Create output directory
                    output_dir = Path("data")
                    output_dir.mkdir(exist_ok=True)
                    
                    # Clean up existing combined file before creating new one
                    combined_file_path = output_dir / "combined_all_vbcs.csv"
                    if combined_file_path.exists():
                        combined_file_path.unlink()
                    
                    # Process all uploaded files
                    combined_dfs = []
                    for file_obj in uploaded_files:
                        try:
                            # Read the uploaded file
                            df = pd.read_csv(file_obj)
                            
                            # Keep only the first 21 columns
                            original_column_count = len(df.columns)
                            if original_column_count > 21:
                                df = df.iloc[:, :21]
                                st.info(f"{file_obj.name}: Kept first 21 columns out of {original_column_count} total columns")
                            
                            df['Source_File'] = file_obj.name  # Add source file column
                            combined_dfs.append(df)
                            st.success(f"Loaded {file_obj.name}: {len(df)} records")
                        except Exception as e:
                            safe_msg = safe_error_message(e)
                            st.error(f"Error loading {file_obj.name}: {safe_msg}")
                            continue  # Continue with other files instead of returning
                    
                    if combined_dfs:
                        # Concatenate all dataframes
                        combined_df = pd.concat(combined_dfs, ignore_index=True)
                        
                        # Remove duplicates
                        combined_df = combined_df.drop_duplicates()
                        
                        st.info(f"Combined data: {len(combined_df)} records with {len(combined_df.columns)} columns (first 21 columns from each file)")
                        
                        # Save to data directory
                        output_path = output_dir / "combined_all_vbcs.csv"
                        combined_df.to_csv(output_path, index=False)
                        
                        # Verify the file was saved
                        if output_path.exists():
                            file_size = output_path.stat().st_size
                            st.success(f"Success: Successfully combined {len(combined_dfs)} files into {len(combined_df)} records!")
                            st.info(f"Combined file saved: {output_path} ({file_size} bytes)")
                            
                            # Force reload the data to include the new combined file
                            st.info("Reloading data to include combined file...")
                            data_files, output_dir = load_existing_data()
                            
                            # Verify the combined file is now in data_files
                            if "combined_all_vbcs.csv" in data_files:
                                st.success("Combined file successfully loaded and available for download!")
                            else:
                                st.warning("Combined file saved but not loaded. Please refresh the page.")
                        else:
                            st.error(f"Error: Failed to save combined file to {output_path}")
                        
                        st.rerun()  # Refresh the page to show new data
                    else:
                        st.error("Error: No VBCS files could be processed successfully.")
                        
                except Exception as e:
                    safe_msg = safe_error_message(e)
                    st.error(f"Error: {safe_msg}")
        else:
            st.warning("Please upload at least one VBCS file before running the combination.")
    
    # Download Output section
    st.subheader("Download Output")
    
    # Debug: Show what files are available
    if data_files:
        st.info(f"Available data files: {list(data_files.keys())}")
    else:
        st.info("No data files loaded")
    
    if "combined_all_vbcs.csv" in data_files:
        df = data_files["combined_all_vbcs.csv"]
        
        # Download button
        csv = df.to_csv(index=False)
        st.download_button(
            label="Download Combined VBCS",
            data=csv,
            file_name="combined_all_vbcs.csv",
            mime="text/csv"
        )
    else:
        st.info("No combined data available for download. Please run the combination first.")


def run_pricing_validation(data_files):
    """Run pricing validation analysis"""
    st.markdown("### Pricing Update Validation")
    
    st.info("🚧 Pricing validation feature is under development.")
    st.markdown("""
    This feature will analyze pricing discrepancies and validate pricing updates.
    
    **Planned functionality:**
    - Compare pricing data across different time periods
    - Identify pricing anomalies and discrepancies
    - Validate pricing calculations
    - Generate validation reports
    
    **Coming soon in a future update!**
    """)

