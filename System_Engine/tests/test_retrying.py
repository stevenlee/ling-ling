"""core/retrying.py — shared retry primitives (P1)."""

import time

import pytest

from core.retrying import retry_call, reroll


class Boom(Exception):
    pass


class Fatal(Exception):
    pass


@pytest.fixture
def sleep_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(time, "sleep", lambda d: calls.append(d))
    return calls


# ── retry_call ──────────────────────────────────────────────────────────


def test_first_try_success_no_sleep(sleep_calls):
    assert retry_call(lambda: "ok") == "ok"
    assert sleep_calls == []


def test_retries_then_succeeds(sleep_calls):
    tries = []

    def fn():
        tries.append(1)
        if len(tries) < 3:
            raise Boom("transient")
        return "ok"

    assert retry_call(fn, retries=3, jitter=0) == "ok"
    assert len(tries) == 3
    assert sleep_calls == [1.0, 2.0]  # exponential: 1.0 * 2**(n-1)


def test_exhausted_raises_last_exception(sleep_calls):
    def fn():
        raise Boom("always")

    with pytest.raises(Boom):
        retry_call(fn, retries=3, jitter=0)
    assert len(sleep_calls) == 2  # no sleep after the final attempt


def test_non_retryable_raises_immediately(sleep_calls):
    tries = []

    def fn():
        tries.append(1)
        raise Fatal("permanent")

    with pytest.raises(Fatal):
        retry_call(fn, retries=3, is_retryable=lambda e: not isinstance(e, Fatal))
    assert len(tries) == 1
    assert sleep_calls == []


def test_hooks_fire_per_attempt_and_error(sleep_calls):
    attempts, errors = [], []
    outcomes = iter([Boom("t"), Boom("t"), "ok"])

    def fn():
        o = next(outcomes)
        if isinstance(o, Exception):
            raise o
        return o

    retry_call(
        fn,
        retries=3,
        jitter=0,
        on_attempt=lambda a: attempts.append(a),
        on_error=lambda a, e: errors.append((a, str(e))),
    )
    assert attempts == [1, 2, 3]
    assert errors == [(1, "t"), (2, "t")]


def test_on_attempt_fires_even_on_single_success(sleep_calls):
    attempts = []
    retry_call(lambda: "ok", on_attempt=lambda a: attempts.append(a))
    assert attempts == [1]


def test_delay_fn_overrides_schedule(sleep_calls):
    def fn():
        raise Boom("t")

    def delay(attempt, exc):
        return 2 ** (attempt - 1) + 2  # the research_pipeline 429 schedule

    with pytest.raises(Boom):
        retry_call(fn, retries=3, jitter=0, delay_fn=delay)
    assert sleep_calls == [3, 4]


def test_jitter_bounded(sleep_calls):
    def fn():
        raise Boom("t")

    with pytest.raises(Boom):
        retry_call(fn, retries=2, initial_delay=1.0, jitter=0.2)
    (delay,) = sleep_calls
    assert 1.0 <= delay <= 1.2


def test_retries_must_be_positive():
    with pytest.raises(ValueError):
        retry_call(lambda: "ok", retries=0)


# ── reroll ──────────────────────────────────────────────────────────────


def test_reroll_accepts_first_good_result():
    assert reroll(lambda a: "good", lambda r: bool(r)) == "good"


def test_reroll_retries_until_accept():
    results = iter([None, {"total_count": 1}])
    out = reroll(lambda a: next(results), lambda r: r is not None, attempts=2)
    assert out == {"total_count": 1}


def test_reroll_returns_fallback_when_exhausted():
    out = reroll(lambda a: None, lambda r: r is not None, attempts=2, fallback={"score": None})
    assert out == {"score": None}


def test_reroll_passes_attempt_number():
    seen = []

    def fn(attempt):
        seen.append(attempt)
        return None

    reroll(fn, lambda r: False, attempts=3)
    assert seen == [1, 2, 3]


def test_reroll_propagates_errors_by_default():
    def fn(attempt):
        raise Boom("no")

    with pytest.raises(Boom):
        reroll(fn, lambda r: True)


def test_reroll_swallow_errors_counts_as_rejected():
    errors = []

    def fn(attempt):
        if attempt == 1:
            raise Boom("flaky")
        return "ok"

    out = reroll(
        fn,
        lambda r: bool(r),
        attempts=2,
        swallow_errors=True,
        on_error=lambda a, e: errors.append((a, str(e))),
    )
    assert out == "ok"
    assert errors == [(1, "flaky")]


def test_reroll_falsy_but_accepted_result_returns():
    # A literal [] from the model is a genuine zero, not a parse failure —
    # accept() decides, not truthiness.
    out = reroll(lambda a: [], lambda r: r is not None, attempts=2, fallback=None)
    assert out == []
