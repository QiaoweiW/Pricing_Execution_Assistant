"""
OneLake-backed store for the resin pricing input pair
(``Resin_Calculator.csv`` and ``Resin_Cost_Tracker.csv``).

Both files used to be uploaded by the user on every visit to the
Market Barometer's Monthly Movers section.  They are slow-changing
reference data — the per-SKU usage / Gal-per-Each lookup
(`Resin_Calculator`) and the per-month $/Gal baseline used by the
Resin Mover FG (`Resin_Cost_Tracker`) — so they belong in the same
Pricing_Lakehouse alongside the milk-cost artefacts.

Storage layout
--------------
``Files/Resin_freight_cost_tracker/Resin_Calculator.csv``      — calculator inputs.
``Files/Resin_freight_cost_tracker/Resin_Cost_Tracker.csv``    — per-month baseline.

Public API
----------
* :func:`read_resin_calculator_df`         — current calculator table.
* :func:`read_resin_cost_tracker_df`       — current cost-tracker table.
* :func:`latest_month`                     — newest Month present in the tracker.
* :func:`has_month`                        — quick membership test.
* :func:`append_new_month_from_latest`     — duplicate the latest-month rows,
                                             re-stamp Month, and overwrite the
                                             ``Resin Cost ($/Gal)`` column from
                                             the two Resin Mover FGs (joined on
                                             ``Product ID``).  Idempotent on
                                             repeated calls for the same month.
* :func:`seed_from_local_if_empty`         — bootstrap helper for first-ever run.
* :func:`get_calculator_label`             — UI caption helper.
* :func:`get_cost_tracker_label`           — UI caption helper.

Concurrency
-----------
Writes go through :func:`fabric_lakehouse_io.update_csv`, which provides
ETag-based optimistic concurrency with bounded retries.  Two simultaneous
"Refresh" clicks for the same brand-new month therefore collapse into a
single append (whichever lands first); the second sees the month already
present and no-ops.

Configuration
-------------
Reads ``workspace`` and ``lakehouse`` from
``[fabric_resin_cost_tracker]`` when present, falling back to
``[fabric_htst]`` so deployments don't need to duplicate config
when every artefact lives in the same Pricing_Lakehouse.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, Optional

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io


logger = logging.getLogger(__name__)


# ── Public errors ────────────────────────────────────────────────────────────


class ResinCostTrackerStoreError(RuntimeError):
    """Raised on configuration / auth / I/O failure for this store.

    Wraps :class:`fabric_lakehouse_io.LakehouseIOError` so the page renders
    a single clean error path.
    """


# ── Constants ────────────────────────────────────────────────────────────────

# A single secrets-section name for both blobs — they always co-locate. The
# store inherits workspace/lakehouse from [fabric_htst] when absent.
_SECRETS_SECTION: str = "fabric_resin_cost_tracker"

# Canonical folder under Files/ — matches the lakehouse URL the user shared.
_FOLDER_PREFIX: str = "Resin_freight_cost_tracker"

CALCULATOR_BLOB_PATH:   str = f"{_FOLDER_PREFIX}/Resin_Calculator.csv"
COST_TRACKER_BLOB_PATH: str = f"{_FOLDER_PREFIX}/Resin_Cost_Tracker.csv"

# Canonical column names — kept in lock-step with the consumer
# ``pages/monthly_resin_freight_mover_tracker.py`` so renames need to be
# done in exactly one place when the upstream schema evolves.
COL_PRODUCT_ID:   str = "Product ID"
COL_PRODUCT_DESC: str = "Product Description"
COL_RESIN:        str = "Resin"
COL_RESIN_GAL:    str = "Resin Cost ($/Gal)"
COL_MONTH:        str = "Month"
COL_PRICING_CAT:  str = "Pricing Category"
COL_USAGE_LBS:    str = "Usage (Lbs/Ea)"
COL_GAL_EA:       str = "Gal/Ea"

# The minimum column set we expect from the cost tracker.  Anything else
# present in the file is preserved verbatim through reads and writes.
_TRACKER_REQUIRED: tuple[str, ...] = (COL_PRODUCT_ID, COL_RESIN_GAL, COL_MONTH)

# Calculator columns are looser — every consumer references the columns it
# needs by literal string and tolerates extras, so we don't enforce a strict
# schema here.

# Streamlit-cache TTL for blob reads.  These files are touched at most once
# per month — caching for 5 minutes keeps reruns instant while still picking
# up out-of-band edits within ~5 min.
_READ_CACHE_TTL_SECONDS: int = 300

# Default seed locations — used only to bootstrap a fresh OneLake folder on
# first ever render against an empty lakehouse.  Kept optional so deployments
# without the local CSVs can still operate as long as the lakehouse blobs
# already exist.
_DEFAULT_SEED_DIR: Path = (
    Path(__file__).resolve().parent.parent
    / "data" / "Market Barometer" / "Montly Movers"
)


# ── Internal helpers ─────────────────────────────────────────────────────────


@st.cache_data(ttl=_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _read_csv_cached(blob_path: str) -> Optional[pd.DataFrame]:
    """Cached fetch of one CSV blob.  Returns ``None`` when absent.

    NOTE: callers are expected to bypass the cache (via
    :func:`invalidate_read_cache`) when they observe a ``None`` /
    empty return on a cold read — the cache otherwise pins a stale
    "absent" answer for ``ttl`` seconds even after a brand-new file
    is dropped into OneLake.  ``read_resin_calculator_df`` /
    ``read_resin_cost_tracker_df`` perform that one-shot bypass
    automatically.
    """
    try:
        df, _etag = _io.read_csv(_SECRETS_SECTION, blob_path)
    except _io.LakehouseIOError as exc:
        raise ResinCostTrackerStoreError(str(exc)) from exc
    return df


def _invalidate_read_cache() -> None:
    """Drop every cached frame after a write so the next read sees fresh data."""
    _read_csv_cached.clear()


def invalidate_read_cache() -> None:
    """Public alias of :func:`_invalidate_read_cache`.

    Pages call this immediately before re-injecting the resin DataFrames
    into the local upload registry — typically on a "Refresh" click —
    so a cached "file absent" answer (which can persist for up to
    ``_READ_CACHE_TTL_SECONDS`` after the user uploads the files
    out-of-band into OneLake) is dropped before the next read.
    """
    _invalidate_read_cache()


def _read_with_fallback(blob_path: str) -> Optional[pd.DataFrame]:
    """Read a blob with a one-shot retry that bypasses the local cache.

    The Streamlit ``cache_data`` decorator above keeps even ``None``
    return values for up to ``_READ_CACHE_TTL_SECONDS``.  If a
    user uploads the files into OneLake AFTER we already cached an
    absent state, plain :func:`_read_csv_cached` would keep returning
    ``None`` and the page would falsely report that the lakehouse is
    empty.  This helper tries the cached read first (fast path); when
    it returns ``None`` it invalidates the cache once and re-reads
    directly via the lakehouse client to confirm whether the blob is
    genuinely missing.  The slow-path read costs at most one HTTPS
    round-trip per refresh — negligible compared to the user-visible
    payoff of always seeing the live state of OneLake.
    """
    df = _read_csv_cached(blob_path)
    if df is not None and not df.empty:
        return df
    _invalidate_read_cache()
    df, _etag = _io.read_csv(_SECRETS_SECTION, blob_path)
    return df


def _normalise_month(value) -> Optional[pd.Timestamp]:
    """Parse ``value`` into a first-of-month ``pd.Timestamp`` (None on failure)."""
    if value is None:
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return pd.Timestamp(ts).normalize().replace(day=1)


def _stringify_month(ts: pd.Timestamp) -> str:
    """Render a Month value as ``M/D/YYYY`` (matching the legacy CSV format)."""
    return f"{ts.month}/{ts.day}/{ts.year}"


def _strip_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` with header whitespace stripped."""
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    return out


