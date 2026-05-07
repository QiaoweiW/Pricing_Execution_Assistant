"""
New Price Quote page view.

This module provides the UI for querying and viewing pricing data.

Data flow
---------
The 9 source CSVs (``Product_Class_Plant.csv`` etc.) live in Microsoft
Fabric Lakehouse at ``Files/Activity_Model/<filename>.csv``. The page
itself never asks the user to upload them anymore. On every render:

1. ``data_sources.htst_activity_store.bootstrap_from_local_if_empty``
   pushes the local seed copies up to Fabric on the first ever load
   (idempotent — no-op afterwards).
2. We compute a "Fabric signature" (the tuple of ETags across all 9
   files) and compare it to the signature recorded next to the cached
   ``pricing_data.parquet`` file. If anything changed (or the parquet
   doesn't yet exist), we materialise the 9 CSVs to a temp directory
   and re-run the existing ``processing/new_pricing_processor.py``
   subprocess, exactly as the legacy upload path did. Otherwise the
   cached parquet is reused — no reprocessing.
3. The query / filter UI is unchanged.

The "Upload to refresh database" panel has been replaced by:
  * A **Download** strip — one ``st.download_button`` per file that
    serves the byte-for-byte copy currently in Fabric.
  * An **Upload-to-replace** strip — single ``st.file_uploader``;
    each file's name must match one of the 9 known filenames; the
    column set is strict-validated against the canonical schema; on
    success the file is pushed back to Fabric and the parquet is
    rebuilt in the same render.

Features:
- Multi-item search by item number or description (semicolon-separated)
- Filtering by plant, volume brackets, pallet, mileage, and drop size
- CSV export of filtered results
- Per-file download / upload-to-replace round-trip with Fabric
"""
import streamlit as st
import pandas as pd
import json
import sys
import subprocess
import tempfile
import datetime
from pathlib import Path
from typing import Optional

from utils.ui_helpers import apply_custom_css

from data_sources import htst_activity_store as _activity_store
from data_sources import product_milk_base_cost_updater as _pmbc_updater

# Required CSV files for database creation. Kept in sync with the
# ``htst_activity_store.EXPECTED_FILES`` table at import time so the
# legacy "REQUIRED_FILES" list and the new Fabric store can never drift.
REQUIRED_FILES = [spec.filename for spec in _activity_store.EXPECTED_FILES]

# Sidecar JSON that records the Fabric ETag signature the cached parquet
# was built from. Lives next to the parquet in ``tempfile.gettempdir()``.
# When the recorded signature differs from the live one, we rebuild.
_PARQUET_SIDECAR_NAME: str = "pricing_data.fabric_signature.json"

# Session-state key used to remember whether bootstrap-from-local has
# already been attempted in this session (so we don't re-run it on every
# rerun — the function itself is idempotent, but skipping the network
# round-trip is cheaper).
_SS_BOOTSTRAP_DONE: str = "_npq_activity_bootstrap_done"

# Session-state slot recording the most recent PMBC auto-update outcome.
# We re-run on every page render (so a tab-switch back from Market
# Barometer picks up newly-appended tracker rows quickly), but skip
# the Fabric round-trip when the previous run in this session SUCCEEDED
# within the freshness window below — keeps the UI responsive on
# Streamlit reruns triggered by filter clicks etc.
_SS_PMBC_LAST_RUN: str = "_npq_pmbc_last_auto_update"

# How long a successful auto-update is considered "fresh" within the
# same Streamlit session. Re-running inside this window is a no-op
# (no Fabric I/O); after the window we re-evaluate against the
# tracker's latest month so a Market-Barometer Refresh in another
# tab is picked up promptly.
_PMBC_FRESHNESS_SECONDS: int = 60


