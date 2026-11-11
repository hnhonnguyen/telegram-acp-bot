"""CLI sub-command to register Telegram slash commands via the Bot API.

Uses `setMyCommands` (or `deleteMyCommands` when `--delete` is given) to keep
the bot's registered commands in sync with {py:data}`telegram_acp_bot.telegram.bot.BOT_COMMANDS`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Any

from telegram import (
    Bot,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
)
from telegram.error import TelegramError

from telegram_acp_bot.agent_config import agent_tokens
from telegram_acp_bot.agent_identity import DEFAULT_AGENT_NAME
from telegram_acp_bot.config_file import ConfigFileError, discover_config_path, load_config_file
from telegram_acp_bot.logging_context import quiet_transport_logging
from telegram_acp_bot.telegram.bot import BOT_COMMANDS

_SCOPE_CLASSES = {
    "default": BotCommandScopeDefault,
    "all_private_chats": BotCommandScopeAllPrivateChats,
    "all_group_chats": BotCommandScopeAllGroupChats,
    "all_chat_administrators": BotCommandScopeAllChatAdministrators,
}

SCOPE_CHOICES: list[str] = list(_SCOPE_CLASSES)

_SUBCOMMAND_NAME = "register-commands"
_SUBCOMMAND_HELP = "Register Telegram slash commands via the Bot API (setMyCommands)."
_TOKEN_REQUIRED_MESSAGE = "--telegram-token (or telegram.bot_token in the config file) is required"


def _populate_register_commands_args(parser: argparse.ArgumentParser) -> None:
    """Add `register-commands` arguments to *parser* (shared by standalone and subparser)."""
    parser.add_argument(
        "--telegram-token",
        default="",
        help="Telegram bot token. Default: telegram.bot_token from the config file.",
    )
    parser.add_argument(
        "--bot",
        default="",
        help="Name of one declared agent to register for. Default: every agent in the config file.",
    )
    parser.add_argument(
        "--scope",
        default="default",
        choices=SCOPE_CHOICES,
        help="BotCommandScope to register commands for (default: default).",
    )
    parser.add_argument(
        "--language-code",
        default="",
        help="IETF language code for language-specific registration (e.g. 'en').",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be registered without calling the API.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete registered commands instead of setting them (deleteMyCommands).",
    )


def add_register_commands_subparser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Add `register-commands` as a subparser to an existing subparsers group.

    Sets `func` on the resulting namespace so callers can dispatch via
    `opts.func(opts)`.  See also `{py:func}`get_register_commands_parser`` for
    the standalone parser used by `{py:func}`register_commands_main``.
    """
    rc: argparse.ArgumentParser = subparsers.add_parser(
        _SUBCOMMAND_NAME,
        help=_SUBCOMMAND_HELP,
        description=_SUBCOMMAND_HELP,
    )
    _populate_register_commands_args(rc)
    rc.set_defaults(func=_execute_register_commands)
    return rc


def get_register_commands_parser() -> argparse.ArgumentParser:
    """Return a standalone argument parser for the `register-commands` sub-command."""
    parser = argparse.ArgumentParser(
        prog=f"telegram-acp-bot {_SUBCOMMAND_NAME}",
        description=_SUBCOMMAND_HELP,
    )
    _populate_register_commands_args(parser)
    return parser


async def _call_api(
    *,
    token: str,
    scope_str: str,
    language_code: str | None,
    delete: bool,
) -> None:
    """Invoke `setMyCommands` or `deleteMyCommands` on the Telegram Bot API."""
    bot_scope = _SCOPE_CLASSES[scope_str]()
    async with Bot(token=token) as bot:
        if delete:
            await bot.delete_my_commands(scope=bot_scope, language_code=language_code)
        else:
            await bot.set_my_commands(list(BOT_COMMANDS), scope=bot_scope, language_code=language_code)


