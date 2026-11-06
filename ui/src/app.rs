use crate::api::{Api, Req, Resp};
use crate::gpu::{Gpu, GraphCallback, Uniforms};
use crate::model::*;
use crate::theme::{self, ALERT, BG, DIM, FAINT, INK, LINE, LINE_HI, PANEL, WHITE};
use eframe::egui::{self, Align2, Color32, FontId, Pos2, Rect, RichText, Sense, Stroke, Vec2};
use eframe::egui_wgpu;
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use std::time::Instant;

pub struct Opts {
    pub url: Option<String>,
    pub demo: bool,
    pub shot: Option<String>,
    pub shot_at: f32,
    pub mode: Option<String>,
    pub select: Option<String>,
    pub query: Option<String>,
    pub skip_boot: bool,
}

#[derive(PartialEq, Clone, Copy)]
enum Mode { Graph, Review, History }

#[derive(Clone)]
struct Cam { center: [f32; 2], zoom: f32, auto_fit: bool }

struct View {
    slot: u8,
    cam: Cam,
    positions: Arc<Mutex<Vec<[f32; 2]>>>,
    alpha: f32,
    decay: f32,
    hover: i32,
    pin: i32,
    version: u64,
    data: Arc<GraphData>,
}

impl View {
    fn new(slot: u8) -> View {
        View { slot, cam: Cam { center: [0.0, 0.0], zoom: 0.9, auto_fit: true }, positions: Arc::new(Mutex::new(vec![])), alpha: 1.0, decay: if slot == 1 { 0.94 } else { 0.988 }, hover: -1, pin: -1, version: 0, data: Arc::new(GraphData::default()) }
    }
    fn set(&mut self, d: GraphData) {
        self.version += 1;
        self.data = Arc::new(d);
        self.alpha = 1.0;
        self.cam.auto_fit = true;
        self.hover = -1;
    }
}

struct Boot {
    t0: Instant,
    done_at: Option<Instant>,
    stats: bool,
    graph: bool,
    changes: bool,
    lines_shown: usize,
    last_line: f32,
}

pub struct App {
    api: Api,
    opts: Opts,
    t0: Instant,
    boot: Boot,
    adapter: String,
    mode: Mode,
    stats: Stats,
    ex: View,
    diff: View,
    selected: i32,
    pending: Vec<Change>,
    recent: Vec<Change>,
    rsel: usize,
    query: String,
    last_query: String,
    hits: Vec<Hit>,
    detail: Option<Detail>,
    cur_ref: String,
    editing: Option<(String, String)>,
    new_fact: String,
    merge_to: String,
    merging: bool,
    confirm_del: bool,
    toast: Option<(String, Option<String>, Instant)>,
    shot_sent: bool,
    started_frames: u32,
    left_w: f32,
    right_w: f32,
    last_poll: Instant,
}

impl App {
    pub fn new(cc: &eframe::CreationContext<'_>, opts: Opts) -> App {
        theme::install(&cc.egui_ctx);
        let mut adapter = String::from("GPU");
        if let Some(rs) = cc.wgpu_render_state.as_ref() {
            adapter = rs.adapter.get_info().name;
            rs.renderer.write().callback_resources.insert(Gpu::new(&rs.device, rs.target_format));
        }
        let api = Api::start(opts.url.clone(), cc.egui_ctx.clone());
        api.send(Req::Stats);
        api.send(Req::Graph);
        api.send(Req::Changes);
        let q0 = opts.query.clone().unwrap_or_default();
        if !q0.is_empty() { api.send(Req::Search(q0.clone())); }
        let mode = match opts.mode.as_deref() { Some("review") => Mode::Review, Some("history") => Mode::History, _ => Mode::Graph };
        let now = Instant::now();
        App {
            api, t0: now, adapter,
            boot: Boot { t0: now, done_at: if opts.skip_boot { Some(now) } else { None }, stats: false, graph: false, changes: false, lines_shown: 0, last_line: 0.0 },
            opts, mode, stats: Stats::default(), ex: View::new(0), diff: View::new(1), selected: -1,
            pending: vec![], recent: vec![], rsel: 0, query: q0.clone(), last_query: q0, hits: vec![], detail: None, cur_ref: String::new(),
            editing: None, new_fact: String::new(), merge_to: String::new(), merging: false, confirm_del: false, toast: None, shot_sent: false, started_frames: 0, left_w: 300.0, right_w: 360.0, last_poll: now,
        }
    }

    // ------------------------------------------------------------------ data in
    fn pump(&mut self) {
        while let Ok(r) = self.api.rx.try_recv() {
            match r {
                Resp::Stats(v) => { self.stats = stats_from(&v); self.boot.stats = true; }
                Resp::Graph(v) => {
                    let g = graph_from_explore(&v);
                    let keep = self.cur_ref.clone();
                    self.ex.set(g);
                    self.boot.graph = true;
                    self.selected = self.ex.data.index_of(&keep).map(|i| i as i32).unwrap_or(-1);
                    if self.selected < 0 && self.cur_ref.is_empty() { self.apply_select_arg(); }
                }
                Resp::Changes(v) => {
                    self.pending = v["pending"].as_array().map(|a| a.iter().map(change_from).collect()).unwrap_or_default();
                    self.recent = v["recent"].as_array().map(|a| a.iter().map(change_from).collect()).unwrap_or_default();
                    self.boot.changes = true;
                    if self.rsel >= self.pending.len() { self.rsel = 0; }
                    self.load_diff();
                }
                Resp::Search(q, v) => { if q == self.query { self.hits = hits_from(&v); } }
                Resp::Get(v) => { self.detail = Some(detail_from(&v)); }
                Resp::Decided(_, _) => { self.api.send(Req::Changes); self.api.send(Req::Graph); self.api.send(Req::Stats); }
                Resp::Applied(label, v) => {
                    let st = v["status"].as_str().unwrap_or("");
                    if st == "applied" {
                        self.toast = Some((label, v["changeset"].as_str().map(String::from), Instant::now()));
                    } else if st == "unchanged" {
                        self.toast = Some(("ALREADY SO. NOTHING CHANGED".into(), None, Instant::now()));
                    } else {
                        let msg = v["errors"].as_array().and_then(|a| a.first()).and_then(|e| e.as_str()).or(v["message"].as_str()).unwrap_or("FAILED").to_string();
                        self.toast = Some((format!("REFUSED: {msg}"), None, Instant::now()));
                    }
                    self.editing = None; self.confirm_del = false; self.merging = false; self.new_fact.clear();
                    self.api.send(Req::Graph); self.api.send(Req::Stats); self.api.send(Req::Changes);
                    if !self.cur_ref.is_empty() { self.api.send(Req::Get(self.cur_ref.clone())); }
                }
                Resp::Error(e) => { self.toast = Some((format!("LINK ERROR: {e}"), None, Instant::now())); }
            }
        }
    }

