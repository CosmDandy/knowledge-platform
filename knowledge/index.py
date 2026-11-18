"""Read-only Markdown indexer: notes, wikilink/parent graph and FTS5 chunks in one SQLite file.

The vault is never written to. The database is derived state and can be deleted and rebuilt.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
  id TEXT PRIMARY KEY,            -- vault-relative path; a rename is delete + add
  title TEXT NOT NULL,
  mtime_ns INTEGER NOT NULL,
  size INTEGER NOT NULL,
  hash TEXT NOT NULL,
  indexed_at TEXT NOT NULL,
  tags TEXT NOT NULL,             -- JSON list: statuses only, by vault convention
  aliases TEXT NOT NULL           -- JSON list
);
CREATE TABLE IF NOT EXISTS edges (
  src TEXT NOT NULL,
  kind TEXT NOT NULL,             -- LINKS_TO | CHILD_OF
  dst_name TEXT NOT NULL,
  dst_id TEXT                     -- NULL while the target note does not exist
);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src, kind);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst_id, kind);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  note_id UNINDEXED, heading UNINDEXED, title, aliases, body,
  tokenize = "unicode61 remove_diacritics 2 tokenchars '.-_'"
);
"""

SKIP_DIRS = {"node_modules"}
WIKILINK = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
SECRET_LINE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S{6,}|AKIA[0-9A-Z]{16})"
)
CHUNK_MAX = 1500
# '.' is a token character so 10.0.0.5 stays whole; a sentence-final dot must not stick to it.
SENTENCE_DOT = re.compile(r"(?<=\S)\.(?=\s|$)", re.MULTILINE)


def connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.executescript(SCHEMA)
    return con


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Subset of YAML sufficient for Obsidian properties: scalars, block lists, inline lists."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    block, body = text[4:end], text[end + 4:].lstrip("\n")
    fm: dict = {}
    key = None
    for line in block.split("\n"):
        m = re.match(r"^([^\s:][^:]*):\s*(.*)$", line)
        if m:
            key, val = m.group(1).strip(), m.group(2).strip()
            if val == "":
                fm[key] = []
            elif val.startswith("[") and val.endswith("]"):
                fm[key] = [_unquote(v) for v in val[1:-1].split(",") if v.strip()]
            else:
                fm[key] = _unquote(val)
        elif key and re.match(r"^\s+-\s+", line):
            item = _unquote(re.sub(r"^\s+-\s+", "", line).strip())
            if not isinstance(fm.get(key), list):
                fm[key] = [fm[key]] if fm.get(key) else []
            fm[key].append(item)
    return fm, body


def _unquote(v: str) -> str:
    v = v.strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v


def as_list(v) -> list[str]:
    if v is None or v == "":
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def link_names(values: list[str]) -> list[str]:
    names = []
    for v in values:
        found = WIKILINK.findall(v)
        names.extend(n.strip() for n in found) if found else names.append(v.strip())
    return [n for n in names if n]


def scrub(text: str) -> str:
    """Drop lines that look like credentials so they never reach the index."""
    out, in_key = [], False
    for line in text.split("\n"):
        if "BEGIN" in line and "PRIVATE KEY" in line:
            in_key = True
        if in_key or SECRET_LINE.search(line):
            out.append("[redacted]")
        else:
            out.append(line)
        if in_key and "END" in line and "PRIVATE KEY" in line:
            in_key = False
    return "\n".join(out)


def chunk(body: str) -> list[tuple[str, str]]:
    """Split by heading, then by paragraph to CHUNK_MAX. Returns (heading path, text)."""
    chunks, stack, buf = [], [], []

    def flush():
        text = "\n".join(buf).strip()
        buf.clear()
        if not text:
            return
        head = " > ".join(h for _, h in stack)
        cur = ""
        for para in re.split(r"\n\s*\n", text):
            if cur and len(cur) + len(para) > CHUNK_MAX:
                chunks.append((head, cur))
                cur = ""
            cur = f"{cur}\n\n{para}" if cur else para
        if cur:
            chunks.append((head, cur))

    in_code = False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2)))
        else:
            buf.append(line)
    flush()
    return chunks


def iter_markdown(root: Path, include: list[str] | None = None):
    """Yield .md files; with `include`, only those under the given vault-relative folders."""
    starts = [root / i for i in include] if include else [root]
    for start in starts:
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
            for f in filenames:
                if f.endswith(".md") and not f.startswith("."):
                    yield Path(dirpath) / f


