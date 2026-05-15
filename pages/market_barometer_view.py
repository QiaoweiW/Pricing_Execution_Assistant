"""
Market Barometer page view.

Sections
--------
1. Paths & processing module          (BASE_DIR, DATA_DIR, CSV_FILE, …, mbp)
2. Configuration                      (FRED_SERIES_URLS, EIA URLs, SERIES_GROUPS)
3. Data loading & caching             (_load_csv_cached, _load_csv,
                                       generate_forecast_data_cached,
                                       _build_summary_table)
4. Chart builders                     (_create_line_chart, _render_series_group,
                                       _create_market_indices_dashboard)
5. API key & data management          (check_api_keys, _render_api_key_upload,
                                       _handle_auto_refresh,
                                       _load_or_generate_forecast)
6. Page section renderers             (_render_instructions,
                                       _render_market_indices_section)
7. Entry point                        (render)

Design notes
------------
Caching strategy
  A single generic @st.cache_data function (_load_csv_cached) serves both the
  inflation CSV and the forecast CSV. The cache key includes the file's mtime so
  the cache auto-invalidates whenever the file changes on disk, without any
  manual cache-busting calls.

  Forecast generation is similarly cached by inflation_data.csv mtime so the
  expensive model-training step runs at most once per source-data update, never
  on plain UI interactions such as changing the date slicers.

Series groups
  SERIES_GROUPS is an ordered dict that controls both the display order and the
  membership of each cost-category section. Changing order or adding a new group
  requires only a single edit here; all rendering code iterates SERIES_GROUPS
  automatically.
"""
import importlib
import importlib.util
import re
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from data_sources import cola_program_tracker_store as _cola_store
from data_sources import fabric_auth as _fabric_auth
from pages.monthly_resin_freight_mover_tracker import (
    render_monthly_resin_freight_mover_tracker,
)
from pages.walmart_fresh_tracker import render_walmart_fresh_tracker
from pages.weekly_and_monthly_butter_tracker import (
    render_weekly_and_monthly_butter_tracker,
)
from utils.ui_helpers import apply_custom_css


# ── 1. Paths & processing module ──────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent

DATA_DIR       = BASE_DIR / "data" / "Market Barometer"
CSV_FILE       = DATA_DIR / "inflation_data.csv"       # historical market data
FUTURE_CSV_FILE = DATA_DIR / "future_data.csv"         # generated 24-month forecast
API_KEYS_FILE  = DATA_DIR / "API_Keys.txt"

# Market Barometer processing module location — loaded LAZILY by
# :func:`_get_mbp` so the ~700-line ``Market_Barometer_Processing.py``
# (with its ``requests``/``urllib3`` import chain and series configs) only
# pays its import cost on the FIRST navigation that actually needs it
# (API-key validation, auto-refresh, or forecast generation).  Eagerly
# loading at module-import time turned out to be a measurable cold-start
# hit on every app boot, even for users who never visit this view.
_PROCESSING_FILE = BASE_DIR / "processing" / "Market_Barometer_Processing.py"


