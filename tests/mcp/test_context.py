"""Tests for MCP request-context resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.mcp.support import (
    STATE_FILE_ENV,
    TOKEN_ENV,
    PromptRequester,
    mcp_channel,
    save_last_session_id,
    save_prompt_message_id,
    save_prompt_requester,
    save_session_chat_map,
)

PROMPT_MESSAGE_ID = 42
REQUESTER_USER_ID = 88


def test_resolve_request_context_reports_missing_token_by_default():
    result = mcp_channel._resolve_request_context(session_id="s1")
    assert result == f"missing {TOKEN_ENV}"


def test_resolve_request_context_requires_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.setenv(STATE_FILE_ENV, str(tmp_path / "state.json"))
    result = mcp_channel._resolve_request_context(session_id="s1")
    assert result == f"missing {TOKEN_ENV}"


def test_resolve_request_context_requires_state_file_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.delenv(STATE_FILE_ENV, raising=False)
    result = mcp_channel._resolve_request_context(session_id="s1")
    assert result == f"missing {STATE_FILE_ENV}"


def test_resolve_request_context_requires_inferable_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))

    result = mcp_channel._resolve_request_context(session_id=None)

    assert result == "missing session_id and no active session could be inferred"


def test_resolve_request_context_requires_explicit_session_when_multiple_mappings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123, "s2": 456})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))

    result = mcp_channel._resolve_request_context(session_id=None)

    assert (
        result == "missing session_id: multiple active sessions exist and no last active session could be inferred. "
        "Available session_ids: s1, s2"
    )


def test_resolve_request_context_uses_last_active_session_when_multiple_mappings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123, "s2": 456})
    save_last_session_id(state_file, "s2")
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))

    result = mcp_channel._resolve_request_context(session_id=None)

    assert result == mcp_channel._RequestContext(
        token="TOKEN", chat_id=456, session_id="s2", prompt_message_id=None, requester=None, state_file=state_file
    )


def test_resolve_request_context_includes_active_prompt_message_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123})
    save_prompt_message_id(state_file, "s1", 42)
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))

    result = mcp_channel._resolve_request_context(session_id="s1")

    assert result == mcp_channel._RequestContext(
        token="TOKEN", chat_id=123, session_id="s1", prompt_message_id=42, requester=None, state_file=state_file
    )
    assert result.source_message_id == PROMPT_MESSAGE_ID
    assert result.requester_user_id is None
    assert result.requester_username is None
    assert result.requester_name is None
    assert result.chat_type is None


def test_resolve_request_context_includes_requester(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    requester = PromptRequester(
        user_id=88,
        username="alice",
        display_name="Alice Doe",
        chat_type="group",
    )
    save_session_chat_map(state_file, {"s1": 123})
    save_prompt_message_id(state_file, "s1", 42)
    save_prompt_requester(state_file, "s1", requester)
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))

    result = mcp_channel._resolve_request_context(session_id="s1")

    assert isinstance(result, mcp_channel._RequestContext)
    assert result.requester == requester
    assert result.requester_user_id == REQUESTER_USER_ID
    assert result.requester_username == "alice"
    assert result.requester_name == "Alice Doe"
    assert result.chat_type == "group"


def test_resolve_request_context_ignores_stale_last_session_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123, "s2": 456})
    save_last_session_id(state_file, "stale")
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))

    result = mcp_channel._resolve_request_context(session_id=None)

    assert isinstance(result, str)
    assert result.startswith(
        "missing session_id: multiple active sessions exist and no last active session could be inferred."
    )
