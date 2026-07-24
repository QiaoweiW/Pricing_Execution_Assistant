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
import time
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


# ── Device-code prompt plumbing ──────────────────────────────────────────────
#
# ``DeviceCodeCredential`` is our last-resort fallback when the more
# convenient credentials in the chain (env-var SP, interactive browser,
# Azure CLI) are unavailable.  By default azure-identity prints the
# device-code prompt to stdout — fine for a developer running streamlit
# from a terminal, but useless for a user who launched it via a desktop
# shortcut or a service.  The custom prompt callback below stashes the
# verification URL + user code into module-level state so the Streamlit
# UI can render it as a banner via :func:`get_device_code_prompt`.
#
# We don't put this in ``st.session_state`` because the credential is
# built inside an ``@st.cache_resource`` (process-wide) function and
# captures the callback by reference; reading session_state from inside
# would couple module-wide state to one specific session.

_DEVICE_CODE_PROMPT: dict[str, str] | None = None
_DEVICE_CODE_PROMPT_LOCK: threading.Lock = threading.Lock()


def _device_code_prompt_callback(verification_uri: str, user_code: str, expires_at) -> None:
    """Capture the device-code prompt for later rendering by the UI.

    Also prints to stdout so a developer running ``streamlit run`` from
    a terminal still sees the code without needing the in-app banner.
    """
    with _DEVICE_CODE_PROMPT_LOCK:
        global _DEVICE_CODE_PROMPT
        _DEVICE_CODE_PROMPT = {
            "verification_uri": verification_uri,
            "user_code": user_code,
        }
    print(
        f"\n[fabric_auth] Device-code sign-in required.\n"
        f"   1) Open {verification_uri}\n"
        f"   2) Enter code: {user_code}\n",
        flush=True,
    )


def get_device_code_prompt() -> dict[str, str] | None:
    """Return the active device-code prompt, or ``None`` if none is pending.

    Called by the page error-banner code so users can see the URL +
    code without rooting around in the terminal that started Streamlit.
    """
    with _DEVICE_CODE_PROMPT_LOCK:
        return None if _DEVICE_CODE_PROMPT is None else dict(_DEVICE_CODE_PROMPT)


def clear_device_code_prompt() -> None:
    """Drop the captured device-code prompt after a successful sign-in."""
    with _DEVICE_CODE_PROMPT_LOCK:
        global _DEVICE_CODE_PROMPT
        _DEVICE_CODE_PROMPT = None


# ── Background device-code sign-in ───────────────────────────────────────────
#
# Goal: let a user authenticate from inside the Streamlit UI even when
# their workstation has no working browser-launch path AND no Azure CLI
# installed (a pure ``webbrowser.open`` failure with ``az`` missing).
#
# Why a background thread?  ``DeviceCodeCredential.get_token`` blocks for
# up to ``DEVICE_CODE_FLOW_TIMEOUT`` seconds while it polls Azure AD for
# the user to complete sign-in.  Calling it directly on a button click
# would freeze the entire Streamlit page during that wait, hiding the
# very URL + code the user needs to see in order to complete it.  Off-
# loading to a daemon thread lets the main thread render the prompt
# immediately and poll for completion via :func:`device_code_signin_status`.
#
# State is module-level (process-wide) rather than ``st.session_state``
# because the worker thread cannot touch session_state safely — it is
# created in a non-ScriptRunner thread.  A single ``threading.Lock``
# serialises all reads/writes to the state dict.
#
# Concurrency invariants:
#   * Exactly one worker may be live at a time.  ``start_device_code_signin``
#     is a no-op while a previous attempt is still polling.
#   * The worker writes its outcome to ``_DEVICE_CODE_RESULT`` exactly
#     once and clears ``_DEVICE_CODE_THREAD`` before returning.
#   * On success the worker also resets the auth-failure cache and
#     clears the device-code prompt so the next render re-checks the
#     (now-warm) credential chain instead of the stale failure.

