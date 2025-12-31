"""
Pricing Data Processor

This module processes uploaded CSV files containing pricing data and creates
a parquet file for use in the Streamlit pricing application.

Business Rules:
- Custom-label volume must be <= Sell-to Volume (e.g., if Sell-to is B, Custom-label can be B, C, D, E)
- Volume tiers are ordered alphabetically: A (highest volume) < B < C < D < E (lowest volume)
"""
import pandas as pd
from pathlib import Path
import tempfile
import gc

def parse_dollar(val):
    """
    Parse dollar values from various formats (string with $, commas, or numeric).
    
    Args:
        val: Value to parse (can be string, float, or NaN)
        
    Returns:
        float: Parsed dollar value, or 0.0 if parsing fails
    """
    if pd.isnull(val):
        return 0.0
    if isinstance(val, str):
        # Remove $ and commas, convert to float
        clean_val = val.replace('$', '').replace(',', '').strip()
        try:
            return float(clean_val)
        except (ValueError, TypeError):
            return 0.0
    return float(val)


def normalize_column_names(df, column_mappings):
    """
    Normalize column names in a dataframe based on provided mappings.
    
    Args:
        df: DataFrame to normalize
        column_mappings: Dictionary of old_name: new_name mappings
        
    Returns:
        DataFrame with normalized column names
    """
    for old_name, new_name in column_mappings.items():
        if old_name in df.columns:
            df = df.rename(columns={old_name: new_name})
    return df


def compare_volume_tiers(sell_to_tier, custom_label_tier):
    """
    Compare volume tiers to enforce business rule: Custom-label volume <= Sell-to Volume.
    
    Business Rule: If Sell-to Volume is tier B, Custom-label volume should be B or below B
    (i.e., B, C, D, E). Volume tiers are ordered alphabetically where A < B < C < D < E.
    "Below B" means lower volume tiers (C, D, E), which come after B alphabetically.
    
    Args:
        sell_to_tier: Sell-to Volume Bracket value
        custom_label_tier: Custom Label Bracket value
        
    Returns:
        bool: True if custom_label_tier is valid (<= sell_to_tier), False otherwise
    """
    if pd.isna(sell_to_tier) or pd.isna(custom_label_tier):
        return True  # Keep rows with missing data for now
    
    sell_to_str = str(sell_to_tier).strip().upper()
    custom_label_str = str(custom_label_tier).strip().upper()
    
    # Try numeric comparison first (if tiers are numeric like 1, 2, 3, 4)
    try:
        sell_to_num = float(sell_to_str)
        custom_label_num = float(custom_label_str)
        # For numeric: if 1=highest, then custom_label >= sell_to means lower tier
        # But typically higher number = lower tier, so we want custom_label_num >= sell_to_num
        return custom_label_num >= sell_to_num
    except (ValueError, TypeError):
        # Alphabetical comparison: A < B < C < D < E
        # "B or below B" means B, C, D, E, which are >= B alphabetically
        # Therefore: custom_label >= sell_to (alphabetically)
        return custom_label_str >= sell_to_str


def save_parquet_with_fallback(df, parquet_path):
    """
    Save DataFrame to parquet with compression fallback logic.
    Tries brotli first, then gzip, then snappy, then fastparquet.
    
    Args:
        df: DataFrame to save
        parquet_path: Path where parquet file should be saved
        
    Returns:
        bool: True if successful, False otherwise
    """
    compression_options = [
        ('brotli', 'pyarrow'),
        ('gzip', 'pyarrow'),
        ('snappy', 'pyarrow'),
        (None, 'fastparquet')
    ]
    
    for compression, engine in compression_options:
        try:
            df.to_parquet(parquet_path, index=False, engine=engine, compression=compression)
            print(f"Saved with {compression or 'no'} compression using {engine}")
            return True
        except Exception as e:
            if compression_options.index((compression, engine)) < len(compression_options) - 1:
                print(f"{compression or 'default'} compression failed, trying next option: {e}")
            else:
                print(f"All compression options failed: {e}")
                return False
    return False


