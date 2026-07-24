"""Cooperative request admission for one shared LLM backend."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager


class PriorityRequestGate:
    """Serialize requests while letting queued core work pass enrichment.

    The ingestion pipeline still interleaves core and artifact stages.  This
    gate only prevents two requests from contending inside a single-server
    backend, where observed overlap increased both latencies.  Bounded-lag
    backpressure eventually stops new core work, giving enrichment a turn.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active = False
        self._core_waiters = 0

    @contextmanager
    def admit(self, priority: str):
        core = priority == "core"
        started = time.perf_counter()
        with self._condition:
            if core:
                self._core_waiters += 1
            try:
                self._condition.wait_for(
                    lambda: not self._active and (core or self._core_waiters == 0)
                )
                self._active = True
            finally:
                if core:
                    self._core_waiters -= 1
        waited_ms = max(0, round((time.perf_counter() - started) * 1000))
        try:
            yield waited_ms
        finally:
            with self._condition:
                self._active = False
                self._condition.notify_all()
