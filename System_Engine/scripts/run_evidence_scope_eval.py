#!/usr/bin/env python3
"""明確呼叫模型，隔離評估 A01 合成案例；不執行 maintenance 或 live apply。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.config import DATABASE_DIR, OPERATIONS_DIR
from services.evidence_evaluation import load_dataset
from services.evidence_model_evaluation import run_model_evaluation, validate_output_dir
from services.llm_client import LLMClient
from services.trace_store import TraceStore

_DEFAULT_CASES = (
    Path(__file__).resolve().parents[1] / "tests/fixtures/evidence_evaluation/cases.json"
)


def _daemon_idle(path: Path) -> bool:
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(status, dict) and status.get("busy") is False
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=_DEFAULT_CASES)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-cases", type=int, default=24)
    parser.add_argument("--max-errors", type=int, default=1)
    parser.add_argument("--daemon-status", type=Path, default=DATABASE_DIR / "daemon_status.json")
    args = parser.parse_args(argv)
    try:
        output = validate_output_dir(args.output_dir)
        dataset = load_dataset(args.cases)
        if not 1 <= args.max_cases <= len(dataset.cases) or args.max_errors < 1:
            raise ValueError("評估上限無效")
        if not _daemon_idle(args.daemon_status):
            raise ValueError("daemon 忙碌或狀態不可讀，未開始模型評估")
        output.mkdir(parents=True, exist_ok=False)
        trace = TraceStore(output / "trace.sqlite")
        llm = LLMClient(trace_store=trace)
        prompt = llm._load_capability_body(OPERATIONS_DIR / "judge_evidence_scope.md")
        with trace.run(
            intent="evaluation.evidence_scope",
            trigger_type="manual",
            metadata={"dataset_id": dataset.id, "dataset_sha256": dataset.sha256},
        ):
            try:
                result = run_model_evaluation(
                    dataset,
                    llm,
                    output,
                    prompt=prompt,
                    max_cases=args.max_cases,
                    max_errors=args.max_errors,
                    can_start=lambda: _daemon_idle(args.daemon_status),
                    progress=lambda message: print(message, flush=True),
                )
            except KeyboardInterrupt as error:
                raise RuntimeError("評估被中斷；已保存的 started row 不視為有效判讀") from error
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in (
                        "total",
                        "correct",
                        "errors",
                        "missing",
                        "unsafe_full_support",
                        "passed",
                    )
                }
            )
        )
        return 0 if result["passed"] else 1
    except (OSError, ValueError, RuntimeError) as error:
        print(f"評估未完成：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
