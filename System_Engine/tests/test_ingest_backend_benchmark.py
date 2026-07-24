import threading
import time

import pytest

from services.ingest.backend_benchmark import benchmark_backend_concurrency


def test_parallel_backend_clears_speedup_gate():
    def request():
        time.sleep(0.03)

    result = benchmark_backend_concurrency(request, samples=4, workers=2, threshold=1.5)

    assert result.failures == 0
    assert result.speedup >= 1.5
    assert result.concurrency_eligible is True


def test_failures_block_concurrency_even_when_fast():
    lock = threading.Lock()
    calls = 0

    def request():
        nonlocal calls
        with lock:
            calls += 1
            current = calls
        if current == 1:
            raise RuntimeError("provider failure")
        time.sleep(0.005)

    result = benchmark_backend_concurrency(request, samples=4, workers=2, threshold=1.01)

    assert result.failures == 1
    assert result.concurrency_eligible is False


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"samples": 1}, "samples"),
        ({"workers": 1}, "workers"),
        ({"threshold": 1.0}, "threshold"),
    ],
)
def test_invalid_benchmark_parameters(kwargs, message):
    with pytest.raises(ValueError, match=message):
        benchmark_backend_concurrency(lambda: None, **kwargs)