def _registration_targets(opts: argparse.Namespace, tokens: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """The bots to register: one named agent, every declared agent, or the flat config's bot."""
    if not opts.bot:
        return list(tokens) or [(DEFAULT_AGENT_NAME, opts.telegram_token)]
    chosen = [(name, token) for name, token in tokens if name == opts.bot]
    if not chosen:
        print(
            f"telegram-acp-bot {_SUBCOMMAND_NAME}: error: no agent named {opts.bot!r} is declared",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return chosen


def _require_tokens(targets: Sequence[tuple[str, str]]) -> None:
    """Fail before any API call when a bot has nothing to authenticate with."""
    for name, token in targets:
        if token:
            continue
        if name == DEFAULT_AGENT_NAME:
            detail = _TOKEN_REQUIRED_MESSAGE
        else:
            detail = f"agent {name!r} has no token; set telegram.bot_token under agents.{name} in the config file"
        print(f"telegram-acp-bot {_SUBCOMMAND_NAME}: error: {detail}", file=sys.stderr)
        raise SystemExit(2)


def _label(name: str) -> str:
    """Prefix that says which bot a line of output belongs to, silent for a single agent."""
    return "" if name == DEFAULT_AGENT_NAME else f"[{name}] "


def _print_dry_run(*, name: str, opts: argparse.Namespace, language_code: str | None) -> None:
    """Report what would happen for one bot, without calling the API."""
    prefix = _label(name)
    if opts.delete:
        print(f"{prefix}[dry-run] Would delete commands: scope={opts.scope!r}, language_code={language_code!r}")
        return
    print(
        f"{prefix}[dry-run] Would register {len(BOT_COMMANDS)} command(s): "
        f"scope={opts.scope!r}, language_code={language_code!r}"
    )
    for cmd, desc in BOT_COMMANDS:
        print(f"  /{cmd} - {desc}")


def _register_one(*, name: str, token: str, opts: argparse.Namespace, language_code: str | None) -> bool:
    """Register one bot's commands; `False` when the API refused, so the caller can fail the run."""
    prefix = _label(name)
    try:
        asyncio.run(
            _call_api(
                token=token,
                scope_str=opts.scope,
                language_code=language_code,
                delete=opts.delete,
            )
        )
    except TelegramError as exc:
        print(f"{prefix}Telegram API error: {exc}", file=sys.stderr)
        return False

    if opts.delete:
        print(f"{prefix}Commands deleted: scope={opts.scope!r}, language_code={language_code!r}")
    else:
        print(
            f"{prefix}Registered {len(BOT_COMMANDS)} command(s): scope={opts.scope!r}, language_code={language_code!r}"
        )
    return True


def _execute_register_commands(opts: argparse.Namespace, *, tokens: Sequence[tuple[str, str]] = ()) -> int:
    """Execute the `register-commands` logic from a pre-parsed namespace.

    `tokens` carries one `(agent, token)` pair per agent the config file declares; without
    it the namespace's own token registers the single bot of a flat config.  Called both by
    `{py:func}`register_commands_main`` and by the argparse subparser dispatch in
    `{py:func}`telegram_acp_bot.main``.

    Returns:
        0 on success, 1 when any bot's registration failed, exits with code 2 on a missing
        token or an unknown `--bot`.
    """
    targets = _registration_targets(opts, tokens)
    _require_tokens(targets)
    language_code: str | None = opts.language_code.strip() or None

    if opts.dry_run:
        for name, _ in targets:
            _print_dry_run(name=name, opts=opts, language_code=language_code)
        return 0

    # This sub-command never reaches main()'s logging setup, so transport logs are
    # still at their defaults: httpx would print the request URL, token included.
    quiet_transport_logging()

    registered = [
        _register_one(name=name, token=token, opts=opts, language_code=language_code) for name, token in targets
    ]
    return 0 if all(registered) else 1


def register_commands_main(args: list[str] | None = None) -> int:
    """Run the `register-commands` sub-command standalone.

    Parses *args* with `{py:func}`get_register_commands_parser``, discovers the config
    file, and delegates to `{py:func}`_execute_register_commands`` with one `(agent, token)`
    pair per declared agent.

    Returns:
        0 on success, 1 on API error.
    """
    parser = get_register_commands_parser()
    opts = parser.parse_args(args=args)

    config_path = discover_config_path()
    config_data: dict[str, Any] = {}
    if config_path is not None:
        try:
            config_data = load_config_file(config_path)
        except ConfigFileError as exc:
            print(f"telegram-acp-bot {_SUBCOMMAND_NAME}: error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc

    tokens = agent_tokens(config_data=config_data, fallback_token=opts.telegram_token)
    return _execute_register_commands(opts, tokens=tokens)


__all__: list[str] = [
    "SCOPE_CHOICES",
    "add_register_commands_subparser",
    "get_register_commands_parser",
    "register_commands_main",
]