def load_pricing_data():
    """
    Load pricing data from parquet file or session state.
    
    Returns:
        tuple: (DataFrame or None, record_count, file_size_mb, file_mod_time)
    """
    temp_dir = Path(tempfile.gettempdir())
    parquet_path = temp_dir / "pricing_data.parquet"
    
    if parquet_path.exists():
        try:
            df = pd.read_parquet(parquet_path)
            record_count = len(df)
            file_size_mb = parquet_path.stat().st_size / (1024 * 1024)
            file_mod_time = datetime.datetime.fromtimestamp(parquet_path.stat().st_mtime)
            return df, record_count, file_size_mb, file_mod_time
        except Exception as e:
            st.error(f"Error loading parquet file: {e}")
            return None, 0, 0, None
    
    # Try to load from session state if available
    if hasattr(st.session_state, 'processed_df') and st.session_state.processed_df is not None:
        df = st.session_state.processed_df
        record_count = len(df)
        file_size_mb = 0  # Unknown
        file_mod_time = datetime.datetime.now()
        return df, record_count, file_size_mb, file_mod_time
    
    return None, 0, 0, None


def display_database_status(record_count, file_size_mb, file_mod_time):
    """
    Display database connection status in a 4-column layout.
    
    Args:
        record_count: Number of records in database
        file_size_mb: File size in MB
        file_mod_time: File modification time
    """
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.success("✅ Database Connected")
    with col2:
        st.info(f"📊 {record_count:,} records")
    with col3:
        st.info(f"📁 {file_size_mb:.1f} MB")
    with col4:
        st.info(f"🕒 {file_mod_time.strftime('%Y-%m-%d %H:%M')}")


def initialize_filter_session_state(available_options):
    """
    Initialize session state for all filters with default values (all options selected).
    
    Args:
        available_options: Dictionary mapping filter names to available option lists
    """
    for filter_name, options in available_options.items():
        session_key = f'filter_{filter_name}'
        if session_key not in st.session_state:
            st.session_state[session_key] = options
    
    # Initialize item search separately
    if 'filter_item_search' not in st.session_state:
        st.session_state.filter_item_search = ""
    # Initialize item description search separately
    if 'filter_item_description_search' not in st.session_state:
        st.session_state.filter_item_description_search = ""


def _apply_semicolon_substring_filter(df: pd.DataFrame, column: str, search_text: str) -> pd.DataFrame:
    """Filter ``df`` to rows where ``column`` contains ANY of the
    semicolon-separated terms in ``search_text`` (case-insensitive,
    literal substring match).

    Returns ``df`` unchanged when:
      * ``search_text`` is empty / whitespace-only,
      * every term strips to empty,
      * ``column`` is missing from the frame.
    """
    if not search_text or column not in df.columns:
        return df
    terms = [t.strip() for t in search_text.split(';') if t.strip()]
    if not terms:
        return df

    series = df[column].astype(str)
    mask = pd.Series(False, index=df.index)
    for term in terms:
        mask |= series.str.contains(term, case=False, na=False, regex=False)
    return df[mask]


def apply_item_search_filter(df, search_text):
    """Filter by item number(s), separated by ``;``."""
    return _apply_semicolon_substring_filter(df, 'Item', search_text)


def apply_item_description_search_filter(df, search_text):
    """Filter by item description(s), separated by ``;``."""
    return _apply_semicolon_substring_filter(df, 'Item Description', search_text)


def format_numeric_columns(df, decimal_places=4):
    """
    Format numeric columns for display with specified decimal places.
    
    Args:
        df: DataFrame to format
        decimal_places: Number of decimal places to display
        
    Returns:
        DataFrame with formatted numeric columns
    """
    display_df = df.copy()
    numeric_columns = [
        col for col in display_df.columns
        if '($/Gal)' in col or '($/gal)' in col or 'per Gallon' in col or 'per Each' in col or 'per Case' in col
    ]
    
    for col in numeric_columns:
        if col in display_df.columns:
            display_df[col] = display_df[col].apply(
                lambda x: f"{x:.{decimal_places}f}" if pd.notna(x) and isinstance(x, (int, float)) else x
            )
    
    return display_df


# ── Fabric → parquet plumbing ─────────────────────────────────────────────────

