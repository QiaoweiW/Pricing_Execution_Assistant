"""
Monthly Resin & Freight Mover Tracker — self-contained section for the
Market Barometer page.

Sections
--------
1. Constants           (SS_PREFIX, source-file keyword map,
                        canonical column names)
2. Parsing helpers     (_parse_month, _parse_money, _parse_percent,
                        _to_csv_bytes)
3. File ingestion      (_detect_role, _load_uploaded_files,
                        _file_sig, REQUIRED_ROLES)
4. Chart builder       (_build_packaging_index_chart)
5. Editable table      (_visible_movers_window, _render_editable_movers)
6. Calculations        (HTST helpers: _filter_htst_included,
                        _resolve_htst_column, _build_htst_gallons_lookup,
                        _build_freight_impact_detail;
                        Pipeline: _build_resincalculate,
                        _build_updated_resin_cost_tracker,
                        _build_resin_mover_fg,
                        _enrich_resin_mover_fg_with_htst,
                        _build_example_prices_impact_table,
                        _compute_all_outputs)
7. UI fragments        (_render_monthly_sop_and_upload_intro, _render_upload_panel,
                        _render_chart, _render_freight_outlook,
                        _render_table_and_refresh, _render_results,
                        _clear_upload_state, _clear_freight_state)
8. Public API          (render_monthly_resin_freight_mover_tracker)

Design notes
------------
Isolation
  Everything lives under the ``_SS_PREFIX`` namespace in ``st.session_state``
  so this fragment never collides with other page state. The public entry
  point is a ``@st.fragment`` so uploads, edits and the Submit button rerun
  only this block, not the full Market Barometer page.

Robustness
  * Files are matched by filename keyword — the exact dated filenames do
    NOT need to be known in advance, so the user can drop any vintage of
    these files (e.g. "..._20260422.csv" today, "..._20260521.csv" next
    month) and the pipeline keeps working.
  * **Streamlit Cloud / fragment reruns:** ``st.rerun(scope="fragment")`` can
    raise ``StreamlitAPIException`` when no fragment id is active (e.g. this
    widget tree lives inside a parent ``st.expander``). This module avoids
    fragment-scoped reruns; it uses same-run fall-through or full
    ``st.rerun()`` where a hard reset is required.
  * "Current month" is derived from ``date.today()`` at render time, so the
    same code automatically advances month-over-month without edits.
  * HTST annualized gallons CSV is matched by filename keyword and left-joined
    onto ``resin_mover_fg`` for Impact volume columns and freight totals.
    The HTST merge is filtered to rows where ``Include/N == "Y"`` so excluded
    customers (Costco, Walmart, USF) never contribute to either impact metric.
  * Dollar and percent text cells (e.g. "$0.915 ", "1%") are parsed with
    dedicated helpers that strip whitespace / symbols before casting.

Calculation contract (matches user spec)
  1. ``resincalculate`` = copy of Resin_Calculator CSV + new column
       Resin Cost ($/Gal) = resin_cost_next_month × Usage (Lbs/Ea)
                          × (1 + scrape_fraction) / Gal/Ea
     where ``resin_cost_next_month`` is the Resin Cost ($/lbs) value for
     the month AFTER the current month (row shown last in the editor) and
     ``scrape_fraction`` is the latest scrape / yield-loss rate from
     ``Scrape_Tracker``.
  2. Updated Resin_Cost_Tracker: append a duplicate of the latest-month
     rows, stamp the duplicate with the current month, then left-join the
     ``resincalculate`` Resin Cost ($/Gal) onto that current-month section
     keyed on Resin == Pricing Category.
  3. ``resin_mover_fg`` = per-product dataframe comparing the new (current
     month) vs old (previous month) Resin Cost ($/Gal) plus their delta,
     left-joined to HTST annualized gallons on product description ↔
     PRODUCTDESC, with ``Est. Monthly Gallons`` and
     ``Est. Monthly Resin Impact`` columns. Impact metrics aggregate these.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


# ── 1. Constants ──────────────────────────────────────────────────────────────

# Namespace for every key this fragment writes into st.session_state so that
# we never collide with other views on the Market Barometer page.
_SS_PREFIX = "mrfmt"

# SharePoint folder that holds the Monthly Resin & Freight source CSVs (linked
# from the upload panel — no hard-coded local paths in the UI).
_MONTHLY_MOVERS_SHAREPOINT_URL: str = (
    "https://darigold1com.sharepoint.com/:f:/r/sites/BrandedPricing/"
    "Shared%20Documents/General/02%20Resources/"
    "Streamlit%20Folders%20(DO%20NOT%20DELETE)/"
    "Monthly%20Resin%20%26%20Freight?csf=1&web=1&e=mXUs7H"
)

# Role → filename keyword mapping (case-insensitive, tested in order so more
# specific keys come first — e.g. "pkg_index" must be checked before a shorter
# "pkg" keyword would match the richer "packaging_index" file).
_ROLE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("movers_tracker",     ("movers_tracker",)),
    ("resin_calculator",   ("resin_calculator",)),
    ("resin_cost_tracker", ("resin_cost_tracker",)),
    # ``scrap_tracker`` keyword retained so legacy filenames still classify
    # until all users rename to ``Scrape_Tracker``.
    ("scrape_tracker",     ("scrape_tracker", "scrap_tracker")),
    ("packaging_index",    ("packaging_index",)),
    ("pkg_index",          ("pkg_index",)),
    ("ibp_volume",         ("ibp_volume", "ibp_volume_c1")),
    ("htst_annualized_gallons", ("htst_annualized_gallons", "htst_annualized")),
    ("example_prices",     ("example_prices",)),
]

# Roles that MUST be provided before the Refresh step can run. The chart only
# needs `packaging_index` — the remaining files are required for the
# calculations that run after Refresh (including Impact metrics).
REQUIRED_ROLES: tuple[str, ...] = (
    "movers_tracker",
    "resin_calculator",
    "resin_cost_tracker",
    "scrape_tracker",
    "htst_annualized_gallons",
    "example_prices",
)

# Canonical column names (one definition → avoids typos across the file).
_COL_PRODUCT_ID   = "Product ID"
_COL_PRODUCT_DESC = "Product Description"
_COL_RESIN        = "Resin"
_COL_RESIN_GAL    = "Resin Cost ($/Gal)"
_COL_MONTH        = "Month"
_COL_PRICING_CAT  = "Pricing Category"
_COL_USAGE_LBS    = "Usage (Lbs/Ea)"
_COL_GAL_EA       = "Gal/Ea"
_COL_RESIN_LBS    = "Resin Cost ($/lbs)"
_COL_FREIGHT_LBS  = "Freight Mover ($/lbs)"


# ── 2. Parsing helpers ────────────────────────────────────────────────────────

def _parse_month(value) -> Optional[pd.Timestamp]:
    """Parse a date-ish value into the first day of its month as a Timestamp.

    Accepts pandas Timestamps, python datetimes, and common string formats
    (``M/D/YYYY``, ``YYYY-MM-DD``, etc.). Returns ``None`` on failure so the
    caller can filter invalid rows without raising.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return ts.normalize().replace(day=1)


def _parse_money(value) -> Optional[float]:
    """Parse a currency-ish cell like '$0.915 ' or ' - ' into a float.

    Dashes, blanks, NaN and non-numeric strings all collapse to ``None`` so
    calculations can skip them without raising.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if pd.isna(value) else float(value)
    s = str(value).strip().replace("$", "").replace(",", "").strip()
    if not s or s in {"-", "–", "—"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_percent(value) -> Optional[float]:
    """Parse a percent-ish cell like '1%' or '0.01' into a fraction (0.01).

    Strings ending in ``%`` are divided by 100; bare numbers are taken as-is.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if pd.isna(value) else float(value)
    s = str(value).strip()
    if not s:
        return None
    has_percent = s.endswith("%")
    s = s.rstrip("%").strip()
    try:
        num = float(s)
    except ValueError:
        return None
    return num / 100.0 if has_percent else num


