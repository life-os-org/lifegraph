"""Optional: keep the unlock keys in the OS keyring (the login keyring) so the graph is open whenever you're logged in.

Without it you unlock by hand. Nothing here is required: on a machine with no keyring backend every call quietly does nothing."""
from __future__ import annotations

import json

from . import crypto

SERVICE = "lifegraph"


def _user() -> str:
    return f"keys:{crypto.home()}"       # one entry per data directory, so test sandboxes never touch the real one


def save(db_key_hex: str, secret_key_b64: str) -> bool:
    try:
        import keyring
        keyring.set_password(SERVICE, _user(), json.dumps({"db_key": db_key_hex, "secret_key": secret_key_b64}))
        return True
    except Exception:   # noqa: BLE001  (no backend, locked keyring, ...)
        return False


def load() -> tuple[str, str] | None:
    try:
        import keyring
        v = keyring.get_password(SERVICE, _user())
        d = json.loads(v) if v else None
        return (d["db_key"], d["secret_key"]) if d else None
    except Exception:   # noqa: BLE001
        return None


def clear() -> None:
    try:
        import keyring
        keyring.delete_password(SERVICE, _user())
    except Exception:   # noqa: BLE001
        pass
