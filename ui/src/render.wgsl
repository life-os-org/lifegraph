@group(0) @binding(0) var<uniform> u: U;
@group(0) @binding(1) var<storage, read> sim: array<vec4<f32>>;
@group(0) @binding(2) var<storage, read> attr: array<vec4<f32>>;
@group(0) @binding(3) var<storage, read> edges: array<vec4<f32>>;       // a b weight state

fn to_ndc(px: vec2<f32>) -> vec4<f32> {
    return vec4<f32>(px.x / u.res.x * 2.0 - 1.0, 1.0 - px.y / u.res.y * 2.0, 0.0, 1.0);
}
fn world_px(w: vec2<f32>) -> vec2<f32> {
    return (w - u.center) * u.zoom + u.res * 0.5;
}

// ------------------------------------------------------------------ background
struct BgOut { @builtin(position) pos: vec4<f32>, @location(0) px: vec2<f32> };

@vertex
fn vs_bg(@builtin(vertex_index) vi: u32) -> BgOut {
    var p = array<vec2<f32>, 3>(vec2<f32>(-1.0, -1.0), vec2<f32>(3.0, -1.0), vec2<f32>(-1.0, 3.0));
    var o: BgOut;
    o.pos = vec4<f32>(p[vi], 0.0, 1.0);
    o.px = vec2<f32>((p[vi].x * 0.5 + 0.5) * u.res.x, (0.5 - p[vi].y * 0.5) * u.res.y);
    return o;
}

fn ring_line(d: f32, r: f32, w: f32) -> f32 {
    return 1.0 - smoothstep(0.0, w, abs(d - r));
}

@fragment
fn fs_bg(in: BgOut) -> @location(0) vec4<f32> {
    let px = in.px;
    let wp = (px - u.res * 0.5) / u.zoom + u.center;
    var c = 0.0;
    // dot grid in world space, with brighter cross marks every fifth line
    let cell = 48.0;
    let g = wp / cell;
    let gf = fract(g + 0.5) - 0.5;
    let gd = length(gf) * cell * u.zoom;
    let major = (abs(fract(g.x / 5.0 + 0.5) - 0.5) < 0.5 / 5.0 * 0.4) && (abs(fract(g.y / 5.0 + 0.5) - 0.5) < 0.5 / 5.0 * 0.4);
    var dot_a = 1.0 - smoothstep(0.6, 1.4, gd);
    c = c + dot_a * 0.10;
    if (major) {
        let m = min(abs(gf.x), abs(gf.y)) * cell * u.zoom;
        let arm = max(abs(gf.x), abs(gf.y)) * cell * u.zoom;
        c = c + (1.0 - smoothstep(0.4, 1.0, m)) * (1.0 - smoothstep(5.0, 7.0, arm)) * 0.22;
    }
    // range rings around the origin
    let rr = length(wp);
    for (var k = 1; k <= 4; k = k + 1) {
        let r = f32(k) * 260.0;
        c = c + ring_line(rr * u.zoom, r * u.zoom, 1.0) * 0.05;
    }
    // vignette + scanlines + grain
    let uv = px / u.res;
    let vig = 1.0 - 0.55 * pow(length(uv - 0.5) * 1.45, 2.4);
    c = c * clamp(vig, 0.0, 1.0);
    c = c + 0.012 * sin(px.y * 3.14159 / max(u.ppp, 1.0));
    c = c + (hash21(px + vec2<f32>(u.time * 37.0, u.time * 11.0)) - 0.5) * 0.028;

    if (u.mode == 2u) {
        // boot: a radar sweep over a range-ring field that opens up as loading progresses
        let m = min(u.res.x, u.res.y);
        let q = (px - u.res * vec2<f32>(0.64, 0.54)) / m;
        let d = length(q);
        let R = 0.14 + 0.24 * smoothstep(0.0, 1.0, u.boot);
        var a = atan2(q.y, q.x) / 6.28318 + 0.5;
        let sweep = fract(a - u.time * 0.28);
        let beam = pow(1.0 - sweep, 7.0) * step(d, R);
        c = c + beam * 0.55;
        c = c + ring_line(d, R, 0.0035) * 0.55;
        c = c + ring_line(d, R * 0.66, 0.002) * 0.22;
        c = c + ring_line(d, R * 0.33, 0.002) * 0.22;
        c = c + (1.0 - smoothstep(0.0, 0.0025, abs(q.x))) * step(d, R) * 0.16;
        c = c + (1.0 - smoothstep(0.0, 0.0025, abs(q.y))) * step(d, R) * 0.16;
        // tick marks on the outer ring
        let tk = fract(a * 72.0);
        c = c + step(tk, 0.06) * step(abs(d - R * 1.04), R * 0.025) * 0.5;
        // blips the sweep reveals
        for (var i = 0; i < 9; i = i + 1) {
            let fi = f32(i);
            let ang = hash21(vec2<f32>(fi, 1.7)) ;
            let rad = (0.25 + 0.7 * hash21(vec2<f32>(fi, 9.1))) * R;
            let bp = vec2<f32>(cos(ang * 6.28318 - 3.14159), sin(ang * 6.28318 - 3.14159)) * rad;
            let seen = pow(1.0 - fract(ang - u.time * 0.28), 3.0);
            c = c + (1.0 - smoothstep(0.0, 0.006, length(q - bp))) * seen * 0.9;
        }
    }
    c = clamp(c, 0.0, 1.0);
    return vec4<f32>(c, c, c, 1.0);
}

