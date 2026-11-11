"""Run every bot instance of one process from a single event loop."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Callable, Sequence

from telegram.ext import Application

from telegram_acp_bot.instance import BotInstance
from telegram_acp_bot.logging_context import bind_log_context
from telegram_acp_bot.telegram.app import build_application, start_application, stop_application
from telegram_acp_bot.telegram.constants import RESTART_EXIT_CODE

logger = logging.getLogger(__name__)

_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _application_for(instance: BotInstance) -> Application:
    """Build one instance's Telegram application, installing its handlers."""
    return build_application(instance.config, instance.bridge, instance.scheduler)


async def run_instances(
    instances: Sequence[BotInstance],
    stop_event: asyncio.Event,
    *,
    app_factory: Callable[[BotInstance], Application] = _application_for,
) -> int:
    """Start every instance, wait for one stop request, then tear all of them down.

    A failing instance stops the whole process rather than leaving a half fleet
    running: an agent that died silently still answers its own chat with nothing,
    and systemd brings everything back a second later anyway.
    """
    installed = _install_stop_handlers(stop_event)
    try:
        outcomes = await asyncio.gather(
            *(_drive_instance(instance.spec.name, app_factory(instance), stop_event) for instance in instances)
        )
    finally:
        _restore_stop_handlers(installed)
    if any(instance.bridge.restart_requested for instance in instances):
        return RESTART_EXIT_CODE
    return 0 if all(outcomes) else 1


async def _drive_instance(name: str, app: Application, stop_event: asyncio.Event) -> bool:
    """Run one application until the shared stop event fires; `False` when it failed."""
    with bind_log_context(agent=name):
        try:
            await start_application(app)
        except Exception:
            logger.exception("Agent %s failed to start; stopping the process", name)
            stop_event.set()
            return False
        try:
            await stop_event.wait()
        finally:
            try:
                await stop_application(app)
            except Exception:
                logger.exception("Agent %s failed to shut down cleanly", name)
                return False
        return True


def _install_stop_handlers(stop_event: asyncio.Event) -> tuple[int, ...]:
    """Ask `stop_event` on the usual termination signals, as blocking polling used to."""
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    for sig in _STOP_SIGNALS:
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            logger.warning("This event loop cannot handle %s; stop the bot with /restart", sig)
            continue
        installed.append(sig)
    return tuple(installed)


def _restore_stop_handlers(installed: Sequence[int]) -> None:
    """Give the signals back to whatever the process did before this loop ran."""
    loop = asyncio.get_running_loop()
    for sig in installed:
        loop.remove_signal_handler(sig)


def _requested_restart_command(instances: Sequence[BotInstance]) -> Sequence[str] | None:
    """The re-exec command of the agent that asked for a restart, if any."""
    return next(
        (instance.spec.restart_command_parts for instance in instances if instance.bridge.restart_requested),
        None,
    )


def run_bot(instances: Sequence[BotInstance], stop_event: asyncio.Event) -> int:
    """Poll every instance until normal exit; re-exec the process on a restart request."""
    while True:
        exit_code = asyncio.run(run_instances(instances, stop_event))
        if exit_code != RESTART_EXIT_CODE:
            return exit_code
        restart_command_parts = _requested_restart_command(instances)
        try:
            if restart_command_parts is not None:
                logger.info(
                    "Restart requested via Telegram command. Re-execing restart command: %s",
                    restart_command_parts,
                )
                os.execvp(restart_command_parts[0], list(restart_command_parts))
            # TODO: Drop manual re-exec when uv can natively watch local package code and
            # restart commands for this workflow. Tracking: astral-sh/uv#9652.
            argv = [sys.executable, *sys.argv]
            logger.info("Restart requested via Telegram command. Re-execing: %s", argv)
            os.execv(sys.executable, argv)
        except OSError:
            logger.exception("Failed to re-exec process after /restart")
            return 1


__all__: list[str] = ["run_bot", "run_instances"]
