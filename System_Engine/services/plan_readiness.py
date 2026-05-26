"""Readiness diagnostics for planner-produced PipelineSpec drafts.

The PipelineSpec schema answers "is this structurally parseable?". This module
answers the next question: "would this plan probably execute meaningfully?".
It is intentionally advisory for planner preview and guarded execution reports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from services.builtin_adapters import builtin_adapter_names
from services.capability_manager import CapabilitySpec
from services.pipeline_runner import PipelineSpec


_PLACEHOLDER_RE = re.compile(r"^\$\{steps\.([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)\}$")
_WIKILINK_RE = re.compile(r"^\[\[.+\]\]$")

_ALLOWED_TOP_LEVEL_FIELDS = frozenset({"id", "description", "summary", "steps"})
_ALLOWED_STEP_FIELDS = frozenset({
    "id", "capability", "adapter", "inputs", "when", "rationale",
})


@dataclass(frozen=True)
class ReadinessFinding:
    severity: str
    code: str
    step_id: str | None
    message: str
    suggestion: str = ""


@dataclass(frozen=True)
class ReadinessReport:
    verdict: str
    findings: tuple[ReadinessFinding, ...]

    @property
    def score(self) -> int:
        penalties = {"error": 35, "warning": 15, "info": 3}
        total = sum(penalties.get(f.severity, 0) for f in self.findings)
        return max(0, 100 - total)


def assess_plan_readiness(
    *,
    spec: PipelineSpec,
    plan_dict: dict,
    capabilities: list[CapabilitySpec],
    registered_adapters: set[str] | None = None,
    target_titles: list[str] | None = None,
) -> ReadinessReport:
    """Return advisory diagnostics for a validated plan."""
    registered_adapters = registered_adapters or set(builtin_adapter_names())
    cap_by_name = {c.name: c for c in capabilities}
    step_by_id = {s.id: s for s in spec.steps}
    raw_step_by_id = _raw_steps_by_id(plan_dict)

    findings: list[ReadinessFinding] = []
    findings.extend(_unknown_top_level_fields(plan_dict))

    for step in spec.steps:
        raw_step = raw_step_by_id.get(step.id, {})
        cap = cap_by_name.get(step.capability)

        findings.extend(_unknown_step_fields(raw_step, step.id))

        if cap is None:
            findings.append(ReadinessFinding(
                "error", "unknown_capability", step.id,
                f"Capability `{step.capability}` is not registered.",
                "Re-plan using only capabilities listed in CapabilityManager.",
            ))
        if step.adapter not in registered_adapters:
            findings.append(ReadinessFinding(
                "error", "unregistered_adapter", step.id,
                f"Adapter `{step.adapter}` is not allowlisted for execution.",
                "Add an explicit built-in adapter before enabling execution.",
            ))

        if cap is not None:
            findings.extend(_missing_expected_inputs(step.id, step.inputs, cap))
            findings.extend(_expected_context_not_wired(step.id, step.inputs, raw_step, cap))

        findings.extend(_unresolved_wikilink_sources(step.id, step.inputs))
        findings.extend(_critique_candidate_misuse(step.id, step.capability, step.inputs))
        findings.extend(_upstream_output_shape_warnings(
            step_id=step.id,
            inputs=step.inputs,
            expected_inputs=cap.expected_inputs if cap else (),
            step_by_id=step_by_id,
            cap_by_name=cap_by_name,
        ))

    findings.extend(_check_multi_source_digest_rules(spec, cap_by_name, target_titles))

    if any(f.severity == "error" for f in findings):
        verdict = "blocked"
    elif any(f.severity == "warning" for f in findings):
        verdict = "needs_review"
    else:
        verdict = "ready"
    return ReadinessReport(verdict=verdict, findings=tuple(findings))


def _raw_steps_by_id(plan_dict: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for raw in plan_dict.get("steps") or []:
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            out[raw["id"]] = raw
    return out


def _unknown_top_level_fields(plan_dict: dict) -> list[ReadinessFinding]:
    unknown = sorted(set(plan_dict) - _ALLOWED_TOP_LEVEL_FIELDS)
    if not unknown:
        return []
    return [ReadinessFinding(
        "info", "unknown_top_level_fields", None,
        f"Top-level fields will be ignored by the runner: {unknown}.",
        "Move runtime data into step `inputs` or supported schema fields.",
    )]


def _unknown_step_fields(raw_step: dict, step_id: str) -> list[ReadinessFinding]:
    unknown = sorted(set(raw_step) - _ALLOWED_STEP_FIELDS)
    if not unknown:
        return []
    return [ReadinessFinding(
        "warning", "unknown_step_fields", step_id,
        f"Step contains unsupported fields that PipelineRunner will ignore: {unknown}.",
        "Move these values under `inputs` if the adapter needs them.",
    )]


def _missing_expected_inputs(
    step_id: str,
    inputs: dict[str, Any],
    cap: CapabilitySpec,
) -> list[ReadinessFinding]:
    missing = [name for name in cap.expected_inputs if name not in inputs]
    if not missing:
        return []
    return [ReadinessFinding(
        "warning", "missing_expected_inputs", step_id,
        f"Capability `{cap.name}` expects inputs not present in step inputs: {missing}.",
        "Add the missing keys under `inputs` or make the adapter contract explicit.",
    )]


def _expected_context_not_wired(
    step_id: str,
    inputs: dict[str, Any],
    raw_step: dict,
    cap: CapabilitySpec,
) -> list[ReadinessFinding]:
    missing_context = [name for name in cap.expected_context if name not in inputs]
    if not missing_context:
        return []
    raw_context = raw_step.get("context")
    if isinstance(raw_context, dict) and any(name in raw_context for name in missing_context):
        return [ReadinessFinding(
            "warning", "context_not_wired", step_id,
            f"Capability `{cap.name}` context keys are present under unsupported `context`: {missing_context}.",
            "PipelineRunner passes only `inputs` to adapters; move these keys into `inputs`.",
        )]
    return [ReadinessFinding(
        "info", "missing_expected_context", step_id,
        f"Capability `{cap.name}` declares contextual keys not wired in inputs: {missing_context}.",
        "If the adapter needs them, pass them under `inputs`.",
    )]


def _unresolved_wikilink_sources(
    step_id: str,
    inputs: dict[str, Any],
) -> list[ReadinessFinding]:
    sources = inputs.get("sources")
    if isinstance(sources, list) and any(isinstance(s, str) and _WIKILINK_RE.match(s) for s in sources):
        return [ReadinessFinding(
            "warning", "wikilink_sources_unresolved", step_id,
            "`sources` contains wikilinks, not loaded source text.",
            "Add a prior `load_sources` step using `adapter: vault.load_sources`, then pass `${steps.load_sources.source_text}` into `sources`.",
        )]
    if isinstance(sources, str) and _WIKILINK_RE.match(sources):
        return [ReadinessFinding(
            "warning", "wikilink_sources_unresolved", step_id,
            "`sources` is a wikilink, not loaded source text.",
            "Add a prior `load_sources` step using `adapter: vault.load_sources`, then pass `${steps.load_sources.source_text}` into `sources`.",
        )]
    return []


def _critique_candidate_misuse(
    step_id: str,
    capability: str,
    inputs: dict[str, Any],
) -> list[ReadinessFinding]:
    if capability != "critique":
        return []
    candidate = inputs.get("candidate")
    if not isinstance(candidate, str):
        return []
    if candidate.startswith("${"):
        return []
    instruction_markers = (
        "請", "please", "compare", "比較", "提出", "生成", "產出",
        "action guide", "行動指引", "批判角度",
    )
    lowered = candidate.lower()
    if any(marker in lowered for marker in instruction_markers):
        return [ReadinessFinding(
            "warning", "critique_candidate_looks_like_instruction", step_id,
            "`critique.candidate` looks like a task instruction, not a candidate answer.",
            "Use `answer_from_sources` to generate the final answer, or feed critique a real upstream candidate output.",
        )]
    return []


def _upstream_output_shape_warnings(
    *,
    step_id: str,
    inputs: dict[str, Any],
    expected_inputs: tuple[str, ...],
    step_by_id: dict,
    cap_by_name: dict[str, CapabilitySpec],
) -> list[ReadinessFinding]:
    findings: list[ReadinessFinding] = []
    for input_name, value in inputs.items():
        if input_name not in expected_inputs:
            continue
        if not isinstance(value, str):
            continue
        match = _PLACEHOLDER_RE.match(value)
        if not match:
            continue
        upstream_id, output_key = match.groups()
        upstream_step = step_by_id.get(upstream_id)
        if upstream_step is None:
            continue
        upstream_cap = cap_by_name.get(upstream_step.capability)
        produced = set(upstream_cap.produces if upstream_cap else ())
        if output_key == "output" and produced:
            expected_signal = _expected_signal(input_name)
            if expected_signal and expected_signal not in produced:
                findings.append(ReadinessFinding(
                    "warning", "upstream_output_shape_uncertain", step_id,
                    (
                        f"Input `{input_name}` receives `${{steps.{upstream_id}.output}}`, "
                        f"but upstream capability `{upstream_step.capability}` produces {sorted(produced)}."
                    ),
                    "Insert a transform/synthesis step or align the adapter contract before execution.",
                ))
        elif output_key != "output" and input_name == "part_digests":
            if output_key != "part_digests":
                findings.append(ReadinessFinding(
                    "warning", "upstream_output_shape_uncertain", step_id,
                    (
                        f"Input `part_digests` receives `${{steps.{upstream_id}.{output_key}}}`, "
                        "which is not a structured part-digest output."
                    ),
                    "Use structured part digests or add an adapter that converts source text into digest-shaped inputs.",
                ))
    return findings


def _expected_signal(input_name: str) -> str | None:
    if input_name == "part_digests":
        return "part_digests"
    if input_name == "candidate":
        return "synthesis_text"
    if input_name == "sources":
        return "source_text"
    return None


def _check_multi_source_digest_rules(
    spec: PipelineSpec,
    cap_by_name: dict[str, CapabilitySpec],
    target_titles: list[str] | None = None,
) -> list[ReadinessFinding]:
    findings = []
    
    # 1. Find if we have a load_sources step that loads multiple titles
    load_step_id = None
    has_multi_titles = False
    for step in spec.steps:
        if step.capability == "load_sources":
            load_step_id = step.id
            titles = step.inputs.get("titles") or step.inputs.get("target_titles")
            if titles == "${context.target_titles}":
                if target_titles is not None:
                    has_multi_titles = len(target_titles) >= 2
                else:
                    has_multi_titles = True
            elif isinstance(titles, list) and len(titles) >= 2:
                has_multi_titles = True
            break

    if not has_multi_titles or not load_step_id:
        return []

    # Check if there is any digest step in the plan
    has_digest_step = any(step.capability == "digest_sources" for step in spec.steps)

    # Check if the final answer (or any answer_from_sources step) feeds directly from load_sources.source_text
    feeds_directly_to_answer = False
    answer_step_id = None
    for step in spec.steps:
        if step.capability == "answer_from_sources":
            answer_step_id = step.id
            sources_input = step.inputs.get("sources") or step.inputs.get("source_text")
            if isinstance(sources_input, str) and f"${{steps.{load_step_id}.source_text}}" in sources_input:
                feeds_directly_to_answer = True
                break

    # Warning 1: Multi-source feed directly to answer without digest
    if feeds_directly_to_answer and not has_digest_step:
        findings.append(ReadinessFinding(
            severity="warning",
            code="multi_source_no_digest",
            step_id=answer_step_id,
            message=f"Multiple sources from `{load_step_id}` feed directly to final answer `{answer_step_id}` without a digest step.",
            suggestion="Insert a `digest_sources` step using `adapter: llm.digest_sources` to ensure balanced source coverage.",
        ))

    # Warning 2: digest_sources capability exists, but plan skipped it
    has_digest_cap = "digest_sources" in cap_by_name
    if has_multi_titles and has_digest_cap and not has_digest_step:
        findings.append(ReadinessFinding(
            severity="warning",
            code="digest_skipped",
            step_id=None,
            message="Multiple sources are referenced and `digest_sources` capability is available, but no digest step is included in the plan.",
            suggestion="Use the canonical three-step planning pattern (load_digest_answer) to summarize sources before answering.",
        ))

    return findings
