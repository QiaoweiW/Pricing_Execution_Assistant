"""
Bid Asset Intelligence page view.

Sections
--------
1. Types & constants     (_FilterResult, _FINANCIAL_COLS, _GROUP_COLS,
                          _STATUS_RULES, _DEFAULT_STATUS_COLOR, _COLOR_LEGEND,
                          _CHART_FONT, _SHAREPOINT_URL)
2. Formatting helpers    (_fmt_currency, _fmt_volume, _fmt_pct, _to_csv_bytes,
                          _apply_display_formats)
3. Data helpers          (_month_sort_key, _coerce_month_to_label, _sel_hash,
                          _excel_serial_to_date, _parse_currency_col,
                          _filter_by_month_range)
4. Chart helpers         (_round_num, _make_bid_label, _status_color,
                          _prepare_chart_data, _build_overview_chart)
5. Data loading          (_load_and_normalise)
6. Page sections         (_render_bid_overview, _render_search_filters,
                          _render_rfp_summary, _render_detail_table)
7. Entry point           (render)
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import NamedTuple, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from utils.ui_helpers import apply_custom_css

# ── 1. Types & constants ──────────────────────────────────────────────────────

class _FilterResult(NamedTuple):
    """Typed return value from _render_search_filters.

    Bundles the filtered DataFrame together with the active cascading-filter
    selections so callers receive an explicit contract instead of hidden state
    stored on DataFrame.attrs.
    """
    df:          pd.DataFrame
    sel_company: list[str]
    sel_bid:     list[str]
    sel_round:   list[str]


# Used for both currency parsing on load AND aggregation/display in tables.
# Single source of truth — no separate SUM_COLS / NUMERIC_COLS needed.
_FINANCIAL_COLS = ["Volume (lbs)", "FOB Revenue $/Yr", "PCM $/Yr", "GP $/Yr"]

# Column names used to group rows in the RFP Summary aggregation.
_GROUP_COLS = [
    "Format", "Company", "Bid Description", "Brand",
    "Round", "Month", "Status", "Bid Rationale", "Feedback",
]

# Status keyword → (hex colour, legend label).
# "award" maps to the same colour/label as "accept" intentionally.
_STATUS_RULES: list[tuple[str, str, str]] = [
    ("accept", "#4CAF50", "Accepted"),
    ("award",  "#4CAF50", "Accepted"),
    ("reject", "#9E9E9E", "Rejected"),
]
_DEFAULT_STATUS_COLOR = "#2196F3"   # "Other" / unknown

# Ordered legend entries for the chart (de-duplicated view of _STATUS_RULES + default).
_COLOR_LEGEND: list[tuple[str, str]] = [
    ("#4CAF50", "Accepted"),
    ("#9E9E9E", "Rejected"),
    (_DEFAULT_STATUS_COLOR, "Other"),
]

_CHART_FONT = dict(family="Segoe UI, Tahoma, Geneva, Verdana, sans-serif", size=14)

_SHAREPOINT_URL = (
    "https://darigold1com.sharepoint.com/sites/BrandedPricing/Shared%20Documents"
    "/Forms/AllItems.aspx?id=%2Fsites%2FBrandedPricing%2FShared%20Documents"
    "%2FGeneral%2F02%20Resources%2FRFP%20Management"
    "&viewid=9103ebc3%2Df944%2D4451%2Dbe05%2Dd0cb7479e27e"
)

# ── 2. Formatting helpers ─────────────────────────────────────────────────────

def _fmt_currency(val) -> str:
    if pd.isna(val):
        return ""
    return f"$({abs(val):,.0f})" if val < 0 else f"${val:,.0f}"


def _fmt_volume(val) -> str:
    return "" if pd.isna(val) else f"{val:,.0f}"


def _fmt_pct(val) -> str:
    """Format a ratio (already multiplied by 100) as a percentage string.

    Uses a try/except instead of isinstance() so that numpy scalar types
    (e.g. np.float64, which is not a subclass of float in NumPy ≥ 2.0)
    are handled correctly without an explicit numpy dependency.
    """
    if pd.isna(val):
        return "—"
    try:
        return f"{val:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def _apply_display_formats(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Return a copy of *df* with financial columns formatted for display.

    Volume (lbs) → comma-separated integer; everything else → $currency string.
    Columns in *cols* that are not present in *df* are silently skipped.
    """
    out = df.copy()
    for col in cols:
        if col in out.columns:
            out[col] = out[col].apply(
                _fmt_volume if col == "Volume (lbs)" else _fmt_currency
            )
    return out


