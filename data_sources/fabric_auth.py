"""
Shared Microsoft Fabric / OneLake auth + DuckDB plumbing.

This module is the SINGLE source of truth for everything every Fabric-
backed connector in the codebase shares:

* Reading and validating a Fabric secrets section from ``st.secrets``.
* Promoting service-principal keys to env vars so ``EnvironmentCredential``
  picks them up.
* Building ONE persistent ``ChainedTokenCredential`` (interactive browser
  fallback for local dev, env-var SP for headless deployments) keyed on
  ONE process-wide cache name.
* Acquiring a bearer token for the OneLake Storage scope.
* Pre-warming both the credential and a shared DuckDB connection at app
  startup so the user only pays auth/extension-load latency ONCE per
  session (and only sees ONE browser sign-in across the app).
* Owning a single DuckDB connection with the ``azure`` and ``delta``
  extensions pre-loaded — every Delta-table connector binds the same
  bearer token to it via :func:`bind_storage_token` instead of paying
  ``LOAD azure`` / ``LOAD delta`` on every cold fetch.

Why one cache name across every connector
-----------------------------------------
Fragmenting the on-disk MSAL token cache (which is what happens when
each connector picks its own ``cache_persistence_options.name``) means
the user sees a fresh "sign in to Microsoft" prompt the first time
they navigate to a different connector — even when every connector
talks to the same tenant, the same identity, and the same Storage
scope. There is no security justification for that fragmentation:
the refresh token is the same OAuth grant either way. We therefore
publish ``DEFAULT_CACHE_NAME`` as a constant and every Fabric-backed
caller imports it.

Public API surface
------------------
* ``DEFAULT_CACHE_NAME``                — the single MSAL cache name
* ``STORAGE_SCOPE``                     — the Azure Storage scope string
* ``GUID_RE``                           — RFC-4122 GUID regex (URL builders)
* ``FabricAuthError``                   — typed error every caller wraps
* ``read_section``                      — read+validate a [section] from secrets
* ``promote_sp_secrets_to_env``         — copy SP keys into AZURE_* env vars
* ``get_credential``                    — cached ``ChainedTokenCredential``
* ``acquire_storage_token``             — bearer token for OneLake
* ``warmup``                            — pre-acquire token + DuckDB at session start
* ``get_duckdb_connection``             — shared DuckDB connection
* ``bind_storage_token``                — attach a bearer token to a DuckDB con
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional

import streamlit as st


logger = logging.getLogger(__name__)


# ── Public constants ─────────────────────────────────────────────────────────

# RFC-4122 GUID pattern (case-insensitive). Used by callers that build
# OneLake URIs to decide whether the workspace/lakehouse identifier is a
# GUID (canonical, stable) or a display name (friendly but rename-fragile).
GUID_RE: re.Pattern[str] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# OneLake honors any token issued for the Azure Storage scope. Centralised
# here so callers don't have to remember the magic string.
STORAGE_SCOPE: str = "https://storage.azure.com/.default"

# THE single MSAL on-disk cache name shared by every Fabric connector in
# this codebase. Keeping the historical "streamlit_htst_shipment" value
# preserves the existing on-disk caches users have already populated by
# signing in to the HTST / Milk Mover pages — only the IBP connector,
# which previously kept a separate cache, will trigger a one-time
# re-sign-in after this consolidation. After that single migration
# every connector shares one MSAL cache and one in-memory credential.
DEFAULT_CACHE_NAME: str = "streamlit_htst_shipment"

# DuckDB ``CREATE SECRET`` name used to attach the OneLake bearer token
# to the shared connection. Stable across calls so ``CREATE OR REPLACE
# SECRET <name>`` rewrites the token in place rather than leaking
# multiple parallel secrets per session.
_DUCKDB_TOKEN_SECRET_NAME: str = "onelake_token"


# ── Errors ───────────────────────────────────────────────────────────────────

class FabricAuthError(RuntimeError):
    """Raised on any configuration or auth failure in this module.

    Callers should catch this and re-raise as their own domain error so
    error messages on the page stay contextual.
    """


# ── Configuration ────────────────────────────────────────────────────────────

def read_section(
    section: str,
    *,
    required: tuple[str, ...] = (),
    defaults: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Read and validate a section from ``st.secrets``.

    Parameters
    ----------
    section
        Name of the secrets section, e.g. ``"fabric_htst"``.
    required
        Keys that must be present and non-empty.
    defaults
        Optional defaults applied with ``setdefault`` after validation —
        used by callers that have safe fallbacks for non-required keys
        (e.g. ``table = "htst_shipment"``).

    Raises
    ------
    FabricAuthError
        With a precise, actionable message — never leaks a Streamlit
        internal stack trace.
    """
    try:
        has_section = section in st.secrets
    except Exception as exc:  # noqa: BLE001
        # st.secrets raises StreamlitSecretNotFoundError when no
        # secrets.toml file exists at all. Translate to a clean message.
        raise FabricAuthError(
            "No .streamlit/secrets.toml file found.\n\n"
            "To fix:\n"
            "1. Copy .streamlit/secrets.toml.example to .streamlit/secrets.toml.\n"
            "2. Fill in the workspace, lakehouse and (optional) service-principal values.\n"
            "3. Reload this page."
        ) from exc

    if not has_section:
        raise FabricAuthError(
            f"Missing [{section}] section in .streamlit/secrets.toml. "
            f"See .streamlit/secrets.toml.example for the required schema."
        )

    cfg = dict(st.secrets[section])
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise FabricAuthError(
            f"[{section}] is missing required keys: {', '.join(missing)}. "
            "See .streamlit/secrets.toml.example."
        )
    if defaults:
        for k, v in defaults.items():
            cfg.setdefault(k, v)
    return cfg


