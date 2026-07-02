"""ChunkMetadata — THE chunk metadata schema (P2e).

Previously ~15 implicit keys assembled inline in RAGManager.add_document;
changing one meant grepping every reader. This dataclass is the single
authoritative definition of what a chunk row carries. Readers still receive
the flat dict Chroma stores (via ``to_chroma()``) — only the write side is
typed, so no reader changes were needed.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.rag.chroma_store import sanitize_tag_key


@dataclass
class ChunkMetadata:
    source: str  # filename of the vault file
    source_path: str  # vault-relative path, forward slashes
    doc_id: str  # sha256 of the vault-relative path
    title: str
    start_offset: int
    end_offset: int
    timestamp: str  # ISO time of indexing
    tags: str  # display form: ",tag1,tag2," (or "")
    section_path: str  # ">a>b>" marker so `$contains` can match a section
    boundary_type: str
    content_hash: str  # unchanged-content short-circuit key
    section_levels: list[str]  # raw section path; expands to section_l1..l6
    norm_tags: list[str]  # normalized tags; expand to tag_* boolean keys

    def to_chroma(self) -> dict:
        """Flatten to the exact dict shape ChromaDB stores (and every reader
        expects): fixed keys + tag_* booleans + section_l1..l6 + depth/full."""
        meta = {
            "source": self.source,
            "source_path": self.source_path,
            "doc_id": self.doc_id,
            "title": self.title,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "timestamp": self.timestamp,
            "tags": self.tags,
            "section_path": self.section_path,
            "boundary_type": self.boundary_type,
            "content_hash": self.content_hash,
        }

        # Boolean tag fields (sanitized) for where-clause filtering.
        for tag in self.norm_tags:
            san_tag = sanitize_tag_key(tag)
            if san_tag:
                meta[san_tag] = True

        # Section level mappings (l1 to l6).
        meta["section_depth"] = len(self.section_levels)
        for idx in range(6):
            key = f"section_l{idx + 1}"
            meta[key] = (
                self.section_levels[idx].lower().strip() if idx < len(self.section_levels) else ""
            )

        meta["section_path_full"] = " > ".join(s.lower().strip() for s in self.section_levels)
        return meta
