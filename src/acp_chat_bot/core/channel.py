"""The outbound surface every chat channel implements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from acp_chat_bot.acp.models import AgentActivityBlock, FilePayload, ImagePayload
from acp_chat_bot.core.chat import ChoiceRequest, DownloadedAttachment, InboundAttachment
from acp_chat_bot.core.ids import ChatId, MessageId


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    """What a channel can render; the bridge degrades when a feature is missing."""

    attachments: bool = False
    choices: bool = False
    editable_messages: bool = False
    streaming_cards: bool = False
    reactions: bool = False
    group_history: bool = True
    max_text_length: int = 4096


class Channel(Protocol):
    """Outbound operations an agent bridge needs from one chat platform."""

    kind: str
    capabilities: ChannelCapabilities

    async def send_text(
        self,
        chat_id: ChatId,
        text: str,
        *,
        reply_to: MessageId | None = None,
    ) -> MessageId | None: ...

    async def send_markdown(
        self,
        chat_id: ChatId,
        text: str,
        *,
        reply_to: MessageId | None = None,
    ) -> MessageId | None: ...

    async def edit_markdown(self, chat_id: ChatId, message_id: MessageId, text: str) -> bool: ...

    async def delete_message(self, chat_id: ChatId, message_id: MessageId) -> None: ...

    async def send_activity(self, chat_id: ChatId, block: AgentActivityBlock) -> None: ...

    async def present_choice(self, chat_id: ChatId, request: ChoiceRequest) -> MessageId | None: ...

    async def update_choice(self, chat_id: ChatId, message_id: MessageId, text: str) -> None: ...

    async def set_typing(self, chat_id: ChatId) -> None: ...

    async def set_reaction(self, chat_id: ChatId, message_id: MessageId, status: str) -> None: ...

    async def send_image(self, chat_id: ChatId, payload: ImagePayload) -> None: ...

    async def send_file(self, chat_id: ChatId, payload: FilePayload) -> None: ...

    async def download(self, attachment: InboundAttachment) -> DownloadedAttachment: ...
