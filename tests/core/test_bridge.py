from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest

from acp_chat_bot.acp.models import (
    ActivityMode,
    AgentActivityBlock,
    AgentCommand,
    AgentReply,
    ElicitationOption,
    ElicitationQuestion,
    ElicitationRequest,
    FilePayload,
    ImagePayload,
    PermissionRequest,
    ResumableSession,
)
from acp_chat_bot.core.bridge import (
    BUSY_NOTICE,
    HELP_TEXT,
    NO_SESSION_TEXT,
    AgentChatBridge,
    _parse_run_at,
)
from acp_chat_bot.core.channel import ChannelCapabilities
from acp_chat_bot.core.chat import ChatType, InboundAttachment, InboundMessage
from acp_chat_bot.core.config import ChatConfig
from acp_chat_bot.scheduled_tasks import ScheduledTaskDeferredError, ScheduledTaskExecutionError
from tests.core.support import FakeAgentService, FakeChannel, make_store

pytestmark = pytest.mark.asyncio

CHAT_ID = 100
OTHER_CHAT_ID = 200
WORKSPACE = Path("/workspace")
RUN_AT = datetime(2030, 1, 1, tzinfo=UTC)
EXPECTED_ATTACHMENTS = 2
EXPECTED_PROMPTS = 2


def make_bridge(  # noqa: PLR0913
    tmp_path: Path,
    *,
    capabilities: ChannelCapabilities | None = None,
    activity_mode: ActivityMode = "normal",
    group_require_mention: bool = True,
    group_history_size: int = 200,
    with_store: bool = False,
) -> tuple[AgentChatBridge, FakeAgentService, FakeChannel, list[str]]:
    service = FakeAgentService()
    channel = FakeChannel(capabilities=capabilities or FakeChannel().capabilities)
    stops: list[str] = []
    bridge = AgentChatBridge(
        channel=channel,
        agent_service=service,
        config=ChatConfig(
            default_workspace=WORKSPACE,
            activity_mode=activity_mode,
            group_require_mention=group_require_mention,
            group_history_size=group_history_size,
        ),
        scheduled_task_store=make_store(tmp_path) if with_store else None,
        stop_request=lambda: stops.append("stop"),
        agent_name="test",
    )
    bridge.install()
    return bridge, service, channel, stops


def message(  # noqa: PLR0913
    text: str,
    *,
    chat_id: int = CHAT_ID,
    chat_type: ChatType = "private",
    message_id: int = 1,
    attachments: tuple[InboundAttachment, ...] = (),
    mentioned_bot: bool = True,
) -> InboundMessage:
    return InboundMessage(
        chat_id=chat_id,
        chat_type=chat_type,
        message_id=message_id,
        user_id=7,
        user_name="Alice",
        text=text,
        attachments=attachments,
        mentioned_bot=mentioned_bot,
    )


async def test_prompt_starts_an_implicit_session_and_replies(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path)

    await bridge.handle_inbound(message("hello"))

    assert service.new_session_calls == [(CHAT_ID, WORKSPACE)]
    assert service.prompts == [(CHAT_ID, "hello", (), ())]
    assert channel.typing == [CHAT_ID]
    assert channel.sent[-1].text == "agent reply"


async def test_group_mention_gate_and_history(tmp_path: Path):
    bridge, service, _, _ = make_bridge(tmp_path)

    await bridge.handle_inbound(message("ignored", chat_type="group", mentioned_bot=False))
    await bridge.handle_inbound(message("mentioned", chat_type="group", mentioned_bot=True, message_id=2))

    assert [prompt[1] for prompt in service.prompts] == ["mentioned"]
    assert service.group_messages[CHAT_ID][0]["text"] == "mentioned"


async def test_group_history_can_be_disabled(tmp_path: Path):
    bridge, service, _, _ = make_bridge(tmp_path, group_history_size=0)

    await bridge.handle_inbound(message("hello", chat_type="group"))

    assert service.group_messages == {}


