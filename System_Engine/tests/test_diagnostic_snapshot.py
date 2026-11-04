"""Diagnostic snapshots expose bounded references without source/prompt contents."""

import json
from datetime import datetime

import pytest

from services.trace_store import TraceStore


def test_snapshot_is_bounded_and_keeps_statuses_and_unscored_insight_refs(tmp_path):
    store = TraceStore(db_path=tmp_path / "trace.sqlite")
    for intent in ("maintenance.evidence_traceback", "insight", "unrelated"):
        for i in range(5):
            with store.run(
                intent=intent,
                trigger_type="maintenance_scheduler" if intent == "insight" else "fixture",
            ):
                store.record_artifact(
                    path=tmp_path / f"{i}.md",
                    artifact_type="report_insight" if intent == "insight" else "synthesis",
                    quality_verdict=None if intent == "insight" else "unparseable",
                    metadata={"source_text": "private source omitted"},
                )
                store.record_llm_call(
                    system_prompt="private prompt omitted",
                    user_msg="private input omitted",
                    response_text=None,
                    provider="fixture",
                    model="fixture-model",
                    stage="critique_text",
                    status="failed",
                    error="offline" * 100,
                )
    try:
        with store.run(intent="maintenance.cortex_consolidation"):
            raise OSError("task down")
    except OSError:
        pass
    with store._connect() as conn:
        conn.execute("UPDATE runs SET started_at='2000-01-01T00:00:00Z' WHERE intent='unrelated'")
    snapshot = store.query_diagnostic_snapshot(since_days=1)
    assert snapshot["status"] == "available" and snapshot["sampled"] is True
    assert snapshot["window_days"] == 1 and snapshot["captured_at"]
    intents = [r["intent"] for r in snapshot["runs"]]
    assert intents.count("maintenance.evidence_traceback") == 3
    assert intents.count("insight") == 3 and "unrelated" not in intents
    failed = next(r for r in snapshot["runs"] if r["intent"] == "maintenance.cortex_consolidation")
    assert failed["trace_status"] == "failed" and failed["error"] == "task down"
    assert len(snapshot["artifacts"]) == 6
    assert sum(r["quality_verdict"] is None for r in snapshot["artifacts"]) == 3
    assert len(snapshot["failed_calls"]) == 2
    assert all(len(r["error"]) == 300 for r in snapshot["failed_calls"])
    encoded = json.dumps(snapshot)
    for omitted in ("private source omitted", "private prompt omitted", "private input omitted"):
        assert omitted not in encoded


def test_empty_window_is_available_but_not_a_claim_that_features_are_absent(tmp_path):
    store = TraceStore(db_path=tmp_path / "trace.sqlite")
    snapshot = store.query_diagnostic_snapshot()
    assert snapshot["status"] == "available" and snapshot["sampled"]
    assert snapshot["runs"] == snapshot["artifacts"] == snapshot["failed_calls"] == []


@pytest.mark.parametrize(
    "status", ["succeeded", "skipped", "failed", "exception", "recording_outage"]
)
def test_scheduler_result_references_distinguish_skip_and_failure(tmp_path, monkeypatch, status):
    from core.state import BusyState
    from watchers.maintenance_scheduler import (
        MaintenanceScheduler,
        MaintenanceTask,
        MaintenanceResult,
    )

    store = TraceStore(db_path=tmp_path / "trace.sqlite")
    busy = BusyState()
    busy.lock_file = tmp_path / "lock"
    monkeypatch.setattr("watchers.maintenance_scheduler.global_busy_state", busy)

    class Client:
        trace_store = store

        def trace_run(self, **kwargs):
            return store.run(**kwargs)

    def action():
        if status == "exception":
            raise OSError("task failed before returning")
        return MaintenanceResult(
            "succeeded" if status == "recording_outage" else status, "fixture summary"
        )

    if status == "recording_outage":

        def unavailable(**kwargs):
            raise OSError("artifact storage offline")

        monkeypatch.setattr(store, "record_artifact", unavailable)
    task = MaintenanceTask("probe", action, intent="maintenance.probe")
    scheduler = MaintenanceScheduler(
        tmp_path, Client(), None, tasks=[task], state_file=tmp_path / "state.json", enabled=False
    )
    scheduler._run_task(task, datetime(2026, 11, 4, 12))
    run = store.query_diagnostic_snapshot()["runs"][0]
    expected_trace = "failed" if status in ("failed", "exception") else "succeeded"
    assert run["trace_status"] == expected_trace and not busy.is_busy()
    if status in ("exception", "recording_outage"):
        assert run["task_result"] is None  # unknown, never synthesize success
    else:
        assert run["task_result"]["status"] == status
        assert run["task_result"]["summary"] == "fixture summary"
        assert run["task_result"]["artifact_id"].startswith("artifact_")
    assert scheduler.state["probe"]["last_status"] == (
        "failed"
        if status == "exception"
        else "succeeded"
        if status == "recording_outage"
        else status
    )
