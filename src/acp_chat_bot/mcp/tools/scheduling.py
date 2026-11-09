"""Deferred follow-up scheduling tools for the internal channel MCP server."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from mcp.server.fastmcp import FastMCP

from acp_chat_bot.channel_policy import CHANNEL_POLICY_REMINDER
from acp_chat_bot.mcp.context import RequestContext, resolve_request_context
from acp_chat_bot.scheduled_tasks import (
    ACP_SCHEDULED_TASKS_DB_ENV,
    ScheduledTask,
    ScheduledTaskMode,
    ScheduledTaskStatus,
    ScheduledTaskStore,
)
from acp_chat_bot.scheduled_tasks.recurrence import next_occurrence, normalize_cron_expression
from acp_chat_bot.scheduled_tasks.store import parse_utc_timestamp, utc_now

MISSING_SCHEDULED_DB_ERROR = f"missing {ACP_SCHEDULED_TASKS_DB_ENV}"
RUN_AT_OR_DELAY_ERROR = "provide cron or run_at or at least one delay input"
MIXED_RUN_AT_AND_DELAY_ERROR = "provide either run_at or delay inputs, not both"
MIXED_CRON_AND_TIME_ERROR = "provide either cron or run_at/delay inputs, not both"
NEGATIVE_DELAY_ERROR = "delay inputs must be zero or positive"
TASK_NOT_FOUND_ERROR = "task not found"
TASK_RUNNING_ERROR = "task is currently running; try again after it finishes"
TASK_NOT_PENDING_ERROR = "only pending tasks can be updated"
TASK_NOT_CANCELLABLE_ERROR = "only pending tasks can be cancelled"
TASK_CANCEL_FAILED_ERROR = "task could not be cancelled"
INVALID_LIMIT_ERROR = "limit must be between 1 and 50"
SCHEDULED_TASK_MODES = frozenset({"notify", "prompt_agent"})
SCHEDULED_TASK_STATUS_VALUES: tuple[ScheduledTaskStatus, ...] = (
    "pending",
    "running",
    "done",
    "failed",
    "cancelled",
)
SCHEDULED_TASK_DEFAULT_STATUSES: tuple[ScheduledTaskStatus, ...] = ("pending", "running")
SCHEDULED_TASK_LIST_LIMIT_DEFAULT = 20
SCHEDULED_TASK_LIST_LIMIT_MAX = 50
INVALID_STATUSES_ERROR = f"statuses must be a non-empty subset of {sorted(SCHEDULED_TASK_STATUS_VALUES)}"

SCHEDULE_TASK_DESCRIPTION = (
    "Create a durable reminder or recurring schedule for the current chat. "
    "Use this tool for ANY reminder, follow-up, or repeated task. Never use an engine-native cron "
    "or scheduler: those are session-scoped and disappear when the ACP session resets. "
    "Tasks created here persist across session resets, bot restarts, and engine switches. "
    "Provide exactly one of: `cron` (UTC expression; 5 fields min hour dom month dow, or 6 fields "
    "with seconds last) for a recurring task, or `run_at`/`delay_*` for a one-shot. "
    "`notify` sends text; `prompt_agent` re-prompts the chat's session, starting a new session "
    "in the default workspace when none is active. "
    "Always confirm the schedule to the user.\n\n" + CHANNEL_POLICY_REMINDER
)
LIST_SCHEDULED_TASKS_DESCRIPTION = (
    "List durable scheduled reminders for the current chat. Use it before updating or cancelling a task."
)
UPDATE_SCHEDULED_TASK_DESCRIPTION = (
    "Update a pending scheduled task. Pass an empty string as `cron` to clear recurrence. "
    "Times and cron expressions use UTC; routing fields are immutable."
)
CANCEL_SCHEDULED_TASK_DESCRIPTION = "Cancel a pending scheduled task for the current chat."


def register_scheduling_tools(mcp: FastMCP) -> None:
    """Register scheduling tools on the provided MCP server."""

    mcp.tool(name="schedule_task", description=SCHEDULE_TASK_DESCRIPTION)(schedule_task)
    mcp.tool(name="list_scheduled_tasks", description=LIST_SCHEDULED_TASKS_DESCRIPTION)(list_scheduled_tasks)
    mcp.tool(name="update_scheduled_task", description=UPDATE_SCHEDULED_TASK_DESCRIPTION)(update_scheduled_task)
    mcp.tool(name="cancel_scheduled_task", description=CANCEL_SCHEDULED_TASK_DESCRIPTION)(cancel_scheduled_task)


async def schedule_task(  # noqa: PLR0913, PLR0917
    mode: str,
    run_at: str | None = None,
    delay_seconds: int | None = None,
    delay_minutes: int | None = None,
    delay_hours: int | None = None,
    cron: str | None = None,
    prompt_text: str | None = None,
    notify_text: str | None = None,
    session_id: str | None = None,
) -> dict[str, object]:
    """Create a one-shot or recurring task for the current chat."""

    loaded = _load_context(session_id=session_id)
    if isinstance(loaded, str):
        return _fail(loaded)
    context, store = loaded

    error = _mode_text_error(mode, prompt_text=prompt_text, notify_text=notify_text)
    if error is not None:
        return _fail(error)

    has_time_input = _has_time_input(
        run_at=run_at,
        delay_seconds=delay_seconds,
        delay_minutes=delay_minutes,
        delay_hours=delay_hours,
    )
    normalized_cron: str | None
    if cron is not None:
        if has_time_input:
            return _fail(MIXED_CRON_AND_TIME_ERROR)
        try:
            normalized_cron = normalize_cron_expression(cron)
            run_at_dt = next_occurrence(normalized_cron, after=utc_now())
        except ValueError as exc:
            return _fail(str(exc))
    else:
        normalized_cron = None
        try:
            run_at_dt = resolve_run_at(
                run_at=run_at,
                delay_seconds=delay_seconds,
                delay_minutes=delay_minutes,
                delay_hours=delay_hours,
            )
        except ValueError as exc:
            return _fail(str(exc))

    task = store.create_task(
        chat_id=context.chat_id,
        session_id=context.session_id,
        mode=cast(ScheduledTaskMode, mode),
        prompt_text=prompt_text,
        notify_text=notify_text,
        run_at=run_at_dt,
        cron_expression=normalized_cron,
        source_message_id=context.source_message_id,
        requester_user_id=context.requester_user_id,
        requester_username=context.requester_username,
        requester_name=context.requester_name,
        chat_type=context.chat_type,
    )
    return {
        "ok": True,
        **_task_payload(task),
        "summary": format_scheduled_summary(run_at_dt, cron_expression=normalized_cron),
    }


async def list_scheduled_tasks(
    statuses: list[str] | None = None,
    limit: int = SCHEDULED_TASK_LIST_LIMIT_DEFAULT,
    session_id: str | None = None,
) -> dict[str, object]:
    """List scheduled tasks owned by the current Telegram chat."""

    loaded = _load_context(session_id=session_id)
    if isinstance(loaded, str):
        return _fail(loaded)
    context, store = loaded

    if limit < 1 or limit > SCHEDULED_TASK_LIST_LIMIT_MAX:
        return _fail(INVALID_LIMIT_ERROR)
    selected = _selected_statuses(statuses)
    if selected is None:
        return _fail(INVALID_STATUSES_ERROR)

    tasks = store.list_tasks_for_chat(chat_id=context.chat_id, statuses=selected, limit=limit)
    return {"ok": True, "tasks": [_task_payload(task) for task in tasks]}


async def update_scheduled_task(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0917
    task_id: str,
    mode: str | None = None,
    cron: str | None = None,
    run_at: str | None = None,
    delay_seconds: int | None = None,
    delay_minutes: int | None = None,
    delay_hours: int | None = None,
    prompt_text: str | None = None,
    notify_text: str | None = None,
    session_id: str | None = None,
) -> dict[str, object]:
    """Update one pending task owned by the current Telegram chat."""

    loaded = _load_context(session_id=session_id)
    if isinstance(loaded, str):
        return _fail(loaded)
    context, store = loaded

    task = store.get_task(task_id)
    if task is None or task.chat_id != context.chat_id:
        return _fail(TASK_NOT_FOUND_ERROR)
    if task.status == "running":
        return _fail(TASK_RUNNING_ERROR)
    if task.status != "pending":
        return _fail(f"{TASK_NOT_PENDING_ERROR} (current status: {task.status})")

    new_mode = task.mode if mode is None else mode
    error = _mode_text_error(
        new_mode,
        prompt_text=prompt_text if prompt_text is not None else task.prompt_text,
        notify_text=notify_text if notify_text is not None else task.notify_text,
    )
    if error is not None:
        return _fail(error)

    has_time_input = _has_time_input(
        run_at=run_at,
        delay_seconds=delay_seconds,
        delay_minutes=delay_minutes,
        delay_hours=delay_hours,
    )
    new_cron: str | None
    if cron is not None:
        if has_time_input:
            return _fail(MIXED_CRON_AND_TIME_ERROR)
        if cron.strip():
            try:
                new_cron = normalize_cron_expression(cron)
                new_run_at = next_occurrence(new_cron, after=utc_now())
            except ValueError as exc:
                return _fail(str(exc))
        else:
            new_cron = None
            new_run_at = task.run_at
    elif has_time_input:
        new_cron = None
        try:
            new_run_at = resolve_run_at(
                run_at=run_at,
                delay_seconds=delay_seconds,
                delay_minutes=delay_minutes,
                delay_hours=delay_hours,
            )
        except ValueError as exc:
            return _fail(str(exc))
    else:
        new_cron = task.cron_expression
        new_run_at = task.run_at

    updated = store.update_task(
        task_id=task_id,
        chat_id=context.chat_id,
        mode=cast(ScheduledTaskMode, new_mode),
        run_at=new_run_at,
        cron_expression=new_cron,
        prompt_text=prompt_text if prompt_text is not None else task.prompt_text,
        notify_text=notify_text if notify_text is not None else task.notify_text,
    )
    if updated is None:
        return _fail(f"{TASK_NOT_PENDING_ERROR} (the task may have started running)")
    return {"ok": True, "task": _task_payload(updated)}


async def cancel_scheduled_task(task_id: str, session_id: str | None = None) -> dict[str, object]:
    """Cancel one pending task owned by the current Telegram chat."""

    loaded = _load_context(session_id=session_id)
    if isinstance(loaded, str):
        return _fail(loaded)
    context, store = loaded

    task = store.get_task(task_id)
    if task is None or task.chat_id != context.chat_id:
        return _fail(TASK_NOT_FOUND_ERROR)
    if task.status == "running":
        return _fail(TASK_RUNNING_ERROR)
    if task.status != "pending":
        return _fail(f"{TASK_NOT_CANCELLABLE_ERROR} (current status: {task.status})")
    if not store.cancel_task(chat_id=context.chat_id, task_id=task_id):
        return _fail(TASK_CANCEL_FAILED_ERROR)
    return {"ok": True, "task_id": task_id}


def _load_context(session_id: str | None) -> tuple[RequestContext, ScheduledTaskStore] | str:
    context = resolve_request_context(session_id=session_id)
    if isinstance(context, str):
        return context
    try:
        store = load_scheduled_task_store()
    except RuntimeError as exc:
        return str(exc)
    return context, store


def _fail(error: str) -> dict[str, object]:
    return {"ok": False, "error": error}


def _has_time_input(
    *,
    run_at: str | None,
    delay_seconds: int | None,
    delay_minutes: int | None,
    delay_hours: int | None,
) -> bool:
    return any(value is not None for value in (run_at, delay_seconds, delay_minutes, delay_hours))


def _mode_text_error(mode: str, *, prompt_text: str | None, notify_text: str | None) -> str | None:
    if mode not in SCHEDULED_TASK_MODES:
        return "mode must be one of: notify, prompt_agent"
    if mode == "notify" and not (notify_text or "").strip():
        return "notify mode requires notify_text"
    if mode == "prompt_agent" and not (prompt_text or "").strip():
        return "prompt_agent mode requires prompt_text"
    return None


def _selected_statuses(statuses: list[str] | None) -> tuple[ScheduledTaskStatus, ...] | None:
    if statuses is None:
        return SCHEDULED_TASK_DEFAULT_STATUSES
    if not statuses or any(status not in SCHEDULED_TASK_STATUS_VALUES for status in statuses):
        return None
    return tuple(cast(ScheduledTaskStatus, status) for status in statuses)


def _task_payload(task: ScheduledTask) -> dict[str, object]:
    return {
        "task_id": task.id,
        "chat_id": task.chat_id,
        "mode": task.mode,
        "cron_expression": task.cron_expression,
        "run_at": task.run_at.isoformat(),
        "status": task.status,
        "prompt_text": task.prompt_text,
        "notify_text": task.notify_text,
        "source_message_id": task.source_message_id,
        "anchor_message_id": task.anchor_message_id,
        "reply_to_message_id": task.anchor_message_id or task.source_message_id,
        "requester_user_id": task.requester_user_id,
        "requester_username": task.requester_username,
        "requester_name": task.requester_name,
        "chat_type": task.chat_type,
        "last_error": task.last_error,
    }


def load_scheduled_task_store() -> ScheduledTaskStore:
    """Load and initialize the scheduled-task store for MCP scheduling tools."""

    db_path_raw = os.getenv(ACP_SCHEDULED_TASKS_DB_ENV, "").strip()
    if not db_path_raw:
        raise RuntimeError(MISSING_SCHEDULED_DB_ERROR)
    store = ScheduledTaskStore(Path(db_path_raw).expanduser())
    store.initialize()
    return store


def format_scheduled_summary(run_at: datetime, *, cron_expression: str | None = None) -> str:
    """Render a short summary suitable for the agent's scheduling confirmation."""

    timestamp = run_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if cron_expression is not None:
        return f"Recurring schedule {cron_expression!r} (UTC). Next run at {timestamp}."
    return f"Scheduled for {timestamp}."


def resolve_run_at(
    *,
    run_at: str | None,
    delay_seconds: int | None,
    delay_minutes: int | None,
    delay_hours: int | None,
) -> datetime:
    """Resolve an execution time from an absolute timestamp or relative delay inputs."""

    delays = [value for value in (delay_seconds, delay_minutes, delay_hours) if value is not None]
    if run_at is not None and delays:
        raise ValueError(MIXED_RUN_AT_AND_DELAY_ERROR)
    if run_at is not None:
        return parse_utc_timestamp(run_at)
    if not delays:
        raise ValueError(RUN_AT_OR_DELAY_ERROR)
    if any(value < 0 for value in delays):
        raise ValueError(NEGATIVE_DELAY_ERROR)
    delta = timedelta(
        seconds=0 if delay_seconds is None else delay_seconds,
        minutes=0 if delay_minutes is None else delay_minutes,
        hours=0 if delay_hours is None else delay_hours,
    )
    return utc_now() + delta
