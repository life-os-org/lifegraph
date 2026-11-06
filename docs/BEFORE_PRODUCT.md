# Before this can be taken to product

Done in the streamlining pass: changesets (`propose`/`undo`) with per-name "existing or new" resolution, graph-diff review page,
three permission levels per agent, undo for 7 days, graph kept open via the OS keyring, four MCP tools instead of eight.

## Blockers (don't ship without)
1. **Same-user trust boundary.** Everything runs as the user. An agent with a shell can read `admin.session` (runtime dir) and the
   OS keyring entry, so *read* level is a guardrail against mistakes and prompt injection, not isolation from a hostile agent.
   Approval, grant and forget already need the passphrase. For real isolation: run the daemon as a separate system user and give
   each agent a socket it can only use through its token; stop writing `admin.session` (hand it to the CLI another way).
2. **Keyring dependence.** Headless / no Secret Service machines fall back to "unlock after every restart". Needs a clear UX,
   a macOS Keychain / Windows Credential Manager check, and a decision on whether "open while logged in" is the default for everyone.
3. **Undo data keeps deleted text for 7 days.** `life purge` drops it; productise: per-delete "delete permanently", a visible countdown,
   and an option to set the window to 0.
4. **Merges can't be undone** and always wait for approval. Add a snapshot-based undo or a pre-merge backup.
5. **Review UX.** The review page is a local server with a passphrase prompt in the terminal. A product needs it in the desktop
   app (notification when something is waiting, inline approval from the chat client via MCP elicitation where supported).
6. **Migration safety.** `grants.level`, `changesets` are created on open. Add a schema_version table, backup-before-migrate, downgrade story.
7. **Passphrase/recovery UX.** No reset by design; needs a confirmed recovery-key flow at init and a tested "lost passphrase" path.
8. **Packaging.** Venv + `pip -e` today. Needs an installer, upgrade-safe autostart, and an uninstall that also removes MCP registrations.
9. **Existing registrations break.** Agents registered before this change use the old tool names/env var (`--read`, `LIFEGRAPH_READ_TOKEN`
   still load, but the old tools are gone). Re-run `life connect <agent>`; ship a `life doctor` that detects and fixes it.

## Should-have
10. **Delete by query** ("forget everything about X"): `propose` takes refs only. Add a `forget_matching` op that returns the matching refs for confirmation, with a cap.
11. **Orphans.** Deleting an entity leaves events that mention nobody (the preview names them). Add opt-in cascade and a sweep command.
12. **Per-agent limits.** Rate/blast-radius caps (ops per hour, max deletes per change) and an anomaly line in the audit log.
13. **Agent-visible history.** `search` can't filter by source/agent, so "undo everything this chat did" is only possible per change. Add `history(agent)` and bulk undo.
14. **Entity edits.** Rename/retype an entity and edit link properties (only facts are editable today).
15. **Observability.** Structured daemon logs; `life doctor` (inbox.failed viewer, queue health, token status).
16. **Performance.** `graph_view` caps at 400 nodes with full scans; test at 50k events. Preview computation re-runs the ops in a transaction, fine at 50 ops, test large merges.
17. **Tests still missing:** concurrent proposals, drain racing an approve, locked-daemon undo, a pending change whose targets were since deleted (approve returns `invalid`; covered only by hand), macOS/Windows paths.
18. **Docs.** User-facing privacy statement (what is kept, how long, where) and onboarding that explains the three levels.
