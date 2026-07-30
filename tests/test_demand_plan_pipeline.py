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
    # The orchestrator archives the previous plan CSVs (read_bytes → archive_bytes)
    # before overwriting; simulate a prior copy existing so the archive path runs.
    monkeypatch.setattr(dp, "read_bytes", lambda sec, path: (b"prev", "e"))
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
    # Portfolio Major + Supply Format now travel on the file itself.
    assert {"Portfolio Major", "Supply Format"} <= set(mgmt.columns)
    row = mgmt[mgmt["Item"] == "310180"].iloc[0]
    assert row["Portfolio Major"] == "Butter" and row["Supply Format"] == "Carton"
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
    assert list(hist.columns) == dp._TRACKER_COLUMNS      # PMaj/SFmt + Cycle carried
    assert set(hist["Cycle"]) == {"C4", "C5"}            # C4 kept, C5 appended
    c5 = hist[hist["Cycle"] == "C5"]
    # Tracker text style: M/D/YYYY dates, trailing .0 stripped.
    assert "6/1/2026" in set(c5["Start of Month"])
    assert "1944" in set(c5["Demand Plan Pounds"])
    # New rows carry Portfolio Major / Supply Format from the enriched mgmt_full.
    assert set(c5["Portfolio Major"]) == {"Butter"}
    assert set(c5["Supply Format"]) == {"Carton"}


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


def test_backfill_adds_attribute_columns_and_archives(monkeypatch):
    """backfill_plan_attribute_columns enriches both files in place + archives first."""
    src = _sources()
    # Existing files on the LEGACY schema (no Portfolio Major / Supply Format).
    legacy_mgmt = pd.DataFrame({
        "Start of Month": ["2026-06-01"], "Item": ["310180"], "Item Description": ["DG"],
        "Party Site Number": ["10036"], "Demand Plan Pounds": ["2000"],
        "Forecast Type": ["Base Plan"], "Business Unit": ["B2C"],
    })
    legacy_trk = legacy_mgmt.assign(Cycle="C5")
    reads = {dp._PDH_BLOB: src[dp._PDH_BLOB], dp._RO_ITEMS_BLOB: src[dp._RO_ITEMS_BLOB],
             dp._MGMT_PLAN_FULL_BLOB: legacy_mgmt, dp._HISTORY_TRACKER_BLOB: legacy_trk}
    written = _patch_io(monkeypatch, reads)

    res = dp.backfill_plan_attribute_columns()
    assert res.ok, res.errors
    assert res.mgmt_full_archived and res.tracker_archived   # archived before write

    mgmt = written[dp._MGMT_PLAN_FULL_BLOB]
    assert list(mgmt.columns) == dp._MGMT_FULL_COLUMNS
    assert mgmt.iloc[0]["Portfolio Major"] == "Butter"
    assert mgmt.iloc[0]["Supply Format"] == "Carton"
    trk = written[dp._HISTORY_TRACKER_BLOB]
    assert list(trk.columns) == dp._TRACKER_COLUMNS
    assert trk.iloc[0]["Supply Format"] == "Carton"


