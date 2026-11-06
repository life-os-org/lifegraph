//! Talks to `life ui`'s local server (127.0.0.1, secret path) on a worker thread, so the UI never blocks on the network.
//! `--demo` swaps in made-up data and needs no daemon.
use serde_json::{json, Value};
use std::sync::mpsc::{channel, Receiver, Sender};

pub enum Req {
    Stats,
    Graph,
    Changes,
    Search(String),
    Get(String),
    Decide { id: String, action: String },
    Apply { ops: Vec<Value>, label: String },
}

pub enum Resp {
    Stats(Value),
    Graph(Value),
    Changes(Value),
    Search(String, Value),
    Get(Value),
    Decided(String, Value),
    Applied(String, Value),
    Error(String),
}

pub struct Api {
    pub tx: Sender<Req>,
    pub rx: Receiver<Resp>,
}

fn enc(s: &str) -> String {
    let mut o = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) {
            o.push(b as char);
        } else {
            o.push_str(&format!("%{b:02X}"));
        }
    }
    o
}

impl Api {
    pub fn start(base: Option<String>, ctx: eframe::egui::Context) -> Api {
        let (tx, wrx) = channel::<Req>();
        let (wtx, rx) = channel::<Resp>();
        std::thread::spawn(move || {
            let mut demo = crate::demo::Demo::new();
            let agent = ureq::Agent::config_builder().timeout_global(Some(std::time::Duration::from_secs(60))).build().new_agent();
            let get = |path: &str| -> Result<Value, String> {
                let url = format!("{}{}", base.as_deref().unwrap_or(""), path);
                agent.get(&url).call().map_err(|e| e.to_string())?.body_mut().read_json::<Value>().map_err(|e| e.to_string())
            };
            let post = |path: &str, body: Value| -> Result<Value, String> {
                let url = format!("{}{}", base.as_deref().unwrap_or(""), path);
                agent.post(&url).send_json(&body).map_err(|e| e.to_string())?.body_mut().read_json::<Value>().map_err(|e| e.to_string())
            };
            while let Ok(req) = wrx.recv() {
                let live = base.is_some();
                let resp = match req {
                    Req::Stats => if live { get("api/stats").map(Resp::Stats) } else { Ok(Resp::Stats(demo.stats())) },
                    Req::Graph => if live { get("api/graph").map(Resp::Graph) } else { Ok(Resp::Graph(demo.graph())) },
                    Req::Changes => if live { get("api/changes").map(Resp::Changes) } else { Ok(Resp::Changes(demo.changes())) },
                    Req::Search(q) => if live { get(&format!("api/search?q={}", enc(&q))).map(|v| Resp::Search(q, v)) } else { Ok(Resp::Search(q.clone(), demo.search(&q))) },
                    Req::Get(r) => if live { get(&format!("api/get?ref={}", enc(&r))).map(Resp::Get) } else { Ok(Resp::Get(demo.get(&r))) },
                    Req::Decide { id, action } => if live {
                        post("api/decide", json!({"id": id, "action": action})).map(|v| Resp::Decided(action, v))
                    } else { Ok(Resp::Decided(action.clone(), demo.decide(&id, &action))) },
                    Req::Apply { ops, label } => if live {
                        post("api/apply", json!({"ops": ops})).map(|v| Resp::Applied(label, v))
                    } else { Ok(Resp::Applied(label, demo.apply(&ops))) },
                };
                let _ = wtx.send(resp.unwrap_or_else(Resp::Error));
                ctx.request_repaint();
            }
        });
        Api { tx, rx }
    }
    pub fn send(&self, r: Req) {
        let _ = self.tx.send(r);
    }
}
