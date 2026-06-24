"""P4 end-to-end smoke tests for ThoughtfulSplitter wiring.

These tests verify two contracts:

1. **Flag OFF (default)**: the system behaves exactly as before. Existing
   272 tests cover this in detail; we add a couple of focused asserts
   here to make the contract explicit.

2. **Flag ON**: `ThoughtfulSplitter` is selected, chunk metadata
   (section_path, boundary_type) flows through `part_info` into
   `wiki_meta`, and `rag.add_document` receives the section_path.

We never call a real LLM here — we stub it. The point is plumbing, not
content quality.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

import importlib

import pytest


CORPUS_DIR = Path(__file__).parent / "corpus"


def _reload_with_flags(**env_overrides):
    """Reload `core.config` and the pipeline module so env flag changes
    take effect. Returns the reloaded `IngestionPipeline` + `CounterAgent` classes.
    """
    # Set env vars
    for k, v in env_overrides.items():
        os.environ[k] = v

    import core.config
    importlib.reload(core.config)

    import services.ingestion_pipeline as ip_mod
    importlib.reload(ip_mod)

    import agents.counter_agent as ca_mod
    importlib.reload(ca_mod)

    return ip_mod.IngestionPipeline, ca_mod.CounterAgent


def _clear_thoughtful_env():
    """Wipe THOUGHTFUL_* env so other tests aren't polluted."""
    for k in (
        "USE_THOUGHTFUL_SPLITTER",
        "THOUGHTFUL_USE_LLM_FOR_INGEST",
        "THOUGHTFUL_USE_LLM_FOR_COUNTER",
        "THOUGHTFUL_EMIT_SUMMARY",
        "THOUGHTFUL_CACHE_DIR",
    ):
        os.environ.pop(k, None)


@pytest.fixture(autouse=True)
def _isolate_env():
    """Save / restore env around every test in this file."""
    backup = {
        k: os.environ.get(k)
        for k in (
            "USE_THOUGHTFUL_SPLITTER",
            "THOUGHTFUL_USE_LLM_FOR_INGEST",
            "THOUGHTFUL_USE_LLM_FOR_COUNTER",
            "THOUGHTFUL_EMIT_SUMMARY",
            "THOUGHTFUL_CACHE_DIR",
        )
    }
    yield
    _clear_thoughtful_env()
    for k, v in backup.items():
        if v is not None:
            os.environ[k] = v
    # Reload back to original state so other test files see defaults.
    import core.config
    importlib.reload(core.config)
    import services.ingestion_pipeline as ip_mod
    importlib.reload(ip_mod)
    import agents.counter_agent as ca_mod
    importlib.reload(ca_mod)


# ─── Splitter selection ────────────────────────────────────────────

class TestSplitterSelection:
    def test_flag_off_uses_text_splitter(self):
        _clear_thoughtful_env()
        IngestionPipeline, CounterAgent = _reload_with_flags(USE_THOUGHTFUL_SPLITTER="false")
        from services.text_splitter import TextSplitter
        from services.thoughtful_splitter import ThoughtfulSplitter

        pipeline = IngestionPipeline(llm_client=MagicMock(), rag_manager=MagicMock())
        assert isinstance(pipeline.splitter, TextSplitter)
        assert not isinstance(pipeline.splitter, ThoughtfulSplitter)

        counter = CounterAgent(llm=MagicMock())
        assert isinstance(counter.splitter, TextSplitter)

    def test_flag_on_uses_thoughtful_splitter(self):
        IngestionPipeline, CounterAgent = _reload_with_flags(USE_THOUGHTFUL_SPLITTER="true")
        from services.thoughtful_splitter import ThoughtfulSplitter

        pipeline = IngestionPipeline(llm_client=MagicMock(), rag_manager=MagicMock())
        assert isinstance(pipeline.splitter, ThoughtfulSplitter)

        counter = CounterAgent(llm=MagicMock())
        assert isinstance(counter.splitter, ThoughtfulSplitter)

    def test_counter_agent_defaults_use_llm_off_even_when_ingest_is_on(self):
        """Two separate env flags: counter's default is OFF even if ingest's is ON."""
        IngestionPipeline, CounterAgent = _reload_with_flags(
            USE_THOUGHTFUL_SPLITTER="true",
            THOUGHTFUL_USE_LLM_FOR_INGEST="true",
            THOUGHTFUL_USE_LLM_FOR_COUNTER="false",
        )

        pipeline = IngestionPipeline(llm_client=MagicMock(), rag_manager=MagicMock())
        counter = CounterAgent(llm=MagicMock())

        assert pipeline.splitter._default_use_llm is True
        assert counter.splitter._default_use_llm is False


