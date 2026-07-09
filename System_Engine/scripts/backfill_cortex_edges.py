#!/usr/bin/env python3
"""O0 backfill: re-link the existing Cortex belief graph at the lowered
threshold, so the ~65 stranded claim nodes gain their missing edges.

Why this exists (see DesignDoc/Ontology_SemanticEntropy_implementation_plan.md):
the old neighbor floor (0.80) filtered out cross-domain pairs, so the graph
grew almost no edges (contradictions empty, related on 4/65). Lowering the
link threshold fixes NEW claims going forward; this script applies the same
adjudication to the EXISTING backlog. It never merges — only adds typed edges.

Safe to run while the daemon is up, with caveats:
  - Does NOT construct RAGManager / open ChromaDB (single-writer; the daemon
    owns it). A shim provides `ef` (embeddings straight to Ollama).
  - Does NOT touch the daemon's consolidation state file. Embeddings are
    recomputed in-memory and discarded.
  - DOES need a real LLM (adjudication) and DOES share the adjudication cache
    (content-addressed, additive) and write Cortex/*.md (VaultWatcher reindexes
    through its own single-writer path).
  - Best run outside the 1–5am dreaming window so it doesn't race the nightly
    consolidation writing the same pages.

Usage:
  python scripts/backfill_cortex_edges.py --dry-run            # count pairs, no LLM/writes
  python scripts/backfill_cortex_edges.py --report             # + connected-components stats
  python scripts/backfill_cortex_edges.py --max-adjudications 400
  python scripts/backfill_cortex_edges.py --link-threshold 0.60
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from core.config import (  # noqa: E402
    CORTEX_ADJUDICATION_CACHE,
    CORTEX_DIR,
    CORTEX_NEIGHBOR_TOP_K,
    EMBEDDING_MAX_CHARS,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    settings,
)
from maintenance.cortex_consolidation import _Consolidator, _load_json, _save_json  # noqa: E402
from services.llm_client import LLMClient  # noqa: E402
from services.rag.embedding import build_embedding_function  # noqa: E402

OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


class _NoChromaRAG:
    """_Consolidator needs only `.ef` for backfill (we skip page indexing)."""

    def __init__(self):
        self.ef = build_embedding_function(
            provider=EMBEDDING_PROVIDER,
            model=EMBEDDING_MODEL,
            ollama_api_base=OLLAMA_API_BASE,
            gemini_api_key=GEMINI_API_KEY,
            max_chars=EMBEDDING_MAX_CHARS,
            cache_enabled=False,
            cache_db_path=None,
        )


def _connected_components(pages) -> tuple[int, int]:
    """(#components, largest component size) over related+contradiction edges."""
    ids = {p.claim_id for p in pages}
    adj: dict = {p.claim_id: set() for p in pages}
    for p in pages:
        for nid in list(p.related) + list(p.contradictions):
            if nid in ids:
                adj[p.claim_id].add(nid)
                adj[nid].add(p.claim_id)
    seen: set = set()
    sizes = []
    for start in adj:
        if start in seen:
            continue
        stack, size = [start], 0
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            size += 1
            stack.extend(adj[n] - seen)
        sizes.append(size)
    return len(sizes), (max(sizes) if sizes else 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="count candidate pairs; no LLM/writes"
    )
    parser.add_argument("--report", action="store_true", help="print connected-components stats")
    parser.add_argument("--max-adjudications", type=int, default=400)
    parser.add_argument("--link-threshold", type=float, default=None, help="override Scripture")
    parser.add_argument("--top-k", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    link_threshold = (
        args.link_threshold if args.link_threshold is not None else settings.CORTEX_LINK_THRESHOLD
    )
    top_k = args.top_k if args.top_k is not None else CORTEX_NEIGHBOR_TOP_K

    llm = None if args.dry_run else LLMClient()
    cache = {} if args.dry_run else _load_json(CORTEX_ADJUDICATION_CACHE, {})

    worker = _Consolidator(
        llm,
        _NoChromaRAG(),
        cortex_dir=CORTEX_DIR,
        state={},  # throwaway: don't touch the daemon's consolidation state
        adjudication_cache=cache,
        max_adjudications=args.max_adjudications,
        top_k=top_k,
        link_threshold=link_threshold,
        merge_threshold=settings.CORTEX_MERGE_THRESHOLD,
        max_variants=0,
    )

    if args.report:
        comps, largest = _connected_components(worker.pages)
        print(f"BEFORE: {len(worker.pages)} nodes, {comps} components, largest={largest}")

    stats = worker.relink_all_pages(dry_run=args.dry_run)

    if not args.dry_run:
        _save_json(CORTEX_ADJUDICATION_CACHE, cache)

    print(
        f"\nlink_threshold={link_threshold} top_k={top_k} | "
        f"candidate pairs={stats['pairs']} adjudicated={stats['adjudicated']} "
        f"related+={stats['related']} contradictions+={stats['contradictions']}"
        f"{' (dry-run)' if args.dry_run else ''}"
        f"{' [QUOTA HIT — re-run to continue]' if stats['quota_hit'] else ''}"
    )
    if args.report and not args.dry_run:
        # reload from disk to reflect the edges just written
        from services.cortex_store import load_all_pages

        comps, largest = _connected_components(load_all_pages(CORTEX_DIR))
        print(f"AFTER:  {len(worker.pages)} nodes, {comps} components, largest={largest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
