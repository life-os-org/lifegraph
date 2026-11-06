// Berkeley Mono is a commercial font: it is never committed. If you own it, `scripts/install-ui.sh` copies your installed copy into
// assets/fonts/ and it is embedded in YOUR build. Without it the app falls back to a bundled open monospace font.
fn main() {
    println!("cargo:rustc-check-cfg=cfg(no_berkeley)");
    println!("cargo:rerun-if-changed=assets/fonts");
    if !std::path::Path::new("assets/fonts/BerkeleyMono-Regular.otf").exists() {
        println!("cargo:rustc-cfg=no_berkeley");
        println!("cargo:warning=Berkeley Mono not found in ui/assets/fonts; using the fallback monospace font");
    }
}
