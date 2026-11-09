"""Run the deprecated package alias as a module."""

from __future__ import annotations

from acp_chat_bot.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
