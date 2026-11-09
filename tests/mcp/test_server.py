"""Tests for MCP server bootstrap and capability metadata."""

from __future__ import annotations

import pytest

from acp_chat_bot.channel_policy import CHANNEL_POLICY_REMINDER
from acp_chat_bot.mcp.tools.scheduling import SCHEDULE_TASK_DESCRIPTION
from tests.mcp.support import mcp_channel


def test_telegram_channel_info_reports_active_capabilities():
    payload = mcp_channel.telegram_channel_info()
    assert payload["status"] == "active"
    assert payload["supports_attachment_delivery"] is True
    assert payload["supports_media_fetching"] is True
    assert payload["supports_message_reactions"] is True
    assert "❤" in payload["supported_reaction_emojis"]
    assert payload["supports_scheduled_tasks"] is True
    assert payload["supports_recurring_scheduled_tasks"] is True
    assert payload["supports_scheduled_task_management"] is True
    assert payload["scheduled_task_timezone"] == "UTC"
    assert payload["supports_group_history"] is True
    assert payload["supports_followup_buttons"] is False


def test_mcp_instructions_require_durable_scheduling():
    instructions = mcp_channel.mcp.instructions
    assert instructions is not None

    assert CHANNEL_POLICY_REMINDER in instructions
    assert "engine-native" in instructions
    assert "session resets" in instructions
    assert "`schedule_task`" in instructions
    assert "`list_scheduled_tasks`" in instructions
    assert "`update_scheduled_task`" in instructions
    assert "`cancel_scheduled_task`" in instructions


def test_schedule_task_description_requires_durable_scheduling():
    assert CHANNEL_POLICY_REMINDER in SCHEDULE_TASK_DESCRIPTION
    assert "Never use an engine-native cron" in SCHEDULE_TASK_DESCRIPTION
    assert "persist across session resets" in SCHEDULE_TASK_DESCRIPTION
    assert "cron" in SCHEDULE_TASK_DESCRIPTION
    assert "UTC" in SCHEDULE_TASK_DESCRIPTION


@pytest.mark.asyncio
async def test_server_registers_scheduling_crud_tools():
    tools = await mcp_channel.mcp.list_tools()

    names = {tool.name for tool in tools}
    assert {
        "schedule_task",
        "list_scheduled_tasks",
        "update_scheduled_task",
        "cancel_scheduled_task",
    } <= names


def test_main_runs_stdio_server(mocker):
    run = mocker.patch.object(mcp_channel.mcp, "run")
    mcp_channel.main()
    run.assert_called_once_with("stdio")