_DEVICE_CODE_LOCK: threading.Lock = threading.Lock()
_DEVICE_CODE_THREAD: Optional[threading.Thread] = None
_DEVICE_CODE_RESULT: dict = {"state": "idle", "error": None}


def start_device_code_signin(cache_name: str = DEFAULT_CACHE_NAME) -> bool:
    """Kick off a device-code sign-in flow in a background daemon thread.

    Returns ``True`` if a new flow was started, ``False`` if a previous
    flow is still running (idempotent — safe to call repeatedly from a
    button-click handler that triggers a Streamlit rerun).

    The flow uses the same ``DEFAULT_CACHE_NAME`` MSAL token cache as
    the rest of the credential chain; a successful sign-in here
    therefore unlocks every other credential path silently.
    """
    global _DEVICE_CODE_THREAD

    with _DEVICE_CODE_LOCK:
        if _DEVICE_CODE_THREAD is not None and _DEVICE_CODE_THREAD.is_alive():
            return False
        _DEVICE_CODE_RESULT["state"] = "pending"
        _DEVICE_CODE_RESULT["error"] = None

    # Reset prompt + failure caches OUTSIDE the lock so we don't deadlock
    # against the locks they take internally.
    clear_device_code_prompt()

    def _worker() -> None:
        global _DEVICE_CODE_THREAD
        try:
            from azure.identity import (
                DeviceCodeCredential,
                TokenCachePersistenceOptions,
            )

            persistence = TokenCachePersistenceOptions(
                name=cache_name,
                allow_unencrypted_storage=True,
            )
            cred = DeviceCodeCredential(
                cache_persistence_options=persistence,
                prompt_callback=_device_code_prompt_callback,
            )
            # Blocking — the prompt_callback fires before this enters
            # its polling loop, so the main thread already has the
            # URL + code by the time we're stuck here.
            cred.get_token(STORAGE_SCOPE)
        except Exception as exc:  # noqa: BLE001 — surfaced to UI
            with _DEVICE_CODE_LOCK:
                _DEVICE_CODE_RESULT["state"] = "failed"
                _DEVICE_CODE_RESULT["error"] = str(exc)
            logger.warning("Device-code sign-in failed: %s", exc)
        else:
            # Sign-in succeeded for the DeviceCodeCredential's own MSAL
            # instance — but the rest of the app reads tokens through
            # the SEPARATE ``InteractiveBrowserCredential`` cached
            # inside :func:`_build_credential_cached`.  Both credentials
            # share an on-disk MSAL cache file via the same
            # ``TokenCachePersistenceOptions(name=cache_name)``, but
            # the on-disk handoff can fail in rare cases (locked /
            # unwritable cache file on a corporate-locked-down PC,
            # azure-identity version mismatch where the persistent
            # cache isn't actually re-read by the silent flow, etc.).
            #
            # Without an explicit verification step the user would
            # only discover this on the NEXT page render when stores
            # start failing — which previously surfaced as a confusing
            # "kicked back to Check sign-in status" UX.  By verifying
            # here we either declare a clean success the moment the
            # warm chain CAN reuse the token, or we surface a precise
            # failure the user can act on immediately.
            #
            # Order matters: clear the prompt + reset the failure cache
            # FIRST so the verify call below actually exercises the
            # chain instead of short-circuiting on the stale failure.
            clear_device_code_prompt()
            reset_auth_failure_cache(cache_name)

            try:
                acquire_storage_token(cache_name)
            except FabricAuthError as verify_exc:
                logger.warning(
                    "Device-code sign-in succeeded but the main "
                    "credential chain cannot reuse the cached token: %s",
                    verify_exc,
                )
                # ``acquire_storage_token`` already re-recorded the
                # failure on its own failure path, so callers will
                # see the same precise error on the next render.
                with _DEVICE_CODE_LOCK:
                    _DEVICE_CODE_RESULT["state"] = "failed"
                    _DEVICE_CODE_RESULT["error"] = (
                        "Sign-in succeeded but the main credential "
                        "chain could not reuse the new token. This "
                        "usually means the on-disk MSAL token cache "
                        "is locked or unreadable. Try restarting "
                        f"Streamlit. Underlying error: {verify_exc}"
                    )
            else:
                with _DEVICE_CODE_LOCK:
                    _DEVICE_CODE_RESULT["state"] = "success"
                    _DEVICE_CODE_RESULT["error"] = None
        finally:
            with _DEVICE_CODE_LOCK:
                _DEVICE_CODE_THREAD = None

    t = threading.Thread(
        target=_worker,
        name="fabric-device-code-signin",
        daemon=True,
    )
    with _DEVICE_CODE_LOCK:
        _DEVICE_CODE_THREAD = t
    t.start()
    return True


