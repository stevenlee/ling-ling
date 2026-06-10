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
    def __init__(self, rag_manager, llm_client=None):
        super().__init__()
        self.rag = rag_manager
        self.llm = llm_client
        self._timers = {}
        self._timers_lock = threading.Lock()

    def on_created(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return

        filepath = Path(event.src_path)
        if not self._should_watch(filepath):
            return

        if global_busy_state.is_busy():
            # If busy, reschedule for later instead of dropping
            self._schedule_process(filepath, filepath.stem, delay=30.0)
            return

        title = filepath.stem
        self._schedule_process(filepath, title, delay=2.0)

    def on_deleted(self, event):
        # Deleting a whole article folder (pages/<title>/) only emits a
        # directory event on some platforms — the per-file events never
        # arrive, which used to leave every chunk stranded in ChromaDB.
        # An orphan sweep reconciles the DB against the filesystem instead.
        if event.is_directory:
            if self._is_indexed_dir(Path(event.src_path)):
                self._schedule_orphan_sweep()
            return
        if not event.src_path.endswith('.md'):
            return
        filepath = Path(event.src_path)
        if not self._should_watch(filepath):
            return

        title = filepath.stem

        if self._should_refresh_index_only(filepath):
            with self._timers_lock:
                if title in self._timers:
                    self._timers[title].cancel()
                    del self._timers[title]
            from core.vault_utils import update_wiki_index
            update_wiki_index(filepath, title, sync_reading_index=True)
            return
        
        with self._timers_lock:
            if title in self._timers:
                self._timers[title].cancel()
                del self._timers[title]

        self._process_deletion(title)

    def _process_deletion(self, title: str, attempt: int = 0):
        # Keyed separately from modification timers so a later modify event
        # on the same title cannot cancel a pending deletion retry.
        timer_key = f"del::{title}"
        with self._timers_lock:
            self._timers.pop(timer_key, None)

        if not global_busy_state.try_set_busy():
            if attempt >= 10:
                logging.error(
                    f"Vault: giving up on delete of {title} after {attempt} retries; "
                    f"RAG may hold a stale entry until the next full sync."
                )
                return
            with self._timers_lock:
                timer = threading.Timer(5.0, self._process_deletion, args=[title, attempt + 1])
                self._timers[timer_key] = timer
                timer.start()
            return

        try:
            logging.info(f"File deleted in Vault: {title}. Removing from RAG memory...")
            self.rag.delete_document(title)
            from core.vault_utils import update_wiki_index
            update_wiki_index(sync_reading_index=True)
        finally:
            global_busy_state.set_busy(False)

    def on_moved(self, event):
        """Renames/moves used to leave the old path's chunks behind forever
        (no handler existed). Treat as delete-old + index-new."""
        src = Path(event.src_path)
        dest = Path(getattr(event, "dest_path", "") or "")

        if event.is_directory:
            # Folder rename/move: sweep clears chunks keyed by the old
            # paths; re-index whatever now lives at the destination.
            if self._is_indexed_dir(src) or self._is_indexed_dir(dest):
                self._schedule_orphan_sweep()
                if dest and self._is_indexed_dir(dest) and dest.exists():
                    for file in dest.rglob("*.md"):
                        self._schedule_process(file, file.stem, delay=5.0)
            return

        if str(src).endswith(".md") and self._should_watch(src):
            self._process_deletion(src.stem)
        if str(dest).endswith(".md") and self._should_watch(dest):
            self._schedule_process(dest, dest.stem, delay=2.0)

    def on_modified(self, event):
        if event.is_directory or not event.src_path.endswith('.md'):
            return
        filepath = Path(event.src_path)
        if not self._should_watch(filepath):
            return

        title = filepath.stem
        
        # Special case: Scripture (System Settings)
        from core.config import SCRIPTURE_FILE, settings
        if filepath.absolute() == SCRIPTURE_FILE.absolute():
            logging.info("Scripture (Settings) modification detected. Reloading...")
            settings.reload()
            return

        if not filepath.exists():
            return

        if self._should_refresh_index_only(filepath):
            self._schedule_process(filepath, title, delay=2.0)
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
            
        if not global_busy_state.try_set_busy():
            # Reschedule and try again later to avoid lock clashing
            self._schedule_process(filepath, title, delay=10.0)
            return
            
        try:
            # 0. Whitelist Filter: Only index pages/ and Notes/
            if self._should_refresh_index_only(filepath):
                logging.info(f"Reading index modified: {title}. Rebuilding index.md...")
                from core.vault_utils import update_wiki_index
                update_wiki_index(filepath, title)
                return

            if not self._should_index(filepath):
                return
                
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
            if tags_to_translate and self.llm:
                learned_map = self.llm.translate_tags(tags_to_translate)
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
            # 1. Update RAG
            self.rag.add_document(filepath, title, content, tags=final_tags)
            
            # 2. Update index.md
            from core.vault_utils import update_wiki_index
            update_wiki_index(filepath, title, sync_reading_index=True)
        except Exception as e:
            logging.exception(f"Failed to update RAG on modification for {title}")
            try:
                from core.ui import ui
                ui.error(f"同步失敗：{title} 未寫入記憶（{e}）")
            except Exception:
                pass
        finally:
            global_busy_state.set_busy(False)

    def _is_indexed_dir(self, path: Path) -> bool:
        from core.config import PAGES_DIR, NOTES_DIR
        abs_path = path.absolute()
        return self._is_relative_to(abs_path, PAGES_DIR.absolute()) or self._is_relative_to(
            abs_path, NOTES_DIR.absolute()
        )

    def _schedule_orphan_sweep(self, delay: float = 5.0):
        """Debounced full reconcile of ChromaDB against the filesystem."""
        key = "orphan::sweep"
        with self._timers_lock:
            if key in self._timers:
                self._timers[key].cancel()
            timer = threading.Timer(delay, self._process_orphan_sweep)
            self._timers[key] = timer
            timer.start()

    def _process_orphan_sweep(self, attempt: int = 0):
        key = "orphan::sweep"
        with self._timers_lock:
            self._timers.pop(key, None)

        if not global_busy_state.try_set_busy():
            if attempt >= 10:
                logging.error(
                    "Vault: orphan sweep gave up after lock contention; "
                    "stale chunks remain until the daily sweep."
                )
                return
            with self._timers_lock:
                timer = threading.Timer(5.0, self._process_orphan_sweep, args=[attempt + 1])
                self._timers[key] = timer
                timer.start()
            return

        try:
            result = self.rag.prune_orphan_chunks()
            if result.get("deleted_chunks"):
                from core.ui import ui
                ui.info(
                    f"🧹 已清除 {result['deleted_chunks']} 個殘留 chunks"
                    f"（{result['orphan_docs']} 份已刪除文件）"
                )
            from core.vault_utils import update_wiki_index
            update_wiki_index(sync_reading_index=True)
        except Exception:
            logging.exception("Vault: orphan sweep failed")
        finally:
            global_busy_state.set_busy(False)

    def _update_file_tags(self, filepath: Path, tags: list[str]):
        from core.vault_utils import update_file_tags
        update_file_tags(filepath, tags)

    def _should_watch(self, filepath: Path) -> bool:
        from core.config import SCRIPTURE_FILE
        return (
            filepath.absolute() == SCRIPTURE_FILE.absolute()
            or self._should_refresh_index_only(filepath)
            or self._should_index(filepath)
        )

    def _should_refresh_index_only(self, filepath: Path) -> bool:
        from core.vault_utils import READING_INDEX_FILE
        return filepath.absolute() == READING_INDEX_FILE.absolute()

    def _should_index(self, filepath: Path) -> bool:
        from core.config import PAGES_DIR, NOTES_DIR
        abs_path = filepath.absolute()
        return self._is_relative_to(abs_path, PAGES_DIR.absolute()) or self._is_relative_to(abs_path, NOTES_DIR.absolute())

    def _is_relative_to(self, path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False
