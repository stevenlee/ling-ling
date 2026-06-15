import sys
from pathlib import Path
import pytest
import hashlib
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from services.rag_manager import (
    RAGManager,
    OllamaEmbeddingFunction,
    GeminiEmbeddingFunction,
    get_effective_model_name
)
import services.rag_manager as rag_module
import importlib
migration_002 = importlib.import_module("maintenance.migrations.002_expand_metadata_keys")


@pytest.fixture(autouse=True)
def stable_embedding_env(monkeypatch):
    """Keep these tests independent from the developer's .env provider."""
    monkeypatch.setattr(rag_module, "EMBEDDING_PROVIDER", "local")
    monkeypatch.setattr(rag_module, "EMBEDDING_MODEL", None)
    monkeypatch.setattr(rag_module, "EMBEDDING_CACHE_ENABLED", False)
    monkeypatch.setattr(rag_module, "RERANKER_ENABLED", False)
    monkeypatch.setattr(rag_module, "HYBRID_RETRIEVAL_ENABLED", False)


class TestEmbeddingFunctions:
    @patch("requests.post")
    def test_ollama_embedding_function(self, mock_post):
        # Mock requests response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "embeddings": [[0.1, 0.2] * 384, [0.3, 0.4] * 384]
        }
        mock_post.return_value = mock_response

        fn = OllamaEmbeddingFunction(api_base="http://test-ollama", model_name="nomic-embed-text")
        embeddings = fn(["hello", "world"])
        
        assert len(embeddings) == 2
        assert len(embeddings[0]) == 768
        mock_post.assert_called_once_with(
            "http://test-ollama/api/embed",
            json={"model": "nomic-embed-text", "input": ["hello", "world"]},
            timeout=60
        )

    @patch("requests.post")
    def test_ollama_batch_nan_isolates_bad_item(self, mock_post):
        # bge-m3 NaN on one input 500s the whole batch; we isolate per-item so
        # the good vectors still index and the bad one gets a placeholder.
        good = [0.5] * 1024

        def resp_for(*args, **kwargs):
            inp = kwargs["json"]["input"]
            r = MagicMock()
            if len(inp) > 1:                       # the batch call → NaN 500
                r.status_code = 500
                r.text = '{"error":"failed to encode response: json: unsupported value: NaN"}'
                return r
            if inp[0] == "bad":                    # the offending single input
                r.status_code = 500
                r.text = "NaN"
                return r
            r.status_code = 200
            r.json.return_value = {"embeddings": [good]}
            return r

        mock_post.side_effect = resp_for
        fn = OllamaEmbeddingFunction(api_base="http://t", model_name="bge-m3", max_chars=0)
        out = fn(["ok1", "bad", "ok2"])   # chromadb coerces to ndarray → element-wise asserts
        assert len(out) == 3
        assert out[0][0] == 0.5 and out[2][0] == 0.5 and out[0][-1] == 0.5
        assert len(out[1]) == 1024 and out[1][0] == 1.0 and float(sum(out[1][1:])) == 0.0  # unit placeholder

    @patch("requests.post")
    def test_ollama_nan_in_200_response_isolated(self, mock_post):
        good = [0.5] * 1024
        nan_vec = [float("nan")] * 1024

        def resp_for(*args, **kwargs):
            inp = kwargs["json"]["input"]
            r = MagicMock()
            r.status_code = 200
            if len(inp) > 1:                       # batch returns a NaN vector
                r.json.return_value = {"embeddings": [good, nan_vec]}
            else:
                r.json.return_value = {"embeddings": [nan_vec if inp[0] == "bad" else good]}
            return r

        mock_post.side_effect = resp_for
        fn = OllamaEmbeddingFunction(api_base="http://t", model_name="bge-m3", max_chars=0)
        out = fn(["ok", "bad"])
        assert out[0][0] == 0.5 and out[0][-1] == 0.5
        assert out[1][0] == 1.0 and float(sum(out[1][1:])) == 0.0

    @patch("requests.post")
    def test_ollama_all_fail_raises(self, mock_post):
        # Every item failing on its own is an outage, not an input-specific NaN
        # — must raise (not silently fill placeholders).
        r = MagicMock()
        r.status_code = 500
        r.text = "Internal Server Error"
        mock_post.return_value = r
        fn = OllamaEmbeddingFunction(api_base="http://t", model_name="bge-m3", max_chars=0)
        import pytest
        with pytest.raises(RuntimeError):
            fn(["a", "b"])

    def test_gemini_embedding_function(self):
        # Mock google-genai Client structure
        mock_client_class = MagicMock()
        mock_client = MagicMock()
        mock_response = MagicMock()
        
        mock_emb_1 = MagicMock()
        mock_emb_1.values = [0.1] * 768
        mock_emb_2 = MagicMock()
        mock_emb_2.values = [0.2] * 768
        mock_response.embeddings = [mock_emb_1, mock_emb_2]
        
        mock_client.models.embed_content.return_value = mock_response
        mock_client_class.return_value = mock_client

        with patch("google.genai.Client", mock_client_class):
            fn = GeminiEmbeddingFunction(api_key="test-key", model_name="text-embedding-004")
            embeddings = fn(["hello", "world"])
            
            assert len(embeddings) == 2
            assert len(embeddings[0]) == 768
            mock_client.models.embed_content.assert_called_once_with(
                model="text-embedding-004",
                contents=["hello", "world"]
            )


