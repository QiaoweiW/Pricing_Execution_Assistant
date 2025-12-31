"""
Market Barometer page view.

This page allows users to view market data from FRED and EIA APIs,
visualize the inflation data in an interactive dashboard organized by cost categories,
and view forecasts for future trends.
"""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from pathlib import Path
import importlib.util
from datetime import date
from typing import Dict, List, Optional, Tuple

from utils.ui_helpers import apply_custom_css

# Import the processing module dynamically
BASE_DIR = Path(__file__).parent.parent
PROCESSING_FILE = BASE_DIR / "processing" / "Market_Barometer_Processing.py"
spec = importlib.util.spec_from_file_location("Market_Barometer_Processing", str(PROCESSING_FILE))
mbp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mbp)

# Data paths
DATA_DIR = BASE_DIR / "data" / "Market Barometer"
CSV_FILE = DATA_DIR / "inflation_data.csv"
FUTURE_CSV_FILE = DATA_DIR / "future_data.csv"
API_KEYS_FILE = DATA_DIR / "API_Keys.txt"


# --- Configuration: Series Grouping and URL Mapping ---

# Map series names to their FRED URLs (for clickable links)
FRED_SERIES_URLS: Dict[str, str] = {
    "PPI Food Industry": "https://fred.stlouisfed.org/series/PCU311311",
    "PPI All Commodities": "https://fred.stlouisfed.org/series/PPIACO",
    "PPI Maintenance/Repair Construction": "https://fred.stlouisfed.org/series/WPUIP2320001",
    "PPI Paperboard": "https://fred.stlouisfed.org/series/WPU091411",
    "PPI Plastics Material and Resin Manufacturing": "https://fred.stlouisfed.org/series/PCU325211325211",
    "PPI Chocolate and Confectionery Manufacturing": "https://fred.stlouisfed.org/series/PCU3113531135",
    "Global Price of Cocoa": "https://fred.stlouisfed.org/series/PCOCOUSDM",
    "Sugar Beet Sugar Price": "https://fred.stlouisfed.org/series/WPU02530702",
    "Avg Hourly Earnings Total Private": "https://fred.stlouisfed.org/series/CES0500000003",
    "Wages Private Industry": "https://fred.stlouisfed.org/series/ECIWAG",
    "Wood Pallets Price": "https://fred.stlouisfed.org/series/PCU3219203219205",
    "West Coast Diesel Price": "https://fred.stlouisfed.org/series/GASDESWCW",
    "US Diesel Sales Price": "https://fred.stlouisfed.org/series/GASDESW",
    "Natural Gas Price (Henry Hub)": "https://fred.stlouisfed.org/series/MHHNGSP"
}

# EIA URLs
EIA_ELECTRICITY_URL: str = "https://www.eia.gov/electricity/data/browser/#/topic/5?agg=0,1&geo=vvvvvvvvvvvvo&linechart=ELEC.SALES.TX-ALL.M~ELEC.SALES.TX-RES.M~ELEC.SALES.TX-COM.M~ELEC.SALES.TX-IND.M&columnchart=ELEC.SALES.TX-ALL.M~ELEC.SALES.TX-RES.M~ELEC.SALES.TX-COM.M~ELEC.SALES.TX-IND.M&map=ELEC.SALES.US-ALL.M&freq=M&start=200101&end=201510&ctype=linechart&ltype=pin&rtype=s&maptype=0&rse=0&pin=&endsec=vg"
EIA_CRUDE_OIL_URL: str = "https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm"

# Define series groups for organized display
SERIES_GROUPS: Dict[str, List[str]] = {
    "Labor Cost": [
        "Avg Hourly Earnings Total Private",
        "Wages Private Industry"
    ],
    "Electricity Cost": [
        "Electricity Price Industrial - WA",
        "Electricity Price Industrial - OR",
        "Electricity Price Industrial - ID",
        "Electricity Price Industrial - MT"
    ],
    "Natural Gas Cost": [
        "Natural Gas Price (Henry Hub)"
    ],
    "Other Manufacturing Costs": [
        "PPI Food Industry",
        "PPI All Commodities",
        "PPI Maintenance/Repair Construction"
    ],
    "Packaging Cost": [
        "PPI Paperboard",
        "PPI Plastics Material and Resin Manufacturing"
    ],
    "Ingredient Cost": [
        "Global Price of Cocoa",
        "PPI Chocolate and Confectionery Manufacturing",
        "Sugar Beet Sugar Price"
    ],
    "Freight Cost": [
        "West Coast Diesel Price",
        "US Diesel Sales Price",
        "WTI Crude Oil",
        "Wood Pallets Price"
    ]
}