def device_code_signin_status() -> dict:
    """Return the current device-code sign-in state.

    Schema::

        {
          "state": "idle" | "pending" | "success" | "failed",
          "error": str | None,         # only meaningful when state == "failed"
          "thread_alive": bool,        # True while the worker is still polling
        }

    The Streamlit page calls this once per render to decide whether to
    show the "click here, enter code" prompt, a success toast, or a
    failure banner.
    """
    with _DEVICE_CODE_LOCK:
        thread_alive = (
            _DEVICE_CODE_THREAD is not None and _DEVICE_CODE_THREAD.is_alive()
        )
        return {
            "state": _DEVICE_CODE_RESULT["state"],
            "error": _DEVICE_CODE_RESULT["error"],
            "thread_alive": thread_alive,
        }


def reset_device_code_signin() -> None:
    """Drop any remembered success / failure state.

    Used by the page after a successful sign-in is acknowledged so the
    banner doesn't keep showing a stale "✓ signed in" toast forever.
    Has no effect on a still-pending flow.
    """
    with _DEVICE_CODE_LOCK:
        if _DEVICE_CODE_THREAD is not None and _DEVICE_CODE_THREAD.is_alive():
            return
        _DEVICE_CODE_RESULT["state"] = "idle"
        _DEVICE_CODE_RESULT["error"] = None


# ── In-process AzureCliCredential token cache ───────────────────────────────
#
# ``AzureCliCredential`` shells out to ``az.exe account get-access-token``
# on EVERY call — there is no in-process token cache built in.  On a
# typical Monthly Movers render that does ~6–8 OneLake reads in
# sequence, that's 6–8 subprocess spawns of ``az``, each ~300 ms–2 s on
# a corporate-locked-down workstation.  Beyond the obvious latency
# penalty, the slow-tail ``az`` invocations were what re-set the auth
# failure cache mid-session and previously caused the "kicked back to
# Check sign-in status" UX bug.
#
# This wrapper layers a tiny in-process cache over the upstream
# credential, keyed by scope tuple.  Tokens are kept until they're
# within :data:`_AZ_CLI_CACHE_BUFFER_SECONDS` of their natural
# ``expires_on`` deadline (so we never hand out a near-expired token to
# a slow downstream operation), at which point the next call re-runs
# ``az`` to get a fresh one.  Concurrency-safe via a per-instance lock;
# in practice contention is zero because Streamlit runs one
# ScriptRunner thread per session in steady state.
#
# We deliberately do NOT cache failures — a transient ``az`` blip
# should re-shell on the very next call, not poison the cache for
# minutes at a time.

_AZ_CLI_CACHE_BUFFER_SECONDS: int = 60


