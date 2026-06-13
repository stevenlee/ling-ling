# System_Engine 程式碼審查與效能稽核報告

## 1. 摘要

本次稽核確認 **24 項** 問題（已通過獨立 skeptic 驗證），另有 33 項低優先未驗證項目供參考。

**已確認問題分布**

| Dimension | High | Medium | 小計 |
|---|---|---|---|
| correctness | 4 | 3 | 7 |
| performance | 4 | 7 | 11 |
| architecture | 0 | 2 | 2 |
| simplification | 0 | 3 | 3 |
| **合計** | **8** | **15** | **23** |

> 註：原始確認清單共 24 筆，其中 `_dereference_facets`、`embedding_cache` 兩項經 skeptic 下修嚴重度（見效能專節）；下表以 skeptic 校正後的 severity 為準呈現。

**最重要的一個結論**：correctness 類的高優先問題（不是效能）才是首要風險——`ingest_to_wiki` 的 `(Synthesis)` 標題誤標（`ingestion_pipeline.py:403`）、`add_document` 的同名標題誤刪（`rag_manager.py:424`）、counter_agent 的 RAG fallback 把 markdown 當原文（`counter_agent.py:250`）與空陣列偵測過寬（`counter_agent.py:337`）會**靜默損毀資料與索引**，影響範圍隨時間累積且不可逆。效能問題雖多，但多數可後修；資料正確性問題應最優先處理。

---

## 2. 🔴 高優先（已確認）

### A. 索引／資料完整性（correctness）— 最優先

**A1. 短文件頁面被誤加 `(Synthesis)` 後綴 — `ingestion_pipeline.py:403`**
- **What**：`title = ... if part_info else f"{base_title} (Synthesis)"`。短文件路徑（`:107`，`part_info=None`）的標題永遠多出 ` (Synthesis)`，頁面寫到 `PAGES_DIR/base_title/"{stem} (Synthesis).md"`（`:421`），RAG 索引與 `update_wiki_index` 都記錄錯名。
- **Why**：與 `_write_synthesis` 為同一 stem 產生的真正 synthesis 文件衝突，且**每一個短文件頁面都被永久誤標**。
- **Fix**：改為三分支，`else base_title`；`(Synthesis)` 命名只保留在 `_write_synthesis` 內。`_write_synthesis` 目前不經 `ingest_to_wiki`，移除 else 後綴不會回歸真正的 synthesis 頁面。

**A2. `add_document` 對每次寫入都 `delete_document(title)`，會誤刪同名文件 — `rag_manager.py:424-425`**
- **What**：依 `doc_id` 刪除後（`:424`）又無條件 `delete_document(title)`，後者執行 `collection.delete(where={'title': title})`（`:1176`）。
- **Why**：兩篇同名（如 `Introduction`/`Overview`）的筆記中，重建其一會**靜默刪掉另一篇的 chunks**。
- **Fix**：移除 hot path 上的 `delete_document(title)`；`doc_id` 刪除已涵蓋自身 chunks。legacy 清理改為一次性 migration，僅針對缺 `doc_id` 的列：`where={"$and":[{"title":title},{"doc_id":{"$exists":False}}]}`。注意原建議的 `{'doc_id':{'$ne':doc_id}}` 仍會誤刪有 doc_id 的兄弟文件，務必改用「不存在 doc_id」條件。

### B. Lens / Counter 萃取正確性（correctness）

**B1. 空陣列偵測使用去空白字串比對，遮蔽 parse 失敗 — `counter_agent.py:337`**
- **What**：`if instances or "[]" in _WS_RE.sub("", raw):`。去除所有空白後做 substring 比對，`{"key":[]}`、markdown 表格、code block 等任何含連續 `[]` 的回覆都會 match 並抑制 retry。
- **Why**：真正的 JSON parse 失敗被誤判為「LLM 宣告空集合」，retry 被吃掉，結果靜默為空。
- **Fix**：要求**整段**去空白後等於 `[]`：`_ws_collapsed = _WS_RE.sub("", raw); if instances or _ws_collapsed == "[]":`。可接受 `[ ]`，排除 `{"key":[]}`／markdown 誤判。若模型常以 ```json fence 包裹，先剝除 fence 再比對。原建議的 `re.search(r'\[\s*\]', raw)` 無法修掉巢狀 `{"x":[]}`，不採用。

**B2. RAG fallback 把格式化 markdown 當 `article_text` — `counter_agent.py:250`**
- **What**：`results.append(("(RAG result)", rag_results[0], ""))`。`query_similar_notes` 回傳的是已格式化 markdown（`### [來自筆記: {title}]\n{doc}`），卻被當原文餵進 `TextSplitter.split_text` 與萃取 prompt。
- **Why**：注入的 `### ` heading 會污染 grounding 錨點與抽出的 quotes；且 `top_k=1` 不論文章長度只取單一 chunk。
- **Fix**：改用 dict API：`rag_results = self.rag.query_notes(user_directive, top_k=1)`，append `rag_results[0]["text"]`，title 取 `metadata.get("title")` 取代字面 `"(RAG result)"`。或在 append 前剝除注入的 header line。可考慮提高 `top_k` 或 chunk-join 避免多 chunk 文章被截斷。