def _index_note(con: sqlite3.Connection, root: Path, path: Path, st, digest: str, now: str) -> None:
    rel = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    fm, body = parse_frontmatter(text)
    body = scrub(body)
    title = path.stem
    tags, aliases = as_list(fm.get("tags")), as_list(fm.get("aliases"))
    con.execute("DELETE FROM fts WHERE note_id = ?", (rel,))
    con.execute("DELETE FROM edges WHERE src = ?", (rel,))
    con.execute(
        "INSERT OR REPLACE INTO notes VALUES (?,?,?,?,?,?,?,?)",
        (rel, title, st.st_mtime_ns, st.st_size, digest, now, json.dumps(tags, ensure_ascii=False),
         json.dumps(aliases, ensure_ascii=False)),
    )
    seen = set()
    for name in link_names(as_list(fm.get("parent"))):
        con.execute("INSERT INTO edges(src,kind,dst_name) VALUES (?,?,?)", (rel, "CHILD_OF", name))
    for name in WIKILINK.findall(body) + link_names(as_list(fm.get("links"))):
        name = name.strip()
        if name and name not in seen:
            seen.add(name)
            con.execute("INSERT INTO edges(src,kind,dst_name) VALUES (?,?,?)", (rel, "LINKS_TO", name))
    al = " ".join(aliases)
    rows = chunk(body) or [("", "")]
    con.executemany(
        "INSERT INTO fts(note_id,heading,title,aliases,body) VALUES (?,?,?,?,?)",
        [(rel, h, title, al, SENTENCE_DOT.sub(" .", t)) for h, t in rows],
    )


def resolve_edges(con: sqlite3.Connection) -> None:
    by_name: dict[str, str] = {}
    for nid, title, aliases in con.execute("SELECT id,title,aliases FROM notes ORDER BY id"):
        by_name.setdefault(title.lower(), nid)
        for a in json.loads(aliases):
            by_name.setdefault(a.lower(), nid)
    rows = con.execute("SELECT rowid, dst_name FROM edges").fetchall()
    con.executemany("UPDATE edges SET dst_id = ? WHERE rowid = ?",
                    [(by_name.get(n.lower()) or by_name.get(n.rsplit("/", 1)[-1].lower()), rid)
                     for rid, n in rows])


def sync(root: str | Path, db_path: str | Path, full: bool = False, include: list[str] | None = None) -> dict:
    """Bring the index in line with the vault. Cheap on no-op: stat only, hash on mtime change."""
    root = Path(root).resolve()
    con = connect(db_path)
    if full:
        con.executescript("DELETE FROM notes; DELETE FROM edges; DELETE FROM fts;")
    known = {r[0]: (r[1], r[2], r[3]) for r in con.execute("SELECT id,mtime_ns,size,hash FROM notes")}
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    added = changed = 0
    on_disk = set()
    for path in iter_markdown(root, include):
        rel = path.relative_to(root).as_posix()
        on_disk.add(rel)
        st = path.stat()
        prev = known.get(rel)
        if prev and prev[0] == st.st_mtime_ns and prev[1] == st.st_size:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if prev and prev[2] == digest:
            con.execute("UPDATE notes SET mtime_ns=?, size=? WHERE id=?", (st.st_mtime_ns, st.st_size, rel))
            continue
        _index_note(con, root, path, st, digest, now)
        changed += bool(prev)
        added += not prev
    gone = set(known) - on_disk
    for rel in gone:
        con.execute("DELETE FROM notes WHERE id=?", (rel,))
        con.execute("DELETE FROM edges WHERE src=?", (rel,))
        con.execute("DELETE FROM fts WHERE note_id=?", (rel,))
    resolve_edges(con)
    con.execute("INSERT OR REPLACE INTO state VALUES ('root', ?)", (str(root),))
    con.execute("INSERT OR REPLACE INTO state VALUES ('last_sync', ?)", (now,))
    con.commit()
    con.close()
    return {"added": added, "changed": changed, "removed": len(gone), "total": len(on_disk)}


def parent_cycles(con: sqlite3.Connection) -> list[list[str]]:
    """Cycles in the parent relation (the vault allows several parents, so it is a DAG at best)."""
    graph: dict[str, list[str]] = {}
    for src, dst in con.execute("SELECT src,dst_id FROM edges WHERE kind='CHILD_OF' AND dst_id IS NOT NULL"):
        graph.setdefault(src, []).append(dst)
    cycles, state = [], {}

    def visit(n, trail):
        state[n] = 1
        for m in graph.get(n, []):
            if state.get(m) == 1:
                cycles.append(trail[trail.index(m):] + [m] if m in trail else [n, m])
            elif m not in state:
                visit(m, trail + [m])
        state[n] = 2

    for n in list(graph):
        if n not in state:
            visit(n, [n])
    return cycles