def _ensure_activity_bootstrap() -> dict[str, bool]:
    """First-run-of-session bootstrap of the Activity_Model folder in Fabric.

    Idempotent OneLake-side too (each file is bootstrap-only-if-absent), so
    re-calling on a populated lakehouse is harmless. The session-state
    sentinel just spares us the network round-trip when we already know
    the bootstrap fired earlier in this session.

    Returns ``{filename: was_uploaded_now}`` only on the first call;
    returns ``{}`` thereafter.
    """
    if st.session_state.get(_SS_BOOTSTRAP_DONE):
        return {}
    written = _activity_store.bootstrap_from_local_if_empty(
        _activity_store.DEFAULT_SEED_DIR,
    )
    st.session_state[_SS_BOOTSTRAP_DONE] = True
    return written


def _read_recorded_signature(parquet_dir: Path) -> dict[str, str]:
    """Read the sidecar JSON that records the ETags the parquet was built from."""
    sidecar = parquet_dir / _PARQUET_SIDECAR_NAME
    if not sidecar.exists():
        return {}
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_recorded_signature(parquet_dir: Path, sig: dict[str, str | None]) -> None:
    """Persist the Fabric ETag signature so the next render can short-circuit."""
    sidecar = parquet_dir / _PARQUET_SIDECAR_NAME
    sidecar.write_text(
        json.dumps({k: ("" if v is None else v) for k, v in sig.items()}, indent=2),
        encoding="utf-8",
    )


def _rebuild_parquet_from_fabric(parquet_dir: Path) -> tuple[bool, str]:
    """Materialise the 9 CSVs from Fabric and rebuild the parquet.

    Returns ``(success, message)``. ``message`` is the processor stderr on
    failure, or an empty string on success. Caller is responsible for
    surfacing the message to the UI; this function just runs the build
    and returns the outcome.
    """
    # Materialise the 9 CSVs to the same temp dir the legacy upload path used —
    # the processor's __main__ block looks for ``*.csv`` files there.
    parquet_dir.mkdir(parents=True, exist_ok=True)
    try:
        _activity_store.materialise_to_dir(parquet_dir)
    except _activity_store.ActivityModelStoreError as exc:
        return False, f"OneLake fetch failed: {exc}"

    script_path = Path("processing/new_pricing_processor.py")
    if not script_path.exists():
        return False, f"Processing script not found at {script_path}"

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "Processing timed out after 5 minutes."
    except Exception as exc:  # noqa: BLE001
        return False, f"Processor invocation failed: {exc}"

    if result.returncode != 0:
        return False, result.stderr or "Processing failed (no stderr)."
    return True, ""


def _ensure_pricing_data_from_fabric() -> tuple[Optional[pd.DataFrame], int, float, Optional[datetime.datetime], Optional[str]]:
    """Make sure the cached parquet is up-to-date with Fabric, then load it.

    Returns the same 4-tuple as :func:`load_pricing_data` plus a 5th
    element: an optional error message. When there's an error we try to
    fall back to the cached parquet (so the page is still usable on a
    transient network blip), and surface the error in the banner.
    """
    parquet_dir = Path(tempfile.gettempdir())
    parquet_path = parquet_dir / "pricing_data.parquet"

    try:
        live_sig = _activity_store.fabric_etags()
    except _activity_store.ActivityModelStoreError as exc:
        # OneLake unreachable. Fall back to whatever is in the cached
        # parquet (if anything) so the page is still usable.
        df, rc, mb, mt = load_pricing_data()
        return df, rc, mb, mt, f"Fabric unreachable; using last cached data. {exc}"

    recorded_sig = _read_recorded_signature(parquet_dir)
    needs_rebuild = (
        not parquet_path.exists()
        or {k: ("" if v is None else v) for k, v in live_sig.items()} != recorded_sig
    )

    if needs_rebuild:
        ok, msg = _rebuild_parquet_from_fabric(parquet_dir)
        if not ok:
            df, rc, mb, mt = load_pricing_data()
            return df, rc, mb, mt, msg
        _write_recorded_signature(parquet_dir, live_sig)

    df, rc, mb, mt = load_pricing_data()
    return df, rc, mb, mt, None


