"""Retrieval stages for the hybrid RAG query pipeline (P2e).

The former 376-line RAGManager.query_notes body, decomposed into named
stages. RAGManager.query_notes remains the orchestrator; each stage here is
independently unit-testable. Stage order (all optional except the first):

    vector → cross-lingual variants → BM25 supplement → RRF fusion
           → facet dereference (owned by RAGManager: needs store helpers)
           → cross-encoder rerank → MMR diversify → per-document cap
           → trace record

`by_id` accumulates the candidate pool; `candidate_info` mirrors per-layer
signals into the trace breakdown.
"""

from __future__ import annotations

import logging

from services.bm25_index import rrf_merge


def resolve_variants(
    query_text: str,
    *,
    extra_queries: list[str] | None,
    cross_lingual: bool | None,
    default_enabled: bool,
    translator,
    target_langs,
) -> list[str]:
    """Extra query strings to fuse in. Explicit extra_queries win; otherwise
    translate when enabled and a translator is wired. Fail-open — never
    blocks mono-lingual retrieval."""
    if extra_queries:
        return [q for q in extra_queries if q and q.strip()]
    use_xl = default_enabled if cross_lingual is None else cross_lingual
    if use_xl and translator is not None:
        from services.cross_lingual import expand_queries

        return expand_queries(query_text, translator, target_langs)
    return []


def _fresh_info(*, vector_distance, vector_rank, layers: list[str]) -> dict:
    return {
        "vector_distance": vector_distance,
        "vector_rank": vector_rank,
        "bm25_score": None,
        "bm25_rank": None,
        "rrf_score": None,
        "rerank_score": None,
        "rerank_rank": None,
        "mmr_selected": False,
        "passed_layers": layers,
    }


def gather_vector_candidates(
    collection,
    query_text: str,
    variants: list[str],
    *,
    n_pool: int,
    where: dict | None,
    need_embeddings: bool,
) -> tuple[dict, dict, list[str], list[list[str]]]:
    """Primary vector retrieval + one query per cross-lingual variant.

    Returns (by_id, candidate_info, vec_ids, extra_vec_id_lists). Each
    variant's ranked id list feeds RRF later; the original query still
    drives reranking, so precision stays anchored to user intent.
    """
    include = ["documents", "metadatas", "distances"]
    if need_embeddings:
        include.append("embeddings")

    vec_results = collection.query(
        query_texts=[query_text],
        n_results=n_pool,
        where=where,
        include=include,
    )

    vec_ids = vec_results.get("ids", [[]])[0]
    documents = vec_results.get("documents", [[]])[0]
    metadatas = vec_results.get("metadatas", [[]])[0]
    distances = vec_results.get("distances", [[]])[0]
    embeddings = vec_results.get("embeddings", [[]])[0] if need_embeddings else []

    candidate_info: dict[str, dict] = {}
    by_id: dict[str, dict] = {}
    for i, cid in enumerate(vec_ids):
        c = {
            "text": documents[i] if i < len(documents) else "",
            "metadata": metadatas[i] if i < len(metadatas) else {},
            "distance": distances[i] if i < len(distances) else 0.0,
            "id": cid,
        }
        if need_embeddings and i < len(embeddings):
            c["embedding"] = embeddings[i]
        by_id[cid] = c
        candidate_info[cid] = _fresh_info(
            vector_distance=distances[i] if i < len(distances) else 0.0,
            vector_rank=i + 1,
            layers=["vector"],
        )

    extra_vec_id_lists: list[list[str]] = []
    for variant in variants:
        try:
            v_res = collection.query(
                query_texts=[variant], n_results=n_pool, where=where, include=include
            )
        except Exception as e:
            logging.debug(f"Cross-lingual variant query failed ({variant!r}): {e}")
            continue
        v_ids = v_res.get("ids", [[]])[0]
        v_docs = v_res.get("documents", [[]])[0]
        v_metas = v_res.get("metadatas", [[]])[0]
        v_dists = v_res.get("distances", [[]])[0]
        v_embs = v_res.get("embeddings", [[]])[0] if need_embeddings else []
        extra_vec_id_lists.append(v_ids)
        for i, cid in enumerate(v_ids):
            if cid not in by_id:
                c = {
                    "text": v_docs[i] if i < len(v_docs) else "",
                    "metadata": v_metas[i] if i < len(v_metas) else {},
                    "distance": v_dists[i] if i < len(v_dists) else 0.0,
                    "id": cid,
                }
                if need_embeddings and i < len(v_embs):
                    c["embedding"] = v_embs[i]
                by_id[cid] = c
                candidate_info[cid] = _fresh_info(
                    vector_distance=v_dists[i] if i < len(v_dists) else 0.0,
                    vector_rank=None,
                    layers=["vector_xlingual"],
                )
            elif "vector_xlingual" not in candidate_info[cid]["passed_layers"]:
                candidate_info[cid]["passed_layers"].append("vector_xlingual")

    return by_id, candidate_info, vec_ids, extra_vec_id_lists


