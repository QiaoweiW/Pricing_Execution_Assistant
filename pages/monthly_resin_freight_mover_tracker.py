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
                              _previous_month_extract, _build_resin_mover_fg,
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
                              editable table + Refresh, mover downloads,
                              results, state clearers)
8. Public API                (render_monthly_resin_freight_mover_tracker)

Design notes
------------
Isolation
  Every key this fragment writes into ``st.session_state`` is namespaced under
  ``_SS_PREFIX`` so it never collides with other Market Barometer sections.
  The public entry point is a ``@st.fragment`` so uploads, edits and the
  Refresh button only rerun this block.

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
         topco_resin_mover_fg      ← TOPCO HTST Resin Mover ($/lbs)

     Each FG has columns
         Product ID | Product Description | Resin |
         Old Resin Cost ($/Gal) | New Resin Cost ($/Gal) | Resin Mover ($/Gal)
     where "Old" comes from the rows of ``Resin_Cost_Tracker`` for the month
     immediately BEFORE the editing month (= last-row Month of the editable
     tracker, e.g. April when editing May), and "New" is the freshly-computed
     cost using the tracker's last-row $/lbs driver. Each FG is exposed as a
     CSV download. No monthly-gallons / monthly-impact columns live here —
     those live on the mover_details_table.

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
     row sums populate the three headline metrics — Monthly Freight Impact,
     Monthly Resin Impact, Monthly Milk Impact.

  3. Milk pipeline (driven by the Start/End Month time slicer above the
     Milk Commodity Cost chart):

         Start Month Milk Cost =
             (Start Skim Rate × Skim Usage + Start Butterfat Rate × Butterfat Usage)
             × (1 + Milk Scrape%)
         End Month Milk Cost   = same formula with End-month rates.
         Milk Cost Mover $/Gal = End Month Milk Cost − Start Month Milk Cost.

     Skim/Butterfat rates per (Category, Class) come from
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
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# OneLake-backed store + USDA-PDF auto-update workflow for Milk_Mover_Tracker.
# These two modules replace the legacy ``Milk_Mover_Tracker.csv`` upload —
# the milk DataFrame is now read from a Microsoft Fabric Lakehouse blob at
# render time, and a separate orchestrator keeps it in sync with the USDA
# advanced-prices PDF. None of the downstream calculation code has been
# touched: only how the milk DataFrame enters this view changed.
from data_sources import milk_mover_autoupdate as _milk_autoupdate
from data_sources import milk_mover_store as _milk_store
from data_sources import milk_usage_stable_store as _milk_usage_store
from data_sources import base_milk_cost_tracker_store as _base_milk_cost_tracker
from data_sources import resin_cost_tracker_store as _resin_store
from data_sources import mover_details_table_store as _mover_details_store
from data_sources import monthly_pricing_execution_store as _mpe_store


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
_NMT_COL_TOPCO_RESIN   = "TOPCO HTST Resin Mover ($/lbs)"
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

