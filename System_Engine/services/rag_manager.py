import os
import hashlib
from pathlib import Path
import chromadb
from chromadb.utils import embedding_functions
import logging
import time
from functools import wraps
from datetime import datetime

from core.config import (
    EMBEDDING_PROVIDER,
    EMBEDDING_MODEL,
    EMBEDDING_CACHE_ENABLED,
    RERANKER_ENABLED,
    RERANKER_MODEL,
    RERANKER_MULTIPLIER,
    HYBRID_RETRIEVAL_ENABLED,
    BM25_MULTIPLIER,
    WIKI_VAULT_DIR,
    USE_THOUGHTFUL_SPLITTER
)
from core.tag_manager import TagManager
from services.embedding_cache import CachedEmbeddingFunction
from services.bm25_index import BM25Index, rrf_merge

OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Decorator to handle temporary database locks in SQLite
def retry_on_db_lock(retries=3, delay=1):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for i in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if "database is locked" in str(e).lower() or "timeout" in str(e).lower():
                        logging.warning(f"Database locked, retrying {i+1}/{retries}...")
                        time.sleep(delay * (i + 1))
                        last_err = e
                    else:
                        raise e
            raise last_err
        return wrapper
    return decorator


def get_effective_model_name(provider: str, model: str | None) -> str:
    if provider == "local":
        return "all-MiniLM-L6-v2"
    if provider == "ollama":
        return model or "nomic-embed-text"
    if provider == "gemini":
        return model or "text-embedding-004"
    return model or "unknown"


class GeminiEmbeddingFunction(chromadb.EmbeddingFunction):
    def __init__(self, api_key: str, model_name: str = "text-embedding-004"):
        from google import genai
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name

    def __call__(self, input: list[str]) -> list[list[float]]:
        response = self.client.models.embed_content(
            model=self.model_name,
            contents=input,
        )
        return [emb.values for emb in response.embeddings]

    def embed_query(self, input: list[str]) -> list[list[float]]:
        return self(input)

    def name(self) -> str:
        return "GeminiEmbeddingFunction"


class OllamaEmbeddingFunction(chromadb.EmbeddingFunction):
    def __init__(self, api_base: str, model_name: str = "nomic-embed-text"):
        self.api_url = f"{api_base.rstrip('/')}/api/embed"
        self.model_name = model_name

    def __call__(self, input: list[str]) -> list[list[float]]:
        import requests
        # Truncate each input string to 1200 characters to prevent context length errors in Ollama
        safe_input = [text[:1200] for text in input]
        try:
            resp = requests.post(
                self.api_url,
                json={"model": self.model_name, "input": safe_input},
                timeout=60
            )
            if resp.status_code != 200:
                logging.error(f"Ollama embedding HTTP error {resp.status_code}: {resp.text}")
            resp.raise_for_status()
            return resp.json()["embeddings"]
        except Exception as e:
            logging.error(f"Ollama embedding failed for model {self.model_name}: {e}")
            raise e

    def embed_query(self, input: list[str]) -> list[list[float]]:
        return self(input)

    def name(self) -> str:
        return "OllamaEmbeddingFunction"


