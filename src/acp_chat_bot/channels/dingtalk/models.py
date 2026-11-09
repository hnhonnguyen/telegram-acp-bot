"""Translate DingTalk callback payloads into transport-neutral messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from dingtalk_stream import ChatbotMessage

from acp_chat_bot.core.chat import InboundAttachment, InboundMessage
from acp_chat_bot.core.ids import ChatId, MessageId, UserId

_GROUP_CONVERSATION = "2"
_LEADING_MENTION_RE = re.compile(r"^@\S+\s*")


@dataclass(frozen=True, slots=True)
class ConversationTarget:
    """Where proactive DingTalk messages for one chat must go."""

    chat_id: ChatId
    chat_type: str
    user_id: UserId
    title: str | None = None


def conversation_target(message: ChatbotMessage) -> ConversationTarget:
    return ConversationTarget(
        chat_id=message.conversation_id or "",
        chat_type="group" if message.conversation_type == _GROUP_CONVERSATION else "private",
        user_id=message.sender_staff_id or message.sender_id or "",
        title=message.conversation_title,
    )


def webhook_expiry(message: ChatbotMessage) -> datetime | None:
    raw = message.session_webhook_expired_time
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def inbound_message(message: ChatbotMessage) -> InboundMessage | None:
    chat_id = message.conversation_id
    if not chat_id:
        return None
    text = "\n".join(item for item in (message.get_text_list() or []) if item).strip()
    is_group = message.conversation_type == _GROUP_CONVERSATION
    if is_group and text.startswith("@"):
        text = _LEADING_MENTION_RE.sub("", text, count=1).strip()
    message_id: MessageId = message.message_id or uuid4().hex
    user_id: UserId = message.sender_staff_id or message.sender_id or "unknown"
    return InboundMessage(
        chat_id=chat_id,
        chat_type="group" if is_group else "private",
        message_id=message_id,
        user_id=user_id,
        user_name=message.sender_nick,
        text=text,
        attachments=_attachments(message),
        mentioned_bot=bool(message.is_in_at_list),
    )


def _attachments(message: ChatbotMessage) -> tuple[InboundAttachment, ...]:
    attachments = [
        InboundAttachment(ref=download_code, kind="image", mime_type="image/jpeg")
        for download_code in message.get_image_list() or []
    ]
    content = message.extensions.get("content")
    if isinstance(content, dict):
        download_code = content.get("downloadCode")
        file_name = content.get("fileName")
        file_type = content.get("fileType") or content.get("mimeType")
        if isinstance(download_code, str) and download_code:
            kind = "image" if message.message_type == "picture" else "file"
            attachments.append(
                InboundAttachment(
                    ref=download_code,
                    kind=kind,
                    mime_type=file_type if isinstance(file_type, str) else None,
                    name=file_name if isinstance(file_name, str) else None,
                )
            )
    return tuple(attachments)
