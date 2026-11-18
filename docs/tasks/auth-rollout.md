# Task: roll out bearer-token auth for the knowledge MCP and retire project memory in dotfiles

Self-contained brief. Read it once, do not re-explore what it already states. Token budget is tight:
open only the files named here, no repo-wide greps, no re-reading files after you edit them.
Report in the format at the end.

## Facts (verified 2026-11-18, do not re-verify)

- Server: `~/knowledge-platform` (repo `CosmDandy/knowledge-platform`, branch `master`). Container `knowledge-mcp`
  on the Mac, published at `http://100.64.0.1:7234/mcp` (tailnet). Index of `~/KnowledgeBase/01 Архив`.
- Server-side auth is DONE and committed (`5aab9c2`, `knowledge/auth.py`): every HTTP request needs
  `Authorization: Bearer <token>`; token read from `/run/secrets/knowledge_token` (compose secret from
  env `KNOWLEDGE_MCP_TOKEN`); the server refuses to start without it unless `KNOWLEDGE_AUTH=off`.
  Tested: no token 401, wrong 401, right 200. **The running container is still the OLD, unauthenticated build.**
  Do NOT redeploy it until step 4: it would cut off every client that has no token yet.
- Token: generated, stored in `~/knowledge-platform/secrets.sops.yaml` under `KNOWLEDGE_MCP_TOKEN`
  (uncommitted change to that encrypted file: commit it), and extracted on the Mac to
  `~/.config/knowledge-platform/token` (0600). **Never print, echo, log or paste the token**; never put it
  in an env var of a container, a command line, a commit, a note, or this report.
