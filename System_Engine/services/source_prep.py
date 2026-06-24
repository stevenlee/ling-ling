"""Source-text pre-passes applied before chunking.

Run on the in-memory document text (never edits the source file). Order in the
pipeline: strip_boilerplate -> normalize_structure -> split.

- strip_boilerplate (0c): drop Project Gutenberg license header/footer and a
  Table-of-Contents section, so they don't become low-value Parts. Leading YAML
  frontmatter is preserved.
- normalize_structure (0b): added later — promote plain-text chapter cues to
  markdown headings when a doc lacks them.
"""

from __future__ import annotations

import re

# Leading YAML frontmatter (the web clipper's title/source/tags block).
_FRONTMATTER_RE = re.compile(r"\A(---\n.*?\n---\n)", re.DOTALL)

# Gutenberg markers. The markdown converter escapes the asterisks (`\*\*\*`),
# so tolerate any mix of backslashes / asterisks / spaces before the keyword.
_GUT_START = re.compile(
    r"^[\\*\s]*START OF TH(?:E|IS) PROJECT GUTENBERG EBOOK\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_GUT_END = re.compile(
    r"^[\\*\s]*END OF TH(?:E|IS) PROJECT GUTENBERG EBOOK\b.*$",
    re.IGNORECASE | re.MULTILINE,
)

# A markdown Table-of-Contents heading and its body, up to the next heading.
_TOC_RE = re.compile(
    r"^#{1,6}[ \t]*(?:Table of Contents|Contents|目錄|目次)[ \t]*$.*?(?=^#{1,6}[ \t]|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def _split_frontmatter(text: str) -> tuple[str, str]:
    m = _FRONTMATTER_RE.match(text)
    return (m.group(1), text[m.end():]) if m else ("", text)


_MD_HEADING = re.compile(r"^#{1,6}\s", re.MULTILINE)
# Explicit chapter cues (uppercase keyword + number/roman) — high confidence.
_CHAPTER_CUE = re.compile(r"(?:CHAPTER|PART|SECTION|BOOK)\s+(?:[IVXLCDM]+|\d+)\b")
# Chinese chapter markers.
_CN_CHAPTER = re.compile(r"第[一二三四五六七八九十百千零〇\d]+[章回品節篇卷]")
# An ALL-CAPS standalone header: letters/spaces/&'- only (no digits, no comma),
# 4-60 chars — catches "RISK FACTORS", "BUSINESS"; rejects "FORM S-1", "WASHINGTON, DC 20549".
_ALLCAPS = re.compile(r"[A-Z][A-Z &'\-]{3,59}")
_TOC_WORDS = re.compile(r"(?:TABLE OF CONTENTS|CONTENTS|目錄|目次)\Z", re.IGNORECASE)


def _is_heading_cue(stripped: str, prev_blank: bool, next_blank: bool) -> bool:
    if _TOC_WORDS.match(stripped):
        return False
    if _CHAPTER_CUE.match(stripped) or _CN_CHAPTER.match(stripped):
        return True
    # ALL-CAPS line must be isolated (blank on at least one side) to avoid
    # promoting mid-paragraph emphasis.
    if (prev_blank or next_blank) and _ALLCAPS.fullmatch(stripped):
        return True
    return False


def normalize_structure(text: str) -> tuple[str, list[str]]:
    """Promote plain-text chapter cues to markdown headings — but ONLY for
    documents that lack markdown structure (the structure-aware splitter handles
    the rest). Frontmatter preserved. Returns (text, info)."""
    fm, body = _split_frontmatter(text)
    if len(_MD_HEADING.findall(body)) >= 3:
        return fm + body, []  # already structured — leave it alone

    lines = body.split("\n")
    promoted = 0
    for i, line in enumerate(lines):
        if line.startswith("#"):
            continue
        s = line.strip()
        if not s or s != line:  # require the line be exactly its stripped form (no indent)
            continue
        prev_blank = i == 0 or lines[i - 1].strip() == ""
        next_blank = i == len(lines) - 1 or lines[i + 1].strip() == ""
        if _is_heading_cue(s, prev_blank, next_blank):
            lines[i] = "## " + s
            promoted += 1
    info = [f"promoted {promoted} headings"] if promoted else []
    return fm + "\n".join(lines), info


def strip_boilerplate(text: str) -> tuple[str, list[str]]:
    """Remove Gutenberg license header/footer and a TOC section from the body.
    Frontmatter is preserved. Returns (cleaned_text, removed_markers)."""
    fm, body = _split_frontmatter(text)
    removed: list[str] = []

    # Keep only what's between the Gutenberg START and END markers.
    s = _GUT_START.search(body)
    if s:
        body = body[s.end():]
        removed.append("gutenberg_header")
    e = _GUT_END.search(body)
    if e:
        body = body[:e.start()]
        removed.append("gutenberg_footer")

    # Drop a markdown Table-of-Contents section.
    body, n = _TOC_RE.subn("", body)
    if n:
        removed.append(f"toc_section x{n}")

    return fm + body.lstrip("\n"), removed
