#!/usr/bin/env python3
"""Opt-in live benchmark; spends 2 * --samples small LLM requests."""

from __future__ import annotations

import argparse
import json

from services.ingest.backend_benchmark import benchmark_backend_concurrency
from services.llm_client import LLMClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=4)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--threshold", type=float, default=1.5)
    args = parser.parse_args()

    llm = LLMClient()

    def request() -> None:
        response = llm.complete(
            "Return exactly the word OK. No punctuation.",
            "OK",
            max_tokens=8,
            stage="benchmark_ingest_backend",
        )
        if not response.strip():
            raise RuntimeError("backend returned an empty response")

    result = benchmark_backend_concurrency(
        request,
        samples=args.samples,
        workers=args.workers,
        threshold=args.threshold,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
