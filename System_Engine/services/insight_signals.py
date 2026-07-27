import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from core.config import (
    INSIGHT_SIGNALS_ENABLED,
    INSIGHT_REFUTE_ENABLED,
    INSIGHT_SIGNALS_FILE,
    PAGES_DIR,
    NOTES_DIR,
)
from core.parser import strip_body_frontmatter
from core.retrying import retry_call
from core.vault_utils import sanitize_filename
from services.ingest.atomic_io import atomic_write_text


@dataclass
class InsightSignals:
    groundedness: float | None  # 0–1
    broken_links: list[str] | None  # 引用但不存在的頁面
    novelty: float | None  # 0–1（1 = 全新）
    max_similar_insight: str | None  # 最相似的歷史 insight id
    bridging: float | None  # 0–1（1 = 連接最遠的聚落）
    refute_verdict: str | None  # "survived" | "refuted" | None(未跑)
    refute_notes: str  # 反駁者摘要（≤500 字）


_WIKILINK_RE = re.compile(r"\[\[(.*?)\]\]")
_sidecar_lock = threading.Lock()


def _valid_history_entries(history: object, emb_dim: int) -> tuple[dict, int]:
    """Return consumer-safe sidecar entries and the number discarded."""
    if not isinstance(history, dict):
        return {}, 1
    clean = {}
    for key, data in history.items():
        if not isinstance(key, str) or not isinstance(data, dict):
            continue
        raw_emb = data.get("embedding")
        if not isinstance(raw_emb, list) or len(raw_emb) != emb_dim:
            continue
        try:
            vector = np.asarray(raw_emb, dtype=float)
        except (TypeError, ValueError):
            continue
        if not np.all(np.isfinite(vector)) or np.linalg.norm(vector) <= 0:
            continue
        clean[key] = data
    return clean, len(history) - len(clean)


