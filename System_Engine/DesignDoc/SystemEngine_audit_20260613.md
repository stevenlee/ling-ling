# System_Engine 程式碼審查與效能稽核報告

## 1. 摘要

**確認發現（45 項，已由獨立 skeptic 驗證）依維度與嚴重度分布：**

| 維度 dimension | High | Medium | 小計 |
|---|---|---|---|
| correctness | 7 | 6 | 13 |
| performance | 5 | 12 | 17 |
| architecture | 0 | 3 | 3 |
| simplification | 0 | 5 | 5 |
| **合計** | **12** | **26** | **38 confirmed** |

（另有 33 項 low-severity 未驗證，僅列於第 5 節供參。）

**最重要的 takeaway：** 真正會造成資料/行為錯誤的高優先 correctness bug 集中在三處 — 並行控制（`maintenance_scheduler.py:389` 的 `set_busy` 搶佔、`prompt_watcher.py:81` 阻塞 dispatch thread）、靜默資料遺失（`ingestion_pipeline.py:403` 短文標題誤掛 `(Synthesis)`、`rag_manager.py:424` 同名文件互刪 chunk、`parser.py:431` 空 label 節點被刪、`profile_manager.py` 大小寫 key miss、`trace_store.py` finally 遮蔽原始例外）。**這些應先於所有效能優化處理**，因為它們在正常運行下就會悄悄毀損資料或狀態，而多數效能項在單機/retention-bounded 規模下其實是 low/medium 等級的 cleanup。

---

## 2. 🔴 高優先（confirmed high）

### A. 並行與排程正確性（race / blocking）

**`watchers/maintenance_scheduler.py:389` — `set_busy(True)` 搶佔並行 owner**
`_run_task` 無條件呼叫 `global_busy_state.set_busy(True)`，未檢查 flag 是否已被持有。若 vault watcher / prompt watcher 已持有 busy state，maintenance task 會靜默接管；原 owner 的 `finally` 隨後 `set_busy(False)`，在 maintenance task 仍執行中提前釋放守衛，允許第二個並行 owner。
**修法：** 在進入 try 前以 `try_set_busy()` 取得守衛，回傳 False 時直接 `return`（不進入 try/finally，否則 finally 的 `set_busy(False)` 會釋放他人的鎖）。可順帶移除 line 347 的 `idle_required/is_busy()` 預檢，讓 `try_set_busy()` 成為唯一權威 gate（涵蓋 `idle_required=False` 的 `trace_prune_daily`）。

**`watchers/prompt_watcher.py:81` — `time.sleep(1)` 阻塞 watchdog dispatch thread**
`_handle_event` 在 watchdog observer thread 上 `time.sleep(1)`；單一 dispatch thread 被阻塞一整秒，期間 OS 緩衝後續事件，快速建檔時造成事件延遲/合併。隨後 `_drain_queue()` 又在同 thread 同步持有 busy state。
**修法：** 移除 sleep；將 `_drain_queue()`/`process_prompt()` 移到專用 worker thread，`_handle_event` 只 enqueue 後即返回。若仍需 FS 穩定延遲，在 worker dequeue 後 re-check `filepath.exists()`，不要阻塞 dispatch thread。

### B. 靜默資料遺失 / 損毀

**`System_Engine/services/ingestion_pipeline.py:403` — 短文頁面被誤命名為 `"{stem} (Synthesis)"`**
短文路徑（line 107，`part_info=None`）走 else 分支，標題永遠加上 ` (Synthesis)` 後綴並寫入 `PAGES_DIR / base_title / "{stem} (Synthesis).md"`，RAG index 與 `update_wiki_index` 都記錄此誤名，會與後續同 stem long-doc 的真正 synthesis 文件碰撞，且每個短文頁面永久標籤錯誤。
**修法：** 改為 `title = f"{base_title} (Part {part_info['current']})" if part_info and part_info.get('current') else base_title`，`(Synthesis)` 命名只保留在 `_write_synthesis` 內（它不經 `ingest_to_wiki`，故移除 else 分支不會回歸真正 synthesis 頁）。

