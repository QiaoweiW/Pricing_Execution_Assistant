"""
Bid Asset Intelligence page view.

Features:
- Upload Bid Asset CSV file
- Multiselect filters: Format, Company, Bid Description, Month, Round (all mandatory)
- Metrics row (single company + bid description + round only)
- RFP Summary table (aggregated, grouped by key dimensions)
- Detailed item-level table extract
- Download buttons for both tables
"""
import hashlib
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from plotly.subplots import make_subplots
import plotly.graph_objects as go
from utils.ui_helpers import apply_custom_css


# ── Helpers ───────────────────────────────────────────────────────────────────

def _month_sort_key(m_str: str) -> datetime:
    """Parse 'Mon YYYY' strings into datetime for chronological sorting."""
    try:
        return datetime.strptime(str(m_str).strip(), "%b %Y")
    except Exception:
        return datetime.min


def _sel_hash(*selections) -> str:
    """Short hash of upstream selections — used to key downstream widgets so
    they reset (default = all) whenever a parent filter changes."""
    combined = "|".join(
        str(x) for sel in selections for x in sorted(str(s) for s in sel)
    )
    return hashlib.md5(combined.encode()).hexdigest()[:8]


def _excel_serial_to_date(serial) -> str:
    """Convert an Excel serial date integer to a human-readable 'Mon YYYY' string."""
    try:
        dt = datetime(1899, 12, 30) + timedelta(days=int(float(serial)))
        return dt.strftime("%b %Y")
    except Exception:
        return str(serial)


def _parse_currency_col(series: pd.Series) -> pd.Series:
    """
    Convert currency strings like ' $424,236 ' or ' $(3,846)' to floats.
    Leaves already-numeric series unchanged.
    """
    if pd.api.types.is_numeric_dtype(series):
        return series
    cleaned = (
        series.astype(str)
        .str.strip()
        .str.replace(r"\$", "", regex=True)
        .str.replace(",", "", regex=False)
        .str.replace(" ", "", regex=False)
    )
    is_neg = cleaned.str.startswith("(")
    cleaned = cleaned.str.replace(r"[()]", "", regex=True)
    result = pd.to_numeric(cleaned, errors="coerce")
    result = result.where(~is_neg, -result)
    return result


def _fmt_currency(val) -> str:
    if pd.isna(val):
        return ""
    if val < 0:
        return f"$({abs(val):,.0f})"
    return f"${val:,.0f}"


def _fmt_volume(val) -> str:
    if pd.isna(val):
        return ""
    return f"{val:,.0f}"


def _fmt_pct(val) -> str:
    if pd.isna(val) or not isinstance(val, (int, float)):
        return "—"
    return f"{val:.1f}%"


def _to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


# ── Main render ───────────────────────────────────────────────────────────────