# ── Public API: reads ────────────────────────────────────────────────────────


def read_resin_calculator_df() -> pd.DataFrame:
    """Return the Resin_Calculator table.  Empty DataFrame when blob is absent.

    Uses :func:`_read_with_fallback` so a cached "absent" answer from a
    prior cold render does not mask a freshly-uploaded OneLake blob.
    """
    df = _read_with_fallback(CALCULATOR_BLOB_PATH)
    if df is None or df.empty:
        return pd.DataFrame()
    return _strip_columns(df)


def read_resin_cost_tracker_df() -> pd.DataFrame:
    """Return the Resin_Cost_Tracker table.  Empty DataFrame when blob is absent.

    Uses :func:`_read_with_fallback` so a cached "absent" answer from a
    prior cold render does not mask a freshly-uploaded OneLake blob.
    """
    df = _read_with_fallback(COST_TRACKER_BLOB_PATH)
    if df is None or df.empty:
        return pd.DataFrame()
    return _strip_columns(df)


def _parsed_months(df: pd.DataFrame) -> pd.Series:
    """Return a Series of first-of-month Timestamps for every row of ``df``."""
    if df.empty or COL_MONTH not in df.columns:
        return pd.Series(dtype="datetime64[ns]")
    return df[COL_MONTH].apply(_normalise_month)