**`System_Engine/services/rag_manager.py:424` — `add_document` 每次都 `delete_document(title)`，誤刪同名他文件的 chunk**
line 424-425 在以 `doc_id` 刪除後，又無條件呼叫 `delete_document(title)`，後者以 `collection.delete(where={'title': title})`（line 1176）刪除。兩篇皆名為 'Introduction'/'Overview' 的筆記，重建第一篇時會靜默刪掉第二篇既有 chunk。
**修法：** 移除 hot path 的 `delete_document(title)`（doc_id 刪除已涵蓋自身 chunk）。legacy title-only chunk（無 doc_id 的舊資料）改為一次性 migration，scope 在 `where={"$and":[{"title":title},{"doc_id":{"$exists":False}}]}`。注意原建議的 `{'doc_id':{'$ne':doc_id}}` 仍會誤刪有 doc_id 的 sibling，須改用「缺 doc_id」條件。

**`core/parser.py:431` — `_quote_labels_in_line` 靜默刪除空 label 節點定義**
label 空白（如 `A[]`）時 `if stripped:` 為 False，node-ID 前綴與 shape 括號都未 append 到 `out`，但 `matched=True` 且 `i` 已前進，節點定義被靜默刪除（`A[]` 是合法 Mermaid）。
**修法：** 為 `if stripped:` 補 else，在 `i = close_at + len(closer)` 前 `out.append(code_part[i:close_at + len(closer)])` 原樣輸出，`changed` 維持 False。

**`System_Engine/services/profile_manager.py` — profile key 以原始大小寫存、以 lowercase 查，mixed-case stem 必 miss**
`reload()`（line 160）以 `spec.name = path.stem`（原始 stem）存入 `self._specs`，`get()`（line 165）以 `.strip().lower()` 查。`Book.md` 存為 `'Book'` 卻查 `'book'`，保證 miss。
**修法：** 存入時正規化 key：`self._specs[spec.name.lower()] = spec`（line 160）；保留 `spec.name` 為可讀 stem 供 `selection_options()` 顯示。可額外對 lowered key 碰撞發 warning。

**`System_Engine/services/trace_store.py` — `run()` finally 的 DB 例外遮蔽原始 run 例外**
`run()` context manager（line 218-225）finally 內 `conn.execute('UPDATE runs ...')`；若 UPDATE 拋例外（DB locked / disk full），Python 以新例外取代 `yield` 的原始例外，永久遺失原錯，且 run record 永遠停在 `status='running'`。
**修法：** 將 finally 的 UPDATE 包進自有 try/except 並 log（比照本檔 `prune_old`/`recently_retrieved_titles` 既有防禦模式），保留原始例外傳播。orphaned 'running' row 需另以啟動掃描補救（非本修法範圍）。

### C. 傳輸/語意正確性（LLM client / counter）

**`services/llm_client.py:829-900` — `translate_tags` 繞過 `_complete_text`，無 transport retry、trace 樣板重複**
直接呼叫 Gemini `generate_content` / OpenAI `chat.completions.create`，繞過 `_complete_text` 與 `_complete_provider_text_with_retry`；短暫 429/503 靜默回 `{}` 而不重試，並手動重建約 35 行成功/失敗 trace 區塊。
**修法：** 整個 body 改為單一 `_complete_json(kind="object", system_prompt=..., user_msg=f"Tags: {tags}", temperature=0.1, trace_context={"stage":"translate_tags","metadata":{"tag_count":len(tags)}})`，繼承 retry 與集中 trace/token 處理。可棄 Gemini `response_mime_type` fork（`extract_json_object` 已處理 fenced 輸出，line 900 即依賴此）；`_complete_json` parse miss 回空 dict，與現 `{}` fallback 一致。

