# PipelineRunner Roadmap (Deferred from Phase 4)

This document captures the deferred PipelineRunner / Pipeline DSL design discussed during Phase 4 planning. The component is intentionally held back; CapabilityManager + Operation/Skill frontmatter + Lens dual-link ship first under Phase 4, and PipelineRunner returns once the registry is in place.

## Why Deferred

- **Nervous-system-first sequencing**: Trace → RAG explain → MaintenanceScheduler + bench → Capability registry → **PipelineRunner** → Planner. The runner is meaningless without a populated capability registry to invoke; Phase 4 supplies that prerequisite.
- **Several unresolved design questions** (DSL variable syntax, condition evaluation, trigger surface, trace integration) made early commitment risky. Holding the design draft here so the rationale survives.
- **No immediate consumer pressure**: the existing ingestion / insight pipelines are hardcoded but adequate. The runner only earns its keep when (a) more capabilities exist, or (b) Phase 5 planner needs a target to emit.

## MVP Sketch

### Storage Location

Pipelines live under `lings-desktop/Templates/Pipelines/`, consistent with Scripture / Personas / Operations / Skills being vault-side and user-editable.

Explicitly **not** `System_Engine/scratch/pipelines/` — `scratch/` is the experiment area, and putting a first-class abstraction there would mislabel its lifecycle.

### File Format

- YAML, one pipeline per file.
- Filename stem = canonical pipeline id (matches the capability convention from Phase 4).

### DSL Shape (draft)

```yaml
name: "Synthesize with self-critique"
description: "Run synthesis, then critique its output; abort if synthesis is empty."

steps:
  - name: synthesize_doc
    capability: synthesize
    inputs:
      part_digests: "${context.part_digests}"
      title:        "${context.title}"
    outputs:
      synthesis_text: result

  - name: critique_synth
    capability: critique
    when:
      var: synthesis_text
      op: nonempty
    inputs:
      candidate: "${synthesis_text}"
      sources:   "${context.part_digests_text}"
    outputs:
      critique_findings: result
```

### Variable Resolution

- `${context.X}`  → read from the pipeline run's initial context dict.
- `${X}`          → read from a previous step's named output.
- Implementation: hand-rolled mini-resolver (~10 lines). Supports dotted-path (`${context.a.b}`). No jinja2 — this DSL does not need filters, loops, or if-blocks.

### Condition Syntax

No string `eval()`. Use a structured form:

```yaml
when:
  var: synthesis_text
  op: nonempty       # nonempty | equals | not_equals | gt | lt | exists
  value: ...         # required by ops that need a comparand
```

Extending the op vocabulary as new needs surface is cheaper, safer, and easier to test than building a safe expression parser. The DSL is config, and config files in `Templates/` are user-editable — `eval()` is a code-injection surface we are not willing to open.

### Trigger Surface

Three entry points in priority order:

1. **Primary**: `@ling-pipeline <name>` command, registered in [PromptWatcher](../watchers/prompt_watcher.py)'s intent routing table.
2. **Secondary**: programmatic invocation from agents or `MaintenanceScheduler` tasks.
3. **Tertiary (Phase 5)**: planner-generated pipelines passed in-memory to the runner.

### LLMTrace Integration

The Phase 1 [trace_store.py](../services/trace_store.py) already supplies the spine; no schema migration is needed.

- Each pipeline invocation opens a `TraceStore.run()` context (gets a `run_id`).
- Every LLM call inside step bodies inherits that `run_id` automatically via the existing `_CURRENT_RUN_ID` ContextVar.
- Per-step outputs are recorded as `artifacts` rows with `artifact_type = "pipeline_step_output"`.
- The `runs.metadata_json` blob holds the pipeline name + step graph + final status per step.

This means `SELECT * FROM llm_calls WHERE run_id = ?` already answers "what LLM work happened inside this pipeline" the day the runner ships.

## Open Questions Before Implementation

1. **Failure policy**
   - Default proposal: abort on first failed step. Per-step `on_failure: abort | skip | retry(n)`. Pipeline-level `continue_on_error: false` default.

2. **Capability ↔ pipeline input alignment**
   - Capability spec declares `expected_inputs: [part_digests]`; pipeline step provides `inputs: {part_digests: ...}`. Before invoking a step, call `CapabilityManager.validate_inputs(name, provided_keys)` so typos surface before LLM cost.

3. **Hot-reload vs at-start load**
   - Operations / Skills frontmatter is read via `_file_cache` (sticky for the daemon's life). Pipelines benefit more from hot-reload during authoring. Decision pending; lean towards `watchdog`-based invalidation scoped to `Templates/Pipelines/`.

4. **Step-level cost aggregation**
   - Aggregate from constituent capabilities' `cost_class` rather than re-declaring at pipeline level. Display in a future `@ling-pipeline list`.

5. **Localization**
   - Pipelines themselves are usually language-agnostic. The capability invocations under them inherit the localized prompts via the existing `_load_localized_content` chain — no separate l10n needed for pipelines.

## Dependencies

- [x] Phase 1: LLMTrace (`trace_store.py`)
- [x] Phase 2: RAG explain
- [x] Phase 3: MaintenanceScheduler + retrieval bench
- [ ] Phase 4: CapabilityManager + Operation/Skill frontmatter — **prerequisite, in flight**

No further dependencies. Phase 4 unblocks the runner.

## Suggested Phasing After Phase 4 Lands

| Phase | Scope | Shippable? |
|---|---|---|
| 4.5 | Runner skeleton: parse YAML → invoke capabilities sequentially → no conditions, no variables | Yes |
| 4.6 | `${...}` variable resolver + structured `when:` conditions | Yes |
| 4.7 | `@ling-pipeline` intent in PromptWatcher + `list` / `run` subcommands | Yes |
| 5 | Planner agent emits pipelines into the runner | Yes |

Each step is independently useful, so we don't have to commit to the whole arc up front.

## Non-Goals

- Loops / iteration inside the DSL. If a pipeline needs to fan out over a list, the calling agent does that work and invokes the runner per item.
- Parallel step execution. Initial runner is strictly sequential. Parallelism is a Phase-5+ concern.
- Inline prompt text in the DSL. Prompts always come from `Operations/*.md` or `Skills/*.md`; the DSL only orchestrates, never authors.
- Cross-pipeline dependencies. A pipeline is self-contained; chaining is done by a calling agent.
