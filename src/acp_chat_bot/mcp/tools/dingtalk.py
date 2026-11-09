"""Attachment-delivery tool for the internal DingTalk MCP server."""

from __future__ import annotations

import asyncio
import base64
import binascii
import mimetypes
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from acp_chat_bot.channels.dingtalk.api import DingTalkAPI, DingTalkAPIError
from acp_chat_bot.core.ids import ChatId
from acp_chat_bot.mcp.context import resolve_request_context
from acp_chat_bot.mcp.state import (
    DINGTALK_CLIENT_ID_ENV,
    DINGTALK_CLIENT_SECRET_ENV,
    DINGTALK_ROBOT_CODE_ENV,
    load_conversation_target,
)
from acp_chat_bot.mcp.tools.attachments import ALLOW_PATH_ENV

EXACTLY_ONE_ATTACHMENT_SOURCE_ERROR = "provide exactly one of `path` or `data_base64`"
DEFAULT_MIME_TYPE = "application/octet-stream"


def register_dingtalk_tools(mcp: FastMCP) -> None:
    """Register DingTalk-specific tools on the provided MCP server."""

    mcp.tool(
        name="dingtalk_send_attachment",
        description=(
            "Send one image or file to the current DingTalk chat. "
            "If session_id is omitted, the server auto-resolves it when possible. "
            "Use `path` for a file on disk inside the agent working directory, or `data_base64` "
            "for in-memory bytes; provide exactly one. Images (`image/*`) are sent as pictures and "
            "everything else as files. Set `name` when sending base64 so the type can be inferred."
        ),
    )(dingtalk_send_attachment)


async def dingtalk_send_attachment(  # noqa: PLR0911
    path: str | None = None,
    data_base64: str | None = None,
    name: str | None = None,
    mime_type: str | None = None,
    session_id: str | None = None,
) -> dict[str, object]:
    if (path is None) == (data_base64 is None):
        return _fail(EXACTLY_ONE_ATTACHMENT_SOURCE_ERROR)

    context = resolve_request_context(session_id=session_id)
    if isinstance(context, str):
        return _fail(context)

    client_id = os.getenv(DINGTALK_CLIENT_ID_ENV, "").strip()
    client_secret = os.getenv(DINGTALK_CLIENT_SECRET_ENV, "").strip()
    if not client_id or not client_secret:
        return _fail(f"missing {DINGTALK_CLIENT_ID_ENV} or {DINGTALK_CLIENT_SECRET_ENV}")
    robot_code = os.getenv(DINGTALK_ROBOT_CODE_ENV, "").strip() or client_id

    target = load_conversation_target(context.state_file, context.chat_id)
    if target is None:
        return _fail(f"no DingTalk conversation target stored for chat {context.chat_id!r}")

    source = await _read_source(path=path, data_base64=data_base64, name=name)
    if isinstance(source, str):
        return _fail(source)
    raw, filename = source
    resolved_mime = mime_type or mimetypes.guess_type(filename)[0] or DEFAULT_MIME_TYPE
    kind = "image" if resolved_mime.startswith("image/") else "file"

    api = DingTalkAPI(
        client_id=client_id,
        client_secret=client_secret,
        robot_code=robot_code,
    )
    try:
        media_id = await api.upload_media(
            raw,
            filename=filename,
            mime_type=resolved_mime,
            media_type=kind,
        )
        await _deliver(api, target=target, chat_id=context.chat_id, kind=kind, media_id=media_id, filename=filename)
    except DingTalkAPIError as exc:
        return _fail(str(exc))
    finally:
        await api.close()

    return {
        "ok": True,
        "name": filename,
        "mime_type": resolved_mime,
        "delivered_as": kind,
        "bytes": len(raw),
    }


async def _read_source(
    *,
    path: str | None,
    data_base64: str | None,
    name: str | None,
) -> tuple[bytes, str] | str:
    if path is not None:
        resolved = _resolve_path(path)
        if isinstance(resolved, str):
            return resolved
        try:
            return await asyncio.to_thread(resolved.read_bytes), name or resolved.name
        except OSError as exc:
            return f"could not read `{resolved}`: {exc}"
    assert data_base64 is not None
    try:
        return base64.b64decode(data_base64, validate=True), name or "attachment"
    except (binascii.Error, ValueError):
        return "`data_base64` is not valid base64"


def _resolve_path(path: str) -> Path | str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if os.getenv(ALLOW_PATH_ENV) != "1" and not candidate.is_relative_to(Path.cwd().resolve()):
        return f"`path` must stay inside the agent working directory ({Path.cwd()})"
    if not candidate.is_file():
        return f"file not found: {candidate}"
    return candidate


async def _deliver(  # noqa: PLR0913
    api: DingTalkAPI,
    *,
    target: dict[str, object],
    chat_id: ChatId,
    kind: str,
    media_id: str,
    filename: str,
) -> None:
    conversation_id = str(chat_id)
    conversation_type = str(target.get("chat_type") or "private")
    user_id = str(target.get("user_id") or "")
    if kind == "image":
        await api.send_image(
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            user_id=user_id,
            media_id=media_id,
        )
        return
    await api.send_file(
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        user_id=user_id,
        media_id=media_id,
        filename=filename,
    )


def _fail(error: str) -> dict[str, object]:
    return {"ok": False, "error": error}
