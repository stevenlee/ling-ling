"""B1 resume — `IngestionPipeline._resume_part` skip-existing logic.

Covers the completeness gates (missing / pre-B1 note) and the chunk-fingerprint
guard that re-distills a Part whose source was re-chunked under a different
config instead of silently reusing a now-mismatched note.
"""

from pathlib import Path


from core.parser import dump_markdown_with_metadata
from services.ingestion_pipeline import IngestionPipeline

_DIGEST_HEADER = "## 🧩 Part Digest Appendix"
_DIGEST = {"part": 1, "title": "P1", "thesis": "t"}


def _write_part(path: Path, *, meta: dict, with_appendix: bool = True) -> None:
    body = "Part body.\n"
    if with_appendix:
        body += f"\n{_DIGEST_HEADER}\n- **Thesis**: t\n"
    path.write_text(dump_markdown_with_metadata(meta, body), encoding="utf-8")


def test_missing_note_returns_none(tmp_path):
    assert IngestionPipeline._resume_part(tmp_path / "nope.md", "chunk") is None


def test_pre_b1_note_without_resume_state_returns_none(tmp_path):
    """A finalized note from before B1 (appendix but no part_digest) re-distills
    rather than being silently dropped."""
    p = tmp_path / "part.md"
    _write_part(p, meta={"tags": ["x"]})  # no part_digest in frontmatter
    assert IngestionPipeline._resume_part(p, "chunk") is None


def test_complete_note_resumes(tmp_path):
    p = tmp_path / "part.md"
    chunk = "the original chunk text"
    _write_part(
        p,
        meta={
            "tags": ["x"],
            "pending_concepts": "carry",
            "part_digest": _DIGEST,
            "part_chunk_hash": IngestionPipeline._chunk_fingerprint(chunk),
        },
    )
    resumed = IngestionPipeline._resume_part(p, chunk)
    assert resumed is not None
    assert resumed["pending_concepts"] == "carry"
    assert resumed["part_digest"] == _DIGEST
    assert resumed["tags"] == ["x"]


def test_chunk_fingerprint_mismatch_forces_redistill(tmp_path):
    """Same Part path, but the chunk text changed (re-chunked) → stale → None."""
    p = tmp_path / "part.md"
    _write_part(
        p,
        meta={
            "tags": ["x"],
            "part_digest": _DIGEST,
            "part_chunk_hash": IngestionPipeline._chunk_fingerprint("OLD chunk"),
        },
    )
    assert IngestionPipeline._resume_part(p, "DIFFERENT chunk now") is None
    # ...but the matching chunk still resumes.
    assert IngestionPipeline._resume_part(p, "OLD chunk") is not None


def test_legacy_note_without_hash_still_resumes(tmp_path):
    """Notes written before fingerprinting carry no part_chunk_hash; they resume
    as before (no forced mass re-distillation of the existing corpus)."""
    p = tmp_path / "part.md"
    _write_part(p, meta={"tags": ["x"], "part_digest": _DIGEST})  # no hash
    assert IngestionPipeline._resume_part(p, "any chunk") is not None


def test_fingerprint_is_stable_and_sensitive():
    f = IngestionPipeline._chunk_fingerprint
    assert f("abc") == f("abc")
    assert f("abc") != f("abc ")
    assert len(f("abc")) == 16
