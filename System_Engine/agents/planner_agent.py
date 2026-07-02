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
from pathlib import Path

from agents.base_agent import BaseAgent
from core.config import PLANS_DIR
from core.ui import ui
from services.capability_manager import CapabilitySpec
from services.plan_readiness import assess_plan_readiness
from services.pipeline_runner import (
    PipelineSpec,
)
from services.planner_service import PlannerService


class PlannerAgent(BaseAgent):
    """Produces declarative pipeline plans. Never executes them."""

    ERROR_LABEL = "Planner Error"
    ERROR_REPORT_TYPE = "ins-plan"
    ERROR_META = {"error": True}
    ERROR_STATUS = "🎐 Planner 失敗：{msg}"

    def execute(self, task_context: dict) -> str:
        user_directive = (task_context.get("user_directive") or "").strip()
        target_titles = task_context.get("target_titles") or []
        forced_template = task_context.get("forced_template")

        if not user_directive:
            return self._error_report(
                "PlannerAgent received an empty user directive. "
                "Provide some text after the @ling-plan command."
            )

        ui.set_status(f"🎐 Planner 正在規劃：{user_directive[:60]}…")

        result = PlannerService(self.llm).generate_plan(
            user_directive=user_directive,
            target_titles=target_titles,
            forced_template=forced_template,
        )
        if not result.ok:
            message = result.error or "PlannerAgent: planning failed."
            if result.status == "empty_registry":
                message = message.replace("PlannerService", "PlannerAgent")
            elif result.status in {"empty_response", "no_json", "invalid_schema"}:
                message = message.replace("PlannerService", "PlannerAgent")
            if result.status == "invalid_schema" and result.plan_dict is not None:
                message += (
                    "\n\n**Raw JSON the planner produced:**\n\n"
                    "```json\n"
                    f"{json.dumps(result.plan_dict, indent=2, ensure_ascii=False)}\n"
                    "```"
                )
            return self._error_report(message)

        spec = result.spec
        plan_dict = result.plan_dict
        capabilities = result.capabilities

        # Persist the validated plan as a sidecar JSON so @ling-do can
        # load it later by plan_id. The markdown report is for humans;
        # this JSON is the executable hand-off.
        sidecar_path = self._write_sidecar(spec.id, plan_dict)
        readiness = assess_plan_readiness(
            spec=spec,
            plan_dict=plan_dict,
            capabilities=capabilities,
            target_titles=target_titles,
        )

        # Build report and write it. NO execution.
        report = self._render_plan_report(
            spec, plan_dict, user_directive, capabilities, readiness=readiness
        )

        meta = {
            "target_titles": target_titles,
            "user_directive": user_directive,
            "plan_id": spec.id,
            "step_count": len(spec.steps),
            "plan_json": plan_dict,
            "plan_sidecar": str(sidecar_path),
            "readiness_verdict": readiness.verdict,
            "readiness_score": readiness.score,
            "readiness_findings": [
                {
                    "severity": f.severity,
                    "code": f.code,
                    "step_id": f.step_id,
                    "message": f.message,
                    "suggestion": f.suggestion,
                }
                for f in readiness.findings
            ],
            "capability_resolution": self._resolve_plan_capabilities(spec, capabilities),
        }
        title = f"{spec.description or spec.id}"
        self._write_report(title, report, "ins-plan", meta)
        ui.success(
            f"🎐 Planner 完成：{spec.id}（{len(spec.steps)} 個步驟） → 用 `@ling-do {spec.id}` 執行"
        )
        return report

    @staticmethod
    def _write_sidecar(plan_id: str, plan_dict: dict) -> Path:
        """Persist the plan JSON for downstream @ling-do execution."""
        PLANS_DIR.mkdir(parents=True, exist_ok=True)
        path = PLANS_DIR / f"{plan_id}.json"
        path.write_text(
            json.dumps(plan_dict, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    # ── LLM call ───────────────────────────────────────────────────────

    def _ask_llm_for_plan(
        self,
        *,
        user_directive: str,
        target_titles: list[str],
        capabilities: list[CapabilitySpec],
        forced_template: str | None,
    ) -> str:
        return PlannerService(self.llm).ask_llm_for_plan(
            user_directive=user_directive,
            target_titles=target_titles,
            capabilities=capabilities,
            forced_template=forced_template,
        )

    # ── Capability listing ────────────────────────────────────────────

    def _collect_capabilities(self) -> list[CapabilitySpec]:
        return PlannerService(self.llm).collect_capabilities()

    @staticmethod
    def _format_capability_listing(capabilities: list[CapabilitySpec]) -> str:
        return PlannerService.format_capability_listing(capabilities)

    @staticmethod
    def _format_capability_entry(c: CapabilitySpec) -> str:
        return PlannerService.format_capability_entry(c)

    # ── Resolution record (for trace metadata) ─────────────────────────

    @staticmethod
    def _resolve_plan_capabilities(
        spec: PipelineSpec,
        capabilities: list[CapabilitySpec],
    ) -> dict:
        """Per-step capability lookup status, lands in trace metadata."""
        return PlannerService.resolve_plan_capabilities(spec, capabilities)

    # ── Report rendering ──────────────────────────────────────────────

    def _render_plan_report(
        self,
        spec: PipelineSpec,
        plan_dict: dict,
        user_directive: str,
        capabilities: list[CapabilitySpec],
        readiness=None,
    ) -> str:
        cap_names = {c.name for c in capabilities}
        unknown_caps = [s.capability for s in spec.steps if s.capability not in cap_names]

        summary = (plan_dict.get("summary") or "").strip() or "(planner did not provide a summary)"

        lines: list[str] = [
            f"# 🎐 Plan: {spec.description or spec.id}",
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

        if readiness is not None:
            lines.extend(self._render_readiness_section(readiness))

        for idx, step in enumerate(spec.steps, 1):
            step_raw = self._raw_step(plan_dict, step.id)
            rationale = (step_raw.get("rationale") or "").strip()
            lines.append(f"### Step {idx}: `{step.id}`")
            lines.append(
                f"- **Capability**: `{step.capability}`"
                + (" 💦 NOT in registry" if step.capability not in cap_names else "")
            )
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
            lines.append("## 💦 Plan References Unregistered Capabilities")
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
    def _render_readiness_section(readiness) -> list[str]:
        verdict_label = {
            "ready": "Ready for guarded execution",
            "needs_review": "Needs review",
            "blocked": "Blocked",
        }.get(readiness.verdict, readiness.verdict)

        lines = [
            "## Readiness Check",
            "",
            f"- **Verdict**: `{readiness.verdict}` ({verdict_label})",
            f"- **Score**: `{readiness.score}/100`",
            "",
        ]
        if not readiness.findings:
            lines.extend(["No readiness issues detected by static checks.", ""])
            return lines

        for finding in readiness.findings:
            scope = f"step `{finding.step_id}`" if finding.step_id else "plan"
            lines.append(
                f"- **{finding.severity.upper()} `{finding.code}`** ({scope}): {finding.message}"
            )
            if finding.suggestion:
                lines.append(f"  - Suggestion: {finding.suggestion}")
        lines.append("")
        return lines

    @staticmethod
    def _raw_step(plan_dict: dict, step_id: str) -> dict:
        for step in plan_dict.get("steps") or []:
            if isinstance(step, dict) and step.get("id") == step_id:
                return step
        return {}

    # ── Error path ─────────────────────────────────────────────────────
