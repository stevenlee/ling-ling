"""services/ingest/result.py — typed ingest outcomes (post-P4 follow-up).

Pins the contract that replaced dict/None: falsy-on-failure (so existing
``if not result:`` guards hold), stage tracking, and error-kind
classification that lets callers tell a transient LLM failure from a
filesystem error.
"""

from pathlib import Path
from unittest.mock import MagicMock

from services.ingest.result import IngestResult
from services.ingestion_pipeline import IngestionPipeline


def test_bool_follows_ok():
    assert IngestResult(ok=True)
    assert not IngestResult(ok=False, stage="llm")


def test_failure_classification():
    fs = IngestResult.failure("write", OSError("disk full"))
    assert (fs.error_kind, fs.stage, fs.ok) == ("fs_error", "write", False)
    llm = IngestResult.failure("llm", ValueError("LLM generation failed."))
    assert llm.error_kind == "llm_error"
    other = IngestResult.failure("rag_index", RuntimeError("segfault-adjacent"))
    assert other.error_kind == "unexpected"
    assert "segfault" in (other.detail or "")


def _pipe(llm):
    pipe = IngestionPipeline.__new__(IngestionPipeline)
    pipe.llm = llm
    pipe.rag = MagicMock()
    return pipe


def test_llm_failure_reports_llm_stage():
    llm = MagicMock()
    llm.generate_entity_page.return_value = None  # model produced nothing
    result = _pipe(llm).ingest_to_wiki("body", Path("Doc.md"))
    assert not result
    assert result.stage == "llm"
    assert result.error_kind == "llm_error"


def test_write_failure_reports_write_stage(monkeypatch, tmp_path):
    llm = MagicMock()
    llm.generate_entity_page.return_value = {"title": "T", "tags": [], "content": "body"}
    llm.current_trace_ids.return_value = []
    llm.current_run_id.return_value = None
    pipe = _pipe(llm)

    import services.ingestion_pipeline as ip_mod

    monkeypatch.setattr(ip_mod, "PAGES_DIR", tmp_path / "pages")

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(ip_mod, "atomic_write_text", boom)
    result = pipe.ingest_to_wiki("body", Path("Doc.md"))
    assert not result
    assert result.stage == "write"
    assert result.error_kind == "fs_error"


def test_success_carries_payload(monkeypatch, tmp_path):
    llm = MagicMock()
    llm.generate_entity_page.return_value = {
        "title": "T",
        "tags": ["math"],
        "content": "raw body",
        "pending_concepts": "carry",
    }
    llm.current_trace_ids.return_value = []
    llm.current_run_id.return_value = None
    pipe = _pipe(llm)

    import services.ingestion_pipeline as ip_mod

    monkeypatch.setattr(ip_mod, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(ip_mod, "update_wiki_index", MagicMock())

    result = pipe.ingest_to_wiki("body", Path("Doc.md"))
    assert result and result.stage == "done"
    assert result.title == "Doc (Synthesis)"
    assert result.page_path and result.page_path.exists()
    assert result.tags == ["math"]
    assert result.content == "raw body"  # pre-quality-check LLM body, as before
    assert result.pending_concepts == "carry"
