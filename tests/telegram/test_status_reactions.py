from __future__ import annotations

# ruff: noqa: F403, F405, I001

import logging
from types import SimpleNamespace
from typing import cast

import pytest
from telegram.error import TelegramError
from telegram_acp_bot.mcp.tools.reactions import STANDARD_REACTION_EMOJIS
from telegram_acp_bot.telegram.config import StatusReactionLevel
from telegram_acp_bot.telegram.reactions import STATUS_REACTION_EMOJI, PromptStatus, StatusReactionSender
from tests.telegram.support import *

BUSY_CHAT_LOCK_OWNER_MESSAGE_ID = 11
QUEUED_TARGET_MESSAGE_ID = 7
REACTION_WARNING_MARKER = "Status reaction"
ECHO_PROMPT_TEXT = "hello"


class ReactionNotAllowedError(TelegramError):
    """Telegram rejects the reaction, for example in a chat with reactions disabled."""

    def __init__(self) -> None:
        super().__init__("Bad Request: reaction not allowed")


class ReactionFailingBot(DummyBot):
    """Bot double whose reaction calls always fail."""

    async def set_message_reaction(self, **kwargs: object) -> None:
        del kwargs
        raise ReactionNotAllowedError


class LimitErrorPromptService:
    """Prompt always fails with the ACP stdio limit error, so the cycle ends without a reply."""

    async def new_session(self, *, chat_id: int, workspace):
        del workspace
        return f"s-{chat_id}"

    async def prompt(self, *, chat_id: int, text: str, images=(), files=()):
        del chat_id, text, images, files
        raise AgentOutputLimitExceededError(ACP_STDIO_LIMIT_ERROR)

    def get_workspace(self, *, chat_id: int):
        del chat_id

    async def cancel(self, *, chat_id: int) -> bool:
        del chat_id
        return False

    async def stop(self, *, chat_id: int) -> bool:
        del chat_id
        return False

    async def clear(self, *, chat_id: int) -> bool:
        del chat_id
        return False

    def get_permission_policy(self, *, chat_id: int):
        del chat_id

    async def set_session_permission_mode(self, *, chat_id: int, mode):
        del chat_id, mode
        return False

    async def set_next_prompt_auto_approve(self, *, chat_id: int, enabled: bool):
        del chat_id, enabled
        return False


def make_reaction_bridge(
    level: StatusReactionLevel = "minimal",
    *,
    bot: DummyBot | None = None,
    service: object | None = None,
    with_app: bool = True,
) -> tuple[TelegramBridge, DummyBot]:
    config = make_config(
        token="TOKEN",
        allowed_user_ids=[],
        workspace=".",
        status_reactions=level,
    )
    agent_service = EchoAgentService(SessionRegistry()) if service is None else service
    bridge = TelegramBridge(config=config, agent_service=cast(AgentService, agent_service))
    dummy_bot = DummyBot() if bot is None else bot
    if with_app:
        bridge._app = cast(Application, SimpleNamespace(bot=dummy_bot))
    return bridge, dummy_bot


def reaction_emojis(bot: DummyBot) -> list[str]:
    return [cast(str, entry["reaction"]) for entry in bot.reactions]


async def test_accepted_prompt_is_reacted_to_while_thinking_and_when_done():
    bridge, bot = make_reaction_bridge()
    update = make_update(text=ECHO_PROMPT_TEXT, message_id=BUSY_CHAT_LOCK_OWNER_MESSAGE_ID)

    await bridge.on_message(update, make_context())

    assert reaction_emojis(bot) == [
        STATUS_REACTION_EMOJI[PromptStatus.THINKING],
        STATUS_REACTION_EMOJI[PromptStatus.DONE],
    ]
    assert all(entry["message_id"] == BUSY_CHAT_LOCK_OWNER_MESSAGE_ID for entry in bot.reactions)
    assert all(entry["chat_id"] == TEST_CHAT_ID for entry in bot.reactions)


