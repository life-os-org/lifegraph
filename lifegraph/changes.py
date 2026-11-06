"""Changesets: the one way anything is added to, edited in or removed from the graph by an agent.

An agent sends a list of operations. The graph answers with exactly what each name will attach to (existing or new),
what will be added and what will go, as a summary AND as a small graph diff. Depending on the agent's grant the change is
then applied at once, held until the owner says yes, or refused. Everything applied is recorded with enough to undo it.

Operations (dicts with an "op" key):
  event      title, when, body, kind, people[], places[], projects[], topics[], entities[], props     something that happened
  note       text, title, when, people[], projects[], topics[]                                           a free-form note
  fact       subject, text, when                                                                         a durable fact about a thing
  link       src, rel, dst, start, end                                                                   a relationship
  edit_fact  ref (f:N), text                                                                             replace a fact's wording
  forget     ref (e:N, v:N, f:N or g:N)                                                                  delete
  merge      from, to                                                                                    fold a duplicate into the survivor
"""
from __future__ import annotations

import json
import time

from .store import UNDO_DAYS, Store, iso

ADDITIVE = {"event", "note", "fact", "link"}
DESTRUCTIVE = {"edit_fact", "forget", "merge"}
ALIASES = {"log_event": "event", "remember": "fact", "add_note": "note", "delete": "forget", "remove": "forget"}
REQUIRED = {"event": ["title"], "note": ["text"], "fact": ["subject", "text"], "link": ["src", "rel", "dst"],
            "edit_fact": ["ref", "text"], "forget": ["ref"], "merge": ["from", "to"]}
MAX_OPS = 50


# ---------------------------------------------------------------- input
def normalize(ops) -> tuple[list[dict], list[str]]:
    if isinstance(ops, dict):
        ops = [ops]
    if not isinstance(ops, list) or not ops:
        return [], ["send a non-empty list of operations"]
    if len(ops) > MAX_OPS:
        return [], [f"at most {MAX_OPS} operations per change; split it"]
    out, errs = [], []
    for i, raw in enumerate(ops, 1):
        if not isinstance(raw, dict):
            errs.append(f"op {i}: must be an object like {{\"op\": \"fact\", ...}}")
            continue
        kind = ALIASES.get(str(raw.get("op") or raw.get("type") or "").strip().lower(), str(raw.get("op") or raw.get("type") or "").strip().lower())
        if kind not in REQUIRED:
            errs.append(f"op {i}: unknown op {kind!r}; use one of {', '.join(sorted(REQUIRED))}")
            continue
        op = {k: v for k, v in raw.items() if k not in ("op", "type")}
        op["op"] = kind
        miss = [f for f in REQUIRED[kind] if not str(op.get(f) or "").strip() and not (kind == "forget" and op.get("refs"))]
        if miss:
            errs.append(f"op {i} ({kind}): missing {', '.join(miss)}")
            continue
        out.append(op)
    # forget with several refs is just several forgets
    flat = []
    for op in out:
        flat.extend([{**op, "ref": r} for r in op.pop("refs")] if op["op"] == "forget" and isinstance(op.get("refs"), list) else [op])
    return flat, errs


def decide(grant: dict, ops: list[dict]) -> tuple[str, str]:
    """('apply' | 'hold' | 'deny', why). The grant sets how much an agent may do on its own."""
    lvl = grant["level"]
    if lvl == "read":
        return "deny", "This agent is read-only. The owner can raise it with `life connect <agent> --level write` (add) or `--level manage` (also edit/delete)."
    kinds = {o["op"] for o in ops}
    if kinds & DESTRUCTIVE:
        if lvl != "manage":
            return "deny", ("This agent may add to the graph but not edit, delete or merge. "
                            "The owner can allow that with `life connect <agent> --level manage`.")
        if "merge" in kinds or not grant["auto_destructive"]:
            return "hold", "Edits, deletes and merges wait for the owner's yes."
        return "apply", ""
    if grant["ask_adds"]:
        return "hold", "This agent's changes wait for the owner's yes."
    return "apply", ""


def _lists(op: dict) -> list:
    out = []
    for key, kind in (("people", "person"), ("places", "place"), ("projects", "project"), ("topics", "topic")):
        out += [{"kind": kind, "name": n} for n in (op.get(key) or []) if str(n).strip()]
    return out + list(op.get("entities") or [])