    fn apply_select_arg(&mut self) {
        if let Some(name) = self.opts.select.clone() {
            if let Some(i) = self.ex.data.nodes.iter().position(|n| n.name.eq_ignore_ascii_case(&name)) {
                self.select_idx(i);
            }
        }
    }

    fn load_diff(&mut self) {
        let list = if self.mode == Mode::History { &self.recent } else { &self.pending };
        if let Some(c) = list.get(self.rsel) {
            let mut g = c.graph.clone();
            for n in g.nodes.iter_mut() { n.size = n.size.max(2.0); }
            self.diff.set(g);
        } else {
            self.diff.set(GraphData::default());
        }
    }

    fn select_idx(&mut self, i: usize) {
        self.selected = i as i32;
        let key = self.ex.data.nodes[i].key.clone();
        self.open_ref(&key);
    }

    fn open_ref(&mut self, r: &str) {
        self.cur_ref = r.to_string();
        self.detail = None;
        self.editing = None; self.merging = false; self.confirm_del = false;
        self.api.send(Req::Get(r.to_string()));
        if let Some(i) = self.ex.data.index_of(r) { self.selected = i as i32; }
    }

    fn apply(&mut self, ops: Vec<Value>, label: &str) {
        self.api.send(Req::Apply { ops, label: label.to_string() });
    }
}

// ---------------------------------------------------------------------- small widgets
fn tag(ui: &mut egui::Ui, text: &str, hi: bool) {
    let galley = ui.painter().layout_no_wrap(text.to_uppercase(), theme::mono(10.0), if hi { BG } else { DIM });
    let (rect, _) = ui.allocate_exact_size(galley.size() + Vec2::new(10.0, 4.0), Sense::hover());
    if hi { ui.painter().rect_filled(rect, 0.0, WHITE); } else { ui.painter().rect_stroke(rect, 0.0, Stroke::new(1.0, LINE_HI), egui::StrokeKind::Inside); }
    ui.painter().galley(rect.min + Vec2::new(5.0, 2.0), galley, if hi { BG } else { DIM });
}

fn section(ui: &mut egui::Ui, title: &str, n: Option<usize>) {
    ui.add_space(10.0);
    ui.horizontal(|ui| {
        ui.label(RichText::new(title.to_uppercase()).font(FontId::new(10.5, theme::mono_bold())).color(DIM));
        if let Some(n) = n { ui.label(RichText::new(format!("[{n:02}]")).font(theme::mono(10.5)).color(FAINT)); }
    });
    let y = ui.cursor().top();
    ui.painter().hline(ui.max_rect().x_range(), y, Stroke::new(1.0, LINE));
    ui.add_space(4.0);
}

fn btn(ui: &mut egui::Ui, text: &str) -> egui::Response { ui.add(egui::Button::new(RichText::new(text.to_uppercase()).font(theme::mono(11.0)))) }
fn btn_alert(ui: &mut egui::Ui, text: &str) -> egui::Response {
    ui.scope(|ui| {
        let w = &mut ui.style_mut().visuals.widgets;
        w.inactive.fg_stroke = Stroke::new(1.0, ALERT);
        w.inactive.bg_stroke = Stroke::new(1.0, ALERT);
        w.hovered.bg_fill = ALERT; w.hovered.weak_bg_fill = ALERT; w.hovered.bg_stroke = Stroke::new(1.0, ALERT); w.hovered.fg_stroke = Stroke::new(1.0, BG);
        ui.add(egui::Button::new(RichText::new(text.to_uppercase()).font(theme::mono(11.0))))
    }).inner
}

fn corner_brackets(p: &egui::Painter, r: Rect, col: Color32) {
    let l = 14.0;
    let s = Stroke::new(1.0, col);
    for (c, dx, dy) in [(r.left_top(), 1.0, 1.0), (r.right_top(), -1.0, 1.0), (r.left_bottom(), 1.0, -1.0), (r.right_bottom(), -1.0, -1.0)] {
        p.line_segment([c + Vec2::new(0.0, 0.0), c + Vec2::new(dx * l, 0.0)], s);
        p.line_segment([c, c + Vec2::new(0.0, dy * l)], s);
    }
}

