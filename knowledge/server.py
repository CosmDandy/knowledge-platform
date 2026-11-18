"""MCP server over streamable HTTP.

Reads the vault read-only. The only write path is propose_note, which opens a pull request on
GitHub; without KNOWLEDGE_GITHUB_TOKEN the write tools are disabled and reading still works.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

from mcp.server.mcpserver import MCPServer

from . import index, query
from .proposals import GitHub, ProposalError, Proposer

VAULT = os.environ.get("KNOWLEDGE_VAULT", "/vault")
DB = os.environ.get("KNOWLEDGE_DB", "/data/knowledge.db")
# Folders to index; the default keeps the diary and the library out of reach of every tailnet node.
INCLUDE = [p.strip() for p in os.environ.get("KNOWLEDGE_INCLUDE", "01 Архив").split(",") if p.strip()]
INTERVAL = int(os.environ.get("KNOWLEDGE_SYNC_INTERVAL", "300"))

mcp = MCPServer("knowledge")


def _title_exists(title: str) -> bool:
    con = query.open_ro(DB)
    try:
        return con.execute("SELECT 1 FROM notes WHERE lower(title) = lower(?)", (title,)).fetchone() is not None
    finally:
        con.close()


def _make_proposer() -> Proposer | None:
    token = os.environ.get("KNOWLEDGE_GITHUB_TOKEN")
    if not token:
        return None
    gh = GitHub(os.environ.get("KNOWLEDGE_GITHUB_REPO", "CosmDandy/KnowledgeBase"), token)
    return Proposer(
        gh,
        prefix=os.environ.get("KNOWLEDGE_BRANCH_PREFIX", "knowledge"),
        base=os.environ.get("KNOWLEDGE_BASE_BRANCH", "master"),
        folder=os.environ.get("KNOWLEDGE_NOTES_FOLDER", "01 Архив"),
        max_open=int(os.environ.get("KNOWLEDGE_MAX_OPEN_PRS", "5")),
        max_files=int(os.environ.get("KNOWLEDGE_MAX_FILES_PER_PR", "20")),
        exists=_title_exists,
    )


proposer = _make_proposer()
_WRITE_OFF = json.dumps({"error": "writing is disabled: KNOWLEDGE_GITHUB_TOKEN is not set"})


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _ro(fn, *args):
    con = query.open_ro(DB)
    try:
        return _j(fn(con, *args))
    finally:
        con.close()


@mcp.tool()
def search(text: str, limit: int = 10) -> str:
    """Full-text (BM25) search over notes. Exact hostnames, IPs and versions match literally."""
    return _ro(query.search, text, min(limit, 50))


@mcp.tool()
def read(path: str, heading: str | None = None) -> str:
    """Read a note, or one section of it by heading, by its vault-relative path."""
    return _ro(query.read_note, path, heading)


@mcp.tool()
def related(path: str) -> str:
    """Parents, children, outgoing links and backlinks of a note."""
    return _ro(query.related, path)


@mcp.tool()
def status() -> str:
    """Index health: counts, unresolved links, parent cycles, last sync time."""
    return _ro(query.status)


@mcp.tool()
def propose_note(topic: str, title: str, body: str, parent: str | None = None,
                 sources: list[str] | None = None) -> str:
    """Propose a NEW note. It lands in a pull request, never directly in the vault.

    All notes with the same topic share one branch and one PR, so pick one topic per subject
    and reuse it. Existing notes cannot be changed or deleted this way. `parent` is a plain
    note name; `sources` says where the facts come from. Never put credentials in the body.
    """
    if proposer is None:
        return _WRITE_OFF
    try:
        return _j(proposer.propose_note(topic, title, body, parent, sources))
    except ProposalError as e:
        return _j({"error": str(e)})


@mcp.tool()
def list_proposals() -> str:
    """Open proposals (one PR per topic) waiting for review."""
    if proposer is None:
        return _WRITE_OFF
    try:
        return _j(proposer.list_open())
    except ProposalError as e:
        return _j({"error": str(e)})


def _maintenance() -> None:
    """Reindex and delete finished topic branches. Logs never contain note text."""
    while True:
        time.sleep(INTERVAL)
        # A broad except is deliberate: a failed tick must not kill the thread, the next one retries.
        try:
            index.sync(VAULT, DB, include=INCLUDE)
        except Exception as e:  # noqa: BLE001
            print(f"sync failed: {type(e).__name__}", file=sys.stderr, flush=True)
        if proposer:
            try:
                deleted = proposer.cleanup()
                if deleted:
                    print(f"deleted branches: {deleted}", file=sys.stderr, flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"cleanup failed: {type(e).__name__}", file=sys.stderr, flush=True)


def main() -> None:
    index.sync(VAULT, DB, include=INCLUDE)
    mcp.run(transport="stdio")


def main_http() -> None:
    import anyio
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    result = index.sync(VAULT, DB, include=INCLUDE)
    print(f"indexed {result['total']} notes from {INCLUDE or 'whole vault'}; "
          f"writing {'enabled' if proposer else 'disabled'}", file=sys.stderr, flush=True)
    threading.Thread(target=_maintenance, daemon=True).start()
    host = os.environ.get("KNOWLEDGE_HOST", "127.0.0.1")
    # NOTE: no auth by design; the container port is published on the tailnet address only.
    app = mcp.streamable_http_app(
        host=host,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    config = uvicorn.Config(app, host=host, port=int(os.environ.get("KNOWLEDGE_PORT", "7234")),
                            log_level="info")
    anyio.run(uvicorn.Server(config).serve)


if __name__ == "__main__":
    main_http()
