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

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*', re.DOTALL)
_FRONTMATTER_NL_RE = re.compile(r'^---\s*\n(.*?)\n---\s*(?:\n|$)', re.DOTALL)
_NATURAL_SORT_RE = re.compile(r'([0-9]+)')
_PART_RE = re.compile(r'\(Part \d+\)')


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


def update_wiki_index(filepath: Path = None, title: str = None):
    """Regenerate index.md from a full scan of Notes/, pages/, and raw/consolidate/."""
    try:
        logging.info("Wiki Utils: Regenerating Knowledge Map Index...")

        sections = {
            "Notes":    {"icon": "✍️", "files": _collect_section(NOTES_DIR)},
            "Entities": {"icon": "🤖", "files": _collect_section(PAGES_DIR)},
        }

        # Inject raw/consolidate markdown into Entities, grouped by stem.
        if RAW_CONSOLIDATE_DIR.exists():
            entities = sections["Entities"]["files"]
            for f in RAW_CONSOLIDATE_DIR.glob("*.md"):
                if f.name.startswith(".") or f.name.startswith("_"):
                    continue
                entities.setdefault(f.stem, []).append(_read_metadata(f))

        from core.version import VERSION
        lines = [f"# 🎀 Knowledge Dashboard (v{VERSION})", "---", ""]

        for s_name, s_info in sections.items():
            lines.append(f"## {s_info['icon']} {s_name}")

            for folder in sorted(s_info["files"]):
                files = sorted(s_info["files"][folder], key=lambda x: _natural_sort_key(x["title"]))

                if folder == "Root":
                    for meta in files:
                        date_str = f" | 📅 {meta['date']}" if meta["date"] else ""
                        lines.append(f"- [[{meta['title']}]] {_tag_inline(meta['tags'])}{date_str}")
                else:
                    folder_date = files[0]["date"] if files else ""
                    date_suffix = f" | 📅 {folder_date}" if folder_date else ""
                    lines.append(f"> [!abstract]- 📂 {folder} ({len(files)} items){date_suffix}")

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