// ---------------------------------------------------------------------- the mark
/// The logo: a double helix whose crossings are graph nodes and whose rungs are edges. `scroll` slides it along its axis.
fn draw_helix(p: &egui::Painter, centre_x: f32, top: f32, height: f32, scroll: f32, ink: Color32) {
    let amp = height * 0.17;
    let period = height * 0.5;
    let clip = Rect::from_min_max(Pos2::new(centre_x - amp - 14.0, top), Pos2::new(centre_x + amp + 14.0, top + height));
    let p = p.with_clip_rect(clip);
    let ph = |y: f32| std::f32::consts::TAU * (y - top + scroll) / period;
    let edge_fade = |y: f32| (((y - top).min(top + height - y)) / (height * 0.12)).clamp(0.0, 1.0);
    let col = |a: f32, y: f32| Color32::from_rgba_unmultiplied(ink.r(), ink.g(), ink.b(), (a * edge_fade(y) * 255.0) as u8);
    let n = (height / 2.0) as usize;
    for pass in 0..2 {                                  // pass 0: the strand that is behind, pass 1: the one in front
        for sign in [-1.0f32, 1.0] {
            let mut run: Vec<Pos2> = vec![];
            let mut run_front = false;
            let flush = |run: &mut Vec<Pos2>, front: bool| {
                if run.len() > 1 && (front as usize) == pass {
                    let y = run[run.len() / 2].y;
                    p.add(egui::Shape::line(run.clone(), Stroke::new(if front { 1.9 } else { 1.0 }, col(if front { 1.0 } else { 0.5 }, y))));
                }
                run.clear();
            };
            for i in 0..=n {
                let y = top + height * i as f32 / n as f32;
                let t = ph(y);
                let front = (t.cos() * sign) > 0.0;
                if i > 0 && front != run_front { let last = *run.last().unwrap(); flush(&mut run, run_front); run.push(last); }
                run_front = front;
                run.push(Pos2::new(centre_x + sign * amp * t.sin(), y));
            }
            flush(&mut run, run_front);
        }
    }
    // rungs where the strands are furthest apart, nodes where they cross
    let first = ((scroll / (period / 2.0)) - 1.0).floor() as i32;
    for k in first..first + (height / (period / 2.0)) as i32 + 3 {
        let yc = top - scroll + k as f32 * period / 2.0;                       // crossing
        let yr = yc + period / 4.0;                                            // rung
        if yr > top && yr < top + height {
            p.line_segment([Pos2::new(centre_x - amp, yr), Pos2::new(centre_x + amp, yr)], Stroke::new(0.8, col(0.4, yr)));
            for s in [-1.0, 1.0] { p.circle_filled(Pos2::new(centre_x + s * amp, yr), 1.7, col(1.0, yr)); }
        }
        if yc > top && yc < top + height {
            p.circle_filled(Pos2::new(centre_x, yc), 5.0, BG);
            p.circle_stroke(Pos2::new(centre_x, yc), 3.8, Stroke::new(1.5, col(1.0, yc)));
            p.circle_filled(Pos2::new(centre_x, yc), 1.1, col(1.0, yc));
        }
    }
}

// ---------------------------------------------------------------------- graph canvas
struct CanvasOut { clicked: Option<i32>, }

fn world_to_screen(c: &Cam, rect: Rect, w: [f32; 2]) -> Pos2 {
    rect.center() + Vec2::new((w[0] - c.center[0]) * c.zoom, (w[1] - c.center[1]) * c.zoom)
}
fn screen_to_world(c: &Cam, rect: Rect, p: Pos2) -> [f32; 2] {
    let d = p - rect.center();
    [c.center[0] + d.x / c.zoom, c.center[1] + d.y / c.zoom]
}

