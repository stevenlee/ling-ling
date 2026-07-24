import pytest
import threading
from unittest.mock import MagicMock
from services.ingestion_pipeline import IngestionPipeline
from services.profile_manager import render_profile_markdown


class FakeLLM:
    """Routes like the real LLMClient: closed-choice select_profile over the
    registered options, open-ended classify_document for new categories."""

    def __init__(self):
        self.generate_entity_page_calls = []
        self.generate_part_digest_calls = []
        self.generate_synthesis_calls = []
        self.select_profile_calls = []
        self.model = "fake-model"
        self.trace_store = MagicMock()

    def generate_entity_page(
        self,
        markdown_content,
        filename,
        index_content,
        context_hint=None,
        persona=None,
        forced_template=None,
        content_attempts=2,
    ):
        self.generate_entity_page_calls.append(
            {
                "content": markdown_content,
                "filename": filename,
                "persona": persona,
                "forced_template": forced_template,
                "context_hint": context_hint,
            }
        )
        return {
            "title": filename,
            "tags": ["topic"],
            "type": "entity",
            "content": "Generated content from fake LLM",
        }

    def generate_part_digest(
        self, title, part_number, total_parts, raw_chunk, part_note, pending_concepts=""
    ):
        self.generate_part_digest_calls.append(part_number)
        return {
            "part": part_number,
            "title": f"Part {part_number}",
            "thesis": "Thesis description",
        }

    def generate_synthesis(self, title, part_digests, final_concepts, template=None, persona=None):
        self.generate_synthesis_calls.append(
            {
                "title": title,
                "template": template,
                "persona": persona,
            }
        )
        return "Fake synthesis body"

    def critique_text(self, candidate, sources, focus=None):
        return "Overall Verdict: keep"

    def select_profile(self, filename, content_prefix, options):
        self.select_profile_calls.append({"filename": filename, "options": options})
        names = {opt["name"] for opt in options}
        filename_lower = filename.lower()
        content_lower = content_prefix.lower()
        if "patent" in names and (
            filename_lower.startswith(("us", "ep", "jp", "cn"))
            or "patent" in filename_lower
            or ("claims" in content_lower and "prior art" in content_lower)
        ):
            return "patent"
        if "paper" in names and "abstract" in content_lower and "introduction" in content_lower:
            return "paper"
        return "none"

    def classify_document(self, filename, content_prefix):
        if "story" in content_prefix.lower() or "novel" in filename.lower():
            return "novel"
        return "default"

    def generate_persona_and_template(self, category):
        return {
            "persona_name": f"{category}-assistant",
            "persona_content": f"# {category.capitalize()} Assistant\nFake guidelines for {category}",
            "template_name": f"{category}-summary",
            "template_content": f"# {category.capitalize()} Summary\nFake structure for {category}",
        }


class FakeRAG:
    def __init__(self):
        self.docs = []
        self.facets = []

    def add_document(self, path, title, markdown, tags=None, section_path=None):
        self.docs.append((path, title, tags))

    def add_facets(self, path, title, facets, tags=None):
        self.facets.append((title, facets))


@pytest.fixture
def fake_services():
    llm = FakeLLM()
    rag = FakeRAG()
    return llm, rag


