"""Tests for agents.planner_agent — Phase 5B plan-only logic.

No real LLM, no daemon. Uses a stub LLMClient that returns canned
answer_query output and exposes a real CapabilityManager so the
capability listing reflects production-shaped data.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from services.capability_manager import CapabilityManager, CapabilitySpec
from agents.planner_agent import PlannerAgent
from services.pipeline_runner import PipelineSpec, PipelineStep


# ── Test doubles ──────────────────────────────────────────────────────


class _FakeCapMgr:
    """Holds CapabilitySpec instances directly (no filesystem)."""

    def __init__(self, specs: list[CapabilitySpec]):
        self._by_name = {s.name: s for s in specs}

    def get(self, name):
        return self._by_name.get(name)

    def all(self):
        return list(self._by_name.values())


class _FakeTraceStore:
    def __init__(self):
        self.artifacts: list[dict] = []

    def record_artifact(self, **kwargs):
        self.artifacts.append(kwargs)
        return "art_1"


class _FakeLLM:
    """Minimum LLMClient surface PlannerAgent + BaseAgent touch."""

    def __init__(self, capability_manager, response: str = ""):
        self.capability_manager = capability_manager
        self.response = response
        self.provider = "test"
        self.model = "test"
        self.trace_store = _FakeTraceStore()
        self.calls: list[dict] = []

    def answer_query(self, query_content, wiki_context="", **kwargs):
        self.calls.append({
            "query": query_content,
            **kwargs,
        })
        return self.response

    def current_trace_ids(self):
        return []

    def current_run_id(self):
        return None


def _spec(name, **kw):
    """Tiny CapabilitySpec factory."""
    return CapabilitySpec(
        name=name,
        type=kw.get("type", "operation"),
        source_path=Path(f"/fake/{name}.md"),
        description=kw.get("description", f"description of {name}"),
        expected_inputs=tuple(kw.get("expected_inputs", ())),
        expected_context=tuple(kw.get("expected_context", ())),
        produces=tuple(kw.get("produces", ())),
        cost_class=kw.get("cost_class", "low"),
    )


def _planner(llm) -> PlannerAgent:
    """Build a PlannerAgent that won't write files to disk."""
    agent = PlannerAgent(llm)
    # Stub _write_report to avoid touching FROM_LLM_DIR in tests.
    agent._writes = []

    def fake_write_report(title, body, report_type, metadata=None):
        agent._writes.append({
            "title": title,
            "body": body,
            "report_type": report_type,
            "metadata": metadata or {},
        })
        return (Path("/fake/report.md"), body)

    agent._write_report = fake_write_report  # type: ignore[assignment]
    return agent


# ── Pure helpers ────────────────────────────────────────────────────


class TestCapabilityListing:
    def test_groups_operations_and_skills(self):
        caps = [
            _spec("synthesize", type="operation", description="combine inputs",
                  expected_inputs=("part_digests",), produces=("synthesis_text",),
                  cost_class="medium"),
            _spec("critique", type="operation", description="evaluate",
                  expected_inputs=("candidate", "sources"), produces=("critique_findings",),
                  cost_class="low"),
            _spec("recency", type="skill", description="recent additions",
                  expected_inputs=("user_directive",), produces=("insight_report",),
                  cost_class="low"),
        ]
        out = PlannerAgent._format_capability_listing(caps)
        assert "Operations" in out
        assert "synthesize" in out
        assert "critique" in out
        assert "Skills" in out
        assert "recency" in out
        # Operations section appears before Skills
        assert out.index("synthesize") < out.index("recency")

    def test_entry_includes_metadata(self):
        c = _spec(
            "synthesize", description="combine",
            expected_inputs=("part_digests",), produces=("synthesis_text",),
            cost_class="medium",
        )
        entry = PlannerAgent._format_capability_entry(c)
        assert "**synthesize**" in entry
        assert "medium" in entry
        assert "part_digests" in entry
        assert "synthesis_text" in entry


# ── Plan rendering ──────────────────────────────────────────────────


