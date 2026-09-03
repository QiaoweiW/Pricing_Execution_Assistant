"""
Centralized Microsoft Fabric sign-in widget.

This module owns the ONLY interactive Fabric sign-in UI in the application.
It is called exclusively from the **Home & Fabric Sign-in** page.

Every other page that requires Fabric access should:
  1. Call :func:`is_fabric_signed_in` to check auth status.
  2. If not signed in, render a warning directing the user here
     (do NOT render a second sign-in prompt).

Design rationale
----------------
Previously the device-code sign-in flow (URL + code + "Check status" button)
was embedded directly in the Monthly Milk, Resin & Freight Movers module.
That created user confusion: other Fabric-backed pages (Shipment Monitor,
Bid Asset Intelligence, Demand Planner Analytics) had separate, inconsistent
warnings with no clear single place to authenticate. By centralizing here:

* Users authenticate once → every Fabric-backed view is immediately unlocked.
* Other views show a simple, consistent "go to Home & Fabric Sign-in" message
  instead of competing sign-in prompts or cryptic Azure error text.
* Future Fabric-backed features inherit the benefit without any sign-in code
  of their own.

Public API
----------
* ``render_fabric_signin_section()``  — full sign-in panel for the Home page.
* ``is_fabric_signed_in()``           — True when auth is currently healthy.
"""
from __future__ import annotations

import streamlit as st

from data_sources import fabric_auth as _fabric_auth


# ── Session-state keys (app-wide, no module prefix to keep them shared) ──────
#
# _SS_DEVCODE_AUTOSTARTED
#   Guards the "auto-fire a device-code flow on first broken render" branch.
#   Without it, every transient auth glitch (e.g. slow `az` subprocess) would
#   auto-pop a brand-new device-code prompt, confusing users who already
#   completed sign-in.  Once set, only the manual "Try sign-in again" button
#   can restart the flow.
#
# _SS_SIGNIN_RECOVERY_DONE
#   Guards the generic auth-failure-cache clearing that runs immediately after
#   a successful sign-in.  Per-store cache busts (milk mover, resin, etc.) are
#   the responsibility of each module's own recovery callback — this key only
#   protects the app-wide reset_auth_failure_cache() call so it doesn't re-fire
#   on every render while the success banner is still visible.

_SS_DEVCODE_AUTOSTARTED:   str = "_fab_signin_devcode_autostarted"
_SS_SIGNIN_RECOVERY_DONE:  str = "_fab_signin_recovery_done"


def is_fabric_signed_in() -> bool:
    """Return True when the Fabric credential chain is currently healthy.

    A ``True`` result means ``acquire_storage_token`` will succeed on the
    next call — no interactive sign-in is needed.  Pages use this to decide
    whether to attempt their Fabric reads or to show a "go to Home & Fabric
    Sign-in" warning instead.

    Note: this is a cheap, non-blocking check.  It reads the in-process
    auth-failure cache without touching the network.
    """
    return _fabric_auth.cached_auth_error() is None


