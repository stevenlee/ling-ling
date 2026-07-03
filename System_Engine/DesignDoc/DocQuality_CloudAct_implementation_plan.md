# DocQuality (cloud_act 審查) — Implementation Plan

> 依據：對 `lings-desktop/pages/cloud_act/` 全部 8 個生成檔（Part 1–6、Stitched、Synthesis）的逐檔審查，
> 加上 40 個 Mermaid 區塊的實際 parse 驗證（mermaid v11 + jsdom，5/40 失敗），
> 以及對 pipeline 原始碼的根因追蹤。撰於 2026-07-03。
>
> **進度（2026-07-03）**：止血批次 **P1（1.1–1.4）+ 3.1/3.2 已完成並通過端到端驗證**
> （commits 3d0316b、6e6883d；daemon 已用新碼重啟並完整重跑 cloud_act）。
> 重跑實測：**mermaid 0/42 parse fail**（基線 5/40）；critique 判 revise 時 Synthesis
> 正確標 `#NeedsReview` + 檔頂 🔔 警示 callout + `quality_verdict` frontmatter；
> Part 3/5 因 LLM 瞬時逾時走 fallback digest，正確標 `digest_degraded: true` 且未進
> facet index（Part 1/2/4/6 正常索引 6 facets）。追加 hardening：gemma 又出現
> `**總體評定：**` 判定變體 → parser 已泛化（6e6883d）。
> 其餘：P2（source_prep 行號表格）、P3.3–3.6、P4、P5 未動工——重跑輸出中行號假表格
> 仍在（預期內，P2 未做）。

---

## 一、審查發現（依嚴重度）

### A. 品質閘門失效 — critique 判 Reject 卻照樣發佈 #PerfectPitch（critical）

Synthesis 內含兩個 critical 事實錯誤，critique 自己都抓到了：

- 「於通知國會 **1180 天**後生效」— 來源是 180 天（Synthesis 第 186 行附近，critique 已列出）
- 「**向國際證明**」— 來源是「向國會提交書面證明」（§2.2）

critique 總體判定為「**拒絕 (Reject)**」，但檔案仍以 `status: '#PerfectPitch'` 發佈，錯誤原文未修。根因鏈（三段，各自都要修）：

1. **verdict 解析失敗**：`services/ingest/critique_loop.py` 的 `_VERDICT_RE` 只匹配
   `Overall Verdict: ...` 單行格式；gemma 實際輸出是
   `### 總體判定` + 下一行 `**拒絕 (Reject)**`。跨行 → `parse_verdict` 回 `None` → 不觸發 retry
   （frontmatter 無 `quality_verdict` 欄位可佐證 verdict=None）。
2. **status 硬編碼**：`services/ingestion_pipeline.py:619,627` 無條件寫 `#PerfectPitch`，
   與 verdict 完全脫鉤。
3. **critique 結果不回寫**：即使 verdict 解析成功，retry 也只是「整篇重生成」；
   最終版仍可能帶著已知錯誤發佈，且讀者看到的是「本文說 1180 天、文末 critique 說錯了應為 180 天」
   的自相矛盾文件。

### B. 來源預處理缺口 — OCR 行號假表格（critical，是「表格錯誤」的總根因）

原始檔 `lings-desktop/raw/consolidate/cloud_act.md`（PDF→markdown 轉換產物）把美國國會法案的
**頁邊行號**轉成了兩欄表格（`| 2 | SEC. 101. SHORT TITLE. |`），全檔 671 行表格列，並帶有：

- 行中斷字：`con | tents`、`dis | closing`
- `<br>` 塞縫：`legal<br>obligations<br>when<br>chapter`
- 跳號、空列（`| 25 | |`）

翻譯模型忠實保留 → Part 1/3/4/5/6 內文充滿假表格、Part 4 出現孤兒列、Part 5 出現
「（下略）」截斷、Part 6 整段條文被切成 7 張行號表。這不是翻譯層能修的，必須在 chunking 前把源頭攤平。

### C. Mermaid 語法錯誤（實測數據）

40 個區塊實際 parse：**5 個失敗，全部是同一根因**：

| 檔案 | 位置 | 根因 |
|---|---|---|
| Part 1 :231 / Part 5 :198 / Synthesis :238 / Stitched :250、:952 | classDiagram (ontology) | `<<instance>> X` 套在**未宣告**的 class 上，mermaid v11 直接 crash（`Cannot read properties of undefined (reading 'annotations')`）。已用最小案例隔離證實：先 `class X` 宣告即可通過。 |

