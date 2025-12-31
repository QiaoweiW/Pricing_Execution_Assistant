import pandas as pd
import sys
import os
from pathlib import Path

def load_vbcs_file(file_path):
    """Load a VBCS CSV file and return a DataFrame."""
    try:
        print(f"Loading {file_path}...")
        df = pd.read_csv(file_path)
        print(f"  ✓ Loaded {len(df)} rows from {os.path.basename(file_path)}")
        
        # Remove unnecessary columns if they exist
        columns_to_remove = ['Pricelistname', 'Pricinguom', 'Baselineprice', 'Item_Name']
        existing_columns_to_remove = [col for col in columns_to_remove if col in df.columns]
        
        if existing_columns_to_remove:
            df = df.drop(columns=existing_columns_to_remove)
            print(f"  ✓ Removed unnecessary columns: {existing_columns_to_remove}")
        
        # Clean up any NaN values in critical columns
        critical_columns = ['Pricelistname*', 'Itemname*', 'Customername']
        for col in critical_columns:
            if col in df.columns:
                # Replace NaN with empty string for string columns
                df[col] = df[col].fillna('')
                # Also replace any 'nan' strings that might exist
                df[col] = df[col].replace('nan', '')
        
        return df
    except FileNotFoundError:
        print(f"  ❌ File not found: {file_path}")
        return None
    except Exception as e:
        print(f"  ❌ Error loading {file_path}: {e}")
        return None

def get_relative_path(relative_path):
    """
    Get absolute path from relative path, works from script directory
    """
    script_dir = Path(__file__).parent.absolute()
    return script_dir / relative_path

def combine_vbcs_files():
    """
    Combine all VBCS files into a single combined file
    """
    print("Starting VBCS file combination process...")
    print(f"Script location: {Path(__file__).parent.absolute()}")
    
    # Define file paths using robust relative path handling
    output_folder = get_relative_path('../../../Output/')
    batch_file = output_folder / 'batch_vbcs.csv'
    fixed_file = output_folder / 'fixed_vbcs.csv'
    urm_file = output_folder / 'urm_vbcs.csv'
    winco_file = output_folder / 'winco_vbcs.csv'
    ks_file = output_folder / 'ks_htst_vbcs.csv'
    
    print(f"Looking for files in: {output_folder}")
    
    # List of files to combine with their descriptions
    files_to_combine = [
        ('Batch VBCS', batch_file),
        ('Fixed VBCS', fixed_file),
        ('URM VBCS', urm_file),
        ('Winco VBCS', winco_file),
        ('KS VBCS', ks_file)
    ]
    
    # Load and combine all files
    combined_data = []
    
    for description, file_path in files_to_combine:
        try:
            if file_path.exists():
                df = pd.read_csv(file_path)
                print(f"✓ Loaded {description}: {len(df)} rows from {file_path}")
                
                # Add source identifier
                df['Source_File'] = description
                
                combined_data.append(df)
            else:
                print(f"⚠️ Warning: {file_path} not found, skipping {description}")
        except Exception as e:
            print(f"❌ Error loading {description} from {file_path}: {e}")
    
    if not combined_data:
        print("❌ No VBCS files could be loaded. Exiting.")
        return None
    
    # Combine all DataFrames
    combined_df = pd.concat(combined_data, ignore_index=True)
    print(f"\n✓ Combined {len(combined_df)} total rows from {len(combined_data)} files")
    
    # Ensure consistent column names across all files
    print("\nEnsuring consistent column names...")
    
    # Define the expected column structure based on the reference file
    expected_columns = [
        'Pricelistname', 'Pricinguom', 'Baselineprice', 'Chargestartdate', 'Chargeenddate',
        'Item_Name', 'Customername', 'Customernumber', 'Shiptositename', 'Customersitenumber',
        'Adjustmenttype', 'Adjustmentamount', 'Adjustmentbasis', 'Precedence', 'Market',
        'Age', 'Spec', 'Grade', 'Adjustmentstartdate', 'Adjustmentenddate', 'Status',
        '', 'Region'
    ]
    
    # Check for missing columns and add them if necessary
    for col in expected_columns:
        if col not in combined_df.columns:
            if col == '':  # Empty column name
                combined_df[''] = ''
            else:
                combined_df[col] = ''
            print(f"  ✓ Added missing column: '{col}'")
    
    # Reorder columns to match expected structure
    combined_df = combined_df[expected_columns + ['Source_File']]
    
    # Validate critical columns exist
    critical_columns = ['Pricelistname', 'Item_Name', 'Customername']
    missing_critical = [col for col in critical_columns if col not in combined_df.columns]
    
    if missing_critical:
        print(f"❌ Critical columns missing: {missing_critical}")
        return None
    
    print("✓ All columns validated successfully")
    
    # Save combined file
    output_path = output_folder / "combined_all_vbcs.csv"
    combined_df.to_csv(output_path, index=False)
    
    print(f"\n✅ Successfully created combined VBCS file:")
    print(f"   Output: {output_path}")
    print(f"   Total rows: {len(combined_df)}")
    print(f"   Total columns: {len(combined_df.columns)}")
    
    return combined_df

def main():
    """Main function to run the VBCS combiner."""
    try:
        combine_vbcs_files()
    except Exception as e:
        print(f"❌ Unexpected error occurred: {e}")
        print("Please check that all required VBCS files exist in the Output folder")
        sys.exit(1)

if __name__ == "__main__":
    main()
