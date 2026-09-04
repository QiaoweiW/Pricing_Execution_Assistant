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
* ``SEVERITY_ACK`` — the file is structurally sound but references items that
  are not in ``RO_Item_Master.csv``.  Those items will be unclassified in the
  roll-up, which is sometimes legitimate (a genuinely new SKU), so the caller
  may proceed once the planner explicitly acknowledges it.

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

#: Columns the RO_Seed build groups on — a missing one silently collapses rows.
REQUIRED_KEY_COLUMNS: tuple = (
    "Format", "Customer", "Taxonomy", "Brand", "Item #", "Item Desc",
    "Probability", "First Ship Date",
)

#: Columns that are summed. A bad cell here becomes zero volume if unguarded.
REQUIRED_NUMERIC_COLUMNS: tuple = ("Lbs./yr", "PC$/yr", "Slotting")

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


def _check_required_columns(df: pd.DataFrame) -> list:
    """Every grouping and numeric column the RO_Seed build needs must exist."""
    expected = list(REQUIRED_KEY_COLUMNS) + list(REQUIRED_NUMERIC_COLUMNS)
    missing = [c for c in expected if c not in df.columns]
    if not missing:
        return []

    # Show the alias where one exists — the planner's export may use it.
    reverse_alias = {v: k for k, v in HEADER_ALIASES.items()}
    rows = [
        (c, reverse_alias.get(c, "—"))
        for c in missing
    ]
    return [Finding(
        code="MISSING_COLUMNS",
        severity=SEVERITY_BLOCK,
        title=f"{len(missing)} required column(s) are missing from your file",
        means=(
            "The app groups and totals your rows using these columns. If one "
            "is absent, rows that should be separate get merged together and "
            "the totals come out wrong."
        ),
        fix_where=FIX_IN_EXCEL,
        fix_steps=(
            "Open your file in Excel and look at the header row (row 1).",
            "Add each missing column below. The header text must match "
            "exactly — no extra spaces, same capitalisation.",
            "If your export uses the older name shown in the second column, "
            "either name works — check for a typo rather than adding a "
            "duplicate.",
            "Save as CSV (UTF-8) and upload again.",
        ),
        cells=pd.DataFrame(rows, columns=["Missing column", "Older name also accepted"]),
    )]


