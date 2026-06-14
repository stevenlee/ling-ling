"""Self-improvement proposal generator — Metacognition M3.

Turns an M2 diagnosis into a CONCRETE, REVIEWABLE revision of an actual
prompt/template file — the step where the loop first changes the system,
but always through a gated proposal, never a silent edit.

For a diagnosed report-quality problem, M3 maps the worst report type to the
prompt/template that generates it, loads that file's CURRENT text, and asks
the LLM to rewrite it to address the diagnosis's candidate fixes. The result
is queued in `_pending` (see improvement_store) with the original + a diff.
A human approves via `@ling-improve approve <id>`; only then is it written.

Scope (v1): the report-quality axis, whose diagnosis→file mapping is clean
(report_type → its generating prompt). Other axes (retrieval, Cortex) imply
config/code changes that aren't a single-file prompt rewrite — M3 records that
honestly and leaves them to a human. This is deliberate: propose only where a
prompt edit is genuinely the right lever.

Lean LLM via `llm.complete` (no answer_query scaffolding). Gated by
SELF_IMPROVE_ENABLED at the weekly call site; the @ling-improve command can
invoke generation on-demand regardless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from core.config import (
    GUIDELINES_DIR,
    IMPROVEMENTS_PENDING_DIR,
    PERSONAS_DIR,
    TEMPLATES_DIR,
    WIKI_VAULT_DIR,
)
from services.improvement_store import make_proposal, save_proposal

# report_type → the tunable asset that generates it (vault-relative path).
# Only report-quality targets in v1; extend as other axes get clean levers.
_TARGET_MAP = {
    "lens_report": "Templates/Prompts/agent_counter.md",
    "synthesis": "Templates/Operations/synthesize.md",
    "report_insight": "Templates/Prompts/agent_insight.md",
    "report_insight_full": "Templates/Prompts/agent_insight.md",
}

# Approval may only ever write inside these (improvement_store re-checks too).
ALLOWED_ASSET_DIRS = [TEMPLATES_DIR, PERSONAS_DIR, GUIDELINES_DIR]

_REWRITE_SYSTEM = (
    "你是 prompt/template 的修訂者,服務系統的自我改善。給你一份**現行的 prompt/模板檔全文**,"
    "以及一份針對它的診斷與候選改善。你的任務:輸出這份檔案的**修訂全文**,把候選改善落實進去。\n"
    "硬規則:\n"
    "1. 只輸出修訂後的檔案完整內容,不要任何解說、不要 ``` 圍欄、不要前後綴。\n"
    "2. **保留原檔的結構、格式慣例、變數佔位符（如 {var}、frontmatter）**;這是要直接取代原檔的。\n"
    "3. 做**針對性的最小修改**以解決診斷,不要整篇重寫或改變其用途。\n"
    "4. 若你判斷現行檔其實沒問題、改了弊大於利,原樣輸出即可。"
)


@dataclass
class ImproveResult:
    status: str = "succeeded"   # succeeded | skipped
    message: str = ""
    proposals: list = field(default_factory=list)   # list[dict] saved proposals
    skipped_axes: list = field(default_factory=list)  # [(axis, reason)]


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
    ranked = sorted(by_type.items(),
                    key=lambda kv: -(kv[1]["bad"] / kv[1]["total"] if kv[1]["total"] else 0))
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
        if dx.axis != "報告品質":
            result.skipped_axes.append((dx.axis, "v1 僅對報告品質軸產生 prompt 提案;其餘需人工/工程處理"))
            continue
        worst = _worst_report_type(axis_detail.get(dx.axis, {}))
        target_rel = _TARGET_MAP.get(worst or "")
        if not target_rel:
            result.skipped_axes.append((dx.axis, f"報告型別 `{worst}` 無對應可改的 prompt 檔"))
            continue
        target = vault_dir / target_rel
        if not target.exists():
            result.skipped_axes.append((dx.axis, f"目標檔不存在：{target_rel}"))
            continue

        original = target.read_text(encoding="utf-8")
        fixes = "\n".join(f"- {f}" for f in dx.candidate_fixes)
        user_msg = (
            f"診斷根因：{dx.root_cause}\n\n要落實的候選改善：\n{fixes}\n\n"
            f"=== 現行檔案 `{target_rel}` 全文 ===\n{original}"
        )
        try:
            revised = llm.complete(_REWRITE_SYSTEM, user_msg, temperature=0.2,
                                   stage="self_improve_rewrite")
        except Exception as e:
            logging.warning(f"self_improve: rewrite failed for {target_rel}: {e}")
            result.skipped_axes.append((dx.axis, f"LLM 改寫失敗：{e}"))
            continue

        revised = (revised or "").strip()
        # Guardrails: must be non-trivial, different, AND a plausible targeted
        # edit. The structural check catches the observed derail where a weaker
        # model echoes the meta-instruction instead of revising the file.
        if not revised or revised == original.strip():
            result.skipped_axes.append((dx.axis, "LLM 未產生有效或不同的修訂,跳過"))
            continue
        ok, why = _looks_like_targeted_edit(original, revised)
        if not ok:
            result.skipped_axes.append((dx.axis, f"{why},跳過"))
            continue

        proposal = make_proposal(
            axis=dx.axis, target_path=target_rel, rationale=dx.root_cause,
            addressed_fixes=dx.candidate_fixes, original_content=original,
            revised_content=revised,
        )
        save_proposal(proposal, pending_dir)
        result.proposals.append(proposal)

    if result.proposals:
        result.message = f"產生 {len(result.proposals)} 份修訂提案待審：" + "、".join(
            p["id"] for p in result.proposals)
    else:
        result.message = "本次未產生可提案的修訂。"
        result.status = "skipped"
    return result
