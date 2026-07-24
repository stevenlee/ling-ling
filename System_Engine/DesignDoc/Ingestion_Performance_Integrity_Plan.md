# Ingestion 效能與完整性執行計畫

> 範圍：原提案的 1、2、3、6、7。ThoughtfulSplitter（A）暫緩，不在本輪修改。

## 目標與驗收

1. 每次長文 ingest 留下 split、逐 Part distill、stitch、synthesis、index 與總耗時；逐 Part 再區分 entity、digest、commit。只有本次真正執行的 Part 才在完成或失敗時立即顯示耗時，並寫入獨立 `ingestion_part_timing` trace；resume 僅在 `ingestion_run` 記錄 Part 編號與數量，不製造 `0.0s` latency 樣本。整份文件結束後另寫 `ingestion_run` 彙總。
2. 只有所有 Part 都抵達「頁面＋digest 已持久化並完成 RAG 寫入」的 commit point，才允許 stitch、synthesis 與來源歸檔。
3. Part 主輸出與結構化 digest 優先由同一個 LLM response 產生；格式不合格才退回既有 `generate_part_digest`，以保持相容與品質底線。
4. facet indexing 改由既有 idle-time `FacetBackfillPump` 從已落盤的 digest appendix 補做。Learning artifact 是獨立 enrichment pipeline：Part 核心 commit 後立即排入 worker，正文主線繼續下一 Part；完成後以 conflict-safe inline patch 插入同一頁的「知識導航」之前。
5. 生產併發必須先通過相同 request set、零失敗且至少 1.5× speedup 的 backend benchmark。

## 失敗語義

- Entity response 採 typed parse outcome：`valid`、`salvaged`、`invalid`。發現 YAML header 但解析失敗時，不得把 raw response 當正文發布。保守 salvage 只處理 LaTeX backslash、digest flat-list 縮排、獨立全形標點與明確 schema coercion；無法確定語義的引號錯誤必須 reroll。
- Publication gate 的 hard failures 包含 YAML 洩漏、reasoning/prompt 洩漏、空正文、重複字元截斷與未閉合 fence；HTML heading 與損壞 Mermaid ID 屬 semantic suspect。兩類皆在落盤前觸發至多一次 content reroll，transport retry 仍只由 transport 層負責。
- Part 生成、digest 或 commit 任一步失敗：document status=`partial`、`archivable=false`，保留來源供 resume/retry；不得用殘缺 Part 進行 synthesis。無效 entity 不得呼叫 fallback digest，因為 digest 不能把壞正文「補成成功」。
- 新 Part commit 先以原子寫入落 `ingest_status: pending_index`，完成 RAG upsert 後再原子更新為 `complete`；resume 拒絕 pending/unknown status。舊頁不全量重跑，而是在 resume 時通過現行 deterministic gate 才視為 legacy clean。
- `ingest_status: complete` 不等待 learning artifact。頁面先放不可見、內容定址的 artifact slot；artifact worker 只擁有該 slot，不持有正文 snapshot。套用時重讀最新版並做 optimistic hash check、同目錄原子寫入與可恢復備份；slot 外人工編輯原樣保留，slot 被人工修改則標 conflict、生成結果送 `_pending/LearningArtifacts`，不得覆寫。
- Part N 的 artifact 可與 Part N+1 的正文 LLM 呼叫重疊；本機 dispatcher 目前以 bounded worker 實作，保留 `submit/wait` seam 供未來 server-aware job dispatcher 取代。所有 daemon 內 vault commit 仍由 pipeline commit lock 序列化；artifact 是 derivative content，不重做正文 RAG embedding。
- Part artifact jobs 在正文迴圈結束時 fan-in，之後才產生 Stitched，確保 Stitched 帶入已完成的圖。Synthesis 本身也先發布 core，再執行相同 enrichment job。空／不適合視覺化是 `skipped`，產圖失敗或 conflict 不得把正文降成 partial。
- 確定性 entity 失敗以 source、Part、chunk SHA-256、model 與 contract version 內容定址，attempt 在呼叫前持久化，達上限進 quarantine；TTL 僅放行一次 half-open probe。內容/model/contract 改變取得新 budget，provider outage 回退 attempt、不消耗 poison budget。
- Stitch 或 synthesis 失敗：document status=`failed`、`archivable=false`。
- trace 寫入失敗仍為 fail-soft，不反向破壞已完成的文件 commit。
- 舊測試 double 若回傳 `None` 仍視為 legacy success；正式 pipeline 一律回 typed result。

## 稽核與恢復

