from __future__ import annotations

import time
from typing import cast

import aiohttp
import pytest

from acp_chat_bot.channels.dingtalk.api import DingTalkAPI, DingTalkAPIError
from tests.channels.dingtalk.support import FakeResponse, FakeSession

pytestmark = pytest.mark.asyncio


def make_api(*responses: FakeResponse, session: FakeSession | None = None) -> tuple[DingTalkAPI, FakeSession]:
    fake_session = session or FakeSession(*responses)
    api = DingTalkAPI(
        client_id="client-id",
        client_secret="client-secret",
        robot_code="robot-code",
        base_url="https://api.test",
        session=cast("aiohttp.ClientSession", fake_session),
    )
    api._token = "cached-token"
    api._token_expires_at = time.monotonic() + 3600
    return api, fake_session


async def test_access_token_is_cached():
    session = FakeSession(FakeResponse(text='{"accessToken": "tok", "expireIn": 7200}'))
    api = DingTalkAPI(
        client_id="id",
        client_secret="secret",
        robot_code="robot",
        session=cast("aiohttp.ClientSession", session),
    )

    assert await api.get_access_token() == "tok"
    assert await api.get_access_token() == "tok"
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == "https://api.dingtalk.com/v1.0/oauth2/accessToken"


async def test_access_token_errors_when_missing():
    session = FakeSession(FakeResponse(text='{"expireIn": 7200}'))
    api = DingTalkAPI(
        client_id="id",
        client_secret="secret",
        robot_code="robot",
        session=cast("aiohttp.ClientSession", session),
    )

    with pytest.raises(DingTalkAPIError):
        await api.get_access_token()


async def test_access_token_uses_default_expiry_for_bad_expire_in():
    session = FakeSession(FakeResponse(text='{"accessToken": "tok", "expireIn": "later"}'))
    api = DingTalkAPI(
        client_id="id",
        client_secret="secret",
        robot_code="robot",
        session=cast("aiohttp.ClientSession", session),
    )

    assert await api.get_access_token() == "tok"


async def test_http_errors_and_invalid_json_are_reported():
    api, _ = make_api(FakeResponse(status=500, text="boom"))
    with pytest.raises(DingTalkAPIError, match="500"):
        await api.reply_webhook("https://hook", {"msgtype": "text"})

    api, _ = make_api(FakeResponse(text="not json"))
    with pytest.raises(DingTalkAPIError, match="invalid JSON"):
        await api.reply_webhook("https://hook", {"msgtype": "text"})

    api, _ = make_api(FakeResponse(text="[]"))
    assert await api.reply_webhook("https://hook", {"msgtype": "text"}) == {"result": []}

    api, _ = make_api(FakeResponse(text=""))
    assert await api.reply_webhook("https://hook", {"msgtype": "text"}) == {}


async def test_reply_webhook_posts_the_payload():
    api, session = make_api(FakeResponse(text="{}"))

    await api.reply_webhook("https://hook", {"msgtype": "text"})

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == "https://hook"
    assert session.calls[0]["json"] == {"msgtype": "text"}


async def test_send_text_and_markdown_use_the_right_endpoints():
    api, session = make_api(FakeResponse(text="{}"))
    await api.send_text(conversation_id="cid", conversation_type="group", user_id="u1", text="hi")
    group_call = session.calls[-1]
    assert group_call["url"] == "https://api.test/v1.0/robot/groupMessages/send"
    assert group_call["json"]["openConversationId"] == "cid"
    assert group_call["json"]["msgKey"] == "sampleText"

    api, session = make_api(FakeResponse(text="{}"))
    await api.send_markdown(
        conversation_id="cid",
        conversation_type="private",
        user_id="u1",
        title="Title",
        text="body",
    )
    private_call = session.calls[-1]
    assert private_call["url"] == "https://api.test/v1.0/robot/oToMessages/batchSend"
    assert private_call["json"]["userIds"] == ["u1"]
    assert private_call["json"]["msgKey"] == "sampleMarkdown"


async def test_send_image_and_file_payloads():
    api, session = make_api(FakeResponse(text="{}"), FakeResponse(text="{}"))

    await api.send_image(conversation_id="cid", conversation_type="private", user_id="u1", media_id="media")
    await api.send_file(
        conversation_id="cid",
        conversation_type="group",
        user_id="u1",
        media_id="media",
        filename="a.txt",
    )

    assert session.calls[0]["json"]["msgKey"] == "sampleImageMsg"
    assert session.calls[1]["json"]["msgKey"] == "sampleFile"
    assert "a.txt" in session.calls[1]["json"]["msgParam"]


async def test_download_message_file_fetches_the_returned_url():
    api, session = make_api(
        FakeResponse(text='{"downloadUrl": "https://download.test/file"}'),
        FakeResponse(data=b"payload"),
    )

    data = await api.download_message_file("code-1")

    assert data == b"payload"
    assert session.calls[0]["url"] == "https://api.test/v1.0/robot/messageFiles/download"
    assert session.calls[1]["url"] == "https://download.test/file"


async def test_download_message_file_rejects_missing_url_and_failed_get():
    api, _ = make_api(FakeResponse(text="{}"))
    with pytest.raises(DingTalkAPIError, match="download URL"):
        await api.download_message_file("code-1")

    api, _ = make_api(
        FakeResponse(text='{"downloadUrl": "https://download.test/file"}'),
        FakeResponse(status=404, text="missing"),
    )
    with pytest.raises(DingTalkAPIError, match="404"):
        await api.download_message_file("code-1")


async def test_upload_media_returns_the_media_id():
    api, session = make_api(FakeResponse(text='{"media_id": "media-1"}'))

    media_id = await api.upload_media(b"data", filename="a.txt", mime_type="text/plain", media_type="file")

    assert media_id == "media-1"
    assert session.calls[0]["url"] == "https://api.test/media/upload"
    assert session.calls[0]["data"] is not None


async def test_upload_media_reject_a_missing_id():
    api, _ = make_api(FakeResponse(text="{}"))

    with pytest.raises(DingTalkAPIError, match="media id"):
        await api.upload_media(b"data", filename="a.txt", mime_type="text/plain", media_type="file")


async def test_close_closes_only_the_session_it_created(mocker):
    owned = FakeSession()
    mocker.patch("aiohttp.ClientSession", return_value=owned)
    api = DingTalkAPI(client_id="id", client_secret="secret", robot_code="robot")
    assert api.session is owned

    await api.close()
    assert owned.closed is True

    external = FakeSession()
    api = DingTalkAPI(
        client_id="id",
        client_secret="secret",
        robot_code="robot",
        session=cast("aiohttp.ClientSession", external),
    )
    await api.close()
    assert external.closed is False


async def test_authenticated_requests_add_the_token_header():
    api, session = make_api(FakeResponse(text="{}"))

    await api._request("GET", "/v1.0/ping")

    assert session.calls[0]["headers"]["x-acs-dingtalk-access-token"] == "cached-token"


async def test_session_property_creates_a_client_session(mocker):
    fake_session = FakeSession()
    factory = mocker.patch("aiohttp.ClientSession", return_value=fake_session)
    api = DingTalkAPI(client_id="id", client_secret="secret", robot_code="robot")

    assert api.session is fake_session
    factory.assert_called_once()
    await api.close()
    assert fake_session.closed is True
