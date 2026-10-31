#!/usr/bin/env python3
"""匯出 A2 合成評估輸入，或離線評分既有判讀結果；不呼叫 LLM／RAG。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from services.evidence_evaluation import evaluate_predictions, load_dataset, read_json

_DEFAULT_CASES = (
    Path(__file__).resolve().parents[1] / "tests/fixtures/evidence_evaluation/cases.json"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=_DEFAULT_CASES)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--inputs", action="store_true", help="只匯出案例輸入，不包含參考答案")
    group.add_argument("--predictions", type=Path, help="評分已保存的分類結果 JSON")
    args = parser.parse_args(argv)
    try:
        dataset = load_dataset(args.cases)
        if args.inputs:
            result = dataset.inputs()
        else:
            result = evaluate_predictions(dataset, read_json(args.predictions))
    except (OSError, ValueError) as exc:
        print(f"評估輸入錯誤：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.inputs or result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
