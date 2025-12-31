import pandas as pd
from datetime import datetime
import os
from pathlib import Path

def get_relative_path(relative_path):
    """
    Get absolute path from relative path, works from script directory
    """
    script_dir = Path(__file__).parent.absolute()
    return script_dir / relative_path

def load_date_assumptions(assumptions_path):
    """
    Load and parse the effective_date_assumption.csv file to derive dates
    """
    try:
        assumptions_df = pd.read_csv(assumptions_path)
        
        # Find adj_start_date from "Current Month" rule
        current_month_row = assumptions_df[assumptions_df['Rules'] == 'Current Month']
        if current_month_row.empty:
            raise ValueError("No 'Current Month' rule found in assumptions file")
        adj_start_date = pd.to_datetime(current_month_row['Value'].iloc[0])
        
        # Calculate adj_end_date as last day of the month
        if adj_start_date.month == 12:
            next_month = adj_start_date.replace(year=adj_start_date.year + 1, month=1, day=1)
        else:
            next_month = adj_start_date.replace(month=adj_start_date.month + 1, day=1)
        adj_end_date = next_month - pd.Timedelta(days=1)
        
        # Add proper timestamps
        adj_start_date = adj_start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        adj_end_date = adj_end_date.replace(hour=23, minute=59, second=0, microsecond=0)
        
        return adj_start_date, adj_end_date
        
    except Exception as e:
        print(f"Error loading date assumptions: {e}")
        raise

