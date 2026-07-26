"""Unit tests for IRI consumer sell-through velocity + index (pure builder)."""
import pandas as pd
import pytest

import data_sources.iri_velocity as iri


def _raw() -> pd.DataFrame:
    # velocity = Σ units ÷ Σ (units ÷ units-per-store).  One row → velocity == upss.
    cols = [iri.COL_GEOGRAPHY, iri.COL_BRAND, iri.COL_SUBTYPE, iri.COL_PROCESS,
            iri.COL_SIZE, iri.COL_WEEK, iri.COL_U_SALES, iri.COL_UNITS_PER_STORE]
    rows = [
        # W1: GeoA 100u/10ps → 10 stores; GeoB 200u/40ps → 5 stores → 300u/15st = 20
        ["GeoA", "DARIGOLD", "REGULAR", "ESL", "20.1-47.9 OZ", "1/5/2025", 100, 10],
        ["GeoB", "DARIGOLD", "REGULAR", "ESL", "20.1-47.9 OZ", "1/5/2025", 200, 40],
        # W2: single row → velocity == upss = 30
        ["GeoA", "DARIGOLD", "REGULAR", "ESL", "20.1-47.9 OZ", "1/12/2025", 100, 30],
        # W3: velocity == 10
        ["GeoA", "DARIGOLD", "REGULAR", "ESL", "20.1-47.9 OZ", "1/19/2025", 100, 10],
        # A competitor row that must be filtered out when brand=DARIGOLD.
        ["GeoA", "FAIRLIFE", "REGULAR", "ESL", "20.1-47.9 OZ", "1/5/2025", 999, 999],
    ]
    return pd.DataFrame(rows, columns=cols)


def test_build_iri_weekly_aggregates_and_indexes():
    res = iri.build_iri_weekly(_raw(), brands=["DARIGOLD"])
    w = res.weekly.sort_values(iri.WEEK_START).reset_index(drop=True)
    assert list(w[iri.SELL_THROUGH_VELOCITY].round(4)) == [20.0, 30.0, 10.0]
    assert res.baseline == pytest.approx(20.0)                 # median of [20,30,10]
    assert list(w[iri.SELL_THROUGH_INDEX].round(1)) == [100.0, 150.0, 50.0]


def test_week_start_is_monday_of_ending_sunday():
    res = iri.build_iri_weekly(_raw(), brands=["DARIGOLD"])
    # Circana week ending Sun 1/5/2025 → Monday 2024-12-30.
    first = res.weekly.sort_values(iri.WEEK_START)[iri.WEEK_START].iloc[0]
    assert pd.Timestamp(first).weekday() == 0                  # Monday
    assert pd.Timestamp(first).date().isoformat() == "2024-12-30"


def test_empty_and_no_match():
    assert iri.build_iri_weekly(None).weekly.empty
    assert iri.build_iri_weekly(_raw(), brands=["NOPE"]).weekly.empty


# ── Consumer mix shift ───────────────────────────────────────────────────────

def _raw_mix() -> pd.DataFrame:
    cols = [iri.COL_GEOGRAPHY, iri.COL_BRAND, iri.COL_SUBTYPE, iri.COL_PROCESS,
            iri.COL_SIZE, iri.COL_WEEK, iri.COL_U_SALES]
    rows = [
        # DARIGOLD trades half-gal → pint between W1 and W2.
        ["GeoA", "DARIGOLD", "REGULAR", "ESL", "48.0-95.9 OZ", "1/5/2025", 60],
        ["GeoA", "DARIGOLD", "REGULAR", "ESL", "12.1-20.0 OZ", "1/5/2025", 40],
        ["GeoA", "DARIGOLD", "REGULAR", "ESL", "48.0-95.9 OZ", "1/12/2025", 40],
        ["GeoA", "DARIGOLD", "REGULAR", "ESL", "12.1-20.0 OZ", "1/12/2025", 60],
        # Competitor: category grows around us (DARIGOLD share 50% → 25%).
        ["GeoA", "FAIRLIFE", "REGULAR", "ESL", "48.0-95.9 OZ", "1/5/2025", 100],
        ["GeoA", "FAIRLIFE", "REGULAR", "ESL", "48.0-95.9 OZ", "1/12/2025", 300],
    ]
    return pd.DataFrame(rows, columns=cols)


def test_mix_size_shares_and_weighted_avg():
    mix = iri.build_iri_mix(_raw_mix(), iri.MIX_SIZE, brands=["DARIGOLD"])
    # Size lens respects the brand filter → DARIGOLD only, two size segments.
    assert mix.marquee_unit == "oz"
    assert set(mix.segments) == {"Pint (12–20oz)", "Half-gal+ (48–96oz)"}
    dm = mix.weekly.sort_values(iri.WEEK_START).reset_index(drop=True)
    # Shares sum to 100 each week.
    assert dm[list(mix.segments)].sum(axis=1).round(1).tolist() == [100.0, 100.0]
    # Wtd-avg oz: W1 (64*60+16*40)/100 = 44.8 → W2 = 35.2 (trading down).
    assert dm[iri.MARQUEE].round(1).tolist() == [44.8, 35.2]


def test_mix_brand_ignores_brand_filter_and_summary():
    # Brand lens must span the whole category even when a brand filter is set.
    mix = iri.build_iri_mix(_raw_mix(), iri.MIX_BRAND, brands=["DARIGOLD"])
    assert set(mix.segments) == {"DARIGOLD", "FAIRLIFE"}
    dm = mix.weekly.sort_values(iri.WEEK_START).reset_index(drop=True)
    # DARIGOLD marquee share 50% → 25%.
    assert dm[iri.MARQUEE].round(1).tolist() == [50.0, 25.0]
    summ = iri.summarize_iri_mix(dm, mix)
    assert "LOSING category share" in summ["headline"]
    assert summ["loser"][0] == "DARIGOLD" and summ["gainer"][0] == "FAIRLIFE"


def test_mix_top_n_rolls_tail_into_other():
    cols = [iri.COL_GEOGRAPHY, iri.COL_BRAND, iri.COL_SUBTYPE, iri.COL_PROCESS,
            iri.COL_SIZE, iri.COL_WEEK, iri.COL_U_SALES]
    rows = [["Geo", f"BRAND{i:02d}", "REGULAR", "ESL", "12.1-20.0 OZ",
             "1/5/2025", 100 - i] for i in range(9)]              # 9 brands
    mix = iri.build_iri_mix(pd.DataFrame(rows, columns=cols), iri.MIX_BRAND, top_n=6)
    assert len(mix.segments) == 7 and "Other" in mix.segments      # 6 + Other
    dm = mix.weekly
    assert dm[list(mix.segments)].sum(axis=1).round(1).iloc[0] == 100.0  # still 100%


def test_mix_summary_size_trade_down_flags_watch():
    mix = iri.build_iri_mix(_raw_mix(), iri.MIX_SIZE, brands=["DARIGOLD"])
    dm = mix.weekly.sort_values(iri.WEEK_START).reset_index(drop=True)
    summ = iri.summarize_iri_mix(dm, mix)
    assert "trading DOWN" in summ["headline"]
    assert summ["shift_text"].endswith("smaller")
    assert summ["mix_index"] == pytest.approx(20.0)      # ½(|−20|+|+20|)
    import data_sources.velocity_signals as vsig
    assert summ["level"] == vsig.LEVEL_WATCH
