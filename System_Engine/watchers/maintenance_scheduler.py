"""Generic background scheduler for autonomous maintenance tasks."""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from agents.insight_agent import InsightAgent
from core.config import (
    MAINTENANCE_POLL_SECONDS,
    MAINTENANCE_SCHEDULER_ENABLED,
    MAINTENANCE_STATE_FILE,
    WIKI_VAULT_DIR,
    settings,
)
from core.state import global_busy_state
from core.ui import ui
from maintenance.retrieval_bench import run_retrieval_bench


@dataclass
class MaintenanceResult:
    status: str
    summary: str


@dataclass
class MaintenanceTask:
    name: str
    action: Callable[[], MaintenanceResult]
    interval_seconds: int | None = None
    daily: bool = False
    idle_required: bool = True
    window_start_hour: int | None = None
    window_end_hour: int | None = None
    intent: str = "maintenance"
    agent: str = "MaintenanceScheduler"
    initial_last_run_at: str | None = None


class MaintenanceScheduler(threading.Thread):
    def __init__(
        self,
        project_root: Path,
        llm,
        rag,
        tasks: list[MaintenanceTask] | None = None,
        *,
        state_file: Path = MAINTENANCE_STATE_FILE,
        poll_seconds: int = MAINTENANCE_POLL_SECONDS,
        enabled: bool = MAINTENANCE_SCHEDULER_ENABLED,
    ):
        super().__init__(daemon=True)
        self.project_root = project_root
        self.llm = llm
        self.rag = rag
        self.state_file = state_file
        self.poll_seconds = poll_seconds
        self.enabled = enabled
        self.tasks = tasks or self._default_tasks()
        self._state_lock = threading.Lock()
        self.state = self._load_state()
        self._seed_initial_task_state()

    def _default_tasks(self) -> list[MaintenanceTask]:
        insight_agent = InsightAgent(self.llm, self.rag)

        def daily_insight() -> MaintenanceResult:
            # Doc-anchored by default: targeted montecarlo over seeds picked
            # by the interest+exploration sampler. Vault-wide rumination
            # measured poorly (80% broken links, refute coverage 0) and is
            # demoted to the weekly task below.
            from core.config import INSIGHT_SEED_TARGETS
            from services.seed_sampler import SeedSampler

            sampler = SeedSampler(self.rag, getattr(self.llm, "trace_store", None))
            targets = sampler.select_targets(INSIGHT_SEED_TARGETS)
            if not targets:
                insight_agent.generate_full_insight(
                    user_directive="Scheduled daily comprehensive reflection."
                )
                return MaintenanceResult(
                    "succeeded", "No seed targets; fell back to full insight."
                )
            links = " ".join(f"[[{t}]]" for t in targets)
            insight_agent.generate_insight(
                "montecarlo",
                user_directive=f"Scheduled doc-anchored insight. {links}",
                target_titles=targets,
            )
            return MaintenanceResult(
                "succeeded", f"Doc-anchored insight generated for: {', '.join(targets)}."
            )

        def weekly_full_insight() -> MaintenanceResult:
            insight_agent.generate_full_insight(
                user_directive="Scheduled weekly comprehensive reflection."
            )
            return MaintenanceResult("succeeded", "Scheduled full insight generated.")

        def retrieval_bench() -> MaintenanceResult:
            from core.config import BENCH_HISTORY_FILE, FACET_INDEX_ENABLED, FROM_LLM_DIR
            result = run_retrieval_bench(
                self.rag,
                ab_facets=FACET_INDEX_ENABLED,
                history_path=BENCH_HISTORY_FILE,
                report_dir=FROM_LLM_DIR,
            )
            return MaintenanceResult(result.status, result.message)

        def bench_builder() -> MaintenanceResult:
            from maintenance.bench_builder import build_bench_cases
            result = build_bench_cases(self.rag, self.llm)
            return MaintenanceResult(result.status, result.message)

        def trace_prune() -> MaintenanceResult:
            trace_store = getattr(self.llm, "trace_store", None)
            if trace_store is None:
                return MaintenanceResult("skipped", "No trace store associated with LLM client.")
            trace_store.prune_old()
            return MaintenanceResult("succeeded", "SQLite trace logs pruned successfully.")

        def cortex_consolidation() -> MaintenanceResult:
            from core.config import CORTEX_CONSOLIDATION_ENABLED
            if not CORTEX_CONSOLIDATION_ENABLED:
                return MaintenanceResult("skipped", "Cortex consolidation disabled.")
            from maintenance.cortex_consolidation import run_consolidation
            result = run_consolidation(self.llm, self.rag)
            return MaintenanceResult(result.status, result.message)

        def cortex_decay() -> MaintenanceResult:
            from core.config import CORTEX_DECAY_ENABLED
            if not CORTEX_DECAY_ENABLED:
                return MaintenanceResult("skipped", "Cortex decay disabled.")
            from maintenance.cortex_decay_pass import run_decay_pass
            result = run_decay_pass(self.llm, self.rag)
            return MaintenanceResult(result.status, result.message)

        def cortex_ledger() -> MaintenanceResult:
            from core.config import CORTEX_LEDGER_ENABLED
            if not CORTEX_LEDGER_ENABLED:
                return MaintenanceResult("skipped", "Cortex ledger disabled.")
            from maintenance.cortex_ledger import run_ledger_pass
            result = run_ledger_pass(self.llm, self.rag)
            return MaintenanceResult(result.status, result.message)

        def rag_orphan_sweep() -> MaintenanceResult:
            result = self.rag.prune_orphan_chunks()
            if result["deleted_chunks"]:
                return MaintenanceResult(
                    "succeeded",
                    f"Removed {result['deleted_chunks']} orphan chunks "
                    f"({result['orphan_docs']} vanished docs).",
                )
            return MaintenanceResult(
                "succeeded", f"No orphan chunks ({result['scanned']} scanned)."
            )

        def template_audit() -> MaintenanceResult:
            from maintenance.template_audit import run_template_audit
            result = run_template_audit()
            return MaintenanceResult(result.status, result.message)

        def routing_report() -> MaintenanceResult:
            trace_store = getattr(self.llm, "trace_store", None)
            if trace_store is None:
                return MaintenanceResult("skipped", "No trace store associated with LLM client.")
            from maintenance.routing_report import run_routing_report
            result = run_routing_report(trace_store)
            return MaintenanceResult(result.status, result.message)

        def weekly_memoir() -> MaintenanceResult:
            trace_store = getattr(self.llm, "trace_store", None)
            if trace_store is None:
                return MaintenanceResult("skipped", "No trace store associated with LLM client.")
            from maintenance.weekly_memoir import run_weekly_memoir
            result = run_weekly_memoir(trace_store)
            return MaintenanceResult(result.status, result.message)

        def echo_canary() -> MaintenanceResult:
            # F1 defense 5: watch grounded vs cold insight signals for the
            # echo-chamber signature. Reports "insufficient" until F1 is enabled
            # and grounded insights accumulate — harmless to run meanwhile.
            from maintenance.echo_canary import run_echo_canary
            result = run_echo_canary()
            return MaintenanceResult("succeeded", f"[{result.status}] {result.message}")

        def self_assessment() -> MaintenanceResult:
            # Metacognition M1: aggregate every quality signal (report verdicts,
            # LLM health, bench, Cortex tensions, decay, insight signals) into one
            # health scorecard. Read-only, zero LLM. Sensing layer of the
            # self-improvement arc — reports, never acts.
            trace_store = getattr(self.llm, "trace_store", None)
            if trace_store is None:
                return MaintenanceResult("skipped", "No trace store associated with LLM client.")
            from maintenance.self_assessment import run_self_assessment
            result = run_self_assessment(trace_store)
            return MaintenanceResult(result.status, result.message)

        return [
            MaintenanceTask(
                name="insight_daily",
                action=daily_insight,
                daily=True,
                idle_required=True,
                window_start_hour=settings.DREAMING_FROM,
                window_end_hour=settings.DREAMING_TO,
                intent="insight",
                agent="InsightAgent",
                initial_last_run_at=self._latest_full_insight_at(),
            ),
            MaintenanceTask(
                name="retrieval_bench_daily",
                action=retrieval_bench,
                daily=True,
                idle_required=True,
                intent="maintenance.retrieval_bench",
                agent="RetrievalBench",
            ),
            MaintenanceTask(
                name="trace_prune_daily",
                action=trace_prune,
                daily=True,
                idle_required=False,
                intent="maintenance.trace_prune",
                agent="TraceStore",
            ),
            MaintenanceTask(
                name="insight_full_weekly",
                action=weekly_full_insight,
                interval_seconds=7 * 86400,
                idle_required=True,
                window_start_hour=settings.DREAMING_FROM,
                window_end_hour=settings.DREAMING_TO,
                intent="insight.full_weekly",
                agent="InsightAgent",
            ),
            MaintenanceTask(
                name="cortex_consolidation_daily",
                action=cortex_consolidation,
                daily=True,
                idle_required=True,
                window_start_hour=settings.DREAMING_FROM,
                window_end_hour=settings.DREAMING_TO,
                intent="maintenance.cortex_consolidation",
                agent="CortexConsolidation",
            ),
            MaintenanceTask(
                name="cortex_decay_daily",
                action=cortex_decay,
                daily=True,
                idle_required=True,
                window_start_hour=settings.DREAMING_FROM,
                window_end_hour=settings.DREAMING_TO,
                intent="maintenance.cortex_decay",
                agent="CortexDecay",
            ),
            MaintenanceTask(
                name="cortex_ledger_daily",
                action=cortex_ledger,
                daily=True,
                idle_required=True,
                window_start_hour=settings.DREAMING_FROM,
                window_end_hour=settings.DREAMING_TO,
                intent="maintenance.cortex_ledger",
                agent="CortexLedger",
            ),
            MaintenanceTask(
                name="bench_builder_weekly",
                action=bench_builder,
                interval_seconds=7 * 86400,
                idle_required=True,
                intent="maintenance.bench_builder",
                agent="BenchBuilder",
            ),
            MaintenanceTask(
                name="rag_orphan_sweep_daily",
                action=rag_orphan_sweep,
                daily=True,
                idle_required=True,
                intent="maintenance.rag_orphan_sweep",
                agent="RAGManager",
            ),
            MaintenanceTask(
                name="routing_report_weekly",
                action=routing_report,
                interval_seconds=7 * 86400,
                idle_required=True,
                intent="maintenance.routing_report",
                agent="RoutingReport",
            ),
            MaintenanceTask(
                name="template_audit_weekly",
                action=template_audit,
                interval_seconds=7 * 86400,
                idle_required=True,
                intent="maintenance.template_audit",
                agent="TemplateAudit",
            ),
            MaintenanceTask(
                name="weekly_memoir",
                action=weekly_memoir,
                interval_seconds=7 * 86400,
                idle_required=True,
                intent="maintenance.weekly_memoir",
                agent="WeeklyMemoir",
            ),
            MaintenanceTask(
                name="echo_canary_weekly",
                action=echo_canary,
                interval_seconds=7 * 86400,
                idle_required=True,
                intent="maintenance.echo_canary",
                agent="EchoCanary",
            ),
            MaintenanceTask(
                name="self_assessment_weekly",
                action=self_assessment,
                interval_seconds=7 * 86400,
                idle_required=True,
                intent="maintenance.self_assessment",
                agent="SelfAssessment",
            ),
        ]

    def _seed_initial_task_state(self) -> None:
        with self._state_lock:
            changed = False
            for task in self.tasks:
                if task.initial_last_run_at and task.name not in self.state:
                    self.state[task.name] = {
                        "last_run_at": task.initial_last_run_at,
                        "last_status": "seeded",
                        "last_summary": "Seeded from existing artifacts.",
                    }
                    changed = True
            if changed:
                self._save_state()

    def _load_state(self) -> dict:
        if not self.state_file.exists():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logging.warning(f"MaintenanceScheduler: failed to load state: {e}")
            return {}

    def _save_state(self) -> None:
        """Atomic write (temp file + rename). Caller must hold _state_lock."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True)
        tmp_file = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp_file.write_text(payload, encoding="utf-8")
        tmp_file.replace(self.state_file)

    def run(self):
        if not self.enabled:
            logging.info("MaintenanceScheduler: disabled.")
            return
        logging.info(
            "MaintenanceScheduler: started with tasks: %s",
            ", ".join(task.name for task in self.tasks),
        )
        while True:
            self.run_due_once(datetime.now())
            time.sleep(self.poll_seconds)

    def run_due_once(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now()
        ran = []
        for task in self.tasks:
            if not self._task_due(task, now):
                continue
            if task.idle_required and global_busy_state.is_busy():
                logging.debug("MaintenanceScheduler: task %s skipped; system busy.", task.name)
                continue
            self._run_task(task, now)
            ran.append(task.name)
        return ran

    def _task_due(self, task: MaintenanceTask, now: datetime) -> bool:
        if not self._in_window(task, now):
            return False
        task_state = self.state.get(task.name, {})
        last_run_at = self._parse_dt(task_state.get("last_run_at"))
        if task.daily:
            return last_run_at is None or last_run_at.date() != now.date()
        if task.interval_seconds is not None:
            if last_run_at is None:
                return True
            return (now - last_run_at).total_seconds() >= task.interval_seconds
        return last_run_at is None

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _in_window(task: MaintenanceTask, now: datetime) -> bool:
        start = task.window_start_hour
        end = task.window_end_hour
        if start is None or end is None:
            return True
        hour = now.hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    def _run_task(self, task: MaintenanceTask, now: datetime) -> None:
        logging.info("MaintenanceScheduler: running task %s", task.name)
        # Acquire the busy lock atomically. set_busy(True) used to stomp it: a
        # user prompt could grab busy between the is_busy() gate in
        # run_due_once and here, and the task's final set_busy(False) would then
        # release a lock it never owned (audit R7-E). try_set_busy closes that
        # race. An idle_required task that loses the race skips entirely; a
        # non-idle task (e.g. retrieval_bench) still runs but must NOT clear a
        # lock it didn't acquire — only the owner does.
        acquired = global_busy_state.try_set_busy()
        if task.idle_required and not acquired:
            logging.debug("MaintenanceScheduler: task %s skipped; system became busy.", task.name)
            return
        status = "failed"
        summary = ""
        try:
            ui.set_status(f"Maintenance: {task.name}")
            run_context = (
                self.llm.trace_run(
                    intent=task.intent,
                    agent=task.agent,
                    trigger_type="maintenance_scheduler",
                    command_id=task.name,
                    metadata={"task": task.name},
                )
                if hasattr(self.llm, "trace_run")
                else contextlib.nullcontext()
            )
            with run_context:
                result = task.action()
            status = result.status
            summary = result.summary
            logging.info("MaintenanceScheduler: task %s finished: %s", task.name, summary)
        except Exception as e:
            summary = str(e)
            # Log the full traceback — debugging today's insight_daily
            # failure took 30 minutes of cross-referencing the trace DB
            # because the original log line dropped the stack entirely.
            logging.exception(
                "MaintenanceScheduler: task %s failed", task.name,
            )
        finally:
            with self._state_lock:
                self.state[task.name] = {
                    "last_run_at": now.isoformat(timespec="seconds"),
                    "last_status": status,
                    "last_summary": summary,
                }
                self._save_state()
            ui.set_status("Ling Ling is waiting... (๑´ㅂ`๑)zZ... (Ctrl-C to Quit)", is_busy=False)
            if acquired:
                global_busy_state.set_busy(False)

    @staticmethod
    def _latest_full_insight_at() -> str | None:
        insights_dir = WIKI_VAULT_DIR / "Insights"
        if not insights_dir.exists():
            return None
        latest: date | None = None
        for path in insights_dir.glob("*.md"):
            try:
                date_part = MaintenanceScheduler._full_insight_date_part(path)
                if not date_part:
                    continue
                run_date = datetime.strptime(date_part, "%Y%m%d").date()
            except (IndexError, ValueError):
                continue
            if latest is None or run_date > latest:
                latest = run_date
        if latest is None:
            return None
        return datetime.combine(latest, datetime.min.time()).isoformat(timespec="seconds")

    @staticmethod
    def _full_insight_date_part(path: Path) -> str | None:
        stem = path.stem
        if "full-insight-" in stem:
            return stem.split("full-insight-", 1)[1][:8]
        if stem.endswith("][full-insight]") and stem.startswith("["):
            return stem[1:9]
        return None