def _normalize_desc(value) -> str:
    """Normalise a product description for case-insensitive joins.

    Empty / NaN values map to the empty string so they never false-match.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip().lower()


def _parse_gallons_cell(value) -> Optional[float]:
    """Parse HTST ``Monthly Gallons`` cells that may contain commas / spaces."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").strip()
    if not s or s in {"-", "–", "—"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _strip_df_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Strip leading/trailing whitespace from every column name."""
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    return out


def _to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Serialise a DataFrame to UTF-8 CSV bytes for st.download_button."""
    return df.to_csv(index=False).encode("utf-8")


# ── 3. File ingestion ─────────────────────────────────────────────────────────

def _detect_role(filename: str) -> Optional[str]:
    """Classify an uploaded CSV by filename keyword.

    Returns the role id (e.g. "movers_tracker") or ``None`` when no keyword
    matches. Keywords are tested in ``_ROLE_KEYWORDS`` order so more specific
    matches win.
    """
    name = filename.lower()
    for role, keywords in _ROLE_KEYWORDS:
        if any(kw in name for kw in keywords):
            return role
    return None


def _file_sig(files: list) -> str:
    """Hash the (name, size) of each uploaded file to detect upload changes.

    Used as a cache key so we only re-parse the CSVs when the uploaded set
    actually changes — not on every widget rerun.
    """
    h = hashlib.md5()
    for f in sorted(files, key=lambda x: x.name):
        h.update(f.name.encode("utf-8"))
        h.update(str(getattr(f, "size", 0)).encode("utf-8"))
    return h.hexdigest()


@dataclass
class _Uploaded:
    """Parsed representation of one uploaded CSV.

    ``role`` is the classified role id; ``df`` is the raw DataFrame (no
    per-file cleaning — cleaning happens in the calculation functions so
    this stays a dumb data holder).
    """
    role: str
    filename: str
    df: pd.DataFrame


def _load_uploaded_files(files: list) -> dict[str, _Uploaded]:
    """Read uploaded files into a ``role → _Uploaded`` dict.

    Unclassified files are silently skipped so stray junk in the uploader
    doesn't block the pipeline. Read errors are surfaced as Streamlit errors
    but do not raise, keeping the UI responsive.
    """
    result: dict[str, _Uploaded] = {}
    for f in files:
        role = _detect_role(f.name)
        if role is None:
            continue
        try:
            f.seek(0)
            df = pd.read_csv(f)
        except Exception as exc:
            st.error(f"❌ Failed to read `{f.name}`: {exc}")
            continue
        result[role] = _Uploaded(role=role, filename=f.name, df=df)
    return result


# ── 4. Chart builder ──────────────────────────────────────────────────────────

def _build_packaging_index_chart(pkg_df: pd.DataFrame) -> go.Figure:
    """Build a multi-series time-series chart of the Packaging Index.

    Resin price series (HDPE, LDPE, PET, PP) are drawn on the primary y-axis
    in $/lb; Linerboard (if present) is drawn on a secondary y-axis in $/ton
    because it lives on a completely different scale.
    """
    df = pkg_df.copy()
    if "Time" not in df.columns:
        return go.Figure()

    df["Time"] = pd.to_datetime(df["Time"], errors="coerce")
    df = df.dropna(subset=["Time"]).sort_values("Time")

    resin_cols = [c for c in df.columns if c != "Time" and "linerboard" not in c.lower()]
    board_cols = [c for c in df.columns if "linerboard" in c.lower()]

    palette = ["#1f77b4", "#2ca02c", "#d62728", "#9467bd", "#ff7f0e", "#17becf"]

    fig = go.Figure()
    for idx, col in enumerate(resin_cols):
        fig.add_trace(go.Scatter(
            x=df["Time"],
            y=pd.to_numeric(df[col], errors="coerce"),
            mode="lines+markers",
            name=col,
            line=dict(color=palette[idx % len(palette)], width=2),
            marker=dict(size=5),
            hovertemplate=f"<b>{col}</b><br>%{{x|%b %Y}}<br>$%{{y:.3f}}<extra></extra>",
        ))

    for col in board_cols:
        fig.add_trace(go.Scatter(
            x=df["Time"],
            y=pd.to_numeric(df[col], errors="coerce"),
            mode="lines+markers",
            name=col,
            line=dict(color="#7f7f7f", width=2, dash="dash"),
            marker=dict(size=5),
            yaxis="y2",
            hovertemplate=f"<b>{col}</b><br>%{{x|%b %Y}}<br>$%{{y:.0f}}<extra></extra>",
        ))

    # Chart title is intentionally omitted here — the section header above the
    # chart (rendered in _render_chart via st.markdown) is the single source
    # of truth for this section's name. The top margin is reduced to reclaim
    # the vertical space the title would have occupied.
    layout_kwargs = dict(
        xaxis=dict(title="", showgrid=False, showline=True, linecolor="#e0e0e0"),
        yaxis=dict(title="Resin ($/lb)", showgrid=True, gridcolor="#f0f0f0",
                   showline=True, linecolor="#e0e0e0",
                   rangemode="tozero"),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(l=50, r=50, t=30, b=80),
        height=360,
        # Legend is placed BELOW the plot (orientation="h", y<0) so the long
        # procurement column labels get room to breathe even at half-width,
        # and the font size is bumped to 14px for readability — matching the
        # larger-font request without crowding the plot area above.
        legend=dict(
            orientation="h",
            yanchor="top", y=-0.18,
            xanchor="center", x=0.5,
            font=dict(size=14),
        ),
        hovermode="x unified",
    )
    if board_cols:
        layout_kwargs["yaxis2"] = dict(
            title="Linerboard ($/ton)", overlaying="y", side="right",
            showgrid=False, showline=True, linecolor="#e0e0e0",
            rangemode="tozero",
        )
    fig.update_layout(**layout_kwargs)
    return fig


# ── 5. Editable table ─────────────────────────────────────────────────────────

def _visible_movers_window(
    movers_df: pd.DataFrame,
    current_month: pd.Timestamp,
) -> pd.DataFrame:
    """Return Movers Tracker rows through the month AFTER current_month.

    Parses the Month column with ``_parse_month`` so mixed string / date
    inputs work. Rows whose month cannot be parsed are dropped (they usually
    indicate formatting drift and are safe to omit from the editor).
    """
    if movers_df.empty or _COL_MONTH not in movers_df.columns:
        return movers_df.head(0)

    df = movers_df.copy()
    df["_month_dt"] = df[_COL_MONTH].apply(_parse_month)
    df = df.dropna(subset=["_month_dt"])

    cutoff = (current_month + pd.DateOffset(months=1)).normalize().replace(day=1)
    df = df[df["_month_dt"] <= cutoff].sort_values("_month_dt").reset_index(drop=True)
    df = df.drop(columns=["_month_dt"])
    return df


def _render_editable_movers(
    movers_df: pd.DataFrame,
    current_month: pd.Timestamp,
) -> pd.DataFrame:
    """Render the Movers Tracker with the final row's numeric columns editable.

    Layout
    ------
    * Rows [0 : -1]  → read-only ``st.dataframe`` (historical / locked).
    * Row  [-1]      → single-row ``st.data_editor``. Both the Resin Cost
                       ($/lbs) and Freight Mover ($/lbs) cells are editable;
                       the Month cell stays locked so the row can't drift to
                       the wrong month by accident.

    Root-cause note (why a single ``st.data_editor`` over the full window
    did not work): mixing ``None`` with a numeric in an editable column
    yields an ``object`` dtype. When ``st.data_editor`` sees an object dtype
    under a ``NumberColumn`` config, it silently disables editing on cells
    that already carry a concrete number — exactly the pathology that
    previously locked the May row. Splitting the final row into its own
    editor gives each editable cell a clean single-value numeric column.

    Returns the full visible window with the edited last-row values merged
    back in, so downstream calculations can keep using it unchanged.
    """
    visible = _visible_movers_window(movers_df, current_month)
    if visible.empty:
        st.info("No rows to display in the Movers Tracker for the configured window.")
        return visible

    static_part = visible.iloc[:-1].reset_index(drop=True)
    last_row    = visible.iloc[-1:].reset_index(drop=True).copy()

    # Render the static historical rows.
    if not static_part.empty:
        st.dataframe(static_part, hide_index=True, use_container_width=True)

    # Columns the user is allowed to edit in the last row. We resolve them by
    # name (not position) so re-ordering the source CSV columns doesn't flip
    # which cells are editable.
    editable_cols: tuple[str, ...] = tuple(
        c for c in (_COL_RESIN_LBS, _COL_FREIGHT_LBS) if c in last_row.columns
    )

    # Coerce each editable cell to a clean float64 so ``NumberColumn`` sees a
    # numeric dtype (never ``object``). NaN is the correct "blank" value —
    # it survives the editor round-trip without type drift.
    for col in editable_cols:
        last_row[col] = pd.to_numeric(
            last_row[col].apply(_parse_money), errors="coerce",
        ).astype("float64")

    # Column config: every non-editable column is explicitly disabled; each
    # editable column becomes a NumberColumn with matching formatting.
    column_config: dict = {
        col: st.column_config.Column(col, disabled=True)
        for col in last_row.columns if col not in editable_cols
    }
    for col in editable_cols:
        column_config[col] = st.column_config.NumberColumn(
            col,
            help=f"Editable — enter the {col} value for the last month.",
            format="%.4f",
            step=0.0001,
        )

    edited_last = st.data_editor(
        last_row,
        column_config=column_config,
        num_rows="fixed",
        use_container_width=True,
        hide_index=True,
        key=f"{_SS_PREFIX}_last_row_editor",
    )

    # Recombine so downstream consumers (e.g. _resin_cost_next_month) can keep
    # treating this as one tidy DataFrame keyed on Month.
    return pd.concat([static_part, edited_last], ignore_index=True)


# ── 6. Calculations ───────────────────────────────────────────────────────────

def _latest_scrape_fraction(scrape_tracker_df: pd.DataFrame) -> float:
    """Extract the latest scrape / yield-loss rate from ``Scrape_Tracker``.

    The CSV has columns Month, Parameter, Resin where *Resin* holds a percent
    string like "1%". We pick the row with the latest Month and return the
    parsed fraction (0.01 for "1%"). Falls back to 0.0 on any parsing issue
    so the downstream multiplication stays numeric.
    """
    if scrape_tracker_df.empty:
        return 0.0
    df = scrape_tracker_df.copy()
    if "Month" in df.columns:
        df["_month_dt"] = df["Month"].apply(_parse_month)
        df = df.dropna(subset=["_month_dt"]).sort_values("_month_dt")
    if df.empty:
        return 0.0

    # "Resin" column holds the percent in the supplied spec (naming is
    # historical; the file itself is ``Scrape_Tracker``).
    value_col = "Resin" if "Resin" in df.columns else df.columns[-1]
    pct = _parse_percent(df.iloc[-1][value_col])
    return float(pct) if pct is not None else 0.0


def _resin_cost_next_month(
    edited_movers: pd.DataFrame,
    current_month: pd.Timestamp,
) -> Optional[float]:
    """Pick the Resin Cost ($/lbs) value for the month AFTER ``current_month``.

    Per the product spec, this is "the resin cost from the last column [row]
    (the month after current month)" — i.e. the final visible row in the
    editor because we already truncated to current_month + 1.
    """
    if edited_movers.empty or _COL_RESIN_LBS not in edited_movers.columns:
        return None

    df = edited_movers.copy()
    df["_month_dt"] = df[_COL_MONTH].apply(_parse_month)
    target = (current_month + pd.DateOffset(months=1)).normalize().replace(day=1)
    match = df[df["_month_dt"] == target]
    if match.empty:
        return None
    return _parse_money(match.iloc[0][_COL_RESIN_LBS])


def _freight_mover_next_month(
    edited_movers: pd.DataFrame,
    current_month: pd.Timestamp,
) -> Optional[float]:
    """Freight Mover ($/lbs) for the month after ``current_month`` (editable row)."""
    if edited_movers.empty or _COL_FREIGHT_LBS not in edited_movers.columns:
        return None
    df = edited_movers.copy()
    df["_month_dt"] = df[_COL_MONTH].apply(_parse_month)
    target = (current_month + pd.DateOffset(months=1)).normalize().replace(day=1)
    match = df[df["_month_dt"] == target]
    if match.empty:
        return None
    val = match.iloc[0][_COL_FREIGHT_LBS]
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return None if pd.isna(val) else float(val)
    return _parse_money(val)


# Sentinel value meaning "row is in scope for the monthly impact calculations"
# in the HTST ``Include/N`` flag column. Comparisons are case-insensitive and
# whitespace-trimmed — see ``_filter_htst_included``.
_INCLUDE_FLAG_YES: str = "Y"


def _norm_header(name: str) -> str:
    """Return a lower-case, alphanumeric-only version of a column header.

    Used by ``_resolve_htst_column`` so small header variations ("SHIPTONAME"
    vs "Ship-To Name", "PRODUCTDESC" vs "Product Desc") all resolve to the
    same logical column without maintaining an exhaustive alias list.
    """
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _resolve_htst_column(df: pd.DataFrame, *needles: str) -> Optional[str]:
    """Return the first column whose normalised header contains *all* ``needles``.

    The needles are themselves normalised, so callers can pass natural-language
    hints like ``"product"``, ``"group"`` without worrying about spacing or case.
    Returns ``None`` when no column matches — callers decide whether that is a
    hard error or a soft fallback.
    """
    target = [_norm_header(n) for n in needles]
    for c in df.columns:
        cn = _norm_header(c)
        if all(n in cn for n in target):
            return c
    return None


def _resolve_include_flag_column(df: pd.DataFrame) -> Optional[str]:
    """Locate the HTST Include/N flag column.

    Tries the canonical "Include/N" header first (exact, whitespace-insensitive)
    and falls back to any column starting with "include" so mild header drift
    (``"Include"`` or ``"Include (Y/N)"``) still works.
    """
    for c in df.columns:
        if str(c).strip().lower().replace(" ", "") in {"include/n", "includen"}:
            return c
    for c in df.columns:
        if str(c).strip().lower().startswith("include"):
            return c
    return None


def _filter_htst_included(htst_df: pd.DataFrame) -> pd.DataFrame:
    """Return HTST rows where the Include/N flag equals ``"Y"``.

    Column whitespace is stripped first so downstream column resolution always
    sees clean headers. When the flag column cannot be located the unfiltered
    (but stripped) DataFrame is returned — this keeps legacy files without the
    Include/N column working while newer files gain the Costco/Walmart/USF
    exclusion automatically.
    """
    htst = _strip_df_columns(htst_df)
    flag_col = _resolve_include_flag_column(htst)
    if flag_col is None:
        return htst
    mask = (
        htst[flag_col].astype(str).str.strip().str.upper()
        == _INCLUDE_FLAG_YES
    )
    return htst.loc[mask].copy()


def _resolve_htst_columns(htst_df: pd.DataFrame) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve PRODUCTDESC, Monthly Gallons, and Pricing Method column names.

    Thin convenience wrapper around ``_resolve_htst_column`` that returns the
    three columns most HTST consumers need in a single call.
    """
    return (
        _resolve_htst_column(htst_df, "productdesc"),
        _resolve_htst_column(htst_df, "monthly", "gallon"),
        _resolve_htst_column(htst_df, "pricing", "method"),
    )


def _build_htst_gallons_lookup(htst_df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """One row per normalised product description with summed monthly gallons.

    Applies the Include/N == Y filter before aggregating so excluded customers
    (Costco / Walmart / USF) never contribute volume to ``resin_mover_fg``.
    """
    htst = _filter_htst_included(htst_df)
    prod_c, mg_c, _pm = _resolve_htst_columns(htst)
    if prod_c is None or mg_c is None:
        return None
    htst = htst.copy()
    htst["_mg"] = htst[mg_c].map(_parse_gallons_cell)
    grouped = (
        htst.groupby(prod_c, dropna=False, as_index=False)["_mg"]
        .sum(min_count=1)
        .rename(columns={"_mg": "Est. Monthly Gallons"})
    )
    grouped["_match_key"] = grouped[prod_c].map(_normalize_desc)
    return grouped[["_match_key", "Est. Monthly Gallons"]]


# Canonical output column names for the freight-impact detail table. Kept as
# module-level constants so the UI and the builder stay in lockstep — reorder
# or rename in one place.
FREIGHT_IMPACT_DETAIL_COLUMNS: tuple[str, ...] = (
    "Customer",
    "SHIPTONAME",
    "PRODUCTDESC",
    "Product Group",
    "Annualized Gallons",
    "Monthly Gallons",
    "Pricing Method",
    "Monthly Freight Mover",
)


def _build_freight_impact_detail(
    htst_df: pd.DataFrame,
    freight_per_lb: Optional[float],
) -> tuple[pd.DataFrame, Optional[str]]:
    """Build the per-row table that backs the Est. Monthly Freight Impact metric.

    Columns (see ``FREIGHT_IMPACT_DETAIL_COLUMNS``):
      Customer | SHIPTONAME | PRODUCTDESC | Product Group |
      Annualized Gallons | Monthly Gallons | Pricing Method |
      Monthly Freight Mover

    The first seven columns are sourced verbatim from the HTST annualized
    gallons file, restricted to rows where ``Include/N == "Y"``. The final
    column is computed as::

        Monthly Freight Mover
            = Monthly Gallons × Pricing Method × 8.6 × freight_per_lb

    Returns ``(detail_df, warning_message)``. When the HTST file is missing
    any required source column, ``detail_df`` is returned empty with the
    shape intact and ``warning_message`` explains which columns were absent,
    so the UI can surface a friendly warning without crashing the page.
    """
    empty = pd.DataFrame(columns=list(FREIGHT_IMPACT_DETAIL_COLUMNS))
    htst = _filter_htst_included(htst_df)
    if htst.empty:
        return empty, None

    # Resolve every source column up-front; report missing ones collectively so
    # the user fixes the headers once rather than in a whack-a-mole loop.
    resolved = {
        "Customer":           _resolve_htst_column(htst, "customer"),
        "SHIPTONAME":         _resolve_htst_column(htst, "shipto", "name"),
        "PRODUCTDESC":        _resolve_htst_column(htst, "productdesc"),
        "Product Group":      _resolve_htst_column(htst, "product", "group"),
        "Annualized Gallons": _resolve_htst_column(htst, "annualized", "gallon"),
        "Monthly Gallons":    _resolve_htst_column(htst, "monthly", "gallon"),
        "Pricing Method":     _resolve_htst_column(htst, "pricing", "method"),
    }
    missing = [label for label, src in resolved.items() if src is None]
    if missing:
        return empty, (
            "Could not resolve HTST column(s): " + ", ".join(missing) +
            ". The Est. Monthly Freight Impact detail table will be empty."
        )

    # Numeric coercion for the three columns feeding the multiplication.
    monthly_gal   = htst[resolved["Monthly Gallons"]].map(_parse_gallons_cell)
    annual_gal    = htst[resolved["Annualized Gallons"]].map(_parse_gallons_cell)
    pricing_meth  = pd.to_numeric(htst[resolved["Pricing Method"]], errors="coerce")

    fm = float(freight_per_lb) if freight_per_lb is not None and not pd.isna(freight_per_lb) else None
    if fm is None:
        monthly_freight_mover = pd.Series([float("nan")] * len(htst), index=htst.index)
    else:
        monthly_freight_mover = (
            monthly_gal.fillna(0.0)
            * pricing_meth.fillna(0.0)
            * 8.6
            * fm
        )

    detail = pd.DataFrame({
        "Customer":           htst[resolved["Customer"]].values,
        "SHIPTONAME":         htst[resolved["SHIPTONAME"]].values,
        "PRODUCTDESC":        htst[resolved["PRODUCTDESC"]].values,
        "Product Group":      htst[resolved["Product Group"]].values,
        "Annualized Gallons": annual_gal.round(2).values,
        "Monthly Gallons":    monthly_gal.round(2).values,
        "Pricing Method":     pricing_meth.values,
        "Monthly Freight Mover": monthly_freight_mover.round(2).values,
    })
    # Preserve declared column order defensively even if pandas reorders.
    return detail[list(FREIGHT_IMPACT_DETAIL_COLUMNS)], None


def _enrich_resin_mover_fg_with_htst(
    resin_mover_fg: pd.DataFrame,
    htst_df: pd.DataFrame,
) -> tuple[pd.DataFrame, Optional[str]]:
    """Left-join ``resin_mover_fg`` to HTST gallons on product description.

    Adds ``Est. Monthly Gallons`` (summed Monthly Gallons per PRODUCTDESC)
    and ``Est. Monthly Resin Impact`` = ``resin mover $/gal`` × that volume.
    """
    if resin_mover_fg.empty:
        return resin_mover_fg, None

    desc_col = "product description"
    if desc_col not in resin_mover_fg.columns:
        out = resin_mover_fg.copy()
        out["Est. Monthly Gallons"] = pd.NA
        out["Est. Monthly Resin Impact"] = pd.NA
        return out, "resin_mover_fg is missing the `product description` column."

    lookup = _build_htst_gallons_lookup(htst_df)
    if lookup is None:
        out = resin_mover_fg.copy()
        out["Est. Monthly Gallons"] = pd.NA
        out["Est. Monthly Resin Impact"] = pd.NA
        return out, (
            "Could not resolve PRODUCTDESC and/or Monthly Gallons columns "
            "in the HTST annualized gallons file."
        )

    out = resin_mover_fg.copy()
    out["_match_key"] = out[desc_col].map(_normalize_desc)
    out = out.merge(lookup, on="_match_key", how="left")
    out = out.drop(columns=["_match_key"], errors="ignore")

    mover = pd.to_numeric(out["resin mover $/gal"], errors="coerce")
    vol = pd.to_numeric(out["Est. Monthly Gallons"], errors="coerce")
    out["Est. Monthly Gallons"] = vol.round(2)
    out["Est. Monthly Resin Impact"] = (mover * vol).round(2)
    return out, None


def _resolve_price_ea_column(df: pd.DataFrame) -> Optional[str]:
    """Locate the ``Price $/EA`` column (handles minor header variants)."""
    for c in df.columns:
        cl = c.lower().replace(" ", "")
        if "price" in cl and ("$/ea" in c.lower() or "/ea" in c.lower()):
            return c
    return next((c for c in df.columns if "price" in c.lower()), None)


def _resolve_item_description_column(df: pd.DataFrame) -> Optional[str]:
    """Locate the item / product description column for joining to ``resin_mover_fg``."""
    for c in df.columns:
        cl = c.lower().replace(" ", "")
        if "item" in cl and "description" in cl:
            return c
        if cl == "productdescription":
            return c
    return next((c for c in df.columns if "description" in c.lower()), None)


def _resin_mover_gal_by_description(resin_mover_fg: pd.DataFrame) -> pd.DataFrame:
    """One row per normalised product description with mean ``resin mover $/gal``."""
    if resin_mover_fg.empty or "product description" not in resin_mover_fg.columns:
        return pd.DataFrame(columns=["_match_key", "resin mover $/gal"])
    fg = resin_mover_fg.copy()
    fg["_match_key"] = fg["product description"].map(_normalize_desc)
    return (
        fg.dropna(subset=["_match_key"])
        .groupby("_match_key", as_index=False)["resin mover $/gal"]
        .mean()
    )


def _build_example_prices_impact_table(
    example_df: pd.DataFrame,
    resin_mover_fg: pd.DataFrame,
    freight_per_lb: Optional[float],
) -> tuple[pd.DataFrame, Optional[str]]:
    """Enrich example-prices rows with resin / freight movers and price impact %.

    * **Resin Mover $/EA** — looked up from ``resin_mover_fg`` on a normalised
      join between ``Item Description`` (or equivalent) and
      ``product description``. The source field is ``resin mover $/gal``; the
      example file has no per-SKU gal/ea, so that $/gal value is carried into
      the $/EA column as the line-item driver (equivalent to gal/ea = 1).
    * **Freight Mover $/EA** — uniform across rows:
      ``freight_mover ($/lbs) × 8.6`` from the Movers tracker last row.
    * **Price Increase%** — ``(Resin Mover $/EA + Freight Mover $/EA) / Price $/EA``
      as a percentage (0–100 scale in the numeric cell; rendered with a %% sign
      in the UI).

    New columns are inserted immediately after the resolved ``Price $/EA``
    column so the table reads naturally left-to-right.
    """
    ex = _strip_df_columns(example_df)
    price_col = _resolve_price_ea_column(ex)
    desc_col = _resolve_item_description_column(ex)
    if price_col is None:
        return ex, "Could not find a `Price $/EA` column in the example prices file."
    if desc_col is None:
        return ex, "Could not find an item / product description column for joining."

    out = ex.copy()
    out["_match_key"] = out[desc_col].map(_normalize_desc)

    rm_lookup = _resin_mover_gal_by_description(resin_mover_fg)
    out = out.merge(rm_lookup, on="_match_key", how="left")
    out = out.drop(columns=["_match_key"], errors="ignore")

    # Carry $/gal into the $/EA column per business rule (see docstring).
    out["Resin Mover $/EA"] = pd.to_numeric(out["resin mover $/gal"], errors="coerce")
    out = out.drop(columns=["resin mover $/gal"], errors="ignore")

    if freight_per_lb is not None and not pd.isna(freight_per_lb):
        out["Freight Mover $/EA"] = float(freight_per_lb) * 8.6
    else:
        out["Freight Mover $/EA"] = float("nan")

    price = pd.to_numeric(out[price_col], errors="coerce")
    resin_ea = pd.to_numeric(out["Resin Mover $/EA"], errors="coerce")
    freight_ea = pd.to_numeric(out["Freight Mover $/EA"], errors="coerce")
    mover_sum = resin_ea.fillna(0.0) + freight_ea.fillna(0.0)
    # Use NA when price missing or zero so we never show a misleading 0%.
    out["Price Increase%"] = (
        (mover_sum / price.replace(0, pd.NA)) * 100.0
    ).where(price.notna() & price.ne(0), other=pd.NA)

    # Round currency / % columns for a clean table.
    out["Resin Mover $/EA"]   = resin_ea.round(4)
    out["Freight Mover $/EA"] = freight_ea.round(4)
    out["Price Increase%"]   = pd.to_numeric(out["Price Increase%"], errors="coerce").round(2)

    # Insert new columns immediately after ``Price $/EA``.
    base_cols = [c for c in out.columns if c not in (
        "Resin Mover $/EA", "Freight Mover $/EA", "Price Increase%",
    )]
    ix = base_cols.index(price_col)
    ordered = (
        base_cols[: ix + 1]
        + ["Resin Mover $/EA", "Freight Mover $/EA", "Price Increase%"]
        + base_cols[ix + 1 :]
    )
    out = out[[c for c in ordered if c in out.columns]]
    return out, None


def _build_resincalculate(
    resin_calculator_df: pd.DataFrame,
    resin_cost_per_lb: float,
    scrape_fraction: float,
) -> pd.DataFrame:
    """Build the ``resincalculate`` DataFrame.

    Formula (per spec):
        Resin Cost ($/Gal) = resin_cost_per_lb
                           × Usage (Lbs/Ea)
                           × (1 + scrape_fraction)
                           ÷ Gal/Ea

    The input CSV is copied (never mutated) and the derived column is appended
    to the right so downstream joins can find it by name.
    """
    df = resin_calculator_df.copy()
    usage  = pd.to_numeric(df.get(_COL_USAGE_LBS), errors="coerce")
    gal_ea = pd.to_numeric(df.get(_COL_GAL_EA),   errors="coerce")

    # A zero or NaN gal/ea would yield inf/NaN; both surface as NaN after the
    # subsequent rename operations so no special handling is required here.
    df[_COL_RESIN_GAL] = (
        resin_cost_per_lb * usage * (1.0 + scrape_fraction) / gal_ea
    ).round(4)
    return df


def _build_updated_resin_cost_tracker(
    resin_cost_tracker_df: pd.DataFrame,
    resincalculate: pd.DataFrame,
    current_month: pd.Timestamp,
) -> pd.DataFrame:
    """Append a current-month duplicate of the latest-month rows and re-cost it.

    Steps
    -----
    1. Detect the latest month present in the tracker (= "last month").
    2. Duplicate every latest-month row, stamp each duplicate with the current
       month, and append them at the end (preserves the original rows).
    3. Clear the Resin Cost ($/Gal) values in the duplicated (current-month)
       block, then left-join ``resincalculate`` on
       ``Resin == Pricing Category`` to populate them.

    This produces a tracker whose head matches the source CSV exactly and
    whose tail contains freshly-calculated current-month rows.
    """
    df = resin_cost_tracker_df.copy()
    df["_month_dt"] = df[_COL_MONTH].apply(_parse_month)

    # Ensure we never pick a stale current-month block if the source CSV was
    # already updated at some point — always derive "last month" from months
    # strictly before the current one.
    past = df[df["_month_dt"] < current_month]
    if past.empty:
        # Fallback: use whichever months exist, pick the max. Prevents crashes
        # when the source CSV only contains current-month rows for some reason.
        last_month_dt = df["_month_dt"].max()
    else:
        last_month_dt = past["_month_dt"].max()

    last_month_rows = df[df["_month_dt"] == last_month_dt].copy()

    # Build the current-month block from the last-month rows. The Month cell
    # is formatted as M/D/YYYY to match the source CSV convention without
    # relying on platform-specific strftime directives (Windows vs Unix).
    current_block = last_month_rows.copy()
    current_block[_COL_MONTH] = (
        f"{current_month.month}/{current_month.day}/{current_month.year}"
    )
    current_block["_month_dt"] = current_month
    current_block[_COL_RESIN_GAL] = pd.NA

    # Left-join the new Resin Cost ($/Gal) onto the current-month block.
    lookup = (
        resincalculate[[_COL_PRICING_CAT, _COL_RESIN_GAL]]
        .rename(columns={_COL_PRICING_CAT: _COL_RESIN,
                         _COL_RESIN_GAL: f"{_COL_RESIN_GAL}__new"})
        .drop_duplicates(subset=[_COL_RESIN])
    )
    current_block = current_block.merge(lookup, on=_COL_RESIN, how="left")
    current_block[_COL_RESIN_GAL] = current_block[f"{_COL_RESIN_GAL}__new"]
    current_block = current_block.drop(columns=[f"{_COL_RESIN_GAL}__new"])

    # Concatenate: original rows first (preserved verbatim), then new block.
    combined = pd.concat([df, current_block], ignore_index=True)
    combined = combined.drop(columns=["_month_dt"])
    return combined


def _build_resin_mover_fg(
    updated_tracker: pd.DataFrame,
    current_month: pd.Timestamp,
) -> pd.DataFrame:
    """Build ``resin_mover_fg`` — per-product MoM Resin Cost ($/Gal) delta.

    Columns: Product ID | Product Description | Resin |
             new resin cost $/gal | old resin cost $/gal | resin mover $/gal

    Matching strategy
    -----------------
    * "new" = the current-month rows in ``updated_tracker``.
    * "old" = rows for the most recent month strictly prior to ``current_month``.
    * Products are matched on Product ID so renamed descriptions never
      accidentally unmatch a product.
    """
    df = updated_tracker.copy()
    df["_month_dt"] = df[_COL_MONTH].apply(_parse_month)

    new = df[df["_month_dt"] == current_month].copy()
    past = df[df["_month_dt"] < current_month].copy()
    if past.empty or new.empty:
        return pd.DataFrame(columns=[
            _COL_PRODUCT_ID, _COL_PRODUCT_DESC, _COL_RESIN,
            "new resin cost $/gal", "old resin cost $/gal", "resin mover $/gal",
        ])

    last_month_dt = past["_month_dt"].max()
    old = past[past["_month_dt"] == last_month_dt].copy()

    new_slim = (
        new[[_COL_PRODUCT_ID, _COL_PRODUCT_DESC, _COL_RESIN, _COL_RESIN_GAL]]
        .drop_duplicates(subset=[_COL_PRODUCT_ID], keep="last")
        .rename(columns={_COL_RESIN_GAL: "new resin cost $/gal"})
    )
    old_slim = (
        old[[_COL_PRODUCT_ID, _COL_RESIN_GAL]]
        .drop_duplicates(subset=[_COL_PRODUCT_ID], keep="last")
        .rename(columns={_COL_RESIN_GAL: "old resin cost $/gal"})
    )

    merged = new_slim.merge(old_slim, on=_COL_PRODUCT_ID, how="left")
    merged["new resin cost $/gal"] = pd.to_numeric(merged["new resin cost $/gal"], errors="coerce")
    merged["old resin cost $/gal"] = pd.to_numeric(merged["old resin cost $/gal"], errors="coerce")
    merged["resin mover $/gal"] = (
        merged["new resin cost $/gal"] - merged["old resin cost $/gal"]
    ).round(4)

    # Normalise column names to the lower-case spec the user specified.
    return merged.rename(columns={
        _COL_PRODUCT_ID:   "product id",
        _COL_PRODUCT_DESC: "product description",
        _COL_RESIN:        "resin",
    })[[
        "product id", "product description", "resin",
        "new resin cost $/gal", "old resin cost $/gal", "resin mover $/gal",
    ]]


def _compute_all_outputs(
    uploads: dict[str, _Uploaded],
    edited_movers: pd.DataFrame,
    current_month: pd.Timestamp,
) -> Optional[dict[str, pd.DataFrame]]:
    """Run the full calculation pipeline and return the three output DataFrames.

    Returns ``None`` when any required input is missing or the resin cost for
    the target month cannot be parsed. The caller surfaces a user-facing
    error in that case.
    """
    for role in REQUIRED_ROLES:
        if role not in uploads:
            st.error(f"❌ Missing required file: `{role}`. Please re-upload.")
            return None

    scrape_fraction = _latest_scrape_fraction(uploads["scrape_tracker"].df)
    resin_lbs = _resin_cost_next_month(edited_movers, current_month)
    if resin_lbs is None:
        st.error(
            "❌ Could not determine the Resin Cost ($/lbs) for "
            f"{(current_month + pd.DateOffset(months=1)).strftime('%b %Y')}. "
            "Make sure that row has a numeric value in the editable table."
        )
        return None

    resincalculate = _build_resincalculate(
        uploads["resin_calculator"].df, resin_lbs, scrape_fraction,
    )
    updated_tracker = _build_updated_resin_cost_tracker(
        uploads["resin_cost_tracker"].df, resincalculate, current_month,
    )
    resin_mover_fg_base = _build_resin_mover_fg(updated_tracker, current_month)

    htst_df = uploads["htst_annualized_gallons"].df
    resin_mover_fg, join_msg = _enrich_resin_mover_fg_with_htst(
        resin_mover_fg_base, htst_df,
    )
    if join_msg:
        st.warning(f"⚠️ {join_msg}")

    impact_col = "Est. Monthly Resin Impact"
    monthly_resin_impact_total = float(
        pd.to_numeric(resin_mover_fg.get(impact_col), errors="coerce").sum(skipna=True)
    ) if impact_col in resin_mover_fg.columns else 0.0

    # Freight $/lbs from the editable next-month row — needed both for the
    # portfolio-level freight impact metric and for per-example-row $/EA.
    freight_per_lb = _freight_mover_next_month(edited_movers, current_month)

    example_impact, ex_warn = _build_example_prices_impact_table(
        uploads["example_prices"].df, resin_mover_fg, freight_per_lb,
    )
    if ex_warn:
        st.warning(f"⚠️ {ex_warn}")

    # Freight-impact detail table (Include/N == Y) — drives both the download
    # and the Est. Monthly Freight Impact metric (metric = Σ Monthly Freight
    # Mover). Deriving the metric from the same table we expose to the user
    # guarantees the number on screen always reconciles with the download.
    freight_detail, fd_warn = _build_freight_impact_detail(htst_df, freight_per_lb)
    if fd_warn:
        st.warning(f"⚠️ {fd_warn}")

    est_monthly_freight_impact: Optional[float] = None
    if freight_per_lb is not None and not freight_detail.empty:
        est_monthly_freight_impact = float(
            pd.to_numeric(
                freight_detail["Monthly Freight Mover"], errors="coerce",
            ).sum(skipna=True)
        )

    return {
        "resincalculate":  resincalculate,
        "updated_tracker": updated_tracker,
        "resin_mover_fg":  resin_mover_fg,
        "example_prices_impact": example_impact,
        "monthly_resin_impact_total": monthly_resin_impact_total,
        "est_monthly_freight_impact": est_monthly_freight_impact,
        "freight_impact_detail": freight_detail,
        "_meta": pd.DataFrame([{
            "scrape_fraction":      scrape_fraction,
            "resin_cost_per_lb":    resin_lbs,
            "current_month":        current_month.strftime("%Y-%m-%d"),
            "next_month_of_editor": (current_month + pd.DateOffset(months=1)).strftime("%Y-%m-%d"),
            "freight_mover_next_mo_$lb": freight_per_lb,
            "freight_impact_row_count":  int(len(freight_detail)),
        }]),
    }


# ── 7. UI fragments ───────────────────────────────────────────────────────────

def _render_monthly_sop_and_upload_intro() -> None:
    """SharePoint guidance + Monthly SOP — always visible for this fragment."""
    st.markdown(
        "Upload all files in this SharePoint Folder "
        f"([LINK]({_MONTHLY_MOVERS_SHAREPOINT_URL}))."
    )
    st.markdown(
        """
**Monthly SOP**

- Maintain and refresh source files (**DO NOT change file names**):
  - `Pkg_Index` (Procurement)
  - `example_prices` (RGM)
  - `Movers_Tracker` (RGM), `Resin_Cost_Tracker` (RGM, make sure last month data is appended)
  - `Scrape_Tracker` (RGM, as needed)
  - `htst_annualized_gallons` (RGM, annually)
- Use the Mover Tracker to align movers with commercial leaders; click **Refresh** to see impact.
- Download `resin_mover_fg` in the **Impact** section for Pricing Execution.

_Files are still matched automatically by filename keyword after upload._
        """.strip()
    )


def _render_upload_panel() -> list:
    """Render the multi-file uploader (intro + SOP: `_render_monthly_sop_and_upload_intro`)."""
    return st.file_uploader(
        "Select Monthly Movers CSV files",
        type=["csv"],
        accept_multiple_files=True,
        key=f"{_SS_PREFIX}_uploader",
    ) or []


def _current_month_hdpe(pkg_df: pd.DataFrame, current_month: pd.Timestamp) -> Optional[float]:
    """Return the HDPE ($/lb) value for ``current_month`` from the packaging index.

    Works with either uploaded variant:
      * Packaging_Index_from_Bryan — uses column ``HDPE ($/lb)``
      * Pkg_Index_from_Bryan       — uses column ``HDPE``

    Returns ``None`` when the file is empty, the HDPE column is absent, or
    the current month is not present in the data — so the caller can simply
    skip rendering the metric without extra branching.
    """
    if pkg_df is None or pkg_df.empty or "Time" not in pkg_df.columns:
        return None

    hdpe_col = next(
        (c for c in pkg_df.columns if c.lower().startswith("hdpe")),
        None,
    )
    if hdpe_col is None:
        return None

    df = pkg_df.copy()
    df["_month_dt"] = df["Time"].apply(_parse_month)
    match = df[df["_month_dt"] == current_month]
    if match.empty:
        return None

    value = pd.to_numeric(match.iloc[0][hdpe_col], errors="coerce")
    return None if pd.isna(value) else float(value)


def _render_chart(uploads: dict[str, _Uploaded], current_month: pd.Timestamp) -> None:
    """Render the Packaging Index Outlook section: header → metric → chart.

    The section header is a plain markdown ``####`` heading so it picks up
    Streamlit's default section typography — matching the Freight Index
    Outlook below and the Mover Tracker further down. The Plotly chart
    itself no longer carries a title (see ``_build_packaging_index_chart``).
    """
    st.markdown("#### 📈 Packaging Index Outlook (from Procurement)")

    pkg = uploads.get("packaging_index") or uploads.get("pkg_index")
    if pkg is None:
        st.info(
            "📈 Upload `Packaging_Index_from_Bryan*.csv` to see the resin & "
            "linerboard trend chart."
        )
        return

    # HDPE current-month spotlight metric. The section now lives in a
    # half-width column, so the metric takes ~half of that (≈25% of screen)
    # — wide enough to read comfortably without overpowering the chart below.
    hdpe_value = _current_month_hdpe(pkg.df, current_month)
    metric_col, _spacer = st.columns([1, 1])
    with metric_col:
        st.metric(
            label=f"HDPE ($/lbs) — {current_month.strftime('%b %Y')}",
            value=f"${hdpe_value:.3f}" if hdpe_value is not None else "N/A",
            help="HDPE price from the uploaded Packaging Index for the "
                 "current month.",
        )

    fig = _build_packaging_index_chart(pkg.df)
    st.plotly_chart(fig, use_container_width=True, key=f"{_SS_PREFIX}_pkg_chart")


# ── Freight Index Outlook (EIA) ───────────────────────────────────────────────
#
# An independent mini-section: it owns its own session_state keys under the
# same ``_SS_PREFIX`` namespace so it never collides with the CSV upload flow.
# State machine
#   * No file cached  → show header + file uploader.
#   * File cached     → show header + rendered preview (image or PDF), the
#                       uploader is completely hidden, a small "Replace file"
#                       escape hatch lets the user reset.
#
# The file bytes are held in session_state rather than written to disk so the
# section stays ephemeral and no server-side cleanup is required.

# Session-state keys scoped to the freight-outlook mini-section.
_SS_FREIGHT_BYTES    = f"{_SS_PREFIX}_freight_bytes"
_SS_FREIGHT_MIME     = f"{_SS_PREFIX}_freight_mime"
_SS_FREIGHT_FILENAME = f"{_SS_PREFIX}_freight_filename"

# File extensions we accept in the uploader. PDF is displayed via a browser
# iframe; the rest go through ``st.image``. Keep this list tight — arbitrary
# binaries should NOT be rendered inline.
_FREIGHT_ACCEPTED_TYPES: tuple[str, ...] = ("pdf", "png", "jpg", "jpeg", "webp", "gif")


def _guess_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    """Return a minimal MIME type guess from a filename extension.

    We intentionally keep this inline (rather than pulling in ``mimetypes``)
    so the mapping is obvious, deterministic, and limited to the handful of
    types this section actually renders.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "pdf":  "application/pdf",
        "png":  "image/png",
        "jpg":  "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif":  "image/gif",
    }.get(ext, fallback)


