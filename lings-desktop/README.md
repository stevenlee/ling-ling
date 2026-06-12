# Ling-Ling Mentor / 玲玲小老師

Ling-Ling 是一個 file-based agent。你用 Obsidian 管理資料，Ling-Ling 監聽資料夾、讀寫 Markdown、呼叫本地或遠端 LLM，並把整理後的知識重新寫回 vault。

她的核心目標是把「閱讀、整理、查詢、洞察」變成可追蹤的檔案流程：人負責挑選材料與判斷，AI 負責清洗、拆解、交叉比對與產出初稿。

## 解決的問題

- **注意力不足**：長文可以先拆成 Parts，再產出忠實接合版與洞察型總結。
- **AI 難以精準駕馭**：角色、語言、創造力、記憶長度與搜尋深度集中在 `Scripture/Scripture.md`。
- **人機共筆麻煩**：把檔案丟進 `Consolidate/` 或 `toLingLing/`，daemon 會自動處理並歸檔。
- **知識管理困難**：本地 ChromaDB 與 Markdown vault 同步，支援 RAG 問答與語意搜尋。
- **保密與可控**：可接 Ollama、vLLM 或 Gemini；資料與 Obsidian vault 保留在本機專案內。
- **想像力素材不足**：`@ling-insight` 會從既有筆記生成跨領域洞察。

## 核心功能

- **Scripture-driven settings**：`Scripture/Scripture.md` 是可熱載入的行為設定，控制 persona、輸出語言、temperature、context window、RAG top-k 以及預設輸出模板 (`use_template`) 等參數。
- **Long-document ingestion pipeline**：長文進入 `Consolidate/` 後，`IngestionPipeline` 會切分文字、生成 Part notes、產生 structured digests、輸出 Stitched Article 與 Synthesis。
- **Source-grounded Parts**：每個 Part 會記錄原文 char/line range，Stitched Article 也會顯示 `Original range`，方便從分析結果回到原始段落。
- **Stitched Article**：`Title (Stitched).md` 保留各 Part 的主要正文，適合完整閱讀與校對。
- **Synthesis Note**：`Title (Synthesis).md` 使用 Part digests 進行總合成，並附上 Part Digest Appendix。
- **Insight mirror reports**：`@ling-insight` 會同時寫入 canonical report 到 `fromLingLing/`，並在 `Insights/` 放一份 byte-identical copy，保留完整 YAML frontmatter 供 Obsidian/Dataview/indexing 使用。
- **LingLens 概念透鏡**：`@ling-lens` 可用概念視角掃描文章，找出不能只靠 Ctrl+F 找到的語意實例；`@ling-count` 保留為 legacy alias。
- **Agentic command workflow**：在 `toLingLing/` 放入 `@ling-*` 指令檔，即可觸發巡邏、修復、洞察、合併、備份、還原等任務。支援 `/template` 指令動態切換輸出格式。
- **Markdown quality checker**：寫入 Obsidian 前會修復常見 Markdown/Mermaid/LaTeX 問題，包含裸 Mermaid block、未關閉 fence、node label quote、body frontmatter 與 `\rightarrow` 這類 carriage-return 轉義錯誤，並在 metadata 留下 `quality_checker` 紀錄。
- **Knowledge dashboard**：`index.md` 會自動更新，支援自然排序與 Obsidian callouts。

## 快速開始

### 1. 必要條件

- Python 3.10+
- Obsidian
- 一個 LLM provider：Ollama、vLLM 或 Gemini API

### 2. 安裝

```bash
git clone <repo-url>
cd ling-ling
cp .env.example .env
vim .env
```

`.env` 主要設定：

- `LLM_PROVIDER=ollama|vllm|gemini`
- `OLLAMA_API_BASE` / `OLLAMA_MODEL`
- `VLLM_API_BASE` / `VLLM_MODEL`
- `GEMINI_API_KEY` / `GEMINI_MODEL`
- `CHUNK_SIZE` / `CHUNK_OVERLAP`

### 3. 啟動 daemon

```bash
./start.sh
```

啟動腳本會在第一次執行時建立 `venv` 並安裝 `requirements.txt`。

健康檢查：

```bash
./start.sh --check
```

## 使用方式

### 整理長文

1. 把 Markdown 放進 `lings-desktop/Consolidate/`。
2. Ling-Ling 會產生：
   - `pages/<Title>/<Title> (Part N).md`
   - `pages/<Title>/<Title> (Stitched).md`
   - `pages/<Title>/<Title> (Synthesis).md`
