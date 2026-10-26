"""TelegramBridge: Telegram command and message dispatch for the ACP bot."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import cast
from urllib.parse import urlparse
from uuid import uuid4

import dateparser
from telegram import (
    Bot,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Message,
    MessageEntity,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegramify_markdown import MessageEntity as MarkdownMessageEntity
from telegramify_markdown import convert, split_entities, utf16_len

from telegram_acp_bot.acp.models import (
    ActivityMode,
    AgentActivityBlock,
    AgentCommand,
    AgentConnectionLostError,
    AgentOutputLimitExceededError,
    AgentReply,
    ElicitationOption,
    ElicitationQuestion,
    ElicitationRequest,
    FilePayload,
    ImagePayload,
    PermissionRequest,
    PromptFile,
    PromptImage,
    ResumableSession,
)
from telegram_acp_bot.agent_identity import DEFAULT_AGENT_NAME
from telegram_acp_bot.logging_context import bind_log_context, log_text_preview
from telegram_acp_bot.scheduled_tasks import (
    ScheduledTask,
    ScheduledTaskDeferredError,
    ScheduledTaskExecutionError,
    ScheduledTaskStore,
)
from telegram_acp_bot.scheduled_tasks.store import parse_utc_timestamp, utc_now
from telegram_acp_bot.telegram.activity import (
    _ActivityModeHandler,
    _CompactActivityModeHandler,
    _NormalActivityModeHandler,
    _VerboseActivityModeHandler,
)
from telegram_acp_bot.telegram.config import BotConfig
from telegram_acp_bot.telegram.constants import (
    ACTIVITY_MODE_CHOICES,
    ACTIVITY_MODE_HELP,
    AGENT_COMMAND_CALLBACK_PARTS,
    AGENT_COMMAND_CALLBACK_PREFIX,
    AGENT_COMMAND_DESCRIPTION_CHARS,
    AGENT_COMMAND_KEYBOARD_COLUMNS,
    AGENT_COMMAND_KEYBOARD_INFO_CALLBACK_DATA,
    AGENT_COMMAND_KEYBOARD_INFO_LABEL,
    AGENT_COMMAND_KEYBOARD_MAX_ROWS,
    BUSY_CALLBACK_PARTS,
    BUSY_CALLBACK_PREFIX,
    BUSY_QUEUED_TEXT,
    BUSY_SENT_TEXT,
    BUSY_STILL_QUEUED_TEXT,
    COMPACT_STATUS_HINT_CHARS,
    COMPACT_STATUS_TICK_SECONDS,
    ELICITATION_ANSWER_JOINER,
    ELICITATION_ANSWER_RECORD_PREFIX,
    ELICITATION_CALLBACK_PARTS,
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
    GROUP_BOT_TURN_BUDGET,
    GROUP_BUDGET_HOLD_TEXT,
    KIND_LABELS,
    MAX_RESTART_ARGS,
    MAX_RESUME_ARGS,
    PERMISSION_CALLBACK_PARTS,
    PERMISSION_CALLBACK_PREFIX,
    REPLY_LABEL,
    RESUME_CALLBACK_PARTS,
    RESUME_CALLBACK_PREFIX,
    RESUME_KEYBOARD_MAX_ROWS,
    SCHEDULE_COMMAND_USAGE,
    SCHEDULED_CALLBACK_PARTS,
    SCHEDULED_CALLBACK_PREFIX,
    SCHEDULED_KEYBOARD_MAX_ROWS,
    SEARCH_LABEL_NEUTRAL,
    SEARCH_LABEL_WEB,
    TELEGRAM_MAX_UTF16_MESSAGE_LENGTH,
    TELEGRAM_UNCHANGED_MESSAGE_ERROR,
)
from telegram_acp_bot.telegram.models import (
    AgentService,
    ChatRequiredError,
    _PendingPrompt,
    _PromptInput,
    _RestartArgs,
    _ResumeArgs,
)
from telegram_acp_bot.telegram.reactions import PromptStatus, StatusReactionSender

logger = logging.getLogger(__name__)
APP_NOT_RUNNING_ERROR = "application is not running"
SCHEDULED_CHAT_BUSY_ERROR = "chat is busy"
SCHEDULED_PROMPT_TRY_LOCK_TIMEOUT_SECONDS = 1e-9
SCHEDULED_MISSING_ANCHOR_ERROR = "scheduled task is missing an anchor message"
SCHEDULED_TASK_PREVIEW_MAX_CHARS = 72
SCHEDULE_COMMAND_MIN_ARGS = 2
_SCHEDULE_DELAY_RE = re.compile(r"^(\d+)(s|m|h|d)$", re.IGNORECASE)
GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})
_AGENT_COMMAND_TOKEN_RE = re.compile(r"^/([A-Za-z0-9_-]{1,32})(?:@([A-Za-z0-9_]{1,32}))?(?:\s|$)")
_AGENT_COMMAND_BUTTON_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def _text_mentions_handle(text: str | None, entities: tuple[MessageEntity, ...] | None, handle: str) -> bool:
    lowered = (text or "").lower()
    if handle in lowered:
        return True
    return any(
        entity.type == "mention" and lowered[entity.offset : entity.offset + entity.length] == handle
        for entity in entities or ()
    )


def _mentions_handle(message: Message, handle: str) -> bool:
    """True when a message names this bot, whichever of its text or caption carries the mention."""
    if _text_mentions_handle(message.text, getattr(message, "entities", None), handle):
        return True
    return _text_mentions_handle(message.caption, getattr(message, "caption_entities", None), handle)


def _media_references(message: Message) -> tuple[tuple[str, str | None], ...]:
    """Return `(file_id, mime_type)` for media a message carries, largest photo rendition only."""

    if message.photo:
        return ((message.photo[-1].file_id, "image/jpeg"),)
    document = message.document
    if document is not None:
        return ((document.file_id, document.mime_type),)
    return ()


PENDING_MEDIA_HEADER = (
    "Pending Telegram attachments (not downloaded yet). "
    "Call telegram_fetch_attachment with a file_id when you actually need one, or "
    "telegram_download_file with the message_id when the file is larger than 20 MB, "
    "which the Bot API refuses to serve. "
    "Telegram keeps a media file downloadable for roughly an hour after it arrives, "
    "so fetch the ones you need now:"
)


def _with_pending_media(text: str, refs: tuple[dict[str, object], ...]) -> str:
    """Prefix a prompt with the media references the mention gate parked for this chat."""

    if not refs:
        return text
    lines = [PENDING_MEDIA_HEADER]
    lines.extend(
        f"- file_id={ref['file_id']} message_id={ref['message_id']} "
        f"mime={ref['mime_type']} queued_at={ref['queued_at']}"
        for ref in refs
    )
    notice = "\n".join(lines)
    return f"{notice}\n\n{text}" if text else notice


def _sender_label(user: object) -> str:
    """Who sent an update, in the form the agent should address them with.

    A public username wins, because a reply that repeats it becomes a Telegram mention the person is
    notified about. A member without one falls back to their display name, which identifies them in
    text but cannot be mentioned.
    """
    username = str(getattr(user, "username", None) or "").lstrip("@").strip()
    if username:
        return f"@{username}"
    return str(getattr(user, "full_name", None) or "").strip()


def _with_sender_label(text: str, label: str) -> str:
    """Prefix a group prompt with the sender, so the agent knows who it is answering."""

    if not label:
        return text
    return f"{label}: {text}" if text else label


GROUP_MENTION_REMINDER = (
    "Group chat: everyone reads your reply, and nobody is notified unless you name them. "
    "Open your answer with the @username of the person you are replying to — that is the sender below "
    "when they asked you, and someone else when the conversation is about them, since a request like "
    "'discuss this with Bob' wants an answer addressed to Bob, not to the requester. "
    "`telegram_list_messages` lists the @usernames of recent senders when you need one you have not seen. "
    "Skip the mention only when the whole group is the audience."
)


def _with_group_context(text: str, label: str) -> str:
    """Tag a group prompt with its sender and the reminder that only a named reply notifies anyone."""

    tagged = _with_sender_label(text, label)
    if not tagged:
        return GROUP_MENTION_REMINDER
    return f"{GROUP_MENTION_REMINDER}\n\n{tagged}"


def _command_name(name: str) -> str:
    """Normalize a command name for comparison, whichever casing the agent published."""
    return name.removeprefix("/").lower()


def _command_button(command: AgentCommand) -> InlineKeyboardButton | None:
    """Tap-to-run button for one advertised command, or None when its name cannot travel as callback data.

    Callback data is capped at 64 bytes and the gate only recognises `[A-Za-z0-9_-]{1,32}` names, so a
    name outside that shape would produce a button whose tap the prompt path could never honour.
    """
    name = _command_name(command.name)
    if _AGENT_COMMAND_BUTTON_NAME_RE.fullmatch(name) is None:
        return None
    return InlineKeyboardButton(text=f"/{name}", callback_data=f"{AGENT_COMMAND_CALLBACK_PREFIX}|{name}")


def _bot_handle(context: object) -> str:
    """This bot's own Telegram username, lowercased and without the leading `@`."""
    username = getattr(getattr(context, "bot", None), "username", None) or ""
    return username.lstrip("@").strip().lower()


def _without_own_mention(text: str, match: re.Match[str], handle: str) -> str:
    """Drop the `@<this bot>` suffix Telegram appends to menu-picked commands, keeping the arguments.

    Agents parse the raw prompt text, and an advertised command written `/goal@some_bot` is not a
    command name they know, so the mention is removed before the text reaches `session/prompt`.
    """
    mention = match.group(2)
    if mention is None or mention.lower() != handle.lower():
        return text
    return f"/{match.group(1)}{text[match.end(1) + len(mention) + 1 :]}"


class _AgentCommandMessage(filters.MessageFilter):
    """Match messages that open with a `/command` token, in the text or in a media caption.

    `telegram.ext.filters.COMMAND` keys on the `bot_command` entity, which the Bot API only
    attaches reliably in private chats, so the token is matched from the raw text instead.
    """

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__(name="filters.AgentCommand")

    def filter(self, message: Message) -> bool:
        return _AGENT_COMMAND_TOKEN_RE.match(message.text or message.caption or "") is not None


_AGENT_COMMAND = _AgentCommandMessage()

_BotCommandHandler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


