"""Unit tests for the 'withdraw a base-plan upload' tool.

Withdrawing has two halves that must not be confused: the history tracker is
MULTI-cycle (drop only the chosen cycles' rows) while the four derived files
are SINGLE-cycle snapshots of the latest run (delete outright).  Every file is
archived first so the operation is recoverable.

Fabric I/O is stubbed — no lakehouse, no Streamlit.
"""
from __future__ import annotations

import pandas as pd
import pytest

import data_sources.demand_plan_pipeline as dpp


# ── Fabric stub ──────────────────────────────────────────────────────────────

class _Fabric:
    """In-memory stand-in for the OneLake blob calls withdraw_cycles makes."""

    def __init__(self, tracker: pd.DataFrame | None, present: set[str]):
        self.tracker = tracker
        self.present = set(present)
        self.written: dict[str, pd.DataFrame] = {}
        self.deleted: list[str] = []
        self.archived: list[str] = []

    def read_csv(self, _section, blob, **_kw):
        if blob == dpp._HISTORY_TRACKER_BLOB:
            return (self.tracker, "etag")
        return (None, None)

    def read_bytes(self, _section, blob):
        return (b"payload", "etag") if blob in self.present else (None, None)

    def archive_bytes(self, _section, archive_dir, filename, _payload):
        path = f"{archive_dir}/{filename}"
        self.archived.append(path)
        return path

    def write_csv(self, _section, blob, df, **_kw):
        self.written[blob] = df

    def delete_blob(self, _section, blob):
        if blob in self.present:
            self.present.discard(blob)
            self.deleted.append(blob)
            return True
        return False


def _tracker(*rows: tuple[str, str]) -> pd.DataFrame:
    """Minimal tracker frame: (Cycle, Start of Month) pairs."""
    return pd.DataFrame(
        [{"Cycle": c, "Start of Month": m, "Item": "342065",
          "Demand Plan Pounds": "100"} for c, m in rows]
    )


_ALL_SNAPSHOTS = {blob for blob, _dir in dpp._WITHDRAW_SNAPSHOTS}
# What read_bytes() finds on the lakehouse by default: the four snapshots plus
# the tracker itself (which is archived before it is rewritten, not deleted).
_ALL_BLOBS = _ALL_SNAPSHOTS | {dpp._HISTORY_TRACKER_BLOB}


@pytest.fixture
def fabric(monkeypatch):
    def _install(tracker, present=_ALL_BLOBS):
        fk = _Fabric(tracker, present)
        for name in ("read_csv", "read_bytes", "archive_bytes", "write_csv", "delete_blob"):
            monkeypatch.setattr(dpp, name, getattr(fk, name))
        return fk
    return _install


# ── Tracker half: only the chosen cycles go ──────────────────────────────────

def test_withdraw_removes_only_the_chosen_cycle(fabric):
    fk = fabric(_tracker(("C6", "9/1/2026"), ("C6", "10/1/2026"), ("C5", "9/1/2026")))
    res = dpp.withdraw_cycles(["C6"])

    assert res.ok
    assert res.rows_removed == 2
    assert res.rows_remaining == 1
    remaining = fk.written[dpp._HISTORY_TRACKER_BLOB]
    assert remaining["Cycle"].tolist() == ["C5"]


def test_withdraw_accepts_several_cycles(fabric):
    fk = fabric(_tracker(("C6", "9/1/2026"), ("C5", "9/1/2026"), ("C1", "3/1/2026")))
    res = dpp.withdraw_cycles(["C6", "C5"])

    assert res.rows_removed == 2
    assert fk.written[dpp._HISTORY_TRACKER_BLOB]["Cycle"].tolist() == ["C1"]


def test_cycle_labels_are_matched_after_trimming(fabric):
    fk = fabric(_tracker((" C6 ", "9/1/2026"), ("C5", "9/1/2026")))
    res = dpp.withdraw_cycles(["C6"])

    assert res.rows_removed == 1
    assert fk.written[dpp._HISTORY_TRACKER_BLOB]["Cycle"].tolist() == ["C5"]


def test_unknown_cycle_leaves_the_tracker_untouched(fabric):
    fk = fabric(_tracker(("C6", "9/1/2026")))
    res = dpp.withdraw_cycles(["C99"])

    assert res.ok
    assert res.rows_removed == 0
    assert dpp._HISTORY_TRACKER_BLOB not in fk.written     # no pointless rewrite
    assert any("No tracker rows matched" in w for w in res.warnings)


# ── Snapshot half: all four go, whichever cycle was picked ───────────────────

