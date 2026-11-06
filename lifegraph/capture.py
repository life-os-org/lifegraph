"""Auto-capture: when a Claude Code session ends, read its transcript, ask an LLM what is worth remembering, and PROPOSE it.

Nothing captured is ever applied on its own: it lands in `life review` as a change from the agent named "capture", and capture may
only ADD (events, notes, facts, links), never edit or delete. Turn it on and off with `life capture on|off`.

Privacy: the transcript is sent to whichever LLM backend you choose (the Claude CLI by default, i.e. Anthropic; or a local Ollama
model, which stays on your machine). Obvious secrets are scrubbed first. Set LIFEGRAPH_NO_CAPTURE=1 to skip one session."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

from . import changes, config, crypto, ipc

MAX_CHARS = 60_000
MAX_OPS = 12
MAX_FILES = 12
SECRETS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[private key]"),
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{16,}"), "[api key]"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}"), "[token]"),
    (re.compile(r"\blg_[A-Za-z0-9_\-]{20,}"), "[token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[aws key]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[token]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{20,}"), r"\1 [token]"),
    (re.compile(r"(?i)\b(password|passwd|passphrase|secret|api[_ -]?key|token)\b(\s*[:=]\s*)\S+"), r"\1\2[redacted]"),
]


def token_path() -> Path:
    return crypto.home() / "capture.token"


def log_path() -> Path:
    return crypto.home() / "capture.log"


def log(msg: str):
    with open(log_path(), "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    os.chmod(log_path(), 0o600)


def scrub(text: str) -> str:
    for rx, rep in SECRETS:
        text = rx.sub(rep, text)
    return text


# ---------------------------------------------------------------- transcripts
WRITE_TOOLS = {"Edit": 3, "MultiEdit": 3, "Write": 3, "NotebookEdit": 3, "Read": 1}
NOISE = re.compile(r"(^|/)(node_modules|\.git|\.venv|venv|__pycache__|\.cache|dist|build|\.next|target)(/|$)|^/(tmp|var|proc|sys|dev)/|\.(log|lock|pyc)$")


def project_root(cwd: str) -> str:
    """The git root above cwd if there is one (that is the project), else cwd itself."""
    p = Path(cwd) if cwd else None
    while p and p != p.parent and (p == Path(cwd) or p != Path.home()):       # a dotfiles repo in ~ must not make "home" the project
        if (p / ".git").exists():
            return str(p)
        p = p.parent
    return cwd


def read_claude_transcript(path: str) -> tuple[list[tuple[str, str]], str, dict]:
    """(turns, session_date, context). Turns keep what was said (text only: no tool output or injected system text). Context is where
    the work happened: the working folder and the files the session wrote or read, found from its tool calls, not guessed."""
    turns: list[tuple[str, str]] = []
    first_ts = ""
    cwds: Counter = Counter()
    files: dict[str, list] = {}
    for line in Path(path).read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") not in ("user", "assistant") or e.get("isSidechain") or e.get("isMeta"):
            continue
        first_ts = first_ts or str(e.get("timestamp") or "")[:10]
        if e.get("cwd"):
            cwds[e["cwd"]] += 1
        c = (e.get("message") or {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in WRITE_TOOLS:
                    fp = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path")
                    if isinstance(fp, str) and fp.startswith("/") and not NOISE.search(fp):
                        w = files.setdefault(fp, [0, 0])
                        w[0] += WRITE_TOOLS[b["name"]]
                        w[1] += 1
            c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        c = (c or "").strip()
        if not c or c.startswith(("<command-", "<local-command", "<system-reminder", "Caveat:")) or "<system-reminder>" in c[:40]:
            continue
        turns.append((e["type"], c))
    cwd = cwds.most_common(1)[0][0] if cwds else ""
    ranked = sorted(files, key=lambda f: (-files[f][0], -files[f][1]))[:MAX_FILES]
    return turns, first_ts, {"cwd": cwd, "files": ranked, "written": [f for f in ranked if files[f][0] >= 3]}


def render(turns: list[tuple[str, str]]) -> str:
    out = "\n\n".join(f"{'ME' if r == 'user' else 'ASSISTANT'}: {t}" for r, t in turns)
    return out[-MAX_CHARS:]        # the end of a long session is where decisions land


PROMPT = """You maintain a private personal knowledge graph for the user ("me"). Below is a conversation between the user and an AI assistant.
Extract only what the user would want to remember in a year: decisions, things done or learned, people, projects, plans, durable facts about them.
Skip small talk, how-to chatter, code details, and anything secret. If nothing qualifies, return {{"ops": []}}.

