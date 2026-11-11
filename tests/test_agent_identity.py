"""Tests for the shared agent-name rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_acp_bot.agent_identity import (
    AGENT_NAME_ERROR,
    DEFAULT_AGENT_NAME,
    agent_state_dir,
    validate_agent_name,
)

LONG_NAME = "a" * 63


@pytest.mark.parametrize("name", [DEFAULT_AGENT_NAME, "qoder", "claude-code", "agent_2", "9front", LONG_NAME])
def test_validate_agent_name_accepts_a_portable_name(name: str):
    assert validate_agent_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "Qoder", "-qoder", "_qoder", "a" * 64, "with space", "with/slash", "with.dot", None, 7],
    ids=[
        "empty",
        "uppercase",
        "leading-dash",
        "leading-underscore",
        "too-long",
        "space",
        "slash",
        "dot",
        "none",
        "int",
    ],
)
def test_validate_agent_name_rejects_a_name_that_is_unsafe_as_a_path(name):
    with pytest.raises(ValueError, match=AGENT_NAME_ERROR.split()[0]):
        validate_agent_name(name)


def test_agent_state_dir_honours_xdg_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert agent_state_dir("qoder") == tmp_path / "telegram-acp-bot" / "agents" / "qoder"


def test_agent_state_dir_defaults_under_the_home_state_directory(monkeypatch: pytest.MonkeyPatch):
    home = Path("/home/tester")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)

    assert agent_state_dir("qoder") == home / ".local" / "state" / "telegram-acp-bot" / "agents" / "qoder"


def test_agent_state_dir_rejects_an_unsafe_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    with pytest.raises(ValueError, match=AGENT_NAME_ERROR.split()[0]):
        agent_state_dir("../escape")