def _clear_freight_state() -> None:
    """Evict the cached freight-outlook file from session_state."""
    for key in (_SS_FREIGHT_BYTES, _SS_FREIGHT_MIME, _SS_FREIGHT_FILENAME):
        st.session_state.pop(key, None)


def _render_freight_outlook() -> None:
    """Render the 'Freight Index Outlook (EIA)' upload-or-preview section.

    State contract
    --------------
    * **No file cached** → section header + single ``st.file_uploader``.
      Once the user picks a file, bytes are cached in session_state; the
      preview renders in the same run (no fragment-scoped rerun).
    * **File cached** → section header + rendered preview (image via
      ``st.image``, PDF via a base64 iframe) + a subtle "Replace file" button
      so the user can swap the outlook without reloading the page.
    """
    st.markdown("#### 🚚 Freight Index Outlook (EIA)")
    # Source instruction — the word "LINK" is hyperlinked to the EIA STEO PDF.
    # Using ``st.caption`` keeps the instruction visually secondary so it
    # doesn't compete with the section header above it.
    st.caption(
        "source: EIA.gov "
        "([LINK](https://www.eia.gov/outlooks/steo/pdf/steo_full.pdf))"
    )

    cached_bytes = st.session_state.get(_SS_FREIGHT_BYTES)

    # ── Upload state ─────────────────────────────────────────────────────────
    if not cached_bytes:
        uploaded = st.file_uploader(
            "Upload the EIA Freight Outlook (PDF or image)",
            type=list(_FREIGHT_ACCEPTED_TYPES),
            accept_multiple_files=False,
            key=f"{_SS_PREFIX}_freight_uploader",
            help="Accepted: PDF, PNG, JPG, JPEG, WEBP, GIF.",
        )
        if uploaded is None:
            return
        try:
            data = uploaded.getvalue()
        except Exception as exc:
            st.error(f"❌ Could not read `{uploaded.name}`: {exc}")
            return
        st.session_state[_SS_FREIGHT_BYTES]    = data
        st.session_state[_SS_FREIGHT_MIME]     = getattr(uploaded, "type", None) \
                                                  or _guess_mime(uploaded.name)
        st.session_state[_SS_FREIGHT_FILENAME] = uploaded.name
        # No fragment-scoped rerun here (see module note on Streamlit Cloud).
        # Re-read bytes — the local ``cached_bytes`` from the top of the function
        # is still stale in the branch where we just wrote session_state.
        cached_bytes = st.session_state.get(_SS_FREIGHT_BYTES)

    # ── Preview state ────────────────────────────────────────────────────────
    if not cached_bytes:
        return

    mime     = st.session_state.get(_SS_FREIGHT_MIME, "application/octet-stream")
    filename = st.session_state.get(_SS_FREIGHT_FILENAME, "uploaded-file")

    col_status, col_btn = st.columns([5, 1])
    with col_status:
        st.caption(f"Showing **{filename}**")
    with col_btn:
        if st.button(
            "🔄 Replace file",
            key=f"{_SS_PREFIX}_freight_replace",
            use_container_width=True,
            help="Remove the current file and show the upload panel again.",
        ):
            _clear_freight_state()
            # Full rerun: reliable on all hosts; fragment-scoped rerun is brittle
            # when the fragment stack is unavailable (see freight upload path).
            st.rerun()

    if mime == "application/pdf":
        # Browsers can render PDFs directly from a base64 data URL inside an
        # iframe — no extra Python deps (pdf2image/poppler) required. Height
        # is fixed at 720px which fits a typical one-page outlook comfortably.
        b64 = base64.b64encode(cached_bytes).decode("utf-8")
        st.markdown(
            f'<iframe src="data:application/pdf;base64,{b64}" '
            f'width="100%" height="720" '
            f'style="border: 1px solid #e0e0e0; border-radius: 4px;" '
            f'type="application/pdf"></iframe>',
            unsafe_allow_html=True,
        )
    else:
        # Everything else in _FREIGHT_ACCEPTED_TYPES is an image format that
        # ``st.image`` can render natively from raw bytes.
        st.image(cached_bytes, use_container_width=True, caption=filename)


