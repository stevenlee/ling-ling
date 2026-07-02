"""Block-level Markdown scanner for the Thoughtful Splitter.

Parses markdown into a flat sequence of `Block` objects. We only solve the
block-level structure problem; inline parsing (emphasis, links, etc.) is
not our concern. The output is fed to `ThoughtfulSplitter` Phase 2
(boundary weighting) and Phase 3 (chunking).

Design choices
--------------
* **Line-driven scanner**, not a parser-combinator. Markdown's block-level
  structure is fundamentally line-based; a parser-combinator would over-
  engineer this.
* **Leaf-block coverage**: every character of the input lies in exactly
  one "leaf" block (everything except `LIST`, which is a virtual
  container). `LIST` blocks intentionally overlap with their child
  `LIST_ITEM` blocks — this is the price of giving Phase 2 both "this is
  a list" and "these are the splittable items between which we may cut".
* **Atomic flag**: blocks that must not be split internally. The chunker
  uses this to guard against mid-fence/mid-table/mid-item cuts.

Public API
----------
    scan(text: str) -> list[Block]
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class BlockKind(Enum):
    FRONTMATTER = "frontmatter"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"  # Virtual container, not atomic.
    LIST_ITEM = "list_item"  # Top-level item with its sub-items, atomic.
    CODE_FENCE = "code_fence"
    TABLE = "table"
    BLOCKQUOTE = "blockquote"
    CALLOUT = "callout"  # Obsidian `> [!type]`.
    MATH_BLOCK = "math_block"
    HR = "hr"
    BLANK = "blank"


@dataclass(frozen=True)
class Block:
    kind: BlockKind
    text: str
    start: int  # char offset of first char in source text
    end: int  # char offset one past last char
    level: int = 0  # heading: 1-6; list nesting depth; else 0
    heading_text: str = ""  # heading only: text without # markers
    atomic: bool = False
    parent_kind: BlockKind | None = None


# ─── Regex patterns ────────────────────────────────────────────────────

# CommonMark allows up to 3 leading spaces of indent on most block markers.
_LEAD = r"^( {0,3})"

_HEADING_RE = re.compile(_LEAD + r"(#{1,6})\s+(.+?)\s*#*\s*$")
_HR_DASH_RE = re.compile(_LEAD + r"-{3,}\s*$")
_HR_UNDER_RE = re.compile(_LEAD + r"_{3,}\s*$")
_HR_STAR_RE = re.compile(_LEAD + r"\*{3,}\s*$")
_CODE_FENCE_RE = re.compile(_LEAD + r"(`{3,}|~{3,})(.*)$")
_MATH_OPEN_RE = re.compile(_LEAD + r"\$\$")
_LIST_ITEM_RE = re.compile(r"^( *)([-*+]|\d+[.)])\s")
_BLOCKQUOTE_RE = re.compile(r"^( {0,3})>")
_CALLOUT_RE = re.compile(r"^( {0,3})>\s*\[!\w+\][+-]?")
_SETEXT_H1_RE = re.compile(_LEAD + r"=+\s*$")
_SETEXT_H2_RE = re.compile(_LEAD + r"-+\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


# ─── Public entry point ────────────────────────────────────────────────


def scan(text: str) -> list[Block]:
    """Parse `text` into a flat sequence of `Block` objects."""
    if not text:
        return []
    return _Scanner(text).scan()


# ─── Implementation ────────────────────────────────────────────────────


class _Scanner:
    """Stateful line-driven scanner. One instance per call to scan()."""

    def __init__(self, text: str):
        self.text = text
        # `splitlines(keepends=True)` keeps the trailing `\n` on each line so
        # char offsets line up with the original source.
        self.lines: list[str] = text.splitlines(keepends=True)
        self.n = len(self.lines)
        # `offsets[i]` = char offset of the start of line i; `offsets[n]` = len(text)
        self.offsets: list[int] = self._build_offsets()
        self.i = 0
        self.blocks: list[Block] = []

    def _build_offsets(self) -> list[int]:
        offsets = [0] * (self.n + 1)
        cursor = 0
        for idx, line in enumerate(self.lines):
            offsets[idx] = cursor
            cursor += len(line)
        offsets[self.n] = cursor
        return offsets

    def scan(self) -> list[Block]:
        # Frontmatter only at the very top of the file.
        self._scan_frontmatter()

        while self.i < self.n:
            line = self.lines[self.i]
            stripped = line.rstrip("\n")

            if not stripped.strip():
                self._emit_simple(BlockKind.BLANK, self.i, self.i + 1)
                self.i += 1
                continue

            if self._scan_code_fence(line, stripped):
                continue
            if self._scan_math_block(stripped):
                continue
            if self._scan_heading(line, stripped):
                continue
            if self._scan_setext_or_hr(stripped):
                continue
            if self._scan_callout(stripped):
                continue
            if self._scan_blockquote(stripped):
                continue
            if self._scan_table(stripped):
                continue
            if self._scan_list(line):
                continue
            self._scan_paragraph(stripped)

        return self.blocks

    # ── Frontmatter ────────────────────────────────────────────────────

    def _scan_frontmatter(self) -> None:
        if self.n < 2:
            return
        if self.lines[0].rstrip("\n").strip() != "---":
            return
        for j in range(1, self.n):
            if self.lines[j].rstrip("\n").strip() == "---":
                self._emit_range(BlockKind.FRONTMATTER, 0, j + 1, atomic=True)
                self.i = j + 1
                return
        # Unclosed frontmatter — treat as not-frontmatter, let paragraph path
        # handle the body lines.

    # ── Code fence ─────────────────────────────────────────────────────

    def _scan_code_fence(self, line: str, stripped: str) -> bool:
        m = _CODE_FENCE_RE.match(stripped)
        if not m:
            return False
        fence_marker = m.group(2)
        fence_char = fence_marker[0]
        fence_len = len(fence_marker)

        start_i = self.i
        j = self.i + 1
        while j < self.n:
            jline = self.lines[j].rstrip("\n")
            close_match = re.match(
                rf"^( {{0,3}}){re.escape(fence_char)}{{{fence_len},}}\s*$", jline
            )
            if close_match:
                j += 1
                break
            j += 1
        # If unterminated (j == self.n with no close found), the block
        # extends to EOF — still atomic so the chunker won't try to split
        # inside it. This is the documented behaviour for unclosed fences.
        self._emit_range(BlockKind.CODE_FENCE, start_i, j, atomic=True)
        self.i = j
        return True

    # ── Math block ─────────────────────────────────────────────────────

    def _scan_math_block(self, stripped: str) -> bool:
        if not _MATH_OPEN_RE.match(stripped):
            return False
        start_i = self.i
        j = self.i + 1
        while j < self.n:
            if _MATH_OPEN_RE.match(self.lines[j].rstrip("\n")):
                j += 1
                break
            j += 1
        self._emit_range(BlockKind.MATH_BLOCK, start_i, j, atomic=True)
        self.i = j
        return True

    # ── ATX heading ────────────────────────────────────────────────────

    def _scan_heading(self, line: str, stripped: str) -> bool:
        m = _HEADING_RE.match(stripped)
        if not m:
            return False
        level = len(m.group(2))
        heading_text = m.group(3).strip()
        self.blocks.append(
            Block(
                kind=BlockKind.HEADING,
                text=line,
                start=self.offsets[self.i],
                end=self.offsets[self.i + 1],
                level=level,
                heading_text=heading_text,
            )
        )
        self.i += 1
        return True

    # ── Setext heading / HR ────────────────────────────────────────────

    def _scan_setext_or_hr(self, stripped: str) -> bool:
        """`---` and `===` lines.

        Priority:
          1. If the previous block is a PARAGRAPH that ended at this line's
             start, this is a setext heading underline — promote the
             paragraph.
          2. Otherwise, if the line is `---`/`___`/`***`, it's an HR.
          3. `=` underlines that aren't following a paragraph are emitted as
             paragraphs (no other meaning in markdown).
        """
        is_setext_h1 = bool(_SETEXT_H1_RE.match(stripped))
        is_setext_h2 = bool(_SETEXT_H2_RE.match(stripped))
        is_hr_dash = bool(_HR_DASH_RE.match(stripped))
        is_hr_under = bool(_HR_UNDER_RE.match(stripped))
        is_hr_star = bool(_HR_STAR_RE.match(stripped))

        if not (is_setext_h1 or is_setext_h2 or is_hr_dash or is_hr_under or is_hr_star):
            return False

        # Setext promotion check.
        prev = self.blocks[-1] if self.blocks else None
        adjacent_paragraph = (
            prev is not None
            and prev.kind == BlockKind.PARAGRAPH
            and prev.end == self.offsets[self.i]
        )
        if adjacent_paragraph and (is_setext_h1 or is_setext_h2):
            last = self.blocks.pop()
            level = 1 if is_setext_h1 else 2
            self.blocks.append(
                Block(
                    kind=BlockKind.HEADING,
                    text=last.text + self.lines[self.i],
                    start=last.start,
                    end=self.offsets[self.i + 1],
                    level=level,
                    heading_text=last.text.strip(),
                )
            )
            self.i += 1
            return True

        # Plain HR.
        if is_hr_dash or is_hr_under or is_hr_star:
            self._emit_simple(BlockKind.HR, self.i, self.i + 1)
            self.i += 1
            return True

        # Lone `===` without preceding paragraph — degenerate; emit as paragraph.
        return False

    # ── Callout (Obsidian) ─────────────────────────────────────────────

    def _scan_callout(self, stripped: str) -> bool:
        if not _CALLOUT_RE.match(stripped):
            return False
        start_i = self.i
        j = self.i + 1
        while j < self.n and _BLOCKQUOTE_RE.match(self.lines[j].rstrip("\n")):
            j += 1
        self._emit_range(BlockKind.CALLOUT, start_i, j, atomic=True)
        self.i = j
        return True

    # ── Blockquote ─────────────────────────────────────────────────────

    def _scan_blockquote(self, stripped: str) -> bool:
        if not _BLOCKQUOTE_RE.match(stripped):
            return False
        start_i = self.i
        j = self.i + 1
        while j < self.n and _BLOCKQUOTE_RE.match(self.lines[j].rstrip("\n")):
            j += 1
        self._emit_range(BlockKind.BLOCKQUOTE, start_i, j, atomic=True)
        self.i = j
        return True

    # ── Table ──────────────────────────────────────────────────────────

    def _scan_table(self, stripped: str) -> bool:
        # A table is: header row (contains `|`), then a separator row
        # matching `_TABLE_SEP_RE`, then any number of body rows.
        if "|" not in stripped or self.i + 1 >= self.n:
            return False
        next_stripped = self.lines[self.i + 1].rstrip("\n")
        if not _TABLE_SEP_RE.match(next_stripped):
            return False
        start_i = self.i
        # Header + separator
        j = self.i + 2
        while j < self.n:
            jline = self.lines[j].rstrip("\n")
            if "|" not in jline or not jline.strip():
                break
            j += 1
        self._emit_range(BlockKind.TABLE, start_i, j, atomic=True)
        self.i = j
        return True

    # ── List ───────────────────────────────────────────────────────────

    def _scan_list(self, line: str) -> bool:
        m = _LIST_ITEM_RE.match(line)
        if not m:
            return False
        base_indent = len(m.group(1))

        list_start_i = self.i
        item_starts: list[int] = []
        j = self.i

        while j < self.n:
            jline_full = self.lines[j]
            jline = jline_full.rstrip("\n")

            if not jline.strip():
                # Blank line: list may continue if the next non-blank line is
                # another list item at >= base_indent. Lazy / loose lists.
                k = j + 1
                while k < self.n and not self.lines[k].rstrip("\n").strip():
                    k += 1
                if k >= self.n:
                    break
                next_m = _LIST_ITEM_RE.match(self.lines[k])
                if next_m and len(next_m.group(1)) >= base_indent:
                    j = k
                    continue
                break

            jm = _LIST_ITEM_RE.match(jline_full)
            if jm and len(jm.group(1)) == base_indent:
                item_starts.append(j)
                j += 1
                continue
            if jm and len(jm.group(1)) > base_indent:
                # Sub-item; stays with the current top-level item.
                j += 1
                continue
            # Continuation line (no marker). Counts as belonging to the
            # current item iff it is indented at least one space beyond
            # base_indent (lazy continuation per CommonMark relaxed).
            leading_ws = len(jline) - len(jline.lstrip())
            if leading_ws > base_indent:
                j += 1
                continue
            # Otherwise list ended.
            break

        list_end_i = j

        # Emit virtual LIST container (not atomic — splitting BETWEEN items is OK).
        self._emit_range(BlockKind.LIST, list_start_i, list_end_i, atomic=False)

        # Emit individual LIST_ITEM blocks (atomic).
        if not item_starts:
            # Defensive: a single-item list with no detected markers
            # shouldn't happen, but if it does, fall back to one item.
            item_starts = [list_start_i]

        for idx, item_start in enumerate(item_starts):
            item_end = item_starts[idx + 1] if idx + 1 < len(item_starts) else list_end_i
            self.blocks.append(
                Block(
                    kind=BlockKind.LIST_ITEM,
                    text=self.text[self.offsets[item_start] : self.offsets[item_end]],
                    start=self.offsets[item_start],
                    end=self.offsets[item_end],
                    atomic=True,
                    parent_kind=BlockKind.LIST,
                )
            )

        self.i = list_end_i
        return True

    # ── Paragraph (default) ────────────────────────────────────────────

    def _scan_paragraph(self, _stripped_unused: str) -> None:
        start_i = self.i
        j = self.i + 1
        while j < self.n:
            jline_full = self.lines[j]
            jline = jline_full.rstrip("\n")
            if not jline.strip():
                break
            if (
                _HEADING_RE.match(jline)
                or _CODE_FENCE_RE.match(jline)
                or _MATH_OPEN_RE.match(jline)
                or _LIST_ITEM_RE.match(jline_full)
                or _BLOCKQUOTE_RE.match(jline)
                or _CALLOUT_RE.match(jline)
            ):
                break
            # Table-start lookahead.
            if "|" in jline and j + 1 < self.n:
                nxt = self.lines[j + 1].rstrip("\n")
                if _TABLE_SEP_RE.match(nxt):
                    break
            # Setext / HR markers terminate the paragraph (handled next iteration).
            if (
                _SETEXT_H1_RE.match(jline)
                or _SETEXT_H2_RE.match(jline)
                or _HR_DASH_RE.match(jline)
                or _HR_UNDER_RE.match(jline)
                or _HR_STAR_RE.match(jline)
            ):
                break
            j += 1
        self._emit_range(BlockKind.PARAGRAPH, start_i, j)
        self.i = j

    # ── Emit helpers ───────────────────────────────────────────────────

    def _emit_simple(
        self, kind: BlockKind, start_i: int, end_i: int, *, atomic: bool = False
    ) -> None:
        self.blocks.append(
            Block(
                kind=kind,
                text=self.text[self.offsets[start_i] : self.offsets[end_i]],
                start=self.offsets[start_i],
                end=self.offsets[end_i],
                atomic=atomic,
            )
        )

    def _emit_range(
        self, kind: BlockKind, start_i: int, end_i: int, *, atomic: bool = False
    ) -> None:
        self.blocks.append(
            Block(
                kind=kind,
                text=self.text[self.offsets[start_i] : self.offsets[end_i]],
                start=self.offsets[start_i],
                end=self.offsets[end_i],
                atomic=atomic,
            )
        )


# ─── Convenience helpers (used by tests + Phase 2) ─────────────────────


def leaf_blocks(blocks: list[Block]) -> list[Block]:
    """Drop virtual containers (LIST), keep everything else.

    Phase 2 / coverage checks operate on this filtered view.
    """
    return [b for b in blocks if b.kind != BlockKind.LIST]


def covers_all(text: str, blocks: list[Block]) -> bool:
    """Verify the leaf blocks cover every char of `text` exactly once."""
    leaves = leaf_blocks(blocks)
    if not leaves:
        return not text
    cursor = 0
    for b in leaves:
        if b.start != cursor:
            return False
        cursor = b.end
    return cursor == len(text)
