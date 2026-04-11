"""
HTST Shipment Monitor page view.

Sections
--------
1. Types & constants   (_FilePattern, _FILE_PATTERNS, _DROP_COLS, _PREVIEW_ROWS,
                        _PALLET_FULL_ROW_MIN, _PALLET_FULL_AGG_MIN)
2. I/O helpers         (_detect_files, _to_csv_bytes, _widget_key)
3. DataFrame utilities (_insert_col_after, _drop_blank_columns)
4. Processing pipeline (_process_shipment_data)
5. Analytics           (_compute_duration, _build_customer_site_summary)
6. UI rendering        (_render_upload_section, _render_filters,
                        _render_customer_site_details, _render_output_section)
7. Entry point         (render)

Design notes
------------
* A single multi-file uploader accepts all CSVs at once.  Files are matched to
  their roles by case-insensitive keyword search on the filename, so exact
  naming is not required.
* The full enriched DataFrame is NEVER pushed to the browser as a table.  Only
  _PREVIEW_ROWS rows go through st.dataframe().  Download buttons stream via
  HTTP (not the WebSocket), so they work for any dataset size on Streamlit Cloud.
* Duration is computed once from the unfiltered enriched dataset so it remains
  stable regardless of filter state.
* Pallet classification uses two thresholds defined in section 1:
    _PALLET_FULL_ROW_MIN : per-row — Pallet% >= threshold → "Full".
    _PALLET_FULL_AGG_MIN : summary — Full Pallet% > threshold → "Full".
* All lookup joins in _build_customer_site_summary receive pre-loaded DataFrames
  passed from render().  No function below section 4 reads from disk or from
  local file paths.
* Exactly two CSV outputs are produced: the filtered enriched report and the
  Customer-Site Summary.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import NamedTuple, Optional

import pandas as pd
import streamlit as st

from utils.ui_helpers import apply_custom_css


# ── 1. Types & constants ──────────────────────────────────────────────────────

class _FilePattern(NamedTuple):
    key:      str        # dict key used throughout this module
    required: bool       # must be present before processing can start
    label:    str        # human-readable display name
    keywords: list[str]  # any one of these substrings (case-insensitive) in
                         # the filename causes this pattern to match


# Order matters: the first matching pattern claims each uploaded file.
_FILE_PATTERNS: list[_FilePattern] = [
    _FilePattern("shipment",        True,  "HTST Shipment Report",           ["htst shipment", "htst_shipment", "shipment report"]),
    _FilePattern("plant_tracker",   True,  "Shipment Plant Tracker",          ["plant_tracker", "plant tracker", "shipment_plant"]),
    _FilePattern("mileage_tracker", True,  "Ship Route Mileage Tracker",      ["mileage_tracker", "mileage tracker", "route_mileage", "route mileage"]),
    _FilePattern("demantra",        True,  "Demantra Item Master",            ["demantra"]),
    _FilePattern("pricing_tracker", True,  "Delivered vs FOB Pricing Tracker",["delivered vs fob", "delivered_vs_fob", "fob_tracker", "fob tracker"]),
    _FilePattern("custom_label",    False, "Custom Label Volume Bracket Fee", ["custom label", "custom_label"]),
    _FilePattern("delivery_miles",  False, "Delivery Miles Tier Fee",         ["delivery", "miles tier", "drop size"]),
    _FilePattern("pallet_fee",      False, "Pallet Fee",                      ["pallet_fee", "pallet fee"]),
    _FilePattern("sell_to",         False, "Sell-To Volume Bracket Fee",      ["sell-to", "sell_to"]),
]

# Columns to remove from the enriched output — not needed downstream.
_DROP_COLS = ["Reason Code", "Include for Fill Rate Calculations", "Past Due by Request Date"]

# Maximum rows shown in the browser preview to avoid WebSocket message-size errors.
_PREVIEW_ROWS = 500

# Pallet classification thresholds — single source of truth for both views.
# _PALLET_FULL_ROW_MIN : per-row  — Pallet% >= threshold → row "Full".
# _PALLET_FULL_AGG_MIN : summary  — Full Pallet% > threshold → site "Full".
_PALLET_FULL_ROW_MIN: float = 0.9
_PALLET_FULL_AGG_MIN: float = 0.8

# Sell-to volume bracket classification.
#
# Thresholds are hardcoded because the bracket labels in Sell-to_Volume_Bracket_Fee.csv
# are free-text strings ("A. >= 1MM", "F. <= 10K", etc.).  Parsing those strings
# programmatically to extract operators and K/MM multipliers is fragile and
# adds meaningful complexity for a 6-row table that rarely changes.
#
# Fee VALUES remain dynamic — they are read from the uploaded CSV at runtime
# and joined on the bracket label.  _SELLTO_FEES_FALLBACK is used only when
# the Sell-To file was not uploaded.
_SELLTO_BINS: list[float] = [
    float("-inf"), 10_000, 100_000, 250_000, 500_000, 1_000_000, float("inf")
]
_SELLTO_LABELS: list[str] = [
    "F. <= 10K",
    "E. 10K - 100K",
    "D. 100K - 250K",
    "C. 250K - 500K",
    "B. 500K - 1MM",
    "A. >= 1MM",
]
_SELLTO_FEES_FALLBACK: dict[str, float] = {
    "A. >= 1MM":      0.00,
    "B. 500K - 1MM":  0.01,
    "C. 250K - 500K": 0.02,
    "D. 100K - 250K": 0.03,
    "E. 10K - 100K":  0.10,
    "F. <= 10K":      0.35,
}


# ── 2. I/O helpers ────────────────────────────────────────────────────────────

def _detect_files(uploaded_files: list) -> dict[str, object]:
    """Map each uploaded file to its logical role via filename keyword matching.

    Iterates _FILE_PATTERNS in order.  The first pattern whose keywords appear
    (substring, case-insensitive) in the filename claims that role.  Each role
    is claimed at most once; unrecognised files are silently skipped.

    Returns a dict keyed by _FilePattern.key; unmatched roles map to None.
    """
    result: dict[str, object] = {p.key: None for p in _FILE_PATTERNS}
    for f in uploaded_files:
        name_lower = f.name.lower()
        for pattern in _FILE_PATTERNS:
            if result[pattern.key] is not None:
                continue  # role already filled by an earlier file
            if any(kw in name_lower for kw in pattern.keywords):
                result[pattern.key] = f
                break
    return result


def _to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Serialize *df* to UTF-8 CSV bytes for st.download_button.

    st.download_button streams this payload via HTTP (not WebSocket), so
    there is no Streamlit message-size constraint on the output size.
    """
    return df.to_csv(index=False).encode("utf-8")