Reply with ONLY a JSON object: {{"ops": [ ... ]}} with at most {max_ops} operations, each one of:
  {{"op":"event","title":"short past-tense summary","when":"YYYY-MM-DD","body":"details worth keeping","kind":"event|decision|milestone|learning|meeting","people":[],"places":[],"projects":[],"topics":[],"paths":[]}}
  {{"op":"fact","subject":"kind:Name","text":"a durable fact"}}
  {{"op":"link","src":"kind:Name","rel":"snake_case_verb","dst":"kind:Name"}}
  {{"op":"note","text":"a reflection said in the user's words","title":"..."}}
Rules: refer to the user as "me". Resolve relative dates against the session date {date}. Use the user's own spelling for names.
REUSE these existing names exactly when they refer to the same thing (kind:name): {known}
Never include passwords, keys or tokens.
{where}
CONVERSATION
{transcript}"""


def build_prompt(transcript: str, date: str, known: list[str], ctx: dict | None = None) -> str:
    ctx = ctx or {}
    root = ctx.get("root")
    where = ""
    if root:
        from .changes import norm_path
        fl = [norm_path(f)[0] for f in ctx.get("files", [])]
        where = (f"The work happened in the project folder {norm_path(root + '/')[0]}/ (the app adds it to each event for you). "
                 "In an event's \"paths\", list ONLY files from this list that the event is really about, at most 8, most important first: "
                 + (", ".join(fl) or "(none)") + "\n")
    return PROMPT.format(max_ops=MAX_OPS, date=date or time.strftime("%Y-%m-%d"), known=", ".join(known[:80]) or "(none yet)", transcript=transcript, where=where)


def attach_paths(ops: list[dict], ctx: dict) -> list[dict]:
    """Make every event/note say where it happened: always the project folder, plus files from the session that the model picked.
    The model can only choose from the files the session really touched, so it can't invent a path."""
    from .changes import norm_path
    root = ctx.get("root")
    if not root:
        return ops
    allowed = {norm_path(f)[0]: f for f in ctx.get("files", [])}
    fallback = [f for f in ctx.get("written", [])][:3]
    out = []
    for op in ops:
        if op["op"] in ("event", "note"):
            keep = []
            for raw in op.get("paths") or []:
                n = norm_path(raw)
                if n and n[0] in allowed and n[1] == "file" and allowed[n[0]] not in keep:
                    keep.append(allowed[n[0]])
            op = {**op, "paths": [root + "/"] + (keep or fallback)[:8]}
        out.append(op)
    return out


# ---------------------------------------------------------------- backends
def _env() -> dict:
    return dict(os.environ, LIFEGRAPH_CAPTURE_RUN="1")      # the hook checks this, so our own headless run never captures itself


