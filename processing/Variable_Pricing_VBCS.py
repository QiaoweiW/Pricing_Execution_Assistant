"""
Variable Pricing VBCS Processing Script

This script processes variable pricing data and generates VBCS (Value-Based Cost Structure) files
for Oracle upload. It handles URM/TOPCO, Winco, and Batch customer groups separately.

Key Features:
- Processes execution data with UOM calculations
- Applies effective dates and market classifications
- Handles special cross-dock logic for Winco and URM customers
- Generates separate VBCS CSV files for different customer groups
- Excel automation for URM and Winco custom sheets (Windows only):
  * URM: Opens template, runs macros, pastes data, sends email
  * Winco: Opens template, runs macros, pastes data, sends email

Excel Automation:
- Requires pywin32 library (auto-installs on Windows)
- Processes custom Excel sheets with macros
- Sends automated emails via Excel macros
- Non-blocking: VBCS generation continues even if Excel automation fails

Author: Pricing Execution Agent Team
"""
import pandas as pd
import numpy as np
import sys # Used to exit gracefully if files are not found
import os
from pathlib import Path
import subprocess
import importlib.util

# Try to import duckdb with better error handling
try:
    import duckdb
    print("DuckDB imported successfully")
except ImportError as e:
    print(f"Error importing DuckDB: {e}")
    print("Please ensure DuckDB is installed: pip install duckdb")
    sys.exit(1)

# --- Configuration ---
# Set to True to save intermediate files for debugging, False for production
SAVE_INTERMEDIATE_FILES = False 

def get_relative_path(relative_path):
    """
    Get absolute path from relative path, works from script directory
    """
    script_dir = Path(__file__).parent.absolute()
    return script_dir / relative_path

# Define folder paths using robust relative path handling
EXECUTION_FOLDER = get_relative_path('../../../../')
PL_FOLDER = get_relative_path('../../../../../Monthly Refreshed Data_Common/')
STABLE_FOLDER = get_relative_path('../../../../../Monthly Refreshed Data_Common/Stable/')
OUTPUT_FOLDER = get_relative_path('../../../Output/')
CUSTOMER_REPORT_PATH = get_relative_path('../../../../../Monthly Refreshed Data_Common/Customer_Extract_Report.csv')

# Define file names
EXECUTION_FILE = 'Execution_final.csv'
UOM_FILE = 'HTST Pricing_UOMS_v1.csv'
EFFECTIVE_DATES_FILE = 'Effective_Date_Assumptions.csv'
MARKET_INDEX_FILE = get_relative_path('../../../../../Monthly Refreshed Data_Common/Stable/Milk_Market_Index.csv')

# --- Main Processing Function ---

def generate_vbcs_files():
    """
    Generate VBCS CSV files from execution data.
    
    This function performs all data processing steps (1-9) and generates the three
    VBCS CSV files (urm_vbcs.csv, winco_vbcs.csv, batch_vbcs.csv).
    
    Returns:
        tuple: (urm_output_path, winco_output_path, batch_output_path, urm_vbcs, winco_vbcs, batch_vbcs)
               Returns None for paths/DataFrames if processing fails
    """
    try:
        print("Starting Variable Pricing Execution - CSV Generation Phase...")
        print(f"Script location: {Path(__file__).parent.absolute()}")
        
        # 1. Load initial data
        execution_path = EXECUTION_FOLDER / EXECUTION_FILE
        print(f"Looking for execution file at: {execution_path}")
        if not execution_path.exists():
            print(f"ERROR: Execution file not found at: {execution_path}")
            return None, None, None, None, None, None
        
        execution_df = load_data(str(execution_path))
        print(f"SUCCESS: Loaded execution data: {len(execution_df)} rows")
        
        uom_path = STABLE_FOLDER / UOM_FILE
        print(f"Looking for UOM file at: {uom_path}")
        if not uom_path.exists():
            print(f"ERROR: UOM file not found at: {uom_path}")
            return None, None, None, None, None, None
            
        uom_df = load_data(str(uom_path))
        print(f"SUCCESS: Loaded UOM data: {len(uom_df)} rows")
        
        # 2. Merge UOM and calculate prices
        processed_df = merge_uom_and_calculate_prices(execution_df, uom_df)
        if SAVE_INTERMEDIATE_FILES:
            processed_df.to_csv(f"{EXECUTION_FOLDER}TEMP_1_Calculated_Prices.csv", index=False)

        # 3. Pivot data from wide to long format and apply rounding
        pivoted_df = pivot_and_round_data(processed_df)
        if SAVE_INTERMEDIATE_FILES:
            pivoted_df.to_csv(f"{EXECUTION_FOLDER}TEMP_2_Pivoted_Data.csv", index=False)

        # 4. Skip Oracle validation - use pivoted data directly
        validated_df = pivoted_df.copy()
        validated_df['Prior Month Oracle Price'] = 0
        validated_df['Price Difference'] = 0
        
        # 5. Apply effective dates
        dates_path = PL_FOLDER / EFFECTIVE_DATES_FILE
        print(f"Looking for dates file at: {dates_path}")
        if not dates_path.exists():
            print(f"ERROR: Dates file not found at: {dates_path}")
            return None, None, None, None, None, None
            
        dates_df = load_data(str(dates_path))
        print(f"SUCCESS: Loaded dates data: {len(dates_df)} rows")
        dated_df = apply_effective_dates(validated_df, dates_df)
        if SAVE_INTERMEDIATE_FILES:
            dated_df.to_csv(f"{EXECUTION_FOLDER}TEMP_4_With_Dates.csv", index=False)

        # 6. Apply market class from Milk Market Index file
        print(f"Looking for market index file at: {MARKET_INDEX_FILE}")
        if not MARKET_INDEX_FILE.exists():
            print(f"ERROR: Market index file not found at: {MARKET_INDEX_FILE}")
            return None, None, None, None, None, None
            
        market_df = load_data(str(MARKET_INDEX_FILE))
        print(f"SUCCESS: Loaded market index data: {len(market_df)} rows")
        final_data_df = apply_market_class(dated_df, market_df)
        if SAVE_INTERMEDIATE_FILES:
            final_data_df.to_csv(f"{EXECUTION_FOLDER}TEMP_5_With_Market_Index.csv", index=False)

        # 7. Format the data into the final VBCS structure
        vbcs_initial_df = format_for_vbcs(final_data_df)

        # 8. Handle special cross-dock logic for Winco and URM
        print(f"Looking for customer report at: {CUSTOMER_REPORT_PATH}")
        if not CUSTOMER_REPORT_PATH.exists():
            print(f"ERROR: Customer report file not found at: {CUSTOMER_REPORT_PATH}")
            return None, None, None, None, None, None
            
        customer_report_df = load_data(str(CUSTOMER_REPORT_PATH))
        print(f"SUCCESS: Loaded customer report data: {len(customer_report_df)} rows")
        vbcs_final_df = handle_crossdock(vbcs_initial_df, customer_report_df)
        
        # 9. Split data and create three separate VBCS files
        # Split data based on customer names
        urm_vbcs = vbcs_final_df[vbcs_final_df['Customername'].str.contains('URM|TOPCO', case=False, na=False)].copy()
        winco_vbcs = vbcs_final_df[vbcs_final_df['Customername'].str.contains('WINCO', case=False, na=False)].copy()
        batch_vbcs = vbcs_final_df[~vbcs_final_df['Customername'].str.contains('URM|TOPCO|WINCO', case=False, na=False)].copy()
        
        # Remove duplicate rows from all three datasets
        urm_vbcs = urm_vbcs.drop_duplicates()
        winco_vbcs = winco_vbcs.drop_duplicates()
        batch_vbcs = batch_vbcs.drop_duplicates()
        
        # Ensure Pricelistname column has the correct value in all three datasets
        urm_vbcs['Pricelistname'] = "CP_Market Price"
        winco_vbcs['Pricelistname'] = "CP_Market Price"
        batch_vbcs['Pricelistname'] = "CP_Market Price"
        
        # Save the three separate files
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        urm_output_path = OUTPUT_FOLDER / "urm_vbcs.csv"
        winco_output_path = OUTPUT_FOLDER / "winco_vbcs.csv"
        batch_output_path = OUTPUT_FOLDER / "batch_vbcs.csv"
        
        urm_vbcs.to_csv(urm_output_path, index=False)
        winco_vbcs.to_csv(winco_output_path, index=False)
        batch_vbcs.to_csv(batch_output_path, index=False)
        
        print(f" Successfully created three VBCS files:")
        print(f"   - URM/TOPCO customers: {urm_output_path} ({len(urm_vbcs)} rows)")
        print(f"   - Winco customers: {winco_output_path} ({len(winco_vbcs)} rows)")
        print(f"   - Batch customers: {batch_output_path} ({len(batch_vbcs)} rows)")
        
        return urm_output_path, winco_output_path, batch_output_path, urm_vbcs, winco_vbcs, batch_vbcs
        
    except UnicodeDecodeError as e:
        print(f"ERROR: Unicode encoding error: {e}")
        print(f"This usually means one of your CSV files has special characters that can't be decoded.")
        print(f"Please check your CSV files for special characters or try saving them as UTF-8.")
        print(f"The Customer_Extract_Report.csv file was detected as having special characters.")
        print(f"Enhanced handling should have processed this file - check the logs above for details.")
        import traceback
        print(f"Full error details: {traceback.format_exc()}")
        return None, None, None, None, None, None
    except Exception as e:
        print(f"ERROR: Error during CSV generation: {e}")
        import traceback
        print(f"Full error details: {traceback.format_exc()}")
        return None, None, None, None, None, None