@st.cache_data
def load_inflation_data_cached(csv_path: Path, file_mtime: float) -> pd.DataFrame:
    """
    Load inflation data from CSV file with caching.
    
    Cache key includes file modification time, so cache invalidates when file is updated.
    This ensures efficient loading without unnecessary file reads.
    
    Args:
        csv_path: Path to the CSV file
        file_mtime: File modification time (used as cache key)
        
    Returns:
        DataFrame with inflation data
    """
    if not csv_path.exists():
        return pd.DataFrame()
    
    try:
        df = pd.read_csv(csv_path)
        df['Date'] = pd.to_datetime(df['Date'])
        return df
    except Exception as e:
        st.error(f"Error loading data: {e}")
        return pd.DataFrame()


def load_inflation_data(csv_path: Path) -> pd.DataFrame:
    """
    Load inflation data from CSV file.
    Wrapper that gets file modification time for caching.
    
    Args:
        csv_path: Path to the CSV file
        
    Returns:
        DataFrame with inflation data
    """
    if not csv_path.exists():
        return pd.DataFrame()
    
    file_mtime = csv_path.stat().st_mtime
    return load_inflation_data_cached(csv_path, file_mtime)


@st.cache_data
def load_forecast_data_cached(csv_path: Path, file_mtime: float) -> pd.DataFrame:
    """
    Load forecast data from CSV file with caching.
    
    Cache key includes file modification time, so cache invalidates when file is updated.
    This ensures efficient loading without unnecessary file reads.
    
    Args:
        csv_path: Path to the future_data.csv file
        file_mtime: File modification time (used as cache key)
        
    Returns:
        DataFrame with forecast data (Date, Series, Baseline, Upper, Lower)
    """
    if not csv_path.exists():
        return pd.DataFrame()
    
    try:
        df = pd.read_csv(csv_path)
        df['Date'] = pd.to_datetime(df['Date'])
        return df
    except Exception as e:
        return pd.DataFrame()


def load_forecast_data(csv_path: Path) -> pd.DataFrame:
    """
    Load forecast data from CSV file.
    Wrapper that gets file modification time for caching.
    
    Args:
        csv_path: Path to the future_data.csv file
        
    Returns:
        DataFrame with forecast data (Date, Series, Baseline, Upper, Lower)
    """
    if not csv_path.exists():
        return pd.DataFrame()
    
    file_mtime = csv_path.stat().st_mtime
    return load_forecast_data_cached(csv_path, file_mtime)


@st.cache_data
def generate_forecast_data_cached(
    df: pd.DataFrame,
    horizon: int,
    output_path: Path,
    inflation_data_mtime: float
) -> pd.DataFrame:
    """
    Cached wrapper for forecast data generation.
    
    This function is decorated with @st.cache_data to ensure heavy model training
    only runs once per inflation_data.csv update, not on every UI interaction.
    
    The cache key includes the inflation_data.csv modification time, so the cache
    will be invalidated when the source data is updated.
    
    Args:
        df: DataFrame with columns ['Date', 'Value', 'Series', 'Source']
        horizon: Number of months to forecast forward
        output_path: Path for output CSV file
        inflation_data_mtime: Modification time of inflation_data.csv (used as cache key)
        
    Returns:
        DataFrame with columns: ['Date', 'Series', 'Baseline', 'Upper', 'Lower']
    """
    return mbp.get_forecast_data(df, horizon=horizon, output_path=output_path)


