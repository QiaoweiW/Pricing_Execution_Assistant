"""Shared TLS / CA-bundle plumbing for every OneLake DuckDB reader.

Why this module exists
----------------------
DuckDB's ``azure`` extension statically links the Azure SDK for C++ which
uses ``libcurl`` as one of its transport adapters.  That bundled
``libcurl`` has no notion of the Windows certificate store and on Linux
it looks for the CA bundle at the RHEL path
(``/etc/pki/tls/certs/ca-bundle.crt``) which does not exist on
Debian / Ubuntu / Alpine.  When the bundle isn't found, the TLS
handshake to ``onelake.blob.fabric.microsoft.com`` fails with one of:

* ``Problem with the SSL CA cert (path? access rights?)``
* ``Fail to get a new connection for: https://onelake.blob...  SSL connect error``

— neither of which has anything to do with the bearer token, the
workspace identifiers, or network reachability.

The fix is the same in every OneLake Delta reader:

1. Resolve a real CA-bundle path (operator override → standard env vars
   → ``certifi.where()``) and export the libcurl-recognised env vars
   (``CURL_CA_INFO`` / ``CURL_CA_BUNDLE`` / ``SSL_CERT_FILE``) **before**
   DuckDB's azure extension initialises libcurl.
2. Pass an ``ssl_verify`` flag through to
   :func:`fabric_auth.bind_storage_token` so the connector can also
   disable verification as an explicit, audited last-resort escape
   hatch (corporate-MITM proxies that re-sign ``*.fabric.microsoft.com``
   certificates with a CA the workstation cannot otherwise trust).

This module owns step 1's resolution helpers and step 2's flag
interpretation.  It is import-cheap, has no Streamlit / Azure imports
at module load time, and is consumed by every Delta-reader connector
(HTST, dp_dimitems, future OneLake tables).

See https://github.com/duckdb/duckdb_azure/issues/8 for the upstream
discussion.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional


def resolve_ca_cert_file(cfg: Mapping[str, str]) -> Optional[str]:
    """Return a filesystem path to a CA bundle libcurl can use, or ``None``.

    Resolution order (first hit wins):

    1. ``ca_cert_file`` from *cfg* — explicit operator override that
       the operator can set in their secrets section to a corporate
       root-CA bundle (e.g. when Zscaler / Netskope / a forward-proxy
       is doing TLS inspection on ``*.fabric.microsoft.com``).
    2. ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE`` / ``SSL_CERT_FILE``
       env vars — the de-facto standard for Python TLS clients on
       locked-down corporate workstations; honored transparently.
    3. ``certifi.where()`` — Mozilla root-CA bundle shipped with the
       ``certifi`` package (transitive dep of ``requests`` and
       ``azure-*``), so it is virtually always available.  Sufficient
       for any non-MITM connection.

    Returning a real path lets the caller export ``CURL_CA_INFO`` /
    ``CURL_CA_BUNDLE`` *before* DuckDB's azure extension initialises
    libcurl — see :func:`apply_ca_cert_env`.

    Returns ``None`` only when none of the three candidate paths
    resolve to a real file (extremely rare; means ``certifi`` is not
    installed AND no env vars / override are configured).
    """
    explicit = cfg.get("ca_cert_file")
    if explicit and os.path.isfile(explicit):
        return explicit

    for env_name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        env_val = os.environ.get(env_name)
        if env_val and os.path.isfile(env_val):
            return env_val

    try:
        import certifi  # transitive dep of requests / azure-* — always present
    except ImportError:
        return None
    bundle = certifi.where()
    return bundle if os.path.isfile(bundle) else None


def ssl_verify_enabled(cfg: Mapping[str, str]) -> bool:
    """Return ``True`` unless secrets opt out via ``ssl_verify = false``.

    Last-resort escape hatch for situations where (a) a corporate MITM
    proxy is in play, (b) the operator cannot get the corporate root
    CA file, and (c) the alternative is the page being completely
    unusable.  Callers are expected to log loudly when this returns
    ``False`` so it is impossible to leave on by accident.
    """
    raw = str(cfg.get("ssl_verify", "true")).strip().lower()
    return raw not in ("false", "0", "no", "off")


def apply_ca_cert_env(ca_cert_file: Optional[str]) -> None:
    """Set the libcurl-recognised CA-bundle env vars in-process.

    No-op when *ca_cert_file* is ``None`` — the caller already
    handled the rare "no bundle available anywhere" case.

    Uses ``setdefault`` semantics: an operator who explicitly set
    these env vars BEFORE launching Python keeps their override.
    Setting all three covers libcurl, OpenSSL, and the Python
    ``ssl`` module in one shot — cheap insurance against the
    bundled libcurl picking a different env var than we expect.
    """
    if not ca_cert_file:
        return
    for env_name in ("CURL_CA_INFO", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        os.environ.setdefault(env_name, ca_cert_file)


def tls_error_hint(exc: BaseException, *, section: str = "fabric_htst") -> str:
    """Return a remediation hint when *exc* looks like a libcurl TLS/CA failure.

    Empty string when the error is unrelated to TLS, so callers can append it
    unconditionally.  Keeps the "how do I fix the SSL connect error" guidance
    identical across every OneLake Delta reader.
    """
    msg = str(exc).lower()
    if not any(tok in msg for tok in ("ssl", "certificate", "ca cert", "curl")):
        return ""
    return (
        "\n\nThis looks like a TLS / CA-certificate failure inside DuckDB's "
        "bundled libcurl — NOT a problem with your token, workspace identifiers "
        "or permissions (a corporate proxy is likely re-signing "
        "*.fabric.microsoft.com).  Fix it in .streamlit/secrets.toml under "
        f"[{section}]:\n"
        "  (a) [PREFERRED] point at a CA bundle your machine trusts:\n"
        '        ca_cert_file = "C:/path/to/corporate_ca.pem"\n'
        "  (b) [LAST RESORT, trusted networks only] skip verification:\n"
        "        ssl_verify = false"
    )


__all__ = [
    "apply_ca_cert_env",
    "resolve_ca_cert_file",
    "ssl_verify_enabled",
    "tls_error_hint",
]
