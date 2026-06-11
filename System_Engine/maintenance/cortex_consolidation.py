"""Cortex consolidation — the nightly hippocampus→neocortex pass.

Insights that survived Phase 1's signal tower are distilled into atomic
claims and consolidated into Cortex/ pages (one claim per page):

    candidate insights (signals gate) → extract_claims (quota)
        → neighbor search (cached embeddings, ≥0.80, top-K)
        → entailment adjudication (quota, content-addressed cache, no TTL)
        → equivalent: merge into the existing page (reconsolidation)
          everything else: new page + typed links

Merging happens ONLY on a bidirectional-entailment (equivalent) verdict
— CortexMemory invariant 5. All page writes go through cortex_store —
invariant from plan §2.1. Quotas bound the nightly LLM spend; everything
fails open (a skipped insight just waits for the next night).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from core.config import (
    CORTEX_ADJUDICATION_CACHE,
    CORTEX_CONSOLIDATION_ENABLED,
    CORTEX_DIR,
    CORTEX_MAX_ADJUDICATIONS_PER_NIGHT,
    CORTEX_MAX_INSIGHTS_PER_NIGHT,
    CORTEX_MAX_VARIANTS,
    CORTEX_NEIGHBOR_SIM_THRESHOLD,
    CORTEX_NEIGHBOR_TOP_K,
    CORTEX_STATE_FILE,
    FROM_LLM_DIR,
    INSIGHTS_DIR,
    MAINTENANCE_LOG_FILE,
)
from core.parser import parse_markdown_metadata, strip_body_frontmatter
from services.cortex_store import (
    CortexPage,
    claim_filename,
    load_all_pages,
    make_claim_id,
    render_cortex_page,
    save_cortex_page,
)

_GROUNDEDNESS_GATE = 0.5
_INSIGHT_TEXT_CAP = 8000
_CONFIDENCE_STEP = 0.1
_CONFIDENCE_CAP = 0.9
_CONTRADICTION_DENT = 0.2
_CONFIDENCE_FLOOR = 0.1
# Insight frontmatter keys that may carry the source titles (fail-open).
_SOURCE_KEYS = ("related_docs", "related_titles", "target_titles", "sources")


@dataclass
class ConsolidationResult:
    status: str                  # "succeeded" | "skipped"
    message: str
    created: int = 0
    merged: int = 0
    contradiction_links: int = 0
    insights_processed: int = 0
    adjudications_used: int = 0
    report_path: Path | None = None


def _claim_hash(claim: str) -> str:
    return hashlib.sha256(claim.strip().encode("utf-8")).hexdigest()


def _pair_key(claim_a: str, claim_b: str) -> str:
    ha, hb = sorted((_claim_hash(claim_a), _claim_hash(claim_b)))
    return hashlib.sha256((ha + hb).encode("utf-8")).hexdigest()


def _cosine(a, b) -> float:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _load_json(path: Path, default):
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, type(default)):
                return data
    except Exception as e:
        logging.warning(f"Cortex: {path.name} unreadable, starting fresh: {e}")
    return default


def _save_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logging.warning(f"Cortex: failed to write {path.name}: {e}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class _Consolidator:
    """One night's run. Holds quotas, caches, and the in-memory page set."""

    def __init__(self, llm, rag, *, cortex_dir, state, adjudication_cache,
                 max_adjudications, top_k, sim_threshold, max_variants):
        self.llm = llm
        self.rag = rag
        self.cortex_dir = Path(cortex_dir)
        self.state = state
        self.cache = adjudication_cache
        self.max_adjudications = max_adjudications
        self.top_k = top_k
        self.sim_threshold = sim_threshold
        self.max_variants = max_variants

        self.pages: list[CortexPage] = load_all_pages(self.cortex_dir)
        self.by_claim_id = {p.claim_id: p for p in self.pages}
        self.embeddings: dict[str, list[float]] = {}
        self.adjudications_used = 0
        self.created = 0
        self.merged = 0
        self.contradiction_links = 0
        self._refresh_page_embeddings()

    # ── Embeddings (cached in the state file, invalidated by `updated`) ──

    def _embed(self, text: str) -> list[float] | None:
        try:
            if not hasattr(self.rag, "ef"):
                return None
            vec = self.rag.ef([text])[0]
            return [float(x) for x in vec]
        except Exception as e:
            logging.warning(f"Cortex: embedding failed: {e}")
            return None

    def _refresh_page_embeddings(self) -> None:
        cached = self.state.setdefault("claim_embeddings", {})
        for page in self.pages:
            entry = cached.get(page.claim_id)
            if entry and entry.get("updated") == page.updated:
                self.embeddings[page.claim_id] = entry["embedding"]
                continue
            vec = self._embed(page.claim)
            if vec is not None:
                self.embeddings[page.claim_id] = vec
                cached[page.claim_id] = {"embedding": vec, "updated": page.updated}
        # Drop cache entries for pages that no longer exist.
        for claim_id in list(cached):
            if claim_id not in self.by_claim_id:
                del cached[claim_id]

    # ── Adjudication (quota + content-addressed cache, no TTL) ──────────

    def _adjudicate(self, claim_a: str, claim_b: str) -> str | None:
        """Returns a verdict, or None when the nightly quota is exhausted."""
        key = _pair_key(claim_a, claim_b)
        hit = self.cache.get(key)
        if hit:
            return hit.get("verdict", "unrelated")
        if self.adjudications_used >= self.max_adjudications:
            return None
        self.adjudications_used += 1
        result = self.llm.adjudicate_claims(claim_a, claim_b)
        verdict = result.get("verdict", "unrelated") if isinstance(result, dict) else "unrelated"
        self.cache[key] = {
            "verdict": verdict,
            "rationale": (result or {}).get("rationale", "") if isinstance(result, dict) else "",
            "hashes": sorted((_claim_hash(claim_a), _claim_hash(claim_b))),
            "ts": _now(),
        }
        return verdict

    # ── Page operations (all through cortex_store) ───────────────────────

    def _index_page(self, page: CortexPage) -> None:
        try:
            self.rag.add_document(page.path, page.claim_id, render_cortex_page(page))
            self.rag.add_facets(page.path, page.claim_id, [page.claim])
        except Exception as e:
            logging.warning(f"Cortex: indexing failed for {page.claim_id}: {e}")

    def _merge_into(self, page: CortexPage, claim: str, evidence: dict) -> None:
        """Reconsolidation: section-level mutations only, zero LLM calls."""
        page.evidence.append(evidence)
        page.S += 1
        page.confidence = round(min(_CONFIDENCE_CAP, page.confidence + _CONFIDENCE_STEP), 4)
        page.last_reinforced_at = _now()
        page.updated = _now()
        if claim != page.claim and claim not in page.variants:
            page.variants.append(claim)
            if len(page.variants) > self.max_variants:
                page.variants = page.variants[-self.max_variants:]
        save_cortex_page(page)
        self.state["claim_embeddings"].pop(page.claim_id, None)  # updated changed
        self._index_page(page)
        self.merged += 1

    def _dent_confidence(self, page: CortexPage) -> None:
        page.confidence = round(max(_CONFIDENCE_FLOOR, page.confidence - _CONTRADICTION_DENT), 4)
        page.updated = _now()

    def process_claim(self, claim: str, summary: str, insight_name: str, sources: list[str]) -> None:
        evidence = {
            "insight": insight_name,
            "sources": sources,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "summary": summary,
        }

        # Exact-duplicate fast path: identical claim text needs no adjudication.
        claim_id = make_claim_id(claim)
        existing = self.by_claim_id.get(claim_id)
        if existing is not None:
            self._merge_into(existing, claim, evidence)
            return

        related: list[str] = []
        contradictions: list[str] = []

        vec = self._embed(claim)
        if vec is not None:
            scored = [
                (self._sim(vec, pid), pid)
                for pid in self.embeddings
            ]
            neighbors = sorted(
                [(s, pid) for s, pid in scored if s >= self.sim_threshold],
                reverse=True,
            )[: self.top_k]

            for _, neighbor_id in neighbors:
                neighbor = self.by_claim_id.get(neighbor_id)
                if neighbor is None:
                    continue
                verdict = self._adjudicate(claim, neighbor.claim)
                if verdict is None:
                    break  # quota exhausted — remaining relations wait for tomorrow
                if verdict == "equivalent":
                    self._merge_into(neighbor, claim, evidence)
                    return
                if verdict in ("entails", "entailed_by", "complementary"):
                    related.append(neighbor_id)
                elif verdict == "contradicts":
                    contradictions.append(neighbor_id)

        # No equivalent found → a new claim enters the cortex.
        now = _now()
        confidence = 0.5
        if contradictions:
            confidence = max(_CONFIDENCE_FLOOR, confidence - _CONTRADICTION_DENT * len(contradictions))
        page = CortexPage(
            claim_id=claim_id,
            path=claim_filename(claim, claim_id, self.cortex_dir),
            claim=claim,
            confidence=round(confidence, 4),
            S=1,
            last_reinforced_at=now,
            created=now,
            updated=now,
            evidence=[evidence],
            contradictions=list(contradictions),
            related=list(related),
        )
        save_cortex_page(page)
        self.pages.append(page)
        self.by_claim_id[claim_id] = page
        if vec is not None:
            self.embeddings[claim_id] = vec
            self.state["claim_embeddings"][claim_id] = {"embedding": vec, "updated": page.updated}
        self._index_page(page)
        self.created += 1

        # Back-links on the counterparts (typed, symmetric).
        for other_id in related:
            other = self.by_claim_id.get(other_id)
            if other and claim_id not in other.related:
                other.related.append(claim_id)
                other.updated = _now()
                save_cortex_page(other)
        for other_id in contradictions:
            other = self.by_claim_id.get(other_id)
            if other and claim_id not in other.contradictions:
                other.contradictions.append(claim_id)
                self._dent_confidence(other)
                save_cortex_page(other)
                self._index_page(other)
                self.contradiction_links += 1

    def _sim(self, vec, claim_id: str) -> float:
        return _cosine(vec, self.embeddings[claim_id])

    def prune_adjudication_cache(self) -> None:
        """Drop entries where BOTH claims have left the cortex (conservative)."""
        live = {_claim_hash(p.claim) for p in self.pages}
        for key in list(self.cache):
            hashes = self.cache[key].get("hashes") or []
            if not any(h in live for h in hashes):
                del self.cache[key]


