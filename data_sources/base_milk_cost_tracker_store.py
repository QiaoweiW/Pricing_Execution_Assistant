"""
OneLake-backed store for ``base_milk_cost_monthly_tracker.csv``.

Schema
------
``Item`` (str), ``Item Description`` (str), ``End Month Milk Cost`` (float),
``End Month`` (date — first-of-month).

Built incrementally each time the Market Barometer's **Confirm** click
finishes a milk-mover run where the slicer's End Month is exactly one
calendar month after the Start Month.  The stored file is an
audit-friendly history of per-item milk-cost-by-end-month — it does
NOT drive any downstream calculation today; the live consumer
(``Product_Milk Base Cost.csv``) is overwritten directly by the
Market Barometer's Refresh handler.

Overwrite semantics (May-2026 contract change)
----------------------------------------------
``upsert_rows_for_end_month`` is the sole writer.  The earlier
append-only-when-month-is-new gate has been retired in favour of
overwrite-allowed semantics:

    Confirm in the Market Barometer page is the explicit, user-initiated
    trigger; the page enforces the "End = Start + 1" gate before
    calling this store.  Idempotency lives at the trigger layer, not at
    the storage layer, so an operator who re-Confirms an End Month after
    fixing a usage row gets a truthful update rather than a silent skip.

Storage layout
--------------
``Files/Milk_cost_tracker/base_milk_cost_monthly_tracker.csv``

Located in the same Pricing Lakehouse as the milk-mover blobs.  An
optional ``[fabric_base_milk_cost_tracker]`` block in ``secrets.toml``
may override workspace/lakehouse if needed.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from data_sources import fabric_lakehouse_io as _io


logger = logging.getLogger(__name__)


# ── Public errors ────────────────────────────────────────────────────────────

class BaseMilkCostTrackerError(RuntimeError):
    """Raised on configuration / auth / I/O failure for this store."""


# ── Constants ────────────────────────────────────────────────────────────────

_SECRETS_SECTION: str = "fabric_base_milk_cost_tracker"

_BLOB_PATH: str = "Milk_cost_tracker/base_milk_cost_monthly_tracker.csv"

# Canonical column order — used for both the empty-frame init and the
# read-validate path. We keep "End Month" as the trailing column so a
# human eye scanning the CSV in OneLake sees Item → Description → Cost
# → Month, which reads naturally.
COL_ITEM:        str = "Item"
COL_ITEM_DESC:   str = "Item Description"
COL_END_COST:    str = "End Month Milk Cost"
COL_END_MONTH:   str = "End Month"
ALL_COLUMNS: tuple[str, ...] = (COL_ITEM, COL_ITEM_DESC, COL_END_COST, COL_END_MONTH)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _normalise_end_month(value) -> Optional[pd.Timestamp]:
    """Parse ``value`` into a ``pd.Timestamp`` normalised to the 1st of the month.

    Returns ``None`` for unparseable input — callers drop those rows.
    """
    if value is None:
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return pd.Timestamp(ts).normalize().replace(day=1)


def _stringify_end_month(ts: pd.Timestamp) -> str:
    """Render an End Month value as ``YYYY-MM-DD``.

    We deliberately use ISO-8601 in the stored CSV (not the M/D/YYYY
    format the Mover tracker uses) so a future SQL-style consumer doing
    a string equality check works without a parse step. The page UI can
    still re-render it any way it wants on read.
    """
    return ts.strftime("%Y-%m-%d")


def _validate_columns(df: pd.DataFrame) -> None:
    """Raise if ``df`` is missing any of the required columns."""
    missing = [c for c in ALL_COLUMNS if c not in df.columns]
    if missing:
        raise BaseMilkCostTrackerError(
            f"base_milk_cost_monthly_tracker.csv is missing required columns "
            f"{missing!r}. Expected exactly: {list(ALL_COLUMNS)}."
        )


def _drop_rows_for_end_month(df: pd.DataFrame, em: pd.Timestamp) -> pd.DataFrame:
    """Return a copy of ``df`` with every row whose End Month equals ``em`` dropped.

    Used by :func:`upsert_rows_for_end_month` to clear an existing month
    before inserting the new payload.
    """
    if df is None or df.empty or COL_END_MONTH not in df.columns:
        return df.copy() if df is not None else pd.DataFrame(columns=list(ALL_COLUMNS))
    parsed = df[COL_END_MONTH].apply(_normalise_end_month)
    keep = parsed != em
    return df.loc[keep].reset_index(drop=True)


def _coerce_payload(rows: pd.DataFrame, end_month: pd.Timestamp) -> pd.DataFrame:
    """Coerce ``rows`` to the canonical schema, fixing types and dropping NaNs.

    The page passes us a slice of ``milk_usage_with_movers_df`` containing
    the 3 raw columns plus whatever extras came along; this normalises
    to exactly the 4 stored columns and stamps every row with ``end_month``.
    """
    if rows is None or rows.empty:
        return pd.DataFrame(columns=list(ALL_COLUMNS))

    df = rows.copy()
    df.columns = [str(c).strip() for c in df.columns]

    needed = (COL_ITEM, COL_ITEM_DESC, COL_END_COST)
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise BaseMilkCostTrackerError(
            f"Cannot build base_milk_cost_monthly_tracker upsert payload: "
            f"missing source columns {missing!r}. "
            f"Expected at least: {list(needed)}."
        )

    out = pd.DataFrame({
        COL_ITEM:      df[COL_ITEM].astype(str).str.strip(),
        COL_ITEM_DESC: df[COL_ITEM_DESC].astype(str).str.strip(),
        COL_END_COST:  pd.to_numeric(df[COL_END_COST], errors="coerce"),
    })
    # Drop rows that can't usefully participate in a join (no item or no cost).
    out = out[(out[COL_ITEM] != "") & (out[COL_ITEM] != "nan")]
    out = out.dropna(subset=[COL_END_COST])
    out[COL_END_MONTH] = _stringify_end_month(end_month)
    return out[list(ALL_COLUMNS)].reset_index(drop=True)


# ── Public API ───────────────────────────────────────────────────────────────

def read_tracker_df() -> pd.DataFrame:
    """Return the tracker table as a DataFrame, or an empty one when absent."""
    try:
        df, _etag = _io.read_csv(_SECRETS_SECTION, _BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise BaseMilkCostTrackerError(str(exc)) from exc
    if df is None or df.empty:
        return pd.DataFrame(columns=list(ALL_COLUMNS))
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    _validate_columns(df)
    return df


def upsert_rows_for_end_month(
    rows: pd.DataFrame,
    end_month,
) -> tuple[int, bool]:
    """Insert or overwrite the rows for ``end_month`` in one read-modify-write.

    Parameters
    ----------
    rows
        DataFrame with at minimum ``Item``, ``Item Description``, and
        ``End Month Milk Cost`` columns. Extra columns are ignored.
    end_month
        Anything ``pd.to_datetime`` accepts — normalised to first-of-month.

    Returns
    -------
    (rows_written, was_overwrite)
        ``rows_written`` is the number of rows ultimately stamped to the
        file for ``end_month``; ``was_overwrite`` is True iff the End
        Month was already present in the tracker (the existing rows were
        dropped and the new payload took their place), False when the
        End Month was new.

    Concurrency
    -----------
    Uses ``fabric_lakehouse_io.update_csv`` for a read-modify-write
    cycle with bounded ETag-conflict retries. Two simultaneous Confirm
    clicks on the same End Month therefore collapse into one canonical
    write.
    """
    em = _normalise_end_month(end_month)
    if em is None:
        raise BaseMilkCostTrackerError(
            f"end_month {end_month!r} is not parseable — "
            "expected something pd.to_datetime accepts."
        )

    payload = _coerce_payload(rows, em)
    if payload.empty:
        return 0, False

    rows_written = 0
    was_overwrite = False

    def _mutate(current: Optional[pd.DataFrame]) -> pd.DataFrame:
        nonlocal rows_written, was_overwrite
        if current is None or current.empty:
            existing = pd.DataFrame(columns=list(ALL_COLUMNS))
        else:
            existing = current.copy()
            existing.columns = [str(c).strip() for c in existing.columns]
            _validate_columns(existing)
        # Detect overwrite BEFORE dropping so the caller can render the
        # right verb in the success caption.
        parsed = existing[COL_END_MONTH].apply(_normalise_end_month)
        was_overwrite = bool((parsed == em).any())
        # Drop any pre-existing rows for the target End Month, then append.
        existing = _drop_rows_for_end_month(existing, em)
        rows_written = len(payload)
        return pd.concat([existing, payload], ignore_index=True)

    try:
        _io.update_csv(
            _SECRETS_SECTION,
            _BLOB_PATH,
            _mutate,
            initial_default=pd.DataFrame(columns=list(ALL_COLUMNS)),
        )
    except _io.LakehouseIOError as exc:
        raise BaseMilkCostTrackerError(str(exc)) from exc

    return rows_written, was_overwrite


def get_store_label() -> str:
    """Short human-readable label for UI captions."""
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=_BLOB_PATH,
    ).display


__all__ = [
    "BaseMilkCostTrackerError",
    "COL_ITEM",
    "COL_ITEM_DESC",
    "COL_END_COST",
    "COL_END_MONTH",
    "ALL_COLUMNS",
    "read_tracker_df",
    "upsert_rows_for_end_month",
    "get_store_label",
]