def entity_refs(op: dict) -> list:
    k = op["op"]
    if k in ("event", "note"):
        return _lists(op)
    if k == "fact":
        return [op["subject"]]
    if k == "link":
        return [op["src"], op["dst"]]
    if k == "merge":
        return [op["from"], op["to"]]
    return []


# ---------------------------------------------------------------- running
def _maxes(st: Store) -> dict:
    return {t: (st.con.execute(f"SELECT max(id) FROM {tbl}").fetchone()[0] or 0)
            for t, tbl in (("e", "entities"), ("v", "events"), ("f", "facts"), ("g", "edges"))}


def _new_ids(st: Store, before: dict) -> dict:
    return {t: [r[0] for r in st.con.execute(f"SELECT id FROM {tbl} WHERE id>? ORDER BY id", (before[t],))]
            for t, tbl in (("e", "entities"), ("v", "events"), ("f", "facts"), ("g", "edges"))}


def _label(st: Store, eid: int) -> str:
    n = st._name(eid)
    return f"{n['kind']}:{n['name']}"


def execute(st: Store, ops: list[dict], source: str, ts: float) -> dict:
    """Run the ops inside the caller's transaction (the caller commits or rolls back). Never writes if a name is ambiguous
    or a ref is wrong: those come back in `errors` before anything runs."""
    errors, targets, seen = [], [], set()
    for i, op in enumerate(ops, 1):
        for r in entity_refs(op):
            p = st.peek(r)
            if p["status"] == "ambiguous":
                errors.append(f"op {i}: {p['error']}")
            elif p["as"] not in seen:
                seen.add(p["as"])
                targets.append(p)
        if op["op"] in ("forget", "edit_fact"):
            try:
                k, n = st._parse_ref(op["ref"])
                if op["op"] == "edit_fact" and k != "f":
                    errors.append(f"op {i}: edit_fact needs a fact ref like f:3")
                else:
                    snap = st._snapshot(k, n)
                    if snap is None:
                        errors.append(f"op {i}: nothing at {op['ref']} (already gone?)")
                    elif op["op"] == "edit_fact" and snap["row"][4]:
                        errors.append(f"op {i}: {op['ref']} was already replaced by f:{snap['row'][4]}; edit that one")
            except ValueError as e:
                errors.append(f"op {i}: {e}")
    if errors:
        return {"errors": errors, "targets": targets}

    before = _maxes(st)
    lines, deleted, superseded = [], [], []
    touched: set[int] = set()
    notes_add: dict[str, list[str]] = {}
    notes_del: dict[str, list[str]] = {}
    removed_nodes: dict[str, dict] = {}
    removed_links: list[dict] = []
    merged = False
    created_events: list[int] = []

    def note(d, eid, text):
        d.setdefault(f"e:{eid}", []).append(text)

    for i, op in enumerate(ops, 1):
        k = op["op"]
        try:
            if k in ("event", "note"):
                ents = _lists(op)
                if k == "event":
                    r = st.log_event(op["title"], op.get("when"), op.get("body", ""), op.get("kind", "event"), ents, op.get("props"),
                                     source, op.get("dedupe"), ts=None if op.get("when") else ts)
                    what = f"event “{op['title']}”"
                else:
                    r = st.log_event(op.get("title") or str(op["text"])[:80], op.get("when"), op["text"], "note", ents, None, source, op.get("dedupe"),
                                     ts=None if op.get("when") else ts)
                    what = f"note “{(op.get('title') or str(op['text']))[:60]}”"
                if r.get("duplicate"):
                    lines.append(f"Skip {what}: already in the graph")
                else:
                    created_events.append(r["event_id"])
                    with_ = ", ".join(r["entities"])
                    lines.append(f"Add {what} on {r['when']}" + (f" involving {with_}" if with_ else ""))
            elif k == "fact":
                r = st.remember(op["subject"], op["text"], op.get("when"), source)
                if r.get("duplicate"):
                    lines.append(f"Skip fact “{op['text'][:60]}”: already known")
                else:
                    eid = st.con.execute("SELECT entity_id FROM facts WHERE id=?", (r["fact_id"],)).fetchone()[0]
                    note(notes_add, eid, op["text"])
                    touched.add(eid)
                    lines.append(f"Add fact to {_label(st, eid)}: “{op['text'][:100]}”")
            elif k == "link":
                r = st.link(op["src"], op["rel"], op["dst"], op.get("start"), op.get("end"), None, source)
                if r.get("duplicate"):
                    lines.append("Skip link: already in the graph")
                else:
                    a, rel, b = st.con.execute("SELECT src,rel,dst FROM edges WHERE id=?", (r["edge_id"],)).fetchone()
                    lines.append(f"Add link {_label(st, a)} —{rel}→ {_label(st, b)}")
            elif k == "edit_fact":
                fid = int(op["ref"][2:])
                eid, old = st.con.execute("SELECT entity_id,text FROM facts WHERE id=?", (fid,)).fetchone()
                new = str(op["text"]).strip()
                if new == old:
                    lines.append(f"Skip edit of {op['ref']}: same wording")
                else:
                    nid = st.con.execute("INSERT INTO facts(entity_id,text,ts,source) VALUES(?,?,?,?)", (eid, new, time.time(), source)).lastrowid
                    st.con.execute("UPDATE facts SET superseded_by=? WHERE id=?", (nid, fid))
                    st._refresh_entity_fts(eid)
                    st.audit(source, "edit_fact", f"f:{fid} -> f:{nid}")
                    superseded.append([fid, nid])
                    note(notes_del, eid, old); note(notes_add, eid, new)
                    touched.add(eid)
                    lines.append(f"Change a fact on {_label(st, eid)}: “{old[:70]}” → “{new[:70]}”")
            elif k == "forget":
                kind, n = st._parse_ref(op["ref"])
                snap = st._snapshot(kind, n)
                lines.append(_forget_line(st, snap, removed_nodes, removed_links, notes_del, touched))
                st.delete_snapshot(snap)
                st.audit(source, "forget", f"{kind}:{n}")
                deleted.append(snap)
            elif k == "merge":
                a, b = st.peek(op["from"]), st.peek(op["to"])
                st.merge(op["from"], op["to"], source)
                merged = True
                removed_nodes[a["ref"] or "m"] = {"id": a["ref"] or "m", "kind": a["as"].split(":")[0], "name": a["as"].split(":", 1)[1], "state": "removed"}
                if b["ref"]:
                    touched.add(int(b["ref"][2:]))
                    removed_links.append({"s": a["ref"], "t": b["ref"], "rel": "merged into", "state": "removed"})
                lines.append(f"Merge {a['as']} into {b['as']} (everything attached moves; the old name keeps resolving)")
        except (ValueError, KeyError) as e:
            errors.append(f"op {i} ({k}): {e}")
            return {"errors": errors, "targets": targets}

    created = _new_ids(st, before)
    effects = {"created": created, "deleted": deleted, "superseded": superseded, "merged": merged}
    return {"errors": [], "lines": lines, "targets": targets, "effects": effects,
            "preview": _preview(st, lines, targets, created, created_events, touched, notes_add, notes_del, removed_nodes, removed_links, deleted)}


