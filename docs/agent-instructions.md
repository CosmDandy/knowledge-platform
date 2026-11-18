# Block for agents' CLAUDE.md

Paste into the user-level `CLAUDE.md` of every machine and container where the `knowledge` MCP server is connected. Written for the model, so in English; the notes themselves are in Russian (assumption: the vault is Russian and the owner reads this memory).

```markdown
## Knowledge base (MCP server `knowledge`)

This is your memory for facts about infrastructure, projects and decisions. Use it instead
of the local auto-memory files.

- Before answering or changing anything non-trivial, `search` (exact names work best:
  hostnames, services, versions), then `read` the best hits. A hit with `agent_memory: true`
  is your own memory; the rest are the owner's notes: trust them, do not rewrite them
  without a reason.
- Write only what you verified. Put the origin and the date in `sources`. Never write
  passwords, tokens or keys: they live in sops, say where they are, not what they are.
  Contacts, access paths and known vulnerabilities are fine.
- New fact: `propose_note`, one note per entity or decision, written in Russian. Existing
  note: `propose_edit` with the complete new body and no frontmatter. Use `parent` to attach
  a note to its hub note.
- One `topic` per piece of work, reused on every call, so everything lands in one pull request.
- A proposal is not saved until the owner merges the PR. Tell the owner the PR link and do
  not describe the change as done.
- Tags are managed by the server: notes you create get `agent-memory`; never add or remove tags.
```
