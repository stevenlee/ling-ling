"""Shared plan-generation service for PlannerAgent and Insight planner preview.

PlannerService owns the reusable planning core:

  1. Collect registered capabilities from the live LLMClient.
  2. Ask the LLM, through the `plan` operation axis, for a JSON pipeline spec.
  3. Parse and validate the JSON against PipelineSpec.

It never writes reports, never persists sidecars, and never executes plans.
Agents decide how to present or store the validated result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.parser import extract_json_object
from services.capability_manager import CapabilitySpec
from services.pipeline_runner import (
    PipelineError,
    PipelineSpec,
    load_pipeline_from_dict,
)


@dataclass
class PlanningResult:
    status: str
    capabilities: list[CapabilitySpec]
    raw_response: str = ""
    plan_dict: dict | None = None
    spec: PipelineSpec | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.spec is not None and self.plan_dict is not None


class PlannerService:
    """Generate and validate declarative pipeline plans. No execution."""

    def __init__(self, llm) -> None:
        self.llm = llm

    def generate_plan(
        self,
        *,
        user_directive: str,
        target_titles: list[str] | None = None,
        forced_template: str | None = None,
        default_id: str = "planner_plan",
        context_note: str | None = None,
    ) -> PlanningResult:
        user_directive = (user_directive or "").strip()
        target_titles = target_titles or []

        if not user_directive:
            return PlanningResult(
                status="empty_directive",
                capabilities=[],
                error=(
                    "PlannerService received an empty user directive. "
                    "Provide some text for the planner to decompose."
                ),
            )

        capabilities = self.collect_capabilities()
        if not capabilities:
            return PlanningResult(
                status="empty_registry",
                capabilities=[],
                error=(
                    "PlannerService: no capabilities found in CapabilityManager. "
                    "Cannot plan against an empty registry."
                ),
            )

        raw_response = self.ask_llm_for_plan(
            user_directive=user_directive,
            target_titles=target_titles,
            capabilities=capabilities,
            forced_template=forced_template,
            context_note=context_note,
        )
        if not raw_response:
            return PlanningResult(
                status="empty_response",
                capabilities=capabilities,
                raw_response=raw_response,
                error="PlannerService: LLM returned an empty response. No plan was produced.",
            )

        plan_dict = extract_json_object(raw_response)
        if not plan_dict:
            return PlanningResult(
                status="no_json",
                capabilities=capabilities,
                raw_response=raw_response,
                error=(
                    "PlannerService: LLM output did not contain a JSON object. "
                    "Raw response (truncated):\n\n"
                    + raw_response[:800]
                ),
            )

        try:
            spec = load_pipeline_from_dict(plan_dict, default_id=default_id)
        except PipelineError as e:
            return PlanningResult(
                status="invalid_schema",
                capabilities=capabilities,
                raw_response=raw_response,
                plan_dict=plan_dict,
                error=(
                    "PlannerService: produced plan failed validation against the "
                    f"PipelineSpec schema.\n\n**Validation error:** {e}"
                ),
            )

        return PlanningResult(
            status="ok",
            capabilities=capabilities,
            raw_response=raw_response,
            plan_dict=plan_dict,
            spec=spec,
        )

    def ask_llm_for_plan(
        self,
        *,
        user_directive: str,
        target_titles: list[str],
        capabilities: list[CapabilitySpec],
        forced_template: str | None,
        context_note: str | None = None,
    ) -> str:
        cap_listing = self.format_capability_listing(capabilities)
        target_section = (
            "\n".join(f"- {t}" for t in target_titles)
            if target_titles else "(none)"
        )
        note_section = f"\n# Planning Context\n\n{context_note}\n\n" if context_note else ""

        user_prompt = (
            "# Available Capabilities\n\n"
            f"{cap_listing}\n\n"
            "# Execution Readiness Rules\n\n"
            f"{self.execution_readiness_rules()}\n\n"
            "# Canonical Planning Patterns\n\n"
            f"{self.canonical_planning_patterns(capabilities, target_titles)}\n\n"
            f"{note_section}"
            "# User Directive\n\n"
            f"{user_directive}\n\n"
            "# Target References (from [[wikilinks]])\n"
            f"{target_section}\n\n"
            "Produce the JSON plan now. Output ONE fenced ```json block. "
            "No prose outside the fence."
        )

        try:
            return self.llm.answer_query(
                user_prompt,
                wiki_context="",
                custom_instruction=(
                    "Produce a declarative pipeline plan (JSON) for the "
                    "user's directive. Do not execute anything."
                ),
                temperature=0.2,
                forced_template="none",
                persona="none",
                operation="plan",
            )
        except Exception as e:
            logging.error(f"PlannerService LLM call failed: {e}")
            return ""

    def collect_capabilities(self) -> list[CapabilitySpec]:
        cap_mgr = getattr(self.llm, "capability_manager", None)
        if cap_mgr is None:
            return []
        return [s for s in cap_mgr.all() if s.found]

    @staticmethod
    def execution_readiness_rules() -> str:
        """Rules the planner sees so preview plans are closer to executable."""
        return (
            "- The only supported top-level fields are `id`, `description`, "
            "`summary`, and `steps`.\n"
            "- The only supported step fields are `id`, `capability`, `adapter`, "
            "`inputs`, `when`, and `rationale`. Do not emit step-level `context`; "
            "PipelineRunner will ignore it.\n"
            "- Every value an adapter needs must be placed under step `inputs`. "
            "Capability `expected_context` keys should also be passed under `inputs` "
            "when needed.\n"
            "- Do not use `critique` to generate the user's final answer. `critique` "
            "evaluates an existing candidate. For final answers from loaded sources, "
            "prefer `answer_from_sources` when available.\n"
            "- Do not pass bare wikilinks as `sources`. Use loaded source text from "
            "`${context.source_text}`. If the `load_sources` capability is available, "
            "add a step with `adapter: vault.load_sources` and pass "
            "`${steps.<load_step_id>.source_text}` downstream.\n"
            "- `llm.synthesize` accepts inputs `title`, `part_digests`, "
            "`final_concepts`, and optional `template`; keep all of them under `inputs`.\n"
            "- `llm.answer_from_sources` accepts inputs `query`, `sources`, and optional "
            "`focus`; use it to produce a final source-grounded response.\n"
            "- `llm.critique` accepts inputs `candidate`, `sources`, and optional "
            "`focus`; keep all of them under `inputs`.\n"
            "- Avoid feeding `${steps.<id>.output}` into semantically different "
            "inputs unless the upstream output really matches the downstream input "
            "contract. For example, critique findings are not part digests."
        )

    @staticmethod
    def canonical_planning_patterns(
        capabilities: list[CapabilitySpec],
        target_titles: list[str] | None = None,
    ) -> str:
        cap_names = {c.name for c in capabilities}
        lines: list[str] = []
        if {"load_sources", "digest_sources", "answer_from_sources"}.issubset(cap_names) and target_titles and len(target_titles) >= 2:
            lines.extend([
                "## Pattern: load vault sources, digest per-source, then answer from digests",
                "",
                "Use this when multiple Target References are present (>= 2) to perform per-source compression",
                "and ensure balanced coverage across all sources in the final answer.",
                "",
                "```json",
                "{",
                '  "id": "load_digest_answer",',
                '  "description": "Load sources, digest per-source, then answer from digests.",',
                '  "summary": "Loads multiple wikilink targets into source_text, digests each source individually using llm.digest_sources, and composes a final answer using the compressed digest_text.",',
                '  "steps": [',
                "    {",
                '      "id": "load_sources",',
                '      "capability": "load_sources",',
                '      "adapter": "vault.load_sources",',
                '      "inputs": {"titles": "${context.target_titles}"},',
                '      "rationale": "Resolve target wikilinks into real markdown source text."'
                "    },",
                "    {",
                '      "id": "digest_sources",',
                '      "capability": "digest_sources",',
                '      "adapter": "llm.digest_sources",',
                '      "inputs": {',
                '        "query": "${context.user_directive}",',
                '        "sources": "${steps.load_sources.source_text}"',
                '      },',
                '      "when": {"var": "steps.load_sources.source_text", "op": "nonempty"},',
                '      "rationale": "Perform per-source digesting to fit context and maintain balanced coverage."'
                "    },",
                "    {",
                '      "id": "answer",',
                '      "capability": "answer_from_sources",',
                '      "adapter": "llm.answer_from_sources",',
                '      "inputs": {',
                '        "query": "${context.user_directive}",',
                '        "sources": "${steps.digest_sources.digest_text}"',
                '      },',
                '      "when": {"var": "steps.digest_sources.digest_text", "op": "nonempty"},',
                '      "rationale": "Produce the final source-grounded answer using the balanced digests."'
                "    }",
                "  ]",
                "}",
                "```",
            ])
        elif {"load_sources", "answer_from_sources"}.issubset(cap_names):
            target_hint = (
                "Use this when Target References are present."
                if target_titles else
                "Use this when the directive references vault wikilinks."
            )
            lines.extend([
                "## Pattern: load vault sources before final answer",
                "",
                target_hint,
                "",
                "```json",
                "{",
                '  "id": "load_sources_then_answer",',
                '  "description": "Load referenced vault sources, then write the final source-grounded answer.",',
                '  "summary": "Loads wikilink targets into source_text before composing the requested comparison, critique angles, and action guidance.",',
                '  "steps": [',
                "    {",
                '      "id": "load_sources",',
                '      "capability": "load_sources",',
                '      "adapter": "vault.load_sources",',
                '      "inputs": {"titles": "${context.target_titles}"},',
                '      "rationale": "Resolve target wikilinks into real markdown source text."',
                "    },",
                "    {",
                '      "id": "answer",',
                '      "capability": "answer_from_sources",',
                '      "adapter": "llm.answer_from_sources",',
                '      "when": {"var": "steps.load_sources.source_text", "op": "nonempty"},',
                '      "inputs": {',
                '        "query": "${context.user_directive}",',
                '        "sources": "${steps.load_sources.source_text}",',
                '        "focus": "${context.focus}"',
                '      },',
                '      "rationale": "Produce the final source-grounded answer directly."',
                "    }",
                "  ]",
                "}",
                "```",
            ])
        elif {"load_sources", "critique"}.issubset(cap_names):
            lines.extend([
                "## Pattern: load vault sources before critique",
                "",
                "Use this only when an upstream step or user context provides a real candidate text to evaluate. Do not use critique to generate the final answer.",
            ])

        if not lines:
            return "(none)"
        return "\n".join(lines)

    @staticmethod
    def format_capability_listing(capabilities: list[CapabilitySpec]) -> str:
        operations = [c for c in capabilities if c.type == "operation"]
        skills = [c for c in capabilities if c.type == "skill"]
        others = [c for c in capabilities if c.type not in ("operation", "skill")]

        lines: list[str] = []
        if operations:
            lines.append("## Operations (methodology prompts; pick these for direct steps)")
            lines.append("")
            for c in operations:
                lines.append(PlannerService.format_capability_entry(c))
            lines.append("")
        if skills:
            lines.append("## Skills (insight strategies; usually consumed by InsightAgent — adapters may not yet exist)")
            lines.append("")
            for c in skills:
                lines.append(PlannerService.format_capability_entry(c))
            lines.append("")
        if others:
            lines.append("## Other")
            lines.append("")
            for c in others:
                lines.append(PlannerService.format_capability_entry(c))
            lines.append("")
        return "\n".join(lines).rstrip()

    @staticmethod
    def format_capability_entry(c: CapabilitySpec) -> str:
        parts = [f"- **{c.name}** ({c.cost_class}): {c.description or '(no description)'}"]
        if c.name == "load_sources":
            parts.append("  - adapter: vault.load_sources")
        if c.name == "answer_from_sources":
            parts.append("  - adapter: llm.answer_from_sources")
        if c.expected_inputs:
            parts.append(f"  - expected_inputs: {list(c.expected_inputs)}")
        if c.expected_context:
            parts.append(f"  - expected_context: {list(c.expected_context)}")
        if c.produces:
            parts.append(f"  - produces: {list(c.produces)}")
        return "\n".join(parts)

    @staticmethod
    def resolve_plan_capabilities(
        spec: PipelineSpec,
        capabilities: list[CapabilitySpec],
    ) -> dict:
        by_name = {c.name: c for c in capabilities}
        return {
            "steps": [
                {
                    "step_id": s.id,
                    "capability": s.capability,
                    "registered": s.capability in by_name,
                    "adapter": s.adapter,
                }
                for s in spec.steps
            ],
        }
