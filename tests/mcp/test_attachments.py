"""Tests for MCP attachment delivery tools."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types

from telegram_acp_bot.mcp.tools import attachments as attachments_module
from telegram_acp_bot.mcp.tools import mtproto as mtproto_module
from tests.mcp.support import (
    AGENT_NAME_ENV,
    STATE_FILE_ENV,
    TOKEN_ENV,
    mcp_channel,
    save_last_session_id,
    save_session_chat_map,
)


@pytest.mark.asyncio
async def test_send_attachment_from_path_as_photo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker):
    state_file = tmp_path / "state.json"
    image_file = tmp_path / "webcam.jpg"
    image_file.write_bytes(b"jpeg")
    save_session_chat_map(state_file, {"s1": 123})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(mcp_channel.ALLOW_PATH_ENV, "1")
    bot = mocker.AsyncMock()
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=bot)

    result = await mcp_channel.telegram_send_attachment(path=str(image_file))

    assert result["ok"] is True
    assert result["delivered_as"] == "photo"
    assert result["session_id"] == "s1"
    bot.send_photo.assert_awaited_once()
    bot.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_send_attachment_from_base64_as_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123})
    save_last_session_id(state_file, "s1")
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    bot = mocker.AsyncMock()
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=bot)

    result = await mcp_channel.telegram_send_attachment(
        session_id="s1",
        data_base64=base64.b64encode(b"payload").decode("ascii"),
        name="artifact.bin",
    )

    assert result["ok"] is True
    assert result["delivered_as"] == "document"
    bot.send_document.assert_awaited_once()
    bot.send_photo.assert_not_called()


@pytest.mark.asyncio
async def test_send_attachment_infers_last_active_session_when_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker
):
    expected_chat_id = 456
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123, "s2": expected_chat_id})
    save_last_session_id(state_file, "s2")
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    bot = mocker.AsyncMock()
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=bot)

    result = await mcp_channel.telegram_send_attachment(
        data_base64=base64.b64encode(b"payload").decode("ascii"),
        name="artifact.bin",
    )

    assert result["ok"] is True
    assert result["session_id"] == "s2"
    assert result["chat_id"] == expected_chat_id
    bot.send_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_attachment_rejects_missing_session_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(mcp_channel.ALLOW_PATH_ENV, "1")

    result = await mcp_channel.telegram_send_attachment(session_id="unknown", path=str(tmp_path / "x.jpg"))
    assert result["ok"] is False
    assert "unknown session_id" in cast(str, result["error"])


@pytest.mark.asyncio
async def test_send_attachment_rejects_invalid_base64_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))

    result = await mcp_channel.telegram_send_attachment(session_id="s1", data_base64="***not-base64***")

    assert result["ok"] is False
    assert result["error"] == "invalid base64 payload"


def test_load_attachment_bytes_rejects_missing_path(tmp_path: Path):
    result = mcp_channel._load_attachment_bytes(path=str(tmp_path / "missing.bin"), data_base64=None, name=None)
    assert isinstance(result, str)
    assert result.startswith("file not found:")


def test_load_attachment_bytes_requires_exactly_one_source():
    assert (
        mcp_channel._load_attachment_bytes(path=None, data_base64=None, name=None)
        == "provide exactly one of `path` or `data_base64`"
    )
    assert (
        mcp_channel._load_attachment_bytes(path="artifact.bin", data_base64="cGF5bG9hZA==", name=None)
        == "provide exactly one of `path` or `data_base64`"
    )


@pytest.mark.asyncio
async def test_send_attachment_rejects_path_when_not_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.delenv(mcp_channel.ALLOW_PATH_ENV, raising=False)

    result = await mcp_channel.telegram_send_attachment(session_id="s1", path=str(tmp_path / "x.jpg"))
    assert result["ok"] is False
    assert "`path` input is disabled by default" in cast(str, result["error"])


@pytest.mark.asyncio
async def test_send_attachment_uses_overridden_name_for_mime_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker
):
    state_file = tmp_path / "state.json"
    binary_file = tmp_path / "payload.bin"
    binary_file.write_bytes(b"jpeg")
    save_session_chat_map(state_file, {"s1": 123})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(mcp_channel.ALLOW_PATH_ENV, "1")
    bot = mocker.AsyncMock()
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=bot)

    result = await mcp_channel.telegram_send_attachment(path=str(binary_file), name="forced.jpg")

    assert result["ok"] is True
    assert result["delivered_as"] == "photo"
    bot.send_photo.assert_awaited_once()


@pytest.fixture
def channel_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    return state_file


def make_download_bot(mocker, payload: bytes):
    bot = mocker.AsyncMock()
    bot.get_file.return_value.download_as_bytearray = mocker.AsyncMock(return_value=bytearray(payload))
    return bot


@pytest.mark.asyncio
async def test_fetch_attachment_writes_into_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker, channel_env: Path
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    bot = make_download_bot(mocker, b"jpeg-bytes")
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=bot)

    result = await mcp_channel.telegram_fetch_attachment(session_id="s1", file_id="f1", mime_type="image/jpeg")

    assert result["ok"] is True
    path = Path(cast(str, result["path"]))
    assert path.parent == workspace / mcp_channel.DEFAULT_MEDIA_DIRNAME
    assert path.suffix == ".jpg"
    assert path.read_bytes() == b"jpeg-bytes"
    assert result["mime_type"] == "image/jpeg"
    assert result["bytes"] == len(b"jpeg-bytes")


@pytest.mark.asyncio
async def test_fetch_attachment_namespaces_media_by_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker, channel_env: Path
):
    """A named agent writes under .telegram-media/<agent>, so two agents never share a file."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv(AGENT_NAME_ENV, "qoder")
    bot = make_download_bot(mocker, b"jpeg-bytes")
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=bot)

    result = await mcp_channel.telegram_fetch_attachment(session_id="s1", file_id="f1", mime_type="image/jpeg")

    assert result["ok"] is True
    path = Path(cast(str, result["path"]))
    assert path.parent == workspace / mcp_channel.DEFAULT_MEDIA_DIRNAME / "qoder"


