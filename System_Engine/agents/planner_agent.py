"""Phase 5B PlannerAgent — plan-only, no execution.

Reads CapabilityManager registry, asks the LLM (via the `plan` Operation
prompt) to decompose the user's directive into a pipeline-spec-shaped
JSON. Validates the JSON via `load_pipeline_from_dict` so any structural
problem surfaces here, before Phase 5C's `@ling-do` would execute it.

The agent NEVER runs the plan. Output is a markdown report containing:
  1. A human-readable summary + per-step rationale.
  2. The validated plan JSON in a fenced code block.

Phase 5C will read this JSON (or accept it directly from PlannerAgent)
and feed it to PipelineRunner.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agents.base_agent import BaseAgent
from core.parser import extract_json_object
from core.ui import ui
from services.capability_manager import CapabilitySpec
from services.pipeline_runner import (
    PipelineError,
    PipelineSpec,
    load_pipeline_from_dict,
)


class PlannerAgent(BaseAgent):
    """Produces declarative pipeline plans. Never executes them."""

    def execute(self, task_context: dict) -> str:
        user_directive = (task_context.get("user_directive") or "").strip()
        target_titles = task_context.get("target_titles") or []
        forced_template = task_context.get("forced_template")

        if not user_directive:
            return self._error_report(
                "PlannerAgent received an empty user directive. "
                "Provide some text after the @ling-plan command."
            )

        capabilities = self._collect_capabilities()
        if not capabilities:
            return self._error_report(
                "PlannerAgent: no capabilities found in CapabilityManager. "
                "Cannot plan against an empty registry."
            )

        ui.set_status(f"🧠 Planner 正在規劃：{user_directive[:60]}…")

        raw_response = self._ask_llm_for_plan(
            user_directive=user_directive,
            target_titles=target_titles,
            capabilities=capabilities,
            forced_template=forced_template,
        )
        if not raw_response:
            return self._error_report(
                "PlannerAgent: LLM returned an empty response. "
                "No plan was produced."
            )

        plan_dict = extract_json_object(raw_response)
        if not plan_dict:
            return self._error_report(
                "PlannerAgent: LLM output did not contain a JSON object. "
                "Raw response (truncated):\n\n"
                + raw_response[:800]
            )

        try:
            spec = load_pipeline_from_dict(plan_dict, default_id="planner_plan")
        except PipelineError as e:
            return self._error_report(
                "PlannerAgent: produced plan failed validation against the "
                f"PipelineSpec schema.\n\n**Validation error:** {e}\n\n"
                "**Raw JSON the planner produced:**\n\n"
                "```json\n"
                f"{json.dumps(plan_dict, indent=2, ensure_ascii=False)}\n"
                "```"
            )

        # Build report and write it. NO execution.
        report = self._render_plan_report(spec, plan_dict, user_directive, capabilities)

        meta = {
            "target_titles": target_titles,
            "user_directive": user_directive,
            "plan_id": spec.id,
            "step_count": len(spec.steps),
            "plan_json": plan_dict,
            "capability_resolution": self._resolve_plan_capabilities(spec, capabilities),
        }
        title = f"Plan: {spec.description or spec.id}"
        self._write_report(title, report, "planner_plan", meta)
        ui.success(f"🧠 Planner 完成：{spec.id}（{len(spec.steps)} 個步驟，未執行）")
        return report

    # ── LLM call ───────────────────────────────────────────────────────

    def _ask_llm_for_plan(
        self,
        *,
        user_directive: str,
        target_titles: list[str],
        capabilities: list[CapabilitySpec],
        forced_template: str | None,
    ) -> str:
        cap_listing = self._format_capability_listing(capabilities)

        target_section = (
            "\n".join(f"- {t}" for t in target_titles)
            if target_titles else "(none)"
        )

        user_prompt = (
            "# Available Capabilities\n\n"
            f"{cap_listing}\n\n"
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
            logging.error(f"PlannerAgent LLM call failed: {e}")
            return ""

    # ── Capability listing ────────────────────────────────────────────

    def _collect_capabilities(self) -> list[CapabilitySpec]:
        cap_mgr = getattr(self.llm, "capability_manager", None)
        if cap_mgr is None:
            return []
        return [s for s in cap_mgr.all() if s.found]

    @staticmethod
    def _format_capability_listing(capabilities: list[CapabilitySpec]) -> str:
        operations = [c for c in capabilities if c.type == "operation"]
        skills     = [c for c in capabilities if c.type == "skill"]
        others     = [c for c in capabilities if c.type not in ("operation", "skill")]

        lines: list[str] = []
        if operations:
            lines.append("## Operations (methodology prompts; pick these for direct steps)")
            lines.append("")
            for c in operations:
                lines.append(PlannerAgent._format_capability_entry(c))
            lines.append("")
        if skills:
            lines.append("## Skills (insight strategies; usually consumed by InsightAgent — adapters may not yet exist)")
            lines.append("")
            for c in skills:
                lines.append(PlannerAgent._format_capability_entry(c))
            lines.append("")
        if others:
            lines.append("## Other")
            lines.append("")
            for c in others:
                lines.append(PlannerAgent._format_capability_entry(c))
            lines.append("")
        return "\n".join(lines).rstrip()

    @staticmethod
    def _format_capability_entry(c: CapabilitySpec) -> str:
        parts = [f"- **{c.name}** ({c.cost_class}): {c.description or '(no description)'}"]
        if c.expected_inputs:
            parts.append(f"  - expected_inputs: {list(c.expected_inputs)}")
        if c.expected_context:
            parts.append(f"  - expected_context: {list(c.expected_context)}")
        if c.produces:
            parts.append(f"  - produces: {list(c.produces)}")
        return "\n".join(parts)

    # ── Resolution record (for trace metadata) ─────────────────────────

    @staticmethod
    def _resolve_plan_capabilities(
        spec: PipelineSpec,
        capabilities: list[CapabilitySpec],
    ) -> dict:
        """Per-step capability lookup status, lands in trace metadata."""
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

    # ── Report rendering ──────────────────────────────────────────────

    def _render_plan_report(
        self,
        spec: PipelineSpec,
        plan_dict: dict,
        user_directive: str,
        capabilities: list[CapabilitySpec],
    ) -> str:
        cap_names = {c.name for c in capabilities}
        unknown_caps = [s.capability for s in spec.steps if s.capability not in cap_names]

        summary = (plan_dict.get("summary") or "").strip() or "(planner did not provide a summary)"

        lines: list[str] = [
            f"# 🧠 Plan: {spec.description or spec.id}",
            "",
            "> [!IMPORTANT]",
            "> This is a **plan only**. No steps have been executed. "
            "Phase 5C's `@ling-do <plan_id>` will execute approved plans.",
            "",
            "## 📝 Summary",
            "",
            summary,
            "",
            "## 📂 User Directive",
            "",
            "```",
            user_directive,
            "```",
            "",
            "## 🪜 Planned Steps",
            "",
        ]

        for idx, step in enumerate(spec.steps, 1):
            step_raw = self._raw_step(plan_dict, step.id)
            rationale = (step_raw.get("rationale") or "").strip()
            lines.append(f"### Step {idx}: `{step.id}`")
            lines.append(f"- **Capability**: `{step.capability}`"
                         + (" ⚠️ NOT in registry" if step.capability not in cap_names else ""))
            lines.append(f"- **Adapter**: `{step.adapter}`")
            if step.when:
                lines.append(f"- **When**: `{step.when}`")
            if step.inputs:
                lines.append("- **Inputs**:")
                for k, v in step.inputs.items():
                    lines.append(f"  - `{k}`: `{v}`")
            if rationale:
                lines.append("")
                lines.append(f"> {rationale}")
            lines.append("")

        if unknown_caps:
            lines.append("## ⚠️ Plan References Unregistered Capabilities")
            lines.append("")
            lines.append(
                "The planner produced steps that reference capabilities not in "
                "the registry. These will fail validation when (eventually) "
                "executed by `@ling-do`. List:"
            )
            for cap in unknown_caps:
                lines.append(f"- `{cap}`")
            lines.append("")

        lines.append("## 📋 Raw Plan JSON")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(plan_dict, indent=2, ensure_ascii=False))
        lines.append("```")

        return "\n".join(lines)

    @staticmethod
    def _raw_step(plan_dict: dict, step_id: str) -> dict:
        for step in plan_dict.get("steps") or []:
            if isinstance(step, dict) and step.get("id") == step_id:
                return step
        return {}

    # ── Error path ─────────────────────────────────────────────────────

    def _error_report(self, message: str) -> str:
        body = f"# ❌ Planner Error\n\n{message}\n"
        self._write_report("Planner Error", body, "planner_plan",
                            {"error": True})
        ui.error(f"🧠 Planner 失敗：{message[:120]}")
        return body
