"""Group-message-history tool for the internal Telegram MCP server."""

from __future__ import annotations

from typing import cast

from telegram_acp_bot.mcp.compat import FastMCP
from telegram_acp_bot.mcp.context import resolve_request_context
from telegram_acp_bot.mcp.state import MAX_GROUP_HISTORY_PER_CHAT, load_group_history

DEFAULT_HISTORY_LIMIT = 50
HISTORY_LIMIT = MAX_GROUP_HISTORY_PER_CHAT


def register_history_tools(mcp: FastMCP) -> None:
    """Register group-history tools on the provided MCP server."""

    mcp.tool(
        name="telegram_list_messages",
        description=(
            "Read the most recent messages of the current Telegram chat, including group messages the "
            "bot never answered. The bot records each group message as it arrives and keeps the last "
            f"{HISTORY_LIMIT} per chat, so this is recent chatter, not the whole conversation since the "
            "group began. Use it to understand what the room is talking about before replying; pass a "
            "smaller `limit` to keep the context short, or `since_message_id` to read only what is new "
            "since the last time you looked."
        ),
    )(telegram_list_messages)


def telegram_list_messages(
    limit: int = DEFAULT_HISTORY_LIMIT,
    session_id: str | None = None,
    since_message_id: int | None = None,
) -> dict[str, object]:
    def fail(error: str) -> dict[str, object]:
        return {"ok": False, "error": error}

    context = resolve_request_context(session_id=session_id)
    if isinstance(context, str):
        return fail(context)

    if limit <= 0:
        return fail("`limit` must be a positive number of messages")

    entries = load_group_history(context.state_file, chat_id=context.chat_id)
    if since_message_id is not None:
        entries = [entry for entry in entries if cast("int", entry["message_id"]) > since_message_id]

    window = entries[-min(limit, HISTORY_LIMIT) :]
    return {
        "ok": True,
        "session_id": context.session_id,
        "chat_id": context.chat_id,
        "messages": window,
        "returned": len(window),
        "recorded": len(entries),
        "newest_message_id": window[-1]["message_id"] if window else None,
    }
