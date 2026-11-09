from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, cast

import pytest
from dingtalk_stream import AckMessage, CallbackMessage, ChatbotMessage

from acp_chat_bot.channels.dingtalk.api import DingTalkAPI
from acp_chat_bot.channels.dingtalk.channel import DingTalkChannel
from acp_chat_bot.channels.dingtalk.runtime import (
    DEDUPE_MAX_ENTRIES,
    DEDUPE_TTL_SECONDS,
    DingTalkBotHandler,
    DingTalkRuntime,
)
from acp_chat_bot.core.bridge import AgentChatBridge
from acp_chat_bot.core.chat import InboundMessage
from tests.channels.dingtalk.support import FakeAPI, make_config, make_message

pytestmark = pytest.mark.asyncio

MESSAGES_WITHOUT_ID = 2


class FakeBridge:
    agent_name = "qoder"

    def __init__(self, *, error: Exception | None = None, gate: asyncio.Event | None = None) -> None:
        self.error = error
        self.gate = gate
        self.inbound: list[InboundMessage] = []

    async def handle_inbound(self, inbound: InboundMessage) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        self.inbound.append(inbound)


class FakeWebSocket:
    def __init__(self, *, close_error: bool = False) -> None:
        self.close_error = close_error
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        if self.close_error:
            raise RuntimeError("already closed")  # noqa: TRY003


