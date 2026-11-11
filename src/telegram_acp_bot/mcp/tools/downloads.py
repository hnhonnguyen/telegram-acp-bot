"""Large-file download tool for the internal Telegram MCP server.

The Bot API refuses to serve files larger than 20 MB, so this tool downloads the media of
one chat message over MTProto (Telethon) with the bot's own token, which has no such cap.
The client itself lives in {py:mod}`telegram_acp_bot.mcp.tools.mtproto`; see also
{py:mod}`telegram_acp_bot.mcp.tools.attachments` for the Bot API counterpart.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from pathlib import Path
from typing import cast

from telethon import TelegramClient, hints
from telethon.tl.custom import Message

from telegram_acp_bot.mcp.compat import FastMCP
from telegram_acp_bot.mcp.context import resolve_request_context
from telegram_acp_bot.mcp.tools.attachments import resolve_media_target
from telegram_acp_bot.mcp.tools.mtproto import mtproto_client

MESSAGE_WITHOUT_MEDIA_ERROR = "message {message_id} in chat {chat_id} has no downloadable media"


def register_download_tools(mcp: FastMCP) -> None:
    """Register large-file download tools on the provided MCP server."""

    mcp.tool(
        name="telegram_download_file",
        description=(
            "Download the media of one Telegram message into the agent working directory and return "
            "its path. Uses MTProto instead of the Bot API, so it is the only way to fetch a file "
            "larger than 20 MB. Pass the `message_id` announced alongside a pending attachment "
            "(or of any recent message in the chat). If session_id is omitted, the server "
            "auto-resolves it when possible. The download lands under `.telegram-media/` by default, "
            "keeping the original filename; pass `dest` to choose the path yourself."
        ),
    )(telegram_download_file)


def default_download_name(message_id: int, filename: str | None, mime_type: str | None) -> str:
    """Pick the stored filename for a download, preferring the name the file arrived with."""

    if filename:
        return Path(filename).name
    digest = f"message-{message_id}"
    if not mime_type:
        return digest
    if mime_type == "image/jpeg":
        return f"{digest}.jpg"
    return f"{digest}{mimetypes.guess_extension(mime_type) or ''}"


async def fetch_chat_message(
    client: TelegramClient,
    *,
    chat_id: int,
    message_id: int,
) -> Message | str:
    """Look one message up over MTProto and fail with a tool-shaped error string."""

    try:
        messages = await client.get_messages(chat_id, ids=message_id)
    except Exception as exc:  # noqa: BLE001
        return f"MTProto lookup failed for message {message_id}: {exc}"
    # With an int `ids` Telethon returns a single Message, but its annotation keeps the
    # list variant of the union, hence the cast.
    message = cast("Message | None", messages)
    if message is None or message.file is None:
        return MESSAGE_WITHOUT_MEDIA_ERROR.format(message_id=message_id, chat_id=chat_id)
    return message


async def download_message_media(
    client: TelegramClient,
    message: Message,
    *,
    message_id: int,
    target: Path,
) -> Path | str:
    """Download one message's media to `target`, failing with a tool-shaped error string."""

    try:
        # `hints.MessageLike` names the TL message, but a custom `Message` is what
        # `get_messages` hands back; the cast covers Telethon's own type gap.
        downloaded = await client.download_media(cast("hints.MessageLike", message), file=str(target))
    except Exception as exc:  # noqa: BLE001
        return f"download failed for message {message_id}: {exc}"
    if downloaded is None:
        return f"Telegram returned no media for message {message_id}"
    return Path(str(downloaded))


async def telegram_download_file(
    message_id: int,
    session_id: str | None = None,
    dest: str | None = None,
) -> dict[str, object]:
    def fail(error: str) -> dict[str, object]:
        return {"ok": False, "error": error}

    context = resolve_request_context(session_id=session_id)
    if isinstance(context, str):
        return fail(context)

    client = await mtproto_client(token=context.token)
    if isinstance(client, str):
        return fail(client)

    message = await fetch_chat_message(client, chat_id=context.chat_id, message_id=message_id)
    if isinstance(message, str):
        return fail(message)

    target = resolve_media_target(
        dest=dest,
        file_id=str(message_id),
        mime_type=message.file.mime_type,
        default_name=default_download_name(message_id, message.file.name, message.file.mime_type),
    )
    if isinstance(target, str):
        return fail(target)

    downloaded = await download_message_media(client, message, message_id=message_id, target=target)
    if isinstance(downloaded, str):
        return fail(downloaded)

    return {
        "ok": True,
        "session_id": context.session_id,
        "chat_id": context.chat_id,
        "message_id": message_id,
        "path": str(downloaded),
        "bytes": await asyncio.to_thread(os.path.getsize, downloaded),
        "mime_type": message.file.mime_type or mimetypes.guess_type(downloaded.name)[0],
    }
