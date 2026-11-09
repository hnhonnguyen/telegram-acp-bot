"""Internal MCP server exposed to agents running on DingTalk."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from acp_chat_bot.channel_policy import CHANNEL_POLICY_REMINDER
from acp_chat_bot.mcp.context import RequestContext, resolve_request_context
from acp_chat_bot.mcp.state import load_group_history
from acp_chat_bot.mcp.tools.dingtalk import register_dingtalk_tools
from acp_chat_bot.mcp.tools.scheduling import register_scheduling_tools

_RequestContext = RequestContext
_resolve_request_context = resolve_request_context
MAX_HISTORY_LIMIT = 200

__all__ = [
    "_RequestContext",
    "_resolve_request_context",
    "dingtalk_channel_info",
    "dingtalk_list_messages",
    "load_group_history",
    "main",
    "mcp",
]

mcp = FastMCP(
    name="dingtalk-channel",
    instructions=(
        "Channel helper tools for DingTalk clients. Use these tools when you need channel-specific behavior. "
        "For reminders, recurring schedules, or deferred follow-ups, always use the scheduling tools: "
        "`schedule_task`, `list_scheduled_tasks`, `update_scheduled_task`, and `cancel_scheduled_task`. "
        "Never use a built-in or engine-native cron or scheduler: those are session-scoped and disappear "
        "when the ACP session resets. Cron expressions use UTC.\n\n" + CHANNEL_POLICY_REMINDER
    ),
)


@mcp.tool(
    name="dingtalk_channel_info",
    description="Return capabilities currently exposed by the DingTalk channel MCP server.",
)
def dingtalk_channel_info() -> dict[str, object]:
    return {
        "supports_attachment_delivery": True,
        "supports_media_fetching": False,
        "supports_message_reactions": False,
        "supports_scheduled_tasks": True,
        "supports_recurring_scheduled_tasks": True,
        "supports_scheduled_task_management": True,
        "scheduled_task_timezone": "UTC",
        "supports_group_history": True,
        "group_history_scope": "messages addressed to the bot",
        "status": "active",
    }


@mcp.tool(
    name="dingtalk_list_messages",
    description=(
        "List recent group messages that DingTalk delivered to this bot. "
        "DingTalk only delivers messages that mention the bot, so this is not a full chat log."
    ),
)
def dingtalk_list_messages(
    limit: int = 20,
    since_message_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, object]:
    if limit <= 0 or limit > MAX_HISTORY_LIMIT:
        return {"error": f"limit must be between 1 and {MAX_HISTORY_LIMIT}"}
    context = resolve_request_context(session_id=session_id)
    if isinstance(context, str):
        return {"error": context}
    history = load_group_history(context.state_file, chat_id=context.chat_id)
    if since_message_id is not None:
        ids = [str(entry.get("message_id")) for entry in history]
        if str(since_message_id) in ids:
            history = history[ids.index(str(since_message_id)) + 1 :]
    return {"messages": history[-limit:]}


register_scheduling_tools(mcp)
register_dingtalk_tools(mcp)


def main() -> None:
    """Run the MCP server over stdio."""

    mcp.run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
