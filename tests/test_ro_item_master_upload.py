"""Validation of a replacement RO_Item_Master.csv.

This upload overwrites a shared reference file that every downstream
classification reads, and a mistake stays invisible until the next report
shows items under no portfolio row.  So the checks lean on the two things
that actually break it: a missing key column, and silently losing items the
current file already classifies.
"""
from __future__ import annotations

import pandas as pd
import pytest

from data_sources import ro_input_preflight as pf


_HEADER = "Item #,Item Desc,Brand Category,Portfolio Major,Portfolio Minor,Supply Format\n"


def _row(item="340021", desc="Milk Gallon", brand="Branded",
         pmaj="HTST", pminor="Gallon Jug", sfmt="Jug") -> str:
    return f"{item},{desc},{brand},{pmaj},{pminor},{sfmt}\n"


def _csv(*rows: str, header: str = _HEADER) -> bytes:
    return (header + "".join(rows)).encode("utf-8")


def _codes(result) -> set:
    return {f.code for f in result.findings}


def _current(*items) -> pd.DataFrame:
    return pd.DataFrame({
        "Item #": list(items),
        "Portfolio Major": ["HTST"] * len(items),
        "Portfolio Minor": ["Jug"] * len(items),
    })


# ── The happy path ───────────────────────────────────────────────────────────

def test_a_clean_replacement_is_accepted_silently():
    res = pf.check_ro_item_master(_csv(_row()), current_master_df=_current("340021"))
    assert res.ok_to_run and res.clean, _codes(res)
    assert res.row_count == 1


def test_adding_items_is_not_a_warning():
    res = pf.check_ro_item_master(
        _csv(_row("340021"), _row("111111")), current_master_df=_current("340021"),
    )
    assert res.clean, _codes(res)


def test_the_first_ever_upload_needs_no_current_file():
    assert pf.check_ro_item_master(_csv(_row())).clean


# ── Blocking ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("col", ["Item #", "Portfolio Major", "Portfolio Minor"])
def test_a_missing_key_column_blocks(col):
    header = _HEADER.replace(f"{col},", "").replace(f",{col}", "")
    res = pf.check_ro_item_master(_csv(_row(), header=header))
    assert not res.ok_to_run, col
    finding = next(f for f in res.findings if f.code == "MISSING_COLUMNS")
    assert col in list(finding.cells["Missing column"])
    assert finding.fix_where == pf.FIX_IN_EXCEL


def test_an_empty_file_blocks():
    res = pf.check_ro_item_master(_HEADER.encode())
    assert not res.ok_to_run
    assert "NO_ROWS" in _codes(res)


def test_unreadable_bytes_block_without_raising():
    res = pf.check_ro_item_master(b"\x00\x01 not a csv \xff")
    assert not res.ok_to_run


def test_a_blocking_file_skips_the_later_checks():
    """No point reporting duplicates in a file that has no Item # column."""
    header = _HEADER.replace("Item #,", "")
    res = pf.check_ro_item_master(_csv(_row(), header=header))
    assert _codes(res) == {"MISSING_COLUMNS"}


# ── Acknowledgeable ─────────────────────────────────────────────────────────

def test_losing_items_warns_and_names_them():
    """The classic mistake: editing a filtered view and uploading that."""
    res = pf.check_ro_item_master(
        _csv(_row("340021")), current_master_df=_current("340021", "999999"),
    )
    assert res.ok_to_run and not res.clean
    finding = next(f for f in res.findings if f.code == "ITEMS_DROPPED")
    assert list(finding.cells["Item # no longer present"]) == ["999999"]
    assert "filtered view" in " ".join(finding.fix_steps)


def test_duplicate_items_warn_that_the_last_row_wins():
    res = pf.check_ro_item_master(_csv(_row("340021"), _row("340021", pmaj="ESL")))
    finding = next(f for f in res.findings if f.code == "DUPLICATE_ITEMS")
    assert list(finding.cells["Item #"]) == ["340021"]
    assert "last" in finding.means


def test_a_blank_portfolio_field_is_reported_with_its_row():
    res = pf.check_ro_item_master(_csv(_row(), _row("111111", pminor="")))
    finding = next(f for f in res.findings if f.code == "UNCLASSIFIED_ROWS")
    row = finding.cells.iloc[0]
    assert row["Excel row"] == 3          # header is row 1
    assert row["Item #"] == "111111"
    assert row["Blank field(s)"] == "Portfolio Minor"


def test_a_row_with_no_item_number_is_reported_not_fatal():
    res = pf.check_ro_item_master(_csv(_row(), _row(item="")))
    assert res.ok_to_run
    assert "BLANK_ITEM_NUMBER" in _codes(res)


def test_missing_optional_columns_do_not_block():
    header = "Item #,Portfolio Major,Portfolio Minor\n"
    res = pf.check_ro_item_master(("Item #,Portfolio Major,Portfolio Minor\n"
                                   "340021,HTST,Jug\n").encode())
    assert res.ok_to_run
    finding = next(f for f in res.findings if f.code == "MISSING_OPTIONAL_COLUMNS")
    assert finding.severity == pf.SEVERITY_ACK


def test_item_numbers_compare_across_dtypes_when_detecting_loss():
    """340021 and 340021.0 are the same item — not a dropped one."""
    current = pd.DataFrame({"Item #": [340021.0], "Portfolio Major": ["HTST"],
                            "Portfolio Minor": ["Jug"]})
    res = pf.check_ro_item_master(_csv(_row("340021")), current_master_df=current)
    assert "ITEMS_DROPPED" not in _codes(res)


def test_every_finding_says_what_to_do():
    res = pf.check_ro_item_master(
        _csv(_row("340021"), _row("340021"), _row("111111", pmaj="")),
        current_master_df=_current("340021", "999999"),
    )
    for f in res.findings:
        if f.severity == pf.SEVERITY_INFO:
            continue
        assert f.title and f.means and f.fix_steps, f.code
