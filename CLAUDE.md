# knowledge-platform

Indexer and MCP server (`knowledge`) over the owner's Obsidian vault, plus the write path for agent memory.
Owner-facing docs are in Russian (`README.md`, `docs/`); code, comments, commits and this file are in English.
Notes written into the vault are in Russian.

## Layout

- `knowledge/index.py` parser, chunks, SQLite FTS5 + link graph, incremental `sync`, `FORMAT` version.
- `knowledge/embeddings.py` e5-small vectors (fastembed/ONNX) stored as BLOBs in the index, numpy brute force. OFF by default (`KNOWLEDGE_SEMANTIC=on`, image built with `KNOWLEDGE_SEMANTIC_BUILD=1`); measured: MRR 0.28 keyword vs 0.40 hybrid on 20 self-labelled queries, ~800 MB RAM, 24 min to embed 3410 chunks on 6 VM cores.
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

## Memory migration

`code-kvt` is moved (vault PRs #5, #6 merged); the other projects are not. The brief for the next run is
`docs/tasks/memory-migration.md`.

Open item, not yet built: the Mac does not pull the vault automatically after a merge
(obsidian-git auto-pull is off), so new memory shows up in search only after a manual `git pull`.