def render():
    """Render the Bid Asset Intelligence page."""
    apply_custom_css()

    st.markdown(
        '<h1 class="main-header">Bid Asset Intelligence</h1>',
        unsafe_allow_html=True,
    )

    # ── Welcome section ───────────────────────────────────────────────────────
    st.markdown("""
### Welcome

Use this page to analyze historical trends since December 2025. These insights drive post-mortem analysis
and sharpen future bid strategies. Key resources include:

- **Visualizations:** Charts for bid comparisons.
- **RFP Summary:** High-level tracking of program size, status and key financials.
- **Granular Data:** Detailed breakdowns of item-level PCM, GP and price builds.
""")

    st.markdown("---")

    # ── Upload section ────────────────────────────────────────────────────────
    st.markdown("### 📤 Upload Bid Asset CSV File")
    st.markdown(
        "Upload Bid Asset CSV export saved in the "
        "[SharePoint Folder](https://darigold1com.sharepoint.com/sites/BrandedPricing/Shared%20Documents/Forms/AllItems.aspx"
        "?id=%2Fsites%2FBrandedPricing%2FShared%20Documents%2FGeneral%2F02%20Resources%2FRFP%20Management"
        "&viewid=9103ebc3%2Df944%2D4451%2Dbe05%2Dd0cb7479e27e)"
    )

    uploaded_file = st.file_uploader(
        "Select Bid Asset CSV",
        type=["csv"],
        key="bid_asset_uploader",
    )

    if uploaded_file is None:
        st.info("👆 Upload a CSV file above to unlock the search and analysis tables.")
        return

    # ── Load & normalise ──────────────────────────────────────────────────────
    try:
        raw_df = pd.read_csv(uploaded_file)
    except Exception as exc:
        st.error(f"Could not read the uploaded file: {exc}")
        return

    raw_df.columns = raw_df.columns.str.strip()

    if "Rounds" in raw_df.columns:
        raw_df = raw_df.rename(columns={"Rounds": "Round"})

    if "Month" in raw_df.columns:
        first_val = raw_df["Month"].dropna().iloc[0] if not raw_df["Month"].dropna().empty else None
        if first_val is not None and pd.api.types.is_numeric_dtype(raw_df["Month"]):
            raw_df["Month"] = raw_df["Month"].apply(_excel_serial_to_date)

    NUMERIC_COLS = ["Volume (lbs)", "FOB Revenue $/Yr", "PCM $/Yr", "GP $/Yr"]
    for col in NUMERIC_COLS:
        if col in raw_df.columns:
            raw_df[col] = _parse_currency_col(raw_df[col])

    st.success(f"✅ File loaded — **{len(raw_df):,} rows**, **{len(raw_df.columns)} columns**")

    st.markdown("---")

    # ── Bid Overview (independent of search filters) ──────────────────────────
    st.markdown("### 📈 Bid Overview")
    st.caption(
        "Bars show total Volume (lbs) from the **latest round** per bid. "
        "Color: green = Accepted, gray = Rejected, blue = Other. "
        "Dotted line shows Total PCM $/Yr (right axis). "
        "This chart is independent of the search filters below."
    )

    # ── Chart controls row ────────────────────────────────────────────────────
    ctrl_left, ctrl_right = st.columns([2, 3])

    with ctrl_left:
        chart_fmt_opts = (
            sorted(raw_df["Format"].dropna().astype(str).unique().tolist())
            if "Format" in raw_df.columns else []
        )
        selected_chart_fmts = st.multiselect(
            "Format",
            options=chart_fmt_opts,
            default=chart_fmt_opts,
            key="chart_format_filter",
        )

    with ctrl_right:
        if "Month" in raw_df.columns:
            all_months_sorted = sorted(
                raw_df["Month"].dropna().astype(str).unique().tolist(),
                key=_month_sort_key,
            )
            if len(all_months_sorted) >= 2:
                month_range = st.select_slider(
                    "Month Range",
                    options=all_months_sorted,
                    value=(all_months_sorted[0], all_months_sorted[-1]),
                    key="chart_month_range",
                )
                chart_start_month, chart_end_month = month_range
            elif len(all_months_sorted) == 1:
                chart_start_month = chart_end_month = all_months_sorted[0]
            else:
                chart_start_month = chart_end_month = None
        else:
            chart_start_month = chart_end_month = None

    # Build base chart dataset
    chart_base = raw_df.copy()

    if selected_chart_fmts and "Format" in chart_base.columns:
        chart_base = chart_base[chart_base["Format"].astype(str).isin(selected_chart_fmts)]

    if chart_start_month and chart_end_month and "Month" in chart_base.columns:
        start_dt = _month_sort_key(chart_start_month)
        end_dt   = _month_sort_key(chart_end_month)
        chart_base = chart_base[
            chart_base["Month"].apply(lambda m: start_dt <= _month_sort_key(m) <= end_dt)
        ]

    if not chart_base.empty and "Round" in chart_base.columns:

        def _round_num(r):
            digits = "".join(c for c in str(r) if c.isdigit())
            return int(digits) if digits else 0

        chart_base = chart_base.copy()
        chart_base["_round_num"] = chart_base["Round"].apply(_round_num)

        group_keys = [c for c in ["Company", "Bid Description"] if c in chart_base.columns]

        if group_keys:
            latest = (
                chart_base.groupby(group_keys)["_round_num"]
                .max()
                .reset_index()
                .rename(columns={"_round_num": "_max_round"})
            )
            chart_base = chart_base.merge(latest, on=group_keys)
            chart_base = chart_base[chart_base["_round_num"] == chart_base["_max_round"]]

        agg_keys = group_keys + ["Round"]
        sum_cols  = [c for c in ["Volume (lbs)", "PCM $/Yr"] if c in chart_base.columns]
        chart_agg = chart_base.groupby(agg_keys, as_index=False)[sum_cols].sum()

        if "Status" in chart_base.columns:
            status_mode = (
                chart_base.groupby(agg_keys)["Status"]
                .agg(lambda x: x.mode().iloc[0] if not x.mode().empty else "Unknown")
                .reset_index()
            )
            chart_agg = chart_agg.merge(status_mode, on=agg_keys, how="left")
        else:
            chart_agg["Status"] = "Unknown"

        if "Volume (lbs)" in chart_agg.columns:
            chart_agg = chart_agg.sort_values("Volume (lbs)", ascending=False).reset_index(drop=True)

        def _make_label(row):
            co  = str(row.get("Company", ""))
            bid = str(row.get("Bid Description", ""))
            rnd = str(row.get("Round", ""))
            return f"{co}<br>{bid}<br>({rnd})"

        chart_agg["_label"] = chart_agg.apply(_make_label, axis=1)

        def _bar_color(status):
            s = str(status).lower()
            if "reject" in s:
                return "#9E9E9E"
            elif "accept" in s or "award" in s:
                return "#4CAF50"
            return "#2196F3"

        chart_agg["_color"] = chart_agg["Status"].apply(_bar_color)

        fig = make_subplots(specs=[[{"secondary_y": True}]])

        color_groups = [
            ("#4CAF50", "Accepted"),
            ("#9E9E9E", "Rejected"),
            ("#2196F3", "Other"),
        ]
        for color_hex, legend_name in color_groups:
            mask = chart_agg["_color"] == color_hex
            if mask.any():
                subset = chart_agg[mask]
                fig.add_trace(
                    go.Bar(
                        x=subset["_label"],
                        y=subset["Volume (lbs)"] if "Volume (lbs)" in subset.columns else [],
                        name=legend_name,
                        marker_color=color_hex,
                        opacity=0.85,
                    ),
                    secondary_y=False,
                )

        if "PCM $/Yr" in chart_agg.columns:
            fig.add_trace(
                go.Scatter(
                    x=chart_agg["_label"],
                    y=chart_agg["PCM $/Yr"],
                    name="Total PCM $/Yr",
                    mode="markers",
                    marker=dict(size=10, color="#d32f2f", symbol="circle"),
                    hovertemplate="%{x}<br>PCM: $%{y:,.1f}<extra></extra>",
                ),
                secondary_y=True,
            )

        fig.update_layout(
            barmode="overlay",
            font=dict(family="Segoe UI, Tahoma, Geneva, Verdana, sans-serif", size=14),
            xaxis=dict(
                title=dict(text="Company / Bid Description (Round)", font=dict(size=15)),
                tickangle=-20,
                tickfont=dict(size=13),
            ),
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                font=dict(size=13),
            ),
            height=560,
            margin=dict(l=80, r=80, t=60, b=180),
            plot_bgcolor="white",
            paper_bgcolor="white",
        )
        fig.update_yaxes(
            title_text="Volume (lbs)",
            title_font=dict(size=15),
            tickfont=dict(size=13),
            secondary_y=False,
            gridcolor="#f0f0f0",
        )
        fig.update_yaxes(
            title_text="Total PCM $/Yr",
            title_font=dict(size=15),
            tickfont=dict(size=13),
            secondary_y=True,
            showgrid=False,
            tickformat="$.2s",
        )

        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No data available for the selected format.")

    st.markdown("---")

    # ── Search & Filter ────────────────────────────────────────────────────────
    st.markdown("### 🔍 Search & Filter")
    st.caption(
        "**Month** is an independent time-range slicer. "
        "**Company** anchors the remaining cascading filters."
    )

    # ── 1. Month range slicer — independent of all other filters ──────────────
    if "Month" in raw_df.columns:
        all_filter_months = sorted(
            raw_df["Month"].dropna().astype(str).unique().tolist(),
            key=_month_sort_key,
        )
    else:
        all_filter_months = []

    if len(all_filter_months) >= 2:
        filter_month_range = st.select_slider(
            "📅 Month Range",
            options=all_filter_months,
            value=(all_filter_months[0], all_filter_months[-1]),
            key="filter_month_range",
        )
        filter_start_dt = _month_sort_key(filter_month_range[0])
        filter_end_dt   = _month_sort_key(filter_month_range[1])
    elif len(all_filter_months) == 1:
        filter_start_dt = filter_end_dt = _month_sort_key(all_filter_months[0])
    else:
        filter_start_dt = filter_end_dt = None

    # Apply month range to base dataset for the cascading filters
    if filter_start_dt and filter_end_dt and "Month" in raw_df.columns:
        df_month = raw_df[
            raw_df["Month"].apply(lambda m: filter_start_dt <= _month_sort_key(m) <= filter_end_dt)
        ]
    else:
        df_month = raw_df.copy()

    # ── 2–5. Cascading filters: Company → Bid Description → Round → Format ────
    f1, f2, f3, f4 = st.columns(4)

    # Company — draws from full dataset (independent of month for option list,
    # but final data intersects with month-filtered rows)
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

    # Bid Description — cascades from Company (within month-filtered slice)
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

    # Round — cascades from Company + Bid Description
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

    # Format — cascades from Company + Bid Description + Round
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

    # selections dict (used by metrics conditions)
    selections = {
        "Company":         sel_company,
        "Bid Description": sel_bid,
        "Round":           sel_round,
        "Format":          sel_format,
    }

    # Guard: warn if any cascading filter is empty
    empty_filters = [k for k, v in selections.items() if not v]
    if empty_filters:
        st.warning(
            f"⚠️ Please select at least one value for: **{', '.join(empty_filters)}**"
        )
        return

    st.markdown(f"**{len(filtered_df):,} records** match the current filter criteria.")

    st.markdown("---")

    # ── RFP Summary ───────────────────────────────────────────────────────────
    st.markdown("### 📊 RFP Summary")
    st.markdown(
        "Item-level PCM, GP, and detailed price builds can be extracted from the "
        "**\"Detailed Item-level Data\"** section below. "
        "Note the % here is a comparison against FOB Revenue."
    )

    GROUP_COLS = [
        "Format", "Company", "Bid Description", "Brand",
        "Round", "Month", "Status", "Bid Rationale", "Feedback",
    ]
    SUM_COLS = ["Volume (lbs)", "FOB Revenue $/Yr", "PCM $/Yr", "GP $/Yr"]

    available_group = [c for c in GROUP_COLS if c in filtered_df.columns]
    available_sum   = [c for c in SUM_COLS   if c in filtered_df.columns]

    # ── Conditional metrics ───────────────────────────────────────────────────
    one_company  = len(selections.get("Company",         [])) == 1
    one_bid_desc = len(selections.get("Bid Description", [])) == 1
    one_round    = len(selections.get("Round",           [])) == 1

    if one_company and one_bid_desc and one_round and available_sum:
        total_lbs = filtered_df["Volume (lbs)"].sum()       if "Volume (lbs)"     in filtered_df.columns else None
        total_fob = filtered_df["FOB Revenue $/Yr"].sum()   if "FOB Revenue $/Yr" in filtered_df.columns else None
        total_pcm = filtered_df["PCM $/Yr"].sum()           if "PCM $/Yr"         in filtered_df.columns else None
        total_gp  = filtered_df["GP $/Yr"].sum()            if "GP $/Yr"          in filtered_df.columns else None

        pcm_pct = (total_pcm / total_fob * 100) if (total_fob and total_fob != 0) else None
        gp_pct  = (total_gp  / total_fob * 100) if (total_fob and total_fob != 0) else None

        # Status: show unique value(s) for the current selection
        if "Status" in filtered_df.columns:
            unique_statuses = filtered_df["Status"].dropna().astype(str).unique().tolist()
            status_display = " / ".join(sorted(unique_statuses)) if unique_statuses else "—"
        else:
            status_display = "—"

        m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
        with m1:
            st.metric("Total Pounds", _fmt_volume(total_lbs))
        with m2:
            st.metric("Total FOB Revenue $/Yr", _fmt_currency(total_fob))
        with m3:
            st.metric("Total PCM $/Yr", _fmt_currency(total_pcm))
        with m4:
            st.metric("Total GP $/Yr", _fmt_currency(total_gp))
        with m5:
            st.metric("PCM %", _fmt_pct(pcm_pct))
        with m6:
            st.metric("GP %", _fmt_pct(gp_pct))
        with m7:
            st.metric("Status", status_display)

        st.markdown("")  # spacer

    # ── Summary table ─────────────────────────────────────────────────────────
    if available_group and available_sum:
        summary_df = (
            filtered_df
            .groupby(available_group, as_index=False, dropna=False)[available_sum]
            .sum()
        )

        summary_display = summary_df.copy()
        for col in available_sum:
            if col == "Volume (lbs)":
                summary_display[col] = summary_df[col].apply(_fmt_volume)
            else:
                summary_display[col] = summary_df[col].apply(_fmt_currency)

        # Add "Price Implement Time" from source data if available, else blank
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
    else:
        st.warning("Not enough columns available to build the RFP Summary table.")

    st.markdown("---")

    # ── Detailed item-level table ──────────────────────────────────────────────
    st.markdown("### 📋 Detailed Item-Level Data")
    st.caption("Full extract of the CSV filtered by the search criteria above.")

    # Identify $/EA columns and round to 4 decimal places
    EA_COLS = [c for c in filtered_df.columns if "$/EA" in c or "$/ea" in c.lower()]

    detail_download = filtered_df.copy()
    for col in EA_COLS:
        if pd.api.types.is_numeric_dtype(detail_download[col]):
            detail_download[col] = detail_download[col].round(4)

    detail_display = detail_download.copy()
    for col in available_sum:
        if col in detail_display.columns:
            if col == "Volume (lbs)":
                detail_display[col] = detail_download[col].apply(_fmt_volume)
            else:
                detail_display[col] = detail_download[col].apply(_fmt_currency)

    st.dataframe(detail_display, use_container_width=True, hide_index=True)

    st.download_button(
        label="⬇️ Download Detailed Table (CSV)",
        data=_to_csv_bytes(detail_download),
        file_name=f"bid_asset_detail_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        key="download_detail",
    )
