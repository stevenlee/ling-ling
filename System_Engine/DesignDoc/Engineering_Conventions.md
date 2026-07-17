# Engineering Conventions — System_Engine 隱形慣例書

> 給所有實作者（人類或 AI）的必讀文件。這些慣例大多不寫在各功能的
> DesignDoc 裡，但違反任何一條都會在 review 被退回。
> 每條都有既有程式碼可參照——動工前先讀範例。

## 1. 併發紀律（最重要，違反 = 直接退回）

- **busy lock 是 vault / ChromaDB 寫入的唯一全域排程互斥機制**：
  `core/state.py` 的 `global_busy_state`。寫 vault 或 ChromaDB 前
  `try_set_busy()`，失敗就排程重試或放棄，**絕不裸跑**（範例：
  `vault_watcher._process_deletion`）。釋放永遠在 `finally`。物件內部的
  `threading.Lock` 只保護該物件的共享狀態，不能取代 busy lock。
- **idle callback 不做實際工作**：執行時鎖仍被持有，在裡面跑 LLM
  等於插隊。callback 只能排 timer（範例：`FacetBackfillPump.on_idle`）。
- callback 註冊順序 = 優先權：使用者工作在前，背景在後
  （見 `main.py` §3.1/3.2）。
- 一般背景 maintenance 用 **timer 泵**（`threading.Timer` + debounce
  key + 忙碌重試），不新增**無生命週期管理**的常駐執行緒（範例：
  `FacetBackfillPump`、`VaultWatcher._schedule_orphan_sweep`）。
  watchdog / scheduler / 序列化 worker / telemetry 等基礎設施可以
  常駐（範例：`clipping_watcher._worker`），但必須有明確 ownership、
  停止機制，且共享狀態上鎖。
- 共享可變狀態加 `threading.Lock`；read-modify-write 全程在鎖內
  （範例：`MaintenanceScheduler._state_lock`）。

## 2. 檔案 I/O

- **原子寫入**：先寫 temp 再 `Path.replace()`。temp 必須與目標**同一
  目錄**（跨 filesystem 的 rename 不原子）；多 writer 情境用
  `mkstemp` 唯一檔名，不用固定 `.tmp` 互撞；重要落盤前 fsync
  （範例：`improvement_store._atomic_write_text`）。
- 一律 `pathlib.Path` + `encoding="utf-8"`；寫檔前
  `parent.mkdir(parents=True, exist_ok=True)`。
- 多步寫入要**指明 commit point**：commit 前失敗 = 乾淨回滾、回報
  未套用；commit 後失敗 = 誠實回報「已生效但待清理」，絕不謊稱未套用
  （範例：`improvement_store.approve_proposal`）。
- `raw/` 允許建立／歸檔新檔，**歸檔後不得原地改寫**；migration
  例外，但必須先備份。報告輸出到 `fromLingLing/`；需審核的自動生成
  資產進 `_pending/`（範例：ProfileManager）。

## 3. Fail-soft 哲學

- 背景功能的失敗**不得癱瘓 daemon**：catch → `logging.warning` /
  `logging.exception`（要 stack trace 用後者）→ 回傳狀態。
- 使用者看得到的失敗用 `ui.error()` 浮出，不准靜默吞掉。
- 品質訊號類功能 **fail-open**：寧可少一個分數，不可擋住主流程
  （範例：`InsightAgent._check_skill_preconditions`）。
- **失敗 ≠ 一個具體判定值**：parse/transport 失敗不可偽裝成保守
  verdict 寫進持久狀態（尤其 no-TTL 快取）。標記 `valid: False` 或
  回 `None` 走既有重試路徑，讓下游分得清「判了」跟「沒判成」
  （範例：`adjudicate_claims` + `_Consolidator._adjudicate`）。

## 4. 狀態管理：推導優於追蹤

- 能安全、便宜推導時，「哪些東西待處理」優先從 ground truth
  （檔案系統、ChromaDB）**現場推導**，不持久化 done-list
  （範例：orphan sweep、backfill queue）。
- 工作**昂貴（LLM）、非確定或需要 crash-resume** 時，可持久化
  progress ledger（合規範例：ingestion 的 B1 part resume），但必須有
  內容 hash 或 schema version、冪等語義、損壞時的恢復策略。失敗
  ledger（attempts/quarantine/budget）一律持久化。consolidation 現有的
  `processed` 只有 filename/date/claims，**是待補 input hash 的 legacy
  debt，不是可照抄的範例**；同名 insight 內容改變後應能重新處理。
- ID 用**內容定址**（hash of content）確保重跑冪等；寫入前先刪同
  key 舊資料（範例：`add_facets`）。
- 持久化狀態會活得比寫它的 code 久——邊界檢查放在**消費端接縫**，
  不能只靠寫入端（範例：`autotune_store.get_tuned(min_value=,
  max_value=)`）。型別強轉（`type(default)(x)`）會改變語意時，加註
  解或 assert 標明約束，別留暗坑。

## 5. LLM 呼叫

- 一律經 `LLMClient`；新 prompt = `llm_client.py` 上的新方法，
  **必帶** `trace_context={"stage": "<名稱>", "metadata": {...}}`。
- **retry 分兩層，不可混**：transport error（連線、逾時、5xx）交給
  `_complete_text` / `core.retrying.retry_call`，caller 不得疊加；
  解析失敗或空 content 用 `core.retrying.reroll` 做**有上限**的
  content 重試，並留下 attempt telemetry（範例：`adjudicate_claims`
  的 3 attempts + 升溫）。
