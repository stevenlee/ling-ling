"""Context retrieval by strategy method: recency / tag-cluster / island / random.

Moved verbatim from agents/insight_agent.py (P2f). Mixin: methods keep
running on the InsightAgent instance (self.llm / self.rag / self.strategies /
self.insights_dir), so tests and behavior are unchanged.
"""

from __future__ import annotations

from typing import Any

import logging
import random


from agents.insight.common import (
    _HASHTAG_RE,
    _SYSTEM_TAGS,
    _WIKILINK_RE,
)


class ContextAssemblyMixin:
    # Contract: provided by the composed InsightAgent (BaseAgent state +
    # sibling mixins). Declared so each mixin documents what it needs.
    rag: Any
    _book_root: Any
    _parse_stored_tags: Any

    def _get_context_by_method(self, method: str, limit: int, user_directive: str = "") -> str:
        target_file = None
        target_tag = None
        if file_matches := _WIKILINK_RE.findall(user_directive):
            target_file = file_matches[0].split("|")[0].strip()
            if target_file.lower().endswith(".md"):
                target_file = target_file[:-3]
        if tag_matches := _HASHTAG_RE.findall(user_directive):
            target_tag = tag_matches[0]

        if method == "recency":
            return self._get_recent_context(limit)
        if method == "tags":
            return self._get_tag_cluster_context(limit, target_tag)
        if method == "islands":
            return self._get_island_context(limit, target_file)
        return self._get_random_sample_context(limit, target_file)

    def _get_recent_context(self, limit: int) -> str:
        try:
            results = self.rag.all_chunks()
            if not results.get("documents"):
                return "No documents found."
            docs_with_meta = list(zip(results["documents"], results["metadatas"]))
            docs_with_meta.sort(key=lambda x: x[1].get("timestamp", ""), reverse=True)
            pool_size = min(len(docs_with_meta), limit * 3)
            selection = random.sample(docs_with_meta[:pool_size], min(pool_size, limit))
            return "\n---\n".join(x[0] for x in selection)
        except Exception as e:
            logging.debug(f"InsightAgent: recent context retrieval failed: {e}")
            return "No recent data found."

    def _get_tag_cluster_context(self, limit: int, target_tag: str | None = None) -> str:
        try:
            results = self.rag.all_chunks()
            if not results.get("metadatas"):
                return self._get_random_sample_context(limit)

            if not target_tag:
                # Count tags per book, not per chunk — otherwise a 1000-chunk
                # textbook makes every one of its single-book tags trivially
                # pass `c >= 2`, and `interesting` ends up dominated by tags
                # that only exist in one book (defeating "cluster").
                tag_books: dict[str, set[str]] = {}
                for meta in results["metadatas"]:
                    book = self._book_root(meta.get("title", ""))
                    for tag in self._parse_stored_tags(meta.get("tags", "")):
                        if tag.lower() in _SYSTEM_TAGS:
                            continue
                        tag_books.setdefault(tag, set()).add(book)
                if not tag_books:
                    return self._get_random_sample_context(limit)
                interesting = [t for t, books in tag_books.items() if len(books) >= 2]
                target_tag = random.choice(interesting if interesting else list(tag_books))

            cluster_docs = [
                doc
                for doc, meta in zip(results["documents"], results["metadatas"])
                if target_tag in self._parse_stored_tags(meta.get("tags", ""))
            ]
            if not cluster_docs:
                return self._get_random_sample_context(limit)
            selection = random.sample(cluster_docs, min(len(cluster_docs), limit))
            return f"Focusing on Cluster: #{target_tag}\n\n" + "\n---\n".join(selection)
        except Exception as e:
            logging.debug(f"InsightAgent: tag cluster retrieval failed: {e}")
            return self._get_random_sample_context(limit)

    def _get_island_context(self, limit: int, target_island: str | None = None) -> str:
        if target_island:
            try:
                results = self.rag.chunks_by_title(target_island, limit=limit)
            except Exception as e:
                logging.debug(f"InsightAgent: targeted island fetch failed: {e}")
                results = {}
            docs = results.get("documents", []) if results else []
            if docs:
                return (
                    f"Analysis target (Knowledge Island): [[{target_island}]]\n\n"
                    + "\n---\n".join(docs)
                )

        try:
            results = self.rag.all_chunks()
            if not results.get("documents"):
                return self._get_random_sample_context(limit)

            all_docs_meta = list(zip(results["documents"], results["metadatas"]))
            tag_to_titles: dict[str, set[str]] = {}
            title_to_entry: dict[str, tuple[str, list[str]]] = {}

            for doc, meta in all_docs_meta:
                title = meta.get("title", "Unknown")
                tags = self._parse_stored_tags(meta.get("tags", ""))
                title_to_entry[title] = (doc, tags)
                for tag in tags:
                    tag_to_titles.setdefault(tag, set()).add(title)

            connectivity = {}
            for title, (_, tags) in title_to_entry.items():
                connected = set()
                for tag in tags:
                    connected.update(tag_to_titles.get(tag, set()))
                connected.discard(title)
                connectivity[title] = len(connected)

            isolated = sorted(connectivity, key=lambda t: connectivity[t])
            island_titles = isolated[:limit]
            if not island_titles:
                return self._get_random_sample_context(limit)

            island_docs = []
            for title in island_titles:
                if title in title_to_entry:
                    doc, tags = title_to_entry[title]
                    island_docs.append(
                        f"### 🏝️ [[{title}]] (connectivity: {connectivity[title]})\n"
                        f"Tags: {', '.join(tags) if tags else '(none)'}\n\n{doc}"
                    )
            return "Knowledge Islands Detected (lowest connectivity scores):\n\n" + "\n---\n".join(
                island_docs
            )
        except Exception as e:
            logging.debug(f"InsightAgent: island detection failed: {e}")
            return self._get_random_sample_context(limit)

    def _get_random_sample_context(self, limit: int, target_file: str | None = None) -> str:
        try:
            if target_file:
                results = self.rag.chunks_by_title(target_file)
                docs = results.get("documents", [])
                if docs:
                    return f"Analysis target: [[{target_file}]]\n\n" + "\n---\n".join(docs)
            results = self.rag.all_chunks()
            docs = results.get("documents", [])
            if not docs:
                return "Empty KB."
            selection = random.sample(docs, min(len(docs), limit))
            return "\n---\n".join(selection)
        except Exception as e:
            logging.debug(f"InsightAgent: random sample retrieval failed: {e}")
            return "Error retrieving context."

    # ── Helpers ──────────────────────────────────────────────────────
