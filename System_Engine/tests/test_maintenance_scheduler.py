import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from watchers.maintenance_scheduler import (
    MaintenanceResult,
    MaintenanceScheduler,
    MaintenanceTask,
)


class FakeLLM:
    pass


def make_scheduler(tmp_path, task):
    return MaintenanceScheduler(
        Path(tmp_path),
        FakeLLM(),
        rag=None,
        tasks=[task],
        state_file=tmp_path / "maintenance_state.json",
        poll_seconds=1,
        enabled=True,
    )


def test_daily_task_runs_once_per_day(tmp_path):
    calls = []

    def action():
        calls.append("run")
        return MaintenanceResult("succeeded", "ok")

    task = MaintenanceTask(name="daily", action=action, daily=True, idle_required=False)
    scheduler = make_scheduler(tmp_path, task)
    now = datetime(2026, 5, 24, 10, 0)

    assert scheduler.run_due_once(now) == ["daily"]
    assert scheduler.run_due_once(now + timedelta(hours=1)) == []
    assert scheduler.run_due_once(now + timedelta(days=1)) == ["daily"]
    assert calls == ["run", "run"]


def test_task_respects_hour_window(tmp_path):
    calls = []

    def action():
        calls.append("run")
        return MaintenanceResult("succeeded", "ok")

    task = MaintenanceTask(
        name="windowed",
        action=action,
        daily=True,
        idle_required=False,
        window_start_hour=1,
        window_end_hour=5,
    )
    scheduler = make_scheduler(tmp_path, task)

    assert scheduler.run_due_once(datetime(2026, 5, 24, 0, 59)) == []
    assert scheduler.run_due_once(datetime(2026, 5, 24, 1, 0)) == ["windowed"]
    assert calls == ["run"]


def test_scheduler_persists_state_across_instances(tmp_path):
    calls = []

    def action():
        calls.append("run")
        return MaintenanceResult("succeeded", "ok")

    task = MaintenanceTask(name="daily", action=action, daily=True, idle_required=False)
    now = datetime(2026, 5, 24, 10, 0)

    scheduler = make_scheduler(tmp_path, task)
    assert scheduler.run_due_once(now) == ["daily"]

    scheduler2 = make_scheduler(tmp_path, task)
    assert scheduler2.run_due_once(now + timedelta(hours=2)) == []
    assert calls == ["run"]


class MockTraceStore:
    def __init__(self):
        self.pruned = False

    def prune_old(self):
        self.pruned = True


class MockLLMForDefaultTasks:
    def __init__(self, trace_store=None):
        self.trace_store = trace_store


def test_default_tasks_registration(tmp_path):
    llm = MockLLMForDefaultTasks()
    scheduler = MaintenanceScheduler(
        project_root=Path(tmp_path),
        llm=llm,
        rag=None,
        state_file=tmp_path / "maintenance_state.json",
        enabled=False,
    )
    task_names = [t.name for t in scheduler.tasks]
    assert "trace_prune_daily" in task_names

    prune_task = next(t for t in scheduler.tasks if t.name == "trace_prune_daily")
    assert prune_task.daily is True
    assert prune_task.idle_required is False

    result1 = prune_task.action()
    assert result1.status == "skipped"

    store = MockTraceStore()
    llm.trace_store = store
    result2 = prune_task.action()
    assert result2.status == "succeeded"
    assert store.pruned is True


def test_echo_canary_task_registered(tmp_path):
    # F1 defense 5 monitoring is scheduled (weekly, idle-gated). Action not
    # invoked here — it scans the real vault; its logic is covered in
    # test_echo_canary.py.
    scheduler = MaintenanceScheduler(
        project_root=Path(tmp_path), llm=MockLLMForDefaultTasks(), rag=None,
        state_file=tmp_path / "maintenance_state.json", enabled=False,
    )
    names = [t.name for t in scheduler.tasks]
    assert "echo_canary_weekly" in names
    t = next(t for t in scheduler.tasks if t.name == "echo_canary_weekly")
    assert t.interval_seconds == 7 * 86400 and t.idle_required is True


def test_full_insight_date_part_supports_old_and_new_filenames():
    old_path = Path("🎐full-insight-20260529-213000.md")
    new_path = Path("[20260530-101119][Siddhartha][full-insight].md")

    assert MaintenanceScheduler._full_insight_date_part(old_path) == "20260529"
    assert MaintenanceScheduler._full_insight_date_part(new_path) == "20260530"
    assert (
        MaintenanceScheduler._full_insight_date_part(Path("[20260530][Vault][insight-recency].md"))
        is None
    )


# ── R7-E: busy-lock acquisition must not stomp a concurrent owner ──────

from watchers.maintenance_scheduler import global_busy_state


def _result_action(calls):
    def action():
        calls.append(1)
        return MaintenanceResult(status="succeeded", summary="ok")
    return action


def test_idle_task_skips_when_owner_already_busy(tmp_path):
    calls = []
    task = MaintenanceTask(name="idle_t", action=_result_action(calls), idle_required=True)
    scheduler = make_scheduler(tmp_path, task)
    assert global_busy_state.try_set_busy() is True   # a user/other owner holds busy
    try:
        scheduler._run_task(task, datetime(2026, 6, 13, 3, 0))
        assert calls == []                            # task did not run
        assert global_busy_state.is_busy() is True    # owner's lock untouched
    finally:
        global_busy_state.set_busy(False)


def test_non_idle_task_runs_but_does_not_release_foreign_lock(tmp_path):
    calls = []
    task = MaintenanceTask(name="bench", action=_result_action(calls), idle_required=False)
    scheduler = make_scheduler(tmp_path, task)
    assert global_busy_state.try_set_busy() is True
    try:
        scheduler._run_task(task, datetime(2026, 6, 13, 3, 0))
        assert calls == [1]                           # non-idle task ran anyway
        assert global_busy_state.is_busy() is True    # but did NOT clear the owner's lock
    finally:
        global_busy_state.set_busy(False)


def test_task_acquires_and_releases_when_idle(tmp_path):
    calls = []
    task = MaintenanceTask(name="idle_t", action=_result_action(calls), idle_required=True)
    scheduler = make_scheduler(tmp_path, task)
    assert global_busy_state.is_busy() is False
    scheduler._run_task(task, datetime(2026, 6, 13, 3, 0))
    assert calls == [1]
    assert global_busy_state.is_busy() is False       # acquired then released