class RAGManager:
    def __init__(self, db_path: str = None, skip_config_check: bool = False):
        from core.config import DATABASE_DIR
        self.db_dir = (DATABASE_DIR / db_path) if db_path else DATABASE_DIR
        self.db_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize persistent client
        self.client = chromadb.PersistentClient(path=str(self.db_dir))

        # Initialize trace store
        from services.trace_store import TraceStore
        self.trace_store = TraceStore()
        
        # Initialize embedding function based on configuration
        if EMBEDDING_PROVIDER == "gemini":
            if not GEMINI_API_KEY or GEMINI_API_KEY == "your_gemini_api_key_here":
                raise ValueError("GEMINI_API_KEY is not configured but gemini embedding provider is selected.")
            self.ef = GeminiEmbeddingFunction(api_key=GEMINI_API_KEY, model_name=EMBEDDING_MODEL or "text-embedding-004")
        elif EMBEDDING_PROVIDER == "ollama":
            api_base = OLLAMA_API_BASE
            if api_base.endswith("/v1"):
                api_base = api_base[:-3]
            self.ef = OllamaEmbeddingFunction(api_base=api_base, model_name=EMBEDDING_MODEL or "nomic-embed-text")
        else:
            self.ef = embedding_functions.DefaultEmbeddingFunction()

        # Wrap with persistent cache so re-embedding the same text (after
        # wipe, provider swap, or partial reindex) is a SQLite lookup
        # rather than a fresh model call. Disable via EMBEDDING_CACHE_ENABLED=false.
        if EMBEDDING_CACHE_ENABLED:
            self.ef = CachedEmbeddingFunction(
                inner=self.ef,
                model_name=get_effective_model_name(EMBEDDING_PROVIDER, EMBEDDING_MODEL),
                db_path=self.db_dir / "embedding_cache.sqlite",
            )

        # Collection for wiki pages
        try:
            if skip_config_check:
                try:
                    self.collection = self.client.get_collection(
                        name="wiki_pages",
                        embedding_function=self.ef,
                    )
                except Exception:
                    self.collection = self.client.create_collection(
                        name="wiki_pages",
                        embedding_function=self.ef
                    )
            else:
                self.collection = self.client.get_or_create_collection(
                    name="wiki_pages",
                    embedding_function=self.ef
                )
        except ValueError as e:
            if "embedding function" in str(e).lower() or "conflict" in str(e).lower():
                curr_provider = EMBEDDING_PROVIDER
                curr_model = get_effective_model_name(curr_provider, EMBEDDING_MODEL)
                try:
                    old_coll = self.client.get_collection(name="wiki_pages")
                    db_metadata = old_coll.metadata
                    if not db_metadata or "embedding_provider" not in db_metadata:
                        db_provider = "local"
                        db_model = "all-MiniLM-L6-v2"
                        db_dim = 384
                    else:
                        db_provider = db_metadata.get("embedding_provider")
                        db_model = db_metadata.get("embedding_model")
                        db_dim = int(db_metadata.get("embedding_dimension") or 0)
                except Exception:
                    db_provider, db_model, db_dim = "unknown", "unknown", "unknown"
                
                error_msg = (
                    f"Embedding configuration mismatch detected!\n"
                    f"Database collection has: provider={db_provider}, model={db_model}, dimension={db_dim}\n"
                    f"Current config expects: provider={curr_provider}, model={curr_model}\n"
                    f"Please wipe the database and perform a full re-index to apply changes:\n"
                    f"run 'python System_Engine/maintenance/init_rag.py --wipe'"
                )
                logging.critical(error_msg)
                raise ValueError(error_msg) from e
            else:
                raise e

        # Initialize appropriate text splitter
        if USE_THOUGHTFUL_SPLITTER:
            from services.thoughtful_splitter import ThoughtfulSplitter
            self.splitter = ThoughtfulSplitter(
                default_use_llm=False,
                default_emit_summary=False
            )
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
                logging.warning(
                    f"Reranker disabled: sentence-transformers not installed ({e})"
                )
                self._reranker = False
                return None
            except Exception as e:
                logging.warning(f"Reranker init failed, falling back to vector-only: {e}")
                self._reranker = False
                return None
        return self._reranker

    def _check_metadata_mismatch(self):
        """Validate the persisted embedding config matches the current one.

        We avoid probing the embedding model on every startup — provider and
        model name (recorded in collection metadata) are authoritative. The
        dimension probe only runs when we genuinely need it: an empty
        collection that needs its metadata initialised. That makes startup
        a no-op for steady-state Gemini/Ollama setups (no wasted API call).
        """
        curr_provider = EMBEDDING_PROVIDER
        curr_model = get_effective_model_name(curr_provider, EMBEDDING_MODEL)
        db_metadata = self.collection.metadata or {}

        has_complete_meta = all(
            key in db_metadata
            for key in ("embedding_provider", "embedding_model", "embedding_dimension")
        )
        if (
            has_complete_meta
            and db_metadata.get("embedding_provider") == curr_provider
            and db_metadata.get("embedding_model") == curr_model
        ):
            return

        if self.collection.count() == 0:
            try:
                curr_dim = len(self.ef(["test"])[0])
            except Exception as e:
                logging.warning(f"Could not automatically detect embedding dimension: {e}")
                curr_dim = 768 if curr_provider in ("ollama", "gemini") else 384
            new_meta = {
                **db_metadata,
                "embedding_provider": curr_provider,
                "embedding_model": curr_model,
                "embedding_dimension": curr_dim,
            }
            self.collection.modify(metadata=new_meta)
            return

        if not db_metadata or "embedding_provider" not in db_metadata:
            db_provider = "local"
            db_model = "all-MiniLM-L6-v2"
            db_dim = 384
        else:
            db_provider = db_metadata.get("embedding_provider")
            db_model = db_metadata.get("embedding_model")
            db_dim = int(db_metadata.get("embedding_dimension") or 0)

        if db_provider == curr_provider and db_model == curr_model:
            return

        error_msg = (
            f"Embedding configuration mismatch detected!\n"
            f"Database collection has: provider={db_provider}, model={db_model}, dimension={db_dim}\n"
            f"Current config expects: provider={curr_provider}, model={curr_model}\n"
            f"Please wipe the database and perform a full re-index to apply changes:\n"
            f"run 'python System_Engine/maintenance/init_rag.py --wipe'"
        )
        logging.critical(error_msg)
        raise ValueError(error_msg)

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

    @staticmethod
    def _sanitize_tag_key(tag_name: str) -> str:
        tag = TagManager.normalize(tag_name)
        if not tag:
            return ""
        sanitized = tag.replace("/", "_").replace("\\", "_")
        res = "".join(c for c in sanitized if c.isalnum() or c in ("_", "-"))
        while "__" in res:
            res = res.replace("__", "_")
        while "--" in res:
            res = res.replace("--", "-")
        res = res.strip("_").strip("-")
        return f"tag_{res}" if res else ""

    def _build_where_clause(
        self,
        tags: list[str] | None = None,
        section_path: list[str] | None = None,
        where_filter: dict | None = None
    ) -> dict | None:
        filters = []
        
        if tags:
            for t in tags:
                san_tag = self._sanitize_tag_key(t)
                if san_tag:
                    filters.append({san_tag: True})
                    
        if section_path:
            for idx, level in enumerate(section_path):
                if idx < 6:
                    filters.append({f"section_l{idx + 1}": level.lower().strip()})
                    
        if where_filter:
            filters.append(where_filter)
            
        if not filters:
            return None
        if len(filters) == 1:
            return filters[0]
        return {"$and": filters}

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
        payload = "\x00".join([
            text,
            ",".join(norm_tags),
            ",".join(section_path or []),
        ])
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
        tags: list[str] = None,
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
                    s_path = list(ts_chunk.section_path) if ts_chunk.section_path else (section_path or [])
                    boundary = ts_chunk.boundary_type.label
                    chunks_data.append({
                        "text": ts_chunk.text,
                        "start": ts_chunk.start,
                        "end": ts_chunk.end,
                        "section_path": s_path,
                        "boundary_type": boundary,
                    })
            else:
                ts_chunks = self.splitter.split_text_with_spans(text)
                chunks_data = []
                for ts_chunk in ts_chunks:
                    chunks_data.append({
                        "text": ts_chunk["text"],
                        "start": ts_chunk["start"],
                        "end": ts_chunk["end"],
                        "section_path": section_path or [],
                        "boundary_type": "paragraph",
                    })

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
                    ">" + ">".join(s.lower().strip() for s in s_path) + ">"
                    if s_path else ""
                )

                chunk_id = f"{doc_id}_chunk_{start}_{end}"
                ids.append(chunk_id)
                documents.append(chunk_text)

                meta = {
                    "source": filepath.name,
                    "source_path": source_path,
                    "doc_id": doc_id,
                    "title": title,
                    "start_offset": start,
                    "end_offset": end,
                    "timestamp": timestamp,
                    "tags": tags_display,
                    "section_path": section_marker,
                    "boundary_type": boundary,
                    "content_hash": content_hash,
                }

                # Add boolean tag fields for sanitization
                for tag in norm_tags:
                    san_tag = self._sanitize_tag_key(tag)
                    if san_tag:
                        meta[san_tag] = True

                # Add section level mappings (l1 to l6)
                meta["section_depth"] = len(s_path)
                for idx in range(6):
                    key = f"section_l{idx + 1}"
                    if idx < len(s_path):
                        meta[key] = s_path[idx].lower().strip()
                    else:
                        meta[key] = ""
                
                meta["section_path_full"] = " > ".join(s.lower().strip() for s in s_path)
                metadatas.append(meta)

            self._upsert_with_retry(
                documents=documents,
                metadatas=metadatas,
                ids=ids
            )
            self._bm25.mark_dirty()
            logging.info(f"Added document '{title}' ({doc_id[:8]}) to RAG DB ({len(chunks_data)} chunks)")

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
                query_text, top_k=top_k, tags=tags,
                section_path=section_path, diversity=diversity,
                rerank=rerank, hybrid=hybrid,
            )
            context_pieces = []
            for item in results:
                meta = item["metadata"]
                doc = item["text"]
                source_title = meta.get('title', 'Unknown Source')
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
    ) -> list[dict]:
        """Query RAG returning structured dict lists.

        Pipeline composes (in order): vector retrieve → (optional BM25
        supplement + RRF) → (optional cross-encoder rerank) → (optional
        MMR diversification). Each stage is independently togglable; the
        ``None`` defaults respect the corresponding ``*_ENABLED`` env.

        - ``diversity`` (0.0–1.0): MMR weight. 0 = relevance-only.
        - ``rerank``: cross-encoder re-scoring. Best precision win.
        - ``hybrid``: BM25 + vector fused via RRF. Catches proper nouns
          and code identifiers that pure vector misses.
        """
        try:
            use_rerank = rerank if rerank is not None else RERANKER_ENABLED
            reranker = self._get_reranker() if use_rerank else None
            if use_rerank and reranker is None:
                use_rerank = False
            use_hybrid = hybrid if hybrid is not None else HYBRID_RETRIEVAL_ENABLED

            where = self._build_where_clause(tags=tags, section_path=section_path)

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
            include = ["documents", "metadatas", "distances"]
            if need_embeddings:
                include.append("embeddings")

            vec_results = self.collection.query(
                query_texts=[query_text],
                n_results=n_pool,
                where=where,
                include=include,
            )

            vec_ids = vec_results.get('ids', [[]])[0]
            documents = vec_results.get('documents', [[]])[0]
            metadatas = vec_results.get('metadatas', [[]])[0]
            distances = vec_results.get('distances', [[]])[0]
            embeddings = vec_results.get('embeddings', [[]])[0] if need_embeddings else []

            # Trace candidate info
            candidate_info: dict[str, dict] = {}

            by_id: dict[str, dict] = {}
            for i, cid in enumerate(vec_ids):
                c = {
                    "text": documents[i] if i < len(documents) else "",
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                    "distance": distances[i] if i < len(distances) else 0.0,
                    "id": cid,
                }
                if need_embeddings and i < len(embeddings):
                    c["embedding"] = embeddings[i]
                by_id[cid] = c

                candidate_info[cid] = {
                    "vector_distance": distances[i] if i < len(distances) else 0.0,
                    "vector_rank": i + 1,
                    "bm25_score": None,
                    "bm25_rank": None,
                    "rrf_score": None,
                    "rerank_score": None,
                    "rerank_rank": None,
                    "mmr_selected": False,
                    "passed_layers": ["vector"]
                }

            bm25_ids: list[str] = []
            rrf_scores: dict[str, float] = {}
            if use_hybrid:
                bm25_hits = self._bm25.query(query_text, top_k * BM25_MULTIPLIER)
                bm25_score_map = {cid: score for cid, score in bm25_hits}
                raw_ids = [cid for cid, _ in bm25_hits]
                if where and raw_ids:
                    try:
                        filtered = self.collection.get(ids=raw_ids, where=where, include=[])
                        allowed = set(filtered.get("ids", []) or [])
                        bm25_ids = [cid for cid in raw_ids if cid in allowed]
                    except Exception as e:
                        logging.debug(f"BM25 where-filter failed, using unfiltered: {e}")
                        bm25_ids = raw_ids
                else:
                    bm25_ids = raw_ids

                missing = [cid for cid in bm25_ids if cid not in by_id]
                if missing:
                    miss_include = ["documents", "metadatas"]
                    if need_embeddings:
                        miss_include.append("embeddings")
                    try:
                        miss = self.collection.get(ids=missing, include=miss_include)
                        m_ids = miss.get("ids", []) or []
                        m_docs = miss.get("documents", []) or []
                        m_metas = miss.get("metadatas", []) or []
                        m_embs = miss.get("embeddings", []) or []
                        for i, cid in enumerate(m_ids):
                            new_c = {
                                "text": m_docs[i] if i < len(m_docs) else "",
                                "metadata": m_metas[i] if i < len(m_metas) else {},
                                "distance": 0.0,
                                "id": cid,
                            }
                            if need_embeddings and i < len(m_embs):
                                new_c["embedding"] = m_embs[i]
                            by_id[cid] = new_c
                    except Exception as e:
                        logging.debug(f"BM25 chunk fetch failed: {e}")

                for i, cid in enumerate(bm25_ids):
                    if cid not in candidate_info:
                        candidate_info[cid] = {
                            "vector_distance": None,
                            "vector_rank": None,
                            "bm25_score": bm25_score_map.get(cid),
                            "bm25_rank": i + 1,
                            "rrf_score": None,
                            "rerank_score": None,
                            "rerank_rank": None,
                            "mmr_selected": False,
                            "passed_layers": ["bm25"]
                        }
                    else:
                        candidate_info[cid]["bm25_score"] = bm25_score_map.get(cid)
                        candidate_info[cid]["bm25_rank"] = i + 1
                        candidate_info[cid]["passed_layers"].append("bm25")

                rrf_scores = rrf_merge([vec_ids, bm25_ids])
                for cid, rrf_s in rrf_scores.items():
                    if cid in candidate_info:
                        candidate_info[cid]["rrf_score"] = rrf_s

                ordered_ids = sorted(by_id.keys(), key=lambda c: rrf_scores.get(c, 0.0), reverse=True)
                candidates = [by_id[cid] for cid in ordered_ids]
            else:
                candidates = [by_id[cid] for cid in vec_ids if cid in by_id]

            # Facet hits are pointers, not content: swap each for its
            # parent's real chunk BEFORE reranking, so the cross-encoder
            # scores actual content and downstream consumers never see
            # summary text masquerading as source material.
            # use_facets=False drops facet hits instead (the A/B baseline
            # the retrieval bench uses to measure facet lift).
            if use_facets is False:
                candidates = [
                    c for c in candidates
                    if (c.get("metadata") or {}).get("role") != "facet"
                ]
            else:
                candidates = self._dereference_facets(candidates, candidate_info, need_embeddings)

            if use_rerank and candidates:
                scores = reranker.score(query_text, [c["text"] for c in candidates])
                for c, s in zip(candidates, scores):
                    c["rerank_score"] = s
                    cid = c["id"]
                    if cid in candidate_info:
                        candidate_info[cid]["rerank_score"] = s
                        candidate_info[cid]["passed_layers"].append("rerank")
                candidates.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
                for idx, c in enumerate(candidates):
                    cid = c["id"]
                    if cid in candidate_info:
                        candidate_info[cid]["rerank_rank"] = idx + 1

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
                    candidates = self._mmr_select(
                        candidates, top_k, lambda_param, relevance=relevance,
                    )
                elif use_hybrid:
                    relevance = [rrf_scores.get(c["id"], 0.0) for c in candidates]
                    candidates = self._mmr_select(
                        candidates, top_k, lambda_param, relevance=relevance,
                    )
                else:
                    query_emb = self.ef([query_text])[0]
                    candidates = self._mmr_select(
                        candidates, top_k, lambda_param, query_emb=query_emb,
                    )

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

            # Record event in trace store
            options = {
                "tags": tags,
                "section_path": section_path,
                "diversity": diversity,
                "rerank": use_rerank,
                "hybrid": use_hybrid,
                "use_facets": use_facets,
            }
            recorded_results = []
            for c in final_returned:
                recorded_results.append({
                    "id": c["id"],
                    "title": c["metadata"].get("title"),
                    "source": c["metadata"].get("source"),
                    "retrieval_breakdown": c.get("retrieval_breakdown"),
                })
            
            try:
                trace_id = None
                if hasattr(self.trace_store, "current_trace_ids"):
                    trace_ids = self.trace_store.current_trace_ids()
                    if trace_ids:
                        trace_id = trace_ids[-1]
                
                self.trace_store.record_retrieval_event(
                    query_text=query_text,
                    top_k=top_k,
                    options=options,
                    results=recorded_results,
                    trace_id=trace_id,
                    status="succeeded",
                )
            except Exception as e:
                logging.debug(f"Failed to record retrieval event: {e}")

            return final_returned
        except Exception as e:
            logging.error(f"RAG query failed: {e}")
            try:
                options = {
                    "tags": tags,
                    "section_path": section_path,
                    "diversity": diversity,
                    "rerank": rerank,
                    "hybrid": hybrid,
                }
                trace_id = None
                if hasattr(self.trace_store, "current_trace_ids"):
                    trace_ids = self.trace_store.current_trace_ids()
                    if trace_ids:
                        trace_id = trace_ids[-1]
                self.trace_store.record_retrieval_event(
                    query_text=query_text,
                    top_k=top_k,
                    options=options,
                    results=[],
                    trace_id=trace_id,
                    status="failed",
                    error=str(e),
                )
            except Exception as trace_error:
                logging.debug(f"Failed to record failed retrieval event: {trace_error}")
            return []


    @staticmethod
    def _mmr_select(
        candidates: list[dict],
        top_k: int,
        lambda_param: float,
        *,
        query_emb=None,
        relevance: list[float] | None = None,
    ) -> list[dict]:
        """Maximal Marginal Relevance over candidate embeddings.

        Pass ``query_emb`` to derive relevance from cosine-to-query, OR
        ``relevance`` to use a precomputed score (e.g. reranker output).
        Cosine sim for the diversity term is computed on length-normalized
        vectors so this works regardless of ChromaDB's hnsw:space setting.
        """
        import numpy as np

        usable_with_idx = [
            (i, c) for i, c in enumerate(candidates) if c.get("embedding") is not None
        ]
        if not usable_with_idx:
            return candidates[:top_k]
        usable = [c for _, c in usable_with_idx]

        embs = np.asarray([c["embedding"] for c in usable], dtype=np.float32)
        norms = np.linalg.norm(embs, axis=1, keepdims=True) + 1e-9
        embs_norm = embs / norms

        if relevance is not None:
            rel = np.asarray(
                [relevance[i] for i, _ in usable_with_idx], dtype=np.float32,
            )
            if len(rel) > 1:
                lo, hi = float(rel.min()), float(rel.max())
                rel = (rel - lo) / (hi - lo) if hi > lo else np.zeros_like(rel)
        elif query_emb is not None:
            q = np.asarray(query_emb, dtype=np.float32)
            q_norm = q / (np.linalg.norm(q) + 1e-9)
            rel = embs_norm @ q_norm
        else:
            raise ValueError("_mmr_select requires either query_emb or relevance")

        selected_idx: list[int] = []
        remaining = list(range(len(usable)))

        while remaining and len(selected_idx) < top_k:
            if not selected_idx:
                best = remaining[int(np.argmax(rel[remaining]))]
            else:
                sel = embs_norm[selected_idx]
                cand = embs_norm[remaining]
                sim_to_selected = (cand @ sel.T).max(axis=1)
                mmr = lambda_param * rel[remaining] - (1 - lambda_param) * sim_to_selected
                best = remaining[int(np.argmax(mmr))]
            selected_idx.append(best)
            remaining.remove(best)

        return [usable[i] for i in selected_idx]

    @retry_on_db_lock()
    def _upsert_with_retry(self, **kwargs):
        self.collection.upsert(**kwargs)

    @retry_on_db_lock()
    def add_facets(self, filepath: Path, title: str, facets: list[str], tags: list[str] = None):
        """Index LLM-generated facet sentences (thesis / key points) as
        retrieval pointers for a document.

        Facets share the parent's doc_id (so deletion paths and the orphan
        sweep cover them automatically) and carry role="facet". At query
        time a facet hit is dereferenced to the parent's real chunk — facet
        text itself is never returned as content. Stale facets for the doc
        are dropped first, so re-ingestion stays idempotent.
        """
        facets = [f.strip() for f in (facets or []) if isinstance(f, str) and f.strip()]
        if not facets:
            return
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
                metadatas.append({
                    "role": "facet",
                    "doc_id": doc_id,
                    "title": title,
                    "source": Path(filepath).name,
                    "facet_index": i,
                    "timestamp": timestamp,
                    "tags": tags_display,
                })

            self._upsert_with_retry(documents=documents, metadatas=metadatas, ids=ids)
            self._bm25.mark_dirty()
            logging.info(f"Indexed {len(ids)} facets for '{title}' ({doc_id[:8]})")
        except Exception as e:
            logging.error(f"Failed to add facets for '{title}': {e}")

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
                cid for cid, meta in zip(results.get("ids") or [], results.get("metadatas") or [])
                if (meta or {}).get("role") == "facet"
            ]
            if facet_ids:
                self.collection.delete(ids=facet_ids)
        except Exception as e:
            logging.debug(f"Facet cleanup failed for {doc_id[:8]}: {e}")

    def get_facet_entries(self) -> list[dict]:
        """All facet entries: [{title, text, facet_index, timestamp}].

        Powers the bench builder — facet theses are the raw material for
        auto-generated regression queries.
        """
        try:
            results = self.collection.get(where={"role": "facet"}, include=["documents", "metadatas"])
        except Exception as e:
            logging.debug(f"Facet listing failed: {e}")
            return []
        out = []
        documents = results.get("documents") or []
        metadatas = results.get("metadatas") or []
        for i, meta in enumerate(metadatas):
            meta = meta or {}
            out.append({
                "title": meta.get("title"),
                "text": documents[i] if i < len(documents) else "",
                "facet_index": meta.get("facet_index", 0),
                "timestamp": meta.get("timestamp", ""),
            })
        return out

    def all_chunks(self, include: tuple[str, ...] = ("metadatas", "documents")) -> dict:
        """Every indexed chunk's fields, as ChromaDB's {ids, documents,
        metadatas} dict. A named accessor so callers (e.g. InsightAgent's
        sampling/context builders) don't reach into `.collection` directly and
        couple to the vector store (audit R7-C-2)."""
        return self.collection.get(include=list(include))

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
        self, doc_ids: list[str], need_embeddings: bool = False
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
        rescued: list[dict] = []
        seen: set[str] = set()
        for c in candidates:
            meta = c.get("metadata") or {}
            if meta.get("role") != "facet":
                if c["id"] not in seen:
                    seen.add(c["id"])
                    direct.append(c)
                continue

            base = parents.get(meta.get("doc_id"))
            if base is None:
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
                swapped["passed_layers"] = list(facet_info.get("passed_layers", [])) + ["facet_deref"]
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

        results = self.collection.get(include=["metadatas"])
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
        legacy_ids = [
            cid for cid, md in zip(ids, metas) if not (md or {}).get("doc_id")
        ]
        if legacy_ids:
            try:
                self.collection.delete(ids=legacy_ids)
                logging.info(f"Deleted {len(legacy_ids)} legacy (doc_id-less) chunk(s) for title '{title}'")
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
            results = self.collection.get(include=['metadatas'])
            metadatas = results.get('metadatas', [])
            titles = set(m.get('title') for m in metadatas if m and 'title' in m)
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
            results = self.collection.get(limit=sample_limit, include=['metadatas'])
            for meta in results.get('metadatas') or []:
                if meta and any(k.startswith('tag_') for k in meta):
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
                "embedding_dimension": curr_dim
            }

            self.collection = self.client.create_collection(
                name="wiki_pages",
                embedding_function=self.ef,
                metadata=metadata
            )
            self._bm25.replace_collection(self.collection)
            logging.info("RAGManager: Collection wiped and recreated.")
        except Exception as e:
            logging.error(f"RAGManager: Failed to wipe collection: {e}")


if __name__ == "__main__":
    manager = RAGManager()
    print(f"RAG Manager initialized. Collection count: {manager.collection.count()}")
