from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from acp_chat_bot.acp.models import AgentActivityBlock, FilePayload, ImagePayload
from acp_chat_bot.channels.dingtalk.api import DingTalkAPI, DingTalkAPIError
from acp_chat_bot.channels.dingtalk.cards import AI_CARD_TEMPLATE_ID
from acp_chat_bot.channels.dingtalk.channel import DingTalkChannel
from acp_chat_bot.core.chat import ChoiceOption, ChoiceRequest, InboundAttachment
from acp_chat_bot.mcp.state import load_conversation_target
from tests.channels.dingtalk.support import FakeAPI, make_config, make_message

pytestmark = pytest.mark.asyncio

WEBHOOK = "https://example.test/webhook"
CARDS_FOR_TWO_PROMPTS = 2


def make_channel(*, state_file=None, **config_kwargs) -> tuple[DingTalkChannel, FakeAPI]:
    api = FakeAPI()
    channel = DingTalkChannel(
        config=make_config(**config_kwargs),
        api=cast("DingTalkAPI", api),
        state_file=state_file,
    )
    return channel, api


def remember(channel: DingTalkChannel, message=None) -> None:
    channel.remember_conversation(message or make_message())


async def test_remember_conversation_stores_the_target_and_webhook():
    channel, _ = make_channel()

    remember(channel, make_message(conversationType="2", conversationTitle="Team"))

    target = channel.target("cid-1")
    assert target is not None
    assert target.chat_id == "cid-1"
    assert target.chat_type == "group"
    assert target.user_id == "staff-1"
    assert target.title == "Team"
    assert channel.target("missing") is None


async def test_remember_conversation_ignores_messages_without_a_conversation():
    channel, _ = make_channel()

    channel.remember_conversation(make_message(conversationId=None))

    assert channel.target("") is None


async def test_remember_conversation_persists_the_target(tmp_path):
    state_file = tmp_path / "state.json"
    channel, _ = make_channel(state_file=state_file)

    remember(channel, make_message(conversationType="2", conversationTitle="Team"))

    assert load_conversation_target(state_file, "cid-1") == {
        "chat_type": "group",
        "user_id": "staff-1",
        "title": "Team",
    }


async def test_a_fresh_channel_reuses_a_persisted_target(tmp_path):
    state_file = tmp_path / "state.json"
    first, _ = make_channel(state_file=state_file)
    remember(first, make_message(sessionWebhook=""))
    second, api = make_channel(state_file=state_file)

    await second.send_text("cid-1", "hello")

    assert api.texts[0]["conversation_id"] == "cid-1"
    assert api.texts[0]["conversation_type"] == "private"


async def test_send_text_replies_through_the_session_webhook():
    channel, api = make_channel()
    remember(channel)

    assert await channel.send_text("cid-1", "hello") is None

    assert api.replies == [(WEBHOOK, {"msgtype": "text", "text": {"content": "hello"}})]


async def test_send_text_falls_back_to_openapi_without_a_webhook():
    channel, api = make_channel()
    remember(channel, make_message(sessionWebhook=""))

    await channel.send_text("cid-1", "hello")

    assert api.replies == []
    assert api.texts == [
        {"conversation_id": "cid-1", "conversation_type": "private", "user_id": "staff-1", "text": "hello"}
    ]


async def test_send_text_ignores_an_expired_webhook():
    channel, api = make_channel()
    expired = int((datetime.now(UTC) - timedelta(minutes=1)).timestamp() * 1000)
    remember(channel, make_message(sessionWebhookExpiredTime=expired))

    await channel.send_text("cid-1", "hello")

    assert api.replies == []
    assert len(api.texts) == 1


async def test_send_text_chunks_long_messages():
    channel, api = make_channel(max_text_length=10)
    remember(channel)

    await channel.send_text("cid-1", "x" * 25)

    assert [payload["text"]["content"] for _, payload in api.replies] == ["x" * 10, "x" * 10, "x" * 5]


async def test_send_markdown_replies_through_the_session_webhook():
    channel, api = make_channel()
    remember(channel)

    await channel.send_markdown("cid-1", "## Title\nbody")

    assert api.replies == [
        (WEBHOOK, {"msgtype": "markdown", "markdown": {"title": "## Title", "text": "## Title\nbody"}})
    ]


async def test_send_markdown_falls_back_to_openapi():
    channel, api = make_channel()
    remember(channel, make_message(sessionWebhook=""))

    await channel.send_markdown("cid-1", "## Title\nbody")

    assert api.replies == []
    assert api.markdowns == [
        {
            "conversation_id": "cid-1",
            "conversation_type": "private",
            "user_id": "staff-1",
            "title": "## Title",
            "text": "## Title\nbody",
        }
    ]


