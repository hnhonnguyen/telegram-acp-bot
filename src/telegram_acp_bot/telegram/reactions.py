"""Bot-driven status reactions that track a prompt cycle on its Telegram message."""

from __future__ import annotations

import logging
from enum import StrEnum

from telegram import Bot
from telegram.constants import ReactionEmoji
from telegram.error import TelegramError

from telegram_acp_bot.telegram.config import StatusReactionLevel

logger = logging.getLogger(__name__)


class PromptStatus(StrEnum):
    """Lifecycle moments the bot can mark on the message that triggered a prompt."""

    QUEUED = "queued"
    THINKING = "thinking"
    DONE = "done"
    ERROR = "error"


STATUS_REACTION_EMOJI: dict[PromptStatus, str] = {
    PromptStatus.QUEUED: ReactionEmoji.EYES.value,
    PromptStatus.THINKING: ReactionEmoji.THINKING_FACE.value,
    PromptStatus.DONE: ReactionEmoji.THUMBS_UP.value,
    PromptStatus.ERROR: ReactionEmoji.BROKEN_HEART.value,
}


class StatusReactionSender:
    """Replace the bot's reaction on a prompt message to show cycle progress.

    Delivery failures are logged and swallowed: a reaction is decoration, and losing one
    must never turn into a lost reply.
    """

    def __init__(self, level: StatusReactionLevel) -> None:
        self._level = level

    @property
    def enabled(self) -> bool:
        return self._level != "off"

    async def set(self, *, bot: Bot, chat_id: int, message_id: int, status: PromptStatus) -> None:
        if not self.enabled:
            return
        reaction = STATUS_REACTION_EMOJI[status]
        try:
            await bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction=reaction)
            logger.info("Status reaction %s set on chat_id=%s message_id=%s", status.value, chat_id, message_id)
        except TelegramError as exc:
            logger.warning(
                "Status reaction %s failed for chat_id=%s message_id=%s: %s",
                status.value,
                chat_id,
                message_id,
                exc,
            )
