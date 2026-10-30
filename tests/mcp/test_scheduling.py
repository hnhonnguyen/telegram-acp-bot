"""Tests for MCP scheduling tools."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from tests.mcp.support import (
    ACP_SCHEDULED_TASKS_DB_ENV,
    STATE_FILE_ENV,
    TEST_SCHEDULED_CHAT_ID,
    TOKEN_ENV,
    ScheduledTaskStore,
    mcp_channel,
    save_session_chat_map,
)


@pytest.mark.asyncio
async def test_schedule_task_persists_unanchored_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    scheduled_db = tmp_path / "scheduled.sqlite3"
    save_session_chat_map(state_file, {"s1": 123})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(ACP_SCHEDULED_TASKS_DB_ENV, str(scheduled_db))

    result = await mcp_channel.schedule_task(
        run_at="2026-03-30T21:00:00+00:00",
        mode="notify",
        notify_text="Review the PR now",
    )

    store = ScheduledTaskStore(scheduled_db)
    task = store.get_task(cast(str, result["task_id"]))

    assert result["ok"] is True
    assert result["anchor_message_id"] is None
    assert task is not None
    assert task.chat_id == TEST_SCHEDULED_CHAT_ID
    assert task.session_id == "s1"
    assert task.anchor_message_id is None
    assert task.mode == "notify"
    assert task.notify_text == "Review the PR now"


@pytest.mark.asyncio
async def test_schedule_task_rejects_invalid_timestamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    scheduled_db = tmp_path / "scheduled.sqlite3"
    save_session_chat_map(state_file, {"s1": 123})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(ACP_SCHEDULED_TASKS_DB_ENV, str(scheduled_db))

    result = await mcp_channel.schedule_task(
        run_at="2026-03-30T21:00:00",
        mode="notify",
        notify_text="Review the PR now",
    )

    assert result["ok"] is False
    assert "timezone" in cast(str, result["error"])


@pytest.mark.asyncio
async def test_schedule_task_rejects_invalid_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    scheduled_db = tmp_path / "scheduled.sqlite3"
    save_session_chat_map(state_file, {"s1": TEST_SCHEDULED_CHAT_ID})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(ACP_SCHEDULED_TASKS_DB_ENV, str(scheduled_db))

    result = await mcp_channel.schedule_task(
        run_at="2026-03-30T21:00:00+00:00",
        mode="later",
        notify_text="Review the PR now",
    )

    assert result["ok"] is False
    assert "mode must be one of" in cast(str, result["error"])


@pytest.mark.asyncio
async def test_schedule_task_requires_notify_text_for_notify_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state_file = tmp_path / "state.json"
    scheduled_db = tmp_path / "scheduled.sqlite3"
    save_session_chat_map(state_file, {"s1": TEST_SCHEDULED_CHAT_ID})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(ACP_SCHEDULED_TASKS_DB_ENV, str(scheduled_db))

    result = await mcp_channel.schedule_task(
        run_at="2026-03-30T21:00:00+00:00",
        mode="notify",
    )

    assert result["ok"] is False
    assert result["error"] == "notify mode requires notify_text"


@pytest.mark.asyncio
async def test_schedule_task_requires_prompt_text_for_prompt_agent_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state_file = tmp_path / "state.json"
    scheduled_db = tmp_path / "scheduled.sqlite3"
    save_session_chat_map(state_file, {"s1": TEST_SCHEDULED_CHAT_ID})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(ACP_SCHEDULED_TASKS_DB_ENV, str(scheduled_db))

    result = await mcp_channel.schedule_task(
        run_at="2026-03-30T21:00:00+00:00",
        mode="prompt_agent",
    )

    assert result["ok"] is False
    assert result["error"] == "prompt_agent mode requires prompt_text"


@pytest.mark.asyncio
async def test_schedule_task_accepts_relative_delay_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    scheduled_db = tmp_path / "scheduled.sqlite3"
    save_session_chat_map(state_file, {"s1": TEST_SCHEDULED_CHAT_ID})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(ACP_SCHEDULED_TASKS_DB_ENV, str(scheduled_db))

    before = mcp_channel.datetime.now(mcp_channel.UTC)
    result = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Review the PR now",
        delay_minutes=10,
    )
    after = mcp_channel.datetime.now(mcp_channel.UTC)
    scheduled = mcp_channel.parse_utc_timestamp(str(result["run_at"]))

    assert result["ok"] is True
    assert before <= scheduled - mcp_channel.timedelta(minutes=10) <= after


@pytest.mark.asyncio
async def test_schedule_task_rejects_mixed_absolute_and_relative_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state_file = tmp_path / "state.json"
    scheduled_db = tmp_path / "scheduled.sqlite3"
    save_session_chat_map(state_file, {"s1": TEST_SCHEDULED_CHAT_ID})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(ACP_SCHEDULED_TASKS_DB_ENV, str(scheduled_db))

    result = await mcp_channel.schedule_task(
        run_at="2026-03-30T21:00:00+00:00",
        mode="notify",
        notify_text="Review the PR now",
        delay_seconds=30,
    )

    assert result["ok"] is False
    assert result["error"] == "provide either run_at or delay inputs, not both"


@pytest.mark.asyncio
async def test_schedule_task_requires_absolute_or_relative_time_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state_file = tmp_path / "state.json"
    scheduled_db = tmp_path / "scheduled.sqlite3"
    save_session_chat_map(state_file, {"s1": TEST_SCHEDULED_CHAT_ID})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(ACP_SCHEDULED_TASKS_DB_ENV, str(scheduled_db))

    result = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Review the PR now",
    )

    assert result["ok"] is False
    assert result["error"] == "provide cron or run_at or at least one delay input"


@pytest.mark.asyncio
async def test_schedule_task_rejects_negative_delay_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state_file = tmp_path / "state.json"
    scheduled_db = tmp_path / "scheduled.sqlite3"
    save_session_chat_map(state_file, {"s1": TEST_SCHEDULED_CHAT_ID})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(ACP_SCHEDULED_TASKS_DB_ENV, str(scheduled_db))

    result = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Review the PR now",
        delay_seconds=-1,
    )

    assert result["ok"] is False
    assert result["error"] == "delay inputs must be zero or positive"


@pytest.mark.asyncio
async def test_schedule_task_reports_missing_scheduled_db_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": TEST_SCHEDULED_CHAT_ID})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.delenv(ACP_SCHEDULED_TASKS_DB_ENV, raising=False)

    result = await mcp_channel.schedule_task(
        run_at="2026-03-30T21:00:00+00:00",
        mode="notify",
        notify_text="Review the PR now",
    )

    assert result["ok"] is False
    assert result["error"] == f"missing {ACP_SCHEDULED_TASKS_DB_ENV}"


@pytest.mark.asyncio
async def test_schedule_task_reports_context_resolution_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(STATE_FILE_ENV, raising=False)

    result = await mcp_channel.schedule_task(
        run_at="2026-03-30T21:00:00+00:00",
        mode="notify",
        notify_text="Review the PR now",
    )

    assert result["ok"] is False
    assert result["error"] == f"missing {TOKEN_ENV}"
