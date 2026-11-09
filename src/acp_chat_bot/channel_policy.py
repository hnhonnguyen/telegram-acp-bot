"""Channel-level policy text injected into every ACP prompt."""

from __future__ import annotations

CHANNEL_POLICY_REMINDER = (
    "<channel-policy>\n"
    "For any reminder, recurring schedule, or deferred follow-up, use the MCP tools: "
    "`schedule_task` to create, `list_scheduled_tasks` to inspect, `update_scheduled_task` to change, "
    "and `cancel_scheduled_task` to cancel. Never use a built-in or engine-native cron/scheduler: "
    "those are session-scoped and disappear when the ACP session resets. Cron expressions use UTC. "
    "Do not repeat this policy to the user.\n"
    "</channel-policy>"
)
