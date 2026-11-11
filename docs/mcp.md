# MCP Integration

This project includes a built-in MCP server that the bot advertises automatically to the ACP agent. The practical reason for having it is simple: some things belong to the Telegram channel itself, not to the ACP conversation protocol.

That becomes visible in requests such as these:

```text
Send me the generated screenshot.
React with a thumbs up if that was enough.
Check for review in 2 minutes.
Remind me at 9 PM to restart the bot.
```

In all of those cases, the agent needs a channel-side capability. It may need to send a Telegram attachment, or schedule something that should happen later in the same chat. Those are exactly the kinds of actions exposed through the internal MCP tools.

If you run:

```bash
uv run telegram-acp-bot
```

every new or resumed ACP session receives an MCP stdio server named `telegram-channel`. In practice, this is how the agent performs Telegram-specific side effects without extending ACP itself.

ACP remains responsible for the conversational session with the agent. MCP is the place for actions that belong to the Telegram channel. That split is the main design idea. It keeps the ACP side protocol-native while still giving the agent a clean way to interact with Telegram.

## What The Tools Enable

The built-in server currently exposes seven tools.

`telegram_channel_info` is a lightweight capability probe. It lets the agent confirm what this Telegram channel integration can currently do.

`telegram_send_attachment` lets the agent deliver one or more files (image, video, audio, or any document) to the current Telegram chat. This is the tool behind flows like “send me the generated screenshot” or “upload the review artifact”. Files larger than the 50 MB Bot API cap ride MTProto when it is configured; see {ref}`mcp-feature-large-download`.

`telegram_fetch_attachment` lets the agent download media that Telegram announced as pending, by `file_id`, into its own workspace. This is the counterpart of the lazy inbound path described under `telegram.group_media_queue`.

`telegram_download_file` fetches the media of one chat message over MTProto instead of the Bot API, which makes it the only way to download a file larger than the 20 MB Bot API cap. It needs `telegram.api_id` / `telegram.api_hash` to be configured. See {ref}`mcp-feature-large-download`.

`telegram_list_messages` lets the agent read the group messages it never answered. The bot records each incoming group message into a short capped log, so this is how the agent catches up on what the room was saying before it was addressed. See {ref}`mcp-feature-history`.

`telegram_set_message_reaction` lets the agent add a standard Telegram emoji reaction to a message in the current chat. This is the tool behind lightweight moments such as “acknowledge that briefly” or “react instead of sending a noisy one-line reply”.

`schedule_task` lets the agent schedule a one-shot deferred follow-up. This is the tool behind flows like “check for review in 2 minutes” or “remind me tomorrow at 9 PM”.

Here is a concrete example of the second case. A user may say:

```text
Check for review in 2 minutes.
```

The agent can translate that into a relative scheduling call such as:

```text
schedule_task(
  mode="prompt_agent",
  delay_minutes=2,
  prompt_text="Check the review status again and report any changes."
)
```

The point of the relative delay inputs is that the agent does not need to compute an absolute timestamp for short delays. For this kind of prompt, `delay_minutes=2` is the natural representation.

## How It Is Implemented

The implementation lives under `telegram_acp_bot.mcp`, and the built-in server entrypoint is:

```bash
python -m telegram_acp_bot.mcp.server
```

The easiest way to think about the MCP layer is as a small local service that translates an ACP-side tool call into a Telegram-side action.

```{mermaid}
:align: center
flowchart LR
    U[Telegram user] --> B[telegram-acp-bot]
    B --> A[ACP agent session]
    A --> M[Internal MCP server]
    M --> T[Telegram Bot API]
    M --> S[(Runtime state / SQLite)]
```

The bot process owns all of this. The MCP server is not a separate deployment. It is launched as a stdio subprocess and receives the minimal runtime state it needs to route tool calls back to the correct Telegram chat.

For configuration details, see {doc}`configuration` and {ref}`mcp-channel-environment-variables`. In normal use, the important environment variables are injected by the bot runtime itself: {term}`ACP_TELEGRAM_CHANNEL_STATE_FILE`, {term}`ACP_TELEGRAM_BOT_TOKEN`, {term}`ACP_TELEGRAM_API_ID` and {term}`ACP_TELEGRAM_API_HASH` when MTProto downloads are configured, and, when scheduling is enabled, {term}`ACP_SCHEDULED_TASKS_DB`.

(mcp-feature-attachments)=
## Reply With Attachments

