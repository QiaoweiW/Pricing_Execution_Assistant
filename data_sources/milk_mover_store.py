"""
OneLake-backed store for the Milk Mover Tracker.

Replaces the previous local-SQLite implementation. The data sits in two
JSON blobs in the same Microsoft Fabric Lakehouse the HTST Activity Monitor
already reads from — no new vendor, no new auth flow, persistent across
Streamlit Cloud redeploys.

Storage layout
--------------
``Files/milk_mover_tracker.json``   — the table, as a JSON array of row dicts.
``Files/milk_mover_state.json``     — PDF source-state cache (one entry per URL).

Why two blobs and not one?
    1. A user opening ``milk_mover_tracker.json`` in OneLake explorer to
       audit/edit a row should not see machine-managed PDF fingerprints
       alongside their data.
    2. Writes to one don't invalidate the ETag of the other, so the
       advanced-prices change-detection cycle can update its bookkeeping
       without conflicting with a concurrent manual edit to the table.

Public API (mirrors the old SQLite module so the autoupdate orchestrator
and the view need only minimal changes):
    read_milk_mover_df()             -> pd.DataFrame
    latest_month()                   -> Optional[pd.Timestamp]
    insert_rows(rows)                -> int  (number of rows actually added)
    has_rows_for_month(target_month) -> bool
    seed_from_csv_if_empty(csv_path) -> int  (number of rows seeded; 0 if non-empty)
    get_pdf_state(url)               -> Optional[dict]
    upsert_pdf_state(url, **fields)  -> None
    get_store_label()                -> str  (human-readable for status captions)

Concurrency model
-----------------
All writes use ETag-based optimistic concurrency: read the blob with its
``ETag``, modify in pandas, upload with ``If-Match`` against the same ETag.
On the rare ``ResourceModifiedError`` (someone else wrote between our read
and our write) we retry up to 3 times with the freshly-read ETag.

Configuration
-------------
The store reads ``workspace`` and ``lakehouse`` from ``[fabric_milk_mover]``
when present, falling back to ``[fabric_htst]`` (the existing HTST block) so
deployments don't need to duplicate config.  Service-principal keys, when
present in either section, are promoted to ``AZURE_*`` env vars exactly as
``htst_shipment.py`` does — same auth chain, same disk-persistent token
cache.
"""
from __future__ import annotations

import io
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
import streamlit as st

from data_sources.fabric_auth import (
    GUID_RE,
    FabricAuthError,
    acquire_storage_token,  # noqa: F401  — used indirectly via get_credential
    get_credential,
    promote_sp_secrets_to_env,
    read_section,
)


logger = logging.getLogger(__name__)


# ── Public errors ────────────────────────────────────────────────────────────

class MilkMoverStoreError(RuntimeError):
    """Raised on any configuration, auth, or storage failure for the store.

    Wrapped so the page renders a single clean error path instead of leaking
    Azure SDK / Streamlit stack traces.
    """


# ── Constants ────────────────────────────────────────────────────────────────

# Filenames inside the lakehouse Files/ folder. Hard-coded by deliberate
# choice: the user explicitly asked for the simplest path layout.
_TABLE_BLOB_NAME: str = "milk_mover_tracker.json"
_STATE_BLOB_NAME: str = "milk_mover_state.json"

# OneLake's Azure Data Lake Storage Gen2 endpoint.
_ONELAKE_ACCOUNT_URL: str = "https://onelake.dfs.fabric.microsoft.com"

# Disk-persistent MSAL token-cache name. We deliberately reuse the existing
# HTST cache name so a user who has already signed in once (via the HTST
# Activity Monitor) does not get prompted again for milk_mover. They are the
# same Azure scope, same identity — sharing the cache is correct.
_TOKEN_CACHE_NAME: str = "streamlit_htst_shipment"

# Canonical column names — same shape as the legacy CSV / SQLite schema so
# every downstream consumer of read_milk_mover_df() keeps working unchanged.
COL_CATEGORY  = "Category"
COL_MONTH     = "Month"
COL_CLASS     = "Class"
COL_SKIM      = "Skim Rate"
COL_BUTTERFAT = "Butterfat Rate"
ALL_COLUMNS: tuple[str, ...] = (COL_CATEGORY, COL_MONTH, COL_CLASS, COL_SKIM, COL_BUTTERFAT)

# Streamlit-cache TTL for blob reads. OneLake reads cost ~50-200 ms; caching
# for one minute makes the page feel instant on rapid reruns while still
# picking up out-of-band edits within ~60 s.
_READ_CACHE_TTL_SECONDS: int = 60

