"""
Pre-flight validation for the R&O uploads.

Two entry points share one findings vocabulary:
:func:`check_distribution_tracker` for the monthly tracker and
:func:`check_ro_item_master` for the item-classification file, both returning
a :class:`PreflightResult` — so the page renders either through the same
panel, with the same block / acknowledge semantics and fix-list downloads.

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

from .ro_dates import canonical_date_series
from .ro_keys import canonical_cell
from .ro_risk import seed_scope_mask
from .ro_rules_config import RoRulesConfig


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
    rows_in_scope: int = 0
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
    # The pipeline's own parser: ISO, mm/dd/yyyy AND Excel serials.  A bare
    # pd.to_datetime here would call every serial-formatted Month unreadable.
    parsed = canonical_date_series(df[MONTH_COLUMN])

    bad_rows = []
    for idx, text, ts in zip(raw.index, raw, parsed):
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
    for idx, text in df[col].astype(str).items():
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
    for idx, text, value in zip(raw.index, raw, frac):
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
    # Excel writes a date-formatted cell as a serial (45641 = 2024-12-15) and
    # the pipeline reads it fine, so the validator must too — see
    # :mod:`data_sources.ro_dates`.
    parsed = canonical_date_series(df["First Ship Date"])
    bad_rows = [
        (_excel_row(idx), text or "(blank)",
         "A date like 2027-01-01 (an Excel date serial such as 45641 is fine too)")
        for idx, text, ts in zip(raw.index, raw, parsed)
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
            "Fill in each row listed below with a real date, e.g. "
            "**2027-01-01**.",
            "You do NOT need to reformat dates that already work — the app "
            "reads `2027-01-01`, `01/15/2027` and Excel's own date numbers "
            "(like `45641`) equally well. Only the rows below need attention.",
            "Save as CSV (UTF-8) and upload again.",
        ),
        cells=pd.DataFrame(
            bad_rows, columns=["Excel row", "What your file has", "What it needs"],
        ),
    )]


def _scope_note(
    df: pd.DataFrame, scoped: pd.DataFrame, cfg,
) -> list:
    """Explain, once, why the checks looked at fewer rows than the file has.

    Without this the counts are baffling: a 3,000-row upload reporting "40
    places to fix" reads like the check gave up half way.  Naming the reasons
    also tells the planner something useful — that most of her file is already
    in the base plan, or already declined.
    """
    skipped = len(df) - len(scoped)
    if skipped <= 0:
        return []

    reasons = []
    if cfg.reflected_in_aps_only and "Reflected in APS" in df.columns:
        reasons.append("already reflected in APS")
    if cfg.normalised_excludes() and "Pipeline Status" in df.columns:
        reasons.append(
            f"status is {' or '.join(cfg.pipeline_status_excludes)}"
        )
    if "Probability" in df.columns:
        reasons.append(
            f"probability is {cfg.min_opp_probability:.0%} or lower"
        )
    why = "; ".join(reasons) if reasons else "they do not qualify as R&O"

    return [Finding(
        code="OUT_OF_SCOPE_ROWS",
        severity=SEVERITY_INFO,
        title=f"{skipped:,} of {len(df):,} row(s) were not checked — "
              f"they never reach the report",
        means=(
            f"Skipped because {why}. Those rows are filtered out before the "
            f"report is built, so nothing in them can change a number — "
            f"there is no point asking you to fix them. The checks below "
            f"cover the {len(scoped):,} row(s) that do count."
        ),
    )]


#: Fields on the cascaded dim frame that place an item on a portfolio row.
#: These are ``build_item_dim_frame``'s internal names; a blank in either means
#: the item's volume lands in Total B2C under no portfolio line.
_DIM_PORTFOLIO_FIELDS: tuple = ("pmaj", "pminor")

#: Internal field name → the label a planner sees in the two source files.
_DIM_FIELD_LABELS: dict = {
    "pmaj": "Portfolio Major",
    "pminor": "Portfolio Minor",
}

def _check_item_classification(
    df: pd.DataFrame,
    item_dims: Optional[pd.DataFrame],
    item_master_path: str,
) -> list:
    """Report every item whose classification will fail, and why.

    *item_dims* is the **already-cascaded** dim frame from
    :func:`data_sources.demand_plan_comparison.build_item_dim_frame_cascade`
    — ``qry_pdh.csv`` first, ``RO_Item_Master.csv`` filling its gaps, coalesced
    per field.  Checking against the cascade rather than RO_Item_Master alone
    matters: an item PDH already classifies needs no master row, and flagging
    it would send the planner to edit a file that was never the problem.

    Its ``__item_key`` is built by ``_vectorised_item_key``, whose contract
    (strip, drop a trailing ``.0``) is the same as
    :func:`data_sources.ro_keys.canonical_cell` — the key used here, so the
    two sides cannot mismatch.  ``tests/test_ro_keys.py`` pins that equality.

    Two distinct causes, same visible symptom in the report, so they are listed
    together with a per-item reason rather than split into two findings the
    planner has to correlate:

    * the item appears in neither PDH nor RO_Item_Master;
    * it is known, but a portfolio field is blank in both.
    """
    if "Item #" not in df.columns:
        return []                              # already reported as missing

    if item_dims is None or item_dims.empty or "__item_key" not in item_dims.columns:
        return [Finding(
            code="ITEM_MASTER_UNAVAILABLE",
            severity=SEVERITY_ACK,
            title="The item files could not be read, so items weren’t checked",
            means=(
                "Items are sorted into portfolio rows using **qry_pdh.csv** "
                "and **RO_Item_Master.csv**. Neither could be read this "
                "session, so we cannot tell you which items will classify."
            ),
            fix_where=FIX_IN_FABRIC,
            fix_steps=(
                "This is usually a Fabric sign-in that has expired — check "
                "the status at the top of the **Documentation** page.",
                "If you are signed in, open the link below and confirm "
                "**RO_Item_Master.csv** is still in the folder.",
                "Re-upload your file here to run the check again.",
            ),
            fabric_path=item_master_path,
        )]

    dims = item_dims.set_index("__item_key")
    # Only the fields that place an item on a portfolio row.  Brand and supply
    # format are filled by the cascade in ways that are not a classification
    # failure, so they are not treated as gaps here.
    fields = [c for c in _DIM_PORTFOLIO_FIELDS if c in dims.columns]

    blanks_by_key: dict = {}
    for key, row in dims[fields].iterrows():
        blank = [
            _DIM_FIELD_LABELS[c] for c in fields
            if not str(row[c]).strip()
            or str(row[c]).strip().lower() in ("nan", "none")
        ]
        # Last row wins, matching the cascade's own duplicate handling.
        blanks_by_key[str(key)] = blank
    known = set(blanks_by_key)

    desc = (df["Item Desc"].astype(str) if "Item Desc" in df.columns
            else pd.Series([""] * len(df), index=df.index))
    file_keys = df["Item #"].map(canonical_cell)

    # raw item -> [description, row count, why, what to fill in]
    problems: dict = {}
    for idx, key in file_keys.items():
        if not key:
            continue
        raw = str(df.at[idx, "Item #"]).strip()
        if key not in known:
            why = "Not in qry_pdh.csv or RO_Item_Master.csv"
            todo = ("Add a row to RO_Item_Master.csv: Item #, Item Desc, "
                    + ", ".join(_DIM_FIELD_LABELS.values()))
        else:
            blank = blanks_by_key[key]
            if not blank:
                continue                       # properly classified
            why = f"Known, but {', '.join(blank)} is blank in both files"
            todo = "Fill in " + ", ".join(blank)
        entry = problems.setdefault(raw, [str(desc.at[idx]).strip(), 0, why, todo])
        entry[1] += 1

    if not problems:
        return []

    rows = [(item, d or "—", n, why, todo)
            for item, (d, n, why, todo) in sorted(problems.items())]
    n_absent = sum(1 for r in rows if r[3].startswith("Not in"))
    n_blank = len(rows) - n_absent
    detail = " · ".join(filter(None, [
        f"{n_absent} not in either file" if n_absent else "",
        f"{n_blank} known but unclassified" if n_blank else "",
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
    item_dims: Optional[pd.DataFrame] = None,
    item_master_path: str = "RO Tracking/RO_Item_Master.csv",
    config=None,
) -> PreflightResult:
    """Validate an uploaded ``Distribution_Tracker.csv`` before anything runs.

    Parameters
    ----------
    file_bytes
        The raw uploaded bytes.
    item_dims
        The cascaded per-item dim frame from
        :func:`data_sources.demand_plan_comparison.build_item_dim_frame_cascade`
        (``qry_pdh.csv`` first, ``RO_Item_Master.csv`` filling its gaps).
        Pass ``None`` when Fabric is unreachable — the check then reports that
        it could not run rather than silently passing.
    item_master_path
        Lakehouse path of RO_Item_Master, echoed into the finding so the UI can
        build a deep link.
    config
        The planner's current :class:`RoRulesConfig`.  Decides which rows are
        in scope for the row-level checks, so retuning the rules retunes what
        gets validated.  ``None`` → the canonical defaults.

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

    # ── File-level checks: the whole upload, in scope or not ─────────────
    # A missing Month column or an absent required header breaks the merge
    # itself, so these are not row-scoped.
    result.findings.extend(_check_month_column(df))
    result.findings.extend(_check_columns(df))

    # ── Row-level checks: only rows that could reach RO_Seed ─────────────
    # Scrutinising a row the pipeline is about to discard — an item already
    # reflected in APS, a declined programme, a zero-probability line — blocks
    # the run over data that cannot affect the report.  ``seed_scope_mask`` is
    # a deliberate superset of what the pipeline keeps, so an unreadable gate
    # cell leaves its row IN scope and still gets checked.
    cfg = config or RoRulesConfig.default()
    in_scope = seed_scope_mask(df, config=cfg)
    scoped = df.loc[in_scope]                  # index preserved → Excel rows
    result.rows_in_scope = int(len(scoped))
    result.findings.extend(_scope_note(df, scoped, cfg))

    # Ordered by what breaks the report worst, so the first thing a planner
    # reads is the thing most worth fixing.
    result.findings.extend(_check_volume(scoped))
    result.findings.extend(_check_probability(scoped))
    result.findings.extend(_check_ship_dates(scoped))
    result.findings.extend(
        _check_item_classification(scoped, item_dims, item_master_path)
    )
    result.findings.extend(_check_money(scoped))
    return result


