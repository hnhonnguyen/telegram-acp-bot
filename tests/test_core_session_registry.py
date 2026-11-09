from pathlib import Path

from acp_chat_bot.core.session_registry import SessionRegistry


def test_registry_create_get_clear():
    registry = SessionRegistry()

    session = registry.create_or_replace(chat_id=10, workspace=Path("/tmp"))
    assert registry.get(10) == session

    registry.clear(10)
    assert registry.get(10) is None


def test_registry_replace_existing_session():
    registry = SessionRegistry()

    first = registry.create_or_replace(chat_id=20, workspace=Path("/a"))
    second = registry.create_or_replace(chat_id=20, workspace=Path("/b"))

    assert first.session_id != second.session_id
    assert registry.get(20) == second
    assert second.workspace == Path("/b")


def test_registry_accepts_opaque_string_chat_ids():
    registry = SessionRegistry()

    session = registry.create_or_replace(chat_id="cid-dingtalk", workspace=Path("/tmp"))
    assert registry.get("cid-dingtalk") == session

    registry.clear("cid-dingtalk")
    assert registry.get("cid-dingtalk") is None
