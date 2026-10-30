# How To Use The Bot From Telegram

This guide shows the main command flow for day-to-day usage.

## 1. Start working (implicit session)

Send a normal prompt. If the chat has no active session, the bot creates one automatically in the default workspace.

Example:

```text
Please review the latest commit and suggest a fix.
```

You can also attach images/documents; the bot forwards them to ACP prompt input.

## 2. Start or switch sessions with `/new`

Create a session in the default workspace:

```text
/new
```

Create a session in a specific workspace:

```text
/new /home/user/project
```

The bot will reply with the ACP `session_id` and workspace path.

Use `/new` when you want a different session/workspace than the current one.

## 3. Resume an existing ACP session

List resumable sessions and pick one from buttons:

```text
/resume
```

Resume by list index (0-based, where `0` is the most recent):

```text
/resume 0
```

List resumable sessions for a specific workspace:

```text
/resume /home/user/project
```

This resumes the most recent session in that workspace.

When you tap a button, the bot calls ACP `session/load` and switches the active conversation for that chat.

`/resume` accepts either an index or a workspace path (not both in the same command).

If your agent does not support ACP `session/list`, `/resume` will report it.

## 4. Inspect current session

```text
/session
```

Shows the active session workspace.

## 5. Cancel, stop, and clear

Cancel current in-flight operation:

```text
/cancel
```

Stop current session process:

```text
/stop
```

Clear current session:

```text
/clear
```

## 6. Switch activity mode

Show the current mode:

```text
/mode
```

Set the mode explicitly:

```text
/mode normal
/mode compact
/mode verbose
```

The available modes are:

- `normal`: separate activity messages, no streaming edits.
- `compact`: one in-progress status message that names the current action and becomes the final answer.
- `verbose`: in-place append-only streaming for reply text and active tool output.

(how-to-busy-queue)=
## 7. Busy-state handling

When you send a message while the agent is already processing a previous prompt, the bot:

- Queues your message automatically.
- Replies to that queued message with the busy notice.
- Shows a temporary **Send now** inline button.

### Send now

Pressing **Send now** immediately cancels the current in-flight operation and processes your queued message.
The queued notice is updated to `✅ Sent.` so chat state matches what happened.

If the current task finishes naturally before you press the button, your queued message runs automatically and the button is removed (pressing it after that shows "Already sent." with no side effects).

## 8. Schedule a prompt directly

You can schedule a deferred agent prompt without going through the agent, using `/schedule`. This is useful when the model rejects your request due to quota limits or when you want to queue work for later without starting an agent session first.

```text
/schedule <time> <prompt text>
```

`/schedule` is one-shot only. For recurring reminders, or to list, change, or cancel tasks through the agent, see {ref}`how-to-recurring-reminders` below.

Supported time formats:

- `30s` — 30 seconds from now
- `10m` — 10 minutes from now
- `2h` — 2 hours from now
- `1d` — 1 day from now
- Natural-language dates such as `tomorrow 9am` or `mañana 9am`
- ISO timestamp with timezone, e.g. `2026-04-11T10:00:00+00:00`

Example:

```text
/schedule tomorrow 9am Check for new review comments on the open PR
Summarize what changed
Flag anything blocking merge

/schedule mañana 9am Generate the weekly summary report
Include pending reviews
Mention overdue follow-ups

/schedule 2026-04-12T09:00:00+00:00 Generate the weekly summary report
Include pending reviews
Mention overdue follow-ups
```

The bot replies with the scheduled execution time. When the time arrives, the stored prompt is sent to the agent and the reply is posted back to the chat as a reply to your `/schedule` message.

The prompt may also continue on following lines. Everything after the time spec is stored verbatim, so multiline prompts keep their line breaks.

Natural-language parsing uses English and Spanish by default. You can customize the accepted languages with `telegram.schedule_languages` in the config file or the `--schedule-language` flag.

Single-line prompts still work too:

```text
/schedule 2h Generate the weekly summary report
```

