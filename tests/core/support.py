from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

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
from acp_chat_bot.core.channel import ChannelCapabilities
from acp_chat_bot.core.chat import ChoiceRequest, DownloadedAttachment, InboundAttachment
from acp_chat_bot.core.ids import ChatId, MessageId, UserId
from acp_chat_bot.scheduled_tasks import ScheduledTaskStore


@dataclass
class SentMessage:
    kind: str
    chat_id: ChatId
    text: str
    reply_to: MessageId | None
    message_id: MessageId


class FakeAgentService:
    def __init__(self) -> None:
        self.workspaces: dict[ChatId, Path] = {}
        self.sessions: dict[ChatId, str] = {}
        self.prompts: list[tuple[ChatId, str, tuple[PromptImage, ...], tuple[PromptFile, ...]]] = []
        self.reply: AgentReply | None = AgentReply(text="agent reply")
        self.prompt_gate: asyncio.Event | None = None
        self.prompt_started = asyncio.Event()
        self.prompt_error: Exception | None = None
        self.new_session_error: Exception | None = None
        self.load_session_error: Exception | None = None
        self.new_session_calls: list[tuple[ChatId, Path]] = []
        self.load_session_calls: list[tuple[ChatId, str, Path]] = []
        self.cancel_result = True
        self.stop_result = True
        self.clear_result = True
        self.commands: tuple[AgentCommand, ...] | None = ()
        self.resume_candidates: tuple[ResumableSession, ...] | None = ()
        self.supports_loading: bool | None = True
        self.group_messages: dict[ChatId, list[dict[str, object]]] = {}
        self.permission_responses: list[tuple[ChatId, str, PermissionDecisionAction]] = []
        self.elicitation_responses: list[tuple[ChatId, str, Mapping[str, tuple[str, ...]] | None]] = []
        self.permission_handler: Callable[[PermissionRequest], Awaitable[None]] | None = None
        self.activity_handler: Callable[[ChatId, AgentActivityBlock], Awaitable[None]] | None = None
        self.elicitation_handler: Callable[[ElicitationRequest], Awaitable[None]] | None = None
        self.expiry_handler: Callable[[ChatId, str], Awaitable[None]] | None = None
        self.permission_policy: PermissionPolicy | None = None

    async def new_session(self, *, chat_id: ChatId, workspace: Path) -> str:
        if self.new_session_error is not None:
            raise self.new_session_error
        self.new_session_calls.append((chat_id, workspace))
        self.workspaces[chat_id] = workspace
        self.sessions[chat_id] = f"session-{len(self.new_session_calls)}"
        return self.sessions[chat_id]

    async def load_session(self, *, chat_id: ChatId, session_id: str, workspace: Path) -> str:
        if self.load_session_error is not None:
            raise self.load_session_error
        self.load_session_calls.append((chat_id, session_id, workspace))
        self.workspaces[chat_id] = workspace
        self.sessions[chat_id] = session_id
        return session_id

    async def list_resumable_sessions(
        self,
        *,
        chat_id: ChatId,
        workspace: Path | None = None,
    ) -> tuple[ResumableSession, ...] | None:
        objects = self.resume_candidates
        if objects is None:
            return None
        if workspace is None:
            return objects
        return tuple(item for item in objects if item.workspace == workspace)

    async def prompt(
        self,
        *,
        chat_id: ChatId,
        text: str,
        images: tuple[PromptImage, ...] = (),
        files: tuple[PromptFile, ...] = (),
    ) -> AgentReply | None:
        self.prompts.append((chat_id, text, images, files))
        self.prompt_started.set()
        if self.prompt_gate is not None:
            await self.prompt_gate.wait()
        if self.prompt_error is not None:
            raise self.prompt_error
        return self.reply

    def get_workspace(self, *, chat_id: ChatId) -> Path | None:
        return self.workspaces.get(chat_id)

    def get_active_session_context(self, *, chat_id: ChatId) -> tuple[str, Path] | None:
        workspace = self.workspaces.get(chat_id)
        session_id = self.sessions.get(chat_id)
        if workspace is None or session_id is None:
            return None
        return session_id, workspace

    def supports_session_loading(self, *, chat_id: ChatId) -> bool | None:
        del chat_id
        return self.supports_loading

    async def cancel(self, *, chat_id: ChatId) -> bool:
        del chat_id
        return self.cancel_result

    async def stop(self, *, chat_id: ChatId) -> bool:
        del chat_id
        return self.stop_result

    async def clear(self, *, chat_id: ChatId) -> bool:
        del chat_id
        return self.clear_result

    def get_permission_policy(self, *, chat_id: ChatId) -> PermissionPolicy | None:
        del chat_id
        return self.permission_policy

    def get_available_commands(self, *, chat_id: ChatId) -> tuple[AgentCommand, ...] | None:
        del chat_id
        return self.commands

    async def set_session_permission_mode(self, *, chat_id: ChatId, mode: PermissionMode) -> bool:
        del chat_id, mode
        return True

    async def set_next_prompt_auto_approve(self, *, chat_id: ChatId, enabled: bool) -> bool:
        del chat_id, enabled
        return True

    def set_permission_request_handler(
        self,
        handler: Callable[[PermissionRequest], Awaitable[None]] | None,
    ) -> None:
        self.permission_handler = handler

    def set_activity_event_handler(
        self,
        handler: Callable[[ChatId, AgentActivityBlock], Awaitable[None]] | None,
    ) -> None:
        self.activity_handler = handler

    def set_elicitation_request_handler(
        self,
        handler: Callable[[ElicitationRequest], Awaitable[None]] | None,
    ) -> None:
        self.elicitation_handler = handler

    def set_elicitation_expiry_handler(
        self,
        handler: Callable[[ChatId, str], Awaitable[None]] | None,
    ) -> None:
        self.expiry_handler = handler

    async def respond_permission_request(
        self,
        *,
        chat_id: ChatId,
        request_id: str,
        action: PermissionDecisionAction,
    ) -> bool:
        self.permission_responses.append((chat_id, request_id, action))
        return True

    async def respond_elicitation_request(
        self,
        *,
        chat_id: ChatId,
        request_id: str,
        answers: Mapping[str, tuple[str, ...]] | None,
    ) -> bool:
        self.elicitation_responses.append((chat_id, request_id, answers))
        return True

    async def emit_permission(self, request: PermissionRequest) -> None:
        assert self.permission_handler is not None
        await self.permission_handler(request)

    async def emit_activity(self, chat_id: ChatId, block: AgentActivityBlock) -> None:
        assert self.activity_handler is not None
        await self.activity_handler(chat_id, block)

    async def emit_elicitation(self, request: ElicitationRequest) -> None:
        assert self.elicitation_handler is not None
        await self.elicitation_handler(request)

    async def emit_expiry(self, chat_id: ChatId, request_id: str) -> None:
        assert self.expiry_handler is not None
        await self.expiry_handler(chat_id, request_id)

    async def record_group_message(  # noqa: PLR0913
        self,
        *,
        chat_id: ChatId,
        message_id: MessageId,
        sender_id: UserId | None,
        sender_name: str | None,
        text: str,
        max_entries: int,
    ) -> None:
        del max_entries
        self.group_messages.setdefault(chat_id, []).append(
            {
                "message_id": message_id,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "text": text,
            }
        )

    async def read_group_history(self, *, chat_id: ChatId) -> tuple[dict[str, object], ...]:
        return tuple(self.group_messages.get(chat_id, ()))


