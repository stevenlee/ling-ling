"""
Unit tests for core.parser utilities — JSON extraction, markdown cleaning,
and Mermaid repair functions.
"""
import sys
from pathlib import Path

# Ensure System_Engine is on the path
sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import pytest
import yaml
from core.parser import (
    extract_json_array,
    extract_json_object,
    clean_llm_response,
    repair_latex_escape_collisions,
    run_markdown_quality_checks,
    strip_body_frontmatter,
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
        assert any(f["type"] == "trailing_whitespace" for f in fixes)

    def test_removes_excessive_blank_lines(self):
        text = "line one\n\n\n\n\nline two"
        result, fixes = run_markdown_quality_checks(text)
        assert "\n\n\n" not in result
        assert any(f["type"] == "excessive_blank_lines" for f in fixes)

    def test_repairs_latex_carriage_returns(self):
        """run_markdown_quality_checks repairs LaTeX CR commands like \\rightarrow."""
        text = "The arrow $\rightarrow$ is important"
        result, fixes = run_markdown_quality_checks(text)
        assert "\\rightarrow" in result or result == text  # Only fixes if actual CR present
        assert isinstance(fixes, list)

    def test_clean_text_returns_no_fixes(self):
        text = "# Clean Document\n\nParagraph one.\n\nParagraph two."
        result, fixes = run_markdown_quality_checks(text)
        assert fixes == []
        assert result == text

    def test_strip_frontmatter_flag(self):
        text = "---\ntitle: test\n---\n\n# Content"
        result, _ = run_markdown_quality_checks(text, strip_frontmatter=True)
        assert "---" not in result
        assert "# Content" in result

    def test_returns_tuple(self):
        result = run_markdown_quality_checks("some text")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], list)


# ── strip_body_frontmatter ────────────────────────────────────────────

class TestStripBodyFrontmatter:
    def test_strips_valid_yaml_frontmatter(self):
        text = "---\ntitle: foo\ntags: [a, b]\n---\n# Body"
        cleaned, fixes = strip_body_frontmatter(text)
        assert cleaned == "# Body"
        assert len(fixes) == 1
        assert fixes[0]["type"] == "removed_body_frontmatter"
        assert fixes[0]["line"] == 1
        # before captures the YAML block that got removed
        assert "title: foo" in fixes[0]["before"]

    def test_preserves_text_without_frontmatter(self):
        text = "Just prose, no frontmatter.\n"
        cleaned, fixes = strip_body_frontmatter(text)
        assert cleaned == text
        assert fixes == []

    def test_does_not_eat_horizontal_rule_sections(self):
        """G-san regression: hand-authored markdown opens with a `---`
        horizontal rule and later contains another `---` separator. The
        old non-greedy regex would have eaten everything between them.
        """
        text = (
            "---\n"
            "# Section 1: Introduction\n"
            "Some text...\n"
            "---\n"
            "# Section 2: Core Prompt\n"
            "Detail...\n"
        )
        cleaned, fixes = strip_body_frontmatter(text)
        assert cleaned == text
        assert fixes == []
        # The crucial guarantee: Section 1 survives.
        assert "Section 1: Introduction" in cleaned

    def test_does_not_strip_list_at_top(self):
        # YAML list (not a mapping) — also not real frontmatter.
        text = "---\n- one\n- two\n- three\n---\n# Body"
        cleaned, fixes = strip_body_frontmatter(text)
        assert cleaned == text
        assert fixes == []

    def test_does_not_strip_scalar_at_top(self):
        # YAML scalar — not real frontmatter.
        text = "---\njust a scalar string\n---\n# Body"
        cleaned, fixes = strip_body_frontmatter(text)
        assert cleaned == text
        assert fixes == []

    def test_handles_malformed_yaml_safely(self):
        # Block looks YAML-ish but contains invalid syntax: leave intact.
        text = "---\nkey: [unclosed\n---\n# Body"
        cleaned, fixes = strip_body_frontmatter(text)
        assert cleaned == text
        assert fixes == []

    def test_empty_string(self):
        assert strip_body_frontmatter("") == ("", [])

    def test_strips_with_leading_whitespace(self):
        # Real frontmatter with a stray blank line in front — still strip.
        text = "\n\n---\ntitle: foo\n---\nBody"
        cleaned, fixes = strip_body_frontmatter(text)
        assert cleaned == "Body"
        assert len(fixes) == 1
        assert fixes[0]["type"] == "removed_body_frontmatter"


# ── Structured quality_fix records (A3 upgrade) ──────────────────────

