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

import logging
from dataclasses import dataclass
from datetime import date


@dataclass
class DailyInsightResult:
    status: str
    summary: str


def pick_rotation_strategy(available: dict, *, today: date | None = None) -> str:
    """Deterministic daily pick from the Scripture `insight_rotation` list.

    Cycles by date ordinal (no RNG — same day, same strategy, testable).
    Unknown names are skipped with a warning so a typo in Scripture degrades
    to the rest of the rotation instead of killing the nightly insight;
    an empty/fully-invalid rotation falls back to montecarlo."""
    from core.config import settings

    raw = getattr(settings, "INSIGHT_ROTATION", "") or "montecarlo"
    rotation = [s.strip() for s in raw.split(",") if s.strip()]
    unknown = [s for s in rotation if s not in available]
    if unknown:
        logging.warning(f"insight_rotation: unknown strategies skipped: {unknown}")
    valid = [s for s in rotation if s in available]
    if not valid:
        return "montecarlo"
    today = today or date.today()
    return valid[today.toordinal() % len(valid)]


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
    strategy = pick_rotation_strategy(insight_agent.strategies)
    insight_agent.generate_insight(
        strategy,
        user_directive=f"{occasion} doc-anchored insight. {links}",
        target_titles=targets,
    )
    return DailyInsightResult(
        "succeeded", f"Doc-anchored insight ({strategy}) generated for: {', '.join(targets)}."
    )