class TestRenderPlanReport:
    def _build_spec(self):
        return PipelineSpec(
            id="demo_plan",
            description="synth then critique",
            steps=(
                PipelineStep(
                    id="synth", capability="synthesize", adapter="llm.synthesize",
                    inputs={"title": "${context.title}"},
                ),
                PipelineStep(
                    id="crit", capability="critique", adapter="llm.critique",
                    inputs={"candidate": "${steps.synth.output}"},
                    when={"var": "steps.synth.output", "op": "nonempty"},
                ),
            ),
        )

    def _build_plan_dict(self):
        return {
            "id": "demo_plan",
            "description": "synth then critique",
            "summary": "Synthesize the doc, then critique the synthesis.",
            "steps": [
                {"id": "synth", "capability": "synthesize", "adapter": "llm.synthesize",
                 "inputs": {"title": "${context.title}"},
                 "rationale": "Build a coherent summary."},
                {"id": "crit", "capability": "critique", "adapter": "llm.critique",
                 "when": {"var": "steps.synth.output", "op": "nonempty"},
                 "inputs": {"candidate": "${steps.synth.output}"},
                 "rationale": "Check the synthesis for defects."},
            ],
        }

    def test_report_contains_summary_and_each_step(self):
        agent = _planner(_FakeLLM(_FakeCapMgr([
            _spec("synthesize"), _spec("critique"),
        ])))
        spec = self._build_spec()
        plan_dict = self._build_plan_dict()
        report = agent._render_plan_report(
            spec, plan_dict, "directive text",
            agent._collect_capabilities(),
        )
        assert "Synthesize the doc, then critique" in report
        assert "Step 1: `synth`" in report
        assert "Step 2: `crit`" in report
        assert "Build a coherent summary." in report
        assert "Check the synthesis for defects." in report
        assert "directive text" in report
        # Raw JSON block at the end
        assert "```json" in report
        assert "demo_plan" in report

    def test_report_warns_on_unregistered_capability(self):
        agent = _planner(_FakeLLM(_FakeCapMgr([_spec("critique")])))  # synth missing
        spec = self._build_spec()
        report = agent._render_plan_report(
            spec, self._build_plan_dict(), "directive",
            agent._collect_capabilities(),
        )
        assert "NOT in registry" in report
        assert "Unregistered Capabilities" in report

    def test_report_marks_no_execution(self):
        agent = _planner(_FakeLLM(_FakeCapMgr([_spec("synthesize"), _spec("critique")])))
        report = agent._render_plan_report(
            self._build_spec(), self._build_plan_dict(), "directive",
            agent._collect_capabilities(),
        )
        # The IMPORTANT callout must be present.
        assert "plan only" in report.lower()
        assert "no steps have been executed" in report.lower()


# ── execute() flow ──────────────────────────────────────────────────


_HAPPY_LLM_RESPONSE = """\
Here is the plan you asked for.

```json
{
  "id": "synth_then_crit",
  "description": "Synthesize then critique",
  "summary": "User wants synthesis followed by critique.",
  "steps": [
    {
      "id": "synth",
      "capability": "synthesize",
      "adapter": "llm.synthesize",
      "inputs": {"title": "${context.title}"},
      "rationale": "Produce a synthesis of the source."
    },
    {
      "id": "crit",
      "capability": "critique",
      "adapter": "llm.critique",
      "when": {"var": "steps.synth.output", "op": "nonempty"},
      "inputs": {"candidate": "${steps.synth.output}"},
      "rationale": "Critique the synthesis."
    }
  ]
}
```
"""


