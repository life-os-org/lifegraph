"""lifegraphd: holds the graph open while you're logged in, files agents' changes, answers reads.

Unlocking sends it the key material (derived from your passphrase by the `life` CLI). By default the keys are also kept in
the OS keyring, so the daemon reopens the graph by itself at login; `life lock` forgets them everywhere. An unlock with
--hours is temporary and is never kept. The sealed mailbox is only the fallback for when the graph is locked or the daemon is down.
"""
from __future__ import annotations

import base64
import fcntl
import hmac
import json
import os
import secrets
import socketserver
import threading
import time

from . import changes, crypto, ipc, keystore
from .inbox import append_sealed, inbox_path, pending  # noqa: F401  (re-exported: mcp_server and cli use daemon.append_sealed)
from .store import Store

PROTOCOL = 1            # bump on any incompatible change to the socket messages; see docs/PROTOCOL.md
DEFAULT_HOURS = 2.0
READ_OPS = {"search", "entity", "related", "timeline", "stats", "graph"}
ADMIN_OPS = READ_OPS | {"audit", "grants", "export", "merge_preview", "changes", "change"}
AGENT_OPS = {"search", "get", "propose", "undo"}


class State:
    def __init__(self):
        self.keys: crypto.Keys | None = None
        self.store: Store | None = None
        self.expires = 0.0       # 0 = until `life lock`
        self.admin = ""          # session secret handed to the CLI at unlock (also written to the runtime dir, mode 600)
        self.lock = threading.RLock()

    @property
    def unlocked(self) -> bool:
        return self.keys is not None

    def do_unlock(self, db_key: str, secret_key: str, hours: float | None):
        with self.lock:
            self.do_lock()
            k = crypto.Keys(bytes.fromhex(db_key), base64.b64decode(secret_key))
            self.store = Store(k)           # raises PermissionError if the key is wrong
            self.keys = k
            self.expires = time.time() + hours * 3600 if hours else 0.0
            self.admin = secrets.token_urlsafe(24)
            f = crypto.runtime_dir() / "admin.session"
            f.write_text(self.admin)
            os.chmod(f, 0o600)
            self.drain()
            return self.admin

    def do_lock(self):
        with self.lock:
            if self.store:
                self.store.close()
            if self.keys:
                self.keys.zero()
            self.keys = self.store = None
            (crypto.runtime_dir() / "admin.session").unlink(missing_ok=True)
            self.admin = ""
            self.expires = 0.0

    def drain(self) -> int:
        """Open every sealed entry and apply it. Poison entries are set aside, never lost."""
        with self.lock:
            if not self.unlocked or not inbox_path().exists():
                return 0
            done = 0
            with open(inbox_path(), "r+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                lines = [l for l in f.read().splitlines() if l.strip()]
                bad = []
                for l in lines:
                    try:
                        e = crypto.open_sealed(l, self.keys)
                        if e["op"] == "changeset":     # an agent's proposal that arrived while the graph was locked
                            a = e.get("args") or {}
                            g = self.store.grant_info(a.get("token"))
                            if not g:
                                raise ValueError("queued change from a token that is no longer valid")
                            r = changes.submit(self.store, a["ops"], g, e.get("source") or g["name"], e.get("ts"))
                            self.store.audit(g["name"], f"queued:{r['status']}", (r.get("changeset") or "; ".join(r.get("errors") or [r.get("message", "")]))[:120])
                            self.store.con.commit()
                        else:
                            self.store.apply(e["op"], e.get("args") or {}, e.get("ts"), e.get("source") or "unknown")
                        done += 1
                    except Exception as ex:   # noqa: BLE001
                        bad.append(l)
                        self.store.audit("daemon", "inbox_error", str(ex)[:120])
                        self.store.con.commit()
                if bad:
                    with open(crypto.home() / "inbox.failed", "a") as fb:
                        fb.write("\n".join(bad) + "\n")
                f.seek(0)
                f.truncate()
                fcntl.flock(f, fcntl.LOCK_UN)
            return done


S = State()


def authorized(msg: dict, admin_ok: bool) -> str | None:
    """Reads need a valid per-chat token, or the admin session the CLI got at unlock."""
    if admin_ok and msg.get("admin") and S.admin and hmac.compare_digest(str(msg["admin"]), S.admin):
        return "admin"
    t = msg.get("token")
    return S.store.check_token(t) if t and S.store else None


def handle(msg: dict) -> dict:
    cmd = msg.get("cmd")
    if cmd == "ping":
        return {"ok": True, "protocol": PROTOCOL}
    if cmd == "status":
        r = {"ok": True, "locked": not S.unlocked, "pending": pending(), "initialized": crypto.exists()}
        if S.unlocked:
            r["expires_in"] = max(0, int(S.expires - time.time())) if S.expires else None
            r["awaiting"] = changes.awaiting(S.store)       # changes agents proposed that need your yes
        return r
    if cmd == "unlock":
        try:
            admin = S.do_unlock(msg["db_key"], msg["secret_key"], float(msg["hours"]) if msg.get("hours") else None)
        except PermissionError as e:
            return {"ok": False, "error": "bad-key", "message": str(e)}
        return {"ok": True, "admin": admin, "expires_in": int(S.expires - time.time()) if S.expires else None}
    if cmd == "lock":
        S.do_lock()
        keystore.clear()
        return {"ok": True}
    if not S.unlocked:
        return {"ok": False, "error": "locked", "message": "The life graph is locked. Ask the user to run `life unlock`."}
    if cmd == "extend":
        with S.lock:
            S.expires = time.time() + float(msg.get("hours") or DEFAULT_HOURS) * 3600
        return {"ok": True, "expires_in": int(S.expires - time.time())}
    if cmd == "agent":
        return handle_agent(msg)
    if cmd == "drain":
        return {"ok": True, "applied": S.drain()}
    if cmd == "read":
        who = authorized(msg, admin_ok=True)
        op = msg.get("op")
        if not who:
            return {"ok": False, "error": "denied", "message": "No valid read grant. Ask the user to run `life grant <name>` and add the token to this chat's MCP config."}
        if op not in READ_OPS and not (who == "admin" and op in ADMIN_OPS):
            return {"ok": False, "error": "denied", "message": f"{op!r} is not available to this caller"}
        a = msg.get("args") or {}
        try:
            S.store.audit(who, f"read:{op}", json.dumps(a)[:100])
            S.store.con.commit()
            if op == "search":
                data = S.store.search(a["query"], int(a.get("limit", 10)))
            elif op == "entity":
                data = S.store.entity(a["name"], int(a.get("events", 10)))
            elif op == "related":
                data = S.store.related(a["name"], int(a.get("depth", 1)), int(a.get("limit", 40)))
            elif op == "timeline":
                data = S.store.timeline(a.get("since"), a.get("until"), a.get("entity"), a.get("kind"), int(a.get("limit", 30)))
            elif op == "graph":
                data = S.store.graph_view()
            elif op == "stats":
                data = S.store.stats()
            elif op == "merge_preview":
                data = S.store.merge(a["from"], a["to"], "cli", dry_run=True)
            elif op == "audit":
                data = S.store.audit_log(int(a.get("limit", 30)))
            elif op == "changes":
                data = changes.listing(S.store, a.get("status"), int(a.get("limit", 30)), full=True)
            elif op == "change":
                data = changes.show(S.store, a["id"], full=True)
            elif op == "grants":
                data = S.store.grants()
            elif op == "export":
                data = {"events": S.store.timeline(limit=10**6), "entities": [S.store.entity(f"{r[0]}:{r[1]}", 10**6) for r in S.store.con.execute("SELECT kind,name FROM entities")]}
            return {"ok": True, "data": data}
        except (KeyError, ValueError) as e:
            return {"ok": False, "error": "bad-request", "message": str(e)}
    if cmd == "admin":      # mutating admin ops: the caller must also present the database key (i.e. know the passphrase)
        if not (S.admin and hmac.compare_digest(str(msg.get("admin", "")), S.admin) and hmac.compare_digest(str(msg.get("db_key", "")), S.keys.db_key_hex)):
            return {"ok": False, "error": "denied", "message": "admin operations need the passphrase"}
        op, a = msg.get("op"), msg.get("args") or {}
        try:
            if op == "grant":
                return {"ok": True, "data": {"token": S.store.grant(a["name"], a.get("hours"), a.get("level", "read"), bool(a.get("ask_adds")), bool(a.get("auto_destructive")))}}
            if op == "revoke":
                return {"ok": True, "data": {"revoked": S.store.revoke(a["name"])}}
            if op == "merge":
                return {"ok": True, "data": S.store.merge(a["from"], a["to"], "cli")}
            if op == "forget":
                return {"ok": True, "data": {"forgotten": S.store.forget(a["ref"], "cli")}}
            if op == "approve":
                return {"ok": True, "data": changes.approve(S.store, a["id"])}
            if op == "reject":
                return {"ok": True, "data": changes.reject(S.store, a["id"])}
            if op == "undo":
                return {"ok": True, "data": changes.undo(S.store, a["id"])}
            if op == "purge_undo":
                return {"ok": True, "data": {"purged": changes.purge_undo(S.store)}}
        except (KeyError, ValueError) as e:
            return {"ok": False, "error": "bad-request", "message": str(e)}
    return {"ok": False, "error": "unknown-command", "message": f"unknown command {cmd!r}"}


def handle_agent(msg: dict) -> dict:
    """The four things an agent can do: search, get, propose, undo. What `propose` does depends on the agent's grant."""
    g = S.store.grant_info(msg.get("token") or "")
    if not g:
        return {"ok": False, "error": "denied", "message": "No valid token for this agent. The user can set one up with `life connect <agent>`."}
    op, a = msg.get("op"), msg.get("args") or {}
    if op not in AGENT_OPS:
        return {"ok": False, "error": "bad-request", "message": f"unknown agent op {op!r}"}
    try:
        S.store.audit(g["name"], f"agent:{op}", json.dumps(a, default=str)[:100])
        S.store.con.commit()
        if op == "search":
            q = (a.get("query") or "").strip()
            lim = max(1, min(int(a.get("limit", 10)), 50))
            data = S.store.search(q, lim) if q else S.store.timeline(a.get("since"), a.get("until"), a.get("about"), None, lim)
        elif op == "get":
            ref = str(a["ref"]).strip()
            data = changes.show(S.store, ref, g) if ref.startswith("c:") else S.store.get(ref, int(a.get("depth", 0)))
            if data is None:
                return {"ok": True, "data": {"error": f"nothing found for {ref!r}"}}
        elif op == "propose":
            data = changes.submit(S.store, a["ops"], g, msg.get("source") or g["name"], dry_run=bool(a.get("dry_run")))
        else:
            data = changes.undo(S.store, a["changeset"], g, g["name"])
        return {"ok": True, "data": data}
    except (KeyError, ValueError, TypeError) as e:
        return {"ok": False, "error": "bad-request", "message": str(e)}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(1 << 20)
            out = handle(json.loads(line)) if line else {"ok": False, "error": "empty"}
        except Exception as e:   # noqa: BLE001
            out = {"ok": False, "error": "internal", "message": str(e)[:200]}
        self.wfile.write(json.dumps(out, default=str).encode() + b"\n")


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def background():
    while True:
        time.sleep(1)
        try:
            if S.unlocked:
                if S.expires and time.time() >= S.expires:
                    S.do_lock()
                elif pending():
                    S.drain()
        except Exception:   # noqa: BLE001
            pass


def serve():
    path = ipc.sock_path()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    old = os.umask(0o177)
    srv = Server(path, Handler)
    os.umask(old)
    os.chmod(path, 0o600)
    kept = keystore.load() if crypto.exists() else None
    if kept:      # the keys were left in the OS keyring at the last `life unlock`: reopen without a passphrase
        try:
            S.do_unlock(kept[0], kept[1], None)
        except Exception:   # noqa: BLE001  (stale keys after a passphrase/identity change)
            keystore.clear()
    threading.Thread(target=background, daemon=True).start()
    print(f"lifegraphd listening on {path}", flush=True)
    try:
        srv.serve_forever()
    finally:
        S.do_lock()


if __name__ == "__main__":
    serve()