class _CachedAzureCliCredential:
    """Thin in-process token cache wrapping :class:`AzureCliCredential`.

    Only :meth:`get_token` is required by ``ChainedTokenCredential`` —
    we forward every other attribute access to the inner credential so
    duck-typing checks (``.close()``, ``.get_token_info(...)`` on newer
    SDKs, etc.) keep working.
    """

    def __init__(self) -> None:
        from azure.identity import AzureCliCredential
        self._inner = AzureCliCredential()
        self._lock: threading.Lock = threading.Lock()
        self._cache: dict[tuple[str, ...], object] = {}

    def get_token(self, *scopes: str, **kwargs):  # type: ignore[no-untyped-def]
        # ``ChainedTokenCredential`` always passes scopes positionally,
        # so a tuple is a stable cache key.  ``kwargs`` (claims,
        # tenant_id) are intentionally NOT in the key — those are not
        # expected from the chain path and the cache is the wrong
        # layer to handle them.
        key = tuple(scopes)
        now = int(time.time())

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and (
                getattr(cached, "expires_on", 0) - now
                > _AZ_CLI_CACHE_BUFFER_SECONDS
            ):
                return cached

        # Cache miss / near-expiry — go shell out to ``az``.  Do this
        # OUTSIDE the lock so a slow subprocess can't block parallel
        # readers waiting on a different scope.
        token = self._inner.get_token(*scopes, **kwargs)
        with self._lock:
            self._cache[key] = token
        return token

    def __getattr__(self, name: str):  # noqa: D401 — passthrough
        # Delegate everything else (close, get_token_info, etc.) to the
        # inner credential so users of newer azure-identity APIs see no
        # behavioural change.
        return getattr(self._inner, name)


