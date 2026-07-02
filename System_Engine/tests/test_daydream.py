"""Daydream pump: daytime-only, low-priority idle work that yields to user
work, walks the consolidate→makeup→spontaneous ladder, and is hard-capped by a
per-day budget. Behavioural knobs are Scripture-driven (settings.DAYDREAM_*),
so tests drive them by overriding settings, the same way the dreaming window
is set."""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock


import pytest

import maintenance.daydream as dd_mod
from maintenance.daydream import DaydreamPump

DAY = datetime(2026, 6, 18, 10, 0, 0)  # 10am — outside the 1–5am window
NIGHT = datetime(2026, 6, 18, 3, 0, 0)  # 3am — inside the window


@pytest.fixture
def env(tmp_path, monkeypatch):
    to_llm = tmp_path / "toLingLing"
    consolidate = tmp_path / "Consolidate"
    for d in (to_llm, consolidate):
        d.mkdir()
    monkeypatch.setattr(dd_mod, "TO_LLM_DIR", to_llm)
    monkeypatch.setattr(dd_mod, "CONSOLIDATE_DIR", consolidate)
    monkeypatch.setattr(dd_mod, "MAINTENANCE_LOG_FILE", tmp_path / "maintenance.log.md")
    # Scripture-driven settings (the unit under config control):
    monkeypatch.setattr(dd_mod.settings, "DREAMING_FROM", 1)
    monkeypatch.setattr(dd_mod.settings, "DREAMING_TO", 5)
    monkeypatch.setattr(dd_mod.settings, "DAYDREAM_ENABLED", True)
    monkeypatch.setattr(dd_mod.settings, "DAYDREAM_SPONTANEOUS_ENABLED", True)
    monkeypatch.setattr(dd_mod.settings, "DAYDREAM_CONSOLIDATION_BUDGET", 10)
    monkeypatch.setattr(dd_mod.settings, "DAYDREAM_BITE_ADJUDICATIONS", 4)
    monkeypatch.setattr(dd_mod.settings, "DAYDREAM_INSIGHT_BUDGET", 1)
    monkeypatch.setattr(dd_mod.settings, "DAYDREAM_SPONTANEOUS_BUDGET", 1)
    busy = MagicMock()
    busy.is_busy.return_value = False
    busy.try_set_busy.return_value = True
    monkeypatch.setattr(dd_mod, "global_busy_state", busy)
    return tmp_path, to_llm, consolidate, busy, monkeypatch


def _pump(tmp_path, *, clock=DAY, ran_today=False, **kw):
    # Scheduler state file: did insight_daily run today?
    mstate = tmp_path / "maintenance_state.json"
    if ran_today is not None:
        last = clock.isoformat() if ran_today else "2026-06-01T03:00:00"
        mstate.write_text(json.dumps({"insight_daily": {"last_run_at": last}}), encoding="utf-8")
    defaults = dict(
        state_file=tmp_path / "daydream_state.json",
        maintenance_state_file=mstate,
        clock=lambda: clock,
    )
    defaults.update(kw)
    pump = DaydreamPump(MagicMock(), MagicMock(), **defaults)
    pump._kicks = []
    pump.kick = lambda delay=None, replace=True: pump._kicks.append(delay)
    return pump


def _fake_consolidation(monkeypatch, processed=1, status="succeeded"):
    calls = []

    def f(llm, rag, *, max_insights=None, max_adjudications=None):
        calls.append((max_insights, max_adjudications))
        return SimpleNamespace(status=status, message="m", insights_processed=processed)

    monkeypatch.setattr(dd_mod, "run_consolidation", f)
    return calls


def _fake_insight(monkeypatch, status="succeeded"):
    calls = []

    def f(llm, rag, *, occasion="Scheduled"):
        calls.append(occasion)
        return SimpleNamespace(status=status, summary="s")

    monkeypatch.setattr(dd_mod, "run_daily_insight", f)
    return calls


class TestDaytimeGate:
    def test_in_window_does_no_work(self, env):
        tmp_path, _, _, busy, monkeypatch = env
        monkeypatch.setattr(dd_mod, "has_pending_insights", lambda *a, **k: True)
        _fake_consolidation(monkeypatch)
        pump = _pump(tmp_path, clock=NIGHT)
        pump._run_step()
        busy.try_set_busy.assert_not_called()
        assert pump._kicks and pump._kicks[0] > 0  # rescheduled for after the window


