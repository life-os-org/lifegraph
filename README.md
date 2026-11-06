# lifegraph

A private, encrypted graph of your life that your chats can write to. See [docs/DESIGN.md](docs/DESIGN.md).

```bash
scripts/install.sh      # the `life` command + a systemd user service (starts at login)
life init               # choose a passphrase; keep the recovery key it prints. The graph is open when init finishes.
life connect claude     # one step per agent: registers it and gives it its own token (also: codex, opencode, all)
life status
```

The graph stays open while you're logged in: `life unlock` keeps the keys in the OS keyring, so the daemon reopens it at
login without a passphrase. `life lock` closes it and forgets the keys everywhere (agents can still queue changes, which are
filed at the next unlock).

## How agents use it

Every agent gets the same four tools; its token says what it may do on its own.

| Tool | What it does |
|---|---|
| `search(query)` | find things; with no query, recent events. Every hit has a `ref` (e:entity v:event f:fact g:link c:change) |
| `get(ref)` | one thing in full, or the state of a change (`c:12`) |
| `propose(ops)` | add / edit / delete / merge. The reply says, per name, whether it attached to something **existing** or created something **new**, lists every change in words, and says what happens next. `dry_run` shows it without changing anything |
| `undo(changeset)` | reverse a change (or withdraw one still waiting); possible for 7 days |

`life connect AGENT --level`:

| Level | Adds | Edits, deletes, merges |
|---|---|---|
| `read` | refused | refused |
| `write` (default) | applied at once, undoable | refused |
| `manage` | applied at once | wait for your yes (`--auto-delete` applies them at once; merges always wait) |

`--ask` makes even adds wait for your yes. Run `life connect` again to change a level.

## Reviewing what agents want to change

```bash
life review          # a local page: each waiting change drawn as a graph diff (green added, red deleted), Yes / No
life pending         # the same, as text        life approve c:3 | all     life reject c:3
life history         # what agents changed      life undo c:3
life forget REF      # hard delete, no undo     life purge   # drop the undo data so deletes are really gone
```

Approving asks for your passphrase, so an agent that can run shell commands cannot approve its own changes.

Tests: `.venv/bin/python -m pytest` (a real daemon and a real MCP client run in a sandbox).