def send_email_notifications(urm_output_path, winco_output_path, urm_vbcs, winco_vbcs):
    """
    Send email notifications via Excel macros.
    
    This function opens Excel files, pastes CSV data, and runs email macros only.
    This is step 2 in the new workflow: CSV files are already generated and available for download.
    
    Args:
        urm_output_path: Path to urm_vbcs.csv file
        winco_output_path: Path to winco_vbcs.csv file
        urm_vbcs: DataFrame containing URM VBCS data
        winco_vbcs: DataFrame containing Winco VBCS data
    """
    # Excel automation only works on Windows, so skip on other platforms
    if sys.platform != "win32":
        print(f"INFO: Email automation skipped (not running on Windows - current platform: {sys.platform})")
        return
    
    # Send URM email notification
    if urm_output_path and urm_output_path.exists() and urm_vbcs is not None and len(urm_vbcs) > 0:
        print("\n" + "=" * 80)
        print("Starting URM email notification...")
        print("=" * 80)
        try:
            send_urm_email(urm_output_path)
            print("=" * 80)
            print("SUCCESS: URM email notification sent successfully")
            print("=" * 80)
            print("NOTE: If you did not receive an email, please check:")
            print("  1. Outlook is configured and running")
            print("  2. Email settings in the Excel macro are correct")
            print("  3. Your email address is in the macro's recipient list")
            print("  4. Check Outlook's Sent Items folder to verify email was sent")
            print("=" * 80)
        except Exception as e:
            print("=" * 80)
            print(f"ERROR: Failed to send URM email notification: {e}")
            import traceback
            print(f"Full error details: {traceback.format_exc()}")
            print("=" * 80)
            print("DIAGNOSTIC: This error indicates the email automation failed.")
            print("  The VBCS files were generated successfully and are available for download.")
            print("  To troubleshoot email issues:")
            print("    1. Check if Excel file exists and macros are enabled")
            print("    2. Verify Outlook is installed and configured")
            print("    3. Check macro email settings in the Excel file")
            print("=" * 80)
            # Don't exit - allow the script to continue even if email automation fails
    
    # Send Winco email notification
    if winco_output_path and winco_output_path.exists() and winco_vbcs is not None and len(winco_vbcs) > 0:
        print("\n" + "=" * 80)
        print("Starting Winco email notification...")
        print("=" * 80)
        try:
            send_winco_email(winco_output_path)
            print("=" * 80)
            print("SUCCESS: Winco email notification sent successfully")
            print("=" * 80)
            print("NOTE: If you did not receive an email, please check:")
            print("  1. Outlook is configured and running")
            print("  2. Email settings in the Excel macro are correct")
            print("  3. Your email address is in the macro's recipient list")
            print("  4. Check Outlook's Sent Items folder to verify email was sent")
            print("=" * 80)
        except Exception as e:
            print("=" * 80)
            print(f"ERROR: Failed to send Winco email notification: {e}")
            import traceback
            print(f"Full error details: {traceback.format_exc()}")
            print("=" * 80)
            print("DIAGNOSTIC: This error indicates the email automation failed.")
            print("  The VBCS files were generated successfully and are available for download.")
            print("  To troubleshoot email issues:")
            print("    1. Check if Excel file exists and macros are enabled")
            print("    2. Verify Outlook is installed and configured")
            print("    3. Check macro email settings in the Excel file")
            print("=" * 80)
            # Don't exit - allow the script to continue even if email automation fails


def run_excel_macros(urm_output_path, winco_output_path, urm_vbcs, winco_vbcs):
    """
    Run Excel macros for data processing (Step1 and Step2 macros).
    
    This function runs the Excel automation macros that process and save data.
    This is step 3 in the new workflow: CSV files are available and emails have been sent.
    
    Args:
        urm_output_path: Path to urm_vbcs.csv file
        winco_output_path: Path to winco_vbcs.csv file
        urm_vbcs: DataFrame containing URM VBCS data
        winco_vbcs: DataFrame containing Winco VBCS data
    """
    # Excel automation only works on Windows, so skip on other platforms
    if sys.platform != "win32":
        print(f"INFO: Excel macro automation skipped (not running on Windows - current platform: {sys.platform})")
        return
    
    # Run URM macros
    if urm_output_path and urm_output_path.exists() and urm_vbcs is not None and len(urm_vbcs) > 0:
        print("\n" + "=" * 80)
        print("Starting URM Excel macro processing...")
        print("=" * 80)
        try:
            run_urm_macros(urm_output_path)
            print("=" * 80)
            print("SUCCESS: URM Excel macros completed successfully")
            print("=" * 80)
        except Exception as e:
            print("=" * 80)
            print(f"ERROR: Failed to run URM Excel macros: {e}")
            import traceback
            print(f"Full error details: {traceback.format_exc()}")
            print("=" * 80)
            print("DIAGNOSTIC: This error indicates the Excel macro automation failed.")
            print("  The VBCS files were generated successfully and are available for download.")
            print("  To troubleshoot macro issues:")
            print("    1. Check if Excel file exists and macros are enabled")
            print("    2. Verify the macros Step1_UpdateData and Step2_SaveNewMonthasValues exist")
            print("    3. Check macro security settings in Excel")
            print("=" * 80)
            # Don't exit - allow the script to continue even if macro automation fails
    
    # Run Winco macros
    if winco_output_path and winco_output_path.exists() and winco_vbcs is not None and len(winco_vbcs) > 0:
        print("\n" + "=" * 80)
        print("Starting Winco Excel macro processing...")
        print("=" * 80)
        try:
            run_winco_macros(winco_output_path)
            print("=" * 80)
            print("SUCCESS: Winco Excel macros completed successfully")
            print("=" * 80)
        except Exception as e:
            print("=" * 80)
            print(f"ERROR: Failed to run Winco Excel macros: {e}")
            import traceback
            print(f"Full error details: {traceback.format_exc()}")
            print("=" * 80)
            print("DIAGNOSTIC: This error indicates the Excel macro automation failed.")
            print("  The VBCS files were generated successfully and are available for download.")
            print("  To troubleshoot macro issues:")
            print("    1. Check if Excel file exists and macros are enabled")
            print("    2. Verify the macros Step1_RollForwardData and Step2_ExportCleanVersion exist")
            print("    3. Check macro security settings in Excel")
            print("=" * 80)
            # Don't exit - allow the script to continue even if macro automation fails


def run_excel_automation():
    """
    Run Excel automation for URM and Winco custom sheets.
    
    This function runs the full Excel automation process:
    - URM: Step1_UpdateData → paste CSV → Step2_SaveNewMonthasValues → Step3_SendPreparedEmail
    - Winco: Step1_RollForwardData → paste CSV → Step2_ExportCleanVersion → Step3_EmailPriceList
    
    CSV files must already exist in OUTPUT_FOLDER before calling this function.
    """
    try:
        # Load CSV file paths from output directory
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        urm_output_path = OUTPUT_FOLDER / "urm_vbcs.csv"
        winco_output_path = OUTPUT_FOLDER / "winco_vbcs.csv"
        
        # Verify CSV files exist
        if not urm_output_path.exists() and not winco_output_path.exists():
            print("ERROR: No VBCS CSV files found. Please run CSV generation first.")
            return
        
        # Load DataFrames to check if they have data
        try:
            urm_vbcs = pd.read_csv(urm_output_path) if urm_output_path.exists() else None
            winco_vbcs = pd.read_csv(winco_output_path) if winco_output_path.exists() else None
        except Exception as e:
            print(f"ERROR: Failed to load CSV files: {e}")
            urm_vbcs = None
            winco_vbcs = None
        
        # Excel automation only works on Windows
        if sys.platform != "win32":
            print(f"INFO: Excel automation skipped (not running on Windows - current platform: {sys.platform})")
            return
        
        # Run full URM Excel automation (if urm_vbcs file exists)
        # This includes: Step1_UpdateData → paste CSV → Step2_SaveNewMonthasValues → Step3_SendPreparedEmail
        if urm_output_path.exists() and urm_vbcs is not None and len(urm_vbcs) > 0:
            print("\n" + "=" * 80)
            print("Starting URM Excel automation (Step1 → Paste CSV → Step2 → Step3)...")
            print("=" * 80)
            try:
                process_urm_custom_sheet_and_email(urm_output_path)
                print("=" * 80)
                print("SUCCESS: URM Excel automation completed and email sent")
                print("=" * 80)
                print("NOTE: If you did not receive an email, please check:")
                print("  1. Outlook is configured and running")
                print("  2. Email settings in the Excel macro are correct")
                print("  3. Your email address is in the macro's recipient list")
                print("  4. Check Outlook's Sent Items folder to verify email was sent")
                print("=" * 80)
            except Exception as e:
                print("=" * 80)
                print(f"ERROR: Failed to process URM custom sheet: {e}")
                import traceback
                print(f"Full error details: {traceback.format_exc()}")
                print("=" * 80)
                print("DIAGNOSTIC: This error indicates the Excel automation failed.")
                print("  The VBCS files were generated successfully and are available for download.")
                print("  To troubleshoot:")
                print("    1. Check if Excel file exists and macros are enabled")
                print("    2. Verify Outlook is installed and configured")
                print("    3. Check macro email settings in the Excel file")
                print("=" * 80)
        
        # Run full Winco Excel automation (if winco_vbcs file exists)
        # This includes: Step1_RollForwardData → paste CSV → Step2_ExportCleanVersion → Step3_EmailPriceList
        if winco_output_path.exists() and winco_vbcs is not None and len(winco_vbcs) > 0:
            print("\n" + "=" * 80)
            print("Starting Winco Excel automation (Step1 → Paste CSV → Step2 → Step3)...")
            print("=" * 80)
            try:
                process_winco_custom_sheet_and_email(winco_output_path)
                print("=" * 80)
                print("SUCCESS: Winco Excel automation completed and email sent")
                print("=" * 80)
                print("NOTE: If you did not receive an email, please check:")
                print("  1. Outlook is configured and running")
                print("  2. Email settings in the Excel macro are correct")
                print("  3. Your email address is in the macro's recipient list")
                print("  4. Check Outlook's Sent Items folder to verify email was sent")
                print("=" * 80)
            except Exception as e:
                print("=" * 80)
                print(f"ERROR: Failed to process Winco custom sheet: {e}")
                import traceback
                print(f"Full error details: {traceback.format_exc()}")
                print("=" * 80)
                print("DIAGNOSTIC: This error indicates the Excel automation failed.")
                print("  The VBCS files were generated successfully and are available for download.")
                print("  To troubleshoot:")
                print("    1. Check if Excel file exists and macros are enabled")
                print("    2. Verify Outlook is installed and configured")
                print("    3. Check macro email settings in the Excel file")
                print("=" * 80)
        
    except Exception as e:
        print(f"ERROR: Unexpected error in run_excel_automation(): {e}")
        import traceback
        print(f"Full error details: {traceback.format_exc()}")
        sys.exit(1)