# ── 3. Data helpers ───────────────────────────────────────────────────────────

def _month_sort_key(m_str: str) -> datetime:
    """Parse a canonical 'Mon YYYY' string to datetime for sorting/comparison.

    Returns datetime.min on failure so unparseable values sort to the front
    without raising.  After _load_and_normalise runs, every Month value is
    guaranteed to be in this format, so failure should never occur in practice.
    """
    try:
        return datetime.strptime(str(m_str).strip(), "%b %Y")
    except Exception:
        return datetime.min


def _coerce_month_to_label(val) -> str:
    """Normalise any common month representation to the canonical 'Mon YYYY' label.

    Tries a series of explicit strptime formats first (fastest, most predictable),
    then falls back to pandas' flexible parser.  This is the single place that
    bridges the variety of month formats found in uploaded CSVs to the format
    expected by _month_sort_key and the month-range slider.
    """
    s = str(val).strip()
    for fmt in ("%b %Y", "%B %Y", "%Y-%m", "%m/%Y", "%m-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%b %Y")
        except ValueError:
            pass
    try:
        return pd.to_datetime(s).strftime("%b %Y")
    except Exception:
        return s  # return as-is; slider will display it but filtering may not work


def _sel_hash(*selections) -> str:
    """Return a short stable hash of one or more multiselect value lists.

    Used as a widget key suffix so downstream cascading filters auto-reset
    whenever any upstream selection changes.  Order within each selection list
    is ignored; order across selection groups is preserved.
    """
    combined = "|".join(
        str(x) for sel in selections for x in sorted(str(s) for s in sel)
    )
    return hashlib.md5(combined.encode()).hexdigest()[:8]


def _excel_serial_to_date(serial) -> str:
    """Convert an Excel date serial integer to a 'Mon YYYY' label."""
    try:
        dt = datetime(1899, 12, 30) + timedelta(days=int(float(serial)))
        return dt.strftime("%b %Y")
    except Exception:
        return str(serial)


def _parse_currency_col(series: pd.Series) -> pd.Series:
    """Convert currency strings like '$424,236' or '$(3,846)' to floats.

    Numeric series are returned unchanged.  Negative values expressed with
    parentheses (accounting notation) are converted to negative floats.
    """
    if pd.api.types.is_numeric_dtype(series):
        return series
    cleaned = (
        series.astype(str)
        .str.strip()
        .str.replace(r"\$", "", regex=True)
        .str.replace(",",   "", regex=False)
        .str.replace(" ",   "", regex=False)
    )
    is_neg = cleaned.str.startswith("(")
    cleaned = cleaned.str.replace(r"[()]", "", regex=True)
    result  = pd.to_numeric(cleaned, errors="coerce")
    return result.where(~is_neg, -result)


def _filter_by_month_range(
    df: pd.DataFrame,
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
) -> pd.DataFrame:
    """Return rows whose Month falls within [start_dt, end_dt] (inclusive).

    Vectorized via pd.to_datetime — avoids row-by-row Python apply overhead.
    Relies on Month having been normalised to 'Mon YYYY' by _load_and_normalise;
    unparseable values become NaT and are excluded from the result.
    """
    if "Month" not in df.columns or start_dt is None or end_dt is None:
        return df
    month_dts = pd.to_datetime(df["Month"], format="%b %Y", errors="coerce")
    return df[(month_dts >= start_dt) & (month_dts <= end_dt)]


# ── 4. Chart helpers ──────────────────────────────────────────────────────────