def _forget_line(st, snap, removed_nodes, removed_links, notes_del, touched) -> str:
    k = snap["kind"]
    if k == "f":
        eid = snap["row"][0]
        touched.add(eid)
        notes_del.setdefault(f"e:{eid}", []).append(snap["row"][1])
        return f"Delete {snap['label']}"
    if k == "g":
        a, rel, b = snap["row"][0], snap["row"][1], snap["row"][2]
        touched.update((a, b))
        removed_links.append({"s": f"e:{a}", "t": f"e:{b}", "rel": rel, "state": "removed"})
        return f"Delete {snap['label']}"
    if k == "v":
        removed_nodes[f"v:{snap['id']}"] = {"id": f"v:{snap['id']}", "kind": "event", "name": snap["row"][1], "state": "removed"}
        for eid, _ in snap["links"]:
            touched.add(eid)
            removed_links.append({"s": f"v:{snap['id']}", "t": f"e:{eid}", "rel": "involves", "state": "removed"})
        return f"Delete {snap['label']}"
    kind, name = snap["row"][0], snap["row"][1]
    removed_nodes[f"e:{snap['id']}"] = {"id": f"e:{snap['id']}", "kind": kind, "name": name, "state": "removed"}
    for g in snap["edges"]:
        other = g[3] if g[1] == snap["id"] else g[1]
        touched.add(other)
        removed_links.append({"s": f"e:{g[1]}", "t": f"e:{g[3]}", "rel": g[2], "state": "removed"})
    extra = f" (with {len(snap['facts'])} facts, {len(snap['edges'])} links, {len(snap['event_links'])} event mentions)"
    orphans = snap["events_left_without_entities"]
    if orphans:
        extra += f"; {len(orphans)} event(s) will mention nobody: " + ", ".join(f"{o['ref']} “{o['title'][:40]}”" for o in orphans[:5])
    return f"Delete {kind}:{name}{extra}"


