"""
Weekly & Monthly Butter Movers tracker (Market Barometer section).

Renders two side-by-side charts inside one foldable expander:

* **CME weekly price** (left) — Weekly Average $/lb for Cheese (40-LB
  Blocks), Butter (Grade AA), Nonfat Dry Milk (Grade A) and Dry Whey
  (Extra Grade). Sourced from USDA's CME Group Weekly Recap PDF
  (``ams_1602.pdf``) — the only stable, parseable mirror of the CME
  page's weekly-average row (the live CME page is JS-rendered and
  can't be scraped without a headless browser).
  Auto-refreshes Fridays ≥ 09:00 local time, max once per week. Manual
  "Force refresh" button and CSV-upload fallback for any week the
  auto-pull fails.

* **USDA Dairy Products Sales** (right) — Weighted Price for Nonfat
  Dry Milk and Dry Whey, sourced from
  ``dywdairyproductssales.pdf``. Auto-refreshes whenever the upstream
  PDF's HTTP fingerprint changes; appends one row per (Week Ending,
  Product) per refresh. Force-refresh button surfaced next to the
  source link.

Both charts share:
* A time-range slicer (default = last 6 months)
* A "Download chart data" button
* A status caption with the last successful pull timestamp

Public entry point: :func:`render_weekly_and_monthly_butter_tracker`.
"""
from __future__ import annotations

import hashlib
import io
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from data_sources import cme_spot_call_scraper as _cme_pdf
from data_sources import cme_spot_call_store as _cme_store
from data_sources import usda_dairy_products_pdf as _usda_pdf
from data_sources import usda_dairy_products_store as _usda_store


# Default window when the slicer opens: ~6 months of weekly data
# (≈ 26 weeks). Matches the May-2026 spec's "last 6 months" answer.
_DEFAULT_SLICER_WINDOW_DAYS: int = 6 * 30  # ~180 days

# Streamlit session-state keys. Namespaced so neighbouring sections
# never collide on rerun.
_SS_PREFIX:                  str = "_wmbt_"
_SS_CME_LAST_RESULT:         str = _SS_PREFIX + "cme_last_result"
_SS_USDA_LAST_RESULT:        str = _SS_PREFIX + "usda_last_result"
# Sentinels guarding the once-per-session auto-pull. Without them the
# OneLake state-blob read fires on every Streamlit rerun (including
# reruns triggered by widgets in unrelated sections of the page),
# which adds visible latency and can cause flickering. The persistent
# Friday-09:00 gate still lives in the state blob and rules out
# re-pulling across sessions; these sentinels just keep us from
# re-checking that gate dozens of times within ONE session.
_SS_CME_SESSION_CHECKED:     str = _SS_PREFIX + "cme_session_checked"
_SS_USDA_SESSION_CHECKED:    str = _SS_PREFIX + "usda_session_checked"
# Content hash of the last-processed manual upload. Streamlit's
# ``file_uploader`` continues to return the same ``UploadedFile`` on
# every rerun until the user explicitly clears it, so without this
# sentinel an ``st.rerun()`` after a successful upload creates an
# infinite re-processing loop (the second call dedups to zero rows
# but ``result["ok"]`` stays True → the rerun fires again …).
_SS_CME_UPLOAD_PROCESSED:    str = _SS_PREFIX + "cme_upload_processed_sha256"

# Public page URL the operator clicks in the source caption. Distinct
# from the USDA mirror URL used internally (see scraper docstring).
_CME_OPERATOR_URL = _cme_pdf.CME_LIVE_PAGE_URL
_USDA_OPERATOR_URL = _usda_pdf.USDA_DAIRY_PRODUCTS_URL


# ── CME auto-refresh helpers ─────────────────────────────────────────────────

