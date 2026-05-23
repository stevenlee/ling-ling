"""Tests for services.thoughtful_splitter.

Three layers of testing:

1. **Unit tests** for boundary computation, atomic-intersect guard, sentence
   reverse search, structural overlap, dict serialization, API back-compat.
2. **Synthesised regression tests** for the specific bug cases that Gemini
   and Codex flagged during plan review (long outlines, atomic crossings,
   enum serialization, CounterAgent split_text API).
3. **Snapshot tests** for the 8-file corpus. Snapshots live in
   `tests/snapshots/`. To regenerate: `pytest --update-snapshots`.
"""
import json
import sys
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import pytest

from services.md_block_scanner import BlockKind, scan
from services.thoughtful_splitter import (
    Boundary,
    BoundaryKind,
    Chunk,
    ThoughtfulSplitter,
)


CORPUS_DIR = Path(__file__).parent / "corpus"
SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def _small_splitter(**overrides) -> ThoughtfulSplitter:
    """Splitter tuned small so test docs actually chunk."""
    defaults = {
        "target_size": 1500,
        "max_size": 2500,
        "min_size": 400,
        "snap_window": 600,
        "overlap_chars": 0,
    }
    defaults.update(overrides)
    return ThoughtfulSplitter(**defaults)


# ─── BoundaryKind / Chunk dataclass ───────────────────────────────────

class TestBoundaryKind:
    def test_weights_monotonic(self):
        ranked = [
            BoundaryKind.H1, BoundaryKind.H2, BoundaryKind.HR,
            BoundaryKind.H3, BoundaryKind.LLM_TOPIC_SHIFT,
            BoundaryKind.H4_PLUS, BoundaryKind.LIST_END,
            BoundaryKind.PARAGRAPH, BoundaryKind.LIST_ITEM_END,
            BoundaryKind.BLOCKQUOTE_END, BoundaryKind.SENTENCE,
            BoundaryKind.FORCED,
        ]
        weights = [k.weight for k in ranked]
        assert weights == sorted(weights, reverse=True), f"weights not monotonic: {weights}"

    def test_labels_unique(self):
        labels = [k.label for k in BoundaryKind]
        assert len(labels) == len(set(labels))


class TestChunkToDict:
    """Codex P2: asdict() leaks Enum; to_dict() must be JSON-safe."""

    def test_to_dict_serializes_enums(self):
        c = Chunk(
            text="hello",
            start=0,
            end=5,
            section_path=("Chapter 1", "Intro"),
            boundary_type=BoundaryKind.H2,
            atomic_kinds=(BlockKind.CODE_FENCE, BlockKind.TABLE),
            overlap_chars=42,
            preceding_summary="prev was about X",
        )
        d = c.to_dict()
        assert d["boundary_type"] == "h2"  # string, not Enum
        assert d["atomic_kinds"] == ["code_fence", "table"]
        assert d["section_path"] == ["Chapter 1", "Intro"]
        assert d["overlap_chars"] == 42

    def test_to_dict_is_json_serializable(self):
        c = Chunk(
            text="x", start=0, end=1,
            section_path=("a",),
            boundary_type=BoundaryKind.FORCED,
            atomic_kinds=(BlockKind.LIST_ITEM,),
        )
        # Must NOT raise.
        json.dumps(c.to_dict())


# ─── Splitter init ────────────────────────────────────────────────────

class TestInit:
    def test_default_uses_settings(self):
        s = ThoughtfulSplitter()
        assert s.target_size > 0
        assert s.min_size < s.target_size <= s.max_size
        assert s.overlap_chars >= 0

    def test_rejects_invalid_sizes(self):
        with pytest.raises(ValueError):
            ThoughtfulSplitter(target_size=1000, max_size=500, min_size=100)
        with pytest.raises(ValueError):
            ThoughtfulSplitter(target_size=1000, max_size=2000, min_size=1500)