def promote_sp_secrets_to_env(cfg: dict[str, str]) -> None:
    """If a service principal is configured in ``cfg``, expose it via env vars.

    DefaultAzureCredential's ``EnvironmentCredential`` reads
    ``AZURE_TENANT_ID`` / ``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET``.
    Copying them here means the same code path supports both local-dev
    (no SP, browser sign-in) and production-with-SP without any
    branching at the call site.

    Only sets the vars when ALL THREE keys are present and non-empty.
    Avoids overwriting variables already set in the parent process —
    e.g. an operator running ``az login`` deliberately.
    """
    keys = ("tenant_id", "client_id", "client_secret")
    if not all(cfg.get(k) for k in keys):
        return
    env_map = {
        "AZURE_TENANT_ID":     cfg["tenant_id"],
        "AZURE_CLIENT_ID":     cfg["client_id"],
        "AZURE_CLIENT_SECRET": cfg["client_secret"],
    }
    for env_name, value in env_map.items():
        os.environ.setdefault(env_name, value)


# ── Credential & token ───────────────────────────────────────────────────────

def _is_headless_runtime() -> bool:
    """Detect a headless Streamlit runtime (Streamlit Community Cloud,
    container deployments, etc.).

    The credential chain MUST NOT include ``InteractiveBrowserCredential``
    in such environments — there is no browser to receive the OAuth
    redirect, and the credential blocks waiting for a callback that
    never arrives (~5 minute hang before it finally errors). Detecting
    headless mode lets us drop the interactive credential so any auth
    failure (missing SP creds, expired secret, etc.) surfaces immediately
    as a clean error instead of freezing the page.

    Note: ``--server.headless=true`` on the CLI does NOT set
    ``STREAMLIT_SERVER_HEADLESS`` in the script's env; that flag is
    consumed by Streamlit internally. Only Community-Cloud / container
    runtimes export the env vars below, which is the only context where
    we genuinely cannot reach a browser.
    """
    return (
        os.environ.get("STREAMLIT_SHARING_MODE") is not None
        or os.environ.get("STREAMLIT_SERVER_HEADLESS", "").lower() == "true"
        or os.environ.get("STREAMLIT_RUNTIME_ENV", "").lower() == "cloud"
    )


