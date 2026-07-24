"""Unit tests for services.md_block_scanner.

We test the scanner's structural correctness on synthetic minimal inputs
(one behaviour per test) and then run it against the corpus to verify the
coverage invariant.
"""

import time
from pathlib import Path


import pytest

from services.md_block_scanner import (
    BlockKind,
    covers_all,
    leaf_blocks,
    scan,
)


CORPUS_DIR = Path(__file__).parent / "corpus"


def _kinds(blocks):
    """Return tuple of BlockKind values, for assertion clarity."""
    return tuple(b.kind for b in blocks)


# ── Empty / trivial input ──────────────────────────────────────────


class TestEmptyAndTrivial:
    def test_empty_returns_empty(self):
        assert scan("") == []

    def test_single_blank_line(self):
        blocks = scan("\n")
        assert _kinds(blocks) == (BlockKind.BLANK,)

    def test_single_paragraph_no_trailing_newline(self):
        text = "Just a single paragraph."
        blocks = scan(text)
        assert _kinds(blocks) == (BlockKind.PARAGRAPH,)
        assert blocks[0].text == text
        assert (blocks[0].start, blocks[0].end) == (0, len(text))

    def test_two_paragraphs_separated_by_blank(self):
        text = "First.\n\nSecond.\n"
        kinds = _kinds(scan(text))
        assert kinds == (BlockKind.PARAGRAPH, BlockKind.BLANK, BlockKind.PARAGRAPH)


# ── Frontmatter ────────────────────────────────────────────────────


class TestFrontmatter:
    def test_basic_frontmatter(self):
        text = "---\ntitle: x\n---\n\nBody.\n"
        blocks = scan(text)
        assert blocks[0].kind == BlockKind.FRONTMATTER
        assert blocks[0].atomic is True
        assert blocks[0].text.startswith("---") and blocks[0].text.endswith("---\n")

    def test_unclosed_frontmatter_falls_back_to_body(self):
        """If `---` opens but never closes, we don't treat it as frontmatter."""
        text = "---\ntitle: x\nbody continues forever"
        blocks = scan(text)
        assert blocks[0].kind != BlockKind.FRONTMATTER

    def test_frontmatter_only_at_file_start(self):
        text = "Some body.\n\n---\ntitle: late\n---\n"
        kinds = _kinds(scan(text))
        # The mid-file `---`/`---` pair must NOT be parsed as frontmatter.
        assert BlockKind.FRONTMATTER not in kinds

    def test_pure_frontmatter_no_body(self):
        text = "---\ntitle: x\n---\n"
        blocks = scan(text)
        assert blocks[0].kind == BlockKind.FRONTMATTER
        # Everything after frontmatter (one trailing newline at most) is fine.


# ── ATX heading ────────────────────────────────────────────────────


class TestAtxHeading:
    def test_h1(self):
        blocks = scan("# Title\n")
        assert blocks[0].kind == BlockKind.HEADING
        assert blocks[0].level == 1
        assert blocks[0].heading_text == "Title"

    def test_h6_with_trailing_hashes(self):
        blocks = scan("###### Sub-sub-sub ######\n")
        assert blocks[0].kind == BlockKind.HEADING
        assert blocks[0].level == 6
        assert blocks[0].heading_text == "Sub-sub-sub"

    def test_indented_heading_up_to_3_spaces(self):
        blocks = scan("   ## Indented\n")
        assert blocks[0].kind == BlockKind.HEADING
        assert blocks[0].level == 2

    def test_four_space_indent_is_not_heading(self):
        """CommonMark: 4+ spaces is code block (we treat as paragraph)."""
        blocks = scan("    # Not a heading\n")
        assert blocks[0].kind == BlockKind.PARAGRAPH


# ── Setext heading ─────────────────────────────────────────────────


class TestSetextHeading:
    def test_setext_h1(self):
        text = "Title here\n===\n"
        blocks = scan(text)
        assert blocks[0].kind == BlockKind.HEADING
        assert blocks[0].level == 1
        assert "Title here" in blocks[0].heading_text

    def test_setext_h2(self):
        text = "Title here\n---\n"
        blocks = scan(text)
        assert blocks[0].kind == BlockKind.HEADING
        assert blocks[0].level == 2

    def test_hr_when_no_preceding_paragraph(self):
        text = "First para.\n\n---\n\nSecond.\n"
        blocks = scan(text)
        kinds = _kinds(blocks)
        # Sequence: PARAGRAPH, BLANK, HR, BLANK, PARAGRAPH
        assert BlockKind.HR in kinds


