"""Unit tests for data_sources.trade_spend (pure builder; no Fabric/Streamlit)."""
import pandas as pd
import pytest

import data_sources.trade_spend as tsp


def _mk(rows) -> pd.DataFrame:
    """Build a normalised trade-spend frame (as fetch would return it)."""
    df = pd.DataFrame(rows, columns=[
        "item", "corp", "tactic", "status", "start", "end", "spend", "vol", "desc"])
    return pd.DataFrame({
        tsp.COL_ITEM: df["item"],
        tsp.ITEM_KEY: df["item"].map(tsp.norm_item),
        tsp.COL_CORP: df["corp"].astype(str).str.strip(),
        tsp.COL_TACTIC: df["tactic"].astype(str).str.strip(),
        tsp.COL_STATUS: df["status"].astype(str).str.strip(),
        tsp.COL_START_EVENT: pd.to_datetime(df["start"]),
        tsp.COL_END_EVENT: pd.to_datetime(df["end"]),
        tsp.START_DATE: pd.to_datetime(df["start"]),
        tsp.END_DATE: pd.to_datetime(df["end"]),
        tsp.COL_DESC: df["desc"],
        tsp.COL_SPEND: df["spend"].astype(float),
        tsp.COL_VOLUME: df["vol"].astype(float),
    })


# All Mondays: 2025-01-06, -13, -20.
_ROWS = [
    # item, corp, tactic, status, start, end, spend, vol, desc
    (100, "Kroger", "Ad Feature", "Claims Applied", "2025-01-06", "2025-01-19", 1000, 500, "K Feature"),
    (200, "Kroger", "Ad Feature", "Closed", "2025-01-06", "2025-01-12", 400, 200, "K Feature"),
    (100, "Target", "TPR Only", "Committed", "2025-01-20", "2025-01-26", 200, 100, "T TPR"),
    (100, "Kroger", "Corp Program", "Claims Applied", "2025-01-06", "2026-01-05", 9, 9, "Annual"),
    (200, "Kroger", "Ad Feature", "Cancelled", "2025-01-06", "2025-01-19", 999, 999, "Nope"),
]


def test_norm_item():
    assert tsp.norm_item(340021) == "340021"
    assert tsp.norm_item("0340021") == "340021"
    assert tsp.norm_item("34-0021") == "340021"


def test_per_week_intensity_excludes_programs_and_cancelled():
    win = (pd.Timestamp("2025-01-01").date(), pd.Timestamp("2025-02-01").date())
    pw = tsp.build_promo_windows(_mk(_ROWS), week_window=win)
    b = pw.bands.set_index(pw.bands["week_start"].dt.strftime("%Y-%m-%d"))
    assert list(b.index) == ["2025-01-06", "2025-01-13", "2025-01-20"]
    # Corp Program (year-long) + Cancelled never appear.
    assert "Corp Program" not in set(pw.bands["tactic"])
    assert pw.tactics == ("Ad Feature", "TPR Only")
    # Week 1: two Ad Feature SKUs; spend = 1000/2wks + 400/1wk = 900; vol 250+200.
    w1 = b.loc["2025-01-06"]
    assert w1["tactic"] == "Ad Feature" and w1["weight"] == 2 and w1["total_skus"] == 2
    assert w1["spend"] == pytest.approx(900.0) and w1["volume"] == pytest.approx(450.0)
    # Week 2: only item 100's feature continues → weight 1.
    assert b.loc["2025-01-13"]["weight"] == 1
    assert b.loc["2025-01-20"]["tactic"] == "TPR Only"
    assert pw.max_weight == 2.0


def test_corporate_group_filter():
    win = (pd.Timestamp("2025-01-01").date(), pd.Timestamp("2025-02-01").date())
    pw = tsp.build_promo_windows(_mk(_ROWS), corporate_groups=["Kroger"], week_window=win)
    # Target's TPR (2025-01-20) drops → only the two Kroger Ad Feature weeks.
    assert list(pw.bands["week_start"].dt.strftime("%Y-%m-%d")) == ["2025-01-06", "2025-01-13"]
    assert set(pw.bands["tactic"]) == {"Ad Feature"}


def test_item_scope_filter():
    win = (pd.Timestamp("2025-01-01").date(), pd.Timestamp("2025-02-01").date())
    pw = tsp.build_promo_windows(_mk(_ROWS), item_keys={"100"}, week_window=win)
    # Item 200 excluded → week 1 Ad Feature is item 100 only (1 SKU, spend 500).
    w1 = pw.bands[pw.bands["week_start"] == pd.Timestamp("2025-01-06")].iloc[0]
    assert w1["total_skus"] == 1 and w1["spend"] == pytest.approx(500.0)


def test_week_window_clips():
    win = (pd.Timestamp("2025-01-06").date(), pd.Timestamp("2025-01-13").date())
    pw = tsp.build_promo_windows(_mk(_ROWS), week_window=win)
    assert list(pw.bands["week_start"].dt.strftime("%Y-%m-%d")) == ["2025-01-06", "2025-01-13"]


def test_tactic_override_can_show_programs():
    win = (pd.Timestamp("2025-01-01").date(), pd.Timestamp("2025-02-01").date())
    pw = tsp.build_promo_windows(_mk(_ROWS), tactics=["Corp Program"], week_window=win)
    # The year-long program now shows, clipped to the window's four weeks.
    assert set(pw.bands["tactic"]) == {"Corp Program"}
    assert len(pw.bands) == 4


def test_empty_and_no_match():
    assert tsp.build_promo_windows(pd.DataFrame()).bands.empty
    win = (pd.Timestamp("2030-01-01").date(), pd.Timestamp("2030-02-01").date())
    assert tsp.build_promo_windows(_mk(_ROWS), week_window=win).bands.empty
