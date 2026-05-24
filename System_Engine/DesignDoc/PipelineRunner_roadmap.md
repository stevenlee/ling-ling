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

### Capability + adapter binding (no private-method spelunking)

PipelineRunner does NOT call private methods or guess where a capability
lives. Each step declares two bindings:

- `capability:` — the registered Operation/Skill (CapabilityManager registry)
- `adapter:` — the named callable that actually fulfills it

Adapters are registered in a small in-process table (`runner.register_adapter(name, callable)`)
and looked up at execution time. This is the **only** place capability
metadata maps to executable Python — pipelines never touch
`IngestionPipeline._write_synthesis` or similar production private flows.

### DSL Shape (draft)

```yaml
id: synthesize_critique_demo
description: PipelineRunner smoke test. Fixture input, named adapters.

steps:
  - id: synthesize
    capability: synthesize
    adapter: llm.generate_synthesis_from_text
    inputs:
      part_digests: "${context.part_digests}"
      title:        "${context.title}"
    outputs:
      output: result

  - id: critique
    capability: critique
    adapter: llm.critique_text
    condition: "steps.synthesize.output != ''"
    inputs:
      candidate: "${steps.synthesize.output}"
      sources:   "${context.part_digests_text}"
    outputs:
      output: result
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

No direct user-facing `@ling-pipeline` command is planned. The user's
flow into the runner goes through the planner instead:

1. **Phase 4.5–4.6**: programmatic invocation only (fixtures, then real
   adapters). Runs are kicked off from tests / scratch scripts to verify
   semantics.
2. **Phase 5** (`@ling-plan`): planner produces a plan but does not
   execute it. User reviews.
3. **Phase 5.5** (`@ling-do <plan_id>`): user-approved plans are passed
   to the runner. Requires PipelineRunner rollback + reporting first.
4. **Phase 6**: `@ling-insight` planner-mode (feature-flagged opt-in) is
   the eventual fold-in path for the highest-frequency entry point.

See [Roadmap_Phase4.5_onwards](Roadmap_Phase4.5_onwards.md) for the full
phasing and the mandatory checkpoint tests between sub-phases.

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
| 4.5 | Dry-run runner: parse YAML, validate capabilities, resolve `${...}` variables, evaluate structured conditions, invoke **fixture adapters only**, write per-step trace under `pipeline_run_id`. | Yes |
| 4.6 | Register real adapters (`llm.answer_query`, `llm.critique_text`, `llm.generate_synthesis_from_text`). Pipelines do real LLM work. Controlled execution; no autonomous planner. | Yes |
| 4.7 | Define the plan schema that Phase 5 planner will emit. No execution. | Yes |
| 5   | `@ling-plan`: planner produces plan, does not execute. | Yes |
| 5.5 | `@ling-do <plan_id>`: planner-driven execution. Requires rollback / failure / reporting. | Yes |
| 6   | `@ling-insight planner-mode` opt-in. | Yes |

**Mandatory checkpoint between 4.5 and 4.6**: run retrieval bench, LingLens,
and one long-doc synthesis-critique; confirm trace DB + report metadata
are queryable.

**Mandatory checkpoint between 4.6 and 4.7**: run synthesize → critique
on real data via the runner; verify artifacts, trace propagation, and
failure modes.

Do not push 4.5 / 4.6 / 4.7 back-to-back. See
[Roadmap_Phase4.5_onwards](Roadmap_Phase4.5_onwards.md) for the rationale.

## Non-Goals

- Loops / iteration inside the DSL. If a pipeline needs to fan out over a list, the calling agent does that work and invokes the runner per item.
- Parallel step execution. Initial runner is strictly sequential. Parallelism is a Phase-5+ concern.
- Inline prompt text in the DSL. Prompts always come from `Operations/*.md` or `Skills/*.md`; the DSL only orchestrates, never authors.
- Cross-pipeline dependencies. A pipeline is self-contained; chaining is done by a calling agent.
