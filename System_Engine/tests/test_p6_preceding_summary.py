"""P6 — preceding_summary (opt-in LLM context preamble).

Strategy mirrors P5:

* Test `summarize_for_context` contract with synthesized inputs (no LLM).
* Test ThoughtfulSplitter wiring with a stub LLM injected at construction.
* Cover every failure mode — bad JSON, LLM raise, missing LLM — must
  degrade silently to "no summary" (empty string).
* Test the cache: memory hit, disk hit, empty answer is also cached
  (so we don't keep asking about chunks the LLM declined to summarize).
* Test the mutual exclusion: `emit_summary=True` disables structural overlap.

P6 must never make ingestion worse than P3 — same acceptance gate as P5.
"""

import os

os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from services.thoughtful_splitter import (
    ThoughtfulSplitter,
    _ContentHashCache,
    _SUMMARY_OPEN,
    _SUMMARY_CLOSE,
    _OVERLAP_OPEN,
)


# ─── Test helpers ───────────────────────────────────────────────────


class StubSummaryLLM:
    """Stub LLM that scripts `summarize_for_context` returns + records calls."""

    def __init__(self, responses=None, raises=None):
        self.responses = list(responses or [{"summary": "Stub summary."}])
        self.raises = list(raises or [])
        self.calls = []

    def summarize_for_context(self, text, prompt_version="v1", max_chars=200):
        self.calls.append(text)
        if self.raises:
            exc = self.raises.pop(0) if len(self.raises) > 1 else self.raises[0]
            raise exc
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    # ThoughtfulSplitter only calls summarize_for_context here, but a real
    # LLMClient also exposes find_topic_shifts — make it a no-op so P5
    # eligibility doesn't blow up if invoked by accident.
    def find_topic_shifts(self, paragraphs, prompt_version="v1"):
        return {"split_after": [], "prompt_version": prompt_version}


def _long_paragraph_text(n_paragraphs=8, words_per_paragraph=40):
    paras = []
    for i in range(n_paragraphs):
        text = " ".join(f"word{i}-{j}" for j in range(words_per_paragraph))
        paras.append(text + ".")
    return "\n\n".join(paras) + "\n"


def _splitter(stub_llm=None, **kw):
    defaults = {
        "target_size": 1500,
        "max_size": 2500,
        "min_size": 400,
        "snap_window": 600,
        "overlap_chars": 300,
        "llm": stub_llm,
    }
    defaults.update(kw)
    return ThoughtfulSplitter(**defaults)


# ─── summarize_for_context contract ─────────────────────────────────


class TestSummarizeForContextContract:
    def setup_method(self):
        from services.llm_client import LLMClient

        self.client = LLMClient.__new__(LLMClient)

    def test_empty_input_returns_empty_without_llm_call(self):
        for empty in ("", "   ", None):
            result = self.client.summarize_for_context(empty)
            assert result["summary"] == ""

    def test_unknown_prompt_version_returns_empty(self):
        result = self.client.summarize_for_context("real text here", prompt_version="v999")
        assert result["summary"] == ""

    def test_llm_failure_returns_empty(self):
        def boom(**kw):
            raise RuntimeError("LLM down")

        self.client._complete_text = boom
        result = self.client.summarize_for_context("real text")
        assert result["summary"] == ""

    def test_non_string_summary_in_response_returns_empty(self):
        self.client._complete_text = lambda **kw: '{"summary": 12345}'
        result = self.client.summarize_for_context("text")
        assert result["summary"] == ""

    def test_truncates_to_max_chars(self):
        long = "x" * 500
        self.client._complete_text = lambda **kw: f'{{"summary": "{long}"}}'
        result = self.client.summarize_for_context("text", max_chars=200)
        assert len(result["summary"]) == 200
        assert result["summary"].endswith("…")

    def test_collapses_internal_whitespace(self):
        self.client._complete_text = lambda **kw: '{"summary": "Multi\\n\\nline\\twith   spaces."}'
        result = self.client.summarize_for_context("text")
        # Whitespace runs collapsed to single spaces.
        assert "  " not in result["summary"]
        assert "\n" not in result["summary"]
        assert "\t" not in result["summary"]

    def test_passes_through_short_summary_intact(self):
        self.client._complete_text = lambda **kw: '{"summary": "A brief note."}'
        result = self.client.summarize_for_context("text")
        assert result["summary"] == "A brief note."


# ─── No LLM configured ─────────────────────────────────────────────