def _widget_key(value: str) -> str:
    """Return an 8-character hex hash of *value* for use as a widget key suffix.

    Embedding this hash in a widget key causes Streamlit to treat the widget as
    brand-new whenever *value* changes, resetting it to its default without any
    manual session_state manipulation.
    """
    return hashlib.md5(str(value).encode()).hexdigest()[:8]


# ── 3. DataFrame utilities ────────────────────────────────────────────────────

def _insert_col_after(df: pd.DataFrame, after_col: str, new_col: str) -> pd.DataFrame:
    """Return *df* with *new_col* repositioned immediately after *after_col*.

    *new_col* must already be present in *df* (e.g. just added by a merge).
    If *after_col* is not found, *new_col* is moved to the end.
    Column-selection produces a new DataFrame — no extra copy needed.
    """
    cols = [c for c in df.columns if c != new_col]
    idx = (cols.index(after_col) + 1) if after_col in cols else len(cols)
    cols.insert(idx, new_col)
    return df[cols]


def _drop_blank_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Remove columns that carry no data.

    A column is treated as blank when its header is empty/whitespace-only,
    every value is NaN (any dtype), or every non-NaN value is an empty string
    (object dtype only).
    """
    df = df.loc[:, df.columns.str.strip() != ""]  # blank-named headers first

    def _all_empty(s: pd.Series) -> bool:
        if s.isna().all():
            return True
        return s.dtype == object and s.fillna("").eq("").all()

    blank = [c for c in df.columns if _all_empty(df[c])]
    return df.drop(columns=blank) if blank else df


# ── 4. Processing pipeline ────────────────────────────────────────────────────

def _process_shipment_data(
    shipment_file,
    plant_tracker_file,
    mileage_tracker_file,
    demantra_file,
    pricing_tracker_file,
) -> Optional[pd.DataFrame]:
    """Load and enrich the HTST Shipment Report in a sequential pipeline.

    Enrichment steps
    ----------------
    0.  Drop noise columns (_DROP_COLS).
    1.  Left-join Plant Tracker on 'Shipping Warehouse'
        → 'Sourcing Plant' inserted after 'Shipping Warehouse'.
    2.  Left-join Mileage Tracker on ('Sourcing Plant', 'SHIPTONAME')
        → 'Mileage' inserted after 'Sourcing Plant'; missing rows → 'n/a'.
    3.  Left-join Demantra on PRODUCTDESC = 'Item Description'
        → 'Total Each Per Pallet' and 'Unit Net Weight' after 'Ordered LBS'.
    3b. Row-level pallet metrics (depends on step 3 columns):
        → 'Pallet%'       = Ordered LBS / (Total Each Per Pallet × Unit Net Weight),
                            inserted after 'Unit Net Weight'.
        → 'Pallet Status' = "Full" if Pallet% >= _PALLET_FULL_ROW_MIN, else "Mixed",
                            inserted after 'Pallet%'.
    4.  Left-join Pricing Tracker on (PRODUCTDESC, Party Site Number)
        → 'Pricing Method' inserted after 'PRODUCTDESC'; unmatched → 1.
    5.  Drop all-blank columns.

    Returns None on any read/join failure (st.error is called internally).
    All files are read from the uploaded file objects; no local paths are used.
    """
    # ── Load main report ──────────────────────────────────────────────────────
    try:
        df = pd.read_csv(shipment_file, low_memory=False)
        df.columns = df.columns.str.strip()
    except Exception as exc:
        st.error(f"Could not read HTST Shipment Report: {exc}")
        return None

    # Step 0: drop columns not needed in the output
    df = df.drop(columns=[c for c in _DROP_COLS if c in df.columns])

    # ── Step 1: Sourcing Plant ────────────────────────────────────────────────
    try:
        plant_df = pd.read_csv(plant_tracker_file)
        plant_df.columns = plant_df.columns.str.strip()
        plant_lookup = (
            plant_df[["Shipping Warehouse", "Plant"]]
            .drop_duplicates(subset=["Shipping Warehouse"])
            .rename(columns={"Plant": "Sourcing Plant"})
        )
        df = df.merge(plant_lookup, on="Shipping Warehouse", how="left")
        df = _insert_col_after(df, "Shipping Warehouse", "Sourcing Plant")
    except Exception as exc:
        st.error(f"Could not apply Plant Tracker lookup: {exc}")
        return None

    # ── Step 2: Mileage ───────────────────────────────────────────────────────
    try:
        mile_df = pd.read_csv(mileage_tracker_file)
        mile_df.columns = mile_df.columns.str.strip()
        mile_lookup = (
            mile_df[["Sourcing Plant", "SHIPTONAME", "Mileage"]]
            .drop_duplicates(subset=["Sourcing Plant", "SHIPTONAME"])
        )
        df = df.merge(mile_lookup, on=["Sourcing Plant", "SHIPTONAME"], how="left")
        df["Mileage"] = df["Mileage"].fillna("n/a")
        df = _insert_col_after(df, "Sourcing Plant", "Mileage")
    except Exception as exc:
        st.error(f"Could not apply Mileage Tracker lookup: {exc}")
        return None

    # ── Step 3: Pallet & weight data from Demantra ───────────────────────────
    try:
        dem_df = pd.read_csv(demantra_file, low_memory=False)
        dem_df.columns = dem_df.columns.str.strip()
        dem_lookup = (
            dem_df[["Item Description", "Total Each Per Pallet", "Unit Net Weight"]]
            .drop_duplicates(subset=["Item Description"])
        )
        df = df.merge(
            dem_lookup,
            left_on="PRODUCTDESC",
            right_on="Item Description",
            how="left",
        )
        df = df.drop(columns=["Item Description"], errors="ignore")
        df = _insert_col_after(df, "Ordered LBS", "Total Each Per Pallet")
        df = _insert_col_after(df, "Total Each Per Pallet", "Unit Net Weight")
    except Exception as exc:
        st.error(f"Could not apply Demantra lookup: {exc}")
        return None

    # ── Step 3b: Row-level pallet metrics ────────────────────────────────────
    # Pallet% measures the fraction of one full pallet's total weight ordered.
    # Rows with missing denominator (no Demantra match) get NaN → "Mixed".
    full_pallet_lbs = df["Total Each Per Pallet"] * df["Unit Net Weight"]
    df["Pallet%"] = (df["Ordered LBS"] / full_pallet_lbs).round(4)
    df["Pallet Status"] = df["Pallet%"].apply(
        lambda v: "Full" if pd.notna(v) and v >= _PALLET_FULL_ROW_MIN else "Mixed"
    )
    df = _insert_col_after(df, "Unit Net Weight", "Pallet%")
    df = _insert_col_after(df, "Pallet%", "Pallet Status")

    # ── Step 4: Pricing Method from Delivered vs FOB Tracker ─────────────────
    try:
        price_df = pd.read_csv(pricing_tracker_file)
        price_df.columns = price_df.columns.str.strip()
        # Cast Party Site Number to str in both tables to prevent silent type
        # mismatches that would drop rows during the join.
        price_lookup = (
            price_df[["Item Description", "Party Site Number", "Pricing Method"]]
            .drop_duplicates(subset=["Item Description", "Party Site Number"])
            .assign(**{"Party Site Number": lambda x: x["Party Site Number"].astype(str)})
        )
        df["Party Site Number"] = df["Party Site Number"].astype(str)
        df = df.merge(
            price_lookup,
            left_on=["PRODUCTDESC", "Party Site Number"],
            right_on=["Item Description", "Party Site Number"],
            how="left",
        )
        df = df.drop(columns=["Item Description"], errors="ignore")
        df["Pricing Method"] = df["Pricing Method"].fillna(1).astype(int)
        df = _insert_col_after(df, "PRODUCTDESC", "Pricing Method")
    except Exception as exc:
        st.error(f"Could not apply Pricing Tracker lookup: {exc}")
        return None

    # ── Step 5: Drop all-blank columns ───────────────────────────────────────
    return _drop_blank_columns(df)


# ── 5. Analytics ─────────────────────────────────────────────────────────────

def _bracket_sellto_volume(vol: pd.Series) -> pd.Series:
    """Classify a sell-to volume series (gallons) into the standard bracket labels.

    Uses pd.cut with right=False so every interval is [left, right):
        [-inf, 10 000)   → F. <= 10K
        [10 000, 100 000) → E. 10K - 100K
        [100 000, 250 000) → D. 100K - 250K
        [250 000, 500 000) → C. 250K - 500K
        [500 000, 1 000 000) → B. 500K - 1MM
        [1 000 000, inf)  → A. >= 1MM

    Returns object dtype (str) so downstream joins work without category quirks.
    """
    return pd.cut(
        vol,
        bins=_SELLTO_BINS,
        labels=_SELLTO_LABELS,
        right=False,
    ).astype(str)


def _compute_duration(df: pd.DataFrame, date_col: str = "Order Date") -> int:
    """Return the span in calendar days between the earliest and latest order date.

    Uses the full (unfiltered) enriched DataFrame so Duration is filter-invariant.
    Falls back to 1 on parse failure to prevent division-by-zero downstream.
    """
    if date_col not in df.columns:
        return 1
    dates = pd.to_datetime(df[date_col], format="%d-%b", errors="coerce").dropna()
    if dates.empty or dates.max() == dates.min():
        return 1
    return int((dates.max() - dates.min()).days)


def _build_customer_site_summary(
    filtered_df: pd.DataFrame,
    duration_days: int,
    pallet_fee_df: Optional[pd.DataFrame] = None,
    sell_to_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Aggregate the filtered enriched DataFrame into the Customer-Site Details table.

    Aggregation granularity: Customer × SHIPTONAME × PRODUCTDESC × PRODUCTGROUP.

    Columns produced (in order)
    ---------------------------
    Customer, SHIPTONAME, PRODUCTDESC, Product Group,
    Ordered Secondary QTY, Ordered LBS,
    Count of Unique Orders         — unique Order Numbers per Customer+SHIPTONAME,
    Duration                       — global days span (filter-invariant, passed in),
    Annualized Gallons             — Ordered Secondary QTY / Duration × 350,
    Site-level Sell-to Volume      — sum of Annualized Gallons per Customer+SHIPTONAME,
    Site-level Custom-label Volume — same sum but only non-"DG" products; 0 for DG rows,
    Drop Size                      — sum(Ordered LBS at site) / Count of Unique Orders,
    Pricing Method, Mileage,
    Full Pallet%                   — fraction of rows with row-level Pallet Status "Full",
    Pallet Status                  — "Full" if Full Pallet% > _PALLET_FULL_AGG_MIN,
    Mixed Pallet Fee               — joined from pallet_fee_df on Pallet Status,
    Sell-to Volume Bracket         — volume bracket label from _bracket_sellto_volume(),
    Sell-to Volume Fee ($/Gal)     — fee from sell_to_df if uploaded, else fallback dict.

    Design decisions
    ----------------
    * Full Pallet% consumes the row-level Pallet Status set by _process_shipment_data.
      No threshold is re-evaluated here.
    * Sell-to bracket thresholds are hardcoded (_SELLTO_BINS / _SELLTO_LABELS).
      Fee values are dynamic when sell_to_df is provided, static otherwise.
    * No file I/O occurs in this function; all lookup DataFrames are pre-loaded
      by render() and passed as arguments.
    """
    if filtered_df.empty:
        return pd.DataFrame()

    site_keys    = ["Customer", "SHIPTONAME"]
    product_keys = ["Customer", "SHIPTONAME", "PRODUCTDESC", "PRODUCTGROUP"]

    # ── Product × site aggregation ────────────────────────────────────────────
    agg = (
        filtered_df
        .groupby(product_keys, as_index=False)
        .agg(
            **{
                "Ordered Secondary QTY": ("Ordered Secondary QTY", "sum"),
                "Ordered LBS":           ("Ordered LBS",           "sum"),
                "Pricing Method":        ("Pricing Method",         "first"),
                "Mileage":               ("Mileage",                "first"),
            }
        )
    )

    # ── Count of unique Order Numbers (site level — same across all products) ──
    order_counts = (
        filtered_df
        .groupby(site_keys)["Order Number"]
        .nunique()
        .reset_index()
        .rename(columns={"Order Number": "Count of Unique Orders"})
    )
    agg = agg.merge(order_counts, on=site_keys, how="left")

    # ── Duration and Annualized Gallons ───────────────────────────────────────
    # Duration is passed from render() so it is invariant to filter state.
    agg["Duration"] = duration_days
    agg["Annualized Gallons"] = (agg["Ordered Secondary QTY"] / duration_days * 350).round(1)

    # ── Site-level Sell-to Volume (Gallons) ───────────────────────────────────
    # Sum of Annualized Gallons across ALL products at each Customer+SHIPTONAME.
    site_vol = (
        agg.groupby(site_keys, as_index=False)["Annualized Gallons"]
        .sum()
        .rename(columns={"Annualized Gallons": "Site-level Sell-to Volume (Gallons)"})
    )
    agg = agg.merge(site_vol, on=site_keys, how="left")

    # ── Site-level Custom-label Volume (Gallons) ──────────────────────────────
    # Only non-"DG" products contribute.  For DG product rows the value is 0.
    non_dg_mask = ~agg["PRODUCTDESC"].str.startswith("DG", na=True)
    if non_dg_mask.any():
        custom_vol = (
            agg[non_dg_mask]
            .groupby(site_keys, as_index=False)["Annualized Gallons"]
            .sum()
            .rename(columns={"Annualized Gallons": "_custom_vol"})
        )
        agg = agg.merge(custom_vol, on=site_keys, how="left")
        agg["_custom_vol"] = agg["_custom_vol"].fillna(0)
    else:
        agg["_custom_vol"] = 0

    # Vectorised assignment: keep the site total for non-DG rows, 0 for DG rows.
    is_dg = agg["PRODUCTDESC"].str.startswith("DG", na=True)
    agg["Site-level Custom-label Volume (Gallons)"] = agg["_custom_vol"].where(~is_dg, 0)
    agg = agg.drop(columns=["_custom_vol"])

    # ── Drop Size (lbs per order, at site level) ──────────────────────────────
    # Total Ordered LBS at the site divided by Count of Unique Orders.
    # Count of Unique Orders is already site-level, so using it directly as
    # the denominator gives the average lbs per shipment for that site.
    site_lbs = (
        agg.groupby(site_keys, as_index=False)["Ordered LBS"]
        .sum()
        .rename(columns={"Ordered LBS": "_site_lbs"})
    )
    agg = agg.merge(site_lbs, on=site_keys, how="left")
    agg["Drop Size"] = (agg["_site_lbs"] / agg["Count of Unique Orders"]).round(1)
    agg = agg.drop(columns=["_site_lbs"])

    # ── Full Pallet% and Pallet Status ────────────────────────────────────────
    # Consolidate total-row and full-row counts in a single groupby to avoid
    # redundant passes over the data.
    if "Pallet Status" in filtered_df.columns:
        pallet_grp = (
            filtered_df
            .groupby(product_keys)["Pallet Status"]
            .agg(
                _total="count",
                _full=lambda s: (s == "Full").sum(),
            )
            .reset_index()
        )
        agg = agg.merge(pallet_grp, on=product_keys, how="left")
        agg["Full Pallet%"] = (agg["_full"] / agg["_total"]).round(4)
        agg["Pallet Status"] = (
            agg["Full Pallet%"]
            .gt(_PALLET_FULL_AGG_MIN)
            .map({True: "Full", False: "Mixed"})
        )
        agg = agg.drop(columns=["_full", "_total"])

    # ── Mixed Pallet Fee (from uploaded Pallet Fee file) ─────────────────────
    # Join on Pallet Status == Pallet to retrieve the fee per gallon.
    # This column is skipped when pallet_fee_df was not uploaded.
    if pallet_fee_df is not None and "Pallet Status" in agg.columns:
        fee_lookup = (
            pallet_fee_df[["Pallet", "Mixed Pallet Fee ($/Gal)"]]
            .drop_duplicates(subset=["Pallet"])
            .rename(columns={"Mixed Pallet Fee ($/Gal)": "Mixed Pallet Fee"})
        )
        agg = agg.merge(
            fee_lookup,
            left_on="Pallet Status",
            right_on="Pallet",
            how="left",
        )
        agg = agg.drop(columns=["Pallet"], errors="ignore")

    # ── Sell-to Volume Bracket and Fee ───────────────────────────────────────
    # Bracket: assigned via hardcoded pd.cut thresholds (see _bracket_sellto_volume).
    # Fee:     joined from sell_to_df when uploaded; falls back to _SELLTO_FEES_FALLBACK.
    agg["Sell-to Volume Bracket"] = _bracket_sellto_volume(
        agg["Site-level Sell-to Volume (Gallons)"]
    )
    if sell_to_df is not None:
        fee_lookup = (
            sell_to_df[["Sell-to Volume Bracket", "Sell-to Volume Fee ($/Gal)"]]
            .drop_duplicates(subset=["Sell-to Volume Bracket"])
        )
        agg = agg.merge(fee_lookup, on="Sell-to Volume Bracket", how="left")
    else:
        agg["Sell-to Volume Fee ($/Gal)"] = (
            agg["Sell-to Volume Bracket"].map(_SELLTO_FEES_FALLBACK)
        )

    # ── Rename and enforce column order ──────────────────────────────────────
    agg = agg.rename(columns={"PRODUCTGROUP": "Product Group"})

    ordered_cols = [
        "Customer", "SHIPTONAME", "PRODUCTDESC", "Product Group",
        "Ordered Secondary QTY", "Ordered LBS",
        "Count of Unique Orders", "Duration", "Annualized Gallons",
        "Site-level Sell-to Volume (Gallons)",
        "Site-level Custom-label Volume (Gallons)",
        "Drop Size",
        "Pricing Method", "Mileage",
        "Full Pallet%", "Pallet Status", "Mixed Pallet Fee",
        "Sell-to Volume Bracket", "Sell-to Volume Fee ($/Gal)",
    ]
    present = [c for c in ordered_cols if c in agg.columns]
    return (
        agg[present]
        .sort_values(["Customer", "SHIPTONAME", "PRODUCTDESC"])
        .reset_index(drop=True)
    )