def _preview(st, lines, targets, created, created_events, touched, notes_add, notes_del, removed_nodes, removed_links, deleted) -> dict:
    nodes: dict[str, dict] = {}
    new_e = set(created["e"])
    ent_ids = set(touched) | new_e
    for t in targets:                      # every name an op mentioned, even if only used by an event
        if t["ref"]:
            ent_ids.add(int(t["ref"][2:]))
    for eid in ent_ids:
        r = st.con.execute("SELECT kind,name FROM entities WHERE id=?", (eid,)).fetchone()
        if r:
            nodes[f"e:{eid}"] = {"id": f"e:{eid}", "kind": r[0], "name": r[1], "state": "new" if eid in new_e else "existing"}
    links = []
    for vid in created["v"]:
        r = st.con.execute("SELECT title,kind FROM events WHERE id=?", (vid,)).fetchone()
        if not r:
            continue
        nodes[f"v:{vid}"] = {"id": f"v:{vid}", "kind": "event", "name": r[0], "state": "new"}
        for (eid,) in st.con.execute("SELECT entity_id FROM event_entities WHERE event_id=?", (vid,)):
            nodes.setdefault(f"e:{eid}", {"id": f"e:{eid}", "kind": st._name(eid)["kind"], "name": st._name(eid)["name"], "state": "existing"})
            links.append({"s": f"v:{vid}", "t": f"e:{eid}", "rel": "involves", "state": "new"})
    for gid in created["g"]:
        r = st.con.execute("SELECT src,rel,dst FROM edges WHERE id=?", (gid,)).fetchone()
        if r:
            for eid in (r[0], r[2]):
                nodes.setdefault(f"e:{eid}", {"id": f"e:{eid}", "kind": st._name(eid)["kind"], "name": st._name(eid)["name"], "state": "existing"})
            links.append({"s": f"e:{r[0]}", "t": f"e:{r[2]}", "rel": r[1], "state": "new"})
    for key, n in removed_nodes.items():
        nodes[key] = n
    links += removed_links
    nodes = {k: v for k, v in nodes.items() if k}
    for key, texts in notes_add.items():
        if key in nodes:
            nodes[key]["adds"] = texts
    for key, texts in notes_del.items():
        if key in nodes:
            nodes[key]["removes"] = texts
    links = [l for l in links if l["s"] in nodes and l["t"] in nodes]
    counts = {"new_entities": len(created["e"]), "new_events": len(created["v"]), "new_facts": len(created["f"]), "new_links": len(created["g"]),
              "removed": len(deleted)}
    return {"summary": lines, "targets": targets, "counts": counts, "graph": {"nodes": list(nodes.values()), "links": links}}


# ---------------------------------------------------------------- lifecycle
def _row_to_summary(r) -> dict:
    cid, ts, agent, source, status, preview, note = r
    p = json.loads(preview)
    return {"changeset": f"c:{cid}", "when": iso(ts), "agent": agent, "source": source, "status": status, "summary": p.get("summary", []),
            "counts": p.get("counts", {}), **({"note": note} if note else {})}