# ── Candidate selection ───────────────────────────────────────────────

def _is_candidate(meta: dict) -> bool:
    signals = meta.get("signals")
    if not isinstance(signals, dict):
        return False
    if signals.get("refute_verdict") == "refuted":
        return False
    groundedness = signals.get("groundedness")
    if groundedness is not None:
        try:
            if float(groundedness) < _GROUNDEDNESS_GATE:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _insight_sources(meta: dict) -> list[str]:
    for key in _SOURCE_KEYS:
        value = meta.get(key)
        if isinstance(value, list) and value:
            return [str(v) for v in value]
    return []


# ── Entry point ───────────────────────────────────────────────────────

def run_consolidation(
    llm,
    rag,
    *,
    insights_dir: Path = None,
    cortex_dir: Path = None,
    state_file: Path = None,
    cache_file: Path = None,
    report_dir: Path = None,
    log_path: Path = None,
    max_insights: int = None,
    max_adjudications: int = None,
    top_k: int = None,
    sim_threshold: float = None,
    max_variants: int = None,
    enabled: bool = None,
) -> ConsolidationResult:
    insights_dir = insights_dir or INSIGHTS_DIR
    cortex_dir = cortex_dir or CORTEX_DIR
    state_file = state_file or CORTEX_STATE_FILE
    cache_file = cache_file or CORTEX_ADJUDICATION_CACHE
    report_dir = report_dir or FROM_LLM_DIR
    log_path = log_path or MAINTENANCE_LOG_FILE
    max_insights = max_insights if max_insights is not None else CORTEX_MAX_INSIGHTS_PER_NIGHT
    max_adjudications = (
        max_adjudications if max_adjudications is not None else CORTEX_MAX_ADJUDICATIONS_PER_NIGHT
    )
    top_k = top_k if top_k is not None else CORTEX_NEIGHBOR_TOP_K
    sim_threshold = sim_threshold if sim_threshold is not None else CORTEX_NEIGHBOR_SIM_THRESHOLD
    max_variants = max_variants if max_variants is not None else CORTEX_MAX_VARIANTS
    enabled = enabled if enabled is not None else CORTEX_CONSOLIDATION_ENABLED

    if not enabled:
        return ConsolidationResult(status="skipped", message="Cortex consolidation disabled.")

    state = _load_json(state_file, {})
    state.setdefault("processed", {})
    state.setdefault("claim_embeddings", {})
    cache = _load_json(cache_file, {})

    candidates = []
    if insights_dir.exists():
        for path in sorted(insights_dir.glob("*.md")):
            if path.name in state["processed"]:
                continue
            try:
                meta = parse_markdown_metadata(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if _is_candidate(meta):
                candidates.append((path, meta))
    if not candidates:
        return ConsolidationResult(
            status="skipped", message="No unprocessed insights with healthy signals."
        )

    worker = _Consolidator(
        llm, rag, cortex_dir=cortex_dir, state=state, adjudication_cache=cache,
        max_adjudications=max_adjudications, top_k=top_k,
        sim_threshold=sim_threshold, max_variants=max_variants,
    )

    insights_processed = 0
    for path, meta in candidates[:max_insights]:
        claim_count = 0
        try:
            body, _ = strip_body_frontmatter(path.read_text(encoding="utf-8"))
            claims = llm.extract_claims(body[:_INSIGHT_TEXT_CAP])
            sources = _insight_sources(meta)
            for item in claims if isinstance(claims, list) else []:
                if not isinstance(item, dict) or not isinstance(item.get("claim"), str):
                    continue
                worker.process_claim(
                    item["claim"], str(item.get("summary") or ""), path.name, sources
                )
                claim_count += 1
        except Exception:
            logging.exception(f"Cortex: consolidation failed for {path.name}")
        finally:
            # Processed regardless of outcome — a bad insight must not be
            # re-extracted (and re-billed) every night.
            state["processed"][path.name] = {"date": _now(), "claims": claim_count}
            insights_processed += 1
            _save_json(state_file, state)

    worker.prune_adjudication_cache()
    _save_json(cache_file, cache)
    _save_json(state_file, state)

    result = ConsolidationResult(
        status="succeeded",
        message=(
            f"Cortex: {insights_processed} insight(s) → {worker.created} new claim(s), "
            f"{worker.merged} merged, {worker.contradiction_links} contradiction link(s), "
            f"{worker.adjudications_used}/{max_adjudications} adjudications."
        ),
        created=worker.created,
        merged=worker.merged,
        contradiction_links=worker.contradiction_links,
        insights_processed=insights_processed,
        adjudications_used=worker.adjudications_used,
    )

    _append_log(log_path, result)
    if worker.created or worker.merged:
        result.report_path = _write_report(report_dir, result)
    return result


def _append_log(log_path: Path, result: ConsolidationResult) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## [{stamp}] Cortex Consolidation | {result.message}\n")
    except Exception as e:
        logging.warning(f"Cortex: maintenance log append failed: {e}")


def _write_report(report_dir: Path, result: ConsolidationResult) -> Path | None:
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        path = report_dir / f"[report] cortex consolidation {stamp}.md"
        lines = [
            "# 🧠 Cortex 鞏固報告",
            "",
            f"- 處理 insights：**{result.insights_processed}**",
            f"- 新增主張頁：**{result.created}**",
            f"- 合併（reconsolidation）：**{result.merged}**",
            f"- 矛盾連結：**{result.contradiction_links}**",
            f"- 蘊涵裁決用量：{result.adjudications_used}",
            "",
            "新主張已進入 `Cortex/` 並納入檢索（facets）。合併與連結的",
            "完整證據鏈在各頁 frontmatter，可隨時回溯拆解。",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception as e:
        logging.warning(f"Cortex: report write failed: {e}")
        return None
