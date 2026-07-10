"""Load and validate the Scout targets file (Scripture/Scout.md frontmatter).

The file is user-edited in Obsidian, so validation is forgiving: a broken
entry is logged and skipped, never fatal — the rest of the list still runs.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.parsing.markdown_metadata import parse_markdown_metadata
from services.scout.models import ScoutTarget

_VALID_CADENCES = ("daily", "weekly")


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
        metadata = parse_markdown_metadata(path.read_text(encoding="utf-8"))
    except Exception as e:
        logging.error(f"Scout: failed to parse targets file: {e}")
        return [], ""

    language = str(metadata.get("language") or "").strip()
    raw_targets = metadata.get("targets")
    if not isinstance(raw_targets, list):
        logging.warning("Scout: targets file has no `targets:` list.")
        return [], language

    targets: list[ScoutTarget] = []
    for i, entry in enumerate(raw_targets):
        target = _parse_entry(entry, index=i)
        if target is not None:
            targets.append(target)
    return targets, language


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
