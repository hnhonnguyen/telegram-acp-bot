# CLI Reference

The bot entrypoint is `telegram-acp-bot`.

```{richterm} env PYTHONPATH=../src uv run -m telegram_acp_bot --help
:hide-command: true
```

## Arguments

- `--config`
  Path to the JSON config file to load.
  Default: the first file found in a standard location. A pinned path that cannot be read stops
  the run instead of falling back to discovery; see {ref}`config-file`.

- `--telegram-token`
  Default: `telegram.bot_token` in the config file.
  Required if that key is not set.

- `--agent-command`
  Default: `acp.agent_command` in the config file.
  Required if that key is not set.

- `--restart-command`
  Default: `acp.restart_command` in the config file.
  Optional command used by `/restart` to relaunch the process.

- `--allowed-user-id`
  Repeatable allowlist of Telegram user IDs.
  At least one allowlist entry is required across IDs/usernames.

- `--allowed-username`
  Repeatable allowlist of Telegram usernames.
  `@` prefix is optional; values are normalized to lowercase.
  At least one allowlist entry is required across IDs/usernames.

- `--agent NAME=COMMAND`
  Repeatable. Declares one agent of a multi-agent run, or overrides the ACP command line of an
  agent the config file already declares, without editing the file.
  Each name is a separate Telegram bot, so give it its own `agents.<name>.telegram.bot_token`.
  Names must match `^[a-z0-9][a-z0-9_-]{0,62}$`. See {ref}`config-agents`.

- `--only NAME`
  Repeatable. Runs just the named agent(s) of the declared set, and refuses a name nothing declares.
  The config file offers the same choice as a top-level `"only"` list, which this flag replaces.

- `--group-require-mention`
  Default: `telegram.group_require_mention` in the config file.
  Flag: when set, group messages are answered only if they `@mention` the bot or reply to
  one of its messages. Private chats are unaffected.

- `--group-media-queue`
  Default: `telegram.group_media_queue` in the config file.
  Flag: when set, media in group messages that the mention gate ignores is kept as a
  `file_id` reference and announced to the agent on its next answered prompt, where
  `telegram_fetch_attachment` can download it on demand. Needs `--group-require-mention`.

- `--group-history-size`
  Default: `telegram.group_history_size` in the config file, then `200`.
  Number of recent group messages the bot keeps per chat for `telegram_list_messages`.
  Set it to `0` to stop recording group chat altogether. See {ref}`mcp-feature-history`.

- `--status-reactions`
  Default: `telegram.status_reactions` in the config file.
  Allowed values: `off`, `minimal`.
  In `minimal`, the bot puts its own emoji reaction on the message that carried the prompt and
  replaces it as the cycle moves: 👀 when the chat is already busy, 🤔 while the agent works,
  then 👍 when the answer is delivered or 💔 when the cycle ends without one.
  In `off` (default) the bot adds no reactions of its own; an agent can still react through
  `telegram_set_message_reaction`. A reaction that Telegram refuses is logged and dropped, never
  retried, and never delays the reply; each reaction that lands is logged at `INFO`.
  Only these two levels exist: a finer level that also marks tool activity mid-cycle is not
  implemented, because per-step reactions would fight the activity messages for the same message.
  Telegram stores one reaction per chat on a message, so with `minimal` the bot's terminal emoji
  replaces an emoji the agent put on that same message through `telegram_set_message_reaction`.

- `--workspace`
  Default: `acp.workspace` in the config file.
  Default workspace used by `/new` when no workspace path is provided.

- `--permission-mode`
  Default: `acp.permission_mode` in the config file.
  Allowed values: `ask`, `approve`, `deny`.

- `--permission-event-output`
  Default: `acp.permission_event_output` in the config file.
  Allowed values: `stdout`, `off`.

- `--acp-stdio-limit`
  Default: `acp.stdio_limit` in the config file.
  Asyncio stdio reader limit in bytes.

- `--acp-connect-timeout`
  Default: `acp.connect_timeout` in the config file.
  Timeout in seconds for ACP `initialize` + `new_session` handshake.

- `-m`, `--activity-mode`
  Default: `acp.activity_mode` in the config file.
  Allowed values: `normal`, `compact`, `verbose`.
  In `normal`, the bot sends activity/tool updates as separate messages without
  streaming edits.
  In `compact`, the bot keeps a single in-progress reply message per prompt,
  updates that message in place, and keeps the normal activity emoji in the
  message text while work is in progress. The status line also names the action
  in progress (the command being run, or the file being touched relative to the
  workspace), on one row. Follow-up updates are applied at most once every few
  seconds, so a burst of tool activity stays inside Telegram's per-chat message
  limit.
  In `verbose`, the bot streams append-only updates in place for active reply
  text and tool activity, then finalizes the same message when possible.

