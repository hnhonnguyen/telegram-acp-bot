"""Deprecated compatibility package for :mod:`acp_chat_bot`.

The project was renamed from ``telegram-acp-bot`` to ``acp-chat-bot`` once DingTalk
became a second channel. This shim keeps ``import telegram_acp_bot`` and its
submodules working during the deprecation window; it forwards every module to the
corresponding :mod:`acp_chat_bot` module so classes keep their identity.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys
import types
import warnings
from typing import Any

_OLD = "telegram_acp_bot"
_NEW = "acp_chat_bot"


class _AliasFinder:
    """Import ``telegram_acp_bot.*`` as a proxy around ``acp_chat_bot.*``."""

    def find_spec(
        self,
        fullname: str,
        path: object | None = None,
        target: object | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        del path, target
        if fullname == _OLD or not fullname.startswith(f"{_OLD}."):
            return None
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(fullname))

    def find_module(self, fullname: str, path: object | None = None) -> object | None:
        del path
        return _AliasLoader(fullname) if fullname.startswith(f"{_OLD}.") else None


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, fullname: str) -> None:
        self.fullname = fullname

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType:
        del spec
        target_name = _NEW + self.fullname[len(_OLD) :]
        target = importlib.import_module(target_name)
        module = types.ModuleType(self.fullname)
        module.__dict__.update(target.__dict__)
        return module

    def exec_module(self, module: types.ModuleType) -> None:
        del module


warnings.warn(
    "telegram_acp_bot is deprecated; install acp-chat-bot and import acp_chat_bot instead",
    DeprecationWarning,
    stacklevel=2,
)

if not any(isinstance(finder, _AliasFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())

_forwarded: dict[str, Any] = importlib.import_module(_NEW).__dict__
for _name, _value in _forwarded.items():
    if not _name.startswith("__"):
        globals()[_name] = _value

__all__: list[str] = [name for name in _forwarded if not name.startswith("__")]
