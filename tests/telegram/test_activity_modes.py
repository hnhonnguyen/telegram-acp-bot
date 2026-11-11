from __future__ import annotations

# ruff: noqa: F403, F405, I001

import logging

from tests.telegram.support import *

from telegram_acp_bot.telegram.constants import COMPACT_STATUS_HINT_CHARS, COMPACT_STATUS_TICK_SECONDS

SECOND_REPLY_MESSAGE_ID = 2


async def test_make_config_compact_activity_defaults_to_false():
    config = make_config(token="T", allowed_user_ids=[1], workspace=".")
    assert config.compact_activity is False


async def test_make_config_compact_activity_can_be_set():
    config = make_config(token="T", allowed_user_ids=[1], workspace=".", compact_activity=True)
    assert config.compact_activity is True


async def test_compact_on_activity_event_sends_initial_status_message():
    bridge = make_compact_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    block = AgentActivityBlock(kind="think", title="Checking", status="in_progress", text="")

    await bridge.on_activity_event(TEST_CHAT_ID, block)

    assert len(bot.sent_messages) == 1
    status_text = cast(str, bot.sent_messages[0]["text"])
    assert status_text == "💡 Thinking"
    assert TEST_CHAT_ID in bridge._compact_status_msg_id


async def test_compact_on_activity_event_defers_follow_up_edits_to_the_refresher():
    bridge = make_compact_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    bridge._compact_status_msg_id[TEST_CHAT_ID] = COMPACT_STATUS_MSG_ID
    bridge._compact_status_displayed[TEST_CHAT_ID] = "💡 Thinking"
    block = AgentActivityBlock(kind="execute", title="Run ls", status="in_progress", text="")

    await bridge.on_activity_event(TEST_CHAT_ID, block)

    assert bot.sent_messages == []
    assert bot.edited_messages == []
    assert bridge._compact_status_label[TEST_CHAT_ID] == "⚙️ Running: ls"
    assert TEST_CHAT_ID in bridge._compact_status_tasks


async def test_compact_status_hint_names_the_command_of_a_multiline_block():
    """Fenced multi-line commands collapse onto one status row."""
    block = AgentActivityBlock(
        kind="execute",
        title="Run cd /tmp && python3 scripts/thuchi.py add --tien 75000",
        status="in_progress",
        text="",
    )

    hint = TelegramBridge._compact_status_hint(block, workspace=Path("."))

    assert "\n" not in hint
    assert "cd /tmp" in hint


async def test_compact_status_hint_truncates_a_long_title():
    block = AgentActivityBlock(kind="read", title="Read " + "x" * 200, status="in_progress", text="")

    hint = TelegramBridge._compact_status_hint(block, workspace=Path("."))

    assert len(hint) <= COMPACT_STATUS_HINT_CHARS
    assert hint.endswith("…")


async def test_compact_status_hint_is_empty_when_the_block_has_no_title_row():
    think = AgentActivityBlock(kind="think", title="Long internal reasoning", status="in_progress", text="")
    blank = AgentActivityBlock(kind="execute", title="", status="in_progress", text="")

    assert TelegramBridge._compact_status_hint(think, workspace=None) == ""
    assert TelegramBridge._compact_status_hint(blank, workspace=None) == ""


async def test_compact_status_hint_keeps_paths_outside_the_workspace_absolute():
    outside = AgentActivityBlock(kind="edit", title="Edit /etc/hosts", status="in_progress", text="")
    without_workspace = AgentActivityBlock(kind="read", title="Read /var/log/sys", status="in_progress", text="")

    assert TelegramBridge._compact_status_hint(outside, workspace=Path(".")) == "/etc/hosts"
    assert TelegramBridge._compact_status_hint(without_workspace, workspace=None) == "/var/log/sys"


async def _drive_compact_refresher(bridge: TelegramBridge, mocker, *, ticks: int) -> None:
    """Run the compact refresher for a fixed number of paint passes, then stop its loop."""
    steps = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal steps
        assert seconds == COMPACT_STATUS_TICK_SECONDS
        steps += 1
        if steps > ticks:
            bridge._compact_status_label.pop(TEST_CHAT_ID, None)

    mocker.patch("telegram_acp_bot.telegram.bridge.asyncio.sleep", side_effect=fake_sleep)
    await bridge._refresh_compact_status(chat_id=TEST_CHAT_ID)


