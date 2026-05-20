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

Schema (cost tracker, May-2026 contract)
----------------------------------------
The tracker now carries a leading **"Rest Market vs TOPCO"** dimension
so each month materialises **two** rows per Product ID — one for the
Rest-of-Market HTST resin cost, one for the TOPCO HTST resin cost::

    Rest Market vs TOPCO | Product ID | Product Description | Resin |
    Resin Cost ($/Gal)   | Month      | Pricing Category    | ...

The Side ("Rest" / "TOPCO") is the leftmost column by spec and is
treated as part of the composite write key ``(Month, Side)``.  Legacy
rows that pre-date this column are preserved verbatim on read and
write — they simply lack a Side value and are invisible to the
side-keyed Refresh rewrite.

Public API
----------
* :func:`read_resin_calculator_df`         — current calculator table.
* :func:`read_resin_cost_tracker_df`       — current cost-tracker table.
* :func:`latest_month`                     — newest Month present in the tracker.
* :func:`latest_month_for_side`            — newest Month for one Side.
* :func:`has_month`                        — quick membership test.
* :func:`upsert_for_sides`                 — sole writer (May-2026-late
                                              single-Refresh contract).
                                              For every ``(Month, Side)``
                                              key in the payload, drops
                                              the matching persisted
                                              rows and concats the
                                              payload onto the
                                              survivors — functions
                                              both as "overwrite
                                              existing months" and
                                              "append new months" in
                                              one call.  Rows outside
                                              the payload's key set
                                              are PRESERVED verbatim
                                              so months in the file
                                              but absent from the NMT
                                              (e.g. Sep–Nov 2025)
                                              stay as-is.
* :func:`seed_from_local_if_empty`         — bootstrap helper for first-ever run.
* :func:`get_calculator_label`             — UI caption helper.
* :func:`get_cost_tracker_label`           — UI caption helper.

Concurrency
-----------
Writes go through :func:`fabric_lakehouse_io.update_csv`, which provides
ETag-based optimistic concurrency with bounded retries.  Two simultaneous
"Refresh" clicks for the same set of months therefore collapse cleanly —
both upserts land on the same final state because each one drops the
matching ``(Month, Side)`` rows before inserting.

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
from typing import Iterable, Optional

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
COL_REST_VS_TOPCO: str = "Rest Market vs TOPCO"
COL_PRODUCT_ID:   str = "Product ID"
COL_PRODUCT_DESC: str = "Product Description"
COL_RESIN:        str = "Resin"
COL_RESIN_GAL:    str = "Resin Cost ($/Gal)"
COL_MONTH:        str = "Month"
COL_PRICING_CAT:  str = "Pricing Category"
COL_USAGE_LBS:    str = "Usage (Lbs/Ea)"
COL_GAL_EA:       str = "Gal/Ea"

# Canonical Side values for the "Rest Market vs TOPCO" dimension.
SIDE_REST:  str = "Rest"
SIDE_TOPCO: str = "TOPCO"

# Canonical column ordering for newly-written rows — keeps the
# "Rest Market vs TOPCO" dimension flush-left per the May-2026 spec.
# Any extra columns surviving from legacy rows are appended after.
_CANONICAL_COLUMN_ORDER: tuple[str, ...] = (
    COL_REST_VS_TOPCO,
    COL_PRODUCT_ID,
    COL_PRODUCT_DESC,
    COL_RESIN,
    COL_RESIN_GAL,
    COL_MONTH,
    COL_PRICING_CAT,
    COL_USAGE_LBS,
    COL_GAL_EA,
)

# The minimum column set we expect from the cost tracker.  Anything else
# present in the file is preserved verbatim through reads and writes.
# ``COL_REST_VS_TOPCO`` is INTENTIONALLY NOT in this set — legacy rows
# that pre-date the new dimension are tolerated on read and pass through
# write cycles untouched.
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
    """Drop every cached frame and re-arm the once-per-session bypass guard."""
    _read_csv_cached.clear()
    # Re-arm the bypass guard so the next ``_read_with_fallback`` is
    # willing to try a direct (non-cached) OneLake read again.  Without
    # this reset a manual "Refresh" click would not actually re-probe
    # OneLake on the very next render — it would just re-populate the
    # (still-empty) cache via the slow path with stale confirmation.
    for key in list(st.session_state.keys()):
        if isinstance(key, str) and key.startswith(_SS_BYPASS_PREFIX):
            del st.session_state[key]


