"""P5 — LLM topic refinement.

The hardest phase to test because the LLM is non-deterministic. Strategy:

* The `find_topic_shifts` LLM contract is locked in `llm_client.py`. We test
  its **validation logic** with synthesized inputs (no LLM needed).
* The Phase-4 wiring in `ThoughtfulSplitter` is tested with a **stub LLM**
  injected at construction time, so behaviour is fully deterministic.
* We test every failure mode: stub returns junk, stub raises, no LLM
  configured, oversized splits, splits that hit atomic blocks, splits
  outside chunk range, splits producing tiny fragments.
* We test the content-hash cache: memory hit, disk hit, key changes when
  text changes, malformed disk entries don't crash the read path.

The acceptance gate is: **every failure path degrades silently to the
P3 result.** P5 must never make ingestion worse.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from services.md_block_scanner import BlockKind, scan
from services.thoughtful_splitter import (
    BoundaryKind,
    ThoughtfulSplitter,
    _TopicShiftCache,
)


# ─── Test helpers ─────────────────────────────────────────────────────

class StubLLM:
    """Records every find_topic_shifts call and returns scripted answers."""

    def __init__(self, responses=None, raises=None):
        # `responses`: list of return-values, consumed in order; if exhausted,
        # the last one repeats. `raises`: list of exceptions to raise instead.
        self.responses = list(responses or [{"split_after": []}])
        self.raises = list(raises or [])
        self.calls = []  # list of (paragraphs,) tuples

    def find_topic_shifts(self, paragraphs, prompt_version="v1"):
        self.calls.append(paragraphs)
        if self.raises:
            exc = self.raises.pop(0) if len(self.raises) > 1 else self.raises[0]
            raise exc
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def _long_paragraph_chunk(n_paragraphs=8, words_per_paragraph=50):
    """Build a long PARAGRAPH-only chunk for triggering P5."""
    paras = []
    for i in range(n_paragraphs):
        # Make each paragraph distinct enough that a real LLM could find topics.
        topic = "alpha" if i < n_paragraphs // 2 else "beta"
        text = " ".join(f"{topic}-{i}-{j}" for j in range(words_per_paragraph))
        paras.append(text + ".")
    return "\n\n".join(paras) + "\n"


def _splitter(stub_llm=None, **kw):
    """Splitter tuned to trigger P5 on a ~3-4KB unstructured input."""
    defaults = {
        "target_size": 1500,
        "max_size": 2500,
        "min_size": 400,
        "snap_window": 600,
        "overlap_chars": 0,
        "llm": stub_llm,
        "default_use_llm": True,
    }
    defaults.update(kw)
    return ThoughtfulSplitter(**defaults)


# ─── _TopicShiftCache ────────────────────────────────────────────────

class TestTopicShiftCacheMemory:
    def test_round_trip(self):
        c = _TopicShiftCache()
        assert c.get("hello world") is None
        c.put("hello world", [2, 4])
        assert c.get("hello world") == [2, 4]

    def test_different_keys(self):
        c = _TopicShiftCache()
        c.put("alpha", [1])
        c.put("beta", [3])
        assert c.get("alpha") == [1]
        assert c.get("beta") == [3]

    def test_unicode_safe(self):
        c = _TopicShiftCache()
        c.put("中文段落", [2])
        assert c.get("中文段落") == [2]


class TestTopicShiftCacheDisk:
    def test_disk_round_trip(self, tmp_path):
        c1 = _TopicShiftCache(tmp_path)
        c1.put("hello", [1, 2])
        # A FRESH cache instance reads from disk.
        c2 = _TopicShiftCache(tmp_path)
        assert c2.get("hello") == [1, 2]

    def test_persists_empty_answer(self, tmp_path):
        """Caching an empty result IS valuable — it means 'asked, no shifts'."""
        c1 = _TopicShiftCache(tmp_path)
        c1.put("text without shifts", [])
        c2 = _TopicShiftCache(tmp_path)
        assert c2.get("text without shifts") == []

    def test_content_hash_invalidates_on_text_change(self, tmp_path):
        c = _TopicShiftCache(tmp_path)
        c.put("original", [3])
        assert c.get("edited") is None  # different text = different hash = miss

    def test_unparseable_disk_entry_doesnt_crash(self, tmp_path):
        """A corrupted JSON file is treated as a cache miss, not an error."""
        from hashlib import sha256
        key = sha256("test".encode()).hexdigest()
        (tmp_path / f"{key}.json").write_text("not valid json {{{")
        c = _TopicShiftCache(tmp_path)
        assert c.get("test") is None

    def test_uncreatable_cache_dir_falls_back_to_memory(self, tmp_path, caplog):
        # Use a file-as-directory to make mkdir fail.
        bad = tmp_path / "blocker"
        bad.write_text("I'm a file, not a dir")
        c = _TopicShiftCache(bad)
        # Despite the bad disk path, memory still works.
        c.put("hello", [1])
        assert c.get("hello") == [1]


# ─── No LLM configured ───────────────────────────────────────────────

class TestNoLlmConfigured:
    def test_use_llm_true_but_no_llm_is_silent_noop(self):
        text = _long_paragraph_chunk()
        s = _splitter(stub_llm=None)  # llm=None
        chunks = s.split_thoughtful(text, use_llm=True)
        # No LLM_TOPIC_SHIFT boundary should appear.
        assert not any(c.boundary_type == BoundaryKind.LLM_TOPIC_SHIFT for c in chunks)

    def test_use_llm_false_skips_llm_even_when_configured(self):
        stub = StubLLM(responses=[{"split_after": [4]}])
        text = _long_paragraph_chunk()
        s = _splitter(stub_llm=stub)
        chunks = s.split_thoughtful(text, use_llm=False)
        assert stub.calls == [], "LLM was called despite use_llm=False"


# ─── Eligibility ──────────────────────────────────────────────────────

class TestEligibility:
    def test_short_chunks_not_refined(self):
        """Chunk under 1.2 × target_size is not eligible."""
        # One paragraph chunk that fits in one P3 chunk.
        text = "A short note.\n\nA short tail.\n"
        stub = StubLLM(responses=[{"split_after": [1]}])
        s = _splitter(stub_llm=stub)
        s.split_thoughtful(text, use_llm=True)
        assert stub.calls == [], "LLM was called on a short chunk"

    def test_chunks_with_headings_not_refined(self):
        """Even if size triggers, a chunk containing a heading is skipped."""
        # Carefully construct a single P3 chunk that contains an H2.
        body = "Some paragraph text. " * 200
        text = f"# Top\n\n{body}\n\n## Sub\n\n{body}\n"
        stub = StubLLM(responses=[{"split_after": [1]}])
        s = _splitter(stub_llm=stub, target_size=8000, max_size=20000, min_size=500, snap_window=1000)
        s.split_thoughtful(text, use_llm=True)
        # The chunk spans the heading, so P5 should skip it.
        # NB: this also exercises the eligibility-2 (no heading) gate.
        assert stub.calls == [], f"LLM called despite chunk containing heading: {stub.calls}"

    def test_chunks_with_code_fence_not_refined(self):
        """Atomic-content chunks (code/table/list/callout) are not eligible."""
        body = "Some paragraph text. " * 200
        code = "```py\n" + "x = 1\n" * 50 + "```"
        text = f"{body}\n\n{code}\n\n{body}\n"
        stub = StubLLM(responses=[{"split_after": [1]}])
        s = _splitter(stub_llm=stub, target_size=8000, max_size=20000, min_size=500, snap_window=1000)
        s.split_thoughtful(text, use_llm=True)
        assert stub.calls == [], f"LLM called on chunk containing code fence: {stub.calls}"

    def test_paragraph_only_long_chunk_is_refined(self):
        text = _long_paragraph_chunk(n_paragraphs=8)
        stub = StubLLM(responses=[{"split_after": [4]}])
        # Make max_size big enough that the whole chunk fits in ONE P3 chunk
        # (otherwise the structural splitter beats P5 to the punch).
        s = _splitter(stub_llm=stub, target_size=2000, max_size=10000, min_size=400, snap_window=600)
        chunks = s.split_thoughtful(text, use_llm=True)
        assert len(stub.calls) >= 1, "LLM should have been called on eligible chunk"
        # And the response should have created a split.
        assert any(c.boundary_type == BoundaryKind.LLM_TOPIC_SHIFT for c in chunks)


# ─── Happy paths ─────────────────────────────────────────────────────

class TestHappyPath:
    def test_single_split_applied(self):
        text = _long_paragraph_chunk(n_paragraphs=8)
        stub = StubLLM(responses=[{"split_after": [4]}])
        s = _splitter(stub_llm=stub, target_size=2000, max_size=10000, min_size=400, snap_window=600)
        chunks = s.split_thoughtful(text, use_llm=True)
        topic_chunks = [c for c in chunks if c.boundary_type == BoundaryKind.LLM_TOPIC_SHIFT]
        assert len(topic_chunks) == 1
        # Verify the split actually divided the original chunk.
        assert topic_chunks[0].start == 0
        assert topic_chunks[0].end > 0
        assert topic_chunks[0].end < len(text)

    def test_two_splits_applied(self):
        text = _long_paragraph_chunk(n_paragraphs=12)
        stub = StubLLM(responses=[{"split_after": [4, 8]}])
        s = _splitter(stub_llm=stub, target_size=4000, max_size=20000, min_size=400, snap_window=600)
        chunks = s.split_thoughtful(text, use_llm=True)
        # Should have inserted 2 LLM_TOPIC_SHIFT boundaries → 3 segments from this chunk.
        topic_chunks = [c for c in chunks if c.boundary_type == BoundaryKind.LLM_TOPIC_SHIFT]
        assert len(topic_chunks) == 2

    def test_empty_response_means_no_split(self):
        text = _long_paragraph_chunk(n_paragraphs=8)
        stub = StubLLM(responses=[{"split_after": []}])
        s = _splitter(stub_llm=stub, target_size=2000, max_size=10000, min_size=400, snap_window=600)
        chunks = s.split_thoughtful(text, use_llm=True)
        assert not any(c.boundary_type == BoundaryKind.LLM_TOPIC_SHIFT for c in chunks)

    def test_chunks_remain_contiguous_after_split(self):
        text = _long_paragraph_chunk(n_paragraphs=8)
        stub = StubLLM(responses=[{"split_after": [4]}])
        s = _splitter(stub_llm=stub, target_size=2000, max_size=10000, min_size=400, snap_window=600)
        chunks = s.split_thoughtful(text, use_llm=True)
        cursor = 0
        for c in chunks:
            assert c.start == cursor, f"gap before chunk {c.start} (expected {cursor})"
            cursor = c.end
        assert cursor == len(text)


# ─── Failure modes ───────────────────────────────────────────────────

class TestFailureModes:
    def test_llm_raises_falls_back_silently(self, caplog):
        text = _long_paragraph_chunk(n_paragraphs=8)
        stub = StubLLM(raises=[RuntimeError("LLM unreachable")])
        s = _splitter(stub_llm=stub, target_size=2000, max_size=10000, min_size=400, snap_window=600)
        chunks = s.split_thoughtful(text, use_llm=True)
        # No LLM_TOPIC_SHIFT boundary; the chunks are whatever P3 produced.
        assert not any(c.boundary_type == BoundaryKind.LLM_TOPIC_SHIFT for c in chunks)
        # Should have logged a warning but not propagated the exception.
        assert any("topic-shift llm" in r.message.lower() for r in caplog.records)

    def test_bad_response_shape_degrades_to_p3(self):
        """LLM returns a non-dict (e.g. just a string) — should be no-op."""
        text = _long_paragraph_chunk(n_paragraphs=8)
        stub = StubLLM(responses=["not a dict"])
        s = _splitter(stub_llm=stub, target_size=2000, max_size=10000, min_size=400, snap_window=600)
        chunks = s.split_thoughtful(text, use_llm=True)
        assert not any(c.boundary_type == BoundaryKind.LLM_TOPIC_SHIFT for c in chunks)

    def test_out_of_range_indices_filtered(self):
        """LLM returns indices outside [1, N-1] — they get rejected silently."""
        text = _long_paragraph_chunk(n_paragraphs=6)
        # All indices are out of [1, 5].
        stub = StubLLM(responses=[{"split_after": [0, 6, 99]}])
        s = _splitter(stub_llm=stub, target_size=2000, max_size=10000, min_size=400, snap_window=600)
        chunks = s.split_thoughtful(text, use_llm=True)
        assert not any(c.boundary_type == BoundaryKind.LLM_TOPIC_SHIFT for c in chunks)

    def test_tiny_fragment_split_rejected(self):
        """If a proposed split would produce a fragment smaller than ~min_size/2,
        reject it (avoid 30-character useless slivers)."""
        # 50-word paragraphs → each para is ~350 chars. Splitting after P1
        # produces a ~350-char first sub-chunk. With min_size=1000, the
        # tiny-cutoff is 500 chars, so this split should be rejected.
        text = _long_paragraph_chunk(n_paragraphs=8, words_per_paragraph=50)
        stub = StubLLM(responses=[{"split_after": [1]}])
        s = _splitter(
            stub_llm=stub,
            target_size=4000, max_size=20000,
            min_size=1000, snap_window=1000,
        )
        chunks = s.split_thoughtful(text, use_llm=True)
        assert not any(c.boundary_type == BoundaryKind.LLM_TOPIC_SHIFT for c in chunks)


# ─── Cache integration ────────────────────────────────────────────────

class TestCacheIntegration:
    def test_repeated_chunk_hits_cache(self):
        text = _long_paragraph_chunk(n_paragraphs=8)
        stub = StubLLM(responses=[{"split_after": [4]}])
        s = _splitter(stub_llm=stub, target_size=2000, max_size=10000, min_size=400, snap_window=600)
        s.split_thoughtful(text, use_llm=True)
        first_calls = len(stub.calls)
        # Run again with the same text — second call should NOT increment.
        s.split_thoughtful(text, use_llm=True)
        assert len(stub.calls) == first_calls, "second call should be served from cache"

    def test_different_text_misses_cache(self):
        stub = StubLLM(responses=[{"split_after": [4]}])
        s = _splitter(stub_llm=stub, target_size=2000, max_size=10000, min_size=400, snap_window=600)
        s.split_thoughtful(_long_paragraph_chunk(n_paragraphs=8), use_llm=True)
        s.split_thoughtful(_long_paragraph_chunk(n_paragraphs=10), use_llm=True)
        assert len(stub.calls) == 2, "different chunk text must miss cache"

    def test_disk_cache_persists_across_splitter_instances(self, tmp_path):
        text = _long_paragraph_chunk(n_paragraphs=8)
        stub1 = StubLLM(responses=[{"split_after": [4]}])
        s1 = _splitter(stub_llm=stub1, cache_dir=tmp_path,
                       target_size=2000, max_size=10000, min_size=400, snap_window=600)
        s1.split_thoughtful(text, use_llm=True)
        assert len(stub1.calls) == 1

        # Fresh splitter, fresh stub — should not call the LLM because disk cache hits.
        stub2 = StubLLM(responses=[{"split_after": [4]}])
        s2 = _splitter(stub_llm=stub2, cache_dir=tmp_path,
                       target_size=2000, max_size=10000, min_size=400, snap_window=600)
        chunks = s2.split_thoughtful(text, use_llm=True)
        assert stub2.calls == [], "second splitter should hit disk cache, not call LLM"
        assert any(c.boundary_type == BoundaryKind.LLM_TOPIC_SHIFT for c in chunks)


# ─── find_topic_shifts contract (lightweight, no real LLM) ────────────

class TestFindTopicShiftsContract:
    def setup_method(self):
        from services.llm_client import LLMClient
        self.client = LLMClient.__new__(LLMClient)

    def test_too_few_paragraphs_returns_empty_without_llm_call(self):
        """< 3 paragraphs → no possible useful split → no LLM call."""
        for input_ in ([], ["one"], ["one", "two"]):
            result = self.client.find_topic_shifts(input_)
            assert result["split_after"] == []

    def test_unknown_prompt_version_returns_empty(self):
        result = self.client.find_topic_shifts(["p1", "p2", "p3"], prompt_version="v999")
        assert result["split_after"] == []
        assert result["prompt_version"] == "v999"

    def test_llm_failure_returns_empty(self, caplog):
        """If the underlying _complete_text raises, no crash."""
        def boom(**kw):
            raise RuntimeError("dead")
        self.client._complete_text = boom
        result = self.client.find_topic_shifts(["p1", "p2", "p3"])
        assert result["split_after"] == []

    def test_validation_filters_out_of_range(self):
        v = type(self.client)._validate_topic_shifts
        # N=6 → valid range [1, 5]
        assert v([0, 3, 6, 7], 6) == [3]
        assert v([1, 5], 6) == [1, 5]

    def test_validation_caps_at_two(self):
        v = type(self.client)._validate_topic_shifts
        assert v([1, 2, 3, 4], 10) == [1, 2]

    def test_validation_dedupes_and_sorts(self):
        v = type(self.client)._validate_topic_shifts
        assert v([4, 2, 2, 3], 10) == [2, 3]

    def test_validation_rejects_non_int(self):
        v = type(self.client)._validate_topic_shifts
        assert v(["one", None, True, False, 2.5, 3], 10) == [2, 3]

    def test_validation_rejects_non_list_input(self):
        v = type(self.client)._validate_topic_shifts
        assert v("not a list", 10) == []
        assert v({"split_after": [1]}, 10) == []
        assert v(None, 10) == []


# ─── End-to-end via injected stub (sanity) ────────────────────────────

class TestEndToEndWithStub:
    def test_p5_runs_when_enabled_and_skips_when_disabled(self):
        """A single corpus document; same content, P5 on vs off."""
        text = _long_paragraph_chunk(n_paragraphs=10, words_per_paragraph=40)

        stub_on = StubLLM(responses=[{"split_after": [5]}])
        s_on = _splitter(stub_llm=stub_on,
                         target_size=2000, max_size=10000, min_size=400, snap_window=600)
        chunks_on = s_on.split_thoughtful(text, use_llm=True)

        stub_off = StubLLM(responses=[{"split_after": [5]}])
        s_off = _splitter(stub_llm=stub_off,
                          target_size=2000, max_size=10000, min_size=400, snap_window=600)
        chunks_off = s_off.split_thoughtful(text, use_llm=False)

        assert stub_off.calls == []
        # ON should produce more chunks (one extra cut) than OFF.
        assert len(chunks_on) > len(chunks_off)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