def _render_table_and_refresh(
    uploads: dict[str, _Uploaded],
    current_month: pd.Timestamp,
) -> None:
    """Render the Mover Tracker (last-row editable) with an inline Refresh button.

    Clicking **Refresh** re-runs the full calculation pipeline against the
    currently-edited last-row value, so the user can iterate: tweak the
    editable cell → click Refresh → see updated ``resin_mover_fg`` below.
    """
    movers = uploads.get("movers_tracker")
    if movers is None:
        st.warning(
            "⚠️ `Movers_Tracker*.csv` is required to display and edit the "
            "monthly movers table."
        )
        return

    next_month_label = (current_month + pd.DateOffset(months=1)).strftime("%b %Y")
    st.markdown("#### 📝 Mover Tracker - last month row editable")
    st.caption(
        f"Only the **last row ({next_month_label})** is editable (Freight "
        f"Mover $/lbs). Adjust the value as needed, then click **Refresh** "
        "to recompute the resin cost tracker."
    )

    col_table, col_btn = st.columns([5, 1])
    with col_table:
        edited = _render_editable_movers(movers.df, current_month)
    with col_btn:
        st.markdown("<div style='margin-top: 2.2rem'></div>", unsafe_allow_html=True)
        refresh_clicked = st.button(
            "🔄 Refresh",
            type="primary",
            use_container_width=True,
            key=f"{_SS_PREFIX}_refresh",
            help="Rerun the resin cost calculations with the current "
                 "editable last-row value.",
        )

    if refresh_clicked:
        with st.spinner("Running resin cost calculations..."):
            outputs = _compute_all_outputs(uploads, edited, current_month)
        if outputs is not None:
            st.session_state[f"{_SS_PREFIX}_outputs"] = outputs
            st.success("✅ Calculations complete. Results below.")