def process_costco_pricing_data():
    """
    Process Costco Fresh pricing data for KS items
    
    Returns:
    final_result: DataFrame with processed Costco pricing data
    """
    
    # Load required data files using robust paths
    print("Loading required data files...")
    
    # Load Costco pricing data from HTST & ESL PL folder
    costco_prices_path = get_relative_path('../../../../Costco_HTST_Pricing.csv')
    if not costco_prices_path.exists():
        raise FileNotFoundError(f"Costco pricing file not found at: {costco_prices_path}")
    costco_prices = pd.read_csv(costco_prices_path)
    print(f"Loaded Costco pricing data: {len(costco_prices)} rows")
    
    # Load Price Build Report
    price_build_report_path = get_relative_path('../../../../../Monthly Refreshed Data_Common/Old_Price_Build.csv')
    if not price_build_report_path.exists():
        raise FileNotFoundError(f"Price Build Report not found at: {price_build_report_path}")
    Price_Build_Report = pd.read_csv(price_build_report_path)
    print(f"Loaded Price Build Report: {len(Price_Build_Report)} rows")
    
    # Load Costco regions lookup
    costco_regions_lookup_path = get_relative_path('../../../../../Monthly Refreshed Data_Common/Costco_HTST_Region_Lookup.csv')
    if not costco_regions_lookup_path.exists():
        raise FileNotFoundError(f"Costco regions lookup not found at: {costco_regions_lookup_path}")
    costco_regions_lookup = pd.read_csv(costco_regions_lookup_path)
    print(f"Loaded Costco regions lookup: {len(costco_regions_lookup)} rows")
    
    # Load and parse date assumptions
    assumptions_path = get_relative_path('../../../../../Monthly Refreshed Data_Common/Effective_Date_Assumptions.csv')
    if not assumptions_path.exists():
        raise FileNotFoundError(f"Assumptions file not found at: {assumptions_path}")
    
    print("Loading date assumptions...")
    adj_start_date, adj_end_date = load_date_assumptions(assumptions_path)
    print(f"Using adjustment dates: {adj_start_date} to {adj_end_date}")
    
    # Step 1: Filter Price_Build_Report for KS items and CLASS markets
    pb_filtered = Price_Build_Report[
        (Price_Build_Report['Item Description'].str.contains('KS', na=False)) &
        (Price_Build_Report['Market Index Name'].str.contains('CLASS', na=False))
    ].copy()
    
    print(f"Filtered Price Build Report for KS items: {len(pb_filtered)} rows")
    
    # Step 2: Join with costco_regions_lookup to get region information
    # Convert Party Site Number to string for joining
    pb_filtered['Party Site Number'] = pb_filtered['Party Site Number'].astype(str)
    costco_regions_lookup['Ship To Site Number'] = costco_regions_lookup['Ship To Site Number'].astype(str)
    
    # Left join with costco_regions_lookup
    pb_with_region = pb_filtered.merge(
        costco_regions_lookup[['Ship To Site Number', 'Region']],
        left_on='Party Site Number',
        right_on='Ship To Site Number',
        how='left'
    )
    
    # Trim spaces from region names to match price mapping format
    pb_with_region['Region'] = pb_with_region['Region'].str.strip()
    
    print(f"Joined with regions lookup: {len(pb_with_region)} rows")
    
    # Step 3: Join with costco_prices to get the correct pricing for each item-region combination
    # Convert Item to string for joining
    pb_with_region['Item_str'] = pb_with_region['Item'].astype(str)
    costco_prices['Prod_#_str'] = costco_prices['Prod #'].astype(str)
    
    # Create a mapping of item to region prices
    price_mapping = {}
    for _, row in costco_prices.iterrows():
        item = str(row['Prod #'])
        # Define region columns with and without spaces to handle different formats
        region_columns = [
            (' PNW ', 'PNW'),
            (' PNW X-Dock ', 'PNW X-Dock'),
            (' WA/OR Total ', 'WA/OR Total'),
            (' Alaska ', 'Alaska'),
            (' Montana ', 'Montana'),
            (' SLC, UT  ', 'SLC, UT'),
            (' St. George, UT ', 'St. George, UT'),
            (' Denver, CO ', 'Denver, CO'),
            (' Gypsum, CO ', 'Gypsum, CO'),
            (' Boise, ID ', 'Boise, ID')
        ]
        
        for region_col, region_name in region_columns:
            # Try both the original column name and the trimmed version
            if region_col in costco_prices.columns:
                if pd.notna(row[region_col]):
                    price_mapping[(item, region_name)] = row[region_col]
            elif region_name in costco_prices.columns:
                if pd.notna(row[region_name]):
                    price_mapping[(item, region_name)] = row[region_name]
    
    print(f"Created price mapping for {len(price_mapping)} item-region combinations")
    
    # Step 4: Create the final VBCS structure with correct column order and format
    final_rows = []
    
    processed_count = 0
    skipped_no_region = 0
    skipped_no_price = 0
    
    for _, row in pb_with_region.iterrows():
        item = str(row['Item'])
        region = row['Region']
        
        # Skip if no region found
        if pd.isna(region):
            skipped_no_region += 1
            continue
            
        # Get the price for this item-region combination
        price = price_mapping.get((item, region), 0)
        
        # Skip if no price found
        if price == 0:
            skipped_no_price += 1
            continue
        
        # Create row for each UOM (EA and CA)
        for uom in ['EA', 'CA']:
            final_rows.append({
                'Pricelistname': 'CP_Market Price',
                'Pricinguom': uom,
                'Baselineprice': 0,
                'Chargestartdate': '43831',  # Excel date for 2020-01-01
                'Chargeenddate': '',
                'Item_Name': row['Item'],
                'Customername': row['Customer'],
                'Customernumber': '',
                'Shiptositename': row['Ship To Site Name'],
                'Customersitenumber': row['Party Site Number'],
                'Adjustmenttype': 'MARKUP_AMOUNT',
                'Adjustmentamount': round(price, 2),
                'Adjustmentbasis': '',
                'Precedence': '',
                'Market': row['Market Index Name'],
                'Age': '',
                'Spec': '',
                'Grade': '',
                'Adjustmentstartdate': adj_start_date.strftime('%m/%d/%Y'),
                'Adjustmentenddate': adj_end_date.strftime('%m/%d/%Y %H:%M'),
                'Status': 'N',
                '': ''  # Empty column as in reference file
            })
            processed_count += 1
    
    print(f"Processing summary:")
    print(f"  - Processed items: {processed_count}")
    print(f"  - Skipped (no region): {skipped_no_region}")
    print(f"  - Skipped (no price): {skipped_no_price}")
    
    # Create final DataFrame
    final_result = pd.DataFrame(final_rows)
    
    # Remove duplicates
    final_result = final_result.drop_duplicates()
    
    print(f"Final result after removing duplicates: {len(final_result)} rows")
    
    return final_result

def main():
    """Main function to run the KS Pricing Execution."""
    try:
        print("Starting KS Pricing Execution...")
        
        # Process the Costco pricing data
        result = process_costco_pricing_data()
        
        # Save the result to Output folder using robust path
        output_folder = get_relative_path('../../../Output/')
        output_folder.mkdir(parents=True, exist_ok=True)
        
        output_path = output_folder / "ks_htst_vbcs.csv"
        result.to_csv(output_path, index=False)
        
        print(f"Processing completed successfully!")
        print(f"Output saved to: {output_path}")
        print(f"Total records processed: {len(result)}")
        print("The ks_htst_vbcs.csv file has been created and is ready to be included")
        print("in the combined_all_vbcs.csv file.")
        
    except Exception as e:
        print(f"Error occurred: {e}")
        import traceback
        traceback.print_exc()
        return None

if __name__ == "__main__":
    main()
    