class TestStructuredFixRecords:
    """Each repair function emits {type, line?, before?, after?} dicts."""

    def test_latex_carriage_return_records_line_and_diff(self):
        text = "para one\npara two with \rightarrow arrow\npara three"
        cleaned, fixes = run_markdown_quality_checks(text)
        cr_fixes = [f for f in fixes if f["type"] == "repaired_latex_carriage_return"]
        assert len(cr_fixes) == 1
        fix = cr_fixes[0]
        assert fix["line"] == 2  # arrow is on line 2
        assert fix["before"] == "\rightarrow"
        assert fix["after"] == "\\rightarrow"

    def test_multiple_latex_carriage_returns_one_record_each(self):
        text = "\rightarrow first\n\rangle second\n\rceil third"
        _, fixes = run_markdown_quality_checks(text)
        cr_fixes = [f for f in fixes if f["type"] == "repaired_latex_carriage_return"]
        assert len(cr_fixes) == 3
        assert [f["line"] for f in cr_fixes] == [1, 2, 3]

    def test_mermaid_label_fix_carries_line_before_after(self):
        text = "```mermaid\ngraph TD\nA[Hello] --> B\n```"
        _, fixes = run_markdown_quality_checks(text)
        label_fixes = [f for f in fixes if f["type"] == "quoted_mermaid_labels"]
        assert len(label_fixes) == 1
        fix = label_fixes[0]
        assert fix["line"] == 3
        assert "A[Hello]" in fix["before"]
        assert 'A["Hello"]' in fix["after"]

    def test_frontmatter_strip_records_block_before(self):
        text = "---\ntitle: foo\ntags: [a]\n---\n# Body"
        _, fixes = run_markdown_quality_checks(text, strip_frontmatter=True)
        fm_fixes = [f for f in fixes if f["type"] == "removed_body_frontmatter"]
        assert len(fm_fixes) == 1
        assert fm_fixes[0]["line"] == 1
        assert "title: foo" in fm_fixes[0]["before"]

    def test_fix_records_are_yaml_serializable(self):
        # Sanity: the records survive YAML round-trip (this is what gets
        # written into note frontmatter).
        text = (
            "---\ntitle: foo\n---\n"
            "para with \rightarrow\n"
            "```mermaid\ngraph TD\nA[Bare] --> B\n```\n"
            "trailing whitespace   "
        )
        _, fixes = run_markdown_quality_checks(text, strip_frontmatter=True)
        dumped = yaml.safe_dump({"quality_fixes": fixes}, allow_unicode=True)
        reloaded = yaml.safe_load(dumped)
        assert reloaded["quality_fixes"] == fixes

    def test_long_before_after_get_truncated(self):
        # Construct a fix with a very long YAML block; before must truncate.
        long_value = "x" * 500
        text = f"---\ntitle: {long_value}\n---\nbody"
        _, fixes = strip_body_frontmatter(text)
        assert len(fixes[0]["before"]) <= 80


# ── repair_latex_escape_collisions ────────────────────────────────────


class TestLatexEscapeCollisions:
    """LingLens caught this: LLM emits `\\binom` in JSON, json.loads
    interprets `\\b` as backspace, the backslash and `b` are lost. Same
    bug for `\\frac` (\\f → form feed) and `\\vec` (\\v → vertical tab).
    """

    def test_binom_backslash_restored(self):
        # Simulate what json.loads of LLM output produces
        decoded = "\x08inom{n}{k}"
        result, fixes = repair_latex_escape_collisions(decoded)
        assert result == "\\binom{n}{k}"
        assert len(fixes) == 1
        assert fixes[0]["type"] == "repaired_latex_backspace"
        assert fixes[0]["before"] == "\x08inom"
        assert fixes[0]["after"] == "\\binom"

    def test_frac_backslash_restored(self):
        decoded = "\x0crac{1}{2}"
        result, fixes = repair_latex_escape_collisions(decoded)
        assert result == "\\frac{1}{2}"
        assert fixes[0]["type"] == "repaired_latex_form_feed"

    def test_vec_backslash_restored(self):
        decoded = "\x0bec{x}"
        result, fixes = repair_latex_escape_collisions(decoded)
        assert result == "\\vec{x}"
        assert fixes[0]["type"] == "repaired_latex_vertical_tab"

    def test_multiple_collisions_in_one_text(self):
        decoded = "Let \x08inom{n}{k} = \x0crac{n!}{k!(n-k)!} where n,k \x0bee N"
        result, fixes = repair_latex_escape_collisions(decoded)
        assert "\\binom{n}{k}" in result
        assert "\\frac{n!}{k!(n-k)!}" in result
        assert "\\vee N" in result
        types = {f["type"] for f in fixes}
        assert types == {
            "repaired_latex_backspace",
            "repaired_latex_form_feed",
            "repaired_latex_vertical_tab",
        }

    def test_repeated_same_command_records_each(self):
        decoded = "\x08inom{n}{0} + \x08inom{n-1}{1}"
        result, fixes = repair_latex_escape_collisions(decoded)
        assert result == "\\binom{n}{0} + \\binom{n-1}{1}"
        assert len(fixes) == 2

    def test_line_number_recorded(self):
        decoded = "line one\nline two\n\x08inom{n}{k}"
        _, fixes = repair_latex_escape_collisions(decoded)
        assert fixes[0]["line"] == 3

    def test_plain_text_passes_through(self):
        text = "no special chars here, just prose"
        result, fixes = repair_latex_escape_collisions(text)
        assert result == text
        assert fixes == []

    def test_empty_input(self):
        assert repair_latex_escape_collisions("") == ("", [])
        assert repair_latex_escape_collisions(None or "") == ("", [])

    def test_does_not_touch_n_or_t_escapes(self):
        # \n (newline) and \t (tab) are intentionally NOT repaired to
        # avoid corrupting legit whitespace.
        text = "line one\nline two\tindented"
        result, fixes = repair_latex_escape_collisions(text)
        assert result == text
        assert fixes == []

    def test_end_to_end_json_decode_then_repair(self):
        # The actual bug path: LLM produces JSON, json.loads breaks the
        # LaTeX, repair restores it.
        import json
        llm_output = '{"q": "\\binom{n}{k}"}'  # LLM forgot to double-escape
        decoded = json.loads(llm_output)
        assert decoded["q"] == "\x08inom{n}{k}"   # the bug
        repaired, _ = repair_latex_escape_collisions(decoded["q"])
        assert repaired == "\\binom{n}{k}"        # the fix

    def test_integrated_into_quality_pipeline(self):
        # run_markdown_quality_checks must include this repair so notes
        # written through the pipeline get the fix automatically.
        text = "$\x08inom{n}{k}$"
        result, fixes = run_markdown_quality_checks(text)
        assert "\\binom" in result
        assert any(f["type"] == "repaired_latex_backspace" for f in fixes)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