**`System_Engine/agents/counter_agent.py:337` — 空陣列檢查用去空白字串，遮蔽 parse 失敗**
`if instances or "[]" in _WS_RE.sub("", raw):` 會讓 `{"key":[]}`、markdown 表格等任何含連續 `[]` 的回覆 short-circuit 掉 retry，誤判為「LLM 宣告空集合」而非 parse 失敗。
**修法：** 要求整段去空白後恰等於 `[]`，而非「包含」：`_ws_collapsed = _WS_RE.sub("", raw); if instances or _ws_collapsed == "[]":`。可同時剝除前後 ```json fence 再比對。注意原建議的 `re.search(r'\[\s*\]', raw)` 仍會誤中巢狀 `{"x":[]}`，須用整串相等。

**`System_Engine/agents/counter_agent.py:250` — RAG fallback 把格式化 markdown 當 article_text 傳入**
`results.append(("(RAG result)", rag_results[0], ""))`；`query_similar_notes` 回的是已格式化 markdown（`### [來自筆記: {title}]\n{doc}`），直接餵 `TextSplitter.split_text` 與抽取 prompt，注入的 heading 會污染 quote anchor 與抽取的 quote；且 `top_k=1` 不論文章長度只取單 chunk。
**修法：** 改用 dict API：`rag_results = self.rag.query_notes(user_directive, top_k=1)`，append `rag_results[0]["text"]`，title 取自 `rag_results[0]["metadata"].get("title")`。或剝除注入的 `### [來自筆記: ...]\n` header line。多 chunk 文章可酌量提高 top_k 或 chunk-join。

### D. 高優先效能（詳見第 4 節）

- `services/builtin_adapters.py:367` — `digest_sources` 每 source 一次序列 LLM 呼叫，無並行（真高優先：直接影響使用者等待延遲）。
- `maintenance/cortex_consolidation.py` — `load_all_pages` 在每個 nightly pass 各自呼叫，整個 Cortex/ 每晚重解析 5-6 次。
- 其餘原列為 high 但 skeptic 重分級為 low/medium 的（`rag_manager.py:1069`、`embedding_cache.py:37`、`insight_agent.py:1287`、`counter_agent.py:771`），見第 4 節說明。

---

## 3. 🟡 中優先（confirmed medium，依主題分組）

### 主題一：correctness（語意/邊界）

- **`services/llm_client.py:1565` — `_assess_falsifiability_once` 在多語 codebase 硬編 Traditional Chinese。** prompt 強制 `falsifier_zh` 為「繁體中文」而不看 `settings.OUTPUT_LANGUAGE`，簡中/日文部署產出錯誤 gloss（line 1589 append）。修法：改用 `self._get_lang_hint()` 並更名為中性 `falsifier_localized`，或移除 localized 欄位改於 render 層處理；當語言為英文或已相符時 guard 不加 gloss。
- **`System_Engine/agents/insight_agent.py:1372` — `_build_targeted_pairs` 最終 fallback 用 `random.choice(all_docs)` 忽略 exclude set。** 小型 vault 可能回傳前輪已探索過的 pair，破壞跨輪去重。修法：從 `other_docs`（或排除 self 的 `all_docs`）取樣，並以 `_pair_key` 檢查不在 exclude 才 append；全部探索完回空亦可（caller line 1043 視無 pair 為停止條件）。
- **`maintenance/cortex_decay_pass.py:211-215` — revalidation sources 為空時靜默 `continue`，fading page 既未強化也未懲罰。** 未寫 `revalidated` timestamp，導致該頁每晚被重選，空耗 quota。修法：在 `if not contents: continue` 前先寫 `observed.setdefault(page.claim_id, {})['revalidated'] = _now_iso()`，讓 cooldown 推進；exception path 是否同樣前移視 transient 失敗是否該提早重試而定。
- **`core/parser.py:56` — `parse_markdown_metadata` 在檔案結尾 `---` 後無換行時漏抓 frontmatter。** `_FRONTMATTER_RE` 要求閉合 `---` 後有字面 `\n`，無尾換行的檔案完全 miss，所有 frontmatter YAML key 遺失。修法：pattern 改 `r'^---\s*\n(.*?)\n---\s*(?:\n|$)'`（比照 `vault_utils._FRONTMATTER_NL_RE`、`config._FRONTMATTER_RE`）。

### 主題二：architecture（抽象邊界）

