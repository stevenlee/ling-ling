import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from services.capability_manager import CapabilitySpec
from services.plan_readiness import assess_plan_readiness
from services.pipeline_runner import load_pipeline_from_dict


def _cap(name, *, expected_inputs=(), expected_context=(), produces=()):
    return CapabilitySpec(
        name=name,
        type="operation",
        source_path=Path(f"/fake/{name}.md"),
        expected_inputs=tuple(expected_inputs),
        expected_context=tuple(expected_context),
        produces=tuple(produces),
        cost_class="low",
    )


def test_readiness_flags_semantic_execution_risks():
    plan = {
        "id": "compare_and_guide_report",
        "description": "Compare and guide",
        "summary": "Compare two notes, critique, then synthesize.",
        "steps": [
            {
                "id": "critique",
                "capability": "critique",
                "adapter": "llm.critique",
                "inputs": {
                    "candidate": "Please compare these two notes.",
                    "sources": ["[[A]]", "[[B]]"],
                    "focus": "differences and action guidance",
                },
            },
            {
                "id": "synthesize",
                "capability": "synthesize",
                "adapter": "llm.synthesize",
                "inputs": {
                    "part_digests": "${steps.critique.output}",
                },
                "context": {
                    "title": "Comparison report",
                    "final_concepts": "Turn findings into actions.",
                },
                "when": {"var": "steps.critique.output", "op": "nonempty"},
            },
        ],
    }
    spec = load_pipeline_from_dict(plan)
    report = assess_plan_readiness(
        spec=spec,
        plan_dict=plan,
        capabilities=[
            _cap("critique", expected_inputs=("candidate", "sources"),
                 expected_context=("focus",), produces=("critique_findings",)),
            _cap("synthesize", expected_inputs=("part_digests",),
                 expected_context=("title", "final_concepts"), produces=("synthesis_text",)),
        ],
    )

    assert report.verdict == "needs_review"
    codes = {f.code for f in report.findings}
    assert "wikilink_sources_unresolved" in codes
    assert "unknown_step_fields" in codes
    assert "context_not_wired" in codes
    assert "upstream_output_shape_uncertain" in codes
    wikilink_finding = next(f for f in report.findings if f.code == "wikilink_sources_unresolved")
    assert "vault.load_sources" in wikilink_finding.suggestion


def test_readiness_ready_when_schema_and_contracts_align():
    plan = {
        "id": "critique_synthesis",
        "steps": [
            {
                "id": "synth",
                "capability": "synthesize",
                "adapter": "llm.synthesize",
                "inputs": {
                    "part_digests": "${context.part_digests}",
                    "title": "${context.title}",
                    "final_concepts": "${context.final_concepts}",
                },
            },
            {
                "id": "critique",
                "capability": "critique",
                "adapter": "llm.critique",
                "inputs": {
                    "candidate": "${steps.synth.output}",
                    "sources": "${context.source_text}",
                    "focus": "source-grounding",
                },
            },
        ],
    }
    spec = load_pipeline_from_dict(plan)
    report = assess_plan_readiness(
        spec=spec,
        plan_dict=plan,
        capabilities=[
            _cap("synthesize", expected_inputs=("part_digests",),
                 expected_context=("title", "final_concepts"), produces=("synthesis_text",)),
            _cap("critique", expected_inputs=("candidate", "sources"),
                 expected_context=("focus",), produces=("critique_findings",)),
        ],
    )

    assert report.verdict == "ready"
    assert report.score == 100
    assert report.findings == ()


def test_readiness_accepts_load_sources_to_critique_pattern():
    plan = {
        "id": "load_then_critique",
        "steps": [
            {
                "id": "load",
                "capability": "load_sources",
                "adapter": "vault.load_sources",
                "inputs": {
                    "titles": "${context.target_titles}",
                },
            },
            {
                "id": "critique",
                "capability": "critique",
                "adapter": "llm.critique",
                "when": {"var": "steps.load.source_text", "op": "nonempty"},
                "inputs": {
                    "candidate": "${context.candidate}",
                    "sources": "${steps.load.source_text}",
                    "focus": "compare differences",
                },
            },
        ],
    }
    spec = load_pipeline_from_dict(plan)
    report = assess_plan_readiness(
        spec=spec,
        plan_dict=plan,
        capabilities=[
            _cap("load_sources", expected_inputs=("titles",),
                 produces=("source_text", "sources", "missing_titles")),
            _cap("critique", expected_inputs=("candidate", "sources"),
                 expected_context=("focus",), produces=("critique_findings",)),
        ],
    )

    assert report.verdict == "ready"
    assert report.findings == ()


def test_readiness_warns_when_critique_candidate_is_instruction():
    plan = {
        "id": "bad_critique_generation",
        "steps": [
            {
                "id": "load_sources",
                "capability": "load_sources",
                "adapter": "vault.load_sources",
                "inputs": {"titles": "${context.target_titles}"},
            },
            {
                "id": "critique",
                "capability": "critique",
                "adapter": "llm.critique",
                "inputs": {
                    "candidate": "請綜合比較這兩份資料的差異，並提出批判角度與行動指引。",
                    "sources": "${steps.load_sources.source_text}",
                },
            },
        ],
    }
    spec = load_pipeline_from_dict(plan)
    report = assess_plan_readiness(
        spec=spec,
        plan_dict=plan,
        capabilities=[
            _cap("load_sources", expected_inputs=("titles",),
                 produces=("source_text", "sources", "missing_titles")),
            _cap("critique", expected_inputs=("candidate", "sources"),
                 produces=("critique_findings",)),
        ],
    )

    assert report.verdict == "needs_review"
    finding = next(f for f in report.findings if f.code == "critique_candidate_looks_like_instruction")
    assert "answer_from_sources" in finding.suggestion


def test_readiness_warns_when_source_text_feeds_part_digests():
    plan = {
        "id": "load_then_synthesize",
        "steps": [
            {
                "id": "load_sources",
                "capability": "load_sources",
                "adapter": "vault.load_sources",
                "inputs": {"titles": "${context.target_titles}"},
            },
            {
                "id": "synthesize",
                "capability": "synthesize",
                "adapter": "llm.synthesize",
                "inputs": {
                    "part_digests": "${steps.load_sources.source_text}",
                    "title": "Comparison",
                    "final_concepts": "Compare sources.",
                },
            },
        ],
    }
    spec = load_pipeline_from_dict(plan)
    report = assess_plan_readiness(
        spec=spec,
        plan_dict=plan,
        capabilities=[
            _cap("load_sources", expected_inputs=("titles",),
                 produces=("source_text", "sources", "missing_titles")),
            _cap("synthesize", expected_inputs=("part_digests",),
                 expected_context=("title", "final_concepts"), produces=("synthesis_text")),
        ],
    )

    assert report.verdict == "needs_review"
    finding = next(f for f in report.findings if f.code == "upstream_output_shape_uncertain")
    assert "part_digests" in finding.message
    assert "source_text" in finding.message