fn canvas(ui: &mut egui::Ui, v: &mut View, selected: i32, diff_mode: bool, time: f32, rect: Rect) -> CanvasOut {
    let resp = ui.allocate_rect(rect, Sense::click_and_drag());
    let ppp = ui.ctx().pixels_per_point();
    let pos = v.positions.lock().unwrap().clone();
    let n = v.data.nodes.len();
    let ptr = resp.hover_pos();
    let mut out = CanvasOut { clicked: None };

    // hover = nearest node under the pointer
    v.hover = -1;
    if let (Some(p), true) = (ptr, pos.len() == n) {
        let mut best = (18.0f32, -1i32);
        for (i, w) in pos.iter().enumerate() {
            let d = world_to_screen(&v.cam, rect, *w).distance(p);
            if d < best.0 { best = (d, i as i32); }
        }
        v.hover = best.1;
    }
    // drag: a node if the press started on one, otherwise pan
    if resp.drag_started() && v.hover >= 0 && !diff_mode { v.pin = v.hover; }
    if resp.drag_started() && v.pin < 0 { v.cam.auto_fit = false; }
    if resp.dragged() {
        if v.pin >= 0 { v.alpha = v.alpha.max(0.35); }
        else { let d = resp.drag_delta(); v.cam.center[0] -= d.x / v.cam.zoom; v.cam.center[1] -= d.y / v.cam.zoom; }
    }
    if resp.drag_stopped() { v.pin = -1; }
    // zoom around the pointer
    if resp.hovered() {
        let dy = ui.input(|i| i.smooth_scroll_delta.y);
        if dy != 0.0 {
            v.cam.auto_fit = false;
            if let Some(p) = ptr {
                let before = screen_to_world(&v.cam, rect, p);
                v.cam.zoom = (v.cam.zoom * (dy * 0.0016).exp()).clamp(0.08, 6.0);
                let after = screen_to_world(&v.cam, rect, p);
                v.cam.center[0] += before[0] - after[0];
                v.cam.center[1] += before[1] - after[1];
            }
        }
    }
    if resp.clicked() { out.clicked = Some(v.hover); }
    if ui.input(|i| i.key_pressed(egui::Key::F)) && resp.hovered() { v.cam.auto_fit = true; }

    // fit the view to the graph (continuously while the layout is still opening up)
    if v.cam.auto_fit && pos.len() == n && n > 0 {
        // frame the main mass: ignore the outermost 6% on each side so a few stray nodes can't zoom the camera away
        let mut lo = [0.0f32; 2];
        let mut hi = [0.0f32; 2];
        for k in 0..2 {
            let mut c: Vec<f32> = pos.iter().map(|w| w[k]).collect();
            c.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let cut = (c.len() as f32 * 0.06) as usize;
            lo[k] = c[cut.min(c.len() - 1)];
            hi[k] = c[(c.len() - 1 - cut).max(0)];
        }
        let target_c = [(lo[0] + hi[0]) * 0.5, (lo[1] + hi[1]) * 0.5];
        let span = [(hi[0] - lo[0]).max(120.0) + 160.0, (hi[1] - lo[1]).max(120.0) + 180.0];
        let target_z = (rect.width() / span[0]).min(rect.height() / span[1]).clamp(0.1, 2.4);
        let k = 0.12;
        for i in 0..2 { v.cam.center[i] += (target_c[i] - v.cam.center[i]) * k; }
        v.cam.zoom += (target_z - v.cam.zoom) * k;
    }

    // the GPU draws background, edges and nodes
    let pin_world = match (v.pin, ptr.or(ui.input(|i| i.pointer.latest_pos()))) { (p, Some(pp)) if p >= 0 => screen_to_world(&v.cam, rect, pp), _ => [0.0, 0.0] };
    v.alpha = (v.alpha * v.decay).max(if v.pin >= 0 { 0.3 } else { 0.012 });
    let steps = if v.alpha > 0.5 { if diff_mode { 10 } else { 6 } } else if v.alpha > 0.08 { 3 } else { 1 };
    let u = Uniforms {
        res: [rect.width() * ppp, rect.height() * ppp],
        center: v.cam.center,
        zoom: v.cam.zoom * ppp,
        time,
        dt: 1.0,
        alpha: v.alpha,
        selected: selected,
        hover: v.hover,
        pin: v.pin,
        mode: if diff_mode { 1 } else { 0 },
        ppp,
        pin_pos: pin_world,
        ..Default::default()
    };
    let cb = GraphCallback { slot: v.slot, data: v.data.clone(), version: v.version, uniforms: u, steps, positions: v.positions.clone(), background_only: false };
    ui.painter().add(egui_wgpu::Callback::new_paint_callback(rect, cb));
    if v.alpha > 0.02 || v.pin >= 0 || ui.input(|i| i.pointer.any_down()) || true { ui.ctx().request_repaint(); }

    // labels, drawn by egui on top of the GPU layer
    let p = ui.painter_at(rect);
    if pos.len() == n {
        let deg = v.data.degree();
        let mut order: Vec<usize> = (0..n).collect();
        order.sort_by(|a, b| (v.data.nodes[*b].size + deg[*b] as f32).partial_cmp(&(v.data.nodes[*a].size + deg[*a] as f32)).unwrap());
        let mut rank = vec![0usize; n];
        for (r, i) in order.iter().enumerate() { rank[*i] = r; }
        let neighbours: std::collections::HashSet<usize> = if selected >= 0 {
            v.data.links.iter().filter_map(|l| if l.a as i32 == selected { Some(l.b) } else if l.b as i32 == selected { Some(l.a) } else { None }).collect()
        } else { Default::default() };
        let budget = if diff_mode { 99 } else { (6.0 + v.cam.zoom * 16.0) as usize };
        for i in 0..n {
            let nd = &v.data.nodes[i];
            let focus = i as i32 == selected || i as i32 == v.hover;
            if !(focus || diff_mode || rank[i] < budget || (neighbours.contains(&i) && v.cam.zoom > 0.5)) { continue; }
            let sp = world_to_screen(&v.cam, rect, pos[i]);
            if !rect.expand(40.0).contains(sp) { continue; }
            let r = 7.0 + nd.size.sqrt() * 1.2;
            let col = if nd.state == 2 { ALERT } else if focus || nd.state == 1 { WHITE } else { Color32::from_rgb(190, 190, 190) };
            let text = if nd.kind == "event" { nd.name.clone() } else { nd.name.to_uppercase() };
            let text = if text.chars().count() > 30 { format!("{}…", text.chars().take(29).collect::<String>()) } else { text };
            let g = p.layout_no_wrap(text, theme::mono(if focus { 11.0 } else { 10.0 }), col);
            let at = sp + Vec2::new(r + 5.0, -g.size().y * 0.5);
            p.rect_filled(Rect::from_min_size(at - Vec2::new(2.0, 0.0), g.size() + Vec2::new(4.0, 0.0)), 0.0, Color32::from_black_alpha(190));
            p.galley(at, g, col);
            if diff_mode {
                let mut y = at.y + 13.0;
                for t in nd.adds.iter().take(2) { p.text(Pos2::new(at.x, y), Align2::LEFT_TOP, format!("+ {}", clip(t, 40)), theme::mono(9.5), WHITE); y += 12.0; }
                for t in nd.removes.iter().take(2) { p.text(Pos2::new(at.x, y), Align2::LEFT_TOP, format!("- {}", clip(t, 40)), theme::mono(9.5), ALERT); y += 12.0; }
            }
        }
    }
    // HUD
    corner_brackets(&p, rect.shrink(8.0), LINE_HI);
    let c = &v.cam;
    p.text(rect.left_bottom() + Vec2::new(18.0, -16.0), Align2::LEFT_BOTTOM, format!("X {:+07.1}  Y {:+07.1}  Z {:04.2}   {} NODES  {} LINKS", c.center[0], c.center[1], c.zoom, n, v.data.links.len()), theme::mono(10.0), DIM);
    let lg = rect.right_top() + Vec2::new(-18.0, 16.0);
    if rect.width() > 520.0 { p.text(lg, Align2::RIGHT_TOP, if diff_mode { "WHITE NEW   RED DASHED DELETED   GREY EXISTING" } else { "RING ENTITY   DIAMOND EVENT   DRAG NODE   SCROLL ZOOM   F FIT" }, theme::mono(10.0), FAINT); }
    out
}

fn clip(s: &str, n: usize) -> String { if s.chars().count() > n { format!("{}…", s.chars().take(n - 1).collect::<String>()) } else { s.to_string() } }

// ---------------------------------------------------------------------- the app
impl eframe::App for App {
    fn ui(&mut self, root: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = &root.ctx().clone();
        self.started_frames += 1;
        self.pump();
        if self.last_poll.elapsed().as_secs_f32() > 4.0 && self.boot.done_at.is_some() {
            self.last_poll = Instant::now();
            self.api.send(Req::Changes);
            self.api.send(Req::Stats);
        }
        let t = self.t0.elapsed().as_secs_f32();
        ctx.request_repaint();

        // screenshot for docs / checking the look
        if let Some(path) = self.opts.shot.clone() {
            if t >= self.opts.shot_at && !self.shot_sent {
                self.shot_sent = true;
                let c2 = ctx.clone();
                ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(egui::ScreenshotCallback::new(move |img| {
                    let (w, h) = (img.width() as u32, img.height() as u32);
                    let raw: Vec<u8> = img.pixels.iter().flat_map(|c| c.to_array()).collect();
                    if let Some(buf) = image::RgbaImage::from_raw(w, h, raw) { let _ = buf.save(&path); }
                    c2.send_viewport_cmd(egui::ViewportCommand::Close);
                })));
            }
        }

