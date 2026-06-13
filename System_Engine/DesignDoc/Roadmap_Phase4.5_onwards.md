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
| R3 | ~~**Lens tally 路徑補 retry**~~ ✅ 2026-06-13 — `_tally_instances` LLM 去重路徑補上與 extraction 同款的「無 object 重試一次、再 fallback 本地去重」防禦 + 守護測試 | S | 完成 |
| R4 | ~~**Reasoning-channel 強健性盤點**~~ ✅ 2026-06-13 — 新增 `LLMClient._complete_json` 統一「complete + parse + 重試一次、literal `[]`/`{}` 視為真零不 re-roll」防禦，盤點後把 6 個裸呼叫（generate_part_digest、find_topic_shifts、summarize_for_context、extract_claims、adjudicate_claims、generate_persona_and_template）改走它。**刻意保留 bespoke**：`score_text_quality`（`reason` 要分辨 transport error vs parse miss，兩者皆 test-pinned，但仍加了 parse-miss re-roll）、`_assess_falsifiability_once`（re-roll 條件更嚴，要 score 本身解析成功）。**仍有缺口**：`translate_tags` 走 provider dispatch 不經 `_complete_text`（已設 `response_format=json_object`，曝險較低），未納入。10 個 helper 單元測試 + 170 個既有受影響測試綠。 | M | 完成 |
| R5 | **LENS_QUOTE_MIN_GROUNDED_RATIO 調參**：0.8 是拍腦袋預設；翻譯文章的引文錨定率天然偏低 | S | 等 lens verdict 數據累積幾週後用實際分佈校準 |
| R6 | **檢索品質漂移**：bench 100%（5月底）→ 73%（6/11+）。**已診斷（2026-06-13），非 code 回歸**：純 hybrid（vector+BM25）檢索隨索引增長碰撞加劇，cross-encoder reranker 未裝也未開（`RERANKER_ENABLED=false`、`sentence-transformers` 未安裝）。失敗模式：同一母文件 chunk 洗版（Hardy Synthesis 6 個 chunk 佔 rank 2–8），把預期文件擠到 rank 9–10。**per-doc cap 不可行**——會打爛合法依賴同文件多 chunk 的查詢（Lax-Milgram 的 top-5 正是同一本書 5 個 chunk）。Trench 對 Taylor 查詢甚至不在 top-30（embedder recall 缺口，中文查詢更明顯：弱解→Hamlet）。**真正的修法是基建決策**：裝並開 cross-encoder reranker（`_get_reranker` 已能在缺套件時優雅降級），和／或換更強的 embedder（現為 nomic-embed-text）。 | M–L | **卡在依賴決策**：reranker 需 `pip install sentence-transformers` + 下載 BAAI/bge-reranker-v2-m3（~2GB），由 Steven 拍板 |

## R7 — 全模組稽核後續（2026-06-13，見 [SystemEngine_audit_20260613.md](SystemEngine_audit_20260613.md)）

Workflow 稽核（10 reader → dedup → 對抗式驗證 → 彙整）。99 raw → **41 confirmed**（全 10 子系統皆已驗證；第二輪 resume 補完前次因 session 額度中斷的 5 塊）。

**已處理（batch-A 資料完整性）**：B1 空陣列偵測、B2 lens RAG fallback、A2 同名 scoped 清理 → 已修；A1 (Synthesis) 命名 → 確認為慣例、文件化。

**剩餘批次**：

