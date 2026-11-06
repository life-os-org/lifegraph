"""MCP server: how a chat uses the user's life graph. Four tools, the same for every agent.

  search(query)       find things (or, with no query, list recent events)       -> refs
  get(ref)            one entity / event / fact / link / pending change          -> details and refs
  propose(ops)        add, edit, delete or merge: shows exactly what it attaches to, and what happens next
  undo(changeset)     reverse a change (or withdraw one still waiting)

What an agent may do on its own is decided by the grant its token carries (read / write / manage), not by which tools it
sees. The calling model does the understanding; this server never calls an LLM. If the graph is locked or the daemon is
down, `propose` falls back to a sealed mailbox that is filed at the next unlock.
"""
from __future__ import annotations

import argparse
import os
import time

from mcp.server.mcpserver import MCPServer

from . import commands, crypto, inbox, ipc

INSTRUCTIONS = """\
This is the user's private, encrypted life graph: what they did, who was involved, what they decided and learned, and how
projects, people and places relate. Use it to record things worth remembering in a year (not every message), and to look
things up before you write so names connect.

How to use it
- search(query) and get(ref) read. Every result carries a `ref` (e:12 entity, v:7 event, f:3 fact, g:5 link, c:2 change).
- propose(ops) writes. Look before you write: search for the person/project first and reuse the exact name you find.
  Refer to the user as "me". Resolve relative dates ("yesterday") to ISO dates yourself. Never store secrets.
- The reply to propose tells you, per name, whether it attached to something that already exists or created something new,
  and lists every change in plain words. If you are unsure what a name will attach to, call propose with dry_run=true first.
- status "applied": done, and you can undo it. "awaiting_approval": the user must say yes; show them the summary and tell them
  to run `life review`, do not resend it, and check get("c:N") later. "invalid": nothing was changed; fix what the errors say.
  "denied": you are not allowed to do that; tell the user what level they would need to grant.
- To delete or correct something, find its ref with search/get, then propose a forget or edit_fact op on that exact ref.
  Prefer the narrowest ref (one fact or link, not a whole person). A misspelled duplicate is a merge, not a delete.
- Say where work lives: on events and notes about code or files, pass `paths` (the project folder ending in "/", plus the few files
  that mattered) and link the project to its folder with rel "located_at". Only paths you actually saw or used. Later, get("project:X")
  tells you where it lives.
- Show the user what you are about to change in a sentence or two when it is destructive.
"""


def source_label() -> str:
    return f"{os.environ.get('LIFEGRAPH_SOURCE', 'mcp')}@{os.path.basename(os.getcwd()) or '/'}"


def token() -> str:
    return os.environ.get("LIFEGRAPH_TOKEN") or os.environ.get("LIFEGRAPH_READ_TOKEN", "")


def ask(op: str, **args):
    if not token():
        return {"error": "This agent isn't connected to the life graph yet. The user can run `life connect <agent>` once."}
    r = ipc.call({"cmd": "agent", "op": op, "args": args, "token": token(), "source": source_label()})
    return r["data"] if r.get("ok") else {"error": r.get("message") or r.get("error"), "code": r.get("error")}


def queue(ops: list) -> dict:
    inbox.append_sealed({"op": "changeset", "args": {"ops": ops, "token": token()}, "ts": time.time(), "source": source_label()})
    return {"status": "queued", "message": "The graph is locked right now, so this was sealed and queued. It will be checked against this agent's "
                                           "permissions and filed when the user next unlocks; the outcome is not known yet."}


def build() -> MCPServer:
    mcp = MCPServer("lifegraph", instructions=INSTRUCTIONS)

    @mcp.tool()
    def search(query: str = "", about: str | None = None, since: str | None = None, until: str | None = None, limit: int = 10) -> list | dict:
        """Find things in the graph. With `query`: full-text search over events, people, projects and facts. Without it: recent events,
        newest first, optionally between ISO dates `since`/`until` or only those involving `about` (an entity name or ref).
        Every hit has a `ref` you can pass to get() or to a forget/edit_fact op."""
        return ask("search", query=query, about=about, since=since, until=until, limit=limit)

    @mcp.tool()
    def get(ref: str, depth: int = 0) -> dict:
        """Everything about one thing. `ref` is e:12 / v:7 / f:3 / g:5 / c:2, or a name ("person:Sam", "Thesis"). For an entity you get
        facts, relations and recent events, each with its own ref; depth 1-2 adds what is connected to it. A c:N ref shows the state
        of a change you proposed (applied, awaiting_approval, rejected, undone)."""
        return ask("get", ref=ref, depth=depth)

    @mcp.tool()
    def propose(ops: list[dict], dry_run: bool = False) -> dict:
        """Change the graph. `ops` is a list (max 50) applied together, all or nothing. Each op is an object with an "op" key:
          {"op":"event", "title":"Finished the OS lab", "when":"2026-10-14T17:30", "body":"...", "kind":"event|decision|milestone|learning|meeting",
             "people":["Sam"], "places":[], "projects":["OSLab"], "topics":["Algorithms"], "entities":[{"kind":"org","name":"Acme University","role":"at"}],
             "paths":["~/projects/oslab/", "~/projects/oslab/sched.c"]}      (paths: where the work lives; folders end with "/", home is ~)
          {"op":"note", "text":"...", "title":"...", "when":"...", "people":[], "projects":[], "topics":[]}
          {"op":"fact", "subject":"person:Sam", "text":"is in my Algorithms class", "when":"..."}
          {"op":"link", "src":"project:OSLab", "rel":"part_of", "dst":"course:Algorithms", "start":"...", "end":"..."}
          {"op":"edit_fact", "ref":"f:3", "text":"new wording"}
          {"op":"forget", "ref":"f:3"}            (also e:12 entity, v:7 event, g:5 link; use the narrowest ref)
          {"op":"merge", "from":"person:Sammy", "to":"person:Sam"}
        Names are "kind:name" or a bare name; "me" is the user. The reply says, per name, whether it matched something existing
        or is new, lists each change in words, and gives a `status`: applied | awaiting_approval | invalid | denied | preview.
        dry_run=true shows all of that without changing anything."""
        r = ask("propose", ops=ops, dry_run=dry_run)
        if not dry_run and isinstance(r, dict) and r.get("code") in ("locked", "daemon-down"):
            return queue(ops) if token() else r
        return r

    @mcp.tool()
    def undo(changeset: str) -> dict:
        """Reverse a change you applied (c:N from propose), including restoring what it deleted, or withdraw one still awaiting approval.
        Possible for 7 days. Merges can't be undone."""
        return ask("undo", changeset=changeset)

    @mcp.prompt(name="lifegraph", description="Save what's worth remembering from this conversation to the user's life graph")
    def lifegraph(focus: str = "") -> str:
        return commands.text(focus)

    return mcp


def main():
    p = argparse.ArgumentParser(description="life graph MCP server (stdio)")
    p.add_argument("--read", action="store_true", help=argparse.SUPPRESS)      # old registrations; permissions now come from the token
    p.add_argument("--forget", action="store_true", help=argparse.SUPPRESS)
    p.parse_args()
    if not crypto.exists():
        raise SystemExit("life graph not initialised: run `life init` first")
    build().run("stdio")


if __name__ == "__main__":
    main()