@st.cache_resource(show_spinner=False)
def _get_mbp() -> ModuleType:
    """Return the lazily-loaded ``Market_Barometer_Processing`` module.

    Cached at module level via ``@st.cache_resource`` so the dynamic
    ``importlib.util.spec_from_file_location`` + ``exec_module`` chain
    runs exactly once per Python process, regardless of how many call
    sites reference it.

    Why dynamic load and not a regular ``from processing import …``?
    The ``processing/`` folder is intentionally NOT a Python package
    (no ``__init__.py``); keeping the side-effecting analytics modules
    out of the package hierarchy avoids polluting the import graph for
    pages that don't need them.  This loader bridges that gap.
    """
    spec = importlib.util.spec_from_file_location(
        "Market_Barometer_Processing", str(_PROCESSING_FILE)
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"Could not build import spec for {_PROCESSING_FILE}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── 2. Configuration ──────────────────────────────────────────────────────────

# FRED series → clickable source URLs shown in the summary table.
FRED_SERIES_URLS: Dict[str, str] = {
    "PPI Food Industry":                          "https://fred.stlouisfed.org/series/PCU311311",
    "PPI All Commodities":                        "https://fred.stlouisfed.org/series/PPIACO",
    "PPI Maintenance/Repair Construction":        "https://fred.stlouisfed.org/series/WPUIP2320001",
    "PPI Paperboard":                             "https://fred.stlouisfed.org/series/WPU091411",
    "PPI Plastics Material and Resin Manufacturing": "https://fred.stlouisfed.org/series/PCU325211325211",
    "PPI Chocolate and Confectionery Manufacturing": "https://fred.stlouisfed.org/series/PCU3113531135",
    "Global Price of Cocoa":                      "https://fred.stlouisfed.org/series/PCOCOUSDM",
    "Sugar Beet Sugar Price":                     "https://fred.stlouisfed.org/series/WPU02530702",
    "Avg Hourly Earnings Total Private":          "https://fred.stlouisfed.org/series/CES0500000003",
    "Wages Private Industry":                     "https://fred.stlouisfed.org/series/ECIWAG",
    "Wood Pallets Price":                         "https://fred.stlouisfed.org/series/PCU3219203219205",
    "West Coast Diesel Price":                    "https://fred.stlouisfed.org/series/GASDESWCW",
    "US Diesel Sales Price":                      "https://fred.stlouisfed.org/series/GASDESW",
    "Natural Gas Price (Henry Hub)":              "https://fred.stlouisfed.org/series/MHHNGSP",
}

EIA_ELECTRICITY_URL: str = (
    "https://www.eia.gov/electricity/data/browser/#/topic/5?agg=0,1&geo=vvvvvvvvvvvvo"
    "&linechart=ELEC.SALES.TX-ALL.M~ELEC.SALES.TX-RES.M~ELEC.SALES.TX-COM.M~ELEC.SALES.TX-IND.M"
    "&columnchart=ELEC.SALES.TX-ALL.M~ELEC.SALES.TX-RES.M~ELEC.SALES.TX-COM.M~ELEC.SALES.TX-IND.M"
    "&map=ELEC.SALES.US-ALL.M&freq=M&start=200101&end=201510&ctype=linechart&ltype=pin"
    "&rtype=s&maptype=0&rse=0&pin=&endsec=vg"
)
# EIA series → clickable source URLs. Mirrors FRED_SERIES_URLS so adding a new
# EIA series is a single dict entry. The Electricity Cost group shares one
# dashboard URL across all four state series, handled as a group-level fallback
# in _build_summary_table rather than duplicated four times here.
EIA_SERIES_URLS: Dict[str, str] = {
    "WTI Crude Oil":                                "https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm",
    "West Coast Diesel Price (Except California)": "https://www.eia.gov/dnav/pet/PET_PRI_GND_DCUS_R5XCA_W.htm",
}

# Ordered dict — insertion order determines the section render order on the page.
# Freight and Packaging are listed first for prominence.
SERIES_GROUPS: Dict[str, List[str]] = {
    "Freight Cost": [
        "West Coast Diesel Price",
        "West Coast Diesel Price (Except California)",
        "US Diesel Sales Price",
        "WTI Crude Oil",
        "Wood Pallets Price",
    ],
    "Packaging Cost": [
        "PPI Paperboard",
        "PPI Plastics Material and Resin Manufacturing",
    ],
    "Labor Cost": [
        "Avg Hourly Earnings Total Private",
        "Wages Private Industry",
    ],
    "Electricity Cost": [
        "Electricity Price Industrial - WA",
        "Electricity Price Industrial - OR",
        "Electricity Price Industrial - ID",
        "Electricity Price Industrial - MT",
    ],
    "Natural Gas Cost": [
        "Natural Gas Price (Henry Hub)",
    ],
    "Other Manufacturing Costs": [
        "PPI Food Industry",
        "PPI All Commodities",
        "PPI Maintenance/Repair Construction",
    ],
    "Ingredient Cost": [
        "Global Price of Cocoa",
        "PPI Chocolate and Confectionery Manufacturing",
        "Sugar Beet Sugar Price",
    ],
}


# ── 3. Data loading & caching ─────────────────────────────────────────────────

@st.cache_data
def _load_csv_cached(csv_path: Path, file_mtime: float) -> pd.DataFrame:
    """Load a CSV and parse its 'Date' column. Cached by (path, mtime).

    The *file_mtime* argument is used purely as a cache key — when the file
    changes on disk its mtime changes, which invalidates the cache entry and
    forces a fresh read without any manual cache-busting.
    """
    try:
        df = pd.read_csv(csv_path)
        df["Date"] = pd.to_datetime(df["Date"])
        return df
    except Exception as exc:
        st.error(f"Error loading {csv_path.name}: {exc}")
        return pd.DataFrame()


def _load_csv(csv_path: Path) -> pd.DataFrame:
    """Load a CSV file, returning an empty DataFrame when the file is absent.

    Thin wrapper around _load_csv_cached that supplies the current mtime as the
    cache-invalidation key. All callers use this function — never the cached
    variant directly — so the mtime logic stays in one place.
    """
    if not csv_path.exists():
        return pd.DataFrame()
    return _load_csv_cached(csv_path, csv_path.stat().st_mtime)


@st.cache_data
def generate_forecast_data_cached(
    df: pd.DataFrame,
    horizon: int,
    output_path: Path,
    inflation_data_mtime: float,
) -> pd.DataFrame:
    """Run the forecast pipeline and return results. Cached by inflation data mtime.

    *inflation_data_mtime* is included in the cache key so this expensive
    model-training step re-runs only when inflation_data.csv is updated, never
    on UI interactions such as changing the date range slicers.

    Returns a DataFrame with columns: Date, Series, Baseline, Upper, Lower.
    """
    return _get_mbp().get_forecast_data(df, horizon=horizon, output_path=output_path)


@st.cache_data
def _build_summary_table(
    inflation_data_mtime: float,
    future_data_mtime: Optional[float],
    start_date: date,
    end_date: date,
    max_historical_date: Optional[date],
) -> pd.DataFrame:
    """Compute per-series summary statistics for the selected date range.

    Cached by ``(inflation_data_mtime, future_data_mtime, start_date,
    end_date, max_historical_date)``.  The two source DataFrames are
    re-loaded from disk *inside* this function — they are not parameters —
    so Streamlit's cache key stays a handful of small primitives instead
    of pickling two ~10–50k-row DataFrames on every call.  The on-disk
    reads themselves are cached by :func:`_load_csv_cached` and keyed on
    the same mtimes, so we still hit the disk at most once per change.

    Returns a DataFrame with columns:
      Series | Start Date | End Date | %Change | Source | Source_URL
    and optionally:
      Confidence Level  (only when end_date falls in the forecast horizon)
    """
    df = _load_csv(CSV_FILE)
    if df.empty:
        return pd.DataFrame()

    future_df = (
        _load_csv(FUTURE_CSV_FILE) if future_data_mtime is not None else pd.DataFrame()
    )

    max_hist_ts = pd.Timestamp(max_historical_date) if max_historical_date else df["Date"].max()
    df_filtered = df[
        (df["Date"] >= pd.Timestamp(start_date)) & (df["Date"] <= max_hist_ts)
    ].copy()

    if df_filtered.empty:
        return pd.DataFrame()

    use_forecast = bool(max_historical_date and end_date > max_historical_date)

    rows = []
    for series in df_filtered["Series"].unique():
        series_data = df_filtered[df_filtered["Series"] == series].sort_values("Date")
        if series_data.empty:
            continue

        start_value      = series_data.iloc[0]["Value"]
        start_date_actual = series_data.iloc[0]["Date"]
        source           = series_data.iloc[0]["Source"]
        confidence_level = None

        if use_forecast and future_df is not None and not future_df.empty:
            series_fc = future_df[future_df["Series"] == series].copy()
            if not series_fc.empty:
                end_ts = pd.Timestamp(end_date)
                series_fc["date_diff"] = (series_fc["Date"] - end_ts).abs()
                closest = series_fc.loc[series_fc["date_diff"].idxmin()]

                if closest["Date"] <= end_ts:
                    end_value      = closest["Baseline"]
                    end_date_actual = closest["Date"]
                    confidence_level = f"{closest['Lower']:,.2f} - {closest['Upper']:,.2f}"
                else:
                    first_fc       = series_fc.iloc[0]
                    end_value      = first_fc["Baseline"]
                    end_date_actual = first_fc["Date"]
                    confidence_level = f"{first_fc['Lower']:,.2f} - {first_fc['Upper']:,.2f}"
            else:
                end_value      = series_data.iloc[-1]["Value"]
                end_date_actual = series_data.iloc[-1]["Date"]
        else:
            end_value      = series_data.iloc[-1]["Value"]
            end_date_actual = series_data.iloc[-1]["Date"]

        pct_change = (
            ((end_value - start_value) / start_value) * 100
            if pd.notna(start_value) and pd.notna(end_value) and start_value != 0
            else None
        )

        # Resolve clickable source URL
        source_url = None
        if source == "FRED" and series in FRED_SERIES_URLS:
            source_url = FRED_SERIES_URLS[series]
        elif source == "EIA":
            if series in EIA_SERIES_URLS:
                source_url = EIA_SERIES_URLS[series]
            elif series in SERIES_GROUPS.get("Electricity Cost", []):
                source_url = EIA_ELECTRICITY_URL

        row: dict = {
            "Series":     series,
            "Start Date": start_date_actual.strftime("%Y-%m-%d"),
            "End Date":   end_date_actual.strftime("%Y-%m-%d"),
            "%Change":    f"{pct_change:.2f}%" if pct_change is not None else "N/A",
            "Source":     source,
            "Source_URL": source_url,
        }
        if use_forecast:
            row["Confidence Level"] = confidence_level or "N/A"

        rows.append(row)

    return pd.DataFrame(rows)


# ── 4. Chart builders ─────────────────────────────────────────────────────────

def _create_line_chart(
    series_data: pd.DataFrame,
    series_name: str,
    start_date: date,
    end_date: date,
    future_df: Optional[pd.DataFrame] = None,
    max_historical_date: Optional[date] = None,
) -> go.Figure:
    """Create a compact line chart for a single series with an optional forecast overlay.

    Historical data is shown as a solid blue line with labelled start/end points.
    When end_date extends beyond the historical record the chart also renders:
      • A green shaded band for the 95 % confidence interval.
      • An orange dotted line for the baseline projected trend.
    """
    fig = go.Figure()

    show_forecast = bool(
        future_df is not None
        and not future_df.empty
        and max_historical_date
        and end_date > max_historical_date
    )

    # Historical slice: extend to max_historical_date when forecast is active so
    # the dotted forecast line connects cleanly to the last known data point.
    history_end = pd.Timestamp(max_historical_date if show_forecast else end_date)
    historical_data = series_data[
        (series_data["Date"] >= pd.Timestamp(start_date))
        & (series_data["Date"] <= history_end)
    ].sort_values("Date")

    if historical_data.empty and not show_forecast:
        return fig

    if not historical_data.empty:
        fig.add_trace(go.Scatter(
            x=historical_data["Date"],
            y=historical_data["Value"],
            mode="lines",
            name=series_name,
            line=dict(color="#1f77b4", width=2),
            hovertemplate="<b>%{fullData.name}</b><br>Date: %{x}<br>Value: %{y:,.2f}<extra></extra>",
        ))

        # Label the start point (green) and end point (red) of the historical slice.
        start_row = historical_data.iloc[0]
        fig.add_trace(go.Scatter(
            x=[start_row["Date"]], y=[start_row["Value"]],
            mode="markers+text",
            text=[f"{start_row['Value']:,.2f}"],
            textposition="top center",
            marker=dict(size=8, color="#2ca02c"),
            showlegend=False,
            hovertemplate=f"Start: {start_row['Date'].strftime('%Y-%m-%d')}<br>Value: {start_row['Value']:,.2f}<extra></extra>",
        ))
        if len(historical_data) > 1:
            end_row = historical_data.iloc[-1]
            fig.add_trace(go.Scatter(
                x=[end_row["Date"]], y=[end_row["Value"]],
                mode="markers+text",
                text=[f"{end_row['Value']:,.2f}"],
                textposition="top center",
                marker=dict(size=8, color="#d62728"),
                showlegend=False,
                hovertemplate=f"End: {end_row['Date'].strftime('%Y-%m-%d')}<br>Value: {end_row['Value']:,.2f}<extra></extra>",
            ))

    if show_forecast and future_df is not None and not future_df.empty:
        try:
            series_fc = future_df[future_df["Series"] == series_name].copy()
            series_fc = series_fc[series_fc["Date"] <= pd.Timestamp(end_date)].sort_values("Date")

            if not series_fc.empty:
                dates    = series_fc["Date"].tolist()
                baseline = series_fc["Baseline"].tolist()
                upper    = series_fc["Upper"].tolist()
                lower    = series_fc["Lower"].tolist()
                n = min(len(dates), len(baseline), len(upper), len(lower))

                if n > 0:
                    dates, baseline, upper, lower = dates[:n], baseline[:n], upper[:n], lower[:n]

                    # Shaded 95 % confidence interval
                    fig.add_trace(go.Scatter(
                        x=dates + dates[::-1],
                        y=upper + lower[::-1],
                        fill="toself",
                        fillcolor="rgba(144, 238, 144, 0.3)",
                        line=dict(color="rgba(255,255,255,0)"),
                        name="Risk/Volatility (95% CI)",
                        showlegend=True,
                        hoverinfo="skip",
                    ))

                    # Dotted baseline forecast connected to the last historical point
                    last_val  = historical_data["Value"].iloc[-1]
                    last_date = historical_data["Date"].iloc[-1]
                    fig.add_trace(go.Scatter(
                        x=[last_date] + dates,
                        y=[last_val]  + baseline,
                        mode="lines",
                        name="Projected Trend",
                        line=dict(color="orange", width=2, dash="dot"),
                        hovertemplate="<b>Projected Trend</b><br>Date: %{x}<br>Value: %{y:,.2f}<extra></extra>",
                    ))
        except Exception:
            pass  # chart degrades gracefully — historical data still visible

    fig.update_layout(
        title=dict(text=series_name, font=dict(size=12)),
        xaxis=dict(title="", showgrid=False, showline=True, linecolor="#e0e0e0"),
        yaxis=dict(
            title="", showgrid=False, showline=True, linecolor="#e0e0e0",
            # Anchor every cost-series axis at zero so cross-chart visual scale
            # comparisons stay honest — small relative moves on a high-base
            # series should not look as dramatic as the same delta on a low-base
            # series.
            rangemode="tozero",
        ),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(l=40, r=40, t=40, b=40),
        height=200,
        showlegend=show_forecast,
    )
    return fig


def _slugify(name: str) -> str:
    """Convert a series name to a filename-safe slug.

    Collapses any run of non-alphanumeric characters to a single hyphen so
    downloaded files like ``west-coast-diesel-price-except-california_…csv``
    are portable across operating systems.
    """
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "series"


def _series_csv_bytes(
    df: pd.DataFrame,
    series_name: str,
    start_date: date,
    end_date: date,
) -> bytes:
    """Return UTF-8 CSV bytes for one series, sliced to a date range.

    Historical data only — by design, forecast values are never included so
    speculative numbers can't leak out of the app via the download button.
    Generation is a single pandas slice + ``to_csv`` call (sub-millisecond
    per series), so it's done inline on each render rather than cached;
    cache lookup overhead would exceed the work for these tiny payloads.
    """
    series_df = df[
        (df["Series"] == series_name)
        & (df["Date"] >= pd.Timestamp(start_date))
        & (df["Date"] <= pd.Timestamp(end_date))
    ].sort_values("Date")
    out = series_df[["Date", "Series", "Value", "Source"]].copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    return out.to_csv(index=False).encode("utf-8")


def _render_series_group(
    group_name: str,
    series_list: List[str],
    df: pd.DataFrame,
    summary_df: pd.DataFrame,
    start_date: date,
    end_date: date,
    key_prefix: str,
    future_df: Optional[pd.DataFrame] = None,
    max_historical_date: Optional[date] = None,
) -> None:
    """Render one cost-category section: series filter, summary table, and charts.

    Layout: summary table (1/3 width left) | small-multiple line charts (2/3 right).
    Series missing from the data are silently skipped so the UI degrades cleanly
    when a data refresh hasn't yet populated a new series.
    """
    available_series = [s for s in series_list if s in df["Series"].unique()]
    if not available_series:
        return

    st.markdown(f"### {group_name}")

    # Persist the user's series selection across reruns via session state.
    filter_key = f"{key_prefix}_filter_{group_name}"
    if filter_key not in st.session_state:
        st.session_state[filter_key] = available_series.copy()

    current_selection = [s for s in st.session_state[filter_key] if s in available_series]
    if not current_selection:
        current_selection = available_series.copy()

    selected_series = st.multiselect(
        "Select series to display",
        options=available_series,
        default=current_selection,
        key=f"{key_prefix}_multiselect_{group_name}",
    )

    # Fall back to all available series when the user clears the selection.
    if not selected_series:
        selected_series = available_series.copy()
    st.session_state[filter_key] = selected_series

    group_summary = summary_df[summary_df["Series"].isin(selected_series)].copy()
    group_data    = df[df["Series"].isin(selected_series)].copy()

    if group_summary.empty or group_data.empty:
        return

    col_left, col_right = st.columns([1, 2])

    with col_left:
        has_confidence = "Confidence Level" in group_summary.columns

        # Build an HTML table so the Source column can contain clickable hyperlinks,
        # which st.dataframe does not support natively.
        html = "<table style='width:100%; border-collapse: collapse;'><thead><tr style='background-color:#f0f0f0;'>"
        for header in ["Series", "Start Date", "End Date", "%Change"]:
            html += f"<th style='padding:8px; text-align:left; border:1px solid #ddd;'>{header}</th>"
        if has_confidence:
            html += "<th style='padding:8px; text-align:left; border:1px solid #ddd;'>Confidence Level</th>"
        html += "<th style='padding:8px; text-align:left; border:1px solid #ddd;'>Source</th>"
        html += "</tr></thead><tbody>"

        for _, row in group_summary.iterrows():
            source_cell = (
                f"<a href='{row['Source_URL']}' target='_blank' "
                f"style='color:#1f77b4; text-decoration:underline;'>{row['Source']}</a>"
                if pd.notna(row.get("Source_URL"))
                else row["Source"]
            )
            html += (
                f"<tr>"
                f"<td style='padding:8px; border:1px solid #ddd;'>{row['Series']}</td>"
                f"<td style='padding:8px; border:1px solid #ddd;'>{row['Start Date']}</td>"
                f"<td style='padding:8px; border:1px solid #ddd;'>{row['End Date']}</td>"
                f"<td style='padding:8px; border:1px solid #ddd;'>{row['%Change']}</td>"
            )
            if has_confidence:
                html += f"<td style='padding:8px; border:1px solid #ddd;'>{row.get('Confidence Level', 'N/A')}</td>"
            html += f"<td style='padding:8px; border:1px solid #ddd;'>{source_cell}</td></tr>"

        html += "</tbody></table>"
        st.markdown(html, unsafe_allow_html=True)

    # Cap CSV downloads at the last historical date so users never get
    # speculative forecast values in the exported file.
    hist_end = (
        min(end_date, max_historical_date) if max_historical_date else end_date
    )

    with col_right:
        cols_per_row = 2
        for row_idx in range((len(selected_series) + cols_per_row - 1) // cols_per_row):
            chart_cols = st.columns(cols_per_row)
            for col_idx in range(cols_per_row):
                series_idx = row_idx * cols_per_row + col_idx
                if series_idx >= len(selected_series):
                    break
                with chart_cols[col_idx]:
                    s_name = selected_series[series_idx]
                    fig = _create_line_chart(
                        group_data[group_data["Series"] == s_name].copy(),
                        s_name, start_date, end_date,
                        future_df=future_df,
                        max_historical_date=max_historical_date,
                    )
                    st.plotly_chart(
                        fig, use_container_width=True,
                        key=f"{key_prefix}_chart_{group_name}_{series_idx}",
                    )

                    csv_bytes = _series_csv_bytes(
                        df=group_data,
                        series_name=s_name,
                        start_date=start_date,
                        end_date=hist_end,
                    )
                    st.download_button(
                        label="📥 CSV",
                        data=csv_bytes,
                        file_name=(
                            f"{_slugify(s_name)}_"
                            f"{start_date.isoformat()}_{hist_end.isoformat()}.csv"
                        ),
                        mime="text/csv",
                        key=f"{key_prefix}_dl_{group_name}_{series_idx}",
                        type="tertiary",
                        help="Download the historical data behind this chart (forecast excluded).",
                    )


def _create_market_indices_dashboard(
    df: pd.DataFrame,
    start_date: date,
    end_date: date,
    future_df: Optional[pd.DataFrame] = None,
    max_historical_date: Optional[date] = None,
) -> None:
    """Render the full Market Indices dashboard: one section per entry in SERIES_GROUPS.

    Computes the per-series summary table once (cached) then delegates rendering
    of each group to _render_series_group. Groups are separated by horizontal rules.
    """
    if df.empty:
        st.warning("No data available for dashboard display.")
        return

    inflation_mtime = CSV_FILE.stat().st_mtime if CSV_FILE.exists() else 0.0
    future_mtime    = FUTURE_CSV_FILE.stat().st_mtime if FUTURE_CSV_FILE.exists() else None

    # NOTE: We intentionally pass only the file mtimes + date window — not
    # ``df`` / ``future_df`` themselves — so the cache key stays cheap to
    # hash.  ``_build_summary_table`` re-loads the underlying CSVs through
    # the mtime-keyed ``_load_csv`` cache, giving us the same data with a
    # small-primitives-only cache key.
    summary_df = _build_summary_table(
        inflation_data_mtime=inflation_mtime,
        future_data_mtime=future_mtime,
        start_date=start_date,
        end_date=end_date,
        max_historical_date=max_historical_date,
    )

    if summary_df.empty:
        st.warning("No data available for the selected date range.")
        return

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
            max_historical_date=max_historical_date,
        )
        st.markdown("---")


# ── 5. API key & data management ──────────────────────────────────────────────

# How long a successful API-key validation is trusted before we re-test
# against FRED + EIA.  The keys realistically don't expire mid-session, so
# 15 minutes is plenty short to surface a rotated key on the next visit
# yet long enough to spare every page render the two synchronous HTTP
# round-trips ``test_api_keys`` performs.
_API_KEY_CHECK_TTL_SECONDS: int = 15 * 60


@st.cache_data(ttl=_API_KEY_CHECK_TTL_SECONDS, show_spinner=False)
def _check_api_keys_cached(
    keys_file_mtime: float,
    keys_file_size: int,
) -> Tuple[bool, str]:
    """Cached wrapper around the FRED + EIA API-key probes.

    Cache key is ``(mtime, size)`` of ``API_KEYS_FILE`` so a freshly
    uploaded keys file blows the cache automatically on the next call.
    Returns the same ``(is_valid, error_message)`` tuple as the public
    :func:`check_api_keys`.

    NOTE: Streamlit caches ONLY successful return values, but it also
    caches non-exception returns — including invalid-key tuples like
    ``(False, "One or more API keys are invalid or expired")``.  That's
    exactly what we want: a transient FRED 5xx still hits the network
    next time (because the function raised), while a steady-state valid
    key stays cached.
    """
    mbp = _get_mbp()
    api_keys = mbp.load_api_keys(API_KEYS_FILE)
    fred_valid, eia_valid = mbp.test_api_keys(api_keys)
    if not fred_valid or not eia_valid:
        return False, "One or more API keys are invalid or expired"
    return True, ""


def check_api_keys() -> Tuple[bool, str]:
    """Test whether the stored FRED and EIA API keys are valid.

    Returns ``(is_valid, error_message)`` — ``error_message`` is the
    empty string when valid.

    Cached for :data:`_API_KEY_CHECK_TTL_SECONDS` keyed on the API key
    file's mtime + size, so the two synchronous HTTP probes that back
    this check don't fire on every page render.
    """
    if not API_KEYS_FILE.exists():
        return False, "API keys file not found"

    try:
        stat = API_KEYS_FILE.stat()
        return _check_api_keys_cached(stat.st_mtime, stat.st_size)
    except Exception as exc:  # noqa: BLE001 — surface as a user-friendly banner
        return False, str(exc)


def _invalidate_api_keys_cache() -> None:
    """Drop the cached API-key validation result.

    Wired to the "Save API Keys" upload flow and to the future "Re-test
    API keys" button so a user-triggered action immediately reflects the
    new key state.
    """
    _check_api_keys_cached.clear()


def _render_api_key_upload() -> None:
    """Render the API key upload widget.

    On successful save, immediately fetches fresh market data and reruns the page.
    Isolated here so render() stays focused on page layout.
    """
    st.markdown("---")
    st.markdown("### 📤 Upload API Keys")

    uploaded_file = st.file_uploader(
        "",
        type=["txt"],
        help="Upload your API_Keys.txt file containing FRED and EIA API keys",
        label_visibility="collapsed",
    )

    if uploaded_file is not None and st.button("📥 Save API Keys", type="primary"):
        try:
            API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
            API_KEYS_FILE.write_bytes(uploaded_file.getbuffer())
            # Drop the cached "keys are invalid" answer so the next render
            # tests the freshly-saved keys against FRED + EIA immediately.
            _invalidate_api_keys_cache()
            st.success("✅ API keys file saved successfully!")

            with st.spinner("🔄 Fetching market data with new API keys..."):
                try:
                    _get_mbp().main()
                    st.cache_data.clear()
                    st.success("✅ Market data fetched and forecasts generated successfully!")
                except Exception as exc:
                    st.warning(f"⚠️ Data fetch failed: {exc}. You can try again by refreshing the page.")
            st.rerun()
        except Exception as exc:
            st.error(f"❌ Error saving API keys file: {exc}")


def _handle_auto_refresh() -> None:
    """Trigger an automatic data refresh when needed.

    Refreshes when the CSV is stale (older than 15 days) and fetches from scratch
    when the file is absent. Clears all Streamlit caches after a successful update
    and reruns the page so the new data is immediately visible.
    """
    mbp = _get_mbp()
    if CSV_FILE.exists():
        if not mbp.should_refresh_data(CSV_FILE):
            return  # data is fresh — nothing to do
        with st.spinner("🔄 Data is being refreshed automatically (every 15 days)..."):
            try:
                mbp.auto_refresh_data()
                st.cache_data.clear()
                st.success("✅ Data refreshed successfully! Forecasts have been regenerated.")
                st.rerun()
            except Exception as exc:
                st.warning(f"⚠️ Auto-refresh failed: {exc}. Using existing data.")
    else:
        with st.spinner("🔄 Fetching initial market data..."):
            try:
                mbp.main()
                st.cache_data.clear()
                st.success("✅ Market data fetched successfully!")
                st.rerun()
            except Exception as exc:
                st.warning(f"⚠️ Data fetch failed: {exc}. Please check API keys.")


def _load_or_generate_forecast(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Return a valid forecast DataFrame, generating it if necessary.

    Regenerates only when inflation_data.csv is newer than future_data.csv, or
    when future_data.csv is missing/empty — never on plain UI interactions.
    Returns None when statsmodels is unavailable or generation fails.
    """
    try:
        import statsmodels  # noqa: F401 — presence check only
    except ImportError:
        st.error(
            "❌ **statsmodels is not installed**\n\n"
            "Forecasting requires the `statsmodels` library. Install it with:\n\n"
            "```bash\npip install statsmodels\n```\n\n"
            "After installation, refresh this page and select a future end date again."
        )
        return None

    future_df = _load_csv(FUTURE_CSV_FILE)

    should_regenerate = (
        not FUTURE_CSV_FILE.exists()
        or future_df.empty
        or (CSV_FILE.exists() and CSV_FILE.stat().st_mtime > FUTURE_CSV_FILE.stat().st_mtime)
    )

    if not should_regenerate:
        return future_df if not future_df.empty else None

    with st.spinner("🔄 Generating forecast data (this may take a minute)..."):
        try:
            future_df = generate_forecast_data_cached(
                df=df,
                horizon=24,
                output_path=FUTURE_CSV_FILE,
                inflation_data_mtime=CSV_FILE.stat().st_mtime,
            )
            if future_df.empty:
                st.error("❌ Forecast data generation failed. Please check the console for details.")
                return None
            st.success("✅ Forecast data generated successfully!")
            return _load_csv(FUTURE_CSV_FILE)  # reload from disk for cache consistency
        except Exception as exc:
            st.error(f"❌ Error generating forecasts: {exc}")
            with st.expander("🔍 Error Details"):
                st.code(traceback.format_exc())
            return None


# ── 6. Page section renderers ─────────────────────────────────────────────────

def _render_instructions() -> None:
    """Render the static instructions block at the top of the page."""
    st.markdown("""
### 📋 Instructions

This page tracks **a)** monthly resin and freight fluctuations, **b)** Walmart
Fresh movers, and **c)** real-time market indices from FRED and EIA (a 24-month
statistical forecast is enabled for all key indices).
""")


# ── Annual COLA Movers section ────────────────────────────────────────────────
#
# OneLake-backed editable table.  The DataFrame is loaded on every render
# from ``Files/Monthly_Pricing_Execution/COLA_Program_Tracker.csv`` (via
# ``cola_program_tracker_store``); the user can freely add / edit / remove
# rows in ``st.data_editor`` and click **Refresh** to push the working
# copy back to OneLake (authoritative overwrite).  A second button
# downloads the live working copy as a CSV.
#
# Why session-state keyed by editor key
# -------------------------------------
# Streamlit's data_editor returns the edited frame on every rerun.  The
# Refresh handler needs to publish the LATEST frame, including in-flight
# edits the user just typed before clicking the button — which means
# reading the editor's value out of session state in the same render.
# The editor key is module-private so other pages (or future re-uses of
# this view) can't accidentally collide.

# Editor widget key — also the session-state slot the Refresh handler
# reads to publish the live working copy.
_COLA_EDITOR_KEY: str = "market_barometer_cola_editor"

# Friendly text shown in the empty state.  Kept short — when the blob
# is genuinely empty the editor itself is the call-to-action.
_COLA_EMPTY_HINT: str = (
    "No rows yet.  Add rows in the editor below and click 🔄 Refresh "
    "to publish the table to OneLake."
)


def _normalise_cola_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Best-effort normalisation of the COLA table for the editor.

    The COLA workbook schema is owned by the Pricing team and may
    evolve over time, so we deliberately do NOT enforce a fixed
    column set — whatever the file holds is shown verbatim.  This
    helper exists only to make the editor experience pleasant:

    * Strip leading / trailing whitespace from column headers so the
      editor's column resize / sort widgets aren't confused by stray
      spaces operators added in Excel.
    * Detect a column-set that is auto-numeric ``RangeIndex(0..N)``
      AND whose first row is plausibly the *real* header (every cell
      a non-numeric string).  This shape arises when an upstream
      writer accidentally serialised a frame without headers —
      ``read_csv`` then promotes row 0 to the header.  Promoting it
      back here keeps the editor (and the next ``replace_table``
      write) from compounding the corruption.

    Returns a copy; the input is never mutated.
    """
    if df is None or df.empty:
        return df.copy() if df is not None else pd.DataFrame()
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]

    # Recover from "headers became first data row" corruption.  We
    # ONLY do this when ALL of the following hold (so we never
    # accidentally drop a legitimate first data row):
    #   • columns are exactly the integer strings "0".."N-1" (the
    #     telltale of a write without headers)
    #   • the first row's cells are ALL non-empty strings
    #   • none of those first-row cells parses as a number (so it's
    #     genuinely header-shaped, not numeric data)
    n_cols = len(out.columns)
    expected_numeric_cols = [str(i) for i in range(n_cols)]
    if list(out.columns) == expected_numeric_cols and len(out) > 0:
        first_row = out.iloc[0]
        all_string_headers = all(
            isinstance(v, str) and v.strip() for v in first_row
        )
        any_numeric = any(
            isinstance(v, (int, float)) and not isinstance(v, bool)
            for v in first_row
        )
        if all_string_headers and not any_numeric:
            new_columns = [str(v).strip() for v in first_row]
            out = out.iloc[1:].reset_index(drop=True)
            out.columns = pd.Index(new_columns)

    return out