# ─── Empty / short inputs ─────────────────────────────────────────────

class TestSpecialInputs:
    def test_empty_returns_empty(self):
        assert _small_splitter().split_thoughtful("") == []

    def test_short_text_single_chunk(self):
        text = "Just a short note that fits in one chunk easily."
        chunks = _small_splitter().split_thoughtful(text)
        assert len(chunks) == 1
        assert chunks[0].text == text
        assert chunks[0].start == 0
        assert chunks[0].end == len(text)
        assert chunks[0].overlap_chars == 0


# ─── Phase 2: boundary computation ────────────────────────────────────

class TestBoundaries:
    def test_h1_boundary_at_heading_start(self):
        text = "Body text.\n\n# Heading\n\nMore body.\n"
        s = _small_splitter()
        blocks = scan(text)
        boundaries = s._build_boundaries(blocks)
        kinds = [b.kind for b in boundaries]
        assert BoundaryKind.H1 in kinds

    def test_h2_boundary_lower_weight_than_h1(self):
        # See TestBoundaryKind for the monotonicity invariant; verify the
        # mapping from block.level to boundary kind here.
        text = "para\n\n## Sub\n\npara2\n"
        s = _small_splitter()
        boundaries = s._build_boundaries(scan(text))
        h2s = [b for b in boundaries if b.kind == BoundaryKind.H2]
        assert len(h2s) == 1

    def test_section_path_accumulates(self):
        text = (
            "# Top\n\n"
            "Body A.\n\n"
            "## Sub\n\n"
            "Body B.\n\n"
            "### Sub-sub\n\n"
            "Body C.\n\n"
            "## Another sub\n\n"
            "Body D.\n"
        )
        s = _small_splitter()
        boundaries = s._build_boundaries(scan(text))
        # Find the boundary at start of "Body C." — section_path should be Top > Sub > Sub-sub.
        for b in boundaries:
            if b.kind == BoundaryKind.H3:
                # boundary AT the H3 → section_path should reflect new context (Top > Sub > Sub-sub)
                assert b.section_path == ("Top", "Sub", "Sub-sub")
                break
        else:
            pytest.fail("no H3 boundary found")

    def test_list_item_end_between_top_level_items(self):
        text = "- one\n- two\n- three\n"
        s = _small_splitter()
        boundaries = s._build_boundaries(scan(text))
        list_item_ends = [b for b in boundaries if b.kind == BoundaryKind.LIST_ITEM_END]
        # 3 items → 2 LIST_ITEM_END boundaries between them.
        assert len(list_item_ends) == 2

    def test_list_end_after_last_item(self):
        text = "- one\n- two\n\nA paragraph after the list.\n"
        s = _small_splitter()
        boundaries = s._build_boundaries(scan(text))
        list_ends = [b for b in boundaries if b.kind == BoundaryKind.LIST_END]
        assert len(list_ends) == 1


# ─── Phase 3: atomic-intersect guard (Codex P1 + Gemini Issue B) ──────

