"""Tests for JSON config file loading and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from telegram_acp_bot.config_file import ConfigFileError, load_config_file

CONNECT_TIMEOUT_VALUE = 2.5


def write_config(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def test_load_valid_minimal_config(tmp_path: Path) -> None:
    path = write_config(tmp_path, {})
    assert load_config_file(path) == {}


def test_load_full_config(tmp_path: Path) -> None:
    data = {
        "telegram": {
            "bot_token": "123:abc",
            "allowed_user_ids": [1, 2],
            "allowed_usernames": ["alice", "@bob"],
            "schedule_languages": ["en", "es"],
        },
        "acp": {
            "agent_command": "npx codex",
            "restart_command": "uv run ...",
            "permission_mode": "approve",
            "permission_event_output": "off",
            "stdio_limit": 1024,
            "connect_timeout": 15.5,
            "log_format": "json",
            "log_level": "DEBUG",
            "activity_mode": "compact",
            "scheduled_tasks_db": "/tmp/tasks.db",
            "workspace": "/tmp/work",
        },
    }
    assert load_config_file(write_config(tmp_path, data)) == data


def test_load_config_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match="not found"):
        load_config_file(tmp_path / "missing.json")


def test_load_config_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{invalid")
    with pytest.raises(ConfigFileError, match="Invalid JSON"):
        load_config_file(path)


def test_load_config_top_level_not_object(tmp_path: Path) -> None:
    path = write_config(tmp_path, [1, 2, 3])
    with pytest.raises(ConfigFileError, match="JSON object"):
        load_config_file(path)


def test_telegram_not_object(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram' must be a JSON object"):
        load_config_file(write_config(tmp_path, {"telegram": "bad"}))


def test_telegram_bot_token_not_string(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.bot_token' must be a string"):
        load_config_file(write_config(tmp_path, {"telegram": {"bot_token": 123}}))


def test_telegram_api_id_not_integer(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.api_id' must be a non-negative integer"):
        load_config_file(write_config(tmp_path, {"telegram": {"api_id": "12345"}}))


def test_telegram_api_id_rejects_booleans(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.api_id' must be a non-negative integer"):
        load_config_file(write_config(tmp_path, {"telegram": {"api_id": True}}))


def test_telegram_api_hash_not_string(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.api_hash' must be a string"):
        load_config_file(write_config(tmp_path, {"telegram": {"api_hash": 12345}}))


def test_telegram_api_credentials_are_accepted_together(tmp_path: Path) -> None:
    data = {"telegram": {"api_id": 12345, "api_hash": "deadbeef"}}

    assert load_config_file(write_config(tmp_path, data)) == data


def test_telegram_allowed_user_ids_not_list(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.allowed_user_ids' must be a list"):
        load_config_file(write_config(tmp_path, {"telegram": {"allowed_user_ids": "1,2"}}))


def test_telegram_allowed_user_ids_not_integers(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.allowed_user_ids' must be a list of integers"):
        load_config_file(write_config(tmp_path, {"telegram": {"allowed_user_ids": ["one"]}}))


def test_telegram_allowed_user_ids_rejects_booleans(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.allowed_user_ids' must be a list of integers"):
        load_config_file(write_config(tmp_path, {"telegram": {"allowed_user_ids": [True]}}))


def test_telegram_allowed_usernames_not_list_of_strings(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.allowed_usernames' must be a list of strings"):
        load_config_file(write_config(tmp_path, {"telegram": {"allowed_usernames": [1, 2]}}))


def test_telegram_schedule_languages_not_list_of_strings(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.schedule_languages' must be a list of strings"):
        load_config_file(write_config(tmp_path, {"telegram": {"schedule_languages": [1, 2]}}))


def test_telegram_group_require_mention_not_boolean(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.group_require_mention' must be a boolean"):
        load_config_file(write_config(tmp_path, {"telegram": {"group_require_mention": "yes"}}))


def test_telegram_group_media_queue_not_boolean(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.group_media_queue' must be a boolean"):
        load_config_file(write_config(tmp_path, {"telegram": {"group_media_queue": "yes"}}))


def test_telegram_group_history_size_not_integer(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.group_history_size' must be a non-negative integer"):
        load_config_file(write_config(tmp_path, {"telegram": {"group_history_size": "200"}}))


def test_telegram_group_history_size_rejects_boolean(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.group_history_size' must be a non-negative integer"):
        load_config_file(write_config(tmp_path, {"telegram": {"group_history_size": True}}))


def test_telegram_group_history_size_rejects_a_negative_cap(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.group_history_size' must be a non-negative integer"):
        load_config_file(write_config(tmp_path, {"telegram": {"group_history_size": -1}}))


def test_telegram_group_history_size_accepts_zero(tmp_path: Path) -> None:
    assert load_config_file(write_config(tmp_path, {"telegram": {"group_history_size": 0}}))["telegram"] == {
        "group_history_size": 0
    }


def test_telegram_status_reactions_unknown_level(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'telegram\.status_reactions' must be one of"):
        load_config_file(write_config(tmp_path, {"telegram": {"status_reactions": "loud"}}))


def test_telegram_status_reactions_minimal_is_accepted(tmp_path: Path) -> None:
    data = load_config_file(write_config(tmp_path, {"telegram": {"status_reactions": "minimal"}}))
    assert data["telegram"]["status_reactions"] == "minimal"


def test_acp_not_object(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'acp' must be a JSON object"):
        load_config_file(write_config(tmp_path, {"acp": "string"}))


def test_acp_allow_path_inputs_not_boolean(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'acp\.allow_path_inputs' must be a boolean"):
        load_config_file(write_config(tmp_path, {"acp": {"allow_path_inputs": "yes"}}))


def test_acp_channel_state_file_not_string(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'acp\.channel_state_file' must be a string"):
        load_config_file(write_config(tmp_path, {"acp": {"channel_state_file": 5}}))


def test_acp_invalid_permission_mode(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'acp\.permission_mode' must be one of"):
        load_config_file(write_config(tmp_path, {"acp": {"permission_mode": "invalid"}}))


def test_acp_invalid_permission_event_output(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'acp\.permission_event_output' must be one of"):
        load_config_file(write_config(tmp_path, {"acp": {"permission_event_output": "invalid"}}))


def test_acp_invalid_log_format(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'acp\.log_format' must be one of"):
        load_config_file(write_config(tmp_path, {"acp": {"log_format": "csv"}}))


def test_acp_invalid_activity_mode(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'acp\.activity_mode' must be one of"):
        load_config_file(write_config(tmp_path, {"acp": {"activity_mode": "extreme"}}))


def test_acp_stdio_limit_not_integer(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'acp\.stdio_limit' must be an integer"):
        load_config_file(write_config(tmp_path, {"acp": {"stdio_limit": "8mb"}}))


def test_acp_stdio_limit_rejects_booleans(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'acp\.stdio_limit' must be an integer"):
        load_config_file(write_config(tmp_path, {"acp": {"stdio_limit": True}}))


def test_acp_connect_timeout_not_number(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'acp\.connect_timeout' must be a number"):
        load_config_file(write_config(tmp_path, {"acp": {"connect_timeout": "fast"}}))


def test_acp_connect_timeout_rejects_booleans(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'acp\.connect_timeout' must be a number"):
        load_config_file(write_config(tmp_path, {"acp": {"connect_timeout": False}}))


def test_acp_string_fields_not_string(tmp_path: Path) -> None:
    for field in ("agent_command", "restart_command", "log_level", "scheduled_tasks_db", "workspace"):
        with pytest.raises(ConfigFileError, match=rf"'acp\.{field}' must be a string"):
            load_config_file(write_config(tmp_path, {"acp": {field: 42}}))


def test_connect_timeout_accepts_float(tmp_path: Path) -> None:
    data = {"acp": {"connect_timeout": CONNECT_TIMEOUT_VALUE}}
    result = load_config_file(write_config(tmp_path, data))
    assert result["acp"]["connect_timeout"] == CONNECT_TIMEOUT_VALUE


def test_unknown_keys_are_allowed(tmp_path: Path) -> None:
    """Unknown keys are tolerated for forward compatibility."""
    data = {"telegram": {"bot_token": "tok", "future_key": "value"}, "new_section": {}}
    result = load_config_file(write_config(tmp_path, data))
    assert result["telegram"]["future_key"] == "value"


def test_load_config_os_error(tmp_path: Path, mocker) -> None:
    """OSError while reading the file is wrapped in ConfigFileError."""
    path = tmp_path / "config.json"
    path.write_text("{}")
    mocker.patch("pathlib.Path.read_text", side_effect=OSError("permission denied"))
    with pytest.raises(ConfigFileError, match="Cannot read config file"):
        load_config_file(path)


# mcp_servers validation tests


def test_mcp_servers_valid_stdio(tmp_path: Path) -> None:
    data = {"mcp_servers": {"echo": {"command": "uv", "args": ["run", "echo.py"], "env": {"KEY": "val"}}}}
    result = load_config_file(write_config(tmp_path, data))
    assert result["mcp_servers"]["echo"]["command"] == "uv"


def test_mcp_servers_valid_remote(tmp_path: Path) -> None:
    data = {"mcp_servers": {"remote": {"url": "https://mcp.example.com/mcp", "headers": {"Auth": "Bearer tok"}}}}
    result = load_config_file(write_config(tmp_path, data))
    assert result["mcp_servers"]["remote"]["url"] == "https://mcp.example.com/mcp"


def test_mcp_servers_not_object(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'mcp_servers' must be a JSON object"):
        load_config_file(write_config(tmp_path, {"mcp_servers": [{"command": "x"}]}))


def test_mcp_servers_entry_not_object(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match="must be a JSON object"):
        load_config_file(write_config(tmp_path, {"mcp_servers": {"bad": "string"}}))


def test_mcp_servers_name_must_match_slug_pattern(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"must match \^\[a-z0-9-_\]\+\$"):
        load_config_file(write_config(tmp_path, {"mcp_servers": {"Bad Name": {"command": "x"}}}))


def test_mcp_servers_missing_transport(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match="must define 'command'"):
        load_config_file(write_config(tmp_path, {"mcp_servers": {"srv": {"args": []}}}))


def test_mcp_servers_both_transports(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match="cannot define both"):
        load_config_file(write_config(tmp_path, {"mcp_servers": {"srv": {"command": "x", "url": "http://x"}}}))


def test_mcp_servers_stdio_command_not_string(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match="'command' must be a string"):
        load_config_file(write_config(tmp_path, {"mcp_servers": {"srv": {"command": 42}}}))


def test_mcp_servers_stdio_args_not_list_of_strings(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match="'args' must be a list of strings"):
        load_config_file(write_config(tmp_path, {"mcp_servers": {"srv": {"command": "x", "args": [1, 2]}}}))


def test_mcp_servers_stdio_env_not_string_dict(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'env' must be an object"):
        load_config_file(write_config(tmp_path, {"mcp_servers": {"srv": {"command": "x", "env": {"k": 1}}}}))


def test_mcp_servers_remote_url_not_string(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match="'url' must be a string"):
        load_config_file(write_config(tmp_path, {"mcp_servers": {"srv": {"url": 123}}}))


def test_mcp_servers_remote_headers_not_string_dict(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match=r"'headers' must be an object"):
        load_config_file(write_config(tmp_path, {"mcp_servers": {"srv": {"url": "http://x", "headers": {"h": 1}}}}))


def test_agents_section_loads(tmp_path: Path):
    data = {"agents": {"qoder": {"acp": {"agent_command": "qodercli --acp"}}, "claude-code": {"telegram": {}}}}

    assert load_config_file(write_config(tmp_path, data)) == data


def test_agents_must_be_an_object(tmp_path: Path):
    with pytest.raises(ConfigFileError, match="'agents' must be a JSON object"):
        load_config_file(write_config(tmp_path, {"agents": ["qoder"]}))


def test_agents_must_declare_something(tmp_path: Path):
    with pytest.raises(ConfigFileError, match="'agents' must declare at least one agent"):
        load_config_file(write_config(tmp_path, {"agents": {}}))


@pytest.mark.parametrize(
    "name", ["Qoder", "-qoder", "with space", "a" * 64], ids=["uppercase", "dash", "space", "long"]
)
def test_agent_name_must_be_safe_as_a_path(name: str, tmp_path: Path):
    with pytest.raises(ConfigFileError, match="agent names must match"):
        load_config_file(write_config(tmp_path, {"agents": {name: {}}}))


def test_agent_entry_must_be_an_object(tmp_path: Path):
    with pytest.raises(ConfigFileError, match=r"agents\.qoder: must be a JSON object"):
        load_config_file(write_config(tmp_path, {"agents": {"qoder": "qodercli"}}))


def test_agent_entry_rejects_an_unknown_section(tmp_path: Path):
    with pytest.raises(ConfigFileError, match=r"agents.qoder: has unknown section\(s\) \['ssh'\]"):
        load_config_file(write_config(tmp_path, {"agents": {"qoder": {"ssh": "host"}}}))


@pytest.mark.parametrize(
    ("section", "data", "expected"),
    [
        ("telegram", {"bot_token": 5}, "'telegram.bot_token' must be a string"),
        ("telegram", {"group_media_queue": "yes"}, "'telegram.group_media_queue' must be a boolean"),
        ("acp", {"agent_command": 5}, "'acp.agent_command' must be a string"),
        ("acp", {"activity_mode": "loud"}, "'acp.activity_mode' must be one of"),
        ("mcp_servers", {"srv": {"url": "u", "command": "c"}}, "MCP server 'srv' cannot define both"),
    ],
    ids=["token", "flag", "command", "mode", "mcp"],
)
def test_agent_section_errors_name_the_agent(section: str, data: object, expected: str, tmp_path: Path):
    with pytest.raises(ConfigFileError) as caught:
        load_config_file(write_config(tmp_path, {"agents": {"qoder": {section: data}}}))

    assert f"agents.qoder: {expected}" in str(caught.value)


def test_only_must_be_a_list_of_names(tmp_path: Path):
    with pytest.raises(ConfigFileError, match="'only' must be a list of agent names"):
        load_config_file(write_config(tmp_path, {"only": "qoder"}))


def test_only_must_not_be_empty(tmp_path: Path):
    with pytest.raises(ConfigFileError, match="'only' must name at least one agent"):
        load_config_file(write_config(tmp_path, {"only": []}))


def test_only_accepts_a_list_of_names(tmp_path: Path):
    assert load_config_file(write_config(tmp_path, {"only": ["qoder"]})) == {"only": ["qoder"]}
