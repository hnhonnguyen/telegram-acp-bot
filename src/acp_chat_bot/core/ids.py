"""Opaque identifiers shared by every chat channel.

Telegram uses integers and DingTalk uses strings, so the core treats every identifier
as opaque. Values keep the type their channel produced; SQLite affinity and JSON
preserve that type across restarts.
"""

from __future__ import annotations

ChatId = int | str
MessageId = int | str
UserId = int | str

__all__: list[str] = ["ChatId", "MessageId", "UserId"]
