from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from acp_chat_bot.core.ids import ChatId


@dataclass(slots=True, frozen=True)
class ChatSession:
    """State for one chat bound to one active agent session."""

    chat_id: ChatId
    session_id: str
    workspace: Path


class SessionRegistry:
    """In-memory session registry with one active session per chat."""

    def __init__(self) -> None:
        self._sessions: dict[ChatId, ChatSession] = {}

    def create_or_replace(self, *, chat_id: ChatId, workspace: Path, session_id: str | None = None) -> ChatSession:
        session = ChatSession(
            chat_id=chat_id,
            session_id=session_id or str(uuid4()),
            workspace=workspace,
        )
        self._sessions[chat_id] = session
        return session

    def get(self, chat_id: ChatId) -> ChatSession | None:
        return self._sessions.get(chat_id)

    def clear(self, chat_id: ChatId) -> None:
        self._sessions.pop(chat_id, None)
