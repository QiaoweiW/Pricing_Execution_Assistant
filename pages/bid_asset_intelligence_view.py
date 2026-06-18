"""Bid Assistant page view.

The page pulls bid-asset CSVs directly from the Microsoft Fabric Lakehouse,
renders four analytical sections, and lets operators edit the Item-level
Details and publish their changes back to the lakehouse.

Sections
--------
1. Types & constants     (``_FilterResult``, ``_GROUP_COLS``, status colour
                          rules, ``_CHART_FONT``, ``_LAKEHOUSE_FOLDER``,
                          ``_OVERVIEW_TABLE_COLS``, ``_ROW_ID_COL`` and
                          session-state key helpers).
2. Formatting helpers    (``_fmt_currency``, ``_fmt_volume``, ``_fmt_pct``,
                          ``_to_csv_bytes``, ``_apply_display_formats``).
3. Data helpers          (``_month_sort_key``, ``_sel_hash``,
                          ``_filter_by_month_range``).
4. Chart helpers         (``_round_num``, ``_make_bid_label``,
                          ``_status_color``, ``_prepare_chart_data``,
                          ``_build_overview_chart``).
5. Page sections         (``_multiselect_filter``, ``_render_overview_table``,
                          ``_render_bid_overview``, ``_render_search_filters``,
                          ``_render_rfp_summary``, ``_render_program_tracker``,
                          ``_render_editable_item_details``).
6. Entry point           (``render``).

All Lakehouse I/O, schema normalisation, and currency parsing live in
``data_sources.bid_asset_store``; this module is presentation only.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import NamedTuple, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from data_sources import bid_asset_store as _bid_store
from data_sources import rfp_pnl_store as _rfp_pnl_store
from data_sources.bid_asset_store import BidAssetStoreError
from utils import fabric_signin_widget
from utils.embed_helpers import render_embedded_resource, to_powerbi_embed_url
from utils.ui_helpers import apply_custom_css

# Finance P&L Power BI report URL. Kept as a module-level constant so
# it can be updated in one place if Finance ever republishes the report
# under a new ID. Migrated from the now-removed RFP Financial Analysis
# view so the embed continues to live inside the Bid Assistant.
_FINANCE_PNL_REPORT_URL = (
    "https://app.powerbi.com/groups/me/reports/"
    "ff2d4ea3-d3e4-4a14-945d-998bb7a7f03d/ef0f92c30868546c301b"
    "?ctid=c9a55ced-3b88-408c-ab99-8db8b9b90286&experience=power-bi"
)

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


# Financial columns are owned by ``bid_asset_store`` (single source of truth
# for the data shape). This alias keeps the page's call sites short.
_FINANCIAL_COLS: tuple[str, ...] = _bid_store.FINANCIAL_COLS

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

# Ordered (source_col, display_label) pairs for the Bid Overview summary table.
# Only columns present in the aggregated DataFrame are rendered; missing ones are
# silently skipped, so the table stays valid regardless of uploaded CSV schema.
_OVERVIEW_TABLE_COLS: list[tuple[str, str]] = [
    ("Company",         "Company"),
    ("Bid Description", "Bid Description"),
    ("Format",          "Format"),
    ("Size",            "Size"),
    ("Volume (lbs)",    "Total Pounds"),
    ("Round",           "Round"),
    ("PCM $/lb",        "PCM $/lbs"),
    ("Status",          "Status"),
]

# Fabric Lakehouse folder that stores bid-asset CSV files.
# Uses the shared [fabric_htst] secrets section (workspace + lakehouse).
# Files are listed from this path and the most recently modified CSV is
# loaded automatically.  Users can override the selection via the picker
# that appears when more than one CSV is present in the folder.
_LAKEHOUSE_FOLDER: str = "Program_Bid_Management"

# Internal column injected into the in-memory copy of the bid DataFrame to
# give every row a stable identifier ``st.data_editor`` can use to map edits
# back to their source rows. Hidden from the user via ``column_order``.
_ROW_ID_COL: str = "__row_id"

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

# Month formats we attempt before falling back to pandas' flexible parser.
# Ordered by frequency in real source data so common cases short-circuit fast.
_MONTH_FORMATS: tuple[str, ...] = (
    "%b %Y", "%B %Y", "%m/%d/%Y", "%m/%Y", "%Y-%m-%d", "%Y-%m",
)


def _month_sort_key(m_str: str) -> datetime:
    """Parse any common Month representation into a datetime for sort/compare.

    Handles both the canonical ``"Mon YYYY"`` (e.g. ``"Mar 2026"``) AND the
    raw ``"M/D/YYYY"`` (e.g. ``"3/1/2026"``) forms that may live untouched in
    the Lakehouse CSV. Returns ``datetime.min`` on failure so unparseable
    values sort to the front without raising.
    """
    s = str(m_str).strip()
    for fmt in _MONTH_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return pd.to_datetime(s, errors="raise").to_pydatetime()
    except Exception:
        return datetime.min


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


def _filter_by_month_range(
    df: pd.DataFrame,
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
) -> pd.DataFrame:
    """Return rows whose Month falls within [start_dt, end_dt] (inclusive).

    Uses pandas' flexible date parser (no format hint) so the same code works
    whether the source CSV stores Month as ``"Mar 2026"`` (canonical label) or
    ``"3/1/2026"`` (raw spreadsheet date). Unparseable values become ``NaT``
    and are silently excluded from the result.
    """
    if "Month" not in df.columns or start_dt is None or end_dt is None:
        return df
    month_dts = pd.to_datetime(df["Month"], errors="coerce")
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
    4. Attach the modal Status, Format, and Size (most common value in the round).
    5. Attach the display colour and x-axis label.

    Format and Size are captured here so the Bid Overview summary table can
    share exactly this DataFrame without any separate aggregation pass.

    Returns an empty DataFrame when required columns are absent.
    """
    if df.empty or "Round" not in df.columns:
        return pd.DataFrame()

    group_keys = [c for c in ["Company", "Bid Description"] if c in df.columns]
    if not group_keys:
        return pd.DataFrame()

    work = df.copy()
    work["_round_num"] = work["Round"].apply(_round_num)

    # Identify the maximum round number per bid, then keep only those rows.
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

    # Attach modal (most-common) value for every categorical column of interest.
    # Status drives chart colouring; Format and Size populate the summary table.
    modal_cols = [c for c in ["Status", "Format", "Size"] if c in work.columns]
    if modal_cols:
        modal_agg = (
            work.groupby(agg_keys)[modal_cols]
            .agg(lambda x: x.mode().iloc[0] if not x.mode().empty else "Unknown")
            .reset_index()
        )
        agg = agg.merge(modal_agg, on=agg_keys, how="left")

    if "Status" not in agg.columns:
        agg["Status"] = "Unknown"

    if {"Volume (lbs)", "PCM $/Yr"}.issubset(agg.columns):
        safe_vol        = agg["Volume (lbs)"].replace(0, float("nan"))
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


# ── 5. Editor session-state helpers ───────────────────────────────────────────
#
# Lakehouse I/O lives in ``data_sources.bid_asset_store`` — this section holds
# only the per-file session-state keys that back the editable Item-level grid.

def _session_df_key(file_path: str) -> str:
    return f"_bid_edit_df::{file_path}"


def _session_etag_key(file_path: str) -> str:
    return f"_bid_edit_etag::{file_path}"


def _session_dirty_key(file_path: str) -> str:
    return f"_bid_edit_dirty::{file_path}"


def _initialise_edit_state(file_path: str, df: pd.DataFrame, etag: Optional[str]) -> None:
    """Seed per-file edit state in session with a stable row identifier."""
    keyed = df.copy()
    keyed[_ROW_ID_COL] = range(len(keyed))
    st.session_state[_session_df_key(file_path)] = keyed
    st.session_state[_session_etag_key(file_path)] = etag
    st.session_state[_session_dirty_key(file_path)] = False


# ── 6. Page sections ──────────────────────────────────────────────────────────

def _multiselect_filter(
    label: str,
    col: str,
    pool_df: pd.DataFrame,
    *,
    key: str,
    options_df: Optional[pd.DataFrame] = None,
) -> tuple[list[str], pd.DataFrame]:
    """Render a multiselect widget and return *(selection, filtered_df)*.

    This helper eliminates the repetitive options-derive → render → apply-filter
    pattern that would otherwise appear once per cascading filter widget.

    Parameters
    ----------
    label      : Widget label shown to the user.
    col        : Column to filter on.
    pool_df    : DataFrame to filter; also the options source unless *options_df*
                 is provided.
    key        : Unique Streamlit widget key (callers embed _sel_hash for
                 cascading reset behaviour).
    options_df : When supplied, options come from this DataFrame instead of
                 *pool_df*.  Used when a root filter should always list all known
                 values (e.g. Company drawn from the full dataset) while the
                 actual row filtering operates on a month-scoped subset.

    Return contract
    ---------------
    - Column absent in *pool_df*: selection=[], filtered_df=pool_df (pass-through).
    - User cleared all selections: selection=[], filtered_df=empty DataFrame.
    - Normal: selection=chosen values, filtered_df=rows matching selection.
    """
    src  = options_df if options_df is not None else pool_df
    opts = (
        sorted(src[col].dropna().astype(str).unique().tolist())
        if col in src.columns else []
    )

    # Defensive: when widget keys are stable across reruns (no cascading-hash
    # reset), the user's prior selection may contain values that no longer
    # appear in the current option pool — e.g. because an upstream filter has
    # narrowed the available options. Streamlit raises when the persisted
    # selection isn't a subset of `options`, so we sanitise session state in
    # place before the widget renders. This is the fix that makes
    # Bid Description / Round / Format dropdowns interactable across reruns.
    if key in st.session_state:
        try:
            current = [v for v in st.session_state[key] if v in opts]
        except TypeError:
            current = []
        if current != st.session_state[key]:
            st.session_state[key] = current

    if not opts:
        # Column is absent or has no values — show a disabled placeholder so
        # the user sees *why* the dropdown is empty rather than thinking the
        # widget is broken.
        st.multiselect(
            label,
            options=[],
            default=[],
            key=key,
            disabled=True,
            placeholder=f"No {label.lower()} values available",
        )
        return [], pool_df

    sel = st.multiselect(label, options=opts, default=opts, key=key)

    if not sel:
        # User explicitly cleared the widget; return an empty frame so downstream
        # sections know there is no valid selection rather than showing all rows.
        return sel, pool_df.iloc[0:0]

    filtered = (
        pool_df[pool_df[col].astype(str).isin(sel)]
        if col in pool_df.columns else pool_df
    )
    return sel, filtered


