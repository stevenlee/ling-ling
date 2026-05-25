import time
import re
import threading
import logging
import os
import shutil
import contextlib
from pathlib import Path
from datetime import datetime
import watchdog.events

from core.state import global_busy_state
from core.config import TO_LLM_DIR, FROM_LLM_DIR, RAW_PROMPTS_DIR, INDEX_FILE, PROJECT_ROOT, COMMAND_PREFIX, settings
from core.ui import ui
from agents.registry import AgentRegistry

LOCK_FILE = PROJECT_ROOT / ".kb_lock"

# Declarative intent routing table.
# Order matters: longer prefixes (e.g. "patrol-tags") must appear before shorter
# ones (e.g. "patrol") to prevent false matches.
# Each entry: (filename_triggers, slash_triggers, intent_key)
INTENT_ROUTES = [
    (["merge"],                              ["merge"],       "merge"),
    (["lens", "count"],                      ["lens", "count"], "lens"),
    (["patrol-tags"],                        ["patrol-tags"], "patrol_tags"),
    (["repair-tags"],                        ["repair-tags"], "repair_tags"),
    (["patrol"],                             ["patrol"],      "patrol"),
    (["repair-db"],                          ["repair-db"],   "linter"),
    (["insight"],                            ["insight"],     "insight"),
    (["plan"],                               ["plan"],        "plan"),
    (["do"],                                 ["do"],          "do"),
    (["zip"],                                ["zip"],         "kb_zip"),
    (["unzip"],                              ["unzip"],       "kb_unzip"),
    (["reset"],                              ["reset"],       "kb_reset"),
]

class PromptWatcher(watchdog.events.FileSystemEventHandler):
    def __init__(self, llm_client, rag_manager):
        super().__init__()
        self.llm = llm_client
        self.rag = rag_manager
        self.registry = AgentRegistry(self.llm, self.rag)
        
        self._processed_files = set()
        self._processed_lock = threading.Lock()

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
        if not (filepath.suffix.lower() in ['.md', '.txt']):
            return
            
        with self._processed_lock:
            if str(filepath) in self._processed_files:
                return
            self._processed_files.add(str(filepath))
            
        threading.Timer(10.0, self._remove_from_processed, args=[str(filepath)]).start()
        
        # Debounce
        time.sleep(1)
        
        if not filepath.exists():
            return
            
        # Check for KB Lock
        if LOCK_FILE.exists():
            ui.info(f"系統鎖定中 (.kb_lock)。跳過處理：{filepath.name}")
            return

        # Respect global busy state — file stays in toLingLing/ for re-scan on idle
        if global_busy_state.is_busy():
            ui.info(f"⏳ 系統忙碌中，指令已排隊等待：{filepath.name}")
            return

        ui.cmd_received(filepath.name)
        global_busy_state.set_busy(True)
        try:
            ui.set_status(f"正在處理指令：{filepath.name}")
            self.process_prompt(filepath)
            ui.success(f"任務完成：{filepath.name}")
        except Exception as e:
            ui.error(f"指令執行失敗：{e}")
        finally:
            ui.set_status("Ling Ling is waiting... (๑´ㅂ`๑)zZ... (Ctrl-C to Quit)", is_busy=False)
            global_busy_state.set_busy(False)
            
    def scan_existing(self):
        """Scan for prompts already in the directory at startup or after idle."""
        from core.config import TO_LLM_DIR
        processed = 0
        if TO_LLM_DIR.exists():
            for f in sorted(TO_LLM_DIR.iterdir()):
                if f.is_file() and f.suffix.lower() in ['.md', '.txt']:
                    ui.info(f"Startup scan found prompt: {f.name}")
                    ui.cmd_received(f.name)
                    try:
                        ui.set_status(f"正在處理指令：{f.name}")
                        self.process_prompt(f)
                        if not f.exists():
                            processed += 1
                            ui.success(f"任務完成：{f.name}")
                    except Exception as e:
                        ui.error(f"指令執行失敗：{e}")
                    finally:
                        ui.set_status("Ling Ling is waiting... (๑´ㅂ`๑)zZ... (Ctrl-C to Quit)", is_busy=False)
        return processed
            
    def _remove_from_processed(self, path_str):
        with self._processed_lock:
            self._processed_files.discard(path_str)

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

            
            # Deduplication
            if intent_key:
                with self._processed_lock:
                    if intent_key in self._processed_files:
                        logging.info(f"Ignored duplicate intent: {intent_key}")
                        return
                    self._processed_files.add(intent_key)
                threading.Timer(60.0, self._remove_from_processed, args=[intent_key]).start()
            
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

                elif intent_key == "repair_tags":
                    from maintenance.repair_tags import repair_tags_interactively
                    repair_tags_interactively(filepath)

                elif intent_key:
                    agent = self.registry.get_agent(intent_key)
                    if agent:
                        # Prepare context
                        context = {
                            "target_titles": [t.split('|')[0].strip() for t in target_entities],
                            "user_directive": query_content,
                            "strategy_id": "recency",
                            "is_full_report": "/full" in lower_query
                        }
                        
                        template_match = re.search(r'/template[:\s]+([\w-]+)', lower_query)
                        if template_match:
                            context["forced_template"] = template_match.group(1)
                            
                        # Specialized context for InsightAgent
                        if intent_key == "insight":
                            context.update(self._detect_planner_flags(lower_query))
                            for s_id in getattr(agent, 'strategies', {}).keys():
                                if f"/{s_id}" in lower_query or (s_id == "tags" and "/tag" in lower_query):
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
                    relevant = self.rag.query_similar_notes(query_content, top_k=settings.SEARCH_DEPTH)
                    context = "\n---\n".join(relevant) if relevant else (INDEX_FILE.read_text('utf-8') if INDEX_FILE.exists() else "")
                    res = self.llm.answer_query(query_content, context)
                    
                    trace_ids = self.llm.current_trace_ids() if hasattr(self.llm, "current_trace_ids") else []
                    run_id = self.llm.current_run_id() if hasattr(self.llm, "current_run_id") else None
                    trace_meta = ""
                    if run_id or trace_ids:
                        trace_meta = (
                            f"run_id: {run_id or ''}\n"
                            f"trace_ids: {trace_ids}\n"
                        )
                    output_path = FROM_LLM_DIR / f"💌re-{filepath.stem}.md"
                    output_path.write_text(
                        f"---\ntitle: \"re: {filepath.stem}\"\ntype: chat\n{trace_meta}---\n\n"
                        f"> {query_content.strip()}\n\n{res}\n",
                        encoding='utf-8',
                    )
                    if hasattr(self.llm, "trace_store"):
                        self.llm.trace_store.record_artifact(
                            path=output_path,
                            artifact_type="chat",
                            title=f"re: {filepath.stem}",
                            trace_id=trace_ids[-1] if trace_ids else None,
                            metadata={"run_id": run_id, "trace_ids": trace_ids},
                        )

            self._archive_raw(filepath)
            
        except Exception as e:
            logging.error(f"Error answering {filepath.name}: {str(e)}")
            self._write_error_output(filepath, e)
            self._archive_raw(filepath)

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
        output_path = FROM_LLM_DIR / f"❌err-{request_id}.md"
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