def _render_annual_cola_movers_section() -> None:
    """Render the **Annual COLA Movers** collapsible section.

    Sits directly beneath **Monthly Milk, Resin & Freight Movers** and
    hosts the annual cost-of-living-adjustment (COLA) workflow.

    Layout
    ------
    1. Short caption that names the underlying lakehouse blob.
    2. Editable table (``st.data_editor`` with ``num_rows="dynamic"``)
       backed by ``cola_program_tracker_store``.  Any column may be
       edited; rows may be added or deleted.
    3. A button column on the right with:
         * **🔄 Refresh** — overwrite the OneLake blob with the live
           editor contents, then re-read so the editor immediately
           reflects what was published.
         * **⬇️ Download CSV** — download whatever the editor currently
           shows (including unsaved in-flight edits).

    Kept collapsed by default so first page render stays fast — the
    OneLake read fires only when the user opens this section.

    Failure mode
    ------------
    Read failure (auth / network / corrupt blob) renders an error
    caption inside the expander but never raises, so a transient
    Fabric outage cannot break the rest of the Market Barometer view.
    """
    with st.expander("📅 Annual COLA Movers", expanded=False):
        try:
            current = _normalise_cola_frame(_cola_store.read_table())
            read_error: Optional[str] = None
        except _cola_store.ColaProgramTrackerStoreError as exc:
            current = pd.DataFrame()
            read_error = str(exc)

        st.caption(
            f"📡 Sourced from **{_cola_store.get_blob_label()}** in the Pricing "
            "Lakehouse — edits are persisted only when you click **Refresh**."
        )
        if read_error is not None:
            # Render the precise error text inline so a user with a
            # secrets-config issue still sees the actionable detail,
            # then offer a one-click "Retry connection" path.  The
            # retry sweeps every cache the auth chain consults
            # (process-wide failure cache + per-session bypass flag +
            # the @st.cache_data result) so the next render gets a
            # genuinely fresh OneLake attempt.  Most often this is the
            # only fix needed when the user signed in AFTER an earlier
            # render had already cached a failure.
            st.error(
                "❌ Could not load the COLA Program Tracker from OneLake. "
                "Edits below will not be persisted until the connection is "
                f"restored.\n\nUnderlying error: {read_error}"
            )
            if st.button(
                "🔁 Retry connection",
                key="market_barometer_cola_retry",
                type="primary",
                help=(
                    "Drop the cached auth failure and re-attempt the "
                    "OneLake read.  Use after signing in to Microsoft "
                    "Fabric (e.g. via `az login`) to pick up the new "
                    "credential without reloading the page."
                ),
            ):
                try:
                    _fabric_auth.reset_auth_failure_cache()
                except Exception:  # noqa: BLE001 — best-effort recovery
                    pass
                _cola_store.invalidate_read_cache()
                st.rerun()

        col_table, col_btn = st.columns([5, 1])
        with col_table:
            if current.empty and read_error is None:
                st.info(_COLA_EMPTY_HINT)
            edited = st.data_editor(
                current,
                num_rows="dynamic",
                use_container_width=True,
                key=_COLA_EDITOR_KEY,
                hide_index=True,
            )

        with col_btn:
            # Top-align the buttons with the editor's first row to avoid
            # a visually-floating Refresh button on tall tables.
            st.markdown("<div style='margin-top: 2.2rem'></div>", unsafe_allow_html=True)
            refresh_clicked = st.button(
                "🔄 Refresh",
                type="primary",
                use_container_width=True,
                key="market_barometer_cola_refresh",
                help=(
                    "Overwrite the OneLake `COLA_Program_Tracker.csv` "
                    "with the table contents currently shown above."
                ),
                disabled=read_error is not None,
            )
            st.download_button(
                label="⬇️ Download CSV",
                data=edited.to_csv(index=False).encode("utf-8"),
                file_name=(
                    f"COLA_Program_Tracker_{datetime.now():%Y%m%d}.csv"
                ),
                mime="text/csv",
                use_container_width=True,
                key="market_barometer_cola_download",
                help="Download the live editor contents as a CSV.",
            )

        if refresh_clicked:
            with st.spinner("Publishing COLA Program Tracker to OneLake…"):
                try:
                    _cola_store.replace_table(edited)
                except _cola_store.ColaProgramTrackerStoreError as exc:
                    st.error(f"❌ OneLake write failed: {exc}")
                else:
                    rows = len(edited)
                    cols = len(edited.columns)
                    st.success(
                        f"✅ Published {rows} row(s) × {cols} column(s) to "
                        f"{_cola_store.get_blob_label()}."
                    )
                    # Force a fresh render of the editor with the just-
                    # published bytes so a subsequent Refresh click sees
                    # the canonical OneLake state, not a stale local copy.
                    st.rerun()


