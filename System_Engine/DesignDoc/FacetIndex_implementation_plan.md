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
- 品質：上線前後各跑一次 `maintenance/retrieval_bench.py`（bench cases 在
  `scratch/retrieval_bench.yml`），分數沒上升就把 flag 關回去。
