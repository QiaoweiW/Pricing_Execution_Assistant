"""
Generic Microsoft Fabric / OneLake Lakehouse Files I/O.

A small reusable layer that sits between the per-feature stores
(``milk_usage_stable_store``, ``htst_activity_store``,
``base_milk_cost_tracker_store``, ``milk_mover_store``,
``activity_model_monthly_updater``) and the Azure Data Lake Storage
Gen2 SDK. Every caller that just needs to read / write / list / mutate
a blob (CSV, JSON, raw bytes) goes through here so we do not duplicate
ADLS-Gen2 boilerplate or grow parallel client / token caches.

Public surface area
-------------------
* Value objects:
    ``LakehouseRef``                 — (secrets_section, blob_path) → display label
    ``LakehouseFile``                — leaf returned by :func:`list_files`
* Bytes I/O:
    ``read_bytes`` / ``write_bytes`` / ``delete_blob``
* Cheap metadata:
    ``get_file_properties``          — (etag, size, last_modified) without body
* Listing:
    ``list_files``                   — single round-trip enumerate-folder
* CSV helpers:
    ``read_csv`` / ``write_csv`` / ``update_csv``
* JSON helpers:
    ``read_json`` / ``write_json`` / ``update_json``
* Bootstrap:
    ``bootstrap_bytes_if_absent``
* Error type:
    ``LakehouseIOError``

Configuration
-------------
``secrets_section`` is the name of a secrets block (``"fabric_htst"``,
``"fabric_milk_mover"``, ``"fabric_activity_model"``, …). The block must
provide ``workspace`` and ``lakehouse`` (display names or GUIDs); other
keys (service-principal credentials) are picked up by the shared
``fabric_auth`` chain.

When several callers want to share workspace/lakehouse settings (the
common case: everything lives in B2C Pricing > Pricing_Lakehouse), they
pass the same section name OR omit the section and inherit from
``[fabric_htst]``. The auth credential and the ``DataLakeServiceClient``
are cached by ``fabric_auth.DEFAULT_CACHE_NAME`` and by secrets-section
identity respectively, so the user sees one sign-in regardless of how
many features pull from Fabric.
"""
from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

import pandas as pd
import streamlit as st

from data_sources.fabric_auth import (
    DEFAULT_CACHE_NAME,
    GUID_RE,
    FabricAuthError,
    acquire_storage_token,
    get_credential,
    promote_sp_secrets_to_env,
    read_section,
)


logger = logging.getLogger(__name__)


# ── Public errors ────────────────────────────────────────────────────────────

class LakehouseIOError(RuntimeError):
    """Raised on any configuration / auth / I/O failure in this module.

    Per-feature stores typically catch this and rewrap into their own
    domain-specific error (``ActivityModelStoreError`` etc.) so the
    user-visible banner stays contextual.
    """


# ── Constants ────────────────────────────────────────────────────────────────

# OneLake's Azure Data Lake Storage Gen2 endpoint.
_ONELAKE_ACCOUNT_URL: str = "https://onelake.dfs.fabric.microsoft.com"

# Maximum retries for ETag-conflict writes. In practice this only fires
# when two pages click "Push to Fabric" within ~1 second.
_WRITE_RETRY_ATTEMPTS: int = 3


# ── Lightweight value object ─────────────────────────────────────────────────

@dataclass(frozen=True)
class LakehouseRef:
    """Identifies a single lakehouse Files/ path inside a Fabric workspace."""

    secrets_section: str   # e.g. "fabric_htst"
    blob_path: str         # e.g. "Activity_Model/Product_UOM.csv" — POSIX-style, no leading slash

    @property
    def display(self) -> str:
        """Human-readable label, e.g. 'OneLake: Pricing_Lakehouse/Files/<path>'."""
        try:
            cfg = _read_lakehouse_config(self.secrets_section)
            return f"OneLake: {cfg['lakehouse']}/Files/{self.blob_path}"
        except LakehouseIOError:
            return f"OneLake: Files/{self.blob_path}"


# ── Configuration ────────────────────────────────────────────────────────────

