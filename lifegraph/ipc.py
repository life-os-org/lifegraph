"""Tiny newline-delimited JSON protocol over a Unix socket (mode 600, in the per-user runtime dir)."""
from __future__ import annotations

import json
import socket

from . import crypto


def sock_path() -> str:
    return str(crypto.runtime_dir() / "sock")


def call(msg: dict, timeout: float = 15.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path())
    except (FileNotFoundError, ConnectionRefusedError):
        return {"ok": False, "error": "daemon-down", "message": "the life graph daemon isn't running (start it with `life start`)"}
    try:
        s.sendall(json.dumps(msg).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf or b"{}")
    finally:
        s.close()
