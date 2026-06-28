"""Phase 5C ExecutorAgent — runs a Planner-produced plan.

Trigger: `@ling-do <plan_id>` (or `/do <plan_id>`).

Flow:
  1. Parse `<plan_id>` out of the user directive.
  2. Load the sidecar JSON written by PlannerAgent at
     `Database/plans/<plan_id>.json`.
  3. Re-validate via `load_pipeline_from_dict` so a registry that
     drifted between planning time and execution time is caught here,
     not deep inside an adapter.
  4. Build an AdapterRegistry, register every built-in adapter against
     the live LLMClient (see services/builtin_adapters.py).
  5. Hand the spec to PipelineRunner. The runner opens a parent run
     (intent=execute:<plan_id>) plus one child run per step.
  6. Render an execution report covering: status, per-step outcome,
     truncated output excerpts, and pointers to the trace tree.

This is the "controlled execution" half of the planner/executor split.
PlannerAgent guarantees the plan is valid against the schema; this agent
guarantees execution is sandboxed to registered adapters only.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from agents.base_agent import BaseAgent
from core.config import PLANS_DIR
from core.ui import ui
from services.builtin_adapters import register_builtin_adapters
from services.pipeline_runner import (
    AdapterRegistry,
    PipelineError,
    PipelineRunResult,
    PipelineRunner,
    PipelineSpec,
    load_pipeline_from_dict,
)


_PLAN_ID_TOKEN_RE = re.compile(
    r"(?:@ling-do|/do)\s+([\w\-]{3,})",
    re.IGNORECASE,
)


class ExecutorAgent(BaseAgent):
    """Executes a previously-validated plan. No plan generation."""

    def execute(self, task_context: dict) -> str:
        user_directive = (task_context.get("user_directive") or "").strip()
        plan_id = self._parse_plan_id(user_directive, task_context)

        if not plan_id:
            return self._error_report(
                "ExecutorAgent: could not parse a plan_id from the directive.\n\n"
                "Usage: `@ling-do <plan_id>` (run `@ling-plan ...` first to "
                "produce a plan, then use the resulting plan id here)."
            )

        plan_dict = self._load_sidecar(plan_id)
        if plan_dict is None:
            return self._error_report(
                f"ExecutorAgent: no plan found at `{PLANS_DIR / (plan_id + '.json')}`. "
                f"Re-run `@ling-plan` to produce plan `{plan_id}`, or use a "
                "different plan id."
            )

        try:
            spec = load_pipeline_from_dict(plan_dict, default_id=plan_id)
        except PipelineError as e:
            return self._error_report(
                f"ExecutorAgent: plan `{plan_id}` failed re-validation. "
                f"The registry may have drifted since the plan was produced.\n\n"
                f"**Validation error:** {e}"
            )

        # Sandbox: registry contains ONLY the built-in adapters, plus
        # whatever the LLMClient instance exposes. Steps referencing
        # unknown adapters fail PipelineRunner.validate() up front.
        registry = AdapterRegistry()
        register_builtin_adapters(registry, self.llm)

        runner = PipelineRunner(
            capability_manager=self.llm.capability_manager,
            adapter_registry=registry,
            trace_store=getattr(self.llm, "trace_store", None),
        )

        try:
            runner.validate(spec)
        except PipelineError as e:
            return self._error_report(
                f"ExecutorAgent: plan `{plan_id}` references something not "
                f"available in the live registry.\n\n**Validation error:** {e}\n\n"
                "Hint: planning time and execution time saw different "
                "registries. Re-plan to refresh."
            )

        # Readiness gate: structural validation above proves the plan parses
        # and references only registered adapters/capabilities; readiness adds
        # the "would it execute meaningfully?" checks (input wiring, digest
        # rules). A `blocked` verdict means an error-severity finding — refuse
        # rather than running a plan the planner already flagged as broken.
        blocking = self._readiness_blockers(spec, plan_dict, task_context)
        if blocking:
            bullet = "\n".join(f"- **[{f['code']}]** {f['message']}" for f in blocking)
            ui.error(f"⚙️ Executor 拒絕執行 blocked plan：{plan_id}")
            return self._error_report(
                f"ExecutorAgent: plan `{plan_id}` is **blocked** by readiness "
                f"checks and was not executed.\n\n{bullet}\n\n"
                "Re-run `@ling-plan` to produce a clean plan, or fix the issues above."
            )

        ui.set_status(f"⚙️ Executor 執行 plan：{plan_id}（{len(spec.steps)} 個步驟）")

        # Initial context: pull simple values from task_context. The user
        # supplies target wikilinks; richer context lifecycle (loading
        # part_digests from a Stitched article, etc.) is a 5C+ extension.
        initial_context = self._build_initial_context(task_context)

        result = runner.run(spec, context=initial_context)

        report = self._render_execution_report(spec, result, plan_id, initial_context)

        meta = {
            "plan_id": plan_id,
            "pipeline_run_id": result.run_id,
            "step_count": len(spec.steps),
            "execution_status": result.status,
            "step_statuses": {
                step_id: sr.status for step_id, sr in result.steps.items()
            },
            "initial_context_keys": sorted(initial_context.keys()),
        }
        title = f"{spec.description or spec.id}"
        self._write_report(title, report, "cmd", meta)

        if result.status == "succeeded":
            ui.success(
                f"⚙️ Executor 完成：{plan_id} ({len(result.steps)} steps, "
                f"all succeeded)"
            )
        else:
            ui.error(
                f"⚙️ Executor failed at step → see report. "
                f"Plan: {plan_id}"
            )
        return report

    # ── Plan loading ───────────────────────────────────────────────────

    @staticmethod
    def _parse_plan_id(user_directive: str, task_context: dict) -> str | None:
        """Parse the plan_id immediately following `@ling-do` or `/do`.

        Explicit `task_context["plan_id"]` wins. Otherwise the directive
        MUST contain a recognizable command token (`@ling-do <id>` or
        `/do <id>`) — we deliberately don't fall back to fuzzy token
        extraction, which would mis-fire on prose like "do something".
        Plan id must be at least 3 chars.
        """
        explicit = task_context.get("plan_id")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()

        match = _PLAN_ID_TOKEN_RE.search(user_directive)
        if match:
            return match.group(1)
        return None

    def _readiness_blockers(
        self, spec: PipelineSpec, plan_dict: dict, task_context: dict
    ) -> list[dict]:
        """Return error-severity readiness findings, or [] (= allow execution).

        Re-assesses readiness against the LIVE capability set so a plan that
        became unexecutable since planning is caught. Best-effort: if the
        capability manager is unavailable or the assessment raises, we allow
        execution (structural validation already guarded the hard cases) — a
        diagnostic bug must never wedge a legitimate run.
        """
        try:
            from services.plan_readiness import assess_plan_readiness

            cap_mgr = getattr(self.llm, "capability_manager", None)
            if cap_mgr is None or not hasattr(cap_mgr, "all"):
                return []
            readiness = assess_plan_readiness(
                spec=spec,
                plan_dict=plan_dict,
                capabilities=cap_mgr.all(),
                target_titles=task_context.get("target_titles"),
            )
            if readiness.verdict != "blocked":
                return []
            return [
                {"code": f.code, "message": f.message}
                for f in readiness.findings
                if f.severity == "error"
            ]
        except Exception as e:  # noqa: BLE001 — degrade to allow, never block on a bug
            logging.warning(f"ExecutorAgent: readiness re-assessment skipped: {e}")
            return []

    @staticmethod
    def _load_sidecar(plan_id: str) -> dict | None:
        path = PLANS_DIR / f"{plan_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logging.error(f"ExecutorAgent: could not parse plan sidecar {path}: {e}")
            return None

    @staticmethod
    def _build_initial_context(task_context: dict) -> dict:
        """Phase 5C MVP: forward simple scalars from task_context.

        A future increment can auto-load part_digests / Stitched bodies
        when the directive references `[[wikilinks]]`. For 5C, we keep
        the contract minimal — the plan is responsible for declaring
        what it needs via `${context.X}` placeholders.
        """
        ctx = task_context.get("execute_context") or {}
        if not isinstance(ctx, dict):
            ctx = {}
        # Convenience: also expose target_titles so plans can grab them.
        targets = task_context.get("target_titles") or []
        if targets and "target_titles" not in ctx:
            ctx["target_titles"] = targets
        return ctx

    # ── Reporting ──────────────────────────────────────────────────────

    def _render_execution_report(
        self,
        spec: PipelineSpec,
        result: PipelineRunResult,
        plan_id: str,
        initial_context: dict,
    ) -> str:
        verdict_emoji = "✅" if result.status == "succeeded" else "💧"
        lines: list[str] = [
            f"# {verdict_emoji} Execution: {spec.description or spec.id}",
            "",
            "> [!INFO]",
            f"> **Plan ID:** `{plan_id}`  "
            f"**Status:** `{result.status}`  "
            f"**Steps:** {len(result.steps)} / {len(spec.steps)}",
            "",
        ]

        if result.error:
            lines.extend([
                "## 💦 Execution Error",
                "",
                f"```\n{result.error}\n```",
                "",
            ])

        # Initial context summary
        if initial_context:
            lines.append("## 📂 Initial Context")
            lines.append("")
            for key, val in initial_context.items():
                preview = self._preview_value(val)
                lines.append(f"- `{key}`: {preview}")
            lines.append("")

        # Per-step outcomes
        lines.append("## 🪜 Step Outcomes")
        lines.append("")
        for idx, step in enumerate(spec.steps, 1):
            step_result = result.steps.get(step.id)
            if step_result is None:
                lines.append(f"### Step {idx}: `{step.id}` — ⏸ not reached")
                lines.append("")
                continue
            emoji = {
                "succeeded": "✅",
                "skipped": "⏭",
                "failed": "💧",
            }.get(step_result.status, "❔")
            lines.append(
                f"### Step {idx}: `{step.id}` — {emoji} {step_result.status}"
            )
            lines.append(f"- **Capability**: `{step.capability}`")
            lines.append(f"- **Adapter**: `{step.adapter}`")
            if step_result.duration_ms is not None:
                lines.append(f"- **Duration**: {step_result.duration_ms} ms")
            if step_result.error:
                lines.append(f"- **Error**: `{step_result.error}`")
            if step_result.output is not None:
                preview = self._preview_value(step_result.output)
                lines.append(f"- **Output preview**: {preview}")
            lines.append("")

        # Trace pointer (Phase 5C: parent + per-step children)
        if result.run_id:
            lines.extend([
                "## 🔍 Trace",
                "",
                f"- **Pipeline run id**: `{result.run_id}`",
                "- Per-step LLM calls and artifacts are children of this "
                "run in `llm_trace.sqlite`.",
                "- Query: `SELECT * FROM runs WHERE parent_run_id = "
                f"'{result.run_id}' ORDER BY started_at;`",
                "",
            ])

        return "\n".join(lines)

    @staticmethod
    def _preview_value(value: Any, max_len: int = 160) -> str:
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

    # ── Error path ─────────────────────────────────────────────────────

    def _error_report(self, message: str) -> str:
        body = f"# 💧 Executor Error\n\n{message}\n"
        self._write_report("Error", body, "cmd",
                            {"error": True})
        ui.error(f"⚙️ Executor failed: {message.splitlines()[0][:120]}")
        return body
