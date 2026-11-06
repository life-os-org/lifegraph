"""`life`: the human side of the life graph.

  life init                 create the encrypted store (prints a recovery key once) and open it
  life unlock               open it (stays open across restarts via the OS keyring); `life lock` closes it and forgets the keys
  life status               locked / unlocked, changes waiting for you
  life connect AGENT        register claude / codex / opencode: --level read | write (default) | manage
  life capture on|off|status   auto-capture: when a Claude Code session ends, propose what's worth remembering (you review it)
  /lifegraph                in any connected agent: save what's worth remembering from this chat (asks you first)
  life semantic status|backend NAME|reindex   meaning-based search (local embeddings)
  life ui                   the page: review what agents want to change (graph diff, yes/no) and explore/search/fix your graph (alias: review)
  life pending|approve|reject   the same review from the terminal
  life history / undo c:N   what agents changed; reverse one (for 7 days)
  life search|show|related|timeline|stats    read the graph
  life log "text"           add a note from the terminal
  life forget REF / merge FROM TO / purge    delete for good / fold duplicates / drop the undo data
  life grant|revoke|grants  per-agent tokens (connect does this for you)   life audit: who did what
  life backup <file>        encrypted tarball (safe to put anywhere)
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from . import capture, changes, commands, config, crypto, daemon, inbox, ipc, keystore

ADMIN_FILE = lambda: crypto.runtime_dir() / "admin.session"


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def _pinentry(prompt: str, desc: str) -> str | None:
    """Ask for a secret in a native dialog (the pinentry GPG uses), so it works when there is no terminal, for example from a
    chat's `!` shell, and the secret never passes through that chat. Returns None if cancelled or unavailable."""
    exe = os.environ.get("LIFEGRAPH_PINENTRY") or shutil.which("pinentry")
    if not exe:
        return None
    esc = lambda t: t.replace("%", "%25").replace("\n", "%0A").replace("\r", "%0D")
    try:
        pe = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None

    def talk(cmd: str) -> list[bytes]:
        pe.stdin.write(cmd.encode() + b"\n"); pe.stdin.flush()
        out = []
        for line in pe.stdout:
            line = line.rstrip(b"\n")
            if line.startswith(b"OK") or line.startswith(b"ERR"):
                out.append(line)
                return out
            out.append(line)
        return out
    try:
        pe.stdout.readline()                                   # greeting
        for c in (f"SETTITLE Life graph", f"SETDESC {esc(desc)}", f"SETPROMPT {esc(prompt)}"):
            talk(c)
        res = talk("GETPIN")
        talk("BYE")
    except (BrokenPipeError, OSError):
        return None
    finally:
        try:
            pe.kill()
        except OSError:
            pass
    if not res or not res[-1].startswith(b"OK"):
        die("Cancelled.")
    for line in res:
        if line.startswith(b"D "):
            import re
            return re.sub(rb"%([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), line[2:]).decode("utf-8", "replace")
    return ""


def ask(prompt: str, confirm: bool = False) -> str:
    if os.environ.get("LIFEGRAPH_PASSPHRASE"):          # for tests and scripted setups only
        return os.environ["LIFEGRAPH_PASSPHRASE"]
    if sys.stdin.isatty():
        p = getpass.getpass(prompt)
        if confirm and getpass.getpass("Again: ") != p:
            die("Those didn't match.")
        return p
    # No terminal (a chat's `!` shell, a script): never fall back to an echoing prompt.
    p = _pinentry(prompt.rstrip(": "), "Enter your life graph passphrase.")
    if p is None:
        die("This needs a real terminal or a password dialog, and I can't find either.\n"
            "Open a normal terminal and run it there. (Typing it into a chat would put your passphrase in the conversation.)")
    if confirm and _pinentry("Again", "Type it once more to confirm.") != p:
        die("Those didn't match.")
    return p


def ensure_daemon():
    r = ipc.call({"cmd": "ping"}, timeout=2)
    if r.get("ok"):
        return
    if subprocess.run(["systemctl", "--user", "start", "lifegraphd"], capture_output=True).returncode == 0:
        for _ in range(40):
            if ipc.call({"cmd": "ping"}, timeout=1).get("ok"):
                return
            time.sleep(0.1)
    subprocess.Popen([sys.executable, "-m", "lifegraph.daemon"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if ipc.call({"cmd": "ping"}, timeout=1).get("ok"):
            return
        time.sleep(0.1)
    die("Couldn't start the life graph daemon.")


def admin_read(op: str, **args):
    f = ADMIN_FILE()
    admin = f.read_text().strip() if f.exists() else ""
    r = ipc.call({"cmd": "read", "op": op, "args": args, "admin": admin})
    if not r.get("ok"):
        die(r.get("message") or r.get("error") or "failed")
    return r["data"]


def admin_write(op: str, passphrase: str | None = None, **args):
    keys = crypto.unlock(passphrase or ask("Passphrase: "))
    f = ADMIN_FILE()
    r = ipc.call({"cmd": "admin", "op": op, "args": args, "admin": f.read_text().strip() if f.exists() else "", "db_key": keys.db_key_hex})
    keys.zero()
    if not r.get("ok"):
        die(r.get("message") or r.get("error") or "failed")
    return r["data"]


def fmt_age(sec: int) -> str:
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m {sec % 60:02d}s"


# ---------------------------------------------------------------- commands
def cmd_init(a):
    if crypto.exists():
        die("Already initialised. (Your data is in " + str(crypto.home()) + ")")
    pw = ask("Choose a passphrase (10+ characters): ", confirm=True)
    try:
        rec = crypto.create(pw)
    except ValueError as e:
        die(str(e))
    print(f"\nCreated an encrypted life graph in {crypto.home()}\n")
    print("RECOVERY KEY (shown once, never stored; keep it somewhere safe and offline):\n")
    print(f"    {rec}\n")
    print("If you forget the passphrase, this is the only way back in. There is no reset.\n")
    try:
        keys = crypto.unlock(pw)
        r = _open(keys, None, True)
        keys.zero()
        print("Unlocked and ready." + ("" if r.get("kept") else " (No OS keyring found, so you'll run `life unlock` after a restart.)")
              if r.get("ok") else "Run `life unlock` to open it.")
    except SystemExit:
        print("Run `life unlock` to open it.")


def _open(keys, hours: float | None, keep: bool) -> dict:
    """Hand the keys to the daemon. With no --hours they are also kept in the OS keyring so it reopens itself at login."""
    ensure_daemon()
    r = ipc.call({"cmd": "unlock", "db_key": keys.db_key_hex, "secret_key": base64.b64encode(keys.secret_key).decode(), "hours": hours})
    if r.get("ok"):
        ADMIN_FILE().write_text(r["admin"])
        os.chmod(ADMIN_FILE(), 0o600)
        if keep and not hours:
            r["kept"] = keystore.save(keys.db_key_hex, base64.b64encode(keys.secret_key).decode())
    return r


def cmd_unlock(a):
    """Interactive unlock for people; with --stdin/--json it is the contract a front-end app (such as a desktop console) uses:
    the passphrase arrives on stdin, one JSON object comes back on stdout, and --app NAME also mints that app a read token."""
    js = a.json
    def fail(code, msg):
        if js:
            print(json.dumps({"ok": False, "error": code, "message": msg})); sys.exit(1)
        die(msg)
    if not crypto.exists():
        fail("not-initialised", "Nothing to unlock yet: run `life init` first.")
    ensure_daemon()
    pw = sys.stdin.readline().rstrip("\n") if a.stdin else ask("Passphrase: ")
    try:
        keys = crypto.unlock(pw)
    except PermissionError:
        fail("bad-passphrase", "Wrong passphrase.")
    finally:
        pw = ""
    r = _open(keys, a.hours, not a.no_keep)
    token = None
    if r.get("ok") and a.app:
        g = ipc.call({"cmd": "admin", "op": "grant", "args": {"name": a.app, "hours": None, "level": "read"}, "admin": r["admin"], "db_key": keys.db_key_hex})
        token = (g.get("data") or {}).get("token")
    keys.zero()
    if not r.get("ok"):
        fail("unlock-failed", r.get("message") or "unlock failed")
    s = ipc.call({"cmd": "status"})
    if js:
        print(json.dumps({"ok": True, "expires_in": r["expires_in"], "pending": s.get("pending", 0), "awaiting": s.get("awaiting", 0),
                          "kept": bool(r.get("kept")), **({"token": token} if token else {})}))
        return
    if r["expires_in"]:
        print(f"Unlocked for {fmt_age(r['expires_in'])}.")
    else:
        print("Unlocked." + (" It stays open across restarts until you run `life lock`." if r.get("kept")
                             else " (No OS keyring found, so this lasts until the daemon restarts or you run `life lock`.)"))
    if s.get("awaiting"):
        print(f"{s['awaiting']} change(s) from your agents are waiting for you: `life review`")


def cmd_lock(a):
    ipc.call({"cmd": "lock"})
    keystore.clear()
    ADMIN_FILE().unlink(missing_ok=True)
    print("Locked. The keys are forgotten everywhere; agents can still queue changes, filed after your next unlock.")


def cmd_status(a):
    r = ipc.call({"cmd": "status"})
    if r.get("error") == "daemon-down":
        print(("Not started" if crypto.exists() else "Not initialised") + f"; mailbox backlog: {inbox.pending()} sealed entries.")
        return
    if not r.get("initialized"):
        print("Not initialised: run `life init`.")
        return
    print(("LOCKED" if r["locked"] else "UNLOCKED" + (f", {fmt_age(r['expires_in'])} left" if r.get("expires_in") else "")) + f"; mailbox backlog: {r['pending']}")
    if r.get("awaiting"):
        print(f"{r['awaiting']} change(s) from your agents are waiting for your yes: `life review`")
    if not r["locked"]:
        print(json.dumps(admin_read("stats"), indent=1))


def cmd_log(a):
    if not crypto.exists():
        die("Run `life init` first.")
    ents = [{"kind": k, "name": n} for k, _, n in (w.partition(":") for w in a.with_)]
    args = {"text": a.text, **({"entities": ents} if ents else {}), **({"when": a.when} if a.when else {})}
    inbox.append_sealed({"op": "note", "args": args, "ts": time.time(), "source": "cli"})
    print("Sealed into the mailbox." + ("" if ipc.call({"cmd": "status"}).get("locked") is False else " It will be filed the next time you unlock."))


def show(x):
    print(json.dumps(x, indent=1, ensure_ascii=False))


def cmd_search(a): show(admin_read("search", query=" ".join(a.query), limit=a.limit))
def cmd_show(a): show(admin_read("entity", name=" ".join(a.name)))
def cmd_related(a): show(admin_read("related", name=" ".join(a.name), depth=a.depth))
def cmd_timeline(a): show(admin_read("timeline", since=a.since, until=a.until, entity=a.entity, limit=a.limit))
def cmd_stats(a): show(admin_read("stats"))
def cmd_audit(a): show(admin_read("audit", limit=a.limit))
def cmd_grants(a): show(admin_read("grants"))


def cmd_grant(a):
    tok = admin_write("grant", name=a.name, hours=a.hours, level=a.level, ask_adds=a.ask, auto_destructive=a.auto_delete)["token"]
    print(f"Token for {a.name!r} ({a.level}; shown once):\n\n    {tok}\n")
    print("Put it in that agent's MCP config as LIFEGRAPH_TOKEN (`life connect` does this for you).")
    print(f"Cancel it any time with: life revoke {a.name}")


def cmd_revoke(a):
    print("Revoked." if admin_write("revoke", name=a.name)["revoked"] else "No such grant.")


def _print_change(c, graph=False):
    print(f"{c['changeset']}  {c['agent']}" + (f" ({c['source']})" if c.get("source") and c["source"] != c["agent"] else "") + f"  {c['when']}  [{c['status']}]" + (f"  ({c['note']})" if c.get("note") else ""))
    for line in c["summary"]:
        print(f"    • {line}")


def cmd_pending(a):
    cs = admin_read("changes", status="pending")
    if not cs:
        print("Nothing is waiting for you.")
    for c in cs:
        _print_change(c)
    if cs:
        print("\nSee them as a graph: life review     Decide here: life approve c:N | life reject c:N")


def cmd_history(a):
    for c in admin_read("changes", limit=a.limit):
        _print_change(c)


def _decide(op: str, ids: list[str]):
    if ids == ["all"]:
        ids = [c["changeset"] for c in admin_read("changes", status="pending")]
        if not ids:
            print("Nothing is waiting."); return
    keys = crypto.unlock(ask("Passphrase (this is you saying yes): "))
    admin = ADMIN_FILE().read_text().strip() if ADMIN_FILE().exists() else ""
    for i in ids:
        r = ipc.call({"cmd": "admin", "op": op, "args": {"id": i}, "admin": admin, "db_key": keys.db_key_hex})
        if not r.get("ok"):
            print(f"{i}: {r.get('message') or r.get('error')}")
        else:
            d = r["data"]
            print(f"{i}: {d.get('status')}" + (" — " + "; ".join(d.get("errors", [])) if d.get("errors") else ""))
    keys.zero()


def cmd_approve(a): _decide("approve", a.ids)
def cmd_reject(a): _decide("reject", a.ids)
def cmd_undo(a): _decide("undo", [a.id])


def cmd_purge(a):
    print(f"Dropped the undo data of {admin_write('purge_undo')['purged']} change(s). What they deleted is now gone for good.")


def _ui_binary() -> str | None:
    """The native GPU window, if it has been built (scripts/install-ui.sh). Otherwise `life ui` falls back to the browser page."""
    cands = [os.environ.get("LIFEGRAPH_UI"), shutil.which("lifegraph-ui"), str(Path(__file__).resolve().parent.parent / "ui/target/release/lifegraph-ui")]
    return next((c for c in cands if c and Path(c).is_file() and os.access(c, os.X_OK)), None)


def cmd_review(a):
    """A local page (127.0.0.1, random address, closes itself when idle): review what agents want to change as a graph diff, and explore/fix your graph."""
    import http.server
    import secrets as _s
    import threading
    import webbrowser
    from .review_ui import PAGE
    if not crypto.exists():
        die("Run `life init` first.")
    ensure_daemon()
    if ipc.call({"cmd": "status"}).get("locked"):
        die("The graph is locked: run `life unlock` first.")
    keys = crypto.unlock(ask("Passphrase (approving is you saying yes): "))
    dbk = keys.db_key_hex
    keys.zero()
    admin = ADMIN_FILE().read_text().strip() if ADMIN_FILE().exists() else ""
    tok = _s.token_urlsafe(16)
    last = [time.time()]

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *x): pass

        def _guard(self) -> bool:
            host = self.headers.get("Host", "")
            if host not in (f"127.0.0.1:{srv.server_port}", f"localhost:{srv.server_port}") or not self.path.startswith(f"/{tok}/"):
                self.send_error(404); return False
            last[0] = time.time()
            return True

        def _json(self, obj, code=200):
            b = json.dumps(obj, default=str).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)

        def do_GET(self):
            if not self._guard(): return
            from urllib.parse import parse_qs, urlparse
            u = urlparse(self.path[len(tok) + 1:])
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            rd = lambda op, **kw: ipc.call({"cmd": "read", "op": op, "args": kw, "admin": admin})
            if u.path == "/":
                b = PAGE.encode()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(b)))
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
                self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)
            elif u.path == "/api/changes":
                p, h = rd("changes", status="pending", limit=100), rd("changes", limit=15)
                self._json({"pending": p.get("data", []), "recent": [c for c in h.get("data", []) if c["status"] != "pending"]})
            elif u.path == "/api/stats":
                self._json(rd("stats").get("data") or {})
            elif u.path == "/api/graph":
                self._json(rd("graph").get("data") or {"nodes": [], "links": []})
            elif u.path == "/api/search":
                self._json(rd("search", query=q.get("q", ""), limit=20).get("data") or [])
            elif u.path == "/api/get":
                self._json(rd("get", ref=q.get("ref", "")).get("data") or {"error": "not found"})
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._guard(): return
            sub = self.path[len(tok) + 1:]
            if sub not in ("/api/decide", "/api/apply") or "application/json" not in self.headers.get("Content-Type", ""):
                self.send_error(404); return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            if sub == "/api/apply":      # the owner editing their own graph from the Explore tab: applied at once, recorded, undoable
                r = ipc.call({"cmd": "admin", "op": "apply", "args": {"ops": body.get("ops")}, "admin": admin, "db_key": dbk})
                self._json(r.get("data") or {"status": "error", "message": r.get("message") or r.get("error")}); return
            if body.get("action") not in ("approve", "reject", "undo"):
                self._json({"ok": False, "message": "bad action"}, 400); return
            self._json(ipc.call({"cmd": "admin", "op": body["action"], "args": {"id": body.get("id")}, "admin": admin, "db_key": dbk}))

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    url = f"http://127.0.0.1:{srv.server_port}/{tok}/" + (f"#{a.id}" if a.id else "")
    native = None if a.web else _ui_binary()
    if native and not a.no_open:
        print("Opening the native window (close it to stop).", flush=True)
    else:
        print(f"Review page: {url}\n(Ctrl-C to close; it also closes itself after 20 idle minutes.)", flush=True)
        if not a.no_open:
            threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    def watchdog():
        while time.time() - last[0] < 1200:
            time.sleep(5)
        srv.shutdown()
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        if native and not a.no_open:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            subprocess.run([native, "--url", url])
        else:
            srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown() if native and not a.no_open else None
        srv.server_close()


