"""Pre-flight validation of an uploaded Distribution_Tracker.csv.

The pipeline itself is forgiving (coerce-to-NaN, create-missing-blank), so
these tests pin the gate that stops a broken export from being published:
structural problems must BLOCK, an item-master gap must be acknowledgeable,
and anything the pipeline genuinely handles must NOT be flagged.
"""
import pandas as pd
import pytest

from data_sources import ro_input_preflight as pf
from data_sources.demand_plan_comparison import build_item_dim_frame_cascade


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


def _master_frame(*items, **blank) -> pd.DataFrame:
    """An RO_Item_Master-shaped frame, optionally with a classifier blanked."""
    n = len(items)
    return pd.DataFrame({
        "Item #": list(items),
        "Item Desc": ["x"] * n,
        "Portfolio Major": [blank.get("portfolio_major", "HTST")] * n,
        "Portfolio Minor": [blank.get("portfolio_minor", "Gallon Jug")] * n,
        "Brand Category": [blank.get("brand_category", "Branded")] * n,
    })


def _master(*items, **blank) -> pd.DataFrame:
    """The dim cascade with RO_Item_Master only — the pre-PDH baseline.

    Built through the REAL cascade rather than hand-rolled, so these tests
    exercise the same key derivation and column resolution the app uses.
    """
    return build_item_dim_frame_cascade(None, _master_frame(*items, **blank))


def _dims(pdh: pd.DataFrame = None, master: pd.DataFrame = None) -> pd.DataFrame:
    """The cascade over both sources, as the page builds it."""
    return build_item_dim_frame_cascade(pdh, master)


def _pdh_frame(*items) -> pd.DataFrame:
    """A qry_pdh.csv-shaped frame (its own column names)."""
    n = len(items)
    return pd.DataFrame({
        "Item No": list(items),
        "Item Description": ["p"] * n,
        "Portfolio Major": ["ESL"] * n,
        "Portfolio Minor": ["Small Carton"] * n,
    })


def _codes(result) -> set:
    return {f.code for f in result.findings}


# ── The happy path ───────────────────────────────────────────────────────────

def test_clean_file_is_runnable_and_silent():
    res = pf.check_distribution_tracker(_csv(_GOOD_ROW), item_dims=_master(340021))
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
        _csv(_GOOD_ROW, header=header), item_dims=_master(340021),
    )
    assert res.clean, _codes(res)


@pytest.mark.parametrize("prob", ["0.5", "50%", "50", "0", "1"])
def test_every_probability_form_the_pipeline_accepts_passes(prob):
    row = _GOOD_ROW.replace(",0.5,", f",{prob},")
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master(340021))
    assert "BAD_PROBABILITY" not in _codes(res), prob


@pytest.mark.parametrize("value", ["1,250,000", "$1250000", ""])
def test_formats_the_pipeline_cleans_are_not_flagged(value):
    row = _GOOD_ROW.replace(",1000000,", f",\"{value}\",")
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master(340021))
    assert "INVALID_VOLUME" not in _codes(res), value


# ── Blocking: structure ──────────────────────────────────────────────────────

def test_missing_month_column_blocks_with_excel_steps():
    header = _GOOD_HEADER.replace("Month,", "")
    row = _GOOD_ROW.replace("2026-06-01,", "")
    res = pf.check_distribution_tracker(_csv(row, header=header),
                                        item_dims=_master(340021))
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
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master(340021))
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
                                        item_dims=_master(340021))
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
                                        item_dims=_master(340021))
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
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master(340021))
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
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master(340021))
    assert res.ok_to_run, col
    assert "INVALID_MONEY" in _codes(res)
    assert "INVALID_VOLUME" not in _codes(res)


def test_invalid_volume_reports_the_right_excel_row():
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW, _GOOD_ROW.replace(",1000000,", ",#N/A,"), _GOOD_ROW),
        item_dims=_master(340021),
    )
    finding = next(f for f in res.findings if f.code == "INVALID_VOLUME")
    assert list(finding.cells["Excel row"]) == [3]


@pytest.mark.parametrize("prob", ["", "high", "150%", "-0.2"])
def test_unreadable_probability_blocks(prob):
    row = _GOOD_ROW.replace(",0.5,", f",{prob},")
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master(340021))
    assert not res.ok_to_run, prob
    assert "BAD_PROBABILITY" in _codes(res)


