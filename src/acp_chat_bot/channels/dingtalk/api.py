"""Async HTTP client for DingTalk Stream replies and proactive sends."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from uuid import uuid4

import aiohttp

from acp_chat_bot.channels.dingtalk.cards import CARD_CONTENT_KEY

DINGTALK_TIMEOUT_SECONDS = 30.0
TOKEN_EXPIRY_MARGIN_SECONDS = 60.0
HTTP_ERROR_STATUS = 400


class DingTalkAPIError(RuntimeError):
    """Raised when a DingTalk HTTP call fails."""


class DingTalkAPI:
    """Small async wrapper around the DingTalk OpenAPI endpoints we use."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        robot_code: str,
        base_url: str = "https://api.dingtalk.com",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._robot_code = robot_code
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def get_access_token(self) -> str:
        async with self._token_lock:
            if self._token is not None and time.monotonic() < self._token_expires_at:
                return self._token
            payload = await self._request(
                "POST",
                "/v1.0/oauth2/accessToken",
                json_body={"appKey": self._client_id, "appSecret": self._client_secret},
                authenticated=False,
            )
            token = payload.get("accessToken")
            if not isinstance(token, str) or not token:
                raise DingTalkAPIError("DingTalk did not return an access token")  # noqa: TRY003
            expire_in = payload.get("expireIn")
            seconds = float(expire_in) if isinstance(expire_in, (int, float)) else 7200.0
            self._token = token
            self._token_expires_at = time.monotonic() + max(seconds - TOKEN_EXPIRY_MARGIN_SECONDS, 0.0)
            return token

    async def reply_webhook(self, webhook: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", webhook, json_body=payload, authenticated=False)

    async def send_text(
        self,
        *,
        conversation_id: str,
        conversation_type: str,
        user_id: str,
        text: str,
    ) -> dict[str, Any]:
        return await self._send_robot_message(
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            user_id=user_id,
            msg_key="sampleText",
            msg_param={"content": text},
        )

    async def send_markdown(
        self,
        *,
        conversation_id: str,
        conversation_type: str,
        user_id: str,
        title: str,
        text: str,
    ) -> dict[str, Any]:
        return await self._send_robot_message(
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            user_id=user_id,
            msg_key="sampleMarkdown",
            msg_param={"title": title, "text": text},
        )

    async def send_image(
        self,
        *,
        conversation_id: str,
        conversation_type: str,
        user_id: str,
        media_id: str,
    ) -> dict[str, Any]:
        return await self._send_robot_message(
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            user_id=user_id,
            msg_key="sampleImageMsg",
            msg_param={"photoURL": media_id},
        )

    async def send_file(  # noqa: PLR0913
        self,
        *,
        conversation_id: str,
        conversation_type: str,
        user_id: str,
        media_id: str,
        filename: str,
        file_type: str = "file",
    ) -> dict[str, Any]:
        return await self._send_robot_message(
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            user_id=user_id,
            msg_key="sampleFile",
            msg_param={"mediaId": media_id, "fileName": filename, "fileType": file_type},
        )

    async def _send_robot_message(
        self,
        *,
        conversation_id: str,
        conversation_type: str,
        user_id: str,
        msg_key: str,
        msg_param: dict[str, Any],
    ) -> dict[str, Any]:
        token = await self.get_access_token()
        headers = {"x-acs-dingtalk-access-token": token}
        if conversation_type == "group":
            body = {
                "robotCode": self._robot_code,
                "openConversationId": conversation_id,
                "msgKey": msg_key,
                "msgParam": json.dumps(msg_param, ensure_ascii=False),
            }
            return await self._request("POST", "/v1.0/robot/groupMessages/send", json_body=body, headers=headers)
        body = {
            "robotCode": self._robot_code,
            "userIds": [user_id],
            "msgKey": msg_key,
            "msgParam": json.dumps(msg_param, ensure_ascii=False),
        }
        return await self._request("POST", "/v1.0/robot/oToMessages/batchSend", json_body=body, headers=headers)

    async def create_and_deliver_card(  # noqa: PLR0913
        self,
        *,
        card_template_id: str,
        out_track_id: str,
        card_data: dict[str, str],
        conversation_id: str,
        conversation_type: str,
        user_id: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "cardTemplateId": card_template_id,
            "outTrackId": out_track_id,
            "cardData": {"cardParamMap": card_data},
            "callbackType": "STREAM",
            "imGroupOpenSpaceModel": {"supportForward": True},
            "imRobotOpenSpaceModel": {"supportForward": True},
        }
        if conversation_type == "group":
            body |= {
                "openSpaceId": f"dtv1.card//IM_GROUP.{conversation_id}",
                "imGroupOpenDeliverModel": {"robotCode": self._robot_code},
            }
        else:
            body |= {
                "openSpaceId": f"dtv1.card//IM_ROBOT.{user_id}",
                "imRobotOpenDeliverModel": {"spaceType": "IM_ROBOT"},
            }
        return await self._request("POST", "/v1.0/card/instances/createAndDeliver", json_body=body)

    async def stream_card_content(
        self,
        *,
        out_track_id: str,
        content: str,
        finalize: bool = False,
        error: bool = False,
    ) -> dict[str, Any]:
        body = {
            "outTrackId": out_track_id,
            "guid": uuid4().hex,
            "key": CARD_CONTENT_KEY,
            "content": content,
            "isFull": True,
            "isFinalize": finalize,
            "isError": error,
        }
        return await self._request("PUT", "/v1.0/card/streaming", json_body=body)

    async def update_card_data(self, *, out_track_id: str, card_data: dict[str, str]) -> dict[str, Any]:
        body = {"outTrackId": out_track_id, "cardData": {"cardParamMap": card_data}}
        return await self._request("PUT", "/v1.0/card/instances", json_body=body)

    async def download_message_file(self, download_code: str) -> bytes:
        token = await self.get_access_token()
        payload = await self._request(
            "POST",
            "/v1.0/robot/messageFiles/download",
            json_body={"robotCode": self._robot_code, "downloadCode": download_code},
            headers={"x-acs-dingtalk-access-token": token},
        )
        url = payload.get("downloadUrl")
        if not isinstance(url, str) or not url:
            raise DingTalkAPIError("DingTalk did not return a download URL")  # noqa: TRY003
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=DINGTALK_TIMEOUT_SECONDS)) as response:
            if response.status >= HTTP_ERROR_STATUS:
                text = await response.text()
                raise DingTalkAPIError(  # noqa: TRY003
                    f"DingTalk download failed ({response.status}): {text}"
                )
            return await response.read()

    async def upload_media(self, data: bytes, *, filename: str, mime_type: str, media_type: str) -> str:
        token = await self.get_access_token()
        form = aiohttp.FormData()
        form.add_field("type", media_type)
        form.add_field("media", data, filename=filename, content_type=mime_type)
        payload = await self._request(
            "POST",
            "/media/upload",
            data=form,
            headers={"x-acs-dingtalk-access-token": token},
        )
        media_id = payload.get("media_id") or payload.get("mediaId")
        if not isinstance(media_id, str) or not media_id:
            raise DingTalkAPIError("DingTalk did not return a media id")  # noqa: TRY003
        return media_id

    async def _request(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: aiohttp.FormData | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        url = path if path.startswith(("http://", "https://")) else f"{self._base_url}{path}"
        request_headers = dict(headers or {})
        if authenticated and "x-acs-dingtalk-access-token" not in request_headers:
            request_headers["x-acs-dingtalk-access-token"] = await self.get_access_token()
        timeout = aiohttp.ClientTimeout(total=DINGTALK_TIMEOUT_SECONDS)
        async with self.session.request(
            method,
            url,
            json=json_body,
            data=data,
            headers=request_headers,
            timeout=timeout,
        ) as response:
            text = await response.text()
            if response.status >= HTTP_ERROR_STATUS:
                raise DingTalkAPIError(  # noqa: TRY003
                    f"DingTalk request failed ({response.status}): {text}"
                )
            if not text:
                return {}
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise DingTalkAPIError(  # noqa: TRY003
                    f"DingTalk returned invalid JSON: {text[:200]}"
                ) from exc
            return payload if isinstance(payload, dict) else {"result": payload}
