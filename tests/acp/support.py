# ruff: noqa: I001

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from urllib.parse import quote

import pytest
from acp import RequestError
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AllowedOutcome,
    AudioContentBlock,
    AvailableCommand,
    BlobResourceContents,
    DeniedOutcome,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    McpServerStdio,
    PermissionOption,
    RequestPermissionResponse,
    ResourceContentBlock,
    SessionCapabilities,
    SessionInfo,
    SessionListCapabilities,
    TextResourceContents,
    ToolCall,
    ToolCallProgress,
    ToolCallStart,
)

from telegram_acp_bot.acp.compat import text_block, update_available_commands
from telegram_acp_bot.acp.service import AGENT_EXIT_NOTICE, AcpAgentService, _AcpClient, _PendingPermission
from telegram_acp_bot.acp.models import (
    AgentActivityBlock,
    AgentCommand,
    AgentConnectionLostError,
    AgentOutputLimitExceededError,
    AgentReply,
    FilePayload,
    ImagePayload,
    PromptFile,
    PromptImage,
)
from telegram_acp_bot.core.session_registry import SessionRegistry
from telegram_acp_bot.logging_context import LOG_TEXT_PREVIEW_MAX_CHARS, log_text_preview
from telegram_acp_bot.mcp.state import (
    load_last_session_id,
    load_pending_media,
    load_prompt_message_id,
    load_session_chat_map,
    save_last_session_id,
    save_prompt_message_id,
    save_session_chat_map,
)

pytestmark = pytest.mark.asyncio

EXPECTED_CAPTURED_FILES = 2
ACP_STDIO_LIMIT_ERROR = "Separator is found, but chunk is longer than limit"
CONNECTION_CLOSED_ERROR = "Connection closed"
AGENT_BINARY_MISSING_ERROR = "agent binary is gone"


def make_client() -> _AcpClient:
    async def allow_first(_: str, options: list[PermissionOption], tool_call: ToolCall) -> RequestPermissionResponse:
        del tool_call
        if not options:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

        option_id = options[0].option_id
        return RequestPermissionResponse(outcome=AllowedOutcome(option_id=option_id, outcome="selected"))

    return _AcpClient(permission_decider=allow_first)


class FakeProcess:
    def __init__(
        self,
        *,
        with_pipes: bool = True,
        pid: int | None = None,
        raise_on_terminate: bool = False,
        raise_on_kill: bool = False,
    ) -> None:
        self.stdin = object() if with_pipes else None
        self.stdout = object() if with_pipes else None
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.pid = pid
        self._raise_on_terminate = raise_on_terminate
        self._raise_on_kill = raise_on_kill

    def terminate(self) -> None:
        if self._raise_on_terminate:
            raise OSError
        self.terminated = True

    def kill(self) -> None:
        if self._raise_on_kill:
            raise OSError
        self.killed = True

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class FakeConnection:
    def __init__(
        self,
        *,
        session_id: str = "acp-session",
        supports_load_session: bool = True,
        supports_session_list: bool = True,
        listed_sessions: list[SessionInfo] | None = None,
    ) -> None:
        self.client: _AcpClient | None = None
        self.initialized = False
        self.cwd: str | None = None
        self.prompt_calls: list[str] = []
        self.list_calls: list[tuple[str | None, str | None]] = []
        self._supports_load_session = supports_load_session
        self._supports_session_list = supports_session_list
        self._listed_sessions = listed_sessions or []
        self._session_id = session_id
        self.new_session_mcp_servers: list | None = None
        self.load_session_mcp_servers: list | None = None
        self.connect_kwargs: dict[str, bool] = {}

    async def initialize(self, **kwargs):
        self.initialized = True
        assert kwargs["protocol_version"] == 1
        session_capabilities = (
            SessionCapabilities(list=SessionListCapabilities()) if self._supports_session_list else None
        )
        return SimpleNamespace(
            agent_capabilities=AgentCapabilities(
                load_session=self._supports_load_session,
                session_capabilities=session_capabilities,
            )
        )

    async def new_session(self, *, cwd: str, mcp_servers: list) -> SimpleNamespace:
        self.cwd = cwd
        self.new_session_mcp_servers = mcp_servers
        return SimpleNamespace(session_id=self._session_id)

    async def load_session(self, *, cwd: str, session_id: str, mcp_servers: list) -> SimpleNamespace:
        self.cwd = cwd
        self.load_session_mcp_servers = mcp_servers
        return SimpleNamespace(config_options=[], models=[], modes=[])

    async def list_sessions(self, *, cursor: str | None = None, cwd: str | None = None) -> SimpleNamespace:
        self.list_calls.append((cursor, cwd))
        return SimpleNamespace(next_cursor=None, sessions=self._listed_sessions)

    async def prompt(self, *, session_id: str, prompt: list) -> SimpleNamespace:
        self.prompt_calls.append(session_id)
        assert prompt
        assert self.client is not None
        update = AgentMessageChunk(content=text_block("hello from acp"), session_update="agent_message_chunk")
        await self.client.session_update(session_id=session_id, update=update)
        return SimpleNamespace(stop_reason="end_turn")

    async def cancel(self, *, session_id: str) -> None:
        self.prompt_calls.append(f"cancel:{session_id}")


