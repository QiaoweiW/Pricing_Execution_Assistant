"""
Walmart Fresh Tracker — data pipeline and UI for Walmart HTST monthly files.

Sections
--------
1. Constants        (_STORES, _TARGET_DESC, column-name constants,
                     chart-color constants, _CHART_FONT, _LOOKUP_COLS,
                     _SHAREPOINT_URL)
2. Data helpers     (_to_csv_bytes, _parse_month_dt, _clean_invalid_values,
                     _resolve_adj_resin_col, _is_caseless, _gal_multiplier,
                     _detect_files, _build_mover_extract,
                     _build_enriched_mover_extract)
3. Chart primitives (_agg_by_month, _pct_change, _ts_layout, _wf_layout,
                     _build_scenario_change_table)
4. Chart builders   (_build_fuel_ts_chart, _build_resin_ts_chart,
                     _build_waterfall_chart)
5. UI sections      (_render_upload_section, _render_controls,
                     _render_chart_section, _render_downloads)
6. Public API       (render_walmart_fresh_tracker)

Design notes
------------
Upload
  A single multi-file uploader accepts both CSVs at once. Files are matched to
  their roles by case-insensitive keyword search on the filename
  ('mover' → Mover Analysis, 'mapping' → Mapping).

Processing cache
  An MD5 of (name, size) for each file is stored in session_state.
  The pipeline re-runs only when the file set changes, not on every widget
  interaction.

Waterfall structure (go.Bar with explicit base positioning):
  Bar 0 (first month) : Absolute baseline 0 → V[0], blue.
  Bars 1..n-2         : MoM delta stacked on running total, green ↑ / red ↓.
  Bar n-1 (last month): Final MoM delta, black (marks endpoint).
  Connector lines     : Dotted horizontals bridge adjacent bars.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


# ── 1. Constants ──────────────────────────────────────────────────────────────

# Stores (Store/Club values) and product targeted by the filter.
_STORES: list[str] = ["2070", "2385"]
_TARGET_DESC: str  = "2% Milk"

# Canonical column names for the renamed / derived change-from-base columns.
_COL_FUEL_EA   = "Fuel Change from Base $/EA"
_COL_FUEL_GAL  = "Fuel Change from Base $/Gal"
_COL_RESIN_EA  = "Resin Change from Base $/EA"
_COL_RESIN_GAL = "Resin Change from Base $/Gal"

# Columns pulled from the Mapping file and appended during enrichment.
_LOOKUP_COLS: list[str] = ["Item Code", "Item Description", "ShiptoName"]

# Waterfall bar palette — single source of truth for both waterfall charts.
_C_BASELINE = "#5c85d6"   # blue  : first (absolute baseline) bar
_C_UP       = "#2e7d32"   # green : positive MoM delta bars
_C_DOWN     = "#c62828"   # red   : negative MoM delta bars
_C_LAST     = "#212121"   # black : final bar (marks series endpoint)

# Shared Plotly font spec — consistent with other pages in the app.
_CHART_FONT = dict(family="Segoe UI, Tahoma, Geneva, Verdana, sans-serif", size=13)

# SharePoint folder where the source CSV files are maintained.
_SHAREPOINT_URL = (
    "https://darigold1com.sharepoint.com/sites/BrandedPricing/Shared%20Documents"
    "/Forms/AllItems.aspx?id=%2Fsites%2FBrandedPricing%2FShared%20Documents"
    "%2FGeneral%2F02%20Resources%2FStreamlit%20Folders%20%28DO%20NOT%20DELETE"
    "%29%2FWalmart%20Fresh&viewid=9103ebc3%2Df944%2D4451%2Dbe05%2Dd0cb7479e27e"
)

# Session-state key prefix — avoids collisions with other pages.
_SS_PREFIX = "_wft"


# ── 2. Data helpers ───────────────────────────────────────────────────────────

def _to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Serialise *df* to UTF-8 CSV bytes for st.download_button."""
    return df.to_csv(index=False).encode("utf-8")


def _parse_month_dt(series: pd.Series) -> pd.Series:
    """Parse a Month column to tz-naive datetime64, returning NaT on failure.

    Used for chronological sorting and time-range filtering only; the original
    string representation is preserved in the DataFrame for display labels.
    """
    return pd.to_datetime(series, errors="coerce")


