"""AcpAgentService: manages one live ACP agent session per Telegram chat."""

from __future__ import annotations

import asyncio
import asyncio.subprocess as aio_subprocess
import base64
import errno
import logging
import mimetypes
import os
import signal
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlparse
from uuid import uuid4

from acp import PROTOCOL_VERSION, connect_to_agent
from acp.core import ClientSideConnection
from acp.schema import (
    AcceptElicitationResponse,
    AgentCapabilities,
    AllowedOutcome,
    CancelElicitationResponse,
    ClientCapabilities,
    CreateElicitationResponse,
    DeclineElicitationResponse,
    DeniedOutcome,
    ElicitationCapabilities,
    ElicitationFormCapabilities,
    ElicitationMode,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    McpServerStdio,
    PermissionOption,
    RequestPermissionResponse,
    SessionInfo,
    ToolCall,
)

from telegram_acp_bot.acp.client import _AcpClient
from telegram_acp_bot.acp.compat import text_block
from telegram_acp_bot.acp.elicitation import elicitation_content, elicitation_form, elicitation_questions
from telegram_acp_bot.acp.models import (
    AgentActivityBlock,
    AgentCommand,
    AgentConnectionLostError,
    AgentOutputLimitExceededError,
    AgentReply,
    ElicitationRequest,
    FilePayload,
    ImagePayload,
    PermissionDecisionAction,
    PermissionEventOutput,
    PermissionMode,
    PermissionPolicy,
    PermissionRequest,
    PromptFile,
    PromptImage,
    ResumableSession,
)
from telegram_acp_bot.acp.protocols import (
    AcpConnectionFactory,
    AcpHandshakeTimeoutError,
    AcpSpawnFn,
    ProcessLike,
    PromptContentBlock,
    SessionLoadNotSupportedError,
    _package_version,
)
from telegram_acp_bot.acp.session import _LiveSession, _PendingElicitation, _PendingPermission
from telegram_acp_bot.core.session_registry import SessionRegistry
from telegram_acp_bot.logging_context import bind_log_context, log_text_preview
from telegram_acp_bot.mcp.state import (
    append_group_message,
    append_pending_media,
    load_group_history,
    load_last_session_id,
    load_session_chat_map,
    save_last_session_id,
    save_prompt_message_id,
    save_session_chat_map,
    take_pending_media,
)

logger = logging.getLogger(__name__)

PROCESS_SHUTDOWN_TIMEOUT_SECONDS = 3

AGENT_EXIT_NOTICE = (
    "The agent process exited while it was working on your last message, so that message was not "
    "finished. The session is back — send the message again to continue."
)


def _is_missing_process_error(exc: OSError) -> bool:
    return isinstance(exc, ProcessLookupError) or exc.errno == errno.ESRCH


def _terminate_process(process: ProcessLike) -> None:
    try:
        process.terminate()
    except OSError as exc:
        if not _is_missing_process_error(exc):
            raise


def _kill_process(process: ProcessLike) -> None:
    try:
        process.kill()
    except OSError as exc:
        if not _is_missing_process_error(exc):
            raise