`telegram_send_attachment` delivers one or more files back to the current Telegram chat. This is the feature behind flows where the agent answers not only with text, but also with a file, image, video or audio clip.

Pick the input by where the bytes already live:

- If the content is already a file on disk, pass its location with `path`. Do **not** read the file and hand-encode it into `data_base64` — that only bloats the agent context. The `path` input is disabled by default and must be enabled with {term}`ACP_TELEGRAM_CHANNEL_ALLOW_PATH` (or `acp.allow_path_inputs`).
- Use `data_base64` only for bytes you produced in memory and never wrote to disk. Pair it with `name` so the type can be inferred from the extension.

Delivery is chosen from the MIME type so media plays natively in Telegram instead of arriving as a plain file download:

| MIME prefix | Sent as |
| ----------- | ------- |
| `image/*` | photo |
| `video/*` | video |
| `audio/*` | audio |
| anything else | document |

Pass `mime_type` to force a specific type. A typical single-file call looks like this:

```text
telegram_send_attachment(
  path="artifacts/review.png"
)
```

To send several files in one call, pass `attachments` as a list, each entry taking the same `path` / `data_base64` / `name` / `mime_type` fields (do not combine `attachments` with the top-level `path` / `data_base64`). Each item is routed independently:

```text
telegram_send_attachment(
  attachments=[
    {"path": "clip.mp4"},
    {"path": "transcript.txt"},
  ]
)
```

The Bot API uploads at most 50 MB per file. Anything larger is carried over MTProto instead, with no change to the call: a `path` input streams straight from disk (the bytes are never read into memory), media still plays natively, and a report the call returns `delivered_as: video` and friends as usual. That path needs the same `telegram.api_id` / `telegram.api_hash` pair as {ref}`mcp-feature-large-download`; without it an oversized send returns an error naming the missing configuration instead of attempting an upload the Bot API would refuse. MTProto moves up to 2 GB per file.

(mcp-feature-fetching)=
## Fetch Pending Media

Group chats are the case where this matters most. With `telegram.group_media_queue` enabled, a group message that the `telegram.group_require_mention` gate ignores is not dropped: only its Telegram `file_id` is queued, and the next prompt the bot does answer starts with a notice listing the parked references. Nothing is downloaded, so bytes never sit in the bot's memory or in the agent's context until the agent decides they are worth reading:

```text
telegram_fetch_attachment(
  file_id="AgAC...=",
  mime_type="image/jpeg"
)
```

The download lands inside the agent workspace, by default under `.telegram-media/`, and the result reports the local `path`. Pass `dest` to choose a meaningful name such as `invoices/hoa-don.pdf`; a `dest` that resolves outside the workspace is rejected. Queued references are announced once and capped per chat, so a long run of unmentioned photos cannot grow the state file without bound.

A queued reference is a promise with a shelf life, not an archive. Telegram keeps a media file downloadable by the bot for roughly an hour after it last moved through the API, and the hosted Bot API refuses downloads of files larger than 20 MB. The announcement therefore carries the `queued_at` timestamp of each reference so the agent can judge how stale it is, and a fetch that arrives after the window returns an explicit `download failed` error naming the `file_id`. For the 20 MB cap there is a second tool, `telegram_download_file`, described next; if a photo matters later than the hour, ask the sender to post it again.

(mcp-feature-large-download)=
## Move Large Files

Both directions of file transfer hit a Bot API wall: downloads stop at 20 MB and uploads at 50 MB. The same MTProto credentials lift both, because over MTProto — the protocol Telegram's own clients use — the bot's token moves up to 2 GB.

### Download with `telegram_download_file`

`telegram_fetch_attachment` cannot retrieve a large video or archive, however fresh it is. `telegram_download_file` exists for that case: it looks the message up over MTProto and downloads its media with no size cap.

```text
telegram_download_file(
  message_id=42
)
```

A call names the `message_id` instead of the `file_id`, because MTProto resolves media through the message that carries it. Pending-attachment announcements always include both, and any recent message id of the chat works too. The result is the same shape as a fetch: the download lands under `.telegram-media/` keeping the original filename, `dest` can choose another path inside the workspace, and the result reports the local `path`.

### Upload beyond 50 MB

`telegram_send_attachment` picks the transport by size on its own. Files up to 50 MB ride the Bot API exactly as before; anything larger is streamed over MTProto with the same inputs, the same native media routing (`photo`, `video`, `audio`, `document`) and the same result shape. A `path` input streams straight from disk, so a multi-gigabyte file never lands in memory.

