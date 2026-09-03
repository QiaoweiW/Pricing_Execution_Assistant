"""
Compare a live Oracle price-adjustment read against a fixed VBCS file.

Given the (already filtered) Oracle read result and a "fixed" VBCS file pulled
from the Fabric lakehouse, find rows whose ``adjustmentamount`` disagree —
matched on ``itemname + pricinguom + shiptositename + adjustmentstartdate``
(ship-to case-insensitive; start-date compared at calendar-date granularity so
the two sources' timezone/format differences don't cause false misses).

Three mismatch types are reported:

  AMOUNT_MISMATCH   — key in both, amounts differ by > 4 decimals
  MISSING_IN_ORACLE — key in the file but not in the (filtered) Oracle read
  MISSING_IN_FILE   — key in the Oracle read but not in the file

Output is a report DataFrame in the **Oracle read's column structure** with
three appended columns — ``file_adjustmentamount``, ``delta`` (oracle − file),
``mismatch_type`` — plus a leveled run log that surfaces validation errors and
any deduplication conflicts (a 4-key carrying two different amounts on one side,
which shouldn't happen but is flagged rather than silently resolved).

Pure pandas, Streamlit-free and Fabric-free, so it is unit-testable; the UI
layer (Fabric sign-in gate, file dropdown, download) lives in editor.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

# Canonical match keys + the value column, by canonical (alnum-lower) name.
_KEY_ITEM = "itemname"
_KEY_UOM = "pricinguom"
_KEY_SITE = "shiptositename"
_KEY_DATE = "adjustmentstartdate"
_VAL_AMT = "adjustmentamount"
_REQUIRED = (_KEY_ITEM, _KEY_UOM, _KEY_SITE, _KEY_DATE, _VAL_AMT)

# Appended report columns (after the Oracle read's own columns).
_COL_FILE_AMT = "file_adjustmentamount"
_COL_DELTA = "delta"
_COL_TYPE = "mismatch_type"

_AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
_MISSING_IN_ORACLE = "MISSING_IN_ORACLE"
_MISSING_IN_FILE = "MISSING_IN_FILE"

_ROUND = 4  # amounts compared equal within 4 decimals (per spec)


@dataclass
class CompareLogEntry:
    level: str            # info | warning | error
    text: str


@dataclass
class CompareResult:
    ok: bool
    log: list[CompareLogEntry] = field(default_factory=list)
    report: pd.DataFrame = field(default_factory=pd.DataFrame)
    counts: dict = field(default_factory=dict)

    @property
    def errors(self) -> list[str]:
        return [e.text for e in self.log if e.level == "error"]

    @property
    def warnings(self) -> list[str]:
        return [e.text for e in self.log if e.level == "warning"]


def _norm_text(v: object) -> str:
    """Collapse NBSP/whitespace and strip — for labels, headers, site names."""
    return re.sub(r"\s+", " ", str(v).replace("\xa0", " ")).strip()


def _norm_key(v: object) -> str:
    """Case-insensitive match key (site / UOM)."""
    return _norm_text(v).upper()


def _norm_item(v: object) -> str:
    """Item number as a clean digit string across int / float / str inputs.

    The two sides spell the same item differently — Oracle hands back the
    string ``"340021"`` while a spreadsheet export can carry the int
    ``340021`` or the float ``340021.0``; all three must collapse to one key.
    """
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return re.sub(r"\.0$", "", _norm_text(v))


def _canon(col: object) -> str:
    """Canonical column key: lower-case, alnum only (``Item_Name`` -> ``itemname``)."""
    return re.sub(r"[^a-z0-9]", "", str(col).lower())


def _resolve_columns(df: pd.DataFrame) -> dict[str, str]:
    """Map each canonical name to the frame's first actual column matching it."""
    out: dict[str, str] = {}
    for col in df.columns:
        out.setdefault(_canon(col), col)
    return out


def _norm_date(series: pd.Series) -> pd.Series:
    """Normalize any date representation to a calendar ``date`` (tz-agnostic)."""
    return pd.to_datetime(series, utc=True, errors="coerce").dt.date


def _key_frame(df: pd.DataFrame, cols: dict[str, str]) -> pd.DataFrame:
    """Return a frame of normalized keys + rounded amount, aligned to ``df.index``."""
    return pd.DataFrame({
        "item": df[cols[_KEY_ITEM]].map(_norm_item),
        "uom":  df[cols[_KEY_UOM]].map(_norm_key),
        "site": df[cols[_KEY_SITE]].map(_norm_key),
        "date": _norm_date(df[cols[_KEY_DATE]]),
        "amt":  pd.to_numeric(df[cols[_VAL_AMT]], errors="coerce").round(_ROUND),
    }, index=df.index)


