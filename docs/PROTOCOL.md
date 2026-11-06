# Front-end contract (protocol 1)

Front-end apps use the life graph through two things only: the `life` command and the daemon's Unix socket. They never import lifegraph code, so the two repos release independently. A front-end should check `ping` -> `protocol` at startup and refuse to run against a different number.

## Socket
`$XDG_RUNTIME_DIR/lifegraph/sock` (mode 600). One JSON object per line in, one out.

| Message | Result |
|---|---|
| `{"cmd":"ping"}` | `{"ok":true,"protocol":1}` |
| `{"cmd":"status"}` | `locked`, `pending`, `initialized`, `expires_in` |
| `{"cmd":"read","op":"graph"\|"stats"\|"search"\|"entity"\|"related"\|"timeline","args":{...},"token":"lg_..."}` | `{"ok":true,"data":...}`; `graph` is names, kinds and links only |
| `{"cmd":"agent","op":"search"\|"get"\|"propose"\|"undo","args":{...},"token":"lg_...","source":"..."}` | what the MCP server calls for an agent; `propose` returns `status` applied / awaiting_approval / invalid / denied / preview, with a plain-language summary and per-name targets |
| `{"cmd":"read","op":"changes"\|"change","args":{"status":"pending"},"admin":"..."}` | changes agents proposed, each with a `graph` diff (nodes/links with state new / existing / removed) |
| `{"cmd":"admin","op":"approve"\|"reject"\|"undo","args":{"id":"c:3"},"admin":"...","db_key":"..."}` | decide on a change (needs the passphrase-derived key) |
| `{"cmd":"read","op":"get","args":{"ref":"e:12"},"admin":"..."}` | one entity / event / fact / link by ref (admin session) |
| `{"cmd":"admin","op":"apply","args":{"ops":[...]},"admin":"...","db_key":"..."}` | the owner's own edits (the Explore tab): applied at once, recorded as a change, undoable |
| `{"cmd":"agent","op":"names","args":{"limit":80},"token":"..."}` | entity names for tools that want to reuse the owner's spellings (used by capture) |
| `{"cmd":"lock"}` | locks and forgets keys (also clears the OS keyring entry) |

Additive since the first version: `status` also returns `awaiting` (changes waiting for the owner); `expires_in` is `null` for an unlock with no `--hours`
(it stays open until `life lock`); grants carry a `level` (read for apps). Apps that pass `--hours` and only read are unaffected.

## Unlocking from an app
`printf '%s\n' "$PASSPHRASE" | life unlock --stdin --json --hours 8 --app front-end-app` prints one object:
`{"ok":true,"expires_in":28800,"pending":0,"token":"lg_..."}` or `{"ok":false,"error":"bad-passphrase"|"not-initialised"|"unlock-failed","message":"..."}` with exit code 1.
`--app NAME` issues that app a fresh read token (replacing the previous one of the same name). Keep it in memory, not on disk.
The passphrase is never echoed or logged.

## What an app must not do
Hold the passphrase after the unlock call, write the token to disk, or expose graph data to anything not logged in.

`search` results (agents and `read`) are keyword + meaning hits merged; each carries `found_by`: keyword / meaning / both. `read` `stats` includes `search_index`
(backend, indexed, waiting).
