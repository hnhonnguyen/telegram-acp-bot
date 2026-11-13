"""DingTalk implementation of the transport-neutral Channel protocol."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from dingtalk_stream import ChatbotMessage

from acp_chat_bot.acp.models import AgentActivityBlock, FilePayload, ImagePayload
from acp_chat_bot.channels.dingtalk.api import DingTalkAPI, DingTalkAPIError
from acp_chat_bot.channels.dingtalk.cards import (
    finished_card_data,
    inputing_card_data,
    merge_segments,
    processing_card_data,
)
from acp_chat_bot.channels.dingtalk.config import DingTalkConfig
from acp_chat_bot.channels.dingtalk.models import (
    ConversationTarget,
    conversation_target,
    webhook_expiry,
)
from acp_chat_bot.channels.dingtalk.render import activity_markdown, choice_markdown, chunk_markdown
from acp_chat_bot.core.channel import ChannelCapabilities
from acp_chat_bot.core.chat import ChoiceRequest, DownloadedAttachment, InboundAttachment
from acp_chat_bot.core.ids import ChatId, MessageId
from acp_chat_bot.mcp.state import load_conversation_target, save_conversation_target

logger = logging.getLogger(__name__)

WEBHOOK_SAFETY_MARGIN = timedelta(seconds=5)
DEFAULT_MARKDOWN_TITLE = "ACP bot"


@dataclass(slots=True)
class _ReplyCard:
    """One AI Card that renders the reply currently being generated."""

    out_track_id: str
    committed: str = ""
    preview: str = ""
    inputing: bool = False


class DingTalkChannel:
    """Render bridge output through DingTalk replies or proactive OpenAPI sends."""

    kind = "dingtalk"

    def __init__(self, *, config: DingTalkConfig, api: DingTalkAPI, state_file: Path | None = None) -> None:
        self._config = config
        self._api = api
        self._state_file = state_file
        self._targets: dict[ChatId, ConversationTarget] = {}
        self._webhooks: dict[ChatId, tuple[str, datetime | None]] = {}
        self._cards: dict[ChatId, _ReplyCard] = {}
        self.capabilities = ChannelCapabilities(
            attachments=True,
            choices=True,
            editable_messages=False,
            streaming_cards=config.streaming_cards,
            reactions=False,
            group_history=True,
            max_text_length=config.max_text_length,
        )

    def remember_conversation(self, message: ChatbotMessage) -> None:
        """Remember where to send later messages after the session webhook expires."""
        target = conversation_target(message)
        if not target.chat_id:
            return
        self._targets[target.chat_id] = target
        if self._state_file is not None:
            save_conversation_target(
                self._state_file,
                chat_id=target.chat_id,
                chat_type=target.chat_type,
                user_id=target.user_id,
                title=target.title,
            )
        if message.session_webhook:
            self._webhooks[target.chat_id] = (message.session_webhook, webhook_expiry(message))

    def target(self, chat_id: ChatId) -> ConversationTarget | None:
        return self._targets.get(chat_id)

    async def send_text(self, chat_id: ChatId, text: str, *, reply_to: MessageId | None = None) -> MessageId | None:
        del reply_to
        for chunk in chunk_markdown(text, self._config.max_text_length):
            await self._send_text_chunk(chat_id, chunk)
        return None

    async def send_markdown(
        self,
        chat_id: ChatId,
        text: str,
        *,
        reply_to: MessageId | None = None,
    ) -> MessageId | None:
        del reply_to
        for chunk in chunk_markdown(text, self._config.max_text_length):
            await self._send_markdown_chunk(chat_id, chunk)
        return None

    async def edit_markdown(self, chat_id: ChatId, message_id: MessageId, text: str) -> bool:
        del chat_id, message_id, text
        return False

    async def delete_message(self, chat_id: ChatId, message_id: MessageId) -> None:
        del chat_id, message_id

    async def send_activity(self, chat_id: ChatId, block: AgentActivityBlock) -> None:
        if block.kind == "reply" and self.capabilities.streaming_cards:
            await self._stream_reply(chat_id, block.text)
            return
        await self.send_markdown(chat_id, activity_markdown(block))

    async def finish_streaming_reply(self, chat_id: ChatId, text: str) -> bool:
        card = self._cards.pop(chat_id, None)
        if card is None:
            return False
        content = text.strip() or merge_segments(card.committed, card.preview)
        try:
            await self._api.update_card_data(
                out_track_id=card.out_track_id,
                card_data=finished_card_data(content),
            )
        except DingTalkAPIError:
            logger.warning("DingTalk AI card finalization failed for chat %s", chat_id, exc_info=True)
            return False
        return True

    async def present_choice(self, chat_id: ChatId, request: ChoiceRequest) -> MessageId | None:
        await self.send_markdown(chat_id, choice_markdown(request))
        return uuid4().hex

    async def update_choice(self, chat_id: ChatId, message_id: MessageId, text: str) -> None:
        del chat_id, message_id, text

    async def set_typing(self, chat_id: ChatId) -> None:
        del chat_id

    async def set_reaction(self, chat_id: ChatId, message_id: MessageId, status: str) -> None:
        del chat_id, message_id, status

    async def send_image(self, chat_id: ChatId, payload: ImagePayload) -> None:
        target = self._require_target(chat_id)
        media_id = await self._api.upload_media(
            _decode_base64(payload.data_base64),
            filename="image",
            mime_type=payload.mime_type,
            media_type="image",
        )
        await self._api.send_image(
            conversation_id=str(target.chat_id),
            conversation_type=target.chat_type,
            user_id=str(target.user_id),
            media_id=media_id,
        )

    async def send_file(self, chat_id: ChatId, payload: FilePayload) -> None:
        target = self._require_target(chat_id)
        data = _payload_bytes(payload)
        media_id = await self._api.upload_media(
            data,
            filename=payload.name,
            mime_type=payload.mime_type or "application/octet-stream",
            media_type="file",
        )
        await self._api.send_file(
            conversation_id=str(target.chat_id),
            conversation_type=target.chat_type,
            user_id=str(target.user_id),
            media_id=media_id,
            filename=payload.name,
        )

    async def download(self, attachment: InboundAttachment) -> DownloadedAttachment:
        data = await self._api.download_message_file(attachment.ref)
        return DownloadedAttachment(data=data, mime_type=attachment.mime_type, name=attachment.name)

    async def _stream_reply(self, chat_id: ChatId, preview: str) -> None:
        text = preview.strip()
        if not text:
            return
        card = self._cards.get(chat_id)
        if card is None:
            card = await self._start_card(chat_id)
            if card is None:
                return
        if card.preview and not text.startswith(card.preview):
            card.committed = merge_segments(card.committed, card.preview)
        card.preview = text
        try:
            if not card.inputing:
                card.inputing = True
                await self._api.update_card_data(
                    out_track_id=card.out_track_id,
                    card_data=inputing_card_data(),
                )
            await self._api.stream_card_content(
                out_track_id=card.out_track_id,
                content=merge_segments(card.committed, text),
            )
        except DingTalkAPIError:
            logger.warning("DingTalk AI card streaming failed for chat %s", chat_id, exc_info=True)

    async def _start_card(self, chat_id: ChatId) -> _ReplyCard | None:
        try:
            target = self._require_target(chat_id)
            out_track_id = uuid4().hex
            await self._api.create_and_deliver_card(
                card_template_id=self._config.card_template_id,
                out_track_id=out_track_id,
                card_data=processing_card_data(),
                conversation_id=str(target.chat_id),
                conversation_type=target.chat_type,
                user_id=str(target.user_id),
            )
        except DingTalkAPIError:
            logger.warning(
                "DingTalk AI card creation failed for chat %s; falling back to one final message",
                chat_id,
                exc_info=True,
            )
            self.capabilities = replace(self.capabilities, streaming_cards=False)
            return None
        card = _ReplyCard(out_track_id=out_track_id)
        self._cards[chat_id] = card
        return card

    async def _send_text_chunk(self, chat_id: ChatId, text: str) -> None:
        target = self._require_target(chat_id)
        webhook = self._webhook(chat_id)
        if webhook is not None:
            await self._api.reply_webhook(webhook, {"msgtype": "text", "text": {"content": text}})
            return
        await self._api.send_text(
            conversation_id=str(target.chat_id),
            conversation_type=target.chat_type,
            user_id=str(target.user_id),
            text=text,
        )

    async def _send_markdown_chunk(self, chat_id: ChatId, text: str) -> None:
        target = self._require_target(chat_id)
        webhook = self._webhook(chat_id)
        title = _markdown_title(text)
        if webhook is not None:
            await self._api.reply_webhook(
                webhook,
                {"msgtype": "markdown", "markdown": {"title": title, "text": text}},
            )
            return
        await self._api.send_markdown(
            conversation_id=str(target.chat_id),
            conversation_type=target.chat_type,
            user_id=str(target.user_id),
            title=title,
            text=text,
        )

    def _require_target(self, chat_id: ChatId) -> ConversationTarget:
        target = self._targets.get(chat_id)
        if target is None:
            target = self._load_target(chat_id)
        if target is None:
            raise DingTalkAPIError(f"Unknown DingTalk conversation {chat_id!r}")  # noqa: TRY003
        return target

    def _load_target(self, chat_id: ChatId) -> ConversationTarget | None:
        if self._state_file is None:
            return None
        stored = load_conversation_target(self._state_file, chat_id)
        if stored is None:
            return None
        title = stored.get("title")
        target = ConversationTarget(
            chat_id=chat_id,
            chat_type=str(stored.get("chat_type") or "private"),
            user_id=str(stored.get("user_id") or ""),
            title=title if isinstance(title, str) else None,
        )
        self._targets[chat_id] = target
        return target

    def _webhook(self, chat_id: ChatId) -> str | None:
        webhook, expires_at = self._webhooks.get(chat_id, (None, None))
        if webhook is None:
            return None
        if expires_at is not None and datetime.now(UTC) + WEBHOOK_SAFETY_MARGIN >= expires_at:
            return None
        return webhook


def _markdown_title(text: str) -> str:
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return first_line[:50] or DEFAULT_MARKDOWN_TITLE


def _decode_base64(value: str) -> bytes:
    return base64.b64decode(value)


def _payload_bytes(payload: FilePayload) -> bytes:
    if payload.data_base64 is not None:
        return _decode_base64(payload.data_base64)
    return (payload.text_content or "").encode("utf-8")