3. 原始檔會移到 `raw/consolidate/`。

如果 Markdown 有 sidecar images，目錄格式為：

```text
Consolidate/
├── Article.md
└── images/
    └── Article/
        └── page_1.jpeg
```

處理後 sidecar images 會一起歸檔到 `raw/consolidate/images/`。

### 下指令給 Ling-Ling

在 `lings-desktop/toLingLing/` 建立 Markdown 指令檔，例如：

```markdown
@ling-lens [[西遊記]]
Count: 幫助孫悟空的人、神、妖怪
Confidence: medium
```

完成後：

- 回覆會寫到 `fromLingLing/`
- 原始指令會移到 `raw/prompts/`

## 指令一覽

- `@ling-lens`：概念透鏡。掃描文章中的語意實例，輸出 evidence、confidence、分析錨點與原文範圍。
- `@ling-count`：`@ling-lens` 的舊別名，仍可使用。
- `@ling-patrol`：全庫健康檢查，找出死連結、孤兒頁面與資料庫不同步。
- `@ling-patrol-tags`：標籤巡邏，找出未翻譯或格式錯誤的標籤。
- `@ling-repair-tags`：根據巡邏清單批量修復標籤。
- `@ling-repair-db`：資料庫修復與重新同步。
- `@ling-insight`：從既有筆記生成跨領域洞察。
- `@ling-merge`：合併筆記。內容包含 `[[A]]` 與 `[[B]]` 即可融合兩篇筆記。
- `@ling-plan`：規劃多步工作流（Pipeline），產出腳本與預覽報告，不立即執行。
- `@ling-do`：執行 `@ling-plan` 產出的工作流腳本。
- `@ling-profiles`：管理文件路由 Profiles。`pending` 看待審草稿、`approve <名稱>` 一鍵生效。
- `@ling-zip` / `@ling-unzip`：備份與還原知識庫。
- `@ling-RESET`：清除內容前會先強制備份。

## 專案結構

```text
ling-ling/
├── Backups/                    # @ling-zip 與 RESET 前備份
├── System_Engine/              # Python daemon 與 agent 程式
│   ├── agents/                 # LingLens、Insight、Merge、Patrol 等 agents
│   ├── core/                   # config、parser、state、ui、tag manager
│   ├── maintenance/            # health check、DB repair、release helper
│   ├── services/               # LLM client、RAG、media、ingestion pipeline
│   ├── tests/                  # pytest-style unit tests
│   └── watchers/               # filesystem watchers
├── lings-desktop/              # Obsidian vault
├── requirements.txt
└── start.sh
```

```text
lings-desktop/
├── Assets/                     # 圖片與附件
├── Clippings/                  # 外部剪報暫存
├── Consolidate/                # 待整理資料入口
├── Database/                   # ChromaDB
├── fromLingLing/               # agent 產出
├── Insights/                   # insight 報告
├── Notes/                      # 人類筆記
├── pages/                      # AI 生成 wiki pages
├── raw/                        # 已處理原始檔與指令歸檔
├── Scripture/                  # persona、語言、系統設定
├── Skills/                     # 分析方法包
├── Templates/                  # 輸出模板與 agent prompts
├── toLingLing/                 # 指令入口
├── toranomaki/                 # @ling-* 指令範例
└── index.md                    # 自動維護的知識地圖
```

## Refactor Notes

### 2026-06-11 Cortex Memory (Phase 2.5 - 可反駁性與抽取錨點)

- **第五訊號 falsifiability**：把 Popper 操作化——`assess_falsifiability` 要求 LLM「描述一個能推翻此主張的具體觀察」，寫不出＝低分（0/0.5/1 三檔，越界自動夾制）。建新 Cortex 頁時評一次（merge 路徑零額外成本），分數掛鉤初始 confidence（`0.3 + 0.4×score`）——不可反駁的主張以低信心進場而非被拒。緣起：首輪驗證發現「有道理＋無法反駁＋不知如何實施」的占星術組合，而模糊是對 refute 訊號的天然護甲。
- **抽取錨點 applies_when**：每條 claim 附「適用情境」，prompt 明文糾偏「原子 ≠ 無條件全稱」；頁面以 `> 適用情境：` blockquote 確定性編碼進 Core Claim 節（round-trip 有測試把關）。
- **證據鏈穿透**：claim 的 sources 除 insight frontmatter 外，追加解析 insight 內文的 `[[wikilink]]`（存在性過濾、上限 5）——「如何實施」的脈絡一跳可達。
- **量尺修缺**：斷鏈率只計過閘 insights（首輪 90% 是把被擋的 planner 文件算進去的假象，且閘門判定改為與鞏固管線共用同一函式）；新增 `refute_coverage` 與 falsifiability 分佈（mean < 0.4 黃線）；人工抽查清單每條附「證偽」提示。
- **回補基線**：`maintenance/backfill_falsifiability.py` 對既有頁面補測（只加測量、不改 confidence/S/時間戳——不破壞 reconsolidation 歷史）。

