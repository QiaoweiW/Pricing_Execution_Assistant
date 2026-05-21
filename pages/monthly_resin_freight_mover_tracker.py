"""
Monthly Milk, Resin & Freight Movers — self-contained section for the
Market Barometer page.

Sections
--------
1. Constants                 (SS_PREFIX, role keywords, schema for the
                              editable Movers Non-Milk Tracker, resin-
                              calculator column names)
2. Parsing helpers           (_parse_month, _parse_money, _parse_percent,
                              _parse_gallons_cell, _normalize, _to_csv_bytes)
3. File ingestion            (_detect_role, _load_uploaded_files, _file_sig,
                              REQUIRED_ROLES)
4. Chart builders            (_build_packaging_index_chart,
                              _build_milk_commodity_chart)
5. Editable Movers           (_seed_movers_non_milk_df,
                              _ensure_movers_non_milk_state,
                              _render_movers_non_milk_editor)
6. Calculations              (FG generation: _build_resincalculate,
                              _build_tracker_rows_for_nmt,
                              _build_effective_resin_cost_tracker,
                              _build_resin_mover_fg_from_tracker,
                              _build_two_resin_mover_fgs;
                              Tag → tracker-column resolution helpers;
                              mover_details_table: _build_mover_details_table_no_milk;
                              Milk pipeline: _milk_rate_lookup_for_month,
                              _build_milk_usage_with_movers,
                              _layer_milk_on_mover_details_table;
                              Example-prices enrichment;
                              Top-level: _compute_all_outputs)
7. UI fragments              (intro, upload, milk-commodity chart + slicer,
                              packaging chart, freight outlook,
                              editable table + single Refresh button,
                              mover downloads, results, state clearers)
8. Public API                (render_monthly_resin_freight_mover_tracker)

Design notes
------------
Isolation
  Every key this fragment writes into ``st.session_state`` is namespaced under
  ``_SS_PREFIX`` so it never collides with other Market Barometer sections.
  The public entry point is a ``@st.fragment`` so uploads, edits, and the
  Refresh button only rerun this block.

Single-trigger model (Refresh)
  The May-2026-late contract collapses the previous Refresh / Confirm
  split into a single **🔄 Refresh** button.  One click runs the whole
  impact pipeline AND every dependent OneLake write under that write's
  own gate:

  * ``Resin_Cost_Tracker.csv`` — upsert for every ``(Month, Side)``
    key present in the editable NMT × Resin_Calculator payload.
    Months in the file but absent from the NMT (e.g. Sep–Nov 2025) are
    PRESERVED verbatim.
  * ``rest_htst_resin_mover_fg.csv`` / ``topco_resin_mover_fg.csv`` —
    regenerated from the persisted tracker, per-side
    ``latest_month_for_side`` (New) and ``latest − 1 calendar month``
    (Old).
  * ``milk_mover.csv``, ``Movers_Non_Milk_Tracker.csv`` — authoritative
    replace, no month gate.
  * ``Product_Milk Base Cost.csv`` — overwrite ``Base Milk Cost per
    Gallon`` by Item match; gated on ``End Month ≥ file's max Month +
    1 calendar month``.
  * ``mover_details_table.csv`` — upsert the editing month; gated on
    "a new row has been INSERTED into the editable NMT since the last
    successful publish" (row-count delta).  Existing month data is
    OVERWRITTEN when the gate is open.  Edit-in-place on the last row
    does NOT qualify.
  * ``base_milk_cost_monthly_tracker.csv`` — upsert the slicer's End
    Month; gated on ``End Month = Start Month + 1 calendar month``.

  See :func:`_render_monthly_sop_and_upload_intro` for the
  operator-facing workflow expander.

Robustness
  * Files are matched by filename keyword — exact dated filenames are not
    required, so any vintage of these CSVs (today's "_20260501.csv" or next
    month's "_20260601.csv") classifies correctly.
  * "Current month" is derived from ``date.today()`` at render time so the
    section advances month-over-month without code changes.
  * The ``Movers_Non_Milk_Tracker`` schema is hard-coded (the user does NOT
    upload it). Historical rows from the example file are seeded into
    ``session_state`` on first render; subsequent edits persist in-memory
    until the user clicks **Change files**.
  * Dollar / percent / comma-separated gallon cells are parsed via dedicated
    helpers that strip whitespace and symbols before casting.

Calculation contract (matches the May-2026 product spec)
  1. Two FG runs of the resin-cost pipeline, one for each "$/lbs" mover
     in the editable tracker's last row::

         rest_htst_resin_mover_fg  ← Rest HTST Resin Cost ($/lbs)
         topco_resin_mover_fg      ← TOPCO HTST Resin Cost ($/lbs)

     Each FG has columns
         Product ID | Product Description | Resin |
         Old Resin Cost ($/Gal) | New Resin Cost ($/Gal) | Resin Mover ($/Gal)
     where both ``Old`` and ``New`` are joined from the lakehouse
     ``Resin_Cost_Tracker.csv`` filtered on the matching Side
     (``Rest`` for ``rest_htst_resin_mover_fg``, ``TOPCO`` for
     ``topco_resin_mover_fg``).  The two anchor months are derived
     **per-side from the tracker** (May-2026-late contract):

         new_month_<side> = max(Month where Side == <side>)  in tracker
         old_month_<side> = new_month_<side> − 1 calendar month

     The subtraction is strict — a missing ``old_month_<side>`` row
     in the tracker surfaces an actionable warning rather than
     silently borrowing from another month.  Each FG is exposed as a
     CSV download.  No monthly-gallons / monthly-impact columns live
     here — those live on the mover_details_table.

  2. mover_details_table = ONE copy of ``site_item_volume`` with the
     following columns appended (in this order):

         Monthly Freight Mover  = Monthly Gallons × Pricing Method × Freight Mover $/Gal
         Monthly Resin Mover    = Monthly Gallons × Resin Mover $/Gal
         Monthly Milk Mover     = Monthly Gallons × Milk Mover $/Gal
         Freight Mover $/Gal    ← tracker last row, Tag-matched freight column
         Resin Mover $/Gal      ← tracker last row, Tag-matched resin column
                                  (Pkg column for Costco HTST), with FG
                                  fallback by product-description match for
                                  Rest HTST / TOPCO tags.
         Milk Mover $/Gal       ← End Month Milk Cost − Start Month Milk Cost
                                  matched by item description (see §3).

     The three $/Gal driver columns sit grouped at the END of the file. Their
     row sums populate the three headline metrics — Monthly Freight Incremental
     Revenue vs. Last Month, Monthly Resin Incremental Revenue vs. Last Month,
     Monthly Milk Incremental Revenue vs. Last Month.

  3. Milk pipeline (driven by the Start/End Month time slicer above the
     Milk Commodity Cost chart):

         Start Month Milk Cost = (
                 Start Skim Rate         × Skim Usage
               + Start Butterfat Rate    × Butterfat Usage
               + Start Protein Rate      × Protein Usage          (May-2026)
               + Start Other Solids Rate × Other Solids Usage     (May-2026)
             ) × (1 + Milk Scrape%)
         End Month Milk Cost   = same formula with End-month rates.
         Milk Cost Mover $/Gal = End Month Milk Cost − Start Month Milk Cost.

     The two new terms drive the Cottage Cheese category (added May-2026).
     HTST/ESL items carry ``0`` for Protein Usage / Other Solids Usage in
     ``Milk_Usage_Stable``, so the formula collapses back to the legacy
     Skim+Butterfat shape for them — backward-compatible by construction.

     All four rates per (Category, Class) come from
     ``Milk_Mover_Tracker`` for the slicer-selected months. Milk Scrape%
     is the last-row ``Milk`` cell of ``Scrape_Tracker``. The result is
     joined onto the mover_details_table by Item Description ↔ PRODUCTDESC match and
     drives Monthly Milk Mover.

  4. Example prices (optional file) is enriched with Resin Mover $/EA
     (from ``rest_htst_resin_mover_fg`` by item description) and Freight
     Mover $/EA (= last row's Rest HTST Freight Mover ($/Gal)), plus the
     resulting Price Increase%.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


# Module-level logger.  Used for low-volume diagnostic events that
# should not surface in the UI but DO need to be discoverable in the
# Streamlit Cloud / OneLake support logs.  Warnings here are NOT a
# substitute for ``st.warning`` / ``st.caption`` — operator-facing
# messages go through Streamlit; this is purely for engineers.
logger = logging.getLogger(__name__)

# OneLake-backed store + USDA-PDF auto-update workflow for Milk_Mover_Tracker.
# These two modules replace the legacy ``Milk_Mover_Tracker.csv`` upload —
# the milk DataFrame is now read from a Microsoft Fabric Lakehouse blob at
# render time, and a separate orchestrator keeps it in sync with the USDA
# advanced-prices PDF. None of the downstream calculation code has been
# touched: only how the milk DataFrame enters this view changed.
from data_sources import fabric_auth as _fabric_auth
from data_sources import milk_mover_autoupdate as _milk_autoupdate
from data_sources import milk_mover_store as _milk_store
from data_sources import milk_usage_stable_store as _milk_usage_store
from data_sources import base_milk_cost_tracker_store as _base_milk_cost_tracker
from data_sources import resin_cost_tracker_store as _resin_store
from data_sources import mover_details_table_store as _mover_details_store
from data_sources import monthly_pricing_execution_store as _mpe_store
from data_sources import cola_program_tracker_store as _cola_store
from data_sources import product_milk_base_cost_store as _pmbc_store


# ── 1. Constants ──────────────────────────────────────────────────────────────

# Namespace for every key this fragment writes into st.session_state.
_SS_PREFIX = "mrfmt"

# SharePoint folder that holds the Monthly Movers source CSVs (linked from the
# upload panel — no hard-coded local paths leak into the UI).
_MONTHLY_MOVERS_SHAREPOINT_URL: str = (
    "https://darigold1com.sharepoint.com/:f:/r/sites/BrandedPricing/"
    "Shared%20Documents/General/02%20Resources/"
    "Streamlit%20Folders%20(DO%20NOT%20DELETE)/"
    "Monthly%20Resin%20%26%20Freight?csf=1&web=1&e=mXUs7H"
)

# Breakthrough Fuel landing page used as the freight outlook source link.
# Hard-coded once here so the caption render stays declarative.
_BREAKTHROUGH_FUEL_URL: str = (
    "https://www.breakthroughfuel.com/"
    "?utm_source=google&utm_medium=cpc&utm_campaign=Branded"
    "&utm_term=breakthrough%20fuel&gad_source=1&gad_campaignid=16627177566"
    "&gclid=Cj0KCQjwh-HPBhCIARIsAC0p3ceH1CH07ikD9MGFfpnRvUk6SVSn7SfQ_wazy9uqJFBkVta6RpITZOAaAmgIEALw_wcB"
)

# Role → filename keyword mapping (case-insensitive, evaluated in order so
# more specific keys win — e.g. "site_item_volume" must be tested before any
# shorter key that could absorb it).  The legacy ``milk_mover_tracker``,
# ``milk_usage_stable``, ``resin_calculator`` and ``resin_cost_tracker``
# roles are intentionally absent: those DataFrames now come from Microsoft
# Fabric Lakehouse blobs (see ``data_sources/milk_mover_store.py``,
# ``data_sources/milk_usage_stable_store.py`` and
# ``data_sources/resin_cost_tracker_store.py``) and are injected into the
# uploads dict by ``render_monthly_resin_freight_mover_tracker``.
_ROLE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("site_item_volume",   ("site_item_volume",)),
    # Legacy filename ``Scrap_Tracker`` still classifies for backwards-compat.
    ("scrape_tracker",     ("scrape_tracker", "scrap_tracker")),
    ("packaging_index",    ("packaging_index",)),
    ("pkg_index",          ("pkg_index",)),
    ("example_prices",     ("example_prices",)),
]

# Roles that MUST be uploaded before the Refresh step can run.  Optional
# roles (packaging_index/pkg_index, example_prices) are accepted but
# skipped silently when absent.  ``milk_usage_stable``, ``milk_mover_tracker``,
# ``resin_calculator`` and ``resin_cost_tracker`` are sourced from Fabric
# and are not part of the upload contract.
REQUIRED_ROLES: tuple[str, ...] = (
    "site_item_volume",
    "scrape_tracker",
)

# ── Resin Calculator / Resin Cost Tracker canonical column names ─────────────
#
# Sourced from ``data_sources.resin_cost_tracker_store`` so the schema is
# defined in exactly ONE place — the store itself.  This page used to
# carry a parallel copy of these literals which had to be kept in lock-step
# manually; the aliases below preserve every existing call-site (``_COL_*``)
# while removing the duplication.
_COL_PRODUCT_ID   = _resin_store.COL_PRODUCT_ID
_COL_PRODUCT_DESC = _resin_store.COL_PRODUCT_DESC
_COL_RESIN        = _resin_store.COL_RESIN
_COL_RESIN_GAL    = _resin_store.COL_RESIN_GAL
_COL_MONTH        = _resin_store.COL_MONTH
_COL_PRICING_CAT  = _resin_store.COL_PRICING_CAT
_COL_USAGE_LBS    = _resin_store.COL_USAGE_LBS
_COL_GAL_EA       = _resin_store.COL_GAL_EA

# ── Movers_Non_Milk_Tracker schema (the editable in-app table) ───────────────
#
# The user does NOT upload this file — the schema lives in code so we can
# guarantee a stable column set regardless of source-file drift. Names are
# verbatim from the May-2026 example file in
# ``data/Market Barometer/Montly Movers/Movers_Non_Milk_Tracker_*.csv``.
_NMT_COL_MONTH         = "Month"
_NMT_COL_REST_RESIN    = "Rest HTST Resin Cost ($/lbs)"
_NMT_COL_REST_FREIGHT  = "Rest HTST Freight Mover ($/Gal)"
_NMT_COL_TOPCO_RESIN   = "TOPCO HTST Resin Cost ($/lbs)"
_NMT_COL_TOPCO_FREIGHT = "TOPCO HTST Freight Mover ($/Gal)"
_NMT_COL_WM_RESIN      = "Walmart HTST Resin Mover FG ($/Gal)"
_NMT_COL_WM_FREIGHT    = "Walmart HTST Freight Mover ($/Gal)"
_NMT_COL_CC_PKG        = "Costco HTST Pkg Mover FG ($/Gal)"
_NMT_COL_CC_FREIGHT    = "Costco HTST PNW Freight Mover ($/Gal)"
_NMT_COL_CCKS_RESIN    = "Costco KS Quarterly Resin Mover FG ($/Gal)"
_NMT_COL_CCKS_FREIGHT  = "Costco KS Quarterly PDX Freight Mover ($/Gal)"

# Numeric columns (everything except Month). Each is rendered as a NumberColumn
# in the editor and stored as float64.
_NMT_NUMERIC_COLUMNS: tuple[str, ...] = (
    _NMT_COL_REST_RESIN,    _NMT_COL_REST_FREIGHT,
    _NMT_COL_TOPCO_RESIN,   _NMT_COL_TOPCO_FREIGHT,
    _NMT_COL_WM_RESIN,      _NMT_COL_WM_FREIGHT,
    _NMT_COL_CC_PKG,        _NMT_COL_CC_FREIGHT,
    _NMT_COL_CCKS_RESIN,    _NMT_COL_CCKS_FREIGHT,
)

# Master ordered column list for the editable tracker.
_NMT_ALL_COLUMNS: tuple[str, ...] = (_NMT_COL_MONTH,) + _NMT_NUMERIC_COLUMNS

# Historical seed rows extracted from the May-2026 example file. New users
# see this populated on first render so they can immediately understand the
# table; from then on they can edit, append, or delete rows freely.
_NMT_SEED_ROWS: list[dict] = [
    {_NMT_COL_MONTH: pd.Timestamp(2025, 12, 1), _NMT_COL_REST_RESIN: 0.87},
    {_NMT_COL_MONTH: pd.Timestamp(2026,  1, 1), _NMT_COL_REST_RESIN: 0.87},
    {_NMT_COL_MONTH: pd.Timestamp(2026,  2, 1), _NMT_COL_REST_RESIN: 0.92},
    {_NMT_COL_MONTH: pd.Timestamp(2026,  3, 1), _NMT_COL_REST_RESIN: 0.92},
    {_NMT_COL_MONTH: pd.Timestamp(2026,  4, 1), _NMT_COL_REST_RESIN: 0.92},
    {
        _NMT_COL_MONTH:         pd.Timestamp(2026, 5, 1),
        _NMT_COL_REST_RESIN:    1.53,
        _NMT_COL_REST_FREIGHT:  0.0516,
        _NMT_COL_TOPCO_RESIN:   2.58,
        _NMT_COL_TOPCO_FREIGHT: 0.0791,
        _NMT_COL_WM_RESIN:      0.02,
        _NMT_COL_WM_FREIGHT:    0.02,
        _NMT_COL_CC_PKG:        0.02,
        _NMT_COL_CC_FREIGHT:    0.02,
        _NMT_COL_CCKS_RESIN:    0.0,
        _NMT_COL_CCKS_FREIGHT:  0.02,
    },
]


# ── 2. Parsing helpers ────────────────────────────────────────────────────────

def _parse_month(value) -> Optional[pd.Timestamp]:
    """Parse a date-ish value into the first day of its month as a Timestamp.

    Accepts pandas Timestamps, ``datetime``s, and common string formats
    (``M/D/YYYY``, ``YYYY-MM-DD``, …). Returns ``None`` on failure so callers
    can drop invalid rows without raising.
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
    """Parse a currency-ish cell like ``"$0.915 "`` or ``" - "`` into a float.

    Dashes, blanks, NaN and non-numeric strings collapse to ``None`` so
    arithmetic skips them rather than raising.
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
    """Parse a percent-ish cell like ``"1%"`` or ``"0.01"`` into a fraction."""
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


def _parse_gallons_cell(value) -> Optional[float]:
    """Parse Monthly/Annualized Gallons cells that may have commas or spaces."""
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


def _normalize_desc(value) -> str:
    """Normalise a product / item description for case-insensitive joins.

    Empty / NaN values map to the empty string so they never false-match.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip().lower()


def _strip_df_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Strip leading/trailing whitespace from every column header."""
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    return out


def _to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Serialise a DataFrame to UTF-8 CSV bytes for ``st.download_button``."""
    return df.to_csv(index=False).encode("utf-8")


# ── 3. File ingestion ─────────────────────────────────────────────────────────

def _detect_role(filename: str) -> Optional[str]:
    """Classify an uploaded CSV by filename keyword.

    Returns the role id (e.g. ``"site_item_volume"``) or ``None`` when no
    keyword matches. Tested in ``_ROLE_KEYWORDS`` order so more specific
    matches win over generic ones.
    """
    name = filename.lower()
    for role, keywords in _ROLE_KEYWORDS:
        if any(kw in name for kw in keywords):
            return role
    return None


def _file_sig(files: list) -> str:
    """Hash the (name, size) of each uploaded file so we re-parse only on change."""
    h = hashlib.md5()
    for f in sorted(files, key=lambda x: x.name):
        h.update(f.name.encode("utf-8"))
        h.update(str(getattr(f, "size", 0)).encode("utf-8"))
    return h.hexdigest()


@dataclass
class _Uploaded:
    """Parsed representation of one uploaded CSV.

    ``role`` is the classified role id; ``df`` is the raw DataFrame (no
    per-file cleaning happens here — calculation functions clean as needed).
    """
    role: str
    filename: str
    df: pd.DataFrame