- **`System_Engine/services/ingestion_pipeline.py:811` — `_run_synthesis_critique` 跨抽象邊界呼叫 LLM client 私有方法 `_format_part_digest_for_prompt`。** 修法：將該 `@staticmethod` 更名為 public `LLMClient.format_digest_for_prompt`，更新 call sites（`llm_client.py:1089`、`ingestion_pipeline.py:811`、`scratch/bench_synthesis_ab.py:42`）；勿把格式邏輯複製進 pipeline（synthesis 與 critique 須一致格式）。
- **`System_Engine/agents/insight_agent.py:1111` — `_fetch_all_title_meta` 等直接 `rag.collection.get(...)`，繞過 RAG service 層。** 同型態重複於 line 1202、1670、1684、1729。修法：在 `RagManager` 加 public 包裝（如 `get_all_metadata()`、`get_chunks_by_title(...)`、`get_all_documents_with_metadata()`，命名比照既有 `get_all_indexed_titles` 風格），replace 全部 call sites，解耦對 ChromaDB result-dict 形狀的依賴。
- **`core/vault_utils.py:321` — `update_wiki_index` 接收從不使用的 `filepath`/`title` 參數，API 誤導。** 兩參數 body 內未被引用，函式永遠做 full rglob 全掃。修法：移除 dead 參數，更新 callers（`ingestion_pipeline.py:443,553`；`vault_watcher.py:60,172,226`）；docstring 已正確標示為 full rebuild。

### 主題三：simplification（重複碼）

- **`System_Engine/agents/insight_agent.py:140` — signals 序列化區塊在 `generate_insight`(140-150) 與 `generate_full_insight`(229-241) 逐字重複。** 修法：抽 `_compute_signals_meta(self, content, target_titles) -> dict`（含 flag 檢查，off 時回 `{}`），兩處改 `.update(...)`。
- **`System_Engine/agents/insight_agent.py:1048` — `tuple(sorted([a["title"], b["title"]]))` 在 1048/1233/1348/1361/1370 重複五次。** 修法：加 `@staticmethod _pair_key(a, b)`，五處全部改走它。
- **`System_Engine/agents/counter_agent.py:770` — `_original_source_title` 與 `_resolve_original_source_path` 重複同一三步查找。** 修法：合併為 `_resolve_original_source(article_title) -> tuple[str, Path|None]`，兩者改為薄 wrapper。
- **`watchers/prompt_watcher.py:266` — template-match regex 在 agent path(262-264) 與 default Q&A path(297-299) 逐字重複。** 修法：抽 `_extract_forced_template(lower_query)`，在分支前呼叫一次；agent arm 僅在 truthy 時設 `context["forced_template"]`。

### 主題四：performance（中優先，見第 4 節彙整）

`rag_manager.py:763`、`bm25_index.py:63`、`ingestion_pipeline.py:930`、`ingestion_pipeline.py:167`、`insight_agent.py:1433/1705`、`cortex_decay_pass.py:152`、`cortex_consolidation.py`（embedding scan / state 寫入）、`trace_store.py`（ts index / ContextVar）— 全數整理於第 4 節。

---

## 4. ⚡ 效能專節

所有 performance 維度 confirmed 發現彙整，含相對成本與建議處理順序。注意：多項在單機 / cache-on / retention-bounded 規模下被 skeptic 降級。