# ── Product_Milk Base Cost auto-update ───────────────────────────────────────
#
# Single source of truth: the tracker's latest End Month
# (``base_milk_cost_monthly_tracker.csv``). On every render we ask the
# updater "is PMBC behind the tracker?" — if yes, refresh PMBC by
# left-joining tracker rows for that month onto PMBC by Item; if no,
# silently no-op. A session-state freshness sentinel skips the Fabric
# round-trip when the previous successful run is still inside
# ``_PMBC_FRESHNESS_SECONDS``.

def _pmbc_run_was_recent_success() -> bool:
    """Return True when the previous auto-update in this session
    succeeded inside the freshness window. Used to skip redundant
    Fabric I/O on rapid Streamlit reruns (filter clicks etc.).
    """
    last = st.session_state.get(_SS_PMBC_LAST_RUN, {})
    if not last.get("ok"):
        return False
    ran_at = last.get("ran_at_epoch")
    if ran_at is None:
        return False
    return (datetime.datetime.now().timestamp() - ran_at) < _PMBC_FRESHNESS_SECONDS


def _run_pmbc_auto_update(*, force: bool = False) -> None:
    """Run the tracker-driven PMBC auto-update. Idempotent and silent
    on success.

    Behaviour
    ---------
    * Reads ``base_milk_cost_monthly_tracker.latest_month()``; if PMBC's
      ``Month`` already matches (or exceeds) it, no-op.
    * Otherwise refreshes PMBC by left-joining the tracker's rows for
      that month onto PMBC by ``Item``.
    * Skips the Fabric round-trip when the last successful run in this
      session is still inside the freshness window (unless ``force``).
    * On failure, renders a yellow warning + retry button so the user
      can act without leaving the page.
    """
    if not force and _pmbc_run_was_recent_success():
        return

    try:
        result = _pmbc_updater.update_if_needed()
    except Exception as exc:  # noqa: BLE001 — defensive top-level guard
        result = _pmbc_updater.UpdateResult(
            ok=False, target_month=None,
            message=f"Unexpected error during auto-update: {exc}",
        )

    st.session_state[_SS_PMBC_LAST_RUN] = {
        "ok": result.ok,
        "message": result.message,
        "skipped_reason": result.skipped_reason,
        "rows_updated": result.rows_updated,
        "rows_blanked": result.rows_blanked,
        "rows_unmatched": result.rows_unmatched,
        "target_month": (
            result.target_month.isoformat() if result.target_month is not None else None
        ),
        "ran_at_epoch": datetime.datetime.now().timestamp(),
    }

    if not result.ok:
        st.warning(
            "⚠️ **Product_Milk Base Cost auto-update failed.** "
            f"{result.message} "
            "The pricing database is still using the last known values. "
            "Click the button below to retry."
        )
        if st.button(
            "🔁 Run auto-update now",
            key="npq_pmbc_manual_retry",
            type="primary",
        ):
            _run_pmbc_auto_update(force=True)
            st.rerun()
        return

    # Success branches. Stay quiet in steady state; surface info when
    # the tracker is empty (the common first-time-bootstrap state) and
    # show a success banner when an actual write happened.
    if result.skipped_reason == "tracker-empty":
        return  # quiet — first-load before any Market Barometer refresh
    if result.skipped_reason == "already-current":
        return  # quiet — nothing to do
    if result.rows_updated or result.rows_blanked:
        st.success(f"☁️ Auto-updated Product_Milk Base Cost: {result.message}")


def _render_pmbc_manual_panel() -> None:
    """Render a status caption + always-visible manual "Run now" button.

    Placed inside the Fabric data-management section so an operator can
    force a refresh after a back-dated tracker edit, or kick the
    update from a different tab session, without waiting on the
    freshness window.
    """
    last = st.session_state.get(_SS_PMBC_LAST_RUN, {})
    target = last.get("target_month")
    msg = last.get("message", "")
    if last.get("ok"):
        if last.get("rows_updated", 0) or last.get("rows_blanked", 0):
            tone = "✅"
        elif last.get("skipped_reason") == "tracker-empty":
            tone = "ℹ️"
        else:
            tone = "✅"
        st.caption(
            f"{tone} Last auto-update: target month "
            f"{target or 'n/a'} — {msg or 'no changes needed.'}"
        )
    elif msg:
        st.caption(f"⚠️ Last auto-update failed: {msg}")

    if st.button(
        "🔁 Run Product_Milk Base Cost auto-update now",
        key="npq_pmbc_manual_panel_button",
        help=(
            "Force a refresh of Product_Milk Base Cost from the latest "
            "End Month in base_milk_cost_monthly_tracker.csv. Normally "
            "runs automatically on every page load."
        ),
    ):
        _run_pmbc_auto_update(force=True)
        st.rerun()