async def test_markdown_without_a_title_gets_a_default_one():
    channel, api = make_channel()
    remember(channel)

    await channel.send_markdown("cid-1", "")

    assert api.replies[-1][1]["markdown"]["title"] == "ACP bot"


async def test_editing_deleting_typing_and_reactions_are_no_ops():
    channel, _ = make_channel()
    remember(channel)

    assert await channel.edit_markdown("cid-1", "m1", "text") is False
    assert await channel.delete_message("cid-1", "m1") is None
    assert await channel.update_choice("cid-1", "m1", "text") is None
    assert await channel.set_typing("cid-1") is None
    assert await channel.set_reaction("cid-1", "m1", "thinking") is None


async def test_send_activity_renders_the_block_as_markdown():
    channel, api = make_channel()
    remember(channel)
    block = AgentActivityBlock(kind="run", title="ls -la", status="in_progress")

    await channel.send_activity("cid-1", block)

    text = api.replies[-1][1]["markdown"]["text"]
    assert "**⚙️ Running**: ls -la" in text


def reply_block(text: str) -> AgentActivityBlock:
    return AgentActivityBlock(kind="reply", title="", status="in_progress", text=text)


async def test_reply_activity_streams_into_an_ai_card():
    channel, api = make_channel()
    remember(channel)

    await channel.send_activity("cid-1", reply_block("Hello"))

    card = api.cards[0]
    assert card["card_template_id"] == AI_CARD_TEMPLATE_ID
    assert card["conversation_id"] == "cid-1"
    assert card["conversation_type"] == "private"
    assert card["user_id"] == "staff-1"
    assert card["card_data"] == {"flowStatus": "1"}
    assert api.card_updates[0]["card_data"]["flowStatus"] == "2"
    assert api.card_streams[0]["out_track_id"] == card["out_track_id"]
    assert api.card_streams[0]["content"] == "Hello"
    assert api.replies == []


async def test_reply_previews_commit_earlier_segments_when_text_restarts():
    channel, api = make_channel()
    remember(channel)

    await channel.send_activity("cid-1", reply_block("Hello"))
    await channel.send_activity("cid-1", reply_block("Hello world"))
    await channel.send_activity("cid-1", reply_block("Second part"))

    assert [item["content"] for item in api.card_streams] == [
        "Hello",
        "Hello world",
        "Hello world\n\nSecond part",
    ]


async def test_finish_streaming_reply_finalizes_the_card():
    channel, api = make_channel()
    remember(channel)
    await channel.send_activity("cid-1", reply_block("partial"))

    assert await channel.finish_streaming_reply("cid-1", "Final answer") is True

    update = api.card_updates[-1]
    assert update["out_track_id"] == api.cards[0]["out_track_id"]
    assert update["card_data"]["flowStatus"] == "3"
    assert update["card_data"]["msgContent"] == "Final answer"
    assert await channel.finish_streaming_reply("cid-1", "again") is False


async def test_finish_streaming_reply_falls_back_to_the_last_preview():
    channel, api = make_channel()
    remember(channel)
    await channel.send_activity("cid-1", reply_block("partial"))

    assert await channel.finish_streaming_reply("cid-1", "  ") is True

    assert api.card_updates[-1]["card_data"]["msgContent"] == "partial"


async def test_card_finalization_failure_returns_false():
    channel, api = make_channel()
    remember(channel)
    await channel.send_activity("cid-1", reply_block("partial"))
    api.card_error = True

    assert await channel.finish_streaming_reply("cid-1", "final") is False


async def test_empty_reply_previews_do_not_create_a_card():
    channel, api = make_channel()
    remember(channel)

    await channel.send_activity("cid-1", reply_block("   "))

    assert api.cards == []


async def test_card_streaming_failure_keeps_the_card_for_the_final_answer():
    channel, api = make_channel()
    remember(channel)
    await channel.send_activity("cid-1", reply_block("first"))
    api.card_error = True

    await channel.send_activity("cid-1", reply_block("first more"))

    assert api.cards
    assert len(api.card_streams) == 1

    api.card_error = False
    assert await channel.finish_streaming_reply("cid-1", "final") is True


async def test_concurrent_reply_previews_create_only_one_card():
    channel, api = make_channel()
    remember(channel)
    api.create_gate = asyncio.Event()

    first = asyncio.create_task(channel.send_activity("cid-1", reply_block("one")))
    await asyncio.sleep(0)
    second = asyncio.create_task(channel.send_activity("cid-1", reply_block("two")))
    await asyncio.sleep(0)
    api.create_gate.set()
    await asyncio.gather(first, second)

    assert len(api.cards) == 1
    assert [item["content"] for item in api.card_streams] == ["one", "one\n\ntwo"]