@pytest.mark.parametrize("ship", ["", "soon"])
def test_unreadable_ship_date_blocks(ship):
    row = _GOOD_ROW.replace(",2027-01-01,", f",{ship},")
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master(340021))
    assert not res.ok_to_run, ship
    assert "BAD_SHIP_DATE" in _codes(res)


# ── Acknowledgeable: item-master linkage ─────────────────────────────────────

@pytest.mark.parametrize("blanked,field", [
    ({"portfolio_major": ""}, "Portfolio Major"),
    ({"portfolio_minor": "  "}, "Portfolio Minor"),
])
def test_item_present_but_unclassified_says_which_field_is_blank(blanked, field):
    """The second way linkage fails: the row exists, the classifier doesn't."""
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW), item_dims=_master(340021, **blanked),
    )
    assert res.ok_to_run                       # not a volume problem
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_GAPS")
    row = finding.cells.iloc[0]
    assert row["Item #"] == "340021"
    assert field in row["Why it will fail"], row["Why it will fail"]
    assert field in row["What to fill in"]
    assert "blank in both files" in row["Why it will fail"]


def test_a_source_missing_a_portfolio_column_entirely_is_reported():
    """An absent column classifies nothing, same as a blank cell."""
    master = _master_frame(340021).drop(columns=["Portfolio Minor"])
    res = pf.check_distribution_tracker(_csv(_GOOD_ROW), item_dims=_dims(master=master))
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_GAPS")
    assert "Portfolio Minor" in finding.cells.iloc[0]["Why it will fail"]


def test_the_two_causes_are_distinguished_per_item():
    """One list, one reason per row — the planner shouldn't have to correlate."""
    rows = _GOOD_ROW + _GOOD_ROW.replace(",340021,", ",111111,")
    res = pf.check_distribution_tracker(
        _csv(rows),
        item_dims=_master(340021, portfolio_minor=""),   # 111111 absent entirely
    )
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_GAPS")
    why = dict(zip(finding.cells["Item #"], finding.cells["Why it will fail"]))
    assert why["340021"].startswith("Known, but")
    assert why["111111"] == "Not in qry_pdh.csv or RO_Item_Master.csv"
    assert "not in either file" in finding.title
    assert "known but unclassified" in finding.title


def test_unlinked_item_is_acknowledgeable_not_blocking():
    res = pf.check_distribution_tracker(_csv(_GOOD_ROW), item_dims=_master(999999))
    assert res.ok_to_run            # structurally fine — planner may proceed
    assert not res.clean            # but it needs an explicit acknowledgement
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_GAPS")
    assert finding.severity == pf.SEVERITY_ACK
    assert finding.fix_where == pf.FIX_IN_FABRIC
    assert finding.fabric_path.endswith("RO_Item_Master.csv")
    assert finding.cells.iloc[0]["Item #"] == "340021"
    assert finding.cells.iloc[0]["Rows in your file"] == 1


def test_item_numbers_match_across_int_float_and_text():
    """"340021", 340021 and 340021.0 are one item to the cascade and to us."""
    for value in ("340021", 340021, 340021.0):
        res = pf.check_distribution_tracker(_csv(_GOOD_ROW), item_dims=_master(value))
        assert "ITEM_MASTER_GAPS" not in _codes(res), value


def test_a_leading_zero_item_number_is_reported():
    """It genuinely will not classify: the dim files key on 340021, not 0340021.

    The old check stripped leading zeros and so hid this; the cascade does not,
    which means downstream the item lands under no portfolio row.  Reporting it
    is the point.
    """
    row = _GOOD_ROW.replace(",340021,", ",0340021,")
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master("340021"))
    assert "ITEM_MASTER_GAPS" in _codes(res)


def test_missing_item_master_is_reported_not_silently_passed():
    res = pf.check_distribution_tracker(_csv(_GOOD_ROW), item_dims=None)
    assert res.ok_to_run
    assert "ITEM_MASTER_UNAVAILABLE" in _codes(res)