@st.cache_resource(show_spinner=False)
def _build_credential_cached(cache_name: str):
    """Build a credential chain with disk-persistent MSAL token cache.

    Cached by ``cache_name`` so every caller using the same name shares
    one in-memory credential object (and the same MSAL cache file on disk).
    In this codebase that's everyone: see :data:`DEFAULT_CACHE_NAME`.

    Chain composition (tried in order; first to succeed wins):
      1. ``EnvironmentCredential`` — picks up ``AZURE_TENANT_ID`` /
         ``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET`` (set by
         ``promote_sp_secrets_to_env``).  Configured service principal
         short-circuits everything else.
      2. ``InteractiveBrowserCredential`` (workstation only) — best UX
         when the browser actually launches; uses the disk-persistent
         MSAL cache below so the sign-in is silent on subsequent runs.
         Fails fast as ``CredentialUnavailable`` when ``webbrowser.open``
         can't launch (locked-down corporate Windows defaults, missing
         ``BROWSER`` env), so the chain proceeds rather than wedging.
      3. :class:`_CachedAzureCliCredential` (workstation only) — covers
         users who already authenticated for other Microsoft tooling
         via ``az login``.  Wrapped in an in-process token cache so a
         single page render that triggers several OneLake reads pays
         the ``az`` subprocess tax exactly once instead of once per
         read.  When ``az`` is not installed the inner credential
         reports ``CredentialUnavailable`` and the chain skips it
         without blocking.

    A device-code flow is intentionally NOT in the default chain —
    ``DeviceCodeCredential.get_token`` blocks for up to 60 s waiting
    for the user to complete the flow, which would freeze every app
    start on a workstation that can't open a browser.  The prompt
    helpers (:func:`get_device_code_prompt`,
    :func:`_device_code_prompt_callback`) remain available for a
    future explicit "Sign in with device code" UI button.

    Headless runtimes (Streamlit Cloud, containers) drop credentials 2+
    entirely: the only thing that can possibly succeed there is a
    configured service principal via ``EnvironmentCredential``.  See
    :func:`_is_headless_runtime` for the detection logic.
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

    # Promote a configured service principal ([fabric_htst]) to the AZURE_*
    # env vars BEFORE constructing EnvironmentCredential — which reads them at
    # __init__.  Without this, the SP is invisible on every path that hasn't
    # first gone through a lakehouse-IO read (the sign-in status check and the
    # Demand Planner Analytics Delta scans), so on a headless Cloud deployment
    # — where the SP is the ONLY usable credential — auth fails even though the
    # secret is set.  Best-effort: no [fabric_htst] SP → the paths below handle
    # it; ``setdefault`` inside the promote never clobbers an operator's own
    # AZURE_* / az-login environment.
    try:
        promote_sp_secrets_to_env(read_section("fabric_htst"))
    except Exception:  # noqa: BLE001 — never block credential build on secrets
        pass

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
        credentials.extend([
            InteractiveBrowserCredential(
                cache_persistence_options=persistence,
            ),
            _CachedAzureCliCredential(),
        ])

    return ChainedTokenCredential(*credentials)


def get_credential(cache_name: str = DEFAULT_CACHE_NAME):
    """Return a process-shared ``ChainedTokenCredential`` keyed by ``cache_name``.

    Always pass the default — this argument is kept for back-compat with
    the small number of callers that historically picked a different
    name. Future callers MUST use :data:`DEFAULT_CACHE_NAME` so the
    MSAL cache stays unified.
    """
    return _build_credential_cached(cache_name)


# ── Auth-failure short-circuit ───────────────────────────────────────────────
#
# The credential chain takes 2–10 seconds to fail on a workstation
# without a working interactive credential (each adapter probes its
# environment serially).  Without the failure cache below, every
# downstream reader (resin store, milk-mover store, mover-details
# store, …) would re-run the chain on every Streamlit rerun, stacking
# multiple identical "Failed to open a browser" banners and leaving
# the page visibly frozen for tens of seconds.
#
# The cache stores the last :class:`FabricAuthError` keyed by
# ``cache_name`` (which is process-wide); within ``_AUTH_FAILURE_TTL``
# seconds of a failure subsequent ``acquire_storage_token`` calls
# raise the cached error immediately.  A new sign-in attempt is
# permitted after the TTL elapses so a transient network blip doesn't
# permanently disable the integration.
#
# The cache is cleared automatically on the first successful token
# acquisition.  Manual override: :func:`reset_auth_failure_cache`.

_AUTH_FAILURE_TTL_SECONDS: float = 60.0
_AUTH_FAILURE_LOCK: threading.Lock = threading.Lock()
_AUTH_FAILURES: dict[str, tuple[float, "FabricAuthError"]] = {}


def reset_auth_failure_cache(cache_name: Optional[str] = None) -> None:
    """Drop the cached auth failure so the next call retries the chain.

    With ``cache_name=None`` clears every entry (used by the "Sign in"
    button).  Pass a specific ``cache_name`` to clear just one slot.
    """
    with _AUTH_FAILURE_LOCK:
        if cache_name is None:
            _AUTH_FAILURES.clear()
        else:
            _AUTH_FAILURES.pop(cache_name, None)


def _delete_msal_cache_files(cache_name: str) -> int:
    """Best-effort delete the on-disk MSAL token cache for *cache_name*.

    ``TokenCachePersistenceOptions(name=…)`` stores the cache under
    ``%LOCALAPPDATA%\\.IdentityService\\<name>`` on Windows (plus a sibling
    lock file).  Returns the number of files removed; never raises — a locked
    or absent file just contributes 0.  A no-op on platforms without
    ``LOCALAPPDATA`` (the interactive cache path is Windows-workstation only).
    """
    import glob
    import os

    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return 0
    pattern = os.path.join(base, ".IdentityService", f"{cache_name}*")
    removed = 0
    for path in glob.glob(pattern):
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass  # locked / vanished — best-effort
    return removed


def reset_credential_cache(cache_name: str = DEFAULT_CACHE_NAME) -> str:
    """Full credential reset for the "Reset Fabric credential cache" button.

    Clears everything a stuck sign-in can get wedged on, so the next attempt
    starts from a clean slate:

    1. the process-wide ``@st.cache_resource`` credential chain (so it is
       rebuilt and re-reads the on-disk MSAL cache on the next acquire) —
       this is what a browser reload alone can NOT clear;
    2. the auth-failure short-circuit + the device-code sign-in state;
    3. the on-disk MSAL token cache file(s) (best-effort), which fixes the
       "could not reuse the new token" case where that file is stale / locked.

    Returns a short human-readable summary of what was cleared (for the UI).
    Never raises — every step degrades gracefully.
    """
    cleared: list[str] = []
    try:
        _build_credential_cached.clear()   # st.cache_resource → .clear()
        cleared.append("in-process credential")
    except Exception:  # noqa: BLE001 — reset must never raise
        pass
    reset_auth_failure_cache()
    reset_device_code_signin()
    cleared.append("sign-in state")
    removed = _delete_msal_cache_files(cache_name)
    if removed:
        cleared.append(f"{removed} on-disk token file(s)")
    return "Cleared " + ", ".join(cleared) + "."


def _peek_auth_failure(cache_name: str) -> Optional["FabricAuthError"]:
    """Return the cached failure if it's still within the TTL, else ``None``."""
    with _AUTH_FAILURE_LOCK:
        entry = _AUTH_FAILURES.get(cache_name)
        if entry is None:
            return None
        ts, err = entry
        if time.monotonic() - ts < _AUTH_FAILURE_TTL_SECONDS:
            return err
        del _AUTH_FAILURES[cache_name]
        return None