def test_packaged_butter_forced_b2c_bulk_butter_stays_b2b(monkeypatch):
    """PDH tags packaged-butter SKUs B2B, but the pipeline forces Portfolio
    Minor 'Packaged Butter' to B2C so EVERY format flows in (not just the few
    PDH marks B2C).  Regression: 'Western Quarters only'.  'Bulk Butter'
    (ingredient) stays B2B and is dropped."""
    base = pd.DataFrame({
        "Start of Month": ["2026-06-01"] * 3,
        "Item": ["500001", "500002", "500003"],
        "Item Description": ["WhF Btr Elg 30-1lb", "Btr Gr AA 25kg", "DG Btr Chip"],
        "Value": ["10036"] * 3,
        "Total": ["1000", "2000", "3000"],
        "Corporate Group": ["ACME"] * 3,
        "month": ["2026-06-01"] * 3,
        "Cycle": ["C5"] * 3,
    }).to_csv(index=False).encode()

    reads = _sources()
    # PDH marks ALL three butter items B2B (as the live PDH does).
    reads[dp._PDH_BLOB] = pd.DataFrame({
        "Item No": ["500001", "500002", "500003", "310180"],
        "Business Unit": ["B2B", "B2B", "B2B", "B2C"],
        "Portfolio Major": ["Butter", "Butter", "Butter", "Butter"],
        "Portfolio Minor": ["Packaged Butter", "Bulk Butter", "Packaged Butter", "Qtr"],
        "Supply Format": ["Elgin Solid", "Bulk", "Chips", "Carton"],
    })
    # None of the butter items are in RO_Item_Master (so the NaN-fallback can't
    # rescue them) — only the Packaged Butter override can keep them.
    written = _patch_io(monkeypatch, reads)
    res = dp.run_demand_plan_pipeline(base)
    assert res.ok, res.errors

    mgmt = written[dp._MGMT_PLAN_FULL_BLOB]
    base_rows = mgmt[mgmt["Forecast Type"] == "Base Plan"]
    kept = set(base_rows["Item"])
    # Packaged Butter (Elgin Solid + Chips) kept despite PDH B2B.
    assert "500001" in kept and "500003" in kept
    assert set(base_rows[base_rows["Item"] == "500001"]["Supply Format"]) == {"Elgin Solid"}
    assert set(base_rows[base_rows["Item"] == "500003"]["Supply Format"]) == {"Chips"}
    # Bulk Butter dropped (stays B2B).
    assert "500002" not in kept
    assert (mgmt["Business Unit"] == "B2C").all()


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


def test_ro_input_to_long_preserves_negative_volume():
    """A negative RO monthly value (demand risk) survives as negative; a blank
    '-' still coerces to 0.  Regression for the '-'→'0' mangle that flipped a
    risk (-51697) into an opportunity (+51697)."""
    mcols = [f"Month {i}" for i in range(1, dp._N_MONTHS + 1)]
    row = {"Item #": "500", "Item Desc": "Risk"}
    for m in mcols:
        row[m] = "-"                         # blank placeholder → 0
    row["Month 1"] = "-51,697.97"            # genuine negative (comma fmt)
    row["Month 2"] = "1,000"                 # positive
    tri = pd.DataFrame([row])
    qm = pd.DataFrame({
        "Month Number": mcols,
        "Start of Month": [
            (pd.Timestamp("2026-04-01") + pd.DateOffset(months=i)).date()
            for i in range(len(mcols))],
    })
    out = dp._ro_input_to_long(tri, qm)
    nz = out[out["Demand Plan Pounds"] != 0].sort_values("Start of Month")
    vals = [round(float(v), 2) for v in nz["Demand Plan Pounds"]]
    assert vals == [-51697.97, 1000.0]       # negative preserved, blanks → 0


def test_negative_ro_volume_flows_into_mgmt_full(monkeypatch):
    """A negative-Lbs./yr RO opportunity (a demand risk) reaches qry_mgmt_plan_
    full as a NEGATIVE R&O row (previously mangled positive, then it would have
    been dropped by the >0 filter)."""
    reads = _sources()
    seed = reads[dp._RO_SEED_BLOB].copy()
    neg = dict(seed.iloc[0])
    neg.update({"Item #": "310181", "Item Desc": "Risk SKU", "Lbs./yr": "-3650"})
    reads[dp._RO_SEED_BLOB] = pd.concat(
        [seed, pd.DataFrame([neg])], ignore_index=True)
    pdh = reads[dp._PDH_BLOB].copy()
    reads[dp._PDH_BLOB] = pd.concat([pdh, pd.DataFrame([{
        "Item No": "310181", "Business Unit": "B2C", "Portfolio Major": "Butter",
        "Portfolio Minor": "Qtr", "Supply Format": "Carton"}])], ignore_index=True)
    written = _patch_io(monkeypatch, reads)

    res = dp.run_demand_plan_pipeline(_base_plan_bytes())
    assert res.ok, res.errors
    mgmt = written[dp._MGMT_PLAN_FULL_BLOB]
    ro = mgmt[(mgmt["Item"] == "310181") & (mgmt["Forecast Type"] == "R&O")]
    assert not ro.empty                                   # risk row kept
    assert (pd.to_numeric(ro["Demand Plan Pounds"]) < 0).any()   # and it's NEGATIVE