- Client path convention (owner's decision): `~/.config/knowledge-platform/token`, same shape as the existing
  `~/.config/claude/token`.
- Secrets reach dev containers through `tools/zsh/conf.d/devpod.zsh` in `~/.dotfiles`:
  `_dp_put_file <id> '<dest, single-quoted>'` writes stdin as a 0600 file over ssh;
  `typeset -ga DP_SECRETS=( "name|description|function|position" ... )` (line ~1793) lists the secrets
  (claude, age, env, github, gitlab = positions 1..5); `tools/devpod/probe-host.sh` reports a digit per
  secret in the same order (see its header comment: "five digits"), and `devpod.zsh` ~line 723 reads them.
  A new secret = one `DP_SECRETS` line + a `_dp_put_knowledge` function + a sixth digit in the probe + a test
  there. Pattern to copy: `_dp_put_workenv` (file from the Mac) and `_dp_put_claude`.
- MCP registration lives in `tools/claude/custom/install.sh` (submodule repo, separate commits).
  `add_mcp` (line ~101) registers stdio servers; the Timing http block (lines ~146-148) is the pattern for an
  http server: `claude mcp add -s user -t http timing "${TIMING_MCP_URL:-http://100.64.0.1:7233}/mcp"`.
  `claude mcp add` has `-H/--header` but NO option for `headersHelper`; `claude mcp add` does not update an
  existing entry (remove, then add). Claude Code version in containers: 2.1.326.
- Dev containers reach `100.64.0.1:7234` over the tailnet (checked from `kvt-d-01`).
  Workspaces: `devpod list` (all on provider `kvt-d-01-ssh`, ssh as `<id>.devpod`).
  `knowledge` is currently registered (unauthenticated) in: the Mac user scope, `kvt-platform-main`,
  `kvt-cv-cosmdandy-dev-main`, and the `pi` agent on kvt-platform-main.

## Steps (in this order)

1. **Pick the client auth method by testing, not by docs.** Public reports conflict on whether
   `headers: {"Authorization": "Bearer ${VAR}"}` expands in user-scope config. Start a throwaway server:
   `docker run -d --name knowledge-auth-test -p 127.0.0.1:7299:7234 -v "$HOME/KnowledgeBase:/vault:ro"
   -v "$HOME/.config/knowledge-platform/token:/run/secrets/knowledge_token:ro" --read-only --tmpfs /tmp
   --tmpfs /data:uid=10001 --cap-drop ALL knowledge-platform:latest`. Register it under a TEMPORARY name
   with (a) `headersHelper` pointing at a small script that prints `{"Authorization":"Bearer <file contents>"}`
   as JSON and (b) a static header with `${VAR}`; `claude mcp get <name>` must say Connected. Prefer (a) if it
   works: it needs no env var and works in non-interactive shells. Remove the test registrations and the
   container afterwards. Record which one won and why in one line.
2. **Dotfiles delivery** (`~/.dotfiles`, repo main): add the `knowledge` secret to `DP_SECRETS`
   (`_dp_put_knowledge`: source `~/.config/knowledge-platform/token` on the Mac, dest
   `$HOME/.config/knowledge-platform/token`), extend `probe-host.sh` to six digits and fix the readers of that
   column, add the test the header comment asks for. Run `zsh -n` on touched zsh files and `shellcheck -S warning`
   on bash ones. Ship the headersHelper script (if step 1 chose it) from `tools/claude/` and make it
   executable; it must print `{}` and exit 0 when the token file is missing.
3. **Dotfiles install** (`tools/claude/custom/install.sh`): register `knowledge` as an http server with the
   chosen auth, URL `${KNOWLEDGE_MCP_URL:-http://100.64.0.1:7234/mcp}`, only if the token file exists
   (otherwise print one warning line). Idempotent: remove then add, safe to re-run. If the install needs the
   token file on the Mac, extract it from sops there (`sops decrypt --extract '["KNOWLEDGE_MCP_TOKEN"]'
   ~/knowledge-platform/secrets.sops.yaml`, stdout straight into a 0600 file, never to the terminal).
   Commit in the submodule first, then the pointer in the superproject; conventional commits, English.
4. **Roll out, then switch the server on.** (a) Deliver the token with `dp secrets <id> knowledge` to
   `kvt-platform-main` and `kvt-cv-cosmdandy-dev-main`, update their registration, and re-register on the Mac
   and in the `pi` config on kvt-platform-main. (b) Confirm every client still answers (old server). (c) Only
   then: `cd ~/knowledge-platform && sops exec-env secrets.sops.yaml 'KNOWLEDGE_BIND=100.64.0.1 docker compose
   up -d --build'`. (d) Verify each client with `claude mcp get knowledge` (Connected) AND one real `search`
   call, and that a request without the token returns 401. **Rollback if any client is cut off:**
   `KNOWLEDGE_AUTH=off sops exec-env secrets.sops.yaml 'KNOWLEDGE_BIND=100.64.0.1 docker compose up -d'`.
5. **Retire `tools/claude/custom/knowledge/code-kvt`** (submodule). It holds 11 memory files and an EMPTY
   template `CLAUDE.md` (project "work-odoo-main"). First reconcile each memory file with the vault notes tagged
   `agent-memory` (parent `code-kvt`, listed by `search` with `memory="agent", parent="code-kvt"`):
   backup_system, infra_overview, monitoring_stack, nomad_services, project_odoo, review_environments,
   security_issues, team, urls_and_access have an evident counterpart; **`repos_overview.md` and
   `user_profile.md` do not**. For those two, compare content with the vault; if a fact is missing, write it
   through `propose_note` (topic `code-kvt-memory`, parent `code-kvt`, Russian, no credentials) and STOP at
   the PR for the owner to merge; do not delete until it is merged and visible in `search`. Then delete the
   directory, remove whatever links it into `~/.claude/projects/*/memory` (check how the install wires it before
   deleting; a dangling symlink is a bug), commit in the submodule and bump the pointer. Do not touch
   `knowledge/dotfiles/` or any other project: only `code-kvt` for now.
6. **Describe the system in the vault through the MCP, not in a local file.** `propose_note`, topic
   `knowledge-platform`, Russian, owner's template, no credentials, titles `knowledge-platform — <topic>`: a hub
   note `knowledge-platform` (parent: `Dotfiles`) plus notes for: architecture and data flow, operations
   (deploy, rollback, rebuild, where secrets live), search settings and measured quality (BM25 only; semantic
   search off by default; numbers in `CLAUDE.md` of the repo), and auth. Facts only from this brief and the repo.
   Leave the PR for the owner to merge.

## Constraints

- Read-modify-write one thing at a time; run the repo's checks after each (`python3 -m unittest discover -s
  tests -t .`, `uvx ruff check --config pyproject.toml .` in knowledge-platform; `zsh -n` / `shellcheck` in
  dotfiles). Stage by explicit path, never `git add -A`.
- Do not push anything and do not merge any PR: the owner does that. Do not change master protection.
- Production-affecting steps are only 4(c) and the PRs of 5-6. Everything else is local and reversible.
- If the same step fails twice with no new hypothesis, stop and report what you ruled out.

## Report (20 lines max, Russian)

`✔ done` per step with the check that proved it; `✘ failed` what you tried; `! found` anything that contradicts
this brief; `» act` what only the owner can do (merge PRs, push). No secrets, no file dumps.
