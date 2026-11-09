"""Tests for recurring schedule creation and agent-side CRUD tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from tests.mcp.support import (
    ACP_SCHEDULED_TASKS_DB_ENV,
    STATE_FILE_ENV,
    TEST_SCHEDULED_CHAT_ID,
    TOKEN_ENV,
    PromptRequester,
    ScheduledTaskStore,
    mcp_channel,
    save_prompt_message_id,
    save_prompt_requester,
    save_session_chat_map,
)

pytestmark = pytest.mark.asyncio

SOURCE_MESSAGE_ID = 77
REQUESTER_USER_ID = 88


@pytest.fixture
def scheduling_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    state_file = tmp_path / "state.json"
    scheduled_db = tmp_path / "scheduled.sqlite3"
    save_session_chat_map(state_file, {"s1": TEST_SCHEDULED_CHAT_ID})
    monkeypatch.setenv(TOKEN_ENV, "TOKEN")
    monkeypatch.setenv(STATE_FILE_ENV, str(state_file))
    monkeypatch.setenv(ACP_SCHEDULED_TASKS_DB_ENV, str(scheduled_db))
    return state_file, scheduled_db


def make_store(path: Path) -> ScheduledTaskStore:
    store = ScheduledTaskStore(path)
    store.initialize()
    return store


async def test_schedule_task_creates_recurring_task_with_first_occurrence(
    scheduling_env: tuple[Path, Path],
    freezer,
):
    del scheduling_env
    freezer.move_to(datetime(2026, 3, 30, 12, 0, tzinfo=UTC))

    result = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Drink water",
        cron="0 9 * * *",
    )

    assert result["ok"] is True
    assert result["cron_expression"] == "0 9 * * *"
    assert mcp_channel.parse_utc_timestamp(str(result["run_at"])) == datetime(2026, 3, 31, 9, 0, tzinfo=UTC)
    assert "Recurring schedule '0 9 * * *' (UTC)" in cast(str, result["summary"])


async def test_schedule_task_normalizes_cron_whitespace(scheduling_env: tuple[Path, Path]):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)

    result = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Drink water",
        cron="  0   9  *  *  *  ",
    )

    task = store.get_task(str(result["task_id"]))
    assert result["ok"] is True
    assert task is not None
    assert task.cron_expression == "0 9 * * *"


async def test_schedule_task_persists_routing_context(scheduling_env: tuple[Path, Path]):
    state_file, scheduled_db = scheduling_env
    save_prompt_message_id(state_file, "s1", SOURCE_MESSAGE_ID)
    save_prompt_requester(
        state_file,
        "s1",
        PromptRequester(user_id=REQUESTER_USER_ID, username="alice", display_name="Alice Doe", chat_type="group"),
    )
    store = make_store(scheduled_db)

    result = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Drink water",
        delay_minutes=10,
    )

    task = store.get_task(str(result["task_id"]))
    assert task is not None
    assert result["source_message_id"] == SOURCE_MESSAGE_ID
    assert result["reply_to_message_id"] == SOURCE_MESSAGE_ID
    assert result["requester_user_id"] == REQUESTER_USER_ID
    assert result["requester_username"] == "alice"
    assert result["requester_name"] == "Alice Doe"
    assert result["chat_type"] == "group"
    assert task.source_message_id == SOURCE_MESSAGE_ID
    assert task.requester_user_id == REQUESTER_USER_ID


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"cron": "0 9 * * *", "run_at": "2026-03-30T21:00:00+00:00"}, "not both"),
        ({"cron": "0 9 * * *", "delay_minutes": 10}, "not both"),
        ({"cron": "   "}, "must not be empty"),
        ({"cron": "not a cron"}, "invalid cron expression"),
        ({"cron": "0 0 31 2 *"}, "invalid cron expression"),
    ],
)
async def test_schedule_task_rejects_invalid_cron_inputs(
    scheduling_env: tuple[Path, Path],
    kwargs: dict[str, Any],
    error: str,
):
    del scheduling_env

    result = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Drink water",
        **kwargs,
    )

    assert result["ok"] is False
    assert error in cast(str, result["error"])


async def test_schedule_task_one_shot_has_no_cron(scheduling_env: tuple[Path, Path]):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)

    result = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Drink water",
        delay_minutes=10,
    )

    task = store.get_task(str(result["task_id"]))
    assert result["ok"] is True
    assert result["cron_expression"] is None
    assert task is not None
    assert task.cron_expression is None


async def test_list_scheduled_tasks_returns_only_current_chat(scheduling_env: tuple[Path, Path]):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)
    now = datetime.now(UTC)
    first = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Mine",
        run_at=(now + timedelta(minutes=5)).isoformat(),
    )
    store.create_task(
        chat_id=TEST_SCHEDULED_CHAT_ID + 1,
        mode="notify",
        notify_text="Other chat",
        run_at=now + timedelta(minutes=5),
    )

    result = await mcp_channel.list_scheduled_tasks()

    assert result["ok"] is True
    tasks = cast(list[dict[str, object]], result["tasks"])
    assert [task["task_id"] for task in tasks] == [first["task_id"]]
    assert tasks[0]["notify_text"] == "Mine"
    assert tasks[0]["chat_id"] == TEST_SCHEDULED_CHAT_ID


async def test_list_scheduled_tasks_filters_statuses(scheduling_env: tuple[Path, Path]):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)
    now = datetime.now(UTC)
    cancelled = store.create_task(
        chat_id=TEST_SCHEDULED_CHAT_ID,
        mode="notify",
        notify_text="Cancelled",
        run_at=now + timedelta(minutes=5),
    )
    store.create_task(
        chat_id=TEST_SCHEDULED_CHAT_ID,
        mode="notify",
        notify_text="Pending",
        run_at=now + timedelta(minutes=5),
    )
    store.cancel_task(chat_id=TEST_SCHEDULED_CHAT_ID, task_id=cancelled.id)

    result = await mcp_channel.list_scheduled_tasks(statuses=["cancelled"])

    tasks = cast(list[dict[str, object]], result["tasks"])
    assert [task["task_id"] for task in tasks] == [cancelled.id]


async def test_list_scheduled_tasks_rejects_invalid_statuses(scheduling_env: tuple[Path, Path]):
    del scheduling_env

    result = await mcp_channel.list_scheduled_tasks(statuses=["later"])

    assert result["ok"] is False
    assert "statuses must be" in cast(str, result["error"])


async def test_list_scheduled_tasks_rejects_empty_statuses(scheduling_env: tuple[Path, Path]):
    del scheduling_env

    result = await mcp_channel.list_scheduled_tasks(statuses=[])

    assert result["ok"] is False
    assert "statuses must be" in cast(str, result["error"])


@pytest.mark.parametrize("limit", [0, 51])
async def test_list_scheduled_tasks_rejects_invalid_limit(scheduling_env: tuple[Path, Path], limit: int):
    del scheduling_env

    result = await mcp_channel.list_scheduled_tasks(limit=limit)

    assert result["ok"] is False
    assert result["error"] == "limit must be between 1 and 50"


async def test_list_scheduled_tasks_reports_missing_store(
    scheduling_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    del scheduling_env
    monkeypatch.delenv(ACP_SCHEDULED_TASKS_DB_ENV)

    result = await mcp_channel.list_scheduled_tasks()

    assert result["ok"] is False
    assert "missing" in cast(str, result["error"])


async def test_update_scheduled_task_changes_text(scheduling_env: tuple[Path, Path]):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)
    created = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Old",
        delay_minutes=10,
    )

    result = await mcp_channel.update_scheduled_task(
        str(created["task_id"]),
        notify_text="New",
    )

    assert result["ok"] is True
    updated = cast(dict[str, object], result["task"])
    assert updated["notify_text"] == "New"
    task = store.get_task(str(created["task_id"]))
    assert task is not None
    assert task.notify_text == "New"


async def test_update_scheduled_task_switches_mode(scheduling_env: tuple[Path, Path]):
    del scheduling_env
    created = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Old",
        delay_minutes=10,
    )

    result = await mcp_channel.update_scheduled_task(
        str(created["task_id"]),
        mode="prompt_agent",
        prompt_text="Check again",
    )

    assert result["ok"] is True
    updated = cast(dict[str, object], result["task"])
    assert updated["mode"] == "prompt_agent"
    assert updated["prompt_text"] == "Check again"


async def test_update_scheduled_task_rejects_mode_without_text(scheduling_env: tuple[Path, Path]):
    del scheduling_env
    created = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Old",
        delay_minutes=10,
    )

    result = await mcp_channel.update_scheduled_task(str(created["task_id"]), mode="prompt_agent")

    assert result["ok"] is False
    assert result["error"] == "prompt_agent mode requires prompt_text"


async def test_update_scheduled_task_switches_to_cron(scheduling_env: tuple[Path, Path], freezer):
    del scheduling_env
    freezer.move_to(datetime(2026, 3, 30, 12, 0, tzinfo=UTC))
    created = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Old",
        run_at="2026-03-30T13:00:00+00:00",
    )

    result = await mcp_channel.update_scheduled_task(str(created["task_id"]), cron="*/5 * * * *")

    assert result["ok"] is True
    updated = cast(dict[str, object], result["task"])
    assert updated["cron_expression"] == "*/5 * * * *"
    assert mcp_channel.parse_utc_timestamp(str(updated["run_at"])) == datetime(2026, 3, 30, 12, 5, tzinfo=UTC)


async def test_update_scheduled_task_clears_cron(scheduling_env: tuple[Path, Path]):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)
    created = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Old",
        cron="0 9 * * *",
    )
    before = store.get_task(str(created["task_id"]))
    assert before is not None

    result = await mcp_channel.update_scheduled_task(str(created["task_id"]), cron="")

    assert result["ok"] is True
    updated = cast(dict[str, object], result["task"])
    assert updated["cron_expression"] is None
    assert mcp_channel.parse_utc_timestamp(str(updated["run_at"])) == before.run_at


async def test_update_scheduled_task_accepts_relative_time(scheduling_env: tuple[Path, Path], freezer):
    del scheduling_env
    freezer.move_to(datetime(2026, 3, 30, 12, 0, tzinfo=UTC))
    created = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Old",
        cron="0 9 * * *",
    )

    result = await mcp_channel.update_scheduled_task(str(created["task_id"]), delay_minutes=10)

    assert result["ok"] is True
    updated = cast(dict[str, object], result["task"])
    assert updated["cron_expression"] is None
    assert mcp_channel.parse_utc_timestamp(str(updated["run_at"])) == datetime(2026, 3, 30, 12, 10, tzinfo=UTC)


async def test_update_scheduled_task_rejects_cron_with_time(scheduling_env: tuple[Path, Path]):
    del scheduling_env
    created = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Old",
        delay_minutes=10,
    )

    result = await mcp_channel.update_scheduled_task(
        str(created["task_id"]),
        cron="0 9 * * *",
        delay_minutes=10,
    )

    assert result["ok"] is False
    assert result["error"] == "provide either cron or run_at/delay inputs, not both"


async def test_update_scheduled_task_rejects_invalid_cron(scheduling_env: tuple[Path, Path]):
    del scheduling_env
    created = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Old",
        delay_minutes=10,
    )

    result = await mcp_channel.update_scheduled_task(str(created["task_id"]), cron="not a cron")

    assert result["ok"] is False
    assert "invalid cron expression" in cast(str, result["error"])


async def test_update_scheduled_task_rejects_invalid_time_input(scheduling_env: tuple[Path, Path]):
    del scheduling_env
    created = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Old",
        delay_minutes=10,
    )

    result = await mcp_channel.update_scheduled_task(str(created["task_id"]), run_at="2026-03-30T21:00:00")

    assert result["ok"] is False
    assert "timezone" in cast(str, result["error"])


async def test_update_scheduled_task_reports_missing_store(
    scheduling_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    del scheduling_env
    monkeypatch.delenv(ACP_SCHEDULED_TASKS_DB_ENV)

    result = await mcp_channel.update_scheduled_task("missing", notify_text="New")

    assert result["ok"] is False
    assert "missing" in cast(str, result["error"])


async def test_update_scheduled_task_rejects_running_task(scheduling_env: tuple[Path, Path]):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)
    now = datetime.now(UTC)
    task = store.create_task(
        chat_id=TEST_SCHEDULED_CHAT_ID,
        mode="notify",
        notify_text="Old",
        run_at=now - timedelta(minutes=1),
    )
    store.claim_due_tasks(now=now)

    result = await mcp_channel.update_scheduled_task(task.id, notify_text="New")

    assert result["ok"] is False
    assert result["error"] == "task is currently running; try again after it finishes"


async def test_update_scheduled_task_rejects_unknown_task(scheduling_env: tuple[Path, Path]):
    del scheduling_env

    result = await mcp_channel.update_scheduled_task("missing", notify_text="New")

    assert result["ok"] is False
    assert result["error"] == "task not found"


async def test_update_scheduled_task_rejects_other_chat(scheduling_env: tuple[Path, Path]):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)
    task = store.create_task(
        chat_id=TEST_SCHEDULED_CHAT_ID + 1,
        mode="notify",
        notify_text="Old",
        run_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    result = await mcp_channel.update_scheduled_task(task.id, notify_text="New")

    assert result["ok"] is False
    assert result["error"] == "task not found"


async def test_update_scheduled_task_rejects_cancelled_task(scheduling_env: tuple[Path, Path]):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)
    task = store.create_task(
        chat_id=TEST_SCHEDULED_CHAT_ID,
        mode="notify",
        notify_text="Old",
        run_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    store.cancel_task(chat_id=TEST_SCHEDULED_CHAT_ID, task_id=task.id)

    result = await mcp_channel.update_scheduled_task(task.id, notify_text="New")

    assert result["ok"] is False
    assert "only pending tasks can be updated" in cast(str, result["error"])


async def test_update_scheduled_task_reports_lost_race(scheduling_env: tuple[Path, Path], mocker):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)
    task = store.create_task(
        chat_id=TEST_SCHEDULED_CHAT_ID,
        mode="notify",
        notify_text="Old",
        run_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    mocker.patch.object(store, "update_task", return_value=None)
    mocker.patch("acp_chat_bot.mcp.tools.scheduling.load_scheduled_task_store", return_value=store)

    result = await mcp_channel.update_scheduled_task(task.id, notify_text="New")

    assert result["ok"] is False
    assert "may have started running" in cast(str, result["error"])


async def test_cancel_scheduled_task_cancels_pending(scheduling_env: tuple[Path, Path]):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)
    created = await mcp_channel.schedule_task(
        mode="notify",
        notify_text="Old",
        delay_minutes=10,
    )

    result = await mcp_channel.cancel_scheduled_task(str(created["task_id"]))

    assert result["ok"] is True
    task = store.get_task(str(created["task_id"]))
    assert task is not None
    assert task.status == "cancelled"
    listed = await mcp_channel.list_scheduled_tasks()
    assert listed["tasks"] == []


async def test_cancel_scheduled_task_rejects_running_task(scheduling_env: tuple[Path, Path]):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)
    now = datetime.now(UTC)
    task = store.create_task(
        chat_id=TEST_SCHEDULED_CHAT_ID,
        mode="notify",
        notify_text="Old",
        run_at=now - timedelta(minutes=1),
    )
    store.claim_due_tasks(now=now)

    result = await mcp_channel.cancel_scheduled_task(task.id)

    assert result["ok"] is False
    assert result["error"] == "task is currently running; try again after it finishes"


async def test_cancel_scheduled_task_rejects_unknown_task(scheduling_env: tuple[Path, Path]):
    del scheduling_env

    result = await mcp_channel.cancel_scheduled_task("missing")

    assert result["ok"] is False
    assert result["error"] == "task not found"


async def test_cancel_scheduled_task_rejects_other_chat(scheduling_env: tuple[Path, Path]):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)
    task = store.create_task(
        chat_id=TEST_SCHEDULED_CHAT_ID + 1,
        mode="notify",
        notify_text="Old",
        run_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    result = await mcp_channel.cancel_scheduled_task(task.id)

    assert result["ok"] is False
    assert result["error"] == "task not found"


async def test_cancel_scheduled_task_rejects_cancelled_task(scheduling_env: tuple[Path, Path]):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)
    task = store.create_task(
        chat_id=TEST_SCHEDULED_CHAT_ID,
        mode="notify",
        notify_text="Old",
        run_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    store.cancel_task(chat_id=TEST_SCHEDULED_CHAT_ID, task_id=task.id)

    result = await mcp_channel.cancel_scheduled_task(task.id)

    assert result["ok"] is False
    assert "only pending tasks can be cancelled" in cast(str, result["error"])


async def test_cancel_scheduled_task_reports_missing_store(
    scheduling_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    del scheduling_env
    monkeypatch.delenv(ACP_SCHEDULED_TASKS_DB_ENV)

    result = await mcp_channel.cancel_scheduled_task("missing")

    assert result["ok"] is False
    assert "missing" in cast(str, result["error"])


async def test_cancel_scheduled_task_reports_lost_race(scheduling_env: tuple[Path, Path], mocker):
    _, scheduled_db = scheduling_env
    store = make_store(scheduled_db)
    task = store.create_task(
        chat_id=TEST_SCHEDULED_CHAT_ID,
        mode="notify",
        notify_text="Old",
        run_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    mocker.patch.object(store, "cancel_task", return_value=False)
    mocker.patch("acp_chat_bot.mcp.tools.scheduling.load_scheduled_task_store", return_value=store)

    result = await mcp_channel.cancel_scheduled_task(task.id)

    assert result["ok"] is False
    assert result["error"] == "task could not be cancelled"
