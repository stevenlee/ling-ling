# Cortex Phase 2.5 — 可反駁性訊號與抽取錨點（工作計劃）

> Status: **planned 2026-06-11, not yet implemented**。
> 緣起：首輪加速驗證後 Steven 對 Cortex 頁的評價——「全部保留、
> 都有點道理、但太抽象不知如何實施」。共同診斷：**「有道理＋
> 無法反駁＋無法實施」是占星術特徵組合**——模糊是對 refute 的天然
> 護甲，而現有四訊號（groundedness/novelty/bridging/refute）都
> 量不出空靈。survival 投票因此失去鑑別力（6/6 存活）。
> 修量尺優先於修管線。
> 必讀：① Engineering_Conventions.md ② CortexMemory_implementation_plan.md
> 核心不變量 ③ 本文件。分支 `cortex/phase-2.5`，review 兩輪上限。

## 0. 範圍與不做

**做**：第五訊號（falsifiability）、抽取錨點（applies_when）、
證據鏈穿透、量尺修缺、既有頁面回補評估。

**不做**（碰了退回）：
- ❌ 生成端配比（ε 探索、doc-anchored 配額）——另案（見 §6 backlog）
- ❌ Phase 3（S/R 衰減）、Phase 4（falsified 管線）
- ❌ 刪除或降權既有 Cortex 頁（記錄哲學：量出來，不私刑）
- ❌ 修改既有測試

## 1. D1 — 第五訊號：可反駁性（falsifiability）

**操作化 Popper**：一個主張有經驗內容，若且唯若能描述「推翻它的
具體觀察」。

### 1.1 `llm_client.assess_falsifiability(claim) -> dict`

```python
{"score": float 0–1, "falsifier": "<一個能推翻此主張的具體觀察，≤200字>"}
```

- Prompt 要求：先嘗試寫出 falsifier；寫不出或只能寫出同義反覆
  （「當它不成立的時候」）→ score 低。評分基準錨定在 prompt 內：
  - 1.0：falsifier 是具體、可觀察、可指認的情境
  - 0.5：falsifier 存在但需要再操作化才能檢驗
  - 0.0：主張不可反駁（全稱模糊語、定義式真理、價值宣言）
- temperature 0.1，stage `assess_falsifiability`，解析失敗 →
  `{"score": None, "falsifier": ""}`（fail-open，缺值不是最差值——
  Phase 1 review S1 的既定原則）。

### 1.2 落點：鞏固時評估，存進頁面

- `cortex_consolidation.process_claim()` 在建新頁**前**呼叫一次
  （merge 路徑不重評——主張沒變）。每晚新增 LLM 成本 ≈ 新 claim 數
  ×1 call，受既有 insight 配額自然封頂。
- `CortexPage` 新增欄位（**additive，不 bump schema_version**，
  舊頁 parse 得到預設值）：
  ```yaml
  falsifiability: 0.5        # float | null
  falsifier: "<文字>"         # str，可空
  ```
- **初始 confidence 與 falsifiability 掛鉤**：
  `confidence_0 = 0.3 + 0.4 × score`（score None → 維持 0.5）。
  不可反駁的主張以低 confidence 進場，而非被拒——記錄哲學。
- round-trip 測試擴充涵蓋新欄位。

## 2. D2 — 抽取錨點：applies_when

### 2.1 `extract_claims` prompt 修訂

- 輸出 schema 增為
  `[{"claim", "summary", "applies_when": "<此主張適用的具體情境/條件>"}]`。
- Prompt 明文糾偏：**「原子」≠「無條件全稱」**——
  「在 X 情境下，A 導致 B」比「A 的本質是 B」更原子也更有用；
  鼓勵條件式主張，主張內可含具體實體與條件。
- applies_when 缺失不擋（fail-open），存空字串。

### 2.2 頁面呈現（確定性 round-trip 規格）

Core Claim 節格式擴充——parser 規則必須無歧義：

```markdown
## Core Claim
<claim 單行>
> 適用情境：<applies_when>      ← 可選；以 "> 適用情境：" 前綴辨識
```

- parse：節內第一個非空、非 blockquote 行 = claim；
  `> 適用情境：` 前綴行 = applies_when。
- `CortexPage` 新增 `applies_when: str = ""`，render/parse 對稱，
  round-trip 測試涵蓋「有/無 applies_when」兩態。