# ── HR ─────────────────────────────────────────────────────────────


class TestHr:
    def test_dash_hr(self):
        blocks = scan("para.\n\n---\n\nmore.\n")
        assert any(b.kind == BlockKind.HR for b in blocks)

    def test_underscore_hr(self):
        blocks = scan("para.\n\n___\n\nmore.\n")
        assert any(b.kind == BlockKind.HR for b in blocks)

    def test_star_hr(self):
        blocks = scan("para.\n\n***\n\nmore.\n")
        assert any(b.kind == BlockKind.HR for b in blocks)


# ── Code fence ─────────────────────────────────────────────────────


class TestCodeFence:
    def test_basic_fence(self):
        text = "```python\nx = 1\n```\n"
        blocks = scan(text)
        assert blocks[0].kind == BlockKind.CODE_FENCE
        assert blocks[0].atomic is True
        assert "x = 1" in blocks[0].text

    def test_tilde_fence(self):
        text = "~~~\nplain code\n~~~\n"
        blocks = scan(text)
        assert blocks[0].kind == BlockKind.CODE_FENCE

    def test_unterminated_fence_extends_to_eof(self):
        text = "```\nx = 1\ny = 2\nno close"
        blocks = scan(text)
        fence_blocks = [b for b in blocks if b.kind == BlockKind.CODE_FENCE]
        assert len(fence_blocks) == 1
        # Unterminated fence should run to end of file.
        assert fence_blocks[0].end == len(text)

    def test_longer_close_required_for_long_open(self):
        """A `````fence``` cannot be closed by `````` 3-backtick line."""
        text = "````\ncode containing ``` inside\n````\n"
        blocks = scan(text)
        # Note: this exercises that fence length matters; we just confirm
        # we get one fence block covering all of it.
        assert sum(1 for b in blocks if b.kind == BlockKind.CODE_FENCE) == 1


# ── Math block ─────────────────────────────────────────────────────


class TestMathBlock:
    def test_basic_math(self):
        text = "$$\nx = y + z\n$$\n"
        blocks = scan(text)
        assert blocks[0].kind == BlockKind.MATH_BLOCK
        assert blocks[0].atomic is True

    def test_single_line_math_closes_on_same_line(self):
        text = "$$a^2 + b^2 = c^2.$$\n\nProse between formulas.\n\n$$x = y.$$\n"
        blocks = scan(text)
        math_blocks = [b for b in blocks if b.kind == BlockKind.MATH_BLOCK]

        assert [b.text for b in math_blocks] == [
            "$$a^2 + b^2 = c^2.$$\n",
            "$$x = y.$$\n",
        ]
        assert any(b.kind == BlockKind.PARAGRAPH and "Prose between" in b.text for b in blocks)

    def test_multiline_math_can_close_at_end_of_content_line(self):
        text = "$$\\begin{array}\nx & y\n\\end{array}$$\nAfter.\n"
        blocks = scan(text)

        assert blocks[0].kind == BlockKind.MATH_BLOCK
        assert blocks[0].text == "$$\\begin{array}\nx & y\n\\end{array}$$\n"
        assert blocks[1].kind == BlockKind.PARAGRAPH

    def test_escaped_dollars_do_not_close_single_line_math(self):
        text = "$$ price \\$$ remains\ncontinued\n$$\n"
        blocks = scan(text)

        assert len(blocks) == 1
        assert blocks[0].kind == BlockKind.MATH_BLOCK
        assert blocks[0].text == text


# ── Blockquote vs callout ─────────────────────────────────────────


