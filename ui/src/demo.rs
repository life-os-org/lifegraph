#![allow(dead_code)]
//! Made-up data for `--demo` (screenshots, trying the UI without a daemon). Nothing here is real.
use serde_json::{json, Value};
use std::collections::HashSet;

struct Ent { id: i64, kind: &'static str, name: String, facts: Vec<(i64, String)>, }
struct Ev { id: i64, when: String, title: String, kind: &'static str, with: Vec<i64>, }

pub struct Demo {
    ents: Vec<Ent>,
    links: Vec<(i64, i64, &'static str, i64)>, // a, b, rel, ref id
    events: Vec<Ev>,
    next: i64,
    pending: Vec<Value>,
    applied: Vec<Value>,
}

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 { self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407); self.0 >> 33 }
    fn below(&mut self, n: usize) -> usize { (self.next() % n as u64) as usize }
}

impl Demo {
    pub fn new() -> Demo {
        let mut r = Rng(7);
        let mut ents: Vec<Ent> = vec![Ent { id: 1, kind: "person", name: "Me".into(), facts: vec![] }];
        let people = ["Sam", "Priya", "Jordan", "Alex", "Mina", "Theo", "Ravi", "Noor", "Lena", "Omar"];
        let projects = ["Kernel", "Website redesign", "Compiler", "Robotics arm", "Photo archive", "Budget tool", "Mesh network", "Capstone"];
        let topics = ["Scheduling", "Algorithms", "Rust", "Lock ordering", "Embeddings", "Typography", "Control theory", "Networking", "Testing", "Caching",
                      "Graph layout", "Shaders", "Encryption", "Parsing", "Sensors", "Calibration", "Databases", "Tracing", "Packaging", "Accessibility"];
        let others = [("course", "Operating Systems"), ("course", "Linear Algebra"), ("course", "Signals"), ("place", "Library"), ("place", "Lab 204"),
                      ("org", "Acme University"), ("org", "Open Source Guild"), ("repo", "kernel-lab"), ("repo", "site-v2"), ("directory", "~/projects/kernel")];
        let mut id = 2;
        for p in people { ents.push(Ent { id, kind: "person", name: p.into(), facts: vec![] }); id += 1; }
        for p in projects { ents.push(Ent { id, kind: "project", name: p.into(), facts: vec![] }); id += 1; }
        for t in topics { ents.push(Ent { id, kind: "topic", name: t.into(), facts: vec![] }); id += 1; }
        for (k, n) in others { ents.push(Ent { id, kind: k, name: n.into(), facts: vec![] }); id += 1; }
        let n = ents.len();
        let by_kind = |k: &str| ents.iter().filter(|e| e.kind == k).map(|e| e.id).collect::<Vec<_>>();
        let (pe, pr, to) = (by_kind("person"), by_kind("project"), by_kind("topic"));
        let mut links = vec![];
        let mut lid = 1000;
        for &p in &pe { if p != 1 { links.push((p, 1, "friend_of", lid)); lid += 1; } }
        for (i, &p) in pr.iter().enumerate() {
            links.push((p, 1, "owned_by", lid)); lid += 1;
            for _ in 0..2 { links.push((pe[1 + r.below(pe.len() - 1)], p, "teammate_on", lid)); lid += 1; }
            for _ in 0..3 { links.push((p, to[(i * 2 + r.below(6)) % to.len()], "uses", lid)); lid += 1; }
        }
        let facts_pool = ["prefers morning meetings", "is vegetarian", "leads the test suite", "joined in September", "owns the build server", "likes concise reviews",
                          "is allergic to peanuts", "writes the weekly summary", "has the spare key", "moved to the east campus"];
        for e in ents.iter_mut() {
            if e.kind == "person" || e.kind == "project" {
                for _ in 0..(1 + r.below(3)) { lid += 1; e.facts.push((lid, facts_pool[r.below(facts_pool.len())].to_string())); }
            }
        }
        let titles = ["Debugged a deadlock", "Reviewed the design", "Finished the problem set", "Kicked off the sprint", "Shipped the release", "Study group",
                      "Decided to use static pages", "Benchmarked the cache", "Planned the demo", "Fixed flaky tests", "Wrote the README", "Paired on the parser"];
        let mut events = vec![];
        for i in 0..42 {
            let mut with = vec![pr[r.below(pr.len())], pe[1 + r.below(pe.len() - 1)]];
            with.push(to[r.below(to.len())]);
            events.push(Ev { id: 100 + i, when: format!("2026-{:02}-{:02}", 9 + (i / 16), 1 + (i * 2) % 28), title: titles[r.below(titles.len())].to_string(), kind: "event", with });
        }
        let _ = n;
        let mut d = Demo { ents, links, events, next: 5000, pending: vec![], applied: vec![] };
        d.seed_changes();
        d
    }

