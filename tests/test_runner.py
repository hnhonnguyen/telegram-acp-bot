"""Tests for driving several bot instances from one event loop."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telegram.ext import Application

from telegram_acp_bot.instance import BotInstance, build_bot_instance
from telegram_acp_bot.runner import run_bot, run_instances
from telegram_acp_bot.telegram.constants import RESTART_EXIT_CODE
from tests.test_agent_config import base_spec

TEST_APP_TOKEN = "111:qoder"
AGENT_START_ERROR = "the agent command is not on PATH"
SHUTDOWN_ERROR = "the bot is already stopped"
RESTART_COMMAND = ("uv", "run", "telegram-acp-bot")


class _FakeApp:
    """Stands in for a Telegram application the runner starts and stops."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakeBridge:
    def __init__(self, *, restart_requested: bool = False) -> None:
        self.restart_requested = restart_requested


def fake_instance(name: str, *, restart: bool = False, restart_command: tuple[str, ...] | None = None) -> BotInstance:
    """An instance carrying only what the runner reads: a name and a restart flag."""
    return cast(
        BotInstance,
        SimpleNamespace(
            spec=SimpleNamespace(name=name, restart_command_parts=restart_command),
            bridge=_FakeBridge(restart_requested=restart),
        ),
    )


def fake_app_factory(*names: str) -> Callable[[BotInstance], Application]:
    """Build one stand-in application per agent name."""
    apps = {name: _FakeApp(name) for name in names}

    def build(instance: BotInstance) -> Application:
        return cast(Application, apps[instance.spec.name])

    return build


def fleet(*names: str, restart: str | None = None) -> tuple[list[BotInstance], Callable[..., Application]]:
    """The instances of a named set of agents, plus the application factory that fits them."""
    instances = [fake_instance(name, restart=name == restart) for name in names]
    return instances, fake_app_factory(*names)


def patch_lifecycle(mocker, start=None, stop=None):
    """Replace the application lifecycle calls the runner makes, recording shutdowns.

    The signal handlers are neutralized too: a test must not leave a real
    `SIGTERM` handler installed on the loop pytest runs it in.
    """
    started = asyncio.Event()
    stops: list[str] = []
    loop = asyncio.get_running_loop()
    mocker.patch.object(loop, "add_signal_handler", return_value=True)
    mocker.patch.object(loop, "remove_signal_handler", return_value=True)

    def record_start(app):
        if start is not None:
            start(app)
        started.set()

    def record_stop(app):
        if stop is not None:
            stop(app)
        else:
            stops.append(app.name)

    mocker.patch("telegram_acp_bot.runner.start_application", new=mocker.AsyncMock(side_effect=record_start))
    mocker.patch("telegram_acp_bot.runner.stop_application", new=mocker.AsyncMock(side_effect=record_stop))
    return started, stops


@pytest.mark.asyncio
async def test_run_instances_starts_and_stops_every_instance(mocker):
    instances, app_factory = fleet("qoder", "claude")
    started, stops = patch_lifecycle(mocker)
    stop_event = asyncio.Event()
    stop_event.set()

    assert await run_instances(instances, stop_event, app_factory=app_factory) == 0
    assert started.is_set() is True
    assert stops == ["qoder", "claude"]


@pytest.mark.asyncio
async def test_run_instances_builds_a_real_application_per_instance(mocker, tmp_path: Path):
    stop_event = asyncio.Event()
    stop_event.set()
    instances = [
        build_bot_instance(
            base_spec(
                name="qoder",
                token=TEST_APP_TOKEN,
                channel_state_file=tmp_path / "state.json",
                scheduled_tasks_db=tmp_path / "tasks.sqlite3",
            ),
            stop_event=stop_event,
        )
    ]
    apps: list[Application] = []
    patch_lifecycle(mocker, start=apps.append, stop=lambda app: None)

    assert await run_instances(instances, stop_event) == 0
    assert len(apps) == 1
    assert isinstance(apps[0], Application)


@pytest.mark.asyncio
async def test_one_failing_start_stops_the_whole_process(mocker):
    instances, app_factory = fleet("qoder", "claude")

    def fail_on_claude(app):
        if app.name == "claude":
            raise RuntimeError(AGENT_START_ERROR)

    _, stops = patch_lifecycle(mocker, start=fail_on_claude)
    stop_event = asyncio.Event()

    assert await run_instances(instances, stop_event, app_factory=app_factory) == 1
    assert stop_event.is_set() is True
    assert stops == ["qoder"]


