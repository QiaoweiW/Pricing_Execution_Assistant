"""
Email notification helpers for task reminders.

Uses SMTP credentials stored in Streamlit secrets under:

[task_manager_email]
smtp_host = "..."
smtp_port = 587
smtp_username = "..."
smtp_password = "..."
from_email = "..."
use_tls = true
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

import pandas as pd
import streamlit as st


class NotificationError(RuntimeError):
    """Raised when notification config or email sending fails."""


def send_due_soon_summary(tasks_due_df: pd.DataFrame) -> dict[str, list[str]]:
    """Send one summary email per assignee and return sent task ids by assignee.

    Input DataFrame must include:
    ``task_id``, ``title``, ``due_date``, ``status``, ``description``, ``assignee_email``.
    """
    if tasks_due_df.empty:
        return {}

    cfg = _read_email_config()
    grouped = {}
    for assignee, group in tasks_due_df.groupby("assignee_email"):
        email = str(assignee or "").strip()
        if not email:
            continue
        sent_ids = _send_group(cfg, email, group.copy())
        if sent_ids:
            grouped[email] = sent_ids
    return grouped


def _read_email_config() -> dict[str, Any]:
    section = "task_manager_email"
    if section not in st.secrets:
        raise NotificationError(
            "Missing [task_manager_email] in .streamlit/secrets.toml "
            "(smtp_host, smtp_port, smtp_username, smtp_password, from_email)."
        )
    cfg = dict(st.secrets[section])
    required = ("smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise NotificationError(
            f"[task_manager_email] missing required keys: {', '.join(missing)}"
        )
    cfg.setdefault("use_tls", True)
    return cfg


def _send_group(cfg: dict[str, Any], recipient: str, group: pd.DataFrame) -> list[str]:
    group = group.sort_values(by=["due_date", "status", "title"], ascending=[True, True, True])
    subject = f"Pricing Execution tasks due in 5 days ({len(group)} task(s))"
    lines = [
        "The following Pricing Execution tasks are due in 5 days:",
        "",
    ]
    for _, row in group.iterrows():
        lines.append(
            f"- {row.get('title', '')} | Due: {row.get('due_date', '')} | "
            f"Status: {row.get('status', '')} | Description: {row.get('description', '')}"
        )
    body = "\n".join(lines)

    msg = EmailMessage()
    msg["From"] = str(cfg["from_email"])
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        server = smtplib.SMTP(str(cfg["smtp_host"]), int(cfg["smtp_port"]), timeout=30)
        try:
            if bool(cfg.get("use_tls", True)):
                server.starttls()
            server.login(str(cfg["smtp_username"]), str(cfg["smtp_password"]))
            server.send_message(msg)
        finally:
            server.quit()
    except Exception as exc:  # noqa: BLE001
        raise NotificationError(f"Failed sending reminder to {recipient}: {exc}") from exc

    return group["task_id"].astype(str).tolist()