def cmd_forget(a):
    print("Forgotten." if admin_write("forget", ref=a.ref)["forgotten"] else "Nothing with that ref.")


def cmd_merge(a):
    if a.dry_run:
        r = admin_read("merge_preview", **{"from": a.src, "to": a.dst})
    else:
        print("Tip: `life backup <file>` first if you have not recently; a merge cannot be undone.")
        r = admin_write("merge", **{"from": a.src, "to": a.dst})
    if r.get("noop"):
        print(r["message"]); return
    print(("Would merge" if r.get("dry_run") else "Merged") + f" {r['from']} into {r['to']}:")
    for k in ("events_moved", "event_links_dropped", "edges_moved", "edges_dropped", "facts_moved", "facts_dropped", "aliases_moved"):
        print(f"  {k.replace('_', ' ')}: {r[k]}")
    if r.get("dry_run"): print("(dry run: nothing changed)")


AGENTS = {"claude": "claude-code", "codex": "codex", "opencode": "opencode"}


def _mcp_bin() -> str:
    return str(Path(sys.executable).parent / "lifegraph-mcp")


def _register(agent: str, token: str):
    """Point one agent at the life graph MCP server (user scope, every folder). The token carries what it may do."""
    exe, src = _mcp_bin(), AGENTS[agent]
    env = {"LIFEGRAPH_SOURCE": src, "LIFEGRAPH_TOKEN": token}
    if agent == "claude":
        subprocess.run(["claude", "mcp", "remove", "lifegraph", "-s", "user"], capture_output=True)
        cmd = ["claude", "mcp", "add", "-s", "user", "lifegraph", *[x for k, v in env.items() for x in ("-e", f"{k}={v}")], "--", exe]
    elif agent == "codex":
        subprocess.run(["codex", "mcp", "remove", "lifegraph"], capture_output=True)
        cmd = ["codex", "mcp", "add", "lifegraph", *[x for k, v in env.items() for x in ("--env", f"{k}={v}")], "--", exe]
    else:   # opencode keeps MCP servers in its JSON config
        cfg = Path(os.environ.get("OPENCODE_CONFIG") or Path.home() / ".config/opencode/opencode.json")
        d = json.loads(cfg.read_text()) if cfg.exists() else {}
        d.setdefault("mcp", {})["lifegraph"] = {"type": "local", "command": [exe], "environment": env, "enabled": True}
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(d, indent=2))
        cfg.chmod(0o600)
        return
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        die(f"Couldn't register with {agent}: {(r.stderr or r.stdout).strip()[:200]}")


