"""Tests for recording group messages into the agent-readable history."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from telegram_acp_bot.acp.echo_service import EchoAgentService
from telegram_acp_bot.core.session_registry import SessionRegistry
from telegram_acp_bot.telegram.bridge import TelegramBridge
from telegram_acp_bot.telegram.config import make_config
from tests.telegram.support import make_context, make_update

pytestmark = pytest.mark.asyncio

GROUP_HISTORY_CAP = 200
GROUP_CHAT_ID = 100


def make_history_bridge(
    *, cap: int = GROUP_HISTORY_CAP, allowed_ids: set[int] | None = None, require_mention: bool = False
) -> tuple[TelegramBridge, EchoAgentService]:
    service = EchoAgentService(SessionRegistry())
    config = make_config(
        token="TOKEN",
        allowed_user_ids=list(allowed_ids or set()),
        workspace=".",
        group_require_mention=require_mention,
        group_history_size=cap,
    )
    return TelegramBridge(config=config, agent_service=service), service


async def recorded(service: EchoAgentService) -> list[dict[str, object]]:
    return list(await service.read_group_history(chat_id=GROUP_CHAT_ID))


async def test_group_message_from_an_unallowed_sender_is_still_recorded():
    bridge, service = make_history_bridge(allowed_ids={99})
    update = make_update(user_id=1, text="ai cần help?", chat_type="group")

    await bridge.on_message(update, make_context())

    assert [entry["text"] for entry in await recorded(service)] == ["ai cần help?"]
    assert update.message is not None
    assert update.message.replies == ["Access denied for this bot."]


async def test_recorded_group_message_keeps_the_sender():
    bridge, service = make_history_bridge()
    update = make_update(user_id=7, username="@Alice", text="trời nóng ghê", chat_type="group", message_id=55)

    await bridge.on_message(update, make_context())

    assert await recorded(service) == [
        {
            "message_id": 55,
            "sender_id": 7,
            "sender_name": "Alice",
            "text": "trời nóng ghê",
            "sent_at": "echo",
        }
    ]


async def test_recorded_group_message_falls_back_to_the_full_name():
    bridge, service = make_history_bridge()
    update = make_update(user_id=7, text="không có username", chat_type="group")
    update.effective_user = SimpleNamespace(id=7, username=None, full_name="Alice Nguyen")

    await bridge.on_message(update, make_context())

    assert (await recorded(service))[0]["sender_name"] == "Alice Nguyen"


async def test_unmentioned_group_message_is_recorded_before_the_mention_gate_silences_it():
    bridge, service = make_history_bridge(require_mention=True)
    update = make_update(text="chuyện nội bộ", chat_type="group")

    await bridge.on_message(update, make_context())

    assert [entry["text"] for entry in await recorded(service)] == ["chuyện nội bộ"]
    assert update.message is not None
    assert update.message.replies == []


async def test_group_media_caption_is_recorded():
    bridge, service = make_history_bridge()
    update = make_update(caption="ảnh này ở đâu", chat_type="group")

    await bridge.on_message(update, make_context())

    assert [entry["text"] for entry in await recorded(service)] == ["ảnh này ở đâu"]


async def test_group_message_without_any_text_is_not_recorded():
    bridge, service = make_history_bridge()

    await bridge.on_message(make_update(chat_type="group"), make_context())

    assert await recorded(service) == []


async def test_private_message_is_not_recorded():
    bridge, service = make_history_bridge()

    await bridge.on_message(make_update(text="riêng tư thôi", chat_type="private"), make_context())

    assert await recorded(service) == []


async def test_update_without_a_message_is_not_recorded():
    bridge, service = make_history_bridge()

    await bridge.on_message(make_update(with_message=False, chat_type="group"), make_context())

    assert await recorded(service) == []


async def test_history_cap_of_zero_records_nothing():
    bridge, service = make_history_bridge(cap=0)

    await bridge.on_message(make_update(text="đừng giữ gì cả", chat_type="group"), make_context())

    assert await recorded(service) == []


async def test_history_drops_the_oldest_message_beyond_the_cap():
    bridge, service = make_history_bridge(cap=2)

    for message_id in (1, 2, 3):
        await bridge.on_message(
            make_update(text=f"msg {message_id}", chat_type="group", message_id=message_id), make_context()
        )

    assert [entry["message_id"] for entry in await recorded(service)] == [2, 3]


async def test_a_replayed_group_message_is_recorded_once():
    bridge, service = make_history_bridge()

    await bridge.on_message(make_update(text="Telegram gửi lại", chat_type="group", message_id=9), make_context())
    await bridge.on_message(make_update(text="Telegram gửi lại", chat_type="group", message_id=9), make_context())

    assert [entry["message_id"] for entry in await recorded(service)] == [9]


async def test_group_agent_command_is_recorded():
    bridge, service = make_history_bridge()

    await bridge.on_agent_command(make_update(text="/goal status", chat_type="group"), make_context())

    assert [entry["text"] for entry in await recorded(service)] == ["/goal status"]