async def test_compact_status_message_shows_the_hint_and_repaints_it(mocker):
    """The refresher re-renders the whole line, so the action name survives later updates."""
    bridge = make_compact_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    block = AgentActivityBlock(kind="edit", title="Edit src/app.py", status="in_progress", text="")

    await bridge.on_activity_event(TEST_CHAT_ID, block)

    assert bridge._compact_status_label[TEST_CHAT_ID] == "✏️ Editing: src/app.py"
    assert cast(str, bot.sent_messages[0]["text"]) == "✏️ Editing: src/app.py"

    bridge._compact_status_label[TEST_CHAT_ID] = "✏️ Editing: src/config.py"
    await _drive_compact_refresher(bridge, mocker, ticks=1)

    edited = [cast(str, m["text"]) for m in bot.edited_messages]
    assert edited == ["✏️ Editing: src/config.py"]
    assert bridge._compact_status_displayed[TEST_CHAT_ID] == "✏️ Editing: src/config.py"


async def test_compact_on_activity_event_send_failure_leaves_no_status_message(caplog):
    bridge = make_compact_bridge()

    class SendFailBot(DummyBot):
        async def send_message(self, **kwargs: object) -> SimpleNamespace:
            raise MarkdownFailureError

    bot = SendFailBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    block = AgentActivityBlock(kind="think", title="x", status="in_progress", text="")

    with caplog.at_level(logging.WARNING):
        await bridge.on_activity_event(TEST_CHAT_ID, block)

    assert TEST_CHAT_ID not in bridge._compact_status_msg_id
    assert TEST_CHAT_ID not in bridge._compact_status_tasks
    assert "Compact status send failed" in caplog.text


async def test_compact_refresher_edit_failure_keeps_the_status_message(mocker, caplog):
    bridge = make_compact_bridge()
    bot = FailingEditBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    bridge._compact_status_msg_id[TEST_CHAT_ID] = COMPACT_STATUS_MSG_ID
    bridge._compact_status_label[TEST_CHAT_ID] = "💡 Thinking"

    with caplog.at_level(logging.WARNING):
        await _drive_compact_refresher(bridge, mocker, ticks=1)

    assert bridge._compact_status_msg_id[TEST_CHAT_ID] == COMPACT_STATUS_MSG_ID
    assert "Compact status edit failed" in caplog.text


async def test_compact_refresher_drops_the_status_message_once_it_is_gone(mocker, caplog):
    class GoneEditBot(DummyBot):
        async def edit_message_text(self, **kwargs: object) -> None:
            raise StatusGoneError

    bridge = make_compact_bridge()
    bridge._app = cast(Application, SimpleNamespace(bot=GoneEditBot()))
    bridge._compact_status_msg_id[TEST_CHAT_ID] = COMPACT_STATUS_MSG_ID
    bridge._compact_status_displayed[TEST_CHAT_ID] = "💡 Thinking"
    bridge._compact_status_label[TEST_CHAT_ID] = "⚙️ Running: ls"

    with caplog.at_level(logging.WARNING):
        await _drive_compact_refresher(bridge, mocker, ticks=1)

    assert TEST_CHAT_ID not in bridge._compact_status_msg_id
    assert TEST_CHAT_ID not in bridge._compact_status_displayed
    assert "disappeared" in caplog.text


async def test_compact_dispatch_reply_edits_status_message():
    bridge = make_compact_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    bridge._compact_status_msg_id[TEST_CHAT_ID] = COMPACT_STATUS_MSG_ID
    update = make_update(text="hello")
    reply = AgentReply(text="Final answer.")

    await bridge._dispatch_reply(chat_id=TEST_CHAT_ID, update=update, reply=reply)

    assert len(bot.edited_messages) == 1
    assert "Final answer." in cast(str, bot.edited_messages[0]["text"])
    assert update.message is not None
    assert update.message.replies == []
    assert TEST_CHAT_ID not in bridge._compact_status_msg_id


async def test_compact_dispatch_reply_fallback_on_edit_failure():
    bridge = make_compact_bridge()
    bot = FailingEditBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    bridge._compact_status_msg_id[TEST_CHAT_ID] = COMPACT_STATUS_MSG_ID
    update = make_update(text="hello")
    reply = AgentReply(text="Final answer.")

    await bridge._dispatch_reply(chat_id=TEST_CHAT_ID, update=update, reply=reply)

    assert (TEST_CHAT_ID, COMPACT_STATUS_MSG_ID) in bot.deleted_message_ids
    assert update.message is not None
    assert "Final answer." in update.message.replies[-1]
    assert TEST_CHAT_ID not in bridge._compact_status_msg_id


async def test_compact_dispatch_reply_no_status_message_sends_normally():
    bridge = make_compact_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    update = make_update(text="hello")
    reply = AgentReply(text="Final answer.")

    await bridge._dispatch_reply(chat_id=TEST_CHAT_ID, update=update, reply=reply)

    assert bot.edited_messages == []
    assert update.message is not None
    assert "Final answer." in update.message.replies[-1]


