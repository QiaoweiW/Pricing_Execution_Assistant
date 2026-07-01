"""Unit tests for the in-app Demand Plan ETL (data_sources.demand_plan_pipeline).

The pipeline's only side effects are Fabric reads/writes via the connector layer,
so we monkeypatch those four functions and assert on the in-memory frames it
produces. No network, no Streamlit runtime.
"""
from __future__ import annotations

import io

import pandas as pd

import data_sources.demand_plan_pipeline as dp


def _base_plan_bytes() -> bytes:
    """Upload with two Base Plan items (one unknown → must drop) + month/Cycle."""
    return pd.DataFrame({
        "Start of Month": ["2026-06-01", "2026-07-01", "2026-06-01"],
        "Item": ["310180", "310180", "999999"],
        "Item Description": ["DG Btr", "DG Btr", "Mystery"],
        "Value": ["10036", "10036", "10036"],        # → Party Site Number
        "Total": ["1,944.0", "2,000", "5"],          # → Demand Plan Pounds (comma fmt)
        "Corporate Group": ["ACME", "ACME", "ZZZ"],
        "month": ["2026-06-01"] * 3,
        "Cycle": ["C5"] * 3,
    }).to_csv(index=False).encode()


def _sources() -> dict:
    seed = pd.DataFrame({
        "Format": ["F1"], "Customer": ["CustA"], "Taxonomy": ["T"], "Brand": ["B"],
        "Item #": ["310180"], "Item Desc": ["DG Btr"], "Probability": ["1.0"],
        "First Ship Date": ["2026-04-01"], "Lbs./yr": ["3650"], "PC$/yr": ["100"],
        "Slotting": ["0"],
    })
    tblm = pd.DataFrame({
        "Month Number": [f"Month {i}" for i in range(1, 37)],
        "Start of Month": [(pd.Timestamp("2026-04-01") + pd.DateOffset(months=i - 1))
                           .strftime("%Y-%m-%d") for i in range(1, 37)],
    })
    pdh = pd.DataFrame({"Item No": ["310180"], "Business Unit": ["B2C"],
                        "Portfolio Major": ["Butter"], "Portfolio Minor": ["Qtr"],
                        "Supply Format": ["Carton"]})
    ro_master = pd.DataFrame({"Item #": ["310180"], "Business Unit": ["B2C"],
                              "Portfolio Major": ["Butter"], "Portfolio Minor": ["Qtr"],
                              "Supply Format": ["Carton"]})
    hist_c4 = pd.DataFrame({
        "Start of Month": ["5/1/2026"], "Item": ["310180"], "Item Description": ["x"],
        "Party Site Number": ["10036"], "Demand Plan Pounds": ["100"],
        "Forecast Type": ["Base Plan"], "Business Unit": ["B2C"], "Cycle": ["C4"],
    })
    return {dp._RO_SEED_BLOB: seed, dp._TBL_MONTHS_BLOB: tblm, dp._PDH_BLOB: pdh,
            dp._RO_ITEMS_BLOB: ro_master, dp._HISTORY_TRACKER_BLOB: hist_c4}


def _patch_io(monkeypatch, reads: dict) -> dict:
    """Patch the connector calls; return the dict of what got written."""
    written: dict = {}
    monkeypatch.setattr(dp, "read_csv",
                        lambda sec, path, read_csv_kwargs=None:
                        (reads[path].copy() if path in reads else None, "e"))
    monkeypatch.setattr(dp, "write_csv",
                        lambda sec, path, df, etag=None, to_csv_kwargs=None:
                        (written.__setitem__(path, df.copy()), "e")[1])
    monkeypatch.setattr(dp, "write_bytes",
                        lambda sec, path, payload, etag=None:
                        (written.__setitem__(path, payload), "e")[1])
    monkeypatch.setattr(dp, "archive_bytes",
                        lambda sec, d, name, payload, timestamp=None: f"{d}/{name}_TS.csv")
    return written


