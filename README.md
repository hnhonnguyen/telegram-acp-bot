# ACP chat bot

[![CI](https://github.com/mgaitan/telegram-acp-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/mgaitan/telegram-acp-bot/actions/workflows/ci.yml)
[![docs](https://img.shields.io/badge/docs-blue.svg?style=flat)](https://mgaitan.github.io/telegram-acp-bot/)
[![PyPI version](https://img.shields.io/pypi/v/acp-chat-bot?logo=pypi&logoColor=white)](https://pypi.org/project/acp-chat-bot/)
[![Changelog](https://img.shields.io/github/v/release/mgaitan/telegram-acp-bot?include_prereleases&label=changelog)](https://github.com/mgaitan/telegram-acp-bot/releases)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/mgaitan/telegram-acp-bot/actions/workflows/ci.yml)
[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/mgaitan/telegram-acp-bot/blob/main/LICENSE)

A chat bot that implements the [Agent Client Protocol](https://agentclientprotocol.com/) to interact with AI agents over **Telegram** or **DingTalk**. Each agent runs one channel, and one process can host several agents on different channels.

Project documentation: <https://mgaitan.github.io/telegram-acp-bot/>

## Status

This project is in **alpha** and under active development. Development has included extensive use of AI agents. Human and agents contributions are welcome.

## Features

Some of the most useful capabilities are documented here:

- Reply with attachments:
  https://mgaitan.github.io/telegram-acp-bot/mcp.html#mcp-feature-attachments
- React to messages (Telegram):
  https://mgaitan.github.io/telegram-acp-bot/mcp.html#mcp-feature-reactions
- Scheduled tasks:
  https://mgaitan.github.io/telegram-acp-bot/deferred_followups.html#deferred-followups-ux
- Queue messages when the agent is busy:
  https://mgaitan.github.io/telegram-acp-bot/how-to.html#how-to-busy-queue
- Run the same bot on DingTalk:
  https://mgaitan.github.io/telegram-acp-bot/dingtalk.html
- Internal MCP tools overview:
  https://mgaitan.github.io/telegram-acp-bot/mcp.html

## Quick Start

Run directly without installing via `uvx`:

```bash
uvx --from acp-chat-bot acp-chat-bot --help
```

Run the latest development version from git:

```bash
uvx git+https://github.com/mgaitan/telegram-acp-bot --help
```

Run the bot with a real ACP agent on Telegram:

```bash
acp-chat-bot \
  --telegram-token 123456:abc \
  --allowed-user-id 123456789 \
  --agent-command "npx @zed-industries/codex-acp"
```

Run it on DingTalk instead:

```bash
acp-chat-bot \
  --channel dingtalk \
  --dingtalk-client-id dingxxxxxxxx \
  --dingtalk-client-secret secret \
  --allowed-staff-id 0123456789 \
  --agent-command "npx @zed-industries/codex-acp"
```

Current interaction capabilities:
- `/new [workspace]`, `/resume [N|workspace]`, `/session`, `/cancel`, `/stop`, `/clear`, `/restart [N [workspace]]`
- `/mode [normal|compact|verbose]` to switch the activity display per chat
- `/cmds` for tap-to-run buttons of the slash commands the agent advertises, which are then forwarded to it unchanged
- Interactive permission prompts with inline buttons on Telegram; numbered replies on DingTalk
- Agent questions asked over ACP elicitation, including multi-select answers and a skip option
- Plain text prompts
- Image and document attachments from chat messages
- ACP `file://` resources are sent as attachments when they resolve to files inside the active workspace
- Agent markdown output (with chunking and a plain-text fallback when a channel rejects the markup)

Message flow:
- The bot sends activity blocks while the prompt is running.
- Activity display modes:
  - `normal`: separate activity messages, no streaming edits.
  - `compact`: one in-progress status message that becomes the final answer.
  - `verbose`: append-only in-place streaming for active reply text and tool activity.
- Common labels are `💡 Thinking`, `⚙️ Running`, `📖 Reading`, `✏️ Editing`, `✍️ Writing`, `🌐 Searching web`, and `🔎 Querying`.
- Permission prompts for risky actions are sent as independent messages with inline buttons.
- In `normal`, the final answer is sent as a separate message after activity blocks.
- If the final text is empty, no dummy "(no text response)" message is sent.

For development, `/restart` stops polling and relaunches the process.
If `ACP_RESTART_COMMAND` (or `--restart-command`) is configured, that command is used (recommended when running with `uv run ...` and extra flags).
Otherwise, it falls back to re-execing the current process (`sys.executable + sys.argv`).

## Telegram bot token

Create your token with [@BotFather](https://t.me/BotFather):

1. Open BotFather and run `/newbot`.
2. Choose a bot name and username.
3. Copy the token returned by BotFather.

Keep the token and the allowlist in a JSON config file (gitignored, `chmod 600`):

```json
{
  "channel": "telegram",
  "telegram": {
    "bot_token": "123456:abc",
    "allowed_user_ids": [123456789]
  },
  "acp": {
    "agent_command": "npx @zed-industries/codex-acp"
  }
}
```

Then run:

```bash
acp-chat-bot --config ./acp-chat-bot.json
```

At least one Telegram allowlist entry (`allowed_user_ids` or `allowed_usernames`) is required.
For DingTalk, set `"channel": "dingtalk"` and configure `dingtalk.client_id`,
`dingtalk.client_secret` and an `allowed_staff_ids`/`allowed_nicknames` entry; see
[docs/dingtalk.md](docs/dingtalk.md).

## Agent command

Set `acp.agent_command` (or `--agent-command`) to the ACP-compatible agent command you want the
bot to run.

Example:

```json
{ "acp": { "agent_command": "npx @zed-industries/codex-acp" } }
```

To install the tool permanently:

```bash
uv tool install acp-chat-bot
```

## Development

- Install dependencies with `uv sync`.
- Then run `uv run acp-chat-bot`
- New dependency releases are delayed by one week via `uv` cooldown (`[tool.uv].exclude-newer = "1 week"`), with per-package overrides when required (for example, `ty`).
- Run the QA bundle with [`ty`](https://github.com/astral-sh/ty):

```bash
uv run ty check
```