def _record_auth_failure(cache_name: str, err: "FabricAuthError") -> None:
    """Stash an auth failure so subsequent calls fast-fail for the TTL."""
    with _AUTH_FAILURE_LOCK:
        _AUTH_FAILURES[cache_name] = (time.monotonic(), err)


def acquire_storage_token(cache_name: str = DEFAULT_CACHE_NAME) -> str:
    """Acquire a bearer token for the OneLake Storage scope.

    On a workstation: first call typically opens a browser window for
    sign-in; subsequent calls within the cache TTL are silent.  When
    the browser path fails (locked-down default, missing ``BROWSER``
    env), the chain falls through to ``AzureCliCredential`` and finally
    ``DeviceCodeCredential`` — see :func:`_build_credential_cached`.

    On a headless runtime (Streamlit Cloud / containers): only the
    service-principal env vars (``AZURE_TENANT_ID`` / ``AZURE_CLIENT_ID``
    / ``AZURE_CLIENT_SECRET``) can satisfy this call.

    A failed token acquisition is cached for
    :data:`_AUTH_FAILURE_TTL_SECONDS` so retry storms don't block the
    page render path.  Manual recovery via :func:`reset_auth_failure_cache`.

    Raises
    ------
    FabricAuthError
        On any underlying auth failure — caller should translate.
    """
    cached_err = _peek_auth_failure(cache_name)
    if cached_err is not None:
        raise cached_err

    credential = get_credential(cache_name)
    try:
        token = credential.get_token(STORAGE_SCOPE).token
    except Exception as exc:  # noqa: BLE001
        # Log the verbose chain failure ONCE so it's available in the
        # streamlit log for diagnosis, but keep the user-visible message
        # concise — every downstream store renders ``str(err)`` in a
        # caption, and stuffing the multi-page Azure Identity dump into
        # 4-5 panels turns the page into a wall of text.
        logger.warning(
            "Fabric auth chain failed for cache %r: %s", cache_name, exc
        )
        if _is_headless_runtime():
            err = FabricAuthError(
                "Microsoft Fabric not connected — service-principal "
                "credentials required for headless deployments. Set "
                "tenant_id / client_id / client_secret in [fabric_htst] "
                "of .streamlit/secrets.toml."
            )
        else:
            err = FabricAuthError(
                "Microsoft Fabric not connected. Sign in by running "
                "`az login` in a terminal (then reload this page), "
                "ensure your default browser is set, or configure a "
                "service principal in .streamlit/secrets.toml."
            )
        # Stash the underlying-error detail on the exception object so a
        # diagnostics-friendly caller (e.g. an "auth banner" expander)
        # can surface the full Azure Identity chain message without
        # forcing it into every per-panel caption.
        err.details = str(exc)
        _record_auth_failure(cache_name, err)
        raise err from exc

    # Success path — clear any sticky failure and any pending device-code
    # prompt the credential captured during the chain probe.
    reset_auth_failure_cache(cache_name)
    clear_device_code_prompt()
    return token