@st.cache_resource(show_spinner=False)
def _build_credential_cached(cache_name: str):
    """Build a credential chain with disk-persistent MSAL token cache.

    Cached by ``cache_name`` so every caller using the same name shares
    one in-memory credential object (and the same MSAL cache file on disk).
    In this codebase that's everyone: see :data:`DEFAULT_CACHE_NAME`.

    Chain composition:
      * ``EnvironmentCredential`` — picks up ``AZURE_TENANT_ID`` /
        ``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET`` (set by
        ``promote_sp_secrets_to_env``). Always first so a configured
        service principal short-circuits the rest.
      * ``InteractiveBrowserCredential`` — only added when running
        with an attached browser (i.e. NOT on Streamlit Cloud or any
        container deployment). See ``_is_headless_runtime``.
    """
    try:
        from azure.identity import (
            ChainedTokenCredential,
            EnvironmentCredential,
            InteractiveBrowserCredential,
            TokenCachePersistenceOptions,
        )
    except ImportError as exc:
        raise FabricAuthError(
            "Python package 'azure-identity' is not installed. "
            "Run: pip install -r requirements.txt"
        ) from exc

    credentials = [EnvironmentCredential()]

    if not _is_headless_runtime():
        # allow_unencrypted_storage=True is required on Windows
        # workstations that lack a managed keyring (i.e. virtually all
        # corporate-locked-down PCs). The cache file lives at
        # %LOCALAPPDATA%\.IdentityService\<name> — a user-profile
        # location that needs no admin rights.
        persistence = TokenCachePersistenceOptions(
            name=cache_name,
            allow_unencrypted_storage=True,
        )
        credentials.append(
            InteractiveBrowserCredential(
                cache_persistence_options=persistence,
            )
        )

    return ChainedTokenCredential(*credentials)


def get_credential(cache_name: str = DEFAULT_CACHE_NAME):
    """Return a process-shared ``ChainedTokenCredential`` keyed by ``cache_name``.

    Always pass the default — this argument is kept for back-compat with
    the small number of callers that historically picked a different
    name. Future callers MUST use :data:`DEFAULT_CACHE_NAME` so the
    MSAL cache stays unified.
    """
    return _build_credential_cached(cache_name)


def acquire_storage_token(cache_name: str = DEFAULT_CACHE_NAME) -> str:
    """Acquire a bearer token for the OneLake Storage scope.

    On a workstation: first call typically opens a browser window for
    sign-in; subsequent calls within the cache TTL are silent.

    On a headless runtime (Streamlit Cloud / containers): only the
    service-principal env vars (``AZURE_TENANT_ID`` / ``AZURE_CLIENT_ID``
    / ``AZURE_CLIENT_SECRET``) can satisfy this call. The error message
    below adapts to surface that requirement clearly.

    Raises
    ------
    FabricAuthError
        On any underlying auth failure — caller should translate.
    """
    credential = get_credential(cache_name)
    try:
        return credential.get_token(STORAGE_SCOPE).token
    except Exception as exc:  # noqa: BLE001
        if _is_headless_runtime():
            raise FabricAuthError(
                "Could not acquire an Azure Storage token for OneLake on "
                "this headless deployment.\n\n"
                "A service principal is required to read Microsoft Fabric "
                "from Streamlit Cloud (or any --server.headless runtime). "
                "Add the following keys to your secrets in 'Manage app → "
                "Settings → Secrets':\n"
                "    tenant_id     = \"<Azure AD tenant GUID>\"\n"
                "    client_id     = \"<App registration client GUID>\"\n"
                "    client_secret = \"<Client secret value>\"\n"
                "Place them inside the [fabric_htst] block (or any "
                "[fabric_*] override block) — they are picked up by "
                "EnvironmentCredential automatically.\n\n"
                f"Underlying error: {exc}"
            ) from exc
        raise FabricAuthError(
            "Could not acquire an Azure Storage token for OneLake. "
            "If a sign-in window opened, complete it and reload this page. "
            "If no window appeared, your browser may have blocked the popup "
            "or your account is restricted from interactive auth. "
            f"Underlying error: {exc}"
        ) from exc


# ── Shared DuckDB connection ─────────────────────────────────────────────────
#
# Every Delta-table connector previously did:
#
#     con = duckdb.connect(":memory:")
#     con.execute("INSTALL azure"); con.execute("LOAD azure")
#     con.execute("INSTALL delta"); con.execute("LOAD delta")
#
# on every cold fetch — ~300–500 ms of redundant work per connector per
# query, with two parallel copies of the same boilerplate in
# ``htst_shipment`` and ``ibp_official``. Centralising here gives:
#
#   1. One in-memory DuckDB process per Streamlit session, with the
#      ``azure`` + ``delta`` extensions linked exactly once.
#   2. One place to centralise transport tweaks (``curl`` adapter,
#      optional cert pinning, etc.) — see ``bind_storage_token``.
#   3. A clean handoff for the bearer token: callers run
#      ``CREATE OR REPLACE SECRET`` per fetch so the cached connection
#      always sees a fresh, non-expired token.
#
# Thread safety: DuckDB connections are NOT thread-safe; we serialise
# access via a module-level lock. Streamlit serves one ScriptRunner
# thread per session in steady state, so this lock is uncontended in
# practice; it is here to make rare cross-session collisions safe.

