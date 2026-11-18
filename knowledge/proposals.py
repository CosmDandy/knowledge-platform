"""Write path: new notes go to one branch and one pull request per topic. The vault is never touched.

Everything goes through the GitHub REST API, so there is no local clone, no lock and no rebase:
two agents on the same topic both end up adding commits to the same branch and PR.
"""
from __future__ import annotations

import base64
import datetime
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .index import SECRET_LINE

FORBIDDEN_TITLE = re.compile(r'[\\/:*?"<>|#^\[\]\n\r\t]')
MAX_BODY = 200_000


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


def slugify(topic: str) -> str:
    slug = re.sub(r"[^\w]+", "-", topic.strip().lower()).strip("-_")
    return slug[:50].strip("-_")


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="/")


class Proposer:
    def __init__(self, gh, prefix="knowledge", base="master", folder="01 Архив",
                 max_open=5, max_files=20, exists=lambda title: False):
        self.gh, self.prefix, self.base, self.folder = gh, prefix, base, folder
        self.max_open, self.max_files, self.exists = max_open, max_files, exists
        self._orphans: set[str] = set()

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

    def _validate(self, topic, title, body, parent, sources) -> str:
        slug = slugify(topic or "")
        if not slug:
            raise ProposalError("topic is empty: name the subject this note belongs to")
        if not title or len(title) > 120 or FORBIDDEN_TITLE.search(title) or title.startswith("."):
            raise ProposalError("title must be 1-120 chars without / \\ : * ? \" < > | # ^ [ ] or line breaks")
        if not body.strip() or len(body) > MAX_BODY:
            raise ProposalError(f"body must be non-empty and at most {MAX_BODY} chars")
        if SECRET_LINE.search(body) or "PRIVATE KEY" in body:
            raise ProposalError("body looks like it contains a credential: remove it and refer to the secret store")
        if parent and re.search(r"[\[\]\n]", parent):
            raise ProposalError("parent is a plain note name, without [[ ]]")
        if len(sources) > 20 or any(len(s) > 300 or "\n" in s for s in sources):
            raise ProposalError("sources: at most 20 single-line entries of 300 chars")
        return slug

    def _render(self, title, body, parent, sources) -> str:
        today = datetime.date.today().isoformat()
        head = ["---"]
        if parent:
            head.append(f"parent: {json.dumps(f'[[{parent}]]', ensure_ascii=False)}")
        head += [f"created: {today}", f"updated: {today}"]
        if sources:
            head.append("sources:")
            head += [f"  - {json.dumps(s, ensure_ascii=False)}" for s in sources]
        head.append("---")
        return "\n".join(head) + "\n" + body.strip() + "\n"

    def _file_exists(self, path: str, ref: str) -> bool:
        status, _ = self.gh.request("GET", f"/contents/{_q(path)}?ref={_q(ref)}")
        return status == 200

    def _ensure_branch(self, branch: str) -> None:
        data = self._call("GET", f"/git/ref/heads/{_q(self.base)}")
        status, body = self.gh.request(
            "POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": data["object"]["sha"]})
        if status not in (201, 422):  # 422: another agent created it first, which is fine
            raise ProposalError(f"cannot create branch {branch}: {status} {(body or {}).get('message', '')}")

    def propose_note(self, topic: str, title: str, body: str, parent: str | None = None,
                     sources: list[str] | None = None) -> dict:
        sources = sources or []
        slug = self._validate(topic, title, body, parent, sources)
        branch, path = f"{self.prefix}/{slug}", f"{self.folder}/{title}.md"
        if self.exists(title) or self._file_exists(path, self.base):
            raise ProposalError(f"a note named '{title}' already exists; propose a different title")
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
        if self._file_exists(path, branch):
            raise ProposalError(f"'{title}' is already proposed in topic '{slug}'")
        self._call("PUT", f"/contents/{_q(path)}", {
            "message": f"knowledge: add {title}",
            "content": base64.b64encode(self._render(title, body, parent, sources).encode()).decode(),
            "branch": branch,
        })
        new_pr = pr is None
        if new_pr:
            status, data = self.gh.request("POST", "/pulls", {
                "title": f"knowledge: {topic.strip()[:80]}", "head": branch, "base": self.base,
                "body": "Notes proposed by an agent through the knowledge MCP server. "
                        "Review the diff, then merge or close; the branch is deleted afterwards.",
            })
            if status == 201:
                pr = data
            else:  # 422: a concurrent agent opened it between our check and now
                pr = next((p for p in self._open_prs() if p["head"]["ref"] == branch), None)
                if pr is None:
                    raise ProposalError(f"cannot open PR: {status} {(data or {}).get('message', '')}")
                new_pr = False
        return {"pr": pr["html_url"], "branch": branch, "path": path, "new_pr": new_pr}

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
            status, _ = self.gh.request("DELETE", f"/git/refs/heads/{_q(branch)}")
            if status in (204, 404, 422):
                deleted.append(branch)
        self._orphans = orphans - set(deleted)
        return deleted
