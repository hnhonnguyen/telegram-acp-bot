# Agent Setup

This chapter explains how to configure common ACP agents with this bot.

Use `acp.agent_command` in the config file — or the `--agent-command` flag — to define the
command executed by `/new`. The agent's own credentials (an API key, for example) are still
passed as environment variables when you launch the bot: the bot forwards its environment to the
ACP subprocess, and that subprocess — not the bot — reads them.

You do not have to pick just one: declare several and the process runs one bot per agent. See
{ref}`config-agents`.

## Quick compatibility map

| Agent | ACP support | Recommended `--agent-command` value |
| --- | --- | --- |
| [Gemini CLI](https://google-gemini.github.io/gemini-cli/) | Native ACP support (experimental flag required) | `npx @google/gemini-cli --experimental-acp` |
| GitHub Copilot CLI | Native ACP support (`--acp`) | `copilot --acp` |
| [Codex CLI](https://developers.openai.com/codex/cli) | Via ACP adapter | `npx @zed-industries/codex-acp` |
| [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview) / [Claude Code](https://code.claude.com/docs/en/cli-reference) | Via ACP adapter | `npx @zed-industries/claude-agent-acp` |
| Qoder CLI | Native ACP support (`--acp`); advertises its slash commands over `available_commands_update`, proxies its multiple-choice questions as form elicitations, and answers commands that need its own terminal itself | `qodercli --acp` |

## Gemini CLI (native ACP)

1. Install and authenticate Gemini CLI first.
2. Set `GEMINI_API_KEY` (or another auth method supported by Gemini CLI).
3. Start the bot with Gemini as agent:

```bash
GEMINI_API_KEY=... \
uv run telegram-acp-bot \
  --agent-command "npx @google/gemini-cli --experimental-acp" \
  --telegram-token <TOKEN> --allowed-user-id <YOUR_ID>
```

If `/new` does not return, set a lower `--acp-connect-timeout` and `acp.log_level` to `DEBUG` in
the config file to see where handshake stops.
See also [Gemini CLI repository](https://github.com/google-gemini/gemini-cli).

## Codex CLI (via adapter)

[Codex CLI](https://github.com/openai/codex) is an OpenAI coding agent. For ACP clients, use the [Codex ACP adapter](https://github.com/zed-industries/codex-acp).

```bash
OPENAI_API_KEY=... \
uv run telegram-acp-bot \
  --agent-command "npx @zed-industries/codex-acp" \
  --telegram-token <TOKEN> --allowed-user-id <YOUR_ID>
```

You can also install the adapter binary from [adapter releases](https://github.com/zed-industries/codex-acp/releases) and use `codex-acp` directly.

### Codex profiles, sandbox, and behavior differences

When using Codex through Zed, behavior can differ from running `npx @zed-industries/codex-acp` directly in this bot.
Zed may apply profile/session settings (for example, default profile and tool permissions) that result in a different
effective sandbox/approval mode than a plain adapter command.

If you need deterministic behavior in Telegram, pass explicit Codex config overrides in `--agent-command`:

```bash
OPENAI_API_KEY=... \
uv run telegram-acp-bot \
  --agent-command 'npx @zed-industries/codex-acp -c sandbox_mode="danger-full-access" -c approval_policy="never"' \
  --telegram-token <TOKEN> --allowed-user-id <YOUR_ID>
```

Safer alternative:

```bash
OPENAI_API_KEY=... \
uv run telegram-acp-bot \
  --agent-command 'npx @zed-industries/codex-acp -c sandbox_mode="workspace-write" -c approval_policy="on-request"' \
  --telegram-token <TOKEN> --allowed-user-id <YOUR_ID>
```

If a tool works in Zed but fails in Telegram with permission-like runtime errors, check the active runtime policy inside
the agent session (for example `NoNewPrivs`/`Seccomp`) and compare against your Zed session.

## GitHub Copilot CLI (native ACP)

If you already have Copilot CLI installed and authenticated, run:

```bash
uv run telegram-acp-bot \
  --agent-command "copilot --acp" \
  --telegram-token <TOKEN> --allowed-user-id <YOUR_ID>
```

## Claude Agent / Claude Code (via adapter)

Claude integration for ACP clients is provided by [`@zed-industries/claude-agent-acp`](https://github.com/zed-industries/claude-agent-acp).

```bash
ANTHROPIC_API_KEY=... \
uv run telegram-acp-bot \
  --agent-command "npx @zed-industries/claude-agent-acp" \
  --telegram-token <TOKEN> --allowed-user-id <YOUR_ID>
```

If you prefer global install:

```bash
npm install -g @zed-industries/claude-agent-acp
ANTHROPIC_API_KEY=... uv run telegram-acp-bot \
  --agent-command "claude-agent-acp" \
  --telegram-token <TOKEN> --allowed-user-id <YOUR_ID>
```

## Other agents

For other ACP-compatible agents, check:

- [ACP agents list](https://agentclientprotocol.com/get-started/agents)
- [ACP registry](https://agentclientprotocol.com/registry/index)

When trying a new agent, start with:

1. `acp.log_level` set to `DEBUG` in the config file
2. A short `--acp-connect-timeout` (for example `15`)
3. A command that is known to run non-interactively on stdio