def _render_results() -> None:
    """Render Impact metrics, CSV download, and the example-prices table.

    Surfaced only after a successful Refresh — otherwise this is a no-op.
    """
    outputs = st.session_state.get(f"{_SS_PREFIX}_outputs")
    if not outputs:
        return

    resin_mover_fg    = outputs["resin_mover_fg"]
    monthly_resin_total = outputs.get("monthly_resin_impact_total", 0.0)
    freight_impact    = outputs.get("est_monthly_freight_impact")
    freight_detail: pd.DataFrame = outputs.get(
        "freight_impact_detail", pd.DataFrame(columns=list(FREIGHT_IMPACT_DETAIL_COLUMNS)),
    )

    st.markdown("---")
    # Heading now flags the HTST Include/N filter so users immediately know
    # the Costco/Walmart/USF volumes have been excluded from both metrics and
    # the resin_mover_fg join.
    st.markdown("### Impact (EXCLUDE COSTCO, WALMART, USF)")
    st.caption(
        "Rows with HTST `Include/N` = **N** (Costco, Walmart, USF) are "
        "excluded. Download the per-product table (with HTST volumes and "
        "resin impact columns); summary metrics and example-price scenarios "
        "follow below."
    )

    if resin_mover_fg.empty:
        st.warning(
            "No rows produced for the resin mover. Check that the Resin Cost "
            "Tracker has a prior month of data to compare against."
        )
        return

    # Downloads are grouped side-by-side so the two artifacts that back the
    # metrics below — resin_mover_fg (backs Monthly Resin Impact) and
    # freight_mover_breakdown (backs Est. Monthly Freight Impact) — are
    # equally prominent and visually paired.
    today = datetime.now().strftime("%Y%m%d")
    dl_resin, dl_freight = st.columns(2, gap="medium")
    with dl_resin:
        st.download_button(
            label="⬇️ Download resin_mover_fg (CSV)",
            data=_to_csv_bytes(resin_mover_fg),
            file_name=f"resin_mover_fg_{today}.csv",
            mime="text/csv",
            use_container_width=True,
            key=f"{_SS_PREFIX}_download_resin_mover_fg",
        )
    with dl_freight:
        # Disable when the detail is empty (e.g. HTST column resolution failed
        # upstream and surfaced a warning) so we never hand users an empty CSV.
        st.download_button(
            label="⬇️ Download freight_mover_breakdown (CSV)",
            data=_to_csv_bytes(freight_detail),
            file_name=f"freight_mover_breakdown_{today}.csv",
            mime="text/csv",
            use_container_width=True,
            disabled=freight_detail.empty,
            help=(
                "Per-row backing for Est. Monthly Freight Impact. "
                "Monthly Freight Mover = Monthly Gallons × Pricing Method × "
                "8.6 × Freight Mover ($/lbs) from the last row of the "
                "Mover Tracker."
            ),
            key=f"{_SS_PREFIX}_download_freight_mover_breakdown",
        )

    m1, m2 = st.columns(2, gap="medium")
    with m1:
        st.metric(
            label="Monthly Resin Impact",
            value=f"${monthly_resin_total:,.2f}",
            help="Sum of Est. Monthly Resin Impact (resin mover $/gal × "
                 "Est. Monthly Gallons per FG row, after HTST left join "
                 "filtered to Include/N = Y).",
        )
    with m2:
        st.metric(
            label="Est. Monthly Freight Impact",
            value=(
                f"${freight_impact:,.2f}"
                if freight_impact is not None
                else "N/A"
            ),
            help=(
                "Σ over HTST rows (Include/N = Y) of "
                "Monthly Gallons × Pricing Method × 8.6 × Freight Mover "
                "($/lbs). Use **Download freight_mover_breakdown** above "
                "for the per-row backing."
            ),
        )

    example_impact = outputs.get("example_prices_impact")
    if example_impact is not None and not example_impact.empty:
        st.markdown("#### Example prices")
        _cfg_raw: dict = {
            "Resin Mover $/EA": st.column_config.NumberColumn(
                "Resin Mover $/EA",
                format="$%.4f",
                help="From ``resin_mover_fg``: ``resin mover $/gal`` matched on "
                     "Item Description (gal/ea assumed 1 when absent).",
            ),
            "Freight Mover $/EA": st.column_config.NumberColumn(
                "Freight Mover $/EA",
                format="$%.4f",
                help="Freight Mover ($/lbs) from the Movers last row × 8.6.",
            ),
            "Price Increase%": st.column_config.NumberColumn(
                "Price Increase%",
                help="(Resin Mover $/EA + Freight Mover $/EA) ÷ Price $/EA × 100.",
                format="%.2f",
            ),
        }
        cfg = {k: v for k, v in _cfg_raw.items() if k in example_impact.columns}
        st.dataframe(
            example_impact,
            column_config=cfg,
            hide_index=True,
            use_container_width=True,
            key=f"{_SS_PREFIX}_example_prices_table",
        )
        st.caption(
            "Values in **Price Increase%** are percentage points "
            "(e.g. `12.34` means 12.34%)."
        )


