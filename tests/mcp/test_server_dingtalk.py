"""Tests for the internal MCP server exposed to DingTalk agents."""

from __future__ import annotations

from pathlib import Path

import pytest

from acp_chat_bot.channel_policy import CHANNEL_POLICY_REMINDER
from acp_chat_bot.mcp import server_dingtalk
from acp_chat_bot.mcp.state import STATE_FILE_ENV, TOKEN_ENV, append_group_message, save_session_chat_map

DT_CHAT_ID = "cid-1"
SENT_AT = "2026-04-01T09:00:00+00:00"
SEEDED_MESSAGES = 3


def seed_state(state_file: Path, *, count: int = SEEDED_MESSAGES) -> None:
    save_session_chat_map(state_file, {"s1": DT_CHAT_ID})
    for message_id in range(1, count + 1):
        append_group_message(
            state_file,
            chat_id=DT_CHAT_ID,
            message_id=f"msg-{message_id}",
            sender_id="staff-1",
            sender_name="alice",
            text=f"hello {message_id}",
            sent_at=SENT_AT,
        )


@pytest.fixture
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.json"
    seed_state(path)
    monkeypatch.setenv(TOKEN_ENV, "client-id")
    monkeypatch.setenv(STATE_FILE_ENV, str(path))
    return path


def test_channel_info_reports_the_dingtalk_capabilities():
    payload = server_dingtalk.dingtalk_channel_info()

    assert payload["status"] == "active"
    assert payload["supports_attachment_delivery"] is True
    assert payload["supports_media_fetching"] is False
    assert payload["supports_message_reactions"] is False
    assert payload["supports_scheduled_tasks"] is True
    assert payload["supports_group_history"] is True
    assert payload["group_history_scope"] == "messages addressed to the bot"


def test_instructions_require_durable_scheduling():
    instructions = server_dingtalk.mcp.instructions
    assert instructions is not None

    assert CHANNEL_POLICY_REMINDER in instructions
    assert "engine-native" in instructions
    assert "`schedule_task`" in instructions


@pytest.mark.asyncio
async def test_server_registers_the_scheduling_tools():
    tools = await server_dingtalk.mcp.list_tools()

    names = {tool.name for tool in tools}
    assert {
        "dingtalk_channel_info",
        "dingtalk_list_messages",
        "dingtalk_send_attachment",
        "schedule_task",
        "list_scheduled_tasks",
        "update_scheduled_task",
        "cancel_scheduled_task",
    } <= names


def test_main_runs_stdio_server(mocker):
    run = mocker.patch.object(server_dingtalk.mcp, "run")

    server_dingtalk.main()

    run.assert_called_once_with("stdio")


def test_list_messages_returns_the_newest_window(state_file: Path):
    result = server_dingtalk.dingtalk_list_messages(limit=2, session_id="s1")

    messages = result["messages"]
    assert isinstance(messages, list)
    assert [message["message_id"] for message in messages] == ["msg-2", "msg-3"]


def test_list_messages_returns_only_messages_after_a_cursor(state_file: Path):
    result = server_dingtalk.dingtalk_list_messages(since_message_id="msg-1", session_id="s1")

    messages = result["messages"]
    assert isinstance(messages, list)
    assert [message["message_id"] for message in messages] == ["msg-2", "msg-3"]


def test_list_messages_ignores_an_unknown_cursor(state_file: Path):
    result = server_dingtalk.dingtalk_list_messages(since_message_id="missing", session_id="s1")

    messages = result["messages"]
    assert isinstance(messages, list)
    assert len(messages) == SEEDED_MESSAGES


@pytest.mark.parametrize("limit", [0, -1, server_dingtalk.MAX_HISTORY_LIMIT + 1])
def test_list_messages_rejects_an_out_of_range_limit(state_file: Path, limit: int):
    result = server_dingtalk.dingtalk_list_messages(limit=limit, session_id="s1")

    assert result == {"error": f"limit must be between 1 and {server_dingtalk.MAX_HISTORY_LIMIT}"}


def test_list_messages_reports_an_unresolved_session(state_file: Path):
    result = server_dingtalk.dingtalk_list_messages(session_id="nope")

    assert isinstance(result["error"], str)