class TestPlannerExecute:
    def _make_caps(self):
        return _FakeCapMgr([
            _spec("synthesize", expected_inputs=("part_digests", "title"),
                  produces=("synthesis_text",), cost_class="medium"),
            _spec("critique", expected_inputs=("candidate", "sources"),
                  produces=("critique_findings",), cost_class="low"),
        ])

    def test_empty_directive_errors_out(self):
        agent = _planner(_FakeLLM(self._make_caps()))
        body = agent.execute({"user_directive": "   "})
        assert "❌" in body
        assert "empty user directive" in body
        # _write_report still called (error report)
        assert len(agent._writes) == 1
        assert agent._writes[0]["report_type"] == "planner_plan"

    def test_no_capabilities_errors_out(self):
        agent = _planner(_FakeLLM(_FakeCapMgr([])))
        body = agent.execute({"user_directive": "do something"})
        assert "empty registry" in body.lower() or "no capabilities" in body.lower()

    def test_llm_returns_no_json_errors_out(self):
        llm = _FakeLLM(self._make_caps(), response="Sorry, I cannot plan that.")
        agent = _planner(llm)
        body = agent.execute({"user_directive": "synth and critique"})
        assert "did not contain a JSON object" in body
        # The raw response gets included in the report
        assert "Sorry" in body

    def test_llm_returns_invalid_plan_errors_out(self):
        bad_json = (
            "```json\n"
            '{"id": "bad", "steps": [{"id": "a"}]}\n'  # missing capability+adapter
            "```\n"
        )
        agent = _planner(_FakeLLM(self._make_caps(), response=bad_json))
        body = agent.execute({"user_directive": "do it"})
        assert "failed validation" in body
        assert "missing 'capability'" in body

    def test_happy_path_writes_plan_report(self):
        llm = _FakeLLM(self._make_caps(), response=_HAPPY_LLM_RESPONSE)
        agent = _planner(llm)
        body = agent.execute({"user_directive": "synthesize then critique"})
        # The body is the rendered report
        assert "Plan: Synthesize then critique" in body
        assert "User wants synthesis followed by critique" in body
        assert "Step 1: `synth`" in body
        assert "Step 2: `crit`" in body
        # Exactly one report was written
        assert len(agent._writes) == 1
        write = agent._writes[0]
        assert write["report_type"] == "planner_plan"
        assert write["metadata"]["plan_id"] == "synth_then_crit"
        assert write["metadata"]["step_count"] == 2
        # The captured plan_json is the parsed dict, NOT a string
        plan_json = write["metadata"]["plan_json"]
        assert isinstance(plan_json, dict)
        assert plan_json["id"] == "synth_then_crit"
        # capability_resolution lists per-step lookups
        res = write["metadata"]["capability_resolution"]
        assert {s["step_id"] for s in res["steps"]} == {"synth", "crit"}
        for s in res["steps"]:
            assert s["registered"] is True

    def test_llm_call_uses_plan_operation_axis(self):
        llm = _FakeLLM(self._make_caps(), response=_HAPPY_LLM_RESPONSE)
        agent = _planner(llm)
        agent.execute({"user_directive": "do it"})
        # The agent asked the LLM with persona='none', operation='plan'
        assert len(llm.calls) == 1
        call = llm.calls[0]
        assert call["persona"] == "none"
        assert call["operation"] == "plan"
        assert call["forced_template"] == "none"

    def test_plan_referencing_unknown_capability_still_renders(self):
        # Plan references a capability that's NOT in the fake registry.
        llm = _FakeLLM(
            _FakeCapMgr([_spec("critique")]),  # synthesize missing
            response=_HAPPY_LLM_RESPONSE,
        )
        agent = _planner(llm)
        body = agent.execute({"user_directive": "do it"})
        # Validation passes (load_pipeline_from_dict doesn't check registry —
        # that's PipelineRunner.validate() at execution time).
        # The plan IS rendered with a warning.
        assert "Plan:" in body
        assert "NOT in registry" in body


# ── Capability resolution record ────────────────────────────────────


class TestResolvePlanCapabilities:
    def test_records_per_step_status(self):
        caps = [_spec("synthesize"), _spec("critique")]
        spec = PipelineSpec(
            id="x", description="",
            steps=(
                PipelineStep(id="a", capability="synthesize", adapter="llm.synthesize"),
                PipelineStep(id="b", capability="ghost",      adapter="llm.ghost"),
            ),
        )
        res = PlannerAgent._resolve_plan_capabilities(spec, caps)
        assert res == {
            "steps": [
                {"step_id": "a", "capability": "synthesize", "registered": True,  "adapter": "llm.synthesize"},
                {"step_id": "b", "capability": "ghost",      "registered": False, "adapter": "llm.ghost"},
            ],
        }


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