class TestRAGManagerLogic:
    def test_get_effective_model_name(self):
        assert get_effective_model_name("local", None) == "all-MiniLM-L6-v2"
        assert get_effective_model_name("ollama", None) == "nomic-embed-text"
        assert get_effective_model_name("ollama", "custom-ollama") == "custom-ollama"
        assert get_effective_model_name("gemini", None) == "text-embedding-004"
        assert get_effective_model_name("gemini", "custom-gemini") == "custom-gemini"

    def test_sanitize_tag_key(self):
        # Normalized tags
        assert RAGManager._sanitize_tag_key("China/History") == "tag_china_history"
        assert RAGManager._sanitize_tag_key("China History") == "tag_china-history"
        assert RAGManager._sanitize_tag_key("Completed") == "tag_completed"
        # Pure punctuation tag should sanitize to empty string
        assert RAGManager._sanitize_tag_key("!!!") == ""

    def test_build_where_clause(self):
        manager = RAGManager(db_path="test_temp_db_where", skip_config_check=True)
        
        # Test tags only
        where = manager._build_where_clause(tags=["Completed", "History"])
        assert where == {
            "$and": [
                {"tag_completed": True},
                {"tag_history": True}
            ]
        }
        
        # Test section path only
        where_sec = manager._build_where_clause(section_path=["Chapter 1", "Introduction"])
        assert where_sec == {
            "$and": [
                {"section_l1": "chapter 1"},
                {"section_l2": "introduction"}
            ]
        }

        # Test combined with caller custom where filter
        where_combined = manager._build_where_clause(
            tags=["Completed"],
            section_path=["Chapter 1"],
            where_filter={"doc_id": "test_doc"}
        )
        assert where_combined == {
            "$and": [
                {"tag_completed": True},
                {"section_l1": "chapter 1"},
                {"doc_id": "test_doc"}
            ]
        }

        # Test empty input
        assert manager._build_where_clause() is None

        # Cleanup test DB
        manager.wipe_collection()
        import shutil
        shutil.rmtree(manager.db_dir, ignore_errors=True)

    def test_fail_fast_mismatch_guard(self, tmpdir):
        # Create a mock database collection with a mismatching configuration
        db_path = Path(tmpdir) / "test_mismatch_db"
        
        # Initialize RAGManager with local default
        with patch("services.rag_manager.EMBEDDING_PROVIDER", "local"), \
             patch("services.rag_manager.EMBEDDING_MODEL", None):
            manager = RAGManager(db_path=str(db_path))
            # Insert a dummy chunk to count > 0
            manager.collection.add(
                ids=["chunk_1"],
                documents=["test content"],
                metadatas=[{"title": "test_doc", "doc_id": "abc"}]
            )
            # metadata is currently: provider=local, model=all-MiniLM-L6-v2, dimension=384
            assert manager.collection.metadata["embedding_provider"] == "local"

        # Now, try initializing with ollama. It should raise ValueError
        with patch("services.rag_manager.EMBEDDING_PROVIDER", "ollama"), \
             patch("services.rag_manager.EMBEDDING_MODEL", "nomic-embed-text"), \
             patch("services.rag_manager.OllamaEmbeddingFunction") as mock_ollama_ef:
            
            # Setup mock Ollama function return dimensions
            mock_instance = MagicMock()
            mock_instance.return_value = [[0.1] * 768]
            mock_ollama_ef.return_value = mock_instance

            with pytest.raises(ValueError) as exc_info:
                RAGManager(db_path=str(db_path))
            
            assert "Embedding configuration mismatch detected!" in str(exc_info.value)

        # Cleanup
        import shutil
        shutil.rmtree(db_path, ignore_errors=True)

    def test_doc_id_generation(self):
        # Ensure SHA-256 hash is collision-free and stable
        filepath_1 = Path("pages/a/b.md")
        filepath_2 = Path("pages/a_b.md")
        
        doc_id_1 = RAGManager._get_doc_id(filepath_1)
        doc_id_2 = RAGManager._get_doc_id(filepath_2)
        
        assert doc_id_1 != doc_id_2
        assert len(doc_id_1) == 64  # hex representation of sha256

    def test_skip_config_check_keeps_embedding_function(self, tmp_path, monkeypatch):
        class FakeCollection:
            metadata = {
                "embedding_provider": "local",
                "embedding_model": "all-MiniLM-L6-v2",
                "embedding_dimension": 384,
            }

            def count(self):
                return 1

        class FakeClient:
            def __init__(self):
                self.embedding_function = None

            def get_collection(self, name, embedding_function=None):
                self.embedding_function = embedding_function
                return FakeCollection()

        fake_client = FakeClient()
        monkeypatch.setattr(rag_module.chromadb, "PersistentClient", lambda path: fake_client)

        manager = RAGManager(db_path=str(tmp_path), skip_config_check=True)

        assert fake_client.embedding_function is manager.ef

    def test_metadata_check_trusts_matching_empty_collection_metadata(self):
        class FakeCollection:
            metadata = {
                "embedding_provider": "local",
                "embedding_model": "all-MiniLM-L6-v2",
                "embedding_dimension": 384,
            }

            def count(self):
                return 0

            def modify(self, metadata):
                raise AssertionError("matching metadata should not be rewritten")

        manager = RAGManager.__new__(RAGManager)
        manager.collection = FakeCollection()
        manager.ef = MagicMock(side_effect=AssertionError("embedding probe should not run"))

        manager._check_metadata_mismatch()

        manager.ef.assert_not_called()

    def test_delete_document_distinguishes_title_from_doc_id(self):
        class FakeCollection:
            def __init__(self):
                self.deleted = []

            def delete(self, where):
                self.deleted.append(where)

        class FakeBM25:
            def mark_dirty(self):
                pass

        manager = RAGManager.__new__(RAGManager)
        manager.collection = FakeCollection()
        manager._bm25 = FakeBM25()

        manager.delete_document("Human Title")
        assert manager.collection.deleted == [{"title": "Human Title"}]

        manager.collection.deleted.clear()
        doc_id = "a" * 64
        manager.delete_document(doc_id)
        assert manager.collection.deleted == [{"doc_id": doc_id}]

    def test_add_document_cleans_doc_id_and_only_legacy_title_chunks(self, tmp_path):
        # Audit A2: legacy title cleanup must be SCOPED to doc_id-less chunks.
        # A same-title chunk that carries a (different) doc_id belongs to
        # another document and must survive. The old blanket delete-by-title
        # wiped it; the test now pins the corrected scoped behavior.
        sibling_doc_id = "f" * 64  # an unrelated document sharing the title

        class FakeCollection:
            def __init__(self):
                self.deleted = []        # where-clause deletes
                self.deleted_ids = []    # id-list deletes
                self.upserts = []

            def get(self, **kwargs):
                # Two same-title chunks: one legacy (no doc_id), one sibling.
                return {
                    "ids": ["legacy_chunk", "sibling_chunk"],
                    "metadatas": [
                        {"title": "Legacy Title"},
                        {"title": "Legacy Title", "doc_id": sibling_doc_id},
                    ],
                }

            def delete(self, where=None, ids=None):
                if ids is not None:
                    self.deleted_ids.extend(ids)
                if where is not None:
                    self.deleted.append(where)

            def upsert(self, **kwargs):
                self.upserts.append(kwargs)

        class FakeBM25:
            def mark_dirty(self):
                pass

        class FakeSplitter:
            def split_text_with_spans(self, text):
                return [{"text": text, "start": 0, "end": len(text)}]

        manager = RAGManager.__new__(RAGManager)
        manager.collection = FakeCollection()
        manager._bm25 = FakeBM25()
        manager.splitter = FakeSplitter()

        path = tmp_path / "Legacy Title.md"
        manager.add_document(path, "Legacy Title", "fresh content", tags=["AI"], strict=True)

        expected_doc_id = RAGManager._get_doc_id(path)
        # Own chunks cleared by doc_id.
        assert {"doc_id": expected_doc_id} in manager.collection.deleted
        # Blanket delete-by-title must NOT happen anymore.
        assert {"title": "Legacy Title"} not in manager.collection.deleted
        # Only the doc_id-less legacy chunk is removed; the sibling survives.
        assert manager.collection.deleted_ids == ["legacy_chunk"]
        assert len(manager.collection.upserts) == 1


