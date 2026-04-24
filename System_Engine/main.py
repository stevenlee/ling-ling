import os
import time
import logging
import watchdog.observers

from core.config import (
    LLM_PROVIDER, PROJECT_ROOT, WIKI_VAULT_DIR, CLIPPINGS_DIR, 
    TO_LLM_DIR, PAGES_DIR, NOTES_DIR, SCRIPTURE_DIR, PID_FILE, ensure_directories, settings
)
from core.utils import acquire_pid_lock
from services.llm_client import LLMClient
from services.rag_manager import RAGManager
from watchers.clipping_watcher import ClippingWatcher
from watchers.prompt_watcher import PromptWatcher
from watchers.vault_watcher import VaultWatcher
from watchers.insight_scheduler import InsightScheduler

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
    llm_client = LLMClient()
    rag_manager = RAGManager()
    
    # 3. Initialize Watchers
    event_handler_clippings = ClippingWatcher(llm_client, rag_manager)
    event_handler_prompts = PromptWatcher(llm_client, rag_manager)
    event_handler_vault = VaultWatcher(rag_manager)
    
    # 4. Schedule Watchdogs
    observer = watchdog.observers.Observer()
    observer.schedule(event_handler_clippings, str(CLIPPINGS_DIR), recursive=False)
    observer.schedule(event_handler_prompts, str(TO_LLM_DIR), recursive=False)
    observer.schedule(event_handler_vault, str(WIKI_VAULT_DIR), recursive=True)
    observer.schedule(event_handler_vault, str(PAGES_DIR), recursive=True)
    observer.schedule(event_handler_vault, str(NOTES_DIR), recursive=True)
    observer.schedule(event_handler_vault, str(SCRIPTURE_DIR), recursive=True)
    observer.start()
    
    # 5. Start Background Schedulers
    scheduler = InsightScheduler(PROJECT_ROOT, llm_client, rag_manager)
    scheduler.start()
    
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
