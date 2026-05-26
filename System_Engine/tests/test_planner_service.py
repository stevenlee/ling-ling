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


def test_canonical_pattern_recommends_digest_for_multi_target():
    specs = [
        CapabilitySpec(
            name="load_sources",
            type="operation",
            source_path=Path("/fake/load_sources.md"),
            description="load sources",
            cost_class="low",
        ),
        CapabilitySpec(
            name="digest_sources",
            type="operation",
            source_path=Path("/fake/digest_sources.md"),
            description="digest sources",
            cost_class="medium",
        ),
        CapabilitySpec(
            name="answer_from_sources",
            type="operation",
            source_path=Path("/fake/answer_from_sources.md"),
            description="answer from sources",
            cost_class="medium",
        )
    ]
    # We can invoke canonical_planning_patterns directly
    patterns = PlannerService.canonical_planning_patterns(specs, target_titles=["BookA", "BookB"])
    assert "Pattern: load vault sources, digest per-source, then answer from digests" in patterns
    assert "load_digest_answer" in patterns
    assert "llm.digest_sources" in patterns

    # For 1 target title, it should recommend the 2-step pattern
    patterns_single = PlannerService.canonical_planning_patterns(specs, target_titles=["BookA"])
    assert "Pattern: load vault sources before final answer" in patterns_single
    assert "load_sources_then_answer" in patterns_single
    assert "load_digest_answer" not in patterns_single

