"""Tests for the DingTalk attachment tool exposed to agents."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from acp_chat_bot.channels.dingtalk.api import DingTalkAPIError
from acp_chat_bot.mcp.state import (
    DINGTALK_CLIENT_ID_ENV,
    DINGTALK_CLIENT_SECRET_ENV,
    DINGTALK_ROBOT_CODE_ENV,
    STATE_FILE_ENV,
    TOKEN_ENV,
    save_conversation_target,
    save_session_chat_map,
)
from acp_chat_bot.mcp.tools import dingtalk as tools
from acp_chat_bot.mcp.tools.attachments import ALLOW_PATH_ENV

DT_CHAT_ID = "cid-1"
MEDIA_ID = "media-1"

pytestmark = pytest.mark.asyncio


class FakeDingTalkAPI:
    instances: ClassVar[list[FakeDingTalkAPI]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.uploads: list[dict[str, Any]] = []
        self.images: list[dict[str, Any]] = []
        self.files: list[dict[str, Any]] = []
        self.closed = False
        self.fail: Exception | None = None
        FakeDingTalkAPI.instances.append(self)

    async def upload_media(self, data: bytes, *, filename: str, mime_type: str, media_type: str) -> str:
        if self.fail is not None:
            raise self.fail
        self.uploads.append({"data": data, "filename": filename, "mime_type": mime_type, "media_type": media_type})
        return MEDIA_ID

    async def send_image(self, **kwargs: Any) -> dict[str, Any]:
        self.images.append(kwargs)
        return {}

    async def send_file(self, **kwargs: Any) -> dict[str, Any]:
        self.files.append(kwargs)
        return {}

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def reset_instances():
    FakeDingTalkAPI.instances.clear()


@pytest.fixture
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.json"
    save_session_chat_map(path, {"s1": DT_CHAT_ID})
    save_conversation_target(path, chat_id=DT_CHAT_ID, chat_type="group", user_id="staff-1", title="Team")
    monkeypatch.setenv(TOKEN_ENV, "client-id")
    monkeypatch.setenv(STATE_FILE_ENV, str(path))
    monkeypatch.setenv(DINGTALK_CLIENT_ID_ENV, "client-id")
    monkeypatch.setenv(DINGTALK_CLIENT_SECRET_ENV, "client-secret")
    monkeypatch.setenv(DINGTALK_ROBOT_CODE_ENV, "robot-code")
    monkeypatch.setenv(ALLOW_PATH_ENV, "1")
    return path


def patch_api(mocker) -> None:
    mocker.patch.object(tools, "DingTalkAPI", FakeDingTalkAPI)


def error_of(result: dict[str, object]) -> str:
    error = result["error"]
    assert isinstance(error, str)
    return error


async def test_an_image_from_disk_is_uploaded_and_sent(state_file: Path, tmp_path: Path, mocker):
    path = tmp_path / "picture.png"
    path.write_bytes(b"png-bytes")
    patch_api(mocker)

    result = await tools.dingtalk_send_attachment(path=str(path), session_id="s1")

    assert result == {
        "ok": True,
        "name": "picture.png",
        "mime_type": "image/png",
        "delivered_as": "image",
        "bytes": len(b"png-bytes"),
    }
    api = FakeDingTalkAPI.instances[-1]
    assert api.kwargs == {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "robot_code": "robot-code",
    }
    assert api.uploads == [
        {"data": b"png-bytes", "filename": "picture.png", "mime_type": "image/png", "media_type": "image"}
    ]
    assert api.images == [
        {
            "conversation_id": DT_CHAT_ID,
            "conversation_type": "group",
            "user_id": "staff-1",
            "media_id": MEDIA_ID,
        }
    ]
    assert api.closed is True


async def test_base64_bytes_are_sent_as_a_file(state_file: Path, mocker):
    patch_api(mocker)

    result = await tools.dingtalk_send_attachment(
        data_base64=base64.b64encode(b"notes").decode(),
        name="notes.txt",
        session_id="s1",
    )

    assert result["delivered_as"] == "file"
    api = FakeDingTalkAPI.instances[-1]
    assert api.uploads[0]["media_type"] == "file"
    assert api.files == [
        {
            "conversation_id": DT_CHAT_ID,
            "conversation_type": "group",
            "user_id": "staff-1",
            "media_id": MEDIA_ID,
            "filename": "notes.txt",
        }
    ]


async def test_a_relative_path_is_resolved_against_the_working_directory(
    state_file: Path, tmp_path: Path, mocker, monkeypatch: pytest.MonkeyPatch
):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "note.txt").write_text("hello", encoding="utf-8")
    monkeypatch.chdir(workdir)
    monkeypatch.delenv(ALLOW_PATH_ENV)
    patch_api(mocker)

    result = await tools.dingtalk_send_attachment(path="note.txt", mime_type="text/plain", session_id="s1")

    assert result["ok"] is True
    assert FakeDingTalkAPI.instances[-1].uploads[0]["data"] == b"hello"


async def test_the_robot_code_defaults_to_the_client_id(state_file: Path, tmp_path: Path, mocker, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_text("hi", encoding="utf-8")
    monkeypatch.delenv(DINGTALK_ROBOT_CODE_ENV)
    patch_api(mocker)

    await tools.dingtalk_send_attachment(path=str(path), session_id="s1")

    assert FakeDingTalkAPI.instances[-1].kwargs["robot_code"] == "client-id"


async def test_exactly_one_source_is_required(state_file: Path):
    both = await tools.dingtalk_send_attachment(path="a", data_base64="aGk=", session_id="s1")
    neither = await tools.dingtalk_send_attachment(session_id="s1")

    assert both == {"ok": False, "error": tools.EXACTLY_ONE_ATTACHMENT_SOURCE_ERROR}
    assert neither == {"ok": False, "error": tools.EXACTLY_ONE_ATTACHMENT_SOURCE_ERROR}


async def test_invalid_base64_is_rejected(state_file: Path):
    result = await tools.dingtalk_send_attachment(data_base64="not base64!", session_id="s1")

    assert result == {"ok": False, "error": "`data_base64` is not valid base64"}


async def test_a_missing_file_is_reported(state_file: Path, tmp_path: Path):
    result = await tools.dingtalk_send_attachment(path=str(tmp_path / "missing.txt"), session_id="s1")

    assert result["ok"] is False
    assert "file not found" in error_of(result)


async def test_paths_outside_the_workspace_are_rejected(
    state_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workdir = tmp_path / "work"
    workdir.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.chdir(workdir)
    monkeypatch.delenv(ALLOW_PATH_ENV)

    result = await tools.dingtalk_send_attachment(path=str(outside), session_id="s1")

    assert result["ok"] is False
    assert "inside the agent working directory" in error_of(result)


async def test_unreadable_files_are_reported(state_file: Path, tmp_path: Path, mocker):
    path = tmp_path / "locked.txt"
    path.write_text("secret", encoding="utf-8")
    mocker.patch.object(Path, "read_bytes", side_effect=OSError("permission denied"))

    result = await tools.dingtalk_send_attachment(path=str(path), session_id="s1")

    assert result["ok"] is False
    assert "could not read" in error_of(result)


async def test_missing_credentials_are_reported(state_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(DINGTALK_CLIENT_SECRET_ENV)

    result = await tools.dingtalk_send_attachment(data_base64="aGk=", session_id="s1")

    assert result["ok"] is False
    assert DINGTALK_CLIENT_SECRET_ENV in error_of(result)


async def test_a_missing_conversation_target_is_reported(state_file: Path):
    state_file.write_text(json.dumps({"sessions": {"s1": DT_CHAT_ID}}), encoding="utf-8")

    result = await tools.dingtalk_send_attachment(data_base64="aGk=", session_id="s1")

    assert result["ok"] is False


async def test_an_unresolved_session_is_reported(state_file: Path):
    result = await tools.dingtalk_send_attachment(data_base64="aGk=", session_id="nope")

    assert result["ok"] is False
    assert "unknown session_id" in error_of(result)


async def test_api_errors_are_reported_and_the_client_is_closed(state_file: Path, mocker):
    patch_api(mocker)
    mocker.patch.object(FakeDingTalkAPI, "upload_media", side_effect=DingTalkAPIError("boom"))

    result = await tools.dingtalk_send_attachment(data_base64="aGk=", session_id="s1")

    assert result == {"ok": False, "error": "boom"}
    assert FakeDingTalkAPI.instances[-1].closed is True