class TestMigration002:
    def test_migration_expansion(self, tmpdir):
        db_path = Path(tmpdir) / "test_migration_db"
        
        # Initialize legacy manager
        with patch("core.config.EMBEDDING_PROVIDER", "local"), \
             patch("core.config.EMBEDDING_MODEL", None):
            manager = RAGManager(db_path=str(db_path))
            
            # Add a legacy formatted metadata document
            manager.collection.add(
                ids=["legacy_1"],
                documents=["test migration prose"],
                metadatas=[{
                    "title": "Legacy Doc",
                    "source": "Legacy.md",
                    "tags": ",Completed,History,",
                    "section_path": ">Chapter 1>Introduction>"
                }]
            )
            
            # Run migration 002
            stats = migration_002.run(manager)
            assert stats["chunks_upgraded"] == 1
            
            # Verify the upgraded properties
            updated_results = manager.collection.get(ids=["legacy_1"], include=["metadatas"])
            meta = updated_results["metadatas"][0]
            
            # Boolean tags
            assert meta.get("tag_completed") is True
            assert meta.get("tag_history") is True
            assert meta.get("tags") == ",completed,history,"
            
            # Section Levels
            assert meta.get("section_depth") == 2
            assert meta.get("section_l1") == "chapter 1"
            assert meta.get("section_l2") == "introduction"
            assert meta.get("section_l3") == ""
            assert meta.get("section_path_full") == "chapter 1 > introduction"
            
            # doc_id is resolved and generated
            assert "doc_id" in meta
            assert len(meta["doc_id"]) == 64
            assert meta["source_path"] == "Legacy.md"

        # Cleanup
        import shutil
        shutil.rmtree(db_path, ignore_errors=True)


