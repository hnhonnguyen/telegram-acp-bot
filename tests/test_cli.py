"""Tests for the CLI.

Configuration reaches the bot through exactly two doors: CLI flags and the config file.
There is no environment layer, so these tests pass values through those doors only.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
from importlib import metadata
from pathlib import Path

import pytest

from telegram_acp_bot import get_parser, get_version, main
from telegram_acp_bot.mcp.state import MAX_GROUP_HISTORY_PER_CHAT, STATE_FILE_ENV, TOKEN_ENV

CUSTOM_STDIO_LIMIT = 12_345
CUSTOM_CONNECT_TIMEOUT = 42.5
CONFIG_FILE_STDIO_LIMIT = 1024
CONFIG_FILE_CONNECT_TIMEOUT = 5.0
CUSTOM_GROUP_HISTORY_SIZE = 40
TWO_AGENTS = 2

BASE_ARGS = ["--telegram-token", "TOKEN", "--agent-command", "agent", "--allowed-user-id", "1"]


def test_main_requires_token():
    """Running the bot without token should fail fast."""
    with pytest.raises(SystemExit):
        main([])


def test_main_requires_agent_command():
    """Running the bot without ACP agent command should fail fast."""
    with pytest.raises(SystemExit):
        main(["--telegram-token", "TOKEN"])


def test_main_requires_allowlist(mocker):
    """Running the bot without explicit allowlist should fail fast."""
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    with pytest.raises(SystemExit):
        main(["--telegram-token", "TOKEN", "--agent-command", "agent"])


def test_main_accepts_allowed_usernames_from_cli(mocker):
    """Username allowlist from the CLI should satisfy startup requirements."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)

    assert main(["--telegram-token", "TOKEN", "--agent-command", "agent", "--allowed-username", "alice"]) == 0
    mock_run_bot.assert_called_once()


def test_main_normalizes_allowed_username_cli(mocker):
    """CLI usernames should be normalized (lowercase, no @)."""
    mocker.patch("telegram_acp_bot.instance.AcpAgentService")
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)

    assert (
        main(
            [
                "--telegram-token",
                "TOKEN",
                "--agent-command",
                "agent",
                "--allowed-username",
                "@Alice",
            ]
        )
        == 0
    )
    assert mock_run_bot.call_args is not None
    config = mock_run_bot.call_args.args[0][0].config
    assert config.allowed_usernames == {"alice"}