async def test_previews_after_finalization_are_ignored():
    channel, api = make_channel()
    remember(channel)
    await channel.send_activity("cid-1", reply_block("partial"))
    assert await channel.finish_streaming_reply("cid-1", "final") is True

    await channel.send_activity("cid-1", reply_block("late"))

    assert len(api.cards) == 1
    assert len(api.card_streams) == 1


async def test_a_new_prompt_starts_a_fresh_card():
    channel, api = make_channel()
    remember(channel)
    await channel.send_activity("cid-1", reply_block("first"))
    await channel.finish_streaming_reply("cid-1", "first answer")
    await channel.set_typing("cid-1")

    await channel.send_activity("cid-1", reply_block("second"))

    assert len(api.cards) == CARDS_FOR_TWO_PROMPTS
    assert api.card_streams[-1]["content"] == "second"


async def test_set_typing_keeps_an_open_card():
    channel, api = make_channel()
    remember(channel)
    await channel.send_activity("cid-1", reply_block("partial"))

    await channel.set_typing("cid-1")
    await channel.send_activity("cid-1", reply_block("partial more"))

    assert len(api.cards) == 1
    assert api.card_streams[-1]["content"] == "partial more"


async def test_finishing_a_reply_without_a_card_returns_false():
    channel, _ = make_channel()

    assert await channel.finish_streaming_reply("cid-1", "final") is False


async def test_card_creation_failure_falls_back_to_plain_messages():
    channel, api = make_channel()
    remember(channel)
    api.card_error = True

    await channel.send_activity("cid-1", reply_block("partial"))

    assert api.cards == []
    assert channel.capabilities.streaming_cards is False
    assert await channel.finish_streaming_reply("cid-1", "final") is False

    await channel.send_markdown("cid-1", "final")
    assert api.replies[-1][1]["markdown"]["text"] == "final"


async def test_reply_activity_uses_markdown_when_cards_are_disabled():
    channel, api = make_channel(streaming_cards=False)
    remember(channel)

    await channel.send_activity("cid-1", reply_block("partial"))

    assert api.cards == []
    assert "**Reply**" in api.replies[-1][1]["markdown"]["text"]


async def test_present_choice_sends_numbered_options_and_returns_an_id():
    channel, api = make_channel()
    remember(channel)
    request = ChoiceRequest(
        request_id="req-1",
        kind="permission",
        title="Permission required",
        body="Allow the tool?",
        options=(ChoiceOption(value="allow", label="Allow"), ChoiceOption(value="deny", label="Deny")),
    )

    message_id = await channel.present_choice("cid-1", request)

    assert isinstance(message_id, str) and message_id
    text = api.replies[-1][1]["markdown"]["text"]
    assert "1. Allow" in text
    assert "2. Deny" in text


async def test_send_image_uploads_and_sends_the_media_id():
    channel, api = make_channel()
    remember(channel)
    payload = ImagePayload(data_base64=base64.b64encode(b"image-bytes").decode(), mime_type="image/png")

    await channel.send_image("cid-1", payload)

    assert api.uploads == [
        {"data": b"image-bytes", "filename": "image", "mime_type": "image/png", "media_type": "image"}
    ]
    assert api.images == [
        {
            "conversation_id": "cid-1",
            "conversation_type": "private",
            "user_id": "staff-1",
            "media_id": "media-1",
        }
    ]


async def test_send_file_uses_base64_then_text_content():
    channel, api = make_channel()
    remember(channel)

    await channel.send_file("cid-1", FilePayload(name="a.bin", data_base64=base64.b64encode(b"raw").decode()))
    await channel.send_file("cid-1", FilePayload(name="b.txt", mime_type="text/plain", text_content="hi"))

    assert api.uploads[0]["data"] == b"raw"
    assert api.uploads[0]["mime_type"] == "application/octet-stream"
    assert api.uploads[1]["data"] == b"hi"
    assert api.uploads[1]["mime_type"] == "text/plain"
    assert [item["filename"] for item in api.files] == ["a.bin", "b.txt"]


async def test_download_fetches_the_attachment_bytes():
    channel, api = make_channel()
    attachment = InboundAttachment(ref="code-1", kind="file", mime_type="text/plain", name="notes.txt")

    downloaded = await channel.download(attachment)

    assert api.downloads == ["code-1"]
    assert downloaded.data == b"download-bytes"
    assert downloaded.mime_type == "text/plain"
    assert downloaded.name == "notes.txt"


async def test_sending_to_an_unknown_conversation_fails():
    channel, _ = make_channel()

    with pytest.raises(DingTalkAPIError, match="Unknown DingTalk conversation"):
        await channel.send_text("missing", "hello")


async def test_sending_to_an_unpersisted_conversation_fails(tmp_path):
    channel, _ = make_channel(state_file=tmp_path / "state.json")

    with pytest.raises(DingTalkAPIError, match="Unknown DingTalk conversation"):
        await channel.send_text("missing", "hello")
