"""Vault/RAG document loading: title metadata, book parts, per-title chunks.

Moved verbatim from agents/insight_agent.py (P2f). Mixin: methods keep
running on the InsightAgent instance (self.llm / self.rag / self.strategies /
self.insights_dir), so tests and behavior are unchanged.
"""

from __future__ import annotations

from typing import Any

import logging
import random


from agents.insight.common import (
    _BOOK_SUFFIX_RE,
    _STITCHED_SUFFIX_RE,
    _SYNTHESIS_SUFFIX_RE,
)


class DocRetrievalMixin:
    # Contract: provided by the composed InsightAgent (BaseAgent state +
    # sibling mixins). Declared so each mixin documents what it needs.
    rag: Any

    def _fetch_all_title_meta(self) -> dict[str, dict]:
        """Single metadata scan: title → first-seen metadata dict.

        Used by both `_get_all_documents` and `_resolve_target_doc` so a
        Monte Carlo run with N targets doesn't issue N+1 full scans.
        """
        try:
            results = self.rag.all_chunks(include=("metadatas",))
            metadatas = results.get("metadatas", []) or []
        except Exception as e:
            logging.error(f"Monte Carlo: failed to fetch metadata: {e}")
            return {}

        out: dict[str, dict] = {}
        for meta in metadatas:
            title = (meta or {}).get("title", "Unknown")
            out.setdefault(title, meta)
        return out

    def _get_all_documents(
        self,
        max_docs: int = 50,
        chunks_per_book: int = 5,
        title_meta: dict | None = None,
    ) -> list[dict]:
        """Sample books uniformly, then up to `chunks_per_book` concept chunks per book.

        Book-level uniform sampling prevents a 143-Part book from drowning
        out a 5-Part note. Within a sampled book, multiple chunks are drawn
        from raw Parts (preferred) so the carrier pool reflects concept-level
        diversity rather than just one distilled summary per book.
        """
        if title_meta is None:
            title_meta = self._fetch_all_title_meta()
        if not title_meta:
            return []

        book_to_titles: dict[str, list[str]] = {}
        for title in title_meta:
            book_to_titles.setdefault(self._book_root(title), []).append(title)

        book_roots = list(book_to_titles)
        target_books = max(1, max_docs // max(chunks_per_book, 1))
        if len(book_roots) > target_books:
            book_roots = random.sample(book_roots, target_books)

        docs = []
        for book in book_roots:
            docs.extend(self._docs_from_book(book_to_titles[book], title_meta, chunks_per_book))

        logging.info(
            f"Monte Carlo: {len(title_meta)} titles across {len(book_to_titles)} books, "
            f"loaded {len(docs)} chunks from {len(book_roots)} sampled books "
            f"(chunks_per_book={chunks_per_book})"
        )
        return docs

    @staticmethod
    def _book_root(title: str) -> str:
        """Strip `(Part N)` / `(Stitched)` / `(Synthesis)` so book parts collapse."""
        return _BOOK_SUFFIX_RE.sub("", title or "").strip()

    def _docs_from_book(
        self,
        book_titles: list[str],
        title_meta: dict,
        k: int,
    ) -> list[dict]:
        """Return up to k chunk docs from one book.

        Tier priority: raw Parts > (Synthesis) > (Stitched). Raw Parts win
        because they preserve unrefined concepts; the distilled tiers compress
        many concepts into one view and dampen collision novelty.
        """
        stitched = [t for t in book_titles if _STITCHED_SUFFIX_RE.search(t)]
        synthesis = [t for t in book_titles if _SYNTHESIS_SUFFIX_RE.search(t)]
        stitched_set = set(stitched)
        synthesis_set = set(synthesis)
        parts = [t for t in book_titles if t not in stitched_set and t not in synthesis_set]

        tier = parts or synthesis or stitched
        if not tier:
            return []

        chosen_titles = random.sample(tier, min(k, len(tier)))
        docs = []
        for t in chosen_titles:
            tags = self._parse_stored_tags(title_meta[t].get("tags", ""))
            doc = self._doc_from_rag_title(t, tags=tags)
            if doc:
                docs.append(doc)
        return docs

    def _doc_from_rag_title(self, title: str, tags: list[str] | None = None) -> dict | None:
        """Fetch one representative chunk for an exact indexed title."""
        try:
            chunk_results = self.rag.chunks_by_title(title, include=("documents", "metadatas"))
        except Exception as e:
            logging.debug(f"Monte Carlo: failed to fetch chunk for '{title}': {e}")
            return None

        chunk_docs = chunk_results.get("documents", []) or []
        if not chunk_docs:
            return None
        metadatas = chunk_results.get("metadatas", []) or []
        if tags is None:
            tags = self._parse_stored_tags((metadatas[0] if metadatas else {}).get("tags", ""))
        return {
            "title": title,
            "content": random.choice(chunk_docs)[:2000],
            "tags": tags,
        }

    # ── Sampling ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_stored_tags(tags_str: str) -> list[str]:
        """Parse the ',tag1,tag2,tag3,' format used in ChromaDB metadata."""
        if not tags_str:
            return []
        return [t.strip() for t in tags_str.split(",") if t.strip()]