def test_main_runs_bot(mocker):
    """Run path delegates to the multi-agent runner."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    assert main(["--telegram-token", "TOKEN", "--agent-command", "agent --flag", "--allowed-user-id", "1"]) == 0
    mock_run_bot.assert_called_once()


def test_main_rejects_blank_agent_command(mocker):
    """Whitespace-only agent command should be rejected."""
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    with pytest.raises(SystemExit):
        main(["--telegram-token", "TOKEN", "--agent-command", "   ", "--allowed-user-id", "1"])


def test_main_rejects_non_positive_stdio_limit(mocker):
    """ACP stdio limit must be a positive integer."""
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    with pytest.raises(SystemExit):
        main([*BASE_ARGS, "--acp-stdio-limit", "0"])


def test_main_rejects_non_positive_connect_timeout(mocker):
    """ACP connect timeout must be a positive number."""
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    with pytest.raises(SystemExit):
        main([*BASE_ARGS, "--acp-connect-timeout", "0"])


def test_main_passes_stdio_limit_to_service(mocker):
    """CLI forwards acp stdio limit to service constructor."""
    mock_service = mocker.patch("telegram_acp_bot.instance.AcpAgentService")
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)

    assert main([*BASE_ARGS, "--acp-stdio-limit", str(CUSTOM_STDIO_LIMIT)]) == 0
    assert mock_service.call_args is not None
    assert mock_service.call_args.kwargs["stdio_limit"] == CUSTOM_STDIO_LIMIT


def test_main_passes_connect_timeout_to_service(mocker):
    """CLI forwards ACP connect timeout to service constructor."""
    mock_service = mocker.patch("telegram_acp_bot.instance.AcpAgentService")
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)

    assert main([*BASE_ARGS, "--acp-connect-timeout", str(CUSTOM_CONNECT_TIMEOUT)]) == 0
    assert mock_service.call_args is not None
    assert mock_service.call_args.kwargs["connect_timeout"] == CUSTOM_CONNECT_TIMEOUT


def test_main_passes_default_internal_mcp_server_to_service(mocker):
    """CLI always forwards the built-in MCP stdio server to service constructor."""
    mock_service = mocker.patch("telegram_acp_bot.instance.AcpAgentService")
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)

    assert main(BASE_ARGS) == 0
    assert mock_service.call_args is not None
    mcp_servers = mock_service.call_args.kwargs["mcp_servers"]
    assert len(mcp_servers) == 1
    server = mcp_servers[0]
    assert server.name == "telegram-channel"
    assert server.command == sys.executable
    assert server.args == ["-m", "telegram_acp_bot.mcp.server"]
    env = {item.name: item.value for item in server.env}
    assert env[TOKEN_ENV] == "TOKEN"
    assert STATE_FILE_ENV in env


def test_main_forwards_configured_state_file(mocker, tmp_path: Path):
    """An explicit acp.channel_state_file reaches both the agent service and the injected MCP server."""
    configured = tmp_path / "channel-state.json"
    mock_service = mocker.patch("telegram_acp_bot.instance.AcpAgentService")
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {
            "telegram": {"bot_token": "TOKEN", "allowed_user_ids": [1]},
            "acp": {"agent_command": "agent", "channel_state_file": str(configured)},
        },
    )

    assert main(["--config", str(config_path)]) == 0
    assert mock_service.call_args is not None
    assert mock_service.call_args.kwargs["channel_state_file"] == configured
    server = mock_service.call_args.kwargs["mcp_servers"][0]
    assert {item.name: item.value for item in server.env}[STATE_FILE_ENV] == str(configured)


def test_main_defaults_state_file_to_the_current_pid(mocker):
    """Without an override the state file stays private to this process."""
    mock_service = mocker.patch("telegram_acp_bot.instance.AcpAgentService")
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)

    assert main(BASE_ARGS) == 0
    assert mock_service.call_args is not None
    state_file = mock_service.call_args.kwargs["channel_state_file"]
    assert state_file.name == f"telegram-acp-bot-mcp-state-{os.getpid()}.json"


def test_show_help(capsys: pytest.CaptureFixture):
    """Show help."""
    with pytest.raises(SystemExit):
        main(["-h"])
    captured = capsys.readouterr()
    assert "telegram-acp-bot" in captured.out
    assert "register-commands" in captured.out


def test_show_version(mocker, capsys: pytest.CaptureFixture):
    """Show version."""
    mocker.patch("telegram_acp_bot.get_version", return_value="0.1.0")
    with pytest.raises(SystemExit):
        main(["-V"])
    captured = capsys.readouterr()
    assert "0.1.0" in captured.out


def test_main_module(mocker):
    """Test running the CLI via __main__ (python -m ...)."""
    module_name = "telegram_acp_bot.__main__"
    # Simulate: python -m telegram-acp-bot --version
    mocker.patch.object(sys, "argv", ["telegram-acp-bot", "-V"])
    with pytest.raises(SystemExit):
        runpy.run_module(module_name, run_name="__main__", alter_sys=False)


def test_get_version_package_not_found(mocker):
    """Test get_version returns 'unknown' if package is not found."""
    mocker.patch(
        "importlib.metadata.version",
        side_effect=metadata.PackageNotFoundError("not found"),
    )
    assert get_version() == "unknown"


def test_main_activity_mode_defaults_to_normal(mocker):
    """Default activity mode is normal."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    assert main(BASE_ARGS) == 0
    assert mock_run_bot.call_args is not None
    config = mock_run_bot.call_args.args[0][0].config
    assert config.activity_mode == "normal"
    assert config.compact_activity is False
    assert config.schedule_languages == ("en", "es")


def test_main_compact_activity_mode_sets_flag(mocker):
    """--activity-mode compact sets compact_activity=True on the config."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    assert main([*BASE_ARGS, "--activity-mode", "compact"]) == 0
    assert mock_run_bot.call_args is not None
    config = mock_run_bot.call_args.args[0][0].config
    assert config.compact_activity is True


def test_main_verbose_activity_mode_sets_flag(mocker):
    """--activity-mode verbose keeps the verbose activity mode on the config."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    assert main([*BASE_ARGS, "--activity-mode", "verbose"]) == 0
    assert mock_run_bot.call_args is not None
    config = mock_run_bot.call_args.args[0][0].config
    assert config.activity_mode == "verbose"


