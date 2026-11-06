"""Whole-system test in a sandbox: real daemon process, real CLI, real MCP client over stdio."""
import asyncio, json, os, subprocess, sys, time
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PW = "correct horse battery"
DAEMONS = {}          # the sandbox's own daemon per data dir (never touch the real one)
BIN = os.path.join(os.path.dirname(sys.executable))

@pytest.fixture
def sandbox(tmp_path):
    env = dict(os.environ, LIFEGRAPH_HOME=str(tmp_path / "home"), LIFEGRAPH_RUNTIME=str(tmp_path / "run"), LIFEGRAPH_PASSPHRASE=PW,
               LIFEGRAPH_SOURCE="test-chat", PYTHON_KEYRING_BACKEND="fakering.FileKeyring", FAKERING_FILE=str(tmp_path / "keyring.json"),
               HOME=str(tmp_path / "h"), LIFEGRAPH_EMBED="lite", PYTHONPATH=os.pathsep.join(filter(None, [os.path.dirname(__file__), os.environ.get("PYTHONPATH", "")])))
    (tmp_path / "h").mkdir()
    daemon = subprocess.Popen([os.path.join(BIN, "lifegraphd")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(50):
        if (tmp_path / "run" / "lifegraph" / "sock").exists(): break
        time.sleep(0.1)
    DAEMONS[env["LIFEGRAPH_HOME"]] = daemon
    yield env
    DAEMONS[env["LIFEGRAPH_HOME"]].terminate(); DAEMONS[env["LIFEGRAPH_HOME"]].wait(5)

def life(env, *args, check=True):
    r = subprocess.run([os.path.join(BIN, "life"), *args], env=env, capture_output=True, text=True)
    if check and r.returncode: raise AssertionError(f"life {args} failed: {r.stderr}")
    return r

async def mcp(env, tool, args, token=None):
    e = dict(env, **({"LIFEGRAPH_TOKEN": token} if token else {}))
    params = StdioServerParameters(command=os.path.join(BIN, "lifegraph-mcp"), args=[], env=e)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            res = await s.call_tool(tool, args) if tool else None
            return tools, (res.structured_content if res and res.structured_content is not None else (json.loads(res.content[0].text) if res and res.content else None))

def grant(env, name, *flags):
    return [l for l in life(env, "grant", name, *flags).stdout.split() if l.startswith("lg_")][0]

def unwrap(x): return x.get("result", x) if isinstance(x, dict) else x

def test_one_flow_add_review_delete_undo(sandbox):
    env = sandbox
    assert "Unlocked and ready" in life(env, "init").stdout                     # no separate unlock step to get started
    assert "UNLOCKED" in life(env, "status").stdout
    w = grant(env, "chat-w", "--level", "write"); m = grant(env, "chat-m", "--level", "manage"); r = grant(env, "chat-r", "--level", "read")

    # every agent sees the same four tools; the grant decides what they may do
    tools, out = asyncio.run(mcp(env, "propose", {"ops": [{"op": "event", "title": "Finished OS lab", "when": "2026-10-14T17:30", "people": ["Sam"], "projects": ["OSLab"], "body": "it was brutal"},
                                                          {"op": "fact", "subject": "person:Sam", "text": "owns a zebra-striped bike"}]}, token=w))
    assert tools == ["search", "get", "propose", "undo"] and out["status"] == "applied"
    assert {t["as"]: t["status"] for t in out["targets"]} == {"person:Sam": "new", "project:OSLab": "new"}     # it said what it attached to
    time.sleep(0.2)
    assert "brutal" in life(env, "search", "brutal").stdout                       # filed immediately, no unlock/drain wait

    # read-only agents read but cannot write; writers cannot delete
    _, hits = asyncio.run(mcp(env, "search", {"query": "zebra"}, token=r)); assert "Sam" in json.dumps(hits)
    _, den = asyncio.run(mcp(env, "propose", {"ops": [{"op": "fact", "subject": "person:Sam", "text": "x"}]}, token=r)); assert den["status"] == "denied"
    _, den = asyncio.run(mcp(env, "propose", {"ops": [{"op": "forget", "ref": "v:1"}]}, token=w)); assert den["status"] == "denied"

    # a manage agent finds the ref, previews, asks; the delete waits for the owner
    _, ent = asyncio.run(mcp(env, "get", {"ref": "person:Sam"}, token=m)); ent = unwrap(ent); fref = ent["facts"][0]["ref"]
    _, pv = asyncio.run(mcp(env, "propose", {"ops": [{"op": "forget", "ref": fref}], "dry_run": True}, token=m)); assert pv["status"] == "preview" and "zebra" in json.dumps(pv)
    _, held = asyncio.run(mcp(env, "propose", {"ops": [{"op": "forget", "ref": fref}]}, token=m)); assert held["status"] == "awaiting_approval"
    assert "zebra" in life(env, "show", "Sam").stdout and "1 change" in life(env, "status").stdout
    assert "zebra" in life(env, "pending").stdout
    life(env, "approve", held["changeset"])
    assert "zebra" not in life(env, "show", "Sam").stdout and "zebra" not in life(env, "search", "zebra").stdout
    _, st = asyncio.run(mcp(env, "get", {"ref": held["changeset"]}, token=m)); assert st["status"] == "applied"
    life(env, "undo", held["changeset"]); assert "zebra" in life(env, "show", "Sam").stdout

    # the review page serves a graph diff for pending changes, and only to the right address
    _, held2 = asyncio.run(mcp(env, "propose", {"ops": [{"op": "fact", "subject": "person:Sam", "text": "likes tea"}, {"op": "forget", "ref": fref}]}, token=m))
    from lifegraph import ipc
    os.environ["LIFEGRAPH_RUNTIME"] = env["LIFEGRAPH_RUNTIME"]
    admin = open(os.path.join(env["LIFEGRAPH_RUNTIME"], "lifegraph", "admin.session")).read().strip()
    ch = ipc.call({"cmd": "read", "op": "changes", "args": {"status": "pending"}, "admin": admin})["data"]
    g = ch[0]["graph"]; assert ch[0]["changeset"] == held2["changeset"] and {n["state"] for n in g["nodes"]} == {"existing"} and any(n.get("adds") == ["likes tea"] for n in g["nodes"]) and any(n.get("removes") for n in g["nodes"])
    life(env, "reject", held2["changeset"]); assert "likes tea" not in life(env, "show", "Sam").stdout

    _, n = asyncio.run(mcp(env, "undo", {"changeset": out["changeset"]}, token=w)); assert n["status"] == "undone" and "brutal" not in life(env, "search", "brutal").stdout

    # locked: reads refused, agents can still queue, and the queue is checked against their grant when the graph reopens
    life(env, "lock"); assert "LOCKED" in life(env, "status").stdout and life(env, "search", "zebra", check=False).returncode != 0
    _, q = asyncio.run(mcp(env, "propose", {"ops": [{"op": "note", "text": "written while locked", "topics": ["testing"]}]}, token=w)); assert q["status"] == "queued"
    inbox = open(os.path.join(env["LIFEGRAPH_HOME"], "inbox.log")).read(); assert "locked" not in inbox and "testing" not in inbox
    life(env, "unlock"); time.sleep(1.5)
    assert "written" in life(env, "search", "written", "locked").stdout

    # encrypted backup contains no plaintext
    import tarfile; bk = os.path.join(os.path.dirname(env["LIFEGRAPH_HOME"]), "backup.tgz"); life(env, "backup", bk)
    with tarfile.open(bk) as t:
        blob = b"".join(t.extractfile(m_).read() for m_ in t.getmembers())
    assert b"brutal" not in blob and b"OSLab" not in blob and b"Sam" not in blob

def test_graph_reopens_by_itself_after_a_daemon_restart(sandbox):
    env = sandbox; life(env, "init"); assert "UNLOCKED" in life(env, "status").stdout
    DAEMONS[env["LIFEGRAPH_HOME"]].terminate(); DAEMONS[env["LIFEGRAPH_HOME"]].wait(5)
    d = DAEMONS[env["LIFEGRAPH_HOME"]] = subprocess.Popen([os.path.join(BIN, "lifegraphd")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            if "UNLOCKED" in life(env, "status", check=False).stdout: break
            time.sleep(0.1)
        assert "UNLOCKED" in life(env, "status").stdout                              # keys came from the keyring, no passphrase
        life(env, "lock"); d.terminate(); d.wait(5)
        d = DAEMONS[env["LIFEGRAPH_HOME"]] = subprocess.Popen([os.path.join(BIN, "lifegraphd")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True); time.sleep(1)
        assert "LOCKED" in life(env, "status").stdout                                 # `life lock` forgot them for good
    finally:
        pass          # the fixture stops whichever daemon is current

def test_wrong_passphrase_and_recovery(sandbox):
    env = sandbox; rec = [l.strip() for l in life(env, "init").stdout.splitlines() if l.strip() and l.strip().isalnum() and l.strip().isupper()][0]
    r = life(dict(env, LIFEGRAPH_PASSPHRASE="not the passphrase"), "unlock", check=False); assert r.returncode != 0 and "Wrong passphrase" in r.stderr
    assert life(dict(env, LIFEGRAPH_PASSPHRASE=rec), "unlock").returncode == 0           # the recovery key opens it too

def test_unlock_window_expires(sandbox):
    env = sandbox; life(env, "init"); life(env, "unlock", "--hours", "0.0007")           # ~2.5 seconds
    assert "UNLOCKED" in life(env, "status").stdout; time.sleep(4); assert "LOCKED" in life(env, "status").stdout


FAKE_PINENTRY = """#!/usr/bin/env python3
import sys
print("OK Pleased to meet you", flush=True)
for line in sys.stdin:
    c = line.strip()
    if c == "GETPIN":
        print("D %PASS%", flush=True); print("OK", flush=True)
    elif c == "BYE":
        print("OK closing connection", flush=True); break
    else:
        print("OK", flush=True)
"""

def test_unlock_without_a_terminal_uses_a_dialog_and_never_echoes(sandbox, tmp_path):
    env = dict(sandbox); env.pop("LIFEGRAPH_PASSPHRASE")
    life(dict(sandbox), "init")
    # stdin is not a terminal here (subprocess pipe), exactly like a chat's `!` shell
    # 1) no dialog available -> refuses clearly, and does not print the passphrase prompt as an echoing one
    r = subprocess.run([os.path.join(BIN, "life"), "unlock"], env=dict(env, PATH="/nonexistent", LIFEGRAPH_PINENTRY=""), capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.returncode != 0 and "real terminal" in r.stderr and "echo" not in r.stderr.lower().replace("never", "")
    # 2) a dialog available -> the secret comes from it (percent-encoding handled), unlock succeeds
    fake = tmp_path / "fake-pinentry"; fake.write_text(FAKE_PINENTRY.replace("%PASS%", PW)); fake.chmod(0o755)
    r = subprocess.run([os.path.join(BIN, "life"), "unlock"], env=dict(env, LIFEGRAPH_PINENTRY=str(fake)), capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and "Unlocked" in r.stdout, r.stderr
    # 3) the wrong secret from the dialog is rejected
    bad = tmp_path / "bad-pinentry"; bad.write_text(FAKE_PINENTRY.replace("%PASS%", "nope nope nope")); bad.chmod(0o755)
    r = subprocess.run([os.path.join(BIN, "life"), "unlock"], env=dict(env, LIFEGRAPH_PINENTRY=str(bad)), capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.returncode != 0 and "Wrong passphrase" in r.stderr

def test_front_end_unlock_contract(sandbox):
    """What the front-end app relies on: passphrase on stdin, JSON out, an app-scoped read token that works and can be revoked."""
    env = dict(sandbox); env.pop("LIFEGRAPH_PASSPHRASE"); life(sandbox, "init")
    run = lambda pw, *a: subprocess.run([os.path.join(BIN, "life"), "unlock", "--stdin", "--json", *a], env=env, input=pw + "\n", capture_output=True, text=True)
    bad = run("nope nope nope"); assert bad.returncode != 0 and json.loads(bad.stdout)["error"] == "bad-passphrase"
    ok = run(PW, "--hours", "8", "--app", "test-front"); j = json.loads(ok.stdout)
    assert j["ok"] and j["token"].startswith("lg_") and j["expires_in"] > 7 * 3600 and PW not in ok.stdout + ok.stderr
    from lifegraph import ipc
    os.environ["LIFEGRAPH_RUNTIME"] = env["LIFEGRAPH_RUNTIME"]
    r = ipc.call({"cmd": "read", "op": "stats", "args": {}, "token": j["token"]}); assert r["ok"]
    assert ipc.call({"cmd": "read", "op": "graph", "args": {}, "token": j["token"]})["ok"]

def test_connect_registers_agents_with_a_level(sandbox, tmp_path):
    """`life connect` against fake agent CLIs: each agent gets its own token; the level says what it may do."""
    env = dict(sandbox); life(env, "init")
    fake = tmp_path / "bin"; fake.mkdir()
    for n in ("claude", "codex"):
        (fake / n).write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/{n}.log"\n'); (fake / n).chmod(0o755)
    env["PATH"] = f"{fake}:{env['PATH']}"; env["OPENCODE_CONFIG"] = str(tmp_path / "oc.json")
    assert "connected as write" in life(env, "connect", "all").stdout
    log = (tmp_path / "codex.log").read_text().splitlines()[-1]; tok = [w.split("=", 1)[1] for w in log.split() if w.startswith("LIFEGRAPH_TOKEN=")][0]
    assert tok.startswith("lg_") and "--read" not in log
    oc = json.loads((tmp_path / "oc.json").read_text())["mcp"]["lifegraph"]; assert oc["environment"]["LIFEGRAPH_TOKEN"] != tok     # one token per agent
    _, out = asyncio.run(mcp(env, "propose", {"ops": [{"op": "fact", "subject": "person:Sam", "text": "hi"}]}, token=tok)); assert out["status"] == "applied"
    life(env, "connect", "codex", "--level", "read")                                                                                 # re-running changes the level
    tok2 = [w.split("=", 1)[1] for w in (tmp_path / "codex.log").read_text().splitlines()[-1].split() if w.startswith("LIFEGRAPH_TOKEN=")][0]
    _, out = asyncio.run(mcp(env, "propose", {"ops": [{"op": "fact", "subject": "person:Sam", "text": "again"}]}, token=tok2)); assert out["status"] == "denied"
    _, out = asyncio.run(mcp(env, "propose", {"ops": [{"op": "fact", "subject": "person:Sam", "text": "again"}]}, token=tok)); assert "error" in json.dumps(out)   # old token replaced
    life(env, "revoke", "agent-claude")

def test_review_page_shows_pending_changes_and_decides(sandbox):
    import urllib.request, urllib.error, re
    env = sandbox; life(env, "init"); m = grant(env, "chat-m", "--level", "manage", "--ask")
    _, held = asyncio.run(mcp(env, "propose", {"ops": [{"op": "event", "title": "Met Alex", "people": ["Alex"]}]}, token=m)); assert held["status"] == "awaiting_approval"
    p = subprocess.Popen([os.path.join(BIN, "life"), "review", "--no-open"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        url = re.search(r"http://\S+/", p.stdout.readline()).group(0)
        page = urllib.request.urlopen(url).read().decode(); assert "Life graph" in page and "Explore" in page
        ch = json.loads(urllib.request.urlopen(url + "api/changes").read())
        assert [c["changeset"] for c in ch["pending"]] == [held["changeset"]] and any(n["name"] == "Met Alex" and n["state"] == "new" for n in ch["pending"][0]["graph"]["nodes"])
        with pytest.raises(urllib.error.HTTPError): urllib.request.urlopen(url.rsplit("/", 2)[0] + "/wrong/api/changes")          # no guessing the address
        req = urllib.request.Request(url + "api/decide", json.dumps({"id": held["changeset"], "action": "approve"}).encode(), {"Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req).read())["ok"]
        assert "Met Alex" in life(env, "search", "Alex").stdout
        req = urllib.request.Request(url + "api/decide", json.dumps({"id": held["changeset"], "action": "approve"}).encode(), {"Content-Type": "text/plain"})
        with pytest.raises(urllib.error.HTTPError): urllib.request.urlopen(req)                                                    # browsers' cross-site posts can't use it
    finally:
        p.terminate(); p.wait(5)


FAKE_LLM = """#!/usr/bin/env python3
import sys, json
prompt = sys.stdin.read()
open(sys.argv[1], "w").write(prompt)
print("Sure! " + json.dumps({"ops": [
  {"op": "event", "title": "Decided to ship the review page", "when": "2026-11-05", "people": ["Sam"], "projects": ["Lifegraph"], "kind": "decision", "paths": ["/work/lifegraph/src/a.py", "/etc/passwd"]},
  {"op": "fact", "subject": "project:Lifegraph", "text": "uses a local daemon"},
  {"op": "forget", "ref": "e:1"}]}))
"""

def write_transcript(path, secret="sk-abcdefghijklmnop1234567890"):
    rows = []
    for i in range(5):
        rows.append({"type": "user", "timestamp": "2026-11-05T10:00:00Z", "cwd": "/work/lifegraph", "message": {"role": "user", "content": f"message {i} about Lifegraph with Sam. my key is {secret}" if i == 0 else f"message {i}"}})
        rows.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": f"reply {i}"}, {"type": "tool_use", "name": "Bash", "input": {"command": "TOOL-NOISE"}},
                                                                                  {"type": "tool_use", "name": "Edit", "input": {"file_path": "/work/lifegraph/src/a.py"}},
                                                                                  {"type": "tool_use", "name": "Read", "input": {"file_path": "/work/lifegraph/README.md"}},
                                                                                  {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/scratch.log"}}]}})
    rows.append({"type": "user", "isMeta": True, "message": {"role": "user", "content": "META-NOISE"}})
    path.write_text("\n".join(json.dumps(r) for r in rows))

def wait_for(fn, secs=8):
    t = time.time()
    while time.time() - t < secs:
        v = fn()
        if v: return v
        time.sleep(0.2)
    return fn()

def test_capture_on_off_proposes_only_and_scrubs(sandbox, tmp_path):
    env = sandbox; life(env, "init"); home = tmp_path / "h"
    settings = home / ".claude" / "settings.json"; settings.parent.mkdir()
    settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}], "SessionEnd": [{"hooks": [{"type": "command", "command": "other-tool"}]}]}}))
    llm = tmp_path / "llm.py"; llm.write_text(FAKE_LLM); llm.chmod(0o755); seen = tmp_path / "prompt.txt"
    tr = tmp_path / "t.jsonl"; write_transcript(tr)
    assert "OFF" not in life(env, "capture", "status").stdout and "off" in life(env, "capture", "status").stdout
    out = life(env, "capture", "on", "--backend", "command", "--command", f"{llm} {seen}").stdout
    assert "ON" in out and "sent to" in out                                                              # says where the transcript goes
    hooks = json.loads(settings.read_text())["hooks"]
    assert hooks["Stop"][0]["hooks"][0]["command"] == "mine" and len(hooks["SessionEnd"]) == 2          # nothing of the user's was touched
    assert (home / ".claude" / "settings.json.lifegraph-backup").exists() and "installed" in life(env, "capture", "status").stdout

    # the hook returns at once; the work happens detached. A tiny session is ignored, a real one is proposed, never applied.
    stdin = json.dumps({"transcript_path": str(tr), "cwd": "/work/lifegraph", "hook_event_name": "SessionEnd"})
    t0 = time.time(); subprocess.run([os.path.join(BIN, "life"), "capture", "hook"], env=env, input=stdin, text=True, check=True); assert time.time() - t0 < 5
    pend = wait_for(lambda: "capture" in life(env, "pending").stdout)
    assert pend, life(env, "capture", "status").stdout
    p = life(env, "pending").stdout
    assert "Decided to ship the review page" in p and "uses a local daemon" in p and "forget" not in p.lower().replace("forgot", "")   # destructive ops are dropped
    assert "Decided" not in life(env, "search", "Decided").stdout                                           # proposed, not applied
    assert "tied to 2 path(s): /work/lifegraph, /work/lifegraph/src/a.py" in p and "located_at→ directory:/work/lifegraph" in p       # the folder and the touched file, nobody typed them
    assert "/etc/passwd" not in p and "scratch.log" not in p                                                                       # an invented path and temp noise are dropped
    prompt = seen.read_text(); assert "/work/lifegraph/src/a.py" in prompt and "/work/lifegraph/README.md" in prompt

    assert "sk-abcdefghijklmnop" not in prompt and "[api key]" in prompt and "TOOL-NOISE" not in prompt and "META-NOISE" not in prompt and "message 3" in prompt
    assert "capture:claude@lifegraph" in p                                                                   # you can see which session it came from
    life(env, "approve", "all"); assert "Decided" in life(env, "search", "Decided").stdout
    proj = life(env, "show", "project:Lifegraph").stdout; assert "located_at" in proj and "directory:/work/lifegraph" in proj          # "where does Lifegraph live?" is now answerable

    # never captures itself, can be skipped per session, and off means off
    seen.unlink(); before = life(env, "pending").stdout
    for extra in ({"LIFEGRAPH_CAPTURE_RUN": "1"}, {"LIFEGRAPH_NO_CAPTURE": "1"}):
        subprocess.run([os.path.join(BIN, "life"), "capture", "hook"], env=dict(env, **extra), input=stdin, text=True, check=True)
    time.sleep(1.5); assert not seen.exists()
    short = tmp_path / "short.jsonl"; short.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}))
    subprocess.run([os.path.join(BIN, "life"), "capture", "hook"], env=env, input=json.dumps({"transcript_path": str(short)}), text=True, check=True); time.sleep(1.5); assert not seen.exists()
    life(env, "capture", "off")
    assert json.loads(settings.read_text())["hooks"]["SessionEnd"] == [{"hooks": [{"type": "command", "command": "other-tool"}]}]      # only our entry removed
    subprocess.run([os.path.join(BIN, "life"), "capture", "hook"], env=env, input=stdin, text=True, check=True); time.sleep(1.5); assert not seen.exists()
    r = life(env, "capture", "run", "--transcript", str(tr), "--dry-run"); assert "Decided to ship" in r.stdout and seen.exists()

def test_slash_command_is_installed_and_served_as_an_mcp_prompt(sandbox, tmp_path):
    env = dict(sandbox); life(env, "init"); fake = tmp_path / "bin"; fake.mkdir()
    for n in ("claude", "codex"):
        (fake / n).write_text("#!/bin/sh\n"); (fake / n).chmod(0o755)
    env["PATH"] = f"{fake}:{env['PATH']}"; env["OPENCODE_CONFIG"] = str(tmp_path / "oc.json")
    life(env, "connect", "all"); h = tmp_path / "h"
    for f in (h / ".claude/commands/lifegraph.md", h / ".codex/prompts/lifegraph.md", h / ".config/opencode/command/lifegraph.md"):
        body = f.read_text(); assert "dry_run=true" in body and "Save these?" in body and "$ARGUMENTS" in body
    async def prompts():
        params = StdioServerParameters(command=os.path.join(BIN, "lifegraph-mcp"), args=[], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize(); names = [p.name for p in (await s.list_prompts()).prompts]
                got = await s.get_prompt("lifegraph", {"focus": "the decisions"})
                return names, got.messages[0].content.text
    names, txt = asyncio.run(prompts()); assert names == ["lifegraph"] and "Focus on: the decisions" in txt and "search" in txt


def test_explorer_search_open_fix_and_undo(sandbox):
    import urllib.request, urllib.parse, re
    env = sandbox; life(env, "init"); w = grant(env, "chat-w", "--level", "write")
    asyncio.run(mcp(env, "propose", {"ops": [{"op": "event", "title": "Debugged the scheduler deadlock", "when": "2026-10-01", "projects": ["Kernel"], "people": ["Sam"]},
                                             {"op": "fact", "subject": "person:Sam", "text": "prefers vegetarian food"}, {"op": "fact", "subject": "person:Sam", "text": "likes tea"}]}, token=w))
    p = subprocess.Popen([os.path.join(BIN, "life"), "ui", "--no-open"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        url = re.search(r"http://\S+/", p.stdout.readline()).group(0)
        get = lambda path: json.loads(urllib.request.urlopen(url + path).read())
        post = lambda path, body: json.loads(urllib.request.urlopen(urllib.request.Request(url + path, json.dumps(body).encode(), {"Content-Type": "application/json"})).read())
        g = get("api/graph"); assert {n["name"] for n in g["nodes"]} >= {"Sam", "Kernel"} and g["links"]
        st = get("api/stats"); assert st["entities"] >= 2 and "search_index" in st                          # what the native window's boot screen reads
        wait_for(lambda: get("api/search?q=" + urllib.parse.quote("schedulr deadlok")))               # typos: found through the meaning index once it has built
        hits = wait_for(lambda: get("api/search?q=" + urllib.parse.quote("schedulr deadlok")))
        assert hits and hits[0]["name"] == "Debugged the scheduler deadlock", hits
        sam = get("api/get?ref=" + urllib.parse.quote("person:Sam")); tea = next(f for f in sam["facts"] if f["text"] == "likes tea")
        assert get("api/get?ref=" + urllib.parse.quote(sam["ref"]))["name"] == "Sam"                    # refs open exactly that entity
        r = post("api/apply", {"ops": [{"op": "forget", "ref": tea["ref"]}]}); assert r["status"] == "applied"
        assert "likes tea" not in life(env, "show", "Sam").stdout
        post("api/apply", {"ops": [{"op": "edit_fact", "ref": next(f for f in get("api/get?ref=" + urllib.parse.quote(sam["ref"]))["facts"] if "vegetarian" in f["text"])["ref"], "text": "is vegan"}]})
        assert "is vegan" in life(env, "show", "Sam").stdout
        post("api/apply", {"ops": [{"op": "fact", "subject": sam["ref"], "text": "added from the UI"}]}); assert "added from the UI" in life(env, "show", "Sam").stdout   # an e: ref targets that entity, not a new one
        assert post("api/decide", {"id": r["changeset"], "action": "undo"})["ok"] and "likes tea" in life(env, "show", "Sam").stdout          # the toast's Undo
        bad = post("api/apply", {"ops": [{"op": "forget", "ref": "f:9999"}]}); assert bad["status"] == "invalid"
    finally:
        p.terminate(); p.wait(5)

def test_semantic_status_and_search_by_meaning(sandbox):
    env = sandbox; life(env, "init"); w = grant(env, "chat-w", "--level", "write")
    asyncio.run(mcp(env, "propose", {"ops": [{"op": "event", "title": "Debugged the scheduler deadlock", "when": "2026-10-01", "projects": ["Kernel"]}]}, token=w))
    st = wait_for(lambda: "waiting: 0" in life(env, "semantic", "status").stdout and life(env, "semantic", "status").stdout)
    assert st and "lite" in st and "indexed: 0" not in st
    _, hits = asyncio.run(mcp(env, "search", {"query": "schedulr deadlok"}, token=w)); hits = hits.get("result", hits)
    assert hits and hits[0]["name"] == "Debugged the scheduler deadlock" and hits[0]["found_by"] == "meaning"           # agents get this automatically
    assert "queued" in life(env, "semantic", "backend", "lite").stdout or "set to" in life(env, "semantic", "backend", "lite").stdout
    assert "Re-indexing" in life(env, "semantic", "reindex").stdout
