"""
OneLake-backed store for the cumulative ``mover_details_table.csv``.

The Monthly Movers section of the Market Barometer page writes a Month's
worth of mover details into a shared, audit-friendly file in the Pricing
Lakehouse.  The lakehouse copy is the canonical history of every month's
per-SKU mover values; the page treats it as the source of truth.

Storage layout
--------------
``Files/Monthly_Mover_Reporting/mover_details_table.csv``

Public API
----------
* :func:`read_table_df`       — current table (empty when absent).
* :func:`has_month`           — quick membership test on the Month column.
* :func:`upsert_for_month`    — overwrite-allowed write for ``new_month``:
                                drops any existing rows for that month
                                and appends the new payload as a single
                                ETag-guarded read-modify-write cycle.
* :func:`get_store_label`     — UI caption helper.

Overwrite semantics (May-2026 contract change)
----------------------------------------------
``upsert_for_month`` is the sole writer.  The earlier "append-only,
month-must-be-new" gate has been retired in favour of overwrite-allowed
semantics because the Confirm button in the Market Barometer page now
owns the trigger:

* If the editable Movers Non-Milk Tracker grew by one row (row-count
  delta > 0) AND the user clicked **Confirm**, the page calls this
  function.
* When ``new_month`` already exists in the file, its rows are
  overwritten with the payload.  The new history is therefore always a
  truthful reflection of the latest confirmed mover values for that
  month.

Concurrency
-----------
Writes go through :func:`fabric_lakehouse_io.update_csv`, which provides
ETag-based optimistic concurrency with bounded retries.  Two simultaneous
Confirm clicks therefore collapse into a single canonical write.

Configuration
-------------
Reads ``workspace`` and ``lakehouse`` from
``[fabric_mover_details_table]`` when present, falling back to
``[fabric_htst]`` so deployments don't need to duplicate config when
every artefact lives in the same Pricing Lakehouse.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from data_sources import fabric_lakehouse_io as _io


logger = logging.getLogger(__name__)


# ── Public errors ────────────────────────────────────────────────────────────


class MoverDetailsTableStoreError(RuntimeError):
    """Raised on configuration / auth / I/O failure for this store.

    Wraps :class:`fabric_lakehouse_io.LakehouseIOError` so the page renders
    a single clean error path instead of leaking generic OneLake stack traces.
    """


# ── Constants ────────────────────────────────────────────────────────────────

_SECRETS_SECTION: str = "fabric_mover_details_table"

# Folder + file matches the lakehouse URL the Pricing team shared.
_FOLDER_PREFIX: str = "Monthly_Mover_Reporting"
_BLOB_PATH:     str = f"{_FOLDER_PREFIX}/mover_details_table.csv"

# Column name carried on every appended row to identify which month the
# row was generated for.  Defined once here so the page renderer and the
# downstream consumer reference the same string.
COL_MONTH: str = "Month"


# ── Internal helpers ─────────────────────────────────────────────────────────


def _normalise_month(value) -> Optional[pd.Timestamp]:
    """Parse ``value`` into a first-of-month ``pd.Timestamp`` (None on failure)."""
    if value is None:
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return pd.Timestamp(ts).normalize().replace(day=1)


def _stringify_month(ts: pd.Timestamp) -> str:
    """Render a Month value as ``YYYY-MM-01`` for consistent string equality.

    ISO format keeps duplicate detection trivial (string equality) and
    makes the cumulative file scan cleanly in any spreadsheet locale.
    """
    return ts.strftime("%Y-%m-01")


def _strip_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` with header whitespace stripped."""
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    return out


def _existing_months(df: pd.DataFrame) -> set[pd.Timestamp]:
    """Return the set of first-of-month Timestamps already in ``df``."""
    if df is None or df.empty or COL_MONTH not in df.columns:
        return set()
    parsed = df[COL_MONTH].apply(_normalise_month).dropna()
    return set(parsed.tolist())


def _drop_rows_for_month(df: pd.DataFrame, month: pd.Timestamp) -> pd.DataFrame:
    """Return a copy of ``df`` with every row whose Month equals ``month`` dropped.

    Used by :func:`upsert_for_month` to clear an existing month before
    inserting the new payload.  Tolerates the same mixed Month formats
    the cumulative file has carried over time — first parse-then-compare.
    """
    if df is None or df.empty or COL_MONTH not in df.columns:
        return df.copy() if df is not None else pd.DataFrame()
    parsed = df[COL_MONTH].apply(_normalise_month)
    keep = parsed != month
    return df.loc[keep].reset_index(drop=True)