LEVEL_TEXT = {"read": "can search and read; cannot change anything",
              "write": "can add (applied at once, undoable); edits and deletes are refused",
              "manage": "can add at once; edits, deletes and merges wait for your yes"}


def cmd_connect(a):
    """One step per agent: registers it and gives it a token whose level says what it may do on its own."""
    names = list(AGENTS) if a.agent == "all" else [a.agent]
    keys = crypto.unlock(ask("Passphrase: "))
    ensure_daemon()
    if ipc.call({"cmd": "status"}).get("locked"):
        u = _open(keys, None, True)
        if not u.get("ok"):
            die(u.get("message") or "unlock failed")
    admin = ADMIN_FILE().read_text().strip()
    for n in names:
        g = ipc.call({"cmd": "admin", "op": "grant", "args": {"name": f"agent-{n}", "hours": None, "level": a.level, "ask_adds": a.ask,
                                                             "auto_destructive": a.auto_delete}, "admin": admin, "db_key": keys.db_key_hex})
        if not g.get("ok"):
            die(g.get("message") or "couldn't issue a token")
        _register(n, g["data"]["token"])
        cf = commands.install(n)
        extra = (" Every change waits for your yes." if a.ask else "") + (" Its deletes apply without asking." if a.auto_delete and a.level == "manage" else "")
        print(f"{n}: connected as {a.level} ({LEVEL_TEXT[a.level]}).{extra} Start a new {n} session to pick it up. Type /lifegraph in it to save what matters from a chat.")
    keys.zero()
    print("Change the level any time by running connect again. Cut one off: life revoke agent-<name>")


