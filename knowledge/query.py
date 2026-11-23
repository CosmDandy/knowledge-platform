"""Read-only queries over the index. Every function opens the database read-only."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from . import embeddings
from .index import HEADING, parent_cycles


def open_ro(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _match(terms: list[str], op: str) -> str:
    return f" {op} ".join('"' + t.replace('"', '""') + '"' for t in terms)


AGENT_TAG = "agent-memory"
# Constant fragments only: `memory` is checked against this table, never interpolated as given.
_MEMORY_FILTER = {
    "all": "",
    "agent": f" AND n.tags LIKE '%\"{AGENT_TAG}\"%'",
    "owner": f" AND n.tags NOT LIKE '%\"{AGENT_TAG}\"%'",
}


MODES = ("hybrid", "keyword", "semantic")
RRF_K = 60  # reciprocal rank fusion constant: larger flattens the gap between ranks
# e5-small cosines are compressed: on the real vault right notes sit at 0.83-0.88 and wrong ones at 0.80-0.90.
# 0.86 maximised hybrid MRR on 20 hand-labelled queries and let no off-topic query through; re-measure after
# changing the model or the chunking.
SEMANTIC_MIN = float(os.environ.get("KNOWLEDGE_SEMANTIC_MIN", "0.86"))
CANDIDATES = 50  # chunks taken from each ranker before fusion

_HIT_COLUMNS = "f.note_id, f.heading, substr(f.body, 1, 240) AS head, n.hash, n.indexed_at, n.title, n.tags"


# The hub and everything under it through parent links, at any depth. UNION keeps parent cycles finite.
_SUBTREE = ("WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT e.src FROM edges e JOIN d ON e.dst_id = d.id "
            "WHERE e.kind = 'CHILD_OF') SELECT id FROM d")


def _find_hub(con: sqlite3.Connection, name: str) -> str | None:
    """A note by vault path, title or alias (case-insensitive); None when nothing or several match."""
    name = name.strip().removeprefix("[[").removesuffix("]]")
    for sql in ("SELECT id FROM notes WHERE id = ?", "SELECT id FROM notes WHERE lower(title) = lower(?)",
                "SELECT id FROM notes WHERE lower(aliases) LIKE '%' || lower(json_quote(?)) || '%'"):
        found = [r[0] for r in con.execute(sql, (name,))]
        if len(found) == 1:
            return found[0]
    return None


def _scope(memory: str, hub: str | None) -> tuple[str, list]:
    """SQL fragment (on alias n) and its parameters for the memory and parent filters."""
    sql, params = _MEMORY_FILTER[memory], []
    if hub:
        sql += f" AND n.id IN ({_SUBTREE})"
        params.append(hub)
    return sql, params


def _keyword_hits(con: sqlite3.Connection, terms: list[str], scope: tuple[str, list]
                  ) -> tuple[list[sqlite3.Row], str]:
    for op in ("AND", "OR"):  # fall back to OR only when the strict query finds nothing
        match = _match(terms, op)
        rows = con.execute(
            f"SELECT f.rowid AS chunk, {_HIT_COLUMNS}, "
            "bm25(fts, 0, 0, 5.0, 3.0, 1.0) AS score "
            "FROM fts f JOIN notes n ON n.id = f.note_id "
            "WHERE fts MATCH ?" + scope[0] + " ORDER BY score LIMIT ?",
            (match, *scope[1], CANDIDATES),
        ).fetchall()
        if rows:
            return rows, match
    return [], ""


def _snippets(con: sqlite3.Connection, match: str, chunks: list[int]) -> dict[int, str]:
    if not chunks:
        return {}
    placeholders = ",".join("?" for _ in chunks)
    return dict(con.execute(
        "SELECT rowid, snippet(fts, 4, '[', ']', '…', 24) FROM fts "
        f"WHERE fts MATCH ? AND rowid IN ({placeholders})", (match, *chunks)))


def _semantic_hits(con: sqlite3.Connection, query: str, scope: tuple[str, list],
                   embedder) -> list[tuple[int, str, float]]:
    allowed = None
    if scope[0]:
        allowed = {r[0] for r in con.execute("SELECT n.id FROM notes n WHERE 1=1" + scope[0], scope[1])}
    vector = embedder.embed_query(query)
    return [h for h in embeddings.nearest(con, vector, CANDIDATES, allowed) if h[2] >= SEMANTIC_MIN]


def search(con: sqlite3.Connection, query: str, limit: int = 10, memory: str = "all",
           mode: str = "hybrid", embedder=None, parent: str | None = None) -> dict:
    """Find notes by words (BM25), by meaning (e5 vectors) or both fused by rank.

    Keyword terms are quoted, so hostnames and IPs match literally. `memory` narrows by who keeps the note:
    "agent" (tagged agent-memory), "owner" (the rest), "all". Without an embedder (semantic search is off) hybrid
    is plain keyword search and mode "semantic" is an error; when the embedder fails, the answer is keyword-only
    and carries `"semantic": "unavailable"`. `parent` (a note's title, alias or path)
    keeps only that note and everything under it through parent links, at any depth: one project's notes.
    """
    if memory not in _MEMORY_FILTER:
        return {"error": f"memory must be one of {sorted(_MEMORY_FILTER)}"}
    if mode not in MODES:
        return {"error": f"mode must be one of {list(MODES)}"}
    if embedder is None:
        if mode == "semantic":
            return {"error": "semantic search is off on this server (KNOWLEDGE_SEMANTIC=on enables it)"}
        mode = "keyword"
    terms = re.findall(r"\S+", query)
    if not terms:
        return {"results": []}
    hub = None
    if parent:
        hub = _find_hub(con, parent)
        if hub is None:
            return {"error": f"parent {parent!r} is not a single known note (use its title, alias or path)"}
    scope = _scope(memory, hub)
    if con.in_transaction:
        return _search_matches(con, query, terms, limit, mode, embedder, scope)
    # Keep the ranked rowids and deferred snippets on the same index snapshot.
    con.execute("BEGIN")
    try:
        return _search_matches(con, query, terms, limit, mode, embedder, scope)
    finally:
        con.execute("ROLLBACK")


def _search_matches(con: sqlite3.Connection, query: str, terms: list[str], limit: int,
                    mode: str, embedder, scope: tuple[str, list]) -> dict:
    extra: dict = {}
    keyword, match = _keyword_hits(con, terms, scope) if mode != "semantic" else ([], "")
    semantic: list[tuple[int, str, float]] = []
    if mode != "keyword":
        try:
            semantic = _semantic_hits(con, query, scope, embedder)
        except Exception:  # noqa: BLE001 - a broken model must not take keyword search down with it
            extra["semantic"] = "unavailable"
            if mode == "semantic":
                return {"results": [], **extra}

    # Rank notes, not chunks: a note's place in a list is where its best chunk stands.
    fused: dict[str, dict] = {}
    for position, r in enumerate(keyword):
        entry = fused.setdefault(r["note_id"], {"score": 0.0, "bm25": None, "similarity": None, "row": r})
        if entry["bm25"] is None:
            entry["score"] += 1 / (RRF_K + position + 1)
            entry["bm25"] = round(r["score"], 3)
    for position, (chunk, note_id, similarity) in enumerate(semantic):
        entry = fused.setdefault(note_id, {"score": 0.0, "bm25": None, "similarity": None, "row": None})
        if entry["similarity"] is None:
            entry["score"] += 1 / (RRF_K + position + 1)
            entry["similarity"] = round(similarity, 3)
            entry["chunk"] = chunk
    selected = sorted(fused.items(), key=lambda kv: -kv[1]["score"])[:limit]
    snippets = _snippets(con, match, [e["row"]["chunk"] for _, e in selected if e["row"] is not None])
    results = []
    for note_id, e in selected:
        r = e["row"]
        if r is None:  # found by meaning only: take the chunk that matched
            r = con.execute(f"SELECT {_HIT_COLUMNS} FROM fts f JOIN notes n ON n.id = f.note_id "
                            "WHERE f.rowid = ?", (e["chunk"],)).fetchone()
            snip = ""
        else:
            snip = snippets[r["chunk"]]
        # A title-only match leaves the body without a highlight: show where the text starts instead.
        snippet = snip if "[" in snip else " ".join(r["head"].split())
        tags = json.loads(r["tags"])
        results.append({
            "path": note_id, "title": r["title"], "heading": r["heading"], "snippet": snippet,
            "tags": tags, "agent_memory": AGENT_TAG in tags,
            "rank": len(results) + 1,  # 1 is the best; bm25 (lower is better) and similarity are raw scores
            "reason": "both" if e["bm25"] is not None and e["similarity"] is not None
            else "keyword" if e["bm25"] is not None else "semantic",
            "bm25": e["bm25"], "similarity": e["similarity"],
            "source_hash": r["hash"], "indexed_at": r["indexed_at"],
        })
    return {"results": results, **extra}


# Unrelated notes reach 0.90 here, so only near-copies count as duplicates. TODO: not validated on real duplicates.
SIMILAR_MIN = float(os.environ.get("KNOWLEDGE_SIMILAR_MIN", "0.92"))


def similar(con: sqlite3.Connection, text: str, embedder, limit: int = 3) -> list[dict]:
    """Existing notes close in meaning to `text`, best first: a duplicate check before a new note."""
    best: dict[str, float] = {}
    for _, note_id, similarity in embeddings.nearest(con, embedder.embed_query(text), CANDIDATES):
        if similarity >= SIMILAR_MIN and note_id not in best:
            best[note_id] = round(similarity, 3)
    titles = dict(con.execute("SELECT id, title FROM notes"))
    return [{"path": p, "title": titles[p], "similarity": s} for p, s in list(best.items())[:limit]]


def related(con: sqlite3.Connection, path: str) -> dict:
    if not con.execute("SELECT 1 FROM notes WHERE id=?", (path,)).fetchone():
        return {"error": f"unknown note: {path}"}

    def edges(sql, arg):
        return [dict(r) for r in con.execute(sql, (arg,))]

    return {
        "parents": edges("SELECT dst_name AS name, dst_id AS path FROM edges WHERE src=? AND kind='CHILD_OF'", path),
        "children": edges("SELECT src AS path FROM edges WHERE dst_id=? AND kind='CHILD_OF'", path),
        "links_out": edges("SELECT dst_name AS name, dst_id AS path FROM edges WHERE src=? AND kind='LINKS_TO'", path),
        "backlinks": edges("SELECT DISTINCT src AS path FROM edges WHERE dst_id=? AND kind='LINKS_TO'", path),
    }


def read_note(con: sqlite3.Connection, path: str, heading: str | None = None) -> dict:
    """Read from the mirror on disk, never outside the vault root."""
    row = con.execute("SELECT v FROM state WHERE k='root'").fetchone()
    if not row:
        return {"error": "index is empty"}
    root = Path(row[0]).resolve()
    note = con.execute("SELECT 1 FROM notes WHERE id=?", (path,)).fetchone()
    target = (root / path).resolve()
    if not note or root not in target.parents:
        return {"error": f"unknown note: {path}"}
    text = target.read_text(encoding="utf-8", errors="replace")
    if heading:
        text = _section(text, heading)
        if text is None:
            return {"error": f"no heading '{heading}' in {path}"}
    return {"path": path, "heading": heading, "text": text}


def _section(text: str, heading: str) -> str | None:
    lines, out, level, in_code = text.split("\n"), [], None, False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)
        if level is None:
            if m and m.group(2).strip().lower() == heading.strip().lower():
                level, out = len(m.group(1)), [line]
        elif m and len(m.group(1)) <= level:
            break
        else:
            out.append(line)
    return "\n".join(out) if level is not None else None


def status(con: sqlite3.Connection) -> dict:
    def one(sql):
        return con.execute(sql).fetchone()[0]

    state = dict(con.execute("SELECT k,v FROM state").fetchall())
    return {
        "notes": one("SELECT COUNT(*) FROM notes"),
        "chunks": one("SELECT COUNT(*) FROM fts"),
        "edges": one("SELECT COUNT(*) FROM edges"),
        "unresolved_links": one("SELECT COUNT(*) FROM edges WHERE dst_id IS NULL"),
        "multi_parent_notes": one(
            "SELECT COUNT(*) FROM (SELECT src FROM edges WHERE kind='CHILD_OF' GROUP BY src HAVING COUNT(*)>1)"),
        "parent_cycles": parent_cycles(con),
        "last_sync": state.get("last_sync"),
    }
