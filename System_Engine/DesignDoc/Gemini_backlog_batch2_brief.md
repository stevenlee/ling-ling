# Backlog Batch 2 — 實作規格（Gemini 委外交付用）

> 三個獨立小工作打包一單：T1 critique retry、T2 新 Operations 四件套、
> T3 select_profile 選單修復。
> 必讀順序：① [Engineering_Conventions.md](Engineering_Conventions.md)
> ② 本文件。工作分支：`backlog/batch-2`（不准 commit 到 main）。
> Review 修正最多兩輪，未收斂由 reviewer 接手。

## 0. 硬性條款（前三輪委外的教訓，違反直接退回）

1. **每個交付的可執行腳本／live 行為必須附執行輸出**（terminal
   transcript，貼在 PR 說明或 `System_Engine/scratch/` 下的 log 檔；
   該目錄已 gitignore，留檔即可不必 commit）。T3 特別要求對著真實
   Ollama 跑。
2. **Commit 範圍自查**：commit 前 `git status`，只准 add 本規格
   交付清單內的檔案。個人 vault 內容（`Cortex/`、`Insights/`、
   Excalidraw、Kanban 等）碰了直接退——batch-1 第一輪就是這樣
   被退的。
3. **零修改既有測試**。覺得必須改 = 你的實作缺防禦，改你的碼。
4. 指涉物照字面實作：規格說改哪一行就改哪一行，不准用「等價」
   寫法替換鄰近程式碼。

## T1 — Synthesis critique retry loop（停車場 D1）

**問題**：critique postcheck 已存在（`ingestion_pipeline.py`
`_run_synthesis_critique`，verdict ∈ keep/revise/reject），但 verdict
是 revise/reject 時只記錄、不行動。落了一半的閉環。

**規格**：
- `core/config.py` 新增 `SYNTHESIS_CRITIQUE_MAX_RETRIES`
  （env 同名，預設 `1`，`max(0, int(...))`）。`.env.example` 補一行。
- `llm_client.generate_synthesis` 新增 keyword-only 參數
  `critique_feedback: str | None = None`：
  - `None` 時組出的 prompt 必須與現狀 **byte-identical**。
  - 有值時在 user prompt 的 Task 段前插入一段
    `Previous attempt was critiqued. Address these findings:\n{critique_feedback}\n\n`。
    system prompt 一字不動。
- `ingestion_pipeline._write_synthesis` 重試迴圈：
  - 首次 critique verdict ∈ {"revise", "reject"} 且還有重試額度 →
    以 critique 全文為 `critique_feedback` 重呼 `generate_synthesis`，
    重跑 `run_markdown_quality_checks` 與 `_run_synthesis_critique`。
  - **採用規則**：verdict 排名 keep=2 > revise=1 > reject=0 >
    None=-1。重試結果排名**嚴格較高**才採用（文本＋critique 節＋
    verdict 一起換），否則保留原版。
  - verdict 為 None（critique 失敗或不可解析）**不觸發重試**——
    只對明確的 revise/reject 行動。
  - metadata 新增：`critique_attempts`（int，跑了幾次 critique）；
    重試發生時另記 `quality_verdict_history`（list，依序）。
    `quality_verdict` 維持最終採用版的 verdict，語意不變。
- 成本寫進 docstring：最壞情況每份長文 +1 次 synthesis +1 次
  critique（本地模型）。

**測試**（hermetic，mock `self.llm`，照 `test_ingestion_*` 既有
fixture 風格；測 `_write_synthesis` 或抽出的迴圈 helper 皆可，
但不准改既有測試）：
- verdict=keep → 零重試，`generate_synthesis` 恰被呼叫 1 次，
  metadata 無 `quality_verdict_history`。
- verdict=revise → 重試一次；重試 verdict=keep → 採用新文本，
  `quality_verdict == "keep"`，history `["revise", "keep"]`。
- 重試 verdict 相同或更差（revise→revise、revise→reject）→
  保留原版文本，`quality_verdict == "revise"`，history 仍記兩筆。
- `SYNTHESIS_CRITIQUE_MAX_RETRIES=0` → 行為與現狀一致，
  `critique_attempts == 1`。
- verdict=None → 不重試。
- `generate_synthesis(critique_feedback=None)` 與舊呼叫產生的
  prompt byte-identical（比對 `_complete_*` 收到的 user prompt）。

## T2 — 新 Operations 四件套：Compare / Classify / Outline / Explain（停車場 D3）

**目標**：CapabilityManager 掃 `lings-desktop/Templates/Operations/`
自動拾取（檔名 stem 即 capability id，見 `capability_manager.py`
docstring）。本工作 = 四個模板檔 + 註冊驗證測試，**零 Python
產品碼變動**。

