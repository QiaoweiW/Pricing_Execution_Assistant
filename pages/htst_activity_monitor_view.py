"""
HTST Activity Monitor page view.

Sections
--------
1. Types & constants   (_FilePattern, _FILE_PATTERNS, _SHIPMENT_PATTERN,
                        _PRODUCT_GROUP_FILTER, _DROP_COLS, _PREVIEW_ROWS,
                        _PALLET_FULL_ROW_MIN, _PALLET_FULL_AGG_MIN,
                        bracket bins/labels/fees, _DELIVERY_FEES)
2. I/O helpers         (_detect_files, _to_csv_bytes, _load_optional_lookup,
                        _widget_key, _filter_to_htst,
                        _htst_filtered_csv_bytes)
3. DataFrame utilities (_insert_col_after, _drop_blank_columns)
4. Processing pipeline (_process_shipment_data)
5. Analytics           (_bracket_sellto_volume, _bracket_custom_label_volume,
                        _bracket_mileage, _bracket_drop_size,
                        _build_customer_site_summary)
6. UI rendering        (_render_shipment_source, _render_upload_section,
                        _render_filters, _render_customer_site_details,
                        _render_output_section)
7. Entry point         (render)

Data flow
---------
                  ┌─ PRIMARY (Pricing Lakehouse pull) ───────────────────┐
                  │   _render_shipment_source pulls Shipments from the   │
                  │   Fabric Lakehouse Delta table; user uploads only    │
                  │   the lookup CSVs. A "Download HTST-Only Shipment    │
                  │   Data (CSV)" button is rendered here so users can   │
                  │   capture a local snapshot of the HTST-filtered raw  │
                  │   dataflow data, pre-merge.                          │
                  │                                                       │
                  ├─ FALLBACK (auto, on lakehouse failure) ──────────────┤
                  │   When the lakehouse pull fails — typically because  │
                  │   interactive Azure sign-in is unavailable on a      │
                  │   Streamlit Cloud (web) deployment — the page        │
                  │   automatically reveals the full upload section and  │
                  │   accepts the HTST Shipment Report alongside all     │
                  │   lookup CSVs in a single multi-file uploader.       │
                  └──────────────────────────────────────────────────────┘
                                     │
                                     ▼
          _process_shipment_data ──> enriched_df  (HTST-only, joined)
                                     │  cached in st.session_state
                                     ▼
              _render_filters    ──> filtered_df + duration_days
                                     │
                                     ├──> _render_customer_site_details
                                     │       (uses optional lookup CSVs for
                                     │        Pallet/SellTo/CustomLabel fees)
                                     │
                                     └──> _render_output_section

Design notes
------------
* Single source-mode policy: the Pricing Lakehouse pull is always tried
  FIRST (no checkbox toggle).  When it succeeds the page shows a
  smaller "lookups only" upload panel.  When the pull fails — most
  commonly because a headless Streamlit Cloud server cannot complete
  the interactive Azure browser sign-in — the page automatically falls
  back to the full multi-file uploader (HTST Shipment Report + lookup
  CSVs).  No user action is required to switch between modes.
* The "shipment" pattern is defined separately as _SHIPMENT_PATTERN so it
  is appended to the active pattern list only in default mode.  Pattern
  matching in _detect_files is first-match-wins, with the lookup
  patterns ordered before _SHIPMENT_PATTERN so generic substrings like
  "shipment" in lookup filenames (e.g. "Shipment_Plant_Tracker.csv") are
  claimed by their specific lookup pattern before the broader shipment
  one is consulted.
* Filter to PRODUCTGROUP == _PRODUCT_GROUP_FILTER ("HTST") happens at the
  TOP of _process_shipment_data, before any merge or aggregation, so the
  full Shipments table is reduced to the HTST subset before consuming
  memory or CPU on row-by-row work.  This is also semantically required —
  site-level aggregates like "Site-level Sell-to Volume" must not be
  contaminated by non-HTST product rows.  The same filter logic is
  re-used by _filter_to_htst() to power the dataflow download button.
* The full enriched DataFrame is NEVER pushed to the browser as a table.
  Only _PREVIEW_ROWS rows go through st.dataframe().  Download buttons
  stream via HTTP (not the WebSocket), so they work for any dataset size
  on Streamlit Cloud.
* Duration equals (sel_end − sel_start).days from the Order Date range
  slicer, clamped to ≥ 1 to prevent division-by-zero.  It drives the
  Annualized Gallons denominator in Customer-Site Details and updates
  automatically when the slicer changes.  The enriched DataFrame is cached
  in st.session_state (keyed by a composite signature of the shipment
  snapshot identity AND the lookup files' name+size) so the expensive
  enrichment pipeline does not re-run on every widget interaction.
* Pallet classification uses two thresholds defined in section 1:
    _PALLET_FULL_ROW_MIN : per-row — Pallet% >= threshold → "Full".
    _PALLET_FULL_AGG_MIN : summary — Full Pallet% > threshold → "Full".
* Volume and delivery bracket thresholds are all hardcoded in section 1.
  Fee values for the volume brackets are dynamic (read from uploaded CSVs)
  with hardcoded fallbacks.  Delivery charges (_DELIVERY_FEES) are fully
  hardcoded as a 2-D dict keyed by (Mileage Fee Tier, Drop Fee Tier) —
  the 12×5 table is small and stable; dynamic parsing of the
  dollar-prefixed strings would add fragility.
* All optional-lookup DataFrames (pallet_fee_df, sell_to_df,
  custom_label_df) are loaded in render() via _load_optional_lookup and
  passed as arguments.  No function in section 5 reads from disk.
  _process_shipment_data (section 4) is the sole location that performs
  file I/O on the uploaded lookup file objects.
* Exactly two CSV outputs are produced: the filtered enriched report and
  the Customer-Site Summary.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import NamedTuple, Optional

import pandas as pd
import streamlit as st

from data_sources.htst_shipment import (
    HTSTShipmentSourceError,
    SnapshotMeta,
    fetch_htst_shipment_df,
)
from utils.ui_helpers import apply_custom_css


# ── 1. Types & constants ──────────────────────────────────────────────────────

class _FilePattern(NamedTuple):
    key:      str        # dict key used throughout this module
    required: bool       # must be present before processing can start
    label:    str        # human-readable display name
    keywords: list[str]  # any one of these substrings (case-insensitive) in
                         # the filename causes this pattern to match


# Lookup-file patterns.  Order matters: the first matching pattern claims each
# uploaded file.  The HTST Shipment Report itself is handled by the separate
# _SHIPMENT_PATTERN below — it is appended to this list only in the default
# (manual-upload) mode, so dataflow-mode users do not have to re-upload it.
_FILE_PATTERNS: list[_FilePattern] = [
    _FilePattern("plant_tracker",   True,  "Shipment Plant Tracker",          ["plant_tracker", "plant tracker", "shipment_plant"]),
    _FilePattern("mileage_tracker", True,  "Ship Route Mileage Tracker",      ["mileage_tracker", "mileage tracker", "route_mileage", "route mileage"]),
    _FilePattern("demantra",        True,  "Demantra Item Master",            ["demantra"]),
    _FilePattern("pricing_tracker", True,  "Delivered vs FOB Pricing Tracker",["delivered vs fob", "delivered_vs_fob", "fob_tracker", "fob tracker"]),
    _FilePattern("custom_label",    False, "Custom Label Volume Bracket Fee", ["custom label", "custom_label"]),
    _FilePattern("pallet_fee",      False, "Pallet Fee",                      ["pallet_fee", "pallet fee"]),
    _FilePattern("sell_to",         False, "Sell-To Volume Bracket Fee",      ["sell-to", "sell_to"]),
]

# Shipment-Report pattern, kept separate from _FILE_PATTERNS so it can be
# included or excluded depending on the source mode (manual upload vs Fabric
# dataflow).  Keywords are deliberately specific ("htst_shipment",
# "shipment_report", …) to avoid claiming lookup files such as
# "Shipment_Plant_Tracker.csv" — those are claimed by their dedicated lookup
# pattern, which sits earlier in the matching order.
_SHIPMENT_PATTERN: _FilePattern = _FilePattern(
    key="shipment",
    required=True,
    label="HTST Shipment Report",
    keywords=["htst_shipment", "htst shipment", "shipment_report", "shipment report"],
)

# Product-group filter applied in _process_shipment_data BEFORE any merge or
# aggregation runs.  The Fabric Shipments table contains every product group
# Darigold ships (HTST, Cheese, Powder, etc.); only HTST rows belong on this
# page.  Filtering early shrinks the working set ~7× (matters for memory and
# for every downstream operation), and is more correct than filtering at the
# UI layer because incorrect rows would otherwise contaminate site-level
# aggregates (e.g. Site-level Sell-to Volume sums).  Comparison is uppercase
# + stripped to absorb minor formatting drift in the source data.
_PRODUCT_GROUP_FILTER: str = "HTST"

# Columns to remove from the enriched output — not needed downstream.
_DROP_COLS = ["Reason Code", "Include for Fill Rate Calculations", "Past Due by Request Date"]

# Maximum rows shown in the browser preview to avoid WebSocket message-size errors.
_PREVIEW_ROWS = 500

# SharePoint library where HTST Shipment Monitor CSVs are maintained (linked from
# the upload caption — no local path assumptions in the UI).
_HTST_SHIPMENT_MONITOR_SHAREPOINT_URL: str = (
    "https://darigold1com.sharepoint.com/sites/BrandedPricing/Shared%20Documents"
    "/Forms/AllItems.aspx?id=%2Fsites%2FBrandedPricing%2FShared%20Documents"
    "%2FGeneral%2F02%20Resources%2FStreamlit%20Folders%20%28DO%20NOT%20DELETE%29"
    "%2FHTST%20Activity%20Model%20Monitor&viewid=9103ebc3%2Df944%2D4451%2Dbe05%2Dd0cb7479e27e"
)

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

    # Custom-label volume bracket classification — same design rationale as sell-to.
# "Not Applicable" is assigned when the site-level custom-label volume is zero,
# meaning every product at that site is DG-branded and nothing contributes to
# custom-label volume.  pd.cut handles all non-zero volumes.
_CUSTOM_LABEL_BINS: list[float] = [
    float("-inf"), 250_000, 500_000, 1_000_000, 5_000_000, float("inf")
]
_CUSTOM_LABEL_LABELS: list[str] = [
    "E. < 250K",
    "D. 250K - 500K",
    "C. 500K - 1MM",
    "B. 1MM - 5MM",
    "A. >= 5MM",
]
_CUSTOM_LABEL_FEES_FALLBACK: dict[str, float] = {
    "A. >= 5MM":      0.00,
    "B. 1MM - 5MM":   0.01,
    "C. 500K - 1MM":  0.02,
    "D. 250K - 500K": 0.03,
    "E. < 250K":      0.05,
    "Not Applicable": 0.00,
}

# Delivery charge — 2-D lookup keyed by (Mileage Fee Tier, Drop Fee Tier).
#
# Both tier dimensions are hardcoded from Delivery_Miles Tier_Drop Size Tier_Fee.csv
# using the same rationale as the sell-to and custom-label brackets: the tier
# labels are free-text strings with K/MM multipliers that would be fragile to
# parse programmatically, and the table is small and rarely changes.
#
# Missing (mileage_tier, drop_tier) combos — e.g. when Mileage is "n/a" — default
# to 0.0 via dict.get().  Pricing Method == 0 (FOB) forces Delivery Charge to 0
# regardless of tier, applied as a post-lookup override.
_MILEAGE_BINS: list[float] = [
    float("-inf"), 50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1_000, float("inf")
]
_MILEAGE_LABELS: list[str] = [
    "L. <50 mi",
    "K. 100 - 50 mi",
    "J. 200 - 100 mi",
    "I. 300 - 200 mi",
    "H. 400 - 300 mi",
    "G. 500 - 400 mi",
    "F. 600 - 500 mi",
    "E. 700 - 600 mi",
    "D. 800 - 700 mi",
    "C. 900 - 800 mi",
    "B. 1000 - 900 mi",
    "A. >= 1000 mi",
]

_DROP_BINS: list[float] = [
    float("-inf"), 4_000, 10_000, 20_000, 30_000, float("inf")
]
_DROP_LABELS: list[str] = [
    "E. < 4k lbs",
    "D. 4k - 10k lbs",
    "C. 10k - 20k lbs",
    "B. 20k - 30k lbs",
    "A. >= 30k lbs",
]

_DELIVERY_FEES: dict[tuple[str, str], float] = {
    ("A. >= 1000 mi",    "A. >= 30k lbs"):    0.82,
    ("A. >= 1000 mi",    "B. 20k - 30k lbs"): 1.32,
    ("A. >= 1000 mi",    "C. 10k - 20k lbs"): 2.13,
    ("A. >= 1000 mi",    "D. 4k - 10k lbs"):  4.90,
    ("A. >= 1000 mi",    "E. < 4k lbs"):      10.67,
    ("B. 1000 - 900 mi", "A. >= 30k lbs"):    0.77,
    ("B. 1000 - 900 mi", "B. 20k - 30k lbs"): 1.23,
    ("B. 1000 - 900 mi", "C. 10k - 20k lbs"): 1.98,
    ("B. 1000 - 900 mi", "D. 4k - 10k lbs"):  4.57,
    ("B. 1000 - 900 mi", "E. < 4k lbs"):      9.94,
    ("C. 900 - 800 mi",  "A. >= 30k lbs"):    0.69,
    ("C. 900 - 800 mi",  "B. 20k - 30k lbs"): 1.11,
    ("C. 900 - 800 mi",  "C. 10k - 20k lbs"): 1.79,
    ("C. 900 - 800 mi",  "D. 4k - 10k lbs"):  4.12,
    ("C. 900 - 800 mi",  "E. < 4k lbs"):      8.97,
    ("D. 800 - 700 mi",  "A. >= 30k lbs"):    0.65,
    ("D. 800 - 700 mi",  "B. 20k - 30k lbs"): 1.04,
    ("D. 800 - 700 mi",  "C. 10k - 20k lbs"): 1.68,
    ("D. 800 - 700 mi",  "D. 4k - 10k lbs"):  3.86,
    ("D. 800 - 700 mi",  "E. < 4k lbs"):      8.41,
    ("E. 700 - 600 mi",  "A. >= 30k lbs"):    0.64,
    ("E. 700 - 600 mi",  "B. 20k - 30k lbs"): 1.03,
    ("E. 700 - 600 mi",  "C. 10k - 20k lbs"): 1.66,
    ("E. 700 - 600 mi",  "D. 4k - 10k lbs"):  3.83,
    ("E. 700 - 600 mi",  "E. < 4k lbs"):      8.33,
    ("F. 600 - 500 mi",  "A. >= 30k lbs"):    0.55,
    ("F. 600 - 500 mi",  "B. 20k - 30k lbs"): 0.88,
    ("F. 600 - 500 mi",  "C. 10k - 20k lbs"): 1.42,
    ("F. 600 - 500 mi",  "D. 4k - 10k lbs"):  3.28,
    ("F. 600 - 500 mi",  "E. < 4k lbs"):      7.13,
    ("G. 500 - 400 mi",  "A. >= 30k lbs"):    0.45,
    ("G. 500 - 400 mi",  "B. 20k - 30k lbs"): 0.72,
    ("G. 500 - 400 mi",  "C. 10k - 20k lbs"): 1.17,
    ("G. 500 - 400 mi",  "D. 4k - 10k lbs"):  2.68,
    ("G. 500 - 400 mi",  "E. < 4k lbs"):      5.84,
    ("H. 400 - 300 mi",  "A. >= 30k lbs"):    0.39,
    ("H. 400 - 300 mi",  "B. 20k - 30k lbs"): 0.63,
    ("H. 400 - 300 mi",  "C. 10k - 20k lbs"): 1.02,
    ("H. 400 - 300 mi",  "D. 4k - 10k lbs"):  2.35,
    ("H. 400 - 300 mi",  "E. < 4k lbs"):      5.11,
    ("I. 300 - 200 mi",  "A. >= 30k lbs"):    0.28,
    ("I. 300 - 200 mi",  "B. 20k - 30k lbs"): 0.46,
    ("I. 300 - 200 mi",  "C. 10k - 20k lbs"): 0.74,
    ("I. 300 - 200 mi",  "D. 4k - 10k lbs"):  1.69,
    ("I. 300 - 200 mi",  "E. < 4k lbs"):      3.68,
    ("J. 200 - 100 mi",  "A. >= 30k lbs"):    0.20,
    ("J. 200 - 100 mi",  "B. 20k - 30k lbs"): 0.32,
    ("J. 200 - 100 mi",  "C. 10k - 20k lbs"): 0.52,
    ("J. 200 - 100 mi",  "D. 4k - 10k lbs"):  1.20,
    ("J. 200 - 100 mi",  "E. < 4k lbs"):      2.61,
    ("K. 100 - 50 mi",   "A. >= 30k lbs"):    0.11,
    ("K. 100 - 50 mi",   "B. 20k - 30k lbs"): 0.18,
    ("K. 100 - 50 mi",   "C. 10k - 20k lbs"): 0.29,
    ("K. 100 - 50 mi",   "D. 4k - 10k lbs"):  0.66,
    ("K. 100 - 50 mi",   "E. < 4k lbs"):      1.44,
    ("L. <50 mi",        "A. >= 30k lbs"):    0.04,
    ("L. <50 mi",        "B. 20k - 30k lbs"): 0.06,
    ("L. <50 mi",        "C. 10k - 20k lbs"): 0.10,
    ("L. <50 mi",        "D. 4k - 10k lbs"):  0.22,
    ("L. <50 mi",        "E. < 4k lbs"):      0.48,
}


# ── 2. I/O helpers ────────────────────────────────────────────────────────────

def _detect_files(
    uploaded_files: list,
    patterns: Optional[list[_FilePattern]] = None,
) -> dict[str, object]:
    """Map each uploaded file to its logical role via filename keyword matching.

    Iterates *patterns* (defaults to _FILE_PATTERNS) in order.  The first
    pattern whose keywords appear (substring, case-insensitive) in the
    filename claims that role.  Each role is claimed at most once;
    unrecognised files are silently skipped.

    Returns a dict keyed by _FilePattern.key; unmatched roles map to None.
    """
    pats = patterns if patterns is not None else _FILE_PATTERNS
    result: dict[str, object] = {p.key: None for p in pats}
    for f in uploaded_files:
        name_lower = f.name.lower()
        for pattern in pats:
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


def _load_optional_lookup(file_obj, label: str) -> Optional[pd.DataFrame]:
    """Read an optional lookup CSV, normalising headers; return None on failure.

    Centralises the read/strip/warn pattern that the three optional fee files
    (Pallet Fee, Sell-To Volume, Custom Label Volume) all share.  Returning
    None — instead of raising — lets the page degrade to hardcoded fallback
    fees while still warning the user about the specific file that failed.
    """
    if file_obj is None:
        return None
    try:
        df = pd.read_csv(file_obj)
        df.columns = df.columns.str.strip()
        return df
    except Exception as exc:  # noqa: BLE001
        st.warning(f"{label} could not be read — fallback values will be used: {exc}")
        return None


def _widget_key(value: str) -> str:
    """Return an 8-character hex hash of *value* for use as a widget key suffix.

    Embedding this hash in a widget key causes Streamlit to treat the widget as
    brand-new whenever *value* changes, resetting it to its default without any
    manual session_state manipulation.
    """
    return hashlib.md5(str(value).encode()).hexdigest()[:8]


def _filter_to_htst(df: pd.DataFrame) -> pd.DataFrame:
    """Return the HTST product-group subset with normalised column names.

    Columns are stripped of leading/trailing whitespace; rows are kept where
    PRODUCTGROUP equals _PRODUCT_GROUP_FILTER under case- and whitespace-
    insensitive comparison.  This is the single source of truth for the HTST
    filter and is reused by both _process_shipment_data (pre-merge) and
    _htst_filtered_csv_bytes (the dataflow download button).

    Raises KeyError("PRODUCTGROUP") if the column is missing — callers decide
    whether to surface a Streamlit error or skip the optional download.
    """
    df = df.rename(columns=str.strip)
    if "PRODUCTGROUP" not in df.columns:
        raise KeyError("PRODUCTGROUP")
    mask = (
        df["PRODUCTGROUP"].astype(str).str.strip().str.upper().eq(_PRODUCT_GROUP_FILTER)
    )
    return df[mask]


@st.cache_data(show_spinner=False)
def _htst_filtered_csv_bytes(_df: pd.DataFrame, snapshot_sig: str) -> bytes:
    """Serialize the HTST-filtered subset of *_df* to UTF-8 CSV bytes (cached).

    The leading underscore on *_df* tells Streamlit not to hash the DataFrame
    (which would be wasteful on a 400 K-row table); cache identity is keyed on
    *snapshot_sig*, which already encodes the dataflow Delta version + last-
    modified timestamp.  As a result the filter + CSV-encode pass runs at
    most once per dataflow snapshot, even though Streamlit reruns the page on
    every widget interaction.
    """
    return _filter_to_htst(_df).to_csv(index=False).encode("utf-8")


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
    shipment_df: pd.DataFrame,
    plant_tracker_file,
    mileage_tracker_file,
    demantra_file,
    pricing_tracker_file,
) -> Optional[pd.DataFrame]:
    """Enrich the HTST Shipment Report in a sequential pipeline.

    The shipment DataFrame is supplied directly by the caller — sourced from
    either the Fabric dataflow connector (preferred) or the manual-upload
    fallback (CSV → DataFrame conversion happens in _render_shipment_source).
    The four lookup tables remain CSVs uploaded by the user.

    Enrichment steps
    ----------------
    0a. Normalise column names (strip whitespace).
    0b. Filter to PRODUCTGROUP == _PRODUCT_GROUP_FILTER ("HTST").  Done first
        so all downstream merges and aggregations operate on the ~1/7 row
        subset rather than the full Shipments table.
    0c. Defensive copy + drop noise columns (_DROP_COLS).
    0d. Pre-parse 'Order Date' once into datetime64.  _render_filters then
        skips its own O(n) parse on every widget interaction.
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
    """
    # ── Steps 0a + 0b: Normalise headers and filter to HTST product group ───
    # _filter_to_htst centralises both the column-strip and the case-insensitive
    # PRODUCTGROUP filter so the logic stays in lockstep with the dataflow
    # download button (_htst_filtered_csv_bytes).  KeyError signals a missing
    # PRODUCTGROUP column — surfaced as a Streamlit error and aborted.
    try:
        df = _filter_to_htst(shipment_df)
    except KeyError:
        st.error(
            "Shipment data is missing the 'PRODUCTGROUP' column — cannot "
            "filter to HTST.  Verify the Fabric dataflow output schema."
        )
        return None
    if df.empty:
        st.error(
            f"No rows found with PRODUCTGROUP == '{_PRODUCT_GROUP_FILTER}' in "
            "the shipment data.  Verify the dataflow refresh produced HTST rows."
        )
        return None

    # ── Step 0c: Defensive copy + drop noise columns ─────────────────────────
    # Copy AFTER the HTST filter — same correctness, much smaller allocation.
    # Without the copy we'd mutate the connector's cached frame.
    df = df.copy()
    df = df.drop(columns=[c for c in _DROP_COLS if c in df.columns])

    # ── Step 0d: Pre-parse Order Date once ───────────────────────────────────
    # The page's _render_filters needs datetime semantics for the slider AND
    # row mask.  Parsing once here (run once per shipment-snapshot change)
    # eliminates a per-rerun O(n) parse pass on every filter-widget interaction.
    if "Order Date" in df.columns:
        df["Order Date"] = pd.to_datetime(df["Order Date"], errors="coerce")

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
    # Vectorised boolean avoids row-by-row Python overhead on 400K+ rows.
    full_pallet_lbs = df["Total Each Per Pallet"] * df["Unit Net Weight"]
    df["Pallet%"] = (df["Ordered LBS"] / full_pallet_lbs).round(4)
    is_full = df["Pallet%"].notna() & (df["Pallet%"] >= _PALLET_FULL_ROW_MIN)
    df["Pallet Status"] = is_full.map({True: "Full", False: "Mixed"})
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


def _bracket_custom_label_volume(vol: pd.Series) -> pd.Series:
    """Classify a custom-label volume series (gallons) into the standard bracket labels.

    Uses pd.cut with right=False so every non-zero interval is [left, right):
        (0, 250 000)       → E. < 250K
        [250 000, 500 000) → D. 250K - 500K
        [500 000, 1 000 000) → C. 500K - 1MM
        [1 000 000, 5 000 000) → B. 1MM - 5MM
        [5 000 000, inf)   → A. >= 5MM
        0 (sites where every product is DG-branded) → Not Applicable

    The "Not Applicable" assignment is a post-cut override applied via
    Series.where() — the same vectorised pattern used in _bracket_sellto_volume.
    Returns object dtype (str) so downstream joins work without category quirks.
    """
    result = pd.cut(
        vol,
        bins=_CUSTOM_LABEL_BINS,
        labels=_CUSTOM_LABEL_LABELS,
        right=False,
    ).astype(str)
    # Rows with zero custom-label volume are not in any numeric bracket.
    return result.where(vol > 0, "Not Applicable")


def _bracket_mileage(mileage: pd.Series) -> pd.Series:
    """Classify a mileage series into the standard delivery tier labels.

    Mileage is stored as a mixed-type column: numeric values for matched routes
    and the string "n/a" for unmatched ones.  pd.to_numeric coerces "n/a" to NaN
    so pd.cut can operate on the numeric subset; NaN rows are then overridden to
    "Not Applicable" via Series.where(), matching the pattern in the other bracket
    functions.

    Returns object dtype (str) so the downstream tuple-key fee lookup works cleanly.
    """
    numeric = pd.to_numeric(mileage, errors="coerce")
    result = pd.cut(
        numeric,
        bins=_MILEAGE_BINS,
        labels=_MILEAGE_LABELS,
        right=False,
    ).astype(str)
    return result.where(numeric.notna(), "Not Applicable")


def _bracket_drop_size(drop_size: pd.Series) -> pd.Series:
    """Classify a drop size series (lbs per drop) into the standard delivery tier labels.

    Drop Size is always numeric (site LBS / count of orders), so no "n/a"
    handling is needed.  Returns object dtype (str) for consistent downstream joins.
    """
    return pd.cut(
        drop_size,
        bins=_DROP_BINS,
        labels=_DROP_LABELS,
        right=False,
    ).astype(str)



def _build_customer_site_summary(
    filtered_df: pd.DataFrame,
    duration_days: int,
    pallet_fee_df: Optional[pd.DataFrame] = None,
    sell_to_df: Optional[pd.DataFrame] = None,
    custom_label_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Aggregate the filtered enriched DataFrame into the Customer-Site Details table.

    Aggregation granularity: Customer × SHIPTONAME × PRODUCTDESC × PRODUCTGROUP.

    Columns produced (in order)
    ---------------------------
    Customer, SHIPTONAME, PRODUCTDESC, Product Group,
    Ordered Secondary QTY, Ordered LBS,
    Count of Unique Orders           — unique Order Numbers per Customer+SHIPTONAME,
    Duration                         — calendar-day span of the selected Order Date range
                                       (passed in from _render_filters),
    Annualized Gallons               — Ordered Secondary QTY / Duration × 350,
    Site-level Sell-to Volume        — sum of Annualized Gallons per Customer+SHIPTONAME,
    Site-level Custom-label Volume   — sum of non-"DG" Annualized Gallons at site;
                                       uniform across all product rows at that site
                                       (0 only when the entire site is DG-branded),
    Drop Size (lbs per drop)         — sum(Ordered LBS at site) / Count of Unique Orders,
    Pricing Method, Mileage,
    Mileage Fee Tier (Mi)            — delivery distance tier from _bracket_mileage(),
    Drop Fee Tier (lbs/Drop Size)    — delivery drop-size tier from _bracket_drop_size(),
    Delivery Charge ($/Gal)          — fee from _DELIVERY_FEES[(mileage_tier, drop_tier)];
                                       forced to 0 when Pricing Method == 0 (FOB),
    Full Pallet%                     — fraction of rows with row-level Pallet Status "Full",
    Pallet Status                    — "Full" if Full Pallet% > _PALLET_FULL_AGG_MIN,
    Mixed Pallet Fee                 — joined from pallet_fee_df on Pallet Status,
    Sell-to Volume Bracket           — bracket from _bracket_sellto_volume(),
    Sell-to Volume Fee ($/Gal)       — fee from sell_to_df if uploaded, else fallback,
    Custom Label Bracket (Gal/Yr)    — bracket from _bracket_custom_label_volume(),
    Custom Label Fee ($/Gal)         — fee from custom_label_df if uploaded, else fallback.

    Design decisions
    ----------------
    * Full Pallet% consumes the row-level Pallet Status set by _process_shipment_data;
      no threshold is re-evaluated here.
    * Volume bracket thresholds are hardcoded; fee values are dynamic from the uploaded
      CSVs when available, falling back to hardcoded dicts otherwise.
    * No file I/O occurs here; all lookup DataFrames are pre-loaded by render().
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
    # Duration comes from the Order Date range slicer, so Annualized Gallons
    # automatically reflect the chosen period.
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
    # Only non-"DG" products contribute to the site total.  Every row at a site
    # (including DG rows) receives the same site-level value so the column is
    # uniform across all product lines for a given Customer × SHIPTONAME.
    # Sites where every product is DG-branded get 0 (left-join produces NaN → 0).
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

    agg = agg.rename(columns={"_custom_vol": "Site-level Custom-label Volume (Gallons)"})

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
    agg["Drop Size (lbs per drop)"] = (agg["_site_lbs"] / agg["Count of Unique Orders"]).round(1)
    agg = agg.drop(columns=["_site_lbs"])

    # ── Delivery tiers and charge ─────────────────────────────────────────────
    # Mileage Fee Tier: derived from the "Mileage" column (may contain "n/a").
    # Drop Fee Tier:    derived from "Drop Size (lbs per drop)" (always numeric).
    # Delivery Charge:  looked up from the hardcoded _DELIVERY_FEES 2-D dict.
    #   • Unrecognised (tier, tier) combos (e.g. "Not Applicable" × a valid drop
    #     tier) default to 0.0 via dict.get().
    #   • Pricing Method == 0 indicates FOB delivery — the customer is not billed
    #     for delivery, so the charge is forced to 0 regardless of tiers.
    agg["Mileage Fee Tier (Mi)"] = _bracket_mileage(agg["Mileage"])
    agg["Drop Fee Tier (lbs/Drop Size)"] = _bracket_drop_size(agg["Drop Size (lbs per drop)"])

    fee_keys = list(zip(agg["Mileage Fee Tier (Mi)"], agg["Drop Fee Tier (lbs/Drop Size)"]))
    agg["Delivery Charge ($/Gal)"] = [_DELIVERY_FEES.get(k, 0.0) for k in fee_keys]

    # FOB override: Pricing Method 0 means the seller, not the buyer, covers freight.
    is_fob = agg["Pricing Method"].astype(str).eq("0")
    agg.loc[is_fob, "Delivery Charge ($/Gal)"] = 0.0

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

    # ── Custom Label Bracket and Fee ─────────────────────────────────────────
    # Same pattern as sell-to: hardcoded thresholds, dynamic fees when uploaded.
    # Zero-volume rows (DG products) are assigned "Not Applicable" by the
    # bracketing function before the fee lookup is applied.
    agg["Custom Label Bracket (Gal/Yr)"] = _bracket_custom_label_volume(
        agg["Site-level Custom-label Volume (Gallons)"]
    )
    if custom_label_df is not None:
        cl_fee_lookup = (
            custom_label_df[["Custom Label Bracket (Gal/Yr)", "Custom Label Fee ($/Gal)"]]
            .drop_duplicates(subset=["Custom Label Bracket (Gal/Yr)"])
        )
        agg = agg.merge(cl_fee_lookup, on="Custom Label Bracket (Gal/Yr)", how="left")
    else:
        agg["Custom Label Fee ($/Gal)"] = (
            agg["Custom Label Bracket (Gal/Yr)"].map(_CUSTOM_LABEL_FEES_FALLBACK)
        )

    # ── Rename and enforce column order ──────────────────────────────────────
    agg = agg.rename(columns={"PRODUCTGROUP": "Product Group"})

    ordered_cols = [
        "Customer", "SHIPTONAME", "PRODUCTDESC", "Product Group",
        "Ordered Secondary QTY", "Ordered LBS",
        "Count of Unique Orders", "Duration", "Annualized Gallons",
        "Site-level Sell-to Volume (Gallons)",
        "Site-level Custom-label Volume (Gallons)",
        "Drop Size (lbs per drop)", "Drop Fee Tier (lbs/Drop Size)",
        "Pricing Method", "Mileage", "Mileage Fee Tier (Mi)",
        "Delivery Charge ($/Gal)",
        "Full Pallet%", "Pallet Status", "Mixed Pallet Fee",
        "Sell-to Volume Bracket", "Sell-to Volume Fee ($/Gal)",
        "Custom Label Bracket (Gal/Yr)", "Custom Label Fee ($/Gal)",
    ]
    present = [c for c in ordered_cols if c in agg.columns]
    return (
        agg[present]
        .sort_values(["Customer", "SHIPTONAME", "PRODUCTDESC"])
        .reset_index(drop=True)
    )


# ── 6. UI rendering ───────────────────────────────────────────────────────────

class _ShipmentSourceResult(NamedTuple):
    """Outcome of an attempted shipment-source pull.

    Attributes
    ----------
    df          : Shipments DataFrame on success; None when the lakehouse
                  fetch failed and the page should fall back to upload.
    meta        : SnapshotMeta on success, else None.
    signature   : meta.cache_key on success — used by render() as part of
                  the enrichment-cache signature.
    error       : Human-readable error message on failure (for diagnostics).
                  None on success.
    """
    df:        Optional[pd.DataFrame]
    meta:      Optional[SnapshotMeta]
    signature: Optional[str]
    error:     Optional[str]


def _render_shipment_source() -> _ShipmentSourceResult:
    """Render the Pricing Lakehouse shipment-source section.

    The lakehouse pull is the page's default ingest mode — there is no
    longer a user-facing toggle.  This function tries the pull, surfaces
    the resulting "data as of" caption + HTST-only download button when it
    succeeds, and returns a structured failure result (no inline error
    rendering) when it fails so render() can switch into upload mode
    without the user seeing a red banner.

    Returns
    -------
    :class:`_ShipmentSourceResult`
        Carries either the snapshot + cache signature (success) or a
        plain-text failure reason (fallback path).
    """
    st.markdown("### 🛰️ HTST Shipment Source — Pricing Lakehouse")

    # The refresh button must be wired BEFORE fetch_htst_shipment_df() runs so
    # that clicking it clears the cache on this same render pass.
    refresh_clicked = st.button(
        "🔄 Refresh from Lakehouse",
        key="htst_shipment_refresh",
        help="Bypass the 15-minute cache and re-read the latest lakehouse snapshot.",
    )
    try:
        with st.spinner("Reading HTST Shipment Report from the Pricing Lakehouse…"):
            df, meta = fetch_htst_shipment_df(force_refresh=refresh_clicked)
    except HTSTShipmentSourceError as exc:
        # Don't surface an error banner here — render() decides how loudly
        # to message the fallback once it has the structured failure.
        return _ShipmentSourceResult(None, None, None, str(exc))

    last_mod = meta.last_modified.strftime("%Y-%m-%d %H:%M UTC") if meta.last_modified else "unknown"
    st.caption(
        f"🛰️ Shipment data **as of {last_mod}** "
        f"· Delta version **v{meta.version}** "
        f"· **{meta.row_count:,}** rows"
    )

    # ── HTST-only raw download button ─────────────────────────────────────────
    # Available in both local and Streamlit Cloud sessions because
    # st.download_button streams over HTTP, not the WebSocket — there is no
    # client-side limit on the payload size.  CSV bytes are produced lazily
    # and cached by snapshot signature inside _htst_filtered_csv_bytes, so
    # the filter + CSV-encode pass runs at most once per lakehouse refresh.
    try:
        csv_bytes = _htst_filtered_csv_bytes(df, meta.cache_key)
    except KeyError:
        st.warning(
            "HTST-only download unavailable: the lakehouse table is missing "
            "the 'PRODUCTGROUP' column."
        )
    else:
        if csv_bytes:
            today = datetime.now().strftime("%Y%m%d")
            st.download_button(
                label="⬇️ Download HTST-Only Shipment Data (CSV)",
                data=csv_bytes,
                file_name=f"HTST_Shipment_{today}.csv",
                mime="text/csv",
                key="htst_dataflow_raw_download",
                help=(
                    "HTST-filtered raw shipment table from the lakehouse, "
                    "before any lookup merges or enrichment."
                ),
            )

    return _ShipmentSourceResult(df, meta, meta.cache_key, None)


def _render_upload_section(*, include_shipment: bool) -> dict[str, object]:
    """Render the multi-file uploader and return the auto-detected file map.

    Two layouts share the same uploader widget (and Streamlit key) so files
    persist when the user toggles the dataflow checkbox in either direction:

    * include_shipment=True  (default mode) — the user is expected to upload
      the HTST Shipment Report alongside the four lookup CSVs in a single
      action.  _SHIPMENT_PATTERN is appended to the active pattern list and
      the returned dict carries an additional "shipment" key.
    * include_shipment=False (dataflow mode) — the shipment is sourced from
      _render_shipment_source(); only the lookup CSVs are needed here.

    The SharePoint folder link stays visible in both layouts so users always
    know where to find the canonical files.
    """
    if include_shipment:
        # _SHIPMENT_PATTERN is appended LAST so its broad keywords ("shipment_
        # report", etc.) cannot accidentally claim a lookup file whose name
        # happens to contain "shipment" (e.g. "Shipment_Plant_Tracker.csv").
        patterns = [*_FILE_PATTERNS, _SHIPMENT_PATTERN]
        header = "### 📤 Upload Required Reports"
        body = (
            "Upload the HTST Shipment Report and all lookup CSVs together "
            "(Plant Tracker, Mileage Tracker, Demantra, Pricing Tracker, "
            "plus any optional fee files).  Files are identified automatically "
            "by filename keywords. "
            f"[📁 Find these reports in this SharePoint folder]({_HTST_SHIPMENT_MONITOR_SHAREPOINT_URL})"
        )
        prompt = "Select all the HTST CSV files (Shipment Report + Lookups)"
    else:
        patterns = list(_FILE_PATTERNS)
        header = "### 📤 Upload Lookup Files"
        body = (
            "Upload the lookup CSVs (Plant Tracker, Mileage Tracker, Demantra, "
            "Pricing Tracker, plus any optional fee files).  The HTST Shipment "
            "Report is being pulled from the Fabric dataflow above. "
            f"[📁 Find these reports in this SharePoint folder]({_HTST_SHIPMENT_MONITOR_SHAREPOINT_URL})"
        )
        prompt = "Select the HTST lookup CSV files"

    st.markdown(header)
    st.caption(body)

    uploaded_files = st.file_uploader(
        prompt,
        type=["csv"],
        accept_multiple_files=True,
        key="htst_all_files",
    )
    return _detect_files(uploaded_files or [], patterns=patterns)


def _render_filters(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Render date range slicer, Customer multiselect, and SHIPTONAME multiselect.

    Returns (filtered_df, duration_days) where:
    - filtered_df   — subset of *df* matching all current widget selections.
    - duration_days — calendar-day span of the selected Order Date range (≥ 1),
                      used as the annualisation denominator in Customer-Site Details
                      so that all volume metrics reflect the chosen period.

    Filter application order (each stage feeds the next):
      1. Order Date range  → date_df       (bounds Customer/Ship-To options)
      2. Customer          → customer_df
      3. Ship-To Name      → filtered_df   (returned)

    Widget auto-reset strategy
    --------------------------
    All three widget keys embed hashes of upstream selections so that Streamlit
    treats them as brand-new whenever an upstream filter changes, resetting them
    to their full defaults without manual session_state manipulation:
    - Customer key   embeds the date range hash.
    - Ship-To key    embeds both date range and customer selection hashes.
    """
    st.markdown("### 🔍 Filter")

    # ── 1. Order Date range ───────────────────────────────────────────────────
    # 'Order Date' is pre-parsed to datetime64 by _process_shipment_data, so
    # this is just an idempotent dtype check (microseconds).  Strings still
    # work because pd.to_datetime is a no-op on already-datetime Series.
    date_col = "Order Date"
    if date_col in df.columns:
        date_series: Optional[pd.Series] = pd.to_datetime(df[date_col], errors="coerce")
        valid_dates = date_series.dropna()
        min_dt: Optional[date] = valid_dates.min().date() if not valid_dates.empty else None
        max_dt: Optional[date] = valid_dates.max().date() if not valid_dates.empty else None
    else:
        date_series = None
        min_dt = max_dt = None

    if min_dt is not None and min_dt != max_dt:
        date_range = st.date_input(
            "Order Date Range",
            value=(min_dt, max_dt),
            min_value=min_dt,
            max_value=max_dt,
            key="htst_filter_date_range",
            help=(
                "Select the order date window.  All downstream metrics — "
                "Annualized Gallons, volume brackets, and fees — "
                "recalculate automatically based on this period."
            ),
        )
        # Guard: during user interaction st.date_input may return a 1-element
        # tuple (start only, end not yet clicked).  Fall back to the full range.
        if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
            sel_start, sel_end = date_range[0], date_range[1]
        else:
            sel_start, sel_end = min_dt, max_dt

        # Normalise to midnight before comparison so rows whose Order Date
        # parsed with a time component are not unintentionally excluded.
        norm  = date_series.dt.normalize()
        mask  = (norm >= pd.Timestamp(sel_start)) & (norm <= pd.Timestamp(sel_end))
        date_df      = df[mask.fillna(False)]
        duration_days = max((sel_end - sel_start).days, 1)
    else:
        # No parseable dates — skip the date filter entirely.
        if min_dt is None:
            st.caption("ℹ️ 'Order Date' column could not be parsed — date filter unavailable.")
        sel_start = sel_end = None
        date_df       = df
        duration_days = 1

    # Fragment embedded in Customer and Ship-To keys; changes when the date
    # range changes, causing both downstream widgets to auto-reset.
    date_key = f"{sel_start}:{sel_end}" if sel_start is not None else "nodate"

    # ── 2. Customer & Ship-To filters ─────────────────────────────────────────
    f1, f2 = st.columns(2)

    with f1:
        all_customers = sorted(date_df["Customer"].dropna().astype(str).unique().tolist())
        sel_customers = st.multiselect(
            "Customer",
            options=all_customers,
            default=all_customers,
            # Key changes with the date range so the widget resets when the
            # date window changes (customers in range may differ).
            key=f"htst_filter_customer_{_widget_key(date_key)}",
            help="Select one or more customers.  Ship-To options narrow automatically.",
        )

    df_by_customer = (
        date_df[date_df["Customer"].astype(str).isin(sel_customers)]
        if sel_customers else date_df.iloc[0:0]
    )

    with f2:
        shiptoname_opts = sorted(
            df_by_customer["SHIPTONAME"].dropna().astype(str).unique().tolist()
        )
        sel_shiptonames = st.multiselect(
            "Ship-To Name",
            options=shiptoname_opts,
            default=shiptoname_opts,
            # Key embeds both date and customer so this widget resets whenever
            # either upstream selection changes.
            key=f"htst_filter_shiptoname_{_widget_key(date_key + str(sorted(sel_customers)))}",
            help="Options narrow automatically based on the Customer selection above.",
        )

    filtered = (
        df_by_customer[df_by_customer["SHIPTONAME"].astype(str).isin(sel_shiptonames)]
        if sel_shiptonames else df_by_customer.iloc[0:0]
    )
    st.caption(f"**{len(filtered):,}** rows match the current filter criteria.")
    return filtered, duration_days


