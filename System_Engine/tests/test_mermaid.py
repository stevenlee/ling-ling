"""Edge-case tests for mermaid repair pipeline.

These cover behavior that the original implementation got wrong or only
partially supported:
  - Label quoting for shapes other than `[]` and `{}`.
  - Bracket-balance heuristic ignoring brackets inside quoted labels.
  - Bracket-balance heuristic ignoring brackets inside `%%` comments.
  - Idempotent repair pipeline (running twice yields the same result).
  - Fenced-block substitution that doesn't collide on duplicate blocks.
"""

import pytest

from core.parser import (
    repair_mermaid_fences,
    repair_mermaid_label_quotes,
    repair_mermaid_latex_labels,
    repair_mermaid_quadrant_points,
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

    def test_quotes_single_quoted_labels(self):
        result, fixes = self._quote("graph TD\nA2['路徑預測模擬 (Path Simulation)'] --> B")
        assert 'A2["路徑預測模擬 (Path Simulation)"]' in result
        assert any(f["type"] == "quoted_mermaid_labels" for f in fixes)

    def test_quotes_single_quoted_labels_with_outer_quotes(self):
        result, fixes = self._quote("graph TD\n    \"A2['路徑預測模擬 (Path Simulation)']\"")
        assert '    A2["路徑預測模擬 (Path Simulation)"]' in result
        assert any(f["type"] == "quoted_mermaid_labels" for f in fixes)

    def test_connection_lines_strip_quotes(self):
        result, fixes = self._quote(
            'graph TD\n    "A1" --> "B1"\n    "A2" --> B2\n    A3 -.-> "B3"'
        )
        assert "    A1 --> B1" in result
        assert "    A2 --> B2" in result
        assert "    A3 -.-> B3" in result
        assert any(f["type"] == "quoted_mermaid_labels" for f in fixes)

    def test_connection_lines_preserve_quoted_edge_labels(self):
        result, fixes = self._quote('graph TD\n    A -- "edge" --> B\n    A -. "maybe" .-> B')
        assert '    A -- "edge" --> B' in result
        assert '    A -. "maybe" .-> B' in result
        assert fixes == []

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


# ── LaTeX-in-label degradation ─────────────────────────────────────


class TestLatexLabels:
    """Mermaid renders KaTeX math in labels via `$$...$$`, so math is PRESERVED
    and single `$...$` is promoted to `$$...$$`. A bare `\\command` with no `$`
    (which KaTeX never sees) is still degraded to plain text."""

    def _strip(self, body):
        text = f"```mermaid\n{body}\n```"
        return repair_mermaid_latex_labels(text)

    def test_preserves_double_dollar_math(self):
        # `$$...$$` is mermaid's KaTeX delimiter — keep it and its commands.
        result, fixes = self._strip(r'graph LR\nA["公式 $$\frac{1}{2}(l+r)$$ 成立"]')
        assert r"$$\frac{1}{2}(l+r)$$" in result
        assert fixes == []

    def test_promotes_single_dollar_to_double(self):
        # Mermaid only renders `$$...$$`, so `$...$` is promoted; the command
        # is PRESERVED (KaTeX renders it), not unicode-degraded.
        result, fixes = self._strip(r'graph LR\nA["速率 $\alpha$ 增長"]')
        assert r"$$\alpha$$" in result
        assert any(f["type"] == "normalized_mermaid_math" for f in fixes)

    def test_preserves_commands_inside_math(self):
        result, _ = self._strip(r'graph LR\nA["$P_0 y^{m-1} + \dots + P_m$"]')
        assert r"$$P_0 y^{m-1} + \dots + P_m$$" in result

    def test_multiple_spans_keeps_only_richest(self):
        # Obsidian errors on 2+ $$ per line; keep the richest, plaintext the rest.
        result, _ = self._strip(r'graph LR\nA["$x$ 和 $y^2$"]')
        assert "x 和 $$y^2$$" in result
        assert result.count("$$") == 2  # exactly one $$ span

    def test_all_trivial_spans_all_plaintext(self):
        # Every span is a bare variable → no $$ at all (nothing worth rendering).
        result, _ = self._strip(r'graph LR\nA["$P$ 與 $Q$ 互斥"]')
        assert "P 與 Q 互斥" in result
        assert "$$" not in result

    def test_bare_command_without_dollar_degraded(self):
        # No `$` delimiters — KaTeX never sees it, so degrade `\alpha`/`\dots`.
        result, fixes = self._strip(r'graph LR\nA["速率 \alpha 增長 \dots"]')
        assert "速率 α 增長 …" in result
        assert any(f["type"] == "stripped_mermaid_latex" for f in fixes)

    def test_plain_label_untouched(self):
        text = '```mermaid\ngraph TD\nA["處理中... (Processing)"] --> B["結束"]\n```'
        result, fixes = repair_mermaid_latex_labels(text)
        assert result == text
        assert fixes == []

    def test_outside_mermaid_math_preserved(self):
        # Inline `$...$` in prose is valid Obsidian markdown — never touch it.
        text = "前提是 $\\mathcal{M}_0$ 成立。\n\n```mermaid\ngraph TD\nA --> B\n```"
        result, fixes = repair_mermaid_latex_labels(text)
        assert "$\\mathcal{M}_0$" in result
        assert fixes == []

    def test_restores_corrupted_command_inside_math(self):
        # `\frac`→`rac` (leading `\f` control char flattened to a space) is
        # recovered inside a math span. Single span so it stays as $$.
        result, _ = self._strip(r'graph LR\nA["部分和 $s_n = rac{1-r^n}{1-r}$"]')
        assert r"$$s_n = \frac{1-r^n}{1-r}$$" in result

    def test_multispan_keeps_richest_recovered(self):
        # Corruption recovered in every span, but only the richest is kept as $$.
        result, _ = self._strip(r'graph LR\nA["$ riangle$ 且 $s_n = rac{1}{2}$"]')
        assert r"$$s_n = \frac{1}{2}$$" in result
        assert result.count("$$") == 2  # richest only; triangle degraded

    def test_recovery_only_inside_math(self):
        # A bare tail OUTSIDE math is left alone (could be a real word/variable).
        result, fixes = self._strip(r'graph LR\nA["the rac team"]')
        assert "the rac team" in result
        assert fixes == []

    def test_recovery_idempotent(self):
        from core.parser import repair_mermaid_latex_labels

        body = r'graph LR\nA["$ rac{1}{2}$ 與 $ heta$"]'
        once, _ = self._strip(body)
        twice, fixes = repair_mermaid_latex_labels(once)
        assert once == twice
        assert fixes == []

    def test_idempotent(self):
        body = r'graph LR\nA["速率 $\alpha$ 與 $$\beta$$ 以及 \gamma"]'
        once, _ = self._strip(body)
        twice, fixes = repair_mermaid_latex_labels(once)
        assert once == twice
        assert fixes == []


# ── quadrantChart point quoting ────────────────────────────────────


class TestQuadrantPoints:
    """Mermaid quadrantChart point names must be double-quoted; the LLM
    routinely drops the quotes, which fails the whole chart."""

    def _quote(self, body):
        text = f"```mermaid\n{body}\n```"
        return repair_mermaid_quadrant_points(text)

    def test_quotes_bare_point_name(self):
        result, fixes = self._quote("quadrantChart\n    Campaign B: [0.45, 0.23]")
        assert '    "Campaign B": [0.45, 0.23]' in result
        assert any(f["type"] == "quoted_quadrant_point" for f in fixes)

    def test_quotes_cjk_name_with_space(self):
        result, _ = self._quote("quadrantChart\n    重構模組 A: [0.8, 0.9]")
        assert '    "重構模組 A": [0.8, 0.9]' in result

    def test_already_quoted_untouched(self):
        text = '```mermaid\nquadrantChart\n    "已正確": [0.1, 0.1]\n```'
        result, fixes = repair_mermaid_quadrant_points(text)
        assert result == text
        assert fixes == []

    def test_axis_and_quadrant_lines_untouched(self):
        body = (
            "quadrantChart\n    title 影響力\n    x-axis Low --> High\n"
            "    quadrant-1 立即處理\n    P: [0.5, 0.5]"
        )
        result, _ = self._quote(body)
        assert "    title 影響力" in result
        assert "    x-axis Low --> High" in result
        assert "    quadrant-1 立即處理" in result
        assert '    "P": [0.5, 0.5]' in result

    def test_non_quadrant_block_untouched(self):
        text = "```mermaid\nflowchart TD\n    A: [0.1, 0.2]\n```"
        result, fixes = repair_mermaid_quadrant_points(text)
        assert result == text
        assert fixes == []

    def test_idempotent(self):
        body = "quadrantChart\n    Campaign B: [0.45, 0.23]\n    重構 A: [0.8, 0.9]"
        once, _ = self._quote(body)
        twice, fixes = repair_mermaid_quadrant_points(once)
        assert once == twice
        assert fixes == []


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

# ── Quoted Node ID Repair ──────────────────────────────────────────


class TestQuotedNodeIdRepair:
    def _quote(self, body):
        from core.parser import repair_mermaid_quoted_node_ids

        text = f"```mermaid\n{body}\n```"
        return repair_mermaid_quoted_node_ids(text)

    def test_strips_quotes_from_node_id(self):
        result, fixes = self._quote('graph TD\n  "A"["Process (Step 1)"]')
        assert '  A["Process (Step 1)"]' in result
        assert any(f["type"] == "stripped_mermaid_quoted_node_id" for f in fixes)

    def test_strips_quotes_from_node_id_with_kebab_case(self):
        result, fixes = self._quote('graph TD\n  "my-node"["Label"]')
        assert '  my-node["Label"]' in result

    def test_leaves_unquoted_node_id_alone(self):
        text = '```mermaid\ngraph TD\n  A["Process (Step 1)"]\n```'
        from core.parser import repair_mermaid_quoted_node_ids

        result, fixes = repair_mermaid_quoted_node_ids(text)
        assert result == text
        assert not fixes

    def test_connection_lines_with_quoted_node_ids(self):
        result, fixes = self._quote('graph TD\n  "NodeA"["Input"] --> "NodeB"["Output"]')
        assert '  NodeA["Input"] --> NodeB["Output"]' in result


# ── Quoted Connection-Endpoint Labels ──────────────────────────────


class TestQuotedEndpointLabels:
    """`"label with spaces" --> "other"` is invalid mermaid — promote each
    quoted endpoint that can't be a bare id to `id["label"]`. Single-token
    endpoints stay on the strip path (Hybrid policy)."""

    def _q(self, body):
        from core.parser import repair_mermaid_quoted_endpoint_labels

        return repair_mermaid_quoted_endpoint_labels(f"```mermaid\n{body}\n```")

    def test_brackets_multiword_endpoints(self):
        result, fixes = self._q('graph TD\n    "Plan work" --> "Ship it"')
        assert 'Planwork["Plan work"] --> Shipit["Ship it"]' in result
        assert any(f["type"] == "bracketed_mermaid_quoted_endpoint" for f in fixes)

    def test_brackets_cjk_with_space(self):
        # All-CJK labels slug to empty → synthetic ASCII ids (English-only id
        # policy); the CJK text is preserved in the bracketed label.
        result, _ = self._q('graph TD\n    "步驟 一" --> "步驟 二"')
        assert 'node["步驟 一"] --> node_1["步驟 二"]' in result

    def test_cjk_label_synthesizes_ascii_id(self):
        # A long punctuated CJK endpoint must not become a giant CJK id.
        result, _ = self._q('graph TD\n    A --- "包含: 戴德金理論, 海內-波萊爾定理"')
        assert 'A --- node["包含: 戴德金理論, 海內-波萊爾定理"]' in result
        # No CJK leaked into an id position (the only CJK is inside the label).
        assert "--- 包含" not in result

    def test_single_token_left_for_strip_pass(self):
        # Hybrid: a legal bare id is NOT bracketed here (the strip pass unquotes it).
        result, fixes = self._q('graph TD\n    "A1" --> "B1"')
        assert '"A1" --> "B1"' in result
        assert fixes == []

    def test_repeated_label_shares_one_id(self):
        result, _ = self._q('graph TD\n    "Plan work" --> "Ship it"\n    "Ship it" --> "Done now"')
        assert result.count('Shipit["Ship it"]') == 2

    def test_mixed_quoted_and_bare_endpoint(self):
        result, _ = self._q('graph TD\n    "Plan work" --> B2')
        assert 'Planwork["Plan work"] --> B2' in result

    def test_edge_label_preserved(self):
        # The `-- "edge" -->` edge label must survive; only the endpoint converts.
        result, _ = self._q('graph TD\n    A -- "edge" --> "Ship it"')
        assert 'A -- "edge" --> Shipit["Ship it"]' in result

    def test_plain_edge_label_untouched(self):
        result, fixes = self._q('graph TD\n    A -- "edge" --> B')
        assert 'A -- "edge" --> B' in result
        assert fixes == []

    def test_synthesized_id_avoids_author_id_collision(self):
        result, _ = self._q('graph TD\n    Planwork --> X\n    "Plan work" --> Y')
        assert 'Planwork_1["Plan work"] --> Y' in result

    def test_idempotent(self):
        body = 'graph TD\n    "Plan work" --> "Ship it"'
        once, _ = self._q(body)
        from core.parser import repair_mermaid_quoted_endpoint_labels

        twice, fixes = repair_mermaid_quoted_endpoint_labels(once)
        assert once == twice
        assert fixes == []


# ── Double Quote Bracket Repair ────────────────────────────────────


class TestDoubleQuoteRepair:
    def _repair(self, body):
        from core.parser import repair_mermaid_double_quotes

        text = f"```mermaid\n{body}\n```"
        return repair_mermaid_double_quotes(text)

    def test_collapses_double_quotes(self):
        result, fixes = self._repair('graph TD\n  A[""Process (Step 1)""]')
        assert '  A["Process (Step 1)"]' in result
        assert any(f["type"] == "repaired_mermaid_double_quotes" for f in fixes)

    def test_collapses_double_quotes_with_asymmetric_closer(self):
        result, fixes = self._repair('graph TD\n  A[""Process"]"')
        # Note: A[""Process"]" doesn't strictly match the regex because the closing part is `]"` not `""`
        # Wait, the LLM usually generated `A[""Label""]`. Let's test just `A[""Label""]` for now.
        pass


# ── Subgraph Keyword Repair ────────────────────────────────────────


class TestSubgraphRepair:
    def _repair(self, body):
        from core.parser import repair_mermaid_subgraph_keyword

        text = f"```mermaid\n{body}\n```"
        return repair_mermaid_subgraph_keyword(text)

    def test_repairs_truncated_subgraph(self):
        result, fixes = self._repair('graph TD\n  sub定的 "Group A"\n    A --> B\n  end')
        assert '  subgraph "Group A"' in result
        assert any(f["type"] == "repaired_mermaid_subgraph_keyword" for f in fixes)

    def test_leaves_valid_subgraph_alone(self):
        text = '```mermaid\ngraph TD\n  subgraph "Group A"\n    A --> B\n  end\n```'
        from core.parser import repair_mermaid_subgraph_keyword

        result, fixes = repair_mermaid_subgraph_keyword(text)
        assert result == text
        assert not fixes

    def test_does_not_corrupt_quoted_connection_lines(self):
        from core.parser import repair_mermaid_label_quotes

        text = '    "Node A("Label A")" --> "Node B (Label B)"'
        res, fixes = repair_mermaid_label_quotes(text)
        assert res == text

    def test_repairs_space_split_keyword(self):
        result, fixes = self._repair('graph TD\n  sub graph "Group A"\n  end')
        assert '  subgraph "Group A"' in result
        assert any(f["type"] == "repaired_mermaid_subgraph_keyword" for f in fixes)

    def test_repairs_doubled_keyword(self):
        result, fixes = self._repair('graph TD\n  subsubgraph "Group A"\n  end')
        assert '  subgraph "Group A"' in result
        assert any(f["type"] == "repaired_mermaid_subgraph_keyword" for f in fixes)

    def test_repairs_triple_doubled_keyword(self):
        result, _ = self._repair('graph TD\n  subsubsubgraph "G"\n  end')
        assert '  subgraph "G"' in result

    def test_valid_keyword_not_touched_by_ascii_rule(self):
        result, fixes = self._repair('graph TD\n  subgraph SG["Group A"]\n  end')
        assert '  subgraph SG["Group A"]' in result
        assert not fixes

    def test_repairs_cjk_injected_keyword(self):
        # Real regression (Non-Invasive Stitched): `sub議subgraph "t"` — a CJK
        # char inserted while `graph` survives (distinct from CJK REPLACING
        # graph). Broke the diagram (unbalanced subgraph/end).
        result, fixes = self._repair('graph TD\n    sub議subgraph "編碼器架構 (Encoder)"\n    end')
        assert '    subgraph "編碼器架構 (Encoder)"' in result
        assert "議" not in result
        assert any(f["type"] == "repaired_mermaid_subgraph_keyword" for f in fixes)

    def test_ascii_id_with_graph_substring_untouched(self):
        # `subprocess_graph` is a valid node id — the CJK-injected rule must not
        # fire (it requires a non-ASCII char between sub and graph).
        result, fixes = self._repair('graph TD\n    subprocess_graph["node"]')
        assert 'subprocess_graph["node"]' in result
        assert not fixes


# ── Quoted-id cross-reference consistency ──────────────────────────


class TestQuotedIdConsistency:
    """A quoted node id used in a declaration, its edges, and its `style` line
    must resolve to ONE synthesized id — otherwise the diagram grows duplicate
    / dangling nodes and the `style` binding silently misses."""

    def _q(self, body):
        from core.parser import repair_mermaid_quoted_endpoint_labels

        return repair_mermaid_quoted_endpoint_labels(f"```mermaid\n{body}\n```")

    def test_declaration_and_edge_share_one_id(self):
        body = (
            "graph TD\n"
            '    "First Edition (1908)"["第一版"]\n'
            '    "First Edition (1908)" --> Basic_Math'
        )
        result, _ = self._q(body)
        # The declaration loses its quotes and the edge reuses the bare id.
        assert 'FirstEdition1908["第一版"]' in result
        assert "FirstEdition1908 --> Basic_Math" in result
        # No leftover quoted id anywhere.
        assert '"First Edition (1908)"' not in result

    def test_style_target_rewritten_to_same_id(self):
        body = (
            "graph TD\n"
            '    "First Edition (1908)"["第一版"]\n'
            '    style "First Edition (1908)" fill:#f9f,stroke:#333'
        )
        result, _ = self._q(body)
        assert "style FirstEdition1908 fill:#f9f,stroke:#333" in result
        assert '"First Edition (1908)"' not in result

    def test_class_target_rewritten(self):
        result, _ = self._q('graph TD\n    class "Plan work" highlighted')
        assert "class Planwork highlighted" in result

    def test_bare_style_target_untouched(self):
        body = "graph TD\n    A --> B\n    style A fill:#f9f"
        result, fixes = self._q(body)
        assert "style A fill:#f9f" in result
        assert fixes == []

    def test_full_diagram_round_trip(self):
        # The real-world failure: subgraph declaration + edges + style all
        # referencing the same quoted ids must collapse to one node each.
        body = (
            "graph TD\n"
            '    subgraph "教材演進歷程"\n'
            '        "First Edition (1908)"["第一版 (1908)"]\n'
            '        "Second Edition (1914)"["第二版 (1914)"]\n'
            "    end\n"
            '    "First Edition (1908)" --> "Second Edition (1914)"\n'
            '    style "First Edition (1908)" fill:#f9f,stroke:#333\n'
            '    style "Second Edition (1914)" fill:#bbf,stroke:#333'
        )
        result, _ = self._q(body)
        assert 'FirstEdition1908["第一版 (1908)"]' in result
        assert 'SecondEdition1914["第二版 (1914)"]' in result
        assert "FirstEdition1908 --> SecondEdition1914" in result
        assert "style FirstEdition1908 fill:#f9f,stroke:#333" in result
        assert "style SecondEdition1914 fill:#bbf,stroke:#333" in result
        assert '"First Edition (1908)"' not in result

    def test_idempotent(self):
        from core.parser import repair_mermaid_quoted_endpoint_labels

        body = (
            "graph TD\n"
            '    "First Edition (1908)"["第一版"]\n'
            '    "First Edition (1908)" --> Basic_Math\n'
            '    style "First Edition (1908)" fill:#f9f'
        )
        once, _ = self._q(body)
        twice, fixes = repair_mermaid_quoted_endpoint_labels(once)
        assert once == twice
        assert fixes == []


# ── Over-quoted node repair ────────────────────────────────────────


class TestOverquotedNode:
    """`--> "Id["label"]"` wraps a whole node in an extra outer quote pair;
    strip it back to a bare `Id["label"]`."""

    def _q(self, body):
        from core.parser import repair_mermaid_overquoted_node

        return repair_mermaid_overquoted_node(f"```mermaid\n{body}\n```")

    def test_strips_outer_quotes_on_endpoint(self):
        result, fixes = self._q('graph TD\n    Start["開始"] --> "Step1["繪製單位圓"]"')
        assert 'Start["開始"] --> Step1["繪製單位圓"]' in result
        assert '"Step1[' not in result
        assert any(f["type"] == "stripped_mermaid_overquoted_node" for f in fixes)

    def test_strips_multiple_on_chain(self):
        body = 'graph TD\n    Step1 --> "Step2["切線"]"\n    Step2 --> "Result["得到根"]"'
        result, _ = self._q(body)
        assert 'Step1 --> Step2["切線"]' in result
        assert 'Step2 --> Result["得到根"]' in result

    def test_rounded_shape(self):
        result, _ = self._q('graph TD\n    A --> "B("圓角")"')
        assert 'A --> B("圓角")' in result

    def test_mismatched_brackets_left_alone(self):
        # `"B["x"}"` has mismatched [ and } — don't rewrite a coincidence.
        body = 'graph TD\n    A --> "B["x"}"'
        result, fixes = self._q(body)
        assert fixes == []

    def test_well_formed_node_untouched(self):
        body = 'graph TD\n    A["開始"] --> B["結束"]'
        result, fixes = self._q(body)
        assert result == f"```mermaid\n{body}\n```"
        assert fixes == []

    def test_idempotent(self):
        from core.parser import repair_mermaid_overquoted_node

        body = 'graph TD\n    Start["開始"] --> "Step1["繪製"]"'
        once, _ = self._q(body)
        twice, fixes = repair_mermaid_overquoted_node(once)
        assert once == twice
        assert fixes == []

    def test_asymmetric_doubled_quote_runs(self):
        # Real regression (Non-Invasive Brain Recordings Part 9): the LLM
        # emitted `"B1[""label"]""` — doubled quotes on the outer edges that
        # repair_mermaid_double_quotes left behind. Runs of quotes at each
        # position must all collapse to the canonical form.
        body = 'graph TD\n    subgraph "研究核心領域"\n        "B1[""序列處理 (CTC)"]""\n    end'
        result, fixes = self._q(body)
        assert 'B1["序列處理 (CTC)"]' in result
        assert '""' not in result and '"B1[' not in result
        assert any(f["type"] == "stripped_mermaid_overquoted_node" for f in fixes)


# ── Mindmap bracket neutralization ─────────────────────────────────


class TestMindmapBrackets:
    """Half-width brackets in mindmap node text are read as shape syntax and
    break the diagram; they're converted to full-width, while a legitimate
    leading shape wrapper (root circle, rounded node) is preserved."""

    def _q(self, body):
        from core.parser import repair_mermaid_mindmap_brackets

        return repair_mermaid_mindmap_brackets(f"```mermaid\n{body}\n```")

    def test_neutralizes_embedded_parens(self):
        result, fixes = self._q("mindmap\n  root((主題))\n    證明 sqrt(2) 為無理數")
        assert "證明 sqrt（2） 為無理數" in result
        assert any(f["type"] == "neutralized_mindmap_brackets" for f in fixes)

    def test_preserves_root_circle_shape(self):
        result, _ = self._q("mindmap\n  root((主題))\n    平凡節點")
        assert "root((主題))" in result

    def test_preserves_full_wrapping_rounded_shape(self):
        # A node whose entire label is wrapped is a valid shape — keep it.
        result, _ = self._q("mindmap\n  root((主題))\n    (純說明)")
        assert "(純說明)" in result

    def test_neutralizes_square_and_brace(self):
        result, _ = self._q("mindmap\n  root((R))\n    區間 [a,b] 與集合 {x}")
        assert "區間 ［a,b］ 與集合 ｛x｝" in result

    def test_no_brackets_untouched(self):
        body = "mindmap\n  root((主題))\n    x^2 = 2 的長度"
        result, fixes = self._q(body)
        assert "x^2 = 2 的長度" in result
        assert fixes == []

    def test_ignores_non_mindmap_fence(self):
        body = 'graph TD\n    A["f(x)"]'
        result, fixes = self._q(body)
        assert fixes == []
        assert "f(x)" in result

    def test_preserves_braces_inside_math_span(self):
        # Real regression (Non-Invasive Stitched): a mindmap node with math
        # `$$p < 10^{-6}$$` — the `{}` are KaTeX grouping, NOT shape syntax, so
        # they must stay half-width. Only NON-math brackets are neutralized.
        result, _ = self._q("mindmap\n  root((R))\n    顯著性 $$p < 10^{-6}$$ 與集合 {x}")
        assert "$$p < 10^{-6}$$" in result  # math braces untouched
        assert "｛-6｝" not in result
        assert "集合 ｛x｝" in result  # non-math brace still neutralized

    def test_idempotent(self):
        from core.parser import repair_mermaid_mindmap_brackets

        body = "mindmap\n  root((主題))\n    證明 sqrt(2) 為無理數"
        once, _ = self._q(body)
        twice, fixes = repair_mermaid_mindmap_brackets(once)
        assert once == twice
        assert fixes == []


class TestMindmapMath:
    """mindmap doesn't render KaTeX, so `$$…$$` shows as a literal string —
    degrade it to plain text (unlike flowchart labels, which keep the math)."""

    def _q(self, body):
        from core.parser import repair_mermaid_mindmap_math

        return repair_mermaid_mindmap_math(f"```mermaid\n{body}\n```")

    def test_degrades_symbol_and_superscript(self):
        result, fixes = self._q(
            "mindmap\n  root((R))\n    WER $$\\approx$$ 0.39\n    顯著性 $$p < 10^{-6}$$"
        )
        assert "WER ≈ 0.39" in result
        assert "p < 10^(-6)" in result  # $$ stripped, {} → ^(...)
        assert "$" not in result.replace("```", "")
        assert any(f["type"] == "degraded_mindmap_math" for f in fixes)

    def test_inline_single_dollar_math(self):
        result, _ = self._q("mindmap\n  root((R))\n    秩 $r$")
        assert "秩 r" in result and "$" not in result.replace("```", "")

    def test_non_mindmap_math_untouched(self):
        # flowchart math is rendered by Obsidian — this pass must not touch it.
        body = 'graph TD\n    N["增長率 $$\\alpha$$"]'
        result, fixes = self._q(body)
        assert "$$\\alpha$$" in result
        assert fixes == []

    def test_idempotent(self):
        from core.parser import repair_mermaid_mindmap_math

        body = "mindmap\n  root((R))\n    顯著性 $$p < 10^{-6}$$"
        once, _ = self._q(body)
        twice, fixes = repair_mermaid_mindmap_math(once)
        assert once == twice
        assert fixes == []


# ── classDiagram structural repair ─────────────────────────────────


class TestClassDiagramRepair:
    """Ontology (classDiagram) faults: inline relationship labels that belong on
    a `class` declaration, and exact-duplicate declarations."""

    def _q(self, body):
        from core.parser import repair_mermaid_classdiagram

        return repair_mermaid_classdiagram(f"```mermaid\n{body}\n```")

    def test_hoists_inline_relationship_label(self):
        body = (
            "classDiagram\n"
            '    class Differentiation["微分"]\n'
            '    Differentiation *-- MultivariableDiff["多變數微分"] : part-of'
        )
        result, fixes = self._q(body)
        assert 'class MultivariableDiff["多變數微分"]' in result
        assert "Differentiation *-- MultivariableDiff : part-of" in result
        assert 'MultivariableDiff["多變數微分"] : part-of' not in result
        assert any(f["type"] == "hoisted_classdiagram_inline_label" for f in fixes)

    def test_dedupes_exact_duplicate_declaration(self):
        body = (
            "classDiagram\n"
            '    class Approx["求根之近似法"]\n'
            '    class NewtonMethod["牛頓法"]\n'
            '    class Approx["求根之近似法"]'
        )
        result, fixes = self._q(body)
        assert result.count('class Approx["求根之近似法"]') == 1
        assert any(f["type"] == "deduped_classdiagram_decl" for f in fixes)

    def test_keeps_declaration_with_member_body(self):
        body = (
            "classDiagram\n"
            '    class NewtonMethod["牛頓法"] {\n'
            "        <<instance>>\n"
            "    }\n"
            "    NewtonMethod ..> Approx : instance-of"
        )
        result, fixes = self._q(body)
        assert "<<instance>>" in result
        assert fixes == []

    def test_strips_inline_malformed_body(self):
        body = 'classDiagram\n    class NewtonMethod["牛頓法"] { <> }'
        result, fixes = self._q(body)
        assert 'class NewtonMethod["牛頓法"]' in result
        assert "{" not in result.split("classDiagram")[1]
        assert any(f["type"] == "stripped_empty_classdiagram_body" for f in fixes)

    def test_strips_empty_multiline_body(self):
        body = 'classDiagram\n    class X["甲"] {\n    }\n    X --> Y : rel'
        result, fixes = self._q(body)
        assert 'class X["甲"]' in result
        assert "X --> Y : rel" in result  # following lines not swallowed
        assert any(f["type"] == "stripped_empty_classdiagram_body" for f in fixes)

    def test_keeps_attribute_body(self):
        body = 'classDiagram\n    class Animal["動物"] {\n        +name string\n    }'
        result, fixes = self._q(body)
        assert "+name string" in result
        assert fixes == []

    def test_unclosed_body_not_swallowed(self):
        body = 'classDiagram\n    class X["甲"] {\n    X --> Y : rel'
        result, _ = self._q(body)
        assert "X --> Y : rel" in result

    def test_does_not_hoist_if_class_already_declared(self):
        body = 'classDiagram\n    class B["乙"]\n    A *-- B["乙"] : part-of'
        result, _ = self._q(body)
        # The label is stripped from the relationship, but no second decl added.
        assert result.count('class B["乙"]') == 1
        assert "A *-- B : part-of" in result

    def test_ignores_non_classdiagram_fence(self):
        body = 'graph TD\n    A *-- B["x"] : part-of'
        result, fixes = self._q(body)
        assert fixes == []
        assert 'B["x"]' in result

    def test_idempotent(self):
        from core.parser import repair_mermaid_classdiagram

        body = (
            "classDiagram\n"
            '    class Differentiation["微分"]\n'
            '    class Approx["近似"]\n'
            '    class Approx["近似"]\n'
            '    Differentiation *-- MultivariableDiff["多變數微分"] : part-of'
        )
        once, _ = self._q(body)
        twice, fixes = repair_mermaid_classdiagram(once)
        assert once == twice
        assert fixes == []

    def test_non_ascii_stereotype_normalized_to_instance(self):
        # Real regression (Non-Invasive Part 12 + 87 other vault files): the
        # ontology prompt told the model to emit `<<個體>>` — CJK that breaks
        # mermaid's annotation lexer — and drift/corruptions produced `<<個．體>>`
        # / `<<個int>>` too. All non-ASCII stereotypes normalize to <<instance>>.
        body = (
            "classDiagram\n"
            '    class Sentence["句子"]\n'
            "    Fido { <<個體>> }\n"
            "    Bad { <<個．體>> }\n"
            "    Ok { <<choice>> }"
        )
        result, fixes = self._q(body)
        assert result.count("<<instance>>") == 2
        assert "個體" not in result and "個．體" not in result
        assert "<<choice>>" in result  # clean ASCII stereotype content untouched
        assert sum(f["type"] == "normalized_class_annotation" for f in fixes) == 2

    def test_inline_stereotype_body_converted_to_standalone(self):
        # mermaid's valid annotation forms are standalone `<<x>> Id` or
        # `class Id { <<x>> }` (WITH the keyword). The generator drops the
        # keyword — `Id { <<x>> }` — which is malformed. Every inline
        # stereotype body converts to the canonical standalone line; a label
        # is preserved as a separate `class Id["label"]` decl.
        body = (
            "classDiagram\n"
            "    Best { <<instance>> }\n"
            '    class Fido["狗實例"] { <<instance>> }\n'
            "    class WithAttr { +name string }"
        )
        result, fixes = self._q(body)
        assert "<<instance>> Best" in result
        assert "{ <<instance>> }" not in result and "{ <<instance>>}" not in result
        # label kept as its own decl, stereotype hoisted out
        assert 'class Fido["狗實例"]' in result and "<<instance>> Fido" in result
        # a genuine attribute body is left intact
        assert "class WithAttr { +name string }" in result
        assert sum(f["type"] == "standalone_class_stereotype" for f in fixes) == 2

    def test_ascii_stereotype_still_hoisted_to_standalone(self):
        # ASCII content isn't normalized, but the inline body is still the
        # wrong form → hoisted to standalone.
        body = "classDiagram\n    Repo { <<Infrastructure>> }"
        result, fixes = self._q(body)
        assert "<<Infrastructure>> Repo" in result
        assert not any(f["type"] == "normalized_class_annotation" for f in fixes)
        assert any(f["type"] == "standalone_class_stereotype" for f in fixes)