- 確定性任務 temperature 0–0.3；輸出先清洗再 parse；防禦非 str /
  MagicMock 回傳（測試會用 mock LLM 打你的碼）。
- prompt 本體放 vault（`Templates/Operations/` 等），frontmatter 由
  `_load_capability_body` 剝除後注入。

## 6. Config

- 開關與參數放 `core/config.py`：env 讀取、給預設值、大寫常數；
  路徑是 module-level 常數。
- **預設 module top** `from core.config import X` 後直接引用；測試
  monkeypatch **實際消費的符號**（`your_module.X`）。需要 hot
  reload、避免循環依賴或明確 dependency injection 時可 late import
  （範例：`monte_carlo._should_ground`），但要註明原因，且測試必須
  打得進去。

## 7. Markdown / Frontmatter

- 解析用 `core/parser.py` 的 `parse_markdown_metadata` /
  `strip_body_frontmatter`，不要自寫 regex 解析 YAML。
- 機器要再讀的章節用**穩定標頭常數**（範例：`_PART_DIGEST_HEADER`），
  解析器必須是確定性的。
- **要寫入 vault 的 Markdown** LLM 產出先過
  `run_markdown_quality_checks`（JSON verdict 等結構化輸出不適用）。

## 8. Trace 整合

- 分析用事件寫 `trace_store.record_artifact(artifact_type=...,
  metadata=...)`；查詢用 `query_artifacts` / `query_llm_calls`。
- maintenance 任務的 intent 命名 `maintenance.<task_name>`。
- 頁面 metadata 透過 `_attach_trace_metadata` 模式帶 trace_ids。

## 9. 測試（驗收的硬門檻）

- 位置 `System_Engine/tests/`；匯入路徑由 `pyproject.toml` 的 pytest
  `pythonpath` 統一提供，**不得在個別測試修改 `sys.path`**。
- **完全 hermetic**：所有可變 runtime 路徑與寫入全導進 `tmp_path`
  （monkeypatch 模組級常數），絕不碰真實 vault / DB / 開發者本機的
  live state——會洩漏的旗標用 autouse fixture 關掉（範例：
  `test_grounded_insight._isolate_live_autotune`）。允許唯讀存取 repo 內
  受版本控制的 fixture / prompt asset，但不得依賴未追蹤的本機內容。
- Fake 用簡單 class（FakeLLM/FakeRAG 模式，見
  `test_dynamic_pipeline.py`），行為要像真物（簽名相容）。
- **修 bug 必附打在失敗路徑上的測試**：monkeypatch I/O 使其 raise，
  驗證回滾與回報語意，不是只測 happy path（範例：
  `test_improvement_store` 的 approve 交易測試）。
- **不得為了讓新碼過關而削弱、刪除或放寬既有測試**。刻意演進契約時
  可以同步更新測試，但必須在同一變更中說明契約差異，並為舊的失敗
  案例補回歸測試（範例：`valid` 欄位加入時 adjudicate 斷言的同步
  更新 + `test_failed_adjudication_is_pending_not_permanently_cached`）。
- 交付門檻 = **`make check` exit 0**（ruff + mypy + fast tests；CI
  跑的就是它）。測試數只是參考基線（2026-07：1669 passed），不是
  門檻本身。

## 10. 風格與 Git

- 註解密度跟著周圍程式碼走；只寫「程式碼本身講不出的約束」，
  不寫敘事、不寫「這行在做什麼」。
- 使用者可見字串用繁體中文；docstring / 識別字用英文。
- commit 格式 `feat(scope): ...` / `fix(scope): ...`，subject 一句話
  講清「為何」而不只「什麼」。
- **預設 feature branch**；只有 repo owner 明確要求時才直接提交或
  推送 main。

## 11. 交付與交接契約（多 agent 協作，違反 = 退回）

> 本 repo 由多個 agent（Claude、Codex、Gemini）與人類共用。
> 「程式改對了」只是一半，另一半是下一個接手的人不用讀 diff
> 反推你做了什麼。

- **只有使用者明確要求 commit／交付 commit 時才提交**；一般
  change/build 授權本身不包含 commit 或 push。已授權 commit 的實作，
  做完不留半成品在未提交的 working tree——daemon 與其他 agent 都可能
  覆蓋它。任務只要求 review / diagnosis、或尚未授權 commit 時，保持
  工作樹並清楚交接；**不得自行擴張成 commit、push 或 PR**。還沒好的
  放 branch 並講明。
- **一個邏輯修復 = 一個 commit**。五件獨立的事不塞一坨。
- **handoff 依規模分級**：行為契約、資料格式、架構、操作流程或
  roadmap 狀態改變時，更新 roadmap/DesignDoc（做了什麼 / 為何 /
  驗了什麼 / 還欠什麼；本 repo 的 `docs(roadmap): ...` commit 即此
  用途）。小型局部修復由 commit message、測試名稱與交付摘要完成
  handoff 即可，不用為 typo 製造 docs commit。
- **附可覆核的驗證聲明**：「已跑 `make check`：exit 0 / mypy clean」
  這種能被重跑核對的一句話。動了測試或 CI 本身時尤其要先自己跑過。
  失敗就講失敗，跳過就講跳過。
- **你的守衛讓舊碼變死碼時，順手刪掉**——別留給下一個人猜它
  還算不算數。
- 一個 Phase 一個交付；review 兩輪仍未收斂時，**重新界定問題或升級
  討論**，不得為了收斂而犧牲正確性。
