"""Pre-flight validation of an uploaded Distribution_Tracker.csv.

The pipeline itself is forgiving (coerce-to-NaN, create-missing-blank), so
these tests pin the gate that stops a broken export from being published:
structural problems must BLOCK, an item-master gap must be acknowledgeable,
and anything the pipeline genuinely handles must NOT be flagged.
"""
import pandas as pd
import pytest

from data_sources import ro_input_preflight as pf


_GOOD_HEADER = (
    "Month,Format,Customer,Taxonomy,Brand,Item #,Item Desc,Probability,"
    "First Ship Date,Lbs./yr,PC$/yr,Slotting\n"
)
_GOOD_ROW = (
    "2026-06-01,HTST,Walmart,Retail,DG,340021,Milk Gallon,0.5,"
    "2027-01-01,1000000,50000,0\n"
)


def _csv(*rows: str, header: str = _GOOD_HEADER) -> bytes:
    return (header + "".join(rows)).encode("utf-8")


def _master(*items, **blank) -> pd.DataFrame:
    """A fully classified RO_Item_Master, unless a classifier is blanked out.

    ``_master(340021)`` → the item classifies cleanly.
    ``_master(340021, portfolio_minor="")`` → present but unclassified.
    """
    n = len(items)
    cols = {
        "Item #": list(items),
        "Item Desc": ["x"] * n,
        "Portfolio Major": [blank.get("portfolio_major", "HTST")] * n,
        "Portfolio Minor": [blank.get("portfolio_minor", "Gallon Jug")] * n,
        "Brand Category": [blank.get("brand_category", "Branded")] * n,
    }
    return pd.DataFrame(cols)


def _codes(result) -> set:
    return {f.code for f in result.findings}


# ── The happy path ───────────────────────────────────────────────────────────

def test_clean_file_is_runnable_and_silent():
    res = pf.check_distribution_tracker(_csv(_GOOD_ROW), item_master_df=_master(340021))
    assert res.ok_to_run
    assert res.clean
    assert res.findings == []
    assert res.row_count == 1
    assert res.months == ["2026-06-01"]


def test_older_header_names_are_accepted():
    header = (
        "Month,Format,Customer,Taxonomy,Brand,Item #,Item Desc,Probability,"
        "First Ship Date,Anticipated Annual Lbs. Vol,Annual PC $,"
        "Total Anticipated Slotting Costs\n"
    )
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW, header=header), item_master_df=_master(340021),
    )
    assert res.clean, _codes(res)


@pytest.mark.parametrize("prob", ["0.5", "50%", "50", "0", "1"])
def test_every_probability_form_the_pipeline_accepts_passes(prob):
    row = _GOOD_ROW.replace(",0.5,", f",{prob},")
    res = pf.check_distribution_tracker(_csv(row), item_master_df=_master(340021))
    assert "BAD_PROBABILITY" not in _codes(res), prob


@pytest.mark.parametrize("value", ["1,250,000", "$1250000", ""])
def test_formats_the_pipeline_cleans_are_not_flagged(value):
    row = _GOOD_ROW.replace(",1000000,", f",\"{value}\",")
    res = pf.check_distribution_tracker(_csv(row), item_master_df=_master(340021))
    assert "INVALID_VOLUME" not in _codes(res), value


# ── Blocking: structure ──────────────────────────────────────────────────────

def test_missing_month_column_blocks_with_excel_steps():
    header = _GOOD_HEADER.replace("Month,", "")
    row = _GOOD_ROW.replace("2026-06-01,", "")
    res = pf.check_distribution_tracker(_csv(row, header=header),
                                        item_master_df=_master(340021))
    assert not res.ok_to_run
    finding = next(f for f in res.findings if f.code == "MISSING_MONTH_COLUMN")
    assert finding.fix_where == pf.FIX_IN_EXCEL
    assert finding.fix_steps


@pytest.mark.parametrize("month,reason", [
    ("2026-06-15", "mid-month"),
    ("", "blank"),
    ("June", "not a date"),
])
def test_bad_month_values_block_and_name_the_row(month, reason):
    row = _GOOD_ROW.replace("2026-06-01,", f"{month},")
    res = pf.check_distribution_tracker(_csv(row), item_master_df=_master(340021))
    assert not res.ok_to_run, reason
    finding = next(f for f in res.findings if f.code == "BAD_MONTH_VALUE")
    # Row 2 = first data row (row 1 is the header).
    assert finding.cells.iloc[0]["Excel row"] == 2