# ── Fabric data-management UI ────────────────────────────────────────────────

def _render_fabric_data_panel() -> None:
    """Render the Download + Upload-to-replace round-trip with Fabric.

    Two expanders:

    * **Download files from Fabric** — one ``st.download_button`` per
      file. Bytes are streamed straight from OneLake (no pandas
      round-trip) so the user gets the EXACT current copy.
    * **Upload to replace files in Fabric** — single multi-file
      uploader. Filename matching is strict: each uploaded file must
      have a name in :data:`REQUIRED_FILES`. The column set is also
      strict-validated (see ``htst_activity_store._validate_schema``)
      before anything is written. On success: each matching file is
      pushed back to Fabric with an ``If-Match`` ETag, the parquet
      cache is invalidated, and the page reruns to pick up the
      reprocessed data.
    """
    with st.expander("⬇️ Download files from Fabric (Activity_Model)", expanded=False):
        st.caption(
            "Each button serves the byte-for-byte copy currently stored in "
            f"`{_activity_store.get_store_label()}`."
        )
        cols = st.columns(3)
        for i, spec in enumerate(_activity_store.EXPECTED_FILES):
            with cols[i % 3]:
                try:
                    raw = _activity_store.read_raw_bytes(spec.filename)
                except _activity_store.ActivityModelStoreError as exc:
                    st.error(f"❌ {spec.filename}: {exc}")
                    continue
                st.download_button(
                    label=f"⬇️ {spec.filename}",
                    data=raw,
                    file_name=spec.filename,
                    mime="text/csv",
                    help=spec.description,
                    key=f"npq_dl_{spec.filename}",
                    width="stretch",
                )

    with st.expander("⬆️ Upload to replace files in Fabric", expanded=False):
        st.markdown(
            "Drop one or more of the canonical CSVs below. Each file's "
            "**name must exactly match** one of the 9 expected filenames, "
            "and its **column set must match** the canonical schema. "
            "Validation runs before anything is written to Fabric."
        )
        st.caption(f"Expected filenames: {', '.join(REQUIRED_FILES)}.")
        uploaded_files = st.file_uploader(
            "Choose one or more CSV files to push to Fabric",
            type=["csv"],
            accept_multiple_files=True,
            key="npq_replace_uploader",
        )

        if uploaded_files:
            # Filename match — every uploaded file's name must be one of
            # the 9 expected names. We DON'T silently skip mismatches:
            # a user uploading a wrongly-named file should see a hard error.
            valid: list[tuple[str, bytes]] = []
            invalid_names: list[str] = []
            for f in uploaded_files:
                if f.name in REQUIRED_FILES:
                    valid.append((f.name, f.getbuffer().tobytes()))
                else:
                    invalid_names.append(f.name)

            if invalid_names:
                st.error(
                    "❌ Filename mismatch — these files are not part of the "
                    "Activity_Model contract: "
                    + ", ".join(invalid_names)
                    + f". Expected one of: {', '.join(REQUIRED_FILES)}."
                )

            if valid:
                st.info(f"📦 Ready to push {len(valid)} file(s) to Fabric.")
                if st.button(
                    "🚀 Push to Fabric (replace + reprocess)",
                    type="primary",
                    use_container_width=True,
                    key="npq_replace_push",
                ):
                    schema_errors: list[str] = []
                    write_errors: list[str] = []
                    pushed: list[str] = []
                    for name, raw in valid:
                        try:
                            _activity_store.write_csv_bytes(name, raw)
                            pushed.append(name)
                        except _activity_store.ActivityModelSchemaError as exc:
                            schema_errors.append(str(exc))
                        except _activity_store.ActivityModelStoreError as exc:
                            write_errors.append(f"{name}: {exc}")

                    if schema_errors:
                        for msg in schema_errors:
                            st.error(f"❌ Schema error: {msg}")
                    if write_errors:
                        for msg in write_errors:
                            st.error(f"❌ Write error: {msg}")

                    if pushed:
                        st.success(
                            f"✅ Pushed {len(pushed)} file(s) to Fabric: "
                            + ", ".join(pushed)
                            + ". Rebuilding the pricing database…"
                        )
                        # Invalidate the cached parquet's signature so the
                        # next render rebuilds from the freshly-pushed data.
                        sidecar = (
                            Path(tempfile.gettempdir()) / _PARQUET_SIDECAR_NAME
                        )
                        try:
                            if sidecar.exists():
                                sidecar.unlink()
                        except OSError:
                            pass
                        # Reset filter state — schema didn't change but
                        # values may have, so a clean filter view is safer.
                        for k in (
                            "query_executed",
                            "filter_item_search",
                            "filter_item_description_search",
                        ):
                            st.session_state.pop(k, None)
                        st.rerun()


