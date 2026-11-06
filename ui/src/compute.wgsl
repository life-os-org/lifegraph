// Force-directed layout, one thread per node. Ping-pong buffers: read `sim_in`, write `sim_out`.
@group(0) @binding(0) var<uniform> u: U;
@group(0) @binding(1) var<storage, read> sim_in: array<vec4<f32>>;       // x y vx vy
@group(0) @binding(2) var<storage, read_write> sim_out: array<vec4<f32>>;
@group(0) @binding(3) var<storage, read> attr: array<vec4<f32>>;         // size kind state flags
@group(0) @binding(4) var<storage, read> adj_off: array<u32>;
@group(0) @binding(5) var<storage, read> adj: array<u32>;

const REPULSION: f32 = 520.0;
const SPRING: f32 = 0.075;
const REST: f32 = 62.0;
const GRAVITY: f32 = 0.055;
const DAMP: f32 = 0.78;

@compute @workgroup_size(64)
fn solve_layout(@builtin(global_invocation_id) gid: vec3<u32>) {
    let i = gid.x;
    if (i >= u.n_nodes) { return; }
    let p = sim_in[i].xy;
    var v = sim_in[i].zw;
    var f = vec2<f32>(0.0, 0.0);
    for (var j = 0u; j < u.n_nodes; j = j + 1u) {
        if (j == i) { continue; }
        let d = p - sim_in[j].xy;
        let r2 = max(dot(d, d), 100.0);
        let fall = 1.0 - smoothstep(350.0, 900.0, sqrt(r2));          // far nodes barely push, so strays are not flung away
        f = f + d / r2 * REPULSION * (0.6 + 0.4 * attr[j].x) * fall;
    }
    let a0 = adj_off[i];
    let a1 = adj_off[i + 1u];
    for (var k = a0; k < a1; k = k + 1u) {
        let j = adj[k];
        let d = sim_in[j].xy - p;
        let len = length(d) + 0.001;
        f = f + d / len * (len - REST) * SPRING;
    }
    let deg = f32(a1 - a0);
    f = f - p * GRAVITY * (1.0 + 3.0 / (1.0 + deg));              // loosely connected nodes are pulled in harder
    v = (v + f * u.alpha) * DAMP;
    let sp = length(v);
    if (sp > 18.0) { v = v / sp * 18.0; }
    var np = p + v;
    if (i32(i) == u.pin) { np = u.pin_pos; v = vec2<f32>(0.0, 0.0); }
    sim_out[i] = vec4<f32>(np, v);
}
