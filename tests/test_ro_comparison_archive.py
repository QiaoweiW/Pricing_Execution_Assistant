"""Unit tests for the RO Pipeline Review archive helpers in ro_comparison.

Pure — the Fabric I/O primitives (archive_bytes / list_files) are monkeypatched,
so no network or Streamlit is involved.
"""

import pandas as pd
import pytest

import data_sources.ro_comparison as roc
from data_sources.fabric_lakehouse_io import LakehouseFile, LakehouseIOError


def test_save_pipeline_review_snapshot(monkeypatch):
    captured = {}

    def fake_archive(section, adir, fname, payload, timestamp=None):
        captured.update(
            section=section, adir=adir, fname=fname,
            payload=payload.decode("utf-8"), ts=timestamp)
        return f"{adir}/RO_Pipeline_Review_{timestamp}.csv"

    monkeypatch.setattr(roc, "archive_bytes", fake_archive)
    df = pd.DataFrame({
        "Program": ["Acme — 1 Widget"],
        "First Ship Date": pd.to_datetime(["2026-08-01"]),
        "Action": ["Chase"],
    })
    path = roc.save_pipeline_review_snapshot(df, timestamp="20260721_010203")

    assert path.endswith("RO_Pipeline_Review_20260721_010203.csv")
    assert captured["adir"] == "RO Tracking/RO Pipeline Review Archive"
    assert captured["section"] == roc._SECRETS_SECTION
    assert captured["fname"] == "RO_Pipeline_Review.csv"
    # Datetime columns serialise as YYYY-MM-DD (no HH:MM:SS noise) in the CSV.
    lines = captured["payload"].splitlines()
    assert "First Ship Date" in lines[0]
    assert "2026-08-01" in lines[1] and "00:00:00" not in lines[1]


def test_save_pipeline_review_snapshot_wraps_io_error(monkeypatch):
    def boom(*a, **k):
        raise LakehouseIOError("write failed")

    monkeypatch.setattr(roc, "archive_bytes", boom)
    with pytest.raises(roc.RoComparisonError):
        roc.save_pipeline_review_snapshot(
            pd.DataFrame({"x": [1]}), timestamp="t")


def test_list_pipeline_review_snapshots_sorted_newest_first(monkeypatch):
    def _f(name):
        return LakehouseFile(name=name, full_path=f"x/{name}", size=1,
                             etag=None, last_modified=None)

    files = [
        _f("RO_Pipeline_Review_20260101_000000.csv"),
        _f("RO_Pipeline_Review_20260301_000000.csv"),
        _f("RO_Pipeline_Review_20260201_000000.csv"),
    ]
    monkeypatch.setattr(roc, "list_files", lambda *a, **k: list(files))
    out = roc.list_pipeline_review_snapshots()
    assert [f.name for f in out] == [
        "RO_Pipeline_Review_20260301_000000.csv",
        "RO_Pipeline_Review_20260201_000000.csv",
        "RO_Pipeline_Review_20260101_000000.csv",
    ]


def test_list_pipeline_review_snapshots_wraps_io_error(monkeypatch):
    def boom(*a, **k):
        raise LakehouseIOError("list failed")

    monkeypatch.setattr(roc, "list_files", boom)
    with pytest.raises(roc.RoComparisonError):
        roc.list_pipeline_review_snapshots()
