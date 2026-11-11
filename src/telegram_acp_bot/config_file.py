"""Config file loading and validation for telegram-acp-bot."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from telegram_acp_bot.agent_identity import validate_agent_name

_VALID_PERMISSION_MODES = frozenset({"ask", "approve", "deny"})
_VALID_PERMISSION_EVENT_OUTPUTS = frozenset({"stdout", "off"})
_VALID_LOG_FORMATS = frozenset({"text", "json"})
_VALID_ACTIVITY_MODES = frozenset({"compact", "normal", "verbose"})
_VALID_STATUS_REACTION_LEVELS = frozenset({"off", "minimal"})
_MCP_SERVER_NAME_RE = re.compile(r"^[a-z0-9-_]+$")
_AGENT_SECTIONS = frozenset({"telegram", "acp", "mcp_servers"})


class ConfigFileError(ValueError):
    """Raised when the config file is missing, unreadable, or contains invalid values."""


def load_config_file(path: Path) -> dict[str, Any]:
    """Load, parse, and validate a JSON config file.

    Returns the parsed config dict.
    Raises :exc:`ConfigFileError` if the file cannot be read, is not valid
    JSON, or fails schema validation.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigFileError(f"Config file not found: {path}") from None  # noqa: TRY003
    except OSError as exc:
        raise ConfigFileError(f"Cannot read config file {path}: {exc}") from exc  # noqa: TRY003

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigFileError(f"Invalid JSON in {path}: {exc}") from exc  # noqa: TRY003

    if not isinstance(data, dict):
        raise ConfigFileError(  # noqa: TRY003
            f"Config file {path} must contain a JSON object at the top level, got {type(data).__name__}"
        )

    _validate_config(data, path)
    return data


def _err(path: Path, msg: str, where: str = "") -> ConfigFileError:
    """Build a ConfigFileError with a path prefix, and the agent it belongs to."""
    return ConfigFileError(f"{path}: {where}{msg}")


def _validate_config(data: dict[str, Any], path: Path) -> None:
    if "telegram" in data:
        _validate_telegram_section(data["telegram"], path)
    if "acp" in data:
        _validate_acp_section(data["acp"], path)
    if "mcp_servers" in data:
        _validate_mcp_servers_section(data["mcp_servers"], path)
    if "agents" in data:
        _validate_agents_section(data["agents"], path)
    if "only" in data:
        _validate_only_section(data["only"], path)


def _validate_agents_section(agents: object, path: Path) -> None:
    """Validate the optional per-agent overrides.

    Each entry may only repeat the top-level sections, so one syntax describes both a
    single bot and a fleet of them.
    """
    if not isinstance(agents, dict):
        raise _err(path, "'agents' must be a JSON object")
    if not agents:
        raise _err(path, "'agents' must declare at least one agent")
    for name, section in agents.items():
        where = f"agents.{name}: "
        try:
            validate_agent_name(name)
        except ValueError as exc:
            raise _err(path, str(exc)) from None
        if not isinstance(section, dict):
            raise _err(path, "must be a JSON object", where)
        unknown = sorted(set(section) - _AGENT_SECTIONS)
        if unknown:
            raise _err(path, f"has unknown section(s) {unknown}, expected one of {sorted(_AGENT_SECTIONS)}", where)
        if "telegram" in section:
            _validate_telegram_section(section["telegram"], path, where)
        if "acp" in section:
            _validate_acp_section(section["acp"], path, where)
        if "mcp_servers" in section:
            _validate_mcp_servers_section(section["mcp_servers"], path, where)


def _validate_only_section(only: object, path: Path) -> None:
    if not isinstance(only, list) or not all(isinstance(name, str) for name in only):
        raise _err(path, "'only' must be a list of agent names")
    if not only:
        raise _err(path, "'only' must name at least one agent")


def _validate_telegram_section(tg: dict[str, Any], path: Path, where: str = "") -> None:
    if not isinstance(tg, dict):
        raise _err(path, "'telegram' must be a JSON object", where)
    if "bot_token" in tg and not isinstance(tg["bot_token"], str):
        raise _err(path, "'telegram.bot_token' must be a string", where)
    _validate_api_credentials(tg, path, where)
    if "allowed_user_ids" in tg:
        ids = tg["allowed_user_ids"]
        if not isinstance(ids, list) or not all(type(x) is int for x in ids):
            raise _err(path, "'telegram.allowed_user_ids' must be a list of integers", where)
    if "allowed_usernames" in tg:
        names = tg["allowed_usernames"]
        if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
            raise _err(path, "'telegram.allowed_usernames' must be a list of strings", where)
    if "schedule_languages" in tg:
        languages = tg["schedule_languages"]
        if not isinstance(languages, list) or not all(isinstance(x, str) for x in languages):
            raise _err(path, "'telegram.schedule_languages' must be a list of strings", where)
    _require_optional_bool(tg, path, "group_require_mention", where)
    _require_optional_bool(tg, path, "group_media_queue", where)
    _require_optional_non_negative_int(tg, path, "group_history_size", where)
    if "status_reactions" in tg and tg["status_reactions"] not in _VALID_STATUS_REACTION_LEVELS:
        raise _err(
            path,
            f"'telegram.status_reactions' must be one of"
            f" {sorted(_VALID_STATUS_REACTION_LEVELS)}, got {tg['status_reactions']!r}",
            where,
        )