class TestBlockquoteVsCallout:
    def test_plain_blockquote(self):
        text = "> Wisdom of the ancients.\n> Sometimes wrong.\n"
        blocks = scan(text)
        assert blocks[0].kind == BlockKind.BLOCKQUOTE
        assert blocks[0].atomic is True

    def test_obsidian_callout(self):
        text = "> [!note]\n> A reminder.\n> With detail.\n"
        blocks = scan(text)
        assert blocks[0].kind == BlockKind.CALLOUT
        assert blocks[0].atomic is True

    def test_callout_with_collapse_marker(self):
        text = "> [!warning]+\n> Collapsible callout.\n"
        blocks = scan(text)
        assert blocks[0].kind == BlockKind.CALLOUT


# ── Tables ─────────────────────────────────────────────────────────


class TestTable:
    def test_basic_table(self):
        text = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
        blocks = scan(text)
        assert blocks[0].kind == BlockKind.TABLE
        assert blocks[0].atomic is True

    def test_table_with_alignment(self):
        text = "| a | b |\n|:---|---:|\n| 1 | 2 |\n"
        blocks = scan(text)
        assert blocks[0].kind == BlockKind.TABLE

    def test_pipe_in_paragraph_is_not_table(self):
        """A line containing `|` is not a table unless followed by a separator row."""
        text = "Just some|prose with|pipes.\n"
        blocks = scan(text)
        assert blocks[0].kind == BlockKind.PARAGRAPH


# ── Lists ──────────────────────────────────────────────────────────


class TestList:
    def test_simple_bullet_list(self):
        text = "- one\n- two\n- three\n"
        blocks = scan(text)
        # Expect 1 LIST container + 3 LIST_ITEM blocks.
        assert blocks[0].kind == BlockKind.LIST
        assert blocks[0].atomic is False
        items = [b for b in blocks if b.kind == BlockKind.LIST_ITEM]
        assert len(items) == 3
        for it in items:
            assert it.atomic is True
            assert it.parent_kind == BlockKind.LIST

    def test_numbered_list(self):
        text = "1. one\n2. two\n3. three\n"
        items = [b for b in scan(text) if b.kind == BlockKind.LIST_ITEM]
        assert len(items) == 3

    def test_nested_list_stays_with_parent(self):
        """Sub-items belong to their parent top-level item, not as separate
        LIST_ITEMs. This is the Gemini Issue A fix — top-level items are
        the splittable unit."""
        text = "- Top one\n    - Sub 1a\n    - Sub 1b\n- Top two\n    - Sub 2a\n- Top three\n"
        items = [b for b in scan(text) if b.kind == BlockKind.LIST_ITEM]
        assert len(items) == 3
        # First item must contain its sub-items.
        assert "Sub 1a" in items[0].text
        assert "Sub 1b" in items[0].text
        # Second item ditto.
        assert "Sub 2a" in items[1].text
        # Third has no sub-items.
        assert "Sub" not in items[2].text

    def test_long_outline_splittable_into_many_items(self):
        """REGRESSION (Gemini Issue A): a 50-item outline must produce 50
        LIST_ITEM blocks, not one giant atomic LIST."""
        lines = [f"- Item {i}\n" for i in range(50)]
        text = "".join(lines)
        items = [b for b in scan(text) if b.kind == BlockKind.LIST_ITEM]
        assert len(items) == 50

    def test_list_with_blank_lines_between_items(self):
        """Loose list — blank lines between items are allowed."""
        text = "- one\n\n- two\n\n- three\n"
        items = [b for b in scan(text) if b.kind == BlockKind.LIST_ITEM]
        assert len(items) == 3


# ── Block.start/end coverage on corpus ─────────────────────────────