class TestRAGExplainMode:
    def test_query_notes_records_retrieval_breakdown_and_event(self, tmpdir):
        db_path = Path(tmpdir) / "test_explain_db"
        from services.trace_store import TraceStore
        trace_db = db_path / "trace.sqlite"
        trace_store = TraceStore(db_path=trace_db)

        # Patch EMBEDDING_CACHE_ENABLED and other provider settings
        with patch("services.rag_manager.EMBEDDING_PROVIDER", "local"), \
             patch("services.rag_manager.EMBEDDING_MODEL", None), \
             patch("services.rag_manager.EMBEDDING_CACHE_ENABLED", False):
            manager = RAGManager(db_path=str(db_path))
            manager.trace_store = trace_store

            # Add document
            manager.collection.add(
                ids=["chunk_1"],
                documents=["test explain mode content"],
                metadatas=[{"title": "test_doc", "doc_id": "abc"}]
            )

            # Query with trace
            with trace_store.run(intent="test_rag_explain", agent="TestAgent") as run_id:
                results = manager.query_notes("explain mode", top_k=1)
                trace_ids = trace_store.current_trace_ids()

            assert len(results) == 1
            res = results[0]
            assert "retrieval_breakdown" in res
            breakdown = res["retrieval_breakdown"]
            assert breakdown["vector_rank"] == 1
            assert "vector_distance" in breakdown
            assert "vector" in breakdown["passed_layers"]
            assert breakdown["mmr_selected"] is False
            assert "mmr" not in breakdown["passed_layers"]

            # Verify SQLite event
            conn = sqlite3_connect = trace_store._connect()
            try:
                event = conn.execute("SELECT * FROM retrieval_events WHERE run_id = ?", (run_id,)).fetchone()
            finally:
                conn.close()

            assert event is not None
            assert event["query_text"] == "explain mode"
            assert event["top_k"] == 1
            assert "hybrid" in event["options_json"]
            import json
            results_json = json.loads(event["results_json"])
            assert len(results_json) == 1
            assert results_json[0]["id"] == "chunk_1"
            assert results_json[0]["retrieval_breakdown"]["vector_rank"] == 1
            assert results_json[0]["retrieval_breakdown"]["mmr_selected"] is False

        # Cleanup
        import shutil
        shutil.rmtree(db_path, ignore_errors=True)

    def test_extra_queries_trigger_rrf_fusion_and_tag_candidates(self, tmpdir):
        """extra_queries (the cross-lingual mechanism) must fold the variant's
        candidates into the pool and run RRF fusion — even with hybrid off.
        Asserted via the mechanism, not embedding-model ranking quirks: with
        no extra_queries and hybrid off, no fusion runs (rrf_score is None);
        with extra_queries, fusion runs (rrf_score populated) and a candidate
        reached via the variant is tagged 'vector_xlingual'."""
        db_path = Path(tmpdir) / "test_xlingual_db"
        with patch("services.rag_manager.EMBEDDING_PROVIDER", "local"), \
             patch("services.rag_manager.EMBEDDING_MODEL", None), \
             patch("services.rag_manager.EMBEDDING_CACHE_ENABLED", False), \
             patch("services.rag_manager.HYBRID_RETRIEVAL_ENABLED", False):
            manager = RAGManager(db_path=str(db_path))
            manager.collection.add(
                ids=["a", "b"],
                documents=[
                    "The cat sat on the warm mat in the sunny kitchen.",
                    "Quantum chromodynamics describes the strong force between quarks.",
                ],
                metadatas=[{"title": "cat_doc", "doc_id": "A"}, {"title": "physics_doc", "doc_id": "B"}],
            )

            # Baseline: no fusion (hybrid off, no variants) → rrf_score stays None.
            plain = manager.query_notes("kitchen cat mat", top_k=2)
            assert all(r["retrieval_breakdown"]["rrf_score"] is None for r in plain)

            # With an explicit variant matching the physics doc, fusion runs.
            fused = manager.query_notes(
                "kitchen cat mat", top_k=2,
                extra_queries=["quantum chromodynamics quarks strong force"],
            )
            by_id = {r["id"]: r for r in fused}
            assert "b" in by_id, "variant-reached doc should be in the fused pool"
            assert by_id["b"]["retrieval_breakdown"]["rrf_score"] is not None
            assert "vector_xlingual" in by_id["b"]["retrieval_breakdown"]["passed_layers"]

        import shutil
        shutil.rmtree(db_path, ignore_errors=True)

    def test_cross_lingual_uses_injected_translator(self, tmpdir):
        """cross_lingual=True with a wired translator expands the query; the
        translated variant's candidate enters the pool."""
        db_path = Path(tmpdir) / "test_xlingual_translator_db"
        with patch("services.rag_manager.EMBEDDING_PROVIDER", "local"), \
             patch("services.rag_manager.EMBEDDING_MODEL", None), \
             patch("services.rag_manager.EMBEDDING_CACHE_ENABLED", False), \
             patch("services.rag_manager.HYBRID_RETRIEVAL_ENABLED", False), \
             patch("services.rag_manager.CROSS_LINGUAL_TARGET_LANGS", ["en", "zh"]):
            manager = RAGManager(db_path=str(db_path))
            manager.collection.add(
                ids=["a", "b"],
                documents=[
                    "The cat sat on the warm mat in the sunny kitchen.",
                    "Quantum chromodynamics describes the strong force between quarks.",
                ],
                metadatas=[{"title": "cat_doc", "doc_id": "A"}, {"title": "physics_doc", "doc_id": "B"}],
            )
            # zh query → translator returns an English variant that matches doc B.
            manager.translator = lambda text, langs: {"en": "quantum chromodynamics quarks strong force"}
            fused = manager.query_notes("貓咪 廚房", top_k=2, cross_lingual=True)
            by_id = {r["id"]: r for r in fused}
            assert "b" in by_id
            assert "vector_xlingual" in by_id["b"]["retrieval_breakdown"]["passed_layers"]

        import shutil
        shutil.rmtree(db_path, ignore_errors=True)

    def test_base_agent_builds_rag_explain_appendix(self, tmpdir):
        from agents.base_agent import BaseAgent
        from services.trace_store import TraceStore

        db_path = Path(tmpdir) / "test_explain_agent_db"
        trace_db = db_path / "trace.sqlite"
        trace_store = TraceStore(db_path=trace_db)

        # Mock LLM and trace store
        mock_llm = MagicMock()
        mock_llm.trace_store = trace_store
        
        agent = BaseAgent(mock_llm)
        
        # Insert raw retrieval event
        with trace_store.run(intent="test", agent="TestAgent") as run_id:
            trace_store.record_retrieval_event(
                query_text="agent query",
                top_k=2,
                options={"hybrid": True, "rerank": False, "diversity": 0.0},
                results=[
                    {
                        "id": "c1",
                        "title": "Doc Title",
                        "source": "Doc.md",
                        "retrieval_breakdown": {
                            "passed_layers": ["vector", "bm25"],
                            "vector_distance": 0.1234,
                            "vector_rank": 1,
                            "bm25_score": 12.5,
                            "bm25_rank": 2,
                            "rrf_score": 0.016,
                            "rerank_score": None,
                            "rerank_rank": None,
                            "mmr_selected": True
                        }
                    }
                ]
            )

        appendix = agent._build_rag_explain_appendix(run_id)
        assert "RAG Retrieval Explanation Appendix" in appendix
        assert "### Query 1: `agent query`" in appendix
        assert "Doc Title" in appendix
        assert "Vector Dist: 0.1234" in appendix
        assert "BM25 Score: 12.50" in appendix
        assert "RRF: 0.0160" in appendix
        assert "MMR Selected" in appendix

        # Cleanup
        import shutil
        shutil.rmtree(db_path, ignore_errors=True)