class AcpAgentService:
    """ACP-backed service to manage one running agent session per Telegram chat."""

    def __init__(  # noqa: PLR0913
        self,
        registry: SessionRegistry,
        *,
        program: str,
        args: list[str],
        default_permission_mode: PermissionMode = "ask",
        permission_event_output: PermissionEventOutput = "stdout",
        mcp_servers: tuple[McpServerStdio | HttpMcpServer, ...] = (),
        channel_state_file: Path | None = None,
        spawn_env: Mapping[str, str] | None = None,
        stdio_limit: int = 8_388_608,
        connect_timeout: float = 30.0,
        connector: AcpConnectionFactory | None = None,
        spawner: AcpSpawnFn | None = None,
    ) -> None:
        if stdio_limit <= 0:
            raise ValueError(stdio_limit)
        if connect_timeout <= 0:
            raise ValueError(connect_timeout)
        self._registry = registry
        self._program = program
        self._args = args
        self._default_permission_mode = default_permission_mode
        self._permission_event_output = permission_event_output
        self._mcp_servers = mcp_servers
        self._channel_state_file = channel_state_file
        self._spawn_env = spawn_env
        self._stdio_limit = stdio_limit
        self._connect_timeout = connect_timeout
        self._connector = connector or cast(AcpConnectionFactory, connect_to_agent)
        self._spawner = spawner or cast(AcpSpawnFn, asyncio.create_subprocess_exec)
        self._live_by_chat: dict[int, _LiveSession] = {}
        self._chat_by_session: dict[str, int] = {}
        self._commands_by_session: dict[str, tuple[AgentCommand, ...]] = {}
        self._pending_permissions: dict[str, _PendingPermission] = {}
        self._permission_prompt_handler: Callable[[PermissionRequest], Awaitable[None]] | None = None
        self._pending_elicitations: dict[str, _PendingElicitation] = {}
        self._elicitation_prompt_handler: Callable[[ElicitationRequest], Awaitable[None]] | None = None
        self._elicitation_expiry_handler: Callable[[int, str], Awaitable[None]] | None = None
        self._activity_event_handler: Callable[[int, AgentActivityBlock], Awaitable[None]] | None = None
        self._channel_state_lock = asyncio.Lock()

    async def new_session(self, *, chat_id: int, workspace: Path) -> str:
        workspace = self._normalize_workspace(workspace)
        with bind_log_context(chat_id=chat_id):
            logger.info("Starting ACP session for chat_id=%s workspace=%s", chat_id, workspace)
        if workspace.exists() and not workspace.is_dir():
            raise ValueError(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        existing = self._live_by_chat.pop(chat_id, None)
        if existing is not None:
            await self._shutdown(existing.process)
            await self._drop_channel_session_mapping(session_id=existing.acp_session_id)
            self._chat_by_session.pop(existing.acp_session_id, None)
            self._commands_by_session.pop(existing.acp_session_id, None)

        process, connection, client, capabilities = await self._start_initialized_connection(chat_id=chat_id)
        try:
            logger.debug("Requesting ACP new_session for chat_id=%s", chat_id)
            session = await asyncio.wait_for(
                connection.new_session(cwd=str(workspace), mcp_servers=list(self._mcp_servers)),
                timeout=self._connect_timeout,
            )
        except TimeoutError as exc:
            await self._shutdown(process)
            raise AcpHandshakeTimeoutError(self._connect_timeout) from exc

        self._registry.create_or_replace(chat_id=chat_id, workspace=workspace, session_id=session.session_id)
        await self._save_channel_session_mapping(chat_id=chat_id, session_id=session.session_id)
        self._live_by_chat[chat_id] = _LiveSession(
            acp_session_id=session.session_id,
            workspace=workspace,
            process=process,
            connection=connection,
            client=client,
            supports_load_session=self._supports_load_session(capabilities),
            supports_session_list=self._supports_session_list(capabilities),
            permission_mode=self._default_permission_mode,
        )
        self._chat_by_session[session.session_id] = chat_id
        with bind_log_context(chat_id=chat_id, session_id=session.session_id):
            logger.info("ACP session started for chat_id=%s session_id=%s", chat_id, session.session_id)
        return session.session_id

    async def load_session(self, *, chat_id: int, session_id: str, workspace: Path) -> str:
        workspace = self._normalize_workspace(workspace)
        with bind_log_context(chat_id=chat_id, session_id=session_id):
            logger.info("Loading ACP session for chat_id=%s session_id=%s workspace=%s", chat_id, session_id, workspace)
        if workspace.exists() and not workspace.is_dir():
            raise ValueError(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        existing = self._live_by_chat.pop(chat_id, None)
        if existing is not None:
            await self._shutdown(existing.process)
            await self._drop_channel_session_mapping(session_id=existing.acp_session_id)
            self._chat_by_session.pop(existing.acp_session_id, None)
            self._commands_by_session.pop(existing.acp_session_id, None)

        process, connection, client, capabilities = await self._start_initialized_connection(chat_id=chat_id)
        if not self._supports_load_session(capabilities):
            await self._shutdown(process)
            raise SessionLoadNotSupportedError()
        try:
            await asyncio.wait_for(
                connection.load_session(
                    cwd=str(workspace),
                    session_id=session_id,
                    mcp_servers=list(self._mcp_servers),
                ),
                timeout=self._connect_timeout,
            )
        except TimeoutError as exc:
            await self._shutdown(process)
            raise AcpHandshakeTimeoutError(self._connect_timeout) from exc

        self._registry.create_or_replace(chat_id=chat_id, workspace=workspace, session_id=session_id)
        await self._save_channel_session_mapping(chat_id=chat_id, session_id=session_id)
        self._live_by_chat[chat_id] = _LiveSession(
            acp_session_id=session_id,
            workspace=workspace,
            process=process,
            connection=connection,
            client=client,
            supports_load_session=self._supports_load_session(capabilities),
            supports_session_list=self._supports_session_list(capabilities),
            permission_mode=self._default_permission_mode,
        )
        self._chat_by_session[session_id] = chat_id
        with bind_log_context(chat_id=chat_id, session_id=session_id):
            logger.info("ACP session loaded for chat_id=%s session_id=%s", chat_id, session_id)
        return session_id

    async def list_resumable_sessions(
        self,
        *,
        chat_id: int,
        workspace: Path | None = None,
    ) -> tuple[ResumableSession, ...] | None:
        normalized_workspace = None if workspace is None else self._normalize_workspace(workspace)
        live = self._live_by_chat.get(chat_id)
        if live is not None:
            if not live.supports_session_list:
                return None
            return await self._list_sessions_from_connection(
                connection=live.connection,
                workspace=normalized_workspace,
            )

        process, connection, _client, capabilities = await self._start_initialized_connection(chat_id=chat_id)
        try:
            if not self._supports_session_list(capabilities):
                return None
            return await self._list_sessions_from_connection(connection=connection, workspace=normalized_workspace)
        finally:
            await self._shutdown(process)

    async def prompt(
        self,
        *,
        chat_id: int,
        text: str,
        images: tuple[PromptImage, ...] = (),
        files: tuple[PromptFile, ...] = (),
    ) -> AgentReply | None:
        live = self._live_by_chat.get(chat_id)
        if live is None:
            with bind_log_context(chat_id=chat_id):
                logger.warning("Prompt ignored because no live session exists for chat_id=%s", chat_id)
            return None

        async with live.prompt_lock:
            if self._agent_exited(live):
                # The agent died between turns: reviving before the prompt means this message still
                # runs, because none of it reached the dead process.
                live = await self._revive_session(chat_id=chat_id, stale=live)
            return await self._exchange(
                chat_id=chat_id,
                live=live,
                text=text,
                prompt_blocks=self._prompt_blocks(text=text, images=images, files=files),
            )

    def _prompt_blocks(
        self,
        *,
        text: str,
        images: tuple[PromptImage, ...],
        files: tuple[PromptFile, ...],
    ) -> list[PromptContentBlock]:
        prompt_blocks: list[PromptContentBlock] = [text_block(text)]
        prompt_blocks.extend(
            [ImageContentBlock(data=image.data_base64, mime_type=image.mime_type, type="image") for image in images]
        )
        for file in files:
            if file.text_content is not None:
                prompt_blocks.append(text_block(f"File: {file.name}\n\n{file.text_content}"))
                continue
            if file.data_base64 is not None:
                prompt_blocks.append(text_block(f"Binary file attached: {file.name} ({file.mime_type or 'unknown'})"))
        return prompt_blocks

    async def _exchange(
        self,
        *,
        chat_id: int,
        live: _LiveSession,
        text: str,
        prompt_blocks: list[PromptContentBlock],
    ) -> AgentReply:
        """Run one prompt turn, reviving the chat when the agent dies underneath it."""
        with bind_log_context(chat_id=chat_id, session_id=live.acp_session_id):
            logger.info("Prompt to ACP: %s", log_text_preview(text))
            if live.next_prompt_auto_approve:
                live.active_prompt_auto_approve = True
                live.next_prompt_auto_approve = False
            live.client.start_capture(live.acp_session_id)
            try:
                try:
                    await live.connection.prompt(session_id=live.acp_session_id, prompt=prompt_blocks)
                except ValueError as exc:
                    if "chunk is longer than limit" in str(exc):
                        raise AgentOutputLimitExceededError from exc
                    raise
                except ConnectionError as exc:
                    # The agent is unreachable for every method now, not just this turn, and tools it
                    # had already run may have left side effects. Revive the session and say so rather
                    # than replaying a half-done turn onto a chat that expects an answer.
                    await self._revive_session(chat_id=chat_id, stale=live, cause=exc)
                    return AgentReply(text=AGENT_EXIT_NOTICE)
                response = await live.client.finish_capture(live.acp_session_id)
                response = self._resolve_file_uri_resources(response=response, workspace=live.workspace)
                logger.debug("Reply from ACP: %s", log_text_preview(response.text))
                return AgentReply(
                    text=response.text,
                    images=response.images,
                    files=response.files,
                )
            finally:
                live.active_prompt_auto_approve = False
                self._close_session_elicitations(session_id=live.acp_session_id)

    async def _revive_session(
        self,
        *,
        chat_id: int,
        stale: _LiveSession,
        cause: BaseException | None = None,
    ) -> _LiveSession:
        """Replace a dead agent with a fresh connection, keeping the conversation where possible.

        The old `session_id` is reloaded when the new agent advertises ACP `session/load`, so context
        survives the crash. Otherwise the chat moves to a new session and the old one stays reachable
        through `/resume`. Permission mode carries over: a chat that was set to approve must not fall
        back to asking after an accident.
        """
        session_id = stale.acp_session_id
        with bind_log_context(chat_id=chat_id, session_id=session_id):
            if cause is None:
                logger.warning(
                    "ACP agent exited between turns (returncode=%s); reviving chat_id=%s",
                    stale.process.returncode,
                    chat_id,
                )
            else:
                logger.warning("ACP agent died mid-prompt (%s); reviving chat_id=%s", cause, chat_id)

        self._close_session_elicitations(session_id=session_id)
        self._close_session_permissions(session_id=session_id)
        await self._shutdown(stale.process)
        # Drop the corpse first: `load_session`/`new_session` shut down whatever live entry they find.
        self._live_by_chat.pop(chat_id, None)

        # `Exception` keeps `asyncio.CancelledError` out of these handlers, so a shutdown of the
        # prompt task still propagates instead of being mistaken for a dead agent.
        try:
            await self.load_session(chat_id=chat_id, session_id=session_id, workspace=stale.workspace)
        except Exception as exc:  # noqa: BLE001
            with bind_log_context(chat_id=chat_id, session_id=session_id):
                logger.warning("ACP session could not be reloaded (%s); starting a new session", exc)
            try:
                await self.new_session(chat_id=chat_id, workspace=stale.workspace)
            except Exception as exc:
                with bind_log_context(chat_id=chat_id, session_id=session_id):
                    logger.exception("ACP agent could not be restarted for chat_id=%s", chat_id)
                raise AgentConnectionLostError from exc

        revived = self._live_by_chat[chat_id]
        revived.permission_mode = stale.permission_mode
        with bind_log_context(chat_id=chat_id, session_id=revived.acp_session_id):
            logger.info("ACP session revived for chat_id=%s", chat_id)
        return revived

    def _close_session_permissions(self, *, session_id: str) -> None:
        """Stop waiting on approvals the dead agent can no longer act on."""
        for request_id, pending in list(self._pending_permissions.items()):
            if pending.acp_session_id != session_id or pending.future.done():
                continue
            with bind_log_context(chat_id=pending.chat_id, session_id=session_id):
                logger.info("Permission request dropped with the dead agent: request_id=%s", request_id)
            self._pending_permissions.pop(request_id, None)
            pending.future.set_result(RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled")))

    @staticmethod
    def _agent_exited(live: _LiveSession) -> bool:
        return live.process.returncode is not None

    def get_workspace(self, *, chat_id: int) -> Path | None:
        session = self._registry.get(chat_id)
        return None if session is None else session.workspace

    def get_active_session_context(self, *, chat_id: int) -> tuple[str, Path] | None:
        session = self._registry.get(chat_id)
        if session is None:
            return None
        return session.session_id, session.workspace

    def supports_session_loading(self, *, chat_id: int) -> bool | None:
        live = self._live_by_chat.get(chat_id)
        if live is None:
            return None
        return live.supports_load_session

    async def cancel(self, *, chat_id: int) -> bool:
        live = self._live_by_chat.get(chat_id)
        if live is None or self._agent_exited(live):
            return False

        await live.connection.cancel(session_id=live.acp_session_id)
        return True

    async def stop(self, *, chat_id: int) -> bool:
        live = self._live_by_chat.pop(chat_id, None)
        if live is None:
            return False

        stale = [request_id for request_id, pending in self._pending_permissions.items() if pending.chat_id == chat_id]
        for request_id in stale:
            pending = self._pending_permissions.pop(request_id)
            if not pending.future.done():
                pending.future.set_result(RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled")))

        stale = [request_id for request_id, pending in self._pending_elicitations.items() if pending.chat_id == chat_id]
        for request_id in stale:
            pending = self._pending_elicitations.pop(request_id)
            if not pending.future.done():
                pending.future.set_result(CancelElicitationResponse(action="cancel"))

        await self._shutdown(live.process)
        self._registry.clear(chat_id)
        self._chat_by_session.pop(live.acp_session_id, None)
        self._commands_by_session.pop(live.acp_session_id, None)
        await self._drop_channel_session_mapping(session_id=live.acp_session_id)
        return True

    async def clear(self, *, chat_id: int) -> bool:
        return await self.stop(chat_id=chat_id)

    def get_permission_policy(self, *, chat_id: int) -> PermissionPolicy | None:
        live = self._live_by_chat.get(chat_id)
        if live is None:
            return None
        return PermissionPolicy(
            session_mode=live.permission_mode,
            next_prompt_auto_approve=live.next_prompt_auto_approve,
        )

    def get_available_commands(self, *, chat_id: int) -> tuple[AgentCommand, ...] | None:
        """Commands the agent last advertised for this chat, or None without a live session."""
        live = self._live_by_chat.get(chat_id)
        if live is None:
            return None
        return self._commands_by_session.get(live.acp_session_id, ())

    async def set_session_permission_mode(self, *, chat_id: int, mode: PermissionMode) -> bool:
        live = self._live_by_chat.get(chat_id)
        if live is None:
            return False
        live.permission_mode = mode
        return True

    async def set_next_prompt_auto_approve(self, *, chat_id: int, enabled: bool) -> bool:
        live = self._live_by_chat.get(chat_id)
        if live is None:
            return False
        live.next_prompt_auto_approve = enabled
        return True

    def set_permission_request_handler(
        self,
        handler: Callable[[PermissionRequest], Awaitable[None]] | None,
    ) -> None:
        self._permission_prompt_handler = handler

    def set_elicitation_request_handler(
        self,
        handler: Callable[[ElicitationRequest], Awaitable[None]] | None,
    ) -> None:
        self._elicitation_prompt_handler = handler

    def set_elicitation_expiry_handler(
        self,
        handler: Callable[[int, str], Awaitable[None]] | None,
    ) -> None:
        self._elicitation_expiry_handler = handler

    def set_activity_event_handler(
        self,
        handler: Callable[[int, AgentActivityBlock], Awaitable[None]] | None,
    ) -> None:
        self._activity_event_handler = handler

    async def respond_permission_request(
        self,
        *,
        chat_id: int,
        request_id: str,
        action: PermissionDecisionAction,
    ) -> bool:
        pending = self._pending_permissions.get(request_id)
        if pending is None or pending.chat_id != chat_id or pending.future.done():
            with bind_log_context(chat_id=chat_id):
                logger.warning(
                    "Permission response ignored: request_id=%s chat_id=%s pending=%s",
                    request_id,
                    chat_id,
                    pending is not None,
                )
            return False

        with bind_log_context(chat_id=chat_id, session_id=pending.acp_session_id):
            logger.info("Permission response received: request_id=%s action=%s", request_id, action)
        available_actions = self._available_actions(pending.options)
        if action not in available_actions:
            with bind_log_context(chat_id=chat_id, session_id=pending.acp_session_id):
                logger.warning(
                    "Permission response rejected: unavailable action request_id=%s chat_id=%s action=%s available=%s",
                    request_id,
                    chat_id,
                    action,
                    ",".join(available_actions),
                )
            return False

        response = self._build_permission_response(
            options=pending.options,
            action=action,
        )
        if action == "always":
            live = self._live_by_chat.get(chat_id)
            if live is not None:
                live.permission_mode = "approve"
        with bind_log_context(chat_id=chat_id, session_id=pending.acp_session_id):
            logger.info("Permission response accepted: request_id=%s chat_id=%s action=%s", request_id, chat_id, action)
        pending.future.set_result(response)
        return True

    async def respond_elicitation_request(
        self,
        *,
        chat_id: int,
        request_id: str,
        answers: Mapping[str, tuple[str, ...]] | None,
    ) -> bool:
        """Resolve one pending elicitation: tap answers accept, `None` declines."""
        pending = self._pending_elicitations.get(request_id)
        if pending is None or pending.chat_id != chat_id or pending.future.done():
            with bind_log_context(chat_id=chat_id):
                logger.warning(
                    "Elicitation response ignored: request_id=%s chat_id=%s pending=%s",
                    request_id,
                    chat_id,
                    pending is not None,
                )
            return False

        if answers is None:
            response: CreateElicitationResponse = DeclineElicitationResponse(action="decline")
            summary = "declined"
        else:
            content = self._elicitation_content(pending=pending, answers=answers)
            if content is None:
                response = DeclineElicitationResponse(action="decline")
                summary = "incomplete"
                with bind_log_context(chat_id=chat_id, session_id=pending.acp_session_id):
                    logger.warning("Elicitation response rejected: incomplete answers request_id=%s", request_id)
            else:
                response = AcceptElicitationResponse(action="accept", content=content)
                summary = ",".join(f"{field}={'|'.join(values)}" for field, values in sorted(answers.items()))
        with bind_log_context(chat_id=chat_id, session_id=pending.acp_session_id):
            logger.info("Elicitation resolved: request_id=%s answers=%s", request_id, summary)
        pending.future.set_result(response)
        return True

    @staticmethod
    def _elicitation_content(
        *,
        pending: _PendingElicitation,
        answers: Mapping[str, tuple[str, ...]],
    ) -> dict[str, str | list[str]] | None:
        """Validate the taps against the declared options, or None when they do not answer the form."""
        for question in pending.questions:
            values = answers.get(question.field)
            if not values:
                return None
            allowed = {option.value for option in question.options}
            if any(value not in allowed for value in values):
                return None
            if not question.multiple and len(values) > 1:
                return None
        return elicitation_content(pending.questions, answers)

    async def _shutdown(self, process: ProcessLike) -> None:
        if process.returncode is not None:
            return

        pid: int | None = getattr(process, "pid", None)
        killpg = getattr(os, "killpg", None)
        if killpg is not None and pid is not None:
            try:
                killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError as exc:
                if not _is_missing_process_error(exc):
                    _terminate_process(process)
        else:
            _terminate_process(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=PROCESS_SHUTDOWN_TIMEOUT_SECONDS)
        except TimeoutError:
            if killpg is not None and pid is not None:
                try:
                    killpg(os.getpgid(pid), signal.SIGKILL)
                except OSError as exc:
                    if not _is_missing_process_error(exc):
                        _kill_process(process)
            else:
                _kill_process(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=PROCESS_SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning(
                    "ACP process did not exit after SIGKILL fallback within %ss: pid=%s",
                    PROCESS_SHUTDOWN_TIMEOUT_SECONDS,
                    pid,
                )

    async def _start_initialized_connection(
        self,
        *,
        chat_id: int,
    ) -> tuple[ProcessLike, ClientSideConnection, _AcpClient, AgentCapabilities]:
        process = await self._spawner(
            self._program,
            *self._args,
            stdin=aio_subprocess.PIPE,
            stdout=aio_subprocess.PIPE,
            limit=self._stdio_limit,
            start_new_session=True,
            **({"env": {**os.environ, **self._spawn_env}} if self._spawn_env else {}),
        )
        with bind_log_context(chat_id=chat_id):
            logger.debug("Spawned ACP process for chat_id=%s pid=%s", chat_id, getattr(process, "pid", "unknown"))
        if process.stdin is None or process.stdout is None:
            raise RuntimeError

        client = _AcpClient(
            permission_decider=self._decide_permission,
            elicitation_decider=self._decide_elicitation,
            event_reporter=self._report_permission_event,
            activity_reporter=self._forward_activity_event,
            commands_reporter=self._store_available_commands,
        )
        # Without this the SDK router answers elicitation/create with method_not_found.
        connection = self._connector(client, process.stdin, process.stdout, use_unstable_protocol=True)
        try:
            with bind_log_context(chat_id=chat_id):
                logger.debug("Initializing ACP connection for chat_id=%s", chat_id)
            initialized = await asyncio.wait_for(
                connection.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(
                        elicitation=ElicitationCapabilities(form=ElicitationFormCapabilities()),
                    ),
                    client_info=Implementation(
                        name="telegram-acp-bot",
                        title="Telegram ACP Bot",
                        version=_package_version(),
                    ),
                ),
                timeout=self._connect_timeout,
            )
        except TimeoutError as exc:
            await self._shutdown(process)
            raise AcpHandshakeTimeoutError(self._connect_timeout) from exc
        capabilities = AgentCapabilities()
        if initialized is not None and initialized.agent_capabilities is not None:
            capabilities = initialized.agent_capabilities
        return process, connection, client, capabilities

    async def _list_sessions_from_connection(
        self,
        *,
        connection: ClientSideConnection,
        workspace: Path | None,
    ) -> tuple[ResumableSession, ...]:
        cursor: str | None = None
        sessions: list[SessionInfo] = []
        for _ in range(5):
            result = await asyncio.wait_for(
                connection.list_sessions(cursor=cursor, cwd=None if workspace is None else str(workspace)),
                timeout=self._connect_timeout,
            )
            sessions.extend(result.sessions or [])
            cursor = result.next_cursor
            if cursor is None:
                break
        normalized_target = self._resolved_workspace_or_none(workspace)
        mapped: list[ResumableSession] = []
        for info in sessions:
            item_workspace = self._workspace_from_session_cwd(info.cwd)
            if normalized_target is not None and item_workspace != normalized_target:
                continue
            mapped.append(
                ResumableSession(
                    session_id=info.session_id,
                    workspace=item_workspace,
                    title=(info.title or "").strip() or "(untitled session)",
                    updated_at=info.updated_at or "",
                )
            )
        mapped.sort(key=lambda item: item.updated_at, reverse=True)
        return tuple(mapped)

    @staticmethod
    def _supports_load_session(capabilities: AgentCapabilities) -> bool:
        # Some agents expose capabilities with partial/empty values; treat
        # explicit False as unsupported and probe otherwise.
        return capabilities.load_session is not False

    @staticmethod
    def _supports_session_list(capabilities: AgentCapabilities) -> bool:
        session_capabilities = capabilities.session_capabilities
        if session_capabilities is None:
            return False
        # Some agents expose `session_capabilities.list` as null/empty metadata
        # while still implementing `session/list`.
        return session_capabilities.list is not False

    @staticmethod
    def _normalize_workspace(workspace: Path) -> Path:
        return workspace.expanduser().resolve()

    @staticmethod
    def _resolved_workspace_or_none(workspace: Path | None) -> Path | None:
        return None if workspace is None else workspace.resolve()

    @staticmethod
    def _workspace_from_session_cwd(cwd: str | None) -> Path:
        raw_cwd = cwd or "."
        return Path(raw_cwd).expanduser().resolve()

    async def _decide_permission(
        self,
        session_id: str,
        options: list[PermissionOption],
        tool_call: ToolCall,
    ) -> RequestPermissionResponse:
        if not options:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

        for chat_id, live in self._live_by_chat.items():
            if live.acp_session_id != session_id:
                continue
            if live.permission_mode == "approve" or live.active_prompt_auto_approve:
                auto_action = self._auto_approve_action(tuple(options))
                return self._build_permission_response(options=tuple(options), action=auto_action)
            if live.permission_mode == "deny":
                return self._build_permission_response(options=tuple(options), action="deny")

            request_id = uuid4().hex[:12]
            future: asyncio.Future[RequestPermissionResponse] = asyncio.get_running_loop().create_future()
            pending = _PendingPermission(
                request_id=request_id,
                chat_id=chat_id,
                acp_session_id=session_id,
                tool_title=tool_call.title,
                tool_call_id=tool_call.tool_call_id,
                options=tuple(options),
                future=future,
            )
            self._pending_permissions[request_id] = pending
            if self._permission_prompt_handler is None:
                self._pending_permissions.pop(request_id, None)
                return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
            request = PermissionRequest(
                chat_id=pending.chat_id,
                request_id=request_id,
                tool_title=pending.tool_title,
                tool_call_id=pending.tool_call_id,
                available_actions=self._available_actions(options=pending.options),
            )
            await self._permission_prompt_handler(request)

            try:
                response = await asyncio.wait_for(future, timeout=300)
            except TimeoutError:
                response = RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
            finally:
                self._pending_permissions.pop(request_id, None)
            return response

        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    def _close_session_elicitations(self, *, session_id: str) -> None:
        """Stop waiting on questions the agent abandoned while its turn was still open.

        A JSON-RPC request cannot be cancelled from the server side, so a turn that ends underneath an open
        question leaves us blocking until the deadline with buttons nobody can use. The turn ending is the
        only signal; `_decide_elicitation` owns popping the entry and notifying the UI.
        """
        for request_id, pending in list(self._pending_elicitations.items()):
            if pending.acp_session_id != session_id or pending.future.done():
                continue
            with bind_log_context(chat_id=pending.chat_id, session_id=session_id):
                logger.info(
                    "Elicitation abandoned by the agent: request_id=%s",
                    request_id,
                )
            pending.future.set_result(CancelElicitationResponse(action="cancel"))

    async def _decide_elicitation(self, message: str, mode: ElicitationMode) -> CreateElicitationResponse:
        """Turn the agent's structured question into tap-able choices and wait for the user."""
        form = elicitation_form(mode)
        if form is None:
            return CancelElicitationResponse(action="cancel")

        session_id = form.session_id
        questions = elicitation_questions(form.requested_schema)
        if questions is None:
            with bind_log_context(session_id=session_id):
                logger.info("Elicitation unsupported: session_id=%s message=%s", session_id, log_text_preview(message))
            return CancelElicitationResponse(action="cancel")

        chat_id = self._chat_by_session.get(session_id)
        if chat_id is None:
            return CancelElicitationResponse(action="cancel")

        request_id = uuid4().hex[:12]
        future: asyncio.Future[CreateElicitationResponse] = asyncio.get_running_loop().create_future()
        pending = _PendingElicitation(
            request_id=request_id,
            chat_id=chat_id,
            acp_session_id=session_id,
            message=message,
            questions=questions,
            future=future,
        )
        self._pending_elicitations[request_id] = pending
        handler = self._elicitation_prompt_handler
        if handler is None:
            self._pending_elicitations.pop(request_id, None)
            return CancelElicitationResponse(action="cancel")

        with bind_log_context(chat_id=chat_id, session_id=session_id):
            logger.info("Elicitation requested: request_id=%s questions=%s", request_id, len(questions))
        try:
            try:
                # The handler sends the keyboard, and a send can fail (flood control, a chat that
                # vanished). Declining instead of propagating matches every other case the bot
                # cannot render: the agent falls back to plain text rather than failing its tool.
                await handler(
                    ElicitationRequest(
                        chat_id=chat_id,
                        request_id=request_id,
                        title=form.requested_schema.title or message,
                        questions=questions,
                    )
                )
            except Exception:
                with bind_log_context(chat_id=chat_id, session_id=session_id):
                    logger.exception("Elicitation could not be shown: request_id=%s", request_id)
                return CancelElicitationResponse(action="cancel")
            try:
                response = await asyncio.wait_for(future, timeout=300)
            except TimeoutError:
                with bind_log_context(chat_id=chat_id, session_id=session_id):
                    logger.info("Elicitation timed out: request_id=%s", request_id)
                response = CancelElicitationResponse(action="cancel")
        finally:
            self._pending_elicitations.pop(request_id, None)
            # The question stopped waiting, whether or not it was answered: the UI has to take the
            # keyboard away, otherwise a dead question looks exactly like a live one.
            expiry = self._elicitation_expiry_handler
            if expiry is not None:
                await expiry(chat_id, request_id)
        return response

    @staticmethod
    def _available_actions(options: tuple[PermissionOption, ...]) -> tuple[PermissionDecisionAction, ...]:
        kinds = {option.kind for option in options}
        actions: list[PermissionDecisionAction] = []
        if "allow_always" in kinds:
            actions.append("always")
        if "allow_once" in kinds:
            actions.append("once")
        actions.append("deny")
        return tuple(actions)

    @staticmethod
    def _build_permission_response(
        *,
        options: tuple[PermissionOption, ...],
        action: PermissionDecisionAction,
    ) -> RequestPermissionResponse:
        if action == "deny":
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

        target_kind = "allow_always" if action == "always" else "allow_once"
        for option in options:
            if option.kind == target_kind:
                return RequestPermissionResponse(outcome=AllowedOutcome(option_id=option.option_id, outcome="selected"))
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    @staticmethod
    def _auto_approve_action(options: tuple[PermissionOption, ...]) -> PermissionDecisionAction:
        kinds = {option.kind for option in options}
        if "allow_once" in kinds:
            return "once"
        if "allow_always" in kinds:
            return "always"
        return "deny"

    def _report_permission_event(self, session_id: str, event: str) -> None:
        if self._permission_event_output != "stdout":
            return
        chat_id = self._chat_id_by_session(session_id)
        with bind_log_context(chat_id=chat_id, session_id=session_id):
            logger.info("ACP permission event: %s", event)

    async def _forward_activity_event(self, session_id: str, block: AgentActivityBlock) -> None:
        handler = self._activity_event_handler
        if handler is None:
            return
        chat_id = self._chat_id_by_session(session_id)
        if chat_id is None:
            return
        await handler(chat_id, block)

    async def _store_available_commands(self, session_id: str, commands: tuple[AgentCommand, ...]) -> None:
        # Agents publish this update while `session/new` is still in flight, so no chat mapping exists yet.
        self._commands_by_session[session_id] = commands

    def _chat_id_by_session(self, session_id: str) -> int | None:
        return self._chat_by_session.get(session_id)

    def _resolve_file_uri_resources(self, *, response: AgentReply, workspace: Path) -> AgentReply:
        """Resolve `file://` resources from ACP into binary payloads for Telegram delivery."""
        images = list(response.images)
        files: list[FilePayload] = []
        warnings: list[str] = []
        workspace_root = workspace.resolve()

        for payload in response.files:
            if payload.data_base64 is not None or payload.text_content is None:
                files.append(payload)
                continue
            if not payload.text_content.startswith("file://"):
                files.append(payload)
                continue

            resolved_path, warning = self._resolve_local_file_uri(payload.text_content, workspace_root)
            if warning is not None:
                warnings.append(f"{payload.name}: {warning}")
                continue

            assert resolved_path is not None
            try:
                raw = resolved_path.read_bytes()
            except OSError as exc:
                warnings.append(f"{payload.name}: unreadable file ({exc})")
                continue

            mime_type = payload.mime_type or mimetypes.guess_type(resolved_path.name)[0] or "application/octet-stream"
            if mime_type.startswith("image/"):
                images.append(ImagePayload(data_base64=base64.b64encode(raw).decode("ascii"), mime_type=mime_type))
                continue

            files.append(
                FilePayload(
                    name=payload.name or resolved_path.name,
                    mime_type=mime_type,
                    data_base64=base64.b64encode(raw).decode("ascii"),
                )
            )

        text = response.text
        if warnings:
            warning_text = "\n".join(f"Attachment warning: {warning}" for warning in warnings)
            text = f"{text}\n{warning_text}".strip() if text else warning_text

        return AgentReply(
            text=text,
            activity_blocks=response.activity_blocks,
            images=tuple(images),
            files=tuple(files),
        )

    async def _save_channel_session_mapping(self, *, chat_id: int, session_id: str) -> None:
        if self._channel_state_file is None:
            return
        async with self._channel_state_lock:
            await asyncio.to_thread(self._write_channel_session_mapping, chat_id=chat_id, session_id=session_id)

    async def _drop_channel_session_mapping(self, *, session_id: str) -> None:
        if self._channel_state_file is None:
            return
        async with self._channel_state_lock:
            await asyncio.to_thread(self._remove_channel_session_mapping, session_id=session_id)

    async def record_pending_media(
        self,
        *,
        chat_id: int,
        message_id: int,
        file_id: str,
        mime_type: str | None,
    ) -> None:
        """Queue an unclaimed media reference so a later prompt can fetch it on demand."""

        if self._channel_state_file is None:
            return
        queued_at = datetime.now(UTC).isoformat(timespec="seconds")
        async with self._channel_state_lock:
            await asyncio.to_thread(
                append_pending_media,
                self._channel_state_file,
                chat_id=chat_id,
                file_id=file_id,
                message_id=message_id,
                mime_type=mime_type,
                queued_at=queued_at,
            )

    async def take_pending_media(self, *, chat_id: int) -> tuple[dict[str, object], ...]:
        """Claim and clear the media references queued for a chat."""

        if self._channel_state_file is None:
            return ()
        async with self._channel_state_lock:
            refs = await asyncio.to_thread(take_pending_media, self._channel_state_file, chat_id=chat_id)
        return tuple(refs)

    async def record_group_message(  # noqa: PLR0913
        self,
        *,
        chat_id: int,
        message_id: int,
        sender_id: int | None,
        sender_name: str | None,
        text: str,
        max_entries: int,
    ) -> None:
        """Keep one group message on disk so the agent can read the room later."""

        if self._channel_state_file is None or max_entries <= 0:
            return
        sent_at = datetime.now(UTC).isoformat(timespec="seconds")
        async with self._channel_state_lock:
            await asyncio.to_thread(
                append_group_message,
                self._channel_state_file,
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
                sent_at=sent_at,
                max_entries=max_entries,
            )

    async def read_group_history(self, *, chat_id: int) -> tuple[dict[str, object], ...]:
        """Return the recorded group messages of a chat, oldest first."""

        if self._channel_state_file is None:
            return ()
        async with self._channel_state_lock:
            entries = await asyncio.to_thread(load_group_history, self._channel_state_file, chat_id=chat_id)
        return tuple(entries)

    async def set_prompt_message_context(self, *, session_id: str, message_id: int | None) -> None:
        """Persist the active Telegram prompt message id for the current turn.

        While a turn is in flight (`message_id` is set) the session also becomes the
        last active one, so an MCP attachment call that omits `session_id` resolves to
        the chat that most recently prompted the agent, not merely the one that created
        a session first.
        """

        if self._channel_state_file is None:
            return
        async with self._channel_state_lock:
            await asyncio.to_thread(save_prompt_message_id, self._channel_state_file, session_id, message_id)
            if message_id is not None:
                await asyncio.to_thread(save_last_session_id, self._channel_state_file, session_id)

    def _write_channel_session_mapping(self, *, chat_id: int, session_id: str) -> None:
        assert self._channel_state_file is not None
        mapping = load_session_chat_map(self._channel_state_file)
        mapping[session_id] = chat_id
        save_session_chat_map(self._channel_state_file, mapping)
        save_last_session_id(self._channel_state_file, session_id)

    def _remove_channel_session_mapping(self, *, session_id: str) -> None:
        assert self._channel_state_file is not None
        mapping = load_session_chat_map(self._channel_state_file)
        if session_id not in mapping:
            return
        mapping.pop(session_id, None)
        save_session_chat_map(self._channel_state_file, mapping)
        save_prompt_message_id(self._channel_state_file, session_id, None)
        if load_last_session_id(self._channel_state_file) == session_id:
            save_last_session_id(self._channel_state_file, None)

    @staticmethod
    def _resolve_local_file_uri(uri: str, workspace_root: Path) -> tuple[Path | None, str | None]:
        parsed = urlparse(uri)
        warning: str | None = None
        resolved_path: Path | None = None

        if parsed.scheme != "file":
            warning = f"unsupported URI scheme `{parsed.scheme}`"
        elif parsed.netloc not in {"", "localhost"}:
            warning = f"unsupported file URI host `{parsed.netloc}`"
        else:
            decoded_path = unquote(parsed.path)
            if not decoded_path:
                warning = "empty file URI path"
            else:
                try:
                    resolved = Path(decoded_path).resolve(strict=True)
                except FileNotFoundError:
                    warning = "file not found"
                except OSError as exc:
                    warning = f"invalid file path ({exc})"
                else:
                    if not resolved.is_file():
                        warning = "path is not a file"
                    else:
                        try:
                            resolved.relative_to(workspace_root)
                        except ValueError:
                            warning = "path is outside active workspace"
                        else:
                            resolved_path = resolved

        return resolved_path, warning
