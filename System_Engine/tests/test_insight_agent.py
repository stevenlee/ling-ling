"""Regression tests for the insight-mirror metadata bug.

When InsightAgent writes a report it also drops a copy in Insights/ for
Obsidian browsing. A regression introduced during refactor wrote the
Insights/ copy from just the report body, losing all the
title/type/version/date_created/stats frontmatter that _write_report
adds. For single-strategy runs the frontmatter was partial; for
generate_full_insight it was missing entirely.

These tests pin the contract: the Insights/ copy is byte-identical to
the canonical FROM_LLM_DIR report.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from agents.insight_agent import InsightAgent


@pytest.fixture
def stub_agent(tmp_path, monkeypatch):
    """An InsightAgent skeleton with both report directories redirected to tmp_path."""
    from core import config

    insights_dir = tmp_path / "Insights"
    from_llm_dir = tmp_path / "fromLingLing"
    insights_dir.mkdir()
    from_llm_dir.mkdir()
    monkeypatch.setattr(config, "FROM_LLM_DIR", from_llm_dir)
    # base_agent imported FROM_LLM_DIR at module load — patch that binding too.
    import agents.base_agent as base_agent_mod
    monkeypatch.setattr(base_agent_mod, "FROM_LLM_DIR", from_llm_dir)

    agent = InsightAgent.__new__(InsightAgent)
    agent.llm = _StubLLM()
    agent.rag = None
    agent.stats = {"input_chars": 0, "output_chars": 0}
    agent.insights_dir = insights_dir
    agent.skills_dir = tmp_path / "Skills"  # unused in these tests
    agent.strategies = {}
    return agent, from_llm_dir, insights_dir


class _StubLLM:
    """Minimal LLM stub: returns deterministic content; satisfies _self_correct calls."""
    model = "stub-model"

    def answer_query(self, *a, **kw):
        return "## Test Content\n\nBody."


class TestMirrorMetadata:
    def test_mirror_matches_canonical_report_bytes(self, stub_agent):
        """The Insights/ copy must be byte-identical to FROM_LLM_DIR."""
        agent, from_llm_dir, insights_dir = stub_agent

        path, full_markdown = agent._write_report(
            "Test Report", "Body of report.", "report_insight",
            {"exercise_strategy": "recency", "pipeline": "single"},
        )
        agent._mirror_to_insights(full_markdown, prefix="🎐insight")

        # Canonical report on disk equals the returned full_markdown.
        assert path.read_text(encoding="utf-8") == full_markdown

        # Insights/ copy equals the canonical report — same bytes.
        mirrored = list(insights_dir.glob("🎐insight-*.md"))
        assert len(mirrored) == 1
        assert mirrored[0].read_text(encoding="utf-8") == full_markdown

    def test_mirror_includes_canonical_frontmatter_fields(self, stub_agent):
        """The mirror must keep the fields _write_report adds (title, type,
        version, date_created, input_chars, output_chars), not just the
        caller's partial metadata."""
        agent, _, insights_dir = stub_agent

        _, full_markdown = agent._write_report(
            "Test Report", "Body.", "report_insight",
            {"exercise_strategy": "recency"},
        )
        agent._mirror_to_insights(full_markdown, prefix="🎐insight")

        mirror = next(insights_dir.iterdir()).read_text(encoding="utf-8")
        assert mirror.startswith("---\n"), "mirror should have YAML frontmatter"
        for field in ("title:", "type: report_insight", "version:", "date_created:",
                      "input_chars:", "output_chars:", "exercise_strategy: recency"):
            assert field in mirror, f"mirror missing {field!r}"

    def test_full_insight_mirror_has_frontmatter(self, stub_agent):
        """REGRESSION: generate_full_insight previously passed metadata={} to
        the mirror helper, which fell through to writing the raw body with no
        frontmatter at all. The mirror must always include the canonical
        report frontmatter."""
        agent, _, insights_dir = stub_agent

        # Simulate the no-extra-metadata case generate_full_insight uses.
        _, full_markdown = agent._write_report(
            "全方位洞察報告", "Body content.", "report_insight_full",
        )
        agent._mirror_to_insights(full_markdown, prefix="🎐full-insight")

        mirror = next(insights_dir.glob("🎐full-insight-*.md")).read_text(encoding="utf-8")
        assert mirror.startswith("---\n"), "full-insight mirror lost its frontmatter"
        assert "type: report_insight_full" in mirror
        assert "title:" in mirror
        assert "version:" in mirror


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
