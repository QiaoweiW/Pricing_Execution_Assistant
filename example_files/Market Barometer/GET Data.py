import pandas as pd
import requests
import os

# --- Configuration: API Keys and Series Information ---
# For better security, it's recommended to use environment variables or a config file for API keys.
API_KEYS = {
    "FRED": "e81063730aa37556a7b27602308970ad",
    "EIA": "ZbPskzmEle3efeCEapFSjiJaiGd5YRRmfSiOQhnI"
}

# Series IDs for the data we want to fetch from the APIs
FRED_SERIES = {
    "PPI Food Industry": "PCU311311",
    "PPI All Commodities": "PPIACO",
    "PPI Maintenance/Repair Construction": "WPUIP2320001",
    "PPI Paperboard": "WPU091411",
    "PPI Plastics Material and Resin Manufacturing": "PCU325211325211",
    "PPI Chocolate and Confectionery Manufacturing": "PCU3113531135",
    "Global Price of Cocoa": "PCOCOUSDM",
    "Sugar Beet Sugar Price": "WPU02530702",
    "Avg Hourly Earnings Total Private": "CES0500000003",
    "Wages Private Industry": "ECIWAG",
    "Wood Pallets Price": "PCU3219203219205",
    "West Coast Diesel Price": "GASDESWCW",
    "US Diesel Sales Price": "GASDESW",
    "Natural Gas Price (Henry Hub)": "MHHNGSP"
}

# --- EIA API Configuration ---
# EIA API uses a different structure with routes and facets
EIA_SERIES_V2 = {
    "WTI Crude Oil": {
        "route": "petroleum/pri/spt",
        "params": {
            "frequency": "daily",
            "data[0]": "value",
            "facets[series][]": "RWTC"
        }
    },
    "Electricity Price Industrial - WA": {
        "route": "electricity/retail-sales",
        "params": {
            "frequency": "monthly",
            "data[0]": "price",
            "facets[stateid][]": "WA",
            "facets[sectorid][]": "IND"
        }
    },
    "Electricity Price Industrial - OR": {
        "route": "electricity/retail-sales",
        "params": {
            "frequency": "monthly",
            "data[0]": "price",
            "facets[stateid][]": "OR",
            "facets[sectorid][]": "IND"
        }
    },
    "Electricity Price Industrial - ID": {
        "route": "electricity/retail-sales",
        "params": {
            "frequency": "monthly",
            "data[0]": "price",
            "facets[stateid][]": "ID",
            "facets[sectorid][]": "IND"
        }
    },
    "Electricity Price Industrial - MT": {
        "route": "electricity/retail-sales",
        "params": {
            "frequency": "monthly",
            "data[0]": "price",
            "facets[stateid][]": "MT",
            "facets[sectorid][]": "IND"
        }
    }
}


# --- Function to Fetch FRED Data ---
def get_fred_data(series_id, api_key, series_name):
    """
    Fetches and processes data for a given series from the FRED API.
    """
    print(f"Fetching '{series_name}' from FRED...")
    url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={api_key}&file_type=json"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if not data.get('observations'):
            print(f"Warning: No observations found for FRED series '{series_name}' ({series_id}).")
            return pd.DataFrame()

        df = pd.DataFrame(data['observations'])
        df = df[['date', 'value']]
        df.rename(columns={'date': 'Date', 'value': 'Value'}, inplace=True)
        
        df['Date'] = pd.to_datetime(df['Date'])
        df['Value'] = pd.to_numeric(df['Value'], errors='coerce')
        df['Series'] = series_name
        df['Source'] = 'FRED' # Add source column
        return df
    except requests.exceptions.RequestException as e:
        print(f"Error fetching FRED data for {series_name}: {e}")
        return pd.DataFrame()

# --- Function to Fetch EIA Data (Updated for V2 API) ---
def get_eia_data_v2(api_key, series_name, series_config):
    """
    Fetches and processes data for a given series from the EIA V2 API.
    """
    print(f"Fetching '{series_name}' from EIA...")
    base_url = f"https://api.eia.gov/v2/{series_config['route']}/data/"
    params = series_config['params'].copy()
    params['api_key'] = api_key
    
    try:
        response = requests.get(base_url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if 'response' not in data or 'data' not in data['response'] or not data['response']['data']:
            print(f"Warning: No data found for EIA series '{series_name}'.")
            return pd.DataFrame()

        df = pd.DataFrame(data['response']['data'])
        
        value_col = 'value' if 'value' in df.columns else 'price'
        df.rename(columns={'period': 'Date', value_col: 'Value'}, inplace=True)
        
        df['Date'] = pd.to_datetime(df['Date'])
        df['Value'] = pd.to_numeric(df['Value'], errors='coerce')
        df['Series'] = series_name
        df['Source'] = 'EIA' # Add source column
        
        return df[['Date', 'Value', 'Series', 'Source']]
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching EIA data for {series_name}: {e}")
        return pd.DataFrame()


# --- Main Script Execution ---
if __name__ == "__main__":
    all_data_frames = []
    failed_fetches = []

    # Fetch all specified FRED data series
    for name, series_id in FRED_SERIES.items():
        fred_df = get_fred_data(series_id, API_KEYS["FRED"], name)
        if not fred_df.empty:
            all_data_frames.append(fred_df)
        else:
            failed_fetches.append(f"{name} (FRED)")

    # Fetch all specified EIA data series using the new V2 function
    for name, config in EIA_SERIES_V2.items():
        eia_df = get_eia_data_v2(API_KEYS["EIA"], name, config)
        if not eia_df.empty:
            all_data_frames.append(eia_df)
        else:
            failed_fetches.append(f"{name} (EIA)")

    # Print final status message
    print("\n--- Fetch Status ---")
    if not failed_fetches:
        print("All fetches completed successfully.")
    else:
        print("Fetches failed for the following series:")
        for failed_item in failed_fetches:
            print(f"- {failed_item}")
    print("--------------------")

    if not all_data_frames:
        print("\nNo data was fetched. Exiting script.")
    else:
        # Combine all fetched data into a single DataFrame
        final_df = pd.concat(all_data_frames, ignore_index=True)
        
        # Clean up the data: drop rows with no value or no date and sort
        final_df.dropna(subset=['Value', 'Date'], inplace=True)
        final_df.sort_values(by=['Series', 'Date'], inplace=True)

        # Save the final DataFrame to a CSV file
        output_filename = 'inflation_data.csv'
        final_df.to_csv(output_filename, index=False)

        print(f"\nData fetching complete.")
        print(f"Combined data saved to '{os.path.abspath(output_filename)}'")