@st.cache_data
def _process_data_for_dashboard(
    inflation_data_mtime: float,
    future_data_mtime: Optional[float],
    start_date: date,
    end_date: date,
    max_historical_date: Optional[date],
    df: pd.DataFrame,
    future_df: Optional[pd.DataFrame] = None
) -> pd.DataFrame:
    """
    Process the inflation data for dashboard display, including forecast data if end_date is in the future.
    
    Cached based on file modification times and dates to ensure efficient updates when timeslicers change.
    The cache invalidates when source data files are updated.
    
    Args:
        inflation_data_mtime: Modification time of inflation_data.csv (cache key)
        future_data_mtime: Modification time of future_data.csv (cache key, None if not available)
        start_date: Start date for filtering
        end_date: End date for filtering (can be in the future)
        max_historical_date: Maximum date in historical data
        df: Raw inflation data DataFrame
        future_df: Optional DataFrame with forecast data (Date, Series, Baseline, Upper, Lower)
        
    Returns:
        DataFrame with summary statistics by series, including clickable Source links and Confidence Level
    """
    if df.empty:
        return pd.DataFrame()
    
    # Filter historical data by date range
    max_hist_date_ts = pd.Timestamp(max_historical_date) if max_historical_date else df['Date'].max()
    df_filtered = df[(df['Date'] >= pd.Timestamp(start_date)) & (df['Date'] <= max_hist_date_ts)].copy()
    
    if df_filtered.empty:
        return pd.DataFrame()
    
    # Determine if we're using forecast data
    use_forecast = end_date > max_historical_date if max_historical_date else False
    
    # Calculate summary by series
    summary_data = []
    for series in df_filtered['Series'].unique():
        series_data = df_filtered[df_filtered['Series'] == series].sort_values('Date')
        
        if len(series_data) == 0:
            continue
        
        start_value = series_data.iloc[0]['Value']
        start_date_actual = series_data.iloc[0]['Date']
        source = series_data.iloc[0]['Source']
        
        # Determine end value and date
        confidence_level = None
        if use_forecast and future_df is not None and not future_df.empty:
            # Use forecast data for future end date
            series_forecast = future_df[future_df['Series'] == series].copy()
            
            if not series_forecast.empty:
                end_date_ts = pd.Timestamp(end_date)
                
                # Find the forecast row closest to end_date
                series_forecast['date_diff'] = (series_forecast['Date'] - end_date_ts).abs()
                closest_row = series_forecast.loc[series_forecast['date_diff'].idxmin()]
                
                if closest_row['Date'] <= end_date_ts:
                    end_value = closest_row['Baseline']
                    end_date_actual = closest_row['Date']
                    confidence_level = f"{closest_row['Lower']:,.2f} - {closest_row['Upper']:,.2f}"
                else:
                    # Use the first forecast if end_date is before any forecast
                    first_forecast = series_forecast.iloc[0]
                    end_value = first_forecast['Baseline']
                    end_date_actual = first_forecast['Date']
                    confidence_level = f"{first_forecast['Lower']:,.2f} - {first_forecast['Upper']:,.2f}"
            else:
                # No forecast for this series, use historical
                end_value = series_data.iloc[-1]['Value']
                end_date_actual = series_data.iloc[-1]['Date']
        else:
            # Use historical data
            end_value = series_data.iloc[-1]['Value']
            end_date_actual = series_data.iloc[-1]['Date']
        
        # Calculate percentage change
        if pd.notna(start_value) and pd.notna(end_value) and start_value != 0:
            pct_change = ((end_value - start_value) / start_value) * 100
        else:
            pct_change = None
        
        # Store URL for clickable link
        source_url = None
        if source == 'FRED' and series in FRED_SERIES_URLS:
            source_url = FRED_SERIES_URLS[series]
        elif source == 'EIA':
            # Check for specific EIA series URLs
            if series in SERIES_GROUPS.get("Electricity Cost", []):
                source_url = EIA_ELECTRICITY_URL
            elif series == "WTI Crude Oil":
                source_url = EIA_CRUDE_OIL_URL
        
        row_data = {
            'Series': series,
            'Start Date': start_date_actual.strftime('%Y-%m-%d'),
            'End Date': end_date_actual.strftime('%Y-%m-%d'),
            '%Change': f"{pct_change:.2f}%" if pct_change is not None else "N/A",
            'Source': source,
            'Source_URL': source_url
        }
        
        # Add Confidence Level column if using forecast
        if use_forecast:
            row_data['Confidence Level'] = confidence_level if confidence_level else "N/A"
        
        summary_data.append(row_data)
    
    return pd.DataFrame(summary_data)


