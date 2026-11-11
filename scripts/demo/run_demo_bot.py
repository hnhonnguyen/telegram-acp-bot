from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shlex
import sys
from pathlib import Path
from typing import cast

from dotenv import load_dotenv

from telegram_acp_bot.acp.models import PermissionEventOutput, PermissionMode
from telegram_acp_bot.instance import BotInstanceSpec, build_bot_instance
from telegram_acp_bot.mcp.state import MAX_GROUP_HISTORY_PER_CHAT, default_state_file
from telegram_acp_bot.runner import run_bot
from telegram_acp_bot.scheduled_tasks import default_scheduled_tasks_db_path

TOKEN_REQUIRED_MESSAGE = "--telegram-token (or TELEGRAM_BOT_TOKEN) is required"
ALLOWLIST_REQUIRED_MESSAGE = "Provide at least one allowlist entry via --allowed-user-id or --allowed-username"
INVALID_ALLOWED_IDS_MESSAGE = "TELEGRAM_ALLOWED_USER_IDS must be a comma-separated list of integers"
DEFAULT_AGENT_COMMAND_TEMPLATE = "{python} scripts/demo/fake_acp_agent.py --scenario scripts/demo/demo_story.json"
STDIO_LIMIT_MESSAGE = "--acp-stdio-limit must be a positive integer"
CONNECT_TIMEOUT_MESSAGE = "--acp-connect-timeout must be a positive number"
EMPTY_COMMAND_MESSAGE = "--agent-command is empty after parsing"
DEMO_AGENT_NAME = "demo"


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_allowed_user_ids(value: str) -> list[int]:
    try:
        return [int(item) for item in _parse_csv(value)]
    except ValueError as exc:
        raise SystemExit(INVALID_ALLOWED_IDS_MESSAGE) from exc


def _normalize_usernames(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]


def _default_agent_command() -> str:
    return DEFAULT_AGENT_COMMAND_TEMPLATE.format(python=shlex.quote(sys.executable))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run-demo-bot",
        description=(
            "Run telegram-acp-bot with AcpAgentService using the scripted fake ACP agent for deterministic demos."
        ),
    )
    parser.add_argument(
        "--telegram-token",
        default=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        help="Telegram bot token. Defaults to TELEGRAM_BOT_TOKEN.",
    )
    parser.add_argument(
        "--allowed-user-id",
        action="append",
        default=_parse_allowed_user_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")),
        type=int,
        help="Allowed Telegram user ID. Can be repeated.",
    )
    parser.add_argument(
        "--allowed-username",
        action="append",
        default=_parse_csv(os.getenv("TELEGRAM_ALLOWED_USERNAMES", "")),
        help="Allowed Telegram username (with or without @). Can be repeated.",
    )
    parser.add_argument(
        "--workspace",
        default=str(Path.cwd()),
        help="Default workspace path for /new when path is not provided.",
    )
    parser.add_argument(
        "--agent-command",
        default=_default_agent_command(),
        help="ACP agent command line. Defaults to fake demo ACP agent script (ignores ACP_AGENT_COMMAND).",
    )
    parser.add_argument(
        "--permission-mode",
        default=os.getenv("ACP_PERMISSION_MODE", "ask"),
        choices=["ask", "approve", "deny"],
        help="Default ACP permission mode.",
    )
    parser.add_argument(
        "--permission-event-output",
        default=os.getenv("ACP_PERMISSION_EVENT_OUTPUT", "stdout"),
        choices=["stdout", "off"],
        help="Where ACP permission/tool event logs are emitted.",
    )
    parser.add_argument(
        "--acp-stdio-limit",
        default=int(os.getenv("ACP_STDIO_LIMIT", "8388608")),
        type=int,
        help="Asyncio StreamReader limit in bytes for ACP stdio transport.",
    )
    parser.add_argument(
        "--acp-connect-timeout",
        default=float(os.getenv("ACP_CONNECT_TIMEOUT", "30")),
        type=float,
        help="Timeout in seconds for ACP initialize/new_session handshake.",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("ACP_LOG_LEVEL", "INFO"),
        help="Log level for demo bot process.",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv(override=False)
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    token: str = args.telegram_token
    allowed_user_ids: list[int] = list(args.allowed_user_id)
    allowed_usernames: list[str] = _normalize_usernames(list(args.allowed_username))

    if not token:
        raise SystemExit(TOKEN_REQUIRED_MESSAGE)
    if not allowed_user_ids and not allowed_usernames:
        raise SystemExit(ALLOWLIST_REQUIRED_MESSAGE)
    if args.acp_stdio_limit <= 0:
        raise SystemExit(STDIO_LIMIT_MESSAGE)
    if args.acp_connect_timeout <= 0:
        raise SystemExit(CONNECT_TIMEOUT_MESSAGE)

    command_parts = shlex.split(args.agent_command)
    if not command_parts:
        raise SystemExit(EMPTY_COMMAND_MESSAGE)

    spec = BotInstanceSpec(
        name=DEMO_AGENT_NAME,
        token=token,
        mtproto_api_id=None,
        mtproto_api_hash="",
        allowed_user_ids=tuple(allowed_user_ids),
        allowed_usernames=tuple(allowed_usernames),
        workspace=args.workspace,
        activity_mode="normal",
        schedule_languages=("en",),
        group_require_mention=False,
        group_media_queue=False,
        group_history_size=MAX_GROUP_HISTORY_PER_CHAT,
        status_reactions="off",
        command_parts=tuple(command_parts),
        permission_mode=cast(PermissionMode, args.permission_mode),
        permission_event_output=cast(PermissionEventOutput, args.permission_event_output),
        stdio_limit=args.acp_stdio_limit,
        connect_timeout=args.acp_connect_timeout,
        extra_mcp_servers={},
        channel_state_file=default_state_file(pid=os.getpid()),
        scheduled_tasks_db=default_scheduled_tasks_db_path(name=DEMO_AGENT_NAME),
        allow_path_inputs=False,
        restart_command_parts=None,
    )
    stop_event = asyncio.Event()
    return run_bot([build_bot_instance(spec, stop_event=stop_event)], stop_event)


if __name__ == "__main__":
    raise SystemExit(main())
