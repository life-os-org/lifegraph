"""Embeddings for meaning-based search, all local.

  fastembed  a small ONNX model (BAAI/bge-small-en-v1.5, ~130 MB, downloaded once on first use): real semantic search
  ollama     a local Ollama server's embedding model
  lite       character n-gram hashing, no download: finds typos, word forms and shared words, NOT meanings
  auto       fastembed if it is installed and loads, otherwise lite

Pick one with `life semantic backend NAME` (or LIFEGRAPH_EMBED=NAME). Vectors are stored inside the encrypted graph database."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.request

import numpy as np

from . import config


class Lite:
    name, dim = "lite-ngram-512", 512

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            t = re.sub(r"\s+", " ", t.lower())
            grams = [t[j:j + 3] for j in range(max(len(t) - 2, 1))] + re.findall(r"\w+", t)
            for g in grams:
                h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=4).digest(), "little")
                out[i, h % self.dim] += 1.0 if h & 0x80000000 else -1.0
        n = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.maximum(n, 1e-9)


class Fast:
    MODEL = "BAAI/bge-small-en-v1.5"
    dim = 384

    def __init__(self):
        from fastembed import TextEmbedding   # ImportError -> caller falls back
        from . import crypto
        self.m = TextEmbedding(self.MODEL, cache_dir=str(crypto.home() / "models"))      # inside the data folder: the service can write there
        self.name = f"fastembed:{self.MODEL}"

    def embed(self, texts: list[str]) -> np.ndarray:
        v = np.array(list(self.m.embed(texts)), dtype=np.float32)
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


class Ollama:
    def __init__(self, model: str):
        self.model, self.name, self.dim = model, f"ollama:{model}", 0
        self.embed(["probe"])

    def embed(self, texts: list[str]) -> np.ndarray:
        rows = []
        for t in texts:
            req = urllib.request.Request("http://127.0.0.1:11434/api/embeddings", json.dumps({"model": self.model, "prompt": t}).encode(),
                                         {"Content-Type": "application/json"})
            rows.append(json.loads(urllib.request.urlopen(req, timeout=60).read())["embedding"])
        v = np.array(rows, dtype=np.float32)
        self.dim = v.shape[1]
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


_lock = threading.Lock()
_cached: tuple[str, object, str] | None = None     # (wanted, embedder, note)


def get() -> tuple[object, str]:
    """(embedder, note). Loads once; falls back to `lite` with a note saying why."""
    global _cached
    cfg = config.load()["semantic"]
    want = os.environ.get("LIFEGRAPH_EMBED") or cfg["backend"]
    with _lock:
        if _cached and _cached[0] == want:
            return _cached[1], _cached[2]
        emb, note = None, ""
        if want in ("auto", "fastembed"):
            try:
                emb = Fast()
            except Exception as e:   # noqa: BLE001  (not installed, offline on first download, ...)
                note = f"fastembed unavailable ({type(e).__name__}); using lite matching. `pip install 'lifegraph[semantic]'` for real semantic search."
        elif want == "ollama":
            try:
                emb = Ollama(cfg["ollama_model"])
            except Exception as e:   # noqa: BLE001
                note = f"ollama unavailable ({type(e).__name__}); using lite matching."
        elif want != "lite":
            note = f"unknown backend {want!r}; using lite matching."
        emb = emb or Lite()
        _cached = (want, emb, note)
        return emb, note