def submit(st: Store, ops, grant: dict, source: str = "", ts: float | None = None, dry_run: bool = False) -> dict:
    ops, errs = normalize(ops)
    if errs:
        return {"status": "invalid", "errors": errs, "message": "Nothing was changed."}
    decision, why = decide(grant, ops)
    if decision == "deny":
        return {"status": "denied", "message": why}
    ts = ts or time.time()
    source = source or grant["name"]
    with st.tx():
        res = execute(st, ops, source, ts)
        if res["errors"]:
            st.con.rollback()
            return {"status": "invalid", "errors": res["errors"], "message": "Nothing was changed. Fix these and send it again.",
                    "targets": res["targets"]}
        eff = res["effects"]
        empty = not (any(eff["created"].values()) or eff["deleted"] or eff["superseded"] or eff["merged"])
        view = {"summary": res["lines"], "targets": res["targets"], "counts": res["preview"]["counts"]}
        if dry_run or empty or decision == "hold":
            st.con.rollback()
            if empty:
                return {"status": "unchanged", **view, "message": "Everything in this change is already in the graph."}
            if dry_run:
                return {"status": "preview", "would": "apply now" if decision == "apply" else "wait for the owner's approval", **view}
            cid = st.con.execute("INSERT INTO changesets(ts,agent,source,status,ops,preview) VALUES(?,?,?,?,?,?)",
                                 (ts, grant["name"], source, "pending", json.dumps(ops), json.dumps(res["preview"]))).lastrowid
            st.audit(grant["name"], "propose", f"c:{cid}")
            st.con.commit()
            return {"status": "awaiting_approval", "changeset": f"c:{cid}", **view,
                    "next": "Show the user this summary and tell them to run `life review` (or `life approve c:%d`). "
                            "Check the outcome later with get('c:%d'); do not re-send it." % (cid, cid)}
        cid = st.con.execute("INSERT INTO changesets(ts,agent,source,status,ops,preview,effects,decided_at) VALUES(?,?,?,?,?,?,?,?)",
                             (ts, grant["name"], source, "applied", json.dumps(ops), json.dumps(res["preview"]), json.dumps(eff), time.time())).lastrowid
        st.audit(grant["name"], "apply", f"c:{cid}")
        st.con.commit()
    return {"status": "applied", "changeset": f"c:{cid}", **view, "undo": f"undo('c:{cid}') reverses this for {UNDO_DAYS} days"}


def _cid(ref) -> int:
    s = str(ref).strip()
    s = s[2:] if s.startswith("c:") else s
    if not s.isdigit():
        raise ValueError("changeset ids look like c:12")
    return int(s)


def approve(st: Store, ref, who: str = "owner") -> dict:
    cid = _cid(ref)
    with st.tx():
        r = st.con.execute("SELECT ts,source,status,ops FROM changesets WHERE id=?", (cid,)).fetchone()
        if not r:
            raise ValueError(f"no change c:{cid}")
        if r[2] != "pending":
            raise ValueError(f"c:{cid} is {r[2]}, not waiting")
        res = execute(st, json.loads(r[3]), r[1], r[0])
        if res["errors"]:
            st.con.rollback()
            st.con.execute("UPDATE changesets SET note=? WHERE id=?", ("couldn't apply: " + "; ".join(res["errors"])[:300], cid))
            st.con.commit()
            return {"status": "invalid", "changeset": f"c:{cid}", "errors": res["errors"],
                    "message": "The graph changed since this was proposed and it no longer applies. Reject it and ask the agent to retry."}
        st.con.execute("UPDATE changesets SET status='applied', effects=?, preview=?, decided_at=?, note=NULL WHERE id=?",
                       (json.dumps(res["effects"]), json.dumps(res["preview"]), time.time(), cid))
        st.audit(who, "approve", f"c:{cid}")
        st.con.commit()
    return {"status": "applied", "changeset": f"c:{cid}", "summary": res["lines"]}


def reject(st: Store, ref, who: str = "owner", status: str = "rejected") -> dict:
    cid = _cid(ref)
    with st.lock:
        n = st.con.execute("UPDATE changesets SET status=?, decided_at=? WHERE id=? AND status='pending'", (status, time.time(), cid)).rowcount
        if not n:
            raise ValueError(f"c:{cid} isn't waiting for a decision")
        st.audit(who, status, f"c:{cid}")
        st.con.commit()
    return {"status": status, "changeset": f"c:{cid}"}


