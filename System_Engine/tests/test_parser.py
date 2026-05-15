"""
Unit tests for core.parser utilities — JSON extraction, markdown cleaning,
and Mermaid repair functions.
"""
import sys
from pathlib import Path

# Ensure System_Engine is on the path
sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import pytest
from core.parser import (
    extract_json_array,
    extract_json_object,
    clean_llm_response,
    run_markdown_quality_checks,
)


# ── extract_json_array ───────────────────────────────────────────────

class TestExtractJsonArray:
    def test_plain_array(self):
        assert extract_json_array('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]

    def test_fenced_json(self):
        text = 'Some preamble\n```json\n[{"x": 1}]\n```\nMore text'
        assert extract_json_array(text) == [{"x": 1}]

    def test_fenced_no_lang(self):
        text = '```\n[{"y": 2}]\n```'
        assert extract_json_array(text) == [{"y": 2}]

    def test_empty_input(self):
        assert extract_json_array("") == []
        assert extract_json_array(None) == []

    def test_no_json(self):
        assert extract_json_array("just some text") == []

    def test_filters_non_dicts(self):
        assert extract_json_array('[1, 2, {"a": 3}]') == [{"a": 3}]

    def test_embedded_in_text(self):
        text = 'Here is the result: [{"quote": "hello"}] end.'
        assert extract_json_array(text) == [{"quote": "hello"}]


# ── extract_json_object ──────────────────────────────────────────────

class TestExtractJsonObject:
    def test_plain_object(self):
        assert extract_json_object('{"key": "value"}') == {"key": "value"}

    def test_fenced_json(self):
        text = '```json\n{"total_count": 5}\n```'
        assert extract_json_object(text) == {"total_count": 5}

    def test_empty_input(self):
        assert extract_json_object("") == {}
        assert extract_json_object(None) == {}

    def test_no_json(self):
        assert extract_json_object("no json here") == {}

    def test_embedded_object(self):
        text = 'The result is: {"count": 3, "items": [1,2,3]} done.'
        result = extract_json_object(text)
        assert result["count"] == 3
        assert result["items"] == [1, 2, 3]

    def test_returns_dict_not_list(self):
        """Should return {} if the top-level JSON is a list, not a dict."""
        assert extract_json_object('[1, 2, 3]') == {}

    def test_nested_objects(self):
        text = '{"outer": {"inner": true}}'
        assert extract_json_object(text) == {"outer": {"inner": True}}


# ── clean_llm_response ──────────────────────────────────────────────

class TestCleanLlmResponse:
    def test_strips_markdown_fence(self):
        assert clean_llm_response("```markdown\n# Hello\n```") == "# Hello"

    def test_strips_md_fence(self):
        assert clean_llm_response("```md\n# Hello\n```") == "# Hello"

    def test_preserves_mermaid_fence(self):
        text = "```mermaid\ngraph TD\n  A-->B\n```"
        assert clean_llm_response(text) == text

    def test_no_fence(self):
        assert clean_llm_response("# Hello World") == "# Hello World"

    def test_empty_input(self):
        assert clean_llm_response("") == ""


# ── run_markdown_quality_checks ──────────────────────────────────────

class TestMarkdownQualityChecks:
    def test_removes_trailing_whitespace(self):
        text = "line one   \nline two  "
        result, fixes = run_markdown_quality_checks(text)
        assert "   " not in result
        assert "trailing_whitespace" in fixes

    def test_removes_excessive_blank_lines(self):
        text = "line one\n\n\n\n\nline two"
        result, fixes = run_markdown_quality_checks(text)
        assert "\n\n\n" not in result

    def test_no_fixes_on_clean_text(self):
        text = "# Clean Document\n\nParagraph one.\n\nParagraph two."
        result, fixes = run_markdown_quality_checks(text)
        assert fixes == []
        assert result == text

    def test_strip_frontmatter_flag(self):
        text = "---\ntitle: test\n---\n\n# Content"
        result, _ = run_markdown_quality_checks(text, strip_frontmatter=True)
        assert "---" not in result
        assert "# Content" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
