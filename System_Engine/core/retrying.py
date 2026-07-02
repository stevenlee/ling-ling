"""Shared retry primitives (P1 of the refactor roadmap).

Two distinct failure shapes, two helpers — don't conflate them:

- retry_call: transport-level failure — the callable RAISES (HTTP 429,
  provider timeout, connection reset). Exponential backoff + jitter between
  attempts, an ``is_retryable`` gate so permanent errors surface immediately,
  and hooks for telemetry side channels (e.g. llm_client's retry_meta).

- reroll: content-level failure — the callable RETURNS an unusable value
  without raising (reasoning models intermittently emit the whole reply into
  the reasoning channel and hand back empty/unparseable text). Re-invoke until
  ``accept`` passes. No sleeping: the provider isn't overloaded, the dice were.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable


def retry_call(
    fn: Callable[[], Any],
    *,
    retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    jitter: float = 0.2,
    is_retryable: Callable[[Exception], bool] | None = None,
    delay_fn: Callable[[int, Exception], float] | None = None,
    on_attempt: Callable[[int], None] | None = None,
    on_error: Callable[[int, Exception], None] | None = None,
    log_label: str | None = None,
) -> Any:
    """Call ``fn`` up to ``retries`` times; return its first successful result.

    - ``is_retryable(exc)`` False → re-raise immediately (default: retry all).
    - Delay before attempt N+1 is ``initial_delay * backoff_factor**(N-1)``
      plus uniform jitter of up to ``jitter``× the delay, unless ``delay_fn``
      (attempt, exc) overrides the schedule (jitter still applies on top).
    - ``on_attempt(attempt)`` fires at the START of every attempt (1-based),
      ``on_error(attempt, exc)`` on every failure — both exist so callers can
      mirror progress into telemetry (e.g. trace metadata) without owning the
      loop.
    - The last exception propagates unchanged when attempts are exhausted.

    ``time.sleep`` is looked up at call time so tests can monkeypatch it.
    """
    if retries < 1:
        raise ValueError(f"retries must be >= 1, got {retries}")

    for attempt in range(1, retries + 1):
        if on_attempt is not None:
            on_attempt(attempt)
        try:
            return fn()
        except Exception as e:
            if on_error is not None:
                on_error(attempt, e)
            if (is_retryable is not None and not is_retryable(e)) or attempt >= retries:
                raise
            if delay_fn is not None:
                delay = delay_fn(attempt, e)
            else:
                delay = initial_delay * (backoff_factor ** (attempt - 1))
            delay += random.uniform(0, jitter * delay)
            if log_label:
                logging.warning(
                    f"{log_label} failed transiently (attempt {attempt}/{retries}): {e}. "
                    f"Retrying in {delay:.2f} seconds..."
                )
            time.sleep(delay)


def reroll(
    fn: Callable[[int], Any],
    accept: Callable[[Any], bool],
    *,
    attempts: int = 2,
    fallback: Any = None,
    swallow_errors: bool = False,
    on_error: Callable[[int, Exception], None] | None = None,
) -> Any:
    """Re-invoke ``fn(attempt)`` until ``accept(result)`` passes.

    Returns the first accepted result, else ``fallback`` after ``attempts``
    tries. ``fn`` receives the 1-based attempt number (for logging). With
    ``swallow_errors`` an exception counts as a rejected attempt (``on_error``
    fires); without it the exception propagates — the caller owns that path.
    """
    for attempt in range(1, attempts + 1):
        try:
            result = fn(attempt)
        except Exception as e:
            if not swallow_errors:
                raise
            if on_error is not None:
                on_error(attempt, e)
            continue
        if accept(result):
            return result
    return fallback