def test_pipeline_builds_all_outputs_and_filters_b2c(monkeypatch):
    written = _patch_io(monkeypatch, _sources())
    res = dp.run_demand_plan_pipeline(_base_plan_bytes())

    assert res.ok, res.errors
    assert res.cycle == "C5"
    assert res.meeting_month.isoformat() == "2026-06-01"
    assert res.window_end.isoformat() == "2028-06-01"   # meeting + 24 months

    mgmt = written[dp._MGMT_PLAN_FULL_BLOB]
    assert list(mgmt.columns) == dp._MGMT_FULL_COLUMNS
    assert (mgmt["Business Unit"] == "B2C").all()
    # Unknown item 999999 (no PDH/RO match → no BU) is dropped.
    assert "999999" not in set(mgmt["Item"])
    # Base Plan pounds parsed from comma format.
    base = mgmt[mgmt["Forecast Type"] == "Base Plan"]
    assert set(base["Demand Plan Pounds"].round(0)) == {1944.0, 2000.0}


def test_pipeline_upserts_history_with_authored_cycle(monkeypatch):
    written = _patch_io(monkeypatch, _sources())
    res = dp.run_demand_plan_pipeline(_base_plan_bytes())
    assert res.ok

    hist = written[dp._HISTORY_TRACKER_BLOB]
    assert set(hist["Cycle"]) == {"C4", "C5"}            # C4 kept, C5 appended
    c5 = hist[hist["Cycle"] == "C5"]
    # Tracker text style: M/D/YYYY dates, trailing .0 stripped.
    assert "6/1/2026" in set(c5["Start of Month"])
    assert "1944" in set(c5["Demand Plan Pounds"])


def test_pipeline_idempotent_on_same_cycle(monkeypatch):
    """Re-running the same cycle replaces its rows, never duplicates them."""
    reads = _sources()
    written = _patch_io(monkeypatch, reads)
    first = dp.run_demand_plan_pipeline(_base_plan_bytes())
    assert first.ok
    # Feed the freshly-written tracker back in and re-run the same cycle.
    reads[dp._HISTORY_TRACKER_BLOB] = written[dp._HISTORY_TRACKER_BLOB]
    second = dp.run_demand_plan_pipeline(_base_plan_bytes())
    assert second.ok
    hist = written[dp._HISTORY_TRACKER_BLOB]
    assert set(hist["Cycle"]) == {"C4", "C5"}
    assert (hist["Cycle"] == "C5").sum() == first.mgmt_full_rows  # not doubled


def test_pipeline_rejects_multi_valued_cycle(monkeypatch):
    _patch_io(monkeypatch, _sources())
    df = pd.read_csv(io.BytesIO(_base_plan_bytes()), dtype=str)
    df.loc[0, "Cycle"] = "C6"                            # two distinct cycles
    res = dp.run_demand_plan_pipeline(df.to_csv(index=False).encode())
    assert not res.ok
    assert any("Cycle" in e for e in res.errors)


def test_pipeline_skips_write_when_seed_missing(monkeypatch):
    reads = _sources()
    del reads[dp._RO_SEED_BLOB]                          # RO_Seed absent
    written = _patch_io(monkeypatch, reads)
    res = dp.run_demand_plan_pipeline(_base_plan_bytes())
    assert not res.ok
    assert dp._MGMT_PLAN_FULL_BLOB not in written        # nothing written


def test_history_tracker_dedupes_identical_rows(monkeypatch):
    """Existing duplicate rows for other cycles collapse; no dupes in output."""
    dup = {
        "Start of Month": "5/1/2026", "Item": "310180", "Item Description": "x",
        "Party Site Number": "10036", "Demand Plan Pounds": "100",
        "Forecast Type": "Base Plan", "Business Unit": "B2C", "Cycle": "C4",
    }
    existing = pd.DataFrame([dup, dict(dup)])            # two identical C4 rows
    monkeypatch.setattr(dp, "read_csv",
                        lambda sec, path, read_csv_kwargs=None: (existing.copy(), "e"))

    mgmt_full = pd.DataFrame([{
        "Start of Month": "2026-06-01", "Item": "310180", "Item Description": "DG",
        "Party Site Number": "10036", "Demand Plan Pounds": "2000",
        "Forecast Type": "Base Plan", "Business Unit": "B2C",
    }])
    combined = dp._append_history_tracker(mgmt_full, "C5", dp._Log())

    assert int(combined.duplicated().sum()) == 0        # no duplicate rows
    assert len(combined) == 2                            # 1 deduped C4 + 1 new C5
    assert set(combined["Cycle"]) == {"C4", "C5"}
