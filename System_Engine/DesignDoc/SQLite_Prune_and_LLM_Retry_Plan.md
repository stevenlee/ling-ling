# SQLite Pruning and LLM Retry Plan

## Goal

Reduce trace-store overhead and make LLM calls resilient to transient provider failures without polluting LLMTrace.

This plan has two tightly scoped changes:

- Move SQLite trace pruning out of every `TraceStore.run()` and into `MaintenanceScheduler`.
- Retry transient provider calls inside `LLMClient` while recording only one final trace row per logical LLM call.

## Non-Goals

- Do not change planner, executor, or adapter semantics.
- Do not change Mermaid parsing or tests in this phase.
- Do not retry non-transient errors such as invalid arguments, authentication failures, permission failures, or missing models.
- Do not write one failed trace row per retry attempt.

## Design Constraints

### Trace Rule

One logical `_complete_text()` call must write at most one `llm_calls` row:

- success after retries -> one `succeeded` trace row
- exhausted transient retries -> one `failed` trace row
- non-transient failure -> one `failed` trace row

Retry attempts should appear in `metadata_json`, not as separate failed trace records.

### Retry Boundary

Do not decorate all of `_complete_text()`.

Instead:

```text
_complete_text()
    -> _complete_provider_text_with_retry()
        -> _complete_provider_text_once()
    -> record final trace exactly once
```

This keeps trace writing, token accounting, and retry control separate.

## Proposed Changes

### 1. TraceStore Pruning

#### Modify `System_Engine/services/trace_store.py`

Remove synchronous pruning from the `finally:` block of `TraceStore.run()`:

```python
-            self.prune_old()
```

Keep `TraceStore.prune_old()` unchanged. It remains the single cleanup implementation and can be called by maintenance tasks.

#### Tests

Add/modify tests in `System_Engine/tests/test_trace_store_parent.py` or `test_maintenance_scheduler.py`:

- `TraceStore.run()` does not call `prune_old()` automatically.
- `prune_old()` remains callable directly.

### 2. Daily Maintenance Prune Task

#### Modify `System_Engine/watchers/maintenance_scheduler.py`

Inside `_default_tasks()`, add:

```python
def trace_prune() -> MaintenanceResult:
    trace_store = getattr(self.llm, "trace_store", None)
    if trace_store is None:
        return MaintenanceResult("skipped", "No trace store associated with LLM client.")
    trace_store.prune_old()
    return MaintenanceResult("succeeded", "SQLite trace logs pruned successfully.")
```

Register:

```python
MaintenanceTask(
    name="trace_prune_daily",
    action=trace_prune,
    daily=True,
    idle_required=False,
    intent="maintenance.trace_prune",
    agent="TraceStore",
)
```

Rationale for `idle_required=False`: pruning is local SQLite cleanup, does not invoke LLMs, and should not be starved by normal dreaming/insight activity.

#### Tests

Modify `System_Engine/tests/test_maintenance_scheduler.py`:

- Default tasks include `trace_prune_daily`.
- The task calls `llm.trace_store.prune_old()`.
- If no trace store exists, the task returns `skipped`.

### 3. Provider-Call Retry Layer

#### Modify `System_Engine/services/llm_client.py`

Add helpers:

```python
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404}

def _error_status_code(exc: Exception) -> int | None:
    ...

def _is_non_retryable_llm_error(exc: Exception) -> bool:
    ...

def _is_transient_llm_error(exc: Exception) -> bool:
    ...
```

Use duck typing rather than SDK-specific exception imports:

- Look for `status_code`, `status`, or `code`.
- Treat class names containing `RateLimit`, `Timeout`, `Connection`, `APIConnection`, `ServiceUnavailable` as transient.
- Treat class names containing `Authentication`, `Permission`, `BadRequest`, `InvalidArgument`, `NotFound` as non-retryable.
- Use lowercase message fallback for `timeout`, `temporarily unavailable`, `connection`, `rate limit`, `too many requests`.

Add:

```python
def _complete_provider_text_once(
    self,
    system_prompt: str,
    user_msg: Any,
    temperature: float,
    max_tokens: int,
) -> tuple[str, int | None, int | None, int | None]:
    ...
```

This method contains the current provider-specific Gemini/OpenAI-compatible dispatch.

Add:

```python
def _complete_provider_text_with_retry(
    self,
    system_prompt: str,
    user_msg: Any,
    temperature: float,
    max_tokens: int,
    *,
    retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
) -> tuple[str, int | None, int | None, int | None, dict]:
    ...
```

Behavior:

- Attempt count includes the initial call.
- Retry only transient errors.
- Fail immediately for non-retryable errors.
- Sleep between retries using exponential backoff plus small jitter.
- Return retry metadata:

```python
{
    "retry_attempts": attempts,
    "retry_transient": bool,
    "retry_last_error": "...",
}
```

Update `_complete_text()`:

- Call `_complete_provider_text_with_retry()`.
- Merge retry metadata into trace metadata.
- Record exactly one success or failure trace.

### 4. LLM Retry Tests

#### New `System_Engine/tests/test_llm_retry.py`

Use fake client objects; do not hit real providers.

Required tests:

1. Transient error then success:
   - fake provider raises 429/timeout once, then returns success
   - `_complete_text()` returns final text
   - provider called twice
   - only one successful trace row exists
   - trace metadata has `retry_attempts: 2`

2. Non-transient error:
   - fake provider raises authentication/bad request style error
   - no retry
   - only one failed trace row exists
   - metadata has `retry_attempts: 1`

3. Exhausted transient retries:
   - fake provider always raises 503/timeout
   - provider called `retries` times
   - only one failed trace row exists
   - metadata includes final error and transient marker

Implementation hint:

- Set retry delays to zero in tests or monkeypatch `time.sleep`.
- Use `TraceStore(tmp_path / "trace.sqlite")` to inspect `llm_calls`.

## Verification Commands

Focused profile:

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python -m pytest -q \
  System_Engine/tests/test_llm_retry.py \
  System_Engine/tests/test_maintenance_scheduler.py \
  System_Engine/tests/test_trace_store_parent.py
```

Full release gate:

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python -m pytest -q System_Engine/tests
```

## Acceptance Criteria

- `TraceStore.run()` no longer prunes synchronously.
- `MaintenanceScheduler` includes `trace_prune_daily`.
- Transient LLM provider errors retry and can recover.
- Non-transient provider errors fail immediately.
- Exhausted transient retries fail cleanly.
- LLMTrace has one row per logical `_complete_text()` call, not one row per attempt.
- Full suite passes.
