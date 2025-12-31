"""
Processing script execution helper functions.
"""
import os
import sys
import subprocess
import tempfile
import time
import threading
from pathlib import Path
from datetime import datetime, timedelta


def _safe_exception_to_string(e):
    """
    Safely convert an exception to an ASCII-safe string.
    Avoids calling str(e) directly which can fail with UnicodeEncodeError on Windows.
    Handles cases where the exception message itself contains Unicode characters.
    
    Args:
        e: Exception object
    
    Returns:
        ASCII-safe string representation of the exception
    """
    error_type = type(e).__name__
    
    # Special handling for UnicodeEncodeError - these are particularly tricky
    # because their message often contains the problematic character
    if isinstance(e, UnicodeEncodeError):
        try:
            # For UnicodeEncodeError, we can access the problematic object directly
            if hasattr(e, 'object') and e.object:
                obj = e.object
                if isinstance(obj, str):
                    # Try to get a safe representation of the problematic string
                    safe_obj = obj.encode('utf-8', errors='replace').decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
                    return f"UnicodeEncodeError: Could not encode character (problematic text: {safe_obj[:50]}...)"
                elif isinstance(obj, bytes):
                    safe_obj = obj.decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
                    return f"UnicodeEncodeError: Could not encode bytes (problematic data: {safe_obj[:50]}...)"
        except Exception:
            pass
        # Fallback for UnicodeEncodeError
        return f"UnicodeEncodeError: Character encoding failed (could not safely convert error message)"
    
    # Try to get exception args directly (avoids calling __str__)
    try:
        if hasattr(e, 'args') and e.args and len(e.args) > 0:
            arg = e.args[0]
            # Handle different types
            if isinstance(arg, bytes):
                # Already bytes - decode safely
                return arg.decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
            elif isinstance(arg, str):
                # String - encode to UTF-8 bytes first, then to ASCII
                # This avoids the 'charmap' codec issue
                # Use 'replace' to handle any problematic characters
                utf8_bytes = arg.encode('utf-8', errors='replace')
                return utf8_bytes.decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
            else:
                # Other types - convert to string representation
                try:
                    arg_str = repr(arg)
                    return arg_str.encode('ascii', errors='replace').decode('ascii')
                except Exception:
                    return f"{error_type}: Error occurred (could not convert argument to string)"
    except Exception:
        pass
    
    # Try repr() which is usually ASCII-safe and doesn't call __str__
    try:
        error_repr = repr(e)
        return error_repr.encode('ascii', errors='replace').decode('ascii')
    except Exception:
        pass
    
    # Try to manually construct message from exception type and args
    try:
        if hasattr(e, 'args') and e.args:
            # Build message manually without calling str() on args
            args_parts = []
            for arg in e.args:
                try:
                    if isinstance(arg, str):
                        safe_arg = arg.encode('utf-8', errors='replace').decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
                        args_parts.append(f'"{safe_arg}"')
                    elif isinstance(arg, bytes):
                        safe_arg = arg.decode('utf-8', errors='replace').encode('ascii', errors='replace').decode('ascii')
                        args_parts.append(f'b"{safe_arg}"')
                    else:
                        args_parts.append(repr(arg).encode('ascii', errors='replace').decode('ascii'))
                except Exception:
                    args_parts.append('<could not convert>')
            args_str = ', '.join(args_parts)
            return f"{error_type}({args_str})"
        else:
            return f"{error_type}()"
    except Exception:
        pass
    
    # Final fallback
    return f"{error_type}: Error occurred (could not convert error message to ASCII)"


def cleanup_output_files(output_dir):
    """Clean up all output files in the data directory"""
    if output_dir and output_dir.exists():
        try:
            # Get all CSV files in the output directory
            csv_files = list(output_dir.glob("*.csv"))
            print(f"Cleaning up {len(csv_files)} files in {output_dir}")
            for file_path in csv_files:
                print(f"  - Deleting {file_path.name}")
                file_path.unlink()  # Delete the file
            return True, f"Cleaned up {len(csv_files)} output files"
        except Exception as e:
            # Safely handle exception message encoding
            error_msg = _safe_exception_to_string(e)
            print(f"Error cleaning up files: {error_msg}")
            return False, f"Error cleaning up files: {error_msg}"
    print("No output directory to clean")
    return True, "No output directory to clean"


