import json, os, sqlite3, time
import pytest
from lifegraph import crypto, store

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEGRAPH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIFEGRAPH_RUNTIME", str(tmp_path / "run"))
    return tmp_path

def test_identity_roundtrip_and_wrong_passphrase(env):
    rec = crypto.create("correct horse battery")
    k = crypto.unlock("correct horse battery"); assert len(k.db_key) == 32
    k2 = crypto.unlock(rec); assert k2.db_key == k.db_key            # recovery key opens the same identity
    with pytest.raises(PermissionError): crypto.unlock("wrong passphrase!!")
    with pytest.raises(ValueError): crypto.create("short") if not crypto.exists() else (_ for _ in ()).throw(ValueError())

def test_database_is_actually_encrypted(env):
    crypto.create("correct horse battery"); k = crypto.unlock("correct horse battery")
    s = store.Store(k); s.log_event("Finished the OS lab", entities=["person:Sam"], body="secret-marker-xyz"); s.close()
    raw = (crypto.home() / "graph.db").read_bytes()
    assert b"secret-marker-xyz" not in raw and b"OSLab" not in raw and not raw.startswith(b"SQLite format 3")
    with pytest.raises(sqlite3.DatabaseError): sqlite3.connect(str(crypto.home() / "graph.db")).execute("select * from sqlite_master").fetchall()
    with pytest.raises(PermissionError): store.Store(crypto.Keys(b"\x01" * 32, b"\x02" * 32))   # wrong key refused

def test_sealed_mailbox_is_unreadable_without_unlock(env):
    crypto.create("correct horse battery")
    line = crypto.seal({"op": "note", "args": {"text": "very private thought"}})
    assert "private" not in line
    k = crypto.unlock("correct horse battery"); assert crypto.open_sealed(line, k)["args"]["text"] == "very private thought"

def test_graph_ops(env):
    crypto.create("correct horse battery"); s = store.Store(crypto.unlock("correct horse battery"))
    r = s.log_event("Finished OS lab", when="2026-10-14T17:30", entities=["person:Sam", {"kind": "course", "name": "Algorithms", "aliases": ["ALGO"]}, "project:Thesis"], body="it was brutal")
    assert r["entities"] == ["Sam", "Algorithms", "Thesis"]
    assert s.log_event("Finished OS lab", when="2026-10-14T17:30")["duplicate"]                  # a chat retrying does not double-log
    s.link("person:Sam", "classmate_of", "me"); s.link("project:Thesis", "part_of", "course:Algorithms")
    s.remember("person:Sam", "studies at the same university"); assert s.remember("person:Sam", "studies at the same university")["duplicate"]
    assert s.resolve("ALGO") == s.resolve("course:Algorithms")                                     # alias resolves
    e = s.entity("Sam"); assert e["facts"][0]["text"].startswith("studies") and e["events"][0]["title"] == "Finished OS lab"
    hits = s.search("brutal"); assert hits and hits[0]["ref"].startswith("v:")
    assert any(x["entity"].endswith("Thesis") for x in s.related("Sam", depth=1))                   # co-occurrence edge
    assert any(x["entity"].endswith("Algorithms") for x in s.related("Sam", depth=2))
    assert s.timeline(since="2026-10-14", until="2026-10-15")[0]["title"] == "Finished OS lab"
    assert s.stats()["events"] == 1
    with pytest.raises(ValueError): s.log_event("x", entities=["person:Sam", "course:Sam"]) and s.resolve("Sam")  # ambiguous bare name is refused, not guessed

def test_grants(env):
    crypto.create("correct horse battery"); s = store.Store(crypto.unlock("correct horse battery"))
    t = s.grant("claude-code:life", hours=1); assert s.check_token(t) == "claude-code:life" and s.check_token("lg_nope") is None
    s.revoke("claude-code:life"); assert s.check_token(t) is None

def test_the_user_is_always_one_entity(env):
    crypto.create("correct horse battery"); s = store.Store(crypto.unlock("correct horse battery"))
    ids = {s.resolve(x) for x in ["me", "I", "myself", "person:me", "person:Me", {"kind": "person", "name": "myself"}]}
    assert len(ids) == 1
    s.link("person:Sam", "classmate_of", "me"); s.log_event("Went climbing", entities=["I", "person:Sam"])
    assert s.entity("me")["name"] == "Me" and len(s.entity("me")["events"]) == 1
    assert s.resolve("course:Me") != s.resolve("me")          # only people can be "me"

