"""Settings shared by every chat channel."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from acp_chat_bot.acp.models import ActivityMode
from acp_chat_bot.mcp.state import MAX_GROUP_HISTORY_PER_CHAT

ChannelKind = Literal["telegram", "dingtalk"]


@dataclass(frozen=True, slots=True)
class ChatConfig:
    """Transport-neutral settings the bridge reads."""

    default_workspace: Path
    activity_mode: ActivityMode = "normal"
    schedule_languages: tuple[str, ...] = ("en", "es")
    group_require_mention: bool = False
    group_history_size: int = MAX_GROUP_HISTORY_PER_CHAT