# ── RO_Item_Master.csv ───────────────────────────────────────────────────────

#: Without these the file classifies nothing: ``Item #`` is the join key and
#: the two Portfolio fields are what place an item on a report row.
ITEM_MASTER_REQUIRED: tuple = ("Item #", "Portfolio Major", "Portfolio Minor")

#: Carried along and useful, but no total depends on them.
ITEM_MASTER_OPTIONAL: tuple = ("Item Desc", "Brand Category", "Supply Format")


def check_ro_item_master(
    file_bytes: bytes,
    *,
    current_master_df: Optional[pd.DataFrame] = None,
) -> PreflightResult:
    """Validate a replacement ``RO_Item_Master.csv`` before it overwrites Fabric.

    This upload is more dangerous than the tracker: it overwrites a shared
    reference file every downstream classification reads, and a mistake stays
    invisible until the next report comes out with items under no portfolio
    row.  So the checks lean on the two things that actually break it — a
    missing key column, and losing items the current file already classifies.

    Parameters
    ----------
    current_master_df
        The file being replaced.  Supplied so the check can warn when the new
        version drops items the old one covered — the classic symptom of
        editing a filtered view and uploading that instead of the full list.
    """
    result = PreflightResult()

    try:
        df = pd.read_csv(io.BytesIO(file_bytes), **_READ_KW)
    except Exception as exc:  # noqa: BLE001
        result.findings.append(Finding(
            code="CANNOT_READ",
            severity=SEVERITY_BLOCK,
            title="This file could not be opened as a CSV",
            means="Nothing was uploaded — the file itself has to read first.",
            fix_where=FIX_IN_EXCEL,
            fix_steps=(
                "In Excel choose **File → Save As** and pick "
                "**CSV UTF-8 (Comma delimited) (*.csv)**.",
                "Upload the saved CSV — not an .xlsx renamed to .csv.",
            ),
        ))
        result.findings.append(Finding(
            code="CANNOT_READ_DETAIL", severity=SEVERITY_INFO,
            title="Technical detail (for IT, if you need to ask)",
            means=f"{type(exc).__name__}: {exc}",
        ))
        return result

    df.columns = [str(c).strip() for c in df.columns]
    result.parsed = df
    result.row_count = len(df)
    result.rows_in_scope = len(df)

    if df.empty:
        result.findings.append(Finding(
            code="NO_ROWS",
            severity=SEVERITY_BLOCK,
            title="The file has headers but no items",
            means=(
                "Uploading it would leave every item unclassified — every "
                "portfolio row in the report would go blank."
            ),
            fix_where=FIX_IN_EXCEL,
            fix_steps=("Check you saved the sheet with its rows, then upload again.",),
        ))
        return result

    missing = [c for c in ITEM_MASTER_REQUIRED if c not in df.columns]
    if missing:
        result.findings.append(Finding(
            code="MISSING_COLUMNS",
            severity=SEVERITY_BLOCK,
            title=f"{len(missing)} column(s) the file cannot work without",
            means=(
                "**Item #** is how items are matched, and the two **Portfolio** "
                "columns are what put an item on a row in the report. Without "
                "them this file classifies nothing."
            ),
            fix_where=FIX_IN_EXCEL,
            fix_steps=(
                "Open your file in Excel and look at the header row (row 1).",
                "Add each column below, spelled exactly as shown — capitals "
                "and spaces included.",
                "Easier: download the current file with the button above and "
                "edit that copy. Then the headers are already right.",
                "Save as CSV (UTF-8) and upload again.",
            ),
            cells=pd.DataFrame({"Missing column": missing}),
        ))
        return result                          # later checks need these columns

    optional_missing = [c for c in ITEM_MASTER_OPTIONAL if c not in df.columns]
    if optional_missing:
        result.findings.append(Finding(
            code="MISSING_OPTIONAL_COLUMNS",
            severity=SEVERITY_ACK,
            title=f"{len(optional_missing)} extra column(s) are missing",
            means=(
                "Items will still classify correctly — these fields just come "
                "through blank wherever they are shown."
            ),
            fix_where=FIX_IN_EXCEL,
            fix_steps=("Add them if you want those fields filled, or tick the "
                       "box below to upload without them.",),
            cells=pd.DataFrame({"Missing column": optional_missing}),
        ))

    keys = df["Item #"].map(canonical_cell)

    blank_key = [_excel_row(i) for i, key in keys.items() if not key]
    if blank_key:
        result.findings.append(Finding(
            code="BLANK_ITEM_NUMBER",
            severity=SEVERITY_ACK,
            title=f"{len(blank_key)} row(s) have no item number",
            means="Those rows classify nothing and will simply be ignored.",
            fix_where=FIX_IN_EXCEL,
            fix_steps=("Fill in the item number, or delete the empty rows.",),
            cells=pd.DataFrame({"Excel row": blank_key}),
        ))

    dupes = keys[keys.ne("")].duplicated(keep=False)
    if bool(dupes.any()):
        dup_items = sorted(set(keys[keys.ne("")][dupes]))
        result.findings.append(Finding(
            code="DUPLICATE_ITEMS",
            severity=SEVERITY_ACK,
            title=f"{len(dup_items)} item(s) appear more than once",
            means=(
                "Only the **last** row for each item is used. If the copies "
                "disagree, the one furthest down the file quietly wins."
            ),
            fix_where=FIX_IN_EXCEL,
            fix_steps=(
                "Find each item below in your file (Ctrl+F).",
                "Keep the correct row and delete the others.",
                "Save as CSV (UTF-8) and upload again.",
            ),
            cells=pd.DataFrame({"Item #": dup_items}),
        ))

    unclassified = []
    for idx in df.index:
        if not keys[idx]:
            continue
        blank = [
            c for c in ("Portfolio Major", "Portfolio Minor")
            if not str(df.at[idx, c]).strip()
        ]
        if blank:
            unclassified.append(
                (_excel_row(idx), str(df.at[idx, "Item #"]).strip(), ", ".join(blank))
            )
    if unclassified:
        result.findings.append(Finding(
            code="UNCLASSIFIED_ROWS",
            severity=SEVERITY_ACK,
            title=f"{len(unclassified)} item(s) have a blank Portfolio field",
            means=(
                "Those items will count in Total B2C but appear under no "
                "portfolio row, so the portfolio lines will not add up to the "
                "total."
            ),
            fix_where=FIX_IN_EXCEL,
            fix_steps=("Fill in the blank Portfolio cells listed below, or "
                       "upload now and fix them later.",),
            cells=pd.DataFrame(
                unclassified, columns=["Excel row", "Item #", "Blank field(s)"],
            ),
        ))

    if (current_master_df is not None and not current_master_df.empty
            and "Item #" in current_master_df.columns):
        had = set(current_master_df["Item #"].map(canonical_cell)) - {""}
        lost = sorted(had - (set(keys) - {""}))
        if lost:
            result.findings.append(Finding(
                code="ITEMS_DROPPED",
                severity=SEVERITY_ACK,
                title=f"{len(lost)} item(s) in the current file are missing from yours",
                means=(
                    "This upload **replaces** the whole file, so those items "
                    "would stop being classified. Fine if you meant to retire "
                    "them — but it is also exactly what happens when a "
                    "filtered view is uploaded instead of the full list."
                ),
                fix_where=FIX_IN_EXCEL,
                fix_steps=(
                    "If you edited a filtered view, go back and upload the "
                    "**whole** list instead.",
                    "Safest habit: download the current file, edit that copy, "
                    "upload it back — then nothing can go missing by accident.",
                    "If you really are retiring these items, tick the box "
                    "below and upload.",
                ),
                cells=pd.DataFrame({"Item # no longer present": lost}),
            ))

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
    "_DIM_FIELD_LABELS",
    "Finding",
    "PreflightResult",
    "check_distribution_tracker",
    "check_ro_item_master",
    "ITEM_MASTER_REQUIRED",
    "ITEM_MASTER_OPTIONAL",
]