def cmd_backup(a):
    out = Path(a.file)
    with tarfile.open(out, "w:gz") as t:
        for n in ("identity.json", "graph.db", "inbox.log"):
            p = crypto.home() / n
            if p.exists():
                t.add(p, arcname=n)
    out.chmod(0o600)
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB). Everything in it is encrypted; the passphrase and recovery key are not.")


def cmd_passwd(a):
    try:
        crypto.change_passphrase(ask("Current passphrase: "), ask("New passphrase: ", confirm=True))
    except (PermissionError, ValueError) as e:
        die(str(e))
    print("Passphrase changed. (The recovery key still works.)")


def cmd_capture(a):
    act = a.action
    if act == "hook":
        capture.hook(sys.stdin.read(), capture.life_exe())
        return
    if act == "run":
        if not a.transcript:
            die("--transcript FILE is required")
        r = capture.run_logged(a.transcript, a.agent, a.cwd or "", dry_run=a.dry_run, force=a.force or a.dry_run)
        show(r) if (a.dry_run or sys.stdout.isatty()) else None
        return
    cfg = config.load()["capture"]
    if act == "on":
        keys = crypto.unlock(ask("Passphrase: "))
        ensure_daemon()
        if ipc.call({"cmd": "status"}).get("locked"):
            u = _open(keys, None, True)
            if not u.get("ok"):
                die(u.get("message") or "unlock failed")
        admin = ADMIN_FILE().read_text().strip()
        g = ipc.call({"cmd": "admin", "op": "grant", "args": {"name": "capture", "hours": None, "level": "write", "ask_adds": True},
                      "admin": admin, "db_key": keys.db_key_hex})
        keys.zero()
        if not g.get("ok"):
            die(g.get("message") or "couldn't issue the capture token")
        capture.token_path().write_text(g["data"]["token"])
        os.chmod(capture.token_path(), 0o600)
        cfg = config.update("capture", enabled=True, **{k: v for k, v in (("backend", a.backend), ("model", a.model), ("command", a.command)) if v})["capture"]
        try:
            capture.set_claude_hook(capture.life_exe(), True)
        except RuntimeError as e:
            die(str(e))
        where = {"claude": "Anthropic (through your Claude CLI)", "ollama": "your local Ollama (it stays on this machine)", "command": f"`{cfg['command']}`"}[cfg["backend"]]
        print(f"Auto-capture is ON. When a Claude Code session ends, its conversation (secrets scrubbed) is sent to {where}.")
        print("It only proposes additions. Review them with `life review`. Skip one session: LIFEGRAPH_NO_CAPTURE=1. Turn off: life capture off")
    elif act == "off":
        config.update("capture", enabled=False)
        try:
            capture.set_claude_hook(capture.life_exe(), False)
        except RuntimeError as e:
            print(f"(couldn't touch Claude settings: {e})")
        print("Auto-capture is OFF and the Claude Code hook is removed. Nothing already captured is lost; review or reject it with `life pending`.")
    else:
        print(f"auto-capture: {'ON' if cfg['enabled'] else 'off'}   backend: {cfg['backend']}" + (f" ({cfg['model']})" if cfg["backend"] == "claude" else ""))
        print(f"Claude Code hook: {'installed' if capture.hook_installed() else 'not installed'}   token: {'yes' if capture.token() else 'no'}   min messages: {cfg['min_turns']}")
        try:
            tail = capture.log_path().read_text().splitlines()[-5:]
        except FileNotFoundError:
            tail = []
        print("recent runs:" + ("" if tail else " none yet"))
        for l in tail:
            print("  " + l)