def test_all_four_snapshots_are_deleted(fabric):
    fk = fabric(_tracker(("C6", "9/1/2026")))
    res = dpp.withdraw_cycles(["C6"])

    assert set(fk.deleted) == _ALL_SNAPSHOTS
    assert len(res.files_deleted) == 4
    assert "ibp_base_plan_current.csv" in res.files_deleted


def test_snapshots_are_cleared_even_for_an_older_cycle(fabric):
    # They describe the LATEST run only, so they are stale regardless.
    fk = fabric(_tracker(("C6", "9/1/2026"), ("C5", "9/1/2026")))
    dpp.withdraw_cycles(["C5"])
    assert set(fk.deleted) == _ALL_SNAPSHOTS


def test_absent_snapshots_are_reported_not_failed(fabric):
    fk = fabric(_tracker(("C6", "9/1/2026")),
                present={dpp._HISTORY_TRACKER_BLOB, dpp._MGMT_PLAN_FULL_BLOB})
    res = dpp.withdraw_cycles(["C6"])

    assert res.ok
    assert res.files_deleted == ("qry_mgmt_plan_full.csv",)
    assert len(res.files_absent) == 3


def test_tbl_ro_input_is_never_touched(fabric):
    # It comes from RO_Seed, not from the base-plan upload.
    fk = fabric(_tracker(("C6", "9/1/2026")))
    dpp.withdraw_cycles(["C6"])
    assert dpp._TBL_RO_INPUT_BLOB not in fk.deleted
    assert dpp._TBL_RO_INPUT_BLOB not in fk.written


# ── Recoverability ───────────────────────────────────────────────────────────

def test_everything_is_archived_before_it_changes(fabric):
    fk = fabric(_tracker(("C6", "9/1/2026")))
    dpp.withdraw_cycles(["C6"])

    archived = " ".join(fk.archived)
    assert "qry_mgmt_plan_history_tracker.csv" in archived
    assert "qry_mgmt_plan_full.csv" in archived
    assert "qry_demand_item_customer_detail.csv" in archived
    assert "qry_total_item_level_demand.csv" in archived
    # The base plan archives beside its own uploads, not with the plan files.
    assert f"{dpp._BASE_PLAN_ARCHIVE_DIR}/ibp_base_plan_current.csv" in fk.archived


# ── Guard rails ──────────────────────────────────────────────────────────────

def test_empty_selection_is_rejected(fabric):
    fk = fabric(_tracker(("C6", "9/1/2026")))
    res = dpp.withdraw_cycles([])

    assert not res.ok
    assert not fk.deleted and not fk.written
    assert any("at least one cycle" in e for e in res.errors)


def test_blank_labels_are_ignored(fabric):
    fabric(_tracker(("C6", "9/1/2026")))
    assert not dpp.withdraw_cycles(["", "   "]).ok


def test_missing_cycle_column_is_an_error_not_a_wipe(fabric):
    fk = fabric(pd.DataFrame({"Item": ["342065"]}))
    res = dpp.withdraw_cycles(["C6"])

    assert not res.ok
    assert not fk.deleted and not fk.written        # nothing destroyed


def test_io_failure_is_reported_not_raised(fabric):
    fk = fabric(_tracker(("C6", "9/1/2026")))

    def boom(*_a, **_k):
        raise dpp.LakehouseIOError("network down")

    fk.write_csv = boom
    import data_sources.demand_plan_pipeline as m
    m.write_csv = boom

    res = dpp.withdraw_cycles(["C6"])
    assert not res.ok
    assert any("network down" in e for e in res.errors)


# ── Cycle picker ─────────────────────────────────────────────────────────────

def test_picker_lists_cycles_oldest_to_newest(fabric):
    # The live tracker stores Start of Month as a MIX of M/D/YYYY text and Excel
    # day-serials (46054 = 2026-02-01, 46235 = 2026-08-01).  Both must parse, or
    # the serial rows mis-date and the horizon order scrambles — the picker must
    # go through demand_plan_comparison's tolerant parser, not a bare
    # to_datetime.  Labels also wrap at the fiscal year, so C12 is the OLDEST.
    fabric(_tracker(
        ("C6", "46235"), ("C1", "3/1/2026"), ("C12", "46054"),
    ))
    assert dpp.list_history_tracker_cycles() == ["C12", "C1", "C6"]


def test_picker_is_empty_when_the_tracker_is_missing(fabric):
    fabric(None)
    assert dpp.list_history_tracker_cycles() == []
