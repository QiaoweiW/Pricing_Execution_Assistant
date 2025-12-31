
import pandas as pd
from datetime import datetime
import numpy as np
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
        # Debug: Uncomment the lines below if you need to see the assumptions file structure
        # print(f"Assumptions file columns: {assumptions_df.columns.tolist()}")
        # print(f"Assumptions file contents:")
        # print(assumptions_df)
        
        # Find adj_start_date from "Current Month" rule (note the capitalization)
        current_month_row = assumptions_df[assumptions_df['Rules'] == 'Current Month']
        if current_month_row.empty:
            raise ValueError("No 'Current Month' rule found in assumptions file")
        adj_start_date = pd.to_datetime(current_month_row['Value'].iloc[0])
        
        # Calculate adj_end_date as last day of the month
        # Get the last day of the month from adj_start_date
        if adj_start_date.month == 12:
            next_month = adj_start_date.replace(year=adj_start_date.year + 1, month=1, day=1)
        else:
            next_month = adj_start_date.replace(month=adj_start_date.month + 1, day=1)
        adj_end_date = next_month - pd.Timedelta(days=1)
        
        # Find filtered_date from "Last Month" rule (note the capitalization)
        last_month_row = assumptions_df[assumptions_df['Rules'] == 'Last Month']
        if last_month_row.empty:
            raise ValueError("No 'Last Month' rule found in assumptions file")
        filtered_date = pd.to_datetime(last_month_row['Value'].iloc[0])
        
        # Add proper timestamps
        adj_start_date = adj_start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        adj_end_date = adj_end_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        return adj_start_date, adj_end_date, filtered_date
        
    except Exception as e:
        print(f"Error loading date assumptions: {e}")
        raise

def process_price_data(csv_path, table_name='price_build_report_202508', 
                      adj_start_date='2025-09-01', adj_end_date='2025-09-30',
                      filter_date='2025-09-01'):
    """
    Convert SQL query logic to Python pandas operations
    
    Parameters:
    csv_path: Path to the CSV file
    table_name: Name of the source table (for reference)
    adj_start_date: Adjustment start date (format: 'YYYY-MM-DD')
    adj_end_date: Adjustment end date (format: 'YYYY-MM-DD')
    filter_date: Filter date for Price Adjustment Start Date (format: 'YYYY-MM-DD')
    """
    # Load the CSV file
    df = pd.read_csv(csv_path)
    
    # Apply WHERE clause filters
    # Market Index Name contains 'Fixed' or 'Quarterly'
    market_filter = (df['Market Index Name'].str.contains('Fixed', na=False) | 
                    df['Market Index Name'].str.contains('Quarterly', na=False))
    print(f"Debug: Records with Market Index Name containing 'Fixed' or 'Quarterly': {market_filter.sum()}")
    # Item Description that doesn't begin with "DG"
    item_filter = ~df['Item Description'].str.startswith('DG', na=False)
    print(f"Debug: Records with Item Description not starting with 'DG': {item_filter.sum()}")

    
    # Price Adjustment Start Date equals the specified date
    # Convert both dates to the same format for comparison
    df_dates = pd.to_datetime(df['Price Adjustment Start Date']).dt.date
    target_date = pd.to_datetime(filter_date).date()
    date_filter = (df_dates == target_date)
    print(f"Debug: Records with start date {filter_date}: {date_filter.sum()}")
    
    # Apply filters (removed DG filter as Fixed pricing should include all items)
    filtered_df = df[market_filter & date_filter & item_filter].copy()
    print(f"Debug: Final filtered records: {len(filtered_df)}")
    
    # Create the result DataFrame with SELECT clause mappings
    result = pd.DataFrame({
        'Pricelistname': 'CP_Market Price',
        'Pricinguom': filtered_df['Pricing UOM'],
        'Baselineprice': 0,
        'Chargestartdate': pd.to_datetime('2020-01-01 00:00:00'),
        'Chargeenddate': '',
        'Item_Name': filtered_df['Item'],
        'Customername': filtered_df['Customer'],
        'Customernumber': '',
        'Shiptositename': filtered_df['Ship To Site Name'],
        'Customersitenumber': filtered_df['Party Site Number'],
        'Adjustmenttype': 'MARKUP_AMOUNT',
        'Adjustmentamount': filtered_df['Total Price Per Pricing UOM'],
        'Adjustmentbasis': '',
        'Precedence': '',
        'Market': filtered_df['Market Index Name'],
        'Age': '',
        'Spec': '',
        'Grade': '',
        'Adjustmentstartdate': pd.to_datetime(f'{adj_start_date} 00:00:00'),
        'Adjustmentenddate': pd.to_datetime(f'{adj_end_date} 23:59:00'),
        'Status': 'N'
    })
    
    return result

# Main execution
if __name__ == "__main__":
    print("Starting Fixed Pricing Execution...")
    print(f"Current working directory: {os.getcwd()}")
    
    try:
        # Use relative paths from script directory
        price_data_path = get_relative_path('../../../../../Monthly Refreshed Data_Common/Old_Price_Build.csv')
        assumptions_path = get_relative_path('../../../../../Monthly Refreshed Data_Common/Effective_Date_Assumptions.csv')
        
        print(f"Script location: {Path(__file__).parent.absolute()}")
        print(f"Looking for price data at: {price_data_path}")
        print(f"Looking for assumptions at: {assumptions_path}")
        
        # Debug: Uncomment the lines below if you need to see the target directory contents
        # target_dir = price_data_path.parent
        # print(f"Contents of {target_dir}:")
        # if target_dir.exists():
        #     for item in target_dir.iterdir():
        #         print(f"  - {item.name}")
        # else:
        #     print(f"  Directory does not exist: {target_dir}")
        
        # Check if files exist
        if not price_data_path.exists():
            print(f"✗ Price data file not found at: {price_data_path}")
            print("Please check if the file exists and the path is correct.")
            exit(1)
        else:
            print(f"✓ Found price data file at: {price_data_path}")
        
        if not assumptions_path.exists():
            print(f"✗ Assumptions file not found at: {assumptions_path}")
            print("Please check if the file exists and the path is correct.")
            exit(1)
        else:
            print(f"✓ Found assumptions file at: {assumptions_path}")
        
        # Load the primary DataFrame
        price_df = pd.read_csv(price_data_path)
        print(f"Loaded {len(price_df)} records from Old_Price_Build.csv")
        
        # Load and parse date assumptions
        print("Loading date assumptions...")
        adj_start_date, adj_end_date, filtered_date = load_date_assumptions(assumptions_path)
        
        print(f"Derived dates:")
        print(f"  adj_start_date: {adj_start_date}")
        print(f"  adj_end_date: {adj_end_date}")
        print(f"  filtered_date: {filtered_date}")
        
        # Process the price data with derived dates
        print("Calling process_price_data function...")
        result_df = process_price_data(
            csv_path=str(price_data_path),
            adj_start_date=adj_start_date.strftime('%Y-%m-%d'),
            adj_end_date=adj_end_date.strftime('%Y-%m-%d'),
            filter_date=filtered_date.strftime('%Y-%m-%d')
        )
        print(f"Function returned {len(result_df)} records")
        
        # Save the result to CSV using relative path
        output_dir = get_relative_path('../../../Output')
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / 'fixed_vbcs.csv'
        result_df.to_csv(output_path, index=False)
        
        print(f"Processing completed successfully!")
        print(f"Output saved to: {output_path}")
        print(f"Total records processed: {len(result_df)}")
        
    except Exception as e:
        print(f"Error during execution: {e}")
        print("Please check the error message above and fix any issues.")
        exit(1)