"""Write path: new notes go to one branch and one pull request per topic. The vault is never touched.

Everything goes through the GitHub REST API, so there is no local clone, no lock and no rebase:
two agents on the same topic both end up adding commits to the same branch and PR.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .index import SECRET_LINE

FORBIDDEN_TITLE = re.compile(r'[\\/:*?"<>|#^\[\]\n\r\t]')
MAX_BODY = 200_000
LEADING_FRONTMATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n.*?^---[ \t]*\r?$", re.MULTILINE | re.DOTALL)
DEFAULT_MAX_OPEN_PRS = 30
# Marks notes that are the agent's memory, not the owner's knowledge. Set only on creation.
AGENT_TAG = "agent-memory"


class ProposalError(Exception):
    """A rejected proposal: the message is meant for the calling agent."""


class GitHub:
    def __init__(self, repo: str, token: str, api: str = "https://api.github.com"):
        self.repo, self.token, self.api = repo, token, api

    def request(self, method: str, path: str, body: dict | None = None):
        """Returns (status, parsed json or None). HTTP errors are returned, not raised."""
        req = urllib.request.Request(
            f"{self.api}/repos/{self.repo}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "knowledge-mcp",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, json.loads(raw) if raw else None


_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
     "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"],
    strict=True,
))


def slugify(topic: str) -> str:
    """ASCII only: GitHub flags non-ASCII branch names as possibly containing hidden characters."""
    slug = "".join(_TRANSLIT.get(c, c) for c in topic.strip().lower())
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")[:50].strip("-")
    return slug or "topic-" + hashlib.sha1(topic.encode()).hexdigest()[:8]


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="/")


class Proposer:
    def __init__(self, gh, prefix="knowledge", base="master", folder="01 Архив",
                 max_open=DEFAULT_MAX_OPEN_PRS, max_files=20, exists=lambda title: False):
        self.gh, self.prefix, self.base, self.folder = gh, prefix, base, folder
        self.max_open, self.max_files, self.exists = max_open, max_files, exists
        self._orphans: set[str] = set()

    def _guard(self, branch: str) -> None:
        """The one rule behind every write: only topic branches, never the base branch."""
        if branch == self.base or not branch.startswith(self.prefix + "/") or len(branch) == len(self.prefix) + 1:
            raise ProposalError(f"refusing to write to '{branch}': only {self.prefix}/<topic> branches are allowed")

    def _call(self, method, path, body=None, ok=(200, 201, 204)):
        status, data = self.gh.request(method, path, body)
        if status not in ok:
            raise ProposalError(f"GitHub {method} {path} -> {status}: {(data or {}).get('message', '')}")
        return data

    def _open_prs(self) -> list[dict]:
        data = self._call("GET", "/pulls?state=open&per_page=100")
        return [p for p in data if p["head"]["ref"].startswith(self.prefix + "/")]

    def list_open(self) -> list[dict]:
        return [{"topic": p["head"]["ref"][len(self.prefix) + 1:], "branch": p["head"]["ref"],
                 "pr": p["html_url"], "title": p["title"]} for p in self._open_prs()]

    @staticmethod
    def _names(values, what: str) -> list[str]:
        """Plain note names; [[wrapped]] ones are accepted and unwrapped."""
        if not values:
            return []
        out = []
        for v in [values] if isinstance(values, str) else values:
            v = v.strip()
            if wrapped := re.fullmatch(r"\[\[(.+)\]\]", v):
                v = wrapped[1].strip()
            if not v:
                continue
            if len(v) > 120 or re.search(r"[\[\]\n|#^]", v):
                raise ProposalError(f"{what}: plain note names up to 120 chars, without [ ] | # ^ or line breaks")
            out.append(v)
        if len(out) > 20:
            raise ProposalError(f"{what}: at most 20 entries")
        return out

    @staticmethod
    def _validate_body(body: str) -> None:
        text = body.strip()
        if not text or len(body) > MAX_BODY:
            raise ProposalError(f"body must be non-empty and at most {MAX_BODY} chars")
        if LEADING_FRONTMATTER.match(text):
            raise ProposalError("body must not include frontmatter; send only the Markdown after the Properties block")
        if SECRET_LINE.search(body) or "PRIVATE KEY" in body:
            raise ProposalError("body looks like it contains a credential: remove it and refer to the secret store")

    def _validate(self, topic, title, body) -> str:
        if not (topic or "").strip():
            raise ProposalError("topic is empty: name the subject this note belongs to")
        slug = slugify(topic)
        if not title or len(title) > 120 or FORBIDDEN_TITLE.search(title) or title.startswith("."):
            raise ProposalError("title must be 1-120 chars without / \\ : * ? \" < > | # ^ [ ] or line breaks")
        self._validate_body(body)
        return slug

    def _render(self, title, body, parents, links, aliases) -> str:
        """The owner's template: created, updated, aliases, parent, links, tags, then `# Title`."""
        today = datetime.date.today().isoformat()

        def field(key, items, wrap):
            return [f"{key}:"] + [f"  - {json.dumps(f'[[{i}]]' if wrap else i, ensure_ascii=False)}" for i in items]

        head = ["---", f"created: {today}", f"updated: {today}"]
        head += field("aliases", aliases, False) + field("parent", parents, True) + field("links", links, True)
        head += ["tags:", f"  - {AGENT_TAG}", "---"]
        text = body.strip()
        if not re.match(r"#\s+\S", text):  # every note opens with its own title
            text = f"# {title}\n\n{text}"
        return "\n".join(head) + "\n" + text + "\n"

    def _file_exists(self, path: str, ref: str) -> bool:
        status, _ = self.gh.request("GET", f"/contents/{_q(path)}?ref={_q(ref)}")
        return status == 200

    def _ensure_branch(self, branch: str) -> None:
        self._guard(branch)
        data = self._call("GET", f"/git/ref/heads/{_q(self.base)}")
        status, body = self.gh.request(
            "POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": data["object"]["sha"]})
        if status not in (201, 422):  # 422: another agent created it first, which is fine
            raise ProposalError(f"cannot create branch {branch}: {status} {(body or {}).get('message', '')}")

    def _join_topic(self, slug: str):
        """Return the branch, its open PR (if any), and the open proposal count."""
        branch = f"{self.prefix}/{slug}"
        open_prs = self._open_prs()
        pr = next((p for p in open_prs if p["head"]["ref"] == branch), None)
        if pr:
            files = self._call("GET", f"/pulls/{pr['number']}/files?per_page=100")
            if len(files) >= self.max_files:
                raise ProposalError(f"PR for topic '{slug}' already has {len(files)} files; open a narrower topic")
        else:
            if len(open_prs) >= self.max_open:
                raise ProposalError(f"{self.max_open} proposals are already open; wait for review")
            self._ensure_branch(branch)
        return branch, pr, len(open_prs)

    def _ensure_pr(self, topic: str, branch: str, pr):
        """Open the PR for a topic on its first change. Returns (pr, is_new)."""
        if pr is not None:
            return pr, False
        status, data = self.gh.request("POST", "/pulls", {
            "title": f"knowledge: {topic.strip()[:80]}", "head": branch, "base": self.base,
            "body": "Changes proposed by an agent through the knowledge MCP server. "
                    "Review the diff, then merge or close; the branch is deleted afterwards.",
        })
        if status == 201:
            return data, True
        # 422: a concurrent agent opened it between our check and now
        pr = next((p for p in self._open_prs() if p["head"]["ref"] == branch), None)
        if pr is None:
            raise ProposalError(f"cannot open PR: {status} {(data or {}).get('message', '')}")
        return pr, False

    def propose_note(self, topic: str, title: str, body: str, parent=None, links=None, aliases=None) -> dict:
        """Create a new note. It is agent memory by definition, so it carries the agent tag."""
        slug = self._validate(topic, title, body)
        parents, link_names, alias_list = (
            self._names(parent, "parent"), self._names(links, "links"), self._names(aliases, "aliases"))
        path = f"{self.folder}/{title}.md"
        if self.exists(title) or self._file_exists(path, self.base):
            raise ProposalError(f"a note named '{title}' already exists; propose a different title")
        branch, pr, open_prs = self._join_topic(slug)
        if self._file_exists(path, branch):
            raise ProposalError(f"'{title}' is already proposed in topic '{slug}'")
        self._guard(branch)
        self._call("PUT", f"/contents/{_q(path)}", {
            "message": f"knowledge: add {title}",
            "content": base64.b64encode(
                self._render(title, body, parents, link_names, alias_list).encode()).decode(),
            "branch": branch,
        })
        pr, new_pr = self._ensure_pr(topic, branch, pr)
        return self._proposal_result(pr, branch, path, new_pr, open_prs)

    def propose_edit(self, topic: str, path: str, body: str, reason: str = "") -> dict:
        """Replace the body of an existing note. Frontmatter is kept as it is, apart from `updated`.

        The agent tag is never added here: a note the owner wrote stays the owner's, and only
        notes created through propose_note carry the tag.
        """
        if not (topic or "").strip():
            raise ProposalError("topic is empty: name the subject this change belongs to")
        if (not path.startswith(self.folder + "/") or not path.endswith(".md")
                or ".." in path.split("/") or "\\" in path):
            raise ProposalError(f"path must be a note under '{self.folder}/', as returned by search")
        self._validate_body(body)
        if len(reason) > 300 or "\n" in reason:
            raise ProposalError("reason: one line, at most 300 chars")
        if not self._file_exists(path, self.base):
            raise ProposalError(f"no such note: {path}; use propose_note to create a new one")
        branch, pr, open_prs = self._join_topic(slugify(topic))
        current = self._call("GET", f"/contents/{_q(path)}?ref={_q(branch)}")
        old = base64.b64decode(current["content"]).decode()
        match = re.match(r"\A---\n.*?\n---\n?", old, re.DOTALL)
        front, old_body = (old[:match.end()], old[match.end():]) if match else ("", old)
        if old_body.strip() == body.strip():
            raise ProposalError("the new body is identical to the current one")
        today = datetime.date.today().isoformat()
        front = re.sub(r"(?m)^updated:.*$", f"updated: {today}", front, count=1)
        whose = AGENT_TAG if re.search(rf"(?m)^\s*-\s*{re.escape(AGENT_TAG)}\s*$", front) else "owner's note"
        title = path.rsplit("/", 1)[-1][:-3]
        self._guard(branch)
        self._call("PUT", f"/contents/{_q(path)}", {
            "message": f"knowledge: edit {title} ({whose})" + (f"\n\n{reason}" if reason else ""),
            "content": base64.b64encode((front + body.strip() + "\n").encode()).decode(),
            "sha": current["sha"],
            "branch": branch,
        })
        pr, new_pr = self._ensure_pr(topic, branch, pr)
        return {**self._proposal_result(pr, branch, path, new_pr, open_prs),
                "owner_note": whose != AGENT_TAG}

    def _proposal_result(self, pr, branch, path, new_pr, open_prs) -> dict:
        return {"pr": pr["html_url"], "branch": branch, "path": path, "new_pr": new_pr,
                "open_prs": open_prs + int(new_pr), "max_open_prs": self.max_open}

    def cleanup(self) -> list[str]:
        """Delete topic branches that have no open PR (merged or closed).

        A branch must be seen orphaned on two consecutive runs: a branch created a moment ago
        by a concurrent propose_note has no PR yet and must not be removed.
        """
        open_heads = {p["head"]["ref"] for p in self._open_prs()}
        refs = self._call("GET", f"/git/matching-refs/heads/{self.prefix}/")
        orphans = {r["ref"][len("refs/heads/"):] for r in refs} - open_heads
        deleted = []
        for branch in orphans & self._orphans:
            self._guard(branch)
            status, _ = self.gh.request("DELETE", f"/git/refs/heads/{_q(branch)}")
            if status in (204, 404, 422):
                deleted.append(branch)
        self._orphans = orphans - set(deleted)
        return deleted
