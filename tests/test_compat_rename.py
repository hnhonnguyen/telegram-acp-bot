from __future__ import annotations

import importlib
import sys

import pytest


def test_deprecated_package_forwards_modules_and_classes():
    sys.modules.pop("telegram_acp_bot", None)
    sys.modules.pop("telegram_acp_bot.channels", None)
    sys.modules.pop("telegram_acp_bot.channels.telegram", None)
    sys.modules.pop("telegram_acp_bot.channels.telegram.bridge", None)

    with pytest.warns(DeprecationWarning):
        old = importlib.import_module("telegram_acp_bot")

    new = importlib.import_module("acp_chat_bot")
    old_bridge = importlib.import_module("telegram_acp_bot.channels.telegram.bridge")
    new_bridge = importlib.import_module("acp_chat_bot.channels.telegram.bridge")

    assert old.main is new.main  # ty: ignore[unresolved-attribute]
    assert old.get_parser is new.get_parser  # ty: ignore[unresolved-attribute]
    assert old_bridge.TelegramBridge is new_bridge.TelegramBridge
