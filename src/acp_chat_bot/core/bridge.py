"""Platform-neutral orchestration for one agent chat channel."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import dateparser

from acp_chat_bot.acp.models import (
    ActivityMode,
    AgentActivityBlock,
    AgentReply,
    ElicitationRequest,
    PermissionDecisionAction,
    PermissionRequest,
    PromptFile,
    PromptImage,
)
from acp_chat_bot.core.agent import ChatAgentService
from acp_chat_bot.core.channel import Channel
from acp_chat_bot.core.chat import (
    ChoiceOption,
    ChoiceRequest,
    InboundMessage,
)
from acp_chat_bot.core.config import ChatConfig
from acp_chat_bot.core.ids import ChatId, MessageId
from acp_chat_bot.logging_context import bind_log_context
from acp_chat_bot.scheduled_tasks import (
    ScheduledTask,
    ScheduledTaskDeferredError,
    ScheduledTaskExecutionError,
    ScheduledTaskStore,
)
from acp_chat_bot.scheduled_tasks.recurrence import next_occurrence, normalize_cron_expression
from acp_chat_bot.scheduled_tasks.store import utc_now

logger = logging.getLogger(__name__)

SKIP_CHOICE_VALUE = "__skip__"
ACTIVITY_MODES: tuple[ActivityMode, ...] = ("normal", "compact", "verbose")
ACTIVITY_LABELS = {
    "think": "💡 Thinking",
    "run": "⚙️ Running",
    "read": "📖 Reading",
    "edit": "✏️ Editing",
    "write": "✍️ Writing",
    "search": "🌐 Searching web",
    "query": "🔎 Querying",
}
HELP_TEXT = (
    "**ACP chat bot**\n"
    "`/new [workspace]` start a session\n"
    "`/session` show the active session\n"
    "`/resume` load a previous session\n"
    "`/cancel` cancel the running prompt\n"
    "`/stop` stop the active session\n"
    "`/clear` clear the active session\n"
    "`/mode [normal|compact|verbose]` switch activity display\n"
    "`/cmds` list the agent's slash commands\n"
    "`/schedule <when> <prompt>` create a durable reminder\n"
    "`/scheduled` list scheduled reminders\n"
    "`/unschedule <id>` cancel a reminder\n"
    "`/restart` restart the bot process"
)
BUSY_NOTICE = "The agent is busy. Your message is queued and will run when the current prompt finishes."
NO_SESSION_TEXT = "No active session. Send a prompt or use `/new` to start one."
UNKNOWN_CHOICE_TEXT = "Please answer with one of the numbered options."
CRON_MIN_ARGS = 3


@dataclass(slots=True)
class _PendingChoice:
    request: ChoiceRequest
    resolve: Callable[[tuple[str, ...] | None], Awaitable[None]]


@dataclass(slots=True)
class _ActivityState:
    text: str = ""
    message_id: MessageId | None = None
    edited: bool = False


class AgentChatBridge:
    """Drive one ACP agent from any channel that implements {py:class}`Channel`."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        channel: Channel,
        agent_service: ChatAgentService,
        config: ChatConfig,
        scheduled_task_store: ScheduledTaskStore | None = None,
        stop_request: Callable[[], None] | None = None,
        agent_name: str = "default",
    ) -> None:
        self._channel = channel
        self._service = agent_service
        self._config = config
        self._store = scheduled_task_store
        self._stop_request = stop_request
        self._agent_name = agent_name
        self._locks: dict[ChatId, asyncio.Lock] = {}
        self._busy: dict[ChatId, InboundMessage] = {}
        self._pending_choices: dict[ChatId, _PendingChoice] = {}
        self._activity_modes: dict[ChatId, ActivityMode] = {}
        self._activity: dict[ChatId, _ActivityState] = {}
        self._restart_requested = False

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested

    @property
    def channel(self) -> Channel:
        return self._channel

    @property
    def agent_name(self) -> str:
        return self._agent_name

    def install(self) -> None:
        """Register this bridge on the agent service's callbacks."""
        self._service.set_permission_request_handler(self.on_permission_request)
        self._service.set_activity_event_handler(self.on_activity)
        self._service.set_elicitation_request_handler(self.on_elicitation_request)
        self._service.set_elicitation_expiry_handler(self.on_elicitation_expired)

    async def handle_inbound(self, message: InboundMessage) -> None:
        """Route one authorized inbound message."""
        if message.chat_type == "group" and self._config.group_require_mention and not message.mentioned_bot:
            return
        await self._record_group_message(message)
        if await self._resolve_pending_choice(message):
            return
        text = message.text.strip()
        if text.startswith("/"):
            await self._handle_command(message, text)
            return
        if not text and not message.attachments:
            return
        await self._submit(message)

    async def resolve_choice(self, *, chat_id: ChatId, request_id: str, values: tuple[str, ...]) -> bool:
        """Resolve a choice from a channel callback (for example a card button)."""
        pending = self._pending_choices.get(chat_id)
        if pending is None or pending.request.request_id != request_id:
            return False
        self._pending_choices.pop(chat_id, None)
        await pending.resolve(values)
        return True

    # ------------------------------------------------------------------
    # Prompts and sessions
    # ------------------------------------------------------------------

    async def _submit(self, message: InboundMessage) -> None:
        lock = self._lock_for(message.chat_id)
        if lock.locked():
            await self._queue_busy(message)
            return
        async with lock:
            try:
                await self._ensure_session(message.chat_id)
                await self._run_prompt(message)
            except Exception:
                logger.exception("Prompt failed for chat_id=%s", message.chat_id)
                await self._send(message.chat_id, "The agent could not process that message.")
        await self._drain_busy(message.chat_id)

    async def _run_prompt(self, message: InboundMessage) -> None:
        chat_id = message.chat_id
        with bind_log_context(agent=self._agent_name, chat_id=chat_id):
            await self._channel.set_typing(chat_id)
            images, files = await self._download_prompt_attachments(message)
            reply = await self._service.prompt(
                chat_id=chat_id,
                text=message.text,
                images=images,
                files=files,
            )
            if reply is None:
                await self._send(chat_id, NO_SESSION_TEXT)
                return
            await self._deliver_reply(chat_id, reply, reply_to=message.message_id)
            await self._reset_activity(chat_id)

    async def _ensure_session(self, chat_id: ChatId) -> None:
        if self._service.get_workspace(chat_id=chat_id) is not None:
            return
        await self._service.new_session(chat_id=chat_id, workspace=self._config.default_workspace)

    async def _download_prompt_attachments(
        self, message: InboundMessage
    ) -> tuple[tuple[PromptImage, ...], tuple[PromptFile, ...]]:
        if not message.attachments:
            return (), ()
        if not self._channel.capabilities.attachments:
            return (), ()
        images: list[PromptImage] = []
        files: list[PromptFile] = []
        for attachment in message.attachments:
            downloaded = await self._channel.download(attachment)
            encoded = base64.b64encode(downloaded.data).decode("ascii")
            mime_type = downloaded.mime_type or attachment.mime_type
            if attachment.kind == "image":
                images.append(PromptImage(data_base64=encoded, mime_type=mime_type or "image/jpeg"))
            else:
                files.append(
                    PromptFile(
                        name=downloaded.name or attachment.name or "attachment",
                        mime_type=mime_type,
                        data_base64=encoded,
                    )
                )
        return tuple(images), tuple(files)

    async def _deliver_reply(self, chat_id: ChatId, reply: AgentReply, *, reply_to: MessageId | None = None) -> None:
        text = reply.text.strip()
        if text:
            activity = self._activity.get(chat_id)
            delivered = False
            if (
                activity is not None
                and activity.message_id is not None
                and self._activity_mode(chat_id) == "compact"
                and self._channel.capabilities.editable_messages
            ):
                delivered = await self._channel.edit_markdown(chat_id, activity.message_id, text)
            if not delivered:
                await self._channel.send_markdown(chat_id, text, reply_to=reply_to)
        for image in reply.images:
            await self._channel.send_image(chat_id, image)
        for payload in reply.files:
            await self._channel.send_file(chat_id, payload)

    async def _queue_busy(self, message: InboundMessage) -> None:
        self._busy[message.chat_id] = message
        await self._send(message.chat_id, BUSY_NOTICE)

    async def _drain_busy(self, chat_id: ChatId) -> None:
        queued = self._busy.pop(chat_id, None)
        if queued is not None:
            await self._submit(queued)

    async def _send(self, chat_id: ChatId, text: str, *, reply_to: MessageId | None = None) -> None:
        await self._channel.send_markdown(chat_id, text, reply_to=reply_to)

    # ------------------------------------------------------------------
    # Activity display
    # ------------------------------------------------------------------

    def _activity_mode(self, chat_id: ChatId) -> ActivityMode:
        return self._activity_modes.get(chat_id, self._config.activity_mode)

    async def on_activity(self, chat_id: ChatId, block: AgentActivityBlock) -> None:
        # Reply previews are streaming updates of the final answer. A channel that cannot
        # edit messages would turn each preview into a separate, increasingly complete
        # message, so only the final answer is delivered there.
        if block.kind == "reply" and not self._channel.capabilities.editable_messages:
            return
        mode = self._activity_mode(chat_id)
        state = self._activity.setdefault(chat_id, _ActivityState())
        line = _activity_line(block)
        state.text = f"{state.text}\n{line}".strip() if state.text else line
        if mode == "normal" or not self._channel.capabilities.editable_messages:
            await self._channel.send_activity(chat_id, block)
            return
        if state.message_id is None:
            state.message_id = await self._channel.send_markdown(chat_id, state.text)
        else:
            state.edited = await self._channel.edit_markdown(chat_id, state.message_id, state.text)

    async def _reset_activity(self, chat_id: ChatId) -> None:
        self._activity.pop(chat_id, None)

    # ------------------------------------------------------------------
    # Choices: permissions, elicitations and internal menus
    # ------------------------------------------------------------------

    async def on_permission_request(self, request: PermissionRequest) -> None:
        labels = {"once": "Allow once", "always": "Always allow", "deny": "Deny"}
        options = tuple(ChoiceOption(value=action, label=labels[action]) for action in request.available_actions)

        async def resolve(values: tuple[str, ...] | None) -> None:
            if not values:
                return
            await self._service.respond_permission_request(
                chat_id=request.chat_id,
                request_id=request.request_id,
                action=_permission_action(values[0]),
            )

        choice = ChoiceRequest(
            request_id=request.request_id,
            kind="permission",
            title="Permission required",
            body=request.tool_title,
            options=options,
        )
        await self._ask(request.chat_id, choice, resolve)

    async def on_elicitation_request(self, request: ElicitationRequest) -> None:
        options = [
            ChoiceOption(
                value=f"{question.field}\x1f{option.value}",
                label=f"{question.prompt}: {option.label}",
            )
            for question in request.questions
            for option in question.options
        ]
        if not options:
            await self._service.respond_elicitation_request(
                chat_id=request.chat_id,
                request_id=request.request_id,
                answers=None,
            )
            await self._send(request.chat_id, "The agent asked a free-form question; skipped.")
            return

        async def resolve(values: tuple[str, ...] | None) -> None:
            answers = _elicitation_answers(values)
            await self._service.respond_elicitation_request(
                chat_id=request.chat_id,
                request_id=request.request_id,
                answers=answers,
            )

        choice = ChoiceRequest(
            request_id=request.request_id,
            kind="elicitation",
            title=request.title,
            body="Choose the answers for the agent.",
            options=tuple(options),
            multiple=True,
            skippable=True,
        )
        await self._ask(request.chat_id, choice, resolve)

    async def on_elicitation_expired(self, chat_id: ChatId, request_id: str) -> None:
        pending = self._pending_choices.get(chat_id)
        if pending is not None and pending.request.request_id == request_id:
            self._pending_choices.pop(chat_id, None)

    async def _ask(
        self,
        chat_id: ChatId,
        request: ChoiceRequest,
        resolve: Callable[[tuple[str, ...] | None], Awaitable[None]],
    ) -> None:
        self._pending_choices[chat_id] = _PendingChoice(request=request, resolve=resolve)
        await self._channel.present_choice(chat_id, request)

    async def _resolve_pending_choice(self, message: InboundMessage) -> bool:
        pending = self._pending_choices.get(message.chat_id)
        if pending is None:
            return False
        values = _match_choice(message.text, pending.request)
        if values is None:
            return False
        self._pending_choices.pop(message.chat_id, None)
        await pending.resolve(values)
        await self._send(message.chat_id, "Selection recorded.")
        return True

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _handle_command(self, message: InboundMessage, text: str) -> None:  # noqa: C901, PLR0912
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        if not tokens:
            return
        command = tokens[0].split("@", maxsplit=1)[0].lower()
        args = tokens[1:]
        chat_id = message.chat_id
        match command:
            case "/start" | "/help":
                await self._send(chat_id, HELP_TEXT)
            case "/new":
                await self._command_new(chat_id, args)
            case "/session":
                await self._command_session(chat_id)
            case "/cancel":
                cancelled = await self._service.cancel(chat_id=chat_id)
                await self._send(chat_id, "Cancelled." if cancelled else "Nothing to cancel.")
            case "/stop":
                await self._send(chat_id, "Stopped." if await self._service.stop(chat_id=chat_id) else NO_SESSION_TEXT)
            case "/clear":
                await self._send(chat_id, "Cleared." if await self._service.clear(chat_id=chat_id) else NO_SESSION_TEXT)
            case "/restart":
                self._restart_requested = True
                await self._send(chat_id, "Restarting the bot.")
                if self._stop_request is not None:
                    self._stop_request()
            case "/mode":
                await self._command_mode(chat_id, args)
            case "/resume":
                await self._command_resume(chat_id)
            case "/cmds":
                await self._command_cmds(chat_id)
            case "/schedule":
                await self._command_schedule(message, args)
            case "/scheduled":
                await self._command_scheduled(chat_id)
            case "/unschedule":
                await self._command_unschedule(chat_id, args)
            case _:
                await self._submit(
                    InboundMessage(
                        chat_id=chat_id,
                        chat_type=message.chat_type,
                        message_id=message.message_id,
                        user_id=message.user_id,
                        user_name=message.user_name,
                        text=text,
                        mentioned_bot=message.mentioned_bot,
                    )
                )

    async def _command_new(self, chat_id: ChatId, args: list[str]) -> None:
        workspace = Path(args[0]).expanduser() if args else self._config.default_workspace  # noqa: ASYNC240
        try:
            session_id = await self._service.new_session(chat_id=chat_id, workspace=workspace)
        except Exception as exc:  # noqa: BLE001
            await self._send(chat_id, f"Could not start a session: {exc}")
            return
        await self._send(chat_id, f"Started session `{session_id}` in `{workspace}`.")

    async def _command_session(self, chat_id: ChatId) -> None:
        context = self._service.get_active_session_context(chat_id=chat_id)
        if context is None:
            await self._send(chat_id, NO_SESSION_TEXT)
            return
        session_id, workspace = context
        await self._send(chat_id, f"Session `{session_id}`\nWorkspace `{workspace}`")

    async def _command_mode(self, chat_id: ChatId, args: list[str]) -> None:
        if not args:
            await self._send(chat_id, f"Current activity mode: `{self._activity_mode(chat_id)}`")
            return
        mode = args[0].lower()
        if mode not in ACTIVITY_MODES:
            await self._send(chat_id, "Usage: `/mode normal|compact|verbose`")
            return
        self._activity_modes[chat_id] = mode
        if mode != "normal" and not self._channel.capabilities.editable_messages:
            await self._send(chat_id, "This channel cannot edit messages; activity will stay separate.")
        else:
            await self._send(chat_id, f"Activity mode set to `{mode}`.")

    async def _command_resume(self, chat_id: ChatId) -> None:
        supported = self._service.supports_session_loading(chat_id=chat_id)
        if supported is False:
            await self._send(chat_id, "This agent does not support resuming sessions.")
            return
        candidates = await self._service.list_resumable_sessions(chat_id=chat_id)
        if candidates is None:
            await self._send(chat_id, "This agent does not support listing sessions.")
            return
        if not candidates:
            await self._send(chat_id, "No previous sessions found.")
            return
        if len(candidates) == 1:
            await self._load_candidate(chat_id, candidates[0].session_id, candidates[0].workspace)
            return

        async def resolve(values: tuple[str, ...] | None) -> None:
            if not values:
                return
            index = int(values[0])
            candidate = candidates[index]
            await self._load_candidate(chat_id, candidate.session_id, candidate.workspace)

        options = tuple(
            ChoiceOption(
                value=str(index),
                label=f"{index + 1}. {candidate.title or candidate.session_id} ({candidate.workspace})",
            )
            for index, candidate in enumerate(candidates)
        )
        await self._ask(
            chat_id,
            ChoiceRequest(
                request_id=uuid4().hex,
                kind="resume",
                title="Resume a session",
                body="Choose the session to load.",
                options=options,
            ),
            resolve,
        )

    async def _load_candidate(self, chat_id: ChatId, session_id: str, workspace: Path) -> None:
        try:
            loaded = await self._service.load_session(chat_id=chat_id, session_id=session_id, workspace=workspace)
        except Exception as exc:  # noqa: BLE001
            await self._send(chat_id, f"Could not load session: {exc}")
            return
        await self._send(chat_id, f"Loaded session `{loaded}` in `{workspace}`.")

    async def _command_cmds(self, chat_id: ChatId) -> None:
        commands = self._service.get_available_commands(chat_id=chat_id) or ()
        if not commands:
            await self._send(chat_id, "The agent did not advertise any commands.")
            return

        async def resolve(values: tuple[str, ...] | None) -> None:
            if not values:
                return
            await self._submit(
                InboundMessage(
                    chat_id=chat_id,
                    chat_type="private",
                    message_id=uuid4().hex,
                    user_id="",
                    user_name=None,
                    text=f"/{values[0]}",
                )
            )

        options = tuple(
            ChoiceOption(value=command.name, label=f"{command.name} — {command.description}") for command in commands
        )
        await self._ask(
            chat_id,
            ChoiceRequest(
                request_id=uuid4().hex,
                kind="commands",
                title="Agent commands",
                body="Choose a command to run.",
                options=options,
            ),
            resolve,
        )

    async def _command_schedule(self, message: InboundMessage, args: list[str]) -> None:  # noqa: PLR0911
        if self._store is None:
            await self._send(message.chat_id, "Scheduling is not configured.")
            return
        if not args:
            await self._send(message.chat_id, "Usage: `/schedule <when> <prompt>`")
            return
        mode = "prompt_agent"
        if args[0].lower() in {"notify", "prompt"}:
            mode = "notify" if args[0].lower() == "notify" else "prompt_agent"
            args = args[1:]
        if not args:
            await self._send(message.chat_id, "Usage: `/schedule <when> <prompt>`")
            return
        cron_expression: str | None = None
        if args[0].lower() == "cron":
            if len(args) < CRON_MIN_ARGS:
                await self._send(message.chat_id, "Usage: `/schedule cron '<expr>' <prompt>`")
                return
            try:
                cron_expression = normalize_cron_expression(args[1])
                run_at = next_occurrence(cron_expression, after=utc_now())
            except ValueError as exc:
                await self._send(message.chat_id, f"Invalid schedule: {exc}")
                return
            body = " ".join(args[2:])
        else:
            resolved = _parse_run_at(args, self._config.schedule_languages)
            if resolved is None:
                await self._send(message.chat_id, "I could not understand that time. Try `in 10m` or an ISO timestamp.")
                return
            run_at, body = resolved
        session_context = self._service.get_active_session_context(chat_id=message.chat_id)
        try:
            task = self._store.create_task(
                chat_id=message.chat_id,
                session_id=None if session_context is None else session_context[0],
                anchor_message_id=message.message_id,
                mode=mode,
                run_at=run_at,
                prompt_text=body if mode == "prompt_agent" else None,
                notify_text=body if mode == "notify" else None,
                cron_expression=cron_expression,
                source_message_id=message.message_id,
                requester_user_id=message.user_id,
                requester_username=None,
                requester_name=message.user_name,
                chat_type=message.chat_type,
            )
        except Exception as exc:  # noqa: BLE001
            await self._send(message.chat_id, f"Could not schedule that: {exc}")
            return
        repeat = f" (repeats `{cron_expression}`)" if cron_expression else ""
        await self._send(
            message.chat_id,
            f"Scheduled task `{task.id}` for {run_at.isoformat()}{repeat}.",
        )

    async def _command_scheduled(self, chat_id: ChatId) -> None:
        if self._store is None:
            await self._send(chat_id, "Scheduling is not configured.")
            return
        tasks = await asyncio.to_thread(self._store.list_tasks_for_chat, chat_id=chat_id)
        if not tasks:
            await self._send(chat_id, "No scheduled tasks.")
            return
        lines = ["**Scheduled tasks**"]
        for task in tasks:
            body = (task.prompt_text or task.notify_text or "").strip().replace("\n", " ")
            lines.append(f"`{task.id}` {task.run_at.isoformat()} [{task.mode}] {body[:60]}")
        lines.append("Use `/unschedule <id>` to cancel one.")
        await self._send(chat_id, "\n".join(lines))

    async def _command_unschedule(self, chat_id: ChatId, args: list[str]) -> None:
        if self._store is None:
            await self._send(chat_id, "Scheduling is not configured.")
            return
        if not args:
            await self._send(chat_id, "Usage: `/unschedule <id>`")
            return
        cancelled = await asyncio.to_thread(self._store.cancel_task, chat_id=chat_id, task_id=args[0])
        await self._send(chat_id, "Cancelled." if cancelled else "Task not found or not pending.")

    # ------------------------------------------------------------------
    # Scheduled task execution
    # ------------------------------------------------------------------

    async def execute_scheduled_task(self, task: ScheduledTask) -> None:
        if task.mode == "notify":
            text = (task.notify_text or "Completed.").strip()
            await self._channel.send_markdown(task.chat_id, text, reply_to=task.anchor_message_id)
            return
        prompt_text = (task.prompt_text or "").strip()
        if not prompt_text:
            raise ScheduledTaskExecutionError("scheduled prompt is empty")  # noqa: TRY003
        lock = self._lock_for(task.chat_id)
        if lock.locked():
            raise ScheduledTaskDeferredError("chat is busy")  # noqa: TRY003
        async with lock:
            await self._ensure_session(task.chat_id)
            reply = await self._service.prompt(chat_id=task.chat_id, text=prompt_text)
            if reply is None:
                raise ScheduledTaskExecutionError("no active session")  # noqa: TRY003
            await self._deliver_reply(task.chat_id, reply, reply_to=task.anchor_message_id)

    # ------------------------------------------------------------------
    # Group history and helpers
    # ------------------------------------------------------------------

    async def _record_group_message(self, message: InboundMessage) -> None:
        if message.chat_type != "group" or self._config.group_history_size <= 0 or not message.text:
            return
        await self._service.record_group_message(
            chat_id=message.chat_id,
            message_id=message.message_id,
            sender_id=message.user_id,
            sender_name=message.user_name,
            text=message.text,
            max_entries=self._config.group_history_size,
        )

    def _lock_for(self, chat_id: ChatId) -> asyncio.Lock:
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[chat_id] = lock
        return lock


