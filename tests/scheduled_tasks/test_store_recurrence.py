from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_acp_bot.scheduled_tasks.store import (
    OPTIONAL_SCHEDULED_TASK_COLUMNS,
    ScheduledTaskStore,
    format_utc_timestamp,
)
from tests.telegram.support import TEST_CHAT_ID

pytestmark = pytest.mark.asyncio

ANCHOR_MESSAGE_ID = 42
SOURCE_MESSAGE_ID = 77
REQUESTER_USER_ID = 88

LEGACY_SCHEMA = """
CREATE TABLE scheduled_tasks (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    session_id TEXT,
    anchor_message_id INTEGER,
    mode TEXT NOT NULL,
    prompt_text TEXT,
    notify_text TEXT,
    run_at TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    claimed_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def make_store(tmp_path: Path) -> ScheduledTaskStore:
    store = ScheduledTaskStore(tmp_path / "scheduled.sqlite3")
    store.initialize()
    return store


async def test_store_claims_due_task_without_anchor(tmp_path: Path):
    store = make_store(tmp_path)
    now = datetime.now(UTC)
    task = store.create_task(
        chat_id=TEST_CHAT_ID,
        mode="notify",
        notify_text="Ping now",
        run_at=now - timedelta(minutes=1),
    )

    claimed = store.claim_due_tasks(now=now)

    assert [item.id for item in claimed] == [task.id]


async def test_store_persists_cron_and_routing_fields(tmp_path: Path):
    store = make_store(tmp_path)
    task = store.create_task(
        chat_id=TEST_CHAT_ID,
        session_id="session-1",
        mode="notify",
        notify_text="Ping now",
        run_at=datetime.now(UTC) + timedelta(minutes=5),
        cron_expression="0 9 * * *",
        source_message_id=SOURCE_MESSAGE_ID,
        requester_user_id=REQUESTER_USER_ID,
        requester_username="alice",
        requester_name="Alice Doe",
        chat_type="supergroup",
    )

    stored = store.get_task(task.id)

    assert stored is not None
    assert stored.cron_expression == "0 9 * * *"
    assert stored.source_message_id == SOURCE_MESSAGE_ID
    assert stored.requester_user_id == REQUESTER_USER_ID
    assert stored.requester_username == "alice"
    assert stored.requester_name == "Alice Doe"
    assert stored.chat_type == "supergroup"


async def test_store_initialize_migrates_legacy_database(tmp_path: Path):
    db_path = tmp_path / "legacy.sqlite3"
    now = format_utc_timestamp(datetime.now(UTC))
    with sqlite3.connect(db_path) as connection:
        connection.execute(LEGACY_SCHEMA)
        connection.execute(
            """
            INSERT INTO scheduled_tasks (
                id, chat_id, mode, notify_text, run_at, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("legacy-task", TEST_CHAT_ID, "notify", "Old reminder", now, "pending", now, now),
        )

    store = ScheduledTaskStore(db_path)
    store.initialize()

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(scheduled_tasks)")}
    assert set(OPTIONAL_SCHEDULED_TASK_COLUMNS) <= columns
    stored = store.get_task("legacy-task")
    assert stored is not None
    assert stored.notify_text == "Old reminder"
    assert stored.cron_expression is None
    assert stored.source_message_id is None
    assert stored.requester_user_id is None
    assert stored.chat_type is None