@pytest.mark.asyncio
async def test_fetch_attachment_honours_a_relative_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker, channel_env: Path
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    bot = make_download_bot(mocker, b"%PDF")
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=bot)

    result = await mcp_channel.telegram_fetch_attachment(file_id="f1", dest="invoices/hoa-don.pdf")

    assert result["ok"] is True
    assert Path(cast(str, result["path"])) == workspace / "invoices" / "hoa-don.pdf"
    assert result["mime_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_fetch_attachment_rejects_dest_outside_the_working_directory(mocker, channel_env: Path):
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=make_download_bot(mocker, b"x"))

    result = await mcp_channel.telegram_fetch_attachment(file_id="f1", dest="../../escape.png")

    assert result["ok"] is False
    assert result["error"] == mcp_channel.MEDIA_OUTSIDE_WORKSPACE_ERROR


@pytest.mark.asyncio
async def test_fetch_attachment_rejects_dest_that_is_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker, channel_env: Path
):
    workspace = tmp_path / "ws"
    (workspace / "dir").mkdir(parents=True)
    monkeypatch.chdir(workspace)
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=make_download_bot(mocker, b"x"))

    result = await mcp_channel.telegram_fetch_attachment(file_id="f1", dest="dir")

    assert result["ok"] is False
    assert "`dest` is a directory" in cast(str, result["error"])


@pytest.mark.asyncio
async def test_fetch_attachment_reports_download_failures(mocker, channel_env: Path):
    bot = mocker.AsyncMock()
    bot.get_file.side_effect = OSError("telegram is unreachable")
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=bot)

    result = await mcp_channel.telegram_fetch_attachment(file_id="f1")

    assert result["ok"] is False
    assert "download failed for file_id `f1`" in cast(str, result["error"])