def _render_overview_table(chart_agg: pd.DataFrame) -> None:
    """Render the Bid Overview summary table above the chart.

    Each row corresponds to exactly one bar on the chart — same data, same
    aggregation (final round only, volumes summed, PCM $/lb derived).  The
    table updates automatically whenever *chart_agg* changes because it is
    derived directly from _prepare_chart_data output.

    Columns: Company, Bid Description, Format, Size, Total Pounds, Round,
             PCM $/lbs, Status  (columns absent from *chart_agg* are omitted).
    """
    # Build the display DataFrame from the ordered column map.
    src_cols   = [src for src, _   in _OVERVIEW_TABLE_COLS if src in chart_agg.columns]
    disp_names = [lbl for src, lbl in _OVERVIEW_TABLE_COLS if src in chart_agg.columns]

    if not src_cols:
        return  # nothing to show if aggregation produced no relevant columns

    display = chart_agg[src_cols].copy()
    display.columns = disp_names

    if "Total Pounds" in display.columns:
        display["Total Pounds"] = display["Total Pounds"].apply(_fmt_volume)

    if "PCM $/lbs" in display.columns:
        display["PCM $/lbs"] = display["PCM $/lbs"].apply(
            lambda v: f"${v:.2f}" if pd.notna(v) else "—"
        )

    st.dataframe(display, use_container_width=True, hide_index=True)


