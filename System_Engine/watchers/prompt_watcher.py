import time
import re
import threading
import queue
import logging
import os
import shutil
import contextlib
from pathlib import Path
from datetime import datetime
import watchdog.events

from core.state import global_busy_state
from core.config import (
    TO_LLM_DIR,
    FROM_LLM_DIR,
    RAW_PROMPTS_DIR,
    INDEX_FILE,
    PROJECT_ROOT,
    COMMAND_PREFIX,
    LOAD_SOURCES_MAX_CHARS_PER_SOURCE,
    settings,
)
from core.ui import ui
from agents.registry import AgentRegistry
from services.builtin_adapters import _resolve_source_paths

LOCK_FILE = PROJECT_ROOT / ".kb_lock"

# Declarative intent routing table.
# Order matters: longer prefixes (e.g. "patrol-tags") must appear before shorter
# ones (e.g. "patrol") to prevent false matches.
# Each entry: (filename_triggers, slash_triggers, intent_key)
INTENT_ROUTES = [
    (["visualize"],                          ["visualize"],   "visualize"),
    (["merge"],                              ["merge"],       "merge"),
    (["lens", "count"],                      ["lens", "count"], "lens"),
    (["patrol-tags"],                        ["patrol-tags"], "patrol_tags"),
    (["repair-tags"],                        ["repair-tags"], "repair_tags"),
    (["patrol"],                             ["patrol"],      "patrol"),
    (["repair-db"],                          ["repair-db"],   "linter"),
    (["insight"],                            ["insight"],     "insight"),
    # Publishing track — turn a note's Synthesis into a learning-first blog
    # review/report (報導者／書評人). Dispatches to ReviewAgent.
    (["review"],                             ["review"],      "review"),
    # Publish track step 1 (ling-ling push): transform Blog/ → kafu/content/.
    # Build + deploy stay on the kafu side (`make publish`). Dispatches BlogAgent.
    (["blog"],                               ["blog"],        "blog"),
    (["profiles", "profile"],                ["profiles", "profile"], "profiles"),
    (["recall"],                             ["recall"],      "recall"),
    (["tensions", "tension"],                ["tensions", "tension"], "tensions"),
    (["improve", "improvements"],            ["improve"],     "improve"),
    (["cortex"],                             ["cortex"],      "cortex"),
    # Brain ops — fire a maintenance/cognition pass on demand (TUI or Obsidian).
    # They run the SAME functions the scheduler/daydream pump use, under the
    # busy lock the worker already holds. No agent class; dispatched directly.
    (["resynthesize", "re-synthesize"],      ["resynthesize", "re-synthesize"], "resynthesize"),
    (["consolidate"],                        ["consolidate"], "consolidate"),
    (["dream"],                              ["dream"],       "dream"),
    (["decay"],                              ["decay"],       "decay"),
    (["ledger"],                             ["ledger"],      "ledger"),
    (["assess", "checkup"],                  ["assess", "checkup"], "assess"),
    (["plan"],                               ["plan"],        "plan"),
    (["do"],                                 ["do"],          "do"),
    (["zip"],                                ["zip"],         "kb_zip"),
    (["unzip"],                              ["unzip"],       "kb_unzip"),
    (["reset"],                              ["reset"],       "kb_reset"),
    (["research"],                           ["research"],    "research"),
]

# Intents dispatched directly to a maintenance/cognition function (no agent).
_BRAIN_OPS = {"dream", "consolidate", "decay", "ledger", "assess", "resynthesize"}

