"""Identity shared by every surface that names one agent.

An agent name is a log field and a path component at the same time, so the shape is
validated once here instead of at each use site.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_AGENT_NAME = "default"
AGENT_NAME_ERROR = "agent names must match ^[a-z0-9][a-z0-9_-]{0,62}$ so they stay safe as a path component"
_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def validate_agent_name(name: object) -> str:
    """Return `name` when it can safely identify an agent, else raise `ValueError`."""

    if not isinstance(name, str) or not _AGENT_NAME.fullmatch(name):
        raise ValueError(AGENT_NAME_ERROR)
    return name


def agent_state_dir(name: str) -> Path:
    """Return the directory holding one agent's private runtime state."""

    xdg_state_home = os.getenv("XDG_STATE_HOME", "").strip()
    state_root = Path(xdg_state_home).expanduser() if xdg_state_home else Path.home() / ".local" / "state"
    return state_root / "telegram-acp-bot" / "agents" / validate_agent_name(name)
