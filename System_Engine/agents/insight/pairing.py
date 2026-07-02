"""Document pair sampling: random pairs, targeted pairs, target resolution, dedup keys.

Moved verbatim from agents/insight_agent.py (P2f). Mixin: methods keep
running on the InsightAgent instance (self.llm / self.rag / self.strategies /
self.insights_dir), so tests and behavior are unchanged.
"""

from __future__ import annotations

from typing import Any

import logging
import random
from itertools import combinations


class PairingMixin:
    # Contract: provided by the composed InsightAgent (BaseAgent state +
    # sibling mixins). Declared so each mixin documents what it needs.
    rag: Any
    _fetch_all_title_meta: Any
    _doc_from_rag_title: Any
    _parse_stored_tags: Any

    @staticmethod
    def _pair_key(a: dict, b: dict) -> tuple:
        """Order-independent dedup key for a document pair (audit R7-D — this
        idiom appeared verbatim at five sites)."""
        return tuple(sorted([a["title"], b["title"]]))

    def _sample_random_pairs(
        self, docs: list[dict], num_pairs: int, exclude: set | None = None
    ) -> list[tuple]:
        pairs: list[tuple[dict, dict]] = []
        exclude = exclude or set()
        max_attempts = num_pairs * 4

        for attempt in range(1, max_attempts + 1):
            if len(pairs) >= num_pairs:
                break
            a, b = random.sample(docs, 2)
            key = self._pair_key(a, b)
            if key in exclude:
                continue

            tags_a = set(a.get("tags", []))
            tags_b = set(b.get("tags", []))
            overlap = len(tags_a & tags_b) / max(len(tags_a | tags_b), 1)

            if overlap < 0.3 or attempt > num_pairs * 2:
                pairs.append((a, b))

        return pairs

    @staticmethod
    def _normalize_title(title: str) -> str:
        return (title or "").split("|", 1)[0].strip().lower()

    def _target_match_score(self, requested_title: str, candidate_title: str) -> int:
        requested = self._normalize_title(requested_title)
        candidate = self._normalize_title(candidate_title)
        if not requested or not candidate:
            return 0
        if candidate == requested:
            return 100
        if candidate == f"{requested} (stitched)":
            return 95
        if candidate == f"{requested} (synthesis)":
            return 90
        if requested in candidate or candidate in requested:
            return 60
        return 0

    def _resolve_target_doc(
        self,
        requested_title: str,
        all_docs: list[dict],
        title_meta: dict | None = None,
    ) -> dict | None:
        """Resolve one [[target]] to a representative document.

        Prefer exact / Stitched / Synthesis matches across the full RAG index,
        not only the sampled pool — otherwise a requested book that wasn't
        sampled gets silently dropped.
        """
        best_doc, best_score = None, 0

        for doc in all_docs:
            score = self._target_match_score(requested_title, doc.get("title", ""))
            if score > best_score:
                best_doc, best_score = doc, score

        if title_meta is None:
            title_meta = self._fetch_all_title_meta()

        for title, meta in title_meta.items():
            score = self._target_match_score(requested_title, title)
            if score > best_score:
                best_score = score
                best_doc = self._doc_from_rag_title(
                    title,
                    tags=self._parse_stored_tags((meta or {}).get("tags", "")),
                )

        if best_doc:
            logging.info(
                f"Monte Carlo: target '{requested_title}' resolved to '{best_doc['title']}'"
            )
            return best_doc

        try:
            similar = self.rag.query_similar_notes(requested_title, top_k=1)
        except Exception as e:
            logging.debug(f"Monte Carlo: semantic search for '{requested_title}' failed: {e}")
            similar = []

        if similar:
            logging.info(f"Monte Carlo: target '{requested_title}' resolved via semantic search")
            return {"title": requested_title, "content": similar[0][:2000], "tags": []}

        logging.warning(f"Monte Carlo: target '{requested_title}' not found.")
        return None

    def _build_targeted_pairs(
        self,
        all_docs: list[dict],
        target_titles: list[str],
        num_pairs: int,
        exclude: set | None = None,
        title_meta: dict | None = None,
    ) -> list[tuple]:
        exclude = exclude or set()

        target_docs = []
        seen_target_titles: set[str] = set()
        for title in target_titles:
            doc = self._resolve_target_doc(title, all_docs, title_meta=title_meta)
            if doc and doc["title"] not in seen_target_titles:
                target_docs.append(doc)
                seen_target_titles.add(doc["title"])

        if not target_docs:
            logging.warning(
                f"Monte Carlo: targets {target_titles} not found, falling back to random."
            )
            return self._sample_random_pairs(all_docs, num_pairs, exclude=exclude)

        target_title_set = {doc["title"] for doc in target_docs}
        other_docs = [
            doc
            for doc in all_docs
            if doc["title"] not in target_title_set
            and not any(self._target_match_score(t, doc["title"]) for t in target_titles)
        ]

        pairs: list[tuple] = []

        if len(target_docs) >= 2:
            all_combos = list(combinations(target_docs, 2))
            random.shuffle(all_combos)
            for a, b in all_combos:
                if self._pair_key(a, b) in exclude:
                    continue
                pairs.append((a, b))
                if len(pairs) >= num_pairs:
                    break

            if other_docs and len(pairs) < num_pairs:
                shuffled_targets = list(target_docs)
                random.shuffle(shuffled_targets)
                for target in shuffled_targets:
                    if len(pairs) >= num_pairs:
                        break
                    neighbor = random.choice(other_docs)
                    if self._pair_key(target, neighbor) not in exclude:
                        pairs.append((target, neighbor))
        else:
            target = target_docs[0]
            if other_docs:
                candidates = random.sample(other_docs, min(len(other_docs), num_pairs * 2))
                for other in candidates:
                    if len(pairs) >= num_pairs:
                        break
                    if self._pair_key(target, other) not in exclude:
                        pairs.append((target, other))
            if not pairs:
                # Last-resort partner for the target. Respect the exclude set
                # and avoid self-pairing (audit R7-D): a blind random.choice
                # could re-emit an already-explored pair and break cross-round
                # dedup. If nothing fresh exists, return empty — the caller
                # treats that as a stop signal.
                target = target_docs[0]
                for other in random.sample(all_docs, len(all_docs)):
                    if other["title"] == target["title"]:
                        continue
                    if self._pair_key(target, other) not in exclude:
                        pairs.append((target, other))
                        break

        return pairs[:num_pairs]

    # ── Spark / Expand ───────────────────────────────────────────────
