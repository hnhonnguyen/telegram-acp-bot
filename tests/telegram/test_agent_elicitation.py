from __future__ import annotations

# ruff: noqa: F403, F405, I001

from collections.abc import Awaitable, Callable, Mapping

from telegram import InlineKeyboardButton, MessageEntity
from telegram.error import BadRequest
from telegram_acp_bot.acp.models import ElicitationOption, ElicitationQuestion, ElicitationRequest
from telegram_acp_bot.telegram.constants import (
    ELICITATION_ANSWER_RECORD_PREFIX,
    ELICITATION_CALLBACK_PREFIX,
    ELICITATION_FORMS_MAX,
    ELICITATION_KEYBOARD_COLUMNS,
    ELICITATION_OPTION_LABEL_CHARS,
    ELICITATION_REMOVED_KEYBOARD,
    ELICITATION_SELECTED_PREFIX,
    ELICITATION_SKIP_LABEL,
    ELICITATION_SKIP_RECORD,
    ELICITATION_SKIP_TOKEN,
    ELICITATION_SUBMIT_LABEL,
    ELICITATION_SUBMIT_TOKEN,
)
from tests.telegram.support import *

CATEGORY_OPTIONS = (("category", "Theo danh mục"), ("month", "Theo tháng"))
SPENT_OPTIONS = (("food", "Ăn uống"), ("rent", "Nhà trọ"))


class TapQuery:
    """Callback query that records every keyboard and text the bridge redraws on the tapped message."""

    def __init__(
        self,
        data: str,
        *,
        message: DummyMessage,
        edit_error: Exception | None = None,
        text_edit_error: Exception | None = None,
    ) -> None:
        self.data = data
        self.message = message
        self.answers: list[str] = []
        self.markups: list[object] = []
        self.texts: list[dict[str, object]] = []
        self.edit_error = edit_error
        self.text_edit_error = text_edit_error

    async def answer(self, text: str | None = None) -> None:
        self.answers.append(text or "")

    async def edit_message_text(self, text: str, **kwargs: object) -> None:
        error = self.text_edit_error or self.edit_error
        if error is not None:
            raise error
        self.texts.append({"text": text, **kwargs})

    async def edit_message_reply_markup(
        self,
        *,
        reply_markup: object | None = None,
        api_kwargs: dict[str, object] | None = None,
    ) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        # Telegram only sees the merged payload, so api_kwargs wins over an omitted reply_markup.
        payload: dict[str, object] = {"reply_markup": reply_markup}
        payload.update(api_kwargs or {})
        self.markups.append(payload["reply_markup"])


class ElicitationService(ImplicitSessionServiceBase):
    """Service double that accepts the bridge as its elicitation handler and records tapped answers."""

    def __init__(self, *, resolve: bool = True, error: Exception | None = None) -> None:
        super().__init__()
        self.resolve = resolve
        self.error = error
        self.handler: Callable[[ElicitationRequest], Awaitable[None]] | None = None
        self.expiry_handler: Callable[[int, str], Awaitable[None]] | None = None
        self.resolved: list[tuple[int, str, Mapping[str, tuple[str, ...]] | None]] = []

    def set_elicitation_request_handler(
        self,
        handler: Callable[[ElicitationRequest], Awaitable[None]] | None,
    ) -> None:
        self.handler = handler

    def set_elicitation_expiry_handler(
        self,
        handler: Callable[[int, str], Awaitable[None]] | None,
    ) -> None:
        self.expiry_handler = handler

    async def respond_elicitation_request(
        self,
        *,
        chat_id: int,
        request_id: str,
        answers: Mapping[str, tuple[str, ...]] | None,
    ) -> bool:
        self.resolved.append((chat_id, request_id, answers))
        if self.error is not None:
            raise self.error
        return self.resolve


def make_elicitation_bridge(
    *,
    resolve: bool = True,
    error: Exception | None = None,
    allowed_ids: set[int] | None = None,
    compact: bool = False,
) -> tuple[TelegramBridge, ElicitationService]:
    service = ElicitationService(resolve=resolve, error=error)
    config = make_config(
        token="TOKEN",
        allowed_user_ids=list(allowed_ids or []),
        workspace=".",
        compact_activity=compact,
    )
    bridge = TelegramBridge(config=config, agent_service=cast(AgentService, service))
    bridge._app = cast(Application, SimpleNamespace(bot=DummyBot()))
    return bridge, service


