from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dingtalk_stream import ChatbotMessage

from acp_chat_bot.channels.dingtalk.config import DingTalkConfig


class FakeResponse:
    def __init__(self, *, status: int = 200, text: str = "{}", data: bytes = b"") -> None:
        self.status = status
        self._text = text
        self._data = data

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._data


class FakeSession:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.popleft()

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self.responses.popleft()

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeAPI:
    upload_media_id: str = "media-1"
    download_data: bytes = b"download-bytes"
    replies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    texts: list[dict[str, Any]] = field(default_factory=list)
    markdowns: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[dict[str, Any]] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    closed: bool = False

    async def reply_webhook(self, webhook: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.replies.append((webhook, payload))
        return {}

    async def send_text(self, **kwargs: Any) -> dict[str, Any]:
        self.texts.append(kwargs)
        return {}

    async def send_markdown(self, **kwargs: Any) -> dict[str, Any]:
        self.markdowns.append(kwargs)
        return {}

    async def send_image(self, **kwargs: Any) -> dict[str, Any]:
        self.images.append(kwargs)
        return {}

    async def send_file(self, **kwargs: Any) -> dict[str, Any]:
        self.files.append(kwargs)
        return {}

    async def upload_media(self, data: bytes, **kwargs: Any) -> str:
        self.uploads.append({"data": data, **kwargs})
        return self.upload_media_id

    async def download_message_file(self, download_code: str) -> bytes:
        self.downloads.append(download_code)
        return self.download_data

    async def close(self) -> None:
        self.closed = True


def make_config(
    *,
    allowed_staff_ids: frozenset[str] = frozenset({"staff-1"}),
    allowed_nicknames: frozenset[str] = frozenset(),
    max_text_length: int = 4000,
    group_allow: bool = True,
) -> DingTalkConfig:
    return DingTalkConfig(
        client_id="client-id",
        client_secret="client-secret",
        robot_code="robot-code",
        allowed_staff_ids=allowed_staff_ids,
        allowed_nicknames=allowed_nicknames,
        default_workspace=Path("/workspace"),
        max_text_length=max_text_length,
        group_require_mention=group_allow,
    )


def make_message(**overrides: Any) -> ChatbotMessage:
    payload: dict[str, Any] = {
        "conversationId": "cid-1",
        "conversationType": "1",
        "senderStaffId": "staff-1",
        "senderNick": "Alice",
        "senderId": "sender-1",
        "msgId": "msg-1",
        "msgtype": "text",
        "text": {"content": "hello"},
        "sessionWebhook": "https://example.test/webhook",
        "sessionWebhookExpiredTime": 4102444800000,
        "isInAtList": True,
    }
    payload.update(overrides)
    return ChatbotMessage.from_dict(payload)
