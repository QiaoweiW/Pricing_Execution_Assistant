"""
OneLake-backed store helpers for Bid Asset Intelligence.

Responsibilities
----------------
* List and read bid-asset CSV files under ``Files/Program_Bid_Management``.
* Normalise schema for Program Implementation tracking columns.
* Persist full-file edits back to OneLake using optimistic concurrency (ETag).
* Provide a canonical Program Implementation Tracker projection and sorting.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

from data_sources import fabric_lakehouse_io as _io


class BidAssetStoreError(RuntimeError):
    """Raised on configuration/auth/I-O failures for bid asset storage."""


_SECRETS_SECTION: str = "fabric_htst"
_FOLDER: str = "Program_Bid_Management"

# ── Canonical column names ────────────────────────────────────────────────────
# These are the column identifiers used everywhere inside the application. They
# intentionally MATCH the names in the source-of-truth Lakehouse CSV so that
# read → edit → publish is a true round-trip without renaming columns.
#
# The page renames a few of these to friendlier labels at display time only
# (see `_render_program_tracker` in the page module).
COL_STATUS: str = "Price Implement Status"
COL_BASE_PLAN: str = "1st Base Plan Cycle"
COL_PRICE_IMPLEMENT_TIME: str = "Price Implement Time"

# Numeric financial columns. Source CSVs sometimes store these as currency
# strings ("$424,236", "$(3,846)"), but downstream charts/aggregations need
# floats. ``normalise_bid_df`` parses each of these in place on read.
FINANCIAL_COLS: tuple[str, ...] = (
    "Volume (lbs)",
    "FOB Revenue $/Yr",
    "PCM $/Yr",
    "GP $/Yr",
)

# ── Bidirectional column aliases ──────────────────────────────────────────────
# Source CSVs occasionally use a different spelling than the canonical name the
# UI expects. We rename CSV → canonical on read, and canonical → CSV on write,
# so the file's original schema is preserved.
_READ_COLUMN_ALIASES: dict[str, str] = {
    "Rounds": "Round",
}
_WRITE_COLUMN_ALIASES: dict[str, str] = {v: k for k, v in _READ_COLUMN_ALIASES.items()}

# ── Canonical status vocabulary ───────────────────────────────────────────────
STATUS_NOT_STARTED: str = "Not Started"
STATUS_START_SOON: str = "Start Soon"
STATUS_IN_PROGRESS: str = "In Progress"
STATUS_DONE: str = "Done"

# Casefolded aliases for the spelling/punctuation variants that appear in the
# wild (e.g. "Not-started" with a hyphen, "in-progress", etc.). Unknown values
# pass through unchanged so we never silently mangle user data.
_STATUS_ALIASES: dict[str, str] = {
    "not started":     STATUS_NOT_STARTED,
    "not-started":     STATUS_NOT_STARTED,
    "not_started":     STATUS_NOT_STARTED,
    "notstarted":      STATUS_NOT_STARTED,
    "start soon":      STATUS_START_SOON,
    "startsoon":       STATUS_START_SOON,
    "start-soon":      STATUS_START_SOON,
    "in progress":     STATUS_IN_PROGRESS,
    "in-progress":     STATUS_IN_PROGRESS,
    "inprogress":      STATUS_IN_PROGRESS,
    "done":            STATUS_DONE,
    "complete":        STATUS_DONE,
    "completed":       STATUS_DONE,
}

_STATUS_PRIORITY: dict[str, int] = {
    STATUS_NOT_STARTED.casefold(): 0,
    STATUS_START_SOON.casefold(): 1,
    STATUS_IN_PROGRESS.casefold(): 2,
    STATUS_DONE.casefold(): 3,
}


def canonicalise_status(value: object) -> str:
    """Return the canonical form of an Implementation Status value.

    Maps known case/spelling/punctuation variants (e.g. ``"Not-started"``,
    ``"in-progress"``) to the canonical labels exposed as module constants.
    Unknown values are returned unchanged (so e.g. ``"Not Applicable"`` or
    ``"TBD"`` survive without being silently relabelled).
    """
    s = str(value).strip()
    return _STATUS_ALIASES.get(s.casefold(), s)


@dataclass(frozen=True)
class BidAssetFile:
    """Minimal metadata for a bid-asset CSV file in OneLake."""

    name: str
    full_path: str
    etag: Optional[str]
    last_modified: Optional[str]
    size: int


def list_bid_files() -> list[BidAssetFile]:
    """Return bid-asset CSV files sorted newest-first."""
    try:
        files = _io.list_files(_SECRETS_SECTION, _FOLDER, suffix=".csv")
    except _io.LakehouseIOError as exc:
        raise BidAssetStoreError(str(exc)) from exc

    out = [
        BidAssetFile(
            name=f.name,
            full_path=f.full_path,
            etag=f.etag,
            last_modified=f.last_modified,
            size=f.size,
        )
        for f in files
    ]
    return sorted(out, key=lambda f: f.last_modified or "", reverse=True)


def read_bid_file(file_path: str) -> tuple[pd.DataFrame, Optional[str]]:
    """Read one bid CSV and return ``(normalised_df, etag)``.

    The CSV's column names are aliased to the application's canonical names
    (e.g. ``Rounds`` → ``Round``) so the rest of the code can use stable
    identifiers regardless of small spelling differences in the source file.
    """
    try:
        df, etag = _io.read_csv(_SECRETS_SECTION, file_path)
    except _io.LakehouseIOError as exc:
        raise BidAssetStoreError(str(exc)) from exc
    if df is None:
        raise BidAssetStoreError(f"File not found in OneLake: Files/{file_path}")
    return normalise_bid_df(_apply_aliases(df, _READ_COLUMN_ALIASES)), etag


def overwrite_bid_file(file_path: str, df: pd.DataFrame, *, etag: Optional[str]) -> str:
    """Overwrite the full bid CSV with ETag protection and schema normalisation.

    Canonical column names are translated back to the source CSV's original
    spellings (e.g. ``Round`` → ``Rounds``) so a read → edit → publish cycle is
    schema-stable and never mutates the lakehouse file's column headers.
    """
    try:
        return _io.write_csv(
            _SECRETS_SECTION,
            file_path,
            _apply_aliases(normalise_bid_df(df), _WRITE_COLUMN_ALIASES),
            etag=etag,
        )
    except _io.LakehouseIOError as exc:
        raise BidAssetStoreError(str(exc)) from exc


def normalise_bid_df(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure tracker columns exist and carry canonical value shape.

    Note: column-name aliasing is handled separately by ``_apply_aliases``
    in ``read_bid_file``/``overwrite_bid_file``. This function assumes inputs
    already use canonical column names and focuses on value shape only.
    """
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]

    if COL_STATUS not in out.columns:
        out[COL_STATUS] = STATUS_NOT_STARTED
    if COL_BASE_PLAN not in out.columns:
        out[COL_BASE_PLAN] = ""

    # Strip whitespace and replace empty-string with the canonical default.
    # Existing value variants (e.g. "Not-started") are PRESERVED at the
    # storage level — comparison and sort logic uses ``canonicalise_status``
    # so we never rewrite user data behind their back.
    out[COL_STATUS] = (
        out[COL_STATUS]
        .astype(str)
        .str.strip()
        .replace({"": STATUS_NOT_STARTED, "nan": STATUS_NOT_STARTED, "NaN": STATUS_NOT_STARTED})
    )
    out[COL_BASE_PLAN] = out[COL_BASE_PLAN].fillna("").astype(str).str.strip()

    # Parse currency strings → floats for all known financial columns. This
    # is the single source of truth that prevents string/string division
    # errors in downstream chart aggregation and KPI math.
    for col in FINANCIAL_COLS:
        if col in out.columns:
            out[col] = parse_currency_series(out[col])
    return out