| 批次 | 內容 | 風險 |
|---|---|---|
| R7-B | **LLM fan-out 並行化（DEFERRED 2026-06-13，實測驅動）**：lens 逐 chunk(P1)、`digest_sources`(P2)、`_expand_seed`(P3) 確為串行 loop。但對著真實 backend 實測：單一 ollama **gemma-26B 完全串行化**——3 並發 wall-clock 7.2s vs 單發 2.6s（2.81× ≈ 全排隊），並行化只換到 **1.07×**。一個 26B 模型一張 GPU 一次只生一個請求。**收益只在 cloud（gemini）或 `OLLAMA_NUM_PARALLEL>1` + 足夠 GPU 才出現**。成本卻實在：ThreadPool worker 不繼承 trace ContextVar，要全程 `copy_context()` 傳 run_id，且 `_CURRENT_TRACE_IDS` 收集仍有缺口；外加 thread-safe ui、SQLite 並發、ordering/dedup、flag。**當下不值得**。**重啟條件**：切到 gemini，或 ollama 開 num_parallel 並驗證 backend 真能並行。 | 中 | Deferred（等 backend 能並行） |
| R7-C | ChromaDB/FS 收斂 + adapter 邊界：facet deref 批次、vault filename index、`format_digest_for_prompt` 公開化、insight 繞過 `rag.collection` | 低–中 |
| R7-D | 純清理：signals/pair-key helper 抽取、多語 falsifier、廉價 perf | 低 |
| R7-E | ~~**新確認的 correctness（5 塊補驗證後）**~~ ✅ 2026-06-13 — 修了 5 條獨立 bug：`profile_manager` 大小寫 key（get() 對混合大小寫 stem 全 miss）、`maintenance_scheduler:389` set_busy→try_set_busy（acquired-guard 防搶占 + 不釋放非自有 lock）、`trace_store` run() finally 包 try/except（不再遮蔽 body 原例外）、`parser:431` 空 label 節點保留原樣、`parser:56` frontmatter regex 允許無尾換行。+7 pinned tests，902 passed。 | 低–中 | 完成 |
| R7-F | ~~**新確認的 perf（maintenance/storage）**~~ ✅ 2026-06-13 — 逐項查證後，**唯一成立且值得做的是 trace_store ts 索引**：trace DB 無上限成長，所有時間窗查詢（analytics / memoir / prune）原本全表掃。新增 `(artifact_type,ts)`、`(stage,ts)` 複合 + 三表 plain `ts` 索引，EXPLAIN 確認 SCAN→SEARCH，CREATE IF NOT EXISTS 自動套用既有 DB。其餘審查項**查證後不做**：`load_all_pages` 多 pass（現況僅 ~9 頁 = 毫秒級，且 pass 會中途寫頁，cache 有 staleness 風險）→ 待 Cortex 成長到數百頁再議；`cortex_consolidation` N+1 embedding → **誤報**（embedding 已用 `claim_embeddings` 跨 run 快取，鄰居搜尋是記憶體內 O(n) cosine）；consolidation 每筆 insight 寫 state → **刻意的崩潰可復原性**（`state["processed"]`）；decay 雙讀小 JSON、`_CURRENT_TRACE_IDS` O(n²) tuple ref → 可忽略。+1 test，903 passed。 | — | 完成 |
| R7-G | **prompt_watcher dispatch-thread 阻塞**（從 R7-E 分出）：`_handle_event`→`_drain_queue`→`process_prompt` 把**整段 LLM 處理**（不只 `time.sleep(1)`）跑在 watchdog 單一 dispatch thread 上，阻塞後續所有檔案事件。**非小修**——需把處理移到專屬 worker thread，動到 watcher 並發模型（ordering、busy-state、`scan_existing` idle callback 互動），該獨立謹慎處理。 | M | 待排 |

## Resolved decisions

1. **4.5 demo pipeline**: synthesize → critique, **fixture/dry-run only**. Must not touch `IngestionPipeline._write_synthesis` or any production private method. All capability invocations go through named adapters in a registry, not through string-matching against existing code paths.
2. **Phase 5 trigger**: `@ling-plan` first (plan only, no execution). `@ling-do` deferred to 5.5 (needs PipelineRunner rollback + reporting). `@ling-insight` planner-mode deferred to 6, behind a feature flag.
3. **Pacing**: Insert real-usage test between 4.5 and 4.6, and between 4.6 and 4.7. Do not push three runner sub-phases back-to-back. The bridge gets built one segment at a time.
