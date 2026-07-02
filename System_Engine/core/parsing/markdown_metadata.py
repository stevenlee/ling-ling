"""Markdown frontmatter + tag metadata (parse/dump).

Moved verbatim out of core/parser.py (P2a of the refactor roadmap).
"""

from __future__ import annotations

import logging
import re

import yaml


# ─── Tag manager (lazy singleton) ──────────────────────────────────────

_tag_manager_instance = None


def _get_tag_manager():
    """Return a cached TagManager instance (lazy import to avoid cycles)."""
    global _tag_manager_instance
    if _tag_manager_instance is None:
        from core.tag_manager import TagManager
        from core.config import TAG_MAP_FILE

        _tag_manager_instance = TagManager(TAG_MAP_FILE)
    return _tag_manager_instance


# ─── Frontmatter ───────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
# Body hashtag: `#word` preceded by SOL or whitespace.  CJK ranges included.
_HASHTAG_RE = re.compile(r"(?:^|\s)#([\w一-鿿]+)")


def parse_markdown_metadata(content: str) -> dict:
    """Extract YAML frontmatter + body hashtags from a markdown string."""
    if not content:
        return {"tags": []}

    tags: set[str] = set()
    metadata: dict = {}
    remaining = content

    fm = _FRONTMATTER_RE.search(content)
    if fm:
        try:
            yaml_data = yaml.safe_load(fm.group(1))
        except Exception as e:
            logging.error(f"Parser: failed to parse YAML frontmatter: {e}")
            yaml_data = None

        if isinstance(yaml_data, dict):
            for key, value in yaml_data.items():
                if key == "tags":
                    if isinstance(value, list):
                        tags.update(str(t).strip() for t in value)
                    elif isinstance(value, str):
                        tags.update(t.strip() for t in value.split(","))
                else:
                    metadata[key] = value
        remaining = content[fm.end() :]

    tags.update(_HASHTAG_RE.findall(remaining))

    tm = _get_tag_manager()
    metadata["tags"] = sorted({tm.normalize(t) for t in tags if t})
    return metadata


def dump_markdown_with_metadata(metadata: dict, content: str) -> str:
    """Serialize metadata + body as a single markdown document."""
    clean: dict = {}
    for k, v in (metadata or {}).items():
        if k == "tags" and isinstance(v, (set, list, tuple)):
            clean[k] = sorted({str(t) for t in v})
        elif isinstance(v, set):
            clean[k] = sorted(v)
        else:
            clean[k] = v

    frontmatter = yaml.safe_dump(clean, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{frontmatter}\n---\n\n{content}"
