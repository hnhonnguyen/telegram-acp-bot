"""Tests for MCP shared state persistence helpers."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from tempfile import gettempdir

import pytest

from tests.mcp.support import (
    DEFAULT_AGENT_NAME,
    MAX_PENDING_MEDIA_PER_CHAT,
    PER_AGENT_STATE_FILE_NAME,
    STATE_FILE_PRIVATE_MODE,
    TEST_MEDIA_CHAT_ID,
    TEST_MEDIA_FILE_ID,
    TEST_MEDIA_MESSAGE_ID,
    TEST_QUEUED_AT,
    agent_state_dir,
    append_pending_media,
    channel_state_module,
    default_state_file,
    load_last_session_id,
    load_pending_media,
    load_prompt_message_id,
    mcp_channel,
    resolve_state_file,
    save_last_session_id,
    save_prompt_message_id,
    save_session_chat_map,
    take_pending_media,
)

PROMPT_MESSAGE_ID = 42
TEST_PID = 4242


def queue_media(path: Path, *, chat_id: int = TEST_MEDIA_CHAT_ID, file_id: str = TEST_MEDIA_FILE_ID) -> None:
    append_pending_media(
        path,
        chat_id=chat_id,
        file_id=file_id,
        message_id=TEST_MEDIA_MESSAGE_ID,
        mime_type="image/jpeg",
        queued_at=TEST_QUEUED_AT,
    )


def test_load_session_chat_map_handles_invalid_json(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text("{invalid", encoding="utf-8")

    assert mcp_channel.load_session_chat_map(state_file) == {}


def test_load_session_chat_map_handles_non_dict_sessions(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text('{"sessions": []}', encoding="utf-8")

    assert mcp_channel.load_session_chat_map(state_file) == {}


def test_load_last_session_id_handles_invalid_json(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text("{invalid", encoding="utf-8")

    assert load_last_session_id(state_file) is None


def test_save_last_session_id_coerces_non_dict_sessions(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"sessions": []}), encoding="utf-8")

    save_last_session_id(state_file, "s1")

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["sessions"] == {}
    assert payload["last_session_id"] == "s1"


def test_state_file_permissions_are_restricted(tmp_path: Path):
    state_file = tmp_path / "state.json"

    save_session_chat_map(state_file, {"s1": 123})
    save_last_session_id(state_file, "s1")

    mode = stat.S_IMODE(state_file.stat().st_mode)
    assert mode == STATE_FILE_PRIVATE_MODE


def test_save_prompt_message_id_round_trips(tmp_path: Path):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123})

    save_prompt_message_id(state_file, "s1", PROMPT_MESSAGE_ID)

    assert load_prompt_message_id(state_file, "s1") == PROMPT_MESSAGE_ID


def test_save_prompt_message_id_preserves_existing_session_state(tmp_path: Path):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123})
    save_last_session_id(state_file, "s1")

    save_prompt_message_id(state_file, "s1", PROMPT_MESSAGE_ID)

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["sessions"] == {"s1": 123}
    assert payload["last_session_id"] == "s1"
    assert payload["prompt_message_ids"] == {"s1": PROMPT_MESSAGE_ID}


def test_save_prompt_message_id_can_clear_existing_value(tmp_path: Path):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123})
    save_prompt_message_id(state_file, "s1", PROMPT_MESSAGE_ID)

    save_prompt_message_id(state_file, "s1", None)

    assert load_prompt_message_id(state_file, "s1") is None


def test_save_session_chat_map_preserves_prompt_message_ids(tmp_path: Path):
    state_file = tmp_path / "state.json"
    save_prompt_message_id(state_file, "s1", PROMPT_MESSAGE_ID)

    save_session_chat_map(state_file, {"s1": 123})

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["prompt_message_ids"] == {"s1": PROMPT_MESSAGE_ID}


def test_save_last_session_id_preserves_prompt_message_ids(tmp_path: Path):
    state_file = tmp_path / "state.json"
    save_prompt_message_id(state_file, "s1", PROMPT_MESSAGE_ID)

    save_last_session_id(state_file, "s1")

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["prompt_message_ids"] == {"s1": PROMPT_MESSAGE_ID}


def test_state_file_atomic_write_removes_temp_file_on_replace_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"

    def boom(src: Path | str, dst: Path | str) -> None:
        del src, dst
        raise OSError

    monkeypatch.setattr(channel_state_module.os, "replace", boom)

    with pytest.raises(OSError):
        save_session_chat_map(state_file, {"s1": 123})

    assert list(tmp_path.glob(f".{state_file.name}*.tmp")) == []


def test_append_pending_media_round_trips(tmp_path: Path):
    state_file = tmp_path / "state.json"

    queue_media(state_file)

    assert load_pending_media(state_file) == {
        str(TEST_MEDIA_CHAT_ID): [
            {
                "file_id": TEST_MEDIA_FILE_ID,
                "message_id": TEST_MEDIA_MESSAGE_ID,
                "mime_type": "image/jpeg",
                "queued_at": TEST_QUEUED_AT,
            }
        ]
    }


def test_append_pending_media_keeps_one_reference_per_file_id(tmp_path: Path):
    state_file = tmp_path / "state.json"
    queue_media(state_file)

    queue_media(state_file)

    assert len(load_pending_media(state_file)[str(TEST_MEDIA_CHAT_ID)]) == 1


def test_append_pending_media_drops_the_oldest_references_beyond_the_cap(tmp_path: Path):
    state_file = tmp_path / "state.json"

    for index in range(MAX_PENDING_MEDIA_PER_CHAT + 5):
        queue_media(state_file, file_id=f"file-{index}")

    refs = load_pending_media(state_file)[str(TEST_MEDIA_CHAT_ID)]
    assert len(refs) == MAX_PENDING_MEDIA_PER_CHAT
    assert refs[0]["file_id"] == "file-5"


def test_take_pending_media_returns_and_clears_only_that_chat(tmp_path: Path):
    state_file = tmp_path / "state.json"
    queue_media(state_file)
    queue_media(state_file, chat_id=456, file_id="other")

    refs = take_pending_media(state_file, chat_id=TEST_MEDIA_CHAT_ID)

    assert [ref["file_id"] for ref in refs] == [TEST_MEDIA_FILE_ID]
    assert list(load_pending_media(state_file)) == ["456"]


def test_take_pending_media_does_not_write_when_nothing_is_queued(tmp_path: Path):
    state_file = tmp_path / "state.json"

    assert take_pending_media(state_file, chat_id=TEST_MEDIA_CHAT_ID) == []
    assert not state_file.exists()


def test_take_pending_media_keeps_other_state_sections(tmp_path: Path):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": TEST_MEDIA_CHAT_ID})
    save_prompt_message_id(state_file, "s1", PROMPT_MESSAGE_ID)
    queue_media(state_file)

    take_pending_media(state_file, chat_id=TEST_MEDIA_CHAT_ID)

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["sessions"] == {"s1": TEST_MEDIA_CHAT_ID}
    assert payload["prompt_message_ids"] == {"s1": PROMPT_MESSAGE_ID}
    assert "pending_media" not in payload


def test_load_pending_media_drops_invalid_entries(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "pending_media": {
                    "123": [{"file_id": TEST_MEDIA_FILE_ID}, "junk", {"file_id": ""}],
                    "456": [{"file_id": "ok"}],
                    "789": [],
                    "999": "not-a-list",
                }
            }
        ),
        encoding="utf-8",
    )

    pending = load_pending_media(state_file)

    assert pending[str(TEST_MEDIA_CHAT_ID)][0]["message_id"] == 0
    assert pending[str(TEST_MEDIA_CHAT_ID)][0]["mime_type"] is None
    assert list(pending) == ["123", "456"]


def test_load_pending_media_handles_non_dict_section(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state_file.write_text('{"pending_media": []}', encoding="utf-8")

    assert load_pending_media(state_file) == {}


@pytest.mark.parametrize(
    "saver",
    [
        lambda path: save_session_chat_map(path, {"s1": TEST_MEDIA_CHAT_ID}),
        lambda path: save_last_session_id(path, "s1"),
        lambda path: save_prompt_message_id(path, "s1", PROMPT_MESSAGE_ID),
    ],
    ids=["sessions", "last_session", "prompt_message"],
)
def test_state_savers_preserve_queued_media(tmp_path: Path, saver):
    state_file = tmp_path / "state.json"
    queue_media(state_file)

    saver(state_file)

    assert list(load_pending_media(state_file)) == [str(TEST_MEDIA_CHAT_ID)]


def test_resolve_state_file_uses_the_configured_override(tmp_path: Path):
    configured = tmp_path / "channel-state.json"

    assert resolve_state_file(pid=TEST_PID, override=configured) == configured


def test_resolve_state_file_expands_home_in_an_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    assert resolve_state_file(pid=TEST_PID, override="  ~/.acp/channel-state.json ") == (
        home / ".acp" / "channel-state.json"
    )


@pytest.mark.parametrize("override", [None, "", "   "], ids=["none", "empty", "whitespace"])
def test_resolve_state_file_falls_back_when_the_override_is_blank(override: str | None):
    assert resolve_state_file(pid=TEST_PID, override=override) == default_state_file(pid=TEST_PID)


def test_resolve_state_file_without_an_override_is_scoped_to_the_pid():
    assert resolve_state_file(pid=TEST_PID) == Path(gettempdir()) / f"telegram-acp-bot-mcp-state-{TEST_PID}.json"


def test_resolve_state_file_gives_a_named_agent_a_directory_of_its_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert resolve_state_file(name="qoder", pid=TEST_PID) == (agent_state_dir("qoder") / PER_AGENT_STATE_FILE_NAME)


def test_resolve_state_file_honours_an_override_for_a_named_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pinned = tmp_path / "qoder-state.json"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    assert resolve_state_file(name="qoder", pid=TEST_PID, override=pinned) == pinned


def test_resolve_state_file_default_agent_without_override_is_pid_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The default agent has no per-agent directory, so it stays on the pid-scoped temp."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert resolve_state_file(name=DEFAULT_AGENT_NAME, pid=TEST_PID) == default_state_file(pid=TEST_PID)


def test_configured_state_file_keeps_queued_media_for_a_new_process(tmp_path: Path):
    """A pinned path is reused by the next process, so queued references outlive a restart."""
    configured = tmp_path / "channel-state.json"
    queue_media(configured)

    reopened = resolve_state_file(pid=TEST_PID + 1, override=configured)

    assert reopened == configured
    assert list(load_pending_media(reopened)) == [str(TEST_MEDIA_CHAT_ID)]
