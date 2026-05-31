import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import pytest
from unittest.mock import MagicMock
from services.ingestion_pipeline import IngestionPipeline

class FakeLLM:
    def __init__(self):
        self.generate_entity_page_calls = []
        self.generate_synthesis_calls = []
        self.model = "fake-model"
        self.trace_store = MagicMock()

    def generate_entity_page(self, markdown_content, filename, index_content, context_hint=None, persona=None, forced_template=None):
        self.generate_entity_page_calls.append({
            "content": markdown_content,
            "filename": filename,
            "persona": persona,
            "forced_template": forced_template,
        })
        return {
            "title": filename,
            "tags": ["topic"],
            "type": "entity",
            "content": "Generated content from fake LLM",
        }

    def generate_part_digest(self, title, part_number, total_parts, raw_chunk, part_note, pending_concepts=""):
        return {
            "part": part_number,
            "title": f"Part {part_number}",
            "thesis": "Thesis description",
        }

    def generate_synthesis(self, title, part_digests, final_concepts, template=None, persona=None):
        self.generate_synthesis_calls.append({
            "title": title,
            "template": template,
            "persona": persona,
        })
        return "Fake synthesis body"

    def critique_text(self, candidate, sources, focus=None):
        return "Overall Verdict: keep"


class FakeRAG:
    def __init__(self):
        self.docs = []

    def add_document(self, path, title, markdown, tags=None, section_path=None):
        self.docs.append((path, title, tags))


@pytest.fixture
def fake_services():
    llm = FakeLLM()
    rag = FakeRAG()
    return llm, rag


class TestDynamicPipeline:
    def test_short_doc_patent_auto_detection_by_keywords(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)

        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        monkeypatch.setattr(pipeline, "rag", rag)

        # Content has claims and prior art
        content = "This describes an invention.\n\nClaims\n1. A device...\n\nPrior Art\nExisting systems..."
        source_file = tmp_path / "MyPatent.md"
        source_file.write_text(content)

        monkeypatch.setattr("services.ingestion_pipeline.update_wiki_index", MagicMock())

        # Direct patching of config/settings directories to temp dirs
        monkeypatch.setattr("services.ingestion_pipeline.PAGES_DIR", tmp_path / "pages")
        monkeypatch.setattr("services.ingestion_pipeline.INDEX_FILE", tmp_path / "index.md")

        pipeline.ingest_markdown(content, source_file)

        # Single page, so it should use synthesis persona/template directly
        assert len(llm.generate_entity_page_calls) == 1
        call = llm.generate_entity_page_calls[0]
        assert call["persona"] == "patent-expert"
        assert call["forced_template"] == "sw-inv-disclosure-rpt"

    def test_short_doc_paper_auto_detection_by_filename(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)

        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        monkeypatch.setattr(pipeline, "rag", rag)

        # Filename starts with US -> should be patent
        content = "Standard paper."
        source_file = tmp_path / "US9876543.md"
        source_file.write_text(content)

        monkeypatch.setattr("services.ingestion_pipeline.update_wiki_index", MagicMock())

        monkeypatch.setattr("services.ingestion_pipeline.PAGES_DIR", tmp_path / "pages")
        monkeypatch.setattr("services.ingestion_pipeline.INDEX_FILE", tmp_path / "index.md")

        pipeline.ingest_markdown(content, source_file)

        assert len(llm.generate_entity_page_calls) == 1
        call = llm.generate_entity_page_calls[0]
        assert call["persona"] == "patent-expert"
        assert call["forced_template"] == "sw-inv-disclosure-rpt"

    def test_short_doc_frontmatter_overrides(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)

        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        monkeypatch.setattr(pipeline, "rag", rag)

        content = "---\ndocument_type: paper\nsynthesis_persona: super-analyst\nsynthesis_template: custom-rpt\n---\nSome body"
        source_file = tmp_path / "paper.md"
        source_file.write_text(content)

        monkeypatch.setattr("services.ingestion_pipeline.update_wiki_index", MagicMock())

        monkeypatch.setattr("services.ingestion_pipeline.PAGES_DIR", tmp_path / "pages")
        monkeypatch.setattr("services.ingestion_pipeline.INDEX_FILE", tmp_path / "index.md")

        pipeline.ingest_markdown(content, source_file)

        assert len(llm.generate_entity_page_calls) == 1
        call = llm.generate_entity_page_calls[0]
        assert call["persona"] == "super-analyst"
        assert call["forced_template"] == "custom-rpt"

    def test_long_doc_dynamic_routing(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)

        # Set chunk_size small to force multi-part split
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 20)
        monkeypatch.setattr(pipeline, "rag", rag)

        content = "US Patent Document\n\nClaims\nPrior Art\n" + ("Part content section. " * 100)
        source_file = tmp_path / "US_Patent_Long.md"
        source_file.write_text(content)

        monkeypatch.setattr("services.ingestion_pipeline.update_wiki_index", MagicMock())

        pages_dir = tmp_path / "pages"
        monkeypatch.setattr("services.ingestion_pipeline.PAGES_DIR", pages_dir)
        monkeypatch.setattr("services.ingestion_pipeline.INDEX_FILE", tmp_path / "index.md")

        # Mock out the critique enabled to false to avoid complexity
        monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", False)

        pipeline.ingest_markdown(content, source_file)

        # For parts, ingest_to_wiki should be called with translator/translation-rpt
        assert len(llm.generate_entity_page_calls) > 1
        for call in llm.generate_entity_page_calls:
            assert call["persona"] == "translator"
            assert call["forced_template"] == "translation-rpt"

        # For final synthesis, it should use patent-expert/sw-inv-disclosure-rpt based on US prefix
        assert len(llm.generate_synthesis_calls) == 1
        syn_call = llm.generate_synthesis_calls[0]
        assert syn_call["persona"] == "patent-expert"
        assert syn_call["template"] == "sw-inv-disclosure-rpt"
