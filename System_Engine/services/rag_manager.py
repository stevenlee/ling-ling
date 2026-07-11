import os
from typing import Any
import hashlib
from pathlib import Path
import chromadb
import logging
from datetime import datetime

from core.config import (
    EMBEDDING_PROVIDER,
    EMBEDDING_MODEL,
    EMBEDDING_CACHE_ENABLED,
    EMBEDDING_MAX_CHARS,
    RERANKER_ENABLED,
    RERANKER_MODEL,
    RERANKER_MULTIPLIER,
    HYBRID_RETRIEVAL_ENABLED,
    BM25_MULTIPLIER,
    RETRIEVAL_MAX_PER_DOC,
    CROSS_LINGUAL_ENABLED,
    CROSS_LINGUAL_TARGET_LANGS,
    WIKI_VAULT_DIR,
    USE_THOUGHTFUL_SPLITTER,
)
from core.tag_manager import TagManager
from services.bm25_index import BM25Index
from services.rag import retrieval
from services.rag.chroma_store import (
    build_where_clause,
    check_metadata_mismatch,
    ensure_collection,
    retry_on_db_lock,
    sanitize_tag_key,
)
from services.rag.chunk_meta import ChunkMetadata
from services.rag.embedding import (  # noqa: F401  (re-exported: tests/maintenance import these)
    GeminiEmbeddingFunction,
    OllamaEmbeddingFunction,
    build_embedding_function,
    get_effective_model_name,
)

OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


