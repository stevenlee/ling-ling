import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from services.capability_manager import CapabilitySpec
from services.planner_service import PlannerService


class _CapMgr:
    def __init__(self, specs):
        self.specs = specs

    def all(self):
        return list(self.specs)


class _LLM:
    def __init__(self, response, specs=None):
        self.response = response
        self.capability_manager = _CapMgr(specs if specs is not None else [
            CapabilitySpec(
                name="critique",
                type="operation",
                source_path=Path("/fake/critique.md"),
                description="critique candidate",
                cost_class="low",
            ),
            CapabilitySpec(
                name="load_sources",
                type="operation",
                source_path=Path("/fake/load_sources.md"),
                description="load source text",
                cost_class="low",
            ),
            CapabilitySpec(
                name="answer_from_sources",
                type="operation",
                source_path=Path("/fake/answer_from_sources.md"),
                description="final answer",
                cost_class="medium",
            )
        ])
        self.calls = []

    def answer_query(self, query_content, wiki_context="", **kwargs):
        self.calls.append({"query": query_content, **kwargs})
        return self.response


def test_generate_plan_validates_pipeline_spec():
    llm = _LLM("""```json
{
  "id": "critique_only",
  "description": "Critique only",
  "steps": [
    {
      "id": "crit",
      "capability": "critique",
      "adapter": "llm.critique",
      "inputs": {"candidate": "${context.candidate}"}
    }
  ]
}
```""")

    result = PlannerService(llm).generate_plan(user_directive="critique this")

    assert result.ok is True
    assert result.spec.id == "critique_only"
    assert result.spec.steps[0].capability == "critique"
    assert llm.calls[0]["operation"] == "plan"
    assert "Available Capabilities" in llm.calls[0]["query"]
    assert "Execution Readiness Rules" in llm.calls[0]["query"]
    assert "Canonical Planning Patterns" in llm.calls[0]["query"]
    assert "Pattern: load vault sources before final answer" in llm.calls[0]["query"]
    assert "adapter: vault.load_sources" in llm.calls[0]["query"]
    assert "adapter: llm.answer_from_sources" in llm.calls[0]["query"]
    assert "Do not pass bare wikilinks as `sources`" in llm.calls[0]["query"]


def test_generate_plan_reports_no_json():
    result = PlannerService(_LLM("no json here")).generate_plan(
        user_directive="make a plan"
    )

    assert result.ok is False
    assert result.status == "no_json"
    assert "did not contain a JSON object" in result.error


def test_generate_plan_reports_invalid_schema():
    result = PlannerService(_LLM("""```json
{"id": "broken", "steps": [{"id": "x"}]}
```""")).generate_plan(user_directive="make a plan")

    assert result.ok is False
    assert result.status == "invalid_schema"
    assert "failed validation" in result.error
