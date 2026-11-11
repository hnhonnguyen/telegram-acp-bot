"""Shared state helpers for Telegram MCP tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import gettempdir, mkstemp

from telegram_acp_bot.agent_identity import DEFAULT_AGENT_NAME, agent_state_dir

STATE_FILE_ENV = "ACP_TELEGRAM_CHANNEL_STATE_FILE"
TOKEN_ENV = "ACP_TELEGRAM_BOT_TOKEN"
API_ID_ENV = "ACP_TELEGRAM_API_ID"
API_HASH_ENV = "ACP_TELEGRAM_API_HASH"
AGENT_NAME_ENV = "ACP_TELEGRAM_AGENT_NAME"
STATE_FILE_MODE = 0o600
MAX_PENDING_MEDIA_PER_CHAT = 20
MAX_GROUP_HISTORY_PER_CHAT = 200
GROUP_HISTORY_TEXT_MAX_CHARS = 700
PER_AGENT_STATE_FILE_NAME = "channel-state.json"


def default_state_file(*, pid: int) -> Path:
    """Return the default state file for a bot process."""

    return Path(gettempdir()) / f"telegram-acp-bot-mcp-state-{pid}.json"


def resolve_state_file(*, name: str | None = None, pid: int, override: str | Path | None = None) -> Path:
    """Return the state file of one agent, honouring an explicit override.

    All routing state comes from the config file, never the ambient environment. A
    named agent keeps its state in a directory of its own so two bots in one process
    never route through the same file. The `default` agent, and a run with no name at
    all, fall back to a file scoped to the pid, which keeps concurrent runs isolated but
    discards everything it holds on exit. `override` pins one agent's path exactly, which
    is how a single-bot deployment carries routing state across restarts.
    """

    if override is not None and str(override).strip():
        return Path(str(override).strip()).expanduser()
    if name is not None and name != DEFAULT_AGENT_NAME:
        return agent_state_dir(name) / PER_AGENT_STATE_FILE_NAME
    return default_state_file(pid=pid)


def load_session_chat_map(path: Path) -> dict[str, int]:
    """Load `session_id -> chat_id` mapping from disk."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}
    sessions = raw.get("sessions")
    if not isinstance(sessions, dict):
        return {}

    return {key: value for key, value in sessions.items() if isinstance(key, str) and isinstance(value, int)}


def load_prompt_message_id(path: Path, session_id: str) -> int | None:
    """Load the active Telegram prompt message id for an ACP session."""

    prompt_message_ids = _clean_prompt_message_ids(_load_raw_state(path).get("prompt_message_ids"))
    value = prompt_message_ids.get(session_id)
    return value if isinstance(value, int) else None


def save_session_chat_map(path: Path, mapping: dict[str, int]) -> None:
    """Persist `session_id -> chat_id` mapping atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _base_state_payload(_load_raw_state(path))
    payload["sessions"] = mapping
    _write_json_atomic(path, payload)


def load_last_session_id(path: Path) -> str | None:
    """Load the most recent session id used for prompt-side effects."""

    value = _load_raw_state(path).get("last_session_id")
    return value if isinstance(value, str) else None


def save_last_session_id(path: Path, session_id: str | None) -> None:
    """Persist the last active session id while keeping session mappings."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _base_state_payload(_load_raw_state(path))
    if session_id is None:
        payload.pop("last_session_id", None)
    else:
        payload["last_session_id"] = session_id
    _write_json_atomic(path, payload)


def save_prompt_message_id(path: Path, session_id: str, message_id: int | None) -> None:
    """Persist or clear the active Telegram prompt message id for a session."""

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_raw_state(path)
    payload = _base_state_payload(existing)

    prompt_message_ids = _clean_prompt_message_ids(existing.get("prompt_message_ids"))
    if message_id is None:
        prompt_message_ids.pop(session_id, None)
    else:
        prompt_message_ids[session_id] = message_id
    if prompt_message_ids:
        payload["prompt_message_ids"] = prompt_message_ids
    else:
        payload.pop("prompt_message_ids", None)
    _write_json_atomic(path, payload)


def load_pending_media(path: Path) -> dict[str, list[dict[str, object]]]:
    """Load `chat_id -> queued media references` from disk."""

    return _clean_pending_media(_load_raw_state(path).get("pending_media"))


def append_pending_media(  # noqa: PLR0913
    path: Path,
    *,
    chat_id: int,
    file_id: str,
    message_id: int,
    mime_type: str | None,
    queued_at: str,
) -> None:
    """Queue one media reference for a chat, dropping the oldest beyond the cap."""

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_raw_state(path)
    payload = _base_state_payload(existing)

    pending = _clean_pending_media(existing.get("pending_media"))
    refs = pending.get(str(chat_id), [])
    if any(ref["file_id"] == file_id for ref in refs):
        return
    refs.append({"file_id": file_id, "message_id": message_id, "mime_type": mime_type, "queued_at": queued_at})
    pending[str(chat_id)] = refs[-MAX_PENDING_MEDIA_PER_CHAT:]
    payload["pending_media"] = pending
    _write_json_atomic(path, payload)


