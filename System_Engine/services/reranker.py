"""Cross-encoder reranker for RAG.

Wraps a sentence-transformers CrossEncoder. The heavy import is deferred
to ``__init__`` so RAGManager startup stays light when reranking is
disabled. First call downloads the model (~568MB for the default
``BAAI/bge-reranker-v2-m3``), subsequent runs hit HuggingFace cache.

Reranker improves retrieval precision by re-scoring the (query, chunk)
pair with a full cross-attention transformer instead of relying on the
dot product of independent embeddings.
"""

from __future__ import annotations

import logging


class CrossEncoderReranker:
    def __init__(self, model_name: str, max_length: int = 512):
        from sentence_transformers import CrossEncoder
        logging.info(f"Loading cross-encoder reranker: {model_name}")
        self.model = CrossEncoder(model_name, max_length=max_length)
        self.model_name = model_name

    def score(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        pairs = [(query, d) for d in docs]
        scores = self.model.predict(pairs, show_progress_bar=False)
        return [float(s) for s in scores]
