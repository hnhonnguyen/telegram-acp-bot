"""DingTalk Stream mode lifecycle and inbound callback routing."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from contextlib import suppress
from typing import Any

from dingtalk_stream import (
    AckMessage,
    CallbackHandler,
    CallbackMessage,
    ChatbotMessage,
    Credential,
    DingTalkStreamClient,
)

from acp_chat_bot.channels.dingtalk.api import DingTalkAPI
from acp_chat_bot.channels.dingtalk.channel import DingTalkChannel
from acp_chat_bot.channels.dingtalk.config import DingTalkConfig
from acp_chat_bot.channels.dingtalk.models import inbound_message
from acp_chat_bot.core.bridge import AgentChatBridge
from acp_chat_bot.core.chat import InboundMessage
from acp_chat_bot.logging_context import bind_log_context

logger = logging.getLogger(__name__)

DEDUPE_TTL_SECONDS = 300.0
DEDUPE_MAX_ENTRIES = 2000
STOP_CANCEL_ATTEMPTS = 5
STOP_TASK_TIMEOUT_SECONDS = 5.0


class DingTalkBotHandler(CallbackHandler):
    """Acknowledge DingTalk callbacks immediately and process them in the background."""

    def __init__(self, runtime: DingTalkRuntime) -> None:
        super().__init__()
        self._runtime = runtime

    async def process(self, message: CallbackMessage) -> tuple[int, str]:
        self._runtime.handle_callback(message.data)
        return AckMessage.STATUS_OK, "OK"


class DingTalkRuntime:
    """Run the Stream client for one DingTalk robot."""

    def __init__(
        self,
        *,
        config: DingTalkConfig,
        bridge: AgentChatBridge,
        channel: DingTalkChannel,
        api: DingTalkAPI,
    ) -> None:
        self._config = config
        self._bridge = bridge
        self._channel = channel
        self._api = api
        self._client: DingTalkStreamClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()
        self._seen: OrderedDict[str, float] = OrderedDict()

    @property
    def bridge(self) -> AgentChatBridge:
        return self._bridge

    @property
    def channel(self) -> DingTalkChannel:
        return self._channel

    async def start(self) -> None:
        self._client = DingTalkStreamClient(Credential(self._config.client_id, self._config.client_secret))
        self._client.register_callback_handler(ChatbotMessage.TOPIC, DingTalkBotHandler(self))
        self._task = asyncio.create_task(self._client.start())
        logger.info("DingTalk Stream client started for robot %s", self._config.robot_code)

    async def stop(self) -> None:
        await self._close_websocket()
        task = self._task
        self._task = None
        if task is not None:
            await _cancel_task(task)
        for inflight in tuple(self._inflight):
            inflight.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        await self._api.close()

    async def _close_websocket(self) -> None:
        websocket = None if self._client is None else self._client.websocket
        if websocket is not None:
            with suppress(Exception):
                await websocket.close()

    def handle_callback(self, data: dict[str, Any]) -> None:
        """Parse, filter and schedule one Stream callback without blocking its ack."""
        try:
            message = ChatbotMessage.from_dict(data)
        except Exception:
            logger.exception("Ignoring malformed DingTalk callback")
            return
        if self._is_duplicate(message.message_id):
            logger.debug("Ignoring duplicate DingTalk message %s", message.message_id)
            return
        if not self._is_allowed(message.sender_staff_id, message.sender_nick):
            logger.warning(
                "DingTalk access denied for staff_id=%s nick=%s",
                message.sender_staff_id,
                message.sender_nick,
            )
            return
        self._channel.remember_conversation(message)
        inbound = inbound_message(message)
        if inbound is None:
            logger.debug("Ignoring DingTalk message without a conversation id")
            return
        task = asyncio.create_task(self._run_inbound(inbound))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _run_inbound(self, inbound: InboundMessage) -> None:
        with bind_log_context(agent=self._bridge.agent_name, chat_id=inbound.chat_id):
            try:
                await self._bridge.handle_inbound(inbound)
            except Exception:
                logger.exception("DingTalk message processing failed")

    def _is_allowed(self, staff_id: str | None, nickname: str | None) -> bool:
        allowed_ids = self._config.allowed_staff_ids
        allowed_nicks = {name.casefold() for name in self._config.allowed_nicknames}
        if not allowed_ids and not allowed_nicks:
            return False
        if staff_id is not None and str(staff_id) in allowed_ids:
            return True
        return nickname is not None and nickname.casefold() in allowed_nicks

    def _is_duplicate(self, message_id: str | None) -> bool:
        if not message_id:
            return False
        now = time.monotonic()
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        while len(self._seen) > DEDUPE_MAX_ENTRIES:
            self._seen.popitem(last=False)
        cutoff = now - DEDUPE_TTL_SECONDS
        while self._seen:
            _, seen_at = next(iter(self._seen.items()))
            if seen_at >= cutoff:
                break
            self._seen.popitem(last=False)
        return False


async def _cancel_task(task: asyncio.Task[None]) -> None:
    """Cancel the SDK stream task, which swallows the first cancellation and reconnects."""
    for _ in range(STOP_CANCEL_ATTEMPTS):
        task.cancel()
        await asyncio.sleep(0)
        if task.done():
            break
    with suppress(asyncio.CancelledError, Exception):
        await asyncio.wait({task}, timeout=STOP_TASK_TIMEOUT_SECONDS)
        task.exception()