`services/learning_artifacts.py` 的 ontology prompt（~L122）示範了 `<<instance>> Fido` 寫法，
但**沒有要求先宣告 class** → 模型每次都產出必掛的圖。ontology 是被 ONTOLOGY_BIAS 偏好的圖型，
命中率高，等於每篇都埋雷。

parse 通過但**渲染退化**的（會顯示成原始 LaTeX 字串）：

- stateDiagram-v2 標籤內嵌 `$$\text{...}$$`、`$\S 2705("a")(2)$`（Part 2 :224、Part 6 :335、Synthesis :317）
- timeline 事件內嵌 `$$\rightarrow$$`（Part 5 :179）
- mindmap 的 math degrade 已有（f7f3a47），但 stateDiagram / timeline 沒有覆蓋
- sequenceDiagram `rect rgb("240, 240, 240")`（引號包住 rgb 參數，顏色失效）
- Synthesis 的 `quoted_mermaid_labels` 修復 pass 把 `"美國政府" --> "簽署行政協議"` 修成
  **剝引號直接當 node id**，與既定原則（id 純 ASCII、CJK 只放 label，見 mermaid id canonicalization）
  相反；`_synthesize_node_id` 還產出純數字 id（`5["每 5 年定期審查"]`）
- Part 6 stateDiagram 有孤兒錯字狀態 `HouseDelary_Process`（模型 typo，parse 過但圖不對）

### D. 譯文字元污染與數字失真（major，可被 deterministic lint 攔截）

- `訴ทาง訴訟` — **泰文字元**混入（Part 3 :103）
- `法​法案` — U+200B 零寬空格 + 重複「法」（Part 1 摘要，並擴散到 digest、Stitched、Synthesis）
- `19 78 年`（Part 6 frontmatter highlight）、`(s)(B)`（Part 6 修訂條款，應為 `(4)(B)`）
- 錯字：`撤算`（Part 1 :99）、`合格外務政府`（Part 1 詞彙表）、`協議條組件`（Part 4 :116）、
  `眾議案之決議案`（Part 6）
- 條號錯誤：`第 312 條` 應為第 3124 條（Part 3 :94）
- **1180 天**（Synthesis）— 上述 A 的實例，數字保真檢查同樣能攔

### E. 知識萃取品質 — Part 1 digest 是 silent fallback 垃圾（major）

Part 1 的 `part_digest.key_points` 是：`摘要`、`翻譯內文`、`第五部分—CLOUD 法案`——章節標題，不是重點；
`handoff` 被塞成 pending_concepts 清單（YAML anchor `*id001` 可證）。根因：
`services/llm_client.py::_part_digest_fallback` 在結構化 digest LLM 呼叫失敗時，
**拿 note 前 6 行充當 thesis/key_points**，且不留任何 degraded 標記。下游影響：
facet index 收到「摘要」「翻譯內文」這種假 facets；synthesis 的 Part 1 依據是空殼。

（Part 2–6 的 digest 是正常結構化輸出，品質尚可——thesis/evidence/open_questions 都有內容。）

### F. 閱讀效率問題（minor–major）

1. **Synthesis 雙層殼**：`## 📝 Executive Summary` 之下模型又輸出了一份完整 H1 報告，
   含幻覺元資料（`日期: 2024-05-22`、`作者: 文件的架構師`）——universal-document-template
   沒有禁止模型自產元資料區；標題層級倒置（section 內出現 H1）。
2. **重複內容**：part_digest 同時存在於 frontmatter 與 `## 🧩 Part Digest Appendix`；
   Synthesis frontmatter 有 40 行 trace_ids；Stitched 內 6 份詞彙表大量重疊
   （Executive Agreement 出現 6 次、每次譯註略異）。
3. **空標題區**：Part 1/3 `## 🖼️ 學習輔助（argument_map）` 之後直接又是 `## 🧩 論證結構（Toulmin）`
   雙標題（artifact 注入時 heading 沒有與 artifact 自帶 heading 合併）。
4. **巨型表格濫用**：Part 5 把法條 (4)–(7) 全部塞進一張表（空白儲存格模擬 rowspan + `<br>` 段落）
   ——不可讀也不可檢索；這是 B 的下游症狀，源頭修好後應由模板明確要求「法條逐項用巢狀清單，禁用表格」。

---

## 二、實施計劃

原則：行為開關一律走 Scripture DynamicSettings（不進 .env）；重跑 ingest 一律 touch vault 檔
讓 daemon VaultWatcher 處理（不跑 standalone reindexer）。

### Phase 1 — 品質閘門修復（最優先：防再犯，工作量小）

