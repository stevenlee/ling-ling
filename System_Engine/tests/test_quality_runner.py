"""Unit tests for quality_runner.coherence_score — no real LLM needed."""

import pytest

from tests.quality_runner import ChunkScore, coherence_score, median_corpus_score


def _stub(scores_in_order):
    """Returns a callable that yields the given scores in sequence."""
    iterator = iter(scores_in_order)

    def fn(text):
        s = next(iterator)
        return {"score": s, "reason": f"stub-{s}", "prompt_version": "v1"}

    return fn


class TestCoherenceScore:
    def test_median_of_three(self):
        result = coherence_score("any chunk", score_fn=_stub([6, 8, 7]), runs=3)
        assert result.score == 7
        assert result.raw_scores == (6, 8, 7)
        assert result.prompt_version == "v1"

    def test_single_run(self):
        result = coherence_score("any", score_fn=_stub([5]), runs=1)
        assert result.score == 5

    def test_one_failure_doesnt_dominate(self):
        # Score=0 = failure. Median of [0, 8, 9] is 8 — failure dropped.
        result = coherence_score("any", score_fn=_stub([0, 8, 9]), runs=3)
        assert result.score == 8

    def test_all_failures_returns_zero(self):
        result = coherence_score("any", score_fn=_stub([0, 0, 0]), runs=3)
        assert result.score == 0

    def test_empty_runs_raises(self):
        with pytest.raises(ValueError):
            coherence_score("any", score_fn=_stub([]), runs=0)

    def test_reasons_captured(self):
        result = coherence_score("any", score_fn=_stub([5, 6]), runs=2)
        assert result.reasons == ("stub-5", "stub-6")

    def test_handles_missing_keys(self):
        def fn(text):
            return {}  # no score, no reason — should not crash

        result = coherence_score("any", score_fn=fn, runs=1)
        assert result.score == 0


class TestMedianCorpusScore:
    def test_empty(self):
        assert median_corpus_score([]) == 0.0

    def test_median(self):
        chunks = [
            ChunkScore(score=7, raw_scores=(7,), reasons=("",), prompt_version="v1"),
            ChunkScore(score=8, raw_scores=(8,), reasons=("",), prompt_version="v1"),
            ChunkScore(score=6, raw_scores=(6,), reasons=("",), prompt_version="v1"),
        ]
        assert median_corpus_score(chunks) == 7.0


class TestScoreTextQualityContract:
    """Lightweight contract check: when LLMClient.score_text_quality is
    imported and given bogus input, it returns the documented schema
    rather than raising."""

    def test_unknown_prompt_version_returns_score_zero(self, monkeypatch):
        import os

        os.environ.setdefault("LLM_PROVIDER", "vllm")
        from services.llm_client import LLMClient

        agent = LLMClient.__new__(LLMClient)
        result = agent.score_text_quality("any text", prompt_version="v999")
        assert result["score"] == 0
        assert "unknown" in result["reason"].lower()
        assert result["prompt_version"] == "v999"

    def test_empty_text_returns_score_zero(self):
        import os

        os.environ.setdefault("LLM_PROVIDER", "vllm")
        from services.llm_client import LLMClient

        agent = LLMClient.__new__(LLMClient)
        for empty in ("", "   ", None):
            result = agent.score_text_quality(empty)
            assert result["score"] == 0, f"failed for {empty!r}"

    def test_llm_failure_returns_score_zero(self, monkeypatch):
        """If the underlying _complete_text raises, we shouldn't crash."""
        import os

        os.environ.setdefault("LLM_PROVIDER", "vllm")
        from services.llm_client import LLMClient

        agent = LLMClient.__new__(LLMClient)

        # Inject a failing _complete_text
        def boom(*a, **kw):
            raise RuntimeError("llm unreachable")

        agent._complete_text = boom
        result = agent.score_text_quality("hello world")
        assert result["score"] == 0
        assert "llm error" in result["reason"].lower()

    def test_clamps_out_of_range_score(self):
        """If the LLM returns 15 or -3, we clamp to [1, 10]."""
        import os

        os.environ.setdefault("LLM_PROVIDER", "vllm")
        from services.llm_client import LLMClient

        agent = LLMClient.__new__(LLMClient)
        agent._complete_text = lambda **kw: '{"score": 15, "reason": "too high"}'
        result = agent.score_text_quality("hello")
        assert result["score"] == 10

        agent._complete_text = lambda **kw: '{"score": -3, "reason": "too low"}'
        result = agent.score_text_quality("hello")
        assert result["score"] == 1

    def test_handles_non_numeric_score(self):
        import os

        os.environ.setdefault("LLM_PROVIDER", "vllm")
        from services.llm_client import LLMClient

        agent = LLMClient.__new__(LLMClient)
        agent._complete_text = lambda **kw: '{"score": "high", "reason": "x"}'
        result = agent.score_text_quality("hello")
        assert result["score"] == 0
        assert "non-numeric" in result["reason"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