def _load_uploaded_files(files: list) -> dict[str, _Uploaded]:
    """Read uploaded files into a ``role → _Uploaded`` dict.

    Unclassified files are silently skipped so stray junk doesn't block the
    pipeline. Read errors are surfaced via ``st.error`` but never raise.
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


# ── 3a. Milk Mover OneLake source ────────────────────────────────────────────
#
# The Milk Mover Tracker has been migrated from "user uploads a CSV" to
# "system reads a Microsoft Fabric Lakehouse blob, kept in sync with the USDA
# advanced-prices PDF." These helpers are the only seam between the new
# storage layer and the unchanged downstream pipeline — every other consumer
# of ``uploads["milk_mover_tracker"]`` keeps working byte-for-byte because
# ``read_milk_mover_df`` returns the exact same DataFrame shape the legacy
# CSV produced (Category, Month, Class, Skim Rate, Butterfat Rate).

# Session-state key under which we cache the latest auto-update result, so
# the status caption rendered under the milk slicer can show what happened
# without re-running the orchestrator on every rerun.
_SS_MILK_AUTOUPDATE_RESULT = "monthly_movers_milk_autoupdate_result"


def _load_milk_mover_uploaded_from_store() -> Optional[_Uploaded]:
    """Return an ``_Uploaded`` populated from the OneLake milk-mover store.

    Returns ``None`` when the store is empty AND the seed CSV is missing —
    at that point we have no rows to render and the milk chart degrades to
    a friendly empty-state message.
    """
    try:
        df = _milk_store.read_milk_mover_df()
    except _milk_store.MilkMoverStoreError as exc:
        # Surface a clean, single-line caption; the empty-state branch in
        # ``_render_milk_commodity`` will then render the connectivity
        # message rather than the chart.
        st.session_state[_SS_MILK_AUTOUPDATE_RESULT] = (
            _milk_autoupdate.AutoUpdateResult(
                checked_at=datetime.now(),
                errors=[f"OneLake read failed: {exc}"],
            )
        )
        return None
    if df is None or df.empty:
        return None
    return _Uploaded(
        role="milk_mover_tracker",
        filename=_milk_store.get_store_label(),
        df=df,
    )


def _run_milk_mover_autoupdate(*, force: bool = False) -> None:
    """Run the USDA-PDF auto-update once and cache the result.

    The orchestrator is itself TTL-guarded (default 1 h), so calling this on
    every rerun is cheap once we've checked recently. The cached result is
    rendered as a small status caption beneath the milk slicer.
    """
    try:
        result = _milk_autoupdate.maybe_update_from_pdfs(force=force)
    except Exception as exc:  # pragma: no cover — defensive only
        # Never let the auto-update path break the page render. The DB still
        # holds the last known good data; we just won't show new rows.
        result = _milk_autoupdate.AutoUpdateResult(
            checked_at=datetime.now(),
            errors=[f"unexpected error: {exc}"],
        )
    st.session_state[_SS_MILK_AUTOUPDATE_RESULT] = result


_SS_MILK_BOOTSTRAP_TRIED = "monthly_movers_milk_bootstrap_tried"

# True once the routine ("force=False") USDA-PDF orchestrator has executed
# in this Streamlit session.  The orchestrator is itself TTL-guarded
# (1-hour cooldown via the ``Milk_cost_tracker/fmmo_state.json`` blob) so
# subsequent calls are *cheap* logic-wise — but they still pay one OneLake
# round-trip for the state read on every Streamlit rerun.  Streamlit
# reruns the whole page on every widget interaction (slicer drags, button
# clicks, etc.), so even a "cheap" idempotent call quickly turns into the
# single biggest contributor to perceived page-load cost.  Guarding with
# this flag pins the orchestrator to "exactly once per session" for the
# default routine path, while the explicit "USDA refresh" button still
# bypasses it via ``force=True`` (and clears this flag so a later routine
# tick can re-fire if the user navigates away and returns).
_SS_MILK_AUTOUPDATE_TICK_RAN = "monthly_movers_milk_autoupdate_tick_ran"


def _inject_milk_mover_from_store(uploads: dict[str, _Uploaded]) -> None:
    """Populate ``uploads['milk_mover_tracker']`` from the OneLake store.

    Done in-place so callers don't need to thread a new dict through the
    rest of the section.  When the store reads back empty on a cold
    start (no local seed CSV, no prior auto-update run) we make ONE
    bootstrap attempt — invalidate the cache, force the USDA-PDF
    auto-updater, then re-read — before declaring the store empty.
    Subsequent renders skip the bootstrap (a session-state flag dedupes)
    so reactive widgets never trigger a redundant USDA round-trip.

    If the second read still returns empty the key is left absent so
    ``_render_milk_commodity`` renders the actionable empty-state branch
    (which exposes a manual "USDA refresh" button + a link to the seed
    location).
    """
    sourced = _load_milk_mover_uploaded_from_store()
    if sourced is not None:
        uploads["milk_mover_tracker"] = sourced
        return

    # Cold-start bootstrap: try ONCE per session to coax the auto-updater
    # into seeding the FMMO table from USDA.  Without this the page sits
    # stuck on the empty-state message until the user manually clicks
    # the "USDA refresh" button — a confusing first impression on a
    # fresh deployment with no local seed CSV checked in.
    already_tried = bool(st.session_state.get(_SS_MILK_BOOTSTRAP_TRIED))
    if not already_tried:
        st.session_state[_SS_MILK_BOOTSTRAP_TRIED] = True
        try:
            _milk_store.invalidate_read_cache()
        except Exception:  # noqa: BLE001 — non-fatal cache bust
            pass
        _run_milk_mover_autoupdate(force=True)
        try:
            _milk_store.invalidate_read_cache()
        except Exception:  # noqa: BLE001
            pass
        sourced = _load_milk_mover_uploaded_from_store()
        if sourced is not None:
            uploads["milk_mover_tracker"] = sourced
            return

    uploads.pop("milk_mover_tracker", None)


_SS_MILK_USAGE_SEED_DONE = "monthly_movers_milk_usage_seed_done"


def _load_milk_usage_stable_uploaded_from_store() -> Optional[_Uploaded]:
    """Return an ``_Uploaded`` populated from the OneLake Milk_Usage_Stable store.

    Returns ``None`` when the blob is empty AND the seed CSV is missing.
    Errors raised by the store are swallowed and surfaced through the
    auto-update result session-state slot, mirroring the milk-mover path
    so the page always renders a coherent caption.

    The ``seed_from_csv_if_empty()`` bootstrap is gated by
    :data:`_SS_MILK_USAGE_SEED_DONE` so the OneLake "is the blob empty?"
    probe fires at most once per session; the read itself is cheap but
    it's still a network round-trip on every Streamlit rerun without
    this guard.
    """
    try:
        if not st.session_state.get(_SS_MILK_USAGE_SEED_DONE):
            st.session_state[_SS_MILK_USAGE_SEED_DONE] = True
            # Cheap idempotent bootstrap — only writes on the first ever render
            # against an empty lakehouse; a no-op afterwards.
            _milk_usage_store.seed_from_csv_if_empty()
        df = _milk_usage_store.read_milk_usage_stable_df()
    except _milk_usage_store.MilkUsageStableStoreError as exc:
        st.session_state[_SS_MILK_AUTOUPDATE_RESULT] = (
            _milk_autoupdate.AutoUpdateResult(
                checked_at=datetime.now(),
                errors=[f"Milk_Usage_Stable OneLake read failed: {exc}"],
            )
        )
        return None
    if df is None or df.empty:
        return None
    return _Uploaded(
        role="milk_usage_stable",
        filename=_milk_usage_store.get_store_label(),
        df=df,
    )


def _inject_milk_usage_stable_from_store(uploads: dict[str, _Uploaded]) -> None:
    """Populate ``uploads['milk_usage_stable']`` from the OneLake store.

    Same shape as :func:`_inject_milk_mover_from_store` so the two
    Fabric-sourced inputs are wired the same way. The role key matches
    the value the legacy upload-classifier used so every downstream
    consumer (``_compute_milk_usage_for_render`` etc.) keeps working.
    """
    sourced = _load_milk_usage_stable_uploaded_from_store()
    if sourced is None:
        uploads.pop("milk_usage_stable", None)
        return
    uploads["milk_usage_stable"] = sourced


# ── 3b. Resin Calculator + Resin Cost Tracker OneLake source ─────────────────
#
# Both files used to be uploaded by the user on every visit; they are now
# resolved from a Fabric Lakehouse Files/ folder via
# ``data_sources/resin_cost_tracker_store.py``.  These helpers are the only
# seam between the new storage layer and the unchanged downstream pipeline:
# every consumer of ``uploads["resin_calculator"]`` / ``uploads["resin_cost_tracker"]``
# keeps working byte-for-byte because :func:`read_resin_*_df` returns the
# same column shape the legacy CSVs produced.

# Session-state slot used to surface lakehouse pull failures in the UI
# without crashing the rest of the page.
_SS_RESIN_STORE_ERROR = f"{_SS_PREFIX}_resin_store_error"


def _load_resin_uploaded_from_store(
    role: str,
    label_provider,
    reader,
) -> Optional[_Uploaded]:
    """Generic helper that materialises one resin blob into an ``_Uploaded``.

    Parameters
    ----------
    role
        Role key the downstream pipeline keys on (e.g. ``"resin_calculator"``).
    label_provider
        Zero-argument callable returning a short OneLake path label, used as
        the synthetic filename in the resulting ``_Uploaded`` so the page's
        download/audit captions stay informative.
    reader
        Zero-argument callable returning the raw DataFrame from the store.

    Returns ``None`` (and caches the error in session_state) on failure so
    callers can surface a single status caption rather than crashing.
    """
    try:
        df = reader()
    except _resin_store.ResinCostTrackerStoreError as exc:
        st.session_state[_SS_RESIN_STORE_ERROR] = (
            f"OneLake read failed for {role}: {exc}"
        )
        return None
    if df is None or df.empty:
        return None
    return _Uploaded(role=role, filename=label_provider(), df=df)


def _inject_resin_inputs_from_store(uploads: dict[str, _Uploaded]) -> None:
    """Populate ``uploads['resin_calculator']`` and ``uploads['resin_cost_tracker']``.

    Mirrors :func:`_inject_milk_mover_from_store` so the three Fabric-sourced
    inputs (milk mover, milk usage stable, resin pair) are wired the same
    way.  The role keys match the values the legacy upload-classifier used
    so every downstream consumer keeps working unchanged.
    """
    # Drop any stale error from a prior render — successful reads below will
    # leave the slot empty, otherwise the helper will repopulate it.
    st.session_state.pop(_SS_RESIN_STORE_ERROR, None)

    calc = _load_resin_uploaded_from_store(
        role="resin_calculator",
        label_provider=_resin_store.get_calculator_label,
        reader=_resin_store.read_resin_calculator_df,
    )
    if calc is None:
        uploads.pop("resin_calculator", None)
    else:
        uploads["resin_calculator"] = calc

    tracker = _load_resin_uploaded_from_store(
        role="resin_cost_tracker",
        label_provider=_resin_store.get_cost_tracker_label,
        reader=_resin_store.read_resin_cost_tracker_df,
    )
    if tracker is None:
        uploads.pop("resin_cost_tracker", None)
    else:
        uploads["resin_cost_tracker"] = tracker


def _render_resin_store_status() -> None:
    """Render a one-line caption describing the resin OneLake source.

    Surfaces any error captured in session_state so the user has a
    single, actionable place to look when the store is unreachable.
    """
    err = st.session_state.get(_SS_RESIN_STORE_ERROR)
    if err:
        st.caption(f"⚠️ {err}")
        return
    st.caption(
        "🧪 Resin Calculator & Cost Tracker sourced from the Fabric Lakehouse "
        f"(`{_resin_store.get_cost_tracker_label()}`)."
    )


# ── 4. Chart builder ──────────────────────────────────────────────────────────

def _build_packaging_index_chart(
    pkg_df: pd.DataFrame,
    *,
    start_time: Optional[pd.Timestamp] = None,
    end_time: Optional[pd.Timestamp] = None,
    selected_indices: Optional[list[str]] = None,
) -> go.Figure:
    """Multi-series time-series chart of the Packaging Index.

    Resin price series (HDPE, LDPE, PET, PP) plot on the primary y-axis in
    $/lb; Linerboard (if present) plots on a secondary y-axis in $/ton because
    it lives on a different scale.

    Parameters
    ----------
    pkg_df
        Raw packaging-index DataFrame; must contain a ``Time`` column.
    start_time, end_time
        Optional inclusive bounds on the ``Time`` axis. ``None`` means
        "no bound on that side" so the chart defaults to "all data".
    selected_indices
        Optional whitelist of column names to plot. ``None`` (or the
        empty list, after the caller normalises "All" to every column)
        means "plot every non-Time column". Unknown column names in the
        whitelist are silently skipped so a stale filter state never
        blanks the chart.
    """
    df = pkg_df.copy()
    if "Time" not in df.columns:
        return go.Figure()

    df["Time"] = pd.to_datetime(df["Time"], errors="coerce")
    df = df.dropna(subset=["Time"]).sort_values("Time")

    # Apply the time-slicer bounds BEFORE the column split so every
    # downstream trace automatically respects the operator's selection.
    if start_time is not None:
        df = df[df["Time"] >= pd.Timestamp(start_time)]
    if end_time is not None:
        df = df[df["Time"] <= pd.Timestamp(end_time)]
    if df.empty:
        return go.Figure()

    all_non_time = [c for c in df.columns if c != "Time"]
    # Normalise the whitelist. None / empty list → every column.
    if selected_indices:
        whitelist = {c for c in selected_indices if c in all_non_time}
        if not whitelist:
            return go.Figure()  # user deselected every index
    else:
        whitelist = set(all_non_time)

    resin_cols = [
        c for c in all_non_time
        if c in whitelist and "linerboard" not in c.lower()
    ]
    board_cols = [
        c for c in all_non_time
        if c in whitelist and "linerboard" in c.lower()
    ]

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

    layout_kwargs = dict(
        xaxis=dict(title="", showgrid=False, showline=True, linecolor="#e0e0e0"),
        yaxis=dict(title="Resin ($/lb)", showgrid=True, gridcolor="#f0f0f0",
                   showline=True, linecolor="#e0e0e0", rangemode="tozero"),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(l=50, r=50, t=30, b=80),
        height=360,
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


# Color map for the milk-commodity chart. Each (Category, Class) pair
# gets its own colour. The line style encodes the metric:
#
#   Skim         → solid line on the PRIMARY (left) $/cwt-scaled axis
#                  (Skim ≈ 0.08–0.15)
#   Butterfat    → dashed line on the SECONDARY (right) $/lb-scaled axis
#                  (Butterfat ≈ 1.4–3.0)
#   Protein      → dotted line on the SECONDARY axis (Cottage Cheese only)
#   Other Solids → dash-dot line on the SECONDARY axis (Cottage Cheese only)
#
# Cottage Cheese has no Skim component; its three metrics all sit on the
# secondary $/lb axis. Since Protein Rate and Other Solids Rate carry the
# SAME value for Cottage Cheese (both equal the Class II Nonfat Solids
# Price), those two lines overlap visually — both still appear in the
# legend so the user can confirm both are populated.
_MILK_COLOR_MAP: dict[tuple[str, str], str] = {
    ("HTST",           "I"):  "#1f77b4",
    ("HTST",           "II"): "#aec7e8",
    ("ESL",            "I"):  "#d62728",
    ("ESL",            "II"): "#ff9896",
    ("COTTAGE CHEESE", "II"): "#2ca02c",
}


def _mirror_cc_ii_skim_bfat_from_esl_ii(df: pd.DataFrame) -> pd.DataFrame:
    """Patch Cottage Cheese II rows so Skim/Butterfat mirror ESL II.

    Operates on the chart's working DataFrame (NOT the lakehouse) — for
    every CC II row whose Skim or Butterfat cell is null, we copy in
    the corresponding ESL II value for the same Month. Bfat-only or
    skim-only rows are repaired field-by-field so a row already half-
    populated still benefits.

    Pure function on a DataFrame copy — never mutates the input.
    """
    if df is None or df.empty:
        return df

    out = df.copy()
    skim_col = next(
        (c for c in out.columns if "skim" in c.lower() and "nonfat" not in c.lower()),
        None,
    )
    bf_col = next((c for c in out.columns if "butter" in c.lower()), None)
    if skim_col is None and bf_col is None:
        return out
    if "Category" not in out.columns or "Class" not in out.columns or "Month" not in out.columns:
        return out

    cat = out["Category"].astype(str).str.strip().str.upper()
    cls = out["Class"].astype(str).str.strip().str.upper()

    esl_mask = (cat == "ESL") & (cls.isin({"II", "CLASS II", "2", "CLASS 2"}))
    cc_mask  = (cat == "COTTAGE CHEESE") & cls.isin({"II", "CLASS II", "2", "CLASS 2"})
    if not esl_mask.any() or not cc_mask.any():
        return out

    esl_by_month: dict[pd.Timestamp, tuple[Optional[float], Optional[float]]] = {}
    esl_subset = out[esl_mask]
    for _, row in esl_subset.iterrows():
        try:
            month_key = pd.Timestamp(row["Month"]).normalize().replace(day=1)
        except Exception:  # noqa: BLE001 — defensive
            continue
        skim_val = pd.to_numeric(row[skim_col], errors="coerce") if skim_col else None
        bf_val   = pd.to_numeric(row[bf_col],   errors="coerce") if bf_col   else None
        esl_by_month[month_key] = (
            None if (skim_val is None or pd.isna(skim_val)) else float(skim_val),
            None if (bf_val   is None or pd.isna(bf_val))   else float(bf_val),
        )

    for idx in out[cc_mask].index:
        try:
            month_key = pd.Timestamp(out.at[idx, "Month"]).normalize().replace(day=1)
        except Exception:  # noqa: BLE001
            continue
        ref = esl_by_month.get(month_key)
        if ref is None:
            continue
        ref_skim, ref_bf = ref
        if skim_col is not None and ref_skim is not None:
            cur = pd.to_numeric(out.at[idx, skim_col], errors="coerce")
            if pd.isna(cur):
                out.at[idx, skim_col] = ref_skim
        if bf_col is not None and ref_bf is not None:
            cur = pd.to_numeric(out.at[idx, bf_col], errors="coerce")
            if pd.isna(cur):
                out.at[idx, bf_col] = ref_bf

    return out


def _milk_commodity_visible_slice(
    milk_df: pd.DataFrame,
    *,
    start_month: Optional[pd.Timestamp] = None,
    end_month: Optional[pd.Timestamp] = None,
    category_filter: Optional[str] = None,
    class_filter: Optional[str] = None,
) -> pd.DataFrame:
    """Return the FMMO rows the Milk Commodity Cost chart actually plots.

    The chart applies a fixed sequence of filters before drawing — slicer
    bounds, the CC II → ESL II Skim/Bfat mirror, and the chart-only
    Category / Class knobs.  This helper applies the SAME transformations
    so an "Export visible series" download surfaces exactly the data the
    operator sees on screen.

    Parameters mirror :func:`_build_milk_commodity_chart` — pass the
    live slicer values and the chart-only Category / Class filters
    (``None`` / ``"All"`` ⇒ no filter on that axis).

    Returns the canonical FMMO columns (``Category``, ``Month``,
    ``Class``, ``Skim Rate``, ``Butterfat Rate``, ``Protein Rate``,
    ``Other Solids Rate``) with ``Month`` rendered as a first-of-month
    ``YYYY-MM-DD`` string for spreadsheet-friendly sorting.
    """
    if milk_df is None or milk_df.empty:
        return pd.DataFrame()

    df = _strip_df_columns(milk_df).copy()
    if "Month" not in df.columns or "Category" not in df.columns or "Class" not in df.columns:
        return pd.DataFrame()

    # ── Month normalisation + slicer bounds ──────────────────────────────
    df["Month"] = pd.to_datetime(df["Month"], errors="coerce")
    df = df.dropna(subset=["Month"]).sort_values(["Month", "Category", "Class"])
    if start_month is not None:
        df = df[df["Month"] >= pd.Timestamp(start_month)]
    if end_month is not None:
        df = df[df["Month"] <= pd.Timestamp(end_month)]
    if df.empty:
        return pd.DataFrame()

    # ── Mirror Cottage Cheese II Skim/Bfat from ESL II ───────────────────
    # Same defence-in-depth as the chart builder — legacy rows with null
    # CC II skim/bfat get their values from the matching ESL II row.
    df = _mirror_cc_ii_skim_bfat_from_esl_ii(df)

    # ── Chart-only Category + Class knobs ────────────────────────────────
    _VALID_CAT = {"HTST", "ESL", "COTTAGE CHEESE"}
    cat_filter = (category_filter or "").strip().upper()
    if cat_filter and cat_filter in _VALID_CAT:
        df = df[df["Category"].astype(str).str.upper() == cat_filter]

    cls_filter = (class_filter or "").strip().upper()
    if cls_filter in {"I", "II"}:
        df = df[df["Class"].astype(str).str.upper() == cls_filter]

    if df.empty:
        return pd.DataFrame()

    # Spreadsheet-friendly Month rendering.  Keep the existing FMMO
    # column ordering so the download mirrors the lakehouse schema.
    df = df.copy()
    df["Month"] = df["Month"].dt.strftime("%Y-%m-%d")
    canonical_cols = [
        "Category", "Month", "Class",
        "Skim Rate", "Butterfat Rate",
        "Protein Rate", "Other Solids Rate",
    ]
    return df[[c for c in canonical_cols if c in df.columns]].reset_index(drop=True)


def _build_milk_commodity_chart(
    milk_df: pd.DataFrame,
    start_month: Optional[pd.Timestamp] = None,
    end_month: Optional[pd.Timestamp] = None,
    category_filter: Optional[str] = None,
    class_filter: Optional[str] = None,
) -> go.Figure:
    """Multi-series time-series chart of milk Skim, Butterfat, Protein,
    and Other Solids rates.

    The chart visualises every (Category, Class) combination that
    exists in ``Milk_Mover_Tracker``. Skim Rate plots on the primary
    y-axis (solid); Butterfat / Protein / Other Solids plot on the
    secondary y-axis (dashed, dotted, dash-dot respectively). The two
    Cottage Cheese-only metrics (Protein, Other Solids) auto-suppress
    for HTST/ESL series since those rows carry ``null`` rates for them.
    Missing-column / all-null traces are skipped silently — the chart
    degrades gracefully rather than raising.

    The optional ``start_month`` / ``end_month`` arguments restrict the
    plotted x-range so the chart reacts to the time slicer rendered
    above it. Both bounds are inclusive; ``None`` means "no bound on
    that side".

    ``category_filter`` is a chart-only knob that restricts which
    Category families are drawn — accepts ``"HTST"`` / ``"ESL"`` /
    ``"Cottage Cheese"`` (case-insensitive) to isolate one family, or
    ``None`` / ``"All"`` to draw every series.

    ``class_filter`` is the parallel chart-only knob for Class —
    accepts ``"I"`` / ``"II"`` (case-insensitive) to isolate one milk
    class, or ``None`` / ``"All"`` to draw every class. The two filters
    compose (``category_filter="HTST"`` + ``class_filter="I"`` shows
    HTST Class I only).

    Neither filter influences any downstream calculation (metrics,
    mover_details_table, milk_mover downloads, example-prices enrichment) — those continue
    to consume every (Category, Class) row from ``Milk_Mover_Tracker``
    exactly as before.
    """
    df = _strip_df_columns(milk_df).copy()
    if "Month" not in df.columns or "Category" not in df.columns or "Class" not in df.columns:
        return go.Figure()

    df["Month"] = pd.to_datetime(df["Month"], errors="coerce")
    df = df.dropna(subset=["Month"]).sort_values("Month")

    # Apply the slicer bounds before splitting into per-series traces so every
    # downstream slice automatically respects the user's selection.
    if start_month is not None:
        df = df[df["Month"] >= pd.Timestamp(start_month)]
    if end_month is not None:
        df = df[df["Month"] <= pd.Timestamp(end_month)]
    if df.empty:
        return go.Figure()

    # Cottage Cheese II skim/bfat mirror ESL II for the same month
    # (May-2026 contract).  The auto-updater write-side now sets these
    # values explicitly AND the orchestrator runs a one-shot in-place
    # repair pass on legacy rows — but a brief window can exist between
    # a deploy and the first repair tick, so we mirror at chart-display
    # time too as defence in depth.  Cheap: one indexed merge per
    # chart render.
    df = _mirror_cc_ii_skim_bfat_from_esl_ii(df)

    # Tolerant column resolution — header drift like "Skim Rate " or
    # "Butterfat Rate $" still resolves to the expected metric. "Other
    # Solids" is matched FIRST so it can't be greedily absorbed by a
    # future column named just "solids"; "Skim" excludes "Nonfat Solids"
    # defensively (cheap insurance against future PDF labelling).
    def _resolve_chart_col(predicate) -> Optional[str]:
        return next((c for c in df.columns if predicate(c.lower())), None)

    other_col   = _resolve_chart_col(lambda c: "other solids" in c)
    protein_col = _resolve_chart_col(lambda c: "protein" in c)
    bf_col      = _resolve_chart_col(lambda c: "butter" in c)
    skim_col    = _resolve_chart_col(
        lambda c: "skim" in c and "nonfat" not in c and "non-fat" not in c
    )
    if all(c is None for c in (skim_col, bf_col, protein_col, other_col)):
        return go.Figure()

    # Normalise the chart-only category filter once. Anything outside
    # the known set falls back to "show everything" so unexpected values
    # can never silently blank out the chart. Cottage Cheese was added
    # in May-2026; the comparison set is uppercased once so the chart's
    # case-insensitive matching stays consistent below.
    _VALID_CAT_FILTERS = {"HTST", "ESL", "COTTAGE CHEESE"}
    cat_filter = (category_filter or "").strip().upper()
    if cat_filter not in _VALID_CAT_FILTERS:
        cat_filter = ""

    # Same defensive normalisation for the chart-only class filter; the keys
    # in _MILK_COLOR_MAP are already upper-case Roman numerals.
    cls_filter = (class_filter or "").strip().upper()
    if cls_filter not in {"I", "II"}:
        cls_filter = ""

    fig = go.Figure()
    for (cat, cls), color in _MILK_COLOR_MAP.items():
        if cat_filter and cat != cat_filter:
            continue
        if cls_filter and cls != cls_filter:
            continue
        sub = df[
            (df["Category"].astype(str).str.upper() == cat)
            & (df["Class"].astype(str).str.upper() == cls)
        ].sort_values("Month")
        if sub.empty:
            continue

        # Display label uses the canonical mixed-case spelling so the
        # legend reads "Cottage Cheese Class II Butterfat" rather than
        # "COTTAGE CHEESE Class II Butterfat".
        display_cat = cat.title() if cat == "COTTAGE CHEESE" else cat

        # ── Skim — primary axis (only relevant for HTST / ESL) ─────────
        if skim_col is not None:
            skim_y = pd.to_numeric(sub[skim_col], errors="coerce")
            # Skip drawing when every value is null (e.g. Cottage Cheese
            # rows have null Skim Rate by design — no point in adding an
            # empty trace and cluttering the legend).
            if skim_y.notna().any():
                fig.add_trace(go.Scatter(
                    x=sub["Month"],
                    y=skim_y,
                    mode="lines+markers",
                    name=f"{display_cat} Class {cls} Skim",
                    line=dict(color=color, width=2, dash="solid"),
                    marker=dict(size=4),
                    hovertemplate=(
                        f"<b>{display_cat} Class {cls} Skim</b><br>"
                        "%{x|%b %Y}<br>$%{y:.4f}<extra></extra>"
                    ),
                ))

        # ── Butterfat — secondary axis ────────────────────────────────
        if bf_col is not None:
            bf_y = pd.to_numeric(sub[bf_col], errors="coerce")
            if bf_y.notna().any():
                fig.add_trace(go.Scatter(
                    x=sub["Month"],
                    y=bf_y,
                    mode="lines+markers",
                    name=f"{display_cat} Class {cls} Butterfat",
                    line=dict(color=color, width=2, dash="dash"),
                    marker=dict(size=4, symbol="diamond"),
                    yaxis="y2",
                    hovertemplate=(
                        f"<b>{display_cat} Class {cls} Butterfat</b><br>"
                        "%{x|%b %Y}<br>$%{y:.4f}<extra></extra>"
                    ),
                ))

        # ── Protein — secondary axis (Cottage Cheese only by data) ────
        if protein_col is not None:
            protein_y = pd.to_numeric(sub[protein_col], errors="coerce")
            if protein_y.notna().any():
                fig.add_trace(go.Scatter(
                    x=sub["Month"],
                    y=protein_y,
                    mode="lines+markers",
                    name=f"{display_cat} Class {cls} Protein",
                    line=dict(color=color, width=2, dash="dot"),
                    marker=dict(size=4, symbol="square"),
                    yaxis="y2",
                    hovertemplate=(
                        f"<b>{display_cat} Class {cls} Protein</b><br>"
                        "%{x|%b %Y}<br>$%{y:.4f}<extra></extra>"
                    ),
                ))

        # ── Other Solids — secondary axis (Cottage Cheese only by data)
        if other_col is not None:
            other_y = pd.to_numeric(sub[other_col], errors="coerce")
            if other_y.notna().any():
                fig.add_trace(go.Scatter(
                    x=sub["Month"],
                    y=other_y,
                    mode="lines+markers",
                    name=f"{display_cat} Class {cls} Other Solids",
                    line=dict(color=color, width=2, dash="dashdot"),
                    marker=dict(size=4, symbol="triangle-up"),
                    yaxis="y2",
                    hovertemplate=(
                        f"<b>{display_cat} Class {cls} Other Solids</b><br>"
                        "%{x|%b %Y}<br>$%{y:.4f}<extra></extra>"
                    ),
                ))

    fig.update_layout(
        xaxis=dict(title="", showgrid=False, showline=True, linecolor="#e0e0e0"),
        yaxis=dict(title="Skim Rate ($)", showgrid=True, gridcolor="#f0f0f0",
                   showline=True, linecolor="#e0e0e0", rangemode="tozero"),
        # The right axis carries Butterfat, Protein, and Other Solids
        # rates — all three quoted in $/lb so they share a scale.
        yaxis2=dict(title="Butterfat / Protein / Other Solids Rate ($/lb)",
                    overlaying="y", side="right",
                    showgrid=False, showline=True, linecolor="#e0e0e0",
                    rangemode="tozero"),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(l=50, r=50, t=30, b=80),
        height=360,
        legend=dict(
            orientation="h",
            yanchor="top", y=-0.18,
            xanchor="center", x=0.5,
            font=dict(size=12),
        ),
        hovermode="x unified",
    )
    return fig


# ── 5. Editable Movers_Non_Milk_Tracker ───────────────────────────────────────

# Session-state key for the **stable seed** DataFrame passed to
# ``st.data_editor``.  Seeded exactly once per session (and on header
# migrations) and otherwise NEVER overwritten — see the bug-fix note in
# :func:`_render_movers_non_milk_editor` for the rationale.  Downstream
# consumers do NOT read from this slot; they read the editor's live
# output from :data:`_SS_NMT_EDITED_VIEW` instead.
_SS_NMT_DF = f"{_SS_PREFIX}_movers_non_milk_df"

# Session-state key for the editor's coerced output (Month → datetime,
# numerics → float64).  Repopulated on every rerun by
# :func:`_render_movers_non_milk_editor` and consumed by the Refresh
# orchestrator, the editing-month resolver, and the Mover Downloads
# publisher.  Decoupling it from ``_SS_NMT_DF`` (the editor's seed)
# is what fixes the "entries disappear on ENTER" bug.
_SS_NMT_EDITED_VIEW = f"{_SS_PREFIX}_movers_non_milk_edited_view"

# Internal Streamlit session-state key the ``st.data_editor`` widget
# uses to persist the per-cell edit deltas across reruns.  Centralised
# here so test code can locate it without re-deriving the prefix.
_SS_NMT_EDITOR_KEY = f"{_SS_PREFIX}_nmt_editor"

# Session-state keys for the Milk Commodity Cost time slicer. Persisted here
# so changing the slicer reactively re-layers the milk columns on the mover_details_table
# table without requiring another Refresh click.
_SS_MILK_START = f"{_SS_PREFIX}_milk_start_month"
_SS_MILK_END   = f"{_SS_PREFIX}_milk_end_month"

# Session-state keys for the chart-only Category ("HTST vs ESL") and Class
# ("I vs II") filters. Both live beside the time slicer keys but are
# intentionally NOT consumed by the milk pipeline — they only restrict which
# (Category, Class) lines are rendered on the Milk Commodity Cost chart, so
# metrics, mover_details_table, milk_mover downloads and example-prices enrichment
# are unaffected.
_SS_MILK_CATEGORY = f"{_SS_PREFIX}_milk_category_filter"
_SS_MILK_CLASS    = f"{_SS_PREFIX}_milk_class_filter"

# Filter options for the chart-only category selector. ``"All"`` is the
# default and preserves the legacy multi-line view; ``"HTST"`` / ``"ESL"``
# isolate the two pasteurisation methods so users can compare classes
# within a single family without legend clutter. ``"Cottage Cheese"``
# was added in May-2026 alongside the Class II Nonfat Solids ingestion
# — selecting it isolates Cottage Cheese's three $/lb rate series
# (Butterfat, Protein, Other Solids).
_MILK_CATEGORY_ALL: str = "All"
_MILK_CATEGORY_OPTIONS: tuple[str, ...] = (
    _MILK_CATEGORY_ALL, "HTST", "ESL", "Cottage Cheese",
)

# Filter options for the chart-only class selector. ``"All"`` preserves the
# default view (both Class I and Class II for whatever Category is selected);
# ``"I"`` / ``"II"`` isolate one milk class so users can compare HTST vs ESL
# within a single class without legend clutter.
_MILK_CLASS_ALL: str = "All"
_MILK_CLASS_OPTIONS: tuple[str, ...] = (_MILK_CLASS_ALL, "I", "II")


def _hardcoded_movers_non_milk_seed() -> pd.DataFrame:
    """Return the historical hard-coded seed.

    Used as a fall-back when the lakehouse copy is absent (cold-bootstrap
    deployments) or unreadable for any reason — kept as a code-side
    safety net so a brand-new tenant can still bring the editor up.
    """
    df = pd.DataFrame(_NMT_SEED_ROWS, columns=list(_NMT_ALL_COLUMNS))
    return _coerce_nmt_seed_dtypes(df)


def _coerce_nmt_seed_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """Type-coerce a candidate seed frame to the editor's expected dtypes.

    Month → ``datetime64[ns]`` (normalised to month-start);
    every numeric column → ``float64``.

    Centralised so the lakehouse-sourced seed and the hard-coded seed
    go through the exact same coercion path — the data-editor refuses
    to edit numeric columns whose dtype is ``object``, so this matters.
    """
    out = df.copy()
    if _NMT_COL_MONTH in out.columns:
        out[_NMT_COL_MONTH] = pd.to_datetime(out[_NMT_COL_MONTH], errors="coerce")
    for col in _NMT_NUMERIC_COLUMNS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    return out


def _seed_movers_non_milk_df() -> pd.DataFrame:
    """Return a fresh seed DataFrame for the Movers Non-Milk Tracker.

    Resolution order — operator-facing contract is "the UI reflects
    the latest lakehouse copy on cold start":

    1.  Read ``Files/Monthly_Pricing_Execution/Movers_Non_Milk_Tracker.csv``
        from OneLake via :func:`_mpe_store.read_movers_non_milk_tracker_df`.
        Apply the same header-migration + dtype-coercion pipeline used
        on user-edited frames so legacy column names land on the
        current canon.  Reindex to :data:`_NMT_ALL_COLUMNS` so extra
        columns (e.g. transient publishing artefacts) are dropped and
        missing columns are filled with NaN.
    2.  Fall back to :func:`_hardcoded_movers_non_milk_seed` when the
        lakehouse blob is absent, empty, or raises an unexpected error.
        Hard-coded fallback keeps fresh-tenant onboarding alive and
        provides a deterministic schema even with no Refresh history.

    Every numeric column is forced to ``float64`` regardless of source —
    mixing ``None`` with a numeric in an editable column otherwise
    yields ``object`` dtype, which Streamlit's NumberColumn refuses to
    edit cell-by-cell.
    """
    try:
        lakehouse = _mpe_store.read_movers_non_milk_tracker_df()
    except Exception as exc:
        # Defensive — any unexpected error from the read path must not
        # block the page from rendering.  Log + fall back so the
        # editor still mounts with the hard-coded seed.
        logger.warning(
            "Failed to read Movers_Non_Milk_Tracker from lakehouse "
            "(falling back to hard-coded seed): %s", exc,
        )
        lakehouse = None

    if lakehouse is not None and not lakehouse.empty:
        seeded = _migrate_nmt_headers(lakehouse)
        # Drop any columns we don't render and add any required column
        # that's absent in the lakehouse copy — keeps the editor schema
        # stable across schema migrations in either direction.
        seeded = seeded.reindex(columns=list(_NMT_ALL_COLUMNS))
        return _coerce_nmt_seed_dtypes(seeded)

    return _hardcoded_movers_non_milk_seed()


# Legacy → canonical header migration map for the Movers Non-Milk Tracker.
# Each entry survives one cycle of widespread re-deployment; once every
# session has been visited at least once after the rename ships, the
# entry can be removed safely.  May-2026: TOPCO resin header rebrand.
_NMT_HEADER_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("TOPCO HTST Resin Mover ($/lbs)", _NMT_COL_TOPCO_RESIN),
)


def _migrate_nmt_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Rename any legacy NMT columns to the current canonical names.

    Called from :func:`_ensure_movers_non_milk_state` and from the
    lakehouse-seeded code path in :func:`_seed_movers_non_milk_df`, so
    both in-memory cached frames AND lakehouse-resident frames land on
    the current schema without losing any data.  Safe no-op when the
    legacy column is absent or the canonical column already exists.
    """
    if df is None or df.empty:
        return df
    out = df
    for legacy, canonical in _NMT_HEADER_MIGRATIONS:
        if legacy in out.columns and canonical not in out.columns:
            out = out.rename(columns={legacy: canonical})
    return out


