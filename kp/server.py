"""Read-only MCP adapter. No tool writes to the vault or to the index.

Run: KP_DB=/path/index.db uv run --with 'mcp<2' python -m kp.server
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import query

DB = os.environ["KP_DB"]
mcp = FastMCP("knowledge")


@mcp.tool()
def search(query_text: str, limit: int = 10) -> dict:
    """Full-text (BM25) search over notes. Exact hostnames, IPs and versions match literally."""
    return query.search(query.open_ro(DB), query_text, min(limit, 50))


@mcp.tool()
def read(path: str, heading: str | None = None) -> dict:
    """Read a note, or one section of it by heading, by its vault-relative path."""
    return query.read_note(query.open_ro(DB), path, heading)


@mcp.tool()
def related(path: str) -> dict:
    """Parents, children, outgoing links and backlinks of a note."""
    return query.related(query.open_ro(DB), path)


@mcp.tool()
def status() -> dict:
    """Index health: counts, unresolved links, parent cycles, last sync time."""
    return query.status(query.open_ro(DB))


if __name__ == "__main__":
    mcp.run()
