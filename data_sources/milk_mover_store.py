"""
OneLake-backed store for the FMMO (Federal Milk Marketing Order) tracker.

Two JSON blobs live in the same Microsoft Fabric Lakehouse the HTST
Activity Monitor already reads from — no new vendor, no new auth flow,
persistent across Streamlit Cloud redeploys.

Storage layout
--------------
``Files/Milk_cost_tracker/fmmo_tracker.json``    — the table, as a JSON array of row dicts.
``Files/Milk_cost_tracker/milk_mover_state.json`` — PDF source-state cache (one entry per URL).

Both blobs live alongside ``Milk_Usage_Stable.csv`` and
``base_milk_cost_monthly_tracker.csv`` under
``Files/Milk_cost_tracker/``, so every artefact that backs the Milk
Commodity Cost view sits in one folder a user can open in OneLake
explorer for auditing.

Why two blobs and not one?
    1. A user opening ``fmmo_tracker.json`` in OneLake explorer to
       audit/edit a row should not see machine-managed PDF fingerprints
       alongside their data.
    2. Writes to one don't invalidate the ETag of the other, so the
       advanced-prices change-detection cycle can update its
       bookkeeping without conflicting with a concurrent manual edit
       to the table.

Public API (the autoupdate orchestrator and the view import these
directly; new code should prefer ``upsert_rows`` over any historical
append-only API):
    read_milk_mover_df()             -> pd.DataFrame
    latest_month()                   -> Optional[pd.Timestamp]
    upsert_rows(rows)                -> tuple[int, int]  (inserted, updated)
    has_rows_for_month(target_month) -> bool
    seed_from_csv_if_empty(csv_path) -> int  (number of rows seeded; 0 if non-empty)
    get_pdf_state(url)               -> Optional[dict]
    upsert_pdf_state(url, **fields)  -> None
    get_store_label()                -> str  (human-readable for status captions)
    get_table_blob_path()            -> str  (live path for UI captions)

Concurrency model
-----------------
Every write goes through :func:`fabric_lakehouse_io.update_json`, which
implements ETag-based optimistic concurrency with bounded retries. We
no longer maintain our own copy of that logic.

Configuration
-------------
The store reads ``workspace`` and ``lakehouse`` from
``[fabric_milk_mover]`` when present, falling back to ``[fabric_htst]``
(the existing HTST block) so deployments don't need to duplicate
config. Service-principal keys are honoured by the shared ``fabric_auth``
chain — no module-local credential plumbing.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
import streamlit as st

from data_sources import fabric_lakehouse_io as _io


# ── Public errors ────────────────────────────────────────────────────────────

class MilkMoverStoreError(RuntimeError):
    """Raised on any configuration, auth, or storage failure for the store.

    Wrapped so the page renders a single clean error path instead of
    leaking Azure SDK / Streamlit stack traces.
    """


# ── Constants ────────────────────────────────────────────────────────────────

# Secrets-section name. Inherits workspace/lakehouse from [fabric_htst]
# when the dedicated [fabric_milk_mover] block is absent — see
# ``fabric_lakehouse_io._read_lakehouse_config``.
_SECRETS_SECTION: str = "fabric_milk_mover"

# Subfolder inside the lakehouse Files/ root that holds every milk-cost
# artefact (FMMO tracker, PDF state cache, Milk_Usage_Stable, and the
# append-only base_milk_cost_monthly_tracker). Co-locating them under
# one folder keeps the lakehouse explorer view tidy and makes
# per-feature permissions / lifecycle policies easy to apply.
_FOLDER_PREFIX: str = "Milk_cost_tracker"

# Blob paths (POSIX-style, relative to the lakehouse Files/ root). The
# table file is named ``fmmo_tracker.json`` because it stores the four
# Federal Milk Marketing Order rows (HTST/ESL × Class I/II) derived from
# USDA's advanced-prices PDF; the previous flat-root name
# ``milk_mover_tracker.json`` was retired when the folder was introduced.
_TABLE_BLOB_PATH: str = f"{_FOLDER_PREFIX}/fmmo_tracker.json"
_STATE_BLOB_PATH: str = f"{_FOLDER_PREFIX}/milk_mover_state.json"

# Canonical column names — the original five preserve the legacy CSV /
# SQLite schema so every downstream consumer of ``read_milk_mover_df()``
# keeps working unchanged. ``Protein Rate`` and ``Other Solids Rate``
# were introduced for the Cottage Cheese category (May-2026); they are
# additive — legacy rows simply have ``null`` for these two fields and
# round-trip back to ``NaN`` when read into pandas.
COL_CATEGORY     = "Category"
COL_MONTH        = "Month"
COL_CLASS        = "Class"
COL_SKIM         = "Skim Rate"
COL_BUTTERFAT    = "Butterfat Rate"
COL_PROTEIN      = "Protein Rate"
COL_OTHER_SOLIDS = "Other Solids Rate"

# Numeric rate columns. Defined once so ``_normalise_rows`` and any
# future schema-evolution helpers iterate over a single tuple instead of
# repeating the column literals.
_RATE_COLUMNS: tuple[str, ...] = (
    COL_SKIM, COL_BUTTERFAT, COL_PROTEIN, COL_OTHER_SOLIDS,
)

ALL_COLUMNS: tuple[str, ...] = (
    COL_CATEGORY, COL_MONTH, COL_CLASS,
    COL_SKIM, COL_BUTTERFAT, COL_PROTEIN, COL_OTHER_SOLIDS,
)

# Streamlit-cache TTL for blob reads. OneLake reads cost ~50–200 ms;
# caching for one minute makes the page feel instant on rapid reruns
# while still picking up out-of-band edits within ~60 s.
_READ_CACHE_TTL_SECONDS: int = 60

# Default seed CSV used to bootstrap a fresh OneLake table — same path
# the legacy SQLite implementation used.
_DEFAULT_SEED_CSV: Path = (
    Path(__file__).resolve().parent.parent
    / "data" / "Market Barometer" / "Montly Movers" / "Milk_Mover_Tracker.csv"
)


# ── Row serialisation helpers ────────────────────────────────────────────────

def _row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Return the composite primary key for a row, used for INSERT-OR-IGNORE."""
    return (
        str(row[COL_CATEGORY]).strip(),
        str(row[COL_MONTH]),
        str(row[COL_CLASS]).strip(),
    )


