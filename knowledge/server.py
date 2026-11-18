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

from . import auth, embeddings, index, query
from .proposals import GitHub, ProposalError, Proposer

VAULT = os.environ.get("KNOWLEDGE_VAULT", "/vault")
DB = os.environ.get("KNOWLEDGE_DB", "/data/knowledge.db")
# Folders to index; the default keeps the diary and the library out of reach of every tailnet node.
INCLUDE = [p.strip() for p in os.environ.get("KNOWLEDGE_INCLUDE", "01 Архив").split(",") if p.strip()]
INTERVAL = int(os.environ.get("KNOWLEDGE_SYNC_INTERVAL", "300"))

# Shown to the client's model whenever the server is connected: what this is and how to use it.
INSTRUCTIONS = """\
BLUF: Main memory layer. Search it before guessing.
CONTENT: Owner's notes and agent memory (agent_memory: true) on projects: hosts, services, people, decisions, reasons.
FIND: search (parent="<project hub>" = one project). read = full note. related = parents, children, links.
ADD: propose_note = new, propose_edit = correction. Each opens a PR; invisible to search until merged."""

mcp = MCPServer("knowledge", instructions=INSTRUCTIONS)
# Semantic search is opt-in: KNOWLEDGE_SEMANTIC=on loads the model (about 800 MB of RAM); otherwise the
# server is keyword-only and never touches it.
embedder = (embeddings.Embedder(os.environ.get("KNOWLEDGE_MODEL_DIR"))
            if os.environ.get("KNOWLEDGE_SEMANTIC", "off") == "on" else None)


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
    gh = GitHub(os.environ.get("KNOWLEDGE_GITHUB_REPO", "CosmDandy/knowledge"), token)
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
def search(text: str, limit: int = 10, memory: str = "all", parent: str | None = None) -> str:
    """Find notes by words (BM25). Exact names, hostnames and IPs match literally.

    text: the query. limit: 1-50 (default 10).
    memory: "all" (default), "agent" (only agent notes) or "owner" (only the owner's).
    parent: a project hub note (title, alias or path); searches it and everything under it.
    Each hit has path, title, heading, snippet, agent_memory.
    """
    return _ro(query.search, text, min(limit, 50), memory, "hybrid", embedder, parent)


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
def propose_note(topic: str, title: str, body: str, parent: str | list[str] | None = None,
                 links: list[str] | None = None, aliases: list[str] | None = None) -> str:
    """Propose a NEW note (opens or extends a pull request). Send the body without frontmatter;
    never put credentials in it.

    topic: one per subject; notes with the same topic share one PR.
    title: no colon (use an em dash). parent: the project hub name. links/aliases: optional.
    """
    if proposer is None:
        return _WRITE_OFF
    try:
        result = proposer.propose_note(topic, title, body, parent, links, aliases)
    except ProposalError as e:
        return _j({"error": str(e)})
    if embedder:
        try:  # advisory only: the proposal is already made, a failed check must not hide it
            con = query.open_ro(DB)
            try:
                result["similar_existing"] = query.similar(con, f"{title}\n{body}", embedder)
            finally:
                con.close()
        except Exception as e:  # noqa: BLE001
            print(f"similar check failed: {type(e).__name__}", file=sys.stderr, flush=True)
    return _j(result)


@mcp.tool()
def propose_edit(topic: str, path: str, body: str, reason: str | None = None) -> str:
    """Propose a change to an EXISTING note. Send the complete new body without frontmatter;
    tags are never changed. Never put credentials in it.

    path: as returned by search. reason: one line, optional.
    """
    if proposer is None:
        return _WRITE_OFF
    try:
        return _j(proposer.propose_edit(topic, path, body, reason or ""))
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
    while True:  # the first pass runs at once: it loads the model and embeds what the quick startup sync left
        # A broad except is deliberate: a failed tick must not kill the thread, the next one retries.
        try:
            started = time.monotonic()
            result = index.sync(VAULT, DB, include=INCLUDE, embedder=embedder)
            if result["embedded"]:
                print(f"embedded {result['embedded']} chunks in {time.monotonic() - started:.0f}s",
                      file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"sync failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr, flush=True)
        if proposer:
            try:
                deleted = proposer.cleanup()
                if deleted:
                    print(f"deleted branches: {deleted}", file=sys.stderr, flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"cleanup failed: {type(e).__name__}", file=sys.stderr, flush=True)
        time.sleep(INTERVAL)


def main() -> None:
    index.sync(VAULT, DB, include=INCLUDE)
    mcp.run(transport="stdio")


def main_http() -> None:
    import anyio
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    result = index.sync(VAULT, DB, include=INCLUDE)  # words first: search works while vectors are still computed
    print(f"indexed {result['total']} notes from {INCLUDE or 'whole vault'}; "
          f"writing {'enabled' if proposer else 'disabled'}; semantic {'on' if embedder else 'off'}",
          file=sys.stderr, flush=True)
    threading.Thread(target=_maintenance, daemon=True).start()
    host = os.environ.get("KNOWLEDGE_HOST", "127.0.0.1")
    # The port is published on the tailnet address only, and every request needs the bearer token.
    app = mcp.streamable_http_app(
        host=host,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    if os.environ.get("KNOWLEDGE_AUTH", "on") != "off":
        app = auth.BearerAuth(app, auth.load_token())
    else:
        print("WARNING: auth is off, anyone who reaches the port can read and propose notes",
              file=sys.stderr, flush=True)
    config = uvicorn.Config(app, host=host, port=int(os.environ.get("KNOWLEDGE_PORT", "7234")),
                            log_level="info")
    anyio.run(uvicorn.Server(config).serve)


if __name__ == "__main__":
    main_http()