### 2026-06-11 Cortex Memory (Phase 2 - 夜間鞏固)

- **Insight → 原子主張 → Cortex 頁**：dreaming window 的每日任務把通過 Phase 1 訊號閘門的 insights（排除 refuted 與低 groundedness）蒸餾成原子主張（每份最多 3 條），鞏固進獨立的 `Cortex/` 目錄——**一頁一主張**，這是長期記憶的最高層。
- **只有雙向蘊涵才合併**：embedding 鄰居（≥0.80、top-3）交給 LLM 做六值裁決（equivalent / entails / entailed_by / complementary / contradicts / unrelated）；只有 `equivalent` 觸發合併（reconsolidation：證據鏈增長、S+1、confidence 上調 cap 0.9、變體區 cap 5），其餘建**型別化連結**；`contradicts` 雙方互記並下調 confidence（floor 0.1，不擊殺——falsified 是 Phase 4）。裁決結果內容定址快取、無 TTL。
- **頁面結構鎖死**：[services/cortex_store.py](System_Engine/services/cortex_store.py) 是唯一讀寫路徑——機器狀態（S / confidence / 證據鏈）在 frontmatter、內文固定四節，全部確定性程式碼操作，LLM 永不重寫整頁；`parse(render())` round-trip 有測試把關。
- **進檢索閉環**：Cortex 頁建立/更新即 `add_document` + facet；`Cortex/` 納入 vault watcher 與 orphan sweep——你刪頁面，chunks 自動清理。
- 配額：每晚 10 份 insight 抽取、20 次蘊涵裁決（`CORTEX_*` flags 可調）。

### 2026-06-10 Cortex Memory (Phase 1 - Insight Quality Signals)

- **四項品質雷達**：為所有 `@ling-insight` 產出加上 `groundedness` (引用存活率)、`novelty` (內容新穎度，Cosine Distance vs `insight_signals.json` 歷史)、`bridging` (跨領域融合度，Target embeddings 間的最大距離)，以及 `refute_verdict` (LLM Challenger 壓力測試)。
- **Fail-open 哲學**：所有計算皆包裹於 try-except，不干擾報告主流程；即使 LLM 失敗或 RAG 失聯，依然會寫入檔案（帶空訊號）。
- **Sidecar FIFO**：採用 atomic swap 更新 `Database/insight_signals.json`，並利用 UTC timestamp 滾動淘汰舊的 embedding 快取，將記憶體與磁碟佔用上限鎖定在 500 筆。

### 2026-06-10 Profile Routing（取代 DocType.md）

- **Profile 制**：`Scripture/Profiles/*.md` 每檔一個具名「persona + template」配對（frontmatter：`persona / template / operations / description / applicable_when`），路由器選 profile 而非分別選 persona/template，配對衝突從結構上消失。新的 [services/profile_manager.py](System_Engine/services/profile_manager.py) 負責掃描、`DocType.md` 一次性遷移與 `_pending/` 審核佇列。使用說明見 [Scripture/Profiles/_README.md](Scripture/Profiles/_README.md)。
- **三層解析**：文件 frontmatter 覆寫（`profile:` 或 `synthesis_persona`/`synthesis_template`）> 自動選擇（`document_type` 直接命中，否則 `LLMClient.select_profile` 在已註冊 profile 中做封閉式選擇）> `default` profile / Scripture 設定。封閉式選擇取代了舊的開放式分類＋查表，答案永遠可執行。
- **審核佇列**：無法分類的新類型會自動草擬 persona/template/profile 三件套進 `Scripture/Profiles/_pending/<類型>/` 並通知 `fromLingLing/`，審核搬檔後才生效；當次先用 `default` 處理（品質優先於即時性）。
- **統一資產契約**：Personas 與 Templates 改經 `_load_capability_body` 載入，frontmatter 一律剝除後才進 system prompt——之後可安全地替 persona/template 加上與 Operations 同款的 metadata。
- **併發加固**：vault 刪檔搶不到鎖改為排程重試（不再無鎖裸跑 RAG）；`DynamicSettings.reload()` 加鎖並改為先解析再原子套用；MaintenanceScheduler 狀態檔改 temp-file + rename 原子寫入；watcher 同步/處理失敗改為 `ui.error` 浮出。

