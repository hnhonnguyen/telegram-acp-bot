"""Compatibility shims for agent-client-protocol API drift.

`acp` 1.0 release candidates removed top-level `text_block` and the
`acp.helpers` module that 0.x releases provided. Re-export them here so
the rest of the codebase tracks either API generation.
"""

__all__ = ["text_block", "update_available_commands"]

try:
    from acp import text_block
except ImportError:  # pragma: no cover
    from acp.schema import TextContentBlock

    def text_block(text: str) -> TextContentBlock:
        return TextContentBlock(type="text", text=text)


try:
    from acp.helpers import update_available_commands  # ty: ignore[unresolved-import]
except ImportError:  # pragma: no cover
    from acp.schema import AvailableCommand, AvailableCommandsUpdate

    def update_available_commands(commands: list[AvailableCommand]) -> AvailableCommandsUpdate:
        return AvailableCommandsUpdate(available_commands=commands, session_update="available_commands_update")