def test_main_activity_mode_short_alias_sets_flag(mocker):
    """-m verbose is accepted as shorthand for --activity-mode verbose."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    assert main([*BASE_ARGS, "-m", "verbose"]) == 0
    assert mock_run_bot.call_args is not None
    config = mock_run_bot.call_args.args[0][0].config
    assert config.activity_mode == "verbose"


def test_main_group_media_queue_flag_sets_config(mocker):
    """--group-media-queue turns the lazy group media queue on."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    assert main([*BASE_ARGS, "--group-media-queue"]) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.group_media_queue is True


def test_main_group_media_queue_config_file_sets_config(mocker, tmp_path: Path):
    """telegram.group_media_queue in the config file turns the queue on without a flag."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {
            "telegram": {"bot_token": "T", "allowed_user_ids": [1], "group_media_queue": True},
            "acp": {"agent_command": "a"},
        },
    )
    assert main(["--config", str(config_path)]) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.group_media_queue is True


def test_main_group_media_queue_defaults_to_off(mocker):
    """Group media is dropped rather than queued unless the operator opts in."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    assert main(BASE_ARGS) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.group_media_queue is False


def test_main_group_history_size_flag_sets_config(mocker):
    """--group-history-size chooses how many group messages are kept."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    assert main([*BASE_ARGS, "--group-history-size", str(CUSTOM_GROUP_HISTORY_SIZE)]) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.group_history_size == CUSTOM_GROUP_HISTORY_SIZE


def test_main_group_history_size_config_file_sets_config(mocker, tmp_path: Path):
    """telegram.group_history_size in the config file sets the cap without a flag."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {
            "telegram": {"bot_token": "T", "allowed_user_ids": [1], "group_history_size": 0},
            "acp": {"agent_command": "a"},
        },
    )
    assert main(["--config", str(config_path)]) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.group_history_size == 0


def test_main_group_history_size_defaults_to_the_shared_cap(mocker):
    """The history log is on by default, at the cap the MCP tool advertises."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    assert main(BASE_ARGS) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.group_history_size == MAX_GROUP_HISTORY_PER_CHAT


def test_main_status_reactions_flag_sets_config(mocker):
    """--status-reactions minimal makes the bot react on each prompt message."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    assert main([*BASE_ARGS, "--status-reactions", "minimal"]) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.status_reactions == "minimal"


def test_main_status_reactions_defaults_to_off(mocker):
    """Bot reactions stay out of the way until the operator asks for them."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    assert main(BASE_ARGS) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.status_reactions == "off"


def test_main_status_reactions_config_file_sets_config(mocker, tmp_path: Path):
    """telegram.status_reactions in the config file is applied as a parser default."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {
            "telegram": {"bot_token": "T", "allowed_user_ids": [1], "status_reactions": "minimal"},
            "acp": {"agent_command": "a"},
        },
    )
    assert main(["--config", str(config_path)]) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.status_reactions == "minimal"


# Config file tests


def _write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def test_main_config_file_provides_token_and_agent_command(mocker, tmp_path: Path):
    """Config file values are used when CLI flags are absent."""
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {"telegram": {"bot_token": "CFG_TOKEN", "allowed_user_ids": [1]}, "acp": {"agent_command": "cfg_agent"}},
    )
    assert main(["--config", str(config_path)]) == 0


def test_main_config_file_provides_allowlist(mocker, tmp_path: Path):
    """Allowed user IDs from config file satisfy startup validation."""
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {"telegram": {"bot_token": "T", "allowed_user_ids": [42]}, "acp": {"agent_command": "a"}},
    )
    assert main(["--config", str(config_path)]) == 0