def _bot(bridge: TelegramBridge) -> DummyBot:
    assert bridge._app is not None
    return cast(DummyBot, bridge._app.bot)


def _question(
    *,
    field: str = "q0",
    prompt: str = "Báo cáo theo",
    options: tuple[tuple[str, str], ...] = CATEGORY_OPTIONS,
    multiple: bool = False,
) -> ElicitationQuestion:
    return ElicitationQuestion(
        field=field,
        prompt=prompt,
        options=tuple(ElicitationOption(value=value, label=label) for value, label in options),
        multiple=multiple,
    )


def _request(
    *questions: ElicitationQuestion,
    request_id: str = "abc123",
    title: str = "Answer Questions",
) -> ElicitationRequest:
    return ElicitationRequest(
        chat_id=TEST_CHAT_ID,
        request_id=request_id,
        title=title,
        questions=questions or (_question(),),
    )


def _tap(
    request_id: str,
    token: str,
    *,
    edit_error: Exception | None = None,
    text_edit_error: Exception | None = None,
) -> tuple[Update, TapQuery]:
    tapped = DummyMessage("The agent is asking", message_id=7)
    query = TapQuery(
        f"{ELICITATION_CALLBACK_PREFIX}|{request_id}|{token}",
        message=tapped,
        edit_error=edit_error,
        text_edit_error=text_edit_error,
    )
    update = cast(
        Update,
        SimpleNamespace(
            effective_user=SimpleNamespace(id=1, username=None),
            effective_chat=SimpleNamespace(id=TEST_CHAT_ID, type="private"),
            callback_query=query,
            message=None,
            effective_message=tapped,
        ),
    )
    return update, query


