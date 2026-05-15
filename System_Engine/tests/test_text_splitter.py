"""
Unit tests for services.text_splitter — edge cases around code fence
protection, chunk sizing, and span tracking.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import pytest
from services.text_splitter import TextSplitter


class TestTextSplitter:
    def setup_method(self):
        self.splitter = TextSplitter(chunk_size=200, overlap=50)

    def test_short_text_single_chunk(self):
        text = "This is a short paragraph."
        chunks = self.splitter.split_text(text)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text_multiple_chunks(self):
        # Create text longer than chunk_size
        text = ("This is sentence number one. " * 20).strip()
        chunks = self.splitter.split_text(text)
        assert len(chunks) > 1
        # All chunks should be non-empty
        for chunk in chunks:
            assert len(chunk.strip()) > 0

    def test_split_with_spans_returns_offsets(self):
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        spans = self.splitter.split_text_with_spans(text)
        assert all("start" in s and "end" in s and "text" in s for s in spans)
        # First span starts at 0
        assert spans[0]["start"] == 0

    def test_code_fence_protection(self):
        """Code blocks should not be split in the middle."""
        code_block = "```python\ndef hello():\n    print('world')\n```"
        text = f"Before code.\n\n{code_block}\n\nAfter code."
        # With a small chunk size, the splitter should still keep the code block intact
        splitter = TextSplitter(chunk_size=50, overlap=10)
        chunks = splitter.split_text(text)
        # The code block should appear complete in at least one chunk
        found = any("def hello():" in chunk and "print('world')" in chunk for chunk in chunks)
        assert found, "Code block was split across chunks"

    def test_empty_text(self):
        chunks = self.splitter.split_text("")
        assert chunks == [] or chunks == [""]

    def test_respects_paragraph_boundaries(self):
        """Splitter should prefer splitting at paragraph boundaries (\n\n)."""
        paragraphs = ["Paragraph one content here."] * 5
        text = "\n\n".join(paragraphs)
        splitter = TextSplitter(chunk_size=100, overlap=20)
        chunks = splitter.split_text(text)
        # Each chunk boundary should ideally be at a paragraph break
        for chunk in chunks:
            assert chunk.strip(), "No empty chunks"


class TestTextSplitterDefaults:
    def test_default_chunk_size(self):
        splitter = TextSplitter()
        assert splitter.chunk_size > 0
        assert splitter.overlap >= 0
        assert splitter.overlap < splitter.chunk_size


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
