# Roadmap: Phase 4.5 onwards

Where we are after the Phase 1–4 nervous-system sweep, what's deferred,
and what shape Phase 5 (Planner) takes once PipelineRunner lands.

## Completed snapshot

```
Phase 1  ✅ LLMTrace                       (sensory  / proprioception)
Phase 2  ✅ RAG explain                    (acuity)
Phase 3  ✅ MaintenanceScheduler + bench   (autonomic reflex)
mid     ✅ Synthesis critique-as-postcheck (f54a6ec)
Phase 4  ✅ Capability layer + Lens dual-link (structural map)
Phase 5  ⏳ Planner                        (motor cortex)
```

Lower layers are prerequisites for the layer that consumes them. See
[evolution_phasing](../../../.claude/projects/-Users-stevenlee-projects-ling-ling/memory/evolution_phasing.md)
memory for the design rationale.

## Bucket A — Cleanup chores

Independent of the main arc; can land anytime.

| # | Item | Size | Trigger |
|---|---|---|---|
| A1 | ~~Rename `Skills/montecario.md` → `montecarlo.md`~~ ✅ | S | Done — typo warning removed |
| A2 | ~~Fix `Templates/translation-rpt.md` YAML-example leak~~ ✅ | S | Done — fenced YAML examples + explicit "do not reproduce in body" across all 12 templates |
| A3 | Upgrade `quality_fixes` from type-list to `{type, before, after, line}` | S–M | Better trace readability |
| A4 | Verify Scripture hot-reload actually exists (README claims it; audit didn't find it) | S | Reconcile docs vs code |

## Bucket B — PipelineRunner (Phase 4.5 / 4.6 / 4.7)

Hard prerequisite for Phase 5. See
[PipelineRunner_roadmap](PipelineRunner_roadmap.md) for the DSL design.

### Architectural constraint: capability + adapter (no private-method spelunking)

PipelineRunner does NOT call private methods or infer where a capability
lives. Each step declares two bindings:

- `capability:` — the registered Operation/Skill (CapabilityManager registry)
- `adapter:` — the named callable that actually executes it

The adapter layer is the **only** place capability metadata maps to
executable Python. No sneaky calls into `IngestionPipeline._write_synthesis`
or other production private flows. This keeps the demo pipeline an
experimental rig, not vascular surgery on the ingestion main line.

### Sub-phases with mandatory usage-test checkpoints

Each sub-phase is independently shippable, but **a real-usage checkpoint
must pass before the next sub-phase starts**. We are laying rails segment
by segment, pressure-testing each, not pushing a train across an
unfinished bridge.

| Sub-phase | Scope | Required checkpoint before next |
|---|---|---|
| **4.5** Dry-run runner | Parse pipeline YAML; validate capabilities exist in registry; resolve `${context.X}` / `${steps.X.output}` variables; evaluate structured `when:` conditions; invoke fixture adapters; write per-step telemetry to LLMTrace under a `pipeline_run_id`. **No real adapter, no production command.** Demo: `synthesize → critique` fed by fixture text. | Run retrieval bench, LingLens, and one long-doc synthesis-critique. Confirm `llm_trace.sqlite` and report metadata are queryable end-to-end. |
| **4.6** Real adapters | Register a small adapter set (`llm.answer_query`, `llm.critique_text`, `llm.generate_synthesis_from_text`). Pipelines invoke them through capability metadata. Controlled execution only — no autonomous planner. | Run `synthesize → critique` on real data via the runner. Inspect artifacts, trace propagation, and failure modes (capability missing, adapter raises, condition false). |
| **4.7** Plan schema | Define the JSON/markdown plan schema that Phase 5 planner will emit. No execution yet. Includes: how a plan references capabilities; how steps express dependencies; how preconditions are declared. | — (sets up Phase 5) |

### Demo pipeline (4.5)

```yaml
id: synthesize_critique_demo
description: PipelineRunner smoke test. Fixture input, fixture adapters.
steps:
  - id: synthesize
    capability: synthesize
    adapter: llm.generate_synthesis_from_text
  - id: critique
    capability: critique
    adapter: llm.critique_text
    condition: "steps.synthesize.output != ''"
```

In 4.5 both adapters are stubs returning canned text. The runner verifies
the *plumbing* — registry lookup, variable resolution, condition
evaluation, trace propagation, error paths — not the LLM behavior. The
same YAML is reused in 4.6 once adapters become real.

## Bucket C — Phase 5+ Planner

Three-stage rollout, each behind its own command. **No surprise upgrades
to existing high-traffic commands.**

| Phase | Trigger | Behavior |
|---|---|---|
| **5** | `@ling-plan` | Planner reads capability registry, trace history, maintenance state. Outputs "what I would do" as plan JSON + markdown report. **Does not execute.** Safe mode. |
| **5.5** | `@ling-do <plan_id>` | Executes a previously-produced plan after PipelineRunner gains rollback / failure / reporting. |
| **6** | `@ling-insight planner-mode` (feature flag) | Insight upgrades into a planner-driven entry point. Last, because `@ling-insight` is an existing high-frequency command; baking planner instability into it would degrade a known-good path. |

| # | Item | Size | Phase |
|---|---|---|---|
| C1 | `agents/planner_agent.py` — intent + registry + trace → plan spec | M | 5 |
| C2 | `Templates/Operations/plan.md` — planning prompt (third Operations citizen) | M | 5 |
| C3 | Plan formatter + report writer (renders JSON + human-readable markdown) | S | 5 |
| C4 | `@ling-plan` intent in PromptWatcher | S | 5 |
| C5 | Plan validation: missing capability / malformed JSON / circular deps | M | 5 |
| C6 | Rollback / failure handling + per-step reporting in PipelineRunner | M | 5.5 |
| C7 | `@ling-do <plan_id>` intent and approval flow | S | 5.5 |
| C8 | Insight planner-mode flag + opt-in path | M | 6 |

Architecture decision: **single-shot LLM planner, NOT a ReAct iterative
agent loop**. Ling-Ling's existing LLM calls are stateless; introducing
multi-step agent loops would break the architectural pattern. Upgrade
later if real workloads demand it.

## Bucket D — Parking lot (after Phase 6)

Not on the critical path. Worth noting so they aren't forgotten.

- **Critique retry loop**: regenerate when verdict < threshold (postcheck exists; retry doesn't).
- **Critique applied to Insight / Lens reports**, not just Synthesis.
- **New Operations**: Compare, Classify, Outline, Explain — CapabilityManager picks them up automatically.
- **Planner learning loop**: feed `quality_verdict` traces back to planner prompt for self-tuning. Phase 6+ territory.

## Execution order

```
1. A1 + A2                            → clean slate
2. Phase 4.5  (dry-run runner)        → fixture pipeline runs end-to-end
   ★ Checkpoint: retrieval bench / lens / synthesis-critique real-usage test
3. Phase 4.6  (real adapters)         → pipelines do real LLM work
   ★ Checkpoint: real-data synthesize → critique; verify artifacts + trace + failure modes
4. Phase 4.7  (plan schema)           → defines what Phase 5 planner will emit
5. Phase 5    (@ling-plan)            → planner produces plans, doesn't execute
6. Phase 5.5  (@ling-do)              → planner-driven execution with safety nets
7. Phase 6    (insight planner-mode)  → optional opt-in via feature flag
8. Pick from parking lot
```

A3 + A4 are orthogonal — slot them in opportunistically.

## Resolved decisions

1. **4.5 demo pipeline**: synthesize → critique, **fixture/dry-run only**. Must not touch `IngestionPipeline._write_synthesis` or any production private method. All capability invocations go through named adapters in a registry, not through string-matching against existing code paths.
2. **Phase 5 trigger**: `@ling-plan` first (plan only, no execution). `@ling-do` deferred to 5.5 (needs PipelineRunner rollback + reporting). `@ling-insight` planner-mode deferred to 6, behind a feature flag.
3. **Pacing**: Insert real-usage test between 4.5 and 4.6, and between 4.6 and 4.7. Do not push three runner sub-phases back-to-back. The bridge gets built one segment at a time.
