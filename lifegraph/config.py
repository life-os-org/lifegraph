"""Small non-secret settings in config.json next to the graph (which capture backend to use, whether capture is on, ...).
Secrets (tokens, keys) never go in here."""
from __future__ import annotations

import copy
import json
import os

from . import crypto

DEFAULTS = {
    "capture": {"enabled": False, "backend": "claude", "model": "haiku", "ollama_model": "llama3.1", "command": "", "min_turns": 4, "agents": []},
    "semantic": {"backend": "auto", "ollama_model": "nomic-embed-text"},
}


def path():
    return crypto.home() / "config.json"


def load() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    try:
        saved = json.loads(path().read_text())
    except (FileNotFoundError, ValueError):
        saved = {}
    for sect, vals in saved.items():
        if isinstance(vals, dict):
            cfg.setdefault(sect, {}).update(vals)
    return cfg


def save(cfg: dict) -> None:
    p = path()
    p.write_text(json.dumps(cfg, indent=1))
    os.chmod(p, 0o600)


def update(section: str, **kv) -> dict:
    cfg = load()
    cfg[section].update(kv)
    save(cfg)
    return cfg