def test_a_bare_name_prefers_real_things_over_buckets(env):
    crypto.create("correct horse battery"); s = store.Store(crypto.unlock("correct horse battery"))
    course = s.resolve("course:Algorithms"); bucket = s.resolve("bucket:algorithms")
    assert course != bucket
    assert s.resolve("Algorithms") == course and s.entity("algorithms")["kind"] == "course"      # bare name = the course
    assert s.entity("bucket:algorithms")["kind"] == "bucket"                                       # the bucket is still reachable explicitly
    s.resolve("person:Sam"); s.resolve("course:Sam")
    with pytest.raises(ValueError): s.resolve("Sam")                                               # two real things: still refuses to guess

def test_graph_view_has_names_and_links_but_no_text(env):
    crypto.create("correct horse battery"); k = crypto.unlock("correct horse battery"); s = store.Store(k)
    s.log_event("Finished the OS lab", entities=["person:Sam", "course:OSLab"], body="secret-marker-xyz")
    s.link("person:Sam", "classmate_of", "me"); s.remember("course:OSLab", "secret-fact-abc")
    g = s.graph_view(); names = {n["name"] for n in g["nodes"]}
    assert {"Sam", "OSLab"} <= names and g["links"]
    assert "secret-marker-xyz" not in json.dumps(g) and "secret-fact-abc" not in json.dumps(g)
    s.close()

def _fresh(env):
    crypto.create("correct horse battery"); return store.Store(crypto.unlock("correct horse battery"))

def _count(s, t): return s.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]

def test_merge_unions_history_and_old_names_keep_resolving(env):
    s = _fresh(env)
    s.log_event("Lab 1", entities=["topic:OSLab", "person:Sam"]); s.log_event("Lab 2", entities=["course:Operating Systems", "topic:OSLab"])
    s.remember("topic:OSLab", "meets Tuesdays"); s.remember("topic:OSLab", "shared fact"); s.remember("course:Operating Systems", "shared fact")
    s.link("topic:OSLab", "taught_by", "person:Sam"); s.link("course:Operating Systems", "taught_by", "person:Sam"); s.link("topic:OSLab", "same_as", "course:Operating Systems")
    to = s.resolve("course:Operating Systems", create=False); s.resolve({"kind": "topic", "name": "OSLab", "props": {"x": 1}})
    r = s.merge("topic:OSLab", "course:Operating Systems")
    assert r["facts_dropped"] == 1 and r["facts_moved"] == 1 and r["edges_dropped"] >= 2
    assert s.resolve("topic:OSLab", create=False) == to and s.resolve("OSLab", create=False) == to          # old kind and bare name both land on the survivor
    n = _count(s, "entities"); s.link("topic:OSLab", "also", "person:Sam"); s.remember("OSLab", "new"); assert _count(s, "entities") == n   # no new entity
    e = s.entity("course:Operating Systems"); assert len(e["events"]) == 2 and sum(1 for f in e["facts"]) >= 2
    assert any(f"e:{to}" in str(x) for x in s.search("OSLab"))  # fts finds the survivor by the old name
    assert not s.con.execute("SELECT 1 FROM fts WHERE ref LIKE 'e:%' AND name='OSLab'").fetchone()           # no ghost row
    assert s.con.execute("SELECT count(*) FROM audit WHERE action='merge'").fetchone()[0] == 1
    s.close()

def test_merge_refuses_bad_input_and_changes_nothing(env):
    s = _fresh(env); s.log_event("x", entities=["person:Sam", "topic:Foo", "course:Foo"])
    before = (_count(s, "entities"), _count(s, "edges"))
    for a, b in [("person:Sam", "person:Sam"), ("me", "person:Sam"), ("Foo", "person:Sam"), ("nothing here", "person:Sam"), ("person:Sam", "e:9999")]:
        with pytest.raises(ValueError): s.merge(a, b)
    assert (_count(s, "entities"), _count(s, "edges")) == before
    s.close()