### C. 高影響效能（performance）— 詳見第 4 節

**C1. `digest_sources` 每個 source 串行一次 LLM 呼叫 — `builtin_adapters.py:358-385`**
- 以 `ThreadPoolExecutor` 並行各 section 的 digest，依原序收集；保留每 source 的 try/except；延遲由「總和」降為「最慢單一 source」。Adapter contract 是 `Callable[[dict],dict]`，外層無法並行化，**必須在內部做**。

**C2. counter_agent 逐 chunk 串行 LLM — `counter_agent.py:171`**
- `_run_single_count` 對每 chunk 串行 `answer_query`，50-chunk 文章光萃取就 100–200s。用 `ThreadPoolExecutor`（`max_workers≈4`）並行，依 chunk index 重組以維持 dedup 行為，旗標 `LENS_PARALLEL_CHUNKS` 可關閉。matrix 路徑（articles × concepts）效益加乘。`:175-190` 的逐 chunk 進度回報需調整。

**C3. `_expand_seed` 逐 winner 串行 embedding+RAG+LLM — `insight_agent.py:1433`**
- 在 `_expand_winners`（`:1095-1100`）以 `ThreadPoolExecutor` 並行各獨立的 expansion，注意維持輸出排序與 `ui.set_status` thread-safety。**捨棄** prompt hoist 建議——已命中 `_PROMPT_CACHE`，無重複磁碟讀取。

> 註：原列為 high 的 `_resolve_target_doc` N+1（`insight_agent.py:1287`）、`_original_source_title` I/O（`counter_agent.py:771`）兩項，經 skeptic 校正為 low/medium，移至效能專節說明。

---

## 3. 🟡 中優先（已確認，依主題分組）

### 效能（performance）

- **`rag_manager.py:1069` `_dereference_facets` 無批次／memoize**（skeptic 下修為 medium）：對每個 facet hit 呼叫 `_first_chunk_of_doc`，後者以 `collection.get(where={'doc_id':...})` 抓全文。8 個 facet 指向 3 個 parent 即 8 次往返，5 次重複。Fix：先收集 unique `doc_id`，單次 `where={'doc_id':{'$in':unique_ids}}` 批抓並 group；至少在呼叫內以 local dict memoize 消除重複往返。
- **`rag_manager.py:763` MMR 路徑重複 embed query**：`diversity>0` 時 `self.ef([query_text])[0]` 重算 query 向量。因 `CachedEmbeddingFunction` 預設開啟，通常是 SQLite cache hit 而非網路往返。Fix：`need_embeddings` 時預先算一次 `q_emb`，以 `query_embeddings=[q_emb]` 傳入 `collection.query`（`:606`）並於 `:765` 重用。cache 開啟時現況已近最佳，屬低/選用優先。
- **`bm25_index.py:63` rebuild 全量載入文本**：`_build()` 以 `collection.get(include=['documents'])` 無上限拉全部文本。Fix：增量維護 `_corpus_tokens`/`_chunk_ids`，add/delete 時 patch；`BM25Okapi` 仍須重建但省下全量 fetch 與重 tokenize。已有 lazy coalescing，建議僅在 10k+ chunk vault profiling 顯示明確成本時才做。
- **`ingestion_pipeline.py:930` part file 寫後立即讀回**：`:422` 寫入後 `_append_part_digest_to_note`（`:930`）又讀回再重寫，N-part 文件 2N 次 I/O。Fix：讓 `ingest_to_wiki` 回傳組好的 body，於 `_process_parts` append digest 後單次 `write_text`。**勿**採「把 digest 傳入 `ingest_to_wiki`」變體——digest 在該呼叫回傳後才算出。屬低影響。
- **`ingestion_pipeline.py:167` 短文件多一次 LLM 呼叫填 facet index**：`_index_short_doc_facets` 無條件 `generate_part_digest`，疊在既有 `generate_entity_page` 上，高量短文件 ingestion 成本翻倍。Fix（首選）：擴充 entity-page template 回傳結構化 `thesis`/`key_points`，餵 `_facets_from_digest` 零額外呼叫；否則須改 template，純欄位重用在現有 template 下會幾乎都落到 fallback。
- **`insight_agent.py:1705` `_get_tag_cluster_context` 每 chunk 解析 tags 兩次**：`:1694-1698` 與 `:1705-1708` 對同一 metadatas 各 parse 一次，10k chunk 即 20k 次。Fix：`parsed = [self._parse_stored_tags(m.get("tags","")) for m in ...]` 解析一次重用。僅在 `target_tag is None` 路徑發生，屬低嚴重度 cleanup。