def gather_bm25_candidates(
    bm25,
    collection,
    query_text: str,
    *,
    k: int,
    where: dict | None,
    by_id: dict,
    candidate_info: dict,
    need_embeddings: bool,
) -> list[str]:
    """BM25 lexical supplement: query the index, apply the where-filter via a
    membership check, fetch any chunks the vector stage didn't already load,
    and stamp per-candidate signals. Returns the ranked bm25 id list."""
    bm25_hits = bm25.query(query_text, k)
    bm25_score_map = {cid: score for cid, score in bm25_hits}
    raw_ids = [cid for cid, _ in bm25_hits]
    if where and raw_ids:
        try:
            filtered = collection.get(ids=raw_ids, where=where, include=[])
            allowed = set(filtered.get("ids", []) or [])
            bm25_ids = [cid for cid in raw_ids if cid in allowed]
        except Exception as e:
            logging.debug(f"BM25 where-filter failed, using unfiltered: {e}")
            bm25_ids = raw_ids
    else:
        bm25_ids = raw_ids

    missing = [cid for cid in bm25_ids if cid not in by_id]
    if missing:
        miss_include = ["documents", "metadatas"]
        if need_embeddings:
            miss_include.append("embeddings")
        try:
            miss = collection.get(ids=missing, include=miss_include)
            m_ids = miss.get("ids", []) or []
            m_docs = miss.get("documents", []) or []
            m_metas = miss.get("metadatas", []) or []
            m_embs = miss.get("embeddings", []) or []
            for i, cid in enumerate(m_ids):
                new_c = {
                    "text": m_docs[i] if i < len(m_docs) else "",
                    "metadata": m_metas[i] if i < len(m_metas) else {},
                    "distance": 0.0,
                    "id": cid,
                }
                if need_embeddings and i < len(m_embs):
                    new_c["embedding"] = m_embs[i]
                by_id[cid] = new_c
        except Exception as e:
            logging.debug(f"BM25 chunk fetch failed: {e}")

    for i, cid in enumerate(bm25_ids):
        if cid not in candidate_info:
            candidate_info[cid] = _fresh_info(
                vector_distance=None, vector_rank=None, layers=["bm25"]
            )
            candidate_info[cid]["bm25_score"] = bm25_score_map.get(cid)
            candidate_info[cid]["bm25_rank"] = i + 1
        else:
            candidate_info[cid]["bm25_score"] = bm25_score_map.get(cid)
            candidate_info[cid]["bm25_rank"] = i + 1
            candidate_info[cid]["passed_layers"].append("bm25")

    return bm25_ids


def fuse_rankings(
    by_id: dict,
    candidate_info: dict,
    vec_ids: list[str],
    extra_vec_id_lists: list[list[str]],
    bm25_ids: list[str],
    *,
    use_hybrid: bool,
) -> tuple[list[dict], dict[str, float]]:
    """RRF over the primary vector ranking + every cross-lingual variant
    ranking + (when hybrid) the BM25 ranking. Runs whenever there is more
    than one ranking to fuse; otherwise preserves pure vector order."""
    if use_hybrid or extra_vec_id_lists:
        rankings = [vec_ids, *extra_vec_id_lists]
        if use_hybrid:
            rankings.append(bm25_ids)
        rrf_scores = rrf_merge(rankings)
        for cid, rrf_s in rrf_scores.items():
            if cid in candidate_info:
                candidate_info[cid]["rrf_score"] = rrf_s

        ordered_ids = sorted(by_id.keys(), key=lambda c: rrf_scores.get(c, 0.0), reverse=True)
        return [by_id[cid] for cid in ordered_ids], rrf_scores
    return [by_id[cid] for cid in vec_ids if cid in by_id], {}


def apply_rerank(reranker, query_text: str, candidates: list[dict], candidate_info: dict) -> None:
    """Cross-encoder re-scoring against the ORIGINAL query; sorts in place."""
    scores = reranker.score(query_text, [c["text"] for c in candidates])
    for c, s in zip(candidates, scores):
        c["rerank_score"] = s
        cid = c["id"]
        if cid in candidate_info:
            candidate_info[cid]["rerank_score"] = s
            candidate_info[cid]["passed_layers"].append("rerank")
    candidates.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
    for idx, c in enumerate(candidates):
        cid = c["id"]
        if cid in candidate_info:
            candidate_info[cid]["rerank_rank"] = idx + 1


