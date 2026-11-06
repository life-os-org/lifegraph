#![allow(dead_code)]
//! Data the UI shows, and the one graph shape the GPU draws (both the whole graph and a change's before/after diff).
use serde::Deserialize;
use serde_json::Value;

#[derive(Clone, Debug, Default)]
pub struct GNode {
    pub key: String,           // "e:12", "v:7"
    pub kind: String,
    pub name: String,
    pub state: u8,             // 0 existing, 1 new, 2 removed
    pub size: f32,
    pub adds: Vec<String>,
    pub removes: Vec<String>,
}

#[derive(Clone, Debug, Default)]
pub struct GLink {
    pub a: usize,
    pub b: usize,
    pub rel: String,
    pub state: u8,
    pub w: f32,
}

#[derive(Clone, Debug, Default)]
pub struct GraphData {
    pub nodes: Vec<GNode>,
    pub links: Vec<GLink>,
}

impl GraphData {
    pub fn degree(&self) -> Vec<u32> {
        let mut d = vec![0u32; self.nodes.len()];
        for l in &self.links {
            d[l.a] += 1;
            d[l.b] += 1;
        }
        d
    }
    pub fn index_of(&self, key: &str) -> Option<usize> {
        self.nodes.iter().position(|n| n.key == key)
    }
}

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}
fn arr<'a>(v: &'a Value, k: &str) -> &'a [Value] {
    v.get(k).and_then(|x| x.as_array()).map(|a| a.as_slice()).unwrap_or(&[])
}
fn state_of(x: &str) -> u8 {
    match x {
        "new" => 1,
        "removed" => 2,
        _ => 0,
    }
}

/// `/api/graph`: {nodes:[{id,kind,name,events,facts}], links:[{s,t,rel,w}]}
pub fn graph_from_explore(v: &Value) -> GraphData {
    let mut g = GraphData::default();
    let mut ix = std::collections::HashMap::new();
    for n in arr(v, "nodes") {
        let id = n.get("id").and_then(|x| x.as_i64()).unwrap_or(0);
        ix.insert(id, g.nodes.len());
        let sz = n.get("events").and_then(|x| x.as_f64()).unwrap_or(0.0) + n.get("facts").and_then(|x| x.as_f64()).unwrap_or(0.0);
        g.nodes.push(GNode { key: format!("e:{id}"), kind: s(n, "kind"), name: s(n, "name"), size: sz as f32, ..Default::default() });
    }
    for l in arr(v, "links") {
        let (a, b) = (l.get("s").and_then(|x| x.as_i64()), l.get("t").and_then(|x| x.as_i64()));
        if let (Some(a), Some(b)) = (a, b) {
            if let (Some(&ia), Some(&ib)) = (ix.get(&a), ix.get(&b)) {
                g.links.push(GLink { a: ia, b: ib, rel: s(l, "rel"), state: 0, w: l.get("w").and_then(|x| x.as_f64()).unwrap_or(1.0) as f32 });
            }
        }
    }
    g
}

/// A change's `graph`: {nodes:[{id,kind,name,state,adds,removes}], links:[{s,t,rel,state}]}
pub fn graph_from_diff(v: &Value) -> GraphData {
    let mut g = GraphData::default();
    let mut ix = std::collections::HashMap::new();
    for n in arr(v, "nodes") {
        let key = s(n, "id");
        ix.insert(key.clone(), g.nodes.len());
        let strs = |k: &str| arr(n, k).iter().filter_map(|x| x.as_str().map(String::from)).collect::<Vec<_>>();
        g.nodes.push(GNode { key, kind: s(n, "kind"), name: s(n, "name"), state: state_of(&s(n, "state")), size: 2.0, adds: strs("adds"), removes: strs("removes") });
    }
    for l in arr(v, "links") {
        if let (Some(&a), Some(&b)) = (ix.get(&s(l, "s")), ix.get(&s(l, "t"))) {
            g.links.push(GLink { a, b, rel: s(l, "rel"), state: state_of(&s(l, "state")), w: 2.0 });
        }
    }
    g
}

#[derive(Clone, Debug, Deserialize, Default)]
pub struct Target {
    #[serde(rename = "as")]
    pub name: String,
    pub status: String,
    #[serde(rename = "ref")]
    pub r: Option<String>,
}

#[derive(Clone, Debug, Default)]
pub struct Change {
    pub id: String,
    pub agent: String,
    pub source: String,
    pub when: String,
    pub status: String,
    pub note: String,
    pub summary: Vec<String>,
    pub targets: Vec<Target>,
    pub graph: GraphData,
}

pub fn change_from(v: &Value) -> Change {
    Change {
        id: s(v, "changeset"),
        agent: s(v, "agent"),
        source: s(v, "source"),
        when: s(v, "when"),
        status: s(v, "status"),
        note: s(v, "note"),
        summary: arr(v, "summary").iter().filter_map(|x| x.as_str().map(String::from)).collect(),
        targets: arr(v, "targets").iter().filter_map(|t| serde_json::from_value(t.clone()).ok()).collect(),
        graph: v.get("graph").map(graph_from_diff).unwrap_or_default(),
    }
}

#[derive(Clone, Debug, Default)]
pub struct Hit {
    pub r: String,
    pub kind: String,
    pub name: String,
    pub snippet: String,
    pub when: String,
    pub found_by: String,
}

