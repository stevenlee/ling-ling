import time
import logging
import shutil
import contextlib
from pathlib import Path
from datetime import datetime
import watchdog.events

from core.state import global_busy_state
from services.media_processor import process_image
from services.ingestion_pipeline import IngestionPipeline
from core.config import (
    INDEX_FILE, PAGES_DIR,
    RAW_CONSOLIDATE_DIR, RAW_ASSETS_DIR, ASSETS_DIR
)
from core.ui import ui


class ClippingWatcher(watchdog.events.FileSystemEventHandler):
    """Thin filesystem event handler — delegates all document processing to IngestionPipeline."""

    def __init__(self, llm_client, rag_manager):
        super().__init__()
        self.llm = llm_client
        self.rag = rag_manager
        self.pipeline = IngestionPipeline(llm_client, rag_manager)

    def on_created(self, event):
        self._handle_event(event)

    def on_moved(self, event):
        if not event.is_directory:
            # ONLY handle if the file is being moved INTO the monitored directory
            from core.config import CONSOLIDATE_DIR
            dest_path = Path(event.dest_path)
            if CONSOLIDATE_DIR in dest_path.parents:
                self._handle_event(event, is_move=True)

    def _handle_event(self, event, is_move=False):
        if event.is_directory:
            return
            
        if global_busy_state.is_busy():
            return
        
        filepath = Path(event.dest_path) if is_move else Path(event.src_path)
        if filepath.name.startswith((".", "@")):
            return
            
        supported_extensions = ['.md', '.png', '.jpg', '.jpeg']
        if filepath.suffix.lower() not in supported_extensions:
            return
        
        global_busy_state.set_busy(True)
        try:
            ui.set_status(f"Preparing: {filepath.name}")
            time.sleep(1) # Small buffer for file system stability
            
            if not filepath.exists():
                return
                
            self.process_file(filepath)
            ui.success(f"Successfully Consolidated: {filepath.name}")
        except Exception as e:
            ui.error(f"Consolidation Failed: {e}")
        finally:
            ui.set_status("Ling Ling is waiting... (๑´ㅂ`๑)zZ... (Ctrl-C to Quit)", is_busy=False)
            global_busy_state.set_busy(False)

    def scan_existing(self):
        """Scan for files already in the directory at startup."""
        from core.config import CONSOLIDATE_DIR
        processed = 0
        if CONSOLIDATE_DIR.exists():
            supported_extensions = {'.md', '.png', '.jpg', '.jpeg'}
            for f in sorted(CONSOLIDATE_DIR.iterdir()):
                if (
                    f.is_file()
                    and not f.name.startswith((".", "@"))
                    and f.suffix.lower() in supported_extensions
                ):
                    ui.info(f"Startup scan found: {f.name}")
                    self.process_file(f)
                    if not f.exists():
                        processed += 1
        return processed
        
    def process_file(self, filepath: Path):
        run_context = (
            self.llm.trace_run(
                intent="ingest",
                agent="IngestionPipeline",
                trigger_type="clipping_file",
                command_id=filepath.name,
                source_event_id=str(filepath),
                metadata={"suffix": filepath.suffix.lower()},
            )
            if hasattr(self.llm, "trace_run")
            else contextlib.nullcontext()
        )
        with run_context:
            ext = filepath.suffix.lower()
            if ext == '.md':
                self._handle_markdown(filepath)
            elif ext in ['.png', '.jpg', '.jpeg']:
                self._handle_image(filepath)

    def _handle_markdown(self, filepath: Path):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            self.pipeline.ingest_markdown(content, filepath)
            
            # Archive
            self._archive_markdown_with_sidecar_images(filepath)
            ui.success(f"Clipping complete: [bold]{filepath.stem}[/bold]")
            
        except Exception as e:
            logging.error(f"Error handling markdown {filepath.name}: {e}")

    def _handle_image(self, filepath: Path):
        index_content = INDEX_FILE.read_text('utf-8') if INDEX_FILE.exists() else ""
        result = process_image(filepath, self.llm, index_content, ASSETS_DIR)
        if result:
            ingested = self.pipeline.ingest_to_wiki(None, filepath, llm_result=result)
            if ingested:
                self._archive_processed_file(filepath, RAW_ASSETS_DIR)

    # ── Archival ─────────────────────────────────────────────────────

    def _archive_processed_file(self, filepath: Path, archive_dir: Path):
        if not filepath.exists():
            return

        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / filepath.name
        if dest.exists():
            dest = archive_dir / f"{filepath.stem}_{datetime.now().strftime('%Y%m%d-%H%M%S')}{filepath.suffix}"
        shutil.move(str(filepath), str(dest))

    def _archive_markdown_with_sidecar_images(self, filepath: Path):
        self._archive_processed_file(filepath, RAW_CONSOLIDATE_DIR)
        sidecar_dir = filepath.parent / "images" / filepath.stem
        if sidecar_dir.exists() and sidecar_dir.is_dir():
            archive_dir = RAW_CONSOLIDATE_DIR / "images"
            self._archive_processed_file(sidecar_dir, archive_dir)

    def process_clipping(self, filepath: Path):
        self.process_file(filepath)
