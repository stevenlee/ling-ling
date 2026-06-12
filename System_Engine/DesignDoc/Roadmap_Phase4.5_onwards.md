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
Phase 5B ✅ PlannerAgent plan-only         (motor cortex, plan-only)
Phase 5C ✅ @ling-do controlled execution  (motor cortex, execution)
```

Lower layers are prerequisites for the layer that consumes them. See
[evolution_phasing](../../../.claude/projects/-Users-stevenlee-projects-ling-ling/memory/evolution_phasing.md)
memory for the design rationale.

## Bucket A — Cleanup chores

Independent of the main arc; can land anytime.

| # | Item | Size | Trigger |
|---|---|---|---|
| A1 | ~~Rename `Skills/montecarlo.md` → `montecarlo.md`~~ ✅ | S | Done — typo warning removed |
| A2 | ~~Fix `Templates/translation-rpt.md` YAML-example leak~~ ✅ | S | Done — fenced YAML examples + explicit "do not reproduce in body" across all 12 templates |
| A3 | ~~Upgrade `quality_fixes` from type-list to `{type, before, after, line}`~~ ✅ | S–M | Done — each repair now emits structured records; snippets truncated to 80 chars |
| A4 | ~~Verify Scripture hot-reload actually exists~~ ✅ | S | Done — hot-reload IS wired (`config.DynamicSettings.reload()` + `vault_watcher`); the earlier audit was wrong. Added regression tests. |

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

- ~~**Critique retry loop**: regenerate when verdict < threshold~~ ✅ batch-2 — `SYNTHESIS_CRITIQUE_MAX_RETRIES`, adopt on strictly-better verdict only.
- ~~**Critique applied to Insight / Lens reports**~~ ✅ — Insight half superseded by Phase 2.5/3 groundedness + refute signals; Lens half landed as deterministic quote verification (`quality_verdict` keep/revise by grounded-quote ratio, batch-3).
- ~~**New Operations**: Compare, Classify, Outline, Explain~~ ✅ batch-2 — four templates, registry auto-pickup.
- **Planner learning loop**: feed `quality_verdict` traces back to planner prompt for self-tuning. Phase 6+ territory. Verdict data now accumulates from three report types (synthesis, lens, planner pipelines) — revisit once there is enough volume to mean something.
- **Falsifiability hypothesis check** (from CortexMemory_phase2_5_brief §6): validate "doc-anchored seeds produce more falsifiable claims" with data. Gen-mix landed 2026-06-12; ripe ~2026-06-26 with two weeks of nightly data.

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

## Remaining work（2026-06-12 batch-3 收斂後的全量清單）

主線 Phase 1–6 與停車場 D1–D3 全部落地（batch-1/2/3）。剩下：

| # | 項目 | 大小 | 條件 / 時點 |
|---|---|---|---|
| R1 | **Falsifiability 假設驗證**：doc-anchored seeds 是否產出更可反駁的主張（兩組分佈比較 + 黃線比例 + 0612 前 baseline） | S | ✅ 已排程 2026-06-26 09:00 一次性 scheduled task（純唯讀統計，報告進 scratch/） |
| R2 | **Planner learning loop**（停車場 D4）：把 quality_verdict traces 餵回 planner prompt 自我調校 | M | verdict 數據現在從三個來源累積（synthesis critique、lens quote verification、planner pipelines）；等量夠了再立案——建議 R1 跑完一併看數據量 |
| R3 | **Lens tally 路徑補 retry**：`_tally_instances` 的 LLM 去重路徑（>3 實例）沒有 extraction 那層「無 array 重試一次」防禦，同樣暴露在 gemma reasoning-channel 間歇失敗下 | S | 隨手可做 |
| R4 | **Reasoning-channel 強健性盤點**：全面盤點 `_complete_text` 的 JSON 端呼叫（extract_claims、adjudicate_claims、generate_part_digest、translate_tags…）哪些缺「解析失敗重試」，統一防禦模式 | M | batch-3 證實此失敗模式發生率不低（單日 live 三發中兩發）；建議下一批 |
| R5 | **LENS_QUOTE_MIN_GROUNDED_RATIO 調參**：0.8 是拍腦袋預設；翻譯文章的引文錨定率天然偏低 | S | 等 lens verdict 數據累積幾週後用實際分佈校準 |

## Resolved decisions

1. **4.5 demo pipeline**: synthesize → critique, **fixture/dry-run only**. Must not touch `IngestionPipeline._write_synthesis` or any production private method. All capability invocations go through named adapters in a registry, not through string-matching against existing code paths.
2. **Phase 5 trigger**: `@ling-plan` first (plan only, no execution). `@ling-do` deferred to 5.5 (needs PipelineRunner rollback + reporting). `@ling-insight` planner-mode deferred to 6, behind a feature flag.
3. **Pacing**: Insert real-usage test between 4.5 and 4.6, and between 4.6 and 4.7. Do not push three runner sub-phases back-to-back. The bridge gets built one segment at a time.
