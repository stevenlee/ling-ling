"""Source-text pre-passes applied before chunking.

Run on the in-memory document text (never edits the source file). Order in the
pipeline: strip_boilerplate -> flatten_linenumber_tables -> normalize_structure
-> split.

- strip_boilerplate (0c): drop Project Gutenberg license header/footer and a
  Table-of-Contents section, so they don't become low-value Parts. Leading YAML
  frontmatter is preserved.
- flatten_linenumber_tables (0d): PDF→markdown conversion of legislative /
  scanned documents turns per-page line numbering into two-column tables
  (`| 23 | half a sentence |`), which translation models then faithfully
  reproduce as garbage tables (observed live: cloud_act, 671 table rows).
  Flattened back to prose here, before the splitter, so every downstream
  stage sees clean paragraphs.
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
    return (m.group(1), text[m.end() :]) if m else ("", text)


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


# ── 0d: OCR line-number tables → prose ──────────────────────────────

_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_SEP_CELL_RE = re.compile(r"^\s*:?-{2,}:?\s*$")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
# A fragment that starts a new statutory unit — begin a fresh paragraph there
# rather than gluing the whole page into one block. Covers `SEC. 103.`,
# `''§ 2713.`, `(1)` / `(A)` / `(iii)` / `(aa)` enumerators (with or without
# the bill's `''` quote prefix).
_PARA_CUE_RE = re.compile(r"^(?:'')?(?:SEC\.\s|§\s?\d|\([A-Za-z0-9]{1,4}\)\s)")
_WORD_RE = re.compile(r"[A-Za-z]{4,}")


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿"


def _split_row(line: str) -> list[str] | None:
    m = _TABLE_ROW_RE.match(line)
    if not m:
        return None
    return [c.strip() for c in m.group(1).split("|")]


def _is_blank_cell(cell: str) -> bool:
    return cell in ("", "-")


def _is_linenumber_table(rows: list[list[str]], relaxed: bool = False) -> bool:
    """True for a two-column table whose first column is page line numbering:
    ≥80% of its non-blank cells are pure digits and the digit sequence is
    (mostly) non-decreasing. Real data tables — text in the first column, or
    numbers that jump around — are left alone.

    ``relaxed`` (used once the document has at least one strictly-detected
    table, i.e. its PDF-line-number origin is confirmed) also accepts the 1–2
    row stragglers around page breaks, provided the numbers are small enough
    to be page line numbers rather than data (years, amounts)."""
    min_rows = 1 if relaxed else 3
    if len(rows) < min_rows or any(len(r) != 2 for r in rows):
        return False
    firsts = [r[0] for r in rows if not _is_blank_cell(r[0])]
    if not firsts:
        return True  # all-blank number column (`| - | text |`): still line-layout
    digits = [c for c in firsts if c.isdigit()]
    if len(digits) / len(firsts) < 0.8:
        return False
    nums = [int(c) for c in digits]
    if relaxed and len(rows) < 3 and any(n >= 100 for n in nums):
        return False
    if len(nums) >= 2:
        keeps_order = sum(1 for a, b in zip(nums, nums[1:]) if b >= a)
        if keeps_order / (len(nums) - 1) < 0.8:
            return False
    return True


def _iter_table_blocks(lines: list[str]):
    """Yield each run of consecutive table rows as parsed data rows
    (separator rows excluded)."""
    i = 0
    while i < len(lines):
        row = _split_row(lines[i])
        if row is None:
            i += 1
            continue
        rows: list[list[str]] = []
        while i < len(lines):
            row = _split_row(lines[i])
            if row is None:
                break
            if not all(_SEP_CELL_RE.match(c) or c == "" for c in row):
                rows.append(row)
            i += 1
        yield rows


def _smart_join(left: str, frag: str, vocab: set[str]) -> str:
    """Append ``frag`` to ``left``, repairing the two page-wrap artifacts:
    a word split across rows without a hyphen (`com` + `munications` — rejoin
    only when the concatenation is a word seen elsewhere in the document) and
    a hyphen left dangling at a row end (`communications-` + `service`)."""
    if not left:
        return frag
    if _is_cjk(left[-1]) and _is_cjk(frag[0]):
        return left + frag
    if left.endswith("-"):
        return left + frag
    lm = re.search(r"([A-Za-z]+)\Z", left)
    rm = re.match(r"([A-Za-z]+)", frag)
    if lm and rm and (lm.group(1) + rm.group(1)).lower() in vocab:
        return left + frag
    return left + " " + frag


def _fragments_to_prose(frags: list[str], vocab: set[str]) -> str:
    paras: list[str] = []
    cur = ""
    for frag in frags:
        if cur and _PARA_CUE_RE.match(frag):
            paras.append(cur)
            cur = frag
        else:
            cur = _smart_join(cur, frag, vocab)
    if cur:
        paras.append(cur)
    return "\n\n".join(paras)


def _repair_wrap_artifacts(body: str, vocab: set[str]) -> str:
    """Repair PDF line-wrap hyphen artifacts anywhere in the body:
    `ter-rorism` / `po- tential` rejoin when the concatenation is a word seen
    elsewhere in the document; a hyphen + stray space between two words
    (`communications- service`) collapses to a normal compound. Deliberate
    forms — real compounds (`communications-service`) and suspended hyphens
    (`pre- and post-`) — pass through unchanged."""

    def repl(m: re.Match) -> str:
        left, space, right = m.group(1), m.group(2), m.group(3)
        if (left + right).lower() in vocab:
            return left + right
        if space and right.lower() not in ("and", "or"):
            return f"{left}-{right}"
        return m.group(0)

    return re.sub(r"([A-Za-z]+)-([ ]+)?([A-Za-z]+)", repl, body)


def flatten_linenumber_tables(text: str) -> tuple[str, list[str]]:
    """Rewrite OCR line-number tables as prose paragraphs. Frontmatter is
    preserved; genuine data tables are untouched. Consecutive line-number
    tables separated only by blank lines (page breaks) merge into one flow.
    Returns (text, info)."""
    fm, body = _split_frontmatter(text)
    if body.count("|") < 6:
        return fm + body, []

    vocab = {w.lower() for w in _WORD_RE.findall(body)}
    lines = body.split("\n")

    # Pre-scan with the strict rule: only a document that provably contains
    # line-number tables gets the relaxed rule for 1–2 row page-break
    # stragglers — standalone small tables elsewhere stay untouched.
    relaxed = any(_is_linenumber_table(block) for block in _iter_table_blocks(lines))

    out: list[str] = []
    pending: list[str] = []
    tables = 0

    def flush() -> None:
        if pending:
            out.append(_fragments_to_prose(pending, vocab))
            out.append("")
            pending.clear()

    i = 0
    while i < len(lines):
        cells = _split_row(lines[i])
        if cells is not None:
            j = i
            rows: list[list[str]] = []
            while j < len(lines):
                row = _split_row(lines[j])
                if row is None:
                    break
                if not all(_SEP_CELL_RE.match(c) or c == "" for c in row):
                    rows.append(row)
                j += 1
            if _is_linenumber_table(rows, relaxed=relaxed):
                for row in rows:
                    cell = _BR_RE.sub(" ", row[1]).strip()
                    if cell:
                        pending.append(cell)
                tables += 1
                i = j
                continue
            flush()
            out.extend(lines[i:j])
            i = j
            continue
        if not lines[i].strip() and pending:
            i += 1  # page break between two line-number tables — keep merging
            continue
        flush()
        out.append(lines[i])
        i += 1
    flush()
    if not tables:
        return fm + body, []

    # The same PDF conversion that produced the tables also leaves wrap
    # artifacts in the non-table prose (bullet lists etc.) — repair doc-wide,
    # but only for documents confirmed to be of that origin.
    repaired = _repair_wrap_artifacts("\n".join(out), vocab)
    return fm + repaired, [f"flattened {tables} line-number tables"]


def strip_boilerplate(text: str) -> tuple[str, list[str]]:
    """Remove Gutenberg license header/footer and a TOC section from the body.
    Frontmatter is preserved. Returns (cleaned_text, removed_markers)."""
    fm, body = _split_frontmatter(text)
    removed: list[str] = []

    # Keep only what's between the Gutenberg START and END markers.
    s = _GUT_START.search(body)
    if s:
        body = body[s.end() :]
        removed.append("gutenberg_header")
    e = _GUT_END.search(body)
    if e:
        body = body[: e.start()]
        removed.append("gutenberg_footer")

    # Drop a markdown Table-of-Contents section.
    body, n = _TOC_RE.subn("", body)
    if n:
        removed.append(f"toc_section x{n}")

    return fm + body.lstrip("\n"), removed