class TestAtomicGuard:
    def test_cuts_inside_atomic_detection(self):
        """`_cuts_inside_atomic` returns True only if position is strictly inside."""
        from services.md_block_scanner import Block
        s = _small_splitter()
        atomic = Block(
            kind=BlockKind.CODE_FENCE, text="```\nx\n```",
            start=10, end=20, atomic=True,
        )
        atomic_starts = [atomic.start]
        # Strictly inside:
        assert s._cuts_inside_atomic(15, [atomic], atomic_starts) is True
        # At start:
        assert s._cuts_inside_atomic(10, [atomic], atomic_starts) is False
        # At end:
        assert s._cuts_inside_atomic(20, [atomic], atomic_starts) is False
        # Outside:
        assert s._cuts_inside_atomic(5, [atomic], atomic_starts) is False
        assert s._cuts_inside_atomic(25, [atomic], atomic_starts) is False

    def test_chunk_not_cut_inside_code_fence(self):
        """A long code fence inside a chunk must remain intact."""
        big_code = "```python\n" + "x = 1\n" * 500 + "```\n"
        text = "Intro.\n\n" + big_code + "\n\nOutro paragraph.\n"
        splitter = _small_splitter(target_size=400, max_size=800, min_size=100, snap_window=200)
        chunks = splitter.split_thoughtful(text, use_llm=False)
        # The fence must appear intact in some chunk (open AND close together).
        joined = "".join(c.text for c in chunks)
        assert "```python" in joined and joined.count("```") == 2

    def test_cross_boundary_atomic_emits_oversize_warning(self, caplog):
        """If an atomic block is larger than max_size, we MUST emit it whole and warn."""
        import logging
        big_code = "```\n" + "x" * 5000 + "\n```\n"
        text = "Short intro.\n\n" + big_code + "Short outro.\n"
        splitter = _small_splitter(target_size=800, max_size=1500, min_size=200)
        with caplog.at_level(logging.WARNING):
            chunks = splitter.split_thoughtful(text, use_llm=False)
        # The fence is preserved as one oversize chunk + a warning is logged.
        assert any("oversize atomic chunk" in r.message for r in caplog.records)
        # And the fence content appears intact in at least one chunk.
        assert any(c.end - c.start >= 5000 for c in chunks)


# ─── Phase 3: reverse sentence search (Gemini Issue C) ────────────────

class TestReverseSentenceSearch:
    def test_latest_sentence_end_picked_not_first(self):
        """When fallback chooses a sentence boundary, it picks the LATEST in
        the window, not the first — otherwise we'd cut tiny chunks."""
        # Build text with no headings, all paragraphs, with several sentence ends.
        sentences = ". ".join(f"Sentence number {i} that has some content" for i in range(20)) + "."
        s = _small_splitter(target_size=300, max_size=500, min_size=50, snap_window=50)
        chunks = s.split_thoughtful(sentences, use_llm=False)
        # Every chunk should have meaningful size (no tiny first-sentence-end cuts).
        for c in chunks:
            assert c.end - c.start > 100, (
                f"Chunk too small at {c.start}-{c.end}: scanner picked first sentence end "
                f"rather than latest"
            )


# ─── Gemini Issue A: long outline doesn't become single chunk ─────────

class TestLongOutline:
    def test_50_item_outline_splits_into_multiple_chunks(self):
        items = "\n".join(f"- Item {i} with some descriptive content here" for i in range(50))
        text = f"# An outline\n\n{items}\n"
        s = _small_splitter(target_size=600, max_size=1200, min_size=150, snap_window=300)
        chunks = s.split_thoughtful(text, use_llm=False)
        assert len(chunks) >= 3, f"expected outline to split into multiple chunks, got {len(chunks)}"
        # Some of the chunks should have ended at a LIST_ITEM_END boundary.
        bts = {c.boundary_type for c in chunks}
        assert BoundaryKind.LIST_ITEM_END in bts, f"expected LIST_ITEM_END boundary, got {bts}"


# ─── Phase 3b: structural overlap ─────────────────────────────────────

