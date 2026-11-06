"""The `/lifegraph` command: one instruction, delivered to every agent as an MCP prompt and as a slash-command file.

It works because the agent already has the whole conversation in context: no transcript capture, no extra model, no per-agent plumbing."""
from __future__ import annotations

import os
from pathlib import Path

BODY = """\
Save what is worth remembering from this conversation to my life graph.

1. Look back over this conversation for things I would want to know in a year: decisions made, things built or learned, people
   and projects involved, plans and deadlines. Skip small talk, code-level detail, and anything secret (passwords, keys).
2. For every person, project, course or place you will mention, `search` the lifegraph first and reuse the exact name it already has.
3. Attach WHERE the work lives, without me telling you: put the project's folder and up to ~10 files that mattered (created, edited, or
   central to the discussion; not everything you merely read) in `paths` on each event. Use ~ for my home folder, end folders with "/",
   and only use paths you actually saw or used in this session. Add a `link` from the project to its folder (rel "located_at").
4. Call `propose` with dry_run=true and the operations (events, facts, links). Then show me a SHORT plain summary: what you will add,
   including the paths, and for each name whether it attaches to something that already exists or is new.
5. Ask me "Save these?". Only after I say yes, call `propose` again with the same operations and dry_run=false.
   If it comes back awaiting_approval, tell me to run `life review`.
Nothing found worth saving? Say so and stop.
{focus}"""


def text(focus: str = "") -> str:
    return BODY.format(focus=(f"\nFocus on: {focus}\n" if focus.strip() else ""))


def _targets(home: Path) -> dict[str, tuple[Path, str]]:
    desc = "Save what's worth remembering from this conversation to your life graph"
    return {
        "claude": (home / ".claude/commands/lifegraph.md", f"---\ndescription: {desc}\n---\n" + BODY.format(focus="\n$ARGUMENTS\n")),
        "codex": (home / ".codex/prompts/lifegraph.md", f"---\ndescription: {desc}\n---\n" + BODY.format(focus="\n$ARGUMENTS\n")),
        "opencode": (home / ".config/opencode/command/lifegraph.md", f"---\ndescription: {desc}\n---\n" + BODY.format(focus="\n$ARGUMENTS\n")),
    }


def install(agent: str, home: Path | None = None) -> Path | None:
    t = _targets(home or Path(os.environ.get("HOME") or Path.home())).get(agent)
    if not t:
        return None
    t[0].parent.mkdir(parents=True, exist_ok=True)
    t[0].write_text(t[1])
    return t[0]


def uninstall(agent: str, home: Path | None = None) -> None:
    t = _targets(home or Path(os.environ.get("HOME") or Path.home())).get(agent)
    if t:
        t[0].unlink(missing_ok=True)
