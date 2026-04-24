# Refactoring Plan

## 1. Remove PDF Functionality
- `auto_ingest.py`: Remove all references to `.pdf`, `magic-pdf`, `_handle_pdf()`, and `temp_pdf`.
- `auto_ingest.py`: Update `supported_extensions` to exclude `.pdf`.

## 2. Directory Structure Optimization
- Move `/Users/stevenlee/projects/llm_wiki/Database` to `/Users/stevenlee/projects/llm_wiki/lings-desktop/Database`.
- `auto_ingest.py`: Change `self.raw_clippings_dir` to point to `lings-desktop/Database/raw/clippings`.
- `auto_ingest.py`: Change `self.raw_prompts_dir` to point to `lings-desktop/Database/raw/prompts`.
- `rag_manager.py`: Change `self.db_dir` to point to `lings-desktop/Database/chroma_db`.

## 3. VaultHandler Debouncing (RAG updates)
- `auto_ingest.py` `VaultHandler`: Currently triggers RAG update in `on_modified` immediately after sleeping 1s.
- Requirement: Wait 1 minute after the *last* edit before triggering the RAG update.
- Implementation: Use a dedicated `Timer` thread for each modified file. When `on_modified` fires, cancel the existing timer for that file, and start a new 60-second timer. If the timer completes without being cancelled, update the document in RAG.

## 4. Fix "Patrol" Triggering Twice
- Issue: `watchdog` might emit multiple `on_created` events, or editing the file right after creating it could cause issues. However, `PromptHandler` only implements `on_created`. Alternatively, when moving/renaming the original command file, the file system might trigger something.
- Quick Fix: Implement a debouncing/deduplication mechanism in `PromptHandler.on_created` to ignore files that have recently been processed or are already scheduled. Similar to VaultHandler, we can use a small delay (2s) and cache recently processed paths.
- Another factor: "is_patrol" checks `"/patrol" in query_content`. Make sure it's not matched twice or logic doesn't loop.

## 5. Modularization & Cleanup
- Future step/current step: The handlers in `auto_ingest.py` are intertwined. I will ensure the debouncing introduces clean timeout managers instead of messy nested dicts if possible.