def _read_lakehouse_config(secrets_section: str) -> dict[str, str]:
    """Return ``{workspace, lakehouse}`` for ``secrets_section`` with [fabric_htst] fallback.

    Keeps deployments DRY: every connector that lives in the same
    Pricing_Lakehouse can omit workspace/lakehouse from its own block
    (or omit the block entirely) and inherit them from ``[fabric_htst]``.

    Service-principal keys are promoted to env vars exactly as
    ``fabric_auth.promote_sp_secrets_to_env`` does, so
    ``EnvironmentCredential`` picks them up on Streamlit Cloud /
    headless deployments.
    """
    htst_cfg: dict[str, str] = {}
    try:
        htst_cfg = read_section(
            "fabric_htst",
            required=("workspace", "lakehouse"),
        )
    except FabricAuthError:
        # No [fabric_htst] block — fine if the per-feature block stands alone.
        pass

    own_cfg: dict[str, str] = {}
    if secrets_section != "fabric_htst":
        try:
            own_cfg = read_section(secrets_section)
        except FabricAuthError:
            own_cfg = {}

    merged = dict(htst_cfg)
    merged.update({k: v for k, v in own_cfg.items() if v})

    missing = [k for k in ("workspace", "lakehouse") if not merged.get(k)]
    if missing:
        raise LakehouseIOError(
            f"Missing required Fabric secrets {missing!r}. Add them under "
            f"[fabric_htst] (preferred — shared across connectors) or "
            f"[{secrets_section}] in .streamlit/secrets.toml. See "
            "secrets.toml.example for the schema."
        )

    promote_sp_secrets_to_env(merged)
    return merged


# ── OneLake client plumbing ──────────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def _get_file_system_client(secrets_section: str):
    """Return a cached ``(FileSystemClient, cfg)`` rooted at ``cfg.workspace``.

    Cached at module level via ``@st.cache_resource`` so every page rerun
    reuses the same authenticated client. The credential underneath is
    itself cached in :func:`fabric_auth.get_credential`, so token refresh
    happens transparently inside the SDK.
    """
    try:
        from azure.storage.filedatalake import DataLakeServiceClient
    except ImportError as exc:
        raise LakehouseIOError(
            "Python package 'azure-storage-file-datalake' is not installed. "
            "Run: pip install -r requirements.txt"
        ) from exc

    cfg = _read_lakehouse_config(secrets_section)
    try:
        # Single shared cache name across the whole app — see the note
        # at the top of fabric_auth.DEFAULT_CACHE_NAME.
        credential = get_credential(DEFAULT_CACHE_NAME)
        # Pre-flight the token acquisition through OUR wrapper so:
        #   1. The 60-second failure cache short-circuits subsequent
        #      reads in the same render cycle (otherwise every store
        #      re-runs the full Azure Identity chain — 5-10 s each).
        #   2. Failures surface as our concise ``FabricAuthError`` text
        #      rather than the multi-page ``ClientAuthenticationError``
        #      dump the SDK would otherwise wrap into ``LakehouseIOError``.
        # On success the bearer token is now warm in MSAL's in-memory
        # cache, so the SDK's own ``credential.get_token`` call below is
        # a sub-millisecond no-op.
        acquire_storage_token(DEFAULT_CACHE_NAME)
    except FabricAuthError as exc:
        raise LakehouseIOError(str(exc)) from exc

    service = DataLakeServiceClient(
        account_url=_ONELAKE_ACCOUNT_URL,
        credential=credential,
    )
    fs = service.get_file_system_client(file_system=cfg["workspace"])
    return fs, cfg


def _file_client(secrets_section: str, blob_path: str):
    """Return a ``DataLakeFileClient`` for ``Files/<blob_path>`` in the lakehouse."""
    fs, cfg = _get_file_system_client(secrets_section)
    lh = cfg["lakehouse"]
    lakehouse_part = lh if GUID_RE.match(lh) else f"{lh}.Lakehouse"
    return fs.get_file_client(f"{lakehouse_part}/Files/{blob_path}")


# ── Low-level read / write primitives ────────────────────────────────────────