def test_merge_dry_run_changes_nothing_and_alias_merge_is_noop(env):
    s = _fresh(env); s.log_event("x", entities=["topic:AV", "course:Compilers"])
    r = s.merge("topic:AV", "course:Compilers", dry_run=True); assert r["dry_run"] and r["events_moved"] + r["event_links_dropped"] == 1
    assert s.resolve("topic:AV", create=False) is not None and _count(s, "entities") == 2
    n = _count(s, "entities"); s.merge("topic:AV", "course:Compilers"); assert _count(s, "entities") == n - 1
    assert s.merge("topic:AV", "course:Compilers").get("noop")
    s.close()

from lifegraph import changes

W = {"name": "agent-w", "level": "write", "ask_adds": False, "auto_destructive": False}
M = {"name": "agent-m", "level": "manage", "ask_adds": False, "auto_destructive": False}

def test_refs_are_on_every_read(env):
    s = _fresh(env)
    s.log_event("Finished OS lab", when="2026-10-14", entities=["person:Sam", "project:OSLab"]); s.link("person:Sam", "classmate_of", "me"); s.remember("person:Sam", "likes tea")
    e = s.entity("Sam")
    assert e["ref"].startswith("e:") and all(x["ref"][:2] in ("f:", "g:", "v:") for x in e["facts"] + e["relations"] + e["events"])
    assert s.timeline()[0]["ref"].startswith("v:") and s.related("Sam")[0]["ref"].startswith("e:")
    assert s.get(e["ref"])["name"] == "Sam" and s.get(e["facts"][0]["ref"])["text"] == "likes tea" and s.get(e["events"][0]["ref"])["with"]
    assert s.get(e["relations"][0]["ref"])["rel"] == "classmate_of"

def test_propose_tells_you_what_it_attaches_to_and_applies_by_grant(env):
    s = _fresh(env); s.resolve("person:Sam")
    ops = [{"op": "event", "title": "Finished OS lab", "when": "2026-10-14", "people": ["Sam"], "projects": ["OSLab"]},
           {"op": "fact", "subject": "person:Sam", "text": "owns a zebra-striped bike"}]
    pv = changes.submit(s, ops, W, dry_run=True)
    assert pv["status"] == "preview" and s.stats()["events"] == 0                                           # dry run: nothing written
    st = {t["as"]: t["status"] for t in pv["targets"]}; assert st == {"person:Sam": "existing", "project:OSLab": "new"}
    r = changes.submit(s, ops, W); assert r["status"] == "applied" and r["changeset"].startswith("c:") and s.search("zebra")
    assert changes.submit(s, ops, W)["status"] == "unchanged"                                               # resending is harmless
    rd = changes.submit(s, [{"op": "forget", "ref": "f:1"}], W); assert rd["status"] == "denied" and "manage" in rd["message"]
    assert changes.submit(s, ops, {**W, "level": "read"})["status"] == "denied"
    s.resolve("course:Sam")                                                                                  # now "Sam" is ambiguous
    bad = changes.submit(s, [{"op": "fact", "subject": "Sam", "text": "x"}], W); assert bad["status"] == "invalid" and "matches 2" in bad["errors"][0]
    assert changes.submit(s, [{"op": "nope"}], W)["status"] == "invalid"

def test_all_or_nothing(env):
    s = _fresh(env)
    r = changes.submit(s, [{"op": "fact", "subject": "person:Sam", "text": "a"}, {"op": "forget", "ref": "f:99"}], M)
    assert r["status"] == "invalid" and s.stats()["entities"] == 0 and s.stats()["facts"] == 0                # no half-applied change

def test_ask_mode_holds_then_approve_or_reject(env):
    s = _fresh(env); ask = {**W, "ask_adds": True}
    r = changes.submit(s, [{"op": "fact", "subject": "person:Sam", "text": "likes tea"}], ask)
    assert r["status"] == "awaiting_approval" and s.stats()["facts"] == 0 and changes.awaiting(s) == 1
    assert changes.listing(s, "pending", full=True)[0]["graph"]["nodes"]                                      # the review page draws this
    assert changes.approve(s, r["changeset"])["status"] == "applied" and s.stats()["facts"] == 1 and changes.awaiting(s) == 0
    r2 = changes.submit(s, [{"op": "fact", "subject": "person:Sam", "text": "likes coffee"}], ask)
    changes.reject(s, r2["changeset"]); assert s.stats()["facts"] == 1 and changes.show(s, r2["changeset"])["status"] == "rejected"
    r3 = changes.submit(s, [{"op": "fact", "subject": "person:Sam", "text": "likes cake"}], ask)
    assert changes.undo(s, r3["changeset"], ask)["status"] == "withdrawn"                                     # an agent can withdraw its own

