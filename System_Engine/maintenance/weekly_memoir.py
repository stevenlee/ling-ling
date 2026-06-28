"""Weekly memoir report — narrative export of the system's episodic memory."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from core.config import (
    FROM_LLM_DIR,
    MAINTENANCE_LOG_FILE,
    CORTEX_DIR,
    INSIGHTS_DIR,
    BENCH_HISTORY_FILE,
)
from core.parser import parse_markdown_metadata


DEFAULT_WINDOW_DAYS = 7


@dataclass
class MemoirResult:
    status: str
    message: str
    report_path: Path | None = None


def run_weekly_memoir(
    trace_store,
    *,
    cortex_dir: Path | None = None,
    insights_dir: Path | None = None,
    bench_history: Path | None = None,
    report_dir: Path | None = None,
    log_path: Path | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> MemoirResult:
    cortex_dir = cortex_dir or CORTEX_DIR
    insights_dir = insights_dir or INSIGHTS_DIR
    bench_history = bench_history or BENCH_HISTORY_FILE
    report_dir = report_dir or FROM_LLM_DIR
    log_path = log_path or MAINTENANCE_LOG_FILE

    cutoff_time = datetime.now() - timedelta(days=window_days)
    
    # 1. 你問了什麼
    try:
        queries = trace_store.recent_query_texts(since_days=window_days)[:10]
        if queries:
            section_1 = "\n".join(f"- {q}" for q in queries)
        else:
            section_1 = None
    except Exception as e:
        logging.warning(f"Memoir: failed to load queries: {e}")
        section_1 = "（本節資料不可用）"

    # 2. 我讀了什麼
    try:
        decisions = trace_store.query_artifacts("routing_decision", since_days=window_days)
        if decisions:
            profiles = {}
            files = []
            for d in decisions:
                meta = d.get("metadata", {})
                profile = meta.get("profile", "default")
                profiles[profile] = profiles.get(profile, 0) + 1
                if "filename" in meta:
                    files.append(meta["filename"])
            
            profile_stats = ", ".join(f"`{k}`: {v}" for k, v in sorted(profiles.items(), key=lambda x: -x[1]))
            files_list = "\n".join(f"- {f}" for f in files[:15])
            if len(files) > 15:
                files_list += f"\n- ...及其他 {len(files) - 15} 篇"
            section_2 = f"Profile 分佈：{profile_stats}\n\n文件列表：\n{files_list}"
        else:
            section_2 = None
    except Exception as e:
        logging.warning(f"Memoir: failed to load routing decisions: {e}")
        section_2 = "（本節資料不可用）"

    # 3. 我想了什麼
    try:
        if insights_dir.exists():
            insight_files = []
            for p in insights_dir.glob("*.md"):
                try:
                    stat = p.stat()
                    mtime = datetime.fromtimestamp(stat.st_mtime)
                    if mtime >= cutoff_time:
                        meta = parse_markdown_metadata(p.read_text(encoding="utf-8"))
                        groundedness = meta.get("signals", {}).get("groundedness", "N/A")
                        refute = meta.get("signals", {}).get("refute_verdict", "N/A")
                        insight_files.append((p.name, groundedness, refute, mtime))
                except Exception:
                    continue
            insight_files.sort(key=lambda x: x[3], reverse=True)
            if insight_files:
                section_3 = "\n".join(f"- `{f[0]}` (groundedness: {f[1]}, refute: {f[2]})" for f in insight_files)
            else:
                section_3 = None
        else:
            section_3 = None
    except Exception as e:
        logging.warning(f"Memoir: failed to load insights: {e}")
        section_3 = "（本節資料不可用）"

    # 4. 大腦長了什麼
    try:
        from services.cortex_store import load_all_pages
        if cortex_dir.exists():
            pages = load_all_pages(cortex_dir)
            new_claims = []
            for page in pages:
                # 判斷 created / updated 是否在窗口內
                created_dt = None
                updated_dt = None
                try:
                    if page.created:
                        created_dt = datetime.fromisoformat(page.created.replace("Z", "+00:00"))
                    if page.updated:
                        updated_dt = datetime.fromisoformat(page.updated.replace("Z", "+00:00"))
                except ValueError:
                    pass
                
                in_window = False
                if updated_dt and updated_dt.replace(tzinfo=None) >= cutoff_time:
                    in_window = True
                elif created_dt and created_dt.replace(tzinfo=None) >= cutoff_time:
                    in_window = True
                
                if in_window:
                    falsified = " 【Falsified】" if page.status == "falsified" else ""
                    claim_text = page.claim.strip().replace("\n", " ")
                    new_claims.append(f"-{falsified} {claim_text}")
            
            if new_claims:
                section_4 = "\n".join(new_claims)
            else:
                section_4 = None
        else:
            section_4 = None
    except Exception as e:
        logging.warning(f"Memoir: failed to load cortex pages: {e}")
        section_4 = "（本節資料不可用）"

    # 5. 健康一行
    try:
        if bench_history.exists():
            data = json.loads(bench_history.read_text(encoding="utf-8"))
            if data and isinstance(data, list):
                last_entry = data[-1]
                pass_rate = last_entry.get("pass_rate", 0.0)
                facet_lift = last_entry.get("facet_lift", 0.0)
                section_5 = f"Pass Rate: {pass_rate:.1%}, Facet Lift: {facet_lift:+.1%}"
            else:
                section_5 = None
        else:
            section_5 = None
    except Exception as e:
        logging.warning(f"Memoir: failed to load bench history: {e}")
        section_5 = "（本節資料不可用）"

    result = MemoirResult(
        status="succeeded",
        message="Weekly memoir generated successfully."
    )

    _append_maintenance_log(log_path, result, window_days)
    result.report_path = _write_report(
        report_dir, result, window_days,
        section_1, section_2, section_3, section_4, section_5
    )
    return result


def _append_maintenance_log(log_path: Path, result: MemoirResult, window_days: int) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        line = f"\n## [{stamp}] Weekly Memoir ({window_days}d) | {result.message}\n"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        logging.warning(f"Memoir: failed to append maintenance log: {e}")


def _write_report(
    report_dir: Path,
    result: MemoirResult,
    window_days: int,
    sec_1: str,
    sec_2: str,
    sec_3: str,
    sec_4: str,
    sec_5: str,
) -> Path | None:
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        path = report_dir / f"✅sys-memoir-{stamp}.md"

        lines = [
            f"# 📖 本週記事（近 {window_days} 天）",
            "",
        ]
        
        if sec_1:
            lines.extend(["## 1. 你問了什麼", sec_1, ""])
        if sec_2:
            lines.extend(["## 2. 我讀了什麼", sec_2, ""])
        if sec_3:
            lines.extend(["## 3. 我想了什麼", sec_3, ""])
        if sec_4:
            lines.extend(["## 4. 大腦長了什麼", sec_4, ""])
        if sec_5:
            lines.extend(["## 5. 健康一行", sec_5, ""])
            
        lines.extend([
            "---",
            "*由 MaintenanceScheduler 的 weekly_memoir 任務自動產生。*",
        ])
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception as e:
        logging.warning(f"Memoir: failed to write full report: {e}")
        return None
