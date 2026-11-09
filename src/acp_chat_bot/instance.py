"""The resolved description of one agent running in this process."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acp.schema import EnvVariable, HttpHeader, HttpMcpServer, McpServerStdio

from acp_chat_bot.acp.models import ActivityMode, PermissionEventOutput, PermissionMode
from acp_chat_bot.acp.service import AcpAgentService
from acp_chat_bot.channels.dingtalk.api import DingTalkAPI
from acp_chat_bot.channels.dingtalk.channel import DingTalkChannel
from acp_chat_bot.channels.dingtalk.config import DingTalkConfig
from acp_chat_bot.channels.dingtalk.runtime import DingTalkRuntime
from acp_chat_bot.channels.telegram.bridge import TelegramBridge
from acp_chat_bot.channels.telegram.config import BotConfig, StatusReactionLevel, make_config
from acp_chat_bot.core.bridge import AgentChatBridge
from acp_chat_bot.core.config import ChannelKind, ChatConfig
from acp_chat_bot.core.runtime import ChannelRuntime
from acp_chat_bot.core.session_registry import SessionRegistry
from acp_chat_bot.mcp.state import (
    AGENT_NAME_ENV,
    API_HASH_ENV,
    API_ID_ENV,
    CHANNEL_KIND_ENV,
    DINGTALK_CLIENT_ID_ENV,
    DINGTALK_CLIENT_SECRET_ENV,
    DINGTALK_ROBOT_CODE_ENV,
    STATE_FILE_ENV,
    TOKEN_ENV,
)
from acp_chat_bot.mcp.tools.attachments import ALLOW_PATH_ENV
from acp_chat_bot.scheduled_tasks import (
    ACP_SCHEDULED_TASKS_DB_ENV,
    ScheduledTaskRunner,
    ScheduledTaskScheduler,
    ScheduledTaskStore,
)


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
    channel: ChannelKind = "telegram"
    dingtalk_client_id: str = ""
    dingtalk_client_secret: str = ""
    dingtalk_robot_code: str = ""
    allowed_staff_ids: tuple[str, ...] = ()
    allowed_dingtalk_nicknames: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class BotInstance:
    """One fully wired bot: transport, agent service, scheduler and stop signal.

    Built by {py:func}`build_bot_instance` and driven by
    {py:func}`acp_chat_bot.runner.run_instances`.
    """

    spec: BotInstanceSpec
    config: BotConfig | DingTalkConfig
    service: AcpAgentService
    store: ScheduledTaskStore
    bridge: TelegramBridge | AgentChatBridge
    scheduler: ScheduledTaskScheduler
    stop_event: asyncio.Event
    runtime: ChannelRuntime | None = None


def build_channel_env(  # noqa: PLR0913
    *,
    telegram_token: str = "",
    state_file: Path,
    scheduled_tasks_db: Path,
    allow_path_inputs: bool,
    agent_name: str,
    mtproto_api_id: int | None = None,
    mtproto_api_hash: str = "",
    channel: ChannelKind = "telegram",
    dingtalk_client_id: str = "",
    dingtalk_client_secret: str = "",
    dingtalk_robot_code: str = "",
) -> dict[str, str]:
    """Return the internal env one instance hands to its channel MCP server.

    This is the single source of truth for the bot-to-child plumbing: the same mapping is
    injected as the declared ``mcpServers`` env and layered over the ACP subprocess spawn
    env, so agent2's server can never read agent1's token, state file or media directory.
    """
    env = {
        STATE_FILE_ENV: str(state_file),
        ACP_SCHEDULED_TASKS_DB_ENV: str(scheduled_tasks_db),
        ALLOW_PATH_ENV: "1" if allow_path_inputs else "0",
        AGENT_NAME_ENV: agent_name,
    }
    if channel == "dingtalk":
        env |= {
            TOKEN_ENV: dingtalk_client_id,
            CHANNEL_KIND_ENV: "dingtalk",
            DINGTALK_CLIENT_ID_ENV: dingtalk_client_id,
            DINGTALK_CLIENT_SECRET_ENV: dingtalk_client_secret,
            DINGTALK_ROBOT_CODE_ENV: dingtalk_robot_code or dingtalk_client_id,
        }
        return env
    env[TOKEN_ENV] = telegram_token
    if mtproto_api_id is not None and mtproto_api_hash.strip():
        env[API_ID_ENV] = str(mtproto_api_id)
        env[API_HASH_ENV] = mtproto_api_hash.strip()
    return env


def build_mcp_servers(  # noqa: PLR0913
    *,
    telegram_token: str = "",
    state_file: Path,
    scheduled_tasks_db: Path,
    allow_path_inputs: bool,
    agent_name: str,
    extra_servers: Mapping[str, Any] | None = None,
    mtproto_api_id: int | None = None,
    mtproto_api_hash: str = "",
    channel: ChannelKind = "telegram",
    dingtalk_client_id: str = "",
    dingtalk_client_secret: str = "",
    dingtalk_robot_code: str = "",
) -> tuple[McpServerStdio | HttpMcpServer, ...]:
    """Return MCP servers to expose to the ACP agent.

    Always includes the internal channel server, pointed at this instance's own
    credentials, state file and scheduled-tasks database so two agents never read
    each other's chat state.
    """
    channel_env = build_channel_env(
        telegram_token=telegram_token,
        state_file=state_file,
        scheduled_tasks_db=scheduled_tasks_db,
        allow_path_inputs=allow_path_inputs,
        agent_name=agent_name,
        mtproto_api_id=mtproto_api_id,
        mtproto_api_hash=mtproto_api_hash,
        channel=channel,
        dingtalk_client_id=dingtalk_client_id,
        dingtalk_client_secret=dingtalk_client_secret,
        dingtalk_robot_code=dingtalk_robot_code,
    )
    server_name = "dingtalk-channel" if channel == "dingtalk" else "telegram-channel"
    server_module = "acp_chat_bot.mcp.server_dingtalk" if channel == "dingtalk" else "acp_chat_bot.mcp.server"
    internal = McpServerStdio(
        name=server_name,
        command=sys.executable,
        args=["-m", server_module],
        env=[EnvVariable(name=k, value=v) for k, v in channel_env.items()],
    )
    servers: dict[str, McpServerStdio | HttpMcpServer] = {server_name: internal}
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
    if spec.channel == "dingtalk":
        return _build_dingtalk_instance(spec, stop_event=stop_event)
    return _build_telegram_instance(spec, stop_event=stop_event)


def _build_service(spec: BotInstanceSpec) -> AcpAgentService:
    mcp_servers = build_mcp_servers(
        telegram_token=spec.token,
        state_file=spec.channel_state_file,
        scheduled_tasks_db=spec.scheduled_tasks_db,
        allow_path_inputs=spec.allow_path_inputs,
        agent_name=spec.name,
        extra_servers=spec.extra_mcp_servers,
        mtproto_api_id=spec.mtproto_api_id,
        mtproto_api_hash=spec.mtproto_api_hash,
        channel=spec.channel,
        dingtalk_client_id=spec.dingtalk_client_id,
        dingtalk_client_secret=spec.dingtalk_client_secret,
        dingtalk_robot_code=spec.dingtalk_robot_code,
    )
    spawn_env = build_channel_env(
        telegram_token=spec.token,
        state_file=spec.channel_state_file,
        scheduled_tasks_db=spec.scheduled_tasks_db,
        allow_path_inputs=spec.allow_path_inputs,
        agent_name=spec.name,
        mtproto_api_id=spec.mtproto_api_id,
        mtproto_api_hash=spec.mtproto_api_hash,
        channel=spec.channel,
        dingtalk_client_id=spec.dingtalk_client_id,
        dingtalk_client_secret=spec.dingtalk_client_secret,
        dingtalk_robot_code=spec.dingtalk_robot_code,
    )
    return AcpAgentService(
        SessionRegistry(),
        program=spec.command_parts[0],
        args=list(spec.command_parts[1:]),
        default_permission_mode=spec.permission_mode,
        permission_event_output=spec.permission_event_output,
        mcp_servers=mcp_servers,
        channel_state_file=spec.channel_state_file,
        spawn_env=spawn_env,
        stdio_limit=spec.stdio_limit,
        connect_timeout=spec.connect_timeout,
    )


def _build_telegram_instance(spec: BotInstanceSpec, *, stop_event: asyncio.Event) -> BotInstance:
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
    service = _build_service(spec)
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


def _build_dingtalk_instance(spec: BotInstanceSpec, *, stop_event: asyncio.Event) -> BotInstance:
    workspace = Path(spec.workspace).expanduser()
    config = DingTalkConfig(
        client_id=spec.dingtalk_client_id,
        client_secret=spec.dingtalk_client_secret,
        robot_code=spec.dingtalk_robot_code or spec.dingtalk_client_id,
        allowed_staff_ids=frozenset(spec.allowed_staff_ids),
        allowed_nicknames=frozenset(spec.allowed_dingtalk_nicknames),
        default_workspace=workspace,
        activity_mode=spec.activity_mode,
        group_require_mention=spec.group_require_mention,
        group_history_size=spec.group_history_size,
    )
    chat_config = ChatConfig(
        default_workspace=workspace,
        activity_mode=spec.activity_mode,
        schedule_languages=spec.schedule_languages,
        group_require_mention=spec.group_require_mention,
        group_history_size=spec.group_history_size,
    )
    service = _build_service(spec)
    store = ScheduledTaskStore(spec.scheduled_tasks_db)
    api = DingTalkAPI(
        client_id=spec.dingtalk_client_id,
        client_secret=spec.dingtalk_client_secret,
        robot_code=spec.dingtalk_robot_code or spec.dingtalk_client_id,
    )
    channel = DingTalkChannel(config=config, api=api, state_file=spec.channel_state_file)
    bridge = AgentChatBridge(
        channel=channel,
        agent_service=service,
        config=chat_config,
        scheduled_task_store=store,
        stop_request=stop_event.set,
        agent_name=spec.name,
    )
    bridge.install()
    scheduler = ScheduledTaskScheduler(store=store, runner=ScheduledTaskRunner(bridge.execute_scheduled_task))
    runtime = DingTalkRuntime(config=config, bridge=bridge, channel=channel, api=api)
    return BotInstance(
        spec=spec,
        config=config,
        service=service,
        store=store,
        bridge=bridge,
        scheduler=scheduler,
        stop_event=stop_event,
        runtime=runtime,
    )
