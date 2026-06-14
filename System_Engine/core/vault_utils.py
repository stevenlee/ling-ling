import logging
import re
from datetime import datetime
from pathlib import Path

import yaml

from core.config import (
    INDEX_FILE,
    NOTES_DIR,
    PAGES_DIR,
    RAW_CONSOLIDATE_DIR,
)

READING_INDEX_FILE = INDEX_FILE.parent / "ReadingIndex.md"
_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*', re.DOTALL)
_FRONTMATTER_NL_RE = re.compile(r'^---\s*\n(.*?)\n---\s*(?:\n|$)', re.DOTALL)
_NATURAL_SORT_RE = re.compile(r'([0-9]+)')
_PART_RE = re.compile(r'\(Part \d+\)')
_READING_INDEX_COLUMNS = (
    "Article",
    "Stat",
    "Re",
    "Im",
    "Comment",
)
_READING_KEY_MAP = {
    "Stat": "status",
    "Re": "relevance",
    "Im": "importance",
    "Comment": "comment",
    "Comments": "comment",
    "Status": "status",
    "Relevance": "relevance",
    "Importance": "importance",
    "Priority": "priority",
    "Progress": "progress",
    "Updated": "updated",
}
_READING_INDEX_INTRO = [
    "# ReadingIndex",
    "",
    "Edit the human-maintained columns. The Article column is regenerated from the vault.",
    "",
    "- Stat: unread, reading, read, parked, skip",
    "- Re (Relevance): 1-5, fit for your current question or project",
    "- Im (Importance): 1-5, long-term value or objective weight",
    "- Comment: short human note for deciding what to read next",
    "",
]


def _natural_sort_key(s):
    s = str(s or "")
    return [int(text) if text.isdigit() else text.lower() for text in _NATURAL_SORT_RE.split(s)]


def _read_metadata(f_path: Path) -> dict:
    """Return {title, tags, date} for a markdown file; tolerant of bad YAML."""
    mtime = datetime.fromtimestamp(f_path.stat().st_mtime).strftime("%Y-%m-%d")
    meta = {"title": f_path.stem, "tags": [], "date": mtime}
    try:
        content = f_path.read_text(encoding="utf-8")
    except Exception as e:
        logging.debug(f"Wiki index: failed to read {f_path.name}: {e}")
        return meta

    fm = _FRONTMATTER_RE.search(content)
    if not fm:
        return meta
    try:
        data = yaml.safe_load(fm.group(1))
    except Exception as e:
        logging.debug(f"Wiki index: failed to parse YAML of {f_path.name}: {e}")
        return meta

    if not isinstance(data, dict):
        return meta

    meta["title"] = str(data.get("title") or f_path.stem)
    tags = data.get("tags", [])
    meta["tags"] = tags if isinstance(tags, list) else [tags]
    yaml_date = str(data.get("date_created") or data.get("created") or data.get("date") or "").strip()
    if yaml_date:
        meta["date"] = yaml_date
    return meta


def _collect_section(base_dir: Path) -> dict[str, list[dict]]:
    """Walk a section directory and group markdown metadata by top-level folder."""
    files: dict[str, list[dict]] = {}
    if not base_dir.exists():
        return files
    for f in base_dir.rglob("*.md"):
        if f.name.startswith(".") or f.name.startswith("_"):
            continue
        rel_parts = f.relative_to(base_dir).parts
        folder = "Root" if len(rel_parts) == 1 else rel_parts[0]
        files.setdefault(folder, []).append(_read_metadata(f))
    return files


def _file_icon(title: str) -> str:
    if "(Synthesis)" in title:
        return "🌟"
    if "(Stitched)" in title:
        return "🧵"
    return "📄"


def _tag_inline(tags: list) -> str:
    if not tags:
        return ""
    return f" `{'` `'.join(tags[:3])}`"