class TestNoLlm:
    def test_emit_summary_true_but_no_llm_yields_empty_summaries(self):
        text = _long_paragraph_text()
        s = _splitter(stub_llm=None)
        chunks = s.split_thoughtful(text, use_llm=True, emit_summary=True)
        assert all(c.preceding_summary == "" for c in chunks)
        # And: no <!-- summary --> markers in the chunk texts.
        assert all(_SUMMARY_OPEN not in c.text for c in chunks)


# ─── Happy paths ───────────────────────────────────────────────────


class TestHappyPath:
    def test_first_chunk_summary_empty(self):
        text = _long_paragraph_text(n_paragraphs=12)
        stub = StubSummaryLLM(responses=[{"summary": "First summary."}])
        s = _splitter(stub_llm=stub, target_size=500, max_size=2000, min_size=100, snap_window=200)
        chunks = s.split_thoughtful(text, use_llm=False, emit_summary=True)
        assert chunks[0].preceding_summary == ""
        assert _SUMMARY_OPEN not in chunks[0].text

    def test_later_chunks_have_summary(self):
        text = _long_paragraph_text(n_paragraphs=12)
        stub = StubSummaryLLM(
            responses=[
                {"summary": "Summary for chunk 1."},
                {"summary": "Summary for chunk 2."},
            ]
        )
        s = _splitter(stub_llm=stub, target_size=500, max_size=2000, min_size=100, snap_window=200)
        chunks = s.split_thoughtful(text, use_llm=False, emit_summary=True)
        assert len(chunks) >= 2
        for c in chunks[1:]:
            assert c.preceding_summary, f"chunk at {c.start} has empty preceding_summary"
            assert _SUMMARY_OPEN in c.text
            assert _SUMMARY_CLOSE in c.text

    def test_emit_summary_disables_overlap(self):
        text = _long_paragraph_text(n_paragraphs=12)
        stub = StubSummaryLLM(responses=[{"summary": "Summary."}])
        s = _splitter(
            stub_llm=stub,
            target_size=500,
            max_size=2000,
            min_size=100,
            snap_window=200,
            overlap_chars=300,
        )
        chunks = s.split_thoughtful(text, use_llm=False, emit_summary=True)
        for c in chunks:
            assert c.overlap_chars == 0, "overlap_chars should be 0 when summary is on"
            assert _OVERLAP_OPEN not in c.text, "no overlap block when summary is on"

    def test_chunks_remain_contiguous_with_summary(self):
        text = _long_paragraph_text(n_paragraphs=12)
        stub = StubSummaryLLM(responses=[{"summary": "Summary."}])
        s = _splitter(stub_llm=stub, target_size=500, max_size=2000, min_size=100, snap_window=200)
        chunks = s.split_thoughtful(text, use_llm=False, emit_summary=True)
        cursor = 0
        for c in chunks:
            assert c.start == cursor, f"gap at chunk start {c.start}"
            cursor = c.end
        assert cursor == len(text)

    def test_summary_text_appears_in_chunk_text(self):
        text = _long_paragraph_text(n_paragraphs=12)
        stub = StubSummaryLLM(responses=[{"summary": "A distinctive summary fact."}])
        s = _splitter(stub_llm=stub, target_size=500, max_size=2000, min_size=100, snap_window=200)
        chunks = s.split_thoughtful(text, use_llm=False, emit_summary=True)
        assert "A distinctive summary fact." in chunks[1].text


# ─── Failure modes ────────────────────────────────────────────────


class TestFailureModes:
    def test_llm_raises_falls_back_to_empty_summary(self, caplog):
        text = _long_paragraph_text(n_paragraphs=12)
        stub = StubSummaryLLM(raises=[RuntimeError("LLM unreachable")])
        s = _splitter(stub_llm=stub, target_size=500, max_size=2000, min_size=100, snap_window=200)
        chunks = s.split_thoughtful(text, use_llm=False, emit_summary=True)
        assert all(c.preceding_summary == "" for c in chunks)
        assert any("summary llm" in r.message.lower() for r in caplog.records)

    def test_bad_response_shape_yields_empty(self):
        text = _long_paragraph_text(n_paragraphs=12)
        stub = StubSummaryLLM(responses=["not a dict"])
        s = _splitter(stub_llm=stub, target_size=500, max_size=2000, min_size=100, snap_window=200)
        chunks = s.split_thoughtful(text, use_llm=False, emit_summary=True)
        assert all(c.preceding_summary == "" for c in chunks)


