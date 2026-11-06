# PRD: `life merge` (combine duplicate entities)

Audience: the engineer/AI implementing this in `~/projects/lifegraph`. Read `docs/DESIGN.md` first for the security model.

## Problem

Entities are matched only on exact `(kind, normalised name)` (`store.py`, `Store.resolve`; UNIQUE(kind, norm)). Chats name things
differently ("OSLab", "CS 301 Operating Systems", "course:OSLab", "topic:OSLab"), so the same real-world thing becomes
several entities. Typical symptoms: orphan "OSLab", orphan "Compilers-lab" next to "CS 410 Compilers", orphan "Side" next to
"Side projects", a duplicate, and "notes folder" next to "Notes". Events, facts and edges end up split across the copies,
so `life show` / `related` / `timeline` give partial answers.

There is no way to fix this today: no merge or rename command exists, and the MCP tools are write-only by design, so a chat
cannot repair it either. `forget` only deletes.

## Goal

A human-run CLI command that merges one entity into another without losing history, so the duplicate names keep resolving to the survivor.

## Non-goals

- No MCP tool for merging. Chats stay write-only; merging is a human action (same trust level as `forget`, `grant`).
- No automatic or fuzzy duplicate detection that merges by itself. Suggesting candidates is optional (see "Nice to have"); merging is always explicit.
- No LLM calls inside the system.
- Do not touch the inbox / sealed-mailbox format.

## Users and flow

One user (the owner), in a terminal, graph unlocked:

```
life merge OSLab "course:CS 301 Operating Systems"        # merge FROM into TO (second argument survives)
life merge --dry-run OSLab "course:CS 301 Operating Systems"
```

Arguments are entity refs resolved with the same rules as `resolve`: `kind:name`, or a bare name if it matches exactly one
entity. Bare names that match several entities must fail with the existing "say which with kind:name" message, listing the candidates. Resolve with `create=False`; a merge must never create an entity.
Also accept the numeric ref form used by `forget` (`e:12`).

## Behaviour (what "merge FROM into TO" does, in one transaction)

1. Refuse if FROM and TO are the same entity, or either is `person:Me` as FROM (the self entity can only be a target).
2. `event_entities`: repoint FROM -> TO. PK is (event_id, entity_id, role): where TO already has that (event, role), drop the FROM row instead of failing.
3. `edges`: repoint `src` and `dst` from FROM to TO. Then drop edges that became self-loops (src == dst), and de-duplicate exact duplicates (same src, rel, dst, start_ts, end_ts), keeping the oldest.
4. `facts`: repoint `entity_id`. Keep `ts`, `source`, `superseded_by` untouched. If text is identical to an existing fact on TO, drop the duplicate.
5. `aliases`: FROM's existing aliases move to TO (INSERT OR IGNORE; UNIQUE(entity_id, norm)). FROM's own name also becomes an alias of TO, so future writes using the old name resolve to TO. (Check the alias lookup in `resolve` is kind-scoped when a kind is given: if FROM was `topic:OSLab` and TO is `course:...`, a later `topic:OSLab` must still land on TO. Decide and document how; the likely answer is to also match aliases regardless of kind only for merged names, or to store the alias with its original kind.)
6. `props`: merge FROM.props into TO.props without overwriting keys TO already has. Keep the earlier `created_at`; set `updated_at` to now.
7. Delete FROM (cascades should have nothing left to cascade), refresh the FTS rows for TO, and remove FROM's FTS row.
8. Write an audit row: `action="merge"`, `ref="e:<from> -> e:<to>"`, source `cli`.
9. Print a summary: counts of events, edges, facts, aliases moved and duplicates dropped.

`--dry-run` runs the same logic inside a transaction and rolls back, printing the same summary.

## Where it lives

- `store.py`: `Store.merge(from_ref, to_ref, source, dry_run=False) -> dict` (counts), holding `self.lock`, in one SQLite transaction.
- `daemon.py`: new admin op `merge`, alongside `forget`. It requires the unlocked daemon like other admin ops.
- `cli.py`: `life merge` subcommand, prints the summary. Needs no passphrase beyond the existing unlock.
- Not exposed in `mcp_server.py`.

## Safety requirements

- Atomic: any error leaves the graph unchanged.
- Not destructive without a trace: recommend `life backup <file>` before the first merge (print a one-line reminder). Optionally refuse to run if no backup newer than N minutes exists, unless `--yes`.
- Idempotent in effect: merging a name that already resolves to TO (via alias) is a no-op with a clear message.
- Never print graph contents beyond entity names and counts.
- Tests must use sandbox dirs (`LIFEGRAPH_HOME`, `LIFEGRAPH_RUNTIME`) and fake data only. No real graph data in test output or commits.

## Acceptance criteria

1. Merging B into A: every event, fact and edge that referenced B now references A; `life show A` shows the union; `life show B` finds A through the alias.
2. A write after the merge using B's old name (as `link`, `remember`, `log_event`) attaches to A and creates no new entity.
3. Self-loop and duplicate edges/facts created by the merge are removed; event links are not duplicated.
4. Merging an entity into itself, merging `person:Me` away, and an ambiguous bare name all fail with clear errors and change nothing.
5. `--dry-run` reports counts and changes nothing.
6. Audit log has one `merge` row per merge.
7. FTS search for B's old name returns A, not a ghost row.
8. Existing tests still pass; new tests cover 1-7 in `tests/`.

## Nice to have (only after the above)

- `life dupes`: list likely duplicate pairs (same norm across kinds, substring/acronym matches, entities with zero edges) to help choose merges. Read-only, suggestions only.
- `life orphans`: list entities with no edges and no events.
- `life merge --kind`: change the kind during merge when the surviving entity has the wrong one (e.g. `topic:` -> `course:`).

## Context for the person running it (initial cleanup list)

Likely merges after the build, names to be confirmed from `life search`: OSLab -> `course:CS 301 Operating Systems`; Compilers-lab -> `course:CS 410 Compilers`; Side -> `project:Side projects`; `notes folder` -> `project:Notes`; plus one more duplicate the owner noticed.