def latest_month() -> Optional[pd.Timestamp]:
    """Return the most recent Month present in the cost tracker (or ``None``)."""
    df = read_resin_cost_tracker_df()
    if df.empty:
        return None
    months = _parsed_months(df).dropna()
    if months.empty:
        return None
    return pd.Timestamp(months.max())


def has_month(target_month) -> bool:
    """Return True when ``target_month`` is already present in the cost tracker."""
    em = _normalise_month(target_month)
    if em is None:
        return False
    df = read_resin_cost_tracker_df()
    if df.empty:
        return False
    return bool((_parsed_months(df) == em).any())


# ── Public API: append-new-month workflow ────────────────────────────────────


def append_new_month_from_latest(
    target_month,
    *,
    new_resin_cost_lookup: Mapping[str, float],
) -> tuple[int, bool]:
    """Append a freshly-stamped copy of the latest month's rows for ``target_month``.

    Workflow (matches the May-2026 spec verbatim):

    1. If ``target_month`` is already present in the cost tracker, no-op
       (returns ``(0, False)``).
    2. Otherwise duplicate the rows of the most-recent month, change every
       row's :data:`COL_MONTH` to ``target_month``, and overwrite the
       :data:`COL_RESIN_GAL` column using ``new_resin_cost_lookup`` keyed on
       :data:`COL_PRODUCT_ID`.
    3. The duplicated rows are then appended to the cost tracker via an
       ETag-guarded read-modify-write cycle; cached reads are invalidated
       so the very next ``read_resin_cost_tracker_df`` sees the new rows.

    Parameters
    ----------
    target_month
        Anything ``pd.to_datetime`` accepts; normalised to first-of-month.
    new_resin_cost_lookup
        ``{Product ID (str) → New Resin Cost ($/Gal) (float)}`` keyed on the
        upstream FG output(s).  Caller is responsible for merging the Rest
        HTST and TOPCO FGs into one combined lookup (see consumer code).

    Returns
    -------
    (rows_appended, month_was_new)
        ``rows_appended`` is the number of rows actually written;
        ``month_was_new`` is True iff the call resulted in a new month
        being appended.  When False we deliberately do nothing — the
        existing rows for that month win.
    """
    em = _normalise_month(target_month)
    if em is None:
        raise ResinCostTrackerStoreError(
            f"target_month {target_month!r} is not parseable — "
            "expected something pd.to_datetime accepts."
        )

    rows_appended = 0
    month_was_new = False

    def _mutate(current: Optional[pd.DataFrame]) -> pd.DataFrame:
        nonlocal rows_appended, month_was_new

        existing = (
            pd.DataFrame() if current is None or current.empty
            else _strip_columns(current)
        )
        if existing.empty:
            # Nothing to duplicate from — bail out cleanly so we don't
            # write a malformed empty frame.
            return existing

        missing = [c for c in _TRACKER_REQUIRED if c not in existing.columns]
        if missing:
            raise ResinCostTrackerStoreError(
                f"Resin_Cost_Tracker is missing required columns "
                f"{missing!r}.  Expected at least {list(_TRACKER_REQUIRED)}."
            )

        months = existing[COL_MONTH].apply(_normalise_month)
        if (months == em).any():
            # Already tracked — no-op return.
            return existing

        valid = months.dropna()
        if valid.empty:
            return existing

        latest = pd.Timestamp(valid.max())
        latest_rows = existing[months == latest].copy()
        if latest_rows.empty:
            return existing

        # Stamp the new month and overwrite Resin Cost ($/Gal) when the
        # FG lookup carries a value for that Product ID.  Rows whose
        # Product ID is not in either FG keep the latest-month $/Gal
        # value — that's a defensive default so partial FG outputs don't
        # blow holes in the cost tracker.
        latest_rows[COL_MONTH] = _stringify_month(em)
        if new_resin_cost_lookup and COL_PRODUCT_ID in latest_rows.columns:
            mapped = latest_rows[COL_PRODUCT_ID].astype(str).str.strip().map(
                {str(k).strip(): v for k, v in new_resin_cost_lookup.items()}
            )
            mapped = pd.to_numeric(mapped, errors="coerce")
            new_vals = mapped.where(mapped.notna(), latest_rows[COL_RESIN_GAL])
            latest_rows[COL_RESIN_GAL] = pd.to_numeric(
                new_vals, errors="coerce"
            ).round(4)

        rows_appended = len(latest_rows)
        month_was_new = True
        return pd.concat([existing, latest_rows], ignore_index=True)

    try:
        _io.update_csv(
            _SECRETS_SECTION,
            COST_TRACKER_BLOB_PATH,
            _mutate,
            initial_default=pd.DataFrame(),
        )
    except _io.LakehouseIOError as exc:
        raise ResinCostTrackerStoreError(str(exc)) from exc

    if month_was_new:
        _invalidate_read_cache()
    return rows_appended, month_was_new


