"""Checkout provenance stays tied to a process and cannot be forged by callers."""

import json
import subprocess
from datetime import datetime
from types import SimpleNamespace

import pytest

from core import runtime_identity as provenance
from core.ui import LingLingUI
from services.llm_client import LLMClient
from services.trace_store import TraceStore
from watchers.maintenance_scheduler import MaintenanceResult, MaintenanceScheduler, MaintenanceTask


@pytest.mark.parametrize("status,dirty", [("", False), (" M System_Engine/main.py", True)])
def test_capture_checkout_status(monkeypatch, status, dirty):
    calls = []

    def git(*args):
        calls.append(args)
        return status if args[0] == "status" else "a" * 40

    monkeypatch.setattr(provenance, "_git", git)
    identity = provenance._capture_identity()
    assert identity["commit"] == "a" * 40
    assert identity["engine_dirty"] is dirty
    assert identity["session_id"] and identity["captured_at"] and identity["pid"]
    assert calls[1][-3:] == ("System_Engine", "pyproject.toml", "Makefile")


@pytest.mark.parametrize(
    "failure", [OSError("git unavailable"), subprocess.TimeoutExpired("git", 2)]
)
def test_missing_git_is_unknown_not_clean(monkeypatch, failure):
    def fail(*args):
        raise failure

    monkeypatch.setattr(provenance, "_git", fail)
    result = provenance._capture_identity()
    assert result["commit"] is None and result["engine_dirty"] is None


def test_status_failure_does_not_claim_clean_commit(monkeypatch):
    def git(*args):
        if args[0] == "status":
            raise subprocess.CalledProcessError(128, "git")
        return "a" * 40

    monkeypatch.setattr(provenance, "_git", git)
    result = provenance._capture_identity()
    assert result["commit"] == "a" * 40 and result["engine_dirty"] is None


@pytest.mark.parametrize("heads", [("invalid",), ("a" * 40, "b" * 40)])
def test_invalid_or_moving_checkout_is_unknown(monkeypatch, heads):
    values = iter(heads)
    monkeypatch.setattr(provenance, "_git", lambda *a: "" if a[0] == "status" else next(values))
    result = provenance._capture_identity()
    assert result["commit"] is None and result["engine_dirty"] is None


def test_frozen_identity_is_not_relabelled_by_later_git_or_caller(monkeypatch):
    initial = provenance.runtime_identity()
    monkeypatch.setattr(provenance, "_git", lambda *a: pytest.fail("must not poll git"))
    modified = provenance.runtime_identity()
    modified["commit"] = "forged"
    assert provenance.runtime_identity() == initial


def test_config_allowlist_excludes_secrets_and_observes_dynamic_flags(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret")
    before = provenance.runtime_config()
    monkeypatch.setattr(
        provenance.config.settings,
        "EVIDENCE_TRACEBACK_APPLY",
        not before["evidence_traceback_apply"],
    )
    after = provenance.runtime_config()
    assert after["evidence_traceback_apply"] is not before["evidence_traceback_apply"]
    assert "secret" not in json.dumps(after)
    assert set(after) == {
        "embedding_provider",
        "embedding_model",
        "reranker_enabled",
        "reranker_model",
        "facet_index_enabled",
        "cross_lingual_enabled",
        "evidence_traceback_enabled",
        "evidence_traceback_apply",
        "evidence_traceback_max_distance",
    }


def test_status_and_nested_runs_share_provenance_without_mutating_input(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.DAEMON_STATUS_FILE", tmp_path / "status.json")
    LingLingUI._persist_status("ready", False)
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    store = TraceStore(db_path=tmp_path / "trace.sqlite")
    metadata = {"task": "test", "runtime": {"commit": "forged"}, "runtime_config": {}}
    with store.run(intent="outer", metadata=metadata):
        with store.run(intent="inner"):
            pass
    with store._connect() as conn:
        rows = conn.execute("SELECT metadata_json FROM runs").fetchall()
    assert len(rows) == 2
    for row in rows:
        data = json.loads(row[0])
        assert data["runtime"] == status["runtime"] == provenance.runtime_identity()
        assert data["runtime_config"] == status["runtime_config"]
    assert metadata["runtime"]["commit"] == "forged" and metadata["runtime_config"] == {}
    assert status["busy"] is False and status["message"] == "ready"


@pytest.mark.parametrize("failed", [False, True])
def test_task_state_and_trace_include_same_session_on_failure(tmp_path, monkeypatch, failed):
    from core.state import BusyState

    monkeypatch.setattr("watchers.maintenance_scheduler.global_busy_state", BusyState())
    store = TraceStore(db_path=tmp_path / "trace.sqlite")
    client = LLMClient.__new__(LLMClient)
    client.trace_store, client.provider, client.model = store, "fake", "test-model"
    metadata = {"task": "kept", "llm": {"model": "forged"}}
    with client.trace_run(metadata=metadata):
        pass
    assert metadata["llm"]["model"] == "forged"
    task = MaintenanceTask(
        "test", lambda: MaintenanceResult("failed" if failed else "succeeded", "done")
    )
    scheduler = MaintenanceScheduler(
        tmp_path,
        client,
        SimpleNamespace(),
        tasks=[task],
        state_file=tmp_path / "scheduler.json",
        enabled=False,
    )
    scheduler._run_task(task, datetime(2026, 11, 4, 12))
    state = json.loads((tmp_path / "scheduler.json").read_text(encoding="utf-8"))["test"]
    with store._connect() as conn:
        rows = conn.execute("SELECT metadata_json, status FROM runs").fetchall()
    assert state["last_runtime"] == provenance.runtime_identity()
    assert state["last_status"] == ("failed" if failed else "succeeded")
    for row in rows:
        data = json.loads(row[0])
        assert data["runtime"] == state["last_runtime"]
        assert data["llm"] == {"provider": "fake", "model": "test-model"}
    assert rows[-1][1] == state["last_status"]
