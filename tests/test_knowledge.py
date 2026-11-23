"""Run: python3 -I -m unittest discover -s tests -t . (from the repo root)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge import index, query


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class KP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        self.db = Path(self.tmp.name) / "idx.db"
        write(self.root, "A/Infra.md", "---\naliases:\n  - Инфраструктура\n---\n# Обзор\nMOC.\n")
        write(self.root, "A/Traefik.md",
              '---\nparent:\n  - "[[Infra]]"\n  - "[[Net]]"\ntags:\n  - ready\n---\n'
              "# Размещение\nHost hhh-sto-02.example.dev has IP 10.0.0.5.\n\n## Секреты\npassword: hunter2hunter2\n")
        write(self.root, "A/Net.md", "---\nparent: \"[[Traefik]]\"\n---\nСеть, см. [[Infra]] и [[Missing]].\n")
        write(self.root, ".obsidian/x.md", "hidden hostname hhh-sto-02")
        index.sync(self.root, self.db)
        self.con = query.open_ro(self.db)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_excalidraw_drawings_are_not_indexed(self):
        write(self.root, "A/Scheme.excalidraw.md", "---\nexcalidraw-plugin: parsed\n---\n```json\n{\"zzquux\": 1}\n```\n")
        index.sync(self.root, self.db)
        self.assertEqual(query.search(self.con, "zzquux")["results"], [])
        self.assertEqual(query.status(self.con)["notes"], 3)

    def test_hidden_dirs_skipped(self):
        self.assertEqual(query.status(self.con)["notes"], 3)

    def test_exact_hostname_and_ip(self):
        for q in ("hhh-sto-02.example.dev", "10.0.0.5"):
            hits = query.search(self.con, q)["results"]
            self.assertEqual([h["path"] for h in hits], ["A/Traefik.md"], q)
        self.assertEqual(query.search(self.con, "hhh-sto-02.example.dev")["results"][0]["heading"], "Размещение")

    def test_alias_and_cyrillic(self):
        self.assertEqual(query.search(self.con, "инфраструктура")["results"][0]["path"], "A/Infra.md")

    def test_secret_never_indexed(self):
        self.assertEqual(query.search(self.con, "hunter2hunter2")["results"], [])

    def test_graph_multi_parent_backlinks_unresolved(self):
        rel = query.related(self.con, "A/Traefik.md")
        self.assertEqual(sorted(p["path"] for p in rel["parents"]), ["A/Infra.md", "A/Net.md"])
        self.assertEqual([c["path"] for c in rel["children"]], ["A/Net.md"])
        self.assertEqual(sorted(b["path"] for b in query.related(self.con, "A/Infra.md")["backlinks"]), ["A/Net.md"])
        st = query.status(self.con)
        self.assertEqual(st["unresolved_links"], 1)
        self.assertEqual(st["multi_parent_notes"], 1)
        self.assertTrue(st["parent_cycles"])  # Traefik -> Net -> Traefik

    def test_tags_and_memory_filter(self):
        write(self.root, "A/Mem.md", "---\ntags:\n  - agent-memory\n---\nTraefik memory zebrax.\n")
        index.sync(self.root, self.db)
        con = query.open_ro(self.db)
        hit = query.search(con, "zebrax")["results"][0]
        self.assertEqual((hit["tags"], hit["agent_memory"]), (["agent-memory"], True))
        self.assertEqual([h["path"] for h in query.search(con, "Traefik", memory="agent")["results"]], ["A/Mem.md"])
        owner = [h["path"] for h in query.search(con, "Traefik", memory="owner")["results"]]
        self.assertEqual(owner, ["A/Traefik.md"])
        self.assertIn("error", query.search(con, "Traefik", memory="nonsense"))

    def test_snippets_only_for_returned_notes_with_the_successful_match(self):
        write(self.root, "A/Other.md", "Another host with IP 10.0.0.5.")
        index.sync(self.root, self.db)
        for text, op in (("10.0.0.5", "AND"), ("10.0.0.5 nonexistent", "OR")):
            with self.subTest(text=text), patch.object(query, "_snippets", wraps=query._snippets) as snippets:
                hits = query.search(self.con, text, limit=1)["results"]
                self.assertEqual(len(hits), 1)
                self.assertIn("[10.0.0.5]", hits[0]["snippet"])
                _, match, chunks = snippets.call_args.args
                self.assertEqual(match, query._match(text.split(), op))
                self.assertEqual(len(chunks), 1)

    def test_search_does_not_leave_a_snapshot_open_in_autocommit(self):
        con = sqlite3.connect(self.db, autocommit=True)
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        self.assertTrue(query.search(con, "10.0.0.5")["results"])
        self.assertFalse(con.in_transaction)

    def test_snippet_falls_back_to_text_start_on_title_only_match(self):
        write(self.root, "A/Titleonly.md", "---\n---\nCompletely different words here.\n")
        index.sync(self.root, self.db)
        hit = query.search(query.open_ro(self.db), "Titleonly")["results"][0]
        self.assertTrue(hit["snippet"].startswith("Completely different"))

    def test_title_match_shows_the_start_of_the_note_not_a_short_chunk(self):
        write(self.root, "A/Alphaname.md", "---\n---\nIntro text of the note.\n\n# Section\n***\n")
        index.sync(self.root, self.db)
        hit = query.search(query.open_ro(self.db), "Alphaname")["results"][0]
        self.assertEqual(hit["heading"], "")
        self.assertTrue(hit["snippet"].startswith("Intro text"))

    def test_separator_only_chunks_are_not_indexed(self):
        write(self.root, "A/Gammaname.md", "---\n---\n# Gammaname\n\n***\n\n## Definition\nReal text here.\n")
        index.sync(self.root, self.db)
        hit = query.search(query.open_ro(self.db), "Gammaname")["results"][0]
        self.assertTrue(hit["snippet"].startswith("Real text"))

    def test_old_index_format_is_rebuilt_on_next_sync(self):
        con = sqlite3.connect(self.db)
        con.executescript("UPDATE state SET v='1' WHERE k='format'; DELETE FROM fts;")
        con.commit()
        con.close()
        self.assertEqual(query.search(query.open_ro(self.db), "10.0.0.5")["results"], [])
        r = index.sync(self.root, self.db)  # no file changed: only the stale format forces the work
        self.assertEqual(r["added"], 3)
        self.assertEqual(len(query.search(query.open_ro(self.db), "10.0.0.5")["results"]), 1)

    def test_path_qualified_link_resolves_by_basename(self):
        write(self.root, "A/Other.md", "см. [[A/Infra]]")
        index.sync(self.root, self.db)
        con = query.open_ro(self.db)
        self.assertIn("A/Other.md", [b["path"] for b in query.related(con, "A/Infra.md")["backlinks"]])

    def test_incremental_delete_rename_edit(self):
        r = index.sync(self.root, self.db)
        self.assertEqual((r["added"], r["changed"], r["removed"]), (0, 0, 0))
        (self.root / "A/Net.md").rename(self.root / "A/Network.md")
        write(self.root, "A/Infra.md", "---\n---\nновое слово zebra\n")
        r = index.sync(self.root, self.db)
        self.assertEqual((r["added"], r["changed"], r["removed"]), (1, 1, 1))
        con = query.open_ro(self.db)
        self.assertEqual([h["path"] for h in query.search(con, "zebra")["results"]], ["A/Infra.md"])
        self.assertEqual(query.related(con, "A/Net.md"), {"error": "unknown note: A/Net.md"})
        self.assertEqual(query.search(con, "Сеть")["results"][0]["path"], "A/Network.md")

    def test_rebuild_equals_incremental(self):
        before = query.status(self.con)
        index.sync(self.root, self.db, full=True)
        after = query.status(query.open_ro(self.db))
        before.pop("last_sync"), after.pop("last_sync")
        self.assertEqual(before, after)

    def test_read_section_and_traversal(self):
        self.assertIn("10.0.0.5", query.read_note(self.con, "A/Traefik.md", "Размещение")["text"])
        self.assertNotIn("Обзор", query.read_note(self.con, "A/Traefik.md", "Размещение")["text"])
        for bad in ("../outside.md", "/etc/passwd", ".obsidian/x.md"):
            self.assertIn("error", query.read_note(self.con, bad))

    def test_index_is_read_only_for_queries(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.con.execute("DELETE FROM notes")


if __name__ == "__main__":
    unittest.main()
