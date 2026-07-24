"""Reproducible backend concurrency benchmark for ingest LLM calls.

The benchmark never changes production concurrency. A backend is eligible for
bounded parallel distillation only when it completes the same request set with
no failures and reaches the configured speedup threshold.
"""

from __future__ import annotations

import contextvars
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Callable


@dataclass(frozen=True)
class BackendBenchmarkResult:
    samples: int
    workers: int
    sequential_seconds: float
    concurrent_seconds: float
    speedup: float
    failures: int
    threshold: float
    concurrency_eligible: bool

    def to_dict(self) -> dict:
        return asdict(self)


def benchmark_backend_concurrency(
    request: Callable[[], object],
    *,
    samples: int = 4,
    workers: int = 2,
    threshold: float = 1.5,
) -> BackendBenchmarkResult:
    """Compare sequential and bounded-thread execution of one request shape.

    This intentionally performs ``samples`` requests in each arm. Callers must
    use a cheap, representative prompt and opt in knowingly because a live
    backend will be billed for ``2 * samples`` requests.
    """
    if samples < 2:
        raise ValueError("samples must be at least 2")
    if workers < 2:
        raise ValueError("workers must be at least 2")
    if threshold <= 1:
        raise ValueError("threshold must be greater than 1")

    failures = 0
    started = time.perf_counter()
    for _ in range(samples):
        try:
            request()
        except Exception:
            failures += 1
    sequential_seconds = time.perf_counter() - started

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = []
        for _ in range(samples):
            ctx = contextvars.copy_context()
            futures.append(executor.submit(ctx.run, request))
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                failures += 1
    concurrent_seconds = time.perf_counter() - started

    speedup = sequential_seconds / concurrent_seconds if concurrent_seconds > 0 else 0.0
    eligible = failures == 0 and speedup >= threshold
    return BackendBenchmarkResult(
        samples=samples,
        workers=workers,
        sequential_seconds=round(sequential_seconds, 3),
        concurrent_seconds=round(concurrent_seconds, 3),
        speedup=round(speedup, 3),
        failures=failures,
        threshold=threshold,
        concurrency_eligible=eligible,
    )
