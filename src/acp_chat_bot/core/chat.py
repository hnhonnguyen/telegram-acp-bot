"""Transport-neutral chat events and choice prompts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from acp_chat_bot.core.ids import ChatId, MessageId, UserId

ChatType = Literal["private", "group"]
AttachmentKind = Literal["image", "file"]
ChoiceKind = Literal["permission", "elicitation", "busy", "resume", "scheduled", "commands"]


@dataclass(frozen=True, slots=True)
class InboundAttachment:
    """One attachment carried by an inbound message, still to be downloaded."""

    ref: str
    kind: AttachmentKind
    mime_type: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One message a channel accepted for this bridge."""

    chat_id: ChatId
    chat_type: ChatType
    message_id: MessageId
    user_id: UserId
    user_name: str | None
    text: str
    attachments: tuple[InboundAttachment, ...] = ()
    mentioned_bot: bool = False


@dataclass(frozen=True, slots=True)
class DownloadedAttachment:
    """Attachment bytes fetched by a channel."""

    data: bytes
    mime_type: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ChoiceOption:
    """One selectable answer, with the value sent back to the agent."""

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class ChoiceRequest:
    """A question a channel can render as buttons or numbered text."""

    request_id: str
    kind: ChoiceKind
    title: str
    body: str
    options: tuple[ChoiceOption, ...]
    multiple: bool = False
    skippable: bool = False
