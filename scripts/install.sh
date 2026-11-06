#!/usr/bin/env bash
# Installs the `life` command and the (not auto-started) systemd user service.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
[ -x "$here/.venv/bin/life" ] || { python3 -m venv "$here/.venv" && "$here/.venv/bin/python" -m pip install -q -e "$here"; }
mkdir -p ~/.local/bin ~/.config/systemd/user ~/.local/share/lifegraph
chmod 700 ~/.local/share/lifegraph
ln -sf "$here/.venv/bin/life" ~/.local/bin/life
sed "s|%h/projects/lifegraph|$here|g" "$here/scripts/lifegraphd.service" > ~/.config/systemd/user/lifegraphd.service
systemctl --user daemon-reload
echo "installed. Next: life init"