def invalidate_read_cache() -> None:
    """Public alias of :func:`_invalidate_read_cache`.

    Pages call this immediately before re-injecting the resin DataFrames
    into the local upload registry — typically on a "Refresh" click —
    so a cached "file absent" answer (which can persist for up to
    ``_READ_CACHE_TTL_SECONDS`` after the user uploads the files
    out-of-band into OneLake) is dropped before the next read.
    """
    _invalidate_read_cache()


# Per-session "we already paid for the slow-path bypass once" guard.
# Without it, a *genuinely* empty OneLake folder would force every
# render to perform an extra non-cached HTTPS round-trip — see the
# rationale in :func:`_read_with_fallback`.  Stored under a stable
# prefix so :func:`_invalidate_read_cache` can sweep all entries when
# the user explicitly clicks "Refresh".
_SS_BYPASS_PREFIX = "resin_store_bypass_done::"


def _read_with_fallback(blob_path: str) -> Optional[pd.DataFrame]:
    """Read a blob with a once-per-session retry that bypasses the local cache.

    The Streamlit ``cache_data`` decorator above keeps even ``None``
    return values for up to ``_READ_CACHE_TTL_SECONDS``.  If a user
    uploads the files into OneLake AFTER we already cached an absent
    state, plain :func:`_read_csv_cached` would keep returning ``None``
    and the page would falsely report that the lakehouse is empty.

    To unstick that case we perform exactly *one* direct (cache-free)
    OneLake read per session per blob path — gated by
    :data:`_SS_BYPASS_PREFIX`.  The fast cached path is the default;
    only an empty cached answer triggers the slow path, and the
    session-state guard prevents the slow path from re-firing on every
    rerun in the steady-state "blob really is empty" case.

    Manual refresh clicks call :func:`invalidate_read_cache`, which
    sweeps every ``_SS_BYPASS_PREFIX`` entry — that re-arms this
    helper so the next render is willing to re-probe OneLake directly.
    """
    df = _read_csv_cached(blob_path)
    if df is not None and not df.empty:
        return df

    flag_key = f"{_SS_BYPASS_PREFIX}{blob_path}"
    if st.session_state.get(flag_key):
        return df  # already paid for the bypass this session — trust the cache.
    st.session_state[flag_key] = True

    # Clear ONLY the cached-frame entries here — *not* the bypass flags
    # (we just set ours and want it to persist for the rest of the
    # session).  ``_invalidate_read_cache`` would sweep both, undoing
    # the gate we just installed.
    _read_csv_cached.clear()
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