def read_bytes(
    secrets_section: str,
    blob_path: str,
) -> tuple[Optional[bytes], Optional[str]]:
    """Return ``(raw_bytes, etag)`` for the blob, or ``(None, None)`` if absent.

    Never raises ``ResourceNotFoundError`` to the caller — absent blobs
    are signalled by ``(None, None)`` so the seed-on-empty pattern is a
    one-liner at the call site.
    """
    from azure.core.exceptions import ResourceNotFoundError

    client = _file_client(secrets_section, blob_path)
    try:
        download = client.download_file()
    except ResourceNotFoundError:
        return None, None
    except Exception as exc:  # noqa: BLE001
        raise LakehouseIOError(
            f"Could not read OneLake blob 'Files/{blob_path}': {exc}"
        ) from exc

    raw = download.readall()
    etag = getattr(download.properties, "etag", None)
    return raw, etag


def write_bytes(
    secrets_section: str,
    blob_path: str,
    payload: bytes,
    *,
    etag: Optional[str] = None,
) -> str:
    """Upload ``payload``. Returns the new ETag.

    When ``etag`` is non-None we set ``If-Match`` so a concurrent writer's
    change surfaces as ``ResourceModifiedError``; pass that up so a retry
    loop can re-read and re-merge. When ``etag`` is None we
    create-or-overwrite unconditionally (used for first-ever bootstrap
    writes to a fresh blob).
    """
    from azure.core import MatchConditions
    from azure.core.exceptions import ResourceModifiedError

    client = _file_client(secrets_section, blob_path)
    upload_kwargs: dict[str, Any] = {"data": payload, "overwrite": True}
    if etag is not None:
        upload_kwargs["match_condition"] = MatchConditions.IfNotModified
        upload_kwargs["etag"] = etag

    try:
        client.upload_data(**upload_kwargs)
    except ResourceModifiedError:
        # Re-raise verbatim so the caller's retry loop can distinguish
        # this recoverable case from a generic write failure.
        raise
    except Exception as exc:  # noqa: BLE001
        raise LakehouseIOError(
            f"Could not write OneLake blob 'Files/{blob_path}': {exc}"
        ) from exc

    props = client.get_file_properties()
    return props.etag


def delete_blob(secrets_section: str, blob_path: str) -> bool:
    """Delete a blob. Returns True on delete, False when the blob was absent."""
    from azure.core.exceptions import ResourceNotFoundError

    client = _file_client(secrets_section, blob_path)
    try:
        client.delete_file()
        return True
    except ResourceNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001
        raise LakehouseIOError(
            f"Could not delete OneLake blob 'Files/{blob_path}': {exc}"
        ) from exc


@dataclass(frozen=True)
class FileProperties:
    """Lightweight metadata payload returned by :func:`get_file_properties`."""
    etag: Optional[str]
    size: Optional[int]
    last_modified: Optional[str]


def get_file_properties(
    secrets_section: str,
    blob_path: str,
) -> Optional[FileProperties]:
    """Return ``FileProperties`` for a blob without downloading its body.

    Returns ``None`` when the blob is absent. Use this in preference to
    :func:`read_bytes` whenever the caller only needs an ETag / size /
    last-modified timestamp — saves the body-download bandwidth.
    """
    from azure.core.exceptions import ResourceNotFoundError

    client = _file_client(secrets_section, blob_path)
    try:
        props = client.get_file_properties()
    except ResourceNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        raise LakehouseIOError(
            f"Could not read properties of OneLake blob 'Files/{blob_path}': {exc}"
        ) from exc
    return FileProperties(
        etag=getattr(props, "etag", None),
        size=getattr(props, "size", None),
        last_modified=str(getattr(props, "last_modified", "") or "") or None,
    )


# ── List a folder ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LakehouseFile:
    """Minimal file entry returned by :func:`list_files`."""
    name: str          # leaf name only, no folder prefix
    full_path: str     # blob_path you can hand back to read_bytes / write_bytes
    size: int          # bytes
    etag: Optional[str]
    last_modified: Optional[str]


