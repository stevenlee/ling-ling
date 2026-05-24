"""Golden-query retrieval benchmark for maintenance runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from core.config import (
    MAINTENANCE_LOG_FILE,
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


def _evaluate_case(rag, case: dict, default_top_k: int) -> dict:
    query = str(case["query"])
    top_k = int(case.get("top_k") or default_top_k)
    results = rag.query_notes(
        query,
        top_k=top_k,
        tags=case.get("tags"),
        section_path=case.get("section_path"),
        diversity=float(case.get("diversity") or 0.0),
        rerank=case.get("rerank"),
        hybrid=case.get("hybrid"),
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
    log_path: Path = MAINTENANCE_LOG_FILE,
    min_pass_rate: float = RETRIEVAL_BENCH_MIN_PASS_RATE,
    default_top_k: int = 3,
) -> RetrievalBenchResult:
    cases = load_bench_cases(bench_path)
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
    result = RetrievalBenchResult(
        status=status,
        total=total,
        passed=passed,
        pass_rate=pass_rate,
        rows=rows,
        message=f"Retrieval bench {passed}/{total} passed ({pass_rate:.0%})",
    )
    append_bench_log(result, log_path, min_pass_rate=min_pass_rate)
    return result


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