async def test_store_initialize_is_idempotent_on_legacy_database(tmp_path: Path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(LEGACY_SCHEMA)

    store = ScheduledTaskStore(db_path)
    store.initialize()
    store.initialize()

    with sqlite3.connect(db_path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(scheduled_tasks)")]
    assert len(columns) == len(set(columns))


async def test_store_reschedule_task_returns_next_occurrence_to_pending(tmp_path: Path):
    store = make_store(tmp_path)
    now = datetime.now(UTC)
    task = store.create_task(
        chat_id=TEST_CHAT_ID,
        mode="notify",
        notify_text="Ping now",
        run_at=now - timedelta(minutes=1),
        cron_expression="*/5 * * * *",
    )
    store.claim_due_tasks(now=now)
    next_run = now + timedelta(minutes=5)

    assert store.reschedule_task(task.id, run_at=next_run)

    stored = store.get_task(task.id)
    assert stored is not None
    assert stored.status == "pending"
    assert stored.run_at == next_run
    assert stored.attempt_count == 0
    assert stored.last_error is None
    assert stored.claimed_at is None
    assert stored.started_at is None
    assert stored.finished_at is None


async def test_store_reschedule_task_preserves_error(tmp_path: Path):
    store = make_store(tmp_path)
    now = datetime.now(UTC)
    task = store.create_task(
        chat_id=TEST_CHAT_ID,
        mode="notify",
        notify_text="Ping now",
        run_at=now - timedelta(minutes=1),
        cron_expression="*/5 * * * *",
    )
    store.claim_due_tasks(now=now)

    assert store.reschedule_task(task.id, run_at=now + timedelta(minutes=5), error="boom")

    stored = store.get_task(task.id)
    assert stored is not None
    assert stored.last_error == "boom"


async def test_store_reschedule_task_ignores_rows_that_are_not_running(tmp_path: Path):
    store = make_store(tmp_path)
    task = store.create_task(
        chat_id=TEST_CHAT_ID,
        mode="notify",
        notify_text="Ping now",
        run_at=datetime.now(UTC),
        cron_expression="*/5 * * * *",
    )

    assert not store.reschedule_task(task.id, run_at=datetime.now(UTC))
    assert not store.reschedule_task("missing", run_at=datetime.now(UTC))


async def test_store_update_task_changes_schedule_but_not_routing(tmp_path: Path):
    store = make_store(tmp_path)
    now = datetime.now(UTC)
    task = store.create_task(
        chat_id=TEST_CHAT_ID,
        session_id="session-1",
        anchor_message_id=ANCHOR_MESSAGE_ID,
        mode="notify",
        notify_text="Old reminder",
        run_at=now + timedelta(minutes=5),
        source_message_id=SOURCE_MESSAGE_ID,
        requester_user_id=REQUESTER_USER_ID,
        requester_username="alice",
        requester_name="Alice Doe",
        chat_type="group",
    )
    store.claim_due_tasks(now=now + timedelta(minutes=10))
    store.release_task(task.id)
    new_run = now + timedelta(hours=1)

    updated = store.update_task(
        task_id=task.id,
        chat_id=TEST_CHAT_ID,
        mode="prompt_agent",
        run_at=new_run,
        cron_expression="0 9 * * *",
        prompt_text="New prompt",
        notify_text=None,
    )

    assert updated is not None
    assert updated.mode == "prompt_agent"
    assert updated.prompt_text == "New prompt"
    assert updated.notify_text is None
    assert updated.cron_expression == "0 9 * * *"
    assert updated.run_at == new_run
    assert updated.attempt_count == 0
    assert updated.claimed_at is None
    assert updated.started_at is None
    assert updated.source_message_id == SOURCE_MESSAGE_ID
    assert updated.anchor_message_id == ANCHOR_MESSAGE_ID
    assert updated.requester_user_id == REQUESTER_USER_ID
    assert updated.requester_username == "alice"
    assert updated.requester_name == "Alice Doe"
    assert updated.chat_type == "group"


async def test_store_update_task_rejects_running_task(tmp_path: Path):
    store = make_store(tmp_path)
    now = datetime.now(UTC)
    task = store.create_task(
        chat_id=TEST_CHAT_ID,
        mode="notify",
        notify_text="Ping now",
        run_at=now - timedelta(minutes=1),
    )
    store.claim_due_tasks(now=now)

    updated = store.update_task(
        task_id=task.id,
        chat_id=TEST_CHAT_ID,
        mode="notify",
        run_at=now,
        cron_expression=None,
        prompt_text=None,
        notify_text="Changed",
    )

    assert updated is None


async def test_store_update_task_rejects_unknown_task(tmp_path: Path):
    store = make_store(tmp_path)
    now = datetime.now(UTC)

    updated = store.update_task(
        task_id="missing",
        chat_id=TEST_CHAT_ID,
        mode="notify",
        run_at=now,
        cron_expression=None,
        prompt_text=None,
        notify_text="Changed",
    )

    assert updated is None


async def test_store_update_task_rejects_task_of_another_chat(tmp_path: Path):
    store = make_store(tmp_path)
    now = datetime.now(UTC)
    task = store.create_task(
        chat_id=TEST_CHAT_ID,
        mode="notify",
        notify_text="Ping now",
        run_at=now + timedelta(minutes=5),
    )

    updated = store.update_task(
        task_id=task.id,
        chat_id=TEST_CHAT_ID + 1,
        mode="notify",
        run_at=now,
        cron_expression=None,
        prompt_text=None,
        notify_text="Changed",
    )

    assert updated is None


async def test_store_update_task_rejects_cancelled_task(tmp_path: Path):
    store = make_store(tmp_path)
    now = datetime.now(UTC)
    task = store.create_task(
        chat_id=TEST_CHAT_ID,
        mode="notify",
        notify_text="Ping now",
        run_at=now + timedelta(minutes=5),
    )
    store.cancel_task(chat_id=TEST_CHAT_ID, task_id=task.id)

    updated = store.update_task(
        task_id=task.id,
        chat_id=TEST_CHAT_ID,
        mode="notify",
        run_at=now,
        cron_expression=None,
        prompt_text=None,
        notify_text="Changed",
    )

    assert updated is None