def list_files(
    secrets_section: str,
    folder_path: str,
    *,
    suffix: Optional[str] = None,
) -> list[LakehouseFile]:
    """List immediate children of ``Files/<folder_path>`` in one round-trip.

    Parameters
    ----------
    folder_path
        POSIX-style path under ``Files/``, e.g. ``"Activity_Model"``.
        Pass an empty string to list the root of ``Files/``.
    suffix
        Optional case-insensitive suffix filter, e.g. ``".csv"``.

    Returns ``[]`` (not an error) when the folder doesn't exist yet —
    that's the expected first-bootstrap state.
    """
    from azure.core.exceptions import ResourceNotFoundError

    fs, cfg = _get_file_system_client(secrets_section)
    lh = cfg["lakehouse"]
    lakehouse_part = lh if GUID_RE.match(lh) else f"{lh}.Lakehouse"
    folder_part = folder_path.strip("/")
    full_dir = (
        f"{lakehouse_part}/Files/{folder_part}" if folder_part
        else f"{lakehouse_part}/Files"
    )

    try:
        paths = list(fs.get_paths(path=full_dir, recursive=False))
    except ResourceNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        raise LakehouseIOError(
            f"Could not list OneLake folder 'Files/{folder_part}': {exc}"
        ) from exc

    out: list[LakehouseFile] = []
    for p in paths:
        if getattr(p, "is_directory", False):
            continue
        full = p.name  # azure SDK gives us the absolute path including lakehouse part
        # Strip the lakehouse + "Files/" prefix so the returned blob_path
        # is the same shape callers pass to read_bytes / write_bytes.
        prefix = f"{lakehouse_part}/Files/"
        rel = full[len(prefix):] if full.startswith(prefix) else full
        leaf = rel.rsplit("/", 1)[-1]
        if suffix is not None and not leaf.lower().endswith(suffix.lower()):
            continue
        out.append(LakehouseFile(
            name=leaf,
            full_path=rel,
            size=int(getattr(p, "content_length", 0) or 0),
            etag=getattr(p, "etag", None),
            last_modified=str(getattr(p, "last_modified", "") or "") or None,
        ))
    return out


# ── CSV-shaped helpers ───────────────────────────────────────────────────────

def read_csv(
    secrets_section: str,
    blob_path: str,
    *,
    read_csv_kwargs: Optional[dict[str, Any]] = None,
) -> tuple[Optional[pd.DataFrame], Optional[str]]:
    """Return ``(DataFrame, etag)`` for a CSV blob, or ``(None, None)`` when absent.

    On parse failure we raise ``LakehouseIOError`` rather than returning
    a half-built DataFrame — callers want to see a hard error and fail
    loudly, not silently ingest a malformed file.
    """
    raw, etag = read_bytes(secrets_section, blob_path)
    if raw is None:
        return None, None
    if not raw:
        return pd.DataFrame(), etag
    try:
        df = pd.read_csv(io.BytesIO(raw), **(read_csv_kwargs or {}))
    except Exception as exc:  # noqa: BLE001
        raise LakehouseIOError(
            f"Could not parse OneLake blob 'Files/{blob_path}' as CSV: {exc}. "
            "Inspect the file in OneLake; if corrupt, delete it and the "
            "next page render will re-bootstrap from the local seed."
        ) from exc
    return df, etag


