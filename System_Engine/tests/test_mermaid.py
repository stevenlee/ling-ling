"""Edge-case tests for mermaid repair pipeline.

These cover behavior that the original implementation got wrong or only
partially supported:
  - Label quoting for shapes other than `[]` and `{}`.
  - Bracket-balance heuristic ignoring brackets inside quoted labels.
  - Bracket-balance heuristic ignoring brackets inside `%%` comments.
  - Idempotent repair pipeline (running twice yields the same result).
  - Fenced-block substitution that doesn't collide on duplicate blocks.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import pytest

from core.parser import (
    repair_mermaid_fences,
    repair_mermaid_label_quotes,
    run_markdown_quality_checks,
)


# ── Label quoting across shapes ─────────────────────────────────────

class TestLabelQuoting:
    def _quote(self, body):
        text = f"```mermaid\n{body}\n```"
        result, fixes = repair_mermaid_label_quotes(text)
        return result, fixes

    def test_quotes_rectangle(self):
        result, fixes = self._quote("graph TD\nA[Hello world] --> B")
        assert 'A["Hello world"]' in result
        assert any(f["type"] == "quoted_mermaid_labels" for f in fixes)

    def test_quotes_round(self):
        result, fixes = self._quote("graph TD\nA(Click here) --> B")
        assert 'A("Click here")' in result
        assert any(f["type"] == "quoted_mermaid_labels" for f in fixes)

    def test_quotes_circle(self):
        result, fixes = self._quote("graph TD\nA((Start)) --> B")
        assert 'A(("Start"))' in result

    def test_quotes_rhombus(self):
        result, fixes = self._quote("graph TD\nA{Decide?} --> B")
        assert 'A["Decide?"]' in result or 'A{"Decide?"}' in result

    def test_quotes_hexagon(self):
        result, _ = self._quote("graph TD\nA{{Prep}} --> B")
        assert 'A{{"Prep"}}' in result

    def test_quotes_subroutine(self):
        result, _ = self._quote("graph TD\nA[[Sub task]] --> B")
        assert 'A[["Sub task"]]' in result

    def test_quotes_cylinder(self):
        result, _ = self._quote("graph TD\nA[(Database)] --> B")
        assert 'A[("Database")]' in result

    def test_quotes_stadium(self):
        result, _ = self._quote("graph TD\nA([Start]) --> B")
        assert 'A(["Start"])' in result

    def test_skips_already_quoted(self):
        text = '```mermaid\ngraph TD\nA["already quoted"] --> B\n```'
        result, fixes = repair_mermaid_label_quotes(text)
        assert result == text
        assert fixes == []

    def test_escapes_internal_quotes(self):
        result, _ = self._quote('graph TD\nA[say "hi"] --> B')
        assert r'A["say \"hi\""]' in result

    def test_skips_lines_outside_mermaid(self):
        # `[label]` outside a mermaid block is a markdown link target — never quote.
        text = "See [the docs] for info.\n\n```mermaid\ngraph TD\nA[Hello] --> B\n```"
        result, _ = repair_mermaid_label_quotes(text)
        assert "[the docs]" in result
        assert 'A["Hello"]' in result

    def test_arrow_between_rectangles_not_corrupted(self):
        """REGRESSION: `A[Start] --> B[End]` previously parsed `-->X]` as if
        `-->` were a node id and `>` an asymmetric-shape opener, producing
        invalid output `A["Start"] -->"B[End"]`. Both ends should be quoted
        independently and the arrow must remain intact."""
        result, _ = self._quote("graph TD\nA[Start] --> B[End]")
        assert 'A["Start"] --> B["End"]' in result
        assert '-->"B[End"]' not in result

    def test_arrow_variants_not_corrupted(self):
        """The same risk applies to dotted/thick/cross arrows containing `>`."""
        for arrow in ("-.->", "==>", "--x", "--o", "-->"):
            body = f"graph TD\nA[X] {arrow} B[Y]"
            result, _ = self._quote(body)
            assert f'A["X"] {arrow} B["Y"]' in result, f"arrow {arrow!r} got corrupted: {result}"

    def test_kebab_case_node_ids_still_quoted(self):
        """Node ids with internal hyphens (e.g. `my-node`) should still get
        their label quoted; the regex must only forbid LEADING hyphens."""
        result, _ = self._quote("graph TD\nmy-node[Label] --> other-node[Stuff]")
        assert 'my-node["Label"]' in result
        assert 'other-node["Stuff"]' in result

    def test_real_asymmetric_shape_still_supported(self):
        """The `>...]` asymmetric shape is rare but legitimate; preserve it."""
        result, _ = self._quote("graph TD\nA>Asymmetric label] --> B")
        assert 'A>"Asymmetric label"]' in result


# ── Bracket-balance heuristic ──────────────────────────────────────

class TestBrokenHeuristic:
    """The bracket-balance check must ignore quoted labels and %% comments."""

    def setup_method(self):
        # Import inside the test to avoid agent module init at collection time.
        from agents.base_agent import BaseAgent
        self.check = BaseAgent._is_mermaid_broken

    def test_balanced_is_not_broken(self):
        assert not self.check("graph TD\n  A --> B\n")

    def test_bracket_inside_quoted_label_is_ok(self):
        assert not self.check('graph TD\n  A["label [with bracket]"] --> B')

    def test_bracket_inside_comment_is_ok(self):
        assert not self.check("graph TD\n  A --> B\n  %% TODO [rewrite this")

    def test_actually_unbalanced_is_broken(self):
        assert self.check("graph TD\n  A[unterminated --> B")

    def test_empty_is_broken(self):
        assert self.check("")
        assert self.check("   \n  ")


# ── Pipeline idempotency ───────────────────────────────────────────

class TestIdempotent:
    def test_pipeline_is_idempotent(self):
        text = (
            "Some intro prose.\n\n"
            "```mermaid\ngraph TD\nA[hello] --> B((world))\n```\n\n"
            "More text   \n\n\n\n"
            "End."
        )
        once, _ = run_markdown_quality_checks(text)
        twice, fixes = run_markdown_quality_checks(once)
        assert once == twice
        assert fixes == []

    def test_already_clean_yields_no_fixes(self):
        text = "# Title\n\nParagraph.\n"
        _, fixes = run_markdown_quality_checks(text)
        assert fixes == []


# ── Fence repair ──────────────────────────────────────────────────

class TestFenceRepair:
    def test_wraps_bare_mermaid_keyword(self):
        text = "Intro\n\nmermaid\ngraph TD\n  A --> B\n\nOutro"
        result, fixes = repair_mermaid_fences(text)
        assert "```mermaid" in result
        assert any(f["type"] == "wrapped_bare_mermaid" for f in fixes)

    def test_closes_unterminated(self):
        text = "```mermaid\ngraph TD\n  A --> B"
        result, fixes = repair_mermaid_fences(text)
        assert result.endswith("```")
        assert any(f["type"] == "closed_unterminated_mermaid" for f in fixes)


# ── Block substitution doesn't collide on duplicates ──────────────

class TestDuplicateBlockSubstitution:
    """Two identical broken blocks should both be considered (or both kept)
    without one's repair clobbering the other."""

    def test_heal_handles_duplicate_blocks(self, monkeypatch):
        from agents.base_agent import BaseAgent

        agent = BaseAgent.__new__(BaseAgent)
        agent.llm = None
        agent.rag = None
        agent.stats = {"input_chars": 0, "output_chars": 0}

        # Stub out the LLM repair to a fixed valid block so we can assert
        # both occurrences were addressed independently.
        monkeypatch.setattr(
            BaseAgent,
            "_llm_repair_mermaid",
            lambda self, block: "graph TD\n  A --> B",
        )

        broken = "graph TD\n  A[unterminated --> B"
        content = f"```mermaid\n{broken}\n```\n\nBetween\n\n```mermaid\n{broken}\n```"
        healed = agent._heal_mermaid_blocks(content)
        # Both blocks should be healed; the healed text appears twice.
        assert healed.count("A --> B") == 2
        # And the unterminated form should be gone.
        assert "unterminated" not in healed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