### 正確性（correctness）

- **`llm_client.py:1565` `_assess_falsifiability_once` 硬編 Traditional Chinese**：prompt 不論 `settings.OUTPUT_LANGUAGE` 都產 `falsifier_zh`（繁中），Simplified/Japanese 部署會靜默產生錯誤 gloss（`:1589` 接到英文 falsifier 後）。Fix：(a) 改用 `self._get_lang_hint()` 並改名 `falsifier_localized`，當語言為英文時不加 gloss；或 (b) 從 JSON contract 移除該欄、由 render 層在地化。
- **`insight_agent.py:1372` `_build_targeted_pairs` fallback 忽略 exclude set**：`other_docs` 與 `pairs` 皆空時 append `(target, random.choice(all_docs))` 未檢查 exclude，小 vault 會回傳已探索過的 pair，破壞跨輪 dedup。Fix：以同樣 idiom 檢查 exclude，且從 `other_docs`（或排除 self 的 pool）取樣；若小 vault 確實無新 pair，回傳空可接受（caller `:1043` 已視空為停止條件）。

### 架構（architecture）

- **`ingestion_pipeline.py:811` 跨抽象邊界呼叫 LLM client 私有方法**：`self.llm._format_part_digest_for_prompt(d)`。Fix：將既有 `@staticmethod _format_part_digest_for_prompt` 改名為公開 `LLMClient.format_digest_for_prompt`（已是 static、無 self 依賴），更新三處呼叫：`llm_client.py:1089`、`ingestion_pipeline.py:811`、`scratch/bench_synthesis_ab.py:42`。**勿**把邏輯複製進 pipeline（synthesis 與 critique 須格式一致）。
- **`insight_agent.py:1111` 直接存取 `rag.collection`，繞過 RAG 服務層**：此 pattern 重複於 `:1202,:1670,:1684,:1729`。Fix：在 `RagManager` 新增公開方法（沿用既有命名風格，如 `get_all_metadata()`、`get_chunks_by_title()`、`get_all_documents_with_metadata()`），讓 InsightAgent 不再依賴 ChromaDB result-dict 形狀。

### 簡化（simplification）

- **`insight_agent.py:140` Signals 序列化區塊在兩處逐字重複**：`generate_insight`（`:140-150`）與 `generate_full_insight`（`:229-241`）byte-for-byte 相同。Fix：抽 `_compute_signals_meta(self, content, target_titles) -> dict`（含 flag 檢查），兩處呼叫 `.update(...)`。
- **`insight_agent.py:1048` pair-key 構造重複五次**：`tuple(sorted([x["title"], y["title"]]))` 出現於 `:1048,:1233,:1348,:1361,:1370`。Fix：加 `@staticmethod _pair_key(a,b)`，五處全部改用。

---

## 4. ⚡ 效能專節（所有 performance 確認項彙整）

依「影響 ÷ 成本」與相依性建議的處理順序：

