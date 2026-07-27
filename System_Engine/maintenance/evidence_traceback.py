"""Cortex evidence traceback (A2) — falsifier-first corroboration scan, dry-run.

98.8% of Cortex claims carry exactly one evidence entry: each belief was born
from a single insight and never corroborated again. The N.1 observation window
(2026-07-24 re-audit) measured that the three passive levers do NOT thicken
evidence — grounding reinforces S but never appends evidence entries, merges
need a rare `equivalent` adjudication, decay only fades. Evidence has to be
looked for, so this task goes looking.

FALSIFIER-FIRST: each claim already states what observation would refute it
(`falsifier`). The scan queries the vault for THAT scenario before it queries
for the claim itself — a stress test, not confirmation harvesting. This is the
anti-echo-chamber discipline: a belief distilled from the vault must not count
re-finding its own origin as new support, so derivative content (Cortex/,
Insights/, own reports) and the claim's originating sources are excluded from
candidates.

DRY-RUN ONLY (current form): judgments are written to a fromLingLing/ report;
NOTHING in Cortex state is mutated. The apply path (append evidence / record
tension / reinforce) ships separately once dry-run hit rates have been
reviewed. The only file this task owns is its rotation-cursor state file —
task bookkeeping, not belief state.

Failure semantics (per house hardening rules): an LLM failure or unparseable
relation is COUNTED AND SHOWN as such, never coerced into "neutral" — a
failure is not a verdict. One claim failing never aborts the batch.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.config import (
    CORTEX_DIR,
    EVIDENCE_TRACEBACK_STATE_FILE,
    FROM_LLM_DIR,
    settings,
)
from services.cortex_store import active_evidence, load_all_pages
from services.ingest.atomic_io import atomic_write_text

# Candidate passages actually judged per claim (post-exclusion cap): keeps the
# nightly LLM spend bounded at batch × _MAX_PASSAGES lean calls.
_MAX_PASSAGES = 3
_TOP_K = 5

_VALID_RELATIONS = ("supports", "contradicts", "neutral")
_DERIVATIVE_ROOTS = {"cortex", "insights", "fromlingling"}
_SOURCE_VARIANT_RE = re.compile(r"\s+\((?:Part\s+\d+|Stitched|Synthesis)\)$", re.IGNORECASE)

# Control-plane JSON-extraction prompt: stays in code on purpose (the
# content/control boundary from the prompt-system review — vault files carry
# voice and methodology, structured-output contracts stay next to their
# parser).
_SYSTEM = (
    "You are a rigorous evidence auditor for a personal knowledge base.\n"
    "Given a CLAIM, its FALSIFIER (the observation that would refute it), and one\n"
    "PASSAGE retrieved from the knowledge base, judge the passage's evidential\n"
    "relation to the claim.\n"
    'Return JSON only: {"relation": "supports" | "contradicts" | "neutral",\n'
    ' "reason": "<one sentence, Traditional Chinese>"}\n'
    "- contradicts: the passage describes or strongly implies the falsifier\n"
    "  scenario, or otherwise conflicts with the claim.\n"
    "- supports: the passage INDEPENDENTLY corroborates the claim — not merely\n"
    "  restating it.\n"
    "- neutral: related topic, no evidential force either way.\n"
    "Judge evidential force only. Do not invent content beyond the passage."
)


@dataclass
class PassageJudgment:
    title: str
    relation: str  # supports / contradicts / neutral / unparseable / error
    reason: str = ""
    distance: float | None = None


@dataclass
class ClaimScan:
    claim_id: str
    claim: str
    falsifier: str
    judgments: list[PassageJudgment] = field(default_factory=list)
    excluded_self: int = 0
    excluded_far: int = 0


@dataclass
class TracebackResult:
    status: str
    summary: str
    report_path: Path | None = None
    scans: list[ClaimScan] = field(default_factory=list)


def _load_state(state_file: Path) -> dict:
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("checked"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.warning(f"evidence_traceback: unreadable state file, starting fresh: {e}")
    return {"checked": {}}


def _thin_pool(pages) -> list:
    return [
        p
        for p in pages
        if p.status == "active" and len(active_evidence(p)) <= 1 and p.claim.strip()
    ]


def _select_thin_claims(pool, state: dict, batch: int):
    """Thin-evidence claims, least-recently-checked first.

    The rotation cursor (state["checked"]) is what stops the same top-N claims
    from burning the whole nightly budget every run; never-checked claims sort
    ahead of any checked one, oldest claim first within a tier.
    """
    checked = state.get("checked", {})
    ordered = sorted(pool, key=lambda p: (checked.get(p.claim_id, ""), p.created, p.claim_id))
    return ordered[: max(0, batch)]


def _is_independent(hit: dict, page) -> tuple[bool, str]:
    """Whether a retrieval hit counts as INDEPENDENT evidence for this claim.

    Excluded: derivative content (Cortex pages, Insights output, own reports —
    a belief must not corroborate itself through its own downstream artifacts)
    and the claim's originating sources (re-finding the origin is the same
    single observation, not a second one).
    """
    meta = hit.get("metadata") or {}
    title = str(meta.get("title") or "").strip()
    path = str(meta.get("source_path") or meta.get("source") or "")
    if not title:
        return False, "untitled"
    if title.casefold().startswith("cortex-") or title.casefold() == str(page.claim_id).casefold():
        return False, "derivative"
    if title.lstrip("✅").strip().startswith(("Scout-", "Dig-", "EvidenceTrace-")):
        return False, "own-report"
    path_parts = {part.casefold() for part in path.replace("\\", "/").split("/") if part.strip()}
    if path_parts & _DERIVATIVE_ROOTS:
        return False, "derivative"
    # Insight filenames truncate long source titles, so origin matching works
    # on a prefix — but only when the prefix is long enough to be distinctive
    # (a 3-char title would substring-match half the vault). A false
    # "independent" here only costs one judged passage (and shows up in the
    # report for review), never a state write.
    prefix = title[:24]
    fuzzy = len(prefix) >= 6
    for entry in page.evidence:
        origin = str(entry.get("insight") or "")
        if (prefix in origin) if fuzzy else (title == origin):
            return False, "self-source"
        for source in entry.get("sources") or []:
            s = str(source)
            if (prefix in s or s[:24] in title) if fuzzy else (title == s):
                return False, "self-source"
    return True, ""


def _source_family(hit: dict) -> str:
    """Canonical underlying document key across Part/Stitched/Synthesis views."""
    meta = hit.get("metadata") or {}
    source = str(meta.get("source_path") or meta.get("source") or "").strip()
    source_path = Path(source.replace("\\", "/")) if source else None
    name = source_path.stem if source_path else ""
    name = name or str(meta.get("title") or "").strip()
    previous = None
    while name and name != previous:
        previous = name
        name = _SOURCE_VARIANT_RE.sub("", name).strip()
    if source_path:
        parent = source_path.parent.as_posix().casefold()
        return f"{parent}/{name.casefold()}" if parent != "." else name.casefold()
    return name.casefold()


def _candidate_passages(rag, page, max_distance: float) -> tuple[list[dict], int, int]:
    """Falsifier-first retrieval: the falsifier query runs FIRST and its hits
    rank ahead of claim-text hits at equal footing (stress test before
    corroboration). Returns (passages, excluded_self, excluded_far)."""
    queries = []
    if page.falsifier.strip():
        queries.append(page.falsifier.strip()[:500])
    queries.append(page.claim.strip()[:500])

    candidates: dict[str, dict] = {}
    candidate_order: list[str] = []
    excluded_self = 0
    excluded_far = 0
    for query in queries:
        try:
            hits = rag.query_notes(query, top_k=_TOP_K) or []
        except Exception as e:
            logging.warning(f"evidence_traceback: retrieval failed for {page.claim_id}: {e}")
            continue
        for hit in hits:
            key = _source_family(hit)
            if not key:
                key = f"untitled:{len(candidate_order)}"
            previous = candidates.get(key)
            if previous is None:
                candidates[key] = hit
                candidate_order.append(key)
                continue
            old_distance = previous.get("distance")
            new_distance = hit.get("distance")
            old_rank = (
                float(old_distance)
                if isinstance(old_distance, (int, float)) and math.isfinite(float(old_distance))
                else math.inf
            )
            new_rank = (
                float(new_distance)
                if isinstance(new_distance, (int, float)) and math.isfinite(float(new_distance))
                else math.inf
            )
            if new_rank < old_rank:
                candidates[key] = hit

    passages: list[dict] = []
    for key in candidate_order:
        hit = candidates[key]
        distance = hit.get("distance")
        if isinstance(distance, (int, float)) and (
            not math.isfinite(float(distance)) or distance > max_distance
        ):
            excluded_far += 1
            continue
        independent, _why = _is_independent(hit, page)
        if not independent:
            excluded_self += 1
            continue
        passages.append(hit)
        if len(passages) >= _MAX_PASSAGES:
            break
    return passages, excluded_self, excluded_far


def _judge_passage(llm, page, hit: dict) -> PassageJudgment:
    meta = hit.get("metadata") or {}
    title = str(meta.get("title") or "").strip()
    distance = hit.get("distance") if isinstance(hit.get("distance"), (int, float)) else None
    text = str(hit.get("text") or "")[:2000]
    user_msg = (
        f"## CLAIM\n{page.claim}\n\n"
        f"## FALSIFIER\n{page.falsifier or '(none stated)'}\n\n"
        f"## PASSAGE（來源：{title}）\n{text}\n"
    )
    try:
        parsed = llm._complete_json(
            kind="object",
            system_prompt=_SYSTEM,
            user_msg=user_msg,
            temperature=0.1,
            trace_context={
                "stage": "evidence_traceback",
                "metadata": {"claim_id": page.claim_id, "passage_title": title},
            },
        )
    except Exception as e:
        logging.warning(f"evidence_traceback: judgment failed for {page.claim_id}: {e}")
        return PassageJudgment(
            title=title, relation="error", reason=str(e)[:200], distance=distance
        )

    relation = (
        str((parsed or {}).get("relation") or "").strip().lower()
        if isinstance(parsed, dict)
        else ""
    )
    reason = (
        str((parsed or {}).get("reason") or "").strip()[:300] if isinstance(parsed, dict) else ""
    )
    if relation not in _VALID_RELATIONS:
        # An unreadable verdict is reported as unparseable, never coerced to
        # neutral — same discipline as the synthesis critique gate.
        return PassageJudgment(
            title=title, relation="unparseable", reason=reason, distance=distance
        )
    return PassageJudgment(title=title, relation=relation, reason=reason, distance=distance)


def _dry_run_action(scan: ClaimScan) -> str:
    relations = {j.relation for j in scan.judgments}
    if "contradicts" in relations:
        return "記 tension（falsifier 情境有跡可循，主張待壓力複核）"
    if "supports" in relations:
        return "+1 evidence（獨立佐證）＋強化"
    return "無（維持薄證據，交由 decay）"


def _render_report(scans: list[ClaimScan], pool_size: int, now: datetime) -> str:
    counts = {"supports": 0, "contradicts": 0, "neutral": 0, "unparseable": 0, "error": 0}
    for scan in scans:
        for j in scan.judgments:
            counts[j.relation] = counts.get(j.relation, 0) + 1
    lines = [
        f"# 🌱 證據追溯（dry-run）{now:%Y-%m-%d}",
        "",
        "> Cortex 薄證據主張的 falsifier-first 佐證掃描。**本報告為 dry-run：未寫入任何",
        "> Cortex 狀態**——「擬動作」僅記錄若開啟 apply 將會發生的事。",
        "",
        f"- 薄證據池：{pool_size} 條主張；本輪掃描 {len(scans)} 條",
        f"- 段落判定：supports {counts['supports']} ・ contradicts {counts['contradicts']}"
        f" ・ neutral {counts['neutral']} ・ 無法解析 {counts['unparseable']} ・ 錯誤 {counts['error']}",
        "",
    ]
    for scan in scans:
        lines.append(f"## {scan.claim[:80]}")
        lines.append(f"- claim_id：`{scan.claim_id}`")
        lines.append(f"- falsifier：{scan.falsifier or '（未陳述）'}")
        lines.append(
            f"- 候選段落 {len(scan.judgments)}（排除自我來源 {scan.excluded_self}、距離過遠 {scan.excluded_far}）"
        )
        for j in scan.judgments:
            dist = f"（distance {j.distance:.2f}）" if j.distance is not None else ""
            lines.append(f"  - [{j.relation}] {j.title} — {j.reason}{dist}")
        lines.append(f"- 擬動作：{_dry_run_action(scan)}")
        lines.append("")
    return "\n".join(lines)


def run_evidence_traceback(
    llm,
    rag,
    *,
    cortex_dir: Path | None = None,
    out_dir: Path | None = None,
    state_file: Path | None = None,
    now: datetime | None = None,
) -> TracebackResult:
    """One dry-run traceback batch. Read-only against Cortex and the RAG index;
    writes exactly two files it owns: the dated report and the rotation cursor."""
    cortex_dir = cortex_dir or CORTEX_DIR
    out_dir = out_dir or FROM_LLM_DIR
    state_file = state_file or EVIDENCE_TRACEBACK_STATE_FILE
    now = now or datetime.now()

    if rag is None:
        return TracebackResult("skipped", "Evidence traceback skipped: no RAG available.")

    pages = load_all_pages(cortex_dir)
    state = _load_state(state_file)
    pool = _thin_pool(pages)
    batch = _select_thin_claims(pool, state, settings.EVIDENCE_TRACEBACK_BATCH)
    if not batch:
        return TracebackResult("ok", "Evidence traceback: no thin-evidence claims to scan.")

    scans: list[ClaimScan] = []
    for page in batch:
        passages, excluded_self, excluded_far = _candidate_passages(
            rag, page, settings.EVIDENCE_TRACEBACK_MAX_DISTANCE
        )
        scan = ClaimScan(
            claim_id=page.claim_id,
            claim=page.claim,
            falsifier=page.falsifier,
            excluded_self=excluded_self,
            excluded_far=excluded_far,
        )
        scan.judgments = [_judge_passage(llm, page, hit) for hit in passages]
        scans.append(scan)
        state.setdefault("checked", {})[page.claim_id] = now.isoformat(timespec="seconds")

    report_path = out_dir / f"✅EvidenceTrace-{now:%Y-%m-%d}.md"
    atomic_write_text(report_path, _render_report(scans, len(pool), now))
    atomic_write_text(state_file, json.dumps(state, ensure_ascii=False, indent=2))

    judged = sum(len(s.judgments) for s in scans)
    hits = sum(1 for s in scans for j in s.judgments if j.relation in ("supports", "contradicts"))
    summary = (
        f"Evidence traceback (dry-run): {len(scans)} claims scanned "
        f"({len(pool)} thin), {judged} passages judged, {hits} evidential hits → {report_path.name}"
    )
    return TracebackResult("ok", summary, report_path=report_path, scans=scans)