def test_main_cli_token_overrides_config_file(mocker, tmp_path: Path):
    """--telegram-token CLI flag takes precedence over the config file."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {"telegram": {"bot_token": "CFG_TOKEN", "allowed_user_ids": [1]}, "acp": {"agent_command": "a"}},
    )
    assert main(["--config", str(config_path), "--telegram-token", "CLI_TOKEN"]) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.token == "CLI_TOKEN"


def test_main_config_file_activity_mode(mocker, tmp_path: Path):
    """activity_mode from config file is applied."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {
            "telegram": {"bot_token": "T", "allowed_user_ids": [1]},
            "acp": {"agent_command": "a", "activity_mode": "compact"},
        },
    )
    assert main(["--config", str(config_path)]) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.compact_activity is True


def test_main_schedule_languages_from_config_file(mocker, tmp_path: Path):
    """schedule_languages from config file are applied when the CLI is silent."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {
            "telegram": {"bot_token": "T", "allowed_user_ids": [1], "schedule_languages": ["es"]},
            "acp": {"agent_command": "a"},
        },
    )

    assert main(["--config", str(config_path)]) == 0

    config = mock_run_bot.call_args.args[0][0].config
    assert config.schedule_languages == ("es",)


def test_main_schedule_languages_cli_override_config(mocker, tmp_path: Path):
    """CLI schedule languages override the config file."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {
            "telegram": {"bot_token": "T", "allowed_user_ids": [1], "schedule_languages": ["es"]},
            "acp": {"agent_command": "a"},
        },
    )

    assert main(["--config", str(config_path), "--schedule-language", "en", "--schedule-language", "pt"]) == 0

    config = mock_run_bot.call_args.args[0][0].config
    assert config.schedule_languages == ("en", "pt")


def test_main_config_file_not_found_error(tmp_path: Path):
    """--config pointing to a missing file causes a startup error."""
    with pytest.raises(SystemExit):
        main(["--config", str(tmp_path / "missing.json"), "--telegram-token", "T", "--agent-command", "a"])


def test_main_config_file_invalid_json_error(tmp_path: Path):
    """--config pointing to a file with invalid JSON causes a startup error."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    with pytest.raises(SystemExit):
        main(["--config", str(bad), "--telegram-token", "T", "--agent-command", "a"])


def test_main_config_file_invalid_value_error(tmp_path: Path):
    """--config with an invalid value (e.g. bad permission_mode) causes a startup error."""
    config_path = _write_config(tmp_path, {"acp": {"permission_mode": "not_valid"}})
    with pytest.raises(SystemExit):
        main(["--config", str(config_path), "--telegram-token", "T", "--agent-command", "a"])


def test_main_config_file_workspace(mocker, tmp_path: Path):
    """workspace from config file is passed to make_config."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {
            "telegram": {"bot_token": "T", "allowed_user_ids": [1]},
            "acp": {"agent_command": "a", "workspace": str(tmp_path)},
        },
    )
    assert main(["--config", str(config_path)]) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.default_workspace == tmp_path


def test_main_config_file_allow_path_inputs(mocker, tmp_path: Path):
    """acp.allow_path_inputs reaches the spec and the injected MCP env."""
    mock_service = mocker.patch("telegram_acp_bot.instance.AcpAgentService")
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {
            "telegram": {"bot_token": "T", "allowed_user_ids": [1]},
            "acp": {"agent_command": "a", "allow_path_inputs": True},
        },
    )
    assert main(["--config", str(config_path)]) == 0
    assert mock_service.call_args is not None
    instances = mock_run_bot.call_args.args[0]
    assert instances[0].spec.allow_path_inputs is True


def test_main_config_file_external_stdio_mcp_server(mocker, tmp_path: Path):
    """Extra stdio MCP servers from config file are passed to the service."""
    mock_service = mocker.patch("telegram_acp_bot.instance.AcpAgentService")
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {
            "telegram": {"bot_token": "T", "allowed_user_ids": [1]},
            "acp": {"agent_command": "a"},
            "mcp_servers": {"echo": {"command": "uv", "args": ["run", "echo.py"], "env": {"K": "v"}}},
        },
    )
    assert main(["--config", str(config_path)]) == 0
    mcp_servers = mock_service.call_args.kwargs["mcp_servers"]
    names = [s.name for s in mcp_servers]
    assert "telegram-channel" in names
    assert "echo" in names
    echo = next(s for s in mcp_servers if s.name == "echo")
    assert echo.command == "uv"
    assert echo.args == ["run", "echo.py"]
    assert any(e.name == "K" and e.value == "v" for e in echo.env)