def doc_key(candidate: dict) -> str:
    """Base-document identity for a chunk, for the per-document cap.

    Derived from the title with any trailing ``(Part N)``/``(Synthesis)``/
    ``(Stitched)`` parenthetical stripped — so all chunks of one document
    share a key while genuinely distinct documents (incl. different
    language editions like ``Siddhartha(EN)`` vs ``Siddhartha(DE)``) stay
    separate. Falls back to source, then chunk id.
    """
    meta = candidate.get("metadata") or {}
    title = meta.get("title")
    if title:
        base = str(title).rsplit(" (", 1)[0] if str(title).endswith(")") else str(title)
        if base.strip():
            return base.strip()
    return str(meta.get("source") or candidate.get("id") or id(candidate))


def cap_per_document(candidates: list[dict], cap: int) -> list[dict]:
    """Keep at most ``cap`` chunks per base-document, preserving order.

    Order-preserving and any-match-safe: a document's first ``cap`` (best-
    ranked) chunks are kept, later ones dropped, letting lower-ranked
    distinct documents move up. Never reorders within the kept set.
    """
    seen: dict[str, int] = {}
    out: list[dict] = []
    for c in candidates:
        k = doc_key(c)
        n = seen.get(k, 0)
        if n >= cap:
            continue
        seen[k] = n + 1
        out.append(c)
    return out


def mmr_select(
    candidates: list[dict],
    top_k: int,
    lambda_param: float,
    *,
    query_emb=None,
    relevance: list[float] | None = None,
) -> list[dict]:
    """Maximal Marginal Relevance over candidate embeddings.

    Pass ``query_emb`` to derive relevance from cosine-to-query, OR
    ``relevance`` to use a precomputed score (e.g. reranker output).
    Cosine sim for the diversity term is computed on length-normalized
    vectors so this works regardless of ChromaDB's hnsw:space setting.
    """
    import numpy as np

    usable_with_idx = [(i, c) for i, c in enumerate(candidates) if c.get("embedding") is not None]
    if not usable_with_idx:
        return candidates[:top_k]
    usable = [c for _, c in usable_with_idx]

    embs = np.asarray([c["embedding"] for c in usable], dtype=np.float32)
    norms = np.linalg.norm(embs, axis=1, keepdims=True) + 1e-9
    embs_norm = embs / norms

    if relevance is not None:
        rel = np.asarray(
            [relevance[i] for i, _ in usable_with_idx],
            dtype=np.float32,
        )
        if len(rel) > 1:
            lo, hi = float(rel.min()), float(rel.max())
            rel = (rel - lo) / (hi - lo) if hi > lo else np.zeros_like(rel)
    elif query_emb is not None:
        q = np.asarray(query_emb, dtype=np.float32)
        q_norm = q / (np.linalg.norm(q) + 1e-9)
        rel = embs_norm @ q_norm
    else:
        raise ValueError("mmr_select requires either query_emb or relevance")

    selected_idx: list[int] = []
    remaining = list(range(len(usable)))

    while remaining and len(selected_idx) < top_k:
        if not selected_idx:
            best = remaining[int(np.argmax(rel[remaining]))]
        else:
            sel = embs_norm[selected_idx]
            cand = embs_norm[remaining]
            sim_to_selected = (cand @ sel.T).max(axis=1)
            mmr = lambda_param * rel[remaining] - (1 - lambda_param) * sim_to_selected
            best = remaining[int(np.argmax(mmr))]
        selected_idx.append(best)
        remaining.remove(best)

    return [usable[i] for i in selected_idx]


def record_retrieval_trace(
    trace_store,
    *,
    query_text: str,
    top_k: int,
    options: dict,
    results: list[dict],
    status: str,
    error: str | None = None,
) -> None:
    """Fail-soft trace of one retrieval event (success or failure)."""
    try:
        trace_id = None
        if hasattr(trace_store, "current_trace_ids"):
            trace_ids = trace_store.current_trace_ids()
            if trace_ids:
                trace_id = trace_ids[-1]
        kwargs = dict(
            query_text=query_text,
            top_k=top_k,
            options=options,
            results=results,
            trace_id=trace_id,
            status=status,
        )
        if error is not None:
            kwargs["error"] = error
        trace_store.record_retrieval_event(**kwargs)
    except Exception as e:
        logging.debug(f"Failed to record retrieval event ({status}): {e}")