async def test_attachments_are_downloaded_and_forwarded(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path)

    await bridge.handle_inbound(
        message(
            "look",
            attachments=(
                InboundAttachment(ref="img-1", kind="image", mime_type="image/png"),
                InboundAttachment(ref="file-1", kind="file", mime_type="text/plain", name="notes.txt"),
            ),
        )
    )

    images = service.prompts[0][2]
    files = service.prompts[0][3]
    assert images[0].data_base64 == base64.b64encode(b"attachment-bytes").decode("ascii")
    assert images[0].mime_type == "image/png"
    assert files[0].name == "notes.txt"
    assert len(channel.downloads) == EXPECTED_ATTACHMENTS


async def test_attachments_are_ignored_when_the_channel_cannot_receive_them(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(
        tmp_path,
        capabilities=ChannelCapabilities(attachments=False, choices=True, editable_messages=True),
    )

    await bridge.handle_inbound(message("look", attachments=(InboundAttachment(ref="img-1", kind="image"),)))

    assert service.prompts[0][2] == ()
    assert channel.downloads == []


async def test_reply_images_and_files_are_sent(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path)
    service.reply = AgentReply(
        text="",
        images=(ImagePayload(data_base64="aGk=", mime_type="image/png"),),
        files=(FilePayload(name="a.txt", text_content="hello"),),
    )

    await bridge.handle_inbound(message("hello"))

    assert channel.images[0][0] == CHAT_ID
    assert channel.files[0][0] == CHAT_ID


async def test_busy_queue_runs_the_queued_prompt_after_the_current_one(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path)
    service.prompt_gate = asyncio.Event()

    first = asyncio.create_task(bridge.handle_inbound(message("first", message_id=1)))
    await service.prompt_started.wait()

    await bridge.handle_inbound(message("second", message_id=2))
    assert channel.sent[-1].text == BUSY_NOTICE

    service.prompt_gate.set()
    await first

    assert [prompt[1] for prompt in service.prompts] == ["first", "second"]


async def test_activity_normal_sends_each_block(tmp_path: Path):
    _, service, channel, _ = make_bridge(tmp_path)
    block = AgentActivityBlock(kind="think", title="Checking", status="completed")

    await service.emit_activity(CHAT_ID, block)

    assert channel.activities == [(CHAT_ID, block)]


async def test_activity_compact_creates_and_edits_a_status_message(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path, activity_mode="compact")
    first = AgentActivityBlock(kind="think", title="Checking", status="completed")
    second = AgentActivityBlock(kind="run", title="Running tests", status="completed", text="pytest")

    await service.emit_activity(CHAT_ID, first)
    await service.emit_activity(CHAT_ID, second)

    assert channel.sent[0].text.startswith("💡 Thinking: Checking")
    assert channel.updates[0][2].startswith("💡 Thinking: Checking")
    assert "⚙️ Running: Running tests" in channel.updates[0][2]

    service.reply = AgentReply(text="final")
    await service.emit_activity(CHAT_ID, second)
    await bridge.handle_inbound(message("hello"))
    assert channel.updates[-1][2] == "final"


async def test_activity_compact_falls_back_to_a_new_message_when_editing_fails(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path, activity_mode="compact")
    channel.edit_result = False

    await service.emit_activity(CHAT_ID, AgentActivityBlock(kind="think", title="Checking", status="completed"))
    service.reply = AgentReply(text="final")
    await bridge.handle_inbound(message("hello"))

    assert channel.sent[-1].text == "final"


async def test_activity_modes_degrade_when_the_channel_cannot_edit(tmp_path: Path):
    _, service, channel, _ = make_bridge(
        tmp_path,
        activity_mode="verbose",
        capabilities=ChannelCapabilities(editable_messages=False),
    )
    block = AgentActivityBlock(kind="think", title="Checking", status="completed")

    await service.emit_activity(CHAT_ID, block)

    assert channel.activities == [(CHAT_ID, block)]


async def test_permission_choice_resolves_by_number_and_callback(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path)
    request = PermissionRequest(
        chat_id=CHAT_ID,
        request_id="perm-1",
        tool_title="Run ls",
        tool_call_id="tool-1",
        available_actions=("once", "always", "deny"),
    )

    await service.emit_permission(request)
    assert channel.choices[-1][1].kind == "permission"

    await bridge.handle_inbound(message("1", message_id=2))
    assert service.permission_responses == [(CHAT_ID, "perm-1", "once")]

    await service.emit_permission(request)
    assert await bridge.resolve_choice(chat_id=CHAT_ID, request_id="wrong", values=("deny",)) is False
    assert await bridge.resolve_choice(chat_id=CHAT_ID, request_id="perm-1", values=("deny",)) is True
    assert service.permission_responses[-1] == (CHAT_ID, "perm-1", "deny")


async def test_permission_choice_ignores_unrelated_text(tmp_path: Path):
    bridge, service, _, _ = make_bridge(tmp_path)
    request = PermissionRequest(
        chat_id=CHAT_ID,
        request_id="perm-1",
        tool_title="Run ls",
        tool_call_id="tool-1",
        available_actions=("once", "deny"),
    )

    await service.emit_permission(request)
    await bridge.handle_inbound(message("hello", message_id=2))

    assert service.permission_responses == []
    assert service.prompts[0][1] == "hello"


async def test_elicitation_choices_combine_answers_and_support_skip(tmp_path: Path):
    bridge, service, _, _ = make_bridge(tmp_path)
    request = ElicitationRequest(
        chat_id=CHAT_ID,
        request_id="ask-1",
        title="Pick colors",
        questions=(
            ElicitationQuestion(
                field="color",
                prompt="Color",
                options=(
                    ElicitationOption(value="red", label="Red"),
                    ElicitationOption(value="blue", label="Blue"),
                ),
            ),
        ),
    )

    await service.emit_elicitation(request)
    await bridge.handle_inbound(message("1,2", message_id=2))
    assert service.elicitation_responses[-1] == (CHAT_ID, "ask-1", {"color": ("red", "blue")})

    await service.emit_elicitation(request)
    await bridge.handle_inbound(message("skip", message_id=3))
    assert service.elicitation_responses[-1] == (CHAT_ID, "ask-1", None)


async def test_elicitation_without_options_is_skipped(tmp_path: Path):
    _, service, channel, _ = make_bridge(tmp_path)
    request = ElicitationRequest(
        chat_id=CHAT_ID,
        request_id="ask-1",
        title="Type something",
        questions=(ElicitationQuestion(field="name", prompt="Name", options=()),),
    )

    await service.emit_elicitation(request)

    assert service.elicitation_responses == [(CHAT_ID, "ask-1", None)]
    assert "free-form" in channel.sent[-1].text


async def test_elicitation_expiry_drops_the_pending_choice(tmp_path: Path):
    bridge, service, _, _ = make_bridge(tmp_path)
    request = ElicitationRequest(
        chat_id=CHAT_ID,
        request_id="ask-1",
        title="Pick",
        questions=(
            ElicitationQuestion(
                field="color",
                prompt="Color",
                options=(ElicitationOption(value="red", label="Red"),),
            ),
        ),
    )

    await service.emit_elicitation(request)
    await service.emit_expiry(CHAT_ID, "ask-1")

    assert await bridge.resolve_choice(chat_id=CHAT_ID, request_id="ask-1", values=("x",)) is False


async def test_help_and_unknown_commands(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path)

    await bridge.handle_inbound(message("/help"))
    assert channel.sent[-1].text == HELP_TEXT

    await bridge.handle_inbound(message("/agent-command now", message_id=2))
    assert service.prompts[-1][1] == "/agent-command now"


async def test_new_session_command_and_errors(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path)

    await bridge.handle_inbound(message("/new /tmp/other"))
    assert service.new_session_calls[-1] == (CHAT_ID, Path("/tmp/other"))
    assert "Started session" in channel.sent[-1].text

    service.new_session_error = RuntimeError("boom")
    await bridge.handle_inbound(message("/new", message_id=2))
    assert "Could not start a session: boom" in channel.sent[-1].text


async def test_session_command_reports_active_and_missing_sessions(tmp_path: Path):
    bridge, _, channel, _ = make_bridge(tmp_path)

    await bridge.handle_inbound(message("/session"))
    assert channel.sent[-1].text == NO_SESSION_TEXT

    await bridge.handle_inbound(message("hello", message_id=2))
    await bridge.handle_inbound(message("/session", message_id=3))
    assert "session-1" in channel.sent[-1].text
    assert str(WORKSPACE) in channel.sent[-1].text


async def test_cancel_stop_clear_commands(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path)

    for command in ("/cancel", "/stop", "/clear"):
        service.cancel_result = service.stop_result = service.clear_result = True
        await bridge.handle_inbound(message(command))
        assert channel.sent[-1].text in {"Cancelled.", "Stopped.", "Cleared."}

    service.cancel_result = service.stop_result = service.clear_result = False
    for command in ("/cancel", "/stop", "/clear"):
        await bridge.handle_inbound(message(command, message_id=2))
    assert channel.sent[-3].text == "Nothing to cancel."
    assert channel.sent[-2].text == NO_SESSION_TEXT
    assert channel.sent[-1].text == NO_SESSION_TEXT


async def test_restart_command_sets_the_flag_and_stops(tmp_path: Path):
    bridge, _, channel, stops = make_bridge(tmp_path)

    await bridge.handle_inbound(message("/restart"))

    assert bridge.restart_requested is True
    assert stops == ["stop"]
    assert channel.sent[-1].text == "Restarting the bot."


async def test_mode_command(tmp_path: Path):
    bridge, _, channel, _ = make_bridge(tmp_path)

    await bridge.handle_inbound(message("/mode"))
    assert "Current activity mode: `normal`" in channel.sent[-1].text

    await bridge.handle_inbound(message("/mode quiet", message_id=2))
    assert "Usage:" in channel.sent[-1].text

    await bridge.handle_inbound(message("/mode compact", message_id=3))
    assert channel.sent[-1].text == "Activity mode set to `compact`."


async def test_mode_warns_when_the_channel_cannot_edit(tmp_path: Path):
    bridge, _, channel, _ = make_bridge(
        tmp_path,
        capabilities=ChannelCapabilities(editable_messages=False),
    )

    await bridge.handle_inbound(message("/mode verbose"))

    assert "cannot edit messages" in channel.sent[-1].text


async def test_resume_command_handles_every_supported_state(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path)
    service.supports_loading = False
    await bridge.handle_inbound(message("/resume"))
    assert "does not support resuming" in channel.sent[-1].text

    service.supports_loading = True
    service.resume_candidates = None
    await bridge.handle_inbound(message("/resume", message_id=2))
    assert "does not support listing" in channel.sent[-1].text

    service.resume_candidates = ()
    await bridge.handle_inbound(message("/resume", message_id=3))
    assert "No previous sessions" in channel.sent[-1].text

    single = (ResumableSession("s-1", Path("/a"), "One", "2026-01-01"),)
    service.resume_candidates = single
    await bridge.handle_inbound(message("/resume", message_id=4))
    assert service.load_session_calls[-1] == (CHAT_ID, "s-1", Path("/a"))

    service.resume_candidates = (
        ResumableSession("s-1", Path("/a"), "One", "2026-01-01"),
        ResumableSession("s-2", Path("/b"), "Two", "2026-01-02"),
    )
    await bridge.handle_inbound(message("/resume", message_id=5))
    await bridge.handle_inbound(message("2", message_id=6))
    assert service.load_session_calls[-1] == (CHAT_ID, "s-2", Path("/b"))

    service.load_session_error = RuntimeError("load boom")
    service.resume_candidates = single
    await bridge.handle_inbound(message("/resume", message_id=7))
    assert "Could not load session: load boom" in channel.sent[-1].text


async def test_cmds_command_lists_and_runs_advertised_commands(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path)

    service.commands = ()
    await bridge.handle_inbound(message("/cmds"))
    assert "did not advertise" in channel.sent[-1].text

    service.commands = (AgentCommand(name="goal", description="Manage goal"),)
    await bridge.handle_inbound(message("/cmds", message_id=2))
    assert channel.choices[-1][1].kind == "commands"
    await bridge.handle_inbound(message("1", message_id=3))
    assert service.prompts[-1][1] == "/goal"


async def test_schedule_command_creates_prompt_and_notify_tasks(tmp_path: Path):
    bridge, _, channel, _ = make_bridge(tmp_path, with_store=True)
    store = bridge._store
    assert store is not None

    await bridge.handle_inbound(message(f"/schedule notify {RUN_AT.isoformat()} ping"))
    tasks = store.list_tasks_for_chat(chat_id=CHAT_ID)
    assert tasks[-1].mode == "notify"
    assert tasks[-1].notify_text == "ping"

    await bridge.handle_inbound(message(f"/schedule {RUN_AT.isoformat()} do work", message_id=2))
    tasks = store.list_tasks_for_chat(chat_id=CHAT_ID)
    assert tasks[-1].mode == "prompt_agent"
    assert tasks[-1].prompt_text == "do work"

    await bridge.handle_inbound(message('/schedule cron "0 9 * * *" daily', message_id=3))
    tasks = store.list_tasks_for_chat(chat_id=CHAT_ID)
    assert next(task for task in tasks if task.cron_expression is not None).cron_expression == "0 9 * * *"
    assert "repeats" in channel.sent[-1].text


async def test_schedule_command_handles_bad_inputs(tmp_path: Path, mocker):
    bridge, _, channel, _ = make_bridge(tmp_path, with_store=True)

    await bridge.handle_inbound(message("/schedule"))
    assert "Usage:" in channel.sent[-1].text

    await bridge.handle_inbound(message('/schedule cron "bad" prompt', message_id=2))
    assert "Invalid schedule" in channel.sent[-1].text

    await bridge.handle_inbound(message("/schedule xyzzy prompt", message_id=3))
    assert "could not understand" in channel.sent[-1].text

    store = bridge._store
    assert store is not None
    mocker.patch.object(store, "create_task", side_effect=RuntimeError("db boom"))
    await bridge.handle_inbound(message(f"/schedule {RUN_AT.isoformat()} prompt", message_id=4))
    assert "Could not schedule that: db boom" in channel.sent[-1].text


async def test_schedule_command_without_a_store(tmp_path: Path):
    bridge, _, channel, _ = make_bridge(tmp_path)

    await bridge.handle_inbound(message("/schedule now hello"))

    assert "Scheduling is not configured." in channel.sent[-1].text


async def test_scheduled_and_unschedule_commands(tmp_path: Path):
    bridge, _, channel, _ = make_bridge(tmp_path, with_store=True)
    store = bridge._store
    assert store is not None

    await bridge.handle_inbound(message("/scheduled"))
    assert channel.sent[-1].text == "No scheduled tasks."

    task = store.create_task(
        chat_id=CHAT_ID,
        mode="notify",
        notify_text="ping",
        run_at=RUN_AT,
    )
    await bridge.handle_inbound(message("/scheduled", message_id=2))
    assert task.id in channel.sent[-1].text

    await bridge.handle_inbound(message(f"/unschedule {task.id}", message_id=3))
    assert channel.sent[-1].text == "Cancelled."

    await bridge.handle_inbound(message("/unschedule missing", message_id=4))
    assert channel.sent[-1].text == "Task not found or not pending."


async def test_scheduled_and_unschedule_require_a_store(tmp_path: Path):
    bridge, _, channel, _ = make_bridge(tmp_path)

    await bridge.handle_inbound(message("/scheduled"))
    assert "Scheduling is not configured." in channel.sent[-1].text

    await bridge.handle_inbound(message("/unschedule abc", message_id=2))
    assert "Scheduling is not configured." in channel.sent[-1].text


async def test_unschedule_requires_an_id(tmp_path: Path):
    bridge, _, channel, _ = make_bridge(tmp_path, with_store=True)

    await bridge.handle_inbound(message("/unschedule"))

    assert "Usage:" in channel.sent[-1].text


async def test_execute_notify_task(tmp_path: Path):
    bridge, _, channel, _ = make_bridge(tmp_path, with_store=True)
    store = bridge._store
    assert store is not None
    task = store.create_task(chat_id=CHAT_ID, mode="notify", notify_text="ping", run_at=RUN_AT)

    await bridge.execute_scheduled_task(task)

    assert channel.sent[-1].text == "ping"


async def test_execute_prompt_task_and_errors(tmp_path: Path):
    bridge, service, _, _ = make_bridge(tmp_path, with_store=True)
    store = bridge._store
    assert store is not None
    task = store.create_task(
        chat_id=CHAT_ID,
        mode="prompt_agent",
        prompt_text="do work",
        run_at=RUN_AT,
    )

    await bridge.execute_scheduled_task(task)
    assert service.prompts[-1][1] == "do work"

    empty = store.create_task(chat_id=CHAT_ID, mode="prompt_agent", prompt_text="", run_at=RUN_AT)
    with pytest.raises(ScheduledTaskExecutionError):
        await bridge.execute_scheduled_task(empty)

    service.reply = None
    no_reply = store.create_task(chat_id=CHAT_ID, mode="prompt_agent", prompt_text="x", run_at=RUN_AT)
    with pytest.raises(ScheduledTaskExecutionError):
        await bridge.execute_scheduled_task(no_reply)


async def test_execute_prompt_task_defers_when_the_chat_is_busy(tmp_path: Path):
    bridge, _, _, _ = make_bridge(tmp_path, with_store=True)
    store = bridge._store
    assert store is not None
    task = store.create_task(chat_id=CHAT_ID, mode="prompt_agent", prompt_text="x", run_at=RUN_AT)

    async with bridge._lock_for(CHAT_ID):
        with pytest.raises(ScheduledTaskDeferredError):
            await bridge.execute_scheduled_task(task)


async def test_prompt_failure_is_reported(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path)
    service.prompt_error = RuntimeError("boom")

    await bridge.handle_inbound(message("hello"))

    assert "could not process" in channel.sent[-1].text


async def test_choice_skip_and_numbers_out_of_range(tmp_path: Path):
    bridge, service, _, _ = make_bridge(tmp_path)
    request = ElicitationRequest(
        chat_id=CHAT_ID,
        request_id="ask-1",
        title="Pick",
        questions=(
            ElicitationQuestion(
                field="color",
                prompt="Color",
                options=(ElicitationOption(value="red", label="Red"),),
            ),
        ),
    )

    await service.emit_elicitation(request)
    await bridge.handle_inbound(message("9", message_id=2))
    assert service.prompts[-1][1] == "9"

    await service.emit_elicitation(request)
    await bridge.handle_inbound(message("skip", message_id=3))
    assert service.elicitation_responses[-1][2] is None


async def test_choice_with_an_option_label(tmp_path: Path):
    bridge, service, _, _ = make_bridge(tmp_path)
    request = PermissionRequest(
        chat_id=CHAT_ID,
        request_id="perm-1",
        tool_title="Run ls",
        tool_call_id="tool-1",
        available_actions=("once", "deny"),
    )

    await service.emit_permission(request)
    await bridge.handle_inbound(message("Deny", message_id=2))

    assert service.permission_responses[-1][2] == "deny"


async def test_channel_property_and_empty_messages(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path)

    assert bridge.channel is channel
    assert bridge.agent_name == "test"
    await bridge.handle_inbound(message(""))
    assert service.prompts == []


async def test_prompt_without_a_session_reply(tmp_path: Path):
    bridge, service, channel, _ = make_bridge(tmp_path)
    service.reply = None

    await bridge.handle_inbound(message("hello"))

    assert channel.sent[-1].text == NO_SESSION_TEXT


async def test_permission_and_elicitation_resolvers_ignore_empty_values(tmp_path: Path):
    bridge, service, _, _ = make_bridge(tmp_path)
    permission = PermissionRequest(
        chat_id=CHAT_ID,
        request_id="perm-1",
        tool_title="Run ls",
        tool_call_id="tool-1",
        available_actions=("once",),
    )
    await service.emit_permission(permission)
    assert await bridge.resolve_choice(chat_id=CHAT_ID, request_id="perm-1", values=()) is True

    elicitation = ElicitationRequest(
        chat_id=CHAT_ID,
        request_id="ask-1",
        title="Pick",
        questions=(
            ElicitationQuestion(
                field="color",
                prompt="Color",
                options=(ElicitationOption(value="red", label="Red"),),
            ),
        ),
    )
    await service.emit_elicitation(elicitation)
    assert await bridge.resolve_choice(chat_id=CHAT_ID, request_id="ask-1", values=()) is True


async def test_command_parser_handles_bad_quotes_and_empty_tokens(tmp_path: Path):
    bridge, service, _, _ = make_bridge(tmp_path)

    await bridge.handle_inbound(message('/new "unterminated'))
    assert service.new_session_calls

    await bridge._handle_command(message(""), "")


async def test_resume_and_cmds_resolvers_ignore_empty_values(tmp_path: Path):
    bridge, service, _, _ = make_bridge(tmp_path)
    service.resume_candidates = (
        ResumableSession("s-1", Path("/a"), "One", "2026-01-01"),
        ResumableSession("s-2", Path("/b"), "Two", "2026-01-02"),
    )
    await bridge.handle_inbound(message("/resume"))
    request_id = bridge._pending_choices[CHAT_ID].request.request_id
    assert await bridge.resolve_choice(chat_id=CHAT_ID, request_id=request_id, values=()) is True

    service.commands = (AgentCommand(name="goal", description="Manage goal"),)
    await bridge.handle_inbound(message("/cmds", message_id=2))
    request_id = bridge._pending_choices[CHAT_ID].request.request_id
    assert await bridge.resolve_choice(chat_id=CHAT_ID, request_id=request_id, values=()) is True


async def test_schedule_rejects_a_missing_body_and_short_cron(tmp_path: Path):
    bridge, _, channel, _ = make_bridge(tmp_path, with_store=True)

    await bridge.handle_inbound(message("/schedule notify"))
    assert "Usage:" in channel.sent[-1].text

    await bridge.handle_inbound(message('/schedule cron "0 9 * * *"', message_id=2))
    assert "Usage:" in channel.sent[-1].text


async def test_match_choice_rejects_whitespace_and_punctuation(tmp_path: Path):
    bridge, service, _, _ = make_bridge(tmp_path)
    request = ElicitationRequest(
        chat_id=CHAT_ID,
        request_id="ask-1",
        title="Pick",
        questions=(
            ElicitationQuestion(
                field="color",
                prompt="Color",
                options=(ElicitationOption(value="red", label="Red"),),
            ),
        ),
    )

    await service.emit_elicitation(request)
    await bridge.handle_inbound(message("   ", message_id=2))
    await bridge.handle_inbound(message(",", message_id=3))
    await bridge.handle_inbound(message("nope", message_id=4))

    assert service.elicitation_responses == []
    assert len(service.prompts) == EXPECTED_PROMPTS


async def test_elicitation_and_permission_resolvers_skip_malformed_values(tmp_path: Path):
    bridge, service, _, _ = make_bridge(tmp_path)
    elicitation = ElicitationRequest(
        chat_id=CHAT_ID,
        request_id="ask-1",
        title="Pick",
        questions=(
            ElicitationQuestion(
                field="color",
                prompt="Color",
                options=(ElicitationOption(value="red", label="Red"),),
            ),
        ),
    )
    await service.emit_elicitation(elicitation)
    await bridge.resolve_choice(chat_id=CHAT_ID, request_id="ask-1", values=("malformed",))
    assert service.elicitation_responses[-1][2] == {}

    permission = PermissionRequest(
        chat_id=CHAT_ID,
        request_id="perm-1",
        tool_title="Run ls",
        tool_call_id="tool-1",
        available_actions=("once",),
    )
    await service.emit_permission(permission)
    await bridge.resolve_choice(chat_id=CHAT_ID, request_id="perm-1", values=("bogus",))
    assert service.permission_responses[-1][2] == "deny"


async def test_parse_run_at_edge_cases(mocker):
    assert _parse_run_at(["2030-01-01T00:00:00Z", ""], ("en",)) is None
    assert _parse_run_at(["2000-01-01T00:00:00Z", "x"], ("en",)) is None

    mocker.patch("acp_chat_bot.core.bridge.dateparser.parse", return_value=datetime(2030, 1, 1))
    parsed = _parse_run_at(["tomorrow", "x"], ("en",))
    assert parsed is not None
    assert parsed[0].tzinfo is not None
