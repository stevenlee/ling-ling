"""Cortex recall — the READ side of long-term memory (Cortex Phase 5, F2).

`recall_claims` ranks the system's distilled Cortex claims by relevance to a
query. This is the reusable primitive: F2 (`@ling-recall`) renders it for the
user, and F1 (Cortex-grounded insight) / F3 (tension digest) consume the same
ranking. It deliberately returns the structured CortexPage (claim + its
epistemics: confidence, falsifiability, falsifier, contradictions, evidence) —
NOT raw RAG chunks — because the point of recall is the worldview *with* its
uncertainty, not the prose.

Embeddings go through the RAG embedding function, which is backed by the
persistent embedding cache — so re-embedding unchanged claims is a cache hit.
Fail-open: any failure returns an empty list, never raises.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.config import CORTEX_DIR
from services.bm25_index import tokenize
from services.cortex_store import CortexPage, load_all_pages


def _cosine(a, b) -> float:
    import numpy as np

    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom else 0.0


# Beliefs the system actively holds. `falsified` claims are disproven — excluded
# from "what do I believe" by default, but callers can ask for them.
_DEFAULT_STATUSES = ("active", "dormant")


def recall_claims(
    rag,
    query: str,
    *,
    cortex_dir: Path | None = None,
    top_k: int = 8,
    statuses: tuple[str, ...] | None = _DEFAULT_STATUSES,
    min_score: float = 0.0,
    hybrid: bool = True,
) -> list[tuple[float, CortexPage]]:
    """Top-k Cortex claims most relevant to `query`, as (cosine, page) pairs.

    `hybrid=True` fuses the embedding ranking with a lexical (BM25) ranking via
    RRF — the same hybrid the RAG layer uses — so a literal term overlap (e.g.
    "知識圖譜") can surface a claim the embedder's flat same-language band
    buries. The returned score is always the cosine similarity (interpretable),
    but ORDER reflects the fusion when hybrid is on.

    `statuses=None` includes every status. `min_score` filters on cosine.
    Returns [] on empty query, no pages, or embedding failure (fail-open).
    """
    cortex_dir = cortex_dir or CORTEX_DIR
    if not query or not query.strip():
        return []
    if not hasattr(rag, "ef"):
        return []

    pages = [p for p in load_all_pages(cortex_dir) if p.claim.strip()]
    if statuses is not None:
        pages = [p for p in pages if p.status in statuses]
    if not pages:
        return []

    try:
        # One batched call: query first, then every claim (cache-backed).
        vectors = rag.ef([query] + [p.claim for p in pages])
    except Exception as e:
        logging.warning(f"cortex_recall: embedding failed: {e}")
        return []
    if not vectors or len(vectors) != len(pages) + 1:
        logging.warning("cortex_recall: embedding count mismatch; skipping.")
        return []

    query_vec = vectors[0]
    cosine = {p.claim_id: _cosine(query_vec, vectors[i + 1]) for i, p in enumerate(pages)}
    cosine = {cid: s for cid, s in cosine.items() if s >= min_score}
    pages = [p for p in pages if p.claim_id in cosine]
    if not pages:
        return []

    if hybrid and len(pages) > 1:
        bm25 = _bm25_scores(query, pages)  # {claim_id: raw bm25}
        max_bm25 = max(bm25.values()) if bm25 else 0.0

        # MAGNITUDE-aware fusion, not rank-based RRF. At Cortex scale (~dozens
        # of claims) RRF's k=60 dampening flattens a strong, spiky BM25 signal
        # (a literal-term hit scoring 4x the runner-up) down to a rank-1-barely-
        # beats-rank-2 nudge that the embedder's flat same-language band then
        # overrides. Keeping BM25 magnitude lets a clear lexical match surface.
        # Cosine stays on its absolute scale so min_score still means something;
        # bm25 is scaled by its own max. No lexical overlap → bm25 all 0 →
        # falls back to pure cosine ordering.
        def fused(cid: str) -> float:
            b = (bm25.get(cid, 0.0) / max_bm25) if max_bm25 > 0 else 0.0
            return _W_VEC * cosine[cid] + _W_BM25 * b

        order = sorted(pages, key=lambda p: fused(p.claim_id), reverse=True)
    else:
        order = sorted(pages, key=lambda p: cosine[p.claim_id], reverse=True)

    return [(cosine[p.claim_id], p) for p in order[:top_k]]


# Fusion weights for recall (not the RAG-layer RRF). Balanced: the embedder
# carries conceptual matches, BM25 carries literal-term matches.
_W_VEC = 0.5
_W_BM25 = 0.5


def select_diverse(
    rag,
    scored_pages: list[tuple[float, CortexPage]],
    k: int,
    *,
    lambda_: float = 0.5,
) -> list[CortexPage]:
    """MMR-select k pages balancing query-relevance against mutual diversity.

    Pure top-k relevance repeatedly returns the same few CENTRAL claims — the
    generic "從 X 轉向 Y" hubs win every relevance race — so, over many runs,
    insights ground on a dozen claims while dozens more never anchor anything
    (2026-07-12 audit: top-4 cortex ids carried 88% of all grounding). MMR
    breaks that: because the hub claims are near-duplicates of one another,
    picking one makes the rest look redundant, so the second and third slots go
    to relevant-but-distinct claims. `scored_pages` must be sorted by relevance
    descending (recall_claims output). Fail-open: on any embedding failure,
    falls back to the incoming top-k order (the previous behavior).
    """
    if k <= 0 or not scored_pages:
        return []
    if len(scored_pages) <= k:
        return [p for _, p in scored_pages]
    try:
        import numpy as np

        vecs = rag.ef([p.claim for _, p in scored_pages])
        if not vecs or len(vecs) != len(scored_pages):
            raise ValueError("embedding count mismatch")
        mat = [np.asarray(v, dtype=float) for v in vecs]
        norms = [float(np.linalg.norm(v)) or 1.0 for v in mat]

        def sim(i: int, j: int) -> float:
            return float(np.dot(mat[i], mat[j])) / (norms[i] * norms[j])

        rel = [s for s, _ in scored_pages]
        selected: list[int] = []
        candidates = list(range(len(scored_pages)))
        while candidates and len(selected) < k:
            best_i, best_score = candidates[0], float("-inf")
            for i in candidates:
                diversity = max((sim(i, j) for j in selected), default=0.0)
                score = lambda_ * rel[i] - (1.0 - lambda_) * diversity
                if score > best_score:
                    best_score, best_i = score, i
            selected.append(best_i)
            candidates.remove(best_i)
        return [scored_pages[i][1] for i in selected]
    except Exception as e:
        logging.warning(f"cortex_recall.select_diverse: MMR failed, using top-k: {e}")
        return [p for _, p in scored_pages[:k]]


def _bm25_scores(query: str, pages: list[CortexPage]) -> dict[str, float]:
    """{claim_id: BM25 score} over claim text (char-level CJK tokens).

    Built fresh per call — trivial at Cortex scale. Empty dict on failure so
    fusion degrades to vector-only.
    """
    try:
        from rank_bm25 import BM25Okapi

        bm25 = BM25Okapi([tokenize(p.claim) for p in pages])
        scores = bm25.get_scores(tokenize(query))
        return {pages[i].claim_id: float(scores[i]) for i in range(len(pages))}
    except Exception as e:
        logging.warning(f"cortex_recall: BM25 scoring failed, vector-only: {e}")
        return {}