def _split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]

    cells = []
    current = []
    idx = 0
    while idx < len(line):
        char = line[idx]
        next_char = line[idx + 1] if idx + 1 < len(line) else ""
        if char == "\\" and next_char in ("|", "\\"):
            current.append(next_char)
            idx += 2
            continue
        if char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        idx += 1
    cells.append("".join(current).strip())
    return cells


def _table_cell(value) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _title_from_article_cell(value: str) -> str:
    value = str(value or "").strip()
    match = re.search(r"\[\[(.*?)\]\]", value)
    if match:
        value = match.group(1)
    title = value.split("|", 1)[0].strip()
    if title.endswith(" (Synthesis)"):
        title = title[:-12]
    return title


def _load_reading_index() -> tuple[dict, bool, bool]:
    """Load human reading annotations.

    Returns (annotations, parsed_ok, schema_matched). If a user-edited ReadingIndex exists but
    cannot be parsed, callers must avoid rewriting it. schema_matched indicates if the table
    headers on disk match current columns exactly.
    """
    if not READING_INDEX_FILE.exists():
        return {}, True, True
    try:
        content = READING_INDEX_FILE.read_text(encoding="utf-8")
    except Exception as e:
        logging.debug(f"Wiki index: failed to read reading index: {e}")
        return {}, False, False

    if content.strip() == "":
        return {}, True, False

    lines = content.splitlines()
    for idx, line in enumerate(lines):
        headers = _split_table_row(line)
        if headers and headers[0] == "Article":
            allowed_schemas = [
                ["Article", "Stat", "Re", "Im", "Comment"],
                ["Article", "Stat", "Re", "Im", "Comments"],
                ["Article", "Stat", "Im", "Re", "Comment"],
                ["Article", "Stat", "Im", "Re", "Comments"],
                ["Article", "Status", "Re", "Im", "Comment"],
                ["Article", "Status", "Re", "Im", "Comments"],
                ["Article", "Status", "Im", "Re", "Comment"],
                ["Article", "Status", "Im", "Re", "Comments"],
                ["Article", "Status", "Priority", "Importance", "Relevance", "Progress", "Comment", "Updated"],
                ["Article", "Status", "Priority", "Importance", "Relevance", "Progress", "Comments", "Updated"],
            ]
            if headers not in allowed_schemas:
                return {}, False, False
            schema_matched = (headers == list(_READING_INDEX_COLUMNS))
            if idx + 1 >= len(lines):
                return {}, False, False
            separator = _split_table_row(lines[idx + 1])
            if len(separator) != len(headers):
                return {}, False, False
            annotations = {}
            for row in lines[idx + 2:]:
                if not row.strip().startswith("|"):
                    break
                cells = _split_table_row(row)
                if len(cells) < len(headers):
                    cells.extend([""] * (len(headers) - len(cells)))
                row_data = dict(zip(headers, cells))
                title = _title_from_article_cell(row_data.get("Article", ""))
                if not title:
                    continue
                annotation = {}
                for header, key in _READING_KEY_MAP.items():
                    value = row_data.get(header, "").strip()
                    if value:
                        annotation[key] = value
                annotations[title] = annotation
            return annotations, True, schema_matched
    return {}, False, False


def _sync_reading_index(article_titles: list[str]):
    """Keep article rows current while preserving human-maintained columns."""
    existing, parsed_ok, schema_matched = _load_reading_index()
    if not parsed_ok:
        logging.warning("Wiki index: ReadingIndex.md could not be parsed; skipping automatic sync.")
        return

    all_titles = set(article_titles) | {title for title, annotation in existing.items() if annotation}
    sorted_titles = sorted(all_titles, key=_natural_sort_key)

    lines = _READING_INDEX_INTRO + [
        "| " + " | ".join(_READING_INDEX_COLUMNS) + " |",
        "| " + " | ".join(["---"] * len(_READING_INDEX_COLUMNS)) + " |",
    ]
    for title in sorted_titles:
        annotation = existing.get(title, {})
        cells = [f"[[{_table_cell(title)} (Synthesis)\\|{_table_cell(title)}]]"]
        for header in _READING_INDEX_COLUMNS[1:]:
            cells.append(_table_cell(annotation.get(_READING_KEY_MAP[header], "")))
        lines.append("| " + " | ".join(cells) + " |")

    content = "\n".join(lines) + "\n"
    if READING_INDEX_FILE.exists():
        try:
            if READING_INDEX_FILE.read_text(encoding="utf-8") == content:
                return
        except Exception:
            pass
    READING_INDEX_FILE.write_text(content, encoding="utf-8")


