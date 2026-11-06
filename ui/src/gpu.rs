//! The GPU side: force layout in a compute shader, and background/edges/nodes in render shaders, drawn inside egui's render pass.
use crate::model::GraphData;
use bytemuck::{Pod, Zeroable};
use eframe::egui_wgpu::{wgpu, CallbackResources, CallbackTrait, ScreenDescriptor};
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use wgpu::util::DeviceExt;

#[repr(C)]
#[derive(Clone, Copy, Pod, Zeroable, Default)]
pub struct Uniforms {
    pub res: [f32; 2],
    pub center: [f32; 2],
    pub zoom: f32,
    pub time: f32,
    pub dt: f32,
    pub alpha: f32,
    pub n_nodes: u32,
    pub n_edges: u32,
    pub selected: i32,
    pub hover: i32,
    pub pin: i32,
    pub mode: u32,
    pub boot: f32,
    pub ppp: f32,
    pub pin_pos: [f32; 2],
    pub _pad: [f32; 2],
}

enum Rb {
    Idle,
    Copied,
    Mapping,
}

struct GpuGraph {
    version: u64,
    n_nodes: u32,
    n_edges: u32,
    uniform: wgpu::Buffer,
    sim: [wgpu::Buffer; 2],
    cur: usize,                       // which sim buffer holds the latest positions
    compute_bg: [wgpu::BindGroup; 2], // [0]: read sim0 write sim1, [1]: read sim1 write sim0
    render_bg: [wgpu::BindGroup; 2],  // render reads sim0 / sim1
    staging: wgpu::Buffer,
    rb: Rb,
    rb_ready: Arc<AtomicBool>,
}

pub struct Gpu {
    compute: wgpu::ComputePipeline,
    bg: wgpu::RenderPipeline,
    edge: wgpu::RenderPipeline,
    node: wgpu::RenderPipeline,
    compute_bgl: wgpu::BindGroupLayout,
    render_bgl: wgpu::BindGroupLayout,
    graphs: HashMap<u8, GpuGraph>,
}

fn storage_entry(binding: u32, vis: wgpu::ShaderStages, read_only: bool) -> wgpu::BindGroupLayoutEntry {
    wgpu::BindGroupLayoutEntry {
        binding,
        visibility: vis,
        ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only }, has_dynamic_offset: false, min_binding_size: None },
        count: None,
    }
}
fn uniform_entry(binding: u32, vis: wgpu::ShaderStages) -> wgpu::BindGroupLayoutEntry {
    wgpu::BindGroupLayoutEntry {
        binding,
        visibility: vis,
        ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Uniform, has_dynamic_offset: false, min_binding_size: None },
        count: None,
    }
}

