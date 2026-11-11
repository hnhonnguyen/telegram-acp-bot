from __future__ import annotations

# ruff: noqa: F403, F405, I001

from acp.schema import (
    AcceptElicitationResponse,
    ClientCapabilities,
    CreateElicitationResponse,
    ElicitationBooleanPropertySchema,
    ElicitationFormRequestMode,
    ElicitationFormSessionMode,
    ElicitationMultiSelectPropertySchema,
    ElicitationNumberPropertySchema,
    ElicitationSchema,
    ElicitationStringPropertySchema,
    ElicitationUrlSessionMode,
    EnumOption,
    OtherMultiSelectItems,
    StringMultiSelectItems,
    TitledMultiSelectItems,
)
from telegram.error import NetworkError
from telegram_acp_bot.acp.elicitation import elicitation_content, elicitation_form, elicitation_questions
from telegram_acp_bot.acp.models import ElicitationRequest
from tests.acp.support import *

REPORT_FORMAT_OPTIONS = (("category", "Theo danh mục"), ("month", "Theo tháng"))


def _single_choice_field(
    *,
    description: str = "Chọn nhóm để tổng hợp",
    options: tuple[tuple[str, str], ...] = REPORT_FORMAT_OPTIONS,
) -> ElicitationStringPropertySchema:
    """A field in the shape qodercli really sends for a single-choice question."""
    return ElicitationStringPropertySchema(
        type="string",
        title="Báo cáo theo",
        description=description,
        one_of=[EnumOption(const=value, title=label) for value, label in options],
    )


def _multi_select_field(*, titled: bool = True) -> ElicitationMultiSelectPropertySchema:
    items: object
    if titled:
        items = TitledMultiSelectItems(
            any_of=[EnumOption(const="food", title="Ăn uống"), EnumOption(const="rent", title="Nhà trọ")]
        )
    else:
        items = StringMultiSelectItems(type="string", enum=["food"])
    return ElicitationMultiSelectPropertySchema(
        type="array",
        title="Danh mục",
        description="Chọn danh mục cần gộp",
        min_items=1,
        items=items,
    )


def _schema(
    **properties: ElicitationStringPropertySchema
    | ElicitationNumberPropertySchema
    | ElicitationBooleanPropertySchema
    | ElicitationMultiSelectPropertySchema,
) -> ElicitationSchema:
    return ElicitationSchema(
        type="object",
        title="Answer Questions",
        properties=properties,
        required=list(properties),
    )


def _form_mode(schema: ElicitationSchema, *, session_id: str = "elicit") -> ElicitationFormSessionMode:
    return ElicitationFormSessionMode(session_id=session_id, tool_call_id="tc-1", requested_schema=schema)


def _one_question_mode() -> ElicitationFormSessionMode:
    return _form_mode(_schema(q0=_single_choice_field()))


async def _start_service(
    tmp_path: Path,
    session_id: str = "elicit",
    connection: FakeConnection | None = None,
) -> AcpAgentService:
    connection = connection or FakeConnection(session_id=session_id)
    process = FakeProcess()

    async def fake_spawn(program: str, *args: str, **kwargs):
        del program, args, kwargs
        return process

    def fake_connect(client, input_stream, output_stream, **kwargs):
        del input_stream, output_stream
        connection.client = client
        connection.connect_kwargs = kwargs
        return connection

    service = AcpAgentService(
        SessionRegistry(),
        program="agent",
        args=[],
        spawner=fake_spawn,
        connector=fake_connect,
    )
    await service.new_session(chat_id=1, workspace=tmp_path)
    return service


def test_only_a_form_bound_to_a_session_can_be_rendered() -> None:
    schema = _schema(q0=_single_choice_field())
    session_mode = _form_mode(schema)
    assert elicitation_form(session_mode) is session_mode
    assert elicitation_form(ElicitationFormRequestMode(request_id="r", requested_schema=schema)) is None
    assert (
        elicitation_form(ElicitationUrlSessionMode(session_id="s", url="https://x.test/e", elicitation_id="e")) is None
    )
    assert elicitation_form(object()) is None


def test_a_single_choice_field_becomes_one_option_per_declared_answer() -> None:
    questions = elicitation_questions(_schema(q0=_single_choice_field()))
    assert questions is not None
    (question,) = questions
    assert question.field == "q0"
    assert question.prompt == "Chọn nhóm để tổng hợp"
    assert question.multiple is False
    assert [(option.value, option.label) for option in question.options] == list(REPORT_FORMAT_OPTIONS)


