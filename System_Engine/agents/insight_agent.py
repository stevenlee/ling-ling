import logging
import json
import random
import re
import yaml
from collections import Counter
from datetime import datetime
from itertools import combinations
from pathlib import Path

from agents.base_agent import BaseAgent
from core.config import SKILLS_DIR, WIKI_VAULT_DIR
from core.parser import extract_json_array, extract_json_object
from services.builtin_adapters import builtin_adapter_names, register_builtin_adapters
from services.plan_readiness import assess_plan_readiness
from services.pipeline_runner import AdapterRegistry, PipelineError, PipelineRunner, PipelineRunResult, PipelineSpec
from services.planner_service import PlannerService


_WIKILINK_RE = re.compile(r'\[\[(.*?)\]\]')
_HASHTAG_RE = re.compile(r'#([^\s#]+)')
_SKILL_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)
_BOOK_SUFFIX_RE = re.compile(r'\s*\((?:Part\s+\d+|Stitched|Synthesis)\)\s*$', re.IGNORECASE)
_STITCHED_SUFFIX_RE = re.compile(r'\(Stitched\)\s*$', re.IGNORECASE)
_SYNTHESIS_SUFFIX_RE = re.compile(r'\(Synthesis\)\s*$', re.IGNORECASE)

# Auto-attached by ingestion_pipeline.py — not content topics, so excluded
# from tag-cluster sampling (otherwise nearly every run picks one).
_SYSTEM_TAGS = frozenset({"synthesis", "completed", "stitched", "longform", "perfectpitch"})
_PLANNER_EXECUTE_MAX_STEPS = 4


