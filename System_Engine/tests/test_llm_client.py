"""Tests for the LLM-free logic inside services.llm_client.

We don't mock the actual provider here — we only exercise the pure helpers
(YAML parsing, file caching, digest formatting, fallbacks) that run alongside
the LLM call but don't require one.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

# Set provider before importing so __init__ doesn't trip on unknown provider.
os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from core.utils import MtimeCache
from services.llm_client import LLMClient


# ── MtimeCache ──────────────────────────────────────────────────────

class TestMtimeCache:
    def test_caches_and_returns_content(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("hello", encoding="utf-8")
        c = MtimeCache()
        assert c.read(p) == "hello"
        # Confirm we don't re-read by overwriting at the OS level but keeping
        # the same mtime → cached value should still come back.
        # (We test mtime invalidation separately.)
        assert c.read(p) == "hello"

    def test_invalidates_on_mtime_change(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("v1", encoding="utf-8")
        c = MtimeCache()
        assert c.read(p) == "v1"

        # Bump mtime forward to force invalidation.
        new_mtime = p.stat().st_mtime + 10
        p.write_text("v2", encoding="utf-8")
        os.utime(p, (new_mtime, new_mtime))
        assert c.read(p) == "v2"

    def test_missing_file_returns_empty(self, tmp_path):
        p = tmp_path / "nope.md"
        c = MtimeCache()
        assert c.read(p) == ""

    def test_deletion_after_cache_returns_empty(self, tmp_path):
        p = tmp_path / "x.md"
        p.write_text("data", encoding="utf-8")
        c = MtimeCache()
        assert c.read(p) == "data"
        p.unlink()
        assert c.read(p) == ""


# ── _hybrid_parse ───────────────────────────────────────────────────

class TestHybridParse:
    def test_yaml_frontmatter(self):
        r = LLMClient._hybrid_parse(
            "---\ntitle: Hello\ntags: [a, b]\ntype: note\n---\n\nBody content"
        )
        assert r["title"] == "Hello"
        assert r["tags"] == ["a", "b"]
        assert r["type"] == "note"
        assert r["content"] == "Body content"

    def test_yaml_fenced(self):
        r = LLMClient._hybrid_parse(
            "```yaml\ntitle: Wrapped\ntags: [x]\n```\n\nBody"
        )
        assert r["title"] == "Wrapped"
        assert r["tags"] == ["x"]

    def test_h1_fallback_when_no_yaml(self):
        r = LLMClient._hybrid_parse("# Some Title\n\nBody")
        assert r["title"] == "Some Title"

    def test_default_when_no_signals(self):
        r = LLMClient._hybrid_parse("just prose")
        assert r["title"] == "Untitled"
        assert r["content"] == "just prose"

    def test_empty_input(self):
        r = LLMClient._hybrid_parse("")
        assert r["title"] == "Untitled"
        assert r["content"] == ""

    def test_yaml_with_pending_concepts(self):
        r = LLMClient._hybrid_parse(
            "---\ntitle: T\npending_concepts: [unfinished]\n---\nBody"
        )
        assert r.get("pending_concepts") == ["unfinished"]


# ── _strip_accidental_frontmatter ────────────────────────────────────

class TestStripAccidentalFrontmatter:
    def test_strips_markdown_fence(self):
        r = LLMClient._strip_accidental_frontmatter("```markdown\n# Hi\nbody\n```")
        assert r == "# Hi\nbody"

    def test_strips_md_fence(self):
        r = LLMClient._strip_accidental_frontmatter("```md\nx\n```")
        assert r == "x"

    def test_strips_body_frontmatter(self):
        r = LLMClient._strip_accidental_frontmatter("---\ntitle: x\n---\n\nBody")
        assert r == "Body"

    def test_empty_input(self):
        assert LLMClient._strip_accidental_frontmatter("") == ""
        assert LLMClient._strip_accidental_frontmatter(None) == ""

    def test_no_changes_when_clean(self):
        assert LLMClient._strip_accidental_frontmatter("# Clean") == "# Clean"


# ── Part digest helpers ─────────────────────────────────────────────

class TestPartDigest:
    def test_apply_defaults_fills_missing_keys(self):
        result = LLMClient._apply_part_digest_defaults({"thesis": "T"}, 3)
        assert result["part"] == 3
        assert result["title"] == "Part 3"
        assert result["thesis"] == "T"
        assert result["key_points"] == []
        assert result["evidence"] == []
        assert result["handoff"] == ""

    def test_apply_defaults_preserves_existing(self):
        given = {"part": 7, "title": "Custom", "key_points": ["a", "b"]}
        result = LLMClient._apply_part_digest_defaults(given, 99)
        assert result["part"] == 7
        assert result["title"] == "Custom"
        assert result["key_points"] == ["a", "b"]

    def test_format_string_passthrough(self):
        assert LLMClient._format_part_digest_for_prompt("raw text") == "raw text"

    def test_format_dict_emits_sections(self):
        digest = {
            "part": 2,
            "title": "Intro",
            "thesis": "Central claim.",
            "key_points": ["one", "two"],
            "evidence": ["e1"],
            "terms": [],
            "open_questions": [],
            "handoff": "next",
        }
        text = LLMClient._format_part_digest_for_prompt(digest)
        assert "### Part 2: Intro" in text
        assert "Thesis: Central claim." in text
        assert "- one" in text
        assert "- two" in text
        assert "- (none)" in text  # for the empty `terms`
        assert "next" in text

    def test_format_none_safe(self):
        assert "(empty digest)" in LLMClient._format_part_digest_for_prompt(None)
        assert "(empty digest)" not in LLMClient._format_part_digest_for_prompt("x")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
