# Refactor Roadmap 2026-07 — 強韌・彈性・乾淨・效率

> **STATUS：P0–P4 全部完成（2026-07-02，17 個 commit，`c83fc15..fc5b4a5`）。**
> 成果：parser 2140→54(facade)、llm_client 2100→1462、prompt_watcher 567→248、
> ingestion 1296→1038、rag_manager 1618→1080、insight_agent 1902→210；
> golden bench 前後皆 1.000；mypy 豁免清單 43→36（只減不增契約持續有效）；
> 測試 1191→1321。尚餘：7 個既存過期測試（獨立 task）、tension_agent._render 死碼裁決、
> （選配）insight mixin→協作者、IngestResult 型別化。
>
> 基於 2026-07-02 的四份全面架構掃描（core 層／services 層／agents 層／watchers・tests・工具鏈）。
> 專案規模：System_Engine 共 191 個 .py、約 48.5k 行；262 commits（2026-04 起）。

## 0. 現況診斷

### 已經健康的部分（不要動）

- **分層乾淨**：core → services → agents/watchers/maintenance，無反向依賴、無循環 import。services 層零上行 import。
- **並發模型**：`global_busy_state` + idle callback + worker thread 的設計是穩的，鎖保護完整。
- **BaseAgent 共用邏輯**：`_load_prompt()`（mtime cache）與 `_write_report()`（自我修正＋trace 記錄）已集中，13 個 agent 共用。
- **TraceStore / CapabilityManager / PipelineRunner + AdapterRegistry**：設計乾淨，adapter 約束（不得直呼 production 私有方法）有被遵守。
- **測試基礎**：~84 個測試檔，多為 mock 良好的單元測試，有 `--run-live-llm` 開關。
- **DynamicSettings 熱重載**（Scripture 驅動）是刻意設計，保留 singleton；問題不在它本身，而在存取紀律（見 P3）。

### 核心病灶（依嚴重度）

| # | 問題 | 證據 |
|---|------|------|
| 1 | **六個 god module** | `core/parser.py` 2140 行（5 種職責）；`services/llm_client.py` 2046 行（7 種職責）；`agents/insight_agent.py` 1902 行（11 種職責）；`services/rag_manager.py` 1618 行（檢索 376 行單一函式）；`services/ingestion_pipeline.py` 1296 行；`watchers/prompt_watcher.py` 567 行（watcher 兼 command dispatcher） |
| 2 | **橫切邏輯重複** | retry+backoff 至少 3 份（llm_client ×2、research_pipeline）；LLM JSON 解析／salvage 至少 4 份；frontmatter 讀改寫三連招在 ingestion 內重複 3 處；HTTP throttle+UA 手工散落 |
| 3 | **依賴取得不一致** | llm/rag 由 registry 注入（好），但 PlannerService、AdapterRegistry、TextSplitter 等在 agent 方法內臨時 new；`rag_manager.translator` 靠 main.py 手動事後掛線 |
| 4 | **無工程品質關卡** | 無 pyproject.toml、無 ruff/mypy/pre-commit、三個 requirements 檔、測試靠 sys.path hack |
| 5 | **隱式 schema 與外漏耦合** | chunk metadata ~15 個 key 無 dataclass；ChromaDB where-clause 語法直接散在 rag_manager；`@retry_on_db_lock` 對呼叫端不可見 |
| 6 | **死碼與衛生** | registry 死別名（`count`/`counter`/`tag_patrol`）；InsightScheduler 14 行相容 shim；RecallAgent 44 行硬編碼 prompt；daemon.pid 被 git 追蹤 |

---

## 總體策略

1. **絞殺榕（strangler-fig）拆分**：拆大檔時，舊模組保留為 thin facade（re-export），所有既有 import 不破；全部遷完才刪 facade。
2. **每一步行為不變（behavior-preserving）**：每個 PR 過完整測試 + golden retrieval bench（`scratch/retrieval_bench.yml`，現值 1.000，是 rag 重構的回歸閘門）。
3. **小 PR、一次一個模組**，先做安全網（P0），再殺重複（P1），再拆大檔（P2），最後統一注入（P3）。
4. **不引入 DI framework**：維持 main.py 手動 composition root，符合專案規模。
5. **YAGNI**：不做完整 VectorStore 抽象、不換向量庫、不動排程器設計。
6. **運行紀律**：涉及 ChromaDB 的整合驗證前先停 daemon（單一寫者鐵律）；重構後重啟 daemon 驗證。

---

## Phase 0 — 工具鏈與地基（½–1 天，先做，讓後面每步都有保險）