### 2026-06-10 Routing Observability & Ops（Profile 制後續五件套）

- **路由決策追蹤**：每次 ingestion 寫一筆 `routing_decision` artifact（profile、解析層 `frontmatter_override/frontmatter_profile/llm_selection/default_profile/settings_fallback`、是否 fallback、是否送審）。TraceStore 新增 `query_artifacts()` / `query_llm_calls()` 分析介面。
- **路由健康報告**（[maintenance/routing_report.py](System_Engine/maintenance/routing_report.py)）：每週彙整 fallback 率、各層分佈、未被選用的 profile、待審草稿。摘要固定進 `maintenance.log.md`；有可行動項（fallback 超過 30%、有待審草稿、選擇器失敗）才寫完整報告到 `fromLingLing/`。
- **Template 版本化**：template frontmatter 可宣告 `version:`；生成頁面 frontmatter 蓋上 `template` / `template_version` 戳記。每週 [maintenance/template_audit.py](System_Engine/maintenance/template_audit.py) 稽核 `pages/`，列出用舊版 template 渲染的頁面（不自動重渲染，只追蹤）。
- **`@ling-profiles` 指令**（[agents/profiles_agent.py](System_Engine/agents/profiles_agent.py)）：`@ling-profiles` 總覽、`pending` 草稿明細、`approve <名稱>` 一鍵把審核通過的草稿搬入正式位置（拒絕覆寫既有檔案）。
- **Skill 前置條件強制檢查**：`Skills/*.md` 的 `applicable_when`（`database_populated` / `min_documents` / `has_tag_graph`）在執行前對照實際 vault 狀態驗證，不滿足就明確拒跑（例如知識庫只有 5 份文件時擋下 montecarlo）。RAG 失效時 fail-open，不會反過來癱瘓 insight。

### 2026-06-10 Facet Backfill Pump（閒置時低優先權回填）

- **行為**：系統閒置（busy→idle 後 180 秒 grace）時，一次回填一頁的 facets，步間隔 30 秒；`toLingLing/`、`Consolidate/` 有新檔（mtime < 10 分鐘）就讓路，使用者工作永遠優先（busy lock 仲裁 + idle callback 最後註冊）。陳舊的卡住檔案不會餓死回填。
- **零成本捷徑**：Part 頁面的 facets 直接解析既有的「Part Digest Appendix」，不花 LLM call；只有無 appendix 的頁面才花一次 digest call。
- **佇列從 DB 推導**（不持久化，與 orphan sweep 同哲學），優先序：Synthesis → 近 30 天被檢索命中（trace 查詢）→ 一般頁 → Part 頁。唯一持久狀態是失敗 ledger（`Database/facet_backfill_state.json`）：單頁失敗 3 次隔離（檔案改過自動解除）、連續多頁失敗視為服務中斷退避 1 小時、每日 LLM 預算 1000 calls。
- **補完即安靜**：佇列空了就什麼都不做（一次性完成通知），新 ingestion 自帶 facets，佇列只在異常事件後重新出現。
- 參數：`FACET_BACKFILL_ENABLED` / `_GRACE_SECONDS`(180) / `_STEP_GAP_SECONDS`(30) / `_DAILY_BUDGET`(1000) / `_MAX_ATTEMPTS`(3) / `_MIN_BYTES`(400)。

### 2026-06-10 Self-Improving Bench Loop（檢索品質自動進步迴路）

