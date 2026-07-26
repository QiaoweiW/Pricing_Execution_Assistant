"""
IRI / Circana weekly **consumer sell-through** velocity, indexed to 100.

Source: ``Files/RO Tracking/IRI/IRI_Weekly_Units_*.csv`` in the HTST lakehouse —
category POS data (multiple brands incl. competitors, many retail geographies) at
``Geography × Major Brand × Custom Subtype × Custom Process × Custom Size × Week``.
Circana weeks are labelled by their **ending Sunday**; we anchor every week to the
Monday of that Mon–Sun span so it lines up with the shipments velocity grid.

The demand-planning use is a **leading indicator**: consumer sell-through leads,
retailer orders follow, our shipments lag.  To compare shapes across series with
different units, each is rebased to an index:

    velocity   = Σ Units ÷ Σ (stores selling)         (a true blended per-store
                 rate — reconstruct stores = U Sales ÷ Units-per-Store-Selling,
                 then aggregate numerator/denominator; never average the ratio)
    baseline   = MEDIAN velocity over weeks with U Sales > 0   (FULL history)
    index      = velocity ÷ baseline × 100

The baseline is fixed over full history, so slicing the Week window only zooms —
the index always reads as "vs a typical week".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io

_SECRETS_SECTION: str = "fabric_htst"
_FOLDER: str = "RO Tracking/IRI"
_FILE_PREFIX: str = "IRI_Weekly_Units_"
_CACHE_TTL_SECONDS: int = 60 * 60

# Source columns.
COL_GEOGRAPHY: str = "Geography"
COL_BRAND: str = "Major Brand"
COL_SUBTYPE: str = "Custom Subtype"
COL_PROCESS: str = "Custom Process"
COL_SIZE: str = "Custom Size"
COL_WEEK: str = "Week"
COL_U_SALES: str = "U Sales"
COL_UNITS_PER_STORE: str = "Units per Store Selling"

# The five filterable IRI dimensions, in display order.
FILTER_COLS: tuple[str, ...] = (COL_GEOGRAPHY, COL_BRAND, COL_SUBTYPE, COL_PROCESS, COL_SIZE)

# Output (tidy weekly) columns.
WEEK_START: str = "week_start"
SELL_THROUGH_VELOCITY: str = "sell_through_velocity"
SELL_THROUGH_INDEX: str = "sell_through_index"
U_SALES_WK: str = "u_sales"


class IRIVelocityError(RuntimeError):
    """Raised on configuration / auth / I-O failures for the IRI extract."""


@dataclass(frozen=True)
class IRIWeekly:
    """Weekly IRI sell-through velocity + index for the current filter.

    ``weekly`` has ``week_start`` (Monday), ``sell_through_velocity`` (units per
    store selling, blended), ``sell_through_index`` (÷ baseline × 100) and
    ``u_sales``.  ``baseline`` is the full-history median velocity (the "100").
    ``file_name`` is the source snapshot for the caption.
    """
    weekly: pd.DataFrame
    baseline: Optional[float]
    file_name: str


def _latest_iri_path() -> tuple[str, str]:
    """``(full_path, name)`` of the newest ``IRI_Weekly_Units_*.csv``."""
    try:
        files = _io.list_files(_SECRETS_SECTION, _FOLDER, suffix=".csv")
    except _io.LakehouseIOError as exc:
        raise IRIVelocityError(f"Could not list 'Files/{_FOLDER}': {exc}") from exc
    cands = [f for f in files if f.name.startswith(_FILE_PREFIX)]
    if not cands:
        raise IRIVelocityError(
            f"No '{_FILE_PREFIX}*.csv' under 'Files/{_FOLDER}'.")
    newest = max(cands, key=lambda f: (f.name, f.last_modified or ""))
    return newest.full_path, newest.name


@st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_fetch() -> tuple[pd.DataFrame, str]:
    full_path, leaf = _latest_iri_path()
    try:
        df, _etag = _io.read_csv(_SECRETS_SECTION, full_path,
                                 read_csv_kwargs={"low_memory": False})
    except _io.LakehouseIOError as exc:
        raise IRIVelocityError(
            f"Could not read 'Files/{full_path}': {exc}") from exc
    if df is None:
        raise IRIVelocityError(f"File not found in OneLake: Files/{full_path}")
    df.columns = df.columns.str.strip()
    return df, leaf


def fetch_iri_df(*, force_refresh: bool = False) -> tuple[pd.DataFrame, str]:
    """Return ``(raw IRI dataframe, source file name)``.  ``force_refresh``
    clears the cache slot before reading."""
    if force_refresh:
        _cached_fetch.clear()
    return _cached_fetch()


def _week_start(week: pd.Series) -> pd.Series:
    """Monday of each Circana week (labelled by its ending Sunday)."""
    dt = pd.to_datetime(week, errors="coerce")
    return (dt - pd.to_timedelta(dt.dt.weekday, unit="D")).dt.normalize()


def distinct_values(df: pd.DataFrame, col: str) -> list[str]:
    """Sorted distinct non-blank values of *col* (``[]`` when absent)."""
    if df is None or df.empty or col not in df.columns:
        return []
    s = df[col].dropna().astype(str).str.strip()
    return sorted({v for v in s if v and v.lower() not in ("nan", "none")})


def build_iri_weekly(
    df: pd.DataFrame, *,
    geographies: Optional[list[str]] = None,
    brands: Optional[list[str]] = None,
    subtypes: Optional[list[str]] = None,
    processes: Optional[list[str]] = None,
    sizes: Optional[list[str]] = None,
) -> IRIWeekly:
    """Filter the IRI frame and build weekly sell-through velocity + index.

    Aggregates correctly across the selection: reconstructs stores-selling per
    row (``U Sales ÷ Units-per-Store-Selling``), sums Units and stores per week,
    then ``velocity = Σ Units ÷ Σ stores``.  Baseline = median velocity over
    weeks with U Sales > 0 (full history), so the index is comparable across
    slices.  Empty / column-less input → an empty, well-shaped result.
    """
    cols = [WEEK_START, SELL_THROUGH_VELOCITY, SELL_THROUGH_INDEX, U_SALES_WK]
    empty = IRIWeekly(pd.DataFrame(columns=cols), None, "")
    if df is None or df.empty or COL_WEEK not in df.columns:
        return empty

    work = df
    dim_filters = {
        COL_GEOGRAPHY: geographies, COL_BRAND: brands, COL_SUBTYPE: subtypes,
        COL_PROCESS: processes, COL_SIZE: sizes,
    }
    mask = pd.Series(True, index=work.index)
    for col, values in dim_filters.items():
        if values and col in work.columns:
            mask &= work[col].astype(str).str.strip().isin([str(v) for v in values])
    work = work.loc[mask]
    if work.empty:
        return empty

    units = pd.to_numeric(work.get(COL_U_SALES), errors="coerce").fillna(0.0)
    upss = pd.to_numeric(work.get(COL_UNITS_PER_STORE), errors="coerce")
    # stores selling = units ÷ (units per store); 0 when the ratio is missing/0.
    stores = np.where((upss > 0) & units.notna(), units / upss.replace(0, np.nan), 0.0)
    grouped = pd.DataFrame({
        WEEK_START: _week_start(work[COL_WEEK]).to_numpy(),
        "_units": units.to_numpy(),
        "_stores": np.nan_to_num(stores),
    }).dropna(subset=[WEEK_START])
    if grouped.empty:
        return empty

    weekly = (grouped.groupby(WEEK_START, as_index=False)
              .agg(_units=("_units", "sum"), _stores=("_stores", "sum"))
              .sort_values(WEEK_START).reset_index(drop=True))
    denom = weekly["_stores"].where(weekly["_stores"] > 0)
    weekly[SELL_THROUGH_VELOCITY] = weekly["_units"] / denom
    weekly[U_SALES_WK] = weekly["_units"]

    active = weekly.loc[weekly["_units"] > 0, SELL_THROUGH_VELOCITY].dropna()
    baseline = float(active.median()) if not active.empty and active.median() > 0 else None
    weekly[SELL_THROUGH_INDEX] = (
        weekly[SELL_THROUGH_VELOCITY] / baseline * 100.0 if baseline else np.nan)

    return IRIWeekly(
        weekly=weekly[[WEEK_START, SELL_THROUGH_VELOCITY, SELL_THROUGH_INDEX, U_SALES_WK]],
        baseline=baseline, file_name="")


# ─────────────────────────────────────────────────────────────────────────────
# Consumer MIX SHIFT — is the shopper's basket composition moving?
#
# A second, complementary read to velocity: velocity asks "how fast is the
# category selling"; mix asks "*what* are shoppers buying — and is that
# shifting".  We expose three lenses, each a distinct buying-preference signal:
#
#   • Pack Size  — trading down to small / up to large packs (affordability vs
#                  pantry-loading).  Marquee = weighted-avg pack size (oz).
#   • Brand      — our share of the category vs competitors (Fairlife, Nestlé…).
#                  Marquee = DARIGOLD share.  NB: this lens must span the WHOLE
#                  category, so it ignores the Brand filter (see build below).
#   • Subtype    — Regular / Specialty / Egg-Nog: premiumisation & seasonality.
#                  Marquee = Specialty (premium) share.
#
# All shares are unit shares (Σ U Sales), computed per week so the 100%-stacked
# view reads composition and the indexed view (base = each segment's first-week
# share) magnifies who is gaining / losing.
# ─────────────────────────────────────────────────────────────────────────────
MIX_SIZE: str = "size"
MIX_BRAND: str = "brand"
MIX_SUBTYPE: str = "subtype"
# (key, display label) in selector order.
MIX_DIMENSIONS: tuple[tuple[str, str], ...] = (
    (MIX_SIZE, "Pack Size"), (MIX_BRAND, "Brand share"), (MIX_SUBTYPE, "Subtype"),
)
_MIX_LABELS: dict[str, str] = dict(MIX_DIMENSIONS)

# Representative oz for each Custom Size band (bands are ranges → weighted-avg
# size is approximate, but the *shift %* is insensitive to the exact numbers as
# long as they stay monotonic).
_SIZE_OZ: dict[str, float] = {
    "<=12.0 OZ": 10.0, "12.1-20.0 OZ": 16.0, "20.1-47.9 OZ": 32.0, "48.0-95.9 OZ": 64.0,
}
_SIZE_LABELS: dict[str, str] = {
    "<=12.0 OZ": "Single-serve (≤12oz)", "12.1-20.0 OZ": "Pint (12–20oz)",
    "20.1-47.9 OZ": "Quart (20–48oz)", "48.0-95.9 OZ": "Half-gal+ (48–96oz)",
}
_MARQUEE_BRAND_SEG: str = "DARIGOLD"
_MARQUEE_SUBTYPE_SEG: str = "SPECIALTY"

# The IRI file spans the whole dairy category and mixes several size-band
# schemes / 20+ brands, so we cap the stack at the top-N segments by share and
# roll the remainder into "Other" (standard for a readable mix chart).
_MIX_TOP_N: int = 6
_OTHER: str = "Other"

# Output column carrying the per-week marquee metric (oz or share%).
MARQUEE: str = "_marquee"


def _band_oz(band: str) -> float:
    """Representative oz for any size-band label, so the weighted-avg pack size
    works across the several band schemes in the file (not just the milk bands).
    Known milk bands use a canonical pack oz (half-gal = 64…); everything else
    parses to a midpoint — ``"13.0-17.9 OZ"`` → 15.45, ``"<=12"`` → 9, ``">=80"``
    → 100.  Only monotonicity matters for the shift %."""
    if band in _SIZE_OZ:
        return _SIZE_OZ[band]
    nums = [float(n) for n in re.findall(r"\d+\.?\d*", str(band))]
    if not nums:
        return float("nan")
    text = str(band)
    if "<" in text or "≤" in text:
        return nums[0] * 0.75
    if ">" in text or "≥" in text:
        return nums[0] * 1.25
    return sum(nums) / len(nums)


@dataclass(frozen=True)
class IRIMix:
    """Weekly consumer-mix composition for one dimension.

    ``weekly`` = ``week_start`` + one unit-**share %** column per segment (in
    stacking order, small tail rolled into "Other") + ``_marquee`` (the headline
    number per week: weighted-avg pack size in oz for Size, else the marquee
    segment's share %).  ``segments`` lists the share columns in order; ``lens``
    is the ``MIX_*`` key; ``marquee_seg`` is the tracked segment (None for Size);
    ``marquee_label`` / ``marquee_unit`` ("oz" | "%") describe the headline.
    """
    weekly: pd.DataFrame
    segments: tuple[str, ...]
    dimension: str
    lens: str
    marquee_label: str
    marquee_unit: str
    marquee_seg: Optional[str]


def build_iri_mix(
    df: pd.DataFrame, dimension: str, *,
    geographies: Optional[list[str]] = None,
    brands: Optional[list[str]] = None,
    subtypes: Optional[list[str]] = None,
    processes: Optional[list[str]] = None,
    sizes: Optional[list[str]] = None,
    top_n: int = _MIX_TOP_N,
) -> IRIMix:
    """Weekly unit-share composition for *dimension* (``MIX_SIZE`` / ``MIX_BRAND``
    / ``MIX_SUBTYPE``) over the full history.

    Respects the IRI filters, **except the Brand lens ignores the Brand filter**
    (share of a single brand is meaningless — we always compare against the whole
    category).  The marquee is computed from *all* segments before the top-N +
    "Other" rollup, so capping the stack never distorts it.  Empty / column-less
    input → an empty, well-shaped result.
    """
    empty = IRIMix(pd.DataFrame(columns=[WEEK_START, MARQUEE]), (),
                   _MIX_LABELS.get(dimension, dimension), dimension, "", "%", None)
    if df is None or df.empty or COL_WEEK not in df.columns:
        return empty

    # Per-dimension config.
    if dimension == MIX_SIZE:
        gcol, is_size, ignore_brand, want_seg, marquee_unit = COL_SIZE, True, False, None, "oz"
    elif dimension == MIX_BRAND:
        gcol, is_size, ignore_brand, want_seg, marquee_unit = \
            COL_BRAND, False, True, _MARQUEE_BRAND_SEG, "%"
    elif dimension == MIX_SUBTYPE:
        gcol, is_size, ignore_brand, want_seg, marquee_unit = \
            COL_SUBTYPE, False, False, _MARQUEE_SUBTYPE_SEG, "%"
    else:
        return empty
    if gcol not in df.columns:
        return empty

    dim_filters = {
        COL_GEOGRAPHY: geographies,
        COL_BRAND: (None if ignore_brand else brands),
        COL_SUBTYPE: subtypes, COL_PROCESS: processes, COL_SIZE: sizes,
    }
    mask = pd.Series(True, index=df.index)
    for col, values in dim_filters.items():
        if values and col in df.columns:
            mask &= df[col].astype(str).str.strip().isin([str(v) for v in values])
    work = df.loc[mask]
    if work.empty:
        return empty

    tidy = pd.DataFrame({
        WEEK_START: _week_start(work[COL_WEEK]).to_numpy(),
        "_seg": work[gcol].astype(str).str.strip().to_numpy(),
        "_u": pd.to_numeric(work.get(COL_U_SALES), errors="coerce").fillna(0.0).to_numpy(),
    }).dropna(subset=[WEEK_START])
    tidy = tidy[~tidy["_seg"].str.lower().isin(("nan", "none", ""))]
    if tidy.empty:
        return empty

    # Units per (week, segment) → per-week share.
    wide = tidy.pivot_table(index=WEEK_START, columns="_seg", values="_u",
                            aggfunc="sum", fill_value=0.0).sort_index()
    total = wide.sum(axis=1).replace(0.0, np.nan)

    # Marquee (from ALL segments, before any rollup).
    if is_size:
        oz_row = pd.Series({s: _band_oz(s) for s in wide.columns}).fillna(0.0)
        marquee = wide.mul(oz_row, axis=1).sum(axis=1) / total
        marquee_seg = None
        marquee_label = "Wtd. avg pack size"
    else:
        marquee_seg = want_seg if want_seg in wide.columns else str(wide.sum().idxmax())
        marquee = wide.get(marquee_seg, 0.0) / total * 100.0
        marquee_label = ("Specialty share"
                         if marquee_seg == _MARQUEE_SUBTYPE_SEG and dimension == MIX_SUBTYPE
                         else f"{marquee_seg} share")

    share = wide.div(total, axis=0) * 100.0

    # Segment order: ascending pack size for Size, else descending total share.
    if is_size:
        order = sorted(share.columns, key=lambda s: (_band_oz(s) if pd.notna(_band_oz(s)) else 1e9))
    else:
        order = list(share.sum().sort_values(ascending=False).index)

    # Top-N by mean share; roll the tail into "Other" (kept segments hold order).
    if top_n and len(order) > top_n:
        top = set(share[order].mean().sort_values(ascending=False).index[:top_n])
        kept = [s for s in order if s in top]
        tail = [s for s in order if s not in top]
        stacked = share[kept].copy()
        if tail:
            stacked[_OTHER] = share[tail].sum(axis=1)
            kept = kept + [_OTHER]
        share, order = stacked, kept
    else:
        share = share[order]

    if is_size:
        share = share.rename(columns={s: _SIZE_LABELS.get(s, s) for s in share.columns})
        if marquee_seg:  # (unused for size, kept defensive)
            marquee_seg = _SIZE_LABELS.get(marquee_seg, marquee_seg)

    weekly = share.reset_index()
    weekly[MARQUEE] = marquee.to_numpy()
    segments = tuple(c for c in weekly.columns if c not in (WEEK_START, MARQUEE))
    return IRIMix(weekly=weekly, segments=segments,
                  dimension=_MIX_LABELS.get(dimension, dimension), lens=dimension,
                  marquee_label=marquee_label, marquee_unit=marquee_unit,
                  marquee_seg=marquee_seg)


def summarize_iri_mix(display: pd.DataFrame, mix: IRIMix) -> dict:
    """First→last read of a *displayed* mix window → KPIs + an actionable so-what.

    Returns ``{"available", "marquee_first", "marquee_last", "shift_text",
    "mix_index", "gainer", "loser", "level", "headline", "detail"}``.  All numbers
    are taken between the first and last displayed week, so the read tracks the
    Week slider.  ``mix_index`` = ½·Σ|Δ share| = the share of volume that would
    have to move to restore the opening mix ("% of volume reallocated").
    """
    out: dict = {"available": False}
    if display is None or display.empty or not mix.segments:
        return out
    segs = [s for s in mix.segments if s in display.columns]
    if not segs:
        return out
    first, last = display.iloc[0], display.iloc[-1]

    deltas = {s: float(last.get(s, 0.0) or 0.0) - float(first.get(s, 0.0) or 0.0)
              for s in segs}
    mix_index = 0.5 * sum(abs(d) for d in deltas.values())
    gainer = max(deltas, key=deltas.get)
    loser = min(deltas, key=deltas.get)

    mq = pd.to_numeric(display.get(MARQUEE), errors="coerce").dropna()
    mq_first = float(mq.iloc[0]) if not mq.empty else None
    mq_last = float(mq.iloc[-1]) if not mq.empty else None

    adverse = False
    if mix.lens == MIX_SIZE and mq_first and mq_last:
        shift_pct = (mq_last - mq_first) / mq_first * 100.0
        if mq_last < mq_first:
            word, headline = "smaller", "Shoppers are trading DOWN to smaller packs."
            detail = ("A smaller weighted-avg pack size is a classic affordability "
                      "signal — expect revenue-per-unit and mix pressure; make sure "
                      "small-pack supply can follow the shelf.")
            adverse = shift_pct <= -3.0
        elif mq_last > mq_first:
            word, headline = "larger", "Shoppers are trading UP to larger packs."
            detail = ("A larger weighted-avg pack size points to value-seeking / "
                      "pantry-loading — watch for a pull-forward that later softens "
                      "replenishment.")
        else:
            word, headline, detail = "flat", "Pack-size mix is stable.", \
                "No meaningful trade between pack sizes over the window."
        shift_text = f"{shift_pct:+.0f}% {word}"
    else:
        d = (mq_last - mq_first) if (mq_first is not None and mq_last is not None) else None
        shift_text = f"{d:+.1f} pp" if d is not None else "—"
        seg_name = mix.marquee_seg or "leading segment"
        if mix.lens == MIX_BRAND and mix.marquee_seg == _MARQUEE_BRAND_SEG:
            if d is not None and d < 0:
                headline = "We're LOSING category share to competitors."
                detail = ("Shoppers are shifting to other brands — a demand risk beyond "
                          "the category trend.  Probe distribution, price gaps and "
                          "promo support vs Fairlife / Nestlé.")
                adverse = d <= -1.0
            elif d is not None and d > 0:
                headline = "We're GAINING category share."
                detail = ("We're winning shelf preference vs competitors — protect the "
                          "gain with supply and check it isn't purely promo-bought.")
            else:
                headline, detail = "Brand share is holding.", \
                    "Our share of the category is broadly flat over the window."
        elif mix.lens == MIX_SUBTYPE and mix.marquee_seg == _MARQUEE_SUBTYPE_SEG:
            if d is not None and d < 0:
                headline = "Shoppers are DE-premiumising."
                detail = ("Premium (Specialty) share is slipping toward Regular — a "
                          "trade-down within the range; expect mix-led margin drag.")
                adverse = d <= -2.0
            elif d is not None and d > 0:
                headline = "Shoppers are premiumising."
                detail = ("Premium (Specialty) share is rising — a favourable mix shift; "
                          "make sure premium supply keeps pace.")
            else:
                headline, detail = "Subtype mix is stable.", \
                    "The Regular / Specialty split is broadly flat over the window."
        else:  # generic segment-share read (e.g. broad scope, no premium anchor)
            if d is not None and d > 0:
                headline = f"“{seg_name}” is gaining share."
            elif d is not None and d < 0:
                headline = f"“{seg_name}” is losing share."
            else:
                headline = f"“{seg_name}” share is stable."
            detail = ("Tracking the largest segment.  Filter the IRI block to a "
                      "product family for a sharper premium / trade-down read.")

    material = adverse or mix_index >= 5.0
    from data_sources import velocity_signals as _vsig
    level = _vsig.LEVEL_WATCH if material else _vsig.LEVEL_ALIGNED
    return {
        "available": True, "marquee_first": mq_first, "marquee_last": mq_last,
        "shift_text": shift_text, "mix_index": mix_index,
        "gainer": (gainer, deltas[gainer]), "loser": (loser, deltas[loser]),
        "level": level, "headline": headline, "detail": detail,
    }
