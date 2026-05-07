"""
OneLake-backed store for ``base_milk_cost_monthly_tracker.csv``.

Schema
------
``Item`` (str), ``Item Description`` (str), ``End Month Milk Cost`` (float),
``End Month`` (date — first-of-month).

Built incrementally each time the Market Barometer's "Refresh" pipeline
finishes a milk-mover run with a NEW end-month selection. The stored
file is the single source of truth for every per-month milk-cost lookup
downstream — currently used by:

* The "Base Milk Cost per Gallon" auto-update on Product_Milk Base Cost
  (see ``data_sources/product_milk_base_cost_updater.py``).
* Future audit dashboards that need per-item milk-cost history.

Append-only by design
---------------------
``append_rows_for_end_month`` looks at the End Month value of the
incoming rows and writes ONLY when that month is not already present in
the file. This honours the contract requested in the migration plan:

    "whenever both a new milk_mover csv is generated AND a new end month
     is selected that doesn't exist in this file yet, append the new rows."

Existing months are left untouched — even if the underlying milk_mover
values changed (rare; happens only when someone manually edits
milk_mover_tracker.json). If we ever want overwrite-on-existing semantics,
add a ``replace_for_end_month`` helper alongside this one.

Storage layout
--------------
``Files/Milk_cost_tracker/base_milk_cost_monthly_tracker.csv``

Located in the same Pricing_Lakehouse as the milk-mover blobs. An
optional ``[fabric_base_milk_cost_tracker]`` block in secrets.toml may
override workspace/lakehouse if needed.
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


def _existing_end_months(df: pd.DataFrame) -> set[pd.Timestamp]:
    """Return the set of End-Month values already in ``df`` (as Timestamps)."""
    if df is None or df.empty or COL_END_MONTH not in df.columns:
        return set()
    parsed = df[COL_END_MONTH].apply(_normalise_end_month).dropna()
    return set(parsed.tolist())


def _validate_columns(df: pd.DataFrame) -> None:
    """Raise if ``df`` is missing any of the required columns."""
    missing = [c for c in ALL_COLUMNS if c not in df.columns]
    if missing:
        raise BaseMilkCostTrackerError(
            f"base_milk_cost_monthly_tracker.csv is missing required columns "
            f"{missing!r}. Expected exactly: {list(ALL_COLUMNS)}."
        )


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
            f"Cannot build base_milk_cost_monthly_tracker append payload: "
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


def append_rows_for_end_month(
    rows: pd.DataFrame,
    end_month,
) -> tuple[int, bool]:
    """Append ``rows`` for ``end_month`` if (and only if) that month is new.

    Parameters
    ----------
    rows
        DataFrame with at minimum ``Item``, ``Item Description``, and
        ``End Month Milk Cost`` columns. Extra columns are ignored.
    end_month
        Anything ``pd.to_datetime`` accepts — we'll normalise to first-of-month.

    Returns
    -------
    (rows_appended, month_was_new)
        ``rows_appended`` is the number of rows actually written;
        ``month_was_new`` is True iff this end_month wasn't already
        present in the tracker. When ``month_was_new`` is False we
        deliberately do nothing — the existing rows for that month win.

    Concurrency
    -----------
    Uses ``fabric_lakehouse_io.update_csv`` for a read-modify-write
    cycle with bounded ETag-conflict retries. Two simultaneous Refresh
    clicks on a brand-new end_month will collapse into one write
    (whichever lands first); the second sees the month present and
    no-ops.
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

    rows_appended = 0
    month_was_new = False

    def _mutate(current: Optional[pd.DataFrame]) -> pd.DataFrame:
        nonlocal rows_appended, month_was_new
        if current is None or current.empty:
            existing = pd.DataFrame(columns=list(ALL_COLUMNS))
        else:
            existing = current.copy()
            existing.columns = [str(c).strip() for c in existing.columns]
            _validate_columns(existing)
        if em in _existing_end_months(existing):
            # Month already tracked — return the unchanged frame so we
            # don't waste an upload round-trip.
            return existing
        month_was_new = True
        rows_appended = len(payload)
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

    return rows_appended, month_was_new


def has_end_month(end_month) -> bool:
    """Return True when ``end_month`` is already present in the tracker."""
    em = _normalise_end_month(end_month)
    if em is None:
        return False
    return em in _existing_end_months(read_tracker_df())


def latest_month() -> Optional[pd.Timestamp]:
    """Return the most recent End Month in the tracker (or ``None`` if empty).

    The PMBC auto-update on the New Price Quote page treats this value
    as its single source of truth: when PMBC's max ``Month`` lags
    behind, it gets refreshed against the tracker rows for THIS month.
    """
    months = _existing_end_months(read_tracker_df())
    if not months:
        return None
    return max(months)


def lookup_for_end_month(end_month) -> dict[str, float]:
    """Return ``{Item: End Month Milk Cost}`` for a given End Month.

    Used by the Product_Milk Base Cost auto-update path. Returns an
    empty dict when the month isn't in the tracker yet — the caller
    treats that as "no items to update."
    """
    em = _normalise_end_month(end_month)
    if em is None:
        return {}
    df = read_tracker_df()
    if df.empty:
        return {}
    parsed = df[COL_END_MONTH].apply(_normalise_end_month)
    matched = df[parsed == em]
    if matched.empty:
        return {}
    return {
        str(item): float(cost)
        for item, cost in zip(
            matched[COL_ITEM].astype(str).str.strip(),
            pd.to_numeric(matched[COL_END_COST], errors="coerce").fillna(0.0),
        )
    }


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
    "append_rows_for_end_month",
    "has_end_month",
    "latest_month",
    "lookup_for_end_month",
    "get_store_label",
]