def _clean_invalid_values(df: pd.DataFrame) -> pd.DataFrame:
    """Replace Excel error strings (any cell starting with '#') with 0.

    Covers #DIV/0!, #VALUE!, #REF!, etc. Numeric coercion of affected columns
    happens downstream so aggregations operate on float, not the string '0'.
    """
    return df.replace(r"^#.*$", "0", regex=True)


def _resolve_adj_resin_col(columns: pd.Index) -> Optional[str]:
    """Find the Adjusted Resin column regardless of spacing variants.

    Matches any column whose lowercase name contains both 'adj resin' and 'nma',
    handling both 'Adj Resin W/ 0.365 NMA' and 'Adj Resin W/0.365 NMA'.
    Returns None when no matching column is found.
    """
    for col in columns:
        low = col.lower()
        if "adj resin" in low and "nma" in low:
            return col
    return None


def _is_caseless(plant_city: str) -> bool:
    """Return True when Plant_City indicates a caseless-delivery plant."""
    return "caseless" in str(plant_city).lower()


def _gal_multiplier(size: str) -> float:
    """Return the $/EA → $/Gal conversion factor based on product Size.

    Half Gallon: 1 gallon = 2 half-gallon units  →  factor = 2.0
    Gallon (and all other sizes): already per-gallon  →  factor = 1.0
    """
    return 2.0 if "half" in str(size).strip().lower() else 1.0


def _detect_files(
    uploaded_files: list,
) -> tuple[Optional[object], Optional[object]]:
    """Match uploaded files to Mover Analysis and Mapping roles by filename keyword.

    Scans each filename (case-insensitive) for 'mover' or 'mapping'.
    The first matching file claims each role; later matches are ignored.
    Returns (mover_file, mapping_file) — either may be None if not yet uploaded.
    """
    mover_file = mapping_file = None
    for f in (uploaded_files or []):
        name = f.name.lower()
        if mover_file is None and "mover" in name:
            mover_file = f
        elif mapping_file is None and "mapping" in name:
            mapping_file = f
    return mover_file, mapping_file