    fn name(&self, id: i64) -> String { self.ents.iter().find(|e| e.id == id).map(|e| format!("{}:{}", e.kind, e.name)).unwrap_or_default() }

    fn seed_changes(&mut self) {
        let sam = self.ents.iter().find(|e| e.name == "Sam").unwrap().id;
        let kernel = self.ents.iter().find(|e| e.name == "Kernel").unwrap().id;
        let priya = self.ents.iter().find(|e| e.name == "Priya").unwrap().id;
        let fact = self.ents.iter().find(|e| e.id == priya).and_then(|e| e.facts.first().cloned()).unwrap();
        let node = |id: String, kind: &str, name: &str, state: &str, adds: Vec<&str>, removes: Vec<&str>| json!({"id": id, "kind": kind, "name": name, "state": state, "adds": adds, "removes": removes});
        self.pending.push(json!({
            "changeset": "c:7", "agent": "agent-codex", "source": "codex@kernel", "when": "2026-11-05 22:08", "status": "pending", "note": null,
            "summary": ["Add event “Reviewed the scheduler design with Sam” on 2026-11-04 involving Sam, Kernel",
                        "Add fact to person:Sam: “now works on the test harness”", "Add link person:Alex —mentor_of→ person:Sam",
                        format!("Delete fact about person:Priya: {}", fact.1)],
            "targets": [{"as": "person:Sam", "ref": format!("e:{sam}"), "status": "existing"}, {"as": "project:Kernel", "ref": format!("e:{kernel}"), "status": "existing"}, {"as": "person:Alex", "ref": null, "status": "new"}],
            "graph": {"nodes": [node(format!("e:{sam}"), "person", "Sam", "existing", vec!["now works on the test harness"], vec![]),
                                 node(format!("e:{kernel}"), "project", "Kernel", "existing", vec![], vec![]),
                                 node("v:900".into(), "event", "Reviewed the scheduler design with Sam", "new", vec![], vec![]),
                                 node("e:901".into(), "person", "Alex", "new", vec![], vec![]),
                                 node(format!("e:{priya}"), "person", "Priya", "existing", vec![], vec![&fact.1])],
                      "links": [{"s": "v:900", "t": format!("e:{sam}"), "rel": "involves", "state": "new"}, {"s": "v:900", "t": format!("e:{kernel}"), "rel": "involves", "state": "new"},
                                {"s": "e:901", "t": format!("e:{sam}"), "rel": "mentor_of", "state": "new"}]}
        }));
        self.applied.push(json!({"changeset": "c:6", "agent": "agent-claude", "source": "claude-code@site-v2", "when": "2026-11-05 21:40", "status": "applied", "note": null,
            "summary": ["Add event “Decided to use static pages” on 2026-10-14", "Link project:Website redesign —located_at→ directory:~/projects/site-v2"], "targets": [], "graph": {"nodes": [], "links": []}}));
    }

    pub fn stats(&self) -> Value { json!({"entities": self.ents.len(), "edges": self.links.len(), "events": self.events.len(), "facts": self.ents.iter().map(|e| e.facts.len()).sum::<usize>(),
        "search_index": {"indexed": self.ents.len() + self.events.len(), "waiting": 0, "backend": "fastembed:bge-small-en-v1.5", "note": ""}}) }

    pub fn graph(&self) -> Value {
        let mut deg = std::collections::HashMap::new();
        for e in &self.events { for &w in &e.with { *deg.entry(w).or_insert(0) += 1; } }
        let nodes: Vec<Value> = self.ents.iter().map(|e| json!({"id": e.id, "kind": e.kind, "name": e.name, "events": deg.get(&e.id).copied().unwrap_or(0), "facts": e.facts.len()})).collect();
        let mut seen: HashSet<(i64, i64)> = HashSet::new();
        let mut links: Vec<Value> = vec![];
        for &(a, b, rel, _) in &self.links { if seen.insert((a.min(b), a.max(b))) { links.push(json!({"s": a, "t": b, "rel": rel, "w": 3})); } }
        for e in &self.events { for i in 0..e.with.len() { for j in i + 1..e.with.len() { let (a, b) = (e.with[i].min(e.with[j]), e.with[i].max(e.with[j])); if seen.insert((a, b)) { links.push(json!({"s": a, "t": b, "rel": "together", "w": 1})); } } } }
        json!({"nodes": nodes, "links": links})
    }

