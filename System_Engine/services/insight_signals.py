import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from core.config import INSIGHT_SIGNALS_ENABLED, INSIGHT_REFUTE_ENABLED, INSIGHT_SIGNALS_FILE, PAGES_DIR, NOTES_DIR
from core.parser import strip_body_frontmatter

@dataclass
class InsightSignals:
    groundedness: float        # 0–1
    broken_links: list[str]    # 引用但不存在的頁面
    novelty: float             # 0–1（1 = 全新）
    max_similar_insight: str | None   # 最相似的歷史 insight id
    bridging: float            # 0–1（1 = 連接最遠的聚落）
    refute_verdict: str | None # "survived" | "refuted" | None(未跑)
    refute_notes: str          # 反駁者摘要（≤500 字）

_WIKILINK_RE = re.compile(r'\[\[(.*?)\]\]')
_sidecar_lock = threading.Lock()

def compute_signals(report_content: str, related_titles: list[str], rag, llm, *, run_refute: bool = True) -> InsightSignals:
    if not INSIGHT_SIGNALS_ENABLED:
        return InsightSignals(0.0, [], 0.0, None, 0.0, None, "")
        
    groundedness = 0.0
    broken_links = []
    novelty = 1.0
    max_similar_insight = None
    bridging = 0.0
    refute_verdict = None
    refute_notes = ""
    
    # 1. Groundedness
    try:
        links = _WIKILINK_RE.findall(report_content)
        # Parse aliases like [[A|B]] -> A
        links = [l.split('|')[0].strip() for l in links]
        if not links:
            groundedness = 1.0
        else:
            all_indexed_titles = rag.get_all_indexed_titles() if hasattr(rag, 'get_all_indexed_titles') else set()
            valid_count = 0
            for link in links:
                if link in all_indexed_titles or (PAGES_DIR / f"{link}.md").exists() or (NOTES_DIR / f"{link}.md").exists():
                    valid_count += 1
                else:
                    broken_links.append(link)
            groundedness = valid_count / len(links)
    except Exception as e:
        logging.error(f"InsightSignals: Groundedness calculation failed: {e}")
        
    # 2. Novelty
    try:
        if rag and hasattr(rag, 'ef'):
            core_text, _ = strip_body_frontmatter(report_content)
            core_text = core_text[:2000]
            emb = rag.ef([core_text])[0]
            
            with _sidecar_lock:
                history = {}
                if INSIGHT_SIGNALS_FILE.exists():
                    try:
                        history = json.loads(INSIGHT_SIGNALS_FILE.read_text(encoding="utf-8"))
                    except Exception as e:
                        logging.warning(f"Failed to read {INSIGHT_SIGNALS_FILE}: {e}")
                        history = {}
                
                max_sim = 0.0
                best_id = None
                for hist_id, data in history.items():
                    hist_emb = data.get("embedding")
                    if hist_emb:
                        # Cosine similarity
                        sim = float(np.dot(emb, hist_emb) / (np.linalg.norm(emb) * np.linalg.norm(hist_emb)))
                        if sim > max_sim:
                            max_sim = sim
                            best_id = hist_id
                            
                if best_id:
                    novelty = max(0.0, 1.0 - max_sim)
                    max_similar_insight = best_id
                    
                # Save new embedding (generate a quick id based on content hash)
                import hashlib
                new_id = hashlib.sha256(core_text.encode('utf-8')).hexdigest()[:16]
                history[new_id] = {
                    "embedding": [float(x) for x in emb],
                    "ts": datetime.now(timezone.utc).isoformat()
                }
                
                # Keep only last 500
                if len(history) > 500:
                    sorted_keys = sorted(history.keys(), key=lambda k: history[k].get("ts", ""))
                    history = {k: history[k] for k in sorted_keys[-500:]}
                
                INSIGHT_SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)
                tmp_file = INSIGHT_SIGNALS_FILE.with_suffix('.tmp')
                tmp_file.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
                tmp_file.replace(INSIGHT_SIGNALS_FILE)
    except Exception as e:
        logging.error(f"InsightSignals: Novelty calculation failed: {e}")
        
    # 3. Bridging
    try:
        if rag and hasattr(rag, 'ef') and related_titles and len(related_titles) >= 2:
            titles_to_embed = [str(t) for t in related_titles]
            embs = rag.ef(titles_to_embed)
            if len(embs) == len(titles_to_embed):
                min_sim = 1.0
                for i in range(len(embs)):
                    for j in range(i + 1, len(embs)):
                        sim = float(np.dot(embs[i], embs[j]) / (np.linalg.norm(embs[i]) * np.linalg.norm(embs[j])))
                        if sim < min_sim:
                            min_sim = sim
                bridging = max(0.0, 1.0 - min_sim)
    except Exception as e:
        logging.error(f"InsightSignals: Bridging calculation failed: {e}")
        
    # 4. Refute
    try:
        if run_refute and INSIGHT_REFUTE_ENABLED and llm and hasattr(llm, "refute_insight"):
            candidate_text, _ = strip_body_frontmatter(report_content)
            candidate_text = candidate_text[:2000]
            sources_text = [str(t) for t in (related_titles or [])]
            res = llm.refute_insight(candidate_text, sources_text)
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