def _build_mover_extract(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Build the Mover Extract from the raw Walmart Mover Analysis CSV.

    Pipeline
    --------
    1. Strip column-name whitespace; replace Excel error strings with 0.
    2. Cast Store/Club to string and filter to _STORES WHERE Description == _TARGET_DESC.
    3. Create "Scenario": "{Store City} - {Description} - {Size} - {Caseless/Non-Caseless}".
    4. Rename original change columns to canonical $/EA names.
    5. Coerce $/EA columns to float, then derive $/Gal siblings via _gal_multiplier().

    Raises ValueError when a required column is absent.
    """
    df = raw_df.copy()
    df.columns = df.columns.str.strip()
    df = _clean_invalid_values(df)

    for col in ("Store/Club", "Description"):
        if col not in df.columns:
            raise ValueError(f"Required column '{col}' not found in Mover Analysis CSV.")

    df["Store/Club"] = df["Store/Club"].astype(str).str.strip()
    mask = (
        df["Store/Club"].isin(_STORES)
        & (df["Description"].astype(str).str.strip() == _TARGET_DESC)
    )
    df = df[mask].copy().reset_index(drop=True)

    if df.empty:
        st.warning(
            f"No rows found for Store/Club {_STORES} with "
            f"Description '{_TARGET_DESC}'. Check your input file."
        )
        return df

    plant_col = "Plant_City"
    caseless_label = (
        df[plant_col].apply(_is_caseless).map({True: "Caseless", False: "Non-Caseless"})
        if plant_col in df.columns
        else pd.Series("Non-Caseless", index=df.index)
    )
    city_part = (
        df["Store City"].astype(str).str.strip()
        if "Store City" in df.columns
        else df["Store/Club"].astype(str)
    )
    size_part = (
        df["Size"].astype(str).str.strip()
        if "Size" in df.columns
        else pd.Series("Unknown", index=df.index)
    )
    df["Scenario"] = (
        city_part + " - "
        + df["Description"].astype(str).str.strip() + " - "
        + size_part + " - "
        + caseless_label
    )

    rename_map: dict[str, str] = {}
    if "Fuel Change from Base" in df.columns:
        rename_map["Fuel Change from Base"] = _COL_FUEL_EA
    if "Resin Change from Base" in df.columns:
        rename_map["Resin Change from Base"] = _COL_RESIN_EA
    df = df.rename(columns=rename_map)

    for col in (_COL_FUEL_EA, _COL_RESIN_EA):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    multiplier = (
        df["Size"].apply(_gal_multiplier)
        if "Size" in df.columns
        else pd.Series(1.0, index=df.index)
    )
    if _COL_FUEL_EA in df.columns:
        df[_COL_FUEL_GAL] = df[_COL_FUEL_EA] * multiplier
    if _COL_RESIN_EA in df.columns:
        df[_COL_RESIN_GAL] = df[_COL_RESIN_EA] * multiplier

    return df


def _build_enriched_mover_extract(
    mover_df: pd.DataFrame,
    mapping_df: pd.DataFrame,
) -> pd.DataFrame:
    """Left-join Mover Extract with Mapping on 'Index'.

    Appends Item Code, Item Description, ShiptoName from the Mapping file.
    The Mapping is deduplicated by Index before joining so no duplicate rows
    are introduced. Unmatched rows receive 'Unknown' for lookup columns.
    """
    mapping = mapping_df.copy()
    mapping.columns = mapping.columns.str.strip()

    keep = [c for c in ["Index"] + _LOOKUP_COLS if c in mapping.columns]
    lookup = mapping[keep].drop_duplicates(subset=["Index"])

    if "Index" not in mover_df.columns:
        return mover_df.copy()

    enriched = mover_df.merge(lookup, on="Index", how="left")

    for col in _LOOKUP_COLS:
        if col in enriched.columns:
            enriched[col] = enriched[col].fillna("Unknown")

    return enriched


# ── 3. Chart primitives ───────────────────────────────────────────────────────

def _agg_by_month(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Group *df* by Month (chronological order) and compute the mean of *col*.

    Returns a DataFrame with columns [month_dt, Month, <col>].
    Rows with unparseable Month strings or non-numeric *col* values are dropped.
    """
    if "Month" not in df.columns or col not in df.columns:
        return pd.DataFrame(columns=["month_dt", "Month", col])

    work = df[["Month", col]].copy()
    work[col] = pd.to_numeric(work[col], errors="coerce")
    work["month_dt"] = _parse_month_dt(work["Month"])

    return (
        work.dropna(subset=["month_dt", col])
        .groupby("month_dt", as_index=False)
        .agg(Month=("Month", "first"), **{col: (col, "mean")})
        .sort_values("month_dt")
        .reset_index(drop=True)
    )


def _pct_change(first: float, last: float) -> Optional[float]:
    """Percentage change from *first* to *last*. Returns None when *first* is 0."""
    if not first or pd.isna(first) or pd.isna(last):
        return None
    return (last - first) / abs(first) * 100


def _ts_layout(title: str, y_title: str) -> dict:
    """Shared Plotly layout kwargs for time-series line charts."""
    return dict(
        title=title,
        font=_CHART_FONT,
        xaxis=dict(title="Month", tickformat="%b %Y", gridcolor="#f0f0f0"),
        yaxis=dict(title=y_title, gridcolor="#f0f0f0", rangemode="tozero"),
        plot_bgcolor="white",
        paper_bgcolor="white",
        height=360,
        margin=dict(l=65, r=40, t=45, b=65),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )


def _wf_layout(title: str, y_title: str) -> dict:
    """Shared Plotly layout kwargs for waterfall bar charts."""
    return dict(
        title=title,
        font=_CHART_FONT,
        xaxis=dict(title="Month", tickangle=-30, gridcolor="#f0f0f0"),
        yaxis=dict(title=y_title, gridcolor="#f0f0f0", zeroline=True, rangemode="tozero"),
        plot_bgcolor="white",
        paper_bgcolor="white",
        showlegend=False,
        height=360,
        margin=dict(l=65, r=40, t=45, b=85),
    )


def _build_scenario_change_table(
    enriched_df: pd.DataFrame,
    filtered_df: pd.DataFrame,
    col: str,
) -> pd.DataFrame:
    """Compute first-to-last $/Gal change for every Scenario in the active time window.

    Uses the time window from *filtered_df* but applies it to ALL scenarios in
    *enriched_df* so the table is never limited by the Scenario filter selection.

    Returns a DataFrame with columns:
      Scenario | First Month | Last Month | First Month Change from Base |
      Last Month Change from Base | Change $/Gal
    """
    if (
        "Scenario" not in enriched_df.columns
        or col not in enriched_df.columns
        or filtered_df.empty
        or "Month" not in filtered_df.columns
    ):
        return pd.DataFrame()

    time_dts = _parse_month_dt(filtered_df["Month"]).dropna()
    if time_dts.empty:
        return pd.DataFrame()
    start_dt, end_dt = time_dts.min(), time_dts.max()

    work = enriched_df.copy()
    work["_month_dt"] = _parse_month_dt(work["Month"])
    work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work[
        work["_month_dt"].notna()
        & (work["_month_dt"] >= start_dt)
        & (work["_month_dt"] <= end_dt)
    ].copy()

    if work.empty:
        return pd.DataFrame()

    rows: list[dict] = []
    for scenario in sorted(work["Scenario"].dropna().astype(str).unique()):
        scen_agg = (
            work[work["Scenario"].astype(str) == scenario]
            .groupby("_month_dt", as_index=False)[col]
            .mean()
            .sort_values("_month_dt")
        )
        if scen_agg.empty:
            continue
        v_first = scen_agg[col].iloc[0]
        v_last  = scen_agg[col].iloc[-1]
        rows.append({
            "Scenario":                     scenario,
            "First Month":                  scen_agg["_month_dt"].iloc[0].strftime("%b %Y"),
            "Last Month":                   scen_agg["_month_dt"].iloc[-1].strftime("%b %Y"),
            "First Month Change from Base": round(v_first, 5),
            "Last Month Change from Base":  round(v_last,  5),
            "Change $/Gal":                 round(v_last - v_first, 5),
        })

    return pd.DataFrame(rows)


# ── 4. Chart builders ─────────────────────────────────────────────────────────

def _build_fuel_ts_chart(
    df: pd.DataFrame,
) -> tuple[go.Figure, Optional[float]]:
    """Time-series of mean Current Fuel cost (index) across selected scenarios.

    Returns (figure, fuel_pct) where fuel_pct is the index % change first → last.
    """
    agg = _agg_by_month(df, "Current Fuel")
    fig = go.Figure()
    pct: Optional[float] = None

    if not agg.empty:
        fig.add_trace(go.Scatter(
            x=agg["month_dt"],
            y=agg["Current Fuel"],
            mode="lines+markers",
            name="Current Fuel",
            line=dict(color="#d32f2f", width=2),
            marker=dict(size=6),
            hovertemplate="%{x|%b %Y}<br>Current Fuel: $%{y:.4f}<extra></extra>",
        ))
        if len(agg) >= 2:
            pct = _pct_change(agg["Current Fuel"].iloc[0], agg["Current Fuel"].iloc[-1])

    fig.update_layout(**_ts_layout("Current Fuel Cost (Index) Over Time", "Current Fuel ($/gal)"))
    return fig, pct


def _build_resin_ts_chart(
    df: pd.DataFrame,
) -> tuple[go.Figure, Optional[float], Optional[float]]:
    """Time-series of mean Current Resin and Adjusted Resin cost (index).

    Returns (figure, resin_pct, adj_resin_pct).
    """
    adj_col   = _resolve_adj_resin_col(pd.Index(df.columns))
    agg_resin = _agg_by_month(df, "Current Resin")
    agg_adj   = _agg_by_month(df, adj_col) if adj_col else pd.DataFrame()

    fig = go.Figure()
    resin_pct: Optional[float] = None
    adj_pct:   Optional[float] = None

    if not agg_resin.empty:
        fig.add_trace(go.Scatter(
            x=agg_resin["month_dt"],
            y=agg_resin["Current Resin"],
            mode="lines+markers",
            name="Current Resin",
            line=dict(color="#1565c0", width=2),
            marker=dict(size=6),
            hovertemplate="%{x|%b %Y}<br>Current Resin: $%{y:.4f}<extra></extra>",
        ))
        if len(agg_resin) >= 2:
            resin_pct = _pct_change(
                agg_resin["Current Resin"].iloc[0],
                agg_resin["Current Resin"].iloc[-1],
            )

    if not agg_adj.empty and adj_col:
        fig.add_trace(go.Scatter(
            x=agg_adj["month_dt"],
            y=agg_adj[adj_col],
            mode="lines+markers",
            name="Adj Resin W/ 0.365 NMA",
            line=dict(color="#2e7d32", width=2, dash="dash"),
            marker=dict(size=6),
            hovertemplate="%{x|%b %Y}<br>Adj Resin: $%{y:.4f}<extra></extra>",
        ))
        if len(agg_adj) >= 2:
            adj_pct = _pct_change(agg_adj[adj_col].iloc[0], agg_adj[adj_col].iloc[-1])

    fig.update_layout(**_ts_layout(
        "Current Resin & Adjusted Resin Cost (Index) Over Time", "Resin Price ($/lb)"
    ))
    return fig, resin_pct, adj_pct


def _build_waterfall_chart(
    df: pd.DataFrame,
    col: str,
    title: str,
    y_title: str,
) -> tuple[go.Figure, Optional[float]]:
    """Month-over-month waterfall chart for *col* ($/Gal values).

    Uses go.Bar with explicit `base` positioning rather than go.Waterfall so
    that each bar can receive an individual marker color.

    Returns (figure, absolute_change_gal).
    Single-point guard: returns an empty figure when fewer than 2 months exist.
    """
    agg = _agg_by_month(df, col)
    fig = go.Figure()
    change_gal: Optional[float] = None

    if len(agg) < 2:
        if not agg.empty:
            st.caption(
                f"Only one time period in view for '{title}' — "
                "waterfall requires at least two months."
            )
        fig.update_layout(**_wf_layout(title, y_title))
        return fig, change_gal

    values       = agg[col].tolist()
    month_labels = [dt.strftime("%b %Y") for dt in agg["month_dt"].tolist()]
    n            = len(values)
    change_gal   = round(values[-1] - values[0], 6)

    bar_bases:   list[float] = []
    bar_heights: list[float] = []
    bar_colors:  list[str]   = []
    hover_texts: list[str]   = []

    for i in range(n):
        if i == 0:
            base   = min(0.0, values[0])
            height = abs(values[0])
            color  = _C_BASELINE
            hover  = f"{month_labels[0]}<br>Baseline: {values[0]:.5f}"
        else:
            delta  = values[i] - values[i - 1]
            base   = values[i - 1] if delta >= 0 else values[i]
            height = abs(delta)
            color  = _C_LAST if i == n - 1 else (_C_UP if delta >= 0 else _C_DOWN)
            hover  = f"{month_labels[i]}<br>Δ MoM: {delta:+.5f}"
            if i == n - 1:
                hover += f"<br>Last Value: {values[-1]:.5f}"

        bar_bases.append(base)
        bar_heights.append(height)
        bar_colors.append(color)
        hover_texts.append(hover)

    # width=0.6 → each bar spans ±0.3 around its category centre, leaving a
    # 0.4-unit gap for the dotted connector lines between adjacent bars.
    fig.add_trace(go.Bar(
        x=month_labels,
        y=bar_heights,
        base=bar_bases,
        marker_color=bar_colors,
        marker_line=dict(width=0.5, color="white"),
        hovertext=hover_texts,
        hoverinfo="text",
        showlegend=False,
        width=0.6,
    ))

    # Connector lines: on a categorical x-axis, shape x coords are 0-indexed
    # integers per category. Right edge of bar i is i+0.3; left edge of bar
    # i+1 is i+0.7. The connector bridges this gap at y = values[i].
    for i in range(n - 1):
        fig.add_shape(
            type="line",
            x0=i + 0.3, x1=i + 0.7,
            y0=values[i], y1=values[i],
            line=dict(color="#888888", width=1, dash="dot"),
            xref="x", yref="y",
        )

    fig.add_hline(y=0, line_dash="dash", line_color="#666666", line_width=0.8)
    fig.update_layout(**_wf_layout(title, y_title))
    return fig, change_gal


# ── 5. UI sections ────────────────────────────────────────────────────────────

def _render_upload_section() -> list:
    """Render a single multi-file uploader for both Walmart CSVs.

    Files are matched by filename keyword ('mover' / 'mapping') in _detect_files().
    Returns the raw list of uploaded file objects (may be empty).
    """
    st.markdown("#### 📤 Upload Walmart Fresh Data Files")
    st.caption(
        f"Upload **both** CSV files at once from the "
        f"[📁 SharePoint folder]({_SHAREPOINT_URL}). "
        "Files are matched automatically by filename keyword: "
        "**'mover'** → Walmart Mover Analysis,  "
        "**'mapping'** → Walmart Mapping."
    )
    return st.file_uploader(
        "Select Walmart Mover Analysis and Mapping CSV files",
        type=["csv"],
        accept_multiple_files=True,
        key=f"{_SS_PREFIX}_upload",
    ) or []


def _render_controls(enriched_df: pd.DataFrame) -> pd.DataFrame:
    """Render the Scenario multiselect and time-range slicer.

    Returns the subset of *enriched_df* that satisfies both controls.
    The time-range slicer affects charts only; downloads always use the full extracts.
    """
    st.markdown("#### 🔍 Filter & Time Range")
    c_filter, c_slicer = st.columns([1, 2])

    with c_filter:
        scenarios = (
            sorted(enriched_df["Scenario"].dropna().astype(str).unique().tolist())
            if "Scenario" in enriched_df.columns else []
        )
        sel_scenarios = st.multiselect(
            "Scenario",
            options=scenarios,
            default=scenarios,
            key=f"{_SS_PREFIX}_scenario_filter",
            help=(
                "Scenario = Store City – Description – Size – Caseless/Non-Caseless. "
                "Select one or more to focus the charts. "
                "The per-scenario summary tables always show all scenarios."
            ),
        )
        st.markdown("**Select a single scenario to view the waterfall charts.**")

    if not sel_scenarios:
        return enriched_df.iloc[0:0]

    df_scen = (
        enriched_df[enriched_df["Scenario"].astype(str).isin(sel_scenarios)].copy()
        if "Scenario" in enriched_df.columns else enriched_df.copy()
    )

    with c_slicer:
        if "Month" not in df_scen.columns or df_scen.empty:
            return df_scen

        df_scen["_month_dt"] = _parse_month_dt(df_scen["Month"])
        valid_mask = df_scen["_month_dt"].notna()
        all_month_labels: list[str] = (
            df_scen.loc[valid_mask]
            .drop_duplicates(subset=["_month_dt"])
            .sort_values("_month_dt")["Month"]
            .tolist()
        )

        if len(all_month_labels) >= 2:
            sel_start, sel_end = st.select_slider(
                "Time Range",
                options=all_month_labels,
                value=(all_month_labels[0], all_month_labels[-1]),
                key=f"{_SS_PREFIX}_time_range",
                help="Slide to restrict which months appear in the charts.",
            )
            start_dt = _parse_month_dt(pd.Series([sel_start])).iloc[0]
            end_dt   = _parse_month_dt(pd.Series([sel_end])).iloc[0]
            df_scen  = df_scen[
                valid_mask
                & (df_scen["_month_dt"] >= start_dt)
                & (df_scen["_month_dt"] <= end_dt)
            ]

    df_scen = df_scen.drop(columns=["_month_dt"], errors="ignore")
    st.caption(f"**{len(df_scen):,}** rows feed the charts after applying all controls.")
    return df_scen


def _render_chart_section(
    filtered_df: pd.DataFrame,
    enriched_df: pd.DataFrame,
) -> None:
    """Render all four trend charts in a 2×2 grid.

    Row 1 (time-series): Fuel TS chart | Resin + Adj Resin TS chart
    Row 2 (waterfall):   Fuel MoM waterfall | Resin MoM waterfall
                         (row 2 is only shown when exactly one scenario is selected)

    Parameters
    ----------
    filtered_df : Scenario- and time-filtered enriched extract (drives charts).
    enriched_df : Full enriched extract — used by summary tables so all scenarios
                  are shown regardless of the Scenario filter selection.
    """
    if filtered_df.empty:
        st.info("No data matches the current controls — adjust the Scenario or Time Range.")
        return

    st.markdown("#### 📈 Trend Analysis")
    st.caption(
        "Charts aggregate by month (mean across selected scenarios). "
        "Waterfall: **blue** = baseline, **green** = MoM increase, "
        "**red** = decrease, **black** = last month (endpoint). "
        "Summary tables show ALL scenarios for the selected time range."
    )

    # Row 1: time-series charts
    col1, col2 = st.columns(2)
    with col1:
        fig_fuel, fuel_pct = _build_fuel_ts_chart(filtered_df)
        if fuel_pct is not None:
            st.metric(
                "Fuel Index Change%",
                f"{fuel_pct:.1f}%",
                help="(Last month − First month) / |First month| for Current Fuel index.",
            )
        st.plotly_chart(fig_fuel, use_container_width=True)

    with col2:
        fig_resin, resin_pct, adj_pct = _build_resin_ts_chart(filtered_df)
        m_r, m_a = st.columns(2)
        with m_r:
            if resin_pct is not None:
                st.metric(
                    "Resin Index Change%",
                    f"{resin_pct:.1f}%",
                    help="(Last − First) / |First| for Current Resin index.",
                )
        with m_a:
            if adj_pct is not None:
                st.metric(
                    "Adj Resin Index Change%",
                    f"{adj_pct:.1f}%",
                    help="(Last − First) / |First| for Adj Resin W/ 0.365 NMA index.",
                )
        st.plotly_chart(fig_resin, use_container_width=True)

    # Row 2: waterfall charts — require exactly one scenario selected
    single_scenario = (
        "Scenario" in filtered_df.columns
        and filtered_df["Scenario"].nunique() == 1
    )
    if not single_scenario:
        st.info(
            "💡 **Select exactly one scenario** from the filter above to display "
            "the waterfall charts."
        )
        return

    col3, col4 = st.columns(2)
    with col3:
        fig_fuel_wf, _ = _build_waterfall_chart(
            filtered_df,
            _COL_FUEL_GAL,
            "Fuel Change from Base: Month-over-Month ($/Gal)",
            "Fuel Change ($/Gal)",
        )
        fuel_tbl = _build_scenario_change_table(filtered_df, filtered_df, _COL_FUEL_GAL)
        if not fuel_tbl.empty:
            st.caption("Fuel Change $/Gal — filtered by selected scenarios & time range:")
            st.dataframe(fuel_tbl, use_container_width=True, hide_index=True)
        st.plotly_chart(fig_fuel_wf, use_container_width=True)

    with col4:
        fig_resin_wf, _ = _build_waterfall_chart(
            filtered_df,
            _COL_RESIN_GAL,
            "Resin Change from Base: Month-over-Month ($/Gal)",
            "Resin Change ($/Gal)",
        )
        resin_tbl = _build_scenario_change_table(filtered_df, filtered_df, _COL_RESIN_GAL)
        if not resin_tbl.empty:
            st.caption("Resin Change $/Gal — filtered by selected scenarios & time range:")
            st.dataframe(resin_tbl, use_container_width=True, hide_index=True)
        st.plotly_chart(fig_resin_wf, use_container_width=True)


def _render_downloads(
    enriched_extract: pd.DataFrame,
    mover_extract: pd.DataFrame,
) -> None:
    """Render download buttons for the full Enriched Mover Extract and Mover Extract.

    Downloads are always the complete processed output — never filtered by the
    Scenario or Time Range controls above.
    """
    st.markdown("#### ⬇️ Download Processed Data")
    today = datetime.now().strftime("%Y%m%d")
    dc1, dc2 = st.columns(2)
    with dc1:
        st.download_button(
            label="⬇️ Download Enriched Mover Extract (CSV)",
            data=_to_csv_bytes(enriched_extract),
            file_name=f"Walmart_Enriched_Mover_Extract_{today}.csv",
            mime="text/csv",
            key=f"{_SS_PREFIX}_download_enriched",
        )
    with dc2:
        st.download_button(
            label="⬇️ Download Mover Extract (CSV)",
            data=_to_csv_bytes(mover_extract),
            file_name=f"Walmart_Mover_Extract_{today}.csv",
            mime="text/csv",
            key=f"{_SS_PREFIX}_download_mover",
        )


# ── 6. Public API ─────────────────────────────────────────────────────────────

def _render_processed_view(
    mover_extract: pd.DataFrame,
    enriched_extract: pd.DataFrame,
) -> None:
    """Render the post-upload view: compact status bar + controls + charts + downloads.

    The upload widget and file-processing instructions are intentionally absent
    here. A 'Change files' button lets the user reset back to the upload state.
    """
    col_status, col_btn = st.columns([4, 1])
    with col_status:
        st.success(
            f"✅ Files loaded — "
            f"Mover Extract: **{len(mover_extract):,} rows** | "
            f"Enriched Extract: **{len(enriched_extract):,} rows**"
        )
    with col_btn:
        if st.button(
            "📁 Change files",
            key=f"{_SS_PREFIX}_clear_btn",
            use_container_width=True,
            help="Clear the current files and show the upload panel again.",
        ):
            # Evict all processed data from session state to return to upload view.
            for key in (
                f"{_SS_PREFIX}_file_sig",
                f"{_SS_PREFIX}_mover_extract",
                f"{_SS_PREFIX}_enriched_extract",
            ):
                st.session_state.pop(key, None)
            st.rerun(scope="fragment")

    st.markdown("---")
    filtered_df = _render_controls(enriched_extract)
    st.markdown("---")
    _render_chart_section(filtered_df, enriched_extract)
    st.markdown("---")
    _render_downloads(enriched_extract, mover_extract)


@st.fragment
def render_walmart_fresh_tracker() -> None:
    """Render the Walmart Fresh Tracker as an isolated page fragment.

    Decorated with @st.fragment so that widget interactions inside this section
    (file uploads, scenario filters, time-range sliders) only rerun this fragment,
    never the full page. This means the Market Indices section above is completely
    unaffected by any activity inside the tracker.

    State machine
    -------------
    Upload state  (no cached data)
        → Show upload instructions + file uploader.
        → When both files are present, process them and store results in
          session_state, then rerun the fragment.

    Processed state  (cached data exists)
        → Show compact status bar + 'Change files' button.
        → Show filter controls, charts, and download buttons.
        → 'Change files' evicts cached data and reruns, returning to upload state.
    """
    mover_extract    = st.session_state.get(f"{_SS_PREFIX}_mover_extract")
    enriched_extract = st.session_state.get(f"{_SS_PREFIX}_enriched_extract")

    # ── Processed state ───────────────────────────────────────────────────────
    if mover_extract is not None and enriched_extract is not None:
        _render_processed_view(mover_extract, enriched_extract)
        return

    # ── Upload state ──────────────────────────────────────────────────────────
    uploaded_files = _render_upload_section()
    mover_file, mapping_file = _detect_files(uploaded_files)

    if mover_file is None or mapping_file is None:
        missing = [
            label for label, f in [
                ("Walmart Mover Analysis  (filename must contain 'mover')",  mover_file),
                ("Walmart Mapping  (filename must contain 'mapping')",       mapping_file),
            ]
            if f is None
        ]
        st.info(f"👆 Still needed: **{' | '.join(missing)}**")
        return

    # ── Processing ────────────────────────────────────────────────────────────
    file_sig = hashlib.md5(
        f"{mover_file.name}:{mover_file.size}:{mapping_file.name}:{mapping_file.size}"
        .encode()
    ).hexdigest()

    # Guard against re-processing the same files (e.g. if rerun fires before
    # the session_state branch is entered on the next render cycle).
    if st.session_state.get(f"{_SS_PREFIX}_file_sig") == file_sig:
        st.rerun(scope="fragment")
        return

    with st.spinner("Processing — building Mover Extract and Enriched Mover Extract…"):
        try:
            raw_mover = pd.read_csv(mover_file, low_memory=False)
        except Exception as exc:
            st.error(f"Could not read Mover Analysis file: {exc}")
            return
        try:
            raw_mapping = pd.read_csv(mapping_file)
        except Exception as exc:
            st.error(f"Could not read Mapping file: {exc}")
            return

        raw_mover.columns   = raw_mover.columns.str.strip()
        raw_mapping.columns = raw_mapping.columns.str.strip()

        try:
            mover_extract = _build_mover_extract(raw_mover)
        except ValueError as exc:
            st.error(str(exc))
            return

        enriched_extract = _build_enriched_mover_extract(mover_extract, raw_mapping)

    st.session_state[f"{_SS_PREFIX}_mover_extract"]    = mover_extract
    st.session_state[f"{_SS_PREFIX}_enriched_extract"] = enriched_extract
    st.session_state[f"{_SS_PREFIX}_file_sig"]         = file_sig

    # Rerun the fragment now that session_state is populated so the next render
    # enters the "Processed state" branch and shows the clean view.
    st.rerun(scope="fragment")
