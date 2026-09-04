"""
Pre-flight validation for an uploaded ``Distribution_Tracker.csv``.

Why this module exists
----------------------
The RO pipeline (:mod:`data_sources.ro_seed_pipeline`) is deliberately
forgiving: a missing ``Month`` column only warns, absent RO_Seed columns are
created blank, and every numeric cell goes through
``pd.to_numeric(errors="coerce")``.  That is the right behaviour for a batch
job — but in an app it means a broken export **runs to completion**, writes
three files to Fabric, and produces a plausible-looking report in which an
``#N/A`` has silently become zero volume.

This module is the gate in front of that.  It reads the uploaded bytes, finds
everything wrong, and returns findings a non-technical planner can act on
without help: what is wrong, what it means, and exactly where to fix it —
either *which spreadsheet cell* or *which Fabric file, step by step*.

Two severities, by design
-------------------------
* ``SEVERITY_BLOCK`` — the file's structure or numbers are wrong.  Running
  would corrupt the published report, so the caller must refuse.  Always
  fixable in the planner's own spreadsheet.
* ``SEVERITY_ACK`` — the file is structurally sound, but something will come
  through blank or unclassified: an item missing from ``RO_Item_Master.csv``,
  a blank classifier on the master row, a broken dollar cell, or an absent
  optional column.  None of it makes the volume numbers wrong, and each is
  sometimes legitimate (a genuinely new SKU), so the caller may proceed once
  the planner explicitly acknowledges it.

Deliberately NOT checked
------------------------
Anything that cannot make ``RO_Comparison_Output.csv`` wrong.  Duplicate rows
(the pipeline sums them, by design), blank optional text fields, and cells in
columns no total depends on are left alone: a gate that reports harmless
findings trains people to click past the ones that matter.

Nothing here imports Streamlit or touches Fabric — the caller supplies the
bytes and (optionally) the already-fetched RO_Item_Master frame — so the whole
rule set is unit-testable.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


# ── Severities & fix locations ───────────────────────────────────────────────

SEVERITY_BLOCK: str = "block"
SEVERITY_ACK: str = "acknowledge"
SEVERITY_INFO: str = "info"

FIX_IN_EXCEL: str = "excel"
FIX_IN_FABRIC: str = "fabric"
FIX_NONE: str = ""


# ── The input contract (mirrors ro_seed_pipeline) ────────────────────────────

#: The snapshot column. Every row must carry the month being uploaded.
MONTH_COLUMN: str = "Month"

#: Header aliases the pipeline accepts — older exports use the long names.
#: Validation applies these first so a file using either spelling passes.
HEADER_ALIASES: dict = {
    "Anticipated Annual Lbs. Vol": "Lbs./yr",
    "Annual PC $": "PC$/yr",
    "Total Anticipated Slotting Costs": "Slotting",
}

#: Columns whose absence or corruption makes ``RO_Comparison_Output.csv``
#: wrong.  Only these block a run — the point of this module is to catch what
#: breaks the report, not to audit every cell in the file.
#:
#: * ``Format`` / ``Customer`` / ``Item #`` — the RO Key and the portfolio
#:   join.  Lose one and rows merge together or classify nowhere.
#: * ``Probability`` — multiplies every probabilized volume.
#: * ``First Ship Date`` — decides how much lands inside the fiscal year.
#: * ``Lbs./yr`` — the volume every headline number is built from.
CRITICAL_COLUMNS: tuple = (
    "Format", "Customer", "Item #", "Probability", "First Ship Date", "Lbs./yr",
)

#: Part of the contract, but the report still builds without them: these come
#: through blank rather than wrong, so they are worth a mention, not a block.
OPTIONAL_COLUMNS: tuple = ("Taxonomy", "Brand", "Item Desc", "PC$/yr", "Slotting")

#: The one column whose bad cells silently become **zero volume**.
VOLUME_COLUMN: str = "Lbs./yr"

#: Dollar metrics — they ride along in the report but drive no volume, so a
#: broken cell here is worth flagging without stopping the run.
MONEY_COLUMNS: tuple = ("PC$/yr", "Slotting")

#: Cap on how many problem rows are rendered inline; the full set always goes
#: into the downloadable fix list.  A planner fixing 400 cells wants the CSV,
#: not 400 rows on screen.
MAX_CELLS_SHOWN: int = 25

#: Read as strings so we see exactly what the planner's file contains — a
#: pandas-parsed frame would already have turned "#N/A" into NaN and hidden it.
_READ_KW: dict = {"dtype": str, "keep_default_na": False}

#: Excel's own error literals. These are the single most common cause of a
#: silently-zeroed row, because they survive a CSV export as text.
_EXCEL_ERRORS: frozenset = frozenset({
    "#N/A", "#REF!", "#VALUE!", "#DIV/0!", "#NAME?", "#NULL!", "#NUM!",
    "NA", "N/A", "#SPILL!", "#CALC!",
})


def _is_excel_error(value: str) -> bool:
    """True when a cell holds one of Excel's error literals (any casing)."""
    return value.strip().upper() in _EXCEL_ERRORS


