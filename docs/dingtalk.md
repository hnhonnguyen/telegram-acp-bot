# DingTalk

The bot can run on **DingTalk** as an alternative to Telegram. One agent runs exactly one
channel; a single process can still host several agents on different channels.

DingTalk support uses **Stream mode**: the bot opens an outbound WebSocket to DingTalk, so it
needs no public URL, TLS certificate, or inbound firewall rule. Replies use the session webhook
that arrives with each message; proactive sends (long-running agents, scheduled tasks) fall back
to the DingTalk OpenAPI.

## Create the app

1. In the [DingTalk developer console](https://open-dev.dingtalk.com/), create an **internal
   application** for your organization.
2. Under **Robot**, enable the robot and switch its message-receiving mode to **Stream**.
3. Note the **Client ID** (AppKey), **Client secret** (AppSecret), and the robot code. The robot
   code defaults to the Client ID.
4. Grant the application permission to send robot messages and upload media.
5. Publish the application version and add the robot to the target users or group.

The bot subscribes to the `/v1.0/im/bot/messages/get` callback topic. Group chats only deliver
messages in which the robot is `@mentioned`, so the mention gate is effectively enforced by
DingTalk itself.

## Allowlist

DingTalk identities are staff IDs, not phone numbers. The bot refuses to start without at least
one allowed staff ID or sender nickname:

```json
{
  "channel": "dingtalk",
  "dingtalk": {
    "client_id": "dingxxxxxxxx",
    "client_secret": "secret",
    "robot_code": "dingxxxxxxxx",
    "allowed_staff_ids": ["0123456789"],
    "allowed_nicknames": ["alice"]
  },
  "acp": {
    "agent_command": "qodercli --acp"
  }
}
```

Nicknames are matched case-insensitively as a secondary path. Prefer staff IDs, which cannot be
changed by renaming a user.

## Run it

```bash
acp-chat-bot --config ./acp-chat-bot.json
```

Everything can also be passed on the command line:

```bash
acp-chat-bot \
  --channel dingtalk \
  --dingtalk-client-id dingxxxxxxxx \
  --dingtalk-client-secret secret \
  --allowed-staff-id 0123456789 \
  --agent-command "qodercli --acp"
```

The old `telegram-acp-bot` command and the `telegram_acp_bot` import still work during the
rename window. They emit a `DeprecationWarning` and forward to the new names.

## Behaviour

The DingTalk channel implements the same command and session model as Telegram:

- `/new`, `/resume`, `/session`, `/cancel`, `/stop`, `/clear`, `/restart`
- `/mode`, `/cmds`, `/schedule`, `/scheduled`, `/unschedule`, `/help`
- Busy-state handling: send now, replace the queued prompt, or keep it queued
- Permission and elicitation prompts answered by replying with the option number
- Scheduled tasks that survive a restart

The last conversation target per chat is persisted in the channel state file, so scheduled
notifications and prompts can be sent after the process restarts or the session webhook expires.
Group history stores only the messages DingTalk delivered to the bot, which is a subset of the
group conversation.

DingTalk robot messages cannot be edited after they are sent, so the bot suppresses the agent's
streaming reply previews and delivers the final answer once. Tool and reasoning activity still
arrives as separate status messages; `compact` and `verbose` therefore degrade to that same
one-message-per-update display.

## Files and images

Inbound images and files are downloaded through the DingTalk file API and handed to the agent as
attachments. The agent can send images and files back as part of its reply, or call the
`dingtalk_send_attachment` MCP tool with a path inside the working directory or base64 bytes.

Media uploads are limited by DingTalk's own media size limits; unlike Telegram, there is no
MTProto path for oversized files.

## MCP server

Agents running on DingTalk receive an internal MCP server named `dingtalk-channel` with:

- `dingtalk_channel_info`
- `dingtalk_list_messages` — messages addressed to the bot, newest first
- `dingtalk_send_attachment`
- `schedule_task`, `list_scheduled_tasks`, `update_scheduled_task`, `cancel_scheduled_task`

Telegram-only tools (`telegram_*`, reactions, MTProto) are not exposed.

## Current limitations

| Capability | DingTalk status |
|---|---|
| Text, markdown, chunking | Supported |
| Commands, sessions, resume | Supported |
| Permission and elicitation choices | Numbered text replies (interactive cards not yet) |
| Activity modes | `normal`; `compact` and `verbose` degrade to separate messages |
| Streaming reply previews | Not shown; the final answer is delivered once |
| Busy queue | Supported |
| Files and images, inbound/outbound | Supported through the DingTalk media API |
| Scheduled tasks | Supported, including proactive sends after restart |
| Group history | Only messages delivered to the bot |
| Typing indicator and reactions | Not available on DingTalk |
| MTProto-sized uploads | No equivalent |

Interactive cards and streaming card updates depend on the permissions and card templates of the
tenant application. They are planned after a tenant spike; the numbered fallback is always
available.