def _ensure_movers_non_milk_state() -> None:
    """Seed ``session_state[_SS_NMT_DF]`` on first render of this fragment.

    Also applies header migrations to any frame already cached in session
    state so we don't lose user edits made before a schema rebrand.
    """
    if _SS_NMT_DF not in st.session_state:
        st.session_state[_SS_NMT_DF] = _seed_movers_non_milk_df()
        return

    migrated = _migrate_nmt_headers(st.session_state[_SS_NMT_DF])
    if migrated is not st.session_state[_SS_NMT_DF]:
        st.session_state[_SS_NMT_DF] = migrated


def _coerce_nmt_edited_frame(edited: pd.DataFrame) -> pd.DataFrame:
    """Re-type ``edited`` so downstream readers don't need to re-coerce.

    Month → ``datetime64[ns]``; every numeric column → ``float64``.
    Returns a fresh copy so callers can mutate freely without poking
    the widget's internal state.
    """
    out = edited.copy()
    if _NMT_COL_MONTH in out.columns:
        out[_NMT_COL_MONTH] = pd.to_datetime(out[_NMT_COL_MONTH], errors="coerce")
    for col in _NMT_NUMERIC_COLUMNS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    return out


def _render_movers_non_milk_editor() -> pd.DataFrame:
    """Render the editable Movers_Non_Milk_Tracker; return the latest frame.

    Behaviour
    ---------
    * All cells (including Month) are editable.
    * Rows can be added / removed via the editor's built-in toolbar
      (``num_rows="dynamic"``).
    * The returned frame is the coerced view (Month → ``datetime64``,
      numerics → ``float64``) so downstream calculations don't need to
      re-coerce.  A copy of that view is also stashed in
      :data:`_SS_NMT_EDITED_VIEW` for orchestrators that re-read NMT
      state outside of this fragment's render call (e.g. the Refresh
      orchestrator's Mover Downloads publisher).

    Bug-fix notes (May-2026-late "entries disappear on ENTER")
    ----------------------------------------------------------
    The previous version wrote ``edited`` BACK to ``_SS_NMT_DF`` on
    every rerun.  ``st.data_editor`` is delta-based: it owns its own
    edit state under ``key=`` and applies that state to the ``data=``
    frame on every render.  When ``data=`` changes between renders
    (which is exactly what writing ``edited`` back does), the widget
    re-rebases its deltas onto a frame that *already* contains them —
    and the most-recently typed cell ends up clobbered, OR a freshly-
    added row gets dropped because the rebase can't find it.

    The fix is **stop the feedback loop**: pass the original seed
    DataFrame to ``st.data_editor`` and never overwrite it.  Every
    edit lives in the widget's internal state under
    :data:`_SS_NMT_EDITOR_KEY`; the widget returns the fully-applied
    frame as ``edited`` and we just coerce + cache that for downstream
    reads.  Header migrations remain the only path that mutates the
    seed slot (see :func:`_ensure_movers_non_milk_state`).
    """
    _ensure_movers_non_milk_state()
    seed_df: pd.DataFrame = st.session_state[_SS_NMT_DF]

    # Column config: Month as DateColumn (M/D/YYYY for parity with source),
    # numerics as NumberColumn with 4-decimal precision (same as $/lbs / $/Gal
    # values in the source file).
    column_config: dict = {
        _NMT_COL_MONTH: st.column_config.DateColumn(
            _NMT_COL_MONTH,
            help="First-of-month date for this row.",
            format="MM/DD/YYYY",
            step=1,
        ),
    }
    for col in _NMT_NUMERIC_COLUMNS:
        column_config[col] = st.column_config.NumberColumn(
            col,
            help=f"Editable — enter the {col} value.",
            format="%.4f",
            step=0.0001,
        )

    edited = st.data_editor(
        seed_df,
        column_config=column_config,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        key=_SS_NMT_EDITOR_KEY,
    )

    # Build the coerced view AND stash it for downstream reads.  We do
    # NOT touch ``_SS_NMT_DF`` — that slot stays at the seed shape so
    # ``st.data_editor`` keeps its delta state stable across reruns.
    edited_view = _coerce_nmt_edited_frame(edited)
    st.session_state[_SS_NMT_EDITED_VIEW] = edited_view
    return edited_view


def _get_nmt_edited_view() -> Optional[pd.DataFrame]:
    """Return the cached, coerced NMT edited view (or ``None``).

    Read by orchestrators that need NMT state outside of the editor's
    render call (Mover Downloads publisher, editing-month resolver,
    Refresh orchestrator).  Falls back to ``_SS_NMT_DF`` (the seed)
    only on the very first render, before
    :func:`_render_movers_non_milk_editor` has executed in this
    session — the seed is correctly shaped, just not yet edited.
    """
    view = st.session_state.get(_SS_NMT_EDITED_VIEW)
    if view is not None:
        return view
    return st.session_state.get(_SS_NMT_DF)


# ── 6. Calculations ───────────────────────────────────────────────────────────

# 6a. Resin FG generation — unchanged contract from the legacy module so the
# downstream consumers (resin_mover_fg downloads, resin-mover_details_table fallback) keep
# matching the spec the team is already familiar with.

def _latest_scrape_fraction(scrape_tracker_df: pd.DataFrame) -> float:
    """Latest scrape / yield-loss rate from ``Scrape_Tracker`` (defaults 0.0).

    The CSV has columns ``Month``, ``Parameter``, ``Resin`` where the *Resin*
    column holds a percent string like ``"1%"``. We pick the row with the
    latest Month and return the parsed fraction (``0.01`` for ``"1%"``).
    """
    if scrape_tracker_df.empty:
        return 0.0
    df = scrape_tracker_df.copy()
    if "Month" in df.columns:
        df["_month_dt"] = df["Month"].apply(_parse_month)
        df = df.dropna(subset=["_month_dt"]).sort_values("_month_dt")
    if df.empty:
        return 0.0
    value_col = "Resin" if "Resin" in df.columns else df.columns[-1]
    pct = _parse_percent(df.iloc[-1][value_col])
    return float(pct) if pct is not None else 0.0


def _build_resincalculate(
    resin_calculator_df: pd.DataFrame,
    resin_cost_per_lb: float,
    scrape_fraction: float,
) -> pd.DataFrame:
    """Build the ``resincalculate`` DataFrame.

    Formula::

        Resin Cost ($/Gal) = resin_cost_per_lb
                           × Usage (Lbs/Ea)
                           × (1 + scrape_fraction)
                           ÷ Gal/Ea
    """
    df = resin_calculator_df.copy()
    usage  = pd.to_numeric(df.get(_COL_USAGE_LBS), errors="coerce")
    gal_ea = pd.to_numeric(df.get(_COL_GAL_EA),   errors="coerce")
    df[_COL_RESIN_GAL] = (
        resin_cost_per_lb * usage * (1.0 + scrape_fraction) / gal_ea
    ).round(4)
    return df


# Output column names for the resin-mover FG. Defined once at module scope so
# the builder, downstream consumers (resin-mover_details_table fallback, example-prices
# enrichment) and tests can reference a single source of truth.
_FG_COL_OLD       = "Old Resin Cost ($/Gal)"
_FG_COL_NEW       = "New Resin Cost ($/Gal)"
_FG_COL_MOVER     = "Resin Mover ($/Gal)"
_FG_OUTPUT_COLUMNS: tuple[str, ...] = (
    _COL_PRODUCT_ID, _COL_PRODUCT_DESC, _COL_RESIN,
    _FG_COL_OLD, _FG_COL_NEW, _FG_COL_MOVER,
)


# ── Resin_Cost_Tracker payload builders (NMT + Resin_Calculator → tracker rows) ─


def _build_resincalculate(
    resin_calculator_df: pd.DataFrame,
    resin_cost_per_lb: float,
    scrape_fraction: float,
) -> pd.DataFrame:
    """Build the per-Product-ID ``Resin Cost ($/Gal)`` table for one $/lbs driver.

    Formula::

        Resin Cost ($/Gal) = resin_cost_per_lb
                           × Usage (Lbs/Ea)
                           × (1 + scrape_fraction)
                           ÷ Gal/Ea

    The output preserves every Resin_Calculator column verbatim and
    overwrites only ``Resin Cost ($/Gal)``.  Defined at module scope so
    both the FG-from-tracker path and the tracker-rewrite payload
    builder share a single cost formula.
    """
    df = resin_calculator_df.copy()
    usage  = pd.to_numeric(df.get(_COL_USAGE_LBS), errors="coerce")
    gal_ea = pd.to_numeric(df.get(_COL_GAL_EA),   errors="coerce")
    df[_COL_RESIN_GAL] = (
        resin_cost_per_lb * usage * (1.0 + scrape_fraction) / gal_ea
    ).round(4)
    return df


# Side label used in the Resin_Cost_Tracker schema (the May-2026
# "Rest Market vs TOPCO" dimension).  Re-exported from the store so
# the page never disagrees with the file on canonical Side spelling.
_TRACKER_COL_SIDE  = _resin_store.COL_REST_VS_TOPCO
_TRACKER_SIDE_REST  = _resin_store.SIDE_REST
_TRACKER_SIDE_TOPCO = _resin_store.SIDE_TOPCO


def _build_tracker_rows_for_nmt(
    movers_non_milk_df: pd.DataFrame,
    resin_calculator_df: pd.DataFrame,
    scrape_fraction: float,
) -> pd.DataFrame:
    """Materialise the per-(Month × Side) rows of ``Resin_Cost_Tracker``.

    For every row of the Movers Non-Milk Tracker we emit two blocks of
    Resin_Calculator rows — one for ``Rest`` (``Rest HTST Resin Cost
    ($/lbs)``) and one for ``TOPCO`` (``TOPCO HTST Resin Cost ($/lbs)``).
    Each block applies the per-side $/lbs cost through the
    :func:`_build_resincalculate` formula and tags the resulting rows
    with ``Rest Market vs TOPCO`` plus the canonical first-of-month
    ``Month``.

    NMT rows whose Month is unparseable, or whose corresponding $/lbs
    cell is missing for a given side, are silently skipped on that side
    — the per-side payload simply lacks that month.

    Returned schema (canonical order)::

        Rest Market vs TOPCO | Pricing Category | Resin Cost ($/Gal) |
        Month | Usage (Lbs/Ea) | Gal/Ea | <calculator extras…>

    ``Pricing Category`` is the natural row identifier per the
    lakehouse ``Resin_Calculator.csv`` schema (e.g. ``"Alb - GAL
    Conventional - 62 grams"``).  ``Product ID`` / ``Product
    Description`` / ``Resin`` are NOT present in the calculator file
    and therefore not in this payload either — they're carried in the
    canonical column list only as forward-compat placeholders for any
    legacy tracker rows that already have them.

    Used by:

    * The single Refresh writer (passes the entire payload through
      :func:`resin_cost_tracker_store.upsert_for_sides` — that one
      call performs both overwrite-existing and append-new in a
      single ETag-guarded write).
    * The in-memory FG builder (merged with the persisted tracker into
      an "effective tracker" so Refresh-time FG metrics reflect the
      operator's just-edited NMT before the lakehouse write lands).
    """
    if movers_non_milk_df is None or movers_non_milk_df.empty:
        return pd.DataFrame()
    if resin_calculator_df is None or resin_calculator_df.empty:
        return pd.DataFrame()

    blocks: list[pd.DataFrame] = []

    # Iterate every NMT row to capture every month the operator has
    # entered, not just the latest.  The per-side branches are
    # independent so a row with only Rest filled in still contributes a
    # Rest block.
    for _, nmt_row in movers_non_milk_df.iterrows():
        month_ts = _parse_month(nmt_row.get(_NMT_COL_MONTH))
        if month_ts is None:
            continue

        for side_label, lbs_col in (
            (_TRACKER_SIDE_REST,  _NMT_COL_REST_RESIN),
            (_TRACKER_SIDE_TOPCO, _NMT_COL_TOPCO_RESIN),
        ):
            lbs_value = pd.to_numeric(nmt_row.get(lbs_col), errors="coerce")
            if pd.isna(lbs_value):
                continue

            block = _build_resincalculate(
                resin_calculator_df, float(lbs_value), scrape_fraction,
            )
            block = block.copy()
            block[_TRACKER_COL_SIDE] = side_label
            block[_COL_MONTH] = month_ts
            blocks.append(block)

    if not blocks:
        return pd.DataFrame()

    out = pd.concat(blocks, ignore_index=True)

    # Canonical column ordering: Side first, then natural identity
    # (Pricing Category — the lakehouse calculator's row identifier),
    # then any forward-compat legacy identity columns if they happen to
    # be present, then computed $/Gal, then Month, then auxiliary
    # calculator columns trailing.  Columns not in the leading list
    # pass through in their original order so any extra calculator
    # fields (Grams/ea, Lbs/gram, etc.) survive the round-trip.
    leading = [
        _TRACKER_COL_SIDE, _COL_PRICING_CAT,
        _COL_PRODUCT_ID, _COL_PRODUCT_DESC, _COL_RESIN,
        _COL_RESIN_GAL, _COL_MONTH,
    ]
    leading_present = [c for c in leading if c in out.columns]
    trailing = [c for c in out.columns if c not in leading_present]
    return out[leading_present + trailing]


def _build_effective_resin_cost_tracker(
    persisted_tracker_df: pd.DataFrame,
    payload_rows: pd.DataFrame,
) -> pd.DataFrame:
    """Merge NMT-derived ``payload_rows`` onto the persisted tracker in memory.

    Every ``(Month, Side)`` pair present in ``payload_rows`` REPLACES the
    matching persisted rows; every other row of ``persisted_tracker_df``
    is preserved verbatim.  Used to produce the in-memory frame the FG
    builder reads from — keeps Refresh-time metrics consistent with the
    operator's just-edited NMT, even before the lakehouse rewrite has
    actually landed.

    The effective tracker is purely an in-memory artefact — it never
    touches OneLake on its own.  The actual lakehouse write is performed
    by the orchestrator helpers below using a side payload subset that
    matches the single-Refresh upsert path.
    """
    if persisted_tracker_df is None or persisted_tracker_df.empty:
        return payload_rows.copy() if payload_rows is not None else pd.DataFrame()
    if payload_rows is None or payload_rows.empty:
        return persisted_tracker_df.copy()

    existing = persisted_tracker_df.copy()
    # Normalise the persisted column headers so the merge is robust against
    # stray whitespace in source files.
    existing.columns = [str(c).strip() for c in existing.columns]

    months  = existing[_COL_MONTH].apply(_parse_month) \
        if _COL_MONTH in existing.columns else pd.Series([None] * len(existing))
    if _TRACKER_COL_SIDE in existing.columns:
        sides_raw = existing[_TRACKER_COL_SIDE]
    else:
        sides_raw = pd.Series([None] * len(existing))
    sides = sides_raw.apply(_resin_store.normalise_side)

    payload_keys: set[tuple[pd.Timestamp, str]] = set()
    payload_months = payload_rows[_COL_MONTH].apply(_parse_month)
    payload_sides  = payload_rows[_TRACKER_COL_SIDE].apply(_resin_store.normalise_side)
    for m, s in zip(payload_months, payload_sides):
        if m is not None and s is not None:
            payload_keys.add((m, s))

    drop_mask = pd.Series(
        [(m, s) in payload_keys for m, s in zip(months, sides)],
        index=existing.index,
    )
    survivors = existing.loc[~drop_mask]
    return pd.concat([survivors, payload_rows], ignore_index=True)


def _build_resin_mover_fg_from_tracker(
    tracker_df: pd.DataFrame,
    *,
    old_month: pd.Timestamp,
    new_month: pd.Timestamp,
    side: str,
) -> pd.DataFrame:
    """Build a Resin Mover FG by joining one Side's Old + New months on Product ID.

    Selection rule for both Old and New columns::

        Resin Cost ($/Gal) where (Month == X AND Rest Market vs TOPCO == side)

    Joined by Product ID so every (Old, New, Mover) triple lines up
    against a single SKU.  Rows lacking either an Old or New cost
    surface as NaN in that column and an NaN mover — the FG still
    includes them so downstream consumers can see what's missing.

    Returns the canonical 6-column FG (matches the legacy schema)::

        Product ID | Product Description | Resin |
        Old Resin Cost ($/Gal) | New Resin Cost ($/Gal) | Resin Mover ($/Gal)
    """
    if tracker_df is None or tracker_df.empty:
        return pd.DataFrame(columns=list(_FG_OUTPUT_COLUMNS))
    if _COL_MONTH not in tracker_df.columns or _TRACKER_COL_SIDE not in tracker_df.columns:
        return pd.DataFrame(columns=list(_FG_OUTPUT_COLUMNS))

    df = tracker_df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df["_month_dt"] = df[_COL_MONTH].apply(_parse_month)
    df["_side"]     = df[_TRACKER_COL_SIDE].apply(_resin_store.normalise_side)

    side_normal = _resin_store.normalise_side(side)
    side_df = df[df["_side"] == side_normal]
    if side_df.empty:
        return pd.DataFrame(columns=list(_FG_OUTPUT_COLUMNS))

    old_rows = side_df[side_df["_month_dt"] == old_month].copy()
    new_rows = side_df[side_df["_month_dt"] == new_month].copy()
    if old_rows.empty and new_rows.empty:
        return pd.DataFrame(columns=list(_FG_OUTPUT_COLUMNS))

    # Collapse Product-ID-only duplicates per side per month — the source
    # tracker occasionally carries two Product IDs with identical
    # (Product Description, Resin) pairs.  Their $/Gal is identical by
    # construction, so we keep the first Product ID for a clean FG.
    def _dedupe(rows: pd.DataFrame) -> pd.DataFrame:
        if rows.empty:
            return rows
        cols_for_dedupe = [c for c in (_COL_PRODUCT_DESC, _COL_RESIN) if c in rows.columns]
        if not cols_for_dedupe:
            return rows
        return rows.drop_duplicates(subset=cols_for_dedupe, keep="first")

    old_rows = _dedupe(old_rows)
    new_rows = _dedupe(new_rows)

    # Skinny side-keyed projections for the join.
    keep_cols = [c for c in (_COL_PRODUCT_ID, _COL_PRODUCT_DESC, _COL_RESIN) if c in side_df.columns]
    old_slim = old_rows[keep_cols + [_COL_RESIN_GAL]].rename(
        columns={_COL_RESIN_GAL: _FG_COL_OLD},
    )
    new_slim = new_rows[keep_cols + [_COL_RESIN_GAL]].rename(
        columns={_COL_RESIN_GAL: _FG_COL_NEW},
    )

    if old_slim.empty:
        merged = new_slim.copy()
        merged[_FG_COL_OLD] = pd.NA
    elif new_slim.empty:
        merged = old_slim.copy()
        merged[_FG_COL_NEW] = pd.NA
    else:
        # Outer join keeps Product IDs that appear in only one of the two
        # months so the operator can see partial coverage in the FG.
        merge_keys = [c for c in keep_cols if c in old_slim.columns and c in new_slim.columns]
        merged = old_slim.merge(new_slim, on=merge_keys, how="outer")

    new_vals = pd.to_numeric(merged.get(_FG_COL_NEW), errors="coerce")
    old_vals = pd.to_numeric(merged.get(_FG_COL_OLD), errors="coerce")
    merged[_FG_COL_NEW]   = new_vals.round(4)
    merged[_FG_COL_OLD]   = old_vals.round(4)
    merged[_FG_COL_MOVER] = (new_vals - old_vals).round(4)

    return merged[[c for c in _FG_OUTPUT_COLUMNS if c in merged.columns]].reset_index(drop=True)