# ─── Cache ─────────────────────────────────────────────────────────


class TestCache:
    def test_repeated_chunk_hits_cache(self):
        text = _long_paragraph_text(n_paragraphs=12)
        stub = StubSummaryLLM(responses=[{"summary": "S."}])
        s = _splitter(stub_llm=stub, target_size=500, max_size=2000, min_size=100, snap_window=200)
        s.split_thoughtful(text, use_llm=False, emit_summary=True)
        first = len(stub.calls)
        s.split_thoughtful(text, use_llm=False, emit_summary=True)
        assert len(stub.calls) == first

    def test_empty_answer_is_also_cached(self):
        """If LLM declined / failed, don't keep asking."""
        text = _long_paragraph_text(n_paragraphs=12)
        # First call raises, but subsequent splits shouldn't ask again
        # for the same prev-chunk text — that's the point of caching empty.
        stub = StubSummaryLLM(raises=[RuntimeError("once")])
        s = _splitter(stub_llm=stub, target_size=500, max_size=2000, min_size=100, snap_window=200)
        s.split_thoughtful(text, use_llm=False, emit_summary=True)
        first_calls = len(stub.calls)

        # Reset stub.raises so a second call WOULD return a real summary
        # if reached — proving the cache is what suppresses the call.
        stub.raises = []
        stub.responses = [{"summary": "would be returned"}]
        s.split_thoughtful(text, use_llm=False, emit_summary=True)
        assert len(stub.calls) == first_calls, "empty cache entry should suppress retry"

    def test_disk_cache_persists(self, tmp_path):
        text = _long_paragraph_text(n_paragraphs=12)
        stub1 = StubSummaryLLM(responses=[{"summary": "Persisted."}])
        s1 = _splitter(
            stub_llm=stub1,
            cache_dir=tmp_path,
            target_size=500,
            max_size=2000,
            min_size=100,
            snap_window=200,
        )
        s1.split_thoughtful(text, use_llm=False, emit_summary=True)
        first = len(stub1.calls)
        assert first >= 1

        # New splitter, new stub, same disk cache.
        stub2 = StubSummaryLLM(responses=[{"summary": "WOULD BE NEW"}])
        s2 = _splitter(
            stub_llm=stub2,
            cache_dir=tmp_path,
            target_size=500,
            max_size=2000,
            min_size=100,
            snap_window=200,
        )
        chunks = s2.split_thoughtful(text, use_llm=False, emit_summary=True)
        assert stub2.calls == [], "second splitter should hit disk cache"
        # And the cached value should be applied:
        assert any("Persisted." in c.text for c in chunks)

    def test_disk_cache_uses_separate_subdirs_from_topic_shifts(self, tmp_path):
        """Summary and topic-shift caches must not collide on disk —
        same content hash, different value types."""
        c_topic = _ContentHashCache(tmp_path, subdir="topic_shifts")
        c_summary = _ContentHashCache(tmp_path, subdir="summaries")

        # Same key text → different keyspaces on disk.
        c_topic.put("shared key text", [1, 2])
        c_summary.put("shared key text", "a short summary")

        assert c_topic.get("shared key text") == [1, 2]
        assert c_summary.get("shared key text") == "a short summary"


# ─── _ContentHashCache directly ────────────────────────────────────


class TestContentHashCacheGeneric:
    def test_round_trip_string_value(self, tmp_path):
        c = _ContentHashCache(tmp_path)
        c.put("k", "value as string")
        assert c.get("k") == "value as string"
        # Disk read in a fresh instance:
        c2 = _ContentHashCache(tmp_path)
        assert c2.get("k") == "value as string"

    def test_round_trip_list_value(self, tmp_path):
        c = _ContentHashCache(tmp_path)
        c.put("k", [1, 2, 3])
        assert c.get("k") == [1, 2, 3]
        c2 = _ContentHashCache(tmp_path)
        assert c2.get("k") == [1, 2, 3]

    def test_round_trip_dict_value(self, tmp_path):
        c = _ContentHashCache(tmp_path)
        c.put("k", {"a": 1, "b": "two"})
        assert c.get("k") == {"a": 1, "b": "two"}

    def test_corrupt_disk_payload_treated_as_miss(self, tmp_path):
        from hashlib import sha256

        key = sha256("test".encode()).hexdigest()
        (tmp_path / f"{key}.json").write_text("malformed json {{")
        c = _ContentHashCache(tmp_path)
        assert c.get("test") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