def _annotation_for(annotations: dict, title: str) -> dict:
    value = annotations.get(title)
    return value if isinstance(value, dict) else {}


def _annotation_inline(annotation: dict) -> str:
    parts = []
    status = str(annotation.get("status") or "").strip()
    if status:
        parts.append(f"🔖 {status}")

    scores = []
    for field, label in (
        ("priority", "P"),
        ("importance", "I"),
        ("relevance", "R"),
    ):
        value = annotation.get(field)
        if value not in (None, ""):
            scores.append(f"{label}{value}")
    if scores:
        parts.append("⭐ " + " ".join(scores))

    progress = str(annotation.get("progress") or "").strip()
    if progress:
        parts.append(f"📍 {progress}")

    return f" | {' | '.join(parts)}" if parts else ""


def _status_label(annotation: dict) -> str:
    status = str(annotation.get("status") or "").strip()
    return status.capitalize() if status else ""


def _importance_relevance_label(annotation: dict) -> str:
    scores = []
    for field, label in (("importance", "I"), ("relevance", "R")):
        value = annotation.get(field)
        if value not in (None, ""):
            scores.append(f"{label}{value}")
    return " ".join(scores)


def _folder_header(date: str, annotation: dict) -> str:
    parts = []
    if date:
        parts.append(f"📅 {date}")
    status = _status_label(annotation)
    if status:
        parts.append(status)
    scores = _importance_relevance_label(annotation)
    if scores:
        parts.append(scores)
    return " | ".join(parts) or "No reading metadata"


def _append_annotation_lines(lines: list[str], annotation: dict, prefix: str = ""):
    progress = str(annotation.get("progress") or "").strip()
    if progress:
        lines.append(f"{prefix}- 📍 {progress}")

    comment = str(annotation.get("comment") or "").strip()
    if comment:
        lines.append(f"{prefix}- 💬 {comment}")