def _excel_row(index: int) -> int:
    """Spreadsheet row number for a 0-based frame index (row 1 = header)."""
    return int(index) + 2


# ── Findings ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """One thing wrong with the upload, written for a non-technical reader.

    Attributes
    ----------
    code
        Stable machine identifier (used by tests and by the UI to pick an icon).
    severity
        One of :data:`SEVERITY_BLOCK`, :data:`SEVERITY_ACK`, :data:`SEVERITY_INFO`.
    title
        One plain-English line naming the problem. No jargon, no column
        internals the planner has never seen.
    means
        What it will do to the numbers if it is not fixed — the "so what".
    fix_where
        :data:`FIX_IN_EXCEL`, :data:`FIX_IN_FABRIC` or :data:`FIX_NONE`.
    fix_steps
        Numbered instructions. For a Fabric fix these are literal click steps.
    cells
        Optional table of the exact places to fix, with a spreadsheet row
        number so the planner can jump straight to it.
    fabric_path
        Optional lakehouse path (no ``Files/`` prefix) the fix applies to; the
        UI turns this into a deep link.
    """
    code: str
    severity: str
    title: str
    means: str
    fix_where: str = FIX_NONE
    fix_steps: tuple = ()
    cells: Optional[pd.DataFrame] = None
    fabric_path: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == SEVERITY_BLOCK


@dataclass
class PreflightResult:
    """Outcome of validating one upload."""
    findings: list = field(default_factory=list)
    row_count: int = 0
    months: list = field(default_factory=list)
    parsed: Optional[pd.DataFrame] = None

    @property
    def blocking(self) -> list:
        return [f for f in self.findings if f.severity == SEVERITY_BLOCK]

    @property
    def acknowledgeable(self) -> list:
        return [f for f in self.findings if f.severity == SEVERITY_ACK]

    @property
    def informational(self) -> list:
        return [f for f in self.findings if f.severity == SEVERITY_INFO]

    @property
    def ok_to_run(self) -> bool:
        """True when nothing structural is wrong (acknowledgement aside)."""
        return not self.blocking

    @property
    def clean(self) -> bool:
        """True when the file needs no attention at all."""
        return not self.blocking and not self.acknowledgeable


# ── Individual checks ────────────────────────────────────────────────────────


def _check_month_column(df: pd.DataFrame) -> list:
    """The ``Month`` column must exist and hold a first-of-month date."""
    if MONTH_COLUMN not in df.columns:
        return [Finding(
            code="MISSING_MONTH_COLUMN",
            severity=SEVERITY_BLOCK,
            title="Your file has no “Month” column",
            means=(
                "Without it the app cannot tell which month you are uploading, "
                "so it cannot replace the right month in the history — you "
                "would end up with two copies of the same data."
            ),
            fix_where=FIX_IN_EXCEL,
            fix_steps=(
                "Open your file in Excel.",
                "Add one new column and type **Month** in its header row "
                "(row 1). Spelling and capitalisation must match exactly.",
                "Fill EVERY data row with the first day of the month you are "
                "uploading — for example **2026-06-01** for June 2026. The "
                "same value goes in every row.",
                "Save as CSV (UTF-8) and upload again.",
            ),
        )]

    raw = df[MONTH_COLUMN].astype(str).str.strip()
    parsed = pd.to_datetime(raw, errors="coerce")

    bad_rows = []
    for idx, (text, ts) in enumerate(zip(raw, parsed)):
        if not text:
            bad_rows.append((_excel_row(idx), "(blank)", "A date like 2026-06-01"))
        elif pd.isna(ts):
            bad_rows.append((_excel_row(idx), text, "A date like 2026-06-01"))
        elif ts.day != 1:
            bad_rows.append((
                _excel_row(idx), text,
                f"The FIRST of the month — {ts.strftime('%Y-%m')}-01",
            ))

    if not bad_rows:
        return []

    return [Finding(
        code="BAD_MONTH_VALUE",
        severity=SEVERITY_BLOCK,
        title=f"{len(bad_rows)} row(s) have a Month that isn’t the first of a month",
        means=(
            "The Month value is how the app finds and replaces the right "
            "month of history. A blank, a mid-month date or text here means "
            "those rows land in the wrong month — or in no month at all."
        ),
        fix_where=FIX_IN_EXCEL,
        fix_steps=(
            "Open your file in Excel and go to the **Month** column.",
            "Fix each row listed below so it reads the first day of the month, "
            "for example **2026-06-01**.",
            "Tip: it is normally the same value in every row. Type it once and "
            "fill down.",
            "Save as CSV (UTF-8) and upload again.",
        ),
        cells=pd.DataFrame(
            bad_rows, columns=["Excel row", "What your file has", "What it needs"],
        ),
    )]


def _check_columns(df: pd.DataFrame) -> list:
    """Critical columns must exist; optional ones only earn a mention."""
    reverse_alias = {v: k for k, v in HEADER_ALIASES.items()}
    out = []

    missing_critical = [c for c in CRITICAL_COLUMNS if c not in df.columns]
    if missing_critical:
        out.append(Finding(
            code="MISSING_COLUMNS",
            severity=SEVERITY_BLOCK,
            title=f"{len(missing_critical)} column(s) the report cannot be built without",
            means=(
                "These columns are what the report groups and totals by. With "
                "one missing, rows that should be separate merge together and "
                "every total below them is wrong."
            ),
            fix_where=FIX_IN_EXCEL,
            fix_steps=(
                "Open your file in Excel and look at the header row (row 1).",
                "Add each column below. The header text must match exactly — "
                "no extra spaces, same capitalisation.",
                "If your export uses the older name shown alongside, either "
                "name works — so look for a typo before adding a duplicate.",
                "Save as CSV (UTF-8) and upload again.",
            ),
            cells=pd.DataFrame(
                [(c, reverse_alias.get(c, "—")) for c in missing_critical],
                columns=["Missing column", "Older name also accepted"],
            ),
        ))

    missing_optional = [c for c in OPTIONAL_COLUMNS if c not in df.columns]
    if missing_optional:
        out.append(Finding(
            code="MISSING_OPTIONAL_COLUMNS",
            severity=SEVERITY_ACK,
            title=f"{len(missing_optional)} column(s) missing — the report will "
                  f"build, but those fields come through blank",
            means=(
                "Nothing breaks: totals stay correct. Rows just carry no "
                f"{', '.join(missing_optional)}, so they will read as blank "
                "in the report and in any drill-in."
            ),
            fix_where=FIX_IN_EXCEL,
            fix_steps=(
                "Add the column(s) below to your export if you want those "
                "fields populated.",
                "Or tick the box below to run without them.",
            ),
            cells=pd.DataFrame(
                [(c, reverse_alias.get(c, "—")) for c in missing_optional],
                columns=["Missing column", "Older name also accepted"],
            ),
        ))
    return out


def _bad_number_rows(df: pd.DataFrame, col: str) -> list:
    """Rows in *col* the pipeline cannot read as a number."""
    bad = []
    for idx, text in enumerate(df[col].astype(str)):
        stripped = text.strip()
        if not stripped:
            continue                          # blank is legitimately zero
        if _is_excel_error(stripped):
            bad.append((
                _excel_row(idx), col, stripped,
                "A number — the formula behind this cell is broken",
            ))
            continue
        # Mirror the pipeline's own cleanup before judging it unparseable, so
        # "1,234" and "$1,234" (which it handles) are NOT flagged.
        cleaned = pd.to_numeric(
            pd.Series([stripped]).str.replace(r"[^\d.-]", "", regex=True),
            errors="coerce",
        ).iloc[0]
        if pd.isna(cleaned):
            bad.append((_excel_row(idx), col, stripped, "A number, e.g. 1250000"))
    return bad


_CELL_COLUMNS: list = ["Excel row", "Column", "What your file has", "What it needs"]


def _check_volume(df: pd.DataFrame) -> list:
    """``Lbs./yr`` must be numeric — a bad cell here becomes zero volume."""
    if VOLUME_COLUMN not in df.columns:
        return []
    bad = _bad_number_rows(df, VOLUME_COLUMN)
    if not bad:
        return []
    return [Finding(
        code="INVALID_VOLUME",
        severity=SEVERITY_BLOCK,
        title=f"{len(bad)} row(s) have a broken {VOLUME_COLUMN} value",
        means=(
            "This is the one that bites. A cell the app cannot read counts as "
            "**zero volume**, so the opportunity quietly vanishes from the "
            "report — no error, no warning, just a smaller number than the "
            "truth."
        ),
        fix_where=FIX_IN_EXCEL,
        fix_steps=(
            "Open your file in Excel and go to each row listed below.",
            "An **#N/A** or **#REF!** means a lookup lost its source. Fix the "
            "formula, or paste the correct number in as a value.",
            "A genuinely empty cell is fine — leave it blank rather than "
            "typing “NA”.",
            "Save as CSV (UTF-8) and upload again.",
        ),
        cells=pd.DataFrame(bad, columns=_CELL_COLUMNS),
    )]


def _check_money(df: pd.DataFrame) -> list:
    """``PC$/yr`` / ``Slotting`` — flag, but never block: no volume rides on them."""
    bad = []
    for col in MONEY_COLUMNS:
        if col in df.columns:
            bad.extend(_bad_number_rows(df, col))
    if not bad:
        return []
    return [Finding(
        code="INVALID_MONEY",
        severity=SEVERITY_ACK,
        title=f"{len(bad)} row(s) have a broken dollar value",
        means=(
            "Volume and the probabilized totals are unaffected — these columns "
            "carry dollars only. The affected rows will show $0 rather than "
            "their real value."
        ),
        fix_where=FIX_IN_EXCEL,
        fix_steps=(
            "Fix the rows below if the dollar figures matter for this cycle.",
            "Otherwise tick the box below and run — the volume numbers are "
            "correct either way.",
        ),
        cells=pd.DataFrame(bad, columns=_CELL_COLUMNS),
    )]


def _check_probability(df: pd.DataFrame) -> list:
    """Probability must land in [0, 1] — the pipeline accepts 0.5, 50 or 50%."""
    if "Probability" not in df.columns:
        return []                              # already reported as missing

    raw = df["Probability"].astype(str).str.strip()
    is_pct = raw.str.endswith("%")
    num = pd.to_numeric(
        raw.str.replace(r"[^\d.-]", "", regex=True), errors="coerce",
    )
    frac = num.where(~is_pct, num / 100.0)
    # A bare value in (1, 100] is unambiguously a percent — the pipeline
    # normalises it, so it is NOT an error here either.
    frac = frac.where(~((~is_pct) & frac.notna() & (frac > 1.0) & (frac <= 100.0)),
                      frac / 100.0)

    bad_rows = []
    for idx, (text, value) in enumerate(zip(raw, frac)):
        if not text:
            bad_rows.append((_excel_row(idx), "(blank)", "A probability, e.g. 0.5 or 50%"))
        elif pd.isna(value):
            bad_rows.append((_excel_row(idx), text, "A probability, e.g. 0.5 or 50%"))
        elif value < 0.0 or value > 1.0:
            bad_rows.append((_excel_row(idx), text, "Between 0 and 1 (or 0%–100%)"))

    if not bad_rows:
        return []

    return [Finding(
        code="BAD_PROBABILITY",
        severity=SEVERITY_BLOCK,
        title=f"{len(bad_rows)} row(s) have a Probability the app cannot read",
        means=(
            "Probability multiplies every volume in the report. A row the app "
            "cannot read gets no probabilized volume at all, so the "
            "opportunity is missing from the plan."
        ),
        fix_where=FIX_IN_EXCEL,
        fix_steps=(
            "Open your file in Excel and go to the **Probability** column.",
            "Write each value as a fraction (**0.5**) or an explicit percent "
            "(**50%**). Both are accepted.",
            "Do not leave it blank — if you genuinely do not know, use 0.",
            "Save as CSV (UTF-8) and upload again.",
        ),
        cells=pd.DataFrame(
            bad_rows, columns=["Excel row", "What your file has", "What it needs"],
        ),
    )]


def _check_ship_dates(df: pd.DataFrame) -> list:
    """First Ship Date drives the in-year proration, so it must be a date."""
    if "First Ship Date" not in df.columns:
        return []

    raw = df["First Ship Date"].astype(str).str.strip()
    parsed = pd.to_datetime(raw, errors="coerce")
    bad_rows = [
        (_excel_row(idx), text or "(blank)", "A date like 2027-01-01")
        for idx, (text, ts) in enumerate(zip(raw, parsed))
        if not text or pd.isna(ts)
    ]
    if not bad_rows:
        return []

    return [Finding(
        code="BAD_SHIP_DATE",
        severity=SEVERITY_BLOCK,
        title=f"{len(bad_rows)} row(s) have a First Ship Date the app cannot read",
        means=(
            "The ship date decides how much of the annual volume falls inside "
            "this fiscal year. Without it the row contributes **nothing** to "
            "the in-year number, even though its annual volume looks fine."
        ),
        fix_where=FIX_IN_EXCEL,
        fix_steps=(
            "Open your file in Excel and go to the **First Ship Date** column.",
            "Give every row a real date, e.g. **2027-01-01**.",
            "Watch for dates stored as text — if the cell is left-aligned in "
            "Excel it is text, not a date. Re-type it.",
            "Save as CSV (UTF-8) and upload again.",
        ),
        cells=pd.DataFrame(
            bad_rows, columns=["Excel row", "What your file has", "What it needs"],
        ),
    )]


#: The RO_Item_Master fields that classify an item into the report's rows.
#: A blank in any of them puts the item's volume in Total B2C but under no
#: portfolio row — the same visible symptom as a missing item.
ITEM_MASTER_CLASSIFIERS: tuple = (
    "Portfolio Major", "Portfolio Minor", "Brand Category",
)


def _norm_item_key(series: pd.Series) -> pd.Series:
    """Digits-only item key, leading zeros dropped, blanks as NA."""
    return (series.astype(str).str.replace(r"[^\d]", "", regex=True)
            .str.lstrip("0").replace("", pd.NA))


def _check_item_master_linkage(
    df: pd.DataFrame,
    item_master_df: Optional[pd.DataFrame],
    item_master_path: str,
) -> list:
    """Report every item whose classification will fail, and why.

    Two distinct causes, same visible symptom in the report, so they are listed
    together with a per-item reason rather than split into two findings the
    planner has to correlate:

    * the item has no row in ``RO_Item_Master.csv`` at all;
    * it has a row, but one of the classifier fields is blank.
    """
    if "Item #" not in df.columns:
        return []                              # already reported as missing

    if item_master_df is None or item_master_df.empty:
        return [Finding(
            code="ITEM_MASTER_UNAVAILABLE",
            severity=SEVERITY_ACK,
            title="RO_Item_Master.csv could not be read, so items weren’t checked",
            means=(
                "Items are classified into Portfolio Major / Minor and Brand "
                "Category through that file. Without it, rows may land "
                "unclassified in the report."
            ),
            fix_where=FIX_IN_FABRIC,
            fix_steps=(
                "Open the Fabric link below and confirm "
                "**RO_Item_Master.csv** is in the folder.",
                "If it is missing, upload the latest copy there.",
                "Re-upload your file here to run the check again.",
            ),
            fabric_path=item_master_path,
        )]

    master = item_master_df.copy()
    master.columns = [str(c).strip() for c in master.columns]
    has_key = "Item #" in master.columns
    master_keys = _norm_item_key(master["Item #"]) if has_key else pd.Series(dtype=object)

    # key -> the classifier fields that are blank on that master row
    blanks_by_key: dict = {}
    if has_key:
        classifiers = [c for c in ITEM_MASTER_CLASSIFIERS if c in master.columns]
        absent = [c for c in ITEM_MASTER_CLASSIFIERS if c not in master.columns]
        for pos, key in enumerate(master_keys):
            if pd.isna(key):
                continue
            blank = [
                c for c in classifiers
                if not str(master[c].iloc[pos]).strip()
                or str(master[c].iloc[pos]).strip().lower() in ("nan", "none")
            ]
            blanks_by_key[key] = blank + absent
    known = set(blanks_by_key)

    desc = (df["Item Desc"].astype(str) if "Item Desc" in df.columns
            else pd.Series([""] * len(df), index=df.index))
    file_keys = _norm_item_key(df["Item #"])

    # raw item -> [description, row count, why, what to fill in]
    problems: dict = {}
    for pos, key in enumerate(file_keys):
        if pd.isna(key):
            continue
        raw = str(df["Item #"].iloc[pos]).strip()
        if key not in known:
            why = "Not in RO_Item_Master.csv"
            todo = "Add a row: Item #, Item Desc, " + ", ".join(
                ITEM_MASTER_CLASSIFIERS)
        else:
            blank = blanks_by_key[key]
            if not blank:
                continue                       # properly classified
            why = f"In RO_Item_Master.csv, but {', '.join(blank)} is blank"
            todo = "Fill in " + ", ".join(blank)
        entry = problems.setdefault(raw, [str(desc.iloc[pos]).strip(), 0, why, todo])
        entry[1] += 1

    if not problems:
        return []

    rows = [(item, d or "—", n, why, todo)
            for item, (d, n, why, todo) in sorted(problems.items())]
    n_absent = sum(1 for r in rows if r[3].startswith("Not in"))
    n_blank = len(rows) - n_absent
    detail = " · ".join(filter(None, [
        f"{n_absent} not in the file" if n_absent else "",
        f"{n_blank} present but unclassified" if n_blank else "",
    ]))

    return [Finding(
        code="ITEM_MASTER_GAPS",
        severity=SEVERITY_ACK,
        title=f"{len(rows)} item(s) will not classify — {detail}",
        means=(
            "Each item below still counts in **Total B2C**, but it appears "
            "under no portfolio row — so the portfolio lines will not add up "
            "to the total. Expected for a brand-new SKU; a real problem if the "
            "item has been sold before."
        ),
        fix_where=FIX_IN_FABRIC,
        fix_steps=(
            "Download **RO_Item_Master.csv** — the red button in Step 4c, or "
            "the Fabric link below (⋯ → Download).",
            "Open it in Excel and work through the list below: each row says "
            "whether the item is missing entirely or just unclassified, and "
            "which fields to fill in.",
            "In Fabric, **delete the existing RO_Item_Master.csv**, then "
            "upload your edited file under the *same name* "
            "(⋯ → Upload → Upload files).",
            "Come back and re-upload your Distribution Tracker — this check "
            "will clear.",
            "In a hurry? Tick the box below to run now and classify later.",
        ),
        cells=pd.DataFrame(rows, columns=[
            "Item #", "Item description", "Rows in your file",
            "Why it will fail", "What to fill in",
        ]),
        fabric_path=item_master_path,
    )]


# ── Entry point ──────────────────────────────────────────────────────────────


def check_distribution_tracker(
    file_bytes: bytes,
    *,
    item_master_df: Optional[pd.DataFrame] = None,
    item_master_path: str = "RO Tracking/RO_Item_Master.csv",
) -> PreflightResult:
    """Validate an uploaded ``Distribution_Tracker.csv`` before anything runs.

    Parameters
    ----------
    file_bytes
        The raw uploaded bytes.
    item_master_df
        Already-fetched ``RO_Item_Master.csv`` frame, used for the linkage
        check. Pass ``None`` when Fabric is unreachable — the check then
        reports that it could not run rather than silently passing.
    item_master_path
        Lakehouse path of RO_Item_Master, echoed into the finding so the UI can
        build a deep link.

    Returns
    -------
    PreflightResult
        ``ok_to_run`` is False whenever anything structural is wrong.  A file
        with only ``SEVERITY_ACK`` findings is runnable once the planner
        acknowledges them.
    """
    result = PreflightResult()

    try:
        df = pd.read_csv(io.BytesIO(file_bytes), **_READ_KW)
    except Exception as exc:  # noqa: BLE001 — any parse failure is one finding
        result.findings.append(Finding(
            code="CANNOT_READ",
            severity=SEVERITY_BLOCK,
            title="This file could not be opened as a CSV",
            means="Nothing can be checked or run until the file itself reads.",
            fix_where=FIX_IN_EXCEL,
            fix_steps=(
                "In Excel choose **File → Save As** and pick "
                "**CSV UTF-8 (Comma delimited) (*.csv)**.",
                "Make sure you are uploading the saved CSV, not an .xlsx "
                "renamed to .csv.",
                "Upload the new file.",
            ),
        ))
        result.findings.append(Finding(
            code="CANNOT_READ_DETAIL",
            severity=SEVERITY_INFO,
            title="Technical detail (for IT, if you need to ask)",
            means=f"{type(exc).__name__}: {exc}",
        ))
        return result

    df.columns = [str(c).strip() for c in df.columns]
    df = df.rename(columns=HEADER_ALIASES)
    result.parsed = df
    result.row_count = len(df)

    if df.empty:
        result.findings.append(Finding(
            code="NO_ROWS",
            severity=SEVERITY_BLOCK,
            title="The file has headers but no data rows",
            means="There is nothing to add to the history.",
            fix_where=FIX_IN_EXCEL,
            fix_steps=(
                "Check you exported the **Customer Input** table with its rows, "
                "not just the header.",
                "Re-export and upload again.",
            ),
        ))
        return result

    if MONTH_COLUMN in df.columns:
        result.months = sorted(
            {str(m)[:10] for m in pd.to_datetime(
                df[MONTH_COLUMN], errors="coerce").dropna().unique()}
        )

    # Ordered by what breaks the report worst, so the first thing a planner
    # reads is the thing most worth fixing.
    result.findings.extend(_check_month_column(df))
    result.findings.extend(_check_columns(df))
    result.findings.extend(_check_volume(df))
    result.findings.extend(_check_probability(df))
    result.findings.extend(_check_ship_dates(df))
    result.findings.extend(
        _check_item_master_linkage(df, item_master_df, item_master_path)
    )
    result.findings.extend(_check_money(df))
    return result


__all__ = [
    "SEVERITY_BLOCK",
    "SEVERITY_ACK",
    "SEVERITY_INFO",
    "FIX_IN_EXCEL",
    "FIX_IN_FABRIC",
    "FIX_NONE",
    "MONTH_COLUMN",
    "HEADER_ALIASES",
    "CRITICAL_COLUMNS",
    "OPTIONAL_COLUMNS",
    "VOLUME_COLUMN",
    "MONEY_COLUMNS",
    "MAX_CELLS_SHOWN",
    "ITEM_MASTER_CLASSIFIERS",
    "Finding",
    "PreflightResult",
    "check_distribution_tracker",
]