def main():
    """
    Main function to generate VBCS CSV files.
    
    Workflow:
    1. Generate CSV files and make them available for download immediately
    
    Excel automation is handled separately via run_excel_automation() function.
    """
    try:
        # Generate CSV files
        urm_output_path, winco_output_path, batch_output_path, urm_vbcs, winco_vbcs, batch_vbcs = generate_vbcs_files()
        
        if urm_output_path is None:
            # Generation failed, error already printed
            return
        
        # CSV files are now available for download
        print("\n" + "=" * 80)
        print("CSV FILES GENERATED: Available for download immediately")
        print("=" * 80)
        print(f"   - URM/TOPCO customers: {urm_output_path} ({len(urm_vbcs)} rows)")
        print(f"   - Winco customers: {winco_output_path} ({len(winco_vbcs)} rows)")
        print(f"   - Batch customers: {batch_output_path} ({len(batch_vbcs)} rows)")
        print("=" * 80)
        
    except Exception as e:
        print(f"ERROR: Unexpected error in main(): {e}")
        import traceback
        print(f"Full error details: {traceback.format_exc()}")
        sys.exit(1)


# --- Helper Functions ---

def load_data(file_path, sheet_name=None):
    """Loads data from a CSV or Excel file into a pandas DataFrame."""
    try:
        if file_path.endswith('.csv'):
            # Special handling for Customer_Extract_Report.csv - use simple, proven method
            # This matches the previous working implementation
            if 'Customer_Extract_Report.csv' in file_path:
                print(f"Using special handling for Customer_Extract_Report.csv")
                try:
                    # Read as binary, clean problematic bytes, then convert to DataFrame
                    with open(file_path, 'rb') as f:
                        content = f.read()
                    
                    # Clean problematic bytes (Windows-1252 control characters)
                    import re
                    cleaned_content = re.sub(b'[\x80-\x9F]', b'', content)
                    
                    # Write cleaned content to temporary file
                    import os
                    temp_path = file_path + '.cleaned'
                    with open(temp_path, 'wb') as f:
                        f.write(cleaned_content)
                    
                    # Read the cleaned file with latin-1 (handles all bytes) and error replacement
                    df = pd.read_csv(temp_path, encoding='latin-1', on_bad_lines='skip', engine='python', errors='replace')
                    print(f"Successfully read Customer_Extract_Report.csv with binary cleaning: {len(df)} rows")
                    os.remove(temp_path)
                    return df
                except Exception as e:
                    print(f"Binary cleaning failed for Customer_Extract_Report.csv: {e}")
                    # Fall back to regular method below
            
            # Regular CSV reading for other files
            encodings_to_try = ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1', 'utf-16']
            
            for encoding in encodings_to_try:
                try:
                    print(f"Trying to read {file_path} with {encoding} encoding...")
                    df = pd.read_csv(file_path, encoding=encoding, on_bad_lines='skip')
                    print(f"Successfully read {file_path} with {encoding} encoding: {len(df)} rows")
                    return df
                except UnicodeDecodeError as e:
                    print(f"Failed to read {file_path} with {encoding}: {e}")
                    continue
                except Exception as e:
                    print(f"Error reading {file_path} with {encoding}: {e}")
                    continue
            
            # If all encodings fail, try with error handling and skip bad lines
            print(f"All encodings failed for {file_path}, trying with error replacement and bad line skipping...")
            try:
                return pd.read_csv(file_path, encoding='utf-8', errors='replace', on_bad_lines='skip')
            except:
                # Last resort: try with latin-1 and error replacement
                return pd.read_csv(file_path, encoding='latin-1', errors='replace', on_bad_lines='skip')
            
        elif file_path.endswith('.xlsx'):
            # Requires the 'openpyxl' library: pip install openpyxl
            return pd.read_excel(file_path, sheet_name=sheet_name)
        else:
            raise ValueError("Unsupported file format. Please use .csv or .xlsx")
    except FileNotFoundError:
        print(f"ERROR: Error: File not found at {file_path}")
        sys.exit(1) # Exit the script

def merge_uom_and_calculate_prices(execution_df, uom_df):
    """Merges UOM data, cleans data types, and calculates prices for different UOMs."""
    # Clean UOM data types
    for col in ['CA per ST', 'CA per PL', 'CA per BC']:
        uom_df[col] = pd.to_numeric(uom_df[col], errors='coerce')
        
    # Merge execution data with UOM data
    merged_df = pd.merge(execution_df, uom_df, left_on='Item', right_on='Product ID', how='left')
    
    # Clean merged data types
    merged_df['Item'] = pd.to_numeric(merged_df['Item'], errors='coerce')
    merged_df['Rounding Rule'] = pd.to_numeric(merged_df['Rounding Rule'], errors='coerce').fillna(0).astype(int)
    
    # Handle the Eaches per Case column - use the one from execution data if it exists, otherwise from UOM data
    if 'Eaches per Case' in merged_df.columns:
        merged_df['Eaches per Case'] = pd.to_numeric(merged_df['Eaches per Case'], errors='coerce')
    elif 'Eaches per Case_x' in merged_df.columns:
        merged_df['Eaches per Case'] = pd.to_numeric(merged_df['Eaches per Case_x'], errors='coerce')
        merged_df.drop(columns=['Eaches per Case_x'], inplace=True)
    elif 'Eaches per Case_y' in merged_df.columns:
        merged_df['Eaches per Case'] = pd.to_numeric(merged_df['Eaches per Case_y'], errors='coerce')
        merged_df.drop(columns=['Eaches per Case_y'], inplace=True)
    else:
        print(" Warning: No 'Eaches per Case' column found in merged data")
        merged_df['Eaches per Case'] = 1  # Default value
    
    merged_df['Total Price Per Pricing UOM ($/EA)'] = pd.to_numeric(merged_df['Total Price Per Pricing UOM ($/EA)'], errors='coerce')

    # Calculate prices based on UOM
    price_ea = merged_df['Total Price Per Pricing UOM ($/EA)']
    eaches_per_case = merged_df['Eaches per Case']
    price_ca = price_ea * eaches_per_case
    
    merged_df['Total Price Per Pricing UOM ($/CA)'] = price_ca
    merged_df['Total Price Per Pricing UOM ($/ST)'] = price_ca * merged_df['CA per ST']
    merged_df['Total Price Per Pricing UOM ($/PL)'] = price_ca * merged_df['CA per PL']
    merged_df['Total Price Per Pricing UOM ($/BC)'] = price_ca * merged_df['CA per BC']
    
    return merged_df

def pivot_and_round_data(df):
    """Pivots the DataFrame from wide to long format and applies custom rounding."""
    necessary_columns = [
        'Item', 'Customer Name', 'Party Site Name', 'Party Site Number', 'Pricing UOM',
        'Rounding Rule', 'Effective Dates', 'Total Price Per Pricing UOM ($/EA)',
        'Total Price Per Pricing UOM ($/CA)', 'Total Price Per Pricing UOM ($/ST)',
        'Total Price Per Pricing UOM ($/PL)', 'Total Price Per Pricing UOM ($/BC)'
    ]
    df_subset = df[necessary_columns]
    
    price_columns = [col for col in necessary_columns if col.startswith('Total Price')]
    
    # Use melt to unpivot the DataFrame
    melted_df = pd.melt(
        df_subset,
        id_vars=['Item', 'Customer Name', 'Party Site Name', 'Party Site Number', 'Rounding Rule', 'Effective Dates'],
        value_vars=price_columns,
        var_name='Original_Price_Column',
        value_name='Total Price'
    )
    
    # Extract the new Pricing UOM from the column name (e.g., EA from '...($/EA)')
    melted_df['Pricing UOM'] = melted_df['Original_Price_Column'].str.extract(r'\(\$/([A-Z]+)\)')
    melted_df.drop(columns='Original_Price_Column', inplace=True)
    
    # Filter out rows with a price of 0 or NaN
    melted_df.dropna(subset=['Total Price'], inplace=True)
    melted_df = melted_df[melted_df['Total Price'] != 0]
    
    # Apply rounding
    melted_df['Total Price Per Pricing UOM_Rounded'] = melted_df.apply(
        lambda row: round(row['Total Price'], row['Rounding Rule']),
        axis=1
    )
    melted_df.drop(columns='Total Price', inplace=True)
    
    return melted_df.reset_index(drop=True)

    