@pytest.mark.asyncio
async def test_fetch_attachment_reports_an_empty_payload(mocker, channel_env: Path):
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=make_download_bot(mocker, b""))

    result = await mcp_channel.telegram_fetch_attachment(file_id="f1")

    assert result["ok"] is False
    assert "empty payload" in cast(str, result["error"])


@pytest.mark.asyncio
async def test_fetch_attachment_rejects_an_unknown_session(mocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    save_session_chat_map(tmp_path / "state.json", {"s1": 123})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(tmp_path / "state.json"))
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=make_download_bot(mocker, b"x"))

    result = await mcp_channel.telegram_fetch_attachment(session_id="unknown", file_id="f1")

    assert result["ok"] is False
    assert "unknown session_id" in cast(str, result["error"])


def test_default_media_name_uses_the_digest_and_a_known_extension():
    named = mcp_channel.default_media_name("f1", "image/png")
    unnamed = mcp_channel.default_media_name("f1", None)
    vague = mcp_channel.default_media_name("f1", "application/x-unknown-type")

    assert named.endswith(".png")
    assert unnamed == named[: -len(".png")]
    assert vague == unnamed
    assert "f1" not in named


def test_route_media_kind_maps_mime_to_delivery():
    assert attachments_module.route_media_kind("image/png") == "photo"
    assert attachments_module.route_media_kind("video/mp4") == "video"
    assert attachments_module.route_media_kind("audio/mpeg") == "audio"
    assert attachments_module.route_media_kind("application/pdf") == "document"


def test_build_send_specs_rejects_mixing_top_level_and_attachments():
    result = attachments_module.build_send_specs(
        path="a.jpg",
        data_base64=None,
        name=None,
        mime_type=None,
        attachments=[{"path": "b.jpg"}],
    )
    assert result == attachments_module.MIXED_TOP_LEVEL_AND_ATTACHMENTS_ERROR


def test_build_send_specs_rejects_empty_attachments_list():
    assert (
        attachments_module.build_send_specs(path=None, data_base64=None, name=None, mime_type=None, attachments=[])
        == attachments_module.EMPTY_ATTACHMENTS_ERROR
    )


def test_build_send_specs_wraps_a_single_source():
    specs = attachments_module.build_send_specs(
        path="a.jpg", data_base64=None, name="x", mime_type="image/jpeg", attachments=None
    )
    assert specs == [{"path": "a.jpg", "name": "x", "mime_type": "image/jpeg"}]


@pytest.mark.asyncio
async def test_send_attachment_routes_video_natively(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker):
    state_file = tmp_path / "state.json"
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"mp4")
    save_session_chat_map(state_file, {"s1": 123})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(mcp_channel.ALLOW_PATH_ENV, "1")
    bot = mocker.AsyncMock()
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=bot)

    result = await mcp_channel.telegram_send_attachment(path=str(video_file))

    assert result["ok"] is True
    assert result["delivered_as"] == "video"
    bot.send_video.assert_awaited_once()
    bot.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_send_attachment_routes_audio_natively(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    bot = mocker.AsyncMock()
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=bot)

    result = await mcp_channel.telegram_send_attachment(
        data_base64=base64.b64encode(b"id3").decode("ascii"), name="song.mp3"
    )

    assert result["ok"] is True
    assert result["delivered_as"] == "audio"
    bot.send_audio.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_attachment_sends_multiple_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker):
    state_file = tmp_path / "state.json"
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"jpeg")
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF")
    save_session_chat_map(state_file, {"s1": 123})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(mcp_channel.ALLOW_PATH_ENV, "1")
    bot = mocker.AsyncMock()
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=bot)

    attachments = [{"path": str(photo)}, {"path": str(doc)}]
    result = await mcp_channel.telegram_send_attachment(attachments=attachments)

    assert result["ok"] is True
    assert result["count"] == len(attachments)
    assert "delivered_as" not in result
    delivered = cast(list[dict[str, object]], result["delivered"])
    kinds = [item["delivered_as"] for item in delivered]
    assert kinds == ["photo", "document"]
    bot.send_photo.assert_awaited_once()
    bot.send_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_attachment_requires_a_source():
    result = await mcp_channel.telegram_send_attachment()
    assert result["ok"] is False
    assert result["error"] == attachments_module.EXACTLY_ONE_ATTACHMENT_SOURCE_ERROR