# ── 6. UI rendering ───────────────────────────────────────────────────────────

def _render_upload_section() -> dict[str, object]:
    """Render a single multi-file uploader and return the auto-detected file map.

    The user drops all CSVs from the HTST Shipment Monitor folder in one action;
    each file is identified automatically by filename keywords.
    """
    st.markdown("### 📤 Upload Data Files")
    st.caption(
        "Select or drag-and-drop all CSVs from your HTST Shipment Monitor folder. "
        "Files are identified automatically by their filename."
    )
    uploaded_files = st.file_uploader(
        "Select all HTST Shipment Monitor CSV files",
        type=["csv"],
        accept_multiple_files=True,
        key="htst_all_files",
    )
    return _detect_files(uploaded_files or [])


def _render_filters(df: pd.DataFrame) -> pd.DataFrame:
    """Render a Customer selectbox and a cascading SHIPTONAME multiselect.

    Returns the subset of *df* that matches the current widget selections.
    Customer is single-select so the downstream views stay focused.
    SHIPTONAME options narrow automatically when the customer changes; the
    widget key embeds a hash of the selected customer so Streamlit resets it
    (back to all Ship-To options) on every customer switch.
    """
    st.markdown("### 🔍 Filter")
    f1, f2 = st.columns(2)

    with f1:
        all_customers = sorted(df["Customer"].dropna().astype(str).unique().tolist())
        sel_customer = st.selectbox(
            "Customer",
            options=all_customers,
            key="htst_filter_customer",
            help="Select a customer. Ship-To options narrow automatically.",
        )

    df_by_customer = (
        df[df["Customer"].astype(str) == sel_customer]
        if sel_customer else df.iloc[0:0]
    )

    with f2:
        shiptoname_opts = sorted(
            df_by_customer["SHIPTONAME"].dropna().astype(str).unique().tolist()
        )
        sel_shiptonames = st.multiselect(
            "Ship-To Name",
            options=shiptoname_opts,
            default=shiptoname_opts,
            key=f"htst_filter_shiptoname_{_widget_key(sel_customer or '')}",
            help="Options narrow automatically based on the Customer selection above.",
        )

    filtered = (
        df_by_customer[df_by_customer["SHIPTONAME"].astype(str).isin(sel_shiptonames)]
        if sel_shiptonames else df_by_customer.iloc[0:0]
    )
    st.caption(f"**{len(filtered):,}** rows match the current filter criteria.")
    return filtered


