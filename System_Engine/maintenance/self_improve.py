"""Self-improvement proposal generator — Metacognition M3.

Turns an M2 diagnosis into a CONCRETE, REVIEWABLE revision of an actual
prompt/template file — the step where the loop first changes the system,
but always through a gated proposal, never a silent edit.

For a diagnosed problem, M3 resolves the axis to the prompt/template that
governs it, loads that file's CURRENT text, and asks the LLM to rewrite it to
address the diagnosis's candidate fixes. The result is queued in `_pending`
(see improvement_store) with the original + a diff. A human approves via
`@ling-improve approve <id>`; only then is it written.

Scope: any axis with a genuine vault-template lever — 報告品質 (worst report
type → its generating prompt) and 洞察品質 (the insight-generation prompt). The
2026-07-12 audit found the arc broke here: M2 produced sharp Cortex/insight
diagnoses every week, but M3's hard `axis == 報告品質` gate discarded them all,
so chronic observations recurred with no actionable output. Axes whose real
lever is code/config (Cortex claim-building and falsifiability prompts are
hardcoded in llm_client; retrieval is index/reranker config) are recorded with
a SPECIFIC reason and left to a human/engineer — M3 proposes only where a
prompt edit is genuinely the right lever, and never edits code.

Lean LLM via `llm.complete` (no answer_query scaffolding). Gated by
SELF_IMPROVE_ENABLED at the weekly call site; the @ling-improve command can
invoke generation on-demand regardless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.config import (
    GUIDELINES_DIR,
    IMPROVEMENTS_PENDING_DIR,
    PERSONAS_DIR,
    SELF_IMPROVE_STALE_DAYS,
    TEMPLATES_DIR,
    WIKI_VAULT_DIR,
)
from services.improvement_store import list_proposals, make_proposal, save_proposal

# report_type → the tunable asset that generates it (vault-relative path).
# Used for the 報告品質 axis, whose diagnosis is broken down per report type.
_TARGET_MAP = {
    "lens_report": "Templates/Prompts/agent_counter.md",
    "synthesis": "Templates/Operations/synthesize.md",
    "report_insight": "Templates/Prompts/agent_insight.md",
    "report_insight_full": "Templates/Prompts/agent_insight.md",
}

# Axes whose lever is a SINGLE governing prompt file (no per-report-type split).
# 洞察品質's candidate fixes (novelty-differentiation field, adversarial step)
# are exactly edits to the insight-generation prompt — the audit's clean win.
# Cortex 信念 → the falsifiability-assessment prompt (externalized 2026-07-13,
# A1): M2's applicable Cortex fix is the "reverse pressure test / reduce
# dogmatism" angle, which is an edit to how claims are scored for falsifiability.
_AXIS_DIRECT_TARGET = {
    "洞察品質": "Templates/Prompts/agent_insight.md",
    "Cortex 信念": "Templates/Prompts/cortex_falsifiability.md",
}

# Axes whose real lever is code/config, not a vault prompt. M3 cannot (and must
# not) edit those; it records a specific, honest reason so the recurring
# observation names its actual blocker instead of a generic "needs a human".
_NON_PROMPT_AXIS_REASON = {
    "檢索品質": "檢索由索引/embedding/reranker 的 config 與 code 決定,非單一 prompt 可解。",
    "LLM 健康": "LLM 健康屬 provider/連線層,非 prompt 可解。",
    "記憶衰減": "衰減校準是 config(半衰期/門檻),非 prompt 可解。",
    "語義熵": "語義熵是產出多樣性的下游指標,靠 operation 輪替與 seed 多樣性,非單一 prompt。",
}


def _resolve_target(axis: str, detail: dict) -> tuple[str | None, str]:
    """Map a diagnosed axis to the vault prompt M3 should revise. Returns
    (target_rel, "") when a prompt lever exists, else (None, specific_reason)."""
    if axis == "報告品質":
        worst = _worst_report_type(detail)
        target_rel = _TARGET_MAP.get(worst or "")
        if not target_rel:
            return None, f"報告型別 `{worst}` 無對應可改的 prompt 檔"
        return target_rel, ""
    if axis in _AXIS_DIRECT_TARGET:
        return _AXIS_DIRECT_TARGET[axis], ""
    return None, _NON_PROMPT_AXIS_REASON.get(
        axis, f"{axis} 無對應可改的 prompt 檔(需 code/config 或人工)"
    )


# Approval may only ever write inside these (improvement_store re-checks too).
ALLOWED_ASSET_DIRS = [TEMPLATES_DIR, PERSONAS_DIR, GUIDELINES_DIR]

# Structured/sectional edits instead of a full-file rewrite. A weaker local
# model derails when asked to reproduce a whole file (it echoes the
# meta-instruction); asking only for find/replace snippets sidesteps that —
# the model emits just the parts it changes, and we reconstruct the revision
# deterministically, guaranteeing everything else is preserved verbatim.
_EDIT_SYSTEM = (
    "你是 prompt/模板的修訂器,服務系統的自我改善。給你一份**現行檔全文**與一份診斷+候選改善。"
    "回傳一組**精確的 find/replace 編輯**來落實改善——只回要改的片段,不要重寫整檔。\n"
    "硬規則:\n"
    "1. 每個 `find` 必須是現行檔中**逐字、連續存在**的片段（含標點與換行）,不可改寫、不可節錄到對不上。\n"
    "2. 做**最小修改**:只改需要改的地方,不要動無關內容,不要重排全檔。\n"
    "3. `replace` 是該片段修訂後的版本,扣著改善目的;不要塞進與該片段無關的大段內容。\n"
    "4. 若判斷現行檔其實不需改,回空的 edits。\n\n"
    '回 JSON：{"edits": [{"find": "<逐字片段>", "replace": "<修訂後片段>", "why": "<一句原因>"}]}'
)


def _structured_rewrite(llm, original: str, root_cause: str, fixes_text: str):
    """Ask for find/replace edits; apply them deterministically to `original`.
    Returns (revised_text, applied_edits) or (None, []) if nothing usable.

    An edit whose `find` isn't a verbatim substring (model hallucinated it) is
    silently skipped — so a partially-wrong response still yields the edits
    that DO match, and a fully-wrong one yields nothing (no garbage proposal)."""
    try:
        parsed = llm._complete_json(
            kind="object",
            system_prompt=_EDIT_SYSTEM,
            user_msg=f"診斷根因：{root_cause}\n\n要落實的候選改善：\n{fixes_text}\n\n=== 現行檔全文 ===\n{original}",
            temperature=0.2,
            trace_context={"stage": "self_improve_edits", "metadata": {}},
        )
    except Exception as e:
        logging.warning(f"self_improve: structured rewrite failed: {e}")
        return None, []
    edits = parsed.get("edits") if isinstance(parsed, dict) else None
    if not isinstance(edits, list):
        return None, []
    revised = original
    applied = []
    for e in edits:
        if not isinstance(e, dict):
            continue
        find = str(e.get("find") or "")
        repl = str(e.get("replace") or "")
        if not find or find == repl or find not in revised:
            continue
        revised = revised.replace(find, repl, 1)  # first occurrence only
        applied.append({"find": find, "replace": repl, "why": str(e.get("why") or "").strip()})
    if not applied or revised == original:
        return None, []
    return revised, applied


@dataclass
class ImproveResult:
    status: str = "succeeded"  # succeeded | skipped
    message: str = ""
    proposals: list = field(default_factory=list)  # list[dict] saved proposals
    skipped_axes: list = field(default_factory=list)  # [(axis, reason)]
    stale_pending: list = field(default_factory=list)  # [(id, age_days)] review-stalled


def _stale_pending(pending_dir: Path, max_age_days: int) -> list[tuple[str, int]]:
    """Pending proposals older than `max_age_days`, newest-stale first. A review
    that never happens turns the human gate into a silent black hole — surfacing
    the age is the nudge. Fail-open per proposal: an unparseable `created` is
    skipped, never crashes the run."""
    now = datetime.now()
    out = []
    for p in list_proposals(pending_dir):
        try:
            age = (now - datetime.fromisoformat(p["created"])).days
        except (KeyError, TypeError, ValueError):
            continue
        if age >= max_age_days:
            out.append((p.get("id", "?"), age))
    return sorted(out, key=lambda x: -x[1])


def _retained_fraction(original: str, revised: str) -> float:
    """Fraction of the original's substantial lines (>=20 chars) still present
    verbatim in the revision. A targeted edit keeps most of them; a derail
    (model echoing the meta-prompt, rewriting from scratch) keeps ~none."""
    anchors = [ln.strip() for ln in original.splitlines() if len(ln.strip()) >= 20]
    if not anchors:
        return 1.0
    kept = sum(1 for a in anchors if a in revised)
    return kept / len(anchors)


def _looks_like_targeted_edit(original: str, revised: str) -> tuple[bool, str]:
    """Reject revisions that aren't a plausible targeted edit of the original.
    Catches the observed failure mode where the LLM ignores the file and emits
    the rewrite-instruction text instead (different content, often much longer)."""
    o, r = len(original), len(revised)
    if r < o * 0.5:
        return False, "修訂內容過短（疑似截斷）"
    if r > o * 2.5:
        return False, "修訂內容暴增 >2.5×（疑似離題/複述指令,非針對性修改）"
    frac = _retained_fraction(original, revised)
    if frac < 0.35:
        return False, f"僅保留 {frac:.0%} 原結構（疑似整篇重寫/離題,非最小修改）"
    return True, ""


def _worst_report_type(detail: dict) -> str | None:
    by_type = detail.get("by_type") or {}
    ranked = sorted(
        by_type.items(), key=lambda kv: -(kv[1]["bad"] / kv[1]["total"] if kv[1]["total"] else 0)
    )
    for t, c in ranked:
        if c.get("bad"):
            return t
    return None


def run_self_improve(
    llm,
    assessment,
    diagnosis_result,
    *,
    vault_dir: Path | None = None,
    pending_dir: Path | None = None,
) -> ImproveResult:
    """Generate revision proposals from a diagnosis. Queues them; never applies.

    `assessment` (M1) supplies the per-type breakdown; `diagnosis_result` (M2)
    supplies the root cause + candidate fixes. Matched by axis name.
    """
    vault_dir = vault_dir or WIKI_VAULT_DIR
    pending_dir = pending_dir or IMPROVEMENTS_PENDING_DIR

    axis_detail = {a.name: a.detail for a in assessment.axes}
    result = ImproveResult()

    for dx in diagnosis_result.diagnoses:
        if not dx.candidate_fixes:
            continue
        target_rel, skip_reason = _resolve_target(dx.axis, axis_detail.get(dx.axis, {}))
        if not target_rel:
            result.skipped_axes.append((dx.axis, skip_reason))
            continue
        target = vault_dir / target_rel
        if not target.exists():
            result.skipped_axes.append((dx.axis, f"目標檔不存在：{target_rel}"))
            continue

        original = target.read_text(encoding="utf-8")
        fixes = "\n".join(f"- {f}" for f in dx.candidate_fixes)

        revised, edits = _structured_rewrite(llm, original, dx.root_cause, fixes)
        if revised is None:
            result.skipped_axes.append(
                (dx.axis, "LLM 未產生可套用的編輯（find 對不上或無變更）,跳過")
            )
            continue
        # Backstop: even reconstructed-from-edits, a giant replace could balloon
        # the file. The structural check rejects non-targeted results.
        ok, why = _looks_like_targeted_edit(original, revised)
        if not ok:
            result.skipped_axes.append((dx.axis, f"{why},跳過"))
            continue

        proposal = make_proposal(
            axis=dx.axis,
            target_path=target_rel,
            rationale=dx.root_cause,
            addressed_fixes=dx.candidate_fixes,
            original_content=original,
            revised_content=revised,
            edits=edits,
        )
        save_proposal(proposal, pending_dir)
        result.proposals.append(proposal)

    result.stale_pending = _stale_pending(pending_dir, SELF_IMPROVE_STALE_DAYS)

    if result.proposals:
        result.message = f"產生 {len(result.proposals)} 份修訂提案待審：" + "、".join(
            p["id"] for p in result.proposals
        )
    else:
        result.message = "本次未產生可提案的修訂。"
        result.status = "skipped"
    if result.stale_pending:
        oldest_id, oldest_age = result.stale_pending[0]
        result.message += (
            f"　⚠️ {len(result.stale_pending)} 份提案待審逾 {SELF_IMPROVE_STALE_DAYS} 天"
            f"（最久 `{oldest_id}` {oldest_age} 天）——`@ling-improve approve/reject`。"
        )
    return result