def test_unlinked_items_are_deduplicated_and_counted():
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW, _GOOD_ROW, _GOOD_ROW.replace(",340021,", ",111111,")),
        item_dims=_master(999999),
    )
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_GAPS")
    counts = dict(zip(finding.cells["Item #"], finding.cells["Rows in your file"]))
    assert counts == {"340021": 2, "111111": 1}


# ── Informational ────────────────────────────────────────────────────────────

def test_duplicate_rows_are_not_reported_at_all():
    """The pipeline sums duplicates by design, so flagging them is pure noise."""
    res = pf.check_distribution_tracker(_csv(_GOOD_ROW, _GOOD_ROW),
                                        item_dims=_master(340021))
    assert res.clean, _codes(res)


def test_every_finding_carries_actionable_guidance():
    """No finding may say what is wrong without saying what to do."""
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW.replace(",1000000,", ",#N/A,")), item_dims=_master(999999),
    )
    for f in res.findings:
        if f.severity == pf.SEVERITY_INFO:
            continue
        assert f.title and f.means, f.code
        assert f.fix_where in (pf.FIX_IN_EXCEL, pf.FIX_IN_FABRIC), f.code
        assert f.fix_steps, f.code


# ── Scope: rows the pipeline discards are not scrutinised ────────────────────
#
# Reported from the app: 932 rows flagged for an unreadable First Ship Date,
# most of them already reflected in APS — rows that never reach the report, so
# blocking the run on them was pure noise.  Row-level checks now run only over
# rows that could land in RO_Seed (see data_sources.ro_risk.seed_scope_mask).

_SCOPED_HEADER = (
    "Month,Format,Customer,Taxonomy,Brand,Item #,Item Desc,Probability,"
    "First Ship Date,Reflected in APS,Pipeline Status,Lbs./yr,PC$/yr,Slotting\n"
)


def _scoped_row(*, item="340021", prob="0.5", ship="2027-01-01",
                reflected="no", status="Presented", lbs="1000000") -> str:
    return (
        f"2026-06-01,HTST,Walmart,Retail,DG,{item},Milk Gallon,{prob},"
        f"{ship},{reflected},{status},{lbs},0,0\n"
    )


def _scoped_csv(*rows: str) -> bytes:
    return (_SCOPED_HEADER + "".join(rows)).encode("utf-8")


@pytest.mark.parametrize("field,value", [
    ("reflected", "yes"),
    ("status", "Declined"),
    ("status", "Closed"),
    ("prob", "0"),
])
def test_a_broken_cell_on_an_out_of_scope_row_does_not_block(field, value):
    """The row never reaches the report, so its bad date is not the app's business."""
    row = _scoped_row(ship="soon", **{field: value})
    res = pf.check_distribution_tracker(_scoped_csv(row),
                                        item_dims=_master(340021))
    assert res.ok_to_run, f"{field}={value} should not block"
    assert "BAD_SHIP_DATE" not in _codes(res)


def test_the_same_broken_cell_still_blocks_on_an_in_scope_row():
    """The control: scoping must not have disabled the check itself."""
    res = pf.check_distribution_tracker(_scoped_csv(_scoped_row(ship="soon")),
                                        item_dims=_master(340021))
    assert not res.ok_to_run
    assert "BAD_SHIP_DATE" in _codes(res)


def test_out_of_scope_rows_are_counted_and_explained():
    res = pf.check_distribution_tracker(
        _scoped_csv(
            _scoped_row(item="340021"),
            _scoped_row(item="111111", reflected="yes", ship="soon"),
            _scoped_row(item="222222", status="Declined", ship="soon"),
        ),
        item_dims=_master(340021, 111111, 222222),
    )
    assert res.row_count == 3
    assert res.rows_in_scope == 1
    note = next(f for f in res.findings if f.code == "OUT_OF_SCOPE_ROWS")
    assert note.severity == pf.SEVERITY_INFO
    assert "2 of 3" in note.title
    assert "reflected in APS" in note.means


def test_excel_row_numbers_survive_the_scope_filter():
    """The fix list must point at the row in the planner's file, not the subset."""
    res = pf.check_distribution_tracker(
        _scoped_csv(
            _scoped_row(item="111111", reflected="yes"),   # file row 2, skipped
            _scoped_row(item="222222", reflected="yes"),   # file row 3, skipped
            _scoped_row(item="340021", ship="soon"),      # file row 4, broken
        ),
        item_dims=_master(340021, 111111, 222222),
    )
    finding = next(f for f in res.findings if f.code == "BAD_SHIP_DATE")
    assert list(finding.cells["Excel row"]) == [4]


