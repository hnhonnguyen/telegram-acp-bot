from __future__ import annotations

# ruff: noqa: F403, F405, I001

from telegram_acp_bot.telegram.bridge import TelegramBridge
from telegram_acp_bot.telegram.constants import GROUP_BOT_TURN_BUDGET, GROUP_BUDGET_HOLD_TEXT
from tests.telegram.support import *

EXPECTED_ECHO_SUFFIX = "alo"
EXPECTED_IMAGE_ACK = "images=1"


def assert_not_answered(update):
    """The only replies on the message are the session notice, never an agent answer."""
    assert update.message is not None
    assert all(EXPECTED_ECHO_SUFFIX not in reply for reply in update.message.replies)


def make_mention_bridge() -> TelegramBridge:
    return make_bridge(group_require_mention=True)


async def test_group_message_without_mention_is_ignored():
    bridge = make_mention_bridge()
    update = make_update(text="alo", chat_type="group")
    await bridge.new_session(update, make_context())
    await bridge.on_message(update, make_context())

    assert_not_answered(update)


async def test_group_message_with_mention_is_answered():
    bridge = make_mention_bridge()
    update = make_update(text="@echo_bot alo", chat_type="group")
    await bridge.new_session(update, make_context())
    await bridge.on_message(update, make_context())

    assert update.message is not None
    assert update.message.replies[-1].endswith(EXPECTED_ECHO_SUFFIX)


async def test_group_message_mentioning_another_bot_is_ignored():
    bridge = make_mention_bridge()
    update = make_update(text="@other_bot alo", chat_type="group")
    await bridge.new_session(update, make_context())
    await bridge.on_message(update, make_context())

    assert_not_answered(update)


async def test_group_mention_entity_is_answered():
    bridge = make_mention_bridge()
    entity = MessageEntity(type="mention", offset=0, length=9)
    update = make_update(text="@Echo_Bot alo", chat_type="group", entities=[entity])
    await bridge.new_session(update, make_context())
    await bridge.on_message(update, make_context())

    assert update.message is not None
    assert update.message.replies[-1].endswith(EXPECTED_ECHO_SUFFIX)


async def test_group_photo_with_mentioned_caption_is_answered():
    bridge = make_mention_bridge()
    update = make_update(caption="@echo_bot alo", photo=[SimpleNamespace(file_id="p1")], chat_type="group")
    context = make_context()
    context.bot.files["p1"] = b"abc"
    await bridge.new_session(update, make_context())
    await bridge.on_message(update, context)

    assert update.message is not None
    assert EXPECTED_IMAGE_ACK in update.message.replies[-1]


async def test_group_photo_without_mentioned_caption_is_ignored():
    bridge = make_mention_bridge()
    update = make_update(photo=[SimpleNamespace(file_id="p1")], chat_type="group")
    context = make_context()
    context.bot.files["p1"] = b"abc"
    await bridge.new_session(update, make_context())
    await bridge.on_message(update, context)

    assert_not_answered(update)


async def test_group_reply_to_bot_message_is_answered():
    bridge = make_mention_bridge()
    update = make_update(
        text="alo",
        chat_type="group",
        reply_to_message=SimpleNamespace(from_user=SimpleNamespace(username="echo_bot")),
    )
    await bridge.new_session(update, make_context())
    await bridge.on_message(update, make_context())

    assert update.message is not None
    assert update.message.replies[-1].endswith(EXPECTED_ECHO_SUFFIX)


async def test_private_message_without_mention_is_answered():
    bridge = make_mention_bridge()
    update = make_update(text="alo", chat_type="private")
    await bridge.new_session(update, make_context())
    await bridge.on_message(update, make_context())

    assert update.message is not None
    assert update.message.replies[-1].endswith(EXPECTED_ECHO_SUFFIX)


async def test_group_message_without_flag_is_answered():
    bridge = make_bridge()
    update = make_update(text="alo", chat_type="group")
    await bridge.new_session(update, make_context())
    await bridge.on_message(update, make_context())

    assert update.message is not None
    assert update.message.replies[-1].endswith(EXPECTED_ECHO_SUFFIX)


def make_bot_update(*, text: str = "@echo_bot alo"):
    """A group message written by the peer bot sharing this group."""
    return make_update(text=text, chat_type="group", username="peer_bot", is_bot=True)


async def test_group_reply_to_bot_from_a_bot_is_ignored():
    """A bot answer quotes the message it answers, and the quote alone must not summon the peer."""
    bridge = make_mention_bridge()
    update = make_update(
        text="alo",
        chat_type="group",
        username="peer_bot",
        is_bot=True,
        reply_to_message=SimpleNamespace(from_user=SimpleNamespace(username="echo_bot")),
    )
    await bridge.new_session(update, make_context())
    await bridge.on_message(update, make_context())

    assert_not_answered(update)


async def test_bot_message_mentioning_this_bot_is_answered():
    bridge = make_mention_bridge()
    update = make_bot_update()
    await bridge.new_session(update, make_context())
    await bridge.on_message(update, make_context())

    assert update.message is not None
    assert update.message.replies[-1].endswith(EXPECTED_ECHO_SUFFIX)


async def test_bot_turn_budget_stops_a_runaway_exchange():
    bridge = make_mention_bridge()
    await bridge.new_session(make_bot_update(), make_context())

    for _ in range(GROUP_BOT_TURN_BUDGET):
        update = make_bot_update()
        await bridge.on_message(update, make_context())
        assert update.message is not None
        assert update.message.replies[-1].endswith(EXPECTED_ECHO_SUFFIX)

    stopped = make_bot_update()
    await bridge.on_message(stopped, make_context())

    assert stopped.message is not None
    assert_not_answered(stopped)
    assert GROUP_BUDGET_HOLD_TEXT in stopped.message.replies

    again = make_bot_update()
    await bridge.on_message(again, make_context())
    assert again.message is not None
    assert GROUP_BUDGET_HOLD_TEXT not in again.message.replies


async def test_human_message_refills_the_bot_turn_budget():
    bridge = make_mention_bridge()
    await bridge.new_session(make_bot_update(), make_context())
    for _ in range(GROUP_BOT_TURN_BUDGET):
        await bridge.on_message(make_bot_update(), make_context())

    human = make_update(text="@echo_bot alo", chat_type="group", username="nhonnh")
    await bridge.on_message(human, make_context())
    assert human.message is not None
    assert human.message.replies[-1].endswith(EXPECTED_ECHO_SUFFIX)

    after = make_bot_update()
    await bridge.on_message(after, make_context())
    assert after.message is not None
    assert after.message.replies[-1].endswith(EXPECTED_ECHO_SUFFIX)