- **評測集自動生長**（[maintenance/bench_builder.py](System_Engine/maintenance/bench_builder.py)，週任務）：每篇有 facet 的未覆蓋頁面，LLM 把 thesis 改寫成自然問句（禁止逐字抄），且**當下系統答得對才收錄**（品質閘門）。哲學是 regression guard：auto case 鎖定今天可用的能力，未來改動讓它失敗＝退步。寫入獨立的 `scratch/retrieval_bench_auto.yml`，手寫 bench 檔永不被改寫，auto 檔可隨時刪除重來。上限 `BENCH_AUTO_MAX_CASES`（30）、每輪 `BENCH_AUTO_PER_RUN`（5）。
- **Facet A/B lift**：每日 retrieval bench 每條 case 跑兩次（`use_facets` 開/關），量化 facet 淨貢獻。lift 持續為負時告警會建議關閉 `FACET_INDEX_ENABLED`。
- **歷史趨勢＋退步告警**：每次 bench 結果存入 `Database/bench_history.json`（365 筆、原子寫入）；pass rate 低於上次 → status 升為 `regressed` 並寫 `fromLingLing/` 告警，列出失敗查詢。
- 迴路全貌：**ingest（facet 自動產生）→ bench builder（評測集跟著長）→ daily bench（A/B 測量）→ history（趨勢）→ alert（退步立即可見）**——vault 越大，評測越完整，系統越知道自己哪裡退步。

### 2026-06-10 Facet Index（摘要向量指標檢索，Phase A+B）

- **概念**：LLM digest 的 `thesis` 與 `key_points` 被 embed 成「facet」條目存入同一個 collection（`role: facet`，共用母文件 doc_id）。Facet 是**檢索指標不是內容**——query 命中 facet 後，rerank 之前就解參照回母頁面的真實 chunk，下游永遠拿到原文。解決 query（問句）與 chunk（散文）的語意落差與跨語言落差。
- **Phase A（零新增 LLM 成本）**：長文 pipeline 既有的 part digests 直接轉成 facets，每個 part 頁面一批。
- **Phase B（每篇短文 +1 次輕量 LLM call）**：短文 ingest 後補跑一次 `generate_part_digest`（1/1），同樣轉 facets。
- **安全設計**：facet 共用母文件 doc_id，刪除路徑與 orphan sweep 自動涵蓋；重新 ingest 會先清舊 facets（冪等）；母 chunk 已在候選池時以較高排名者優先、不重複；幻覺風險被「只當指標、不當內容」的設計隔離。
- **控制**：`FACET_INDEX_ENABLED`（預設 true）、`FACET_MAX_PER_DOC`（預設 8）。建議用 `scratch/retrieval_bench.yml` 前後對比驗證檢索品質。

### 2026-06-10 RAG Orphan Sweep（殘留 chunk 清理）

- **問題**：刪除整個文章資料夾（`pages/<標題>/`）只發 directory 事件，舊 watcher 直接忽略；改名/搬移完全沒有 handler——兩者都讓 chunks 永久殘留在 ChromaDB。
- **修復**：`RAGManager.prune_orphan_chunks()` 以檔案系統為真相來源——掃描 `pages/`+`Notes/` 算出有效 doc_id 集合，刪除其餘所有 chunks（含無 doc_id 的 legacy chunks）。觸發點三處：資料夾刪除事件（debounce 5s）、新增的 `on_moved` handler（改名＝刪舊+索引新）、每日 maintenance task `rag_orphan_sweep_daily`（涵蓋 daemon 關機期間的刪除）。

### 2026-05-24 Capability Layer & Lens Dual-Link (Phase 4)

- **Capability metadata**：`Templates/Operations/*.md` 與 `Skills/*.md` 加上 frontmatter（`type / expected_inputs / produces / cost_class / applicable_when`）。新的 [services/capability_manager.py](System_Engine/services/capability_manager.py) 在 daemon 啟動時掃描並建索引；`LLMClient._build_system_prompt` 改回傳 `(prompt, resolution)`，resolution 寫入 `llm_calls.metadata_json.capability_resolution`，**不會**注入到 system prompt。Operations 的 frontmatter 在組 system prompt 前由 `_load_capability_body` 剝除。
- **Lens dual-link**：lens 報告的 evidence 同時輸出 Obsidian wikilink 與 `file:///` 連結。`file:///` 連結帶 `#L<start>-L<end>` fragment——只有 VS Code / Cursor 系列編輯器會跳到指定行，Obsidian 點擊與系統 `open` 會開檔但忽略行號。Wikilink 走 Obsidian 原生導航，永遠可用。
- **PipelineRunner / Pipeline DSL** 暫緩，設計草案見 [DesignDoc/PipelineRunner_roadmap.md](System_Engine/DesignDoc/PipelineRunner_roadmap.md)。

### 2026-05-23 RAG Quality & Cost Stack

