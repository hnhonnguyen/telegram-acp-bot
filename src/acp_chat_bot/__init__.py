"""
Telegram ACP bot

A Telegram bot that implements Agent Client Protocol to interact with AI agents.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shlex
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, cast

from acp_chat_bot.acp.models import ActivityMode, PermissionEventOutput, PermissionMode
from acp_chat_bot.agent_config import agent_tokens, resolve_specs
from acp_chat_bot.agent_identity import DEFAULT_AGENT_NAME
from acp_chat_bot.channels.telegram.bot import StatusReactionLevel
from acp_chat_bot.config_file import ConfigFileError, discover_config_path, load_config_file
from acp_chat_bot.core.config import ChannelKind
from acp_chat_bot.instance import BotInstanceSpec, build_bot_instance
from acp_chat_bot.logging_context import configure_logging
from acp_chat_bot.mcp.state import MAX_GROUP_HISTORY_PER_CHAT, resolve_state_file
from acp_chat_bot.register_commands import add_register_commands_subparser
from acp_chat_bot.runner import run_bot
from acp_chat_bot.scheduled_tasks import default_scheduled_tasks_db_path


def get_version() -> str:
    try:
        return metadata.version("acp-chat-bot")
    except metadata.PackageNotFoundError:
        return "unknown"


def get_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser."""
    parser = argparse.ArgumentParser(prog="acp-chat-bot")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {get_version()}")

    subparsers = parser.add_subparsers(dest="subcommand", metavar="command")
    add_register_commands_subparser(subparsers)

    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help=(
            "Path to a JSON config file. If not provided, the bot looks for config files in "
            "standard locations. Values are overridden by CLI flags. See docs for the full schema."
        ),
    )
    parser.add_argument(
        "--channel",
        default="telegram",
        choices=["telegram", "dingtalk"],
        help="Chat channel for this agent. Defaults to telegram.",
    )
    parser.add_argument("--telegram-token", default="", help="Telegram bot token")
    parser.add_argument(
        "--dingtalk-client-id",
        default="",
        help="DingTalk Stream client ID (AppKey). Also set by `dingtalk.client_id`.",
    )
    parser.add_argument(
        "--dingtalk-client-secret",
        default="",
        help="DingTalk Stream client secret (AppSecret). Also set by `dingtalk.client_secret`.",
    )
    parser.add_argument(
        "--dingtalk-robot-code",
        default="",
        help="DingTalk robot code; defaults to the client ID. Also set by `dingtalk.robot_code`.",
    )
    parser.add_argument(
        "--allowed-staff-id",
        action="append",
        default=[],
        help="Allowed DingTalk staff ID. Can be repeated. Also set by `dingtalk.allowed_staff_ids`.",
    )
    parser.add_argument(
        "--allowed-dingtalk-nickname",
        action="append",
        default=[],
        help="Allowed DingTalk sender nickname. Can be repeated. Also set by `dingtalk.allowed_nicknames`.",
    )
    parser.add_argument(
        "--telegram-api-id",
        default=None,
        type=int,
        help=(
            "Telegram API id from my.telegram.org; enables MTProto file transfers beyond the "
            "Bot API limits (`telegram_download_file` for files over 20 MB, `telegram_send_attachment` "
            "for uploads over 50 MB, up to 2 GB). "
            "Also set by `telegram.api_id`; pair it with --telegram-api-hash."
        ),
    )
    parser.add_argument(
        "--telegram-api-hash",
        default="",
        help=(
            "Telegram API hash from my.telegram.org; required together with --telegram-api-id. "
            "Also set by `telegram.api_hash`."
        ),
    )
    parser.add_argument(
        "--agent-command",
        default="",
        help="ACP agent command line, e.g. 'codex-acp' or 'uv run examples/echo_agent.py'.",
    )
    parser.add_argument(
        "--allowed-user-id",
        action="append",
        default=[],
        type=int,
        help="Allowed Telegram user ID. Can be repeated.",
    )
    parser.add_argument(
        "--allowed-username",
        action="append",
        default=[],
        help="Allowed Telegram username (with or without @). Can be repeated.",
    )
    parser.add_argument(
        "--agent",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
        help=(
            "Declare or override the ACP command line of one agent, repeatable: "
            "--agent qoder='qodercli --acp' --agent claude=claude-code-acp. Each name is a "
            "separate bot, so give it its own telegram.bot_token under agents.<name>."
        ),
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="Run just the named agent(s) of the declared set. Can be repeated.",
    )
    parser.add_argument(
        "--workspace",
        default=os.getcwd(),
        help="Default workspace path for /new when path is not provided.",
    )
    parser.add_argument(
        "--schedule-language",
        action="append",
        default=[],
        help=(
            "Language code accepted by natural-language /schedule parsing. Can be repeated; defaults to 'en' and 'es'."
        ),
    )
    parser.add_argument(
        "--permission-mode",
        default="ask",
        choices=["ask", "approve", "deny"],
        help="Default ACP permission mode.",
    )
    parser.add_argument(
        "--permission-event-output",
        default="stdout",
        choices=["stdout", "off"],
        help="Where ACP permission/tool event logs are emitted.",
    )
    parser.add_argument(
        "--acp-stdio-limit",
        default=8388608,
        type=int,
        help="Asyncio StreamReader limit in bytes for ACP stdio transport.",
    )
    parser.add_argument(
        "--acp-connect-timeout",
        default=30.0,
        type=float,
        help="Timeout in seconds for ACP initialize/new_session handshake.",
    )
    parser.add_argument(
        "--restart-command",
        default="",
        help="Optional command used by /restart to relaunch the bot (e.g. 'uv run telegram-acp-bot').",
    )
    parser.add_argument(
        "--log-format",
        default="text",
        choices=["text", "json"],
        help="Application log format.",
    )
    parser.add_argument(
        "-m",
        "--activity-mode",
        default="normal",
        choices=["compact", "normal", "verbose"],
        help=(
            "Activity display mode. "
            "'normal' (default) emits each activity event as its own message. "
            "'compact' collapses all intermediate events into a single in-place status message "
            "that is replaced by the final answer when the agent responds. "
            "'verbose' streams append-only updates for active reply and tool activity."
        ),
    )
    parser.add_argument(
        "--group-require-mention",
        action="store_true",
        default=False,
        help=(
            "Ignore group messages that do not @mention the bot (or reply to one of its messages). "
            "Private chats are always answered. Also set by `telegram.group_require_mention`."
        ),
    )
    parser.add_argument(
        "--group-media-queue",
        action="store_true",
        default=False,
        help=(
            "Keep group media that the mention gate ignores as file_id references, then announce them "
            "on the next accepted prompt so the agent can download only what it needs. "
            "Also set by `telegram.group_media_queue`."
        ),
    )
    parser.add_argument(
        "--group-history-size",
        type=int,
        default=MAX_GROUP_HISTORY_PER_CHAT,
        help=(
            "How many recent group messages to keep per chat for the `telegram_list_messages` tool. "
            f"Default {MAX_GROUP_HISTORY_PER_CHAT}; 0 disables the history log. "
            "Also set by `telegram.group_history_size`."
        ),
    )
    parser.add_argument(
        "--status-reactions",
        default="off",
        choices=["off", "minimal"],
        help=(
            "React on the prompt message with the bot's own lifecycle emoji. "
            "'off' (default) adds no bot reactions; the agent can still react through its MCP tool. "
            "'minimal' marks 👀 while the chat is busy, 🤔 while the agent works, then 👍 or 💔 "
            "when the cycle ends. "
            "Also set by `telegram.status_reactions`."
        ),
    )
    parser.add_argument(
        "--scheduled-tasks-db",
        default=str(default_scheduled_tasks_db_path()),
        help="SQLite database path used for deferred scheduled follow-ups.",
    )
    return parser