# ── Public API ───────────────────────────────────────────────────────────────


def read_table_df() -> pd.DataFrame:
    """Return the current cumulative table, or an empty DataFrame when absent."""
    try:
        df, _etag = _io.read_csv(_SECRETS_SECTION, _BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise MoverDetailsTableStoreError(str(exc)) from exc
    if df is None or df.empty:
        return pd.DataFrame()
    return _strip_columns(df)


def has_month(target_month) -> bool:
    """Return True when ``target_month`` is already present in the table."""
    em = _normalise_month(target_month)
    if em is None:
        return False
    return em in _existing_months(read_table_df())


def upsert_for_month(
    rows: pd.DataFrame,
    new_month,
) -> tuple[int, bool]:
    """Insert or overwrite the rows for ``new_month`` in one read-modify-write.

    Parameters
    ----------
    rows
        DataFrame to write.  Must already include the :data:`COL_MONTH`
        column populated to ``new_month``; the page builds this column
        before calling.  Other columns may be anything — the lakehouse
        file is column-tolerant.
    new_month
        Anything ``pd.to_datetime`` accepts; normalised to first-of-month.

    Returns
    -------
    (rows_written, was_overwrite)
        ``rows_written`` is the number of rows ultimately stamped to the
        file for ``new_month``; ``was_overwrite`` is True iff the month
        was already present in the file (the existing rows were dropped
        and the new payload took their place), False when the month was
        new (pure append).

    Behaviour
    ---------
    Overwrite-allowed by design — re-Confirming a month replaces the
    file's rows for that month with the latest payload.  This matches
    the May-2026 contract where Confirm in the Market Barometer page
    is the explicit, user-initiated trigger and idempotency lives at
    the trigger layer (the row-count-delta gate) rather than at the
    storage layer.
    """
    em = _normalise_month(new_month)
    if em is None:
        raise MoverDetailsTableStoreError(
            f"new_month {new_month!r} is not parseable — "
            "expected something pd.to_datetime accepts."
        )
    if rows is None or rows.empty:
        return 0, False

    # Defensive normalisation: ensure the Month column on the incoming
    # rows is the canonical ISO-1st-of-month string we use for storage.
    payload = _strip_columns(rows)
    payload[COL_MONTH] = _stringify_month(em)

    rows_written = 0
    was_overwrite = False

    def _mutate(current: Optional[pd.DataFrame]) -> pd.DataFrame:
        nonlocal rows_written, was_overwrite
        if current is None or current.empty:
            existing = pd.DataFrame()
        else:
            existing = _strip_columns(current)

        was_overwrite = em in _existing_months(existing)
        # Drop any pre-existing rows for the target month before insert —
        # this is what makes the operation an "upsert" rather than a
        # blind append.  No-op when the month was never tracked before.
        existing = _drop_rows_for_month(existing, em)

        # Align columns: union of (existing ∪ payload), preserving the
        # order existing rows already established and extending with any
        # new columns from the payload at the end.  This protects against
        # column drift between months without losing data.
        if existing.empty:
            combined = payload.copy()
        else:
            ordered_cols = list(dict.fromkeys(
                list(existing.columns) + list(payload.columns)
            ))
            combined = pd.concat(
                [
                    existing.reindex(columns=ordered_cols),
                    payload.reindex(columns=ordered_cols),
                ],
                ignore_index=True,
            )

        rows_written = len(payload)
        return combined

    try:
        _io.update_csv(
            _SECRETS_SECTION,
            _BLOB_PATH,
            _mutate,
            initial_default=pd.DataFrame(),
        )
    except _io.LakehouseIOError as exc:
        raise MoverDetailsTableStoreError(str(exc)) from exc

    return rows_written, was_overwrite


def get_store_label() -> str:
    """Return a short human-readable label of where the data lives."""
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=_BLOB_PATH,
    ).display


__all__ = [
    "MoverDetailsTableStoreError",
    "COL_MONTH",
    "read_table_df",
    "has_month",
    "upsert_for_month",
    "get_store_label",
]