def undo(st: Store, ref, grant: dict | None = None, who: str = "owner") -> dict:
    """Reverse an applied change (or withdraw a pending one). Agents can undo their own; the owner can undo any."""
    cid = _cid(ref)
    with st.lock:
        r = st.con.execute("SELECT agent,status,effects FROM changesets WHERE id=?", (cid,)).fetchone()
    if not r:
        raise ValueError(f"no change c:{cid}")
    if grant and grant["level"] != "manage" and r[0] != grant["name"]:
        raise ValueError("an agent can only undo its own changes")
    if grant and grant["level"] == "read":
        raise ValueError("this agent is read-only")
    if r[1] == "pending":
        return reject(st, cid, who, "withdrawn")
    if r[1] != "applied":
        raise ValueError(f"c:{cid} is {r[1]}; nothing to undo")
    if not r[2]:
        raise ValueError(f"c:{cid} is older than {UNDO_DAYS} days (or was purged), so it can no longer be undone")
    eff = json.loads(r[2])
    if eff.get("merged"):
        raise ValueError("merges can't be undone automatically")
    kept = []
    with st.tx():
        for old, new in eff["superseded"]:
            snap = st._snapshot("f", new)
            if snap:
                st.delete_snapshot(snap)
            st.con.execute("UPDATE facts SET superseded_by=NULL WHERE id=?", (old,))
            row = st.con.execute("SELECT entity_id FROM facts WHERE id=?", (old,)).fetchone()
            if row:
                st._refresh_entity_fts(row[0])
        for kind in ("g", "f", "v"):
            for i in eff["created"][kind]:
                snap = st._snapshot(kind, i)
                if snap:
                    st.delete_snapshot(snap)
        for i in eff["created"]["e"]:
            used = st.con.execute("SELECT (SELECT count(*) FROM facts WHERE entity_id=?)+(SELECT count(*) FROM edges WHERE src=? OR dst=?)"
                                  "+(SELECT count(*) FROM event_entities WHERE entity_id=?)", (i, i, i, i)).fetchone()[0]
            snap = st._snapshot("e", i)
            if not snap:
                continue
            if used:
                kept.append(f"e:{i}")
            else:
                st.delete_snapshot(snap)
        skipped = 0
        order = {"e": 0, "v": 1, "f": 2, "g": 3}
        for snap in sorted(eff["deleted"], key=lambda s: order[s["kind"]]):
            skipped += st.restore_snapshot(snap)
        st.con.execute("UPDATE changesets SET status='undone', decided_at=?, effects=NULL WHERE id=?", (time.time(), cid))
        st.audit(who, "undo", f"c:{cid}")
        st.con.commit()
    out = {"status": "undone", "changeset": f"c:{cid}", "restored": len(eff["deleted"])}
    if kept:
        out["kept"] = f"{', '.join(kept)} still used by other things, so they stay"
    if skipped:
        out["links_skipped"] = skipped
    return out


def show(st: Store, ref, grant: dict | None = None, full: bool = False) -> dict | None:
    cid = _cid(ref)
    with st.lock:
        r = st.con.execute("SELECT id,ts,agent,source,status,preview,note FROM changesets WHERE id=?", (cid,)).fetchone()
    if not r or (grant and grant["level"] != "manage" and r[2] != grant["name"]):
        return None
    out = _row_to_summary((r[0], r[1], r[2], r[3], r[4], r[5], r[6]))
    if full:
        out["graph"] = json.loads(r[5]).get("graph")
        out["targets"] = json.loads(r[5]).get("targets")
    return out


def listing(st: Store, status: str | None = None, limit: int = 30, full: bool = False) -> list[dict]:
    q = "SELECT id,ts,agent,source,status,preview,note FROM changesets"
    args: list = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with st.lock:
        rows = st.con.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = _row_to_summary(r)
        if full:
            p = json.loads(r[5])
            d["graph"], d["targets"] = p.get("graph"), p.get("targets")
        out.append(d)
    return out


def awaiting(st: Store) -> int:
    with st.lock:
        return st.con.execute("SELECT count(*) FROM changesets WHERE status='pending'").fetchone()[0]


def purge_undo(st: Store) -> int:
    """Make everything forgotten really gone: drops the undo data kept for recent changes."""
    with st.lock:
        n = st.con.execute("UPDATE changesets SET effects=NULL WHERE effects IS NOT NULL").rowcount
        st.audit("owner", "purge_undo", str(n))
        st.con.commit()
        return n