class FileResourceConnection(FakeConnection):
    def __init__(self, *, session_id: str, resource: ResourceContentBlock) -> None:
        super().__init__(session_id=session_id)
        self._resource = resource

    async def prompt(self, *, session_id: str, prompt: list) -> SimpleNamespace:
        self.prompt_calls.append(session_id)
        assert prompt
        assert self.client is not None
        await self.client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(content=text_block("resource reply"), session_update="agent_message_chunk"),
        )
        await self.client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(content=self._resource, session_update="agent_message_chunk"),
        )
        return SimpleNamespace(stop_reason="end_turn")


class CommandsConnection(FakeConnection):
    """Fake agent that advertises its slash commands while the session is being created."""

    async def new_session(self, *, cwd: str, mcp_servers: list) -> SimpleNamespace:
        session = await super().new_session(cwd=cwd, mcp_servers=mcp_servers)
        assert self.client is not None
        await self.client.session_update(
            session_id=session.session_id,
            update=update_available_commands(
                [
                    AvailableCommand(
                        name="goal",
                        description="Set or manage a persistent goal",
                        input={"hint": "<objective> | status"},
                    ),
                    AvailableCommand(name="deep-research", description="Research a topic"),
                ]
            ),
        )
        return session


class ReadvertiseCommandsConnection(CommandsConnection):
    """Fake agent that publishes a different command list after each prompt."""

    async def prompt(self, *, session_id: str, prompt: list) -> SimpleNamespace:
        result = await super().prompt(session_id=session_id, prompt=prompt)
        assert self.client is not None
        await self.client.session_update(
            session_id=session_id,
            update=update_available_commands(
                [
                    AvailableCommand(
                        name=f"extra-{len(self.prompt_calls)}",
                        description=f"Command added on prompt {len(self.prompt_calls)}",
                        input={"hint": "<target>"},
                    )
                ]
            ),
        )
        return result


class OversizeLineConnection(FakeConnection):
    async def prompt(self, *, session_id: str, prompt: list) -> SimpleNamespace:
        del session_id, prompt
        raise ValueError(ACP_STDIO_LIMIT_ERROR)


class ConnectionLossConnection(FakeConnection):
    """Fake agent whose stdio dies while a prompt is in flight, like a killed process."""

    async def prompt(self, *, session_id: str, prompt: list) -> SimpleNamespace:
        del session_id, prompt
        raise ConnectionError(CONNECTION_CLOSED_ERROR)


class GenericValueErrorConnection(FakeConnection):
    async def prompt(self, *, session_id: str, prompt: list) -> SimpleNamespace:
        del session_id, prompt
        raise ValueError("unexpected")


class HangingInitializeConnection(FakeConnection):
    async def initialize(self, **kwargs):
        del kwargs
        await asyncio.sleep(10)


class HangingNewSessionConnection(FakeConnection):
    async def new_session(self, *, cwd: str, mcp_servers: list) -> SimpleNamespace:
        del cwd, mcp_servers
        await asyncio.sleep(10)
        return SimpleNamespace()


class HangingLoadSessionConnection(FakeConnection):
    async def load_session(self, *, cwd: str, session_id: str, mcp_servers: list) -> SimpleNamespace:
        del cwd, session_id, mcp_servers
        await asyncio.sleep(10)
        return SimpleNamespace()


class NoSessionCapabilitiesConnection(FakeConnection):
    async def initialize(self, **kwargs):
        self.initialized = True
        assert kwargs["protocol_version"] == 1
        return SimpleNamespace(agent_capabilities=AgentCapabilities(load_session=True, session_capabilities=None))


__all__ = [
    "ACP_STDIO_LIMIT_ERROR",
    "AGENT_BINARY_MISSING_ERROR",
    "AGENT_EXIT_NOTICE",
    "CONNECTION_CLOSED_ERROR",
    "EXPECTED_CAPTURED_FILES",
    "LOG_TEXT_PREVIEW_MAX_CHARS",
    "AcpAgentService",
    "AgentActivityBlock",
    "AgentCapabilities",
    "AgentCommand",
    "AgentConnectionLostError",
    "AgentMessageChunk",
    "AgentOutputLimitExceededError",
    "AgentReply",
    "AllowedOutcome",
    "AudioContentBlock",
    "AvailableCommand",
    "BlobResourceContents",
    "CommandsConnection",
    "ConnectionLossConnection",
    "DeniedOutcome",
    "EmbeddedResourceContentBlock",
    "FakeConnection",
    "FakeProcess",
    "FilePayload",
    "FileResourceConnection",
    "GenericValueErrorConnection",
    "HangingInitializeConnection",
    "HangingLoadSessionConnection",
    "HangingNewSessionConnection",
    "ImageContentBlock",
    "ImagePayload",
    "McpServerStdio",
    "NoSessionCapabilitiesConnection",
    "OversizeLineConnection",
    "Path",
    "PermissionOption",
    "PromptFile",
    "PromptImage",
    "ReadvertiseCommandsConnection",
    "RequestError",
    "RequestPermissionResponse",
    "ResourceContentBlock",
    "SessionCapabilities",
    "SessionInfo",
    "SessionListCapabilities",
    "SessionRegistry",
    "SimpleNamespace",
    "TextResourceContents",
    "ToolCall",
    "ToolCallProgress",
    "ToolCallStart",
    "_AcpClient",
    "_PendingPermission",
    "asyncio",
    "base64",
    "cast",
    "load_last_session_id",
    "load_pending_media",
    "load_prompt_message_id",
    "load_session_chat_map",
    "log_text_preview",
    "logging",
    "make_client",
    "pytest",
    "pytestmark",
    "quote",
    "save_last_session_id",
    "save_prompt_message_id",
    "save_session_chat_map",
    "text_block",
    "update_available_commands",
]
