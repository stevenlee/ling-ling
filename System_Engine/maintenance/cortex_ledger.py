"""Cortex claim ledger — falsification and un-merge feedback (Phase 4).

Two responsibilities, both nightly:

1. **Falsified pipeline (conservative kill).** A page becomes a
   falsification candidate only when it has ≥2 contradiction links whose
   counterpart pages trace back to INDEPENDENT insights (single-source
   pile-ons don't count). Each candidate then needs an LLM refute
   confirmation against the contradicting claims before the verdict:
   status=falsified, facets removed, confidence floored — but the file
   stays. It records what we used to believe, and why we stopped.
   Survivors get a cooldown so they aren't re-tried nightly.

2. **Un-merge tracking → adjudication strictness.** The user splitting
   or shrinking a merged page is the ground-truth signal that a merge
   was wrong. The ledger snapshots evidence/variant counts per page;
   shrinkage = un-merge event, growth = merge event. When the un-merge
   rate crosses the strict threshold, consolidation demotes future
   `equivalent` verdicts to links until the rate recovers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from core.config import (
    CORTEX_DIR,
    CORTEX_FALSIFY_PER_NIGHT,
    CORTEX_LEDGER_ENABLED,
    CORTEX_LEDGER_STATE_FILE,
    CORTEX_UNMERGE_MIN_SAMPLES,
    CORTEX_UNMERGE_RELAX_AT,
    CORTEX_UNMERGE_STRICT_AT,
    MAINTENANCE_LOG_FILE,
)
from services.cortex_store import load_all_pages, save_cortex_page

_CONFIDENCE_FLOOR = 0.1
_FALSIFY_COOLDOWN_DAYS = 14
_EVENTS_CAP = 200


@dataclass
class LedgerPassResult:
    status: str                   # "succeeded" | "skipped"
    message: str
    falsified: list = field(default_factory=list)
    candidates_checked: int = 0
    unmerge_events: int = 0
    merge_events: int = 0
    strict_mode: bool = False


def is_adjudication_strict(state_file: Path = None) -> bool:
    """Read by consolidation: strict mode demotes equivalent → link."""
    state_file = state_file or CORTEX_LEDGER_STATE_FILE
    try:
        if state_file.exists():
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return bool(data.get("adjudication_strict", False))
    except Exception:
        pass
    return False


def _load_state(path: Path) -> dict:
    default = {
        "snapshots": {},          # claim_id -> {evidence, variants}
        "events": [],             # {kind: merge|unmerge, claim_id, ts}
        "falsify_checked": {},    # claim_id -> last check date
        "adjudication_strict": False,
    }
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key, value in default.items():
                    data.setdefault(key, value)
                return data
    except Exception as e:
        logging.warning(f"CortexLedger: state unreadable, starting fresh: {e}")
    return default


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logging.warning(f"CortexLedger: state write failed: {e}")


def _independent_insights(pages: list, exclude_grounded_on: str | None = None) -> set[str]:
    """Distinct insight files backing a set of pages.

    F1 defense 1 (falsification side): an insight that was Cortex-grounded ON
    `exclude_grounded_on` was prompted to challenge that very claim — its
    contradiction is dialectical, not INDEPENDENT external evidence, so it must
    not count toward falsifying it. Otherwise the grounding prompt could
    manufacture the dissent that kills its own prior.
    """
    insights = set()
    for page in pages:
        for evidence in page.evidence:
            name = evidence.get("insight")
            if not name:
                continue
            if exclude_grounded_on and exclude_grounded_on in (evidence.get("grounded_on") or []):
                continue
            insights.add(str(name))
    return insights


def run_ledger_pass(
    llm,
    rag,
    *,
    cortex_dir: Path = None,
    state_file: Path = None,
    log_path: Path = None,
    falsify_quota: int = None,
    enabled: bool = None,
    now: datetime = None,
) -> LedgerPassResult:
    cortex_dir = cortex_dir or CORTEX_DIR
    state_file = state_file or CORTEX_LEDGER_STATE_FILE
    log_path = log_path or MAINTENANCE_LOG_FILE
    falsify_quota = falsify_quota if falsify_quota is not None else CORTEX_FALSIFY_PER_NIGHT
    enabled = enabled if enabled is not None else CORTEX_LEDGER_ENABLED
    now = now or datetime.now()

    if not enabled:
        return LedgerPassResult(status="skipped", message="Cortex ledger disabled.")

    pages = load_all_pages(cortex_dir)
    if not pages:
        return LedgerPassResult(status="skipped", message="No cortex pages.")
    by_id = {p.claim_id: p for p in pages}

    state = _load_state(state_file)
    result = LedgerPassResult(status="succeeded", message="")
    now_iso = now.isoformat(timespec="seconds")

    # ── 1. Un-merge / merge event detection ──────────────────────────
    snapshots = state["snapshots"]
    for page in pages:
        snap = snapshots.get(page.claim_id)
        current = {"evidence": len(page.evidence), "variants": len(page.variants)}
        if snap is not None:
            grew = current["evidence"] > snap.get("evidence", 0)
            shrank = (
                current["evidence"] < snap.get("evidence", 0)
                or current["variants"] < snap.get("variants", 0)
            )
            if shrank:
                state["events"].append(
                    {"kind": "unmerge", "claim_id": page.claim_id, "ts": now_iso}
                )
                result.unmerge_events += 1
            elif grew:
                state["events"].append(
                    {"kind": "merge", "claim_id": page.claim_id, "ts": now_iso}
                )
                result.merge_events += 1
        snapshots[page.claim_id] = current
    for claim_id in list(snapshots):
        if claim_id not in by_id:
            del snapshots[claim_id]
    state["events"] = state["events"][-_EVENTS_CAP:]

    # Strictness feedback with its own hysteresis (strict_at > relax_at).
    merges = sum(1 for e in state["events"] if e["kind"] == "merge")
    unmerges = sum(1 for e in state["events"] if e["kind"] == "unmerge")
    total = merges + unmerges
    if total >= CORTEX_UNMERGE_MIN_SAMPLES:
        rate = unmerges / total
        if rate >= CORTEX_UNMERGE_STRICT_AT and not state["adjudication_strict"]:
            state["adjudication_strict"] = True
            logging.warning(
                f"CortexLedger: un-merge rate {rate:.0%} → adjudication STRICT "
                "(equivalent verdicts demote to links)"
            )
        elif rate < CORTEX_UNMERGE_RELAX_AT and state["adjudication_strict"]:
            state["adjudication_strict"] = False
            logging.info(f"CortexLedger: un-merge rate {rate:.0%} → strictness relaxed")
    result.strict_mode = state["adjudication_strict"]

    # ── 2. Falsified pipeline (conservative kill) ────────────────────
    cooldown_cut = (now - timedelta(days=_FALSIFY_COOLDOWN_DAYS)).strftime("%Y-%m-%d")
    candidates = []
    for page in pages:
        if page.status == "falsified" or len(page.contradictions) < 2:
            continue
        if (state["falsify_checked"].get(page.claim_id, "") or "0000")[:10] > cooldown_cut:
            continue
        contradictors = [by_id[c] for c in page.contradictions if c in by_id]
        if len(_independent_insights(contradictors, exclude_grounded_on=page.claim_id)) < 2:
            continue  # single-source pile-on / prompted self-dissent — not independent
        candidates.append((page, contradictors))

    for page, contradictors in candidates[:max(0, falsify_quota)]:
        if not hasattr(llm, "refute_insight"):
            break
        result.candidates_checked += 1
        state["falsify_checked"][page.claim_id] = now_iso
        sources = [
            f"Contradicting claim: {c.claim}（confidence {c.confidence}）"
            for c in contradictors
        ]
        try:
            verdict = llm.refute_insight(page.claim, sources).get("verdict")
        except Exception as e:
            logging.warning(f"CortexLedger: falsify check failed for {page.claim_id}: {e}")
            continue
        if verdict != "refuted":
            continue
        page.status = "falsified"
        page.confidence = _CONFIDENCE_FLOOR
        page.updated = now_iso
        page.counterpoints.append(
            f"Falsified {now.strftime('%Y-%m-%d')}：與 {len(contradictors)} 個獨立主張矛盾且未通過反駁確認"
        )
        save_cortex_page(page)
        try:
            rag.remove_facets(page.path)
        except Exception as e:
            logging.warning(f"CortexLedger: facet removal failed: {e}")
        result.falsified.append(page.claim_id)
        logging.info(f"CortexLedger: falsified {page.claim_id}: {page.claim[:50]}")

    _save_state(state_file, state)

    result.message = (
        f"Cortex ledger: {result.candidates_checked} falsify check(s), "
        f"{len(result.falsified)} falsified; "
        f"merge/unmerge events {result.merge_events}/{result.unmerge_events}"
        + ("; adjudication STRICT" if result.strict_mode else "")
    )
    _append_log(log_path, result)
    return result


def _append_log(log_path: Path, result: LedgerPassResult) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## [{stamp}] Cortex Ledger | {result.message}\n")
    except Exception as e:
        logging.warning(f"CortexLedger: log append failed: {e}")
