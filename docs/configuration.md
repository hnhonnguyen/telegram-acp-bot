# Configuration

telegram-acp-bot reads its settings from exactly two places. Values are resolved in this order:

```
CLI flags  >  Config file  >  Built-in defaults
```

There is no environment-variable layer for user settings: every token, allowlist, command and
tuning knob lives in the config file or is passed as a flag. This keeps a fleet of agents from
colliding through one inherited shell variable and makes a deployment reproducible from a single
JSON file.

(config-file)=
## Config file

The bot loads configuration from the following locations (in order):

1. `--config PATH`
2. `./.telegram_acp_bot/config.json` (project-local)
3. `{envvar}`XDG_CONFIG_HOME/telegram_acp_bot/config.json` (or `~/.config/telegram_acp_bot/config.json` when unset)
4. `~/.telegram_acp_bot/config.json` (legacy)

The first readable file wins. A path named with `--config` is used as given: when it cannot be
read, the bot stops with that path in the message instead of quietly falling back to discovery.

```bash
# Automatic discovery (no --config needed if the file is in a standard location)
telegram-acp-bot

# Explicit path (overrides automatic discovery)
telegram-acp-bot --config ./telegram-acp-bot.json

# Sub-commands can be pointed at the same file
telegram-acp-bot --config ./telegram-acp-bot.json register-commands --dry-run
```

The file must be a JSON object. All keys are optional. Missing keys fall back to built-in defaults.

```{note}
{envvar}`XDG_CONFIG_HOME` and {envvar}`XDG_STATE_HOME` are the only environment variables the bot
reads, and only to locate its config and state directories — never to supply a setting value. They
follow the XDG Base Directory specification like any other Linux program.
```

### Schema

```json
{
  "telegram": {
    "bot_token": "123456:abc",
    "api_id": 12345,
    "api_hash": "0123456789abcdef0123456789abcdef",
    "allowed_user_ids": [123456789],
    "allowed_usernames": ["alice", "@bob"],
    "schedule_languages": ["en", "es"],
    "group_require_mention": true,
    "group_media_queue": true,
    "group_history_size": 200,
    "status_reactions": "minimal"
  },
  "acp": {
    "agent_command": "npx @zed-industries/codex-acp",
    "restart_command": "uv run telegram-acp-bot --config ./telegram-acp-bot.json",
    "permission_mode": "ask",
    "permission_event_output": "stdout",
    "stdio_limit": 8388608,
    "connect_timeout": 30,
    "log_format": "text",
    "log_level": "INFO",
    "activity_mode": "normal",
    "channel_state_file": "/home/alice/.local/state/telegram-acp-bot/channel-state.json",
    "allow_path_inputs": false,
    "scheduled_tasks_db": "/home/alice/.local/state/telegram-acp-bot/scheduled-tasks.sqlite3",
    "workspace": "/home/alice/projects"
  },
  "agents": {
    "qoder": {
      "acp": {
        "agent_command": "qodercli --acp"
      }
    },
    "codex": {
      "telegram": {
        "bot_token": "987654:xyz",
        "allowed_user_ids": [987654321]
      },
      "acp": {
        "agent_command": "npx @zed-industries/codex-acp",
        "channel_state_file": "/srv/codex/channel-state.json"
      },
      "mcp_servers": {
        "context7": {
          "url": "https://mcp.context7.com/mcp"
        }
      }
    }
  }
}
```

Without `agents`, the flat `telegram`/`acp` sections describe one bot, which is all most
setups need. With `agents`, the process runs one bot per entry; see {ref}`config-agents`.

### Validation

The config file is validated at startup. Invalid values (wrong types, unknown enum values) are reported as clear error messages and the bot does not start.

(config-agents)=
## Multiple agents

One process can run several agents side by side: each declared agent gets its own ACP
child process and its own Telegram bot, all polled from a single event loop. Everything
below describes that fleet; a config without `agents` runs exactly one bot named
`default` and behaves as it always did.

### Names

An agent name must match `^[a-z0-9][a-z0-9_-]{0,62}$`, because the same string is a log
field and a directory name. A name is the folder that holds that agent's routing state and
scheduled-tasks database, so the shape is validated once rather than at each use site.

### What an agent may override

Each `agents.<name>` entry may only repeat the top-level sections `telegram`, `acp` and
`mcp_servers`, so one syntax describes a single bot and a fleet alike. The flat sections
are the base and each agent layers its own keys on top, key by key. `mcp_servers` is
merged by server name and the agent's definition wins, so one agent can swap a shared
server without changing it for the others.

### Tokens

Every bot needs a distinct Telegram token, resolved in this order:

1. `agents.<name>.telegram.bot_token` in the config file.
2. The flat base token (`--telegram-token` or `telegram.bot_token`), which an agent
   inherits only when its own section sets nothing.

Startup refuses two agents that end up on the same token and names both, because Telegram
answers concurrent polls of one bot with error 409 — the fleet would look alive and lose
updates. The flat sections still need a value, because they are what an unset key falls back
to: the base carries `telegram.bot_token` and `acp.agent_command` alongside the per-agent
keys that make each bot distinct. An agent that ends up on the base token is reported by the
same duplicate-token check.

### Declaring agents on the command line

- `--agent NAME=COMMAND` declares an agent, or overrides the command of a declared one,
  without editing the config file. It is repeatable. A name that only appears here still
  needs a token from one of the sources above.
- `--only NAME` runs a subset of the declared agents. It is repeatable and refuses a name
  that nothing declares. The config file offers the same list under a top-level `"only"`
  key, which the flag replaces.

### Runtime files

Two bots must not route through one file, so a named agent keeps its channel state and its
scheduled-tasks database in a directory of its own under {envvar}`XDG_STATE_HOME`
(`~/.local/state/telegram-acp-bot/agents/<name>/` by default). Each agent also gets its own
scheduler and its own SQLite database, which is what stops one task from being claimed
twice. Pin either path per agent by setting `agents.<name>.acp.channel_state_file` or
`agents.<name>.acp.scheduled_tasks_db`; the attachment media directory is namespaced by agent
name automatically, so two agents never write to the same `.telegram-media` folder.

### Failure and restart

The fleet is all-or-nothing on purpose. If any agent fails to start, or fails to shut
down cleanly, the process stops every other bot and exits non-zero, so the service
manager restarts the whole thing. A half-alive fleet would leave one chat silently
answering nothing. `/restart` follows the same rule: it relaunches the process, and every
agent in it.

```{warning}
`/restart` re-execs the recorded restart command. Because no setting is read from the
environment anymore, that command must point at the same config file (`--config ...`) or a
standard discovery location, or the relaunched process comes back with only built-in
defaults.
```

While it runs, every log line an instance produces carries an `agent` field, and `/help`
names the agent behind the bot whenever it is not `default`, so two bots in one process stay
tellable apart. To run two agents, follow {ref}`how-to-two-agents`.

## CLI flags

CLI flags take the highest priority and override the config file. Run `telegram-acp-bot --help` for the full list.

Key flags and their config file equivalents:

| CLI flag | Config key |
|---|---|
| `--config` | — (selects the file) |
| `--telegram-token` | `telegram.bot_token` |
| `--telegram-api-id` | `telegram.api_id` |
| `--telegram-api-hash` | `telegram.api_hash` |
| `--allowed-user-id` | `telegram.allowed_user_ids` |
| `--allowed-username` | `telegram.allowed_usernames` |
| `--schedule-language` | `telegram.schedule_languages` |
| `--group-require-mention` | `telegram.group_require_mention` |
| `--group-media-queue` | `telegram.group_media_queue` |
| `--group-history-size` | `telegram.group_history_size` |
| `--status-reactions` | `telegram.status_reactions` |
| `--agent-command` | `acp.agent_command` |
| `--agent NAME=COMMAND` | `agents` |
| `--only NAME` | `only` |
| `--restart-command` | `acp.restart_command` |
| `--permission-mode` | `acp.permission_mode` |
| `--permission-event-output` | `acp.permission_event_output` |
| `--acp-stdio-limit` | `acp.stdio_limit` |
| `--acp-connect-timeout` | `acp.connect_timeout` |
| `--log-format` | `acp.log_format` |
| `--activity-mode` / `-m` | `acp.activity_mode` |
| `--scheduled-tasks-db` | `acp.scheduled_tasks_db` |
| `--workspace` | `acp.workspace` |

## Migration guide

### From `.env` to `config.json`

The `.env` file and the `TELEGRAM_*` / `ACP_*` environment variables are no longer read. Move
every value into the config file (or pass it as a flag). If you currently use a `.env` file:

```ini
TELEGRAM_BOT_TOKEN=123456:abc
TELEGRAM_ALLOWED_USER_IDS=123456789
ACP_AGENT_COMMAND=npx @zed-industries/codex-acp
ACP_PERMISSION_MODE=approve
ACP_CONFIG_FILE=/home/alice/.acp/config.json
```

The equivalent config file is:

```json
{
  "telegram": {
    "bot_token": "123456:abc",
    "allowed_user_ids": [123456789]
  },
  "acp": {
    "agent_command": "npx @zed-industries/codex-acp",
    "permission_mode": "approve"
  }
}
```

Then run it with an explicit path (there is no `ACP_CONFIG_FILE` anymore):

```bash
telegram-acp-bot --config /home/alice/.acp/config.json
```

```{note}
Because the bot token now lives in the JSON file, protect that file the way you would a
secret: keep it `chmod 600`, outside any shared directory, and out of version control. If you
need the token to stay out of the file, keep it in a flag (`--telegram-token`) on an
interactive shell, or in a service manager's environment for the process launch — the bot
reads the value from there only if you write it into argv yourself; it never looks it up on
its own.
```

## MCP behavior

The bot always advertises an internal MCP stdio server named `telegram-channel`
to the ACP agent. No extra configuration is required.

For adding external MCP servers, see {doc}`mcp`.

(mcp-channel-environment-variables)=
## MCP channel environment variables

The internal `telegram-channel` server runs as a child process, and the ACP/MCP protocol
passes configuration to a child only through its environment. The bot therefore injects a
fixed set of variables into each instance's server and ACP subprocess. **These are plumbing,
not user settings** — you do not set them, and the bot overwrites them per instance so two
agents in one process can never read each other's values.

```{glossary}
ACP_TELEGRAM_BOT_TOKEN
  The token this instance's internal MCP server uses when calling the Telegram Bot API.
  Injected per agent, so each server speaks for its own bot.