def _round_num(r) -> int:
    """Extract the numeric part of a round label (e.g. 'Round 2' → 2)."""
    digits = "".join(c for c in str(r) if c.isdigit())
    return int(digits) if digits else 0


def _make_bid_label(row: pd.Series) -> str:
    """Build the multi-line x-axis tick label: Company / Bid Description / (Round)."""
    return (
        f"{row.get('Company', '')}<br>"
        f"{row.get('Bid Description', '')}<br>"
        f"({row.get('Round', '')})"
    )


def _status_color(status: str) -> str:
    """Map a status string to its chart colour via keyword substring matching."""
    s = str(status).lower()
    for keyword, color, _ in _STATUS_RULES:
        if keyword in s:
            return color
    return _DEFAULT_STATUS_COLOR


def _prepare_chart_data(df: pd.DataFrame) -> pd.DataFrame:
    """Reduce *df* (pre-filtered by chart controls) to one aggregated row per bid.

    Steps
    -----
    1. Keep only the latest round per Company / Bid Description.
    2. Sum Volume (lbs) and PCM $/Yr across all items in that round.
    3. Derive PCM $/lb = PCM $/Yr / Volume (lbs), rounded to 2 dp.
    4. Attach the modal Status, display colour, and x-axis label.

    Returns an empty DataFrame when required columns are absent.
    """
    if df.empty or "Round" not in df.columns:
        return pd.DataFrame()

    group_keys = [c for c in ["Company", "Bid Description"] if c in df.columns]
    if not group_keys:
        return pd.DataFrame()

    work = df.copy()
    work["_round_num"] = work["Round"].apply(_round_num)

    latest_per_bid = (
        work.groupby(group_keys)["_round_num"]
        .max()
        .reset_index()
        .rename(columns={"_round_num": "_max_round"})
    )
    work = work.merge(latest_per_bid, on=group_keys)
    work = work[work["_round_num"] == work["_max_round"]]

    agg_keys = group_keys + ["Round"]
    sum_cols  = [c for c in ["Volume (lbs)", "PCM $/Yr"] if c in work.columns]
    agg       = work.groupby(agg_keys, as_index=False)[sum_cols].sum()

    if "Status" in work.columns:
        status_mode = (
            work.groupby(agg_keys)["Status"]
            .agg(lambda x: x.mode().iloc[0] if not x.mode().empty else "Unknown")
            .reset_index()
        )
        agg = agg.merge(status_mode, on=agg_keys, how="left")
    else:
        agg["Status"] = "Unknown"

    if {"Volume (lbs)", "PCM $/Yr"}.issubset(agg.columns):
        safe_vol      = agg["Volume (lbs)"].replace(0, float("nan"))
        agg["PCM $/lb"] = (agg["PCM $/Yr"] / safe_vol).round(2)

    agg["_color"] = agg["Status"].apply(_status_color)
    agg["_label"] = agg.apply(_make_bid_label, axis=1)

    if "Volume (lbs)" in agg.columns:
        agg = agg.sort_values("Volume (lbs)", ascending=False)

    return agg.reset_index(drop=True)