// ------------------------------------------------------------------ edges
struct EdgeOut {
    @builtin(position) pos: vec4<f32>,
    @location(0) local: vec2<f32>,        // along (px), across (px)
    @location(1) @interpolate(flat) len: f32,
    @location(2) @interpolate(flat) info: vec4<f32>,  // weight, state, highlighted, unused
};

@vertex
fn vs_edge(@builtin(vertex_index) vi: u32, @builtin(instance_index) ii: u32) -> EdgeOut {
    var cs = array<vec2<f32>, 6>(vec2<f32>(0.0, -1.0), vec2<f32>(1.0, -1.0), vec2<f32>(0.0, 1.0), vec2<f32>(0.0, 1.0), vec2<f32>(1.0, -1.0), vec2<f32>(1.0, 1.0));
    let e = edges[ii];
    let ia = u32(e.x);
    let ib = u32(e.y);
    let pa = world_px(sim[ia].xy);
    let pb = world_px(sim[ib].xy);
    let d = pb - pa;
    let len = max(length(d), 0.001);
    let dir = d / len;
    let nrm = vec2<f32>(-dir.y, dir.x);
    let c = cs[vi];
    let half_w = 3.0 * u.ppp;
    let px = pa + d * c.x + nrm * c.y * half_w;
    var o: EdgeOut;
    o.pos = to_ndc(px);
    o.local = vec2<f32>(c.x * len, c.y * half_w);
    o.len = len;
    let hl = select(0.0, 1.0, i32(ia) == u.selected || i32(ib) == u.selected || i32(ia) == u.hover || i32(ib) == u.hover);
    o.info = vec4<f32>(e.z, e.w, hl, 0.0);
    return o;
}

@fragment
fn fs_edge(in: EdgeOut) -> @location(0) vec4<f32> {
    let w = in.info.x;
    let state = in.info.y;
    let hl = in.info.z;
    let across = abs(in.local.y);
    let thick = (0.7 + 0.25 * min(w, 4.0) + hl * 0.5) * u.ppp;
    let line = 1.0 - smoothstep(thick - 0.5 * u.ppp, thick + 0.6 * u.ppp, across);
    var col = vec3<f32>(0.62, 0.62, 0.62);
    var a = line * (0.10 + 0.05 * min(w, 4.0));
    if (hl > 0.5) { col = vec3<f32>(1.0, 1.0, 1.0); a = line * 0.85; }
    if (state > 0.5 && state < 1.5) {          // new: bright, with a pulse travelling along it
        let t = in.local.x / max(in.len, 1.0);
        let pulse = smoothstep(0.82, 1.0, fract(t * 1.0 - u.time * 0.45)) ;
        col = vec3<f32>(1.0, 1.0, 1.0);
        a = line * (0.65 + 0.35 * pulse);
    }
    if (state > 1.5) {                          // removed: marching dashes in muted red
        let dash = step(0.5, fract((in.local.x / (9.0 * u.ppp)) - u.time * 1.2));
        col = vec3<f32>(0.95, 0.27, 0.22);
        a = line * dash * 0.9;
    }
    return vec4<f32>(col * a, a);
}

// ------------------------------------------------------------------ nodes
struct NodeOut {
    @builtin(position) pos: vec4<f32>,
    @location(0) local: vec2<f32>,
    @location(1) @interpolate(flat) idx: u32,
    @location(2) @interpolate(flat) rad: f32,
};

fn node_radius(a: vec4<f32>) -> f32 {
    let base = select(5.0, 4.0, a.y > 0.5);
    return (base + 1.4 * sqrt(max(a.x, 0.0))) * u.ppp * clamp(pow(u.zoom / u.ppp, 0.35), 0.65, 1.6);
}

@vertex
fn vs_node(@builtin(vertex_index) vi: u32, @builtin(instance_index) ii: u32) -> NodeOut {
    var cs = array<vec2<f32>, 6>(vec2<f32>(-1.0, -1.0), vec2<f32>(1.0, -1.0), vec2<f32>(-1.0, 1.0), vec2<f32>(-1.0, 1.0), vec2<f32>(1.0, -1.0), vec2<f32>(1.0, 1.0));
    let a = attr[ii];
    let r = node_radius(a);
    let ext = r * 2.7 + 8.0 * u.ppp;
    let c = cs[vi];
    let px = world_px(sim[ii].xy) + c * ext;
    var o: NodeOut;
    o.pos = to_ndc(px);
    o.local = c * ext;
    o.idx = ii;
    o.rad = r;
    return o;
}