def apply_effective_dates(df, dates_df):
    """Applies adjustment start and end dates based on rules."""
    try:
        # Convert Effective Dates to int and handle any conversion errors
        df['Effective Dates'] = pd.to_numeric(df['Effective Dates'], errors='coerce')
        
        # Check for any NaN values after conversion
        invalid_dates = df[df['Effective Dates'].isna()]
        if not invalid_dates.empty:
            print(f" Warning: Found {len(invalid_dates)} rows with invalid Effective Dates values")
        
        dates_mapping = {
            1: "First Day", 2: "First Monday", 3: "16th of Month",
            4: "First Sunday", 5: "First Sunday (7 Day Leadtime)"
        }
        
        # Apply mapping only to valid numeric dates
        df['Effective Dates_Ref'] = df['Effective Dates'].map(dates_mapping)
        
        # Check for unmapped dates
        unmapped_dates = df[df['Effective Dates_Ref'].isna() & df['Effective Dates'].notna()]
        if not unmapped_dates.empty:
            print(f" Warning: Found {len(unmapped_dates)} rows with unmapped Effective Dates values")
        
        # Perform the merge
        merged_df = pd.merge(
            df,
            dates_df[['Rules', 'Adjustmentstartdate', 'Adjustmentenddate']],
            left_on='Effective Dates_Ref',
            right_on='Rules',
            how='left'
        )
        
        # Check for rows that didn't get dates from the merge
        missing_dates = merged_df[merged_df['Adjustmentstartdate'].isna()]
        if not missing_dates.empty:
            print(f" Warning: Found {len(missing_dates)} rows that couldn't be matched with effective date rules")
        
        # Clean up the merged dataframe
        result_df = merged_df.drop(columns=['Rules', 'Effective Dates_Ref'])
        
        # Set blank dates for any rows that couldn't get proper dates
        result_df.loc[result_df['Adjustmentstartdate'].isna(), 'Adjustmentstartdate'] = ''
        result_df.loc[result_df['Adjustmentenddate'].isna(), 'Adjustmentenddate'] = ''
        
        return result_df
        
    except Exception as e:
        print(f"ERROR: Error in apply_effective_dates: {e}")
        # Return dataframe with blank dates
        df_copy = df.copy()
        df_copy['Adjustmentstartdate'] = ''
        df_copy['Adjustmentenddate'] = ''
        return df_copy

def apply_market_class(df, market_df):
    """Applies the market index name from the Milk Market Index file."""
    try:
        # Check if required columns exist in market_df
        if 'Item' not in market_df.columns:
            print("ERROR: Error: 'Item' column not found in market index file")
            df_copy = df.copy()
            df_copy['Market Index Name'] = ''
            return df_copy
            
        if 'Market Index Name' not in market_df.columns:
            print("ERROR: Error: 'Market Index Name' column not found in market index file")
            df_copy = df.copy()
            df_copy['Market Index Name'] = ''
            return df_copy
        
        # Perform the merge
        merged_df = pd.merge(df, market_df[['Item', 'Market Index Name']], on='Item', how='left')
        
        # Check for rows that didn't get market index names from the merge
        missing_market = merged_df[merged_df['Market Index Name'].isna()]
        if not missing_market.empty:
            print(f" Warning: Found {len(missing_market)} rows that couldn't be matched with market index data")
        
        # Set blank market index names for any rows that couldn't get proper names
        merged_df.loc[merged_df['Market Index Name'].isna(), 'Market Index Name'] = ''
        
        return merged_df
        
    except Exception as e:
        print(f"ERROR: Error in apply_market_class: {e}")
        # Return dataframe with blank market index names
        df_copy = df.copy()
        df_copy['Market Index Name'] = ''
        return df_copy

def format_for_vbcs(df):
    """Formats the processed data into the final VBCS upload structure."""
    vbcs_df = pd.DataFrame()
    vbcs_df['Pricelistname'] = "CP_Market Price"
    vbcs_df['Pricinguom'] = df['Pricing UOM']
    vbcs_df['Baselineprice'] = 0.00
    vbcs_df['Chargestartdate'] = "1/1/2020 12:00:00 AM"
    vbcs_df['Chargeenddate'] = ""
    vbcs_df['Item_Name'] = df['Item']
    vbcs_df['Customername'] = df['Customer Name']
    vbcs_df['Customernumber'] = ""
    vbcs_df['Shiptositename'] = df['Party Site Name']
    vbcs_df['Customersitenumber'] = df['Party Site Number']
    vbcs_df['Adjustmenttype'] = "MARKUP_AMOUNT"
    vbcs_df['Adjustmentamount'] = df['Total Price Per Pricing UOM_Rounded']
    vbcs_df['Adjustmentbasis'] = ""
    vbcs_df['Precedence'] = ""
    vbcs_df['Market'] = df['Market Index Name']
    vbcs_df['Age'] = ""
    vbcs_df['Spec'] = ""
    vbcs_df['Grade'] = ""
    vbcs_df['Adjustmentstartdate'] = df['Adjustmentstartdate']  # Use dynamic dates from Effective Date Assumptions
    vbcs_df['Adjustmentenddate'] = df['Adjustmentenddate']      # Use dynamic dates from Effective Date Assumptions
    vbcs_df['Status'] = "N"
    vbcs_df[''] = ""  # Empty column as in reference file
    return vbcs_df

# --- Excel Automation Functions ---
# These functions handle Excel automation for URM and Winco custom sheets
# They use pywin32 (Windows-only) to interact with Excel COM objects

