# Architecture

The project is a platform-neutral chat core with one adapter per channel. Each agent runs exactly
one channel; one process can host several agents, each with its own channel, ACP child process,
state file and scheduler database.

```{mermaid}
flowchart LR
  subgraph Channels
    TG[Telegram adapter]
    DT[DingTalk adapter]
  end

  subgraph Core
    BR[AgentChatBridge<br/>commands, sessions, queue, choices]
    SVC[ACP agent service]
    SCH[Scheduled task scheduler]
  end

  TG <--> BR
  DT <--> BR
  BR <--> SVC
  BR <--> SCH
  SVC <--> AGENT[ACP agent process]
```

## Layers

- `acp_chat_bot.core` holds the transport-neutral pieces:
  - `channel.py` defines the `Channel` protocol and its capabilities.
  - `chat.py` defines inbound messages, attachments and choice prompts.
  - `bridge.py` implements commands, sessions, the busy queue, permissions, elicitation and
    scheduled execution once for every channel.
  - `runtime.py` defines the `ChannelRuntime` lifecycle used by the runner.
- `acp_chat_bot.channels.telegram` implements the protocol over the Telegram Bot API, including
  rich messages, inline keyboards, reactions and the Telegram-specific activity presenters.
- `acp_chat_bot.channels.dingtalk` implements the protocol over DingTalk Stream mode and the
  DingTalk OpenAPI, including proactive sends and media upload/download.
- `acp_chat_bot.mcp` exposes the internal MCP server. Telegram gets `telegram-channel` and
  DingTalk gets `dingtalk-channel`; both share the scheduling tools.

## Identity

Chat, message and user IDs are opaque `int | str` aliases in `core/ids.py`. Telegram keeps its
integer IDs and stringifies nothing; DingTalk uses its native string IDs. Because an agent only
ever talks to one channel, the two ID spaces never mix for the same agent. The SQLite scheduler
and JSON channel state accept both forms.

## Runtime lifecycle

`runner.py` only knows `ChannelRuntime`. The Telegram runtime wraps `telegram.ext.Application`;
the DingTalk runtime drives the Stream client in an asyncio task. Both expose `start()` and
`stop()`, and both stop when the shared process stop event fires.