@dataclass(frozen=True)
class _ResinFGSelection:
    """The per-side anchor months used to build one Resin Mover FG.

    Surfaces both timestamps + a list of human-readable warning strings
    so the caller can render an actionable ``st.warning`` when the
    tracker is missing the strict ``new − 1 calendar month`` row.  The
    warnings list is empty in the happy path — keeps the orchestrator
    side simple ("if warnings: render banner").
    """
    side:        str
    new_month:   Optional[pd.Timestamp]
    old_month:   Optional[pd.Timestamp]
    warnings:    tuple[str, ...]


def _select_resin_fg_months(
    effective_tracker_df: pd.DataFrame,
    *,
    side: str,
) -> _ResinFGSelection:
    """Pick the per-side ``(new_month, old_month)`` anchors for the FG.

    May-2026-late contract:
      * ``new_month`` = ``max(Month where Side == side)`` in the
        EFFECTIVE tracker (persisted ∪ payload — so a just-typed NMT
        row participates in Refresh-time FG metrics even before the
        lakehouse upsert lands).
      * ``old_month`` = ``new_month − 1 calendar month`` (strict
        calendar subtraction; NOT the next-newest month present).
        A missing ``old_month`` row in the tracker surfaces a warning.

    Returns a :class:`_ResinFGSelection` carrying:
      * ``side`` — the canonical Side label.
      * ``new_month`` / ``old_month`` — the chosen anchors (``None``
        only when the side has no rows in the tracker at all).
      * ``warnings`` — operator-actionable strings, empty in the happy
        path.  Examples::

            "Resin_Cost_Tracker has no rows for the Rest side — add
             at least one row before Refresh can build the FG."
            "Resin_Cost_Tracker is missing the Old month (May 2026) for
             TOPCO — Old Resin Cost ($/Gal) will appear blank.  Add the
             missing row to the lakehouse, then click Refresh again."
    """
    canon = _resin_store.normalise_side(side)
    if canon is None:
        return _ResinFGSelection(side=side, new_month=None, old_month=None,
                                 warnings=(f"Unknown Side label {side!r}.",))

    if effective_tracker_df is None or effective_tracker_df.empty:
        return _ResinFGSelection(
            side=canon, new_month=None, old_month=None,
            warnings=(
                f"Resin_Cost_Tracker has no rows for the {canon} side — "
                "add at least one row before Refresh can build the FG.",
            ),
        )

    df = effective_tracker_df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    if _COL_MONTH not in df.columns or _TRACKER_COL_SIDE not in df.columns:
        return _ResinFGSelection(
            side=canon, new_month=None, old_month=None,
            warnings=(
                "Resin_Cost_Tracker is missing the required "
                f"{_TRACKER_COL_SIDE!r} or {_COL_MONTH!r} column.",
            ),
        )

    months_for_side = (
        df.loc[df[_TRACKER_COL_SIDE].apply(_resin_store.normalise_side) == canon,
               _COL_MONTH]
        .apply(_parse_month)
        .dropna()
    )
    if months_for_side.empty:
        return _ResinFGSelection(
            side=canon, new_month=None, old_month=None,
            warnings=(
                f"Resin_Cost_Tracker has no rows for the {canon} side — "
                "add at least one row before Refresh can build the FG.",
            ),
        )

    new_month = pd.Timestamp(months_for_side.max())
    old_month = (new_month - pd.DateOffset(months=1)).normalize().replace(day=1)

    # Strict calendar subtraction — a missing Old-month row is the
    # spec's hard-warning case (user asked for this in B1).
    side_months_set = set(months_for_side.tolist())
    warnings: list[str] = []
    if old_month not in side_months_set:
        warnings.append(
            f"Resin_Cost_Tracker is missing the Old month "
            f"({old_month:%b %Y}) for {canon} — Old Resin Cost ($/Gal) "
            "will appear blank in the FG.  Add the missing row to the "
            "lakehouse (or to the Movers Non-Milk Tracker), then click "
            "Refresh again."
        )

    return _ResinFGSelection(
        side=canon, new_month=new_month, old_month=old_month,
        warnings=tuple(warnings),
    )


def _build_two_resin_mover_fgs(
    effective_tracker_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, tuple[str, ...]]:
    """Return ``(rest_fg, topco_fg, warnings)`` from the effective tracker.

    Each side picks its OWN anchor pair via :func:`_select_resin_fg_months`
    (``new = max(Side)``, ``old = new − 1`` calendar month).  This
    decouples Rest and TOPCO so an operator who has only published the
    new month for one side still sees a clean FG for the other.

    The returned ``warnings`` tuple aggregates BOTH sides' warnings —
    the orchestrator renders them as ``st.warning`` banners exactly
    once per Refresh so the operator knows precisely which row to fill
    in.
    """
    rest_sel = _select_resin_fg_months(effective_tracker_df, side=_TRACKER_SIDE_REST)
    topco_sel = _select_resin_fg_months(effective_tracker_df, side=_TRACKER_SIDE_TOPCO)

    def _build_or_empty(sel: _ResinFGSelection) -> pd.DataFrame:
        if sel.new_month is None or sel.old_month is None:
            return pd.DataFrame(columns=list(_FG_OUTPUT_COLUMNS))
        return _build_resin_mover_fg_from_tracker(
            effective_tracker_df,
            old_month=sel.old_month,
            new_month=sel.new_month,
            side=sel.side,
        )

    rest_fg  = _build_or_empty(rest_sel)
    topco_fg = _build_or_empty(topco_sel)

    return rest_fg, topco_fg, (*rest_sel.warnings, *topco_sel.warnings)


# 6b. Tag → tracker-column resolution ─────────────────────────────────────────
#
# These helpers turn a site_item_volume Tag string (e.g. "Costco HTST") into
# the matching column in the editable Movers_Non_Milk_Tracker. Matching is
# substring-based, case-insensitive, and (for resin) honours the rule:
#   * default: column header contains "Resin", excludes "Rest"/"TOPCO";
#   * special case: Tag "Costco HTST" maps to the "Pkg Mover" column.

def _tag_norm(tag) -> str:
    """Lower-case + whitespace-trim a Tag string (NaN → empty)."""
    if tag is None or (isinstance(tag, float) and pd.isna(tag)):
        return ""
    return str(tag).strip().lower()


def _resolve_freight_column_for_tag(
    tracker_columns: list[str],
    tag: str,
) -> Optional[str]:
    """Return the tracker column for the freight mover associated with ``tag``.

    Rule: column header contains both ``tag`` (case-insensitive substring)
    and the word ``"freight"``. The first match wins; in practice the
    tracker schema produces exactly one match per tag.
    """
    t = _tag_norm(tag)
    if not t:
        return None
    for col in tracker_columns:
        cn = col.lower()
        if "freight" in cn and t in cn:
            return col
    return None


def _resolve_resin_column_for_tag(
    tracker_columns: list[str],
    tag: str,
) -> Optional[str]:
    """Return the tracker column for the *direct* resin mover for ``tag``.

    Rules (in order):
      1. **Special case** — Tag = ``"Costco HTST"`` resolves to the column
         containing both ``"costco htst"`` and ``"pkg"`` (Costco HTST has no
         resin mover column; the Pkg Mover stands in for it per the May-2026
         spec).
      2. **Default** — column header contains ``"resin"`` AND the tag, but
         does NOT contain ``"rest"`` or ``"topco"`` (those families are
         handled via FG fallback instead).

    Returns ``None`` when no column matches; the caller then falls back to
    the FG lookup (Rest / TOPCO) or leaves Resin Mover blank.
    """
    t = _tag_norm(tag)
    if not t:
        return None

    if t == "costco htst":
        for col in tracker_columns:
            cn = col.lower()
            if "costco htst" in cn and "pkg" in cn:
                return col
        return None

    for col in tracker_columns:
        cn = col.lower()
        if "resin" not in cn:
            continue
        if "rest" in cn or "topco" in cn:
            continue
        if t in cn:
            return col
    return None


def _last_row_value(
    tracker_df: pd.DataFrame,
    column: Optional[str],
) -> Optional[float]:
    """Return the numeric value of ``column`` in the last row, or ``None``."""
    if column is None or tracker_df.empty or column not in tracker_df.columns:
        return None
    val = tracker_df.iloc[-1][column]
    if pd.isna(val):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# 6c. mover_details_tables ──────────────────────────────────────────────────────────
#
# The Monthly Pricing pack uses a single mover_details_table — one copy of
# site_item_volume with Freight, Resin, and Milk movers ($/Gal) plus their
# Monthly equivalents appended. The $/Gal columns sit grouped at the END per
# the May-2026 product spec.
#
# The Milk Mover columns are LAYERED on top of the base mover_details_table at render
# time so they react to the time slicer above the Milk Commodity Cost chart
# without requiring another Refresh click.

def _fg_resin_lookup_by_desc(fg_df: pd.DataFrame) -> dict[str, float]:
    """Build a ``{normalised product description → Resin Mover ($/Gal)}`` dict.

    Uses the *mean* when multiple products normalise to the same description
    (rare; defensive). Empty descriptions are dropped so they never match.

    Column references match the canonical FG schema (``_FG_COL_MOVER``,
    ``_COL_PRODUCT_DESC``); a single source of truth keeps this lookup in
    sync with the FG builder.
    """
    if fg_df is None or fg_df.empty:
        return {}
    if _COL_PRODUCT_DESC not in fg_df.columns or _FG_COL_MOVER not in fg_df.columns:
        return {}
    fg = fg_df.copy()
    fg["_match_key"] = fg[_COL_PRODUCT_DESC].map(_normalize_desc)
    fg = fg[fg["_match_key"] != ""]
    grouped = (
        fg.groupby("_match_key", as_index=False)[_FG_COL_MOVER].mean()
    )
    return dict(zip(grouped["_match_key"], grouped[_FG_COL_MOVER]))


# ── mover_details_table builder (no-milk base) ──────────────────────────────────
#
# Canonical column names for the mover_details_table. Defined once so the builder,
# the milk layer, and consumers all reference a single source of truth.
_BK_COL_MONTH        = _mover_details_store.COL_MONTH
_BK_COL_FREIGHT_GAL  = "Freight Mover $/Gal"
_BK_COL_RESIN_GAL    = "Resin Mover $/Gal"
_BK_COL_MILK_GAL     = "Milk Mover $/Gal"
_BK_COL_MONTHLY_FRT  = "Monthly Freight Mover"
_BK_COL_MONTHLY_RES  = "Monthly Resin Mover"
_BK_COL_MONTHLY_MILK = "Monthly Milk Mover"

# $/Gal columns are grouped together at the END of the final mover_details_table per the
# May-2026 spec. The Monthly columns sit just before them so the source-of-
# truth ($/Gal drivers) and their derived per-row totals stay visually paired.
_BK_GAL_COLUMNS:     tuple[str, ...] = (
    _BK_COL_FREIGHT_GAL, _BK_COL_RESIN_GAL, _BK_COL_MILK_GAL,
)
_BK_MONTHLY_COLUMNS: tuple[str, ...] = (
    _BK_COL_MONTHLY_FRT, _BK_COL_MONTHLY_RES, _BK_COL_MONTHLY_MILK,
)


def _build_mover_details_table_no_milk(
    site_item_volume_df: pd.DataFrame,
    movers_non_milk_df: pd.DataFrame,
    rest_htst_resin_mover_fg: pd.DataFrame,
    topco_resin_mover_fg: pd.DataFrame,
    editing_month: pd.Timestamp,
) -> pd.DataFrame:
    """Single mover_details_table with Freight + Resin movers ($/Gal) and monthly totals.

    The milk columns are *not* added here — they are layered separately by
    :func:`_layer_milk_on_mover_details_table` so they react to the time-slicer without a
    Refresh.

    A leading ``Month`` column is stamped with ``editing_month`` (the
    last-row Month of the editable Movers Non-Milk Tracker) so the mover
    details table can be appended to the cumulative Pricing Lakehouse copy
    without losing track of which month each row was generated for.

    Per-row contract::

        Month                 = editing_month (e.g. 2026-05-01) on every row
        Freight Mover $/Gal   = tracker.last_row[Tag-matched freight column]
        Resin Mover $/Gal     = tracker.last_row[Tag-matched resin column], with
                                FG fallback (rest_htst / topco_resin_mover_fg)
                                when the direct lookup is blank for Rest HTST /
                                TOPCO tags.
        Monthly Freight Mover = Monthly Gallons × Pricing Method × Freight Mover $/Gal
        Monthly Resin Mover   = Monthly Gallons × Resin Mover $/Gal
    """
    base = _strip_df_columns(site_item_volume_df).copy()

    # Stamp the Month column FIRST so the column appears at the leading
    # edge of the mover_details_table even when the rest of the schema is
    # short-circuited by the missing-Tag defensive branch below.  ISO
    # first-of-month keeps duplicate detection in the lakehouse store
    # trivial (string equality).
    month_str = pd.Timestamp(editing_month).strftime("%Y-%m-01")
    base.insert(0, _BK_COL_MONTH, month_str)

    # Defensive default when Tag is missing — keeps the schema stable for the
    # downstream milk-layering step regardless of source-file variants. Only
    # the four non-milk columns are seeded; milk is added later.
    if "Tag" not in base.columns:
        for col in (
            _BK_COL_MONTHLY_FRT, _BK_COL_MONTHLY_RES,
            _BK_COL_FREIGHT_GAL, _BK_COL_RESIN_GAL,
        ):
            base[col] = float("nan")
        return base

    tracker_cols = list(movers_non_milk_df.columns)
    tag_clean    = base["Tag"].astype(str).str.strip()
    unique_tags  = tag_clean.dropna().unique().tolist()

    # ── Freight Mover $/Gal (direct tracker lookup, cached per unique tag) ──
    tag_to_freight: dict[str, Optional[float]] = {
        tag: _last_row_value(
            movers_non_milk_df,
            _resolve_freight_column_for_tag(tracker_cols, tag),
        )
        for tag in unique_tags
    }
    freight_mover = pd.to_numeric(tag_clean.map(tag_to_freight), errors="coerce")

    # ── Resin Mover $/Gal (direct lookup + FG fallback) ─────────────────────
    tag_to_resin: dict[str, Optional[float]] = {
        tag: _last_row_value(
            movers_non_milk_df,
            _resolve_resin_column_for_tag(tracker_cols, tag),
        )
        for tag in unique_tags
    }
    direct = pd.to_numeric(tag_clean.map(tag_to_resin), errors="coerce")

    rest_lookup  = _fg_resin_lookup_by_desc(rest_htst_resin_mover_fg)
    topco_lookup = _fg_resin_lookup_by_desc(topco_resin_mover_fg)
    desc_col     = "PRODUCTDESC" if "PRODUCTDESC" in base.columns else None
    desc_keys    = (
        base[desc_col].map(_normalize_desc)
        if desc_col else pd.Series([""] * len(base), index=base.index)
    )
    tag_norm   = base["Tag"].apply(_tag_norm)
    rest_vals  = desc_keys.map(rest_lookup)
    topco_vals = desc_keys.map(topco_lookup)

    fallback = pd.Series([pd.NA] * len(base), index=base.index, dtype="object")
    rest_mask  = (tag_norm == "rest htst")
    topco_mask = tag_norm.isin(["topco", "topco htst"])
    fallback.loc[rest_mask]  = rest_vals.loc[rest_mask]
    fallback.loc[topco_mask] = topco_vals.loc[topco_mask]
    fallback   = pd.to_numeric(fallback, errors="coerce")

    resin_mover = pd.to_numeric(direct.where(direct.notna(), fallback), errors="coerce")

    # ── Monthly totals + final placement ────────────────────────────────────
    # Coerce both factors to numeric float64 first so a stray ``None`` from
    # _parse_gallons_cell can never produce an object-dtype Series whose
    # arithmetic with float64 risks unexpected NaN propagation.
    monthly_gal = pd.to_numeric(
        base["Monthly Gallons"].apply(_parse_gallons_cell)
        if "Monthly Gallons" in base.columns
        else pd.Series([float("nan")] * len(base), index=base.index),
        errors="coerce",
    )
    pricing_method = (
        pd.to_numeric(base["Pricing Method"], errors="coerce")
        if "Pricing Method" in base.columns
        else pd.Series([float("nan")] * len(base), index=base.index)
    )

    base[_BK_COL_MONTHLY_FRT] = (monthly_gal * pricing_method * freight_mover).round(2)
    base[_BK_COL_MONTHLY_RES] = (monthly_gal * resin_mover).round(2)
    base[_BK_COL_FREIGHT_GAL] = freight_mover.round(4)
    base[_BK_COL_RESIN_GAL]   = resin_mover.round(4)
    return base


# ── Milk pipeline (slicer-driven) ────────────────────────────────────────────

def _latest_milk_scrape_fraction(scrape_tracker_df: pd.DataFrame) -> float:
    """Latest 'Milk' scrape fraction from ``Scrape_Tracker`` (defaults 0.0).

    Per the May-2026 spec the Milk scrape value is read from the *last* row
    of the file (no month filtering), column ``Milk``. A blank or missing
    column collapses to 0.0 so the pipeline never raises on partial inputs.
    """
    if scrape_tracker_df is None or scrape_tracker_df.empty:
        return 0.0
    if "Milk" not in scrape_tracker_df.columns:
        return 0.0
    val = _parse_percent(scrape_tracker_df.iloc[-1]["Milk"])
    return float(val) if val is not None else 0.0


# Type alias for the four (Category, Class) → rate values returned by
# :func:`_milk_rate_lookup_for_month`. Tuple order is intentional and
# referenced by index throughout the milk pipeline:
#   0 → Skim Rate
#   1 → Butterfat Rate
#   2 → Protein Rate
#   3 → Other Solids Rate
_MilkRateTuple = tuple[
    Optional[float], Optional[float], Optional[float], Optional[float]
]
_MILK_RATES_NONE: _MilkRateTuple = (None, None, None, None)


def _normalise_milk_usage_class(raw: object) -> str:
    """Map usage-table ``Class`` labels onto FMMO tracker tokens (``I`` / ``II``).

    ``Milk_Usage_Stable`` historically spells milk class as full phrases
    (e.g. ``"Class II"``) while ``fmmo_tracker`` rows carry the terse
    token ``"II"``.  Without this shim, lookups build keys like
    ``("COTTAGE CHEESE", "CLASS II")`` which never match
    ``("COTTAGE CHEESE", "II")``, so Start/End butterfat columns for
    Cottage Cheese silently read as blank and ``fillna(0)`` washes
    the milk mover arithmetic.
    """
    s = str(raw).strip().upper()
    if s in {"I", "1", "CLASS I", "CLASS 1"}:
        return "I"
    if s in {"II", "2", "CLASS II", "CLASS 2"}:
        return "II"
    return s


def _patch_cottage_cheese_bfat_from_esl_class_ii(
    keys: list[tuple[str, str]],
    rate_tuples: list[_MilkRateTuple],
    month_lookup: dict[tuple[str, str], _MilkRateTuple],
) -> list[_MilkRateTuple]:
    """Force Cottage Cheese **skim AND butterfat** to mirror ESL Class II.

    Business rule (May-2026 milk pipeline): Cottage Cheese II shares the
    same skim/butterfat rates as ESL Class II for the same month — these
    two fields are NEVER scraped independently for Cottage Cheese.  Only
    Protein Rate and Other Solids Rate are taken from the CC tracker row.

    The function is the runtime safety net layered ON TOP OF the
    write-side enforcement in ``milk_mover_autoupdate._derive_rows`` and
    the one-shot in-place repair pass in
    ``milk_mover_store.patch_cottage_cheese_rates``: even if a CC row
    in the lakehouse still carries a stale or null skim/bfat (e.g.
    written before the contract was tightened), the cost pipeline below
    will see the canonical ESL II values.

    Naming is preserved (``..._bfat_from_esl_class_ii``) only to avoid a
    cross-cutting rename of every call-site; behaviour now covers both
    rate slots as described above.
    """
    esl_tuple = month_lookup.get(("ESL", "II"), _MILK_RATES_NONE)
    esl_skim = esl_tuple[0]
    esl_bf   = esl_tuple[1]
    patched: list[_MilkRateTuple] = []
    for key, tup in zip(keys, rate_tuples):
        if key[0] == "COTTAGE CHEESE":
            # Mirror skim/bfat field-by-field — never overwrite a
            # numeric ESL value with None, never overwrite a present CC
            # value with None either (so a future divergence wins gracefully).
            cc_skim = tup[0] if tup[0] is not None else esl_skim
            cc_bf   = tup[1] if tup[1] is not None else esl_bf
            # When ESL II carries a value, we PREFER it as the source of
            # truth for CC II to match the May-2026 contract verbatim.
            if esl_skim is not None:
                cc_skim = esl_skim
            if esl_bf is not None:
                cc_bf = esl_bf
            patched.append((cc_skim, cc_bf, tup[2], tup[3]))
        else:
            patched.append(tup)
    return patched


def _milk_rate_lookup_for_month(
    milk_mover_tracker_df: pd.DataFrame,
    target_month: Optional[pd.Timestamp],
) -> dict[tuple[str, str], _MilkRateTuple]:
    """Return ``{(Category, Class) → (Skim, Butterfat, Protein, OtherSolids)}``.

    Lookup keys are upper-cased + whitespace-trimmed for tolerant matching
    against ``Milk_Usage_Stable``. Returns an empty dict (so downstream
    rates collapse to ``None``) when the source file lacks the expected
    columns or has no rows for ``target_month``.

    Column resolution is case- and whitespace-tolerant so header drift
    (``"Skim Rate "``, ``"Protein Rate $"``) still maps to the expected
    metric. The Protein / Other Solids columns are only present for
    rows inserted on/after the May-2026 schema bump; on older rows they
    read back as ``NaN`` and become ``None`` in the tuple.
    """
    if target_month is None:
        return {}
    df = _strip_df_columns(milk_mover_tracker_df).copy()
    required = {"Month", "Category", "Class"}
    if not required.issubset(df.columns):
        return {}

    df["_month_dt"] = df["Month"].apply(_parse_month)
    matched = df[df["_month_dt"] == pd.Timestamp(target_month)]
    if matched.empty:
        return {}

    # Resolve each metric column tolerantly. "other solids" is matched
    # FIRST so it can't be greedily absorbed by a future column that
    # contains "solids" alone. Skim is matched with a negative match
    # against "non-fat solids" defensively (none currently exists, but
    # the FMMO PDF uses that wording elsewhere — cheap insurance).
    def _resolve(predicate) -> Optional[str]:
        return next((c for c in df.columns if predicate(c.lower())), None)

    other_col = _resolve(lambda c: "other solids" in c)
    protein_col = _resolve(lambda c: "protein" in c)
    bf_col      = _resolve(lambda c: "butter" in c)
    skim_col    = _resolve(
        lambda c: "skim" in c and "nonfat" not in c and "non-fat" not in c
    )

    out: dict[tuple[str, str], _MilkRateTuple] = {}
    for _, row in matched.iterrows():
        key = (
            str(row["Category"]).strip().upper(),
            _normalise_milk_usage_class(row["Class"]),
        )
        out[key] = (
            _parse_money(row[skim_col])    if skim_col    else None,
            _parse_money(row[bf_col])      if bf_col      else None,
            _parse_money(row[protein_col]) if protein_col else None,
            _parse_money(row[other_col])   if other_col   else None,
        )
    return out


# Canonical column names added by _build_milk_usage_with_movers. Defined
# here so the builder, the layering step, downstream Mover-Downloads
# publishing, and any future consumer all reference exactly one source.
# Order matters: the deterministic-column-order reindex at the bottom of
# the builder uses these literals.
_MUM_COL_START_SKIM           = "Start Month Skim Rate"
_MUM_COL_START_BF             = "Start Month Butterfat Rate"
_MUM_COL_START_PROTEIN        = "Start Month Protein Rate"
_MUM_COL_START_OTHER_SOLIDS   = "Start Month Other Solids Rate"
_MUM_COL_START_COST           = "Start Month Milk Cost"
_MUM_COL_END_SKIM             = "End Month Skim Rate"
_MUM_COL_END_BF               = "End Month Butterfat Rate"
_MUM_COL_END_PROTEIN          = "End Month Protein Rate"
_MUM_COL_END_OTHER_SOLIDS     = "End Month Other Solids Rate"
_MUM_COL_END_COST             = "End Month Milk Cost"
_MUM_COL_MILK_COST_GAL        = "Milk Cost Mover $/Gal"

