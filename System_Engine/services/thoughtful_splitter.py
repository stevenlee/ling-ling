"""Thoughtful Splitter — structure + paragraph + concept-aware chunking.

Produces `Chunk` objects with rich metadata (section_path, boundary_type,
atomic_kinds, overlap_chars) instead of mechanical character cuts. See
`DesignDoc/ThoughtfulSplitter_implementation_plan.md` for the full design.

This file implements:
  * Phase 2 — `_build_boundaries`: weighted split candidates from blocks
  * Phase 3 — `_greedy_chunk`: budget-driven boundary selection
                                + `_recursive_fallback` with atomic-intersect
                                  guard (Step 0) and reverse sentence search
                                  (Step 2)
  * Phase 3b — `_apply_structural_overlap`: prepends prev-chunk context
  * Phase 4 — `_llm_topic_refine`: STUB (real impl arrives in P5)
  * Phase 5 — metadata enrichment via `Chunk.atomic_kinds`

Public API
----------
    ThoughtfulSplitter(target_size=None, max_size=None, min_size=None,
                       snap_window=None, overlap_chars=None)
        .split_thoughtful(text, *, use_llm=True, emit_summary=False) -> list[Chunk]
        .split_text(text) -> list[str]                      # back-compat
        .split_text_with_spans(text) -> list[dict]          # back-compat
"""

from __future__ import annotations

import json
import logging
import re
from bisect import bisect_right
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path

from core.config import settings
from services.md_block_scanner import Block, BlockKind, leaf_blocks, scan


# ─── Boundary types ───────────────────────────────────────────────────

class BoundaryKind(Enum):
    """Split-point classifications with priority weight.

    Higher weight = better place to cut. The chunker prefers high-weight
    boundaries when several are available within the snap window.
    """
    FRONTMATTER_END = ("frontmatter_end", 100)
    H1              = ("h1", 100)
    H2              = ("h2", 80)
    HR              = ("hr", 70)
    H3              = ("h3", 60)
    LLM_TOPIC_SHIFT = ("llm_topic_shift", 50)   # P5 — inserted by Phase 4
    H4_PLUS         = ("h4_plus", 40)
    LIST_END        = ("list_end", 35)
    PARAGRAPH       = ("paragraph", 30)
    LIST_ITEM_END   = ("list_item_end", 28)
    BLOCKQUOTE_END  = ("blockquote_end", 25)
    SENTENCE        = ("sentence", 10)
    FORCED          = ("forced", 0)

    def __init__(self, label: str, weight: int):
        self.label = label
        self.weight = weight


@dataclass(frozen=True)
class Boundary:
    position: int                       # char offset where the cut would land
    kind: BoundaryKind
    section_path: tuple[str, ...]       # heading path AS OF this boundary


@dataclass(frozen=True)
class Chunk:
    text: str                           # includes any prepended overlap context
    start: int                          # source offset, ignoring overlap prefix
    end: int                            # source offset (exclusive)
    section_path: tuple[str, ...]
    boundary_type: BoundaryKind         # why this chunk ended here
    atomic_kinds: tuple[BlockKind, ...] # atomic units present inside
    overlap_chars: int = 0              # chars of prev-chunk context prepended
    preceding_summary: str = ""         # P6 — LLM-generated summary (opt-in)

    def to_dict(self) -> dict:
        """JSON-safe serialization. `asdict()` would leak Enum objects."""
        return {
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "section_path": list(self.section_path),
            "boundary_type": self.boundary_type.label,
            "atomic_kinds": [k.value for k in self.atomic_kinds],
            "overlap_chars": self.overlap_chars,
            "preceding_summary": self.preceding_summary,
        }


# ─── Constants ────────────────────────────────────────────────────────

# Sentence enders for the Phase 3 Step 2 fallback. Both ASCII and full-width
# punctuation, plus newline (any line break suggests a sentence boundary).
_SENTENCE_END_RE = re.compile(r"[。!?！？.]\s+|\n")