def _apply_config_file_defaults(
    parser: argparse.ArgumentParser,
    config: dict[str, Any],
) -> None:
    """Apply config file values as parser-level defaults.

    CLI flags always take precedence over these defaults.
    """
    tg = config.get("telegram", {})
    dt = config.get("dingtalk", {})
    acp_cfg = config.get("acp", {})
    updates: dict[str, object] = {}

    # String settings: config file > hardcoded default in add_argument
    _str_map: list[tuple[str, Any]] = [
        ("channel", config.get("channel")),
        ("telegram_token", tg.get("bot_token")),
        ("dingtalk_client_id", dt.get("client_id")),
        ("dingtalk_client_secret", dt.get("client_secret")),
        ("dingtalk_robot_code", dt.get("robot_code")),
        ("telegram_api_id", tg.get("api_id")),
        ("telegram_api_hash", tg.get("api_hash")),
        ("agent_command", acp_cfg.get("agent_command")),
        ("restart_command", acp_cfg.get("restart_command")),
        ("permission_mode", acp_cfg.get("permission_mode")),
        ("permission_event_output", acp_cfg.get("permission_event_output")),
        ("log_format", acp_cfg.get("log_format")),
        ("activity_mode", acp_cfg.get("activity_mode")),
        ("status_reactions", tg.get("status_reactions")),
        ("scheduled_tasks_db", acp_cfg.get("scheduled_tasks_db")),
    ]
    updates |= {dest: cfg_val for dest, cfg_val in _str_map if cfg_val is not None}

    # workspace has no other source than config or the flag
    if "workspace" in acp_cfg:
        updates["workspace"] = acp_cfg["workspace"]

    # Numeric settings
    if "stdio_limit" in acp_cfg:
        updates["acp_stdio_limit"] = int(acp_cfg["stdio_limit"])
    if "connect_timeout" in acp_cfg:
        updates["acp_connect_timeout"] = float(acp_cfg["connect_timeout"])
    group_history_section = dt if config.get("channel") == "dingtalk" else tg
    if "group_history_size" in group_history_section:
        updates["group_history_size"] = int(group_history_section["group_history_size"])

    if updates:
        _set_parser_defaults(parser, updates)


