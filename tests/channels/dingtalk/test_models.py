from __future__ import annotations

from datetime import UTC, datetime

from acp_chat_bot.channels.dingtalk.models import (
    conversation_target,
    inbound_message,
    webhook_expiry,
)
from tests.channels.dingtalk.support import make_message


def test_inbound_private_text_message():
    message = make_message()

    inbound = inbound_message(message)

    assert inbound is not None
    assert inbound.chat_id == "cid-1"
    assert inbound.chat_type == "private"
    assert inbound.message_id == "msg-1"
    assert inbound.user_id == "staff-1"
    assert inbound.user_name == "Alice"
    assert inbound.text == "hello"
    assert inbound.mentioned_bot is True


def test_group_message_strips_the_leading_mention():
    message = make_message(conversationType="2", text={"content": "@bot /new /tmp"})

    inbound = inbound_message(message)

    assert inbound is not None
    assert inbound.chat_type == "group"
    assert inbound.text == "/new /tmp"


def test_rich_text_is_joined_and_images_are_collected():
    message = make_message(
        msgtype="richText",
        content={"richText": [{"text": "first"}, {"downloadCode": "img-1"}, {"text": "second"}]},
    )

    inbound = inbound_message(message)

    assert inbound is not None
    assert inbound.text == "first\nsecond"
    assert [attachment.ref for attachment in inbound.attachments] == ["img-1"]
    assert inbound.attachments[0].kind == "image"


def test_picture_message_becomes_an_image_attachment():
    message = make_message(msgtype="picture", content={"downloadCode": "pic-1"})

    inbound = inbound_message(message)

    assert inbound is not None
    assert inbound.attachments[0].ref == "pic-1"
    assert inbound.attachments[0].kind == "image"


def test_file_message_becomes_a_file_attachment():
    message = make_message(
        msgtype="file",
        content={"downloadCode": "file-1", "fileName": "notes.txt", "fileType": "text/plain"},
    )

    inbound = inbound_message(message)

    assert inbound is not None
    assert inbound.attachments[0].ref == "file-1"
    assert inbound.attachments[0].kind == "file"
    assert inbound.attachments[0].name == "notes.txt"
    assert inbound.attachments[0].mime_type == "text/plain"


def test_message_without_a_conversation_is_ignored():
    message = make_message(conversationId=None)

    assert inbound_message(message) is None


def test_conversation_target_and_webhook_expiry():
    message = make_message(conversationType="2", conversationTitle="Team")

    target = conversation_target(message)
    expiry = webhook_expiry(message)

    assert target.chat_id == "cid-1"
    assert target.chat_type == "group"
    assert target.user_id == "staff-1"
    assert target.title == "Team"
    assert expiry == datetime.fromtimestamp(4102444800, tz=UTC)


def test_webhook_expiry_handles_missing_and_invalid_values():
    message = make_message()
    for raw in (0, None, "not-a-timestamp"):
        message.session_webhook_expired_time = raw
        assert webhook_expiry(message) is None