# Overlap context markers — visible in the chunk text so downstream LLM
# prompts can recognise "this is borrowed context".
_OVERLAP_OPEN  = "<!-- ctx: prev-chunk-tail -->\n"
_OVERLAP_CLOSE = "\n<!-- /ctx -->\n\n"
_SUMMARY_OPEN  = "<!-- summary: prev-chunk -->\n"
_SUMMARY_CLOSE = "\n<!-- /summary -->\n\n"

# Splits paragraphs by blank lines. A "blank line" may contain whitespace.
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


# ─── Content-hashed cache (P5 + P6) ───────────────────────────────────

class _ContentHashCache:
    """Memory + optional-disk cache keyed by sha256(text).

    Used by Phase 4 (topic-shift indices, `subdir="topic_shifts"`) and
    Phase 5 (context summaries, `subdir="summaries"`). The hashed-content
    key makes stale data impossible: any text edit changes the hash, so
    a miss occurs and the LLM is re-asked.

    Stored as `{"value": <json-serializable>}` on disk; in memory the raw
    value is stored. Different subdirs prevent type collisions between
    caches sharing the same root directory.
    """

    __slots__ = ("_memory", "_cache_dir")

    def __init__(self, cache_dir: Path | str | None = None, subdir: str = ""):
        from typing import Any
        self._memory: dict[str, Any] = {}
        self._cache_dir: Path | None = None
        if cache_dir:
            target = Path(cache_dir) / subdir if subdir else Path(cache_dir)
            try:
                target.mkdir(parents=True, exist_ok=True)
                self._cache_dir = target
            except Exception as e:
                logging.warning(f"ContentHashCache: could not create {target}: {e}")

    @staticmethod
    def _key(text: str) -> str:
        return sha256(text.encode("utf-8")).hexdigest()

    def get(self, text: str):
        key = self._key(text)
        if key in self._memory:
            return self._memory[key]
        if self._cache_dir is None:
            return None
        path = self._cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or "value" not in payload:
                return None
            value = payload["value"]
        except Exception as e:
            logging.debug(f"ContentHashCache: failed to read {path.name}: {e}")
            return None
        # Mirror disk hit into memory so subsequent lookups are fast.
        self._memory[key] = value
        return value

    def put(self, text: str, value) -> None:
        key = self._key(text)
        self._memory[key] = value
        if self._cache_dir is None:
            return
        try:
            (self._cache_dir / f"{key}.json").write_text(
                json.dumps({"value": value}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logging.debug(f"ContentHashCache: failed to persist {key[:8]}: {e}")


# Back-compat shim: existing P5 tests import _TopicShiftCache by name.
class _TopicShiftCache(_ContentHashCache):
    def __init__(self, cache_dir: Path | str | None = None):
        super().__init__(cache_dir, subdir="topic_shifts")


# ─── Splitter ─────────────────────────────────────────────────────────

class ThoughtfulSplitter:
    """Structure-aware markdown chunker."""

    def __init__(
        self,
        target_size: int | None = None,
        max_size: int | None = None,
        min_size: int | None = None,
        snap_window: int | None = None,
        overlap_chars: int | None = None,
        *,
        default_use_llm: bool = True,
        default_emit_summary: bool = False,
        llm=None,
        cache_dir: Path | str | None = None,
    ):
        self.target_size = target_size or settings.DIGEST_LIMIT
        self.max_size = max_size or int(self.target_size * settings.DIGEST_MAX_FACTOR)
        self.min_size = min_size if min_size is not None else int(self.target_size * settings.DIGEST_MIN_FACTOR)
        self.snap_window = snap_window if snap_window is not None else 2000
        self.overlap_chars = overlap_chars if overlap_chars is not None else settings.OVERLAP_CHARS

        # Used by the back-compat `split_text` / `split_text_with_spans` shims
        # so callers like CounterAgent can opt out of LLM topic refinement
        # without changing the call site.
        self._default_use_llm = default_use_llm
        self._default_emit_summary = default_emit_summary

        # Phase 4: optional LLM for topic-shift detection. If None, P5 is a
        # silent no-op even when `use_llm=True` is passed at split time.
        self.llm = llm

        # Caches: default to memory-only; persist to disk when caller
        # passes `cache_dir` OR `THOUGHTFUL_CACHE_DIR` env is set upstream.
        # The two caches share a root but use separate subdirs so the
        # value types (lists vs strings) can't collide on key hash.
        if cache_dir is None:
            from core.config import THOUGHTFUL_CACHE_DIR
            cache_dir = THOUGHTFUL_CACHE_DIR
        self._topic_cache = _ContentHashCache(cache_dir, subdir="topic_shifts")
        self._summary_cache = _ContentHashCache(cache_dir, subdir="summaries")

        if not (self.min_size < self.target_size <= self.max_size):
            raise ValueError(
                f"Invalid size config: min={self.min_size} < target={self.target_size} <= max={self.max_size}"
            )

    # Back-compat: TextSplitter exposes `chunk_size`; IngestionPipeline reads
    # it directly. Aliasing target_size keeps the call site polymorphic.
    @property
    def chunk_size(self) -> int:
        return self.target_size

    # ── Public API ────────────────────────────────────────────────────

    def split_thoughtful(
        self,
        text: str,
        *,
        use_llm: bool = True,
        emit_summary: bool = False,
    ) -> list[Chunk]:
        if not text:
            return []
        if len(text) <= self.target_size:
            # Whole document fits in one chunk — no need to walk the algorithm.
            return [self._single_chunk(text)]

        blocks = scan(text)
        leaves = leaf_blocks(blocks)
        boundaries = self._build_boundaries(blocks)
        chunks_raw = self._greedy_chunk(text, leaves, boundaries)

        if use_llm:
            chunks_raw = self._llm_topic_refine(text, chunks_raw, leaves)  # P5: currently a no-op

        # Phase 3b structural overlap auto-disables when emit_summary is True;
        # the LLM-generated summary is a higher-quality context carrier and
        # we don't want both prefixes on the same chunk.
        apply_overlap = self.overlap_chars > 0 and not emit_summary
        apply_summary = emit_summary and self.llm is not None
        chunks = self._finalize(
            text, leaves, chunks_raw,
            apply_overlap=apply_overlap,
            apply_summary=apply_summary,
        )
        return chunks

    def split_text(self, text: str) -> list[str]:
        """Back-compat shim — drop-in replacement for `TextSplitter.split_text`.

        Honors the splitter's `default_use_llm` / `default_emit_summary`
        constructor kwargs, so e.g. CounterAgent can opt out of P5 LLM
        refinement without modifying the call site.
        """
        return [c.text for c in self.split_thoughtful(
            text, use_llm=self._default_use_llm, emit_summary=self._default_emit_summary,
        )]

    def split_text_with_spans(self, text: str) -> list[dict]:
        """Back-compat shim — drop-in replacement for `TextSplitter.split_text_with_spans`."""
        return [c.to_dict() for c in self.split_thoughtful(
            text, use_llm=self._default_use_llm, emit_summary=self._default_emit_summary,
        )]

    # ── Single-chunk short-circuit ────────────────────────────────────

    def _single_chunk(self, text: str) -> Chunk:
        blocks = scan(text)
        leaves = leaf_blocks(blocks)
        atomic_kinds = tuple(sorted(
            {b.kind for b in blocks if b.atomic},
            key=lambda k: k.value,
        ))
        # Section path = the deepest heading active when the document ends.
        section_path = self._section_path_at(len(text), self._build_section_index(leaves))
        return Chunk(
            text=text,
            start=0,
            end=len(text),
            section_path=section_path,
            boundary_type=BoundaryKind.PARAGRAPH,
            atomic_kinds=atomic_kinds,
        )

    # ── Phase 2: boundary construction ────────────────────────────────

    def _build_boundaries(self, blocks: list[Block]) -> list[Boundary]:
        boundaries: list[Boundary] = []
        section_path: list[str] = []
        prev: Block | None = None

        # Skip BLANK (coverage filler) and LIST (virtual container).
        content_blocks = [
            b for b in blocks
            if b.kind not in (BlockKind.BLANK, BlockKind.LIST)
        ]

        for cb in content_blocks:
            # Update section_path BEFORE emitting boundary so a boundary
            # before a heading carries the new section context (which the
            # following chunk will live in).
            if cb.kind == BlockKind.HEADING:
                section_path = section_path[: max(0, cb.level - 1)] + [cb.heading_text]

            if prev is not None:
                kind = self._boundary_kind(prev, cb)
                boundaries.append(Boundary(
                    position=cb.start,
                    kind=kind,
                    section_path=tuple(section_path),
                ))

            prev = cb

        return boundaries

    @staticmethod
    def _boundary_kind(prev: Block, current: Block) -> BoundaryKind:
        if prev.kind == BlockKind.FRONTMATTER:
            return BoundaryKind.FRONTMATTER_END
        if current.kind == BlockKind.HEADING:
            if current.level == 1:
                return BoundaryKind.H1
            if current.level == 2:
                return BoundaryKind.H2
            if current.level == 3:
                return BoundaryKind.H3
            return BoundaryKind.H4_PLUS
        if current.kind == BlockKind.HR:
            return BoundaryKind.HR
        if prev.kind == BlockKind.LIST_ITEM and current.kind == BlockKind.LIST_ITEM:
            return BoundaryKind.LIST_ITEM_END
        if prev.kind == BlockKind.LIST_ITEM and current.kind != BlockKind.LIST_ITEM:
            return BoundaryKind.LIST_END
        if prev.kind == BlockKind.BLOCKQUOTE and current.kind != BlockKind.BLOCKQUOTE:
            return BoundaryKind.BLOCKQUOTE_END
        return BoundaryKind.PARAGRAPH

    # ── Phase 3: greedy chunking ──────────────────────────────────────

    def _greedy_chunk(
        self,
        text: str,
        leaves: list[Block],
        boundaries: list[Boundary],
    ) -> list[tuple[int, int, BoundaryKind, tuple[str, ...]]]:
        atomics = [b for b in leaves if b.atomic]
        atomics.sort(key=lambda b: b.start)
        atomic_starts = [a.start for a in atomics]

        # Sort boundaries by position for linear scan.
        boundaries_sorted = sorted(boundaries, key=lambda b: b.position)
        b_positions = [b.position for b in boundaries_sorted]

        # Pre-compute a section index: list of (heading_position, section_path).
        # This lets `_section_path_at` answer "what section am I in?" for any
        # offset, independent of which boundary the chunker chose.
        section_index = self._build_section_index(leaves)

        chunks_raw: list[tuple[int, int, BoundaryKind, tuple[str, ...]]] = []
        cursor = 0
        n = len(text)

        while cursor < n:
            max_end = cursor + self.max_size

            # Trailing chunk: rest of document fits.
            if max_end >= n:
                path = self._section_path_at(n, section_index)
                chunks_raw.append((cursor, n, BoundaryKind.PARAGRAPH, path))
                cursor = n
                break

            target_end = cursor + self.target_size
            min_end = cursor + self.min_size
            snap_lo = max(min_end, target_end - self.snap_window)
            snap_hi = min(max_end, target_end + self.snap_window)

            # Collect boundaries in [snap_lo, snap_hi].
            lo_idx = bisect_right(b_positions, snap_lo - 1)
            hi_idx = bisect_right(b_positions, snap_hi)
            candidates = boundaries_sorted[lo_idx:hi_idx]

            chosen_end: int | None = None
            chosen_kind: BoundaryKind | None = None

            # Pick best-weight candidate that doesn't slice an atomic block.
            for cand in sorted(
                candidates,
                key=lambda b: (-b.kind.weight, abs(b.position - target_end)),
            ):
                if not self._cuts_inside_atomic(cand.position, atomics, atomic_starts):
                    chosen_end = cand.position
                    chosen_kind = cand.kind
                    break

            if chosen_end is None:
                chosen_end, chosen_kind = self._recursive_fallback(
                    text, cursor, max_end, atomics, atomic_starts,
                )

            # Section path for THIS chunk: the section we're in just inside the
            # chunk's tail (i.e., the latest heading at position < chosen_end).
            path = self._section_path_at(chosen_end, section_index)
            chunks_raw.append((cursor, chosen_end, chosen_kind, path))
            cursor = chosen_end

        return chunks_raw

    @staticmethod
    def _build_section_index(leaves: list[Block]) -> list[tuple[int, tuple[str, ...]]]:
        """Return (heading_start, section_path) for every heading, in order.

        Lets `_section_path_at` look up the active section at any offset
        without re-traversing all blocks.
        """
        index: list[tuple[int, tuple[str, ...]]] = []
        section_path: list[str] = []
        for b in leaves:
            if b.kind == BlockKind.HEADING:
                section_path = section_path[:max(0, b.level - 1)] + [b.heading_text]
                index.append((b.start, tuple(section_path)))
        return index

    @staticmethod
    def _section_path_at(
        position: int,
        section_index: list[tuple[int, tuple[str, ...]]],
    ) -> tuple[str, ...]:
        """Return the section_path that applies at `position`.

        A heading at offset H is "active" for any position > H. (Headings
        define the section that follows them.) If position lands exactly
        at H, that heading is the start of the NEXT chunk, not part of
        the current one — so we use strict `<` comparison.
        """
        if not section_index:
            return tuple()
        result: tuple[str, ...] = tuple()
        for pos, path in section_index:
            if pos < position:
                result = path
            else:
                break
        return result

    @staticmethod
    def _cuts_inside_atomic(
        position: int,
        atomics: list[Block],
        atomic_starts: list[int],
    ) -> bool:
        """True iff `position` is strictly inside any atomic block."""
        if not atomics:
            return False
        idx = bisect_right(atomic_starts, position) - 1
        if idx < 0:
            return False
        a = atomics[idx]
        return a.start < position < a.end

    def _recursive_fallback(
        self,
        text: str,
        cursor: int,
        max_end: int,
        atomics: list[Block],
        atomic_starts: list[int],
    ) -> tuple[int, BoundaryKind]:
        """Choose a chunk end when no high-quality boundary is available.

        Step 0 (NEW vs v1): atomic-intersect guard — refuse to cut inside
        any atomic block that overlaps [cursor, max_end] and extends past
        max_end. Shrink (cut before atomic) or expand (emit oversize chunk)
        depending on which side the atomic starts on.

        Step 1: latest atomic-block end that falls within [cursor+min_size, max_end].
        Step 2: reverse-search for the latest sentence end in the same range.
        Step 3: forced cut at max_end.
        """
        min_end = cursor + self.min_size

        # Step 0: atomic-intersect guard.
        for a in atomics:
            if a.start >= max_end:
                break
            if a.end <= cursor:
                continue
            if a.end > max_end:
                if a.start > cursor:
                    # Shrink: cut RIGHT BEFORE the atomic starts. May
                    # produce a chunk smaller than min_size — that's the
                    # price of atomic protection.
                    return (a.start, BoundaryKind.FORCED)
                # Expand: the atomic started at-or-before cursor and runs
                # past max_end. Emit an oversize chunk; log it.
                logging.warning(
                    "ThoughtfulSplitter: oversize atomic chunk: "
                    f"{a.end - a.start} chars ({a.kind.value} starting at {a.start})"
                )
                return (min(a.end, len(text)), BoundaryKind.FORCED)

        # Step 1: latest atomic end inside the window (must respect min_size).
        last_atomic_end: int | None = None
        for a in atomics:
            if a.start >= max_end:
                break
            if a.end <= min_end:
                continue
            if a.end <= max_end:
                last_atomic_end = a.end
        if last_atomic_end is not None:
            return (last_atomic_end, BoundaryKind.PARAGRAPH)

        # Step 2: reverse-search sentence end in [min_end, max_end].
        sentence_pos = self._latest_sentence_end(text, min_end, max_end)
        if sentence_pos is None:
            # Step 2b: relax range to [cursor, max_end].
            sentence_pos = self._latest_sentence_end(text, cursor, max_end)
        if sentence_pos is not None:
            return (sentence_pos, BoundaryKind.SENTENCE)

        # Step 3: forced cut.
        return (max_end, BoundaryKind.FORCED)

    @staticmethod
    def _latest_sentence_end(text: str, lo: int, hi: int) -> int | None:
        """Find the LATEST sentence-end position in [lo, hi]."""
        if hi <= lo:
            return None
        window = text[lo:hi]
        latest: int | None = None
        for m in _SENTENCE_END_RE.finditer(window):
            latest = lo + m.end()
        return latest

    # ── Phase 4: LLM topic refinement (P5) ────────────────────────────

    def _llm_topic_refine(
        self,
        text: str,
        chunks_raw: list[tuple[int, int, BoundaryKind, tuple[str, ...]]],
        leaves: list[Block],
    ) -> list[tuple[int, int, BoundaryKind, tuple[str, ...]]]:
        """Insert topic-shift splits into qualifying chunks using the LLM.

        Eligibility rules (only the chunks that benefit from this):
          * The chunk contains only `PARAGRAPH` block-content (no headings,
            lists, tables, code, callouts).
          * Size > `target_size * 1.2` (otherwise the structural split was
            already good enough).
          * It spans no heading boundary — split paths through the chunk
            stay within one section.

        For each eligible chunk:
          1. Extract paragraph offsets within the chunk.
          2. Skip if fewer than 3 paragraphs (no useful split available).
          3. Check the content-hash cache; on miss, call the LLM via
             `llm.find_topic_shifts(paragraphs)`.
          4. Validate the returned indices (in-range, dedup, ≤ 2, not
             producing a chunk smaller than `min_size * 0.5`).
          5. Materialize new sub-chunks with boundary_type=LLM_TOPIC_SHIFT.

        ANY failure (no LLM, LLM error, bad JSON, no valid splits)
        returns the chunk untouched. This method never raises.
        """
        if self.llm is None:
            return chunks_raw

        # Pre-compute heading offsets so we can detect "spans no heading".
        heading_offsets = sorted(b.start for b in leaves if b.kind == BlockKind.HEADING)
        atomics = [b for b in leaves if b.atomic]
        atomic_starts = [a.start for a in atomics]

        refined: list[tuple[int, int, BoundaryKind, tuple[str, ...]]] = []
        for chunk_tuple in chunks_raw:
            sub_chunks = self._refine_one(
                text, chunk_tuple, leaves, heading_offsets, atomics, atomic_starts,
            )
            refined.extend(sub_chunks)
        return refined

    def _refine_one(
        self,
        text: str,
        chunk_tuple: tuple[int, int, BoundaryKind, tuple[str, ...]],
        leaves: list[Block],
        heading_offsets: list[int],
        atomics: list[Block],
        atomic_starts: list[int],
    ) -> list[tuple[int, int, BoundaryKind, tuple[str, ...]]]:
        start, end, kind, path = chunk_tuple
        single = [chunk_tuple]

        # Eligibility 1: size threshold.
        if (end - start) <= int(self.target_size * 1.2):
            return single

        # Eligibility 2: chunk spans no heading boundary.
        for h in heading_offsets:
            if h <= start:
                continue
            if h >= end:
                break
            return single  # heading inside → already a structural split point

        # Eligibility 3: chunk content is paragraphs only — no atomics, no
        # headings (the previous check ruled out headings), no other block
        # kinds. This shields against P5 inserting cuts inside code/table/list.
        if not self._chunk_is_paragraphs_only(leaves, start, end):
            return single

        # Extract paragraph offsets relative to the source text.
        chunk_text = text[start:end]
        paragraph_spans = self._paragraph_spans(chunk_text)
        if len(paragraph_spans) < 3:
            return single

        # Cache-or-call.
        splits = self._topic_cache.get(chunk_text)
        if splits is None:
            try:
                response = self.llm.find_topic_shifts(
                    [chunk_text[s:e] for s, e in paragraph_spans],
                )
            except Exception as e:
                logging.warning(f"ThoughtfulSplitter: topic-shift LLM call raised: {e}")
                return single
            splits = response.get("split_after", []) if isinstance(response, dict) else []
            # Cache even an empty answer — a confirmed "no shift here" is
            # also worth not re-asking the LLM about.
            self._topic_cache.put(chunk_text, splits)

        if not splits:
            return single

        # Convert paragraph indices → source offsets.
        # `split_after = [3]` means cut at the END of paragraph 3.
        source_cuts: list[int] = []
        min_tiny = max(int(self.min_size * 0.5), 64)  # reject cuts that yield tiny fragments
        last_cut = start
        for idx in splits:
            if idx < 1 or idx >= len(paragraph_spans):
                continue
            para_end = start + paragraph_spans[idx - 1][1]  # 1-based → 0-based, end of that para
            if para_end <= last_cut + min_tiny:
                continue
            if (end - para_end) < min_tiny:
                continue
            if self._cuts_inside_atomic(para_end, atomics, atomic_starts):
                continue
            source_cuts.append(para_end)
            last_cut = para_end

        if not source_cuts:
            return single

        # Materialize sub-chunks. The first sub-chunk inherits the parent's
        # boundary_type (still ends at the original structural cut for the
        # NEXT chunk). Intermediate sub-chunks end at LLM cuts and use
        # LLM_TOPIC_SHIFT. The last sub-chunk keeps the parent's original
        # boundary_type because it ends where the original chunk ended.
        sub_chunks: list[tuple[int, int, BoundaryKind, tuple[str, ...]]] = []
        cursor = start
        for cut in source_cuts:
            sub_chunks.append((cursor, cut, BoundaryKind.LLM_TOPIC_SHIFT, path))
            cursor = cut
        sub_chunks.append((cursor, end, kind, path))
        return sub_chunks

    @staticmethod
    def _chunk_is_paragraphs_only(leaves: list[Block], start: int, end: int) -> bool:
        """True iff every leaf block overlapping [start, end) is PARAGRAPH or BLANK."""
        for b in leaves:
            if b.end <= start:
                continue
            if b.start >= end:
                break
            if b.kind not in (BlockKind.PARAGRAPH, BlockKind.BLANK):
                return False
        return True

    @staticmethod
    def _paragraph_spans(chunk_text: str) -> list[tuple[int, int]]:
        """Return (start, end) tuples for each paragraph in chunk_text.

        Boundaries are blank-line separators. Empty paragraphs (e.g. from a
        chunk that begins with a blank line) are dropped.
        """
        spans: list[tuple[int, int]] = []
        cursor = 0
        for m in _PARAGRAPH_SPLIT_RE.finditer(chunk_text):
            if cursor < m.start():
                spans.append((cursor, m.start()))
            cursor = m.end()
        if cursor < len(chunk_text):
            tail = chunk_text[cursor:].rstrip()
            if tail:
                spans.append((cursor, cursor + len(tail)))
        return [s for s in spans if chunk_text[s[0]:s[1]].strip()]

    # ── Phase 5 + 3b: finalize chunks with metadata + overlap ─────────

    def _finalize(
        self,
        text: str,
        leaves: list[Block],
        chunks_raw: list[tuple[int, int, BoundaryKind, tuple[str, ...]]],
        *,
        apply_overlap: bool,
        apply_summary: bool = False,
    ) -> list[Chunk]:
        chunks: list[Chunk] = []
        for i, (start, end, kind, section_path) in enumerate(chunks_raw):
            base_text = text[start:end]
            atomic_kinds = self._atomic_kinds_in(leaves, start, end)

            preceding_summary = ""
            if apply_summary and i > 0:
                prev_start, prev_end, _, _ = chunks_raw[i - 1]
                preceding_summary = self._fetch_summary(text[prev_start:prev_end])

            overlap_text = ""
            overlap_n = 0
            # `apply_overlap` and `apply_summary` are mutually exclusive at the
            # `split_thoughtful` entry point — but keep this guard so future
            # callers that flip flags directly don't get both prefixes.
            if apply_overlap and not preceding_summary and i > 0:
                prev_start, prev_end, _, _ = chunks_raw[i - 1]
                overlap_text, overlap_n = self._overlap_prefix(text, leaves, prev_start, prev_end)

            if preceding_summary:
                chunk_text = f"{_SUMMARY_OPEN}{preceding_summary}{_SUMMARY_CLOSE}{base_text}"
            elif overlap_text:
                chunk_text = f"{_OVERLAP_OPEN}{overlap_text}{_OVERLAP_CLOSE}{base_text}"
            else:
                chunk_text = base_text

            chunks.append(Chunk(
                text=chunk_text,
                start=start,
                end=end,
                section_path=section_path,
                boundary_type=kind,
                atomic_kinds=atomic_kinds,
                overlap_chars=overlap_n,
                preceding_summary=preceding_summary,
            ))
        return chunks

    # ── Phase 5 (P6): preceding summary via LLM ───────────────────────

    def _fetch_summary(self, prev_chunk_text: str) -> str:
        """Return a cached or LLM-generated 1-2 sentence summary of prev chunk.

        Returns `""` on any failure — no LLM, LLM raise, empty response.
        The string is never re-raised; ingestion must continue.
        """
        cached = self._summary_cache.get(prev_chunk_text)
        if cached is not None:
            # `None` = miss; `""` = previously asked and got nothing back
            # (worth caching: don't ask the LLM again).
            return cached

        if self.llm is None:
            self._summary_cache.put(prev_chunk_text, "")
            return ""

        try:
            response = self.llm.summarize_for_context(prev_chunk_text)
        except Exception as e:
            logging.warning(f"ThoughtfulSplitter: summary LLM call raised: {e}")
            # Cache the failure too — repeatedly retrying a dead LLM during
            # one ingestion run would multiply latency for no benefit. To
            # force a retry after the LLM recovers, the user can clear the
            # cache directory (it's content-hashed, so safe to wipe).
            self._summary_cache.put(prev_chunk_text, "")
            return ""

        summary = response.get("summary", "") if isinstance(response, dict) else ""
        if not isinstance(summary, str):
            summary = ""
        self._summary_cache.put(prev_chunk_text, summary)
        return summary

    @staticmethod
    def _atomic_kinds_in(leaves: list[Block], start: int, end: int) -> tuple[BlockKind, ...]:
        kinds_set: set[BlockKind] = set()
        for b in leaves:
            if b.start >= end:
                break
            if b.end <= start:
                continue
            if b.atomic:
                kinds_set.add(b.kind)
        return tuple(sorted(kinds_set, key=lambda k: k.value))

    def _overlap_prefix(
        self,
        text: str,
        leaves: list[Block],
        prev_start: int,
        prev_end: int,
    ) -> tuple[str, int]:
        """Return (overlap_text, char_count) for the prefix to prepend to the
        next chunk. Prefers the last paragraph block in the previous chunk;
        falls back to the last `overlap_chars` chars of raw text.
        """
        budget = self.overlap_chars
        if budget <= 0:
            return ("", 0)

        last_para: Block | None = None
        for b in leaves:
            if b.start >= prev_end:
                break
            if b.kind == BlockKind.PARAGRAPH and b.start >= prev_start and b.end <= prev_end:
                last_para = b

        if last_para is not None:
            source = last_para.text.rstrip()
        else:
            # No paragraph in prev chunk — take last `budget` chars of raw text.
            source = text[max(prev_start, prev_end - budget):prev_end].rstrip()

        if len(source) <= budget:
            return (source, len(source))

        # Trim from the start, then snap forward to the next sentence/word
        # boundary so we don't begin mid-word.
        trimmed = source[-budget:]
        for m in _SENTENCE_END_RE.finditer(trimmed):
            # First sentence end → start the overlap just after it.
            cut = m.end()
            if cut < len(trimmed):
                return (trimmed[cut:].lstrip(), len(trimmed) - cut)
            break
        # Fallback: snap forward at first whitespace.
        ws = trimmed.find(" ")
        if 0 < ws < len(trimmed) - 1:
            return (trimmed[ws + 1:], len(trimmed) - ws - 1)
        return (trimmed, len(trimmed))