**規格**：
- 新增四檔於 `lings-desktop/Templates/Operations/`，**格式照抄
  `critique.md`**（frontmatter 鍵集合相同：type/description/
  expected_inputs/expected_context/produces/cost_class/methodology；
  body 為英文 operator prompt，含 Operating Rules、Output Shape、
  Non-Goals 三節；`methodology: fixed`）：
  - `compare.md` — 多源對照。inputs: `candidates`（2+ 文本）；
    context: `dimensions`；produces: `comparison_matrix`；
    cost_class: `medium`。重點規則：逐維度對照、差異要引文佐證、
    禁止平均化分歧。
  - `classify.md` — 封閉選項歸類。inputs: `candidate`, `categories`；
    produces: `classification`；cost_class: `low`。重點規則：只准
    選給定類別或 `none`、附一行理由、禁止發明新類別。
  - `outline.md` — 結構化大綱。inputs: `candidate`；context:
    `depth`；produces: `outline`；cost_class: `low`。重點規則：
    保留原文層級語意、節點附原文錨點、禁止改寫內容。
  - `explain.md` — 受眾化解釋。inputs: `candidate`；context:
    `audience`；produces: `explanation`；cost_class: `medium`。
    重點規則：不犧牲正確性換淺白、術語首次出現須定義、
    禁止超出來源範圍的延伸。
- **測試**（新檔 `tests/test_new_operations.py`，hermetic）：
  - 用 CapabilityManager 指向 repo 內真實 Operations 目錄，斷言
    四個 id 都 `found`、`type == "operation"`、cost_class 與上表
    一致、`expected_inputs`/`produces` 非空。
  - 斷言既有七個 operation（answer_from_sources/critique/
    digest_sources/load_sources/plan/refute/synthesize）仍在——
    防手滑刪檔。

## T3 — select_profile 選單修復（batch-1 live test 發現的真 bug）

**問題**：`llm_client.select_profile` 的選單只印 hint、從未給模型
看 profile 名：

```python
menu = "\n".join(f"- {opt['hint']}" for opt in options)   # 第 1418 行
```

system prompt 卻寫「Available profiles (name: when to use)」並要求
「Return ONLY the profile name」。模型無從得知合法名字，live test
實測答了 hint 字樣（`programmingtutorials`）被既有防禦攔成 `none`
→ 路由靜默退化 default。

**規格**：
1. 選單改為 `f"- {opt['name']}: {opt['hint']}"`。其餘 prompt 一字
   不動。
2. 解析端加一層防禦（在現有 exact match 之後、回 `none` 之前）：
   normalize 後的 choice **恰好包含一個** valid name 的子字串 →
   採用該名並 `logging.info` 記錄 salvage；包含 0 個或 ≥2 個 →
   維持現狀回 `none`。既有 exact-match 與 exception 路徑不動。
3. **測試**（hermetic，mock `_complete_text`，新檔或併入
   `test_llm_fallback.py` 旁的新檔皆可）：
   - 回答恰為 name → 該 name（現狀回歸）。
   - 回答 `"I choose academic."` → `academic`（salvage 路徑）。
   - 回答同時含兩個 name → `none`。
   - 回答為 hint 字樣不含任何 name → `none`。
   - options 為空 → `none`（現狀回歸）。
4. **Live 驗證（硬性）**：對真實 Ollama 重跑 batch-1 的
   `scratch/t3_live_test.py` 場景，`select_profile` 須回傳合法
   name（不再是 `none`），transcript 留 `scratch/` log 檔。

## 驗收標準（整單）

1. 全套既有測試綠（**837 起跳**），零修改既有測試。
2. T1/T2/T3 各自的新測試齊備（上列）。
3. `.env.example` 補 `SYNTHESIS_CRITIQUE_MAX_RETRIES`；README
   Refactor Notes 一節涵蓋三項（格式仿既有條目，標題用
   `backlog batch-2`，不准掛錯 phase 名）。
4. 執行輸出：T3 live transcript；T1 對任一真實長文跑一次
   ingestion 確認 metadata 出現 `critique_attempts`（transcript
   或 trace store 紀錄擇一佐證）。
5. 分支 `backlog/batch-2`，commit 範圍自查。

## Review 重點預告

- T1：`critique_feedback=None` 的 byte-identical prompt（diff 會
  比對）；採用規則的「嚴格較高才換」；None verdict 不重試；
  retries=0 路徑零額外 call。
- T2：frontmatter 鍵集合與 critique.md 一致；cost_class 照表；
  測試是否真的掃 repo 目錄而非寫死清單之外的假目錄。
- T3：menu 行是否照字面改；salvage 只在「恰好一個」時觸發；
  live transcript 是否真實（會抽查 trace store 對應紀錄）。