| # | file:line | 真實影響 | 重分級 |
|---|---|---|---|
| P1 | `builtin_adapters.py:367` | 每 source 一次序列 LLM 呼叫；5 source = 5 次序列往返，直接疊加使用者等待延遲 | **真 high** |
| P2 | `counter_agent.py:171` | `_run_single_count` 每 chunk 一次序列 LLM；50-chunk 文章僅抽取就 100-200s；matrix path 加乘 | **真 high-ish**（最大使用者體感） |
| P3 | `cortex_consolidation.py` (load_all_pages) | 整個 Cortex/ 每晚被 5-6 個 pass 各自重解析（500 頁 ~2MB I/O + YAML/regex ×5-6） | high → 易修、低風險 |
| P4 | `insight_agent.py:1433` | `_expand_seed` 每 winner 一次 embed+RAG+LLM，序列；3×3=9 次阻塞往返 | medium，可平行化 |
| P5 | `rag_manager.py:1069` | `_dereference_facets` 每 facet 一次 `collection.get`（8 facet→3 parent = 5 次冗餘 full-doc fetch） | high → **medium**（受 candidate pool 上限） |
| P6 | `insight_agent.py:1287` | `_resolve_target_doc` 掃 title_meta 時多次 `_doc_from_rag_title` | high → **low**（僅 4 score tier，最多 ~3 次冗餘） |
| P7 | `embedding_cache.py:37` | 每次 `_batch_get/_batch_put` 開新 SQLite 連線 | high → **low/medium**（miss path 的 embedding 呼叫才是主成本；且須 thread-local） |
| P8 | `cortex_consolidation.py` (cosine scan) | 每新 claim 對全頁 O(N) Python cosine；N=500,C=50→25k ops | medium（非 N+1，embedding 有 cache） |
| P9 | `cortex_consolidation.py` (state 寫入) | 迴圈內每 insight 寫整份 state（含 embeddings），預設 max_insights=10 → ~10 次冗餘序列化 | medium |
| P10 | `cortex_decay_pass.py:152` | state file 被讀兩次（`_load_state` + `load_params`） | medium，trivial 修 |
| P11 | `bm25_index.py:63` | rebuild 全 `collection.get(include=['documents'])` 載入全文進 RAM | medium（lazy coalesce 已緩解，須 profiling 佐證） |
| P12 | `rag_manager.py:763` | MMR path `self.ef([query_text])` 重複 embedding | medium → **low**（cache-on 時是 SQLite hit，非 API call） |
| P13 | `ingestion_pipeline.py:930` | part 檔寫後立即讀回 append digest（2N I/O） | medium → **low**（剛寫、cache-resident 小檔，被 per-part LLM 延遲掩蓋） |
| P14 | `ingestion_pipeline.py:167` | 每短文額外一次 `generate_part_digest` LLM 呼叫填 facet index | medium（須 template 改動才能真省） |
| P15 | `insight_agent.py:1705` | `_get_tag_cluster_context` 每 chunk parse tags 兩次 | medium → **low**（僅 no-target path；cheap string split） |
| P16 | `counter_agent.py:771` | `_original_source_title` 每 instance FS I/O，O(instances) 次 | medium（per-article 快取一次即可） |
| P17 | `counter_agent.py:279` | 每 title-miss 對 PAGES_DIR 無上限 rglob，O(M×N) | medium（建 filename→path index 一次） |
| P18 | `trace_store.py` (ts index) | 四表時間窗查詢無 ts index，full scan | medium → low（retention-bounded，便宜保險） |
| P19 | `trace_store.py` (ContextVar) | `_CURRENT_TRACE_IDS` tuple append O(n²) 字串複製 | **micro，非真效能問題** |

**建議處理順序（投報比優先）：**
1. **P1、P2** — 並行化 `digest_sources` 與 `_run_single_count`（`ThreadPoolExecutor`, bounded `max_workers`，config flag `LENS_PARALLEL_CHUNKS`，結果按 index 重組），對使用者體感延遲影響最大。
2. **P3、P10、P9** — Cortex nightly I/O：orchestrator 載一次 pages 並 thread 進各 pass；`load_params(parsed=state.get('params'))` 免二次讀；移除迴圈內 state 寫入。低風險、易驗證。
3. **P5、P16、P17** — 批次/快取式的單機 I/O（facet dereference 批 `$in`、per-article 解析來源一次、vault filename index）。
4. **P4、P8** — 平行化 `_expand_winners`、cortex cosine 改 numpy batched matmul。
5. **其餘（P6/P7/P11/P12/P13/P15/P18/P19）** 視 profiling 結果選做，多為 low/optional cleanup；P19 可略。

---

## 5. 🟢 低優先（未驗證，僅供參考）