class TelegramBridge:
    """Telegram command and message handlers for the MVP bot."""

    def __init__(
        self,
        config: BotConfig,
        agent_service: AgentService,
        *,
        scheduled_task_store: ScheduledTaskStore | None = None,
        stop_request: Callable[[], None] | None = None,
        agent_name: str = DEFAULT_AGENT_NAME,
    ) -> None:
        self._config = config
        self._agent_service = agent_service
        self._scheduled_task_store = scheduled_task_store
        # The agent this bot talks to, named so several bots in one process tell their
        # users apart.
        self._agent_name = agent_name
        # Set when a Telegram command asks the whole process to exit, so the runner can
        # tear every instance down instead of only this bot's application.
        self._stop_request = stop_request
        self._app: Application | None = None
        self._bot_commands: dict[str, _BotCommandHandler] = {}
        self._restart_requested = False
        self._activity_mode_by_chat: dict[int, ActivityMode] = {}
        self._implicit_start_locks_by_chat: dict[int, asyncio.Lock] = {}
        self._pending_resume_choices_by_chat: dict[int, tuple[ResumableSession, ...]] = {}
        self._chat_prompt_locks: dict[int, asyncio.Lock] = {}
        self._pending_prompts_by_chat: dict[int, _PendingPrompt] = {}
        self._dequeued_prompts_by_chat: dict[int, _PendingPrompt] = {}
        self._compact_status_msg_id: dict[int, int] = {}
        # Per-chat lock to prevent concurrent sends from creating multiple status messages.
        self._compact_status_locks: dict[int, asyncio.Lock] = {}
        # Current compact status label per chat (for example "⚙️ Running").
        self._compact_status_label: dict[int, str] = {}
        # Text actually shown on the status message per chat, so unchanged labels skip the API call.
        self._compact_status_displayed: dict[int, str] = {}
        # Background task that paints the current label onto the single status message per chat.
        self._compact_status_tasks: dict[int, asyncio.Task[None]] = {}
        self._suppressed_activity_chats: set[int] = set()
        # Answers one bot drew out of another since a human last spoke in that chat, so a group of
        # bots cannot keep each other busy for ever. See {py:meth}`_reserve_bot_turn`.
        self._bot_turns_by_chat: dict[int, int] = {}
        # Chats that already saw the hold notice since the last human message.
        self._bot_budget_notified: set[int] = set()
        # Form the bot offered per pending elicitation, so a later tap can re-render the same keyboard.
        self._elicitation_forms: dict[str, ElicitationRequest] = {}
        # Option indexes chosen per pending elicitation, keyed by the schema field being answered.
        self._elicitation_selections: dict[str, dict[str, list[int]]] = {}
        # Question message per pending elicitation, so its keyboard can be removed once it stops waiting.
        self._elicitation_messages: dict[str, int | None] = {}
        self._status_reactions = StatusReactionSender(config.status_reactions)
        self._activity_handlers: dict[ActivityMode, _ActivityModeHandler] = {
            "normal": _NormalActivityModeHandler(self),
            "compact": _CompactActivityModeHandler(self),
            "verbose": _VerboseActivityModeHandler(self),
        }
        if hasattr(self._agent_service, "set_permission_request_handler"):
            self._agent_service.set_permission_request_handler(self.on_permission_request)
        if hasattr(self._agent_service, "set_activity_event_handler"):
            self._agent_service.set_activity_event_handler(self.on_activity_event)
        if hasattr(self._agent_service, "set_elicitation_request_handler"):
            self._agent_service.set_elicitation_request_handler(self.on_elicitation_request)
        if hasattr(self._agent_service, "set_elicitation_expiry_handler"):
            self._agent_service.set_elicitation_expiry_handler(self.on_elicitation_expired)

    @property
    def restart_requested(self) -> bool:
        """Whether a `/restart` already asked this process to exit and re-exec."""
        return self._restart_requested

    def install(self, app: Application) -> None:
        self._app = app
        for name, callback in (
            ("start", self.start),
            ("help", self.help),
            ("new", self.new_session),
            ("resume", self.resume_session),
            ("schedule", self.schedule_prompt),
            ("scheduled", self.scheduled),
            ("mode", self.mode),
            ("session", self.session),
            ("cancel", self.cancel),
            ("stop", self.stop),
            ("clear", self.clear),
            ("restart", self.restart),
            ("cmds", self.cmds),
        ):
            self._bot_commands[name] = callback
            app.add_handler(CommandHandler(name, callback))
        app.add_handler(CallbackQueryHandler(self.on_permission_callback, pattern=r"^perm\|"))
        app.add_handler(CallbackQueryHandler(self.on_resume_callback, pattern=r"^resume\|"))
        app.add_handler(CallbackQueryHandler(self.on_scheduled_callback, pattern=r"^scheduled\|"))
        app.add_handler(CallbackQueryHandler(self.on_busy_callback, pattern=r"^busy\|"))
        app.add_handler(CallbackQueryHandler(self.on_agent_command_callback, pattern=r"^cmd\|"))
        app.add_handler(CallbackQueryHandler(self.on_elicitation_callback, pattern=r"^ask\|"))
        app.add_handler(MessageHandler(_AGENT_COMMAND, self.on_agent_command))
        app.add_handler(
            MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~_AGENT_COMMAND, self.on_message)
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._require_access(update):
            return
        await self._reply(
            update,
            "Send a message to start in the default workspace, or use /new [workspace] or /resume [N|workspace].",
        )

    def _help_header(self) -> str:
        """Name the agent behind this bot, so several bots in one process tell users apart."""
        if self._agent_name == DEFAULT_AGENT_NAME:
            return ""
        return f"You are talking to the {self._agent_name} agent.\n\n"

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._require_access(update):
            return
        await self._reply(
            update,
            f"{self._help_header()}Commands: /new [workspace], /resume [N|workspace], /schedule <time> <prompt>, "
            "/scheduled, "
            "/mode [normal|compact|verbose], "
            "/session, /cancel, /stop, /clear, /restart [N [workspace]], /cmds, /help",
        )

    async def cmds(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._require_access(update):
            return
        commands = self._advertised_commands(chat_id=self._chat_id(update))
        if not commands:
            await self._reply(
                update,
                "This agent has not advertised any slash commands yet. Send a message first, or use /new.",
            )
            return
        message = update.message
        if message is None:
            return
        await message.reply_text(
            f"{len(commands)} agent commands. Tap one to run it, or type it with arguments.",
            reply_markup=self._commands_keyboard(commands=commands),
        )

    def _advertised_commands(self, *, chat_id: int) -> tuple[AgentCommand, ...]:
        """Slash commands the agent published for this chat; empty when the service or session has none."""
        provider = getattr(self._agent_service, "get_available_commands", None)
        if not callable(provider):
            return ()
        return provider(chat_id=chat_id) or ()

    @staticmethod
    def _commands_keyboard(*, commands: tuple[AgentCommand, ...]) -> InlineKeyboardMarkup:
        """Grid of tap-to-run buttons, with the plain description listing kept one tap away."""
        buttons = [button for button in map(_command_button, commands) if button is not None]
        visible = buttons[: AGENT_COMMAND_KEYBOARD_MAX_ROWS * AGENT_COMMAND_KEYBOARD_COLUMNS]
        rows = [
            list(visible[start : start + AGENT_COMMAND_KEYBOARD_COLUMNS])
            for start in range(0, len(visible), AGENT_COMMAND_KEYBOARD_COLUMNS)
        ]
        rows.append(
            [
                InlineKeyboardButton(
                    text=AGENT_COMMAND_KEYBOARD_INFO_LABEL,
                    callback_data=AGENT_COMMAND_KEYBOARD_INFO_CALLBACK_DATA,
                )
            ]
        )
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _commands_listing(commands: tuple[AgentCommand, ...]) -> str:
        lines: list[str] = []
        for command in commands:
            description = command.description
            if len(description) > AGENT_COMMAND_DESCRIPTION_CHARS:
                description = f"{description[: AGENT_COMMAND_DESCRIPTION_CHARS - 1].rstrip()}…"
            suffix = f" — `{command.hint}`" if command.hint else ""
            lines.append(f"/{command.name}: {description}{suffix}")
        return "\n".join(lines)

    async def mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_access(update):
            return
        chat_id = self._chat_id(update)
        args = self._context_args(context)
        if not args:
            await self._reply(
                update,
                f"Current activity mode: `{self._activity_mode(chat_id=chat_id)}`\nUsage: `/mode {ACTIVITY_MODE_HELP}`",
            )
            return
        if len(args) != 1 or args[0] not in ACTIVITY_MODE_CHOICES:
            await self._reply(update, f"Usage: `/mode {ACTIVITY_MODE_HELP}`")
            return
        mode = cast(ActivityMode, args[0])
        await self._set_activity_mode(chat_id=chat_id, mode=mode)
        await self._reply(update, f"Activity mode set to `{mode}`.")

    async def new_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_access(update):
            return

        chat_id = self._chat_id(update)
        workspace = self._workspace_from_args(self._context_args(context))
        workspace_was_missing = not workspace.exists()
        started = await self._start_session(
            update=update,
            chat_id=chat_id,
            workspace=workspace,
            invalid_workspace_label="Invalid workspace",
        )
        if started is None:
            return
        session_id, active_workspace = started
        response = f"Session started: `{session_id}` in `{active_workspace}`"
        if workspace_was_missing:
            response = f"{response}\nCreated workspace: `{active_workspace}`"
        await self._reply(update, response)

    async def resume_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: PLR0911
        if not await self._require_access(update):
            return

        chat_id = self._chat_id(update)
        parsed_args = self._parse_resume_args(self._context_args(context))
        if parsed_args is None:
            await self._reply(update, "Usage: /resume, /resume N, or /resume [workspace]")
            return
        try:
            candidates = await self._agent_service.list_resumable_sessions(
                chat_id=chat_id,
                workspace=parsed_args.workspace,
            )
        except Exception as exc:
            logger.exception("Failed to list resumable sessions for chat_id=%s", chat_id)
            await self._reply(update, f"Failed to list resumable sessions: {exc}")
            return
        if candidates is None:
            await self._reply(update, "Agent does not support ACP `session/list`.")
            return
        if not candidates:
            await self._reply(update, "No resumable sessions found.")
            return

        if parsed_args.workspace is not None:
            await self._resume_candidate(update=update, chat_id=chat_id, candidate=candidates[0])
            return

        if parsed_args.resume_index is not None:
            if parsed_args.resume_index < 0 or parsed_args.resume_index >= len(candidates):
                await self._reply(
                    update,
                    f"Invalid resume index `{parsed_args.resume_index}`. Choose 0..{len(candidates) - 1}.",
                )
                return
            await self._resume_candidate(update=update, chat_id=chat_id, candidate=candidates[parsed_args.resume_index])
            return

        if self._app is None:
            await self._resume_candidate(update=update, chat_id=chat_id, candidate=candidates[0])
            return

        self._pending_resume_choices_by_chat[chat_id] = candidates
        keyboard = self._resume_keyboard(candidates=candidates)
        message = "Pick a session to resume:"
        await self._app.bot.send_message(
            chat_id=chat_id,
            text=message,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
        )

    async def session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._require_access(update):
            return

        workspace = self._agent_service.get_workspace(chat_id=self._chat_id(update))
        if workspace is None:
            await self._reply(update, "No active session. Use /new first.")
            return

        await self._reply(update, f"Active session workspace: `{workspace}`")

    async def scheduled(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._require_access(update):
            return
        if self._scheduled_task_store is None:
            await self._reply(update, "Scheduled tasks are unavailable.")
            return

        chat_id = self._chat_id(update)
        tasks = await self._list_visible_scheduled_tasks(chat_id=chat_id)
        text = self._format_scheduled_tasks_text(tasks=tasks)
        keyboard = self._scheduled_keyboard(tasks=tasks)
        if update.message is None:
            return
        await update.message.reply_text(text, reply_markup=keyboard)

    async def schedule_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle `/schedule <time> <prompt>` to queue a deferred agent prompt directly."""
        if not await self._require_access(update):
            return
        message = cast(Message | None, getattr(update, "effective_message", None) or update.message)
        if self._scheduled_task_store is None:
            await self._reply_message(message, "Scheduled tasks are unavailable.")
            return

        chat_id = self._chat_id(update)
        parsed_command = self._parse_schedule_command(update=update, context=context)
        if parsed_command is None:
            await self._reply_message(message, SCHEDULE_COMMAND_USAGE)
            return
        time_spec, prompt_text = parsed_command

        run_at = TelegramBridge._parse_schedule_time(time_spec, languages=self._config.schedule_languages)
        if run_at is None:
            await self._reply_message(message, SCHEDULE_COMMAND_USAGE)
            return

        if message is None:
            return
        anchor_message_id = message.message_id

        session_context = self._active_session_context(chat_id=chat_id)
        session_id = None if session_context is None else session_context[0]

        try:
            task = await asyncio.to_thread(
                self._scheduled_task_store.create_task,
                chat_id=chat_id,
                session_id=session_id,
                anchor_message_id=anchor_message_id,
                mode="prompt_agent",
                prompt_text=prompt_text,
                run_at=run_at,
            )
        except Exception:
            logger.exception("Failed to create scheduled task for chat %s", chat_id)
            await self._reply_message(message, "Could not save scheduled task.")
            return

        timestamp = task.run_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        await self._reply_message(message, f"Scheduled for {timestamp}. Use /scheduled to view or cancel.")

    def _parse_schedule_command(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> tuple[str, str] | None:
        """Extract the time spec and prompt text from `/schedule` input."""

        message = cast(Message | None, getattr(update, "effective_message", None) or update.message)
        raw_text = "" if message is None else (message.text or "")
        if raw_text.strip():
            command_match = re.match(r"^\s*\S+\s+([\s\S]*)$", raw_text)
            if command_match is None:
                return None
            return self._split_schedule_spec_and_prompt(command_match.group(1))

        args = self._context_args(context)
        if len(args) < SCHEDULE_COMMAND_MIN_ARGS:
            return None
        return self._split_schedule_spec_and_prompt(" ".join(args))

    def _split_schedule_spec_and_prompt(self, remainder: str) -> tuple[str, str] | None:
        """Infer the `/schedule` time span and prompt body from *remainder*."""

        token_matches = tuple(re.finditer(r"\S+", remainder))
        for token_count in range(len(token_matches) - 1, 0, -1):
            time_spec = remainder[: token_matches[token_count - 1].end()].strip()
            prompt_text = remainder[token_matches[token_count - 1].end() :].strip()
            if self._parse_schedule_time(time_spec, languages=self._config.schedule_languages) is not None:
                return time_spec, prompt_text
        return None

    @staticmethod
    def _parse_schedule_time(spec: str, *, languages: tuple[str, ...] = ("en", "es")) -> datetime | None:
        """Parse a time spec like `30s`, `10m`, `2h`, `1d` or an ISO timestamp.

        Returns a timezone-aware UTC datetime, or `None` when the spec is
        not recognised. See also :py:class:`~telegram_acp_bot.scheduled_tasks.store.ScheduledTaskStore`.
        """
        normalized_spec = spec.strip()
        if relative_time := TelegramBridge._parse_relative_schedule_time(normalized_spec):
            return relative_time

        try:
            return parse_utc_timestamp(normalized_spec)
        except ValueError:
            pass

        if normalized_spec.isdigit():
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?", normalized_spec):
            return None

        parsed_natural_time = dateparser.parse(
            normalized_spec,
            languages=list(languages),
            settings={
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TIMEZONE": "UTC",
                "TO_TIMEZONE": "UTC",
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": utc_now(),
            },
        )
        return None if parsed_natural_time is None else parsed_natural_time.astimezone(UTC)

    @staticmethod
    def _parse_relative_schedule_time(spec: str) -> datetime | None:
        """Parse `<amount><unit>` schedule delays into an absolute UTC timestamp."""

        match = _SCHEDULE_DELAY_RE.match(spec)
        if match is None:
            return None
        try:
            value = int(match.group(1))
            unit = match.group(2).lower()
            match unit:
                case "s":
                    delta = timedelta(seconds=value)
                case "m":
                    delta = timedelta(minutes=value)
                case "h":
                    delta = timedelta(hours=value)
                case "d":
                    delta = timedelta(days=value)
                case _:  # pragma: no cover
                    return None
            return utc_now() + delta
        except OverflowError:
            return None

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._require_access(update):
            return

        cancelled = await self._agent_service.cancel(chat_id=self._chat_id(update))
        if cancelled:
            await self._reply(update, "Cancelled current operation.")
            return
        await self._reply(update, "No active session. Use /new first.")

    async def stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._require_access(update):
            return

        stopped = await self._agent_service.stop(chat_id=self._chat_id(update))
        if stopped:
            await self._reply(update, "Stopped current session.")
            return
        await self._reply(update, "No active session. Use /new first.")

    async def clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not await self._require_access(update):
            return

        cleared = await self._agent_service.clear(chat_id=self._chat_id(update))
        if cleared:
            await self._reply(update, "Cleared current session.")
            return
        await self._reply(update, "No active session. Use /new first.")

    async def restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_access(update):
            return

        chat_id = self._chat_id(update)
        parsed_args = self._parse_restart_args(self._context_args(context))
        if parsed_args is None:
            await self._reply(update, "Usage: /restart or /restart N [workspace]")
            return
        if parsed_args.workspace is not None and parsed_args.resume_index is None:
            await self._reply(update, "Usage: /restart or /restart N [workspace]")
            return
        if parsed_args.resume_index is not None:
            await self._restart_with_index(
                update=update,
                chat_id=chat_id,
                resume_index=parsed_args.resume_index,
                workspace=parsed_args.workspace,
            )
            return

        await self._restart_process(update=update, chat_id=chat_id)

    async def _restart_with_index(
        self,
        *,
        update: Update,
        chat_id: int,
        resume_index: int,
        workspace: Path | None,
    ) -> None:
        try:
            candidates = await self._agent_service.list_resumable_sessions(
                chat_id=chat_id,
                workspace=workspace,
            )
        except Exception as exc:
            logger.exception("Failed to list resumable sessions for chat_id=%s", chat_id)
            await self._reply(update, f"Failed to list resumable sessions: {exc}")
            return
        if candidates is None:
            await self._reply(update, "Agent does not support ACP `session/list`.")
            return
        if not candidates:
            await self._reply(update, "No resumable sessions found.")
            return
        if resume_index < 0 or resume_index >= len(candidates):
            await self._reply(
                update,
                f"Invalid restart index `{resume_index}`. Choose 0..{len(candidates) - 1}.",
            )
            return
        await self._resume_candidate(
            update=update,
            chat_id=chat_id,
            candidate=candidates[resume_index],
            success_label="Session resumed",
        )

    async def _restart_process(self, *, update: Update, chat_id: int) -> None:
        if self._app is None:
            await self._reply(update, "Restart is unavailable: application is not running.")
            return
        active_session = self._active_session_context(chat_id=chat_id)
        if active_session is None:
            await self._reply(update, "No active session. Use /new first.")
            return
        session_id, workspace = active_session
        await self._reply(update, self._format_restart_response(session_id=session_id, workspace=workspace))
        self._restart_requested = True
        if self._stop_request is not None:
            self._stop_request()

    async def on_elicitation_request(self, request: ElicitationRequest) -> None:
        """Offer the agent's structured question as tap-able choices in the chat it came from."""
        if self._app is None:
            return

        await self._remember_elicitation_form(request)
        sent = await self._send_markdown_to_chat(
            bot=self._app.bot,
            chat_id=request.chat_id,
            text=self._format_elicitation_text(request),
            reply_markup=self._elicitation_keyboard(request, selections={}),
        )
        self._elicitation_messages[request.request_id] = getattr(sent, "message_id", None)
        if sent is not None:
            self._close_compact_status_bubble(request.chat_id)
        with bind_log_context(chat_id=request.chat_id):
            logger.info(
                "Elicitation offered: request_id=%s questions=%s message_id=%s",
                request.request_id,
                len(request.questions),
                getattr(sent, "message_id", None),
            )

    async def on_elicitation_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        query = update.callback_query
        if query is None:
            return
        try:
            if not await self._require_access(update):
                await query.answer("Access denied.")
                return

            parts = (query.data or "").split("|", maxsplit=ELICITATION_CALLBACK_PARTS - 1)
            if len(parts) != ELICITATION_CALLBACK_PARTS:
                await query.answer("Invalid action.")
                return
            form = self._elicitation_forms.get(parts[1])
            if form is None:
                logger.warning("Elicitation tap on a question that stopped waiting: request_id=%s", parts[1])
                await query.answer("This question is no longer waiting for an answer.")
                return
            await self._take_elicitation_choice(form=form, token=parts[2], query=query)
        except Exception:
            logger.exception("Unhandled error while processing elicitation callback")
            await query.answer("Answering this question failed.")

    async def on_elicitation_expired(self, chat_id: int, request_id: str) -> None:
        """Take the keyboard off a question the agent stopped waiting for, answered or not.

        Without this, an abandoned question keeps looking tappable next to a live one, and every tap on
        it only flashes a toast the user can easily miss.
        """
        if self._app is None:
            return
        unanswered = self._elicitation_forms.pop(request_id, None) is not None
        message_id = self._elicitation_messages.pop(request_id, None)
        self._elicitation_selections.pop(request_id, None)
        if not unanswered or message_id is None:
            return
        with bind_log_context(chat_id=chat_id):
            logger.info("Elicitation expired: request_id=%s message_id=%s", request_id, message_id)
        try:
            await self._app.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                api_kwargs=dict(ELICITATION_REMOVED_KEYBOARD),
            )
        except BadRequest as exc:
            with bind_log_context(chat_id=chat_id):
                logger.warning("Elicitation keyboard left in place: request_id=%s error=%s", request_id, exc)

    async def _take_elicitation_choice(self, *, form: ElicitationRequest, token: str, query: CallbackQuery) -> None:
        """Apply one button of an offered form: skip, submit the picks, or record a tap."""
        with bind_log_context(chat_id=form.chat_id):
            logger.info("Elicitation tapped: request_id=%s token=%s", form.request_id, token)
        if token == ELICITATION_SKIP_TOKEN:
            await self._resolve_elicitation(form, answers=None, query=query, toast="Skipped.")
            return
        if token == ELICITATION_SUBMIT_TOKEN:
            submitted = self._elicitation_answers(form, final=True)
            if submitted is None:
                await query.answer("Pick at least one option for every question.")
                return
            await self._resolve_elicitation(form, answers=submitted, query=query, toast="Answer sent.")
            return

        if not self._record_elicitation_tap(form=form, token=token):
            await query.answer("That option is no longer available.")
            return
        answers = self._elicitation_answers(form)
        if answers is None:
            await self._replace_elicitation_markup(query, self._elicitation_keyboard(form))
            await query.answer("Pick every answer, then tap Send my answer.")
            return
        await self._resolve_elicitation(form, answers=answers, query=query, toast="Answer sent.")

    async def _replace_elicitation_markup(self, query: CallbackQuery, markup: InlineKeyboardMarkup | None) -> None:
        """Put a new keyboard on the question message; a tap that changed nothing is not a failure.

        Telegram removes an inline keyboard only when it is told to set an explicitly empty one: both an
        omitted ``reply_markup`` and an empty ``InlineKeyboardMarkup`` (which serializes to ``{}``) mean
        "change nothing" and come back as ``Message is not modified``.
        """
        try:
            if markup is None:
                await query.edit_message_reply_markup(api_kwargs=dict(ELICITATION_REMOVED_KEYBOARD))
            else:
                await query.edit_message_reply_markup(reply_markup=markup)
        except BadRequest as exc:
            if TELEGRAM_UNCHANGED_MESSAGE_ERROR not in str(exc).lower():
                raise
            logger.debug("Elicitation keyboard unchanged: %s", exc)

    async def _remember_elicitation_form(self, request: ElicitationRequest) -> None:
        """Keep the offered form so a later tap can re-render the same keyboard.

        Shrinking the list is not only a memory decision. The service waits on every question it
        asked, so a form dropped silently would leave its buttons on screen and hold the agent open
        for the whole timeout while the bridge denied knowing about it.

        The cap is per chat: a burst of questions in one chat must not decline another chat's
        still-open question, which is what a shared slice did.
        """
        self._elicitation_forms[request.request_id] = request
        self._elicitation_selections[request.request_id] = {}
        same_chat = [form for form in self._elicitation_forms.values() if form.chat_id == request.chat_id]
        for evicted in same_chat[:-ELICITATION_FORMS_MAX]:
            await self._retire_elicitation(evicted)

    async def _retire_elicitation(self, request: ElicitationRequest) -> None:
        """Close a question the bounded form list is dropping, so no keyboard outlives its answer."""
        with bind_log_context(chat_id=request.chat_id):
            logger.info("Elicitation dropped from the form list: request_id=%s", request.request_id)
        await self._agent_service.respond_elicitation_request(
            chat_id=request.chat_id,
            request_id=request.request_id,
            answers=None,
        )
        await self.on_elicitation_expired(request.chat_id, request.request_id)

    def _forget_elicitation(self, request_id: str) -> None:
        self._elicitation_forms.pop(request_id, None)
        self._elicitation_selections.pop(request_id, None)
        self._elicitation_messages.pop(request_id, None)

    @staticmethod
    def _format_elicitation_text(request: ElicitationRequest) -> str:
        numbered = len(request.questions) > 1
        lines = ["*❓ The agent is asking*", request.title]
        for index, question in enumerate(request.questions, start=1):
            prefix = f"{index}. " if numbered else ""
            suffix = " _(pick one or more)_" if question.multiple else ""
            lines.append(f"{prefix}{question.prompt}{suffix}")
        return "\n\n".join(lines)

    def _elicitation_keyboard(
        self,
        request: ElicitationRequest,
        *,
        selections: Mapping[str, list[int]] | None = None,
    ) -> InlineKeyboardMarkup:
        chosen = selections if selections is not None else self._elicitation_selections.get(request.request_id, {})
        rows: list[list[InlineKeyboardButton]] = []
        for question_index, question in enumerate(request.questions):
            picked = chosen.get(question.field, ())
            buttons: list[InlineKeyboardButton] = []
            for option_index, option in enumerate(question.options):
                label = self._elicitation_button_label(question, option, selected=option_index in picked)
                callback_data = f"{ELICITATION_CALLBACK_PREFIX}|{request.request_id}|{question_index}.{option_index}"
                buttons.append(InlineKeyboardButton(text=label, callback_data=callback_data))
                if len(buttons) >= ELICITATION_KEYBOARD_COLUMNS:
                    rows.append(buttons)
                    buttons = []
            if buttons:
                rows.append(buttons)
        if any(question.multiple for question in request.questions):
            submit_data = f"{ELICITATION_CALLBACK_PREFIX}|{request.request_id}|{ELICITATION_SUBMIT_TOKEN}"
            rows.append([InlineKeyboardButton(text=ELICITATION_SUBMIT_LABEL, callback_data=submit_data)])
        skip_data = f"{ELICITATION_CALLBACK_PREFIX}|{request.request_id}|{ELICITATION_SKIP_TOKEN}"
        rows.append([InlineKeyboardButton(text=ELICITATION_SKIP_LABEL, callback_data=skip_data)])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _elicitation_button_label(question: ElicitationQuestion, option: ElicitationOption, *, selected: bool) -> str:
        label = option.label or option.value
        if len(label) > ELICITATION_OPTION_LABEL_CHARS:
            label = f"{label[: ELICITATION_OPTION_LABEL_CHARS - 1].rstrip()}…"
        if question.multiple and selected:
            return f"{ELICITATION_SELECTED_PREFIX}{label}"
        return label

    def _record_elicitation_tap(self, *, form: ElicitationRequest, token: str) -> bool:
        """Apply one tap to the pending selections, or report that it names no offered option."""
        question_part, _, option_part = token.partition(".")
        if not question_part.isdigit() or not option_part.isdigit():
            return False
        if not 0 <= int(question_part) < len(form.questions):
            return False
        question = form.questions[int(question_part)]
        if not 0 <= int(option_part) < len(question.options):
            return False
        selected = int(option_part)
        choices = self._elicitation_selections.setdefault(form.request_id, {})
        current = choices.get(question.field, [])
        if question.multiple:
            if selected in current:
                current.remove(selected)
            else:
                current.append(selected)
        else:
            current = [selected]
        choices[question.field] = current
        return True

    def _elicitation_answers(
        self,
        form: ElicitationRequest,
        *,
        final: bool = False,
    ) -> Mapping[str, tuple[str, ...]] | None:
        """The tap answers, or None while the form still needs a choice or an explicit submit."""
        choices = self._elicitation_selections.get(form.request_id, {})
        answers: dict[str, tuple[str, ...]] = {}
        for question in form.questions:
            selected = choices.get(question.field, ())
            if not selected or (question.multiple and not final):
                return None
            answers[question.field] = tuple(question.options[index].value for index in selected)
        return answers

    async def _resolve_elicitation(
        self,
        form: ElicitationRequest,
        *,
        answers: Mapping[str, tuple[str, ...]] | None,
        query: CallbackQuery,
        toast: str,
    ) -> None:
        accepted = await self._agent_service.respond_elicitation_request(
            chat_id=form.chat_id,
            request_id=form.request_id,
            answers=answers,
        )
        self._forget_elicitation(form.request_id)
        if not accepted:
            with bind_log_context(chat_id=form.chat_id):
                logger.warning("Elicitation response expired: request_id=%s", form.request_id)
            await query.answer("This question is no longer waiting for an answer.")
            return
        await query.answer(toast)
        await self._record_elicitation_outcome(form, query=query, answers=answers)

    async def _record_elicitation_outcome(
        self,
        form: ElicitationRequest,
        *,
        query: CallbackQuery,
        answers: Mapping[str, tuple[str, ...]] | None,
    ) -> None:
        """Turn the question message into the record of the choice.

        The tapped answer is appended to the message that asked, and the same edit drops the
        keyboard — so the question keeps its answer even after the conversation moved on to a new
        bubble (B30). The appended line carries no formatting, so the original entities stay valid
        at their offsets. If Telegram refuses the text edit the keyboard is still removed, because
        a keyboard must never outlive the answer it asked for.
        """
        message = getattr(query, "message", None)
        original = getattr(message, "text", None) if message is not None else None
        line = self._elicitation_answer_line(form, answers=answers)
        edited_text = f"{original}\n{line}" if original else line
        entities = getattr(message, "entities", None) if message is not None else None
        try:
            if entities:
                await query.edit_message_text(
                    edited_text,
                    entities=list(entities),
                    api_kwargs=dict(ELICITATION_REMOVED_KEYBOARD),
                )
            else:
                await query.edit_message_text(edited_text, api_kwargs=dict(ELICITATION_REMOVED_KEYBOARD))
        except TelegramError as exc:
            logger.warning("Elicitation answer record refused, clearing the keyboard instead: %s", exc)
            await self._replace_elicitation_markup(query, None)

    @staticmethod
    def _elicitation_answer_line(
        form: ElicitationRequest,
        *,
        answers: Mapping[str, tuple[str, ...]] | None,
    ) -> str:
        """One plain-text line naming what was tapped, mapping option values back to their labels."""
        if answers is None:
            return ELICITATION_SKIP_RECORD
        numbered = len(form.questions) > 1
        parts: list[str] = []
        for index, question in enumerate(form.questions, start=1):
            chosen = answers.get(question.field, ())
            labels = [option.label or option.value for option in question.options if option.value in chosen]
            prefix = f"{index}. " if numbered else ""
            parts.append(f"{prefix}{', '.join(labels)}")
        return f"{ELICITATION_ANSWER_RECORD_PREFIX} {ELICITATION_ANSWER_JOINER.join(parts)}"

    async def on_permission_request(self, request: PermissionRequest) -> None:
        if self._app is None:
            return

        keyboard = self._permission_keyboard(request)
        message = TelegramBridge._format_permission_request_text(request.tool_title)
        await self._activity_handler(chat_id=request.chat_id).on_permission_request(
            request=request,
            message=message,
            keyboard=keyboard,
        )

    async def on_activity_event(self, chat_id: int, block: AgentActivityBlock) -> None:
        if self._app is None:
            return
        if chat_id in self._suppressed_activity_chats:
            return
        await self._activity_handler(chat_id=chat_id).on_activity_event(chat_id=chat_id, block=block)

    async def execute_scheduled_task(self, task: ScheduledTask) -> None:
        """Execute one due scheduled task in the current bot runtime."""

        if task.mode == "notify":
            await self._execute_scheduled_notification(task)
            return
        await self._execute_scheduled_prompt(task)

    async def _edit_permission_decision_message(self, query: CallbackQuery, *, decision_label: str) -> None:
        try:
            query_message = getattr(query, "message", None)
            original = getattr(query_message, "text", None) if query_message is not None else None
            base_text = original or "Permission request"
            edited_text = f"{base_text}\nDecision: {decision_label}"
            original_entities = getattr(query_message, "entities", None) if query_message is not None else None
            if original_entities:
                await query.edit_message_text(edited_text, entities=list(original_entities))
                return
            await query.edit_message_text(edited_text)
        except TelegramError:
            await query.edit_message_reply_markup(reply_markup=None)

    async def on_permission_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        query = update.callback_query
        if query is None:
            return
        try:
            if not await self._require_access(update):
                await query.answer("Access denied.")
                return

            data = query.data or ""
            parts = data.split("|", maxsplit=2)
            if len(parts) != PERMISSION_CALLBACK_PARTS:
                await query.answer("Invalid action.")
                return
            _, request_id, raw_action = parts
            if raw_action not in {"once", "always", "deny"}:
                await query.answer("Invalid action.")
                return

            chat = update.effective_chat
            chat_id = chat.id if chat is not None else None
            query_message = getattr(query, "message", None)
            if chat_id is None and query_message is not None:
                chat_id = query_message.chat.id
            if chat_id is None:
                await query.answer("Missing chat.")
                return

            with bind_log_context(chat_id=chat_id):
                logger.info("Permission callback received: %s", data)
                accepted = await self._agent_service.respond_permission_request(
                    chat_id=chat_id,
                    request_id=request_id,
                    action=raw_action,
                )
                if not accepted:
                    logger.warning("Permission callback rejected: request_id=%s chat_id=%s", request_id, chat_id)
                    await query.answer("Request expired.")
                    return

                labels = {"once": "Approved this time.", "always": "Approved for this session.", "deny": "Denied."}
                logger.info("Permission callback accepted: request_id=%s action=%s", request_id, raw_action)
                await query.answer(labels[raw_action])
                await self._edit_permission_decision_message(query, decision_label=labels[raw_action])
        except Exception:
            logger.exception("Unhandled error while processing permission callback")
            await query.answer("Permission action failed.")

    async def on_resume_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        query = update.callback_query
        if query is None:
            return
        if not await self._require_access(update):
            await query.answer("Access denied.")
            return
        selection = await self._resolve_resume_selection(update=update, query=query)
        if selection is None:
            return
        chat_id, candidate = selection

        try:
            with bind_log_context(chat_id=chat_id, session_id=candidate.session_id):
                session_id = await self._agent_service.load_session(
                    chat_id=chat_id,
                    session_id=candidate.session_id,
                    workspace=candidate.workspace,
                )
        except Exception as exc:
            with bind_log_context(chat_id=chat_id, session_id=candidate.session_id):
                logger.exception(
                    "Resume failed for chat_id=%s session_id=%s workspace=%s",
                    chat_id,
                    candidate.session_id,
                    candidate.workspace,
                )
            await query.answer("Failed to resume.")
            await self._reply(update, f"Failed to resume session `{candidate.session_id}`: {exc}")
            return

        await query.answer("Session resumed.")
        selected_message = f"Resumed session: {session_id}\nWorkspace: {candidate.workspace}\nTitle: {candidate.title}"
        try:
            await query.edit_message_text(selected_message)
        except TelegramError:
            await query.edit_message_reply_markup(reply_markup=None)
        self._pending_resume_choices_by_chat.pop(chat_id, None)
        await self._reply(update, f"Session resumed: `{session_id}` in `{candidate.workspace}`")

    async def on_scheduled_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        query = update.callback_query
        if query is None:
            return
        if not await self._require_access(update):
            await query.answer("Access denied.")
            return
        if self._scheduled_task_store is None:
            await query.answer("Scheduled tasks are unavailable.")
            return

        parsed = self._parse_scheduled_callback(query.data or "")
        if parsed is None:
            await query.answer("Invalid action.")
            return
        action, task_id = parsed
        chat_id = self._chat_id_from_update_or_query(update=update, query=query)
        if chat_id is None:
            await query.answer("Missing chat.")
            return

        if action == "cancel_all":
            cancelled = await asyncio.to_thread(
                self._scheduled_task_store.cancel_pending_tasks_for_chat, chat_id=chat_id
            )
            await query.answer("No pending tasks." if cancelled == 0 else f"Cancelled {cancelled} task(s).")
            await self._refresh_scheduled_callback_message(query=query, chat_id=chat_id)
            return

        cancelled = await asyncio.to_thread(self._scheduled_task_store.cancel_task, chat_id=chat_id, task_id=task_id)
        await query.answer("Task cancelled." if cancelled else "Task is no longer pending.")
        await self._refresh_scheduled_callback_message(query=query, chat_id=chat_id)

    async def on_busy_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle the *Send now* inline button shown when the agent is busy."""
        del context
        query = update.callback_query
        if query is None:
            return

        if not await self._require_access(update):
            await query.answer("Access denied.")
            return

        data = query.data or ""
        parts = data.split("|", maxsplit=1)
        if len(parts) != BUSY_CALLBACK_PARTS:
            await query.answer("Invalid action.")
            return
        _, token = parts

        chat_id = self._chat_id_from_update_or_query(update=update, query=query)
        if chat_id is None:
            await query.answer("Missing chat.")
            return

        pending, already_dequeued = self._busy_prompt_for_token(chat_id=chat_id, token=token)
        if pending is None:
            await query.answer("Already sent.")
            await self._dismiss_busy_notification(chat_id=chat_id, pending=None, query=query)
            return

        await self._handle_busy_prompt_selection(
            chat_id=chat_id,
            pending=pending,
            already_dequeued=already_dequeued,
            query=query,
        )

    async def _handle_busy_prompt_selection(
        self,
        *,
        chat_id: int,
        pending: _PendingPrompt,
        already_dequeued: bool,
        query: CallbackQuery,
    ) -> None:
        if not already_dequeued:
            with bind_log_context(chat_id=chat_id, prompt_cycle_id=pending.prompt_input.cycle_id):
                try:
                    cancelled = await self._agent_service.cancel(chat_id=chat_id)
                except Exception:
                    logger.exception("Unhandled error while cancelling prompt in busy callback")
                    await self._dismiss_busy_notification(chat_id=chat_id, pending=pending, query=query)
                    await query.answer("Cancel failed.")
                    return
                if not cancelled:
                    await query.answer(BUSY_STILL_QUEUED_TEXT)
                    return
                await query.answer(BUSY_SENT_TEXT)
                await self._update_busy_notification(
                    chat_id=chat_id,
                    pending=pending,
                    query=query,
                    text=BUSY_SENT_TEXT,
                )
                pending.notify_msg_id = None
            return

        await query.answer(BUSY_SENT_TEXT)
        await self._update_busy_notification(
            chat_id=chat_id,
            pending=pending,
            query=query,
            text=BUSY_SENT_TEXT,
        )
        pending.notify_msg_id = None

    async def _resolve_resume_selection(
        self,
        *,
        update: Update,
        query: CallbackQuery,
    ) -> tuple[int, ResumableSession] | None:
        index = self._resume_index(query.data or "")
        if index is None:
            await query.answer("Invalid selection.")
            return None

        chat_id = self._chat_id_from_update_or_query(update=update, query=query)
        if chat_id is None:
            await query.answer("Missing chat.")
            return None

        candidates = self._pending_resume_choices_by_chat.get(chat_id)
        if candidates is None:
            await query.answer("Selection expired.")
            return None
        if index < 0 or index >= len(candidates):
            await query.answer("Invalid selection.")
            return None

        return chat_id, candidates[index]

    async def _resume_candidate(
        self,
        *,
        update: Update,
        chat_id: int,
        candidate: ResumableSession,
        success_label: str = "Session resumed",
        include_restart_notice: bool = False,
    ) -> bool:
        try:
            session_id = await self._agent_service.load_session(
                chat_id=chat_id,
                session_id=candidate.session_id,
                workspace=candidate.workspace,
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"Failed to resume session `{candidate.session_id}`: {exc}")
            return False
        response = f"{success_label}: `{session_id}` in `{candidate.workspace}`"
        if include_restart_notice:
            response = f"Restart requested. Re-launching process...\n{response}"
        await self._reply(update, response)
        return True

    async def on_agent_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward a slash command the agent advertised straight into the normal prompt pipeline."""
        await self._record_group_message(update)

        if not await self._require_access(update):
            return

        if await self._silently_shelve_group_message(update, context):
            return

        message = update.message
        text = "" if message is None else (message.text or message.caption or "")
        match = _AGENT_COMMAND_TOKEN_RE.match(text)
        if match is None:
            return
        name = match.group(1).lower()

        bot_command = self._bot_commands.get(name)
        if bot_command is not None:
            # Telegram only builds a `bot_command` entity for message text, so a bot command written
            # in a media caption never reaches its CommandHandler; dispatch it here instead of losing it.
            await bot_command(update, context)
            return

        chat_id = self._chat_id(update)
        commands = self._advertised_commands(chat_id=chat_id)
        if not any(_command_name(command.name) == name for command in commands):
            await self._reply(update, f"/{name} is not a command this agent advertises. See /cmds.")
            return

        forwarded = _without_own_mention(text, match, _bot_handle(context))
        logger.info("Forwarding agent command on chat_id=%s text=%s", chat_id, log_text_preview(forwarded))
        await self.on_message(update, context, prompt_text=forwarded)

    async def on_agent_command_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Run the agent slash command behind a tapped `/cmds` button, or show the descriptions."""
        query = update.callback_query
        if query is None:
            return
        if not await self._require_access(update):
            await query.answer("Access denied.")
            return
        chat_id = self._chat_id_from_update_or_query(update=update, query=query)
        if chat_id is None:
            await query.answer("Missing chat.")
            return

        commands = self._advertised_commands(chat_id=chat_id)
        data = query.data or ""
        if data == AGENT_COMMAND_KEYBOARD_INFO_CALLBACK_DATA:
            await self._answer_commands_listing(update=update, query=query, commands=commands)
            return
        parts = data.split("|", maxsplit=1)
        if len(parts) != AGENT_COMMAND_CALLBACK_PARTS or parts[0] != AGENT_COMMAND_CALLBACK_PREFIX:
            await query.answer("Invalid action.")
            return
        name = parts[1]
        if not any(_command_name(command.name) == name for command in commands):
            # A keyboard printed before /new or /stop can outlive the session it came from.
            await query.answer("This agent no longer advertises that command. Check /cmds.")
            return
        await query.answer(f"Running /{name}.")
        logger.info("Forwarding tapped agent command on chat_id=%s text=/%s", chat_id, name)
        await self.on_message(update, context, prompt_text=f"/{name}")

    async def _answer_commands_listing(
        self, *, update: Update, query: CallbackQuery, commands: tuple[AgentCommand, ...]
    ) -> None:
        """Send the plain description listing that the keyboard's info button asks for."""
        if not commands:
            await query.answer("No commands advertised any more.")
            return
        await query.answer("Showing descriptions.")
        await self._reply(update, self._commands_listing(commands))

    async def on_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        prompt_text: str | None = None,
    ) -> None:
        await self._record_group_message(update)

        if not await self._require_access(update):
            return

        if await self._silently_shelve_group_message(update, context):
            return

        prompt_input = await self._prompt_input(update=update, context=context, text_override=prompt_text)
        if prompt_input is None:
            return

        if not await self._ensure_session_for_chat(update=update, chat_id=prompt_input.chat_id):
            return

        chat_id = prompt_input.chat_id
        lock = self._chat_prompt_lock(chat_id)
        with bind_log_context(chat_id=chat_id, prompt_cycle_id=prompt_input.cycle_id):
            logger.info("Prompt received: %s", log_text_preview(prompt_input.text))

        if lock.locked():
            await self._set_status_reaction(chat_id=chat_id, update=update, status=PromptStatus.QUEUED)
            await self._queue_busy_prompt(chat_id=chat_id, prompt_input=prompt_input, update=update)
            return

        async with lock:
            await self._drain_prompt_queue(
                chat_id=chat_id,
                update=update,
                context=context,
                prompt_input=prompt_input,
            )

    async def _set_status_reaction(self, *, chat_id: int, update: Update, status: PromptStatus) -> None:
        """Mark the prompt message with the bot's own lifecycle reaction.

        Reactions decorate the reply rather than carry it, so a headless bridge or a
        message without an id is skipped instead of raising.
        """
        if self._app is None:
            return
        message_id = getattr(self._anchor_message(update), "message_id", None)
        if message_id is None:
            return
        await self._status_reactions.set(bot=self._app.bot, chat_id=chat_id, message_id=message_id, status=status)

    async def _drain_prompt_queue(
        self,
        *,
        chat_id: int,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt_input: _PromptInput,
    ) -> None:
        current_input = prompt_input
        current_update = update

        while True:
            with bind_log_context(chat_id=chat_id, prompt_cycle_id=current_input.cycle_id):
                logger.info("Prompt cycle started")
                await self._set_status_reaction(chat_id=chat_id, update=current_update, status=PromptStatus.THINKING)
                reply = await self._request_reply(
                    update=current_update,
                    context=context,
                    prompt_input=current_input,
                )

            # Pop any pending prompt before sending the reply. A concurrent
            # on_message for this chat cannot interfere here because the
            # per-chat lock is still held; new messages observe lock.locked()
            # and take the queue path.
            pending = self._pending_prompts_by_chat.pop(chat_id, None)
            if pending is not None:
                self._dequeued_prompts_by_chat[chat_id] = pending
            else:
                self._dequeued_prompts_by_chat.pop(chat_id, None)

            try:
                if reply is not None:
                    await self._dispatch_reply(chat_id=chat_id, update=current_update, reply=reply)
                    with bind_log_context(chat_id=chat_id, prompt_cycle_id=current_input.cycle_id):
                        logger.info("Prompt cycle completed")
                await self._set_status_reaction(
                    chat_id=chat_id,
                    update=current_update,
                    status=PromptStatus.DONE if reply is not None else PromptStatus.ERROR,
                )
                await self._activity_handler(chat_id=chat_id).clear_chat_state(chat_id=chat_id)

                if pending is None:
                    return

                await self._clear_busy_button(pending)
            finally:
                self._dequeued_prompts_by_chat.pop(chat_id, None)

            with bind_log_context(chat_id=chat_id, prompt_cycle_id=pending.prompt_input.cycle_id):
                logger.info("Dequeued pending prompt cycle")
            current_input = pending.prompt_input
            current_update = pending.update

    async def _dispatch_reply(self, *, chat_id: int, update: Update, reply: AgentReply) -> None:
        if self._app is None:
            workspace = self._activity_workspace(chat_id=chat_id)
            for block in reply.activity_blocks:
                await self._reply_activity_block(update, block, workspace=workspace)
        await self._send_attachments(update, reply)
        if reply.text.strip():
            anchor_message_id = await self._activity_handler(chat_id=chat_id).finalize_reply(
                chat_id=chat_id,
                update=update,
                text=reply.text,
            )
            if anchor_message_id is None:
                sent_message = await self._reply_agent(update, reply.text)
                anchor_message_id = None if sent_message is None else sent_message.message_id
            if anchor_message_id is not None:
                await self._bind_pending_scheduled_tasks(chat_id=chat_id, anchor_message_id=anchor_message_id)
        else:
            await self._activity_handler(chat_id=chat_id).handle_empty_reply(chat_id=chat_id)

    async def _finalize_compact_reply(self, *, chat_id: int, update: Update, text: str) -> int | None:
        """Replace the compact in-progress status message with the final reply.

        Edits the status message in place when possible. Falls back to deleting it
        and delivering the reply as a new message when editing is not possible
        (e.g. reply is too long, Telegram API error).
        """
        app = self._app
        status_msg_id = self._compact_status_msg_id.pop(chat_id, None)
        self._stop_compact_status(chat_id)
        self._compact_status_label.pop(chat_id, None)
        if status_msg_id is not None and app is not None:
            success = await TelegramBridge._edit_markdown_in_chat(
                bot=app.bot, chat_id=chat_id, message_id=status_msg_id, text=text
            )
            if success:
                return status_msg_id
            with suppress(TelegramError):
                await app.bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
        sent_message = await self._reply_agent(update, text)
        return None if sent_message is None else sent_message.message_id

    async def _clear_compact_status(self, chat_id: int) -> None:
        """Delete the in-progress compact status message if one exists."""
        app = self._app
        status_msg_id = self._compact_status_msg_id.pop(chat_id, None)
        self._stop_compact_status(chat_id)
        self._compact_status_label.pop(chat_id, None)
        if status_msg_id is None or app is None:
            return
        with suppress(TelegramError):
            await app.bot.delete_message(chat_id=chat_id, message_id=status_msg_id)

    def _close_compact_status_bubble(self, chat_id: int) -> None:
        """End the compact status bubble without deleting it, so the next one opens fresh.

        A question the user answers marks a turn in the conversation: the bubble that was showing
        the pre-question work stays as history, and later activity and the final reply get their
        own bubble instead of overwriting it.
        """
        self._compact_status_msg_id.pop(chat_id, None)
        self._stop_compact_status(chat_id)
        self._compact_status_label.pop(chat_id, None)

    def _stop_compact_status(self, chat_id: int) -> None:
        task = self._compact_status_tasks.pop(chat_id, None)
        if task is not None:
            task.cancel()
        self._compact_status_displayed.pop(chat_id, None)

    async def _set_activity_mode(self, *, chat_id: int, mode: ActivityMode) -> None:
        await self._clear_activity_mode_state(chat_id=chat_id)
        self._activity_mode_by_chat[chat_id] = mode

    async def _clear_activity_mode_state(self, *, chat_id: int) -> None:
        for handler in self._activity_handlers.values():
            await handler.clear_chat_state(chat_id=chat_id)

    def _activity_mode(self, *, chat_id: int) -> ActivityMode:
        return self._activity_mode_by_chat.get(chat_id, self._config.activity_mode)

    def _activity_handler(self, *, chat_id: int) -> _ActivityModeHandler:
        return self._activity_handlers[self._activity_mode(chat_id=chat_id)]

    def _ensure_compact_status_refresher(self, *, chat_id: int) -> None:
        task = self._compact_status_tasks.get(chat_id)
        if task is not None and not task.done():
            return
        self._compact_status_tasks[chat_id] = asyncio.create_task(self._refresh_compact_status(chat_id=chat_id))

    async def _refresh_compact_status(self, *, chat_id: int) -> None:
        """Repaint the compact status message on a Telegram-safe cadence.

        Activity events can arrive several times per second, while Telegram allows only about 20
        messages a minute in a group, so each wait period applies at most one edit and a label
        that has not changed is skipped entirely.
        """
        app = self._app
        if app is None:
            return
        try:
            while True:
                await asyncio.sleep(COMPACT_STATUS_TICK_SECONDS)
                base_text = self._compact_status_label.get(chat_id)
                message_id = self._compact_status_msg_id.get(chat_id)
                if base_text is None or message_id is None:
                    return
                if base_text == self._compact_status_displayed.get(chat_id):
                    continue
                try:
                    await app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=base_text)
                except BadRequest as exc:
                    logger.warning("Compact status message on chat_id=%s disappeared: %s", chat_id, exc)
                    self._compact_status_msg_id.pop(chat_id, None)
                    self._compact_status_displayed.pop(chat_id, None)
                    return
                except TelegramError as exc:
                    logger.warning(
                        "Compact status edit failed on chat_id=%s message_id=%s: %s",
                        chat_id,
                        message_id,
                        exc,
                    )
                else:
                    self._compact_status_displayed[chat_id] = base_text
                    logger.info(
                        "Compact status updated in place on chat_id=%s message_id=%s text=%r",
                        chat_id,
                        message_id,
                        base_text,
                    )
        finally:
            if self._compact_status_tasks.get(chat_id) is asyncio.current_task():
                self._compact_status_tasks.pop(chat_id, None)

    async def _start_implicit_session(self, *, update: Update, chat_id: int) -> bool:
        workspace = self._config.default_workspace
        started = await self._start_session(
            update=update,
            chat_id=chat_id,
            workspace=workspace,
            invalid_workspace_label="Invalid default workspace",
        )
        return started is not None

    async def _prompt_input(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text_override: str | None = None,
    ) -> _PromptInput | None:
        message = self._anchor_message(update)
        if message is None:
            return None
        chat_id = self._chat_id(update)
        text = text_override if text_override is not None else (message.text or message.caption or "")
        images = await self._extract_prompt_images(message=message, context=context)
        files = await self._extract_prompt_files(message=message, context=context)
        if not text and not images and not files:
            return None
        if getattr(update.effective_chat, "type", None) in GROUP_CHAT_TYPES:
            text = _with_group_context(text, _sender_label(update.effective_user))
        if self._config.group_media_queue:
            pending = await self._agent_service.take_pending_media(chat_id=chat_id)
            text = _with_pending_media(text, pending)
        return _PromptInput(chat_id=chat_id, text=text, images=images, files=files)

    async def _ensure_session_for_chat(self, *, update: Update, chat_id: int) -> bool:
        if self._agent_service.get_workspace(chat_id=chat_id) is not None:
            self._drop_implicit_start_lock(chat_id=chat_id)
            return True
        lock = self._implicit_start_lock(chat_id)
        async with lock:
            if self._agent_service.get_workspace(chat_id=chat_id) is not None:
                self._drop_implicit_start_lock(chat_id=chat_id, expected_lock=lock)
                return True
            started = await self._start_implicit_session(update=update, chat_id=chat_id)
            if self._agent_service.get_workspace(chat_id=chat_id) is not None:
                self._drop_implicit_start_lock(chat_id=chat_id, expected_lock=lock)
            return started

    async def _start_session(
        self,
        *,
        update: Update,
        chat_id: int,
        workspace: Path,
        invalid_workspace_label: str,
    ) -> tuple[str, Path] | None:
        try:
            with bind_log_context(chat_id=chat_id):
                session_id = await self._agent_service.new_session(chat_id=chat_id, workspace=workspace)
        except ValueError as exc:
            message = str(exc) or str(workspace)
            await self._reply(update, f"{invalid_workspace_label}: {message}")
            return None
        except RuntimeError:
            await self._reply(update, "Failed to start session: agent process did not expose stdio pipes.")
            return None
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"Failed to start session: {exc}")
            return None
        active_workspace = self._agent_service.get_workspace(chat_id=chat_id) or workspace
        return session_id, active_workspace

    def _active_session_context(self, *, chat_id: int) -> tuple[str, Path] | None:
        context_provider = getattr(self._agent_service, "get_active_session_context", None)
        if not callable(context_provider):
            return None
        context = context_provider(chat_id=chat_id)
        if context is None:
            return None
        session_id, workspace = context
        return session_id, workspace

    @staticmethod
    def _format_restart_response(*, session_id: str, workspace: Path) -> str:
        return f"Restart requested. Re-launching process...\nSession restarted: `{session_id}` in `{workspace}`"

    def _implicit_start_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._implicit_start_locks_by_chat.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._implicit_start_locks_by_chat[chat_id] = lock
        return lock

    def _drop_implicit_start_lock(self, *, chat_id: int, expected_lock: asyncio.Lock | None = None) -> None:
        current_lock = self._implicit_start_locks_by_chat.get(chat_id)
        if current_lock is None:
            return
        if expected_lock is not None and current_lock is not expected_lock:
            return
        self._implicit_start_locks_by_chat.pop(chat_id, None)

    def _chat_prompt_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_prompt_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_prompt_locks[chat_id] = lock
        return lock

    async def _queue_busy_prompt(
        self,
        *,
        chat_id: int,
        prompt_input: _PromptInput,
        update: Update,
    ) -> None:
        """Queue a prompt while the agent is busy and show a *Send now* inline button."""
        with bind_log_context(chat_id=chat_id, prompt_cycle_id=prompt_input.cycle_id):
            old_pending = self._pending_prompts_by_chat.get(chat_id)
            if old_pending is not None and old_pending.notify_msg_id is not None and self._app is not None:
                with suppress(TelegramError):
                    await self._app.bot.edit_message_reply_markup(
                        chat_id=chat_id,
                        message_id=old_pending.notify_msg_id,
                        reply_markup=None,
                    )

            token = str(uuid4())
            pending = _PendingPrompt(prompt_input=prompt_input, update=update, token=token)
            self._pending_prompts_by_chat[chat_id] = pending
            logger.info("Queued prompt cycle while chat lock is busy")

            if self._app is not None:
                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(text="Send now", callback_data=f"{BUSY_CALLBACK_PREFIX}|{token}")]]
                )
                send_kwargs: dict[str, object] = {
                    "chat_id": chat_id,
                    "text": BUSY_QUEUED_TEXT,
                    "reply_markup": keyboard,
                }
                queued_message = self._anchor_message(update)
                if queued_message is not None and isinstance(queued_message.message_id, int):
                    send_kwargs["reply_to_message_id"] = queued_message.message_id
                    send_kwargs["allow_sending_without_reply"] = True
                try:
                    notify_msg = await self._app.bot.send_message(**send_kwargs)
                    pending.notify_msg_id = getattr(notify_msg, "message_id", None)
                except TelegramError:
                    logger.exception("Failed to send busy notification for chat_id=%s", chat_id)

    async def _clear_busy_button(self, pending: _PendingPrompt | None) -> None:
        """Update the busy notification to `BUSY_SENT_TEXT`.

        Also remove the *Send now* button when the queued prompt is about to
        be processed.
        """
        if pending is None or pending.notify_msg_id is None or self._app is None:
            return
        with suppress(TelegramError):
            await self._app.bot.edit_message_text(
                chat_id=pending.prompt_input.chat_id,
                message_id=pending.notify_msg_id,
                text=BUSY_SENT_TEXT,
                reply_markup=None,
            )
            return
        with suppress(TelegramError):
            await self._app.bot.edit_message_reply_markup(
                chat_id=pending.prompt_input.chat_id,
                message_id=pending.notify_msg_id,
                reply_markup=None,
            )

    async def _update_busy_notification(
        self,
        *,
        chat_id: int,
        pending: _PendingPrompt,
        query: CallbackQuery,
        text: str,
    ) -> None:
        if pending.notify_msg_id is not None and self._app is not None:
            with suppress(TelegramError):
                await self._app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=pending.notify_msg_id,
                    text=text,
                    reply_markup=None,
                )
                return
        with suppress(TelegramError):
            await query.edit_message_text(text, reply_markup=None)
            return
        await self._dismiss_busy_notification(chat_id=chat_id, pending=pending, query=query)

    async def _dismiss_busy_notification(
        self,
        *,
        chat_id: int,
        pending: _PendingPrompt | None,
        query: CallbackQuery,
    ) -> None:
        notify_msg_id = None if pending is None else pending.notify_msg_id
        if notify_msg_id is not None and self._app is not None:
            with suppress(TelegramError):
                await self._app.bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=notify_msg_id,
                    reply_markup=None,
                )
                return
        with suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)

    def _busy_prompt_for_token(self, *, chat_id: int, token: str) -> tuple[_PendingPrompt | None, bool]:
        pending = self._pending_prompts_by_chat.get(chat_id)
        if pending is not None and pending.token == token:
            return pending, False
        dequeued = self._dequeued_prompts_by_chat.get(chat_id)
        if dequeued is not None and dequeued.token == token:
            return dequeued, True
        return None, False

    async def _request_reply(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt_input: _PromptInput,
    ) -> AgentReply | None:
        session_context = self._active_session_context(chat_id=prompt_input.chat_id)
        session_id = None if session_context is None else session_context[0]
        with bind_log_context(
            chat_id=prompt_input.chat_id,
            prompt_cycle_id=prompt_input.cycle_id,
            session_id=session_id,
        ):
            await context.bot.send_chat_action(chat_id=prompt_input.chat_id, action=ChatAction.TYPING)
        source_message_id = getattr(self._anchor_message(update), "message_id", None)
        try:
            await self._set_prompt_message_context(
                session_id=session_id,
                message_id=source_message_id,
            )
            with bind_log_context(
                chat_id=prompt_input.chat_id,
                prompt_cycle_id=prompt_input.cycle_id,
                session_id=session_id,
            ):
                reply = await self._agent_service.prompt(
                    chat_id=prompt_input.chat_id,
                    text=prompt_input.text,
                    images=prompt_input.images,
                    files=prompt_input.files,
                )
        except AgentOutputLimitExceededError:
            await self._reply(
                update,
                "Agent output exceeded ACP stdio limit. Restart with a higher `--acp-stdio-limit` "
                "(or `ACP_STDIO_LIMIT`).",
            )
            return None
        except AgentConnectionLostError:
            await self._reply(update, "The agent died and could not be restarted. Try `/new` to start a session.")
            return None
        finally:
            await self._set_prompt_message_context(session_id=session_id, message_id=None)
        if reply is not None:
            with bind_log_context(
                chat_id=prompt_input.chat_id,
                prompt_cycle_id=prompt_input.cycle_id,
                session_id=session_id,
            ):
                logger.info("Reply sent: %s", log_text_preview(reply.text))
            return reply
        await self._reply(update, "No active session. Send a message again or use /new [workspace].")
        return None

    async def _set_prompt_message_context(self, *, session_id: str | None, message_id: int | None) -> None:
        if session_id is None:
            return
        prompt_context_setter = getattr(self._agent_service, "set_prompt_message_context", None)
        if not callable(prompt_context_setter):
            return
        try:
            await prompt_context_setter(session_id=session_id, message_id=message_id)
        except Exception:
            logger.exception(
                "Failed to persist prompt message context: session_id=%s message_id=%s",
                session_id,
                message_id,
            )

    async def _extract_prompt_images(
        self,
        *,
        message: Message,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> tuple[PromptImage, ...]:
        images: list[PromptImage] = []
        if message.photo:
            photo = message.photo[-1]
            tg_file = await context.bot.get_file(photo.file_id)
            raw = bytes(await tg_file.download_as_bytearray())
            images.append(PromptImage(data_base64=base64.b64encode(raw).decode("ascii"), mime_type="image/jpeg"))

        document = message.document
        if document is not None and document.mime_type and document.mime_type.startswith("image/"):
            tg_file = await context.bot.get_file(document.file_id)
            raw = bytes(await tg_file.download_as_bytearray())
            images.append(PromptImage(data_base64=base64.b64encode(raw).decode("ascii"), mime_type=document.mime_type))

        return tuple(images)

    async def _extract_prompt_files(
        self,
        *,
        message: Message,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> tuple[PromptFile, ...]:
        document = message.document
        if document is None:
            return ()
        if document.mime_type and document.mime_type.startswith("image/"):
            return ()

        tg_file = await context.bot.get_file(document.file_id)
        raw = bytes(await tg_file.download_as_bytearray())
        name = document.file_name or "attachment.bin"
        try:
            text_content = raw.decode("utf-8")
            return (PromptFile(name=name, mime_type=document.mime_type, text_content=text_content),)
        except UnicodeDecodeError:
            return (
                PromptFile(
                    name=name,
                    mime_type=document.mime_type,
                    data_base64=base64.b64encode(raw).decode("ascii"),
                ),
            )

    async def _send_attachments(self, update: Update, reply: AgentReply) -> None:
        for image in reply.images:
            await self._send_image(update, image)
        for file_payload in reply.files:
            await self._send_file(update, file_payload)

    @staticmethod
    def _permission_keyboard(request: PermissionRequest) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        buttons: list[InlineKeyboardButton] = []
        for action in request.available_actions:
            label = {"always": "Always", "once": "This time", "deny": "Deny"}[action]
            callback_data = f"{PERMISSION_CALLBACK_PREFIX}|{request.request_id}|{action}"
            buttons.append(InlineKeyboardButton(text=label, callback_data=callback_data))
        rows.append(buttons)
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _resume_keyboard(*, candidates: tuple[ResumableSession, ...]) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for index, candidate in enumerate(candidates[:RESUME_KEYBOARD_MAX_ROWS]):
            title = candidate.title.strip() or candidate.session_id
            label = f"{index}. {title}"[:48]
            callback_data = f"{RESUME_CALLBACK_PREFIX}|{index}"
            rows.append([InlineKeyboardButton(text=label, callback_data=callback_data)])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _scheduled_keyboard(*, tasks: list[ScheduledTask]) -> InlineKeyboardMarkup | None:
        pending_tasks = [
            (index, task)
            for index, task in enumerate(tasks[:SCHEDULED_KEYBOARD_MAX_ROWS], start=1)
            if task.status == "pending"
        ]
        if not pending_tasks:
            return None
        show_cancel_all = len(pending_tasks) > 1
        visible_pending_tasks = (
            pending_tasks[: SCHEDULED_KEYBOARD_MAX_ROWS - 1]
            if show_cancel_all
            else pending_tasks[:SCHEDULED_KEYBOARD_MAX_ROWS]
        )

        rows = [
            [
                InlineKeyboardButton(
                    text=f"Cancel {index}",
                    callback_data=f"{SCHEDULED_CALLBACK_PREFIX}|cancel|{task.id}",
                )
            ]
            for index, task in visible_pending_tasks
        ]
        if show_cancel_all:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Cancel all pending",
                        callback_data=f"{SCHEDULED_CALLBACK_PREFIX}|cancel_all|_",
                    )
                ]
            )
        return InlineKeyboardMarkup(rows)

    async def _record_group_message(self, update: Update) -> None:
        """Park one group message in the chat's capped history, ahead of every gate.

        Recording runs before the allowlist and the mention rule, so the history the agent reads
        back is the room as it happened rather than only the messages this bot chose to answer.
        An edit rewrites the entry of the message it changed, so the log holds the last version of
        anything the room saw once, which is how a compact answer that replaces its own status
        bubble still reaches the agent.
        """
        cap = self._config.group_history_size
        if cap <= 0:
            return
        chat = update.effective_chat
        message = self._anchor_message(update)
        if chat is None or message is None or getattr(chat, "type", None) not in GROUP_CHAT_TYPES:
            return
        text = message.text or message.caption or ""
        if not text.strip():
            return

        user = update.effective_user
        await self._agent_service.record_group_message(
            chat_id=chat.id,
            message_id=message.message_id,
            sender_id=getattr(user, "id", None),
            sender_name=_sender_label(user).removeprefix("@") or None,
            text=text,
            max_entries=cap,
        )

    async def _require_access(self, update: Update) -> bool:
        allowed_ids = self._config.allowed_user_ids
        allowed_usernames = self._config.allowed_usernames
        if not allowed_ids and not allowed_usernames:
            return True

        user_id = update.effective_user.id if update.effective_user else None
        if user_id in allowed_ids:
            return True
        username_raw = getattr(update.effective_user, "username", None)
        if isinstance(username_raw, str):
            username = username_raw.lstrip("@").strip().lower()
            if username in allowed_usernames:
                return True

        await self._reply(update, "Access denied for this bot.")
        chat = update.effective_chat
        logger.info(
            "Blocked sender user_id=%s username=%s in chat=%s — add it to the allowlist to grant access",
            user_id,
            username_raw,
            chat.id if chat is not None else None,
        )
        return False

    def _ignores_unmentioned_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """True when a group message must be skipped because it does not target the bot.

        Implements {py:attr}`BotConfig.group_require_mention`: private chats always answer,
        group chats only when the bot is @mentioned (or a person replies to the bot).

        The anchor message is read rather than `update.message`, because a compact-mode answer
        arrives as an edit of the status bubble and would otherwise look like an unaddressed post.

        A reply only counts as an address when a person sent it. Every answer this bot posts is a
        reply to the message that triggered it, so honouring the quote from a bot would let two
        agents sustain a conversation on the quote alone — which is how a busy notification kept
        redrawing the peer in a shared group.
        """
        chat = update.effective_chat
        if getattr(chat, "type", None) not in GROUP_CHAT_TYPES:
            return False

        chat_id = chat.id if chat is not None else None
        sender_is_bot = bool(getattr(update.effective_user, "is_bot", False))
        if not sender_is_bot and chat_id is not None:
            self._bot_turns_by_chat.pop(chat_id, None)
            self._bot_budget_notified.discard(chat_id)

        message = self._anchor_message(update)
        handle = _bot_handle(context)
        addressed = False
        if message is not None and handle:
            addressed = _mentions_handle(message, handle)
            if not addressed and not sender_is_bot:
                author = getattr(message.reply_to_message, "from_user", None)
                replied_username = getattr(author, "username", None) or ""
                addressed = replied_username.lstrip("@").strip().lower() == handle

        if addressed and sender_is_bot and chat_id is not None and not self._reserve_bot_turn(chat_id):
            return True
        if addressed:
            return False

        logger.info("Group message in chat=%s ignored: no @%s mention", chat_id, handle)
        return True

    def _reserve_bot_turn(self, chat_id: int) -> bool:
        """Spend one turn of the bot-to-bot budget, refusing the exchange once it is spent.

        Two agents that address each other by name, as {py:data}`GROUP_MENTION_REMINDER` tells them
        to, hand the conversation back and forth with no human in the loop. The budget bounds that
        exchange without forbidding it, and the next person's message refills it.
        """
        used = self._bot_turns_by_chat.get(chat_id, 0)
        if used >= GROUP_BOT_TURN_BUDGET:
            logger.info("Group message in chat=%s ignored: bot-to-bot turn budget spent", chat_id)
            return False
        self._bot_turns_by_chat[chat_id] = used + 1
        return True

    async def _announce_bot_turn_hold(self, update: Update) -> None:
        """Say once why the exchange stopped, since two bots going quiet looks like a hang."""
        message = update.message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        if self._bot_turns_by_chat.get(chat.id, 0) < GROUP_BOT_TURN_BUDGET:
            return
        if chat.id in self._bot_budget_notified:
            return
        self._bot_budget_notified.add(chat.id)
        await self._reply(update, GROUP_BUDGET_HOLD_TEXT)

    async def _silently_shelve_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """True when the mention rule hides this message, which is then queued or dropped without a reply.

        Any handler that may answer a plain message has to consult this before saying anything, so an
        unaddressed group post never draws the bot into the conversation.
        """
        # Tapping one of the bot's own buttons is an explicit address, so the mention rule never hides it.
        if getattr(update, "callback_query", None) is not None:
            return False
        if not self._config.group_require_mention or not self._ignores_unmentioned_group_message(update, context):
            return False
        if self._config.group_media_queue:
            await self._queue_unclaimed_group_media(update=update)
        await self._announce_bot_turn_hold(update)
        return True

    async def _queue_unclaimed_group_media(self, *, update: Update) -> None:
        """Keep a reference to group media that the mention gate drops, so it is not lost.

        Only the Telegram `file_id` is stored; bytes are fetched later by the agent through
        {py:func}`telegram_acp_bot.mcp.tools.attachments.telegram_fetch_attachment`.
        """
        chat = update.effective_chat
        message = update.message
        if chat is None or message is None:
            return

        for file_id, mime_type in _media_references(message):
            await self._agent_service.record_pending_media(
                chat_id=chat.id,
                message_id=message.message_id,
                file_id=file_id,
                mime_type=mime_type,
            )
            logger.info("Queued unclaimed media file_id=%s from chat=%s", file_id[:12], chat.id)

    @staticmethod
    def _chat_id(update: Update) -> int:
        chat = update.effective_chat
        if chat is None:
            raise ChatRequiredError
        return chat.id

    @staticmethod
    def _anchor_message(update: Update) -> Message | None:
        """Message a turn is anchored to: the inbound one, or the tapped one behind an inline button.

        Reply delivery, status reactions and media extraction all hang off this message, so a prompt
        that arrives as a callback query keeps working instead of vanishing on a missing `update.message`.
        """
        if update.message is not None:
            return update.message
        return cast(Message | None, getattr(update, "effective_message", None))

    @staticmethod
    def _chat_id_from_update_or_query(*, update: Update, query: object) -> int | None:
        chat = update.effective_chat
        chat_id = chat.id if chat is not None else None
        if chat_id is not None:
            return chat_id
        query_message = getattr(query, "message", None)
        if query_message is None:
            return None
        return query_message.chat.id

    @staticmethod
    def _parse_scheduled_callback(data: str) -> tuple[str, str] | None:
        parts = data.split("|", maxsplit=2)
        if len(parts) != SCHEDULED_CALLBACK_PARTS or parts[0] != SCHEDULED_CALLBACK_PREFIX:
            return None
        action, value = parts[1], parts[2]
        if action == "cancel" and value:
            return action, value
        if action == "cancel_all" and value == "_":
            return action, value
        return None

    @staticmethod
    def _resume_index(data: str) -> int | None:
        parts = data.split("|", maxsplit=1)
        if len(parts) != RESUME_CALLBACK_PARTS:
            return None
        _, raw_index = parts
        if not raw_index.isdigit():
            return None
        return int(raw_index)

    async def _list_visible_scheduled_tasks(self, *, chat_id: int) -> list[ScheduledTask]:
        if self._scheduled_task_store is None:
            return []
        tasks = await asyncio.to_thread(
            self._scheduled_task_store.list_tasks_for_chat,
            chat_id=chat_id,
            limit=SCHEDULED_KEYBOARD_MAX_ROWS + 1,
        )
        return tasks[:SCHEDULED_KEYBOARD_MAX_ROWS]

    async def _refresh_scheduled_callback_message(self, *, query: CallbackQuery, chat_id: int) -> None:
        tasks = await self._list_visible_scheduled_tasks(chat_id=chat_id)
        text = self._format_scheduled_tasks_text(tasks=tasks)
        reply_markup = self._scheduled_keyboard(tasks=tasks)
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
        except TelegramError:
            with suppress(TelegramError):
                await query.edit_message_reply_markup(reply_markup=reply_markup)

    @staticmethod
    def _format_scheduled_tasks_text(*, tasks: list[ScheduledTask]) -> str:
        if not tasks:
            return "No pending or running scheduled tasks for this chat."

        indexed_tasks = list(enumerate(tasks, start=1))
        pending_lines = TelegramBridge._format_scheduled_task_group(
            heading="Pending",
            tasks=[item for item in indexed_tasks if item[1].status == "pending"],
        )
        running_lines = TelegramBridge._format_scheduled_task_group(
            heading="Running",
            tasks=[item for item in indexed_tasks if item[1].status == "running"],
        )

        lines = ["Scheduled tasks for this chat:"]
        if pending_lines:
            lines.extend(["", *pending_lines])
        if running_lines:
            lines.extend(["", *running_lines])
        return "\n".join(lines)

    @staticmethod
    def _format_scheduled_task_group(*, heading: str, tasks: list[tuple[int, ScheduledTask]]) -> list[str]:
        if not tasks:
            return []
        lines = [f"{heading}:"]
        for index, task in tasks:
            lines.append(f"{index}. {task.mode} | {TelegramBridge._format_scheduled_task_run_at(task)}")
            lines.append(f"  {TelegramBridge._scheduled_task_preview(task)}")
        return lines

    @staticmethod
    def _scheduled_task_preview(task: ScheduledTask) -> str:
        raw = task.prompt_text if task.mode == "prompt_agent" else task.notify_text
        text = " ".join((raw or "").split())
        if not text:
            fallback = "prompt" if task.mode == "prompt_agent" else "notification"
            return f"(empty {fallback})"
        if len(text) <= SCHEDULED_TASK_PREVIEW_MAX_CHARS:
            return text
        return f"{text[: SCHEDULED_TASK_PREVIEW_MAX_CHARS - 3].rstrip()}..."

    @staticmethod
    def _format_scheduled_task_run_at(task: ScheduledTask) -> str:
        return task.run_at.strftime("%Y-%m-%d %H:%M:%S UTC")

    def _workspace_from_args(self, args: list[str]) -> Path:
        if not args:
            return self._config.default_workspace

        candidate = Path(args[0]).expanduser()
        if candidate.is_absolute():
            return candidate
        return self._config.default_workspace / candidate

    @staticmethod
    def _context_args(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
        args = context.args
        if not args:
            return []
        return list(args)

    def _parse_resume_args(self, args: list[str]) -> _ResumeArgs | None:
        if len(args) > MAX_RESUME_ARGS:
            return None
        if not args:
            return _ResumeArgs(resume_index=None, workspace=None)
        arg = args[0]
        if arg.isdigit():
            raw_index = int(arg)
            return _ResumeArgs(resume_index=raw_index, workspace=None)
        return _ResumeArgs(resume_index=None, workspace=self._workspace_from_args([arg]))

    def _parse_restart_args(self, args: list[str]) -> _RestartArgs | None:
        if len(args) > MAX_RESTART_ARGS:
            return None
        raw_index: int | None = None
        workspace_arg: str | None = None
        for arg in args:
            if arg.isdigit():
                if raw_index is not None:
                    return None
                raw_index = int(arg)
                continue
            if workspace_arg is not None:
                return None
            workspace_arg = arg
        workspace = self._workspace_from_args([workspace_arg]) if workspace_arg is not None else None
        resume_index = raw_index
        return _RestartArgs(resume_index=resume_index, workspace=workspace)

    @staticmethod
    async def _reply(update: Update, text: str) -> Message | None:
        message = TelegramBridge._anchor_message(update)
        if message is None:
            return None
        return await TelegramBridge._reply_markdown_message(message, text=text)

    @staticmethod
    async def _reply_message(message: Message | None, text: str) -> Message | None:
        if message is None:
            return None
        return await TelegramBridge._reply_markdown_message(message, text=text)

    @staticmethod
    async def _reply_agent(update: Update, text: str) -> Message | None:
        message = TelegramBridge._anchor_message(update)
        if message is None:
            return None
        return await TelegramBridge._reply_markdown_message(message, text=text)

    @staticmethod
    def _to_telegram_entity(entity: MarkdownMessageEntity) -> MessageEntity:
        return MessageEntity(
            type=entity.type,
            offset=entity.offset,
            length=entity.length,
            url=entity.url,
            language=entity.language,
            custom_emoji_id=entity.custom_emoji_id,
        )

    @staticmethod
    async def _reply_activity_block(
        update: Update, block: AgentActivityBlock, *, workspace: Path | None = None
    ) -> None:
        message = TelegramBridge._anchor_message(update)
        if message is None:
            return

        text = TelegramBridge._format_activity_block(block, workspace=workspace)
        await TelegramBridge._reply_markdown_message(message, text=text)

    @staticmethod
    def _format_activity_block(block: AgentActivityBlock, *, workspace: Path | None = None) -> str:
        if block.kind == "reply":
            return block.text
        label = TelegramBridge._activity_label(block)
        text_parts = [f"*{label}*"]
        normalized_title = TelegramBridge._normalize_activity_title(block, workspace=workspace)
        normalized_text = TelegramBridge._normalize_activity_text(block, workspace=workspace)
        if normalized_title and normalized_text and normalized_title == normalized_text:
            normalized_title = ""
        if normalized_title:
            text_parts.append(
                TelegramBridge._render_activity_part(normalized_title, allow_basic_markdown=block.kind == "think")
            )
        if normalized_text:
            text_parts.append(
                TelegramBridge._render_activity_part(normalized_text, allow_basic_markdown=block.kind == "think")
            )
        if block.status == "failed":
            text_parts.append("_Failed_")
        return "\n\n".join(text_parts)

    @staticmethod
    def _activity_label(block: AgentActivityBlock) -> str:
        if block.kind == "reply":
            return REPLY_LABEL
        if block.kind != "search":
            return KIND_LABELS.get(block.kind, "⚙️ Tool call")
        if TelegramBridge._search_source(block) == "web":
            return SEARCH_LABEL_WEB
        return SEARCH_LABEL_NEUTRAL

    @staticmethod
    def _compact_status_hint(block: AgentActivityBlock, *, workspace: Path | None) -> str:
        """Return a one-line summary of an activity for the compact status message.

        The normalized title is often multi-line (fenced commands, absolute paths); the status
        line only has room for its first meaningful row, so collapse it, show paths relative to
        the workspace, and cap the width.
        """

        title = TelegramBridge._normalize_activity_title(block, workspace=workspace)
        for raw_line in title.splitlines():
            line = raw_line.strip().strip("`").strip()
            if not line:
                continue
            if workspace is not None and line.startswith("/"):
                root = str(Path(workspace).resolve(strict=False)).rstrip("/")
                if root and line.startswith(f"{root}/"):
                    line = line[len(root) + 1 :]
            if len(line) <= COMPACT_STATUS_HINT_CHARS:
                return line
            return f"{line[: COMPACT_STATUS_HINT_CHARS - 1].rstrip()}…"
        return ""

    @staticmethod
    def _search_source(block: AgentActivityBlock) -> str | None:
        content = f"{block.title}\n{block.text}".lower()
        if any(token in content for token in ("http://", "https://", "url:", "web search", "internet")):
            return "web"
        return None

    @staticmethod
    def _normalize_activity_title(block: AgentActivityBlock, *, workspace: Path | None = None) -> str:
        title = block.title.strip()
        if block.kind == "think":
            return ""
        if block.kind == "search":
            return TelegramBridge._normalize_search_activity_part(title, workspace=workspace)
        if block.kind == "execute" and title.startswith("Run "):
            command = title[4:].strip()
            if command:
                commands = TelegramBridge._split_execute_commands(command)
                return "\n".join(TelegramBridge._format_fenced_code(item) for item in commands)
        path_prefix = TelegramBridge._path_prefix_for_kind(block.kind)
        if path_prefix and title.startswith(path_prefix):
            return TelegramBridge._format_read_path(title[len(path_prefix) :], workspace=workspace)
        return title

    @staticmethod
    def _normalize_activity_text(block: AgentActivityBlock, *, workspace: Path | None = None) -> str:
        text = block.text.strip()
        if block.kind == "search":
            return TelegramBridge._normalize_search_activity_part(text, workspace=workspace)
        path_prefix = TelegramBridge._path_prefix_for_kind(block.kind)
        if path_prefix and text.startswith(path_prefix) and "\n" not in text:
            return TelegramBridge._format_read_path(text[len(path_prefix) :], workspace=workspace)
        return text

    @staticmethod
    def _normalize_search_activity_part(text: str, *, workspace: Path | None = None) -> str:
        stripped = text.strip()
        if not stripped:
            return ""
        if stripped.startswith("List "):
            paths = TelegramBridge._split_prefixed_items(stripped, prefix="List ")
            if not paths:
                return stripped
            seen: set[str] = set()
            rendered: list[str] = []
            for path in paths:
                normalized = TelegramBridge._format_search_path(path, workspace=workspace)
                if normalized in seen:
                    continue
                seen.add(normalized)
                rendered.append(f"List {normalized}")
            return ", ".join(rendered)
        if stripped.startswith("Search "):
            pattern = stripped[len("Search ") :].strip().replace("`", "\\`")
            if pattern:
                return f"Search `{pattern}`"
        return stripped

    @staticmethod
    def _split_prefixed_items(text: str, *, prefix: str) -> list[str]:
        if not text.startswith(prefix):
            return []
        remainder = text[len(prefix) :]
        separator = f", {prefix}"
        return [item.strip() for item in remainder.split(separator) if item.strip()]

    @staticmethod
    def _format_search_path(raw_path: str, *, workspace: Path | None) -> str:
        raw = raw_path.strip()
        if workspace is not None and raw == workspace.name:
            return TelegramBridge._format_read_path(".", workspace=workspace)
        return TelegramBridge._format_read_path(raw, workspace=workspace)

    @staticmethod
    def _escape_markdown_preserving_code(text: str, *, allow_basic_markdown: bool = False) -> str:
        escaped: list[str] = []
        in_code = False
        previous_char = ""
        for char in text:
            if char == "`":
                if previous_char != "\\":
                    in_code = not in_code
                escaped.append(char)
                previous_char = char
                continue
            if in_code:
                escaped.append(char)
                previous_char = char
                continue
            if char in {"\\", "["}:
                escaped.append(f"\\{char}")
                previous_char = char
                continue
            if not allow_basic_markdown and char in {"_", "*"}:
                escaped.append(f"\\{char}")
                previous_char = char
                continue
            escaped.append(char)
            previous_char = char
        return "".join(escaped)

    @staticmethod
    def _render_activity_part(text: str, *, allow_basic_markdown: bool = False) -> str:
        if "```" in text:
            return text
        return TelegramBridge._escape_markdown_preserving_code(text, allow_basic_markdown=allow_basic_markdown)

    @staticmethod
    def _format_read_path(raw_path: str, *, workspace: Path | None) -> str:
        raw = raw_path.strip()
        if not raw:
            return ""

        # Prefer explicit file URIs when present in tool titles/text.
        if "file://" in raw:
            parsed = urlparse(raw[raw.index("file://") :])
            if parsed.scheme == "file" and parsed.path:
                safe_uri_path = parsed.path.rstrip(")").replace("`", "\\`")
                return f"`{safe_uri_path}`"

        path = Path(raw).expanduser()
        if path.is_absolute():
            safe_abs_path = str(path.resolve(strict=False)).replace("`", "\\`")
            return f"`{safe_abs_path}`"
        base_workspace = workspace or Path.cwd()
        workspace_path = (base_workspace / path).resolve(strict=False)
        safe_workspace_path = str(workspace_path).replace("`", "\\`")
        return f"`{safe_workspace_path}`"

    @staticmethod
    def _path_prefix_for_kind(kind: str) -> str | None:
        if kind == "read":
            return "Read "
        if kind == "edit":
            return "Edit "
        return None

    @staticmethod
    def _format_permission_tool_title(tool_title: str) -> str:
        title = tool_title.strip()
        if not title:
            return ""
        if title.startswith("Run "):
            return TelegramBridge._normalize_activity_title(
                AgentActivityBlock(kind="execute", title=title, status="in_progress")
            )
        return title

    @staticmethod
    def _format_permission_request_text(tool_title: str) -> str:
        title = TelegramBridge._format_permission_tool_title(tool_title)
        message_parts = ["*⚠️ Permission required*"]
        if title:
            message_parts.append(TelegramBridge._render_activity_part(title))
        return "\n\n".join(message_parts)

    @staticmethod
    def _split_execute_commands(command: str) -> list[str]:
        if ", Run " not in command:
            return [command]
        commands = [part.strip() for part in command.split(", Run ")]
        if commands and all(commands):
            return commands
        return [command]

    @staticmethod
    def _format_fenced_code(text: str) -> str:
        max_backtick_run = 0
        current_run = 0
        for char in text:
            if char == "`":
                current_run += 1
                max_backtick_run = max(max_backtick_run, current_run)
                continue
            current_run = 0

        fence = "`" * max(3, max_backtick_run + 1)
        return f"{fence}\n{text}\n{fence}"

    @staticmethod
    async def _reply_markdown_message(message: Message, *, text: str) -> Message | None:
        first_message: Message | None = None
        try:
            for chunk_text, chunk_entities in TelegramBridge._render_markdown_chunks(text):
                if chunk_entities:
                    try:
                        sent_message = await message.reply_text(chunk_text, entities=chunk_entities)
                    except TelegramError:
                        sent_message = await message.reply_text(chunk_text)
                else:
                    sent_message = await message.reply_text(chunk_text)
                if first_message is None:
                    first_message = sent_message
        except (RuntimeError, ValueError, TypeError):
            return await message.reply_text(text)
        return first_message

    @staticmethod
    def _render_markdown_chunks(text: str) -> list[tuple[str, list[MessageEntity] | None]]:
        rendered_text, rendered_entities = convert(text)
        chunks = split_entities(rendered_text, rendered_entities, max_utf16_len=TELEGRAM_MAX_UTF16_MESSAGE_LENGTH)
        rendered_chunks: list[tuple[str, list[MessageEntity] | None]] = []
        for chunk_text, chunk_entities in chunks:
            if chunk_entities:
                rendered_chunks.append(
                    (chunk_text, [TelegramBridge._to_telegram_entity(entity) for entity in chunk_entities])
                )
                continue
            rendered_chunks.append((chunk_text, None))
        return rendered_chunks

    @staticmethod
    async def _send_rendered_chunks_to_chat(
        *,
        bot: Bot,
        chat_id: int,
        chunks: list[tuple[str, list[MessageEntity] | None]],
        reply_markup: InlineKeyboardMarkup | None = None,
        reply_to_message_id: int | None = None,
    ) -> Message | None:
        first_message: Message | None = None
        for index, (chunk_text, chunk_entities) in enumerate(chunks):
            current_reply_markup = reply_markup if index == 0 else None
            if chunk_entities:
                try:
                    sent_message = await TelegramBridge._send_chat_message(
                        bot=bot,
                        chat_id=chat_id,
                        text=chunk_text,
                        entities=chunk_entities,
                        reply_markup=current_reply_markup,
                        reply_to_message_id=reply_to_message_id,
                    )
                except TelegramError:
                    sent_message = await TelegramBridge._send_chat_message(
                        bot=bot,
                        chat_id=chat_id,
                        text=chunk_text,
                        reply_markup=current_reply_markup,
                        reply_to_message_id=reply_to_message_id,
                    )
            else:
                sent_message = await TelegramBridge._send_chat_message(
                    bot=bot,
                    chat_id=chat_id,
                    text=chunk_text,
                    reply_markup=current_reply_markup,
                    reply_to_message_id=reply_to_message_id,
                )
            if first_message is None:
                first_message = sent_message
        return first_message

    @staticmethod
    async def _edit_rendered_chunk_in_chat(  # noqa: PLR0913
        *,
        bot: Bot,
        chat_id: int,
        message_id: int,
        text: str,
        entities: list[MessageEntity] | None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        if entities:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    entities=entities,
                    reply_markup=reply_markup,
                )
            except TelegramError as exc:
                if TelegramBridge._is_not_modified_error(exc):
                    return True
            else:
                return True
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            return bool(TelegramBridge._is_not_modified_error(exc))
        return True

    @staticmethod
    async def _send_markdown_to_chat(
        *,
        bot: Bot,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        reply_to_message_id: int | None = None,
    ) -> Message | None:
        try:
            chunks = TelegramBridge._render_markdown_chunks(text)
        except (RuntimeError, ValueError, TypeError):
            return await TelegramBridge._send_chat_message(
                bot=bot,
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
            )
        return await TelegramBridge._send_rendered_chunks_to_chat(
            bot=bot,
            chat_id=chat_id,
            chunks=chunks,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
        )

    @staticmethod
    def _render_markdown_preview_chunk(text: str) -> tuple[str, list[MessageEntity] | None] | None:
        try:
            chunks = TelegramBridge._render_markdown_chunks(text)
        except (RuntimeError, ValueError, TypeError):
            return (TelegramBridge._truncate_preview_text(text), None)
        if not chunks:
            return None
        return chunks[0]

    @staticmethod
    def _truncate_preview_text(text: str) -> str:
        if utf16_len(text) <= TELEGRAM_MAX_UTF16_MESSAGE_LENGTH:
            return text
        suffix = "..."
        max_units = TELEGRAM_MAX_UTF16_MESSAGE_LENGTH - utf16_len(suffix)
        preview_chars: list[str] = []
        current_units = 0
        for char in text:
            char_units = utf16_len(char)
            if current_units + char_units > max_units:
                break
            preview_chars.append(char)
            current_units += char_units
        return "".join(preview_chars).rstrip() + suffix

    @staticmethod
    async def _edit_markdown_in_chat(
        *,
        bot: Bot,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Edit an existing chat message in place with markdown content.

        Returns `True` when the message was successfully updated, `False` when
        editing is not possible (API error, or content spans multiple chunks).
        Multi-chunk content signals to the caller to fall back to normal delivery.
        """
        try:
            chunks = TelegramBridge._render_markdown_chunks(text)
        except (RuntimeError, ValueError, TypeError):
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup
                )
            except TelegramError as exc:
                return bool(TelegramBridge._is_not_modified_error(exc))
            else:
                return True
        if len(chunks) != 1:
            return False
        chunk_text, chunk_entities = chunks[0]
        return await TelegramBridge._edit_rendered_chunk_in_chat(
            bot=bot,
            chat_id=chat_id,
            message_id=message_id,
            text=chunk_text,
            entities=chunk_entities,
            reply_markup=reply_markup,
        )

    @staticmethod
    def _is_not_modified_error(error: TelegramError) -> bool:
        return "message is not modified" in str(error).lower()

    def _activity_workspace(self, *, chat_id: int) -> Path:
        return self._agent_service.get_workspace(chat_id=chat_id) or self._config.default_workspace

    async def _bind_pending_scheduled_tasks(self, *, chat_id: int, anchor_message_id: int) -> None:
        if self._scheduled_task_store is None:
            return
        session_context = self._active_session_context(chat_id=chat_id)
        session_id = None if session_context is None else session_context[0]
        try:
            await asyncio.to_thread(
                self._scheduled_task_store.bind_unanchored_tasks,
                chat_id=chat_id,
                session_id=session_id,
                anchor_message_id=anchor_message_id,
            )
        except Exception:
            logger.exception(
                "Failed to bind pending scheduled tasks",
                extra={"chat_id": chat_id, "anchor_message_id": anchor_message_id},
            )

    @staticmethod
    def _scheduled_anchor_message_id(task: ScheduledTask) -> int:
        anchor_message_id = task.anchor_message_id
        if anchor_message_id is None:
            raise ScheduledTaskExecutionError(SCHEDULED_MISSING_ANCHOR_ERROR)
        return anchor_message_id

    async def _send_scheduled_status_message(self, *, task: ScheduledTask, text: str) -> None:
        app = self._app
        if app is None:
            raise ScheduledTaskExecutionError(APP_NOT_RUNNING_ERROR)
        await self._send_markdown_to_chat(
            bot=app.bot,
            chat_id=task.chat_id,
            text=text,
            reply_to_message_id=self._scheduled_anchor_message_id(task),
        )

    async def _execute_scheduled_notification(self, task: ScheduledTask) -> None:
        app = self._app
        if app is None:
            raise ScheduledTaskExecutionError(APP_NOT_RUNNING_ERROR)
        final_text = (task.notify_text or "Completed.").strip()
        await self._send_markdown_to_chat(
            bot=app.bot,
            chat_id=task.chat_id,
            text=final_text,
            reply_to_message_id=self._scheduled_anchor_message_id(task),
        )

    async def _execute_scheduled_prompt(self, task: ScheduledTask) -> None:
        app = self._app
        if app is None:
            raise ScheduledTaskExecutionError(APP_NOT_RUNNING_ERROR)
        if self._active_session_context(chat_id=task.chat_id) is None:
            message = "Could not run automatically: no active session."
            await self._send_scheduled_status_message(task=task, text=message)
            raise ScheduledTaskExecutionError(message)
        prompt_text = (task.prompt_text or "").strip()
        if not prompt_text:
            message = "Could not run automatically: scheduled prompt is empty."
            await self._send_scheduled_status_message(task=task, text=message)
            raise ScheduledTaskExecutionError(message)
        anchor_message_id = self._scheduled_anchor_message_id(task)
        lock = await self._acquire_scheduled_prompt_lock(task.chat_id)
        try:
            session_context = self._active_session_context(chat_id=task.chat_id)
            session_id = None if session_context is None else session_context[0]
            with bind_log_context(chat_id=task.chat_id, session_id=session_id):
                logger.info("Executing scheduled prompt: %s", log_text_preview(prompt_text))
            await app.bot.send_chat_action(chat_id=task.chat_id, action=ChatAction.TYPING)
            self._suppressed_activity_chats.add(task.chat_id)
            try:
                reply = await self._agent_service.prompt(chat_id=task.chat_id, text=prompt_text)
            except AgentOutputLimitExceededError as exc:
                message = "Could not run automatically: agent output exceeded ACP stdio limit."
                await self._send_scheduled_status_message(task=task, text=message)
                raise ScheduledTaskExecutionError(message) from exc
            except Exception as exc:
                message = f"Could not run automatically: {exc}"
                await self._send_scheduled_status_message(task=task, text=message)
                raise ScheduledTaskExecutionError(message) from exc
            finally:
                self._suppressed_activity_chats.discard(task.chat_id)

            if reply is None:
                message = "Could not run automatically: no active session."
                await self._send_scheduled_status_message(task=task, text=message)
                raise ScheduledTaskExecutionError(message)
            try:
                await self._send_agent_reply_to_chat(
                    chat_id=task.chat_id,
                    reply_to_message_id=anchor_message_id,
                    reply=reply,
                )
            except Exception as exc:
                message = f"Could not deliver scheduled reply: {exc}"
                await self._send_scheduled_status_message(task=task, text=message)
                raise ScheduledTaskExecutionError(message) from exc
        finally:
            lock.release()

    async def _acquire_scheduled_prompt_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_prompt_lock(chat_id)
        try:
            # `asyncio.Lock` has no try-acquire API. A tiny positive timeout gives
            # us effective non-blocking behavior without the race in `lock.locked()`.
            await asyncio.wait_for(lock.acquire(), timeout=SCHEDULED_PROMPT_TRY_LOCK_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise ScheduledTaskDeferredError(SCHEDULED_CHAT_BUSY_ERROR) from exc
        return lock

    async def _send_agent_reply_to_chat(
        self,
        *,
        chat_id: int,
        reply_to_message_id: int,
        reply: AgentReply,
    ) -> None:
        app = self._app
        if app is None:
            raise ScheduledTaskExecutionError(APP_NOT_RUNNING_ERROR)
        for image in reply.images:
            await self._send_image_to_chat(
                bot=app.bot,
                chat_id=chat_id,
                reply_to_message_id=reply_to_message_id,
                payload=image,
            )
        for file_payload in reply.files:
            await self._send_file_to_chat(
                bot=app.bot,
                chat_id=chat_id,
                reply_to_message_id=reply_to_message_id,
                payload=file_payload,
            )
        if reply.text.strip():
            await self._send_markdown_to_chat(
                bot=app.bot,
                chat_id=chat_id,
                text=reply.text,
                reply_to_message_id=reply_to_message_id,
            )

    @staticmethod
    def _activity_id(block: AgentActivityBlock) -> str:
        if block.activity_id:
            return block.activity_id
        return f"{block.kind}:{block.title}"

    @staticmethod
    async def _send_image(update: Update, payload: ImagePayload) -> None:
        message = TelegramBridge._anchor_message(update)
        if message is None:
            return

        raw = base64.b64decode(payload.data_base64)
        extension = "jpg" if payload.mime_type == "image/jpeg" else "bin"
        input_file = InputFile(BytesIO(raw), filename=f"agent-image.{extension}")
        await message.reply_photo(photo=input_file)

    @staticmethod
    async def _send_image_to_chat(
        *,
        bot: Bot,
        chat_id: int,
        reply_to_message_id: int | None,
        payload: ImagePayload,
    ) -> None:
        raw = base64.b64decode(payload.data_base64)
        extension = "jpg" if payload.mime_type == "image/jpeg" else "bin"
        input_file = InputFile(BytesIO(raw), filename=f"agent-image.{extension}")
        if reply_to_message_id is not None:
            await bot.send_photo(
                chat_id=chat_id,
                photo=input_file,
                reply_to_message_id=reply_to_message_id,
                allow_sending_without_reply=True,
            )
            return
        await bot.send_photo(chat_id=chat_id, photo=input_file)

    @staticmethod
    async def _send_file(update: Update, payload: FilePayload) -> None:
        message = TelegramBridge._anchor_message(update)
        if message is None:
            return

        if payload.text_content is not None:
            raw = payload.text_content.encode("utf-8")
        elif payload.data_base64 is not None:
            raw = base64.b64decode(payload.data_base64)
        else:
            raw = b""

        input_file = InputFile(BytesIO(raw), filename=payload.name)
        await message.reply_document(document=input_file)

    @staticmethod
    async def _send_file_to_chat(
        *,
        bot: Bot,
        chat_id: int,
        reply_to_message_id: int | None,
        payload: FilePayload,
    ) -> None:
        if payload.text_content is not None:
            raw = payload.text_content.encode("utf-8")
        elif payload.data_base64 is not None:
            raw = base64.b64decode(payload.data_base64)
        else:
            raw = b""
        input_file = InputFile(BytesIO(raw), filename=payload.name)
        if reply_to_message_id is not None:
            await bot.send_document(
                chat_id=chat_id,
                document=input_file,
                reply_to_message_id=reply_to_message_id,
                allow_sending_without_reply=True,
            )
            return
        await bot.send_document(chat_id=chat_id, document=input_file)

    @staticmethod
    async def _send_chat_message(  # noqa: PLR0913
        *,
        bot: Bot,
        chat_id: int,
        text: str,
        entities: list[MessageEntity] | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        reply_to_message_id: int | None = None,
    ) -> Message:
        if reply_to_message_id is not None:
            if entities is not None:
                return await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    entities=entities,
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id,
                    allow_sending_without_reply=True,
                )
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
                allow_sending_without_reply=True,
            )
        if entities is not None:
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                entities=entities,
                reply_markup=reply_markup,
            )
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )
