# Backlog Batch 1 — 實作規格（Gemini 委外交付用）

> 三個獨立小工作打包一單：T1 評分穩定化、T2 每週記事、T3 token 體檢。
> 必讀順序：① [Engineering_Conventions.md](Engineering_Conventions.md)
> ② 本文件。工作分支：`backlog/batch-1`（不准 commit 到 main）。
> Review 修正最多兩輪，未收斂由 reviewer 接手。

## 0. 硬性條款（前兩輪委外的教訓，違反直接退回）

1. **每個交付的可執行腳本／live 行為必須附執行輸出**（terminal
   transcript，貼在 PR 說明或 `scratch/` 下的 log 檔）。寫得出來
   不算完成，跑得起來才算。T3 特別要求對著真實 Ollama 跑。
2. **Commit 範圍自查**：commit 前 `git status`，只准 add 本規格
   交付清單內的檔案。個人 vault 檔案（Excalidraw、Kanban 等）碰了
   直接退。
3. **零修改既有測試**。如果你覺得必須改，那是你的實作缺防禦
   （參考 phase1 review M3 的教訓）——改你的碼。
4. 指涉物照字面實作：「query 文字」就是 retrieval_events 的
   query_text 內容、「Cortex 頁」就是用 `cortex_store.load_all_pages`
   讀——不准用檔名或標題代替內容。

## T1 — falsifiability 評分穩定化（三次取中位數）

**問題**：同一主張兩次評分拿 1.0 與 0.5（已實測）。單點分數不可信。

**規格**：
- `core/config.py` 新增 `CORTEX_FALSIFY_SAMPLES`
  （env `CORTEX_FALSIFY_SAMPLES`，預設 `3`，最小 1）。
- 重構 `llm_client.assess_falsifiability`：
  - 把現有單次評估（含「解析失敗重試一次」的迴圈）抽成
    `_assess_falsifiability_once(claim) -> dict`，行為不變。
  - 外層呼叫 `_assess_falsifiability_once` 共 `CORTEX_FALSIFY_SAMPLES`
    次，收集 score 不為 None 的樣本：
    - 0 個可用樣本 → 回傳 `{"score": None, "falsifier": ""}`（現狀）。
    - ≥1 個 → `score = statistics.median(scores)`，clamp [0,1]、
      round 4；`falsifier` 取「score 與中位數距離最近」的那個樣本的
      falsifier（距離相同取先到的）。
  - `CORTEX_FALSIFY_SAMPLES=1` 時行為必須與現狀 byte-identical
    （不多花任何 call）。
- 成本說明寫進 docstring：每個新主張 ≈ samples × call（本地模型）。

**測試**（hermetic，mock `_complete_text`）：
- 樣本 [0.0, 0.5, 1.0] → score 0.5，falsifier 來自 0.5 那次。
- 樣本 [1.0, 1.0, 0.0] → 1.0。
- 兩個可用樣本 [0.5, 1.0] → statistics.median = 0.75（容許非錨點值，
  斷言 0.75）。
- 只有 1 個可用（其餘解析失敗）→ 用那一個。
- 全部失敗 → None。
- samples=1 → `_complete_text` 只被呼叫一次（含 retry 場景另測）。

## T2 — 每週記事（系統情節記憶的敘事出口）

**目標**：trace store 已是完整自傳，缺人類可讀的出口。每週一頁
「本週記事」到 `fromLingLing/`。**模板照抄
`maintenance/routing_report.py` 的結構**（dataclass result、
`_append_maintenance_log`、`_write_report`、路徑與參數全部可注入）。

**規格**：
- `services/trace_store.py` 新增：

```python
def recent_query_texts(self, since_days: int = 7) -> list[str]:
    """Distinct retrieval query texts in the window, newest first."""
```

  （SELECT query_text FROM retrieval_events，去重保序，排除空值；
  參考既有 `recently_retrieved_titles` 的寫法。）

