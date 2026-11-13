"""Decide which agents one process runs, and with what settings.

The precedence chain lives here as a pure function, so a multi-agent setup can be
asserted without touching Telegram, the filesystem or a subprocess. `main()` builds the
`base` spec from the flat config and CLI flags it already resolves; that spec doubles as
the single-agent answer and as the defaults every named agent overrides.

Precedence for a named agent, highest first:

1. ``--agent NAME=COMMAND`` and ``--only NAME``.
2. The ``agents.<name>`` section of the config file.
3. The flat sections carried by `base`.

There is no environment layer: routing state, tokens and databases come from the config
file alone, so two agents never collide by inheriting one flat variable.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from acp_chat_bot.agent_identity import DEFAULT_AGENT_NAME, validate_agent_name
from acp_chat_bot.config_file import ConfigFileError
from acp_chat_bot.instance import BotInstanceSpec
from acp_chat_bot.mcp.state import resolve_state_file
from acp_chat_bot.scheduled_tasks import default_scheduled_tasks_db_path


def resolve_specs(
    *,
    base: BotInstanceSpec,
    config_data: Mapping[str, Any],
    agent_flags: Sequence[str],
    only: Sequence[str],
    pid: int,
) -> list[BotInstanceSpec]:
    """Return one spec per agent to run, in declaration order.

    An empty `agents` map means the flat config described exactly one bot, so `base`
    is returned unchanged.
    """
    declared = config_data.get("agents") or {}
    flag_commands = _parse_agent_flags(agent_flags)
    names = list(dict.fromkeys([*declared, *flag_commands]))
    if not names:
        return [base]

    specs = [
        _spec_for(
            name,
            base=base,
            section=declared.get(name) or {},
            command=flag_commands.get(name),
            pid=pid,
        )
        for name in _select(names, only)
    ]
    _reject_shared_token(specs)
    return specs


def agent_tokens(*, config_data: Mapping[str, Any], fallback_token: str = "") -> list[tuple[str, str]]:
    """Pair every declared agent with the token it should use, in declaration order.

    An empty list means the flat config describes one bot, so callers keep the single
    token they already resolved. Used by `register-commands`, which needs tokens but no
    ACP wiring.
    """
    declared = config_data.get("agents") or {}
    return [(name, _token_for(name, section=declared.get(name) or {}, fallback=fallback_token)) for name in declared]


def _token_for(name: str, *, section: Mapping[str, Any], fallback: str) -> str:
    """The token one agent polls with: its own section, then the base token."""
    telegram_cfg = section.get("telegram") or {}
    return str(telegram_cfg.get("bot_token") or fallback)


def _spec_for(
    name: str,
    *,
    base: BotInstanceSpec,
    section: Mapping[str, Any],
    command: str | None,
    pid: int,
) -> BotInstanceSpec:
    """Layer one agent's overrides on top of the base spec."""
    channel = section.get("channel", base.channel)
    if channel not in {"telegram", "dingtalk"}:
        raise ConfigFileError(f"agent {name!r} has an unknown channel {channel!r}")  # noqa: TRY003
    telegram_cfg = section.get("telegram") or {}
    dingtalk_cfg = section.get("dingtalk") or {}
    acp_cfg = section.get("acp") or {}
    command_parts = _command_parts(command if command is not None else acp_cfg.get("agent_command", base.command_parts))
    if not command_parts:
        raise ConfigFileError(  # noqa: TRY003
            f"agent {name!r} has no ACP command; set acp.agent_command or pass --agent {name}=COMMAND"
        )

    inherited_dingtalk = base if base.channel == "dingtalk" else None
    if channel == "telegram":
        token = _token_for(name, section=section, fallback=base.token)
        allowed_user_ids = _ids_overlay(telegram_cfg.get("allowed_user_ids"), base.allowed_user_ids)
        allowed_usernames = _usernames_overlay(telegram_cfg.get("allowed_usernames"), base.allowed_usernames)
        allowed_staff_ids: tuple[str, ...] = ()
        allowed_nicknames: tuple[str, ...] = ()
        group_require_mention = bool(telegram_cfg.get("group_require_mention", base.group_require_mention))
        group_media_queue = bool(telegram_cfg.get("group_media_queue", base.group_media_queue))
        group_history_size = int(telegram_cfg.get("group_history_size", base.group_history_size))
        schedule_languages = _languages_overlay(telegram_cfg.get("schedule_languages"), base.schedule_languages)
        status_reactions = telegram_cfg.get("status_reactions", base.status_reactions)
        dingtalk_client_id = ""
        dingtalk_client_secret = ""
        dingtalk_robot_code = ""
        mtproto_api_id = _optional_int_overlay(telegram_cfg.get("api_id"), base.mtproto_api_id)
        mtproto_api_hash = str(telegram_cfg.get("api_hash", base.mtproto_api_hash))
    else:
        token = ""
        allowed_user_ids = ()
        allowed_usernames = ()
        allowed_staff_ids = _string_tuple_overlay(
            dingtalk_cfg.get("allowed_staff_ids"),
            inherited_dingtalk.allowed_staff_ids if inherited_dingtalk is not None else (),
        )
        allowed_nicknames = _string_tuple_overlay(
            dingtalk_cfg.get("allowed_nicknames"),
            inherited_dingtalk.allowed_dingtalk_nicknames if inherited_dingtalk is not None else (),
        )
        if not allowed_staff_ids and not allowed_nicknames:
            raise ConfigFileError(  # noqa: TRY003
                f"agent {name!r} needs dingtalk.allowed_staff_ids or dingtalk.allowed_nicknames"
            )
        group_require_mention = bool(
            dingtalk_cfg.get(
                "group_require_mention",
                inherited_dingtalk.group_require_mention if inherited_dingtalk is not None else True,
            )
        )
        group_media_queue = False
        group_history_size = int(
            dingtalk_cfg.get(
                "group_history_size",
                inherited_dingtalk.group_history_size if inherited_dingtalk is not None else base.group_history_size,
            )
        )
        schedule_languages = _languages_overlay(dingtalk_cfg.get("schedule_languages"), base.schedule_languages)
        status_reactions = "off"
        dingtalk_client_id = str(
            dingtalk_cfg.get(
                "client_id",
                inherited_dingtalk.dingtalk_client_id if inherited_dingtalk is not None else "",
            )
        )
        dingtalk_client_secret = str(
            dingtalk_cfg.get(
                "client_secret",
                inherited_dingtalk.dingtalk_client_secret if inherited_dingtalk is not None else "",
            )
        )
        dingtalk_robot_code = str(
            dingtalk_cfg.get(
                "robot_code",
                inherited_dingtalk.dingtalk_robot_code if inherited_dingtalk is not None else "",
            )
            or dingtalk_client_id
        )
        if not dingtalk_client_id or not dingtalk_client_secret:
            raise ConfigFileError(  # noqa: TRY003
                f"agent {name!r} needs dingtalk.client_id and dingtalk.client_secret"
            )
        mtproto_api_id = None
        mtproto_api_hash = ""

    dingtalk_streaming_cards = bool(
        dingtalk_cfg.get(
            "streaming_cards",
            inherited_dingtalk.dingtalk_streaming_cards
            if inherited_dingtalk is not None
            else base.dingtalk_streaming_cards,
        )
    )
    dingtalk_card_template_id = str(
        dingtalk_cfg.get(
            "card_template_id",
            inherited_dingtalk.dingtalk_card_template_id
            if inherited_dingtalk is not None
            else base.dingtalk_card_template_id,
        )
    )

    return BotInstanceSpec(
        name=name,
        channel=channel,
        token=token,
        mtproto_api_id=mtproto_api_id,
        mtproto_api_hash=mtproto_api_hash,
        allowed_user_ids=tuple(allowed_user_ids),
        allowed_usernames=tuple(allowed_usernames),
        dingtalk_client_id=dingtalk_client_id,
        dingtalk_client_secret=dingtalk_client_secret,
        dingtalk_robot_code=dingtalk_robot_code,
        allowed_staff_ids=allowed_staff_ids,
        allowed_dingtalk_nicknames=allowed_nicknames,
        dingtalk_streaming_cards=dingtalk_streaming_cards,
        dingtalk_card_template_id=dingtalk_card_template_id,
        workspace=acp_cfg.get("workspace", base.workspace),
        activity_mode=acp_cfg.get("activity_mode", base.activity_mode),
        schedule_languages=tuple(schedule_languages),
        group_require_mention=group_require_mention,
        group_media_queue=group_media_queue,
        group_history_size=group_history_size,
        status_reactions=status_reactions,
        command_parts=command_parts,
        permission_mode=acp_cfg.get("permission_mode", base.permission_mode),
        permission_event_output=acp_cfg.get("permission_event_output", base.permission_event_output),
        stdio_limit=int(acp_cfg.get("stdio_limit", base.stdio_limit)),
        connect_timeout=float(acp_cfg.get("connect_timeout", base.connect_timeout)),
        extra_mcp_servers={**base.extra_mcp_servers, **(section.get("mcp_servers") or {})},
        channel_state_file=_state_file_for(name, acp_cfg=acp_cfg, base=base, pid=pid),
        scheduled_tasks_db=_db_for(name, acp_cfg=acp_cfg, base=base),
        allow_path_inputs=bool(acp_cfg.get("allow_path_inputs", base.allow_path_inputs)),
        restart_command_parts=_command_or_none(acp_cfg.get("restart_command")) or base.restart_command_parts,
    )


def _state_file_for(name: str, *, acp_cfg: Mapping[str, Any], base: BotInstanceSpec, pid: int) -> Path:
    if acp_cfg.get("channel_state_file"):
        return resolve_state_file(name=name, pid=pid, override=acp_cfg["channel_state_file"])
    if name == DEFAULT_AGENT_NAME:
        return base.channel_state_file
    return resolve_state_file(name=name, pid=pid)


def _db_for(name: str, *, acp_cfg: Mapping[str, Any], base: BotInstanceSpec) -> Path:
    if acp_cfg.get("scheduled_tasks_db"):
        return Path(acp_cfg["scheduled_tasks_db"]).expanduser()
    if name == DEFAULT_AGENT_NAME:
        return base.scheduled_tasks_db
    return default_scheduled_tasks_db_path(name=name)


def _parse_agent_flags(agent_flags: Sequence[str]) -> dict[str, str]:
    """Read repeatable ``--agent NAME=COMMAND`` values into a map."""
    flags: dict[str, str] = {}
    for item in agent_flags:
        name, separator, command = item.partition("=")
        if not separator:
            raise ConfigFileError(f"--agent expects NAME=COMMAND, got {item!r}")  # noqa: TRY003
        try:
            validate_agent_name(name)
        except ValueError as exc:
            raise ConfigFileError(f"--agent: {exc}") from None  # noqa: TRY003
        if not command.strip():
            raise ConfigFileError(f"--agent {name}= needs a command")  # noqa: TRY003
        flags[name] = command
    return flags


