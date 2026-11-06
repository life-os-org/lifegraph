mod api;
mod app;
mod demo;
mod gpu;
mod model;
mod theme;

use eframe::egui;

fn main() -> eframe::Result {
    let args: Vec<String> = std::env::args().collect();
    let arg = |k: &str| args.iter().position(|a| a == k).and_then(|i| args.get(i + 1)).cloned();
    let opts = app::Opts {
        url: arg("--url"),
        demo: args.iter().any(|a| a == "--demo"),
        shot: arg("--shot"),
        shot_at: arg("--at").and_then(|s| s.parse().ok()).unwrap_or(4.0),
        mode: arg("--mode"),
        select: arg("--select"),
        query: arg("--query"),
        skip_boot: args.iter().any(|a| a == "--no-boot"),
    };
    if opts.url.is_none() && !opts.demo {
        eprintln!("usage: lifegraph-ui --url http://127.0.0.1:PORT/SECRET/   (run `life ui`)   |   lifegraph-ui --demo");
        std::process::exit(2);
    }
    let icon = image::load_from_memory(include_bytes!("../assets/icon.png")).ok().map(|i| {
        let rgba = i.to_rgba8();
        egui::IconData { width: rgba.width(), height: rgba.height(), rgba: rgba.into_raw() }
    });
    let mut vp = egui::ViewportBuilder::default()
        .with_title("LIFEGRAPH")
        .with_app_id("lifegraph-ui")
        .with_inner_size([1500.0, 920.0])
        .with_min_inner_size([980.0, 620.0]);
    if let Some(i) = icon { vp = vp.with_icon(i); }
    let native = eframe::NativeOptions {
        viewport: vp,
        renderer: eframe::Renderer::Wgpu,
        ..Default::default()
    };
    eframe::run_native("lifegraph", native, Box::new(move |cc| Ok(Box::new(app::App::new(cc, opts)))))
}
