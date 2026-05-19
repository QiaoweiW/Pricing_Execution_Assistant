"""Bid Asset Intelligence page view.

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
from data_sources.bid_asset_store import BidAssetStoreError
from utils import fabric_signin_widget
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


# ── 8. Entry point ────────────────────────────────────────────────────────────

def render() -> None:
    """Render the Bid Asset Intelligence page.

    Data is pulled automatically from the Fabric Lakehouse (``Files/Program_Bid_Management``).
    No file upload is required; users must be signed in to Microsoft Fabric via the
    Home & Fabric Sign-in page.

    Orchestrates the four analysis sections in order.  Each section is self-contained:
    it reads Streamlit widget state, computes its own data slice, and renders its own UI.
    render() itself carries no business logic beyond routing the loaded DataFrame.
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
    etag_key = _session_etag_key(selected_file.full_path)
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
