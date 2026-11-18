import base64
import re
import unittest
import urllib.parse

from knowledge.proposals import ProposalError, Proposer, slugify


class FakeGitHub:
    """Just enough of the GitHub REST API: branches, contents, pull requests."""

    def __init__(self):
        self.branches = {"master": "s0"}
        self.files = {("master", "01 Архив/Existing.md"): "x"}
        self.prs = []

    def _file(self, ref, path):
        if (ref, path) in self.files:
            return True
        return ref != "master" and ref in self.branches and ("master", path) in self.files

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
                ref = path.split("ref=")[1]
                return (200, {}) if self._file(ref, file) else (404, {"message": "Not Found"})
            self.files[(body["branch"], file)] = base64.b64decode(body["content"]).decode()
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

    def test_slug_keeps_cyrillic(self):
        self.assertEqual(slugify("Миграция памяти Claude!"), "миграция-памяти-claude")

    def test_first_proposal_opens_branch_and_pr(self):
        r = self.p.propose_note("Traefik", "Traefik", "Reverse proxy.", parent="Инфраструктура",
                                sources=["claude memory 2026-11-18"])
        self.assertEqual((r["branch"], r["new_pr"]), ("knowledge/traefik", True))
        text = self.gh.files[("knowledge/traefik", "01 Архив/Traefik.md")]
        self.assertIn('parent: "[[Инфраструктура]]"', text)
        self.assertIn('  - "claude memory 2026-11-18"', text)
        self.assertEqual(len(self.gh.prs), 1)

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
            {"topic": "t", "title": "A", "body": "x", "parent": "[[A]]"},   # bracketed parent
        ]
        for kw in bad:
            with self.assertRaises(ProposalError, msg=str(kw)):
                self.p.propose_note(**kw)
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
