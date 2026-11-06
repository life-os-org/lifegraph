"""A keyring backend that lives in a JSON file, so the daemon and the CLI (separate processes) share it inside a test sandbox."""
import json, os
from keyring.backend import KeyringBackend

class FileKeyring(KeyringBackend):
    priority = 10
    def _p(self): return os.environ["FAKERING_FILE"]
    def _all(self):
        try: return json.load(open(self._p()))
        except FileNotFoundError: return {}
    def get_password(self, service, username): return self._all().get(f"{service}|{username}")
    def set_password(self, service, username, password):
        d = self._all(); d[f"{service}|{username}"] = password; json.dump(d, open(self._p(), "w"))
    def delete_password(self, service, username):
        d = self._all(); d.pop(f"{service}|{username}", None); json.dump(d, open(self._p(), "w"))
