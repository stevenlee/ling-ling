"""Self-diagnosis — Metacognition layer M2.

Consumes the M1 self-assessment scorecard: for each red/yellow axis, gather
the concrete underlying data (which report type is failing, which Cortex
claims are dogmatic, the retrieval trend, ...) and run ONE lean LLM call to
produce a structured root-cause + candidate fixes. Trend-aware — a chronic
red (high streak) is framed differently from a fresh one.

This is ANALYSIS, not action. It produces candidate fixes; it does not apply
them. M3 turns the best candidates into gated proposals in a `_pending`
review queue. Keeping diagnosis (read-only reasoning) separate from change
(gated, human-approved) is the core anti-drift clause — a self-improving
loop must never become a self-confirming one.

Lean LLM via `llm.complete` / `llm._complete_json` (NO answer_query template
scaffolding — the recall lesson). Per-axis fail-open: one axis's LLM error
doesn't sink the others. Gated by SELF_DIAGNOSIS_ENABLED at the call site.

See DesignDoc/SelfImprovement_metacognition_plan.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.config import CORTEX_DIR, FROM_LLM_DIR, MAINTENANCE_LOG_FILE

# Axes worth spending an LLM call on. "記憶衰減" is informational (self-tuning
# already) and "LLM 健康" failures are usually infra, not a quality root cause
# we can fix in prompts — but we still diagnose them if red.
_GREEN = "🌸"

_SYSTEM = (
    "你是知識系統的診斷器,服務系統的自我改善。給你「一個健康指標軸」的數據與趨勢,"
    "你的任務:找出**最可能的根因**,並提出 2–4 個**具體、可執行**的候選改善。\n"
    "要求:根因要扣著數據講,不要空泛;候選改善要能落到 prompt/template/設定/維護任務的層級,"
    "每個都一句話講清楚「改什麼、為什麼會有幫助」。若數據不足以判斷,誠實說明還需要什麼資料。\n"
    "注意:你只做診斷與建議,**不要假設改動已被套用**;這些是給人審核的候選方案。\n\n"
    "回 JSON：{\"root_cause\": \"<一段，根因>\", "
    "\"candidate_fixes\": [\"<具體改善1>\", \"...\"], "
    "\"confidence\": <0-1>, \"needs\": \"<若資料不足，還需要什麼；否則空字串>\"}"
)


@dataclass
class Diagnosis:
    axis: str
    lamp: str
    streak: int = 1
    root_cause: str = ""
    candidate_fixes: list = field(default_factory=list)
    confidence: float = 0.0
    needs: str = ""
    context: str = ""          # the data we showed the LLM (for auditability)


@dataclass
class DiagnosisResult:
    status: str = "succeeded"  # succeeded | skipped
    message: str = ""
    diagnoses: list = field(default_factory=list)   # list[Diagnosis]
    report_path: Path | None = None


# ── per-axis context (deterministic; what the LLM reasons over) ───────────

def _ctx_report_quality(d: dict) -> str:
    rows = []
    for t, c in sorted((d.get("by_type") or {}).items(),
                       key=lambda kv: -(kv[1]["bad"] / kv[1]["total"] if kv[1]["total"] else 0)):
        rows.append(f"  - 報告型別 `{t}`：{c['total']} 份中 {c['bad']} 份被判 revise/reject")
    return (f"整體 {d.get('total', 0)} 份有評分,其中 {d.get('bad', 0)} 份 revise/reject"
            f"（{d.get('rate', 0):.0%}）。各型別:\n" + "\n".join(rows))


def _ctx_llm_health(d: dict) -> str:
    rows = [f"  - stage `{s}`：{v['total']} 次, 失敗 {v['failed']}, token {v['tokens']:,}"
            for s, v in sorted((d.get("by_stage") or {}).items(), key=lambda kv: -kv[1]["failed"])]
    return (f"近期 {d.get('total', 0)} 次 LLM 呼叫, 失敗率 {d.get('error_rate', 0):.0%}, "
            f"總 token {d.get('total_tokens', 0):,}。各 stage:\n" + "\n".join(rows))


def _ctx_retrieval(d: dict) -> str:
    prev = d.get("prev_pass_rate")
    trend = f"，前次 {prev:.0%}" if isinstance(prev, (int, float)) else ""
    lift = d.get("facet_lift")
    lift_s = f"；facet 索引帶來的 lift = {lift}" if lift is not None else ""
    return (f"檢索 bench 最新 pass_rate {d.get('pass_rate', 0):.0%}{trend}{lift_s}。"
            "（bench 衡量「查詢 → 正確文件排在前面」的命中率。低分可能來自:embedder 同語言天花板、"
            "新文件未索引、bench 案例過時、或 chunk/檢索設定。）")


def _ctx_cortex(d: dict, cortex_dir: Path) -> str:
    lines = [f"Cortex 共 {d.get('total_pages', 0)} 條主張：矛盾 {d.get('contradictions', 0)}、"
             f"教條 {d.get('dogmatic', 0)}（高信心+低可證偽）、薄證據 {d.get('thin_evidence', 0)}（≤1 來源）、"
             f"已證偽 {d.get('falsified', 0)}。"]
    try:
        from services.cortex_tensions import scan_tensions
        rep = scan_tensions(cortex_dir)
        if rep.dogmatic:
            lines.append("教條主張範例:")
            lines += [f"  - {p.claim.strip()[:90]}" for p in rep.dogmatic[:4]]
        if rep.thin_evidence:
            lines.append("薄證據主張範例:")
            lines += [f"  - {p.claim.strip()[:90]}" for p in rep.thin_evidence[:4]]
    except Exception as e:
        logging.debug(f"self_diagnosis: cortex context scan failed: {e}")
    return "\n".join(lines)


def _ctx_insight(d: dict) -> str:
    mn = d.get("mean_novelty")
    nov = f"平均 novelty {mn:.2f}" if isinstance(mn, (int, float)) else "novelty 未知"
    return (f"近期 {d.get('n', 0)} 篇洞察,{nov},"
            f"refute 被推翻 {d.get('refuted', 0)}/{d.get('refute_checked', 0)},"
            f"grounded {d.get('grounded_n', 0)} / cold {d.get('cold_n', 0)}。")


def _gather_context(axis_name: str, detail: dict, cortex_dir: Path) -> str:
    if axis_name == "報告品質":
        return _ctx_report_quality(detail)
    if axis_name == "LLM 健康":
        return _ctx_llm_health(detail)
    if axis_name == "檢索品質":
        return _ctx_retrieval(detail)
    if axis_name == "Cortex 信念":
        return _ctx_cortex(detail, cortex_dir)
    if axis_name == "洞察品質":
        return _ctx_insight(detail)
    return ""


def run_self_diagnosis(
    llm,
    assessment,
    *,
    cortex_dir: Path | None = None,
    report_dir: Path | None = None,
    log_path: Path | None = None,
) -> DiagnosisResult:
    """Diagnose every red/yellow axis of an M1 SelfAssessmentResult.

    `assessment` is a SelfAssessmentResult (axes carry .detail, trend carries
    streak). Returns DiagnosisResult; writes a report when any diagnosis lands.
    """
    cortex_dir = cortex_dir or CORTEX_DIR
    report_dir = report_dir or FROM_LLM_DIR
    log_path = log_path or MAINTENANCE_LOG_FILE

    flagged = [a for a in assessment.axes if a.lamp not in (_GREEN, "🌱")]
    if not flagged:
        return DiagnosisResult(status="skipped", message="所有軸健康,無需診斷。")

    diagnoses: list[Diagnosis] = []
    for ax in flagged:
        ctx = _gather_context(ax.name, ax.detail, cortex_dir)
        streak = (assessment.trend.get(ax.name) or {}).get("streak", 1)
        chronic = "（這是慢性問題,已持續多次）" if streak >= 3 else "（近期出現）"
        dx = Diagnosis(axis=ax.name, lamp=ax.lamp, streak=streak, context=ctx)
        try:
            parsed = llm._complete_json(
                kind="object",
                system_prompt=_SYSTEM,
                user_msg=f"健康軸：{ax.name}（狀態 {ax.lamp}）{chronic}\n摘要：{ax.summary}\n\n數據：\n{ctx}",
                temperature=0.2,
                trace_context={"stage": "self_diagnosis", "metadata": {"axis": ax.name}},
            )
            if isinstance(parsed, dict):
                dx.root_cause = str(parsed.get("root_cause") or "").strip()
                fixes = parsed.get("candidate_fixes")
                dx.candidate_fixes = [str(x).strip() for x in fixes if str(x).strip()] if isinstance(fixes, list) else []
                dx.confidence = float(parsed.get("confidence") or 0.0)
                dx.needs = str(parsed.get("needs") or "").strip()
        except Exception as e:
            logging.warning(f"self_diagnosis: axis {ax.name} diagnosis failed: {e}")
            dx.root_cause = ""
        diagnoses.append(dx)

    landed = [d for d in diagnoses if d.root_cause or d.candidate_fixes]
    result = DiagnosisResult(
        status="succeeded",
        message=f"診斷 {len(landed)}/{len(flagged)} 軸：" + "、".join(d.axis for d in landed),
        diagnoses=diagnoses,
    )
    _append_maintenance_log(log_path, result)
    if landed:
        result.report_path = _write_report(report_dir, result)
    return result


def _append_maintenance_log(log_path: Path, result: DiagnosisResult) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## [{stamp}] Self-Diagnosis | {result.message}\n")
    except Exception as e:
        logging.warning(f"self_diagnosis: failed to append maintenance log: {e}")


def _write_report(report_dir: Path, result: DiagnosisResult) -> Path | None:
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        path = report_dir / f"[report] 系統診斷 {stamp}.md"
        lines = [
            "# 🔬 系統診斷（M2）",
            "",
            "> 這是**診斷與候選改善**,不是已套用的變更。候選方案需人工審核;"
            "實際修改會走 M3 的 `_pending` 審核佇列。",
            "",
        ]
        for d in result.diagnoses:
            if not (d.root_cause or d.candidate_fixes):
                continue
            chronic = f"（連續 {d.streak} 次）" if d.streak >= 3 else ""
            lines += [
                f"## {d.lamp} {d.axis}{chronic}",
                "",
                f"**根因**（信心 {d.confidence:.0%}）：{d.root_cause or '—'}",
                "",
            ]
            if d.candidate_fixes:
                lines.append("**候選改善（尚未套用）**：")
                lines += [f"{i}. {fx}" for i, fx in enumerate(d.candidate_fixes, 1)]
                lines.append("")
            if d.needs:
                lines += [f"> ⓘ 資料不足：{d.needs}", ""]
            lines += ["<details><summary>診斷所依據的數據</summary>", "", "```", d.context, "```", "</details>", ""]
        lines += [
            "---",
            "*由 MaintenanceScheduler 的 self_assessment 任務在 SELF_DIAGNOSIS_ENABLED 開啟時產生。"
            "M2 只診斷;改動走 M3 人工閘。*",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception as e:
        logging.warning(f"self_diagnosis: failed to write report: {e}")
        return None