def _build_overview_chart(
    chart_agg: pd.DataFrame,
    show_pcm_yr: bool = True,
    show_pcm_lb: bool = True,
) -> go.Figure:
    """Build the Bid Overview combo chart.

    Always rendered
    ---------------
    Volume (lbs): status-coloured bars → primary (left) y-axis.
    One trace per status colour keeps the legend self-contained without
    requiring dummy placeholder traces.

    Optional overlays
    -----------------
    Total PCM $/Yr: red dots, no lines  → secondary (right) y-axis.
    PCM $/lb: black dots, no lines      → tertiary (far-right) y-axis.

    PCM $/lb requires its own axis: its $/lb scale (~$0–$5) would be
    invisible against PCM $/Yr (~$100k–$5M) on a shared axis.

    The x-axis domain contracts only as far as active right-side axes require,
    so the bars always fill as much horizontal space as possible.
    """
    fig = make_subplots(specs=[[{"secondary_y": True}]])

    for color_hex, legend_name in _COLOR_LEGEND:
        mask = chart_agg["_color"] == color_hex
        if not mask.any():
            continue
        subset = chart_agg[mask]
        fig.add_trace(
            go.Bar(
                x=subset["_label"],
                y=subset["Volume (lbs)"] if "Volume (lbs)" in subset.columns else [],
                name=legend_name,
                marker_color=color_hex,
                opacity=0.85,
                hovertemplate="%{x}<br>Volume: %{y:,.0f} lbs<extra></extra>",
            ),
            secondary_y=False,
        )

    if show_pcm_yr and "PCM $/Yr" in chart_agg.columns:
        fig.add_trace(
            go.Scatter(
                x=chart_agg["_label"],
                y=chart_agg["PCM $/Yr"],
                name="Total PCM $/Yr",
                mode="markers",
                marker=dict(size=10, color="#d32f2f", symbol="circle"),
                hovertemplate="%{x}<br>PCM $/Yr: $%{y:,.0f}<extra></extra>",
            ),
            secondary_y=True,
        )

    if show_pcm_lb and "PCM $/lb" in chart_agg.columns:
        fig.add_trace(
            go.Scatter(
                x=chart_agg["_label"],
                y=chart_agg["PCM $/lb"],
                name="PCM $/lb",
                mode="markers",
                marker=dict(size=10, color="black", symbol="circle"),
                hovertemplate="%{x}<br>PCM $/lb: $%{y:.2f}<extra></extra>",
                yaxis="y3",
            ),
        )

    if show_pcm_yr and show_pcm_lb:
        x_right = 0.78      # room for two stacked right axes
    elif show_pcm_yr or show_pcm_lb:
        x_right = 0.88      # room for one right axis
    else:
        x_right = 1.0       # volume-only: bars fill full width

    fig.update_layout(
        barmode="overlay",
        font=_CHART_FONT,
        xaxis=dict(
            domain=[0, x_right],
            title=dict(text="Company / Bid Description (Round)", font=dict(size=15)),
            tickangle=-20,
            tickfont=dict(size=13),
        ),
        yaxis=dict(
            title=dict(text="Volume (lbs)", font=dict(size=15)),
            tickfont=dict(size=13),
            gridcolor="#f0f0f0",
        ),
        yaxis2=dict(
            title=dict(text="Total PCM $/Yr", font=dict(size=15)),
            tickfont=dict(size=13),
            showgrid=False,
            tickformat="$.2s",
            visible=show_pcm_yr,
        ),
        yaxis3=dict(
            title=dict(text="PCM $/lb", font=dict(size=14)),
            tickfont=dict(size=12),
            overlaying="y",
            side="right",
            anchor="free",
            position=0.93,
            showgrid=False,
            tickformat="$.2f",
            visible=show_pcm_lb,
        ),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
            font=dict(size=13),
        ),
        height=560,
        margin=dict(l=80, r=180, t=60, b=180),
        plot_bgcolor="white",
        paper_bgcolor="white",
    )

    return fig


# ── 5. Data loading & normalisation ───────────────────────────────────────────

def _load_and_normalise(uploaded_file) -> Optional[pd.DataFrame]:
    """Read the uploaded CSV and normalise it in-place:

    - Strip column-name whitespace.
    - Rename 'Rounds' → 'Round' for schema consistency.
    - Convert Month to canonical 'Mon YYYY' labels, handling numeric Excel
      serials and any string format via _coerce_month_to_label.  This step
      is critical: without it, _month_sort_key returns datetime.min for all
      values, making the month-range slider a silent no-op.
    - Parse financial columns to float via _parse_currency_col.

    Returns None (with st.error displayed) on read failure.
    """
    try:
        df = pd.read_csv(uploaded_file)
    except Exception as exc:
        st.error(f"Could not read the uploaded file: {exc}")
        return None

    df.columns = df.columns.str.strip()

    if "Rounds" in df.columns:
        df = df.rename(columns={"Rounds": "Round"})

    if "Month" in df.columns:
        if pd.api.types.is_numeric_dtype(df["Month"]):
            df["Month"] = df["Month"].apply(_excel_serial_to_date)
        else:
            df["Month"] = df["Month"].apply(_coerce_month_to_label)

    for col in _FINANCIAL_COLS:
        if col in df.columns:
            df[col] = _parse_currency_col(df[col])

    return df


