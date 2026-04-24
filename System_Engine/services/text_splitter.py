import re
import logging
from core.config import settings

class TextSplitter:
    def __init__(self, chunk_size: int = None, overlap: int = None):
        self.chunk_size = chunk_size or settings.DIGEST_LIMIT
        self.overlap = overlap or settings.DIGEST_OVERLAP

    def split_text(self, text: str) -> list[str]:
        if len(text) <= self.chunk_size:
            return [text]

        chunks = []
        start = 0
        
        while start < len(text):
            # 1. Basic target end
            end = start + self.chunk_size
            
            if end >= len(text):
                chunks.append(text[start:].strip())
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
            # Check if we are inside a code block
            pre_content = text[:end]
            code_blocks = pre_content.count('```')
            if code_blocks % 2 != 0:
                # Inside a code block! Seek for the closing tag
                closing_tag = text.find('```', end)
                if closing_tag != -1:
                    end = closing_tag + 3
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
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            
            # Move start to end - overlap
            start = end - self.overlap
            if start < 0: start = 0
            
            # Ensure progress
            if end <= (start + self.overlap) and end < len(text):
                start = end

        return chunks