## 3. D3 — 證據鏈穿透到底

- evidence dict 已有 `sources`；本項補強：claim 級 evidence 的
  sources 除 insight frontmatter 的 related keys 外，**追加解析
  insight 內文中的 `[[wikilink]]`**（去重、上限 5 條、僅保留
  vault 中實際存在的頁——重用 Phase 1 groundedness 的存在性檢查
  路徑）。「如何實施」的脈絡從此一跳可達。
- Vault 型 insight 若無任何可解析來源，sources 維持 []——
  生成端的根治屬 §6 backlog。

## 4. D4 — 量尺修缺（驗證工具）

首輪驗證暴露的兩個 gauge defect：

1. **斷鏈率計算範圍**：`cortex_validation` 的
   `broken_link_insight_rate` 只統計**過閘**（成為鞏固候選）的
   insights——被閘門擋掉的 planner 文件本來就該斷鏈，混入只會
   製造假黃線（首輪 90% 即此假象）。
2. **Refute 覆蓋揭露**：報告增列 `refute_coverage`（有 verdict 的
   比例）——Vault 型 insight 因無來源跳過 refute 是 M4 既定行為，
   但覆蓋率必須可見，否則「存活率」會在低覆蓋時誤導。
3. 報告增列 **falsifiability 分佈**（mean / <0.3 比例）與
   **黃線：falsifiability mean < 0.4**。
4. 人工抽查清單每條 claim 後附 falsifier——讓「這條可以怎麼被
   推翻」成為你掃讀時的判準提示。

## 5. D5 — 既有頁面回補

- 一次性腳本（或併入 consolidation 的 lazy 路徑）：對現存
  6 頁跑 `assess_falsifiability`，寫入 frontmatter。
  confidence **不回溯調整**（它們已有 reconsolidation 歷史，
  重算會破壞既有訊號）；只補測量值。
- 回補後跑一次 validation 報告，把 6 頁的 falsifiability 分佈
  作為本計畫的「before」基線存檔。

## 6. Backlog（明確另案，不在本計畫）

- **生成端配比**：doc-anchored insight 配額 / ε 探索 / 興趣加權
  抽樣——治「素材太空靈」的根。建議在 Phase 2.5 落地、累積兩週
  falsifiability 數據後立案，屆時用數據驗證「doc-anchored 素材
  產出更可反駁的主張」這個假設本身。
- 夜間再驗證（Phase 3 §6）時要求補具體應用例——併入 Phase 3。

## 7. 驗收標準

1. 全套既有測試綠（768 起跳），零修改既有測試。
2. 新測試（hermetic）至少涵蓋：
   - assess_falsifiability 解析（正常/亂格式→None/MagicMock）
   - confidence_0 公式（score 0 / 0.5 / 1 / None 四點）
   - round-trip：falsifiability/falsifier/applies_when 三欄位
     有值與預設兩態
   - Core Claim 節的 applies_when 確定性解析（有/無/異常前綴）
   - extract_claims 新 schema（applies_when 缺失 fail-open）
   - evidence sources 的 wikilink 穿透（存在性過濾、上限 5）
   - 量尺：斷鏈率僅計過閘者、refute_coverage、falsifiability 黃線
   - merge 路徑不重評 falsifiability（零額外 LLM call）
3. flag：沿用 `CORTEX_CONSOLIDATION_ENABLED`，不另設新 flag
   （falsifiability 是鞏固的一部分，不是可選配件）。
4. 文件：README Refactor Notes 一節；本檔狀態更新。
5. Commit 範圍自查；分支 `cortex/phase-2.5`。

## 8. 完成的判定（與驗證框架銜接）

落地後重跑加速驗證（含 D5 回補基線）：
- 新主張的 falsifiability mean ≥ 0.4（黃線）
- 既有 6 頁的分佈作為對照——**預期它們大多 < 0.3**；如果回補
  後它們普遍拿高分，代表第五訊號量不出我們要它量的東西，
  量尺本身退回重設計。

## 9. 分工

建議沿用輪替：**Gemini 實作、Claude 審查**（Phase 1 模式）。
Review 清單即本檔 §7 + Engineering_Conventions 全條款；
特別盯：round-trip 對稱性、merge 路徑零額外 call、量尺範圍修正。
由 Steven 最終指定。