| # | 變更 | 檔案 |
|---|---|---|
| 1.1 | `parse_verdict` 支援中文跨行格式：匹配 `^#{2,4}\s*總體判定` 區段後第一個關鍵詞，以及 `**拒絕 (Reject)**` 行內 `(keep\|revise\|reject)` 括號註記；用 cloud_act 這次的實際 critique 文字做 regression fixture | `services/ingest/critique_loop.py`、`tests/` |
| 1.2 | status 由 verdict 決定：`keep → #PerfectPitch`；`revise/reject/None(critique 有文字但解析不到) → #NeedsReview`；`quality_verdict` 欄位永遠寫入（unparseable 也寫）。reject 的 synthesis 另外進 `_pending` review 佇列（與 profile 系統的 pending 審查機制一致） | `services/ingestion_pipeline.py` (~L619–645) |
| 1.3 | retry 耗盡仍 revise/reject 時：在檔案頂部（Executive Summary 前）插入明顯的 `> ⚠️ 品質警示` callout 摘錄 critical 缺陷，而不是讓矛盾埋在文末 | `services/ingestion_pipeline.py` |
| 1.4 | digest fallback 去毒：`_part_digest_fallback` (a) 過濾模板標題行（`摘要`、`翻譯內文`、`##` 開頭等）；(b) 回傳 `degraded: true`；pipeline 寫入 part frontmatter `digest_degraded: true` 並 log warning；(c) degraded digest 不進 facet index；(d) handoff 不再塞 pending_concepts | `services/llm_client.py::_part_digest_fallback`、`ingestion_pipeline._facets_from_digest` |

驗收：用本次 critique 文字餵 `parse_verdict` 得 `reject`；重跑後 Synthesis status ≠ PerfectPitch（在錯誤仍在時）。

### Phase 2 — source_prep 行號表格攤平（根治表格錯誤）

| # | 變更 | 檔案 |
|---|---|---|
| 2.1 | 新 pre-pass `flatten_linenumber_tables`（放在 `strip_boilerplate` 之後、splitter 之前）：偵測「兩欄、第一欄 ≥80% 為純數字且大致遞增（容忍跳號/空列）」的 markdown 表格 → 抽出第二欄依序接合為段落 | `services/source_prep.py` |
| 2.2 | 接合時的清洗：`<br>` → 空格；de-hyphenate（前列末端英文字 + 次列開頭小寫/續字時直接黏合，如 `con`+`tents`）；跨表格邊界續句接合（前表末列無句號 → 與下一表首列同段） | 同上 |
| 2.3 | 誤殺防護：僅在「數字欄無語意」（欄名為空/行號/Line）時觸發；真實資料表（欄名有語意、數字欄非遞增）不動；所有變更記入 `quality_fixes` | 同上 |
| 2.4 | Golden test：以 `raw/consolidate/cloud_act.md` 節選為 fixture，斷言輸出無 `^\|` 表格列、無斷字殘留 | `tests/test_source_prep.py`（或現有測試檔） |
| 2.5 | 驗收重跑：touch `raw/consolidate/cloud_act.md` 讓 daemon 重新 ingest；檢查新 Part 檔內文無行號假表格 | — |

### Phase 3 — Mermaid 修復（5 個 parse fail + 渲染退化）

| # | 變更 | 檔案 |
|---|---|---|
| 3.1 | classDiagram repair：在 `_repair_classdiagram_body` 收集所有 `<<stereotype>> X` 行引用的類名，若 X 未經 `class X` 宣告則在該行前自動插入宣告（deterministic 保底，修掉全部 5 個 fail） | `core/parsing/mermaid_repair.py` |
| 3.2 | ontology prompt 補強：`<<instance>> Fido` 範例前明示「必須先 `class Fido` 宣告，再寫 stereotype 行」 | `services/learning_artifacts.py` (~L113–124) |
| 3.3 | math degrade 擴展：把 mindmap 的 `$$…$$`/`$…$` 降級邏輯（`_mermaid_latex_to_plaintext`）套用到 stateDiagram-v2 與 timeline 的標籤/事件文字 | `core/parsing/mermaid_repair.py` |
| 3.4 | `repair_mermaid_quoted_endpoint_labels` 系列改走 canonical 路線：`"美國政府" --> "簽署行政協議"` 應修成 `n_us["美國政府"] --> n_agree["簽署行政協議"]`（合成 ASCII id + 保留引號 label），不再剝引號；`_synthesize_node_id` 禁止輸出純數字 id（加前綴） | 同上 |
| 3.5 | sequenceDiagram `rect rgb("…")` 引號剝除小修 | 同上 |
| 3.6 | 把本次驗證器產品化：`scripts/validate_mermaid.mjs`（node + jsdom + mermaid.parse，掃指定資料夾全部 fenced block），接入 Makefile / pre-commit 選跑——讓「渲染失敗率」變成可量測指標而不是靠肉眼 | `scripts/`、`Makefile` |