@pytest.mark.asyncio
async def test_a_failing_shutdown_is_reported(mocker):
    instances, app_factory = fleet("qoder")

    def fail_stop(app):
        raise RuntimeError(SHUTDOWN_ERROR)

    patch_lifecycle(mocker, stop=fail_stop)
    stop_event = asyncio.Event()
    stop_event.set()

    assert await run_instances(instances, stop_event, app_factory=app_factory) == 1


@pytest.mark.asyncio
async def test_a_restart_request_becomes_the_restart_exit_code(mocker):
    instances, app_factory = fleet("qoder", restart="qoder")
    patch_lifecycle(mocker)
    stop_event = asyncio.Event()
    stop_event.set()

    assert await run_instances(instances, stop_event, app_factory=app_factory) == RESTART_EXIT_CODE


@pytest.mark.asyncio
async def test_termination_signals_stop_every_instance(mocker):
    instances, app_factory = fleet("qoder")
    started, stops = patch_lifecycle(mocker)
    handlers: dict[int, Callable[[], None]] = {}
    removed: list[int] = []

    def add_handler(sig, callback, *args):
        del args
        handlers[sig] = callback

    # Applied after `patch_lifecycle`, whose inert patches it replaces.
    loop = asyncio.get_running_loop()

    def remove_handler(sig):
        removed.append(sig)

    mocker.patch.object(loop, "add_signal_handler", side_effect=add_handler)
    mocker.patch.object(loop, "remove_signal_handler", side_effect=remove_handler)

    running = asyncio.create_task(run_instances(instances, asyncio.Event(), app_factory=app_factory))
    await started.wait()
    handlers[signal.SIGINT]()

    assert await running == 0
    assert stops == ["qoder"]
    assert removed == [signal.SIGINT, signal.SIGTERM]


@pytest.mark.asyncio
async def test_instances_survive_a_loop_without_signal_support(mocker):
    instances, app_factory = fleet("qoder")
    patch_lifecycle(mocker)
    loop = asyncio.get_running_loop()
    mocker.patch.object(loop, "add_signal_handler", side_effect=NotImplementedError)
    stop_event = asyncio.Event()
    stop_event.set()

    assert await run_instances(instances, stop_event, app_factory=app_factory) == 0


def test_run_bot_returns_a_normal_exit_code(mocker):
    mock_run_instances = mocker.patch(
        "telegram_acp_bot.runner.run_instances", new_callable=mocker.AsyncMock, return_value=0
    )

    assert run_bot([fake_instance("qoder")], asyncio.Event()) == 0
    mock_run_instances.assert_awaited_once()


def test_run_bot_reexecs_the_restart_command_of_the_requesting_agent(mocker):
    instances = [fake_instance("qoder", restart=True, restart_command=RESTART_COMMAND), fake_instance("claude")]

    def request_restart(instances, stop_event):
        del instances, stop_event
        return RESTART_EXIT_CODE

    mocker.patch("telegram_acp_bot.runner.run_instances", new_callable=mocker.AsyncMock, side_effect=request_restart)
    execvp = mocker.patch("telegram_acp_bot.runner.os.execvp", side_effect=OSError("execvp failed"))

    assert run_bot(instances, asyncio.Event()) == 1
    execvp.assert_called_once_with("uv", list(RESTART_COMMAND))


def test_run_bot_reexecs_the_current_process_without_a_restart_command(mocker):
    instances = [fake_instance("qoder", restart=True)]

    def request_restart(instances, stop_event):
        del instances, stop_event
        return RESTART_EXIT_CODE

    mocker.patch("telegram_acp_bot.runner.run_instances", new_callable=mocker.AsyncMock, side_effect=request_restart)
    execvp = mocker.patch("telegram_acp_bot.runner.os.execvp")
    execv = mocker.patch("telegram_acp_bot.runner.os.execv", side_effect=OSError("exec failed"))
    mocker.patch("telegram_acp_bot.runner.sys.argv", ["telegram-acp-bot", "--agent", "qoder=agent"])
    mocker.patch("telegram_acp_bot.runner.sys.executable", "/usr/bin/python3")

    assert run_bot(instances, asyncio.Event()) == 1
    execvp.assert_not_called()
    execv.assert_called_once_with(
        "/usr/bin/python3",
        ["/usr/bin/python3", "telegram-acp-bot", "--agent", "qoder=agent"],
    )