def compute_signals(
    report_content: str,
    related_titles: list[str],
    rag,
    llm,
    *,
    run_refute: bool = True,
    update_history: bool = True,
    refute_lenient: bool = False,
    refute_kind: str | None = None,
) -> InsightSignals:
    if not INSIGHT_SIGNALS_ENABLED:
        return InsightSignals(None, None, None, None, None, None, "")

    groundedness = None
    broken_links = None
    novelty = None
    max_similar_insight = None
    bridging = None
    refute_verdict = None
    refute_notes = ""

    # 1. Groundedness
    try:
        links = _WIKILINK_RE.findall(report_content)
        # Parse aliases like [[A|B]] -> A
        links = [l.split("|")[0].strip() for l in links]
        if not links:
            groundedness = 1.0
            broken_links = []
        else:
            all_indexed_titles = (
                rag.get_all_indexed_titles() if hasattr(rag, "get_all_indexed_titles") else set()
            )
            # Pages live in nested per-document folders (pages/<Doc>/<Doc> (Synthesis).md),
            # so a flat PAGES_DIR / f"{link}.md" check misses them — match by stem instead.
            vault_stems: set[str] = set()
            for directory in (PAGES_DIR, NOTES_DIR):
                if directory.exists():
                    vault_stems.update(p.stem for p in directory.rglob("*.md"))
            valid_count = 0
            broken_links = []
            for link in links:
                if (
                    link in all_indexed_titles
                    or link in vault_stems
                    or sanitize_filename(link) in vault_stems
                ):
                    valid_count += 1
                else:
                    broken_links.append(link)
            groundedness = valid_count / len(links)
    except Exception as e:
        logging.error(f"InsightSignals: Groundedness calculation failed: {e}")

    # 2. Novelty
    try:
        if rag and hasattr(rag, "ef"):
            core_text, _ = strip_body_frontmatter(report_content)
            core_text = core_text[:2000]
            batch = retry_call(
                lambda: rag.ef([core_text]),
                retries=2,
                initial_delay=0.5,
                log_label="InsightSignals novelty embedding",
            )
            if not isinstance(batch, (list, tuple)) or len(batch) != 1:
                raise ValueError("embedding backend did not return exactly one vector")
            emb = np.asarray(batch[0], dtype=float)
            if emb.ndim != 1 or len(emb) == 0:
                raise ValueError("embedding backend returned an empty or non-vector result")
            if not np.all(np.isfinite(emb)) or np.linalg.norm(emb) <= 0:
                raise ValueError("embedding backend returned a non-finite or zero-norm vector")

            with _sidecar_lock:
                history = {}
                if INSIGHT_SIGNALS_FILE.exists():
                    try:
                        history = json.loads(INSIGHT_SIGNALS_FILE.read_text(encoding="utf-8"))
                    except Exception as e:
                        logging.warning(f"Failed to read {INSIGHT_SIGNALS_FILE}: {e}")
                        history = {}

                emb_dim = len(emb)
                history, discarded = _valid_history_entries(history, emb_dim)
                if discarded:
                    logging.info(
                        f"InsightSignals: purging {discarded} malformed, non-finite, "
                        f"zero-norm, or dimension-mismatched sidecar entries (expected {emb_dim})"
                    )

                # Content-hash id, computed up front so the comparison loop can
                # skip the insight's own earlier entry — otherwise recomputing
                # signals for the same content (backfill re-sign, retries)
                # self-matches at sim 1.0 and reports novelty 0.
                import hashlib

                new_id = hashlib.sha256(core_text.encode("utf-8")).hexdigest()[:16]

                max_sim = 0.0
                best_id = None
                for hist_id, data in history.items():
                    if hist_id == new_id:
                        continue
                    hist_emb = data.get("embedding")
                    if hist_emb:
                        # Cosine similarity
                        sim = float(
                            np.dot(emb, hist_emb) / (np.linalg.norm(emb) * np.linalg.norm(hist_emb))
                        )
                        if sim > max_sim:
                            max_sim = sim
                            best_id = hist_id

                if best_id:
                    novelty = max(0.0, 1.0 - max_sim)
                    max_similar_insight = best_id
                else:
                    novelty = 1.0

                if update_history:
                    history[new_id] = {
                        "embedding": [float(x) for x in emb],
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }

                    # Keep only last 500
                    if len(history) > 500:
                        sorted_keys = sorted(history.keys(), key=lambda k: history[k].get("ts", ""))
                        history = {k: history[k] for k in sorted_keys[-500:]}

                    atomic_write_text(
                        INSIGHT_SIGNALS_FILE,
                        json.dumps(history, ensure_ascii=False),
                    )
    except Exception:
        logging.exception("InsightSignals: Novelty calculation failed")

    def _load_source_contents(titles: list[str]) -> list[str]:
        contents = []
        for title in titles:
            title = sanitize_filename(title)
            # rglob: pages are nested per-document (pages/<Doc>/<Doc> (Synthesis).md)
            target = next(PAGES_DIR.rglob(f"{title}.md"), None) if PAGES_DIR.exists() else None
            if target is None and NOTES_DIR.exists():
                target = next(NOTES_DIR.rglob(f"{title}.md"), None)
            if target:
                try:
                    raw = target.read_text(encoding="utf-8")
                    core, _ = strip_body_frontmatter(raw)
                    contents.append(core[:2000])
                except Exception:
                    pass
        return contents

    # 3. Bridging
    try:
        if rag and hasattr(rag, "ef") and related_titles:
            source_contents = _load_source_contents(related_titles)
            if len(source_contents) >= 2:
                embs = rag.ef(source_contents)
                if len(embs) == len(source_contents):
                    min_sim = 1.0
                    for i in range(len(embs)):
                        for j in range(i + 1, len(embs)):
                            sim = float(
                                np.dot(embs[i], embs[j])
                                / (np.linalg.norm(embs[i]) * np.linalg.norm(embs[j]))
                            )
                            if sim < min_sim:
                                min_sim = sim
                    bridging = max(0.0, 1.0 - min_sim)
            else:
                bridging = 0.0
    except Exception as e:
        logging.error(f"InsightSignals: Bridging calculation failed: {e}")

    # 4. Refute
    try:
        if run_refute and INSIGHT_REFUTE_ENABLED and llm and hasattr(llm, "refute_insight"):
            candidate_text, _ = strip_body_frontmatter(report_content)
            candidate_text = candidate_text[:2000]
            source_contents = _load_source_contents(related_titles or [])
            if not source_contents:
                refute_verdict = None
                refute_notes = "no source content available"
            else:
                res = llm.refute_insight(
                    candidate_text,
                    source_contents,
                    lenient=refute_lenient,
                    candidate_kind=refute_kind,
                )
                refute_verdict = res.get("verdict")
                refute_notes = res.get("notes", "")
    except Exception as e:
        logging.error(f"InsightSignals: Refute calculation failed: {e}")

    return InsightSignals(
        groundedness=groundedness,
        broken_links=broken_links,
        novelty=novelty,
        max_similar_insight=max_similar_insight,
        bridging=bridging,
        refute_verdict=refute_verdict,
        refute_notes=refute_notes,
    )