def _render_customer_site_details(
    filtered_df: pd.DataFrame,
    duration_days: int,
    pallet_fee_df: Optional[pd.DataFrame],
    sell_to_df: Optional[pd.DataFrame],
) -> None:
    """Render the Customer-Site Details aggregated summary table.

    *pallet_fee_df* and *sell_to_df* are pre-loaded by render() from the
    respective uploaded files (None when not uploaded).  Passing them as
    DataFrames keeps this function free of file I/O.
    """
    st.markdown("### 📊 Customer-Site Details")

    summary = _build_customer_site_summary(
        filtered_df, duration_days, pallet_fee_df, sell_to_df
    )

    if summary.empty:
        st.info("No data matches the current filters.")
        return

    st.caption(
        f"Aggregated by Customer × Ship-To × Product. "
        f"Duration ({duration_days:,} days) is computed from the full dataset "
        f"and is filter-invariant."
    )
    st.dataframe(summary, use_container_width=True, hide_index=True)

    today = datetime.now().strftime("%Y%m%d")
    st.download_button(
        label="⬇️ Download Customer-Site Summary (CSV)",
        data=_to_csv_bytes(summary),
        file_name=f"HTST_CustomerSite_Summary_{today}.csv",
        mime="text/csv",
        key="htst_download_summary",
    )


