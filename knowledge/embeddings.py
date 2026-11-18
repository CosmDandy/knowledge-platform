"""Semantic vectors: the embedder, the vector store inside the index database, and the search over it.

Vectors live in SQLite as float32 BLOBs and are compared with numpy (exact brute force). At a few thousand
chunks that is milliseconds, needs no extension and keeps one rebuildable file.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading

import numpy as np

MODEL = os.environ.get("KNOWLEDGE_EMBED_MODEL", "intfloat/multilingual-e5-small")
DIM = 384
# e5 models are trained with these prefixes; without them quality drops noticeably.
QUERY_PREFIX, PASSAGE_PREFIX = "query: ", "passage: "
# Characters of a chunk that reach the model. e5-small reads at most 512 tokens (about 1500 Cyrillic chars).
MAX_CHARS = 1800

SCHEMA = """
CREATE TABLE IF NOT EXISTS vec (rowid INTEGER PRIMARY KEY, note_id TEXT NOT NULL, v BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS vec_note ON vec(note_id);
CREATE TABLE IF NOT EXISTS emb_cache (key TEXT PRIMARY KEY, v BLOB NOT NULL);
"""


def passage_text(title: str, heading: str, body: str) -> str:
    """A chunk with its context: the bare text loses what the note is about."""
    head = f"{title} > {heading}" if heading else title
    return (PASSAGE_PREFIX + head + "\n" + body)[:MAX_CHARS]


def cache_key(text: str) -> str:
    return hashlib.sha1(f"{MODEL}\0{text}".encode()).hexdigest()


class Embedder:
    """Lazy fastembed wrapper. The model is loaded on first use and shared between threads."""

    def __init__(self, cache_dir: str | None = None, model: str = MODEL):
        self.model_name, self.cache_dir = model, cache_dir
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding
                from fastembed.common.model_description import ModelSource, PoolingType

                if self.model_name == "intfloat/multilingual-e5-small":  # not built into fastembed
                    try:
                        TextEmbedding.add_custom_model(
                            model=self.model_name, pooling=PoolingType.MEAN, normalization=True,
                            sources=ModelSource(hf=self.model_name), dim=DIM, model_file="onnx/model.onnx")
                    except ValueError:  # already registered by an earlier Embedder in this process
                        pass
                self._model = TextEmbedding(self.model_name, cache_dir=self.cache_dir)
        return self._model

    def embed(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        """Unit-length float32 vectors, one row per text."""
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        out = np.array(list(self._load().embed(texts, batch_size=batch_size)), dtype=np.float32)
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1, norms)

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed([QUERY_PREFIX + text.strip()[:MAX_CHARS]])[0]


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def drop_note(con: sqlite3.Connection, note_id: str) -> None:
    con.execute("DELETE FROM vec WHERE note_id = ?", (note_id,))


def fill(con: sqlite3.Connection, embedder: Embedder) -> int:
    """Embed every chunk that has no vector yet. Identical text is looked up in the cache first."""
    rows = con.execute(
        "SELECT f.rowid, f.note_id, f.title, f.heading, f.body, n.title FROM fts f "
        "JOIN notes n ON n.id = f.note_id WHERE f.rowid NOT IN (SELECT rowid FROM vec)").fetchall()
    if not rows:
        return 0
    texts = [passage_text(r[5], r[3], r[4]) for r in rows]
    keys = [cache_key(t) for t in texts]
    cached = dict(con.execute(
        f"SELECT key, v FROM emb_cache WHERE key IN ({','.join('?' * len(set(keys)))})", list(set(keys))))
    todo = sorted({k: t for k, t in zip(keys, texts, strict=True) if k not in cached}.items())
    if todo:
        vectors = embedder.embed([t for _, t in todo])
        for (k, _), v in zip(todo, vectors, strict=True):
            blob = v.tobytes()
            cached[k] = blob
            con.execute("INSERT OR REPLACE INTO emb_cache VALUES (?, ?)", (k, blob))
    con.executemany("INSERT OR REPLACE INTO vec VALUES (?, ?, ?)",
                    [(r[0], r[1], cached[k]) for r, k in zip(rows, keys, strict=True)])
    return len(rows)


_matrix_cache: dict = {}


def load_matrix(con: sqlite3.Connection):
    """(rowids, note_ids, matrix) for every stored vector, cached until the index changes."""
    stamp = con.execute("SELECT v FROM state WHERE k = 'last_sync'").fetchone()
    key = (stamp[0] if stamp else None, con.execute("SELECT COUNT(*) FROM vec").fetchone()[0])
    if _matrix_cache.get("key") != key:
        rows = con.execute("SELECT rowid, note_id, v FROM vec ORDER BY rowid").fetchall()
        _matrix_cache.update(
            key=key, rowids=np.array([r[0] for r in rows], dtype=np.int64), notes=[r[1] for r in rows],
            matrix=(np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(len(rows), DIM)
                    if rows else np.zeros((0, DIM), dtype=np.float32)))
    return _matrix_cache["rowids"], _matrix_cache["notes"], _matrix_cache["matrix"]


def nearest(con: sqlite3.Connection, query_vec: np.ndarray, k: int = 30,
            allowed: set[str] | None = None) -> list[tuple[int, str, float]]:
    """Top chunks by cosine similarity: (chunk rowid, note id, similarity). `allowed` restricts notes."""
    rowids, notes, matrix = load_matrix(con)
    if not len(rowids):
        return []
    scores = matrix @ query_vec
    if allowed is not None:
        scores = np.where(np.array([n in allowed for n in notes]), scores, -1.0)
    top = np.argsort(-scores)[:k]
    return [(int(rowids[i]), notes[i], float(scores[i])) for i in top if scores[i] > -1.0]
