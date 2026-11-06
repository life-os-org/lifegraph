"""The encrypted life graph: entities, edges, events and facts in one SQLCipher file.

Everything is a few small tables plus a full-text index, so it stays one file you can back up. The caller (a chat's model)
does the understanding and sends structured arguments; this module stores them with provenance and never needs an LLM.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

import sqlcipher3

from . import crypto

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, norm TEXT NOT NULL,
  props TEXT NOT NULL DEFAULT '{}', created_at REAL, updated_at REAL, source TEXT,
  UNIQUE(kind, norm));
CREATE TABLE IF NOT EXISTS aliases(
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, alias TEXT NOT NULL, norm TEXT NOT NULL,
  UNIQUE(entity_id, norm));
CREATE INDEX IF NOT EXISTS aliases_norm ON aliases(norm);
CREATE TABLE IF NOT EXISTS merged_names(
  kind TEXT NOT NULL, norm TEXT NOT NULL, entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  PRIMARY KEY(kind, norm));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, title TEXT NOT NULL, body TEXT, kind TEXT NOT NULL DEFAULT 'event',
  props TEXT NOT NULL DEFAULT '{}', created_at REAL, source TEXT, dedupe TEXT UNIQUE);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS event_entities(
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, role TEXT NOT NULL DEFAULT 'involves',
  PRIMARY KEY(event_id, entity_id, role));
CREATE INDEX IF NOT EXISTS ee_entity ON event_entities(entity_id);
CREATE TABLE IF NOT EXISTS edges(
  id INTEGER PRIMARY KEY, src INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, rel TEXT NOT NULL,
  dst INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, props TEXT NOT NULL DEFAULT '{}',
  start_ts REAL, end_ts REAL, created_at REAL, source TEXT);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src); CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, text TEXT NOT NULL,
  ts REAL NOT NULL, source TEXT, superseded_by INTEGER);
CREATE TABLE IF NOT EXISTS grants(
  name TEXT PRIMARY KEY, token_hash TEXT NOT NULL, created_at REAL, expires_at REAL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS changesets(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, agent TEXT NOT NULL, source TEXT, status TEXT NOT NULL,
  ops TEXT NOT NULL, preview TEXT NOT NULL, effects TEXT, decided_at REAL, note TEXT);
CREATE INDEX IF NOT EXISTS cs_status ON changesets(status);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT, action TEXT NOT NULL, ref TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(kind, name, body, ref UNINDEXED, tokenize='porter unicode61');
"""

UNDO_DAYS = 7           # what a change deleted stays restorable this long (changeset effects), then is really gone
PENDING_DAYS = 14       # a change nobody reviewed expires
REL_RE = re.compile(r"[^a-z0-9_]+")
# however a chat refers to the owner, it is always the same single entity: person:Me
SELF_WORDS = {"me", "i", "myself", "self", "the user", "user", "my"}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def norm_rel(r: str) -> str:
    return REL_RE.sub("_", norm(r)).strip("_") or "related_to"


def parse_ts(v, default=None) -> float:
    """Accepts epoch seconds, an ISO date/time string (naive = local time), or None (= now)."""
    if v is None or v == "":
        return default if default is not None else time.time()
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"can't read date {v!r}; use ISO like 2026-10-14 or 2026-10-14T17:30") from e
    return d.timestamp()


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else None