def _validate_api_credentials(tg: dict[str, Any], path: Path, where: str = "") -> None:
    """Validate the optional MTProto pair that enables large-file downloads."""

    if "api_id" in tg:
        _require_optional_non_negative_int(tg, path, "api_id", where)
    if "api_hash" in tg and not isinstance(tg["api_hash"], str):
        raise _err(path, "'telegram.api_hash' must be a string", where)


def _require_optional_bool(
    section: dict[str, Any], path: Path, key: str, where: str = "", *, label: str = "telegram"
) -> None:
    if key in section and not isinstance(section[key], bool):
        raise _err(path, f"'{label}.{key}' must be a boolean", where)


def _require_optional_non_negative_int(
    section: dict[str, Any], path: Path, key: str, where: str = "", *, label: str = "telegram"
) -> None:
    if key not in section:
        return
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _err(path, f"'{label}.{key}' must be a non-negative integer", where)


def _validate_acp_section(acp: dict[str, Any], path: Path, where: str = "") -> None:
    if not isinstance(acp, dict):
        raise _err(path, "'acp' must be a JSON object", where)
    for key in (
        "agent_command",
        "restart_command",
        "log_level",
        "scheduled_tasks_db",
        "workspace",
        "channel_state_file",
    ):
        if key in acp and not isinstance(acp[key], str):
            raise _err(path, f"'acp.{key}' must be a string", where)
    _require_optional_bool(acp, path, "allow_path_inputs", where, label="acp")
    if "permission_mode" in acp and acp["permission_mode"] not in _VALID_PERMISSION_MODES:
        raise _err(
            path,
            f"'acp.permission_mode' must be one of {sorted(_VALID_PERMISSION_MODES)}, got {acp['permission_mode']!r}",
            where,
        )
    if "permission_event_output" in acp and acp["permission_event_output"] not in _VALID_PERMISSION_EVENT_OUTPUTS:
        raise _err(
            path,
            f"'acp.permission_event_output' must be one of"
            f" {sorted(_VALID_PERMISSION_EVENT_OUTPUTS)}, got {acp['permission_event_output']!r}",
            where,
        )
    if "log_format" in acp and acp["log_format"] not in _VALID_LOG_FORMATS:
        raise _err(
            path, f"'acp.log_format' must be one of {sorted(_VALID_LOG_FORMATS)}, got {acp['log_format']!r}", where
        )
    if "activity_mode" in acp and acp["activity_mode"] not in _VALID_ACTIVITY_MODES:
        raise _err(
            path,
            f"'acp.activity_mode' must be one of {sorted(_VALID_ACTIVITY_MODES)}, got {acp['activity_mode']!r}",
            where,
        )
    if "stdio_limit" in acp and (isinstance(acp["stdio_limit"], bool) or not isinstance(acp["stdio_limit"], int)):
        raise _err(path, "'acp.stdio_limit' must be an integer", where)
    if "connect_timeout" in acp and (
        isinstance(acp["connect_timeout"], bool) or not isinstance(acp["connect_timeout"], (int, float))
    ):
        raise _err(path, "'acp.connect_timeout' must be a number", where)


def _validate_mcp_servers_section(servers: dict[str, Any], path: Path, where: str = "") -> None:
    if not isinstance(servers, dict):
        raise _err(path, "'mcp_servers' must be a JSON object", where)
    for name, server in servers.items():
        _validate_mcp_server_entry(name, server, path, where)


def _validate_mcp_server_entry(name: str, server: dict[str, Any], path: Path, where: str = "") -> None:
    if not _MCP_SERVER_NAME_RE.fullmatch(name):
        raise _err(path, f"MCP server name {name!r} must match ^[a-z0-9-_]+$ (lowercase, no spaces)", where)
    if not isinstance(server, dict):
        raise _err(path, f"MCP server {name!r} must be a JSON object", where)
    has_command = "command" in server
    has_url = "url" in server
    if not has_command and not has_url:
        raise _err(path, f"MCP server {name!r} must define 'command' (stdio) or 'url' (remote)", where)
    if has_command and has_url:
        raise _err(path, f"MCP server {name!r} cannot define both 'command' and 'url'", where)
    if has_command:
        _validate_stdio_server(name, server, path, where)
    if has_url:
        _validate_http_server(name, server, path, where)


def _validate_stdio_server(name: str, server: dict[str, Any], path: Path, where: str = "") -> None:
    if not isinstance(server["command"], str):
        raise _err(path, f"MCP server {name!r} 'command' must be a string", where)
    if "args" in server:
        args = server["args"]
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise _err(path, f"MCP server {name!r} 'args' must be a list of strings", where)
    if "env" in server:
        env = server["env"]
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise _err(path, f"MCP server {name!r} 'env' must be an object of string → string", where)


def _validate_http_server(name: str, server: dict[str, Any], path: Path, where: str = "") -> None:
    if not isinstance(server["url"], str):
        raise _err(path, f"MCP server {name!r} 'url' must be a string", where)
    if "headers" in server:
        headers = server["headers"]
        if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            raise _err(path, f"MCP server {name!r} 'headers' must be an object of string → string", where)


def discover_config_path() -> Path | None:
    """Find a config file from standard locations (used by callers that skip `--config`)."""

    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
    candidates = [
        Path(".telegram_acp_bot/config.json"),
        xdg_config_home / "telegram_acp_bot" / "config.json",
        Path("~/.telegram_acp_bot/config.json").expanduser(),
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None