# Canonical column order for the published ``milk_mover.csv``. Pulls the
# stable upstream columns first (preserving the exact left-side header
# the user updated in the OneLake Milk_Usage_Stable.csv), then the per-
# month rates grouped together with their cost, then the headline mover.
# Any extra columns that happen to be on the input frame (defensively
# tolerated) get appended at the END so we never silently lose data.
_MUM_INPUT_COLUMNS: tuple[str, ...] = (
    "Item", "Item Description", "Class", "Category",
    "Skim Usage", "Butterfat Usage", "Protein Usage", "Other Solids Usage",
)
_MUM_OUTPUT_ORDER: tuple[str, ...] = _MUM_INPUT_COLUMNS + (
    _MUM_COL_START_SKIM, _MUM_COL_START_BF,
    _MUM_COL_START_PROTEIN, _MUM_COL_START_OTHER_SOLIDS,
    _MUM_COL_START_COST,
    _MUM_COL_END_SKIM, _MUM_COL_END_BF,
    _MUM_COL_END_PROTEIN, _MUM_COL_END_OTHER_SOLIDS,
    _MUM_COL_END_COST,
    _MUM_COL_MILK_COST_GAL,
)


def _build_milk_usage_with_movers(
    milk_usage_stable_df: pd.DataFrame,
    milk_mover_tracker_df: pd.DataFrame,
    milk_scrape_fraction: float,
    start_month: Optional[pd.Timestamp],
    end_month: Optional[pd.Timestamp],
) -> pd.DataFrame:
    """Enrich ``Milk_Usage_Stable`` with Start/End month rates, costs, and Mover.

    Columns appended (in canonical order — see :data:`_MUM_OUTPUT_ORDER`)::

        Start Month Skim Rate | Start Month Butterfat Rate
        Start Month Protein Rate | Start Month Other Solids Rate
        Start Month Milk Cost
        End Month Skim Rate   | End Month Butterfat Rate
        End Month Protein Rate   | End Month Other Solids Rate
        End Month Milk Cost
        Milk Cost Mover $/Gal

    Per-row formula (Start side; End side is symmetric)::

        Start Month Milk Cost = (
              Start Skim Rate          × Skim Usage
            + Start Butterfat Rate     × Butterfat Usage
            + Start Protein Rate       × Protein Usage
            + Start Other Solids Rate  × Other Solids Usage
        ) × (1 + Milk Scrape%)

    ``Milk Cost Mover $/Gal`` = End Month Milk Cost − Start Month Milk Cost.

    Class labels on ``Milk_Usage_Stable`` rows are passed through
    :func:`_normalise_milk_usage_class` before joining so ``"Class II"``
    lines match tracker keys that use ``"II"``.  For **Cottage Cheese**
    category rows, Start and End Month **Butterfat** rates always mirror
    the ESL Class II butterfat pulled from the same month’s lookup—even
    if a tracker row drifted—see :func:`_patch_cottage_cheese_bfat_from_esl_class_ii`.

    Why ``fillna(0)`` on every multiplicative input?
        Either a missing rate (legacy row in fmmo_tracker.json that
        predates the May-2026 schema) or a missing usage (Cottage Cheese
        items have ``Skim Usage=0`` literally; future-added items might
        miss other columns) would otherwise produce ``NaN × 0 = NaN``
        which poisons the sum and blanks the whole cost. Coercing to
        zero is the desired contract: an unknown rate contributes zero
        cost. Strict validation upstream (the
        ``milk_usage_stable_store`` required-columns check) prevents
        the schema itself from drifting silently.

    Returns an empty DataFrame when the source has no rows or the required
    columns are missing — callers handle the "no milk impact" case gracefully.
    """
    out = _strip_df_columns(milk_usage_stable_df).copy()
    required = {
        "Item Description", "Class", "Category",
        "Skim Usage", "Butterfat Usage",
        "Protein Usage", "Other Solids Usage",
    }
    if out.empty or not required.issubset(out.columns):
        return pd.DataFrame()

    start_lookup = _milk_rate_lookup_for_month(milk_mover_tracker_df, start_month)
    end_lookup   = _milk_rate_lookup_for_month(milk_mover_tracker_df, end_month)

    # Vectorised (Category, Class) key construction — one upper-cased pair
    # per row, used to fetch both Start and End month rates.
    cat = out["Category"].astype(str).str.strip().str.upper()
    cls = out["Class"].map(_normalise_milk_usage_class)
    keys = list(zip(cat, cls))

    # Bulk-resolve every rate tuple once per (key, month-side). Indexing
    # tuples is materially faster than four separate ``get()[i]`` calls
    # per row on large frames.
    start_tuples = [start_lookup.get(k, _MILK_RATES_NONE) for k in keys]
    end_tuples   = [end_lookup.get(k,   _MILK_RATES_NONE) for k in keys]
    start_tuples = _patch_cottage_cheese_bfat_from_esl_class_ii(
        keys, start_tuples, start_lookup,
    )
    end_tuples = _patch_cottage_cheese_bfat_from_esl_class_ii(
        keys, end_tuples, end_lookup,
    )

    out[_MUM_COL_START_SKIM]         = [t[0] for t in start_tuples]
    out[_MUM_COL_START_BF]           = [t[1] for t in start_tuples]
    out[_MUM_COL_START_PROTEIN]      = [t[2] for t in start_tuples]
    out[_MUM_COL_START_OTHER_SOLIDS] = [t[3] for t in start_tuples]
    out[_MUM_COL_END_SKIM]           = [t[0] for t in end_tuples]
    out[_MUM_COL_END_BF]             = [t[1] for t in end_tuples]
    out[_MUM_COL_END_PROTEIN]        = [t[2] for t in end_tuples]
    out[_MUM_COL_END_OTHER_SOLIDS]   = [t[3] for t in end_tuples]

    # Coerce every multiplicand to a float Series with ``NaN → 0`` so
    # missing rates / usages contribute zero (see docstring above).
    def _num(series: pd.Series) -> pd.Series:
        return pd.to_numeric(series, errors="coerce").fillna(0.0)

    skim_usage    = _num(out["Skim Usage"])
    bf_usage      = _num(out["Butterfat Usage"])
    protein_usage = _num(out["Protein Usage"])
    other_usage   = _num(out["Other Solids Usage"])

    s_skim    = _num(out[_MUM_COL_START_SKIM])
    s_bf      = _num(out[_MUM_COL_START_BF])
    s_protein = _num(out[_MUM_COL_START_PROTEIN])
    s_other   = _num(out[_MUM_COL_START_OTHER_SOLIDS])
    e_skim    = _num(out[_MUM_COL_END_SKIM])
    e_bf      = _num(out[_MUM_COL_END_BF])
    e_protein = _num(out[_MUM_COL_END_PROTEIN])
    e_other   = _num(out[_MUM_COL_END_OTHER_SOLIDS])

    scrape_factor = 1.0 + float(milk_scrape_fraction)
    out[_MUM_COL_START_COST] = (
        (
            s_skim    * skim_usage
            + s_bf      * bf_usage
            + s_protein * protein_usage
            + s_other   * other_usage
        )
        * scrape_factor
    ).round(4)
    out[_MUM_COL_END_COST] = (
        (
            e_skim    * skim_usage
            + e_bf      * bf_usage
            + e_protein * protein_usage
            + e_other   * other_usage
        )
        * scrape_factor
    ).round(4)
    out[_MUM_COL_MILK_COST_GAL] = (
        out[_MUM_COL_END_COST] - out[_MUM_COL_START_COST]
    ).round(4)

    # Deterministic column ordering — guarantees the published
    # ``milk_mover.csv`` matches the canonical layout regardless of
    # pandas insertion behaviour. Any unexpected upstream columns are
    # preserved at the END so we never silently drop data.
    ordered = [c for c in _MUM_OUTPUT_ORDER if c in out.columns]
    extras  = [c for c in out.columns       if c not in _MUM_OUTPUT_ORDER]
    return out[ordered + extras]


def _milk_lookup_by_desc(milk_usage_with_movers_df: pd.DataFrame) -> dict[str, float]:
    """Build ``{normalised Item Description → Milk Cost Mover $/Gal}``.

    Parallels :func:`_fg_resin_lookup_by_desc`. Uses the row mean when
    multiple items collapse to the same normalised description (defensive
    only — items are unique by ID in the source file).
    """
    if (milk_usage_with_movers_df is None
            or milk_usage_with_movers_df.empty
            or _MUM_COL_MILK_COST_GAL not in milk_usage_with_movers_df.columns
            or "Item Description" not in milk_usage_with_movers_df.columns):
        return {}
    df = milk_usage_with_movers_df.copy()
    df["_match_key"] = df["Item Description"].map(_normalize_desc)
    df = df[df["_match_key"] != ""]
    if df.empty:
        return {}
    grouped = df.groupby("_match_key", as_index=False)[_MUM_COL_MILK_COST_GAL].mean()
    return dict(zip(grouped["_match_key"], grouped[_MUM_COL_MILK_COST_GAL]))


def _layer_milk_on_mover_details_table(
    mover_details_table_base: pd.DataFrame,
    milk_usage_with_movers_df: Optional[pd.DataFrame],
) -> tuple[pd.DataFrame, float]:
    """Add ``Milk Mover $/Gal`` and ``Monthly Milk Mover`` to the base mover_details_table.

    Steps
    -----
    1. Pull ``Milk Cost Mover $/Gal`` from ``milk_usage_with_movers_df`` keyed
       on item description (matched against ``PRODUCTDESC`` in the mover_details_table,
       case-insensitive).
    2. Compute ``Monthly Milk Mover = Monthly Gallons × Milk Mover $/Gal``,
       coercing both inputs to numeric float64 first so a stray ``None`` from
       gallon-cell parsing can never poison the multiplication dtype.
    3. Reorder columns so the three Monthly Movers sit together followed by
       the three $/Gal drivers grouped at the END (per the May-2026 spec).
    4. Compute the headline metric as the sum of the **final mover_details_table**'s
       ``Monthly Milk Mover`` column — this guarantees the metric and the
       downloadable CSV column always agree to the cent.

    Returns ``(final_mover_details_table, monthly_milk_impact_total)``.
    """
    out = mover_details_table_base.copy()
    milk_lookup = _milk_lookup_by_desc(milk_usage_with_movers_df)

    if not milk_lookup or "PRODUCTDESC" not in out.columns:
        # No milk data available — keep schema stable but populate NaN so the
        # download still has the column where the user would expect it.
        out[_BK_COL_MILK_GAL]     = float("nan")
        out[_BK_COL_MONTHLY_MILK] = float("nan")
    else:
        # Coerce both factors to float64 BEFORE multiplying. _parse_gallons_cell
        # returns None for blanks, which produces an object-dtype Series — that
        # combined with floats can yield surprising NaN/object propagation in
        # older pandas versions, so pin everything to numeric here.
        milk_mover_gal = pd.to_numeric(
            out["PRODUCTDESC"].map(_normalize_desc).map(milk_lookup),
            errors="coerce",
        )
        monthly_gal_raw = (
            out["Monthly Gallons"].apply(_parse_gallons_cell)
            if "Monthly Gallons" in out.columns
            else pd.Series([float("nan")] * len(out), index=out.index)
        )
        monthly_gal = pd.to_numeric(monthly_gal_raw, errors="coerce")

        out[_BK_COL_MILK_GAL]     = milk_mover_gal.round(4)
        out[_BK_COL_MONTHLY_MILK] = (monthly_gal * milk_mover_gal).round(2)

    # Final ordering: original siv columns → Monthly Movers (3) → $/Gal
    # drivers (3, grouped at the very end). Reorder BEFORE summing so the
    # headline metric is unambiguously sourced from the same column users
    # see in the downloadable CSV.
    grouped      = set(_BK_GAL_COLUMNS) | set(_BK_MONTHLY_COLUMNS)
    other_cols   = [c for c in out.columns if c not in grouped]
    monthly_pres = [c for c in _BK_MONTHLY_COLUMNS if c in out.columns]
    gal_pres     = [c for c in _BK_GAL_COLUMNS     if c in out.columns]
    final_mover_details_table = out[other_cols + monthly_pres + gal_pres]

    monthly_milk_total = float(
        pd.to_numeric(final_mover_details_table[_BK_COL_MONTHLY_MILK], errors="coerce")
        .sum(skipna=True)
    )
    return final_mover_details_table, monthly_milk_total


# 6d. Example prices enrichment ───────────────────────────────────────────────

def _resolve_price_ea_column(df: pd.DataFrame) -> Optional[str]:
    """Locate the ``Price $/EA`` column (handles minor header variants)."""
    for c in df.columns:
        cl = c.lower().replace(" ", "")
        if "price" in cl and ("$/ea" in c.lower() or "/ea" in c.lower()):
            return c
    return next((c for c in df.columns if "price" in c.lower()), None)


def _resolve_item_description_column(df: pd.DataFrame) -> Optional[str]:
    """Locate the item / product description column for joining to the FG."""
    for c in df.columns:
        cl = c.lower().replace(" ", "")
        if "item" in cl and "description" in cl:
            return c
        if cl == "productdescription":
            return c
    return next((c for c in df.columns if "description" in c.lower()), None)


def _build_example_prices_impact_table(
    example_df: pd.DataFrame,
    rest_htst_resin_mover_fg: pd.DataFrame,
    rest_htst_freight_per_gal: Optional[float],
) -> tuple[pd.DataFrame, Optional[str]]:
    """Enrich example_prices with Rest-HTST-defaulted resin & freight movers.

    * **Resin Mover $/EA** — pulled from ``rest_htst_resin_mover_fg.[resin
      mover $/gal]`` joined on item description (case-insensitive). The
      example file has no per-SKU gal/ea, so $/gal is carried into the $/EA
      column as the line-item driver (equivalent to gal/ea = 1).
    * **Freight Mover $/EA** — uniform across rows: the Rest HTST Freight
      Mover ($/Gal) value from the editable tracker's last row.
    * **Price Increase%** — ``(Resin Mover $/EA + Freight Mover $/EA) ÷ Price $/EA × 100``.

    The new columns are inserted right after the resolved ``Price $/EA``.
    """
    ex = _strip_df_columns(example_df)
    price_col = _resolve_price_ea_column(ex)
    desc_col  = _resolve_item_description_column(ex)
    if price_col is None:
        return ex, "Could not find a `Price $/EA` column in the example prices file."
    if desc_col is None:
        return ex, "Could not find an item / product description column for joining."

    out = ex.copy()
    out["_match_key"] = out[desc_col].map(_normalize_desc)

    rest_lookup = _fg_resin_lookup_by_desc(rest_htst_resin_mover_fg)
    out["Resin Mover $/EA"] = pd.to_numeric(
        out["_match_key"].map(rest_lookup), errors="coerce",
    )
    out = out.drop(columns=["_match_key"], errors="ignore")

    if rest_htst_freight_per_gal is not None and not pd.isna(rest_htst_freight_per_gal):
        out["Freight Mover $/EA"] = float(rest_htst_freight_per_gal)
    else:
        out["Freight Mover $/EA"] = float("nan")

    price      = pd.to_numeric(out[price_col], errors="coerce")
    resin_ea   = pd.to_numeric(out["Resin Mover $/EA"], errors="coerce")
    freight_ea = pd.to_numeric(out["Freight Mover $/EA"], errors="coerce")
    mover_sum  = resin_ea.fillna(0.0) + freight_ea.fillna(0.0)
    out["Price Increase%"] = (
        (mover_sum / price.replace(0, pd.NA)) * 100.0
    ).where(price.notna() & price.ne(0), other=pd.NA)

    out["Resin Mover $/EA"]   = resin_ea.round(4)
    out["Freight Mover $/EA"] = freight_ea.round(4)
    out["Price Increase%"]    = pd.to_numeric(out["Price Increase%"], errors="coerce").round(2)

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


# 6e. Top-level orchestration ─────────────────────────────────────────────────

def _compute_all_outputs(
    uploads: dict[str, _Uploaded],
    movers_non_milk_df: pd.DataFrame,
    current_month: pd.Timestamp,
) -> Optional[dict]:
    """Run the full impact pipeline. Returns ``None`` on missing inputs.

    Pipeline (May-2026-late contract)
    ---------------------------------

    1. Validate required uploads + injected lakehouse inputs.
    2. Materialise the per-(Month × Side) Resin_Cost_Tracker payload
       from the editable NMT × Resin_Calculator
       (:func:`_build_tracker_rows_for_nmt`).
    3. Merge that payload onto the persisted Resin_Cost_Tracker to form
       the in-memory "effective tracker"
       (:func:`_build_effective_resin_cost_tracker`).
    4. Build both Resin Mover FGs from the effective tracker, with
       Old/New month anchors picked PER SIDE
       (:func:`_select_resin_fg_months`): ``new = max(Side)``, ``old =
       new − 1 calendar month``.  Missing Old-month rows in the
       tracker surface as warning strings (rendered as
       ``st.warning`` banners by the orchestrator).
    5. Build the mover_details_table base (Freight + Resin only — milk
       is layered at render time so it reacts to the time slicer).
    6. Roll up the two headline Incremental Revenue totals.

    Outputs (keys in the returned dict):
      * ``rest_htst_resin_mover_fg`` — DataFrame for download.
      * ``topco_resin_mover_fg`` — DataFrame for download.
      * ``mover_details_table_base`` — DataFrame (siv + Freight + Resin movers).
        Milk columns are NOT included here — they are layered at render time
        so they react to the time slicer above the Milk Commodity chart
        without requiring another Refresh click.
      * ``monthly_freight_impact_total`` — float (headline Incremental
        Revenue vs. Last Month, freight).
      * ``monthly_resin_impact_total`` — float (headline Incremental
        Revenue vs. Last Month, resin).
      * ``rest_htst_freight_per_gal`` — Optional[float] (used by example prices).
      * ``example_prices_impact`` — Optional[DataFrame] (None when no upload).
      * ``resin_tracker_upsert_payload`` — DataFrame.  Full NMT-derived
        payload covering every ``(Month, Side)`` row the upsert should
        write.  The single Refresh writer consumes this directly.
      * ``resin_fg_warnings`` — tuple[str, ...].  Operator-actionable
        warnings about missing Old-month rows in the persisted
        tracker (one entry per affected side).  Rendered as
        ``st.warning`` banners by ``_run_refresh_lakehouse_writes``.
      * ``_meta`` — single-row diagnostics DataFrame for debugging.

    Internal dict keys ``monthly_*_impact_total`` retain the legacy
    Python identifier for downstream compatibility — only the
    user-facing label has been rebranded.
    """
    # Validate required uploads up-front.
    for role in REQUIRED_ROLES:
        if role not in uploads:
            st.error(f"❌ Missing required file: `{role}`. Please re-upload.")
            return None

    # ``resin_calculator`` and ``resin_cost_tracker`` are no longer uploaded
    # by the user — they're injected from the Pricing Lakehouse via
    # :func:`_inject_resin_inputs_from_store`.  Validate them separately so
    # the error message can route the operator to the right place
    # (lakehouse permissions / connectivity, NOT the upload panel).
    for role in ("resin_calculator", "resin_cost_tracker"):
        if role not in uploads or uploads[role].df is None or uploads[role].df.empty:
            err = st.session_state.get(_SS_RESIN_STORE_ERROR)
            detail = f"\n\nUnderlying error: {err}" if err else ""
            st.error(
                f"❌ `{role}` is not available from the Pricing Lakehouse "
                "(`Files/Resin_freight_cost_tracker/`).\n\n"
                "**Try first:** click **🔄 Refresh** again — the page "
                "drops the local 5-minute read cache on every Refresh, "
                "so a freshly-uploaded OneLake file shows up immediately.\n\n"
                "**If the error persists:** verify the "
                "`[fabric_resin_cost_tracker]` (or `[fabric_htst]`) section "
                "of `.streamlit/secrets.toml`, confirm your account has "
                "Read access to "
                "`Files/Resin_freight_cost_tracker/Resin_Calculator.csv` "
                "and `Resin_Cost_Tracker.csv`, then reload the page." + detail
            )
            return None

    if movers_non_milk_df.empty:
        st.error(
            "❌ The Movers Non-Milk Tracker is empty. Add at least one row "
            "(the LAST row drives the Incremental Revenue vs. Last Month "
            "calculations)."
        )
        return None

    # Pull the freight-side $/gal driver from the editable tracker's last
    # row — still required for the mover_details_table fallback and for
    # the example-prices enrichment.  The resin $/lbs drivers are no
    # longer read from the last row alone; the tracker-rewrite path
    # consumes the entire NMT.
    last_row = movers_non_milk_df.iloc[-1]
    rest_freight_gal = _last_row_value(movers_non_milk_df, _NMT_COL_REST_FREIGHT)

    # The "editing month" is still derived from the NMT's last row so
    # the mover_details_table carries a meaningful Month stamp.  Falls
    # back to ``current_month`` when the last-row Month is blank.
    editing_month = _parse_month(last_row[_NMT_COL_MONTH]) or current_month

    scrape_fraction = _latest_scrape_fraction(uploads["scrape_tracker"].df)

    # ── 1. Build the per-(Month × Side) Resin_Cost_Tracker payload from
    #       the editable NMT × Resin_Calculator.  Covers every NMT row
    #       that carries at least one numeric $/lbs cell.
    full_payload = _build_tracker_rows_for_nmt(
        movers_non_milk_df,
        uploads["resin_calculator"].df,
        scrape_fraction,
    )

    # ── 2. Effective tracker = persisted tracker, with every (Month,
    #       Side) pair present in the payload REPLACED by the payload's
    #       freshly-computed rows.  Sep–Nov 2025 (and any other months
    #       in the file but absent from NMT) pass through untouched.
    persisted_tracker = uploads["resin_cost_tracker"].df
    effective_tracker = _build_effective_resin_cost_tracker(
        persisted_tracker, full_payload,
    )

    # ── 3. Resin Mover FGs — per-side anchors picked from the EFFECTIVE
    #       tracker.  Each side gets its own ``new = max(Side)`` and
    #       ``old = new − 1 calendar month`` (strict subtraction).
    #       Missing Old-month rows surface as warnings rendered by the
    #       Refresh orchestrator.
    rest_fg, topco_fg, fg_warnings = _build_two_resin_mover_fgs(
        effective_tracker,
    )

    # ── 4. Resin tracker upsert payload — the single Refresh writer
    #       consumes the FULL NMT-derived payload directly.  No more
    #       Refresh-vs-Confirm split: ``upsert_for_sides`` performs
    #       overwrite-existing AND append-new in one ETag-guarded
    #       write.  Months in the file but absent from the NMT (e.g.
    #       Sep–Nov 2025) are PRESERVED verbatim — they're simply not
    #       in the payload.
    resin_tracker_upsert_payload = (
        full_payload.reset_index(drop=True)
        if full_payload is not None and not full_payload.empty
        else pd.DataFrame()
    )

    # ── 5. mover_details_table (Freight + Resin only — milk is layered
    #       at render time).  The Month column is stamped with
    #       ``editing_month`` so the cumulative Lakehouse store can
    #       dedupe by month on the Refresh-time upsert.
    combined_base = _build_mover_details_table_no_milk(
        uploads["site_item_volume"].df,
        movers_non_milk_df,
        rest_fg,
        topco_fg,
        editing_month,
    )
    monthly_freight_total = float(
        pd.to_numeric(combined_base.get(_BK_COL_MONTHLY_FRT), errors="coerce")
        .sum(skipna=True)
    )
    monthly_resin_total = float(
        pd.to_numeric(combined_base.get(_BK_COL_MONTHLY_RES), errors="coerce")
        .sum(skipna=True)
    )

    # ── 6. Optional example_prices enrichment.
    example_impact = None
    if "example_prices" in uploads:
        ex_df, ex_warn = _build_example_prices_impact_table(
            uploads["example_prices"].df, rest_fg, rest_freight_gal,
        )
        example_impact = ex_df
        if ex_warn:
            st.warning(f"⚠️ {ex_warn}")

    return {
        "rest_htst_resin_mover_fg":         rest_fg,
        "topco_resin_mover_fg":             topco_fg,
        "mover_details_table_base":         combined_base,
        # Internal dict keys retain the legacy ``*_impact_total`` Python
        # identifier so downstream consumers don't need to be edited.
        # Only the user-facing label has been rebranded — see the
        # "Incremental Revenue vs. Last Month" section of the UI render.
        "monthly_freight_impact_total":     monthly_freight_total,
        "monthly_resin_impact_total":       monthly_resin_total,
        "rest_htst_freight_per_gal":        rest_freight_gal,
        "example_prices_impact":            example_impact,
        # Single resin tracker write payload — the Refresh orchestrator
        # passes it straight to :func:`_resin_store.upsert_for_sides`.
        "resin_tracker_upsert_payload":     resin_tracker_upsert_payload,
        # Per-side FG warnings (missing Old-month rows in the tracker).
        # Rendered as ``st.warning`` banners by the Refresh
        # orchestrator so operators know exactly which row to fill in.
        "resin_fg_warnings":                fg_warnings,
        "_meta": pd.DataFrame([{
            "scrape_fraction":      scrape_fraction,
            "rest_freight_$/gal":   rest_freight_gal,
            "current_month":        current_month.strftime("%Y-%m-%d"),
            "editing_month":        editing_month.strftime("%Y-%m-%d"),
        }]),
    }


# ── 7. UI fragments ───────────────────────────────────────────────────────────

# Per-session gate: guards the one-time per-store cache invalidation that
# runs immediately after a successful Fabric sign-in is detected.  Without
# this gate the invalidation would re-fire on every Streamlit rerun while
# the module is still within the same session.
#
# NOTE: The device-code sign-in flow itself (URL + code + "Check status"
# button) was moved to the Home & Fabric Sign-in page and lives in
# utils/fabric_signin_widget.py.  This module no longer owns that UI.
_SS_FABRIC_RECOVERY_DONE = f"{_SS_PREFIX}_fabric_recovery_done"