        // boot gate
        let boot_ready = self.boot.stats && self.boot.graph && self.boot.changes;
        if self.boot.done_at.is_none() {
            let bt = self.boot.t0.elapsed().as_secs_f32();
            if boot_ready && bt > 3.1 { self.boot.done_at = Some(Instant::now()); }
            self.draw_boot(root, t, bt);
            return;
        }
        let fade = 1.0 - (self.boot.done_at.unwrap().elapsed().as_secs_f32() / 0.45).min(1.0);

        let w = ctx.content_rect().width();
        (self.left_w, self.right_w) = if w >= 1500.0 { (300.0, 360.0) } else if w >= 1100.0 { (260.0, 320.0) } else { (210.0, 270.0) };
        self.top_bar(root);
        self.left_panel(root);
        self.right_panel(root);
        let time = t;
        egui::CentralPanel::default().frame(egui::Frame::NONE.fill(BG)).show(root, |ui| {
            let rect = ui.max_rect();
            match self.mode {
                Mode::Graph => {
                    let sel = self.selected;
                    let o = canvas(ui, &mut self.ex, sel, false, time, rect);
                    if let Some(i) = o.clicked {
                        if i >= 0 { self.select_idx(i as usize); } else { self.selected = -1; self.cur_ref.clear(); self.detail = None; }
                    }
                }
                Mode::Review | Mode::History => { let _ = canvas(ui, &mut self.diff, -1, true, time, rect); }
            }
            // boot fade-out veil
            if fade > 0.0 { ui.painter().rect_filled(ui.ctx().content_rect(), 0.0, Color32::from_black_alpha((fade * 255.0) as u8)); }
        });
        self.toast_ui(ctx);
    }
}

impl App {
    fn draw_boot(&mut self, root: &mut egui::Ui, t: f32, bt: f32) {
        let ppp = root.ctx().pixels_per_point();
        egui::CentralPanel::default().frame(egui::Frame::NONE.fill(BG)).show(root, |ui| {
            let rect = ui.max_rect();
            let progress = ((self.boot.stats as u8 + self.boot.graph as u8 + self.boot.changes as u8) as f32 / 3.0) * 0.6 + (bt / 3.2).min(1.0) * 0.4;
            let u = Uniforms { res: [rect.width() * ppp, rect.height() * ppp], zoom: 1.0 * ppp, time: t, mode: 2, boot: progress.min(1.0), ppp, selected: -1, hover: -1, pin: -1, ..Default::default() };
            let cb = GraphCallback { slot: 200, data: Arc::new(GraphData::default()), version: 1, uniforms: u, steps: 0, positions: Arc::new(Mutex::new(vec![])), background_only: true };
            ui.painter().add(egui_wgpu::Callback::new_paint_callback(rect, cb));
            let p = ui.painter();
            let lines: Vec<(f32, String, String)> = vec![
                (0.15, "LIFEGRAPH // SECURE MEMORY TERMINAL".into(), "".into()),
                (0.45, format!("BUILD {}   WGPU   {}", env!("CARGO_PKG_VERSION"), self.adapter.to_uppercase()), "".into()),
                (0.80, "OPEN CHANNEL".into(), if self.boot.stats { "127.0.0.1  OK".into() } else { "...".into() }),
                (1.25, "DECRYPT STORE".into(), if self.boot.graph { format!("{} ENTITIES  OK", self.stats.entities) } else { "...".into() }),
                (1.70, "SEMANTIC INDEX".into(), if self.boot.stats { format!("{} VECTORS  OK", self.stats.indexed) } else { "...".into() }),
                (2.15, "LAYOUT SOLVER".into(), "GPU COMPUTE  OK".into()),
                (2.55, "REVIEW QUEUE".into(), if self.boot.changes { format!("{} AWAITING  OK", self.pending.len()) } else { "...".into() }),
            ];
            draw_helix(p, rect.left() + 84.0, rect.top() + 52.0, 150.0, t * 9.0, WHITE);
            let x = rect.left() + 150.0;
            let mut y = rect.top() + 64.0;
            for (i, (at, a, b)) in lines.iter().enumerate() {
                if bt < *at { break; }
                let age = bt - at;
                let head = i < 2;
                let shown_a: String = if head { a.chars().take((age * 70.0) as usize).collect() } else { a.clone() };
                let col = if head { WHITE } else { INK };
                p.text(Pos2::new(x, y), Align2::LEFT_TOP, &shown_a, if i == 0 { FontId::new(17.0, theme::mono_bold()) } else { theme::mono(12.0) }, col);
                if !head {
                    let dots = ".".repeat(((age * 40.0) as usize).min(26));
                    p.text(Pos2::new(x + 190.0, y), Align2::LEFT_TOP, dots, theme::mono(12.0), FAINT);
                    let done = !b.ends_with("...");
                    if age > 0.25 { p.text(Pos2::new(x + 400.0, y), Align2::LEFT_TOP, b, theme::mono(12.0), if done { WHITE } else { DIM }); }
                }
                y += if i == 0 { 30.0 } else { 20.0 };
            }
            // blinking cursor
            if (t * 2.0) as i32 % 2 == 0 { p.rect_filled(Rect::from_min_size(Pos2::new(x, y + 6.0), Vec2::new(8.0, 14.0)), 0.0, WHITE); }
            // progress bar
            let bar = Rect::from_min_size(Pos2::new(rect.left() + 56.0, rect.bottom() - 70.0), Vec2::new((rect.width() - 112.0).min(560.0), 10.0));
            p.rect_stroke(bar, 0.0, Stroke::new(1.0, LINE_HI), egui::StrokeKind::Inside);
            let fill = Rect::from_min_size(bar.min + Vec2::new(2.0, 2.0), Vec2::new((bar.width() - 4.0) * progress.min(1.0), 6.0));
            p.rect_filled(fill, 0.0, WHITE);
            p.text(bar.left_top() + Vec2::new(0.0, -18.0), Align2::LEFT_TOP, format!("INITIALISING  {:03.0}%", progress.min(1.0) * 100.0), theme::mono(10.5), DIM);
            if self.opts.url.is_some() && bt > 6.0 && !self.boot.stats { p.text(bar.left_top() + Vec2::new(0.0, 22.0), Align2::LEFT_TOP, "NO ANSWER FROM THE LOCAL SERVER. IS `life ui` STILL RUNNING?", theme::mono(11.0), ALERT); }
            corner_brackets(p, rect.shrink(24.0), LINE_HI);
            p.text(rect.right_bottom() + Vec2::new(-36.0, -30.0), Align2::RIGHT_BOTTOM, "PRIVATE / LOCAL / ENCRYPTED", theme::mono(10.5), FAINT);
        });
    }

