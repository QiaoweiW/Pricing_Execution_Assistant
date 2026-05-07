"""
Generic Microsoft Fabric / OneLake Lakehouse Files I/O.

A small reusable layer that sits between the per-feature stores
(``milk_usage_stable_store``, ``htst_activity_store``,
``base_milk_cost_tracker_store``, ``product_milk_base_cost_updater``)
and the Azure Data Lake Storage Gen2 SDK. Every caller that just needs
to read a CSV / write a CSV / list a folder / push back raw bytes goes
through here so we do not keep duplicating ADLS-Gen2 boilerplate.

What this module is and is not
------------------------------
* IS:
    - A thin, **stateless** wrapper around ``DataLakeFileClient``.
    - The single home for ETag-based optimistic-concurrency retry logic
      for arbitrary blob types (CSV, JSON, raw bytes).
    - A small ``LakehouseRef`` value object so callers don't have to
      repeat ``f"{lakehouse}.Lakehouse/Files/{path}"`` URI assembly.

* IS NOT:
    - A schema validator. Each per-feature store knows its own column
      contracts and validates before calling ``write_csv``.
    - A substitute for ``milk_mover_store.py``. That module owns its
      own JSON-shaped storage layout and uses richer mutators; this
      module is for "read/write a whole blob" CSV/bytes flows.

Configuration
-------------
``secrets_section`` is the name of a secrets block (``"fabric_htst"``,
``"fabric_milk_mover"``, ``"fabric_activity_model"``, …). The block must
provide ``workspace`` and ``lakehouse`` (display names or GUIDs); other
keys (service-principal credentials) are picked up by the shared
``fabric_auth`` chain.

When several callers want to share workspace/lakehouse settings (the
common case: everything lives in B2C Pricing > Pricing_Lakehouse), they
pass the same section name. The auth credential and the
``DataLakeServiceClient`` are cached by ``cache_name`` and by
secrets-section identity respectively, so the user only sees one
sign-in regardless of how many features pull from Fabric.
"""
from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

import pandas as pd
import streamlit as st

from data_sources.fabric_auth import (
    GUID_RE,
    FabricAuthError,
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

# Disk-persistent MSAL token-cache name. Reusing the existing HTST cache
# means a single browser sign-in unlocks every Fabric feature: HTST
# Shipment, Milk Mover, Milk Usage Stable, Activity_Model, the Base Milk
# Cost tracker and the auto-update of Product_Milk Base Cost. Same scope,
# same identity — there is no security reason to fragment the cache.
_TOKEN_CACHE_NAME: str = "streamlit_htst_shipment"

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
        """Human-readable label, e.g. 'OneLake/B2C Pricing/Pricing_Lakehouse/Files/<path>'."""
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
    ``milk_mover_store`` does, so EnvironmentCredential picks them up
    on Streamlit Cloud / headless deployments.
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
    itself cached in ``fabric_auth.get_credential``, so token refresh
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
        credential = get_credential(_TOKEN_CACHE_NAME)
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


def _directory_client(secrets_section: str, folder_path: str):
    """Return a ``DataLakeDirectoryClient`` for ``Files/<folder_path>``."""
    fs, cfg = _get_file_system_client(secrets_section)
    lh = cfg["lakehouse"]
    lakehouse_part = lh if GUID_RE.match(lh) else f"{lh}.Lakehouse"
    folder_part = folder_path.strip("/")
    suffix = f"/{folder_part}" if folder_part else ""
    return fs.get_directory_client(f"{lakehouse_part}/Files{suffix}")


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
    """List immediate children of ``Files/<folder_path>``.

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


def write_csv(
    secrets_section: str,
    blob_path: str,
    df: pd.DataFrame,
    *,
    etag: Optional[str] = None,
    to_csv_kwargs: Optional[dict[str, Any]] = None,
) -> str:
    """Serialise ``df`` to UTF-8 CSV (no index) and upload. Returns the new ETag."""
    buf = io.StringIO()
    df.to_csv(buf, index=False, **(to_csv_kwargs or {}))
    payload = buf.getvalue().encode("utf-8")
    return write_bytes(secrets_section, blob_path, payload, etag=etag)


# ── Read-modify-write helper for CSV blobs ───────────────────────────────────

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
    from azure.core.exceptions import ResourceModifiedError

    last_exc: Optional[Exception] = None
    for attempt in range(_WRITE_RETRY_ATTEMPTS):
        current_df, etag = read_csv(
            secrets_section,
            blob_path,
            read_csv_kwargs=read_csv_kwargs,
        )
        seed = current_df if current_df is not None else initial_default
        new_df = mutator(seed)
        try:
            write_csv(
                secrets_section,
                blob_path,
                new_df,
                etag=etag,
                to_csv_kwargs=to_csv_kwargs,
            )
            return new_df
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
    "read_bytes",
    "write_bytes",
    "delete_blob",
    "list_files",
    "read_csv",
    "write_csv",
    "update_csv",
    "bootstrap_bytes_if_absent",
]
