# Refactoring LLM Wiki Automation Architecture

The current `auto_ingest.py` has grown into a monolithic script (+750 lines) that handles everything from file watching and LLM parsing to image processing and RAG interactions. This refactoring will decouple these responsibilities into a structured, scalable module layout.

## User Review Required

> [!IMPORTANT]
> The current daemon logic relies heavily on shared global states (e.g., `global_busy_state`) and hardcoded project roots. We will abstract these into a shared configuration or state module. I strongly recommend creating a `core/config.py` in addition to your requested modules to house environment variables and paths cleanly.

> [!WARNING]
> Please stop the daemon (`./start.sh`) before we begin the execution phase, as we will be deleting `auto_ingest.py` and modifying the start script.

## Proposed Changes

We will restructure the `System_Engine` directory into the following layout:

---

### Phase 1: Configuration & State Management

Extract all global constants, environment variables, and the `global_busy_state` into central configuration files to avoid circular imports.

#### [NEW] [config.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/core/config.py)
- Move `LLM_PROVIDER`, `AGENT_ROLE`, `OUTPUT_LANGUAGE`, and logging setup here.
- Define absolute paths (`PROJECT_ROOT`, `WIKI_VAULT_DIR`, `CLIPPINGS_DIR`, etc.) here.

#### [NEW] [state.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/core/state.py)
- Move `global_busy_state` (the `SystemState` class) here so watchers and services can safely access and mutate the global lock.

---

### Phase 2: Core Services

Extract the LLM logic and Media processing logic into dedicated, reusable classes.

#### [NEW] [llm_client.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/services/llm_client.py)
- Move the `LLMWrapper` class here.
- Provide clean, decoupled access to Gemini and vLLM/Ollama providers.

#### [NEW] [media_processor.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/services/media_processor.py)
- Extract image handling logic (e.g., base64 encoding).
- This will act as a utility wrapper for future multimodal ingestions.

---

### Phase 3: File Watchers & Schedulers

Split the three `watchdog` handlers into a new `watchers/` package, ensuring they import the `llm_client` and `state` cleanly.

#### [NEW] [clipping_watcher.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/watchers/clipping_watcher.py)
- Move `ClippingHandler`.

#### [NEW] [prompt_watcher.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/watchers/prompt_watcher.py)
- Move `PromptHandler`.

#### [NEW] [vault_watcher.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/watchers/vault_watcher.py)
- Move `VaultHandler`.

#### [NEW] [insight_scheduler.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/watchers/insight_scheduler.py)
- Move `InsightScheduler` background thread class here.

---

### Phase 4: Main Application Entrypoint

Replace `auto_ingest.py` with a lightweight orchestrator.

#### [NEW] [main.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/main.py)
- Imports configurations and instantiates the `Observer`.
- Registers the various watchers to their respective paths.
- Starts the `InsightScheduler`.

#### [MODIFY] [start.sh](file:///Users/stevenlee/projects/llm_wiki/start.sh)
- Update the bottom execution command from `python3 System_Engine/auto_ingest.py` to `python3 System_Engine/main.py`.

#### [DELETE] [auto_ingest.py](file:///Users/stevenlee/projects/llm_wiki/System_Engine/auto_ingest.py)
- Remove the monolithic script after all functionality has been migrated and verified.

## Open Questions

1. Do you agree with adding the `core/config.py` and `core/state.py` files to resolve circular Python imports between Watchers and Services?
2. Are you ready for me to perform this transition? If so, please approve the plan so I can begin generating the modular files!

## Verification Plan

### Automated Tests
- I will execute `python3 -m py_compile` on all newly created files to ensure syntax correctness.

### Manual Verification
- We will briefly start `./start.sh` and drop a test file into `Clippings/` to verify that the watcher routes to the LLM Client successfully.