def _render_market_indices_section(df: pd.DataFrame) -> None:
    """Render the collapsible Market Indices dashboard with date range controls.

    Validates the selected dates before proceeding; invalid ranges display an
    inline error and abort early. Forecast data is loaded (or generated) only
    when the end date extends beyond the historical record.
    """
    with st.expander("📊 Market Indices", expanded=True):
        # Feature overview for this section only — keeps the top-of-page
        # instructions concise while preserving full context next to the
        # dashboard controls.
        st.markdown("""
**Features**
- 🔄 **Auto-refresh**: Data is automatically refreshed every 15 days.
- 📊 **Interactive dashboard**: Market indices organised by cost category
  (Freight, Packaging, Labor, Electricity, Natural Gas, Manufacturing, Ingredient).
- 📈 **Forecasting** *(probabilistic — should NOT be used to set direction)*:
  Select a future end date to see projected trends (orange dotted line) with
  confidence intervals (green shaded area).
- 🔍 **Filtering**: Customise which series appear in each category.
- 🔗 **Source links**: Click "Source" to view the original public data source.
""")
        st.markdown("---")

        min_date            = df["Date"].min().date()
        max_historical_date = df["Date"].max().date()
        # Allow the end-date picker to reach 24 months into the future for forecasting.
        max_date = (pd.Timestamp(max_historical_date) + pd.DateOffset(months=24)).date()

        # Default start date: 1 year ago from today, clamped to the earliest data point.
        default_start = max(min_date, date.today() - timedelta(days=365))

        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input(
                "Start Date",
                value=default_start,
                min_value=min_date,
                max_value=max_date,
                key="market_indices_start_date",
            )
        with col2:
            end_date = st.date_input(
                "End Date",
                value=max_historical_date,
                min_value=min_date,
                max_value=max_date,
                key="market_indices_end_date",
            )

        if start_date is None or end_date is None:
            st.info("📅 Please select both start and end dates to view the dashboard.")
            return

        if start_date > end_date:
            st.error("❌ Start date must be before end date.")
            return

        future_df = (
            _load_or_generate_forecast(df)
            if end_date > max_historical_date
            else None
        )

        _create_market_indices_dashboard(
            df, start_date, end_date,
            future_df=future_df,
            max_historical_date=max_historical_date,
        )