ACP_TELEGRAM_API_ID
  The Telegram API id of the app whose credentials enable MTProto file transfers:
  the MCP `telegram_download_file` tool for files beyond the 20 MB Bot API download
  cap, and `telegram_send_attachment` for uploads beyond 50 MB, up to 2 GB each way.
  Injected, together with `ACP_TELEGRAM_API_HASH`, only when both `telegram.api_id`
  and `telegram.api_hash` are set; without the pair the tools answer with a
  configuration error instead of attempting a transfer the Bot API cannot finish.

ACP_TELEGRAM_API_HASH
  The Telegram API hash paired with `ACP_TELEGRAM_API_ID`. Injected under the same
  condition, so the large-file capabilities exist exactly where they were configured.

ACP_TELEGRAM_CHANNEL_STATE_FILE
  Path to the MCP channel shared state file (`session_id -> chat_id`, pending media, last
  active session). Resolved from `acp.channel_state_file` (or the per-agent default
  directory), then injected. The attachment media directory is namespaced by the agent name
  injected below.

ACP_SCHEDULED_TASKS_DB
  Path to the SQLite database this instance's scheduling tool writes to. Resolved from
  `acp.scheduled_tasks_db` (or the per-agent default), then injected.

ACP_TELEGRAM_AGENT_NAME
  The name of the agent owning this server, used to namespace its attachment media directory
  under `.telegram-media/<name>`.

ACP_TELEGRAM_CHANNEL_ALLOW_PATH
  Enables `path` inputs for the MCP `telegram_send_attachment` tool. Set from
  `acp.allow_path_inputs` (default `false`), which only opens the tool to local file paths
  when you deliberately trust the agent to read them.
```

Security notes:

- Keep `acp.allow_path_inputs` unset unless your agent is trusted to read local files.
- The injected token and state file are managed by the bot process and should never be shared
  across unrelated runtimes.
