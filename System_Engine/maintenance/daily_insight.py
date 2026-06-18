"""Daily insight generation — the doc-anchored montecarlo reflection.

Extracted from the scheduler's `daily_insight` closure so both the nightly
MaintenanceScheduler task and the daytime DaydreamPump can run the *same*
generation path (no duplicated logic). The seed sampler picks targets via the
interest+exploration policy; an empty pick falls back to a full insight.

The `occasion` label is woven into the user_directive purely for traceability
(it lands in the insight frontmatter / trace) — "Scheduled" reproduces the
original scheduler wording verbatim; the pump passes "Daydream …".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DailyInsightResult:
    status: str
    summary: str


def run_daily_insight(llm, rag, *, occasion: str = "Scheduled") -> DailyInsightResult:
    # Doc-anchored by default: targeted montecarlo over seeds picked by the
    # interest+exploration sampler. Vault-wide rumination measured poorly
    # (80% broken links, refute coverage 0) and is demoted to the weekly task.
    from core.config import INSIGHT_SEED_TARGETS
    from services.seed_sampler import SeedSampler
    from agents.insight_agent import InsightAgent

    insight_agent = InsightAgent(llm, rag)
    sampler = SeedSampler(rag, getattr(llm, "trace_store", None))
    targets = sampler.select_targets(INSIGHT_SEED_TARGETS)
    if not targets:
        insight_agent.generate_full_insight(
            user_directive=f"{occasion} daily comprehensive reflection."
        )
        return DailyInsightResult("succeeded", "No seed targets; fell back to full insight.")
    links = " ".join(f"[[{t}]]" for t in targets)
    insight_agent.generate_insight(
        "montecarlo",
        user_directive=f"{occasion} doc-anchored insight. {links}",
        target_titles=targets,
    )
    return DailyInsightResult(
        "succeeded", f"Doc-anchored insight generated for: {', '.join(targets)}."
    )
