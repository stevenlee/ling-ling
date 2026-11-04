# B02 M2 診斷觀測與推論契約（2026-11-04）

## 問題與行為變更

M2 原先僅對 Cortex 阻擋存量統計推導的根因，其餘軸仍可把模型高信心的因果斷言送給
M3。報告失敗率、novelty 均值、bench 或 entropy 本身都不能證明生成 prompt 的缺陷。
本段補齊觀測參照與能力邊界，將同樣限制推廣到所有由本流程產生的 M2 診斷。

- 模型提出的根因保留為 `hypothesis`，公開給使用者時明列「未驗證」。
- `root_cause` 改為尚未確認、confidence=0、candidate_fixes=[]、proposal_eligible=false，
  並提供 needs／尚未執行的查證項目。模型 JSON 的 reviewed 或 proposal_eligible 不採信。
- Cortex 原有 gate 保留；新增任務計數、run 成功或樣本 ID 均不能解除。
- 空回應／解析或 transport 失敗仍不冒充診斷；各軸 fail-soft，不阻擋其他診斷。
- M3 的既有 `proposal_eligible` 消費端 gate、人工 approve／reject 與目標範圍檢查保留。
  本段未新增「已覆核因果證據」的放行通道，沒有自動批准、修改或刪除既有提案。
  因此本版自動 M2→M3 的 prompt 提案會暫停於查證階段；這是契約改變，不是無聲略過。

後續若要從 M2 恢復可提案的診斷，須另設可重播且經覆核的輸入／來源／輸出與負責元件
契約；不能只加 reviewed=true 或提高 confidence。人類依查核結果進行的正常工程修復
不受此診斷 gate 限制。

## M1 附加觀測快照

`SelfAssessmentResult.diagnostic_evidence` 是 M1 取得並複製的 schema_version=1 快照。
M2 僅使用此份資料，不從另一時刻重新讀 live trace／vault；舊 assessment 或讀取失敗
標為 unknown，不當成零次執行或功能不存在。精簡趨勢 history 不複製參照清單。

`TraceStore.query_diagnostic_snapshot` 在同一 SQLite read transaction 取得限量參照：

| 類型 | 取樣界限 | 內容 |
|---|---|---|
| maintenance run | 每 intent 最新 3，總上限 60 | run ID、intent、trace_status、時間、錯誤前 300 字 |
| 任務實際結果 | 近期最新 180 個 result artifact 對應上述 run | result artifact ID、status 前 40 字、summary 前 600 字；沒有紀錄則 null |
| 有品質 verdict 的產物及洞察 | 每 artifact_type 最新 3，總上限 30 | artifact／run／trace ID、path、時間、類型與 quality_verdict |
| 失敗 LLM 呼叫 | 每 stage 最新 2，總上限 20 | trace／run ID、時間、stage、provider／model、錯誤前 300 字 |

時間窗沿用該次 M1 window_days（預設 7）。SQL 分組取樣後仍有總量上限，不能用清單
長度當作完整發生次數；空清單不代表歷史上未發生。快照不包含 source body、prompt、
user input 或完整模型回應；錯誤及任務摘要可能含私有標題／路徑，仍只留在私有 trace
與診斷報告。參照 ID 不等於 M2 已讀過或驗證了正文，也不替代 A03 的 durable audit。

此 transaction 僅讓附加 SQLite 參照彼此一致；M1 的其他檔案／軸仍分別觀測，不宣稱
整份評估是 vault、RAG 與 SQLite 的全域原子 snapshot。

## 任務結果與元件邊界

scheduler 在 task.action 返回後，以既有 record_artifact 寫入 maintenance_result，保留
當時 status／summary。明示 failed 在 raise 前先記錄；執行中直接拋錯則看 trace error，
沒有返回結果不合成 task result。記錄失敗只 warning，不改寫任務本身的成功／失敗。

trace_status=succeeded 只表示 run 未拋錯，task 仍可能 skipped 或無候選；舊 run 沒有
maintenance_result 時，task_result 保持 null，不拿目前 maintenance state 補造歷史結果。

M2 依軸附加能力限制與相關參照，最多顯示 12 runs／9 artifacts／6 failures：

- Cortex：A2 檢索後只裁決已提供段落，可證偽性評分亦無搜尋工具；配合 A2／consolidation／
  decay／ledger 的任務參照。存量、active cohort、補證計數及因果效果仍分開。
- 報告：來源／候選生成與 critique 分開；缺 verdict、模型漏判、parser 與 transport 分開。
  不能把 unparseable 當內容差，或斷言一定與 prompt 無關。產物 ID 供人工核對 B04 類問題。
- 洞察／entropy：operation、來源、時間與 cohort 差異不能混作干預效果；缺來源 baseline
  不等於零。日／週洞察 run 及洞察產物參照供查核。
- 檢索：bench 低分不唯一指向 embedding／索引／reranker；提供 bench／orphan sweep 參照。
- LLM 健康與 decay：分清 provider／執行／解析與校準過程，M3 沒有基礎設施修改能力。

沒有增加模型呼叫或重試；每 flagged 軸仍沿用一次既有診斷呼叫。新增資料不授予工具或
搜尋能力，context 裡的摘要／錯誤明列為待查資料。

## 驗證與交接

新增 22 個 hermetic 案例：限量、時間窗、未評分洞察參照、缺內容欄位、未知與查詢故障、
M1 副本與 M2 不重查、各軸高信心／偽 reviewed、scheduler skipped／failed／exception／
記錄故障，以及 M2→M3 不呼叫編輯器、不保存提案。10/30 Cortex 誤判重播的既有回歸
保留；三個原先允許彙總診斷產生 fixes 的測試，隨契約改為驗假設／查證與 gate。
沒有刪除或放寬原失敗路徑測試。

`make check` exit 0：ruff clean、mypy 174 source files clean，2155 passed、1 skipped、
1 個既有 Chroma warning；`git diff --check` exit 0。

本段沒有操作 live M1／M2／M3、改模型、重啟 daemon 或寫入 live vault／Chroma。
載入本版後才會開始產生 maintenance_result 與新診斷 context；真模型診斷品質及新報告的
live 驗收尚待正常樣本。已存在的待審提案另由 B03 逐筆 review，不在此批自動處置。