async def test_compact_dispatch_reply_empty_text_deletes_status():
    bridge = make_compact_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    bridge._compact_status_msg_id[TEST_CHAT_ID] = COMPACT_STATUS_MSG_ID
    update = make_update(text="hello")
    reply = AgentReply(text="")

    await bridge._dispatch_reply(chat_id=TEST_CHAT_ID, update=update, reply=reply)

    assert (TEST_CHAT_ID, COMPACT_STATUS_MSG_ID) in bot.deleted_message_ids
    assert update.message is not None
    assert update.message.replies == []


async def test_compact_dispatch_reply_empty_text_no_status_is_noop():
    bridge = make_compact_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    update = make_update(text="hello")
    reply = AgentReply(text="")

    await bridge._dispatch_reply(chat_id=TEST_CHAT_ID, update=update, reply=reply)

    assert bot.deleted_message_ids == []
    assert update.message is not None
    assert update.message.replies == []


async def test_finalize_compact_reply_without_app_sends_normally():
    bridge = make_compact_bridge()
    bridge._compact_status_msg_id[TEST_CHAT_ID] = COMPACT_STATUS_MSG_ID
    update = make_update(text="hello")

    await bridge._finalize_compact_reply(chat_id=TEST_CHAT_ID, update=update, text="Final answer.")

    assert update.message is not None
    assert "Final answer." in update.message.replies[-1]
    assert TEST_CHAT_ID not in bridge._compact_status_msg_id


async def test_clear_compact_status_without_app_is_noop():
    bridge = make_compact_bridge()
    bridge._compact_status_msg_id[TEST_CHAT_ID] = COMPACT_STATUS_MSG_ID

    await bridge._clear_compact_status(TEST_CHAT_ID)

    assert TEST_CHAT_ID not in bridge._compact_status_msg_id


async def test_clear_compact_status_no_status_is_noop():
    bridge = make_compact_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))

    await bridge._clear_compact_status(TEST_CHAT_ID)

    assert bot.deleted_message_ids == []


async def test_clear_compact_status_delete_failure_is_silent():
    bridge = make_compact_bridge()

    class DeleteFailBot(DummyBot):
        async def delete_message(self, **kwargs: object) -> None:
            raise MarkdownFailureError

    bot = DeleteFailBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    bridge._compact_status_msg_id[TEST_CHAT_ID] = COMPACT_STATUS_MSG_ID

    await bridge._clear_compact_status(TEST_CHAT_ID)

    assert TEST_CHAT_ID not in bridge._compact_status_msg_id


async def test_compact_live_activity_full_flow():
    """Integration: compact mode sends one status, then edits it with the final answer."""
    service = LiveActivityService()
    config = make_config(token="TOKEN", allowed_user_ids=[], workspace=".", compact_activity=True)
    bridge = TelegramBridge(config=config, agent_service=cast(AgentService, service))
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    update = make_update(chat_id=TEST_CHAT_ID, text="hello")
    context = make_context()
    context.bot = bot

    await bridge.on_message(update, context)

    assert update.message is not None
    assert update.message.replies == []
    assert len(bot.sent_messages) == 1
    # Status text keeps the normal emoji language in compact mode.
    status_text = cast(str, bot.sent_messages[0]["text"])
    assert status_text == "💡 Thinking"
    assert len(bot.edited_messages) == 1
    assert "Final response." in cast(str, bot.edited_messages[0]["text"])


async def test_edit_markdown_in_chat_returns_true_on_success():

    bot = DummyBot()
    result = await TelegramBridge._edit_markdown_in_chat(
        bot=cast(Bot, bot), chat_id=TEST_CHAT_ID, message_id=COMPACT_STATUS_MSG_ID, text="Hello world"
    )
    assert result is True
    assert bot.edited_messages


async def test_edit_markdown_in_chat_returns_true_for_text_with_entities():

    bot = DummyBot()
    result = await TelegramBridge._edit_markdown_in_chat(
        bot=cast(Bot, bot), chat_id=TEST_CHAT_ID, message_id=COMPACT_STATUS_MSG_ID, text="*bold text*"
    )
    assert result is True
    assert bot.edited_messages


async def test_edit_markdown_in_chat_returns_false_on_multi_chunk():

    bot = DummyBot()
    long_text = "word " * 1500
    result = await TelegramBridge._edit_markdown_in_chat(
        bot=cast(Bot, bot), chat_id=TEST_CHAT_ID, message_id=COMPACT_STATUS_MSG_ID, text=long_text
    )
    assert result is False
    assert not bot.edited_messages


