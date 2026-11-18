"""Semantic search with a fake embedder: concepts are keyword groups, so no model is downloaded."""
import tempfile
import unittest
from pathlib import Path

import numpy as np

from knowledge import embeddings, index, query

CONCEPTS = [("restic", "бэкап", "резервн"), ("kubernetes", "кластер", "под"), ("картофел", "рецепт", "пюре")]


class FakeEmbedder:
    model_name = "fake"

    def __init__(self):
        self.calls = 0

    def embed(self, texts, batch_size=32):
        self.calls += len(texts)
        out = np.zeros((len(texts), embeddings.DIM), dtype=np.float32)
        for i, text in enumerate(texts):
            low = text.lower()
            for axis, words in enumerate(CONCEPTS):
                out[i, axis] = sum(w in low for w in words)
            out[i, -1] = 0.01  # no text maps to the zero vector
        return out / np.linalg.norm(out, axis=1, keepdims=True)

    def embed_query(self, text):
        return self.embed([text])[0]


class Broken(FakeEmbedder):
    def embed_query(self, text):
        raise RuntimeError("model unavailable")


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class Semantic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.db = Path(self.tmp.name) / "idx.db"
        write(self.root, "A/Backup.md", "# Копии\nКаждую ночь restic сохраняет данные в облако.\n")
        write(self.root, "A/K8s.md", "# Оркестрация\nKubernetes держит желаемое состояние кластера.\n")
        write(self.root, "A/Food.md",
              '---\ntags:\n  - agent-memory\n---\n# Еда\nРецепт: картофельное пюре с маслом.\n')
        self.emb = FakeEmbedder()
        self.result = index.sync(self.root, self.db, embedder=self.emb)
        self.con = query.open_ro(self.db)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def paths(self, text, **kw):
        return [h["path"] for h in query.search(self.con, text, embedder=self.emb, **kw)["results"]]

    def test_every_chunk_embedded_once(self):
        chunks = self.con.execute("SELECT COUNT(*) FROM fts").fetchone()[0]
        self.assertEqual(self.result["embedded"], chunks)
        self.assertEqual(index.sync(self.root, self.db, embedder=self.emb)["embedded"], 0)

    def test_meaning_without_shared_words(self):
        self.assertEqual(self.paths("бэкапы", mode="keyword"), [])
        self.assertEqual(self.paths("бэкапы", mode="semantic"), ["A/Backup.md"])
        self.assertEqual(self.paths("бэкапы")[0], "A/Backup.md")

    def test_hybrid_reports_why(self):
        hit = query.search(self.con, "restic бэкап", embedder=self.emb)["results"][0]
        self.assertEqual((hit["path"], hit["reason"]), ("A/Backup.md", "both"))
        self.assertIsNotNone(hit["bm25"])
        self.assertIsNotNone(hit["similarity"])
        only = query.search(self.con, "бэкапы", embedder=self.emb)["results"][0]
        self.assertEqual((only["reason"], only["bm25"]), ("semantic", None))

    def test_below_threshold_is_dropped(self):
        self.assertEqual(self.paths("совершенно постороннее", mode="semantic"), [])

    def test_memory_filter_applies_to_vectors(self):
        self.assertEqual(self.paths("рецепт", mode="semantic", memory="owner"), [])
        self.assertEqual(self.paths("рецепт", mode="semantic", memory="agent"), ["A/Food.md"])

    def test_broken_embedder_falls_back_to_keywords(self):
        res = query.search(self.con, "restic", embedder=Broken())
        self.assertEqual(res["semantic"], "unavailable")
        self.assertEqual([h["path"] for h in res["results"]], ["A/Backup.md"])
        self.assertEqual(query.search(self.con, "restic")["semantic"], "unavailable")  # no embedder at all

    def test_bad_mode(self):
        self.assertIn("error", query.search(self.con, "x", mode="magic"))

    def test_edit_and_delete_follow_the_text(self):
        write(self.root, "A/Backup.md", "# Копии\nТеперь это рецепт пюре.\n")
        (self.root / "A/K8s.md").unlink()
        index.sync(self.root, self.db, embedder=self.emb)
        con = query.open_ro(self.db)
        try:
            self.assertEqual(
                [h["path"] for h in query.search(con, "бэкапы", mode="semantic", embedder=self.emb)["results"]], [])
            self.assertEqual(con.execute("SELECT COUNT(DISTINCT note_id) FROM vec").fetchone()[0], 2)
        finally:
            con.close()

    def test_new_model_discards_old_vectors(self):
        other = FakeEmbedder()
        other.model_name = "other"
        self.assertEqual(index.sync(self.root, self.db, embedder=other)["embedded"],
                         self.con.execute("SELECT COUNT(*) FROM fts").fetchone()[0])

    def test_vectors_are_recomputed_after_format_rebuild(self):
        before = self.emb.calls
        index.sync(self.root, self.db, full=True, embedder=self.emb)
        self.assertEqual(self.emb.calls, before)  # identical text comes from the cache

    def test_parent_scope_covers_subtree_at_any_depth(self):
        write(self.root, "A/Proj.md", "---\naliases:\n  - proj\n---\n# Проект\nХаб проекта.\n")
        write(self.root, "A/Mid.md", '---\nparent: "[[Proj]]"\n---\n# Звено\nПромежуточная заметка про restic.\n')
        write(self.root, "A/Leaf.md", '---\nparent: "[[Mid]]"\n---\n# Лист\nГлубокая заметка про restic.\n')
        index.sync(self.root, self.db, embedder=self.emb)
        con = query.open_ro(self.db)
        try:
            for name in ("Proj", "proj", "A/Proj.md", "[[Proj]]"):  # title, alias, path, wikilink
                hits = query.search(con, "restic", parent=name, embedder=self.emb)["results"]
                self.assertEqual({h["path"] for h in hits}, {"A/Mid.md", "A/Leaf.md"}, name)
            semantic = query.search(con, "бэкапы", mode="semantic", parent="Proj", embedder=self.emb)["results"]
            self.assertEqual({h["path"] for h in semantic}, {"A/Mid.md", "A/Leaf.md"})
            outside = query.search(con, "restic", embedder=self.emb)["results"]
            self.assertIn("A/Backup.md", [h["path"] for h in outside])  # no parent: the whole vault
        finally:
            con.close()

    def test_parent_unknown_or_cyclic(self):
        self.assertIn("error", query.search(self.con, "restic", parent="Нет такой"))
        write(self.root, "A/X.md", '---\nparent: "[[Y]]"\n---\n# X\nrestic\n')
        write(self.root, "A/Y.md", '---\nparent: "[[X]]"\n---\n# Y\nrestic\n')
        index.sync(self.root, self.db, embedder=self.emb)
        con = query.open_ro(self.db)
        try:
            hits = query.search(con, "restic", parent="X", embedder=self.emb)["results"]
            self.assertEqual({h["path"] for h in hits}, {"A/X.md", "A/Y.md"})
        finally:
            con.close()

    def test_similar_finds_duplicates(self):
        found = query.similar(self.con, "Резервное копирование restic", self.emb)
        self.assertEqual([f["path"] for f in found], ["A/Backup.md"])
        self.assertEqual(query.similar(self.con, "что-то совсем другое", self.emb), [])


if __name__ == "__main__":
    unittest.main()