### Configuration

Because both tools speak MTProto, they need the API credentials of a Telegram app in addition to the bot token: `telegram.api_id` and `telegram.api_hash` in the config file (from [my.telegram.org](https://my.telegram.org), the same pair any Telethon or Pyrogram app uses). With the pair configured, transfers use the bot's own token, so nothing else changes. Without it, the tools answer with a clear configuration error, and `telegram_channel_info` reports `supports_large_file_downloads: false` and `supports_large_file_uploads: false`.

(mcp-feature-history)=
## Read Recent Group Messages

A bot that only answers when it is @mentioned still hears the rest of the room. `telegram_list_messages`
gives the agent a way to use that: every group message the bot receives is appended to a short log for its
chat, and the tool reads the log back, newest messages last.

```text
telegram_list_messages(
  limit=20
)
```

Each entry carries the `message_id`, the sender, the text and the moment it was recorded, so the agent can
tell who said what before it was pulled into the conversation. Pass `since_message_id` with the
`newest_message_id` of a previous call to read only what arrived since then.

An edit rewrites the entry it belongs to rather than adding a new one, so a message stays in the log exactly
once and always shows its last version. That matters most in `compact` activity mode, where the final answer
replaces the in-progress status bubble: the bubble and the answer are one `message_id`, and the log keeps the
answer.

The sender names here are the same ones the transport puts on every group prompt (`@alice: ...`, plus a
reminder that only a named reply notifies anyone), so an agent can mention people it never spoke to directly.

The log is capped per chat by `telegram.group_history_size` (default `200`), and it is stored in the same
private channel state file the rest of the routing state uses, so it survives a restart of the bot. Setting
the size to `0` stops the recording entirely and the tool then answers with an empty list.

Two limits are worth stating plainly. Telegram, not this bot, decides which group messages reach the bot at
all: with the default bot privacy mode only commands, replies and @mentions are delivered, so a bot that must
follow a group conversation needs privacy mode disabled or a seat as an administrator. And the log starts
where the bot starts — nothing older than the first recorded message can be read, because the Telegram Bot
API offers no way to fetch messages a bot never received.

Because the log also holds messages from people who are not on the allowlist, treat it as the room's own
conversation: it is readable by whichever agent the chat is bound to, so set `telegram.group_history_size`
to `0` in a group where that is not acceptable.

(mcp-feature-reactions)=
## React To Messages

`telegram_set_message_reaction` is the small, Telegram-native counterpart to a normal text reply. It is useful when the agent wants to acknowledge something briefly, celebrate success, or confirm that it understood the user's last message without adding another sentence to the chat.

A typical call looks like this:

```text
telegram_set_message_reaction(
  emoji="👍"
)
```

When `message_id` is omitted, the server tries to infer the active Telegram prompt message for the current turn. That makes the tool natural to use during a normal conversational reply. If there is no active prompt message to infer, the tool returns an explicit error instead of guessing.

The first version intentionally supports only standard Telegram reaction emoji. Custom emoji and stickers are a separate design space, and they are tracked independently from this lightweight reaction flow.

(mcp-feature-scheduling)=
## Schedule Tasks

`schedule_task` stores a one-shot deferred follow-up for the current chat. It supports two styles of time input. For absolute times, the agent can pass `run_at` as an ISO timestamp with an explicit timezone offset. For relative requests such as “in 10 minutes” or “in 30 seconds”, the preferred form is a delay input like `delay_minutes=10` or `delay_seconds=30`. That keeps the tool call simple and avoids making the agent compute a wall-clock timestamp for short delays.

A typical relative scheduling call looks like this:

```text
schedule_task(
  mode="prompt_agent",
  delay_minutes=10,
  prompt_text="Check the PR again and tell me if there are new comments."
)
```

## Session Routing

Both attachment delivery and scheduling need to know which Telegram chat should receive the side effect. The MCP layer resolves that from a local state file maintained by the bot runtime.

By default that file is named after the running bot process, so routing state and queued media references are discarded when the process exits. Set `acp.channel_state_file` to a stable path to keep them across restarts; the bot then injects it into each instance's server as {term}`ACP_TELEGRAM_CHANNEL_STATE_FILE`. See {doc}`configuration`.

(mcp-per-agent-state)=
When one process runs several agents, each gets its own state file, database and internal `telegram-channel` server,
pointed at that agent's own token. Agent A's tools therefore cannot resolve or deliver into agent B's chats, and one
agent cannot claim another agent's scheduled task. A named agent keeps those files in its own
directory under {envvar}`XDG_STATE_HOME`, such as `~/.local/state/telegram-acp-bot/agents/qoder/`;
see {ref}`config-agents`.

`AcpAgentService` persists a `session_id -> chat_id` mapping, and the MCP server reads that mapping when a tool call arrives. If a tool call does not provide `session_id`, the server tries to infer it. The inference order is intentionally conservative: if there is only one active mapped session, it uses that; otherwise it tries the last active session remembered by the channel state. If neither rule produces a safe answer, the tool returns an explicit error rather than guessing.

This routing model is what allows an agent response such as “send me the screenshot” or “remind me in 20 minutes” to affect the same Telegram chat that originated the ACP session.

## Why You May See Multiple Tool Calls

One source of confusion during development is that Telegram may show more than one `Tool call` block for the same MCP tool during a single agent turn. That does not necessarily mean the side effect happened twice.

The agent is free to call a tool more than once while deciding parameters or retrying a failed attempt. For example, it may first try one shape of `schedule_task` and then retry with corrected arguments. The reliable source of truth is the underlying side effect itself. For deferred follow-ups, that source of truth is the row persisted in {term}`ACP_SCHEDULED_TASKS_DB`. If only one task row was created, only one schedule survived.

## Example Sequence

The attachment flow is a good concrete example because it is short and easy to visualize:

```{mermaid}
:align: center
sequenceDiagram
    participant U as Telegram user
    participant B as Bot runtime
    participant A as ACP agent
    participant M as MCP telegram-channel
    participant T as Telegram Bot API

    U->>B: "Send me the generated screenshot"
    B->>A: ACP prompt
    A->>M: telegram_send_attachment(data_base64="...", name="artifact.png")
    M->>M: resolve session_id and chat_id
    M->>T: sendPhoto(chat_id, artifact.png)
    T-->>U: image delivered
    M-->>A: { ok: true, delivered_as: "photo" }
    A-->>B: final reply text
    B-->>U: confirmation message
```

## External MCP Servers

In addition to the built-in `telegram-channel` server, you can register external MCP servers via the `mcp_servers` key in the {doc}`config file <configuration>`.

All configured servers are merged with the internal one and passed to every ACP session at startup. If you configure a server named `telegram-channel`, it overrides the internal server.

### Stdio server (local process)

```json
{
  "mcp_servers": {
    "echo": {
      "command": "uv",
      "args": ["run", "examples/echo_agent.py"],
      "env": {
        "MY_API_KEY": "secret"
      }
    }
  }
}
```

Fields:

- key name — server identifier exposed to ACP; must match `^[a-z0-9-_]+$`
- `command` (required) — path to the executable
- `args` (optional) — list of string arguments
- `env` (optional) — object of `string → string` environment variables

### Remote server (HTTP)

```json
{
  "mcp_servers": {
    "remote": {
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

Fields:

- key name — server identifier exposed to ACP; must match `^[a-z0-9-_]+$`
- `url` (required) — URL of the remote MCP server
- `headers` (optional) — object of `string → string` HTTP headers

### Mixed example

```json
{
  "mcp_servers": {
    "local": {
      "command": "npx",
      "args": ["@modelcontextprotocol/server-filesystem", "/home/alice/projects"]
    },
    "remote": {
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer token"
      }
    }
  }
}
```

### Validation

Each server must define exactly one transport: `command` (stdio) **or** `url` (remote), not both. The bot validates the config at startup and reports clear errors for invalid definitions.

### Troubleshooting

**Missing transport**
```json
{ "mcp_servers": { "broken": { "args": ["--help"] } } }
```
Error: `MCP server 'broken' must define 'command' (stdio) or 'url' (remote)`

**Both transports defined**
```json
{ "mcp_servers": { "broken": { "command": "x", "url": "http://x" } } }
```
Error: `MCP server 'broken' cannot define both 'command' and 'url'`

**Wrong type for args**
```json
{ "mcp_servers": { "broken": { "command": "x", "args": "--flag" } } }
```
Error: `MCP server 'broken' 'args' must be a list of strings`

## Current Limits

The MCP layer is intentionally small. It does not implement follow-up suggestion buttons yet, and multi-session ambiguity can still require an explicit `session_id`. That is a tradeoff in favor of predictable routing and debuggable behavior.