# Maximum retries for ETag-conflict writes. In practice this only ever fires
# when two people click "USDA refresh" within ~1 second — extremely rare.
_WRITE_RETRY_ATTEMPTS: int = 3


# ── Configuration ────────────────────────────────────────────────────────────

def _read_config() -> dict[str, str]:
    """Return ``{workspace, lakehouse}`` for the milk-mover store.

    Reads ``[fabric_milk_mover]`` when present, then back-fills any missing
    keys from ``[fabric_htst]`` so deployments don't have to duplicate
    workspace/lakehouse settings between the two connectors.

    Service-principal keys (tenant_id / client_id / client_secret) are
    looked up in either section — whichever provides them — and promoted
    to ``AZURE_*`` env vars so EnvironmentCredential picks them up.
    """
    htst_cfg: dict[str, str] = {}
    try:
        htst_cfg = read_section(
            "fabric_htst",
            required=("workspace", "lakehouse"),
        )
    except FabricAuthError:
        # No fabric_htst block — fine, fabric_milk_mover may stand alone.
        pass

    try:
        own_cfg = read_section("fabric_milk_mover")
    except FabricAuthError:
        own_cfg = {}

    merged = dict(htst_cfg)
    merged.update({k: v for k, v in own_cfg.items() if v})

    missing = [k for k in ("workspace", "lakehouse") if not merged.get(k)]
    if missing:
        raise MilkMoverStoreError(
            f"Missing required Fabric secrets {missing!r}. Add them under "
            "[fabric_htst] (preferred) or [fabric_milk_mover] in "
            ".streamlit/secrets.toml. See secrets.toml.example for the schema."
        )

    promote_sp_secrets_to_env(merged)
    return merged


# ── OneLake client plumbing ──────────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def _get_file_system_client():
    """Return a cached ``FileSystemClient`` rooted at the configured workspace.

    Cached at module level via ``@st.cache_resource`` so every page rerun
    reuses the same authenticated client. The credential underneath is
    itself cached in ``fabric_auth.get_credential``, so token refresh
    happens transparently inside the SDK.
    """
    try:
        from azure.storage.filedatalake import DataLakeServiceClient
    except ImportError as exc:
        raise MilkMoverStoreError(
            "Python package 'azure-storage-file-datalake' is not installed. "
            "Run: pip install -r requirements.txt"
        ) from exc

    cfg = _read_config()
    try:
        credential = get_credential(_TOKEN_CACHE_NAME)
    except FabricAuthError as exc:
        raise MilkMoverStoreError(str(exc)) from exc

    service = DataLakeServiceClient(
        account_url=_ONELAKE_ACCOUNT_URL,
        credential=credential,
    )
    return service.get_file_system_client(file_system=cfg["workspace"]), cfg


def _file_client(blob_name: str):
    """Return a ``DataLakeFileClient`` for ``Files/<blob_name>`` in the lakehouse.

    The lakehouse path uses ``.Lakehouse`` for display-name identifiers and
    omits it for GUID identifiers — same convention as ``htst_shipment.py``.
    """
    fs, cfg = _get_file_system_client()
    lh = cfg["lakehouse"]
    lakehouse_part = lh if GUID_RE.match(lh) else f"{lh}.Lakehouse"
    return fs.get_file_client(f"{lakehouse_part}/Files/{blob_name}")


# ── Low-level read / write primitives ────────────────────────────────────────

def _download_json(blob_name: str) -> tuple[Optional[Any], Optional[str]]:
    """Return ``(parsed_json, etag)`` for the blob, or ``(None, None)`` if absent."""
    from azure.core.exceptions import ResourceNotFoundError

    client = _file_client(blob_name)
    try:
        download = client.download_file()
    except ResourceNotFoundError:
        return None, None
    except Exception as exc:  # noqa: BLE001
        raise MilkMoverStoreError(
            f"Could not read OneLake blob 'Files/{blob_name}': {exc}"
        ) from exc

    raw = download.readall()
    if not raw:
        return None, getattr(download.properties, "etag", None)

    try:
        return json.loads(raw.decode("utf-8")), download.properties.etag
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MilkMoverStoreError(
            f"Blob 'Files/{blob_name}' is not valid UTF-8 JSON: {exc}. "
            "Inspect the file in OneLake and either fix it or delete it "
            "(the next page load will re-seed from CSV)."
        ) from exc


