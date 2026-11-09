"""Lifecycle contract for one channel runtime."""

from __future__ import annotations

from typing import Protocol


class ChannelRuntime(Protocol):
    """Start and stop one channel without leaking its transport details."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