def test_a_multi_select_field_becomes_options_that_combine() -> None:
    questions = elicitation_questions(_schema(q1=_multi_select_field()))
    assert questions is not None
    (question,) = questions
    assert question.multiple is True
    assert [(option.value, option.label) for option in question.options] == [("food", "Ăn uống"), ("rent", "Nhà trọ")]


def test_a_multi_select_field_without_titles_repeats_its_values() -> None:
    field = ElicitationMultiSelectPropertySchema(
        type="array",
        title="Flags",
        items=StringMultiSelectItems(type="string", enum=["a", "b"]),
    )
    questions = elicitation_questions(_schema(q2=field))
    assert questions is not None
    assert [(option.value, option.label) for option in questions[0].options] == [("a", "a"), ("b", "b")]


def test_a_field_the_user_must_type_into_is_not_rendered_as_choices() -> None:
    for name, field in (
        ("number", ElicitationNumberPropertySchema(type="number", title="Amount")),
        ("boolean", ElicitationBooleanPropertySchema(type="boolean", title="Confirm")),
        ("free_text", ElicitationStringPropertySchema(type="string", title="Note")),
        ("empty_choice", ElicitationStringPropertySchema(type="string", title="Nothing", one_of=[])),
        (
            "empty_multi",
            ElicitationMultiSelectPropertySchema(
                type="array", title="Empty", items=StringMultiSelectItems(type="string", enum=[])
            ),
        ),
        (
            "typed_multi",
            ElicitationMultiSelectPropertySchema(
                type="array", title="Count", items=OtherMultiSelectItems(type="integer")
            ),
        ),
    ):
        assert elicitation_questions(_schema(**{name: field})) is None


def test_two_questions_arrive_as_one_request_with_both_fields() -> None:
    questions = elicitation_questions(_schema(q0=_single_choice_field(), q1=_multi_select_field()))
    assert questions is not None
    assert [question.field for question in questions] == ["q0", "q1"]


def test_a_prompt_without_a_description_falls_back_to_the_title_then_the_field() -> None:
    titled = ElicitationStringPropertySchema(
        type="string", title="Khoảng thời gian", one_of=[EnumOption(const="m", title="M")]
    )
    unnamed = ElicitationStringPropertySchema(type="string", one_of=[EnumOption(const="m", title="Month")])
    questions = elicitation_questions(_schema(q0=titled, q1=unnamed))
    assert questions is not None
    assert questions[0].prompt == "Khoảng thời gian"
    assert questions[1].prompt == "q1"
    assert questions[1].options[0].label == "Month"


def test_an_answered_single_choice_field_is_sent_as_one_value() -> None:
    questions = elicitation_questions(_schema(q0=_single_choice_field()))
    assert questions is not None
    assert elicitation_content(questions, {"q0": ("month",)}) == {"q0": "month"}


def test_an_answered_multi_select_field_is_sent_as_a_list() -> None:
    questions = elicitation_questions(_schema(q1=_multi_select_field()))
    assert questions is not None
    assert elicitation_content(questions, {"q1": ("food", "rent")}) == {"q1": ["food", "rent"]}


async def test_client_cancels_an_elicitation_without_a_decider() -> None:
    response = await make_client().create_elicitation(message="Answer Questions", mode=_one_question_mode())
    assert response.action == "cancel"


async def test_client_routes_an_elicitation_to_the_decider() -> None:
    seen: list[tuple[str, str]] = []

    async def decide(message: str, mode: object) -> CreateElicitationResponse:
        seen.append((message, str(getattr(mode, "session_id", ""))))
        return AcceptElicitationResponse(action="accept", content={"q0": "month"})

    async def deny(session_id: str, options: list[PermissionOption], tool_call: ToolCall) -> RequestPermissionResponse:
        del session_id, options, tool_call
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    client = _AcpClient(permission_decider=deny, elicitation_decider=decide)
    response = await client.create_elicitation(message="Answer Questions", mode=_one_question_mode(), field_meta=None)
    assert response.action == "accept"
    assert seen == [("Answer Questions", "elicit")]


async def test_initialize_advertises_form_elicitation(tmp_path: Path) -> None:
    connection = _CapabilityRecordingConnection()
    await _start_service(tmp_path, connection=connection)
    capabilities = cast(ClientCapabilities, connection.initialize_kwargs["client_capabilities"])
    assert capabilities.elicitation is not None
    assert capabilities.elicitation.form is not None


async def test_the_connection_turns_on_the_unstable_protocol(tmp_path: Path) -> None:
    connection = FakeConnection(session_id="unstable")
    await _start_service(tmp_path, connection=connection)
    assert connection.connect_kwargs == {"use_unstable_protocol": True}


