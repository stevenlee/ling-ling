# C04 執行版本與設定觀測契約

## 問題與範圍

日期式 BUILD_DATE 不能區分同日提交，也無法辨別 checkout 已更新、daemon 卻仍使用
先前載入模組的情形。C04 增加程序識別與啟動 checkout 快照，不改排程或模型選擇。

`core.runtime_identity` 首次載入時收集一次，之後不再查 Git；main 的依賴匯入會在工作
開始前載入此模組。PID lock 成功後，先發布初始化狀態，再啟動服務與工作。
這是啟動時 checkout 的觀測，不是所有 Python 模組載入內容的逐位元證明。
執行中改檔／切分支可能影響 late import，因此仍須使用穩定 checkout，不能靠此欄位
保證混合載入安全，也不能用事後的 Git HEAD 推定舊程序版本。

## 欄位與寫入位置

| 位置 | 新增內容 |
|---|---|
| `daemon_status.json` | `runtime`、`runtime_config`；保留 busy／message／ts |
| trace `runs.metadata_json` | 每個 run（含巢狀）寫入 `runtime`、`runtime_config`，不需 schema migration |
| 經 `LLMClient.trace_run` 的 run | `llm.provider`、`llm.model`，取自實際 client；沒有模型呼叫的任務亦可識別配置 |
| maintenance state 的已執行 task | `last_runtime`；含成功、明示 failed 與例外路徑 |

`runtime` schema version 1：

- `session_id`：程序首次載入時生成的隨機 ID，同程序各 writer 共用；不同 CLI／daemon
  session 分開。`pid` 是目前程序 PID；此欄位不表示 writer 必定是 daemon。
- `captured_at`：快照取得時間（UTC），不冒充 OS 程序建立時間。
- `commit`：完整 Git HEAD；Git 不存在、輸出無效或查核期間 HEAD 移動時為 null。
- `engine_dirty`：System_Engine、pyproject.toml、Makefile 範圍內是否有 Git 可見的修改
  或未追蹤檔。Git 忽略的 vault／runtime state 不列入；檢查失敗為 null。
  只有 commit 已知且 dirty=false 才能把此快照當作乾淨 checkout 的版本參照。
  dirty=true 不保存 diff，不能還原當時的未提交內容。
- `build_date`：沿用原有 BUILD_DATE（Git 不可用時可能為 mtime），只供輔助顯示。

每次 Git 子命令限時 2 秒，失敗維持 unknown；快照後查詢不再執行 subprocess。
呼叫端取得副本，不能改掉之後的身份；TraceStore 保留上述 metadata key，呼叫端
提供同名資料不能偽造身份，其他 metadata 保留且不原地修改。

`runtime_config` 是事件當下的明確 allowlist：embedding provider／model 設定、reranker
開關／model、facet／cross-lingual 開關及 A2 enabled／apply／max_distance。
A2 動態設定在每次事件寫入時讀取，不凍結成啟動值。這不是完整設定快照：不收錄
API key、URL、整份 env／Scripture／prompt；embedding model=null 仍表示未明示配置，
不能當作實際模型身份。LLM model 是 client 的名稱，不是 Ollama 權重 digest；若需
權重或 prompt 重播資訊，仍用各自既有 audit／模型評估契約。

舊 trace／state／status 缺欄位時視為未知，不補寫成目前版本。若 task 在 busy gate
跳過而未執行，不替換先前的 last_runtime。state 的 last_run_at 保留既有排程時間
語義，實際起訖仍看 trace 的 started_at／ended_at。

## 驗證與部署狀態（2026-11-04）

12 個 hermetic 回歸案例：clean／dirty、Git 缺失／timeout／status 失敗、無效或移動的
HEAD、程序快照固定與副本隔離、設定 allowlist／動態旗標、狀態及巢狀 trace、成功與
明示失敗 task 的 state／trace 身份一致；所有寫入均導向 tmp_path。

`make check` exit 0：ruff clean、mypy 174 source files clean，2128 passed、1 skipped、
1 個既有 Chroma warning；`git diff --check` exit 0。

本輪前使用者已重啟 daemon；本輪唯讀觀察時其 status 尚無 runtime，符合本功能尚未
加入當時版本的預期。C04 新欄位須由之後重新載入本版的 daemon 自行發布；本輪未
自行重啟、觸發 live maintenance／手動自評或修改歷史 trace。實作與隔離驗證完成，
live 新欄位驗收保留，不能把 CLI 的新快照當成 daemon 已載入的證明。
