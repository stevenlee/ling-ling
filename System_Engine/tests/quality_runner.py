"""LLM-driven chunk-coherence scoring helper.

This module is shared infrastructure for any test that wants to know
"is this chunk self-contained?" via the LLM. It runs the scorer N times
per chunk and takes the median to suppress LLM variance.

Design notes:
  * The scoring prompt is locked in `LLMClient.score_text_quality`'s
    `_CHUNK_COHERENCE_PROMPTS` table (services/llm_client.py).
    Don't pass freeform prompts through here — that defeats the
    reproducibility of regression scores.
  * Failures (LLM down, bad JSON) return score=0. Median-of-N drops
    isolated failures gracefully.
  * This module is intentionally importable WITHOUT building an LLMClient
    (e.g. for unit-testing the median/statistics logic).
"""
from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkScore:
    score: int                  # 1-10, or 0 on failure
    raw_scores: tuple[int, ...] # the individual run scores
    reasons: tuple[str, ...]    # one reason string per run
    prompt_version: str


def coherence_score(
    text: str,
    *,
    score_fn: Callable[[str], dict],
    runs: int = 3,
) -> ChunkScore:
    """Score a chunk N times via `score_fn`, return median.

    `score_fn` is typically `llm_client.score_text_quality`. We accept it
    as a parameter so tests can inject a deterministic stub without
    monkey-patching.

    Returns the median raw score along with the full run vector for
    diagnostics. A run that returned 0 (failure) is still counted; if all
    runs fail, the median is 0.
    """
    if runs < 1:
        raise ValueError(f"runs must be >= 1, got {runs}")

    raw_scores: list[int] = []
    reasons: list[str] = []
    prompt_version = "v1"

    for _ in range(runs):
        result = score_fn(text)
        raw_scores.append(int(result.get("score", 0)))
        reasons.append(str(result.get("reason", "")))
        prompt_version = result.get("prompt_version", prompt_version)

    median = int(statistics.median(raw_scores))
    return ChunkScore(
        score=median,
        raw_scores=tuple(raw_scores),
        reasons=tuple(reasons),
        prompt_version=prompt_version,
    )


def median_corpus_score(scores: list[ChunkScore]) -> float:
    """Aggregate per-chunk medians into a corpus-level median."""
    if not scores:
        return 0.0
    return float(statistics.median(s.score for s in scores))
