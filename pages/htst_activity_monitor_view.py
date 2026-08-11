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
from datetime import datetime
from typing import Callable, NamedTuple, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from data_sources.htst_shipment import (
    HTSTShipmentSourceError,
    SnapshotMeta,
    fetch_htst_shipment_df,
)
from data_sources.htst_shipment_lookups import (
    FABRIC_SHIPMENT_REPORT_URL,
    HTSTLookupBundle,
    HTSTLookupError,
    fetch_htst_lookups,
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
# page.  Filtering early is primarily a CORRECTNESS requirement — non-HTST
# rows would otherwise contaminate site-level aggregates (e.g. Site-level
# Sell-to Volume sums).  It is a weak memory lever: measured against
# dbo/Shipments at Delta v97, HTST is 435 773 of 629 403 rows (69 %), not
# the ~1/7 an earlier revision of this page assumed.  The memory win comes
# from the column projection in data_sources/htst_shipment.py instead.
# Comparison is uppercase + stripped to absorb formatting drift in the source.
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

    Row-level frames on this page are ~436 K rows; prefer
    :func:`_lazy_csv_download` over calling this inline so the encode does
    not run on every rerun.
    """
    return df.to_csv(index=False).encode("utf-8")


def _frame_identity(df: pd.DataFrame) -> str:
    """Return a cheap, collision-resistant identity string for *df*.

    Used to decide whether an already-prepared CSV payload still matches
    what is on screen.  Every row-level frame here is an index-preserving
    boolean-mask subset of the enriched frame, so hashing the *index* (plus
    shape and column names) identifies the exact subset without touching
    the values.  ``hash_pandas_object`` is a vectorised C loop — ~2 ms on
    436 K rows, against the ~2-4 s the CSV encode it guards would cost.
    """
    idx_hash = (
        int(pd.util.hash_pandas_object(df.index, index=False).sum()) if len(df) else 0
    )
    cols_hash = hashlib.md5("\x00".join(map(str, df.columns)).encode()).hexdigest()[:8]
    return f"{len(df)}:{cols_hash}:{idx_hash & 0xFFFF_FFFF_FFFF}"


def _lazy_csv_download(
    *,
    label: str,
    key: str,
    file_name: str,
    identity: str,
    build: Callable[[], bytes],
    help_text: Optional[str] = None,
) -> None:
    """Render a prepare→download pair instead of encoding CSV on every rerun.

    Why not a plain ``st.download_button``
    -------------------------------------
    ``st.download_button`` needs its full payload up front, so rendering one
    directly means serialising the entire frame to CSV on EVERY script rerun
    — every filter change, every widget click — even though almost nobody
    clicks download.  At ~436 K rows that is seconds of CPU and tens of MB
    of transient allocation per interaction, on a container with ~1 GB.

    So the encode is deferred behind an explicit "Prepare" click and the
    bytes are parked in ``session_state`` under *key*.

    Staleness
    ---------
    A prepared payload is only offered while *identity* still matches the
    data on screen; change a filter and the download button disappears in
    favour of the prepare button again.  This is the whole reason the
    helper takes an identity rather than just caching blindly — handing a
    user a CSV that silently disagrees with the table above it would be a
    worse bug than the one this function exists to fix.
    """
    slot = f"{key}__prepared"
    prepared = st.session_state.get(slot)

    if prepared is not None and prepared[0] == identity:
        st.download_button(
            label=f"⬇️ Download {label}",
            data=prepared[1],
            file_name=file_name,
            mime="text/csv",
            key=key,
            help=help_text,
        )
        return

    if st.button(
        f"🧾 Prepare {label}",
        key=f"{key}__prepare",
        help=help_text or "Builds the CSV, then reveals the download button.",
    ):
        with st.spinner(f"Building {label}…"):
            st.session_state[slot] = (identity, build())
        st.rerun()


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


def _htst_filtered_csv_bytes(df: pd.DataFrame) -> bytes:
    """Serialize the HTST-filtered subset of *df* to UTF-8 CSV bytes.

    The connector already applies the HTST filter in DuckDB, so
    :func:`_filter_to_htst` here is an idempotent safety net that also
    covers the manual-upload path.

    Not cached: this is invoked only from :func:`_lazy_csv_download`, which
    parks the finished bytes in ``session_state`` keyed by snapshot
    identity.  Caching here as well would keep a second copy of a
    multi-tens-of-MB payload resident for no benefit.
    """
    return _filter_to_htst(df).to_csv(index=False).encode("utf-8")


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
    lookups: dict[str, pd.DataFrame],
) -> Optional[pd.DataFrame]:
    """Enrich the HTST Shipment Report in a sequential pipeline.

    The shipment DataFrame is supplied directly by the caller — sourced from
    either the Fabric Delta table (preferred) or the manual-upload fallback.
    ``lookups`` holds the four enrichment tables as DataFrames, keyed
    ``plant_tracker`` / ``mileage_tracker`` / ``demantra`` / ``pricing_tracker``
    — read from the lakehouse (``htst_shipment_lookups``) or uploaded CSVs.

    Enrichment steps
    ----------------
    0a. Normalise column names (strip whitespace).
    0b. Filter to PRODUCTGROUP == _PRODUCT_GROUP_FILTER ("HTST").  Done first
        so all downstream merges and aggregations operate on the HTST subset
        rather than the full Shipments table.  In lakehouse mode the
        connector has already applied this in DuckDB, so it is an idempotent
        no-op there and load-bearing only on the manual-upload path.
    0c. Defensive copy + drop noise columns (_DROP_COLS).
    0d. Pre-parse 'Order Date' once into datetime64.  _render_filters then
        skips its own O(n) parse on every widget interaction.
    1.  Left-join Plant Tracker on 'Shipping Warehouse'
        → 'Sourcing Plant' inserted after 'Shipping Warehouse'.
    2.  Left-join Mileage Tracker on ('Sourcing Plant', 'SHIPTONAME')
        → 'Mileage' inserted after 'Sourcing Plant'; missing rows → 'n/a'.
    3.  Left-join Demantra on PRODUCTDESC = 'Item Description'
        → 'Total Each Per Pallet' and 'Unit Net Weight' after 'Ordered LBS';
        also derive 'Format' = "{Product Size} {Unit Pkg Type}" (e.g. "Gallon
        Plastic Jug"), inserted after 'PRODUCTDESC' — the dashboard's Format filter.
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
        plant_df = lookups["plant_tracker"].copy()
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
        mile_df = lookups["mileage_tracker"].copy()
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

    # ── Step 3: Pallet & weight data + Format from Demantra ──────────────────
    try:
        dem_df = lookups["demantra"].copy()
        dem_df.columns = dem_df.columns.str.strip()
        # Format = "Product Size + Unit Pkg Type" (e.g. "Gallon Plastic Jug"),
        # the operational pack the dashboard filters by; blanks collapse cleanly.
        size = dem_df.get("Product Size", "").astype(str).str.strip().replace("nan", "")
        pkg = dem_df.get("Unit Pkg Type", "").astype(str).str.strip().replace("nan", "")
        dem_df["Format"] = (size + " " + pkg).str.strip().replace("", "(unmapped)")
        dem_lookup = (
            dem_df[["Item Description", "Total Each Per Pallet", "Unit Net Weight", "Format"]]
            .drop_duplicates(subset=["Item Description"])
        )
        df = df.merge(
            dem_lookup,
            left_on="PRODUCTDESC",
            right_on="Item Description",
            how="left",
        )
        df = df.drop(columns=["Item Description"], errors="ignore")
        df["Format"] = df["Format"].fillna("(unmapped)")
        df = _insert_col_after(df, "PRODUCTDESC", "Format")
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
        price_df = lookups["pricing_tracker"].copy()
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



def _delivery_fee_map(delivery_df: Optional[pd.DataFrame]) -> dict[tuple[str, str], float]:
    """Build the (Mileage Tier, Drop Tier) → $/Gal delivery-charge map.

    Reads the authoritative lakehouse table when available; falls back to the
    hard-coded :data:`_DELIVERY_FEES` otherwise.  The charge column is parsed
    currency-tolerantly (strips ``$``, spaces, commas)."""
    if delivery_df is None or delivery_df.empty:
        return dict(_DELIVERY_FEES)
    cols = {c.strip(): c for c in delivery_df.columns}
    mile_c = cols.get("Mileage Fee Tier (Mi)")
    drop_c = cols.get("Drop Fee Tier (lbs/Drop Size)")
    fee_c = cols.get("Delivery Charge ($/Gal)")
    if not (mile_c and drop_c and fee_c):
        return dict(_DELIVERY_FEES)
    fees = pd.to_numeric(
        delivery_df[fee_c].astype(str).str.replace(r"[$,\s]", "", regex=True),
        errors="coerce",
    )
    out: dict[tuple[str, str], float] = {}
    for m, d, f in zip(delivery_df[mile_c].astype(str).str.strip(),
                       delivery_df[drop_c].astype(str).str.strip(), fees):
        if pd.notna(f):
            out[(m, d)] = float(f)
    return out or dict(_DELIVERY_FEES)


def _build_customer_site_summary(
    filtered_df: pd.DataFrame,
    duration_days: int,
    pallet_fee_df: Optional[pd.DataFrame] = None,
    sell_to_df: Optional[pd.DataFrame] = None,
    custom_label_df: Optional[pd.DataFrame] = None,
    delivery_df: Optional[pd.DataFrame] = None,
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

    # The lakehouse shipment frame stores identity columns as `category` dtype;
    # grouping on categoricals (observed=False) explodes to the Cartesian product
    # of every level.  Cast the group keys to plain strings once (cheap — only
    # these columns are copied) so every groupby here and in _site_fee_stack
    # stays on the observed combinations.
    _cast = {c: filtered_df[c].astype(str)
             for c in ("Customer", "SHIPTONAME", "PRODUCTDESC", "PRODUCTGROUP", "Format")
             if c in filtered_df.columns and str(filtered_df[c].dtype) == "category"}
    if _cast:
        filtered_df = filtered_df.assign(**_cast)

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
                **({"Format": ("Format", "first")} if "Format" in filtered_df.columns else {}),
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

    fee_map = _delivery_fee_map(delivery_df)
    fee_keys = list(zip(agg["Mileage Fee Tier (Mi)"], agg["Drop Fee Tier (lbs/Drop Size)"]))
    agg["Delivery Charge ($/Gal)"] = [fee_map.get(k, 0.0) for k in fee_keys]

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
        "Customer", "SHIPTONAME", "PRODUCTDESC", "Format", "Product Group",
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


# ── 5b. Trailing-window momentum + requote analytics ──────────────────────────
# The dashboard replaces a free date slicer with fixed trailing windows ending
# at the latest Order month, so a planner sees how activity metrics MOVE and
# where behaviour has drifted across a fee bracket (a requote trigger).

# Operating days per year — the same annualisation constant the single-window
# summary uses (Ordered Secondary QTY / window-days × _ANNUALIZE_DAYS).
_ANNUALIZE_DAYS: int = 350
# Trailing windows, widest → narrowest (label, months back from the latest month).
_TRAILING_WINDOWS: tuple[tuple[str, int], ...] = (("L12M", 12), ("L6M", 6), ("L3M", 3))


def _trailing_windows(order_dt: pd.Series) -> dict[str, tuple[pd.Timestamp, pd.Timestamp, int]]:
    """``{label: (start, end, days)}`` for L12M/L6M/L3M ending at the latest
    order date.  Empty when no parseable dates.  ``days`` is the inclusive span,
    used as the annualisation denominator so windows are comparable."""
    valid = pd.to_datetime(order_dt, errors="coerce").dropna()
    if valid.empty:
        return {}
    latest = valid.max().normalize()
    out: dict[str, tuple[pd.Timestamp, pd.Timestamp, int]] = {}
    for label, n in _TRAILING_WINDOWS:
        start = (latest - pd.DateOffset(months=n) + pd.Timedelta(days=1)).normalize()
        out[label] = (start, latest, max((latest - start).days + 1, 1))
    return out


def _window_slice(df: pd.DataFrame, window: tuple[pd.Timestamp, pd.Timestamp, int]) -> pd.DataFrame:
    """Rows of *df* whose Order Date falls in *window* (start, end, _)."""
    if "Order Date" not in df.columns:
        return df.iloc[0:0]
    dt = pd.to_datetime(df["Order Date"], errors="coerce")
    start, end, _ = window
    return df[((dt >= start) & (dt <= end)).fillna(False)]


# Deep-dive charts: (_window_metrics key, display label, unit, chart type).  A
# metric is charted only when it MOVED (see _metric_moved).  Volumes & drop size
# read best as bars with data labels; pallet share as a line.
_DEEPDIVE_CHART_METRICS: tuple[tuple[str, str, str, str], ...] = (
    ("sellto_vol", "Annualized sell-to volume", "gal", "bar"),
    ("customlabel_vol", "Annualized custom-label volume", "gal", "bar"),
    ("drop_size", "Drop size", "lbs", "bar"),
    ("mileage", "Travel distance", "mi", "bar"),
    ("mixed_pct", "Mixed-pallet share", "%", "line"),
)
# A metric "moved" if L3M vs L12M changed materially: ≥1pp for shares, ≥5% for
# the rest (so a stable series isn't charted just for noise).
_DEEPDIVE_MOVE_PP: float = 0.01
_DEEPDIVE_MOVE_REL: float = 0.05


def _metric_moved(first: Optional[float], last: Optional[float], unit: str) -> bool:
    """True when a metric changed enough (L12M→L3M) to be worth charting."""
    if first is None or last is None or pd.isna(first) or pd.isna(last):
        return False
    if unit == "%":
        return abs(last - first) >= _DEEPDIVE_MOVE_PP
    if abs(first) < 1e-9:
        return abs(last) > 1e-9
    return abs(last - first) / abs(first) >= _DEEPDIVE_MOVE_REL


def _window_metrics(df: pd.DataFrame, days: int) -> dict[str, Optional[float]]:
    """The six momentum metrics for one window slice (None when undefined).

    Volumes annualise the window rate (× _ANNUALIZE_DAYS ÷ window-days); pallet
    share and travel distance are pound-weighted so a few big drops dominate."""
    keys = ("mixed_pct", "full_pct", "sellto_vol", "customlabel_vol", "drop_size", "mileage")
    if df.empty:
        return {k: None for k in keys}
    qty = pd.to_numeric(df["Ordered Secondary QTY"], errors="coerce").fillna(0.0)
    lbs = pd.to_numeric(df["Ordered LBS"], errors="coerce").fillna(0.0)
    total_lbs = float(lbs.sum())
    non_dg = ~df["PRODUCTDESC"].astype(str).str.startswith("DG")
    n_orders = int(df["Order Number"].nunique()) if "Order Number" in df.columns else 0
    mnum = pd.to_numeric(df["Mileage"], errors="coerce") if "Mileage" in df.columns else pd.Series(dtype=float)
    mweight = lbs.where(mnum.notna(), 0.0)
    if "Pallet Status" in df.columns and total_lbs > 0:
        mixed_pct = float(lbs.where(df["Pallet Status"].eq("Mixed"), 0.0).sum()) / total_lbs
    else:
        mixed_pct = None
    return {
        "mixed_pct": mixed_pct,
        "full_pct": (1.0 - mixed_pct) if mixed_pct is not None else None,
        "sellto_vol": float(qty.sum()) / days * _ANNUALIZE_DAYS,
        "customlabel_vol": float(qty[non_dg].sum()) / days * _ANNUALIZE_DAYS,
        "drop_size": (total_lbs / n_orders) if n_orders else None,
        "mileage": (float((mnum.fillna(0.0) * mweight).sum()) / float(mweight.sum()))
                   if float(mweight.sum()) > 0 else None,
    }


# A customer qualifies for a deep-dive when the fee change would recover more
# than this per MONTH (annualized ÷ 12) — sales-actionable, not noise.
_DEEPDIVE_MONTHLY_THRESHOLD: float = 1_000.0
_DEEPDIVE_FEE_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("Sell-to Fee", "Sell-to"),
    ("Custom-label Fee", "Custom-label"),
    ("Delivery Fee", "Delivery"),
    ("Mixed-pallet Fee", "Mixed-pallet"),
)


def _build_customer_deepdive(
    filtered_df: pd.DataFrame,
    windows: dict,
    pallet_fee_df: Optional[pd.DataFrame],
    sell_to_df: Optional[pd.DataFrame],
    custom_label_df: Optional[pd.DataFrame],
    delivery_df: Optional[pd.DataFrame],
    monthly_threshold: float = _DEEPDIVE_MONTHLY_THRESHOLD,
) -> list[dict]:
    """Per-customer requote deep-dives (> *monthly_threshold* $/mo), ranked by
    annualized $ impact.

    Rolls the site fee stacks up to the CUSTOMER level: each fee component's
    before (L12M) / after (L3M) $/gal is the customer's L3M-volume-weighted
    average across its ship-to sites, so ``Σ Δcomponent × volume`` equals the
    exact dollar impact.  Also attaches each metric's L12M/L6M/L3M series (only
    the ones that moved) for the charts.  Only sites present in BOTH windows
    count (a drift needs a baseline).
    """
    if not windows or filtered_df.empty or {"L12M", "L3M"} - set(windows):
        return []
    kw = dict(pallet_fee_df=pallet_fee_df, sell_to_df=sell_to_df,
              custom_label_df=custom_label_df, delivery_df=delivery_df)
    s12 = _site_fee_stack(_build_customer_site_summary(
        _window_slice(filtered_df, windows["L12M"]), windows["L12M"][2], **kw))
    s3 = _site_fee_stack(_build_customer_site_summary(
        _window_slice(filtered_df, windows["L3M"]), windows["L3M"][2], **kw))
    if s3.empty or s12.empty:
        return []

    m = s3.merge(s12, on=["Customer", "SHIPTONAME"], how="inner",
                 suffixes=(" (L3M)", " (L12M)"))
    m["_vol"] = pd.to_numeric(m["Sell-to Volume (Gal) (L3M)"], errors="coerce").fillna(0.0)

    out: list[dict] = []
    for cust, g in m.groupby("Customer"):
        tot_vol = float(g["_vol"].sum())
        if tot_vol <= 0:
            continue
        components, total_before, total_after = [], 0.0, 0.0
        for col, name in _DEEPDIVE_FEE_COMPONENTS:
            before = float((g[f"{col} (L12M)"] * g["_vol"]).sum()) / tot_vol
            after = float((g[f"{col} (L3M)"] * g["_vol"]).sum()) / tot_vol
            components.append({"name": name, "before": before, "after": after,
                               "delta": after - before})
            total_before += before
            total_after += after
        annual_impact = (total_after - total_before) * tot_vol
        monthly_impact = annual_impact / 12.0
        if monthly_impact <= monthly_threshold:
            continue

        # Customer-level metric series for the charts (only the ones that moved).
        cust_df = filtered_df[filtered_df["Customer"].astype(str) == str(cust)]
        per_window = {lbl: _window_metrics(_window_slice(cust_df, w), w[2])
                      for lbl, w in windows.items()}
        metrics = []
        for key, label, unit, chart in _DEEPDIVE_CHART_METRICS:
            vals = {lbl: per_window[lbl].get(key) for lbl in windows}
            if _metric_moved(vals.get("L12M"), vals.get("L3M"), unit):
                metrics.append({"key": key, "label": label, "unit": unit,
                                "chart": chart, "values": vals})
        out.append({
            "customer": str(cust), "annual_impact": annual_impact,
            "monthly_impact": monthly_impact, "annual_volume": tot_vol,
            "components": components, "total_before": total_before,
            "total_after": total_after, "total_delta": total_after - total_before,
            "metrics": metrics,
        })
    out.sort(key=lambda d: d["annual_impact"], reverse=True)
    return out


def _site_fee_stack(summary: pd.DataFrame) -> pd.DataFrame:
    """Reduce the product-grain summary to one row per Customer × Ship-To with
    the activity fee stack ($/gal) and the tiers/brackets that set it.

    Sell-to / Custom-label / Delivery fees are site-level (constant across the
    site's products); Mixed-pallet fee is pound-weighted across products.
    """
    if summary.empty:
        return pd.DataFrame()
    site = ["Customer", "SHIPTONAME"]
    first_cols = {
        "Sell-to Fee": "Sell-to Volume Fee ($/Gal)",
        "Custom-label Fee": "Custom Label Fee ($/Gal)",
        "Delivery Fee": "Delivery Charge ($/Gal)",
        "Sell-to Volume (Gal)": "Site-level Sell-to Volume (Gallons)",
        "Mileage Tier": "Mileage Fee Tier (Mi)",
        "Drop Tier": "Drop Fee Tier (lbs/Drop Size)",
        "Sell-to Bracket": "Sell-to Volume Bracket",
        "Custom-label Bracket": "Custom Label Bracket (Gal/Yr)",
    }
    spec = {out: (src, "first") for out, src in first_cols.items() if src in summary.columns}
    out = summary.groupby(site, as_index=False).agg(**spec)

    if "Mixed Pallet Fee" in summary.columns and "Annualized Gallons" in summary.columns:
        tmp = summary[[*site, "Mixed Pallet Fee", "Annualized Gallons"]].copy()
        tmp["_w"] = pd.to_numeric(tmp["Mixed Pallet Fee"], errors="coerce").fillna(0.0) \
            * pd.to_numeric(tmp["Annualized Gallons"], errors="coerce").fillna(0.0)
        w = tmp.groupby(site, as_index=False).agg(
            _w=("_w", "sum"), _g=("Annualized Gallons", "sum"))
        w["Mixed-pallet Fee"] = (w["_w"] / w["_g"]).where(w["_g"] > 0, 0.0)
        out = out.merge(w[[*site, "Mixed-pallet Fee"]], on=site, how="left")
    else:
        out["Mixed-pallet Fee"] = 0.0

    for c in ("Sell-to Fee", "Custom-label Fee", "Delivery Fee", "Mixed-pallet Fee"):
        out[c] = pd.to_numeric(out.get(c), errors="coerce").fillna(0.0)
    out["Total Activity Fee"] = out[
        ["Sell-to Fee", "Custom-label Fee", "Delivery Fee", "Mixed-pallet Fee"]].sum(axis=1)
    return out


def _requote_drivers(r: pd.Series) -> str:
    """Concise, human list of which fee components worsened L12M → L3M."""
    parts: list[str] = []
    if r.get("Δ Mixed-pallet Fee", 0) > 1e-9:
        parts.append(f"pallet → more Mixed (+${r['Δ Mixed-pallet Fee']:.3f}/gal)")
    if r.get("Δ Delivery Fee", 0) > 1e-9:
        parts.append(
            f"delivery tier worse (drop {r.get('Drop Tier (L12M)')}→{r.get('Drop Tier (L3M)')})")
    if r.get("Δ Sell-to Fee", 0) > 1e-9:
        parts.append(
            f"sell-to volume fell ({r.get('Sell-to Bracket (L12M)')}→{r.get('Sell-to Bracket (L3M)')})")
    if r.get("Δ Custom-label Fee", 0) > 1e-9:
        parts.append(
            f"custom-label volume rose ({r.get('Custom-label Bracket (L12M)')}→{r.get('Custom-label Bracket (L3M)')})")
    return "; ".join(parts) or "—"


def _build_requote(
    filtered_df: pd.DataFrame,
    windows: dict,
    pallet_fee_df: Optional[pd.DataFrame],
    sell_to_df: Optional[pd.DataFrame],
    custom_label_df: Optional[pd.DataFrame],
    delivery_df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """Requote candidates: sites whose activity fee stack ROSE from L12M to L3M.

    Builds the site fee stack for each window (reusing _build_customer_site_summary
    → _site_fee_stack), diffs L3M vs L12M, and ranks by ``$ Impact`` =
    Δ total fee × L3M annualized volume.  Only worsened sites are returned, so
    the top of the table is where a requote recovers the most margin.
    """
    if not windows or filtered_df.empty or {"L12M", "L3M"} - set(windows):
        return pd.DataFrame()
    kw = dict(pallet_fee_df=pallet_fee_df, sell_to_df=sell_to_df,
              custom_label_df=custom_label_df, delivery_df=delivery_df)
    s12 = _site_fee_stack(_build_customer_site_summary(
        _window_slice(filtered_df, windows["L12M"]), windows["L12M"][2], **kw))
    s3 = _site_fee_stack(_build_customer_site_summary(
        _window_slice(filtered_df, windows["L3M"]), windows["L3M"][2], **kw))
    if s3.empty or s12.empty:
        return pd.DataFrame()

    site = ["Customer", "SHIPTONAME"]
    m = s3.merge(s12, on=site, how="inner", suffixes=(" (L3M)", " (L12M)"))
    fee_cols = ["Sell-to Fee", "Custom-label Fee", "Delivery Fee",
                "Mixed-pallet Fee", "Total Activity Fee"]
    for c in fee_cols:
        m[f"Δ {c}"] = m[f"{c} (L3M)"] - m[f"{c} (L12M)"]
    m["Annualized Volume (Gal, L3M)"] = pd.to_numeric(
        m["Sell-to Volume (Gal) (L3M)"], errors="coerce").fillna(0.0)
    m["$ Impact (Δfee × L3M vol)"] = (
        m["Δ Total Activity Fee"] * m["Annualized Volume (Gal, L3M)"]).round(0)
    m["Requote Drivers (L12M→L3M)"] = m.apply(_requote_drivers, axis=1)

    m = m[m["Δ Total Activity Fee"] > 1e-9].copy()
    if m.empty:
        return pd.DataFrame()
    cols = [
        "Customer", "SHIPTONAME",
        "Total Activity Fee (L12M)", "Total Activity Fee (L3M)", "Δ Total Activity Fee",
        "Annualized Volume (Gal, L3M)", "$ Impact (Δfee × L3M vol)",
        "Requote Drivers (L12M→L3M)",
        "Δ Sell-to Fee", "Δ Custom-label Fee", "Δ Delivery Fee", "Δ Mixed-pallet Fee",
    ]
    cols = [c for c in cols if c in m.columns]
    return (m[cols]
            .sort_values("$ Impact (Δfee × L3M vol)", ascending=False)
            .reset_index(drop=True))


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
        help="Bypass the 60-minute cache and re-read the latest lakehouse snapshot.",
    )
    # Why st.status (and not st.spinner)
    # ----------------------------------
    # The cold-path lakehouse pull is genuinely slow — auth (~1 s) + Delta
    # scan over the network (often 30-300 s depending on table size and
    # corporate-proxy latency) + dtype optimisation (~1 s).  A bare
    # st.spinner shows ONE static message for that entire window, which
    # makes the page look hung the first time an operator hits it.
    # st.status auto-collapses on completion and supports stage labels
    # so we get progress visibility without persistent UI noise — and a
    # cache hit is fast enough (~ms) that the status panel barely
    # flashes before collapsing.
    with st.status(
        "Reading HTST Shipment Report from the Pricing Lakehouse…",
        expanded=False,
    ) as status:
        try:
            status.update(label="Authenticating with Microsoft Fabric…")
            df, meta = fetch_htst_shipment_df(force_refresh=refresh_clicked)
        except HTSTShipmentSourceError as exc:
            status.update(
                label="Lakehouse read failed — see fallback message below.",
                state="error",
                expanded=False,
            )
            # Don't surface an error banner here — render() decides how
            # loudly to message the fallback once it has the structured
            # failure.
            return _ShipmentSourceResult(None, None, None, str(exc))
        status.update(
            label=f"Loaded {meta.row_count:,} rows (Delta v{meta.version}).",
            state="complete",
            expanded=False,
        )

    last_mod = meta.last_modified.strftime("%Y-%m-%d %H:%M UTC") if meta.last_modified else "unknown"
    st.caption(
        f"🛰️ Shipment data **as of {last_mod}** "
        f"· Delta version **v{meta.version}** "
        f"· **{meta.row_count:,}** rows"
    )

    # ── HTST-only raw download button ─────────────────────────────────────────
    # st.download_button streams over HTTP, not the WebSocket, so payload size
    # is not a client-side constraint — but building the payload is a server-
    # side cost, so it is deferred behind a "Prepare" click (see
    # _lazy_csv_download).  Keyed on meta.cache_key, i.e. the Delta version,
    # so a lakehouse refresh invalidates a previously prepared file.
    # Whitespace-stripped membership test, matching _filter_to_htst's own
    # rename(columns=str.strip) — the build callback runs later (on click),
    # so the precondition has to be checked here rather than caught there.
    if "PRODUCTGROUP" not in {str(c).strip() for c in df.columns}:
        st.warning(
            "HTST-only download unavailable: the lakehouse table is missing "
            "the 'PRODUCTGROUP' column."
        )
    else:
        today = datetime.now().strftime("%Y%m%d")
        _lazy_csv_download(
            label="HTST-Only Shipment Data (CSV)",
            key="htst_dataflow_raw_download",
            file_name=f"HTST_Shipment_{today}.csv",
            identity=meta.cache_key,
            build=lambda: _htst_filtered_csv_bytes(df),
            help_text=(
                "HTST-filtered shipment table from the lakehouse, before any "
                f"lookup merges or enrichment ({len(df.columns)} analysis "
                "columns — see ANALYSIS_COLUMNS in data_sources/htst_shipment.py; "
                "the full-width table lives in the lakehouse itself)."
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


def _multiselect_filter(
    df: pd.DataFrame, col: str, label: str, key_salt: str, help_text: str,
) -> pd.DataFrame:
    """Apply one search-friendly multiselect over *col* (empty pick → no rows).

    The widget key embeds *key_salt* (a hash of upstream selections) so Streamlit
    auto-resets it to the full default whenever an upstream filter changes."""
    if col not in df.columns:
        return df
    opts = sorted(df[col].dropna().astype(str).unique().tolist())
    sel = st.multiselect(
        label, options=opts, default=opts,
        key=f"htst_filter_{col}_{_widget_key(key_salt)}", help=help_text,
    )
    return df[df[col].astype(str).isin(sel)] if sel else df.iloc[0:0]


def _render_filters(df: pd.DataFrame) -> pd.DataFrame:
    """Render the Customer · Format · Product Description filters (no date slicer).

    Time is handled by the fixed L12M/L6M/L3M trailing windows downstream, so
    this function only narrows *which* shipments the dashboard covers.  Each
    filter cascades into the next (options narrow to the prior selection) and
    auto-resets when an upstream pick changes.
    """
    st.markdown("### 🔍 Filter")
    c1, c2, c3 = st.columns(3)
    with c1:
        d1 = _multiselect_filter(
            df, "Customer", "Customer", "",
            "Select one or more customers. Format & Product options narrow automatically.")
    with c2:
        d2 = _multiselect_filter(
            d1, "Format", "Format", str(sorted(d1["Customer"].dropna().astype(str).unique())),
            "Pack format from Demantra (Product Size + Unit Pkg Type).")
    with c3:
        salt = str(sorted(d2.get("Format", pd.Series(dtype=str)).dropna().astype(str).unique()))
        d3 = _multiselect_filter(
            d2, "PRODUCTDESC", "Product Description", salt,
            "Narrows to the selected Customer × Format.")
    st.caption(f"**{len(d3):,}** shipment rows match the current filter.")
    return d3


def _render_sop_panel(bundle: Optional[HTSTLookupBundle], from_lakehouse: bool) -> None:
    """Pinned Standard-Operating-Procedure panel: what the source files are, where
    they live in Fabric, and the download → refresh → re-upload loop."""
    with st.expander("📋 Source Data & Refresh SOP", expanded=False):
        st.markdown(
            "This dashboard reads **everything from the Pricing Lakehouse** — no "
            "uploads once Microsoft Fabric is connected.\n\n"
            "**Enrichment lookups** (Plant Tracker · Ship-Route Mileage · Demantra "
            "Item Master · Delivered-vs-FOB Pricing) live in "
            f"[`Files/Activity_Model/Shipment Report`]({FABRIC_SHIPMENT_REPORT_URL}). "
            "To refresh them each cycle:\n"
            "1. **Download** the current file from its source system.\n"
            "2. **Refresh** the data (add the latest month).\n"
            "3. **Re-upload** to the **same folder**, keeping the naming convention "
            "(e.g. `Demantra_MMDDYYYY.csv`) — the app auto-selects the newest by date "
            "stamp, so no code change is needed.\n\n"
            "**Fee / bracket tables** (Sell-to, Custom-label, Pallet, Delivery) live in "
            "`Files/Activity_Model/` and are read the same way."
        )
        if from_lakehouse and bundle is not None and bundle.files:
            st.markdown("**Files currently in use:**")
            st.dataframe(
                pd.DataFrame([
                    {"Lookup": m.label, "File": m.name,
                     "Folder": f"Files/{m.folder}",
                     "Last modified": (m.last_modified or "")[:19]}
                    for m in bundle.files
                ]),
                use_container_width=True, hide_index=True,
            )
        else:
            st.warning(
                "Lakehouse lookups are unavailable in this session — use the manual "
                "upload panel below (typical on a headless Streamlit Cloud server)."
            )


def _render_formula_panel() -> None:
    """Explicit formula/fee reference, pinned above the dashboard for auditability."""
    with st.expander("🧮 How the metrics & fees are computed", expanded=False):
        st.markdown(
            f"- **Pallet%** = Ordered LBS ÷ (Total Each per Pallet × Unit Net Weight); "
            f"a row is **Full** at ≥ {_PALLET_FULL_ROW_MIN:.0%} of a pallet, else **Mixed** "
            f"→ Mixed pallets should incur the **Mixed Pallet Fee ($/gal)**.\n"
            f"- **Annualized volume** = Ordered Secondary QTY ÷ window-days × "
            f"{_ANNUALIZE_DAYS} (sell-to = all products; **custom-label** = non-DG only).\n"
            f"- **Drop size** = Σ Ordered LBS ÷ count of unique orders → **Drop tier**.\n"
            f"- **Travel distance** = route mileage (Sourcing Plant → Ship-To) → "
            f"**Mileage tier**.\n"
            f"- **Delivery charge ($/gal)** = table[(Mileage tier, Drop tier)]; forced to "
            f"**$0 when FOB** (Pricing Method 0).\n"
            f"- **Sell-to / Custom-label fee ($/gal)** = bracket of the annualized volume.\n"
            f"- **Total activity fee ($/gal)** = Sell-to + Custom-label + Delivery + "
            f"Mixed-pallet — what a customer *should* be charged for how they order."
        )


# Polished chart palette — professional, presentation-ready.
_DEEPDIVE_BAR: str = "#2c6e9c"       # bars (blue)
_DEEPDIVE_BAR_HL: str = "#14476b"    # L3M bar highlighted (darker)
_DEEPDIVE_LINE: str = "#137d78"      # pallet-share line (teal)
_DEEPDIVE_WINDOW_ORDER: tuple[str, ...] = ("L12M", "L6M", "L3M")


def _deepdive_axis(v: Optional[float], unit: str) -> Optional[float]:
    """Chart y-value for a metric (share → percent points; else raw)."""
    if v is None or pd.isna(v):
        return None
    return v * 100.0 if unit == "%" else v


def _deepdive_label(v: Optional[float], unit: str) -> str:
    """Compact on-chart data label."""
    if v is None or pd.isna(v):
        return ""
    if unit == "%":
        return f"{v * 100:.1f}%"
    if unit == "gal":
        return f"{v / 1e6:.1f}M" if abs(v) >= 1e6 else f"{v / 1e3:.0f}K" if abs(v) >= 1e3 else f"{v:,.0f}"
    return f"{v:,.0f}"


def _render_deepdive_metric_chart(customer: str, metric: dict) -> None:
    """One polished L12M→L6M→L3M chart for a moved metric (bar or pallet line)."""
    order = [lbl for lbl in _DEEPDIVE_WINDOW_ORDER if lbl in metric["values"]]
    unit = metric["unit"]
    ys = [_deepdive_axis(metric["values"][lbl], unit) for lbl in order]
    labels = [_deepdive_label(metric["values"][lbl], unit) for lbl in order]
    if metric["chart"] == "line":
        fig = go.Figure(go.Scatter(
            x=order, y=ys, mode="lines+markers+text", text=labels,
            textposition="top center", textfont=dict(size=12, color=_DEEPDIVE_LINE),
            line=dict(color=_DEEPDIVE_LINE, width=3),
            marker=dict(size=11, color=_DEEPDIVE_LINE, line=dict(color="white", width=1.5)),
            hovertemplate="%{x}: %{text}<extra></extra>"))
        yaxis = dict(ticksuffix="%", rangemode="tozero", showgrid=True, gridcolor="#eee")
    else:
        colors = [_DEEPDIVE_BAR] * len(order)
        if order:
            colors[-1] = _DEEPDIVE_BAR_HL      # emphasise the latest quarter
        fig = go.Figure(go.Bar(
            x=order, y=ys, text=labels, textposition="outside",
            textfont=dict(size=12, color="#374151"), marker_color=colors,
            cliponaxis=False, hovertemplate="%{x}: %{text}<extra></extra>"))
        yaxis = dict(rangemode="tozero", showgrid=True, gridcolor="#eee", showticklabels=False)
    fig.update_layout(
        title=dict(text=metric["label"], font=dict(size=13, color="#111827")),
        height=250, margin=dict(l=8, r=8, t=44, b=8), showlegend=False,
        plot_bgcolor="white", font=dict(color="#374151", size=12),
        xaxis=dict(tickfont=dict(size=12)), yaxis=yaxis, bargap=0.35,
    )
    st.plotly_chart(fig, use_container_width=True,
                    key=f"dd_{_widget_key(customer)}_{metric['key']}")


def _bh_fee_money(v: float) -> str:
    return f"${v:,.3f}"


def _render_deepdive_fee_table(dd: dict) -> None:
    """Storytelling fee bridge: each component before → after → Δ, summing to the
    total activity-fee delta and the annualized $ impact."""
    def _row(name, before, after, delta, bold=False):
        dcol = "#c0392b" if delta > 1e-9 else "#137d78" if delta < -1e-9 else "#6b7280"
        nm = f"<b>{name}</b>" if bold else name
        tr_style = " style='border-top:1px solid #d1d5db'" if bold else ""
        sign = "+" if delta >= 0 else "−"
        return (
            f"<tr{tr_style}>"
            f"<td style='text-align:left;padding:2px 10px'>{nm}</td>"
            f"<td style='text-align:right;padding:2px 10px'>{_bh_fee_money(before)}</td>"
            f"<td style='text-align:right;padding:2px 10px'>{_bh_fee_money(after)}</td>"
            f"<td style='text-align:right;padding:2px 10px;color:{dcol}'>"
            f"{sign}${abs(delta):,.3f}</td></tr>"
        )
    head = ("<tr><th style='text-align:left;padding:2px 10px'>Activity fee ($/gal)</th>"
            "<th style='text-align:right;padding:2px 10px'>Before (L12M)</th>"
            "<th style='text-align:right;padding:2px 10px'>After (L3M)</th>"
            "<th style='text-align:right;padding:2px 10px'>Δ</th></tr>")
    body = "".join(_row(c["name"], c["before"], c["after"], c["delta"]) for c in dd["components"])
    body += _row("Total activity fee", dd["total_before"], dd["total_after"], dd["total_delta"], bold=True)
    st.markdown(
        f"<table style='font-size:0.9rem;border-collapse:collapse'>{head}{body}</table>",
        unsafe_allow_html=True)
    st.markdown(
        f"<div style='margin-top:8px;font-size:0.95rem'>Annualized volume "
        f"<b>{dd['annual_volume']:,.0f} gal</b> × <b>+${dd['total_delta']:,.3f}/gal</b> = "
        f"<b style='color:#c0392b'>${dd['annual_impact']:,.0f} / yr</b> "
        f"(≈ ${dd['monthly_impact']:,.0f} / mo) of activity-fee recovery if repriced.</div>",
        unsafe_allow_html=True)


def _render_customer_deepdive(deepdives: list[dict]) -> None:
    """One foldable section per requote-candidate customer, ranked by $ impact —
    the storytelling fee bridge + polished charts sales can show the customer."""
    st.markdown("### 🧲 Customer deep-dive — requote opportunities")
    st.caption(
        "One section per customer whose activity-based fee has risen enough to "
        "matter (> **$1k / month** impact), ranked by annualized $ impact.  Open a "
        "customer for the fee bridge and the charts you can show them to explain "
        "*why* the rate should change."
    )
    if not deepdives:
        st.success(
            "✅ No customers cross the $1k/month activity-fee impact threshold this "
            "quarter under the current filter."
        )
        return
    for dd in deepdives:
        title = (f"{dd['customer']}  —  ${dd['annual_impact']:,.0f}/yr impact "
                 f"(≈ ${dd['monthly_impact']:,.0f}/mo)")
        with st.expander(title, expanded=False):
            _render_deepdive_fee_table(dd)
            if dd["metrics"]:
                st.markdown(
                    "**What changed** — L12M → L6M → L3M (only metrics that moved):")
                for i in range(0, len(dd["metrics"]), 2):
                    for col, metric in zip(st.columns(2), dd["metrics"][i:i + 2]):
                        with col:
                            _render_deepdive_metric_chart(dd["customer"], metric)
            else:
                st.caption(
                    "_Fee moved via a bracket boundary; the underlying metrics were "
                    "otherwise stable._")


def _render_requote(requote_df: pd.DataFrame) -> None:
    """Requote-candidate table — sites whose activity fee rose L12M → L3M."""
    st.markdown("### 🎯 Requote candidates — activity fee rose L12M → L3M")
    st.caption(
        "Sites whose activity-based **$/gal** increased as behaviour drifted across "
        "a fee bracket (pallet → Mixed, smaller drops, lower volume, longer haul).  "
        "Ranked by **$ Impact = Δ fee × L3M annualized volume** — the top rows "
        "recover the most margin if repriced."
    )
    if requote_df.empty:
        st.success("✅ No sites crossed into a higher activity-fee bracket this quarter.")
        return
    show = requote_df.copy()
    money_cols = [c for c in show.columns if c.startswith("Δ ") or "Total Activity Fee" in c]
    for c in money_cols:
        show[c] = pd.to_numeric(show[c], errors="coerce").map(lambda v: f"${v:,.3f}" if pd.notna(v) else "—")
    if "Annualized Volume (Gal, L3M)" in show:
        show["Annualized Volume (Gal, L3M)"] = pd.to_numeric(
            show["Annualized Volume (Gal, L3M)"], errors="coerce").map(lambda v: f"{v:,.0f}")
    if "$ Impact (Δfee × L3M vol)" in show:
        show["$ Impact (Δfee × L3M vol)"] = pd.to_numeric(
            show["$ Impact (Δfee × L3M vol)"], errors="coerce").map(lambda v: f"${v:,.0f}")
    st.dataframe(show, use_container_width=True, hide_index=True)
    today = datetime.now().strftime("%Y%m%d")
    st.download_button(
        "⬇️ Download Requote Candidates (CSV)", data=_to_csv_bytes(requote_df),
        file_name=f"HTST_Requote_Candidates_{today}.csv", mime="text/csv",
        key="htst_dl_requote",
    )


def _render_customer_site_details(
    filtered_df: pd.DataFrame,
    windows: dict,
    pallet_fee_df: Optional[pd.DataFrame],
    sell_to_df: Optional[pd.DataFrame],
    custom_label_df: Optional[pd.DataFrame],
    delivery_df: Optional[pd.DataFrame],
) -> None:
    """Full L3M activity detail (Customer × Ship-To × Product) — foldable + CSV."""
    with st.expander("📊 Customer-Site Details (L3M) — full activity & fee table", expanded=False):
        if not windows or "L3M" not in windows:
            st.info("No trailing windows available.")
            return
        l3 = _window_slice(filtered_df, windows["L3M"])
        summary = _build_customer_site_summary(
            l3, windows["L3M"][2], pallet_fee_df, sell_to_df, custom_label_df, delivery_df)
        if summary.empty:
            st.info("No data matches the current filters.")
            return
        st.caption(
            "Grain: Customer × Ship-To × Product (L3M window).  Annualized Gallons "
            f"= QTY ÷ {windows['L3M'][2]} days × {_ANNUALIZE_DAYS}."
        )
        st.dataframe(summary, use_container_width=True, hide_index=True)
        today = datetime.now().strftime("%Y%m%d")
        st.download_button(
            "⬇️ Download Customer-Site Summary (CSV)", data=_to_csv_bytes(summary),
            file_name=f"HTST_CustomerSite_Summary_{today}.csv", mime="text/csv",
            key="htst_download_summary",
        )


def _render_output_section(filtered_df: pd.DataFrame) -> None:
    """Enriched Shipment Report — demoted to a foldable panel + CSV (HTTP stream)."""
    with st.expander("📋 Enriched Shipment Report (row-level) — download / preview", expanded=False):
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Filtered Rows", f"{len(filtered_df):,}")
        m2.metric("Total Columns", f"{len(filtered_df.columns)}")
        n_no_mileage = int((filtered_df["Mileage"] == "n/a").sum()) if "Mileage" in filtered_df.columns else 0
        m3.metric("Rows with n/a Mileage", f"{n_no_mileage:,}")
        n_no_plant = int(filtered_df["Sourcing Plant"].isna().sum()) if "Sourcing Plant" in filtered_df.columns else 0
        m4.metric("Rows with Unmatched Plant", f"{n_no_plant:,}")
        today = datetime.now().strftime("%Y%m%d")
        # Deferred: this frame is ~436 K rows, and an inline download_button
        # would re-encode all of it on every rerun of the page.
        _lazy_csv_download(
            label="Full Enriched Report (CSV)",
            key="htst_download_output",
            file_name=f"HTST_Shipment_Enriched_{today}.csv",
            identity=_frame_identity(filtered_df),
            build=lambda: _to_csv_bytes(filtered_df),
            help_text=(
                "Row-level enriched report for the current filter selection. "
                "Change a filter and you'll be asked to prepare it again, so "
                "the file always matches what's on screen."
            ),
        )
        st.caption(
            f"Preview: first {min(_PREVIEW_ROWS, len(filtered_df)):,} of "
            f"{len(filtered_df):,} rows. Use the download button for the full dataset."
        )
        st.dataframe(filtered_df.head(_PREVIEW_ROWS), use_container_width=True, hide_index=True)


# ── 7. Entry point ────────────────────────────────────────────────────────────

_REQUIRED_LOOKUP_KEYS: tuple[str, ...] = (
    "plant_tracker", "mileage_tracker", "demantra", "pricing_tracker",
)


def render() -> None:
    """Render the Shipment Monitor & HTST Requote dashboard.

    Once Microsoft Fabric is connected, both the shipment Delta table and every
    lookup are read straight from the lakehouse — **no uploads, no second
    refresh**.  Only a headless Streamlit Cloud session (no interactive Azure
    sign-in) falls back to the manual upload panel.  The enrichment pipeline is
    cached in session_state keyed by the shipment snapshot + lookup file
    identities, so it re-runs only when a source actually changes.  Time is
    handled by fixed L12M/L6M/L3M trailing windows, not a date slicer.
    """
    apply_custom_css()
    st.markdown(
        '<h1 class="main-header">Shipment Monitor &amp; HTST Requote</h1>',
        unsafe_allow_html=True,
    )

    # ── Sources: shipment (Delta) + lookups (Files), both auto from Fabric ───
    source_result = _render_shipment_source()
    shipment_df = source_result.df
    shipment_sig = source_result.signature
    use_upload_fallback = shipment_df is None

    lookup_bundle: Optional[HTSTLookupBundle] = None
    lookup_error: Optional[str] = None
    if not use_upload_fallback:
        try:
            lookup_bundle = fetch_htst_lookups()
        except HTSTLookupError as exc:
            lookup_error = str(exc)
    need_lookup_upload = lookup_bundle is None

    _render_sop_panel(lookup_bundle, from_lakehouse=not need_lookup_upload)
    _render_formula_panel()
    st.markdown("---")

    # ── Upload fallback (headless Cloud) ─────────────────────────────────────
    if use_upload_fallback:
        st.warning(
            "⚠️ Could not read the HTST Shipment Report from the Pricing Lakehouse "
            "— falling back to manual upload.  If Microsoft Fabric is not signed "
            "in, visit **Home & Fabric Sign-in** in the sidebar, then return."
        )
        with st.expander("Why did the lakehouse pull fail?", expanded=False):
            st.code(source_result.error or "Unknown error.", language="text")
    elif need_lookup_upload:
        st.warning(
            "⚠️ Shipment loaded, but the lakehouse **lookup** tables could not be "
            f"read — upload them below.\n\n{lookup_error or ''}"
        )

    detected: dict[str, object] = {}
    if use_upload_fallback or need_lookup_upload:
        detected = _render_upload_section(include_shipment=use_upload_fallback)
        st.markdown("---")

    if use_upload_fallback:
        shipment_file = detected.get("shipment")
        if shipment_file is not None:
            try:
                shipment_df = pd.read_csv(shipment_file, low_memory=False)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not read uploaded HTST Shipment Report: {exc}")
                return
            shipment_sig = f"upload:{shipment_file.name}:{shipment_file.size}"

    # ── Assemble the lookup DataFrames + fee tables + a cache signature ──────
    if not need_lookup_upload and lookup_bundle is not None:
        enrich_lookups: Optional[dict[str, pd.DataFrame]] = {
            k: lookup_bundle.frames[k] for k in _REQUIRED_LOOKUP_KEYS
        }
        pallet_fee_df = lookup_bundle.get("pallet_fee")
        sell_to_df = lookup_bundle.get("sell_to")
        custom_label_df = lookup_bundle.get("custom_label")
        delivery_df = lookup_bundle.get("delivery")
        lookup_sig = "||".join(f"{m.name}:{m.last_modified}" for m in lookup_bundle.files)
    else:
        missing = [p.label for p in _FILE_PATTERNS if p.required and detected.get(p.key) is None]
        if use_upload_fallback and shipment_df is None:
            missing.append(_SHIPMENT_PATTERN.label)
        if missing:
            st.info("⏳ Waiting for the following required file(s):\n"
                    + "\n".join(f"  • {label}" for label in missing))
            return
        enrich_lookups = None  # read lazily on cache miss (below)
        pallet_fee_df = _load_optional_lookup(detected.get("pallet_fee"), "Pallet Fee file")
        sell_to_df = _load_optional_lookup(detected.get("sell_to"), "Sell-To Volume Bracket file")
        custom_label_df = _load_optional_lookup(detected.get("custom_label"), "Custom Label Volume Bracket file")
        delivery_df = None
        lookup_sig = "".join(
            f"{detected[k].name}:{detected[k].size}" for k in _REQUIRED_LOOKUP_KEYS)

    if shipment_df is None:
        st.info("⏳ Waiting for the HTST Shipment Report.")
        return

    # ── Enrichment pipeline (session-state cached by source identities) ──────
    file_sig = hashlib.md5(f"{shipment_sig}||{lookup_sig}".encode()).hexdigest()
    if st.session_state.get("_htst_file_sig") != file_sig:
        if enrich_lookups is None:      # upload mode — materialise the 4 CSVs now
            try:
                enrich_lookups = {
                    k: pd.read_csv(detected[k], low_memory=False) for k in _REQUIRED_LOOKUP_KEYS}
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not read an uploaded lookup CSV: {exc}")
                return
        with st.spinner("Processing — enriching shipment data…"):
            enriched_df = _process_shipment_data(shipment_df, enrich_lookups)
        if enriched_df is None:
            return  # st.error already raised inside _process_shipment_data
        st.session_state["_htst_enriched_df"] = enriched_df
        st.session_state["_htst_file_sig"] = file_sig

    enriched_df = st.session_state.get("_htst_enriched_df")
    if enriched_df is None:
        st.error("Processed data unavailable — refresh the lakehouse pull and retry.")
        return

    st.success(
        f"✅ Ready — **{len(enriched_df):,} rows** enriched.  Trailing-window "
        "metrics update instantly with the filters below."
    )
    st.markdown("---")

    # ── Filters → trailing windows → dashboard ───────────────────────────────
    filtered_df = _render_filters(enriched_df)
    windows = _trailing_windows(enriched_df["Order Date"]) if "Order Date" in enriched_df.columns else {}
    st.markdown("---")

    _render_customer_deepdive(_build_customer_deepdive(
        filtered_df, windows, pallet_fee_df, sell_to_df, custom_label_df, delivery_df))
    st.markdown("---")
    _render_requote(_build_requote(
        filtered_df, windows, pallet_fee_df, sell_to_df, custom_label_df, delivery_df))
    st.markdown("---")
    _render_customer_site_details(
        filtered_df, windows, pallet_fee_df, sell_to_df, custom_label_df, delivery_df)
    _render_output_section(filtered_df)