def render_fabric_signin_section() -> None:
    """Render the centralized Microsoft Fabric sign-in panel.

    Called exclusively from the **Home & Fabric Sign-in** page.  Manages
    the full device-code sign-in state machine:

    State machine (first matching branch wins)
    ------------------------------------------
    A. **Auth healthy** — show a green "Connected" status.  No buttons
       needed: the credential chain already has a valid token.
    B. **Sign-in just succeeded** — clear the auth-failure cache once,
       reset state, show a success toast.
    C. **Sign-in flow active** — show the URL + code + "Check status" button
       so the user can complete the Microsoft device-code flow in a browser.
    D. **Auth broken, never auto-started this session** — auto-fire one
       device-code flow and immediately drop into state C.
    E. **Auth broken, already auto-started OR flow failed** — show the error
       and a manual "Try sign-in again" button.  We do NOT auto-fire here to
       avoid confusing users mid-session (transient credential timeouts are
       common on corporate laptops and should not auto-pop a new login prompt).
    """
    st.markdown("### 🔐 Microsoft Fabric Sign-in")
    st.caption(
        "Sign in once here to unlock all Fabric-backed views: "
        "Monthly Milk/Resin/Freight Movers, Bid Asset Intelligence, "
        "Shipment Monitor, Demand Planner Analytics, and any future "
        "Fabric features."
    )

    status = _fabric_auth.device_code_signin_status()
    err    = _fabric_auth.cached_auth_error()

    # ── (A) Auth healthy — show connected status ──────────────────────────────
    if err is None:
        # Sign-in succeeded (either from warmup, az login, or a device-code
        # flow we started).  Show a clean "connected" indicator.
        st.success(
            "✅ **Microsoft Fabric is connected.** "
            "All Fabric-backed views are available.",
            icon="✅",
        )
        # If a device-code flow was the mechanism, clean up its state so the
        # banner doesn't show stale "success" UI on subsequent renders.
        if status["state"] == "success":
            _clear_signin_state()
        return

    # ── (B) Sign-in just succeeded — run one-time recovery, show toast ────────
    if status["state"] == "success":
        if not st.session_state.get(_SS_SIGNIN_RECOVERY_DONE):
            st.session_state[_SS_SIGNIN_RECOVERY_DONE] = True
            # Clear the process-wide auth-failure cache so all Fabric stores
            # immediately re-exercise the credential chain on next read.
            _fabric_auth.reset_auth_failure_cache()
        _clear_signin_state()
        st.toast("✅ Signed in to Microsoft Fabric — all Fabric views are now available.")
        st.rerun()
        return

    # From here on we know auth is currently broken AND no recent success.

    autostarted = bool(st.session_state.get(_SS_DEVCODE_AUTOSTARTED))

    # ── (D) Auto-fire device-code on the FIRST broken render only ─────────────
    if (
        not autostarted
        and not status["thread_alive"]
        and status["state"] not in ("pending", "failed")
    ):
        st.session_state[_SS_DEVCODE_AUTOSTARTED] = True
        st.session_state.pop(_SS_SIGNIN_RECOVERY_DONE, None)
        _fabric_auth.start_device_code_signin()
        # Re-poll so thread_alive is already True for the branch below.
        status = _fabric_auth.device_code_signin_status()

    # ── (C) Sign-in flow active — show URL + code + check-status button ───────
    if status["thread_alive"] or status["state"] == "pending":
        st.markdown(
            "**Sign in to Microsoft Fabric using the steps below:**"
        )
        prompt = _fabric_auth.get_device_code_prompt()
        if prompt is not None:
            st.info(
                f"1. Open **[{prompt['verification_uri']}]({prompt['verification_uri']})**\n\n"
                f"2. Enter the code: **`{prompt['user_code']}`**\n\n"
                "3. Sign in with your **Darigold Microsoft account**.\n\n"
                "4. Click **Check sign-in status** below."
            )
        else:
            st.info(
                "Initialising sign-in flow… "
                "click **Check sign-in status** in a moment."
            )
        if st.button(
            "🔄 Check sign-in status",
            key="_fab_signin_devcode_poll",
            type="primary",
            help="Re-check whether your Microsoft sign-in has completed.",
        ):
            st.rerun()
        return

    # ── (E) Already auto-started OR flow failed — manual retry only ───────────
    error_text = (
        status["error"]
        if (status["state"] == "failed" and status["error"])
        else str(err)
    )
    st.error(
        "🔒 **Microsoft Fabric is not connected.**\n\n"
        f"{error_text}"
    )
    retry_col, reset_col = st.columns(2)
    with retry_col:
        if st.button(
            "🔁 Try sign-in again",
            key="_fab_signin_retry",
            type="primary",
            help="Clear the cached failure and start a fresh device-code sign-in flow.",
        ):
            _fabric_auth.reset_auth_failure_cache()
            _fabric_auth.reset_device_code_signin()
            # Re-arm the auto-start gate so the next render fires a fresh flow.
            st.session_state.pop(_SS_DEVCODE_AUTOSTARTED, None)
            st.session_state.pop(_SS_SIGNIN_RECOVERY_DONE, None)
            st.rerun()
    with reset_col:
        # Deeper reset for the "signed in but the chain can't reuse the token"
        # case: also drops the process-wide cached credential + the on-disk MSAL
        # token cache (which a page reload alone cannot clear), then re-arms a
        # fresh device-code flow.  Use this when "Try sign-in again" keeps failing.
        if st.button(
            "🧹 Reset Fabric credential cache",
            key="_fab_signin_reset_cache",
            help="Clear the cached credential + on-disk token cache, then start a "
                 "fresh sign-in.  Use this if 'Try sign-in again' keeps failing "
                 "with a token-cache error.",
        ):
            summary = _fabric_auth.reset_credential_cache()
            st.session_state.pop(_SS_DEVCODE_AUTOSTARTED, None)
            st.session_state.pop(_SS_SIGNIN_RECOVERY_DONE, None)
            st.toast(f"🧹 {summary}")
            st.rerun()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _clear_signin_state() -> None:
    """Reset device-code state machine and session gates after a successful sign-in.

    Called from the healthy-auth and post-success branches so subsequent renders
    see a clean slate (no dangling "success" state, re-armed auto-start gate for
    any future token-expiry event within the same session).
    """
    _fabric_auth.reset_device_code_signin()
    # Re-arm the auto-start gate so a future token-expiry can offer a fresh
    # device-code flow instead of falling straight to the manual-retry branch.
    st.session_state.pop(_SS_DEVCODE_AUTOSTARTED, None)
    st.session_state.pop(_SS_SIGNIN_RECOVERY_DONE, None)
