"""Skill/strategy loading + precondition gating.

Moved verbatim from agents/insight_agent.py (P2f). Mixin: methods keep
running on the InsightAgent instance (self.llm / self.rag / self.strategies /
self.insights_dir), so tests and behavior are unchanged.
"""

from __future__ import annotations

from typing import Any

import logging
import yaml


from agents.insight.common import (
    _SKILL_FRONTMATTER_RE,
)


class StrategyLoadingMixin:
    # Contract: provided by the composed InsightAgent (BaseAgent state +
    # sibling mixins). Declared so each mixin documents what it needs.
    rag: Any
    skills_dir: Any

    def _load_strategies(self) -> dict:
        if not self.skills_dir.exists():
            return {}

        strategies: dict = {}
        for filepath in self.skills_dir.glob("*.md"):
            try:
                content = filepath.read_text(encoding="utf-8")
                match = _SKILL_FRONTMATTER_RE.search(content)
                if not match:
                    continue
                yaml_data = yaml.safe_load(match.group(1))
                if not isinstance(yaml_data, dict) or "name" not in yaml_data:
                    continue
                yaml_data["system_prompt"] = content[match.end() :].strip()
                strategies[yaml_data["name"]] = yaml_data
            except Exception as e:
                logging.error(f"Failed to load skill {filepath.name}: {e}")
        return strategies

    # ── Public Entry Points ─────────────────────────────────────────────

    def _check_skill_preconditions(self, applicable_when: dict) -> list[str]:
        """Validate a skill's `applicable_when` frontmatter against the live
        vault. Returns a list of human-readable blockers (empty = runnable).

        Supported keys: `database_populated` (bool), `min_documents` (int,
        compared against indexed chunk count), `has_tag_graph` (bool).
        Unknown keys are ignored so skills can carry forward-compatible
        conditions without breaking older engines. Fail-open on RAG errors —
        a broken precondition check must not disable insights entirely.
        """
        if not applicable_when or not isinstance(applicable_when, dict) or self.rag is None:
            return []

        blockers: list[str] = []
        try:
            needs_count = applicable_when.get("database_populated") or (
                applicable_when.get("min_documents") is not None
            )
            count = self.rag.get_total_chunks_count() if needs_count else None

            if applicable_when.get("database_populated") and not count:
                blockers.append("知識庫是空的，請先 ingest 一些文件")

            min_docs = applicable_when.get("min_documents")
            if isinstance(min_docs, int) and count is not None and count < min_docs:
                blockers.append(f"需要至少 {min_docs} 份索引文件，目前只有 {count}")

            if applicable_when.get("has_tag_graph") and hasattr(self.rag, "has_tagged_documents"):
                if not self.rag.has_tagged_documents():
                    blockers.append("沒有任何帶標籤的文件，無法建立 tag graph")
        except Exception as e:
            logging.warning(f"Skill precondition check failed (allowing run): {e}")
            return []
        return blockers