第二輪 ChromaDB 優化，疊在 embedding provider / mismatch guard / migrations 之上。所有功能都可獨立 toggle，預設行為與舊版兼容。

**Ingestion 端（無腦省錢）**
- **Content-hash skip**：每個 chunk 多存 `content_hash = sha256(text + tags + section_path)`。`add_document` 比對既有 hash，相符就直接 return，連 delete 都不做。在 Obsidian 反覆存檔但未真的修改內容的情況下，零 embedding call。
- **Persistent embedding cache**（[services/embedding_cache.py](System_Engine/services/embedding_cache.py)）：SQLite at `Database/embedding_cache.sqlite`，key = `sha256(model || text)`。Cache hit ≈ 0.1ms vs cold ≈ 70ms (local MiniLM)。跨 provider 切換、wipe + reindex 都從 cache 直接回。控制：`EMBEDDING_CACHE_ENABLED`。

**Retrieval 端（品質 pipeline）**
- **MMR diversity**：`query_notes(diversity: float)` 0~1。over-fetch top_k*3，cosine-MMR 選出 top_k，消除相鄰 chunk 霸佔 top-k 的問題。
- **Cross-encoder reranker**（[services/reranker.py](System_Engine/services/reranker.py)）：`rerank=True` 時 over-fetch top_k*5，用 cross-encoder（預設 `BAAI/bge-reranker-v2-m3`）重新打分。延遲導入，未啟用時零開銷；sentence-transformers 缺失會 graceful fallback 到向量檢索。控制：`RERANKER_ENABLED` + `RERANKER_MODEL` + `RERANKER_MULTIPLIER`。
- **Hybrid BM25 + RRF**（[services/bm25_index.py](System_Engine/services/bm25_index.py)）：`hybrid=True` 時並列查向量與 BM25，Reciprocal Rank Fusion 合併。BM25 lazy rebuild from collection，add/delete 觸發 dirty flag；對「@ling-lens」「XYZBLATZ」這類精確 token 查詢顯著回升 recall。控制：`HYBRID_RETRIEVAL_ENABLED` + `BM25_MULTIPLIER`。

三層 retrieval feature 可以自由組合：`hybrid → rerank → MMR` 依此順序套用，rerank 的分數會餵給 MMR 當 relevance 訊號，hybrid 的 RRF 分數同理。

**啟動成本**
- `_check_metadata_mismatch` 不再每次啟動都 probe 一次 embedding dimension。Provider+model name 已是 authoritative key，匹配時直接 return；只在空 collection 初始化 metadata 時才打一次模型。Gemini 用戶啟動時節省一次 paid API call。

**依賴**
- `rank_bm25` 已加入 `requirements.txt`（純 Python，~10KB）。
- `sentence-transformers` 列為選用依賴；只在需要 reranker 時手動 `pip install`。

### 2026-05-23 Monte Carlo Concept-Level Sampling

- `InsightAgent._get_all_documents` 改成兩階段抽樣：先 uniform 抽 Book，再從每本書抽多個 chunk，讓碰撞池呈現概念層級的多樣性，而不是每本書只露出一個代表 chunk。
- 每本書內的 tier 優先順序反轉為 **raw Parts > (Synthesis) > (Stitched)**，保留未經提煉的原始概念給 Monte Carlo 碰撞使用；distilled tier 只在沒有 Parts 時 fallback。
- 新增 strategy frontmatter 參數 `chunks_per_book`（預設 5）。設小會更廣（更多本書、每本少抽），設大會更深（少本書、每本多抽）。
- 同一本書的不同 chunk 仍可配對，允許跨章節的概念碰撞。
- 移除舊的 `_pick_representative_title`，由新的 `_docs_from_book` 取代。

### 2026-05-23 Refactor Follow-up

- `BaseAgent._write_report()` 現在回傳 `(path, full_markdown)`；第二個值是已寫入磁碟的完整文件（YAML frontmatter + body）。需要 mirror report 的 caller 應直接寫這份完整內容。
- `InsightAgent` 的 `Insights/` mirror copy 改為與 `fromLingLing/` canonical report byte-identical，避免 mirror 失去 `title`、`type`、`version`、`date_created`、`input_chars`、`output_chars` 等 metadata。
- Mermaid label repair 擴充到多種 node shape（`[]`, `()`, `{}`, `{{}}`, `[[ ]]`, `[()]`, `([ ])` 等），並修正 `A[Start] --> B[End]` 這類箭頭被誤判成 asymmetric node 的 regression。
- `TextSplitter` 預先計算 fenced-code regions，避免在每個 chunk boundary 重新掃描全部 fence，並保留 legacy helper API 供測試與外部呼叫。
- 新增 LLM-free regression tests，覆蓋 CounterAgent、IngestionPipeline、Insight mirror、LLMClient helper、Mermaid repair、TextSplitter fence protection。

