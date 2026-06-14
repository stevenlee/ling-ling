# 自我評估 → 自我改善：Metacognition 層（實施計劃）

> 狀態：**M1（統一自評器）實作中（2026-06-14）。** M2–M4 規劃中。
>
> 方向：讓系統「能自動評估、能自動改善」。本計劃遵循專案既定的
> **Nervous System First** 原則——感覺先於行動，低層是高層的前提。
> 自動「評估」是感覺層，必須先於自動「改善」（行動層）落地。

## 為什麼需要這層

盤點後，系統其實**已有不少自我評估訊號**，也有**6 條窄迴路**在自我調節：

| 既有閉迴路 | 來源 → 目標 | 機制 |
|---|---|---|
| 衰減校準 | decay transitions → `params.base_days` | revival rate vs 目標，±20% damped，30 天一次 |
| 帳本嚴格度 | ledger merges/unmerges → `adjudication_strict` | unmerge 率 hysteresis |
| Bench 自長 | bench history → `retrieval_bench_auto.yml` | 加入今天能過的新案例，每週 |
| Bench 回歸告警 | pass_rate 前後比 → 告警報告 | 掉了就寫告警 |
| 同溫層金絲雀 | insights 訊號（grounded vs cold novelty）→ 告警 | M1 之後才有用，僅告警不自調 |
| Cortex 證偽 | 獨立矛盾 → page status | LLM 驗證 + 冷卻期 |

**缺口有二**：
1. **沒有統一的評估視圖**。訊號散落在 6 個子系統，沒有一份「系統現在健康嗎」的總覽。使用者要看 6 個地方。
2. **沒有改善「生成品質」的迴路**。既有自調都只動**單一數值參數**；沒有任何迴路會根據累積的 `quality_verdict` 去改善 **prompt / template / persona / 檢索設定**。Parking lot 早就記著「Planner learning loop：把 verdict 餵回去」——一直沒做。

Metacognition 層補這兩個缺口：**先把所有訊號聚合成一份自評（M1），再從自評診斷出哪個生成元件systematically 表現差（M2），產出具體改善提案（M3，人工閘），最後在嚴格校準邊界內自動套用安全的數值調整（M4，flag + 對照組 + 回退）。**

## 反漂移 / 反同溫層的硬條款

自我改善最大的風險是**自我印證的失控迴路**——系統用自己產生的指標，去調整自己，越調越偏。沿用 F1 的防禦思路：

1. **評估與改善分離**：M1 純讀、零副作用、永遠安全。任何「改」都在 M3 之後，且預設關。
2. **改是提案，不是靜默套用**：M3 把提案寫進 `_pending` 審核佇列（沿用 Profile `_pending` 先例），人核可才生效。
3. **數值自調必須有阻尼 + 對照 + 回退**：M4 只動數值旋鈕，沿用衰減校準的 damped 模式，且每個自調都要有一個「不自調的對照」與一條回退路徑（沿用 F1 cold control + canary 模式）。
4. **不可用單一指標自我獎勵**：改善的判準要多訊號交叉（例如 verdict 改善**且** groundedness 沒掉**且** novelty 沒掉），避免 Goodhart。
5. **所有被壓抑/截斷的事實都要寫出來**：報告若因 top-N、取樣、視窗而漏看，明說，不要讓「沒提到」被讀成「沒問題」。

## 相位

```
M1  自評器（感覺層）        統一聚合所有訊號 → 週報健康計分卡        ← 純讀，本次實作
M2  診斷                    從計分卡找出最差的生成元件（template/op/persona/檢索）
M3  改善提案（人工閘）       對最差元件產出具體修訂 → _pending 審核佇列
M4  邊界內自動套用（flag）   只動安全數值旋鈕，阻尼+對照+回退，預設關
```

每一相獨立可出貨，且**下一相開工前，上一相要先有真實使用驗證**（鋪軌原則）。

## M1 — 統一自評器（本次）

`maintenance/self_assessment.py`：`run_self_assessment(trace_store, *, cortex_dir, insights_dir, ...) -> SelfAssessmentResult`。
**零 LLM 呼叫**，純聚合既有訊號。仿 `routing_report` 的形狀（dataclass result + 可注入路徑 + 一行進 maintenance log + 有 actionable 才寫完整報告）。

聚合的六軸（全部用既有 API / 既有檔案）：

| 軸 | 來源 | 關鍵數字 |
|---|---|---|
| 報告品質 | `trace_store.query_all_artifacts`（新增唯讀 helper） | 各 type 的 keep/revise/reject 數；revise+reject 率 |
| LLM 健康 | `trace_store.llm_call_health`（新增唯讀 helper） | 錯誤率、token 花費、各 stage 分佈 |
| 檢索 | `bench_history.json` | 最新 pass_rate、與前次趨勢、facet_lift、是否回歸 |
| Cortex 信念 | `scan_tensions` + ledger state | 總頁數、矛盾、教條、薄證據、已證偽、嚴格模式 |
| 記憶衰減 | `cortex_decay_state.json` | transition 數、上次校準、現行 base_days |
| 洞察品質 | Insights frontmatter `signals` | 平均 novelty/groundedness、refute 存活率、grounded vs cold 計數 |

輸出：
- **計分卡**：每軸一個狀態燈（🟢/🟡/🔴 由 deterministic 規則決定）+ 關鍵數字。
- **觀察**：deterministic 規則把紅燈轉成「看這裡」的具體條目（例：「template X：6 篇 4 篇 revise → 檢視其 prompt」「bench 回歸 0.95→0.73」「Cortex 有 3 條教條主張」）。**這是 M2 的種子，但只陳述觀察，不採取行動。**
- 一行摘要進 `maintenance.log.md`；完整報告只在「有紅/黃燈」時寫到 `fromLingLing/`（安靜週保持安靜）。

排程：`self_assessment_weekly`（仿 `routing_report_weekly`，idle、dreaming window、無需個別 flag——唯讀報告，受 `MAINTENANCE_SCHEDULER_ENABLED` 總開關管即可）。

## M2–M4（規劃，待 M1 跑出真實資料後細化）

- **M2 診斷**：把 M1 的紅燈軸交給一次 LLM 推理（精簡 `complete`，非 answer_query），產出結構化「根因 + 候選改善」。輸入是聚合數字 + 表現最差元件的實際 prompt/template 文字。
- **M3 提案（人工閘）**：對最差的 template/operation，產生修訂草稿寫入 `Templates/.../_pending/`（或對應佇列），附「為什麼、改了什麼、預期改善哪個指標」。`@ling-` 指令一鍵核可。**永不靜默改 prompt。**
- **M4 數值自調（flag，預設關）**：只對安全旋鈕（如 `SEARCH_DEPTH`、`CORTEX_GROUND_FRACTION`、bench 取樣數）做 damped 自調，每個都要對照 + 回退 + canary。沿用衰減校準的精確模式。

## 測試與驗證

- M1：tmp fixtures（假 trace_store、假 state 檔、假 insights）驗證每軸聚合、狀態燈規則、actionable gating、fail-open（任一來源壞掉不可拖垮整份報告）。
- Live：對真實 DATABASE_DIR + Cortex + Insights 跑一次，人眼看計分卡是否反映現況。