class TestLadder:
    def test_consolidation_backlog_first(self, env):
        tmp_path, _, _, busy, monkeypatch = env
        monkeypatch.setattr(dd_mod, "has_pending_insights", lambda *a, **k: True)
        cons = _fake_consolidation(monkeypatch, processed=1)
        ins = _fake_insight(monkeypatch)
        pump = _pump(tmp_path)
        pump._run_step()
        assert cons == [(1, 4)]  # max_insights=1, bite cap=4
        assert ins == []  # didn't fall through to insight
        assert pump._ledger["budget"]["consolidation"] == 1
        busy.set_busy.assert_called_with(False)  # lock released
        assert pump._kicks == [pump.step_gap_seconds]  # more backlog → next bite

    def test_makeup_insight_when_no_backlog_and_not_run_today(self, env):
        tmp_path, _, _, _, monkeypatch = env
        monkeypatch.setattr(dd_mod, "has_pending_insights", lambda *a, **k: False)
        _fake_consolidation(monkeypatch)
        ins = _fake_insight(monkeypatch)
        pump = _pump(tmp_path, ran_today=False)
        pump._run_step()
        assert ins == ["Daydream makeup"]
        assert pump._ledger["budget"]["insight"] == 1

    def test_spontaneous_when_nothing_owed(self, env):
        tmp_path, _, _, _, monkeypatch = env
        monkeypatch.setattr(dd_mod, "has_pending_insights", lambda *a, **k: False)
        _fake_consolidation(monkeypatch)
        ins = _fake_insight(monkeypatch)
        pump = _pump(tmp_path, ran_today=True)  # insight already ran today
        pump._run_step()
        assert ins == ["Daydream spontaneous"]
        assert pump._ledger["budget"]["spontaneous"] == 1

    def test_spontaneous_disabled_goes_silent(self, env):
        tmp_path, _, _, busy, monkeypatch = env
        monkeypatch.setattr(dd_mod.settings, "DAYDREAM_SPONTANEOUS_ENABLED", False)
        monkeypatch.setattr(dd_mod, "has_pending_insights", lambda *a, **k: False)
        _fake_consolidation(monkeypatch)
        ins = _fake_insight(monkeypatch)
        pump = _pump(tmp_path, ran_today=True)
        pump._run_step()
        assert ins == []
        busy.try_set_busy.assert_not_called()
        assert pump._ledger["completed_logged"] is True


class TestBudgetBound:
    def test_consolidation_budget_caps_daily_bites(self, env):
        tmp_path, _, _, _, monkeypatch = env
        monkeypatch.setattr(dd_mod, "has_pending_insights", lambda *a, **k: True)
        cons = _fake_consolidation(monkeypatch, processed=1)
        _fake_insight(monkeypatch)
        # Only consolidation is eligible; once its budget is spent, silence.
        monkeypatch.setattr(dd_mod.settings, "DAYDREAM_CONSOLIDATION_BUDGET", 2)
        monkeypatch.setattr(dd_mod.settings, "DAYDREAM_INSIGHT_BUDGET", 0)
        monkeypatch.setattr(dd_mod.settings, "DAYDREAM_SPONTANEOUS_ENABLED", False)
        pump = _pump(tmp_path)
        for _ in range(4):
            pump._run_step()
        assert len(cons) == 2  # capped at the daily budget
        assert pump._ledger["budget"]["consolidation"] == 2


class TestYielding:
    def test_busy_means_no_work(self, env):
        tmp_path, _, _, busy, monkeypatch = env
        busy.is_busy.return_value = True
        monkeypatch.setattr(dd_mod, "has_pending_insights", lambda *a, **k: True)
        _fake_consolidation(monkeypatch)
        pump = _pump(tmp_path)
        pump._run_step()
        busy.try_set_busy.assert_not_called()

    def test_fresh_inbox_file_yields(self, env):
        tmp_path, to_llm, _, busy, monkeypatch = env
        (to_llm / "@ling-do-it.md").write_text("x", encoding="utf-8")
        monkeypatch.setattr(dd_mod, "has_pending_insights", lambda *a, **k: True)
        _fake_consolidation(monkeypatch)
        pump = _pump(tmp_path)
        pump._run_step()
        busy.try_set_busy.assert_not_called()

    def test_disabled_does_nothing(self, env):
        tmp_path, _, _, busy, monkeypatch = env
        monkeypatch.setattr(dd_mod.settings, "DAYDREAM_ENABLED", False)
        monkeypatch.setattr(dd_mod, "has_pending_insights", lambda *a, **k: True)
        _fake_consolidation(monkeypatch)
        pump = _pump(tmp_path)
        pump._run_step()
        busy.try_set_busy.assert_not_called()


class TestFailureBackoff:
    def test_repeated_failures_back_off(self, env):
        tmp_path, _, _, _, monkeypatch = env
        monkeypatch.setattr(dd_mod, "has_pending_insights", lambda *a, **k: True)

        def boom(*a, **k):
            raise RuntimeError("provider down")

        monkeypatch.setattr(dd_mod, "run_consolidation", boom)
        pump = _pump(tmp_path)
        import time

        for _ in range(3):
            pump._run_step()
        assert pump._backoff_until > time.time()
