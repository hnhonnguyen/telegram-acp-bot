from __future__ import annotations

# ruff: noqa: F403, F405, I001

from types import SimpleNamespace

from telegram_acp_bot.telegram.bridge import PENDING_MEDIA_HEADER, TelegramBridge
from tests.telegram.support import *

PHOTO_FILE_ID = "AGAC_photo_file_id_value"
DOCUMENT_FILE_ID = "BQAC_document_file_id_value"
EXPECTED_ECHO_SUFFIX = "alo"
PHOTO_MESSAGE_ID = 7
DOCUMENT_MESSAGE_ID = 8
QUEUED_REFERENCE_LINES = 2


def assert_not_answered(update) -> None:
    """The only replies on the message are the session notice, never an agent answer."""
    assert update.message is not None
    assert all(EXPECTED_ECHO_SUFFIX not in reply for reply in update.message.replies)


def make_media_bridge(*, queue: bool = True) -> tuple[TelegramBridge, EchoAgentService]:
    service = EchoAgentService(SessionRegistry())
    config = make_config(
        token="TOKEN",
        allowed_user_ids=[],
        workspace=".",
        group_require_mention=True,
        group_media_queue=queue,
    )
    return TelegramBridge(config=config, agent_service=service), service


async def start_session(bridge: TelegramBridge, *, chat_type: str = "group") -> Update:
    update = make_update(text="@echo_bot /new", chat_type=chat_type)
    await bridge.new_session(update, make_context())
    return update


async def test_unmentioned_group_photo_is_queued_instead_of_dropped():
    bridge, service = make_media_bridge()
    await start_session(bridge)
    update = make_update(
        chat_type="group",
        photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id=PHOTO_FILE_ID)],
    )

    await bridge.on_message(update, make_context())

    assert_not_answered(update)
    queued = service._pending_media[100]
    assert queued[0]["file_id"] == PHOTO_FILE_ID
    assert queued[0]["mime_type"] == "image/jpeg"


async def test_unmentioned_group_document_is_queued_with_its_mime():
    bridge, service = make_media_bridge()
    await start_session(bridge)
    update = make_update(
        chat_type="group",
        document=SimpleNamespace(file_id=DOCUMENT_FILE_ID, mime_type="application/pdf"),
    )

    await bridge.on_message(update, make_context())

    assert service._pending_media[100][0]["file_id"] == DOCUMENT_FILE_ID
    assert service._pending_media[100][0]["mime_type"] == "application/pdf"


async def test_queued_media_is_announced_on_the_next_mentioned_prompt():
    bridge, service = make_media_bridge()
    await start_session(bridge)
    await bridge.on_message(
        make_update(chat_type="group", photo=[SimpleNamespace(file_id=PHOTO_FILE_ID)]),
        make_context(),
    )

    update = make_update(text="@echo_bot nhập giùm", chat_type="group")
    await bridge.on_message(update, make_context())

    reply = update.message.replies[-1]
    assert PENDING_MEDIA_HEADER in reply
    assert f"file_id={PHOTO_FILE_ID}" in reply
    assert "queued_at=echo" in reply
    assert service._pending_media.get(100, []) == []


async def test_pending_media_announcement_lists_every_queued_reference():
    bridge, service = make_media_bridge()
    await start_session(bridge)
    for media in (
        make_update(chat_type="group", photo=[SimpleNamespace(file_id=PHOTO_FILE_ID)], message_id=PHOTO_MESSAGE_ID),
        make_update(
            chat_type="group",
            document=SimpleNamespace(file_id=DOCUMENT_FILE_ID, mime_type="application/pdf"),
            message_id=DOCUMENT_MESSAGE_ID,
        ),
    ):
        await bridge.on_message(media, make_context())

    update = make_update(text="@echo_bot làm gì đó", chat_type="group")
    await bridge.on_message(update, make_context())

    reply = update.message.replies[-1]
    assert reply.count("queued_at=echo") == QUEUED_REFERENCE_LINES
    assert f"file_id={PHOTO_FILE_ID} message_id={PHOTO_MESSAGE_ID} mime=image/jpeg" in reply
    assert f"file_id={DOCUMENT_FILE_ID} message_id={DOCUMENT_MESSAGE_ID} mime=application/pdf" in reply
    assert service._pending_media.get(100, []) == []


async def test_announcement_replaces_itself_on_the_next_prompt():
    bridge, service = make_media_bridge()
    await start_session(bridge)
    await bridge.on_message(
        make_update(chat_type="group", photo=[SimpleNamespace(file_id=PHOTO_FILE_ID)]),
        make_context(),
    )
    first = make_update(text="@echo_bot một", chat_type="group")
    await bridge.on_message(first, make_context())

    second = make_update(text="@echo_bot hai", chat_type="group")
    await bridge.on_message(second, make_context())

    assert PENDING_MEDIA_HEADER not in second.message.replies[-1]
    assert service._pending_media.get(100, []) == []


async def test_mentioned_photo_is_delivered_inline_without_queueing():
    bridge, service = make_media_bridge()
    await start_session(bridge)
    context = make_context()
    context.bot.files[PHOTO_FILE_ID] = b"abc"

    update = make_update(
        text="@echo_bot đọc ảnh",
        chat_type="group",
        photo=[SimpleNamespace(file_id=PHOTO_FILE_ID)],
    )
    await bridge.on_message(update, context)

    assert "images=1" in update.message.replies[-1]
    assert service._pending_media == {}


async def test_plain_group_text_never_queues_media():
    bridge, service = make_media_bridge()
    await start_session(bridge)

    await bridge.on_message(make_update(text="chat cua nguoi dung", chat_type="group"), make_context())

    assert service._pending_media == {}


async def test_queue_disabled_drops_unmentioned_media_entirely():
    bridge, service = make_media_bridge(queue=False)
    await start_session(bridge)

    await bridge.on_message(
        make_update(chat_type="group", photo=[SimpleNamespace(file_id=PHOTO_FILE_ID)]),
        make_context(),
    )

    assert service._pending_media == {}


async def test_prompt_without_content_keeps_queued_media():
    bridge, service = make_media_bridge()
    await bridge.new_session(make_update(text="/new", chat_type="private"), make_context())
    await service.record_pending_media(chat_id=100, message_id=5, file_id=PHOTO_FILE_ID, mime_type="image/jpeg")

    await bridge.on_message(make_update(text="", chat_type="private"), make_context())

    assert [ref["file_id"] for ref in service._pending_media[100]] == [PHOTO_FILE_ID]


async def test_group_update_without_a_message_queues_nothing():
    bridge, service = make_media_bridge()
    await start_session(bridge)

    await bridge.on_message(make_update(chat_type="group", with_message=False), make_context())

    assert service._pending_media == {}


async def test_private_photo_is_never_queued():
    bridge, service = make_media_bridge()
    await bridge.new_session(make_update(text="/new", chat_type="private"), make_context())
    context = make_context()
    context.bot.files[PHOTO_FILE_ID] = b"abc"
    update = make_update(chat_type="private", photo=[SimpleNamespace(file_id=PHOTO_FILE_ID)])

    await bridge.on_message(update, context)

    assert service._pending_media == {}
    assert "images=1" in update.message.replies[-1]