def _upload_json(blob_name: str, payload: Any, *, etag: Optional[str]) -> str:
    """Upload ``payload`` as pretty-printed JSON. Returns the new ETag.

    When ``etag`` is non-None we set ``If-Match`` so a concurrent writer's
    change is detected as a 412; callers handle that by re-reading and
    retrying. When ``etag`` is None we unconditionally create/overwrite —
    used only for the first-ever write to a brand-new blob.
    """
    from azure.core import MatchConditions
    from azure.core.exceptions import ResourceModifiedError

    body = json.dumps(payload, indent=2, default=str, sort_keys=False).encode("utf-8")

    client = _file_client(blob_name)
    upload_kwargs: dict[str, Any] = {"data": body, "overwrite": True}
    if etag is not None:
        upload_kwargs["match_condition"] = MatchConditions.IfNotModified
        upload_kwargs["etag"] = etag

    try:
        client.upload_data(**upload_kwargs)
    except ResourceModifiedError:
        # Re-raise as-is so the caller's retry loop can distinguish this
        # recoverable case from a generic write failure.
        raise
    except Exception as exc:  # noqa: BLE001
        raise MilkMoverStoreError(
            f"Could not write OneLake blob 'Files/{blob_name}': {exc}"
        ) from exc

    # Read back the new ETag — required for any chained writes the caller
    # may want to perform without a second download round-trip.
    props = client.get_file_properties()
    return props.etag


def _read_with_retry(blob_name: str) -> tuple[Optional[Any], Optional[str]]:
    """Thin wrapper around :func:`_download_json`; kept for symmetry with
    :func:`_write_with_retry` and to give us a single seam to add transient-
    error retry logic later if the OneLake endpoint ever flakes."""
    return _download_json(blob_name)


