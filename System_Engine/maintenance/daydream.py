"""Daydream pump — daytime makeup + spontaneous reflection, one bite at a time.

Night belongs to the MaintenanceScheduler's deep sleep (the 1–5am dreaming
window). When that window is busy, the day's cognition is otherwise lost:
`_task_due` gates on `_in_window` *first*, so a missed daily insight /
consolidation is skipped for the whole day, not made up.

This pump fixes that from the daytime side, reusing the FacetBackfillPump
contract verbatim:

1. **Strictly low priority.** The busy lock is the preemption mechanism; the
   pump is registered LAST among idle callbacks, holds the lock for exactly one
   bite, and yields to fresh files in toLingLing/ or Consolidate/.
2. **Work is derived, not tracked.** "Is there owed cognition?" is recomputed
   each step (unprocessed insights via cortex_consolidation.has_pending_insights;
   whether insight_daily ran today via the scheduler's own state file). The only
   persisted state is the per-day budget ledger.
3. **Done means silent.** Nothing owed and budgets spent → schedule nothing.

Per-bite work ladder (highest value / cheapest first, one unit per step):
  1. drain the cortex-consolidation backlog one insight at a time
     (run_consolidation(max_insights=1) is already idempotent + resumable)
  2. make up a daily insight the busy night skipped
  3. when nothing is owed, generate one light spontaneous reflection

Daytime only — `_run_step` no-ops inside the dreaming window so it never
contends with deep sleep. Opportunistic, no SLA: a busy day skips it entirely
and the night's deep sleep still clears the full backlog (shared
cortex_state["processed"] → no double-processing).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from core.config import (
    CONSOLIDATE_DIR,
    DAYDREAM_STATE_FILE,
    MAINTENANCE_LOG_FILE,
    MAINTENANCE_STATE_FILE,
    TO_LLM_DIR,
    settings,
)
from core.state import global_busy_state
from maintenance.cortex_consolidation import has_pending_insights, run_consolidation
from maintenance.daily_insight import run_daily_insight

# Behavioural knobs (enabled / spontaneous / per-day budgets) are Scripture-
# driven and read live off `settings` so editing Scripture.md takes effect on
# the next bite — same hot-reload contract as the dreaming window. The values
# below are pure scheduling internals, not user-facing persona settings.
_GRACE_SECONDS = 180                # delay before the first bite after an idle edge
_STEP_GAP_SECONDS = 30              # gap between consecutive bites
_FRESH_INBOX_SECONDS = 600          # files younger than this = pending user work
_GLOBAL_BACKOFF_SECONDS = 3600      # provider-down backoff
_GLOBAL_FAILURE_THRESHOLD = 3       # bites failing in a row = outage


class DaydreamPump:
    def __init__(
        self,
        llm,
        rag,
        *,
        state_file: Path = DAYDREAM_STATE_FILE,
        grace_seconds: int = _GRACE_SECONDS,
        step_gap_seconds: int = _STEP_GAP_SECONDS,
        maintenance_state_file: Path = MAINTENANCE_STATE_FILE,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.llm = llm
        self.rag = rag
        self.state_file = state_file
        self.grace_seconds = grace_seconds
        self.step_gap_seconds = step_gap_seconds
        self.maintenance_state_file = maintenance_state_file
        self._clock = clock

        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()
        self._consecutive_failures = 0
        self._backoff_until: float = 0.0
        self._ledger = self._load_ledger()

    # ── Triggers ─────────────────────────────────────────────────────

    def on_idle(self) -> int:
        """BusyState idle callback. NEVER does work inline — the callback runs
        while the busy lock is still held, so doing LLM work here would jump
        the priority queue we exist to respect."""
        self.kick(self.grace_seconds, replace=False)
        return 0

    def kick(self, delay: float | None = None, *, replace: bool = True) -> None:
        """Schedule a tick. replace=False keeps an earlier (sooner) timer —
        used by on_idle so the post-step gap isn't stretched back out to the
        grace period every time the lock is released."""
        if not settings.DAYDREAM_ENABLED:
            return
        delay = self.grace_seconds if delay is None else delay
        with self._timer_lock:
            if self._timer is not None:
                if not replace:
                    return
                self._timer.cancel()
            self._timer = threading.Timer(delay, self._tick)
            self._timer.daemon = True
            self._timer.start()

    def stop(self) -> None:
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    # ── Pump body ────────────────────────────────────────────────────

    def _tick(self) -> None:
        with self._timer_lock:
            self._timer = None
        try:
            self._run_step()
        except Exception:
            logging.exception("Daydream tick failed")

    def _run_step(self) -> None:
        if not settings.DAYDREAM_ENABLED:
            return
        # Night belongs to deep sleep. Re-check right after the window closes
        # in case the day is otherwise quiescent (no busy→idle edge to wake us).
        if self._in_dreaming_window():
            self.kick(self._seconds_until_window_end() + self.grace_seconds, replace=False)
            return
        now = time.time()
        if now < self._backoff_until:
            self.kick(max(self._backoff_until - now, 60))
            return
        if global_busy_state.is_busy():
            return  # the next busy→idle edge re-kicks us
        if self._fresh_inbox_pending():
            return

        self._roll_budget_date()
        action = self._choose_action()
        if action is None:
            self._log_completion_once()
            return

        if not global_busy_state.try_set_busy():
            return
        failed = False
        try:
            failed = not self._dispatch(action)
        except Exception:
            logging.exception("Daydream: %s bite failed", action)
            failed = True
        finally:
            if failed:
                self._record_failure()
            else:
                self._consecutive_failures = 0
            self._save_ledger()
            # Schedule the next bite BEFORE releasing the lock (same reason as
            # facet_backfill): the release fires on_idle, which keeps this
            # sooner timer instead of stretching the gap back to grace.
            if self._backoff_until <= time.time() and self._choose_action() is not None:
                self.kick(self.step_gap_seconds)
            global_busy_state.set_busy(False)

    # ── Work ladder ──────────────────────────────────────────────────

    def _choose_action(self) -> str | None:
        b = self._ledger["budget"]
        if b["consolidation"] < settings.DAYDREAM_CONSOLIDATION_BUDGET and has_pending_insights():
            return "consolidate"
        if b["insight"] < settings.DAYDREAM_INSIGHT_BUDGET and not self._insight_ran_today():
            return "insight"
        if settings.DAYDREAM_SPONTANEOUS_ENABLED and b["spontaneous"] < settings.DAYDREAM_SPONTANEOUS_BUDGET:
            return "spontaneous"
        return None

    def _dispatch(self, action: str) -> bool:
        """Run one bite. Returns True on success (or benign no-op), False on a
        failure that should count toward the global backoff."""
        if action == "consolidate":
            result = run_consolidation(
                self.llm, self.rag,
                max_insights=1, max_adjudications=settings.DAYDREAM_BITE_ADJUDICATIONS,
            )
            if getattr(result, "insights_processed", 0) >= 1:
                self._ledger["budget"]["consolidation"] += 1
                logging.info("Daydream: consolidated 1 insight — %s", result.message)
            # status "skipped" with 0 processed = backlog vanished mid-step
            # (race or it got disabled) — a benign no-op, not a failure.
            return getattr(result, "status", "skipped") != "failed"
        if action == "insight":
            result = run_daily_insight(self.llm, self.rag, occasion="Daydream makeup")
            self._ledger["budget"]["insight"] += 1
            logging.info("Daydream: makeup insight — %s", result.summary)
            return result.status != "failed"
        if action == "spontaneous":
            result = run_daily_insight(self.llm, self.rag, occasion="Daydream spontaneous")
            self._ledger["budget"]["spontaneous"] += 1
            logging.info("Daydream: spontaneous insight — %s", result.summary)
            return result.status != "failed"
        return True

    # ── Gates ────────────────────────────────────────────────────────

    def _in_dreaming_window(self) -> bool:
        start = settings.DREAMING_FROM
        end = settings.DREAMING_TO
        if start is None or end is None:
            return False
        hour = self._clock().hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    def _seconds_until_window_end(self) -> float:
        now = self._clock()
        end_hour = settings.DREAMING_TO % 24
        target = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    def _insight_ran_today(self) -> bool:
        """Read the scheduler's own state file (read-only) — did insight_daily
        run today? If not, the night skipped it and it is owed."""
        try:
            data = json.loads(self.maintenance_state_file.read_text(encoding="utf-8"))
        except Exception:
            return False
        entry = data.get("insight_daily") if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            return False
        try:
            return datetime.fromisoformat(entry.get("last_run_at")).date() == self._clock().date()
        except (TypeError, ValueError):
            return False

    def _fresh_inbox_pending(self) -> bool:
        """Fresh inbox files = pending user work; stale ones (stuck/failed)
        must not starve the pump forever."""
        cutoff = time.time() - _FRESH_INBOX_SECONDS
        for inbox in (TO_LLM_DIR, CONSOLIDATE_DIR):
            if not inbox.exists():
                continue
            for f in inbox.iterdir():
                if f.name.startswith("."):
                    continue
                try:
                    if f.stat().st_mtime >= cutoff:
                        return True
                except OSError:
                    continue
        return False

    # ── Budget / backoff ─────────────────────────────────────────────

    def _roll_budget_date(self) -> None:
        today = self._clock().strftime("%Y-%m-%d")
        if self._ledger["budget"].get("date") != today:
            self._ledger["budget"] = {
                "date": today, "consolidation": 0, "insight": 0, "spontaneous": 0,
            }
            self._ledger["completed_logged"] = False
            self._save_ledger()

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _GLOBAL_FAILURE_THRESHOLD:
            self._backoff_until = time.time() + _GLOBAL_BACKOFF_SECONDS
            self._consecutive_failures = 0
            logging.warning("Daydream: repeated failures, backing off 1h")

    # ── Completion ───────────────────────────────────────────────────

    def _log_completion_once(self) -> None:
        if self._ledger.get("completed_logged"):
            return
        self._ledger["completed_logged"] = True
        self._save_ledger()
        message = "Daydream done for today — backlog drained, budgets spent."
        logging.info(message)
        try:
            from core.ui import ui
            ui.success("🌷 今天的白日夢做完了：該補的都補上了")
            MAINTENANCE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            stamp = self._clock().strftime("%Y-%m-%d %H:%M")
            with MAINTENANCE_LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"\n## [{stamp}] Daydream | {message}\n")
        except Exception:
            pass

    def progress(self) -> dict:
        """Snapshot for the routing report."""
        b = self._ledger["budget"]
        return {
            "consolidation_today": b.get("consolidation", 0),
            "makeup_insights_today": b.get("insight", 0),
            "spontaneous_today": b.get("spontaneous", 0),
            "completed": self._ledger.get("completed_logged", False),
        }

    # ── Ledger persistence ───────────────────────────────────────────

    def _load_ledger(self) -> dict:
        default = {
            "budget": {"date": "", "consolidation": 0, "insight": 0, "spontaneous": 0},
            "completed_logged": False,
        }
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for key, value in default.items():
                        data.setdefault(key, value)
                    return data
        except Exception as e:
            logging.warning(f"Daydream ledger unreadable, starting fresh: {e}")
        return default

    def _save_ledger(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_name(self.state_file.name + ".tmp")
            tmp.write_text(
                json.dumps(self._ledger, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.state_file)
        except Exception as e:
            logging.warning(f"Daydream ledger write failed: {e}")