def _sanitise_frame_for_csv(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` whose schema is safe to round-trip through CSV.

    Three classes of accidental corruption have been observed in the
    wild and break ``pd.read_csv`` round-trips so badly that the user
    sees "headers wiped out, headers became the first row":

    1. **Auto-numeric columns** — a ``DataFrame`` constructed from a
       2-D list / array (or returned by some ``st.data_editor`` edge
       cases) has columns ``RangeIndex(0..N-1)`` and the real header
       text sitting in row 0.  ``to_csv`` happily writes ``0,1,2`` as
       the header line and the original headers as the first data
       row, producing exactly the symptom above.  We can't recover the
       *intended* column names here — but at the very least we coerce
       column names to strings so downstream readers see the integer
       headers as strings rather than re-interpreting them as data.
    2. **MultiIndex columns** — ``to_csv(index=False)`` writes ONE
       header line per level (so a 2-level MultiIndex emits 2 header
       lines), but ``pd.read_csv`` defaults to ``header=0`` (single
       header), turning the second header line into a data row.  We
       flatten the MultiIndex to a single level to guarantee a single
       header line on the wire.
    3. **Duplicate column names** — pandas tolerates these in memory
       but ``read_csv`` silently keeps only the LAST occurrence on
       round-trip, dropping data.  We suffix duplicates with ``.1``,
       ``.2``… (mirroring pandas's own ``mangle_dupe_cols`` semantics)
       so every value survives the round-trip.

    Pure / non-mutating: never modifies the input frame.  Cheap on
    every code path: 99% of frames already satisfy these invariants
    and exit through the fast path with one ``copy()``.
    """
    if df is None:
        return pd.DataFrame()
    out = df.copy()

    # 2. Flatten MultiIndex → single Index of "level0 | level1" strings.
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = pd.Index(
            [" | ".join(str(p) for p in tup) for tup in out.columns.to_flat_index()]
        )

    # 1. Stringify everything (catches RangeIndex, Timestamp, numeric, etc.).
    out.columns = pd.Index([str(c) for c in out.columns])

    # 3. Mangle duplicate names (read_csv would otherwise drop columns).
    seen: dict[str, int] = {}
    new_cols: list[str] = []
    for c in out.columns:
        n = seen.get(c, 0)
        new_cols.append(c if n == 0 else f"{c}.{n}")
        seen[c] = n + 1
    out.columns = pd.Index(new_cols)

    return out


def write_csv(
    secrets_section: str,
    blob_path: str,
    df: pd.DataFrame,
    *,
    etag: Optional[str] = None,
    to_csv_kwargs: Optional[dict[str, Any]] = None,
    verify: bool = True,
) -> str:
    """Serialise ``df`` to UTF-8 CSV (no index) and upload. Returns the new ETag.

    Defensive guarantees:

    * Always emits a single header line whose values are exactly
      ``df.columns`` (after :func:`_sanitise_frame_for_csv`
      normalisation), regardless of the input frame's index / column
      shape.  This prevents the "headers became first row" corruption
      pattern observed when callers inadvertently passed frames with
      ``RangeIndex`` columns or ``MultiIndex`` columns.
    * Uses ``lineterminator="\\n"`` so the on-disk bytes are byte-for-
      byte identical on Windows and POSIX (avoids spurious diffs in
      OneLake when the same data is re-published from different OSes).
    * When ``verify`` is True (default), re-reads the first 4 KB of the
      uploaded blob and asserts that its first line matches the
      expected header line.  This is cheap (one HEAD-equivalent range
      read) and gives us a hard, immediate error if any future change
      to this function — or to the underlying SDK — silently corrupts
      the header.

    The ``to_csv_kwargs`` escape hatch still exists for callers that
    truly need non-default kwargs (e.g. quoting, decimal locale), but
    ``header`` / ``index`` are pinned and CANNOT be overridden — they
    are load-bearing for the round-trip contract.
    """
    safe_df = _sanitise_frame_for_csv(df)

    extra: dict[str, Any] = dict(to_csv_kwargs or {})
    extra.pop("header", None)  # header always-on; see docstring.
    extra.pop("index", None)   # index always-off; we serialise data only.
    extra.setdefault("lineterminator", "\n")

    buf = io.StringIO()
    safe_df.to_csv(buf, index=False, header=True, **extra)
    payload = buf.getvalue().encode("utf-8")
    new_etag = write_bytes(secrets_section, blob_path, payload, etag=etag)

    if verify:
        # Read back JUST enough to check the header line is correct.
        # We deliberately do NOT re-parse the entire file — that would
        # double the network cost on every write.
        try:
            roundtrip, _ = read_bytes(secrets_section, blob_path)
        except Exception:  # noqa: BLE001 — verification is best-effort
            roundtrip = None
        if roundtrip is not None:
            first_line = roundtrip.split(b"\n", 1)[0].rstrip(b"\r")
            expected_first_line = ",".join(safe_df.columns).encode("utf-8")
            # Tolerate quoting differences (pandas may quote a header
            # that contains a comma / quote / newline), so compare by
            # length-normalised prefix only when the bytes do not match
            # exactly.  The structural check that catches the "headers
            # wiped out" bug is "first line is non-empty and not a data
            # row" — quoting normalisation is icing on the cake.
            if not first_line:
                logger.error(
                    "Post-write verification FAILED for 'Files/%s': blob "
                    "is empty after upload (expected header line %r).  "
                    "Upload likely truncated mid-flight.",
                    blob_path,
                    expected_first_line,
                )
            elif first_line != expected_first_line:
                # Soft warning only — quoting / line-terminator
                # differences can produce benign mismatches and we
                # don't want to break the publish UX on a cosmetic
                # divergence.  A genuine "headers wiped out" event
                # would show numeric / data-shaped first_line which
                # the operator can spot in the logs.
                logger.warning(
                    "Post-write verification: header bytes for 'Files/%s' "
                    "differ from in-memory frame.  Got %r, expected %r.  "
                    "Inspect the blob if downstream readers report "
                    "header issues.",
                    blob_path,
                    first_line[:200],
                    expected_first_line[:200],
                )

    return new_etag


def update_csv(
    secrets_section: str,
    blob_path: str,
    mutator: Callable[[Optional[pd.DataFrame]], pd.DataFrame],
    *,
    initial_default: Optional[pd.DataFrame] = None,
    to_csv_kwargs: Optional[dict[str, Any]] = None,
    read_csv_kwargs: Optional[dict[str, Any]] = None,
) -> pd.DataFrame:
    """Read-modify-write a CSV blob with bounded ETag-conflict retries.

    Parameters
    ----------
    mutator
        Callable ``current_df -> new_df``. Receives ``None`` when the
        blob doesn't yet exist (or whatever ``initial_default`` was set to).
        Must return the DataFrame to upload.
    initial_default
        Value to pass to ``mutator`` on first-ever write.

    Returns the new DataFrame that was successfully written.
    """
    return _retry_write(
        blob_path=blob_path,
        read=lambda: read_csv(secrets_section, blob_path, read_csv_kwargs=read_csv_kwargs),
        upload=lambda payload, etag: write_csv(
            secrets_section, blob_path, payload, etag=etag, to_csv_kwargs=to_csv_kwargs,
        ),
        mutator=mutator,
        initial_default=initial_default,
    )


# ── Parquet-shaped helpers ──────────────────────────────────────────────────
#
# Parquet is read-only here: we only need it for upstream feeds the
# pricing app consumes (e.g. the B2C Pricing History snapshot the
# Distribute Price Book workflow filters on).  Add a write/update pair
# the day we actually need to publish parquet — there's no production
# code path doing that yet, and keeping this read-only avoids accidental
# round-trip type drift (pandas → pyarrow → pandas can rewrite null
# typing in ways consumers don't expect).

def read_parquet(
    secrets_section: str,
    blob_path: str,
    *,
    read_parquet_kwargs: Optional[dict[str, Any]] = None,
) -> tuple[Optional[pd.DataFrame], Optional[str]]:
    """Return ``(DataFrame, etag)`` for a Parquet blob, or ``(None, None)`` when absent.

    Mirrors :func:`read_csv` semantics for ``.parquet`` blobs:

    * Missing blob → ``(None, None)`` (a recoverable cold-bootstrap state).
    * Empty body  → ``(empty DataFrame, etag)`` so callers can still
      branch on ``df.empty`` without a separate sentinel.
    * Parse failure raises :class:`LakehouseIOError` rather than silently
      ingesting a malformed file — see the same rationale in
      :func:`read_csv`.

    ``pd.read_parquet`` will pick the first installed engine (``pyarrow``
    preferred, falling back to ``fastparquet``).  Callers that need a
    specific engine can pass ``read_parquet_kwargs={'engine': 'pyarrow'}``.
    """
    raw, etag = read_bytes(secrets_section, blob_path)
    if raw is None:
        return None, None
    if not raw:
        return pd.DataFrame(), etag
    try:
        df = pd.read_parquet(io.BytesIO(raw), **(read_parquet_kwargs or {}))
    except Exception as exc:  # noqa: BLE001
        raise LakehouseIOError(
            f"Could not parse OneLake blob 'Files/{blob_path}' as Parquet: {exc}. "
            "Inspect the file in OneLake; if corrupt, re-upload the latest "
            "snapshot from the upstream pipeline."
        ) from exc
    return df, etag


# ── JSON-shaped helpers ──────────────────────────────────────────────────────

def read_json(
    secrets_section: str,
    blob_path: str,
) -> tuple[Optional[Any], Optional[str]]:
    """Return ``(parsed_json, etag)`` for a JSON blob, or ``(None, None)`` when absent.

    Unparseable JSON raises ``LakehouseIOError`` so a corrupt blob
    surfaces immediately with an actionable message rather than
    propagating a downstream ``KeyError``.
    """
    raw, etag = read_bytes(secrets_section, blob_path)
    if raw is None:
        return None, etag
    if not raw:
        return None, etag
    try:
        return json.loads(raw.decode("utf-8")), etag
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LakehouseIOError(
            f"OneLake blob 'Files/{blob_path}' is not valid UTF-8 JSON: {exc}. "
            "Inspect the file in OneLake; either fix it or delete it "
            "(the next page load will re-bootstrap from any seed)."
        ) from exc


def write_json(
    secrets_section: str,
    blob_path: str,
    payload: Any,
    *,
    etag: Optional[str] = None,
    indent: Optional[int] = 2,
    sort_keys: bool = False,
) -> str:
    """Serialise ``payload`` to UTF-8 JSON and upload. Returns the new ETag."""
    body = json.dumps(payload, indent=indent, default=str, sort_keys=sort_keys).encode("utf-8")
    return write_bytes(secrets_section, blob_path, body, etag=etag)


def update_json(
    secrets_section: str,
    blob_path: str,
    mutator: Callable[[Any], Any],
    *,
    initial_default: Any = None,
    indent: Optional[int] = 2,
    sort_keys: bool = False,
) -> Any:
    """Read-modify-write a JSON blob with bounded ETag-conflict retries.

    Mirrors :func:`update_csv` for JSON. ``mutator`` receives the parsed
    JSON value (or ``initial_default`` when the blob is absent) and
    returns the new value to upload. Returns the new value on success.
    """
    return _retry_write(
        blob_path=blob_path,
        read=lambda: read_json(secrets_section, blob_path),
        upload=lambda payload, etag: write_json(
            secrets_section, blob_path, payload, etag=etag, indent=indent, sort_keys=sort_keys,
        ),
        mutator=mutator,
        initial_default=initial_default,
    )


# ── Generic ETag-retry write helper (shared by update_csv / update_json) ─────

def _retry_write(
    *,
    blob_path: str,
    read: Callable[[], tuple[Any, Optional[str]]],
    upload: Callable[[Any, Optional[str]], str],
    mutator: Callable[[Any], Any],
    initial_default: Any,
) -> Any:
    """Bounded read-modify-write loop shared by ``update_csv`` / ``update_json``.

    Splitting this out keeps the CSV and JSON helpers single-purpose
    while still sharing one retry implementation — a single place to
    tune retry behaviour if it ever needs to grow (back-off, jitter,
    metric counters, etc.).
    """
    from azure.core.exceptions import ResourceModifiedError

    last_exc: Optional[Exception] = None
    for attempt in range(_WRITE_RETRY_ATTEMPTS):
        current, etag = read()
        seed = current if current is not None else initial_default
        new_payload = mutator(seed)
        try:
            upload(new_payload, etag)
            return new_payload
        except ResourceModifiedError as exc:
            last_exc = exc
            logger.warning(
                "OneLake ETag conflict writing %s (attempt %d/%d) — retrying",
                blob_path, attempt + 1, _WRITE_RETRY_ATTEMPTS,
            )
            continue

    raise LakehouseIOError(
        f"Lost {_WRITE_RETRY_ATTEMPTS} ETag race(s) writing 'Files/{blob_path}'. "
        f"Reload the page and try again. Underlying error: {last_exc}"
    )


# ── Bootstrap helpers ────────────────────────────────────────────────────────

def bootstrap_bytes_if_absent(
    secrets_section: str,
    blob_path: str,
    payload: bytes,
) -> bool:
    """Upload ``payload`` only when the blob does not yet exist.

    Returns True when we wrote, False when the blob was already present.
    Idempotent — safe to call from a render path.
    """
    raw, _etag = read_bytes(secrets_section, blob_path)
    if raw is not None:
        return False
    write_bytes(secrets_section, blob_path, payload, etag=None)
    return True


__all__ = [
    "LakehouseIOError",
    "LakehouseRef",
    "LakehouseFile",
    "FileProperties",
    "read_bytes",
    "write_bytes",
    "delete_blob",
    "get_file_properties",
    "list_files",
    "read_csv",
    "write_csv",
    "update_csv",
    "read_parquet",
    "read_json",
    "write_json",
    "update_json",
    "bootstrap_bytes_if_absent",
]