@dataclass
class FakeChannel:
    capabilities: ChannelCapabilities = field(
        default_factory=lambda: ChannelCapabilities(
            attachments=True,
            choices=True,
            editable_messages=True,
            streaming_cards=False,
            reactions=True,
            group_history=True,
        )
    )
    kind: str = "fake"
    edit_result: bool = True
    finish_streaming_result: bool = False
    choice_message_id: MessageId | None = 1
    sent: list[SentMessage] = field(default_factory=list)
    finished_streams: list[tuple[ChatId, str]] = field(default_factory=list)
    activities: list[tuple[ChatId, AgentActivityBlock]] = field(default_factory=list)
    choices: list[tuple[ChatId, ChoiceRequest]] = field(default_factory=list)
    updates: list[tuple[ChatId, MessageId, str]] = field(default_factory=list)
    deleted: list[tuple[ChatId, MessageId]] = field(default_factory=list)
    typing: list[ChatId] = field(default_factory=list)
    reactions: list[tuple[ChatId, MessageId, str]] = field(default_factory=list)
    images: list[tuple[ChatId, object]] = field(default_factory=list)
    files: list[tuple[ChatId, object]] = field(default_factory=list)
    downloads: list[InboundAttachment] = field(default_factory=list)
    _next_message_id: int = 1

    def _message_id(self) -> int:
        message_id = self._next_message_id
        self._next_message_id += 1
        return message_id

    async def send_text(self, chat_id: ChatId, text: str, *, reply_to: MessageId | None = None) -> MessageId:
        message_id = self._message_id()
        self.sent.append(SentMessage("text", chat_id, text, reply_to, message_id))
        return message_id

    async def send_markdown(self, chat_id: ChatId, text: str, *, reply_to: MessageId | None = None) -> MessageId:
        message_id = self._message_id()
        self.sent.append(SentMessage("markdown", chat_id, text, reply_to, message_id))
        return message_id

    async def edit_markdown(self, chat_id: ChatId, message_id: MessageId, text: str) -> bool:
        self.updates.append((chat_id, message_id, text))
        return self.edit_result

    async def finish_streaming_reply(self, chat_id: ChatId, text: str) -> bool:
        self.finished_streams.append((chat_id, text))
        return self.finish_streaming_result

    async def delete_message(self, chat_id: ChatId, message_id: MessageId) -> None:
        self.deleted.append((chat_id, message_id))

    async def send_activity(self, chat_id: ChatId, block: AgentActivityBlock) -> None:
        self.activities.append((chat_id, block))

    async def present_choice(self, chat_id: ChatId, request: ChoiceRequest) -> MessageId | None:
        self.choices.append((chat_id, request))
        return self.choice_message_id

    async def update_choice(self, chat_id: ChatId, message_id: MessageId, text: str) -> None:
        self.updates.append((chat_id, message_id, text))

    async def set_typing(self, chat_id: ChatId) -> None:
        self.typing.append(chat_id)

    async def set_reaction(self, chat_id: ChatId, message_id: MessageId, status: str) -> None:
        self.reactions.append((chat_id, message_id, status))

    async def send_image(self, chat_id: ChatId, payload: object) -> None:
        self.images.append((chat_id, payload))

    async def send_file(self, chat_id: ChatId, payload: object) -> None:
        self.files.append((chat_id, payload))

    async def download(self, attachment: InboundAttachment) -> DownloadedAttachment:
        self.downloads.append(attachment)
        return DownloadedAttachment(
            data=b"attachment-bytes",
            mime_type=attachment.mime_type,
            name=attachment.name,
        )


def make_store(tmp_path: Path) -> ScheduledTaskStore:
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    return store