@pytest.mark.parametrize(
    "corpus_file", sorted(p.name for p in CORPUS_DIR.glob("*.md") if p.name != "README.md")
)
class TestCorpusCoverage:
    """For every corpus file, scanner output must cover the source exactly."""

    def test_leaf_blocks_cover_full_text(self, corpus_file):
        text = (CORPUS_DIR / corpus_file).read_text(encoding="utf-8")
        blocks = scan(text)
        assert covers_all(text, blocks), f"coverage failed for {corpus_file}"

    def test_leaf_blocks_strictly_monotonic(self, corpus_file):
        text = (CORPUS_DIR / corpus_file).read_text(encoding="utf-8")
        leaves = leaf_blocks(scan(text))
        for a, b in zip(leaves, leaves[1:]):
            assert a.end == b.start, (
                f"gap in {corpus_file}: block ending {a.end} "
                f"({a.kind.value!r}) → next starts {b.start}"
            )

    def test_list_container_contains_its_items(self, corpus_file):
        """For each LIST container, all LIST_ITEMs immediately following it
        must lie within its span."""
        text = (CORPUS_DIR / corpus_file).read_text(encoding="utf-8")
        blocks = scan(text)
        for i, b in enumerate(blocks):
            if b.kind != BlockKind.LIST:
                continue
            # Find subsequent contiguous LIST_ITEMs
            j = i + 1
            items_total_end = b.start
            while (
                j < len(blocks)
                and blocks[j].kind == BlockKind.LIST_ITEM
                and blocks[j].parent_kind == BlockKind.LIST
            ):
                assert b.start <= blocks[j].start
                assert blocks[j].end <= b.end
                items_total_end = blocks[j].end
                j += 1
            # The items collectively should cover the LIST's range (or close).
            assert items_total_end == b.end, (
                f"LIST in {corpus_file} starts at {b.start}, ends at {b.end}, "
                f"but its LIST_ITEMs only cover up to {items_total_end}"
            )

    def test_all_atomic_blocks_are_self_contained(self, corpus_file):
        """Every atomic block's text must equal `text[start:end]` — no off-by-one."""
        text = (CORPUS_DIR / corpus_file).read_text(encoding="utf-8")
        for b in scan(text):
            assert b.text == text[b.start : b.end], (
                f"text/offset mismatch in {corpus_file} for {b.kind.value} at {b.start}-{b.end}"
            )


# ── Perf budget ───────────────────────────────────────────────────


def test_corpus_scan_under_50ms_each():
    """P1 acceptance: scanner runs each corpus file in < 50ms."""
    for path in CORPUS_DIR.glob("*.md"):
        if path.name == "README.md":
            continue
        text = path.read_text(encoding="utf-8")
        # Warm-up (regex cache, etc.)
        scan(text)
        t0 = time.perf_counter()
        for _ in range(10):
            scan(text)
        avg_ms = (time.perf_counter() - t0) * 1000 / 10
        assert avg_ms < 50, f"{path.name} took {avg_ms:.1f} ms (budget 50)"


# ── Specific corpus expectations ──────────────────────────────────


class TestCorpusSpecifics:
    """A few hand-curated assertions to catch regression on specific files."""

    def test_mermaid_heavy_has_5_code_fences(self):
        text = (CORPUS_DIR / "mermaid_heavy.md").read_text(encoding="utf-8")
        n_fences = sum(1 for b in scan(text) if b.kind == BlockKind.CODE_FENCE)
        assert n_fences == 5

    def test_obsidian_callouts_has_8_callouts(self):
        text = (CORPUS_DIR / "obsidian_callouts.md").read_text(encoding="utf-8")
        n_callouts = sum(1 for b in scan(text) if b.kind == BlockKind.CALLOUT)
        assert n_callouts == 8

    def test_outline_dominant_has_8_list_items(self):
        text = (CORPUS_DIR / "outline_dominant.md").read_text(encoding="utf-8")
        n_items = sum(1 for b in scan(text) if b.kind == BlockKind.LIST_ITEM)
        assert n_items == 8

    def test_nested_lists_and_tables_has_tables(self):
        text = (CORPUS_DIR / "nested_lists_and_tables.md").read_text(encoding="utf-8")
        n_tables = sum(1 for b in scan(text) if b.kind == BlockKind.TABLE)
        assert n_tables >= 2

    def test_long_unstructured_essay_is_mostly_paragraphs(self):
        """The unstructured-essay corpus should produce headings ≤ 1 and many paragraphs."""
        text = (CORPUS_DIR / "long_unstructured_essay.md").read_text(encoding="utf-8")
        blocks = scan(text)
        n_headings = sum(1 for b in blocks if b.kind == BlockKind.HEADING)
        n_paragraphs = sum(1 for b in blocks if b.kind == BlockKind.PARAGRAPH)
        assert n_headings <= 1
        assert n_paragraphs >= 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
