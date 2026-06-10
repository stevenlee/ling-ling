import os
import time
import logging
import watchdog.observers

from core.config import (
    LLM_PROVIDER, PROJECT_ROOT, CLIPPINGS_DIR, CONSOLIDATE_DIR,
    TO_LLM_DIR, PAGES_DIR, NOTES_DIR, SCRIPTURE_DIR, PID_FILE, ensure_directories, settings
)
from core.vault_utils import READING_INDEX_FILE, ensure_wiki_indexes
from core.utils import acquire_pid_lock
from core.state import global_busy_state
from services.llm_client import LLMClient
from services.rag_manager import RAGManager
from watchers.clipping_watcher import ClippingWatcher
from watchers.prompt_watcher import PromptWatcher
from watchers.vault_watcher import VaultWatcher
from watchers.maintenance_scheduler import MaintenanceScheduler

from core.version import VERSION
from core.ui import ui, setup_rich_logging

def main():
    # 0. Setup Pretty UI
    setup_rich_logging()
    ui.start(VERSION)

    # 1. Acquire PID Lock
    acquire_pid_lock(PID_FILE)

    # 2. Initialize core dependencies
    ensure_directories()
    settings.reload()  # Load initial settings from Wiki
    ensure_wiki_indexes()
    llm_client = LLMClient()
    rag_manager = RAGManager()

    # 2.1. Apply any pending DB migrations before watchers start writing.
    # Failures are logged but non-fatal (the migration stays pending and
    # will be retried next launch).
    from maintenance.migrate import apply_pending
    try:
        applied = apply_pending(rag_manager)
        if applied:
            ui.info(f"Applied {len(applied)} DB migration(s): {[m['id'] for m in applied]}")
    except Exception as e:
        logging.error(f"Migration runner crashed (continuing without migrating): {e}")

    # 3. Initialize Watchers
    event_handler_clippings = ClippingWatcher(llm_client, rag_manager)
    event_handler_prompts = PromptWatcher(llm_client, rag_manager)
    event_handler_vault = VaultWatcher(rag_manager, llm_client)
    
    # 3.1. Register idle callbacks: re-scan directories on busy→idle to catch dropped events
    global_busy_state.register_idle_callback(event_handler_clippings.scan_existing)
    global_busy_state.register_idle_callback(event_handler_prompts.scan_existing)

    # 3.2. Facet backfill pump — registered LAST so user-work queues always
    # drain before the pump gets a turn (low-priority by callback order).
    from maintenance.facet_backfill import FacetBackfillPump
    facet_pump = FacetBackfillPump(llm_client, rag_manager)
    global_busy_state.register_idle_callback(facet_pump.on_idle)
    
    # 4. Schedule Watchdogs
    observer = watchdog.observers.Observer()
    
    # 1. Manual Clippings -> Move from 'Clippings/' to 'Consolidate/' to process
    observer.schedule(event_handler_clippings, str(CONSOLIDATE_DIR), recursive=False)
    
    # 2. System Commands -> Use 'toLingLing/' for @ling- commands
    observer.schedule(event_handler_prompts, str(TO_LLM_DIR), recursive=False)
    
    # Watch only content that should be mirrored into RAG. Watching the whole
    # vault creates timers for daemon outputs, archives, logs, and Obsidian
    # metadata churn that are ignored later anyway.
    observer.schedule(event_handler_vault, str(PAGES_DIR), recursive=True)
    observer.schedule(event_handler_vault, str(NOTES_DIR), recursive=True)
    observer.schedule(event_handler_vault, str(SCRIPTURE_DIR), recursive=True)
    observer.schedule(event_handler_vault, str(READING_INDEX_FILE.parent), recursive=False)
    observer.start()
    
    # 5. Startup Scan: Process existing files in Ingest/Command folders
    ui.info("Scanning for existing files in Consolidate and toLingLing...")
    global_busy_state.set_busy(True)
    try:
        event_handler_clippings.scan_existing()
        event_handler_prompts.scan_existing()
    finally:
        global_busy_state.set_busy(False, fire_callbacks=False)
    
    # 5. Start Background Maintenance Scheduler
    scheduler = MaintenanceScheduler(PROJECT_ROOT, llm_client, rag_manager)
    scheduler.start()

    # 5.1. Startup kick: covers the daemon-starts-idle case where no
    # busy→idle edge would otherwise wake the pump.
    facet_pump.kick()
    
    ui.info(f"Provider: [bold cyan]{LLM_PROVIDER}[/bold cyan] | Model: [bold green]{llm_client.model}[/bold green] | Role: [bold yellow]{settings.AGENT_ROLE}[/bold yellow]")
    ui.info("☀️ 반가워요!(Ban-ga-wo-yo!) (๑˃̵ᴗ˂̵)و")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        ui.stop()
        ui.info("まだね~ Another 40-hours-practice day! (๑˃̵ᴗ˂̵)و ✨")
    observer.join()

if __name__ == "__main__":
    main()
