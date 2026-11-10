"""Section-addressed Q&A: map "§2.1 of <document>" to the Part pages that hold it.

Long documents are ingested as Part pages. Similarity retrieval ignores the
document's structure, so a question naming a section (observed live: "§2.1"
of a paper) retrieved the appendix instead, and the user had to list the
Parts by hand. This module does that lookup deterministically:

1. ``parse_section_refs`` pulls section labels out of the question. Questions
   without an explicit section marker return nothing, so ordinary questions
   never touch the filesystem.
2. ``locate_section_parts`` picks the document (an explicit ``[[link]]`` first,
   otherwise a document title written in the question) and returns the Parts
   that hold the section.

Each Part records the raw-source character span it was cut from
(``source_start_char``/``source_end_char``), and the archived raw source keeps
the original Markdown headings. A section's span runs from its heading to the
next heading of the same or higher level, and the Parts overlapping that span
are exactly the Parts holding it — including a short section that starts and
ends inside one Part. When the raw source or the spans are unavailable, fall
back to each Part's ``section_path``; that path is the heading path at the
Part's END boundary, so it can miss a section shorter than a Part.

Read-only: it reads pages and raw sources and never writes the vault.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from core.config import PAGES_DIR, RAW_CONSOLIDATE_DIR, settings
from core.parser import parse_markdown_metadata

# A section label is a number path ("2", "2.1", "3.2.1") or an appendix
# letter with an optional number path ("a", "a.1").
_NUM = r"\d+(?:\.\d+)*"
_APPX = r"[A-Za-z](?:\.\d+)*"

# Each pattern must carry an explicit section marker; a bare "2.1" in a
# question is a version, a ratio or a value far more often than a section.
_SECTION_REF_PATTERNS = (
    re.compile(rf"§\s*({_NUM}|{_APPX})(?![\w.])"),
    re.compile(rf"\b(?:section|sec\.)\s*({_NUM})(?![\w.])", re.IGNORECASE),
    re.compile(rf"第\s*({_NUM})\s*[節节章]"),
    re.compile(rf"\bappendix\s+({_APPX})(?![\w.])", re.IGNORECASE),
    re.compile(rf"附錄\s*({_APPX})(?![\w.])"),
)

# The label a heading starts with: "2.1  Title", "Appendix A Proofs",
# "A.1  Notation", "Section 3 ...", "第 2 章 ...". A bare letter only counts
# with a number path ("A.1") or an appendix prefix, so a heading like
# "A Framework for ..." is not mistaken for appendix A.
_HEADING_LABEL_RE = re.compile(
    rf"^\s*(?:"
    rf"(?:appendix|附錄)\s*(?P<appx>{_APPX})"
    rf"|(?:section|sec\.|chapter|第)?\s*(?P<num>{_NUM})"
    rf"|(?P<lettered>[A-Za-z]\.\d+(?:\.\d+)*)"
    rf")(?=$|[\s.:：、)）章節节])",
    re.IGNORECASE,
)

_MD_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_WIKILINK_TARGET_RE = re.compile(r"\[\[([^\]|#]+)")
_PART_SUFFIX_RE = re.compile(r"\s*\((?:Part \d+|Synthesis|Stitched)\)\s*$")
_PART_NUM_RE = re.compile(r" \(Part (\d+)\)$")
_MIN_TITLE_KEY_CHARS = 12  # shorter titles are too likely to match by accident


@dataclass(frozen=True)
class SectionPart:
    title: str
    path: Path
    part: int
    label: str  # the requested label this Part matched, e.g. "2.1"
    heading: str  # the heading text that matched


def _norm_label(label: str) -> str:
    return label.strip().rstrip(".").lower()


def parse_section_refs(question: str) -> list[str]:
    """Section labels named in the question, normalized and de-duplicated."""
    labels: list[str] = []
    for pattern in _SECTION_REF_PATTERNS:
        for match in pattern.finditer(question or ""):
            label = _norm_label(match.group(1))
            if label and label not in labels:
                labels.append(label)
    return labels


def heading_label(heading: str) -> str | None:
    """The section label a heading starts with, or None for unlabeled headings."""
    match = _HEADING_LABEL_RE.match(str(heading or ""))
    if not match:
        return None
    label = match.group("appx") or match.group("num") or match.group("lettered")
    return _norm_label(label) if label else None


def _label_covers(requested: str, candidate: str) -> bool:
    """'2.1' covers '2.1' and '2.1.3'; '2' covers '2.4' but not '20'."""
    return candidate == requested or candidate.startswith(requested + ".")


def _title_key(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _document_dirs(question: str, explicit_titles: list[str], pages_dir: Path) -> list[Path]:
    """Document folders the question refers to: explicit links first, then
    the longest folder title written in the question."""
    candidates: list[str] = []
    for raw in list(explicit_titles) + _WIKILINK_TARGET_RE.findall(question or ""):
        base = _PART_SUFFIX_RE.sub("", str(raw).split("|")[0]).strip()
        if base and base not in candidates:
            candidates.append(base)
    dirs = [pages_dir / name for name in candidates if (pages_dir / name).is_dir()]
    if dirs:
        return dirs

    key = _title_key(question)
    best: Path | None = None
    best_len = 0
    try:
        folders = [p for p in pages_dir.iterdir() if p.is_dir()]
    except OSError:
        return []
    for folder in folders:
        folder_key = _title_key(folder.name)
        if len(folder_key) >= _MIN_TITLE_KEY_CHARS and folder_key in key:
            if len(folder_key) > best_len:
                best, best_len = folder, len(folder_key)
    return [best] if best else []


def _section_spans(raw_text: str, labels: list[str]) -> list[tuple[int, int, str, str]]:
    """(start, end, requested label, heading) for each raw heading a label
    covers; a span ends at the next heading of the same or higher level."""
    headings: list[tuple[int, int, str]] = []  # (char offset, level, text)
    offset = 0
    in_fence = False
    for line in raw_text.splitlines(keepends=True):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            match = _MD_HEADING_RE.match(line.rstrip("\r\n"))
            if match:
                headings.append((offset, len(match.group(1)), match.group(2).strip()))
        offset += len(line)

    spans: list[tuple[int, int, str, str]] = []
    for i, (start, level, text) in enumerate(headings):
        candidate = heading_label(text)
        if candidate is None:
            continue
        hit = next((lab for lab in labels if _label_covers(lab, candidate)), None)
        if not hit:
            continue
        end = next((h[0] for h in headings[i + 1 :] if h[1] <= level), len(raw_text))
        spans.append((start, end, hit, text))
    return spans


def _part_pages(folder: Path) -> list[tuple[int, Path, dict]]:
    parts: list[tuple[int, Path, dict]] = []
    for md in folder.glob("*.md"):
        part_match = _PART_NUM_RE.search(md.stem)
        if not part_match:
            continue
        try:
            meta = parse_markdown_metadata(md.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        parts.append((int(part_match.group(1)), md, meta if isinstance(meta, dict) else {}))
    parts.sort(key=lambda p: p[0])
    return parts


def _char_span(meta: dict) -> tuple[int, int] | None:
    try:
        start, end = int(meta["source_start_char"]), int(meta["source_end_char"])
    except (KeyError, TypeError, ValueError):
        return None
    return (start, end) if end > start >= 0 else None


def _locate_by_raw_spans(
    folder: Path, parts: list[tuple[int, Path, dict]], labels: list[str], raw_dir: Path
) -> list[SectionPart] | None:
    """Parts overlapping the raw section spans; None when this strategy cannot
    judge (no raw source, missing spans, or no labeled raw heading)."""
    raw_path = raw_dir / f"{folder.name}.md"
    try:
        raw_text = raw_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    spans = _section_spans(raw_text, labels)
    if not spans:
        return None
    part_spans = [(n, md, _char_span(meta)) for n, md, meta in parts]
    if any(span is None for _, _, span in part_spans):
        return None

    found: list[SectionPart] = []
    for n, md, span in part_spans:
        assert span is not None  # guarded above; narrows the type for mypy
        p_start, p_end = span
        for s_start, s_end, label, heading in spans:
            if p_start < s_end and p_end > s_start:
                found.append(SectionPart(md.stem, md, n, label, heading))
                break
    return found


def _locate_by_section_path(
    parts: list[tuple[int, Path, dict]], labels: list[str]
) -> list[SectionPart]:
    found: list[SectionPart] = []
    for n, md, meta in parts:
        path = meta.get("section_path")
        for heading in path if isinstance(path, list) else []:
            candidate = heading_label(str(heading))
            if candidate is None:
                continue
            hit = next((lab for lab in labels if _label_covers(lab, candidate)), None)
            if hit:
                found.append(SectionPart(md.stem, md, n, hit, str(heading).strip()))
                break
    return found


def locate_section_parts(
    question: str,
    explicit_titles: list[str] | None = None,
    *,
    pages_dir: Path | None = None,
    raw_dir: Path | None = None,
    max_parts: int | None = None,
) -> tuple[list[SectionPart], int]:
    """Parts holding the sections the question names.

    Returns ``(parts, total_matched)``: at most ``max_parts`` Parts in reading
    order, plus how many matched before the cap so the caller can say the
    context is partial. ``([], 0)`` when the question names no section, no
    document can be identified, or no Part carries that section.
    """
    labels = parse_section_refs(question)
    if not labels:
        return [], 0
    pages_dir = pages_dir or PAGES_DIR
    raw_dir = raw_dir or RAW_CONSOLIDATE_DIR
    # Read per call: the cap is a hot-reloadable Scripture setting.
    configured = settings.SECTION_LOCATE_MAX_PARTS if max_parts is None else max_parts
    cap = max(int(configured), 0)

    matched: list[SectionPart] = []
    for folder in _document_dirs(question, explicit_titles or [], pages_dir):
        parts = _part_pages(folder)
        if not parts:
            continue
        by_raw = _locate_by_raw_spans(folder, parts, labels, raw_dir)
        matched.extend(by_raw if by_raw is not None else _locate_by_section_path(parts, labels))
    if matched:
        logging.info(
            "Section locate: %s → %s (%d matched, cap %d)",
            ", ".join(labels),
            ", ".join(p.title for p in matched[:cap]),
            len(matched),
            cap,
        )
    return matched[:cap], len(matched)