def call_claude(prompt: str, model: str) -> str:
    cmd = ["claude", "-p", "--model", model, "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--no-session-persistence", "--disable-slash-commands"]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=240, env=_env())
    if r.returncode:
        raise RuntimeError(f"claude exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout


def call_ollama(prompt: str, model: str) -> str:
    req = urllib.request.Request("http://127.0.0.1:11434/api/generate", json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json"}).encode(),
                                 {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=300).read())["response"]


def call_command(prompt: str, command: str) -> str:
    r = subprocess.run(shlex.split(command), input=prompt, capture_output=True, text=True, timeout=300, env=_env())
    if r.returncode:
        raise RuntimeError(f"{command.split()[0]} exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout


def ask_llm(prompt: str, cfg: dict) -> str:
    b = cfg["backend"]
    if b == "claude":
        return call_claude(prompt, cfg["model"])
    if b == "ollama":
        return call_ollama(prompt, cfg["ollama_model"])
    if b == "command":
        if not cfg["command"]:
            raise RuntimeError("backend 'command' needs --command")
        return call_command(prompt, cfg["command"])
    raise RuntimeError(f"unknown capture backend {b!r}")


def parse_ops(reply: str) -> list[dict]:
    """The first JSON object in the reply, as a list of ops. Only additive ops survive, whatever the model wrote."""
    start = reply.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(reply)):
            ch = reply[i]
            if in_str:
                esc = (ch == "\\" and not esc)
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        d = json.loads(reply[start:i + 1])
                    except ValueError:
                        break
                    ops, _ = changes.normalize([o for o in (d.get("ops") or []) if isinstance(o, dict)][:MAX_OPS] or [{}])
                    return [o for o in ops if o["op"] in changes.ADDITIVE]
        start = reply.find("{", start + 1)
    return []


# ---------------------------------------------------------------- the run
def token() -> str:
    try:
        return token_path().read_text().strip()
    except FileNotFoundError:
        return ""


def run(transcript: str, agent: str = "claude", cwd: str = "", dry_run: bool = False, force: bool = False) -> dict:
    cfg = config.load()["capture"]
    if os.environ.get("LIFEGRAPH_CAPTURE_RUN") or os.environ.get("LIFEGRAPH_NO_CAPTURE"):
        return {"status": "skipped", "why": "capture disabled for this run"}
    if not (cfg["enabled"] or force):
        return {"status": "skipped", "why": "capture is off (life capture on)"}
    turns, date, ctx = read_claude_transcript(transcript)
    folder = ctx["cwd"] or cwd
    ctx["root"] = project_root(folder) if folder else ""
    if sum(1 for r, _ in turns if r == "user") < cfg["min_turns"] and not force:
        return {"status": "skipped", "why": f"fewer than {cfg['min_turns']} user messages"}
    tok = token()
    if not tok and not dry_run:
        return {"status": "error", "why": "no capture token: run `life capture on`"}
    known = []
    if tok:
        r = ipc.call({"cmd": "agent", "op": "names", "args": {"limit": 80}, "token": tok})
        known = r.get("data") or [] if r.get("ok") else []
    reply = ask_llm(build_prompt(scrub(render(turns)), date, known, ctx), cfg)
    ops = attach_paths(parse_ops(reply), ctx)
    if not ops:
        return {"status": "nothing", "why": "nothing worth remembering"}
    if dry_run:
        return {"status": "preview", "ops": ops}
    source = f"capture:{agent}@{os.path.basename(cwd) or '/'}"
    r = ipc.call({"cmd": "agent", "op": "propose", "args": {"ops": ops}, "token": tok, "source": source})
    if not r.get("ok"):
        return {"status": "error", "why": r.get("message") or r.get("error")}
    return {"status": r["data"]["status"], "changeset": r["data"].get("changeset"), "ops": len(ops)}


def run_logged(transcript: str, agent: str, cwd: str, **kw) -> dict:
    try:
        r = run(transcript, agent, cwd, **kw)
    except Exception as e:   # noqa: BLE001  (never let capture trouble reach the agent)
        r = {"status": "error", "why": f"{type(e).__name__}: {str(e)[:160]}"}
    if r["status"] != "skipped":
        log(f"{agent} {os.path.basename(cwd) or '/'}: {r['status']}" + (f" ({r['why']})" if r.get("why") else "") + (f" {r['changeset']}" if r.get("changeset") else ""))
    return r


def hook(stdin_text: str, life_exe: str) -> None:
    """Claude Code SessionEnd hook: returns at once and does the work in a detached process, so exiting a session is never slowed."""
    if os.environ.get("LIFEGRAPH_CAPTURE_RUN") or os.environ.get("LIFEGRAPH_NO_CAPTURE"):
        return
    try:
        d = json.loads(stdin_text or "{}")
    except ValueError:
        return
    path = d.get("transcript_path")
    if not path or not config.load()["capture"]["enabled"]:
        return
    subprocess.Popen([life_exe, "capture", "run", "--transcript", path, "--agent", "claude", "--cwd", d.get("cwd") or ""], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=dict(os.environ, LIFEGRAPH_CAPTURE_HOOK="1"))


# ---------------------------------------------------------------- Claude Code settings
MARK = "capture hook"


def _settings(home: Path) -> Path:
    return home / ".claude/settings.json"


def hook_installed(home: Path | None = None) -> bool:
    try:
        d = json.loads(_settings(home or Path(os.environ.get("HOME") or Path.home())).read_text())
    except (FileNotFoundError, ValueError):
        return False
    return any(MARK in h.get("command", "") for g in d.get("hooks", {}).get("SessionEnd", []) for h in g.get("hooks", []))


def set_claude_hook(life_exe: str, enable: bool, home: Path | None = None) -> bool:
    """Add or remove our SessionEnd hook in ~/.claude/settings.json, leaving everything else (other hooks, plugins, settings) as it was."""
    p = _settings(home or Path(os.environ.get("HOME") or Path.home()))
    try:
        d = json.loads(p.read_text())
    except FileNotFoundError:
        d = {}
    except ValueError:
        raise RuntimeError(f"{p} isn't valid JSON, so I left it alone")
    if enable and p.exists() and not (p.parent / "settings.json.lifegraph-backup").exists():
        (p.parent / "settings.json.lifegraph-backup").write_text(p.read_text())
    groups = d.setdefault("hooks", {}).setdefault("SessionEnd", [])
    for g in groups:
        g["hooks"] = [h for h in g.get("hooks", []) if MARK not in h.get("command", "")]
    groups[:] = [g for g in groups if g.get("hooks")]
    if enable:
        groups.append({"hooks": [{"type": "command", "command": f"{shlex.quote(life_exe)} {MARK}", "timeout": 10}]})
    if not groups:
        d["hooks"].pop("SessionEnd", None)
    if not d["hooks"]:
        d.pop("hooks")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    os.replace(tmp, p)
    return enable


def life_exe() -> str:
    return str(Path(sys.executable).parent / "life")
