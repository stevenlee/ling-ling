import time
import logging
import shutil
import contextlib
import threading
import queue
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
    """Filesystem event handler for Consolidate/ — delegates processing to IngestionPipeline.

    Uses an internal job queue so that multiple files dropped into Consolidate/
    are processed sequentially.  Events that arrive while the system is busy are
    held in the queue and drained either when the current job finishes (via the
    ``scan_existing`` idle callback) or the next time ``_drain_queue`` runs.
    """

    _SUPPORTED_EXTENSIONS = {'.md', '.png', '.jpg', '.jpeg'}
    _IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.tiff'}

    def __init__(self, llm_client, rag_manager):
        super().__init__()
        self.llm = llm_client
        self.rag = rag_manager
        self.pipeline = IngestionPipeline(llm_client, rag_manager)
        # ── Job queue (thread-safe) ──
        self._job_queue: queue.Queue[Path] = queue.Queue()
        self._queued_paths: set[str] = set()
        self._queue_lock = threading.Lock()
        # ── Worker thread (audit R7-G-2) ── processing must NOT run on the
        # watchdog dispatch thread; _handle_event only enqueues + wakes this.
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._stability_delay = 1.0  # filesystem-settle wait; 0 in tests

    # ── FSEvent handlers ─────────────────────────────────────────────

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

        filepath = Path(event.dest_path) if is_move else Path(event.src_path)
        if filepath.name.startswith((".", "@")):
            return
        if filepath.suffix.lower() not in self._SUPPORTED_EXTENSIONS:
            return

        # Enqueue and wake the worker; do NOT process here (audit R7-G-2). This
        # runs on the watchdog dispatch thread — ingestion is heavy work and
        # would block every subsequent filesystem event. The worker applies the
        # stability delay and the existence check before running.
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
            target=self._run_worker, name="ClippingWatcherWorker", daemon=True
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
        """Non-blocking dequeue.  Returns *None* when empty."""
        try:
            return self._job_queue.get_nowait()
        except queue.Empty:
            return None

    def _mark_done(self, filepath: Path):
        """Remove *filepath* from the tracked set so it can be re-queued later."""
        with self._queue_lock:
            self._queued_paths.discard(str(filepath))

    def _drain_queue(self):
        """Acquire the global busy state and process every queued file.

        If the system is already busy the files stay in the queue — they will
        be picked up by the ``scan_existing`` idle callback when the system
        transitions back to idle.
        """
        if not global_busy_state.try_set_busy():
            return
        try:
            self._process_queue_items()
        finally:
            global_busy_state.set_busy(False)

    def _process_queue_items(self) -> int:
        """Drain the queue and process each file.  Caller must hold busy state.

        Returns the number of files that were successfully processed (i.e. no
        longer present in the source directory after archival).
        """
        processed = 0
        while True:
            filepath = self._dequeue()
            if filepath is None:
                break
            try:
                if not filepath.exists():
                    continue
                ui.set_status(f"Preparing: {filepath.name}")
                self.process_file(filepath)
                if not filepath.exists():
                    processed += 1
                ui.success(f"Successfully Consolidated: {filepath.name}")
            except Exception as e:
                ui.error(f"Consolidation Failed: {e}")
                logging.error(f"Consolidation failed for {filepath.name}: {e}")
            finally:
                self._mark_done(filepath)
        return processed

    # ── Startup / idle callback ──────────────────────────────────────

    def scan_existing(self):
        """Scan Consolidate/ for un-processed files and drain the queue.

        Called in two contexts — both guarantee that the global busy state is
        already held by the caller:

        1. **Startup scan** (``main.py``) — inside an explicit
           ``set_busy(True)`` block.
        2. **Idle callback** (``BusyState.set_busy(False)``) — the callback
           mechanism holds the busy flag while callbacks execute.

        Returns the number of files processed (the idle-callback loop uses this
        to decide whether to re-scan).
        """
        from core.config import CONSOLIDATE_DIR
        if CONSOLIDATE_DIR.exists():
            for f in sorted(CONSOLIDATE_DIR.iterdir()):
                if (
                    f.is_file()
                    and not f.name.startswith((".", "@"))
                    and f.suffix.lower() in self._SUPPORTED_EXTENSIONS
                ):
                    if self._enqueue(f):
                        ui.info(f"Found pending file: {f.name}")
        return self._process_queue_items()

    # ── File processing ──────────────────────────────────────────────

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
            logging.exception(f"Error handling markdown {filepath.name}")
            ui.error(f"Clipping 處理失敗：{filepath.name}（{e}）— 檔案留在原處，修正後會重試")

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

    def _is_image_only_dir(self, d: Path) -> bool:
        """True if d is a non-empty directory whose visible entries are all
        image files. Guards the flat-layout sidecar fallback so we never sweep
        an unrelated folder that merely shares the document's name."""
        if not (d.exists() and d.is_dir()):
            return False
        entries = [p for p in d.iterdir() if not p.name.startswith(".")]
        return bool(entries) and all(
            p.is_file() and p.suffix.lower() in self._IMAGE_EXTENSIONS for p in entries
        )

    def _archive_markdown_with_sidecar_images(self, filepath: Path):
        parent, stem = filepath.parent, filepath.stem
        self._archive_processed_file(filepath, RAW_CONSOLIDATE_DIR)
        archive_dir = RAW_CONSOLIDATE_DIR / "images"

        # Canonical sidecar layout is Consolidate/images/<stem>/. An upstream
        # transfer that drops the `images/` parent leaves a flat, doc-named
        # Consolidate/<stem>/ of images instead (root-caused 2026-06-19 to a
        # manual output_dir→Consolidate copy that grabbed the inner folder).
        # Accept that layout too, but only when it's image-only so an unrelated
        # same-named folder is never swept.
        canonical = parent / "images" / stem
        flat = parent / stem
        if canonical.exists() and canonical.is_dir():
            self._archive_processed_file(canonical, archive_dir)
        elif self._is_image_only_dir(flat):
            self._archive_processed_file(flat, archive_dir)

        # Belt-and-suspenders: never let a mismatched layout accumulate
        # silently. If anything named after this doc is still in Consolidate
        # after archival, say so instead of leaving it to pile up.
        for leftover in (parent / stem, parent / "images" / stem):
            if leftover.exists():
                ui.warning(
                    f"Consolidate 仍殘留與「{stem}」同名的項目："
                    f"{leftover.relative_to(parent)}（sidecar 圖片版型不符，"
                    "已保留未搬移，請手動確認）"
                )
                break

    def process_clipping(self, filepath: Path):
        self.process_file(filepath)
