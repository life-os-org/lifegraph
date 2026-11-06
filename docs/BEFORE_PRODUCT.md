# Before this can be taken to product

Also built since: `/lifegraph` (MCP prompt + command files), toggleable Claude Code auto-capture (propose-only, secret scrubbing, pluggable LLM
backend), local semantic search (fastembed / ollama / lite), and the Explore tab (search, open, fix, merge, delete with undo).

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

## Added with capture / semantic search / explorer
19. **Capture only covers Claude Code.** Codex and opencode rely on `/lifegraph`; add hooks/adapters if they expose session-end events. Capture quality has only been checked by hand on a few sessions, so build an eval set before trusting it.
20. **Capture sends transcripts to the chosen LLM.** The default backend sends them to Anthropic via the user's CLI; the UI needs a clear one-time consent screen and a per-project opt-out (today: `LIFEGRAPH_NO_CAPTURE=1`).
21. **Capture dedupe.** Re-running on the same session (or two sessions covering the same work) can propose near-duplicates; add a session-id ledger and similarity check against existing events.
22. **Semantic search model download.** fastembed downloads ~130 MB on first use (from Hugging Face); ship the model offline or pin a checksum. Vectors are brute-force cosine in memory: fine to ~100k items, then needs an ANN index.
23. **Explorer is a local page behind a passphrase prompt in the terminal**, with a force-directed layout that gets crowded past ~150 nodes (labels thin out). Needs real graph navigation (filters, neighbourhood expansion) and a desktop-app shell.
24. **Ids on old stores can be reused after a delete.** New stores use AUTOINCREMENT; older databases restore under a new ref when the old one was taken (reported as `new_refs`). Migrate old tables on a schema bump.
25. **Paths are stored in the graph** (folders and files that events touched, home shown as `~`). Decide what to do about paths outside the home folder or in private repos, add a per-project "don't record paths" switch, and handle moved/renamed folders (a `located_at` link goes stale).
26. **Native window is Linux-tested only.** Built and run on Wayland + AMD Vulkan. Needs checks on X11, NVIDIA/Intel, macOS (Metal) and Windows, plus a software-rendering fallback for machines without a usable GPU.
27. **Berkeley Mono is commercial.** It is embedded only from the owner's local copy at build time (never committed). A shipped binary would need Berkeley's redistribution terms or an open font instead.
28. **Labels lag the GPU by a few frames while the layout is still moving** (positions are read back asynchronously). Fine once settled; the proper fix is drawing labels with GPU text or reading positions back synchronously for the visible set.
29. **Window UX gaps:** no keyboard navigation or screen-reader support, no multi-select, no saved layouts/filters, the layout is O(n^2) per step on the GPU (fine to a few thousand nodes, then needs Barnes-Hut or a grid), and `life ui` still asks for the passphrase in the terminal before the window opens.
30. **Brand.** The logo is a first pass (Liberation Sans wordmark, SVGs only; the generator script was not kept). Needs a trademark/name check, a proper typeface decision (Liberation is a stand-in), small-size and favicon variants, and usage rules.