def parse_currency_series(series: pd.Series) -> pd.Series:
    """Convert currency strings like ``"$424,236"`` or ``"$(3,846)"`` to floats.

    Already-numeric series are returned unchanged. Negative values written in
    accounting parentheses notation become negative floats. Unparseable cells
    become ``NaN`` so downstream math degrades gracefully instead of raising.
    """
    if pd.api.types.is_numeric_dtype(series):
        return series
    cleaned = (
        series.astype(str)
        .str.strip()
        .str.replace(r"\$", "", regex=True)
        .str.replace(",", "", regex=False)
        .str.replace(" ", "", regex=False)
    )
    is_neg = cleaned.str.startswith("(")
    cleaned = cleaned.str.replace(r"[()]", "", regex=True)
    result = pd.to_numeric(cleaned, errors="coerce")
    return result.where(~is_neg, -result)


def _apply_aliases(df: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
    """Rename columns per *mapping*, skipping pairs that would collide."""
    pairs = {
        src: dst
        for src, dst in mapping.items()
        if src in df.columns and src != dst and dst not in df.columns
    }
    return df.rename(columns=pairs) if pairs else df


def build_program_tracker(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Return Program Implementation Tracker rows with required ordering."""
    df = normalise_bid_df(raw_df)
    if "Status" in df.columns:
        accepted = df["Status"].astype(str).str.strip().str.casefold() == "accept"
        df = df[accepted].copy()
    if df.empty:
        return df

    cols = [
        c
        for c in (
            "Bid Description",
            "Referenced Item",
            "Referenced Item Description",
            "Month",
            "Variable vs Fixed Pricing",
            "Brand",
            COL_PRICE_IMPLEMENT_TIME,
            COL_STATUS,
            COL_BASE_PLAN,
        )
        if c in df.columns
    ]
    tracker = df[cols].copy()
    # Rank using the canonical status form so spelling variants like
    # "Not-started" still resolve to the correct priority bucket.
    tracker["_status_rank"] = (
        tracker.get(COL_STATUS, "")
        .apply(canonicalise_status)
        .str.casefold()
        .map(_STATUS_PRIORITY)
        .fillna(99)
    )
    if COL_PRICE_IMPLEMENT_TIME in tracker.columns:
        tracker["_pit_sort"] = pd.to_datetime(tracker[COL_PRICE_IMPLEMENT_TIME], errors="coerce")
    else:
        tracker["_pit_sort"] = pd.NaT
    tracker = tracker.sort_values(by=["_status_rank", "_pit_sort"], ascending=[True, False], na_position="last")
    return tracker.drop(columns=["_status_rank", "_pit_sort"], errors="ignore").reset_index(drop=True)


def status_is_not_started(value: object) -> bool:
    """Return True when a status value should be highlighted as Not Started."""
    return canonicalise_status(value).casefold() == STATUS_NOT_STARTED.casefold()


def status_is_start_soon(value: object) -> bool:
    """Return True when a status value should be treated as Start Soon."""
    return canonicalise_status(value).casefold() == STATUS_START_SOON.casefold()


def now_iso() -> str:
    """ISO timestamp helper for audit-friendly metadata fields."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