def test_manage_deletes_wait_and_undo_restores_everything(env):
    s = _fresh(env)
    changes.submit(s, [{"op": "event", "title": "Finished OS lab", "when": "2026-10-14", "people": ["Sam"], "projects": ["OSLab"]},
                       {"op": "event", "title": "Solo walk", "when": "2026-10-15", "people": ["Sam"]},
                       {"op": "fact", "subject": "person:Sam", "text": "owns a zebra-striped bike"}, {"op": "link", "src": "person:Sam", "rel": "classmate_of", "dst": "me"}], W)
    e = s.entity("Sam"); fact = e["facts"][0]
    held = changes.submit(s, [{"op": "forget", "ref": fact["ref"]}], M)
    assert held["status"] == "awaiting_approval" and s.search("zebra")                                         # nothing deleted yet
    changes.approve(s, held["changeset"]); assert not s.search("zebra") and len(s.entity("Sam")["facts"]) == 0   # and it is gone from search, not just the entity
    assert changes.undo(s, held["changeset"], M)["status"] == "undone" and s.search("zebra")
    auto = {**M, "auto_destructive": True}
    pv = changes.submit(s, [{"op": "forget", "ref": e["ref"]}], auto, dry_run=True)
    assert any("Solo walk" in x for x in pv["summary"])                                                      # orphaned events are named in the preview
    r = changes.submit(s, [{"op": "forget", "ref": e["ref"]}], auto); assert r["status"] == "applied" and s.entity("Sam") is None and s.timeline(limit=5)
    changes.undo(s, r["changeset"], auto)
    e2 = s.entity("Sam"); assert len(e2["facts"]) == 1 and len(e2["relations"]) == 1 and len(e2["events"]) == 2 and e2["ref"] == e["ref"]
    with pytest.raises(ValueError): changes.undo(s, r["changeset"], auto)                                      # not twice
    assert changes.submit(s, [{"op": "forget", "ref": f"e:{s.resolve('me')}"}], auto)["status"] == "invalid"   # you can't delete yourself

def test_undo_of_an_add_and_edit_fact(env):
    s = _fresh(env); s.resolve("person:Sam")
    r = changes.submit(s, [{"op": "event", "title": "Met Alex", "people": ["Alex"]}, {"op": "fact", "subject": "person:Alex", "text": "plays chess"}], W)
    assert s.entity("Alex"); changes.undo(s, r["changeset"], W); assert s.entity("Alex") is None and s.stats()["events"] == 0
    changes.submit(s, [{"op": "fact", "subject": "person:Sam", "text": "lives in Paris"}], W)
    fid = s.entity("Sam")["facts"][0]["ref"]
    ed = changes.submit(s, [{"op": "edit_fact", "ref": fid, "text": "lives in Lyon"}], {**M, "auto_destructive": True})
    assert [f["text"] for f in s.entity("Sam")["facts"]] == ["lives in Lyon"] and not s.search("Paris")
    changes.undo(s, ed["changeset"], M); assert [f["text"] for f in s.entity("Sam")["facts"]] == ["lives in Paris"] and s.search("Paris")
    w = changes.submit(s, [{"op": "fact", "subject": "person:Sam", "text": "x"}], W)
    with pytest.raises(ValueError): changes.undo(s, w["changeset"], {**W, "name": "other-agent"})            # agents undo only their own
    assert changes.purge_undo(s) >= 1
    with pytest.raises(ValueError): changes.undo(s, w["changeset"], W)                                         # purged: really gone

def test_agent_grants_carry_a_level(env):
    s = _fresh(env)
    t = s.grant("cleaner", level="manage", auto_destructive=True)
    assert s.grant_info(t) == {"name": "cleaner", "level": "manage", "ask_adds": False, "auto_destructive": True}
    s.revoke("cleaner"); assert s.grant_info(t) is None
    with pytest.raises(ValueError): s.grant("x", level="root")