async def test_edit_markdown_in_chat_entity_edit_fails_falls_back_to_plain():

    bot = EntityFailingEditBot()
    result = await TelegramBridge._edit_markdown_in_chat(
        bot=cast(Bot, bot), chat_id=TEST_CHAT_ID, message_id=COMPACT_STATUS_MSG_ID, text="*bold text*"
    )
    assert result is True
    assert bot.edited_messages
    assert "entities" not in bot.edited_messages[-1]


async def test_edit_markdown_in_chat_treats_not_modified_as_success():

    bot = NotModifiedAwareBot()
    sent = await bot.send_message(chat_id=TEST_CHAT_ID, text="Hello")
    result = await TelegramBridge._edit_markdown_in_chat(
        bot=cast(Bot, bot), chat_id=TEST_CHAT_ID, message_id=sent.message_id, text="Hello"
    )
    assert result is True
    assert bot.edited_messages == []


async def test_edit_markdown_in_chat_returns_false_when_all_entity_edits_fail():

    bot = FailingEditBot()
    result = await TelegramBridge._edit_markdown_in_chat(
        bot=cast(Bot, bot), chat_id=TEST_CHAT_ID, message_id=COMPACT_STATUS_MSG_ID, text="*bold text*"
    )
    assert result is False


async def test_edit_markdown_in_chat_returns_false_when_plain_edit_fails():

    bot = FailingEditBot()
    result = await TelegramBridge._edit_markdown_in_chat(
        bot=cast(Bot, bot), chat_id=TEST_CHAT_ID, message_id=COMPACT_STATUS_MSG_ID, text="Simple text"
    )
    assert result is False


async def test_edit_markdown_in_chat_convert_error_falls_back_to_plain(mocker):

    mocker.patch("telegram_acp_bot.telegram.bridge.convert", side_effect=RuntimeError("bad"))
    bot = DummyBot()
    result = await TelegramBridge._edit_markdown_in_chat(
        bot=cast(Bot, bot), chat_id=TEST_CHAT_ID, message_id=COMPACT_STATUS_MSG_ID, text="Hello"
    )
    assert result is True
    assert bot.edited_messages


async def test_edit_markdown_in_chat_convert_error_returns_false_when_edit_fails(mocker):

    mocker.patch("telegram_acp_bot.telegram.bridge.convert", side_effect=RuntimeError("bad"))
    bot = FailingEditBot()
    result = await TelegramBridge._edit_markdown_in_chat(
        bot=cast(Bot, bot), chat_id=TEST_CHAT_ID, message_id=COMPACT_STATUS_MSG_ID, text="Hello"
    )
    assert result is False


async def test_verbose_final_reply_replaces_streamed_reply_preview_when_markdown_finishes():

    class StreamingMarkdownReplyService(LiveActivityService):
        async def prompt(self, *, chat_id: int, text: str, images=(), files=()):
            del text, images, files
            assert self._activity_handler is not None
            await self._activity_handler(
                chat_id,
                AgentActivityBlock(kind="reply", title="", status="in_progress", text="**bold", activity_id="reply"),
            )
            return AgentReply(text="**bold**")

    bridge = TelegramBridge(
        config=make_config(token="TOKEN", allowed_user_ids=[], workspace=".", activity_mode="verbose"),
        agent_service=cast(AgentService, StreamingMarkdownReplyService()),
    )
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    update = make_update(chat_id=TEST_CHAT_ID, text="hello")
    context = make_context()
    context.bot = bot

    await bridge.on_message(update, context)

    assert len(bot.sent_messages) == 1
    assert len(bot.edited_messages) == 1
    assert cast(int, bot.edited_messages[0]["message_id"]) == 1
    assert cast(str, bot.edited_messages[0]["text"]) == "bold"
    assert bot.edited_messages[0]["entities"]
    assert update.message is not None
    assert update.message.replies == []


