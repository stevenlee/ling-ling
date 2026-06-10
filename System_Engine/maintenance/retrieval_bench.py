"""Golden-query retrieval benchmark for maintenance runs.

Self-improvement loop: cases come from the hand-written bench file PLUS
the auto-grown file (maintenance/bench_builder.py). Each run optionally
A/B-tests the facet index (on vs off) to quantify facet lift, appends a
record to the bench history, and raises a fromLingLing alert when the
pass rate drops against the previous run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from core.config import (
    MAINTENANCE_LOG_FILE,
    RETRIEVAL_BENCH_AUTO_FILE,
    RETRIEVAL_BENCH_FILE,
    RETRIEVAL_BENCH_MIN_PASS_RATE,
)


@dataclass
class RetrievalBenchResult:
    status: str
    total: int
    passed: int
    pass_rate: float
    rows: list[dict]
    message: str
    facet_off_passed: int | None = None   # A/B baseline (facets disabled)
    facet_lift: int | None = None         # passed - facet_off_passed
    regression: bool = False
    alert_path: Path | None = None


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def load_bench_cases(path: Path = RETRIEVAL_BENCH_FILE) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if isinstance(data, dict):
        data = data.get("queries", [])
    if not isinstance(data, list):
        raise ValueError(f"retrieval bench must be a list or {{queries: [...]}}: {path}")
    return [case for case in data if isinstance(case, dict) and case.get("query")]


def _candidate_identifiers(item: dict) -> set[str]:
    meta = item.get("metadata") or {}
    values = {
        item.get("id"),
        meta.get("title"),
        meta.get("source"),
        meta.get("source_path"),
        Path(str(meta.get("source", ""))).stem if meta.get("source") else None,
    }
    return {str(v) for v in values if v not in (None, "")}


def _matches_expected(item: dict, expected: Any) -> bool:
    expected_values = {str(v) for v in _as_list(expected) if v not in (None, "")}
    if not expected_values:
        return False
    identifiers = _candidate_identifiers(item)
    return bool(identifiers & expected_values)


def _evaluate_case(rag, case: dict, default_top_k: int, use_facets: bool | None = None) -> dict:
    query = str(case["query"])
    top_k = int(case.get("top_k") or default_top_k)
    kwargs = {}
    if use_facets is not None:
        kwargs["use_facets"] = use_facets
    results = rag.query_notes(
        query,
        top_k=top_k,
        tags=case.get("tags"),
        section_path=case.get("section_path"),
        diversity=float(case.get("diversity") or 0.0),
        rerank=case.get("rerank"),
        hybrid=case.get("hybrid"),
        **kwargs,
    )

    expected_top_1 = case.get("expected_top_1")
    expected_top_k = case.get("expected_top_k")
    if expected_top_1 is not None:
        passed = bool(results) and _matches_expected(results[0], expected_top_1)
        expectation = f"top-1 in {_as_list(expected_top_1)}"
    elif expected_top_k is not None:
        passed = any(_matches_expected(item, expected_top_k) for item in results)
        expectation = f"top-{top_k} contains {_as_list(expected_top_k)}"
    else:
        passed = bool(results)
        expectation = "any result"

    returned = []
    for item in results:
        meta = item.get("metadata") or {}
        returned.append({
            "id": item.get("id"),
            "title": meta.get("title"),
            "source": meta.get("source"),
        })

    return {
        "query": query,
        "top_k": top_k,
        "passed": passed,
        "expectation": expectation,
        "returned": returned,
    }


def run_retrieval_bench(
    rag,
    *,
    bench_path: Path = RETRIEVAL_BENCH_FILE,
    auto_path: Path | None = RETRIEVAL_BENCH_AUTO_FILE,
    log_path: Path = MAINTENANCE_LOG_FILE,
    min_pass_rate: float = RETRIEVAL_BENCH_MIN_PASS_RATE,
    default_top_k: int = 3,
    ab_facets: bool = False,
    history_path: Path | None = None,
    report_dir: Path | None = None,
) -> RetrievalBenchResult:
    cases = load_bench_cases(bench_path)
    if auto_path is not None:
        cases = cases + load_bench_cases(auto_path)
    if not cases:
        result = RetrievalBenchResult(
            status="skipped",
            total=0,
            passed=0,
            pass_rate=1.0,
            rows=[],
            message=f"No retrieval bench cases found at {bench_path}",
        )
        append_bench_log(result, log_path)
        return result

    rows = [_evaluate_case(rag, case, default_top_k) for case in cases]
    passed = sum(1 for row in rows if row["passed"])
    total = len(rows)
    pass_rate = passed / total if total else 1.0
    status = "passed" if pass_rate >= min_pass_rate else "failed"

    facet_off_passed = None
    facet_lift = None
    if ab_facets:
        rows_off = [
            _evaluate_case(rag, case, default_top_k, use_facets=False) for case in cases
        ]
        facet_off_passed = sum(1 for row in rows_off if row["passed"])
        facet_lift = passed - facet_off_passed

    message = f"Retrieval bench {passed}/{total} passed ({pass_rate:.0%})"
    if facet_lift is not None:
        message += f"; facet lift {facet_lift:+d} (off: {facet_off_passed}/{total})"

    result = RetrievalBenchResult(
        status=status,
        total=total,
        passed=passed,
        pass_rate=pass_rate,
        rows=rows,
        message=message,
        facet_off_passed=facet_off_passed,
        facet_lift=facet_lift,
    )

    if history_path is not None:
        previous = _append_history(history_path, result)
        if previous is not None and pass_rate < float(previous.get("pass_rate", 0.0)):
            result.regression = True
            result.status = "failed" if status == "failed" else "regressed"
            result.message += (
                f"; REGRESSION vs previous run ({previous.get('pass_rate', 0):.0%} → {pass_rate:.0%})"
            )
            if report_dir is not None:
                result.alert_path = _write_regression_alert(report_dir, result, previous)

    append_bench_log(result, log_path, min_pass_rate=min_pass_rate)
    return result


def _append_history(history_path: Path, result: RetrievalBenchResult) -> dict | None:
    """Append this run to the history file (atomic); return the previous record."""
    history: list[dict] = []
    try:
        if history_path.exists():
            data = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                history = data
    except Exception as e:
        logging.warning(f"Bench history unreadable, starting fresh: {e}")

    previous = history[-1] if history else None
    history.append({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "total": result.total,
        "passed": result.passed,
        "pass_rate": result.pass_rate,
        "facet_off_passed": result.facet_off_passed,
        "facet_lift": result.facet_lift,
    })
    history = history[-365:]  # one year of daily runs is plenty

    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = history_path.with_name(history_path.name + ".tmp")
        tmp.write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(history_path)
    except Exception as e:
        logging.warning(f"Bench history write failed: {e}")
    return previous


def _write_regression_alert(
    report_dir: Path, result: RetrievalBenchResult, previous: dict
) -> Path | None:
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        path = report_dir / f"[alert] retrieval regression {stamp}.md"
        failed_rows = [row for row in result.rows if not row["passed"]]
        lines = [
            "# 🚨 檢索品質退步告警",
            "",
            f"- 本次：**{result.passed}/{result.total}**（{result.pass_rate:.0%}）",
            f"- 上次：{previous.get('passed')}/{previous.get('total')}"
            f"（{float(previous.get('pass_rate', 0)):.0%}，{previous.get('ts', '?')}）",
        ]
        if result.facet_lift is not None:
            lines.append(
                f"- Facet lift：{result.facet_lift:+d}"
                + ("" if result.facet_lift >= 0 else " ⚠️ facet 是負貢獻，考慮 `FACET_INDEX_ENABLED=false`")
            )
        lines += ["", "## 失敗的查詢", ""]
        for row in failed_rows[:20]:
            top = (row["returned"][0].get("title") if row["returned"] else None) or "(無結果)"
            lines.append(f"- `{row['query']}` — 預期 {row['expectation']}，實得 `{top}`")
        lines += [
            "",
            "---",
            "*最近一次相關變更（chunking、embedding、reranker、facet）是頭號嫌疑人。"
            "歷史趨勢見 `Database/bench_history.json`。*",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception as e:
        logging.warning(f"Regression alert write failed: {e}")
        return None


def append_bench_log(
    result: RetrievalBenchResult,
    log_path: Path = MAINTENANCE_LOG_FILE,
    *,
    min_pass_rate: float = RETRIEVAL_BENCH_MIN_PASS_RATE,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"\n## Retrieval Bench - {now}",
        "",
        f"- Status: {result.status}",
        f"- Passed: {result.passed}/{result.total}",
        f"- Pass rate: {result.pass_rate:.2%}",
        f"- Threshold: {min_pass_rate:.2%}",
        f"- Summary: {result.message}",
        "",
    ]
    if result.rows:
        lines.extend([
            "| Result | Query | Expectation | Returned top result |",
            "|---|---|---|---|",
        ])
        for row in result.rows:
            returned = row["returned"][0] if row["returned"] else {}
            top = returned.get("title") or returned.get("source") or returned.get("id") or "(none)"
            mark = "PASS" if row["passed"] else "FAIL"
            lines.append(f"| {mark} | `{row['query']}` | {row['expectation']} | {top} |")
        lines.append("")
    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