def _setup_vault(monkeypatch, tmp_path, profiles: dict[str, tuple[str, str]] | None = None):
    """Redirect pipeline paths to tmp and write the given profiles.

    `profiles` maps name → (persona, template). When None, writes the
    standard patent/paper/default trio.
    """
    scripture_dir = tmp_path / "Scripture"
    profiles_dir = scripture_dir / "Profiles"
    from_llm = tmp_path / "fromLingLing"
    scripture_dir.mkdir(exist_ok=True)

    monkeypatch.setattr("services.ingestion_pipeline.SCRIPTURE_DIR", scripture_dir)
    monkeypatch.setattr("services.ingestion_pipeline.PROFILES_DIR", profiles_dir)
    monkeypatch.setattr(
        "services.ingestion_pipeline.PROFILES_PENDING_DIR", profiles_dir / "_pending"
    )
    # queue_new_profile (the only FROM_LLM_DIR user here) moved to the
    # routing unit in P2d — the pipeline module no longer has that name.
    monkeypatch.setattr("services.ingest.profile_routing.FROM_LLM_DIR", from_llm)
    monkeypatch.setattr("services.ingestion_pipeline.update_wiki_index", MagicMock())
    monkeypatch.setattr("services.ingestion_pipeline.PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr("services.ingestion_pipeline.INDEX_FILE", tmp_path / "index.md")
    monkeypatch.setattr("services.ingestion_pipeline.TEMPLATES_DIR", tmp_path / "Templates")
    monkeypatch.setattr(
        "services.ingestion_pipeline.INGEST_FAILURE_STATE_FILE",
        tmp_path / "Database" / "ingest_failure_state.json",
    )
    monkeypatch.setattr(
        "services.ingestion_pipeline.INGEST_ARTIFACT_BACKUP_DIR",
        tmp_path / "Backups" / "artifact_patches",
    )
    monkeypatch.setattr(
        "services.ingestion_pipeline.INGEST_ARTIFACT_PENDING_DIR",
        tmp_path / "_pending" / "LearningArtifacts",
    )

    if profiles is None:
        profiles = {
            "patent": ("patent-expert", "patent-rpt"),
            "paper": ("researcher", "research-rpt"),
            "default": ("default-document-architect", "universal-document-template"),
        }
    profiles_dir.mkdir(parents=True, exist_ok=True)
    for name, (persona, template) in profiles.items():
        (profiles_dir / f"{name}.md").write_text(
            render_profile_markdown(
                persona=persona,
                template=template,
                description=name,
                applicable_when=f"{name} documents",
            ),
            encoding="utf-8",
        )
    return profiles_dir, from_llm


class TestProfileRouting:
    def test_short_doc_patent_selected_by_content(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        _setup_vault(monkeypatch, tmp_path)

        content = "This describes an invention.\n\nClaims\n1. A device...\n\nPrior Art\nExisting systems..."
        source_file = tmp_path / "MyPatent.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)

        assert len(llm.generate_entity_page_calls) == 1
        call = llm.generate_entity_page_calls[0]
        assert call["persona"] == "patent-expert"
        assert call["forced_template"] == "patent-rpt"
        # Selection was closed-choice over the registered profiles.
        assert {o["name"] for o in llm.select_profile_calls[0]["options"]} == {
            "patent",
            "paper",
            "default",
        }

    def test_short_doc_patent_selected_by_filename(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        _setup_vault(monkeypatch, tmp_path)

        content = "Standard paper."
        source_file = tmp_path / "US9876543.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)

        call = llm.generate_entity_page_calls[0]
        assert call["persona"] == "patent-expert"
        assert call["forced_template"] == "patent-rpt"

    def test_frontmatter_persona_template_overrides_win(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        _setup_vault(monkeypatch, tmp_path)

        content = "---\ndocument_type: paper\nsynthesis_persona: super-analyst\nsynthesis_template: custom-rpt\n---\nSome body"
        source_file = tmp_path / "paper.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)

        call = llm.generate_entity_page_calls[0]
        assert call["persona"] == "super-analyst"
        assert call["forced_template"] == "custom-rpt"
        # Full override: no LLM routing call needed.
        assert llm.select_profile_calls == []

    def test_frontmatter_profile_name_skips_llm_selection(
        self, monkeypatch, tmp_path, fake_services
    ):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        _setup_vault(monkeypatch, tmp_path)

        content = "---\nprofile: paper\n---\nWhatever body"
        source_file = tmp_path / "anything.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)

        call = llm.generate_entity_page_calls[0]
        assert call["persona"] == "researcher"
        assert call["forced_template"] == "research-rpt"
        assert llm.select_profile_calls == []

    def test_document_type_matching_profile_skips_llm_selection(
        self, monkeypatch, tmp_path, fake_services
    ):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        _setup_vault(monkeypatch, tmp_path)

        content = "---\ndocument_type: patent\n---\nBody"
        source_file = tmp_path / "doc.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)

        call = llm.generate_entity_page_calls[0]
        assert call["persona"] == "patent-expert"
        assert llm.select_profile_calls == []

    def test_long_doc_parts_translator_synthesis_profile(
        self, monkeypatch, tmp_path, fake_services
    ):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 20)
        _setup_vault(monkeypatch, tmp_path)
        monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", False)

        content = "US Patent Document\n\nClaims\nPrior Art\n" + ("Part content section. " * 100)
        source_file = tmp_path / "US_Patent_Long.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)

        assert len(llm.generate_entity_page_calls) > 1
        for call in llm.generate_entity_page_calls:
            assert call["persona"] == "translator"
            assert call["forced_template"] == "translation-rpt"

        assert len(llm.generate_synthesis_calls) == 1
        syn_call = llm.generate_synthesis_calls[0]
        assert syn_call["persona"] == "patent-expert"
        assert syn_call["template"] == "patent-rpt"


class TestRoutingTraceAndTemplateStamp:
    def test_routing_decision_recorded_as_artifact(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        _setup_vault(monkeypatch, tmp_path)

        content = "Claims\nPrior Art\nAn invention."
        source_file = tmp_path / "MyPatent.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)

        # llm.trace_store is a MagicMock — inspect the routing artifact call.
        routing_calls = [
            c
            for c in llm.trace_store.record_artifact.call_args_list
            if c.kwargs.get("artifact_type") == "routing_decision"
        ]
        assert len(routing_calls) == 1
        meta = routing_calls[0].kwargs["metadata"]
        assert meta["layer"] == "llm_selection"
        assert meta["profile"] == "patent"
        assert meta["fellback_to_default"] is False

    def test_fallback_layer_recorded(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        _setup_vault(monkeypatch, tmp_path)

        content = "A story about nothing."
        source_file = tmp_path / "misc.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)

        routing_calls = [
            c
            for c in llm.trace_store.record_artifact.call_args_list
            if c.kwargs.get("artifact_type") == "routing_decision"
        ]
        meta = routing_calls[0].kwargs["metadata"]
        assert meta["layer"] == "default_profile"
        assert meta["fellback_to_default"] is True

    def test_generated_page_stamped_with_template_version(
        self, monkeypatch, tmp_path, fake_services
    ):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        _setup_vault(monkeypatch, tmp_path)

        templates_dir = tmp_path / "Templates"
        templates_dir.mkdir(exist_ok=True)
        (templates_dir / "patent-rpt.md").write_text(
            "---\nversion: 3\n---\n\n# Patent Report Template\n", encoding="utf-8"
        )

        content = "Claims\nPrior Art\nAn invention."
        source_file = tmp_path / "MyPatent.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)

        pages = list((tmp_path / "pages").rglob("*.md"))
        assert len(pages) == 1
        from core.parser import parse_markdown_metadata

        meta = parse_markdown_metadata(pages[0].read_text(encoding="utf-8"))
        assert meta["template"] == "patent-rpt"
        assert meta["template_version"] == 3


class TestFacetIndexWiring:
    def test_short_doc_facets_are_deferred_to_idle_backfill(
        self, monkeypatch, tmp_path, fake_services
    ):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        _setup_vault(monkeypatch, tmp_path)

        content = "Claims\nPrior Art\nAn invention."
        source_file = tmp_path / "MyPatent.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)

        assert rag.facets == []
        assert llm.generate_part_digest_calls == []

    def test_long_doc_facets_are_deferred_to_idle_backfill(
        self, monkeypatch, tmp_path, fake_services
    ):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 20)
        _setup_vault(monkeypatch, tmp_path)
        monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", False)

        content = "US Patent Document\n\nClaims\nPrior Art\n" + ("Part content section. " * 100)
        source_file = tmp_path / "US_Patent_Long.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)

        # The persisted digest appendix is the idle FacetBackfillPump's input;
        # no optional facet writes remain on the critical ingest path.
        assert rag.facets == []
        part_pages = list((tmp_path / "pages").rglob("*(Part *).md"))
        assert part_pages
        assert all("## 🧩 Part Digest Appendix" in p.read_text() for p in part_pages)


class TestLongDocumentCommitAndInlineDigest:
    def _long_pipeline(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 40)
        _setup_vault(monkeypatch, tmp_path)
        monkeypatch.setattr("services.ingestion_pipeline.SYNTHESIS_CRITIQUE_ENABLED", False)
        return pipeline, llm, rag

    def test_inline_digest_uses_one_llm_call_per_part(self, monkeypatch, tmp_path, fake_services):
        pipeline, llm, _ = self._long_pipeline(monkeypatch, tmp_path, fake_services)
        original = llm.generate_entity_page

        def with_digest(*args, **kwargs):
            value = original(*args, **kwargs)
            value["part_digest"] = {
                "thesis": "Inline thesis",
                "key_points": ["An inline key point with enough detail."],
            }
            return value

        llm.generate_entity_page = with_digest
        content = "Long source. " * 100
        source = tmp_path / "inline.md"
        source.write_text(content)

        result = pipeline.ingest_markdown(content, source)

        assert result.archivable is True
        assert llm.generate_part_digest_calls == []
        assert all(m.get("inline_digest") for m in result.metrics["part_metrics"])
        assert "part_digest" in llm.generate_entity_page_calls[0]["context_hint"]
        timing_calls = [
            call
            for call in llm.trace_store.record_artifact.call_args_list
            if call.kwargs.get("artifact_type") == "ingestion_part_timing"
        ]
        assert len(timing_calls) == result.expected_parts
        assert timing_calls[0].kwargs["metadata"]["status"] == "complete"
        assert timing_calls[0].kwargs["metadata"]["inline_digest"] is True

    def test_malformed_inline_digest_falls_back(self, monkeypatch, tmp_path, fake_services):
        pipeline, llm, _ = self._long_pipeline(monkeypatch, tmp_path, fake_services)
        original = llm.generate_entity_page

        def with_bad_digest(*args, **kwargs):
            value = original(*args, **kwargs)
            value["part_digest"] = {"thesis": "", "key_points": []}
            return value

        llm.generate_entity_page = with_bad_digest
        content = "Long source. " * 100
        source = tmp_path / "fallback.md"
        source.write_text(content)

        result = pipeline.ingest_markdown(content, source)

        assert result.archivable is True
        assert len(llm.generate_part_digest_calls) == result.expected_parts
        assert not any(m.get("inline_digest") for m in result.metrics["part_metrics"])

    def test_resumed_parts_do_not_emit_fake_latency_samples(
        self, monkeypatch, tmp_path, fake_services
    ):
        pipeline, llm, _ = self._long_pipeline(monkeypatch, tmp_path, fake_services)
        content = "Long source. " * 100
        source = tmp_path / "resume.md"
        source.write_text(content)
        first = pipeline.ingest_markdown(content, source)
        assert first.archivable is True

        llm.trace_store.reset_mock()
        second = pipeline.ingest_markdown(content, source)

        assert second.archivable is True
        assert second.metrics["resumed_part_count"] == second.expected_parts
        assert second.metrics["resumed_parts"] == list(range(1, second.expected_parts + 1))
        assert second.metrics["part_metrics"] == []
        timing_calls = [
            call
            for call in llm.trace_store.record_artifact.call_args_list
            if call.kwargs.get("artifact_type") == "ingestion_part_timing"
        ]
        assert timing_calls == []

    def test_failed_part_blocks_synthesis_and_archival(self, monkeypatch, tmp_path, fake_services):
        pipeline, llm, _ = self._long_pipeline(monkeypatch, tmp_path, fake_services)
        original = pipeline.ingest_to_wiki
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                from services.ingest.result import IngestResult

                return IngestResult.failure("generate", RuntimeError("poison part"))
            return original(*args, **kwargs)

        pipeline.ingest_to_wiki = fail_second
        content = "Long source. " * 100
        source = tmp_path / "partial.md"
        source.write_text(content)

        result = pipeline.ingest_markdown(content, source)

        assert result.status == "partial"
        assert result.archivable is False
        assert result.failed_parts[0]["part"] == 2
        assert llm.generate_synthesis_calls == []
        trace_calls = [
            call
            for call in llm.trace_store.record_artifact.call_args_list
            if call.kwargs.get("artifact_type") == "ingestion_run"
        ]
        assert trace_calls
        assert trace_calls[-1].kwargs["metadata"]["metrics"]["stage_ms"]["distill_parts"] >= 0

    def test_reasoning_leak_cannot_be_redeemed_by_fallback_digest(
        self, monkeypatch, tmp_path, fake_services
    ):
        pipeline, llm, _ = self._long_pipeline(monkeypatch, tmp_path, fake_services)
        original = llm.generate_entity_page

        def poisoned(*args, **kwargs):
            value = original(*args, **kwargs)
            value["content"] = (
                "Source Material: harmonic numbers\nGoal: translate faithfully\n"
                "Constraints: preserve equations\nDraft"
            )
            return value

        llm.generate_entity_page = poisoned
        content = "Long source. " * 100
        source = tmp_path / "reasoning-leak.md"
        source.write_text(content)

        result = pipeline.ingest_markdown(content, source)

        assert result.status == "partial"
        assert llm.generate_part_digest_calls == []
        assert not list((tmp_path / "pages").rglob("*(Part *).md"))
        assert all(item["stage"] == "entity_quality" for item in result.failed_parts)

    def test_unclosed_fence_gets_one_immediate_persisted_reroll(
        self, monkeypatch, tmp_path, fake_services
    ):
        pipeline, llm, _ = self._long_pipeline(monkeypatch, tmp_path, fake_services)
        original = llm.generate_entity_page
        poisoned = False

        def fail_once(*args, **kwargs):
            nonlocal poisoned
            value = original(*args, **kwargs)
            if not poisoned:
                poisoned = True
                value["content"] = "```mermaid\ngraph TD\nA --> B"
            return value

        llm.generate_entity_page = fail_once
        content = "Long source. " * 100
        source = tmp_path / "reroll.md"
        source.write_text(content)

        result = pipeline.ingest_markdown(content, source)

        assert result.archivable is True
        assert len(llm.generate_entity_page_calls) == result.expected_parts + 1
        failure_state = tmp_path / "Database" / "ingest_failure_state.json"
        assert '"failures": {}' in failure_state.read_text(encoding="utf-8")

    def test_rag_failure_leaves_pending_note_that_cannot_resume(
        self, monkeypatch, tmp_path, fake_services
    ):
        pipeline, llm, rag = self._long_pipeline(monkeypatch, tmp_path, fake_services)

        def fail_index(*args, **kwargs):
            raise RuntimeError("rag unavailable")

        rag.add_document = fail_index
        content = "Long source. " * 100
        source = tmp_path / "rag-fail.md"
        source.write_text(content)

        result = pipeline.ingest_markdown(content, source)

        assert result.status == "partial"
        first_page = tmp_path / "pages" / "rag-fail" / "rag-fail (Part 1).md"
        assert first_page.exists()
        assert "ingest_status: pending_index" in first_page.read_text(encoding="utf-8")
        assert pipeline._resume_part(first_page, pipeline.splitter.split_text(content)[0]) is None
        assert llm.generate_synthesis_calls == []

    def test_part_artifact_pipeline_overlaps_next_part_core_generation(
        self, monkeypatch, tmp_path, fake_services
    ):
        pipeline, llm, _ = self._long_pipeline(monkeypatch, tmp_path, fake_services)
        from core.config import settings

        monkeypatch.setattr(settings, "VISUAL_ROUTER_ENABLED", True)
        artifact_started = threading.Event()
        release_artifact = threading.Event()
        second_part_started = threading.Event()
        original_entity = llm.generate_entity_page

        def entity(*args, **kwargs):
            if len(llm.generate_entity_page_calls) >= 1:
                second_part_started.set()
            value = original_entity(*args, **kwargs)
            value["part_digest"] = {
                "thesis": "Inline thesis",
                "key_points": ["Enough detail for the inline digest."],
            }
            return value

        def blocking_artifact(*args, **kwargs):
            artifact_started.set()
            assert release_artifact.wait(timeout=5)
            return "## 🖼️ 學習輔助（mindmap）\n\n```mermaid\nmindmap\n  root((完成))\n```\n"

        llm.generate_entity_page = entity
        monkeypatch.setattr("services.learning_artifacts.maybe_artifact_section", blocking_artifact)
        content = "Long source. " * 100
        source = tmp_path / "parallel.md"
        source.write_text(content)
        outcome = {}

        worker = threading.Thread(
            target=lambda: outcome.setdefault("result", pipeline.ingest_markdown(content, source))
        )
        worker.start()
        assert artifact_started.wait(timeout=5)
        assert second_part_started.wait(timeout=5)
        release_artifact.set()
        worker.join(timeout=10)

        assert not worker.is_alive()
        assert outcome["result"].archivable is True
        part_one = tmp_path / "pages" / "parallel" / "parallel (Part 1).md"
        text = part_one.read_text(encoding="utf-8")
        assert text.index("## 🖼️ 學習輔助") < text.index("## 🔗 知識導航")

    def test_synthesis_failure_is_not_archivable(self, monkeypatch, tmp_path, fake_services):
        pipeline, _, _ = self._long_pipeline(monkeypatch, tmp_path, fake_services)
        pipeline._write_synthesis = MagicMock(side_effect=RuntimeError("provider down"))
        content = "Long source. " * 100
        source = tmp_path / "synthesis-fail.md"
        source.write_text(content)

        result = pipeline.ingest_markdown(content, source)

        assert result.status == "failed"
        assert result.stage == "synthesize"
        assert result.archivable is False

    def test_part_commit_failure_blocks_synthesis_and_archival(
        self, monkeypatch, tmp_path, fake_services
    ):
        pipeline, llm, _ = self._long_pipeline(monkeypatch, tmp_path, fake_services)
        original = pipeline._append_part_digest_to_note
        calls = 0

        def fail_second_commit(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            return original(*args, **kwargs)

        pipeline._append_part_digest_to_note = fail_second_commit
        content = "Long source. " * 100
        source = tmp_path / "commit-fail.md"
        source.write_text(content)

        result = pipeline.ingest_markdown(content, source)

        assert result.status == "partial"
        assert result.archivable is False
        assert result.failed_parts[0]["stage"] == "commit"
        assert "disk full" in result.failed_parts[0]["detail"]
        assert llm.generate_synthesis_calls == []


class TestDocTypeMigrationInPipeline:
    def test_empty_profiles_migrates_legacy_doctype(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        profiles_dir, _ = _setup_vault(monkeypatch, tmp_path, profiles={})

        scripture_dir = tmp_path / "Scripture"
        (scripture_dir / "DocType.md").write_text(
            "| Category | Persona | Template | Description |\n"
            "| --- | --- | --- | --- |\n"
            "| patent | patent-expert | patent-rpt | Patent report |\n",
            encoding="utf-8",
        )

        pm = pipeline.load_profiles()
        assert pm.get("patent") is not None
        assert (profiles_dir / "patent.md").exists()


class TestUnknownCategoryPendingFlow:
    def test_unknown_category_queues_pending_and_uses_default(
        self, monkeypatch, tmp_path, fake_services
    ):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        profiles_dir, from_llm = _setup_vault(monkeypatch, tmp_path)

        content = "This is a story about ling-ling the cute assistant in a fantasy land."
        source_file = tmp_path / "novel_clipping.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)

        # Draft bundle queued for review — NOT activated.
        bundle = profiles_dir / "_pending" / "novel"
        assert (bundle / "novel-assistant.md").exists()
        assert (bundle / "novel-summary.md").exists()
        assert (bundle / "novel.md").exists()
        assert not (profiles_dir / "novel.md").exists()

        # Review notice dropped in fromLingLing.
        assert any("novel" in p.name for p in from_llm.glob("*.md"))

        # This run used the default profile, not the unreviewed draft.
        call = llm.generate_entity_page_calls[0]
        assert call["persona"] == "default-document-architect"
        assert call["forced_template"] == "universal-document-template"

    def test_unknown_category_without_default_falls_back_to_settings(
        self, monkeypatch, tmp_path, fake_services
    ):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        _setup_vault(monkeypatch, tmp_path, profiles={"patent": ("patent-expert", "patent-rpt")})

        from core.config import settings

        monkeypatch.setattr(settings, "AGENT_ROLE", "assistant")
        monkeypatch.setattr(settings, "USE_TEMPLATE", None)

        content = "A story about nothing in particular."
        source_file = tmp_path / "novel_thing.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)

        call = llm.generate_entity_page_calls[0]
        assert call["persona"] == "assistant"
        assert call["forced_template"] == "wiki-note"

    def test_pending_draft_not_requeued(self, monkeypatch, tmp_path, fake_services):
        llm, rag = fake_services
        pipeline = IngestionPipeline(llm, rag)
        monkeypatch.setattr(pipeline.splitter, "chunk_size", 10000)
        profiles_dir, from_llm = _setup_vault(monkeypatch, tmp_path)

        content = "This is a story about ling-ling."
        source_file = tmp_path / "novel_clipping.md"
        source_file.write_text(content)

        pipeline.ingest_markdown(content, source_file)
        notices_after_first = len(list(from_llm.glob("*.md")))
        pipeline.ingest_markdown(content, source_file)

        # Second ingest of the same unknown category must not duplicate the bundle/notice.
        assert len(list(from_llm.glob("*.md"))) == notices_after_first
