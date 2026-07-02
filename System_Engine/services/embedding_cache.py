"""Persistent embedding cache for ChromaDB.

SQLite-backed cache that wraps any `chromadb.EmbeddingFunction`. Cache hits
skip the upstream embedding call entirely — survives ChromaDB wipes,
provider swaps and machine restarts so reindex never re-embeds text we've
already seen for the same model.

Cache key: ``sha256(model_name || NUL || text)``. The model-name prefix
isolates entries between providers (e.g. ``nomic-embed-text`` and
``text-embedding-004`` never collide).
"""

from __future__ import annotations

import array
import hashlib
import sqlite3
import threading
import time
from pathlib import Path

import chromadb


class CachedEmbeddingFunction(chromadb.EmbeddingFunction):
    def __init__(self, inner, model_name: str, db_path: Path):
        self.inner = inner
        self.model_name = model_name
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._init_schema()
        self.hits = 0
        self.misses = 0

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                    cache_key TEXT PRIMARY KEY,
                    embedding BLOB NOT NULL,
                    dim INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )

    def _key(self, text: str) -> str:
        h = hashlib.sha256()
        h.update(self.model_name.encode("utf-8"))
        h.update(b"\x00")
        h.update(text.encode("utf-8"))
        return h.hexdigest()

    @staticmethod
    def _pack(emb) -> bytes:
        return array.array("f", list(emb)).tobytes()

    @staticmethod
    def _unpack(blob: bytes) -> list[float]:
        a = array.array("f")
        a.frombytes(blob)
        return a.tolist()

    def _batch_get(self, keys: list[str]) -> dict[str, list[float]]:
        if not keys:
            return {}
        placeholders = ",".join(["?"] * len(keys))
        with self._connect() as conn:
            cur = conn.execute(
                f"SELECT cache_key, embedding FROM embeddings WHERE cache_key IN ({placeholders})",
                keys,
            )
            return {k: self._unpack(b) for k, b in cur.fetchall()}

    def _batch_put(self, entries: list[tuple[str, list[float]]]) -> None:
        if not entries:
            return
        now = int(time.time())
        rows = [(k, self._pack(e), len(e), now) for k, e in entries]
        with self._write_lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO embeddings (cache_key, embedding, dim, created_at) VALUES (?, ?, ?, ?)",
                rows,
            )

    @staticmethod
    def _to_python_floats(emb) -> list[float]:
        # ChromaDB validates `list[list[float]]` strictly — `np.float32`
        # elements slip past `isinstance(x, float)` and the call blows up
        # downstream. Normalize once so downstream sees plain Python floats.
        if hasattr(emb, "tolist"):
            return emb.tolist()
        return [float(x) for x in emb]

    def __call__(self, input: list[str]) -> list[list[float]]:
        keys = [self._key(t) for t in input]
        cached = self._batch_get(keys)

        miss_idx = [i for i, k in enumerate(keys) if k not in cached]
        if miss_idx:
            miss_texts = [input[i] for i in miss_idx]
            new_embs = self.inner(miss_texts)
            if len(new_embs) != len(miss_texts):
                raise RuntimeError(
                    f"Embedding function returned {len(new_embs)} vectors for {len(miss_texts)} inputs"
                )
            normalized = [self._to_python_floats(e) for e in new_embs]
            self._batch_put([(keys[i], normalized[j]) for j, i in enumerate(miss_idx)])
            for j, i in enumerate(miss_idx):
                cached[keys[i]] = normalized[j]

        self.hits += len(input) - len(miss_idx)
        self.misses += len(miss_idx)
        return [cached[k] for k in keys]

    def embed_query(self, input: list[str]) -> list[list[float]]:
        return self(input)

    def name(self) -> str:
        try:
            return self.inner.name()
        except AttributeError:
            return "CachedEmbeddingFunction"

    def stats(self) -> dict:
        with self._connect() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM embeddings")
            stored = cur.fetchone()[0]
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stored": stored,
            "model_name": self.model_name,
            "db_path": str(self.db_path),
        }
