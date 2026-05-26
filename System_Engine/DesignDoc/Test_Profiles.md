# Test Profiles

## Goal

Keep coverage strong while making day-to-day testing less exhausting.

The full suite remains the release gate, but normal development should run the smallest profile that covers the risk surface of the change. This keeps feedback fast without quietly dropping important regression coverage.

## Profiles

### 1. Smoke

Use after tiny doc/config-safe edits or before handing off quickly.

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python -m pytest -q \
  System_Engine/tests/test_parser.py \
  System_Engine/tests/test_llm_client.py
```

### 2. Planner / Executor

Use when touching planner mode, pipeline execution, adapters, readiness, or prompt watcher routing.

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python -m pytest -q \
  System_Engine/tests/test_prompt_watcher.py \
  System_Engine/tests/test_planner_service.py \
  System_Engine/tests/test_plan_readiness.py \
  System_Engine/tests/test_pipeline_runner.py \
  System_Engine/tests/test_planner_agent.py \
  System_Engine/tests/test_executor_agent.py \
  System_Engine/tests/test_insight_agent.py
```

### 3. Ingestion / Markdown

Use when touching parser, markdown quality, ingestion, splitter, or generated vault pages.

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python -m pytest -q \
  System_Engine/tests/test_parser.py \
  System_Engine/tests/test_mermaid.py \
  System_Engine/tests/test_ingestion_pipeline.py \
  System_Engine/tests/test_text_splitter.py \
  System_Engine/tests/test_text_splitter_fences.py \
  System_Engine/tests/test_md_block_scanner.py \
  System_Engine/tests/test_thoughtful_splitter.py
```

### 4. RAG / Maintenance

Use when touching retrieval, retrieval drift, maintenance scheduler, trace store, or RAG explain behavior.

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python -m pytest -q \
  System_Engine/tests/test_rag_manager.py \
  System_Engine/tests/test_retrieval_bench.py \
  System_Engine/tests/test_maintenance_scheduler.py \
  System_Engine/tests/test_trace_store_parent.py
```

### 5. Full Release Gate

Use before calling a phase complete, before tagging a release, or after broad refactors.

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python -m pytest -q System_Engine/tests
```

## Coverage Rule

Coverage is preserved by policy, not by running every test after every keystroke:

- Every behavior change gets a targeted regression test in the profile nearest the changed code.
- Every bug fix gets a test that fails before the fix.
- Any change touching two or more subsystems runs all affected profiles.
- Any release or phase-complete claim runs the full suite.

## Simplification Rule

Prefer fewer, behavior-level tests over many implementation-detail tests.

Good tests ask:

- Did the command route correctly?
- Did the plan pass or fail readiness for the right reason?
- Did the adapter receive the expected inputs?
- Did the generated report expose the source/risk metadata?

Avoid tests that only assert private helper call order unless that order is the actual contract.

## Recommended Phase Defaults

| Change type | Default profile |
| --- | --- |
| Documentation only | Smoke, or no pytest if no executable behavior changed |
| Parser / markdown repair | Ingestion / Markdown |
| Planner prompt / canonical plan | Planner / Executor |
| Built-in adapter | Planner / Executor plus the adapter-specific test file |
| Retrieval bench / scheduler | RAG / Maintenance |
| Cross-agent refactor | Affected profiles plus Full Release Gate |
| Version release | Full Release Gate |
