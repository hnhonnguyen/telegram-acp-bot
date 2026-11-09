"""Runtime configuration for the DingTalk transport."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from acp_chat_bot.acp.models import ActivityMode
from acp_chat_bot.mcp.state import MAX_GROUP_HISTORY_PER_CHAT


@dataclass(frozen=True, slots=True)
class DingTalkConfig:
    """Settings one DingTalk bot needs."""

    client_id: str
    client_secret: str
    robot_code: str
    allowed_staff_ids: frozenset[str]
    allowed_nicknames: frozenset[str] = frozenset()
    default_workspace: Path = Path(".")
    activity_mode: ActivityMode = "normal"
    group_require_mention: bool = True
    group_history_size: int = MAX_GROUP_HISTORY_PER_CHAT
    api_base_url: str = "https://api.dingtalk.com"
    max_text_length: int = 4000
