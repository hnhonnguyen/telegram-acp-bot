"""Compatibility shims for mcp SDK moves.

`FastMCP` lived under `mcp.server.fastmcp`, was briefly re-exported as
`mcp.server.MCPServer`, and moved back when FastMCP merged into the SDK.
Re-export it here so the codebase tracks any of those releases.
"""

__all__ = ["FastMCP"]

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    from mcp.server import MCPServer as FastMCP  # ty: ignore[unresolved-import]
