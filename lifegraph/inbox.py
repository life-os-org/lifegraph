"""The sealed mailbox, write side. Needs only the public key, so it is light and works while the graph is locked."""
from __future__ import annotations

import fcntl
import os

from . import crypto


def inbox_path():
    return crypto.home() / "inbox.log"


def append_sealed(entry: dict):
    """Writer side (used by chats, no key needed): seal to the public key and append one line."""
    line = crypto.seal(entry) + "\n"
    with open(inbox_path(), "a") as f:
        os.chmod(inbox_path(), 0o600)
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f, fcntl.LOCK_UN)


def pending() -> int:
    try:
        return sum(1 for _ in open(inbox_path()))
    except FileNotFoundError:
        return 0