# ── 7. Entry point ────────────────────────────────────────────────────────────

def render() -> None:
    """Render the Market Barometer page.

    Flow
    ----
    1. Instructions
    2. Monthly Milk, Resin & Freight Movers (collapsible, collapsed by default)
    3. API key check → upload widget (if invalid) or auto-refresh (if valid)
    4. Load inflation data — gate on non-empty before proceeding
    5. Walmart Fresh Tracker (collapsible, collapsed by default)
    6. Market Indices dashboard (collapsible, expanded by default)
    """
    apply_custom_css()
    st.markdown('<h1 class="main-header">Market Barometer</h1>', unsafe_allow_html=True)

    _render_instructions()

    # Divider separates the instructions from the collapsible trackers below.
    st.markdown("---")

    # Weekly & Monthly Butter Movers — surfaces two charts (CME weekly
    # average, USDA dairy products weighted price) inside its own
    # foldable expander. Renders BEFORE Monthly Milk/Resin/Freight
    # Movers per the May-2026 spec; a thin divider keeps the two
    # collapsibles visually distinct when both are collapsed.
    render_weekly_and_monthly_butter_tracker()
    st.markdown("---")

    # Monthly Milk, Resin & Freight Movers is a fully self-contained
    # @st.fragment — no data or state is shared with the rest of this view,
    # so uploads / edits here do not trigger reruns elsewhere.
    with st.expander("📦 Monthly Milk, Resin & Freight Movers", expanded=False):
        render_monthly_resin_freight_mover_tracker()

    # Visual divider between the two foldable mover trackers so the page
    # still reads as two distinct collapsible sections even when both are
    # closed (an expander alone leaves no visible separator).
    st.markdown("---")

    # Placeholder home for the Annual COLA (cost-of-living-adjustment) Movers
    # tracker.  Lives under its own collapsible section so the section can
    # grow into a fully-fledged tracker without disturbing the surrounding
    # page layout.
    _render_annual_cola_movers_section()

    # API key management: show upload widget if keys are missing/invalid,
    # otherwise check whether a periodic auto-refresh is due.
    api_keys_valid, api_error = check_api_keys()
    if not api_keys_valid:
        st.warning(f"⚠️ **Prior API Keys expired, please upload new Keys**\n\n{api_error}")
        _render_api_key_upload()
    else:
        _handle_auto_refresh()

    # All sections below require data — bail early with a clear message if absent.
    df = _load_csv(CSV_FILE)
    if df.empty:
        st.error("❌ No data available. Please ensure inflation_data.csv exists in the data folder.")
        return

    st.markdown("---")

    with st.expander("🛒 Walmart Fresh Tracker", expanded=False):
        render_walmart_fresh_tracker()

    st.markdown("---")

    _render_market_indices_section(df)
