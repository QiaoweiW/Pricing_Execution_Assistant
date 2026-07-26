"""Trade-spend / promotion connector — ``dbo.dp_fy_<FY>_actual_trade_spend``.

Powers the **promo overlay** on the Velocity Analysis chart: shaded background
bands that mark the weeks a SKU was on a consumer promotion, so a demand planner
can read a velocity spike against the promo that (likely) caused it.

Source
------
Three Delta tables in the shared IBP lakehouse — one per fiscal year
(``dp_fy_2025_actual_trade_spend`` … ``2027``) — with an identical 33-column
schema.  The columns that matter here::

    item_number          SKU (int)  → PDH ``Item No`` → shipment attributes
    corporate_group      customer roll-up (matches dp_dimcustomernames)
    start_event/end_event  the on-shelf CONSUMER window (what we shade)
    start_ship/end_ship    the retailer order/ship window (leads the event)
    promotion_tactics    Ad Feature / TPR Only / EDLP / Corp Program / …
    promo_status         Cancelled / Claims Applied / Closed / …
    promotion_desc       free-text label
    promo_spend_actuals, promo_actual_ship_volume   magnitudes for the hover

Key modelling decisions (see the page critique)
-----------------------------------------------
* **Only discrete consumer promos are shaded.**  ~66% of rows are year-long
  ``Corp Program`` / fee agreements — shading those would flood the chart.  The
  default :data:`CONSUMER_TACTICS` are the ~4-week shelf events that actually
  move velocity.
* **Cancelled promos are dropped** (they never executed).
* **Day-level events snap to the Monday week grid** and same-tactic overlapping /
  adjacent events **merge into one band**, so N SKUs on one Kroger feature read
  as a single window (with N in the hover), not N stacked slivers.

All transforms (:func:`build_promo_windows`) are Streamlit-/IO-free and tested.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import streamlit as st

from data_sources.fabric_auth import (
    FabricAuthError,
    acquire_storage_token,
    bind_storage_token,
    duckdb_lock,
    get_duckdb_connection,
    prepare_duckdb_tls,
)
from data_sources.fabric_tls import tls_error_hint as _tls_hint


# ── Source identity (same lakehouse as IBP; dbo schema) ──────────────────────
_WORKSPACE_GUID = "bb11c51d-03c8-4f1b-938c-e20657a8f31d"
_LAKEHOUSE_GUID = "a01f513d-eee7-41eb-8c15-670bc40e7fc8"
_SCHEMA = "dbo"
_TABLES: tuple[str, ...] = (
    "dp_fy_2025_actual_trade_spend",
    "dp_fy_2026_actual_trade_spend",
    "dp_fy_2027_actual_trade_spend",
)
_LOCAL_TZ = "America/Los_Angeles"
_CACHE_TTL_SECONDS = 15 * 60

# Columns we read (a narrow slice of the 33) — logical names == source names.
COL_ITEM = "item_number"
COL_CORP = "corporate_group"
COL_START_EVENT = "start_event"
COL_END_EVENT = "end_event"
COL_START_SHIP = "start_ship"
COL_END_SHIP = "end_ship"
COL_TACTIC = "promotion_tactics"
COL_STATUS = "promo_status"
COL_DESC = "promotion_desc"
COL_SPEND = "promo_spend_actuals"
COL_VOLUME = "promo_actual_ship_volume"
COL_SALE_PRICE = "sale_price"
COL_SHELF_PRICE = "shelf_price"
_SELECT_COLS: tuple[str, ...] = (
    COL_ITEM, COL_CORP, COL_START_EVENT, COL_END_EVENT, COL_START_SHIP,
    COL_END_SHIP, COL_TACTIC, COL_STATUS, COL_DESC, COL_SPEND, COL_VOLUME,
    COL_SALE_PRICE, COL_SHELF_PRICE,
)

# Normalised helper columns added on read.
ITEM_KEY = "_item_key"     # digits-only, zero-stripped (join key to PDH Item No)
START_DATE = "_start"      # naive local date (tz-stripped, normalised)
END_DATE = "_end"

# Discrete, consumer-facing shelf tactics (the promo windows that move velocity).
CONSUMER_TACTICS: tuple[str, ...] = (
    "Ad Feature", "Ad Feature and Display", "Display", "TPR Only", "EDLP",
)
# Statuses that never executed → never shaded.
EXCLUDE_STATUSES: tuple[str, ...] = ("Cancelled",)

# One colour per tactic (warm = merchandising/feature, cool = price).
TACTIC_COLORS: dict[str, str] = {
    "Ad Feature": "#f4b400",             # gold (matches the screenshot)
    "Ad Feature and Display": "#e8710a",  # orange
    "Display": "#c98a00",                # amber
    "TPR Only": "#1f77b4",               # blue (price)
    "EDLP": "#2ca02c",                   # green (everyday low)
}
_DEFAULT_COLOR = "#8e8e8e"


class TradeSpendError(RuntimeError):
    """Raised on any failure to read the trade-spend tables."""


# ── Normalisation helpers (pure) ─────────────────────────────────────────────

def norm_item(value) -> str:
    """Digits-only, zero-stripped item key (``'0340021'`` → ``'340021'``)."""
    digits = re.sub(r"\D", "", str(value))
    return digits.lstrip("0") or ("0" if digits else "")


def _to_local_date(series: pd.Series) -> pd.Series:
    """tz-aware timestamp → naive *local* date (midnight), robust to mixed tz."""
    dt = pd.to_datetime(series, errors="coerce", utc=True)
    return dt.dt.tz_convert(_LOCAL_TZ).dt.tz_localize(None).dt.normalize()


def _monday(d: pd.Timestamp) -> pd.Timestamp:
    """Monday of the week containing *d* (normalised)."""
    return (d - pd.to_timedelta(d.weekday(), unit="D")).normalize()


# ── I/O: token-bound DuckDB read of the FY trade-spend tables ────────────────

def _table_uri(table: str) -> str:
    return (f"abfss://{_WORKSPACE_GUID}@onelake.dfs.fabric.microsoft.com/"
            f"{_LAKEHOUSE_GUID}/Tables/{_SCHEMA}/{table}")


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch(_cache_token: str) -> pd.DataFrame:
    """Union of the FY trade-spend tables (narrow column slice), normalised.

    A missing / unreadable single year is skipped (best-effort) — the overlay
    should still work off the years that are present.  Raises only when NONE
    of the tables can be read.
    """
    cols_sql = ", ".join(f'"{c}"' for c in _SELECT_COLS)
    try:
        token = acquire_storage_token()
    except FabricAuthError as exc:
        raise TradeSpendError(str(exc)) from exc
    ssl_verify = prepare_duckdb_tls()
    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    con = get_duckdb_connection()
    with duckdb_lock():
        bind_storage_token(con, token, ssl_verify=ssl_verify)
        for table in _TABLES:
            try:
                df = con.execute(
                    f"SELECT {cols_sql} FROM delta_scan('{_table_uri(table)}')"
                ).df()
                frames.append(df)
            except Exception as exc:  # noqa: BLE001 — skip a missing FY
                errors.append(f"{table}: {exc}")
    if not frames:
        raise TradeSpendError(
            "Could not read any trade-spend table.  "
            + " | ".join(errors) + _tls_hint(errors[0] if errors else ""))
    out = pd.concat(frames, ignore_index=True)
    out[ITEM_KEY] = out[COL_ITEM].map(norm_item)
    out[START_DATE] = _to_local_date(out[COL_START_EVENT])
    out[END_DATE] = _to_local_date(out[COL_END_EVENT])
    out[COL_CORP] = out[COL_CORP].astype(str).str.strip()
    out[COL_TACTIC] = out[COL_TACTIC].astype(str).str.strip()
    out[COL_STATUS] = out[COL_STATUS].astype(str).str.strip()
    return out


def fetch_trade_spend_df(*, force_refresh: bool = False) -> pd.DataFrame:
    """Return the unioned, normalised trade-spend frame.

    Raises :class:`TradeSpendError` only if no year could be read.
    """
    if force_refresh:
        _cached_fetch.clear()
    return _cached_fetch("default")


def distinct_tactics(df: pd.DataFrame) -> list[str]:
    """Tactics present, consumer-facing ones first (for the multiselect)."""
    if df is None or df.empty or COL_TACTIC not in df.columns:
        return []
    present = {t for t in df[COL_TACTIC].dropna().astype(str).str.strip() if t}
    ordered = [t for t in CONSUMER_TACTICS if t in present]
    return ordered + sorted(present - set(ordered))


# ── Promo-window builder (pure) ──────────────────────────────────────────────

@dataclass(frozen=True)
class PromoWindows:
    """Result of :func:`build_promo_windows` — **per-week promo intensity**.

    ``bands`` has one row per week that has promo activity, with the columns
    ``week_start, x0, x1`` (half-week-padded ``vrect`` edges), ``tactic`` /
    ``color`` (the *dominant* tactic that week, for the fill), ``weight`` (that
    tactic's SKU count → opacity), ``total_skus`` / ``spend`` / ``volume``
    (across ALL selected tactics that week), ``corps``, ``tactics_active``
    (``"Ad Feature ·3, TPR ·1"``) and ``descs`` for the hover.  Per-week (not
    merged) so a busy aggregate reads as a graded heat band — build → peak →
    taper — instead of collapsing into one meaningless mega-band.  ``tactics``
    lists the tactics present (legend).  ``max_weight`` scales opacity.
    """
    bands: pd.DataFrame
    tactics: tuple[str, ...]
    max_weight: float = 0.0


_BAND_COLUMNS = ["week_start", "x0", "x1", "tactic", "color", "weight",
                 "total_skus", "spend", "volume", "corps", "tactics_active", "descs"]


def _explode_weeks(start: pd.Timestamp, end: pd.Timestamp,
                   lo: pd.Timestamp, hi: pd.Timestamp) -> list[pd.Timestamp]:
    """Monday weeks an event covers, clipped to the visible ``[lo, hi]`` window."""
    s = _monday(max(start, lo))
    e = _monday(min(end, hi))
    if e < s:
        return []
    return list(pd.date_range(s, e, freq="W-MON"))


def build_promo_windows(
    df: pd.DataFrame, *,
    item_keys: Optional[set] = None,
    corporate_groups: Optional[list[str]] = None,
    tactics: Optional[list[str]] = None,
    week_window: Optional[tuple[date, date]] = None,
) -> PromoWindows:
    """Filter trade spend to a scope and return **per-week** promo-intensity bands.

    * ``item_keys`` — normalised item keys (from the filtered shipment frame via
      PDH).  ``None`` → all items.
    * ``corporate_groups`` — corp-group scope (matches the velocity filter).
      ``None`` / empty → all corps.
    * ``tactics`` — tactic whitelist; ``None`` → :data:`CONSUMER_TACTICS`.
      Cancelled statuses are always excluded.
    * ``week_window`` — inclusive ``(lo, hi)`` clip on the weekly grid.

    Each event is exploded to the weeks it covers; per (week, tactic) we count
    distinct SKUs and spread spend/volume evenly across the event's weeks (so a
    weekly total isn't inflated by long events).  The dominant tactic (most SKUs)
    colours the week; opacity tracks its SKU count.  Empty / no-match → an empty
    (well-shaped) result.
    """
    empty = PromoWindows(pd.DataFrame(columns=_BAND_COLUMNS), (), 0.0)
    if df is None or df.empty or COL_START_EVENT not in df.columns:
        return empty

    use_tactics = list(tactics) if tactics else list(CONSUMER_TACTICS)
    mask = df[COL_TACTIC].isin(use_tactics)
    mask &= ~df[COL_STATUS].isin(EXCLUDE_STATUSES)
    mask &= df[START_DATE].notna() & df[END_DATE].notna()
    if item_keys is not None:
        mask &= df[ITEM_KEY].isin(set(item_keys))
    if corporate_groups:
        mask &= df[COL_CORP].isin([str(c).strip() for c in corporate_groups])
    if week_window is not None:
        lo, hi = pd.Timestamp(week_window[0]), pd.Timestamp(week_window[1])
        mask &= (df[START_DATE] <= hi) & (df[END_DATE] >= lo)
    else:
        lo, hi = df[START_DATE].min(), df[END_DATE].max()
    work = df.loc[mask]
    if work.empty:
        return empty
    # Clean, non-underscore column names so itertuples doesn't mangle them.
    it = pd.DataFrame({
        "start": work[START_DATE].to_numpy(), "end": work[END_DATE].to_numpy(),
        "tactic": work[COL_TACTIC].to_numpy(), "item": work[ITEM_KEY].to_numpy(),
        "corp": work[COL_CORP].to_numpy(),
        "desc": work[COL_DESC].astype(str).str.strip().to_numpy(),
        "spend": pd.to_numeric(work[COL_SPEND], errors="coerce").fillna(0.0).to_numpy(),
        "volume": pd.to_numeric(work[COL_VOLUME], errors="coerce").fillna(0.0).to_numpy(),
    })

    # Explode each event to its (clipped) weeks; spread spend/volume evenly.
    recs: list[dict] = []
    for r in it.itertuples(index=False):
        weeks = _explode_weeks(pd.Timestamp(r.start), pd.Timestamp(r.end), lo, hi)
        if not weeks:
            continue
        n = len(weeks)
        for wk in weeks:
            recs.append({
                "week": wk, "tactic": r.tactic, "item": r.item, "corp": r.corp,
                "desc": r.desc, "spend": r.spend / n, "volume": r.volume / n,
            })
    if not recs:
        return empty
    long = pd.DataFrame(recs)

    # Per (week, tactic): distinct SKUs + spread spend/volume.
    wt = (long.groupby(["week", "tactic"])
              .agg(n_skus=("item", "nunique"), spend=("spend", "sum"),
                   volume=("volume", "sum"))
              .reset_index())

    rows: list[dict] = []
    for wk, g in wt.groupby("week"):
        wk_long = long[long["week"] == wk]
        dom = g.sort_values(["n_skus", "spend"], ascending=False).iloc[0]
        active = ", ".join(f"{t} ·{int(k)}" for t, k in
                           zip(g.sort_values("n_skus", ascending=False)["tactic"],
                               g.sort_values("n_skus", ascending=False)["n_skus"]))
        corps = sorted({c for c in wk_long["corp"] if c})
        descs = sorted({d for d in wk_long["desc"] if d})
        rows.append({
            "week_start": wk,
            "x0": wk - timedelta(days=3, hours=12),
            "x1": wk + timedelta(days=3, hours=12),
            "tactic": dom["tactic"],
            "color": TACTIC_COLORS.get(dom["tactic"], _DEFAULT_COLOR),
            "weight": float(dom["n_skus"]),
            "total_skus": int(wk_long["item"].nunique()),
            "spend": float(g["spend"].sum()),
            "volume": float(g["volume"].sum()),
            "corps": ", ".join(corps[:6]) + ("…" if len(corps) > 6 else ""),
            "tactics_active": active,
            "descs": " · ".join(descs[:2]) + ("…" if len(descs) > 2 else ""),
        })
    bands = pd.DataFrame(rows, columns=_BAND_COLUMNS).sort_values(
        "week_start").reset_index(drop=True)
    present = tuple(t for t in distinct_tactics(work) if t in set(bands["tactic"]))
    return PromoWindows(bands=bands, tactics=present,
                        max_weight=float(bands["weight"].max()) if not bands.empty else 0.0)


__all__ = [
    "TradeSpendError", "PromoWindows",
    "CONSUMER_TACTICS", "EXCLUDE_STATUSES", "TACTIC_COLORS",
    "COL_ITEM", "COL_CORP", "COL_TACTIC", "COL_STATUS", "ITEM_KEY",
    "START_DATE", "END_DATE",
    "norm_item", "fetch_trade_spend_df", "distinct_tactics", "build_promo_windows",
]
