"""PartState — typed loop state for the long-document distillation pass (P2d).

Replaces the six loose variables `_process_parts` threaded through its loop
and returned as an untyped dict. `pending_concepts` stays a plain string on
purpose: it round-trips through part-note frontmatter for B1 resume, and a
structured format would invalidate every existing part note.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PartState:
    master_tags: list = field(default_factory=list)
    pending_concepts: str = ""
    part_digests: list = field(default_factory=list)
    part_paths: list[Path] = field(default_factory=list)
    navigation_items: list[str] = field(default_factory=list)
    total_output_chars: int = 0
