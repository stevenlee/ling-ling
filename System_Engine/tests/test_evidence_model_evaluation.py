"""Isolated evaluation boundary: no providers, vault or default trace writes."""

import json
from pathlib import Path

import pytest

from services import evidence_model_evaluation as evaluation
from services.evidence_audit import text_hash
from services.evidence_evaluation import load_dataset

_PROMPT = "synthetic evaluation prompt"
_CASES = Path(__file__).parent / "fixtures/evidence_evaluation/cases.json"


class FakeLLM:
    provider, model = "fake", "fake-model"

    def __init__(self, reply=None, failure=None):
        self.reply, self.failure, self.calls = reply, failure, []

    def judge_evidence_scope(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        if self.reply is not None:
            return self.reply
        return {
            "contract_version": 2,
            "relation": "neutral",
            "reason": "沒有明確支持",
            "quotes": [],
            "unsupported": [],
            "applicability": "無適用範圍",
            "audit_context": {
                "provider": self.provider,
                "model": self.model,
                "prompt_asset": "fake",
                "prompt_body": _PROMPT,
                "prompt_sha256": text_hash(_PROMPT),
                "contract_version": 2,
                "scope_attempt": 1,
                "trace_run_id": None,
            },
        }


def _run(tmp_path, llm=None, **kwargs):
    return evaluation.run_model_evaluation(
        load_dataset(_CASES), llm or FakeLLM(), tmp_path, prompt=_PROMPT, **kwargs
    )


def test_all_neutral_control_keeps_positive_and_negative_denominators(tmp_path):
    llm = FakeLLM()
    result = _run(tmp_path, llm)
    assert result["correct"] == 6 and result["total"] == 24 and not result["passed"]
    assert len(llm.calls) == 24
    for call in llm.calls:
        assert set(call) == {"claim", "falsifier", "title", "passage", "claim_id"}
        assert "expected_label" not in call and "rationale" not in call
    assert json.loads((tmp_path / "details.json").read_text())["status"] == "completed"


def test_limited_run_does_not_shrink_denominator(tmp_path):
    result = _run(tmp_path, max_cases=1)
    assert result["total"] == 24 and result["missing"] == 23
    assert json.loads((tmp_path / "details.json").read_text())["status"] == "limited"


def test_transport_error_is_not_retried_by_evaluator_or_coerced_to_neutral(tmp_path):
    llm = FakeLLM(failure=ConnectionError("offline"))
    result = _run(tmp_path, llm)
    assert len(llm.calls) == 1
    assert result["errors"] == 1 and result["missing"] == 23
    assert json.loads((tmp_path / "details.json").read_text())["status"] == "stopped_error_budget"


def test_busy_daemon_stops_before_next_model_call(tmp_path):
    checks = iter([True, False])
    llm = FakeLLM()
    result = _run(tmp_path, llm, can_start=lambda: next(checks))
    assert len(llm.calls) == 1 and result["missing"] == 23
    assert (
        json.loads((tmp_path / "details.json").read_text())["status"]
        == "stopped_daemon_busy_or_unavailable"
    )


def test_interrupted_call_has_durable_started_error_row(tmp_path):
    llm = FakeLLM(failure=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, llm)
    predictions = json.loads((tmp_path / "predictions.json").read_text())
    detail = json.loads((tmp_path / "details.json").read_text())
    assert predictions["predictions"][0]["label"] == "error"
    assert detail["status"] == "interrupted" and detail["cases"][0]["status"] == "started"


def test_existing_run_is_not_overwritten_or_replayed(tmp_path):
    _run(tmp_path, max_cases=1)
    original = (tmp_path / "predictions.json").read_bytes()
    llm = FakeLLM()
    with pytest.raises(ValueError, match="已有 run"):
        _run(tmp_path, llm)
    assert llm.calls == [] and (tmp_path / "predictions.json").read_bytes() == original


def test_checkpoint_failure_stops_before_model_call(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(evaluation, "atomic_write_text", fail)
    llm = FakeLLM()
    with pytest.raises(OSError, match="disk full"):
        _run(tmp_path, llm)
    assert llm.calls == []


def test_live_vault_output_is_rejected_before_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "WIKI_VAULT_DIR", tmp_path / "vault")
    with pytest.raises(ValueError, match="live vault"):
        _run(tmp_path / "vault/output")
    assert not (tmp_path / "vault").exists()


def test_live_trace_path_is_rejected_before_any_checkpoint(tmp_path):
    class Trace:
        db_path = tmp_path / "live.sqlite"

    llm = FakeLLM()
    llm.trace_store = Trace()
    with pytest.raises(ValueError, match="獨立 trace"):
        _run(tmp_path / "output", llm)
    assert not (tmp_path / "output").exists()


def test_model_or_prompt_drift_is_an_error_not_mixed_into_scores(tmp_path):
    reply = FakeLLM().judge_evidence_scope()
    reply["audit_context"]["model"] = "changed-model"
    result = _run(tmp_path, FakeLLM(reply=reply))
    assert result["errors"] == 1 and result["correct"] == 0
    assert "版本" in json.loads((tmp_path / "details.json").read_text())["cases"][0]["error"]


def test_contradicts_is_mapped_to_reference_contradiction(tmp_path):
    reply = FakeLLM().judge_evidence_scope()
    reply.update(relation="contradicts", quotes=[load_dataset(_CASES).cases[0].passage])
    _run(tmp_path, FakeLLM(reply=reply), max_cases=1)
    assert (
        json.loads((tmp_path / "predictions.json").read_text())["predictions"][0]["label"]
        == "contradiction"
    )


def test_explicit_client_trace_injection_never_initializes_default_store(tmp_path, monkeypatch):
    from services import llm_client as module
    from services.trace_store import TraceStore

    trace = TraceStore(tmp_path / "isolated.sqlite")
    monkeypatch.setattr(
        module, "TraceStore", lambda: pytest.fail("default trace must not initialize")
    )
    monkeypatch.setattr(module, "CapabilityManager", lambda *args: object())
    monkeypatch.setattr(
        module._transport, "build_client", lambda provider: (object(), "fake-model")
    )
    assert module.LLMClient(trace_store=trace).trace_store is trace


@pytest.mark.parametrize("status", [{"busy": True}, {"busy": "false"}, {}])
def test_cli_busy_or_unknown_status_never_initializes_model(tmp_path, monkeypatch, status):
    from scripts import run_evidence_scope_eval as cli

    state = tmp_path / "status.json"
    state.write_text(json.dumps(status), encoding="utf-8")
    monkeypatch.setattr(cli, "LLMClient", lambda **kwargs: pytest.fail("model must not initialize"))
    result = cli.main(["--output-dir", str(tmp_path / "run"), "--daemon-status", str(state)])
    assert result == 2 and not (tmp_path / "run").exists()


def test_cli_interrupt_marks_isolated_trace_failed_and_keeps_checkpoint(tmp_path, monkeypatch):
    import sqlite3
    from scripts import run_evidence_scope_eval as cli

    state = tmp_path / "status.json"
    state.write_text('{"busy": false}', encoding="utf-8")

    class Client(FakeLLM):
        def __init__(self, *, trace_store):
            super().__init__(failure=KeyboardInterrupt())
            self.trace_store = trace_store

        def _load_capability_body(self, path):
            return _PROMPT

    monkeypatch.setattr(cli, "LLMClient", Client)
    output = tmp_path / "run"
    assert cli.main(["--output-dir", str(output), "--daemon-status", str(state)]) == 2
    with sqlite3.connect(f"file:{output / 'trace.sqlite'}?mode=ro", uri=True) as db:
        assert db.execute("select status from runs").fetchone()[0] == "failed"
    assert json.loads((output / "details.json").read_text())["status"] == "interrupted"


def test_cli_existing_directory_never_initializes_model(tmp_path, monkeypatch):
    from scripts import run_evidence_scope_eval as cli

    state = tmp_path / "status.json"
    state.write_text('{"busy": false}', encoding="utf-8")
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(cli, "LLMClient", lambda **kwargs: pytest.fail("model must not initialize"))
    assert cli.main(["--output-dir", str(output), "--daemon-status", str(state)]) == 2
    assert marker.read_text(encoding="utf-8") == "keep"