# ── 6. Page sections ──────────────────────────────────────────────────────────

def _render_bid_overview(raw_df: pd.DataFrame, all_months_sorted: list[str]) -> None:
    """Render the Bid Overview section.

    Controls (top to bottom)
    ------------------------
    1. Month-range slider  — first filter; narrows the data pool for all below.
    2. Categorical filters — cascading: Format → Size → Referenced Item Description.
       Each filter's options are derived from the rows that survive all filters
       above it, so selecting a Format instantly restricts which Sizes appear,
       and selecting a Size restricts which Referenced Item Descriptions appear.
    3. Overlay toggles     — show/hide PCM $/Yr (red) and PCM $/lb (black) dots.

    All layers feed the same chart_base dataset before aggregation, so every
    metric in the chart reflects the full filter state at all times.
    """
    st.markdown("### 📈 Bid Overview")
    st.caption(
        "Volume (lbs) bars are always shown, colour-coded by bid outcome "
        "(green = Accepted, gray = Rejected, blue = Other). "
        "Use the overlay toggles to add financial rate metrics on separate axes. "
        "All values update dynamically with every filter change."
    )

    # Row 1: month-range slicer (full width)
    if len(all_months_sorted) >= 2:
        chart_month_range = st.select_slider(
            "Month Range",
            options=all_months_sorted,
            value=(all_months_sorted[0], all_months_sorted[-1]),
            key="chart_month_range",
        )
        chart_start_dt = _month_sort_key(chart_month_range[0])
        chart_end_dt   = _month_sort_key(chart_month_range[1])
    elif all_months_sorted:
        chart_start_dt = chart_end_dt = _month_sort_key(all_months_sorted[0])
    else:
        chart_start_dt = chart_end_dt = None

    # Month-filtered pool — basis for all categorical cascades below.
    # Computed here (between the slider and the column widgets) so that Format
    # options already reflect the selected date range on every rerun.
    df_chart_month = _filter_by_month_range(raw_df, chart_start_dt, chart_end_dt)

    # Row 2: cascading categorical filters — Format → Size → Referenced Item Description
    # Each filter's option list is drawn from the rows that survive all upstream
    # filters, so the available choices narrow automatically as you drill down.
    # _sel_hash-keyed widgets reset to "all" whenever a parent selection changes.
    cf1, cf2, cf3 = st.columns(3)

    # Format — root of the cascade; options from the month-filtered pool
    with cf1:
        fmt_opts = (
            sorted(df_chart_month["Format"].dropna().astype(str).unique().tolist())
            if "Format" in df_chart_month.columns else []
        )
        sel_chart_fmt = st.multiselect(
            "Format", options=fmt_opts, default=fmt_opts, key="chart_format"
        )

    df_after_fmt = (
        df_chart_month[df_chart_month["Format"].astype(str).isin(sel_chart_fmt)]
        if sel_chart_fmt and "Format" in df_chart_month.columns
        else df_chart_month if not fmt_opts          # column absent — pass through
        else df_chart_month.iloc[0:0]                # user cleared selection
    )

    # Size — cascades from Format; resets when Format selection changes
    with cf2:
        size_opts = (
            sorted(df_after_fmt["Size"].dropna().astype(str).unique().tolist())
            if "Size" in df_after_fmt.columns else []
        )
        sel_chart_size = st.multiselect(
            "Size", options=size_opts, default=size_opts,
            key=f"chart_size_{_sel_hash(sel_chart_fmt)}",
        )

    df_after_size = (
        df_after_fmt[df_after_fmt["Size"].astype(str).isin(sel_chart_size)]
        if sel_chart_size and "Size" in df_after_fmt.columns
        else df_after_fmt if not size_opts           # column absent — pass through
        else df_after_fmt.iloc[0:0]                  # user cleared selection
    )

    # Referenced Item Description — cascades from Size; resets when Format or Size changes
    with cf3:
        ref_opts = (
            sorted(df_after_size["Referenced Item Description"].dropna().astype(str).unique().tolist())
            if "Referenced Item Description" in df_after_size.columns else []
        )
        sel_chart_ref = st.multiselect(
            "Referenced Item Description", options=ref_opts, default=ref_opts,
            key=f"chart_ref_item_{_sel_hash(sel_chart_fmt, sel_chart_size)}",
        )

    chart_base = (
        df_after_size[df_after_size["Referenced Item Description"].astype(str).isin(sel_chart_ref)]
        if sel_chart_ref and "Referenced Item Description" in df_after_size.columns
        else df_after_size if not ref_opts           # column absent — pass through
        else df_after_size.iloc[0:0]                 # user cleared selection
    )

    # Row 3: metric overlay toggles
    st.markdown("**Overlay metrics** — add financial rate indicators on top of the volume bars:")
    ov1, ov2, _ = st.columns([2, 2, 3])
    with ov1:
        show_pcm_yr = st.checkbox(
            "Total PCM $/Yr  🔴",
            value=True,
            key="chart_show_pcm_yr",
            help=(
                "Show Total PCM $/Yr as red dots on the right axis. "
                "This is the absolute annual profit contribution of each bid."
            ),
        )
    with ov2:
        show_pcm_lb = st.checkbox(
            "PCM $/lb  ⚫",
            value=True,
            key="chart_show_pcm_lb",
            help=(
                "Show PCM per pound as black dots on the far-right axis. "
                "This rate metric normalises profitability by volume, making "
                "bids of different sizes directly comparable."
            ),
        )

    chart_agg = _prepare_chart_data(chart_base)
    if chart_agg.empty:
        st.info("No data available for the selected chart filters.")
    else:
        st.plotly_chart(
            _build_overview_chart(chart_agg, show_pcm_yr=show_pcm_yr, show_pcm_lb=show_pcm_lb),
            use_container_width=True,
        )