@pytest.mark.asyncio
async def test_send_attachment_reports_path_disabled_for_any_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_file = tmp_path / "state.json"
    save_session_chat_map(state_file, {"s1": 123})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.delenv(mcp_channel.ALLOW_PATH_ENV, raising=False)

    result = await mcp_channel.telegram_send_attachment(attachments=[{"path": str(tmp_path / "a.jpg")}])

    assert result["ok"] is False
    assert "`path` input is disabled by default" in cast(str, result["error"])


TINY_LIMIT = 10


@pytest.fixture
def mtproto_env(channel_env: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(attachments_module, "BOT_API_UPLOAD_LIMIT_BYTES", TINY_LIMIT)
    monkeypatch.setenv(mtproto_module.API_ID_ENV, "12345")
    monkeypatch.setenv(mtproto_module.API_HASH_ENV, "hash")
    mtproto_module._clients.clear()
    yield channel_env
    mtproto_module._clients.clear()


def make_mtproto_client(mocker):
    client = mocker.AsyncMock()
    client.is_connected.return_value = True
    mocker.patch("telegram_acp_bot.mcp.tools.mtproto.TelegramClient", mocker.MagicMock(return_value=client))
    return client


def write_oversized(tmp_path: Path, name: str) -> Path:
    source = tmp_path / name
    source.write_bytes(b"x" * (TINY_LIMIT + 1))
    return source


@pytest.mark.asyncio
async def test_send_attachment_streams_an_oversized_path_over_mtproto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker, mtproto_env: Path
):
    video = write_oversized(tmp_path, "clip.mp4")
    monkeypatch.setenv(mcp_channel.ALLOW_PATH_ENV, "1")
    bot = mocker.AsyncMock()
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=bot)
    client = make_mtproto_client(mocker)

    result = await mcp_channel.telegram_send_attachment(path=str(video))

    assert result["ok"] is True
    assert result["delivered_as"] == "video"
    assert cast("list[dict[str, object]]", result["delivered"])[0]["bytes"] == TINY_LIMIT + 1
    client.send_file.assert_awaited_once()
    assert client.send_file.await_args.args[1] == str(video)
    assert client.send_file.await_args.kwargs["supports_streaming"] is True
    bot.send_video.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_attachment_oversized_without_credentials_names_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker, mtproto_env: Path
):
    monkeypatch.delenv(mtproto_module.API_ID_ENV, raising=False)
    monkeypatch.delenv(mtproto_module.API_HASH_ENV, raising=False)
    video = write_oversized(tmp_path, "clip.mp4")
    monkeypatch.setenv(mcp_channel.ALLOW_PATH_ENV, "1")
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=mocker.AsyncMock())

    result = await mcp_channel.telegram_send_attachment(path=str(video))

    assert result["ok"] is False
    error = cast(str, result["error"])
    assert "uploads at most 50 MB" in error
    assert "`telegram.api_id`" in error and "`telegram.api_hash`" in error


@pytest.mark.asyncio
async def test_send_attachment_oversized_base64_keeps_the_name_and_routes_natively(
    tmp_path: Path, mocker, mtproto_env: Path
):
    client = make_mtproto_client(mocker)
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=mocker.AsyncMock())

    result = await mcp_channel.telegram_send_attachment(
        data_base64=base64.b64encode(b"y" * (TINY_LIMIT + 1)).decode("ascii"), name="song.mp3"
    )

    assert result["ok"] is True
    assert result["delivered_as"] == "audio"
    client.send_file.assert_awaited_once()
    sent = client.send_file.await_args
    assert sent.args[1].name == "song.mp3"
    assert sent.kwargs["mime_type"] == "audio/mpeg"
    assert any(isinstance(attribute, types.DocumentAttributeAudio) for attribute in sent.kwargs["attributes"])