def test_meaning_search_and_hybrid(env, monkeypatch):
    monkeypatch.setenv("LIFEGRAPH_EMBED", "lite")
    from lifegraph import embed
    s = _fresh(env); emb, _ = embed.get()
    s.log_event("Debugged the scheduler deadlock", when="2026-10-01", body="mutex ordering bug in the kernel", entities=["project:Kernel"])
    s.log_event("Went climbing", when="2026-10-02", entities=["person:Sam"]); s.remember("person:Sam", "prefers vegetarian food")
    assert s.index_status()["waiting"] >= 3 and s.embed_pending(emb) >= 3 and s.index_status()["waiting"] == 0
    hits = s.hybrid_search(emb, "schedulr deadlok")                               # typos: keyword search finds nothing, lite vectors do
    assert s.search("schedulr deadlok") == [] and hits[0]["name"] == "Debugged the scheduler deadlock" and hits[0]["found_by"] == "meaning"
    assert s.hybrid_search(emb, "climbing")[0]["found_by"] in ("both", "keyword")
    s.remember("person:Sam", "is allergic to peanuts"); assert s.index_status()["waiting"] == 1                     # edits re-queue
    ref = s.entity("Sam")["facts"][0]["ref"]; changes.submit(s, [{"op": "forget", "ref": ref}], {"name": "o", "level": "manage", "ask_adds": False, "auto_destructive": True})
    s.embed_pending(emb); assert not any("peanuts" in str(h) for h in s.hybrid_search(emb, "peanuts allergy", 5) if h["found_by"] != "meaning" and "peanuts" in h.get("snippet", ""))
    s.reindex("other-model"); assert s.index_status()["indexed"] == 0 and s.index_status()["waiting"] > 0         # new backend: everything re-embedded

def test_ids_are_never_reused_and_old_stores_restore_under_a_new_id(env):
    s = _fresh(env); M2 = {"name": "o", "level": "manage", "ask_adds": False, "auto_destructive": True}
    changes.submit(s, [{"op": "fact", "subject": "person:Sam", "text": "a"}, {"op": "fact", "subject": "person:Sam", "text": "b"}], M2)
    b = s.entity("Sam")["facts"][0]["ref"]; d = changes.submit(s, [{"op": "forget", "ref": b}], M2)
    changes.submit(s, [{"op": "fact", "subject": "person:Sam", "text": "c"}], M2)
    assert b not in [f["ref"] for f in s.entity("Sam")["facts"]]                  # the deleted ref is not handed to something new
    assert changes.undo(s, d["changeset"], M2)["status"] == "undone" and sorted(f["text"] for f in s.entity("Sam")["facts"]) == ["a", "b", "c"]
    # a store from before AUTOINCREMENT can reuse ids: undo then restores under a fresh id instead of colliding
    snap = {"kind": "f", "id": s.entity("Sam")["facts"][0]["ref"][2:] and int(s.entity("Sam")["facts"][0]["ref"][2:]), "row": [s.resolve("person:Sam"), "dup", 1.0, "t", None], "label": "x"}
    sk, moved = s.restore_snapshot(snap); assert moved and "→" in moved

def test_paths_become_files_and_folders_tied_to_the_work(env, monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    s = _fresh(env)
    r = changes.submit(s, [{"op": "event", "title": "Rewrote the scheduler", "when": "2026-10-01", "projects": ["Kernel"],
                            "paths": ["/home/someone/projects/kernel/", "/home/someone/projects/kernel/sched.c", "~/projects/kernel/sched.c", {"path": "Makefile", "kind": "file"}]}], W)
    assert r["status"] == "applied" and any("located_at" in x for x in r["summary"])                          # one project + one folder: linked automatically
    k = s.entity("project:Kernel"); assert [x["with"] for x in k["relations"]] == ["directory:~/projects/kernel"]
    ev = s.get(s.entity("project:Kernel")["events"][0]["ref"]); names = {w["entity"] for w in ev["with"]}
    assert {"directory:~/projects/kernel", "file:~/projects/kernel/sched.c", "file:Makefile"} <= names      # ~ names are stable, duplicates collapse
    assert s.entity("file:~/projects/kernel/sched.c")["events"][0]["title"] == "Rewrote the scheduler"      # "what did I do to this file?"
    assert not any(n["kind"] == "file" for n in s.graph_view()["nodes"])                                       # files stay out of the picture, but searchable
    assert s.search("sched.c")
    assert len(changes.path_entities({"paths": [f"/a/{i}" for i in range(40)]})) == changes.MAX_PATHS