def _build_packaging_index_chart(pkg_df: pd.DataFrame) -> go.Figure:
    """Multi-series time-series chart of the Packaging Index.

    Resin price series (HDPE, LDPE, PET, PP) plot on the primary y-axis in
    $/lb; Linerboard (if present) plots on a secondary y-axis in $/ton because
    it lives on a different scale.
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


# Color map for the milk-commodity chart. Each (Category, Class) pair gets its
# own colour; Skim is rendered as a solid line on the primary y-axis and
# Butterfat as a dashed line on the secondary y-axis (the two metrics live on
# very different scales — Skim ≈ 0.08–0.15, Butterfat ≈ 1.4–3.0).
_MILK_COLOR_MAP: dict[tuple[str, str], str] = {
    ("HTST", "I"):  "#1f77b4",
    ("HTST", "II"): "#aec7e8",
    ("ESL",  "I"):  "#d62728",
    ("ESL",  "II"): "#ff9896",
}


def _build_milk_commodity_chart(
    milk_df: pd.DataFrame,
    start_month: Optional[pd.Timestamp] = None,
    end_month: Optional[pd.Timestamp] = None,
    category_filter: Optional[str] = None,
    class_filter: Optional[str] = None,
) -> go.Figure:
    """Multi-series time-series chart of milk Skim & Butterfat rates.

    The chart visualises every (Category, Class) combination that exists in
    ``Milk_Mover_Tracker``. Skim Rate plots on the primary y-axis (solid
    lines); Butterfat Rate plots on the secondary y-axis (dashed lines). When
    a column or combination is missing the corresponding traces are skipped
    silently — the chart degrades gracefully rather than raising.

    The optional ``start_month`` / ``end_month`` arguments restrict the
    plotted x-range so the chart reacts to the time slicer rendered above it.
    Both bounds are inclusive; ``None`` means "no bound on that side".

    ``category_filter`` is a chart-only knob that restricts which Category
    families are drawn — accepts ``"HTST"`` / ``"ESL"`` (case-insensitive) to
    isolate one family, or ``None`` / ``"All"`` to draw every series.

    ``class_filter`` is the parallel chart-only knob for Class — accepts
    ``"I"`` / ``"II"`` (case-insensitive) to isolate one milk class, or
    ``None`` / ``"All"`` to draw every class. The two filters compose
    (``category_filter="HTST"`` + ``class_filter="I"`` shows HTST Class I
    only).

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

    # Tolerant column resolution — header drift like "Skim Rate " or
    # "Butterfat Rate $" still resolves to the expected metric.
    skim_col = next((c for c in df.columns if "skim" in c.lower()), None)
    bf_col   = next((c for c in df.columns if "butter" in c.lower()), None)
    if skim_col is None and bf_col is None:
        return go.Figure()

    # Normalise the chart-only category filter once. Anything outside the
    # known {"HTST", "ESL"} set falls back to "show everything" so unexpected
    # values can never silently blank out the chart.
    cat_filter = (category_filter or "").strip().upper()
    if cat_filter not in {"HTST", "ESL"}:
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

        if skim_col is not None:
            fig.add_trace(go.Scatter(
                x=sub["Month"],
                y=pd.to_numeric(sub[skim_col], errors="coerce"),
                mode="lines+markers",
                name=f"{cat} Class {cls} Skim",
                line=dict(color=color, width=2, dash="solid"),
                marker=dict(size=4),
                hovertemplate=(
                    f"<b>{cat} Class {cls} Skim</b><br>"
                    "%{x|%b %Y}<br>$%{y:.4f}<extra></extra>"
                ),
            ))

        if bf_col is not None:
            fig.add_trace(go.Scatter(
                x=sub["Month"],
                y=pd.to_numeric(sub[bf_col], errors="coerce"),
                mode="lines+markers",
                name=f"{cat} Class {cls} Butterfat",
                line=dict(color=color, width=2, dash="dash"),
                marker=dict(size=4, symbol="diamond"),
                yaxis="y2",
                hovertemplate=(
                    f"<b>{cat} Class {cls} Butterfat</b><br>"
                    "%{x|%b %Y}<br>$%{y:.4f}<extra></extra>"
                ),
            ))

    fig.update_layout(
        xaxis=dict(title="", showgrid=False, showline=True, linecolor="#e0e0e0"),
        yaxis=dict(title="Skim Rate ($)", showgrid=True, gridcolor="#f0f0f0",
                   showline=True, linecolor="#e0e0e0", rangemode="tozero"),
        yaxis2=dict(title="Butterfat Rate ($)", overlaying="y", side="right",
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

# Session-state key for the Movers_Non_Milk_Tracker DataFrame. Persisting it
# here lets edits survive Refresh clicks and Streamlit reruns without a
# round-trip to disk.
_SS_NMT_DF = f"{_SS_PREFIX}_movers_non_milk_df"

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
# default and preserves the legacy four-line view; ``"HTST"`` / ``"ESL"``
# isolate the two pasteurisation methods so users can compare classes within
# a single family without legend clutter.
_MILK_CATEGORY_ALL: str = "All"
_MILK_CATEGORY_OPTIONS: tuple[str, ...] = (_MILK_CATEGORY_ALL, "HTST", "ESL")

# Filter options for the chart-only class selector. ``"All"`` preserves the
# default view (both Class I and Class II for whatever Category is selected);
# ``"I"`` / ``"II"`` isolate one milk class so users can compare HTST vs ESL
# within a single class without legend clutter.
_MILK_CLASS_ALL: str = "All"
_MILK_CLASS_OPTIONS: tuple[str, ...] = (_MILK_CLASS_ALL, "I", "II")


def _seed_movers_non_milk_df() -> pd.DataFrame:
    """Return a fresh DataFrame with the hard-coded schema and seeded rows.

    Every numeric column is forced to ``float64`` — mixing ``None`` with a
    numeric in an editable column otherwise yields ``object`` dtype, which
    Streamlit's NumberColumn refuses to edit cell-by-cell.
    """
    df = pd.DataFrame(_NMT_SEED_ROWS, columns=list(_NMT_ALL_COLUMNS))
    for col in _NMT_NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    df[_NMT_COL_MONTH] = pd.to_datetime(df[_NMT_COL_MONTH], errors="coerce")
    return df


def _ensure_movers_non_milk_state() -> None:
    """Seed ``session_state[_SS_NMT_DF]`` on first render of this fragment."""
    if _SS_NMT_DF not in st.session_state:
        st.session_state[_SS_NMT_DF] = _seed_movers_non_milk_df()


def _render_movers_non_milk_editor() -> pd.DataFrame:
    """Render the editable Movers_Non_Milk_Tracker; return the latest frame.

    Behaviour
    ---------
    * All cells (including Month) are editable.
    * Rows can be added / removed via the editor's built-in toolbar
      (``num_rows="dynamic"``).
    * The current state is persisted in ``session_state`` after every rerun
      so subsequent calculations and downloads see the user's edits.

    Returns the latest DataFrame (typed: Month → datetime, the rest →
    float64), so downstream calculations don't need to re-coerce.
    """
    _ensure_movers_non_milk_state()
    current_df: pd.DataFrame = st.session_state[_SS_NMT_DF]

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
        current_df,
        column_config=column_config,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        key=f"{_SS_PREFIX}_nmt_editor",
    )

    # Re-coerce numeric dtypes after the round-trip and keep the Month as a
    # proper Timestamp so iloc[-1] arithmetic stays well-typed.
    edited = edited.copy()
    edited[_NMT_COL_MONTH] = pd.to_datetime(edited[_NMT_COL_MONTH], errors="coerce")
    for col in _NMT_NUMERIC_COLUMNS:
        edited[col] = pd.to_numeric(edited[col], errors="coerce").astype("float64")

    st.session_state[_SS_NMT_DF] = edited
    return edited


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


def _previous_month_extract(
    resin_cost_tracker_df: pd.DataFrame,
    editing_month: pd.Timestamp,
) -> pd.DataFrame:
    """Return ``Resin_Cost_Tracker`` rows for the month right before ``editing_month``.

    "Right before" means ``editing_month − 1 month`` (e.g. April when editing
    May). If the exact previous month is missing from the file, fall back to
    the latest month strictly before ``editing_month`` so the FG still has
    something to compare against rather than coming back empty.

    Rows with unparseable Month values are dropped. Returns an empty frame
    (with the original columns minus the helper) when no candidate exists.
    """
    df = resin_cost_tracker_df.copy()
    if _COL_MONTH not in df.columns:
        return df.iloc[0:0]
    df["_month_dt"] = df[_COL_MONTH].apply(_parse_month)
    df = df.dropna(subset=["_month_dt"])
    if df.empty:
        return df.drop(columns=["_month_dt"])

    # Preferred target: exactly one month before the editing month.
    prev_month = (editing_month - pd.DateOffset(months=1)).normalize().replace(day=1)
    exact = df[df["_month_dt"] == prev_month]
    if not exact.empty:
        return exact.drop(columns=["_month_dt"]).reset_index(drop=True)

    # Fallback: latest month strictly before editing_month — keeps the FG
    # meaningful when the file has gaps (e.g. previous month not refreshed).
    past = df[df["_month_dt"] < editing_month]
    if past.empty:
        return df.drop(columns=["_month_dt"]).iloc[0:0]
    last = past["_month_dt"].max()
    return past[past["_month_dt"] == last].drop(columns=["_month_dt"]).reset_index(drop=True)


def _build_resin_mover_fg(
    resin_calculator_df: pd.DataFrame,
    resin_cost_tracker_df: pd.DataFrame,
    resin_cost_per_lb: float,
    scrape_fraction: float,
    editing_month: pd.Timestamp,
) -> pd.DataFrame:
    """Build a Resin Mover FG DataFrame for one resin-cost driver.

    Pipeline (matches the May-2026 spec exactly)::

        1. resincalculate = Resin_Calculator + new column ``Resin Cost ($/Gal)``
                          = resin_cost_per_lb × Usage (Lbs/Ea)
                            × (1 + scrape_fraction) ÷ Gal/Ea
        2. extract = rows of Resin_Cost_Tracker for the month immediately
           before ``editing_month`` (or the latest month strictly before it
           when the exact previous month is absent).
        3. RIGHT-merge resincalculate (keyed on Pricing Category) onto extract
           (keyed on Resin) — every row of extract is preserved exactly once.
        4. Rename the extract's ``Resin Cost ($/Gal)`` → ``Old Resin Cost ($/Gal)``
           and the calculator-side value → ``New Resin Cost ($/Gal)``.
        5. ``Resin Mover ($/Gal)`` = New − Old.

    Returns a DataFrame with columns
        Product ID | Product Description | Resin |
        Old Resin Cost ($/Gal) | New Resin Cost ($/Gal) | Resin Mover ($/Gal)
    """
    extract = _previous_month_extract(resin_cost_tracker_df, editing_month)
    if extract.empty:
        return pd.DataFrame(columns=list(_FG_OUTPUT_COLUMNS))

    # Collapse Product-ID-only duplicates: the source cost tracker sometimes
    # carries two Product IDs with identical Product Description + Resin
    # (e.g. ``CVF FF 2-1Gal MT`` shows up under 341715 and 341730 with the
    # same $/Gal). Their Resin Mover is identical by construction (it's a
    # pure function of Resin), so surfacing both rows in the FG download
    # only adds noise. Keep the first Product ID for each unique
    # (Product Description, Resin) pair — downstream lookups already
    # collapse by description anyway.
    extract = (
        extract.drop_duplicates(subset=[_COL_PRODUCT_DESC, _COL_RESIN], keep="first")
               .reset_index(drop=True)
    )

    # Step 1 — calculate the new $/Gal cost on the resin_calculator.
    calc = _build_resincalculate(resin_calculator_df, resin_cost_per_lb, scrape_fraction)

    # Step 2 — slim down the calculator side to the join key + the new value,
    # de-duplicating on Pricing Category so the right-merge can't fan out rows.
    calc_slim = (
        calc[[_COL_PRICING_CAT, _COL_RESIN_GAL]]
        .drop_duplicates(subset=[_COL_PRICING_CAT])
        .rename(columns={
            _COL_PRICING_CAT: _COL_RESIN,        # join key alignment
            _COL_RESIN_GAL:   _FG_COL_NEW,
        })
    )

    # Step 3 — right merge so every unique extract row survives, even if no
    # matching Pricing Category exists (those rows get NaN in the New column).
    extract_renamed = extract.rename(columns={_COL_RESIN_GAL: _FG_COL_OLD})
    merged = calc_slim.merge(extract_renamed, on=_COL_RESIN, how="right")

    # Step 4 — Resin Mover ($/Gal) = New − Old.
    new_vals = pd.to_numeric(merged[_FG_COL_NEW], errors="coerce")
    old_vals = pd.to_numeric(merged[_FG_COL_OLD], errors="coerce")
    merged[_FG_COL_NEW]   = new_vals.round(4)
    merged[_FG_COL_OLD]   = old_vals.round(4)
    merged[_FG_COL_MOVER] = (new_vals - old_vals).round(4)

    # Project to the canonical output column order, dropping any cost-tracker
    # columns we don't surface in the FG (e.g. Month).
    return merged[[c for c in _FG_OUTPUT_COLUMNS if c in merged.columns]]


def _build_two_resin_mover_fgs(
    resin_calculator_df: pd.DataFrame,
    resin_cost_tracker_df: pd.DataFrame,
    rest_resin_per_lb: float,
    topco_resin_per_lb: float,
    scrape_fraction: float,
    editing_month: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run the FG pipeline twice and return ``(rest_fg, topco_fg)``.

    ``_build_resin_mover_fg`` copies its inputs internally, but passing the
    unmutated source frames through both calls keeps the call sites obviously
    independent and side-effect-free. ``editing_month`` is shared because both
    movers compare against the same prior-month baseline in the cost tracker.
    """
    rest_fg = _build_resin_mover_fg(
        resin_calculator_df, resin_cost_tracker_df,
        rest_resin_per_lb, scrape_fraction, editing_month,
    )
    topco_fg = _build_resin_mover_fg(
        resin_calculator_df, resin_cost_tracker_df,
        topco_resin_per_lb, scrape_fraction, editing_month,
    )
    return rest_fg, topco_fg


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


def _milk_rate_lookup_for_month(
    milk_mover_tracker_df: pd.DataFrame,
    target_month: Optional[pd.Timestamp],
) -> dict[tuple[str, str], tuple[Optional[float], Optional[float]]]:
    """Return ``{(Category, Class) → (Skim Rate, Butterfat Rate)}`` for a month.

    Lookup keys are upper-cased + whitespace-trimmed for tolerant matching
    against ``Milk_Usage_Stable``. Returns an empty dict (so downstream
    rates collapse to ``None``) when the source file lacks the expected
    columns or has no rows for ``target_month``.
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

    skim_col = next((c for c in df.columns if "skim" in c.lower()), None)
    bf_col   = next((c for c in df.columns if "butter" in c.lower()), None)

    out: dict[tuple[str, str], tuple[Optional[float], Optional[float]]] = {}
    for _, row in matched.iterrows():
        key = (
            str(row["Category"]).strip().upper(),
            str(row["Class"]).strip().upper(),
        )
        skim = _parse_money(row[skim_col]) if skim_col else None
        bf   = _parse_money(row[bf_col])   if bf_col   else None
        out[key] = (skim, bf)
    return out


# Canonical column names added by _build_milk_usage_with_movers. Defined here
# so the builder, the layering step, and any consumer reference one source.
_MUM_COL_START_SKIM    = "Start Month Skim Rate"
_MUM_COL_START_BF      = "Start Month Butterfat Rate"
_MUM_COL_START_COST    = "Start Month Milk Cost"
_MUM_COL_END_SKIM      = "End Month Skim Rate"
_MUM_COL_END_BF        = "End Month Butterfat Rate"
_MUM_COL_END_COST      = "End Month Milk Cost"
_MUM_COL_MILK_COST_GAL = "Milk Cost Mover $/Gal"


def _build_milk_usage_with_movers(
    milk_usage_stable_df: pd.DataFrame,
    milk_mover_tracker_df: pd.DataFrame,
    milk_scrape_fraction: float,
    start_month: Optional[pd.Timestamp],
    end_month: Optional[pd.Timestamp],
) -> pd.DataFrame:
    """Enrich ``Milk_Usage_Stable`` with Start/End month rates, costs, and Mover.

    Columns appended (in order)::

        Start Month Skim Rate | Start Month Butterfat Rate | Start Month Milk Cost
        End Month Skim Rate   | End Month Butterfat Rate   | End Month Milk Cost
        Milk Cost Mover $/Gal

    Per-row formula (Start side; End side is symmetric)::

        Start Month Milk Cost =
            (Start Skim Rate × Skim Usage + Start Butterfat Rate × Butterfat Usage)
            × (1 + Milk Scrape%)

    ``Milk Cost Mover $/Gal`` = End Month Milk Cost − Start Month Milk Cost.

    Returns an empty DataFrame when the source has no rows or the required
    columns are missing — callers handle the "no milk impact" case gracefully.
    """
    out = _strip_df_columns(milk_usage_stable_df).copy()
    required = {"Item Description", "Class", "Category", "Skim Usage", "Butterfat Usage"}
    if out.empty or not required.issubset(out.columns):
        return pd.DataFrame()

    start_lookup = _milk_rate_lookup_for_month(milk_mover_tracker_df, start_month)
    end_lookup   = _milk_rate_lookup_for_month(milk_mover_tracker_df, end_month)

    # Vectorised (Category, Class) key construction — one upper-cased pair
    # per row, used to fetch both Start and End month rates.
    cat = out["Category"].astype(str).str.strip().str.upper()
    cls = out["Class"].astype(str).str.strip().str.upper()
    keys = list(zip(cat, cls))

    out[_MUM_COL_START_SKIM] = [start_lookup.get(k, (None, None))[0] for k in keys]
    out[_MUM_COL_START_BF]   = [start_lookup.get(k, (None, None))[1] for k in keys]
    out[_MUM_COL_END_SKIM]   = [end_lookup.get(k,   (None, None))[0] for k in keys]
    out[_MUM_COL_END_BF]     = [end_lookup.get(k,   (None, None))[1] for k in keys]

    skim_usage = pd.to_numeric(out["Skim Usage"], errors="coerce")
    bf_usage   = pd.to_numeric(out["Butterfat Usage"], errors="coerce")
    s_skim = pd.to_numeric(out[_MUM_COL_START_SKIM], errors="coerce")
    s_bf   = pd.to_numeric(out[_MUM_COL_START_BF],   errors="coerce")
    e_skim = pd.to_numeric(out[_MUM_COL_END_SKIM],   errors="coerce")
    e_bf   = pd.to_numeric(out[_MUM_COL_END_BF],     errors="coerce")

    scrape_factor = 1.0 + float(milk_scrape_fraction)
    out[_MUM_COL_START_COST]    = ((s_skim * skim_usage + s_bf * bf_usage) * scrape_factor).round(4)
    out[_MUM_COL_END_COST]      = ((e_skim * skim_usage + e_bf * bf_usage) * scrape_factor).round(4)
    out[_MUM_COL_MILK_COST_GAL] = (
        out[_MUM_COL_END_COST] - out[_MUM_COL_START_COST]
    ).round(4)
    return out


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

    Outputs (keys in the returned dict):
      * ``rest_htst_resin_mover_fg`` — DataFrame for download.
      * ``topco_resin_mover_fg`` — DataFrame for download.
      * ``mover_details_table_base`` — DataFrame (siv + Freight + Resin movers).
        Milk columns are NOT included here — they are layered at render time
        so they react to the time slicer above the Milk Commodity chart
        without requiring another Refresh click.
      * ``monthly_freight_impact_total`` — float.
      * ``monthly_resin_impact_total`` — float.
      * ``rest_htst_freight_per_gal`` — Optional[float] (used by example prices).
      * ``example_prices_impact`` — Optional[DataFrame] (None when no upload).
      * ``_meta`` — single-row diagnostics DataFrame for debugging.
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
            "(the LAST row drives the impact calculations)."
        )
        return None

    # Pull last-row driver values from the editable tracker. The "editing
    # month" is the Month cell of that same last row — it tells the FG which
    # rows in Resin_Cost_Tracker are the "old" baseline (the month right
    # before the editing month). Falls back to ``current_month`` when the
    # last-row Month is blank so the pipeline never crashes on a partial row.
    last_row = movers_non_milk_df.iloc[-1]
    rest_resin_lbs   = _last_row_value(movers_non_milk_df, _NMT_COL_REST_RESIN)
    topco_resin_lbs  = _last_row_value(movers_non_milk_df, _NMT_COL_TOPCO_RESIN)
    rest_freight_gal = _last_row_value(movers_non_milk_df, _NMT_COL_REST_FREIGHT)

    if rest_resin_lbs is None or topco_resin_lbs is None:
        st.error(
            "❌ The last row of the Movers Non-Milk Tracker must have numeric "
            f"values for both **{_NMT_COL_REST_RESIN}** and "
            f"**{_NMT_COL_TOPCO_RESIN}** before Refresh can run."
        )
        return None

    editing_month = _parse_month(last_row[_NMT_COL_MONTH]) or current_month

    scrape_fraction = _latest_scrape_fraction(uploads["scrape_tracker"].df)

    # 1. Two FG runs. "Old" baseline = rows of Resin_Cost_Tracker for the
    #    month right before ``editing_month``; "New" = freshly computed from
    #    the editable tracker's last-row driver.
    rest_fg, topco_fg = _build_two_resin_mover_fgs(
        uploads["resin_calculator"].df,
        uploads["resin_cost_tracker"].df,
        rest_resin_lbs,
        topco_resin_lbs,
        scrape_fraction,
        editing_month,
    )

    # 2. mover_details_table (Freight + Resin only — milk is layered at render
    #    time). Freight + Resin totals don't depend on the milk slicer, so
    #    they can be computed (and cached) here once per Refresh.  The Month
    #    column is stamped with ``editing_month`` so the cumulative
    #    Lakehouse store can dedupe by month on append.
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

    # 3. Optional example_prices enrichment.
    example_impact = None
    if "example_prices" in uploads:
        ex_df, ex_warn = _build_example_prices_impact_table(
            uploads["example_prices"].df, rest_fg, rest_freight_gal,
        )
        example_impact = ex_df
        if ex_warn:
            st.warning(f"⚠️ {ex_warn}")

    return {
        "rest_htst_resin_mover_fg":     rest_fg,
        "topco_resin_mover_fg":         topco_fg,
        "mover_details_table_base":        combined_base,
        "monthly_freight_impact_total": monthly_freight_total,
        "monthly_resin_impact_total":   monthly_resin_total,
        "rest_htst_freight_per_gal":    rest_freight_gal,
        "example_prices_impact":        example_impact,
        "_meta": pd.DataFrame([{
            "scrape_fraction":      scrape_fraction,
            "rest_resin_$/lbs":     rest_resin_lbs,
            "topco_resin_$/lbs":    topco_resin_lbs,
            "rest_freight_$/gal":   rest_freight_gal,
            "current_month":        current_month.strftime("%Y-%m-%d"),
            "editing_month":        editing_month.strftime("%Y-%m-%d"),
            "fg_old_month":         (
                editing_month - pd.DateOffset(months=1)
            ).strftime("%Y-%m-%d"),
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
  - `example_prices` (RGM, update pricing)
  - `Resin_Cost_Tracker` (RGM, after movers are finalized — the file lives
    in the Pricing Lakehouse and is **auto-appended** for each new month
    when you click **Refresh**; you no longer need to upload it)
- Use the **Movers Non-Milk Tracker** below to align mover values with
  commercial leaders; click **Refresh** to recompute the impact metrics and
  downloads.

_Files are matched automatically by filename keyword after upload._

**Automatic resin-cost workflow**

Both `Resin_Calculator.csv` and `Resin_Cost_Tracker.csv` are sourced from
the Pricing Lakehouse — no upload needed.  When you click **Refresh**:

1. The Resin Mover FGs are recomputed from the editable tracker's last
   row, exactly as before.
2. If the last-row Month is **new** (not already present in the
   lakehouse `Resin_Cost_Tracker.csv`), the latest-month rows are
   duplicated, the Month is restamped to the new month, and
   `Resin Cost ($/Gal)` is overwritten by the FG outputs joined on
   `Product ID`.  The new rows are then appended back to Fabric.
3. The fully-built `mover_details_table` (with the new `Month` column) is
   appended to `Files/Monthly_Mover_Reporting/mover_details_table.csv`
   in the Pricing Lakehouse — but only when the editing month isn't
   already present, so re-clicking Refresh for the same month is safe.
4. The three Mover Downloads (`rest_htst_resin_mover_fg.csv`,
   `topco_resin_mover_fg.csv`, `milk_mover.csv`) are published to
   `Files/Monthly_Pricing_Execution/` in the Pricing Lakehouse —
   authoritative replace on every Refresh, so the canonical drop-zone
   always reflects the latest run.

**Automatic milk-cost workflow**

The milk pipeline is fully automated end-to-end — no manual file upload
required for any milk artefact:

1. **USDA publishes** a new
   [Advanced Prices PDF](https://www.ams.usda.gov/mnreports/dymadvancedprices.pdf)
   → the page detects the change on next render (or click **🔄 USDA refresh**
   to bypass the 1-hour cooldown) and appends the four FMMO rows
   (HTST/ESL × Class I/II) to `fmmo_tracker.json` in Fabric.
2. **Select a new End Month** in the Milk Commodity Cost slicer and click
   **Refresh** → the `milk_mover` CSV becomes available in **Mover Downloads**
   and the per-item end-month cost is appended to
   `base_milk_cost_monthly_tracker.csv` in Fabric (append-only — existing
   months are never overwritten).
3. **HTST pricing unlocks** in the **New Price Quote** view: the Product
   Milk Base Cost auto-update reads the latest end-month from
   `base_milk_cost_monthly_tracker.csv` and refreshes downstream cost
   inputs so a new HTST quote can be generated immediately.

All three artefacts live together in `Files/Milk_cost_tracker/` in the
Pricing Lakehouse for auditability.
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
                "Monthly Milk Impact metric, the mover_details_table, or the "
                "milk_mover download — those always consume every "
                "(Category, Class) row from Milk_Mover_Tracker."
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
                "Impact metric, the mover_details_table, or the milk_mover "
                "download — those always consume every (Category, Class) "
                "row from Milk_Mover_Tracker."
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


def _render_chart(uploads: dict[str, _Uploaded], current_month: pd.Timestamp) -> None:
    """Render the Packaging Index Outlook section (header → metric → chart)."""
    st.markdown("#### 📈 Packaging Index Outlook (from Procurement)")

    pkg = uploads.get("packaging_index") or uploads.get("pkg_index")
    if pkg is None:
        st.info(
            "📈 Upload `Packaging_Index_from_Bryan*.csv` to see the resin & "
            "linerboard trend chart."
        )
        return

    hdpe_value = _current_month_hdpe(pkg.df, current_month)
    metric_col, _spacer = st.columns([1, 1])
    with metric_col:
        st.metric(
            label=f"HDPE ($/lbs) — {current_month.strftime('%b %Y')}",
            value=f"${hdpe_value:.3f}" if hdpe_value is not None else "N/A",
            help="HDPE price from the uploaded Packaging Index for the current month.",
        )

    fig = _build_packaging_index_chart(pkg.df)
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
    """Render the Movers Non-Milk Tracker editor with Refresh + Download buttons.

    Layout
    ------
    The editable table sits on the left (~5/6 of the row); a vertical button
    stack on the right holds **Refresh** (run impact pipeline) and **Download
    Movers_Non_Milk_Tracker.csv** (download whatever the user has currently
    edited, including newly added rows).
    """
    st.markdown("#### 📝 Movers Non-Milk Tracker — fully editable")
    st.caption(
        "Add, remove, or edit rows freely. The **last row** drives the impact "
        "calculations on Refresh."
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
            help="Run the impact pipeline using the LAST row of this table.",
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

        with st.spinner("Running impact calculations..."):
            outputs = _compute_all_outputs(uploads, edited, current_month)
        if outputs is not None:
            st.session_state[f"{_SS_PREFIX}_outputs"] = outputs
            # Refresh-time Lakehouse writes:
            #   1. Resin_Cost_Tracker.csv             → append-only, dedup by month.
            #   2. mover_details_table.csv            → append-only, dedup by month.
            #   3. Monthly_Pricing_Execution/*.csv    → authoritative replace.
            # All three are idempotent and never raise — failures surface
            # as small captions so the user-facing success message below
            # remains accurate for the in-memory pipeline run.
            _run_refresh_lakehouse_appends(outputs, uploads)
            st.success("✅ Calculations complete. Results below.")


# Session-state slot used to dedupe Base Milk Cost tracker append attempts —
# once we've appended (or confirmed an end_month is already present in the
# tracker) for a given (end_month) value, we don't hit Fabric again for that
# month in this session.
_SS_BASE_MILK_TRACKER_LAST_END = "monthly_movers_base_milk_tracker_last_end"


def _maybe_append_to_base_milk_cost_tracker(
    milk_usage_with_movers: pd.DataFrame,
    end_month: Optional[pd.Timestamp],
) -> None:
    """Append [Item, Item Description, End Month Milk Cost, End Month] to Fabric.

    Honours the contract from the migration plan:

        "whenever both a new milk_mover CSV is generated AND a new end
         month is selected that doesn't exist in this file yet, append
         the new rows to the Base Milk Cost Monthly Tracker."

    "New milk_mover CSV is generated" = ``milk_usage_with_movers`` is a
    non-empty frame (i.e. the milk pipeline successfully ran for the
    current slicer selection). "New end month" = the End Month selected
    by the slicer is not already present in the OneLake tracker.

    Idempotent: the underlying ``append_rows_for_end_month`` no-ops when
    the month is already there, so re-calling on a slicer toggle is safe.
    A session-state cache short-circuits the Fabric round-trip when we
    just confirmed the same End Month milliseconds ago.

    Failures are swallowed (logged + a non-fatal info badge); never
    breaks the page render.
    """
    if (milk_usage_with_movers is None
            or milk_usage_with_movers.empty
            or end_month is None):
        return

    em = pd.Timestamp(end_month).normalize().replace(day=1)
    if st.session_state.get(_SS_BASE_MILK_TRACKER_LAST_END) == em:
        return  # already handled this end_month in this session

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
        rows_appended, was_new = (
            _base_milk_cost_tracker.append_rows_for_end_month(payload, em)
        )
    except _base_milk_cost_tracker.BaseMilkCostTrackerError as exc:
        # Render a small caption so the operator knows the tracker isn't
        # up to date, but DON'T raise — milk-impact rendering still works.
        st.caption(
            f"⚠️ Could not update base_milk_cost_monthly_tracker: {exc}"
        )
        return

    st.session_state[_SS_BASE_MILK_TRACKER_LAST_END] = em
    if was_new and rows_appended:
        st.caption(
            f"✅ Appended {rows_appended} row(s) for {em:%Y-%m} to "
            f"{_base_milk_cost_tracker.get_store_label()}."
        )


# ── Refresh-time Lakehouse appends ────────────────────────────────────────────
#
# Two append-only Fabric writes fire on a successful Refresh click:
#   1. Resin_Cost_Tracker.csv  — duplicate latest-month rows for the new
#      editing month, overwrite Resin Cost ($/Gal) from the FG outputs.
#   2. mover_details_table.csv — append the freshly-built rows for the new
#      editing month to the cumulative monthly-mover-reporting file.
#
# Both helpers are idempotent (the stores no-op when the month already
# exists), so a re-click on Refresh for the same month is safe.  Errors are
# surfaced as small captions; they NEVER raise, so the rest of the Refresh
# pipeline is unaffected by transient lakehouse blips.

# Session-state slots to short-circuit duplicate Fabric round-trips when the
# user clicks Refresh repeatedly for the same editing month within a session.
_SS_RESIN_TRACKER_LAST_MONTH         = f"{_SS_PREFIX}_resin_tracker_last_month"
_SS_MOVER_DETAILS_TABLE_LAST_MONTH   = f"{_SS_PREFIX}_mover_details_table_last_month"


def _combine_fg_new_resin_lookups(
    rest_fg: pd.DataFrame,
    topco_fg: pd.DataFrame,
) -> dict[str, float]:
    """Merge the two Resin Mover FG outputs into a ``{Product ID → New Resin Cost}``.

    Both FGs carry the same column schema; the lookup combines them into
    one map keyed on stripped ``Product ID``.  When a Product ID appears in
    both FGs (rare; happens for shared SKUs) the Rest HTST value wins —
    Rest HTST is the dominant SKU set in the Pricing Lakehouse, so its
    value is the more representative one to keep in the long-term cost
    tracker baseline.

    Returns an empty dict when both FGs are empty or lack the expected
    columns; the caller then short-circuits the append since there are no
    new $/Gal values to write.
    """
    pid_col = _resin_store.COL_PRODUCT_ID
    new_col = _FG_COL_NEW

    def _one(fg_df: pd.DataFrame) -> dict[str, float]:
        if (fg_df is None or fg_df.empty
                or pid_col not in fg_df.columns
                or new_col not in fg_df.columns):
            return {}
        sliced = fg_df[[pid_col, new_col]].dropna(subset=[new_col])
        return {
            str(pid).strip(): float(val)
            for pid, val in zip(sliced[pid_col], sliced[new_col])
            if str(pid).strip()
        }

    # Rest wins on collision — see docstring rationale.
    combined = _one(topco_fg)
    combined.update(_one(rest_fg))
    return combined


def _maybe_append_resin_cost_tracker_for_month(
    rest_fg: pd.DataFrame,
    topco_fg: pd.DataFrame,
    editing_month: pd.Timestamp,
) -> None:
    """Append duplicated latest-month rows to Resin_Cost_Tracker.csv when new.

    Implements the May-2026 contract verbatim:

        "the resin_cost_tracker in lakehouse will be appended only when a
         new month is created in the 'Movers Non-Milk Tracker' table and
         after user hit refresh, it will be updated by duplicate the rows
         from the latest month in the 'resin cost tracker' csv, change
         month to the month in the last row of 'Movers Non-Milk Tracker'
         table, and update the 'Resin Cost ($/Gal)' in the new month rows
         only by pulling data from 'New Resin Cost ($/Gal)' column in both
         'rest htst resin mover fg' csv and 'topco resin_mover_fg' csv
         through product ID match."

    No-op when ``editing_month`` is already present in the lakehouse copy
    (the underlying store handles the dedup).  Failures surface as a small
    caption — never raised — so a transient Fabric outage cannot break the
    rest of the Refresh pipeline.
    """
    if editing_month is None:
        return

    em = pd.Timestamp(editing_month).normalize().replace(day=1)
    if st.session_state.get(_SS_RESIN_TRACKER_LAST_MONTH) == em:
        return  # already handled this editing month in this session

    new_resin_lookup = _combine_fg_new_resin_lookups(rest_fg, topco_fg)
    if not new_resin_lookup:
        # FGs returned no usable Product-ID → $/Gal pairs — nothing to push.
        return

    try:
        rows_appended, was_new = _resin_store.append_new_month_from_latest(
            em, new_resin_cost_lookup=new_resin_lookup,
        )
    except _resin_store.ResinCostTrackerStoreError as exc:
        st.caption(f"⚠️ Could not update Resin_Cost_Tracker: {exc}")
        return

    st.session_state[_SS_RESIN_TRACKER_LAST_MONTH] = em
    if was_new and rows_appended:
        # Drop the cached upload so the next render re-reads the new rows
        # from Fabric — without this the page would still show the stale
        # snapshot until the 5-minute TTL expires.
        st.caption(
            f"✅ Appended {rows_appended} row(s) for {em:%Y-%m} to "
            f"{_resin_store.get_cost_tracker_label()}."
        )


def _maybe_append_mover_details_table_for_month(
    mover_details_table: pd.DataFrame,
    editing_month: pd.Timestamp,
) -> None:
    """Append the freshly-built mover_details_table rows to the cumulative file.

    Implements the May-2026 contract verbatim:

        "include a new column 'Month' and put value as the last month of
         the 'Movers Non-Milk Tracker', and append the rows into the
         [pricing lakehouse mover_details_table.csv] only when user hit
         'refresh' and when a new month in the generated mover_details_table
         csv does not exist in the one in the pricing lakehouse."

    No-op when ``editing_month`` is already present in the lakehouse copy.
    Failures surface as a small caption — never raised.
    """
    if mover_details_table is None or mover_details_table.empty or editing_month is None:
        return

    em = pd.Timestamp(editing_month).normalize().replace(day=1)
    if st.session_state.get(_SS_MOVER_DETAILS_TABLE_LAST_MONTH) == em:
        return  # already handled this editing month in this session

    try:
        rows_appended, was_new = _mover_details_store.append_for_month_if_new(
            mover_details_table, em,
        )
    except _mover_details_store.MoverDetailsTableStoreError as exc:
        st.caption(f"⚠️ Could not update mover_details_table: {exc}")
        return

    st.session_state[_SS_MOVER_DETAILS_TABLE_LAST_MONTH] = em
    if was_new and rows_appended:
        st.caption(
            f"✅ Appended {rows_appended} row(s) for {em:%Y-%m} to "
            f"{_mover_details_store.get_store_label()}."
        )


def _publish_mover_downloads_to_lakehouse(
    rest_fg: pd.DataFrame,
    topco_fg: pd.DataFrame,
    milk_mover_df: Optional[pd.DataFrame],
) -> None:
    """Replace the canonical Monthly_Pricing_Execution CSVs on every Refresh.

    Mirrors the contract from the May-2026 spec:

        "change the code to a copy of the rest_htst_resin_mover fg,
         topco resin mover fg and milk mover csv into this pricing
         lakehouse: Files/Monthly_Pricing_Execution …; If there is
         already data there, then replace the files there with new
         generated files, only when user hit refresh."

    Empty / missing frames are skipped silently so a partial pipeline
    run (e.g. milk slicer not yet selected) cannot accidentally clobber
    a prior good copy.  Failures surface as small captions — they NEVER
    raise so a transient Fabric outage cannot break the rest of the
    Refresh pipeline.
    """
    payload: dict[str, Optional[pd.DataFrame]] = {
        "rest_fg":    rest_fg,
        "topco_fg":   topco_fg,
        "milk_mover": milk_mover_df,
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
        "rest_fg":    "rest_htst_resin_mover_fg.csv",
        "topco_fg":   "topco_resin_mover_fg.csv",
        "milk_mover": "milk_mover.csv",
    }
    files_caption = ", ".join(label_for[r] for r in written)
    st.caption(
        f"✅ Published {files_caption} to "
        f"{_mpe_store.get_folder_label()}."
    )


def _run_refresh_lakehouse_appends(outputs: dict, uploads: dict[str, _Uploaded]) -> None:
    """Run every Refresh-time Lakehouse write with a single editing-month derivation.

    Wired only from the Refresh button handler so reactive milk-slicer
    changes (which don't change the editing month) never re-fire these
    writes.  Auto-derives the editing month from the cached editable
    tracker; falls back silently when the tracker is empty so a transient
    state never raises here.

    Three lakehouse writes happen here, in dependency order:

      1. **Resin_Cost_Tracker.csv** — append-only, idempotent on
         already-tracked months.
      2. **mover_details_table.csv** — append-only, idempotent on
         already-tracked months.
      3. **Monthly_Pricing_Execution/{rest, topco, milk}.csv** —
         authoritative replace on every Refresh (no month gate).
    """
    nmt_df: pd.DataFrame = st.session_state.get(_SS_NMT_DF)
    if nmt_df is None or nmt_df.empty:
        return
    editing_month = _parse_month(nmt_df.iloc[-1][_NMT_COL_MONTH])
    if editing_month is None:
        return

    rest_fg = outputs.get("rest_htst_resin_mover_fg", pd.DataFrame())
    topco_fg = outputs.get("topco_resin_mover_fg", pd.DataFrame())
    base_table: pd.DataFrame = outputs.get("mover_details_table_base", pd.DataFrame())

    # Mover details table append uses the freshest available frame —
    # ideally the milk-layered version so the historical record carries
    # the milk columns too.  Fall back to the no-milk base when the milk
    # pipeline didn't run (uploads incomplete or upstream error).
    milk_usage_with_movers = _compute_milk_usage_for_render(uploads)
    full_table, _ignored = _layer_milk_on_mover_details_table(
        base_table, milk_usage_with_movers,
    )

    _maybe_append_resin_cost_tracker_for_month(rest_fg, topco_fg, editing_month)
    _maybe_append_mover_details_table_for_month(full_table, editing_month)

    # Authoritative replace of the three Mover Downloads in a dedicated
    # Monthly_Pricing_Execution OneLake folder.  Always fires on Refresh —
    # there is no month gate because the canonical drop-zone always
    # holds the latest run's outputs.
    _publish_mover_downloads_to_lakehouse(rest_fg, topco_fg, milk_usage_with_movers)


def _compute_milk_usage_for_render(
    uploads: dict[str, _Uploaded],
) -> Optional[pd.DataFrame]:
    """Build the milk usage table from cached uploads + the live time slicer.

    Returns ``None`` when the milk inputs (Milk_Mover_Tracker AND
    Milk_Usage_Stable) are not both uploaded — the layering step then leaves
    the milk columns blank, which is the intended graceful-degrade behaviour.

    Reads the slicer values from ``session_state`` so the entire downstream
    mover_details_table reacts to slicer changes without requiring another Refresh.

    Side effect: when a fresh DataFrame comes back AND the selected End
    Month isn't already in ``base_milk_cost_monthly_tracker.csv``, we
    append the per-item ``End Month Milk Cost`` rows for that month.
    See :func:`_maybe_append_to_base_milk_cost_tracker` for details.
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

    _maybe_append_to_base_milk_cost_tracker(milk_usage_with_movers, end_month)

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
    """Render Impact metrics + Mover Downloads + mover_details_table + Example prices.

    Surfaced only after a successful Refresh — otherwise this is a no-op.
    Section order (per the May-2026 product spec):

        1. Three headline Impact metrics (Resin / Freight / Milk).
        2. **Mover Downloads** — Rest HTST FG, TOPCO FG, and the slicer-driven
           ``milk_mover`` (milk_usage_table + Milk Cost Mover $/Gal).
        3. **mover_details_table download** — single combined CSV used for the
           monthly pricing update.
        4. Example prices (when uploaded).

    The Milk Mover columns and the ``Monthly Milk Impact`` metric are computed
    each render from the live time slicer + cached uploads, so changing the
    slicer reactively updates everything below WITHOUT requiring Refresh.
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
    st.markdown("### Impact")
    st.caption(
        "Metrics are summed from the mover_details_table; download it "
        "below for the full per-SKU breakdown. Milk Impact reacts to the "
        "Start/End Month slicer above the Milk Commodity Cost chart."
    )

    # ── 1. Headline metrics ──────────────────────────────────────────────────
    m1, m2, m3 = st.columns(3, gap="medium")
    with m1:
        st.metric(
            label="Monthly Resin Impact",
            value=f"${monthly_resin_total:,.2f}",
            help=(
                "Σ(Monthly Resin Mover) over site_item_volume rows — Resin "
                "Mover comes from the editable tracker's last row, with FG "
                "fallback for Rest HTST and TOPCO."
            ),
        )
    with m2:
        st.metric(
            label="Monthly Freight Impact",
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
                "Monthly Milk Impact (Make Sure the Start and End Month are "
                "Selected Correctly to see MOM Change)"
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

    Includes the editable Movers Non-Milk Tracker AND the milk-chart time
    slicer so "Change files" returns the section to a fully pristine state —
    uploads, edits, slicer selections, and computed outputs all reset
    together.
    """
    for key in (
        f"{_SS_PREFIX}_uploads",
        f"{_SS_PREFIX}_sig",
        f"{_SS_PREFIX}_outputs",
        _SS_NMT_DF,
        _SS_MILK_START,
        _SS_MILK_END,
        _SS_MILK_CATEGORY,
        _SS_MILK_CLASS,
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
          the Impact section follow.
    """
    current_month = pd.Timestamp(date.today().replace(day=1))

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