class _CapabilityRecordingConnection(FakeConnection):
    """Fake agent that keeps the capabilities the client offered during the handshake."""

    def __init__(self) -> None:
        super().__init__(session_id="caps")
        self.initialize_kwargs: dict[str, object] = {}

    async def initialize(self, **kwargs):
        self.initialize_kwargs = kwargs
        return await super().initialize(**kwargs)


async def test_an_unsupported_form_is_cancelled_without_asking_the_user(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)

    service.set_elicitation_request_handler(_never_prompt)
    mode = _form_mode(_schema(q0=ElicitationNumberPropertySchema(type="number", title="Amount")))
    response = await service._decide_elicitation("Answer Questions", mode)
    assert response.action == "cancel"
    assert service._pending_elicitations == {}


async def test_an_elicitation_for_an_unknown_session_is_cancelled(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)
    service.set_elicitation_request_handler(_ignore)
    mode = _form_mode(_schema(q0=_single_choice_field()), session_id="not-ours")

    response = await service._decide_elicitation("Answer Questions", mode)

    assert response.action == "cancel"


async def test_an_elicitation_in_a_mode_the_bot_cannot_answer_is_cancelled(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)

    service.set_elicitation_request_handler(_never_prompt)
    mode = ElicitationUrlSessionMode(
        session_id="elicit",
        url="https://example.test/sign-in",
        elicitation_id="e-1",
    )

    response = await service._decide_elicitation("Sign in to continue", mode)

    assert response.action == "cancel"
    assert service._pending_elicitations == {}


async def test_an_elicitation_is_cancelled_when_no_handler_can_show_it(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)
    response = await service._decide_elicitation("Answer Questions", _one_question_mode())
    assert response.action == "cancel"
    assert service._pending_elicitations == {}


async def test_choosing_an_option_accepts_the_elicitation_with_that_value(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)
    offered: list[ElicitationRequest] = []

    async def handler(request: ElicitationRequest) -> None:
        offered.append(request)
        await service.respond_elicitation_request(
            chat_id=request.chat_id,
            request_id=request.request_id,
            answers={"q0": ("category",)},
        )

    service.set_elicitation_request_handler(handler)
    response = await service._decide_elicitation("Answer Questions", _one_question_mode())
    assert response.action == "accept"
    assert response.content == {"q0": "category"}
    (request,) = offered
    assert request.title == "Answer Questions"
    assert request.chat_id == 1
    assert request.questions[0].options[0].label == "Theo danh mục"
    assert service._pending_elicitations == {}


async def test_skipping_the_question_declines_the_elicitation(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)

    async def handler(request: ElicitationRequest) -> None:
        await service.respond_elicitation_request(
            chat_id=request.chat_id,
            request_id=request.request_id,
            answers=None,
        )

    service.set_elicitation_request_handler(handler)
    response = await service._decide_elicitation("Answer Questions", _one_question_mode())
    assert response.action == "decline"


async def test_an_answer_outside_the_declared_options_declines_the_elicitation(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)

    async def handler(request: ElicitationRequest) -> None:
        accepted = await service.respond_elicitation_request(
            chat_id=request.chat_id,
            request_id=request.request_id,
            answers={"q0": ("invented",)},
        )
        assert accepted is True

    service.set_elicitation_request_handler(handler)
    response = await service._decide_elicitation("Answer Questions", _one_question_mode())
    assert response.action == "decline"


async def test_two_answers_for_one_single_choice_question_decline_the_elicitation(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)

    async def handler(request: ElicitationRequest) -> None:
        await service.respond_elicitation_request(
            chat_id=request.chat_id,
            request_id=request.request_id,
            answers={"q0": ("month", "category")},
        )

    service.set_elicitation_request_handler(handler)
    response = await service._decide_elicitation("Answer Questions", _one_question_mode())
    assert response.action == "decline"


async def test_partial_answers_do_not_accept_a_two_question_form(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)

    async def handler(request: ElicitationRequest) -> None:
        assert [question.field for question in request.questions] == ["q0", "q1"]
        accepted = await service.respond_elicitation_request(
            chat_id=request.chat_id,
            request_id=request.request_id,
            answers={"q0": ("month",)},
        )
        assert accepted is True

    service.set_elicitation_request_handler(handler)
    mode = _form_mode(_schema(q0=_single_choice_field(), q1=_multi_select_field()))
    response = await service._decide_elicitation("Answer Questions", mode)
    assert response.action == "decline"