def test_main_config_file_external_http_mcp_server(mocker, tmp_path: Path):
    """Extra remote HTTP MCP servers from config file are passed to the service."""
    mock_service = mocker.patch("telegram_acp_bot.instance.AcpAgentService")
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {
            "telegram": {"bot_token": "T", "allowed_user_ids": [1]},
            "acp": {"agent_command": "a"},
            "mcp_servers": {"remote": {"url": "https://mcp.example.com/mcp", "headers": {"Auth": "Bearer tok"}}},
        },
    )
    assert main(["--config", str(config_path)]) == 0
    mcp_servers = mock_service.call_args.kwargs["mcp_servers"]
    remote = next(s for s in mcp_servers if s.name == "remote")
    assert remote.url == "https://mcp.example.com/mcp"
    assert any(h.name == "Auth" and h.value == "Bearer tok" for h in remote.headers)


def test_main_config_file_external_server_overrides_internal(mocker, tmp_path: Path):
    """A config server named 'telegram-channel' replaces the internal one."""
    mock_service = mocker.patch("telegram_acp_bot.instance.AcpAgentService")
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {
            "telegram": {"bot_token": "T", "allowed_user_ids": [1]},
            "acp": {"agent_command": "a"},
            "mcp_servers": {"telegram-channel": {"command": "custom-server"}},
        },
    )
    assert main(["--config", str(config_path)]) == 0
    mcp_servers = mock_service.call_args.kwargs["mcp_servers"]
    channel = next(s for s in mcp_servers if s.name == "telegram-channel")
    assert channel.command == "custom-server"


def test_main_config_file_stdio_limit_and_timeout(mocker, tmp_path: Path):
    """stdio_limit and connect_timeout from config file are forwarded to the service."""
    mock_service = mocker.patch("telegram_acp_bot.instance.AcpAgentService")
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    config_path = _write_config(
        tmp_path,
        {
            "telegram": {"bot_token": "T", "allowed_user_ids": [1]},
            "acp": {
                "agent_command": "a",
                "stdio_limit": CONFIG_FILE_STDIO_LIMIT,
                "connect_timeout": CONFIG_FILE_CONNECT_TIMEOUT,
            },
        },
    )
    assert main(["--config", str(config_path)]) == 0
    assert mock_service.call_args.kwargs["stdio_limit"] == CONFIG_FILE_STDIO_LIMIT
    assert mock_service.call_args.kwargs["connect_timeout"] == CONFIG_FILE_CONNECT_TIMEOUT


def test_register_commands_subcommand_uses_config_file(mocker, tmp_path: Path):
    """Subcommands keep config-file defaults after the full parse."""
    mock_register = mocker.patch("telegram_acp_bot.register_commands._execute_register_commands", return_value=0)
    config_path = _write_config(tmp_path, {"telegram": {"bot_token": "CFG_TOKEN"}})

    assert main(["--config", str(config_path), "register-commands"]) == 0
    assert mock_register.call_count == 1
    assert mock_register.call_args is not None
    assert mock_register.call_args.kwargs["tokens"] == []
    assert mock_register.call_args.args[0].telegram_token == "CFG_TOKEN"


def test_main_auto_discovers_config_file(mocker, monkeypatch, tmp_path: Path):
    """Config file is automatically discovered from the working directory."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)

    # Create config in one of the standard locations
    config_dir = tmp_path / ".telegram_acp_bot"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config_path.write_text(
        '{"telegram": {"bot_token": "AUTO_TOKEN", "allowed_user_ids": [1]}, "acp": {"agent_command": "auto_agent"}}'
    )

    monkeypatch.chdir(tmp_path)

    assert main([]) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.token == "AUTO_TOKEN"


def test_main_explicit_config_overrides_auto_discovery(mocker, monkeypatch, tmp_path: Path):
    """Explicit --config path takes precedence over auto-discovered files."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)

    # Create config in standard location
    config_dir = tmp_path / ".telegram_acp_bot"
    config_dir.mkdir()
    auto_config = config_dir / "config.json"
    auto_config.write_text(
        '{"telegram": {"bot_token": "AUTO_TOKEN", "allowed_user_ids": [1]}, "acp": {"agent_command": "auto_agent"}}'
    )

    # Create explicit config with different values
    explicit_config = tmp_path / "explicit.json"
    explicit_config.write_text(
        '{"telegram": {"bot_token": "EXPLICIT_TOKEN", "allowed_user_ids": [1]}, '
        '"acp": {"agent_command": "explicit_agent"}}'
    )

    monkeypatch.chdir(tmp_path)

    assert main(["--config", str(explicit_config)]) == 0
    config = mock_run_bot.call_args.args[0][0].config
    assert config.token == "EXPLICIT_TOKEN"


