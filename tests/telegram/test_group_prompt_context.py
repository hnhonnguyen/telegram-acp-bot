from __future__ import annotations

# ruff: noqa: F403, F405, I001

from types import SimpleNamespace

from telegram_acp_bot.telegram.bridge import GROUP_MENTION_REMINDER, PENDING_MEDIA_HEADER, TelegramBridge
from tests.telegram.support import *

PHOTO_FILE_ID = "AGAC_photo_file_id_value"


def make_context_bridge() -> tuple[TelegramBridge, AgentCommandsService]:
    service = AgentCommandsService(None)
    config = make_config(token="TOKEN", allowed_user_ids=[], workspace=".")
    return TelegramBridge(config=config, agent_service=cast(AgentService, service)), service


def prompted(service: AgentCommandsService) -> str:
    return service.prompts[0][0]


def make_edit_update(
    *, text: str, message_id: int = 9, chat_type: str = "group", username: str | None = None, user_id: int = 7
):
    """An update Telegram delivers as `edited_message`, where `update.message` is None."""
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username=username),
        effective_chat=SimpleNamespace(id=100, type=chat_type),
        message=None,
        effective_message=DummyMessage(text, message_id=message_id),
    )


async def test_an_edited_group_message_that_mentions_the_bot_is_answered():
    service = AgentCommandsService(None)
    config = make_config(token="TOKEN", allowed_user_ids=[], workspace=".", group_require_mention=True)
    bridge = TelegramBridge(config=config, agent_service=cast(AgentService, service))

    await bridge.on_message(make_edit_update(text="@echo_bot trả lời giúp"), make_context())

    assert len(service.prompts) == 1
    assert prompted(service).endswith("@echo_bot trả lời giúp")


async def test_an_edited_group_message_without_a_mention_stays_quiet():
    service = AgentCommandsService(None)
    config = make_config(token="TOKEN", allowed_user_ids=[], workspace=".", group_require_mention=True)
    bridge = TelegramBridge(config=config, agent_service=cast(AgentService, service))

    await bridge.on_message(make_edit_update(text="sửa lại cho đúng chính tả"), make_context())

    assert service.prompts == []
    assert [entry["text"] for entry in service.group_messages] == ["sửa lại cho đúng chính tả"]


async def test_an_edit_rewrites_one_history_entry_instead_of_adding_a_second():
    service = EchoAgentService(SessionRegistry())
    config = make_config(token="TOKEN", allowed_user_ids=[], workspace=".")
    bridge = TelegramBridge(config=config, agent_service=service)

    await bridge.on_message(make_edit_update(text="⚙️ đang chạy", message_id=42), make_context())
    await bridge.on_message(make_edit_update(text="✅ xong rồi", message_id=42), make_context())

    assert [entry["text"] for entry in await service.read_group_history(chat_id=100)] == ["✅ xong rồi"]


async def test_group_prompt_arrives_tagged_with_the_sender_username():
    bridge, service = make_context_bridge()

    await bridge.on_message(make_update(username="@alice", text="đau bụng quá", chat_type="group"), make_context())

    assert prompted(service).endswith("@alice: đau bụng quá")


async def test_group_prompt_reminds_the_agent_to_mention_whomever_it_answers():
    bridge, service = make_context_bridge()

    await bridge.on_message(make_update(text="bàn với bob vụ deploy", chat_type="group"), make_context())

    prompt = prompted(service)
    assert prompt.startswith(GROUP_MENTION_REMINDER)
    assert "discuss this with Bob" in prompt


async def test_group_prompt_falls_back_to_the_display_name():
    bridge, service = make_context_bridge()
    update = make_update(text="đau bụng quá", chat_type="group")
    update.effective_user = SimpleNamespace(id=7, username=None, full_name="Alice Nguyễn")

    await bridge.on_message(update, make_context())

    assert prompted(service).endswith("Alice Nguyễn: đau bụng quá")


async def test_group_prompt_without_any_sender_name_is_left_untagged():
    bridge, service = make_context_bridge()
    update = make_update(text="ẩn danh", chat_type="group")
    update.effective_user = SimpleNamespace(id=7, username=None, full_name=None)

    await bridge.on_message(update, make_context())

    assert prompted(service).endswith("\n\nẩn danh")


async def test_private_prompt_gets_no_tag_and_no_reminder():
    bridge, service = make_context_bridge()

    await bridge.on_message(make_update(username="@alice", text="riêng tư", chat_type="private"), make_context())

    assert prompted(service) == "riêng tư"


async def test_media_only_group_prompt_still_names_its_sender():
    bridge, service = make_context_bridge()
    context = make_context()
    context.bot.files[PHOTO_FILE_ID] = b"abc"

    await bridge.on_message(
        make_update(username="@alice", chat_type="group", photo=[SimpleNamespace(file_id=PHOTO_FILE_ID)]),
        context,
    )

    assert prompted(service).endswith("\n\n@alice")


async def test_anonymous_media_only_group_prompt_carries_the_reminder_alone():
    bridge, service = make_context_bridge()
    context = make_context()
    context.bot.files[PHOTO_FILE_ID] = b"abc"
    update = make_update(chat_type="group", photo=[SimpleNamespace(file_id=PHOTO_FILE_ID)])
    update.effective_user = SimpleNamespace(id=7, username=None, full_name=None)

    await bridge.on_message(update, context)

    assert prompted(service) == GROUP_MENTION_REMINDER


class QueuedMediaService(AgentCommandsService):
    """Commands double with one parked photo, so the prompt carries the media notice too."""

    async def take_pending_media(self, *, chat_id: int) -> tuple[dict[str, object], ...]:
        del chat_id
        return ({"file_id": PHOTO_FILE_ID, "message_id": 7, "mime_type": "image/jpeg", "queued_at": "earlier"},)


async def test_group_context_follows_the_pending_media_notice():
    service = QueuedMediaService(None)
    config = make_config(
        token="TOKEN",
        allowed_user_ids=[],
        workspace=".",
        group_media_queue=True,
    )
    bridge = TelegramBridge(config=config, agent_service=cast(AgentService, service))

    await bridge.on_message(make_update(text="xem ảnh giúp", chat_type="group", username="@alice"), make_context())

    prompt = prompted(service)
    assert prompt.index(PENDING_MEDIA_HEADER) < prompt.index(GROUP_MENTION_REMINDER)
    assert prompt.index(GROUP_MENTION_REMINDER) < prompt.index("@alice: xem ảnh giúp")