If no active session exists when the scheduled time arrives, the bot starts a new one in the default workspace before sending the prompt. If the agent process cannot be started, the bot reports that the task could not be run automatically.

## 9. Inspect and cancel scheduled follow-ups

When the agent schedules a deferred follow-up, you can inspect the pending work directly from Telegram:

```text
/scheduled
```

The bot replies with a compact summary of the scheduled tasks for the current chat. Pending tasks are shown with inline **Cancel** buttons, so you do not have to copy or type task ids manually. Recurring tasks show their cron expression, and group chats also show who asked.

A typical interaction looks like this:

```text
/scheduled

Scheduled tasks for this chat:

Pending:
1. notify (recurring: 0 9 * * *) | 2026-03-31 09:00:00 UTC
  to: @alice
  Drink water.
2. prompt_agent | 2026-03-30 22:10:00 UTC
  Check the PR again and report any new comments.
```

From there you can tap **Cancel 1**, **Cancel 2**, and so on, or use **Cancel all pending** if you want to clear the queue for that chat.

Recurring tasks sit in `pending` between runs, so they show up in the list and can be cancelled there. A task that is running finishes its current occurrence first.

For this first version, cancellation is intentionally limited to tasks that are still `pending`. Tasks that are already `running` are visible for inspection, but they are not turned into interruptible jobs by this command.

(how-to-recurring-reminders)=
### Ask the agent to create or manage a recurring reminder

Recurring reminders are created through the agent, not through `/schedule`. Ask in normal language:

```text
Remind me every day at 9 UTC to drink water.
```

The agent calls `schedule_task` with a cron expression, for example `cron="0 9 * * *"`. The task is stored in SQLite, so it survives `/clear`, a bot restart, and a change of ACP engine. A plain `notify` reminder does not need a live session when it fires.

You can manage the queue conversationally too:

```text
Show my reminders.
Change my water reminder to 8 UTC.
Cancel my water reminder.
```

The agent uses `list_scheduled_tasks`, `update_scheduled_task`, and `cancel_scheduled_task`. Management is limited to the current chat, and only pending tasks can be changed or cancelled.

## 10. Restart bot process (dev workflow)

```text
/restart
```

With an active session, the bot replies with:

```text
Restart requested. Re-launching process...
Session restarted: <session_id> in <workspace>
```

Then it exits polling and re-execs the process (or uses `acp.restart_command` / `--restart-command` if configured).

If there is no active session, it replies:

```text
No active session. Use /new first.
```

Resume a specific saved session in-process with the restart command:

```text
/restart 0
```

You can combine index and workspace filter:

```text
/restart 2 /home/user/project
```

That path replies with:

```text
Session resumed: <session_id> in <workspace>
```

## 11. Run the agent's own slash commands

Some agents publish their own slash commands over ACP. Ask for them from Telegram:

```text
/cmds
```

The answer is a keyboard with one button per advertised command, three to a row, plus a last button that prints the
same commands as text with their description and input hint:

```text
/goal: Set or manage a persistent goal — <objective> [--turns <number>] | status | clear
/deep-research: Fan out web research and return a cited report
```

Tapping a button runs that command in the active session with no arguments. Commands that need input still work,
because the agent answers with its own usage text; type the command with its arguments once you know them, which is
matched in message text and in photo or document captions:

```text
/goal status
```

Typing and tapping share the rest of the rules: the allowlist, the group @mention gate, and the busy queue all still
apply. An unmentioned group `/command` is ignored in silence, so the bot does not answer with the unknown-command hint
either. Tapping a button counts as addressing the bot, so a tap runs in a group without an @mention. When Telegram adds
the bot's own `@username` to a command picked from the native menu, the bot strips that mention before forwarding, so
the agent receives `/goal status`.

The keyboard is bounded: `AGENT_COMMAND_KEYBOARD_MAX_ROWS` rows of `AGENT_COMMAND_KEYBOARD_COLUMNS` buttons are shown
and the remaining commands stay available through the listing button and by typing. A command whose name is not built
from `a-z`, `0-9`, `_` or `-` gets no button, because its name cannot travel as button data.

