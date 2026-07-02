"""MarkdownDocument — one vault note as a (frontmatter, body) pair (P1).

Wraps the parse → strip-body-frontmatter → mutate → dump → write sequence
that read-modify-write call sites (ingestion resume state, tag updates,
signal backfill) each hand-rolled. Read-only parses and one-shot dumps don't
need this — keep calling core.parser directly there.

Semantics are exactly core.parser's:
- ``meta`` includes normalized tags harvested from BOTH frontmatter and body
  hashtags (parse_markdown_metadata).
- ``body`` has any leading YAML block removed (strip_body_frontmatter), so a
  document never grows a second frontmatter on re-save.
"""

from __future__ import annotations

from pathlib import Path

from core.parser import (
    dump_markdown_with_metadata,
    parse_markdown_metadata,
    strip_body_frontmatter,
)


class MarkdownDocument:
    def __init__(self, meta: dict | None = None, body: str = "", path: Path | None = None):
        self.meta: dict = meta if meta is not None else {}
        self.body: str = body
        self.path: Path | None = path

    @classmethod
    def from_text(cls, text: str, path: Path | None = None) -> "MarkdownDocument":
        meta = parse_markdown_metadata(text)
        body, _fixes = strip_body_frontmatter(text)
        return cls(meta, body, path)

    @classmethod
    def load(cls, path: Path) -> "MarkdownDocument":
        return cls.from_text(path.read_text(encoding="utf-8"), path=path)

    def to_text(self) -> str:
        return dump_markdown_with_metadata(self.meta, self.body)

    def save(self, path: Path | None = None) -> str:
        """Serialize and write; returns the written text (callers often need
        it for indexing right after)."""
        target = path or self.path
        if target is None:
            raise ValueError("MarkdownDocument.save() needs a path (none was set)")
        text = self.to_text()
        target.write_text(text, encoding="utf-8")
        self.path = target
        return text