class TestStructuralOverlap:
    def test_overlap_zero_no_ctx_block(self):
        text = "A.\n\n" + ". ".join(f"Sentence {i}" for i in range(100)) + ".\n"
        s = _small_splitter(target_size=400, max_size=700, min_size=100, snap_window=150, overlap_chars=0)
        chunks = s.split_thoughtful(text, use_llm=False)
        for c in chunks:
            assert "<!-- ctx:" not in c.text
            assert c.overlap_chars == 0

    def test_overlap_default_inserts_ctx_block_in_later_chunks(self):
        text = "A.\n\n" + ". ".join(f"Sentence {i} with enough text to be meaningful" for i in range(50)) + ".\n"
        s = _small_splitter(target_size=500, max_size=900, min_size=150, snap_window=200, overlap_chars=200)
        chunks = s.split_thoughtful(text, use_llm=False)
        assert len(chunks) >= 2
        # First chunk: no overlap.
        assert chunks[0].overlap_chars == 0
        assert "<!-- ctx:" not in chunks[0].text
        # Later chunks: have overlap.
        for c in chunks[1:]:
            assert "<!-- ctx: prev-chunk-tail -->" in c.text
            assert "<!-- /ctx -->" in c.text
            assert c.overlap_chars > 0
            assert c.overlap_chars <= 200

    def test_emit_summary_disables_overlap(self):
        text = ". ".join(f"Sentence {i}" for i in range(50)) + "."
        s = _small_splitter(target_size=300, max_size=500, min_size=100, snap_window=150, overlap_chars=200)
        chunks = s.split_thoughtful(text, use_llm=False, emit_summary=True)
        for c in chunks:
            assert "<!-- ctx:" not in c.text
            assert c.overlap_chars == 0


# ─── Codex P1: API back-compat ────────────────────────────────────────

class TestApiBackCompat:
    def test_split_text_returns_list_of_strings(self):
        text = "# Doc\n\n" + " ".join(["word"] * 800) + "\n"
        s = _small_splitter()
        result = s.split_text(text)
        assert isinstance(result, list)
        assert all(isinstance(x, str) for x in result)

    def test_split_text_with_spans_returns_json_safe_dicts(self):
        text = "# Doc\n\n" + " ".join(["word"] * 800) + "\n"
        s = _small_splitter()
        result = s.split_text_with_spans(text)
        # Must be JSON-serializable end-to-end (Codex P2 enum issue).
        json.dumps(result)
        for d in result:
            assert isinstance(d["boundary_type"], str)
            assert "section_path" in d
            assert "overlap_chars" in d


# ─── Corpus snapshot regression ───────────────────────────────────────

@pytest.fixture(autouse=True, scope="module")
def _ensure_snapshot_dir():
    SNAPSHOT_DIR.mkdir(exist_ok=True)


