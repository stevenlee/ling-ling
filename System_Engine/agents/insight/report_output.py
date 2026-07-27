"""Insight report mirroring, filenames, signals metadata, learning artifacts.

Moved verbatim from agents/insight_agent.py (P2f). Mixin: methods keep
running on the InsightAgent instance (self.llm / self.rag / self.strategies /
self.insights_dir), so tests and behavior are unchanged.
"""

from __future__ import annotations

from typing import Any

import logging
import re
from datetime import datetime
from pathlib import Path

from services.ingest.atomic_io import atomic_write_text


class InsightArtifactPartialCommit(RuntimeError):
    """Canonical report committed, but its indexable Insights mirror failed."""

    def __init__(self, canonical_path: Path, mirror_error: Exception):
        self.canonical_path = canonical_path
        super().__init__(
            f"Insight report committed at {canonical_path}, but the Insights mirror "
            f"still needs repair: {mirror_error}"
        )


class ReportOutputMixin:
    # Contract: provided by the composed InsightAgent (BaseAgent state +
    # sibling mixins). Declared so each mixin documents what it needs.
    llm: Any
    rag: Any
    insights_dir: Any
    _write_report: Any

    def _write_and_mirror_report(
        self,
        title: str,
        body: str,
        report_type: str,
        metadata: dict | None = None,
        *,
        requested_cmd: str,
        related_titles: list[str] | None = None,
    ) -> str:
        """Commit the canonical report, then its derived indexable mirror.

        The canonical file is the commit point. A later mirror failure is
        surfaced as a partial commit so callers never report that nothing was
        applied.
        """
        canonical_path, full_markdown = self._write_report(title, body, report_type, metadata)
        try:
            self._mirror_to_insights(
                full_markdown,
                requested_cmd=requested_cmd,
                related_titles=related_titles,
            )
        except Exception as exc:
            raise InsightArtifactPartialCommit(canonical_path, exc) from exc
        return full_markdown

    def _signals_meta(self, content: str, target_titles, config: dict | None = None) -> dict:
        """Signals metadata block ({} when disabled). Shared by
        generate_insight and generate_full_insight (audit R7-D — the two were
        byte-for-byte duplicates).

        `config` is the operation's skill frontmatter (None for full-insight,
        which mixes strategies). `refute_mode: lenient` in it switches the
        refute check to non-literal mode — see refute_insight for why.
        """
        from core.config import INSIGHT_SIGNALS_ENABLED

        if not INSIGHT_SIGNALS_ENABLED:
            return {}
        from services.insight_signals import compute_signals

        lenient = ((config or {}).get("refute_mode") or "").strip().lower() == "lenient"
        kind = (config or {}).get("name") if lenient else None
        signals = compute_signals(
            content,
            target_titles,
            self.rag,
            self.llm,
            refute_lenient=lenient,
            refute_kind=kind,
        )
        return {
            "signals": {
                "groundedness": round(signals.groundedness, 4)
                if signals.groundedness is not None
                else None,
                "novelty": round(signals.novelty, 4) if signals.novelty is not None else None,
                "bridging": round(signals.bridging, 4) if signals.bridging is not None else None,
                "refute_verdict": signals.refute_verdict,
            },
            "signals_version": 1,
        }

    def _maybe_artifact(self, content: str) -> str:
        """Phase 6 auto-attach: a learning-aid section for the insight body, or
        "" when Scripture's `visual_router` is off (zero LLM calls). Fail-open —
        a visual is a bonus, never block the insight report on it."""
        try:
            from services.learning_artifacts import maybe_artifact_section

            section = maybe_artifact_section(self.llm, content)
            return f"\n\n---\n\n{section}" if section else ""
        except Exception as e:
            logging.warning(f"insight artifact auto-attach failed: {e}")
            return ""

    def _mirror_to_insights(
        self,
        full_markdown: str,
        requested_cmd: str | None = None,
        related_titles: list[str] | None = None,
        prefix: str | None = None,
    ) -> None:
        """Drop a byte-identical copy of the canonical report in Insights/.

        We re-write the same full markdown (frontmatter + body) that
        `_write_report` just wrote to FROM_LLM_DIR, so the Insights/ copy
        stays indexable in Obsidian with the full title/type/version/stats
        frontmatter.
        """
        insight_file = self.insights_dir / self._build_insights_filename(
            requested_cmd=requested_cmd or self._cmd_from_legacy_prefix(prefix),
            related_titles=related_titles,
        )
        atomic_write_text(insight_file, full_markdown)

    @classmethod
    def _build_insights_filename(
        cls,
        *,
        requested_cmd: str,
        related_titles: list[str] | None = None,
    ) -> str:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        related = cls._related_doc_name(related_titles)
        cmd = cls._sanitize_filename_part(requested_cmd) or "insight"
        return f"[{timestamp}][{related}][{cmd}].md"

    @classmethod
    def _related_doc_name(cls, related_titles: list[str] | None) -> str:
        titles = [
            cleaned
            for title in (related_titles or [])
            if (cleaned := cls._sanitize_filename_part(str(title)))
        ]
        if not titles:
            return "Vault"
        return "+".join(titles)

    @staticmethod
    def _sanitize_filename_part(value: str) -> str:
        from core.vault_utils import sanitize_filename

        # sanitize_filename reduces math and strips path-hostile chars + controls;
        # brackets are additionally neutralized since the filename format is
        # bracket-delimited ([ts][related][cmd]).
        cleaned = re.sub(r"[\[\]]+", "-", sanitize_filename(value)).strip(" .-")
        return cleaned[:80].strip(" .-")

    @staticmethod
    def _cmd_from_legacy_prefix(prefix: str | None) -> str:
        if not prefix:
            return "insight"
        return prefix.removeprefix("🎐").strip("-") or "insight"

    # ── Pipeline: Planner Preview ─────────────────────────────────────
