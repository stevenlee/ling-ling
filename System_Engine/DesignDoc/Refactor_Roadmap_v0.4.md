# Refactor Roadmap v0.4

## Purpose

This roadmap captures broader cleanup work that should follow the 0.3.x source-quality improvements. It is intentionally not the 0.3.1 implementation plan.

The rule for v0.4 is simple: refactor around proven behavior, not ahead of it. Planner execution should first become reliable for long-source work in v0.3.1; then v0.4 can reduce complexity and improve maintainability.

## Scope

### In Scope

- Shared markdown and regex utilities.
- Gradual service injection for large agents.
- Logging cleanup with module-level loggers.
- Formatter/linter configuration for changed files.
- Targeted type checking on service modules.
- README architecture diagram and contributor notes.
- Test profile documentation and release gates.

### Out of Scope

- Changing the planner safety model.
- Moving executable adapter allow-listing into user-editable YAML.
- Full-repo `mypy --strict`.
- Full-repo formatting churn in one commit.
- Large agent rewrites without behavioral tests.

## Recommended Order

### R1: Tooling Without Churn

- Add `ruff` and `black` config.
- Run on changed files only at first.
- Document commands in README.
- Do not mass-format the repository.

### R2: Shared Markdown Utilities

Move small pure helpers into a shared module:

- wikilink/title cleaning
- markdown table escaping
- bold-spacing repair wrappers
- reusable regex constants where ownership is clear

Avoid centralizing regexes that are genuinely domain-specific to one agent.

### R3: Service Injection, Narrow First

Start with optional constructor injection where tests benefit most:

- `PlannerService`
- adapter registry factory
- readiness assessor

Do not force every agent into a framework. The goal is easier tests and smaller seams, not abstraction for its own sake.

### R4: Logging and Error Boundaries

- Replace ad-hoc `logging.error` with `logger = logging.getLogger(__name__)`.
- Use `logger.exception` where stack traces matter.
- Keep user-facing failure messages clear.
- Raise custom errors only at boundaries where callers can recover.

### R5: Targeted Types

Start type checking on stable service modules:

- `services/pipeline_runner.py`
- `services/plan_readiness.py`
- `services/planner_service.py`
- `services/builtin_adapters.py`

Avoid full-repo strict mode until agent interfaces settle.

## Deferred / Rejected Items

### Adapter Allow-List in YAML

Do not move execution allow-listing to user-editable YAML yet.

The current Python registry is an important safety boundary: a capability becomes executable only when code registers an adapter. YAML can describe capabilities, but it should not grant execution authority.

### Full `mypy --strict`

Too much low-signal churn for the current codebase. Use targeted checks first.

### 90 Percent Coverage Target

Prefer risk-based coverage over a global percentage target. The release gate is full regression suite plus targeted tests for changed behavior.

## Test Strategy

Use `System_Engine/DesignDoc/Test_Profiles.md`.

Refactor work should normally run:

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python -m pytest -q \
  System_Engine/tests/test_parser.py \
  System_Engine/tests/test_llm_client.py \
  System_Engine/tests/test_pipeline_runner.py \
  System_Engine/tests/test_plan_readiness.py
```

Any refactor touching agent orchestration must also include:

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python -m pytest -q \
  System_Engine/tests/test_insight_agent.py \
  System_Engine/tests/test_planner_agent.py \
  System_Engine/tests/test_executor_agent.py \
  System_Engine/tests/test_prompt_watcher.py
```

Full suite remains the release gate.

## Exit Criteria for v0.4

- No behavior regressions in the full suite.
- README explains planner/executor/source flow.
- New lint/type checks are documented and scoped.
- Refactors reduce duplicated helper code without widening execution authority.
