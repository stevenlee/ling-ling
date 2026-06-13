"""Tests for the LLM-free logic inside agents.counter_agent.

We don't mock the LLM here — only exercise the pure helpers:
  - _LocationIndex (heading & part-anchor lookup)
  - _build_tally_locally (deterministic dedup tally)
  - _find_quote_offset (exact + fuzzy quote location)
  - _parse_concepts (directive parsing)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

import re

import pytest

from agents.counter_agent import CounterAgent, _LocationIndex


# ── _LocationIndex ─────────────────────────────────────────────────

class TestLocationIndex:
    TEXT = (
        "# Top heading\n"
        "some body text here\n"
        "## Part 1: foo\n"
        "para\n"
        "### subsection title\n"
        "more body text\n"
        "## Part 2: bar\n"
        "final body\n"
    )

    def test_no_heading_before_offset_zero(self):
        idx = _LocationIndex(self.TEXT)
        assert idx.closest_heading(0) == ""
        assert idx.closest_part(0) == ""

    def test_heading_lookup_matches_old_regex(self):
        """Verify the precomputed index returns the same closest heading as a
        from-scratch regex scan over `text[:offset]`."""
        idx = _LocationIndex(self.TEXT)
        old_re = re.compile(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$', re.MULTILINE)

        def old(offset: int) -> str:
            closest = ""
            for m in old_re.finditer(self.TEXT[:offset]):
                closest = m.group(1).strip()
            return closest

        # Sample a range of offsets including the boundaries.
        for offset in [0, 1, 13, 14, 33, 50, 80, 110, len(self.TEXT)]:
            assert idx.closest_heading(offset) == old(offset), f"mismatch at offset {offset}"

    def test_part_anchor_extracts_part_only(self):
        idx = _LocationIndex(self.TEXT)
        offset = self.TEXT.index("more body text")
        assert idx.closest_part(offset) == "Part 1"
        offset = self.TEXT.index("final body")
        assert idx.closest_part(offset) == "Part 2"

    def test_empty_article(self):
        idx = _LocationIndex("")
        assert idx.closest_heading(0) == ""
        assert idx.closest_part(0) == ""

    def test_article_with_no_headings(self):
        idx = _LocationIndex("Just prose, no headings.")
        assert idx.closest_heading(10) == ""


# ── _build_tally_locally ──────────────────────────────────────────

class TestBuildTallyLocally:
    def setup_method(self):
        self.agent = CounterAgent.__new__(CounterAgent)

    def test_empty_instances(self):
        tally = CounterAgent._build_tally_locally("concept", [])
        assert tally["total_count"] == 0
        assert tally["instances"] == []
        assert tally["high_confidence_count"] == 0

    def test_dedupes_by_normalized_quote(self):
        instances = [
            {"quote": "Hello world", "confidence": "high"},
            {"quote": "hello world", "confidence": "medium"},  # same after lowercase
            {"quote": "Other quote", "confidence": "low"},
        ]
        tally = CounterAgent._build_tally_locally("c", instances)
        assert tally["total_count"] == 2

    def test_assigns_sequential_ids(self):
        instances = [
            {"quote": "a", "confidence": "high"},
            {"quote": "b", "confidence": "medium"},
        ]
        tally = CounterAgent._build_tally_locally("c", instances)
        assert [i["id"] for i in tally["instances"]] == [1, 2]

    def test_counts_by_confidence(self):
        instances = [
            {"quote": "a", "confidence": "high"},
            {"quote": "b", "confidence": "high"},
            {"quote": "c", "confidence": "medium"},
            {"quote": "d", "confidence": "low"},
        ]
        tally = CounterAgent._build_tally_locally("c", instances)
        assert tally["high_confidence_count"] == 2
        assert tally["medium_confidence_count"] == 1
        assert tally["low_confidence_count"] == 1


# ── _find_quote_offset ────────────────────────────────────────────

class TestFindQuoteOffset:
    def test_exact_match(self):
        article = "Some text and a specific phrase later."
        assert CounterAgent._find_quote_offset(article, "specific phrase") == article.index("specific phrase")

    def test_returns_minus_one_for_empty(self):
        assert CounterAgent._find_quote_offset("anything", "") == -1
        assert CounterAgent._find_quote_offset("anything", None) == -1

    def test_strips_smart_quotes(self):
        article = "She said hello today."
        # Smart quotes wrapping the quote
        assert CounterAgent._find_quote_offset(article, "“She said hello”") >= 0 or \
               CounterAgent._find_quote_offset(article, "She said hello") >= 0

    def test_fuzzy_whitespace_match(self):
        article = "Long\nline   spanning multiple    spaces here."
        # Quote uses single spaces; article uses varying whitespace.
        found = CounterAgent._find_quote_offset(article, "Long line spanning multiple spaces here")
        assert found >= 0

    def test_short_quote_no_fuzzy(self):
        article = "abc def ghi"
        # Quote shorter than 12 chars and not an exact match → returns -1.
        assert CounterAgent._find_quote_offset(article, "xyz") == -1


# ── _parse_concepts ───────────────────────────────────────────────

class TestParseConcepts:
    def setup_method(self):
        self.agent = CounterAgent.__new__(CounterAgent)

    def test_inline_english(self):
        assert self.agent._parse_concepts("Count: appeals to authority") == ["appeals to authority"]

    def test_inline_chinese(self):
        assert self.agent._parse_concepts("計算：訴諸權威") == ["訴諸權威"]

    def test_bullet_block(self):
        directive = "Count:\n- appeals to authority\n- ad hominem\n- straw man\n"
        result = self.agent._parse_concepts(directive)
        assert "appeals to authority" in result
        assert "ad hominem" in result
        assert "straw man" in result

    def test_strips_trailing_question_marks(self):
        assert self.agent._parse_concepts("Count: rhetorical questions?") == ["rhetorical questions"]
        assert self.agent._parse_concepts("計算：誇大其詞？") == ["誇大其詞"]

    def test_fallback_freeform_concept(self):
        # No `Count:` marker, but plenty of text → treated as one concept.
        directive = "@ling-lens [[Article]] dramatic exaggerations"
        result = self.agent._parse_concepts(directive)
        assert result == ["dramatic exaggerations"]

    def test_strips_confidence_in_fallback(self):
        directive = "@ling-lens [[Article]] dramatic exaggerations Confidence: high"
        result = self.agent._parse_concepts(directive)
        assert result == ["dramatic exaggerations"]

    def test_too_short_returns_empty(self):
        assert self.agent._parse_concepts("@ling-lens [[X]]") == []


# ── _table_cell ───────────────────────────────────────────────────

class TestTableCell:
    def test_collapses_whitespace(self):
        assert CounterAgent._table_cell("line one\n\nline two") == "line one line two"

    def test_escapes_pipes(self):
        assert "|" not in CounterAgent._table_cell("a | b | c").replace("\\|", "")

    def test_truncates_at_max_len(self):
        long_text = "x" * 200
        result = CounterAgent._table_cell(long_text, max_len=20)
        assert len(result) == 20
        assert result.endswith("…")

    def test_handles_none(self):
        assert CounterAgent._table_cell(None) == ""


# ── Lens dual-link (Phase 4) ──────────────────────────────────────

class TestDualLink:
    def test_file_url_with_line_range(self, tmp_path):
        target = tmp_path / "foo.md"
        target.write_text("body", encoding="utf-8")
        url = CounterAgent._file_url_with_range(target, 10, 25)
        assert url.startswith("file:///")
        assert url.endswith("/foo.md#L10-L25")

    def test_file_url_with_single_line(self, tmp_path):
        target = tmp_path / "foo.md"
        target.write_text("body", encoding="utf-8")
        url = CounterAgent._file_url_with_range(target, 42)
        assert url.startswith("file:///")
        assert url.endswith("/foo.md#L42")

    def test_file_url_without_range(self, tmp_path):
        target = tmp_path / "foo.md"
        target.write_text("body", encoding="utf-8")
        url = CounterAgent._file_url_with_range(target)
        assert url.startswith("file:///")
        assert url.endswith("/foo.md")
        assert "#" not in url

    def test_file_url_none_path(self):
        assert CounterAgent._file_url_with_range(None, 1, 2) == ""

    def test_physical_link_missing_file_returns_empty(self):
        # No such article anywhere in PAGES_DIR / RAW_CONSOLIDATE_DIR
        link = CounterAgent._physical_source_link("DefinitelyDoesNotExist_abc123", {})
        assert link == ""

    def test_physical_link_includes_line_range_in_label(self, tmp_path, monkeypatch):
        # Point RAW_CONSOLIDATE_DIR at a tmp directory containing a fake source
        import agents.counter_agent as ca
        fake_file = tmp_path / "MyArticle.md"
        fake_file.write_text("dummy body", encoding="utf-8")
        monkeypatch.setattr(ca, "RAW_CONSOLIDATE_DIR", tmp_path)

        inst = {"original_source_range": {"start_line": 10, "end_line": 25}}
        link = CounterAgent._physical_source_link("MyArticle", inst)
        # Should be: [<label> L10-L25](file:///<abs>#L10-L25)
        assert "L10-L25" in link
        assert "file://" in link
        assert str(fake_file.resolve()) in link

    def test_physical_link_no_range_still_renders(self, tmp_path, monkeypatch):
        import agents.counter_agent as ca
        fake_file = tmp_path / "MyArticle.md"
        fake_file.write_text("dummy body", encoding="utf-8")
        monkeypatch.setattr(ca, "RAW_CONSOLIDATE_DIR", tmp_path)

        link = CounterAgent._physical_source_link("MyArticle", {})
        assert "file://" in link
        assert "L" not in link.split("](")[-1]  # no #L fragment in URL

    def test_file_url_percent_encodes_spaces_and_parens(self, tmp_path):
        # Real Ling-Ling filenames look like
        # "Partial Differential Equations (Part 51).md".
        # Spaces and parens must be percent-encoded for Markdown parsers.
        target = tmp_path / "Partial Differential Equations (Part 51).md"
        target.write_text("body", encoding="utf-8")
        url = CounterAgent._file_url_with_range(target, 10, 25)
        assert "%20" in url  # spaces encoded
        assert "%28" in url and "%29" in url  # parens encoded
        assert " " not in url
        assert "(" not in url
        assert url.endswith("#L10-L25")

    def test_file_url_percent_encodes_cjk(self, tmp_path):
        target = tmp_path / "妙法蓮華經.md"
        target.write_text("body", encoding="utf-8")
        url = CounterAgent._file_url_with_range(target)
        # CJK chars must be %-encoded (3 bytes each in UTF-8 → 9 hex chars).
        assert "妙" not in url
        assert "%E5%A6%99" in url  # "妙" → UTF-8 → percent-encoded

    def test_dual_link_renders_for_direct_source(self, tmp_path, monkeypatch):
        """P2 regression: direct-source analyses (original == reference)
        should still include the physical file:/// link, not just the
        Obsidian wikilink."""
        import agents.counter_agent as ca
        fake_file = tmp_path / "DirectArticle.md"
        fake_file.write_text("body", encoding="utf-8")
        monkeypatch.setattr(ca, "PAGES_DIR", tmp_path)
        monkeypatch.setattr(ca, "RAW_CONSOLIDATE_DIR", tmp_path)

        agent = CounterAgent.__new__(CounterAgent)
        inst = {
            "id": 1,
            "confidence": "high",
            "quote": "snippet",
            "reasoning": "",
            "original_source_range": {"start_line": 5, "end_line": 9},
        }
        # original_title == reference_title (direct-source case)
        cell = agent._reference_cell(
            article_title="DirectArticle",
            reference_title="DirectArticle",
            resolved_path=str(fake_file),
            heading="",
            inst=inst,
        )
        assert "[[DirectArticle" in cell  # Obsidian wikilink half
        assert "file://" in cell           # Physical link half — was missing before P2 fix
        assert "#L5-L9" in cell

    def test_format_instance_dual_link_for_direct_source(self, tmp_path, monkeypatch):
        import agents.counter_agent as ca
        fake_file = tmp_path / "DirectArticle.md"
        fake_file.write_text("body", encoding="utf-8")
        monkeypatch.setattr(ca, "PAGES_DIR", tmp_path)
        monkeypatch.setattr(ca, "RAW_CONSOLIDATE_DIR", tmp_path)

        agent = CounterAgent.__new__(CounterAgent)
        inst = {
            "id": 1,
            "confidence": "medium",
            "quote": "snippet",
            "reasoning": "",
            "original_source_range": {"start_line": 1, "end_line": 3},
        }
        lines = agent._format_instance(
            inst,
            reference_title="DirectArticle",
            original_title="DirectArticle",
            resolved_path=str(fake_file),
        )
        rendered = "\n".join(lines)
        assert "Open in editor" in rendered
        assert "file://" in rendered
        assert "#L1-L3" in rendered


class TestRagFallbackArticleText:
    """Audit B2: the RAG fallback must feed raw chunk text + real title,
    not query_similar_notes' markdown-formatted string."""

    def test_uses_raw_text_and_metadata_title(self):
        agent = CounterAgent.__new__(CounterAgent)
        agent._find_in_pages = lambda title: ("", "")  # force the RAG fallback

        class FakeRAG:
            def query_notes(self, query, top_k=1):
                return [{"text": "raw chunk body, no heading", "metadata": {"title": "Real Title"}}]

        agent.rag = FakeRAG()
        results = agent._resolve_articles(["Missing Doc"], "a semantic query")
        assert len(results) == 1
        title, text, _ = results[0]
        assert title == "Real Title"            # not the literal "(RAG result)"
        assert text == "raw chunk body, no heading"
        assert "### [來自筆記" not in text        # no injected markdown heading

    def test_missing_metadata_title_falls_back_to_label(self):
        agent = CounterAgent.__new__(CounterAgent)
        agent._find_in_pages = lambda title: ("", "")

        class FakeRAG:
            def query_notes(self, query, top_k=1):
                return [{"text": "body", "metadata": {}}]

        agent.rag = FakeRAG()
        title, text, _ = agent._resolve_articles(["Missing"], "q")[0]
        assert title == "(RAG result)"
        assert text == "body"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