    pub fn changes(&self) -> Value { json!({"pending": self.pending, "recent": self.applied}) }

    pub fn search(&self, q: &str) -> Value {
        let q = q.to_lowercase();
        let mut out: Vec<Value> = vec![];
        for e in &self.ents { if e.name.to_lowercase().contains(&q) || e.facts.iter().any(|f| f.1.to_lowercase().contains(&q)) { out.push(json!({"ref": format!("e:{}", e.id), "kind": e.kind, "name": e.name, "snippet": "", "found_by": "keyword"})); } }
        for e in &self.events { if e.title.to_lowercase().contains(&q) { out.push(json!({"ref": format!("v:{}", e.id), "kind": "event", "name": e.title, "snippet": "", "when": e.when, "found_by": "both"})); } }
        if out.is_empty() && !q.is_empty() { if let Some(e) = self.ents.iter().find(|e| e.name == "Scheduling") { out.push(json!({"ref": format!("e:{}", e.id), "kind": e.kind, "name": e.name, "snippet": "", "found_by": "meaning"})); } }
        out.truncate(20);
        json!(out)
    }

    pub fn get(&self, r: &str) -> Value {
        let id: i64 = r.split(':').nth(1).and_then(|x| x.parse().ok()).unwrap_or(-1);
        if r.starts_with("v:") {
            return match self.events.iter().find(|e| e.id == id) {
                Some(e) => json!({"ref": r, "when": e.when, "title": e.title, "body": "Details of this made-up event.", "kind": e.kind, "with": e.with.iter().map(|w| json!({"ref": format!("e:{w}"), "entity": self.name(*w), "role": "involves"})).collect::<Vec<_>>()}),
                None => json!({"error": "not found"}),
            };
        }
        let e = if r.starts_with("e:") { self.ents.iter().find(|e| e.id == id) } else { self.ents.iter().find(|e| r.ends_with(&e.name)) };
        match e {
            None => json!({"error": "not found"}),
            Some(e) => json!({"id": e.id, "ref": format!("e:{}", e.id), "kind": e.kind, "name": e.name, "props": {}, "first_seen": "2026-09-02 10:00", "aliases": [],
                "facts": e.facts.iter().map(|f| json!({"ref": format!("f:{}", f.0), "text": f.1, "when": "2026-10-01 09:00"})).collect::<Vec<_>>(),
                "relations": self.links.iter().filter(|l| l.0 == e.id || l.1 == e.id).map(|l| { let (o, d) = if l.0 == e.id { (l.1, "out") } else { (l.0, "in") }; json!({"ref": format!("g:{}", l.3), "rel": l.2, "direction": d, "with": self.name(o)}) }).collect::<Vec<_>>(),
                "events": self.events.iter().filter(|v| v.with.contains(&e.id)).take(10).map(|v| json!({"ref": format!("v:{}", v.id), "when": v.when, "title": v.title, "kind": "event"})).collect::<Vec<_>>()}),
        }
    }

    pub fn decide(&mut self, id: &str, action: &str) -> Value {
        if let Some(i) = self.pending.iter().position(|c| c["changeset"] == id) {
            let mut c = self.pending.remove(i);
            c["status"] = json!(if action == "approve" { "applied" } else { "rejected" });
            self.applied.insert(0, c);
        }
        json!({"ok": true, "data": {"status": "applied"}})
    }

    pub fn apply(&mut self, ops: &[Value]) -> Value {
        self.next += 1;
        for op in ops {
            let r = op["ref"].as_str().unwrap_or("");
            let id: i64 = r.split(':').nth(1).and_then(|x| x.parse().ok()).unwrap_or(-1);
            match op["op"].as_str() {
                Some("forget") if r.starts_with("f:") => for e in self.ents.iter_mut() { e.facts.retain(|f| f.0 != id) },
                Some("forget") if r.starts_with("g:") => self.links.retain(|l| l.3 != id),
                Some("forget") if r.starts_with("v:") => self.events.retain(|v| v.id != id),
                Some("fact") => { let s = op["subject"].as_str().unwrap_or("").to_string(); let sid: i64 = s.split(':').nth(1).and_then(|x| x.parse().ok()).unwrap_or(-1); let n = self.next; if let Some(e) = self.ents.iter_mut().find(|e| e.id == sid) { e.facts.push((n + 10, op["text"].as_str().unwrap_or("").to_string())); } }
                _ => {}
            }
        }
        json!({"status": "applied", "changeset": format!("c:{}", self.next)})
    }
}