def _check_numeric_cells(df: pd.DataFrame) -> list:
    """Volume / dollar / slotting cells must be numbers, not Excel errors."""
    present = [c for c in REQUIRED_NUMERIC_COLUMNS if c in df.columns]
    if not present:
        return []

    bad_rows = []
    for col in present:
        series = df[col].astype(str)
        for idx, text in enumerate(series):
            stripped = text.strip()
            if not stripped:
                continue                      # blank is legitimately zero
            if _is_excel_error(stripped):
                bad_rows.append((
                    _excel_row(idx), col, stripped,
                    "A number — the formula behind this cell is broken",
                ))
                continue
            # Mirror the pipeline's own cleanup before judging it unparseable,
            # so "1,234" and "$1,234" (which it handles) are NOT flagged.
            cleaned = pd.to_numeric(
                pd.Series([stripped]).str.replace(r"[^\d.-]", "", regex=True),
                errors="coerce",
            ).iloc[0]
            if pd.isna(cleaned):
                bad_rows.append((
                    _excel_row(idx), col, stripped, "A number, e.g. 1250000",
                ))

    if not bad_rows:
        return []

    return [Finding(
        code="INVALID_NUMBER",
        severity=SEVERITY_BLOCK,
        title=f"{len(bad_rows)} cell(s) hold an error or text where a number belongs",
        means=(
            "This is the dangerous one. Left alone, each of these cells is "
            "read as **zero volume** — so the opportunity quietly disappears "
            "from the report instead of showing up as a problem."
        ),
        fix_where=FIX_IN_EXCEL,
        fix_steps=(
            "Open your file in Excel and go to each cell listed below.",
            "An **#N/A** or **#REF!** usually means a lookup formula lost its "
            "source. Fix the formula, or paste the correct number in as a value.",
            "A genuinely empty cell is fine — leave it blank rather than "
            "typing “NA”.",
            "Save as CSV (UTF-8) and upload again.",
        ),
        cells=pd.DataFrame(
            bad_rows,
            columns=["Excel row", "Column", "What your file has", "What it needs"],
        ),
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


def _check_item_master_linkage(
    df: pd.DataFrame,
    item_master_df: Optional[pd.DataFrame],
    item_master_path: str,
) -> list:
    """Items absent from RO_Item_Master will be unclassified in the roll-up."""
    if "Item #" not in df.columns:
        return []
    if item_master_df is None or item_master_df.empty:
        return [Finding(
            code="ITEM_MASTER_UNAVAILABLE",
            severity=SEVERITY_ACK,
            title="RO_Item_Master.csv could not be read, so linkage wasn’t checked",
            means=(
                "Items are classified into Portfolio Major / Minor and Brand "
                "Category through this file. Without it, rows may land "
                "unclassified in the roll-up."
            ),
            fix_where=FIX_IN_FABRIC,
            fix_steps=(
                "Open the Fabric link below and confirm "
                "**RO_Item_Master.csv** is present in the folder.",
                "If it is missing, upload the latest copy to that folder.",
                "Come back here and re-upload your file to re-run the check.",
            ),
            fabric_path=item_master_path,
        )]

    def _norm(series: pd.Series) -> pd.Series:
        return (series.astype(str).str.replace(r"[^\d]", "", regex=True)
                .str.lstrip("0").replace("", pd.NA))

    if "Item #" not in item_master_df.columns:
        known = set()
    else:
        known = set(_norm(item_master_df["Item #"]).dropna())

    file_items = _norm(df["Item #"])
    desc = (df["Item Desc"].astype(str) if "Item Desc" in df.columns
            else pd.Series([""] * len(df), index=df.index))

    unlinked: dict = {}
    for idx, key in enumerate(file_items):
        if pd.isna(key) or key in known:
            continue
        raw_item = str(df["Item #"].iloc[idx]).strip()
        unlinked.setdefault(raw_item, [str(desc.iloc[idx]).strip(), 0])
        unlinked[raw_item][1] += 1

    if not unlinked:
        return []

    rows = [(item, d or "—", n) for item, (d, n) in sorted(unlinked.items())]
    return [Finding(
        code="UNLINKED_ITEMS",
        severity=SEVERITY_ACK,
        title=f"{len(rows)} item number(s) are not in RO_Item_Master.csv",
        means=(
            "These items have no Portfolio Major / Minor or Brand Category, so "
            "their volume will sit unclassified in the RO Summary Report — it "
            "still counts in Total B2C, but it will not appear under the right "
            "portfolio row. That is expected for a brand-new SKU; it is a "
            "problem if the item has been sold before."
        ),
        fix_where=FIX_IN_FABRIC,
        fix_steps=(
            "Click the Fabric link below — it opens the folder holding "
            "**RO_Item_Master.csv**.",
            "Select **RO_Item_Master.csv** and download it (⋯ → Download).",
            "Open it in Excel and add one row per item listed below, filling "
            "in **Item #**, **Item Desc**, **Portfolio Major**, "
            "**Portfolio Minor** and **Brand Category**.",
            "Back in Fabric, **delete the existing RO_Item_Master.csv** in "
            "that folder, then upload your edited file with the *same name* "
            "(⋯ → Upload → Upload files).",
            "Return here and re-upload your Distribution Tracker — the check "
            "will clear.",
            "In a hurry? Tick the acknowledgement box below to run now and "
            "classify these items later.",
        ),
        cells=pd.DataFrame(rows, columns=["Item #", "Item description", "Rows in your file"]),
        fabric_path=item_master_path,
    )]


def _check_duplicates(df: pd.DataFrame) -> list:
    """Exact-duplicate business rows are summed together — worth knowing."""
    keys = [c for c in REQUIRED_KEY_COLUMNS if c in df.columns]
    if not keys or df.empty:
        return []
    dup_count = int(df.duplicated(subset=keys, keep="first").sum())
    if not dup_count:
        return []
    return [Finding(
        code="DUPLICATE_ROWS",
        severity=SEVERITY_INFO,
        title=f"{dup_count} row(s) repeat the same customer / item combination",
        means=(
            "That is fine — the app adds their volumes together into one line. "
            "Flagged only so a copy-paste accident does not double a number "
            "without you noticing."
        ),
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

    result.findings.extend(_check_month_column(df))
    result.findings.extend(_check_required_columns(df))
    result.findings.extend(_check_numeric_cells(df))
    result.findings.extend(_check_probability(df))
    result.findings.extend(_check_ship_dates(df))
    result.findings.extend(
        _check_item_master_linkage(df, item_master_df, item_master_path)
    )
    result.findings.extend(_check_duplicates(df))
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
    "REQUIRED_KEY_COLUMNS",
    "REQUIRED_NUMERIC_COLUMNS",
    "Finding",
    "PreflightResult",
    "check_distribution_tracker",
]