def update_wiki_index(filepath: Path = None, title: str = None, *, sync_reading_index: bool = False):
    """Regenerate index.md from a full scan of Notes/, pages/, and raw/consolidate/."""
    try:
        logging.info("Wiki Utils: Regenerating Knowledge Map Index...")
        page_entities = _collect_section(PAGES_DIR)
        sections = {
            "Notes":    {"icon": "✍️", "files": _collect_section(NOTES_DIR)},
            "Entities": {"icon": "🌷", "files": {folder: list(files) for folder, files in page_entities.items()}},
        }

        # Inject raw/consolidate markdown into Entities, grouped by stem.
        if RAW_CONSOLIDATE_DIR.exists():
            entities = sections["Entities"]["files"]
            for f in RAW_CONSOLIDATE_DIR.glob("*.md"):
                if f.name.startswith(".") or f.name.startswith("_"):
                    continue
                entities.setdefault(f.stem, []).append(_read_metadata(f))

        entity_titles = [folder for folder in page_entities if folder != "Root"]
        if sync_reading_index:
            _sync_reading_index(entity_titles)
        reading_index, _, _ = _load_reading_index()

        from core.version import BUILD_DATE
        lines = [
            "# 🎀 Knowledge Dashboard",
            f"*📅 Last updated: {BUILD_DATE}*",
            "---",
            "",
            "- ✍️ [[ReadingIndex]]",
            "",
        ]

        for s_name, s_info in sections.items():
            if s_name == "Notes" and not s_info["files"]:
                continue
            lines.append(f"## {s_info['icon']} {s_name}")

            for folder in sorted(s_info["files"]):
                files = sorted(s_info["files"][folder], key=lambda x: _natural_sort_key(x["title"]))

                if folder == "Root":
                    for meta in files:
                        annotation = _annotation_for(reading_index, meta["title"])
                        date_str = f" | 📅 {meta['date']}" if meta["date"] else ""
                        lines.append(
                            f"- [[{meta['title']}]] {_tag_inline(meta['tags'])}"
                            f"{date_str}{_annotation_inline(annotation)}"
                        )
                        _append_annotation_lines(lines, annotation, prefix="  ")
                else:
                    annotation = _annotation_for(reading_index, folder)
                    folder_date = files[0]["date"] if files else ""
                    lines.append(
                        f"> [!abstract]- {_folder_header(folder_date, annotation)}"
                        f"<br>**📂 {folder} ({len(files)} items)**"
                    )
                    _append_annotation_lines(lines, annotation, prefix="> ")

                    parts = [f for f in files if _PART_RE.search(f["title"])]
                    mains = [f for f in files if not _PART_RE.search(f["title"])]

                    for meta in mains:
                        lines.append(f"> - {_file_icon(meta['title'])} [[{meta['title']}]]{_tag_inline(meta['tags'])}")

                    if parts:
                        lines.append(f"> - 🧩 *(... plus {len(parts)} raw chunks hidden)*")
                lines.append("")
            lines.append("")

        INDEX_FILE.write_text("\n".join(lines), encoding="utf-8")
        logging.info("Wiki Utils: index.md has been successfully updated.")

    except Exception as e:
        logging.error(f"Wiki Utils: failed to regenerate index.md: {e}")


def ensure_wiki_indexes():
    """Ensure ReadingIndex.md and index.md exist and reflect the current vault."""
    update_wiki_index(sync_reading_index=True)


def update_file_tags(filepath: Path, tags: list[str]):
    """Replace the `tags:` field of `filepath`'s YAML frontmatter."""
    content = filepath.read_text(encoding="utf-8")
    fm = _FRONTMATTER_NL_RE.search(content)

    if not fm:
        tag_str = ", ".join(tags)
        filepath.write_text(f"---\ntags: [{tag_str}]\n---\n\n" + content, encoding="utf-8")
        return

    try:
        data = yaml.safe_load(fm.group(1))
    except Exception as e:
        raise ValueError(f"YAML update failed: {e}") from e

    if not isinstance(data, dict):
        data = {}
    data["tags"] = tags

    new_fm = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
    body = content[fm.end():]
    filepath.write_text(f"---\n{new_fm}\n---\n{body}", encoding="utf-8")


def find_note(title: str) -> Path | None:
    """Find a note by stem in Pages/ or Notes/."""
    for directory in (PAGES_DIR, NOTES_DIR):
        if not directory.exists():
            continue
        p = next(directory.rglob(f"{title}.md"), None)
        if p:
            return p
    return None


def get_note_content(title_or_path) -> str:
    """Retrieve a note's contents by title (stem) or Path."""
    path = find_note(title_or_path) if isinstance(title_or_path, str) else title_or_path
    if path and path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def update_note_with_meta(filepath: Path, body: str, meta: dict):
    """Write a note, merging supplied meta over any existing frontmatter."""
    from core.parser import dump_markdown_with_metadata, parse_markdown_metadata

    existing_meta = {}
    if filepath.exists():
        existing_meta = parse_markdown_metadata(filepath.read_text(encoding="utf-8"))

    existing_meta.update(meta)
    filepath.write_text(dump_markdown_with_metadata(existing_meta, body), encoding="utf-8")