def _recover_after_fabric_signin() -> None:
    """Drop every stale state slot left behind by a failed-auth render.

    Called exactly once per successful sign-in transition (gated by
    :data:`_SS_FABRIC_RECOVERY_DONE`).  Without this helper the page
    would still render its old "Microsoft Fabric not connected" panel
    captions even after the user successfully signed in, because:

    * The per-store ``@st.cache_data`` readers had pinned a ``None``
      / empty answer during the failed render (TTL 5 min).
    * The once-per-session retry-bypass guards (resin store
      ``_SS_BYPASS_PREFIX``, milk-mover store ``_SS_BYPASS_KEY``)
      were already set, so subsequent reads short-circuit to the
      cached empty answer instead of trying OneLake.
    * Session-state error markers (``_SS_RESIN_STORE_ERROR``,
      ``_SS_MILK_AUTOUPDATE_RESULT``) still hold the failed-auth text.
    * The "we already ran the auto-update tick this session" gate
      prevents a fresh USDA/PDF check from firing.

    Each store's public ``invalidate_read_cache()`` already knows how
    to sweep its own bypass flags, so we just have to call it; the rest
    is straightforward session-state cleanup.
    """
    # Drop the process-wide auth failure cache so the next read against
    # ANY Fabric-backed store re-exercises the credential chain — even
    # if the failure was recorded just seconds ago.  Without this the
    # 60-second TTL keeps surfacing "Microsoft Fabric not connected"
    # banners for stores whose first read happens shortly after the
    # user successfully signs in (e.g. the COLA section in Market
    # Barometer, which only reads once the user expands its panel).
    try:
        _fabric_auth.reset_auth_failure_cache()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass

    # Per-store cache busts — each clears its own ``@st.cache_data``
    # frames AND any once-per-session bypass flag it owns.
    for invalidator in (
        _resin_store.invalidate_read_cache,
        _milk_store.invalidate_read_cache,
        _milk_usage_store.invalidate_read_cache,
        _cola_store.invalidate_read_cache,
    ):
        try:
            invalidator()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass

    # Drop session-state error markers + per-session "we ran setup
    # already" gates so the next render gets a clean slate.
    for key in (
        _SS_RESIN_STORE_ERROR,
        _SS_MILK_AUTOUPDATE_RESULT,
        _SS_MILK_BOOTSTRAP_TRIED,
        _SS_MILK_AUTOUPDATE_TICK_RAN,
        _SS_MILK_USAGE_SEED_DONE,
    ):
        st.session_state.pop(key, None)


def _require_fabric_auth() -> bool:
    """Gate on Fabric auth; return True if auth is broken (caller should stop rendering).

    Checks auth status and runs per-store cache recovery when a sign-in that
    was completed on the **Home & Fabric Sign-in** page is detected for the
    first time in this session.

    The interactive sign-in UI (device-code URL, "Check status" button, retry
    button) now lives exclusively on the Home & Fabric Sign-in page
    (``utils/fabric_signin_widget.py``).  This function no longer renders any
    sign-in widgets — it only gates rendering and runs the per-store recovery
    that is specific to this module.

    State machine (first matching branch wins)
    ------------------------------------------
    A. **Sign-in just succeeded** — run per-store cache recovery once per
       session, reset device-code state, return False (render normally).
    B. **Auth healthy** — return False immediately.
    C. **Auth broken** — show a concise warning directing the user to
       Home & Fabric Sign-in, return True (stop rendering this section).
    """
    status = _fabric_auth.device_code_signin_status()
    err    = _fabric_auth.cached_auth_error()

    # ── (A) Sign-in just succeeded — run per-store recovery once ─────────────
    # The sign-in was completed on the Home & Fabric Sign-in page.  Detect it
    # via the process-wide device_code_signin_status() and run the per-store
    # invalidation exactly once per session so stale empty-cache frames from
    # the pre-sign-in render are evicted before this module tries to read them.
    if status["state"] == "success":
        if not st.session_state.get(_SS_FABRIC_RECOVERY_DONE):
            st.session_state[_SS_FABRIC_RECOVERY_DONE] = True
            _recover_after_fabric_signin()
        # Reset to "idle" so this branch doesn't re-fire on subsequent renders.
        # reset_device_code_signin() is a no-op while the worker is alive, but
        # the worker exits before writing "success", so this always succeeds.
        _fabric_auth.reset_device_code_signin()
        return False

    # ── (B) Auth healthy — render the module normally ─────────────────────────
    if err is None:
        return False

    # ── (C) Auth broken — direct user to the centralized sign-in page ─────────
    st.warning(
        "🔒 **Microsoft Fabric is not connected.**\n\n"
        "Please visit **Home & Fabric Sign-in** in the sidebar to sign in. "
        "Once signed in, return here — this module will load automatically."
    )
    return True


def _render_monthly_sop_and_upload_intro() -> None:
    """SharePoint guidance + Monthly SOP + foldable workflow details.

    Layout
    ------
    1. SharePoint folder link + the always-visible Monthly SOP — these
       are the at-a-glance instructions every user needs on every visit.
    2. Two collapsed-by-default :class:`st.expander` blocks containing
       the more detailed automated-workflow narratives (resin + milk).
       Folding keeps the page tight on first load while preserving the
       full reference text for users who want it.
    """
    st.markdown(
        "Upload all files in this SharePoint Folder "
        f"([LINK]({_MONTHLY_MOVERS_SHAREPOINT_URL}))."
    )
    st.markdown(
        """
**Monthly SOP**

- Maintain and refresh source files (**DO NOT change file names**):
  - `Pkg_Index` (Procurement)
  - `example_prices` (RGM, update pricing)
  - `Resin_Cost_Tracker` (RGM — sourced from the Pricing Lakehouse;
    no upload needed.  Updated automatically on **Refresh**.)
- Use the **Movers Non-Milk Tracker** below to align mover values with
  commercial leaders.
  - Click **🔄 Refresh** to recompute the Incremental Revenue vs. Last
    Month metrics AND update every dependent OneLake artefact under
    that artefact's own gate (`Resin_Cost_Tracker`, the two Resin
    Mover FGs, `Product_Milk Base Cost`, the four Mover Downloads,
    `mover_details_table`, `base_milk_cost_monthly_tracker`).  See
    the workflow expander below for the per-file gates.
  - Click **🔄 USDA refresh** (above the Milk Commodity chart) to
    check USDA for a new advance-prices PDF outside the 1-hour
    cooldown.  A new month is appended to `fmmo_tracker.json` ONLY
    when the freshly-scraped rates actually differ from the latest
    month already in the file.

_Files are matched automatically by filename keyword after upload._
        """.strip()
    )

    with st.expander("ℹ️ Automatic resin-cost workflow", expanded=False):
        st.markdown(
            """
Both `Resin_Calculator.csv` and `Resin_Cost_Tracker.csv` are sourced from
the Pricing Lakehouse — no upload needed.

#### Tracker schema

`Resin_Cost_Tracker.csv` carries a leading **`Rest Market vs TOPCO`**
dimension immediately before `Product ID`, so each month materialises
**two** rows per Product ID — one for the Rest-of-Market HTST resin
cost, one for the TOPCO HTST resin cost.  The column order is::

    Rest Market vs TOPCO | Product ID | Product Description |
    Resin | Resin Cost ($/Gal) | Month | Pricing Category | ...

Both the Rest and TOPCO `$/Gal` values are derived through the same
formula::

    Resin Cost ($/Gal) = $/lbs × Usage (Lbs/Ea) × (1 + Scrape%) ÷ Gal/Ea

where `$/lbs` is pulled from the matching Movers Non-Milk Tracker (NMT)
row — `Rest HTST Resin Cost ($/lbs)` for Rest,
`TOPCO HTST Resin Cost ($/lbs)` for TOPCO.

#### Single-Refresh write contract (May-2026-late)

| Trigger | What the writer does | Scope |
|---|---|---|
| **🔄 Refresh** | One ETag-guarded **upsert** keyed by `(Month, Side)`.  For every NMT row × side, drops the matching persisted rows and concats the freshly-computed payload onto the survivors. | Overwrites existing `(Month, Side)` keys AND appends new ones in the same call.  Persisted rows whose key is NOT in the payload (e.g. Sep–Nov 2025) are PRESERVED verbatim. |

#### Resin Mover FGs

`rest_htst_resin_mover_fg.csv` and `topco_resin_mover_fg.csv` are
regenerated on every Refresh from the just-upserted tracker.  Anchors
are picked **per side**:

* **`new_month_<side>`** = `max(Month where Rest Market vs TOPCO == <side>)`
* **`old_month_<side>`** = `new_month_<side> − 1 calendar month`
  (strict calendar subtraction — NOT "the next-newest month present")

Per Product ID::

    Resin Mover ($/Gal) = New Resin Cost ($/Gal) − Old Resin Cost ($/Gal)

If the tracker is missing the `old_month_<side>` row, the FG renders
that side's `Old Resin Cost ($/Gal)` as blank AND surfaces an
actionable warning above the table — fill the missing row into
`Resin_Cost_Tracker.csv` (or add it to the NMT and Refresh again).

#### Mover Downloads + cumulative trackers (downstream)

* The four Mover Downloads (`rest_htst_resin_mover_fg.csv`,
  `topco_resin_mover_fg.csv`, `milk_mover.csv`,
  `Movers_Non_Milk_Tracker.csv`) are published to
  `Files/Monthly_Pricing_Execution/` on every Refresh —
  authoritative replace, no month gate.
* `mover_details_table.csv` is upserted on **Refresh** but ONLY
  when a new row was inserted into the Movers Non-Milk Tracker
  since the last successful publish (row-count delta).  Existing
  month data is OVERWRITTEN when the gate is open.  Edit-in-place
  on the existing last row does NOT qualify and renders a small
  no-op caption — that's what keeps Refresh-iteration safe.

#### Out of scope

The Walmart, Costco HTST and Costco KS sites do NOT participate in
`Resin_Cost_Tracker.csv` upserts — their resin mover columns in the
NMT live on for the per-row mover_details_table fallback but never
reach the Side-keyed tracker.
            """.strip()
        )

    with st.expander("ℹ️ Automatic milk-cost workflow", expanded=False):
        st.markdown(
            """
The milk pipeline is fully automated end-to-end — no manual file
upload required for any milk artefact.  The matrix below names every
lakehouse file the workflow touches, the exact trigger that fires the
write, and the gate conditions that must hold before the write lands.

Two buttons partition the work:

* **🔄 USDA refresh** (above the Milk Commodity chart) checks the
  USDA advanced-prices PDF for a new month and appends `fmmo_tracker.json`
  ONLY when the freshly-scraped rates differ from the latest month
  already in the file.  Existing rows are never mutated.
* **🔄 Refresh** (below the NMT) updates every downstream artefact
  under its own gate (see rows 2–5 below).

#### Per-file trigger / condition matrix

| # | Lakehouse file | Trigger | Conditions that must hold |
|---|---|---|---|
| 1 | `Files/Milk_cost_tracker/fmmo_tracker.json` | USDA publishes a new [Advanced Prices PDF](https://www.ams.usda.gov/mnreports/dymadvancedprices.pdf), or user clicks **🔄 USDA refresh** | **System scrapes all rates from the advance-prices PDF and compares them against the matching cells of the latest month already in `fmmo_tracker.json`.**  A new month row is appended (labelled `max(file) + 1 calendar month`, always first-of-month) ONLY when at least one of the **six canonical advance-prices-driven cells** differs — HTST I Skim & Bfat, HTST II Skim, ESL I Skim, CC II Protein, CC II Other Solids.  HTST II / ESL II / CC II Butterfat are sourced from [dymclassprices.pdf](https://www.ams.usda.gov/mnreports/dymclassprices.pdf) (always the latest published row on page 2) but do NOT gate writes — only the advance-prices PDF triggers a new month.  All other cells are derived by spec (ESL II / CC II Skim+Bfat mirror HTST II, ESL I Bfat mirrors HTST I).  **No change is ever made to existing rows.**  If the class-prices PDF is unreachable / unparseable the new row still writes with Class II Bfat = NULL and a warning banner asks the operator to fill the cell into the lakehouse manually. |
| 2 | `Files/Monthly_Pricing_Execution/milk_mover.csv` | User clicks **🔄 Refresh** | Authoritative replace on every Refresh — no month gate.  When the slicer's End Month has no rows in `fmmo_tracker.json`, rates collapse to zero (silent `$0` cost) so the published file structure stays stable. |
| 3 | `Files/Activity_Model/Product_Milk Base Cost.csv` (column `Base Milk Cost per Gallon`) | User clicks **🔄 Refresh** | Slicer's **End Month ≥ file's max Month + 1 calendar month**.  Update is by Item match (left-merge); unmatched rows are stale-stamped. |
| 4 | `Files/Monthly_Mover_Reporting/mover_details_table.csv` | User clicks **🔄 Refresh** | A **new row was inserted** into the Movers Non-Milk Tracker since the last successful publish (row-count delta).  Edit-in-place on the existing last row does NOT qualify.  **Existing months may be overwritten** when the gate is open — the upsert wins for the current editing month. |
| 5 | `Files/Milk_cost_tracker/base_milk_cost_monthly_tracker.csv` | User clicks **🔄 Refresh** | Slicer's **End Month = Start Month + 1 calendar month**.  **Existing months may be overwritten** — the upsert wins for the slicer's End Month. |

#### Operator-facing checklist

1. **Wait for USDA**: if the End Month you need isn't in the Milk
   Commodity slicer, the advanced-prices PDF hasn't yet published
   different rates than the latest month on file.  Click **🔄 USDA
   refresh** to re-check.  Class II Butterfat is sourced from
   page 2 of the class-prices PDF (always the latest row) and does
   NOT gate writes — if the class-prices PDF is unreachable the
   new month still lands with Class II Bfat = NULL and an amber
   warning will point you at the cell to fill in manually.
2. **Pick `(Start Month, End Month)` in the slicer**.  For the
   `base_milk_cost_monthly_tracker` upsert to fire, the pair must be
   exactly one calendar month apart.
3. **Edit the Movers Non-Milk Tracker** below.  To trigger the
   `mover_details_table` upsert you must **insert a new row** for the
   new month — editing the existing last row is not enough.
4. **Click Refresh**.  Every downstream artefact runs under its own
   gate; each write that runs / skips / fails surfaces a small
   caption.  Iterate freely — the row-count gate on
   `mover_details_table` and the calendar-adjacency gate on
   `base_milk_cost_monthly_tracker` together prevent accidental
   pollution of the cumulative audit history.
5. **HTST pricing unlocks**: the New Price Quote view reads
   `Product_Milk Base Cost.csv` directly, so the new $/gal flows
   through immediately after step 4 — no calendar-rollover wait
   required.

All three milk artefacts (`fmmo_tracker.json`,
`base_milk_cost_monthly_tracker.csv`, and the canonical drop-zone
`milk_mover.csv`) live together in `Files/Milk_cost_tracker/` and
`Files/Monthly_Pricing_Execution/` in the Pricing Lakehouse for
auditability.
            """.strip()
        )


def _render_upload_panel() -> list:
    """Render the multi-file uploader."""
    return st.file_uploader(
        "Select Monthly Movers CSV files",
        type=["csv"],
        accept_multiple_files=True,
        key=f"{_SS_PREFIX}_uploader",
    ) or []


def _current_month_hdpe(pkg_df: pd.DataFrame, current_month: pd.Timestamp) -> Optional[float]:
    """Return the HDPE ($/lb) value for ``current_month`` from the packaging index."""
    if pkg_df is None or pkg_df.empty or "Time" not in pkg_df.columns:
        return None
    hdpe_col = next((c for c in pkg_df.columns if c.lower().startswith("hdpe")), None)
    if hdpe_col is None:
        return None
    df = pkg_df.copy()
    df["_month_dt"] = df["Time"].apply(_parse_month)
    match = df[df["_month_dt"] == current_month]
    if match.empty:
        return None
    value = pd.to_numeric(match.iloc[0][hdpe_col], errors="coerce")
    return None if pd.isna(value) else float(value)


def _available_milk_months(milk_df: pd.DataFrame) -> list[pd.Timestamp]:
    """Return the sorted unique first-of-month timestamps in ``milk_mover_tracker``.

    Used both to populate the time slicer's options and to derive sensible
    defaults. Rows with unparseable Month values are dropped silently.
    """
    df = _strip_df_columns(milk_df)
    if "Month" not in df.columns:
        return []
    months = (
        df["Month"]
        .apply(_parse_month)
        .dropna()
        .drop_duplicates()
        .sort_values()
    )
    return list(months)


def _render_milk_slicer(
    available_months: list[pd.Timestamp],
    current_month: pd.Timestamp,
) -> tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    """Render compact Start/End month selectboxes + chart-only Category & Class filters.

    Behaviour
    ---------
    * Defaults: end = ``current_month`` if present in the source file, else
      the latest available month; start = the month immediately before end
      (or the earliest month when there's only one).
    * Selections persist in ``session_state`` under ``_SS_MILK_START`` /
      ``_SS_MILK_END``; if a previously-stored value is no longer in the
      newly-uploaded data it falls back to the default.
    * Two chart-only filters (``"HTST vs ESL"`` Category and ``"I vs II"``
      Class) live under ``_SS_MILK_CATEGORY`` / ``_SS_MILK_CLASS``. They are
      intentionally NOT returned from this function — the chart reads them
      directly from ``session_state`` so the milk-impact pipeline contract
      (Start/End months only) stays unchanged.
    * Returns the live ``(start, end)`` Timestamps so the chart and the
      milk-impact pipeline both see the same selection in the same render.
    """
    if not available_months:
        return None, None

    default_end = (
        current_month
        if current_month in available_months
        else available_months[-1]
    )
    default_start_idx = max(0, available_months.index(default_end) - 1)
    default_start = available_months[default_start_idx]

    # Seed (or repair stale) session-state values BEFORE rendering the
    # widget so st.selectbox picks them up via its `key` argument.
    if st.session_state.get(_SS_MILK_START) not in available_months:
        st.session_state[_SS_MILK_START] = default_start
    if st.session_state.get(_SS_MILK_END) not in available_months:
        st.session_state[_SS_MILK_END] = default_end
    if st.session_state.get(_SS_MILK_CATEGORY) not in _MILK_CATEGORY_OPTIONS:
        st.session_state[_SS_MILK_CATEGORY] = _MILK_CATEGORY_ALL
    if st.session_state.get(_SS_MILK_CLASS) not in _MILK_CLASS_OPTIONS:
        st.session_state[_SS_MILK_CLASS] = _MILK_CLASS_ALL

    label_map = {m: m.strftime("%b %Y") for m in available_months}

    # Two-row layout keeps each selectbox legible inside the 1/3-width Milk
    # column: Start/End Month on the top row, the two chart-only filters on
    # the bottom row. Four equal sub-columns in a single row would truncate
    # the date labels at the typical viewport width.
    col_s, col_e = st.columns(2)
    with col_s:
        st.selectbox(
            "Start Month",
            options=available_months,
            format_func=lambda m: label_map[m],
            key=_SS_MILK_START,
            help="Drives the 'Start Month' Skim/Butterfat rates in the milk-impact pipeline.",
        )
    with col_e:
        st.selectbox(
            "End Month",
            options=available_months,
            format_func=lambda m: label_map[m],
            key=_SS_MILK_END,
            help="Drives the 'End Month' Skim/Butterfat rates in the milk-impact pipeline.",
        )

    col_cat, col_cls = st.columns(2)
    with col_cat:
        st.selectbox(
            "Category (HTST vs ESL)",
            options=_MILK_CATEGORY_OPTIONS,
            key=_SS_MILK_CATEGORY,
            help=(
                "Chart-only filter. Limits the Skim/Butterfat lines drawn "
                "below to one pasteurisation family. Does not affect the "
                "Monthly Milk Incremental Revenue vs. Last Month metric, "
                "the mover_details_table, or the milk_mover download — "
                "those always consume every (Category, Class) row from "
                "Milk_Mover_Tracker."
            ),
        )
    with col_cls:
        st.selectbox(
            "Class (I vs II)",
            options=_MILK_CLASS_OPTIONS,
            key=_SS_MILK_CLASS,
            help=(
                "Chart-only filter. Limits the Skim/Butterfat lines drawn "
                "below to one milk class. Does not affect the Monthly Milk "
                "Incremental Revenue vs. Last Month metric, the "
                "mover_details_table, or the milk_mover download — those "
                "always consume every (Category, Class) row from "
                "Milk_Mover_Tracker."
            ),
        )

    return (
        st.session_state.get(_SS_MILK_START),
        st.session_state.get(_SS_MILK_END),
    )


def _render_milk_autoupdate_status() -> None:
    """Render the auto-update status caption + 'Force refresh' button.

    Reads the cached :class:`AutoUpdateResult` produced earlier in the same
    render by ``_run_milk_mover_autoupdate``. The button forces a check that
    bypasses the TTL guard — useful when the user knows USDA just published.
    """
    result = st.session_state.get(_SS_MILK_AUTOUPDATE_RESULT)

    col_status, col_btn = st.columns([4, 1])
    with col_status:
        if result is not None:
            # Escalate warnings (errors OR bfat-lag fill-in callouts) to
            # ``st.warning`` so the operator sees them as an amber banner
            # instead of buried small grey caption text.  Successful
            # ticks remain as quiet captions to keep the page calm.
            #
            # ``getattr`` with a default guards the common Streamlit
            # post-deploy case where ``session_state`` still holds an
            # ``AutoUpdateResult`` built by the previous module version —
            # any property added in a later release would otherwise
            # ``AttributeError`` on the stale instance until the user
            # restarts the session.  Falling back to ``False`` means a
            # stale instance simply renders as a quiet caption (the
            # pre-escalation behaviour) instead of crashing the page.
            if getattr(result, "is_warning", False):
                st.warning(result.as_caption())
            else:
                st.caption(result.as_caption())
        else:
            st.caption(
                "🥛 Milk Mover data sourced from the Fabric Lakehouse "
                f"(`Files/{_milk_store.get_table_blob_path()}`), kept in sync with USDA's "
                "[Advanced Prices PDF](https://www.ams.usda.gov/mnreports/dymadvancedprices.pdf)."
            )
    with col_btn:
        if st.button(
            "🔄 USDA refresh",
            key=f"{_SS_PREFIX}_milk_force_refresh",
            help=(
                "Bypass the 1-hour cooldown and check the USDA "
                "advanced-prices PDF right now. Inserts a new row when a "
                "change is detected."
            ),
            use_container_width=True,
        ):
            with st.spinner("Checking USDA…"):
                # Drop the read cache before AND after the orchestrator
                # so a manual click always sees the live blob state — even
                # when the USDA PDF was unchanged and ``insert_rows`` was
                # never called (which is the only path that auto-busts
                # the cache).
                try:
                    _milk_store.invalidate_read_cache()
                except Exception:  # noqa: BLE001
                    pass
                _run_milk_mover_autoupdate(force=True)
                try:
                    _milk_store.invalidate_read_cache()
                except Exception:  # noqa: BLE001
                    pass
            # Reset the per-session bootstrap flag so the next render's
            # ``_inject_milk_mover_from_store`` is willing to retry the
            # whole path again if needed.
            st.session_state.pop(_SS_MILK_BOOTSTRAP_TRIED, None)
            # Also clear the routine-tick gate so the next page load runs
            # the orchestrator naturally again (we just bypassed it with
            # ``force=True`` anyway, but the gate's invariant should
            # remain "set ⇔ orchestrator already ran this session").
            st.session_state.pop(_SS_MILK_AUTOUPDATE_TICK_RAN, None)
            st.rerun()