async def test_prompt_that_arrives_while_the_chat_is_busy_is_marked_queued():
    bridge, bot = make_reaction_bridge()
    await bridge.on_message(make_update(text=ECHO_PROMPT_TEXT), make_context())
    lock = bridge._chat_prompt_lock(TEST_CHAT_ID)
    await lock.acquire()
    try:
        await bridge.on_message(make_update(text="second", message_id=QUEUED_MESSAGE_ID), make_context())
    finally:
        lock.release()

    assert reaction_emojis(bot)[-1] == STATUS_REACTION_EMOJI[PromptStatus.QUEUED]
    assert bot.reactions[-1]["message_id"] == QUEUED_MESSAGE_ID


async def test_failed_prompt_cycle_is_marked_with_a_broken_heart():
    bridge, bot = make_reaction_bridge(service=LimitErrorPromptService())
    update = make_update(text=ECHO_PROMPT_TEXT)

    await bridge.on_message(update, make_context())

    assert reaction_emojis(bot) == [
        STATUS_REACTION_EMOJI[PromptStatus.THINKING],
        STATUS_REACTION_EMOJI[PromptStatus.ERROR],
    ]


async def test_off_level_never_reacts_from_the_bot():
    bridge, bot = make_reaction_bridge("off")

    await bridge.on_message(make_update(text=ECHO_PROMPT_TEXT), make_context())

    assert bot.reactions == []


async def test_headless_bridge_still_replies_without_reacting():
    bridge, _bot = make_reaction_bridge(with_app=False)
    update = make_update(text=ECHO_PROMPT_TEXT)

    await bridge.on_message(update, make_context())

    assert update.message is not None
    assert ECHO_PROMPT_TEXT in update.message.replies[-1]


async def test_update_without_a_prompt_message_is_not_reacted_to():
    bridge, bot = make_reaction_bridge()

    await bridge._set_status_reaction(
        chat_id=TEST_CHAT_ID,
        update=make_update(with_message=False),
        status=PromptStatus.THINKING,
    )

    assert bot.reactions == []


async def test_reaction_delivery_failure_does_not_lose_the_reply(caplog):
    bridge, _bot = make_reaction_bridge(bot=ReactionFailingBot())
    update = make_update(text=ECHO_PROMPT_TEXT)

    with caplog.at_level(logging.WARNING):
        await bridge.on_message(update, make_context())

    assert update.message is not None
    assert ECHO_PROMPT_TEXT in update.message.replies[-1]
    assert REACTION_WARNING_MARKER in caplog.text


def test_off_level_sender_reports_itself_disabled():
    assert StatusReactionSender("off").enabled is False


@pytest.mark.asyncio
async def test_disabled_sender_does_not_call_the_api():
    bot = DummyBot()

    await StatusReactionSender("off").set(
        bot=cast(Bot, bot), chat_id=TEST_CHAT_ID, message_id=QUEUED_TARGET_MESSAGE_ID, status=PromptStatus.DONE
    )

    assert bot.reactions == []


@pytest.mark.asyncio
async def test_enabled_sender_reacts_on_the_given_message(caplog):
    bot = DummyBot()

    with caplog.at_level(logging.INFO):
        await StatusReactionSender("minimal").set(
            bot=cast(Bot, bot), chat_id=TEST_CHAT_ID, message_id=QUEUED_TARGET_MESSAGE_ID, status=PromptStatus.QUEUED
        )

    assert bot.reactions == [
        {
            "chat_id": TEST_CHAT_ID,
            "message_id": QUEUED_TARGET_MESSAGE_ID,
            "reaction": STATUS_REACTION_EMOJI[PromptStatus.QUEUED],
        }
    ]
    assert "Status reaction queued set" in caplog.text


@pytest.mark.asyncio
async def test_sender_logs_a_delivery_failure(caplog):
    bot = ReactionFailingBot()

    with caplog.at_level(logging.WARNING):
        await StatusReactionSender("minimal").set(
            bot=cast(Bot, bot), chat_id=TEST_CHAT_ID, message_id=QUEUED_TARGET_MESSAGE_ID, status=PromptStatus.DONE
        )

    assert REACTION_WARNING_MARKER in caplog.text


def test_every_status_reaction_emoji_is_a_standard_telegram_reaction():
    assert all(emoji in STANDARD_REACTION_EMOJIS for emoji in STATUS_REACTION_EMOJI.values())