async def test_verbose_final_reply_keeps_markdown_when_final_matches_streamed_preview():

    class StableStreamingMarkdownReplyService(LiveActivityService):
        async def prompt(self, *, chat_id: int, text: str, images=(), files=()):
            del text, images, files
            reply = (
                'Sí, pero te conviene separar `"extraer emails plausibles"` de '
                '`"validar RFC completo"`.\n\n'
                "```python\n"
                "print('ok')\n"
                "```"
            )
            assert self._activity_handler is not None
            await self._activity_handler(
                chat_id,
                AgentActivityBlock(kind="reply", title="", status="in_progress", text=reply, activity_id="reply"),
            )
            return AgentReply(text=reply)

    bridge = TelegramBridge(
        config=make_config(token="TOKEN", allowed_user_ids=[], workspace=".", activity_mode="verbose"),
        agent_service=cast(AgentService, StableStreamingMarkdownReplyService()),
    )
    bot = NotModifiedAwareBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    update = make_update(chat_id=TEST_CHAT_ID, text="hello")
    context = make_context()
    context.bot = bot

    await bridge.on_message(update, context)

    assert len(bot.sent_messages) == 1
    assert bot.edited_messages == []
    payload = bot.sent_messages[0]
    assert "entities" in payload
    assert payload["entities"]
    assert update.message is not None
    assert update.message.replies == []


async def test_verbose_finalize_reply_handles_empty_text_and_empty_chunks(mocker):
    bridge = make_verbose_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    handler = cast(bot_module._VerboseActivityModeHandler, bridge._activity_handler(chat_id=TEST_CHAT_ID))
    handler._store_message(
        chat_id=TEST_CHAT_ID,
        slot_key="activity:reply",
        message=bot_module._VerboseActivityMessage(
            activity_id="reply",
            kind="reply",
            title="",
            message_id=1,
            source_text="preview",
        ),
    )

    assert await handler.finalize_reply(chat_id=TEST_CHAT_ID, update=cast(Update, make_update()), text="") == 1

    handler._store_message(
        chat_id=TEST_CHAT_ID,
        slot_key="activity:reply",
        message=bot_module._VerboseActivityMessage(
            activity_id="reply",
            kind="reply",
            title="",
            message_id=2,
            source_text="preview",
        ),
    )
    mocker.patch.object(TelegramBridge, "_render_markdown_chunks", return_value=[])
    assert (
        await handler.finalize_reply(
            chat_id=TEST_CHAT_ID, update=cast(Update, make_update()), text="still empty chunks"
        )
        == SECOND_REPLY_MESSAGE_ID
    )


async def test_verbose_final_reply_deletes_preview_when_final_edit_fails():

    class StreamingReplyService(LiveActivityService):
        async def prompt(self, *, chat_id: int, text: str, images=(), files=()):
            del text, images, files
            assert self._activity_handler is not None
            await self._activity_handler(
                chat_id,
                AgentActivityBlock(kind="reply", title="", status="in_progress", text="preview", activity_id="reply"),
            )
            return AgentReply(text="Final response.")

    bridge = TelegramBridge(
        config=make_config(token="TOKEN", allowed_user_ids=[], workspace=".", activity_mode="verbose"),
        agent_service=cast(AgentService, StreamingReplyService()),
    )
    bot = FailingEditBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    update = make_update(chat_id=TEST_CHAT_ID, text="hello")
    context = make_context()
    context.bot = bot

    await bridge.on_message(update, context)

    assert bot.deleted_message_ids == [(TEST_CHAT_ID, 1)]
    assert update.message is not None
    assert update.message.replies == ["Final response."]


async def test_verbose_in_progress_updates_are_coalesced_before_editing(mocker):
    mocker.patch.object(activity_module, "VERBOSE_STREAM_TICK_SECONDS", 0.01)
    bridge = make_verbose_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))

    await bridge.on_activity_event(
        TEST_CHAT_ID,
        AgentActivityBlock(kind="reply", title="", status="in_progress", text="one", activity_id="reply"),
    )
    await bridge.on_activity_event(
        TEST_CHAT_ID,
        AgentActivityBlock(kind="reply", title="", status="in_progress", text="one two", activity_id="reply"),
    )
    await bridge.on_activity_event(
        TEST_CHAT_ID,
        AgentActivityBlock(kind="reply", title="", status="in_progress", text="one two three", activity_id="reply"),
    )
    await asyncio.sleep(0.03)

    assert len(bot.sent_messages) == 1
    assert len(bot.edited_messages) == 1
    assert cast(str, bot.edited_messages[-1]["text"]) == "one two three"


async def test_verbose_reset_clears_older_pending_preview_before_flush(mocker):
    mocker.patch.object(activity_module, "VERBOSE_STREAM_TICK_SECONDS", 0.01)
    bridge = make_verbose_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))

    await bridge.on_activity_event(
        TEST_CHAT_ID,
        AgentActivityBlock(kind="reply", title="", status="in_progress", text="one", activity_id="reply"),
    )
    await bridge.on_activity_event(
        TEST_CHAT_ID,
        AgentActivityBlock(kind="reply", title="", status="in_progress", text="one two", activity_id="reply"),
    )
    await bridge.on_activity_event(
        TEST_CHAT_ID,
        AgentActivityBlock(kind="reply", title="", status="in_progress", text="restart", activity_id="reply"),
    )
    await asyncio.sleep(0.03)

    assert len(bot.sent_messages) == EXPECTED_REPEATED_ACTIVITY_MESSAGES
    assert cast(str, bot.sent_messages[-1]["text"]) == "restart"
    assert all(cast(str, item.get("text", "")) != "one two" for item in bot.edited_messages)