def _render_milk_commodity(
    uploads: dict[str, _Uploaded],
    current_month: pd.Timestamp,
) -> None:
    """Render the Milk Commodity Cost section (slicer → header → chart).

    Mirrors the visual treatment of ``_render_chart`` so the three side-by-side
    columns (Milk / Packaging / Freight) feel like one cohesive row. The time
    slicer rendered above the chart is the single source of truth for the
    Start/End months used by the downstream Milk Cost Mover pipeline.
    """
    st.markdown("#### 🥛 Milk Commodity Cost")
    milk = uploads.get("milk_mover_tracker")
    if milk is None:
        # The OneLake store is the only source of truth — an empty result
        # means the auto-updater hasn't yet seeded
        # ``Files/Milk_cost_tracker/fmmo_tracker.json`` from USDA's
        # advanced-prices PDF.  Page render already attempted ONE
        # auto-bootstrap in :func:`_inject_milk_mover_from_store`; the
        # status caption + manual "USDA refresh" button below surface
        # the actual error if that attempt failed.
        st.info(
            "🥛 The Milk Mover Lakehouse table is empty.  Click "
            "**🔄 USDA refresh** below to fetch the latest "
            "[advanced-prices PDF](https://www.ams.usda.gov/mnreports/dymadvancedprices.pdf) "
            "and seed the FMMO rows.\n\n"
            "If the refresh fails, verify the `[fabric_htst]` (or "
            "`[fabric_milk_mover]`) section of `.streamlit/secrets.toml`, "
            "confirm your account has Read/Write access to "
            f"`Files/{_milk_store.get_table_blob_path()}`, and reload the page.  "
            "(For a fully offline bootstrap you can also drop a "
            "`Milk_Mover_Tracker.csv` into "
            "`data/Market Barometer/Montly Movers/`.)"
        )
        _render_milk_autoupdate_status()
        return

    available_months = _available_milk_months(milk.df)
    start_month, end_month = _render_milk_slicer(available_months, current_month)
    _render_milk_autoupdate_status()

    # The Category ("HTST vs ESL") and Class ("I vs II") filters are
    # chart-only knobs — read them straight from session_state so the
    # slicer's ``(start, end)`` return stays minimal and the milk-impact
    # pipeline contract is unchanged. ``"All"`` (the default for both) is
    # normalised to ``None`` so the chart builder draws every series along
    # that axis.
    category_choice = st.session_state.get(_SS_MILK_CATEGORY, _MILK_CATEGORY_ALL)
    category_filter = (
        None if category_choice == _MILK_CATEGORY_ALL else category_choice
    )
    class_choice = st.session_state.get(_SS_MILK_CLASS, _MILK_CLASS_ALL)
    class_filter = None if class_choice == _MILK_CLASS_ALL else class_choice

    fig = _build_milk_commodity_chart(
        milk.df,
        start_month=start_month,
        end_month=end_month,
        category_filter=category_filter,
        class_filter=class_filter,
    )
    st.plotly_chart(fig, use_container_width=True, key=f"{_SS_PREFIX}_milk_chart")

    # ── Download: the EXACT slice rendered on the chart ─────────────────────
    # Pulled through the same filter pipeline as the chart (slicer bounds,
    # CC II → ESL II mirror, Category / Class knobs) so the CSV always
    # matches what the operator sees on screen.  Hidden silently when the
    # slice would be empty (no need for an "empty CSV" download button).
    visible_slice = _milk_commodity_visible_slice(
        milk.df,
        start_month=start_month,
        end_month=end_month,
        category_filter=category_filter,
        class_filter=class_filter,
    )
    if not visible_slice.empty:
        st.download_button(
            label="⬇️ Download CSV",
            data=_to_csv_bytes(visible_slice),
            file_name=f"Milk_Commodity_Cost_{datetime.now():%Y%m%d}.csv",
            mime="text/csv",
            key=f"{_SS_PREFIX}_milk_chart_download",
            help=(
                "Download the rows currently visible on the chart — slicer "
                "bounds and the Category / Class filters are applied so the "
                "CSV matches what you see on screen."
            ),
        )


# Session-state keys for the Packaging Index time slicer + index filter.
# Both default to "All" / full range on first render and persist across
# reruns so the operator's selection survives slicer interactions on
# adjacent panels.
_SS_PKG_START   = f"{_SS_PREFIX}_pkg_start"
_SS_PKG_END     = f"{_SS_PREFIX}_pkg_end"
_SS_PKG_INDICES = f"{_SS_PREFIX}_pkg_indices"

# Sentinel label used in the index multiselect's "Select All / clear"
# helper button.  Centralised so the label and the button text can never
# drift apart.
_PKG_INDEX_ALL_LABEL = "All indices"


def _packaging_index_columns(pkg_df: pd.DataFrame) -> list[str]:
    """Return every plottable column from the packaging index DataFrame.

    Every non-``Time`` column (HDPE / LDPE / PET / PP / Linerboard …)
    is a candidate index — the filter exposes the full set so the
    operator can drill into any subset.
    """
    if pkg_df is None or pkg_df.empty or "Time" not in pkg_df.columns:
        return []
    return [c for c in pkg_df.columns if c != "Time"]


def _render_packaging_slicer(
    pkg_df: pd.DataFrame,
) -> tuple[Optional[pd.Timestamp], Optional[pd.Timestamp], list[str]]:
    """Render the time slicer + index multiselect above the Packaging chart.

    Behaviour
    ---------
    * Time slicer defaults to **(earliest, latest)** of the ``Time``
      column — i.e. all data — on first render. Subsequent renders read
      the persisted ``_SS_PKG_START`` / ``_SS_PKG_END`` so the operator's
      selection survives reruns.
    * Index filter is a multiselect over every non-``Time`` column,
      defaulting to every index ("All"). A one-click **"All indices"**
      button restores the default.

    Returns
    -------
    (start_ts, end_ts, selected_indices)
        Live values bound to the widgets in this render — pass them
        straight to :func:`_build_packaging_index_chart`.
    """
    if pkg_df is None or pkg_df.empty or "Time" not in pkg_df.columns:
        return None, None, []

    times = pd.to_datetime(pkg_df["Time"], errors="coerce").dropna()
    if times.empty:
        return None, None, _packaging_index_columns(pkg_df)

    min_t = times.min().to_pydatetime().date()
    max_t = times.max().to_pydatetime().date()
    all_indices = _packaging_index_columns(pkg_df)

    # Seed (or repair stale) session-state values BEFORE rendering the
    # widgets so the date_input picks them up via its ``key`` argument.
    saved_start = st.session_state.get(_SS_PKG_START)
    saved_end   = st.session_state.get(_SS_PKG_END)
    if not isinstance(saved_start, date) or saved_start < min_t or saved_start > max_t:
        st.session_state[_SS_PKG_START] = min_t
    if not isinstance(saved_end, date) or saved_end < min_t or saved_end > max_t:
        st.session_state[_SS_PKG_END] = max_t

    saved_indices = st.session_state.get(_SS_PKG_INDICES)
    if not isinstance(saved_indices, list):
        st.session_state[_SS_PKG_INDICES] = list(all_indices)
    else:
        # Repair: drop selections that no longer exist in the upload
        # (e.g. column renamed); fall back to "All" if the repair
        # leaves the selection empty.
        repaired = [c for c in saved_indices if c in all_indices]
        st.session_state[_SS_PKG_INDICES] = repaired or list(all_indices)

    # Two-row layout keeps everything legible inside the 1/3-width
    # Packaging column.
    col_s, col_e = st.columns(2)
    with col_s:
        st.date_input(
            "Start Time",
            min_value=min_t,
            max_value=max_t,
            key=_SS_PKG_START,
            help="Lower bound of the Packaging Index chart (defaults to earliest data).",
        )
    with col_e:
        st.date_input(
            "End Time",
            min_value=min_t,
            max_value=max_t,
            key=_SS_PKG_END,
            help="Upper bound of the Packaging Index chart (defaults to latest data).",
        )

    st.multiselect(
        "Indices",
        options=all_indices,
        key=_SS_PKG_INDICES,
        help=(
            "Filter which packaging-index columns are drawn. Default is "
            f"**{_PKG_INDEX_ALL_LABEL}** — clear the selection or click "
            f"the **{_PKG_INDEX_ALL_LABEL}** button below to restore."
        ),
    )
    if st.button(
        _PKG_INDEX_ALL_LABEL,
        key=f"{_SS_PREFIX}_pkg_indices_all",
        help="Re-select every available index in one click.",
    ):
        st.session_state[_SS_PKG_INDICES] = list(all_indices)
        st.rerun()

    start_date = st.session_state.get(_SS_PKG_START)
    end_date   = st.session_state.get(_SS_PKG_END)
    start_ts = pd.Timestamp(start_date) if start_date else None
    end_ts   = pd.Timestamp(end_date)   if end_date   else None

    # Defensive swap: if the operator inverts the slider somehow,
    # quietly fix the bounds rather than blanking the chart.
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        start_ts, end_ts = end_ts, start_ts

    selected = list(st.session_state.get(_SS_PKG_INDICES) or all_indices)
    return start_ts, end_ts, selected


def _render_chart(uploads: dict[str, _Uploaded], current_month: pd.Timestamp) -> None:
    """Render the Packaging Index Outlook section (slicer → header → metric → chart).

    Slicer/filter additions (May-2026): a time-range picker and an
    index multiselect now sit ABOVE the metric, mirroring the
    Milk-Commodity section's UX. Defaults are "all data" / "all
    indices" so the visual on first render is the same as before the
    slicer was added.
    """
    st.markdown("#### 📈 Packaging Index Outlook (from Procurement)")

    pkg = uploads.get("packaging_index") or uploads.get("pkg_index")
    if pkg is None:
        st.info(
            "📈 Upload `Packaging_Index_from_Bryan*.csv` to see the resin & "
            "linerboard trend chart."
        )
        return

    start_ts, end_ts, selected_indices = _render_packaging_slicer(pkg.df)

    hdpe_value = _current_month_hdpe(pkg.df, current_month)
    metric_col, _spacer = st.columns([1, 1])
    with metric_col:
        st.metric(
            label=f"HDPE ($/lbs) — {current_month.strftime('%b %Y')}",
            value=f"${hdpe_value:.3f}" if hdpe_value is not None else "N/A",
            help="HDPE price from the uploaded Packaging Index for the current month.",
        )

    fig = _build_packaging_index_chart(
        pkg.df,
        start_time=start_ts,
        end_time=end_ts,
        selected_indices=selected_indices,
    )
    st.plotly_chart(fig, use_container_width=True, key=f"{_SS_PREFIX}_pkg_chart")


# ── Freight Index Outlook (From Transportation) ──────────────────────────────
#
# Independent mini-section: it owns its own session_state keys under the same
# ``_SS_PREFIX`` namespace so it never collides with the CSV upload flow.

_SS_FREIGHT_BYTES    = f"{_SS_PREFIX}_freight_bytes"
_SS_FREIGHT_MIME     = f"{_SS_PREFIX}_freight_mime"
_SS_FREIGHT_FILENAME = f"{_SS_PREFIX}_freight_filename"

_FREIGHT_ACCEPTED_TYPES: tuple[str, ...] = ("pdf", "png", "jpg", "jpeg", "webp", "gif")


def _guess_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    """Minimal MIME guess from a filename extension."""
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
    """Render the 'Freight Index Outlook (From Transportation)' section."""
    st.markdown("#### 🚚 Freight Index Outlook (From Transportation)")
    st.caption(
        "source: Breakthrough Fuel "
        f"([LINK]({_BREAKTHROUGH_FUEL_URL}))"
    )

    cached_bytes = st.session_state.get(_SS_FREIGHT_BYTES)

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
        cached_bytes = st.session_state.get(_SS_FREIGHT_BYTES)

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
            st.rerun()

    if mime == "application/pdf":
        b64 = base64.b64encode(cached_bytes).decode("utf-8")
        st.markdown(
            f'<iframe src="data:application/pdf;base64,{b64}" '
            f'width="100%" height="720" '
            f'style="border: 1px solid #e0e0e0; border-radius: 4px;" '
            f'type="application/pdf"></iframe>',
            unsafe_allow_html=True,
        )
    else:
        st.image(cached_bytes, use_container_width=True, caption=filename)


def _render_table_and_refresh(
    uploads: dict[str, _Uploaded],
    current_month: pd.Timestamp,
) -> None:
    """Render the Movers Non-Milk Tracker editor with Refresh + Download.

    Layout
    ------
    The editable table sits on the left (~5/6 of the row); a vertical button
    stack on the right holds two controls (top to bottom):

      1. **🔄 Refresh** — single-trigger orchestrator that runs the
         impact pipeline AND every dependent OneLake write under that
         write's own gate (see
         :func:`_run_refresh_lakehouse_writes` for the full list of
         downstream artefacts and their gates).  Iterating Refresh
         while only tweaking the existing last row's $/lbs cells is
         safe: the cumulative tracking writes
         (``mover_details_table.csv``,
         ``base_milk_cost_monthly_tracker.csv``) are individually
         gated on "new row inserted in NMT" and "End=Start+1" so
         they never publish stale or duplicate state.
      2. **⬇️ Download CSV** — download the current state of the editable
         tracker (including unsaved edits and newly added rows).
    """
    st.markdown("#### 📝 Movers Non-Milk Tracker — fully editable")
    st.caption(
        "Add, remove, or edit rows freely.  The **last row** drives the "
        "Incremental Revenue vs. Last Month calculations.  Click "
        "**Refresh** to recompute metrics and update every dependent "
        "lakehouse file under its own gate."
    )

    col_table, col_btn = st.columns([5, 1])
    with col_table:
        edited = _render_movers_non_milk_editor()
    with col_btn:
        st.markdown("<div style='margin-top: 2.2rem'></div>", unsafe_allow_html=True)
        refresh_clicked = st.button(
            "🔄 Refresh",
            type="primary",
            use_container_width=True,
            key=f"{_SS_PREFIX}_refresh",
            help=(
                "Recompute the Incremental Revenue vs. Last Month metrics "
                "from the LAST row of this table.  Upserts Resin_Cost_Tracker "
                "(both Rest + TOPCO sides, every NMT month), regenerates the "
                "two Resin Mover FGs, rewrites Product_Milk Base Cost, and "
                "publishes the four Mover Downloads.  Also upserts "
                "mover_details_table.csv (when a NEW row has been inserted "
                "into the tracker since the last successful publish) and "
                "base_milk_cost_monthly_tracker.csv (when the slicer's End "
                "Month = Start Month + 1 calendar month)."
            ),
        )
        st.download_button(
            label="⬇️ Download CSV",
            data=_to_csv_bytes(edited),
            file_name=f"Movers_Non_Milk_Tracker_{datetime.now():%Y%m%d}.csv",
            mime="text/csv",
            use_container_width=True,
            key=f"{_SS_PREFIX}_download_nmt",
            help="Download the current state of this editable table.",
        )

    if refresh_clicked:
        # Drop the resin-store read cache before re-injecting.  The
        # underlying ``@st.cache_data`` decorator otherwise pins a
        # potentially-stale "absent" answer for up to 5 minutes — which
        # surfaces as a misleading "resin_calculator is not available"
        # banner when the operator has just dropped fresh files into
        # OneLake.  This is cheap (one HTTPS round-trip per blob) and
        # only fires on an explicit Refresh click, never on every
        # rerun.
        try:
            _resin_store.invalidate_read_cache()
        except Exception:  # noqa: BLE001 — non-fatal cache invalidation
            pass
        _inject_resin_inputs_from_store(uploads)

        with st.spinner("Running Incremental Revenue calculations..."):
            outputs = _compute_all_outputs(uploads, edited, current_month)
        if outputs is not None:
            st.session_state[f"{_SS_PREFIX}_outputs"] = outputs
            # The single Refresh orchestrator runs every dependent
            # OneLake write under its own gate.  Failures surface as
            # small captions inside the helpers — they never raise.
            _run_refresh_lakehouse_writes(outputs, uploads, edited)
            st.success("✅ Calculations complete. Results below.")


def _is_calendar_month_plus_one(
    start_month: pd.Timestamp,
    end_month: pd.Timestamp,
) -> bool:
    """Return True when ``end_month`` is exactly one calendar month after ``start_month``.

    Used by the base_milk_cost_monthly_tracker Refresh gate:

        "End Month = Start Month + 1 calendar month"

    The comparison is normalised to first-of-month so ``2026-05-15`` /
    ``2026-06-01`` still counts as adjacent — but ``2026-04-01`` /
    ``2026-06-01`` (a 2-month jump) is rejected.
    """
    s = pd.Timestamp(start_month).normalize().replace(day=1)
    e = pd.Timestamp(end_month).normalize().replace(day=1)
    return e == s + pd.DateOffset(months=1)


def _maybe_upsert_base_milk_cost_tracker(
    milk_usage_with_movers: pd.DataFrame,
    start_month: Optional[pd.Timestamp],
    end_month: Optional[pd.Timestamp],
) -> None:
    """Refresh-driven upsert into ``base_milk_cost_monthly_tracker.csv``.

    Honours the May-2026-late contract verbatim:

        "base_milk_cost_monthly_tracker.csv is updated when user hits
         Refresh AND the slicer's End Month = Start Month + 1
         calendar month.  Existing month can be overwritten."

    Conditions enforced here:

      1. ``end_month`` is exactly one calendar month after
         ``start_month`` — see :func:`_is_calendar_month_plus_one`.
         A jump of 2+ months is rejected so historical / out-of-band
         milk runs cannot leak into the tracker.
      2. ``milk_usage_with_movers`` is non-empty (the milk pipeline
         actually produced a per-item payload).

    NOTE on overwrite semantics: the underlying store's
    :func:`upsert_rows_for_end_month` drops any pre-existing rows for
    the End Month before inserting the new payload.  Re-Refreshing a
    month after editing usage values therefore produces a truthful
    rewrite rather than a silent skip — this is the May-2026 contract.

    Failures are swallowed into a small caption — they NEVER raise,
    so a transient Fabric outage cannot break the rest of the Refresh
    pipeline.
    """
    if (milk_usage_with_movers is None
            or milk_usage_with_movers.empty
            or end_month is None
            or start_month is None):
        return

    em = pd.Timestamp(end_month).normalize().replace(day=1)
    sm = pd.Timestamp(start_month).normalize().replace(day=1)

    # Gate: End Month must be exactly +1 calendar month from Start.
    if not _is_calendar_month_plus_one(sm, em):
        st.caption(
            f"ℹ️ base_milk_cost_monthly_tracker not updated: End Month "
            f"({em:%Y-%m}) must be exactly one calendar month after "
            f"Start Month ({sm:%Y-%m}).  Adjust the slicer and click "
            f"Refresh again."
        )
        return

    needed = (
        _MUM_COL_END_COST,        # "End Month Milk Cost"
        "Item",
        "Item Description",
    )
    if not all(c in milk_usage_with_movers.columns for c in needed):
        return  # graceful-degrade: required columns missing

    payload = milk_usage_with_movers[list(needed)].rename(
        columns={
            "Item": _base_milk_cost_tracker.COL_ITEM,
            "Item Description": _base_milk_cost_tracker.COL_ITEM_DESC,
            _MUM_COL_END_COST: _base_milk_cost_tracker.COL_END_COST,
        }
    )

    try:
        rows_written, was_overwrite = (
            _base_milk_cost_tracker.upsert_rows_for_end_month(payload, em)
        )
    except _base_milk_cost_tracker.BaseMilkCostTrackerError as exc:
        st.caption(
            f"⚠️ Could not update base_milk_cost_monthly_tracker: {exc}"
        )
        return

    if rows_written:
        verb = "Overwrote" if was_overwrite else "Appended"
        st.caption(
            f"✅ {verb} {rows_written} row(s) for {em:%Y-%m} in "
            f"{_base_milk_cost_tracker.get_store_label()}."
        )


def _maybe_update_product_milk_base_cost(
    milk_usage_with_movers: pd.DataFrame,
    end_month: Optional[pd.Timestamp],
) -> None:
    """Refresh-driven rewrite of ``Files/Activity_Model/Product_Milk Base Cost.csv``.

    Honours the May-2026 contract verbatim:

        "the 'end month milk cost on $/gal' will be moved to
         Product_Milk Base Cost.csv to rewrite the 'Base Milk Cost per
         Gallon' only when through query based on item match, only when
         the 'End Month' is a month ahead of the existing month in the
         lakehouse file, … Refresh is the sole writer."

    Implementation lives in ``data_sources.product_milk_base_cost_store``
    so the gate logic + left-merge live next to the file, not inside
    the page renderer.  This wrapper handles the page-side concerns
    only: build the ``{Item → End Month Milk Cost}`` payload, dedupe
    duplicate Refresh clicks on the same End Month in-session, surface
    the result as a small caption, and short-circuit cleanly on any
    missing precondition.

    Idempotent on every layer: this wrapper, the store's End-Month gate,
    and the underlying ETag-based write.
    """
    if (milk_usage_with_movers is None
            or milk_usage_with_movers.empty
            or end_month is None):
        return

    em = pd.Timestamp(end_month).normalize().replace(day=1)
    # Short-circuit duplicate clicks for the same End Month — the
    # underlying store already no-ops, but skipping the lakehouse read
    # is still worth doing for snappy slicer interactions.
    if st.session_state.get(_SS_PMBC_LAST_END) == em:
        return

    needed = (_MUM_COL_END_COST, "Item")
    if not all(c in milk_usage_with_movers.columns for c in needed):
        return  # graceful-degrade: required columns missing

    payload_df = milk_usage_with_movers[list(needed)].dropna(subset=[_MUM_COL_END_COST])
    payload_df = payload_df[payload_df["Item"].astype(str).str.strip() != ""]
    if payload_df.empty:
        return

    item_to_end_cost: dict[str, float] = {
        str(item).strip(): float(cost)
        for item, cost in zip(
            payload_df["Item"],
            pd.to_numeric(payload_df[_MUM_COL_END_COST], errors="coerce"),
        )
        if pd.notna(cost)
    }
    if not item_to_end_cost:
        return

    try:
        result = _pmbc_store.maybe_update_for_end_month(item_to_end_cost, em)
    except _pmbc_store.ProductMilkBaseCostStoreError as exc:
        st.caption(f"⚠️ Could not update Product_Milk Base Cost: {exc}")
        return

    st.session_state[_SS_PMBC_LAST_END] = em
    # Render a small caption either way — the store distinguishes
    # "wrote N rows" / "End Month not newer" / "no matches" / "error"
    # in a single string we can surface unchanged.
    if result.ok and result.rows_changed:
        st.caption(result.as_caption())
    elif result.ok and result.skipped_reason:
        # Skipped reasons are informational, not warnings — keep the
        # UI footprint small but visible so the operator knows the
        # rewrite was considered and gated cleanly.
        st.caption(result.as_caption())
    elif not result.ok:
        st.caption(result.as_caption())


# ── Refresh-driven Lakehouse writes (single-trigger contract) ────────────────
#
# One Refresh click, five cumulative artefacts, all helpers idempotent
# and NEVER raise — failures surface as small captions so a transient
# Fabric outage cannot break the rest of the user's session.  Each
# write has its own gate so Refresh-iteration on the existing last row
# remains safe (no surprise pollution of cumulative audit history).
#
#   1. Resin_Cost_Tracker.csv         — single ``upsert_for_sides`` over
#                                       every ``(Month, Side)`` key in
#                                       the NMT × Resin_Calculator
#                                       payload.  Overwrites existing
#                                       AND appends new in the same
#                                       call.  Months in the file but
#                                       absent from the NMT (e.g.
#                                       Sep–Nov 2025) pass through
#                                       untouched.
#   2. Product_Milk Base Cost.csv     — rewrite ``Base Milk Cost per Gallon``
#                                       by Item match when the slicer's End
#                                       Month is at least one calendar month
#                                       newer than the file's max Month.
#   3. Monthly_Pricing_Execution/*.csv — authoritative replace of the four
#                                       Mover Downloads (no month gate).
#   4. mover_details_table.csv        — upsert the freshly-built rows for the
#                                       editing month.  Gated on a strict
#                                       "row-count delta > 0" on the editable
#                                       Movers Non-Milk Tracker (a NEW row
#                                       was inserted since the last
#                                       successful publish).  Existing months
#                                       are overwritten when the gate opens.
#   5. base_milk_cost_monthly_tracker.csv — upsert per-item End Month Milk
#                                       Cost.  Gated on "End = Start + 1
#                                       calendar month".  Existing months
#                                       are overwritten when the gate opens.

# Session-state slot used to short-circuit the Product_Milk Base Cost
# rewrite when Refresh is clicked repeatedly for the same End Month.
# The underlying store is already idempotent (it gates on
# ``end_month >= file_max + 1 month``), but keeping the round-trip out
# of the hot path is still worthwhile during rapid slicer toggling.
_SS_PMBC_LAST_END                    = f"{_SS_PREFIX}_pmbc_last_end"

# Session-state slot for the strict mover_details_table Refresh gate.
# Holds the row count of the editable Movers Non-Milk Tracker at the
# moment of the most-recent successful publish.  A later Refresh fires
# the mover_details_table upsert ONLY when the user has materially
# inserted a new row (count went up).  Edits to existing rows therefore
# do NOT re-fire the upsert, even though the underlying store now
# allows overwrite — that's what keeps Refresh-iteration safe (the
# operator can tweak the last row's $/lbs cells without polluting the
# cumulative audit history).
_SS_NMT_LAST_PUBLISHED_ROW_COUNT     = f"{_SS_PREFIX}_nmt_last_published_row_count"


def _maybe_upsert_resin_cost_tracker(outputs: dict) -> None:
    """Refresh-driven upsert of every (Month, Side) row from the NMT payload.

    May-2026-late contract (resin):

        "user clicks Refresh → calculate Resin Cost ($/Gal) for both
         sides over every NMT month → either overwrite or append into
         Resin_Cost_Tracker.csv"

    The payload comes from :func:`_build_tracker_rows_for_nmt` and
    covers every NMT row that carries at least one numeric ``$/lbs``
    cell.  :func:`resin_cost_tracker_store.upsert_for_sides` then
    drops the matching ``(Month, Side)`` rows from the persisted file
    and concats the payload onto the survivors — overwriting existing
    months AND appending new months in a single ETag-guarded write.

    Rows in the persisted file whose ``(Month, Side)`` key is NOT in
    the payload are PRESERVED verbatim — the "Sep–Nov 2025 stay as-is"
    invariant survives every Refresh.

    The split between overwrite-count and append-count is computed
    here purely for the caption — the store does not need it.

    Failures surface as a small caption — they NEVER raise, so a
    transient Fabric outage cannot break the rest of the Refresh
    pipeline.
    """
    if outputs is None:
        return

    payload = outputs.get("resin_tracker_upsert_payload")
    if payload is None or payload.empty:
        st.caption(
            "ℹ️ Resin_Cost_Tracker unchanged — the Movers Non-Milk "
            "Tracker has no rows with numeric ``$/lbs`` values for "
            "Rest or TOPCO."
        )
        return

    try:
        rows_written, rows_replaced = _resin_store.upsert_for_sides(payload)
    except _resin_store.ResinCostTrackerStoreError as exc:
        st.caption(f"⚠️ Could not update Resin_Cost_Tracker: {exc}")
        return

    if rows_written == 0:
        return

    rows_appended = max(rows_written - rows_replaced, 0)
    months_in_payload = sorted({
        _parse_month(m).strftime("%b %Y")
        for m in payload[_COL_MONTH]
        if _parse_month(m) is not None
    })
    months_caption = ", ".join(months_in_payload) if months_in_payload else "n/a"
    st.caption(
        f"✅ Resin_Cost_Tracker upsert: overwrote {rows_replaced} row(s), "
        f"appended {rows_appended} row(s) across "
        f"{len(months_in_payload)} month(s) ({months_caption})."
    )