@pytest.mark.asyncio
async def test_send_attachment_oversized_path_honours_a_custom_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker, mtproto_env: Path
):
    archive = write_oversized(tmp_path, "archive.tar")
    monkeypatch.setenv(mcp_channel.ALLOW_PATH_ENV, "1")
    client = make_mtproto_client(mocker)
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=mocker.AsyncMock())

    result = await mcp_channel.telegram_send_attachment(path=str(archive), name="release.tar")

    assert result["ok"] is True
    assert result["delivered_as"] == "document"
    sent = client.send_file.await_args
    assert sent.kwargs["force_document"] is True
    assert any(
        isinstance(attribute, types.DocumentAttributeFilename) and attribute.file_name == "release.tar"
        for attribute in sent.kwargs["attributes"]
    )


@pytest.mark.asyncio
async def test_send_attachment_routes_an_oversized_photo_without_extra_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker, mtproto_env: Path
):
    photo = write_oversized(tmp_path, "panorama.jpg")
    monkeypatch.setenv(mcp_channel.ALLOW_PATH_ENV, "1")
    client = make_mtproto_client(mocker)
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=mocker.AsyncMock())

    result = await mcp_channel.telegram_send_attachment(path=str(photo))

    assert result["ok"] is True
    assert result["delivered_as"] == "photo"
    assert client.send_file.await_args.kwargs == {}


@pytest.mark.asyncio
async def test_send_attachment_reports_an_mtproto_upload_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker, mtproto_env: Path
):
    video = write_oversized(tmp_path, "clip.mp4")
    monkeypatch.setenv(mcp_channel.ALLOW_PATH_ENV, "1")
    client = make_mtproto_client(mocker)
    client.send_file = mocker.AsyncMock(side_effect=OSError("flood wait"))
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=mocker.AsyncMock())

    result = await mcp_channel.telegram_send_attachment(path=str(video))

    assert result["ok"] is False
    assert "MTProto upload failed for `clip.mp4`" in cast(str, result["error"])


@pytest.mark.asyncio
async def test_send_attachment_small_files_keep_the_bot_api_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker, mtproto_env: Path
):
    image = tmp_path / "webcam.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.setenv(mcp_channel.ALLOW_PATH_ENV, "1")
    bot = mocker.AsyncMock()
    mocker.patch("telegram_acp_bot.mcp.tools.attachments.Bot", return_value=bot)
    client = make_mtproto_client(mocker)

    result = await mcp_channel.telegram_send_attachment(path=str(image))

    assert result["ok"] is True
    assert result["delivered_as"] == "photo"
    bot.send_photo.assert_awaited_once()
    client.send_file.assert_not_awaited()


def test_prepare_attachment_never_reads_an_oversized_path_into_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(attachments_module, "BOT_API_UPLOAD_LIMIT_BYTES", TINY_LIMIT)
    video = write_oversized(tmp_path, "clip.mp4")

    prepared = attachments_module.prepare_attachment({"path": str(video)})

    assert isinstance(prepared, attachments_module.PreparedAttachment)
    assert prepared.payload is None
    assert prepared.file == str(video)
    assert prepared.size == TINY_LIMIT + 1
    assert prepared.resolved_mime == "video/mp4"


def test_prepare_attachment_reports_a_missing_path(tmp_path: Path):
    prepared = attachments_module.prepare_attachment({"path": str(tmp_path / "missing.bin")})

    assert prepared == f"file not found: {tmp_path / 'missing.bin'}"


def test_oversized_upload_error_names_the_size():
    error = attachments_module.oversized_upload_error("clip.mp4", 3 * 1024 * 1024 * 1024)

    assert "3072 MB" in error
    assert "`telegram.api_id`" in error


def test_prepare_attachment_surfaces_a_loading_failure(tmp_path: Path, mocker):
    image = tmp_path / "webcam.jpg"
    image.write_bytes(b"jpeg")
    mocker.patch.object(attachments_module, "load_attachment_bytes", return_value="unreadable file")

    prepared = attachments_module.prepare_attachment({"path": str(image)})

    assert prepared == "unreadable file"
