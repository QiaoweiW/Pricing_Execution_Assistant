"""
Shared Microsoft Fabric / OneLake auth helpers.

Two modules in ``data_sources/`` need the exact same ChainedTokenCredential
chain pointed at the Azure Storage scope (which OneLake honors):

* ``htst_shipment.py``   — reads the HTST Shipment Delta table.
* ``milk_mover_store.py`` — reads/writes the Milk Mover JSON blob.

This module owns the cross-cutting concerns so neither caller has to
duplicate auth code:

* Reading and validating a Fabric secrets section from ``st.secrets``.
* Promoting service-principal keys to env vars so EnvironmentCredential
  picks them up.
* Building a persistent ``ChainedTokenCredential`` (interactive browser
  fallback for local dev, env-var SP for headless deployments).
* Acquiring a bearer token for the OneLake Storage scope.

Design choices
--------------
* No Streamlit-app-specific behavior beyond the ``@st.cache_resource``
  on the credential builder. Errors raised from this module are typed
  as :class:`FabricAuthError`; each caller is expected to translate
  these to its own domain error class so end-user error messages stay
  contextual ("Could not read HTST Shipment" vs "Could not read Milk
  Mover store").
* Cache name is a parameter, not a constant, so each caller can choose
  whether to share the disk-persistent MSAL token cache with another
  caller (default in this codebase: yes — same scope, same identity,
  one less browser sign-in).
"""
from __future__ import annotations

import os
import re
from typing import Optional

import streamlit as st


# ── Public constants ─────────────────────────────────────────────────────────

# RFC-4122 GUID pattern (case-insensitive). Used by callers that build
# OneLake URIs to decide whether the workspace/lakehouse identifier is a
# GUID (canonical, stable) or a display name (friendly but rename-fragile).
GUID_RE: re.Pattern[str] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# OneLake honors the Azure Storage scope. Centralised here so callers don't
# have to remember the magic string.
STORAGE_SCOPE: str = "https://storage.azure.com/.default"


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
        Keys that must be present and non-empty in the section.
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

@st.cache_resource(show_spinner=False)
def _build_credential_cached(cache_name: str):
    """Build a credential chain with disk-persistent MSAL token cache.

    Cached by ``cache_name`` so two callers using the same name share one
    in-memory credential object (and the same MSAL cache file on disk).
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

    # allow_unencrypted_storage=True is required on Windows workstations
    # that lack a managed keyring (i.e. virtually all corporate-locked-down
    # PCs). The cache file lives at %LOCALAPPDATA%\.IdentityService\<name>
    # — a user-profile location that needs no admin rights.
    persistence = TokenCachePersistenceOptions(
        name=cache_name,
        allow_unencrypted_storage=True,
    )

    return ChainedTokenCredential(
        EnvironmentCredential(),
        InteractiveBrowserCredential(
            cache_persistence_options=persistence,
        ),
    )


def get_credential(cache_name: str):
    """Return a shared ``ChainedTokenCredential`` keyed by ``cache_name``."""
    return _build_credential_cached(cache_name)


def acquire_storage_token(cache_name: str) -> str:
    """Acquire a bearer token for the OneLake Storage scope.

    First call typically opens a browser window for sign-in; subsequent
    calls within the cache TTL are silent (cached refresh token).

    Raises
    ------
    FabricAuthError
        On any underlying auth failure — caller should translate.
    """
    credential = get_credential(cache_name)
    try:
        return credential.get_token(STORAGE_SCOPE).token
    except Exception as exc:  # noqa: BLE001
        raise FabricAuthError(
            "Could not acquire an Azure Storage token for OneLake. "
            "If a sign-in window opened, complete it and reload this page. "
            "If no window appeared, your browser may have blocked the popup "
            "or your account is restricted from interactive auth. "
            f"Underlying error: {exc}"
        ) from exc


__all__ = [
    "GUID_RE",
    "STORAGE_SCOPE",
    "FabricAuthError",
    "read_section",
    "promote_sp_secrets_to_env",
    "get_credential",
    "acquire_storage_token",
]
