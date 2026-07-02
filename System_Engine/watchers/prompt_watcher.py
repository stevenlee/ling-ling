"""PromptWatcher — filesystem watcher for toLingLing/ prompt files (P2c).

Watching only: enqueue on event, settle, drain under the busy lock, archive.
Intent routing + execution live in services/command_dispatcher.py.
"""

import logging
import os
import queue
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import watchdog.events

from core.config import (
    FROM_LLM_DIR,
    PROJECT_ROOT,
    RAW_PROMPTS_DIR,
    TO_LLM_DIR,
)
from core.state import global_busy_state
from core.ui import ui
from services.command_dispatcher import (  # noqa: F401  (INTENT_ROUTES: test/TUI surface)
    INTENT_ROUTES,
    CommandDispatcher,
    detect_intent,
    detect_planner_flags,
)

LOCK_FILE = PROJECT_ROOT / ".kb_lock"


class PromptWatcher(watchdog.events.FileSystemEventHandler):
    def __init__(self, llm_client, rag_manager):
        super().__init__()
        self.llm = llm_client
        self.rag = rag_manager
        self.dispatcher = CommandDispatcher(self.llm, self.rag)
        self.registry = self.dispatcher.registry
        # ── Job queue (thread-safe) ──
        self._job_queue: queue.Queue[Path] = queue.Queue()
        self._queued_paths: set[str] = set()
        self._queue_lock = threading.Lock()
        # ── Worker thread (audit R7-G) ── processing must NOT run on the
        # watchdog dispatch thread; _handle_event only enqueues + wakes this.
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._stability_delay = 1.0  # filesystem-settle wait; 0 in tests

    def on_created(self, event):
        self._handle_event(event)

    def on_moved(self, event):
        if not event.is_directory:
            from core.config import TO_LLM_DIR

            dest_path = Path(event.dest_path)
            if TO_LLM_DIR in dest_path.parents:
                self._handle_event(event, is_move=True)

    def _handle_event(self, event, is_move=False):
        if event.is_directory:
            return

        filepath = Path(event.dest_path) if is_move else Path(event.src_path)
        if filepath.suffix.lower() not in (".md", ".txt"):
            return

        # Enqueue and wake the worker; do NOT process here (audit R7-G). This
        # runs on the watchdog dispatch thread — processing is seconds of LLM
        # work and would block every subsequent filesystem event. The worker
        # applies the stability delay and the existence check before running.
        if self._enqueue(filepath):
            self._wake.set()

    # ── Worker thread ─────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background worker that drains the queue off the dispatch
        thread. Idempotent."""
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run_worker, name="PromptWatcherWorker", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        """Signal the worker to exit and join it."""
        self._stop.set()
        self._wake.set()
        if self._worker:
            self._worker.join(timeout=5)

    def _run_worker(self) -> None:
        while not self._stop.is_set():
            # Wake on new work; the timeout also lets a late enqueue (set
            # before the worker armed the wait) get picked up promptly.
            if not self._wake.wait(timeout=1.0):
                continue
            self._wake.clear()
            if self._stop.is_set():
                break
            if self._stability_delay:
                time.sleep(self._stability_delay)  # let the filesystem settle
            self._drain_queue()

    # ── Job queue internals ──────────────────────────────────────────

    def _enqueue(self, filepath: Path) -> bool:
        """Add *filepath* to the processing queue.  Returns True if newly enqueued."""
        key = str(filepath)
        with self._queue_lock:
            if key in self._queued_paths:
                return False
            self._queued_paths.add(key)
        self._job_queue.put(filepath)
        return True

    def _dequeue(self) -> Path | None:
        try:
            return self._job_queue.get_nowait()
        except queue.Empty:
            return None

    def _mark_done(self, filepath: Path):
        with self._queue_lock:
            self._queued_paths.discard(str(filepath))

    def _drain_queue(self):
        """Acquire global busy state and process every queued prompt file.

        If the system is busy the files stay in the queue — they will be
        picked up by the ``scan_existing`` idle callback.
        """
        if not global_busy_state.try_set_busy():
            return
        try:
            self._process_queue_items()
        finally:
            global_busy_state.set_busy(False)

    def _process_queue_items(self) -> int:
        """Drain the queue.  Caller must hold busy state.  Returns processed count."""
        processed = 0
        while True:
            if LOCK_FILE.exists():
                ui.info("系統鎖定中 (.kb_lock)，暫停處理指令")
                break
            filepath = self._dequeue()
            if filepath is None:
                break
            try:
                if not filepath.exists():
                    continue
                ui.cmd_received(filepath.name)
                ui.set_status(f"正在處理指令：{filepath.name}")
                self.process_prompt(filepath)
                if not filepath.exists():
                    processed += 1
                ui.success(f"任務完成：{filepath.name}")
            except Exception as e:
                ui.error(f"指令執行失敗：{e}")
            finally:
                self._mark_done(filepath)
        return processed

    def scan_existing(self):
        """Scan toLingLing/ for un-processed prompts and drain the queue.

        Called during startup (busy state held by caller) and as an idle
        callback (busy state held by the callback mechanism).
        """
        if TO_LLM_DIR.exists():
            for f in sorted(TO_LLM_DIR.iterdir()):
                if f.is_file() and f.suffix.lower() in (".md", ".txt"):
                    if self._enqueue(f):
                        ui.info(f"Found pending prompt: {f.name}")
        return self._process_queue_items()

    # Thin delegates — routing logic lives in services/command_dispatcher.py
    # (P2c). Kept because tests and the TUI reference these names.

    def _detect_intent(self, lower_name: str, lower_query: str) -> str | None:
        return detect_intent(lower_name, lower_query)

    _detect_planner_flags = staticmethod(detect_planner_flags)

    def _resynthesize(self, target_entities: list[str]) -> str:
        return self.dispatcher._resynthesize(target_entities)

    def process_prompt(self, filepath: Path):
        try:
            query_content = filepath.read_text(encoding="utf-8")
            logging.info(
                f"Answering prompt {filepath.name} using {self.llm.provider.upper()} ({self.llm.model})..."
            )
            self.dispatcher.dispatch(query_content, filepath)
            self._archive_raw(filepath)
        except Exception as e:
            logging.error(f"Error answering {filepath.name}: {str(e)}")
            self._write_error_output(filepath, e)
            self._archive_raw(filepath)

    def _archive_raw(self, filepath: Path):
        if not filepath.exists():
            return
        # Ensure we are using the simple name to avoid weird path corruption
        safe_name = os.path.basename(str(filepath))
        dest = RAW_PROMPTS_DIR / safe_name

        if dest.exists():
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            stem = Path(safe_name).stem
            suffix = Path(safe_name).suffix
            dest = RAW_PROMPTS_DIR / f"{stem}_{timestamp}{suffix}"

        try:
            shutil.move(str(filepath), str(dest))
        except Exception as e:
            logging.error(f"Failed to archive prompt {filepath} to {dest}: {e}")

    def _write_error_output(self, filepath: Path, error: Exception):
        request_id = filepath.stem
        if not request_id.startswith("ocll-"):
            return

        safe_message = str(error).strip() or error.__class__.__name__
        output_path = FROM_LLM_DIR / f"💧err-{request_id}.md"
        body = f"""---
title: "error: {request_id}"
type: error
request_id: "{request_id}"
---

# Ling-Ling Request Failed

{safe_message}
"""
        try:
            output_path.write_text(body, encoding="utf-8")
        except Exception as write_error:
            logging.error(f"Failed to write error output for {request_id}: {write_error}")