def _select(names: Sequence[str], only: Sequence[str]) -> list[str]:
    if not only:
        return list(names)
    unknown = [name for name in only if name not in names]
    if unknown:
        raise ConfigFileError(f"--only names unknown agent(s) {unknown}; declared: {list(names)}")  # noqa: TRY003
    wanted = set(only)
    return [name for name in names if name in wanted]


def _reject_shared_token(specs: Sequence[BotInstanceSpec]) -> None:
    """Refuse two agents on one channel identity, because their transports would collide."""
    seen: dict[tuple[str, str], str] = {}
    for spec in specs:
        identity = spec.token if spec.channel == "telegram" else spec.dingtalk_client_id
        if not identity:
            continue
        key = (spec.channel, str(identity))
        if key in seen:
            raise ConfigFileError(  # noqa: TRY003
                f"agents {seen[key]!r} and {spec.name!r} share one {spec.channel} identity; "
                f"set a distinct channel credential for agents.{spec.name}"
            )
        seen[key] = spec.name


def _string_tuple_overlay(configured: object, inherited: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(configured, list):
        return inherited
    return tuple(str(value).strip() for value in configured if str(value).strip())


def _ids_overlay(configured: object, inherited: tuple[int, ...]) -> tuple[int, ...]:
    """Replace `inherited` allowed user ids when the agent section names its own."""
    if not isinstance(configured, list):
        return inherited
    return tuple(int(value) for value in configured)


def _optional_int_overlay(configured: object, inherited: int | None) -> int | None:
    """Overlay one optional integer, treating a configured `null` as "unset"."""

    if isinstance(configured, int):
        return int(configured)
    return inherited


def _usernames_overlay(configured: object, inherited: tuple[str, ...]) -> tuple[str, ...]:
    """Replace `inherited` usernames, normalized the way the transport expects."""
    if not isinstance(configured, list):
        return inherited
    return tuple(username.lstrip("@").strip().lower() for username in map(str, configured) if username.strip())


def _languages_overlay(configured: object, inherited: tuple[str, ...]) -> tuple[str, ...]:
    """Replace `inherited` schedule languages."""
    if not isinstance(configured, list):
        return inherited
    return tuple(language.strip().lower() for language in map(str, configured) if language.strip())


def _command_or_none(value: str | None) -> tuple[str, ...] | None:
    parts = tuple(shlex.split(value)) if value and value.strip() else ()
    return parts or None


def _command_parts(value: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(shlex.split(value))
    return tuple(value)