def _render_search_filters(
    raw_df: pd.DataFrame,
    all_months_sorted: list[str],
) -> Optional[_FilterResult]:
    """Render the Search & Filter section and return a typed _FilterResult.

    The month slider is independent of the cascading dropdowns — it pre-filters
    the pool from which downstream options are drawn, but Company options always
    come from the full dataset so known companies are never hidden.

    Returns None if any cascading filter has no value selected, signalling the
    caller to stop rendering further sections.
    """
    st.markdown("### 🔍 Search & Filter")
    st.caption(
        "**Month** is an independent time-range slicer. "
        "**Company** anchors the remaining cascading filters."
    )

    if len(all_months_sorted) >= 2:
        filter_month_range = st.select_slider(
            "📅 Month Range",
            options=all_months_sorted,
            value=(all_months_sorted[0], all_months_sorted[-1]),
            key="filter_month_range",
        )
        filter_start_dt = _month_sort_key(filter_month_range[0])
        filter_end_dt   = _month_sort_key(filter_month_range[1])
    elif all_months_sorted:
        filter_start_dt = filter_end_dt = _month_sort_key(all_months_sorted[0])
    else:
        filter_start_dt = filter_end_dt = None

    df_month = _filter_by_month_range(raw_df, filter_start_dt, filter_end_dt)

    # Cascading dropdowns: Company → Bid Description → Round → Format
    f1, f2, f3, f4 = st.columns(4)

    with f1:
        company_opts = (
            sorted(raw_df["Company"].dropna().astype(str).unique().tolist())
            if "Company" in raw_df.columns else []
        )
        sel_company = st.multiselect(
            "Company", options=company_opts, default=company_opts, key="ms_company"
        )

    df1 = (
        df_month[df_month["Company"].astype(str).isin(sel_company)]
        if sel_company and "Company" in df_month.columns
        else df_month.iloc[0:0]
    )

    with f2:
        bid_opts = (
            sorted(df1["Bid Description"].dropna().astype(str).unique().tolist())
            if "Bid Description" in df1.columns else []
        )
        sel_bid = st.multiselect(
            "Bid Description", options=bid_opts, default=bid_opts,
            key=f"ms_bid_{_sel_hash(sel_company)}",
        )

    df2 = (
        df1[df1["Bid Description"].astype(str).isin(sel_bid)]
        if sel_bid and "Bid Description" in df1.columns
        else df1.iloc[0:0]
    )

    with f3:
        round_opts = (
            sorted(df2["Round"].dropna().astype(str).unique().tolist())
            if "Round" in df2.columns else []
        )
        sel_round = st.multiselect(
            "Round", options=round_opts, default=round_opts,
            key=f"ms_round_{_sel_hash(sel_company, sel_bid)}",
        )

    df3 = (
        df2[df2["Round"].astype(str).isin(sel_round)]
        if sel_round and "Round" in df2.columns
        else df2.iloc[0:0]
    )

    with f4:
        format_opts = (
            sorted(df3["Format"].dropna().astype(str).unique().tolist())
            if "Format" in df3.columns else []
        )
        sel_format = st.multiselect(
            "Format", options=format_opts, default=format_opts,
            key=f"ms_format_{_sel_hash(sel_company, sel_bid, sel_round)}",
        )

    filtered_df = (
        df3[df3["Format"].astype(str).isin(sel_format)]
        if sel_format and "Format" in df3.columns
        else df3.iloc[0:0]
    )

    empty = [
        name for name, vals in [
            ("Company", sel_company), ("Bid Description", sel_bid),
            ("Round",   sel_round),   ("Format",          sel_format),
        ]
        if not vals
    ]
    if empty:
        st.warning(f"⚠️ Please select at least one value for: **{', '.join(empty)}**")
        return None

    st.markdown(f"**{len(filtered_df):,} records** match the current filter criteria.")

    return _FilterResult(
        df=filtered_df,
        sel_company=sel_company,
        sel_bid=sel_bid,
        sel_round=sel_round,
    )


