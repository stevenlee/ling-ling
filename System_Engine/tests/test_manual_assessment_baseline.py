"""Manual M1 entrypoints share the scheduled source baseline and failure semantics."""

import json
from types import SimpleNamespace

import pytest

from agents.improve_agent import ImproveAgent
from services.command_dispatcher import CommandDispatcher
from watchers.maintenance_scheduler import MaintenanceScheduler
from maintenance import self_assessment as assessment


class FakeTrace:
    def query_all_artifacts(self, since_days=7):
        return []

    def llm_call_health(self, since_days=7):
        return {"total": 0, "failed": 0, "error_rate": 0, "by_stage": {}}


class SourceRAG:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def sample_document_embeddings(self, limit=300):
        self.calls.append(limit)
        if self.fail:
            raise OSError("source store offline")
        return [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0]]


def _invoke(entrypoint, llm, rag, tmp_path):
    if entrypoint == "improve":
        return ImproveAgent(llm, rag)._generate()
    if entrypoint == "assess":
        dispatcher = CommandDispatcher.__new__(CommandDispatcher)
        dispatcher.llm, dispatcher.rag = llm, rag
        return dispatcher._run_brain_op("assess", [])
    scheduler = MaintenanceScheduler(
        tmp_path, llm, rag, state_file=tmp_path / "scheduler.json", enabled=False
    )
    return (
        next(task for task in scheduler.tasks if task.name == "self_assessment_weekly")
        .action()
        .summary
    )


@pytest.mark.parametrize("entrypoint", ["improve", "assess", "scheduled"])
@pytest.mark.parametrize("source_state", ["available", "unavailable", "absent"])
def test_manual_and_scheduled_source_baseline_contract(
    tmp_path, monkeypatch, entrypoint, source_state
):
    paths = {
        "cortex_dir": tmp_path / "Cortex",
        "insights_dir": tmp_path / "Insights",
        "bench_history_file": tmp_path / "bench.json",
        "decay_file": tmp_path / "decay.json",
        "ledger_file": tmp_path / "ledger.json",
        "insight_signals_file": tmp_path / "insight.json",
        "cortex_state_file": tmp_path / "cortex.json",
        "report_dir": tmp_path / "reports",
        "log_path": tmp_path / "maintenance.md",
        "history_file": tmp_path / "history.json",
    }
    paths["insight_signals_file"].write_text(
        json.dumps({f"insight-{i}": {"embedding": [float(i), 0, 0]} for i in range(5)}),
        encoding="utf-8",
    )
    rag = None if source_state == "absent" else SourceRAG(fail=source_state == "unavailable")
    trace = FakeTrace()
    llm = SimpleNamespace(trace_store=trace)
    real_assess = assessment.run_self_assessment
    results, downstream = [], []

    def isolated_assess(trace_store, *, rag=None):
        assert trace_store is trace
        result = real_assess(trace_store, rag=rag, **paths)
        results.append(result)
        return result

    def diagnose(client, result):
        assert client is llm
        downstream.append(result)
        return "diagnosis"

    def improve(client, result, diagnosis):
        assert client is llm and diagnosis == "diagnosis"
        downstream.append(result)
        return SimpleNamespace(proposals=[], skipped_axes=[], stale_pending=[])

    monkeypatch.setattr(assessment, "run_self_assessment", isolated_assess)
    monkeypatch.setattr("maintenance.self_diagnosis.run_self_diagnosis", diagnose)
    monkeypatch.setattr("maintenance.self_improve.run_self_improve", improve)
    monkeypatch.setattr("core.config.SELF_DIAGNOSIS_ENABLED", False)
    monkeypatch.setattr("core.config.SELF_IMPROVE_ENABLED", False)
    summary = _invoke(entrypoint, llm, rag, tmp_path)
    assert len(results) == 1 and results[0].status == "succeeded"
    axis = next(a for a in results[0].axes if a.name == "語義熵")
    if source_state == "available":
        assert rag.calls == [300]
        assert axis.detail["n_source"] == 4
        assert axis.detail["source_dim"] > 1.5
        assert axis.detail["eff_dim_ratio"] is not None
        assert "無來源基準" not in axis.summary
    else:
        if rag is not None:
            assert rag.calls == [300]
        assert axis.detail["source_dim"] is None
        assert axis.detail["eff_dim_ratio"] is None
        assert axis.detail["n_source"] == 0
        assert "無來源基準" in axis.summary
    assert "自評" in summary
    if entrypoint == "improve":
        assert len(downstream) == 2 and all(item is results[0] for item in downstream)
    else:
        assert downstream == []
    assert paths["history_file"].exists()


@pytest.mark.parametrize("entrypoint", ["improve", "assess"])
def test_missing_trace_still_skips_before_source_sampling(tmp_path, entrypoint):
    rag = SourceRAG()
    summary = _invoke(entrypoint, SimpleNamespace(trace_store=None), rag, tmp_path)
    assert "無法" in summary
    assert rag.calls == []
