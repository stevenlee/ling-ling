"""BM25 lexical index for hybrid retrieval.

Sits alongside ChromaDB's vector index. ChromaDB nails semantic
similarity; BM25 catches exact tokens (proper nouns, acronyms, code
identifiers, book titles) that embeddings tend to wash out. Reciprocal
Rank Fusion combines the two rankings without tuning a weight knob.

Rebuilt lazily from the collection on first query after any add/delete.
Rank_bm25 itself doesn't support incremental updates, so we re-build on
demand instead of trying to mutate; a few-thousand-chunk rebuild is
sub-second on commodity hardware.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict


# Tokenize Latin words (lowercased, alphanum+underscore runs) and treat
# each CJK character as its own token. Sufficient for BM25 matching
# without pulling in jieba; for technical wikis the win comes from
# acronyms/code-ids anyway.
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[一-鿿]")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


class BM25Index:
    def __init__(self, collection):
        self.collection = collection
        self._lock = threading.Lock()
        self._bm25 = None
        self._chunk_ids: list[str] = []
        self._dirty = True
        self._unavailable = False

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    def replace_collection(self, collection) -> None:
        with self._lock:
            self.collection = collection
            self._bm25 = None
            self._chunk_ids = []
            self._dirty = True

    def _build(self) -> None:
        if self._unavailable:
            return
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as e:
            logging.warning(f"BM25 disabled: rank_bm25 not installed ({e})")
            self._unavailable = True
            return
        try:
            results = self.collection.get(include=["documents"])
            chunk_ids = results.get("ids", []) or []
            documents = results.get("documents", []) or []
            if not documents:
                self._bm25 = None
                self._chunk_ids = []
                return
            tokenized = [tokenize(d) for d in documents]
            self._bm25 = BM25Okapi(tokenized)
            self._chunk_ids = chunk_ids
            logging.info(f"BM25: indexed {len(documents)} chunks")
        except Exception as e:
            logging.error(f"BM25 build failed: {e}")
            self._bm25 = None
            self._chunk_ids = []

    def query(self, text: str, top_k: int) -> list[tuple[str, float]]:
        with self._lock:
            if self._dirty:
                self._build()
                self._dirty = False
            if self._unavailable or self._bm25 is None:
                return []
            tokens = tokenize(text)
            if not tokens:
                return []
            scores = self._bm25.get_scores(tokens)
            ranked = sorted(
                ((i, float(s)) for i, s in enumerate(scores) if s > 0),
                key=lambda x: x[1],
                reverse=True,
            )[:top_k]
            return [(self._chunk_ids[i], s) for i, s in ranked]


def rrf_merge(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion. Each input is an ordered ID list.

    RRF score per id = sum over rankings of 1 / (k + rank). The constant
    k (default 60, per Cormack et al.) dampens early-rank dominance so
    fusion doesn't reduce to whichever ranker placed its #1 first.
    """
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] += 1.0 / (k + rank)
    return dict(scores)
