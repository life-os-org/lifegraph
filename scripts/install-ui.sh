#!/usr/bin/env bash
# Builds the native GPU window (Rust + wgpu) and links it as `lifegraph-ui`; `life ui` then opens it instead of the browser page.
# Needs: rust (cargo), a Vulkan/Metal-capable GPU driver.
# Berkeley Mono is a commercial font and is NOT in this repo. If you own it and it is installed, this copies YOUR copy into the build
# (embedded in your binary only). Without it the app uses a bundled open monospace font.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
fonts="$here/ui/assets/fonts"
mkdir -p "$fonts"
for w in Regular Bold; do
  f="$(fc-list 2>/dev/null | grep -i "BerkeleyMono-$w.otf" | head -1 | cut -d: -f1 || true)"
  [ -n "$f" ] && [ -f "$f" ] && cp -n "$f" "$fonts/" && echo "using your Berkeley Mono $w"
done
command -v cargo >/dev/null || { echo "install Rust first: https://rustup.rs"; exit 1; }
( cd "$here/ui" && cargo build --release )
mkdir -p ~/.local/bin
ln -sf "$here/ui/target/release/lifegraph-ui" ~/.local/bin/lifegraph-ui
echo "built. Run: life ui   (or  lifegraph-ui --demo  to look around with made-up data)"