def _set_parser_defaults(parser: argparse.ArgumentParser, updates: dict[str, object]) -> None:
    """Apply defaults to *parser* and any subparsers that expose the same dests."""
    parser_dests = {action.dest for action in parser._actions}
    parser_updates = {dest: value for dest, value in updates.items() if dest in parser_dests}
    if parser_updates:
        parser.set_defaults(**parser_updates)

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for subparser in action.choices.values():
                _set_parser_defaults(subparser, updates)


def _normalize_username(username: str) -> str:
    return username.lstrip("@").strip().lower()


def _resolve_allowed_users(
    *,
    parser: argparse.ArgumentParser,
    opts: argparse.Namespace,
    config_data: dict[str, Any] | None = None,
) -> tuple[list[int], list[str]]:
    cli_allowed_usernames = [_normalize_username(item) for item in opts.allowed_username]
    allowed_user_ids = list(opts.allowed_user_id)
    allowed_usernames = cli_allowed_usernames

    # Config file: used as a fallback when the CLI provided no allowed users
    if not allowed_user_ids and not allowed_usernames and config_data:
        tg = config_data.get("telegram", {})
        allowed_user_ids = list(tg.get("allowed_user_ids", []))
        allowed_usernames = [_normalize_username(u) for u in tg.get("allowed_usernames", [])]

    if not allowed_user_ids and not allowed_usernames:
        parser.error(
            "--allowed-user-id/--allowed-username (or telegram.allowed_user_ids/telegram.allowed_usernames) "
            "must include at least one allowed user",
        )
    return allowed_user_ids, allowed_usernames