_DUCKDB_LOCK: threading.Lock = threading.Lock()


@st.cache_resource(show_spinner=False)
def get_duckdb_connection():
    """Return a process-shared DuckDB connection with extensions loaded.

    Cached at module level so ``LOAD azure`` and ``LOAD delta`` execute
    exactly once per Python process — every subsequent fetch reuses the
    already-linked extensions.

    Concurrency: callers MUST acquire :data:`_DUCKDB_LOCK` (via
    :func:`duckdb_lock`) around any sequence of statements that should
    appear atomic — DuckDB connections are not thread-safe.
    """
    try:
        import duckdb  # noqa: WPS433  — lazy import keeps module-import cheap
    except ImportError as exc:
        raise FabricAuthError(
            "Python package 'duckdb' is not installed. "
            "Run: pip install -r requirements.txt"
        ) from exc

    con = duckdb.connect(":memory:")
    # INSTALL is a no-op when the extension is already in the local
    # cache (~/.duckdb/extensions); LOAD links it into this connection.
    # Both extensions LOAD in <50 ms after first install.
    con.execute("INSTALL azure")
    con.execute("LOAD azure")
    con.execute("INSTALL delta")
    con.execute("LOAD delta")
    # Force the curl transport adapter so behaviour is reproducible
    # across platforms and so the CURL_CA_INFO env var (set by the
    # connector when a corporate CA bundle is required) is honoured.
    con.execute("SET azure_transport_option_type='curl'")
    logger.info("DuckDB connection initialised with azure + delta extensions.")
    return con


def duckdb_lock() -> threading.Lock:
    """Return the module-level lock that serialises DuckDB access."""
    return _DUCKDB_LOCK


def bind_storage_token(con, token: str, *, ssl_verify: bool = True) -> None:
    """Attach (or replace) the OneLake bearer token on the DuckDB connection.

    Idempotent — ``CREATE OR REPLACE SECRET`` overwrites any prior
    binding so it is safe to call before every fetch.

    ``ssl_verify=False`` is the corporate-MITM escape hatch documented
    in ``htst_shipment.py``; toggling it per-call keeps it scoped to
    the connector that asked for it (we do not persistently disable
    verification on the shared connection).
    """
    if not ssl_verify:
        con.execute("SET enable_server_cert_verification=false")
        con.execute("SET enable_curl_server_cert_verification=false")
    else:
        # Restore default behaviour in case a previous call disabled it.
        con.execute("SET enable_server_cert_verification=true")
        con.execute("SET enable_curl_server_cert_verification=true")
    con.execute(
        f"CREATE OR REPLACE SECRET {_DUCKDB_TOKEN_SECRET_NAME} ("
        "TYPE AZURE, PROVIDER ACCESS_TOKEN, "
        f"ACCESS_TOKEN '{token}', ACCOUNT_NAME 'onelake')"
    )


# ── App-startup warm-up ──────────────────────────────────────────────────────

def warmup(*, cache_name: str = DEFAULT_CACHE_NAME) -> None:
    """Pre-acquire the OneLake token and pre-load DuckDB.

    Called once per session from ``streamlit_app.py`` so the user sees
    the (rare) browser sign-in at app start — never hidden behind a
    later page-navigation click — and the first Fabric read pays no
    cold-start tax.

    Best-effort: failures are surfaced via the raised
    :class:`FabricAuthError` so the caller can render a clean banner
    and continue (pages that don't need Fabric remain usable).
    """
    # 1. Force the credential to be built.
    get_credential(cache_name)
    # 2. Acquire one token. After this MSAL has a refresh token cached
    #    on disk (or in memory if a SP is configured), so subsequent
    #    get_token calls are silent.
    acquire_storage_token(cache_name)
    # 3. Eagerly initialise the DuckDB connection so the first
    #    Delta-scan-driven fetch on any page is hot.
    get_duckdb_connection()


__all__ = [
    "GUID_RE",
    "STORAGE_SCOPE",
    "DEFAULT_CACHE_NAME",
    "FabricAuthError",
    "read_section",
    "promote_sp_secrets_to_env",
    "get_credential",
    "acquire_storage_token",
    "get_duckdb_connection",
    "duckdb_lock",
    "bind_storage_token",
    "warmup",
]
