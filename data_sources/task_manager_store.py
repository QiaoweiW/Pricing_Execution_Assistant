"""
OneLake-backed Task Manager store for Pricing Execution Automation.

Storage
-------
``Files/Pricing_Execution_Task_Manager/tasks.json``
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd

from data_sources import bid_asset_store as _bid_store
from data_sources import fabric_lakehouse_io as _io


class TaskManagerStoreError(RuntimeError):
    """Raised on configuration/auth/I-O failures for task manager storage."""


_SECRETS_SECTION: str = "fabric_htst"
_TASKS_BLOB_PATH: str = "Pricing_Execution_Task_Manager/tasks.json"

STATUS_TODO: str = "To Do"
STATUS_IN_PROGRESS: str = "In Progress"
STATUS_DONE: str = "Done"
ALL_STATUSES: tuple[str, ...] = (STATUS_TODO, STATUS_IN_PROGRESS, STATUS_DONE)

# ── Start-Soon auto-task rule (single rolled-up task) ────────────────────────
#
# Semantics:
#   * If ANY tracker row has Price Implementation Status == "Start Soon", a
#     single auto-task must exist in the Task Manager (one task only, not one
#     per row).
#   * If NO tracker row is "Start Soon", the auto-task (if previously created)
#     is soft-deleted so the queue stays consistent with current reality.
#   * Operators can still edit the auto-task in place; only the source-rule
#     identity is used for dedupe so manual edits to title/description/status
#     are preserved across rule runs.
_AUTO_TASK_TITLE: str = (
    "Include New Price Point into HTST & ESL tracker, Upload New Pricing into Oracle"
)
_AUTO_TASK_DESCRIPTION: str = (
    'Reference "Program Implementation Tracker" table in the "Bid Asset Intelligence" page'
)
_AUTO_TASK_RULE: str = "start_soon_tracker_rule"
_AUTO_TASK_SOURCE_KEY: str = "start_soon_rollup"


@dataclass(frozen=True)
class TaskRecord:
    """Runtime-safe task record abstraction."""

    task_id: str
    title: str
    description: str
    assignee_email: str
    due_date: str
    status: str
    created_at: str
    updated_at: str
    done_at: Optional[str]
    source_rule: Optional[str]
    source_key: Optional[str]
    last_reminder_sent_at: Optional[str]
    is_deleted: bool


def list_tasks() -> pd.DataFrame:
    """Return active tasks after retention cleanup."""
    rows = _read_rows()
    cleaned = _cleanup_done_retention(rows)
    if cleaned != rows:
        _write_rows(cleaned)
        rows = cleaned
    if not rows:
        return pd.DataFrame(columns=_task_columns())
    return pd.DataFrame(rows, columns=_task_columns())


def upsert_task(task: dict[str, Any]) -> None:
    """Create or update one task by task_id."""
    now = _now_iso()
    payload = _normalise_task(task, now=now)
    task_id = payload["task_id"]

    def _mutate(current: Any) -> list[dict[str, Any]]:
        rows = _normalise_rows(current)
        replaced = False
        for idx, row in enumerate(rows):
            if row["task_id"] == task_id:
                payload["created_at"] = row.get("created_at") or payload["created_at"]
                rows[idx] = payload
                replaced = True
                break
        if not replaced:
            rows.append(payload)
        return rows

    _update_rows(_mutate)


def soft_delete_task(task_id: str) -> None:
    """Mark a task deleted (soft-delete)."""
    now = _now_iso()

    def _mutate(current: Any) -> list[dict[str, Any]]:
        rows = _normalise_rows(current)
        for row in rows:
            if row["task_id"] == task_id:
                row["is_deleted"] = True
                row["updated_at"] = now
        return rows

    _update_rows(_mutate)


def move_task(task_id: str, new_status: str) -> None:
    """Move task across kanban lanes by status."""
    if new_status not in ALL_STATUSES:
        raise TaskManagerStoreError(f"Invalid task status: {new_status}")
    now = _now_iso()

    def _mutate(current: Any) -> list[dict[str, Any]]:
        rows = _normalise_rows(current)
        for row in rows:
            if row["task_id"] == task_id:
                row["status"] = new_status
                row["updated_at"] = now
                row["done_at"] = now if new_status == STATUS_DONE else None
        return rows

    _update_rows(_mutate)


def mark_reminder_sent(task_ids: list[str]) -> None:
    """Stamp last_reminder_sent_at for a set of task IDs."""
    if not task_ids:
        return
    task_id_set = set(task_ids)
    now = _now_iso()

    def _mutate(current: Any) -> list[dict[str, Any]]:
        rows = _normalise_rows(current)
        for row in rows:
            if row["task_id"] in task_id_set:
                row["last_reminder_sent_at"] = now
                row["updated_at"] = now
        return rows

    _update_rows(_mutate)


def sync_start_soon_tasks_from_bid_df(bid_df: pd.DataFrame) -> int:
    """Reconcile the single rolled-up Start-Soon auto-task with the latest bid data.

    Rule
    ----
    A single Task Manager task representing "there are Start Soon rows that
    need price-point work" exists whenever — and only whenever — at least one
    row in the Program Implementation Tracker has
    ``Price Implementation Status == "Start Soon"``.

    Behavior
    --------
    * Start Soon rows exist AND no live auto-task → create the auto-task with
      the canonical title/description, status ``To Do``, due date = last day of
      the current month. Returns ``+1``.
    * NO Start Soon rows AND a live auto-task exists → soft-delete the
      auto-task (operator queue stays consistent with current reality).
      Returns ``-1``.
    * Otherwise (idempotent no-op) → returns ``0``.

    The return value reports the *net* lifecycle change so the UI can render a
    meaningful message after a sync run.
    """
    tracker = _bid_store.build_program_tracker(bid_df)
    has_start_soon = False
    if not tracker.empty and _bid_store.COL_STATUS in tracker.columns:
        # Use the canonical mapping so spelling variants (e.g. "start-soon",
        # "Start Soon", "startsoon") all count as Start Soon.
        has_start_soon = (
            tracker[_bid_store.COL_STATUS]
            .apply(_bid_store.status_is_start_soon)
            .any()
        )

    delta = 0

    def _mutate(current: Any) -> list[dict[str, Any]]:
        nonlocal delta
        rows = _normalise_rows(current)

        existing_idx: Optional[int] = None
        for i, row in enumerate(rows):
            if (
                row.get("source_rule") == _AUTO_TASK_RULE
                and row.get("source_key") == _AUTO_TASK_SOURCE_KEY
                and not bool(row.get("is_deleted"))
            ):
                existing_idx = i
                break

        if has_start_soon and existing_idx is None:
            rows.append(
                _normalise_task(
                    {
                        "task_id": _task_id(_AUTO_TASK_RULE + ":" + _AUTO_TASK_SOURCE_KEY),
                        "title": _AUTO_TASK_TITLE,
                        "description": _AUTO_TASK_DESCRIPTION,
                        "assignee_email": "",
                        "due_date": _last_day_of_current_month(),
                        "status": STATUS_TODO,
                        "source_rule": _AUTO_TASK_RULE,
                        "source_key": _AUTO_TASK_SOURCE_KEY,
                        "is_deleted": False,
                    }
                )
            )
            delta = 1
        elif (not has_start_soon) and existing_idx is not None:
            rows[existing_idx]["is_deleted"] = True
            rows[existing_idx]["updated_at"] = _now_iso()
            delta = -1

        return rows

    _update_rows(_mutate)
    return delta


def tasks_due_in_days(days: int = 5) -> pd.DataFrame:
    """Return open tasks due in exactly N days and not reminded recently."""
    df = list_tasks()
    if df.empty:
        return df
    today = date.today()
    target = today + timedelta(days=days)
    due = pd.to_datetime(df["due_date"], errors="coerce").dt.date
    open_mask = df["status"].isin([STATUS_TODO, STATUS_IN_PROGRESS])
    due_mask = due == target
    reminded = pd.to_datetime(df["last_reminder_sent_at"], errors="coerce").dt.date
    reminder_mask = reminded != today
    return df[open_mask & due_mask & reminder_mask].copy()


def get_auto_task_title() -> str:
    """Return the canonical auto-generated task title."""
    return _AUTO_TASK_TITLE


def _update_rows(mutator) -> None:
    try:
        _io.update_json(_SECRETS_SECTION, _TASKS_BLOB_PATH, mutator, initial_default=[])
    except _io.LakehouseIOError as exc:
        raise TaskManagerStoreError(str(exc)) from exc


def _read_rows() -> list[dict[str, Any]]:
    try:
        payload, _etag = _io.read_json(_SECRETS_SECTION, _TASKS_BLOB_PATH)
    except _io.LakehouseIOError as exc:
        raise TaskManagerStoreError(str(exc)) from exc
    return _normalise_rows(payload)


def _write_rows(rows: list[dict[str, Any]]) -> None:
    try:
        _io.write_json(_SECRETS_SECTION, _TASKS_BLOB_PATH, rows, etag=None)
    except _io.LakehouseIOError as exc:
        raise TaskManagerStoreError(str(exc)) from exc


def _normalise_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    now = _now_iso()
    return [_normalise_task(row, now=now) for row in payload if isinstance(row, dict)]


def _normalise_task(task: dict[str, Any], *, now: Optional[str] = None) -> dict[str, Any]:
    now = now or _now_iso()
    status = str(task.get("status") or STATUS_TODO).strip()
    if status not in ALL_STATUSES:
        status = STATUS_TODO
    done_at = task.get("done_at")
    if status == STATUS_DONE and not done_at:
        done_at = now
    if status != STATUS_DONE:
        done_at = None
    return {
        "task_id": str(task.get("task_id") or _task_id(str(task.get("title", "")) + now)),
        "title": str(task.get("title") or "").strip(),
        "description": str(task.get("description") or "").strip(),
        "assignee_email": str(task.get("assignee_email") or "").strip(),
        "due_date": str(task.get("due_date") or ""),
        "status": status,
        "created_at": str(task.get("created_at") or now),
        "updated_at": now,
        "done_at": done_at,
        "source_rule": (str(task.get("source_rule")).strip() if task.get("source_rule") else None),
        "source_key": (str(task.get("source_key")).strip() if task.get("source_key") else None),
        "last_reminder_sent_at": task.get("last_reminder_sent_at"),
        "is_deleted": bool(task.get("is_deleted", False)),
    }


def _cleanup_done_retention(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=60)
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        if bool(row.get("is_deleted")):
            continue
        if row.get("status") == STATUS_DONE:
            done_at = pd.to_datetime(row.get("done_at"), errors="coerce", utc=True)
            if pd.notna(done_at) and done_at.to_pydatetime() < cutoff:
                continue
        cleaned.append(row)
    return cleaned


def _last_day_of_current_month() -> str:
    """Return the ISO date string for the last calendar day of the current month."""
    today = date.today()
    if today.month == 12:
        last_day = today.replace(day=31)
    else:
        first_of_next_month = today.replace(month=today.month + 1, day=1)
        last_day = first_of_next_month - timedelta(days=1)
    return last_day.isoformat()


def _task_id(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _task_columns() -> list[str]:
    return [
        "task_id",
        "title",
        "description",
        "assignee_email",
        "due_date",
        "status",
        "created_at",
        "updated_at",
        "done_at",
        "source_rule",
        "source_key",
        "last_reminder_sent_at",
        "is_deleted",
    ]

