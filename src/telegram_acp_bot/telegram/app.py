"""Application factory and polling lifecycle for the Telegram transport."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, cast

from telegram import Update
from telegram.ext import AIORateLimiter, Application, Updater

from telegram_acp_bot.scheduled_tasks.scheduler import ScheduledTaskScheduler
from telegram_acp_bot.telegram.bridge import TelegramBridge
from telegram_acp_bot.telegram.config import BotConfig


def build_application(
    config: BotConfig,
    bridge: TelegramBridge,
    scheduler: ScheduledTaskScheduler | None = None,
) -> Application:
    # Permission prompts are awaited inside message handlers, so callback queries
    # must be processed concurrently to avoid deadlocking the update loop.
    # max_retries=3: when Telegram returns RetryAfter (flood control),
    # the rate limiter waits the requested seconds and retries up to 3
    # times instead of dropping the message immediately (the default
    # max_retries=0 causes silent message loss).
    builder = (
        Application.builder().token(config.token).rate_limiter(AIORateLimiter(max_retries=3)).concurrent_updates(True)
    )
    if scheduler is not None:
        builder = builder.post_init(_post_init_factory(scheduler)).post_shutdown(_post_shutdown_factory(scheduler))
    app = builder.build()
    bridge.install(app)
    return app


async def start_application(app: Application) -> None:
    """Bring one application up to fetching updates, in the order `run_polling` uses.

    Split out of the blocking entrypoint so several applications can share one
    event loop. `{py:func}telegram_acp_bot.runner.run_instances` drives them.
    """
    await app.initialize()
    if app.post_init is not None:
        await app.post_init(app)
    # `build_application` always passes a token, which is what creates the updater.
    await cast(Updater, app.updater).start_polling(allowed_updates=Update.ALL_TYPES)
    await app.start()


async def stop_application(app: Application) -> None:
    """Take a started application back down, in the order `run_polling` uses."""
    await cast(Updater, app.updater).stop()
    await app.stop()
    await app.shutdown()
    if app.post_shutdown is not None:
        await app.post_shutdown(app)


def _post_init_factory(scheduler: ScheduledTaskScheduler) -> Callable[[Application], Coroutine[Any, Any, None]]:
    async def _post_init(_: Application) -> None:
        await scheduler.start()

    return _post_init


def _post_shutdown_factory(scheduler: ScheduledTaskScheduler) -> Callable[[Application], Coroutine[Any, Any, None]]:
    async def _post_shutdown(_: Application) -> None:
        await scheduler.stop()

    return _post_shutdown
