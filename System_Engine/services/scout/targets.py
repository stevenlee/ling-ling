"""Load and validate the Scout targets file (Scripture/Scout.md).

Targets live in a Markdown TABLE in the body (header must include `url`;
`parser` / `cadence` / `max_items` columns optional, empty cell = default) —
Obsidian's properties UI can't edit nested frontmatter lists, a table it can.
The legacy frontmatter `targets:` list still parses (backward compat); both
sources are merged. `language` stays in frontmatter (a scalar is fine there).

The file is user-edited, so validation is forgiving: a broken row/entry is
logged and skipped, never fatal — the rest of the list still runs.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from core.parsing.markdown_metadata import parse_markdown_metadata
from services.scout.models import ScoutTarget

_VALID_CADENCES = ("daily", "weekly")
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*(?:\n|$)", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_TABLE_COLUMNS = ("url", "parser", "cadence", "max_items")


def load_targets(path: Path) -> tuple[list[ScoutTarget], str]:
    """Parse the targets file → (targets, language).

    ``language`` is the report language; "" means follow OUTPUT_LANGUAGE.
    Missing file / empty list → ([], "") so the caller can report "no targets"
    instead of crashing.
    """
    if not path.exists():
        logging.warning(f"Scout: targets file missing: {path}")
        return [], ""

    try:
        content = path.read_text(encoding="utf-8")
        metadata = parse_markdown_metadata(content)
    except Exception as e:
        logging.error(f"Scout: failed to parse targets file: {e}")
        return [], ""

    language = str(metadata.get("language") or "").strip()

    entries: list[dict] = []
    raw_targets = metadata.get("targets")  # legacy frontmatter list
    if isinstance(raw_targets, list):
        entries.extend(e for e in raw_targets if isinstance(e, dict))
    entries.extend(_table_entries(_FRONTMATTER_RE.sub("", content, count=1)))

    if not entries:
        logging.warning("Scout: no targets table (or frontmatter list) found.")
        return [], language

    targets: list[ScoutTarget] = []
    for i, entry in enumerate(entries):
        target = _parse_entry(entry, index=i)
        if target is not None:
            targets.append(target)
    return targets, language


def _table_entries(body: str) -> list[dict]:
    """Rows of the first Markdown table whose header includes `url`."""
    lines = [line.strip() for line in body.splitlines()]
    for start, line in enumerate(lines):
        if not (line.startswith("|") and line.endswith("|")):
            continue
        headers = [cell.strip().lower() for cell in _cells(line)]
        if "url" not in headers:
            continue
        entries = []
        for row in lines[start + 1 :]:
            if not (row.startswith("|") and row.endswith("|")):
                break  # table ended
            cells = _cells(row)
            if all(re.fullmatch(r":?-{2,}:?", cell.strip()) for cell in cells if cell.strip()):
                continue  # separator row
            entry = {
                header: cell.strip()
                for header, cell in zip(headers, cells)
                if header in _TABLE_COLUMNS and cell.strip()
            }
            if entry.get("url"):
                entry["url"] = _clean_url(entry["url"])
                entries.append(entry)
        return entries
    return []


def _cells(row: str) -> list[str]:
    return row.strip().strip("|").split("|")


def _clean_url(cell: str) -> str:
    """Accept `<url>`, `[label](url)`, or bare url — Obsidian renders links."""
    match = _MD_LINK_RE.search(cell)
    if match:
        return match.group(1).strip()
    return cell.strip().strip("<>")


def _parse_entry(entry: object, *, index: int) -> ScoutTarget | None:
    if not isinstance(entry, dict):
        logging.warning(f"Scout: targets[{index}] is not a mapping; skipped.")
        return None
    url = str(entry.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        logging.warning(f"Scout: targets[{index}] has no valid url; skipped.")
        return None

    cadence = str(entry.get("cadence") or "daily").strip().lower()
    if cadence not in _VALID_CADENCES:
        logging.warning(
            f"Scout: targets[{index}] unknown cadence {cadence!r}; falling back to daily."
        )
        cadence = "daily"

    max_items: int | None = None
    if entry.get("max_items") is not None:
        try:
            max_items = max(1, int(entry["max_items"]))
        except (TypeError, ValueError):
            logging.warning(f"Scout: targets[{index}] bad max_items; using default.")

    parser = str(entry["parser"]).strip() if entry.get("parser") else None
    return ScoutTarget(url=url, parser=parser, cadence=cadence, max_items=max_items)