def test_an_out_of_scope_item_is_not_reported_as_unclassified():
    """131 unclassified items, mostly already in APS — same noise, same fix."""
    res = pf.check_distribution_tracker(
        _scoped_csv(_scoped_row(item="999999", reflected="yes")),
        item_dims=_master(340021),
    )
    assert "ITEM_MASTER_GAPS" not in _codes(res)


def test_a_risk_line_is_in_scope_even_when_declined():
    """A committed loss counts, so its broken date still has to be fixed."""
    row = _scoped_row(lbs="-500000", prob="1", status="Declined", ship="soon")
    res = pf.check_distribution_tracker(_scoped_csv(row),
                                        item_dims=_master(340021))
    assert not res.ok_to_run
    assert "BAD_SHIP_DATE" in _codes(res)


def test_the_planners_rules_decide_what_gets_checked():
    """Raise the opportunity floor and a 50% row stops being scrutinised."""
    from data_sources.ro_rules_config import RoRulesConfig

    row = _scoped_row(prob="0.5", ship="soon")
    strict = RoRulesConfig.default().with_updates(min_opp_probability=0.6)

    assert not pf.check_distribution_tracker(
        _scoped_csv(row), item_dims=_master(340021)).ok_to_run
    assert pf.check_distribution_tracker(
        _scoped_csv(row), item_dims=_master(340021),
        config=strict).ok_to_run


# ── Excel serial dates are dates, not errors ─────────────────────────────────
#
# Reported from the app: 932 rows flagged for an "unreadable" First Ship Date,
# the values being 45641 / 45754 / 46006 — Excel's own date serials, which
# ro_seed_pipeline has always read (45641 = 2024-12-15).  The validator used a
# bare pd.to_datetime and disagreed with the pipeline it was gating.


@pytest.mark.parametrize("serial,iso", [
    ("45641", "2024-12-15"),
    ("45754", "2025-04-07"),
    ("46006", "2025-12-15"),
    ("45641.0", "2024-12-15"),
])
def test_an_excel_serial_ship_date_is_accepted(serial, iso):
    row = _GOOD_ROW.replace(",2027-01-01,", f",{serial},")
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master(340021))
    assert res.ok_to_run, f"{serial} is {iso} — the pipeline reads it"
    assert "BAD_SHIP_DATE" not in _codes(res)


@pytest.mark.parametrize("fmt", ["2027-01-01", "01/15/2027", "1/15/2027", "45641"])
def test_every_date_format_the_pipeline_reads_is_accepted(fmt):
    row = _GOOD_ROW.replace(",2027-01-01,", f",{fmt},")
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master(340021))
    assert "BAD_SHIP_DATE" not in _codes(res), fmt


def test_an_excel_serial_month_is_accepted():
    """Month is the same story — 46539 is 2027-06-01, a first-of-month."""
    row = _GOOD_ROW.replace("2026-06-01,", "46539,")
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master(340021))
    assert "BAD_MONTH_VALUE" not in _codes(res), _codes(res)


def test_a_mid_month_excel_serial_is_still_rejected():
    """Parsing it correctly is what lets us apply the first-of-month rule."""
    row = _GOOD_ROW.replace("2026-06-01,", "46553,")   # 2027-06-15
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master(340021))
    assert not res.ok_to_run
    assert "BAD_MONTH_VALUE" in _codes(res)


@pytest.mark.parametrize("bad", ["soon", "TBD", "", "#N/A", "Q1"])
def test_a_genuinely_unreadable_ship_date_still_blocks(bad):
    row = _GOOD_ROW.replace(",2027-01-01,", f",{bad},")
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master(340021))
    assert not res.ok_to_run, bad
    assert "BAD_SHIP_DATE" in _codes(res)


def test_the_ship_date_guidance_does_not_ask_for_pointless_retyping():
    row = _GOOD_ROW.replace(",2027-01-01,", ",soon,")
    res = pf.check_distribution_tracker(_csv(row), item_dims=_master(340021))
    finding = next(f for f in res.findings if f.code == "BAD_SHIP_DATE")
    steps = " ".join(finding.fix_steps)
    assert "do NOT need to reformat" in steps
    assert "45641" in steps