class RAGManager:
    def __init__(
        self, db_path: str | None = None, skip_config_check: bool = False, translator=None
    ):
        from core.config import DATABASE_DIR

        self.db_dir = (DATABASE_DIR / db_path) if db_path else DATABASE_DIR
        self.db_dir.mkdir(parents=True, exist_ok=True)

        # Initialize persistent client
        self.client = chromadb.PersistentClient(path=str(self.db_dir))

        # Initialize trace store
        from services.trace_store import TraceStore

        self.trace_store = TraceStore()

        # Optional cross-lingual query translator (RAGManager stays LLM-free):
        # a callable (text, langs) -> {lang: str}, e.g. LLMClient.translate_query.
        # Passed by main.py's composition root; the bench harness and tests may
        # still assign the attribute after construction.
        self.translator = translator

        # Embedding function: provider dispatch + cache wrap live in
        # services/rag/embedding.py (P2e).
        self.ef = build_embedding_function(
            provider=EMBEDDING_PROVIDER,
            model=EMBEDDING_MODEL,
            ollama_api_base=OLLAMA_API_BASE,
            gemini_api_key=GEMINI_API_KEY,
            max_chars=EMBEDDING_MAX_CHARS,
            cache_enabled=EMBEDDING_CACHE_ENABLED,
            cache_db_path=self.db_dir / "embedding_cache.sqlite",
        )

        # Collection for wiki pages (creation + mismatch messaging:
        # services/rag/chroma_store.py).
        self.collection = ensure_collection(
            self.client,
            self.ef,
            provider=EMBEDDING_PROVIDER,
            model=EMBEDDING_MODEL,
            skip_config_check=skip_config_check,
        )

        # Initialize appropriate text splitter
        self.splitter: Any
        if USE_THOUGHTFUL_SPLITTER:
            from services.thoughtful_splitter import ThoughtfulSplitter

            self.splitter = ThoughtfulSplitter(default_use_llm=False, default_emit_summary=False)
        else:
            from services.text_splitter import TextSplitter

            self.splitter = TextSplitter()

        # Lazy-loaded cross-encoder; first query with rerank=True triggers
        # the import + model download. ``False`` sentinel means we already
        # tried and failed (don't keep retrying).
        self._reranker = None

        # BM25 lexical index for hybrid retrieval; rebuilt lazily.
        self._bm25 = BM25Index(self.collection)

        if not skip_config_check:
            self._check_metadata_mismatch()

    def _get_reranker(self):
        if self._reranker is False:
            return None
        if self._reranker is None:
            try:
                from services.reranker import CrossEncoderReranker

                self._reranker = CrossEncoderReranker(RERANKER_MODEL)
            except ImportError as e:
                logging.warning(f"Reranker disabled: sentence-transformers not installed ({e})")
                self._reranker = False
                return None
            except Exception as e:
                logging.warning(f"Reranker init failed, falling back to vector-only: {e}")
                self._reranker = False
                return None
        return self._reranker

    def _check_metadata_mismatch(self):
        check_metadata_mismatch(
            self.collection, self.ef, provider=EMBEDDING_PROVIDER, model=EMBEDDING_MODEL
        )

    @staticmethod
    def _get_doc_id(filepath: Path) -> str:
        try:
            abs_filepath = Path(filepath).resolve()
            abs_vault_dir = WIKI_VAULT_DIR.resolve()
            rel_path = abs_filepath.relative_to(abs_vault_dir)
        except Exception:
            try:
                rel_path = Path(filepath).relative_to(WIKI_VAULT_DIR)
            except Exception:
                rel_path = Path(filepath)

        posix_path = str(rel_path).replace("\\", "/")
        return hashlib.sha256(posix_path.encode("utf-8")).hexdigest()

    # Where-clause construction + tag-key sanitizing live in
    # services/rag/chroma_store.py (P2e); kept as thin members for callers/tests.
    _sanitize_tag_key = staticmethod(sanitize_tag_key)

    def _build_where_clause(
        self,
        tags: list[str] | None = None,
        section_path: list[str] | None = None,
        where_filter: dict | None = None,
    ) -> dict | None:
        return build_where_clause(tags=tags, section_path=section_path, where_filter=where_filter)

    @staticmethod
    def _compute_content_hash(
        text: str,
        norm_tags: list[str],
        section_path: list[str] | None,
    ) -> str:
        """Fingerprint everything that would change the indexed output.

        Text + tags + section_path are joined with NUL separators so that
        e.g. an empty tag list cannot collide with a one-tag list whose
        sole element is the empty string.
        """
        payload = "\x00".join(
            [
                text,
                ",".join(norm_tags),
                ",".join(section_path or []),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _get_existing_content_hash(self, doc_id: str) -> str | None:
        """Return the content_hash of any existing chunk for this doc_id, or None.

        Skips facet entries (they share the doc_id but carry no
        content_hash); fetches a few rows so a facet landing first doesn't
        defeat the unchanged-content short-circuit.
        """
        try:
            results = self.collection.get(
                where={"doc_id": doc_id},
                limit=10,
                include=["metadatas"],
            )
        except Exception as e:
            logging.debug(f"content_hash lookup failed for {doc_id[:8]}: {e}")
            return None
        for meta in results.get("metadatas") or []:
            content_hash = (meta or {}).get("content_hash")
            if content_hash:
                return content_hash
        return None

    def add_document(
        self,
        filepath: Path,
        title: str,
        text: str,
        tags: list[str] | None = None,
        section_path: list[str] | None = None,
        strict: bool = False,
        force: bool = False,
    ):
        """Chunk and add a markdown document to the ChromaDB.

        Unchanged docs short-circuit via content_hash so reindex runs don't
        re-embed the entire vault. Pass ``force=True`` to bypass the check
        (used by migrations or after schema changes).
        """
        try:
            doc_id = self._get_doc_id(filepath)
            timestamp = datetime.now().isoformat()
            norm_tags = TagManager.normalize_list(tags)
            tags_display = f",{','.join(norm_tags)}," if norm_tags else ""

            content_hash = self._compute_content_hash(text, norm_tags, section_path)
            if not force:
                existing = self._get_existing_content_hash(doc_id)
                if existing == content_hash:
                    logging.info(f"Skipped '{title}' ({doc_id[:8]}): content unchanged")
                    return

            # doc_id is the sha256 of the vault-relative path, so re-ingesting a
            # file always hits the same doc_id and clears its own chunks.
            self.delete_document(doc_id)
            # Legacy cleanup, SCOPED: chunks indexed before doc_id existed were
            # keyed by title only. Delete just those — a blanket
            # delete-by-title would also wipe an unrelated document that happens
            # to share this title (audit A2). ChromaDB has no "field absent"
            # filter, so fetch by title and keep only the doc_id-less rows.
            self._delete_legacy_title_chunks(title)

            try:
                rel_path = Path(filepath).resolve().relative_to(WIKI_VAULT_DIR.resolve())
            except Exception:
                try:
                    rel_path = Path(filepath).relative_to(WIKI_VAULT_DIR)
                except Exception:
                    rel_path = Path(filepath)
            source_path = str(rel_path).replace("\\", "/")

            if USE_THOUGHTFUL_SPLITTER:
                ts_chunks = self.splitter.split_thoughtful(text)
                chunks_data = []
                for ts_chunk in ts_chunks:
                    s_path = (
                        list(ts_chunk.section_path)
                        if ts_chunk.section_path
                        else (section_path or [])
                    )
                    boundary = ts_chunk.boundary_type.label
                    chunks_data.append(
                        {
                            "text": ts_chunk.text,
                            "start": ts_chunk.start,
                            "end": ts_chunk.end,
                            "section_path": s_path,
                            "boundary_type": boundary,
                        }
                    )
            else:
                span_chunks = self.splitter.split_text_with_spans(text)
                chunks_data = []
                for span_chunk in span_chunks:
                    chunks_data.append(
                        {
                            "text": span_chunk["text"],
                            "start": span_chunk["start"],
                            "end": span_chunk["end"],
                            "section_path": section_path or [],
                            "boundary_type": "paragraph",
                        }
                    )

            if not chunks_data:
                return

            ids = []
            documents = []
            metadatas = []

            for chunk_info in chunks_data:
                start = chunk_info["start"]
                end = chunk_info["end"]
                chunk_text = chunk_info["text"]
                s_path = chunk_info["section_path"]
                boundary = chunk_info["boundary_type"]

                # Lowercase + `>...>` so ChromaDB `where` clauses can use
                # `$contains: ">background>"` to find content in that section.
                section_marker = (
                    ">" + ">".join(s.lower().strip() for s in s_path) + ">" if s_path else ""
                )

                chunk_id = f"{doc_id}_chunk_{start}_{end}"
                ids.append(chunk_id)
                documents.append(chunk_text)

                metadatas.append(
                    ChunkMetadata(
                        source=filepath.name,
                        source_path=source_path,
                        doc_id=doc_id,
                        title=title,
                        start_offset=start,
                        end_offset=end,
                        timestamp=timestamp,
                        tags=tags_display,
                        section_path=section_marker,
                        boundary_type=boundary,
                        content_hash=content_hash,
                        section_levels=list(s_path),
                        norm_tags=norm_tags,
                    ).to_chroma()
                )

            self._upsert_with_retry(documents=documents, metadatas=metadatas, ids=ids)
            self._bm25.mark_dirty()
            logging.info(
                f"Added document '{title}' ({doc_id[:8]}) to RAG DB ({len(chunks_data)} chunks)"
            )

        except Exception as e:
            logging.error(f"Failed to add document '{title}' to RAG: {e}")
            if strict:
                raise e

    def query_similar_notes(
        self,
        query_text: str,
        top_k: int = 3,
        tags: list[str] | None = None,
        section_path: list[str] | None = None,
        diversity: float = 0.0,
        rerank: bool | None = None,
        hybrid: bool | None = None,
    ) -> list[str]:
        """Search for most relevant chunks. Returns markdown formatted strings."""
        try:
            results = self.query_notes(
                query_text,
                top_k=top_k,
                tags=tags,
                section_path=section_path,
                diversity=diversity,
                rerank=rerank,
                hybrid=hybrid,
            )
            context_pieces = []
            for item in results:
                meta = item["metadata"]
                doc = item["text"]
                source_title = meta.get("title", "Unknown Source")
                context_pieces.append(f"### [來自筆記: {source_title}]\n{doc}")
            return context_pieces
        except Exception as e:
            logging.error(f"RAG query similar notes failed: {e}")
            return []

    def query_notes(
        self,
        query_text: str,
        top_k: int = 3,
        tags: list[str] | None = None,
        section_path: list[str] | None = None,
        diversity: float = 0.0,
        rerank: bool | None = None,
        hybrid: bool | None = None,
        use_facets: bool | None = None,
        max_per_doc: int | None = None,
        cross_lingual: bool | None = None,
        extra_queries: list[str] | None = None,
    ) -> list[dict]:
        """Query RAG returning structured dict lists.

        Orchestrates the stages in services/rag/retrieval.py (in order):
        vector retrieve → (optional cross-lingual variants) → (optional BM25
        supplement) → RRF fusion → facet dereference → (optional cross-encoder
        rerank) → (optional MMR diversification) → (per-document cap) → trace.
        Each stage is independently togglable; the ``None`` defaults respect
        the corresponding ``*_ENABLED`` env.

        - ``diversity`` (0.0–1.0): MMR weight. 0 = relevance-only.
        - ``rerank``: cross-encoder re-scoring. Best precision win.
        - ``hybrid``: BM25 + vector fused via RRF. Catches proper nouns
          and code identifiers that pure vector misses.
        - ``max_per_doc``: keep at most N chunks from the same source
          document in the final top-k (anti-flood). ``None`` → config
          ``RETRIEVAL_MAX_PER_DOC``; 0 disables. Skipped when MMR runs
          (MMR is already a diversity mechanism).
        - ``cross_lingual``: translate the query into the other corpus
          languages and fold each variant's candidates into the pool (RRF
          fused, reranked against the ORIGINAL query) — so a zh query can
          surface en/de docs. ``None`` → config ``CROSS_LINGUAL_ENABLED``;
          needs ``self.translator`` set. Widens recall, doesn't touch the index.
        - ``extra_queries``: explicit extra query strings to fuse in (same
          mechanism as cross-lingual, but caller-supplied — bypasses the
          translator; used by tests/bench). Takes precedence over translation.
        """
        try:
            use_rerank = rerank if rerank is not None else RERANKER_ENABLED
            reranker = self._get_reranker() if use_rerank else None
            if use_rerank and reranker is None:
                use_rerank = False
            use_hybrid = hybrid if hybrid is not None else HYBRID_RETRIEVAL_ENABLED

            where = self._build_where_clause(tags=tags, section_path=section_path)

            variants = retrieval.resolve_variants(
                query_text,
                extra_queries=extra_queries,
                cross_lingual=cross_lingual,
                default_enabled=CROSS_LINGUAL_ENABLED,
                translator=self.translator,
                target_langs=CROSS_LINGUAL_TARGET_LANGS,
            )

            pool_factor = max(
                RERANKER_MULTIPLIER if use_rerank else 1,
                BM25_MULTIPLIER if use_hybrid else 1,
                3 if diversity > 0 else 1,
            )
            if pool_factor > 1:
                n_pool = max(top_k * pool_factor, top_k + 5)
            else:
                n_pool = top_k

            need_embeddings = diversity > 0

            by_id, candidate_info, vec_ids, extra_vec_id_lists = retrieval.gather_vector_candidates(
                self.collection,
                query_text,
                variants,
                n_pool=n_pool,
                where=where,
                need_embeddings=need_embeddings,
            )

            bm25_ids: list[str] = []
            if use_hybrid:
                bm25_ids = retrieval.gather_bm25_candidates(
                    self._bm25,
                    self.collection,
                    query_text,
                    k=top_k * BM25_MULTIPLIER,
                    where=where,
                    by_id=by_id,
                    candidate_info=candidate_info,
                    need_embeddings=need_embeddings,
                )

            candidates, rrf_scores = retrieval.fuse_rankings(
                by_id,
                candidate_info,
                vec_ids,
                extra_vec_id_lists,
                bm25_ids,
                use_hybrid=use_hybrid,
            )

            # Facet hits are pointers, not content: swap each for its
            # parent's real chunk BEFORE reranking, so the cross-encoder
            # scores actual content and downstream consumers never see
            # summary text masquerading as source material.
            # use_facets=False drops facet hits instead (the A/B baseline
            # the retrieval bench uses to measure facet lift).
            if use_facets is False:
                candidates = [
                    c for c in candidates if (c.get("metadata") or {}).get("role") != "facet"
                ]
            else:
                candidates = self._dereference_facets(candidates, candidate_info, need_embeddings)

            if use_rerank and candidates:
                retrieval.apply_rerank(reranker, query_text, candidates, candidate_info)

            mmr_ran = False
            if diversity > 0 and candidates:
                for c in candidates:
                    cid = c["id"]
                    if cid in candidate_info:
                        candidate_info[cid]["passed_layers"].append("mmr")

                lambda_param = max(0.0, min(1.0, 1.0 - diversity))
                mmr_ran = True
                if use_rerank:
                    relevance = [c.get("rerank_score", 0.0) for c in candidates]
                    candidates = retrieval.mmr_select(
                        candidates, top_k, lambda_param, relevance=relevance
                    )
                elif use_hybrid:
                    relevance = [rrf_scores.get(c["id"], 0.0) for c in candidates]
                    candidates = retrieval.mmr_select(
                        candidates, top_k, lambda_param, relevance=relevance
                    )
                else:
                    query_emb = self.ef([query_text])[0]
                    candidates = retrieval.mmr_select(
                        candidates, top_k, lambda_param, query_emb=query_emb
                    )

            # Per-document cap (anti-flood): on the non-MMR path, stop a single
            # high-volume document from occupying most of the top-k and burying
            # the relevant doc just below the cut. MMR already diversifies, so
            # skip it there to avoid double-shrinking.
            cap = RETRIEVAL_MAX_PER_DOC if max_per_doc is None else max_per_doc
            if (not mmr_ran) and cap and cap > 0 and candidates:
                candidates = retrieval.cap_per_document(candidates, cap)

            final_returned = candidates[:top_k]
            if mmr_ran:
                for c in final_returned:
                    cid = c["id"]
                    if cid in candidate_info:
                        candidate_info[cid]["mmr_selected"] = True

            for c in final_returned:
                c.pop("embedding", None)
                cid = c["id"]
                if cid in candidate_info:
                    c["retrieval_breakdown"] = candidate_info[cid]

            options = {
                "tags": tags,
                "section_path": section_path,
                "diversity": diversity,
                "rerank": use_rerank,
                "hybrid": use_hybrid,
                "use_facets": use_facets,
            }
            recorded_results = [
                {
                    "id": c["id"],
                    "title": c["metadata"].get("title"),
                    "source": c["metadata"].get("source"),
                    "retrieval_breakdown": c.get("retrieval_breakdown"),
                }
                for c in final_returned
            ]
            retrieval.record_retrieval_trace(
                self.trace_store,
                query_text=query_text,
                top_k=top_k,
                options=options,
                results=recorded_results,
                status="succeeded",
            )

            return final_returned
        except Exception as e:
            logging.error(f"RAG query failed: {e}")
            retrieval.record_retrieval_trace(
                self.trace_store,
                query_text=query_text,
                top_k=top_k,
                options={
                    "tags": tags,
                    "section_path": section_path,
                    "diversity": diversity,
                    "rerank": rerank,
                    "hybrid": hybrid,
                },
                results=[],
                status="failed",
                error=str(e),
            )
            return []

    # Ranking helpers live in services/rag/retrieval.py (P2e); kept as thin
    # members for callers/tests.
    _doc_key = staticmethod(retrieval.doc_key)

    @classmethod
    def _cap_per_document(cls, candidates: list[dict], cap: int) -> list[dict]:
        return retrieval.cap_per_document(candidates, cap)

    _mmr_select = staticmethod(retrieval.mmr_select)

    @retry_on_db_lock()
    def _upsert_with_retry(self, **kwargs):
        self.collection.upsert(**kwargs)

    @retry_on_db_lock()
    def add_facets(
        self, filepath: Path, title: str, facets: list[str], tags: list[str] | None = None
    ) -> bool:
        """Index LLM-generated facet sentences (thesis / key points) as
        retrieval pointers for a document.

        Facets share the parent's doc_id (so deletion paths and the orphan
        sweep cover them automatically) and carry role="facet". At query
        time a facet hit is dereferenced to the parent's real chunk — facet
        text itself is never returned as content. Stale facets for the doc
        are dropped first, so re-ingestion stays idempotent.

        Fail-open by design (facets are a retrieval bonus), but the outcome
        is RETURNED: False on a swallowed failure. Callers that must not
        mark work as done on failure (facet backfill) check it — an Ollama
        embedding error here once let the backfill log "+4 facets" and
        permanently retire a page whose upsert had actually failed.
        """
        facets = [f.strip() for f in (facets or []) if isinstance(f, str) and f.strip()]
        if not facets:
            return True
        try:
            doc_id = self._get_doc_id(filepath)
            self._delete_facets(doc_id)

            timestamp = datetime.now().isoformat()
            norm_tags = TagManager.normalize_list(tags)
            tags_display = f",{','.join(norm_tags)}," if norm_tags else ""

            ids, documents, metadatas = [], [], []
            for i, facet_text in enumerate(facets):
                facet_hash = hashlib.sha256(facet_text.encode("utf-8")).hexdigest()[:16]
                ids.append(f"{doc_id}_facet_{facet_hash}")
                documents.append(facet_text)
                metadatas.append(
                    {
                        "role": "facet",
                        "doc_id": doc_id,
                        "title": title,
                        "source": Path(filepath).name,
                        "facet_index": i,
                        "timestamp": timestamp,
                        "tags": tags_display,
                    }
                )

            self._upsert_with_retry(documents=documents, metadatas=metadatas, ids=ids)
            self._bm25.mark_dirty()
            logging.info(f"Indexed {len(ids)} facets for '{title}' ({doc_id[:8]})")
            return True
        except Exception as e:
            logging.error(f"Failed to add facets for '{title}': {e}")
            return False

    @retry_on_db_lock()
    def remove_facets(self, filepath: Path) -> None:
        """Public facet removal for one document (dormant Cortex pages
        leave the facet index but keep their chunks and their file)."""
        self._delete_facets(self._get_doc_id(filepath))
        self._bm25.mark_dirty()

    def _delete_facets(self, doc_id: str) -> None:
        """Remove existing facet entries for one doc (python-side filter —
        legacy chunks have no `role` key, and Chroma's $ne semantics on
        missing keys are unreliable)."""
        try:
            results = self.collection.get(where={"doc_id": doc_id}, include=["metadatas"])
            facet_ids = [
                cid
                for cid, meta in zip(results.get("ids") or [], results.get("metadatas") or [])
                if (meta or {}).get("role") == "facet"
            ]
            if facet_ids:
                self.collection.delete(ids=facet_ids)
        except Exception as e:
            logging.debug(f"Facet cleanup failed for {doc_id[:8]}: {e}")

    def sample_document_embeddings(self, limit: int = 300) -> list[list[float]]:
        """Up to `limit` non-facet chunk embeddings, for corpus-diversity
        metrics (semantic entropy). Read-only, best-effort: returns [] on any
        error. Facet rows are excluded so the sample reflects real content."""
        out: list[list[float]] = []
        try:
            PAGE = 500
            offset = 0
            while len(out) < limit:
                batch = self.collection.get(
                    include=["embeddings", "metadatas"], limit=PAGE, offset=offset
                )
                ids = batch.get("ids") or []
                if not ids:
                    break
                embs = batch.get("embeddings") or []
                metas = batch.get("metadatas") or []
                for emb, meta in zip(embs, metas):
                    if (meta or {}).get("role") == "facet":
                        continue
                    if emb is not None and len(emb):
                        out.append([float(x) for x in emb])
                        if len(out) >= limit:
                            break
                if len(ids) < PAGE:
                    break
                offset += PAGE
        except Exception as e:
            logging.warning(f"sample_document_embeddings failed: {e}")
        return out

    def get_facet_entries(self) -> list[dict]:
        """All facet entries: [{title, text, facet_index, timestamp}].

        Powers the bench builder — facet theses are the raw material for
        auto-generated regression queries.
        """
        try:
            results = self.collection.get(
                where={"role": "facet"}, include=["documents", "metadatas"]
            )
        except Exception as e:
            logging.debug(f"Facet listing failed: {e}")
            return []
        out = []
        documents = results.get("documents") or []
        metadatas = results.get("metadatas") or []
        for i, meta in enumerate(metadatas):
            meta = meta or {}
            out.append(
                {
                    "title": meta.get("title"),
                    "text": documents[i] if i < len(documents) else "",
                    "facet_index": meta.get("facet_index", 0),
                    "timestamp": meta.get("timestamp", ""),
                }
            )
        return out

    def _get_all(self, include: list[str]) -> dict:
        """Fetch the whole collection (ids + requested fields) in pages.

        chromadb 1.5.x raises "too many SQL variables" on a single unbounded
        get() over a large collection (every id lands in one IN(...) clause),
        so we page with limit/offset well under SQLite's bound-variable limit.
        """
        PAGE = 500
        want_docs, want_metas = "documents" in include, "metadatas" in include
        ids: list = []
        docs: list = []
        metas: list = []
        offset = 0
        while True:
            batch = self.collection.get(include=include, limit=PAGE, offset=offset)
            bids = batch.get("ids") or []
            if not bids:
                break
            ids.extend(bids)
            if want_docs:
                docs.extend(batch.get("documents") or [])
            if want_metas:
                metas.extend(batch.get("metadatas") or [])
            if len(bids) < PAGE:
                break
            offset += PAGE
        out: dict = {"ids": ids}
        if want_docs:
            out["documents"] = docs
        if want_metas:
            out["metadatas"] = metas
        return out

    def all_chunks(self, include: tuple[str, ...] = ("metadatas", "documents")) -> dict:
        """Every indexed chunk's fields, as ChromaDB's {ids, documents,
        metadatas} dict. A named accessor so callers (e.g. InsightAgent's
        sampling/context builders) don't reach into `.collection` directly and
        couple to the vector store (audit R7-C-2)."""
        return self._get_all(list(include))

    def chunks_by_title(
        self,
        title: str,
        *,
        include: tuple[str, ...] = ("metadatas", "documents"),
        limit: int | None = None,
    ) -> dict:
        """Chunks for one exact indexed title, same dict shape as all_chunks."""
        kwargs: dict = {"where": {"title": title}, "include": list(include)}
        if limit is not None:
            kwargs["limit"] = limit
        return self.collection.get(**kwargs)

    def _first_chunk_of_doc(self, doc_id: str | None, need_embeddings: bool = False) -> dict | None:
        """Fetch the parent document's leading real chunk (for facet
        dereferencing). Returns the same candidate shape query_notes uses."""
        if not doc_id:
            return None
        try:
            include = ["documents", "metadatas"]
            if need_embeddings:
                include.append("embeddings")
            results = self.collection.get(where={"doc_id": doc_id}, include=include)
        except Exception as e:
            logging.debug(f"Facet parent fetch failed for {doc_id[:8]}: {e}")
            return None

        ids = results.get("ids") or []
        documents = results.get("documents") or []
        metadatas = results.get("metadatas") or []
        embeddings = results.get("embeddings") if need_embeddings else None

        best = None
        for i, cid in enumerate(ids):
            meta = metadatas[i] if i < len(metadatas) else {}
            if (meta or {}).get("role") == "facet":
                continue
            start = (meta or {}).get("start_offset", 0) or 0
            if best is None or start < best[0]:
                candidate = {
                    "text": documents[i] if i < len(documents) else "",
                    "metadata": meta or {},
                    "distance": 0.0,
                    "id": cid,
                }
                if embeddings is not None and i < len(embeddings):
                    candidate["embedding"] = embeddings[i]
                best = (start, candidate)
        return best[1] if best else None

    def _leading_chunks_for_docs(
        self, doc_ids: list[str | None], need_embeddings: bool = False
    ) -> dict[str, dict]:
        """Batch variant of _first_chunk_of_doc: one collection.get for many
        doc_ids (audit R7-C). Returns {doc_id: leading-real-chunk candidate}.

        Previously _dereference_facets issued one get() per facet hit, so N
        facets pointing at M parents cost N round-trips with repeats; this
        collapses them to a single $in query.
        """
        unique = [d for d in dict.fromkeys(doc_ids) if d]
        if not unique:
            return {}
        try:
            include = ["documents", "metadatas"]
            if need_embeddings:
                include.append("embeddings")
            results = self.collection.get(where={"doc_id": {"$in": unique}}, include=include)
        except Exception as e:
            logging.debug(f"Batch facet-parent fetch failed: {e}")
            return {}

        ids = results.get("ids") or []
        documents = results.get("documents") or []
        metadatas = results.get("metadatas") or []
        embeddings = results.get("embeddings") if need_embeddings else None

        # Per doc_id, keep the lowest start_offset non-facet chunk (same rule
        # as _first_chunk_of_doc).
        best: dict[str, tuple[int, dict]] = {}
        for i, cid in enumerate(ids):
            meta = metadatas[i] if i < len(metadatas) else {}
            meta = meta or {}
            if meta.get("role") == "facet":
                continue
            did = meta.get("doc_id")
            if not did:
                continue
            start = meta.get("start_offset", 0) or 0
            if did not in best or start < best[did][0]:
                candidate = {
                    "text": documents[i] if i < len(documents) else "",
                    "metadata": meta,
                    "distance": 0.0,
                    "id": cid,
                }
                if embeddings is not None and i < len(embeddings):
                    candidate["embedding"] = embeddings[i]
                best[did] = (start, candidate)
        return {did: cand for did, (_, cand) in best.items()}

    def _dereference_facets(
        self,
        candidates: list[dict],
        candidate_info: dict,
        need_embeddings: bool = False,
    ) -> list[dict]:
        """Swap facet hits for their parent's real chunk — as a RESCUE tier.

        Facet sentences are short and semantically dense, so they
        systematically outrank long content chunks for short queries —
        letting them compete in place steals top-k slots from direct
        content hits (bench measured facet lift -2 that way). Instead,
        all facet-derived parents are appended AFTER every direct hit:
        they can only fill remaining slots and rescue pages that content
        matching missed, never displace a direct match. When the
        cross-encoder reranker is enabled it runs after this and can
        still promote a rescued parent on merit.

        Dedup: a parent already present as a direct hit wins; dangling
        facets (parent vanished) are dropped.
        """
        # One batched fetch for every facet parent up front, instead of one
        # collection.get per facet hit (audit R7-C).
        facet_doc_ids = [
            (c.get("metadata") or {}).get("doc_id")
            for c in candidates
            if (c.get("metadata") or {}).get("role") == "facet"
        ]
        parents = self._leading_chunks_for_docs(facet_doc_ids, need_embeddings)

        direct: list[dict] = []
        rescued: list[tuple[str, dict]] = []
        seen: set[str] = set()
        for c in candidates:
            meta = c.get("metadata") or {}
            if meta.get("role") != "facet":
                if c["id"] not in seen:
                    seen.add(c["id"])
                    direct.append(c)
                continue

            base = parents.get(meta.get("doc_id") or "")
            if base is None:
                # Dangling facet: its parent chunk vanished (deleted/re-ingested
                # under a new doc_id). Log it — silent drops made facet-count
                # drift invisible; the orphan sweep can then clean these up.
                logging.warning(
                    f"Facet deref dropped an orphan facet (id={c.get('id')!r}, "
                    f"doc_id={meta.get('doc_id')!r}): parent chunk not found."
                )
                continue
            # Copy: multiple facets can share a parent, and we stamp per-facet
            # signals onto it (the dedup below keeps the first).
            parent = dict(base)
            parent["distance"] = c.get("distance", 0.0)
            parent["matched_facet"] = c.get("text", "")
            rescued.append((c["id"], parent))

        out = direct
        for facet_id, parent in rescued:
            if parent["id"] in seen:
                continue
            seen.add(parent["id"])
            out.append(parent)

            # Carry the facet's retrieval signals over to the parent id so
            # the trace breakdown survives the swap.
            facet_info = candidate_info.get(facet_id)
            if facet_info is not None and parent["id"] not in candidate_info:
                swapped = dict(facet_info)
                swapped["passed_layers"] = list(facet_info.get("passed_layers", [])) + [
                    "facet_deref"
                ]
                candidate_info[parent["id"]] = swapped
        return out

    @retry_on_db_lock()
    def prune_orphan_chunks(self, roots: list[Path] | None = None) -> dict:
        """Delete chunks whose source file no longer exists on disk.

        Ground truth is the filesystem: every .md under the indexed roots
        (pages/, Notes/) maps to a doc_id via the same path hash used at
        add time; any chunk carrying an unknown doc_id is an orphan. This
        catches everything the event-based delete path can miss — folder
        deletions, renames/moves, and deletions while the daemon was off.

        Returns {"scanned", "orphan_docs", "deleted_chunks", "titles"}.
        """
        from core.config import CORTEX_DIR, NOTES_DIR, PAGES_DIR

        roots = roots if roots is not None else [PAGES_DIR, NOTES_DIR, CORTEX_DIR]

        valid_doc_ids = set()
        for root in roots:
            root = Path(root)
            if not root.exists():
                continue
            for file in root.rglob("*.md"):
                valid_doc_ids.add(self._get_doc_id(file))

        results = self._get_all(["metadatas"])
        ids = results.get("ids") or []
        metadatas = results.get("metadatas") or []

        orphan_ids: list[str] = []
        orphan_docs: set[str] = set()
        orphan_titles: set[str] = set()
        for chunk_id, meta in zip(ids, metadatas):
            doc_id = (meta or {}).get("doc_id")
            if doc_id not in valid_doc_ids:
                orphan_ids.append(chunk_id)
                orphan_docs.add(doc_id or "<no-doc-id>")
                title = (meta or {}).get("title")
                if title:
                    orphan_titles.add(title)

        if orphan_ids:
            # Delete by chunk id (not where-clause) so legacy chunks without
            # doc_id metadata are swept too.
            self.collection.delete(ids=orphan_ids)
            self._bm25.mark_dirty()
            logging.info(
                f"RAG orphan sweep: removed {len(orphan_ids)} chunks "
                f"from {len(orphan_docs)} vanished documents: {sorted(orphan_titles)[:10]}"
            )

        return {
            "scanned": len(ids),
            "orphan_docs": len(orphan_docs),
            "deleted_chunks": len(orphan_ids),
            "titles": sorted(orphan_titles),
        }

    def _delete_legacy_title_chunks(self, title: str) -> None:
        """Delete only the doc_id-less (pre-doc_id era) chunks for `title`.

        Scoped legacy cleanup for add_document: same-title chunks that DO carry
        a doc_id belong to other documents and must be left alone (audit A2).
        ChromaDB lacks a "field absent" operator, so we fetch by title and
        filter in Python rather than expressing it as a where clause.
        """
        if not title:
            return
        try:
            rows = self.collection.get(where={"title": title})
        except Exception as e:
            logging.debug(f"legacy title cleanup fetch failed for '{title}': {e}")
            return
        ids = rows.get("ids") or []
        metas = rows.get("metadatas") or []
        legacy_ids = [cid for cid, md in zip(ids, metas) if not (md or {}).get("doc_id")]
        if legacy_ids:
            try:
                self.collection.delete(ids=legacy_ids)
                logging.info(
                    f"Deleted {len(legacy_ids)} legacy (doc_id-less) chunk(s) for title '{title}'"
                )
            except Exception as e:
                logging.debug(f"legacy title cleanup delete failed for '{title}': {e}")

    @retry_on_db_lock()
    def delete_document(self, doc_id_or_title: str | Path):
        """Delete all chunks associated with a specific document (by doc_id, path, or title)."""
        doc_id = None
        title = None
        if isinstance(doc_id_or_title, Path):
            doc_id = self._get_doc_id(doc_id_or_title)
            title = doc_id_or_title.stem
        elif "/" in str(doc_id_or_title) or "\\" in str(doc_id_or_title):
            path_val = Path(doc_id_or_title)
            doc_id = self._get_doc_id(path_val)
            title = path_val.stem
        else:
            value = str(doc_id_or_title)
            if len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower()):
                doc_id = value
            else:
                title = value

        if doc_id:
            try:
                self.collection.delete(where={"doc_id": doc_id})
                logging.info(f"Deleted document chunks with doc_id '{doc_id}' from RAG")
            except Exception as e:
                logging.debug(f"Failed to delete by doc_id '{doc_id}': {e}")

        if title and title != doc_id:
            try:
                self.collection.delete(where={"title": title})
                logging.info(f"Deleted document chunks with title '{title}' from RAG")
            except Exception as e:
                if "not found" not in str(e).lower():
                    logging.error(f"Failed to delete document chunks with title '{title}': {e}")

        self._bm25.mark_dirty()

    def get_all_indexed_titles(self) -> set:
        """Retrieves a set of all unique document titles currently in the database."""
        try:
            results = self._get_all(["metadatas"])
            metadatas = results.get("metadatas", [])
            titles = set(m.get("title") for m in metadatas if m and "title" in m)
            return titles
        except Exception as e:
            logging.error(f"Failed to get indexed titles: {e}")
            return set()

    def get_total_chunks_count(self) -> int:
        """Returns the total number of chunks in the collection."""
        try:
            return self.collection.count()
        except Exception as e:
            logging.error(f"Failed to count chunks: {e}")
            return 0

    def has_tagged_documents(self, sample_limit: int = 200) -> bool:
        """True if any indexed chunk carries tag metadata (`tag_*` keys).

        Samples up to `sample_limit` chunks — enough to answer the skill
        precondition `has_tag_graph` without a full collection scan.
        """
        try:
            results = self.collection.get(limit=sample_limit, include=["metadatas"])
            for meta in results.get("metadatas") or []:
                if meta and any(k.startswith("tag_") for k in meta):
                    return True
            return False
        except Exception as e:
            logging.error(f"Failed to check tagged documents: {e}")
            return False

    def wipe_collection(self):
        """Completely wipes the wiki_pages collection."""
        try:
            logging.warning("RAGManager: Wiping 'wiki_pages' collection...")
            self.client.delete_collection("wiki_pages")

            curr_provider = EMBEDDING_PROVIDER
            curr_model = get_effective_model_name(curr_provider, EMBEDDING_MODEL)
            try:
                dummy_emb = self.ef(["test"])
                curr_dim = len(dummy_emb[0])
            except Exception as e:
                logging.warning(f"Could not automatically detect embedding dimension: {e}")
                curr_dim = 768 if curr_provider in ("ollama", "gemini") else 384

            metadata = {
                "embedding_provider": curr_provider,
                "embedding_model": curr_model,
                "embedding_dimension": curr_dim,
            }

            self.collection = self.client.create_collection(
                name="wiki_pages", embedding_function=self.ef, metadata=metadata
            )
            self._bm25.replace_collection(self.collection)
            logging.info("RAGManager: Collection wiped and recreated.")
        except Exception as e:
            logging.error(f"RAGManager: Failed to wipe collection: {e}")


if __name__ == "__main__":
    manager = RAGManager()
    print(f"RAG Manager initialized. Collection count: {manager.collection.count()}")
