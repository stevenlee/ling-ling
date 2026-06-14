"""Routing health report — the self-improving loop over profile routing.

Aggregates `routing_decision` artifacts (written by
IngestionPipeline._record_routing_decision) and `select_profile` llm_calls
into a periodic health check:

- fallback rate (documents that landed on default/settings instead of a
  specialized profile),
- registered profiles that were never selected in the window,
- pending drafts in `_pending/` still waiting for review.

The one-line summary always lands in maintenance.log.md; a full report is
written to `fromLingLing/` only when something is actionable, so quiet
weeks stay quiet.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.config import (
    FROM_LLM_DIR,
    MAINTENANCE_LOG_FILE,
    PROFILES_DIR,
    PROFILES_PENDING_DIR,
)
from services.profile_manager import ProfileManager


DEFAULT_WINDOW_DAYS = 7
DEFAULT_FALLBACK_ALERT_RATE = 0.3


@dataclass
class RoutingReportResult:
    status: str                     # "succeeded" | "skipped"
    message: str
    total: int = 0
    fallback_rate: float = 0.0
    layer_counts: dict = field(default_factory=dict)
    profile_counts: dict = field(default_factory=dict)
    unused_profiles: list[str] = field(default_factory=list)
    pending_drafts: list[str] = field(default_factory=list)
    report_path: Path | None = None


def run_routing_report(
    trace_store,
    *,
    profiles_dir: Path = None,
    pending_dir: Path = None,
    report_dir: Path = None,
    log_path: Path = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    fallback_alert_rate: float = DEFAULT_FALLBACK_ALERT_RATE,
) -> RoutingReportResult:
    profiles_dir = profiles_dir or PROFILES_DIR
    pending_dir = pending_dir or PROFILES_PENDING_DIR
    report_dir = report_dir or FROM_LLM_DIR
    log_path = log_path or MAINTENANCE_LOG_FILE

    decisions = trace_store.query_artifacts("routing_decision", since_days=window_days)
    pending_drafts = sorted(
        p.name for p in pending_dir.iterdir() if p.is_dir()
    ) if pending_dir.exists() else []

    if not decisions and not pending_drafts:
        return RoutingReportResult(
            status="skipped",
            message=f"No routing decisions in the last {window_days} days.",
        )

    layer_counts = Counter()
    profile_counts = Counter()
    fallbacks = 0
    for d in decisions:
        meta = d.get("metadata") or {}
        layer_counts[meta.get("layer") or "unknown"] += 1
        if meta.get("profile"):
            profile_counts[meta["profile"]] += 1
        if meta.get("fellback_to_default"):
            fallbacks += 1

    total = len(decisions)
    fallback_rate = fallbacks / total if total else 0.0

    pm = ProfileManager(profiles_dir, pending_dir=pending_dir)
    registered = {s.name for s in pm.all()}
    # `default` is the catch-all; not being selected by name is healthy.
    unused = sorted(registered - set(profile_counts) - {"default"})

    selector_errors = sum(
        1 for c in trace_store.query_llm_calls("select_profile", since_days=window_days)
        if c.get("status") == "failed"
    )

    actionable = bool(pending_drafts) or (
        total > 0 and fallback_rate >= fallback_alert_rate
    ) or selector_errors > 0

    result = RoutingReportResult(
        status="succeeded",
        message=(
            f"Routing: {total} decisions, fallback {fallback_rate:.0%}, "
            f"{len(pending_drafts)} pending draft(s), {len(unused)} unused profile(s)."
        ),
        total=total,
        fallback_rate=fallback_rate,
        layer_counts=dict(layer_counts),
        profile_counts=dict(profile_counts),
        unused_profiles=unused,
        pending_drafts=pending_drafts,
    )

    _append_maintenance_log(log_path, result, window_days)
    if actionable:
        result.report_path = _write_full_report(
            report_dir, result, window_days, fallback_alert_rate, selector_errors
        )
    return result


def _append_maintenance_log(log_path: Path, result: RoutingReportResult, window_days: int) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        line = f"\n## [{stamp}] Routing Report ({window_days}d) | {result.message}\n"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        logging.warning(f"Routing report: failed to append maintenance log: {e}")


def _write_full_report(
    report_dir: Path,
    result: RoutingReportResult,
    window_days: int,
    alert_rate: float,
    selector_errors: int,
) -> Path | None:
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        path = report_dir / f"[report] routing health {stamp}.md"

        lines = [
            f"# 🧭 路由健康報告（近 {window_days} 天）",
            "",
            f"- 路由決策總數：**{result.total}**",
            f"- Fallback 率（落到 default/settings）：**{result.fallback_rate:.0%}**"
            + (" 💦 超過警戒線" if result.total and result.fallback_rate >= alert_rate else ""),
        ]
        if selector_errors:
            lines.append(f"- select_profile 失敗次數：**{selector_errors}** 💦")
        lines += ["", "## 各層分佈", ""]
        for layer, count in sorted(result.layer_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- `{layer}`: {count}")
        if result.profile_counts:
            lines += ["", "## Profile 使用次數", ""]
            for name, count in sorted(result.profile_counts.items(), key=lambda x: -x[1]):
                lines.append(f"- `{name}`: {count}")
        if result.pending_drafts:
            lines += [
                "", "## ⏳ 待審核草稿", "",
                *[f"- `{name}` — 用 `@ling-profiles approve {name}` 一鍵生效，"
                  f"或到 `Scripture/Profiles/_pending/{name}/` 審閱" for name in result.pending_drafts],
            ]
        if result.unused_profiles:
            lines += [
                "", "## 💤 本期未被選用的 Profile", "",
                *[f"- `{name}` — 若 `applicable_when` 描述不夠精準可調整，或考慮合併" for name in result.unused_profiles],
            ]
        lines += [
            "",
            "---",
            "*由 MaintenanceScheduler 的 routing_report 任務自動產生。"
            "Fallback 率高代表現有 profile 涵蓋不足或選擇 hint 不夠準。*",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception as e:
        logging.warning(f"Routing report: failed to write full report: {e}")
        return None