def _should_pull_cme_now(state: Optional[dict], now: Optional[datetime] = None) -> bool:
    """Decide whether to pull the CME recap on this render.

    Rules (strict Friday-09:00 gate):
    * If we've never pulled successfully, AND today is Friday >= 09:00
      → pull.
    * Otherwise we pull only when (a) today is Friday >= 09:00, AND
      (b) the most-recent Friday-09:00 boundary is strictly AFTER our
      last successful pull. This guarantees at most one auto-pull per
      week regardless of how many times the page reruns.
    """
    now = now or datetime.now()
    if not _cme_pdf.is_friday_after_9am_local(now):
        return False
    boundary = _cme_pdf.most_recent_friday_9am_local(now)
    if not state:
        return True
    last_raw = state.get("last_success_at")
    if not last_raw:
        return True
    last = pd.to_datetime(last_raw, errors="coerce")
    if pd.isna(last):
        return True
    # Strip tz info if any (state stores naive iso); compare as naive.
    last_naive = pd.Timestamp(last).to_pydatetime().replace(tzinfo=None)
    return last_naive < boundary


def _pull_cme_recap(*, force: bool = False) -> dict:
    """Fetch + parse + dedup-append the CME Weekly Recap.

    Returns a dict the UI can render via ``st.caption`` / ``st.error``.
    Catches every failure — never raises — so a transient USDA outage
    can't crash the page.
    """
    out: dict = {
        "ok":         False,
        "fetched":    False,
        "appended":   0,
        "skipped":    0,
        "message":    "",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        body, fp = _cme_pdf.fetch_recap_pdf_bytes()
        out["fetched"] = True
        recap = _cme_pdf.parse_weekly_recap(body)
        rows = [
            {
                _cme_store.COL_WEEK_ENDING:    recap.week_ending,
                _cme_store.COL_PRODUCT:        product,
                _cme_store.COL_WEEKLY_AVERAGE: value,
            }
            for product, value in recap.weekly_averages.items()
        ]
        append = _cme_store.dedup_append_rows(
            rows, source="manual-refresh" if force else "auto-update",
        )
        out["ok"] = True
        out["appended"] = append.inserted
        out["skipped"]  = append.skipped
        out["message"] = (
            f"CME Weekly Recap for {recap.week_ending:%Y-%m-%d}: "
            f"{append.inserted} new row(s), {append.skipped} skipped "
            f"(already on file). Missing products: "
            f"{list(recap.missing_products) or 'none'}."
        )
        # Persist fingerprint last_success_at so the strict gate sees it.
        try:
            _cme_store.upsert_pdf_state(
                _cme_pdf.CME_WEEKLY_RECAP_URL,
                etag=fp.etag,
                last_modified=fp.last_modified,
                content_sha256=fp.content_sha256,
                checked_at=datetime.now(),
                last_success_at=datetime.now(),
            )
        except _cme_store.CMESpotCallStoreError as exc:
            out["message"] += f" (warning: state-blob update failed: {exc})"
    except Exception as exc:  # noqa: BLE001 — top-level UI guard
        out["message"] = (
            f"CME Weekly Recap auto-pull failed: {exc}. "
            f"The chart still shows the data already in OneLake. "
            f"Use the upload widget below to paste in this week's CSV if needed."
        )
    return out


def _absorb_cme_upload(uploaded_bytes: bytes) -> dict:
    """Ingest an operator-supplied CSV with the same long-format schema.

    The CSV must have at minimum the columns ``Week Ending`` and
    ``Product`` plus a numeric ``Weekly Average``. Extra columns are
    ignored. Same dedup semantics as the auto-pull.
    """
    out: dict = {"ok": False, "appended": 0, "skipped": 0, "message": ""}
    try:
        df = pd.read_csv(io.BytesIO(uploaded_bytes))
    except Exception as exc:  # noqa: BLE001
        out["message"] = f"Could not parse the uploaded CSV: {exc}"
        return out

    required = {_cme_store.COL_WEEK_ENDING, _cme_store.COL_PRODUCT,
                _cme_store.COL_WEEKLY_AVERAGE}
    missing = required - set(df.columns)
    if missing:
        out["message"] = (
            f"The uploaded CSV is missing required column(s): "
            f"{sorted(missing)!r}. Expected at minimum "
            f"{sorted(required)!r}."
        )
        return out

    rows = df[list(required)].to_dict(orient="records")
    try:
        result = _cme_store.dedup_append_rows(rows, source="manual-upload")
    except _cme_store.CMESpotCallStoreError as exc:
        out["message"] = f"Upload accepted but write failed: {exc}"
        return out
    out["ok"] = True
    out["appended"] = result.inserted
    out["skipped"]  = result.skipped
    out["message"]  = (
        f"Uploaded CME CSV merged. {result.inserted} new row(s), "
        f"{result.skipped} skipped (already on file). "
        f"Total rows now: {result.total_after}."
    )
    return out


# ── USDA auto-refresh helpers ────────────────────────────────────────────────

def _should_pull_usda_now(state: Optional[dict], now: Optional[datetime] = None) -> bool:
    """Pull the USDA PDF when we've never checked, OR our last check
    is older than 1 hour (TTL guard — the PDF refreshes weekly on
    Tuesday so polling more than once per hour is wasteful).
    """
    now = now or datetime.now()
    if not state:
        return True
    last_raw = state.get("checked_at")
    if not last_raw:
        return True
    last = pd.to_datetime(last_raw, errors="coerce")
    if pd.isna(last):
        return True
    last_naive = pd.Timestamp(last).to_pydatetime().replace(tzinfo=None)
    return (now - last_naive) >= timedelta(hours=1)


def _pull_usda_recap(*, force: bool = False) -> dict:
    """Fetch + parse + dedup-append the USDA Dairy Products PDF.

    Only the products the UI surfaces today (Dry Whey, Nonfat Dry Milk)
    are written to OneLake. The parser extracts all four products as
    a future-proofing measure, but we filter here so the OneLake CSV
    stays narrow.
    """
    out: dict = {
        "ok":         False,
        "fetched":    False,
        "appended":   0,
        "skipped":    0,
        "revised":    0,
        "message":    "",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        body, fp = _usda_pdf.fetch_pdf_bytes()
        out["fetched"] = True
        recap = _usda_pdf.parse_dairy_products_sales(body)
        rows: list[dict] = []
        for product in _usda_pdf.USER_FACING_PRODUCTS:
            for row in recap.rows_by_product.get(product, ()):
                rows.append({
                    _usda_store.COL_WEEK_ENDING:    row.week_ending,
                    _usda_store.COL_PRODUCT:        product,
                    _usda_store.COL_WEIGHTED_PRICE: row.weighted_price,
                    _usda_store.COL_REVISED:        row.revised,
                })
        result = _usda_store.dedup_append_rows(
            rows, source="manual-refresh" if force else "auto-update",
        )
        out["ok"]       = True
        out["appended"] = result.inserted
        out["skipped"]  = result.skipped
        out["revised"]  = result.revised
        out["message"]  = (
            f"USDA Dairy Products Sales for "
            f"{recap.date_range_start:%Y-%m-%d} – {recap.date_range_end:%Y-%m-%d}: "
            f"{result.inserted} new row(s), {result.revised} updated (revised), "
            f"{result.skipped} unchanged. Missing products: "
            f"{list(recap.missing_products) or 'none'}."
        )
        try:
            _usda_store.upsert_pdf_state(
                _usda_pdf.USDA_DAIRY_PRODUCTS_URL,
                etag=fp.etag,
                last_modified=fp.last_modified,
                content_sha256=fp.content_sha256,
                checked_at=datetime.now(),
                last_success_at=datetime.now(),
            )
        except _usda_store.USDADairyProductsStoreError as exc:
            out["message"] += f" (warning: state-blob update failed: {exc})"
    except Exception as exc:  # noqa: BLE001
        out["message"] = (
            f"USDA Dairy Products Sales auto-pull failed: {exc}. "
            f"The chart still shows the data already in OneLake."
        )
    return out


# ── Chart builders ───────────────────────────────────────────────────────────

def _build_chart(
    df: pd.DataFrame,
    *,
    value_col: str,
    title: str,
    yaxis_title: str,
    product_order: tuple[str, ...],
) -> go.Figure:
    """Generic plotly line chart — one trace per product, oldest week
    on the left. ``product_order`` controls the legend / colour order
    so it stays stable as more products get added later.
    """
    fig = go.Figure()
    if df.empty:
        fig.update_layout(
            title=title,
            xaxis_title="Week Ending",
            yaxis_title=yaxis_title,
            annotations=[{
                "text": "No data yet — refresh to populate.",
                "showarrow": False,
                "xref": "paper", "yref": "paper",
                "x": 0.5, "y": 0.5, "font": {"size": 14, "color": "#888"},
            }],
            height=380,
            margin={"l": 50, "r": 20, "t": 50, "b": 50},
        )
        return fig
    # Plot every product the data carries in the canonical order.
    products_present = [p for p in product_order if p in set(df["Product"])]
    extras = sorted(set(df["Product"]) - set(products_present))
    for product in products_present + extras:
        sub = df[df["Product"] == product].sort_values("Week Ending")
        if sub.empty:
            continue
        fig.add_trace(go.Scatter(
            x=sub["Week Ending"],
            y=sub[value_col],
            mode="lines+markers",
            name=product,
            hovertemplate=(
                f"<b>{product}</b><br>"
                "Week ending: %{x|%Y-%m-%d}<br>"
                f"{value_col}: $%{{y:.4f}}/lb<extra></extra>"
            ),
        ))
    fig.update_layout(
        title=title,
        xaxis_title="Week Ending",
        yaxis_title=yaxis_title,
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": -0.25, "xanchor": "center", "x": 0.5},
        height=420,
        margin={"l": 50, "r": 20, "t": 50, "b": 80},
    )
    return fig


# ── Slicer + filter helper ───────────────────────────────────────────────────

def _slicer_window(
    df: pd.DataFrame, *,
    key_prefix: str,
    label: str = "Time range",
) -> tuple[pd.Timestamp, pd.Timestamp, pd.DataFrame]:
    """Render a date-range slicer pinned to the data's actual min/max.

    Returns ``(start, end, df_filtered)``. Defaults to the most-recent
    :data:`_DEFAULT_SLICER_WINDOW_DAYS` of data; the user can drag
    earlier with the slider.
    """
    if df.empty:
        return pd.Timestamp.min, pd.Timestamp.max, df
    dmin = pd.Timestamp(df["Week Ending"].min()).normalize()
    dmax = pd.Timestamp(df["Week Ending"].max()).normalize()
    default_start = max(dmin, dmax - pd.Timedelta(days=_DEFAULT_SLICER_WINDOW_DAYS))
    selection = st.date_input(
        label,
        value=(default_start.date(), dmax.date()),
        min_value=dmin.date(),
        max_value=dmax.date(),
        key=f"{key_prefix}_slicer",
    )
    # ``st.date_input`` returns a tuple iff value was a tuple. Guard for
    # the single-date click case (Streamlit returns a single date until
    # the user picks the end).
    if isinstance(selection, tuple) and len(selection) == 2:
        start, end = selection
    else:
        start = default_start.date()
        end = dmax.date()
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)
    mask = (df["Week Ending"] >= start_ts) & (df["Week Ending"] <= end_ts)
    return start_ts, end_ts, df.loc[mask].copy()


# ── Per-column renderers ─────────────────────────────────────────────────────

def _render_cme_column() -> None:
    """Render the CME Weekly Price chart, slicer, controls."""
    st.markdown("### 🐄 CME Weekly Price")
    st.caption(
        f"Source: [CME Spot Call Data]({_CME_OPERATOR_URL})  •  "
        f"Weekly mirror (USDA AMS): [`ams_1602.pdf`]({_cme_pdf.CME_WEEKLY_RECAP_URL})  •  "
        f"Storage: `{_cme_store.get_table_blob_path()}`"
    )

    # Auto-pull gate (Fridays ≥ 09:00, max once per week).
    #
    # We read the OneLake state blob AT MOST once per Streamlit session
    # — gated by ``_SS_CME_SESSION_CHECKED``. Cross-session protection
    # (the actual "have we pulled since the most-recent Friday-9am"
    # decision) still lives in the state blob, but within one session
    # there is no value in repeatedly reading it: the result of
    # ``_should_pull_cme_now`` cannot change without our own code
    # mutating the blob, which we do exactly once per session.
    last_state: Optional[dict] = None
    if not st.session_state.get(_SS_CME_SESSION_CHECKED):
        try:
            last_state = _cme_store.get_pdf_state(_cme_pdf.CME_WEEKLY_RECAP_URL)
        except _cme_store.CMESpotCallStoreError as exc:
            st.warning(f"⚠️ Could not read CME state blob: {exc}")
        if _should_pull_cme_now(last_state):
            st.session_state[_SS_CME_LAST_RESULT] = _pull_cme_recap()
        st.session_state[_SS_CME_SESSION_CHECKED] = True

    # Action row: manual refresh + last-status caption.
    col_btn, col_status = st.columns([1, 3])
    with col_btn:
        if st.button(
            "🔄 CME refresh",
            key=f"{_SS_PREFIX}cme_refresh",
            help="Force-pull the latest USDA AMS Weekly Recap PDF now, "
                 "bypassing the Friday-09:00 weekly gate.",
        ):
            # The button click itself already triggers exactly one
            # Streamlit rerun for the result to surface; calling
            # ``st.rerun()`` here would cause a second redundant rerun
            # (no infinite loop because ``st.button`` returns True for
            # exactly one rerun, but the redundant rerun shows up as a
            # visible flicker, which we don't want).
            st.session_state[_SS_CME_LAST_RESULT] = _pull_cme_recap(force=True)
    with col_status:
        result = st.session_state.get(_SS_CME_LAST_RESULT)
        if result:
            if result.get("ok"):
                st.caption(f"✅ {result['message']}")
            else:
                st.caption(f"⚠️ {result['message']}")
        else:
            # Lazy-fetch last_success_at only when we don't already have
            # an in-session result to display — keeps the steady-state
            # render off the OneLake network path entirely.
            persisted_pull = (last_state or {}).get("last_success_at")
            if persisted_pull:
                st.caption(f"🕓 Last successful pull: {persisted_pull}")
            else:
                st.caption(
                    "ℹ️ No data ingested yet — click '🔄 CME refresh' or wait "
                    "for the next Friday-09:00 auto-pull."
                )

    # Load + slice + chart.
    try:
        df = _cme_store.read_df()
    except _cme_store.CMESpotCallStoreError as exc:
        st.error(
            f"Could not read CME store: {exc}\n\n"
            "If Microsoft Fabric is not signed in, visit **Home & Fabric Sign-in** "
            "in the sidebar to sign in, then return here."
        )
        return

    _start, _end, df_filtered = _slicer_window(df, key_prefix=f"{_SS_PREFIX}cme")
    fig = _build_chart(
        df_filtered,
        value_col=_cme_store.COL_WEEKLY_AVERAGE,
        title="CME Weekly Average ($/lb)",
        yaxis_title="Weekly Average ($/lb)",
        product_order=_cme_pdf.PRODUCT_ORDER,
    )
    st.plotly_chart(fig, use_container_width=True, key=f"{_SS_PREFIX}cme_chart")

    # Download button — exports the SLICED data so an operator can
    # forward "just May 2026" to a colleague without sharing the full
    # history.
    if not df_filtered.empty:
        csv = df_filtered[[*_cme_store.ALL_COLUMNS]].to_csv(index=False)
        st.download_button(
            "📥 Download chart data (CSV)",
            data=csv,
            file_name=(
                f"cme_spot_call_weekly_"
                f"{datetime.now():%Y%m%d_%H%M%S}.csv"
            ),
            mime="text/csv",
            key=f"{_SS_PREFIX}cme_download",
        )

    # Upload fallback — surfaces only as an expander so it doesn't
    # clutter the steady-state UI. Operators reach for it when the
    # auto-pull errors mid-publishing window.
    #
    # IMPORTANT: ``st.file_uploader`` continues to return the same
    # ``UploadedFile`` on every subsequent Streamlit rerun, even though
    # the user uploaded it only once. Without the SHA-256 sentinel
    # below, calling ``st.rerun()`` after a successful absorb creates
    # an infinite re-processing loop. We instead remember the content
    # hash of the last-processed upload in session state and skip
    # absorption when the same hash comes back around. Result message
    # is parked in session state so it persists across reruns until
    # the next genuine upload.
    with st.expander("📤 Upload CME data manually (fallback)", expanded=False):
        st.caption(
            "Use this when the auto-pull fails (CME / USDA outage). The CSV "
            "must have columns: `Week Ending`, `Product`, `Weekly Average`. "
            "Duplicates (same Week Ending + Product) are silently skipped."
        )
        uploaded = st.file_uploader(
            "Upload CME CSV",
            type=["csv"],
            key=f"{_SS_PREFIX}cme_upload",
            label_visibility="collapsed",
        )
        upload_result_key = f"{_SS_PREFIX}cme_upload_result"
        if uploaded is not None:
            content = uploaded.getvalue()
            content_sig = hashlib.sha256(content).hexdigest()
            last_sig = st.session_state.get(_SS_CME_UPLOAD_PROCESSED)
            if content_sig != last_sig:
                # New (or replaced) file — absorb exactly once.
                st.session_state[_SS_CME_UPLOAD_PROCESSED] = content_sig
                st.session_state[upload_result_key] = _absorb_cme_upload(content)
                # NO ``st.rerun()`` here: the cache-invalidation inside
                # ``dedup_append_rows`` means the chart read further up
                # the file will already see the fresh data on the NEXT
                # render, which Streamlit triggers naturally as part of
                # the upload event itself.
        else:
            # User cleared the uploader (clicked the × on the widget)
            # — reset the sentinels so re-uploading the SAME file later
            # in the session is treated as a fresh upload, not a dedup
            # no-op. Also drop the stale result banner so it doesn't
            # confuse the next visitor to the expander.
            st.session_state.pop(upload_result_key, None)
            st.session_state.pop(_SS_CME_UPLOAD_PROCESSED, None)

        last_upload_result = st.session_state.get(upload_result_key)
        if last_upload_result:
            if last_upload_result["ok"]:
                st.success(f"✅ {last_upload_result['message']}")
            else:
                st.error(f"⚠️ {last_upload_result['message']}")


def _render_usda_column() -> None:
    """Render the USDA Dairy Products Sales chart, slicer, controls."""
    st.markdown("### 🥛 USDA Dairy Products Sales")
    st.caption(
        f"Source: [USDA NDPSR PDF]({_USDA_OPERATOR_URL})  •  "
        f"Storage: `{_usda_store.get_table_blob_path()}`"
    )

    # Auto-pull gate (1 h TTL on the HEAD-like check).
    #
    # As with the CME column, the OneLake state-blob read is gated by
    # a once-per-Streamlit-session sentinel so unrelated reruns (slider
    # drags, neighbouring uploads, etc.) don't hit the network on this
    # critical path. Cross-session protection still lives in the blob.
    last_state: Optional[dict] = None
    if not st.session_state.get(_SS_USDA_SESSION_CHECKED):
        try:
            last_state = _usda_store.get_pdf_state(_usda_pdf.USDA_DAIRY_PRODUCTS_URL)
        except _usda_store.USDADairyProductsStoreError as exc:
            st.warning(f"⚠️ Could not read USDA state blob: {exc}")
        if _should_pull_usda_now(last_state):
            st.session_state[_SS_USDA_LAST_RESULT] = _pull_usda_recap()
        st.session_state[_SS_USDA_SESSION_CHECKED] = True

    col_btn, col_status = st.columns([1, 3])
    with col_btn:
        if st.button(
            "🔄 USDA refresh",
            key=f"{_SS_PREFIX}usda_refresh",
            help="Force-pull the latest USDA Dairy Products Sales PDF now.",
        ):
            # No explicit ``st.rerun()`` — the button click is itself
            # one rerun, and we've already written the result + cache-
            # invalidated the table; the chart re-reads fresh data in
            # the SAME render below.
            st.session_state[_SS_USDA_LAST_RESULT] = _pull_usda_recap(force=True)
    with col_status:
        result = st.session_state.get(_SS_USDA_LAST_RESULT)
        if result:
            if result.get("ok"):
                st.caption(f"✅ {result['message']}")
            else:
                st.caption(f"⚠️ {result['message']}")
        else:
            persisted_pull = (last_state or {}).get("last_success_at")
            if persisted_pull:
                st.caption(f"🕓 Last successful pull: {persisted_pull}")
            else:
                st.caption(
                    "ℹ️ No data ingested yet — click '🔄 USDA refresh' to populate."
                )

    try:
        df = _usda_store.read_df()
    except _usda_store.USDADairyProductsStoreError as exc:
        st.error(
            f"Could not read USDA store: {exc}\n\n"
            "If Microsoft Fabric is not signed in, visit **Home & Fabric Sign-in** "
            "in the sidebar to sign in, then return here."
        )
        return

    _start, _end, df_filtered = _slicer_window(df, key_prefix=f"{_SS_PREFIX}usda")
    fig = _build_chart(
        df_filtered,
        value_col=_usda_store.COL_WEIGHTED_PRICE,
        title="USDA Weighted Price ($/lb)",
        yaxis_title="Weighted Price ($/lb)",
        product_order=_usda_pdf.USER_FACING_PRODUCTS,
    )
    st.plotly_chart(fig, use_container_width=True, key=f"{_SS_PREFIX}usda_chart")

    if not df_filtered.empty:
        csv = df_filtered[[*_usda_store.ALL_COLUMNS]].to_csv(index=False)
        st.download_button(
            "📥 Download chart data (CSV)",
            data=csv,
            file_name=(
                f"usda_dairy_products_weighted_"
                f"{datetime.now():%Y%m%d_%H%M%S}.csv"
            ),
            mime="text/csv",
            key=f"{_SS_PREFIX}usda_download",
        )


# ── Public entry point ───────────────────────────────────────────────────────

def render_weekly_and_monthly_butter_tracker() -> None:
    """Render the Weekly & Monthly Butter Movers expander.

    Called from :func:`pages.market_barometer_view.render` between the
    instructions divider and the Monthly Movers expander. Wrapped in
    an ``st.expander`` so it can be collapsed when the operator is
    focused elsewhere.
    """
    with st.expander("🧈 Weekly & Monthly Butter Movers", expanded=False):
        # Two-column layout. ``st.columns`` keeps the columns equal
        # width on desktop and stacks them vertically on narrow
        # viewports — no extra responsive code needed.
        col_cme, col_usda = st.columns(2, gap="large")
        with col_cme:
            _render_cme_column()
        with col_usda:
            _render_usda_column()