def _render_output_section(filtered_df: pd.DataFrame) -> None:
    """Render the Enriched Shipment Report section.

    Shows summary metrics, a download button (HTTP stream), and a row-capped
    browser preview.  Only _PREVIEW_ROWS rows go through st.dataframe() to
    stay within Streamlit's WebSocket message-size limit.
    """
    st.markdown("### 📋 Enriched Shipment Report")

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Filtered Rows", f"{len(filtered_df):,}")
    with m2:
        st.metric("Total Columns", f"{len(filtered_df.columns)}")
    with m3:
        n_no_mileage = int((filtered_df["Mileage"] == "n/a").sum()) if "Mileage" in filtered_df.columns else 0
        st.metric("Rows with n/a Mileage", f"{n_no_mileage:,}")
    with m4:
        n_no_plant = int(filtered_df["Sourcing Plant"].isna().sum()) if "Sourcing Plant" in filtered_df.columns else 0
        st.metric("Rows with Unmatched Plant", f"{n_no_plant:,}")

    st.markdown("")

    today = datetime.now().strftime("%Y%m%d")
    st.download_button(
        label="⬇️ Download Full Enriched Report (CSV)",
        data=_to_csv_bytes(filtered_df),
        file_name=f"HTST_Shipment_Enriched_{today}.csv",
        mime="text/csv",
        key="htst_download_output",
    )
    st.caption(
        f"Preview: first {min(_PREVIEW_ROWS, len(filtered_df)):,} of "
        f"{len(filtered_df):,} rows. Use the download button above for the full dataset."
    )
    st.dataframe(filtered_df.head(_PREVIEW_ROWS), use_container_width=True, hide_index=True)


