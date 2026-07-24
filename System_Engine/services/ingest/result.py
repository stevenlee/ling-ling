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
    part_digest: dict | None = None
    page_type: str = "entity"
    rendered_markdown: str = ""
    wiki_meta: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    transient: bool = False

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


@dataclass
class DocumentIngestResult:
    """Document-level outcome and commit decision for a long ingest."""

    ok: bool
    status: str  # "complete" | "partial" | "failed"
    stage: str
    expected_parts: int
    completed_parts: list[int] = field(default_factory=list)
    failed_parts: list[dict] = field(default_factory=list)
    synthesis_path: Path | None = None
    archivable: bool = False
    detail: str = ""
    metrics: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok and self.archivable