驗收：cloud_act 8 檔重跑後 `validate_mermaid.mjs` 回報 0/40 fail；`tests/test_mermaid.py` 新增 5 個實際失敗塊 fixture。

### Phase 4 — 譯文污染 lint（deterministic，掛在 markdown quality checker）

| # | 變更 | 檔案 |
|---|---|---|
| 4.1 | 新 pass `cjk_corruption_check`：(a) CJK 段落中出現泰文/西里爾/阿拉伯文等非預期 script → 記 `quality_warnings`；(b) 零寬字元（U+200B/200C/200D/FEFF）→ 直接 strip 並記 fix | `core/parsing/markdown_quality.py`（新 pass，沿用現有 pass 介面） |
| 4.2 | 數字保真檢查（僅 `type: translation`）：抽取譯文中所有阿拉伯數字 token，與來源 chunk 的數字集合 diff；譯文多出的數字（如 1180、19 78）記入 `quality_warnings: number_not_in_source` | 同上 + `ingestion_pipeline`（把來源 chunk 傳給 checker） |
| 4.3 | warnings 升級路徑：任一 part 有 warnings → 該 part frontmatter 標 `#NeedsReview`；synthesis 聚合顯示 | `ingestion_pipeline` |

備註：`撤算`/`合格外務政府` 這類同音錯字屬 LLM 隨機錯誤，deterministic 層不硬修（誤修風險大），
靠 critique 層與人工 review 佇列；lint 只保證「非中文字元污染」與「數字失真」零漏網。

### Phase 5 — 模板與閱讀效率

| # | 變更 | 檔案 |
|---|---|---|
| 5.1 | universal-document-template prompt 禁止模型自產元資料區（日期/作者/狀態/標籤行）——synthesis 殼已有 System Metadata；同時要求輸出從 `##` 開始（不得出現 H1） | Templates（Scripture 端）+ 必要時 `markdown_quality` 加 heading demote pass（body 中 H1 → H2 順降） |
| 5.2 | artifact 注入去重標題：`## 🖼️ 學習輔助（argument_map）` 若 artifact 自帶標題（如 Toulmin），只保留一層 | `services/learning_artifacts.py::maybe_artifact_section` |
| 5.3 | frontmatter 減脂：trace_ids 移到 sidecar（`<note>.trace.json` 或既有 trace 目錄），frontmatter 只留 run_id；part_digest 與 `## 🧩 Part Digest Appendix` 二擇一（建議：**附錄保留**供人讀，frontmatter 只留 thesis + digest_schema 供機器） | `ingestion_pipeline._build_part_metadata`、`_attach_trace_metadata` |
| 5.4 | Stitched 詞彙表合併：stitch post-pass 把各 Part 的「詞彙與關鍵術語」表抽出、以英文術語為 key 去重，文件尾附一份總表（Part 內原表可留可刪，建議刪） | stitch pipeline（`part-note-stitch-v2` 所在模組） |
| 5.5 | 法律文件模板規則：translation-rpt 模板明示「法條逐項用巢狀清單呈現，不得用表格模擬條文編號」（源頭 Phase 2 修好後這條是保險） | Templates |

### 依賴與順序

```
Phase 1（閘門）──獨立，先做
Phase 2（source_prep）──獨立
Phase 3（mermaid）──獨立；3.6 驗證器可最先做（量測基線）
Phase 4（lint）──4.2 依賴 pipeline 傳 source chunk，其餘獨立
Phase 5──5.1/5.5 是 Scripture 模板改動（hot-reload，無需重啟）；5.3 涉及 schema，最後做
```

建議批次：**P1 + 3.1/3.2（一天內可完成、止血最大）** → P2 + P4 → P3 其餘 → P5。
每批完成後 touch `raw/consolidate/cloud_act.md` 重跑一次，以 cloud_act 為 end-to-end 驗收案例。

### 總驗收清單（cloud_act 重跑後）

- [ ] `validate_mermaid.mjs`：0 parse fail（基線 5/40）
- [ ] 內文無 `| n | … |` 行號假表格、無 `<br>` 殘留、無斷字
- [ ] 無泰文/零寬字元污染；數字保真 warnings = 0 或全部有解釋
- [ ] critique 判 reject 時 status ≠ #PerfectPitch，且檔頂有品質警示 callout
- [ ] 無 `digest_degraded` 的 part；facet index 無標題型假 facets
- [ ] Synthesis 無幻覺元資料區、無 body H1、frontmatter trace_ids ≤ 1 行
- [ ] 既有測試全綠：`test_mermaid.py`、`test_parser.py`、`test_ingestion_pipeline.py`、`test_learning_artifacts.py`