def process_uploaded_files(uploaded_files):
    """
    Process uploaded CSV files and create parquet file for Streamlit app.
    
    This function:
    1. Loads and validates required CSV files
    2. Performs joins to combine pricing data
    3. Applies business rules (volume tier constraints)
    4. Calculates pricing metrics (FOB, delivered prices, etc.)
    5. Optimizes data types and saves as parquet
    
    Args:
        uploaded_files: List of uploaded file objects
        
    Returns:
        bool: True if processing succeeded, False otherwise
    """
    print("[START] Processing uploaded CSV files - Current version")
    print(f"[FILE] Processing {len(uploaded_files)} uploaded files")
    
    # Debug: Print file names
    for i, file in enumerate(uploaded_files, 1):
        print(f"  {i}. {file.name} ({len(file.getvalue())} bytes)")
    
    try:
        # Create temporary directory for processing
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir = Path(temp_dir)
            
            # Save uploaded files to temporary directory
            file_mapping = {}
            required_files = [
                'Product_Class_Plant.csv',
                'Plant_Class_Plant Fees.csv', 
                'Product_Milk Base Cost.csv',
                'Product_Processing_Pkg_Ing.csv',
                'Sell-to_Volume Bracket_Fee.csv',
                'Custom Label_Volume Bracket_Fee.csv',
                'Pallet_Fee.csv',
                'Delivery_Miles Tier_Drop Size Tier_Fee.csv',
                'Product_UOM.csv'
            ]
            
            print("Loading uploaded CSV files...")
            
            # Save files and create mapping
            for uploaded_file in uploaded_files:
                file_name = uploaded_file.name
                if file_name in required_files:
                    file_path = temp_dir / file_name
                    with open(file_path, "wb") as f:
                        if hasattr(uploaded_file, 'getbuffer'):
                            f.write(uploaded_file.getbuffer())
                        elif hasattr(uploaded_file, 'read'):
                            f.write(uploaded_file.read())
                        else:
                            raise ValueError(f"Unsupported file type: {type(uploaded_file)}")
                    file_mapping[file_name] = file_path
            
            # Check if all required files are present
            missing_files = [f for f in required_files if f not in file_mapping]
            if missing_files:
                print(f"[ERROR] Missing required files: {missing_files}")
                return False
            
            # Load all CSV files
            product_class_plant = pd.read_csv(file_mapping['Product_Class_Plant.csv'])
            print(f"Loaded Product_Class_Plant: {len(product_class_plant):,} rows")
            
            plant_fees = pd.read_csv(file_mapping['Plant_Class_Plant Fees.csv'])
            print(f"Loaded Plant_Class_Plant Fees: {len(plant_fees):,} rows")
            
            milk_base_cost = pd.read_csv(file_mapping['Product_Milk Base Cost.csv'])
            print(f"Loaded Product_Milk Base Cost: {len(milk_base_cost):,} rows")
            
            processing_costs = pd.read_csv(file_mapping['Product_Processing_Pkg_Ing.csv'])
            print(f"Loaded Product_Processing_Pkg_Ing: {len(processing_costs):,} rows")
            
            sell_to_volume_fees = pd.read_csv(file_mapping['Sell-to_Volume Bracket_Fee.csv'])
            print(f"Loaded Sell-to_Volume Bracket_Fee: {len(sell_to_volume_fees):,} rows")
            
            custom_label_fees = pd.read_csv(file_mapping['Custom Label_Volume Bracket_Fee.csv'])
            print(f"Loaded Custom Label_Volume Bracket_Fee: {len(custom_label_fees):,} rows")
            
            pallet_fees = pd.read_csv(file_mapping['Pallet_Fee.csv'])
            print(f"Loaded Pallet_Fee: {len(pallet_fees):,} rows")
            
            delivery_fees = pd.read_csv(file_mapping['Delivery_Miles Tier_Drop Size Tier_Fee.csv'])
            print(f"Loaded Delivery_Miles Tier_Drop Size Tier_Fee: {len(delivery_fees):,} rows")
            
            product_uom = pd.read_csv(file_mapping['Product_UOM.csv'])
            print(f"Loaded Product_UOM: {len(product_uom):,} rows")
            
            print("\nPerforming joins...")
            
            # Step 1: Join Product_Class_Plant with Plant_Class_Plant_Fees
            # Use the exact column name from the CSV file
            base_joins = product_class_plant.merge(
                plant_fees,
                left_on=['Plant', 'Market Index Name'],
                right_on=['Plant', 'Market Index Name'],
                how='left'
            )
            
            # Step 2: Add milk base cost and month
            # Use the exact column name from the CSV file
            with_milk_cost = base_joins.merge(
                milk_base_cost[['Item', ' Base Milk Cost per Gallon ', 'Month']],
                left_on='Item',
                right_on='Item',
                how='left'
            )
            
            # Step 3: Add processing costs
            with_processing_costs = with_milk_cost.merge(
                processing_costs[['Item', 'Total Processing ($/Gal)', 'Packaging ($/Gal)', 'Ingredients ($/Gal)']],
                left_on='Item',
                right_on='Item',
                how='left'
            )
            
            # Step 4: Add all fee tables using cartesian joins
            with_processing_costs['key'] = 1
            sell_to_volume_fees['key'] = 1
            custom_label_fees['key'] = 1
            pallet_fees['key'] = 1
            delivery_fees['key'] = 1
            
            # Normalize column names to handle variations in CSV files
            custom_label_fees = normalize_column_names(
                custom_label_fees,
                {'Custom Label Bracket (Gal/Yr)': 'Custom Label Bracket'}
            )
            delivery_fees = normalize_column_names(
                delivery_fees,
                {
                    'Drop Fee Tier (lbs/Drop Size)': 'Drop Fee Tier (lbs/Drop)',
                    ' Delivery Charge ($/Gal) ': 'Delivery Charge ($/Gal)'
                }
            )
            
            # Perform cartesian joins
            temp1 = with_processing_costs.merge(sell_to_volume_fees, on='key', how='left')
            temp2 = temp1.merge(custom_label_fees, on='key', how='left')
            temp3 = temp2.merge(pallet_fees, on='key', how='left')
            final_result = temp3.merge(delivery_fees, on='key', how='left')
            final_result = final_result.drop('key', axis=1)
            
            # Apply business rule: Filter out invalid combinations where Custom-label volume > Sell-to Volume
            print("Applying volume tier constraint: Custom-label volume must be <= Sell-to Volume...")
            initial_count = len(final_result)
            final_result = final_result[
                final_result.apply(
                    lambda row: compare_volume_tiers(
                        row.get('Sell-to Volume Bracket'),
                        row.get('Custom Label Bracket')
                    ),
                    axis=1
                )
            ]
            filtered_count = len(final_result)
            print(f"[OK] Filtered out {initial_count - filtered_count:,} invalid combinations "
                  f"({filtered_count:,} valid combinations remaining)")
            
            # Step 5: Add UOM information
            final_result = final_result.merge(
                product_uom[['Item', 'Gallons per Each', 'Gallons per Case']],
                left_on='Item',
                right_on='Item',
                how='left'
            )
            
            # Convert numerical values without dropping any columns
            # Handle the exact column names from CSV files (with leading/trailing spaces)
            base_milk_cost_col = None
            class_i_fee_col = None
            
            print(f"[DEBUG] Available columns: {list(final_result.columns)}")
            
            for col in final_result.columns:
                if 'Base Milk Cost per Gallon' in col:
                    base_milk_cost_col = col
                    print(f"[DEBUG] Found base milk cost column: '{col}'")
                    break
            
            for col in final_result.columns:
                if 'Class I Location & Plant Fees' in col:
                    class_i_fee_col = col
                    print(f"[DEBUG] Found class I fee column: '{col}'")
                    break
            
            # Convert numerical values in place without dropping columns
            if base_milk_cost_col:
                # Convert the existing column to numerical values
                final_result[base_milk_cost_col] = final_result[base_milk_cost_col].apply(parse_dollar)
                # Also create the clean column name for calculations
                final_result['Base Milk Cost per Gallon'] = final_result[base_milk_cost_col]
                print(f"[OK] Processed base milk cost column: {base_milk_cost_col}")
            else:
                final_result['Base Milk Cost per Gallon'] = 0.0
                print(f"[WARNING] Base milk cost column not found, setting to 0.0")
            
            if class_i_fee_col:
                # Convert the existing column to numerical values
                final_result[class_i_fee_col] = final_result[class_i_fee_col].apply(parse_dollar)
                # Also create the clean column name for calculations
                final_result['Class I Location & Plant Fees ($/Gal)'] = final_result[class_i_fee_col]
                print(f"[OK] Processed class I fee column: {class_i_fee_col}")
                print(f"[DEBUG] Columns after processing: {[col for col in final_result.columns if 'Class I' in col]}")
            else:
                final_result['Class I Location & Plant Fees ($/Gal)'] = 0.0
                print(f"[WARNING] Class I fee column not found, setting to 0.0")
            
            # Convert other cost components to numerical format
            for col in ['Total Processing ($/Gal)', 'Packaging ($/Gal)', 'Ingredients ($/Gal)', 'Sell-to Volume Fee ($/Gal)', 'Custom Label Fee ($/Gal)', 'Mixed Pallet Fee ($/Gal)']:
                if col in final_result.columns:
                    final_result[col] = final_result[col].apply(parse_dollar)
                else:
                    final_result[col] = 0.0
            
            # Calculate shrink (1% of total class 1 differential and base milk cost) - after all columns are processed
            print(f"[DEBUG] Calculating shrink with columns: Base Milk Cost per Gallon and Class I Location & Plant Fees ($/Gal)")
            final_result['Shrink ($/gal)'] = (final_result['Base Milk Cost per Gallon'] + final_result['Class I Location & Plant Fees ($/Gal)']) * 0.02
            print(f"[OK] Calculated shrink for {len(final_result)} records")
            
            # Calculate FOB price using the numerical values
            fob_cost_cols = [
                'Base Milk Cost per Gallon',
                'Class I Location & Plant Fees ($/Gal)',
                'Shrink ($/gal)',
                'Total Processing ($/Gal)',
                'Packaging ($/Gal)',
                'Ingredients ($/Gal)',
                'Sell-to Volume Fee ($/Gal)',
                'Custom Label Fee ($/Gal)',
                'Mixed Pallet Fee ($/Gal)'
            ]
            
            final_result['FOB price w.o. trade ($/gal)'] = final_result[fob_cost_cols].sum(axis=1)
            
            # Calculate delivery charge
            if 'Delivery Charge ($/Gal)' in final_result.columns:
                final_result['Delivery Charge ($/Gal)'] = final_result['Delivery Charge ($/Gal)'].apply(parse_dollar)
            else:
                final_result['Delivery Charge ($/Gal)'] = 0.0
            
            # Calculate delivered price (FOB + delivery)
            final_result['Delivered price w.o. trade ($/gal)'] = final_result['FOB price w.o. trade ($/gal)'] + final_result['Delivery Charge ($/Gal)']
            
            # Remove duplicates
            key_columns = ['Item', 'Plant', 'Sell-to Volume Bracket', 'Custom Label Bracket', 'Pallet', 'Mileage Fee Tier (Mi)', 'Drop Fee Tier (lbs/Drop)', 'Market Index Name']
            final_result = final_result.drop_duplicates(subset=key_columns, keep='first')
            
            # Keep all columns as they are - don't drop or rename any columns
            print(f"Final result has {len(final_result.columns)} columns: {list(final_result.columns)}")
            
            # Debug: Check if the key columns have data
            if 'Class I Location & Plant Fees ($/Gal)' in final_result.columns:
                non_null_count = final_result['Class I Location & Plant Fees ($/Gal)'].notna().sum()
                print(f"Class I Location & Plant Fees - Non-null values: {non_null_count:,} out of {len(final_result):,}")
            else:
                print("Class I Location & Plant Fees ($/Gal) column not found in final result")
                
            if 'Base Milk Cost per Gallon' in final_result.columns:
                non_null_count = final_result['Base Milk Cost per Gallon'].notna().sum()
                print(f"Base Milk Cost per Gallon - Non-null values: {non_null_count:,} out of {len(final_result):,}")
            else:
                print("Base Milk Cost per Gallon column not found in final result")
            
            # Sort the data
            sort_cols = [col for col in ['Item', 'Sell-to Volume Bracket', 'Custom Label Bracket', 'Pallet', 'Mileage Fee Tier (Mi)', 'Drop Fee Tier (lbs/Drop)'] if col in final_result.columns]
            if sort_cols:
                final_result = final_result.sort_values(by=sort_cols)
            
            # Optimize data types for smaller file size
            print("Optimizing data types for smaller file size...")
            
            # Convert text columns to categorical for better compression
            # Define numeric columns that should NOT be converted to categorical
            numeric_column_names = [
                'Class I Location & Plant Fees ($/Gal)', 'Base Milk Cost per Gallon',
                'Shrink ($/gal)', 'Total Processing ($/Gal)', 'Packaging ($/Gal)',
                'Ingredients ($/Gal)', 'Mixed Pallet Fee ($/Gal)', 'Sell-to Volume Fee ($/Gal)',
                'Custom Label Fee ($/Gal)', 'FOB price w.o. trade ($/gal)',
                'Delivery Charge ($/Gal)', 'Delivered price w.o. trade ($/gal)',
                'Gallons per Each', 'Gallons per Case'
            ]
            
            # Find categorical columns dynamically (all columns except numeric ones)
            categorical_columns = [col for col in final_result.columns if col not in numeric_column_names]
            
            print(f"[DEBUG] Converting {len(categorical_columns)} categorical columns: {categorical_columns}")
            
            for col in categorical_columns:
                if col in final_result.columns:
                    # Ensure Item is converted to string first for search functionality
                    if 'Item' in col and 'Description' not in col:
                        final_result[col] = final_result[col].astype(str).astype('category')
                    else:
                        final_result[col] = final_result[col].astype('category')
            
            # Convert numeric columns to appropriate types
            # Use the same list as above, plus any columns with price/cost indicators
            numeric_columns = numeric_column_names.copy()
            for col in final_result.columns:
                if col not in numeric_columns and ('$/Gal' in col or 'per Gallon' in col or 'per Each' in col or 'per Case' in col):
                    numeric_columns.append(col)
            
            print(f"[DEBUG] Converting {len(numeric_columns)} numeric columns: {numeric_columns}")
            
            for col in numeric_columns:
                if col in final_result.columns:
                    # Convert to float32 to save space
                    final_result[col] = pd.to_numeric(final_result[col], errors='coerce').astype('float32')
            
            # Save as parquet file in system temp directory (consistent with Streamlit app)
            system_temp_dir = Path(tempfile.gettempdir())
            parquet_path = system_temp_dir / "pricing_data.parquet"
            
            print(f"[DEBUG] Using system temp directory for parquet: {parquet_path}")
            print(f"[DEBUG] System temp directory exists: {system_temp_dir.exists()}")
            
            # Ensure system temp directory is writable
            try:
                system_temp_dir.mkdir(exist_ok=True)
                print(f"[DEBUG] System temp directory is writable: True")
            except Exception as e:
                print(f"[ERROR] Cannot write to system temp directory: {e}")
                return False
            
            # Reorder columns to the correct sequence
            correct_column_order = [
                'Month', 'Item', 'Item Description', 'Item Category', 'Market Index Name', 'Plant',
                'Sell-to Volume Bracket', 'Custom Label Bracket', 'Pallet', 'Mileage Fee Tier (Mi)', 'Drop Fee Tier (lbs/Drop)',
                'Class I Location & Plant Fees ($/Gal)', 'Base Milk Cost per Gallon', 'Shrink ($/gal)',
                'Packaging ($/Gal)', 'Ingredients ($/Gal)', 'Total Processing ($/Gal)', 'Sell-to Volume Fee ($/Gal)', 
                'Custom Label Fee ($/Gal)', 'Mixed Pallet Fee ($/Gal)', 'FOB price w.o. trade ($/gal)',
                'Delivery Charge ($/Gal)', 'Delivered price w.o. trade ($/gal)', 'Gallons per Each', 'Gallons per Case'
            ]
            
            # Reorder columns to match the specified sequence
            final_result = final_result[correct_column_order]
            
            print(f"Saving parquet file to: {parquet_path}")
            print(f"Final result shape: {final_result.shape}")
            print(f"Final result columns: {list(final_result.columns)}")
            
            # Remove existing parquet file if it exists to ensure clean replacement
            if parquet_path.exists():
                parquet_path.unlink()
                print(f"Removed existing parquet file to ensure clean replacement")
            
            # Save with maximum compression (with fallback logic)
            if not save_parquet_with_fallback(final_result, parquet_path):
                print("[ERROR] Failed to save parquet file with any compression method")
                return False
            
            print(f"[OK] Successfully processed {len(final_result):,} records")
            print(f"[OK] Parquet file saved: {parquet_path}")
            print(f"[OK] File size: {parquet_path.stat().st_size / (1024*1024):.1f} MB")
            print(f"[OK] File exists after save: {parquet_path.exists()}")
            
            # Debug: Show sample of final data
            print("[DEBUG] Sample of final processed data:")
            print(final_result.head(3).to_string())
            
            # Clean up memory
            try:
                del final_result
                if 'base_joins' in locals():
                    del base_joins
                if 'with_milk_cost' in locals():
                    del with_milk_cost
                if 'with_processing_costs' in locals():
                    del with_processing_costs
            except:
                pass
            gc.collect()
            
            return True
            
    except Exception as e:
        print(f"[ERROR] Error processing uploaded files: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

# Main execution block for when script is called directly
if __name__ == "__main__":
    print("[START] Starting pricing processor script...")
    
    # Look for CSV files in the temp directory
    import tempfile
    temp_dir = Path(tempfile.gettempdir())
    
    # Find all CSV files in temp directory
    csv_files = list(temp_dir.glob("*.csv"))
    
    if not csv_files:
        print("[ERROR] No CSV files found in temp directory")
        exit(1)
    
    print(f"[FILE] Found {len(csv_files)} CSV files in temp directory")
    for csv_file in csv_files:
        print(f"  - {csv_file.name}")
    
    # Create mock uploaded files from the CSV files
    class MockUploadedFile:
        def __init__(self, file_path):
            self.name = file_path.name
            self._file_path = file_path
            # Cache the file content
            with open(self._file_path, 'rb') as f:
                self._content = f.read()
        
        def getbuffer(self):
            return self._content
        
        def read(self):
            return self._content
        
        def getvalue(self):
            return self._content
    
    # Convert to mock uploaded files
    uploaded_files = [MockUploadedFile(csv_file) for csv_file in csv_files]
    
    # Process the files
    success = process_uploaded_files(uploaded_files)
    
    if success:
        print("[OK] Processing completed successfully!")
        exit(0)
    else:
        print("[ERROR] Processing failed!")
        exit(1)