async def test_two_answered_questions_are_sent_together(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)

    async def handler(request: ElicitationRequest) -> None:
        await service.respond_elicitation_request(
            chat_id=request.chat_id,
            request_id=request.request_id,
            answers={"q0": ("month",), "q1": ("food", "rent")},
        )

    service.set_elicitation_request_handler(handler)
    mode = _form_mode(_schema(q0=_single_choice_field(), q1=_multi_select_field()))
    response = await service._decide_elicitation("Answer Questions", mode)
    assert response.action == "accept"
    assert response.content == {"q0": "month", "q1": ["food", "rent"]}


async def test_a_second_answer_to_the_same_elicitation_is_ignored(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)
    outcomes: list[bool] = []

    async def handler(request: ElicitationRequest) -> None:
        outcomes.append(
            await service.respond_elicitation_request(
                chat_id=request.chat_id,
                request_id=request.request_id,
                answers={"q0": ("month",)},
            )
        )
        outcomes.append(
            await service.respond_elicitation_request(
                chat_id=request.chat_id,
                request_id=request.request_id,
                answers={"q0": ("category",)},
            )
        )

    service.set_elicitation_request_handler(handler)
    response = await service._decide_elicitation("Answer Questions", _one_question_mode())
    assert response.content == {"q0": "month"}
    assert outcomes == [True, False]


async def test_an_answer_from_another_chat_does_not_resolve_the_elicitation(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)

    async def handler(request: ElicitationRequest) -> None:
        rejected = await service.respond_elicitation_request(
            chat_id=request.chat_id + 1,
            request_id=request.request_id,
            answers={"q0": ("month",)},
        )
        assert rejected is False
        await service.respond_elicitation_request(
            chat_id=request.chat_id,
            request_id=request.request_id,
            answers={"q0": ("category",)},
        )

    service.set_elicitation_request_handler(handler)
    response = await service._decide_elicitation("Answer Questions", _one_question_mode())
    assert response.content == {"q0": "category"}


async def test_responding_to_an_unknown_request_is_ignored(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)
    accepted = await service.respond_elicitation_request(chat_id=1, request_id="missing", answers=None)
    assert accepted is False


async def test_stopping_the_chat_cancels_the_question_it_was_asked(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)
    service.set_elicitation_request_handler(_ignore)

    pending = asyncio.create_task(service._decide_elicitation("Answer Questions", _one_question_mode()))
    while not service._pending_elicitations:
        await asyncio.sleep(0)

    assert await service.stop(chat_id=1)
    response = await pending
    assert response.action == "cancel"
    assert service._pending_elicitations == {}


async def test_an_unanswered_elicitation_is_cancelled_after_the_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _start_service(tmp_path)
    service.set_elicitation_request_handler(_ignore)

    async def fake_wait_for(awaitable: asyncio.Future, **kwargs: object) -> object:
        del kwargs
        awaitable.cancel()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    response = await service._decide_elicitation("Answer Questions", _one_question_mode())
    assert response.action == "cancel"
    assert service._pending_elicitations == {}


async def test_a_question_that_stopped_waiting_tells_the_ui_to_drop_its_keyboard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _start_service(tmp_path)
    offered: list[ElicitationRequest] = []
    closed: list[tuple[int, str]] = []

    async def record(request: ElicitationRequest) -> None:
        offered.append(request)

    async def expiry(chat_id: int, request_id: str) -> None:
        closed.append((chat_id, request_id))

    service.set_elicitation_request_handler(record)
    service.set_elicitation_expiry_handler(expiry)

    async def fake_wait_for(awaitable: asyncio.Future, **kwargs: object) -> object:
        del kwargs
        awaitable.cancel()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    response = await service._decide_elicitation("Answer Questions", _one_question_mode())
    assert response.action == "cancel"
    (request,) = offered
    assert closed == [(request.chat_id, request.request_id)]


async def test_a_question_that_could_not_be_shown_is_declined_without_leaking_its_slot(
    tmp_path: Path,
) -> None:
    service = await _start_service(tmp_path)
    shown: list[ElicitationRequest] = []
    closed: list[tuple[int, str]] = []

    async def failing_handler(request: ElicitationRequest) -> None:
        # Like the real bridge: the form is remembered, then Telegram refuses the send.
        shown.append(request)
        raise NetworkError

    async def expiry(chat_id: int, request_id: str) -> None:
        closed.append((chat_id, request_id))

    service.set_elicitation_request_handler(failing_handler)
    service.set_elicitation_expiry_handler(expiry)
    response = await service._decide_elicitation("Answer Questions", _one_question_mode())
    assert response.action == "cancel"
    assert service._pending_elicitations == {}
    (request,) = shown
    assert closed == [(request.chat_id, request.request_id)]