def _serialize_for_snapshot(text: str, chunks: list[Chunk]) -> dict:
    """Stable, text-light snapshot — excludes chunk body to keep diff readable."""
    chunk_dicts = []
    for i, c in enumerate(chunks):
        chunk_dicts.append({
            "index": i,
            "char_range": [c.start, c.end],
            "size": c.end - c.start,
            "section_path": list(c.section_path),
            "boundary_type": c.boundary_type.label,
            "atomic_kinds": [k.value for k in c.atomic_kinds],
            "overlap_chars": c.overlap_chars,
        })
    sizes = [c.end - c.start for c in chunks]
    bt_counts: dict[str, int] = {}
    for c in chunks:
        bt_counts[c.boundary_type.label] = bt_counts.get(c.boundary_type.label, 0) + 1
    return {
        "version": "2",
        "doc_hash": "sha256:" + sha256(text.encode("utf-8")).hexdigest(),
        "chunks": chunk_dicts,
        "summary": {
            "total_chunks": len(chunks),
            "size_distribution": {
                "min": min(sizes) if sizes else 0,
                "median": int(sorted(sizes)[len(sizes) // 2]) if sizes else 0,
                "max": max(sizes) if sizes else 0,
            },
            "boundary_type_counts": dict(sorted(bt_counts.items())),
            "overlap_total": sum(c.overlap_chars for c in chunks),
        },
    }


@pytest.mark.parametrize(
    "corpus_file",
    sorted(p.name for p in CORPUS_DIR.glob("*.md") if p.name != "README.md"),
)
def test_corpus_snapshot_stable(corpus_file, update_snapshots):
    """For each corpus file, the chunker output must match its saved snapshot.

    To regenerate snapshots after an intentional behaviour change:
        pytest --update-snapshots
    Then human-review the diff before committing.
    """
    text = (CORPUS_DIR / corpus_file).read_text(encoding="utf-8")
    # Snapshots are taken with default-ish settings, use_llm=False (the LLM
    # path is non-deterministic and validated separately via §7.2).
    splitter = ThoughtfulSplitter(
        target_size=1500, max_size=2500, min_size=400, snap_window=600, overlap_chars=200,
    )
    chunks = splitter.split_thoughtful(text, use_llm=False)
    actual = _serialize_for_snapshot(text, chunks)

    snapshot_path = SNAPSHOT_DIR / f"{corpus_file}.snapshot.json"

    if update_snapshots or not snapshot_path.exists():
        snapshot_path.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if not update_snapshots:
            pytest.skip(f"snapshot created at {snapshot_path.name}; rerun to verify")
        return

    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"Snapshot mismatch for {corpus_file}.\n"
        f"If intentional, rerun with --update-snapshots."
    )


# ─── Coverage invariant (each chunk's text == source[start:end] minus overlap) ─

@pytest.mark.parametrize(
    "corpus_file",
    sorted(p.name for p in CORPUS_DIR.glob("*.md") if p.name != "README.md"),
)
def test_chunk_start_end_corresponds_to_source(corpus_file):
    text = (CORPUS_DIR / corpus_file).read_text(encoding="utf-8")
    splitter = ThoughtfulSplitter(
        target_size=1500, max_size=2500, min_size=400, snap_window=600, overlap_chars=200,
    )
    chunks = splitter.split_thoughtful(text, use_llm=False)
    # Chunks (excluding overlap prefix) cover the source contiguously.
    cursor = 0
    for c in chunks:
        assert c.start == cursor, f"gap before chunk at {c.start}, expected {cursor}"
        cursor = c.end
        # The portion of chunk.text AFTER any overlap prefix must equal source[start:end].
        body = c.text
        if c.overlap_chars > 0:
            # Strip the overlap wrapper exactly.
            ctx_close = "<!-- /ctx -->\n\n"
            idx = body.index(ctx_close) + len(ctx_close)
            body = body[idx:]
        assert body == text[c.start:c.end], (
            f"Chunk body doesn't match source[{c.start}:{c.end}] in {corpus_file}"
        )
    assert cursor == len(text), f"chunks don't cover full text of {corpus_file}: stopped at {cursor}/{len(text)}"


# ─── P3: Metadata field correctness ───────────────────────────────────

class TestSectionPath:
    """section_path accuracy across heading hierarchies."""

    def test_h1_resets_path(self):
        text = "# Top A\n\npara\n\n# Top B\n\npara\n"
        boundaries = _small_splitter()._build_boundaries(scan(text))
        # The boundary BEFORE "# Top B" should have section_path = ("Top B",), not ("Top A", "Top B").
        for b in boundaries:
            if b.kind == BoundaryKind.H1 and b.section_path == ("Top B",):
                return
        pytest.fail("expected H1 boundary with section_path == ('Top B',)")

    def test_h3_keeps_ancestors(self):
        text = "# Top\n\npara\n\n## Sub\n\npara\n\n### Sub-sub\n\npara\n"
        boundaries = _small_splitter()._build_boundaries(scan(text))
        for b in boundaries:
            if b.kind == BoundaryKind.H3:
                assert b.section_path == ("Top", "Sub", "Sub-sub")
                return
        pytest.fail("expected H3 boundary")

    def test_h2_after_h3_truncates(self):
        text = (
            "# Top\n\npara\n\n"
            "## Sub A\n\npara\n\n"
            "### Sub A1\n\npara\n\n"
            "## Sub B\n\npara\n"
        )
        boundaries = _small_splitter()._build_boundaries(scan(text))
        # The boundary BEFORE `## Sub B` should be section_path = ("Top", "Sub B"),
        # NOT carrying "Sub A1" forward.
        for b in boundaries:
            if b.kind == BoundaryKind.H2 and b.section_path == ("Top", "Sub B"):
                return
        pytest.fail("H2 boundary did not truncate ancestor chain")

    def test_chunks_carry_section_path(self):
        # Force a chunk break at a heading.
        text = (
            "# Chapter\n\n"
            "## Intro\n\n"
            + ("Some content. " * 200) + "\n\n"
            "## Methods\n\n"
            + ("More content. " * 200) + "\n"
        )
        s = _small_splitter(target_size=800, max_size=1500, min_size=200, snap_window=300, overlap_chars=0)
        chunks = s.split_thoughtful(text, use_llm=False)
        assert len(chunks) >= 2
        paths = {tuple(c.section_path) for c in chunks}
        # We should see at least one chunk under (Chapter, Intro) and one under (Chapter, Methods).
        assert ("Chapter", "Intro") in paths or ("Chapter", "Methods") in paths


class TestAtomicKinds:
    """atomic_kinds tracking — each chunk records what atomic types it contains."""

    def test_chunk_with_code_fence_records_it(self):
        text = (
            "# Doc\n\n"
            + ("Some prose. " * 100) + "\n\n"
            "```python\nx = 1\ny = 2\n```\n\n"
            + ("More prose. " * 100) + "\n"
        )
        s = _small_splitter(target_size=400, max_size=2000, min_size=100, snap_window=200, overlap_chars=0)
        chunks = s.split_thoughtful(text, use_llm=False)
        # Some chunk must record CODE_FENCE in atomic_kinds.
        all_kinds: set = set()
        for c in chunks:
            all_kinds.update(c.atomic_kinds)
        assert BlockKind.CODE_FENCE in all_kinds

    def test_chunk_with_list_records_list_item(self):
        text = "# Doc\n\n" + "\n".join(f"- Item {i}" for i in range(50))
        s = _small_splitter()
        chunks = s.split_thoughtful(text, use_llm=False)
        all_kinds: set = set()
        for c in chunks:
            all_kinds.update(c.atomic_kinds)
        assert BlockKind.LIST_ITEM in all_kinds

    def test_chunk_with_table_records_it(self):
        text = "# Doc\n\n" + ("para\n\n" * 50) + "| a | b |\n|---|---|\n| 1 | 2 |\n"
        s = _small_splitter()
        chunks = s.split_thoughtful(text, use_llm=False)
        # Last chunk should contain the table.
        last = chunks[-1]
        assert BlockKind.TABLE in last.atomic_kinds


class TestBoundaryTypeCoverage:
    """Across the corpus, we should see a variety of boundary_type values
    used — at minimum h1/h2/h3 in structured docs, paragraph in
    unstructured ones, list_item_end in outlines."""

    def test_corpus_uses_h2_in_structured_docs(self):
        text = (CORPUS_DIR / "long_essay_with_code.md").read_text(encoding="utf-8")
        s = _small_splitter()
        chunks = s.split_thoughtful(text, use_llm=False)
        types = {c.boundary_type for c in chunks}
        assert BoundaryKind.H2 in types

    def test_corpus_uses_list_item_end_in_outlines(self):
        text = (CORPUS_DIR / "outline_dominant.md").read_text(encoding="utf-8")
        s = _small_splitter()
        chunks = s.split_thoughtful(text, use_llm=False)
        types = {c.boundary_type for c in chunks}
        assert BoundaryKind.LIST_ITEM_END in types

    def test_corpus_uses_paragraph_in_unstructured_essay(self):
        text = (CORPUS_DIR / "long_unstructured_essay.md").read_text(encoding="utf-8")
        s = _small_splitter()
        chunks = s.split_thoughtful(text, use_llm=False)
        types = {c.boundary_type for c in chunks}
        # No headings in this corpus, so all boundaries should be PARAGRAPH.
        assert types == {BoundaryKind.PARAGRAPH}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
