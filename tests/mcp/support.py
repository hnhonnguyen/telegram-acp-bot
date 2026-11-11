from __future__ import annotations

from telegram_acp_bot.agent_identity import DEFAULT_AGENT_NAME, agent_state_dir
from telegram_acp_bot.mcp import server as mcp_channel
from telegram_acp_bot.mcp import state as channel_state_module
from telegram_acp_bot.mcp.state import (
    AGENT_NAME_ENV,
    MAX_GROUP_HISTORY_PER_CHAT,
    MAX_PENDING_MEDIA_PER_CHAT,
    PER_AGENT_STATE_FILE_NAME,
    STATE_FILE_ENV,
    TOKEN_ENV,
    append_group_message,
    append_pending_media,
    default_state_file,
    load_group_history,
    load_last_session_id,
    load_pending_media,
    load_prompt_message_id,
    resolve_state_file,
    save_last_session_id,
    save_prompt_message_id,
    save_session_chat_map,
    take_pending_media,
)
from telegram_acp_bot.scheduled_tasks import ACP_SCHEDULED_TASKS_DB_ENV, ScheduledTaskStore

STATE_FILE_PRIVATE_MODE = 0o600
TEST_SCHEDULED_CHAT_ID = 123
TEST_MEDIA_CHAT_ID = 123
TEST_MEDIA_FILE_ID = "AGAC queued_file_id"
TEST_MEDIA_MESSAGE_ID = 42
TEST_QUEUED_AT = "2026-03-30T09:00:00+00:00"
TEST_HISTORY_CHAT_ID = 123
TEST_HISTORY_TEXT = "ai ơi"
TEST_HISTORY_SENT_AT = "2026-03-30T09:00:00+00:00"

__all__ = [
    "ACP_SCHEDULED_TASKS_DB_ENV",
    "AGENT_NAME_ENV",
    "DEFAULT_AGENT_NAME",
    "MAX_GROUP_HISTORY_PER_CHAT",
    "MAX_PENDING_MEDIA_PER_CHAT",
    "PER_AGENT_STATE_FILE_NAME",
    "STATE_FILE_ENV",
    "STATE_FILE_PRIVATE_MODE",
    "TEST_HISTORY_CHAT_ID",
    "TEST_HISTORY_SENT_AT",
    "TEST_HISTORY_TEXT",
    "TEST_MEDIA_CHAT_ID",
    "TEST_MEDIA_FILE_ID",
    "TEST_MEDIA_MESSAGE_ID",
    "TEST_QUEUED_AT",
    "TEST_SCHEDULED_CHAT_ID",
    "TOKEN_ENV",
    "ScheduledTaskStore",
    "agent_state_dir",
    "append_group_message",
    "append_pending_media",
    "channel_state_module",
    "default_state_file",
    "load_group_history",
    "load_last_session_id",
    "load_pending_media",
    "load_prompt_message_id",
    "mcp_channel",
    "resolve_state_file",
    "save_last_session_id",
    "save_prompt_message_id",
    "save_session_chat_map",
    "take_pending_media",
]