def _render_rfp_summary(result: _FilterResult) -> None:
    """Render the RFP Summary section: optional KPI metrics row + aggregated table."""
    st.markdown("### 📊 RFP Summary")
    st.markdown(
        "Item-level PCM, GP, and detailed price builds can be extracted from the "
        "**\"Detailed Item-Level Data\"** section below. "
        "Note the % here is a comparison against FOB Revenue."
    )

    filtered_df = result.df
    available_group = [c for c in _GROUP_COLS      if c in filtered_df.columns]
    available_sum   = [c for c in _FINANCIAL_COLS  if c in filtered_df.columns]

    # KPI row — only meaningful when a single bid/round is in focus
    if (
        len(result.sel_company) == 1
        and len(result.sel_bid)  == 1
        and len(result.sel_round) == 1
        and available_sum
    ):
        total_lbs = filtered_df["Volume (lbs)"].sum()     if "Volume (lbs)"     in filtered_df.columns else None
        total_fob = filtered_df["FOB Revenue $/Yr"].sum() if "FOB Revenue $/Yr" in filtered_df.columns else None
        total_pcm = filtered_df["PCM $/Yr"].sum()         if "PCM $/Yr"         in filtered_df.columns else None
        total_gp  = filtered_df["GP $/Yr"].sum()          if "GP $/Yr"          in filtered_df.columns else None

        pcm_pct = (total_pcm / total_fob * 100) if total_fob else None
        gp_pct  = (total_gp  / total_fob * 100) if total_fob else None

        if "Status" in filtered_df.columns:
            unique_statuses = filtered_df["Status"].dropna().astype(str).unique().tolist()
            status_display  = " / ".join(sorted(unique_statuses)) if unique_statuses else "—"
        else:
            status_display = "—"

        m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
        with m1: st.metric("Total Pounds",           _fmt_volume(total_lbs))
        with m2: st.metric("Total FOB Revenue $/Yr", _fmt_currency(total_fob))
        with m3: st.metric("Total PCM $/Yr",         _fmt_currency(total_pcm))
        with m4: st.metric("Total GP $/Yr",          _fmt_currency(total_gp))
        with m5: st.metric("PCM %",                  _fmt_pct(pcm_pct))
        with m6: st.metric("GP %",                   _fmt_pct(gp_pct))
        with m7: st.metric("Status",                 status_display)
        st.markdown("")

    if not (available_group and available_sum):
        st.warning("Not enough columns available to build the RFP Summary table.")
        return

    summary_df = (
        filtered_df
        .groupby(available_group, as_index=False, dropna=False)[available_sum]
        .sum()
    )
    summary_display = _apply_display_formats(summary_df, available_sum)

    if "Price Implement Time" in filtered_df.columns:
        pit = filtered_df.groupby(available_group, as_index=False)["Price Implement Time"].first()
        summary_display = summary_display.merge(pit, on=available_group, how="left")
    else:
        summary_display["Price Implement Time"] = ""

    st.dataframe(summary_display, use_container_width=True, hide_index=True)
    st.download_button(
        label="⬇️ Download RFP Summary (CSV)",
        data=_to_csv_bytes(summary_df),
        file_name=f"rfp_summary_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        key="download_summary",
    )