def _dedupe(keys: pd.DataFrame, side: str, log: "list[CompareLogEntry]") -> dict:
    """Build ``{(item,uom,site,date): (index, amt)}``; flag conflicting amounts.

    Keeps the first row per key. If a key carries >1 distinct (rounded) amount,
    that's a data problem — we log it ("deduplication needed") rather than guess.
    """
    lookup: dict[tuple, tuple] = {}
    conflicts: list[tuple] = []
    grouped = keys.groupby(["item", "uom", "site", "date"], dropna=False)
    for key, grp in grouped:
        amts = grp["amt"].dropna().unique()
        if len(amts) > 1:
            conflicts.append(key)
        idx = grp.index[0]
        lookup[key] = (idx, grp.loc[idx, "amt"])
    if conflicts:
        shown = "; ".join(
            f"{i}/{u}/{s}/{d}" for (i, u, s, d) in conflicts[:10]
        )
        more = f" (+{len(conflicts) - 10} more)" if len(conflicts) > 10 else ""
        log.append(CompareLogEntry(
            "warning",
            f"Deduplication needed — {side} has {len(conflicts)} key(s) with "
            f"conflicting amounts; kept the first of each: {shown}{more}.",
        ))
    return lookup


def compare_oracle_to_file(
    oracle_df: pd.DataFrame,
    file_df: pd.DataFrame,
) -> CompareResult:
    """Compare the Oracle read against a fixed VBCS file; return report + log."""
    log: list[CompareLogEntry] = []

    o_cols = _resolve_columns(oracle_df)
    f_cols = _resolve_columns(file_df)
    miss_o = [c for c in _REQUIRED if c not in o_cols]
    miss_f = [c for c in _REQUIRED if c not in f_cols]
    if miss_o:
        log.append(CompareLogEntry("error", f"Oracle read is missing column(s): {miss_o}"))
    if miss_f:
        log.append(CompareLogEntry("error", f"Selected file is missing column(s): {miss_f}"))
    if miss_o or miss_f:
        return CompareResult(ok=False, log=log)

    log.append(CompareLogEntry(
        "info", f"Oracle read: {len(oracle_df):,} rows · VBCS file: {len(file_df):,} rows."))

    o_keys = _key_frame(oracle_df, o_cols)
    f_keys = _key_frame(file_df, f_cols)
    o_map = _dedupe(o_keys, "the Oracle read", log)
    f_map = _dedupe(f_keys, "the file", log)

    # File-side display values (so MISSING_IN_ORACLE rows show real keys, not the
    # normalized form) — first row per key.
    f_disp_cols = [f_cols[_KEY_ITEM], f_cols[_KEY_UOM], f_cols[_KEY_SITE], f_cols[_KEY_DATE]]

    report_cols = list(oracle_df.columns) + [_COL_FILE_AMT, _COL_DELTA, _COL_TYPE]
    rows: list[dict] = []
    counts = {_AMOUNT_MISMATCH: 0, _MISSING_IN_ORACLE: 0, _MISSING_IN_FILE: 0, "matched_ok": 0}

    # Oracle order first (keys present in the read), then file-only keys.
    for key, (o_idx, o_amt) in o_map.items():
        f = f_map.get(key)
        if f is None:
            row = oracle_df.loc[o_idx].to_dict()
            row.update({_COL_FILE_AMT: None, _COL_DELTA: None, _COL_TYPE: _MISSING_IN_FILE})
            rows.append(row); counts[_MISSING_IN_FILE] += 1
            continue
        f_amt = f[1]
        if pd.notna(o_amt) and pd.notna(f_amt) and round(o_amt, _ROUND) == round(f_amt, _ROUND):
            counts["matched_ok"] += 1
            continue
        row = oracle_df.loc[o_idx].to_dict()
        delta = (o_amt - f_amt) if (pd.notna(o_amt) and pd.notna(f_amt)) else None
        row.update({_COL_FILE_AMT: f_amt, _COL_DELTA: delta, _COL_TYPE: _AMOUNT_MISMATCH})
        rows.append(row); counts[_AMOUNT_MISMATCH] += 1

    for key, (f_idx, f_amt) in f_map.items():
        if key in o_map:
            continue
        row = {c: None for c in oracle_df.columns}
        # Populate the four key columns from the file's display values.
        disp = file_df.loc[f_idx, f_disp_cols]
        row[o_cols[_KEY_ITEM]] = disp.iloc[0]
        row[o_cols[_KEY_UOM]] = disp.iloc[1]
        row[o_cols[_KEY_SITE]] = disp.iloc[2]
        row[o_cols[_KEY_DATE]] = disp.iloc[3]
        row.update({_COL_FILE_AMT: f_amt, _COL_DELTA: None, _COL_TYPE: _MISSING_IN_ORACLE})
        rows.append(row); counts[_MISSING_IN_ORACLE] += 1

    report = pd.DataFrame(rows, columns=report_cols)

    counts["oracle_rows"] = len(oracle_df)
    counts["file_rows"] = len(file_df)
    counts["mismatches"] = len(report)
    log.append(CompareLogEntry(
        "info",
        f"Mismatches: {counts[_AMOUNT_MISMATCH]} amount, "
        f"{counts[_MISSING_IN_ORACLE]} missing-in-Oracle, "
        f"{counts[_MISSING_IN_FILE]} missing-in-file "
        f"({counts['matched_ok']} matched OK).",
    ))
    if report.empty:
        log.append(CompareLogEntry("info", "No mismatches — every compared row agrees within 4 decimals."))

    return CompareResult(ok=True, log=log, report=report, counts=counts)


def report_to_csv_bytes(report: pd.DataFrame) -> bytes:
    return report.to_csv(index=False).encode("utf-8")


__all__ = [
    "CompareLogEntry",
    "CompareResult",
    "compare_oracle_to_file",
    "report_to_csv_bytes",
]
