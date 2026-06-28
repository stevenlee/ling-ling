"""Self-assessment — the unified evaluator (Metacognition layer, M1).

The system already emits plenty of quality signals (report verdicts, LLM
health, retrieval bench, Cortex tensions, decay calibration, insight
signals) and runs six narrow self-tuning loops over them — but they're
scattered across six subsystems and there's no single "is the system
healthy right now?" view. M1 aggregates all of them into one weekly
scorecard + deterministic observations.

STRICTLY READ-ONLY, ZERO LLM CALLS. This is the sensing layer of the
self-improvement arc: per the project's Nervous-System-First principle,
auto-EVALUATE must land before auto-IMPROVE. The observations here are the
seed for M2 (diagnosis) but M1 takes no action — it only reports.

Fail-open per axis: if one signal source is broken/missing, its axis
degrades to "unknown" and the rest of the report still renders. Quiet
weeks stay quiet — the full report is only written when an axis is
yellow/red. Mirrors the routing_report task shape.

See DesignDoc/SelfImprovement_metacognition_plan.md.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.config import (
    BENCH_HISTORY_FILE,
    CORTEX_DECAY_STATE_FILE,
    CORTEX_DIR,
    CORTEX_LEDGER_STATE_FILE,
    FROM_LLM_DIR,
    INSIGHTS_DIR,
    MAINTENANCE_LOG_FILE,
    RETRIEVAL_BENCH_MIN_PASS_RATE,
    SELF_ASSESSMENT_HISTORY_FILE,
    SELF_ASSESSMENT_HISTORY_MAX,
)
from core.parser import parse_markdown_metadata

DEFAULT_WINDOW_DAYS = 7

# Status lamp values, ordered worst→best for roll-up.
RED, YELLOW, GREEN, UNKNOWN = "🥀", "🌼", "🌸", "🌱"
_RANK = {RED: 0, YELLOW: 1, GREEN: 2, UNKNOWN: 3}

# Thresholds (deterministic lamp rules).
_VERDICT_REVISE_YELLOW = 0.25   # >=25% revise+reject across reports → yellow
_VERDICT_REVISE_RED = 0.50      # >=50% → red
_LLM_ERROR_YELLOW = 0.05        # >=5% failed LLM calls → yellow
_LLM_ERROR_RED = 0.15           # >=15% → red
_REFUTE_REFUTED_YELLOW = 0.30   # >=30% of refute-checked insights refuted → yellow


@dataclass
class Axis:
    name: str
    lamp: str = UNKNOWN
    summary: str = ""
    detail: dict = field(default_factory=dict)


@dataclass
class SelfAssessmentResult:
    status: str = "succeeded"          # succeeded | skipped
    message: str = ""
    overall: str = UNKNOWN             # worst lamp across axes
    axes: list = field(default_factory=list)        # list[Axis]
    observations: list = field(default_factory=list)  # list[str] — M2 seeds
    trend: dict = field(default_factory=dict)       # axis name → {arrow, prev, streak}
    report_path: Path | None = None


def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logging.debug(f"self_assessment: failed reading {path.name}: {e}")
    return default


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


# Compact, trend-worthy metric per axis (for the history snapshot).
def _axis_metric(ax: "Axis") -> dict:
    d = ax.detail or {}
    keep = ("rate", "error_rate", "pass_rate", "dogmatic", "thin_evidence",
            "contradictions", "falsified", "mean_novelty", "grounded_n", "cold_n")
    return {k: d[k] for k in keep if k in d}


def _persist_and_trend(history_file: Path, axes: list, overall: str, max_keep: int) -> dict:
    """Append this run's snapshot to the trend log and compute per-axis trend
    vs the PREVIOUS run. Fail-open: a broken/missing log just yields 'new'
    everywhere and never blocks the assessment.

    Returns {axis_name: {"arrow": ↑/↓/→/•, "prev": lamp|None, "streak": int}}
    where streak = consecutive runs (incl. this one) at the current lamp.
    """
    hist = _read_json(history_file, [])
    if not isinstance(hist, list):
        hist = []
    prev = hist[-1] if hist else None
    prev_axes = (prev or {}).get("axes", {}) if isinstance(prev, dict) else {}

    trend = {}
    for ax in axes:
        prev_lamp = (prev_axes.get(ax.name) or {}).get("lamp")
        # `prev_lamp not in _RANK` also covers legacy lamp glyphs persisted by
        # older builds (the lamp palette changed) — treat them as no comparison
        # rather than crashing on a stale history entry.
        if prev_lamp not in _RANK or prev_lamp == UNKNOWN or ax.lamp == UNKNOWN:
            arrow = "•"
        elif _RANK[ax.lamp] > _RANK[prev_lamp]:
            arrow = "↑"
        elif _RANK[ax.lamp] < _RANK[prev_lamp]:
            arrow = "↓"
        else:
            arrow = "→"
        # streak: walk history backwards counting same lamp, +1 for this run.
        streak = 1
        for snap in reversed(hist):
            if not isinstance(snap, dict):
                break
            if (snap.get("axes", {}).get(ax.name) or {}).get("lamp") == ax.lamp:
                streak += 1
            else:
                break
        trend[ax.name] = {"arrow": arrow, "prev": prev_lamp, "streak": streak}

    snapshot = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "overall": overall,
        "axes": {ax.name: {"lamp": ax.lamp, **_axis_metric(ax)} for ax in axes},
    }
    hist.append(snapshot)
    if len(hist) > max_keep:
        hist = hist[-max_keep:]
    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = history_file.with_suffix(history_file.suffix + ".tmp")
        tmp.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(history_file)
    except Exception as e:
        logging.warning(f"self_assessment: failed to persist trend history: {e}")
    return trend


# ── per-axis evaluators (each fail-open → returns an Axis) ────────────────

def _axis_report_quality(trace_store, window_days: int, obs: list) -> Axis:
    ax = Axis("報告品質")
    try:
        arts = trace_store.query_all_artifacts(since_days=window_days)
    except Exception as e:
        ax.summary = f"無法讀取 artifacts：{e}"
        return ax
    verdicts = [(a.get("artifact_type") or "?", a.get("quality_verdict"))
                for a in arts if a.get("quality_verdict")]
    if not verdicts:
        ax.lamp = GREEN
        ax.summary = f"近 {window_days} 天 {len(arts)} 份產物，無 verdict 紀錄（多數報告型別未評分）。"
        return ax
    total = len(verdicts)
    bad = sum(1 for _, v in verdicts if v in ("revise", "reject"))
    rate = bad / total
    # Per-type breakdown for the observation seed (which template/op is worst).
    by_type = defaultdict(lambda: [0, 0])   # type → [bad, total]
    for t, v in verdicts:
        by_type[t][1] += 1
        if v in ("revise", "reject"):
            by_type[t][0] += 1
    ax.detail = {"total": total, "bad": bad, "rate": rate,
                 "by_type": {t: {"bad": b, "total": n} for t, (b, n) in by_type.items()}}
    ax.lamp = RED if rate >= _VERDICT_REVISE_RED else YELLOW if rate >= _VERDICT_REVISE_YELLOW else GREEN
    ax.summary = f"{total} 份有評分；revise/reject {bad}（{rate:.0%}）。"
    if ax.lamp != GREEN:
        worst = sorted(by_type.items(), key=lambda kv: -(kv[1][0] / kv[1][1] if kv[1][1] else 0))
        for t, (b, n) in worst:
            if b and n and b / n >= _VERDICT_REVISE_YELLOW:
                obs.append(f"報告型別 `{t}`：{n} 份中 {b} 份被判 revise/reject — 檢視其 prompt/template。")
    return ax


def _axis_llm_health(trace_store, window_days: int, obs: list) -> Axis:
    ax = Axis("LLM 健康")
    try:
        health = trace_store.llm_call_health(since_days=window_days)
    except Exception as e:
        ax.summary = f"無法讀取 llm_calls：{e}"
        return ax
    total, err_rate = health.get("total", 0), health.get("error_rate", 0.0)
    ax.detail = health
    if total == 0:
        ax.lamp = GREEN
        ax.summary = f"近 {window_days} 天無 LLM 呼叫紀錄。"
        return ax
    ax.lamp = RED if err_rate >= _LLM_ERROR_RED else YELLOW if err_rate >= _LLM_ERROR_YELLOW else GREEN
    ax.summary = (f"{total} 次呼叫，失敗率 {err_rate:.0%}，"
                  f"token {health.get('total_tokens', 0):,}。")
    if ax.lamp != GREEN:
        worst = sorted(health.get("by_stage", {}).items(),
                       key=lambda kv: -(kv[1]["failed"]))
        for stage, s in worst[:3]:
            if s["failed"]:
                obs.append(f"stage `{stage}`：{s['total']} 次中 {s['failed']} 次失敗 — 檢視該 stage 的呼叫。")
    return ax


def _axis_retrieval(bench_history_file: Path, obs: list) -> Axis:
    ax = Axis("檢索品質")
    hist = _read_json(bench_history_file, [])
    if not isinstance(hist, list) or not hist:
        ax.summary = "尚無 bench 歷史。"
        return ax
    latest = hist[-1]
    prev = hist[-2] if len(hist) >= 2 else None
    rate = latest.get("pass_rate", 0.0)
    ax.detail = {"pass_rate": rate, "facet_lift": latest.get("facet_lift"),
                 "prev_pass_rate": prev.get("pass_rate") if prev else None}
    regressed = prev is not None and rate < prev.get("pass_rate", 0.0)
    below_floor = rate < RETRIEVAL_BENCH_MIN_PASS_RATE
    ax.lamp = RED if below_floor else YELLOW if regressed else GREEN
    trend = ""
    if prev:
        d = rate - prev.get("pass_rate", 0.0)
        trend = f"（前次 {prev.get('pass_rate', 0.0):.0%}，{'＋' if d >= 0 else ''}{d:.0%}）"
    ax.summary = f"最新 pass_rate {rate:.0%}{trend}。"
    if below_floor:
        obs.append(f"檢索 pass_rate {rate:.0%} 低於警戒線 {RETRIEVAL_BENCH_MIN_PASS_RATE:.0%} — 檢視回歸案例。")
    elif regressed:
        obs.append(f"檢索 pass_rate 較前次下降（{prev.get('pass_rate', 0.0):.0%}→{rate:.0%}）。")
    return ax


def _axis_cortex(cortex_dir: Path, ledger_file: Path, obs: list) -> Axis:
    ax = Axis("Cortex 信念")
    try:
        from services.cortex_tensions import scan_tensions
        report = scan_tensions(cortex_dir)
    except Exception as e:
        ax.summary = f"無法掃描 tensions：{e}"
        return ax
    led = _read_json(ledger_file, {})
    strict = bool(led.get("adjudication_strict")) if isinstance(led, dict) else False
    n_contra = len(report.contradictions)
    n_dog = len(report.dogmatic)
    n_thin = len(report.thin_evidence)
    n_fals = len(report.falsified)
    ax.detail = {"total_pages": report.total_pages, "contradictions": n_contra,
                 "dogmatic": n_dog, "thin_evidence": n_thin, "falsified": n_fals,
                 "adjudication_strict": strict}
    # Dogmatic (confident + unfalsifiable) is the echo-chamber fuel → weigh it.
    ax.lamp = RED if n_dog else YELLOW if (n_contra or n_thin) else GREEN
    ax.summary = (f"{report.total_pages} 條主張；矛盾 {n_contra}、教條 {n_dog}、"
                  f"薄證據 {n_thin}、已證偽 {n_fals}。")
    if n_dog:
        obs.append(f"Cortex 有 {n_dog} 條教條主張（高信心+低可證偽）— 同溫層燃料，考慮針對性證偽。")
    if n_thin:
        obs.append(f"Cortex 有 {n_thin} 條薄證據主張（≤1 來源）— 可在 seed 選擇時補強。")
    return ax


def _axis_decay(decay_file: Path, obs: list) -> Axis:
    ax = Axis("記憶衰減")
    st = _read_json(decay_file, {})
    if not isinstance(st, dict):
        ax.summary = "衰減狀態檔格式異常。"
        return ax
    params = st.get("params") or {}
    base_days = params.get("base_days")
    last_cal = st.get("last_calibration") or ""
    transitions = st.get("transitions") or []
    ax.detail = {"base_days": base_days, "last_calibration": last_cal,
                 "transitions": len(transitions)}
    ax.lamp = GREEN  # informational axis — no failure mode of its own
    bd = f"{base_days:.0f}" if isinstance(base_days, (int, float)) else "預設"
    ax.summary = (f"base_days={bd}，transition {len(transitions)} 次，"
                  f"上次校準 {last_cal or '尚未'}。")
    return ax


def _axis_insight_quality(insights_dir: Path, obs: list) -> Axis:
    ax = Axis("洞察品質")
    nov, gr = [], []
    refute = Counter()
    grounded_n = cold_n = 0
    if insights_dir.exists():
        for p in insights_dir.glob("*.md"):
            try:
                meta = parse_markdown_metadata(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            sig = meta.get("signals") or {}
            if not isinstance(sig, dict):
                continue
            if isinstance(sig.get("novelty"), (int, float)):
                nov.append(float(sig["novelty"]))
            if isinstance(sig.get("groundedness"), (int, float)):
                gr.append(float(sig["groundedness"]))
            rv = sig.get("refute_verdict")
            if rv:
                refute[rv] += 1
            if meta.get("grounded_on"):
                grounded_n += 1
            else:
                cold_n += 1
    n = len(nov)
    if n == 0:
        ax.summary = "尚無帶訊號的洞察。"
        return ax
    refuted = refute.get("refuted", 0)
    checked = refuted + refute.get("survived", 0)
    refuted_rate = refuted / checked if checked else 0.0
    ax.detail = {"n": n, "mean_novelty": _mean(nov), "mean_groundedness": _mean(gr),
                 "refuted": refuted, "refute_checked": checked,
                 "grounded_n": grounded_n, "cold_n": cold_n}
    ax.lamp = YELLOW if refuted_rate >= _REFUTE_REFUTED_YELLOW else GREEN
    mn = _mean(nov)
    ax.summary = (f"{n} 篇；平均 novelty {mn:.2f}" if mn is not None else f"{n} 篇")
    if checked:
        ax.summary += f"，refute 存活 {checked - refuted}/{checked}"
    ax.summary += f"（grounded {grounded_n} / cold {cold_n}）。"
    if refuted_rate >= _REFUTE_REFUTED_YELLOW:
        obs.append(f"洞察 refute 被推翻率 {refuted_rate:.0%}（{refuted}/{checked}）— 生成端可能過度臆測。")
    return ax


def run_self_assessment(
    trace_store,
    *,
    cortex_dir: Path | None = None,
    insights_dir: Path | None = None,
    bench_history_file: Path | None = None,
    decay_file: Path | None = None,
    ledger_file: Path | None = None,
    report_dir: Path | None = None,
    log_path: Path | None = None,
    history_file: Path | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> SelfAssessmentResult:
    cortex_dir = cortex_dir or CORTEX_DIR
    insights_dir = insights_dir or INSIGHTS_DIR
    bench_history_file = bench_history_file or BENCH_HISTORY_FILE
    decay_file = decay_file or CORTEX_DECAY_STATE_FILE
    ledger_file = ledger_file or CORTEX_LEDGER_STATE_FILE
    report_dir = report_dir or FROM_LLM_DIR
    log_path = log_path or MAINTENANCE_LOG_FILE
    history_file = history_file or SELF_ASSESSMENT_HISTORY_FILE

    obs: list[str] = []
    axes = [
        _axis_report_quality(trace_store, window_days, obs),
        _axis_llm_health(trace_store, window_days, obs),
        _axis_retrieval(bench_history_file, obs),
        _axis_cortex(cortex_dir, ledger_file, obs),
        _axis_decay(decay_file, obs),
        _axis_insight_quality(insights_dir, obs),
    ]
    # Overall = worst lamp among axes that actually evaluated (ignore unknown).
    rated = [a.lamp for a in axes if a.lamp != UNKNOWN]
    overall = min(rated, key=lambda l: _RANK[l]) if rated else UNKNOWN

    trend = _persist_and_trend(history_file, axes, overall, SELF_ASSESSMENT_HISTORY_MAX)
    # Chronic problems (red/yellow for ≥3 consecutive runs) deserve their own
    # callout — a fresh red is noise, a standing red is a real backlog item.
    for ax in axes:
        t = trend.get(ax.name, {})
        if ax.lamp in (RED, YELLOW) and t.get("streak", 1) >= 3:
            obs.append(f"`{ax.name}` 已連續 {t['streak']} 次為 {ax.lamp} — 慢性問題,優先處理。")

    result = SelfAssessmentResult(
        status="succeeded",
        message=f"自評 {overall}：" + "／".join(f"{a.name}{a.lamp}{trend.get(a.name, {}).get('arrow', '')}" for a in axes),
        overall=overall,
        axes=axes,
        observations=obs,
        trend=trend,
    )

    _append_maintenance_log(log_path, result, window_days)
    # Quiet weeks stay quiet: only write the full report when something needs a look.
    if overall in (RED, YELLOW) or obs:
        result.report_path = _write_report(report_dir, result, window_days)
    return result


def _append_maintenance_log(log_path: Path, result: SelfAssessmentResult, window_days: int) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        line = f"\n## [{stamp}] Self-Assessment ({window_days}d) | {result.message}\n"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        logging.warning(f"self_assessment: failed to append maintenance log: {e}")


def _write_report(report_dir: Path, result: SelfAssessmentResult, window_days: int) -> Path | None:
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        path = report_dir / f"✅sys-eval-{stamp}.md"
        lines = [
            f"# 🌿 系統自評（近 {window_days} 天）",
            "",
            f"**總體：{result.overall}**",
            "",
            "## 計分卡",
            "",
            "| 軸 | 狀態 | 趨勢 | 摘要 |",
            "|---|:---:|:---:|---|",
        ]
        for a in result.axes:
            arrow = result.trend.get(a.name, {}).get("arrow", "")
            lines.append(f"| {a.name} | {a.lamp} | {arrow} | {a.summary} |")
        if result.observations:
            lines += ["", "## 🔍 觀察（可改善之處，尚未採取行動）", ""]
            lines += [f"- {o}" for o in result.observations]
        lines += [
            "",
            "---",
            "*由 MaintenanceScheduler 的 self_assessment 任務自動產生（純讀、零 LLM）。*",
            "*這是自我改善弧線的感覺層（M1）；觀察條目是 M2 診斷的種子，本報告不採取任何行動。*",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception as e:
        logging.warning(f"self_assessment: failed to write report: {e}")
        return None
