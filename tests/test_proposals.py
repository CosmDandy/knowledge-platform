import base64
import datetime
import hashlib
import re
import unittest
import urllib.parse

from knowledge.proposals import ProposalError, Proposer, slugify


class FakeGitHub:
    """Just enough of the GitHub REST API: branches, contents, pull requests."""

    def __init__(self):
        self.branches = {"master": "s0"}
        self.files = {
            ("master", "01 Архив/Existing.md"):
                '---\nparent: "[[Infra]]"\ntags:\n  - 1_Исследуется\ncreated: 2025-01-01\nupdated: 2025-01-01\n---\n'
                "Old body.\n",
            ("master", "01 Архив/Memo.md"):
                "---\ntags:\n  - agent-memory\ncreated: 2025-01-01\nupdated: 2025-01-01\n---\nMemo text.\n",
        }
        self.prs = []
        self.commits = []

    def _text(self, ref, path):
        if (ref, path) in self.files:
            return self.files[(ref, path)]
        if ref != "master" and ref in self.branches:
            return self.files.get(("master", path))
        return None

    def request(self, method, path, body=None):
        path = urllib.parse.unquote(path)
        route = path.split("?")[0]
        if method == "GET" and route == "/pulls":
            return 200, [self._pr_json(p) for p in self.prs if p["state"] == "open"]
        if m := re.fullmatch(r"/pulls/(\d+)/files", route):
            head = next(p["head"] for p in self.prs if p["number"] == int(m[1]))
            return 200, [{"filename": f} for (r, f) in self.files if r == head]
        if m := re.fullmatch(r"/contents/(.+)", route):
            file = m[1]
            if method == "GET":
                text = self._text(path.split("ref=")[1], file)
                if text is None:
                    return 404, {"message": "Not Found"}
                return 200, {"sha": hashlib.sha1(text.encode()).hexdigest(),
                             "content": base64.b64encode(text.encode()).decode()}
            if "sha" in body:  # updating: the sha must match what the branch holds now
                current = self._text(body["branch"], file)
                if current is None or hashlib.sha1(current.encode()).hexdigest() != body["sha"]:
                    return 409, {"message": "sha mismatch"}
            assert body["branch"] != "master" and body["branch"].startswith("knowledge/"), body["branch"]
            self.files[(body["branch"], file)] = base64.b64decode(body["content"]).decode()
            self.commits.append(body["message"])
            return 201, {}
        if route == "/git/ref/heads/master":
            return 200, {"object": {"sha": "s0"}}
        if method == "POST" and route == "/git/refs":
            name = body["ref"][len("refs/heads/"):]
            if name in self.branches:
                return 422, {"message": "Reference already exists"}
            self.branches[name] = "s1"
            return 201, {}
        if method == "POST" and route == "/pulls":
            if any(p["head"] == body["head"] and p["state"] == "open" for p in self.prs):
                return 422, {"message": "A pull request already exists"}
            pr = {"number": len(self.prs) + 1, "head": body["head"], "title": body["title"], "state": "open"}
            self.prs.append(pr)
            return 201, self._pr_json(pr)
        if route.startswith("/git/matching-refs/heads/"):
            prefix = route[len("/git/matching-refs/heads/"):]
            return 200, [{"ref": f"refs/heads/{b}"} for b in self.branches if b.startswith(prefix)]
        if method == "DELETE" and route.startswith("/git/refs/heads/"):
            self.branches.pop(route[len("/git/refs/heads/"):], None)
            return 204, None
        raise AssertionError(f"unexpected {method} {path}")

    @staticmethod
    def _pr_json(p):
        return {"number": p["number"], "head": {"ref": p["head"]}, "title": p["title"],
                "html_url": f"https://example.test/pull/{p['number']}"}


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.p = Proposer(self.gh, exists=lambda t: t == "InIndex")

    def test_slug_is_ascii(self):
        self.assertEqual(slugify("Миграция памяти Claude!"), "migratsiya-pamyati-claude")
        self.assertEqual(slugify("Щука"), "shchuka")
        self.assertTrue(slugify("日本語").startswith("topic-"))  # nothing transliterable: stable hash
        self.assertEqual(slugify("日本語"), slugify("日本語"))
        self.assertTrue(slugify("Проверка Knowledge MCP").isascii())

    def test_first_proposal_opens_branch_and_pr(self):
        r = self.p.propose_note("Traefik", "Traefik", "Reverse proxy.", parent="Инфраструктура")
        self.assertEqual((r["branch"], r["new_pr"]), ("knowledge/traefik", True))
        self.assertEqual(len(self.gh.prs), 1)

    def test_note_follows_the_owner_template(self):
        self.p.propose_note("t", "Сервис", "Первая строка.", parent=["code-kvt", "[[Сеть]]"],
                            links=["Traefik"], aliases=["svc"])
        text = self.gh.files[("knowledge/t", "01 Архив/Сервис.md")]
        today = datetime.date.today().isoformat()
        self.assertEqual(text, (
            f"---\ncreated: {today}\nupdated: {today}\naliases:\n  - \"svc\"\n"
            'parent:\n  - "[[code-kvt]]"\n  - "[[Сеть]]"\nlinks:\n  - "[[Traefik]]"\n'
            "tags:\n  - agent-memory\n---\n# Сервис\n\nПервая строка.\n"))

    def test_empty_fields_stay_in_template_order_and_own_heading_is_kept(self):
        self.p.propose_note("t", "Пусто", "# Свой заголовок\n\nТекст.")
        text = self.gh.files[("knowledge/t", "01 Архив/Пусто.md")]
        keys = re.findall(r"(?m)^([a-z]+):", text.split("---")[1])
        self.assertEqual(keys, ["created", "updated", "aliases", "parent", "links", "tags"])
        self.assertIn("aliases:\nparent:\nlinks:\ntags:", text)
        self.assertTrue(text.endswith("---\n# Свой заголовок\n\nТекст.\n"))
        self.assertNotIn("# Пусто", text)

    def test_writes_never_touch_master_or_foreign_branches(self):
        for bad in ("master", "feature/x", "knowledge/", "knowledge"):
            with self.assertRaises(ProposalError):
                self.p._guard(bad)
        self.p._guard("knowledge/anything")

    def test_edit_keeps_owner_note_tags_and_bumps_updated(self):
        r = self.p.propose_edit("fixes", "01 Архив/Existing.md", "New body.", reason="stale")
        self.assertTrue(r["owner_note"])
        self.assertEqual((r["new_pr"], r["open_prs"], r["max_open_prs"]), (True, 1, 30))
        text = self.gh.files[("knowledge/fixes", "01 Архив/Existing.md")]
        self.assertIn("tags:\n  - 1_Исследуется\n", text)
        self.assertNotIn("agent-memory", text)           # the tag is never added to the owner's note
        self.assertIn('parent: "[[Infra]]"', text)
        self.assertIn("created: 2025-01-01", text)
        self.assertNotIn("updated: 2025-01-01", text)
        self.assertTrue(text.endswith("---\nNew body.\n"))
        self.assertIn("(owner's note)", self.gh.commits[-1])
        self.assertIn("stale", self.gh.commits[-1])

    def test_edit_rejects_the_full_note_before_creating_a_branch(self):
        path = "01 Архив/Existing.md"
        old = self.gh.files[("master", path)]
        with self.assertRaisesRegex(ProposalError, "body must not include frontmatter"):
            self.p.propose_edit("fixes", path, old.replace("Old body.", "New body."))
        self.assertEqual(set(self.gh.branches), {"master"})
        self.assertEqual(self.gh.commits, [])
        self.assertEqual(self.gh.prs, [])

    def test_edit_accepts_a_thematic_break_after_the_title(self):
        body = "# Existing\n\n---\n\nA paragraph.\n\n---\n\nAnother paragraph."
        self.p.propose_edit("fixes", "01 Архив/Existing.md", body)
        text = self.gh.files[("knowledge/fixes", "01 Архив/Existing.md")]
        self.assertTrue(text.endswith(f"\n---\n{body}\n"))

    def test_edit_of_agent_memory_is_labelled_and_shares_the_topic_pr(self):
        self.p.propose_note("t", "A", "one")
        r = self.p.propose_edit("t", "01 Архив/Memo.md", "Updated memo.")
        self.assertFalse(r["owner_note"])
        self.assertFalse(r["new_pr"])
        self.assertEqual(len(self.gh.prs), 1)
        self.assertIn("(agent-memory)", self.gh.commits[-1])

    def test_second_edit_of_same_note_builds_on_the_branch_version(self):
        self.p.propose_edit("t", "01 Архив/Existing.md", "First.")
        self.p.propose_edit("t", "01 Архив/Existing.md", "Second.")
        self.assertTrue(self.gh.files[("knowledge/t", "01 Архив/Existing.md")].endswith("Second.\n"))

    def test_edit_rejections(self):
        bad = [
            ("t", "01 Архив/Nope.md", "x"),                  # does not exist
            ("t", "03 Дневник/x.md", "x"),                   # outside the notes folder
            ("t", "01 Архив/../secret.md", "x"),             # traversal
            ("t", "01 Архив/Existing.md", "Old body."),      # no change
            ("t", "01 Архив/Existing.md", "token: " + "z" * 12),  # credential
            ("", "01 Архив/Existing.md", "x"),               # no topic
        ]
        for topic, path, body in bad:
            with self.assertRaises(ProposalError, msg=path + body):
                self.p.propose_edit(topic, path, body)
        self.assertEqual(self.gh.commits, [])

    def test_same_topic_reuses_branch_and_pr(self):
        self.p.propose_note("Traefik", "A", "one")
        r = self.p.propose_note("traefik", "B", "two")
        self.assertFalse(r["new_pr"])
        self.assertEqual(len(self.gh.prs), 1)
        self.assertEqual(sum(1 for (b, _) in self.gh.files if b == "knowledge/traefik"), 2)

    def test_other_topic_gets_its_own_pr(self):
        self.p.propose_note("Traefik", "A", "one")
        self.p.propose_note("DNS", "B", "two")
        self.assertEqual(len(self.gh.prs), 2)

    def test_rejections(self):
        bad = [
            {"topic": "t", "title": "Existing", "body": "x"},            # on master
            {"topic": "t", "title": "InIndex", "body": "x"},             # in the index
            {"topic": "", "title": "A", "body": "x"},                    # no topic
            {"topic": "t", "title": "a/b", "body": "x"},                 # bad title
            {"topic": "t", "title": "A", "body": "  "},                  # empty body
            {"topic": "t", "title": "A", "body": "password: hunter2hunter2"},  # credential
            {"topic": "t", "title": "A", "body": "x", "parent": "a|b"},   # not a plain note name
            {"topic": "t", "title": "code-kvt: инфраструктура", "body": "x"},  # a colon is not allowed in a name
        ]
        for kw in bad:
            with self.assertRaises(ProposalError, msg=str(kw)):
                self.p.propose_note(**kw)
        self.assertEqual(self.gh.prs, [])

    def test_new_note_rejects_frontmatter_in_the_body(self):
        body = "\n---\r\ntags:\r\n  - custom\r\n---\r\n# Note\r\n\r\nText."
        with self.assertRaisesRegex(ProposalError, "body must not include frontmatter"):
            self.p.propose_note("t", "Note", body)
        self.assertEqual(set(self.gh.branches), {"master"})
        self.assertEqual(self.gh.commits, [])
        self.assertEqual(self.gh.prs, [])

    def test_duplicate_in_branch_rejected(self):
        self.p.propose_note("t", "A", "one")
        with self.assertRaises(ProposalError):
            self.p.propose_note("t", "A", "again")

    def test_limits(self):
        small = Proposer(self.gh, max_files=2, max_open=1)
        small.propose_note("t", "A", "1")
        small.propose_note("t", "B", "2")
        with self.assertRaises(ProposalError):
            small.propose_note("t", "C", "3")       # PR is full
        with self.assertRaises(ProposalError):
            small.propose_note("other", "D", "4")   # too many open PRs

    def test_default_limit_accepts_thirtieth_pr_but_refuses_a_new_topic_afterwards(self):
        for i in range(30):
            result = self.p.propose_note(f"topic-{i}", f"Note {i}", "Body.")
            self.assertEqual((result["new_pr"], result["open_prs"], result["max_open_prs"]), (True, i + 1, 30))
        with self.assertRaisesRegex(ProposalError, "30 proposals are already open"):
            self.p.propose_note("overflow", "Overflow", "Body.")
        self.assertNotIn("knowledge/overflow", self.gh.branches)
        self.assertEqual(len(self.gh.commits), 30)

        result = self.p.propose_note("topic-0", "Follow-up", "Body.")
        self.assertEqual((result["new_pr"], result["open_prs"]), (False, 30))
        self.assertEqual(len(self.gh.prs), 30)

    def test_cleanup_needs_two_sightings_and_keeps_open_prs(self):
        self.p.propose_note("keep", "A", "1")
        self.p.propose_note("gone", "B", "2")
        next(x for x in self.gh.prs if x["head"] == "knowledge/gone")["state"] = "closed"
        self.assertEqual(self.p.cleanup(), [])
        self.assertIn("knowledge/gone", self.gh.branches)
        self.assertEqual(self.p.cleanup(), ["knowledge/gone"])
        self.assertNotIn("knowledge/gone", self.gh.branches)
        self.assertIn("knowledge/keep", self.gh.branches)

    def test_branch_just_created_is_not_deleted(self):
        self.gh.branches["knowledge/racing"] = "s1"  # a concurrent agent, PR not yet opened
        self.p.cleanup()
        self.p.propose_note("racing", "A", "1")      # PR appears before the second sighting
        self.assertEqual(self.p.cleanup(), [])
        self.assertIn("knowledge/racing", self.gh.branches)


if __name__ == "__main__":
    unittest.main()