fn sd_diamond(p: vec2<f32>, r: f32) -> f32 { return (abs(p.x) + abs(p.y)) - r; }

@fragment
fn fs_node(in: NodeOut) -> @location(0) vec4<f32> {
    let a = attr[in.idx];
    let kind = a.y;
    let state = a.z;
    let r = in.rad;
    let aa = 1.1 * u.ppp;
    let p = in.local;
    let is_sel = i32(in.idx) == u.selected;
    let is_hov = i32(in.idx) == u.hover;
    var d: f32;
    if (kind > 0.5) { d = sd_diamond(p, r * 1.15); } else { d = length(p) - r; }
    let body = 1.0 - smoothstep(-aa, aa, d);                    // filled shape
    let edge = 1.0 - smoothstep(0.0, aa * 1.2, abs(d) - 0.9 * u.ppp);  // 1px outline

    var ink = vec3<f32>(0.72, 0.72, 0.72);
    var fill = 0.92;                                            // black disc so edges don't show through
    var rgb = vec3<f32>(0.0);
    var alpha = body * fill;
    if (state > 0.5 && state < 1.5) { ink = vec3<f32>(1.0); }
    if (state > 1.5) { ink = vec3<f32>(0.95, 0.27, 0.22); }
    if (is_sel) { ink = vec3<f32>(1.0); }
    if (is_hov) { ink = vec3<f32>(1.0); }

    // outline
    var line = edge;
    if (state > 1.5) {                                          // removed: dashed outline
        let ang = atan2(p.y, p.x);
        line = line * step(0.5, fract(ang * 3.0 + u.time * 0.6));
    }
    rgb = ink * line;
    alpha = max(alpha, line);
    // inner mark: a dot for things, nothing for events
    if (kind < 0.5) {
        let dotr = 1.0 - smoothstep(r * 0.22 - aa, r * 0.22 + aa, length(p));
        rgb = rgb + ink * dotr * 0.85;
        alpha = max(alpha, dotr);
    }
    // new: filled bright core + slow halo
    if (state > 0.5 && state < 1.5) {
        let core = 1.0 - smoothstep(-aa, aa, d + r * 0.45);
        rgb = rgb + vec3<f32>(1.0) * core;
        alpha = max(alpha, core);
        let halo = (0.5 + 0.5 * sin(u.time * 2.2 + f32(in.idx))) * exp(-max(d, 0.0) / (r * 0.9)) * 0.28;
        rgb = rgb + vec3<f32>(halo);
        alpha = max(alpha, halo);
    }
    // removed: strike-through
    if (state > 1.5) {
        let s = abs(p.x + p.y) / 1.414;
        let strike = (1.0 - smoothstep(0.6 * u.ppp, 1.6 * u.ppp, s)) * (1.0 - smoothstep(r * 1.1, r * 1.3, length(p)));
        rgb = rgb + ink * strike;
        alpha = max(alpha, strike);
    }
    // hover/selected: soft glow
    if (is_sel || is_hov) {
        let glow = exp(-max(d, 0.0) / (r * 0.7)) * select(0.14, 0.3, is_sel);
        rgb = rgb + vec3<f32>(glow);
        alpha = max(alpha, glow);
    }
    // selected: HUD brackets that rotate slowly + range ring
    if (is_sel) {
        let rr = r * 1.9;
        let rd = length(p);
        let ang = atan2(p.y, p.x) + u.time * 0.6;
        let seg = step(0.55, fract(ang * 4.0 / 6.28318 * 2.0));
        let ringm = (1.0 - smoothstep(0.0, aa, abs(rd - rr) - 0.6 * u.ppp)) * seg;
        rgb = rgb + vec3<f32>(1.0) * ringm * 0.9;
        alpha = max(alpha, ringm * 0.9);
        let q = abs(p);
        let br = r * 2.5;
        let tickx = (1.0 - smoothstep(0.0, aa, abs(q.x - br) - 0.6 * u.ppp)) * step(q.y, br) * step(br - 7.0 * u.ppp, q.y);
        let ticky = (1.0 - smoothstep(0.0, aa, abs(q.y - br) - 0.6 * u.ppp)) * step(q.x, br) * step(br - 7.0 * u.ppp, q.x);
        let br_a = max(tickx, ticky);
        rgb = rgb + vec3<f32>(1.0) * br_a;
        alpha = max(alpha, br_a);
    }
    alpha = clamp(alpha, 0.0, 1.0);
    return vec4<f32>(min(rgb, vec3<f32>(alpha)), alpha);
}
