(usage)=
# Usage guide

This page is the quick reference for talking to the bot. For the narrative walkthrough of a
feature, see {doc}`how-to`; for platform setup, see {doc}`configuration` and
{doc}`dingtalk`.

## Before you start

The bot answers only chats on its allowlist: Telegram user ids or usernames, or DingTalk staff
ids or nicknames. A message from anyone else is ignored. In a group the bot also listens only
when it is addressed — an @mention, a reply to one of its messages, or (Telegram only) a command
when the mention gate is off. See {ref}`config-file` for the allowlist settings.

## Send a prompt

Send ordinary chat text. If the chat has no active session, the bot creates one in the default
workspace and answers there. Attach images or documents to the same message to forward them to
the agent.

```text
Look at this traceback and propose a fix.
```

The bot may show activity blocks while the agent works, then sends the final answer. The
display depends on the activity mode: `normal` sends separate messages, `compact` keeps one
status message that becomes the answer, and `verbose` streams into one message. Change it per
chat with `/mode`.

## Command reference

Commands are matched at the start of a message, or in the caption of a media message.

| Command | What it does |
|---|---|
| `/start` | Start or resume a session in the default workspace. |
| `/new [workspace]` | Create a new session, optionally in another workspace. |
| `/resume [N\|workspace]` | List resumable sessions, or resume the `N`-th most recent one in a workspace. |
| `/session` | Show the workspace of the active session. |
| `/cancel` | Cancel the current agent operation, keeping the session. |
| `/stop` | Stop the current session process. |
| `/clear` | Clear the active session for this chat. |
| `/restart [N [workspace]]` | Restart the bot process, or resume a saved session in-process. |
| `/mode [normal\|compact\|verbose]` | Show or set how activity is displayed in this chat. |
| `/cmds` | List the slash commands the agent advertises, with tap-to-run buttons on Telegram. |
| `/schedule <time> <prompt>` | Schedule a one-shot prompt; `<time>` accepts `30s`, `10m`, `2h`, `1d`, natural language, or an ISO timestamp. |
| `/scheduled` | List pending scheduled tasks and offer cancel buttons. |
| `/help` | Show the commands above. |

Typing an agent command such as `/goal status` sends it to the agent; a command the agent did
not advertise is rejected instead of forwarded as prompt text.

## Sessions

A chat has one active session at a time. The usual flow is:

1. Send a prompt and let the bot create a session implicitly.
2. Use `/session` to see where it runs, or `/new /path/to/project` to work elsewhere.
3. Use `/resume` to come back to an earlier conversation; the list is the agent's own
   `session/list`, so an agent that does not implement it reports that.
4. Use `/cancel` to stop the current turn, `/stop` to stop the agent process, and `/clear` to
   drop the mapping for this chat.

If the agent process dies, the next message restarts it and reloads the same session. The
interrupted prompt is not replayed; send it again. See {ref}`how-to-agent-dies`.

## Permissions and questions

When the agent asks for approval or for an answer to a form, the bot turns the request into a
choice:

- **Telegram** shows inline buttons, including multi-select questions with a submit button and a
  skip option.
- **DingTalk** shows the same options as numbered replies; answer with the number.

A question is answered once. After the answer (or after the agent stops waiting), the buttons
or the choice message no longer accept taps. See {ref}`how-to-busy-queue` for what happens when
you write while the agent is already working.

## Scheduled tasks

`/schedule` is the direct, one-shot form:

```text
/schedule 2h Check the build logs and report failures
```

The bot replies with the execution time, runs the prompt when it fires, and posts the answer in
the same chat. A `notify` reminder created by the agent through `schedule_task` does not need a
live session; a `prompt_agent` task creates a session if none is active. `/scheduled` lists and
cancels pending work. See {doc}`deferred_followups` for recurring reminders.

## Channel differences

| Area | Telegram | DingTalk |
|---|---|---|
| Choice prompts | Inline buttons | Numbered replies |
| Streaming replies | Edited messages | AI Card updates; one final message when cards are unavailable |
| Activity modes | `normal`, `compact`, `verbose` | `normal`; the other modes fall back to separate messages (reply streaming still uses a card) |
| Typing indicator, reactions | Yes | No |
| Group activation | @mention, reply, or every message when the mention gate is off | @mention only |
| Large files | Bot API and MTProto fallback | DingTalk media API limits |
| Group history tool | Full history of messages the bot receives | Only messages the bot receives |

DingTalk setup, app permissions and its limits are in {doc}`dingtalk`.

## Where to run it

Run the CLI in the foreground for a first check, then keep it running as a service:

```bash
acp-chat-bot --config ./acp-chat-bot.json
```

The systemd user service template and its setup steps are in {ref}`how-to-systemd`. All
settings, including the channel and credentials, are described in {doc}`configuration`, and
every flag is listed in {doc}`cli`.