impl Gpu {
    pub fn new(device: &wgpu::Device, format: wgpu::TextureFormat) -> Gpu {
        let common = include_str!("common.wgsl");
        let compute_src = format!("{common}\n{}", include_str!("compute.wgsl"));
        let render_src = format!("{common}\n{}", include_str!("render.wgsl"));
        let cmod = device.create_shader_module(wgpu::ShaderModuleDescriptor { label: Some("lg-compute"), source: wgpu::ShaderSource::Wgsl(compute_src.into()) });
        let rmod = device.create_shader_module(wgpu::ShaderModuleDescriptor { label: Some("lg-render"), source: wgpu::ShaderSource::Wgsl(render_src.into()) });

        let cs = wgpu::ShaderStages::COMPUTE;
        let compute_bgl = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("lg-compute-bgl"),
            entries: &[uniform_entry(0, cs), storage_entry(1, cs, true), storage_entry(2, cs, false), storage_entry(3, cs, true), storage_entry(4, cs, true), storage_entry(5, cs, true)],
        });
        let vf = wgpu::ShaderStages::VERTEX_FRAGMENT;
        let render_bgl = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("lg-render-bgl"),
            entries: &[uniform_entry(0, vf), storage_entry(1, vf, true), storage_entry(2, vf, true), storage_entry(3, vf, true)],
        });
        let cl = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor { label: None, bind_group_layouts: &[Some(&compute_bgl)], immediate_size: 0 });
        let rl = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor { label: None, bind_group_layouts: &[Some(&render_bgl)], immediate_size: 0 });
        let compute = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some("lg-layout"),
            layout: Some(&cl),
            module: &cmod,
            entry_point: Some("solve_layout"),
            compilation_options: Default::default(),
            cache: None,
        });
        let mk = |vs: &str, fs: &str, label: &str| {
            device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
                label: Some(label),
                layout: Some(&rl),
                vertex: wgpu::VertexState { module: &rmod, entry_point: Some(vs), compilation_options: Default::default(), buffers: &[] },
                primitive: wgpu::PrimitiveState { topology: wgpu::PrimitiveTopology::TriangleList, ..Default::default() },
                depth_stencil: None,
                multisample: wgpu::MultisampleState::default(),
                fragment: Some(wgpu::FragmentState {
                    module: &rmod,
                    entry_point: Some(fs),
                    compilation_options: Default::default(),
                    targets: &[Some(wgpu::ColorTargetState { format, blend: Some(wgpu::BlendState::PREMULTIPLIED_ALPHA_BLENDING), write_mask: wgpu::ColorWrites::ALL })],
                }),
                multiview_mask: None,
                cache: None,
            })
        };
        Gpu { compute, bg: mk("vs_bg", "fs_bg", "lg-bg"), edge: mk("vs_edge", "fs_edge", "lg-edge"), node: mk("vs_node", "fs_node", "lg-node"), compute_bgl, render_bgl, graphs: HashMap::new() }
    }

    fn upload(&mut self, device: &wgpu::Device, slot: u8, data: &GraphData, version: u64) {
        let n = data.nodes.len();
        // start on a circle with a little jitter so the layout opens up quickly and deterministically
        let mut sim = Vec::with_capacity(n.max(1));
        for i in 0..n {
            let a = i as f32 / n.max(1) as f32 * std::f32::consts::TAU;
            let r = 140.0 + 90.0 * ((i * 7919 % 13) as f32 / 13.0);
            sim.push([a.cos() * r, a.sin() * r, 0.0, 0.0]);
        }
        if sim.is_empty() { sim.push([0.0; 4]); }
        let deg = data.degree();
        let mut attr: Vec<[f32; 4]> = data.nodes.iter().enumerate().map(|(i, nd)| {
            let sz = (nd.size.max(deg[i] as f32 * 0.5)).min(40.0);
            [sz, if nd.kind == "event" { 1.0 } else { 0.0 }, nd.state as f32, 0.0]
        }).collect();
        if attr.is_empty() { attr.push([0.0; 4]); }
        let mut edges: Vec<[f32; 4]> = data.links.iter().map(|l| [l.a as f32, l.b as f32, l.w, l.state as f32]).collect();
        let n_edges = edges.len() as u32;
        if edges.is_empty() { edges.push([0.0; 4]); }
        // CSR adjacency (undirected) for the spring forces
        let mut lists: Vec<Vec<u32>> = vec![vec![]; n.max(1)];
        for l in &data.links { lists[l.a].push(l.b as u32); lists[l.b].push(l.a as u32); }
        let mut off = vec![0u32];
        let mut adj: Vec<u32> = vec![];
        for l in &lists { adj.extend(l); off.push(adj.len() as u32); }
        if adj.is_empty() { adj.push(0); }

        let buf = |label: &str, bytes: &[u8], usage: wgpu::BufferUsages| device.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: Some(label), contents: bytes, usage });
        let st = wgpu::BufferUsages::STORAGE;
        let uniform = device.create_buffer(&wgpu::BufferDescriptor { label: Some("lg-u"), size: std::mem::size_of::<Uniforms>() as u64, usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST, mapped_at_creation: false });
        let sim0 = buf("lg-sim0", bytemuck::cast_slice(&sim), st | wgpu::BufferUsages::COPY_SRC | wgpu::BufferUsages::COPY_DST);
        let sim1 = buf("lg-sim1", bytemuck::cast_slice(&sim), st | wgpu::BufferUsages::COPY_SRC | wgpu::BufferUsages::COPY_DST);
        let attr_b = buf("lg-attr", bytemuck::cast_slice(&attr), st);
        let edge_b = buf("lg-edges", bytemuck::cast_slice(&edges), st);
        let off_b = buf("lg-off", bytemuck::cast_slice(&off), st);
        let adj_b = buf("lg-adj", bytemuck::cast_slice(&adj), st);
        let staging = device.create_buffer(&wgpu::BufferDescriptor { label: Some("lg-staging"), size: (sim.len() * 16) as u64, usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST, mapped_at_creation: false });
        let cbg = |inb: &wgpu::Buffer, outb: &wgpu::Buffer| device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: None, layout: &self.compute_bgl,
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: uniform.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 1, resource: inb.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 2, resource: outb.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 3, resource: attr_b.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 4, resource: off_b.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 5, resource: adj_b.as_entire_binding() },
            ],
        });
        let rbg = |s: &wgpu::Buffer| device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: None, layout: &self.render_bgl,
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: uniform.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 1, resource: s.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 2, resource: attr_b.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 3, resource: edge_b.as_entire_binding() },
            ],
        });
        let g = GpuGraph {
            version, n_nodes: n as u32, n_edges,
            compute_bg: [cbg(&sim0, &sim1), cbg(&sim1, &sim0)],
            render_bg: [rbg(&sim0), rbg(&sim1)],
            uniform, sim: [sim0, sim1], cur: 0, staging, rb: Rb::Idle, rb_ready: Arc::new(AtomicBool::new(false)),
        };
        self.graphs.insert(slot, g);
    }
}

