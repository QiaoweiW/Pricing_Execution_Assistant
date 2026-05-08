"""
OneLake-backed store for the cumulative ``mover_details_table.csv``.

The Monthly Movers section of the Market Barometer page now appends one
month's worth of mover details to a shared, audit-friendly file in the
Pricing_Lakehouse.  The lakehouse copy is the canonical history of every
month's per-SKU mover values; the page never overwrites a month that has
already been pushed.

Storage layout
--------------
``Files/Monthly_Mover_Reporting/mover_details_table.csv``

Public API
----------
* :func:`read_table_df`              — current table (empty when absent).
* :func:`has_month`                  — quick membership test on the Month column.
* :func:`append_for_month_if_new`    — append the rows for ``new_month`` only
                                       when the month is not yet present.
* :func:`get_store_label`            — UI caption helper.

Concurrency
-----------
Writes go through :func:`fabric_lakehouse_io.update_csv`, which provides
ETag-based optimistic concurrency with bounded retries.  Two simultaneous
"Refresh" clicks for the same brand-new month therefore collapse into a
single append.

Configuration
-------------
Reads ``workspace`` and ``lakehouse`` from
``[fabric_mover_details_table]`` when present, falling back to
``[fabric_htst]`` so deployments don't need to duplicate config when
every artefact lives in the same Pricing_Lakehouse.
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

# Folder + file matches the lakehouse URL the user shared.
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


def append_for_month_if_new(
    rows: pd.DataFrame,
    new_month,
) -> tuple[int, bool]:
    """Append ``rows`` for ``new_month`` if (and only if) that month is new.

    Parameters
    ----------
    rows
        DataFrame to append.  Must already include the :data:`COL_MONTH`
        column populated to ``new_month``; the page builds this column
        before calling.  Other columns may be anything — the lakehouse
        file is column-tolerant.
    new_month
        Anything ``pd.to_datetime`` accepts; normalised to first-of-month.

    Returns
    -------
    (rows_appended, month_was_new)
        ``rows_appended`` is the number of rows actually written;
        ``month_was_new`` is True iff the call resulted in a new month
        being appended.

    Append-only by design — pre-existing months are never overwritten,
    matching the contract requested in the May-2026 spec.
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

    rows_appended = 0
    month_was_new = False

    def _mutate(current: Optional[pd.DataFrame]) -> pd.DataFrame:
        nonlocal rows_appended, month_was_new
        if current is None or current.empty:
            existing = pd.DataFrame()
        else:
            existing = _strip_columns(current)

        if em in _existing_months(existing):
            return existing  # already tracked — leave history alone

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

        rows_appended = len(payload)
        month_was_new = True
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

    return rows_appended, month_was_new


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
    "append_for_month_if_new",
    "get_store_label",
]