# ── 7. Entry point ────────────────────────────────────────────────────────────

def render() -> None:
    """Render the HTST Shipment Monitor page.

    Flow: upload → validate → process → load optional lookups →
          filter → Customer-Site Details → Enriched Report.

    Business logic is fully delegated to section-specific functions.
    Duration is computed before filtering so it is filter-invariant.
    Optional lookup DataFrames (pallet_fee_df) are loaded here from uploaded
    file objects and passed as arguments — no function downstream opens a file.
    """
    apply_custom_css()

    st.markdown(
        '<h1 class="main-header">HTST Shipment Monitor</h1>',
        unsafe_allow_html=True,
    )

    # ── Welcome (TBD) ─────────────────────────────────────────────────────────
    st.markdown("### Welcome")
    st.info("TBD — overview and guidance for this page will be added here.")
    st.markdown("---")

    # ── Upload & auto-detect ──────────────────────────────────────────────────
    detected = _render_upload_section()
    st.markdown("---")

    # Gate on required files — surface exactly which are still missing.
    required_missing = [
        p.label for p in _FILE_PATTERNS
        if p.required and detected.get(p.key) is None
    ]
    if required_missing:
        st.info("👆 Still waiting for: **" + "**, **".join(required_missing) + "**")
        return

    # ── Process main enrichment pipeline ──────────────────────────────────────
    with st.spinner("Processing — enriching shipment data…"):
        enriched_df = _process_shipment_data(
            shipment_file=detected["shipment"],
            plant_tracker_file=detected["plant_tracker"],
            mileage_tracker_file=detected["mileage_tracker"],
            demantra_file=detected["demantra"],
            pricing_tracker_file=detected["pricing_tracker"],
        )

    if enriched_df is None:
        return  # st.error already raised inside _process_shipment_data

    # Duration computed before any filtering so it is filter-invariant.
    duration_days = _compute_duration(enriched_df)

    # ── Load optional lookup tables from uploaded files ───────────────────────
    # Both tables are loaded here so _build_customer_site_summary stays a pure
    # analytics function with no file I/O.  None is passed when not uploaded.

    pallet_fee_df: Optional[pd.DataFrame] = None
    if detected.get("pallet_fee") is not None:
        try:
            pallet_fee_df = pd.read_csv(detected["pallet_fee"])
            pallet_fee_df.columns = pallet_fee_df.columns.str.strip()
        except Exception as exc:
            st.warning(f"Pallet Fee file could not be read — Mixed Pallet Fee column skipped: {exc}")

    sell_to_df: Optional[pd.DataFrame] = None
    if detected.get("sell_to") is not None:
        try:
            sell_to_df = pd.read_csv(detected["sell_to"])
            sell_to_df.columns = sell_to_df.columns.str.strip()
        except Exception as exc:
            st.warning(f"Sell-To Volume Bracket file could not be read — fallback fees will be used: {exc}")

    st.success(
        f"✅ Processing complete — **{len(enriched_df):,} rows**, "
        f"**{len(enriched_df.columns)} columns**"
    )
    st.markdown("---")

    # ── Filters (drive both downstream sections) ──────────────────────────────
    filtered_df = _render_filters(enriched_df)
    st.markdown("---")

    # ── Customer-Site Details ─────────────────────────────────────────────────
    _render_customer_site_details(filtered_df, duration_days, pallet_fee_df, sell_to_df)
    st.markdown("---")

    # ── Enriched Shipment Report (filtered, downloadable) ─────────────────────
    _render_output_section(filtered_df)
