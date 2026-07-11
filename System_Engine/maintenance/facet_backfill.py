"""Facet backfill pump — idle-time, low-priority, one page per bite.

Pages indexed before the facet index existed have no facets. This pump
backfills them whenever the system is idle, under three rules agreed with
the user:

1. **Strictly low priority.** The busy lock is the real preemption
   mechanism: each step is one page, the lock is held only for that step,
   and queued user work (toLingLing/, Consolidate/) drains first on
   release because this pump's idle callback is registered LAST. As a
   second line of defense, a step won't even start while a *fresh* file
   (mtime < 10 min) sits in an inbox — stale files are stuck work and
   must not starve the pump forever.
2. **Work is derived, not tracked.** "Which pages need facets" is
   recomputed from the DB (pages without facet entries), same reconcile
   philosophy as the orphan sweep — no done-list to drift. The only
   persisted state is the failure ledger (quarantine + daily budget).
3. **Done means silent.** Empty queue → the pump schedules nothing and
   touches nothing. New ingestions facet themselves, so the queue only
   refills after unusual events (facet deletion, re-ingestion).

Cost note: Part pages embed their original digest in the "Part Digest
Appendix" section, so backfilling them is a parse — zero LLM calls. Only
pages without an appendix spend one generate_part_digest call.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from core.config import (
    CONSOLIDATE_DIR,
    DIGEST_SOURCES_MAX_SOURCE_CHARS,
    FACET_BACKFILL_DAILY_BUDGET,
    FACET_BACKFILL_ENABLED,
    FACET_BACKFILL_GRACE_SECONDS,
    FACET_BACKFILL_MAX_ATTEMPTS,
    FACET_BACKFILL_MIN_BYTES,
    FACET_BACKFILL_STATE_FILE,
    FACET_BACKFILL_STEP_GAP_SECONDS,
    MAINTENANCE_LOG_FILE,
    NOTES_DIR,
    PAGES_DIR,
    TO_LLM_DIR,
)
from core.parser import strip_body_frontmatter
from core.state import global_busy_state

_FRESH_INBOX_SECONDS = 600  # files younger than this = pending user work
_QUEUE_TTL_SECONDS = 24 * 3600  # rebuild the derived queue at most daily
_GLOBAL_BACKOFF_SECONDS = 3600  # provider-down backoff
_GLOBAL_FAILURE_THRESHOLD = 3  # distinct-page failures in a row = outage

_THESIS_RE = re.compile(r"^\s*-\s*\*\*Thesis\*\*\s*[:：]\s*(.+)$", re.MULTILINE)
_KEY_POINT_RE = re.compile(r"^\s {0,4}-\s+(.+)$")


def parse_digest_appendix(page_text: str) -> dict | None:
    """Recover thesis/key_points from a part note's Digest Appendix.

    Returns a digest-shaped dict (compatible with
    IngestionPipeline._facets_from_digest) or None when no appendix exists.
    """
    from services.ingestion_pipeline import _PART_DIGEST_HEADER

    if _PART_DIGEST_HEADER not in page_text:
        return None
    appendix = page_text.split(_PART_DIGEST_HEADER, 1)[1]

    thesis_match = _THESIS_RE.search(appendix)
    thesis = thesis_match.group(1).strip() if thesis_match else ""

    key_points: list[str] = []
    in_key_points = False
    for line in appendix.splitlines():
        stripped = line.strip()
        if stripped.startswith("- **Key Points**"):
            in_key_points = True
            continue
        if in_key_points:
            if stripped.startswith("- **") or stripped.startswith("#"):
                break
            m = _KEY_POINT_RE.match(line)
            if m and line.startswith("  "):
                key_points.append(m.group(1).strip())
            elif stripped:
                break

    if not thesis and not key_points:
        return None
    return {"thesis": thesis, "key_points": key_points}


class FacetBackfillPump:
    def __init__(
        self,
        llm,
        rag,
        *,
        state_file: Path = FACET_BACKFILL_STATE_FILE,
        enabled: bool = FACET_BACKFILL_ENABLED,
        grace_seconds: int = FACET_BACKFILL_GRACE_SECONDS,
        step_gap_seconds: int = FACET_BACKFILL_STEP_GAP_SECONDS,
        daily_budget: int = FACET_BACKFILL_DAILY_BUDGET,
        max_attempts: int = FACET_BACKFILL_MAX_ATTEMPTS,
        min_bytes: int = FACET_BACKFILL_MIN_BYTES,
    ):
        self.llm = llm
        self.rag = rag
        self.state_file = state_file
        self.enabled = enabled
        self.grace_seconds = grace_seconds
        self.step_gap_seconds = step_gap_seconds
        self.daily_budget = daily_budget
        self.max_attempts = max_attempts
        self.min_bytes = min_bytes

        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()
        self._queue: list[tuple[int, Path, str]] = []
        self._queue_built_at: float = 0.0
        self._queue_ever_built = False
        self._consecutive_failures = 0
        self._backoff_until: float = 0.0
        self._ledger = self._load_ledger()

    # ── Triggers ─────────────────────────────────────────────────────

    def on_idle(self) -> int:
        """BusyState idle callback. NEVER does work inline — the callback
        runs while the busy lock is still held, so doing LLM work here
        would jump the priority queue we exist to respect."""
        self.kick(self.grace_seconds, replace=False)
        return 0

    def kick(self, delay: float | None = None, *, replace: bool = True) -> None:
        """Schedule a tick. replace=False keeps an earlier (sooner) timer —
        used by on_idle so the post-step 30s gap isn't stretched back out
        to the 180s grace every time the lock is released."""
        if not self.enabled:
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
            logging.exception("Facet backfill tick failed")

    def _run_step(self) -> None:
        if not self.enabled:
            return
        now = time.time()
        if now < self._backoff_until:
            self.kick(max(self._backoff_until - now, 60))
            return
        if not self._budget_remaining():
            # Resume shortly after midnight when the budget resets.
            self.kick(self._seconds_until_tomorrow() + self.grace_seconds)
            return
        if global_busy_state.is_busy():
            return  # the next busy→idle edge re-kicks us
        if self._fresh_inbox_pending():
            return

        self._ensure_queue()
        entry = self._next_entry()
        if entry is None:
            self._log_completion_once()
            return

        if not global_busy_state.try_set_busy():
            return

        _, path, title = entry
        used_llm = False
        try:
            status, used_llm = self._process_page(path, title)
            if used_llm:
                self._charge_budget()
            if status == "ok":
                self._consecutive_failures = 0
                self._ledger["attempts"].pop(title, None)
            elif status == "failed":
                self._record_failure(path, title)
            self._save_ledger()
        finally:
            # Schedule the next bite BEFORE releasing the lock: the release
            # fires on_idle, which sees this timer and keeps it (replace=False)
            # instead of stretching the gap back to the grace period.
            if self._queue:
                self.kick(self.step_gap_seconds)
            global_busy_state.set_busy(False)

    # ── Step: one page ───────────────────────────────────────────────

    def _process_page(self, path: Path, title: str) -> tuple[str, bool]:
        """Returns (status, used_llm). status: ok | failed | skipped."""
        from services.ingestion_pipeline import IngestionPipeline

        if not path.exists():
            return "skipped", False
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            logging.warning(f"Facet backfill: cannot read {title}: {e}")
            return "failed", False

        # Part pages carry their original digest in the appendix — parse it,
        # zero LLM cost. Everything else spends one digest call.
        digest = parse_digest_appendix(text)
        used_llm = False
        if digest is None:
            body, _ = strip_body_frontmatter(text)
            body = body[:DIGEST_SOURCES_MAX_SOURCE_CHARS]
            if len(body.encode("utf-8")) < self.min_bytes:
                self._quarantine(path, title, reason="too_short")
                return "skipped", False
            try:
                digest = self.llm.generate_part_digest(title, 1, 1, body, body, "")
                used_llm = True
            except Exception as e:
                logging.warning(f"Facet backfill: digest failed for {title}: {e}")
                return "failed", True

        facets = IngestionPipeline._facets_from_digest(digest)
        if not facets:
            if used_llm:
                # The LLM produced an unusable digest — count it as a failure
                # so repeated offenders reach quarantine via max_attempts.
                return "failed", True
            # Appendix existed but filtered down to nothing — permanent
            # property of this page, quarantine immediately.
            self._quarantine(path, title, reason="no_facets")
            return "skipped", False

        try:
            indexed = self.rag.add_facets(path, title, facets)
        except Exception as e:
            logging.warning(f"Facet backfill: add_facets failed for {title}: {e}")
            return "failed", used_llm
        if indexed is False:  # explicit failure signal; None (legacy fakes) = ok
            # add_facets is fail-open and swallows upsert/embedding errors
            # (already logged there) — but this page's facets are NOT in the
            # index, so it must retry later, not retire as done.
            return "failed", used_llm

        logging.info(
            f"Facet backfill: {title} +{len(facets)} facets "
            f"({'llm' if used_llm else 'appendix parse'})"
        )
        return "ok", used_llm

    # ── Queue (derived from DB, rebuilt daily) ───────────────────────

    def _ensure_queue(self) -> None:
        if self._queue_ever_built and (
            self._queue or time.time() - self._queue_built_at < _QUEUE_TTL_SECONDS
        ):
            return
        self._queue = self._build_queue()
        self._queue_built_at = time.time()
        self._queue_ever_built = True
        if self._queue:
            self._ledger["completed_logged"] = False
            self._save_ledger()
            logging.info(f"Facet backfill: queue rebuilt, {len(self._queue)} page(s) pending")

    def _build_queue(self) -> list[tuple[int, Path, str]]:
        try:
            facet_titles = {e["title"] for e in self.rag.get_facet_entries() if e.get("title")}
        except Exception as e:
            logging.warning(f"Facet backfill: facet listing failed: {e}")
            return []

        recent = set()
        trace_store = getattr(self.llm, "trace_store", None)
        if trace_store is not None and hasattr(trace_store, "recently_retrieved_titles"):
            recent = trace_store.recently_retrieved_titles(30)

        entries: list[tuple[int, float, Path, str]] = []
        for root in (PAGES_DIR, NOTES_DIR):
            if not root.exists():
                continue
            for path in root.rglob("*.md"):
                title = path.stem
                if title in facet_titles or title.startswith("_"):
                    continue
                if "(Stitched)" in title:
                    continue
                if self._is_quarantined(path, title):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size < self.min_bytes:
                    continue
                # Priority: synthesis pages → recently-queried → notes/general
                # → long-book parts. Improve first where users actually look.
                if "(Synthesis)" in title:
                    priority = 0
                elif title in recent:
                    priority = 1
                elif "(Part" in title:
                    priority = 3
                else:
                    priority = 2
                entries.append((priority, -stat.st_mtime, path, title))

        entries.sort(key=lambda e: (e[0], e[1]))
        return [(p, path, title) for p, _, path, title in entries]

    def _next_entry(self) -> tuple[int, Path, str] | None:
        while self._queue:
            entry = self._queue.pop(0)
            if entry[1].exists():
                return entry
        return None

    # ── Yield conditions ─────────────────────────────────────────────

    @staticmethod
    def _fresh_inbox_pending() -> bool:
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

    # ── Failure ledger / quarantine / budget ─────────────────────────

    def _record_failure(self, path: Path, title: str) -> None:
        attempts = self._ledger["attempts"].get(title, 0) + 1
        self._ledger["attempts"][title] = attempts
        if attempts >= self.max_attempts:
            self._quarantine(path, title, reason="max_attempts")
        self._consecutive_failures += 1
        if self._consecutive_failures >= _GLOBAL_FAILURE_THRESHOLD:
            # Distinct pages failing in a row smells like a provider outage,
            # not bad content — back off instead of hammering all night.
            self._backoff_until = time.time() + _GLOBAL_BACKOFF_SECONDS
            self._consecutive_failures = 0
            logging.warning("Facet backfill: repeated failures, backing off 1h")

    def _quarantine(self, path: Path, title: str, *, reason: str) -> None:
        try:
            mtime = path.stat().st_mtime if path.exists() else 0.0
        except OSError:
            mtime = 0.0
        self._ledger["quarantine"][title] = {"mtime": mtime, "reason": reason}
        self._ledger["attempts"].pop(title, None)

    def _is_quarantined(self, path: Path, title: str) -> bool:
        record = self._ledger["quarantine"].get(title)
        if not record:
            return False
        try:
            current_mtime = path.stat().st_mtime
        except OSError:
            return True
        if current_mtime > float(record.get("mtime", 0)) + 1.0:
            # The file changed since quarantine — requalify it.
            del self._ledger["quarantine"][title]
            self._save_ledger()
            return False
        return True

    def _budget_remaining(self) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
        budget = self._ledger["budget"]
        if budget.get("date") != today:
            self._ledger["budget"] = {"date": today, "used": 0}
            return self.daily_budget > 0
        return budget.get("used", 0) < self.daily_budget

    def _charge_budget(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        budget = self._ledger["budget"]
        if budget.get("date") != today:
            self._ledger["budget"] = {"date": today, "used": 1}
        else:
            budget["used"] = budget.get("used", 0) + 1

    @staticmethod
    def _seconds_until_tomorrow() -> float:
        now = datetime.now()
        return (24 - now.hour) * 3600 - now.minute * 60 - now.second

    # ── Completion ───────────────────────────────────────────────────

    def _log_completion_once(self) -> None:
        if self._ledger.get("completed_logged"):
            return
        self._ledger["completed_logged"] = True
        self._save_ledger()
        message = "Facet backfill complete — every eligible page now has facets."
        logging.info(message)
        try:
            from core.ui import ui

            ui.success("🎯 Facet 回填完成：所有合格頁面都有 facets 了")
            MAINTENANCE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with MAINTENANCE_LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"\n## [{stamp}] Facet Backfill | {message}\n")
        except Exception:
            pass

    def progress(self) -> dict:
        """Snapshot for the routing report."""
        return {
            "pending": len(self._queue),
            "quarantined": len(self._ledger["quarantine"]),
            "budget_used_today": self._ledger["budget"].get("used", 0),
            "completed": self._ledger.get("completed_logged", False),
        }

    # ── Ledger persistence ───────────────────────────────────────────

    def _load_ledger(self) -> dict:
        default = {
            "attempts": {},
            "quarantine": {},
            "budget": {"date": "", "used": 0},
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
            logging.warning(f"Facet backfill ledger unreadable, starting fresh: {e}")
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
            logging.warning(f"Facet backfill ledger write failed: {e}")
