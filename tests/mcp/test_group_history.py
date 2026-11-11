"""Tests for the capped group-message history and its MCP tool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from tests.mcp.support import (
    STATE_FILE_ENV,
    TEST_HISTORY_CHAT_ID,
    TEST_HISTORY_SENT_AT,
    TEST_HISTORY_TEXT,
    TOKEN_ENV,
    append_group_message,
    append_pending_media,
    channel_state_module,
    load_group_history,
    mcp_channel,
    save_session_chat_map,
)

MAX_HISTORY_CHARS = channel_state_module.GROUP_HISTORY_TEXT_MAX_CHARS
SEEDED_MESSAGES = 3
HISTORY_WINDOW = 2
NEW_MESSAGES_AFTER_CURSOR = 2


def record(
    path: Path,
    *,
    message_id: int,
    text: str = TEST_HISTORY_TEXT,
    chat_id: int = TEST_HISTORY_CHAT_ID,
) -> None:
    append_group_message(
        path,
        chat_id=chat_id,
        message_id=message_id,
        sender_id=7,
        sender_name="alice",
        text=text,
        sent_at=TEST_HISTORY_SENT_AT,
    )


def test_append_group_message_then_load_it(tmp_path: Path):
    state_file = tmp_path / "state.json"
    record(state_file, message_id=1)
    record(state_file, message_id=2, text="trả lời đi")

    assert [entry["message_id"] for entry in load_group_history(state_file, chat_id=TEST_HISTORY_CHAT_ID)] == [1, 2]
    assert load_group_history(state_file, chat_id=TEST_HISTORY_CHAT_ID)[0] == {
        "message_id": 1,
        "sender_id": 7,
        "sender_name": "alice",
        "text": TEST_HISTORY_TEXT,
        "sent_at": TEST_HISTORY_SENT_AT,
    }


def test_append_group_message_rewrites_a_message_already_recorded(tmp_path: Path):
    state_file = tmp_path / "state.json"
    record(state_file, message_id=1, text="⚙️ đang chạy")

    record(state_file, message_id=1, text="✅ xong rồi")

    entries = load_group_history(state_file, chat_id=TEST_HISTORY_CHAT_ID)
    assert [entry["text"] for entry in entries] == ["✅ xong rồi"]
    assert entries[0]["sent_at"] == TEST_HISTORY_SENT_AT


def test_append_group_message_drops_the_oldest_beyond_the_cap(tmp_path: Path):
    state_file = tmp_path / "state.json"
    for message_id in range(1, 6):
        append_group_message(
            state_file,
            chat_id=TEST_HISTORY_CHAT_ID,
            message_id=message_id,
            sender_id=7,
            sender_name=None,
            text=f"msg {message_id}",
            sent_at=TEST_HISTORY_SENT_AT,
            max_entries=3,
        )

    assert [entry["message_id"] for entry in load_group_history(state_file, chat_id=TEST_HISTORY_CHAT_ID)] == [3, 4, 5]


def test_append_group_message_with_a_zero_cap_writes_nothing(tmp_path: Path):
    state_file = tmp_path / "state.json"

    append_group_message(
        state_file,
        chat_id=TEST_HISTORY_CHAT_ID,
        message_id=1,
        sender_id=7,
        sender_name=None,
        text=TEST_HISTORY_TEXT,
        sent_at=TEST_HISTORY_SENT_AT,
        max_entries=0,
    )

    assert not state_file.exists()


def test_append_group_message_bounds_a_long_message(tmp_path: Path):
    state_file = tmp_path / "state.json"

    record(state_file, message_id=1, text="d" * (MAX_HISTORY_CHARS + 500))

    entry = load_group_history(state_file, chat_id=TEST_HISTORY_CHAT_ID)[0]
    assert entry["text"] == "d" * MAX_HISTORY_CHARS


def test_history_survives_a_pending_media_write(tmp_path: Path):
    state_file = tmp_path / "state.json"
    record(state_file, message_id=1)

    append_pending_media(
        state_file,
        chat_id=TEST_HISTORY_CHAT_ID,
        file_id="f1",
        message_id=1,
        mime_type=None,
        queued_at=TEST_HISTORY_SENT_AT,
    )

    assert [entry["message_id"] for entry in load_group_history(state_file, chat_id=TEST_HISTORY_CHAT_ID)] == [1]


def test_load_group_history_of_an_unknown_chat_is_empty(tmp_path: Path):
    state_file = tmp_path / "state.json"
    record(state_file, message_id=1, chat_id=1)

    assert load_group_history(state_file, chat_id=2) == []


def test_load_group_history_drops_malformed_entries(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "message_history": {
                    "123": [
                        {"message_id": 1, "text": "ok", "sender_id": "nope", "sender_name": 5, "sent_at": 9},
                        {"message_id": "nope", "text": "bad id"},
                        {"message_id": 2},
                        "not a mapping",
                    ],
                    4: [{"message_id": 3, "text": "bad key"}],
                    "5": "not a list",
                }
            }
        ),
        encoding="utf-8",
    )

    assert load_group_history(state_file, chat_id=TEST_HISTORY_CHAT_ID) == [
        {"message_id": 1, "sender_id": None, "sender_name": None, "text": "ok", "sent_at": ""}
    ]


def test_load_group_history_of_invalid_json_is_empty(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text("{invalid", encoding="utf-8")

    assert load_group_history(state_file, chat_id=TEST_HISTORY_CHAT_ID) == []


def seed_tool_state(state_file: Path, *, count: int = SEEDED_MESSAGES) -> None:
    save_session_chat_map(state_file, {"s1": TEST_HISTORY_CHAT_ID})
    for message_id in range(1, count + 1):
        record(state_file, message_id=message_id, text=f"msg {message_id}")


def test_list_messages_returns_the_newest_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    seed_tool_state(state_file)
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))

    result = mcp_channel.telegram_list_messages(limit=HISTORY_WINDOW, session_id="s1")

    assert result == {
        "ok": True,
        "session_id": "s1",
        "chat_id": TEST_HISTORY_CHAT_ID,
        "messages": load_group_history(state_file, chat_id=TEST_HISTORY_CHAT_ID)[-HISTORY_WINDOW:],
        "returned": HISTORY_WINDOW,
        "recorded": SEEDED_MESSAGES,
        "newest_message_id": SEEDED_MESSAGES,
    }


def test_list_messages_reads_only_what_is_new_since_a_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    seed_tool_state(state_file)
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))

    result = mcp_channel.telegram_list_messages(since_message_id=1, session_id="s1")

    messages = cast("list[dict[str, object]]", result["messages"])
    assert [entry["message_id"] for entry in messages] == [2, 3]
    assert result["recorded"] == NEW_MESSAGES_AFTER_CURSOR


def test_list_messages_of_a_chat_with_no_history_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": TEST_HISTORY_CHAT_ID})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))

    result = mcp_channel.telegram_list_messages(session_id="s1")

    assert result["messages"] == []
    assert result["newest_message_id"] is None


def test_list_messages_rejects_a_non_positive_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    seed_tool_state(state_file, count=1)
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))

    assert mcp_channel.telegram_list_messages(limit=0, session_id="s1") == {
        "ok": False,
        "error": "`limit` must be a positive number of messages",
    }


def test_list_messages_clamps_a_limit_above_the_stored_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    seed_tool_state(state_file, count=SEEDED_MESSAGES)
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))

    result = mcp_channel.telegram_list_messages(limit=10_000, session_id="s1")

    assert result["returned"] == SEEDED_MESSAGES


def test_list_messages_reports_an_unresolved_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(tmp_path / "state.json"))

    assert mcp_channel.telegram_list_messages(session_id="nope")["ok"] is False
