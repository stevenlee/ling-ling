"""core/state.py — BusyState state machine, isolated (P4 coverage gap)."""

import threading

from core.state import BusyState


def _state(tmp_path):
    s = BusyState()
    s.lock_file = tmp_path / ".kb_lock"  # keep the real repo lock out of tests
    return s


def test_busy_flag_roundtrip(tmp_path):
    s = _state(tmp_path)
    assert not s.is_busy()
    s.set_busy(True)
    assert s.is_busy()
    s.set_busy(False)
    assert not s.is_busy()


def test_lock_file_forces_busy(tmp_path):
    s = _state(tmp_path)
    s.lock_file.write_text("locked", encoding="utf-8")
    assert s.is_busy()
    assert s.try_set_busy() is False


def test_try_set_busy_is_atomic(tmp_path):
    s = _state(tmp_path)
    assert s.try_set_busy() is True
    assert s.try_set_busy() is False  # second acquisition fails
    s.set_busy(False, fire_callbacks=False)
    assert s.try_set_busy() is True


def test_idle_transition_fires_callbacks(tmp_path):
    s = _state(tmp_path)
    fired = []
    s.register_idle_callback(lambda: fired.append(1) or 0)
    s.set_busy(True)
    s.set_busy(False)
    assert fired == [1]


def test_callbacks_do_not_fire_without_transition(tmp_path):
    s = _state(tmp_path)
    fired = []
    s.register_idle_callback(lambda: fired.append(1) or 0)
    s.set_busy(False)  # idle → idle: no transition
    assert fired == []
    s.set_busy(True)
    s.set_busy(False, fire_callbacks=False)  # explicit opt-out
    assert fired == []


def test_drain_loop_reruns_while_callbacks_report_work(tmp_path):
    s = _state(tmp_path)
    remaining = {"n": 3}

    def worker():
        if remaining["n"] > 0:
            remaining["n"] -= 1
            return 1  # "I processed one item" → drain again
        return 0

    s.register_idle_callback(worker)
    s.set_busy(True)
    s.set_busy(False)
    assert remaining["n"] == 0


def test_callback_exception_does_not_stop_others(tmp_path):
    s = _state(tmp_path)
    fired = []

    def boom():
        raise RuntimeError("callback died")

    s.register_idle_callback(boom)
    s.register_idle_callback(lambda: fired.append(1) or 0)
    s.set_busy(True)
    s.set_busy(False)
    assert fired == [1]
    assert not s.is_busy()  # busy released even after a callback error


def test_no_reentrant_firing(tmp_path):
    # A callback that itself flips busy→idle must NOT trigger a nested drain.
    s = _state(tmp_path)
    calls = []

    def reentrant():
        calls.append("enter")
        s.set_busy(True)
        s.set_busy(False)  # nested transition while _firing_callbacks is held
        calls.append("exit")
        return 0

    s.register_idle_callback(reentrant)
    s.set_busy(True)
    s.set_busy(False)
    assert calls == ["enter", "exit"]  # ran once, not recursively


def test_system_stays_busy_while_draining(tmp_path):
    s = _state(tmp_path)
    seen = []
    s.register_idle_callback(lambda: seen.append(s.is_busy()) or 0)
    s.set_busy(True)
    s.set_busy(False)
    assert seen == [True]  # callbacks run under the busy flag
    assert not s.is_busy()  # released afterwards


def test_thread_safety_of_try_set_busy(tmp_path):
    s = _state(tmp_path)
    wins = []
    barrier = threading.Barrier(8)

    def contender():
        barrier.wait()
        if s.try_set_busy():
            wins.append(1)

    threads = [threading.Thread(target=contender) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1  # exactly one winner
