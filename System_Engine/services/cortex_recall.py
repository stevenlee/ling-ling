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
) -> list[tuple[float, CortexPage]]:
    """Top-k Cortex claims most relevant to `query`, as (score, page) pairs.

    `statuses=None` includes every status (e.g. to surface falsified beliefs).
    Returns [] on empty query, no pages, or any embedding failure (fail-open).
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
    scored = [
        (_cosine(query_vec, vectors[i + 1]), page)
        for i, page in enumerate(pages)
    ]
    scored = [(s, p) for s, p in scored if s >= min_score]
    scored.sort(key=lambda sp: sp[0], reverse=True)
    return scored[:top_k]