def _render_bid_overview(raw_df: pd.DataFrame, all_months_sorted: list[str]) -> None:
    """Render the Bid Overview section.

    Controls (top to bottom)
    ------------------------
    1. Month-range slider  — first filter; narrows the data pool for all below.
    2. Categorical filters — cascading: Format → Size → Referenced Item Description.
       Each filter's options are derived from the rows that survive all filters
       above it, so selecting a Format instantly restricts which Sizes appear,
       and selecting a Size restricts which Referenced Item Descriptions appear.
    3. Summary table       — one row per bid (final round only), mirroring every
       bar in the chart below.  Updates in sync with every filter change.
    4. Overlay toggles     — show/hide PCM $/Yr (red) and PCM $/lb (black) dots.
    5. Chart              — Plotly combo chart driven by the same aggregated data.

    All filter layers feed the same chart_base dataset before aggregation, so
    every metric in both the table and the chart reflects the full filter state.
    """
    st.markdown("### 📈 Bid Volume & PCM Overview")
    st.caption(
        "Volume (lbs) bars are always shown, colour-coded by bid outcome "
        "(green = Accepted, gray = Rejected, blue = Other). "
        "Use the overlay toggles to add financial rate metrics on separate axes. "
        "All values update dynamically with every filter change."
    )

    # ── Month-range slicer (full width) ──────────────────────────────────────
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

    # Month-filtered pool — basis for all categorical cascades.  Computed here
    # (between the slider and the column widgets) so Format options already
    # reflect the selected date range on every rerun.
    df_chart_month = _filter_by_month_range(raw_df, chart_start_dt, chart_end_dt)

    # ── Cascading categorical filters — Format → Size → Referenced Item ───────
    # _multiselect_filter renders the widget and applies the filter in one call.
    # _sel_hash-keyed widgets auto-reset to "all" whenever a parent changes.
    cf1, cf2, cf3 = st.columns(3)
    with cf1:
        sel_chart_fmt, df_after_fmt = _multiselect_filter(
            "Format", "Format", df_chart_month, key="chart_format"
        )
    with cf2:
        sel_chart_size, df_after_size = _multiselect_filter(
            "Size", "Size", df_after_fmt,
            key=f"chart_size_{_sel_hash(sel_chart_fmt)}",
        )
    with cf3:
        _, chart_base = _multiselect_filter(
            "Referenced Item Description", "Referenced Item Description", df_after_size,
            key=f"chart_ref_item_{_sel_hash(sel_chart_fmt, sel_chart_size)}",
        )

    # ── Aggregate once; table and chart share the same result ─────────────────
    chart_agg = _prepare_chart_data(chart_base)

    # ── Summary table — appears between filters and chart ─────────────────────
    if chart_agg.empty:
        st.info("No data available for the selected chart filters.")
        return

    st.markdown(
        "**Bid Summary** — one row per bid at its final round, matching each bar in the chart below:"
    )
    _render_overview_table(chart_agg)

    # ── Overlay metric toggles ─────────────────────────────────────────────────
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

    # ── Chart ─────────────────────────────────────────────────────────────────
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
    st.markdown("### 🔍 RFP Program-level Details")
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

    # ── Cascading dropdowns: Company → Bid Description → Round → Format ────────
    # Company options always come from the full dataset (raw_df) so known
    # companies are never hidden by the month filter, while actual row filtering
    # still operates on the month-scoped pool (df_month).
    # ── Cascading dropdowns with STABLE widget keys ───────────────────────────
    # Using stable (non-hashed) keys lets the user pick Bid Description / Round
    # / Format independently without the widget being recreated every time a
    # parent's selection changes. _multiselect_filter() above prunes any stale
    # values from session_state before each render, so an upstream narrowing
    # never throws on a now-invalid persisted selection.
    f1, f2, f3, f4 = st.columns(4)
    with f1:
        sel_company, df1 = _multiselect_filter(
            "Company", "Company", df_month,
            options_df=raw_df, key="ms_company",
        )
    with f2:
        sel_bid, df2 = _multiselect_filter(
            "Bid Description", "Bid Description", df1,
            key="ms_bid_desc",
        )
    with f3:
        sel_round, df3 = _multiselect_filter(
            "Round", "Round", df2,
            key="ms_round",
        )
    with f4:
        sel_format, filtered_df = _multiselect_filter(
            "Format", "Format", df3,
            key="ms_format",
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
    st.markdown("### 📊 RFP Program-level Table")
    st.markdown(
        "Item-level PCM, GP, and detailed price builds can be extracted from the "
        "**\"Item-level Details\"** section below. "
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


# ── Editable Item-level Details ───────────────────────────────────────────────

# Cascading order used by the Item-level Details quick filters. The DataFrame
# is narrowed by each filter in turn, so the options offered by each downstream
# multiselect are restricted to values that co-occur with the upstream picks.
_EDIT_FILTER_COLS: tuple[str, ...] = (
    "Company",
    "Bid Description",
    "Round",
    "Status",
    "Referenced Item",
)


def _edit_filter_key(col_name: str) -> str:
    """Stable session-state key for an Item-level Details quick-filter widget."""
    return f"edit_filter_{col_name}"


def _render_editable_item_details(file_path: str) -> None:
    """Render the editable item-level table with right-aligned publish action.

    Layout (top → bottom)
    ---------------------
    1. Section heading + caption + (right-aligned) Refresh & Publish to Fabric button.
    2. Optional "unsaved edits" banner.
    3. Cascading quick-filter row with Select All / Clear All shortcuts. Order:
       Company → Bid Description → Round → Status → Referenced Item.
    4. The editable ``st.data_editor`` table itself, with:
         • ``Bid Description`` pinned (frozen) to the left while scrolling.
         • Internal ``__row_id`` column hidden from view via ``column_order``.
    5. Download CSV button.

    Editing semantics
    -----------------
    Edits are merged into ``st.session_state`` keyed by file path on every
    rerun, so the user can edit many rows across many filter views without
    losing any in-progress changes. The page reruns triggered by data-editor
    interactions only rebuild the visible widgets; they never reset edits,
    filter selections, or scroll position. Only the **Refresh & Publish to
    Fabric** button writes to the lakehouse and forces a full re-sync.

    On publish:
        a. Overwrite the selected file in the Fabric Lakehouse (ETag-aware).
        b. Re-read the file from the lakehouse to capture the new ETag.
        c. ``st.rerun()`` so every other section on this page (Bid Overview,
           RFP Program-level Details, Program Implementation Tracker) reflects
           the lakehouse source-of-truth in the same interaction.
    """
    df_key = _session_df_key(file_path)
    dirty_key = _session_dirty_key(file_path)
    etag_key = _session_etag_key(file_path)
    edit_df = st.session_state[df_key]

    # ── Header row: title (left) + Refresh & Publish button (right) ───────────
    head_col, action_col = st.columns([5, 2])
    with head_col:
        st.markdown("### 📋 Item-level Details")
        st.caption(
            "Edit any cell directly. Filters cascade left-to-right; you can edit "
            "rows in any filter view without losing changes from other views. "
            "Click **Refresh & Publish to Fabric** when you are ready to save all "
            "edits to the lakehouse and re-sync every section on this page."
        )
    with action_col:
        # Vertical spacer keeps the button visually aligned with the table edge
        # (matches the typical Streamlit baseline of caption + heading).
        st.markdown("<div style='height: 0.75rem;'></div>", unsafe_allow_html=True)
        publish_clicked = st.button(
            "🔄 Refresh & Publish to Fabric",
            key=f"publish_bid_{hash(file_path)}",
            type="primary",
            help="Save current edits to the Fabric Lakehouse and re-sync every section on this page.",
            use_container_width=True,
        )

    if st.session_state.get(dirty_key):
        st.warning("Unsaved edits detected. Click **Refresh & Publish to Fabric** to persist changes.")

    # ── Bulk-filter shortcuts: Select All / Clear All ────────────────────────
    # Manipulating session state in the button handlers is safe because
    # Streamlit auto-reruns after a button click; the multiselects below
    # render with the freshly-applied state on that next pass.
    btn_all, btn_clear, btn_spacer = st.columns([1, 1, 6])
    with btn_all:
        if st.button(
            "✅ Select All",
            key="edit_filters_select_all",
            help="Reset every Item-level filter to include all available values.",
            use_container_width=True,
        ):
            for col_name in _EDIT_FILTER_COLS:
                st.session_state.pop(_edit_filter_key(col_name), None)
    with btn_clear:
        if st.button(
            "✖ Clear All",
            key="edit_filters_clear_all",
            help="Clear every Item-level filter so the grid shows zero rows.",
            use_container_width=True,
        ):
            for col_name in _EDIT_FILTER_COLS:
                st.session_state[_edit_filter_key(col_name)] = []

    # ── Cascading quick filters ───────────────────────────────────────────────
    # Each filter's option pool is derived from the rows that survived the
    # upstream filters, so picking a Company instantly narrows the Bid
    # Description options, picking a Bid Description narrows Round, etc.
    # ``_multiselect_filter`` prunes stale session-state values before each
    # render, so cascading narrows never throw on invalid persisted selections.
    fcols = st.columns(len(_EDIT_FILTER_COLS))
    filtered = edit_df
    for col_name, col_slot in zip(_EDIT_FILTER_COLS, fcols):
        with col_slot:
            _sel, filtered = _multiselect_filter(
                col_name,
                col_name,
                filtered,
                key=_edit_filter_key(col_name),
            )

    # ── Editable data grid ────────────────────────────────────────────────────
    # Display order: Bid Description first (pinned), then the rest of the file's
    # columns in their original order. The internal `__row_id` column stays in
    # the DataFrame so we can map edits back to the source rows, but it is
    # excluded from `column_order` to keep it visually hidden from the user.
    other_cols = [c for c in edit_df.columns if c not in (_ROW_ID_COL, "Bid Description")]
    visible_cols = (["Bid Description"] if "Bid Description" in edit_df.columns else []) + other_cols

    column_config: dict[str, object] = {}
    if "Bid Description" in visible_cols:
        column_config["Bid Description"] = st.column_config.Column(
            label="Bid Description",
            help="Frozen column — stays in view as you scroll horizontally.",
            pinned=True,
        )

    editor_frame = filtered[[_ROW_ID_COL] + visible_cols].copy()
    edited_frame = st.data_editor(
        editor_frame,
        key=f"item_details_editor_{hash(file_path)}",
        use_container_width=True,
        hide_index=True,
        disabled=[_ROW_ID_COL],
        column_order=visible_cols,  # `__row_id` is omitted → visually hidden.
        column_config=column_config,
        num_rows="fixed",
    )

    # Merge edits back into session state (only the columns the user can see;
    # `__row_id` is the merge key and remains read-only).
    if not edited_frame.empty:
        base = edit_df.set_index(_ROW_ID_COL)
        patch = edited_frame.set_index(_ROW_ID_COL)
        for col in visible_cols:
            if col in patch.columns:
                base.loc[patch.index, col] = patch[col]
        merged = base.reset_index()
        if not merged.equals(edit_df):
            st.session_state[df_key] = merged
            st.session_state[dirty_key] = True

    # ── Publish: write → re-read from lakehouse → rerun ───────────────────────
    # Runs AFTER applying in-run editor patches, so the push always uses the
    # most recent user edits visible in this render.
    if publish_clicked:
        publish_df = st.session_state[df_key].drop(columns=[_ROW_ID_COL], errors="ignore")
        try:
            _bid_store.overwrite_bid_file(
                file_path,
                publish_df,
                etag=st.session_state.get(etag_key),
            )
            # Force re-read from lakehouse so every downstream section on this
            # page (Program Tracker, RFP table, Bid Overview) reflects the
            # authoritative source-of-truth on the very next render.
            source_df, source_etag = _bid_store.read_bid_file(file_path)
        except BidAssetStoreError as exc:
            st.error(
                f"Could not publish edits to Fabric Lakehouse: {exc}\n\n"
                "Tip: Reload from Lakehouse and re-apply your edits if the source file "
                "was updated by another user."
            )
        else:
            _initialise_edit_state(file_path, source_df, source_etag)
            st.success("Published and re-synced from Fabric Lakehouse.")
            st.rerun()

    st.download_button(
        label="⬇️ Download Current Item-level Details (CSV)",
        data=_to_csv_bytes(st.session_state[df_key].drop(columns=[_ROW_ID_COL], errors="ignore")),
        file_name=f"bid_asset_detail_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        key=f"download_detail_{hash(file_path)}",
    )


# ── 7. Pricing Implementation Tracker ─────────────────────────────────────────

_TRACKER_SOURCE_COLS = [
    "Bid Description",
    "Referenced Item",
    "Referenced Item Description",
    "Month",
    "Variable vs Fixed Pricing",
    "Brand",
    "Price Implement Time",
]

_TRACKER_DISPLAY_COLS = {
    "Bid Description": "Bid Description",
    "Referenced Item": "Referenced Item",
    "Referenced Item Description": "Referenced Item Description",
    "Month": "Month",
    "Variable vs Fixed Pricing": "Variable vs Fixed Pricing",
    "Brand": "Brand",
    "Price Implement Time": "Price Implementation Time",
}


def _render_program_tracker(raw_df: pd.DataFrame) -> None:
    """Render Program Implementation Tracker with required priority ordering."""
    st.markdown("### 🗂️ Program Implementation Tracker")
    st.caption(
        "Accepted bids only. Sorted by Price Implementation Status priority "
        "(Not Started first), then Price Implementation Time descending."
    )
    tracker_df = _bid_store.build_program_tracker(raw_df)
    if tracker_df.empty:
        st.info("No rows available for Program Implementation Tracker.")
        return

    # Friendlier display labels — internal column name stays canonical
    # (matches the source CSV) for round-trip safety; the rename here is
    # cosmetic only.
    display_status_label = "Price Implementation Status"
    display = tracker_df.rename(
        columns={
            "Price Implement Time":   "Price Implementation Time",
            _bid_store.COL_STATUS:    display_status_label,
        }
    )

    def _status_font_color(val: object) -> str:
        # Use the centralised canonical mapping so spelling variants like
        # "Not-started" (with a hyphen) and "start soon" (lower-case) still
        # trigger the red emphasis.
        if (
            _bid_store.status_is_not_started(val)
            or _bid_store.status_is_start_soon(val)
        ):
            return "color: #d32f2f; font-weight: 600;"
        return ""

    # Use ``Styler.map`` (the elementwise-CSS API in pandas 2.1+). The older
    # ``Styler.applymap`` alias was removed in pandas 3.0, so Streamlit Cloud
    # — which often installs the latest pandas — raised AttributeError here.
    # ``Styler.map`` is also a no-op when the subset column is absent, so this
    # call is safe even on legacy CSVs that never had the status column.
    styled = display.style.map(_status_font_color, subset=[display_status_label])
    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
    )


# ── 8. RFP P&L Analysis (new section) ─────────────────────────────────────────

_RFP_SECTION_ROWS: list[tuple[str, bool]] = [
    ("Month", False),
    ("Plant", False),
    ("Target SKU Name", False),
    ("Target SKU lbs per Each", False),
    ("Target SKU Volume (units)", False),
    ("Target SKU Volume (pounds)", False),
    ("Reference SKUs", True),
    ("Milk Reference SKU", False),
    ("Ingredient Reference SKU", False),
    ("Packaging Reference SKU", False),
    ("Conversion Reference SKU", False),
    ("Reference SKU UOMs", True),
    ("Milk Reference SKU lbs per Each", False),
    ("Ingredient Reference SKU lbs per Each", False),
    ("Packaging Reference SKU lbs per Each", False),
    ("Conversion Reference SKU lbs per Each", False),
    ("Category", False),
    ("PCM ($/lbs) - Input Required", True),
    ("PCM $/lbs", False),
    ("Target SKU P&L ($/EA)", True),
    ("FOB Price", False),
    ("Milk", False),
    ("Ingredient", False),
    ("Packaging", False),
    ("Conversion Cost", False),
    ("Cost of Quality", False),
    ("Internal Logistics (Shuttling & WHSE)", False),
    ("Other Cost", False),
    ("Total Costs", False),
    ("PCM", False),
    ("PCM%", False),
    ("GP", False),
    ("GP $/lbs", False),
    ("GP%", False),
    # Retail-side block: Retail Price echoes the user input;
    # Delivered Price = FOB + Freight; Retailer's Margin% closes the
    # loop on retail economics.
    ("Delivered Price", False),
    ("Retail Price", False),
    ("Retailer's Margin%", False),
]

_RFP_NEW_SCENARIO_TOKEN = "__new__"
_RFP_ITEMS_KEY = "_rfp_pnl_items"
_RFP_PATH_KEY = "_rfp_pnl_path"
_RFP_ETAG_KEY = "_rfp_pnl_etag"
_RFP_NAME_KEY = "rfp_pnl_scenario_name"
_RFP_INPUT_PREFIX = "rfp_in_"
_RFP_INPUT_SEEDED_FLAG = "_rfp_pnl_inputs_seeded"

# Field groups used by the per-item input panel.
# A "field" is a metric collected from the user (free text, number, or
# dropdown) — only strict calculated metrics (PCM, PCM%, GP, GP%, GP $/lbs,
# Total Costs, FOB Price, Target SKU Volume (pounds)) are excluded.
_RFP_INPUT_DROPDOWN_MONTH_PLANT: tuple[str, ...] = ("Month", "Plant")
_RFP_INPUT_REF_SKU_FIELDS: tuple[str, ...] = (
    "Milk Reference SKU",
    "Ingredient Reference SKU",
    "Packaging Reference SKU",
    "Conversion Reference SKU",
)
_RFP_INPUT_TEXT_FIELDS: tuple[str, ...] = ("Target SKU Name", "Category")
_RFP_INPUT_NUMERIC_FIELDS: tuple[str, ...] = (
    "Target SKU lbs per Each",
    "Target SKU Volume (units)",
    "Milk Reference SKU lbs per Each",
    "Ingredient Reference SKU lbs per Each",
    "Packaging Reference SKU lbs per Each",
    "Conversion Reference SKU lbs per Each",
    "PCM $/lbs",
    # Retail-side inputs (drive Delivered Price + Retailer's Margin%).
    # Freight may be blank — treated as $0/EA by the calc engine.
    "Retail Price",
    "Freight Cost",
    # Cost overrides — analyst-supplied $/EA values that win over the
    # BOM/Budget default whenever non-blank. The displayed cost cell
    # itself is strictly recomputed (see STRICT_CALC_METRICS) so saved
    # values can never silently mask the calculation.
    "Milk Override",
    "Ingredient Override",
    "Packaging Override",
    "Conversion Cost Override",
    "Cost of Quality Override",
    "Internal Logistics (Shuttling & WHSE) Override",
    # Other Cost has no BOM/Budget calc — its value IS the analyst input.
    "Other Cost",
)
_RFP_INPUT_FIELDS: tuple[str, ...] = (
    *_RFP_INPUT_DROPDOWN_MONTH_PLANT,
    *_RFP_INPUT_TEXT_FIELDS,
    *_RFP_INPUT_NUMERIC_FIELDS,
    *_RFP_INPUT_REF_SKU_FIELDS,
)

# Display-only formatting per metric on the rendered scenario table.
# Per analyst preference, every numeric value is rendered with four
# decimal places so spot-checks against the source CSVs are unambiguous.
_RFP_DECIMALS = 4


def _fmt_money(v) -> str:
    f = _rfp_pnl_store._to_float(v)  # noqa: SLF001 — shared parsing helper
    if f is None:
        return ""
    return (
        f"$({abs(f):,.{_RFP_DECIMALS}f})"
        if f < 0
        else f"${f:,.{_RFP_DECIMALS}f}"
    )


def _fmt_number(v) -> str:
    f = _rfp_pnl_store._to_float(v)  # noqa: SLF001
    return "" if f is None else f"{f:,.{_RFP_DECIMALS}f}"


def _fmt_pct(v) -> str:
    f = _rfp_pnl_store._to_float(v)  # noqa: SLF001
    return "" if f is None else f"{f * 100:,.{_RFP_DECIMALS}f}%"


def _fmt_passthrough(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    return str(v)


# Display formatter for each non-section metric row. Anything not listed
# here falls back to passthrough (e.g. Month, Plant, SKU descriptions).
_RFP_METRIC_FORMATTERS: dict[str, callable] = {
    "Target SKU lbs per Each": _fmt_number,
    "Target SKU Volume (units)": _fmt_number,
    "Target SKU Volume (pounds)": _fmt_number,
    "Milk Reference SKU lbs per Each": _fmt_number,
    "Ingredient Reference SKU lbs per Each": _fmt_number,
    "Packaging Reference SKU lbs per Each": _fmt_number,
    "Conversion Reference SKU lbs per Each": _fmt_number,
    "PCM $/lbs": _fmt_money,
    "FOB Price": _fmt_money,
    "Milk": _fmt_money,
    "Ingredient": _fmt_money,
    "Packaging": _fmt_money,
    "Conversion Cost": _fmt_money,
    "Cost of Quality": _fmt_money,
    "Internal Logistics (Shuttling & WHSE)": _fmt_money,
    "Other Cost": _fmt_money,
    "Total Costs": _fmt_money,
    "PCM": _fmt_money,
    "PCM%": _fmt_pct,
    "GP": _fmt_money,
    "GP $/lbs": _fmt_money,
    "GP%": _fmt_pct,
    "Retail Price": _fmt_money,
    "Delivered Price": _fmt_money,
    "Retailer's Margin%": _fmt_pct,
}


def _rfp_input_key(idx: int, field: str) -> str:
    """Stable session-state key for one item-level input widget."""
    slug = field.lower().replace(" ", "_").replace("$", "dlr").replace("/", "_per_").replace("(", "").replace(")", "").replace(",", "")
    return f"{_RFP_INPUT_PREFIX}{idx}__{slug}"


def _rfp_options_with_current(options: tuple[str, ...], current: object) -> list[str]:
    """Return dropdown options (with a leading blank), preserving custom values."""
    cur = str(current or "").strip()
    out = ["", *options]
    if cur and cur not in out:
        out.append(cur)
    return out


def _rfp_clear_input_state() -> None:
    """Drop every per-item input widget value from session_state."""
    for key in list(st.session_state.keys()):
        if key.startswith(_RFP_INPUT_PREFIX):
            del st.session_state[key]


def _rfp_stringify(value: object) -> str:
    """Coerce any cell value to a clean string for use as a widget value.

    Streamlit ``st.text_input`` and ``st.selectbox`` reject non-string state,
    so every seeded input must be a string. Floats whose fractional part is
    zero are rendered without the trailing ``.0`` for cleaner UX.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        if pd.isna(value):
            return ""
        if value.is_integer():
            return str(int(value))
        return f"{value:.6g}"
    s = str(value).strip()
    return "" if s.lower() in {"nan", "none"} else s


def _rfp_seed_inputs_from_items(items_df: pd.DataFrame) -> None:
    """Initialise widget state for each item row from the committed scenario."""
    _rfp_clear_input_state()
    for idx, row in items_df.iterrows():
        for field in _RFP_INPUT_FIELDS:
            st.session_state[_rfp_input_key(idx, field)] = _rfp_stringify(row.get(field, ""))


_RFP_REF_SKU_INHERITANCE = {
    "Ingredient Reference SKU",
    "Packaging Reference SKU",
    "Conversion Reference SKU",
}


def _rfp_collect_inputs_from_state(item_count: int) -> pd.DataFrame:
    """Build a fresh items DataFrame from current widget state.

    Notes
    -----
    * Ingredient / Packaging / Conversion Reference SKUs default to the Milk
      Reference SKU when blank (mirrors the snapshot's default rule).
    * Strict calculated metrics are intentionally left blank — they are
      always overwritten by ``recompute_items``.
    * Every other metric is taken verbatim from widget state (or "" if
      unset) so that clearing an override field re-instates the BOM /
      Budget default on the next recompute.
    """
    rows: list[dict[str, object]] = []
    for idx in range(item_count):
        milk_ref = _rfp_stringify(st.session_state.get(_rfp_input_key(idx, "Milk Reference SKU")))

        row: dict[str, object] = {_rfp_pnl_store.ITEM_COL: f"Item {idx + 1}"}
        for field in _RFP_INPUT_FIELDS:
            value = _rfp_stringify(st.session_state.get(_rfp_input_key(idx, field)))
            if field in _RFP_REF_SKU_INHERITANCE and not value:
                value = milk_ref
            row[field] = value

        # Strict calc metrics are placeholders — recompute_items overwrites.
        for metric in _rfp_pnl_store.METRIC_COLS:
            row.setdefault(metric, "")

        rows.append(row)

    return pd.DataFrame(rows, columns=[_rfp_pnl_store.ITEM_COL, *_rfp_pnl_store.METRIC_COLS])


def _rfp_render_item_inputs(idx: int, sources: _rfp_pnl_store.RfpPnlSources) -> None:
    """Render the foldable input panel for one Target SKU item.

    The panel exposes every manually-editable metric from the snapshot.
    Strict calculated metrics (Volume in lbs, FOB Price, Total Costs, PCM,
    GP, %s) are NOT collected here — they are derived on Refresh.

    Defaults used by the recompute engine:
        * Reference SKU lbs per Each → PDH Item Net Weight Lbs.
        * Category → PDH Portfolio Major.
        * Milk / Ingredient / Packaging / Conversion costs → BOM lookup.
        * Cost of Quality / Internal Logistics → Budget by Category.
    Each of these stays editable so an analyst can override the model
    output for sensitivity / what-if work.
    """
    name_key = _rfp_input_key(idx, "Target SKU Name")
    item_title = _rfp_stringify(st.session_state.get(name_key)) or f"Item {idx + 1}"

    # ``expanded=True`` for every item: when a saved scenario is loaded,
    # ``_rfp_seed_inputs_from_items`` rehydrates widget state for *every*
    # item, so the analyst should see all inputs at once and not have to
    # hunt through collapsed panels. Streamlit preserves user-driven
    # collapse state across reruns within the same session, so an analyst
    # who explicitly collapses a panel keeps it collapsed.
    with st.expander(f"📦 Item {idx + 1} — {item_title}", expanded=True):
        # ── Identification ────────────────────────────────────────────────
        c1, c2 = st.columns(2)
        with c1:
            st.selectbox(
                "Month",
                options=_rfp_options_with_current(sources.month_options, st.session_state.get(_rfp_input_key(idx, "Month"))),
                key=_rfp_input_key(idx, "Month"),
                help="Matches BOM `Per Beg`.",
            )
        with c2:
            st.selectbox(
                "Plant",
                options=_rfp_options_with_current(sources.plant_options, st.session_state.get(_rfp_input_key(idx, "Plant"))),
                key=_rfp_input_key(idx, "Plant"),
                help="Matches BOM `Plant`.",
            )
        st.text_input("Target SKU Name", key=name_key, placeholder="e.g. Cream Whipping 40% HVY")

        # ── Target SKU (sizing + retail-side inputs) ──────────────────────
        # Retail Price feeds the Retailer's Margin% calc; Freight Cost
        # feeds Delivered Price. Freight is allowed to be blank — the
        # calc treats blank freight as $0/EA so analysts who don't have
        # a freight rate yet can still see a valid Delivered Price that
        # equals FOB.
        st.markdown("**Target SKU**")
        n1, n2 = st.columns(2)
        with n1:
            st.text_input(
                "Target SKU lbs per Each",
                key=_rfp_input_key(idx, "Target SKU lbs per Each"),
                help="Numbers only.",
            )
        with n2:
            st.text_input(
                "Target SKU Volume (units)",
                key=_rfp_input_key(idx, "Target SKU Volume (units)"),
                help="Numbers only. Volume (pounds) is calculated.",
            )
        n3, n4 = st.columns(2)
        with n3:
            st.text_input(
                "Target SKU Retail Price ($/EA)",
                key=_rfp_input_key(idx, "Retail Price"),
                help="Manual input. Drives Retailer's Margin%.",
            )
        with n4:
            st.text_input(
                "Target SKU Freight Cost ($/EA)",
                key=_rfp_input_key(idx, "Freight Cost"),
                help="Manual input. Optional — blank is treated as $0/EA. "
                     "Delivered Price = FOB + Freight.",
            )

        # ── Reference SKUs ─────────────────────────────────────────────────
        # Options cascade off this item's Month + Plant: unique Level-1
        # BOM ``Rule Item Desc`` values that also exist in the PDH file.
        # The list updates on the next rerun after Month / Plant change.
        ref_sku_opts = _rfp_pnl_store.reference_sku_options(
            sources,
            month=st.session_state.get(_rfp_input_key(idx, "Month")),
            plant=st.session_state.get(_rfp_input_key(idx, "Plant")),
        )
        st.markdown(
            "**Reference SKUs**  ·  "
            "*Level-1 BOM items for the selected Month + Plant. "
            "Ingredient / Packaging / Conversion default to the Milk Reference SKU when left blank.*"
        )
        ref1, ref2 = st.columns(2)
        with ref1:
            st.selectbox(
                "Milk Reference SKU",
                options=_rfp_options_with_current(ref_sku_opts, st.session_state.get(_rfp_input_key(idx, "Milk Reference SKU"))),
                key=_rfp_input_key(idx, "Milk Reference SKU"),
            )
            st.selectbox(
                "Ingredient Reference SKU",
                options=_rfp_options_with_current(ref_sku_opts, st.session_state.get(_rfp_input_key(idx, "Ingredient Reference SKU"))),
                key=_rfp_input_key(idx, "Ingredient Reference SKU"),
                help="Leave blank to inherit Milk Reference SKU.",
            )
        with ref2:
            st.selectbox(
                "Packaging Reference SKU",
                options=_rfp_options_with_current(ref_sku_opts, st.session_state.get(_rfp_input_key(idx, "Packaging Reference SKU"))),
                key=_rfp_input_key(idx, "Packaging Reference SKU"),
                help="Leave blank to inherit Milk Reference SKU.",
            )
            st.selectbox(
                "Conversion Reference SKU",
                options=_rfp_options_with_current(ref_sku_opts, st.session_state.get(_rfp_input_key(idx, "Conversion Reference SKU"))),
                key=_rfp_input_key(idx, "Conversion Reference SKU"),
                help="Leave blank to inherit Milk Reference SKU.",
            )

        # ── Reference SKU UOMs (manual entry only) ────────────────────────
        st.markdown(
            "**Reference SKU UOMs**  ·  "
            "*Enter lbs/Each for each Reference SKU. These drive the Milk, "
            "Ingredient, Packaging and Conversion cost formulas. "
            "Ingredient / Packaging / Conversion lbs/Each default to "
            "**Milk Ref lbs/Each** when left blank.*"
        )
        u1, u2, u3, u4 = st.columns(4)
        with u1:
            st.text_input(
                "Milk Ref lbs/Each",
                key=_rfp_input_key(idx, "Milk Reference SKU lbs per Each"),
                help="Required for Milk and Ingredient cost. Numbers only.",
            )
        with u2:
            st.text_input(
                "Ingredient Ref lbs/Each",
                key=_rfp_input_key(idx, "Ingredient Reference SKU lbs per Each"),
                help="Numbers only. Leave blank to inherit Milk Ref lbs/Each.",
            )
        with u3:
            st.text_input(
                "Packaging Ref lbs/Each",
                key=_rfp_input_key(idx, "Packaging Reference SKU lbs per Each"),
                help="Numbers only. Leave blank to inherit Milk Ref lbs/Each.",
            )
        with u4:
            st.text_input(
                "Conversion Ref lbs/Each",
                key=_rfp_input_key(idx, "Conversion Reference SKU lbs per Each"),
                help="Numbers only. Leave blank to inherit Milk Ref lbs/Each.",
            )

        # ── Category & PCM ────────────────────────────────────────────────
        st.markdown("**Category & PCM**")
        cat_col, pcm_col = st.columns(2)
        with cat_col:
            st.text_input(
                "Category",
                key=_rfp_input_key(idx, "Category"),
                help="Leave blank to inherit PDH `Portfolio Major` for the Milk Reference SKU.",
            )
        with pcm_col:
            st.text_input(
                "PCM $/lbs",
                key=_rfp_input_key(idx, "PCM $/lbs"),
                help="Required for FOB Price. Numbers only.",
            )

        # ── Cost overrides ────────────────────────────────────────────────
        # Each cost component has a dedicated override input. The
        # corresponding display row in the scenario table below is
        # ``override if non-blank else BOM/Budget default``. Override
        # values bind to ``<Component> Override`` keys (NOT to the
        # display column itself) so the displayed cell can stay strictly
        # calculated — which means Refresh always reflects the live calc
        # engine and saved values can never silently mask it. Other Cost
        # has no calculated default so its widget IS the value.
        st.markdown(
            "**Cost overrides**  ·  "
            "*Leave blank to use the BOM/Budget default; type a value to override.*"
        )
        co1, co2, co3, co4 = st.columns(4)
        with co1:
            st.text_input(
                "Milk Override",
                key=_rfp_input_key(idx, "Milk Override"),
                help="$/EA. Leave blank to use BOM-derived Milk cost.",
            )
        with co2:
            st.text_input(
                "Ingredient Override",
                key=_rfp_input_key(idx, "Ingredient Override"),
                help="$/EA. Leave blank to use BOM-derived Ingredient cost.",
            )
        with co3:
            st.text_input(
                "Packaging Override",
                key=_rfp_input_key(idx, "Packaging Override"),
                help="$/EA. Leave blank to use BOM-derived Packaging cost.",
            )
        with co4:
            st.text_input(
                "Conversion Cost Override",
                key=_rfp_input_key(idx, "Conversion Cost Override"),
                help="$/EA. Leave blank to use BOM-derived Conversion cost.",
            )
        bo1, bo2, bo3 = st.columns(3)
        with bo1:
            st.text_input(
                "Cost of Quality Override",
                key=_rfp_input_key(idx, "Cost of Quality Override"),
                help="$/EA. Leave blank to use Budget-by-Category default.",
            )
        with bo2:
            st.text_input(
                "Internal Logistics Override",
                key=_rfp_input_key(idx, "Internal Logistics (Shuttling & WHSE) Override"),
                help="$/EA. Leave blank to use Budget-by-Category default.",
            )
        with bo3:
            st.text_input(
                "Other Cost",
                key=_rfp_input_key(idx, "Other Cost"),
                help="$/EA. Direct input — defaults to 0.",
            )


def _rfp_build_display_table(items_df: pd.DataFrame) -> pd.DataFrame:
    """Render-friendly matrix: metric rows × item columns, formatted for display."""
    out = pd.DataFrame({"Metric": [label for label, _ in _RFP_SECTION_ROWS]})

    item_labels: list[str] = []
    used: set[str] = set()
    for idx, raw in enumerate(items_df.get("Target SKU Name", pd.Series([""] * len(items_df))).tolist(), start=1):
        base = str(raw).strip() or f"Item {idx}"
        label = base
        suffix = 2
        while label in used:
            label = f"{base} ({suffix})"
            suffix += 1
        used.add(label)
        item_labels.append(label)

    for col_idx, label in enumerate(item_labels):
        col_vals: list[str] = []
        row = items_df.iloc[col_idx] if col_idx < len(items_df) else pd.Series(dtype=object)
        for metric, is_section in _RFP_SECTION_ROWS:
            if is_section:
                col_vals.append("")
                continue
            raw_value = row.get(metric, "")
            formatter = _RFP_METRIC_FORMATTERS.get(metric, _fmt_passthrough)
            col_vals.append(formatter(raw_value))
        out[label] = col_vals
    return out


def _rfp_style_display(display_df: pd.DataFrame):
    """Apply mild row-level styling so section headers stand out."""
    section_metrics = {label for label, is_section in _RFP_SECTION_ROWS if is_section}

    def _row_style(row: pd.Series) -> list[str]:
        if row.get("Metric") in section_metrics:
            base = "background-color: #f4f6fb; color: #1f2d3d; font-weight: 600;"
        else:
            base = ""
        return [base] * len(row)

    return display_df.style.apply(_row_style, axis=1)


def _render_rfp_pnl_analysis() -> None:
    """Render the scenario-based RFP P&L Analysis section.

    Layout
    ------
    1. Scenario picker (existing CSV under New_Bids/ or new empty scenario).
    2. Per-item input expanders: dropdowns + text inputs only.
    3. "Refresh Scenario" button — commits inputs and recomputes the table.
    4. Scenario table (read-only display, formatted).
    5. Save / Download controls.
    """
    # ── Introduction (collapsed by default — analysts only need this
    #    when reviewing the data-source rules or onboarding) ──────────────
    with st.expander("📘 Introduction & data sources", expanded=False):
        st.markdown(
            """
This builder mirrors the Excel "P&L View (Scenario A)" worksheet so an
analyst can model a Target SKU's $/EA economics before committing to a
bid. Each scenario column is one Target SKU; rows fall into four groups
mirroring the snapshot: identification, reference SKUs, cost overrides,
and computed P&L. Use **Refresh Scenario** to (re)compute the table
after editing inputs.

**Data sources** (all pulled from the Microsoft Fabric Lakehouse,
`Files/`):

- **`BOM/BOM_History_Tracker_tagged.csv`** — drives the per-month, per-plant
  Milk / Ingredient / Packaging / Conversion line items via the tagged
  BOM rules. Cost aggregation uses the per-resource `Ext Cost.1` column.

  *Milk / Ingredient cost rule (2-step):* **Step 1** finds the milk-component
  anchor row — `Per Beg` = Month, `Plant` = Plant, `Rule Item Desc` =
  Milk (or Ingredient) Reference SKU, `Level` = 1, `Tag` = `Milk Component`
  — and captures `Ing-Rsrc Desc` (chain key), `Qty.1` and `Top Recipe`.
  **Step 2** sums the sub-recipe lines: `Per Beg` = Month, `Plant` = Plant,
  `Rule Item Desc` = chain key, `Level` contains "2", `Tag` = `Milk` /
  `Ingredient`, scoped to the same `Top Recipe` so a sibling SKU sharing
  the same sub-recipe doesn't double-count.
  Cost = Σ `Ext Cost.1` × `Qty.1` × Target lbs/Each ÷ Reference SKU
  lbs/Each.

  *Conversion cost rule (1-step):* `Per Beg` = Month, `Plant` = Plant,
  `Rule Item Desc` = Conversion Reference SKU, `Level` = 1, `Tag` is
  not blank, and `Tag` ∉ {`Milk Component`, `Ingredient`, `Milk`,
  `Packaging`, `Depreciation`}; sum `Ext Cost.1`, then × Target lbs/Each
  ÷ Conversion Ref lbs/Each.

  *Reference SKU lbs/Each:* Milk Ref lbs/Each is required; Ingredient /
  Packaging / Conversion all inherit Milk Ref lbs/Each when blank.

  *Reference SKU dropdowns* list the unique Level-1 `Rule Item Desc`
  values for the item's selected Month + Plant (restricted to values
  that also exist in PDH).
- **`BOM/Budget/Budget_Update.csv`** — based on the **current fiscal year
  financial budget**. Cost of Quality and Internal Logistics
  (Shuttling & WHSE) are aggregated by `Category` and multiplied by
  Target SKU lbs per Each.
- **`RO Tracking/Demand Plan/qry_pdh.csv`** — gates the Reference SKU
  dropdown list (a `Rule Item Desc` must also appear in PDH
  `Item Description`) and provides the default `Category`
  (Portfolio Major) for the Milk Reference SKU.

**Cost overrides:** every cost component (Milk, Ingredient, Packaging,
Conversion Cost, Cost of Quality, Internal Logistics) supports a manual
override. Type a number into the override field to replace the
BOM/Budget default; leave it blank to use the calculated value. Override
values are persisted alongside the scenario and rehydrated on load.

Saved scenarios live under
`Files/Program_Bid_Management/New_Bids/<scenario>.csv`.
            """.strip()
        )
    st.markdown("---")

    st.caption(
        "Build one scenario at a time. Each Target SKU is one column. "
        "Defaults are pulled from BOM + Budget + PDH. "
        "Scenarios are saved as CSV under `Files/Program_Bid_Management/New_Bids`."
    )

    try:
        sources = _rfp_pnl_store.load_sources()
        saved = _rfp_pnl_store.list_scenarios()
    except _rfp_pnl_store.RfpPnlStoreError as exc:
        st.error(f"Could not load RFP P&L source data: {exc}")
        return

    # ── Scenario picker ───────────────────────────────────────────────────────
    options: dict[str, str] = {_RFP_NEW_SCENARIO_TOKEN: "➕ New empty scenario"}
    for f in saved:
        options[f.full_path] = f"{f.name}  ({f.last_modified or 'unknown date'})"

    current_path = st.session_state.get(_RFP_PATH_KEY)
    selected_default = current_path if current_path in options else _RFP_NEW_SCENARIO_TOKEN
    selected_path = st.selectbox(
        "Scenario",
        options=list(options.keys()),
        index=list(options.keys()).index(selected_default),
        format_func=lambda x: options[x],
        key="rfp_pnl_scenario_picker",
    )

    if _RFP_ITEMS_KEY not in st.session_state:
        st.session_state[_RFP_ITEMS_KEY] = _rfp_pnl_store.build_empty_scenario(item_count=1)
        st.session_state[_RFP_PATH_KEY] = None
        st.session_state[_RFP_ETAG_KEY] = None
        st.session_state[_RFP_NAME_KEY] = ""
        st.session_state[_RFP_INPUT_SEEDED_FLAG] = False

    if selected_path == _RFP_NEW_SCENARIO_TOKEN:
        if st.session_state.get(_RFP_PATH_KEY) is not None:
            st.session_state[_RFP_ITEMS_KEY] = _rfp_pnl_store.build_empty_scenario(item_count=1)
            st.session_state[_RFP_PATH_KEY] = None
            st.session_state[_RFP_ETAG_KEY] = None
            st.session_state[_RFP_NAME_KEY] = ""
            st.session_state[_RFP_INPUT_SEEDED_FLAG] = False
            st.rerun()
    elif st.session_state.get(_RFP_PATH_KEY) != selected_path:
        try:
            loaded_items, loaded_etag = _rfp_pnl_store.read_scenario(selected_path)
        except _rfp_pnl_store.RfpPnlStoreError as exc:
            st.error(f"Could not load scenario: {exc}")
            return
        st.session_state[_RFP_ITEMS_KEY] = loaded_items
        st.session_state[_RFP_PATH_KEY] = selected_path
        st.session_state[_RFP_ETAG_KEY] = loaded_etag
        st.session_state[_RFP_NAME_KEY] = selected_path.rsplit("/", 1)[-1].removesuffix(".csv")
        st.session_state[_RFP_INPUT_SEEDED_FLAG] = False
        st.rerun()

    committed_items: pd.DataFrame = st.session_state[_RFP_ITEMS_KEY]
    if not st.session_state.get(_RFP_INPUT_SEEDED_FLAG):
        _rfp_seed_inputs_from_items(committed_items)
        st.session_state[_RFP_INPUT_SEEDED_FLAG] = True

    # ── Item input panel ──────────────────────────────────────────────────────
    st.markdown("#### Item-level Inputs")
    # Surface the loaded-scenario name right next to the inputs so the
    # connection between the picker above and the panels below is
    # immediately obvious when the analyst comes back to re-edit.
    loaded_name = st.session_state.get(_RFP_NAME_KEY, "") or ""
    if st.session_state.get(_RFP_PATH_KEY) and str(loaded_name).strip():
        st.caption(
            f"✏️ Editing scenario `{loaded_name}` "
            f"({len(committed_items)} item{'s' if len(committed_items) != 1 else ''}). "
            "Update inputs below and click **Refresh Scenario** to recompute."
        )
    item_count = len(committed_items)

    # Add / remove controls (these reset widget state to keep keys aligned with
    # the new row count; user inputs collected up to this point are persisted
    # via `_RFP_ITEMS_KEY` only after the explicit Refresh click below).
    add_col, remove_col, _spacer = st.columns([1, 2, 5])
    with add_col:
        if st.button("➕ Add Item", key="rfp_pnl_add_item", use_container_width=True):
            new_row = _rfp_pnl_store.build_empty_scenario(item_count=1).iloc[0].to_dict()
            st.session_state[_RFP_ITEMS_KEY] = pd.concat(
                [committed_items, pd.DataFrame([new_row])],
                ignore_index=True,
            )
            st.session_state[_RFP_INPUT_SEEDED_FLAG] = False
            st.rerun()
    with remove_col:
        if item_count > 1:
            remove_idx = st.selectbox(
                "Remove item",
                options=list(range(item_count)),
                format_func=lambda i: f"Item {i + 1}",
                key="rfp_pnl_remove_idx",
            )
            if st.button("🗑 Remove selected item", key="rfp_pnl_remove_btn", use_container_width=True):
                kept = committed_items.drop(index=int(remove_idx)).reset_index(drop=True)
                st.session_state[_RFP_ITEMS_KEY] = kept
                st.session_state[_RFP_INPUT_SEEDED_FLAG] = False
                st.rerun()

    for idx in range(item_count):
        _rfp_render_item_inputs(idx, sources)

    # ── Refresh: commit inputs → recompute scenario ───────────────────────────
    refresh_col, hint_col = st.columns([1, 4])
    with refresh_col:
        refresh_clicked = st.button(
            "🔄 Refresh Scenario",
            key="rfp_pnl_refresh",
            type="primary",
            use_container_width=True,
            help="Apply all current inputs and recalculate the scenario table below.",
        )
    with hint_col:
        st.caption(
            "Inputs above don't affect the scenario table until you click **Refresh Scenario**."
        )

    if refresh_clicked:
        gathered = _rfp_collect_inputs_from_state(item_count)
        st.session_state[_RFP_ITEMS_KEY] = _rfp_pnl_store.recompute_items(gathered, sources)
        st.success("Scenario refreshed.")

    # ── Scenario display (read-only, full height — no internal scroll) ───────
    st.markdown("#### Scenario Table")
    current_items: pd.DataFrame = st.session_state[_RFP_ITEMS_KEY]

    # Prompt the analyst to fill in any required input that's blank. Pure
    # data check — see ``find_missing_required_inputs`` for the rule. We
    # still render the table below so the partially-computed cells are
    # visible (helps confirm what *did* compute when only one item is
    # missing inputs).
    missing_issues = _rfp_pnl_store.find_missing_required_inputs(current_items)
    if missing_issues:
        bullets = "\n".join(
            f"- **{label}** — fill in: {', '.join(f'`{f}`' for f in fields)}"
            for label, fields in missing_issues
        )
        st.warning(
            "⚠️ Some items are missing required inputs — the scenario "
            "table below will show blank cells for FOB / PCM / GP / "
            "Delivered Price / Retailer's Margin% until you fill them "
            "in and click **Refresh Scenario**.\n\n" + bullets
        )

    display_df = _rfp_build_display_table(current_items)
    # Streamlit's default-height ``st.dataframe`` clips after ~10 rows. We
    # size the frame to ``rows × ~35px + header`` so every metric row is
    # visible without an internal scrollbar.
    full_height = max(400, 36 * (len(display_df) + 1) + 4)
    st.dataframe(
        _rfp_style_display(display_df),
        use_container_width=True,
        hide_index=True,
        height=full_height,
    )

    # ── Save / Download controls ──────────────────────────────────────────────
    st.markdown("#### Save & Export")
    save_name = st.text_input(
        "Scenario name",
        key=_RFP_NAME_KEY,
        placeholder="e.g. Sysco_FY26Q4",
    )

    target_path = ""
    path_exists = False
    if str(save_name).strip():
        try:
            target_path = _rfp_pnl_store.scenario_path_from_name(save_name)
            path_exists = _rfp_pnl_store.scenario_exists(target_path)
        except _rfp_pnl_store.RfpPnlStoreError:
            target_path = ""
            path_exists = False

    overwrite_ok = st.checkbox(
        "Overwrite existing scenario file (if name already exists)",
        value=False,
        key="rfp_pnl_overwrite",
    )

    save_col, dl_col = st.columns([1, 1])
    with save_col:
        if st.button("💾 Save Scenario", key="rfp_pnl_save", type="primary", use_container_width=True):
            if not str(save_name).strip():
                st.error("Please provide a scenario name before saving.")
            else:
                same_target = target_path and (target_path == st.session_state.get(_RFP_PATH_KEY))
                if path_exists and not overwrite_ok and not same_target:
                    st.error("Scenario already exists. Enable overwrite or choose another name.")
                else:
                    try:
                        write_etag = st.session_state.get(_RFP_ETAG_KEY) if same_target else None
                        written_path, new_etag = _rfp_pnl_store.save_scenario(
                            save_name,
                            current_items,
                            etag=write_etag,
                        )
                    except _rfp_pnl_store.RfpPnlStoreError as exc:
                        st.error(f"Could not save scenario: {exc}")
                    else:
                        st.session_state[_RFP_PATH_KEY] = written_path
                        st.session_state[_RFP_ETAG_KEY] = new_etag
                        st.success(f"Saved scenario to `Files/{written_path}`")
    with dl_col:
        file_stub = str(save_name).strip() or "scenario"
        if not file_stub.lower().endswith(".csv"):
            file_stub = f"{file_stub}.csv"
        st.download_button(
            label="⬇️ Download Scenario",
            data=_to_csv_bytes(current_items),
            file_name=file_stub,
            mime="text/csv",
            use_container_width=True,
            key="rfp_pnl_download",
        )

    # ── BOM Search ────────────────────────────────────────────────────────────
    st.markdown("---")
    _render_bom_search(sources)

    # ── Multi-Scenario Summary ────────────────────────────────────────────────
    st.markdown("---")
    _render_rfp_pnl_summary(sources, saved)


# ── 8a. BOM Search ────────────────────────────────────────────────────────────

# A standalone browser over ``BOM_History_Tracker_tagged.csv``. The store's
# ``bom_search`` returns the Level-1 rows for the chosen Month + Plant + Item
# Description and the chained Level-2 sub-recipe rows. It is independent of the
# scenario items — purely a lookup aid for analysts inspecting the BOM.

_BOM_SEARCH_MONTH_KEY = "rfp_bom_search_month"
_BOM_SEARCH_PLANT_KEY = "rfp_bom_search_plant"
_BOM_SEARCH_ITEM_KEY = "rfp_bom_search_item"


def _render_bom_search(sources: _rfp_pnl_store.RfpPnlSources) -> None:
    """Foldable panel that searches the BOM and offers Level-1 / Level-2 CSVs.

    Three cascading filters — Month (``Per Beg``), Plant, and Item
    Description (Level-1 ``Rule Item Desc``, cascading off Month + Plant) —
    drive two extracts the analyst can preview and download:

    * **Level 1** — rows matching the three filters at ``Level`` = 1.
    * **Level 2** — the chained sub-recipe rows reached from each Level-1
      anchor's ``Ing-Rsrc Desc`` (see :func:`_rfp_pnl_store.bom_search`).

    The expander stays collapsed by default to keep the page compact.
    """
    with st.expander("🔎 BOM Search", expanded=False):
        st.markdown(
            "Search `BOM_History_Tracker_tagged.csv` by **Month**, **Plant** "
            "and **Item Description**, then download the matching **Level 1** "
            "rows and their chained **Level 2** sub-recipe rows. The Item "
            "Description list shows Level-1 `Rule Item Desc` values for the "
            "selected Month + Plant."
        )

        c1, c2 = st.columns(2)
        with c1:
            month = st.selectbox(
                "Month",
                options=_rfp_options_with_current(
                    sources.month_options,
                    st.session_state.get(_BOM_SEARCH_MONTH_KEY),
                ),
                key=_BOM_SEARCH_MONTH_KEY,
                help="Matches BOM `Per Beg`.",
            )
        with c2:
            plant = st.selectbox(
                "Plant",
                options=_rfp_options_with_current(
                    sources.plant_options,
                    st.session_state.get(_BOM_SEARCH_PLANT_KEY),
                ),
                key=_BOM_SEARCH_PLANT_KEY,
                help="Matches BOM `Plant`.",
            )

        # Item Description cascades off Month + Plant. ``_rfp_options_with_current``
        # keeps any previously-picked value in the list so Streamlit never
        # raises when the parents change and the old value drops out.
        item_opts = _rfp_pnl_store.bom_search_item_options(
            sources, month=month, plant=plant,
        )
        item_desc = st.selectbox(
            "Item Description",
            options=_rfp_options_with_current(
                item_opts, st.session_state.get(_BOM_SEARCH_ITEM_KEY)
            ),
            key=_BOM_SEARCH_ITEM_KEY,
            help="Level-1 `Rule Item Desc` for the selected Month + Plant.",
        )

        if not (month and plant and item_desc):
            st.info("Select a Month, Plant and Item Description to search the BOM.")
            return

        try:
            level1, level2 = _rfp_pnl_store.bom_search(
                sources, month=month, plant=plant, item_desc=item_desc,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced to the user
            st.error(f"Could not search the BOM: {exc}")
            return

        slug = item_desc.lower().replace(" ", "_").replace("/", "_")
        for level_label, level_no, df in (
            ("Level 1", 1, level1),
            ("Level 2", 2, level2),
        ):
            st.markdown(f"**{level_label}** — {len(df):,} rows")
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.download_button(
                label=f"⬇️ Download {level_label} ({len(df):,} rows)",
                data=_to_csv_bytes(df),
                file_name=f"bom_search_{slug}_level{level_no}.csv",
                mime="text/csv",
                use_container_width=True,
                key=f"rfp_bom_search_dl_l{level_no}",
                help="Source: BOM_History_Tracker_tagged.csv",
            )


# ── 8b. Multi-Scenario Summary ────────────────────────────────────────────────

# Cell formatters for the long-format summary frame. Volume is rendered
# as a thousands-separated number (lbs); Revenue / GP as $ with two
# decimals (totals get large fast); FOB as $ with four decimals to keep
# parity with the per-item scenario table; PCM% / GP% as percentages.
_SUMMARY_METRIC_FORMATTERS: dict[str, callable] = {
    "Volume (pounds)": lambda v: "" if _rfp_pnl_store._to_float(v) is None  # noqa: SLF001
        else f"{_rfp_pnl_store._to_float(v):,.0f}",  # noqa: SLF001
    "FOB Price": _fmt_money,
    "FOB Revenue": lambda v: "" if _rfp_pnl_store._to_float(v) is None  # noqa: SLF001
        else f"${_rfp_pnl_store._to_float(v):,.2f}",  # noqa: SLF001
    "GP": lambda v: (
        ""
        if _rfp_pnl_store._to_float(v) is None  # noqa: SLF001
        else (
            f"$({abs(_rfp_pnl_store._to_float(v)):,.2f})"  # noqa: SLF001
            if _rfp_pnl_store._to_float(v) < 0  # noqa: SLF001
            else f"${_rfp_pnl_store._to_float(v):,.2f}"  # noqa: SLF001
        )
    ),
    "PCM%": _fmt_pct,
    "GP%": _fmt_pct,
}


def _format_summary_cell(metric: str, value: object) -> str:
    formatter = _SUMMARY_METRIC_FORMATTERS.get(metric, _fmt_passthrough)
    return formatter(value)


def _render_rfp_pnl_summary(
    sources: _rfp_pnl_store.RfpPnlSources,
    saved: list[_rfp_pnl_store.ScenarioFile],
) -> None:
    """Render the Multi-Scenario Summary section.

    Lets the analyst pick any subset of saved scenarios, optionally
    filter by Item / Category, and view a side-by-side comparison with
    a Total roll-up at the bottom (volume-weighted PCM% / GP%).
    """
    st.markdown("### 📊 Multi-Scenario Summary")
    st.caption(
        "Compare any combination of saved scenarios side-by-side. "
        "Each scenario is fully recomputed before aggregation, so the "
        "summary always reflects the live calc engine."
    )

    if not saved:
        st.info(
            "No saved scenarios found in "
            f"`Files/{_rfp_pnl_store.SCENARIO_FOLDER}`. Save a scenario "
            "above to start building the multi-scenario summary."
        )
        return

    # ── Scenario picker ───────────────────────────────────────────────────
    scenario_label_to_path: dict[str, str] = {}
    for s in saved:
        # Display the scenario stem (filename without .csv) — matches
        # the label used by the single-scenario picker above so the
        # analyst sees consistent names across the page.
        label = s.full_path.rsplit("/", 1)[-1].removesuffix(".csv")
        scenario_label_to_path[label] = s.full_path

    selected_labels: list[str] = st.multiselect(
        "Scenarios",
        options=list(scenario_label_to_path.keys()),
        default=[],
        key="rfp_pnl_summary_scenarios",
        help="Pick the scenarios you want to compare. Order is preserved.",
    )

    if not selected_labels:
        st.caption("Select at least one scenario to render the summary.")
        return

    # ── Load & recompute each selected scenario ───────────────────────────
    # We materialize a small dict[label → items_df] up front so that the
    # filter dropdowns can offer the union of items / categories across
    # the selection without re-loading.
    loaded: dict[str, pd.DataFrame] = {}
    for label in selected_labels:
        path = scenario_label_to_path[label]
        try:
            items_df, _etag = _rfp_pnl_store.read_scenario(path)
        except _rfp_pnl_store.RfpPnlStoreError as exc:
            st.error(f"Could not load scenario `{label}`: {exc}")
            return
        loaded[label] = items_df

    # ── Filter options derived from the loaded scenarios ──────────────────
    item_options: list[str] = sorted({
        str(v).strip()
        for df in loaded.values()
        for v in df.get("Target SKU Name", pd.Series(dtype=str)).fillna("").tolist()
        if str(v).strip()
    })
    category_options: list[str] = sorted({
        str(v).strip()
        for df in loaded.values()
        for v in df.get("Category", pd.Series(dtype=str)).fillna("").tolist()
        if str(v).strip()
    })

    f_col1, f_col2 = st.columns(2)
    with f_col1:
        items_filter = st.multiselect(
            "Items (empty = all)",
            options=item_options,
            default=[],
            key="rfp_pnl_summary_items_filter",
        )
    with f_col2:
        categories_filter = st.multiselect(
            "Categories (empty = all)",
            options=category_options,
            default=[],
            key="rfp_pnl_summary_categories_filter",
        )

    # ── Build & display ───────────────────────────────────────────────────
    summary_df = _rfp_pnl_store.summarize_scenarios(
        loaded,
        sources,
        items_filter=items_filter or None,
        categories_filter=categories_filter or None,
    )

    if summary_df.empty:
        st.info("No items match the current filters.")
        return

    # Apply per-metric formatting to scenario columns; leave Item /
    # Category / Metric as plain strings.
    formatted = summary_df.copy()
    for label in selected_labels:
        if label not in formatted.columns:
            continue
        formatted[label] = [
            _format_summary_cell(metric, val)
            for metric, val in zip(formatted["Metric"], formatted[label])
        ]

    # Highlight the Total band so it visually separates from per-item rows.
    def _row_style(row: pd.Series) -> list[str]:
        if row.get("Item") == _rfp_pnl_store.SUMMARY_TOTAL_LABEL:
            return ["background-color: rgba(255, 193, 7, 0.18); font-weight: 600"] * len(row)
        return [""] * len(row)

    height = max(400, 36 * (len(formatted) + 1) + 4)
    st.dataframe(
        formatted.style.apply(_row_style, axis=1),
        use_container_width=True,
        hide_index=True,
        height=height,
    )

    # CSV export of the *unformatted* numeric summary so analysts can pull
    # it into Excel / Tableau without re-parsing the rendered strings.
    st.download_button(
        label="⬇️ Download Summary (CSV)",
        data=_to_csv_bytes(summary_df),
        file_name="rfp_pnl_multi_scenario_summary.csv",
        mime="text/csv",
        key="rfp_pnl_summary_download",
    )


# ── 9. Bid Asset wrapper section ───────────────────────────────────────────────

def _render_bid_asset_section() -> None:
    """Render the original Bid Asset section exactly as before."""
    # ── List CSV files in the Lakehouse folder ────────────────────────────────
    # Show a spinner during the directory listing (first render or after cache
    # expiry); subsequent reruns within the 5-minute TTL use the cached result.
    with st.spinner("Checking Fabric Lakehouse for bid asset files…"):
        try:
            available_files = _bid_store.list_bid_files()
        except BidAssetStoreError as exc:
            st.error(
                f"Could not list files in `Files/{_LAKEHOUSE_FOLDER}`: {exc}\n\n"
                "If Microsoft Fabric is not signed in, visit **Home & Fabric Sign-in** "
                "in the sidebar to sign in, then return here."
            )
            return

    if not available_files:
        st.info(
            f"No CSV files found in `Files/{_LAKEHOUSE_FOLDER}` on the Fabric Lakehouse. "
            "Please upload the Bid Asset CSV to that folder and refresh this page."
        )
        return

    # Sort newest-first so the default selection is always the latest file.
    file_labels = [
        f"{f.name}  ({f.last_modified or 'unknown date'})"
        for f in available_files
    ]

    # ── File picker (only shown when multiple files are present) ──────────────
    # Single file → auto-select silently.  Multiple files → show a selectbox
    # so users can access historical snapshots without navigating OneLake.
    st.markdown("### 📂 Bid Asset Data — Fabric Lakehouse")
    if len(available_files) == 1:
        selected_file = available_files[0]
        st.caption(
            f"Auto-loaded: `{selected_file.name}` "
            f"(last modified: {selected_file.last_modified or 'unknown'})"
        )
    else:
        chosen_label = st.selectbox(
            "Select file to load",
            options=file_labels,
            index=0,
            key="bid_asset_file_picker",
            help=(
                "Files are sorted newest-first. The most recently modified file "
                "is selected by default."
            ),
        )
        chosen_idx = file_labels.index(chosen_label)
        selected_file = available_files[chosen_idx]

    # ── Load & cache the selected file ───────────────────────────────────────
    # On first render (or after Reload) we fetch the file via
    # ``bid_asset_store.read_bid_file`` and stash the result in session state
    # so subsequent reruns (slider drags, filter changes, edits in the data
    # editor) don't re-hit OneLake. ``Refresh & Publish to Fabric`` and the
    # Reload button below are the only paths that re-fetch from the lakehouse.
    col_info, col_reload = st.columns([4, 1])
    with col_reload:
        if st.button(
            "🔄 Reload from Lakehouse",
            key="bid_asset_reload",
            help="Discard local edits and re-fetch the selected file from OneLake.",
        ):
            st.session_state.pop(_session_df_key(selected_file.full_path), None)
            st.session_state.pop(_session_etag_key(selected_file.full_path), None)
            st.session_state.pop(_session_dirty_key(selected_file.full_path), None)
            st.rerun()

    df_key = _session_df_key(selected_file.full_path)
    if df_key not in st.session_state:
        try:
            source_df, source_etag = _bid_store.read_bid_file(selected_file.full_path)
        except BidAssetStoreError as exc:
            st.error(
                f"Could not read bid data from Fabric Lakehouse: {exc}\n\n"
                "If Microsoft Fabric is not signed in, visit **Home & Fabric Sign-in** "
                "in the sidebar to sign in, then return here."
            )
            return
        _initialise_edit_state(selected_file.full_path, source_df, source_etag)

    raw_df = st.session_state[df_key].drop(columns=[_ROW_ID_COL], errors="ignore")

    with col_info:
        st.success(
            f"✅ Loaded `{selected_file.name}` — "
            f"**{len(raw_df):,} rows**, **{len(raw_df.columns)} columns**"
        )
    st.markdown("---")

    # Sorted month list computed once and shared by both sliders.
    all_months_sorted: list[str] = (
        sorted(raw_df["Month"].dropna().astype(str).unique().tolist(), key=_month_sort_key)
        if "Month" in raw_df.columns else []
    )

    _render_bid_overview(raw_df, all_months_sorted)
    st.markdown("---")

    result = _render_search_filters(raw_df, all_months_sorted)
    if result is not None:
        st.markdown("---")
        _render_rfp_summary(result)
        st.markdown("---")
    else:
        st.info(
            "RFP Program-level Table is hidden until all required Search & Filter "
            "selections are set. Item-level editing remains available below."
        )
        st.markdown("---")

    _render_program_tracker(raw_df)
    st.markdown("---")
    _render_editable_item_details(selected_file.full_path)


# ── 10. Entry point ───────────────────────────────────────────────────────────

def _render_finance_pnl_dashboard() -> None:
    """Embed the Finance P&L Power BI report at the bottom of the page.

    The Power BI service serves its viewer URL with an X-Frame-Options
    header that blocks embedding from arbitrary origins. We rewrite the
    URL into the ``reportEmbed`` form via :func:`to_powerbi_embed_url`,
    which appends ``autoAuth=true`` so the embedded frame silently
    completes the Entra-ID handshake for users with an active tenant
    session.
    """
    with st.expander("💰 Finance P&L", expanded=False):
        render_embedded_resource(
            url=_FINANCE_PNL_REPORT_URL,
            title="Finance P&L (Power BI)",
            embed_url=to_powerbi_embed_url(_FINANCE_PNL_REPORT_URL),
            height=900,
            fallback_note=(
                "This is the live Finance P&L Power BI report. The frame "
                "below uses Power BI's embed mode with Entra-ID auto-auth, "
                "but tenant SSO policy may still require an interactive "
                "sign-in. If the frame is blank, use the button below to "
                "open the report directly in Power BI."
            ),
        )


def render() -> None:
    """Render the Bid Assistant page.

    The page now exposes three foldable sections:
    1) RFP P&L Analysis (scenario builder)
    2) Bid Asset (legacy Bid Asset Intelligence workflows)
    3) Finance P&L (embedded Power BI report — migrated from the
       retired RFP Financial Analysis view)
    """
    apply_custom_css()

    st.markdown(
        '<h1 class="main-header">Bid Assistant</h1>',
        unsafe_allow_html=True,
    )

    st.markdown("""
