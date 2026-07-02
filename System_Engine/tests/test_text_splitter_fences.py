"""Additional tests for TextSplitter around the new fence-region precomputation.

The main `test_text_splitter.py` covers the existing public behaviour. These
tests target the perf-relevant invariants of the refactor: fence regions
should be computed once per call, unterminated fences should still be
protected, and identical inputs should produce identical chunk boundaries.
"""

import pytest

from services.text_splitter import TextSplitter


class TestFenceRegions:
    def test_terminated_fence_region_includes_close_line(self):
        text = "before\n\n```python\nx = 1\n```\n\nafter"
        regions = TextSplitter._compute_fence_regions(text)
        assert len(regions) == 1
        start, end = regions[0]
        assert text[start : start + 3] == "```"
        # The region should extend past the closing fence line.
        assert "```\n" in text[start:end]

    def test_unterminated_fence_guards_rest_of_doc(self):
        text = "intro\n\n```python\nno close after this\nlots of text\n"
        regions = TextSplitter._compute_fence_regions(text)
        assert len(regions) == 1
        start, end = regions[0]
        assert end == len(text)

    def test_no_fence_no_regions(self):
        assert TextSplitter._compute_fence_regions("plain text with no fences") == []

    def test_multiple_fences(self):
        text = "```py\nA\n```\n\nmiddle\n\n```js\nB\n```\n"
        regions = TextSplitter._compute_fence_regions(text)
        assert len(regions) == 2
        # Regions should be in order.
        assert regions[0][0] < regions[1][0]


class TestFenceProtectionAcrossSplits:
    def test_split_does_not_land_in_mermaid_block(self):
        # Force a split point right after a mermaid opener.
        body = (
            "intro paragraph.\n\n" * 5
            + "```mermaid\ngraph TD\nA --> B\nC --> D\nE --> F\n```\n\n"
            + "outro paragraph.\n\n" * 5
        )
        splitter = TextSplitter(chunk_size=80, overlap=20)
        chunks = splitter.split_text(body)
        # The mermaid block must appear intact in some chunk.
        joined_chunks = [c for c in chunks if "```mermaid" in c]
        assert any(
            "graph TD" in c and "E --> F" in c and c.rstrip().endswith("```") for c in joined_chunks
        )

    def test_legacy_inside_code_block_helper_still_works(self):
        text = "outside\n```py\ninside\n```\nafter"
        splitter = TextSplitter()
        # Offset inside the fence:
        inside_offset = text.index("inside")
        assert splitter._inside_code_block(text, inside_offset)
        # Offset before the fence:
        assert not splitter._inside_code_block(text, 3)
        # Offset after the close:
        after_offset = text.index("after")
        assert not splitter._inside_code_block(text, after_offset)


class TestDeterminism:
    def test_split_is_stable(self):
        """Calling split_text twice should produce identical results."""
        text = ("Paragraph N.\n\n" * 200).strip()
        s = TextSplitter(chunk_size=300, overlap=50)
        assert s.split_text(text) == s.split_text(text)
        spans = s.split_text_with_spans(text)
        # Spans should be monotonic.
        for a, b in zip(spans, spans[1:]):
            assert a["start"] <= b["start"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