def _render_customer_site_details(
    filtered_df: pd.DataFrame,
    duration_days: int,
    pallet_fee_df: Optional[pd.DataFrame],
    sell_to_df: Optional[pd.DataFrame],
    custom_label_df: Optional[pd.DataFrame],
) -> None:
    """Render the Customer-Site Details aggregated summary table.

    All optional lookup DataFrames (*pallet_fee_df*, *sell_to_df*,
    *custom_label_df*) are pre-loaded by render() from the respective
    uploaded files (None when not uploaded).  Passing them as DataFrames
    keeps this function — and _build_customer_site_summary — free of file I/O.
    """
    st.markdown("### 📊 Customer-Site Details")

    summary = _build_customer_site_summary(
        filtered_df, duration_days, pallet_fee_df, sell_to_df, custom_label_df
    )

    if summary.empty:
        st.info("No data matches the current filters.")
        return

    st.caption(
        f"Aggregated by Customer × Ship-To × Product. "
        f"Duration: **{duration_days:,} days** (selected date range). "
        f"Annualized Gallons reflect the activity rate for this period."
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
    """Render the HTST Activity Monitor page.

    Flow: lakehouse pull (auto) → on success, lookup-only uploads;
          on failure, full upload (shipment + lookups) → validate →
          process (cached) → load optional lookups → filter →
          Customer-Site Details → Enriched Report.

    Business logic is fully delegated to section-specific functions.
    The enrichment pipeline is cached in st.session_state so it only re-runs
    when the shipment snapshot OR the lookup file set changes — not on every
    widget interaction.  duration_days is derived from the Order Date range
    slicer inside _render_filters and is NOT cached — it updates instantly
    with the slicer.  Optional lookup DataFrames (pallet_fee_df, sell_to_df,
    custom_label_df) are loaded here from uploaded file objects and passed as
    arguments — no function downstream opens a file directly.
    """
    apply_custom_css()

    st.markdown(
        '<h1 class="main-header">Shipment Monitor &amp; HTST Requote</h1>',
        unsafe_allow_html=True,
    )

    # ── Welcome ───────────────────────────────────────────────────────────────
    st.markdown("### Welcome")
    st.info(
        "Shipment data is pulled automatically from the Pricing Lakehouse. "
        "Upload the lookup CSVs below to refresh the HTST activity-level "
        "report.  If the lakehouse pull fails (typically when running on "
        "Streamlit Cloud), the full multi-file upload panel will appear "
        "automatically as a fallback."
    )
    st.markdown("---")

    # ── Shipment input — lakehouse first, upload fallback on failure ─────────
    # No user-facing toggle: the page always tries the Pricing Lakehouse
    # pull first.  If the pull fails (typical on a headless Streamlit Cloud
    # session that cannot complete an interactive Azure sign-in), the page
    # automatically reveals the full upload panel below — the user never
    # has to click a checkbox to switch modes.
    source_result = _render_shipment_source()
    use_upload_fallback = source_result.df is None
    shipment_df = source_result.df
    shipment_sig = source_result.signature

    if use_upload_fallback:
        st.warning(
            "⚠️ Could not pull the HTST Shipment Report from the Pricing "
            "Lakehouse — falling back to manual upload.  Upload the HTST "
            "Shipment Report alongside the lookup CSVs in the panel below.\n\n"
            "If Microsoft Fabric is not signed in, visit **Home & Fabric Sign-in** "
            "in the sidebar to sign in, then return to this page."
        )
        with st.expander("Why did the lakehouse pull fail?", expanded=False):
            st.code(source_result.error or "Unknown error.", language="text")
    st.markdown("---")

    # ── Lookup uploads (and the shipment file when in fallback mode) ─────────
    detected = _render_upload_section(include_shipment=use_upload_fallback)
    st.markdown("---")

    # In fallback mode, materialise the uploaded shipment CSV before the
    # required-missing gate so its presence/absence is reflected in the
    # waiting message alongside the lookup files.
    if use_upload_fallback:
        shipment_file = detected.get("shipment")
        if shipment_file is not None:
            try:
                shipment_df = pd.read_csv(shipment_file, low_memory=False)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not read uploaded HTST Shipment Report: {exc}")
                return
            shipment_sig = f"upload:{shipment_file.name}:{shipment_file.size}"

    # Required-files gate.  In fallback mode we additionally check the
    # shipment file via shipment_df, since _SHIPMENT_PATTERN is not part of
    # _FILE_PATTERNS.
    required_missing = [
        p.label for p in _FILE_PATTERNS
        if p.required and detected.get(p.key) is None
    ]
    if use_upload_fallback and shipment_df is None:
        required_missing.append(_SHIPMENT_PATTERN.label)
    if required_missing:
        # Surface the explicit list of files we're still waiting on so the
        # user knows exactly what to upload — much clearer than the prior
        # "calculation in progress" wording, which made the static
        # waiting-state look like a hung pipeline.
        missing_md = "\n".join(f"  • {label}" for label in required_missing)
        st.info(
            "⏳ Waiting for the following required report(s) before "
            f"processing can begin:\n{missing_md}"
        )
        return

    # ── Process main enrichment pipeline (session-state cached) ───────────────
    # _process_shipment_data joins the in-memory shipment DataFrame against
    # four uploaded lookup CSVs — an expensive operation on a 400 K-row table.
    # Streamlit re-executes render() on every widget interaction, so without
    # caching this pipeline would re-run on every click.
    #
    # Strategy: build a composite signature from the shipment snapshot identity
    # AND the four lookup files' name+size.  If the signature matches what is
    # stored in session_state, the enriched DataFrame is read from cache;
    # otherwise the pipeline runs and the results are stored.
    _REQUIRED_LOOKUP_KEYS = [
        "plant_tracker", "mileage_tracker", "demantra", "pricing_tracker"
    ]
    lookup_sig_input = "".join(
        f"{detected[k].name}:{detected[k].size}" for k in _REQUIRED_LOOKUP_KEYS
    )
    file_sig = hashlib.md5(
        f"{shipment_sig}||{lookup_sig_input}".encode()
    ).hexdigest()

    if st.session_state.get("_htst_file_sig") != file_sig:
        with st.spinner("Processing — enriching shipment data…"):
            enriched_df = _process_shipment_data(
                shipment_df=shipment_df,
                plant_tracker_file=detected["plant_tracker"],
                mileage_tracker_file=detected["mileage_tracker"],
                demantra_file=detected["demantra"],
                pricing_tracker_file=detected["pricing_tracker"],
            )
        if enriched_df is None:
            return  # st.error already raised inside _process_shipment_data
        st.session_state["_htst_enriched_df"] = enriched_df
        st.session_state["_htst_file_sig"]    = file_sig

    enriched_df = st.session_state.get("_htst_enriched_df")

    if enriched_df is None:
        # Defensive guard: cache entry missing (e.g. session was reset).
        st.error("Processed data unavailable — please refresh the lakehouse pull and re-upload the lookup files.")
        return

    # ── Load optional lookup tables from uploaded files ───────────────────────
    # All three are loaded here so _build_customer_site_summary stays a pure
    # analytics function with no file I/O.  None is passed when not uploaded;
    # _build_customer_site_summary then substitutes hardcoded fallback fees.
    pallet_fee_df   = _load_optional_lookup(detected.get("pallet_fee"),   "Pallet Fee file")
    sell_to_df      = _load_optional_lookup(detected.get("sell_to"),      "Sell-To Volume Bracket file")
    custom_label_df = _load_optional_lookup(detected.get("custom_label"), "Custom Label Volume Bracket file")

    st.success(
        f"✅ Processing complete — **{len(enriched_df):,} rows**, "
        f"**{len(enriched_df.columns)} columns**"
    )
    st.markdown("---")

    # ── Filters (drive both downstream sections) ──────────────────────────────
    # _render_filters returns the date-filtered + customer/ship-to filtered df
    # together with duration_days derived from the selected date range.
    filtered_df, duration_days = _render_filters(enriched_df)
    st.markdown("---")

    # ── Customer-Site Details ─────────────────────────────────────────────────
    _render_customer_site_details(
        filtered_df, duration_days, pallet_fee_df, sell_to_df, custom_label_df
    )
    st.markdown("---")

    # ── Enriched Shipment Report (filtered, downloadable) ─────────────────────
    _render_output_section(filtered_df)