Three limits to know about:

- The list is empty until the agent advertises its commands, which normally happens when the session is created. Send
  a message or use `/new` first, then ask again.
- The bot's own commands always win. `/help`, `/mode` and the rest belong to the bot even if the agent advertises a
  command with the same name. Telegram only recognises a command in message text, so a bot command written in a media
  caption is dispatched by this gate and still runs the bot's handler without its arguments.
- The agent's commands never join Telegram's native `/` menu, which only mirrors the bot's own command list. Type them
  or copy them from `/cmds`. That menu comes from `register-commands`, so after an upgrade that adds a bot command,
  run `telegram-acp-bot register-commands --telegram-token <TOKEN>` again (or `--config <file> register-commands`) or the new entry stays hidden.

A command the agent did not advertise is rejected instead of forwarded, so a typo never reaches the agent as prompt
text, and a button left over from an older keyboard reports that the command is no longer advertised rather than running
it. Every advertisement replaces the previous one, so an agent that gains commands mid-session shows them the next time
you ask for `/cmds`. Commands that need an interactive terminal answer that themselves, for example
`Command /help requires interactive UI and is not available in ACP.`

## 12. Answer a question the agent asks you

When the agent needs a decision it asks over ACP as an *elicitation*: a form whose fields each list the answers it will
accept. The bot turns that form into a keyboard in the chat the question came from:

```text
❓ The agent is asking

Answer Questions

Báo cáo theo

[ Theo danh mục ]  [ Theo tháng ]
[ ⏭️ Skip this question ]
```

Tapping an option sends that answer to the agent and the prompt continues from it. The status bubble that was tracking
the agent's work closes as history when the question is sent, so the activity after your tap — and the agent's final
answer — arrive in a fresh bubble instead of overwriting the one that announced the question. A question with several answers
(multi-select) marks each pick with `✓` as you tap and waits for `✅ Send my answer`, so one response can carry several
options. `⏭️ Skip this question` declines the elicitation, which lets the agent proceed without an answer instead of
waiting. A form of two questions arrives as one message listing both prompts and offering both sets of buttons; the
agent is answered only once every question has a choice.
Once the answer reaches the agent the buttons are removed from the question message — so the same question cannot be
answered twice — and the answer itself is appended to it, leaving the question as a plain record of what was asked
and what you chose. A question the agent stops waiting for
loses its buttons the same way, even if nobody answered it: as soon as the agent's turn ends, a question that is still
open is closed, and the five-minute deadline is only the backstop for a turn that never ends at all. A question still
showing buttons is therefore the one still waiting, so always tap the newest keyboard rather than an older one left on
screen.

Three limits to know about:

- Only fields the agent offered as a choice of known options become buttons. A field you would have to type into, or an
  elicitation that is not a form, is declined so the agent falls back to asking in plain text. The same fallback happens
  when Telegram refuses the question message itself (for example under flood control): no keyboard is left half-shown,
  and the agent is told at once that the buttons did not arrive.
- The question waits five minutes for a tap and is then cancelled, and the last `ELICITATION_FORMS_MAX` forms are
  remembered **in each chat** — questions in one chat never push out another chat's open question. When a new question
  pushes an older one out of that list, the older question is declined so the agent stops
  waiting for it and its buttons are taken off the message — a keyboard never outlives the answer it is asking for.
  A button from an older keyboard, or one whose question the agent already stopped waiting for, answers with
  a toast instead of being sent twice.
- A button shows at most `ELICITATION_OPTION_LABEL_CHARS` characters of the option and ends in `…` when the agent offered
  a longer one, so a cut label is never mistaken for a complete answer. The question line above still shows what the
  agent asked, but the rest of a cut option is only visible if the agent repeats it there, so an option meant to be
  chosen by tap should be short.

The question is sent as its own message rather than into the compact status bubble, because the status line is rewritten
while the agent works and would overwrite a question nobody has answered yet.