def cleanup_old_files(directory, max_age_minutes=5):
    """
    Clean up files older than max_age_minutes in the given directory.
    
    Args:
        directory: Path to directory to clean
        max_age_minutes: Maximum age in minutes before files are deleted (default: 5)
    
    Returns:
        tuple: (success: bool, message: str, files_deleted: int)
    """
    if not directory or not directory.exists():
        return True, "Directory does not exist", 0
    
    try:
        max_age = timedelta(minutes=max_age_minutes)
        cutoff_time = datetime.now() - max_age
        
        csv_files = list(directory.glob("*.csv"))
        files_deleted = 0
        
        for file_path in csv_files:
            try:
                # Get file modification time
                file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                
                # Only delete input files (not output VBCS files)
                # Output files should persist for download
                is_output_file = any(name in file_path.name for name in [
                    "urm_vbcs.csv", "winco_vbcs.csv", "batch_vbcs.csv",
                    "fixed_vbcs.csv", "ks_htst_vbcs.csv", "combined_all_vbcs.csv"
                ])
                
                # Delete if file is old AND it's an input file (not an output file)
                if not is_output_file and file_mtime < cutoff_time:
                    file_path.unlink()
                    files_deleted += 1
                    print(f"Deleted old input file: {file_path.name} (age: {datetime.now() - file_mtime})")
            except Exception as e:
                # Continue with other files if one fails
                error_msg = _safe_exception_to_string(e)
                print(f"Error deleting {file_path.name}: {error_msg}")
        
        if files_deleted > 0:
            return True, f"Cleaned up {files_deleted} old input files", files_deleted
        else:
            return True, "No old files to clean up", 0
            
    except Exception as e:
        error_msg = _safe_exception_to_string(e)
        print(f"Error during cleanup: {error_msg}")
        return False, f"Error during cleanup: {error_msg}", 0