# ─── section_path / boundary_type flow into wiki_meta ──────────────

class TestPartMetadataFlow:
    """Under flag ON, ThoughtfulSplitter's section_path threads into wiki_meta."""

    def test_part_metadata_includes_section_path_when_set(self):
        IngestionPipeline, _ = _reload_with_flags(USE_THOUGHTFUL_SPLITTER="true")
        meta = IngestionPipeline._build_part_metadata(
            title="X (Part 2)",
            page_type="entity",
            tags=["a"],
            part_info={
                "current": 2,
                "total": 5,
                "master_tags": ["a"],
                "section_path": ["Chapter 2", "Background"],
                "boundary_type": "h2",
                "source_span": {"source_start_line": 1, "source_end_line": 10},
            },
            quality_fixes=[],
        )
        assert meta["section_path"] == ["Chapter 2", "Background"]
        assert meta["boundary_type"] == "h2"

    def test_part_metadata_omits_section_path_when_empty(self):
        """Under legacy splitter, no section_path → field omitted."""
        IngestionPipeline, _ = _reload_with_flags(USE_THOUGHTFUL_SPLITTER="false")
        meta = IngestionPipeline._build_part_metadata(
            title="X (Part 1)",
            page_type="entity",
            tags=[],
            part_info={
                "current": 1, "total": 3, "master_tags": [],
                "section_path": [],  # explicit empty
            },
            quality_fixes=[],
        )
        assert "section_path" not in meta
        assert "boundary_type" not in meta


# ─── End-to-end ingestion smoke ─────────────────────────────────────

