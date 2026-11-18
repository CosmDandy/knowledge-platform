# Block for agents' CLAUDE.md

Paste into the user-level `CLAUDE.md` of every machine and container where the `knowledge` MCP server is connected. Written for the model, so in English; the notes themselves are in Russian (assumption: the vault is Russian and the owner reads this memory).

```markdown
## Knowledge base (MCP server `knowledge`)

This is your memory for facts about infrastructure, projects and decisions. Use it instead
of the local auto-memory files.

- Before answering or changing anything non-trivial, `search` (exact names work best:
  hostnames, services, versions; it matches words, not meaning;
  `parent="<project hub>"` limits it to one project), then `read` the best hits. A hit with `agent_memory: true`
  is your own memory; the rest are the owner's notes: trust them, do not rewrite them
  without a reason.
- Write only what you verified, and say in the text how and when you checked it. Never write
  passwords, tokens or keys: they live in sops, say where they are, not what they are.
  Contacts, access paths and known vulnerabilities are fine.
- New fact: `propose_note`, one note per entity or decision, written in Russian. The server
  builds the frontmatter from the owner's template and adds the `# <title>` heading, so send
  only the text. Set `parent` to the hub note the topic belongs under (for project memory,
  the project name, e.g. "code-kvt") and `links` to related notes; both are plain names and
  may point at notes that do not exist yet. A title cannot contain `: / \ # ^ [ ] | ? * < >`:
  write "code-kvt — инфраструктура", not "code-kvt: инфраструктура".
- Existing note: `propose_edit` with the complete new body and no frontmatter.
- One `topic` per piece of work, reused on every call, so everything lands in one pull request.
- A proposal is not saved until the owner merges the PR. Tell the owner the PR link and do
  not describe the change as done.
- Tags are managed by the server: notes you create get `agent-memory`; never add or remove tags.
```