class FakeStreamClient:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.started = asyncio.Event()
        self.stopped = False
        self.websocket: FakeWebSocket | None = None

    def register_callback_handler(self, topic: str, handler: Any) -> None:
        self.handlers[topic] = handler

    async def start(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.stopped = True
            raise


def make_runtime(
    *,
    allowed_ids: frozenset[str] = frozenset({"staff-1"}),
    allowed_nicks: frozenset[str] = frozenset(),
    bridge: FakeBridge | None = None,
) -> tuple[DingTalkRuntime, FakeBridge, FakeAPI]:
    api = FakeAPI()
    config = make_config(allowed_staff_ids=allowed_ids, allowed_nicknames=allowed_nicks)
    channel = DingTalkChannel(config=config, api=cast("DingTalkAPI", api))
    fake_bridge = bridge or FakeBridge()
    runtime = DingTalkRuntime(
        config=config,
        bridge=cast("AgentChatBridge", fake_bridge),
        channel=channel,
        api=cast("DingTalkAPI", api),
    )
    return runtime, fake_bridge, api


def payload(**overrides: Any) -> dict[str, Any]:
    message = make_message(**overrides)
    data = {
        "conversationId": message.conversation_id,
        "conversationType": message.conversation_type,
        "senderStaffId": message.sender_staff_id,
        "senderNick": message.sender_nick,
        "senderId": message.sender_id,
        "msgId": message.message_id,
        "msgtype": message.message_type,
        "text": {"content": "hello"},
        "sessionWebhook": message.session_webhook,
        "sessionWebhookExpiredTime": message.session_webhook_expired_time,
        "isInAtList": message.is_in_at_list,
    }
    data.update(overrides)
    return data


async def drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_the_runtime_exposes_its_bridge():
    runtime, bridge, _ = make_runtime()

    assert runtime.bridge is bridge


async def test_a_callback_is_acked_and_processed_in_the_background():
    runtime, bridge, _ = make_runtime()
    handler = DingTalkBotHandler(runtime)
    callback = CallbackMessage()
    callback.data = payload()

    status, text = await handler.process(callback)

    assert (status, text) == (AckMessage.STATUS_OK, "OK")
    await drain()
    assert [message.text for message in bridge.inbound] == ["hello"]
    assert runtime.channel.target("cid-1") is not None


async def test_the_stream_client_is_started_and_stopped(mocker):
    client = FakeStreamClient()
    mocker.patch("acp_chat_bot.channels.dingtalk.runtime.DingTalkStreamClient", return_value=client)
    runtime, _, api = make_runtime()

    await runtime.start()
    await client.started.wait()

    assert isinstance(client.handlers[ChatbotMessage.TOPIC], DingTalkBotHandler)
    assert client.handlers[ChatbotMessage.TOPIC]._runtime is runtime

    await runtime.stop()
    assert client.stopped is True
    assert api.closed is True


async def test_stop_closes_the_stream_websocket(mocker):
    client = FakeStreamClient()
    websocket = FakeWebSocket()
    client.websocket = websocket
    mocker.patch("acp_chat_bot.channels.dingtalk.runtime.DingTalkStreamClient", return_value=client)
    runtime, _, _ = make_runtime()
    await runtime.start()

    await runtime.stop()

    assert websocket.closed is True


async def test_stop_suppresses_websocket_close_errors(mocker):
    client = FakeStreamClient()
    client.websocket = FakeWebSocket(close_error=True)
    mocker.patch("acp_chat_bot.channels.dingtalk.runtime.DingTalkStreamClient", return_value=client)
    runtime, _, _ = make_runtime()
    await runtime.start()

    await runtime.stop()

    assert runtime._task is None


async def test_stop_cancels_inflight_processing(mocker):
    gate = asyncio.Event()
    runtime, bridge, api = make_runtime(bridge=FakeBridge(gate=gate))
    client = FakeStreamClient()
    mocker.patch("acp_chat_bot.channels.dingtalk.runtime.DingTalkStreamClient", return_value=client)
    await runtime.start()

    runtime.handle_callback(payload())
    await drain()
    assert bridge.inbound == []

    await runtime.stop()
    assert api.closed is True
    assert runtime._inflight == set()


async def test_stop_without_start_only_closes_the_api():
    runtime, _, api = make_runtime()

    await runtime.stop()

    assert api.closed is True


async def test_malformed_callbacks_are_ignored():
    runtime, bridge, _ = make_runtime()

    runtime.handle_callback({"sessionWebhookExpiredTime": None})
    await drain()

    assert bridge.inbound == []


async def test_duplicate_messages_are_processed_once():
    runtime, bridge, _ = make_runtime()

    runtime.handle_callback(payload())
    runtime.handle_callback(payload())
    await drain()

    assert len(bridge.inbound) == 1


async def test_messages_without_an_id_are_not_deduplicated():
    runtime, bridge, _ = make_runtime()

    runtime.handle_callback(payload(msgId=None))
    runtime.handle_callback(payload(msgId=None))
    await drain()

    assert len(bridge.inbound) == MESSAGES_WITHOUT_ID


async def test_callbacks_from_strangers_are_rejected():
    runtime, bridge, _ = make_runtime(allowed_ids=frozenset({"staff-1"}))

    runtime.handle_callback(payload(senderStaffId="staff-2", senderNick="Mallory"))
    await drain()

    assert bridge.inbound == []


async def test_an_empty_allowlist_rejects_everyone():
    runtime, bridge, _ = make_runtime(allowed_ids=frozenset(), allowed_nicks=frozenset())

    runtime.handle_callback(payload())
    await drain()

    assert bridge.inbound == []


async def test_allowed_nicknames_are_matched_case_insensitively():
    runtime, bridge, _ = make_runtime(
        allowed_ids=frozenset(),
        allowed_nicks=frozenset({"Alice"}),
    )

    runtime.handle_callback(payload(senderStaffId="staff-2", senderNick="alice"))
    await drain()

    assert len(bridge.inbound) == 1


async def test_message_without_a_conversation_is_ignored():
    runtime, bridge, _ = make_runtime()

    runtime.handle_callback(payload(conversationId=None))
    await drain()

    assert bridge.inbound == []


async def test_processing_errors_are_logged(mocker):
    log = mocker.patch("acp_chat_bot.channels.dingtalk.runtime.logger")
    runtime, _, _ = make_runtime(bridge=FakeBridge(error=RuntimeError("boom")))

    runtime.handle_callback(payload())
    await drain()

    log.exception.assert_called_once()


async def test_expired_dedupe_entries_are_pruned():
    runtime, bridge, _ = make_runtime()
    now = time.monotonic()
    runtime._seen = OrderedDict([("old", now - DEDUPE_TTL_SECONDS - 1), ("fresh", now)])

    runtime.handle_callback(payload())
    await drain()

    assert "old" not in runtime._seen
    assert "fresh" in runtime._seen
    assert "msg-1" in runtime._seen
    assert len(bridge.inbound) == 1


async def test_the_dedupe_cache_is_bounded():
    runtime, _, _ = make_runtime()
    now = time.monotonic()
    runtime._seen = OrderedDict((f"m{index}", now) for index in range(DEDUPE_MAX_ENTRIES))

    runtime.handle_callback(payload())
    await drain()

    assert len(runtime._seen) == DEDUPE_MAX_ENTRIES
    assert "m0" not in runtime._seen