async def test_a_question_the_user_answered_is_reported_closed_too(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)
    asked: list[str] = []
    closed: list[tuple[int, str]] = []

    async def handler(request: ElicitationRequest) -> None:
        asked.append(request.request_id)
        await service.respond_elicitation_request(
            chat_id=request.chat_id,
            request_id=request.request_id,
            answers={"q0": ("category",)},
        )

    async def expiry(chat_id: int, request_id: str) -> None:
        closed.append((chat_id, request_id))

    service.set_elicitation_request_handler(handler)
    service.set_elicitation_expiry_handler(expiry)
    response = await service._decide_elicitation("Answer Questions", _one_question_mode())
    assert response.action == "accept"
    assert closed == [(1, asked[0])]


async def test_stopping_the_chat_also_closes_the_question_it_was_asked(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)
    closed: list[tuple[int, str]] = []

    async def expiry(chat_id: int, request_id: str) -> None:
        closed.append((chat_id, request_id))

    service.set_elicitation_request_handler(_ignore)
    service.set_elicitation_expiry_handler(expiry)

    pending = asyncio.create_task(service._decide_elicitation("Answer Questions", _one_question_mode()))
    while not service._pending_elicitations:
        await asyncio.sleep(0)
    (request_id,) = service._pending_elicitations

    assert await service.stop(chat_id=1)
    response = await pending
    assert response.action == "cancel"
    assert closed == [(1, request_id)]


async def test_a_question_left_open_when_the_turn_ends_is_closed_at_once(tmp_path: Path) -> None:
    """A turn that ends underneath an open question must not keep its buttons alive for five minutes."""
    service = await _start_service(tmp_path)
    offered: list[ElicitationRequest] = []
    closed: list[tuple[int, str]] = []

    async def record(request: ElicitationRequest) -> None:
        offered.append(request)

    async def expiry(chat_id: int, request_id: str) -> None:
        closed.append((chat_id, request_id))

    service.set_elicitation_request_handler(record)
    service.set_elicitation_expiry_handler(expiry)

    pending = asyncio.create_task(service._decide_elicitation("Answer Questions", _one_question_mode()))
    while not service._pending_elicitations:
        await asyncio.sleep(0)

    await service.prompt(chat_id=1, text="báo cáo đi")
    response = await asyncio.wait_for(pending, timeout=1)

    assert response.action == "cancel"
    assert service._pending_elicitations == {}
    (request,) = offered
    assert closed == [(request.chat_id, request.request_id)]


async def test_the_answer_sent_a_second_before_the_turn_ended_is_kept(tmp_path: Path) -> None:
    service = await _start_service(tmp_path)
    service.set_elicitation_request_handler(_ignore)

    pending = asyncio.create_task(service._decide_elicitation("Answer Questions", _one_question_mode()))
    while not service._pending_elicitations:
        await asyncio.sleep(0)
    (request_id,) = service._pending_elicitations
    assert await service.respond_elicitation_request(chat_id=1, request_id=request_id, answers={"q0": ("month",)})

    await service.prompt(chat_id=1, text="báo cáo đi")
    response = await pending

    assert response.action == "accept"


async def test_a_question_is_closed_when_the_turn_ends_with_an_error(tmp_path: Path) -> None:
    """`/new` mid-question closes the keyboard only because a killed agent makes the prompt raise."""
    service = await _start_service(tmp_path, connection=GenericValueErrorConnection(session_id="elicit"))
    closed: list[tuple[int, str]] = []

    async def expiry(chat_id: int, request_id: str) -> None:
        closed.append((chat_id, request_id))

    service.set_elicitation_request_handler(_ignore)
    service.set_elicitation_expiry_handler(expiry)

    pending = asyncio.create_task(service._decide_elicitation("Answer Questions", _one_question_mode()))
    while not service._pending_elicitations:
        await asyncio.sleep(0)
    (request_id,) = service._pending_elicitations

    with pytest.raises(ValueError, match="unexpected"):
        await service.prompt(chat_id=1, text="báo cáo đi")
    response = await asyncio.wait_for(pending, timeout=1)

    assert response.action == "cancel"
    assert service._pending_elicitations == {}
    assert closed == [(1, request_id)]


async def _ignore(request: ElicitationRequest) -> None:
    del request


async def _never_prompt(request: ElicitationRequest) -> None:
    """Handler proving the bot cancelled before it tried to render anything."""
    pytest.fail(f"the agent asked {request.request_id} in a form the bot cannot render")