# ── R7-C-2: public chunk accessors (insight no longer touches .collection) ──

class TestPublicChunkAccessors:
    def _rag(self):
        rag = RAGManager.__new__(RAGManager)

        class C:
            def __init__(self): self.calls = []
            def get(self, where=None, include=None, limit=None):
                self.calls.append({"where": where, "include": include, "limit": limit})
                return {"ids": [], "documents": [], "metadatas": []}

        rag.collection = C()
        return rag

    def test_all_chunks_default_and_metadata_only(self):
        rag = self._rag()
        rag.all_chunks()
        rag.all_chunks(include=("metadatas",))
        assert rag.collection.calls[0]["include"] == ["metadatas", "documents"]
        assert rag.collection.calls[0]["where"] is None
        assert rag.collection.calls[1]["include"] == ["metadatas"]

    def test_chunks_by_title_with_and_without_limit(self):
        rag = self._rag()
        rag.chunks_by_title("Doc A")
        rag.chunks_by_title("Doc B", limit=5)
        assert rag.collection.calls[0] == {"where": {"title": "Doc A"}, "include": ["metadatas", "documents"], "limit": None}
        assert rag.collection.calls[1]["where"] == {"title": "Doc B"}
        assert rag.collection.calls[1]["limit"] == 5


class TestPerDocumentCap:
    """RETRIEVAL_MAX_PER_DOC anti-flood cap (verified fix for the SpaceX-flood
    that buried NIST.AI.600-1 below the top-k)."""

    @staticmethod
    def _c(title, cid):
        return {"id": cid, "metadata": {"title": title, "source": f"{title}.md"}}

    def test_doc_key_strips_part_and_synthesis_suffix(self):
        k = RAGManager._doc_key
        assert k(self._c("NIST.AI.600-1 (Part 1)", "a")) == "NIST.AI.600-1"
        assert k(self._c("NIST.AI.600-1 (Synthesis)", "b")) == "NIST.AI.600-1"
        # distinct language editions stay distinct
        assert k(self._c("Siddhartha(EN) (Part 15)", "c")) == "Siddhartha(EN)"
        assert k(self._c("Siddhartha(DE) (Part 10)", "d")) == "Siddhartha(DE)"
        # mid-name parens preserved, only trailing stripped
        assert k(self._c("Hegel Volume 1 (of 3) (Part 97)", "e")) == "Hegel Volume 1 (of 3)"

    def test_cap_keeps_first_n_per_doc_in_order(self):
        cands = [
            self._c("SpaceX (Part 1)", "1"), self._c("SpaceX (Synthesis)", "2"),
            self._c("SpaceX (Part 9)", "3"),   # 3rd SpaceX → dropped at cap=2
            self._c("NIST.AI.600-1 (Part 1)", "4"),
        ]
        out = RAGManager._cap_per_document(cands, cap=2)
        ids = [c["id"] for c in out]
        assert ids == ["1", "2", "4"]          # SpaceX#3 removed, NIST surfaces

    def test_cap_one_maximally_diversifies(self):
        cands = [self._c("A (Part 1)", "1"), self._c("A (Part 2)", "2"),
                 self._c("B (Part 1)", "3")]
        assert [c["id"] for c in RAGManager._cap_per_document(cands, cap=1)] == ["1", "3"]

    def test_cap_preserves_legit_multi_part_within_limit(self):
        # a genuinely single-doc query keeps its top chunks up to the cap
        cands = [self._c("PDE (Part 51)", "1"), self._c("PDE (Part 61)", "2")]
        assert len(RAGManager._cap_per_document(cands, cap=2)) == 2