def normalise_side(value) -> Optional[str]:
    """Coerce a raw Side cell to a canonical ``SIDE_REST`` / ``SIDE_TOPCO``.

    Returns ``None`` for missing / unrecognised values — those rows fall
    outside the new key space and are preserved as-is on rewrite.

    Public so consumers (e.g. ``pages/monthly_resin_freight_mover_tracker``)
    can normalise Side cells without reaching into private symbols.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    txt = str(value).strip()
    if not txt:
        return None
    upper = txt.upper()
    if upper.startswith("REST"):
        return SIDE_REST
    if upper.startswith("TOPCO"):
        return SIDE_TOPCO
    return None


# Internal alias retained so existing module-private callers don't
# need to be edited individually.  Public callers should use
# :func:`normalise_side`.
_normalise_side = normalise_side


def _reorder_to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """Reorder columns so the canonical set comes first, extras trailing.

    Missing canonical columns are skipped (not added) so this is a pure
    reorder.  Used only on writes — reads preserve the source ordering.
    """
    present = [c for c in _CANONICAL_COLUMN_ORDER if c in df.columns]
    trailing = [c for c in df.columns if c not in present]
    return df[present + trailing]


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


def latest_month_for_side(side) -> Optional[pd.Timestamp]:
    """Return the newest Month for ``side`` (``SIDE_REST`` / ``SIDE_TOPCO``).

    Used by the per-side FG builders so the FG's "latest month" anchor
    follows the side's own data, not the file-wide max.  This matters
    because Rest and TOPCO can diverge — e.g. an operator added the
    new month for Rest but not yet for TOPCO.  Returns ``None`` when
    the side has no rows in the tracker.
    """
    canon = normalise_side(side)
    if canon is None:
        return None
    df = read_resin_cost_tracker_df()
    if df.empty or COL_REST_VS_TOPCO not in df.columns:
        return None
    side_mask = df[COL_REST_VS_TOPCO].apply(normalise_side) == canon
    if not side_mask.any():
        return None
    months = _parsed_months(df.loc[side_mask]).dropna()
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


# ── Internal: payload validation + canonicalisation ──────────────────────────


def _validate_and_canonicalise_payload(rows: pd.DataFrame) -> pd.DataFrame:
    """Validate ``rows`` and return a canonicalised copy ready for write.

    Enforces:
      * Required columns present.
      * ``Month`` parseable to first-of-month for every row.
      * ``Rest Market vs TOPCO`` parseable to ``SIDE_REST`` / ``SIDE_TOPCO``
        for every row (no NaN sides allowed in *new* writes — legacy
        sideless rows can only enter the file via the bootstrap seed).

    Side-effects:
      * ``Month`` is restamped to canonical ``M/D/YYYY`` string form.
      * ``Rest Market vs TOPCO`` is restamped to the exact canonical
        ``"Rest"`` / ``"TOPCO"`` casing.
      * Column ordering is normalised via :func:`_reorder_to_canonical`.
    """
    if rows is None or rows.empty:
        return pd.DataFrame(columns=list(_CANONICAL_COLUMN_ORDER))

    df = _strip_columns(rows)

    missing = [c for c in _TRACKER_REQUIRED if c not in df.columns]
    if missing:
        raise ResinCostTrackerStoreError(
            f"Payload is missing required columns {missing!r}.  "
            f"Expected at least {list(_TRACKER_REQUIRED)}."
        )
    if COL_REST_VS_TOPCO not in df.columns:
        raise ResinCostTrackerStoreError(
            f"Payload is missing the {COL_REST_VS_TOPCO!r} column — every "
            "new tracker row must carry a Rest/TOPCO side per the May-2026 "
            "schema."
        )

    sides = df[COL_REST_VS_TOPCO].apply(_normalise_side)
    if sides.isna().any():
        bad = df.loc[sides.isna(), COL_REST_VS_TOPCO].astype(str).tolist()
        raise ResinCostTrackerStoreError(
            f"Payload has unrecognised Side values {bad!r}.  Expected "
            f"{SIDE_REST!r} or {SIDE_TOPCO!r}."
        )
    df[COL_REST_VS_TOPCO] = sides

    months = df[COL_MONTH].apply(_normalise_month)
    if months.isna().any():
        bad = df.loc[months.isna(), COL_MONTH].astype(str).tolist()
        raise ResinCostTrackerStoreError(
            f"Payload has unparseable Month values {bad!r}."
        )
    df[COL_MONTH] = months.apply(_stringify_month)

    return _reorder_to_canonical(df)


def _payload_key_pairs(payload: pd.DataFrame) -> set[tuple[pd.Timestamp, str]]:
    """Return the set of ``(month_ts, side)`` pairs covered by ``payload``."""
    if payload.empty:
        return set()
    months = payload[COL_MONTH].apply(_normalise_month)
    sides  = payload[COL_REST_VS_TOPCO].apply(_normalise_side)
    return {
        (m, s) for m, s in zip(months, sides)
        if m is not None and s is not None
    }


def _drop_rows_for_key_pairs(
    existing: pd.DataFrame,
    keys: Iterable[tuple[pd.Timestamp, str]],
) -> tuple[pd.DataFrame, int]:
    """Drop every row of ``existing`` whose ``(Month, Side)`` is in ``keys``.

    Returns ``(filtered_df, rows_dropped)``.  Rows that lack either a
    parseable Month or a parseable Side are PRESERVED verbatim — they
    fall outside the key space and the caller's intent is to leave them
    alone.
    """
    if existing.empty:
        return existing, 0

    keys_set = set(keys)
    if not keys_set:
        return existing, 0

    months = existing[COL_MONTH].apply(_normalise_month) \
        if COL_MONTH in existing.columns else pd.Series([None] * len(existing))
    sides = existing[COL_REST_VS_TOPCO].apply(_normalise_side) \
        if COL_REST_VS_TOPCO in existing.columns else pd.Series([None] * len(existing))

    pairs = list(zip(months, sides))
    drop_mask = pd.Series(
        [(m, s) in keys_set for m, s in pairs],
        index=existing.index,
    )
    rows_dropped = int(drop_mask.sum())
    return existing.loc[~drop_mask].copy(), rows_dropped


# ── Public API: sole writer (overwrite-existing + append-new in one call) ────


def upsert_for_sides(
    rows: pd.DataFrame,
) -> tuple[int, int]:
    """Upsert tracker rows keyed by ``(Month, Side)`` (May-2026-late contract).

    Sole writer.  Performs both "overwrite existing months" and
    "append new months" in a single call — exactly what the
    operator-facing Refresh contract demands now that the Confirm
    button has been retired::

        "user clicks Refresh → calculate Resin Cost ($/Gal) for both
         sides over every NMT month → either overwrite or append into
         Resin_Cost_Tracker.csv"

    Algorithm:

      1. Validate + canonicalise the payload via
         :func:`_validate_and_canonicalise_payload`.
      2. Compute the set of ``(Month, Side)`` keys covered by the payload.
      3. Drop every persisted row whose key is in that set.
      4. Concat the payload onto the survivors and write back.

    Persisted rows whose ``(Month, Side)`` key is NOT in ``rows`` are
    PRESERVED verbatim — this is how the spec's "Sep–Nov 2025 stay
    as-is" invariant survives a Refresh that only covers Dec 2025 →
    May 2026.  New months and new sides are both supported in the
    same call because step 3 is a no-op when the key is absent.

    Returns ``(rows_written, rows_replaced)``:

      * ``rows_written``  = ``len(payload)`` after canonicalisation.
      * ``rows_replaced`` = how many persisted rows were dropped in
        step 3 (i.e. overwrite count).  ``rows_written - rows_replaced``
        is therefore the net append count, which the page surfaces in
        the Refresh caption.
    """
    payload = _validate_and_canonicalise_payload(rows)
    if payload.empty:
        return 0, 0

    key_pairs = _payload_key_pairs(payload)
    if not key_pairs:
        return 0, 0

    rows_written = len(payload)
    rows_replaced_total = 0

    def _mutate(current: Optional[pd.DataFrame]) -> pd.DataFrame:
        nonlocal rows_replaced_total

        existing = (
            pd.DataFrame() if current is None or current.empty
            else _strip_columns(current)
        )

        # Even a brand-new tracker should land in canonical column order
        # so downstream readers can rely on the leading Side column.
        if existing.empty:
            rows_replaced_total = 0
            return _reorder_to_canonical(payload)

        survivors, dropped = _drop_rows_for_key_pairs(existing, key_pairs)
        rows_replaced_total = dropped
        out = pd.concat([survivors, payload], ignore_index=True)
        return _reorder_to_canonical(out)

    try:
        _io.update_csv(
            _SECRETS_SECTION,
            COST_TRACKER_BLOB_PATH,
            _mutate,
            initial_default=pd.DataFrame(),
        )
    except _io.LakehouseIOError as exc:
        raise ResinCostTrackerStoreError(str(exc)) from exc

    if rows_written or rows_replaced_total:
        _invalidate_read_cache()
    return rows_written, rows_replaced_total


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
    "COL_REST_VS_TOPCO",
    "COL_PRODUCT_ID",
    "COL_PRODUCT_DESC",
    "COL_RESIN",
    "COL_RESIN_GAL",
    "COL_MONTH",
    "COL_PRICING_CAT",
    "COL_USAGE_LBS",
    "COL_GAL_EA",
    "SIDE_REST",
    "SIDE_TOPCO",
    "normalise_side",
    "read_resin_calculator_df",
    "read_resin_cost_tracker_df",
    "invalidate_read_cache",
    "latest_month",
    "latest_month_for_side",
    "has_month",
    "upsert_for_sides",
    "seed_from_local_if_empty",
    "get_calculator_label",
    "get_cost_tracker_label",
]