class Store:
    def __init__(self, keys: crypto.Keys, path: Path | None = None):
        self.path = Path(path or crypto.home() / "graph.db")
        self.lock = threading.RLock()
        self._tx = 0            # >0 while a changeset is running: writes stay uncommitted so the whole thing can roll back
        self.con = sqlcipher3.connect(str(self.path), check_same_thread=False)
        self.con.execute(f"PRAGMA key=\"x'{keys.db_key_hex}'\"")
        try:
            self.con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlcipher3.DatabaseError as e:
            raise PermissionError("can't open the database with this key") from e
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript(SCHEMA)
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(grants)")]
        if "level" not in cols:      # older stores: read tokens stay read-only
            self.con.execute("ALTER TABLE grants ADD COLUMN level TEXT NOT NULL DEFAULT 'read'")
            self.con.execute("ALTER TABLE grants ADD COLUMN ask_adds INTEGER NOT NULL DEFAULT 0")
            self.con.execute("ALTER TABLE grants ADD COLUMN auto_destructive INTEGER NOT NULL DEFAULT 0")
        self.expire_old()
        self.con.commit()
        if self.path.exists():
            self.path.chmod(0o600)

    def _commit(self):
        if not self._tx:
            self.con.commit()

    def expire_old(self):
        now = time.time()
        self.con.execute("UPDATE changesets SET effects=NULL WHERE effects IS NOT NULL AND status IN ('applied','undone') AND ts<?", (now - UNDO_DAYS * 86400,))
        self.con.execute("UPDATE changesets SET status='expired', decided_at=? WHERE status='pending' AND ts<?", (now, now - PENDING_DAYS * 86400))

    @contextmanager
    def tx(self):
        """Run several writes as one unit: nothing is committed until the caller commits; any exception rolls everything back."""
        with self.lock:
            self._tx += 1
            try:
                yield
            except BaseException:
                self.con.rollback()
                raise
            finally:
                self._tx -= 1

    def close(self):
        with self.lock:
            self.con.close()

    # ------------------------------------------------------------------ helpers
    def _fts_set(self, ref: str, kind: str, name: str, body: str):
        self.con.execute("DELETE FROM fts WHERE ref=?", (ref,))
        self.con.execute("INSERT INTO fts(kind,name,body,ref) VALUES(?,?,?,?)", (kind, name, body or "", ref))

    def _entity_text(self, eid: int) -> str:
        e = self.con.execute("SELECT props FROM entities WHERE id=?", (eid,)).fetchone()
        al = [r[0] for r in self.con.execute("SELECT alias FROM aliases WHERE entity_id=?", (eid,))]
        facts = [r[0] for r in self.con.execute("SELECT text FROM facts WHERE entity_id=? AND superseded_by IS NULL", (eid,))]
        props = json.loads(e[0]) if e else {}
        return " ".join(al + facts + [str(v) for v in props.values() if isinstance(v, (str, int, float))])

    def _refresh_entity_fts(self, eid: int):
        r = self.con.execute("SELECT kind,name FROM entities WHERE id=?", (eid,)).fetchone()
        if r:
            self._fts_set(f"e:{eid}", r[0], r[1], self._entity_text(eid))

    def audit(self, source: str, action: str, ref: str = ""):
        self.con.execute("INSERT INTO audit(ts,source,action,ref) VALUES(?,?,?,?)", (time.time(), source, action, ref))

    # ------------------------------------------------------------------ entities
    def resolve(self, ref, source: str = "", create: bool = True) -> int | None:
        """Turn a reference into an entity id. A ref is 'kind:name', {'kind','name'}, or a bare name.
        A bare name matches an existing entity of any kind if there is exactly one; otherwise it becomes a new 'thing'."""
        kind = None
        props = None
        aliases: list[str] = []
        if isinstance(ref, dict):
            kind, name = ref.get("kind"), ref.get("name")
            props, aliases = ref.get("props"), list(ref.get("aliases") or [])
        else:
            name = str(ref)
            m = re.match(r"^([A-Za-z][A-Za-z0-9_ -]{0,19}):\s*(.+)$", name)
            if m and " " not in m.group(1).strip():
                kind, name = m.group(1), m.group(2)
        if not name or not str(name).strip():
            raise ValueError("empty entity name")
        if norm(name) in SELF_WORDS and (not kind or norm_rel(kind) == "person"):
            kind, name = "person", "Me"
        n = norm(name)
        with self.lock:
            if kind:
                k = norm_rel(kind)
                row = self.con.execute("SELECT id FROM entities WHERE kind=? AND norm=?", (k, n)).fetchone() or \
                      self.con.execute("SELECT e.id FROM aliases a JOIN entities e ON e.id=a.entity_id WHERE e.kind=? AND a.norm=?", (k, n)).fetchone() or \
                      self.con.execute("SELECT entity_id FROM merged_names WHERE kind=? AND norm=?", (k, n)).fetchone()
            else:
                rows = self.con.execute("SELECT id,kind FROM entities WHERE norm=? UNION "
                                        "SELECT e.id,e.kind FROM aliases a JOIN entities e ON e.id=a.entity_id WHERE a.norm=?", (n, n)).fetchall()
                if len(rows) > 1:   # app bookkeeping (task buckets) never competes with real things: a bare "Algorithms" is the course
                    real = [r for r in rows if r[1] != "bucket"]
                    if len(real) == 1:
                        rows = real
                row = (rows[0][0],) if len(rows) == 1 else None
                k = "thing"
                if len(rows) > 1:  # ambiguous bare name: don't guess
                    raise ValueError(f"{name!r} matches {len(rows)} entities; say which with kind:name (e.g. person:{name})")
            if row:
                eid = row[0]
                if props:
                    cur = json.loads(self.con.execute("SELECT props FROM entities WHERE id=?", (eid,)).fetchone()[0])
                    cur.update(props)
                    self.con.execute("UPDATE entities SET props=?, updated_at=? WHERE id=?", (json.dumps(cur), time.time(), eid))
            else:
                if not create:
                    return None
                now = time.time()
                eid = self.con.execute("INSERT INTO entities(kind,name,norm,props,created_at,updated_at,source) VALUES(?,?,?,?,?,?,?)",
                                       (k, str(name).strip(), n, json.dumps(props or {}), now, now, source)).lastrowid
            for a in aliases:
                if norm(a) and norm(a) != n:
                    self.con.execute("INSERT OR IGNORE INTO aliases(entity_id,alias,norm) VALUES(?,?,?)", (eid, str(a).strip(), norm(a)))
            self._refresh_entity_fts(eid)
            return eid

    def _name(self, eid: int) -> dict:
        r = self.con.execute("SELECT id,kind,name FROM entities WHERE id=?", (eid,)).fetchone()
        return {"id": r[0], "kind": r[1], "name": r[2]}

    # ------------------------------------------------------------------ writes
    def log_event(self, title: str, when=None, body: str = "", kind: str = "event", entities=None, props=None,
                  source: str = "", dedupe: str | None = None, ts: float | None = None) -> dict:
        if not str(title).strip():
            raise ValueError("an event needs a title")
        t = ts if ts is not None else parse_ts(when)
        key = dedupe or hashlib.sha1(f"{norm(title)}|{int(t // 60)}".encode()).hexdigest()
        with self.lock:
            dup = self.con.execute("SELECT id FROM events WHERE dedupe=?", (key,)).fetchone()
            if dup:
                return {"event_id": dup[0], "duplicate": True}
            eid = self.con.execute("INSERT INTO events(ts,title,body,kind,props,created_at,source,dedupe) VALUES(?,?,?,?,?,?,?,?)",
                                   (t, str(title).strip(), body or "", norm_rel(kind), json.dumps(props or {}), time.time(), source, key)).lastrowid
            linked = []
            for item in entities or []:
                role = "involves"
                if isinstance(item, dict) and item.get("role"):
                    role = norm_rel(item["role"])
                ent = self.resolve(item, source)
                self.con.execute("INSERT OR IGNORE INTO event_entities(event_id,entity_id,role) VALUES(?,?,?)", (eid, ent, role))
                linked.append(self._name(ent)["name"])
            self._fts_set(f"v:{eid}", norm_rel(kind), str(title).strip(), f"{body or ''} {' '.join(linked)}")
            self.audit(source, "log_event", f"v:{eid}")
            self._commit()
            return {"event_id": eid, "entities": linked, "when": iso(t)}

    def link(self, src, rel: str, dst, start=None, end=None, props=None, source: str = "") -> dict:
        with self.lock:
            a, b = self.resolve(src, source), self.resolve(dst, source)
            r = norm_rel(rel)
            s, e = (parse_ts(start) if start else None), (parse_ts(end) if end else None)
            dup = self.con.execute("SELECT id FROM edges WHERE src=? AND rel=? AND dst=? AND start_ts IS ?", (a, r, b, s)).fetchone()
            if dup:
                return {"edge_id": dup[0], "duplicate": True}
            eid = self.con.execute("INSERT INTO edges(src,rel,dst,props,start_ts,end_ts,created_at,source) VALUES(?,?,?,?,?,?,?,?)",
                                   (a, r, b, json.dumps(props or {}), s, e, time.time(), source)).lastrowid
            self.audit(source, "link", f"g:{eid}")
            self._commit()
            return {"edge_id": eid, "edge": f"{self._name(a)['name']} -{r}-> {self._name(b)['name']}"}

    def remember(self, subject, fact: str, when=None, source: str = "") -> dict:
        if not str(fact).strip():
            raise ValueError("empty fact")
        with self.lock:
            eid = self.resolve(subject, source)
            same = self.con.execute("SELECT id FROM facts WHERE entity_id=? AND text=? AND superseded_by IS NULL", (eid, fact.strip())).fetchone()
            if same:
                return {"fact_id": same[0], "duplicate": True}
            fid = self.con.execute("INSERT INTO facts(entity_id,text,ts,source) VALUES(?,?,?,?)", (eid, fact.strip(), parse_ts(when), source)).lastrowid
            self._refresh_entity_fts(eid)
            self.audit(source, "remember", f"f:{fid}")
            self._commit()
            return {"fact_id": fid, "about": self._name(eid)["name"]}

    def apply(self, op: str, args: dict, ts: float | None, source: str) -> dict:
        """One entry from the sealed mailbox (or a direct call)."""
        if op == "log_event":
            return self.log_event(args["title"], args.get("when"), args.get("body", ""), args.get("kind", "event"),
                                  args.get("entities"), args.get("props"), source, args.get("dedupe"), ts=None if args.get("when") else ts)
        if op == "note":
            return self.log_event(args.get("title") or str(args["text"])[:80], args.get("when"), args["text"], "note", args.get("entities"),
                                  None, source, args.get("dedupe"), ts=None if args.get("when") else ts)
        if op == "link":
            return self.link(args["src"], args["rel"], args["dst"], args.get("start"), args.get("end"), args.get("props"), source)
        if op == "remember":
            return self.remember(args["subject"], args["fact"], args.get("when") or ts, source)
        raise ValueError(f"unknown operation {op!r}")

    # ------------------------------------------------------------------ previews and lookups by ref
    def peek(self, ref) -> dict:
        """What a reference WOULD attach to, without creating or changing anything:
        {as: 'person:Sam', ref: 'e:12' | None, status: 'existing' | 'new' | 'ambiguous', error?}."""
        if not isinstance(ref, dict) and re.match(r"^e:\d+$", str(ref).strip()):
            row = self.con.execute("SELECT id FROM entities WHERE id=?", (int(str(ref).strip()[2:]),)).fetchone()
            if not row:
                return {"as": str(ref), "ref": None, "status": "ambiguous", "error": f"no entity {ref}"}
            n = self._name(row[0])
            return {"as": f"{n['kind']}:{n['name']}", "ref": f"e:{row[0]}", "status": "existing"}
        if isinstance(ref, dict):
            kind, name = ref.get("kind"), ref.get("name")
        else:
            name = str(ref)
            m = re.match(r"^([A-Za-z][A-Za-z0-9_ -]{0,19}):\s*(.+)$", name)
            kind, name = (m.group(1), m.group(2)) if m and " " not in m.group(1).strip() else (None, name)
        if not name or not str(name).strip():
            return {"as": "", "ref": None, "status": "ambiguous", "error": "empty entity name"}
        if norm(name) in SELF_WORDS and (not kind or norm_rel(kind) == "person"):
            kind, name = "person", "Me"
        try:
            eid = self.resolve({"kind": kind, "name": name} if kind else str(name), create=False)
        except ValueError as e:
            return {"as": f"{kind or '?'}:{name}", "ref": None, "status": "ambiguous", "error": str(e)}
        if eid is not None:
            n = self._name(eid)
            return {"as": f"{n['kind']}:{n['name']}", "ref": f"e:{eid}", "status": "existing"}
        return {"as": f"{norm_rel(kind) if kind else 'thing'}:{str(name).strip()}", "ref": None, "status": "new"}

    def get(self, ref: str, depth: int = 0) -> dict | None:
        """One thing by ref (e:/v:/f:/g:) or entity name."""
        m = re.match(r"^([evfg]):(\d+)$", str(ref).strip())
        with self.lock:
            if not m or m.group(1) == "e":
                out = self.entity(ref)
                if out and "error" not in out and depth > 0:
                    out["related"] = self.related(out["ref"], depth)
                return out
            k, i = m.group(1), int(m.group(2))
            if k == "v":
                r = self.con.execute("SELECT ts,title,body,kind,props,source FROM events WHERE id=?", (i,)).fetchone()
                if not r:
                    return None
                return {"ref": ref, "when": iso(r[0]), "title": r[1], "body": r[2], "kind": r[3], "props": json.loads(r[4]), "source": r[5],
                        "with": [{"ref": f"e:{e}", "entity": f"{kd}:{nm}", "role": ro} for e, kd, nm, ro in self.con.execute(
                            "SELECT n.id,n.kind,n.name,x.role FROM event_entities x JOIN entities n ON n.id=x.entity_id WHERE x.event_id=?", (i,))]}
            if k == "f":
                r = self.con.execute("SELECT entity_id,text,ts,source,superseded_by FROM facts WHERE id=?", (i,)).fetchone()
                if not r:
                    return None
                n = self._name(r[0])
                return {"ref": ref, "about": f"e:{r[0]}", "entity": f"{n['kind']}:{n['name']}", "text": r[1], "when": iso(r[2]), "source": r[3],
                        "superseded_by": f"f:{r[4]}" if r[4] else None}
            r = self.con.execute("SELECT src,rel,dst,start_ts,end_ts,source FROM edges WHERE id=?", (i,)).fetchone()
            if not r:
                return None
            a, b = self._name(r[0]), self._name(r[2])
            return {"ref": ref, "from": f"e:{r[0]}", "from_name": f"{a['kind']}:{a['name']}", "rel": r[1], "to": f"e:{r[2]}",
                    "to_name": f"{b['kind']}:{b['name']}", "start": iso(r[3]), "end": iso(r[4]), "source": r[5]}

    # ------------------------------------------------------------------ reads
    def search(self, q: str, limit: int = 10) -> list[dict]:
        terms = re.findall(r"[\w']+", q.lower())
        if not terms:
            return []
        match = " ".join(f'"{t}"*' for t in terms)
        with self.lock:
            rows = self.con.execute("SELECT ref,kind,name,snippet(fts,2,'[',']','…',12),bm25(fts) FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?",
                                    (match, limit)).fetchall()
            out = []
            for ref, kind, name, snip, _ in rows:
                item = {"ref": ref, "kind": kind, "name": name, "snippet": snip}
                if ref.startswith("v:"):
                    ev = self.con.execute("SELECT ts FROM events WHERE id=?", (int(ref[2:]),)).fetchone()
                    item["when"] = iso(ev[0]) if ev else None
                out.append(item)
            return out

    def entity(self, ref, events: int = 10) -> dict | None:
        with self.lock:
            try:
                m = re.match(r"^e:(\d+)$", str(ref).strip())
                eid = (int(m.group(1)) if self.con.execute("SELECT 1 FROM entities WHERE id=?", (int(m.group(1)),)).fetchone() else None) if m else self.resolve(ref, create=False)
            except ValueError as e:
                return {"error": str(e)}
            if eid is None:
                return None
            kind, name, props, created = self.con.execute("SELECT kind,name,props,created_at FROM entities WHERE id=?", (eid,)).fetchone()
            out = {"id": eid, "kind": kind, "name": name, "props": json.loads(props), "first_seen": iso(created),
                   "aliases": [r[0] for r in self.con.execute("SELECT alias FROM aliases WHERE entity_id=?", (eid,))],
                   "ref": f"e:{eid}",
                   "facts": [{"ref": f"f:{i}", "text": t, "when": iso(ts)} for i, t, ts in self.con.execute(
                       "SELECT id,text,ts FROM facts WHERE entity_id=? AND superseded_by IS NULL ORDER BY ts DESC", (eid,))]}
            rel = []
            for gid, src, r, dst, s, e in self.con.execute("SELECT id,src,rel,dst,start_ts,end_ts FROM edges WHERE src=? OR dst=?", (eid, eid)):
                other, outgoing = (dst, True) if src == eid else (src, False)
                n = self._name(other)
                rel.append({"ref": f"g:{gid}", "rel": r, "direction": "out" if outgoing else "in", "with": f"{n['kind']}:{n['name']}", "from": iso(s), "to": iso(e)})
            out["relations"] = rel
            out["events"] = [{"ref": f"v:{i}", "when": iso(ts), "title": t, "kind": k} for i, ts, t, k in self.con.execute(
                "SELECT e.id,e.ts,e.title,e.kind FROM events e JOIN event_entities x ON x.event_id=e.id WHERE x.entity_id=? ORDER BY e.ts DESC LIMIT ?", (eid, events))]
            return out

    def related(self, ref, depth: int = 1, limit: int = 40) -> list[dict]:
        """Neighbours through edges and through shared events (co-occurrence), up to depth 2."""
        with self.lock:
            start = self.resolve(ref, create=False)
            if start is None:
                return []
            seen = {start: 0}
            frontier = [start]
            out: list[dict] = []
            for d in range(1, min(depth, 2) + 1):
                nxt = []
                for node in frontier:
                    cands = [(dst, rel, "edge") for dst, rel in self.con.execute("SELECT dst,rel FROM edges WHERE src=?", (node,))]
                    cands += [(src, rel, "edge") for src, rel in self.con.execute("SELECT src,rel FROM edges WHERE dst=?", (node,))]
                    cands += [(o, "co_occurs_with", "event") for (o,) in self.con.execute(
                        "SELECT DISTINCT b.entity_id FROM event_entities a JOIN event_entities b ON a.event_id=b.event_id WHERE a.entity_id=? AND b.entity_id<>?", (node, node))]
                    for other, rel, via in cands:
                        if other in seen:
                            continue
                        seen[other] = d
                        nxt.append(other)
                        n = self._name(other)
                        out.append({"distance": d, "via": self._name(node)["name"], "rel": rel, "entity": f"{n['kind']}:{n['name']}", "ref": f"e:{other}"})
                frontier = nxt
            return out[:limit]

    def timeline(self, since=None, until=None, ref=None, kind: str | None = None, limit: int = 50) -> list[dict]:
        q = "SELECT e.id,e.ts,e.title,e.kind,e.body FROM events e"
        args: list = []
        where = []
        with self.lock:
            if ref:
                eid = self.resolve(ref, create=False)
                if eid is None:
                    return []
                q += " JOIN event_entities x ON x.event_id=e.id"
                where.append("x.entity_id=?")
                args.append(eid)
            if since:
                where.append("e.ts>=?")
                args.append(parse_ts(since))
            if until:
                where.append("e.ts<?")
                args.append(parse_ts(until))
            if kind:
                where.append("e.kind=?")
                args.append(norm_rel(kind))
            if where:
                q += " WHERE " + " AND ".join(where)
            q += " ORDER BY e.ts DESC LIMIT ?"
            args.append(limit)
            out = []
            for eid, ts, title, k, body in self.con.execute(q, args).fetchall():
                names = [f"{r[0]}:{r[1]}" for r in self.con.execute(
                    "SELECT n.kind,n.name FROM event_entities x JOIN entities n ON n.id=x.entity_id WHERE x.event_id=?", (eid,))]
                out.append({"ref": f"v:{eid}", "when": iso(ts), "title": title, "kind": k, "with": names, **({"body": body[:300]} if body else {})})
            return out

    def graph_view(self, limit: int = 400) -> dict:
        """Names, kinds and connections only (no fact or event text): enough to draw the graph. Links are explicit edges plus
        entities that share events."""
        with self.lock:
            ents = self.con.execute("SELECT id,kind,name,updated_at FROM entities ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            ids = {r[0] for r in ents}
            ev = dict(self.con.execute("SELECT entity_id,count(*) FROM event_entities GROUP BY entity_id").fetchall())
            fc = dict(self.con.execute("SELECT entity_id,count(*) FROM facts WHERE superseded_by IS NULL GROUP BY entity_id").fetchall())
            links: dict[tuple, dict] = {}
            for src, rel, dst in self.con.execute("SELECT src,rel,dst FROM edges"):
                if src in ids and dst in ids:
                    links[(src, dst)] = {"s": src, "t": dst, "rel": rel, "w": 3}
            by_event: dict[int, list[int]] = {}
            for e, n in self.con.execute("SELECT event_id,entity_id FROM event_entities"):
                if n in ids:
                    by_event.setdefault(e, []).append(n)
            for members in by_event.values():
                m = sorted(set(members))
                if len(m) > 12:
                    continue
                for i in range(len(m)):
                    for j in range(i + 1, len(m)):
                        k = (m[i], m[j])
                        if k in links or (k[1], k[0]) in links:
                            links.get(k, links.get((k[1], k[0])))["w"] += 1
                        else:
                            links[k] = {"s": m[i], "t": m[j], "rel": "together", "w": 1}
            return {"nodes": [{"id": i, "kind": k, "name": n, "events": ev.get(i, 0), "facts": fc.get(i, 0)} for i, k, n, _ in ents],
                    "links": list(links.values())}

    def _entity_by_ref(self, ref: str) -> int:
        """Existing entity id for kind:name, a bare name (must be unambiguous) or e:12. Never creates."""
        m = re.match(r"^e:(\d+)$", str(ref).strip())
        if m:
            if not self.con.execute("SELECT 1 FROM entities WHERE id=?", (int(m.group(1)),)).fetchone():
                raise ValueError(f"no entity {ref}")
            return int(m.group(1))
        eid = self.resolve(ref, create=False)
        if eid is None:
            raise ValueError(f"nothing in the graph matches {str(ref)!r}")
        return eid

    def merge(self, from_ref: str, to_ref: str, source: str = "cli", dry_run: bool = False) -> dict:
        """Fold one entity into another (TO survives) in a single transaction; the old name keeps resolving to TO."""
        with self.lock:
            try:
                src, dst = self._entity_by_ref(from_ref), self._entity_by_ref(to_ref)
                f = self.con.execute("SELECT kind,name,norm,props,created_at FROM entities WHERE id=?", (src,)).fetchone()
                t = self.con.execute("SELECT kind,name,norm,props,created_at FROM entities WHERE id=?", (dst,)).fetchone()
                if src == dst:
                    if norm(re.sub(r"^[A-Za-z][A-Za-z0-9_ -]{0,19}:\s*", "", str(from_ref))) == t[2] or re.match(r"^e:\d+$", str(from_ref).strip()):
                        raise ValueError("those are the same entity")
                    return {"noop": True, "message": f"{from_ref!r} already resolves to {t[0]}:{t[1]}; nothing to merge", "from": f"e:{src}", "to": f"e:{dst}"}
                if f[0] == "person" and f[2] == "me":
                    raise ValueError("person:Me can only be a merge target, never merged away")
                c = {"from": f"{f[0]}:{f[1]}", "to": f"{t[0]}:{t[1]}"}
                q = lambda sql, *a: self.con.execute(sql, a)
                # events: repoint; where TO already has the same (event, role) the FROM row is dropped
                c["events_moved"] = q("UPDATE OR IGNORE event_entities SET entity_id=? WHERE entity_id=?", dst, src).rowcount
                c["event_links_dropped"] = q("DELETE FROM event_entities WHERE entity_id=?", src).rowcount
                # edges: repoint both ends, then drop self-loops and exact duplicates around TO (oldest kept)
                c["edges_moved"] = q("UPDATE edges SET src=? WHERE src=?", dst, src).rowcount + q("UPDATE edges SET dst=? WHERE dst=?", dst, src).rowcount
                loops = q("DELETE FROM edges WHERE src=dst AND src=?", dst).rowcount
                dups = q("""DELETE FROM edges WHERE (src=? OR dst=?) AND id NOT IN
                            (SELECT min(id) FROM edges GROUP BY src,rel,dst,start_ts,end_ts)""", dst, dst).rowcount
                c["edges_dropped"] = loops + dups
                # facts: drop exact repeats of what TO already says, then repoint the rest untouched
                c["facts_dropped"] = q("DELETE FROM facts WHERE entity_id=? AND text IN (SELECT text FROM facts WHERE entity_id=?)", src, dst).rowcount
                c["facts_moved"] = q("UPDATE facts SET entity_id=? WHERE entity_id=?", dst, src).rowcount
                # names: FROM's aliases and its own name now resolve to TO, also when written with the old kind (merged_names)
                old = [(r[0], r[1]) for r in q("SELECT alias,norm FROM aliases WHERE entity_id=?", src)] + [(f[1], f[2])]
                c["aliases_moved"] = q("UPDATE OR IGNORE aliases SET entity_id=? WHERE entity_id=?", dst, src).rowcount
                for alias, an in old:
                    if an != t[2]:
                        q("INSERT OR IGNORE INTO aliases(entity_id,alias,norm) VALUES(?,?,?)", dst, alias, an)
                    if f[0] != t[0]:
                        q("INSERT OR IGNORE INTO merged_names(kind,norm,entity_id) VALUES(?,?,?)", f[0], an, dst)
                q("UPDATE merged_names SET entity_id=? WHERE entity_id=?", dst, src)
                # props: TO's own keys win; keep the earlier created_at
                fp, tp = json.loads(f[3]), json.loads(t[3])
                for k, v in fp.items():
                    tp.setdefault(k, v)
                q("UPDATE entities SET props=?, created_at=?, updated_at=? WHERE id=?", json.dumps(tp), min(f[4] or time.time(), t[4] or time.time()), time.time(), dst)
                q("DELETE FROM fts WHERE ref=?", f"e:{src}")
                q("DELETE FROM entities WHERE id=?", src)
                self._refresh_entity_fts(dst)
                if dry_run:
                    self.con.rollback()
                    c["dry_run"] = True
                else:
                    self.audit(source, "merge", f"e:{src} -> e:{dst}")
                    self._commit()
                return c
            except Exception:
                if not self._tx:
                    self.con.rollback()
                raise

    def stats(self) -> dict:
        with self.lock:
            c = lambda t: self.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            kinds = dict(self.con.execute("SELECT kind,count(*) FROM entities GROUP BY kind ORDER BY 2 DESC").fetchall())
            first = self.con.execute("SELECT min(ts),max(ts) FROM events").fetchone()
            return {"entities": c("entities"), "edges": c("edges"), "events": c("events"), "facts": c("facts"), "by_kind": kinds,
                    "events_from": iso(first[0]), "events_to": iso(first[1])}

    def audit_log(self, limit: int = 30) -> list[dict]:
        with self.lock:
            return [{"when": iso(ts), "source": s, "action": a, "ref": r} for ts, s, a, r in self.con.execute(
                "SELECT ts,source,action,ref FROM audit ORDER BY id DESC LIMIT ?", (limit,))]

    def _parse_ref(self, ref: str) -> tuple[str, int]:
        m = re.match(r"^([evfg]):(\d+)$", str(ref).strip())
        if not m:
            raise ValueError("ref must look like e:12 (entity), v:7 (event), f:3 (fact) or g:5 (link); every read result shows them")
        return m.group(1), int(m.group(2))

    def _snapshot(self, kind: str, i: int) -> dict | None:
        """Everything that deleting this ref would remove, as plain data (also what undo restores from)."""
        q = lambda sql, *a: self.con.execute(sql, a).fetchall()
        if kind == "f":
            r = q("SELECT entity_id,text,ts,source,superseded_by FROM facts WHERE id=?", i)
            if not r:
                return None
            ent = self._name(r[0][0])
            return {"kind": "f", "id": i, "row": list(r[0]), "label": f"fact about {ent['kind']}:{ent['name']}: {r[0][1][:120]}"}
        if kind == "g":
            r = q("SELECT src,rel,dst,props,start_ts,end_ts,created_at,source FROM edges WHERE id=?", i)
            if not r:
                return None
            a, b = self._name(r[0][0]), self._name(r[0][2])
            return {"kind": "g", "id": i, "row": list(r[0]), "label": f"link {a['name']} -{r[0][1]}-> {b['name']}"}
        if kind == "v":
            r = q("SELECT ts,title,body,kind,props,created_at,source,dedupe FROM events WHERE id=?", i)
            if not r:
                return None
            return {"kind": "v", "id": i, "row": list(r[0]), "label": f"event {r[0][1]!r} ({iso(r[0][0])})",
                    "links": [list(x) for x in q("SELECT entity_id,role FROM event_entities WHERE event_id=?", i)]}
        r = q("SELECT kind,name,norm,props,created_at,updated_at,source FROM entities WHERE id=?", i)
        if not r:
            return None
        if r[0][0] == "person" and r[0][2] == "me":
            raise ValueError("person:Me is you and can't be deleted")
        evs = q("SELECT e.id,e.title FROM events e JOIN event_entities x ON x.event_id=e.id WHERE x.entity_id=?", i)
        orphans = [(v, t) for v, t in evs if self.con.execute("SELECT count(*) FROM event_entities WHERE event_id=? AND entity_id<>?", (v, i)).fetchone()[0] == 0]
        return {"kind": "e", "id": i, "row": list(r[0]), "label": f"{r[0][0]}:{r[0][1]}",
                "aliases": [x[0] for x in q("SELECT alias FROM aliases WHERE entity_id=?", i)],
                "merged_names": [list(x) for x in q("SELECT kind,norm FROM merged_names WHERE entity_id=?", i)],
                "facts": [list(x) for x in q("SELECT id,text,ts,source,superseded_by FROM facts WHERE entity_id=?", i)],
                "edges": [list(x) for x in q("SELECT id,src,rel,dst,props,start_ts,end_ts,created_at,source FROM edges WHERE src=? OR dst=?", i, i)],
                "event_links": [list(x) for x in q("SELECT event_id,role FROM event_entities WHERE entity_id=?", i)],
                "events_left_without_entities": [{"ref": f"v:{v}", "title": t} for v, t in orphans]}

    def _describe(self, snap: dict) -> dict:
        out = {"ref": f"{snap['kind']}:{snap['id']}", "deletes": snap["label"]}
        if snap["kind"] == "e":
            out.update({"also_removes": {"facts": len(snap["facts"]), "links": len(snap["edges"]), "event_mentions": len(snap["event_links"])},
                        "events_left_without_entities": snap["events_left_without_entities"],
                        "note": "Events are kept (they just stop mentioning this entity). Forget the listed events too if they should go."})
        return out


    def delete_snapshot(self, snap: dict) -> None:
        """Remove what `_snapshot` described (cascades do the rest) and keep the search index honest."""
        k, i = snap["kind"], snap["id"]
        tbl = {"e": "entities", "v": "events", "f": "facts", "g": "edges"}[k]
        self.con.execute(f"DELETE FROM {tbl} WHERE id=?", (i,))
        self.con.execute("DELETE FROM fts WHERE ref=?", (f"{k}:{i}",))
        if k == "f":      # a deleted fact must stop being findable through its entity's search text
            self._refresh_entity_fts(snap["row"][0])

    def restore_snapshot(self, sn: dict) -> int:
        """Put a snapshot back under its old ref. Links to things that have since gone are skipped; returns how many."""
        k, i, row = sn["kind"], sn["id"], sn["row"]
        ex = lambda eid: self.con.execute("SELECT 1 FROM entities WHERE id=?", (eid,)).fetchone() is not None
        skipped = 0
        if k == "e":
            self.con.execute("INSERT INTO entities(id,kind,name,norm,props,created_at,updated_at,source) VALUES(?,?,?,?,?,?,?,?)", (i, *row))
            for a in sn["aliases"]:
                self.con.execute("INSERT OR IGNORE INTO aliases(entity_id,alias,norm) VALUES(?,?,?)", (i, a, norm(a)))
            for kk, nn in sn["merged_names"]:
                self.con.execute("INSERT OR IGNORE INTO merged_names(kind,norm,entity_id) VALUES(?,?,?)", (kk, nn, i))
            for f in sn["facts"]:
                self.con.execute("INSERT INTO facts(id,entity_id,text,ts,source,superseded_by) VALUES(?,?,?,?,?,?)", (f[0], i, *f[1:]))
            for g in sn["edges"]:
                if ex(g[1]) and ex(g[3]):
                    self.con.execute("INSERT OR IGNORE INTO edges(id,src,rel,dst,props,start_ts,end_ts,created_at,source) VALUES(?,?,?,?,?,?,?,?,?)", g)
                else:
                    skipped += 1
            for v, role in sn["event_links"]:
                if self.con.execute("SELECT 1 FROM events WHERE id=?", (v,)).fetchone():
                    self.con.execute("INSERT OR IGNORE INTO event_entities(event_id,entity_id,role) VALUES(?,?,?)", (v, i, role))
                else:
                    skipped += 1
            self._refresh_entity_fts(i)
        elif k == "f":
            if not ex(row[0]):
                raise ValueError("the entity this fact belonged to is gone")
            self.con.execute("INSERT INTO facts(id,entity_id,text,ts,source,superseded_by) VALUES(?,?,?,?,?,?)", (i, *row))
            self._refresh_entity_fts(row[0])
        elif k == "g":
            if not (ex(row[0]) and ex(row[2])):
                raise ValueError("one end of this link is gone")
            self.con.execute("INSERT INTO edges(id,src,rel,dst,props,start_ts,end_ts,created_at,source) VALUES(?,?,?,?,?,?,?,?,?)", (i, *row))
        else:
            self.con.execute("INSERT INTO events(id,ts,title,body,kind,props,created_at,source,dedupe) VALUES(?,?,?,?,?,?,?,?,?)", (i, *row))
            names = []
            for eid, role in sn["links"]:
                if ex(eid):
                    self.con.execute("INSERT OR IGNORE INTO event_entities(event_id,entity_id,role) VALUES(?,?,?)", (i, eid, role))
                    names.append(self._name(eid)["name"])
                else:
                    skipped += 1
            self._fts_set(f"v:{i}", row[3], row[1], f"{row[2] or ''} {' '.join(names)}")
        return skipped

    def forget(self, ref: str, source: str = "") -> bool:
        """Hard delete with no undo: the owner's CLI path. The audit log keeps that it happened, not what it was."""
        k, i = self._parse_ref(ref)
        with self.lock:
            snap = self._snapshot(k, i)
            if snap is None:
                return False
            self.delete_snapshot(snap)
            self.audit(source, "forget", f"{k}:{i}")
            self._commit()
            return True

    # ------------------------------------------------------------------ per-agent grants
    LEVELS = ("read", "write", "manage")

    def grant(self, name: str, hours: float | None = None, level: str = "read", ask_adds: bool = False, auto_destructive: bool = False) -> str:
        if level not in self.LEVELS:
            raise ValueError(f"level must be one of {', '.join(self.LEVELS)}")
        token = "lg_" + secrets.token_urlsafe(24)
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO grants(name,token_hash,created_at,expires_at,revoked,level,ask_adds,auto_destructive) VALUES(?,?,?,?,0,?,?,?)",
                             (name, hashlib.sha256(token.encode()).hexdigest(), time.time(), time.time() + hours * 3600 if hours else None,
                              level, int(ask_adds), int(auto_destructive)))
            self.audit("cli", "grant", f"{name} ({level})")
            self._commit()
        return token

    def revoke(self, name: str) -> bool:
        with self.lock:
            n = self.con.execute("UPDATE grants SET revoked=1 WHERE name=?", (name,)).rowcount
            self.audit("cli", "revoke", name)
            self._commit()
            return bool(n)

    def grant_info(self, token: str) -> dict | None:
        """The grant behind a token if it is valid right now: {name, level, ask_adds, auto_destructive}."""
        h = hashlib.sha256((token or "").encode()).hexdigest()
        with self.lock:
            r = self.con.execute("SELECT name,expires_at,revoked,level,ask_adds,auto_destructive FROM grants WHERE token_hash=?", (h,)).fetchone()
        if not r or r[2] or (r[1] and r[1] < time.time()):
            return None
        return {"name": r[0], "level": r[3], "ask_adds": bool(r[4]), "auto_destructive": bool(r[5])}

    def check_token(self, token: str) -> str | None:
        """Returns the grant name if the token is valid right now."""
        g = self.grant_info(token)
        return g["name"] if g else None

    def grants(self) -> list[dict]:
        with self.lock:
            return [{"name": n, "level": lv, "created": iso(c), "expires": iso(e), "revoked": bool(rv),
                     "approval": ("every change" if ask else "deletes/edits only" if lv == "manage" and not auto else "none (deletes too)" if lv == "manage" else "n/a")}
                    for n, c, e, rv, lv, ask, auto in self.con.execute(
                        "SELECT name,created_at,expires_at,revoked,level,ask_adds,auto_destructive FROM grants ORDER BY created_at DESC")]
