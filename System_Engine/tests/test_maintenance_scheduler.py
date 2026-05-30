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


def test_full_insight_date_part_supports_old_and_new_filenames():
    old_path = Path("🎐full-insight-20260529-213000.md")
    new_path = Path("[20260530-101119][Siddhartha][full-insight].md")

    assert MaintenanceScheduler._full_insight_date_part(old_path) == "20260529"
    assert MaintenanceScheduler._full_insight_date_part(new_path) == "20260530"
    assert (
        MaintenanceScheduler._full_insight_date_part(Path("[20260530][Vault][insight-recency].md"))
        is None
    )
