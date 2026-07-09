"""Semantic entropy — how spread-out the knowledge is in embedding space.

The homogenization the audit surfaced (novelty collapsing to 0.14) is a
*distributional* property: outputs clustering into a shrinking region of
embedding space. novelty is a local, pairwise-nearest measure; this is the
global one.

Primary metric: **effective dimensionality** = the participation ratio of the
embedding covariance eigenvalues, (Σλ)² / Σλ². Read it as "the effective
number of independent dimensions the population spreads across". Isotropic
spread → ≈ full dimensionality; collapse onto a line → → 1. No k to choose
(unlike cluster entropy), scale-invariant, pure numpy.

Computed over three populations, all read from cached embeddings (zero LLM):
  - insights   (novelty sidecar)        → output diversity
  - cortex     (consolidation state)    → belief diversity
  - sources    (ChromaDB, if rag given) → the reading diet (input baseline)

The load-bearing signal is the RATIO output/input (insight_dim / source_dim):
a wide input but collapsed output is a GENERATION problem (temperature, seed
pairing, operation rotation); a narrow input is a READING-DIET problem (feed
it more varied material). See DesignDoc/Ontology_SemanticEntropy_implementation_plan.md.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path

import numpy as np


def _read_json(path: Path, default):
    try:
        if path and path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logging.debug(f"semantic_entropy: failed reading {getattr(path, 'name', path)}: {e}")
    return default


def _same_dim(vectors: list) -> list:
    """Keep only vectors of the modal dimension — a model switch (bge-m3
    768→1024) can leave stray-width vectors that would make a ragged matrix."""
    clean = [v for v in (vectors or []) if v]
    if not clean:
        return []
    dims = Counter(len(v) for v in clean)
    d = dims.most_common(1)[0][0]
    return [v for v in clean if len(v) == d]


def effective_dimensionality(vectors: list) -> float | None:
    """Participation ratio of the centered population's covariance spectrum.
    None when undefined (< 3 vectors, or zero variance)."""
    rows = _same_dim(vectors)
    if len(rows) < 3:
        return None
    arr = np.asarray(rows, dtype=float)
    arr = arr - arr.mean(axis=0, keepdims=True)
    try:
        # Singular values of centered data; eigenvalues of the covariance are
        # s²/(n-1). PR is a ratio, so the (n-1) factor cancels — use s² directly.
        s = np.linalg.svd(arr, compute_uv=False)
    except np.linalg.LinAlgError:
        return None
    lam = s.astype(float) ** 2
    total = float(lam.sum())
    if total <= 0.0:
        return None
    pr = (total**2) / float((lam**2).sum())
    return round(float(pr), 3)


def _load_map_embeddings(path: Path, key: str | None = None) -> list:
    data = _read_json(path, {})
    if not isinstance(data, dict):
        return []
    if key:
        data = data.get(key) or {}
    if not isinstance(data, dict):
        return []
    out = []
    for v in data.values():
        emb = v.get("embedding") if isinstance(v, dict) else None
        if emb:
            out.append(emb)
    return out


def compute_entropy_report(
    *,
    insight_signals_file: Path,
    cortex_state_file: Path,
    rag=None,
    source_limit: int = 300,
) -> dict:
    """Effective dimensionality per population + the output/input ratio.
    All fields fail-open to None; the source population is only sampled when a
    rag with sample_document_embeddings is supplied."""
    insight = _load_map_embeddings(insight_signals_file)
    cortex = _load_map_embeddings(cortex_state_file, key="claim_embeddings")

    source = []
    if rag is not None and hasattr(rag, "sample_document_embeddings"):
        try:
            source = rag.sample_document_embeddings(source_limit) or []
        except Exception as e:
            logging.warning(f"semantic_entropy: source sampling failed: {e}")

    d_ins = effective_dimensionality(insight)
    d_cor = effective_dimensionality(cortex)
    d_src = effective_dimensionality(source)
    ratio = round(d_ins / d_src, 3) if (d_ins and d_src) else None

    return {
        "insight_dim": d_ins,
        "cortex_dim": d_cor,
        "source_dim": d_src,
        "eff_dim_ratio": ratio,  # output(insight) / input(source)
        "n_insight": len(_same_dim(insight)),
        "n_cortex": len(_same_dim(cortex)),
        "n_source": len(_same_dim(source)),
    }
