"""Cortex evidence traceback (A2) — versioned passage-scope corroboration scan.

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

TWO MODES:
  * DRY-RUN (default, `evidence_traceback_apply: false`): judgments go to a
    fromLingLing/ report; NOTHING in Cortex state is mutated.
  * APPLY (`evidence_traceback_apply: true`): only a v2 full-scope verdict can
    strengthen a claim. Historical v1 hit rates do not validate this contract.
    With apply enabled, the scan MUTATES the claim, conservatively:
      - full_support → append an independent-evidence entry (keyed by an
        [EvidenceTrace] marker so a re-scan of the same source is idempotent),
        then a SINGLE gentle reinforce (GAIN_REVALIDATION) + small confidence
        step per apply. Thickens the belief's evidence — the whole point.
      - contradicts → record the passage as a COUNTERPOINT (human-visible in
        the rendered page). NO reinforce, NO confidence change, NO auto-flip —
        surfaced for human adjudication, not decided by the scan.
      - partial_support / analogy / neutral / unparseable / error → no mutation.
    Mutations go through save_cortex_page + a consolidation-style re-index, all
    inside the daemon where the maintenance scheduler serialises Cortex writers
    (single-writer discipline). The [EvidenceTrace] provenance and the derivative
    self-source exclusion keep this from becoming an echo chamber.

The only OTHER file this task owns is its rotation-cursor state file — task
bookkeeping, not belief state.

Failure semantics (per house hardening rules): an LLM failure or unparseable
relation is COUNTED AND SHOWN as such, never coerced into "neutral" — a
failure is not a verdict. One claim failing (judgment OR apply) never aborts
the batch.
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
    CORTEX_INDEX_REPAIR_DIR,
    EVIDENCE_TRACEBACK_STATE_FILE,
    FROM_LLM_DIR,
    settings,
)
from services.cortex_store import (
    active_evidence,
    load_all_pages,
    save_cortex_page,
)
from services.ingest.atomic_io import atomic_write_text
from services.llm_client import JSONCompletionParseError
from services.evidence_contract import (
    CONTRACT_VERSION,
    PASSAGE_LIMIT,
    RELATIONS,
    validate_scope_verdict,
)
from maintenance.cortex_index_repair import (
    enqueue_index_repair,
    repair_index_slot,
    repair_pending_indexes,
)

# Candidate passages actually judged per claim (post-exclusion cap): keeps the
# nightly LLM spend bounded at batch × _MAX_PASSAGES lean calls.
_MAX_PASSAGES = 3
_TOP_K = 5

_DERIVATIVE_ROOTS = {"cortex", "insights", "fromlingling"}
_SOURCE_VARIANT_RE = re.compile(r"\s+\((?:Part\s+\d+|Stitched|Synthesis)\)$", re.IGNORECASE)

# Apply-mode provenance marker: A2 evidence entries are keyed by this + the
# source title so a re-scan of the same corroborating passage refreshes in
# place (idempotent) instead of double-counting. It also visibly distinguishes
# traceback-sourced evidence from insight-sourced evidence in the rendered page.
_TRACE_MARKER = "[EvidenceTrace]"
# Confidence bump per apply that adds ≥1 independent evidence — deliberately
# smaller than consolidation's rediscovery step (0.1) and capped below its cap
# (0.9): a corroborating passage is weaker signal than an independent insight
# rediscovery, so it nudges rather than climbs.
_APPLY_CONFIDENCE_STEP = 0.05
_APPLY_CONFIDENCE_CAP = 0.8


@dataclass
class PassageJudgment:
    title: str
    relation: str  # RELATIONS / unparseable / error
    reason: str = ""
    distance: float | None = None
    contract_version: int | None = None
    quotes: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    applicability: str = ""
    passage: str = ""

    def verdict(self) -> dict:
        return {
            "contract_version": self.contract_version,
            "relation": self.relation,
            "reason": self.reason,
            "quotes": self.quotes,
            "unsupported": self.unsupported,
            "applicability": self.applicability,
        }


@dataclass
class ClaimScan:
    claim_id: str
    claim: str
    falsifier: str
    judgments: list[PassageJudgment] = field(default_factory=list)
    excluded_self: int = 0
    excluded_far: int = 0
    excluded_unknown: int = 0
    retrieval_errors: list[str] = field(default_factory=list)
    # Apply-mode outcome (None in dry-run): what was actually written to Cortex.
    applied: dict | None = None


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
    # "independent" can reach apply mode: this heuristic is only an origin
    # exclusion, not proof of original-source independence (A04 tracks lineage).
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


@dataclass
class PassageCandidates:
    passages: list[dict] = field(default_factory=list)
    excluded_self: int = 0
    excluded_far: int = 0
    excluded_unknown: int = 0
    errors: list[str] = field(default_factory=list)


def _passage_distance(hit: dict) -> float | None:
    """Use measured passage distance, never BM25/facet placeholder zeroes."""
    if "matched_facet" in hit:
        return None  # facet distance measures a different text than this passage
    breakdown = hit.get("retrieval_breakdown")
    if "retrieval_breakdown" in hit:
        if not isinstance(breakdown, dict):
            return None
        if "facet_deref" in (breakdown.get("passed_layers") or []):
            return None
        value = breakdown.get("vector_distance")
    else:
        # Legacy/direct vector providers expose only distance. The production
        # RAGManager always supplies a breakdown, including None for BM25-only.
        value = hit.get("distance")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    distance = float(value)
    return distance if math.isfinite(distance) and distance >= 0 else None


def _candidate_passages(rag, page, max_distance: float) -> PassageCandidates:
    """Falsifier-first retrieval: the falsifier query runs FIRST and its hits
    rank ahead of claim-text hits at equal footing (stress test before
    corroboration). Missing distances and retrieval errors remain explicit."""
    queries = []
    if page.falsifier.strip():
        queries.append(page.falsifier.strip()[:500])
    queries.append(page.claim.strip()[:500])

    candidates: dict[str, dict] = {}
    candidate_order: list[str] = []
    result = PassageCandidates()
    for query in queries:
        try:
            hits = rag.query_notes(query, top_k=_TOP_K, raise_on_error=True) or []
        except Exception as e:
            logging.warning(f"evidence_traceback: retrieval failed for {page.claim_id}: {e}")
            result.errors.append(str(e)[:200])
            continue
        for raw_hit in hits:
            hit = {**raw_hit, "distance": _passage_distance(raw_hit)}
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

    for key in candidate_order:
        hit = candidates[key]
        distance = hit.get("distance")
        if distance is None:
            result.excluded_unknown += 1
            continue
        if distance > max_distance:
            result.excluded_far += 1
            continue
        independent, _why = _is_independent(hit, page)
        if not independent:
            result.excluded_self += 1
            continue
        result.passages.append(hit)
        if len(result.passages) >= _MAX_PASSAGES:
            break
    return result


def _judge_passage(llm, page, hit: dict) -> PassageJudgment:
    meta = hit.get("metadata") or {}
    title = str(meta.get("title") or "").strip()
    distance = hit.get("distance") if isinstance(hit.get("distance"), (int, float)) else None
    raw_text = hit.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        return PassageJudgment(
            title=title, relation="unparseable", reason="缺少有效段落", distance=distance
        )
    text = raw_text[:PASSAGE_LIMIT]
    try:
        parsed = llm.judge_evidence_scope(
            claim=page.claim,
            falsifier=page.falsifier,
            title=title,
            passage=text,
            claim_id=page.claim_id,
        )
        verdict = validate_scope_verdict(parsed, text)
    except (JSONCompletionParseError, ValueError) as e:
        return PassageJudgment(
            title=title, relation="unparseable", reason=str(e)[:200], distance=distance
        )
    except Exception as e:
        logging.warning(f"evidence_traceback: judgment failed for {page.claim_id}: {e}")
        return PassageJudgment(
            title=title, relation="error", reason=str(e)[:200], distance=distance
        )
    return PassageJudgment(title=title, distance=distance, passage=text, **verdict)


def _scan_incomplete(scan: ClaimScan) -> bool:
    return bool(scan.retrieval_errors) or any(
        j.relation in ("error", "unparseable") for j in scan.judgments
    )


def _dry_run_action(scan: ClaimScan) -> str:
    if _scan_incomplete(scan):
        return "判讀未完成，本輪不套用"
    relations = {j.relation for j in scan.judgments}
    if "contradicts" in relations:
        return "記 tension（falsifier 情境有跡可循，主張待壓力複核）"
    if "full_support" in relations:
        return "+1 evidence（完整支持；來源獨立性仍依排除規則）＋強化"
    if relations & {"partial_support", "analogy"}:
        return "僅留報告（局部支持／類比，不強化）"
    return "無（維持薄證據，交由 decay）"


def _apply_scan(
    page, scan: ClaimScan, rag, now: datetime, *, repair_dir: Path | None = None
) -> dict:
    """Mutate the claim page from its judgments (apply mode). Conservative:
    full_support thickens evidence + a single gentle reinforce; contradicts become
    human-visible counterpoints with no confidence change. Idempotent across
    re-scans via the [EvidenceTrace] marker. Returns an actions dict; on any
    pre-commit failure returns {"error": <str>}. Post-commit index failure
    keeps the applied counts and a durable intent, never claims rollback."""
    from services.cortex_decay import GAIN_REVALIDATION, reinforce

    # The consumer also validates: hand-built/legacy judgments cannot bypass
    # the model adapter, and an incomplete batch cannot partially strengthen.
    if _scan_incomplete(scan):
        return {"deferred": True}
    if (page.claim_id, page.claim, page.falsifier) != (scan.claim_id, scan.claim, scan.falsifier):
        return {"error": "主張或 falsifier 已改變，必須重新判讀"}
    try:
        for judgment in scan.judgments:
            validate_scope_verdict(judgment.verdict(), judgment.passage)
    except ValueError as validation_error:
        return {"error": f"判讀契約無效：{validation_error}"}

    added_evidence = 0
    refreshed = 0
    tensions = 0
    existing_markers = {e.get("insight") for e in page.evidence if isinstance(e, dict)}
    existing_counterpoints = list(page.counterpoints)

    for j in scan.judgments:
        marker = f"{_TRACE_MARKER} {j.title}"
        if j.relation == "full_support":
            entry = {
                "insight": marker,
                "sources": [j.title],
                "date": now.strftime("%Y-%m-%d"),
                "summary": j.reason or "（追溯佐證）",
                "method": "evidence_traceback",
                "scope_contract_version": CONTRACT_VERSION,
                "scope_relation": j.relation,
                "quotes": j.quotes,
                "applicability": j.applicability,
            }
            if marker in existing_markers:
                # Same corroborating source found again — refresh in place, no
                # double-count and no extra reinforcement (mirrors consolidation's
                # revision semantics).
                for i, e in enumerate(page.evidence):
                    if isinstance(e, dict) and e.get("insight") == marker:
                        page.evidence[i] = entry
                        break
                refreshed += 1
            else:
                page.evidence.append(entry)
                existing_markers.add(marker)
                added_evidence += 1
        elif j.relation == "contradicts":
            cp = f"{_TRACE_MARKER} {now:%Y-%m-%d} 反例：{j.title} — {j.reason}"
            # Dedup on the source title so a re-scan doesn't stack counterpoints.
            if not any(j.title in existing for existing in existing_counterpoints):
                page.counterpoints.append(cp)
                existing_counterpoints.append(cp)
                tensions += 1

    # A single reinforce + confidence nudge per apply that added NEW independent
    # evidence (not per passage — two supports from one scan is still one
    # corroboration event). Refresh-only and contradicts never reinforce.
    reinforced = False
    if added_evidence:
        reinforce(page, GAIN_REVALIDATION, now=now)
        page.confidence = round(
            min(_APPLY_CONFIDENCE_CAP, float(page.confidence) + _APPLY_CONFIDENCE_STEP), 4
        )
        reinforced = True
    if added_evidence or refreshed or tensions:
        page.updated = now.isoformat(timespec="seconds")

    if not (added_evidence or refreshed or tensions):
        return {"added_evidence": 0, "refreshed": 0, "tensions": 0, "reinforced": False}

    repair_dir = repair_dir or CORTEX_INDEX_REPAIR_DIR
    try:
        # Write-ahead intent closes the crash gap between the vault commit and
        # the DB call. If it cannot persist, do not commit the belief mutation.
        slot = enqueue_index_repair(page, repair_dir, page.path.parent)
        save_cortex_page(page)
    except Exception as e:
        logging.error(f"evidence_traceback: apply write failed for {page.claim_id}: {e}")
        return {"error": str(e)[:200]}

    applied: dict = {
        "added_evidence": added_evidence,
        "refreshed": refreshed,
        "tensions": tensions,
        "reinforced": reinforced,
    }
    if rag is None:
        status, error = "waiting", "尚無 RAG，保留索引意圖"
    else:
        status, error = repair_index_slot(slot, rag, page.path.parent, now)
    if status != "repaired":
        applied.update(index_pending=True, index_error=error or "等待索引追補")
    return applied


def _applied_action(applied: dict | None) -> str:
    if not applied:
        return "無（維持薄證據，交由 decay）"
    if applied.get("deferred"):
        return "判讀未完成，本輪未套用"
    if applied.get("error"):
        return f"⚠️ 套用失敗：{applied['error']}"
    parts = []
    if applied.get("added_evidence"):
        suffix = "＋強化" if applied.get("reinforced") else ""
        parts.append(f"+{applied['added_evidence']} evidence{suffix}")
    if applied.get("refreshed"):
        parts.append(f"{applied['refreshed']} 筆既有佐證刷新（不重複計）")
    if applied.get("tensions"):
        parts.append(f"記 {applied['tensions']} 筆 tension（counterpoint，待人審）")
    if applied.get("index_pending"):
        parts.append(f"Cortex 已生效但待索引：{applied.get('index_error', '')}")
    return "已套用：" + "、".join(parts) if parts else "無（維持薄證據，交由 decay）"


def _render_report(
    scans: list[ClaimScan],
    pool_size: int,
    now: datetime,
    *,
    apply: bool,
    repair_summary: str = "",
    repair_errors: list[str] | None = None,
) -> str:
    counts = dict.fromkeys((*RELATIONS, "unparseable", "error"), 0)
    for scan in scans:
        for j in scan.judgments:
            counts[j.relation] = counts.get(j.relation, 0) + 1
    if apply:
        header = f"# 🌱 證據追溯（apply）{now:%Y-%m-%d}"
        blurb = (
            "> Cortex 薄證據主張的 falsifier-first 佐證掃描。**apply 模式：僅 full_support 可寫入\n"
            "> 佐證＋溫和強化、contradicts 可記為 counterpoint 待人審；局部支持與類比僅留報告。**"
        )
        action_label = "動作"
        applied_ev = sum((s.applied or {}).get("added_evidence", 0) for s in scans)
        applied_tn = sum((s.applied or {}).get("tensions", 0) for s in scans)
        applied_err = sum(1 for s in scans if (s.applied or {}).get("error"))
        apply_line = (
            f"- 本輪套用：+{applied_ev} evidence、{applied_tn} tension"
            f"{f'、⚠️ {applied_err} 筆寫入失敗' if applied_err else ''}"
        )
    else:
        header = f"# 🌱 證據追溯（dry-run）{now:%Y-%m-%d}"
        blurb = (
            "> Cortex 薄證據主張的 falsifier-first 佐證掃描。**本報告為 dry-run：未寫入任何\n"
            "> Cortex 狀態**——「擬動作」僅記錄若開啟 apply 將會發生的事。"
        )
        action_label = "擬動作"
        apply_line = None

    lines = [
        header,
        "",
        blurb,
        "",
        "- 引文存在僅驗證字面來源，不證明語義支持或原始來源獨立性。",
        f"- 薄證據池：{pool_size} 條主張；本輪掃描 {len(scans)} 條",
        f"- 判讀契約 v{CONTRACT_VERSION}；段落判定：full_support {counts['full_support']}"
        f" ・ partial_support {counts['partial_support']} ・ analogy {counts['analogy']}"
        f" ・ contradicts {counts['contradicts']}"
        f" ・ neutral {counts['neutral']} ・ 無法解析 {counts['unparseable']} ・ 錯誤 {counts['error']}",
    ]
    if apply_line:
        lines.append(apply_line)
    if repair_summary:
        lines.append(f"- {repair_summary}")
    for error in repair_errors or []:
        lines.append(f"- 索引追補失敗：{error}")
    lines.append("")
    for scan in scans:
        lines.append(f"## {scan.claim[:80]}")
        lines.append(f"- claim_id：`{scan.claim_id}`")
        lines.append(f"- falsifier：{scan.falsifier or '（未陳述）'}")
        lines.append(
            f"- 候選段落 {len(scan.judgments)}（排除自我來源 {scan.excluded_self}、距離過遠 {scan.excluded_far}、"
            f"缺少有效正文距離 {scan.excluded_unknown}）"
        )
        for error in scan.retrieval_errors:
            lines.append(f"- 檢索失敗：{error}")
        for j in scan.judgments:
            dist = f"（distance {j.distance:.2f}）" if j.distance is not None else ""
            lines.append(f"  - [{j.relation}] {j.title} — {j.reason}{dist}")
            if j.applicability:
                lines.append(f"    - 適用範圍：{j.applicability}")
            for quote in j.quotes:
                lines.append(f"    - 引文：{quote}")
            for missing in j.unsupported:
                lines.append(f"    - 未支持部分：{missing}")
        action = _applied_action(scan.applied) if apply else _dry_run_action(scan)
        lines.append(f"- {action_label}：{action}")
        lines.append("")
    return "\n".join(lines)


def run_evidence_traceback(
    llm,
    rag,
    *,
    cortex_dir: Path | None = None,
    out_dir: Path | None = None,
    state_file: Path | None = None,
    repair_dir: Path | None = None,
    now: datetime | None = None,
) -> TracebackResult:
    """One traceback batch. Judgment is read-only; when
    ``EVIDENCE_TRACEBACK_APPLY`` is set the accepted full_support/contradicts also
    mutate the claim page (append evidence / counterpoint, gentle reinforce).
    Always writes the dated report and the rotation cursor."""
    cortex_dir = cortex_dir or CORTEX_DIR
    out_dir = out_dir or FROM_LLM_DIR
    state_file = state_file or EVIDENCE_TRACEBACK_STATE_FILE
    now = now or datetime.now()
    apply = bool(settings.EVIDENCE_TRACEBACK_APPLY)
    repair_dir = repair_dir or CORTEX_INDEX_REPAIR_DIR

    if rag is None:
        return TracebackResult("skipped", "Evidence traceback skipped: no RAG available.")

    # Recovery is independent of thin-pool membership, including empty pools.
    repairs = repair_pending_indexes(rag, cortex_dir, repair_dir, now) if apply else None
    pages = load_all_pages(cortex_dir)
    state = _load_state(state_file)
    pool = _thin_pool(pages)
    batch = _select_thin_claims(pool, state, settings.EVIDENCE_TRACEBACK_BATCH)
    if not batch:
        if repairs and (repairs.attempted or repairs.pending):
            report_path = out_dir / f"✅EvidenceTrace-{now:%Y-%m-%d}.md"
            atomic_write_text(
                report_path,
                _render_report(
                    [],
                    len(pool),
                    now,
                    apply=apply,
                    repair_summary=repairs.summary,
                    repair_errors=repairs.errors,
                ),
            )
            return TracebackResult(
                "failed" if repairs.pending else "ok", repairs.summary, report_path=report_path
            )
        return TracebackResult("ok", "Evidence traceback: no thin-evidence claims to scan.")

    scans: list[ClaimScan] = []
    for page in batch:
        candidates = _candidate_passages(rag, page, settings.EVIDENCE_TRACEBACK_MAX_DISTANCE)
        scan = ClaimScan(
            claim_id=page.claim_id,
            claim=page.claim,
            falsifier=page.falsifier,
            excluded_self=candidates.excluded_self,
            excluded_far=candidates.excluded_far,
            excluded_unknown=candidates.excluded_unknown,
            retrieval_errors=candidates.errors,
        )
        scan.judgments = [_judge_passage(llm, page, hit) for hit in candidates.passages]
        if apply and _scan_incomplete(scan):
            scan.applied = {"deferred": True}
        elif apply:
            # One claim's apply failure is caught and recorded, never aborts the
            # batch (same discipline as a failed judgment).
            try:
                scan.applied = _apply_scan(page, scan, rag, now, repair_dir=repair_dir)
            except Exception as e:
                logging.error(f"evidence_traceback: apply crashed for {page.claim_id}: {e}")
                scan.applied = {"error": str(e)[:200]}
        scans.append(scan)
        # This cursor records attempts, not success. Rotate failed claims too:
        # daily scheduling bounds retries and prevents one poison claim starving
        # the pool. Failure stays visible in the report and task status.
        state.setdefault("checked", {})[page.claim_id] = now.isoformat(timespec="seconds")

    report_path = out_dir / f"✅EvidenceTrace-{now:%Y-%m-%d}.md"
    repair_summary = repairs.summary if repairs else ""
    pending_indexes = len(list(repair_dir.glob("*.json"))) if apply else 0
    if apply:
        repair_summary = (
            f"索引追補：完成 {repairs.repaired if repairs else 0}、待處理 {pending_indexes}"
        )
    atomic_write_text(
        report_path,
        _render_report(
            scans,
            len(pool),
            now,
            apply=apply,
            repair_summary=repair_summary,
            repair_errors=repairs.errors if repairs else None,
        ),
    )
    atomic_write_text(state_file, json.dumps(state, ensure_ascii=False, indent=2))

    judged = sum(len(s.judgments) for s in scans)
    hits = sum(
        1 for s in scans for j in s.judgments if j.relation in ("full_support", "contradicts")
    )
    mode = "apply" if apply else "dry-run"
    extra = ""
    if apply:
        ev = sum((s.applied or {}).get("added_evidence", 0) for s in scans)
        tn = sum((s.applied or {}).get("tensions", 0) for s in scans)
        errs = sum(1 for s in scans if (s.applied or {}).get("error"))
        extra = f", applied +{ev} evidence/{tn} tension" + (f"/{errs} write-fail" if errs else "")
    failed_scans = sum(1 for s in scans if _scan_incomplete(s) or (s.applied or {}).get("error"))
    summary = (
        f"Evidence traceback ({mode}): {len(scans)} claims scanned "
        f"({len(pool)} thin), {judged} passages judged, {hits} evidential hits{extra}, "
        f"判讀或套用未完成 {failed_scans} 條，待索引 {pending_indexes} 條 "
        f"→ {report_path.name}"
    )
    return TracebackResult(
        "failed" if failed_scans or pending_indexes else "ok",
        summary,
        report_path=report_path,
        scans=scans,
    )
