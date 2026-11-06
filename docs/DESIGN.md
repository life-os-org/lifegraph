# Life graph: design

A private, encrypted graph of your life that every agent can use within the permission level you give it.

## Goals

1. **Document a life as a graph.** People, projects, courses, places and decisions, linked to each other and to a timeline of what happened.
2. **Easy to feed from any chat.** Saying "log that I finished the OS lab with Sam" in Claude Code, Codex or opencode should be one tool call.
3. **Encrypted and isolated.** Not every chat should be able to read it, and nothing is stored in the clear.
4. **Private by default.** No cloud service and no extra LLM: the chat's own model does the extraction.

## Shape

```
 any chat ──MCP (stdio)──> lifegraph-mcp ──seal to public key──> inbox.log (append-only, unreadable)
                                                                     │  drained while unlocked
 you ──> life CLI ──passphrase──> lifegraphd (key in memory only while unlocked)
                                      │
                                      └──> graph.db  (SQLCipher / AES-256, one file)
```

### Storage

One SQLCipher file: `entities` (kind + name + aliases), `events` (what happened, when, with whom), `edges` (typed, optionally
time-bounded links), `facts` (durable statements about an entity), an FTS5 index, `grants` (per-chat read tokens) and an `audit`
log. Plain SQLite underneath means one file to back up and no database server.

### Extraction

The calling model does it. Tools take structured arguments (`people`, `projects`, `when` as an ISO date), so no LLM runs inside
the system. Entities are matched by normalised name and aliases, writes are de-duplicated (a chat retrying never double-logs),
and a bare name that matches several kinds of entity is refused rather than guessed.

### Keys

- A random 32-byte database key and an X25519 "mailbox" key pair are created once.
- Both secrets are wrapped (argon2id + XSalsa20-Poly1305) under your passphrase and, separately, under a one-time recovery key.
  Only wrapped forms are on disk. The mailbox's public half is stored in the clear.
- **There is no reset.** Lose the passphrase and the recovery key and the data is gone.

### Isolation, in layers

| Layer | What it stops |
|---|---|
| **Every agent has its own token with a level** (read / write / manage), set by `life connect`; it can expire or be revoked. The daemon, not the MCP server, enforces it. | A chat you didn't pick, or one doing more than you meant |
| **Agents propose; the graph answers with exactly what will happen.** Edits, deletes and merges wait for your yes (a graph diff in `life review`); everything applied is undoable for 7 days. | A confident but wrong delete or edit |
| **Approving, granting and `forget` re-ask for the passphrase.** | An agent with a shell approving its own change |
| **The graph is open while you're logged in** (keys in the OS keyring, forgotten by `life lock`; `--hours` unlocks are temporary and never kept). The sealed mailbox is only the fallback while it is locked. | A stolen disk or backup (still ciphertext) |
| **Audit log** records every write source and every read. | Quiet misuse; you can check |
| **systemd sandbox** on the daemon: read-only home except its data dir, no new privileges, no core dumps. | The daemon being used as a stepping stone |

### What this does not protect against

Everything runs as your user. While the graph is **unlocked**, a program running as you with full shell access can talk to the
daemon's socket. A chat that can run arbitrary commands can also read its own MCP config, so a token in that config is only as
private as that chat. This design stops accidental reads, prompt-injection fishing, other chats, and a stolen disk or backup.
It is not a sandbox against hostile code that is already running as you. If you want that later, the next step is running the
daemon as a separate system user.

## Not built yet

- Semantic recall (local embeddings).
- Console integration: finished tasks become events automatically, a "log:" voice command, and a graph/timeline view.
- Importing existing material.