class PromptWatcher(watchdog.events.FileSystemEventHandler):
    def __init__(self, llm_client, rag_manager):
        super().__init__()
        self.llm = llm_client
        self.rag = rag_manager
        self.registry = AgentRegistry(self.llm, self.rag)
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
        if filepath.suffix.lower() not in ('.md', '.txt'):
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
        from core.config import TO_LLM_DIR
        if TO_LLM_DIR.exists():
            for f in sorted(TO_LLM_DIR.iterdir()):
                if f.is_file() and f.suffix.lower() in ('.md', '.txt'):
                    if self._enqueue(f):
                        ui.info(f"Found pending prompt: {f.name}")
        return self._process_queue_items()

    def _detect_intent(self, lower_name: str, lower_query: str) -> str | None:
        """Walk the INTENT_ROUTES table and return the first matching intent key."""
        for filename_triggers, slash_triggers, intent_key in INTENT_ROUTES:
            for trigger in filename_triggers:
                if f"{COMMAND_PREFIX}{trigger}" in lower_name:
                    return intent_key
            for trigger in slash_triggers:
                if f"/{trigger}" in lower_query:
                    return intent_key
        return None

    @staticmethod
    def _detect_planner_flags(lower_query: str) -> dict:
        """Phase 6A flags for opt-in planner preview on high-frequency intents."""
        return {
            "planner_mode": ("planner-mode" in lower_query or "/planner" in lower_query),
            "execute_plan": ("/execute" in lower_query or "/execution" in lower_query),
        }

    @staticmethod
    def _load_linked_sources(target_entities: list[str]) -> list[str]:
        """Load explicitly linked vault sources for default Q&A prompts."""
        loaded_sources = []
        max_chars = LOAD_SOURCES_MAX_CHARS_PER_SOURCE
        target_titles = [t.split('|')[0].strip() for t in target_entities]
        for title in target_titles:
            resolved = _resolve_source_paths(title)
            if not resolved:
                continue

            text = "\n\n".join(
                path.read_text(encoding="utf-8")
                for path, _ in resolved
            )
            if max_chars > 0 and len(text) > max_chars:
                text = (
                    text[:max_chars].rstrip()
                    + "\n\n<!-- truncated by PromptWatcher default Q&A -->"
                )
            loaded_sources.append(f"## Source: {title}\n\n{text}")
        return loaded_sources
            
    def process_prompt(self, filepath: Path):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                query_content = f.read()
                
            logging.info(f"Answering prompt {filepath.name} using {self.llm.provider.upper()} ({self.llm.model})...")
            
            # Identify Intent
            target_entities = re.findall(r'\[\[(.*?)\]\]', query_content)
            lower_query = query_content.lower()
            lower_name = filepath.name.lower()
            
            intent_key = self._detect_intent(lower_name, lower_query)

            run_context = (
                self.llm.trace_run(
                    intent=intent_key or "chat",
                    agent=intent_key,
                    trigger_type="prompt_file",
                    command_id=filepath.name,
                    source_event_id=str(filepath),
                    metadata={"target_titles": target_entities},
                )
                if hasattr(self.llm, "trace_run")
                else contextlib.nullcontext()
            )
            with run_context:
                # Execution
                output_path = None
                
                # Special case for non-agent maintenance (keep for now or migrate to agents later)
                if intent_key in ["kb_zip", "kb_unzip", "kb_reset"]:
                    from maintenance.kb_manager import KBManager
                    manager = KBManager(self.rag)
                    if intent_key == "kb_zip": res = f"✅ Backup successful: {manager.zip_kb().name}"
                    elif intent_key == "kb_reset": res = manager.reset_kb()
                    else: res = manager.unzip_kb(target_entities[0] if target_entities else None)
                    
                    output_path = FROM_LLM_DIR / f"✅admin-rpt-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
                    output_path.write_text(f"---\ntitle: \"管理報告\"\ntype: report_admin\n---\n\n{res}", encoding='utf-8')

                # Brain ops — run a cognition/maintenance pass directly (no agent),
                # reusing the busy lock the worker already holds.
                elif intent_key in _BRAIN_OPS:
                    res = self._run_brain_op(intent_key, target_entities)
                    output_path = FROM_LLM_DIR / f"✅admin-rpt-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
                    output_path.write_text(
                        f"---\ntitle: \"{intent_key} 報告\"\ntype: report_admin\n---\n\n{res}",
                        encoding='utf-8',
                    )

                elif intent_key == "repair_tags":
                    from maintenance.repair_tags import repair_tags_interactively
                    repair_tags_interactively(filepath)

                elif intent_key == "research":
                    from services.research_pipeline import ResearchPipeline
                    rp = ResearchPipeline(self.llm)
                    
                    loaded_sources = self._load_linked_sources(target_entities)
                    instruction = lower_query.replace(f"{COMMAND_PREFIX}research", "").strip()
                    if not instruction:
                        instruction = "General topic"
                        
                    content = "\n\n".join(loaded_sources) if loaded_sources else query_content
                    
                    res = rp.run_research(instruction, content)
                    
                    output_path = FROM_LLM_DIR / f"💌re-{filepath.stem}.md"
                    output_path.write_text(
                        f"---\ntitle: \"re: {filepath.stem}\"\ntype: research\n---\n\n{res}",
                        encoding='utf-8'
                    )

                elif intent_key:
                    agent = self.registry.get_agent(intent_key)
                    if agent:
                        # Prepare context
                        context = {
                            "target_titles": [t.split('|')[0].strip() for t in target_entities],
                            "user_directive": query_content,
                            "strategy_id": "recency",
                            "is_full_report": "/full" in lower_query,
                            # Discriminator for agents shared across intents — e.g.
                            # LinterAgent serves both "patrol" (full garden report)
                            # and "linter" (@ling-repair-db, focused DB repair).
                            "intent_key": intent_key,
                        }
                        
                        template_match = re.search(r'/template[:\s]+([\w-]+)', lower_query)
                        if template_match:
                            context["forced_template"] = template_match.group(1)
                            
                        # Specialized context for InsightAgent
                        if intent_key == "insight":
                            context.update(self._detect_planner_flags(lower_query))
                            for s_id in getattr(agent, 'strategies', {}).keys():
                                # `/tag` and `/tags` are documented shortcuts for the
                                # tag-cluster strategy (its skill name is "tag-cluster",
                                # not "tags" — that's its `method:` field).
                                if f"/{s_id}" in lower_query or (s_id == "tag-cluster" and "/tag" in lower_query):
                                    context["strategy_id"] = s_id
                                    break
                        # Specialized context for LingLens/CounterAgent
                        elif intent_key == "lens":
                            confidence = "medium"
                            conf_match = re.search(r'(?:confidence|信心)\s*[:：]\s*(high|medium|low)', lower_query)
                            if conf_match:
                                confidence = conf_match.group(1)
                            context["confidence"] = confidence
                        
                        agent.execute(context)
                    else:
                        logging.warning(f"No agent found for intent: {intent_key}")
                
                else:
                    # Default Chat/Q&A
                    loaded_sources = self._load_linked_sources(target_entities)
                    relevant = self.rag.query_similar_notes(query_content, top_k=settings.SEARCH_DEPTH)
                    context_parts = []
                    if loaded_sources:
                        context_parts.extend(loaded_sources)
                    if relevant:
                        context_parts.extend(relevant)

                    context = "\n---\n".join(context_parts) if context_parts else (INDEX_FILE.read_text('utf-8') if INDEX_FILE.exists() else "")
                    
                    forced_template = None
                    template_match = re.search(r'/template[:\s]+([\w-]+)', lower_query)
                    if template_match:
                        forced_template = template_match.group(1)
                    
                    res = self.llm.answer_query(query_content, context, forced_template=forced_template)
                    
                    trace_ids = self.llm.current_trace_ids() if hasattr(self.llm, "current_trace_ids") else []
                    run_id = self.llm.current_run_id() if hasattr(self.llm, "current_run_id") else None
                    
                    output_path = FROM_LLM_DIR / f"💌re-{filepath.stem}.md"
                    
                    if forced_template:
                        # Template path: the model emits its own YAML frontmatter
                        # + body, so write it through verbatim rather than wrapping
                        # it in the chat-reply envelope. Yields a clean
                        # template-shaped document.
                        output_path = FROM_LLM_DIR / f"📄{forced_template}-{filepath.stem}.md"
                        body = res if res.endswith("\n") else f"{res}\n"
                        output_path.write_text(body, encoding='utf-8')
                        artifact_type = "report"
                        artifact_title = f"{forced_template}: {filepath.stem}"
                    else:
                        trace_meta = ""
                        if run_id or trace_ids:
                            trace_meta = (
                                f"run_id: {run_id or ''}\n"
                                f"trace_ids: {trace_ids}\n"
                            )
                        full_content = (
                            f"---\ntitle: \"re: {filepath.stem}\"\ntype: chat\n{trace_meta}---\n\n"
                            f"> {query_content.strip()}\n\n{res}\n"
                        )
                        output_path.write_text(full_content, encoding='utf-8')
                        artifact_type = "chat"
                        artifact_title = f"re: {filepath.stem}"
                    
                    if hasattr(self.llm, "trace_store"):
                        self.llm.trace_store.record_artifact(
                            path=output_path,
                            artifact_type=artifact_type,
                            title=artifact_title,
                            trace_id=trace_ids[-1] if trace_ids else None,
                            metadata={"run_id": run_id, "trace_ids": trace_ids},
                        )

            self._archive_raw(filepath)
            
        except Exception as e:
            logging.error(f"Error answering {filepath.name}: {str(e)}")
            self._write_error_output(filepath, e)
            self._archive_raw(filepath)

    def _run_brain_op(self, intent_key: str, target_entities: list[str]) -> str:
        """Run a brain-op maintenance/cognition function directly (no agent) and
        return a one-line human summary for the admin report. Reuses the exact
        functions the scheduler/daydream pump call; the worker already holds the
        busy lock, so these run under the same contention discipline."""
        if intent_key == "resynthesize":
            return self._resynthesize(target_entities)

        trace_store = getattr(self.llm, "trace_store", None)
        if intent_key == "dream":
            from maintenance.daily_insight import run_daily_insight
            result = run_daily_insight(self.llm, self.rag, occasion="Manual")
        elif intent_key == "consolidate":
            from maintenance.cortex_consolidation import run_consolidation
            result = run_consolidation(self.llm, self.rag)
        elif intent_key == "decay":
            from maintenance.cortex_decay_pass import run_decay_pass
            result = run_decay_pass(self.llm, self.rag)
        elif intent_key == "ledger":
            from maintenance.cortex_ledger import run_ledger_pass
            result = run_ledger_pass(self.llm, self.rag)
        elif intent_key == "assess":
            if trace_store is None:
                return "skipped：沒有 trace store，無法體檢。"
            from maintenance.self_assessment import run_self_assessment
            result = run_self_assessment(trace_store)
        else:
            return f"未知的大腦指令：{intent_key}"

        status = getattr(result, "status", "done")
        # run_* results expose either .message or .summary — accept both.
        message = getattr(result, "message", None) or getattr(result, "summary", "") or str(result)
        return f"[{status}] {message}"

    def _resynthesize(self, target_entities: list[str]) -> str:
        """Re-queue an already-ingested document for synthesis by copying its
        archived source back into Consolidate/ (ClippingWatcher picks it up).
        Sidecar images are restored too so `images/<title>/` links resolve."""
        from core.config import RAW_CONSOLIDATE_DIR, CONSOLIDATE_DIR
        titles = [t.split('|')[0].strip() for t in target_entities]
        if not titles:
            return "skipped：請以 [[標題]] 指定要重新 synthesis 的文件。"
        done, missing = [], []
        for title in titles:
            src = RAW_CONSOLIDATE_DIR / f"{title}.md"
            if not src.exists():
                missing.append(title)
                continue
            CONSOLIDATE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(CONSOLIDATE_DIR / src.name))
            sidecar = RAW_CONSOLIDATE_DIR / "images" / title
            if sidecar.is_dir():
                dest = CONSOLIDATE_DIR / "images" / title
                if not dest.exists():
                    shutil.copytree(str(sidecar), str(dest))
            done.append(title)
        parts = []
        if done:
            parts.append(f"已重新投入 Consolidate（將重跑 synthesis）：{', '.join(done)}")
        if missing:
            parts.append(f"找不到原始檔（raw/consolidate/）：{', '.join(missing)}")
        return "；".join(parts) or "無動作"

    def _archive_raw(self, filepath: Path):
        if not filepath.exists(): return
        # Ensure we are using the simple name to avoid weird path corruption
        safe_name = os.path.basename(str(filepath))
        dest = RAW_PROMPTS_DIR / safe_name
        
        if dest.exists():
            timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
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
            output_path.write_text(body, encoding='utf-8')
        except Exception as write_error:
            logging.error(f"Failed to write error output for {request_id}: {write_error}")