def cached_auth_error(cache_name: str = DEFAULT_CACHE_NAME) -> Optional["FabricAuthError"]:
    """Return the cached :class:`FabricAuthError` (within TTL) or ``None``.

    Pages call this once per render to detect "Fabric is currently
    broken" so they can render a single page-level banner instead of
    letting every downstream store paint its own copy of the same
    error caption.
    """
    return _peek_auth_failure(cache_name)


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


def prepare_duckdb_tls(section: str = "fabric_htst") -> bool:
    """Resolve the ``[section]`` TLS config, apply the CA-bundle env, return ``ssl_verify``.

    Call this BEFORE :func:`bind_storage_token` on any OneLake Delta scan, then
    pass the result through as ``bind_storage_token(con, token,
    ssl_verify=...)``.  It is the one-liner that makes every Delta reader honor
    the ``[fabric_htst].ca_cert_file`` / ``ssl_verify`` overrides identically —
    without it, DuckDB's bundled libcurl can't verify the OneLake TLS chain on
    a workstation behind a corporate MITM proxy and the scan fails with
    ``Fail to get a new connection … SSL connect error`` (see
    :mod:`data_sources.fabric_tls`).

    A missing / malformed secrets section degrades silently to the
    ``certifi.where()`` default (verification stays ON).
    """
    from data_sources.fabric_tls import (
        apply_ca_cert_env, resolve_ca_cert_file, ssl_verify_enabled,
    )
    try:
        cfg = dict(read_section(section))
    except FabricAuthError:
        cfg = {}
    apply_ca_cert_env(resolve_ca_cert_file(cfg))
    verify = ssl_verify_enabled(cfg)
    if not verify:
        logger.warning(
            "TLS certificate verification is DISABLED for OneLake Delta scans "
            "([%s].ssl_verify = false).  Corporate-MITM workaround — restore it "
            "by removing the flag and setting [%s].ca_cert_file to a trusted "
            "CA bundle.", section, section,
        )
    return verify


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

    Why ``reset_auth_failure_cache`` here
    -------------------------------------
    The 60-second failure cache is process-wide — if a previous session
    failed to authenticate (e.g. user hadn't ``az login``'d yet) the
    failure entry survives the new session's warmup unless we
    explicitly clear it.  That otherwise produces a confusing race
    where the user signs in, reloads the page, and STILL sees the
    cached "Microsoft Fabric not connected" error for up to 60 seconds.
    Resetting before the warmup call guarantees every fresh session
    gets a fresh chain attempt.
    """
    # 1. Force the credential to be built.
    get_credential(cache_name)
    # 2. Drop any process-wide stale failure entry so a fresh session
    #    always re-exercises the chain (defensive against the cross-
    #    session staleness window described above).
    reset_auth_failure_cache(cache_name)
    # 3. Acquire one token. After this MSAL has a refresh token cached
    #    on disk (or in memory if a SP is configured), so subsequent
    #    get_token calls are silent.
    acquire_storage_token(cache_name)
    # 4. Eagerly initialise the DuckDB connection so the first
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
    "cached_auth_error",
    "reset_auth_failure_cache",
    "reset_credential_cache",
    "get_device_code_prompt",
    "clear_device_code_prompt",
    "start_device_code_signin",
    "device_code_signin_status",
    "reset_device_code_signin",
    "get_duckdb_connection",
    "duckdb_lock",
    "bind_storage_token",
    "warmup",
]