class TestEndToEndIngestion:
    """Run a long-doc through the full pipeline with mocked LLM/RAG."""

    def _make_pipeline(self, flag_on: bool):
        if flag_on:
            IngestionPipeline, _ = _reload_with_flags(
                USE_THOUGHTFUL_SPLITTER="true",
                THOUGHTFUL_USE_LLM_FOR_INGEST="false",  # don't trigger P5 in tests
            )
        else:
            _clear_thoughtful_env()
            IngestionPipeline, _ = _reload_with_flags(USE_THOUGHTFUL_SPLITTER="false")

        llm = MagicMock()
        llm.model = "test-model"
        # generate_entity_page is called per-part. Return a minimal valid result.
        llm.generate_entity_page = MagicMock(return_value={
            "title": "Stub Page",
            "tags": ["stub"],
            "type": "entity",
            "content": "stub body content\n",
        })
        llm.generate_part_digest = MagicMock(return_value={
            "part": 1, "title": "Stub", "thesis": "Stub thesis",
            "key_points": [], "evidence": [], "terms": [],
            "open_questions": [], "handoff": "",
        })
        llm.generate_synthesis = MagicMock(return_value="Stub synthesis.")
        # Critique post-step needs both helpers; return empty critique so the
        # pipeline's skip-on-empty path keeps these tests focused on splitter/
        # synthesis wiring rather than the critique operation.
        llm.format_digest_for_prompt = MagicMock(return_value="stub-digest")
        llm.critique_text = MagicMock(return_value="")

        rag = MagicMock()
        return IngestionPipeline(llm_client=llm, rag_manager=rag), llm, rag

    def test_flag_off_long_doc_smoke(self, tmp_path, monkeypatch):
        """Legacy splitter: ingestion works end-to-end without crash."""
        # Build the pipeline FIRST (it reloads core.config, which would wipe the
        # path monkeypatches), THEN redirect so the patches survive — otherwise
        # the test leaks part notes into the real vault.
        pipeline, llm, rag = self._make_pipeline(flag_on=False)
        self._redirect_paths(tmp_path, monkeypatch)

        text = (CORPUS_DIR / "long_essay_with_code.md").read_text(encoding="utf-8")
        # Force long-doc path by making chunk_size small.
        pipeline.splitter.chunk_size = 1000
        pipeline.splitter.overlap = 100

        source = tmp_path / "input.md"
        source.write_text(text, encoding="utf-8")
        pipeline.ingest_markdown(text, source)

        # generate_entity_page called per part; generate_synthesis once.
        assert llm.generate_entity_page.called
        assert llm.generate_synthesis.called
        # RAG ingest was triggered.
        assert rag.add_document.called

    def test_flag_on_long_doc_passes_section_path_to_rag(self, tmp_path, monkeypatch):
        """ThoughtfulSplitter: each part's add_document call carries
        section_path matching what the splitter produced."""
        pipeline, llm, rag = self._make_pipeline(flag_on=True)
        self._redirect_paths(tmp_path, monkeypatch)
        # Small sizes so the doc actually splits into multiple parts.
        from services.thoughtful_splitter import ThoughtfulSplitter
        pipeline.splitter = ThoughtfulSplitter(
            target_size=1000, max_size=1800, min_size=200, snap_window=400, overlap_chars=0,
            default_use_llm=False,
        )

        text = (CORPUS_DIR / "long_essay_with_code.md").read_text(encoding="utf-8")
        source = tmp_path / "input.md"
        source.write_text(text, encoding="utf-8")
        pipeline.ingest_markdown(text, source)

        # Collect every section_path kwarg passed to rag.add_document.
        section_paths_passed = [
            call.kwargs.get("section_path")
            for call in rag.add_document.call_args_list
        ]
        # At least one call must have a non-empty section_path (the document
        # starts with "# Notes on Cache Coherence Protocols").
        assert any(sp for sp in section_paths_passed if sp), (
            f"No section_path threaded to RAG. Got: {section_paths_passed}"
        )

    def test_flag_on_long_doc_writes_section_path_to_yaml(self, tmp_path, monkeypatch):
        """ThoughtfulSplitter: the written part-note YAML frontmatter
        contains `section_path:` field."""
        pipeline, llm, rag = self._make_pipeline(flag_on=True)
        self._redirect_paths(tmp_path, monkeypatch)
        from services.thoughtful_splitter import ThoughtfulSplitter
        pipeline.splitter = ThoughtfulSplitter(
            target_size=1000, max_size=1800, min_size=200, snap_window=400, overlap_chars=0,
            default_use_llm=False,
        )

        text = (CORPUS_DIR / "long_essay_with_code.md").read_text(encoding="utf-8")
        source = tmp_path / "input.md"
        source.write_text(text, encoding="utf-8")
        pipeline.ingest_markdown(text, source)

        # Look for any Part *.md file with section_path in frontmatter.
        from core.config import PAGES_DIR
        part_files = list(PAGES_DIR.rglob("*Part*.md"))
        assert part_files, "no part files were written"
        found_section_path = False
        for f in part_files:
            content = f.read_text(encoding="utf-8")
            if "section_path:" in content:
                found_section_path = True
                break
        assert found_section_path, "no Part note has section_path frontmatter"

    @staticmethod
    def _redirect_paths(tmp_path, monkeypatch):
        """Redirect PAGES_DIR / INDEX_FILE / FROM_LLM_DIR so the pipeline writes to tmp."""
        from core import config
        pages = tmp_path / "pages"
        pages.mkdir()
        notes = tmp_path / "notes"
        notes.mkdir()
        from_llm = tmp_path / "fromLingLing"
        from_llm.mkdir()
        index = tmp_path / "index.md"
        index.write_text("# index\n", encoding="utf-8")

        monkeypatch.setattr(config, "PAGES_DIR", pages)
        monkeypatch.setattr(config, "NOTES_DIR", notes)
        monkeypatch.setattr(config, "INDEX_FILE", index)
        monkeypatch.setattr(config, "FROM_LLM_DIR", from_llm)

        # Also patch references that imported these names directly.
        import services.ingestion_pipeline as ip_mod
        monkeypatch.setattr(ip_mod, "PAGES_DIR", pages)
        monkeypatch.setattr(ip_mod, "INDEX_FILE", index)
        monkeypatch.setattr(ip_mod, "SCRIPTURE_DIR", tmp_path / "Scripture")
        monkeypatch.setattr(ip_mod, "PROFILES_DIR", tmp_path / "Scripture" / "Profiles")
        monkeypatch.setattr(ip_mod, "PROFILES_PENDING_DIR", tmp_path / "Scripture" / "Profiles" / "_pending")
        monkeypatch.setattr(ip_mod, "FROM_LLM_DIR", from_llm)

        import core.vault_utils as vu_mod
        monkeypatch.setattr(vu_mod, "PAGES_DIR", pages)
        monkeypatch.setattr(vu_mod, "NOTES_DIR", notes)
        monkeypatch.setattr(vu_mod, "INDEX_FILE", index)
        monkeypatch.setattr(vu_mod, "READING_INDEX_FILE", tmp_path / "ReadingIndex.md")