    fn top_bar(&mut self, root: &mut egui::Ui) {
        egui::Panel::top("bar").exact_size(40.0).frame(egui::Frame::NONE.fill(BG).stroke(Stroke::new(1.0, LINE)).inner_margin(egui::Margin::symmetric(14, 0))).show(root, |ui| {
            ui.horizontal_centered(|ui| {
                ui.label(RichText::new("LIFEGRAPH").font(FontId::new(15.0, theme::sans_bold())).color(WHITE));
                ui.add_space(6.0);
                ui.label(RichText::new("//").font(theme::mono(12.0)).color(FAINT));
                ui.add_space(8.0);
                let waiting = self.pending.len();
                for (m, name) in [(Mode::Graph, "GRAPH".to_string()), (Mode::Review, if waiting > 0 { format!("REVIEW {waiting}") } else { "REVIEW".to_string() }), (Mode::History, "HISTORY".to_string())] {
                    let on = self.mode == m;
                    let r = ui.add(egui::Button::new(RichText::new(name).font(FontId::new(11.5, if on { theme::mono_bold() } else { egui::FontFamily::Monospace })).color(if on { BG } else { INK })).fill(if on { WHITE } else { BG }));
                    if r.clicked() { self.mode = m; self.rsel = 0; self.load_diff(); }
                }
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    let s = &self.stats;
                    ui.label(RichText::new(format!("ENT {}   EVT {}   FACT {}   LINK {}   IDX {}{}", s.entities, s.events, s.facts, s.edges, s.indexed, if s.waiting > 0 { format!("+{}", s.waiting) } else { String::new() })).font(theme::mono(10.5)).color(DIM));
                    ui.label(RichText::new("●").color(if self.boot.stats { WHITE } else { ALERT }).font(theme::mono(11.0)));
                    ui.label(RichText::new(if self.opts.demo { "DEMO DATA" } else { "LINK" }).font(theme::mono(10.5)).color(DIM));
                });
            });
        });
    }

    fn left_panel(&mut self, root: &mut egui::Ui) {
        egui::Panel::left("left").exact_size(self.left_w).resizable(false).frame(egui::Frame::NONE.fill(PANEL).stroke(Stroke::new(1.0, LINE)).inner_margin(egui::Margin::same(12))).show(root, |ui| {
            match self.mode {
                Mode::Graph => {
                    ui.label(RichText::new("QUERY").font(FontId::new(10.5, theme::mono_bold())).color(DIM));
                    let te = ui.add(egui::TextEdit::singleline(&mut self.query).desired_width(f32::INFINITY).font(theme::mono(13.0)).hint_text("words or meaning").margin(egui::Margin::symmetric(8, 6)));
                    if self.query != self.last_query && (te.changed() || te.lost_focus()) {
                        self.last_query = self.query.clone();
                        if self.query.trim().is_empty() { self.hits.clear(); } else { self.api.send(Req::Search(self.query.clone())); }
                    }
                    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
                        if !self.query.trim().is_empty() {
                            section(ui, if self.hits.is_empty() { "no results" } else { "results" }, Some(self.hits.len()));
                            let hits = self.hits.clone();
                            for h in hits {
                                let on = h.r == self.cur_ref;
                                let r = list_row(ui, &h.name, &format!("{}{}{}", h.kind, if h.when.is_empty() { "".into() } else { format!("  {}", h.when) }, if h.found_by == "meaning" { "  BY MEANING" } else { "" }), on);
                                if r.clicked() { self.open_ref(&h.r); }
                            }
                        } else {
                            section(ui, "most connected", None);
                            let deg = self.ex.data.degree();
                            let mut order: Vec<usize> = (0..self.ex.data.nodes.len()).collect();
                            order.sort_by(|a, b| (self.ex.data.nodes[*b].size + deg[*b] as f32).partial_cmp(&(self.ex.data.nodes[*a].size + deg[*a] as f32)).unwrap());
                            for i in order.into_iter().take(40) {
                                let n = self.ex.data.nodes[i].clone();
                                let r = list_row(ui, &n.name, &format!("{}  {} links", n.kind, deg[i]), self.selected == i as i32);
                                if r.clicked() { self.select_idx(i); }
                            }
                        }
                    });
                }
                Mode::Review | Mode::History => {
                    let hist = self.mode == Mode::History;
                    ui.label(RichText::new(if hist { "RECENT CHANGES" } else { "AWAITING YOUR DECISION" }).font(FontId::new(10.5, theme::mono_bold())).color(DIM));
                    if !hist && self.pending.len() > 1 {
                        ui.add_space(6.0);
                        if btn(ui, "approve all").clicked() { for c in self.pending.clone() { self.api.send(Req::Decide { id: c.id, action: "approve".into() }); } }
                    }
                    let list = if hist { self.recent.clone() } else { self.pending.clone() };
                    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
                        if list.is_empty() { ui.add_space(14.0); ui.label(RichText::new(if hist { "NOTHING YET" } else { "NOTHING WAITING" }).font(theme::mono(11.0)).color(FAINT)); }
                        for (i, c) in list.iter().enumerate() {
                            let r = list_row(ui, &format!("{}  {}", c.id, c.agent), &format!("{}  {}", c.status, c.summary.first().map(|s| clip(s, 44)).unwrap_or_default()), self.rsel == i);
                            if r.clicked() { self.rsel = i; self.load_diff(); }
                        }
                    });
                }
            }
        });
    }

    fn right_panel(&mut self, root: &mut egui::Ui) {
        egui::Panel::right("right").exact_size(self.right_w).resizable(false).frame(egui::Frame::NONE.fill(PANEL).stroke(Stroke::new(1.0, LINE)).inner_margin(egui::Margin::same(14))).show(root, |ui| {
            egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| match self.mode {
                Mode::Graph => self.inspector(ui),
                _ => self.change_panel(ui),
            });
        });
    }

    fn inspector(&mut self, ui: &mut egui::Ui) {
        ui.set_max_width(ui.available_width());
        let Some(d) = self.detail.clone() else {
            ui.add_space(8.0);
            ui.label(RichText::new("INSPECTOR").font(FontId::new(10.5, theme::mono_bold())).color(DIM));
            ui.add_space(10.0);
            ui.label(RichText::new(if self.cur_ref.is_empty() { "SELECT A NODE OR SEARCH.\n\nEVERYTHING YOU CHANGE HERE IS RECORDED AND CAN BE UNDONE." } else { "READING..." }).font(theme::mono(11.0)).color(FAINT));
            return;
        };
        if !d.error.is_empty() { ui.label(RichText::new(format!("NOT FOUND: {}", d.error)).font(theme::mono(11.0)).color(ALERT)); return; }
        ui.horizontal(|ui| { tag(ui, if d.is_event { "event" } else { &d.kind }, true); tag(ui, &d.r, false); });
        ui.add_space(6.0);
        ui.add(egui::Label::new(RichText::new(&d.name).font(FontId::new(20.0, theme::sans_bold())).color(WHITE)).wrap());
        ui.label(RichText::new(if d.is_event { d.first_seen.clone() } else { format!("FIRST SEEN {}", d.first_seen) }).font(theme::mono(10.5)).color(DIM));
        if !d.aliases.is_empty() { ui.label(RichText::new(format!("ALSO: {}", d.aliases.join(", "))).font(theme::mono(10.5)).color(DIM)); }

        if d.is_event {
            if !d.body.is_empty() { section(ui, "details", None); ui.label(RichText::new(&d.body).font(theme::mono(12.0))); }
            section(ui, "involves", Some(d.events.len()));
            for (r, role, ent) in &d.events {
                if ui.add(egui::Button::new(RichText::new(format!("{ent}  ·  {role}")).font(theme::mono(11.0))).frame(false)).clicked() { self.open_ref(r); }
            }
            ui.add_space(16.0);
            if self.confirm_del { if btn_alert(ui, "confirm delete event").clicked() { let r = d.r.clone(); self.apply(vec![json!({"op":"forget","ref":r})], "EVENT DELETED"); } }
            else if btn_alert(ui, "delete event").clicked() { self.confirm_del = true; }
            return;
        }

        section(ui, "facts", Some(d.facts.len()));
        for (r, text) in &d.facts {
            if self.editing.as_ref().map(|e| &e.0 == r).unwrap_or(false) {
                let mut buf = self.editing.as_ref().unwrap().1.clone();
                ui.add(egui::TextEdit::multiline(&mut buf).desired_width(ui.available_width()).font(theme::mono(12.0)).desired_rows(2));
                self.editing.as_mut().unwrap().1 = buf.clone();
                let (mut save, mut cancel) = (false, false);
                ui.horizontal(|ui| { save = btn(ui, "save").clicked(); cancel = btn(ui, "cancel").clicked(); });
                if save { self.apply(vec![json!({"op":"edit_fact","ref":r,"text":buf})], "FACT CHANGED"); }
                if cancel { self.editing = None; }
            } else {
                ui.add(egui::Label::new(RichText::new(text).font(theme::mono(12.0))).wrap());
                ui.horizontal(|ui| {
                    if btn(ui, "edit").clicked() { self.editing = Some((r.clone(), text.clone())); }
                    if btn_alert(ui, "delete").clicked() { self.apply(vec![json!({"op":"forget","ref":r})], "FACT DELETED"); }
                });
                ui.add_space(4.0);
            }
        }
        if d.facts.is_empty() { ui.label(RichText::new("NONE").font(theme::mono(11.0)).color(FAINT)); }
        ui.add_space(4.0);
        let te = ui.add(egui::TextEdit::singleline(&mut self.new_fact).desired_width(ui.available_width()).font(theme::mono(12.0)).hint_text("add a fact, enter to save").margin(egui::Margin::symmetric(8, 5)));
        if te.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter)) && !self.new_fact.trim().is_empty() {
            let t = self.new_fact.trim().to_string();
            self.apply(vec![json!({"op":"fact","subject":d.r,"text":t})], "FACT ADDED");
        }

        section(ui, "links", Some(d.relations.len()));
        for (r, rel, dir, with) in &d.relations {
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new(if dir == "out" { "→" } else { "←" }).font(theme::mono(12.0)).color(DIM));
                ui.label(RichText::new(rel.to_uppercase()).font(theme::mono(10.5)).color(DIM));
                let target = with.clone();
                if ui.add(egui::Button::new(RichText::new(clip(&target, 22)).font(theme::mono(11.5))).frame(false)).clicked() {
                    if let Some(i) = self.ex.data.nodes.iter().position(|n| format!("{}:{}", n.kind, n.name) == target) { self.select_idx(i); }
                }
                if btn_alert(ui, "x").clicked() { self.apply(vec![json!({"op":"forget","ref":r})], "LINK DELETED"); }
            });
        }
        if d.relations.is_empty() { ui.label(RichText::new("NONE").font(theme::mono(11.0)).color(FAINT)); }

        section(ui, "events", Some(d.events.len()));
        for (r, when, title) in &d.events {
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new(when.chars().take(10).collect::<String>()).font(theme::mono(10.5)).color(DIM));
                if ui.add(egui::Button::new(RichText::new(clip(title, 24)).font(theme::mono(11.5))).frame(false)).clicked() { self.open_ref(r); }
            });
        }
        if d.events.is_empty() { ui.label(RichText::new("NONE").font(theme::mono(11.0)).color(FAINT)); }

        ui.add_space(18.0);
        ui.separator();
        ui.add_space(6.0);
        if self.merging {
            ui.label(RichText::new("MERGE INTO (kind:name)").font(theme::mono(10.5)).color(DIM));
            ui.add(egui::TextEdit::singleline(&mut self.merge_to).desired_width(ui.available_width()).font(theme::mono(12.0)).margin(egui::Margin::symmetric(8, 5)));
            ui.horizontal(|ui| {
                if btn_alert(ui, "merge (no undo)").clicked() && !self.merge_to.trim().is_empty() { let to = self.merge_to.trim().to_string(); self.apply(vec![json!({"op":"merge","from":d.r,"to":to})], "MERGED"); }
                if btn(ui, "cancel").clicked() { self.merging = false; }
            });
        } else {
            ui.horizontal(|ui| {
                if btn(ui, "merge into…").clicked() { self.merging = true; self.merge_to.clear(); }
                if self.confirm_del { if btn_alert(ui, "confirm delete").clicked() { self.apply(vec![json!({"op":"forget","ref":d.r})], "ENTITY DELETED"); } }
                else if btn_alert(ui, "delete entity").clicked() { self.confirm_del = true; }
            });
            if self.confirm_del { ui.label(RichText::new("DELETES THE ENTITY WITH ITS FACTS AND LINKS. EVENTS STAY.").font(theme::mono(10.0)).color(ALERT)); }
        }
    }

    fn change_panel(&mut self, ui: &mut egui::Ui) {
        let hist = self.mode == Mode::History;
        let list = if hist { self.recent.clone() } else { self.pending.clone() };
        let Some(c) = list.get(self.rsel).cloned() else {
            ui.add_space(8.0);
            ui.label(RichText::new(if hist { "NO CHANGES YET" } else { "NOTHING WAITING.\n\nAGENTS PROPOSE. YOU DECIDE." }).font(theme::mono(11.0)).color(FAINT));
            return;
        };
        ui.horizontal(|ui| { tag(ui, &c.id, true); tag(ui, &c.status, false); });
        ui.add_space(6.0);
        ui.add(egui::Label::new(RichText::new(format!("{} wants to:", c.agent)).font(FontId::new(18.0, theme::sans_bold())).color(WHITE)).wrap());
        ui.label(RichText::new(format!("{}  {}", c.source, c.when)).font(theme::mono(10.5)).color(DIM));
        if !c.note.is_empty() { ui.label(RichText::new(&c.note).font(theme::mono(10.5)).color(ALERT)); }
        section(ui, "what happens", Some(c.summary.len()));
        for s in &c.summary {
            let destructive = s.starts_with("Delete") || s.starts_with("Merge") || s.starts_with("Change");
            ui.horizontal_top(|ui| {
                ui.label(RichText::new(if destructive { "−" } else { "+" }).font(theme::mono_bold_id(12.0)).color(if destructive { ALERT } else { WHITE }));
                ui.add(egui::Label::new(RichText::new(s).font(theme::mono(11.5))).wrap());
            });
            ui.add_space(2.0);
        }
        if !c.targets.is_empty() {
            section(ui, "where it attaches", Some(c.targets.len()));
            for t in &c.targets {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(&t.name).font(theme::mono(11.5)));
                    tag(ui, if t.status == "new" { "new" } else { "existing" }, t.status == "new");
                });
            }
        }
        ui.add_space(18.0);
        ui.horizontal(|ui| {
            if c.status == "pending" {
                if ui.add(egui::Button::new(RichText::new("YES, APPLY").font(theme::mono_bold_id(12.0)).color(BG)).fill(WHITE).min_size(Vec2::new(120.0, 30.0))).clicked() { self.api.send(Req::Decide { id: c.id.clone(), action: "approve".into() }); }
                if btn_alert(ui, "no, reject").clicked() { self.api.send(Req::Decide { id: c.id.clone(), action: "reject".into() }); }
            } else if c.status == "applied" && btn_alert(ui, "undo this").clicked() { self.api.send(Req::Decide { id: c.id.clone(), action: "undo".into() }); }
        });
    }

    fn toast_ui(&mut self, ctx: &egui::Context) {
        let Some((msg, undo, at)) = self.toast.clone() else { return };
        if at.elapsed().as_secs_f32() > 9.0 { self.toast = None; return; }
        egui::Area::new(egui::Id::new("toast")).anchor(Align2::CENTER_BOTTOM, Vec2::new(0.0, -26.0)).order(egui::Order::Foreground).show(ctx, |ui| {
            egui::Frame::NONE.fill(BG).stroke(Stroke::new(1.0, WHITE)).inner_margin(egui::Margin::symmetric(14, 8)).show(ui, |ui| {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(&msg).font(theme::mono(11.5)).color(WHITE));
                    if let Some(id) = undo { if btn(ui, "undo").clicked() { self.api.send(Req::Decide { id, action: "undo".into() }); self.toast = None; } }
                });
            });
        });
    }
}

