"""The ACP session surface the shared bridge consumes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Protocol

from acp_chat_bot.acp.models import (
    AgentActivityBlock,
    AgentCommand,
    AgentReply,
    ElicitationRequest,
    PermissionDecisionAction,
    PermissionMode,
    PermissionPolicy,
    PermissionRequest,
    PromptFile,
    PromptImage,
    ResumableSession,
)
from acp_chat_bot.core.ids import ChatId, MessageId, UserId


class ChatAgentService(Protocol):
    """Minimal agent service surface used by {py:class}`AgentChatBridge`."""

    async def new_session(self, *, chat_id: ChatId, workspace: Path) -> str: ...

    async def load_session(self, *, chat_id: ChatId, session_id: str, workspace: Path) -> str: ...

    async def list_resumable_sessions(
        self,
        *,
        chat_id: ChatId,
        workspace: Path | None = None,
    ) -> tuple[ResumableSession, ...] | None: ...

    async def prompt(
        self,
        *,
        chat_id: ChatId,
        text: str,
        images: tuple[PromptImage, ...] = (),
        files: tuple[PromptFile, ...] = (),
    ) -> AgentReply | None: ...

    def get_workspace(self, *, chat_id: ChatId) -> Path | None: ...

    def get_active_session_context(self, *, chat_id: ChatId) -> tuple[str, Path] | None: ...

    def supports_session_loading(self, *, chat_id: ChatId) -> bool | None: ...

    async def cancel(self, *, chat_id: ChatId) -> bool: ...

    async def stop(self, *, chat_id: ChatId) -> bool: ...

    async def clear(self, *, chat_id: ChatId) -> bool: ...

    def get_permission_policy(self, *, chat_id: ChatId) -> PermissionPolicy | None: ...

    def get_available_commands(self, *, chat_id: ChatId) -> tuple[AgentCommand, ...] | None: ...

    async def set_session_permission_mode(self, *, chat_id: ChatId, mode: PermissionMode) -> bool: ...

    async def set_next_prompt_auto_approve(self, *, chat_id: ChatId, enabled: bool) -> bool: ...

    def set_permission_request_handler(
        self,
        handler: Callable[[PermissionRequest], Awaitable[None]] | None,
    ) -> None: ...

    def set_activity_event_handler(
        self,
        handler: Callable[[ChatId, AgentActivityBlock], Awaitable[None]] | None,
    ) -> None: ...

    def set_elicitation_request_handler(
        self,
        handler: Callable[[ElicitationRequest], Awaitable[None]] | None,
    ) -> None: ...

    def set_elicitation_expiry_handler(
        self,
        handler: Callable[[ChatId, str], Awaitable[None]] | None,
    ) -> None: ...

    async def respond_permission_request(
        self,
        *,
        chat_id: ChatId,
        request_id: str,
        action: PermissionDecisionAction,
    ) -> bool: ...

    async def respond_elicitation_request(
        self,
        *,
        chat_id: ChatId,
        request_id: str,
        answers: Mapping[str, tuple[str, ...]] | None,
    ) -> bool: ...

    async def record_group_message(  # noqa: PLR0913
        self,
        *,
        chat_id: ChatId,
        message_id: MessageId,
        sender_id: UserId | None,
        sender_name: str | None,
        text: str,
        max_entries: int,
    ) -> None: ...

    async def read_group_history(self, *, chat_id: ChatId) -> tuple[dict[str, object], ...]: ...
