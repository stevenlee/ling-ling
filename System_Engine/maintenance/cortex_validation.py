"""Cortex Phase 1+2 validation report — the agreed measuring stick.

Three tiers (per the validation framework agreed 2026-06-11):

1. Pipeline health (machine-checkable RED LINES): page parseability,
   claim_id uniqueness, index consistency (chunks + facets per page,
   no orphan facets), quota compliance, bench regression alerts.
2. Consolidation quality (YELLOW targets): claim yield per insight,
   signals distributions (groundedness, refute survival), with the
   human-review list of new claims / merges / contradictions — the
   user's quality vote is deleting bad pages (survival rate).
3. Retrieval effect: facet lift from bench history, cortex hits in
   retrieval_events.

Output: one report in fromLingLing/, RED verdict on top when any hard
line is crossed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.config import (
    BENCH_HISTORY_FILE,
    CORTEX_DIR,
    CORTEX_MAX_ADJUDICATIONS_PER_NIGHT,
    CORTEX_STATE_FILE,
    FROM_LLM_DIR,
    INSIGHTS_DIR,
)
from core.parser import parse_markdown_metadata
from maintenance.cortex_consolidation import _is_candidate
from services.cortex_store import load_all_pages, parse_cortex_page


@dataclass
class ValidationReport:
    verdict: str                 # "GREEN" | "YELLOW" | "RED"
    red_flags: list[str] = field(default_factory=list)
    yellow_flags: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    report_path: Path | None = None


def run_validation(
    rag,
    *,
    cortex_dir: Path = None,
    insights_dir: Path = None,
    state_file: Path = None,
    bench_history: Path = None,
    report_dir: Path = None,
    last_result=None,            # optional ConsolidationResult of the round
) -> ValidationReport:
    cortex_dir = cortex_dir or CORTEX_DIR
    insights_dir = insights_dir or INSIGHTS_DIR
    state_file = state_file or CORTEX_STATE_FILE
    bench_history = bench_history or BENCH_HISTORY_FILE
    report_dir = report_dir or FROM_LLM_DIR

    red: list[str] = []
    yellow: list[str] = []
    stats: dict = {}

    # ── Tier 1: pipeline health (red lines) ──────────────────────────
    all_files = [p for p in sorted(cortex_dir.glob("*.md")) if not p.stem.startswith("_")] \
        if cortex_dir.exists() else []
    pages = []
    for path in all_files:
        page = parse_cortex_page(path)
        if page is None:
            red.append(f"頁面無法解析：{path.name}")
        else:
            pages.append(page)
    stats["pages_total"] = len(pages)
    status_counts: dict[str, int] = {}
    for page in pages:
        status_counts[page.status] = status_counts.get(page.status, 0) + 1
    stats["status_counts"] = status_counts

    seen_ids: dict[str, str] = {}
    for page in pages:
        if page.claim_id in seen_ids:
            red.append(f"claim_id 重複：{page.claim_id}（{seen_ids[page.claim_id]} vs {page.path.name}）")
        seen_ids[page.claim_id] = page.path.name

    # Index consistency: every page has chunks + a facet; no orphan facets.
    try:
        facet_titles = {e.get("title") for e in rag.get_facet_entries()}
        missing_facets = [p.claim_id for p in pages if p.claim_id not in facet_titles]
        if missing_facets:
            red.append(f"{len(missing_facets)} 頁缺 facet：{missing_facets[:5]}")
        page_ids = {p.claim_id for p in pages}
        orphan_facets = [
            t for t in facet_titles
            if isinstance(t, str) and t.startswith("cortex-") and t not in page_ids
        ]
        if orphan_facets:
            red.append(f"{len(orphan_facets)} 個懸空 cortex facet：{orphan_facets[:5]}")
        stats["facets_ok"] = len(pages) - len(missing_facets)
    except Exception as e:
        yellow.append(f"索引一致性檢查不可用：{e}")

    if last_result is not None:
        stats["last_round"] = {
            "insights": last_result.insights_processed,
            "created": last_result.created,
            "merged": last_result.merged,
            "contradictions": last_result.contradiction_links,
            "adjudications": last_result.adjudications_used,
        }
        if last_result.adjudications_used > CORTEX_MAX_ADJUDICATIONS_PER_NIGHT:
            red.append(
                f"裁決配額越界：{last_result.adjudications_used} > {CORTEX_MAX_ADJUDICATIONS_PER_NIGHT}"
            )

    # Bench regression (red) + facet lift (tier 3).
    try:
        if bench_history.exists():
            history = json.loads(bench_history.read_text(encoding="utf-8"))
            if len(history) >= 2 and history[-1]["pass_rate"] < history[-2]["pass_rate"]:
                red.append(
                    f"Bench 退步：{history[-2]['pass_rate']:.0%} → {history[-1]['pass_rate']:.0%}"
                )
            if history:
                stats["bench_pass_rate"] = history[-1].get("pass_rate")
                stats["facet_lift"] = history[-1].get("facet_lift")
                lift = history[-1].get("facet_lift")
                if lift is not None and lift < 0:
                    yellow.append(f"Facet lift 為負：{lift}")
    except Exception as e:
        yellow.append(f"Bench history 不可讀：{e}")

    # ── Tier 2: consolidation quality (yellow targets) ───────────────
    state = {}
    try:
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        pass
    processed = state.get("processed", {})
    claim_counts = [int(v.get("claims", 0)) for v in processed.values() if isinstance(v, dict)]
    if claim_counts:
        yield_rate = sum(claim_counts) / len(claim_counts)
        stats["claim_yield"] = round(yield_rate, 2)
        stats["insights_processed_total"] = len(claim_counts)
        stats["claims_total"] = sum(claim_counts)
        if not (0.5 <= yield_rate <= 3.0):
            yellow.append(f"Claim 產率 {yield_rate:.2f} 超出目標帶 0.5–3.0")
        # Survival proxy: pages now vs claims that created pages. Merges
        # don't create pages, so survival uses created-claims when known.
        stats["survival_pages"] = len(pages)

    grounded, refute_survived, refute_total, insights_with_signals = [], 0, 0, 0
    if insights_dir.exists():
        for path in insights_dir.glob("*.md"):
            try:
                meta = parse_markdown_metadata(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            signals = meta.get("signals")
            if not isinstance(signals, dict):
                continue
            
            insights_with_signals += 1
            g_val = None
            g_raw = signals.get("groundedness")
            if g_raw is not None:
                try:
                    g_val = float(g_raw)
                except (TypeError, ValueError):
                    pass

            verdict = signals.get("refute_verdict")
            if verdict in ("survived", "refuted"):
                refute_total += 1
                refute_survived += verdict == "survived"

            # Broken-link rate is scoped to GATE-PASSING insights only —
            # planner docs the gate quarantines are supposed to have dead
            # links; counting them manufactured a fake 90% yellow flag.
            # Single source of truth: the consolidation gate itself.
            if _is_candidate(meta) and g_val is not None:
                grounded.append(g_val)
    if grounded:
        broken_rate = sum(1 for g in grounded if g < 0.8) / len(grounded)
        stats["groundedness_mean"] = round(sum(grounded) / len(grounded), 3)
        stats["broken_link_insight_rate"] = round(broken_rate, 3)
        if broken_rate > 0.2:
            yellow.append(f"斷鏈 insight 比例 {broken_rate:.0%} > 20%")
            
    if insights_with_signals > 0:
        stats["refute_coverage"] = round(refute_total / insights_with_signals, 3)
        
    if refute_total:
        survival = refute_survived / refute_total
        stats["refute_survival_rate"] = round(survival, 3)
        if survival > 0.95:
            yellow.append(f"Refute 存活率 {survival:.0%}——反駁者可能太鬆")
        elif survival < 0.3:
            yellow.append(f"Refute 存活率 {survival:.0%}——insight 生成品質堪憂")
            
    # Falsifiability distribution
    falsifiability_scores = [p.falsifiability for p in pages if p.falsifiability is not None]
    if falsifiability_scores:
        f_mean = sum(falsifiability_scores) / len(falsifiability_scores)
        f_low = sum(1 for f in falsifiability_scores if f < 0.3) / len(falsifiability_scores)
        stats["falsifiability_mean"] = round(f_mean, 3)
        stats["falsifiability_lt_0.3_rate"] = round(f_low, 3)
        if f_mean < 0.4:
            yellow.append(f"Falsifiability mean {f_mean:.3f} < 0.4 —— 主張普遍難以反駁（太模糊）")

    # ── Tier 3: cortex retrieval hits ────────────────────────────────
    try:
        trace_store = getattr(rag, "trace_store", None)
        if trace_store is not None and hasattr(trace_store, "recently_retrieved_titles"):
            hits = {
                t for t in trace_store.recently_retrieved_titles(7)
                if isinstance(t, str) and t.startswith("cortex-")
            }
            stats["cortex_retrieval_hits_7d"] = len(hits)
    except Exception:
        pass

    verdict = "RED" if red else ("YELLOW" if yellow else "GREEN")
    report = ValidationReport(verdict=verdict, red_flags=red, yellow_flags=yellow, stats=stats)
    report.report_path = _write_report(report_dir, report, pages)
    return report


def _write_report(report_dir: Path, report: ValidationReport, pages) -> Path | None:
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = report_dir / f"[report] cortex validation {stamp}.md"
        icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}[report.verdict]
        lines = [f"# {icon} Cortex 驗證報告 — {report.verdict}", ""]
        if report.red_flags:
            lines += ["## 🚨 紅線（停下修理）", ""]
            lines += [f"- {f}" for f in report.red_flags] + [""]
        if report.yellow_flags:
            lines += ["## ⚠️ 黃線（調參，不停跑）", ""]
            lines += [f"- {f}" for f in report.yellow_flags] + [""]
        lines += ["## 📊 指標", ""]
        for key, value in report.stats.items():
            lines.append(f"- {key}: `{value}`")
        # Contradiction pairs first — the most valuable, most easily
        # buried signal in a knowledge base (Phase 4 surfacing).
        by_id = {p.claim_id: p for p in pages}
        seen_pairs = set()
        contradiction_lines = []
        for page in pages:
            for other_id in page.contradictions:
                pair = tuple(sorted((page.claim_id, other_id)))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                other = by_id.get(other_id)
                other_text = other.claim if other else f"（已刪除：{other_id}）"
                contradiction_lines.append(f"- ⚔️ 「{page.claim}」 vs 「{other_text}」")
        if contradiction_lines:
            lines += ["## ⚔️ 矛盾對（知識庫裡最珍貴的訊號）", ""]
            lines += contradiction_lines + [""]
        falsified = [p for p in pages if p.status == "falsified"]
        if falsified:
            lines += ["## 🪦 已 falsified（檔案保留，記錄曾相信過什麼）", ""]
            lines += [f"- {p.claim}" for p in falsified] + [""]

        lines += [
            "",
            "## 🔍 人工抽查清單（爛的直接刪頁＝品質投票）",
            "",
        ]
        recent = sorted(pages, key=lambda p: p.updated, reverse=True)[:30]
        for page in recent:
            marks = []
            if len(page.evidence) > 1:
                marks.append(f"合併×{len(page.evidence) - 1}")
            if page.contradictions:
                marks.append(f"矛盾×{len(page.contradictions)}")
            suffix = f"（{'、'.join(marks)}）" if marks else ""
            lines.append(f"- [[{page.path.stem}]] — {page.claim}{suffix}")
            if page.falsifier:
                lines.append(f"    - 證偽：{page.falsifier}")
            f_score = "未測" if page.falsifiability is None else f"{page.falsifiability}"
            lines.append(
                f"    - 📊 falsifiability: {f_score} ｜ confidence: {page.confidence}"
                f" ｜ S: {page.S} ｜ {page.status}"
            )
            for v in page.variants:
                lines.append(f"    - 變體：{v}")
        lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception as e:
        logging.warning(f"Validation report write failed: {e}")
        return None
