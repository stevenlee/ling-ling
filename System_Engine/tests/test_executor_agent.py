"""Tests for agents.executor_agent — Phase 5C controlled execution."""
import json
import os
import sys
from pathlib import Path
from textwrap import dedent

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from services.capability_manager import CapabilitySpec
from agents.executor_agent import ExecutorAgent


# ── Doubles ─────────────────────────────────────────────────────────


class _FakeCapMgr:
    def __init__(self, specs):
        self._by_name = {s.name: s for s in specs}

    def get(self, name):
        return self._by_name.get(name)

    def all(self):
        return list(self._by_name.values())


def _op(name: str, **kw) -> CapabilitySpec:
    return CapabilitySpec(
        name=name,
        type="operation",
        source_path=Path(f"/fake/{name}.md"),
        description=kw.get("description", ""),
        expected_inputs=tuple(kw.get("expected_inputs", ())),
        produces=tuple(kw.get("produces", ("output",))),
        cost_class=kw.get("cost_class", "low"),
    )


class _FakeTraceStore:
    """Records run() opens, supports nested context for parent tracking."""

    def __init__(self):
        self.runs: list[dict] = []
        self.artifacts: list[dict] = []

    def run(self, **kwargs):
        self.runs.append(kwargs)
        return _FakeRunCtx(f"run_{len(self.runs)}")

    def record_artifact(self, **kwargs):
        self.artifacts.append(kwargs)
        return f"art_{len(self.artifacts)}"


class _FakeRunCtx:
    def __init__(self, run_id):
        self.run_id = run_id

    def __enter__(self):
        return self.run_id

    def __exit__(self, *exc):
        return None


class _FakeLLM:
    """Implements just enough of LLMClient for ExecutorAgent + BaseAgent."""

    def __init__(self, capability_manager):
        self.capability_manager = capability_manager
        self.trace_store = _FakeTraceStore()
        self.provider = "test"
        self.model = "test"
        # Synthesis / critique behavior is canned per test.
        self.synthesize_returns = "FAKE SYNTHESIS"
        self.critique_returns = "FAKE CRITIQUE"
        self.synthesize_raises = None
        self.synthesize_calls = []
        self.critique_calls = []

    def generate_synthesis(self, *, title, part_digests, final_concepts, template=None):
        self.synthesize_calls.append({
            "title": title, "part_digests": part_digests,
            "final_concepts": final_concepts, "template": template,
        })
        if self.synthesize_raises:
            raise self.synthesize_raises
        return self.synthesize_returns

    def critique_text(self, *, candidate, sources, focus=None):
        self.critique_calls.append({
            "candidate": candidate, "sources": sources, "focus": focus,
        })
        return self.critique_returns

    def current_trace_ids(self):
        return []

    def current_run_id(self):
        return None


def _executor(llm) -> ExecutorAgent:
    agent = ExecutorAgent(llm)
    agent._writes = []

    def fake_write_report(title, body, report_type, metadata=None):
        agent._writes.append({
            "title": title, "body": body,
            "report_type": report_type, "metadata": metadata or {},
        })
        return (Path("/fake/report.md"), body)

    agent._write_report = fake_write_report  # type: ignore[assignment]
    return agent


def _write_plan(tmp_path: Path, plan_id: str, plan: dict) -> Path:
    plan_path = tmp_path / f"{plan_id}.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan_path


def _valid_plan(plan_id: str = "demo") -> dict:
    return {
        "id": plan_id,
        "description": "Synthesize then critique",
        "steps": [
            {
                "id": "synth",
                "capability": "synthesize",
                "adapter": "llm.synthesize",
                "inputs": {"title": "${context.title}"},
            },
            {
                "id": "crit",
                "capability": "critique",
                "adapter": "llm.critique",
                "when": {"var": "steps.synth.output", "op": "nonempty"},
                "inputs": {"candidate": "${steps.synth.output}"},
            },
        ],
    }


# ── parse_plan_id ──────────────────────────────────────────────────


class TestParsePlanId:
    def test_extracts_plan_id_after_ling_do(self):
        assert ExecutorAgent._parse_plan_id(
            "@ling-do plan_abc_123", {}
        ) == "plan_abc_123"

    def test_explicit_task_context_wins(self):
        assert ExecutorAgent._parse_plan_id(
            "@ling-do other_id",
            {"plan_id": "from_context"},
        ) == "from_context"

    def test_slash_do(self):
        assert ExecutorAgent._parse_plan_id(
            "Please /do plan_xy_z right now", {}
        ) == "plan_xy_z"

    def test_no_match_returns_none(self):
        assert ExecutorAgent._parse_plan_id("just some text", {}) is None

    def test_short_token_rejected(self):
        # The fallback scan requires at least 3 chars
        assert ExecutorAgent._parse_plan_id("@ling-do ab", {}) is None


# ── _load_sidecar ──────────────────────────────────────────────────