def check_and_install_pywin32():
    """
    Check if pywin32 is installed, and install it if not available.
    Returns True if pywin32 is available (either already installed or successfully installed).
    
    Note: pywin32 only works on Windows. This function will return False on non-Windows platforms.
    """
    # Check if running on Windows
    if sys.platform != "win32":
        print(f"WARNING: Excel automation requires Windows. Current platform: {sys.platform}")
        return False
    
    try:
        import win32com.client
        print("SUCCESS: pywin32 is already installed")
        return True
    except ImportError:
        print("WARNING: pywin32 is not installed. Attempting to install...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "pywin32"], 
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            print("SUCCESS: pywin32 installed successfully")
            # Try importing again after installation
            import win32com.client
            return True
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Failed to install pywin32 (return code {e.returncode})")
            print("Please install pywin32 manually: pip install pywin32")
            return False
        except Exception as e:
            print(f"ERROR: Failed to install pywin32: {e}")
            print("Please install pywin32 manually: pip install pywin32")
            return False

def _open_excel_workbook(excel_template_path):
    """
    Helper function to open an Excel workbook and return the application and workbook objects.
    
    This is a shared utility used by both URM and Winco processing functions
    to avoid code duplication while maintaining clear separation of concerns.
    
    Args:
        excel_template_path: Path to the Excel template file
        
    Returns:
        Tuple of (excel_app, workbook) COM objects
        
    Raises:
        Exception: If Excel cannot be opened or file not found
    """
    try:
        import win32com.client
    except ImportError:
        raise Exception("Failed to import win32com.client. Please ensure pywin32 is installed.")
    
    # Verify template file exists
    if not excel_template_path.exists():
        raise Exception(f"Excel template file not found at: {excel_template_path}")
    
    # Create Excel application object
    excel_app = win32com.client.Dispatch("Excel.Application")
    try:
        excel_app.Visible = False  # Run in background
    except:
        # Excel might already be visible, continue anyway
        pass
    excel_app.DisplayAlerts = False  # Suppress alerts
    
    # Open the workbook
    workbook = excel_app.Workbooks.Open(str(excel_template_path.absolute()))
    print("SUCCESS: Excel template opened")
    
    return excel_app, workbook

def _close_excel_workbook(excel_app, workbook):
    """
    Helper function to safely close Excel workbook and application.
    
    This is a shared utility used by both URM and Winco processing functions
    to ensure proper cleanup of COM objects.
    
    Args:
        excel_app: Excel application COM object (can be None)
        workbook: Excel workbook COM object (can be None)
    """
    try:
        if workbook:
            workbook.Close(SaveChanges=False)
        if excel_app:
            excel_app.Quit()
        # Release COM objects
        if workbook:
            del workbook
        if excel_app:
            del excel_app
        print("SUCCESS: Excel application closed")
    except Exception as e:
        print(f"WARNING: Error during Excel cleanup: {e}")

def _paste_csv_to_excel_sheet(workbook, csv_path, sheet_name="REMOVE IN FINAL SHARE-OUT"):
    """
    Efficiently paste CSV data into an Excel worksheet using bulk operations.
    
    This function:
    1. Clears all data from row 2 onwards (preserves row 1 if it has headers)
    2. Reads CSV data and matches columns by name with Excel sheet headers
    3. Pastes data using bulk Range.Value operations for optimal performance
    
    Performance optimizations:
    - Uses Excel Range.Value array assignment (bulk paste) instead of cell-by-cell
    - Clears data using Range.ClearContents (faster than deleting rows)
    - Matches columns by name for accurate data placement
    
    Args:
        workbook: Excel workbook COM object
        csv_path: Path to the CSV file to paste (should be the output file users can download)
        sheet_name: Name of the target sheet (default: "REMOVE IN FINAL SHARE-OUT")
        
    Raises:
        Exception: If sheet not found, CSV file not found, or paste operation fails
    """
    import time
    start_time = time.time()
    
    # Import time module at function level to avoid issues
    # (time is already imported at module level, but explicit for clarity)
    
    # Verify CSV file exists (should be the output file that users can download)
    csv_path = Path(csv_path)  # Ensure it's a Path object
    if not csv_path.exists():
        raise Exception(f"CSV file not found at: {csv_path}. This should be the output file that users can download.")
    
    # Read the CSV file
    print(f"Reading CSV file: {csv_path}")
    try:
        csv_df = pd.read_csv(csv_path)
        print(f"Loaded {len(csv_df)} rows and {len(csv_df.columns)} columns from CSV")
        
        # Validate CSV has data
        if len(csv_df) == 0:
            raise Exception(f"CSV file is empty: {csv_path}")
        if len(csv_df.columns) == 0:
            raise Exception(f"CSV file has no columns: {csv_path}")
            
        print(f"CSV columns: {list(csv_df.columns)[:5]}...")  # Show first 5 columns
    except Exception as e:
        raise Exception(f"Failed to read CSV file {csv_path}: {e}")
    
    # Find or activate the target sheet
    try:
        worksheet = workbook.Worksheets(sheet_name)
    except:
        raise Exception(f"Sheet '{sheet_name}' not found in Excel file. Please verify the sheet name.")
    
    worksheet.Activate()
    
    # Step 1: Clear existing data from row 2 onwards (preserve row 1 if it has headers)
    print("Clearing existing data from row 2 onwards...")
    try:
        # Get the used range to determine how much to clear
        used_range = worksheet.UsedRange
        if used_range is not None:
            last_row = used_range.Rows.Count
            last_col = used_range.Columns.Count
            
            # Clear from row 2 to the last used row
            if last_row > 1:
                clear_range = worksheet.Range(
                    worksheet.Cells(2, 1),
                    worksheet.Cells(last_row, last_col)
                )
                clear_range.ClearContents()
                print(f"Cleared {last_row - 1} rows of existing data")
        else:
            print("No existing data to clear")
    except Exception as e:
        print(f"Warning: Could not clear existing data: {e}. Continuing with paste operation...")
    
    # Step 2: Get Excel sheet headers (from row 1) to match columns
    print("Reading Excel sheet headers for column matching...")
    excel_headers = []
    col_idx = 1
    while True:
        header_cell = worksheet.Cells(1, col_idx).Value
        if header_cell is None or header_cell == "":
            break
        excel_headers.append(str(header_cell).strip())
        col_idx += 1
    
    if not excel_headers:
        raise Exception(f"No headers found in row 1 of sheet '{sheet_name}'. Please ensure the sheet has column headers.")
    
    print(f"Found {len(excel_headers)} columns in Excel sheet: {excel_headers[:5]}...")
    
    # Step 3: Match CSV columns to Excel columns by name
    csv_columns = csv_df.columns.tolist()
    column_mapping = {}  # Maps Excel column index to CSV column name
    
    for excel_col_idx, excel_header in enumerate(excel_headers, start=1):
        # Try to find matching CSV column (case-insensitive, strip whitespace)
        excel_header_clean = excel_header.strip()
        matched_csv_col = None
        
        for csv_col in csv_columns:
            if csv_col.strip().lower() == excel_header_clean.lower():
                matched_csv_col = csv_col
                break
        
        if matched_csv_col:
            column_mapping[excel_col_idx] = matched_csv_col
        else:
            # Column not found in CSV - will leave empty in Excel
            print(f"Warning: Excel column '{excel_header}' not found in CSV. Will be left empty.")
    
    if not column_mapping:
        raise Exception("No matching columns found between CSV and Excel sheet. Please verify column names match.")
    
    print(f"Matched {len(column_mapping)} columns between CSV and Excel sheet")
    
    # Step 4: Prepare data array for bulk paste (only matched columns, in Excel column order)
    num_rows = len(csv_df)
    num_cols = len(excel_headers)
    
    print(f"Preparing data array: {num_rows} rows x {num_cols} columns")
    
    # Create 2D array: [row][col] format for Excel
    # Excel expects array[row][col] where row and col are 0-indexed
    data_array = [[None] * num_cols for _ in range(num_rows)]
    
    # Fill the data array with matched column data
    matched_count = 0
    for excel_col_idx in range(1, num_cols + 1):
        if excel_col_idx in column_mapping:
            csv_col = column_mapping[excel_col_idx]
            matched_count += 1
            for row_idx in range(num_rows):
                value = csv_df.iloc[row_idx][csv_col]
                # Convert NaN, None, and other null types to empty string for Excel compatibility
                if pd.isna(value) or value is None:
                    data_array[row_idx][excel_col_idx - 1] = ""
                else:
                    # Convert to string if needed, but preserve numeric types
                    data_array[row_idx][excel_col_idx - 1] = value
    
    print(f"Populated data array with {matched_count} matched columns")
    
    # Validate data array has content
    non_empty_cells = sum(1 for row in data_array for cell in row if cell is not None and cell != "")
    print(f"Data array contains {non_empty_cells} non-empty cells out of {num_rows * num_cols} total")
    
    if non_empty_cells == 0:
        raise Exception("Data array is empty after column matching. No data to paste. Please verify CSV columns match Excel headers.")
    
    # Step 5: Bulk paste data using Range.Value (much faster than cell-by-cell)
    print(f"Pasting {num_rows} rows of data using bulk operation...")
    
    # Validate data array before pasting
    if num_rows == 0:
        print("WARNING: No data rows to paste. CSV file may be empty.")
        return  # Exit early if no data
    
    if not data_array or len(data_array) == 0:
        raise Exception("Data array is empty. Cannot paste empty data into Excel sheet.")
    
    try:
        # Optimize Excel settings for bulk paste performance
        original_screen_updating = worksheet.Application.ScreenUpdating
        original_enable_events = worksheet.Application.EnableEvents
        original_calculation = worksheet.Application.Calculation
        
        worksheet.Application.ScreenUpdating = False
        worksheet.Application.EnableEvents = False
        worksheet.Application.Calculation = -4105  # xlCalculationManual
        
        try:
            # Define the target range (row 2 to row num_rows+1, all columns)
            target_range = worksheet.Range(
                worksheet.Cells(2, 1),
                worksheet.Cells(num_rows + 1, num_cols)
            )
            
            # Use bulk paste with proper data format for Excel COM
            # Convert to proper Variant array format that Excel COM expects
            print(f"Attempting bulk paste for {num_rows} rows x {num_cols} columns...")
            
            # Clean and convert data to proper types for Excel COM
            # This ensures compatibility with both URM and Winco Excel files
            cleaned_array = []
            for row in data_array:
                cleaned_row = []
                for cell in row:
                    # Convert pandas/numpy types to native Python types for Excel COM
                    if pd.isna(cell) or cell is None:
                        cleaned_row.append("")
                    elif isinstance(cell, (pd.Timestamp, pd.DatetimeTZDtype)):
                        cleaned_row.append(str(cell))
                    elif isinstance(cell, (np.integer, np.floating)):
                        cleaned_row.append(float(cell))
                    elif isinstance(cell, np.bool_):
                        cleaned_row.append(bool(cell))
                    else:
                        cleaned_row.append(cell)
                cleaned_array.append(cleaned_row)
            
            # Initialize paste method tracking
            paste_method_used = None
            
            # Method 1: Try Value2 property with tuple format (fastest, most reliable for Winco)
            # Value2 is preferred over Value as it doesn't trigger Excel's type conversion
            try:
                # Convert to tuple of tuples for Excel COM (more reliable than list of lists)
                data_tuple = tuple(tuple(row) for row in cleaned_array)
                
                # Use Value2 property which is faster and more reliable than Value
                # Value2 bypasses Excel's formatting and is better for Winco files
                target_range.Value2 = data_tuple
                
                print("SUCCESS: Bulk paste (Value2 with tuple format) completed")
                paste_method_used = "bulk_value2"
                
            except Exception as value2_error:
                print(f"Value2 paste failed: {value2_error}. Trying Value property with tuple...")
                try:
                    # Fallback: Use Value property with tuple format
                    target_range.Value = data_tuple
                    print("SUCCESS: Bulk paste (Value with tuple format) completed")
                    paste_method_used = "bulk_value_tuple"
                except Exception as value_error:
                    print(f"Value with tuple failed: {value_error}. Trying Value with list...")
                    try:
                        # Fallback: Try with list of lists (some Excel versions prefer this)
                        target_range.Value = cleaned_array
                        print("SUCCESS: Bulk paste (Value with list format) completed")
                        paste_method_used = "bulk_value_list"
                    except Exception as list_error:
                        # Last resort: Use row-by-row for this specific case
                        print(f"All bulk paste methods failed. Using row-by-row fallback: {list_error}")
                        paste_method_used = "row_by_row"
                        
                        # Row-by-row fallback (only if bulk paste completely fails)
                        for row_idx, row_data in enumerate(cleaned_array):
                            for col_idx, cell_value in enumerate(row_data):
                                if cell_value is not None:
                                    worksheet.Cells(row_idx + 2, col_idx + 1).Value = cell_value
            
            # Re-enable Excel features
            worksheet.Application.ScreenUpdating = original_screen_updating
            worksheet.Application.EnableEvents = original_enable_events
            worksheet.Application.Calculation = original_calculation
            
            # Force Excel to refresh and calculate
            worksheet.Application.Calculate()
            
            # Small delay to ensure Excel processes the paste
            time.sleep(0.1)  # 100ms delay for Excel to process
            
            if paste_method_used:
                print(f"Paste operation completed using method: {paste_method_used}")
            else:
                print("WARNING: Paste method tracking not set - operation may have failed")
            
        except Exception as paste_error:
            # Re-enable Excel features even if paste fails
            worksheet.Application.ScreenUpdating = original_screen_updating
            worksheet.Application.EnableEvents = original_enable_events
            worksheet.Application.Calculation = original_calculation
            raise Exception(f"Paste operation failed: {paste_error}")
        
        # Verify paste was successful by checking if data exists
        # Check multiple rows and columns to account for empty cells
        print("Verifying paste operation...")
        has_data = False
        verification_samples = []
        rows_to_check = min(5, num_rows + 1)  # Check first 5 rows
        cols_to_check = min(num_cols, 30)  # Check up to 30 columns
        
        for row_check in range(2, rows_to_check + 1):
            for col_check in range(1, cols_to_check + 1):
                cell_value = worksheet.Cells(row_check, col_check).Value
                if cell_value is not None:
                    cell_str = str(cell_value).strip()
                    if cell_str != "":
                        has_data = True
                        verification_samples.append(f"R{row_check}C{col_check}={cell_str[:20]}")
                        if len(verification_samples) >= 5:  # Sample 5 cells
                            break
            if has_data:
                break
        
        if not has_data:
            # Final check: count non-empty cells in the entire range
            non_empty_count = 0
            for row_check in range(2, min(num_rows + 2, 100)):  # Check up to 100 rows
                for col_check in range(1, min(num_cols + 1, 50)):  # Check up to 50 columns
                    cell_value = worksheet.Cells(row_check, col_check).Value
                    if cell_value is not None and str(cell_value).strip() != "":
                        non_empty_count += 1
                        if non_empty_count >= 3:  # Found at least 3 non-empty cells
                            has_data = True
                            break
                if has_data:
                    break
            
            if not has_data:
                raise Exception(f"Paste verification failed: No data found in rows 2-{min(num_rows + 1, 100)}. Expected {num_rows} rows with data. This indicates the paste operation failed silently.")
        
        elapsed_time = time.time() - start_time
        print(f"SUCCESS: Pasted {num_rows} rows of data into '{sheet_name}' sheet in {elapsed_time:.2f} seconds")
        if verification_samples:
            print(f"VERIFICATION: Confirmed data exists (sample: {', '.join(verification_samples[:3])})")
        else:
            print(f"VERIFICATION: Confirmed data exists (found {non_empty_count} non-empty cells)")
        
    except Exception as e:
        error_msg = str(e)
        print(f"ERROR during paste operation: {error_msg}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        raise Exception(f"Failed to paste data into Excel sheet: {error_msg}")

def process_urm_custom_sheet_and_email(urm_csv_path):
    """
    Process URM custom sheet Excel file and send email via macros.
    
    This function:
    1. Opens the Excel template file
    2. Runs macro "Step1_UpdateData"
    3. Pastes urm_vbcs.csv data into "REMOVE IN FINAL SHARE-OUT" tab
    4. Runs macros "Step2_SaveNewMonthasValues" and "Step3_SendPreparedEmail"
    
    Args:
        urm_csv_path: Path to the urm_vbcs.csv file to paste into Excel
        
    Raises:
        Exception: If any step fails, with detailed error message
    """
    # Check if pywin32 is available
    if not check_and_install_pywin32():
        raise Exception("pywin32 is required for Excel automation but could not be installed. Please install manually: pip install pywin32")
    
    try:
        import win32com.client
    except ImportError:
        raise Exception("Failed to import win32com.client after installation. Please restart the application.")
    
    # Define Excel template path
    # Path relative to the script location: processing/Variable_Pricing_VBCS.py
    # Excel file is at: ../data/Pricing Execution/Custom Sheets/Monthly PL_URM Cross Dock_Price Lists_WITH LINKS.xlsm
    excel_template_path = get_relative_path('../data/Pricing Execution/Custom Sheets/Monthly PL_URM Cross Dock_Price Lists_WITH LINKS.xlsm')
    
    # Verify template file exists
    if not excel_template_path.exists():
        raise Exception(f"Excel template file not found at: {excel_template_path}")
    
    # Verify CSV file exists (this should be the output file that users can download)
    if not urm_csv_path.exists():
        raise Exception(f"URM VBCS CSV file not found at: {urm_csv_path}. This file should be the output file that users can download (urm_vbcs.csv).")
    
    print(f"Opening Excel template: {excel_template_path}")
    print(f"Using URM VBCS data from: {urm_csv_path} (this is the downloadable output file)")
    
    # Initialize Excel COM object
    excel_app = None
    workbook = None
    
    try:
        # Open Excel workbook using shared helper
        excel_app, workbook = _open_excel_workbook(excel_template_path)
        
        # Step 1: Run macro "Step1_UpdateData"
        print("Running macro: Step1_UpdateData...")
        try:
            excel_app.Run("Step1_UpdateData")
            print("SUCCESS: Macro Step1_UpdateData completed")
        except Exception as e:
            raise Exception(f"Failed to run macro 'Step1_UpdateData': {e}. Please ensure the macro exists in the Excel file.")
        
        # Step 2: Paste CSV data into "REMOVE IN FINAL SHARE-OUT" tab
        print("Pasting CSV data into 'REMOVE IN FINAL SHARE-OUT' tab...")
        try:
            _paste_csv_to_excel_sheet(workbook, urm_csv_path, "REMOVE IN FINAL SHARE-OUT")
        except Exception as e:
            raise Exception(f"Failed to paste CSV data into Excel sheet: {e}")
        
        # Step 3: Run macro "Step2_SaveNewMonthasValues"
        print("Running macro: Step2_SaveNewMonthasValues...")
        try:
            excel_app.Run("Step2_SaveNewMonthasValues")
            print("SUCCESS: Macro Step2_SaveNewMonthasValues completed")
        except Exception as e:
            raise Exception(f"Failed to run macro 'Step2_SaveNewMonthasValues': {e}. Please ensure the macro exists and is enabled.")
        
        # Step 4: Run macro "Step3_SendPreparedEmail"
        print("Running macro: Step3_SendPreparedEmail...")
        try:
            excel_app.Run("Step3_SendPreparedEmail")
            print("SUCCESS: Macro Step3_SendPreparedEmail completed")
            print("INFO: Email should have been sent via Outlook. Please check your inbox and sent items.")
        except Exception as e:
            error_msg = str(e)
            print(f"ERROR: Macro Step3_SendPreparedEmail failed: {error_msg}")
            # Provide more specific error information
            if "macro" in error_msg.lower() or "not found" in error_msg.lower():
                raise Exception(f"Failed to run macro 'Step3_SendPreparedEmail': {e}. Please ensure the macro exists in the Excel file.")
            elif "email" in error_msg.lower() or "outlook" in error_msg.lower():
                raise Exception(f"Failed to send email via macro 'Step3_SendPreparedEmail': {e}. Please check Outlook configuration and email settings in the Excel macro.")
            else:
                raise Exception(f"Failed to run macro 'Step3_SendPreparedEmail': {e}. Please ensure the macro exists and email settings are configured.")
        
        # Save and close workbook
        workbook.Save()
        print("SUCCESS: Workbook saved")
        
    except Exception as e:
        # Re-raise with context
        raise Exception(f"Excel automation error: {e}")
    
    finally:
        # Clean up: Close workbook and Excel application using shared helper
        _close_excel_workbook(excel_app, workbook)

def process_winco_custom_sheet_and_email(winco_csv_path):
    """
    Process Winco custom sheet Excel file and send email via macros.
    
    This function:
    1. Opens the Excel template file
    2. Runs macro "Step1_RollForwardData"
    3. Pastes winco_vbcs.csv data into "REMOVE IN FINAL SHARE-OUT" tab
    4. Runs macros "Step2_ExportCleanVersion" and "Step3_EmailPriceList"
    
    Args:
        winco_csv_path: Path to the winco_vbcs.csv file to paste into Excel
        
    Raises:
        Exception: If any step fails, with detailed error message
    """
    # Check if pywin32 is available
    if not check_and_install_pywin32():
        raise Exception("pywin32 is required for Excel automation but could not be installed. Please install manually: pip install pywin32")
    
    try:
        import win32com.client
    except ImportError:
        raise Exception("Failed to import win32com.client after installation. Please restart the application.")
    
    # Define Excel template path
    # Path relative to the script location: processing/Variable_Pricing_VBCS.py
    # Excel file is at: ../data/Pricing Execution/Custom Sheets/Monthly_Winco DSD Stores_Price Lists_WITH LINKS.xlsm
    excel_template_path = get_relative_path('../data/Pricing Execution/Custom Sheets/Monthly_Winco DSD Stores_Price Lists_WITH LINKS.xlsm')
    
    # Verify template file exists
    if not excel_template_path.exists():
        raise Exception(f"Excel template file not found at: {excel_template_path}")
    
    # Verify CSV file exists (this should be the output file that users can download)
    if not winco_csv_path.exists():
        raise Exception(f"Winco VBCS CSV file not found at: {winco_csv_path}. This file should be the output file that users can download (winco_vbcs.csv).")
    
    print(f"Opening Excel template: {excel_template_path}")
    print(f"Using Winco VBCS data from: {winco_csv_path} (this is the downloadable output file)")
    
    # Initialize Excel COM object
    excel_app = None
    workbook = None
    
    try:
        # Open Excel workbook using shared helper
        excel_app, workbook = _open_excel_workbook(excel_template_path)
        
        # Step 1: Run macro "Step1_RollForwardData"
        print("Running macro: Step1_RollForwardData...")
        try:
            excel_app.Run("Step1_RollForwardData")
            print("SUCCESS: Macro Step1_RollForwardData completed")
        except Exception as e:
            raise Exception(f"Failed to run macro 'Step1_RollForwardData': {e}. Please ensure the macro exists in the Excel file.")
        
        # Step 2: Paste CSV data into "REMOVE IN FINAL SHARE-OUT" tab
        print("Pasting CSV data into 'REMOVE IN FINAL SHARE-OUT' tab...")
        try:
            _paste_csv_to_excel_sheet(workbook, winco_csv_path, "REMOVE IN FINAL SHARE-OUT")
            print("SUCCESS: CSV data paste operation completed successfully")
        except Exception as e:
            error_msg = str(e)
            print(f"ERROR: Failed to paste CSV data into Excel sheet: {error_msg}")
            import traceback
            print(f"Full error details: {traceback.format_exc()}")
            raise Exception(f"Failed to paste CSV data into Excel sheet: {error_msg}")
        
        # Step 3: Run macro "Step2_ExportCleanVersion"
        print("Running macro: Step2_ExportCleanVersion...")
        try:
            excel_app.Run("Step2_ExportCleanVersion")
            print("SUCCESS: Macro Step2_ExportCleanVersion completed")
        except Exception as e:
            raise Exception(f"Failed to run macro 'Step2_ExportCleanVersion': {e}. Please ensure the macro exists and is enabled.")
        
        # Step 4: Run macro "Step3_EmailPriceList"
        print("Running macro: Step3_EmailPriceList...")
        try:
            excel_app.Run("Step3_EmailPriceList")
            print("SUCCESS: Macro Step3_EmailPriceList completed")
            print("INFO: Email should have been sent via Outlook. Please check your inbox and sent items.")
        except Exception as e:
            error_msg = str(e)
            print(f"ERROR: Macro Step3_EmailPriceList failed: {error_msg}")
            # Provide more specific error information
            if "macro" in error_msg.lower() or "not found" in error_msg.lower():
                raise Exception(f"Failed to run macro 'Step3_EmailPriceList': {e}. Please ensure the macro exists in the Excel file.")
            elif "email" in error_msg.lower() or "outlook" in error_msg.lower():
                raise Exception(f"Failed to send email via macro 'Step3_EmailPriceList': {e}. Please check Outlook configuration and email settings in the Excel macro.")
            else:
                raise Exception(f"Failed to run macro 'Step3_EmailPriceList': {e}. Please ensure the macro exists and email settings are configured.")
        
        # Save and close workbook
        workbook.Save()
        print("SUCCESS: Workbook saved")
        
    except Exception as e:
        # Re-raise with context
        raise Exception(f"Excel automation error: {e}")
    
    finally:
        # Clean up: Close workbook and Excel application using shared helper
        _close_excel_workbook(excel_app, workbook)

# --- New Modular Functions for Sequential Workflow ---
# These functions separate email notification from macro execution
# to implement the new workflow: CSV generation → Email → Macros

def send_urm_email(urm_csv_path):
    """
    Send URM email notification via Excel macro.
    
    This function opens Excel, pastes CSV data, and runs only the email macro (Step3_SendPreparedEmail).
    This is used in the new sequential workflow where emails are sent after CSV files are generated.
    
    Args:
        urm_csv_path: Path to the urm_vbcs.csv file to paste into Excel
        
    Raises:
        Exception: If any step fails, with detailed error message
    """
    # Check if pywin32 is available
    if not check_and_install_pywin32():
        raise Exception("pywin32 is required for Excel automation but could not be installed. Please install manually: pip install pywin32")
    
    try:
        import win32com.client
    except ImportError:
        raise Exception("Failed to import win32com.client after installation. Please restart the application.")
    
    # Define Excel template path
    excel_template_path = get_relative_path('../data/Pricing Execution/Custom Sheets/Monthly PL_URM Cross Dock_Price Lists_WITH LINKS.xlsm')
    
    # Verify template file exists
    if not excel_template_path.exists():
        raise Exception(f"Excel template file not found at: {excel_template_path}")
    
    # Verify CSV file exists
    if not urm_csv_path.exists():
        raise Exception(f"URM VBCS CSV file not found at: {urm_csv_path}")
    
    print(f"Opening Excel template for email notification: {excel_template_path}")
    print(f"Using URM VBCS data from: {urm_csv_path}")
    
    # Initialize Excel COM object
    excel_app = None
    workbook = None
    
    try:
        # Open Excel workbook using shared helper
        excel_app, workbook = _open_excel_workbook(excel_template_path)
        
        # Paste CSV data into "REMOVE IN FINAL SHARE-OUT" tab
        print("Pasting CSV data into 'REMOVE IN FINAL SHARE-OUT' tab...")
        try:
            _paste_csv_to_excel_sheet(workbook, urm_csv_path, "REMOVE IN FINAL SHARE-OUT")
        except Exception as e:
            raise Exception(f"Failed to paste CSV data into Excel sheet: {e}")
        
        # Run email macro only (Step3_SendPreparedEmail)
        print("Running email macro: Step3_SendPreparedEmail...")
        try:
            excel_app.Run("Step3_SendPreparedEmail")
            print("SUCCESS: Email macro Step3_SendPreparedEmail completed")
            print("INFO: Email should have been sent via Outlook. Please check your inbox and sent items.")
        except Exception as e:
            error_msg = str(e)
            print(f"ERROR: Email macro Step3_SendPreparedEmail failed: {error_msg}")
            if "macro" in error_msg.lower() or "not found" in error_msg.lower():
                raise Exception(f"Failed to run email macro 'Step3_SendPreparedEmail': {e}. Please ensure the macro exists in the Excel file.")
            elif "email" in error_msg.lower() or "outlook" in error_msg.lower():
                raise Exception(f"Failed to send email via macro 'Step3_SendPreparedEmail': {e}. Please check Outlook configuration and email settings in the Excel macro.")
            else:
                raise Exception(f"Failed to run email macro 'Step3_SendPreparedEmail': {e}. Please ensure the macro exists and email settings are configured.")
        
        # Save and close workbook
        workbook.Save()
        print("SUCCESS: Workbook saved")
        
    except Exception as e:
        raise Exception(f"Excel automation error during email notification: {e}")
    
    finally:
        # Clean up: Close workbook and Excel application using shared helper
        _close_excel_workbook(excel_app, workbook)


def send_winco_email(winco_csv_path):
    """
    Send Winco email notification via Excel macro.
    
    This function opens Excel, pastes CSV data, and runs only the email macro (Step3_EmailPriceList).
    This is used in the new sequential workflow where emails are sent after CSV files are generated.
    
    Args:
        winco_csv_path: Path to the winco_vbcs.csv file to paste into Excel
        
    Raises:
        Exception: If any step fails, with detailed error message
    """
    # Check if pywin32 is available
    if not check_and_install_pywin32():
        raise Exception("pywin32 is required for Excel automation but could not be installed. Please install manually: pip install pywin32")
    
    try:
        import win32com.client
    except ImportError:
        raise Exception("Failed to import win32com.client after installation. Please restart the application.")
    
    # Define Excel template path
    excel_template_path = get_relative_path('../data/Pricing Execution/Custom Sheets/Monthly_Winco DSD Stores_Price Lists_WITH LINKS.xlsm')
    
    # Verify template file exists
    if not excel_template_path.exists():
        raise Exception(f"Excel template file not found at: {excel_template_path}")
    
    # Verify CSV file exists
    if not winco_csv_path.exists():
        raise Exception(f"Winco VBCS CSV file not found at: {winco_csv_path}")
    
    print(f"Opening Excel template for email notification: {excel_template_path}")
    print(f"Using Winco VBCS data from: {winco_csv_path}")
    
    # Initialize Excel COM object
    excel_app = None
    workbook = None
    
    try:
        # Open Excel workbook using shared helper
        excel_app, workbook = _open_excel_workbook(excel_template_path)
        
        # Paste CSV data into "REMOVE IN FINAL SHARE-OUT" tab
        print("Pasting CSV data into 'REMOVE IN FINAL SHARE-OUT' tab...")
        try:
            _paste_csv_to_excel_sheet(workbook, winco_csv_path, "REMOVE IN FINAL SHARE-OUT")
            print("SUCCESS: CSV data paste operation completed successfully")
        except Exception as e:
            error_msg = str(e)
            print(f"ERROR: Failed to paste CSV data into Excel sheet: {error_msg}")
            import traceback
            print(f"Full error details: {traceback.format_exc()}")
            raise Exception(f"Failed to paste CSV data into Excel sheet: {error_msg}")
        
        # Run email macro only (Step3_EmailPriceList)
        print("Running email macro: Step3_EmailPriceList...")
        try:
            excel_app.Run("Step3_EmailPriceList")
            print("SUCCESS: Email macro Step3_EmailPriceList completed")
            print("INFO: Email should have been sent via Outlook. Please check your inbox and sent items.")
        except Exception as e:
            error_msg = str(e)
            print(f"ERROR: Email macro Step3_EmailPriceList failed: {error_msg}")
            if "macro" in error_msg.lower() or "not found" in error_msg.lower():
                raise Exception(f"Failed to run email macro 'Step3_EmailPriceList': {e}. Please ensure the macro exists in the Excel file.")
            elif "email" in error_msg.lower() or "outlook" in error_msg.lower():
                raise Exception(f"Failed to send email via macro 'Step3_EmailPriceList': {e}. Please check Outlook configuration and email settings in the Excel macro.")
            else:
                raise Exception(f"Failed to run email macro 'Step3_EmailPriceList': {e}. Please ensure the macro exists and email settings are configured.")
        
        # Save and close workbook
        workbook.Save()
        print("SUCCESS: Workbook saved")
        
    except Exception as e:
        raise Exception(f"Excel automation error during email notification: {e}")
    
    finally:
        # Clean up: Close workbook and Excel application using shared helper
        _close_excel_workbook(excel_app, workbook)


def run_urm_macros(urm_csv_path):
    """
    Run URM Excel macros for data processing (Step1 and Step2).
    
    This function opens Excel, runs Step1_UpdateData, pastes CSV data, and runs Step2_SaveNewMonthasValues.
    This is used in the new sequential workflow where macros run after emails have been sent.
    
    Args:
        urm_csv_path: Path to the urm_vbcs.csv file to paste into Excel
        
    Raises:
        Exception: If any step fails, with detailed error message
    """
    # Check if pywin32 is available
    if not check_and_install_pywin32():
        raise Exception("pywin32 is required for Excel automation but could not be installed. Please install manually: pip install pywin32")
    
    try:
        import win32com.client
    except ImportError:
        raise Exception("Failed to import win32com.client after installation. Please restart the application.")
    
    # Define Excel template path
    excel_template_path = get_relative_path('../data/Pricing Execution/Custom Sheets/Monthly PL_URM Cross Dock_Price Lists_WITH LINKS.xlsm')
    
    # Verify template file exists
    if not excel_template_path.exists():
        raise Exception(f"Excel template file not found at: {excel_template_path}")
    
    # Verify CSV file exists
    if not urm_csv_path.exists():
        raise Exception(f"URM VBCS CSV file not found at: {urm_csv_path}")
    
    print(f"Opening Excel template for macro processing: {excel_template_path}")
    print(f"Using URM VBCS data from: {urm_csv_path}")
    
    # Initialize Excel COM object
    excel_app = None
    workbook = None
    
    try:
        # Open Excel workbook using shared helper
        excel_app, workbook = _open_excel_workbook(excel_template_path)
        
        # Step 1: Run macro "Step1_UpdateData"
        print("Running macro: Step1_UpdateData...")
        try:
            excel_app.Run("Step1_UpdateData")
            print("SUCCESS: Macro Step1_UpdateData completed")
        except Exception as e:
            raise Exception(f"Failed to run macro 'Step1_UpdateData': {e}. Please ensure the macro exists in the Excel file.")
        
        # Step 2: Paste CSV data into "REMOVE IN FINAL SHARE-OUT" tab
        print("Pasting CSV data into 'REMOVE IN FINAL SHARE-OUT' tab...")
        try:
            _paste_csv_to_excel_sheet(workbook, urm_csv_path, "REMOVE IN FINAL SHARE-OUT")
        except Exception as e:
            raise Exception(f"Failed to paste CSV data into Excel sheet: {e}")
        
        # Step 3: Run macro "Step2_SaveNewMonthasValues"
        print("Running macro: Step2_SaveNewMonthasValues...")
        try:
            excel_app.Run("Step2_SaveNewMonthasValues")
            print("SUCCESS: Macro Step2_SaveNewMonthasValues completed")
        except Exception as e:
            raise Exception(f"Failed to run macro 'Step2_SaveNewMonthasValues': {e}. Please ensure the macro exists and is enabled.")
        
        # Save and close workbook
        workbook.Save()
        print("SUCCESS: Workbook saved")
        
    except Exception as e:
        raise Exception(f"Excel automation error during macro processing: {e}")
    
    finally:
        # Clean up: Close workbook and Excel application using shared helper
        _close_excel_workbook(excel_app, workbook)


def run_winco_macros(winco_csv_path):
    """
    Run Winco Excel macros for data processing (Step1 and Step2).
    
    This function opens Excel, runs Step1_RollForwardData, pastes CSV data, and runs Step2_ExportCleanVersion.
    This is used in the new sequential workflow where macros run after emails have been sent.
    
    Args:
        winco_csv_path: Path to the winco_vbcs.csv file to paste into Excel
        
    Raises:
        Exception: If any step fails, with detailed error message
    """
    # Check if pywin32 is available
    if not check_and_install_pywin32():
        raise Exception("pywin32 is required for Excel automation but could not be installed. Please install manually: pip install pywin32")
    
    try:
        import win32com.client
    except ImportError:
        raise Exception("Failed to import win32com.client after installation. Please restart the application.")
    
    # Define Excel template path
    excel_template_path = get_relative_path('../data/Pricing Execution/Custom Sheets/Monthly_Winco DSD Stores_Price Lists_WITH LINKS.xlsm')
    
    # Verify template file exists
    if not excel_template_path.exists():
        raise Exception(f"Excel template file not found at: {excel_template_path}")
    
    # Verify CSV file exists
    if not winco_csv_path.exists():
        raise Exception(f"Winco VBCS CSV file not found at: {winco_csv_path}")
    
    print(f"Opening Excel template for macro processing: {excel_template_path}")
    print(f"Using Winco VBCS data from: {winco_csv_path}")
    
    # Initialize Excel COM object
    excel_app = None
    workbook = None
    
    try:
        # Open Excel workbook using shared helper
        excel_app, workbook = _open_excel_workbook(excel_template_path)
        
        # Step 1: Run macro "Step1_RollForwardData"
        print("Running macro: Step1_RollForwardData...")
        try:
            excel_app.Run("Step1_RollForwardData")
            print("SUCCESS: Macro Step1_RollForwardData completed")
        except Exception as e:
            raise Exception(f"Failed to run macro 'Step1_RollForwardData': {e}. Please ensure the macro exists in the Excel file.")
        
        # Step 2: Paste CSV data into "REMOVE IN FINAL SHARE-OUT" tab
        print("Pasting CSV data into 'REMOVE IN FINAL SHARE-OUT' tab...")
        try:
            _paste_csv_to_excel_sheet(workbook, winco_csv_path, "REMOVE IN FINAL SHARE-OUT")
            print("SUCCESS: CSV data paste operation completed successfully")
        except Exception as e:
            error_msg = str(e)
            print(f"ERROR: Failed to paste CSV data into Excel sheet: {error_msg}")
            import traceback
            print(f"Full error details: {traceback.format_exc()}")
            raise Exception(f"Failed to paste CSV data into Excel sheet: {error_msg}")
        
        # Step 3: Run macro "Step2_ExportCleanVersion"
        print("Running macro: Step2_ExportCleanVersion...")
        try:
            excel_app.Run("Step2_ExportCleanVersion")
            print("SUCCESS: Macro Step2_ExportCleanVersion completed")
        except Exception as e:
            raise Exception(f"Failed to run macro 'Step2_ExportCleanVersion': {e}. Please ensure the macro exists and is enabled.")
        
        # Save and close workbook
        workbook.Save()
        print("SUCCESS: Workbook saved")
        
    except Exception as e:
        raise Exception(f"Excel automation error during macro processing: {e}")
    
    finally:
        # Clean up: Close workbook and Excel application using shared helper
        _close_excel_workbook(excel_app, workbook)

def handle_crossdock(vbcs_df, customer_report_df):
    """
    Replicates the SQL CROSS JOIN logic for Winco and URM customers.
    This applies pricing from a single source site to many destination sites.
    """
    # --- Winco Logic ---
    winco_initial = vbcs_df[vbcs_df['Customername'].str.contains('WINCO', case=False, na=False)].copy()
    other_customers = vbcs_df[~vbcs_df['Customername'].str.contains('WINCO', case=False, na=False)].copy()

    # Get the single source site's data (the template)
    winco_source_site = winco_initial[winco_initial['Shiptositename'] == 'WINCO 002 KENNEWICK DSD'].copy()
    
    # Get the list of destination sites to apply the template to
    winco_dest_sites = customer_report_df[
        customer_report_df['Party Name'].str.contains('WINCO', case=False, na=False) &
        (customer_report_df['Party Site Name'] != 'WINCO 002 KENNEWICK DSD')
    ][['Party Name', 'Party Site Number', 'Party Site Name']].copy()

    # Perform the cross join
    if not winco_source_site.empty and not winco_dest_sites.empty:
        # Drop columns that will be replaced by the destination site info
        winco_source_site.drop(columns=['Customername', 'Shiptositename', 'Customersitenumber'], inplace=True)
        winco_cross_join = winco_source_site.merge(winco_dest_sites, how='cross')
        
        # Rename columns to match the VBCS format
        winco_cross_join.rename(columns={
            'Party Name': 'Customername',
            'Party Site Name': 'Shiptositename',
            'Party Site Number': 'Customersitenumber'
        }, inplace=True)
        
        # Re-order columns to match the original and combine
        winco_final = pd.concat([winco_initial, winco_cross_join[vbcs_df.columns]], ignore_index=True)
    else:
        winco_final = winco_initial

    # --- URM Logic - Restored original logic with exclusion of unwanted customers ---
    # Use the original LIKE pattern logic for URM/TOPCO customers
    urm_customers = other_customers[
        other_customers['Customername'].str.contains('URM|TOPCO', case=False, na=False)
    ].copy()
    
    # EXCLUDE the specific unwanted customers that were incorrectly included
    unwanted_customers = [
        'DFS Gourmet Specialties, Inc., dba Better Butter',
        'FAIRFIELD GOURMET FOODS'
    ]
    
    urm_customers = urm_customers[
        ~urm_customers['Customername'].isin(unwanted_customers)
    ].copy()
    
    final_other_customers = other_customers[
        ~other_customers['Customername'].str.contains('URM|TOPCO', case=False, na=False)
    ].copy()
    
    # Add back the unwanted customers to the final_other_customers (they should go to batch)
    final_other_customers = pd.concat([
        final_other_customers,
        other_customers[other_customers['Customername'].isin(unwanted_customers)]
    ], ignore_index=True)

    # Get the single source site's data (the template)
    urm_source_site = urm_customers[urm_customers['Shiptositename'] == 'TOWN PUMP'].copy()

    # Get the list of destination sites to apply the template to - using original logic
    # EXCLUDE the unwanted customers from destination sites as well
    urm_dest_sites = customer_report_df[
        (customer_report_df['Party Name'].str.contains('URM|TOPCO', case=False, na=False)) &
        ~customer_report_df['Party Site Name'].isin(['TOWN PUMP', 'URM WHSE SPOKANE HTST', 'URM WHSE SPOKANE']) &
        ~customer_report_df['Party Name'].isin(unwanted_customers)
    ][['Party Name', 'Party Site Number', 'Party Site Name']].copy()

    if not urm_source_site.empty and not urm_dest_sites.empty:
        urm_source_site.drop(columns=['Customername', 'Shiptositename', 'Customersitenumber'], inplace=True)
        urm_cross_join = urm_source_site.merge(urm_dest_sites, how='cross')
        
        urm_cross_join.rename(columns={
            'Party Name': 'Customername',
            'Party Site Name': 'Shiptositename',
            'Party Site Number': 'Customersitenumber'
        }, inplace=True)
        
        urm_final = pd.concat([urm_customers, urm_cross_join[vbcs_df.columns]], ignore_index=True)
    else:
        urm_final = urm_customers
        
    # --- Combine all dataframes ---
    final_df = pd.concat([final_other_customers, winco_final, urm_final], ignore_index=True)
    
    return final_df

# --- Script Execution ---
if __name__ == "__main__":
    import argparse
    
    # Parse command-line arguments to support separate Excel automation execution
    parser = argparse.ArgumentParser(description='Variable Pricing VBCS Processing')
    parser.add_argument('--excel-automation', action='store_true',
                       help='Run Excel automation only (CSV files must already exist)')
    
    args = parser.parse_args()
    
    if args.excel_automation:
        # Run Excel automation only (CSV files should already exist)
        run_excel_automation()
    else:
        # Default: Generate CSV files only
        main()