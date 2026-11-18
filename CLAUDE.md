# knowledge-platform

Indexer and MCP server (`knowledge`) over the owner's Obsidian vault, plus the write path for agent memory.
Owner-facing docs are in Russian (`README.md`, `docs/`); code, comments, commits and this file are in English.
Notes written into the vault are in Russian.

## Layout

- `knowledge/index.py` parser, chunks, SQLite FTS5 + link graph, incremental `sync`, `FORMAT` version.
- `knowledge/embeddings.py` e5-small vectors (fastembed/ONNX) stored as BLOBs in the index, numpy brute force; model baked into the image.
- `knowledge/query.py` read-only queries (`search` = BM25 + vectors fused by RRF, `read`, `related`, `status`).
- `knowledge/proposals.py` the only write path: topic branch + one PR per topic through the GitHub REST API.
- `knowledge/server.py` MCP over streamable HTTP; background reindex and branch cleanup.
- `tests/` unittest; `Dockerfile`, `compose.yaml`; `secrets.sops.yaml` (encrypted, never decrypt into a file).
- `docs/agent-instructions.md` the block agents get; `docs/current-state.md` what was verified about the vault.

## Commands

```sh
python3 -m unittest discover -s tests -t .          # must stay green
uvx ruff check --config pyproject.toml .            # must stay clean
sops exec-env secrets.sops.yaml 'KNOWLEDGE_BIND=100.64.0.1 docker compose up -d --build'   # deploy
```

The container serves `http://100.64.0.1:7234/mcp` on the tailnet. Only `01 Архив` is indexed.

## Invariants (do not weaken)

- The vault is read-only for the server (`:ro` mount). Nothing writes into it except by merged PR.
- Writes go only to `knowledge/<topic>` branches (`Proposer._guard`). Never to `master`, never to other branches.
- Created notes carry the tag `agent-memory`; edits never add or change tags. The owner's tags are statuses
  (`0_Набросок`, ...), his own notes stay his.
- Note format is the owner's template: `created, updated, aliases, parent, links, tags`, then `# Title`.
- No credentials in notes or logs. Passwords live in sops; a note says where, not what.
- Branch names are ASCII only. Commits are conventional (`feat:`, `fix:`, `chore:` ...); gitleaks runs on commit.
- Test fixtures that look like credentials are built in code (`"token: " + "z" * 12`), not written literally.
- Do not call `propose_*` against the live server to "try something": it opens a real PR in the owner's repo.
  Use the fake GitHub in `tests/test_proposals.py`.

## Task: move agent memory into the vault

Goal: the memory Claude Code keeps per project becomes readable notes in the vault, so agents read and improve it
through the `knowledge` MCP server instead of local files. The owner reads these notes too, in Russian.

Sources (each has `MEMORY.md` as an index plus one file per topic):
`<dotfiles>/tools/claude/custom/knowledge/<project>/memory/` for `code-kvt`, `cloud-lab`, `local-lab`, `klian`,
`homepage`, `dotfiles`. Start with `code-kvt` (11 files, about 52 KB) and stop for review before the others.

Rules:

1. Dry run first: print a table `source file -> note title -> parent -> links -> sensitive lines found`.
   Create nothing until the table is written to `PROGRESS.md` and looks right.
2. One memory file becomes one note, translated to Russian, not summarised away: keep every fact.
   `MEMORY.md` becomes the hub note. Hub title: the project name (`code-kvt`); every other note has
   `parent: code-kvt` and links to the notes it depends on.
3. Titles: `<project> — <topic>` (em dash), no `: / \ # ^ [ ] | ? * < >`. Titles must be unique in the flat vault;
   `search` first and use `propose_edit` instead of creating a duplicate.
4. Passwords, tokens, keys, recovery codes: never. Contacts, access paths (hosts, URLs, how to get in),
   vulnerabilities and incident history are allowed and wanted. A secret becomes "stored in sops, key `X`".
5. Facts the owner has not verified stay marked as such in the text (when and how they were checked, or that they
   were not). Do not turn memory into certainty.
6. Where memory and reality disagree (the owner started collecting these), write the corrected fact and keep one
   line saying what the old memory claimed.
7. Topic per project: `<project>-memory` (e.g. `code-kvt-memory`), so one project is one PR. A PR holds at most
   20 files; split the topic (`code-kvt-memory-infra`, ...) if needed.
8. Never call a note saved: it exists after the owner merges the PR and `~/KnowledgeBase` is pulled on the Mac.
   Report the PR link, the list of notes, and what was left out and why.

Verification before reporting: `search` finds each new title and one fact from it after the merge and pull;
`related` on the hub lists all children; no note contains a line matching `password|token|secret|api[_-]?key`.

Open item, not yet built: the Mac does not pull the vault automatically after a merge
(obsidian-git auto-pull is off), so new memory shows up in search only after a manual `git pull`.
