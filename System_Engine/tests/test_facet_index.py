"""Facet index: LLM digest sentences (thesis/key_points) embedded as
retrieval pointers. Facets share the parent's doc_id, carry role="facet",
and are dereferenced to the parent's real chunk before reranking."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from services.ingestion_pipeline import IngestionPipeline
from services.rag_manager import RAGManager


class FakeCollection:
    """In-memory stand-in supporting the subset of Chroma used by facets."""

    def __init__(self):
        self.store = {}  # id -> {"text", "meta"}

    def upsert(self, documents, metadatas, ids):
        for cid, doc, meta in zip(ids, documents, metadatas):
            self.store[cid] = {"text": doc, "meta": meta}

    def get(self, where=None, include=None, limit=None, ids=None):
        items = self.store.items()
        if ids is not None:
            items = [(cid, v) for cid, v in items if cid in set(ids)]
        if where and "doc_id" in where:
            items = [(cid, v) for cid, v in items if v["meta"].get("doc_id") == where["doc_id"]]
        items = list(items)
        if limit:
            items = items[:limit]
        return {
            "ids": [cid for cid, _ in items],
            "documents": [v["text"] for _, v in items],
            "metadatas": [v["meta"] for _, v in items],
        }

    def delete(self, ids=None, where=None):
        for cid in ids or []:
            self.store.pop(cid, None)


def _rag() -> RAGManager:
    rag = RAGManager.__new__(RAGManager)
    rag.collection = FakeCollection()
    rag._bm25 = MagicMock()
    return rag


class TestAddFacets:
    def test_facets_written_with_role_and_doc_id(self, tmp_path):
        rag = _rag()
        page = tmp_path / "pages" / "X" / "X (Part 1).md"
        rag.add_facets(page, "X (Part 1)", ["Core thesis.", "Key point one."])

        metas = [v["meta"] for v in rag.collection.store.values()]
        assert len(metas) == 2
        assert all(m["role"] == "facet" for m in metas)
        assert all(m["doc_id"] == RAGManager._get_doc_id(page) for m in metas)
        assert all(m["title"] == "X (Part 1)" for m in metas)
        rag._bm25.mark_dirty.assert_called_once()

    def test_reingestion_replaces_stale_facets(self, tmp_path):
        rag = _rag()
        page = tmp_path / "p.md"
        rag.add_facets(page, "P", ["Old thesis."])
        rag.add_facets(page, "P", ["New thesis."])

        texts = [v["text"] for v in rag.collection.store.values()]
        assert texts == ["New thesis."]

    def test_empty_or_invalid_facets_noop(self, tmp_path):
        rag = _rag()
        rag.add_facets(tmp_path / "p.md", "P", ["", "   ", None])
        assert rag.collection.store == {}


class TestDereference:
    def _seed_parent(self, rag, page, n_chunks=2):
        doc_id = RAGManager._get_doc_id(page)
        for i in range(n_chunks):
            rag.collection.store[f"{doc_id}_chunk_{i*100}_{i*100+99}"] = {
                "text": f"chunk {i} body",
                "meta": {"doc_id": doc_id, "title": "P", "start_offset": i * 100},
            }
        return doc_id

    def test_facet_hit_swapped_for_first_parent_chunk(self, tmp_path):
        rag = _rag()
        page = tmp_path / "p.md"
        doc_id = self._seed_parent(rag, page)
        facet = {
            "id": f"{doc_id}_facet_abc", "text": "The thesis.",
            "metadata": {"role": "facet", "doc_id": doc_id, "title": "P"},
            "distance": 0.1,
        }
        info = {facet["id"]: {"vector_rank": 1, "passed_layers": ["vector"]}}

        out = rag._dereference_facets([facet], info)

        assert len(out) == 1
        assert out[0]["id"] == f"{doc_id}_chunk_0_99"
        assert out[0]["text"] == "chunk 0 body"
        assert out[0]["matched_facet"] == "The thesis."
        # Retrieval signals carried over to the parent id.
        assert "facet_deref" in info[out[0]["id"]]["passed_layers"]

    def test_parent_already_in_pool_wins(self, tmp_path):
        rag = _rag()
        page = tmp_path / "p.md"
        doc_id = self._seed_parent(rag, page, n_chunks=1)
        parent_id = f"{doc_id}_chunk_0_99"
        chunk = {"id": parent_id, "text": "chunk 0 body", "metadata": {"doc_id": doc_id}, "distance": 0.05}
        facet = {
            "id": f"{doc_id}_facet_abc", "text": "The thesis.",
            "metadata": {"role": "facet", "doc_id": doc_id}, "distance": 0.1,
        }

        out = rag._dereference_facets([chunk, facet], {})

        assert [c["id"] for c in out] == [parent_id]
        assert "matched_facet" not in out[0]  # direct hit, not via facet

    def test_dangling_facet_dropped(self, tmp_path):
        rag = _rag()
        facet = {
            "id": "ghost_facet_1", "text": "Orphan thesis.",
            "metadata": {"role": "facet", "doc_id": "nonexistent"}, "distance": 0.1,
        }
        assert rag._dereference_facets([facet], {}) == []

    def test_plain_chunks_pass_through_with_dedup(self, tmp_path):
        rag = _rag()
        a = {"id": "a", "text": "A", "metadata": {}, "distance": 0.1}
        out = rag._dereference_facets([a, dict(a)], {})
        assert [c["id"] for c in out] == ["a"]


class TestContentHashSkipsFacets:
    def test_facet_first_does_not_defeat_hash_lookup(self, tmp_path):
        rag = _rag()
        page = tmp_path / "p.md"
        doc_id = RAGManager._get_doc_id(page)
        # Facet stored "before" the real chunk in iteration order.
        rag.collection.store[f"{doc_id}_facet_x"] = {
            "text": "thesis", "meta": {"doc_id": doc_id, "role": "facet"},
        }
        rag.collection.store[f"{doc_id}_chunk_0_99"] = {
            "text": "body", "meta": {"doc_id": doc_id, "content_hash": "h123"},
        }
        assert rag._get_existing_content_hash(doc_id) == "h123"


class TestPipelineFacetWiring:
    """Phase A/B wiring: digests become add_facets calls."""

    def _facet_rag(self):
        from unittest.mock import MagicMock
        rag = MagicMock()
        return rag

    def test_facets_from_digest_extraction(self):
        digest = {
            "thesis": "Main argument.",
            "key_points": ["Point one is long enough.", "Point one is long enough.", "x"],
        }
        facets = IngestionPipeline._facets_from_digest(digest)
        assert facets == ["Main argument.", "Point one is long enough."]
        assert IngestionPipeline._facets_from_digest("not a dict") == []
        assert IngestionPipeline._facets_from_digest(MagicMock()) == []
