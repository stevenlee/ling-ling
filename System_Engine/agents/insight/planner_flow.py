"""Planner preview / gated execution and all its report rendering.

Moved verbatim from agents/insight_agent.py (P2f). Mixin: methods keep
running on the InsightAgent instance (self.llm / self.rag / self.strategies /
self.insights_dir), so tests and behavior are unchanged.
"""

from __future__ import annotations

from typing import Any

import json
import re

from services.builtin_adapters import builtin_adapter_names, register_builtin_adapters
from services.plan_readiness import assess_plan_readiness
from services.pipeline_runner import (
    AdapterRegistry,
    PipelineError,
    PipelineRunner,
    PipelineRunResult,
    PipelineSpec,
)
from services.planner_service import PlannerService

from agents.insight.common import (
    _PLANNER_EXECUTE_MAX_STEPS,
)


class PlannerFlowMixin:
    def _planner_service(self) -> PlannerService:
        """Lazily built + cached per agent instance. Lazy (not __init__) so
        tests that build InsightAgent skeletons via __new__ still work."""
        ps = getattr(self, "_planner_service_cache", None)
        if ps is None:
            ps = self._planner_service_cache = PlannerService(self.llm)
        return ps

    def _pipeline_runner(self) -> PipelineRunner:
        """Sandbox runner: the registry contains ONLY the built-in adapters,
        plus whatever the LLMClient instance exposes. Cached per instance."""
        runner = getattr(self, "_pipeline_runner_cache", None)
        if runner is None:
            registry = AdapterRegistry()
            register_builtin_adapters(registry, self.llm)
            runner = self._pipeline_runner_cache = PipelineRunner(
                capability_manager=self.llm.capability_manager,
                adapter_registry=registry,
                trace_store=getattr(self.llm, "trace_store", None),
            )
        return runner

    # Contract: provided by the composed InsightAgent (BaseAgent state +
    # sibling mixins). Declared so each mixin documents what it needs.
    llm: Any
    rag: Any
    strategies: dict
    _write_report: Any
    _mirror_to_insights: Any

    def _run_planner_preview(
        self,
        *,
        user_directive: str,
        target_titles: list[str],
        forced_template: str | None = None,
        execute_plan: bool = False,
    ) -> str:
        """Phase 6A: preview a validated plan for Insight, never execute it."""
        from core.ui import ui

        ui.set_status(
            "🎐 Insight Planner 正在規劃並準備執行..."
            if execute_plan
            else "🎐 Insight Planner 正在規劃預覽..."
        )

        context_note = (
            "This request came from @ling-insight planner-mode. "
            "Generate a plan suitable for an Insight report, but do not execute it. "
            "Prefer registered capabilities with explicit adapters and keep the plan short."
        )
        result = self._planner_service().generate_plan(
            user_directive=user_directive,
            target_titles=target_titles,
            forced_template=forced_template,
            default_id="insight_planner_preview",
            context_note=context_note,
        )

        if result.ok:
            # PlannerService contract: ok=True implies a parsed spec.
            assert result.spec is not None
            readiness = assess_plan_readiness(
                spec=result.spec,
                plan_dict=result.plan_dict or {},
                capabilities=result.capabilities,
                target_titles=target_titles,
            )
            execution_result = None
            execution_blocker = None
            execute_context = self._build_planner_execute_context(
                {
                    "user_directive": user_directive,
                    "target_titles": target_titles,
                }
            )
            if execute_plan:
                execution_blocker = self._planner_execute_blocker(
                    result.spec,
                    readiness,
                    execute_context,
                )
                if execution_blocker is None:
                    execution_result = self._execute_planner_spec(
                        spec=result.spec,
                        execute_context=execute_context,
                    )

            body = self._render_planner_preview_report(
                result=result,
                readiness=readiness,
                user_directive=user_directive,
                target_titles=target_titles,
                execute_plan=execute_plan,
                execution_result=execution_result,
                execution_blocker=execution_blocker,
            )
            spec = result.spec
            plan_dict = result.plan_dict or {}
            meta = {
                "planner_mode": "execute" if execution_result else "preview",
                "execute_requested": execute_plan,
                "execution_status": (
                    execution_result.status
                    if execution_result
                    else ("blocked_by_execution_gate" if execution_blocker else "not_requested")
                ),
                "execution_blocker": execution_blocker,
                "pipeline_run_id": execution_result.run_id if execution_result else None,
                "plan_id": spec.id if spec else None,
                "step_count": len(spec.steps) if spec else 0,
                "step_statuses": (
                    {step_id: sr.status for step_id, sr in execution_result.steps.items()}
                    if execution_result
                    else {}
                ),
                "finality_status": (
                    self._finality_status(spec, execution_result)
                    if execution_result
                    else "not_executed"
                ),
                "target_titles": target_titles,
                "plan_json": plan_dict,
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
                "capability_resolution": (
                    PlannerService.resolve_plan_capabilities(spec, result.capabilities)
                    if spec
                    else {}
                ),
            }
            title = f"{spec.description or spec.id}"
        else:
            body = self._render_planner_preview_error(
                result=result,
                user_directive=user_directive,
                target_titles=target_titles,
                execute_plan=execute_plan,
            )
            meta = {
                "planner_mode": "preview",
                "execute_requested": execute_plan,
                "execution_status": "planning_failed" if execute_plan else "not_requested",
                "planning_status": result.status,
                "target_titles": target_titles,
                "error": True,
            }
            title = "Error"

        report_type = "ins-plan-exe" if meta.get("planner_mode") == "execute" else "ins-plan-pre"
        _, full_markdown = self._write_report(
            title,
            body,
            report_type,
            meta,
        )
        requested_cmd = (
            "insight-plan-execute" if report_type == "ins-plan-exe" else "insight-plan-preview"
        )
        self._mirror_to_insights(
            full_markdown,
            requested_cmd=requested_cmd,
            related_titles=target_titles,
        )
        return full_markdown

    def _render_planner_preview_report(
        self,
        *,
        result,
        readiness,
        user_directive: str,
        target_titles: list[str],
        execute_plan: bool,
        execution_result: PipelineRunResult | None = None,
        execution_blocker: str | None = None,
    ) -> str:
        spec = result.spec
        plan_dict = result.plan_dict or {}
        capability_by_name = {c.name: c for c in result.capabilities}
        summary = (plan_dict.get("summary") or "").strip() or "(planner did not provide a summary)"
        executed = execution_result is not None

        lines: list[str] = [
            f"# Insight Planner {'Execute' if executed else 'Preview'}: {spec.description or spec.id}",
            "",
            "> [!IMPORTANT]",
            (
                "> Phase 6B execution was requested and allowed; this report includes real pipeline step results."
                if executed
                else "> Planner mode preview contains a validated recommended plan; no pipeline steps were executed."
            ),
            "",
        ]
        if execute_plan and execution_blocker:
            lines.extend(
                [
                    "> [!WARNING]",
                    f"> `/execute` was requested, but execution was blocked: {execution_blocker}",
                    "",
                ]
            )

        lines.extend(
            [
                "## Summary",
                "",
                summary,
                "",
                "## User Directive",
                "",
                "```",
                user_directive,
                "```",
                "",
            ]
        )

        if target_titles:
            lines.extend(["## Target References", ""])
            lines.extend(f"- `[[{title}]]`" for title in target_titles)
            lines.append("")

        lines.extend(self._render_readiness_section(readiness))
        if execution_result:
            lines.extend(self._render_planner_execution_section(spec, execution_result))
        else:
            lines.extend(
                self._render_preview_handoff(readiness, plan_dict, execute_plan, execution_blocker)
            )

        lines.extend(["## Recommended Plan", ""])
        for idx, step in enumerate(spec.steps, 1):
            raw_step = self._raw_plan_step(plan_dict, step.id)
            cap = capability_by_name.get(step.capability)
            cost = cap.cost_class if cap else "unknown"
            rationale = (raw_step.get("rationale") or "").strip()

            lines.append(f"### Step {idx}: `{step.id}`")
            lines.append(f"- **Capability**: `{step.capability}`")
            lines.append(f"- **Adapter**: `{step.adapter}`")
            lines.append(f"- **Estimated cost class**: `{cost}`")
            if step.when:
                lines.append(f"- **Condition**: `{step.when}`")
            if step.inputs:
                lines.append("- **Inputs**:")
                for key, value in step.inputs.items():
                    lines.append(f"  - `{key}`: `{value}`")
            if rationale:
                lines.append(f"- **Why here**: {rationale}")
            lines.append("")

        lines.extend(
            [
                "## Risk Notes",
                "",
                (
                    "- `/execute` ran only after readiness, step-count, adapter allowlist, and runner validation gates passed."
                    if executed
                    else "- Preview mode did not execute adapters, mutate files, or create child trace runs."
                ),
                "- Execution stays behind explicit `/execute` because planned steps may invoke high-cost capabilities.",
                "- Any step using an unregistered adapter will fail runner validation until an allowlisted adapter exists.",
                "",
                "## Raw Plan JSON",
                "",
                "```json",
                json.dumps(plan_dict, indent=2, ensure_ascii=False),
                "```",
            ]
        )
        return "\n".join(lines)

    def _planner_execute_blocker(
        self,
        spec: PipelineSpec,
        readiness,
        execute_context: dict,
    ) -> str | None:
        if readiness.verdict != "ready":
            return f"readiness verdict is `{readiness.verdict}`, not `ready`"
        if len(spec.steps) > _PLANNER_EXECUTE_MAX_STEPS:
            return f"plan has {len(spec.steps)} steps; max allowed is {_PLANNER_EXECUTE_MAX_STEPS}"
        allowed = set(builtin_adapter_names())
        adapters = {step.adapter for step in spec.steps}
        unknown = sorted(adapters - allowed)
        if unknown:
            return f"plan references non-allowlisted adapters: {unknown}"
        missing_context = sorted(self._required_context_keys(spec) - set(execute_context))
        if missing_context:
            return (
                f"plan requires context keys not provided by Insight execution: {missing_context}"
            )

        runner = self._pipeline_runner()
        try:
            runner.validate(spec)
        except PipelineError as e:
            return f"runner validation failed: {e}"
        return None

    def _execute_planner_spec(
        self, *, spec: PipelineSpec, execute_context: dict
    ) -> PipelineRunResult:
        from core.ui import ui

        ui.set_status(f"⚙️ Insight Planner 執行 plan：{spec.id}（{len(spec.steps)} 個步驟）")
        runner = self._pipeline_runner()
        return runner.run(spec, context=execute_context)

    @staticmethod
    def _build_planner_execute_context(task_context: dict) -> dict:
        user_directive = task_context.get("user_directive") or ""
        target_titles = task_context.get("target_titles") or []
        return {
            "user_directive": user_directive,
            "target_titles": target_titles,
            "candidate": user_directive,
            "query": user_directive,
            "focus": (
                "Evaluate the user directive against the loaded sources; "
                "surface differences, critique angles, and action implications."
            ),
            "title": "Insight planner execution",
            "final_concepts": user_directive,
        }

    @staticmethod
    def _required_context_keys(spec: PipelineSpec) -> set[str]:
        keys: set[str] = set()
        for step in spec.steps:
            for value in step.inputs.values():
                keys.update(PlannerFlowMixin._context_keys_in_value(value))
            if step.when:
                var = step.when.get("var")
                if isinstance(var, str) and var.startswith("context."):
                    keys.add(var.split(".", 2)[1])
        return keys

    @staticmethod
    def _context_keys_in_value(value) -> set[str]:
        keys: set[str] = set()
        if isinstance(value, str):
            match = re.match(r"^\$\{context\.([a-zA-Z0-9_]+)\}$", value)
            if match:
                keys.add(match.group(1))
        elif isinstance(value, dict):
            for nested in value.values():
                keys.update(PlannerFlowMixin._context_keys_in_value(nested))
        elif isinstance(value, (list, tuple)):
            for nested in value:
                keys.update(PlannerFlowMixin._context_keys_in_value(nested))
        return keys

    @staticmethod
    def _render_planner_execution_section(
        spec: PipelineSpec,
        result: PipelineRunResult,
    ) -> list[str]:
        lines = [
            "## Execution Result",
            "",
            f"- **Status**: `{result.status}`",
            f"- **Pipeline run id**: `{result.run_id or ''}`",
            f"- **Finality**: `{PlannerFlowMixin._finality_status(spec, result)}`",
            "",
        ]
        finality_note = PlannerFlowMixin._finality_note(spec, result)
        if finality_note:
            lines.extend(
                [
                    "> [!WARNING]",
                    f"> {finality_note}",
                    "",
                ]
            )
        if result.error:
            lines.extend(["### Execution Error", "", f"```text\n{result.error}\n```", ""])

        for idx, step in enumerate(spec.steps, 1):
            step_result = result.steps.get(step.id)
            if step_result is None:
                lines.append(f"### Step {idx}: `{step.id}` — not reached")
                lines.append("")
                continue
            lines.append(f"### Step {idx}: `{step.id}` — {step_result.status}")
            lines.append(f"- **Capability**: `{step.capability}`")
            lines.append(f"- **Adapter**: `{step.adapter}`")
            if step_result.duration_ms is not None:
                lines.append(f"- **Duration**: `{step_result.duration_ms} ms`")
            if step_result.error:
                lines.append(f"- **Error**: `{step_result.error}`")
            if step_result.output is not None:
                lines.append(
                    f"- **Output preview**: {PlannerFlowMixin._preview_value(step_result.output)}"
                )
            lines.append("")

        source_lines = PlannerFlowMixin._render_source_appendix(result)
        if source_lines:
            lines.extend(source_lines)

        final_output = PlannerFlowMixin._final_step_output_text(spec, result)
        if final_output:
            lines.extend(
                [
                    "## Final Step Output",
                    "",
                    final_output,
                    "",
                ]
            )
        return lines

    @staticmethod
    def _render_source_appendix(result: PipelineRunResult) -> list[str]:
        sources = []
        missing = []
        digests_by_title = {}
        runtime_warnings = []

        for step_result in result.steps.values():
            output = step_result.output
            if not isinstance(output, dict):
                continue

            # Extract from load_sources step
            if "sources" in output:
                sources.extend(output["sources"])
            if "missing_titles" in output:
                missing.extend(output["missing_titles"])

            # Extract from digest_sources step
            if "source_digests" in output:
                for d in output["source_digests"]:
                    if isinstance(d, dict) and "title" in d:
                        digests_by_title[d["title"]] = d
            if "warnings" in output:
                warnings_val = output["warnings"]
                if isinstance(warnings_val, list):
                    runtime_warnings.extend(warnings_val)
                elif isinstance(warnings_val, str):
                    runtime_warnings.append(warnings_val)

        if not sources and not missing:
            return []

        lines = ["## Source Appendix", ""]
        if sources:
            has_any_digest = len(digests_by_title) > 0
            if has_any_digest:
                lines.extend(
                    [
                        "| Title | Kind | Loaded chars | Original chars | Truncated | Digest chars | Coverage Warning | Path |",
                        "| --- | --- | ---: | ---: | --- | ---: | --- | --- |",
                    ]
                )
            else:
                lines.extend(
                    [
                        "| Title | Kind | Loaded chars | Original chars | Truncated | Path |",
                        "| --- | --- | ---: | ---: | --- | --- |",
                    ]
                )

            for src in sources:
                if not isinstance(src, dict):
                    continue
                title_raw = src.get("title") or ""
                title = PlannerFlowMixin._escape_table_cell(str(title_raw))
                kind = PlannerFlowMixin._escape_table_cell(str(src.get("source_kind") or "unknown"))
                loaded = src.get("loaded_chars", src.get("chars", ""))
                original = src.get("original_chars", "")
                truncated = "yes" if src.get("truncated") else "no"
                path = PlannerFlowMixin._escape_table_cell(str(src.get("path") or ""))

                if has_any_digest:
                    digest_info = digests_by_title.get(title_raw)
                    if digest_info:
                        digest_chars = digest_info.get("digest_chars", "")
                        cov_warnings = []
                        if digest_info.get("truncated_for_digest"):
                            cov_warnings.append("truncated for digest")
                        for w in runtime_warnings:
                            if f"'{title_raw}'" in w or f'"{title_raw}"' in w:
                                cov_warnings.append(w)
                        cov_warning_str = "; ".join(set(cov_warnings)) if cov_warnings else "none"
                    else:
                        digest_chars = "N/A"
                        cov_warning_str = "no digest"

                    cov_warning_str = PlannerFlowMixin._escape_table_cell(cov_warning_str)
                    lines.append(
                        f"| {title} | {kind} | {loaded} | {original} | {truncated} | {digest_chars} | {cov_warning_str} | `{path}` |"
                    )
                else:
                    lines.append(
                        f"| {title} | {kind} | {loaded} | {original} | {truncated} | `{path}` |"
                    )
            lines.append("")

        if missing:
            lines.extend(
                [
                    "**Missing titles:**",
                    "",
                ]
            )
            lines.extend(f"- `{t}`" for t in missing)
            lines.append("")
        return lines

    @staticmethod
    def _escape_table_cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    @staticmethod
    def _finality_status(spec: PipelineSpec, result: PipelineRunResult | None) -> str:
        if result is None:
            return "not_executed"
        if result.status != "succeeded":
            return "failed"
        final_step = spec.steps[-1] if spec.steps else None
        if final_step is None:
            return "empty_plan"
        if final_step.capability == "critique":
            output = PlannerFlowMixin._final_step_output_text(spec, result).lower()
            if "overall verdict" in output and "revise" in output:
                return "critique_requires_revision"
            return "critique_only"
        return "final_output"

    @staticmethod
    def _finality_note(spec: PipelineSpec, result: PipelineRunResult) -> str:
        status = PlannerFlowMixin._finality_status(spec, result)
        if status == "critique_requires_revision":
            return (
                "The final step is a critique with a revise verdict. Treat this as diagnostics, "
                "not the final user-facing answer; add a revision/finalization step before product use."
            )
        if status == "critique_only":
            return (
                "The final step is critique. Treat this as an evaluation report unless a later "
                "step turns findings into a final answer."
            )
        return ""

    @staticmethod
    def _final_step_output_text(spec: PipelineSpec, result: PipelineRunResult) -> str:
        if not spec.steps:
            return ""
        final_step = spec.steps[-1]
        step_result = result.steps.get(final_step.id)
        if step_result is None or step_result.output is None:
            return ""
        output = step_result.output
        if isinstance(output, dict):
            if isinstance(output.get("output"), str):
                return output["output"].strip()
            return json.dumps(output, indent=2, ensure_ascii=False)
        if isinstance(output, str):
            return output.strip()
        return json.dumps(output, indent=2, ensure_ascii=False)

    @staticmethod
    def _preview_value(value, max_len: int = 220) -> str:
        if value is None:
            return "_None_"
        if isinstance(value, dict):
            text = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, (list, tuple)):
            text = json.dumps(list(value), ensure_ascii=False)
        else:
            text = str(value)
        text = text.replace("\n", " ").replace("`", "ʹ")
        if len(text) > max_len:
            text = text[: max_len - 1] + "…"
        return f"`{text}`"

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
            lines.extend(
                [
                    "No readiness issues detected by static checks.",
                    "",
                ]
            )
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
    def _render_preview_handoff(
        readiness,
        plan_dict: dict,
        execute_plan: bool = False,
        execution_blocker: str | None = None,
    ) -> list[str]:
        plan_id = plan_dict.get("id") or "planner_plan"
        lines = ["## Preview Handoff", ""]

        if readiness.verdict == "ready":
            lines.extend(
                [
                    "This plan passed static readiness checks for guarded execution.",
                    "",
                    "- No sidecar execution plan was written by this Insight preview.",
                    "- Add `/execute` or `/execution` to the planner-mode directive to run it through the guarded Insight execution path.",
                    "- Or run the same directive through `@ling-plan`, review its readiness section, then approve with `@ling-do <plan_id>`.",
                    f"- Expected plan id if preserved by the planner: `{plan_id}`.",
                    "",
                ]
            )
        elif readiness.verdict == "needs_review":
            lines.extend(
                [
                    "This plan needs human review before execution.",
                    "",
                    "- Address the readiness findings above, then re-run planner preview.",
                    "- Do not enable `/execute` for this plan until warnings are resolved or explicitly accepted.",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "This plan is blocked for execution.",
                    "",
                    "- Resolve error-level readiness findings first.",
                    "- Re-plan after fixing missing capabilities, adapters, or invalid execution contracts.",
                    "",
                ]
            )
        if execute_plan and execution_blocker:
            lines.extend(
                [
                    "**Execution blocker:**",
                    "",
                    f"```text\n{execution_blocker}\n```",
                    "",
                ]
            )
        return lines

    def _render_planner_preview_error(
        self,
        *,
        result,
        user_directive: str,
        target_titles: list[str],
        execute_plan: bool,
    ) -> str:
        lines = [
            "# Insight Planner Preview Error",
            "",
            "> [!IMPORTANT]",
            "> Planner mode could not produce a valid executable plan. No pipeline steps were executed.",
            "",
            f"**Planning status:** `{result.status}`",
            "",
            "## Error",
            "",
            result.error or "Unknown planning error.",
            "",
            "## User Directive",
            "",
            "```",
            user_directive,
            "```",
            "",
        ]
        if execute_plan:
            lines.extend(
                [
                    "## Execution Request",
                    "",
                    "`/execute` was present, but planning did not produce a valid executable plan.",
                    "",
                ]
            )
        if target_titles:
            lines.extend(["## Target References", ""])
            lines.extend(f"- `[[{title}]]`" for title in target_titles)
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _raw_plan_step(plan_dict: dict, step_id: str) -> dict:
        for step in plan_dict.get("steps") or []:
            if isinstance(step, dict) and step.get("id") == step_id:
                return step
        return {}

    # ── Pipeline: Single-Shot ────────────────────────────────────────