def _write_with_retry(
    blob_name: str,
    mutator,
    *,
    initial_default: Any,
) -> None:
    """Read-modify-write the blob with bounded ETag-conflict retries.

    Parameters
    ----------
    blob_name
        Either ``_TABLE_BLOB_NAME`` or ``_STATE_BLOB_NAME``.
    mutator
        Callable ``current -> new`` taking the parsed-JSON content (or
        ``initial_default`` when the blob doesn't yet exist) and returning
        the new content to upload.
    initial_default
        Value passed to ``mutator`` when the blob is absent on first read.
    """
    from azure.core.exceptions import ResourceModifiedError

    last_exc: Optional[Exception] = None
    for attempt in range(_WRITE_RETRY_ATTEMPTS):
        current, etag = _read_with_retry(blob_name)
        if current is None:
            current = initial_default
        new_payload = mutator(current)
        try:
            _upload_json(blob_name, new_payload, etag=etag)
            return
        except ResourceModifiedError as exc:
            last_exc = exc
            logger.warning(
                "OneLake ETag conflict writing %s (attempt %d/%d) — retrying",
                blob_name, attempt + 1, _WRITE_RETRY_ATTEMPTS,
            )
            continue

    raise MilkMoverStoreError(
        f"Lost {_WRITE_RETRY_ATTEMPTS} ETag race(s) writing 'Files/{blob_name}'. "
        f"Reload the page and try again. Underlying error: {last_exc}"
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
    * NaNs become ``None`` (i.e. JSON null) so a future re-read by pandas
      yields ``NaN`` consistently.
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
        for c in (COL_SKIM, COL_BUTTERFAT):
            v = cleaned[c]
            cleaned[c] = None if (v is None or pd.isna(v)) else float(v)
        cleaned[COL_CATEGORY] = str(cleaned[COL_CATEGORY]).strip()
        cleaned[COL_CLASS]    = str(cleaned[COL_CLASS]).strip()
        out.append(cleaned)
    return out


# ── Public API: data table ───────────────────────────────────────────────────

@st.cache_data(ttl=_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _read_milk_mover_rows_cached() -> list[dict[str, Any]]:
    """Cached fetch of the raw row list. Cleared by every write helper."""
    rows, _etag = _read_with_retry(_TABLE_BLOB_NAME)
    return list(rows or [])


def _invalidate_read_cache() -> None:
    """Drop the cached row list after a write, so the next read sees fresh data."""
    _read_milk_mover_rows_cached.clear()


def read_milk_mover_df() -> pd.DataFrame:
    """Return the milk-mover table as a DataFrame matching the legacy CSV shape.

    Output columns (in order): ``Category, Month, Class, Skim Rate, Butterfat Rate``.
    Months are returned as ``M/D/YYYY`` strings (no zero-padding) — the same
    format the legacy CSV used — so existing parsers (``_parse_month``)
    accept them unchanged.
    """
    rows = _read_milk_mover_rows_cached()
    if not rows:
        return pd.DataFrame(columns=list(ALL_COLUMNS))

    df = pd.DataFrame(rows, columns=list(ALL_COLUMNS))
    months = pd.to_datetime(df[COL_MONTH])
    df[COL_MONTH] = months.apply(lambda d: f"{d.month}/{d.day}/{d.year}")
    return df


def latest_month() -> Optional[pd.Timestamp]:
    """Return the most-recent month present in the table, or ``None`` when empty."""
    rows = _read_milk_mover_rows_cached()
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
    return any(r.get(COL_MONTH) == target_str for r in _read_milk_mover_rows_cached())


def insert_rows(rows: Iterable[dict[str, Any]], *, source: str = "auto-update") -> int:
    """Append rows to the table, skipping duplicates by ``(Category, Month, Class)``.

    Returns the number of rows actually inserted. This is the OneLake-side
    equivalent of SQLite's ``INSERT OR IGNORE`` — re-running the auto-update
    pipeline is a safe no-op when the rows already exist.

    The ``source`` parameter is currently informational only (recorded as
    a top-level field in each row so future tooling can distinguish
    seed/auto-update/manual rows); it has no effect on dedup or read
    behaviour.
    """
    incoming = _normalise_rows(rows)
    if not incoming:
        return 0

    inserted_count = 0

    def _mutate(current: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal inserted_count
        existing_keys = {_row_key(r) for r in current}
        appended: list[dict[str, Any]] = []
        for r in incoming:
            if _row_key(r) in existing_keys:
                continue
            appended.append({**r, "_source": source, "_inserted_at": datetime.utcnow().isoformat()})
            existing_keys.add(_row_key(r))
        inserted_count = len(appended)
        if not appended:
            # Nothing new — short-circuit by returning the unchanged list so
            # we don't waste an upload round-trip on identical content.
            return current
        return current + appended

    _write_with_retry(_TABLE_BLOB_NAME, _mutate, initial_default=[])
    if inserted_count > 0:
        _invalidate_read_cache()
    return inserted_count


def seed_from_csv_if_empty(csv_path: Optional[Path] = None) -> int:
    """Bootstrap the OneLake table from ``csv_path`` when the blob is absent.

    Returns the number of rows seeded (0 when the blob already has content
    or the CSV is missing). Invoked once on first page render after a
    fresh OneLake setup.
    """
    rows, etag = _read_with_retry(_TABLE_BLOB_NAME)
    if rows:
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
    _upload_json(_TABLE_BLOB_NAME, payload, etag=etag)
    _invalidate_read_cache()
    return len(payload)


# Default seed CSV used to bootstrap a fresh OneLake table — same path the
# legacy SQLite implementation used.
_DEFAULT_SEED_CSV: Path = (
    Path(__file__).resolve().parent.parent
    / "data" / "Market Barometer" / "Montly Movers" / "Milk_Mover_Tracker.csv"
)


# ── Public API: PDF source-state cache ───────────────────────────────────────

def get_pdf_state(url: str) -> Optional[dict]:
    """Return the cached fingerprint for ``url`` or ``None`` when never checked."""
    state, _etag = _read_with_retry(_STATE_BLOB_NAME)
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
    def _mutate(current: dict[str, Any]) -> dict[str, Any]:
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

    _write_with_retry(_STATE_BLOB_NAME, _mutate, initial_default={})


# ── Public API: store identity (for status captions) ─────────────────────────

def get_store_label() -> str:
    """Return a short human-readable label of where the data lives.

    Used by the page's milk-mover ``_Uploaded.filename`` slot and the
    auto-update status caption. Never raises — returns a generic string
    when secrets are missing so the caption always renders.
    """
    try:
        cfg = _read_config()
        return f"OneLake: {cfg['lakehouse']}/Files/{_TABLE_BLOB_NAME}"
    except MilkMoverStoreError:
        return f"OneLake: Files/{_TABLE_BLOB_NAME}"


__all__ = [
    "MilkMoverStoreError",
    "COL_CATEGORY",
    "COL_MONTH",
    "COL_CLASS",
    "COL_SKIM",
    "COL_BUTTERFAT",
    "ALL_COLUMNS",
    "read_milk_mover_df",
    "latest_month",
    "has_rows_for_month",
    "insert_rows",
    "seed_from_csv_if_empty",
    "get_pdf_state",
    "upsert_pdf_state",
    "get_store_label",
]