def test_the_validator_and_the_pipeline_agree_on_what_a_date_is():
    """The invariant: for ONE file, the rows the pipeline cannot parse are
    exactly the rows the validator flags.

    Compared on a single upload rather than value-by-value, because pandas
    infers a column's format from the column — so the answer for a value
    legitimately depends on its neighbours.
    """
    from data_sources.ro_seed_pipeline import _canon_date

    values = ["2027-01-01", "01/15/2027", "45641", "45641.0", "1/2/2027",
              "soon", "", "#N/A", "TBD"]
    rows = [
        _GOOD_ROW.replace(",2027-01-01,", f",{v},").replace(",340021,", f",{340000+i},")
        for i, v in enumerate(values)
    ]
    res = pf.check_distribution_tracker(
        _csv(*rows), item_dims=_master(*[340000 + i for i in range(len(values))]),
    )

    pipeline_nat = {
        i + 2 for i, ts in enumerate(_canon_date(pd.Series(values))) if pd.isna(ts)
    }
    finding = next((f for f in res.findings if f.code == "BAD_SHIP_DATE"), None)
    validator_flagged = set(finding.cells["Excel row"]) if finding is not None else set()

    assert validator_flagged == pipeline_nat, (
        f"validator flagged rows {sorted(validator_flagged)} but the pipeline "
        f"cannot parse rows {sorted(pipeline_nat)}"
    )


# ── Classification consults PDH, not just RO_Item_Master ─────────────────────
#
# An item qry_pdh.csv already classifies needs no RO_Item_Master row.  Checking
# the master alone reported items that were never a problem and sent the
# planner to edit the wrong file.  Only an item missing from BOTH is reported.


def test_an_item_known_only_to_pdh_is_not_reported():
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW), item_dims=_dims(pdh=_pdh_frame("340021")),
    )
    assert res.clean, _codes(res)


def test_an_item_known_only_to_the_master_is_not_reported():
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW), item_dims=_dims(master=_master_frame(340021)),
    )
    assert res.clean, _codes(res)


def test_an_item_missing_from_both_is_reported():
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW),
        item_dims=_dims(pdh=_pdh_frame("111111"), master=_master_frame(222222)),
    )
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_GAPS")
    assert finding.cells.iloc[0]["Item #"] == "340021"
    assert finding.cells.iloc[0]["Why it will fail"] == (
        "Not in qry_pdh.csv or RO_Item_Master.csv"
    )


def test_pdh_fills_a_gap_the_master_left_blank():
    """The cascade coalesces per field, so one source can rescue the other."""
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW),
        item_dims=_dims(
            pdh=_pdh_frame("340021"),                          # fully classified
            master=_master_frame(340021, portfolio_minor=""),  # gap here
        ),
    )
    assert res.clean, _codes(res)


def test_a_field_blank_in_both_sources_is_still_reported():
    pdh = _pdh_frame("340021").assign(**{"Portfolio Minor": [""]})
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW),
        item_dims=_dims(pdh=pdh, master=_master_frame(340021, portfolio_minor="")),
    )
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_GAPS")
    assert "Portfolio Minor" in finding.cells.iloc[0]["Why it will fail"]


def test_no_dim_source_at_all_is_reported_not_silently_passed():
    res = pf.check_distribution_tracker(_csv(_GOOD_ROW), item_dims=None)
    assert res.ok_to_run
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_UNAVAILABLE")
    assert "qry_pdh.csv" in finding.means
    assert "RO_Item_Master.csv" in finding.means


def test_the_fix_still_points_at_the_editable_file():
    """PDH is generated upstream; RO_Item_Master is the one a planner edits."""
    res = pf.check_distribution_tracker(
        _csv(_GOOD_ROW), item_dims=_dims(master=_master_frame(999999)),
    )
    finding = next(f for f in res.findings if f.code == "ITEM_MASTER_GAPS")
    assert finding.fabric_path.endswith("RO_Item_Master.csv")
    assert "RO_Item_Master.csv" in finding.cells.iloc[0]["What to fill in"]