- `services/llm_client.py`：`generate_synthesis` 重複 `_build_system_prompt` 的 template 解析（simplification）
- `services/llm_client.py`：`classify_document` 為 `select_profile` 的弱化子集，可移除/統一（architecture）
- `services/llm_client.py`：多處冗餘 `max_tokens=None`（已是預設）（simplification）
- `rag_manager.py`：`add_document` 的 rel_path 解析重複 `_get_doc_id` 的 try/except（simplification）
- `rag_manager.py`：`_mmr_select` 無 embeddings 時靜默退化為 top-k slice（correctness）
- `rag_manager.py`：`_first_chunk_of_doc` 緊耦合 collection internals（architecture）
- `rag_manager.py`：BM25 where-filter 為每個 hit 多一次 `collection.get`（performance）
- `ingestion_pipeline.py`：`_extract_stitchable_body` 的 Path-reading 分支為 dead code（simplification）
- `thoughtful_splitter.py`：`_section_path_at` 線性掃描提早 break 漏掉後續 heading（correctness）
- `ingestion_pipeline.py`：`_extract_stitchable_body` stitching 時對每 part 重跑 `run_markdown_quality_checks`（performance）
- `insight_agent.py`：`_cross_round_evaluation` 呼叫 LLM 私有 `_get_lang_hint()`（correctness）
- `insight_agent.py`：`_build_targeted_pairs` 以 O(N*M) `_target_match_score` 過濾並重複正規化 title（simplification）
- `counter_agent.py`：`_format_matrix_report` 對 `articles` 以相同邏輯迭代三次（simplification）
- `counter_agent.py`：`_ground_tally_locations` 在未傳入時以全文構造 fallback `_LocationIndex`（architecture）
- `builtin_adapters.py`：source metadata 冗餘 `path` 與 `loaded_chars`/`chars` 欄位（correctness）
- `planner_service.py`：`canonical_planning_patterns`/`format_capability_entry` 硬編已存在於 `_BUILTIN_FACTORIES` 的 adapter 名（simplification）
- `plan_readiness.py`：`_upstream_output_shape_warnings` 跳過不在 `expected_inputs` 的輸入，遮蔽 open-ended adapter 的 mismatch（correctness）
- `pipeline_runner.py`：非 dict adapter 輸出被包成 `{"output": value}`，下游 placeholder 無法觸及 typed key（correctness）
- `cortex_ledger.py`：首次 ledger run 對每頁觸發 un-merge/merge 偵測（無前次 snapshot）（correctness）
- `weekly_memoir.py`：直接 import/呼叫 `load_all_pages`，繞過 cortex_store 抽象（architecture）
- `cortex_consolidation.py`：`_refresh_page_embeddings` 內 `_claim_hash` 每頁呼叫兩次未快取（simplification）
- `core/parser.py`：`run_markdown_quality_checks` 為 trailing-whitespace 偵測 split 文字兩次（performance）
- `core/parser.py`：`strip_body_frontmatter` 每次呼叫 inline 編譯 regex（performance）
- `core/config.py`：`DynamicSettings.reload()` 在 lock 外讀屬性做 log（correctness）
- `trace_store.py`：`usage_to_counts` 對純 dict 輸入 getattr 先回 None（correctness）
- `profile_manager.py`：`migrate_from_doctype` 在 `written==0` 時跳過 reload，但 row 可能因既有檔被略過（correctness）
- `md_block_scanner.py`：`_emit_simple` 與 `_emit_range` 相同，一者為 dead code（simplification）
- `cortex_store.py`：`claim_filename` 第二次碰撞靜默覆寫，僅一層 suffix fallback（correctness）
- `vault_watcher.py`：`PAGES_DIR/NOTES_DIR/CORTEX_DIR` 每事件跨三 helper `absolute()`（performance）
- `maintenance_scheduler.py`：`_latest_full_insight_at` 在 scheduler 啟動時 full directory glob（performance）
- `prompt_watcher.py`：processed-count 增量以「檔案不存在」為門，非以成功處理為門（correctness）
- `vault_watcher.py`：`VaultWatcher` 重實作 `_process_deletion` 已有的 orphan-sweep retry 邏輯（architecture）

---

## 6. 建議的重構批次

依「可獨立 ship、風險相近、避免互相踩 hot path」分為四批，比照專案 batch-N 慣例。

### Batch A — 並行安全 hot fix（風險：中；觸及 hot path 與 watcher 執行緒模型）
> 純 correctness，必須先行，因為這些在正常運行下即破壞狀態。