def _activity_line(block: AgentActivityBlock) -> str:
    label = ACTIVITY_LABELS.get(block.kind, block.kind.title() or "Working")
    text = f"{label}: {block.title}"
    if block.text.strip():
        text += f"\n{block.text.strip()}"
    return text


def _match_choice(text: str, request: ChoiceRequest) -> tuple[str, ...] | None:  # noqa: PLR0911
    normalized = text.strip()
    if not normalized:
        return None
    lowered = normalized.casefold()
    if request.skippable and lowered in {"skip", "/skip"}:
        return (SKIP_CHOICE_VALUE,)
    for option in request.options:
        if lowered in {option.value.casefold(), option.label.casefold()}:
            return (option.value,)
    parts = [part for part in re.split(r"[\s,;]+", normalized) if part]
    selected: list[str] = []
    for part in parts:
        try:
            index = int(part) - 1
        except ValueError:
            return None
        if index < 0 or index >= len(request.options):
            return None
        selected.append(request.options[index].value)
    if not selected:
        return None
    return tuple(selected) if request.multiple else (selected[0],)


def _elicitation_answers(values: tuple[str, ...] | None) -> dict[str, tuple[str, ...]] | None:
    if not values or SKIP_CHOICE_VALUE in values:
        return None
    answers: dict[str, list[str]] = {}
    for value in values:
        field_name, separator, answer = value.partition("\x1f")
        if not separator:
            continue
        answers.setdefault(field_name, []).append(answer)
    return {field_name: tuple(answers[field_name]) for field_name in answers}


def _permission_action(value: str) -> PermissionDecisionAction:
    if value in {"once", "always", "deny"}:
        return value  # type: ignore[return-value]
    return "deny"


def _parse_run_at(args: list[str], languages: tuple[str, ...]) -> tuple[datetime, str] | None:
    for end in range(len(args) - 1, 0, -1):
        expression = " ".join(args[:end])
        body = " ".join(args[end:]).strip()
        if not body:
            continue
        parsed = dateparser.parse(
            expression,
            languages=list(languages),
            settings={"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": True},
        )
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        run_at = parsed.astimezone(UTC)
        if run_at <= utc_now():
            continue
        return run_at, body
    return None


__all__: list[str] = ["HELP_TEXT", "SKIP_CHOICE_VALUE", "AgentChatBridge"]