# ─── ChromaDB metadata format ───────────────────────────────────────

class TestChromaDBSectionMetadata:
    """The section_path passed to add_document gets encoded as `>a>b>c>`."""

    def test_section_path_encoded_as_marker_string(self, tmp_path):
        from services.rag_manager import RAGManager

        # We can't easily test the full RAGManager without a chromadb backend.
        # Instead, monkeypatch _upsert_with_retry to capture metadatas.
        captured = {}

        class _FakeBM25:
            def mark_dirty(self):
                pass

        class _FakeSplitter:
            def split_text_with_spans(self, text):
                return [
                    {"text": text[i : i + 100], "start": i, "end": min(i + 100, len(text))}
                    for i in range(0, len(text), 100)
                ] or [{"text": "", "start": 0, "end": 0}]

        class _FakeRAG(RAGManager):
            def __init__(self):
                self.splitter = _FakeSplitter()
                self._bm25 = _FakeBM25()

            def delete_document(self, title):
                pass

            def _upsert_with_retry(self, **kwargs):
                captured["metadatas"] = kwargs["metadatas"]

        rag = _FakeRAG()
        source = tmp_path / "x.md"
        source.write_text("x" * 250, encoding="utf-8")
        rag.add_document(
            source, "MyTitle", "x" * 250,
            tags=["tag1"],
            section_path=["Chapter 1", "Background", "Methodology"],
        )

        # Every chunk's metadata should carry the encoded section_path marker.
        assert captured["metadatas"], "no chunks were upserted"
        marker = captured["metadatas"][0]["section_path"]
        assert marker == ">chapter 1>background>methodology>", f"got {marker!r}"

    def test_section_path_empty_when_none_passed(self, tmp_path):
        from services.rag_manager import RAGManager

        captured = {}

        class _FakeBM25:
            def mark_dirty(self):
                pass

        class _FakeSplitter:
            def split_text_with_spans(self, text):
                return [{"text": "chunk1", "start": 0, "end": len(text)}]

        class _FakeRAG(RAGManager):
            def __init__(self):
                self.splitter = _FakeSplitter()
                self._bm25 = _FakeBM25()

            def delete_document(self, title):
                pass

            def _upsert_with_retry(self, **kwargs):
                captured["metadatas"] = kwargs["metadatas"]

        rag = _FakeRAG()
        source = tmp_path / "x.md"
        source.write_text("test", encoding="utf-8")
        rag.add_document(source, "Title", "test", tags=["t"])

        marker = captured["metadatas"][0]["section_path"]
        assert marker == "", f"expected empty marker when no section_path, got {marker!r}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
