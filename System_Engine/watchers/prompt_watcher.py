import time
import re
import threading
import logging
import os
import shutil
from pathlib import Path
from datetime import datetime
import watchdog.events

from core.state import global_busy_state
from core.config import TO_LLM_DIR, FROM_LLM_DIR, RAW_PROMPTS_DIR, INDEX_FILE, PROJECT_ROOT, COMMAND_PREFIX, settings
from core.ui import ui

LOCK_FILE = PROJECT_ROOT / ".kb_lock"

class PromptWatcher(watchdog.events.FileSystemEventHandler):
    def __init__(self, llm_client, rag_manager):
        super().__init__()
        self.llm = llm_client
        self.rag = rag_manager
        
        self._processed_files = set()
        self._processed_lock = threading.Lock()

    def on_created(self, event):
        if event.is_directory or not (event.src_path.endswith('.md') or event.src_path.endswith('.txt')):
            return
            
        filepath = Path(event.src_path)
        
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
            
    def _remove_from_processed(self, path_str):
        with self._processed_lock:
            self._processed_files.discard(path_str)
            
    def process_prompt(self, filepath: Path):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                query_content = f.read()
                
            logging.info(f"Answering prompt {filepath.name} using {self.llm.provider.upper()} ({self.llm.model})...")
            
            # Intercept Triggers
            target_entities = re.findall(r'\[\[(.*?)\]\]', query_content)
            lower_query = query_content.lower()
            
            is_merge = (f"{COMMAND_PREFIX}merge" in filepath.name or "/merge" in query_content) and len(target_entities) >= 2
            is_patrol = f"{COMMAND_PREFIX}patrol" in filepath.name or "/patrol" in query_content
            is_patrol_tags = f"{COMMAND_PREFIX}patrol-tags" in filepath.name or "/patrol-tags" in query_content
            is_repair_tags = f"{COMMAND_PREFIX}repair-tags" in filepath.name or "/repair-tags" in query_content
            is_insight = f"{COMMAND_PREFIX}insight" in filepath.name or "/insight" in query_content
            is_kb_zip = f"{COMMAND_PREFIX}zip" in filepath.name or "/zip" in query_content
            is_kb_unzip = f"{COMMAND_PREFIX}unzip" in filepath.name or "/unzip" in query_content
            is_kb_reset = f"{COMMAND_PREFIX}RESET" in filepath.name or "/RESET" in query_content
            is_repair_db = f"{COMMAND_PREFIX}repair-db" in filepath.name or "/repair-db" in query_content
            
            # Intent Deduplication
            intent_key = None
            if is_merge: intent_key = "merge"
            elif is_patrol: intent_key = "patrol"
            elif is_patrol_tags: intent_key = "patrol_tags"
            elif is_repair_tags: intent_key = "repair_tags"
            elif is_insight: intent_key = "insight"
            elif is_repair_db: intent_key = "repair_db"
            elif is_kb_zip: intent_key = "kb_zip"
            elif is_kb_unzip: intent_key = "kb_unzip"
            elif is_kb_reset: intent_key = "kb_reset"
            
            if intent_key:
                with self._processed_lock:
                    if intent_key in self._processed_files:
                        logging.info(f"Ignored duplicate intent: {intent_key}")
                        return
                    self._processed_files.add(intent_key)
                threading.Timer(60.0, self._remove_from_processed, args=[intent_key]).start()
            
            date_created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            output_yaml = ""
            output_path = None
            
            # --- Execution Branches ---
            if is_kb_zip or is_kb_unzip or is_kb_reset:
                from maintenance.kb_manager import KBManager
                manager = KBManager(self.rag)
                if is_kb_zip:
                    zip_path = manager.zip_kb()
                    res = f"✅ Backup successful: {zip_path.name}"
                elif is_kb_reset:
                    res = manager.reset_kb()
                else: # unzip
                    zip_matches = re.findall(r'\[\[(.*?)\]\]', query_content)
                    res = manager.unzip_kb(zip_matches[0] if zip_matches else None)
                
                output_yaml = f"---\ntitle: \"管理報告: {filepath.stem}\"\ntype: report_admin\n---\n\n{res}\n"
                output_path = FROM_LLM_DIR / f"✅admin-rpt-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"

            elif is_merge:
                from agents.merge_agent import MergeAgent
                merger = MergeAgent(PROJECT_ROOT)
                clean_targets = [t.split('|')[0].strip() for t in target_entities]
                res = merger.merge_entities(clean_targets, self.llm, self.rag, user_directive=query_content)
                output_yaml = f"---\ntitle: \"合併報告: {filepath.stem}\"\ntype: report_merge\n---\n\n{res}\n"
                output_path = FROM_LLM_DIR / f"✅merge-rpt-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"

            elif is_patrol_tags:
                from agents.tag_patrol_agent import TagPatrolAgent
                logging.info("Initiating Tag Patrol Agent (Scanning Tags)...")
                agent = TagPatrolAgent()
                res = agent.generate_report()
                output_yaml = f"---\ntitle: \"標籤巡邏報告: {filepath.stem}\"\ntype: report_tags\n---\n\n{res}\n"
                output_path = FROM_LLM_DIR / f"✅tag-patrol-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"

            elif is_repair_tags:
                from maintenance.repair_tags import repair_tags_interactively
                repair_tags_interactively(filepath)
                self._archive_raw(filepath)
                return

            elif is_patrol:
                from maintenance.wiki_linter import WikiLinter
                linter = WikiLinter(PROJECT_ROOT, self.rag)
                rep = linter.perform_repair() if settings.SELF_HEALING else ""
                res = linter.generate_report(self.llm) + (f"\n\n### 🛠️ 自動修復\n{rep}" if rep else "")
                output_yaml = f"---\ntitle: \"巡邏報告: {filepath.stem}\"\ntype: report_patrol\n---\n\n{res}\n"
                output_path = FROM_LLM_DIR / f"✅patrol-rpt-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"

            elif is_repair_db:
                from maintenance.wiki_linter import WikiLinter
                linter = WikiLinter(PROJECT_ROOT, self.rag)
                res = linter.perform_repair()
                output_yaml = f"---\ntitle: \"資料庫維護報告\"\ntype: report_maintenance\n---\n\n{res}\n"
                output_path = FROM_LLM_DIR / f"✅repair-db-rpt-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"

            elif is_insight:
                from agents.insight_agent import InsightAgent
                agent = InsightAgent(PROJECT_ROOT, self.llm, self.rag)
                triggered_strategy = "recency"
                for s_id in agent.strategies.keys():
                    if f"/{s_id}" in lower_query or (s_id == "tags" and "/tag" in lower_query):
                        triggered_strategy = s_id
                        break
                
                if "/full" in lower_query:
                    res = agent.generate_full_insight(user_directive=query_content)
                else:
                    res = agent.generate_insight(strategy_id=triggered_strategy, user_directive=query_content)
                
                output_yaml = f"---\ntitle: \"洞察回應: {filepath.stem}\"\ntype: report_insight\n---\n\n{res}\n"
                output_path = FROM_LLM_DIR / f"💌re-{filepath.stem}.md"

            else:
                relevant = self.rag.query_similar_notes(query_content, top_k=settings.SEARCH_DEPTH)
                context = "\n---\n".join(relevant) if relevant else (INDEX_FILE.read_text('utf-8') if INDEX_FILE.exists() else "")
                res = self.llm.answer_query(query_content, context)
                output_yaml = f"---\ntitle: \"re: {filepath.stem}\"\ntype: chat\n---\n\n> {query_content.strip()}\n\n{res}\n"
                output_path = FROM_LLM_DIR / f"💌re-{filepath.stem}.md"

            if output_path:
                # 更精確的邏輯：如果 res 已經是完整的 Markdown (包含 ---)，就直接用它，避免雙重標頭
                if 'res' in locals() and res.strip().startswith("---"):
                    output_path.write_text(res, encoding='utf-8')
                else:
                    output_path.write_text(output_yaml, encoding='utf-8')
            self._archive_raw(filepath)
            
        except Exception as e:
            logging.error(f"Error answering {filepath.name}: {str(e)}")

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