def _maybe_upsert_mover_details_table_for_month(
    mover_details_table: pd.DataFrame,
    editing_month: pd.Timestamp,
    *,
    new_row_inserted: bool,
    nmt_row_count: int,
) -> None:
    """Refresh-driven upsert of mover_details_table rows for ``editing_month``.

    May-2026-late contract:

        "mover_details_table.csv is updated on Refresh AND a new row
         was inserted into the Movers Non-Milk Tracker since the last
         successful publish (row-count delta).  Existing month data
         can be overwritten."

    Gate conditions (both must hold):

      1. ``new_row_inserted=True`` — the editable Movers Non-Milk
         Tracker has grown by at least one row since the last
         successful publish (detected via row-count delta against
         ``_SS_NMT_LAST_PUBLISHED_ROW_COUNT``).  Edit-in-place on the
         existing last row does NOT qualify, even though the underlying
         store now allows overwrite.  This is what keeps Refresh
         iteration safe — the operator can repeatedly tweak the last
         row without polluting the cumulative audit history.
      2. ``mover_details_table`` is non-empty.

    On success, ``_SS_NMT_LAST_PUBLISHED_ROW_COUNT`` is bumped to the
    current count so the next Refresh starts measuring from the new
    baseline.  Failures surface as a small caption — never raised.
    """
    if mover_details_table is None or mover_details_table.empty or editing_month is None:
        return

    em = pd.Timestamp(editing_month).normalize().replace(day=1)

    if not new_row_inserted:
        # Refresh clicked without a new row in the editable tracker —
        # strict trigger says we do NOT upsert here.  Render a small
        # informational caption so the operator understands why nothing
        # was pushed to the cumulative file.
        st.caption(
            "ℹ️ mover_details_table not updated: Refresh publishes a new "
            "month only when a NEW row has been inserted into the Movers "
            "Non-Milk Tracker since the last successful publish.  "
            "Edit-in-place on the existing last row does not qualify."
        )
        return

    try:
        rows_written, was_overwrite = _mover_details_store.upsert_for_month(
            mover_details_table, em,
        )
    except _mover_details_store.MoverDetailsTableStoreError as exc:
        st.caption(f"⚠️ Could not update mover_details_table: {exc}")
        return

    if rows_written:
        # Bump the published-row-count snapshot so the next Refresh
        # measures "new row inserted" from this baseline forward.
        st.session_state[_SS_NMT_LAST_PUBLISHED_ROW_COUNT] = int(nmt_row_count)
        verb = "Overwrote" if was_overwrite else "Appended"
        st.caption(
            f"✅ {verb} {rows_written} row(s) for {em:%Y-%m} in "
            f"{_mover_details_store.get_store_label()}."
        )


def _publish_mover_downloads_to_lakehouse(
    rest_fg: pd.DataFrame,
    topco_fg: pd.DataFrame,
    milk_mover_df: Optional[pd.DataFrame],
    movers_non_milk_tracker_df: Optional[pd.DataFrame],
) -> None:
    """Replace the canonical Monthly_Pricing_Execution CSVs on every Refresh.

    Mirrors the contract from the May-2026 spec:

        "change the code to a copy of the rest_htst_resin_mover fg,
         topco resin mover fg and milk mover csv into this pricing
         lakehouse: Files/Monthly_Pricing_Execution …; If there is
         already data there, then replace the files there with new
         generated files, only when user hit refresh."

    Extended in the May-2026-late spec to also publish the editable
    Movers Non-Milk Tracker as ``Movers_Non_Milk_Tracker.csv`` so
    downstream auditors / pipelines can see the exact mover values
    that drove the FG outputs.

    Empty / missing frames are skipped silently so a partial pipeline
    run (e.g. milk slicer not yet selected, or an empty tracker) cannot
    accidentally clobber a prior good copy.  Failures surface as small
    captions — they NEVER raise so a transient Fabric outage cannot
    break the rest of the Refresh pipeline.
    """
    payload: dict[str, Optional[pd.DataFrame]] = {
        "rest_fg":                 rest_fg,
        "topco_fg":                topco_fg,
        "milk_mover":              milk_mover_df,
        "movers_non_milk_tracker": movers_non_milk_tracker_df,
    }
    try:
        results = _mpe_store.replace_files(payload)
    except _mpe_store.MonthlyPricingExecutionStoreError as exc:
        st.caption(f"⚠️ Could not publish Mover Downloads to OneLake: {exc}")
        return

    written = [role for role, ok in results.items() if ok]
    if not written:
        return

    label_for = {
        "rest_fg":                 "rest_htst_resin_mover_fg.csv",
        "topco_fg":                "topco_resin_mover_fg.csv",
        "milk_mover":              "milk_mover.csv",
        "movers_non_milk_tracker": "Movers_Non_Milk_Tracker.csv",
    }
    files_caption = ", ".join(label_for[r] for r in written)
    st.caption(
        f"✅ Published {files_caption} to "
        f"{_mpe_store.get_folder_label()}."
    )


def _editing_month_from_session() -> Optional[pd.Timestamp]:
    """Resolve the editing month from the cached editable tracker's last row.

    Returns ``None`` when the tracker is empty or its last-row Month is
    unparseable, so callers can short-circuit cleanly without raising.
    Reads the editor's coerced edited view (or the seed on first
    render) — never the stable seed slot directly, which is preserved
    intact for the widget.
    """
    nmt_df = _get_nmt_edited_view()
    if nmt_df is None or nmt_df.empty:
        return None
    return _parse_month(nmt_df.iloc[-1][_NMT_COL_MONTH])


def _run_refresh_lakehouse_writes(
    outputs: dict,
    uploads: dict[str, _Uploaded],
    edited_nmt: pd.DataFrame,
) -> None:
    """Run every Refresh-time OneLake write under the single-trigger contract.

    Wired from the sole Refresh button handler.  Six dependent writes
    fire here, each under its own gate:

      1. **Resin_Cost_Tracker.csv** — single ``upsert_for_sides`` over
         every ``(Month, Side)`` key in the NMT × Resin_Calculator
         payload.  Overwrites existing months AND appends new months
         in one ETag-guarded write.  Months in the file but absent
         from the NMT (e.g. Sep–Nov 2025) are PRESERVED.
      2. **Product_Milk Base Cost.csv** — overwrite
         ``Base Milk Cost per Gallon`` by Item match; gated on
         ``End Month >= file's max Month + 1`` calendar month.
      3. **Monthly_Pricing_Execution/{rest, topco, milk, NMT}.csv** —
         authoritative replace on every Refresh (no month gate).
      4. **mover_details_table.csv** — upsert the editing month;
         gated on **the operator inserted a new row into the NMT
         since the last successful publish** (row-count delta).
         Existing month data is overwritten when the gate is open.
         Edit-in-place on the existing last row does NOT qualify.
      5. **base_milk_cost_monthly_tracker.csv** — upsert the slicer's
         End Month; gated on ``End Month = Start Month + 1`` calendar
         month.  Existing months are overwritten when the gate is
         open.

    Step 4's row-count gate preserves the spec invariant "Refresh
    shows metric changes but iteration on the existing last row
    never pollutes the cumulative audit history" — even though every
    other write now also rides the Refresh click.

    Failures surface as small captions inside each helper — they NEVER
    raise into this orchestrator.
    """
    editing_month = _editing_month_from_session()
    if editing_month is None:
        return

    # ── FG warnings ─────────────────────────────────────────────────────────
    # Render BEFORE any write so operators see the actionable banner
    # alongside the success caption.  Each side's missing Old-month
    # warning is independent, so render them all.
    for warning in outputs.get("resin_fg_warnings", ()):  # type: ignore[arg-type]
        st.warning(f"⚠️ {warning}")

    rest_fg = outputs.get("rest_htst_resin_mover_fg", pd.DataFrame())
    topco_fg = outputs.get("topco_resin_mover_fg", pd.DataFrame())

    # Milk pipeline is reactive on the slicer — recompute here so the
    # Mover Downloads + Product_Milk Base Cost see the freshest values
    # without requiring another Refresh after a slicer interaction.
    milk_usage_with_movers = _compute_milk_usage_for_render(uploads)
    end_month_slicer   = st.session_state.get(_SS_MILK_END)
    start_month_slicer = st.session_state.get(_SS_MILK_START)
    # Read the edited view (not the stable seed) so the published
    # Movers_Non_Milk_Tracker.csv reflects the operator's just-typed
    # values.  See ``_render_movers_non_milk_editor`` for why
    # ``_SS_NMT_DF`` is intentionally left untouched.
    nmt_df: Optional[pd.DataFrame] = _get_nmt_edited_view()

    # ── 1. Resin tracker — single upsert (overwrite + append) ───────────────
    _maybe_upsert_resin_cost_tracker(outputs)

    # ── 2. Product_Milk Base Cost — Refresh-gated overwrite ─────────────────
    _maybe_update_product_milk_base_cost(milk_usage_with_movers, end_month_slicer)

    # ── 3. Mover Downloads — authoritative replace ──────────────────────────
    _publish_mover_downloads_to_lakehouse(
        rest_fg, topco_fg, milk_usage_with_movers, nmt_df,
    )

    # ── 4. mover_details_table — gated by NMT row-count delta ───────────────
    #
    # The row-count gate is what makes Refresh-iteration safe: the
    # operator can hit Refresh repeatedly while tweaking the LAST
    # row's $/lbs cells and the cumulative audit history won't change.
    # Inserting a brand-new row (the only act that means "publish this
    # month") opens the gate exactly once until the next successful
    # publish.
    nmt_row_count = int(len(edited_nmt)) if edited_nmt is not None else 0
    last_published_count = int(
        st.session_state.get(_SS_NMT_LAST_PUBLISHED_ROW_COUNT, 0)
    )
    new_row_inserted = nmt_row_count > last_published_count

    base_table: pd.DataFrame = outputs.get(
        "mover_details_table_base", pd.DataFrame(),
    )
    full_table, _ignored = _layer_milk_on_mover_details_table(
        base_table, milk_usage_with_movers,
    )
    _maybe_upsert_mover_details_table_for_month(
        full_table, editing_month,
        new_row_inserted=new_row_inserted,
        nmt_row_count=nmt_row_count,
    )

    # ── 5. base_milk_cost_monthly_tracker — gated on End=Start+1 ────────────
    _maybe_upsert_base_milk_cost_tracker(
        milk_usage_with_movers, start_month_slicer, end_month_slicer,
    )


def _compute_milk_usage_for_render(
    uploads: dict[str, _Uploaded],
) -> Optional[pd.DataFrame]:
    """Build the milk usage table from cached uploads + the live time slicer.

    Returns ``None`` when the milk inputs (Milk_Mover_Tracker AND
    Milk_Usage_Stable) are not both uploaded — the layering step then leaves
    the milk columns blank, which is the intended graceful-degrade behaviour.

    Reads the slicer values from ``session_state`` so the entire downstream
    mover_details_table reacts to slicer changes without requiring another Refresh.

    Pure read-side computation — this function NEVER mutates OneLake.
    Every dependent write lives under the single Refresh orchestrator
    (:func:`_run_refresh_lakehouse_writes`) so OneLake state is tied
    to deliberate user action only.
    """
    milk = uploads.get("milk_mover_tracker")
    usage = uploads.get("milk_usage_stable")
    if milk is None or usage is None:
        return None

    start_month = st.session_state.get(_SS_MILK_START)
    end_month   = st.session_state.get(_SS_MILK_END)
    scrape_tracker_df = (
        uploads["scrape_tracker"].df if "scrape_tracker" in uploads else pd.DataFrame()
    )
    milk_scrape_fraction = _latest_milk_scrape_fraction(scrape_tracker_df)

    milk_usage_with_movers = _build_milk_usage_with_movers(
        milk_usage_stable_df=usage.df,
        milk_mover_tracker_df=milk.df,
        milk_scrape_fraction=milk_scrape_fraction,
        start_month=start_month,
        end_month=end_month,
    )

    return milk_usage_with_movers


def _render_mover_downloads(
    rest_fg: pd.DataFrame,
    topco_fg: pd.DataFrame,
    milk_usage_with_movers: Optional[pd.DataFrame],
    today: str,
) -> None:
    """Render the **Mover Downloads** section: three reference CSV downloads.

    Layout — three equal-width columns:

        | Rest HTST resin_mover_fg | TOPCO resin_mover_fg | milk_mover (usage) |

    Each download exposes the *driver* table that produced one of the per-row
    mover columns in the mover_details_table — useful for audit, spot-checking,
    and external reuse. The Milk Mover download is the slicer-driven
    ``milk_usage_with_movers_df`` (everything from ``Milk_Usage_Stable`` plus
    the Start/End-month rates, costs, and ``Milk Cost Mover $/Gal``); when the
    relevant uploads are missing or the slicer hasn't produced data yet, the
    button is disabled (rather than hidden) so the section layout stays
    predictable across reruns.
    """
    st.markdown("#### Mover Downloads")

    milk_df = (
        milk_usage_with_movers
        if milk_usage_with_movers is not None
        else pd.DataFrame()
    )

    dl_rest, dl_topco, dl_milk = st.columns(3, gap="medium")
    with dl_rest:
        st.download_button(
            label="⬇️ Download Rest HTST resin_mover_fg (CSV)",
            data=_to_csv_bytes(rest_fg),
            file_name=f"rest_htst_resin_mover_fg_{today}.csv",
            mime="text/csv",
            use_container_width=True,
            disabled=rest_fg.empty,
            help="Resin Mover ($/Gal) FG using the Rest HTST $/lbs driver.",
            key=f"{_SS_PREFIX}_dl_rest_fg",
        )
    with dl_topco:
        st.download_button(
            label="⬇️ Download TOPCO resin_mover_fg (CSV)",
            data=_to_csv_bytes(topco_fg),
            file_name=f"topco_resin_mover_fg_{today}.csv",
            mime="text/csv",
            use_container_width=True,
            disabled=topco_fg.empty,
            help="Resin Mover ($/Gal) FG using the TOPCO HTST $/lbs driver.",
            key=f"{_SS_PREFIX}_dl_topco_fg",
        )
    with dl_milk:
        st.download_button(
            label="⬇️ Download milk_mover (CSV)",
            data=_to_csv_bytes(milk_df),
            file_name=f"milk_mover_{today}.csv",
            mime="text/csv",
            use_container_width=True,
            disabled=milk_df.empty,
            help=(
                "Per-item milk usage table with Start/End Month rates, "
                "costs, and Milk Cost Mover ($/Gal). Reacts to the time "
                "slicer above the Milk Commodity Cost chart."
            ),
            key=f"{_SS_PREFIX}_dl_milk_mover",
        )


def _render_results() -> None:
    """Render Incremental Revenue metrics + Mover Downloads + mover_details_table + Example prices.

    Surfaced only after a successful Refresh — otherwise this is a no-op.
    Section order (per the May-2026 product spec):

        1. Three headline "Incremental Revenue vs. Last Month" metrics
           (Resin / Freight / Milk).
        2. **Mover Downloads** — Rest HTST FG, TOPCO FG, and the slicer-driven
           ``milk_mover`` (milk_usage_table + Milk Cost Mover $/Gal).
        3. **mover_details_table download** — single combined CSV used for the
           monthly pricing update.
        4. Example prices (when uploaded).

    The Milk Mover columns and the Milk Incremental Revenue metric are
    computed each render from the live time slicer + cached uploads, so
    changing the slicer reactively updates everything below WITHOUT
    requiring Refresh.
    """
    outputs = st.session_state.get(f"{_SS_PREFIX}_outputs")
    uploads = st.session_state.get(f"{_SS_PREFIX}_uploads") or {}
    if not outputs:
        return

    rest_fg: pd.DataFrame        = outputs["rest_htst_resin_mover_fg"]
    topco_fg: pd.DataFrame       = outputs["topco_resin_mover_fg"]
    mover_details_table_base: pd.DataFrame   = outputs["mover_details_table_base"]
    monthly_resin_total: float   = outputs.get("monthly_resin_impact_total",   0.0)
    monthly_freight_total: float = outputs.get("monthly_freight_impact_total", 0.0)

    # Layer milk on top of the cached base. This recomputation is cheap (one
    # description-keyed map + one element-wise multiply) so we run it on every
    # render — that's how the slicer stays reactive without a Refresh click.
    milk_usage_with_movers = _compute_milk_usage_for_render(uploads)
    mover_details_table, monthly_milk_total = _layer_milk_on_mover_details_table(
        mover_details_table_base, milk_usage_with_movers,
    )

    st.markdown("---")
    st.markdown("### Incremental Revenue vs. Last Month")
    st.caption(
        "Metrics are summed from the mover_details_table; download it "
        "below for the full per-SKU breakdown.  The Milk metric reacts to "
        "the Start/End Month slicer above the Milk Commodity Cost chart."
    )

    # ── 1. Headline metrics ──────────────────────────────────────────────────
    m1, m2, m3 = st.columns(3, gap="medium")
    with m1:
        st.metric(
            label="Monthly Resin — Incremental Revenue vs. Last Month",
            value=f"${monthly_resin_total:,.2f}",
            help=(
                "Σ(Monthly Resin Mover) over site_item_volume rows — Resin "
                "Mover comes from the editable tracker's last row, with FG "
                "fallback for Rest HTST and TOPCO."
            ),
        )
    with m2:
        st.metric(
            label="Monthly Freight — Incremental Revenue vs. Last Month",
            value=f"${monthly_freight_total:,.2f}",
            help=(
                "Σ(Monthly Freight Mover) — Freight Mover comes from the "
                "editable tracker's last row matched on Tag; Monthly Freight "
                "Mover = Monthly Gallons × Pricing Method × Freight Mover $/Gal."
            ),
        )
    with m3:
        st.metric(
            label=(
                "Monthly Milk — Incremental Revenue vs. Last Month "
                "(Make Sure the Start and End Month are Selected "
                "Correctly to see MOM Change)"
            ),
            value=f"${monthly_milk_total:,.2f}",
            help=(
                "Σ(Monthly Milk Mover) over the mover_details_table — Milk Mover "
                "$/Gal = End Month Milk Cost − Start Month Milk Cost from the "
                "time slicer above the Milk Commodity Cost chart. Monthly "
                "Milk Mover = Monthly Gallons × Milk Mover $/Gal."
            ),
        )

    today = datetime.now().strftime("%Y%m%d")

    # ── 2. Mover Downloads (above the mover_details_table) ──────────────────────────────
    _render_mover_downloads(rest_fg, topco_fg, milk_usage_with_movers, today)

    # ── 3. Single mover_details_table-table download ───────────────────────────
    st.markdown("#### mover_details_table download")
    st.download_button(
        label="⬇️ Download mover_details_table (CSV)",
        data=_to_csv_bytes(mover_details_table),
        file_name=f"mover_details_table_{today}.csv",
        mime="text/csv",
        use_container_width=True,
        disabled=mover_details_table.empty,
        help=(
            "site_item_volume + Monthly Freight/Resin/Milk Mover totals + "
            "Freight/Resin/Milk Mover ($/Gal) drivers grouped at the end. "
            "Use this file for the monthly pricing update."
        ),
        key=f"{_SS_PREFIX}_dl_mover_details_table",
    )

    # ── 4. Example prices (optional) ────────────────────────────────────────
    example_impact = outputs.get("example_prices_impact")
    if example_impact is not None and not example_impact.empty:
        st.markdown("#### Example prices (Rest HTST defaults)")
        cfg_raw: dict = {
            "Resin Mover $/EA": st.column_config.NumberColumn(
                "Resin Mover $/EA",
                format="$%.4f",
                help="From rest_htst_resin_mover_fg matched on Item Description "
                     "(gal/ea assumed 1).",
            ),
            "Freight Mover $/EA": st.column_config.NumberColumn(
                "Freight Mover $/EA",
                format="$%.4f",
                help="Rest HTST Freight Mover ($/Gal) from the tracker's last row.",
            ),
            "Price Increase%": st.column_config.NumberColumn(
                "Price Increase%",
                format="%.2f",
                help="(Resin Mover $/EA + Freight Mover $/EA) ÷ Price $/EA × 100.",
            ),
        }
        cfg = {k: v for k, v in cfg_raw.items() if k in example_impact.columns}
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

    Includes the editable Movers Non-Milk Tracker (seed + coerced view
    + the widget's internal delta state), the milk-chart time slicer,
    the packaging slicer, and every Refresh idempotency flag so
    "Change files" returns the section to a fully pristine state —
    uploads, edits, slicer selections, and computed outputs all reset
    together.
    """
    for key in (
        f"{_SS_PREFIX}_uploads",
        f"{_SS_PREFIX}_sig",
        f"{_SS_PREFIX}_outputs",
        _SS_NMT_DF,
        _SS_NMT_EDITED_VIEW,
        _SS_NMT_EDITOR_KEY,
        _SS_MILK_START,
        _SS_MILK_END,
        _SS_MILK_CATEGORY,
        _SS_MILK_CLASS,
        _SS_NMT_LAST_PUBLISHED_ROW_COUNT,
        _SS_PMBC_LAST_END,
        _SS_PKG_START,
        _SS_PKG_END,
        _SS_PKG_INDICES,
    ):
        st.session_state.pop(key, None)


@st.fragment
def render_monthly_resin_freight_mover_tracker() -> None:
    """Render the Monthly Milk, Resin & Freight Movers section.

    Using ``@st.fragment`` means uploads, edits and the Refresh button only
    rerun THIS section — the Market Indices dashboard and Walmart Fresh
    Tracker above/below stay untouched.

    State machine
    -------------
    * **Upload state** (no cached uploads)
        → Render the file uploader. On successful upload, cache the parsed
          files in session_state and continue in the same run.
    * **Processed state** (uploads cached)
        → Uploader is hidden; "Change files" wipes state and reruns. Chart,
          Freight Outlook, editable Movers Non-Milk Tracker + Refresh, and
          the Incremental Revenue vs. Last Month section follow.
    """
    current_month = pd.Timestamp(date.today().replace(day=1))

    # Fabric auth gate.  If auth is broken, show a warning directing the user
    # to Home & Fabric Sign-in and stop rendering.  If a sign-in was just
    # completed (detected via shared device-code state), run per-store cache
    # recovery before proceeding.  The interactive sign-in UI is on the Home
    # page; this module only gates and recovers.
    if _require_fabric_auth():
        return

    # Run the routine USDA auto-update tick once per *session*.  The
    # orchestrator is also TTL-guarded internally (1 h cooldown), but the
    # TTL check itself reads ``fmmo_state.json`` from OneLake; under
    # Streamlit's rerun-everything execution model that read fires on
    # every widget interaction.  The session-state gate trims it to once
    # per browser visit.  Manual "USDA refresh" clicks bypass this gate
    # (they call ``_run_milk_mover_autoupdate(force=True)`` and clear
    # the flag).  See :data:`_SS_MILK_AUTOUPDATE_TICK_RAN` for the full
    # rationale.
    if not st.session_state.get(_SS_MILK_AUTOUPDATE_TICK_RAN):
        st.session_state[_SS_MILK_AUTOUPDATE_TICK_RAN] = True
        _run_milk_mover_autoupdate(force=False)

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
        st.session_state.pop(f"{_SS_PREFIX}_outputs", None)

    # Source the milk_mover_tracker, milk_usage_stable, resin_calculator
    # and resin_cost_tracker DataFrames from the OneLake stores.  None of
    # these files are recognised upload roles (see ``_ROLE_KEYWORDS``) so
    # these injections are the only place those DataFrames enter the
    # section.  Each helper is idempotent — the underlying readers are
    # cached for ~5 minutes — so calling them on every render is cheap.
    _inject_milk_mover_from_store(uploads)
    _inject_milk_usage_stable_from_store(uploads)
    _inject_resin_inputs_from_store(uploads)

    # Surface any resin-store error captured during the inject above so the
    # user has an actionable single point of feedback when the lakehouse is
    # unreachable (vs. a silent "no data" state below).
    if st.session_state.get(_SS_RESIN_STORE_ERROR):
        _render_resin_store_status()

    # ── Processed state ───────────────────────────────────────────────────────
    if st.button(
        "📁 Change files",
        key=f"{_SS_PREFIX}_change_files",
        help="Clear the currently-loaded files (and the editable tracker) and "
             "return to the upload panel.",
    ):
        _clear_upload_state()
        st.rerun()

    st.markdown("---")
    # Three equal-width columns: Milk Commodity Cost (left), Packaging Index
    # Outlook (middle), Freight Index Outlook (right). Three columns at ~33%
    # width each keep the dashboard scannable left-to-right while leaving the
    # height matching across the row.
    col_milk, col_pkg, col_freight = st.columns(3, gap="medium")
    with col_milk:
        _render_milk_commodity(uploads, current_month)
    with col_pkg:
        _render_chart(uploads, current_month)
    with col_freight:
        _render_freight_outlook()
    st.markdown("---")
    _render_table_and_refresh(uploads, current_month)
    _render_results()
