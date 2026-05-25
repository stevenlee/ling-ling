# 🔍 Ling-Ling Code Review Report (Before Phase 5)

This report audits the implementations of **Phases 1 through 4** in the `ling-ling` codebase. The architecture is clean, highly defensive, and passes the entire 424-test suite successfully. This review highlights key design patterns and suggests minor optimizations to smooth the path toward **Phase 5 (High-Level Cognitive Planning)**.

---

## 📋 Architectural Overview

```mermaid
graph TD
    subgraph Watcher / Ingest (Sensory)
        A[PromptWatcher / Clipping] -->|1. Starts Run| B[TraceStore Context]
    end

    subgraph LLM Client & Capability (Cognitive)
        B -->|2. Resolves Capability| C[CapabilityManager]
        C -->|3. Strips Frontmatter| D[LLMClient]
        D -->|4. Records Trace| B
    end

    subgraph RAG & Database (Memory)
        E[RAGManager] -->|5. Logs Hybrid Decisions| B
        B -->|6. WAL Write| F[(llm_trace.sqlite)]
    end

    subgraph Output Generation (Action)
        G[CounterAgent] -->|7. Resolves Original Range| H[Dual-Link Generator]
        H -->|8. Renders report.md| I[Obsidian / VS Code]
    end
```

---

## 🛠️ Detailed Component Audits

### 1. SQLite Trace Store (`trace_store.py`)
- **Design Pattern**: Uses Python's `contextvars` (`_CURRENT_RUN_ID` and `_CURRENT_TRACE_IDS`) to manage thread-local scopes.
- **Audit Findings**:
  - **Thread-Safety**: Excellent. `ContextVar` is superior to `threading.local` as it provides seamless concurrency-safety across synchronous threads and asynchronous coroutines.
  - **WAL Mode**: Enabling WAL (`PRAGMA journal_mode=WAL`) is a critical choice that allows concurrent reads during trace logging, preventing DB blockings.
  - **Pruning Overhead (Potential Delay)**:
    - *Current behavior*: `prune_old()` is invoked synchronously inside the `finally` block of the `run` context manager.
    - *Risk*: Every user command/ingestion run ends with a synchronous SQL delete. While cheap, it introduces unnecessary latency to user-facing cycles.
    - *Refactoring Recommendation*: Move SQLite pruning into [maintenance_scheduler.py](file:///Users/stevenlee/projects/ling-ling/System_Engine/watchers/maintenance_scheduler.py) as a daily background task (e.g. `TracePruneTask`), keeping the CLI response paths purely append-only and fast.

---

### 2. Capability Layer & System Prompt Resolution (`capability_manager.py` & `llm_client.py`)
- **Design Pattern**: Decouples capability metadata registration from the LLM prompt context.
- **Audit Findings**:
  - **Prompt Leakage Prevention**: [llm_client.py:_load_capability_body](file:///Users/stevenlee/projects/ling-ling/System_Engine/services/llm_client.py#L337-L348) successfully strips YAML frontmatter from Operations/Skills markdown files before prompt compilation. This blocks metadata from leaking to the model context.
  - **Resolution Logging**: Logs the resolution trace to `metadata_json` on SQLite without polluting the actual prompt payload, keeping tokens small and cheap.
  - **Validation Defensiveness**: `_parse_capability_file` is fully wrapped in `try-except`, preventing a single broken file/syntax error in Obsidian from crashing the startup of the system.
  - *Frontmatter Strip Edge-case*: As noted in the pre-check, if a hand-written operation/skill lacks YAML frontmatter but starts with a markdown divider `---` (e.g. for aesthetic headers), `strip_body_frontmatter` will strip until the next `---`, causing instruction loss. This is acceptable as long as authors adhere to standard frontmatter formats.

---

### 3. Homeostasis and Benchmarking (`maintenance_scheduler.py` & `retrieval_bench.py`)
- **Design Pattern**: Implements an autonomous background scheduling daemon with idle state awareness.
- **Audit Findings**:
  - **Concurrency Control**: Well isolated. Uses `global_busy_state` to prevent scheduled tasks from interrupting user-triggered runs.
  - **Markdown Logging**: The retrieval bench appends tabular logs to `maintenance.log.md` with expectation vs. result comparisons, providing a perfect sensory history for evaluating RAG drift.
  - **Test Isolation**: The tests mock the RAG layer comprehensively, ensuring that testing does not pollute the vector store database.

---

### 4. Lens Links & Physical Grounding (`counter_agent.py`)
- **Design Pattern**: Dual-link rendering for target evidence.
- **Audit Findings**:
  - **CJK & Spaces Path Safety**: [counter_agent.py:_file_url_with_range](file:///Users/stevenlee/projects/ling-ling/System_Engine/agents/counter_agent.py#L692-L717) uses `Path.resolve().as_uri()` which ensures CJK characters (e.g., `妙法蓮華經`) and folder spaces are properly percent-encoded, resolving markdown parser breaking issues.
  - **Dual-Link Flow**: Correctly renders native Obsidian links (for internal obsidian navigation) alongside absolute `file:///` URLs (for external editor jumps with line-number highlights).

---

## 📈 Preparation for Phase 5 (Planning & PipelineRunner)

As we transition to Phase 5, the code base is in an optimal state. The capability layer is fully structural, meaning the incoming **Planner Agent** can query `CapabilityManager` dynamically at runtime to discover:
- What skills/operations exist.
- What inputs they require.
- What outputs they guarantee to produce.

### Recommended Minor Cleanup Actions
1. **Background Pruning**: Refactor the trace pruning out of CLI cycles and register it as a task under `MaintenanceScheduler`.
2. **Registry Documentation**: Document the YAML schema keys (`expected_inputs`, `produces`) in `Scripture.md` guidelines so creators keep their skills metadata aligned.
