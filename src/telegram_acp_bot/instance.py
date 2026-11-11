"""The resolved description of one agent running in this process."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acp.schema import EnvVariable, HttpHeader, HttpMcpServer, McpServerStdio

from telegram_acp_bot.acp.models import ActivityMode, PermissionEventOutput, PermissionMode
from telegram_acp_bot.acp.service import AcpAgentService
from telegram_acp_bot.core.session_registry import SessionRegistry
from telegram_acp_bot.mcp.state import AGENT_NAME_ENV, API_HASH_ENV, API_ID_ENV, STATE_FILE_ENV, TOKEN_ENV
from telegram_acp_bot.mcp.tools.attachments import ALLOW_PATH_ENV
from telegram_acp_bot.scheduled_tasks import (
    ACP_SCHEDULED_TASKS_DB_ENV,
    ScheduledTaskRunner,
    ScheduledTaskScheduler,
    ScheduledTaskStore,
)
from telegram_acp_bot.telegram.bridge import TelegramBridge
from telegram_acp_bot.telegram.config import BotConfig, StatusReactionLevel, make_config


@dataclass(slots=True, frozen=True)
class BotInstanceSpec:
    """Everything needed to build one bot, decided before any resource is opened.

    Keeping this frozen and side-effect free makes the multi-agent surface testable
    without starting Telegram or spawning an agent.
    """

    name: str
    token: str
    mtproto_api_id: int | None
    mtproto_api_hash: str
    allowed_user_ids: tuple[int, ...]
    allowed_usernames: tuple[str, ...]
    workspace: str
    activity_mode: ActivityMode
    schedule_languages: tuple[str, ...]
    group_require_mention: bool
    group_media_queue: bool
    group_history_size: int
    status_reactions: StatusReactionLevel
    command_parts: tuple[str, ...]
    permission_mode: PermissionMode
    permission_event_output: PermissionEventOutput
    stdio_limit: int
    connect_timeout: float
    extra_mcp_servers: dict[str, Any]
    channel_state_file: Path
    scheduled_tasks_db: Path
    allow_path_inputs: bool
    restart_command_parts: tuple[str, ...] | None


@dataclass(slots=True, frozen=True)
class BotInstance:
    """One fully wired bot: transport, agent service, scheduler and stop signal.

    Built by {py:func}`build_bot_instance` and driven by
    {py:func}`telegram_acp_bot.runner.run_instances`.
    """

    spec: BotInstanceSpec
    config: BotConfig
    service: AcpAgentService
    store: ScheduledTaskStore
    bridge: TelegramBridge
    scheduler: ScheduledTaskScheduler
    stop_event: asyncio.Event


def build_channel_env(  # noqa: PLR0913
    *,
    telegram_token: str,
    state_file: Path,
    scheduled_tasks_db: Path,
    allow_path_inputs: bool,
    agent_name: str,
    mtproto_api_id: int | None = None,
    mtproto_api_hash: str = "",
) -> dict[str, str]:
    """Return the internal env one instance hands to its ``telegram-channel`` MCP server.

    This is the single source of truth for the bot-to-child plumbing: the same mapping is
    injected as the declared ``mcpServers`` env and layered over the ACP subprocess spawn
    env, so agent2's server can never read agent1's token, state file or media directory.

    The MTProto credentials are optional: they only reach the child when both are set,
    which is what keeps `telegram_download_file` available only where it was configured.
    """
    env = {
        TOKEN_ENV: telegram_token,
        STATE_FILE_ENV: str(state_file),
        ACP_SCHEDULED_TASKS_DB_ENV: str(scheduled_tasks_db),
        ALLOW_PATH_ENV: "1" if allow_path_inputs else "0",
        AGENT_NAME_ENV: agent_name,
    }
    if mtproto_api_id is not None and mtproto_api_hash.strip():
        env[API_ID_ENV] = str(mtproto_api_id)
        env[API_HASH_ENV] = mtproto_api_hash.strip()
    return env


def build_mcp_servers(  # noqa: PLR0913
    *,
    telegram_token: str,
    state_file: Path,
    scheduled_tasks_db: Path,
    allow_path_inputs: bool,
    agent_name: str,
    extra_servers: Mapping[str, Any] | None = None,
    mtproto_api_id: int | None = None,
    mtproto_api_hash: str = "",
) -> tuple[McpServerStdio | HttpMcpServer, ...]:
    """Return MCP servers to expose to the ACP agent.

    Always includes the internal ``telegram-channel`` server, pointed at this
    instance's own token, state file and scheduled-tasks database so two agents
    never read each other's chat state.
    External servers from the config file are added after it, except when an
    external ``telegram-channel`` definition overrides the internal one. In
    that case, the external server replaces the internal entry and keeps its
    position in the resulting tuple.
    """
    channel_env = build_channel_env(
        telegram_token=telegram_token,
        state_file=state_file,
        scheduled_tasks_db=scheduled_tasks_db,
        allow_path_inputs=allow_path_inputs,
        agent_name=agent_name,
        mtproto_api_id=mtproto_api_id,
        mtproto_api_hash=mtproto_api_hash,
    )
    internal = McpServerStdio(
        name="telegram-channel",
        command=sys.executable,
        args=["-m", "telegram_acp_bot.mcp.server"],
        env=[EnvVariable(name=k, value=v) for k, v in channel_env.items()],
    )
    servers: dict[str, McpServerStdio | HttpMcpServer] = {"telegram-channel": internal}
    for name, defn in (extra_servers or {}).items():
        if "command" in defn:
            servers[name] = McpServerStdio(
                name=name,
                command=defn["command"],
                args=defn.get("args", []),
                env=[EnvVariable(name=k, value=v) for k, v in defn.get("env", {}).items()],
            )
        else:
            servers[name] = HttpMcpServer(
                name=name,
                url=defn["url"],
                headers=[HttpHeader(name=k, value=v) for k, v in defn.get("headers", {}).items()],
                type="http",
            )
    return tuple(servers.values())


def build_bot_instance(spec: BotInstanceSpec, *, stop_event: asyncio.Event) -> BotInstance:
    """Open no resource yet, but build everything one bot needs from `spec`.

    Every path in the result is the one this agent owns, which is what lets N agents
    share a process without sharing a channel state file or a scheduler database.
    """
    config = make_config(
        token=spec.token,
        allowed_user_ids=list(spec.allowed_user_ids),
        allowed_usernames=list(spec.allowed_usernames),
        workspace=spec.workspace,
        activity_mode=spec.activity_mode,
        schedule_languages=list(spec.schedule_languages),
        group_require_mention=spec.group_require_mention,
        group_media_queue=spec.group_media_queue,
        status_reactions=spec.status_reactions,
        group_history_size=spec.group_history_size,
    )
    service = AcpAgentService(
        SessionRegistry(),
        program=spec.command_parts[0],
        args=list(spec.command_parts[1:]),
        default_permission_mode=spec.permission_mode,
        permission_event_output=spec.permission_event_output,
        mcp_servers=build_mcp_servers(
            telegram_token=spec.token,
            state_file=spec.channel_state_file,
            scheduled_tasks_db=spec.scheduled_tasks_db,
            allow_path_inputs=spec.allow_path_inputs,
            agent_name=spec.name,
            extra_servers=spec.extra_mcp_servers,
            mtproto_api_id=spec.mtproto_api_id,
            mtproto_api_hash=spec.mtproto_api_hash,
        ),
        channel_state_file=spec.channel_state_file,
        spawn_env=build_channel_env(
            telegram_token=spec.token,
            state_file=spec.channel_state_file,
            scheduled_tasks_db=spec.scheduled_tasks_db,
            allow_path_inputs=spec.allow_path_inputs,
            agent_name=spec.name,
            mtproto_api_id=spec.mtproto_api_id,
            mtproto_api_hash=spec.mtproto_api_hash,
        ),
        stdio_limit=spec.stdio_limit,
        connect_timeout=spec.connect_timeout,
    )
    store = ScheduledTaskStore(spec.scheduled_tasks_db)
    bridge = TelegramBridge(
        config=config,
        agent_service=service,
        scheduled_task_store=store,
        stop_request=stop_event.set,
        agent_name=spec.name,
    )
    scheduler = ScheduledTaskScheduler(store=store, runner=ScheduledTaskRunner(bridge.execute_scheduled_task))
    return BotInstance(
        spec=spec,
        config=config,
        service=service,
        store=store,
        bridge=bridge,
        scheduler=scheduler,
        stop_event=stop_event,
    )