離線 audit 完全唯讀，會把 Part 分成 `complete`、`legacy_clean`、`needs_reprocess` 與 `unreadable`：

```bash
PYTHONPATH=System_Engine venv/bin/python System_Engine/scripts/audit_ingest_entities.py --fail-on-issues
```

修復時先備份受影響頁面，再以原始 source 重跑；chunk hash 未變且通過 gate 的 Part 會 resume，只有 audit 判定不合格或 `pending_index` 的 Part 重新生成。完成後需再次跑 audit，並重建該文件的 stitched/synthesis；不直接原地改寫既有生成內容。

每次 LLM trace 另記錄 `response_channel`、`finish_reason` 與 content attempt。reasoning fallback 仍可服務 JSON 任務；entity 即使來自 reasoning channel，也必須完整通過 YAML 與 publication contract。

每個 artifact job 另寫 `ingestion_artifact_timing`，包含 generation、apply 與總耗時及 `complete/skipped/conflict/failed`。單 Server 時正文仍為高優先主線、artifact worker bounded；未來多 Server dispatcher 可依 job type、capability、server health 與容量把 core/enrichment 派到不同 backend。

## 併發決策

既有本機單 GPU Ollama 測試約 1.07×，低於 1.5× 門檻，因此目前維持 serial。可用下列 opt-in 指令重測（會產生 `2 × samples` 次小型 LLM 請求）：

```bash
PYTHONPATH=System_Engine venv/bin/python System_Engine/scripts/benchmark_ingest_backend.py --samples 4 --workers 2
```

只有結果 `concurrency_eligible=true` 才進下一個獨立變更：加入 provider-scoped bounded concurrency、保留 trace context，並再次跑內容等價與失敗恢復測試。本輪不預先啟用。

## Learning Artifact 路由與延遲（以前半輪為基準的 A/B）

`Mathematics for Computer Science` 跑至 Part 122/225 後由使用者主動停止 daemon，前半輪保留為 baseline；後半輪在相同文件與 backend 上套用本節修改，作為自然 A/B。已完成頁面與圖不回填、不重算，因此比較時須按 Part 範圍分組，不能把全書聚合值當成同一版本。

前半輪基準：109 個新完成 Part、9 個 resumed、4 個 entity failure；artifact job median 651.6 秒、p90 2564 秒、p95 3165.5 秒、max 5415 秒。Ontology 在 76 個可分類樣本中 33 次排名第一、實際 render 43 次；成功 ontology stage 合計 9.82 小時，失敗呼叫另耗 3.76 小時。這些數據足以確認 retry amplification 與 ontology 補位是系統性問題，不再等待全 225 Parts 才修改。

### 暫定問題定義

- Ontology 的使用條件過寬。現行 `ontology_bias` 會在關係圖難分軒輊時偏向 ontology，且 top-3 的第二、第三名也會實際 render；數學內容只要出現多個可關聯概念，就容易把 ontology 當成一般補位圖。
- Table 的分類描述過窄，只涵蓋傳統的「多對象、多維度比較」，沒有明示定義／成立條件／結論／例子／反例、定理／方法／適用情境、練習題／技巧／目標等高價值查閱結構。
- Artifact 使用全域 `max_output` 與正文 transport policy。OpenAI SDK 內建 `max_retries=2`，外層又有三次 application retry，最壞可放大成九次 300 秒 request；SDK 內層 attempts 目前不會出現在 Ling Ling trace。
- `INGEST_ARTIFACT_MAX_LAG_PARTS=2` 的公平性 backpressure 方向正確，但等待時 UI 沿用上一個 `Distilling Part N`，無法看到實際阻塞它的 Part、artifact type、attempt 與 elapsed time。
- 空產物目前同時代表「內容不適合」、「transport/render 失敗」與「validation reject」。前者應是永久 `skipped`，後兩者應保留內容定址的延後／重試語義，不可混成永久跳過。

### 本輪採用的路由方案

保留單一 classifier 與 mindmap 的既有架構，改的是分類 rubric 與 ranked candidates 的採用政策：

1. 移除 ontology 的預設偏好；`ontology_bias` 預設改為 false。Ontology 只有在 taxonomy/schema 本身就是學習重點，且來源明確具有多種 typed relations（例如 is-a、part-of、instance-of）時才合格。僅有「很多相關概念」不算 ontology。
2. Ontology 原則上只能作為高信心的第一名主視圖，不再作為第二、第三名補位。這是 eligibility gate，不是固定配額；真正適合的內容仍可產生 ontology。
3. 擴張 comparison table 的正當適用範圍：多個定義、方法、定理或練習群，只要共享至少兩個可比較欄位（條件、結論、例子、反例、用途、技巧等），即可選 table。
4. 依認知角色組合產物，而非無條件 render top-3：最多生成兩張，先採主視圖；若 table 在其餘候選中則優先作為互補參考視圖，否則採下一個不同類型。分類器仍可回三個候選，第三名只用於選擇、不直接增加第三次 render。
5. 不以 ontology 百分比作 production hard cap。比例只做 regression alarm；router 仍以內容證據決策。

