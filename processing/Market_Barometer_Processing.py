"""
Market Barometer Data Processing Module

This module fetches economic data from FRED (Federal Reserve Economic Data) and EIA (Energy Information Administration)
APIs, processes the data, and saves it to a CSV file. It also provides forecasting capabilities using Holt-Winters
and SARIMA models.

Author: Pricing Execution Agent
"""

import pandas as pd
import requests
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Final
from datetime import datetime
import warnings

# Suppress warnings for cleaner output
warnings.filterwarnings('ignore')

# --- Configuration Constants ---
BASE_DIR = Path(__file__).parent.parent
DATA_DIR: Final[Path] = BASE_DIR / "data" / "Market Barometer"
API_KEYS_FILE: Final[Path] = DATA_DIR / "API_Keys.txt"
OUTPUT_CSV_FILE: Final[Path] = DATA_DIR / "inflation_data.csv"

# API Endpoints
FRED_BASE_URL: Final[str] = "https://api.stlouisfed.org/fred/series/observations"
EIA_BASE_URL: Final[str] = "https://api.eia.gov/v2"
REQUEST_TIMEOUT: Final[int] = 30

# Auto-refresh configuration (15 days)
AUTO_REFRESH_DAYS: Final[int] = 15

# FRED API Configuration
FRED_SERIES: Final[Dict[str, str]] = {
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

# EIA API Configuration (V2)
EIA_SERIES: Final[Dict[str, Dict]] = {
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


# --- Utility Functions ---

def load_api_keys(file_path: Path) -> Dict[str, str]:
    """
    Load API keys, prioritizing environment variables (FRED_API_KEY, EIA_API_KEY) 
    over the text file, and normalizing file keys to uppercase.
    
    Args:
        file_path: Path to the API keys file (for fallback).
            
    Returns:
        Dictionary mapping service names ('FRED', 'EIA') to API keys.
        
    Raises:
        ValueError: If file reading fails.
    """
    api_keys = {}
    
    # 1. Check Environment Variables (Preferred)
    if os.getenv("FRED_API_KEY"):
        api_keys['FRED'] = os.getenv("FRED_API_KEY")
    if os.getenv("EIA_API_KEY"):
        api_keys['EIA'] = os.getenv("EIA_API_KEY")
        
    # 2. Fallback to file if keys are missing
    if (not api_keys.get('FRED') or not api_keys.get('EIA')) and file_path.exists():
        try:
            with open(file_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or ':' not in line:
                        continue
                    
                    service, key = line.split(':', 1)
                    service_name = service.strip().upper()
                    
                    # Only store if it's FRED/EIA AND not already set by ENV
                    if service_name in ['FRED', 'EIA'] and service_name not in api_keys:
                        api_keys[service_name] = key.strip()

        except Exception as e:
            raise ValueError(f"Error reading API keys file: {e}")

    return api_keys


def test_api_keys(api_keys: Dict[str, str]) -> Tuple[bool, bool]:
    """
    Test if API keys are valid by making test requests.
    
    Args:
        api_keys: Dictionary with 'FRED' and 'EIA' keys
        
    Returns:
        Tuple of (fred_valid, eia_valid) boolean values
    """
    fred_valid = False
    eia_valid = False
    
    with requests.Session() as session:
        # Test FRED
        if api_keys.get('FRED'):
            test_fred_params = {
                "series_id": "PPIACO",
                "api_key": api_keys['FRED'],
                "file_type": "json",
                "limit": 1
            }
            test_fred = make_api_request(session, FRED_BASE_URL, test_fred_params)
            fred_valid = test_fred is not None
        
        # Test EIA
        if api_keys.get('EIA'):
            test_eia_url = f"{EIA_BASE_URL}/petroleum/pri/spt/data/"
            test_eia_params = {
                "api_key": api_keys['EIA'],
                "frequency": "daily",
                "data[0]": "value",
                "facets[series][]": "RWTC",
                "length": 1
            }
            test_eia = make_api_request(session, test_eia_url, test_eia_params)
            eia_valid = test_eia is not None
    
    return fred_valid, eia_valid


def make_api_request(
    session: requests.Session, url: str, params: Dict, timeout: int = REQUEST_TIMEOUT
) -> Optional[Dict]:
    """
    Make an HTTP GET request to an API endpoint using a shared requests.Session.
    
    Args:
        session: The requests.Session object for connection pooling.
        url: The API endpoint URL
        params: Query parameters for the request
        timeout: Request timeout in seconds
        
    Returns:
        JSON response as a dictionary, or None if request fails
    """
    try:
        response = session.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException:
        return None


# --- Data Fetching Functions ---

def fetch_fred_series(
    session: requests.Session, series_id: str, api_key: str, series_name: str
) -> pd.DataFrame:
    """
    Fetch data for a single FRED series using a shared Session.
    
    Args:
        session: Requests session for connection pooling
        series_id: FRED series identifier
        api_key: FRED API key
        series_name: Human-readable series name
        
    Returns:
        DataFrame with Date, Value, Series, Source columns
    """
    print(f"Fetching '{series_name}' from FRED...", flush=True)
    
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json"
    }
    
    data = make_api_request(session, FRED_BASE_URL, params)
    
    if not data or not data.get('observations'):
        print(f"Warning: No observations found for FRED series '{series_name}' ({series_id}).", file=sys.stderr, flush=True)
        return pd.DataFrame()
    
    try:
        df = pd.DataFrame(data['observations'])
        df = df[['date', 'value']].copy()
        df.rename(columns={'date': 'Date', 'value': 'Value'}, inplace=True)
        
        df['Date'] = pd.to_datetime(df['Date'])
        df['Value'] = pd.to_numeric(df['Value'], errors='coerce')
        df['Series'] = series_name
        df['Source'] = 'FRED'
        
        return df[['Date', 'Value', 'Series', 'Source']]
    except Exception as e:
        print(f"Error processing FRED data for {series_name}: {e}", file=sys.stderr, flush=True)
        return pd.DataFrame()


def fetch_eia_series(
    session: requests.Session, api_key: str, series_name: str, series_config: Dict
) -> pd.DataFrame:
    """
    Fetch data for a single EIA series using V2 API and a shared Session.
    
    Args:
        session: Requests session for connection pooling
        api_key: EIA API key
        series_name: Human-readable series name
        series_config: Configuration dictionary with 'route' and 'params'
        
    Returns:
        DataFrame with Date, Value, Series, Source columns
    """
    print(f"Fetching '{series_name}' from EIA...", flush=True)
    
    url = f"{EIA_BASE_URL}/{series_config['route']}/data/"
    params = series_config['params'].copy()
    params['api_key'] = api_key
    
    data = make_api_request(session, url, params)
    
    if not data or 'response' not in data:
        print(f"Warning: Invalid response for EIA series '{series_name}'.", file=sys.stderr, flush=True)
        return pd.DataFrame()
    
    response_data = data.get('response', {})
    if 'data' not in response_data or not response_data['data']:
        print(f"Warning: No data found for EIA series '{series_name}'.", file=sys.stderr, flush=True)
        return pd.DataFrame()

    try:
        df = pd.DataFrame(response_data['data'])
        
        value_col = 'value' if 'value' in df.columns else 'price'
        if value_col not in df.columns:
            print(f"Warning: No expected value column found for EIA series '{series_name}'.", file=sys.stderr, flush=True)
            return pd.DataFrame()
        
        df.rename(columns={'period': 'Date', value_col: 'Value'}, inplace=True)
        
        df['Date'] = pd.to_datetime(df['Date'])
        df['Value'] = pd.to_numeric(df['Value'], errors='coerce')
        df['Series'] = series_name
        df['Source'] = 'EIA'
        
        return df[['Date', 'Value', 'Series', 'Source']]
    except Exception as e:
        print(f"Error processing EIA data for {series_name}: {e}", file=sys.stderr, flush=True)
        return pd.DataFrame()


def fetch_all_fred_data(session: requests.Session, api_key: str) -> Tuple[List[pd.DataFrame], List[str]]:
    """
    Fetch all FRED series defined in FRED_SERIES.
    
    Args:
        session: Requests session for connection pooling
        api_key: FRED API key
        
    Returns:
        Tuple of (list of successful DataFrames, list of failed series names)
    """
    data_frames = []
    failed_series = []
    
    for series_name, series_id in FRED_SERIES.items():
        df = fetch_fred_series(session, series_id, api_key, series_name)
        if not df.empty:
            data_frames.append(df)
        else:
            failed_series.append(f"{series_name} (FRED)")
    
    return data_frames, failed_series


def fetch_all_eia_data(session: requests.Session, api_key: str) -> Tuple[List[pd.DataFrame], List[str]]:
    """
    Fetch all EIA series defined in EIA_SERIES.
    
    Args:
        session: Requests session for connection pooling
        api_key: EIA API key
        
    Returns:
        Tuple of (list of successful DataFrames, list of failed series names)
    """
    data_frames = []
    failed_series = []
    
    for series_name, series_config in EIA_SERIES.items():
        df = fetch_eia_series(session, api_key, series_name, series_config)
        if not df.empty:
            data_frames.append(df)
        else:
            failed_series.append(f"{series_name} (EIA)")
    
    return data_frames, failed_series


def process_and_save_data(data_frames: List[pd.DataFrame], output_path: Path) -> bool:
    """
    Combine all data frames, clean the data, and save to CSV.
    
    Args:
        data_frames: List of DataFrames to combine
        output_path: Path where CSV file should be saved
        
    Returns:
        True if successful, False otherwise
    """
    if not data_frames:
        print("No data to process.", flush=True)
        return False
    
    try:
        # Combine all data frames
        final_df = pd.concat(data_frames, ignore_index=True)
        
        # Clean the data: remove rows with missing values
        initial_count = len(final_df)
        final_df.dropna(subset=['Value', 'Date'], inplace=True)
        removed_count = initial_count - len(final_df)
        
        if removed_count > 0:
            print(f"Removed {removed_count} rows with missing (NaN) values.", flush=True)
        
        # Sort by series name and date
        final_df.sort_values(by=['Series', 'Date'], inplace=True)
        
        # Reset index after sorting
        final_df.reset_index(drop=True, inplace=True)
        
        # Ensure output directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Save to CSV
        final_df.to_csv(output_path, index=False)
        
        print(f"\nData processing complete.", flush=True)
        print(f"Total records saved: {len(final_df)}", flush=True)
        print(f"Unique series: {final_df['Series'].nunique()}", flush=True)
        print(f"Date range: {final_df['Date'].min().strftime('%Y-%m-%d')} to {final_df['Date'].max().strftime('%Y-%m-%d')}", flush=True)
        print(f"Data saved to: {output_path.absolute()}", flush=True)
        
        return True
    except Exception as e:
        print(f"Error processing and saving data: {e}", file=sys.stderr, flush=True)
        return False


def print_fetch_summary(failed_series: List[str], total_series: int):
    """
    Print a summary of the data fetching operation.
    
    Args:
        failed_series: List of series names that failed to fetch
        total_series: Total number of series attempted
    """
    print("\n" + "=" * 50, flush=True)
    print("FETCH SUMMARY", flush=True)
    print("=" * 50, flush=True)
    
    successful = total_series - len(failed_series)
    print(f"Successful: {successful}/{total_series}", flush=True)
    
    if failed_series:
        print(f"\nFailed series ({len(failed_series)}):", file=sys.stderr, flush=True)
        for series in failed_series:
            print(f"  - {series}", file=sys.stderr, flush=True)
    else:
        print("\nAll series fetched successfully!", flush=True)
    
    print("=" * 50, flush=True)


def should_refresh_data(csv_path: Path) -> bool:
    """
    Check if data should be refreshed based on file age.
    
    Args:
        csv_path: Path to the CSV file
        
    Returns:
        True if data should be refreshed (file doesn't exist or is older than AUTO_REFRESH_DAYS)
    """
    if not csv_path.exists():
        return True
    
    file_age = datetime.now() - datetime.fromtimestamp(csv_path.stat().st_mtime)
    return file_age.days >= AUTO_REFRESH_DAYS


def auto_refresh_data():
    """
    Automatically refresh data if it's older than AUTO_REFRESH_DAYS.
    This function can be called periodically (e.g., via scheduler or on page load).
    """
    if should_refresh_data(OUTPUT_CSV_FILE):
        print(f"Data is older than {AUTO_REFRESH_DAYS} days. Refreshing...", flush=True)
        main()
    else:
        print("Data is up to date. No refresh needed.", flush=True)


# --- Forecasting Functions ---

FUTURE_CSV_FILE: Final[Path] = DATA_DIR / "future_data.csv"


def get_forecast_data(df: pd.DataFrame, horizon: int = 24, output_path: Optional[Path] = None) -> pd.DataFrame:
    """
    Generate forecasts for each series in the dataframe using Holt-Winters and SARIMA models.
    
    This function calculates Month-over-Month % change, then applies forecasting models.
    The forecast data is saved to a CSV file for efficient loading in the view layer.
    
    Args:
        df: DataFrame with columns ['Date', 'Value', 'Series', 'Source']
        horizon: Number of months to forecast forward (default: 24)
        output_path: Optional path for output CSV file. If None, uses default location.
        
    Returns:
        DataFrame with columns: ['Date', 'Series', 'Baseline', 'Upper', 'Lower']
    """
    if output_path is None:
        output_path = FUTURE_CSV_FILE
    
    try:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing
        from statsmodels.tsa.statespace.sarimax import SARIMAX
        from statsmodels.tools.sm_exceptions import ConvergenceWarning
        warnings.filterwarnings('ignore', category=ConvergenceWarning)
        STATSMODELS_AVAILABLE = True
    except ImportError:
        print("ERROR: statsmodels not available. Forecasting requires statsmodels.", file=sys.stderr, flush=True)
        print("Please install it with: pip install statsmodels", file=sys.stderr, flush=True)
        STATSMODELS_AVAILABLE = False
        return pd.DataFrame()
    
    forecast_rows = []
    
    for series_name in df['Series'].unique():
        series_data = df[df['Series'] == series_name].copy().sort_values('Date')
        
        if len(series_data) < 12:  # Need at least 12 months for meaningful forecasts
            continue
        
        try:
            # Calculate Month-over-Month % change
            series_data_indexed = series_data.set_index('Date').copy()
            series_data_indexed['MoM_PctChange'] = series_data_indexed['Value'].pct_change() * 100
            series_data_indexed = series_data_indexed.dropna()
            
            if len(series_data_indexed) < 6:
                continue
            
            # Get last actual value and date for connection
            last_value = series_data['Value'].iloc[-1]
            last_date = series_data['Date'].iloc[-1]
            
            # Generate future dates
            if isinstance(last_date, str):
                last_date = pd.to_datetime(last_date)
            elif not isinstance(last_date, pd.Timestamp):
                last_date = pd.Timestamp(last_date)
            
            future_dates = pd.date_range(
                start=last_date + pd.DateOffset(months=1),
                periods=horizon,
                freq='MS'  # Month start
            )
            
            # Prepare data for forecasting (use MoM % change)
            y = series_data_indexed['MoM_PctChange'].values
            
            # 1. Holt-Winters (Exponential Smoothing) - Baseline
            try:
                hw_model = ExponentialSmoothing(
                    y,
                    seasonal_periods=12,
                    trend='add',
                    seasonal='add'
                )
                hw_fit = hw_model.fit(optimized=True)
                hw_forecast = hw_fit.forecast(horizon)
                
                # Convert % change back to absolute values
                baseline_values = []
                current_value = last_value
                for pct_change in hw_forecast:
                    current_value = current_value * (1 + pct_change / 100)
                    baseline_values.append(current_value)
            except Exception as e:
                # Fallback: simple linear trend
                print(f"Warning: Holt-Winters failed for {series_name}, using linear trend: {e}", file=sys.stderr, flush=True)
                baseline_values = [last_value] * horizon
            
            # 2. SARIMA - Uncertainty bounds
            try:
                # Auto-select SARIMA parameters (simplified)
                sarima_model = SARIMAX(
                    y,
                    order=(1, 1, 1),
                    seasonal_order=(1, 1, 1, 12),
                    enforce_stationarity=False,
                    enforce_invertibility=False
                )
                sarima_fit = sarima_model.fit(disp=False, maxiter=50)
                sarima_forecast = sarima_fit.get_forecast(steps=horizon)
                sarima_conf_int = sarima_forecast.conf_int()
                
                # Convert % change bounds back to absolute values
                upper_values = []
                lower_values = []
                current_upper = last_value
                current_lower = last_value
                
                # Handle both DataFrame and numpy array formats
                if isinstance(sarima_conf_int, pd.DataFrame):
                    # DataFrame format
                    for idx in range(horizon):
                        upper_pct = sarima_conf_int.iloc[idx, 1]
                        lower_pct = sarima_conf_int.iloc[idx, 0]
                        
                        current_upper = current_upper * (1 + upper_pct / 100)
                        current_lower = current_lower * (1 + lower_pct / 100)
                        
                        upper_values.append(current_upper)
                        lower_values.append(current_lower)
                else:
                    # numpy array format - conf_int() returns array with shape (horizon, 2)
                    # Column 0 is lower bound, column 1 is upper bound
                    for idx in range(horizon):
                        lower_pct = sarima_conf_int[idx, 0]
                        upper_pct = sarima_conf_int[idx, 1]
                        
                        current_upper = current_upper * (1 + upper_pct / 100)
                        current_lower = current_lower * (1 + lower_pct / 100)
                        
                        upper_values.append(current_upper)
                        lower_values.append(current_lower)
            except Exception as e:
                # Fallback: use baseline with ±10% uncertainty
                print(f"Warning: SARIMA failed for {series_name}, using ±10% uncertainty: {e}", file=sys.stderr, flush=True)
                upper_values = [v * 1.1 for v in baseline_values]
                lower_values = [v * 0.9 for v in baseline_values]
            
            # Create rows for this series
            for i, future_date in enumerate(future_dates):
                forecast_rows.append({
                    'Date': future_date,
                    'Series': series_name,
                    'Baseline': baseline_values[i],
                    'Upper': upper_values[i],
                    'Lower': lower_values[i]
                })
            
        except Exception as e:
            print(f"Warning: Could not generate forecast for {series_name}: {e}", file=sys.stderr, flush=True)
            continue
    
    if not forecast_rows:
        print("Warning: No forecast data generated.", file=sys.stderr, flush=True)
        return pd.DataFrame()
    
    # Create DataFrame and save to CSV
    future_df = pd.DataFrame(forecast_rows)
    future_df = future_df.sort_values(by=['Series', 'Date'])
    
    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Save to CSV
    future_df.to_csv(output_path, index=False)
    print(f"Forecast data saved to: {output_path.absolute()}", flush=True)
    print(f"Total forecast records: {len(future_df)}", flush=True)
    
    return future_df


# --- Main Execution ---

def main(api_keys_file_path: Optional[str] = None, output_file_path: Optional[str] = None):
    """
    Main execution function that orchestrates the data fetching and processing.
    
    Args:
        api_keys_file_path: Optional path to API keys file. If None, uses default location.
        output_file_path: Optional path for output CSV file. If None, uses default location.
    """
    try:
        print("Market Barometer Data Processing", flush=True)
        print("=" * 50, flush=True)
        print(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        print(flush=True)
    
        # Determine API keys file path
        if api_keys_file_path:
            api_keys_file = Path(api_keys_file_path)
        else:
            api_keys_file = API_KEYS_FILE
        
        # Determine output file path
        if output_file_path:
            output_csv_file = Path(output_file_path)
        else:
            output_csv_file = OUTPUT_CSV_FILE
        
        # Load API keys
        try:
            api_keys = load_api_keys(api_keys_file)
            fred_key = api_keys.get('FRED')
            eia_key = api_keys.get('EIA')
            
            if not fred_key:
                print("ERROR: FRED API key not found. Check environment variables (FRED_API_KEY) or API_Keys.txt", file=sys.stderr, flush=True)
                return False
            
            if not eia_key:
                print("ERROR: EIA API key not found. Check environment variables (EIA_API_KEY) or API_Keys.txt", file=sys.stderr, flush=True)
                return False
                
            print(f"API keys loaded successfully.", flush=True)
            print(flush=True)
        except Exception as e:
            print(f"ERROR: Failed to load API keys: {e}", file=sys.stderr, flush=True)
            return False

        # Initialize Session for better connection pooling and performance
        with requests.Session() as session:
            # Test API keys
            print("Testing API keys...", flush=True)
            test_fred_params = {
                "series_id": "PPIACO",
                "api_key": fred_key,
                "file_type": "json",
                "limit": 1
            }
            test_fred = make_api_request(session, FRED_BASE_URL, test_fred_params)
            if not test_fred:
                print("WARNING: FRED API key test failed. The key may be invalid or the service is down.", file=sys.stderr, flush=True)
            else:
                print("FRED API key: OK", flush=True)
            
            test_eia_url = f"{EIA_BASE_URL}/petroleum/pri/spt/data/"
            test_eia_params = {
                "api_key": eia_key,
                "frequency": "daily",
                "data[0]": "value",
                "facets[series][]": "RWTC",
                "length": 1
            }
            test_eia = make_api_request(session, test_eia_url, test_eia_params)
            if not test_eia:
                print("WARNING: EIA API key test failed. The key may be invalid or the service is down.", file=sys.stderr, flush=True)
            else:
                print("EIA API key: OK", flush=True)
            print(flush=True)
        
            # Fetch all data
            all_data_frames = []
            all_failed_series = []
            
            # Fetch FRED data
            fred_data, fred_failed = fetch_all_fred_data(session, fred_key)
            all_data_frames.extend(fred_data)
            all_failed_series.extend(fred_failed)
            
            # Fetch EIA data
            eia_data, eia_failed = fetch_all_eia_data(session, eia_key)
            all_data_frames.extend(eia_data)
            all_failed_series.extend(eia_failed)
            
            # Print summary
            total_series = len(FRED_SERIES) + len(EIA_SERIES)
            print_fetch_summary(all_failed_series, total_series)
            
            # Process and save data
            if all_data_frames:
                success = process_and_save_data(all_data_frames, output_csv_file)
                if not success:
                    print("ERROR: Failed to save data to CSV.", file=sys.stderr, flush=True)
                    return False
                
                # Generate and save forecast data
                print("\nGenerating forecast data...", flush=True)
                try:
                    # Load the saved data for forecasting
                    saved_df = pd.read_csv(output_csv_file)
                    saved_df['Date'] = pd.to_datetime(saved_df['Date'])
                    
                    # Generate forecasts and save to CSV
                    future_output_path = output_csv_file.parent / "future_data.csv"
                    get_forecast_data(saved_df, horizon=24, output_path=future_output_path)
                    print("Forecast data generation complete.", flush=True)
                except Exception as e:
                    print(f"Warning: Could not generate forecast data: {e}", file=sys.stderr, flush=True)
            else:
                print("\nERROR: No data was fetched. Cannot create CSV file.", file=sys.stderr, flush=True)
                return False
    
        print(f"\nEnd time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        return True
    
    except Exception as e:
        print(f"\nFATAL ERROR: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return False


if __name__ == "__main__":
    main()
