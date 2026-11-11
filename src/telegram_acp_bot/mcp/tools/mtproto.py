"""The shared MTProto client behind the Telegram tools that outgrow the Bot API.

The Bot API caps downloads at 20 MB and uploads at 50 MB, while the same bot over MTProto
(Telethon) moves files up to 2 GB. This module owns the one client the
`telegram_download_file` and `telegram_send_attachment` tools share. See also
{py:mod}`telegram_acp_bot.mcp.tools.attachments` for the Bot API paths.
"""

from __future__ import annotations

import os

from telethon import TelegramClient
from telethon.sessions import StringSession

from telegram_acp_bot.mcp.state import API_HASH_ENV, API_ID_ENV

MISSING_API_CREDENTIALS_ERROR = (
    "MTProto credentials are not configured for this agent: set `telegram.api_id` and "
    "`telegram.api_hash` in the bot config file to enable transfers of files larger than "
    "the Bot API limits (20 MB down, 50 MB up)"
)

_clients: dict[str, TelegramClient] = {}


def mtproto_credentials_available() -> bool:
    """True when the MTProto credentials the tools need reach the MCP server env."""

    return bool(os.getenv(API_ID_ENV, "").strip() and os.getenv(API_HASH_ENV, "").strip())


async def mtproto_client(*, token: str) -> TelegramClient | str:
    """Return a started MTProto client for one bot token, kept alive across calls.

    A bot token signs in with no user interaction, and the in-memory session keeps the
    client reusable while the MCP server process lives.
    """

    api_id_raw = os.getenv(API_ID_ENV, "").strip()
    api_hash = os.getenv(API_HASH_ENV, "").strip()
    if not api_id_raw or not api_hash:
        return MISSING_API_CREDENTIALS_ERROR
    try:
        api_id = int(api_id_raw)
    except ValueError:
        return f"invalid {API_ID_ENV}: {api_id_raw!r}"

    existing = _clients.get(token)
    if existing is not None:
        if not existing.is_connected():
            await existing.connect()
        return existing

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        # Telethon types `start` as returning the client, but it returns a coroutine
        # whenever an event loop is running, so this await is sound at runtime.
        await client.start(bot_token=token)  # ty: ignore[invalid-await]
    except Exception as exc:  # noqa: BLE001
        await client.disconnect()
        return f"MTProto login failed: {exc}"
    _clients[token] = client
    return client
