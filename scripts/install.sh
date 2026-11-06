#!/usr/bin/env bash
# Installs the `life` command and a systemd user service that starts at login (it stays locked until you `life init` / unlock).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
[ -x "$here/.venv/bin/life" ] || { python3 -m venv "$here/.venv" && { "$here/.venv/bin/python" -m pip install -q -e "$here[semantic]" || "$here/.venv/bin/python" -m pip install -q -e "$here"; }; }
mkdir -p ~/.local/bin ~/.config/systemd/user ~/.local/share/lifegraph
chmod 700 ~/.local/share/lifegraph
ln -sf "$here/.venv/bin/life" ~/.local/bin/life
sed "s|%h/projects/lifegraph|$here|g" "$here/scripts/lifegraphd.service" > ~/.config/systemd/user/lifegraphd.service
systemctl --user daemon-reload
systemctl --user enable lifegraphd >/dev/null 2>&1 || true
echo "installed. Next: life init"