class InsightAgent(BaseAgent):
    """Generate insights from the knowledge base.

    Two pipelines:
      - 'single':     One-shot LLM call with strategy-specific context.
      - 'montecarlo': Multi-round explore → score → filter → expand → synthesize.
    """

    TEMP_SPARK = 0.9
    TEMP_EXPAND = 0.5
    TEMP_SYNTHESIZE = 0.3

    def __init__(self, llm, rag_manager):
        super().__init__(llm, rag_manager)
        self.insights_dir = WIKI_VAULT_DIR / "Insights"
        self.insights_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir = SKILLS_DIR
        self.strategies = self._load_strategies()

    def _load_strategies(self) -> dict:
        if not self.skills_dir.exists():
            return {}

        strategies: dict = {}
        for filepath in self.skills_dir.glob("*.md"):
            try:
                content = filepath.read_text(encoding="utf-8")
                match = _SKILL_FRONTMATTER_RE.search(content)
                if not match:
                    continue
                yaml_data = yaml.safe_load(match.group(1))
                if not isinstance(yaml_data, dict) or "name" not in yaml_data:
                    continue
                yaml_data["system_prompt"] = content[match.end():].strip()
                strategies[yaml_data["name"]] = yaml_data
            except Exception as e:
                logging.error(f"Failed to load skill {filepath.name}: {e}")
        return strategies

    # ── Public Entry Points ─────────────────────────────────────────────

    def execute(self, task_context: dict) -> str:
        strategy_id = task_context.get("strategy_id", "recency")
        user_directive = task_context.get("user_directive", "")
        is_full_report = task_context.get("is_full_report", False)
        forced_template = task_context.get("forced_template")
        target_titles = task_context.get("target_titles") or []

        if task_context.get("planner_mode"):
            return self._run_planner_preview(
                user_directive=user_directive,
                target_titles=target_titles,
                forced_template=forced_template,
                execute_plan=bool(task_context.get("execute_plan")),
            )

        if is_full_report:
            return self.generate_full_insight(
                user_directive,
                forced_template=forced_template,
                target_titles=target_titles,
            )
        return self.generate_insight(
            strategy_id,
            user_directive,
            forced_template=forced_template,
            target_titles=target_titles,
        )

    def _signals_meta(self, content: str, target_titles) -> dict:
        """Signals metadata block ({} when disabled). Shared by
        generate_insight and generate_full_insight (audit R7-D — the two were
        byte-for-byte duplicates)."""
        from core.config import INSIGHT_SIGNALS_ENABLED
        if not INSIGHT_SIGNALS_ENABLED:
            return {}
        from services.insight_signals import compute_signals
        signals = compute_signals(content, target_titles, self.rag, self.llm)
        return {
            "signals": {
                "groundedness": round(signals.groundedness, 4) if signals.groundedness is not None else None,
                "novelty": round(signals.novelty, 4) if signals.novelty is not None else None,
                "bridging": round(signals.bridging, 4) if signals.bridging is not None else None,
                "refute_verdict": signals.refute_verdict,
            },
            "signals_version": 1,
        }

    def _maybe_artifact(self, content: str) -> str:
        """Phase 6 auto-attach: a learning-aid section for the insight body, or
        "" when Scripture's `visual_router` is off (zero LLM calls). Fail-open —
        a visual is a bonus, never block the insight report on it."""
        try:
            from services.learning_artifacts import maybe_artifact_section
            section = maybe_artifact_section(self.llm, content)
            return f"\n\n---\n\n{section}" if section else ""
        except Exception as e:
            logging.warning(f"insight artifact auto-attach failed: {e}")
            return ""

    @staticmethod
    def _pair_key(a: dict, b: dict) -> tuple:
        """Order-independent dedup key for a document pair (audit R7-D — this
        idiom appeared verbatim at five sites)."""
        return tuple(sorted([a["title"], b["title"]]))

    def generate_insight(
        self,
        strategy_id: str,
        user_directive: str = "",
        forced_template: str | None = None,
        target_titles: list[str] | None = None,
    ) -> str:
        if strategy_id not in self.strategies:
            if not self.strategies:
                return "❌ Error: No strategies found."
            strategy_id = random.choice(list(self.strategies.keys()))

        config = self.strategies[strategy_id]

        blockers = self._check_skill_preconditions(config.get("applicable_when") or {})
        if blockers:
            from core.ui import ui
            reasons = "；".join(blockers)
            message = f"⏸️ 技能「{strategy_id}」前置條件未滿足：{reasons}"
            ui.error(message)
            logging.warning(f"Insight skill '{strategy_id}' skipped: {reasons}")
            return message

        pipeline = config.get("pipeline", "single")
        resolved_template = forced_template or config.get("template")
        self._grounded_on_acc = set()   # F1: claims this run grounded on (for frontmatter)

        if pipeline == "montecarlo":
            report_content = self._run_montecarlo(config, user_directive, resolved_template)
        else:
            report_content = self._run_single(config, user_directive, resolved_template)

        meta = {
            "exercise_strategy": strategy_id,
            "exercise_name": config["name"],
            "exercise_description": config["description"],
            "pipeline": pipeline,
        }

        meta.update(self._signals_meta(report_content, target_titles))
        if self._grounded_on_acc:
            meta["grounded_on"] = sorted(self._grounded_on_acc)

        report_content += self._maybe_artifact(report_content)

        _, full_markdown = self._write_report(
            f"洞察分析-{config['name']}", report_content, "report_insight", meta
        )
        self._mirror_to_insights(
            full_markdown,
            requested_cmd=f"insight-{strategy_id}",
            related_titles=target_titles,
        )
        return full_markdown

    def _check_skill_preconditions(self, applicable_when: dict) -> list[str]:
        """Validate a skill's `applicable_when` frontmatter against the live
        vault. Returns a list of human-readable blockers (empty = runnable).

        Supported keys: `database_populated` (bool), `min_documents` (int,
        compared against indexed chunk count), `has_tag_graph` (bool).
        Unknown keys are ignored so skills can carry forward-compatible
        conditions without breaking older engines. Fail-open on RAG errors —
        a broken precondition check must not disable insights entirely.
        """
        if not applicable_when or not isinstance(applicable_when, dict) or self.rag is None:
            return []

        blockers: list[str] = []
        try:
            needs_count = applicable_when.get("database_populated") or (
                applicable_when.get("min_documents") is not None
            )
            count = self.rag.get_total_chunks_count() if needs_count else None

            if applicable_when.get("database_populated") and not count:
                blockers.append("知識庫是空的，請先 ingest 一些文件")

            min_docs = applicable_when.get("min_documents")
            if isinstance(min_docs, int) and count is not None and count < min_docs:
                blockers.append(f"需要至少 {min_docs} 份索引文件，目前只有 {count}")

            if applicable_when.get("has_tag_graph") and hasattr(self.rag, "has_tagged_documents"):
                if not self.rag.has_tagged_documents():
                    blockers.append("沒有任何帶標籤的文件，無法建立 tag graph")
        except Exception as e:
            logging.warning(f"Skill precondition check failed (allowing run): {e}")
            return []
        return blockers

    def generate_full_insight(
        self,
        user_directive: str = "",
        forced_template: str | None = None,
        target_titles: list[str] | None = None,
    ) -> str:
        """Run all strategies, then perform a cross-strategy synthesis."""
        section_results = []
        insight_seeds = []
        self._grounded_on_acc = set()   # F1: accumulates across all strategies' seeds

        for strategy_id, config in self.strategies.items():
            pipeline = config.get("pipeline", "single")
            resolved_template = forced_template or config.get("template")

            if pipeline == "montecarlo":
                section_content = self._run_montecarlo(config, user_directive, resolved_template)
            else:
                section_content = self._run_single(config, user_directive, resolved_template)

            section_results.append(f"## 📌 分析維度：{config['name']}\n\n{section_content}")
            insight_seeds.extend(self._extract_seeds_from_section(section_content, config["name"]))

        cross_synthesis = self._cross_strategy_synthesis(insight_seeds, user_directive)
        sections_joined = "\n\n---\n\n".join(section_results)

        final_markdown = (
            f"# 🎀 Ling Ling 的練習本 (Full Report)\n\n"
            f"## 🔮 跨維度綜合洞察 (Cross-Strategy Synthesis)\n\n{cross_synthesis}\n\n---\n\n"
            f"{sections_joined}"
        )

        meta = self._signals_meta(final_markdown, target_titles)
        if self._grounded_on_acc:
            meta["grounded_on"] = sorted(self._grounded_on_acc)

        final_markdown += self._maybe_artifact(cross_synthesis)

        _, full_markdown = self._write_report("全方位洞察報告", final_markdown, "report_insight_full", meta)
        self._mirror_to_insights(
            full_markdown,
            requested_cmd="full-insight",
            related_titles=target_titles,
        )
        return full_markdown

    def _mirror_to_insights(
        self,
        full_markdown: str,
        requested_cmd: str | None = None,
        related_titles: list[str] | None = None,
        prefix: str | None = None,
    ) -> None:
        """Drop a byte-identical copy of the canonical report in Insights/.

        We re-write the same full markdown (frontmatter + body) that
        `_write_report` just wrote to FROM_LLM_DIR, so the Insights/ copy
        stays indexable in Obsidian with the full title/type/version/stats
        frontmatter.
        """
        insight_file = self.insights_dir / self._build_insights_filename(
            requested_cmd=requested_cmd or self._cmd_from_legacy_prefix(prefix),
            related_titles=related_titles,
        )
        insight_file.write_text(full_markdown, encoding="utf-8")

    @classmethod
    def _build_insights_filename(
        cls,
        *,
        requested_cmd: str,
        related_titles: list[str] | None = None,
    ) -> str:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        related = cls._related_doc_name(related_titles)
        cmd = cls._sanitize_filename_part(requested_cmd) or "insight"
        return f"[{timestamp}][{related}][{cmd}].md"

    @classmethod
    def _related_doc_name(cls, related_titles: list[str] | None) -> str:
        titles = [
            cleaned
            for title in (related_titles or [])
            if (cleaned := cls._sanitize_filename_part(str(title)))
        ]
        if not titles:
            return "Vault"
        return "+".join(titles)

    @staticmethod
    def _sanitize_filename_part(value: str) -> str:
        cleaned = re.sub(r'[\\/*?:"<>|\[\]\n\r\t]+', "-", value).strip(" .-")
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned[:80].strip(" .-")

    @staticmethod
    def _cmd_from_legacy_prefix(prefix: str | None) -> str:
        if not prefix:
            return "insight"
        return prefix.removeprefix("🎐").strip("-") or "insight"

    # ── Pipeline: Planner Preview ─────────────────────────────────────

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
            if execute_plan else
            "🎐 Insight Planner 正在規劃預覽..."
        )

        context_note = (
            "This request came from @ling-insight planner-mode. "
            "Generate a plan suitable for an Insight report, but do not execute it. "
            "Prefer registered capabilities with explicit adapters and keep the plan short."
        )
        result = PlannerService(self.llm).generate_plan(
            user_directive=user_directive,
            target_titles=target_titles,
            forced_template=forced_template,
            default_id="insight_planner_preview",
            context_note=context_note,
        )

        if result.ok:
            readiness = assess_plan_readiness(
                spec=result.spec,
                plan_dict=result.plan_dict or {},
                capabilities=result.capabilities,
                target_titles=target_titles,
            )
            execution_result = None
            execution_blocker = None
            execute_context = self._build_planner_execute_context({
                "user_directive": user_directive,
                "target_titles": target_titles,
            })
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
                    execution_result.status if execution_result
                    else ("blocked_by_execution_gate" if execution_blocker else "not_requested")
                ),
                "execution_blocker": execution_blocker,
                "pipeline_run_id": execution_result.run_id if execution_result else None,
                "plan_id": spec.id if spec else None,
                "step_count": len(spec.steps) if spec else 0,
                "step_statuses": (
                    {step_id: sr.status for step_id, sr in execution_result.steps.items()}
                    if execution_result else {}
                ),
                "finality_status": (
                    self._finality_status(spec, execution_result)
                    if execution_result else "not_executed"
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
                    if spec else {}
                ),
            }
            title_prefix = "Insight Planner Execute" if execution_result else "Insight Planner Preview"
            title = f"{title_prefix}: {spec.description or spec.id}"
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
            title = "Insight Planner Preview Error"

        report_type = (
            "report_insight_planner_execute"
            if meta.get("planner_mode") == "execute"
            else "report_insight_planner_preview"
        )
        _, full_markdown = self._write_report(
            title,
            body,
            report_type,
            meta,
        )
        requested_cmd = (
            "insight-plan-execute"
            if report_type == "report_insight_planner_execute"
            else "insight-plan-preview"
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
                if executed else
                "> Planner mode preview contains a validated recommended plan; no pipeline steps were executed."
            ),
            "",
        ]
        if execute_plan and execution_blocker:
            lines.extend([
                "> [!WARNING]",
                f"> `/execute` was requested, but execution was blocked: {execution_blocker}",
                "",
            ])

        lines.extend([
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
        ])

        if target_titles:
            lines.extend(["## Target References", ""])
            lines.extend(f"- `[[{title}]]`" for title in target_titles)
            lines.append("")

        lines.extend(self._render_readiness_section(readiness))
        if execution_result:
            lines.extend(self._render_planner_execution_section(spec, execution_result))
        else:
            lines.extend(self._render_preview_handoff(readiness, plan_dict, execute_plan, execution_blocker))

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

        lines.extend([
            "## Risk Notes",
            "",
            (
                "- `/execute` ran only after readiness, step-count, adapter allowlist, and runner validation gates passed."
                if executed else
                "- Preview mode did not execute adapters, mutate files, or create child trace runs."
            ),
            "- Execution stays behind explicit `/execute` because planned steps may invoke high-cost capabilities.",
            "- Any step using an unregistered adapter will fail runner validation until an allowlisted adapter exists.",
            "",
            "## Raw Plan JSON",
            "",
            "```json",
            json.dumps(plan_dict, indent=2, ensure_ascii=False),
            "```",
        ])
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
            return f"plan requires context keys not provided by Insight execution: {missing_context}"

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
            return f"runner validation failed: {e}"
        return None

    def _execute_planner_spec(self, *, spec: PipelineSpec, execute_context: dict) -> PipelineRunResult:
        from core.ui import ui

        ui.set_status(f"⚙️ Insight Planner 執行 plan：{spec.id}（{len(spec.steps)} 個步驟）")
        registry = AdapterRegistry()
        register_builtin_adapters(registry, self.llm)
        runner = PipelineRunner(
            capability_manager=self.llm.capability_manager,
            adapter_registry=registry,
            trace_store=getattr(self.llm, "trace_store", None),
        )
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
                keys.update(InsightAgent._context_keys_in_value(value))
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
                keys.update(InsightAgent._context_keys_in_value(nested))
        elif isinstance(value, (list, tuple)):
            for nested in value:
                keys.update(InsightAgent._context_keys_in_value(nested))
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
            f"- **Finality**: `{InsightAgent._finality_status(spec, result)}`",
            "",
        ]
        finality_note = InsightAgent._finality_note(spec, result)
        if finality_note:
            lines.extend([
                "> [!WARNING]",
                f"> {finality_note}",
                "",
            ])
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
                lines.append(f"- **Output preview**: {InsightAgent._preview_value(step_result.output)}")
            lines.append("")

        source_lines = InsightAgent._render_source_appendix(result)
        if source_lines:
            lines.extend(source_lines)

        final_output = InsightAgent._final_step_output_text(spec, result)
        if final_output:
            lines.extend([
                "## Final Step Output",
                "",
                final_output,
                "",
            ])
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
                lines.extend([
                    "| Title | Kind | Loaded chars | Original chars | Truncated | Digest chars | Coverage Warning | Path |",
                    "| --- | --- | ---: | ---: | --- | ---: | --- | --- |",
                ])
            else:
                lines.extend([
                    "| Title | Kind | Loaded chars | Original chars | Truncated | Path |",
                    "| --- | --- | ---: | ---: | --- | --- |",
                ])

            for src in sources:
                if not isinstance(src, dict):
                    continue
                title_raw = src.get("title") or ""
                title = InsightAgent._escape_table_cell(str(title_raw))
                kind = InsightAgent._escape_table_cell(str(src.get("source_kind") or "unknown"))
                loaded = src.get("loaded_chars", src.get("chars", ""))
                original = src.get("original_chars", "")
                truncated = "yes" if src.get("truncated") else "no"
                path = InsightAgent._escape_table_cell(str(src.get("path") or ""))

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
                    
                    cov_warning_str = InsightAgent._escape_table_cell(cov_warning_str)
                    lines.append(f"| {title} | {kind} | {loaded} | {original} | {truncated} | {digest_chars} | {cov_warning_str} | `{path}` |")
                else:
                    lines.append(f"| {title} | {kind} | {loaded} | {original} | {truncated} | `{path}` |")
            lines.append("")

        if missing:
            lines.extend([
                "**Missing titles:**",
                "",
            ])
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
            output = InsightAgent._final_step_output_text(spec, result).lower()
            if "overall verdict" in output and "revise" in output:
                return "critique_requires_revision"
            return "critique_only"
        return "final_output"

    @staticmethod
    def _finality_note(spec: PipelineSpec, result: PipelineRunResult) -> str:
        status = InsightAgent._finality_status(spec, result)
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
            lines.extend([
                "No readiness issues detected by static checks.",
                "",
            ])
            return lines

        for finding in readiness.findings:
            scope = f"step `{finding.step_id}`" if finding.step_id else "plan"
            lines.append(
                f"- **{finding.severity.upper()} `{finding.code}`** ({scope}): "
                f"{finding.message}"
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
            lines.extend([
                "This plan passed static readiness checks for guarded execution.",
                "",
                "- No sidecar execution plan was written by this Insight preview.",
                "- Add `/execute` or `/execution` to the planner-mode directive to run it through the guarded Insight execution path.",
                "- Or run the same directive through `@ling-plan`, review its readiness section, then approve with `@ling-do <plan_id>`.",
                f"- Expected plan id if preserved by the planner: `{plan_id}`.",
                "",
            ])
        elif readiness.verdict == "needs_review":
            lines.extend([
                "This plan needs human review before execution.",
                "",
                "- Address the readiness findings above, then re-run planner preview.",
                "- Do not enable `/execute` for this plan until warnings are resolved or explicitly accepted.",
                "",
            ])
        else:
            lines.extend([
                "This plan is blocked for execution.",
                "",
                "- Resolve error-level readiness findings first.",
                "- Re-plan after fixing missing capabilities, adapters, or invalid execution contracts.",
                "",
            ])
        if execute_plan and execution_blocker:
            lines.extend([
                "**Execution blocker:**",
                "",
                f"```text\n{execution_blocker}\n```",
                "",
            ])
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
            lines.extend([
                "## Execution Request",
                "",
                "`/execute` was present, but planning did not produce a valid executable plan.",
                "",
            ])
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

    def _run_single(self, config: dict, user_directive: str, resolved_template: str | None = None) -> str:
        selection = config.get("selection", {})
        method = config.get("method") or selection.get("method", "random")
        limit = config.get("limit") or selection.get("limit", 10)

        context = self._get_context_by_method(method, limit, user_directive)
        system_base = self._load_prompt("system_base.md")
        agent_instruction = self._load_prompt("agent_insight.md")

        custom_task = (
            f"{system_base}\n\n{agent_instruction}\n\n"
            f"## 分析指令\n{config.get('system_prompt', 'Analyze this.')}\n\n"
            f"## 知識背景\n{context}"
        )

        return self.llm.answer_query(
            query_content=(
                "根據設定的策略進行深度分析。\n"
                f"使用者額外補充：{user_directive if user_directive else '無'}"
            ),
            wiki_context="",
            custom_instruction=custom_task,
            forced_template=resolved_template,
            default_template="insight-rpt",
        )

    # ── Pipeline: Monte Carlo ────────────────────────────────────────

    def _run_montecarlo(self, config: dict, user_directive: str, resolved_template: str | None = None) -> str:
        num_sparks = config.get("num_sparks", 6)
        top_k = config.get("top_k", 3)
        num_rounds = config.get("num_rounds", 3)
        limit = config.get("limit", 10)
        chunks_per_book = config.get("chunks_per_book", 5)

        from core.ui import ui

        target_titles = [m.split("|")[0].strip() for m in _WIKILINK_RE.findall(user_directive)]

        # Fetch the full metadata table ONCE up front. Both _get_all_documents
        # and _resolve_target_doc need it; previously each call re-issued the
        # same scan, which dominated runtime on large vaults.
        title_meta = self._fetch_all_title_meta()
        all_docs = self._get_all_documents(
            limit * 5, chunks_per_book=chunks_per_book, title_meta=title_meta,
        )
        if len(all_docs) < 2:
            logging.warning("Monte Carlo: not enough documents for pairing, falling back to single.")
            return self._run_single(config, user_directive, resolved_template)

        tried_pairs: set[tuple[str, str]] = set()
        round_results: list[dict] = []

        for round_num in range(1, num_rounds + 1):
            ui.set_status(f"Monte Carlo Round {round_num}/{num_rounds}: Generating pairs...")
            logging.info(f"Monte Carlo: starting round {round_num}/{num_rounds}")

            if target_titles:
                pairs = self._build_targeted_pairs(
                    all_docs, target_titles, num_sparks,
                    exclude=tried_pairs, title_meta=title_meta,
                )
            else:
                pairs = self._sample_random_pairs(all_docs, num_sparks, exclude=tried_pairs)

            if not pairs:
                logging.info(f"Monte Carlo round {round_num}: no new pairs, stopping.")
                break

            for a, b in pairs:
                tried_pairs.add(self._pair_key(a, b))

            seeds = self._spark_pairs(pairs, config, round_num, ui)
            if not seeds:
                logging.info(f"Monte Carlo round {round_num}: no seeds generated.")
                round_results.append({
                    "round": round_num, "pairs_tried": len(pairs),
                    "seeds": 0, "winners": [], "expanded": [],
                })
                continue

            seeds.sort(key=lambda s: s.get("novelty_score", 0), reverse=True)
            winners = seeds[:top_k]
            expanded = self._expand_winners(winners, config, round_num, ui)

            round_results.append({
                "round": round_num,
                "pairs_tried": len(pairs),
                "seeds": len(seeds),
                "winners": winners,
                "expanded": expanded,
                "all_scores": [s.get("novelty_score", 0) for s in seeds],
            })

            logging.info(
                f"Monte Carlo round {round_num}: {len(pairs)} pairs → "
                f"{len(seeds)} seeds → top {len(winners)} "
                f"(scores: {[s.get('novelty_score', 0) for s in winners]})"
            )

        if not any(r.get("expanded") for r in round_results):
            logging.warning("Monte Carlo: no insights from any round, falling back to single.")
            return self._run_single(config, user_directive, resolved_template)

        ui.set_status("Monte Carlo: cross-round evaluation & synthesis...")
        return self._synthesize_multi_round(round_results, config, user_directive, resolved_template)

    def _spark_pairs(self, pairs, config, round_num, ui):
        seeds = []
        for i, (doc_a, doc_b) in enumerate(pairs):
            ui.set_status(f"Round {round_num}: Sparking {i + 1}/{len(pairs)}...")
            seed = self._spark_seed(doc_a, doc_b, config)
            if seed:
                seed["round"] = round_num
                seeds.append(seed)
        return seeds

    def _expand_winners(self, winners, config, round_num, ui):
        expanded = []
        for i, seed in enumerate(winners):
            ui.set_status(f"Round {round_num}: Expanding {i + 1}/{len(winners)}...")
            expanded.append(self._expand_seed(seed, config))
        return expanded

    # ── RAG-backed retrieval ─────────────────────────────────────────

    def _fetch_all_title_meta(self) -> dict[str, dict]:
        """Single metadata scan: title → first-seen metadata dict.

        Used by both `_get_all_documents` and `_resolve_target_doc` so a
        Monte Carlo run with N targets doesn't issue N+1 full scans.
        """
        try:
            results = self.rag.all_chunks(include=("metadatas",))
            metadatas = results.get("metadatas", []) or []
        except Exception as e:
            logging.error(f"Monte Carlo: failed to fetch metadata: {e}")
            return {}

        out: dict[str, dict] = {}
        for meta in metadatas:
            title = (meta or {}).get("title", "Unknown")
            out.setdefault(title, meta)
        return out

    def _get_all_documents(
        self,
        max_docs: int = 50,
        chunks_per_book: int = 5,
        title_meta: dict | None = None,
    ) -> list[dict]:
        """Sample books uniformly, then up to `chunks_per_book` concept chunks per book.

        Book-level uniform sampling prevents a 143-Part book from drowning
        out a 5-Part note. Within a sampled book, multiple chunks are drawn
        from raw Parts (preferred) so the carrier pool reflects concept-level
        diversity rather than just one distilled summary per book.
        """
        if title_meta is None:
            title_meta = self._fetch_all_title_meta()
        if not title_meta:
            return []

        book_to_titles: dict[str, list[str]] = {}
        for title in title_meta:
            book_to_titles.setdefault(self._book_root(title), []).append(title)

        book_roots = list(book_to_titles)
        target_books = max(1, max_docs // max(chunks_per_book, 1))
        if len(book_roots) > target_books:
            book_roots = random.sample(book_roots, target_books)

        docs = []
        for book in book_roots:
            docs.extend(
                self._docs_from_book(book_to_titles[book], title_meta, chunks_per_book)
            )

        logging.info(
            f"Monte Carlo: {len(title_meta)} titles across {len(book_to_titles)} books, "
            f"loaded {len(docs)} chunks from {len(book_roots)} sampled books "
            f"(chunks_per_book={chunks_per_book})"
        )
        return docs

    @staticmethod
    def _book_root(title: str) -> str:
        """Strip `(Part N)` / `(Stitched)` / `(Synthesis)` so book parts collapse."""
        return _BOOK_SUFFIX_RE.sub('', title or '').strip()

    def _docs_from_book(
        self,
        book_titles: list[str],
        title_meta: dict,
        k: int,
    ) -> list[dict]:
        """Return up to k chunk docs from one book.

        Tier priority: raw Parts > (Synthesis) > (Stitched). Raw Parts win
        because they preserve unrefined concepts; the distilled tiers compress
        many concepts into one view and dampen collision novelty.
        """
        stitched = [t for t in book_titles if _STITCHED_SUFFIX_RE.search(t)]
        synthesis = [t for t in book_titles if _SYNTHESIS_SUFFIX_RE.search(t)]
        stitched_set = set(stitched)
        synthesis_set = set(synthesis)
        parts = [t for t in book_titles if t not in stitched_set and t not in synthesis_set]

        tier = parts or synthesis or stitched
        if not tier:
            return []

        chosen_titles = random.sample(tier, min(k, len(tier)))
        docs = []
        for t in chosen_titles:
            tags = self._parse_stored_tags(title_meta[t].get("tags", ""))
            doc = self._doc_from_rag_title(t, tags=tags)
            if doc:
                docs.append(doc)
        return docs

    def _doc_from_rag_title(self, title: str, tags: list[str] | None = None) -> dict | None:
        """Fetch one representative chunk for an exact indexed title."""
        try:
            chunk_results = self.rag.chunks_by_title(title, include=("documents", "metadatas"))
        except Exception as e:
            logging.debug(f"Monte Carlo: failed to fetch chunk for '{title}': {e}")
            return None

        chunk_docs = chunk_results.get("documents", []) or []
        if not chunk_docs:
            return None
        metadatas = chunk_results.get("metadatas", []) or []
        if tags is None:
            tags = self._parse_stored_tags((metadatas[0] if metadatas else {}).get("tags", ""))
        return {
            "title": title,
            "content": random.choice(chunk_docs)[:2000],
            "tags": tags,
        }

    # ── Sampling ─────────────────────────────────────────────────────

    def _sample_random_pairs(self, docs: list[dict], num_pairs: int, exclude: set | None = None) -> list[tuple]:
        pairs = []
        exclude = exclude or set()
        max_attempts = num_pairs * 4

        for attempt in range(1, max_attempts + 1):
            if len(pairs) >= num_pairs:
                break
            a, b = random.sample(docs, 2)
            key = self._pair_key(a, b)
            if key in exclude:
                continue

            tags_a = set(a.get("tags", []))
            tags_b = set(b.get("tags", []))
            overlap = len(tags_a & tags_b) / max(len(tags_a | tags_b), 1)

            if overlap < 0.3 or attempt > num_pairs * 2:
                pairs.append((a, b))

        return pairs

    @staticmethod
    def _normalize_title(title: str) -> str:
        return (title or "").split("|", 1)[0].strip().lower()

    def _target_match_score(self, requested_title: str, candidate_title: str) -> int:
        requested = self._normalize_title(requested_title)
        candidate = self._normalize_title(candidate_title)
        if not requested or not candidate:
            return 0
        if candidate == requested:
            return 100
        if candidate == f"{requested} (stitched)":
            return 95
        if candidate == f"{requested} (synthesis)":
            return 90
        if requested in candidate or candidate in requested:
            return 60
        return 0

    def _resolve_target_doc(
        self,
        requested_title: str,
        all_docs: list[dict],
        title_meta: dict | None = None,
    ) -> dict | None:
        """Resolve one [[target]] to a representative document.

        Prefer exact / Stitched / Synthesis matches across the full RAG index,
        not only the sampled pool — otherwise a requested book that wasn't
        sampled gets silently dropped.
        """
        best_doc, best_score = None, 0

        for doc in all_docs:
            score = self._target_match_score(requested_title, doc.get("title", ""))
            if score > best_score:
                best_doc, best_score = doc, score

        if title_meta is None:
            title_meta = self._fetch_all_title_meta()

        for title, meta in title_meta.items():
            score = self._target_match_score(requested_title, title)
            if score > best_score:
                best_score = score
                best_doc = self._doc_from_rag_title(
                    title,
                    tags=self._parse_stored_tags((meta or {}).get("tags", "")),
                )

        if best_doc:
            logging.info(f"Monte Carlo: target '{requested_title}' resolved to '{best_doc['title']}'")
            return best_doc

        try:
            similar = self.rag.query_similar_notes(requested_title, top_k=1)
        except Exception as e:
            logging.debug(f"Monte Carlo: semantic search for '{requested_title}' failed: {e}")
            similar = []

        if similar:
            logging.info(f"Monte Carlo: target '{requested_title}' resolved via semantic search")
            return {"title": requested_title, "content": similar[0][:2000], "tags": []}

        logging.warning(f"Monte Carlo: target '{requested_title}' not found.")
        return None

    def _build_targeted_pairs(
        self,
        all_docs: list[dict],
        target_titles: list[str],
        num_pairs: int,
        exclude: set | None = None,
        title_meta: dict | None = None,
    ) -> list[tuple]:
        exclude = exclude or set()

        target_docs = []
        seen_target_titles: set[str] = set()
        for title in target_titles:
            doc = self._resolve_target_doc(title, all_docs, title_meta=title_meta)
            if doc and doc["title"] not in seen_target_titles:
                target_docs.append(doc)
                seen_target_titles.add(doc["title"])

        if not target_docs:
            logging.warning(f"Monte Carlo: targets {target_titles} not found, falling back to random.")
            return self._sample_random_pairs(all_docs, num_pairs, exclude=exclude)

        target_title_set = {doc["title"] for doc in target_docs}
        other_docs = [
            doc for doc in all_docs
            if doc["title"] not in target_title_set
            and not any(self._target_match_score(t, doc["title"]) for t in target_titles)
        ]

        pairs: list[tuple] = []

        if len(target_docs) >= 2:
            all_combos = list(combinations(target_docs, 2))
            random.shuffle(all_combos)
            for a, b in all_combos:
                if self._pair_key(a, b) in exclude:
                    continue
                pairs.append((a, b))
                if len(pairs) >= num_pairs:
                    break

            if other_docs and len(pairs) < num_pairs:
                shuffled_targets = list(target_docs)
                random.shuffle(shuffled_targets)
                for target in shuffled_targets:
                    if len(pairs) >= num_pairs:
                        break
                    neighbor = random.choice(other_docs)
                    if self._pair_key(target, neighbor) not in exclude:
                        pairs.append((target, neighbor))
        else:
            target = target_docs[0]
            if other_docs:
                candidates = random.sample(other_docs, min(len(other_docs), num_pairs * 2))
                for other in candidates:
                    if len(pairs) >= num_pairs:
                        break
                    if self._pair_key(target, other) not in exclude:
                        pairs.append((target, other))
            if not pairs:
                # Last-resort partner for the target. Respect the exclude set
                # and avoid self-pairing (audit R7-D): a blind random.choice
                # could re-emit an already-explored pair and break cross-round
                # dedup. If nothing fresh exists, return empty — the caller
                # treats that as a stop signal.
                target = target_docs[0]
                for other in random.sample(all_docs, len(all_docs)):
                    if other["title"] == target["title"]:
                        continue
                    if self._pair_key(target, other) not in exclude:
                        pairs.append((target, other))
                        break

        return pairs[:num_pairs]

    # ── Spark / Expand ───────────────────────────────────────────────

    def _spark_seed(self, doc_a: dict, doc_b: dict, config: dict) -> dict | None:
        system_prompt = (
            "You are an Epistemologist evaluating random idea combinations for novel cross-domain insights.\n"
            "Return ONLY a JSON object with this schema:\n"
            '{"idea": "2-3 sentence insight seed", "novelty_score": 1-10, '
            '"reasoning": "why this combination is interesting", '
            '"source_a": "title of note A", "source_b": "title of note B"}\n\n'
            "Scoring guide:\n"
            "- 8-10: Genuinely surprising cross-domain connection with practical implications\n"
            "- 5-7: Interesting analogy but somewhat expected\n"
            "- 1-4: Superficial or forced connection\n"
            "Be HONEST with scoring. Most random pairs deserve 3-5. Reserve 8+ for truly novel connections."
        )
        user_msg = (
            f"## Note A: {doc_a['title']}\n"
            f"Tags: {', '.join(doc_a.get('tags', []))}\n"
            f"{doc_a['content'][:1500]}\n\n"
            f"## Note B: {doc_b['title']}\n"
            f"Tags: {', '.join(doc_b.get('tags', []))}\n"
            f"{doc_b['content'][:1500]}\n\n"
            f"Find a novel, non-obvious connection between these two knowledge fragments."
        )

        try:
            # JSON output: opt out of the template/persona axes, or the
            # default wiki-note template overrides the JSON instruction.
            raw = self.llm.answer_query(
                query_content=user_msg,
                wiki_context="",
                custom_instruction=system_prompt,
                temperature=self.TEMP_SPARK,
                forced_template="none",
                persona="none",
            )
            seed = extract_json_object(raw)
        except Exception as e:
            logging.warning(f"Monte Carlo spark failed: {e}")
            return None

        if not (seed and seed.get("idea")):
            return None

        seed.setdefault("novelty_score", 5)
        seed.setdefault("source_a", doc_a["title"])
        seed.setdefault("source_b", doc_b["title"])
        logging.info(
            f"  Spark: score={seed['novelty_score']}, "
            f"pair=({doc_a['title'][:30]} × {doc_b['title'][:30]})"
        )
        return seed

    def _should_ground(self, idea: str) -> bool:
        """Deterministically pick GROUND_FRACTION of seeds to ground — the rest
        stay cold so the echo-chamber canary has a control group. Hash-based, so
        it's reproducible and testable (not random)."""
        from core.config import CORTEX_GROUNDED_INSIGHT_ENABLED, CORTEX_GROUND_FRACTION
        if not CORTEX_GROUNDED_INSIGHT_ENABLED:
            return False
        # M4: the fraction may be auto-tuned against the echo canary; get_tuned
        # returns the config default unless AUTOTUNE_ENABLED has nudged it.
        from services.autotune_store import get_tuned
        fraction = get_tuned("CORTEX_GROUND_FRACTION", CORTEX_GROUND_FRACTION)
        import hashlib
        bucket = int(hashlib.sha256(idea.encode("utf-8")).hexdigest(), 16) % 100
        return bucket < int(round(fraction * 100))

    def _cortex_priors(self, idea: str) -> list:
        """Relevant Cortex claims to use as DIALECTICAL priors. Falsifiability-
        gated (defense 3): unfalsifiable beliefs can't be wrong, so they only
        self-reinforce — never let them anchor generation. Returns CortexPages."""
        from core.config import (
            CORTEX_DIR, CORTEX_GROUND_MIN_FALSIFIABILITY, CORTEX_GROUND_TOP_K,
        )
        from services.cortex_store import load_all_pages
        falsifiable = [
            p for p in load_all_pages(CORTEX_DIR)
            if p.claim.strip() and p.status in ("active", "dormant")
            and p.falsifiability is not None
            and p.falsifiability >= CORTEX_GROUND_MIN_FALSIFIABILITY
        ]
        if len(falsifiable) <= CORTEX_GROUND_TOP_K:
            return falsifiable
        # Rank by relevance only when there are more than we'll use.
        from services.cortex_recall import recall_claims
        ranked = recall_claims(self.rag, idea, cortex_dir=CORTEX_DIR,
                               top_k=CORTEX_GROUND_TOP_K, min_score=0.0)
        ids = {p.claim_id for _, p in ranked}
        ranked_pages = [p for _, p in ranked]
        # Fall back to the unranked falsifiable set if recall returned nothing.
        return ranked_pages or falsifiable[:CORTEX_GROUND_TOP_K]

    def _grounding_block(self, priors: list) -> str:
        lines = [
            "## 你對相關主題已有的信念（請挑戰，不要附和）",
            "",
        ]
        for p in priors:
            fz = "—" if p.falsifiability is None else f"{p.falsifiability:.2f}"
            line = f"- {p.claim.strip()}（可反駁性 {fz}"
            if p.falsifier:
                line += f"；反例：{p.falsifier}"
            lines.append(line + "）")
        lines += [
            "",
            "這份新分析在哪裡【推翻 / 修正 / 延伸】上述既有信念？最有價值的輸出是**張力與反例**，"
            "不是複述。若新材料只是附和既有信念，明說「無新增」而不要硬湊。",
            "",
        ]
        return "\n".join(lines)

    def _expand_seed(self, seed: dict, config: dict) -> dict:
        idea = seed.get("idea", "")
        try:
            evidence_docs = self.rag.query_similar_notes(idea, top_k=5)
        except Exception as e:
            logging.debug(f"Monte Carlo: evidence search failed: {e}")
            evidence_docs = []
        evidence_context = "\n\n".join(evidence_docs) if evidence_docs else "(No supporting evidence found.)"

        system_base = self._load_prompt("system_base.md")
        agent_instruction = self._load_prompt("agent_insight.md")

        # Cortex-grounded insight (Phase 5 F1, flag-gated, default OFF). Inject
        # relevant falsifiable claims as DIALECTICAL priors — to challenge, not
        # confirm. grounded_on records provenance for the consolidation firewall.
        grounded_on: list[str] = []
        grounding_section = ""
        if self._should_ground(idea):
            priors = self._cortex_priors(idea)
            if priors:
                grounded_on = [p.claim_id for p in priors]
                grounding_section = self._grounding_block(priors)
                # Accumulate for the report frontmatter so consolidation's
                # firewall knows which claims this insight was grounded on.
                if not hasattr(self, "_grounded_on_acc"):
                    self._grounded_on_acc = set()
                self._grounded_on_acc.update(grounded_on)

        expand_prompt = (
            f"{system_base}\n\n{agent_instruction}\n\n"
            f"{grounding_section}"
            f"## 任務\n"
            f"You are developing a winning insight seed into a full analysis.\n\n"
            f"## Seed Insight\n"
            f"**Idea**: {idea}\n"
            f"**Novelty Score**: {seed.get('novelty_score', '?')}/10\n"
            f"**Reasoning**: {seed.get('reasoning', '')}\n"
            f"**Sources**: [[{seed.get('source_a', '')}]] × [[{seed.get('source_b', '')}]]\n\n"
            f"## Supporting Evidence (from semantic search)\n{evidence_context}\n\n"
            f"## Instructions\n"
            f"Develop this seed into a structured analysis section with:\n"
            f"1. A clear thesis statement grounded in the source notes\n"
            f"2. Key arguments supported by evidence from the knowledge base\n"
            f"3. Practical implications and actionable takeaways\n"
            f"4. A Mermaid diagram if it adds clarity\n"
            f"Cite source notes using [[title]] notation."
        )

        expansion_text = self.llm.answer_query(
            query_content=f"Expand this insight seed: {idea}",
            wiki_context="",
            custom_instruction=expand_prompt,
            temperature=self.TEMP_EXPAND,
        )

        return {
            **seed,
            "expanded": expansion_text,
            "evidence_sources": [doc.split("\n")[0] for doc in evidence_docs[:3]] if evidence_docs else [],
            "grounded_on": grounded_on,
        }

    # ── Multi-round synthesis ────────────────────────────────────────

    def _synthesize_multi_round(
        self,
        round_results: list[dict],
        config: dict,
        user_directive: str,
        resolved_template: str | None = None,
    ) -> str:
        num_rounds = len(round_results)
        scorecard = self._build_scorecard(round_results)
        round_sections, all_expanded = self._build_round_sections(round_results)
        evaluation = self._cross_round_evaluation(
            scorecard, all_expanded, num_rounds, user_directive, resolved_template
        )

        total_pairs = sum(r["pairs_tried"] for r in round_results)
        total_seeds = sum(r.get("seeds", 0) for r in round_results)
        total_winners = len(all_expanded)

        return (
            f"# 🎲 Monte Carlo Insight Exploration ({num_rounds} Rounds)\n\n"
            f"## 📊 Round Scorecard\n\n{scorecard}\n\n"
            f"> **Exploration scope**: {total_pairs} pairs tried → {total_seeds} seeds → "
            f"{total_winners} winners expanded across {num_rounds} rounds\n\n"
            f"---\n\n"
            f"## 🏆 Cross-Round Evaluation\n\n{evaluation}\n\n"
            f"---\n\n"
            f"## 🔬 Per-Round Details\n\n"
            + "\n\n---\n\n".join(round_sections)
        )

    @staticmethod
    def _build_scorecard(round_results: list[dict]) -> str:
        rows = []
        for r in round_results:
            if not r.get("winners"):
                rows.append(f"| {r['round']} | {r['pairs_tried']} | 0 | — | — | — |")
                continue
            top = r["winners"][0]
            scores = r.get("all_scores", [top.get("novelty_score", 0)])
            avg = sum(scores) / max(len(scores), 1)
            rows.append(
                f"| {r['round']} | {r['pairs_tried']} | {r['seeds']} | {avg:.1f} | "
                f"{top.get('novelty_score', '?')}/10 | "
                f"[[{top.get('source_a', '?')}]] × [[{top.get('source_b', '?')}]] |"
            )
        return (
            "| Round | Pairs | Seeds | Avg Score | Best | Top Connection |\n"
            "|:-----:|:-----:|:-----:|:---------:|:----:|:---------------|\n"
            + "\n".join(rows)
        )

    @staticmethod
    def _build_round_sections(round_results: list[dict]) -> tuple[list[str], list[dict]]:
        round_sections = []
        all_expanded: list[dict] = []
        for r in round_results:
            expanded = r.get("expanded", [])
            if not expanded:
                round_sections.append(f"### Round {r['round']}\n\n_(No insights generated this round.)_")
                continue

            insights = []
            for i, seed in enumerate(expanded, 1):
                insights.append(
                    f"#### 🌟 R{r['round']}-{i} (Score: {seed.get('novelty_score', '?')}/10)\n"
                    f"**Connection**: [[{seed.get('source_a', '')}]] × [[{seed.get('source_b', '')}]]\n\n"
                    f"{seed.get('expanded', seed.get('idea', ''))}"
                )
                all_expanded.append(seed)

            round_sections.append(f"### Round {r['round']}\n\n" + "\n\n---\n\n".join(insights))
        return round_sections, all_expanded

    def _cross_round_evaluation(
        self,
        scorecard: str,
        all_expanded: list[dict],
        num_rounds: int,
        user_directive: str,
        resolved_template: str | None,
    ) -> str:
        winner_lines = [
            f"- [R{s.get('round', '?')}, score={s.get('novelty_score', '?')}] "
            f"({s.get('source_a', '?')} × {s.get('source_b', '?')}): {s.get('idea', '?')}"
            for s in all_expanded
        ]
        eval_prompt = (
            f"You are evaluating {num_rounds} rounds of Monte Carlo insight exploration.\n\n"
            f"## Scorecard\n{scorecard}\n\n"
            f"## All Winners Across Rounds\n" + "\n".join(winner_lines) + "\n\n"
            f"## Task\n"
            f"Write a cross-round evaluation (3-5 paragraphs) that:\n"
            f"1. Compares the quality and novelty across rounds\n"
            f"2. Identifies the single **global champion** insight and explains why it's the best\n"
            f"3. Notes which rounds were most/least productive and why\n"
            f"4. Identifies meta-patterns that emerged across rounds\n"
            f"5. Gives 2-3 concrete action items for the knowledge base owner\n\n"
            f"Output language: {self.llm._get_lang_hint()}\n"
            f"User context: {user_directive or '(none)'}"
        )
        return self.llm.answer_query(
            query_content="Evaluate the multi-round Monte Carlo exploration.",
            wiki_context="",
            custom_instruction=eval_prompt,
            temperature=self.TEMP_SYNTHESIZE,
            forced_template=resolved_template,
            default_template="insight-rpt",
            persona="none",
            operation="synthesize",
        )

    # ── Cross-Strategy Synthesis ────────────────────────────────────

    def _extract_seeds_from_section(self, section_content: str, strategy_name: str) -> list[dict]:
        extract_prompt = (
            "Extract the 2-3 most important insight claims from this analysis section.\n"
            "Return a JSON array of objects: [{\"claim\": \"...\", \"strategy\": \"...\"}]\n"
            "Each claim should be a single declarative sentence."
        )
        try:
            # JSON output: same template/persona opt-out as the spark call.
            raw = self.llm.answer_query(
                query_content=section_content[:3000],
                wiki_context="",
                custom_instruction=extract_prompt,
                temperature=0.1,
                forced_template="none",
                persona="none",
            )
            seeds = extract_json_array(raw)
        except Exception as e:
            logging.debug(f"Seed extraction failed for {strategy_name}: {e}")
            seeds = []

        if not seeds:
            return [{"claim": section_content[:200], "strategy": strategy_name}]
        for seed in seeds:
            seed["strategy"] = strategy_name
        return seeds

    def _cross_strategy_synthesis(self, all_seeds: list[dict], user_directive: str) -> str:
        if not all_seeds:
            return "(No cross-strategy patterns detected.)"

        seed_text = "\n".join(
            f"- [{s.get('strategy', '?')}] {s.get('claim', '?')}"
            for s in all_seeds[:15]
        )
        synthesis_prompt = (
            f"You have key insights extracted from {len(set(s.get('strategy') for s in all_seeds))} "
            f"different analytical strategies (montecarlo, meta-methods, tag-cluster, recency, islands).\n\n"
            f"## Insight Seeds from All Strategies\n{seed_text}\n\n"
            f"## Task\n"
            f"Identify 2-3 **meta-patterns** that appear across MULTIPLE strategies.\n"
            f"These are higher-order insights that no single strategy would have found alone.\n"
            f"For each meta-pattern:\n"
            f"1. State the pattern clearly\n"
            f"2. Name which strategies contributed to it\n"
            f"3. Explain why this cross-pollination matters\n"
            f"4. Give one concrete action item\n\n"
            f"Output language: {self.llm._get_lang_hint()}\n"
            f"User context: {user_directive or '(none)'}"
        )
        return self.llm.answer_query(
            query_content="Perform cross-strategy synthesis.",
            wiki_context="",
            custom_instruction=synthesis_prompt,
            temperature=self.TEMP_SYNTHESIZE,
            persona="none",
            operation="synthesize",
        )

    # ── Context Retrieval ────────────────────────────────────────────

    def _get_context_by_method(self, method: str, limit: int, user_directive: str = "") -> str:
        target_file = None
        target_tag = None
        if file_matches := _WIKILINK_RE.findall(user_directive):
            target_file = file_matches[0].split("|")[0].strip()
            if target_file.lower().endswith(".md"):
                target_file = target_file[:-3]
        if tag_matches := _HASHTAG_RE.findall(user_directive):
            target_tag = tag_matches[0]

        if method == "recency":
            return self._get_recent_context(limit)
        if method == "tags":
            return self._get_tag_cluster_context(limit, target_tag)
        if method == "islands":
            return self._get_island_context(limit, target_file)
        return self._get_random_sample_context(limit, target_file)

    def _get_recent_context(self, limit: int) -> str:
        try:
            results = self.rag.all_chunks()
            if not results.get("documents"):
                return "No documents found."
            docs_with_meta = list(zip(results["documents"], results["metadatas"]))
            docs_with_meta.sort(key=lambda x: x[1].get("timestamp", ""), reverse=True)
            pool_size = min(len(docs_with_meta), limit * 3)
            selection = random.sample(docs_with_meta[:pool_size], min(pool_size, limit))
            return "\n---\n".join(x[0] for x in selection)
        except Exception as e:
            logging.debug(f"InsightAgent: recent context retrieval failed: {e}")
            return "No recent data found."

    def _get_tag_cluster_context(self, limit: int, target_tag: str | None = None) -> str:
        try:
            results = self.rag.all_chunks()
            if not results.get("metadatas"):
                return self._get_random_sample_context(limit)

            if not target_tag:
                # Count tags per book, not per chunk — otherwise a 1000-chunk
                # textbook makes every one of its single-book tags trivially
                # pass `c >= 2`, and `interesting` ends up dominated by tags
                # that only exist in one book (defeating "cluster").
                tag_books: dict[str, set[str]] = {}
                for meta in results["metadatas"]:
                    book = self._book_root(meta.get("title", ""))
                    for tag in self._parse_stored_tags(meta.get("tags", "")):
                        if tag.lower() in _SYSTEM_TAGS:
                            continue
                        tag_books.setdefault(tag, set()).add(book)
                if not tag_books:
                    return self._get_random_sample_context(limit)
                interesting = [t for t, books in tag_books.items() if len(books) >= 2]
                target_tag = random.choice(interesting if interesting else list(tag_books))

            cluster_docs = [
                doc for doc, meta in zip(results["documents"], results["metadatas"])
                if target_tag in self._parse_stored_tags(meta.get("tags", ""))
            ]
            if not cluster_docs:
                return self._get_random_sample_context(limit)
            selection = random.sample(cluster_docs, min(len(cluster_docs), limit))
            return f"Focusing on Cluster: #{target_tag}\n\n" + "\n---\n".join(selection)
        except Exception as e:
            logging.debug(f"InsightAgent: tag cluster retrieval failed: {e}")
            return self._get_random_sample_context(limit)

    def _get_island_context(self, limit: int, target_island: str | None = None) -> str:
        if target_island:
            try:
                results = self.rag.chunks_by_title(target_island, limit=limit)
            except Exception as e:
                logging.debug(f"InsightAgent: targeted island fetch failed: {e}")
                results = {}
            docs = results.get("documents", []) if results else []
            if docs:
                return f"Analysis target (Knowledge Island): [[{target_island}]]\n\n" + "\n---\n".join(docs)

        try:
            results = self.rag.all_chunks()
            if not results.get("documents"):
                return self._get_random_sample_context(limit)

            all_docs_meta = list(zip(results["documents"], results["metadatas"]))
            tag_to_titles: dict[str, set[str]] = {}
            title_to_entry: dict[str, tuple[str, list[str]]] = {}

            for doc, meta in all_docs_meta:
                title = meta.get("title", "Unknown")
                tags = self._parse_stored_tags(meta.get("tags", ""))
                title_to_entry[title] = (doc, tags)
                for tag in tags:
                    tag_to_titles.setdefault(tag, set()).add(title)

            connectivity = {}
            for title, (_, tags) in title_to_entry.items():
                connected = set()
                for tag in tags:
                    connected.update(tag_to_titles.get(tag, set()))
                connected.discard(title)
                connectivity[title] = len(connected)

            isolated = sorted(connectivity, key=connectivity.get)
            island_titles = isolated[:limit]
            if not island_titles:
                return self._get_random_sample_context(limit)

            island_docs = []
            for title in island_titles:
                if title in title_to_entry:
                    doc, tags = title_to_entry[title]
                    island_docs.append(
                        f"### 🏝️ [[{title}]] (connectivity: {connectivity[title]})\n"
                        f"Tags: {', '.join(tags) if tags else '(none)'}\n\n{doc}"
                    )
            return "Knowledge Islands Detected (lowest connectivity scores):\n\n" + "\n---\n".join(island_docs)
        except Exception as e:
            logging.debug(f"InsightAgent: island detection failed: {e}")
            return self._get_random_sample_context(limit)

    def _get_random_sample_context(self, limit: int, target_file: str | None = None) -> str:
        try:
            if target_file:
                results = self.rag.chunks_by_title(target_file)
                docs = results.get("documents", [])
                if docs:
                    return f"Analysis target: [[{target_file}]]\n\n" + "\n---\n".join(docs)
            results = self.rag.all_chunks()
            docs = results.get("documents", [])
            if not docs:
                return "Empty KB."
            selection = random.sample(docs, min(len(docs), limit))
            return "\n---\n".join(selection)
        except Exception as e:
            logging.debug(f"InsightAgent: random sample retrieval failed: {e}")
            return "Error retrieving context."

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_stored_tags(tags_str: str) -> list[str]:
        """Parse the ',tag1,tag2,tag3,' format used in ChromaDB metadata."""
        if not tags_str:
            return []
        return [t.strip() for t in tags_str.split(",") if t.strip()]