def _normalise_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coerce every row to the canonical column set & types.

    * Month is normalised to ``YYYY-MM-DD`` so JSON sorting is chronological.
    * Every numeric rate (``Skim Rate``, ``Butterfat Rate``, ``Protein
      Rate``, ``Other Solids Rate``) coerces to ``float`` when present;
      missing / NaN values become ``None`` so JSON stores ``null`` and a
      future re-read by pandas yields ``NaN`` consistently.
    * Categorical fields are whitespace-trimmed so ``"HTST "`` and
      ``"HTST"`` collapse to a single dedup key.
    """
    out: list[dict[str, Any]] = []
    for raw in rows:
        cleaned = {c: raw.get(c) for c in ALL_COLUMNS}
        if cleaned[COL_MONTH] is not None:
            ts = pd.to_datetime(cleaned[COL_MONTH], errors="coerce")
            if pd.isna(ts):
                # Skip rows with un-parseable months — never crash the pipeline.
                continue
            cleaned[COL_MONTH] = ts.normalize().strftime("%Y-%m-%d")
        for c in _RATE_COLUMNS:
            v = cleaned[c]
            cleaned[c] = None if (v is None or pd.isna(v)) else float(v)
        cleaned[COL_CATEGORY] = str(cleaned[COL_CATEGORY]).strip()
        cleaned[COL_CLASS]    = str(cleaned[COL_CLASS]).strip()
        out.append(cleaned)
    return out


# ── Internal: raw-rows access (cached) ───────────────────────────────────────

@st.cache_data(ttl=_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _read_rows_cached() -> list[dict[str, Any]]:
    """Cached fetch of the raw row list. Cleared by every write helper.

    NOTE: callers are expected to bypass this cache (via
    :func:`invalidate_read_cache`) when they observe an empty list on a
    cold read — otherwise an "absent blob" answer pins for up to
    ``_READ_CACHE_TTL_SECONDS`` even after the auto-updater (or a manual
    out-of-band write) populates the blob in OneLake.
    """
    try:
        rows, _etag = _io.read_json(_SECRETS_SECTION, _TABLE_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc
    return list(rows or [])


# Per-session "we already paid for the slow-path bypass once" guard.  See
# :func:`_read_rows_with_fallback` for the full rationale — without this
# gate a *genuinely* empty FMMO blob would trigger an extra non-cached
# HTTPS read on every Streamlit rerun.
_SS_BYPASS_KEY = "milk_mover_store_bypass_done"


def _invalidate_read_cache() -> None:
    """Drop the cached row list AND re-arm the once-per-session bypass guard."""
    _read_rows_cached.clear()
    # Re-arming the bypass flag on cache invalidation is what makes the
    # public "USDA refresh" / "Refresh" actions effective: the next call
    # to :func:`_read_rows_with_fallback` is once again willing to hit
    # OneLake directly when the cached answer comes back empty.
    st.session_state.pop(_SS_BYPASS_KEY, None)


def invalidate_read_cache() -> None:
    """Public alias of :func:`_invalidate_read_cache`.

    Page code calls this immediately before re-reading the FMMO table
    after triggering the auto-updater (or after the user clicks
    "USDA refresh"), so a cached "blob empty" answer is dropped before
    the next read.
    """
    _invalidate_read_cache()


def _read_rows_with_fallback() -> list[dict[str, Any]]:
    """Read rows with a once-per-session retry that bypasses the local cache.

    Mirrors the resin-store ``_read_with_fallback`` helper.  Plain
    :func:`_read_rows_cached` keeps even an empty list for up to
    ``_READ_CACHE_TTL_SECONDS``; that's normally fine but breaks the
    cold-start UX when the auto-updater seeds the blob a few hundred ms
    after we've already cached "absent".

    To unstick that case we perform exactly *one* direct (cache-free)
    OneLake read per session — gated by :data:`_SS_BYPASS_KEY`.  In the
    steady-state "blob really is empty" case we therefore avoid the
    extra HTTPS round-trip on every rerun.  Manual refresh paths call
    :func:`invalidate_read_cache` which clears the gate and re-arms
    this fallback.
    """
    rows = _read_rows_cached()
    if rows:
        return rows
    if st.session_state.get(_SS_BYPASS_KEY):
        return rows  # already paid for the bypass this session.
    st.session_state[_SS_BYPASS_KEY] = True

    # Clear ONLY the cached row list here — *not* the bypass flag — so
    # the gate we just set persists for the rest of the session.
    _read_rows_cached.clear()
    try:
        rows, _etag = _io.read_json(_SECRETS_SECTION, _TABLE_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc
    return list(rows or [])


# ── Public API: data table ───────────────────────────────────────────────────

def read_milk_mover_df() -> pd.DataFrame:
    """Return the FMMO table as a DataFrame.

    Output columns (in order): ``Category, Month, Class, Skim Rate,
    Butterfat Rate, Protein Rate, Other Solids Rate``. The first five
    preserve the legacy CSV shape; the trailing two were introduced for
    the Cottage Cheese category (May-2026) and read back as ``NaN`` for
    every legacy row that predates the schema change.

    Months are returned as ``M/D/YYYY`` strings (no zero-padding) — the
    same format the legacy CSV used — so existing parsers
    (``_parse_month`` in ``monthly_resin_freight_mover_tracker``) accept
    them unchanged.

    Uses :func:`_read_rows_with_fallback` so a cached empty answer from
    a prior cold render does not mask a freshly-seeded OneLake blob.
    """
    rows = _read_rows_with_fallback()
    if not rows:
        return pd.DataFrame(columns=list(ALL_COLUMNS))

    df = pd.DataFrame(rows, columns=list(ALL_COLUMNS))
    months = pd.to_datetime(df[COL_MONTH])
    df[COL_MONTH] = months.apply(lambda d: f"{d.month}/{d.day}/{d.year}")
    return df


def latest_month() -> Optional[pd.Timestamp]:
    """Return the most-recent month in the table, or ``None`` when empty."""
    rows = _read_rows_cached()
    if not rows:
        return None
    months = pd.to_datetime([r[COL_MONTH] for r in rows], errors="coerce")
    months = months.dropna() if hasattr(months, "dropna") else pd.Series(months).dropna()
    if len(months) == 0:
        return None
    return pd.Timestamp(max(months)).normalize().replace(day=1)


def has_rows_for_month(target_month: pd.Timestamp) -> bool:
    """Return True when at least one row exists for ``target_month``."""
    target_str = pd.Timestamp(target_month).normalize().strftime("%Y-%m-%d")
    return any(r.get(COL_MONTH) == target_str for r in _read_rows_cached())


def esl_class_ii_rates_by_month() -> dict[pd.Timestamp, tuple[Optional[float], Optional[float]]]:
    """Return ``{first-of-month → (Skim Rate, Butterfat Rate)}`` for ESL Class II.

    Used by the Cottage Cheese historical backfill: CC II skim/bfat
    always mirror ESL II for the same month per the May-2026 contract,
    so the orchestrator builds this lookup once and copies the values
    into every CC row it backfills. The lookup is read straight from
    the cached row list — no extra OneLake round-trip — so calling this
    on every auto-update tick is cheap.

    Missing (Skim, Butterfat) cells round-trip as ``None`` so the
    backfill can clearly distinguish "unknown" from "zero".
    """
    out: dict[pd.Timestamp, tuple[Optional[float], Optional[float]]] = {}
    for r in _read_rows_cached():
        cat = str(r.get(COL_CATEGORY, "")).strip().casefold()
        cls = str(r.get(COL_CLASS, "")).strip().casefold()
        if cat != "esl" or cls != "ii":
            continue
        month_raw = r.get(COL_MONTH)
        if not month_raw:
            continue
        ts = pd.to_datetime(month_raw, errors="coerce")
        if pd.isna(ts):
            continue
        key = pd.Timestamp(ts).normalize().replace(day=1)
        skim = r.get(COL_SKIM)
        bfat = r.get(COL_BUTTERFAT)
        out[key] = (
            float(skim) if skim is not None and not pd.isna(skim) else None,
            float(bfat) if bfat is not None and not pd.isna(bfat) else None,
        )
    return out


# Set of casefolded Category labels that the historical-repair helpers
# treat as the "Culture" category.  The May-2026-late lakehouse rename
# replaced ``"Cottage Cheese"`` with ``"Culture"``; the legacy label is
# retained as an accepted synonym so any rows written before the rename
# still get repaired by these helpers.
_CULTURE_CATEGORY_LABELS: frozenset[str] = frozenset({"culture", "cottage cheese"})


def cottage_cheese_months_with_null_skim() -> list[pd.Timestamp]:
    """Return Culture months whose Skim Rate is null in the store.

    The May-2026 Culture contract (formerly "Cottage Cheese") requires
    Culture Skim Rate to equal ESL Class II Skim for the same month.
    Rows written before that contract carry ``null`` Skim — this helper
    enumerates them so a one-shot repair pass can patch each row by
    copying the ESL II value.

    The Python function name is kept as ``cottage_cheese_months_with_null_skim``
    on purpose: every cross-module import site would otherwise need to
    migrate in the same commit, and the function-name churn brings no
    operator-visible benefit.  Internal logic accepts BOTH the canonical
    ``"Culture"`` label and the legacy ``"Cottage Cheese"`` label via
    :data:`_CULTURE_CATEGORY_LABELS`.
    """
    out: list[pd.Timestamp] = []
    seen: set[pd.Timestamp] = set()
    for r in _read_rows_cached():
        cat = str(r.get(COL_CATEGORY, "")).strip().casefold()
        if cat not in _CULTURE_CATEGORY_LABELS:
            continue
        skim = r.get(COL_SKIM)
        if skim is not None and not pd.isna(skim):
            continue
        month_raw = r.get(COL_MONTH)
        if not month_raw:
            continue
        ts = pd.to_datetime(month_raw, errors="coerce")
        if pd.isna(ts):
            continue
        key = pd.Timestamp(ts).normalize().replace(day=1)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return sorted(out)


def patch_cottage_cheese_rates(
    patches: dict[pd.Timestamp, tuple[Optional[float], Optional[float]]],
) -> int:
    """Overwrite Skim/Butterfat on existing Culture rows in-place.

    ``patches`` is ``{first-of-month → (skim, bfat)}``. For each matched
    Culture row whose ``Month`` equals a key in ``patches``, the Skim and
    Butterfat values are replaced (Protein / Other Solids are left
    untouched).  Returns the number of rows actually modified.

    Idempotent: a no-op when the existing cell already equals the
    incoming value. Safe to re-run after a partial network failure.

    The Python function name is kept as ``patch_cottage_cheese_rates``
    on purpose (same rationale as :func:`cottage_cheese_months_with_null_skim`).
    Internal logic accepts both ``"Culture"`` and the legacy
    ``"Cottage Cheese"`` label via :data:`_CULTURE_CATEGORY_LABELS`.
    """
    if not patches:
        return 0

    # Normalise keys to ``YYYY-MM-DD`` strings to match the on-disk shape.
    keyed: dict[str, tuple[Optional[float], Optional[float]]] = {}
    for k, v in patches.items():
        ts = pd.Timestamp(k).normalize().replace(day=1)
        keyed[ts.strftime("%Y-%m-%d")] = v

    rows_changed = 0

    def _mutate(current: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        nonlocal rows_changed
        rows_now = list(current) if current else []
        for r in rows_now:
            cat = str(r.get(COL_CATEGORY, "")).strip().casefold()
            if cat not in _CULTURE_CATEGORY_LABELS:
                continue
            month_key = r.get(COL_MONTH)
            if not isinstance(month_key, str) or month_key not in keyed:
                continue
            new_skim, new_bfat = keyed[month_key]
            changed = False
            if new_skim is not None:
                cur_skim = r.get(COL_SKIM)
                if cur_skim is None or pd.isna(cur_skim) or float(cur_skim) != float(new_skim):
                    r[COL_SKIM] = float(new_skim)
                    changed = True
            if new_bfat is not None:
                cur_bfat = r.get(COL_BUTTERFAT)
                if cur_bfat is None or pd.isna(cur_bfat) or float(cur_bfat) != float(new_bfat):
                    r[COL_BUTTERFAT] = float(new_bfat)
                    changed = True
            if changed:
                rows_changed += 1
        return rows_now

    try:
        _io.update_json(_SECRETS_SECTION, _TABLE_BLOB_PATH, _mutate, initial_default=[])
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc

    if rows_changed > 0:
        _invalidate_read_cache()
    return rows_changed


def months_missing_category(category: str) -> list[pd.Timestamp]:
    """Return every distinct ``Month`` that lacks a row for ``category``.

    Used by the one-shot Culture (formerly "Cottage Cheese") backfill so
    the auto-update orchestrator can decide cheaply (in-memory, no HTTPS
    round-trip) whether any historical months still need to be filled in.
    Returns an empty list once every month already has a row for
    ``category``.

    Comparison is case-insensitive and whitespace-tolerant so a stray
    ``"COTTAGE CHEESE "`` row still counts as present.

    Synonym handling: when ``category`` is in the Culture family
    (``"Culture"`` or its legacy alias ``"Cottage Cheese"``), every row
    whose Category falls anywhere in :data:`_CULTURE_CATEGORY_LABELS`
    counts as a present row.  This prevents the orchestrator from
    re-backfilling Culture rows for months that still carry the legacy
    label — a common mid-migration scenario.
    """
    target = category.strip().casefold()
    accepted_labels: set[str] = {target}
    if target in _CULTURE_CATEGORY_LABELS:
        accepted_labels = set(_CULTURE_CATEGORY_LABELS)

    months_present: set[str] = set()
    months_with_category: set[str] = set()
    for r in _read_rows_cached():
        month = r.get(COL_MONTH)
        if not month:
            continue
        months_present.add(month)
        row_cat = str(r.get(COL_CATEGORY, "")).strip().casefold()
        if row_cat in accepted_labels:
            months_with_category.add(month)

    missing = months_present - months_with_category
    if not missing:
        return []
    parsed = pd.to_datetime(sorted(missing), errors="coerce")
    return [pd.Timestamp(ts).normalize().replace(day=1)
            for ts in parsed if not pd.isna(ts)]


def upsert_rows(
    rows: Iterable[dict[str, Any]],
    *,
    source: str = "auto-update",
) -> tuple[int, int]:
    """Insert new rows; overwrite rate cells on existing keys when they differ.

    The composite key is ``(Category, Month, Class)``.  For each incoming
    row:

    * **No matching key in the store** → the row is appended (insert).
    * **Matching key exists** → each of the four rate cells (``Skim Rate``,
      ``Butterfat Rate``, ``Protein Rate``, ``Other Solids Rate``) is
      overwritten *only when the incoming value is not ``None`` and
      differs from the stored value*.

    The ``None`` rule is what makes the parser composable with the page-1
    advance-prices history: ESL Class I Skim is reconcilable only for the
    announced month (the Class I ESL Adjustment is published only there),
    so for every OTHER month the orchestrator passes ``Skim Rate=None``
    and the existing (correctly written at announcement time) value is
    preserved.

    Bookkeeping fields ``_source`` and ``_updated_at`` are stamped on each
    cell change so the JSON carries a small audit trail for the most-recent
    writer.  ``_inserted_at`` is set only on a fresh insert.

    Operates as a single ETag-guarded read-modify-write so a concurrent
    auto-update or manual edit fails-and-retries cleanly inside
    ``fabric_lakehouse_io.update_json``.

    Returns
    -------
    ``(rows_inserted, rows_updated)`` — strictly disjoint counts.  Both
    are ``0`` for a complete no-op, which is the steady-state when the
    PDF is unchanged and the store is fully reconciled.
    """
    incoming = _normalise_rows(rows)
    if not incoming:
        return 0, 0

    inserted_count = 0
    updated_count  = 0

    def _mutate(current: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        nonlocal inserted_count, updated_count
        rows_now = list(current) if current else []
        by_key: dict[tuple[str, str, str], dict[str, Any]] = {
            _row_key(r): r for r in rows_now
        }
        appended: list[dict[str, Any]] = []
        for r in incoming:
            key = _row_key(r)
            stored = by_key.get(key)
            if stored is None:
                appended.append({
                    **r,
                    "_source": source,
                    "_inserted_at": datetime.utcnow().isoformat(),
                })
                continue
            # In-place rate overwrite — None never overrides an existing
            # numeric (see docstring for the rationale).
            changed = False
            for col in _RATE_COLUMNS:
                new_val = r.get(col)
                if new_val is None:
                    continue
                cur_val = stored.get(col)
                if (cur_val is None
                        or pd.isna(cur_val)
                        or float(cur_val) != float(new_val)):
                    stored[col] = float(new_val)
                    changed = True
            if changed:
                stored["_source"]     = source
                stored["_updated_at"] = datetime.utcnow().isoformat()
                updated_count += 1
        inserted_count = len(appended)
        return rows_now + appended if appended else rows_now

    try:
        _io.update_json(_SECRETS_SECTION, _TABLE_BLOB_PATH, _mutate, initial_default=[])
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc

    if inserted_count > 0 or updated_count > 0:
        _invalidate_read_cache()
    return inserted_count, updated_count


def seed_from_csv_if_empty(csv_path: Optional[Path] = None) -> int:
    """Bootstrap the OneLake table from ``csv_path`` when the blob is absent.

    Returns the number of rows seeded (0 when the blob already has
    content or the CSV is missing). Invoked once on first page render
    after a fresh OneLake setup.
    """
    try:
        existing, _etag = _io.read_json(_SECRETS_SECTION, _TABLE_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc
    if existing:
        return 0  # already populated; never overwrite real data

    csv = csv_path or _DEFAULT_SEED_CSV
    if not csv.exists():
        return 0

    df = pd.read_csv(csv)
    df.columns = [str(c).strip() for c in df.columns]
    seed_rows = _normalise_rows(df.to_dict(orient="records"))
    if not seed_rows:
        return 0

    payload = [
        {**r, "_source": "seed", "_inserted_at": datetime.utcnow().isoformat()}
        for r in seed_rows
    ]
    try:
        _io.write_json(_SECRETS_SECTION, _TABLE_BLOB_PATH, payload, etag=None)
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc

    _invalidate_read_cache()
    return len(payload)


# ── Public API: PDF source-state cache ───────────────────────────────────────

def get_pdf_state(url: str) -> Optional[dict]:
    """Return the cached fingerprint for ``url`` or ``None`` when never checked."""
    try:
        state, _etag = _io.read_json(_SECRETS_SECTION, _STATE_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc
    if not isinstance(state, dict):
        return None
    return state.get(url)


def upsert_pdf_state(
    url: str,
    *,
    etag: Optional[str],
    last_modified: Optional[str],
    content_sha256: Optional[str],
    checked_at: datetime,
    last_change_at: Optional[datetime] = None,
) -> None:
    """Insert or update the cached PDF fingerprint for ``url``."""

    def _mutate(current: Any) -> dict[str, Any]:
        if not isinstance(current, dict):
            current = {}
        previous = current.get(url, {}) if isinstance(current.get(url), dict) else {}
        current[url] = {
            "etag": etag,
            "last_modified": last_modified,
            "content_sha256": content_sha256 or previous.get("content_sha256"),
            "checked_at": checked_at.isoformat(),
            "last_change_at": (
                last_change_at.isoformat() if last_change_at is not None
                else previous.get("last_change_at")
            ),
        }
        return current

    try:
        _io.update_json(_SECRETS_SECTION, _STATE_BLOB_PATH, _mutate, initial_default={})
    except _io.LakehouseIOError as exc:
        raise MilkMoverStoreError(str(exc)) from exc


# ── Public API: store identity (for status captions) ─────────────────────────

def get_store_label() -> str:
    """Return a short human-readable label of where the data lives.

    Used by the page's auto-update status caption. Never raises —
    falls back to a generic string when secrets are missing so the
    caption always renders.
    """
    return _io.LakehouseRef(
        secrets_section=_SECRETS_SECTION,
        blob_path=_TABLE_BLOB_PATH,
    ).display


def get_table_blob_path() -> str:
    """Return the relative ``Files/<...>`` path of the FMMO tracker JSON.

    Exposed so UI captions can render the live path without duplicating
    the constant — keeps the docstring example and the actual path in
    lock-step if the layout ever moves again.
    """
    return _TABLE_BLOB_PATH


__all__ = [
    "MilkMoverStoreError",
    "COL_CATEGORY",
    "COL_MONTH",
    "COL_CLASS",
    "COL_SKIM",
    "COL_BUTTERFAT",
    "COL_PROTEIN",
    "COL_OTHER_SOLIDS",
    "ALL_COLUMNS",
    "read_milk_mover_df",
    "invalidate_read_cache",
    "latest_month",
    "has_rows_for_month",
    "months_missing_category",
    "esl_class_ii_rates_by_month",
    "cottage_cheese_months_with_null_skim",
    "patch_cottage_cheese_rates",
    "upsert_rows",
    "seed_from_csv_if_empty",
    "get_pdf_state",
    "upsert_pdf_state",
    "get_store_label",
    "get_table_blob_path",
]