@pytest.mark.parametrize("col,cell", [
    ("Format", "HTST,"), ("Customer", "Walmart,"), ("Item #", "340021,"),
])
def test_missing_critical_column_blocks(col, cell):
    header = _GOOD_HEADER.replace(f"{col},", "")
    row = _GOOD_ROW.replace(cell, "", 1)
    res = pf.check_distribution_tracker(_csv(row, header=header),
                                        item_master_df=_master(340021))
    assert not res.ok_to_run, col
    finding = next(f for f in res.findings if f.code == "MISSING_COLUMNS")
    assert col in set(finding.cells["Missing column"])


@pytest.mark.parametrize("col,cell", [
    ("Taxonomy", "Retail,"), ("Item Desc", "Milk Gallon,"),
])
def test_missing_optional_column_is_acknowledgeable_not_blocking(col, cell):
    """Totals stay correct — the field just comes through blank."""
    header = _GOOD_HEADER.replace(f"{col},", "")
    row = _GOOD_ROW.replace(cell, "", 1)
    res = pf.check_distribution_tracker(_csv(row, header=header),
                                        item_master_df=_master(340021))
    assert res.ok_to_run, col
    finding = next(f for f in res.findings if f.code == "MISSING_OPTIONAL_COLUMNS")
    assert finding.severity == pf.SEVERITY_ACK


def test_empty_file_blocks():
    res = pf.check_distribution_tracker(_GOOD_HEADER.encode("utf-8"))
    assert not res.ok_to_run
    assert "NO_ROWS" in _codes(res)


def test_unreadable_bytes_block_without_raising():
    res = pf.check_distribution_tracker(b"\x00\x01\x02 not a csv \xff\xfe")
    assert not res.ok_to_run


# ── Blocking: the silent-zero cases ──────────────────────────────────────────

@pytest.mark.parametrize("bad", ["#N/A", "#REF!", "NA", "n/a", "#VALUE!", "TBD"])
def test_excel_errors_in_the_volume_column_block(bad):
    row = _GOOD_ROW.replace(",1000000,", f",{bad},")
    res = pf.check_distribution_tracker(_csv(row), item_master_df=_master(340021))
    assert not res.ok_to_run, bad
    finding = next(f for f in res.findings if f.code == "INVALID_VOLUME")
    assert finding.cells.iloc[0]["Column"] == "Lbs./yr"
    assert finding.cells.iloc[0]["What your file has"] == bad


@pytest.mark.parametrize("col,before,after", [
    ("PC$/yr", ",50000,", ",#N/A,"),
    ("Slotting", ",0\n", ",#REF!\n"),
])
def test_broken_dollar_cells_do_not_block(col, before, after):
    """No volume rides on these columns, so they must not stop a run."""
    row = _GOOD_ROW.replace(before, after)
    assert row != _GOOD_ROW, "the fixture row changed shape"
    res = pf.check_distribution_tracker(_csv(row), item_master_df=_master(340021))
    assert res.ok_to_run, col
    assert "INVALID_MONEY" in _codes(res)
    assert "INVALID_VOLUME" not in _codes(res)


def test_invalid_volume_reports_the_right_excel_row():
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW, _GOOD_ROW.replace(",1000000,", ",#N/A,"), _GOOD_ROW),
        item_master_df=_master(340021),
    )
    finding = next(f for f in res.findings if f.code == "INVALID_VOLUME")
    assert list(finding.cells["Excel row"]) == [3]


@pytest.mark.parametrize("prob", ["", "high", "150%", "-0.2"])
def test_unreadable_probability_blocks(prob):
    row = _GOOD_ROW.replace(",0.5,", f",{prob},")
    res = pf.check_distribution_tracker(_csv(row), item_master_df=_master(340021))
    assert not res.ok_to_run, prob
    assert "BAD_PROBABILITY" in _codes(res)


@pytest.mark.parametrize("ship", ["", "soon"])
def test_unreadable_ship_date_blocks(ship):
    row = _GOOD_ROW.replace(",2027-01-01,", f",{ship},")
    res = pf.check_distribution_tracker(_csv(row), item_master_df=_master(340021))
    assert not res.ok_to_run, ship
    assert "BAD_SHIP_DATE" in _codes(res)


# ── Acknowledgeable: item-master linkage ─────────────────────────────────────