def _render_detail_table(filtered_df: pd.DataFrame) -> None:
    """Render the Detailed Item-Level Data section."""
    st.markdown("### 📋 Detailed Item-Level Data")
    st.caption("Full extract of the CSV filtered by the search criteria above.")

    available_sum = [c for c in _FINANCIAL_COLS if c in filtered_df.columns]

    detail_download = filtered_df.copy()
    for col in filtered_df.columns:
        if "$/ea" in col.lower() and pd.api.types.is_numeric_dtype(detail_download[col]):
            detail_download[col] = detail_download[col].round(4)

    detail_display = _apply_display_formats(detail_download, available_sum)

    st.dataframe(detail_display, use_container_width=True, hide_index=True)
    st.download_button(
        label="⬇️ Download Detailed Table (CSV)",
        data=_to_csv_bytes(detail_download),
        file_name=f"bid_asset_detail_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        key="download_detail",
    )


# ── 7. Entry point ────────────────────────────────────────────────────────────

def render() -> None:
    """Render the Bid Asset Intelligence page.

    Orchestrates the four page sections in order.  Each section is self-contained:
    it reads Streamlit widget state, computes its own data slice, and renders its
    own UI.  render() itself carries no business logic.
    """
    apply_custom_css()

    st.markdown(
        '<h1 class="main-header">Bid Asset Intelligence</h1>',
        unsafe_allow_html=True,
    )

    st.markdown("""
### Welcome

Use this page to analyze historical trends since December 2025. These insights drive post-mortem analysis
and sharpen future bid strategies. Key resources include:

- **Visualizations:** Charts for bid comparisons.
- **RFP Summary:** High-level tracking of program size, status and key financials.
- **Granular Data:** Detailed breakdowns of item-level PCM, GP and price builds.
""")
    st.markdown("---")

    st.markdown("### 📤 Upload Bid Asset CSV File")
    st.markdown(f"Upload Bid Asset CSV export saved in the [SharePoint Folder]({_SHAREPOINT_URL})")

    uploaded_file = st.file_uploader(
        "Select Bid Asset CSV", type=["csv"], key="bid_asset_uploader"
    )
    if uploaded_file is None:
        st.info("👆 Upload a CSV file above to unlock the search and analysis tables.")
        return

    raw_df = _load_and_normalise(uploaded_file)
    if raw_df is None:
        return

    st.success(f"✅ File loaded — **{len(raw_df):,} rows**, **{len(raw_df.columns)} columns**")
    st.markdown("---")

    # Sorted month list computed once and shared by both sliders.
    all_months_sorted: list[str] = (
        sorted(raw_df["Month"].dropna().astype(str).unique().tolist(), key=_month_sort_key)
        if "Month" in raw_df.columns else []
    )

    _render_bid_overview(raw_df, all_months_sorted)
    st.markdown("---")

    result = _render_search_filters(raw_df, all_months_sorted)
    if result is None:
        return

    st.markdown("---")
    _render_rfp_summary(result)
    st.markdown("---")
    _render_detail_table(result.df)
