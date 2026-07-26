"""Demand-quality analytics for the Velocity Analysis section — pure, testable.

Decomposes the IRI **total** sell-through the app already trusts into its
**base** (everyday, full-price) and **incremental** (promo-driven) parts, and
turns the richer IRI columns (Base/Incremental Units, price vs base price, ACV
feature/display) into three MECE reads a demand planner can react to:

  1. **Base health**   — is underlying pull eroding beneath the promos?
  2. **Promo economics** — how promo-dependent is volume, and is promo still
     efficient (lift per point of feature/display)?
  3. **Promo cohort**  — event-study around our promo onsets: do promos *build*
     demand or just *borrow* it (pull-forward)?

Store reconstruction mirrors :mod:`iri_velocity` (stores = U Sales ÷ Units per
Store Selling), so base velocity = Σ Base Units ÷ Σ stores is distribution-
neutral and comparable across a multi-item selection.  All functions are
Streamlit-/IO-free.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from data_sources import iri_velocity as iri

# ── Extra IRI source columns (beyond U Sales / Units-per-Store) ───────────────
COL_DOLLAR = "$ Sales"
COL_BASE_UNITS = "Base Units"
COL_INC_UNITS = "Incremental Units"
COL_PRICE = "Price per Unit"
COL_BASE_PRICE = "Base Price"
COL_ACV = "ACV Feature and/or Display"

# ── Output (weekly) columns ──────────────────────────────────────────────────
TOTAL_VEL = "total_vel"
BASE_VEL = "base_vel"
INC_VEL = "inc_vel"
TOTAL_INDEX = "total_index"
BASE_INDEX = "base_index"
LIFT_PCT = "lift_pct"          # incremental ÷ base   (promo dependency)
INC_SHARE = "inc_share"        # incremental ÷ total  (share of volume on deal)
DEPTH_PCT = "depth_pct"        # (base price − realised) ÷ base price
ACV = "acv"                    # % ACV on feature/display (merch support)
EFFICIENCY = "efficiency"      # lift% per ACV point (promo weeks only)
STORES = "stores"              # Σ stores selling (distribution breadth)
DIST_INDEX = "dist_index"      # stores ÷ own full-history median × 100
BASE_UNITS = "base_units"      # Σ base units (absolute, everyday)

# Signal levels (reuse the velocity traffic light vocabulary).
LEVEL_GOOD = "aligned"
LEVEL_WATCH = "watch"
LEVEL_ALERT = "alert"

_ACV_MIN = 2.0        # need ≥2% ACV feature/display to call a week "on promo"
_SLOPE_EPS = 0.4      # base-index pts/wk to call a real trend
_RECENT_WEEKS = 13    # base-erosion look-back
_MIN_POINTS = 6       # min weeks to fit a trustworthy base trend
_T_SIGNIF = 2.0       # |slope ÷ std-err| for ~95% significance


@dataclass(frozen=True)
class IRIQuality:
    """Weekly base/incremental decomposition + promo economics for one filter."""
    weekly: pd.DataFrame
    base_baseline: Optional[float]
    total_baseline: Optional[float]


def _week_start(week: pd.Series) -> pd.Series:
    dt = pd.to_datetime(week, errors="coerce")
    return (dt - pd.to_timedelta(dt.dt.weekday, unit="D")).dt.normalize()


def build_iri_quality(
    df: pd.DataFrame, *,
    geographies: Optional[list[str]] = None,
    brands: Optional[list[str]] = None,
    subtypes: Optional[list[str]] = None,
    processes: Optional[list[str]] = None,
    sizes: Optional[list[str]] = None,
) -> IRIQuality:
    """Weekly base vs incremental velocity + promo economics for the IRI filter.

    ``base_vel`` / ``total_vel`` = Σ Base|U Units ÷ Σ stores; each indexed to its
    own fixed full-history median (so slicing the Week window only zooms).
    ``lift_pct`` = Σ Incremental ÷ Σ Base; ``depth_pct`` = (Σ base$ − Σ$Sales) ÷
    Σ base$ per unit; ``acv`` = unit-weighted feature/display %; ``efficiency`` =
    lift% ÷ ACV, only where ACV ≥ 2% (else NaN — it explodes near zero).
    """
    cols = [iri.WEEK_START, TOTAL_VEL, BASE_VEL, INC_VEL, TOTAL_INDEX, BASE_INDEX,
            LIFT_PCT, INC_SHARE, DEPTH_PCT, ACV, EFFICIENCY, STORES, DIST_INDEX,
            BASE_UNITS]
    empty = IRIQuality(pd.DataFrame(columns=cols), None, None)
    if df is None or df.empty or iri.COL_WEEK not in df.columns \
            or COL_BASE_UNITS not in df.columns:
        return empty

    dim_filters = {
        iri.COL_GEOGRAPHY: geographies, iri.COL_BRAND: brands,
        iri.COL_SUBTYPE: subtypes, iri.COL_PROCESS: processes, iri.COL_SIZE: sizes,
    }
    mask = pd.Series(True, index=df.index)
    for col, values in dim_filters.items():
        if values and col in df.columns:
            mask &= df[col].astype(str).str.strip().isin([str(v) for v in values])
    work = df.loc[mask]
    if work.empty:
        return empty

    def num(c):
        return pd.to_numeric(work.get(c), errors="coerce")
    units = num(iri.COL_U_SALES).fillna(0.0)
    base = num(COL_BASE_UNITS).fillna(0.0)
    inc = num(COL_INC_UNITS).fillna(0.0)
    upss = num(iri.COL_UNITS_PER_STORE)
    stores = np.where((upss > 0) & units.notna(), units / upss.replace(0, np.nan), 0.0)
    dollars = num(COL_DOLLAR).fillna(0.0)
    base_price_u = (num(COL_BASE_PRICE) * units).fillna(0.0)   # for weighted base price
    acv_u = (num(COL_ACV) * units).fillna(0.0)                 # unit-weighted ACV

    g = pd.DataFrame({
        iri.WEEK_START: _week_start(work[iri.COL_WEEK]).to_numpy(),
        "u": units.to_numpy(), "base": base.to_numpy(), "inc": inc.to_numpy(),
        "stores": np.nan_to_num(stores), "dollars": dollars.to_numpy(),
        "bpu": base_price_u.to_numpy(), "acvu": acv_u.to_numpy(),
    }).dropna(subset=[iri.WEEK_START])
    if g.empty:
        return empty
    wk = g.groupby(iri.WEEK_START, as_index=False).sum().sort_values(iri.WEEK_START)

    st_denom = wk["stores"].where(wk["stores"] > 0)
    u_denom = wk["u"].where(wk["u"] > 0)
    wk[TOTAL_VEL] = wk["u"] / st_denom
    wk[BASE_VEL] = wk["base"] / st_denom
    wk[INC_VEL] = wk["inc"] / st_denom
    wk[LIFT_PCT] = wk["inc"] / wk["base"].where(wk["base"] > 0) * 100.0
    wk[INC_SHARE] = wk["inc"] / u_denom * 100.0
    realised = wk["dollars"] / u_denom                     # avg realised price
    base_price = wk["bpu"] / u_denom                       # weighted base price
    wk[DEPTH_PCT] = (base_price - realised) / base_price.where(base_price > 0) * 100.0
    wk[ACV] = wk["acvu"] / u_denom
    wk[EFFICIENCY] = np.where(wk[ACV] >= _ACV_MIN, wk[LIFT_PCT] / wk[ACV].replace(0, np.nan), np.nan)

    base_active = wk.loc[wk["base"] > 0, BASE_VEL].dropna()
    tot_active = wk.loc[wk["u"] > 0, TOTAL_VEL].dropna()
    base_baseline = float(base_active.median()) if not base_active.empty and base_active.median() > 0 else None
    total_baseline = float(tot_active.median()) if not tot_active.empty and tot_active.median() > 0 else None
    wk[BASE_INDEX] = wk[BASE_VEL] / base_baseline * 100.0 if base_baseline else np.nan
    wk[TOTAL_INDEX] = wk[TOTAL_VEL] / total_baseline * 100.0 if total_baseline else np.nan

    # Distribution (stores selling) indexed to its own median + absolute base units.
    dist_active = wk.loc[wk["stores"] > 0, "stores"].dropna()
    dist_baseline = float(dist_active.median()) if not dist_active.empty and dist_active.median() > 0 else None
    wk[STORES] = wk["stores"]
    wk[DIST_INDEX] = wk["stores"] / dist_baseline * 100.0 if dist_baseline else np.nan
    wk[BASE_UNITS] = wk["base"]

    return IRIQuality(weekly=wk[[iri.WEEK_START, *cols[1:]]].reset_index(drop=True),
                      base_baseline=base_baseline, total_baseline=total_baseline)


# ── Signals (pure) ───────────────────────────────────────────────────────────

def _slope_ols(series: pd.Series, n: int):
    """OLS slope over the last *n* non-null points → ``(slope, std_err, r2, m)``.
    ``slope`` is None when there are fewer than :data:`_MIN_POINTS` points."""
    s = pd.to_numeric(pd.Series(series), errors="coerce").dropna().tail(n)
    m = len(s)
    if m < _MIN_POINTS:
        return None, None, None, m
    x = np.arange(m, dtype=float)
    y = s.to_numpy(float)
    xm, ym = x.mean(), y.mean()
    sxx = float(((x - xm) ** 2).sum())
    if sxx == 0:
        return 0.0, None, None, m
    b = float(((x - xm) * (y - ym)).sum() / sxx)
    resid = y - (ym + b * (x - xm))
    dof = m - 2
    s2 = float((resid ** 2).sum() / dof) if dof > 0 else None
    se = float((s2 / sxx) ** 0.5) if s2 is not None and s2 >= 0 else None
    sst = float(((y - ym) ** 2).sum())
    r2 = float(1 - (resid ** 2).sum() / sst) if sst > 0 else None
    return b, se, r2, m


def _rate_dist_split(weekly: pd.DataFrame, n: int):
    """Shift-share of the recent base-VOLUME change into rate-of-sale vs
    distribution: Δ(base units) ≈ Δvelocity·avg-stores + Δstores·avg-velocity."""
    if BASE_VEL not in weekly.columns or STORES not in weekly.columns:
        return None
    r = weekly.tail(n)
    v = pd.to_numeric(r[BASE_VEL], errors="coerce")
    s = pd.to_numeric(r[STORES], errors="coerce")
    ok = v.notna() & s.notna()
    v, s = v[ok], s[ok]
    if len(v) < 2:
        return None
    v0, v1, s0, s1 = float(v.iloc[0]), float(v.iloc[-1]), float(s.iloc[0]), float(s.iloc[-1])
    rate = (v1 - v0) * (s0 + s1) / 2.0        # velocity move × avg stores
    dist = (s1 - s0) * (v0 + v1) / 2.0        # stores move × avg velocity
    return {"rate": rate, "dist": dist, "total": rate + dist}


def _last(series: pd.Series) -> Optional[float]:
    s = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
    return float(s.iloc[-1]) if not s.empty else None


def base_erosion_signal(weekly: pd.DataFrame, *, recent_weeks: int = _RECENT_WEEKS) -> dict:
    """Assumption + drill-in on whether the base is eroding — with significance
    (OLS slope vs its std-error) and a rate-of-sale vs distribution attribution."""
    if weekly is None or weekly.empty or BASE_INDEX not in weekly.columns:
        return {"level": LEVEL_GOOD, "headline": "No base data in view.",
                "detail": "", "drill_in": "", "significant": False}
    b, se, r2, npts = _slope_ols(weekly[BASE_INDEX], recent_weeks)
    blast, tlast = _last(weekly[BASE_INDEX]), _last(weekly.get(TOTAL_INDEX))
    if b is None or blast is None:
        return {"level": LEVEL_GOOD,
                "headline": f"Not enough weeks to read a base trend (need ≥{_MIN_POINTS}).",
                "detail": "Widen the Week window or loosen the IRI filters.",
                "drill_in": "", "base_slope": None, "base_last": blast,
                "total_last": tlast, "base_r2": None, "significant": False}
    significant = se is not None and se > 0 and abs(b / se) >= _T_SIGNIF
    material = significant and abs(b) >= _SLOPE_EPS
    conf = f", R²={r2:.2f}" if r2 is not None else ""
    split = _rate_dist_split(weekly, recent_weeks)
    attr = ""
    if split and abs(split["total"]) > 1e-9:
        driver = ("rate-of-sale (per-store velocity)"
                  if abs(split["rate"]) >= abs(split["dist"])
                  else "distribution (stores selling)")
        attr = f"  Driven mostly by **{driver}**."
    masked = tlast is not None and (tlast - blast) >= 8 and material and b < 0
    if material and b < 0:
        level = LEVEL_ALERT if masked else LEVEL_WATCH
        headline = "Assume the base is ERODING."
        detail = (f"Base velocity is trending down ({b:+.1f} idx-pts/wk over {npts} wks"
                  f"{conf}, now {blast:.0f}% of normal)"
                  + (f" while total holds at {tlast:.0f}% — the flat total is promo-masked."
                     if masked else ".") + attr)
    elif material and b > 0:
        level, headline = LEVEL_GOOD, "Assume the base is GROWING."
        detail = (f"Base velocity is rising ({b:+.1f} idx-pts/wk{conf}, now {blast:.0f}% of "
                  f"normal) — real underlying demand, not just promo." + attr)
    else:
        level, headline = LEVEL_GOOD, "Assume the base is HOLDING."
        why = ("not statistically distinguishable from flat" if not significant
               else "within the noise band")
        detail = (f"Base velocity trend {b:+.1f} idx-pts/wk{conf} is {why}; base is "
                  f"~{blast:.0f}% of normal over {npts} wks." + attr)
    return {"level": level, "headline": headline, "detail": detail,
            "base_slope": b, "base_last": blast, "total_last": tlast,
            "base_r2": r2, "significant": significant,
            "drill_in": ("Now shown below: the **distribution** (stores selling) line "
                         "splits a base move into breadth vs per-store **rate-of-sale**.  "
                         "Drill further by subtype / size / geography to see if it's broad "
                         "or one SKU.")}


def promo_economics_signal(weekly: pd.DataFrame, *, recent_weeks: int = 8) -> dict:
    """Assumption + drill-in on promo dependency and efficiency."""
    if weekly is None or weekly.empty:
        return {"level": LEVEL_GOOD, "headline": "No promo data in view.",
                "detail": "", "drill_in": ""}
    lift = pd.to_numeric(weekly.get(LIFT_PCT), errors="coerce")
    depth = pd.to_numeric(weekly.get(DEPTH_PCT), errors="coerce")
    eff = pd.to_numeric(weekly.get(EFFICIENCY), errors="coerce").dropna()
    lift_recent = lift.dropna().tail(recent_weeks).mean()
    depth_recent = depth.dropna().tail(recent_weeks).mean()
    # Efficiency trend: recent promo weeks vs earlier promo weeks.
    eff_trend = None
    if len(eff) >= 6:
        half = len(eff) // 2
        early, late = eff.iloc[:half].mean(), eff.iloc[half:].mean()
        eff_trend = late - early
    if pd.isna(lift_recent):
        return {"level": LEVEL_GOOD, "headline": "Little promo activity in view.",
                "detail": "", "drill_in": ""}
    fatiguing = eff_trend is not None and eff_trend < 0
    level = LEVEL_WATCH if (fatiguing or lift_recent >= 30) else LEVEL_GOOD
    eff_txt = ("efficiency (lift per ACV pt) is "
               + ("falling — promo fatigue" if fatiguing else "steady/rising")
               if eff_trend is not None else "efficiency n/a (thin ACV support)")
    headline = ("Assume promo is FATIGUING." if fatiguing
                else "Assume promo is EFFICIENT." if eff_trend is not None
                else "Promo dependency read.")
    detail = (f"Recent lift is {lift_recent:.0f}% of base at ~{depth_recent:.0f}% "
              f"discount depth; {eff_txt}.")
    return {"level": level, "headline": headline, "detail": detail,
            "lift_recent": float(lift_recent), "depth_recent": float(depth_recent),
            "eff_trend": eff_trend,
            "drill_in": ("To solidify: plot the **depth→lift response curve** "
                         "(diminishing returns), split by tactic (Feature vs TPR), and "
                         "overlay **competitor price** (IRI carries every brand) — are "
                         "you discounting to defend share?")}


def promo_response_curve(weekly: pd.DataFrame, *, min_points: int = _MIN_POINTS) -> dict:
    """Depth→lift response points + an OLS fit → diminishing-returns read.

    Returns ``{available, depth, lift, fit_x, fit_y, marginal, r2, n}`` over the
    weeks with a real discount (depth > 0).  ``marginal`` = extra lift‑points per
    point of discount depth (the slope); a low/flat slope = promo not pulling its
    weight.  Empty / too‑few‑points → ``{"available": False}``.
    """
    out = {"available": False}
    if weekly is None or weekly.empty:
        return out
    d = pd.to_numeric(weekly.get(DEPTH_PCT), errors="coerce")
    lift = pd.to_numeric(weekly.get(LIFT_PCT), errors="coerce")
    ok = d.notna() & lift.notna() & (d > 0)          # promoted weeks only
    d, lift = d[ok], lift[ok]
    if len(d) < min_points:
        return out
    x, y = d.to_numpy(float), lift.to_numpy(float)
    b, a = np.polyfit(x, y, 1)
    yhat = a + b * x
    sst = float(((y - y.mean()) ** 2).sum())
    r2 = float(1 - ((y - yhat) ** 2).sum() / sst) if sst > 0 else None
    lo, hi = float(x.min()), float(x.max())
    return {"available": True, "depth": [float(v) for v in x], "lift": [float(v) for v in y],
            "fit_x": [lo, hi], "fit_y": [float(a + b * lo), float(a + b * hi)],
            "marginal": float(b), "r2": r2, "n": int(len(x))}


# ── Promo cohort (event study around OUR promo onsets) ───────────────────────

@dataclass(frozen=True)
class PromoCohort:
    """Event-study curve (offset weeks → mean base/total index) + summary."""
    curve: pd.DataFrame          # offset, base_mean, total_mean, n
    n_events: int
    summary: dict


def promo_onsets(promo_week_starts) -> list:
    """Onset weeks = promo weeks whose prior week was NOT on promo (a fresh start)."""
    weeks = sorted(pd.Timestamp(w).normalize() for w in promo_week_starts)
    wset = set(weeks)
    return [w for w in weeks if (w - pd.Timedelta(days=7)) not in wset]


def build_promo_cohort(
    weekly: pd.DataFrame, onsets: list, *,
    k_pre: int = 4, k_post: int = 6, min_events: int = 3,
) -> PromoCohort:
    """Align base/total index to event-time around each promo *onset* and average.

    Returns per-offset means (weeks ``-k_pre … +k_post``) plus a build-vs-borrow
    summary: pre-promo baseline, in-promo peak, post-promo trough, pull-forward
    ratio and the base shift (post base − pre base).  Fewer than ``min_events``
    onsets → an empty result (a noisy curve is worse than none).
    """
    empty = PromoCohort(pd.DataFrame(columns=["offset", "base_mean", "total_mean", "n"]),
                        0, {})
    if weekly is None or weekly.empty or not onsets or BASE_INDEX not in weekly.columns:
        return empty
    idx = weekly.set_index(weekly[iri.WEEK_START].map(lambda x: pd.Timestamp(x).normalize()))
    base = pd.to_numeric(idx[BASE_INDEX], errors="coerce")
    total = pd.to_numeric(idx.get(TOTAL_INDEX), errors="coerce")
    rows = []
    used = 0
    for w0 in onsets:
        w0 = pd.Timestamp(w0).normalize()
        # Require the pre-window to exist so the baseline is real.
        if (w0 - pd.Timedelta(days=7)) not in base.index:
            pass
        seen = False
        for off in range(-k_pre, k_post + 1):
            wk = w0 + pd.Timedelta(days=7 * off)
            if wk in base.index and pd.notna(base.loc[wk]):
                rows.append({"offset": off, "base": float(base.loc[wk]),
                             "total": float(total.loc[wk]) if wk in total.index and pd.notna(total.loc[wk]) else np.nan})
                seen = True
        used += 1 if seen else 0
    if used < min_events or not rows:
        return PromoCohort(empty.curve, used, {})
    df = pd.DataFrame(rows)
    curve = (df.groupby("offset")
               .agg(base_mean=("base", "mean"), total_mean=("total", "mean"),
                    n=("base", "size")).reset_index())

    pre = curve[curve["offset"] < 0]
    inp = curve[(curve["offset"] >= 0) & (curve["offset"] <= 2)]
    post = curve[curve["offset"] > 2]
    base_pre = float(pre["base_mean"].mean()) if not pre.empty else np.nan
    base_post = float(post["base_mean"].mean()) if not post.empty else np.nan
    tot_pre = float(pre["total_mean"].mean()) if not pre.empty else np.nan
    tot_peak = float(inp["total_mean"].max()) if not inp.empty else np.nan
    tot_post_min = float(post["total_mean"].min()) if not post.empty else np.nan
    lift = (tot_peak - tot_pre) if pd.notna(tot_peak) and pd.notna(tot_pre) else np.nan
    deficit = (tot_pre - tot_post_min) if pd.notna(tot_post_min) and pd.notna(tot_pre) else np.nan
    pull_fwd = float(max(0.0, deficit) / lift) if pd.notna(lift) and lift > 0 and pd.notna(deficit) else np.nan
    summary = {
        "base_pre": base_pre, "base_post": base_post,
        "base_shift_pct": (base_post - base_pre) if pd.notna(base_post) and pd.notna(base_pre) else np.nan,
        "tot_pre": tot_pre, "tot_peak": tot_peak, "tot_post_min": tot_post_min,
        "pull_forward_ratio": pull_fwd,
    }
    return PromoCohort(curve=curve, n_events=used, summary=summary)


def promo_cohort_signal(cohort: PromoCohort) -> dict:
    """Build-vs-borrow assumption + drill-in from the cohort summary."""
    if cohort is None or cohort.n_events == 0 or not cohort.summary:
        return {"level": LEVEL_GOOD,
                "headline": "Not enough distinct promo onsets for a cohort.",
                "detail": "This slice is near-continuously promoted or has few clean "
                          "starts — widen the Week window or pick a less-promoted slice.",
                "drill_in": ""}
    s = cohort.summary
    shift = s.get("base_shift_pct")
    pull = s.get("pull_forward_ratio")
    borrows = pd.notna(shift) and shift < -2
    builds = pd.notna(shift) and shift > 2
    level = LEVEL_ALERT if (pd.notna(pull) and pull >= 0.5) or borrows else \
        (LEVEL_GOOD if builds else LEVEL_WATCH)
    if builds:
        headline = "Assume these promos BUILD demand."
    elif borrows:
        headline = "Assume these promos BORROW demand (pull-forward)."
    else:
        headline = "Assume these promos are roughly volume-neutral."
    parts = []
    if pd.notna(shift):
        parts.append(f"post-promo base runs {shift:+.0f} idx-pts vs pre-promo")
    if pd.notna(pull):
        parts.append(f"~{pull * 100:.0f}% of the in-promo lift is given back afterward")
    detail = ((" · ".join(parts) + f".  ({cohort.n_events} onsets).") if parts
              else f"{cohort.n_events} onsets averaged.")
    return {"level": level, "headline": headline, "detail": detail,
            "drill_in": ("To solidify: split cohorts by **tactic & depth** (do deeper "
                         "promos borrow more?) and watch **post-promo base recovery** "
                         "over several weeks — a lasting lift = real trial→repeat.")}


__all__ = [
    "IRIQuality", "PromoCohort",
    "TOTAL_VEL", "BASE_VEL", "INC_VEL", "TOTAL_INDEX", "BASE_INDEX",
    "LIFT_PCT", "INC_SHARE", "DEPTH_PCT", "ACV", "EFFICIENCY",
    "STORES", "DIST_INDEX", "BASE_UNITS",
    "LEVEL_GOOD", "LEVEL_WATCH", "LEVEL_ALERT",
    "build_iri_quality", "base_erosion_signal", "promo_economics_signal",
    "promo_response_curve", "promo_onsets", "build_promo_cohort", "promo_cohort_signal",
]