| # | 位置 | 問題 | 粗估影響 | 校正 severity |
|---|---|---|---|---|
| P1 | `counter_agent.py:171` | 逐 chunk 串行 LLM | **極高**：50-chunk 文章萃取 100–200s → 並行後約降至 ~1/4（max_workers=4），matrix 路徑加乘 | high |
| P2 | `builtin_adapters.py:358` | `digest_sources` 串行 LLM | **高**：延遲從 N 個 source 總和降為最慢單一 source | high |
| P3 | `insight_agent.py:1433` | `_expand_seed` 串行 embed+RAG+LLM | **高**：9×(embed+search+LLM) → 並行後接近單回合延遲 | high |
| P4 | `rag_manager.py:1069` | facet deref 無批次 | 中：8→1 次 ChromaDB 往返（受 candidate pool 上限約束） | medium |
| P5 | `insight_agent.py:1287` | `_resolve_target_doc` 重複 `.get()` | 低：受 4 個分數階梯約束，最多 3 次浪費（通常 0–1） | low（原列 high） |
| P6 | `counter_agent.py:771` | `_original_source_title` 反覆 FS I/O | 低：每 report 一次的 stat/glob，per-instance 重解析 | low（原列 high） |
| P7 | `counter_agent.py:279` | 每次 title-miss 全 vault `rglob` | 中：O(M×N)，大 vault 數萬 stat → 建一次 filename→path index 降為 O(1) | medium |
| P8 | `ingestion_pipeline.py:167` | 短文件多一次 digest LLM | 中：高量 ingestion 成本翻倍（需 template 改動才能根治） | medium |
| P9 | `rag_manager.py:763` | MMR 重複 embed query | 低：cache 開啟時僅省一次 SQLite lookup+sha256 | medium→low |
| P10 | `bm25_index.py:63` | rebuild 全量 fetch | 中：O(N) IPC fetch + 重 tokenize（已有 lazy coalescing，profiling 驅動） | medium |
| P11 | `ingestion_pipeline.py:930` | part file 寫後讀回 | 低：剛寫入、cache-resident 小檔，被 per-part LLM 延遲掩蓋 | medium→low |
| P12 | `insight_agent.py:1705` | tags 解析兩次 | 低：cheap string split，被全量 fetch 掩蓋 | medium→low |

**建議順序**：先攻 **P1→P2→P3**（三個串行 LLM fan-out，user-visible 延遲降幅最大，互相獨立可平行開工，各自旗標保護）。其次 **P4、P7**（ChromaDB / FS 往返收斂，中等成本中等收益）。**P5、P6、P9、P11、P12** 為廉價 cleanup，可併入相鄰批次順手處理。**P8、P10** 屬 profiling-driven，需 template/索引結構改動，建議排在有量測數據後再做。

---

## 5. 🟢 低優先（未驗證，僅供留意）

- `llm_client.py` — `generate_synthesis` 重複 `_build_system_prompt` 的 template 解析（simplification）
- `llm_client.py` — `classify_document` 是 `select_profile` 的弱化子集，可移除／統一（architecture）
- `llm_client.py` — 多餘的 `max_tokens=None` 呼叫點（simplification）
- `rag_manager.py` — `add_document` 的 rel_path 解析重複 `_get_doc_id` 的 try/except（simplification）
- `rag_manager.py` — 無 embeddings 時 `_mmr_select` 靜默退化為 top-k slice（correctness）
- `rag_manager.py` — `_first_chunk_of_doc` 與 collection internals 緊耦合（architecture）
- `rag_manager.py` — BM25 where-filter 多一次 `collection.get` 往返驗證（performance）
- `ingestion_pipeline.py` — `_extract_stitchable_body` 的 Path-reading 分支為 dead code（simplification）
- `thoughtful_splitter.py` — `_section_path_at` linear scan 提早 break 漏掉後續 heading（correctness）
- `ingestion_pipeline.py` — `_extract_stitchable_body` 重跑 `run_markdown_quality_checks`（performance）
- `insight_agent.py` — `_cross_round_evaluation` 呼叫 `llm._get_lang_hint()` 私有方法（correctness）
- `insight_agent.py` — `_build_targeted_pairs` 以 O(N*M) `_target_match_score` 反覆正規化標題（simplification）
- `counter_agent.py` — `_format_matrix_report` 以相同邏輯迭代 `articles` 三次（simplification）
- `counter_agent.py` — `_ground_tally_locations` 以全文建 fallback `_LocationIndex`（architecture）
- `builtin_adapters.py` — source metadata dict 中 `path` 與 `loaded_chars`/`chars` 多餘欄位（correctness）
- `planner_service.py` — `canonical_planning_patterns`／`format_capability_entry` 硬編已存於 `_BUILTIN_FACTORIES` 的 adapter 名（simplification）
- `plan_readiness.py` — `_upstream_output_shape_warnings` 跳過不在 `expected_inputs` 的 input，遮蔽不符（correctness）
- `pipeline_runner.py` — 非 dict adapter output 包成 `{"output": value}`，使 typed output key 在下游 placeholder 不可達（correctness）
- `cortex_ledger.py` — 首次 ledger run 對每頁誤觸 un-merge/merge 偵測（correctness）
- `weekly_memoir.py` — 直接 import `load_all_pages`，繞過 `cortex_store` 抽象（architecture）
- `cortex_consolidation.py` — `_claim_hash` 每頁呼叫兩次未快取（simplification）
- `core/parser.py` — `run_markdown_quality_checks` 為 trailing-whitespace 偵測 split 兩次（performance）
- `core/parser.py` — `strip_body_frontmatter` 每次呼叫 inline 編譯 regex（performance）
- `core/config.py` — `DynamicSettings.reload()` 於 lock 外讀屬性記 log（correctness）
- `trace_store.py` — `usage_to_counts` 對 dict 輸入 getattr 先回 None（correctness）
- `profile_manager.py` — `migrate_from_doctype` 在 `written==0` 時跳過 reload，但可能有 row 因檔案已存在被略過（correctness）
- `md_block_scanner.py` — `_emit_simple` 與 `_emit_range` 相同，一為 dead code（simplification）
- `cortex_store.py` — `claim_filename` 第二次碰撞靜默覆寫，僅一級 suffix fallback（correctness）
- `vault_watcher.py` — `PAGES_DIR`/`NOTES_DIR`/`CORTEX_DIR` 每事件三處 `absolute()`（performance）
- `maintenance_scheduler.py` — `_latest_full_insight_at` 啟動時全 dir glob（performance）
- `prompt_watcher.py` — processed-count 以檔案不存在為條件而非處理成功（correctness）
- `vault_watcher.py` — `VaultWatcher` 重實作 `_process_deletion` 的 orphan-sweep retry（architecture）