def take_pending_media(path: Path, *, chat_id: int) -> list[dict[str, object]]:
    """Return and clear the queued media references for a chat."""

    existing = _load_raw_state(path)
    pending = _clean_pending_media(existing.get("pending_media"))
    refs = pending.pop(str(chat_id), [])
    if not refs:
        return []

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _base_state_payload(existing)
    if pending:
        payload["pending_media"] = pending
    else:
        payload.pop("pending_media", None)
    _write_json_atomic(path, payload)
    return refs


def append_group_message(  # noqa: PLR0913
    path: Path,
    *,
    chat_id: int,
    message_id: int,
    sender_id: int | None,
    sender_name: str | None,
    text: str,
    sent_at: str,
    max_entries: int = MAX_GROUP_HISTORY_PER_CHAT,
) -> None:
    """Record one group message for a chat, dropping the oldest beyond the cap.

    A `message_id` already on disk is rewritten in place instead of appended: Telegram replays
    pending updates after a restart, and an edited message must not take a second slot in the log.
    """

    if max_entries <= 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_raw_state(path)
    payload = _base_state_payload(existing)

    history = _clean_group_history(existing.get("message_history"))
    entries = history.get(str(chat_id), [])
    entry = {
        "message_id": message_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "text": text[:GROUP_HISTORY_TEXT_MAX_CHARS],
        "sent_at": sent_at,
    }
    for index, stored in enumerate(entries):
        if stored["message_id"] == message_id:
            entry["sent_at"] = stored["sent_at"]
            entries[index] = entry
            break
    else:
        entries.append(entry)
    history[str(chat_id)] = entries[-max_entries:]
    payload["message_history"] = history
    _write_json_atomic(path, payload)


def load_group_history(path: Path, *, chat_id: int) -> list[dict[str, object]]:
    """Return the recorded group messages of one chat, oldest first."""

    return _clean_group_history(_load_raw_state(path).get("message_history")).get(str(chat_id), [])


def _base_state_payload(existing: dict[str, object]) -> dict[str, object]:
    """Start a payload with every section that a writer must not silently drop."""

    sessions = existing.get("sessions")
    payload: dict[str, object] = {"sessions": sessions if isinstance(sessions, dict) else {}}
    if isinstance(existing.get("last_session_id"), str):
        payload["last_session_id"] = existing["last_session_id"]
    pending = _clean_pending_media(existing.get("pending_media"))
    if pending:
        payload["pending_media"] = pending
    history = _clean_group_history(existing.get("message_history"))
    if history:
        payload["message_history"] = history
    prompt_message_ids = _clean_prompt_message_ids(existing.get("prompt_message_ids"))
    if prompt_message_ids:
        payload["prompt_message_ids"] = prompt_message_ids
    return payload


def _clean_pending_media(raw: object) -> dict[str, list[dict[str, object]]]:
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, list[dict[str, object]]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, list):
            continue
        refs = [_clean_media_ref(item) for item in value]
        kept = [ref for ref in refs if ref is not None]
        if kept:
            cleaned[key] = kept
    return cleaned


def _clean_media_ref(item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    file_id = item.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    message_id = item.get("message_id")
    mime_type = item.get("mime_type")
    queued_at = item.get("queued_at")
    return {
        "file_id": file_id,
        "message_id": message_id if isinstance(message_id, int) else 0,
        "mime_type": mime_type if isinstance(mime_type, str) else None,
        "queued_at": queued_at if isinstance(queued_at, str) else "",
    }


def _clean_group_history(raw: object) -> dict[str, list[dict[str, object]]]:
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, list[dict[str, object]]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, list):
            continue
        entries = [_clean_history_entry(item) for item in value]
        kept = [entry for entry in entries if entry is not None]
        if kept:
            cleaned[key] = kept
    return cleaned


def _clean_history_entry(item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    message_id = item.get("message_id")
    text = item.get("text")
    if not isinstance(message_id, int) or not isinstance(text, str):
        return None
    sender_id = item.get("sender_id")
    sender_name = item.get("sender_name")
    sent_at = item.get("sent_at")
    return {
        "message_id": message_id,
        "sender_id": sender_id if isinstance(sender_id, int) else None,
        "sender_name": sender_name if isinstance(sender_name, str) else None,
        "text": text[:GROUP_HISTORY_TEXT_MAX_CHARS],
        "sent_at": sent_at if isinstance(sent_at, str) else "",
    }


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    fd, tmp_raw = mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, STATE_FILE_MODE)
        os.replace(tmp, path)
        os.chmod(path, STATE_FILE_MODE)
    finally:
        if tmp.exists():
            tmp.unlink()


def _load_raw_state(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _clean_prompt_message_ids(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, int)}