### 2026-05-15 Ingestion Pipeline Refactor

- `ClippingWatcher` 現在只負責 filesystem events、busy state、檔案類型分流與歸檔。
- Markdown ingestion 主流程移到 `services/ingestion_pipeline.py`。
- 長文切分改用 `TextSplitter.split_text_with_spans()`，保留每個 chunk 對應的原文 char/line range。
- Part metadata 會寫入 `source_start_char`、`source_end_char`、`source_start_line`、`source_end_line`。
- Synthesis metadata 會寫入 `part_source_map`。
- Stitched Article 會在每個 Part 前顯示原文範圍，供 LingLens evidence link 使用。
- `extract_json_array()` 與 `extract_json_object()` 移到 `core.parser`，讓 LLM JSON parsing 共用。
- digest value formatting 移到 `core.utils.digest_value_to_text()`。
- `PromptWatcher` 改用 declarative intent routing table，`@ling-lens` 與 `/lens` 成為正式入口。
- `VaultWatcher` 復用 main process 的 `LLMClient` 翻譯新標籤，避免每次手動改檔都建立新的 client。
- **Unified Template Routing**: 實作四層級模板路由（指令 > Skill > Scripture > 系統預設），支援在指令中加註 `/template tech-rpt` 隨時切換格式。
- **Mermaid & YAML Hardening**: 修復 Mermaid 標籤自動加引號（支援中文字元與數字 ID）、修復 Mermaid fence 提前關閉的預判邏輯，以及防止 Markdown 水平線被誤判為 YAML frontmatter 的防呆機制。

### 2026-05-13 LingLens Evidence Grounding

- 新增 `@ling-lens` / `/lens` 概念透鏡指令。
- `@ling-count` / `/count` 保留為 legacy alias。
- 多文章與多概念會輸出 cross-analysis matrix。
- evidence 會盡量連回 Stitched Part anchor 與原始檔。
- 若 Part metadata 有原文範圍，報告會顯示原文 line range。

### 2026-05-10 Synthesis Quality Upgrade

- 長文解析由 Part 第一行摘要升級為 structured digest synthesis。
- 每個 Part 會整理 `thesis`、`key_points`、`evidence`、`terms`、`open_questions`、`handoff`。
- Synthesis 會附上 Part Digest Appendix，方便檢查合成依據。
- Markdown quality checker 會修復裸 Mermaid、未關閉 Mermaid fence、body YAML frontmatter 等常見問題。

## 開發檢查

語法與 whitespace 檢查：

```bash
python3 -m compileall -q System_Engine
git diff --check
```

若使用 `venv`：

```bash
venv/bin/python -m py_compile \
  System_Engine/services/ingestion_pipeline.py \
  System_Engine/watchers/clipping_watcher.py \
  System_Engine/agents/counter_agent.py \
  System_Engine/services/llm_client.py \
  System_Engine/watchers/prompt_watcher.py \
  System_Engine/watchers/vault_watcher.py \
  System_Engine/core/parser.py \
  System_Engine/core/utils.py \
  System_Engine/services/text_splitter.py
```

pytest regression suite（需要先安裝 pytest）：

```bash
venv/bin/pip install pytest
PYTHONPATH="$PWD/System_Engine" venv/bin/python -m pytest -q System_Engine/tests
```

日常開發可用較小的 test profile，避免每次都手動挑一長串測試。完整分層見
`System_Engine/DesignDoc/Test_Profiles.md`。例如 planner/executor 相關改動：

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

封版或宣告 phase complete 前仍以 full suite 作為 gate。

Markdown/Mermaid smoke test：

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python System_Engine/scratch/test_markdown_quality.py
```

LingLens scratch regression：

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python System_Engine/scratch/test_counter_agent.py
```

## License

MIT License Copyright (c) 2026 [MH / Project Ling Ling]

Ling-Ling's Note: "Mom said you can use my code, but don't claim you wrote it yourself. That's plagiarism!"
