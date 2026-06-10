# Cortex Phase 1 — Review Round 1（2026-06-11，commit f71bc98）

> 結論：**退回修正**。骨架合格（732 測試綠、零修改既有測試、hermetic、
> 原子 sidecar、fail-open 結構），但 4 個 must-fix 中有 2 個是
> 「功能在動、量錯東西」的隱性錯誤。修正請在 `cortex/phase-1` 分支
> 進行（本輪直接 commit main 已是違規，不再重犯）。這是兩輪中的第一輪。

## Must-fix

### M1 — flag-off 違反驗收標準 #3
`INSIGHT_SIGNALS_ENABLED=false` 時 `compute_signals` 回傳全零
InsightSignals，`insight_agent` 仍把全零 signals block 寫進 frontmatter
（groundedness 0.0 會被下游讀成「全是幻覺」）。
**要求**：flag 檢查移到 agent 層——關閉時不呼叫 `compute_signals`、
meta 不含 `signals`/`signals_version`，行為與現狀 byte-identical。
補 flag-off 測試。

### M2 — verdict 解析不寬容（且解析路徑零測試）
實測：`**Verdict:** refuted`、`*Verdict*: refuted`、`Verdict：survived`
（全形冒號）全部 MISS。brief 明確要求仿 `ingestion_pipeline._VERDICT_RE`
的寬容寫法。FakeLLM 直接回 dict，`refute_insight` 的解析邏輯完全沒被
測到。
**要求**：放寬 regex（容忍 `*`/`_` 修飾與全形冒號，錨定行尾段）；
新增測試直接打 `refute_insight`（mock `_complete_text` 回傳各種
裝飾格式 + 亂格式 → None）。

### M3 — bridging 量錯對象
`rag.ef(titles_to_embed)` embed 的是**標題字串**而非來源頁內容。
「Siddhartha (Part 15)」vs「(Part 17)」的標題相似度反映命名慣例，
與知識距離無關——此訊號目前無效。
**要求**：對 `related_titles` 解析出頁面檔案（PAGES_DIR/NOTES_DIR，
同 groundedness 的存在性檢查路徑），讀取內容、strip frontmatter、
截 2000 字後 embed；讀不到的來源跳過；可比對的來源 <2 → bridging=0.0
（維持 fail-open）。測試的 FakeRAG.ef 改為依內容（非標題）回傳向量。

### M4 — refute 是蒙眼審判
refute prompt 要求「using ONLY the provided source materials」，但
sources 實際只是標題清單——反駁者無材料可查證，verdict 不可信。
**要求**：與 M3 共用來源載入邏輯，傳入每源內容節錄（每源截斷，
總量上限沿用 `LOAD_SOURCES_MAX_CHARS_PER_SOURCE` 精神）；
無任何可載入內容時跳過 refute（verdict=None），notes 註明
"no source content available"。

## Should-fix

- **S1**：各訊號的 exception 路徑不得回傳「最差值」（groundedness
  0.0）。fail-open 的語意是**缺值**：欄位改 `float | None`，失敗時
  None，frontmatter 寫 null。
- **S2**：補齊 brief §2.2 缺的測試：sidecar 500 上限淘汰、sidecar
  JSON 損毀 → warning + 重建不 crash、flag-off 行為、frontmatter
  出現 signals block 且 `Insights/` 鏡像 byte-identical。
- **S3**：`refute.md` frontmatter 的 `expected_inputs` 用 dict list，
  CapabilityManager 會 str() 成 `"{'candidate': ...}"`。改為字串
  list（照 `critique.md` 既有格式）。
- **S4**：工作流——修正輪在 `cortex/phase-1` 分支交付，commit 訊息
  格式 `Fix(cortex): ...`。

## 已通過（修正時不得回退）

- 全套測試綠、既有測試零修改
- 測試 hermetic（tmp_path + 模組級 monkeypatch）
- sidecar：原子寫入（tmp+replace）、threading.Lock、FIFO 500
- 四段訊號各自獨立 try/except（fail-open 結構正確）
- `refute_insight` 經 `_build_system_prompt(operation="refute",
  persona="none", forced_template="none")`，trace stage 正確
- config flags、`.env.example`、README 條目齊全
- groundedness 的 wikilink 別名解析（`[[A|B]]` → A）

## 驗收提醒

修正交付需重新滿足 brief §2 全部條款；尤其 M1 的 flag-off
byte-identical 與 M2 的解析測試是這輪的硬門檻。下一輪是最後一輪
（第 2/2），未收斂部分將由 reviewer 直接接手。
