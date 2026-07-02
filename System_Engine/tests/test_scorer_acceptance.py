"""P0 acceptance: scorer differentiates 'good chunk' vs 'bad chunk' by ≥ 3 points.

This test makes REAL LLM calls and is therefore skipped by default. Run it
manually once after wiring up the scorer, and again whenever the
`_CHUNK_COHERENCE_PROMPTS["v1"]` text changes:

    LLM_PROVIDER=ollama \\
    OLLAMA_MODEL=gemma2:27b \\
    venv/bin/python -m pytest \\
        System_Engine/tests/test_scorer_acceptance.py \\
        --run-live-llm -v

The chunks below were hand-curated:
- GOOD chunks are complete, standalone thoughts.
- BAD chunks are clearly truncated mid-sentence or mid-paragraph.

Acceptance bar: median GOOD score − median BAD score ≥ 3.
"""

import os
import statistics


import pytest

from tests.quality_runner import coherence_score


# Note: the --run-live-llm flag and `live_llm` marker are configured in
# `tests/conftest.py`. Tests marked `@pytest.mark.live_llm` are skipped by
# default; pass `--run-live-llm` on the pytest command line to opt in.


# ── Curated chunks ───────────────────────────────────────────────────

GOOD_CHUNKS = [
    # Complete paragraph with a clear topic + supporting detail + closure.
    (
        "The DRY principle says every piece of knowledge must have a single, "
        "unambiguous representation in a system. It is widely misapplied to "
        "code that merely looks similar — two functions with the same shape "
        "encoding different concepts are not DRY violations. The principle "
        "is about knowledge, not syntax."
    ),
    # A self-contained explanation with its own conclusion.
    (
        "MESI assigns each cache line one of four states: Modified, Exclusive, "
        "Shared, or Invalid. The protocol's correctness rule is simple: a line "
        "is in Modified state in at most one cache. When another core wants to "
        "read that line, the protocol must transition the writer to Shared or "
        "Invalid first. This is what causes cache-line ping-pong in contended "
        "code: every state transition is interconnect traffic."
    ),
    # A short standalone narrative.
    (
        "Cells are small for the same reason: diffusion timescales scale "
        "with the square of length. Doubling a cell's radius quadruples how "
        "long nutrients take to reach the centre. Eukaryotes evolved active "
        "intracellular transport to escape that constraint."
    ),
]

BAD_CHUNKS = [
    # Cut mid-sentence at the start AND end.
    (
        "ached, the protocol then transitions to Shared and the second cache "
        "receives a copy. If, however, the original writer had pending writes "
        "queued, those must first be flushed before"
    ),
    # Starts mid-paragraph, no opening context, ends mid-thought.
    (
        "and as a result the throughput drops by orders of magnitude as more "
        "cores are added. The cause is not contention on the value itself but "
        "rather coherence traffic. To fix this, you would typically"
    ),
    # Random fragment with no closure.
    (
        "the second category of optimisation involves rearranging the data "
        "layout so that fields accessed together share a cache line, while "
        "fields accessed independently do not. The general"
    ),
]


# ── Acceptance test ────────────────────────────────────────────────


@pytest.mark.live_llm
def test_scorer_separates_good_from_bad_by_at_least_3():
    """Real LLM call. Scorer must give GOOD chunks at least 3 points higher
    than BAD chunks (median-of-3 per chunk, then median across chunks)."""
    # Skip if env doesn't even have a provider set — better than a confusing error.
    if not os.getenv("LLM_PROVIDER"):
        pytest.skip("LLM_PROVIDER env not set; cannot run live test")

    from services.llm_client import LLMClient

    client = LLMClient()

    def score_one(text):
        return coherence_score(text, score_fn=client.score_text_quality, runs=3)

    good_scores = [score_one(t).score for t in GOOD_CHUNKS]
    bad_scores = [score_one(t).score for t in BAD_CHUNKS]

    print(f"\nGOOD chunk scores: {good_scores}")
    print(f"BAD  chunk scores: {bad_scores}")

    good_median = statistics.median(good_scores)
    bad_median = statistics.median(bad_scores)
    gap = good_median - bad_median

    print(f"Good median = {good_median}, Bad median = {bad_median}, gap = {gap}")

    assert gap >= 3, (
        f"Scorer cannot differentiate good vs bad chunks. "
        f"Good median {good_median}, bad median {bad_median}, gap {gap}. "
        f"Either tune _CHUNK_COHERENCE_PROMPTS['v1'] or pick a more capable model."
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--run-live-llm"])