async def test_verbose_in_progress_long_preview_stays_single_message(mocker):
    mocker.patch.object(activity_module, "VERBOSE_STREAM_TICK_SECONDS", 0.01)
    bridge = make_verbose_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    long_text = "a" * 5000
    longer_text = "a" * 5200

    await bridge.on_activity_event(
        TEST_CHAT_ID,
        AgentActivityBlock(kind="reply", title="", status="in_progress", text=long_text, activity_id="reply"),
    )
    await bridge.on_activity_event(
        TEST_CHAT_ID,
        AgentActivityBlock(kind="reply", title="", status="in_progress", text=longer_text, activity_id="reply"),
    )
    await asyncio.sleep(0.03)

    assert len(bot.sent_messages) == 1
    assert len(bot.edited_messages) == 1


async def test_verbose_in_progress_preview_helpers_handle_missing_preview(mocker):
    bridge = make_verbose_bridge()
    handler = cast(bot_module._VerboseActivityModeHandler, bridge._activity_handler(chat_id=TEST_CHAT_ID))
    bot = DummyBot()

    mocker.patch.object(TelegramBridge, "_render_markdown_preview_chunk", return_value=None)

    assert (
        await handler._edit_in_progress_preview(
            bot=cast(Bot, bot),
            chat_id=TEST_CHAT_ID,
            message_id=123,
            text="preview",
        )
        is False
    )
    assert await handler._send_in_progress_preview(bot=cast(Bot, bot), chat_id=TEST_CHAT_ID, text="preview") is None


async def test_render_markdown_preview_chunk_handles_empty_and_fallback(mocker):
    short_text = "plain preview"
    mocker.patch.object(TelegramBridge, "_render_markdown_chunks", side_effect=RuntimeError("boom"))
    short_preview = TelegramBridge._render_markdown_preview_chunk(short_text)

    assert short_preview == (short_text, None)

    long_text = "a" * (bot_module.TELEGRAM_MAX_UTF16_MESSAGE_LENGTH + 50)
    long_preview = TelegramBridge._render_markdown_preview_chunk(long_text)

    assert long_preview is not None
    assert long_preview[1] is None
    assert long_preview[0].endswith("...")
    assert bot_module.utf16_len(long_preview[0]) <= bot_module.TELEGRAM_MAX_UTF16_MESSAGE_LENGTH

    mocker.patch.object(TelegramBridge, "_render_markdown_chunks", return_value=[])
    assert TelegramBridge._render_markdown_preview_chunk("ignored") is None


async def test_verbose_completed_event_without_active_message_sends_and_clears():
    bridge = make_verbose_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))

    await bridge.on_activity_event(
        TEST_CHAT_ID,
        AgentActivityBlock(kind="execute", title="Run", status="completed", text="output", activity_id="exec-1"),
    )

    assert len(bot.sent_messages) == 1


