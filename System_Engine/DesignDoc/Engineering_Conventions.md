# Engineering Conventions — System_Engine 隱形慣例書

> 給外部實作者（人類或 AI）的必讀文件。這些慣例大多不在各功能的
> DesignDoc 裡，但違反任何一條都會在 code review 被退回。
> 每條都有既有程式碼可參照——動工前先讀範例。

## 1. 併發紀律（最重要，違反 = 直接退回）

- **busy lock 是唯一的互斥機制**：`core/state.py` 的
  `global_busy_state`。做任何會寫 vault 或 ChromaDB 的工作前
  `try_set_busy()`，失敗就**排程重試或放棄，絕不裸跑**
  （範例：`vault_watcher._process_deletion`）。釋放永遠在
  `finally` 裡。
- **idle callback 絕不在內做實際工作**：callbacks 在鎖仍被持有時
  執行；在裡面跑 LLM 等於插隊。callback 只能排 timer
  （範例：`facet_backfill.FacetBackfillPump.on_idle`）。
- **callback 註冊順序 = 優先權**：使用者工作的 callbacks 在前，
  背景工作在後（見 `main.py` §3.1/3.2 的註解）。
- 背景工作用 **timer 泵**模式（`threading.Timer` + debounce key +
  忙碌時重試），不開常駐執行緒（範例：`FacetBackfillPump`、
  `VaultWatcher._schedule_orphan_sweep`）。
- 共享可變狀態加 `threading.Lock`；read-modify-write 必須在鎖內
  （範例：`MaintenanceScheduler._state_lock`）。

## 2. 檔案 I/O

- **原子寫入**：先寫 `.tmp` 再 `Path.replace()`
  （範例：`MaintenanceScheduler._save_state`、bench history）。
- 一律 `encoding="utf-8"`、一律 `pathlib.Path`。
- 寫檔前 `parent.mkdir(parents=True, exist_ok=True)`。
- **絕不修改 `raw/`**；報告輸出到 `fromLingLing/`；需審核的自動
  生成資產進 `_pending/`（範例：ProfileManager）。

## 3. Fail-soft 哲學

- 背景功能的任何失敗**不得癱瘓 daemon**：catch →
  `logging.warning/exception`（要完整 stack trace 就用
  `logging.exception`）→ 回傳狀態。
- 使用者看得到的失敗用 `ui.error()` 浮出，不准靜默吞掉。
- 品質訊號類功能失敗時 **fail-open**（寧可少一個分數，不可擋住
  主流程；範例：`InsightAgent._check_skill_preconditions`）。

## 4. 狀態管理哲學：推導優於追蹤

- 「哪些東西待處理」從 ground truth（檔案系統、ChromaDB）**現場
  推導**，不持久化 done-list（範例：orphan sweep、backfill queue）。
- 只持久化**失敗 ledger**（attempts/quarantine/budget）。
- ID 用**內容定址**（hash of content）確保重跑冪等；寫入前先刪
  同 key 舊資料（範例：`add_facets`）。

## 5. LLM 呼叫

- 一律經 `LLMClient` 的方法，新 prompt = `llm_client.py` 上的新
  方法，**必帶** `trace_context={"stage": "<名稱>", "metadata": {...}}`。
- retry 已內建於 `_complete_text`，不要自己再包 retry。
- 確定性任務 temperature 0–0.3；輸出先 strip/清洗再 parse；對
  非 str / MagicMock 回傳做防禦檢查（測試會用 mock LLM 打你的碼）。
- Operations/Skills 的 prompt 本體放 vault（`Templates/Operations/`
  等），frontmatter 由 `_load_capability_body` 剝除後注入。

## 6. Config

- 開關與參數放 `core/config.py`：env 讀取、給預設值、大寫常數。
- 路徑是 module-level 常數。**功能模組在 module top `from
  core.config import X` 後直接引用 X**——測試靠 monkeypatch
  `your_module.X` 重導路徑，不要在函式內重新 import 或快取成
  局部變數，會讓測試打不進去。

## 7. Markdown / Frontmatter

- 解析用 `core/parser.py` 的 `parse_markdown_metadata` /
  `strip_body_frontmatter`，不要自寫 regex 解析 YAML。
- 機器要再讀的章節用**穩定標頭常數**（範例：
  `_PART_DIGEST_HEADER`），解析器必須是確定性的。
- LLM 產出先過 `run_markdown_quality_checks`。

## 8. Trace 整合

- 分析用事件寫 `trace_store.record_artifact(artifact_type=...,
  metadata=...)`；查詢用 `query_artifacts` / `query_llm_calls`。
- maintenance 任務的 intent 命名 `maintenance.<task_name>`。
- 頁面 metadata 透過 `_attach_trace_metadata` 模式帶 trace_ids。

## 9. 測試（驗收的硬門檻）

- 位置 `System_Engine/tests/`，檔頭
  `sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))`。
- **完全 hermetic**：一切路徑導進 `tmp_path`（monkeypatch 模組級
  常數），絕不讀寫真實 vault 或真實 DB。
- Fake 用簡單 class（FakeLLM/FakeRAG 模式，見
  `test_dynamic_pipeline.py`），mock 行為要像真物（簽名相容）。
- **不准修改既有測試來遷就新碼**——既有測試失敗代表你破壞了
  契約，修你的碼。
- 交付前跑全套：`./venv/bin/python -m pytest System_Engine/tests -q`
  必須全綠（目前 726 passed）。

## 10. 風格與 Git

- 註解密度跟著周圍程式碼走；註解只寫「程式碼本身講不出的約束」，
  不寫敘事、不寫「這行在做什麼」。
- 使用者可見字串用繁體中文；docstring/識別字用英文。
- Feature branch 工作，commit 訊息格式 `Feat(scope): ...` /
  `Fix(scope): ...`，**不直接碰 main**。
- 一個 Phase 一個交付；review 修正最多兩輪。
