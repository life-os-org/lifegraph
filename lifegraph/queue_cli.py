"""lifegraph-queue: seal one JSON entry from stdin into the mailbox. For other apps (e.g. a desktop console) that want to
record something without linking against the database. Input: {"op": "...", "args": {...}, "source": "...", "ts": 123}"""
from __future__ import annotations

import json
import sys
import time

from . import crypto
from .inbox import append_sealed


def main():
    if not crypto.exists():
        raise SystemExit("life graph not initialised")
    e = json.loads(sys.stdin.read())
    if e.get("op") not in ("log_event", "remember", "link", "note"):
        raise SystemExit(f"unknown op {e.get('op')!r}")
    append_sealed({"op": e["op"], "args": e.get("args") or {}, "ts": e.get("ts") or time.time(), "source": str(e.get("source") or "app")[:80]})


if __name__ == "__main__":
    main()
