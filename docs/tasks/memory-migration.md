# Task: move agent memory into the vault

Status: `code-kvt` is done (vault PRs #5 and #6 merged). `cloud-lab`, `local-lab`, `klian`, `homepage` and
`dotfiles` have not been moved; do one project per run and stop for review before the next.

Goal: the memory Claude Code keeps per project becomes readable notes in the vault, so agents read and improve it
through the `knowledge` MCP server instead of local files. The owner reads these notes too, in Russian.

Sources (each has `MEMORY.md` as an index plus one file per topic):
`<dotfiles>/tools/claude/custom/knowledge/<project>/memory/` for `code-kvt`, `cloud-lab`, `local-lab`, `klian`,
`homepage`, `dotfiles`.

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
8. Never call a note saved: it exists after the owner merges the PR and `~/knowledge` is pulled on the Mac.
   Report the PR link, the list of notes, and what was left out and why.

Verification before reporting: `search` finds each new title and one fact from it after the merge and pull;
`related` on the hub lists all children; no note contains a line matching `password|token|secret|api[_-]?key`.
