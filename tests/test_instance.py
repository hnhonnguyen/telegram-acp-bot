"""Tests for wiring a resolved spec into one bot."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import cast

from acp.schema import HttpMcpServer, McpServerStdio

from acp_chat_bot.channels.dingtalk.api import DingTalkAPI
from acp_chat_bot.channels.dingtalk.channel import DingTalkChannel
from acp_chat_bot.channels.dingtalk.config import DingTalkConfig
from acp_chat_bot.channels.dingtalk.runtime import DingTalkRuntime
from acp_chat_bot.channels.telegram.bridge import TelegramBridge
from acp_chat_bot.channels.telegram.config import BotConfig
from acp_chat_bot.core.bridge import AgentChatBridge
from acp_chat_bot.instance import build_bot_instance, build_channel_env, build_mcp_servers
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
from acp_chat_bot.scheduled_tasks import ACP_SCHEDULED_TASKS_DB_ENV, ScheduledTaskScheduler, ScheduledTaskStore
from tests.test_agent_config import base_spec

EXTRA_STATE = Path("/agents/qoder/channel-state.json")
EXTRA_DB = Path("/agents/qoder/scheduled-tasks.sqlite3")
OTHER_TOKEN = "222:other"
DT_CLIENT_ID = "dt-client-id"
DT_CLIENT_SECRET = "dt-client-secret"
DT_ROBOT_CODE = "dt-robot-code"


def test_build_bot_instance_hands_the_bridge_the_shared_stop_event():
    stop_event = asyncio.Event()
    instance = build_bot_instance(base_spec(), stop_event=stop_event)

    assert isinstance(instance.bridge, TelegramBridge)
    assert stop_event.is_set() is False
    stop_request = instance.bridge._stop_request
    assert stop_request is not None
    stop_request()
    assert stop_event.is_set()


def test_build_bot_instance_keeps_the_paths_of_its_own_agent(tmp_path: Path):
    spec = base_spec(channel_state_file=tmp_path / "state.json", scheduled_tasks_db=tmp_path / "tasks.sqlite3")
    instance = build_bot_instance(spec, stop_event=asyncio.Event())

    assert instance.service._channel_state_file == spec.channel_state_file
    assert isinstance(instance.store, ScheduledTaskStore)
    assert instance.store.path == spec.scheduled_tasks_db
    assert isinstance(instance.scheduler, ScheduledTaskScheduler)


def test_build_bot_instance_translates_the_spec_into_a_bot_config():
    instance = build_bot_instance(base_spec(), stop_event=asyncio.Event())
    config = cast(BotConfig, instance.config)

    assert config.token == base_spec().token
    assert config.allowed_user_ids == {1}
    assert config.allowed_usernames == {"someone"}
    assert config.schedule_languages == ("en", "es")
    assert config.activity_mode == "normal"


def test_build_bot_instance_starts_the_declared_agent_command():
    spec = base_spec(command_parts=("qodercli", "--acp", "--yolo"))
    instance = build_bot_instance(spec, stop_event=asyncio.Event())

    assert instance.service._program == "qodercli"
    assert instance.service._args == ["--acp", "--yolo"]


def test_build_channel_env_forwards_the_mtproto_credentials_when_both_are_set():
    channel_env = build_channel_env(
        telegram_token=OTHER_TOKEN,
        state_file=EXTRA_STATE,
        scheduled_tasks_db=EXTRA_DB,
        allow_path_inputs=False,
        agent_name="qoder",
        mtproto_api_id=12345,
        mtproto_api_hash="  hash  ",
    )

    assert channel_env[API_ID_ENV] == "12345"
    assert channel_env[API_HASH_ENV] == "hash"


def test_build_channel_env_omits_the_mtproto_credentials_when_incomplete():
    env = build_channel_env(
        telegram_token=OTHER_TOKEN,
        state_file=EXTRA_STATE,
        scheduled_tasks_db=EXTRA_DB,
        allow_path_inputs=False,
        agent_name="qoder",
        mtproto_api_id=12345,
        mtproto_api_hash="",
    )

    assert API_ID_ENV not in env
    assert API_HASH_ENV not in env


def test_build_bot_instance_isolates_the_spawn_env_to_this_agent(tmp_path: Path):
    spec = base_spec(
        name="qoder",
        token=OTHER_TOKEN,
        channel_state_file=tmp_path / "state.json",
        scheduled_tasks_db=tmp_path / "tasks.sqlite3",
        allow_path_inputs=True,
    )
    instance = build_bot_instance(spec, stop_event=asyncio.Event())

    assert instance.service._spawn_env == build_channel_env(
        telegram_token=OTHER_TOKEN,
        state_file=tmp_path / "state.json",
        scheduled_tasks_db=tmp_path / "tasks.sqlite3",
        allow_path_inputs=True,
        agent_name="qoder",
    )
    assert instance.service._spawn_env[STATE_FILE_ENV] == str(tmp_path / "state.json")
    assert instance.service._spawn_env[TOKEN_ENV] == OTHER_TOKEN


def test_build_mcp_servers_points_the_internal_server_at_this_instance():
    servers = build_mcp_servers(
        telegram_token=OTHER_TOKEN,
        state_file=EXTRA_STATE,
        scheduled_tasks_db=EXTRA_DB,
        allow_path_inputs=True,
        agent_name="qoder",
    )

    assert len(servers) == 1
    server = servers[0]
    assert isinstance(server, McpServerStdio)
    assert server.name == "telegram-channel"
    assert server.command == sys.executable
    assert {item.name: item.value for item in server.env} == {
        TOKEN_ENV: OTHER_TOKEN,
        STATE_FILE_ENV: str(EXTRA_STATE),
        ACP_SCHEDULED_TASKS_DB_ENV: str(EXTRA_DB),
        ALLOW_PATH_ENV: "1",
        AGENT_NAME_ENV: "qoder",
    }


def test_build_channel_env_is_the_single_source_of_the_internal_server_env():
    channel_env = build_channel_env(
        telegram_token=OTHER_TOKEN,
        state_file=EXTRA_STATE,
        scheduled_tasks_db=EXTRA_DB,
        allow_path_inputs=False,
        agent_name="qoder",
    )
    servers = build_mcp_servers(
        telegram_token=OTHER_TOKEN,
        state_file=EXTRA_STATE,
        scheduled_tasks_db=EXTRA_DB,
        allow_path_inputs=False,
        agent_name="qoder",
    )

    assert {item.name: item.value for item in servers[0].env} == channel_env


def test_build_mcp_servers_appends_configured_servers():
    servers = build_mcp_servers(
        telegram_token=OTHER_TOKEN,
        state_file=EXTRA_STATE,
        scheduled_tasks_db=EXTRA_DB,
        allow_path_inputs=False,
        agent_name="qoder",
        extra_servers={
            "local": {"command": "uv", "args": ["run", "echo.py"], "env": {"KEY": "val"}},
            "remote": {"url": "https://mcp.example.com/mcp", "headers": {"Auth": "Bearer tok"}},
        },
    )

    assert [server.name for server in servers] == ["telegram-channel", "local", "remote"]
    local = servers[1]
    assert isinstance(local, McpServerStdio)
    assert {item.name: item.value for item in local.env} == {"KEY": "val"}
    remote = servers[2]
    assert isinstance(remote, HttpMcpServer)
    assert {item.name: item.value for item in remote.headers} == {"Auth": "Bearer tok"}


def test_build_mcp_servers_lets_an_external_channel_override_the_internal_one():
    servers = build_mcp_servers(
        telegram_token=OTHER_TOKEN,
        state_file=EXTRA_STATE,
        scheduled_tasks_db=EXTRA_DB,
        allow_path_inputs=False,
        agent_name="qoder",
        extra_servers={"telegram-channel": {"command": "custom-server"}},
    )

    assert len(servers) == 1
    assert servers[0].command == "custom-server"


# DingTalk channel tests


def dingtalk_spec(**overrides: object):
    """A spec declaring one DingTalk robot instead of a Telegram token."""
    return base_spec(
        channel="dingtalk",
        token="",
        allowed_user_ids=(),
        allowed_usernames=(),
        dingtalk_client_id=DT_CLIENT_ID,
        dingtalk_client_secret=DT_CLIENT_SECRET,
        dingtalk_robot_code=DT_ROBOT_CODE,
        allowed_staff_ids=("staff-1",),
        **overrides,
    )


def test_build_channel_env_forwards_the_dingtalk_credentials():
    env = build_channel_env(
        state_file=EXTRA_STATE,
        scheduled_tasks_db=EXTRA_DB,
        allow_path_inputs=True,
        agent_name="dt",
        channel="dingtalk",
        dingtalk_client_id=DT_CLIENT_ID,
        dingtalk_client_secret=DT_CLIENT_SECRET,
        dingtalk_robot_code=DT_ROBOT_CODE,
    )

    assert env[STATE_FILE_ENV] == str(EXTRA_STATE)
    assert env[ACP_SCHEDULED_TASKS_DB_ENV] == str(EXTRA_DB)
    assert env[ALLOW_PATH_ENV] == "1"
    assert env[AGENT_NAME_ENV] == "dt"
    assert env[TOKEN_ENV] == DT_CLIENT_ID
    assert env[CHANNEL_KIND_ENV] == "dingtalk"
    assert env[DINGTALK_CLIENT_ID_ENV] == DT_CLIENT_ID
    assert env[DINGTALK_CLIENT_SECRET_ENV] == DT_CLIENT_SECRET
    assert env[DINGTALK_ROBOT_CODE_ENV] == DT_ROBOT_CODE
    assert API_ID_ENV not in env
    assert API_HASH_ENV not in env


def test_build_channel_env_defaults_the_dingtalk_robot_code_to_the_client_id():
    env = build_channel_env(
        state_file=EXTRA_STATE,
        scheduled_tasks_db=EXTRA_DB,
        allow_path_inputs=False,
        agent_name="dt",
        channel="dingtalk",
        dingtalk_client_id=DT_CLIENT_ID,
        dingtalk_client_secret=DT_CLIENT_SECRET,
    )

    assert env[DINGTALK_ROBOT_CODE_ENV] == DT_CLIENT_ID


def test_build_mcp_servers_uses_the_dingtalk_server():
    servers = build_mcp_servers(
        state_file=EXTRA_STATE,
        scheduled_tasks_db=EXTRA_DB,
        allow_path_inputs=False,
        agent_name="dt",
        channel="dingtalk",
        dingtalk_client_id=DT_CLIENT_ID,
        dingtalk_client_secret=DT_CLIENT_SECRET,
        dingtalk_robot_code=DT_ROBOT_CODE,
    )

    assert len(servers) == 1
    server = servers[0]
    assert isinstance(server, McpServerStdio)
    assert server.name == "dingtalk-channel"
    assert server.args == ["-m", "acp_chat_bot.mcp.server_dingtalk"]
    assert {item.name: item.value for item in server.env} == build_channel_env(
        state_file=EXTRA_STATE,
        scheduled_tasks_db=EXTRA_DB,
        allow_path_inputs=False,
        agent_name="dt",
        channel="dingtalk",
        dingtalk_client_id=DT_CLIENT_ID,
        dingtalk_client_secret=DT_CLIENT_SECRET,
        dingtalk_robot_code=DT_ROBOT_CODE,
    )


def test_build_bot_instance_builds_a_dingtalk_runtime(tmp_path: Path):
    stop_event = asyncio.Event()
    spec = dingtalk_spec(
        name="dt",
        channel_state_file=tmp_path / "state.json",
        scheduled_tasks_db=tmp_path / "tasks.sqlite3",
    )

    instance = build_bot_instance(spec, stop_event=stop_event)

    assert isinstance(instance.config, DingTalkConfig)
    assert instance.config.client_id == DT_CLIENT_ID
    assert instance.config.client_secret == DT_CLIENT_SECRET
    assert instance.config.robot_code == DT_ROBOT_CODE
    assert instance.config.allowed_staff_ids == frozenset({"staff-1"})
    assert isinstance(instance.bridge, AgentChatBridge)
    assert isinstance(instance.runtime, DingTalkRuntime)
    assert instance.runtime.bridge is instance.bridge
    assert isinstance(instance.runtime.channel, DingTalkChannel)
    assert instance.runtime.channel is instance.bridge.channel
    assert instance.service._channel_state_file == tmp_path / "state.json"
    assert instance.store.path == tmp_path / "tasks.sqlite3"
    assert instance.stop_event is stop_event
    assert instance.service._spawn_env == build_channel_env(
        state_file=tmp_path / "state.json",
        scheduled_tasks_db=tmp_path / "tasks.sqlite3",
        allow_path_inputs=False,
        agent_name="dt",
        channel="dingtalk",
        dingtalk_client_id=DT_CLIENT_ID,
        dingtalk_client_secret=DT_CLIENT_SECRET,
        dingtalk_robot_code=DT_ROBOT_CODE,
    )
    assert isinstance(instance.runtime._api, DingTalkAPI)
