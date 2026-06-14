"""Echo-chamber canary — F1 defense 5.

Cortex-grounded insight (F1) risks an echo chamber: if grounded insights
just echo their priors, novelty collapses and the memory calcifies. This
compares the signal distributions of GROUNDED insights (frontmatter carries
`grounded_on`) against COLD ones (no grounding) and raises an alarm on the
calcification signature — grounded insights running systematically LOWER on
novelty. (Higher groundedness for grounded is the intended benefit; lower
novelty is the danger.)

Read-only, fail-open. Meaningful only once F1 is enabled and both groups
exist; until then it reports "not enough grounded insights to compare".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.config import FROM_LLM_DIR, MAINTENANCE_LOG_FILE, INSIGHTS_DIR
from core.parser import parse_markdown_metadata

# Need this many of EACH group before a verdict is statistically worth stating.
_MIN_PER_GROUP = 5
# Grounded mean novelty this far below cold = calcification signature.
_NOVELTY_DROP_ALARM = 0.10


@dataclass
class CanaryResult:
    status: str = "ok"          # ok | insufficient | alarm
    message: str = ""
    grounded_n: int = 0
    cold_n: int = 0
    stats: dict = field(default_factory=dict)
    report_path: Path | None = None


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def run_echo_canary(
    *,
    insights_dir: Path | None = None,
    report_dir: Path | None = None,
    log_path: Path | None = None,
) -> CanaryResult:
    insights_dir = insights_dir or INSIGHTS_DIR
    report_dir = report_dir or FROM_LLM_DIR
    log_path = log_path or MAINTENANCE_LOG_FILE

    grounded = {"novelty": [], "groundedness": []}
    cold = {"novelty": [], "groundedness": []}
    if insights_dir.exists():
        for p in insights_dir.glob("*.md"):
            try:
                meta = parse_markdown_metadata(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            signals = meta.get("signals") or {}
            if not isinstance(signals, dict):
                continue
            nov, gr = signals.get("novelty"), signals.get("groundedness")
            bucket = grounded if meta.get("grounded_on") else cold
            if isinstance(nov, (int, float)):
                bucket["novelty"].append(float(nov))
            if isinstance(gr, (int, float)):
                bucket["groundedness"].append(float(gr))

    g_n, c_n = len(grounded["novelty"]), len(cold["novelty"])
    stats = {
        "grounded_novelty": _mean(grounded["novelty"]),
        "cold_novelty": _mean(cold["novelty"]),
        "grounded_groundedness": _mean(grounded["groundedness"]),
        "cold_groundedness": _mean(cold["groundedness"]),
    }

    if g_n < _MIN_PER_GROUP or c_n < _MIN_PER_GROUP:
        result = CanaryResult(
            status="insufficient", grounded_n=g_n, cold_n=c_n, stats=stats,
            message=f"樣本不足（grounded {g_n}/{_MIN_PER_GROUP}, cold {c_n}/{_MIN_PER_GROUP}），暫不評斷。",
        )
    else:
        drop = (stats["cold_novelty"] or 0) - (stats["grounded_novelty"] or 0)
        if drop >= _NOVELTY_DROP_ALARM:
            result = CanaryResult(
                status="alarm", grounded_n=g_n, cold_n=c_n, stats=stats,
                message=(f"⚠️ 同溫層特徵：grounded 洞察的 novelty 比 cold 低 {drop:.2f}"
                         f"（{stats['grounded_novelty']:.2f} vs {stats['cold_novelty']:.2f}）。"
                         f"考慮關閉 CORTEX_GROUNDED_INSIGHT_ENABLED 或降低 grounding fraction。"),
            )
        else:
            result = CanaryResult(
                status="ok", grounded_n=g_n, cold_n=c_n, stats=stats,
                message=(f"✅ 無同溫層特徵：grounded novelty {stats['grounded_novelty']:.2f} "
                         f"vs cold {stats['cold_novelty']:.2f}（grounded groundedness "
                         f"{stats['grounded_groundedness']:.2f} vs cold {stats['cold_groundedness']:.2f}）。"),
            )

    result.report_path = _write_report(report_dir, result)
    _append_log(log_path, result)
    return result


def _write_report(report_dir: Path, r: CanaryResult) -> Path | None:
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        path = report_dir / f"[canary] echo-chamber {stamp}.md"
        s = r.stats
        def fmt(v): return "—" if v is None else f"{v:.4f}"
        lines = [
            "# 🐤 Echo-Chamber Canary（F1 防禦⑤）",
            "",
            r.message,
            "",
            "| 指標 | grounded | cold |",
            "|---|---|---|",
            f"| 樣本數 | {r.grounded_n} | {r.cold_n} |",
            f"| novelty 均值 | {fmt(s['grounded_novelty'])} | {fmt(s['cold_novelty'])} |",
            f"| groundedness 均值 | {fmt(s['grounded_groundedness'])} | {fmt(s['cold_groundedness'])} |",
            "",
            "> 期望：grounded 的 groundedness 應 ≥ cold（記憶有幫助）；novelty **不該**系統性偏低（偏低＝洞察在自我複述，同溫層形成）。",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception as e:
        logging.warning(f"echo_canary: report write failed: {e}")
        return None


def _append_log(log_path: Path, r: CanaryResult) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## [{stamp}] Echo Canary ({r.status}) | {r.message}\n")
    except Exception as e:
        logging.warning(f"echo_canary: log append failed: {e}")