# ── Render ───────────────────────────────────────────────────────────────────

def render():
    """Render the New Price Quote page."""
    apply_custom_css()
    
    st.markdown('<h1 class="main-header">New Price Quote</h1>', unsafe_allow_html=True)
    
    st.markdown("""
    ### Welcome
    
    Designed to speed up sales cycle, this tool provides rapid, on-demand new price quote for HTST products. If you need a quote for a non-HTST product, please continue following current Smartsheet process or contact RGM team directly.
    """)

    # First-load-of-session bootstrap of the Activity_Model folder in Fabric.
    # No-op on every subsequent render in this session.
    bootstrap_written = _ensure_activity_bootstrap()
    if any(bootstrap_written.values()):
        wrote = [n for n, w in bootstrap_written.items() if w]
        st.success(
            f"☁️ First-load bootstrap: uploaded {len(wrote)} file(s) to Fabric "
            f"({_activity_store.get_store_label()})."
        )

    # Auto-update Product_Milk Base Cost on the first day of each month
    # (or the first page-load thereafter that hits this branch). Cheap
    # when no update is needed; surfaces a banner + manual button on
    # any failure.
    _run_pmbc_auto_update()

    # Pull data from Fabric → parquet, with a sidecar ETag signature so we
    # only reprocess when something in Fabric changed.
    df, record_count, file_size_mb, file_mod_time, fabric_err = (
        _ensure_pricing_data_from_fabric()
    )

    if fabric_err:
        st.warning(f"⚠️ {fabric_err}")

    # Display database status if data is loaded
    if df is not None:
        display_database_status(record_count, file_size_mb, file_mod_time)
        st.caption(f"📡 Source: {_activity_store.get_store_label()}")
    else:
        st.warning(
            "⚠️ No pricing database available. Check the Fabric "
            "secrets configuration in `.streamlit/secrets.toml` and reload."
        )

    # Sample database display
    if df is not None:
        st.markdown("---")
        st.markdown("### 📋 Sample Database (Top 50 Records)")
        
        sample_df = df.head(50)
        
        st.dataframe(
            sample_df,
            width='stretch',
            height=400
        )
        
        # Query section
        st.markdown("---")
        st.markdown("### 🔍 Query Database")
        
        # Get available filter options
        try:
            available_plants = sorted([str(x) for x in df['Plant'].unique().tolist()])
            available_volumes = sorted([str(x) for x in df['Sell-to Volume Bracket'].unique().tolist()])
            available_custom_volumes = sorted([str(x) for x in df['Custom Label Bracket'].unique().tolist()])
            available_pallets = sorted([str(x) for x in df['Pallet'].unique().tolist()])
            available_mileages = sorted([str(x) for x in df['Mileage Fee Tier (Mi)'].unique().tolist()])
            available_drops = sorted([str(x) for x in df['Drop Fee Tier (lbs/Drop)'].unique().tolist()])
            
            # Create filter interface
            st.markdown("#### Check Volume Tier (Optional)")
            st.markdown("If you're quoting for an existing customer, you can view their current order list and annual volume [here](https://darigold1com.sharepoint.com/:x:/r/sites/CPPricing2/Shared%20Documents/General/HTST_Activity_Model_Fundamental_Data/Volume%20Tier%20Monitor/Volume%20Tier%20Monitor.xlsx?d=wc8cd9b703b9f44949ef6ece10098326a&csf=1&web=1&e=wnaNjA). Use this information, along with the new volume, to set your filters.")
            
            st.markdown("#### Filter Options")
            
            # Initialize session state for filters
            initialize_filter_session_state({
                'plants': available_plants,
                'volumes': available_volumes,
                'custom_volumes': available_custom_volumes,
                'pallets': available_pallets,
                'mileages': available_mileages,
                'drops': available_drops
            })
            
            # Get sample items for default placeholder
            sample_items = []
            if df is not None and 'Item' in df.columns:
                unique_items = df['Item'].astype(str).unique()
                sample_items = sorted([item for item in unique_items if item and item != 'nan'])[:3]
            
            # Create default placeholder text for item search
            default_item_placeholder = "e.g., " + ";".join(sample_items) if sample_items else "Enter item numbers separated by ';' (e.g., 340776;340013)"
            
            # Get sample descriptions for default placeholder
            sample_descriptions = []
            if df is not None and 'Item Description' in df.columns:
                unique_descriptions = df['Item Description'].astype(str).unique()
                sample_descriptions = [desc for desc in unique_descriptions if desc and desc != 'nan' and len(desc) > 10][:2]
            
            # Create default placeholder text for description search
            default_desc_placeholder = "e.g., " + ";".join([d[:20] + "..." if len(d) > 20 else d for d in sample_descriptions]) if sample_descriptions else "Enter item descriptions separated by ';' (e.g., description1;description2)"
            
            col1, col2, col3 = st.columns(3)
            
            with col1:
                item_search = st.text_input("Item Search", 
                                          value=st.session_state.filter_item_search,
                                          placeholder=default_item_placeholder,
                                          help="Search by item number(s) separated by ';' (e.g., 340776;340013)",
                                          key="item_search_input")
                if item_search != st.session_state.filter_item_search:
                    st.session_state.filter_item_search = item_search
                
                item_description_search = st.text_input("Item Description Search", 
                                                      value=st.session_state.filter_item_description_search,
                                                      placeholder=default_desc_placeholder,
                                                      help="Search by item description(s) separated by ';'. Multiple descriptions can be searched at once.",
                                                      key="item_description_search_input")
                if item_description_search != st.session_state.filter_item_description_search:
                    st.session_state.filter_item_description_search = item_description_search
                
                selected_plants = st.multiselect("Plant", 
                                               available_plants, 
                                               default=st.session_state.filter_plants,
                                               key="plants_select")
                if selected_plants != st.session_state.filter_plants:
                    st.session_state.filter_plants = selected_plants
                
                selected_volumes = st.multiselect("Sell-to Volume (Gal/yr)", 
                                                available_volumes, 
                                                default=st.session_state.filter_volumes,
                                                key="volumes_select")
                if selected_volumes != st.session_state.filter_volumes:
                    st.session_state.filter_volumes = selected_volumes
            
            with col2:
                selected_custom_volumes = st.multiselect("Custom-label Volume (Gal/Yr)", 
                                                       available_custom_volumes, 
                                                       default=st.session_state.filter_custom_volumes,
                                                       key="custom_volumes_select")
                if selected_custom_volumes != st.session_state.filter_custom_volumes:
                    st.session_state.filter_custom_volumes = selected_custom_volumes
                
                selected_pallets = st.multiselect("Pallet", 
                                                available_pallets, 
                                                default=st.session_state.filter_pallets,
                                                key="pallets_select")
                if selected_pallets != st.session_state.filter_pallets:
                    st.session_state.filter_pallets = selected_pallets
                
                selected_mileages = st.multiselect("Mileage", 
                                                 available_mileages, 
                                                 default=st.session_state.filter_mileages,
                                                 key="mileages_select")
                if selected_mileages != st.session_state.filter_mileages:
                    st.session_state.filter_mileages = selected_mileages
            
            with col3:
                selected_drops = st.multiselect("Drop Size (Lb/drop)", 
                                              available_drops, 
                                              default=st.session_state.filter_drops,
                                              key="drops_select")
                if selected_drops != st.session_state.filter_drops:
                    st.session_state.filter_drops = selected_drops
            
            # Query button
            if st.button("🔍 Query Database", type="primary", use_container_width=True):
                st.session_state.query_executed = True
            
            # Apply filters only after query button is pressed
            if hasattr(st.session_state, 'query_executed') and st.session_state.query_executed:
                filtered_df = df.copy()
                
                # Apply item search filter (supports multiple items separated by ";")
                filtered_df = apply_item_search_filter(filtered_df, st.session_state.filter_item_search)
                
                # Apply item description search filter (supports multiple descriptions separated by ";")
                filtered_df = apply_item_description_search_filter(filtered_df, st.session_state.filter_item_description_search)
                
                # Plant filter
                filtered_df = filtered_df[filtered_df['Plant'].astype(str).isin(st.session_state.filter_plants)]
                
                # Volume filters
                filtered_df = filtered_df[filtered_df['Sell-to Volume Bracket'].astype(str).isin(st.session_state.filter_volumes)]
                filtered_df = filtered_df[filtered_df['Custom Label Bracket'].astype(str).isin(st.session_state.filter_custom_volumes)]
                
                # Other filters
                filtered_df = filtered_df[filtered_df['Pallet'].astype(str).isin(st.session_state.filter_pallets)]
                filtered_df = filtered_df[filtered_df['Mileage Fee Tier (Mi)'].astype(str).isin(st.session_state.filter_mileages)]
                filtered_df = filtered_df[filtered_df['Drop Fee Tier (lbs/Drop)'].astype(str).isin(st.session_state.filter_drops)]
                
                # Display results
                st.markdown(f"#### Query Results ({len(filtered_df):,} records)")
                
                if len(filtered_df) > 0:
                    # Format numeric columns for display
                    display_df = format_numeric_columns(filtered_df, decimal_places=4)
                    
                    st.dataframe(
                        display_df,
                        width='stretch',
                        height=400
                    )
                    
                    # Download button
                    csv_data = display_df.to_csv(index=False)
                    st.download_button(
                        label="📥 Download Filtered Data as CSV",
                        data=csv_data,
                        file_name=f"filtered_pricing_data_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                        mime="text/csv"
                    )
                else:
                    st.info("No records match the selected filters.")
                
        except Exception as e:
            st.error(f"Error setting up filters: {e}")
    
    # ── Fabric data-management section ───────────────────────────────────────
    # Replaces the legacy "upload all 9 CSVs to refresh database" panel. The
    # database is now kept in sync with Fabric automatically on every render
    # (see _ensure_pricing_data_from_fabric). This panel exposes:
    #   * the download / upload-to-replace round-trip so RGM can change
    #     Fabric data without leaving the app, and
    #   * the manual PMBC auto-update trigger + status caption.
    st.markdown("---")
    _render_fabric_data_panel()
    _render_pmbc_manual_panel()

    # Finalize Quote Section
    if df is not None:
        st.markdown("---")
        st.markdown("### 📋 Finalize Quote")
        
        st.markdown("""
        - **If you are Sales**: Please collaborate with RGM team to finalize quote by confirming key input such as trade%, first order date, whether shuttling is involved, demand plan.
        
        - **If you are RGM**: Please use this [standard template](https://darigold1com.sharepoint.com/:x:/r/sites/CPPricing2/Shared%20Documents/General/HTST_Activity_Model_Fundamental_Data/Template/HTST_New%20Business%20Bid_Price%20Build%20Template.xlsx?d=w8f7a5e94128743aca27a89f268ab68da&csf=1&web=1&e=Qef248) saved in RGM teams folder to complete the quote.
        """)


