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

    def test_add_document_cleans_doc_id_and_legacy_title(self, tmp_path):
        class FakeCollection:
            def __init__(self):
                self.deleted = []
                self.upserts = []

            def get(self, **kwargs):
                return {"metadatas": []}

            def delete(self, where):
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
        assert {"doc_id": expected_doc_id} in manager.collection.deleted
        assert {"title": "Legacy Title"} in manager.collection.deleted
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