def _resolve_dingtalk_users(
    *,
    parser: argparse.ArgumentParser,
    opts: argparse.Namespace,
    config_data: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    staff_ids = list(opts.allowed_staff_id)
    nicknames = [nickname.strip() for nickname in opts.allowed_dingtalk_nickname if nickname.strip()]
    if not staff_ids and not nicknames and config_data:
        dt = config_data.get("dingtalk", {})
        staff_ids = [str(value) for value in dt.get("allowed_staff_ids", [])]
        nicknames = [str(value).strip() for value in dt.get("allowed_nicknames", []) if str(value).strip()]
    if not staff_ids and not nicknames:
        parser.error(
            "--allowed-staff-id/--allowed-dingtalk-nickname "
            "(or dingtalk.allowed_staff_ids/dingtalk.allowed_nicknames) must include at least one allowed user",
        )
    return staff_ids, nicknames


def _resolve_schedule_languages(
    *,
    opts: argparse.Namespace,
    config_data: dict[str, Any] | None = None,
) -> list[str]:
    cli_languages = [language.strip() for language in opts.schedule_language if language.strip()]
    if cli_languages:
        return [language.lower() for language in cli_languages]

    if config_data:
        channel = config_data.get("channel", "telegram")
        section = config_data.get("dingtalk" if channel == "dingtalk" else "telegram", {})
        configured = section.get("schedule_languages", [])
        if configured:
            return [str(language).strip().lower() for language in configured if str(language).strip()]

    return ["en", "es"]


def _base_spec(
    *,
    parser: argparse.ArgumentParser,
    opts: argparse.Namespace,
    config_data: dict[str, Any],
) -> BotInstanceSpec:
    """Resolve the flat config into the spec every named agent inherits from.

    With no `agents` declared, this is also the only spec, which is why the
    single-bot deployment keeps exactly the values it had before multi-agent support.
    """
    channel = cast("ChannelKind", opts.channel)
    if channel == "telegram":
        if not opts.telegram_token:
            parser.error("--telegram-token (or telegram.bot_token in the config file) is required")
        allowed_user_ids, allowed_usernames = _resolve_allowed_users(
            parser=parser,
            opts=opts,
            config_data=config_data,
        )
        allowed_staff_ids: list[str] = []
        allowed_dingtalk_nicknames: list[str] = []
    else:
        if not opts.dingtalk_client_id:
            parser.error("--dingtalk-client-id (or dingtalk.client_id in the config file) is required")
        if not opts.dingtalk_client_secret:
            parser.error("--dingtalk-client-secret (or dingtalk.client_secret in the config file) is required")
        allowed_user_ids = []
        allowed_usernames = []
        allowed_staff_ids, allowed_dingtalk_nicknames = _resolve_dingtalk_users(
            parser=parser,
            opts=opts,
            config_data=config_data,
        )
    if not opts.agent_command:
        parser.error("--agent-command (or acp.agent_command in the config file) is required")
    if opts.acp_stdio_limit <= 0:
        parser.error("--acp-stdio-limit must be a positive integer")
    if opts.acp_connect_timeout <= 0:
        parser.error("--acp-connect-timeout must be a positive number")
    schedule_languages = _resolve_schedule_languages(opts=opts, config_data=config_data)
    command_parts = shlex.split(opts.agent_command)
    if not command_parts:
        parser.error("--agent-command is empty after parsing")

    acp_cfg = config_data.get("acp", {})
    telegram_cfg = config_data.get("telegram", {})
    dingtalk_cfg = config_data.get("dingtalk", {})
    channel_state_file = resolve_state_file(
        name=DEFAULT_AGENT_NAME,
        pid=os.getpid(),
        override=acp_cfg.get("channel_state_file"),
    )
    group_require_mention = opts.group_require_mention or (
        bool(dingtalk_cfg.get("group_require_mention", True))
        if channel == "dingtalk"
        else bool(telegram_cfg.get("group_require_mention", False))
    )
    return BotInstanceSpec(
        name=DEFAULT_AGENT_NAME,
        channel=channel,
        token=opts.telegram_token,
        mtproto_api_id=opts.telegram_api_id if channel == "telegram" else None,
        mtproto_api_hash=opts.telegram_api_hash if channel == "telegram" else "",
        allowed_user_ids=tuple(allowed_user_ids),
        allowed_usernames=tuple(allowed_usernames),
        dingtalk_client_id=opts.dingtalk_client_id,
        dingtalk_client_secret=opts.dingtalk_client_secret,
        dingtalk_robot_code=opts.dingtalk_robot_code or opts.dingtalk_client_id,
        allowed_staff_ids=tuple(allowed_staff_ids),
        allowed_dingtalk_nicknames=tuple(allowed_dingtalk_nicknames),
        workspace=opts.workspace,
        activity_mode=cast("ActivityMode", opts.activity_mode),
        schedule_languages=tuple(schedule_languages),
        group_require_mention=group_require_mention,
        group_media_queue=False
        if channel == "dingtalk"
        else (opts.group_media_queue or bool(telegram_cfg.get("group_media_queue", False))),
        group_history_size=int(opts.group_history_size),
        status_reactions=cast("StatusReactionLevel", "off" if channel == "dingtalk" else opts.status_reactions),
        command_parts=tuple(command_parts),
        permission_mode=cast("PermissionMode", opts.permission_mode),
        permission_event_output=cast("PermissionEventOutput", opts.permission_event_output),
        stdio_limit=int(opts.acp_stdio_limit),
        connect_timeout=float(opts.acp_connect_timeout),
        extra_mcp_servers=config_data.get("mcp_servers") or {},
        channel_state_file=channel_state_file,
        scheduled_tasks_db=Path(opts.scheduled_tasks_db).expanduser(),
        allow_path_inputs=bool(acp_cfg.get("allow_path_inputs", False)),
        restart_command_parts=tuple(shlex.split(opts.restart_command)) if opts.restart_command.strip() else None,
    )


def _find_config_file() -> Path | None:
    """Find a config file from standard locations (see `acp_chat_bot.config_file`)."""
    return discover_config_path()


def _load_preparsed_config(
    parser: argparse.ArgumentParser,
    config_arg: str | None,
) -> dict[str, Any]:
    """Load config-file defaults selected during pre-parsing."""
    if config_arg is not None:
        if not config_arg.strip():
            parser.error("--config must not be empty or whitespace")
        config_path = Path(config_arg).expanduser()
    else:
        config_path = _find_config_file()

    if config_path is None:
        return {}

    try:
        config_data = load_config_file(config_path)
    except ConfigFileError as exc:
        parser.error(str(exc))
    _apply_config_file_defaults(parser, config_data)
    return config_data


def main(args: list[str] | None = None) -> int:
    """Run the main program.

    Dispatches to the appropriate sub-command when one is given (e.g.
    `register-commands`), otherwise runs every declared agent as its own Telegram
    bot inside this one process. Sub-commands are registered via argparse
    subparsers and advertised in `--help`.
    """
    parser = get_parser()
    argv = list(args) if args is not None else sys.argv[1:]

    # Pre-parse to extract --config before full argument resolution
    pre_opts, _ = parser.parse_known_args(args=argv)

    config_data = _load_preparsed_config(parser, pre_opts.config)

    # Full parse: catches unknown/invalid flags for both the main command and subcommands.
    opts = parser.parse_args(args=argv)

    # Sub-command dispatch: each sub-command sets opts.func via set_defaults.
    if hasattr(opts, "func"):
        agents = agent_tokens(config_data=config_data, fallback_token=opts.telegram_token)
        return opts.func(opts, tokens=agents)

    log_level = config_data.get("acp", {}).get("log_level", "INFO")
    configure_logging(
        level=getattr(logging, str(log_level).upper(), logging.INFO),
        log_format=opts.log_format,
        close_replaced_handlers=True,
    )

    base = _base_spec(parser=parser, opts=opts, config_data=config_data)
    try:
        specs = resolve_specs(
            base=base,
            config_data=config_data,
            agent_flags=opts.agent,
            only=opts.only or list(config_data.get("only") or []),
            pid=os.getpid(),
        )
    except ConfigFileError as exc:
        parser.error(str(exc))

    stop_event = asyncio.Event()
    return run_bot([build_bot_instance(spec, stop_event=stop_event) for spec in specs], stop_event)


__all__: list[str] = ["get_parser", "main"]
