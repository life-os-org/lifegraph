struct U {
    res: vec2<f32>,        // viewport size in physical pixels
    center: vec2<f32>,     // world point at the middle of the viewport
    zoom: f32,             // physical pixels per world unit
    time: f32,
    dt: f32,
    alpha: f32,            // layout "temperature"
    n_nodes: u32,
    n_edges: u32,
    selected: i32,
    hover: i32,
    pin: i32,              // node held by the mouse, or -1
    mode: u32,             // 0 graph, 1 diff graph, 2 boot
    boot: f32,             // boot progress 0..1
    ppp: f32,              // pixels per point
    pin_pos: vec2<f32>,
    _pad: vec2<f32>,
};

fn hash21(p: vec2<f32>) -> f32 {
    var q = fract(p * vec2<f32>(123.34, 456.21));
    q = q + dot(q, q + 45.32);
    return fract(q.x * q.y);
}
