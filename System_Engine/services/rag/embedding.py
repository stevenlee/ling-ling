"""Embedding backends for RAG (P2e).

Moved verbatim from services/rag_manager.py: the Gemini/Ollama embedding
functions (with the NaN-batch isolation that keeps one bad input from
dropping a whole document), the effective-model-name helper, and the
construction logic (provider dispatch + auto-sized truncation cap + optional
persistent cache wrap).
"""

from __future__ import annotations

import logging

import chromadb
from chromadb.utils import embedding_functions

from services.embedding_cache import CachedEmbeddingFunction


def _vec_has_nan(vec) -> bool:
    """True if any element is NaN (NaN != itself). Guards against embedding
    backends that emit NaN for pathological inputs."""
    return any(x != x for x in vec)


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

    def __call__(self, input: list[str]) -> list[list[float]]:  # type: ignore[override]
        response = self.client.models.embed_content(
            model=self.model_name,
            contents=input,
        )
        return [emb.values for emb in response.embeddings]  # type: ignore[misc,union-attr]

    def embed_query(self, input: list[str]) -> list[list[float]]:  # type: ignore[override]
        return self(input)

    def name(self) -> str:  # type: ignore[override]
        return "GeminiEmbeddingFunction"


class OllamaEmbeddingFunction(chromadb.EmbeddingFunction):
    def __init__(self, api_base: str, model_name: str = "nomic-embed-text", max_chars: int = 1200):
        self.api_url = f"{api_base.rstrip('/')}/api/embed"
        self.model_name = model_name
        # Per-input char cap to avoid Ollama context-length 400s. Sized to the
        # model: nomic's context is short (~1200), bge-m3 holds 8192 tokens so
        # it gets a far larger cap. Too small a cap silently embeds only the
        # head of each chunk (the bug that crippled vector retrieval). 0 = none.
        self.max_chars = max_chars

    def _embed(self, batch: list[str]) -> list[list[float]] | None:
        """POST one batch. Returns embeddings, or None on an HTTP error (e.g.
        Ollama's 500 ``json: unsupported value: NaN`` — bge-m3 occasionally
        emits a NaN embedding for a specific input). Raises only on transport
        errors, so the caller's transient-retry path still applies."""
        import requests

        resp = requests.post(
            self.api_url,
            json={"model": self.model_name, "input": batch},
            timeout=60,
        )
        if resp.status_code != 200:
            # Recoverable upstream: __call__ falls back to per-item embedding and
            # _embed_resilient substitutes a placeholder for the bad input. Log at
            # debug so a handled NaN-500 doesn't masquerade as an error; a genuine
            # outage still surfaces via _embed_resilient's "all inputs failed" raise.
            logging.debug(f"Ollama embedding HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        return resp.json()["embeddings"]

    def __call__(self, input: list[str]) -> list[list[float]]:  # type: ignore[override]
        safe_input = [
            (text[: self.max_chars] if self.max_chars and self.max_chars > 0 else text)
            for text in input
        ]
        try:
            embs = self._embed(safe_input)
        except Exception as e:
            logging.error(f"Ollama embedding failed for model {self.model_name}: {e}")
            raise
        if (
            embs is not None
            and len(embs) == len(safe_input)
            and not any(_vec_has_nan(e) for e in embs)
        ):
            return embs
        # bge-m3 intermittently emits NaN for one specific input, and Ollama
        # then 500s the ENTIRE batch — silently losing a document's whole facet
        # / chunk set. Isolate per-item so one bad input can't drop the rest.
        logging.warning(
            f"Ollama embedding: a batch of {len(safe_input)} hit a NaN/error; "
            f"isolating per-item (model {self.model_name})."
        )
        return self._embed_resilient(safe_input)

    def _embed_resilient(self, safe_input: list[str]) -> list[list[float]]:
        out: list[list[float] | None] = [None] * len(safe_input)
        dim: int | None = None
        bad: list[int] = []
        for i, text in enumerate(safe_input):
            try:
                e = self._embed([text])
            except Exception:
                e = None
            if e and len(e) == 1 and not _vec_has_nan(e[0]):
                out[i] = e[0]
                dim = dim or len(e[0])
            else:
                bad.append(i)
        if bad and len(bad) == len(safe_input):
            # Every item failed on its own. Outage — OR every input is
            # individually NaN-poisoned. The two are indistinguishable when
            # the batch is small: the embedding cache shrinks retry batches
            # to exactly the previously-failed inputs, so one deterministic
            # NaN input (seen live: a truncated-LaTeX facet on bge-m3) became
            # a batch of 1 that "failed for all 1 inputs" forever. Probe with
            # a canary: healthy provider → placeholder the poisoned inputs;
            # dead canary → genuine outage, raise honestly.
            canary = self._embed_canary()
            if canary is None:
                raise RuntimeError(
                    f"Ollama embedding failed for all {len(safe_input)} inputs "
                    f"(model {self.model_name})"
                )
            dim = dim or len(canary)
        for i in bad:
            logging.warning(
                "Ollama embedding produced NaN/error for one input; substituted a "
                f"placeholder vector so the batch still indexes. text={safe_input[i][:100]!r}"
            )
            placeholder = [0.0] * (dim or 0)
            if placeholder:
                placeholder[0] = 1.0  # unit vector: valid cosine, avoids zero-norm NaN
            out[i] = placeholder
        return out  # type: ignore[return-value]

    def _embed_canary(self) -> list[float] | None:
        """A known-good plain sentence; a clean vector proves the provider is
        up (→ the failing inputs are input-specific), None means outage."""
        try:
            e = self._embed(["The quick brown fox jumps over the lazy dog."])
        except Exception:
            return None
        if e and len(e) == 1 and not _vec_has_nan(e[0]):
            return e[0]
        return None

    def embed_query(self, input: list[str]) -> list[list[float]]:  # type: ignore[override]
        return self(input)

    def name(self) -> str:  # type: ignore[override]
        return "OllamaEmbeddingFunction"


def build_embedding_function(
    *,
    provider: str,
    model: str | None,
    ollama_api_base: str,
    gemini_api_key: str | None,
    max_chars: int,
    cache_enabled: bool,
    cache_db_path,
):
    ef: object
    """Construct the embedding function for a provider (+ optional cache wrap).

    Logic moved verbatim from RAGManager.__init__ — including the model-aware
    auto-sizing of the Ollama truncation cap (nomic's short context vs
    bge-m3's 8192-token window; too small a cap silently embeds only the head
    of each chunk, the bug that crippled vector retrieval).
    """
    if provider == "gemini":
        if not gemini_api_key or gemini_api_key == "your_gemini_api_key_here":
            raise ValueError(
                "GEMINI_API_KEY is not configured but gemini embedding provider is selected."
            )
        ef = GeminiEmbeddingFunction(
            api_key=gemini_api_key, model_name=model or "text-embedding-004"
        )
    elif provider == "ollama":
        api_base = ollama_api_base
        if api_base.endswith("/v1"):
            api_base = api_base[:-3]
        ollama_model = model or "nomic-embed-text"
        cap = max_chars
        if cap <= 0:
            # nomic: short context (~1200 safe). bge-m3 & other long-context
            # models: 8000 covers the corpus (p90≈5862, CHUNK_SIZE=5000) and
            # sits within bge-m3's 8192-token window — verified no 400s.
            cap = 1200 if "nomic" in ollama_model.lower() else 8000
        ef = OllamaEmbeddingFunction(api_base=api_base, model_name=ollama_model, max_chars=cap)
    else:
        ef = embedding_functions.DefaultEmbeddingFunction()

    # Wrap with persistent cache so re-embedding the same text (after
    # wipe, provider swap, or partial reindex) is a SQLite lookup
    # rather than a fresh model call. Disable via EMBEDDING_CACHE_ENABLED=false.
    if cache_enabled:
        ef = CachedEmbeddingFunction(
            inner=ef,
            model_name=get_effective_model_name(provider, model),
            db_path=cache_db_path,
        )
    return ef