### Welcome

Use this page to analyze historical trends since December 2025. These insights drive post-mortem analysis
and sharpen future bid strategies. Key resources include:

- **RFP P&L Analysis:** Build scenario-based P&L tables using BOM, Budget and PDH references.
- **Visualizations:** Charts for bid comparisons.
- **RFP Program-level Table:** High-level tracking of program size, status and key financials.
- **Granular Data:** Detailed breakdowns of item-level PCM, GP and price builds.

Data is loaded automatically from the **Fabric Lakehouse** (`Files/Program_Bid_Management`).
Sign in to Microsoft Fabric on the **Home & Fabric Sign-in** page if the data does not appear.
""")
    st.markdown("---")

    # ── Fabric auth gate ──────────────────────────────────────────────────────
    # If the user is not signed in, show a concise redirect warning and stop.
    # The actual sign-in UI lives exclusively on the Home & Fabric Sign-in page.
    if not fabric_signin_widget.is_fabric_signed_in():
        st.warning(
            "🔒 **Microsoft Fabric is not connected.**\n\n"
            "Please visit **Home & Fabric Sign-in** in the sidebar to sign in. "
            "Once signed in, return here — bid data will load automatically."
        )
        return

    with st.expander("RFP P&L Analysis", expanded=False):
        _render_rfp_pnl_analysis()

    st.markdown("---")

    with st.expander("Bid Asset", expanded=False):
        _render_bid_asset_section()

    st.markdown("---")

    _render_finance_pnl_dashboard()