def test_main_rejects_blank_explicit_config_path():
    """Blank explicit --config values fail instead of falling back to discovery."""
    with pytest.raises(SystemExit, match="2"):
        main(["--config", "   "])


def test_agent_and_only_flags_accumulate_in_declaration_order():
    opts = get_parser().parse_args(
        ["--agent", "qoder=qodercli --acp", "--agent", "claude=claude-code-acp", "--only", "qoder"]
    )

    assert opts.agent == ["qoder=qodercli --acp", "claude=claude-code-acp"]
    assert opts.only == ["qoder"]


def test_no_agent_flag_means_the_flat_config_describes_everything():
    opts = get_parser().parse_args([])

    assert opts.agent == []
    assert opts.only == []


def _two_agent_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "telegram": {"bot_token": "0:base", "allowed_user_ids": [1]},
                "acp": {"agent_command": "base-agent"},
                "agents": {
                    "qoder": {"telegram": {"bot_token": "1:qoder"}},
                    "claude": {"telegram": {"bot_token": "2:claude"}},
                },
            }
        )
    )
    return path


def test_main_hands_one_instance_per_agent_to_the_runner(mocker, tmp_path: Path):
    """Every declared agent reaches the runner as its own instance, in declaration order."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)

    assert main(["--config", str(_two_agent_config(tmp_path))]) == 0
    assert mock_run_bot.call_args is not None
    instances = mock_run_bot.call_args.args[0]
    assert [instance.spec.name for instance in instances] == ["qoder", "claude"]
    assert [instance.config.token for instance in instances] == ["1:qoder", "2:claude"]


def test_main_gives_each_agent_its_own_state_file_and_database(mocker, monkeypatch, tmp_path: Path):
    """Two agents in one process never share a routing state file or scheduler database."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)

    assert main(["--config", str(_two_agent_config(tmp_path))]) == 0
    instances = mock_run_bot.call_args.args[0]
    state_files = {instance.spec.channel_state_file for instance in instances}
    databases = {instance.spec.scheduled_tasks_db for instance in instances}
    assert len(state_files) == TWO_AGENTS
    assert len(databases) == TWO_AGENTS


def test_main_shares_one_stop_event_across_the_instances(mocker, tmp_path: Path):
    """A stop request on any agent is a request to stop the whole process."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)

    assert main(["--config", str(_two_agent_config(tmp_path))]) == 0
    assert mock_run_bot.call_args is not None
    instances, stop_event = mock_run_bot.call_args.args
    assert all(instance.stop_event is stop_event for instance in instances)


def test_main_rejects_agents_that_share_one_bot_token(mocker, tmp_path: Path):
    """Two agents on the same token are a startup error, not a Telegram 409 later."""
    mocker.patch("telegram_acp_bot.run_bot", return_value=0)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "telegram": {"bot_token": "1:shared", "allowed_user_ids": [1]},
                "acp": {"agent_command": "a"},
                "agents": {
                    "qoder": {"telegram": {"bot_token": "1:shared"}},
                    "claude": {"telegram": {"bot_token": "1:shared"}},
                },
            }
        )
    )

    with pytest.raises(SystemExit, match="2"):
        main(["--config", str(path)])


def test_main_keeps_the_restart_command_on_the_spec(mocker):
    """--restart-command reaches the spec the runner re-execs with."""
    mock_run_bot = mocker.patch("telegram_acp_bot.run_bot", return_value=0)

    assert main([*BASE_ARGS, "--restart-command", "uv run telegram-acp-bot"]) == 0
    assert mock_run_bot.call_args is not None
    instances = mock_run_bot.call_args.args[0]
    assert instances[0].spec.restart_command_parts == ("uv", "run", "telegram-acp-bot")