### 本輪採用的延遲與恢復方案

1. 關閉 SDK 隱藏 retry，由 application layer 成為唯一 retry owner，逐 HTTP attempt 記錄 latency、status 與 error。
2. classifier、table／argument map、Mermaid renderer 分別使用 2,048、4,096、6,144 token 上限；application transport attempts 上限為 2，artifact 的 strict JSON completion 不再額外做 content reroll。OpenAI-compatible SDK 設 `max_retries=0`，避免 3×3 的隱藏放大。
3. 每個 Part artifact job 的 soft wall-clock budget 為 600 秒；每次啟動下一個 renderer 前檢查 budget。已完成的圖正常 inline commit；超出 budget 的剩餘圖回報 `deferred`，不阻塞正文，也不標成永久 `skipped`。單一已發出的 HTTP request 仍由 transport timeout 終止，因此這是跨 stage 的防累加界線，不是假裝能取消進行中的 request。
4. Backpressure 等待時顯示並記錄 `Waiting for Part N learning aids: <type>, attempt x/y, elapsed ...`，另記 queue、generation、backpressure wait 與 apply latency。
5. 未來 server-aware dispatcher 以 core／enrichment priority、模型能力、server health 與容量派工；在只有一台 Server 時仍要保證正文先行、產圖穿插且 bounded lag，不退回「全文正文完成後才產圖」。

### Part 123–141 中途稽核後的修正

- Entity 的 `unclosed_code_fence` 並非模型截斷，而是 fenced YAML 同時輸出 `---` 與 closing fence 時，body cleanup 把 orphan fence 與 Mermaid close 誤當外層 wrapper。修正順序為：YAML extraction 後先做高信心 `strip_orphan_leading_fence`，再做 outer-wrapper cleanup。33 筆真實 response replay 中，10 筆誤判降為 0；真正 YAML quote failure 仍 invalid。entity contract 升至 v3。
- Table renderer 增加 table-only structural publication gate。中途 audit 找到 Part 136、138 兩個 reasoning scratchpad 被誤當 Table；兩個 generated slot 經 section hash 驗證與備份後重設 pending。`audit_ingest_artifacts.py` 可重跑同一稽核。
- Artifact pending slot 現在同時持久化 attempts、failure hash 與 quarantine TTL；確定性失敗達上限停止重試、outage 回退額度、basis 改變取得新 budget。重啟保留 pending，不再誤轉為 preserved。
- 單 Server 使用 request-level cooperative gate：core waiter 優先於 enrichment waiter；artifact 仍在 classifier/render 邊界穿插，bounded-lag backpressure 會讓 core 暫停並給 enrichment 執行機會。
- 實機小型 probe 證實 Ollama OpenAI-compatible endpoint 接受 `reasoning_effort="none"`：相同 256-token JSON prompt 從 reasoning+content 81 tokens 降為純 content 42 tokens。此參數只套用 artifact classifier/render/argument map，不改正文生成。

### 後半輪需收集的決策資料

- 各 artifact type 的 ranked position、選取率、成功／validation reject／transport failure 比例。
- classifier 與各 renderer 的 count、總耗時、p50／p90／p95／max、超過 300／600 秒比例及實際 HTTP attempts。
- 每個 Part 的 core latency、artifact queue／generation／wait latency，以及正文領先距離。
- 人工抽查至少 15 個 Parts：判斷 ontology 是否真為本體結構、table 是否具一致欄位、不同產物是否提供互補而非重複視角。
- 以目前的中途樣本只作假設：ontology 應明顯下降、table 應上升，mindmap 維持；5–15% 等比例不得在完整統計與人工抽查前寫成硬門檻。

後半輪完成或累積足夠樣本後，形成 before／after 報告；是否進一步加入 server-aware dispatch、持久化 artifact deferred worker 或調整數值，再由使用者決定。本輪修改已由使用者在停止 daemon 後明確授權。

## ThoughtfulSplitter 暫緩項

不改邊界演算法、prompt、threshold、chunk metadata 或預設旗標。後續需先建立人工標註 corpus 與 boundary/completeness 指標，再重新提出 A 的設計。