async def test_verbose_internal_helpers_cover_remaining_branches(mocker):
    bridge = make_verbose_bridge()
    handler = cast(bot_module._VerboseActivityModeHandler, bridge._activity_handler(chat_id=TEST_CHAT_ID))
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))

    assert await handler.finalize_reply(chat_id=TEST_CHAT_ID, update=cast(Update, make_update()), text="hello") is None

    handler._clear_message(chat_id=TEST_CHAT_ID, slot_key="missing")

    bridge._app = None
    await handler._apply_block_locked(
        chat_id=TEST_CHAT_ID,
        slot_key="activity:reply",
        block=AgentActivityBlock(kind="reply", title="", status="in_progress", text="preview", activity_id="reply"),
    )

    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    handler._store_message(
        chat_id=TEST_CHAT_ID,
        slot_key="activity:reply",
        message=bot_module._VerboseActivityMessage(
            activity_id="reply",
            kind="reply",
            title="",
            message_id=7,
            source_text="preview",
        ),
    )
    mocker.patch.object(TelegramBridge, "_edit_markdown_in_chat", return_value=True)
    await handler._apply_block_locked(
        chat_id=TEST_CHAT_ID,
        slot_key="activity:reply",
        block=AgentActivityBlock(kind="reply", title="", status="completed", text="done", activity_id="reply"),
    )
    assert "activity:reply" not in handler._messages_by_chat.get(TEST_CHAT_ID, {})

    handler._store_message(
        chat_id=TEST_CHAT_ID,
        slot_key="activity:reply",
        message=bot_module._VerboseActivityMessage(
            activity_id="reply",
            kind="reply",
            title="",
            message_id=8,
            source_text="preview",
        ),
    )
    mocker.patch.object(handler, "_edit_in_progress_preview", return_value=False)
    mocked_send = mocker.patch.object(handler, "_send_in_progress_preview", return_value=None)
    await handler._apply_block_locked(
        chat_id=TEST_CHAT_ID,
        slot_key="activity:reply",
        block=AgentActivityBlock(kind="reply", title="", status="in_progress", text="retry", activity_id="reply"),
    )
    assert mocked_send.called
    assert "activity:reply" not in handler._messages_by_chat.get(TEST_CHAT_ID, {})

    mocked_message = SimpleNamespace(message_id=9)
    mocker.patch.object(TelegramBridge, "_send_markdown_to_chat", return_value=mocked_message)
    await handler._apply_block_locked(
        chat_id=TEST_CHAT_ID,
        slot_key="activity:exec-2",
        block=AgentActivityBlock(kind="execute", title="Run", status="completed", text="done", activity_id="exec-2"),
    )
    assert "activity:exec-2" not in handler._messages_by_chat.get(TEST_CHAT_ID, {})


async def test_compact_stale_status_cleared_when_reply_is_none():
    """Compact status message is deleted when the prompt cycle exits without a final reply."""

    class LimitErrorCompactService(LiveActivityService):
        async def prompt(self, *, chat_id: int, text: str, images=(), files=()):
            if self._activity_handler is not None:
                await self._activity_handler(
                    chat_id,
                    AgentActivityBlock(kind="think", title="Thinking…", status="in_progress", text=""),
                )
            raise AgentOutputLimitExceededError(ACP_STDIO_LIMIT_ERROR)

    config = make_config(token="TOKEN", allowed_user_ids=[], workspace=".", compact_activity=True)
    bridge = TelegramBridge(config=config, agent_service=cast(AgentService, LimitErrorCompactService()))
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    update = make_update(chat_id=TEST_CHAT_ID, text="hello")
    context = make_context()
    context.bot = bot

    await bridge.on_message(update, context)

    assert TEST_CHAT_ID not in bridge._compact_status_msg_id
    assert len(bot.deleted_message_ids) == 1
    assert update.message is not None
    assert "Agent output exceeded ACP stdio limit." in update.message.replies[-1]


async def test_compact_refresher_returns_when_app_is_missing():
    bridge = make_compact_bridge()
    await bridge._refresh_compact_status(chat_id=TEST_CHAT_ID)


async def test_compact_refresher_applies_at_most_one_edit_per_wait_period(mocker):
    """A burst of activity events collapses into one edit per tick, which keeps Telegram happy."""
    bridge = make_compact_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    block = AgentActivityBlock(kind="think", title="x", status="in_progress", text="")

    await bridge.on_activity_event(TEST_CHAT_ID, block)

    labels = ["⚙️ Running: ls", "📖 Reading: a.py", "✏️ Editing: b.py"]
    steps = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal steps
        assert seconds == COMPACT_STATUS_TICK_SECONDS
        steps += 1
        if steps > len(labels):
            bridge._compact_status_label.pop(TEST_CHAT_ID, None)
            return
        bridge._compact_status_label[TEST_CHAT_ID] = labels[steps - 1]

    mocker.patch("telegram_acp_bot.telegram.bridge.asyncio.sleep", side_effect=fake_sleep)
    await bridge._refresh_compact_status(chat_id=TEST_CHAT_ID)

    assert [cast(str, m["text"]) for m in bot.edited_messages] == labels


async def test_compact_refresher_skips_an_edit_when_the_label_is_unchanged(mocker):
    bridge = make_compact_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    bridge._compact_status_msg_id[TEST_CHAT_ID] = COMPACT_STATUS_MSG_ID
    bridge._compact_status_displayed[TEST_CHAT_ID] = "💡 Thinking"
    bridge._compact_status_label[TEST_CHAT_ID] = "💡 Thinking"

    await _drive_compact_refresher(bridge, mocker, ticks=2)

    assert bot.edited_messages == []