# ── Public API: bootstrap ────────────────────────────────────────────────────


def seed_from_local_if_empty(
    seed_dir: Optional[Path] = None,
) -> dict[str, int]:
    """Bootstrap the calculator + cost-tracker blobs from local CSVs (idempotent).

    Returns ``{blob_path: rows_seeded}``; ``rows_seeded`` is 0 when the blob
    already exists or the local seed file is missing.  Safe to call from a
    render path — costs at most one HEAD per blob in steady state.
    """
    seed = seed_dir or _DEFAULT_SEED_DIR
    return {
        CALCULATOR_BLOB_PATH:   _seed_one_csv(
            CALCULATOR_BLOB_PATH, seed / "Resin_Calculator.csv",
        ),
        COST_TRACKER_BLOB_PATH: _seed_one_csv(
            COST_TRACKER_BLOB_PATH, seed / "Resin_Cost_Tracker.csv",
        ),
    }


def _seed_one_csv(blob_path: str, csv_path: Path) -> int:
    """Bootstrap a single CSV blob from ``csv_path`` when absent.  Returns rows seeded."""
    try:
        existing, _etag = _io.read_csv(_SECRETS_SECTION, blob_path)
    except _io.LakehouseIOError as exc:
        raise ResinCostTrackerStoreError(str(exc)) from exc
    if existing is not None:
        return 0  # never overwrite real data
    if not csv_path.exists():
        return 0

    df = pd.read_csv(csv_path)
    df.columns = [str(c).strip() for c in df.columns]

    try:
        _io.write_csv(_SECRETS_SECTION, blob_path, df, etag=None)
    except _io.LakehouseIOError as exc:
        raise ResinCostTrackerStoreError(str(exc)) from exc

    _invalidate_read_cache()
    return len(df)


# ── Public API: identity captions ────────────────────────────────────────────


def get_calculator_label() -> str:
    """Short human-readable label for the calculator blob."""
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=CALCULATOR_BLOB_PATH,
    ).display


def get_cost_tracker_label() -> str:
    """Short human-readable label for the cost-tracker blob."""
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=COST_TRACKER_BLOB_PATH,
    ).display


__all__ = [
    "ResinCostTrackerStoreError",
    "CALCULATOR_BLOB_PATH",
    "COST_TRACKER_BLOB_PATH",
    "COL_PRODUCT_ID",
    "COL_PRODUCT_DESC",
    "COL_RESIN",
    "COL_RESIN_GAL",
    "COL_MONTH",
    "COL_PRICING_CAT",
    "COL_USAGE_LBS",
    "COL_GAL_EA",
    "read_resin_calculator_df",
    "read_resin_cost_tracker_df",
    "invalidate_read_cache",
    "latest_month",
    "has_month",
    "append_new_month_from_latest",
    "seed_from_local_if_empty",
    "get_calculator_label",
    "get_cost_tracker_label",
]
