"""InsightAgent — routing + orchestration only (P2f).

The seven responsibility mixins in agents/insight/ carry the implementation
(strategy loading, planner flow, monte carlo, doc retrieval, pairing, context
assembly, report output). This file keeps the public entry points and the
two pipeline drivers. Composition-style injection is P3 work.
"""

import logging
import random
import re

from agents.base_agent import BaseAgent
from agents.insight.context_assembly import ContextAssemblyMixin
from agents.insight.doc_retrieval import DocRetrievalMixin
from agents.insight.monte_carlo import MonteCarloMixin
from agents.insight.pairing import PairingMixin
from agents.insight.planner_flow import PlannerFlowMixin
from agents.insight.report_output import ReportOutputMixin
from agents.insight.strategy_loading import StrategyLoadingMixin
from core.config import SKILLS_DIR, WIKI_VAULT_DIR

_WIKILINK_RE = re.compile(r"\[\[(.*?)\]\]")


class InsightAgent(
    StrategyLoadingMixin,
    PlannerFlowMixin,
    MonteCarloMixin,
    DocRetrievalMixin,
    PairingMixin,
    ContextAssemblyMixin,
    ReportOutputMixin,
    BaseAgent,
):
    """Generate insights from the knowledge base.

    Two pipelines:
      - 'single':     One-shot LLM call with strategy-specific context.
      - 'montecarlo': Multi-round explore → score → filter → expand → synthesize.
    """

    TEMP_SPARK = 0.9
    TEMP_EXPAND = 0.5
    TEMP_SYNTHESIZE = 0.3

    def __init__(self, llm, rag=None):
        super().__init__(llm, rag)
        self.insights_dir = WIKI_VAULT_DIR / "Insights"
        self.insights_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir = SKILLS_DIR
        self.strategies = self._load_strategies()

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

    def generate_insight(
        self,
        strategy_id: str,
        user_directive: str = "",
        forced_template: str | None = None,
        target_titles: list[str] | None = None,
    ) -> str:
        if strategy_id not in self.strategies:
            if not self.strategies:
                return "💧 Error: No strategies found."
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
        self._grounded_on_acc = set()  # F1: claims this run grounded on (for frontmatter)

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

        _, full_markdown = self._write_report(f"{config['name']}", report_content, "ins", meta)
        self._mirror_to_insights(
            full_markdown,
            requested_cmd=f"insight-{strategy_id}",
            related_titles=target_titles,
        )
        return full_markdown

    def generate_full_insight(
        self,
        user_directive: str = "",
        forced_template: str | None = None,
        target_titles: list[str] | None = None,
    ) -> str:
        """Run all strategies, then perform a cross-strategy synthesis."""
        section_results = []
        insight_seeds = []
        self._grounded_on_acc = set()  # F1: accumulates across all strategies' seeds

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

        _, full_markdown = self._write_report("full", final_markdown, "ins", meta)
        self._mirror_to_insights(
            full_markdown,
            requested_cmd="full-insight",
            related_titles=target_titles,
        )
        return full_markdown

    def _run_single(
        self, config: dict, user_directive: str, resolved_template: str | None = None
    ) -> str:
        selection = config.get("selection", {})
        method = config.get("method") or selection.get("method", "random")
        limit = config.get("limit") or selection.get("limit", 10)

        context = self._get_context_by_method(method, limit, user_directive)
        system_base = self._load_prompt("system_base.md", required=True)
        agent_instruction = self._load_prompt("agent_insight.md", required=True)

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
