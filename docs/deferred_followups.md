# Deferred Follow-ups

Deferred follow-ups let the agent promise something now and do it later, without keeping the original ACP prompt alive. The feature covers one-shot and recurring reminders: “check this again in 10 minutes”, “remind me every day at 9 AM UTC”, or “send me a short follow-up in 30 seconds”.

The user-facing idea is that scheduling feels conversational, not infrastructural. The user asks for something later. The agent answers normally in the thread. When the scheduled time arrives, the follow-up appears as a reply to that conversation.

```text
User: check this PR again in 10 minutes
Bot: Okay. I’ll check it again in 10 minutes.

... later ...

Bot (replying to that confirmation): There are 3 new comments on the PR.
```

## What Exists Today

Tasks are persisted in the SQLite database configured by {term}`ACP_SCHEDULED_TASKS_DB`, and an in-process scheduler claims due work inside the bot runtime. Because the database is durable, a task survives the ACP session that created it, a bot restart, and a switch to a different ACP engine.

There are two execution modes:

`notify` sends a stored text later. It does not call the agent, so it is the most durable choice for plain reminders.

`prompt_agent` re-enters the agent flow later with a stored prompt and sends the answer back to Telegram. It still needs a live ACP session when the task fires.

The agent manages tasks through four MCP tools:

- `schedule_task` creates a one-shot or recurring task.
- `list_scheduled_tasks` lists tasks for the current chat.
- `update_scheduled_task` changes a pending task.
- `cancel_scheduled_task` cancels a pending task.

The bot also exposes a human-facing management surface: `/scheduled` lists pending and running tasks with inline cancel buttons, and `/schedule` creates a one-shot `prompt_agent` task directly from the chat.

(deferred-followups-ux)=
## How The UX Works

When the agent calls `schedule_task`, the MCP tool persists the task and returns a summary. The tool itself does not send a Telegram message. The agent uses that result to answer the user in normal language. Once that answer reaches Telegram, the bot records it as the task's anchor message.

That design keeps the chat clean: the visible message is the conversational confirmation from the agent, not a second technical artifact from the transport layer.

```{mermaid}
:align: center
sequenceDiagram
    participant U as Telegram user
    participant B as Bot runtime
    participant A as ACP agent
    participant M as MCP scheduling tools
    participant S as Scheduler

    U->>B: "Remind me every day at 9 UTC"
    B->>A: ACP prompt plus channel policy
    A->>M: schedule_task(cron="0 9 * * *")
    M-->>A: task id and routing
    A-->>B: "I'll remind you every day at 9 UTC."
    B-->>U: scheduling confirmation
    B->>B: bind task to that message id
    S->>B: task becomes due
    B-->>U: reply to the confirmation
```

Every reply also carries the original routing: chat, source message, requester, and chat type. If the agent's confirmation never arrives before the task is due, delivery falls back to the user's source message instead of silently failing.

## Relative, Absolute, And Cron Inputs

For short delays, relative time is the preferred path. Requests such as “in 30 seconds” or “in 10 minutes” become `delay_seconds`, `delay_minutes`, or `delay_hours`. That avoids forcing the agent to calculate a wall-clock timestamp.

Absolute scheduling uses `run_at`, an ISO timestamp with an explicit timezone offset, for example `2026-04-11T09:00:00+00:00`.

Recurring scheduling uses `cron`. Cron expressions are evaluated in UTC. The five-field form is `minute hour day-of-month month day-of-week`; a sixth trailing field adds seconds. Examples:

- `0 9 * * *` — every day at 09:00 UTC.
- `*/15 * * * *` — every 15 minutes.
- `0 9 * * 1` — every Monday at 09:00 UTC.

The first occurrence is strictly after the current time. After each run, the scheduler computes the next occurrence strictly after the moment the run finishes, so missed slots during downtime are skipped instead of replayed in a burst. The scheduler polls every 15 seconds, so minute granularity is the practical minimum even when seconds are present in the expression.

## Session Behavior

`notify` does not depend on an ACP session. It survives `/clear`, `/stop`, process restarts, and engine changes.

`prompt_agent` only runs if the chat has an active ACP session when the scheduled time arrives. The implementation reuses the chat's current live session rather than resurrecting one invisibly. If there is no session, the task reports `Could not run automatically: no active session.` and, for recurring tasks, moves on to the next occurrence. The same principle applies to delivery errors: the user sees a plain explanation rather than having the task disappear silently.

## Routing: Which Chat, Which Message, Who Asked

Each task persists:

- `chat_id` — the Telegram chat that receives the result.
- `source_message_id` — the user message that triggered the scheduling request.
- `anchor_message_id` — the agent's confirmation reply, when it arrives before the task fires.
- requester id, username, and display name.
- chat type (`private`, `group`, `supergroup`).

Delivery replies to the anchor when available, otherwise to the source message. In group chats the bot also mentions the requester through a `tg://user` link, so the right person is notified even when the task was scheduled from a busy thread.

These routing fields are immutable for the lifetime of the task. Updating a schedule changes its mode, content, time, or recurrence; it does not move the task to another chat or another person.

## Recurring Schedules

A recurring task has a non-null `cron_expression` and cycles `pending -> running -> pending`. The scheduler reschedules it after every attempt:

- success: next occurrence, with `last_error` cleared;
- execution failure: next occurrence, with `last_error` retained for inspection;
- chat busy: the same occurrence is retried on the next poll;
- corrupt stored expression: the task is marked `failed` instead of looping forever.

Cancellation is a soft delete: a pending task moves to `cancelled` and disappears from `/scheduled`. A task that is running finishes its current occurrence and becomes cancellable again afterwards.

Execution is at-least-once across crashes: if the process dies after delivery but before the reschedule is written, the occurrence can run again after restart.

## Agent Management

The agent can inspect and change schedules through the same MCP server:

```text
list_scheduled_tasks(statuses=["pending"], limit=20)
update_scheduled_task(task_id=..., cron="30 8 * * *")
update_scheduled_task(task_id=..., cron="")      # clear recurrence
cancel_scheduled_task(task_id=...)
```

All management tools are scoped to the current chat: a task id from another chat is reported as `task not found`. Only `pending` tasks can be updated or cancelled; a running task must finish first. Updating a task resets its attempt state and clears the previous error.

The human-facing `/scheduled` command remains the quickest way to see and cancel the queue. See {doc}`how-to` for examples.

## Engine Independence

Scheduling must not depend on an engine-specific cron feature. The bot therefore reinforces the same policy in three places:

1. The internal `telegram-channel` MCP server instructions.
2. The `schedule_task` tool description.
3. A channel policy block included in every ACP prompt.

All three tell the agent to use the bot's scheduling tools and never a built-in or engine-native cron. The prompt-level block is the strongest guarantee, because it reaches engines that ignore or hide MCP instructions. It also tells the agent not to repeat the policy to the user.

## A Note About Multiple MCP Calls

During development you may sometimes see two `schedule_task` tool calls in the Telegram activity stream for what looks like a single request. That is usually the agent retrying or refining its tool usage while reasoning. It does not automatically mean that two follow-ups were scheduled.

The durable source of truth is the task row stored in {term}`ACP_SCHEDULED_TASKS_DB`. If one row was persisted, then one follow-up survived.

## Related Pages

See {doc}`mcp` for the broader MCP architecture and {doc}`configuration` for the runtime settings involved in scheduling.
