"""Key handling for the life graph.

One identity holds two secrets: a random 32-byte database key (SQLCipher) and an X25519 secret key (the "mailbox").
Both are wrapped under a passphrase (argon2id) and, separately, under a one-time recovery key. The public half of the
mailbox key sits in the clear so anything can seal an entry to it, but only an unlocked identity can open them.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
from pathlib import Path

from nacl import exceptions, pwhash, secret, public, utils

b64e = lambda b: base64.b64encode(b).decode()
b64d = lambda s: base64.b64decode(s.encode())

MIN_PASSPHRASE = 10


def _private(p: Path) -> None:
    # chmod bumps ctime even when the mode is unchanged, which fires inotify ATTRIB on the
    # directory; this runs every second from the daemon loop, so only touch it when it's wrong.
    if (p.stat().st_mode & 0o777) != 0o700:
        os.chmod(p, 0o700)


def home() -> Path:
    """Where the encrypted data lives. Overridable for tests."""
    p = Path(os.environ.get("LIFEGRAPH_HOME") or Path.home() / ".local/share/lifegraph")
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private(p)
    return p


def runtime_dir() -> Path:
    base = os.environ.get("LIFEGRAPH_RUNTIME") or os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/lifegraph-{os.getuid()}"
    p = Path(base) / "lifegraph"
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private(p)
    return p


def _kdf(secret_bytes: bytes, salt: bytes, ops: int, mem: int) -> bytes:
    return pwhash.argon2id.kdf(secret.SecretBox.KEY_SIZE, secret_bytes, salt, opslimit=ops, memlimit=mem)


def _wrap(kind: str, secret_bytes: bytes, payload: dict) -> dict:
    salt = utils.random(pwhash.argon2id.SALTBYTES)
    ops, mem = pwhash.argon2id.OPSLIMIT_MODERATE, pwhash.argon2id.MEMLIMIT_MODERATE
    key = _kdf(secret_bytes, salt, ops, mem)
    blob = secret.SecretBox(key).encrypt(json.dumps(payload).encode())
    return {"kind": kind, "salt": b64e(salt), "ops": ops, "mem": mem, "blob": b64e(blob)}


def _unwrap(w: dict, secret_bytes: bytes) -> dict:
    key = _kdf(secret_bytes, b64d(w["salt"]), w["ops"], w["mem"])
    return json.loads(secret.SecretBox(key).decrypt(b64d(w["blob"])))


class Keys:
    """Unlocked key material. Lives only in memory."""

    def __init__(self, db_key: bytes, secret_key: bytes):
        self.db_key = db_key
        self.secret_key = secret_key

    @property
    def db_key_hex(self) -> str:
        return self.db_key.hex()

    def box(self) -> public.SealedBox:
        return public.SealedBox(public.PrivateKey(self.secret_key))

    def zero(self):
        self.db_key = b"\0" * len(self.db_key)
        self.secret_key = b"\0" * len(self.secret_key)


def identity_path() -> Path:
    return home() / "identity.json"


def exists() -> bool:
    return identity_path().exists()


def create(passphrase: str) -> str:
    """Make a new identity. Returns the recovery key, which is shown once and never stored."""
    if len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"passphrase must be at least {MIN_PASSPHRASE} characters")
    if exists():
        raise FileExistsError("an identity already exists here")
    sk = public.PrivateKey.generate()
    payload = {"db_key": secrets.token_hex(32), "secret_key": b64e(bytes(sk))}
    recovery = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    ident = {
        "version": 1,
        "created": time.time(),
        "public_key": b64e(bytes(sk.public_key)),
        "wraps": [_wrap("passphrase", passphrase.encode(), payload), _wrap("recovery", recovery.encode(), payload)],
    }
    p = identity_path()
    p.write_text(json.dumps(ident, indent=1))
    os.chmod(p, 0o600)
    return recovery


def public_key() -> public.PublicKey:
    return public.PublicKey(b64d(json.loads(identity_path().read_text())["public_key"]))


def unlock(secret_text: str) -> Keys:
    """Open the identity with the passphrase or the recovery key."""
    ident = json.loads(identity_path().read_text())
    for w in ident["wraps"]:
        try:
            p = _unwrap(w, secret_text.encode())
            return Keys(bytes.fromhex(p["db_key"]), b64d(p["secret_key"]))
        except (exceptions.CryptoError, ValueError):
            continue
    raise PermissionError("wrong passphrase or recovery key")


def change_passphrase(old: str, new: str):
    if len(new) < MIN_PASSPHRASE:
        raise ValueError(f"passphrase must be at least {MIN_PASSPHRASE} characters")
    k = unlock(old)
    ident = json.loads(identity_path().read_text())
    payload = {"db_key": k.db_key_hex, "secret_key": b64e(k.secret_key)}
    ident["wraps"] = [_wrap("passphrase", new.encode(), payload)] + [w for w in ident["wraps"] if w["kind"] != "passphrase"]
    identity_path().write_text(json.dumps(ident, indent=1))
    k.zero()


# ---- the sealed mailbox: anyone can append, only an unlocked identity can read ----
INBOX_MAX = 64 * 1024


def seal(entry: dict) -> str:
    data = json.dumps(entry, separators=(",", ":")).encode()
    if len(data) > INBOX_MAX:
        raise ValueError("entry too large")
    return b64e(public.SealedBox(public_key()).encrypt(data))


def open_sealed(line: str, keys: Keys) -> dict:
    return json.loads(keys.box().decrypt(b64d(line.strip())))