def _keyboard(bot: DummyBot) -> InlineKeyboardMarkup:
    markup = bot.sent_messages[-1]["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    return markup


def _cleared(query: TapQuery) -> bool:
    """Telegram removes a keyboard only from a raw empty inline_keyboard: omitting it or sending an empty
    InlineKeyboardMarkup (which serializes to ``{}``) both mean "change nothing"."""
    if query.texts:
        # A resolved tap records the answer and removes the keyboard in one edit_message_text call.
        return query.texts[-1].get("api_kwargs") == ELICITATION_REMOVED_KEYBOARD
    return bool(query.markups) and query.markups[-1] == {"inline_keyboard": []}


def _buttons(markup: InlineKeyboardMarkup) -> list[InlineKeyboardButton]:
    return [button for row in markup.inline_keyboard for button in row]


async def test_bridge_receives_elicitation_questions_from_a_supported_service():
    _, service = make_elicitation_bridge()

    assert service.handler is not None


async def test_elicitation_offers_one_button_per_option_and_a_skip_row():
    bridge, _ = make_elicitation_bridge()

    await bridge.on_elicitation_request(_request())

    payload = _bot(bridge).sent_messages[-1]
    assert payload["chat_id"] == TEST_CHAT_ID
    text = cast(str, payload["text"])
    assert "The agent is asking" in text
    assert "Answer Questions" in text
    assert "Báo cáo theo" in text
    rows = _keyboard(_bot(bridge)).inline_keyboard
    assert [button.text for button in rows[0]] == ["Theo danh mục", "Theo tháng"]
    assert rows[-1][0].text == ELICITATION_SKIP_LABEL
    assert rows[-1][0].callback_data == f"ask|abc123|{ELICITATION_SKIP_TOKEN}"


async def test_elicitation_is_dropped_before_a_application_can_send_it():
    service = ElicitationService()
    bridge = TelegramBridge(
        config=make_config(token="TOKEN", allowed_user_ids=[], workspace="."),
        agent_service=cast(AgentService, service),
    )
    bridge._app = None

    await bridge.on_elicitation_request(_request())

    assert bridge._elicitation_forms == {}


async def test_elicitation_keyboard_wraps_options_into_the_configured_columns():
    bridge, _ = make_elicitation_bridge()
    options = tuple((f"o{index}", f"Option {index}") for index in range(ELICITATION_KEYBOARD_COLUMNS + 1))

    await bridge.on_elicitation_request(_request(_question(options=options)))

    rows = _keyboard(_bot(bridge)).inline_keyboard
    assert [len(row) for row in rows] == [ELICITATION_KEYBOARD_COLUMNS, 1, 1]


async def test_option_labels_are_cut_to_the_button_budget_and_fall_back_to_the_value():
    bridge, _ = make_elicitation_bridge()
    long_label = "x" * (ELICITATION_OPTION_LABEL_CHARS + 10)

    await bridge.on_elicitation_request(_request(_question(options=((long_label, ""), ("other", "Second")))))

    first, second = _keyboard(_bot(bridge)).inline_keyboard[0]
    assert first.text == "x" * (ELICITATION_OPTION_LABEL_CHARS - 1) + "…"
    assert second.text == "Second"


async def test_multi_select_offers_a_submit_row_and_marks_every_pick():
    bridge, service = make_elicitation_bridge()
    request = _request(_question(multiple=True, options=SPENT_OPTIONS))

    await bridge.on_elicitation_request(request)

    buttons = _buttons(_keyboard(_bot(bridge)))
    assert buttons[-2].text == ELICITATION_SUBMIT_LABEL
    assert buttons[-2].callback_data == f"ask|{request.request_id}|{ELICITATION_SUBMIT_TOKEN}"

    update, query = _tap(request.request_id, "0.0")
    await bridge.on_elicitation_callback(update, make_context())
    assert query.answers == ["Pick every answer, then tap Send my answer."]
    redrawn = query.markups[-1]
    assert isinstance(redrawn, InlineKeyboardMarkup)
    assert _buttons(redrawn)[0].text == f"{ELICITATION_SELECTED_PREFIX}Ăn uống"

    submit_tap, submit_query = _tap(request.request_id, ELICITATION_SUBMIT_TOKEN)
    await bridge.on_elicitation_callback(submit_tap, make_context())

    assert submit_query.answers == ["Answer sent."]
    assert submit_query.texts and _cleared(submit_query)
    assert service.resolved == [(TEST_CHAT_ID, request.request_id, {"q0": ("food",)})]


async def test_a_single_choice_tap_answers_the_form_at_once():
    bridge, service = make_elicitation_bridge()
    request = _request()
    await bridge.on_elicitation_request(request)

    update, query = _tap(request.request_id, "0.1")
    await bridge.on_elicitation_callback(update, make_context())

    assert query.answers == ["Answer sent."]
    assert service.resolved == [(TEST_CHAT_ID, request.request_id, {"q0": ("month",)})]
    assert bridge._elicitation_forms == {}
    assert bridge._elicitation_selections == {}


async def test_the_tapped_answer_is_written_on_the_question_message():
    bridge, service = make_elicitation_bridge()
    request = _request()
    await bridge.on_elicitation_request(request)

    update, query = _tap(request.request_id, "0.1")
    await bridge.on_elicitation_callback(update, make_context())

    assert query.answers == ["Answer sent."]
    recorded = query.texts[-1]
    assert recorded["text"] == f"The agent is asking\n{ELICITATION_ANSWER_RECORD_PREFIX} Theo tháng"
    assert recorded["api_kwargs"] == ELICITATION_REMOVED_KEYBOARD
    assert service.resolved == [(TEST_CHAT_ID, request.request_id, {"q0": ("month",)})]


async def test_a_two_question_answer_numbers_every_choice():
    bridge, _ = make_elicitation_bridge()
    request = _request(
        _question(),
        _question(field="q1", prompt="Kỳ báo cáo", options=(("2026-09", "Tháng 9"), ("2026-10", "Tháng 10"))),
    )
    await bridge.on_elicitation_request(request)

    first, _ = _tap(request.request_id, "0.1")
    await bridge.on_elicitation_callback(first, make_context())
    second, second_query = _tap(request.request_id, "1.1")
    await bridge.on_elicitation_callback(second, make_context())

    recorded = second_query.texts[-1]
    text = cast(str, recorded["text"])
    assert text.endswith(f"{ELICITATION_ANSWER_RECORD_PREFIX} 1. Theo tháng / 2. Tháng 10")


async def test_the_answer_record_reuses_the_message_entities():
    bridge, _ = make_elicitation_bridge()
    request = _request()
    await bridge.on_elicitation_request(request)

    entity = MessageEntity(offset=19, length=5, type=MessageEntity.BOLD)
    tapped = DummyMessage("The agent is asking", message_id=7, entities=[entity])
    query = TapQuery(f"{ELICITATION_CALLBACK_PREFIX}|{request.request_id}|0.0", message=tapped)
    update = cast(
        Update,
        SimpleNamespace(
            effective_user=SimpleNamespace(id=1, username=None),
            effective_chat=SimpleNamespace(id=TEST_CHAT_ID, type="private"),
            callback_query=query,
            message=None,
            effective_message=tapped,
        ),
    )
    await bridge.on_elicitation_callback(update, make_context())

    recorded = query.texts[-1]
    assert recorded["entities"] == [entity]
    assert recorded["api_kwargs"] == ELICITATION_REMOVED_KEYBOARD


async def test_a_refused_answer_record_still_clears_the_keyboard():
    bridge, service = make_elicitation_bridge()
    request = _request()
    await bridge.on_elicitation_request(request)

    update, query = _tap(request.request_id, "0.0", text_edit_error=BadRequest("Bad chat"))
    await bridge.on_elicitation_callback(update, make_context())

    assert query.answers == ["Answer sent."]
    assert query.texts == []
    assert query.markups[-1] == {"inline_keyboard": []}
    assert service.resolved == [(TEST_CHAT_ID, request.request_id, {"q0": ("category",)})]


async def test_a_two_question_form_lists_both_prompts_and_waits_for_the_second_answer():
    bridge, service = make_elicitation_bridge()
    request = _request(
        _question(),
        _question(field="q1", prompt="Kỳ báo cáo", options=(("2026-09", "Tháng 9"), ("2026-10", "Tháng 10"))),
    )

    await bridge.on_elicitation_request(request)

    text = cast(str, _bot(bridge).sent_messages[-1]["text"])
    assert "1. Báo cáo theo" in text
    assert "2. Kỳ báo cáo" in text

    first, first_query = _tap(request.request_id, "0.0")
    await bridge.on_elicitation_callback(first, make_context())
    assert first_query.answers == ["Pick every answer, then tap Send my answer."]
    assert service.resolved == []

    second, second_query = _tap(request.request_id, "1.1")
    await bridge.on_elicitation_callback(second, make_context())

    assert second_query.answers == ["Answer sent."]
    assert service.resolved == [(TEST_CHAT_ID, request.request_id, {"q0": ("category",), "q1": ("2026-10",)})]


async def test_a_multi_select_question_says_its_answers_may_be_combined():
    bridge, _ = make_elicitation_bridge()
    request = _request(_question(), _question(field="q1", prompt="Nhóm", options=SPENT_OPTIONS, multiple=True))

    await bridge.on_elicitation_request(request)

    text = cast(str, _bot(bridge).sent_messages[-1]["text"])
    assert "2. Nhóm (pick one or more)" in text


async def test_a_second_tap_replaces_the_pick_of_a_single_choice_question():
    bridge, service = make_elicitation_bridge()
    request = _request(_question(), _question(field="q1", options=(("a", "A"), ("b", "B"))))
    await bridge.on_elicitation_request(request)

    first, _ = _tap(request.request_id, "0.0")
    await bridge.on_elicitation_callback(first, make_context())
    replaced, _ = _tap(request.request_id, "0.1")
    await bridge.on_elicitation_callback(replaced, make_context())
    second, second_query = _tap(request.request_id, "1.0")
    await bridge.on_elicitation_callback(second, make_context())

    assert second_query.answers == ["Answer sent."]
    assert service.resolved == [(TEST_CHAT_ID, request.request_id, {"q0": ("month",), "q1": ("a",)})]


async def test_a_tap_toggles_a_multi_select_pick_off_again():
    bridge, service = make_elicitation_bridge()
    request = _request(_question(multiple=True, options=SPENT_OPTIONS))
    await bridge.on_elicitation_request(request)

    on, _ = _tap(request.request_id, "0.0")
    await bridge.on_elicitation_callback(on, make_context())
    off, off_query = _tap(request.request_id, "0.0")
    await bridge.on_elicitation_callback(off, make_context())
    redrawn = off_query.markups[-1]
    assert isinstance(redrawn, InlineKeyboardMarkup)
    assert not _buttons(redrawn)[0].text.startswith(ELICITATION_SELECTED_PREFIX)

    submit, submit_query = _tap(request.request_id, ELICITATION_SUBMIT_TOKEN)
    await bridge.on_elicitation_callback(submit, make_context())

    assert submit_query.answers == ["Pick at least one option for every question."]
    assert service.resolved == []


async def test_a_multi_select_submit_without_any_pick_is_refused():
    bridge, service = make_elicitation_bridge()
    request = _request(_question(multiple=True))
    await bridge.on_elicitation_request(request)

    update, query = _tap(request.request_id, ELICITATION_SUBMIT_TOKEN)
    await bridge.on_elicitation_callback(update, make_context())

    assert query.answers == ["Pick at least one option for every question."]
    assert service.resolved == []


async def test_the_skip_button_declines_without_an_answer():
    bridge, service = make_elicitation_bridge()
    request = _request()
    await bridge.on_elicitation_request(request)

    update, query = _tap(request.request_id, ELICITATION_SKIP_TOKEN)
    await bridge.on_elicitation_callback(update, make_context())

    assert query.answers == ["Skipped."]
    assert service.resolved == [(TEST_CHAT_ID, request.request_id, None)]
    assert query.texts and _cleared(query)
    assert query.texts[-1]["text"] == f"The agent is asking\n{ELICITATION_SKIP_RECORD}"


async def test_a_tap_on_a_question_that_stopped_waiting_is_reported():
    bridge, service = make_elicitation_bridge()

    update, query = _tap("gone000", "0.0")
    await bridge.on_elicitation_callback(update, make_context())

    assert query.answers == ["This question is no longer waiting for an answer."]
    assert service.resolved == []


async def test_callback_data_from_another_keyboard_is_rejected():
    bridge, _ = make_elicitation_bridge()
    tapped = DummyMessage("The agent is asking", message_id=7)
    query = TapQuery("resume|1", message=tapped)
    update = cast(
        Update,
        SimpleNamespace(
            effective_user=SimpleNamespace(id=1, username=None),
            effective_chat=SimpleNamespace(id=TEST_CHAT_ID, type="private"),
            callback_query=query,
            message=None,
            effective_message=tapped,
        ),
    )

    await bridge.on_elicitation_callback(update, make_context())

    assert query.answers == ["Invalid action."]


async def test_a_tap_that_names_no_offered_option_is_reported():
    bridge, service = make_elicitation_bridge()
    request = _request()
    await bridge.on_elicitation_request(request)

    for token in ("0.x", "9.0", "0.9"):
        update, query = _tap(request.request_id, token)
        await bridge.on_elicitation_callback(update, make_context())
        assert query.answers == ["That option is no longer available."]

    assert service.resolved == []


async def test_an_answer_for_a_form_the_agent_already_dropped_is_reported():
    bridge, service = make_elicitation_bridge(resolve=False)
    request = _request()
    await bridge.on_elicitation_request(request)

    update, query = _tap(request.request_id, "0.0")
    await bridge.on_elicitation_callback(update, make_context())

    assert query.answers == ["This question is no longer waiting for an answer."]
    assert query.markups == []
    assert service.resolved == [(TEST_CHAT_ID, request.request_id, {"q0": ("category",)})]
    assert bridge._elicitation_forms == {}


async def test_an_answer_the_service_cannot_take_leaves_the_keyboard_up():
    bridge, _ = make_elicitation_bridge(error=RuntimeError("agent connection lost"))
    request = _request()
    await bridge.on_elicitation_request(request)

    update, query = _tap(request.request_id, "0.0")
    await bridge.on_elicitation_callback(update, make_context())

    assert query.answers == ["Answering this question failed."]
    assert request.request_id in bridge._elicitation_forms


async def test_only_the_most_recent_forms_keep_their_keyboards_alive():
    bridge, service = make_elicitation_bridge()
    offered = [_request(request_id=f"id{index}") for index in range(ELICITATION_FORMS_MAX + 2)]
    for request in offered:
        await bridge.on_elicitation_request(request)

    assert list(bridge._elicitation_forms) == [request.request_id for request in offered[-ELICITATION_FORMS_MAX:]]
    assert len(bridge._elicitation_selections) == ELICITATION_FORMS_MAX
    assert len(service.resolved) == len(offered) - ELICITATION_FORMS_MAX

    stale, stale_query = _tap(offered[0].request_id, "0.0")
    await bridge.on_elicitation_callback(stale, make_context())
    assert stale_query.answers == ["This question is no longer waiting for an answer."]


async def test_a_question_dropped_from_the_form_list_is_declined_and_loses_its_keyboard():
    bridge, service = make_elicitation_bridge()
    offered = [_request(request_id=f"id{index}") for index in range(ELICITATION_FORMS_MAX + 2)]
    message_ids: dict[str, int | None] = {}
    for request in offered:
        await bridge.on_elicitation_request(request)
        message_ids[request.request_id] = bridge._elicitation_messages[request.request_id]

    dropped = offered[:2]
    assert service.resolved == [(TEST_CHAT_ID, request.request_id, None) for request in dropped]
    edits = _bot(bridge).edited_reply_markups
    assert [edit["message_id"] for edit in edits] == [message_ids[request.request_id] for request in dropped]
    assert all(edit["api_kwargs"] == {"reply_markup": {"inline_keyboard": []}} for edit in edits)
    for request in dropped:
        assert request.request_id not in bridge._elicitation_forms
        assert request.request_id not in bridge._elicitation_messages
        assert request.request_id not in bridge._elicitation_selections


async def test_offering_a_question_closes_the_status_bubble_and_the_next_activity_opens_a_new_one():
    bridge, _ = make_elicitation_bridge(compact=True)
    bot = _bot(bridge)
    think = AgentActivityBlock(kind="think", title="Checking", status="in_progress", text="")
    await bridge.on_activity_event(TEST_CHAT_ID, think)
    old_bubble = bridge._compact_status_msg_id[TEST_CHAT_ID]

    await bridge.on_elicitation_request(_request())

    # The pre-question bubble is retired, not deleted: it stays as history.
    assert TEST_CHAT_ID not in bridge._compact_status_msg_id
    assert TEST_CHAT_ID not in bridge._compact_status_label
    assert TEST_CHAT_ID not in bridge._compact_status_tasks
    assert bot.deleted_message_ids == []

    await bridge.on_activity_event(
        TEST_CHAT_ID, AgentActivityBlock(kind="execute", title="Run ls", status="in_progress", text="")
    )

    new_bubble = bridge._compact_status_msg_id[TEST_CHAT_ID]
    assert new_bubble != old_bubble
    assert not any(edit.get("message_id") == old_bubble for edit in bot.edited_messages), (
        "the retired bubble was overwritten after the question"
    )


async def test_a_burst_in_one_chat_does_not_evict_another_chat_open_question():
    bridge, service = make_elicitation_bridge()
    other_chat = ElicitationRequest(
        chat_id=TEST_CHAT_ID + 1,
        request_id="other-chat",
        title="Answer Questions",
        questions=(_question(),),
    )
    await bridge.on_elicitation_request(other_chat)
    burst = [_request(request_id=f"burst{index}") for index in range(ELICITATION_FORMS_MAX + 2)]
    for request in burst:
        await bridge.on_elicitation_request(request)

    assert "other-chat" in bridge._elicitation_forms
    assert "other-chat" in bridge._elicitation_selections
    # Only the burst's own chat is capped, and only its own oldest questions were declined.
    assert sum(1 for form in bridge._elicitation_forms.values() if form.chat_id == TEST_CHAT_ID) == (
        ELICITATION_FORMS_MAX
    )
    assert service.resolved == [(TEST_CHAT_ID, request.request_id, None) for request in burst[:2]]
    assert all(edit["chat_id"] == TEST_CHAT_ID for edit in _bot(bridge).edited_reply_markups)

    tap, query = _tap("other-chat", "0.0")
    await bridge.on_elicitation_callback(tap, make_context())
    assert query.answers == ["Answer sent."]


async def test_a_denied_tap_answers_over_its_own_button():
    bridge, _ = make_elicitation_bridge(allowed_ids={42})
    request = _request()
    await bridge.on_elicitation_request(request)

    _, query = _tap(request.request_id, "0.0")
    denied = cast(
        Update,
        SimpleNamespace(
            effective_user=SimpleNamespace(id=7, username="stranger"),
            effective_chat=SimpleNamespace(id=TEST_CHAT_ID, type="private"),
            callback_query=query,
            message=None,
            effective_message=query.message,
        ),
    )
    await bridge.on_elicitation_callback(denied, make_context())

    assert query.answers == ["Access denied."]
    assert query.message.replies == ["Access denied for this bot."]


async def test_a_query_without_a_callback_is_ignored():
    bridge, service = make_elicitation_bridge()
    update = cast(
        Update,
        SimpleNamespace(
            effective_user=SimpleNamespace(id=1, username=None),
            effective_chat=SimpleNamespace(id=TEST_CHAT_ID, type="private"),
            callback_query=None,
            message=None,
            effective_message=None,
        ),
    )

    await bridge.on_elicitation_callback(update, make_context())

    assert service.resolved == []


async def test_the_elicitation_keyboard_is_registered_after_the_command_keyboard():
    bridge, service = make_elicitation_bridge()
    config = make_config(token="TOKEN", allowed_user_ids=[], workspace=".")
    app = build_application(config, bridge)

    callbacks = [handler.callback for handler in app.handlers[0]]
    assert service.handler == bridge.on_elicitation_request
    assert callbacks.count(bridge.on_elicitation_callback) == 1
    assert callbacks.index(bridge.on_agent_command_callback) < callbacks.index(bridge.on_elicitation_callback)


async def test_a_keyboard_the_agent_left_unchanged_is_not_reported_as_a_failure():
    bridge, service = make_elicitation_bridge()
    request = _request(_question(multiple=True, options=SPENT_OPTIONS))

    await bridge.on_elicitation_request(request)

    update, query = _tap(request.request_id, "0.1", edit_error=BadRequest("Message is not modified: same"))
    await bridge.on_elicitation_callback(update, make_context())

    assert query.answers == ["Pick every answer, then tap Send my answer."]
    assert service.resolved == []


async def test_a_real_edit_failure_still_answers_with_an_error():
    bridge, service = make_elicitation_bridge()
    request = _request(_question(options=SPENT_OPTIONS))

    await bridge.on_elicitation_request(request)

    update, query = _tap(request.request_id, ELICITATION_SKIP_TOKEN, edit_error=BadRequest("Bad chat"))
    await bridge.on_elicitation_callback(update, make_context())

    assert query.answers == ["Skipped.", "Answering this question failed."]
    assert service.resolved == [(TEST_CHAT_ID, request.request_id, None)]


KEYBOARD_CLEAR_REFUSED = BadRequest("Bad chat")


class _RefusingMarkupBot(DummyBot):
    """Bot whose keyboard removal fails, the way an edit Telegram refuses can."""

    async def edit_message_reply_markup(self, **kwargs: object) -> None:
        del kwargs
        raise KEYBOARD_CLEAR_REFUSED


async def test_a_question_the_agent_abandoned_loses_its_keyboard():
    bridge, service = make_elicitation_bridge()
    request = _request(_question())

    await bridge.on_elicitation_request(request)

    assert service.expiry_handler == bridge.on_elicitation_expired
    message_id = bridge._elicitation_messages[request.request_id]

    await bridge.on_elicitation_expired(TEST_CHAT_ID, request.request_id)

    assert _bot(bridge).edited_reply_markups == [
        {
            "chat_id": TEST_CHAT_ID,
            "message_id": message_id,
            "api_kwargs": {"reply_markup": {"inline_keyboard": []}},
        }
    ]
    assert bridge._elicitation_forms == {}
    assert bridge._elicitation_messages == {}
    assert bridge._elicitation_selections == {}


async def test_a_question_the_user_already_answered_is_not_touched_again():
    bridge, _ = make_elicitation_bridge()
    request = _request(_question())

    await bridge.on_elicitation_request(request)
    update, query = _tap(request.request_id, "0.0")
    await bridge.on_elicitation_callback(update, make_context())
    assert query.answers == ["Answer sent."]

    await bridge.on_elicitation_expired(TEST_CHAT_ID, request.request_id)

    assert _bot(bridge).edited_reply_markups == []


async def test_a_keyboard_telegram_refuses_to_clear_is_only_logged():
    bridge, _ = make_elicitation_bridge()
    request = _request(_question())

    await bridge.on_elicitation_request(request)
    assert bridge._app is not None
    bridge._app.bot = _RefusingMarkupBot()

    await bridge.on_elicitation_expired(TEST_CHAT_ID, request.request_id)

    assert bridge._elicitation_forms == {}


async def test_an_expired_question_is_ignored_before_the_bot_is_running():
    bridge, _ = make_elicitation_bridge()
    request = _request(_question())

    await bridge.on_elicitation_request(request)
    bridge._app = None

    await bridge.on_elicitation_expired(TEST_CHAT_ID, request.request_id)

    assert bridge._elicitation_forms == {request.request_id: request}


async def test_a_close_for_a_question_the_bridge_never_offered_does_nothing():
    bridge, _ = make_elicitation_bridge()

    await bridge.on_elicitation_expired(TEST_CHAT_ID, "missing")

    assert _bot(bridge).edited_reply_markups == []