---

## 6. 建議的重構批次

依「可獨立出貨、相依性、是否觸及 hot path / version-locked」分組。

### batch-A：資料完整性修復（correctness，最優先）
- `ingestion_pipeline.py:403`（A1 短文件標題誤標）
- `rag_manager.py:424`（A2 同名文件誤刪）
- `counter_agent.py:337`（B1 空陣列偵測）
- `counter_agent.py:250`（B2 RAG fallback 原文）
- **風險：中–高**。**觸及 hot path**（ingestion 與 RAG add/delete 是核心寫入路徑），且改動會影響**既有索引內容與頁面命名**——須附 migration 或 backfill 計畫，並驗證不回歸真正的 `_write_synthesis` 頁面。建議單獨成批、最高測試覆蓋。

### batch-B：LLM fan-out 並行化（performance，user-visible 延遲）
- `counter_agent.py:171`（P1）、`builtin_adapters.py:358`（P2）、`insight_agent.py:1433`（P3）
- **風險：中**。引入 `ThreadPoolExecutor`，須保證輸出排序與 dedup 不變、`ui.set_status` thread-safe，並各以 config flag（`LENS_PARALLEL_CHUNKS` 等）保護可回退。**觸及 LLM 呼叫的 hot path**，須留意 provider rate limit。三項彼此獨立，可同批但分 commit。

### batch-C：ChromaDB / FS 存取收斂 + 架構整理（performance + architecture）
- `rag_manager.py:1069`（P4 facet 批次）、`counter_agent.py:279`（P7 vault index）、`rag_manager.py:763`（P9）
- `insight_agent.py:1111`（架構：RagManager 公開方法封裝 `collection.get`）
- `ingestion_pipeline.py:811`（架構：`format_digest_for_prompt` 公開化）
- **風險：低–中**。多為非破壞性封裝與快取。`format_digest_for_prompt` 改名需同步 `scratch/bench_synthesis_ab.py:42`。不觸及 version-locked 行為，可漸進出貨。

### batch-D：純清理（simplification + 廉價 correctness/perf）
- `insight_agent.py:140`（signals helper）、`:1048`（pair-key helper）、`:1705`（P12 雙重 parse）、`:1372`（fallback exclude）
- `llm_client.py:1565`（多語 falsifier）
- 廉價 perf：`insight_agent.py:1287`（P5）、`counter_agent.py:771`（P6）、`ingestion_pipeline.py:930`（P11）
- **風險：低**。多為局部、無行為變更（或行為更正確）的重構。`llm_client.py:1565` 須確認 `OUTPUT_LANGUAGE=英文` 時不加 gloss 的分支。可機會性併入相鄰 PR。

> `ingestion_pipeline.py:167`（P8 多一次 digest LLM）與 `bm25_index.py:63`（P10 全量 rebuild）**不納入上述任一批次**：兩者需 template／索引結構變更且應 profiling-driven，建議待量測數據後另立 batch。