"""Lifecycle tests for the Telegram application helpers."""

from __future__ import annotations

# ruff: noqa: F403, F405, I001

from tests.telegram.support import *


class _RecordingUpdater:
    """Captures the polling calls `start_application` and `stop_application` make."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def start_polling(self, *, allowed_updates):
        self._calls.append(f"start_polling:{allowed_updates}")

    async def stop(self):
        self._calls.append("updater.stop")


class _RecordingApplication:
    def __init__(self, calls: list[str], *, post_init=None, post_shutdown=None) -> None:
        self._calls = calls
        self.updater = _RecordingUpdater(calls)
        self.post_init = post_init
        self.post_shutdown = post_shutdown

    async def initialize(self):
        self._calls.append("initialize")

    async def start(self):
        self._calls.append("start")

    async def stop(self):
        self._calls.append("app.stop")

    async def shutdown(self):
        self._calls.append("shutdown")


async def test_start_application_follows_the_blocking_polling_order():
    calls: list[str] = []

    async def post_init(_):
        calls.append("post_init")

    app = _RecordingApplication(calls, post_init=post_init)

    await start_application(cast(Application, app))

    assert calls == ["initialize", "post_init", f"start_polling:{Update.ALL_TYPES}", "start"]


async def test_start_application_without_a_post_init_hook():
    calls: list[str] = []
    app = _RecordingApplication(calls)

    await start_application(cast(Application, app))

    assert calls == ["initialize", f"start_polling:{Update.ALL_TYPES}", "start"]


async def test_stop_application_follows_the_blocking_polling_order():
    calls: list[str] = []

    async def post_shutdown(_):
        calls.append("post_shutdown")

    app = _RecordingApplication(calls, post_shutdown=post_shutdown)

    await stop_application(cast(Application, app))

    assert calls == ["updater.stop", "app.stop", "shutdown", "post_shutdown"]


async def test_stop_application_without_a_post_shutdown_hook():
    calls: list[str] = []
    app = _RecordingApplication(calls)

    await stop_application(cast(Application, app))

    assert calls == ["updater.stop", "app.stop", "shutdown"]