@pytest.mark.parametrize("blanked,field", [
    ({"portfolio_major": ""}, "Portfolio Major"),
    ({"portfolio_minor": "  "}, "Portfolio Minor"),
    ({"brand_category": ""}, "Brand Category"),
])
def test_item_present_but_unclassified_says_which_field_is_blank(blanked, field):
    """The second way linkage fails: the row exists, the classifier doesn't."""
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW), item_master_df=_master(340021, **blanked),
    )
    assert res.ok_to_run                       # not a volume problem
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_GAPS")
    row = finding.cells.iloc[0]
    assert row["Item #"] == "340021"
    assert field in row["Why it will fail"], row["Why it will fail"]
    assert field in row["What to fill in"]
    assert "In RO_Item_Master.csv" in row["Why it will fail"]


def test_a_master_missing_a_classifier_column_entirely_is_reported():
    master = _master(340021).drop(columns=["Portfolio Minor"])
    res = pf.check_distribution_tracker(_csv(_GOOD_ROW), item_master_df=master)
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_GAPS")
    assert "Portfolio Minor" in finding.cells.iloc[0]["Why it will fail"]


def test_the_two_linkage_causes_are_distinguished_per_item():
    """One list, one reason per row — the planner shouldn't have to correlate."""
    rows = _GOOD_ROW + _GOOD_ROW.replace(",340021,", ",111111,")
    master = pd.concat([
        _master(340021, portfolio_minor=""),    # present, unclassified
    ], ignore_index=True)                       # 111111 absent entirely
    res = pf.check_distribution_tracker(_csv(rows), item_master_df=master)
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_GAPS")
    why = dict(zip(finding.cells["Item #"], finding.cells["Why it will fail"]))
    assert why["340021"].startswith("In RO_Item_Master.csv")
    assert why["111111"] == "Not in RO_Item_Master.csv"
    assert "not in the file" in finding.title and "unclassified" in finding.title


def test_unlinked_item_is_acknowledgeable_not_blocking():
    res = pf.check_distribution_tracker(_csv(_GOOD_ROW), item_master_df=_master(999999))
    assert res.ok_to_run            # structurally fine — planner may proceed
    assert not res.clean            # but it needs an explicit acknowledgement
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_GAPS")
    assert finding.severity == pf.SEVERITY_ACK
    assert finding.fix_where == pf.FIX_IN_FABRIC
    assert finding.fabric_path.endswith("RO_Item_Master.csv")
    assert finding.cells.iloc[0]["Item #"] == "340021"
    assert finding.cells.iloc[0]["Rows in your file"] == 1


def test_item_numbers_match_despite_leading_zeros_and_formatting():
    row = _GOOD_ROW.replace(",340021,", ",0340021,")
    res = pf.check_distribution_tracker(_csv(row), item_master_df=_master("340021"))
    assert "ITEM_MASTER_GAPS" not in _codes(res)


def test_missing_item_master_is_reported_not_silently_passed():
    res = pf.check_distribution_tracker(_csv(_GOOD_ROW), item_master_df=None)
    assert res.ok_to_run
    assert "ITEM_MASTER_UNAVAILABLE" in _codes(res)


def test_unlinked_items_are_deduplicated_and_counted():
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW, _GOOD_ROW, _GOOD_ROW.replace(",340021,", ",111111,")),
        item_master_df=_master(999999),
    )
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_GAPS")
    counts = dict(zip(finding.cells["Item #"], finding.cells["Rows in your file"]))
    assert counts == {"340021": 2, "111111": 1}


# ── Informational ────────────────────────────────────────────────────────────

def test_duplicate_rows_are_not_reported_at_all():
    """The pipeline sums duplicates by design, so flagging them is pure noise."""
    res = pf.check_distribution_tracker(_csv(_GOOD_ROW, _GOOD_ROW),
                                        item_master_df=_master(340021))
    assert res.clean, _codes(res)


def test_every_finding_carries_actionable_guidance():
    """No finding may say what is wrong without saying what to do."""
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW.replace(",1000000,", ",#N/A,")), item_master_df=_master(999999),
    )
    for f in res.findings:
        if f.severity == pf.SEVERITY_INFO:
            continue
        assert f.title and f.means, f.code
        assert f.fix_where in (pf.FIX_IN_EXCEL, pf.FIX_IN_FABRIC), f.code
        assert f.fix_steps, f.code