(how-to-agent-dies)=
## 13. Keep working after the agent dies

The agent runs as a child process, so it can die while the bot is still running — it is killed by mistake, runs out
of memory, or exits on its own. The bot notices and brings the chat back by itself; you do not need `/new` or a bot
restart.

What happens depends on when the death is noticed:

- **Between two messages**: the next message starts a fresh agent, reloads the same session, and is answered
  normally. Nothing is lost.
- **In the middle of an answer**: the bot restarts the agent and reloads the same session, then replies with a notice
  instead of an answer. The interrupted message is **not** replayed, because the agent may already have run part of
  it (a command, an edit), and running that twice is worse than asking you once. Send the message again.
- **The agent cannot be started at all** (for example the binary in `acp.agent_command` is gone): the bot says so,
  and `/new` is the way back once the command is fixed.

Reloading uses ACP `session/load`, so the conversation history survives. An agent that does not implement it gets a new
session instead, and the interrupted one stays reachable through `/resume`. The chat's permission mode and any pending
permission or question buttons are handled with the dead agent: approvals that agent can no longer act on are dropped
rather than left waiting for a tap.

(how-to-two-agents)=
## 14. Run two agents side by side

One process can drive several agents at once, each behind its own Telegram bot. That is how
you keep a fast, cheap agent for everyday questions next to a heavier one for long tasks,
without running and supervising a process per bot.

Create a second bot with `@BotFather`, then declare both in a config file. Every token lives in
the file now (there is no environment layer for settings), so keep the file `chmod 600` and out
of version control:

```json
{
  "telegram": {
    "bot_token": "0:shared-default",
    "allowed_user_ids": [123456789]
  },
  "acp": {
    "workspace": "/home/alice/projects"
  },
  "agents": {
    "qoder": {
      "telegram": {
        "bot_token": "111111:qoder"
      },
      "acp": {
        "agent_command": "qodercli --acp"
      }
    },
    "codex": {
      "telegram": {
        "bot_token": "222222:codex",
        "allowed_user_ids": [123456789, 987654321]
      },
      "acp": {
        "agent_command": "npx @zed-industries/codex-acp"
      }
    }
  }
}
```

The flat sections carry what both bots share, and each agent overrides only what differs; see
{ref}`config-agents`. Because an agent that sets no `bot_token` falls back to the flat
`telegram.bot_token`, give each agent its own distinct token — startup refuses two bots that end
up on the same one.

Preview the command menus, register them, and start the fleet:

```bash
telegram-acp-bot --config ./fleet.json register-commands --dry-run
telegram-acp-bot --config ./fleet.json register-commands
telegram-acp-bot --config ./fleet.json
```

You get one ACP child process and one Telegram session per agent. Each bot keeps its own
chats, activity mode, permission decisions, scheduled tasks and channel state, so a long
prompt in one chat does not stop the other bot from answering. While a bot works, every log
line it produces carries `agent=qoder`, and its `/help` opens with the agent's name.

Two things worth knowing before you rely on it:

- A bot that cannot start takes the whole process down, so the service manager brings
  everything back rather than leaving one chat silently unanswered. Use `--only qoder` to run
  one agent while you debug the other.
- `/restart` re-launches the process, so it restarts every agent, not only the bot you asked.

## 15. Follow a group chat

Put the bot in a group and it can take part in the conversation, not just answer direct orders.

Telegram decides what the bot is allowed to hear. With the default bot privacy mode a group bot only
receives commands, replies and @mentions, so to let it follow the whole conversation either turn privacy
off with @BotFather (`/setprivacy` → `Disable`, then remove and re-add the bot to the group) or make it an
administrator.

Then decide how loudly it should speak:

```bash
telegram-acp-bot --group-require-mention
```

With that flag the bot stays quiet until someone @mentions it or replies to one of its messages; without it,
the bot answers every group message. Private chats are unaffected either way.

