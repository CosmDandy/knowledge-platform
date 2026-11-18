"""Read-only queries over the index. Every function opens the database read-only."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .index import HEADING, parent_cycles


def open_ro(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _match(terms: list[str], op: str) -> str:
    return f" {op} ".join('"' + t.replace('"', '""') + '"' for t in terms)


def search(con: sqlite3.Connection, query: str, limit: int = 10) -> dict:
    """BM25 over title, aliases and body. Terms are quoted, so hostnames and IPs match literally."""
    terms = re.findall(r"\S+", query)
    if not terms:
        return {"results": []}
    rows = []
    for op in ("AND", "OR"):  # fall back to OR only when the strict query finds nothing
        rows = con.execute(
            "SELECT f.note_id, f.heading, snippet(fts, 4, '[', ']', '…', 24) AS snip, "
            "bm25(fts, 0, 0, 5.0, 3.0, 1.0) AS rank, n.hash, n.indexed_at, n.title "
            "FROM fts f JOIN notes n ON n.id = f.note_id "
            "WHERE fts MATCH ? ORDER BY rank LIMIT ?",
            (_match(terms, op), limit * 3),
        ).fetchall()
        if rows:
            break
    results, seen = [], set()
    for r in rows:  # one hit per note: the best chunk
        if r["note_id"] in seen:
            continue
        seen.add(r["note_id"])
        results.append({
            "path": r["note_id"], "title": r["title"], "heading": r["heading"], "snippet": r["snip"],
            "rank": len(results) + 1, "bm25": round(r["rank"], 3),
            "source_hash": r["hash"], "indexed_at": r["indexed_at"],
        })
        if len(results) == limit:
            break
    return {"results": results}


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