async def test_compact_refresher_stops_when_the_status_message_is_cleared(mocker):
    bridge = make_compact_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    bridge._compact_status_label[TEST_CHAT_ID] = "💡 Thinking"

    async def clear_message(seconds: float) -> None:
        assert seconds == COMPACT_STATUS_TICK_SECONDS
        bridge._compact_status_msg_id.pop(TEST_CHAT_ID, None)

    mocker.patch("telegram_acp_bot.telegram.bridge.asyncio.sleep", side_effect=clear_message)
    await bridge._refresh_compact_status(chat_id=TEST_CHAT_ID)

    assert bot.edited_messages == []


async def test_compact_refresher_task_removes_itself_when_the_prompt_ends(mocker):
    bridge = make_compact_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    bridge._compact_status_label[TEST_CHAT_ID] = "💡 Thinking"
    bridge._compact_status_msg_id[TEST_CHAT_ID] = COMPACT_STATUS_MSG_ID

    async def end_prompt(seconds: float) -> None:
        assert seconds == COMPACT_STATUS_TICK_SECONDS
        bridge._compact_status_label.pop(TEST_CHAT_ID, None)

    mocker.patch("telegram_acp_bot.telegram.bridge.asyncio.sleep", side_effect=end_prompt)
    bridge._ensure_compact_status_refresher(chat_id=TEST_CHAT_ID)
    await bridge._compact_status_tasks[TEST_CHAT_ID]

    assert TEST_CHAT_ID not in bridge._compact_status_tasks


async def test_compact_concurrent_activity_sends_only_one_message():
    """Race-condition guard: concurrent activity events produce exactly one send."""
    bridge = make_compact_bridge()
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    block = AgentActivityBlock(kind="think", title="x", status="in_progress", text="")

    # Schedule three events concurrently; without the per-chat lock this would
    # create three separate status messages because all three coroutines would
    # read existing_msg_id=None before the first send_message completes.
    await asyncio.gather(
        bridge.on_activity_event(TEST_CHAT_ID, block),
        bridge.on_activity_event(TEST_CHAT_ID, block),
        bridge.on_activity_event(TEST_CHAT_ID, block),
    )

    assert len(bot.sent_messages) == 1, "Only one status message should be created"
    assert TEST_CHAT_ID in bridge._compact_status_msg_id
    assert TEST_CHAT_ID in bridge._compact_status_tasks


async def test_compact_live_activity_full_flow_multi_activity():
    """Integration: compact mode keeps one status message across multiple activity updates."""

    class MultiActivityService(LiveActivityService):
        async def prompt(self, *, chat_id: int, text: str, images=(), files=()):
            del text, images, files
            if self._activity_handler is not None:
                for kind in ("think", "execute"):
                    await self._activity_handler(
                        chat_id,
                        AgentActivityBlock(kind=kind, title="Working", status="in_progress", text=""),
                    )
            return AgentReply(text="Done!")

    config = make_config(token="TOKEN", allowed_user_ids=[], workspace=".", compact_activity=True)
    service = MultiActivityService()
    bridge = TelegramBridge(config=config, agent_service=cast(AgentService, service))
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))
    update = make_update(chat_id=TEST_CHAT_ID, text="hello")
    context = make_context()
    context.bot = bot

    await bridge.on_message(update, context)

    # Exactly one status message sent, zero direct replies.
    assert len(bot.sent_messages) == 1
    assert update.message is not None
    assert update.message.replies == []


async def test_normal_activity_state_is_cleared_after_successful_prompt_cycle():

    class ReusedActivityIdService(LiveActivityService):
        async def prompt(self, *, chat_id: int, text: str, images=(), files=()):
            del images, files
            assert self._activity_handler is not None
            await self._activity_handler(
                chat_id,
                AgentActivityBlock(
                    kind="execute",
                    title="Run command",
                    status="in_progress",
                    text="",
                    activity_id="shared-id",
                ),
            )
            return AgentReply(text=f"done:{text}")

    bridge = TelegramBridge(
        config=make_config(token="TOKEN", allowed_user_ids=[], workspace=".", activity_mode="normal"),
        agent_service=cast(AgentService, ReusedActivityIdService()),
    )
    bot = DummyBot()
    bridge._app = cast(Application, SimpleNamespace(bot=bot))

    first_update = make_update(chat_id=TEST_CHAT_ID, text="first")
    second_update = make_update(chat_id=TEST_CHAT_ID, text="second")
    first_context = make_context()
    first_context.bot = bot
    second_context = make_context()
    second_context.bot = bot

    await bridge.on_message(first_update, first_context)
    await bridge.on_message(second_update, second_context)

    activity_texts = [
        cast(str, payload["text"]) for payload in bot.sent_messages if "Running" in cast(str, payload["text"])
    ]
    assert len(activity_texts) == EXPECTED_REPEATED_ACTIVITY_MESSAGES