class TestLoadSidecar:
    def test_loads_json(self, tmp_path, monkeypatch):
        import agents.executor_agent as exec_mod
        monkeypatch.setattr(exec_mod, "PLANS_DIR", tmp_path)
        _write_plan(tmp_path, "p1", {"id": "p1", "steps": []})
        loaded = ExecutorAgent._load_sidecar("p1")
        assert loaded == {"id": "p1", "steps": []}

    def test_missing_returns_none(self, tmp_path, monkeypatch):
        import agents.executor_agent as exec_mod
        monkeypatch.setattr(exec_mod, "PLANS_DIR", tmp_path)
        assert ExecutorAgent._load_sidecar("nope") is None

    def test_invalid_json_returns_none(self, tmp_path, monkeypatch):
        import agents.executor_agent as exec_mod
        monkeypatch.setattr(exec_mod, "PLANS_DIR", tmp_path)
        (tmp_path / "bad.json").write_text("{not valid json}", encoding="utf-8")
        assert ExecutorAgent._load_sidecar("bad") is None


# ── execute() flow ────────────────────────────────────────────────


class TestExecutorExecute:
    def _setup(self, tmp_path, monkeypatch):
        import agents.executor_agent as exec_mod
        monkeypatch.setattr(exec_mod, "PLANS_DIR", tmp_path)
        cm = _FakeCapMgr([_op("synthesize"), _op("critique")])
        llm = _FakeLLM(cm)
        agent = _executor(llm)
        return agent, llm, exec_mod

    def test_unparseable_directive_errors_out(self, tmp_path, monkeypatch):
        agent, _, _ = self._setup(tmp_path, monkeypatch)
        body = agent.execute({"user_directive": "do something but no plan"})
        assert "could not parse a plan_id" in body
        assert agent._writes[0]["metadata"]["error"] is True

    def test_missing_plan_errors_out(self, tmp_path, monkeypatch):
        agent, _, _ = self._setup(tmp_path, monkeypatch)
        body = agent.execute({"user_directive": "@ling-do ghost_plan"})
        assert "no plan found" in body
        assert "ghost_plan" in body

    def test_invalid_schema_in_sidecar_errors_out(self, tmp_path, monkeypatch):
        agent, _, _ = self._setup(tmp_path, monkeypatch)
        _write_plan(tmp_path, "broken", {"id": "broken", "steps": [{"id": "a"}]})
        body = agent.execute({"user_directive": "@ling-do broken"})
        assert "failed re-validation" in body

    def test_capability_drift_errors_out(self, tmp_path, monkeypatch):
        # Plan references "critique" but the live registry has only synthesize.
        import agents.executor_agent as exec_mod
        monkeypatch.setattr(exec_mod, "PLANS_DIR", tmp_path)
        cm = _FakeCapMgr([_op("synthesize")])  # critique missing!
        llm = _FakeLLM(cm)
        agent = _executor(llm)
        _write_plan(tmp_path, "drift", _valid_plan("drift"))
        body = agent.execute({"user_directive": "@ling-do drift"})
        assert "not available in the live registry" in body
        assert "critique" in body

    def test_happy_path_runs_pipeline_against_real_adapters(self, tmp_path, monkeypatch):
        agent, llm, _ = self._setup(tmp_path, monkeypatch)
        _write_plan(tmp_path, "happy", _valid_plan("happy"))
        body = agent.execute({
            "user_directive": "@ling-do happy",
            "execute_context": {
                "title": "Hamlet",
                "part_digests": [{"part": 1, "thesis": "test"}],
            },
        })
        assert "Execution: Synthesize then critique" in body
        # Both LLM-backed adapters were exercised
        assert len(llm.synthesize_calls) == 1
        assert llm.synthesize_calls[0]["title"] == "Hamlet"
        assert len(llm.critique_calls) == 1
        # Report metadata captures status and pipeline_run_id
        write = agent._writes[0]
        assert write["report_type"] == "executor_run"
        assert write["metadata"]["plan_id"] == "happy"
        assert write["metadata"]["execution_status"] == "succeeded"
        # Status for each step
        steps = write["metadata"]["step_statuses"]
        assert steps == {"synth": "succeeded", "crit": "succeeded"}
        # Parent + child runs were opened on the FakeTraceStore
        intents = [r["intent"] for r in llm.trace_store.runs]
        assert "pipeline:happy" in intents
        assert "step:synth" in intents
        assert "step:crit" in intents

    def test_when_clause_skips_dependent_step(self, tmp_path, monkeypatch):
        agent, llm, _ = self._setup(tmp_path, monkeypatch)
        # synth returns empty → critique's when:nonempty fires false
        llm.synthesize_returns = ""
        _write_plan(tmp_path, "skipper", _valid_plan("skipper"))
        body = agent.execute({
            "user_directive": "@ling-do skipper",
            "execute_context": {"title": "X"},
        })
        write = agent._writes[0]
        steps = write["metadata"]["step_statuses"]
        assert steps["synth"] == "succeeded"
        assert steps["crit"] == "skipped"
        # critique adapter was NOT invoked
        assert len(llm.critique_calls) == 0

    def test_adapter_exception_aborts_pipeline(self, tmp_path, monkeypatch):
        agent, llm, _ = self._setup(tmp_path, monkeypatch)
        llm.synthesize_raises = RuntimeError("boom from synthesize")
        _write_plan(tmp_path, "boom", _valid_plan("boom"))
        body = agent.execute({
            "user_directive": "@ling-do boom",
            "execute_context": {"title": "X"},
        })
        assert "❌" in body
        write = agent._writes[0]
        assert write["metadata"]["execution_status"] == "failed"
        # critique should NOT run after synthesize fails
        assert "crit" not in write["metadata"]["step_statuses"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