def _create_line_chart(
    series_data: pd.DataFrame,
    series_name: str,
    start_date: date,
    end_date: date,
    future_df: Optional[pd.DataFrame] = None,
    max_historical_date: Optional[date] = None
) -> go.Figure:
    """
    Create a line chart for a single series with optional forecast overlay.
    
    Args:
        series_data: DataFrame with Date and Value columns for the series
        series_name: Name of the series
        start_date: Start date for filtering
        end_date: End date for filtering
        future_df: Optional DataFrame with forecast data (Date, Series, Baseline, Upper, Lower)
        max_historical_date: Maximum date in historical data
        
    Returns:
        Plotly figure object
    """
    fig = go.Figure()
    
    # Determine if we need to show forecast
    show_forecast = False
    if future_df is not None and not future_df.empty and max_historical_date and end_date > max_historical_date:
        show_forecast = True
    
    # Filter historical data by date range
    if show_forecast:
        # Show all historical data up to max_historical_date
        historical_data = series_data[
            (series_data['Date'] >= pd.Timestamp(start_date)) & 
            (series_data['Date'] <= pd.Timestamp(max_historical_date))
        ].sort_values('Date')
    else:
        # Show only data within selected range
        historical_data = series_data[
            (series_data['Date'] >= pd.Timestamp(start_date)) & 
            (series_data['Date'] <= pd.Timestamp(end_date))
        ].sort_values('Date')
    
    if historical_data.empty and not show_forecast:
        return fig
    
    # Add historical line trace
    if not historical_data.empty:
        fig.add_trace(go.Scatter(
            x=historical_data['Date'],
            y=historical_data['Value'],
            mode='lines',
            name=series_name,
            line=dict(color='#1f77b4', width=2),
            hovertemplate='<b>%{fullData.name}</b><br>Date: %{x}<br>Value: %{y:,.2f}<extra></extra>'
        ))
        
        # Add data labels only for start and end dates of historical data
        if len(historical_data) > 0:
            # Start date label
            start_row = historical_data.iloc[0]
            fig.add_trace(go.Scatter(
                x=[start_row['Date']],
                y=[start_row['Value']],
                mode='markers+text',
                text=[f"{start_row['Value']:,.2f}"],
                textposition='top center',
                marker=dict(size=8, color='#2ca02c'),
                showlegend=False,
                hovertemplate=f"Start: {start_row['Date'].strftime('%Y-%m-%d')}<br>Value: {start_row['Value']:,.2f}<extra></extra>"
            ))
            
            # End date label (only if different from start)
            if len(historical_data) > 1:
                end_row = historical_data.iloc[-1]
                fig.add_trace(go.Scatter(
                    x=[end_row['Date']],
                    y=[end_row['Value']],
                    mode='markers+text',
                    text=[f"{end_row['Value']:,.2f}"],
                    textposition='top center',
                    marker=dict(size=8, color='#d62728'),
                    showlegend=False,
                    hovertemplate=f"End: {end_row['Date'].strftime('%Y-%m-%d')}<br>Value: {end_row['Value']:,.2f}<extra></extra>"
                ))
    
    # Add forecast if needed
    if show_forecast and future_df is not None and not future_df.empty:
        try:
            # Get forecast data for this series
            series_forecast = future_df[future_df['Series'] == series_name].copy()
            
            if not series_forecast.empty:
                # Get last historical value and date for connection
                last_value = historical_data['Value'].iloc[-1]
                last_date = historical_data['Date'].iloc[-1]
                
                # Filter forecast to selected end date
                end_date_ts = pd.Timestamp(end_date)
                series_forecast_filtered = series_forecast[
                    series_forecast['Date'] <= end_date_ts
                ].sort_values('Date')
                
                if not series_forecast_filtered.empty:
                    forecast_dates = series_forecast_filtered['Date'].tolist()
                    baseline = series_forecast_filtered['Baseline'].tolist()
                    upper = series_forecast_filtered['Upper'].tolist()
                    lower = series_forecast_filtered['Lower'].tolist()
                    
                    # Ensure all lists have the same length
                    min_len = min(len(forecast_dates), len(baseline), len(upper), len(lower))
                    if min_len > 0:
                        forecast_dates = forecast_dates[:min_len]
                        baseline = baseline[:min_len]
                        upper = upper[:min_len]
                        lower = lower[:min_len]
                        
                        # Add confidence interval (shaded area)
                        fig.add_trace(go.Scatter(
                            x=forecast_dates + forecast_dates[::-1],
                            y=upper + lower[::-1],
                            fill='toself',
                            fillcolor='rgba(144, 238, 144, 0.3)',
                            line=dict(color='rgba(255,255,255,0)'),
                            name='Risk/Volatility (95% CI)',
                            showlegend=True,
                            hoverinfo='skip'
                        ))
                        
                        # Add baseline forecast (orange dotted line)
                        # Connect to last historical point
                        forecast_x = [last_date] + forecast_dates
                        forecast_y = [last_value] + baseline
                        
                        fig.add_trace(go.Scatter(
                            x=forecast_x,
                            y=forecast_y,
                            mode='lines',
                            name='Projected Trend',
                            line=dict(color='orange', width=2, dash='dot'),
                            hovertemplate='<b>Projected Trend</b><br>Date: %{x}<br>Value: %{y:,.2f}<extra></extra>'
                        ))
        except Exception as e:
            # Log error for debugging but don't break the chart
            pass
    
    # Update layout for clean background
    fig.update_layout(
        title=dict(text=series_name, font=dict(size=12)),
        xaxis=dict(
            title='',
            showgrid=False,
            showline=True,
            linecolor='#e0e0e0'
        ),
        yaxis=dict(
            title='',
            showgrid=False,
            showline=True,
            linecolor='#e0e0e0'
        ),
        plot_bgcolor='white',
        paper_bgcolor='white',
        margin=dict(l=40, r=40, t=40, b=40),
        height=200,
        showlegend=show_forecast
    )
    
    return fig