1. **`pyproject.toml`**：宣告 package（`System_Engine` 可安裝、拿掉測試 sys.path hack）；deps 收斂為 base + `[tui]` + `[reranker]` optional extras，取代三個 requirements 檔（保留舊檔一版過渡）。
2. **ruff**（lint + format）+ **mypy**（漸進式：先 `--ignore-missing-imports`，新碼強制、舊碼豁免清單）+ **pre-commit**。
3. **pytest 標記**：`slow` / `live_llm` marker 正式化，CI/日常跑 `-m "not slow"`。
4. **衛生**：`git rm --cached System_Engine/daemon.pid`；刪 InsightScheduler shim；刪 registry 死別名或補齊 dispatch；RecallAgent 的 `_SYSTEM_PROMPT` 移到 Prompts/*.md。
5. **Makefile**：`make lint / typecheck / test / test-fast`。

## Phase 1 — 抽共用工具，消滅橫切重複（1–2 天，機械性、低風險）

| 新模組 | 內容 | 取代 |
|--------|------|------|
| `core/retrying.py` | `@retry_with_backoff(transient_check, retries, jitter)` | `llm_client._complete_provider_text_with_retry`、`_assess_falsifiability_once`、`research_pipeline._get_with_retry`、counter_agent 手寫 2-attempt 迴圈 |
| `core/json_extract.py` | 集中 `extract_json_object/array` + salvage + re-roll 策略（`extract_with_retry(text, kind, reroll_fn)`） | parser.py 尾段 90 行 + llm_client `_complete_json`/`_parse_json_array` + 各 agent 裸呼叫 |
| `core/markdown_doc.py` | `MarkdownDocument.load()/update_meta()/save()`（封裝 parse→strip→dump 三連招） | ingestion_pipeline 3 處、vault_utils、各 agent 寫檔 |
| `services/http_client.py` | `PoliteHttpClient.get(url, source)`：內建 per-source throttle、UA、retry | research_pipeline `_throttle` + `_SOURCE_MIN_INTERVAL` + `_get_with_retry` |
| `BaseAgent._error_report()` | 上收 3 份完全相同的複製貼上（counter/planner/executor） | — |

每項附單元測試；舊呼叫點逐一切換，不留雙軌。

## Phase 2 — 拆解 god modules（每個 1–3 天，獨立 PR，依風險由低到高）

### 2a. `core/parser.py`（最先：純函式、測試最厚、零 I/O）
拆成 `core/parsing/` package：
- `markdown_metadata.py`（frontmatter + tags，~70 行）
- `mermaid_repair.py`（~1450 行修復引擎，自成一包）
- `latex_repair.py`（~120 行）
- `markdown_quality.py`（表格／粗體／frontmatter 剝離 + `run_markdown_quality_checks` 編排器）
- JSON 抽取已在 P1 移走
`parser.py` 留 facade re-export。既有 `test_mermaid.py`（832 行）、`test_parser.py` 直接守住行為。

### 2b. `services/llm_client.py`
- `llm/transport.py`：provider dispatch（vllm/gemini/ollama）+ retry（用 P1 decorator）+ usage 計數 → `LLMTransport`
- `llm/prompt_composer.py`：persona × operation × template 三軸組裝 + 語系後綴載入
- `llm/response_parsing.py`：hybrid YAML+MD 解析、frontmatter 剝離（大部分併入 P1 的 json_extract）
- `llm/task_prompts.py`：三個版本鎖定 prompt dict 收成一個帶版本標記的 registry
- **研究／出版方法**（`generate_patent_table`、`generate_elite_digest`、`generate_research_keywords`）搬進 `research_pipeline`（domain 知識歸位，llm_client 不該懂專利表格）
- `LLMClient` 留 facade：對 agents 的公開 API 完全不變。

### 2c. `watchers/prompt_watcher.py` → 抽 `services/command_dispatcher.py`
- `INTENT_ROUTES`（25+ 命令）、正則解析、context 組裝、brain-op／KB／research 特例，全部移入 `CommandDispatcher`
- watcher 只剩：watchdog 事件 → 佇列 → worker → `dispatcher.dispatch(text)`
- 現有 8 條分岔路徑收斂為宣告式路由表（entry: intent → handler kind），`repair_tags`、`research` 一併入表
- 加上 INTENT_ROUTES 的參數化測試（25 個分支目前僅少數被覆蓋）。

### 2d. `services/ingestion_pipeline.py`
- 長文流程改為顯式 stage：`split → distill_parts → stitch → synthesize(+critique loop) → index_facets`，每個 stage 是可單測的物件／函式，state（master_tags、pending_concepts）收進一個 `IngestContext` dataclass（pending_concepts 由字串串接改結構化列表）
- critique 迴圈獨立成 `SynthesisCritiqueLoop`，verdict 解析失敗（None）與「真不確定」分開處理
- frontmatter 操作全面改用 P1 的 `MarkdownDocument`
- 失敗不再回 `None`：定義 `IngestResult(ok, stage, error_kind, detail)`。

### 2e. `services/rag_manager.py`（最後動，風險最高，靠 bench 守門）
- `rag/embedding_backend.py`：provider 抽象 + cache 包裝（現有 CachedEmbeddingFunction 併入）
- `rag/retrieval.py`：把 376 行的 `query_notes` 拆成可組合 stage：`vector → cross_lingual variants → bm25 → rrf_fuse → facet_deref → rerank → mmr → per_doc_cap`，每 stage 可單獨開關與單測
- `rag/chunk_meta.py`：`ChunkMetadata` dataclass，終結隱式 schema
- `rag/chroma_store.py`：where-clause 組裝與 collection 校驗集中一處（不做完整 VectorStore 抽象，只把 Chroma 語法圍起來）
- facet 解參照被丟棄的 orphan 補 log
- **回歸閘門**：每一步跑 golden bench 必須維持 1.000；整合驗證前停 daemon（單一寫者）。

### 2f. `agents/insight_agent.py`
依既有縫隙拆成協作者（agent 本體只剩路由 + 編排，~200 行）：
`StrategyLoader` / `InsightPlanner`（~500 行 planner+execute）/ `DocumentRetrieval` / `PairingEngine` / `MonteCarloEngine`（~350 行）/ `ContextAssembly` / `ReportOutput`。
放在 `agents/insight/` 子套件；`test_insight_agent.py`（660 行）先行為鎖定再搬。

## Phase 3 — 依賴注入一致化（2–3 天）

1. **composition root 正式化**：main.py 建一個輕量 `Services` dataclass（llm、rag、trace_store、capability_manager、adapter_registry、planner_service、profile_manager…）一次建好；`rag.translator = llm.translate_query` 這種事後掛線改為建構參數。
2. **AgentRegistry 注入 `Services`**：agents 統一建構子 `__init__(self, services)`（或維持 `(llm, rag)` 但把 PlannerService／AdapterRegistry／splitter 改由參數傳入），**禁止在方法內 new service**。LinterAgent／InsightAgent 的 rag 必填問題順帶統一。
3. **settings 存取紀律**：保留 `settings` singleton（熱重載是特性），但立規則——動態值只能在**呼叫當下**讀 `settings.X`，禁止模組載入時快照；靜態常數維持 config 頂層。用 ruff 自訂規則或 code review checklist 執法。
4. **MaintenanceScheduler 的 21 個 inline lambda** 抽成具名函式／宣告式 task registry（依賴看得見、可單測）。

## Phase 4 — 強韌性補強（持續性，可穿插）

- **超時保護**：busy-state idle callback 目前無 timeout，掛掉的 callback 會卡死 daemon → 加 per-callback 逾時記錄（先記 log，不強殺執行緒）。
- **TraceStore**：orphan run 目前只在開機收割 → 掛進 MaintenanceScheduler 週期任務；分析查詢 API 統一。
- **補測試缺口**：main.py 啟動整合測試（mock watcher，驗 observer/scheduler/pump 掛載順序）、`core/state.py` 狀態機獨立測試、`agents/registry.py`。
- **FPO HTML 解析**：結構變更時 silently 回 `[]` → 解析到 0 列且 HTTP 200 時發 warning。
- **錯誤分類**：延續 2d 的 Result 模式推廣到 research_pipeline 等。

---

## 執行順序與里程碑

```
P0 工具鏈 ──► P1 共用工具 ──► P2a parser ──► P2b llm_client ──► P2c dispatcher
                                                    │
P4（穿插）◄── P3 注入一致化 ◄── P2f insight ◄── P2e rag ◄── P2d ingestion
```

估計總量：集中做約 3–4 週；每個框都是獨立可合併、可回滾的單位。

## 驗收定義（每個 PR）

- [ ] 全測試綠（`pytest -m "not slow"` + 受影響模組的完整測試）
- [ ] golden retrieval bench 維持 1.000（凡動到 rag/embedding/splitter）
- [ ] ruff + mypy 通過（P0 之後）
- [ ] daemon 實際重啟並跑一輪 ingest + 一個 agent 指令冒煙
- [ ] 舊 import 路徑不破（facade 期間）

## 明確不做（防範圍蔓延）

- 不換向量庫、不做完整 VectorStore 介面
- 不引入 DI framework／async 改寫
- 不動 Scripture 熱重載設計、busy-state 併發模型、scheduler 輪詢架構
- 不重寫 mermaid 修復引擎內部邏輯（只搬家）
