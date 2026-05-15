import re
import logging
from typing import Optional
from core.config import settings

class TextSplitter:
    FENCE_LINE_RE = re.compile(r'^```.*$', re.MULTILINE)

    def __init__(self, chunk_size: int = None, overlap: int = None):
        self.chunk_size = chunk_size or settings.DIGEST_LIMIT
        self.overlap = overlap or settings.DIGEST_OVERLAP

    def _inside_code_block(self, text: str, end: int) -> bool:
        inside = False
        for match in self.FENCE_LINE_RE.finditer(text, 0, end):
            fence_line = match.group(0).strip()
            if inside:
                if fence_line == '```':
                    inside = False
            else:
                inside = True
        return inside

    def _next_closing_fence_line_end(self, text: str, start: int) -> Optional[int]:
        for match in self.FENCE_LINE_RE.finditer(text, start):
            if match.group(0).strip() != '```':
                continue
            line_end = text.find('\n', match.end())
            return len(text) if line_end == -1 else line_end + 1
        return None

    def split_text_with_spans(self, text: str) -> list[dict]:
        if len(text) <= self.chunk_size:
            return [{"text": text, "start": 0, "end": len(text)}]

        chunks = []
        start = 0
        
        while start < len(text):
            # 1. Basic target end
            end = start + self.chunk_size
            
            if end >= len(text):
                raw_chunk = text[start:]
                stripped = raw_chunk.strip()
                if stripped:
                    leading = len(raw_chunk) - len(raw_chunk.lstrip())
                    trailing = len(raw_chunk.rstrip())
                    chunks.append({"text": stripped, "start": start + leading, "end": start + trailing})
                break

            # 2. Search for "Safe Exit" after the chunk_size
            # We look ahead up to 2000 chars for a natural break
            search_window = text[end:end + 2000]
            
            # Priority 1: Headers (# or ## at start of line)
            header_match = re.search(r'\n#{1,3}\s+', search_window)
            if header_match:
                end = end + header_match.start() + 1 # Split before the \n
            else:
                # Priority 2: Paragraph break (\n\n)
                para_match = re.search(r'\n\n', search_window)
                if para_match:
                    end = end + para_match.start() + 2 # Split after \n\n
                else:
                    # Priority 3: Sentence or Line break
                    line_match = re.search(r'[\.。!\?！？]\s+|\n', search_window)
                    if line_match:
                        end = end + line_match.end()
                    else:
                        # Fallback: Just cut at chunk_size
                        pass

            # 3. Syntax Protection (Inhibitors)
            # Check if we are inside a fenced code block. Fence detection is
            # line-aware so ```mermaid is never split into ``` + mermaid.
            if self._inside_code_block(text, end):
                fence_line_end = self._next_closing_fence_line_end(text, end)
                if fence_line_end is not None:
                    end = fence_line_end
                else:
                    # No closing tag? Force split and we will repair it in the next step
                    pass
            
            # Check if we are inside a table line
            current_line_start = text.rfind('\n', 0, end) + 1
            current_line = text[current_line_start:end]
            if '|' in current_line:
                # Might be a table. Seek for end of line
                next_line_end = text.find('\n', end)
                if next_line_end != -1:
                    end = next_line_end + 1

            # Final Chunk
            raw_chunk = text[start:end]
            chunk = raw_chunk.strip()
            if chunk:
                leading = len(raw_chunk) - len(raw_chunk.lstrip())
                trailing = len(raw_chunk.rstrip())
                chunks.append({"text": chunk, "start": start + leading, "end": start + trailing})
            
            # Move start to end - overlap
            start = end - self.overlap
            if start < 0: start = 0
            
            # Ensure progress
            if end <= (start + self.overlap) and end < len(text):
                start = end

        return chunks

    def split_text(self, text: str) -> list[str]:
        return [chunk["text"] for chunk in self.split_text_with_spans(text)]
