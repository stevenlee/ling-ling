"""Markdown-aware text splitter.

Splits long markdown into overlapping chunks while avoiding splits inside
fenced code blocks or table rows. The previous implementation re-scanned all
fence lines from the start of the document on every iteration of the outer
loop — for an N-character document split into K chunks with F fence lines
that was O(K·F). We now precompute fence regions once per call.
"""

from __future__ import annotations

import re
from typing import Optional

from core.config import settings


_FENCE_LINE_RE = re.compile(r'^```.*$', re.MULTILINE)
_HEADER_RE = re.compile(r'\n#{1,3}\s+')
_PARA_BREAK_RE = re.compile(r'\n\n')
_SENTENCE_BREAK_RE = re.compile(r'[\.。!\?！？]\s+|\n')

# How far past the target boundary we'll look for a natural break.
_SAFE_EXIT_WINDOW = 2000


class TextSplitter:
    """Splits long markdown safely along natural boundaries."""

    def __init__(self, chunk_size: int | None = None, overlap: int | None = None):
        self.chunk_size = chunk_size or settings.DIGEST_LIMIT
        self.overlap = overlap or settings.DIGEST_OVERLAP

    # ── Public API ───────────────────────────────────────────────────

    def split_text(self, text: str) -> list[str]:
        return [chunk["text"] for chunk in self.split_text_with_spans(text)]

    def split_text_with_spans(self, text: str) -> list[dict]:
        if not text:
            return []
        if len(text) <= self.chunk_size:
            return [{"text": text, "start": 0, "end": len(text)}]

        fence_regions = self._compute_fence_regions(text)
        chunks: list[dict] = []
        start = 0

        while start < len(text):
            end = self._compute_chunk_end(text, start, fence_regions)
            self._emit_chunk(text, start, end, chunks)

            if end >= len(text):
                break

            next_start = max(end - self.overlap, 0)
            # Guarantee forward progress even when overlap >= chunk_size.
            if next_start <= start:
                next_start = end
            start = next_start

        return chunks

    # ── Chunk boundary search ────────────────────────────────────────

    def _compute_chunk_end(self, text: str, start: int, fence_regions: list[tuple[int, int]]) -> int:
        end = start + self.chunk_size
        if end >= len(text):
            return len(text)

        end = self._snap_to_safe_exit(text, end)
        end = self._extend_past_fence(text, end, fence_regions)
        end = self._extend_past_table_line(text, end)
        return end

    @staticmethod
    def _snap_to_safe_exit(text: str, end: int) -> int:
        """Slide the chunk boundary forward to the nearest natural break.

        Priority: heading > paragraph break > sentence/line break > as-is.
        """
        window = text[end:end + _SAFE_EXIT_WINDOW]

        if m := _HEADER_RE.search(window):
            return end + m.start() + 1  # split before the leading \n
        if m := _PARA_BREAK_RE.search(window):
            return end + m.start() + 2  # split after the \n\n
        if m := _SENTENCE_BREAK_RE.search(window):
            return end + m.end()
        return end

    @staticmethod
    def _extend_past_fence(text: str, end: int, fence_regions: list[tuple[int, int]]) -> int:
        """If `end` lands inside a fenced code block, push it to the block's close."""
        for region_start, region_end in fence_regions:
            if region_start <= end < region_end:
                return region_end
            if region_start >= end:
                break
        return end

    @staticmethod
    def _extend_past_table_line(text: str, end: int) -> int:
        """If `end` lands inside a table line (`|` in the current line), extend to line end."""
        line_start = text.rfind("\n", 0, end) + 1
        current_line = text[line_start:end]
        if "|" not in current_line:
            return end
        next_nl = text.find("\n", end)
        return next_nl + 1 if next_nl != -1 else end

    @staticmethod
    def _emit_chunk(text: str, start: int, end: int, chunks: list[dict]) -> None:
        raw = text[start:end]
        stripped = raw.strip()
        if not stripped:
            return
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw.rstrip())
        chunks.append({"text": stripped, "start": start + leading, "end": start + trailing})

    # ── Fence-region precomputation ──────────────────────────────────

    @staticmethod
    def _compute_fence_regions(text: str) -> list[tuple[int, int]]:
        """Return non-overlapping (open_start, close_line_end) spans for every
        fenced block. Used to detect & skip mid-fence splits in O(log F) per
        chunk instead of O(F) re-scans.
        """
        regions: list[tuple[int, int]] = []
        open_start: int | None = None
        for match in _FENCE_LINE_RE.finditer(text):
            fence_line = match.group(0).strip()
            if open_start is None:
                open_start = match.start()
            elif fence_line == "```":
                # Closing fence — include the trailing newline so the region
                # captures the whole closing line.
                line_end = text.find("\n", match.end())
                close = len(text) if line_end == -1 else line_end + 1
                regions.append((open_start, close))
                open_start = None
            # else: ignore stray opener-style line inside an open fence.

        if open_start is not None:
            # Unterminated fence — guard the rest of the document.
            regions.append((open_start, len(text)))

        return regions

    # ── Back-compat helpers (kept for API stability) ─────────────────

    def _inside_code_block(self, text: str, end: int) -> bool:
        for region_start, region_end in self._compute_fence_regions(text):
            if region_start <= end < region_end:
                return True
            if region_start >= end:
                return False
        return False

    def _next_closing_fence_line_end(self, text: str, start: int) -> Optional[int]:
        for region_start, region_end in self._compute_fence_regions(text):
            if region_start <= start < region_end:
                return region_end
            if region_start >= start:
                break
        return None
