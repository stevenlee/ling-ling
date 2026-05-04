import threading
import logging
from pathlib import Path
import watchdog.events
import re

from core.state import global_busy_state
from core.parser import parse_markdown_metadata

class VaultWatcher(watchdog.events.FileSystemEventHandler):
    """
    Handles deletions and manual modifications in the wiki vault (pages and Notes).
    """
    def __init__(self, rag_manager):
        super().__init__()
        self.rag = rag_manager
        self._timers = {}
        self._timers_lock = threading.Lock()

    def on_created(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
            
        if global_busy_state.is_busy():
            # If busy, reschedule for later instead of dropping
            self._schedule_process(filepath, filepath.stem, delay=30.0)
            return

        filepath = Path(event.src_path)
        title = filepath.stem
        self._schedule_process(filepath, title, delay=2.0)

    def on_deleted(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
        filepath = Path(event.src_path)
        title = filepath.stem
        
        with self._timers_lock:
            if title in self._timers:
                self._timers[title].cancel()
                del self._timers[title]
                
        global_busy_state.set_busy(True)
        try:
            logging.info(f"File deleted in Vault: {title}. Removing from RAG memory...")
            self.rag.delete_document(title)
        finally:
            global_busy_state.set_busy(False)

    def on_modified(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
        filepath = Path(event.src_path)
        title = filepath.stem
        
        # Special case: Scripture (System Settings)
        from core.config import SCRIPTURE_FILE, settings
        if filepath.absolute() == SCRIPTURE_FILE.absolute():
            logging.info("Scripture (Settings) modification detected. Reloading...")
            settings.reload()
            return

        if not filepath.exists():
            return
            
        # logging.info(f"Vault: {title} modified. Reschedule file sync.")
        self._schedule_process(filepath, title, delay=60.0)

    def _schedule_process(self, filepath: Path, title: str, delay: float):
        with self._timers_lock:
            if title in self._timers:
                self._timers[title].cancel()
                
            timer = threading.Timer(delay, self._process_modification, args=[filepath, title])
            self._timers[title] = timer
            timer.start()

    def _process_modification(self, filepath: Path, title: str):
        with self._timers_lock:
            if title in self._timers:
                del self._timers[title]
                
        if not filepath.exists():
            return
            
        if global_busy_state.is_busy():
            # Reschedule and try again later to avoid lock clashing
            self._schedule_process(filepath, title, delay=10.0)
            return
            
        # 0. Whitelist Filter: Only index pages/ and Notes/
        from core.config import PAGES_DIR, NOTES_DIR
        abs_path = str(filepath.absolute())
        is_in_pages = abs_path.startswith(str(PAGES_DIR.absolute()))
        is_in_notes = abs_path.startswith(str(NOTES_DIR.absolute()))
        
        if not (is_in_pages or is_in_notes):
            return
            
        try:
            content = filepath.read_text(encoding='utf-8')
            
            # 使用統一解析器提取標籤
            meta = parse_markdown_metadata(content)
            original_tags = meta.get('tags', [])
            
            # --- 智能標籤強點 (Smart Tag Enrichment) ---
            from core.tag_manager import TagManager
            from core.config import TAG_MAP_FILE
            tm = TagManager(TAG_MAP_FILE)
            
            new_tags = set(original_tags)
            tags_to_translate = []
            
            for tag in original_tags:
                if tm.is_bilingual_needed(tag):
                    eq = tm.get_equivalent(tag)
                    if eq:
                        new_tags.add(tm.normalize(eq))
                    else:
                        tags_to_translate.append(tag)
            
            # 學習新標籤 (如果本機對照表沒有)
            if tags_to_translate:
                from services.llm_client import LLMClient
                llm = LLMClient()
                learned_map = llm.translate_tags(tags_to_translate)
                for src, target in learned_map.items():
                    tm.add_mapping(src, target)
                    new_tags.add(tm.normalize(target))
            
            final_tags = sorted(list(new_tags))
            
            # 回寫 Obsidian (如果標籤有增加)
            if final_tags != original_tags:
                try:
                    self._update_file_tags(filepath, final_tags)
                    logging.info(f"Vault: Enriched tags for {title}: {original_tags} -> {final_tags}")
                except Exception as e:
                    logging.error(f"Vault: Failed to write back enriched tags for {title}: {e}")
            
            from core.ui import ui
            ui.info(f"🛍️ Syncing Brain...：[bold cyan]{title}[/bold cyan] (๑˃̵ᴗ˂̵)و")
            
            logging.info(f"File sync settled: {title}. Updating memory and index...")
            global_busy_state.set_busy(True)
            try:
                # 1. Update RAG
                self.rag.add_document(filepath, title, content, tags=final_tags)
                
                # 2. Update index.md
                from core.vault_utils import update_wiki_index
                update_wiki_index(filepath, title)
            finally:
                global_busy_state.set_busy(False)
        except Exception as e:
            logging.error(f"Failed to update RAG on modification for {title}: {e}")

    def _update_file_tags(self, filepath: Path, tags: list[str]):
        from core.vault_utils import update_file_tags
        update_file_tags(filepath, tags)