- `maintenance/weekly_memoir.py`：
  `run_weekly_memoir(trace_store, *, cortex_dir, insights_dir,
  bench_history, report_dir, log_path, window_days=7) -> MemoirResult`。
  報告內容（繁體中文，敘事語氣，無資料的節省略）：
  1. **你問了什麼**：`recent_query_texts` 前 10 條。
  2. **我讀了什麼**：`query_artifacts("routing_decision", 7)` 的
     檔名與 profile 統計。
  3. **我想了什麼**：`insights_dir` 中 7 天內的 insight 檔
     （檔名時間戳或 mtime 判斷），列檔名與 signals 摘要
     （groundedness / refute_verdict，用 `parse_markdown_metadata`）。
  4. **大腦長了什麼**：Cortex 頁 `created`/`updated` 落在窗口內的，
     列主張全文；falsified 的特別標注。
  5. **健康一行**：bench history 最後一筆 pass_rate 與 facet_lift。
  - 摘要一行永遠進 maintenance.log.md；報告檔名
    `[memoir] 本週記事 YYYYMMDD.md`。
  - 一切 fail-open：任一節資料來源壞掉 → 該節標「（本節資料不可用）」，
    不得 crash。
- `watchers/maintenance_scheduler.py`：新 MaintenanceTask
  `weekly_memoir`，`interval_seconds=7*86400`、`idle_required=True`、
  intent `maintenance.weekly_memoir`、agent `WeeklyMemoir`。
  （action 寫法照抄 `routing_report` 那個。）

**測試**（hermetic）：
- FakeTraceStore（提供 recent_query_texts / query_artifacts）+
  tmp Cortex 頁 + tmp insight 檔 → 報告含五節對應內容。
- 空窗口 → 各節省略、不 crash、maintenance log 仍有一行。
- bench history 損毀 → 健康節標不可用。
- `recent_query_texts` 用真 TraceStore（`db_path` 注入 tmp）seed 幾筆
  retrieval_events 驗證去重與排序。

## T3 — 結構化 LLM 呼叫的 token 上限體檢

**問題**：reasoning 模型（gemma 系）會把思考塞滿小額 max_tokens，
content 空白。`classify_document` 與 `select_profile` 只給 **20
tokens**——在 reasoning 模型下幾乎必然空輸出 → **文件路由靜默退化
到 default profile**，使用者無感。

**規格**：
1. 盤點：`grep -n "max_tokens=" services/llm_client.py`，把所有
   顯式 ≤512 的呼叫列成清單（呼叫名、現值、用途），貼進交付說明。
2. 修正原則：**結構化輸出的呼叫一律 `max_tokens=None`**（交給
   `settings.MAX_OUTPUT`，思考有空間；輸出長度由 prompt 約束，
   不由 token 上限截斷）。已知至少要修：`classify_document`(20)、
   `select_profile`(20)、`translate_tags`（若有小額上限）。
   `score_text_quality` / `find_topic_shifts` / `summarize_for_context`
   是版本鎖定的 P0 評分器——**只改 max_tokens 參數，prompt 一字不動**。
3. 回歸測試：mock 一個「content 空、reasoning 欄含答案」的 response
   物件打 `_openai_chat`，斷言 fallback 後 `classify_document` 與
   `select_profile` 仍解析出正確答案（fallback 機制已存在於
   `_openai_chat`，你要證明這兩個呼叫端與它相容）。
4. **Live 驗證（硬性）**：對著真實 Ollama 跑
   `classify_document("test.md", "Claims\nPrior Art...")` 與
   `select_profile(...)`（任意兩個 profile 選項），貼 terminal
   transcript 證明回傳非空且合法。

## 驗收標準（整單）

1. 全套既有測試綠（**824 起跳**），零修改既有測試。
2. T1/T2/T3 各自的新測試齊備（上列）。
3. `.env.example` 補 `CORTEX_FALSIFY_SAMPLES`；README Refactor Notes
   一節涵蓋三項（格式仿既有條目）。
4. 執行輸出：T2 的 memoir 對著真實 vault 跑一次（report 檔產出）、
   T3 的 live transcript。
5. 分支 `backlog/batch-1`、commit 訊息 `Feat(ops): ...` 或分項
   commit 皆可，範圍自查。

## Review 重點預告

- T1：samples=1 的零成本路徑；median 的偶數樣本行為；falsifier
  選擇規則。
- T2：fail-open 的每一節；報告路徑與參數可注試（hermetic）；
  指涉物是否讀了內容而非檔名。
- T3：P0 評分器 prompt 是否真的一字未動（diff 會查）；live
  transcript 是否真實（會抽查 trace store 的對應紀錄）。