def cmd_semantic(a):
    if a.action == "backend":
        if not a.name:
            die("which backend? auto | fastembed | lite | ollama")
        config.update("semantic", backend=a.name)
        print(f"Search backend set to {a.name}. The daemon re-indexes in the background; check with `life semantic status`.")
        return
    if a.action == "reindex":
        print(f"Re-indexing {admin_write('reindex')['queued']} items in the background.")
        return
    st = admin_read("stats").get("search_index", {})
    print(f"backend: {st.get('backend') or 'starting'}   indexed: {st.get('indexed')}   waiting: {st.get('waiting')}")
    if st.get("note"):
        print(st["note"])


def cmd_start(a):
    ensure_daemon()
    print("Daemon running.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="life", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(f=cmd_init)
    s = sub.add_parser("unlock"); s.add_argument("--hours", type=float, default=None, help="temporary unlock for this long (never kept in the keyring)")
    s.add_argument("--no-keep", action="store_true", help="don't leave the keys in the OS keyring")
    s.add_argument("--stdin", action="store_true", help="read the passphrase from stdin (for front-end apps)"); s.add_argument("--json", action="store_true", help="machine-readable result")
    s.add_argument("--app", metavar="NAME", help="also issue this app a read token (returned in --json output)"); s.set_defaults(f=cmd_unlock)
    sub.add_parser("lock").set_defaults(f=cmd_lock)
    sub.add_parser("status").set_defaults(f=cmd_status)
    sub.add_parser("start").set_defaults(f=cmd_start)
    s = sub.add_parser("log"); s.add_argument("text"); s.add_argument("--when"); s.add_argument("--with", dest="with_", action="append", default=[], metavar="kind:name"); s.set_defaults(f=cmd_log)
    s = sub.add_parser("search"); s.add_argument("query", nargs="+"); s.add_argument("--limit", type=int, default=10); s.set_defaults(f=cmd_search)
    s = sub.add_parser("show"); s.add_argument("name", nargs="+"); s.set_defaults(f=cmd_show)
    s = sub.add_parser("related"); s.add_argument("name", nargs="+"); s.add_argument("--depth", type=int, default=1); s.set_defaults(f=cmd_related)
    s = sub.add_parser("timeline"); s.add_argument("--since"); s.add_argument("--until"); s.add_argument("--entity"); s.add_argument("--limit", type=int, default=30); s.set_defaults(f=cmd_timeline)
    sub.add_parser("stats").set_defaults(f=cmd_stats)
    s = sub.add_parser("audit"); s.add_argument("--limit", type=int, default=30); s.set_defaults(f=cmd_audit)
    sub.add_parser("grants").set_defaults(f=cmd_grants)
    s = sub.add_parser("grant"); s.add_argument("name"); s.add_argument("--hours", type=float)
    s.add_argument("--level", choices=("read", "write", "manage"), default="read")
    s.add_argument("--ask", action="store_true", help="even adds wait for your yes"); s.add_argument("--auto-delete", action="store_true", help="manage: edits/deletes apply without asking")
    s.set_defaults(f=cmd_grant)
    s = sub.add_parser("revoke"); s.add_argument("name"); s.set_defaults(f=cmd_revoke)
    s = sub.add_parser("merge"); s.add_argument("src", metavar="FROM"); s.add_argument("dst", metavar="TO", help="this one survives")
    s.add_argument("--dry-run", action="store_true"); s.set_defaults(f=cmd_merge)
    s = sub.add_parser("connect"); s.add_argument("agent", choices=[*AGENTS, "all"])
    s.add_argument("--level", choices=("read", "write", "manage"), default="write", help="read | write (default) | manage")
    s.add_argument("--ask", action="store_true", help="even adds wait for your yes"); s.add_argument("--auto-delete", action="store_true", help="with manage: edits/deletes apply without asking")
    s.set_defaults(f=cmd_connect)
    s = sub.add_parser("forget"); s.add_argument("ref", help="e:12 / v:7 / f:3 / g:5 (hard delete, no undo)"); s.set_defaults(f=cmd_forget)
    s = sub.add_parser("capture"); s.add_argument("action", choices=("on", "off", "status", "run", "hook"))
    s.add_argument("--backend", choices=("claude", "ollama", "command")); s.add_argument("--model", help="claude model alias (default haiku)")
    s.add_argument("--command", help="with --backend command: a program that reads the prompt on stdin and prints the JSON reply")
    s.add_argument("--transcript"); s.add_argument("--agent", default="claude"); s.add_argument("--cwd")
    s.add_argument("--dry-run", action="store_true", help="run: show what would be proposed"); s.add_argument("--force", action="store_true", help="run even if capture is off"); s.set_defaults(f=cmd_capture)
    s = sub.add_parser("semantic"); s.add_argument("action", choices=("status", "backend", "reindex"), nargs="?", default="status"); s.add_argument("name", nargs="?"); s.set_defaults(f=cmd_semantic)
    sub.add_parser("pending").set_defaults(f=cmd_pending)
    s = sub.add_parser("approve"); s.add_argument("ids", nargs="+", metavar="c:N|all"); s.set_defaults(f=cmd_approve)
    s = sub.add_parser("reject"); s.add_argument("ids", nargs="+", metavar="c:N|all"); s.set_defaults(f=cmd_reject)
    s = sub.add_parser("history"); s.add_argument("--limit", type=int, default=20); s.set_defaults(f=cmd_history)
    s = sub.add_parser("undo"); s.add_argument("id", metavar="c:N"); s.set_defaults(f=cmd_undo)
    for nm in ("review", "ui"):
        s = sub.add_parser(nm, help="open the review + explore page"); s.add_argument("id", nargs="?", metavar="c:N"); s.add_argument("--no-open", action="store_true", help="just print the page's address")
        s.add_argument("--web", action="store_true", help="use the browser page even if the native window is installed"); s.set_defaults(f=cmd_review)
    sub.add_parser("purge").set_defaults(f=cmd_purge)
    s = sub.add_parser("backup"); s.add_argument("file"); s.set_defaults(f=cmd_backup)
    sub.add_parser("passwd").set_defaults(f=cmd_passwd)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