- `-V`, `--version`
  Print CLI version and exit.

## Notes

- `/restart` behavior:
  - If `acp.restart_command` (or `--restart-command`) is set, that command is executed.
  - Otherwise, the bot re-execs itself using `sys.executable + sys.argv`.
  - It requires an active session and reports session context (`session_id`, `workspace`) in the response.
  - `/restart N [workspace]` selects and loads a resumable session in-process (no process relaunch) and replies with a resume acknowledgment instead of a relaunch message.
- Access control behavior:
  - Configure at least one allowlist entry via `--allowed-user-id`, `--allowed-username`,
    `telegram.allowed_user_ids`, or `telegram.allowed_usernames`.
- MCP behavior:
  - `telegram-acp-bot` always advertises an internal MCP stdio server (`telegram-channel`) to the ACP agent.
- Multi-agent behavior (see {ref}`config-agents`):
  - `/restart` relaunches the process, so every agent in the fleet restarts, not only the bot that was asked.
  - One agent failing to start stops all of them and exits non-zero, leaving the whole fleet to the service manager.
  - `--agent`/`--only` never register Telegram commands; run `register-commands` for that.

## register-commands

Register (or delete) the bot's slash commands in Telegram via `setMyCommands`.

```bash
telegram-acp-bot register-commands --telegram-token <TOKEN> [options]
```

### Options

- `--telegram-token`
  Default: `telegram.bot_token` in the config file.
  Required if that key is not set.
  Ignored for agents the config file declares, since each of those resolves its own token.

- `--bot`
  Name of one declared agent to register for.
  Without it, a config file that declares agents registers every one of them in turn, and a flat
  config registers its single bot. Naming an agent nothing declares exits with code `2`.

- `--scope`
  BotCommandScope to target.
  Allowed values: `default`, `all_private_chats`, `all_group_chats`, `all_chat_administrators`.
  Default: `default` (global scope for all users).

- `--language-code`
  IETF language code for language-specific registration (e.g. `en`, `es`).
  Omit to register for all languages.

- `--dry-run`
  Print the commands that would be registered without calling the Telegram API.

- `--delete`
  Delete registered commands for the given scope/language instead of setting them
  (`deleteMyCommands`). Useful for cleanup flows.

### Examples

Register commands for all users (default scope):

```bash
telegram-acp-bot register-commands --telegram-token <TOKEN>
```

Preview what would be registered without calling the API:

```bash
telegram-acp-bot register-commands --telegram-token <TOKEN> --dry-run
```

Register commands only for private chats in English:

```bash
telegram-acp-bot register-commands \
  --telegram-token <TOKEN> \
  --scope all_private_chats \
  --language-code en
```

Delete all registered commands for the default scope:

```bash
telegram-acp-bot register-commands --telegram-token <TOKEN> --delete
```

Register every agent of a fleet, each with its own token, and preview it first:

```bash
telegram-acp-bot --config ./telegram-acp-bot.json register-commands --dry-run
telegram-acp-bot --config ./telegram-acp-bot.json register-commands
```

Register one agent of that fleet:

```bash
telegram-acp-bot --config ./telegram-acp-bot.json register-commands --bot qoder
```

### Notes

- Command definitions are sourced from the codebase constant
  `telegram_acp_bot.telegram.bot.BOT_COMMANDS`.  Running `register-commands` is idempotent —
  it is safe to call multiple times.
- Exit code `0` on success; `1` on Telegram API error; `2` on a missing token or an unknown `--bot`.
- A fleet is registered bot by bot: one refused bot returns `1` after the others were still
  registered, and every line names the bot it belongs to (`[qoder] Registered …`). A flat config
  has one bot, so its lines stay unprefixed.
- A declared agent with no token anywhere is reported as a missing
  `agents.<name>.telegram.bot_token` before any API call, so a half-configured fleet never
  registers anything.
- Transport logs stay quiet on this path. The request URL that `httpx` prints at INFO is the bot
  token, and this sub-command never reaches the bot's own logging setup, so it lowers `httpx`,
  `httpcore`, `telegram.ext` and `apscheduler` to `WARNING` before calling the API. Only the
  confirmation line is printed.
