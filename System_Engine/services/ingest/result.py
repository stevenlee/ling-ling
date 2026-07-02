"""IngestResult — typed outcome of one wiki-page ingestion (post-P4 follow-up).

Replaces the old contract where ingest_to_wiki returned the raw LLM dict
(with `_page_path`/`_title`/`_tags` stuffed in) on success and a bare `None`
on ANY failure — so callers couldn't tell a transient LLM failure from a
filesystem error, and a silently dropped Part was indistinguishable from a
skipped one.

``__bool__`` is ``ok``, so every existing ``if not result:`` guard keeps
working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Stages, in execution order. `stage` on a failure = the phase that raised.
#   llm         generating the wiki page content
#   quality     deterministic markdown quality passes
#   write       writing the page file to the vault
#   rag_index   adding the page to ChromaDB
#   wiki_index  rebuilding the wiki index
#   done        success


@dataclass
class IngestResult:
    ok: bool
    stage: str = "done"
    error_kind: str | None = None  # "llm_error" | "fs_error" | "unexpected"
    detail: str | None = None
    page_path: Path | None = None
    title: str | None = None
    tags: list = field(default_factory=list)
    content: str = ""  # raw LLM body (pre-quality-checks), as before
    pending_concepts: str = ""
    page_type: str = "entity"

    def __bool__(self) -> bool:
        return self.ok

    @classmethod
    def failure(cls, stage: str, exc: Exception) -> "IngestResult":
        if isinstance(exc, OSError):
            kind = "fs_error"
        elif isinstance(exc, ValueError):
            kind = "llm_error"  # ingest raises ValueError on empty LLM output
        else:
            kind = "unexpected"
        return cls(ok=False, stage=stage, error_kind=kind, detail=str(exc))