- `maintenance_scheduler.py:389` — `try_set_busy()` gate（race 搶佔）
- `prompt_watcher.py:81` — 移除 `time.sleep(1)`，processing 移至 worker thread
- `trace_store.py` — `run()` finally 的 UPDATE 包 try/except + log

**風險說明：** 觸及多執行緒 busy-state 與 watchdog dispatch，需在真實 watcher 並行情境驗證；不涉 version-locked 程式。建議搭配並行壓力測試。

### Batch B — 靜默資料遺失修正（風險：中；觸及 ingest/index/parser hot path）
> 修正會悄悄毀損 vault 資料的 bug，獨立於並行修正。

- `ingestion_pipeline.py:403` — 短文標題誤掛 `(Synthesis)`
- `rag_manager.py:424` — 移除 `delete_document(title)` hot path，legacy 改 migration
- `parser.py:431` — 空 label 節點原樣輸出
- `parser.py:56` — frontmatter regex 容許 EOF 無換行
- `profile_manager.py:160` — storage key lowercase

**風險說明：** 直接改 ingest/index/parse 行為，可能影響既有索引內容；建議對既有 vault 做一次 reindex 驗證標題與 frontmatter 不回歸。`rag_manager.py` 改動需確認 doc_id 刪除確實涵蓋自身 chunk。

### Batch C — LLM client / agent correctness + 抽象邊界（風險：中低；部分 version-locked provider 程式）
> 互相鄰近的 client/agent 正確性與 API 整潔。

- `llm_client.py:829` — `translate_tags` 改走 `_complete_json`（**觸及 provider-specific SDK 呼叫，version-locked**）
- `llm_client.py:1565` — `_assess_falsifiability_once` 去除硬編繁中
- `counter_agent.py:337` — 空陣列檢查改整串相等
- `counter_agent.py:250` — RAG fallback 傳 raw text
- `insight_agent.py:1372` — fallback pair 尊重 exclude
- `cortex_decay_pass.py:211` — revalidation 無 sources 時仍寫 timestamp
- `ingestion_pipeline.py:811` + `llm_client.py:1089` — `format_digest_for_prompt` 提升為 public
- `insight_agent.py:1111` 等 — RAG collection 存取改走 public API
- `vault_utils.py:321` — 移除 `update_wiki_index` dead 參數

**風險說明：** `translate_tags` 涉 Gemini/OpenAI SDK 路徑（version-locked），需各 provider 回歸測；其餘為純內部重構，風險低。可與 Batch A/B 並行開發（檔案重疊少，僅 `ingestion_pipeline.py`/`insight_agent.py` 需注意 merge）。

### Batch D — 效能優化（風險：中；最大使用者體感、含並行化）
> 量級最大、最值得做的效能項，獨立於正確性修正後再上。

- **延遲縮減（高優先）：** `builtin_adapters.py:367`、`counter_agent.py:171` — `ThreadPoolExecutor` 並行化 + `LENS_PARALLEL_CHUNKS` flag
- **Cortex nightly I/O：** `cortex_consolidation.py` load_all_pages 共享、state 寫入移出迴圈、cosine 改 batched matmul；`cortex_decay_pass.py:152` 免二次讀
- **單機 I/O 批次/快取：** `rag_manager.py:1069` 批 `$in`、`counter_agent.py:771/279` per-article 快取 + filename index、`insight_agent.py:1433` 平行 expand
- **選做（profiling 佐證後）：** `bm25_index.py:63`、`rag_manager.py:763`、`ingestion_pipeline.py:930/167`、`insight_agent.py:1705`、`trace_store.py` ts index、`embedding_cache.py:37`

**風險說明：** 並行化引入 thread-safety 需求（`ui.set_status` 進度回報、結果按 index 重組以維持 dedup 行為）；建議全部以 config flag 包裹、預設保守並行度以尊重 LLM rate limit。`embedding_cache.py` 若做須用 thread-local 連線（`check_same_thread=False` 即為跨執行緒設）。P19（trace_store ContextVar）建議略過。