"""Tests for the LLM/IO-free logic inside services.ingestion_pipeline.

We don't mock LLM or RAG here — only exercise the pure helpers:
  - _build_navigation
  - _build_part_metadata
  - format_digest_appendix / _format_one_digest
  - _extract_stitchable_body
  - _demote_headings
  - _source_span_for_chunk / _format_source_range
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from services.ingestion_pipeline import IngestionPipeline


@pytest.fixture
def pipeline():
    """A pipeline instance with no LLM/RAG wired in — for pure-helper testing."""
    return IngestionPipeline.__new__(IngestionPipeline)


# ── _build_navigation ──────────────────────────────────────────────

class TestBuildNavigation:
    def test_single_page_links_to_original(self):
        nav = IngestionPipeline._build_navigation("MyDoc", part_info=None)
        assert "[[MyDoc|查看完整原始檔" in nav
        # No part-specific navigation.
        assert "上一篇" not in nav
        assert "下一篇" not in nav

    def test_first_part_only_shows_next(self):
        nav = IngestionPipeline._build_navigation(
            "Doc", part_info={"current": 1, "total": 3, "master_tags": []}
        )
        assert "下一篇" in nav
        assert "上一篇" not in nav
        # Links back to Synthesis / Stitched / Original.
        assert "(Synthesis)" in nav
        assert "(Stitched)" in nav

    def test_middle_part_shows_both_neighbours(self):
        nav = IngestionPipeline._build_navigation(
            "Doc", part_info={"current": 2, "total": 3, "master_tags": []}
        )
        assert "上一篇" in nav
        assert "下一篇" in nav

    def test_last_part_only_shows_previous(self):
        nav = IngestionPipeline._build_navigation(
            "Doc", part_info={"current": 3, "total": 3, "master_tags": []}
        )
        assert "上一篇" in nav
        assert "下一篇" not in nav


# ── _build_part_metadata ───────────────────────────────────────────

class TestBuildPartMetadata:
    def test_single_page_omits_part_fields(self):
        meta = IngestionPipeline._build_part_metadata(
            "X (Synthesis)", "entity", ["a"], part_info=None, quality_fixes=[]
        )
        assert meta["title"] == "X (Synthesis)"
        assert "part" not in meta
        assert "parts_count" not in meta

    def test_part_includes_part_fields_and_source_span(self):
        meta = IngestionPipeline._build_part_metadata(
            "X (Part 2)",
            "entity",
            ["a"],
            part_info={
                "current": 2,
                "total": 5,
                "master_tags": ["a"],
                "source_span": {
                    "source_start_char": 100,
                    "source_end_char": 500,
                    "source_start_line": 5,
                    "source_end_line": 20,
                },
            },
            quality_fixes=["trailing_whitespace"],
        )
        assert meta["part"] == 2
        assert meta["parts_count"] == 5
        assert meta["source_start_char"] == 100
        assert meta["source_end_line"] == 20
        assert meta["quality_fixes"] == ["trailing_whitespace"]

    def test_quality_fixes_omitted_when_empty(self):
        meta = IngestionPipeline._build_part_metadata(
            "X", "entity", [], part_info=None, quality_fixes=[]
        )
        assert "quality_fixes" not in meta


# ── format_digest_appendix ─────────────────────────────────────────

class TestFormatDigestAppendix:
    def test_empty_returns_empty(self, pipeline):
        assert pipeline.format_digest_appendix([]) == ""

    def test_dict_digest_renders_sections(self, pipeline):
        out = pipeline.format_digest_appendix([{
            "part": 1,
            "title": "Intro",
            "thesis": "Central claim.",
            "key_points": ["one", "two"],
            "evidence": ["e"],
            "terms": [],
            "open_questions": [],
            "handoff": "next",
        }])
        assert "## 🧩 Part Digest Appendix" in out
        assert "### Part 1: Intro" in out
        assert "**Thesis**: Central claim." in out
        assert "  - one" in out
        # Empty sections do NOT emit a header.
        assert "**Terms**" not in out
        assert "**Handoff**: next" in out

    def test_string_digest_passthrough(self, pipeline):
        out = pipeline.format_digest_appendix(["raw summary text"])
        assert "### Part 1" in out
        assert "raw summary text" in out

    def test_string_value_in_list_field(self, pipeline):
        """If `key_points` arrives as a string (not a list), it should still render."""
        out = pipeline.format_digest_appendix([{
            "part": 1,
            "thesis": "T",
            "key_points": "single point",
        }])
        assert "  - single point" in out


# ── _extract_stitchable_body ───────────────────────────────────────

class TestExtractStitchableBody:
    def test_strips_frontmatter_and_navigation(self, pipeline):
        content = (
            "---\ntitle: X (Part 1)\ntags: [a]\n---\n\n"
            "# Main Heading\n\nBody text here.\n\n"
            "---\n## 🔗 知識導航\n*   📄 [[Original]]\n"
        )
        body = pipeline._extract_stitchable_body(content)
        assert "title: X" not in body
        assert "🔗 知識導航" not in body
        assert "Body text here." in body

    def test_strips_digest_appendix(self, pipeline):
        content = (
            "---\ntitle: X\n---\n\n# Main\n\nBody.\n\n"
            "## 🧩 Part Digest Appendix\n\nstuff\n"
        )
        body = pipeline._extract_stitchable_body(content)
        assert "Part Digest Appendix" not in body
        assert "stuff" not in body
        assert "Body." in body

    def test_demotes_headings_by_two_levels(self, pipeline):
        content = "# H1\n\nbody\n\n## H2\n\nmore"
        body = pipeline._extract_stitchable_body(content)
        # # → ###, ## → ####
        assert "### H1" in body
        assert "#### H2" in body

    def test_accepts_path(self, pipeline, tmp_path):
        p = tmp_path / "part.md"
        p.write_text("---\ntitle: X\n---\n\n# Main\n\nBody.\n", encoding="utf-8")
        body = pipeline._extract_stitchable_body(p)
        assert "Body." in body


# ── _demote_headings ───────────────────────────────────────────────

class TestDemoteHeadings:
    def test_caps_at_h6(self):
        # ##### + 2 = ####### → capped at ######
        out = IngestionPipeline._demote_headings("##### Five", levels=2)
        assert out == "###### Five"

    def test_default_level_is_one(self):
        out = IngestionPipeline._demote_headings("# Top", levels=1)
        assert out == "## Top"

    def test_no_change_for_non_headings(self):
        out = IngestionPipeline._demote_headings("plain text\nno headings", levels=2)
        assert out == "plain text\nno headings"


# ── _source_span_for_chunk ─────────────────────────────────────────

class TestSourceSpanForChunk:
    def test_lines_one_indexed(self):
        text = "line1\nline2\nline3\nline4"
        span = IngestionPipeline._source_span_for_chunk(
            text, {"start": 0, "end": 11}, 1,
        )
        assert span["part"] == 1
        assert span["source_start_line"] == 1  # start of line1
        # end=11 is right at end of line2; count('\n', 0, 11) = 1, +1 = line 2.
        assert span["source_end_line"] == 2

    def test_handles_missing_keys(self):
        span = IngestionPipeline._source_span_for_chunk("hi", {}, 5)
        assert span["part"] == 5
        assert span["source_start_char"] == 0
        assert span["source_end_char"] == 0


# ── _format_source_range ───────────────────────────────────────────

class TestFormatSourceRange:
    def test_both_line_and_char(self):
        out = IngestionPipeline._format_source_range({
            "source_start_line": 1,
            "source_end_line": 10,
            "source_start_char": 0,
            "source_end_char": 200,
        })
        assert "lines 1-10" in out
        assert "Original chars: 0-200" in out
        assert out.endswith("\n\n")

    def test_lines_only(self):
        out = IngestionPipeline._format_source_range({"source_start_line": 1, "source_end_line": 5})
        assert "lines 1-5" in out
        assert "chars" not in out

    def test_empty_meta_returns_empty(self):
        assert IngestionPipeline._format_source_range({}) == ""


# ── Critique post-step ────────────────────────────────────────────


class _StubLLM:
    """Minimal stub matching the LLMClient surface _run_synthesis_critique uses."""

    def __init__(self, critique_response="", raise_exc=None):
        self.critique_response = critique_response
        self.raise_exc = raise_exc
        self.last_call = None

    @staticmethod
    def _format_part_digest_for_prompt(digest):
        return f"DIGEST::{digest}"

    def critique_text(self, candidate, sources, focus=None):
        self.last_call = {"candidate": candidate, "sources": sources, "focus": focus}
        if self.raise_exc:
            raise self.raise_exc
        return self.critique_response


class TestParseVerdict:
    def test_english_revise(self):
        assert IngestionPipeline._parse_verdict(
            "* [major] foo\n\n**Overall Verdict**: revise. Some reason."
        ) == "revise"

    def test_english_keep_no_emphasis(self):
        assert IngestionPipeline._parse_verdict("Overall Verdict: keep") == "keep"

    def test_zh_revise(self):
        assert IngestionPipeline._parse_verdict(
            "**Overall Verdict**：修訂。需要補充某些細節。"
        ) == "revise"

    def test_zh_keep(self):
        assert IngestionPipeline._parse_verdict("Overall Verdict: 保留") == "keep"

    def test_reject(self):
        assert IngestionPipeline._parse_verdict("**Overall Verdict**: reject — fundamentally off.") == "reject"

    def test_unparseable_returns_none(self):
        assert IngestionPipeline._parse_verdict("No verdict line here.") is None


class TestQualityFixDedupe:
    def test_dedupes_structured_fixes_preserving_order(self):
        fixes = [
            {"type": "a", "line": 1, "before": "x", "after": "y"},
            {"type": "b", "line": 2},
            {"type": "a", "line": 1, "before": "x", "after": "y"},
            "legacy",
            "legacy",
        ]

        assert IngestionPipeline._dedupe_quality_fixes(fixes) == [
            {"type": "a", "line": 1, "before": "x", "after": "y"},
            {"type": "b", "line": 2},
            "legacy",
        ]


class TestRunSynthesisCritique:
    def test_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", False)
        pipe = IngestionPipeline.__new__(IngestionPipeline)
        pipe.llm = _StubLLM(critique_response="* [critical] foo\n**Overall Verdict**: revise")
        section, verdict = pipe._run_synthesis_critique("X", "candidate text", ["d1"])
        assert section == ""
        assert verdict is None
        assert pipe.llm.last_call is None  # never called

    def test_empty_digests_skips(self, monkeypatch):
        monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
        pipe = IngestionPipeline.__new__(IngestionPipeline)
        pipe.llm = _StubLLM(critique_response="ignored")
        section, verdict = pipe._run_synthesis_critique("X", "candidate", [])
        assert (section, verdict) == ("", None)
        assert pipe.llm.last_call is None

    def test_empty_candidate_skips(self, monkeypatch):
        monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
        pipe = IngestionPipeline.__new__(IngestionPipeline)
        pipe.llm = _StubLLM(critique_response="ignored")
        section, verdict = pipe._run_synthesis_critique("X", "   ", ["d1"])
        assert (section, verdict) == ("", None)

    def test_success_path(self, monkeypatch):
        monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
        pipe = IngestionPipeline.__new__(IngestionPipeline)
        critique = (
            "* [major] core concepts → flattens performance into paralysis\n"
            "* [minor] mermaid → subgraph mis-closed\n\n"
            "**Overall Verdict**: revise. The synthesis erased two distinctions present in the digests."
        )
        pipe.llm = _StubLLM(critique_response=critique)
        section, verdict = pipe._run_synthesis_critique("Hamlet", "synth body", ["d1", "d2"])
        assert verdict == "revise"
        assert section.startswith("## 🔍 Quality Critique")
        assert "Overall Verdict" in section
        # Sources should have been pre-formatted via the stub's formatter.
        assert "DIGEST::d1" in pipe.llm.last_call["sources"]
        assert "DIGEST::d2" in pipe.llm.last_call["sources"]
        assert pipe.llm.last_call["candidate"] == "synth body"

    def test_llm_exception_is_swallowed(self, monkeypatch):
        monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
        pipe = IngestionPipeline.__new__(IngestionPipeline)
        pipe.llm = _StubLLM(raise_exc=RuntimeError("network down"))
        section, verdict = pipe._run_synthesis_critique("X", "candidate", ["d1"])
        assert (section, verdict) == ("", None)

    def test_failure_marker_response_is_skipped(self, monkeypatch):
        """LLMClient.critique_text returns 'Critique failed: ...' on error — treat as no critique."""
        monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", True)
        pipe = IngestionPipeline.__new__(IngestionPipeline)
        pipe.llm = _StubLLM(critique_response="Critique failed: bad gateway")
        section, verdict = pipe._run_synthesis_critique("X", "candidate", ["d1"])
        assert (section, verdict) == ("", None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