Quiet is not the same as deaf. Every group message the bot receives is recorded in a short per-chat log, so an
agent that is finally addressed can read what it missed with `telegram_list_messages`. Set
`telegram.group_history_size` to change the cap, or to `0` to keep nothing. See {ref}`mcp-feature-history`.

In a group, the prompt the agent receives is framed for the room. It opens with a reminder that everyone reads
the answer and that nobody is notified unless the agent names them, then tags the message with its sender:
`@alice: can you check the build`, or `Alice Nguyễn: ...` when that member has no public username. Private
chats get neither, because there is only one person to talk to.

That framing is what makes a request like "discuss the deploy with Bob" behave the way you would expect: the
agent answers Bob, opening its message with `@bob`, instead of replying to whoever typed the request. A reply
that repeats an `@username` becomes a real Telegram mention, and Telegram notifies that person. The names come
from the sender tags the agent has seen, and `telegram_list_messages` lists recent senders when it needs one
that has not spoken to it directly. A member with no public username cannot be mentioned at all, so the agent
can only address them in plain words.

The mention rule reads the message a turn ends on, not only the one it started with: editing a group message
to add `@yourbot` wakes the bot, and a `compact` answer that replaces its own status bubble is seen by the rest
of the room in its final form. Two of your own bots in one group hear each other the same way once Telegram
allows bot-to-bot traffic (BotFather → the bot → Bot-to-Bot Communication Mode, plus privacy mode off or admin
rights), and each hears the other under its `@username` — so both are on the allowlist if you want them to
answer one another.

That exchange is bounded, because an answer written to notify a person is also an answer that keeps a bot
addressed. The bot counts answers that came from another bot and stops after eight in a row, until someone
speaks in the group; the next person's message refills the budget. When it stops it says so once, so a quiet
room reads as a pause rather than a hang. Asking a peer to review something fits inside the budget, a pair of
agents arguing with nobody watching does not.

A reply is only read as an address when a person sent it. Every answer this bot posts quotes the message that
triggered it, so treating the quote as an address would keep two bots alive on busy notifications alone — the
"⏳ Agent is busy" notice replying to a peer's message, which the peer answers, which queues again — with
neither ever naming the other.

## Notes

- Session scope is per Telegram chat.
- The first prompt creates a session implicitly when none is active (including after `/restart`).
- `/new` replaces the active session for that chat and is intended for explicitly switching to another workspace/session.
- `/resume` is ACP-native (`session/list` + `session/load`) and depends on agent capabilities.
- `/mode` is stored per Telegram chat, so different chats can use different activity modes.
- `/cmds` shows the slash commands the agent advertised for the current session as tap-to-run buttons; see the how-to
  section above.
- A question the agent asks over ACP (an elicitation) is offered as tap-to-answer buttons; see the how-to section above.
- A dead agent process is restarted and its session reloaded on the next message; see {ref}`how-to-agent-dies`.
- Several agents can run in one process, each behind its own Telegram bot; see {ref}`how-to-two-agents`.

## Logging and traceability

Every application log line includes contextual identifiers:

- `agent`: which agent of this process produced the line. A single-bot deployment always
  reports `default`, and the `text` format omits the field on the few lines logged before any
  bot starts.
- `chat_id`: Telegram chat id.
- `session_id`: ACP session id (when a session is active).
- `prompt_cycle_id`: per-prompt cycle/task id generated for each incoming user prompt.

This lets you trace:

- all activity from one ACP session (`session_id`),
- all logs tied to a single prompt execution (including queued prompts) via `prompt_cycle_id`.

Set `acp.log_format` to `json` (or pass `--log-format json`) to emit structured logs for log aggregators.

By default, the bot also emits compact text previews for prompts and replies:

- `Prompt received: ...`
- `Reply sent: ...`

The human-oriented `text` log format uses `rich` output in the terminal to highlight the logger, chat/session/cycle ids, and the message preview.

To keep logs focused on auditability, verbose transport/framework logs (for example `httpx` Telegram API request lines) are downgraded to warning level by default.