def _render_series_group(
    group_name: str,
    series_list: List[str],
    df: pd.DataFrame,
    summary_df: pd.DataFrame,
    start_date: date,
    end_date: date,
    key_prefix: str,
    future_df: Optional[pd.DataFrame] = None,
    max_historical_date: Optional[date] = None
):
    """
    Render a single series group with filter, summary table, and charts.
    
    Args:
        group_name: Name of the group (e.g., "Labor Cost")
        series_list: List of series names in this group
        df: Full inflation data DataFrame
        summary_df: Summary DataFrame with calculated statistics
        start_date: Start date for filtering
        end_date: End date for filtering
        key_prefix: Prefix for Streamlit keys to ensure uniqueness
        future_df: Optional DataFrame with forecast data (Date, Series, Baseline, Upper, Lower)
        max_historical_date: Maximum date in historical data
    """
    # Filter to only series that exist in the data
    available_series = [s for s in series_list if s in df['Series'].unique()]
    
    if not available_series:
        return
    
    st.markdown(f"### {group_name}")
    
    # Filter bar for this group
    filter_key = f"{key_prefix}_filter_{group_name}"
    
    # Initialize session state with all available series if not set
    if filter_key not in st.session_state:
        st.session_state[filter_key] = available_series.copy()
    
    # Get current selection from session state, ensuring all are still available
    current_selection = [s for s in st.session_state[filter_key] if s in available_series]
    
    # If no series are selected, default to all available
    if not current_selection:
        current_selection = available_series.copy()
    
    selected_series = st.multiselect(
        f"Select series to display",
        options=available_series,
        default=current_selection,
        key=f"{key_prefix}_multiselect_{group_name}"
    )
    
    # Update session state - if empty, keep all available series
    if selected_series:
        st.session_state[filter_key] = selected_series
    else:
        st.session_state[filter_key] = available_series.copy()
        selected_series = available_series.copy()
    
    if not selected_series:
        st.info(f"No series available for {group_name}.")
        return
    
    # Filter summary and data to selected series
    group_summary = summary_df[summary_df['Series'].isin(selected_series)].copy()
    group_data = df[df['Series'].isin(selected_series)].copy()
    
    if group_summary.empty or group_data.empty:
        return
    
    # Create layout: summary table on left, charts on right
    col_left, col_right = st.columns([1, 2])
    
    with col_left:
        # Display summary table with clickable links in Source column
        # Determine if we need Confidence Level column
        has_confidence = 'Confidence Level' in group_summary.columns
        
        html_table = "<table style='width:100%; border-collapse: collapse;'>"
        html_table += "<thead><tr style='background-color: #f0f0f0;'><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Series</th><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Start Date</th><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>End Date</th><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>%Change</th>"
        
        if has_confidence:
            html_table += "<th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Confidence Level</th>"
        
        html_table += "<th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Source</th></tr></thead><tbody>"
        
        for _, row in group_summary.iterrows():
            source_display = row['Source']
            if pd.notna(row.get('Source_URL')):
                source_display = f"<a href='{row['Source_URL']}' target='_blank' style='color: #1f77b4; text-decoration: underline;'>{row['Source']}</a>"
            
            html_table += f"<tr>"
            html_table += f"<td style='padding: 8px; border: 1px solid #ddd;'>{row['Series']}</td>"
            html_table += f"<td style='padding: 8px; border: 1px solid #ddd;'>{row['Start Date']}</td>"
            html_table += f"<td style='padding: 8px; border: 1px solid #ddd;'>{row['End Date']}</td>"
            html_table += f"<td style='padding: 8px; border: 1px solid #ddd;'>{row['%Change']}</td>"
            
            if has_confidence:
                confidence = row.get('Confidence Level', 'N/A')
                html_table += f"<td style='padding: 8px; border: 1px solid #ddd;'>{confidence}</td>"
            
            html_table += f"<td style='padding: 8px; border: 1px solid #ddd;'>{source_display}</td>"
            html_table += f"</tr>"
        
        html_table += "</tbody></table>"
        st.markdown(html_table, unsafe_allow_html=True)
    
    with col_right:
        # Create charts in small multiple format (2 columns)
        num_series = len(selected_series)
        cols_per_row = 2
        
        # Create rows of charts
        for row_idx in range((num_series + cols_per_row - 1) // cols_per_row):
            chart_cols = st.columns(cols_per_row)
            
            for col_idx in range(cols_per_row):
                series_idx = row_idx * cols_per_row + col_idx
                
                if series_idx < len(selected_series):
                    with chart_cols[col_idx]:
                        series_name = selected_series[series_idx]
                        series_data = group_data[group_data['Series'] == series_name].copy()
                        
                        # Create chart
                        fig = _create_line_chart(
                            series_data,
                            series_name,
                            start_date,
                            end_date,
                            future_df=future_df,
                            max_historical_date=max_historical_date
                        )
                        
                        # Display chart
                        st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_chart_{group_name}_{series_idx}")


def _create_market_indices_dashboard(
    df: pd.DataFrame,
    start_date: date,
    end_date: date,
    future_df: Optional[pd.DataFrame] = None,
    max_historical_date: Optional[date] = None
):
    """
    Create the Market Indices dashboard with grouped sections.
    
    Args:
        df: Inflation data DataFrame
        start_date: Start date for filtering
        end_date: End date for filtering (can be in the future)
        future_df: Optional DataFrame with forecast data (Date, Series, Baseline, Upper, Lower)
        max_historical_date: Maximum date in historical data
    """
    if df.empty:
        st.warning("No data available for dashboard display.")
        return
    
    # Calculate summary statistics (including forecast data if end_date is in future)
    # Use file modification times as cache keys to ensure efficient updates
    inflation_mtime = CSV_FILE.stat().st_mtime if CSV_FILE.exists() else 0.0
    future_mtime = FUTURE_CSV_FILE.stat().st_mtime if FUTURE_CSV_FILE.exists() else None
    
    summary_df = _process_data_for_dashboard(
        inflation_data_mtime=inflation_mtime,
        future_data_mtime=future_mtime,
        start_date=start_date,
        end_date=end_date,
        max_historical_date=max_historical_date,
        df=df,
        future_df=future_df
    )
    
    if summary_df.empty:
        st.warning("No data available for the selected date range.")
        return
    
    # Render each group
    for group_name, series_list in SERIES_GROUPS.items():
        _render_series_group(
            group_name=group_name,
            series_list=series_list,
            df=df,
            summary_df=summary_df,
            start_date=start_date,
            end_date=end_date,
            key_prefix="market_indices",
            future_df=future_df,
            max_historical_date=max_historical_date
        )
        
        # Add spacing between groups
        st.markdown("---")


def check_api_keys() -> Tuple[bool, str]:
    """
    Check if API keys are valid.
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not API_KEYS_FILE.exists():
        return False, "API keys file not found"
    
    try:
        api_keys = mbp.load_api_keys(API_KEYS_FILE)
        fred_valid, eia_valid = mbp.test_api_keys(api_keys)
        
        if not fred_valid or not eia_valid:
            return False, "One or more API keys are invalid or expired"
        
        return True, ""
    except Exception as e:
        return False, str(e)


def render():
    """Render the Market Barometer page."""
    apply_custom_css()
    
    st.markdown('<h1 class="main-header">Market Barometer</h1>', unsafe_allow_html=True)
    
    # Instructions with improved visual appeal
    st.markdown("""
    ### 📋 Instructions
    
    This page provides a real-time view on key market indicies (FRED, EIA), combining historical data with a 24-month forecast to support a quick projection on cost trends.
    Select your start and end dates to explore the data!
    
    **Features:**
    - 🔄Auto-refresh: Data will be automatically refreshed every 15 days.
    - 📊 **Interactive Dashboard**: View market indices organized by cost categories (Labor, Electricity, Natural Gas, Manufacturing, Packaging, Ingredient, Freight)
    - 📈 **Forecasting**: Select future dates to see projected trends (orange dotted line) with confidence intervals (greenshaded area)
    - 🔍 **Filtering**: Customize which series to display in each category
    - 🔗 **Source Links**: Click on "Source" to view original, public data sources
    """)
    
    # Check API keys
    api_keys_valid, api_error = check_api_keys()
    
    # Show upload section only if API keys don't work
    if not api_keys_valid:
        st.warning(f"⚠️ **Prior API Keys expired, please upload new Keys**\n\n{api_error}")
        
        st.markdown("---")
        st.markdown("### 📤 Upload API Keys")
        
        uploaded_file = st.file_uploader(
            "",
            type=['txt'],
            help="Upload your API_Keys.txt file containing FRED and EIA API keys",
            label_visibility="collapsed"
        )
        
        if uploaded_file is not None:
            if st.button("📥 Save API Keys", type="primary"):
                try:
                    # Ensure directory exists
                    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
                    
                    # Save uploaded file
                    with open(API_KEYS_FILE, "wb") as f:
                        f.write(uploaded_file.getbuffer())
                    
                    st.success("✅ API keys file saved successfully!")
                    # Trigger data fetch and forecast generation immediately after API keys are uploaded
                    with st.spinner("🔄 Fetching market data with new API keys..."):
                        try:
                            mbp.main()
                            st.cache_data.clear()  # Clear all caches after data refresh
                            st.success("✅ Market data fetched and forecasts generated successfully!")
                        except Exception as e:
                            st.warning(f"⚠️ Data fetch failed: {e}. You can try again by refreshing the page.")
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"❌ Error saving API keys file: {str(e)}")
    
    # Auto-refresh data if needed (every 15 days)
    # CRITICAL: Only check for auto-refresh if CSV files don't exist or are old
    # This ensures time slicer changes don't trigger data fetching
    # Only refresh if CSV file doesn't exist or is older than 15 days
    if api_keys_valid and CSV_FILE.exists():
        # Only auto-refresh if data is actually old (15+ days)
        if mbp.should_refresh_data(CSV_FILE):
            with st.spinner("🔄 Data is being refreshed automatically (every 15 days)..."):
                try:
                    mbp.auto_refresh_data()  # This calls main() which updates inflation_data.csv and generates forecasts
                    st.cache_data.clear()  # Clear all caches after data refresh
                    st.success("✅ Data refreshed successfully! Forecasts have been regenerated.")
                    st.rerun()
                except Exception as e:
                    st.warning(f"⚠️ Auto-refresh failed: {e}. Using existing data.")
    elif api_keys_valid and not CSV_FILE.exists():
        # CSV doesn't exist - need to fetch data
        with st.spinner("🔄 Fetching initial market data..."):
            try:
                mbp.main()
                st.cache_data.clear()
                st.success("✅ Market data fetched successfully!")
                st.rerun()
            except Exception as e:
                st.warning(f"⚠️ Data fetch failed: {e}. Please check API keys.")
    
    # Load data from CSV
    df = load_inflation_data(CSV_FILE)
    
    if df.empty:
        st.error("❌ No data available. Please ensure inflation_data.csv exists in the data folder.")
        return
    
    # Market Indices Dashboard section
    st.markdown("---")
    st.markdown("### 📊 Market Indices")
    
    # Get date range from data
    min_date = df['Date'].min().date()
    max_historical_date = df['Date'].max().date()
    # Allow 24 months forward for forecasts
    max_date = (pd.Timestamp(max_historical_date) + pd.DateOffset(months=24)).date()
    
    # Initialize session state for date tracking
    # This ensures dashboard only updates after both dates are selected
    if 'market_indices_dates_ready' not in st.session_state:
        st.session_state.market_indices_dates_ready = False
    if 'market_indices_last_start_date' not in st.session_state:
        st.session_state.market_indices_last_start_date = None
    if 'market_indices_last_end_date' not in st.session_state:
        st.session_state.market_indices_last_end_date = None
    
    # Date range selector (timeslicer)
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input(
            "Start Date",
            value=min_date,
            min_value=min_date,
            max_value=max_date,
            key="market_indices_start_date"
        )
    
    with col2:
        end_date = st.date_input(
            "End Date",
            value=max_historical_date,
            min_value=min_date,
            max_value=max_date,
            key="market_indices_end_date"
        )
    
    # Check if dates have changed
    dates_changed = (
        start_date != st.session_state.market_indices_last_start_date or
        end_date != st.session_state.market_indices_last_end_date
    )
    
    # Update session state
    st.session_state.market_indices_last_start_date = start_date
    st.session_state.market_indices_last_end_date = end_date
    
    # Validate that both dates are selected before processing
    if start_date is None or end_date is None:
        st.session_state.market_indices_dates_ready = False
        st.info("📅 Please select both start and end dates to view the dashboard.")
        return
    
    # Validate date range
    if start_date > end_date:
        st.session_state.market_indices_dates_ready = False
        st.error("❌ Start date must be before end date.")
        return
    
    # Mark dates as ready only when both are selected and valid
    if start_date is not None and end_date is not None and start_date <= end_date:
        st.session_state.market_indices_dates_ready = True
    
    # Only proceed with dashboard if dates are ready
    if not st.session_state.market_indices_dates_ready:
        st.info("📅 Please select both start and end dates to view the dashboard.")
        return
    
    # Load forecast data if end date is beyond historical data
    future_df = None
    if end_date > max_historical_date:
        # First check if statsmodels is available
        try:
            import statsmodels
            statsmodels_available = True
        except ImportError:
            statsmodels_available = False
            st.error("""
            ❌ **statsmodels is not installed**
            
            Forecasting requires the `statsmodels` library. Please install it by running:
            
            ```bash
            pip install statsmodels
            ```
            
            After installation, refresh this page and select a future end date again.
            """)
        
        if statsmodels_available:
            # Try to load existing forecast data
            future_df = load_forecast_data(FUTURE_CSV_FILE)
            
            # Check if forecast file needs to be regenerated
            # CRITICAL REQUIREMENT: future_data.csv only updates when inflation_data.csv is updated
            # UI interactions (like changing timeslicers) will NOT trigger regeneration
            # Only regenerate if:
            # 1. Forecast file doesn't exist or is empty, OR
            # 2. inflation_data.csv is newer than future_data.csv (meaning source data was updated)
            should_regenerate = False
            if not FUTURE_CSV_FILE.exists() or future_df.empty:
                # Forecast file doesn't exist or is empty - need to generate
                should_regenerate = True
            elif CSV_FILE.exists() and FUTURE_CSV_FILE.exists():
                inflation_mtime = CSV_FILE.stat().st_mtime
                future_mtime = FUTURE_CSV_FILE.stat().st_mtime
                # Only regenerate if inflation_data.csv is newer than future_data.csv
                # This ensures forecasts only update when source data is updated (every 15 days or after API key upload)
                if inflation_mtime > future_mtime:
                    should_regenerate = True
            
            # Generate forecast data if needed
            # This is cached based on inflation_data.csv modification time
            # So it won't regenerate on every UI interaction - only when source data changes
            if should_regenerate and CSV_FILE.exists():
                with st.spinner("🔄 Generating forecast data (this may take a minute)..."):
                    try:
                        # Get modification time of inflation_data.csv to use as cache key
                        # This ensures cache invalidates when source data is updated
                        inflation_data_mtime = CSV_FILE.stat().st_mtime
                        
                        # Use cached forecast generation function
                        # Cache key includes inflation_data_mtime, so it only regenerates when source data changes
                        future_df = generate_forecast_data_cached(
                            df=df,
                            horizon=24,
                            output_path=FUTURE_CSV_FILE,
                            inflation_data_mtime=inflation_data_mtime
                        )
                        
                        if future_df.empty:
                            st.error("❌ Forecast data generation failed. Please check the console for error messages.")
                        else:
                            st.success("✅ Forecast data generated successfully!")
                            # Reload to ensure we have the latest data
                            future_df = load_forecast_data(FUTURE_CSV_FILE)
                    except Exception as e:
                        st.error(f"❌ Error generating forecasts: {str(e)}")
                        import traceback
                        with st.expander("🔍 Error Details"):
                            st.code(traceback.format_exc())
                        future_df = None
    
    # Create dashboard
    _create_market_indices_dashboard(
        df,
        start_date,
        end_date,
        future_df=future_df,
        max_historical_date=max_historical_date
    )
