"""Tests for the MTProto large-file download tool."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_acp_bot.mcp import server as mcp_channel
from telegram_acp_bot.mcp.tools import downloads as downloads_module
from telegram_acp_bot.mcp.tools import mtproto as mtproto_module
from tests.mcp.support import STATE_FILE_ENV, TOKEN_ENV, save_session_chat_map

TEST_API_ID = 12345
TEST_API_HASH = "hash"
TEST_CHAT_ID = 123
TEST_MESSAGE_ID = 42


@pytest.fixture
def channel_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": TEST_CHAT_ID})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(mtproto_module.API_ID_ENV, str(TEST_API_ID))
    monkeypatch.setenv(mtproto_module.API_HASH_ENV, TEST_API_HASH)
    mtproto_module._clients.clear()
    yield state_file
    mtproto_module._clients.clear()


def make_client(mocker, *, file: object | None):
    client = mocker.AsyncMock()
    client.is_connected.return_value = True
    client.get_messages.return_value = SimpleNamespace(file=file) if file is not None else None
    return client


def make_patch(mocker, client):
    factory = mocker.MagicMock(return_value=client)
    mocker.patch("telegram_acp_bot.mcp.tools.mtproto.TelegramClient", factory)
    return factory


def make_message_file(*, name: str | None, mime_type: str | None) -> SimpleNamespace:
    return SimpleNamespace(name=name, mime_type=mime_type)


def fake_download(payload: bytes):
    async def _download(message, file=None):
        target = Path(str(file))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return str(target)

    return _download


@pytest.mark.asyncio
async def test_download_file_keeps_the_original_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker, channel_env: Path
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    client = make_client(mocker, file=make_message_file(name="backup.zip", mime_type="application/zip"))
    client.download_media = fake_download(b"big payload")
    make_patch(mocker, client)

    result = await mcp_channel.telegram_download_file(session_id="s1", message_id=TEST_MESSAGE_ID)

    assert result["ok"] is True
    path = Path(str(result["path"]))
    assert path == workspace / mcp_channel.DEFAULT_MEDIA_DIRNAME / "backup.zip"
    assert path.read_bytes() == b"big payload"
    assert result["bytes"] == len(b"big payload")
    assert result["mime_type"] == "application/zip"
    client.get_messages.assert_awaited_once_with(TEST_CHAT_ID, ids=TEST_MESSAGE_ID)


@pytest.mark.asyncio
async def test_download_file_falls_back_to_the_message_id_without_a_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker, channel_env: Path
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    client = make_client(mocker, file=make_message_file(name=None, mime_type="video/mp4"))
    client.download_media = fake_download(b"video")
    make_patch(mocker, client)

    result = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID)

    assert result["ok"] is True
    assert Path(str(result["path"])).name == f"message-{TEST_MESSAGE_ID}.mp4"


@pytest.mark.asyncio
async def test_download_file_honours_a_relative_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker, channel_env: Path
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    client = make_client(mocker, file=make_message_file(name="backup.zip", mime_type="application/zip"))
    client.download_media = fake_download(b"zip")
    make_patch(mocker, client)

    result = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID, dest="archives/backup.zip")

    assert result["ok"] is True
    assert Path(str(result["path"])) == workspace / "archives" / "backup.zip"


@pytest.mark.asyncio
async def test_download_file_rejects_a_dest_outside_the_workspace(mocker, channel_env: Path):
    client = make_client(mocker, file=make_message_file(name="backup.zip", mime_type="application/zip"))
    make_patch(mocker, client)

    result = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID, dest="../../escape.zip")

    assert result["ok"] is False
    assert result["error"] == mcp_channel.MEDIA_OUTSIDE_WORKSPACE_ERROR


@pytest.mark.asyncio
async def test_download_file_reports_a_message_without_media(mocker, channel_env: Path):
    client = make_client(mocker, file=None)
    make_patch(mocker, client)

    result = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID)

    assert result["ok"] is False
    assert result["error"] == mcp_channel.MESSAGE_WITHOUT_MEDIA_ERROR.format(
        message_id=TEST_MESSAGE_ID, chat_id=TEST_CHAT_ID
    )


@pytest.mark.asyncio
async def test_download_file_requires_the_mtproto_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": TEST_CHAT_ID})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.delenv(mtproto_module.API_ID_ENV, raising=False)
    monkeypatch.delenv(mtproto_module.API_HASH_ENV, raising=False)

    result = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID)

    assert result["ok"] is False
    assert result["error"] == mcp_channel.MISSING_API_CREDENTIALS_ERROR


@pytest.mark.asyncio
async def test_download_file_reports_a_login_failure_without_caching(mocker, channel_env: Path):
    factory = mocker.MagicMock()
    factory.return_value.start = mocker.AsyncMock(side_effect=OSError("network down"))
    factory.return_value.disconnect = mocker.AsyncMock()
    mocker.patch("telegram_acp_bot.mcp.tools.mtproto.TelegramClient", factory)

    result = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID)

    assert result["ok"] is False
    assert "MTProto login failed" in str(result["error"])
    assert mtproto_module._clients == {}


@pytest.mark.asyncio
async def test_download_file_reuses_one_client_across_calls(mocker, channel_env: Path):
    client = make_client(mocker, file=make_message_file(name="backup.zip", mime_type="application/zip"))
    client.download_media = fake_download(b"zip")
    factory = make_patch(mocker, client)

    first = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID)
    second = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID)

    assert first["ok"] is True
    assert second["ok"] is True
    assert factory.call_count == 1


@pytest.mark.asyncio
async def test_download_file_reports_download_failures(mocker, channel_env: Path):
    client = make_client(mocker, file=make_message_file(name="backup.zip", mime_type="application/zip"))
    client.download_media = mocker.AsyncMock(side_effect=OSError("connection reset"))
    make_patch(mocker, client)

    result = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID)

    assert result["ok"] is False
    assert f"download failed for message {TEST_MESSAGE_ID}" in str(result["error"])


@pytest.mark.asyncio
async def test_download_file_reports_an_empty_download(mocker, channel_env: Path):
    client = make_client(mocker, file=make_message_file(name="backup.zip", mime_type="application/zip"))
    client.download_media = mocker.AsyncMock(return_value=None)
    make_patch(mocker, client)

    result = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID)

    assert result["ok"] is False
    assert f"Telegram returned no media for message {TEST_MESSAGE_ID}" in str(result["error"])


@pytest.mark.asyncio
async def test_download_file_reconnects_a_cached_client_that_dropped(mocker, channel_env: Path):
    client = make_client(mocker, file=make_message_file(name="backup.zip", mime_type="application/zip"))
    client.download_media = fake_download(b"zip")
    client.is_connected = lambda: False
    make_patch(mocker, client)

    first = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID)
    second = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID)

    assert first["ok"] is True
    assert second["ok"] is True
    client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_file_rejects_an_invalid_api_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel_env: Path
):
    monkeypatch.setenv(mtproto_module.API_ID_ENV, "not-a-number")

    result = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID)

    assert result["ok"] is False
    assert "invalid" in str(result["error"])
    assert mtproto_module.API_ID_ENV in str(result["error"])


@pytest.mark.asyncio
async def test_download_file_rejects_an_unknown_session(mocker, channel_env: Path):
    make_patch(mocker, make_client(mocker, file=make_message_file(name="x.zip", mime_type=None)))

    result = await mcp_channel.telegram_download_file(session_id="unknown", message_id=TEST_MESSAGE_ID)

    assert result["ok"] is False
    assert "unknown session_id" in str(result["error"])


@pytest.mark.asyncio
async def test_download_file_reports_a_lookup_failure(mocker, channel_env: Path):
    client = make_client(mocker, file=make_message_file(name="backup.zip", mime_type="application/zip"))
    client.get_messages = mocker.AsyncMock(side_effect=OSError("flood wait"))
    make_patch(mocker, client)

    result = await mcp_channel.telegram_download_file(message_id=TEST_MESSAGE_ID)

    assert result["ok"] is False
    assert f"MTProto lookup failed for message {TEST_MESSAGE_ID}" in str(result["error"])


def test_mtproto_credentials_availability_follows_the_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(mtproto_module.API_ID_ENV, raising=False)
    monkeypatch.delenv(mtproto_module.API_HASH_ENV, raising=False)
    assert mcp_channel.mtproto_credentials_available() is False

    monkeypatch.setenv(mtproto_module.API_ID_ENV, str(TEST_API_ID))
    assert mcp_channel.mtproto_credentials_available() is False

    monkeypatch.setenv(mtproto_module.API_HASH_ENV, TEST_API_HASH)
    assert mcp_channel.mtproto_credentials_available() is True


def test_channel_info_reports_large_file_support(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(mtproto_module.API_ID_ENV, raising=False)
    monkeypatch.delenv(mtproto_module.API_HASH_ENV, raising=False)
    info = mcp_channel.telegram_channel_info()
    assert info["supports_large_file_downloads"] is False
    assert info["supports_large_file_uploads"] is False

    monkeypatch.setenv(mtproto_module.API_ID_ENV, str(TEST_API_ID))
    monkeypatch.setenv(mtproto_module.API_HASH_ENV, TEST_API_HASH)
    info = mcp_channel.telegram_channel_info()
    assert info["supports_large_file_downloads"] is True
    assert info["supports_large_file_uploads"] is True


def test_default_download_name_prefers_the_announced_filename():
    assert downloads_module.default_download_name(7, "../evil.zip", None) == "evil.zip"
    assert downloads_module.default_download_name(7, None, "image/jpeg") == "message-7.jpg"
    assert downloads_module.default_download_name(7, None, None) == "message-7"