# ── 8. Public API ─────────────────────────────────────────────────────────────

def _clear_upload_state() -> None:
    """Evict every cached artifact for this section from session_state.

    Called when the user clicks "Change files" so the fragment returns to
    the pristine upload state without leaking stale uploads, file
    signatures, or computed outputs.
    """
    for key in (
        f"{_SS_PREFIX}_uploads",
        f"{_SS_PREFIX}_sig",
        f"{_SS_PREFIX}_outputs",
    ):
        st.session_state.pop(key, None)


@st.fragment
def render_monthly_resin_freight_mover_tracker() -> None:
    """Render the Monthly Resin & Freight Mover Tracker as an isolated fragment.

    Using ``@st.fragment`` means uploads, edits and the Refresh button only
    trigger a rerun of THIS section — the Market Indices dashboard and
    Walmart Fresh Tracker above/below remain untouched.

    State machine
    -------------
    * **Upload state** (no cached uploads)
        → Render the file uploader. On successful upload, cache the parsed
          files in session_state and continue in the same run (no
          ``st.rerun(scope="fragment")`` — see Robustness note in module doc).
    * **Processed state** (uploads cached)
        → Uploader is hidden. A "Change files" button clears state and calls
          full ``st.rerun()``. Chart, Mover Tracker, Refresh, and Impact follow.
    """
    # Always recompute "current month" at render time so the section advances
    # automatically month-over-month without code changes.
    current_month = pd.Timestamp(date.today().replace(day=1))

    _render_monthly_sop_and_upload_intro()

    uploads: dict[str, _Uploaded] | None = st.session_state.get(f"{_SS_PREFIX}_uploads")

    # ── Upload state ──────────────────────────────────────────────────────────
    if not uploads:
        files = _render_upload_panel()
        if not files:
            return
        sig = _file_sig(files)
        uploads = _load_uploaded_files(files)
        st.session_state[f"{_SS_PREFIX}_uploads"] = uploads
        st.session_state[f"{_SS_PREFIX}_sig"] = sig
        # Drop any stale outputs (defensive — the key should already be absent).
        st.session_state.pop(f"{_SS_PREFIX}_outputs", None)
        # Same-run continuation: ``st.rerun(scope="fragment")`` raises on
        # Streamlit Cloud when the runtime has no active fragment id (common
        # when this fragment is nested under ``st.expander`` in the parent page).

    # ── Processed state ───────────────────────────────────────────────────────
    # Uploader and detected-files summary are intentionally NOT rendered here
    # so the section stays clean post-upload. A single escape-hatch button
    # lets the user reset back to the upload state.
    if st.button(
        "📁 Change files",
        key=f"{_SS_PREFIX}_change_files",
        help="Clear the currently-loaded files and return to the upload panel.",
    ):
        _clear_upload_state()
        st.rerun()

    st.markdown("---")
    # Packaging Index Outlook and Freight Index Outlook are laid out in two
    # equal-width columns to keep the view compact and let the eye compare
    # the packaging-resin trend against the freight outlook side-by-side.
    # ``gap="medium"`` adds a tasteful visual separator between the two.
    col_pkg, col_freight = st.columns(2, gap="medium")
    with col_pkg:
        _render_chart(uploads, current_month)
    with col_freight:
        _render_freight_outlook()
    st.markdown("---")
    _render_table_and_refresh(uploads, current_month)
    _render_results()