/// Everything one draw of a graph needs. Cheap to build every frame; the graph data itself is uploaded only when `version` changes.
pub struct GraphCallback {
    pub slot: u8,
    pub data: Arc<GraphData>,
    pub version: u64,
    pub uniforms: Uniforms,
    pub steps: u32,                          // layout iterations this frame
    pub positions: Arc<Mutex<Vec<[f32; 2]>>>, // latest node positions read back from the GPU (for picking and labels)
    pub background_only: bool,
}

impl CallbackTrait for GraphCallback {
    fn prepare(&self, device: &wgpu::Device, queue: &wgpu::Queue, _sd: &ScreenDescriptor, encoder: &mut wgpu::CommandEncoder, res: &mut CallbackResources) -> Vec<wgpu::CommandBuffer> {
        let gpu = match res.get_mut::<Gpu>() { Some(g) => g, None => return vec![] };
        if !gpu.graphs.get(&self.slot).map(|g| g.version == self.version).unwrap_or(false) {
            gpu.upload(device, self.slot, &self.data, self.version);
            *self.positions.lock().unwrap() = Vec::new();
        }
        let (compute, graphs) = (&gpu.compute, &mut gpu.graphs);
        let g = graphs.get_mut(&self.slot).unwrap();
        let mut u = self.uniforms;
        u.n_nodes = g.n_nodes;
        u.n_edges = g.n_edges;
        queue.write_buffer(&g.uniform, 0, bytemuck::bytes_of(&u));

        if !self.background_only && g.n_nodes > 0 {
            for _ in 0..self.steps {
                let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor { label: Some("lg-layout"), timestamp_writes: None });
                pass.set_pipeline(compute);
                pass.set_bind_group(0, &g.compute_bg[g.cur], &[]);   // reads sim[cur], writes the other
                pass.dispatch_workgroups(g.n_nodes.div_ceil(64), 1, 1);
                drop(pass);
                g.cur = 1 - g.cur;
            }
            // read positions back, one stage per frame: copy -> map -> read
            let bytes = (g.n_nodes as u64) * 16;
            match g.rb {
                Rb::Idle => {
                    encoder.copy_buffer_to_buffer(&g.sim[g.cur], 0, &g.staging, 0, bytes);
                    g.rb = Rb::Copied;
                }
                Rb::Copied => {
                    let flag = g.rb_ready.clone();
                    g.staging.slice(..bytes).map_async(wgpu::MapMode::Read, move |r| { if r.is_ok() { flag.store(true, Ordering::SeqCst); } });
                    g.rb = Rb::Mapping;
                }
                Rb::Mapping => {
                    let _ = device.poll(wgpu::PollType::Poll);
                    if g.rb_ready.swap(false, Ordering::SeqCst) {
                        {
                            let view = g.staging.slice(..bytes).get_mapped_range().expect("mapped");
                            let f: &[f32] = bytemuck::cast_slice(&view);
                            let mut out = self.positions.lock().unwrap();
                            out.clear();
                            for i in 0..g.n_nodes as usize { out.push([f[i * 4], f[i * 4 + 1]]); }
                        }
                        g.staging.unmap();
                        g.rb = Rb::Idle;
                    }
                }
            }
        }
        vec![]
    }

    fn paint(&self, _info: eframe::egui::PaintCallbackInfo, pass: &mut wgpu::RenderPass<'static>, res: &CallbackResources) {
        let Some(gpu) = res.get::<Gpu>() else { return };
        let Some(g) = gpu.graphs.get(&self.slot) else { return };
        let bg = &g.render_bg[g.cur];
        pass.set_pipeline(&gpu.bg);
        pass.set_bind_group(0, bg, &[]);
        pass.draw(0..3, 0..1);
        if self.background_only { return; }
        if g.n_edges > 0 {
            pass.set_pipeline(&gpu.edge);
            pass.set_bind_group(0, bg, &[]);
            pass.draw(0..6, 0..g.n_edges);
        }
        if g.n_nodes > 0 {
            pass.set_pipeline(&gpu.node);
            pass.set_bind_group(0, bg, &[]);
            pass.draw(0..6, 0..g.n_nodes);
        }
    }
}
