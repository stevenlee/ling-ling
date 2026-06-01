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

    def classify_document(self, filename, content_prefix):
        filename_lower = filename.lower()
        content_lower = content_prefix.lower()
        if filename_lower.startswith(("us", "ep", "jp", "cn")) or "patent" in filename_lower:
            return "patent"
        elif "claims" in content_lower and "prior art" in content_lower:
            return "patent"
        elif "abstract" in content_lower and "introduction" in content_lower:
            return "paper"
        elif "novel" in filename_lower or "novel" in content_lower:
            return "novel"
        return "default"

    def generate_persona_and_template(self, category):
        return {
            "persona_name": f"{category}-assistant",
            "persona_content": f"# {category.capitalize()} Assistant\nFake guidelines for {category}",
            "template_name": f"{category}-summary",
            "template_content": f"# {category.capitalize()} Summary\nFake structure for {category}"
        }


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

        # Mock out paths to keep tests isolated
        scripture_dir = tmp_path / "Scripture"
        scripture_dir.mkdir()
        monkeypatch.setattr("services.ingestion_pipeline.SCRIPTURE_DIR", scripture_dir)
        monkeypatch.setattr("services.ingestion_pipeline.PERSONAS_DIR", scripture_dir / "Personas")
        monkeypatch.setattr("services.ingestion_pipeline.TEMPLATES_DIR", tmp_path / "Templates")

        # Content has claims and prior art
        content = "This describes an invention.\n\nClaims\n1. A device...\n\nPrior Art\nExisting systems..."
        source_file = tmp_path / "MyPatent.md"
        source_file.write_text(content)

        monkeypatch.setattr("services.ingestion_pipeline.update_wiki_index", MagicMock())
        monkeypatch.setattr("services.ingestion_pipeline.PAGES_DIR", tmp_path / "pages")
        monkeypatch.setattr("services.ingestion_pipeline.INDEX_FILE", tmp_path / "index.md")

        pipeline.ingest_markdown(content, source_file)

        # Single page, so it should use synthesis persona/template directly
        assert len(llm.generate_entity_page_calls) == 1
        call = llm.generate_entity_page_calls[0]
        assert call["persona"] == "patent-expert"
        assert call["forced_template"] == "patent-rpt"

    def test_short_doc_paper_auto_detection_by_filename(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)

        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        monkeypatch.setattr(pipeline, "rag", rag)

        # Mock out paths
        scripture_dir = tmp_path / "Scripture"
        scripture_dir.mkdir()
        monkeypatch.setattr("services.ingestion_pipeline.SCRIPTURE_DIR", scripture_dir)
        monkeypatch.setattr("services.ingestion_pipeline.PERSONAS_DIR", scripture_dir / "Personas")
        monkeypatch.setattr("services.ingestion_pipeline.TEMPLATES_DIR", tmp_path / "Templates")

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
        assert call["forced_template"] == "patent-rpt"

    def test_short_doc_frontmatter_overrides(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)

        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        monkeypatch.setattr(pipeline, "rag", rag)

        # Mock out paths
        scripture_dir = tmp_path / "Scripture"
        scripture_dir.mkdir()
        monkeypatch.setattr("services.ingestion_pipeline.SCRIPTURE_DIR", scripture_dir)
        monkeypatch.setattr("services.ingestion_pipeline.PERSONAS_DIR", scripture_dir / "Personas")
        monkeypatch.setattr("services.ingestion_pipeline.TEMPLATES_DIR", tmp_path / "Templates")

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

        # Mock out paths
        scripture_dir = tmp_path / "Scripture"
        scripture_dir.mkdir()
        monkeypatch.setattr("services.ingestion_pipeline.SCRIPTURE_DIR", scripture_dir)
        monkeypatch.setattr("services.ingestion_pipeline.PERSONAS_DIR", scripture_dir / "Personas")
        monkeypatch.setattr("services.ingestion_pipeline.TEMPLATES_DIR", tmp_path / "Templates")

        content = "US Patent Document\n\nClaims\nPrior Art\n" + ("Part content section. " * 100)
        source_file = tmp_path / "US_Patent_Long.md"
        source_file.write_text(content)

        monkeypatch.setattr("services.ingestion_pipeline.update_wiki_index", MagicMock())
        pages_dir = tmp_path / "pages"
        monkeypatch.setattr("services.ingestion_pipeline.PAGES_DIR", pages_dir)
        monkeypatch.setattr("services.ingestion_pipeline.INDEX_FILE", tmp_path / "index.md")
        monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", False)

        pipeline.ingest_markdown(content, source_file)

        # For parts, ingest_to_wiki should be called with translator/translation-rpt
        assert len(llm.generate_entity_page_calls) > 1
        for call in llm.generate_entity_page_calls:
            assert call["persona"] == "translator"
            assert call["forced_template"] == "translation-rpt"

        # For final synthesis, it should use patent-expert/patent-rpt based on US prefix
        assert len(llm.generate_synthesis_calls) == 1
        syn_call = llm.generate_synthesis_calls[0]
        assert syn_call["persona"] == "patent-expert"
        assert syn_call["template"] == "patent-rpt"

    def test_doctype_table_registry_lifecycle(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)

        scripture_dir = tmp_path / "Scripture"
        scripture_dir.mkdir()
        monkeypatch.setattr("services.ingestion_pipeline.SCRIPTURE_DIR", scripture_dir)

        # File does not exist initially, load mapping should create it and return defaults
        mappings = pipeline.load_doctype_mappings()
        assert "patent" in mappings
        assert mappings["patent"]["persona"] == "patent-expert"
        assert mappings["patent"]["template"] == "patent-rpt"

        doctype_file = scripture_dir / "DocType.md"
        assert doctype_file.exists()

        # Register a new type
        pipeline.register_doctype("novel", "novel-assistant", "novel-summary", "Custom Novel layout")
        
        # Load again and check it's registered
        updated_mappings = pipeline.load_doctype_mappings()
        assert "novel" in updated_mappings
        assert updated_mappings["novel"]["persona"] == "novel-assistant"
        assert updated_mappings["novel"]["template"] == "novel-summary"
        assert updated_mappings["novel"]["description"] == "Custom Novel layout"

    def test_unregistered_category_dynamic_growth(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)

        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        monkeypatch.setattr(pipeline, "rag", rag)

        scripture_dir = tmp_path / "Scripture"
        personas_dir = scripture_dir / "Personas"
        templates_dir = tmp_path / "Templates"
        
        scripture_dir.mkdir()
        personas_dir.mkdir()
        templates_dir.mkdir()

        monkeypatch.setattr("services.ingestion_pipeline.SCRIPTURE_DIR", scripture_dir)
        monkeypatch.setattr("services.ingestion_pipeline.PERSONAS_DIR", personas_dir)
        monkeypatch.setattr("services.ingestion_pipeline.TEMPLATES_DIR", templates_dir)

        monkeypatch.setattr("services.ingestion_pipeline.update_wiki_index", MagicMock())
        monkeypatch.setattr("services.ingestion_pipeline.PAGES_DIR", tmp_path / "pages")
        monkeypatch.setattr("services.ingestion_pipeline.INDEX_FILE", tmp_path / "index.md")

        # Ingest novel document, LLM will classify it as "novel"
        content = "This is a story about ling-ling the cute assistant in a fantasy land."
        source_file = tmp_path / "novel_clipping.md"
        
        pipeline.ingest_markdown(content, source_file)

        # Check that files were dynamically written to the directories
        expected_persona_file = personas_dir / "novel-assistant.md"
        expected_template_file = templates_dir / "novel-summary.md"

        assert expected_persona_file.exists()
        assert expected_template_file.exists()

        assert "Fake guidelines for novel" in expected_persona_file.read_text()
        assert "Fake structure for novel" in expected_template_file.read_text()

        # Check that the mapping was written to DocType.md table
        mappings = pipeline.load_doctype_mappings()
        assert "novel" in mappings
        assert mappings["novel"]["persona"] == "novel-assistant"
        assert mappings["novel"]["template"] == "novel-summary"

        # Check that the ingested page used the dynamically created persona and template
        assert len(llm.generate_entity_page_calls) == 1
        call = llm.generate_entity_page_calls[0]
        assert call["persona"] == "novel-assistant"
        assert call["forced_template"] == "novel-summary"