fn list_row(ui: &mut egui::Ui, title: &str, sub: &str, on: bool) -> egui::Response {
    let w = ui.available_width();
    let galley_t = ui.painter().layout(title.to_string(), FontId::new(13.5, egui::FontFamily::Proportional), if on { BG } else { INK }, w - 20.0);
    let galley_s = ui.painter().layout_no_wrap(sub.to_uppercase(), theme::mono(9.5), if on { BG } else { DIM });
    let h = galley_t.size().y + galley_s.size().y + 14.0;
    let (rect, resp) = ui.allocate_exact_size(Vec2::new(w, h), Sense::click());
    if on { ui.painter().rect_filled(rect, 0.0, WHITE); } else if resp.hovered() { ui.painter().rect_filled(rect, 0.0, Color32::from_rgb(20, 20, 20)); }
    ui.painter().rect_filled(Rect::from_min_size(rect.min, Vec2::new(2.0, rect.height())), 0.0, if on { BG } else if resp.hovered() { WHITE } else { Color32::TRANSPARENT });
    ui.painter().galley(rect.min + Vec2::new(10.0, 5.0), galley_t.clone(), INK);
    ui.painter().galley(rect.min + Vec2::new(10.0, 8.0 + galley_t.size().y), galley_s, DIM);
    resp
}
