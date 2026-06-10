# Facet Index — Summary-as-Pointer Retrieval

> Status: **Phase A + B landed 2026-06-10**. Phase C (cross-document topic
> layer, RAPTOR-style) deferred until bench data justifies it.

## Problem

三個檢索痛點：

1. **Query–chunk 語意落差**：query 是問句/概念，chunk 是敘述散文，向量
   空間天然不對齊。
2. **跨語言落差**：中文 query 對英文內文。
3. **Chunk 污染**：wiki 頁面帶導航區、digest appendix、metadata，稀釋語意。

## Design

LLM digest（`thesis` + `key_points`）embed 成 **facet** 條目，與 chunks
同一 collection：

```
id:   {doc_id}_facet_{sha256(text)[:16]}     # 內容定址，重跑冪等
meta: role="facet", doc_id=<母文件>, title, source, facet_index, timestamp
text: 一句 thesis 或 key_point（這是被 embed 的內容）
```

**核心不變量：facet 是指標不是內容。** `query_notes` 在 vector+BM25 候選
池成形後、rerank **之前**呼叫 `_dereference_facets()`：

- facet 命中 → 換成母文件的第一個真實 chunk（`_first_chunk_of_doc`，
  以 start_offset 最小者為準，python-side 過濾 role）
- 母 chunk 已在池中 → 保留排名較高者，不重複
- 母文件消失（dangling）→ 丟棄
- 命中訊號（vector_rank 等）轉掛到母 chunk id，trace breakdown 不斷鏈，
  `passed_layers` 加 `facet_deref`，結果帶 `matched_facet` 欄位

放在 rerank 之前的理由：cross-encoder 應評分真實內文而非摘要；MMR 的
embedding 也來自母 chunk（need_embeddings 時 parent fetch 帶 embeddings）。

## Phases

- **Phase A**：長文 `_process_parts` 迴圈內，每個 part 的既有 digest 經
  `_facets_from_digest()`（thesis + key_points，去重、≥8 字、上限
  `FACET_MAX_PER_DOC`）→ `rag.add_facets()`。零新增 LLM 成本。
- **Phase B**：短文 `ingest_markdown` 成功後補一次
  `generate_part_digest(title, 1, 1, raw, generated, "")`，同樣轉 facets。
  每篇 +1 次輕量 LLM call。Fail-soft：digest 失敗只 warning，不影響 ingest。
- **Phase C（未做）**：跨文件主題層摘要。先用 retrieval bench 驗證 A/B。

## Safety / lifecycle

- facet 共用母文件 doc_id → `delete_document`（by doc_id）、orphan sweep
  自動涵蓋，無需新清理路徑。
- `add_facets` 先刪該 doc 的舊 facets 再 upsert（重 ingest 冪等）。
- `_get_existing_content_hash` 改為掃前 10 筆取第一個有 content_hash 的
  chunk——facet（無 hash）排在前面時不會打穿 unchanged-content 短路。
- 幻覺隔離：摘要錯最多是「多撈/漏撈一個 chunk」，餵給 LLM 的永遠是原文。
- 開關：`FACET_INDEX_ENABLED`（預設 true）、`FACET_MAX_PER_DOC`（預設 8）。

## Verification

- 單元：`tests/test_facet_index.py`（add/replace/dereference/dedup/
  dangling/content-hash 防護）、`tests/test_dynamic_pipeline.py`
  （Phase A 每 part 一批、Phase B 短文一批、flag off 全關）。
- 品質：由下述自我改進迴路持續驗證，不靠一次性人工對比。

## Self-improving bench loop（2026-06-10 同日落地）

驗證不該是一次性的——迴路設計成隨 vault 自動進步：

1. **評測集自動生長**（`maintenance/bench_builder.py`，週任務）：
   每篇有 facet 的未覆蓋頁面，由 `llm.generate_bench_question()` 把 thesis
   改寫成自然問句（禁止逐字抄 thesis——抄了等於測 facet 自己）。候選 case
   必須通過品質閘門：**當下系統答得對才收錄**。哲學是 regression guard：
   auto case 鎖定今天可用的能力，未來任何改動讓它失敗＝退步。
   寫入獨立的 `scratch/retrieval_bench_auto.yml`（原子寫入，手寫檔與其
   註解永不被改寫；auto 檔可隨時整檔刪除重來）。上限
   `BENCH_AUTO_MAX_CASES`（30）、每輪 `BENCH_AUTO_PER_RUN`（5）。
2. **Facet A/B**：每日 bench 每條 case 跑兩次——`use_facets` 預設 vs
   `False`（後者把 facet 命中直接丟棄，不解參照）——回報 facet lift。
   lift 持續為負時，告警會建議關閉 `FACET_INDEX_ENABLED`。
3. **歷史與退步告警**：每次 bench 結果 append 進
   `Database/bench_history.json`（保留 365 筆，原子寫入）。pass rate 低於
   上一次 → status 升級為 `regressed` 並寫 `fromLingLing/` 告警，列出
   失敗查詢與嫌疑變更方向。

對應測試：`tests/test_bench_loop.py`。

## Idle backfill pump（2026-06-10 同日落地）

既有頁面的 facet 由 `maintenance/facet_backfill.py` 在閒置時回填，設計
不變量：

1. **低優先權的實作 = busy lock + 步驟夠小**：一步一頁，鎖只在步內持
   有；釋放時 BusyState 的 idle-callback 排空機制讓排隊的使用者工作先
   跑（泵的 callback 最後註冊）。idle callback 本體絕不做 LLM 工作——
   它在鎖仍被持有時執行，只負責排 grace timer。
2. **kick(replace=False) 防節奏拉長**：步驟結束釋放鎖會觸發 on_idle，
   若無此防護，30 秒的步間隔每次都會被拉回 180 秒 grace。
3. **佇列從 DB 推導**：候選 =（pages/+Notes/ 檔案）−（有 facet 的
   title）− 排除集（Stitched、底線、<400 bytes）− 隔離區。每日重建，
   自我修復、永不漂移。優先序 Synthesis → 近期被檢索 → 一般 → Part。
4. **Part 頁零成本**：解析頁尾既有 Digest Appendix（thesis/key points
   regex），只有無 appendix 的頁面花一次 `generate_part_digest(1/1)`。
5. **失敗分流**：單頁失敗 3 次 → 隔離（mtime 變更解除）；連續 3 個
   「不同頁面」失敗 → 視為 provider 中斷，全域退避 1h。
6. **餓死防護**：收件匣讓路只看「新鮮」檔案（mtime < 10 分鐘）——
   Consolidate 裡卡住的壞檔案不得永久阻擋回填。
7. **預算**：每日 LLM call 上限（預設 1000，本地模型），耗盡排程到
   隔日午夜後恢復；appendix 解析不計費。
8. **完成即靜默**：佇列空 → 不搶鎖不排 timer，一次性通知後安靜。

對應測試：`tests/test_facet_backfill.py`。