def run_processing_script(script_name, uploaded_files, output_dir, excel_automation=False):
    """
    Run a processing script with uploaded files.
    
    Args:
        script_name: Name of the processing script (without .py extension)
        uploaded_files: Dictionary of {filename: file_data} for uploaded files
        output_dir: Path to output directory
        excel_automation: If True, run Excel automation only (for Variable_Pricing_VBCS)
                         CSV files must already exist in output_dir
    
    Returns:
        tuple: (success: bool, message: str, output_dataframes: dict)
        - success: Whether the script ran successfully
        - message: Status message
        - output_dataframes: Dictionary of {filename: DataFrame} for output files (for session state storage)
    
    UPDATED 2025-01-26: Script path changed to use processing/ directory
    UPDATED 2025-01-27: Returns output DataFrames in memory instead of saving to persistent disk
    UPDATED 2025-01-27: Added excel_automation parameter for separate Excel automation execution
    """
    # VERSION MARKER - If you see this in logs, the updated code is loaded
    print("=" * 80)
    print("VERSION: processing_helpers.py - UPDATED 2025-01-26 - Using processing/ directory")
    print("=" * 80)
    try:
        # Clean up old input files (older than 5 minutes) from output directory
        # This ensures uploaded files don't persist indefinitely
        cleanup_old_success, cleanup_old_message, files_deleted = cleanup_old_files(output_dir, max_age_minutes=5)
        if cleanup_old_success and files_deleted > 0:
            print(f"Cleanup: {cleanup_old_message}")
        
        # Clean up existing output files before processing (optional - can be removed if you want to keep previous outputs)
        # cleanup_success, cleanup_message = cleanup_output_files(output_dir)
        # if not cleanup_success:
        #     return False, f"Failed to clean up existing files: {cleanup_message}"
        
        # Create a temporary directory for input files
        # This directory will be automatically deleted when the context manager exits
        with tempfile.TemporaryDirectory(prefix="pricing_agent_", suffix="_temp") as temp_dir:
            temp_path = Path(temp_dir)
            
            # Save uploaded files to temp directory with proper encoding detection
            # Verify all files are saved successfully before running the script
            # Note: For Excel automation only, uploaded_files may be empty
            files_saved_successfully = {}
            for file_name, file_data in uploaded_files.items():
                if file_data:  # Only save non-empty files
                    file_path = temp_path / file_name
                    print(f"Processing file: {file_name} ({len(file_data)} bytes)")
                    files_saved_successfully[file_name] = False
                    
                    # Try UTF-8 first (most common and what user uploaded)
                    success = False
                    
                    try:
                        # First try UTF-8 directly
                        decoded_data = file_data.decode('utf-8')
                        file_path.write_text(decoded_data, encoding='utf-8')
                        print(f"Successfully saved {file_name} using utf-8 encoding (no conversion needed)")
                        success = True
                    except UnicodeDecodeError:
                        print(f"UTF-8 failed for {file_name}, trying other encodings...")
                        
                        # Only if UTF-8 fails, try other encodings
                        encodings_to_try = ['latin-1', 'cp1252', 'iso-8859-1']
                        
                        for encoding in encodings_to_try:
                            try:
                                decoded_data = file_data.decode(encoding)
                                # Only clean if we had to convert from a different encoding
                                if file_name == "Customer_Extract_Report.csv":
                                    print(f"Converting {file_name} from {encoding} to UTF-8")
                                    
                                    # Remove problematic characters only if needed
                                    import re
                                    decoded_data = re.sub(r'[\x80-\x9F]', '', decoded_data)
                                    decoded_data = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', decoded_data)
                                    
                                    print(f"Cleaned problematic characters from {file_name}")
                                
                                file_path.write_text(decoded_data, encoding='utf-8')
                                print(f"Successfully saved {file_name} using {encoding} encoding (converted to UTF-8)")
                                success = True
                                break
                            except UnicodeDecodeError:
                                continue
                    
                    if not success:
                        # Special handling for Customer_Extract_Report.csv - use simple, proven method
                        # This matches the previous working implementation
                        if file_name == "Customer_Extract_Report.csv":
                            print(f"Using special handling for {file_name}")
                            try:
                                # Simple approach: decode as latin-1 (handles all bytes) and clean problematic characters
                                import re
                                decoded_data = file_data.decode('latin-1', errors='replace')
                                
                                # Remove problematic control characters and Windows-1252 problematic bytes
                                cleaned_data = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]', '', decoded_data)
                                
                                file_path.write_text(cleaned_data, encoding='utf-8')
                                print(f"Successfully cleaned and saved {file_name} using latin-1 with character cleaning")
                                success = True
                            except Exception as e:
                                error_msg = _safe_exception_to_string(e)
                                print(f"Special handling failed for {file_name}: {error_msg}")
                                # Last resort: save as bytes and let pandas handle it
                                file_path.write_bytes(file_data)
                                success = True
                        else:
                            # For other files, if all methods fail, save as bytes
                            if not success:
                                print(f"Warning: Could not decode {file_name}, saving as bytes")
                                file_path.write_bytes(file_data)
                                success = True
                    
                    # Verify file was saved and is readable
                    if success and file_path.exists():
                        try:
                            # Quick verification - try to read first few bytes
                            with open(file_path, 'rb') as f:
                                f.read(100)  # Read first 100 bytes to verify file is readable
                            files_saved_successfully[file_name] = True
                            print(f"✓ Verified {file_name} was saved successfully")
                        except Exception as e:
                            error_msg = _safe_exception_to_string(e)
                            print(f"✗ Warning: Could not verify {file_name} after saving: {error_msg}")
                            files_saved_successfully[file_name] = False
                    else:
                        print(f"✗ Error: Failed to save {file_name}")
                        files_saved_successfully[file_name] = False
            
            # Check if all required files were saved successfully
            # Skip this check if running Excel automation only (no files needed)
            if not excel_automation:
                failed_files = [name for name, success in files_saved_successfully.items() if not success]
                if failed_files:
                    error_msg = f"Failed to save the following files: {', '.join(failed_files)}"
                    if "Customer_Extract_Report.csv" in failed_files:
                        error_msg += "\n\nThe Customer_Extract_Report.csv file had encoding issues that could not be resolved."
                        error_msg += "\nPlease check that the file is saved with proper encoding (UTF-8 recommended)."
                    return False, error_msg, {}
            
            # Create the script path (absolute path from current directory)
            # Scripts are now in the processing/ directory
            # UPDATED 2025-01-26: Changed from old path to processing/ directory
            script_path = Path("processing") / f"{script_name}.py"
            script_path = script_path.resolve()  # Convert to absolute path
            
            # DEBUG: Print to verify we're using the correct path
            print("=" * 80)
            print(f"DEBUG: Looking for script: {script_name}.py")
            print(f"DEBUG: Script path: {script_path}")
            print(f"DEBUG: Script exists: {script_path.exists()}")
            print(f"DEBUG: Current working directory: {os.getcwd()}")
            print("=" * 80)
            
            if not script_path.exists():
                return False, f"Script not found: {script_path}", {}
            
            # Copy the script to temp directory
            temp_script_path = temp_path / f"{script_name}.py"
            
            # Read and modify the script
            script_content = script_path.read_text()
            
            # Modify the script to use the correct file paths
            if script_name == "Fixed_Pricing_VBCS":
                # Replace the hardcoded paths with the actual file names
                script_content = script_content.replace(
                    "price_data_path = get_relative_path('../../../../../Monthly Refreshed Data_Common/Old_Price_Build.csv')",
                    "price_data_path = Path('Old_Price_Build.csv')"
                )
                script_content = script_content.replace(
                    "assumptions_path = get_relative_path('../../../../../Monthly Refreshed Data_Common/Effective_Date_Assumptions.csv')",
                    "assumptions_path = Path('Effective_Date_Assumptions.csv')"
                )
                # Replace the output directory to write to current directory
                script_content = script_content.replace(
                    "output_dir = get_relative_path('../../../Output')",
                    "output_dir = Path('.')"
                )
            elif script_name == "KS_Pricing_VBCS":
                # Replace hardcoded paths with current directory paths
                script_content = script_content.replace(
                    "costco_prices_path = get_relative_path('../../../../Costco_HTST_Pricing.csv')",
                    "costco_prices_path = Path('Costco_HTST_Pricing.csv')"
                )
                script_content = script_content.replace(
                    "price_build_report_path = get_relative_path('../../../../../Monthly Refreshed Data_Common/Old_Price_Build.csv')",
                    "price_build_report_path = Path('Old_Price_Build.csv')"
                )
                script_content = script_content.replace(
                    "costco_regions_lookup_path = get_relative_path('../../../../../Monthly Refreshed Data_Common/Costco_HTST_Region_Lookup.csv')",
                    "costco_regions_lookup_path = Path('Costco_HTST_Region_Lookup.csv')"
                )
                script_content = script_content.replace(
                    "assumptions_path = get_relative_path('../../../../../Monthly Refreshed Data_Common/Effective_Date_Assumptions.csv')",
                    "assumptions_path = Path('Effective_Date_Assumptions.csv')"
                )
                script_content = script_content.replace(
                    "output_folder = get_relative_path('../../../Output/')",
                    "output_folder = Path('.')"
                )
            elif script_name == "Variable_Pricing_VBCS":
                # Replace hardcoded paths with current directory paths
                script_content = script_content.replace(
                    "EXECUTION_FOLDER = get_relative_path('../../../../')",
                    "EXECUTION_FOLDER = Path('.')"
                )
                script_content = script_content.replace(
                    "PL_FOLDER = get_relative_path('../../../../../Monthly Refreshed Data_Common/')",
                    "PL_FOLDER = Path('.')"
                )
                script_content = script_content.replace(
                    "STABLE_FOLDER = get_relative_path('../../../../../Monthly Refreshed Data_Common/Stable/')",
                    "STABLE_FOLDER = Path('.')"
                )
                # For OUTPUT_FOLDER, use absolute path to output_dir so CSV files persist between phases
                output_dir_abs = output_dir.resolve()
                script_content = script_content.replace(
                    "OUTPUT_FOLDER = get_relative_path('../../../Output/')",
                    f"OUTPUT_FOLDER = Path(r'{output_dir_abs}')"
                )
                script_content = script_content.replace(
                    "CUSTOMER_REPORT_PATH = get_relative_path('../../../../../Monthly Refreshed Data_Common/Customer_Extract_Report.csv')",
                    "CUSTOMER_REPORT_PATH = Path('Customer_Extract_Report.csv')"
                )
                script_content = script_content.replace(
                    "MARKET_INDEX_FILE = get_relative_path('../../../../../Monthly Refreshed Data_Common/Stable/Milk_Market_Index.csv')",
                    "MARKET_INDEX_FILE = Path('Milk_Market_Index.csv')"
                )
                # Fix Excel template paths to use absolute paths from project root
                # Get the project root (Pricing_Execution_Agent directory)
                # processing_helpers.py is at: Pricing_Execution_Agent/utils/processing_helpers.py
                # So parent.parent gets us to Pricing_Execution_Agent
                project_root = Path(__file__).parent.parent.absolute()
                urm_excel_path = project_root / "data" / "Pricing Execution" / "Custom Sheets" / "Monthly PL_URM Cross Dock_Price Lists_WITH LINKS.xlsm"
                winco_excel_path = project_root / "data" / "Pricing Execution" / "Custom Sheets" / "Monthly_Winco DSD Stores_Price Lists_WITH LINKS.xlsm"
                script_content = script_content.replace(
                    "excel_template_path = get_relative_path('../data/Pricing Execution/Custom Sheets/Monthly PL_URM Cross Dock_Price Lists_WITH LINKS.xlsm')",
                    f"excel_template_path = Path(r'{urm_excel_path}')"
                )
                script_content = script_content.replace(
                    "excel_template_path = get_relative_path('../data/Pricing Execution/Custom Sheets/Monthly_Winco DSD Stores_Price Lists_WITH LINKS.xlsm')",
                    f"excel_template_path = Path(r'{winco_excel_path}')"
                )
            
            temp_script_path.write_text(script_content)
            
            # Set environment variables to handle encoding
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['PYTHONUTF8'] = '1'
            
            print(f"Running script: {temp_script_path}")
            print(f"Working directory: {temp_path}")
            print(f"Script exists: {temp_script_path.exists()}")
            
            # List files in temp directory before running
            print(f"Files in temp directory before running script:")
            for file in temp_path.glob('*'):
                print(f"  - {file.name} ({file.stat().st_size} bytes)")
            
            # Use standard subprocess.run for all scripts
            # Build command with optional excel-automation flag for Variable_Pricing_VBCS
            cmd = [sys.executable, str(temp_script_path)]
            if excel_automation and script_name == "Variable_Pricing_VBCS":
                cmd.append('--excel-automation')
                print(f"Running script with Excel automation flag")
            
            try:
                result = subprocess.run(
                    cmd,
                    cwd=temp_path,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    env=env,
                    timeout=300  # 5 minute timeout
                )
                print(f"Subprocess completed successfully")
            except Exception as e:
                # Safely handle exception message encoding
                error_msg = _safe_exception_to_string(e)
                print(f"Subprocess failed with exception: {error_msg}")
                return False, f"Subprocess failed: {error_msg}", {}
            
            print(f"Script return code: {result.returncode}")
            print(f"Script stdout: {result.stdout}")
            print(f"Script stderr: {result.stderr}")
            
            # Debug information
            debug_info = f"""
Script: {script_name}
Working directory: {temp_path}
Script path: {temp_script_path}
Return code: {result.returncode}
Stdout: {result.stdout}
Stderr: {result.stderr}
Files in temp dir: {list(temp_path.glob('*'))}
CSV files in temp dir: {list(temp_path.glob('*.csv'))}
Output dir exists: {output_dir.exists()}
Output dir contents: {list(output_dir.glob('*')) if output_dir.exists() else 'N/A'}
"""
            
            # Print debug info to console for troubleshooting
            print("=" * 50)
            print("DEBUG INFO:")
            print(debug_info)
            print("=" * 50)
            
            if result.returncode == 0:
                # Check for output files in both temp directory and output_dir
                # For Variable_Pricing_VBCS with phased execution, files are saved to output_dir
                temp_output_files = list(temp_path.glob("*.csv"))
                output_dir_files = list(output_dir.glob("*.csv")) if output_dir.exists() else []
                
                print(f"Found {len(temp_output_files)} CSV files in temp directory")
                print(f"Found {len(output_dir_files)} CSV files in output directory")
                
                # Filter out input files - only copy output files (VBCS files)
                input_file_names = set(uploaded_files.keys())
                output_file_names = []
                if script_name == "Fixed_Pricing_VBCS":
                    output_file_names = ["fixed_vbcs.csv"]
                elif script_name == "KS_Pricing_VBCS":
                    output_file_names = ["ks_htst_vbcs.csv"]
                elif script_name == "Variable_Pricing_VBCS":
                    output_file_names = ["urm_vbcs.csv", "winco_vbcs.csv", "batch_vbcs.csv"]
                elif script_name == "Combine_VBCS":
                    output_file_names = ["combined_all_vbcs.csv"]
                
                # Collect output files from both locations (prefer output_dir for Variable_Pricing_VBCS)
                output_files_to_load = []
                for file_name in output_file_names:
                    if file_name in input_file_names:
                        continue  # Skip input files
                    # Check output_dir first (for phased execution)
                    output_dir_file = output_dir / file_name
                    if output_dir_file.exists():
                        output_files_to_load.append(output_dir_file)
                    # Otherwise check temp directory
                    else:
                        temp_file = temp_path / file_name
                        if temp_file.exists():
                            output_files_to_load.append(temp_file)
                
                # Load output files into memory (DataFrames) instead of copying to persistent disk
                # These will be stored in session state and cleaned up after 5 minutes
                output_dataframes = {}
                if output_files_to_load:
                    import pandas as pd
                    for file_path in output_files_to_load:
                        try:
                            df = pd.read_csv(file_path)
                            output_dataframes[file_path.name] = df
                            print(f"Loaded output file {file_path.name} into memory ({len(df)} rows) from {file_path.parent}")
                        except Exception as e:
                            error_msg = _safe_exception_to_string(e)
                            print(f"Error loading {file_path.name}: {error_msg}")
                else:
                    print("No output CSV files found in temp directory or output directory")
                
                # Note: Input files remain in temp directory and will be auto-deleted when context exits
                # The temp directory is managed by tempfile.TemporaryDirectory() which ensures cleanup
                print(f"Input files in temp directory will be automatically deleted when processing completes")
                
                # Additional cleanup: Remove any input files that might have been copied to output_dir
                # (This shouldn't happen, but just in case)
                input_file_names = set(uploaded_files.keys())
                for file_name in input_file_names:
                    input_file_path = output_dir / file_name
                    if input_file_path.exists():
                        try:
                            input_file_path.unlink()
                            print(f"Removed input file from output directory: {file_name}")
                        except Exception as e:
                            error_msg = _safe_exception_to_string(e)
                            print(f"Warning: Could not remove input file {file_name}: {error_msg}")
                
                # Check if we successfully loaded output files into memory
                if output_dataframes:
                    found_outputs = list(output_dataframes.keys())
                    # Check if there are Excel automation errors in stdout/stderr
                    # Include them in the message so UI can display them
                    message = f"Script completed successfully. Generated: {', '.join(found_outputs)}"
                    
                    # Check for Excel automation errors in stdout/stderr
                    combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
                    if "ERROR: Failed to process" in combined_output or "Excel automation error" in combined_output:
                        # Extract error lines for better visibility
                        error_lines = [line for line in combined_output.split('\n') 
                                     if 'ERROR' in line.upper() or 'Excel' in line or 'macro' in line.lower() or 'Failed' in line]
                        if error_lines:
                            message += "\n\n⚠️ Excel Automation Warnings:\n" + "\n".join(error_lines[:10])  # Limit to first 10 error lines
                    
                    # Return success with output dataframes for session state storage
                    return True, message, output_dataframes
                else:
                    # No output files found
                    message = "Script completed successfully but no output files were generated."
                    
                    # Check for Excel automation errors
                    combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
                    if "ERROR: Failed to process" in combined_output or "Excel automation error" in combined_output:
                        error_lines = [line for line in combined_output.split('\n') 
                                     if 'ERROR' in line.upper() or 'Excel' in line or 'macro' in line.lower() or 'Failed' in line]
                        if error_lines:
                            message += "\n\n⚠️ Excel Automation Warnings:\n" + "\n".join(error_lines[:10])
                    
                    return True, message, {}
            else:
                # Enhanced error reporting - ensure ASCII-safe encoding
                error_details = f"Script failed with return code {result.returncode}"
                
                # Check for encoding-related errors in output
                has_encoding_error = False
                if result.stderr:
                    stderr_lower = result.stderr.lower()
                    if "encoding" in stderr_lower or "unicode" in stderr_lower or "decode" in stderr_lower:
                        has_encoding_error = True
                if result.stdout:
                    stdout_lower = result.stdout.lower()
                    if "encoding" in stdout_lower or "unicode" in stdout_lower or "decode" in stdout_lower:
                        has_encoding_error = True
                
                if has_encoding_error:
                    error_details += "\n\n⚠️ This error usually indicates an encoding issue with one of your CSV files."
                    error_details += "\nPlease check that all your CSV files are saved with proper encoding (UTF-8 recommended)."
                    error_details += "\nThe Customer_Extract_Report.csv file was detected as having special characters."
                
                if result.stderr:
                    try:
                        stderr_safe = result.stderr.encode('ascii', errors='replace').decode('ascii')
                    except Exception:
                        stderr_safe = "Error output contains non-ASCII characters"
                    error_details += f"\n\nError Output:\n{stderr_safe}"
                if result.stdout:
                    try:
                        stdout_safe = result.stdout.encode('ascii', errors='replace').decode('ascii')
                    except Exception:
                        stdout_safe = "Standard output contains non-ASCII characters"
                    error_details += f"\n\nStandard Output:\n{stdout_safe}"
                return False, error_details, {}
                
    except subprocess.TimeoutExpired:
        return False, "Script timed out after 5 minutes", {}
    except Exception as e:
        # Safely convert exception to string using our helper function
        error_msg = _safe_exception_to_string(e)
        
        # Provide more context for common errors
        error_details = f"Error running script: {error_msg}"
        
        # Check if it's an encoding-related error
        if "encoding" in error_msg.lower() or "unicode" in error_msg.lower() or "decode" in error_msg.lower():
            error_details += "\n\nThis error usually indicates an encoding issue with one of your CSV files."
            error_details += "\nPlease check that all your CSV files are saved with proper encoding (UTF-8 recommended)."
            error_details += "\nThe Customer_Extract_Report.csv file was detected as having special characters."
            error_details += "\nEnhanced handling should have processed this file - check the logs above for details."
        
        # Check if it's a file access error (error code 13 typically means permission denied)
        if "13" in error_msg or "permission" in error_msg.lower() or "access" in error_msg.lower():
            error_details += "\n\nThis error may indicate a file access or permission issue."
            error_details += "\nPlease check that:"
            error_details += "\n  1. All required files are uploaded correctly"
            error_details += "\n  2. The files are not open in another application"
            error_details += "\n  3. You have proper permissions to read/write files"
        
        return False, error_details, {}