pub fn hits_from(v: &Value) -> Vec<Hit> {
    v.as_array().map(|a| a.iter().map(|h| Hit {
        r: s(h, "ref"), kind: s(h, "kind"), name: s(h, "name"), snippet: s(h, "snippet"), when: s(h, "when"), found_by: s(h, "found_by"),
    }).collect()).unwrap_or_default()
}

#[derive(Clone, Debug, Default)]
pub struct Detail {
    pub r: String,
    pub kind: String,
    pub name: String,
    pub first_seen: String,
    pub aliases: Vec<String>,
    pub facts: Vec<(String, String)>,                    // ref, text
    pub relations: Vec<(String, String, String, String)>, // ref, rel, direction, with
    pub events: Vec<(String, String, String)>,           // ref, when, title
    pub body: String,                                    // for an event
    pub is_event: bool,
    pub error: String,
}

pub fn detail_from(v: &Value) -> Detail {
    let mut d = Detail { r: s(v, "ref"), kind: s(v, "kind"), ..Default::default() };
    if let Some(e) = v.get("error") {
        d.error = e.as_str().unwrap_or("not found").to_string();
        return d;
    }
    if v.get("title").is_some() {        // an event
        d.is_event = true;
        d.name = s(v, "title");
        d.first_seen = s(v, "when");
        d.body = s(v, "body");
        d.events = arr(v, "with").iter().map(|w| (s(w, "ref"), s(w, "role"), s(w, "entity"))).collect();
        return d;
    }
    d.name = s(v, "name");
    d.first_seen = s(v, "first_seen");
    d.aliases = arr(v, "aliases").iter().filter_map(|x| x.as_str().map(String::from)).collect();
    d.facts = arr(v, "facts").iter().map(|f| (s(f, "ref"), s(f, "text"))).collect();
    d.relations = arr(v, "relations").iter().map(|r| (s(r, "ref"), s(r, "rel"), s(r, "direction"), s(r, "with"))).collect();
    d.events = arr(v, "events").iter().map(|e| (s(e, "ref"), s(e, "when"), s(e, "title"))).collect();
    d
}

#[derive(Clone, Debug, Default)]
pub struct Stats {
    pub entities: u64,
    pub events: u64,
    pub facts: u64,
    pub edges: u64,
    pub indexed: u64,
    pub waiting: u64,
    pub backend: String,
}

pub fn stats_from(v: &Value) -> Stats {
    let n = |k: &str| v.get(k).and_then(|x| x.as_u64()).unwrap_or(0);
    let idx = v.get("search_index");
    Stats {
        entities: n("entities"), events: n("events"), facts: n("facts"), edges: n("edges"),
        indexed: idx.and_then(|i| i.get("indexed")).and_then(|x| x.as_u64()).unwrap_or(0),
        waiting: idx.and_then(|i| i.get("waiting")).and_then(|x| x.as_u64()).unwrap_or(0),
        backend: idx.map(|i| s(i, "backend")).unwrap_or_default(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn explore_graph_links_by_id_and_drops_dangling() {
        let v = json!({"nodes": [{"id": 1, "kind": "person", "name": "Me", "events": 2, "facts": 1}, {"id": 5, "kind": "project", "name": "Kernel", "events": 0, "facts": 0}],
                       "links": [{"s": 1, "t": 5, "rel": "owned_by", "w": 3}, {"s": 1, "t": 99, "rel": "ghost", "w": 1}]});
        let g = graph_from_explore(&v);
        assert_eq!(g.nodes.len(), 2);
        assert_eq!((g.links.len(), g.links[0].a, g.links[0].b), (1, 0, 1));
        assert_eq!(g.nodes[0].size, 3.0);
        assert_eq!(g.degree(), vec![1, 1]);
        assert_eq!(g.index_of("e:5"), Some(1));
    }

    #[test]
    fn diff_graph_keeps_states_and_text() {
        let v = json!({"nodes": [{"id": "e:1", "kind": "person", "name": "Sam", "state": "existing", "adds": ["likes tea"]},
                                  {"id": "v:2", "kind": "event", "name": "Met", "state": "new"}, {"id": "e:3", "kind": "person", "name": "Old", "state": "removed", "removes": ["x"]}],
                       "links": [{"s": "v:2", "t": "e:1", "rel": "involves", "state": "new"}, {"s": "e:1", "t": "e:3", "rel": "knows", "state": "removed"}]});
        let g = graph_from_diff(&v);
        assert_eq!(g.nodes.iter().map(|n| n.state).collect::<Vec<_>>(), vec![0, 1, 2]);
        assert_eq!(g.nodes[0].adds, vec!["likes tea"]);
        assert_eq!(g.links.iter().map(|l| l.state).collect::<Vec<_>>(), vec![1, 2]);
    }

    #[test]
    fn detail_reads_entities_events_and_errors() {
        let e = detail_from(&json!({"ref": "e:2", "kind": "person", "name": "Sam", "first_seen": "t", "aliases": [], "facts": [{"ref": "f:1", "text": "a"}],
                                    "relations": [{"ref": "g:1", "rel": "r", "direction": "out", "with": "project:K"}], "events": [{"ref": "v:1", "when": "d", "title": "T"}]}));
        assert!(!e.is_event && e.facts.len() == 1 && e.relations[0].3 == "project:K");
        let v = detail_from(&json!({"ref": "v:1", "title": "Met", "when": "d", "body": "b", "with": [{"ref": "e:1", "entity": "person:Sam", "role": "involves"}]}));
        assert!(v.is_event && v.name == "Met");
        assert_eq!(detail_from(&json!({"error": "nothing found"})).error, "nothing found");
    }
}
