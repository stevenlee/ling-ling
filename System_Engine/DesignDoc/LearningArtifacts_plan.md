# Learning Artifacts — 視覺/學習輔助層（規格）

> 狀態：**規格（待實作）**。範圍：① 學習產物 Router、③ 論證圖。② 概念卡 + 間隔重複**暫緩**（整合 Anki/外部會把系統變複雜）。
> 核心使命：Ling-Ling 幫人**學**——縮短理解、組織、批判思考、找出隱藏邏輯。現況只會 Mermaid flowchart,不足。

## 0. 重新框定（不是「缺圖種」）

- Mermaid 本身就支援約 10 種圖（flowchart / mindmap / timeline / stateDiagram / quadrantChart / sankey / xychart / ER / sequence …）。系統**不是只能畫流程圖,是只用了 flowchart**。
- 真正缺口：沒有「**內容的認知結構 → 對的學習產物**」的對應。現在的 `Guidelines/Visualization.md` 注入的是「有助於理解就加個 Mermaid」一刀切。
- 因此核心是一個 **router**：判斷結構 → 選對的產物 → 渲染 → 驗證。接上既有 Operations / capability 框架（compare/classify/outline 已是 Operations）。

## 1. 架構：classify → render → validate

```
content ──▶ (1) classify 認知結構 ──▶ artifact type(s) + 信心
                                         │
                              ┌──────────┴───────────┐
                              ▼                      ▼
                       per-type renderer        none（不硬塞圖）
                              │
                              ▼
                    validate / repair（擴張既有 markdown quality checker）
                              │
                              ▼
                    附到 note 的「## 🖼️ 學習輔助」節（可重生）
```

- **classify**：用 R4 的精簡 `LLMClient.complete` + structured output（`_complete_json`）回傳 `{type, confidence, reason}`，**不要**走 answer_query（避免 Visualization/template 樣板污染——recall 的教訓）。
- **render**：每個 type 一個 prompt/模板,輸出結構化欄位,**版面由模板/程式組,不讓 LLM freehand**。
- **validate**：Mermaid 走既有 quality checker（已會修 fence/label/箭頭）；擴張成「每圖種 linter」。

## 2. 規格 ①：學習產物 Router

### 2.1 Artifact type 選單 + 選擇規則 + 基材
| type | 何時選 | 渲染基材（穩健優先） |
|---|---|---|
| `comparison_table` | 內容在比較 ≥2 個對象的多個維度 | **Markdown 表格**（最穩,重用 Compare operation 輸出形狀） |
| `flowchart` | 流程 / 因果序列 / 步驟 | Mermaid flowchart |
| `mindmap` | 一個主題的階層分解 | Mermaid mindmap |
| `timeline` | 時序 / 階段 / 歷史 | Mermaid timeline |
| `quadrant` | 物件落在 2 軸 / 取捨空間 | Mermaid quadrantChart |
| `concept_map` | 網狀關係（非序列） | Mermaid graph + typed edges |
| `argument_map` | 論證性內容 | → 規格 ③ |
| `none` | 沒有強視覺結構 | **不產圖**（重要：不硬塞是有效輸出） |

### 2.2 整合點
- **Auto**：`ingestion_pipeline._write_synthesis` 與 insight 生成——以 router 取代現有「有幫助就加 Mermaid」。先 **flag 後 default**（`VISUAL_ROUTER_ENABLED`,初期 off,驗證後開,避免擾動既有 synthesis 輸出）。
- **On-demand**：新指令 `@ling-visualize [[doc]]`（新 intent + agent）——重生任一筆記為最佳產物;支援指定 `as timeline` 等強制類型。

### 2.3 穩健性硬條款
- classify 走 `_complete_json`,不走 answer_query。
- 任何 Mermaid 產物必須過 linter,失敗則降級為文字大綱（不輸出壞圖）。
- `none` 必須是真選項,classify prompt 明文「沒有強結構就回 none,不要硬湊」。

### 2.4 測試 / 驗收
- 分類映射：比較文→table、歷史文→timeline、流程文→flowchart、鬆散散文→none。
- 每個 renderer 產出可解析的產物;壞 Mermaid 被 linter 擋下降級。
- Live：對真實 vault 數篇不同結構的文件跑,人工確認選型合理。

## 3. 規格 ③：論證圖（批判性思考 + 找出隱藏邏輯）

### 3.1 結構（Toulmin）
```
主張 Claim
  ├─ 根據 Grounds（證據）           ← 系統已抽取
  ├─ 隱含前提 Warrant（連結根據→主張的未明說假設）  ← 新增,這是「隱藏邏輯」
  ├─ 適用條件 Qualifier            ← 重用 Cortex applies_when
  └─ 反駁 Rebuttal（矛盾 / 反例）    ← 重用 falsifier + contradictions
```

### 3.2 重用既有抽取,value-add 是 Warrant
- 系統已抽：claim / evidence / falsifier / contradiction / applies_when。
- **論證圖唯一的新 LLM 工作 = 抽出 warrant**（連結證據到主張的未明說前提）+ 標示「最弱的一環」（哪個 warrant 未明說且可爭議）。這直接服務「找出隱藏邏輯」。
- 來源最乾淨的是 **Cortex 主張**（資料幾乎齊全,只缺 warrant）;也可跑在 synthesis / insight 上。

### 3.3 渲染（穩健優先）
- **預設：結構化 Markdown Toulmin 區塊**（callout/表格,最穩、最可讀）。
- **選配：Mermaid graph**（主張節點、grounds、warrant 虛線、rebuttal 紅色）——過 linter,失敗就只留文字版。
- 明確標注 unstated warrant 與 qualifier,讓使用者看到「沒說出口的前提」。

### 3.4 整合
- 作為 router 的一個 type（argumentative 內容自動 dispatch）。
- on-demand 經 `@ling-visualize [[doc]] as argument`。
- 與 critique / refute / tensions 同源——共用既有抽取,不重造。

### 3.5 測試 / 驗收
- 給 claim+evidence → 抽出合理 warrant;falsifier/contradiction 正確進 rebuttal。
- 無證據 → 優雅標「根據不足」而非編造。
- Live：對一條真實 Cortex 主張產論證圖,人工確認 warrant 確實是未明說前提。

## 4. 共通原則（這次 session 的教訓）
- **基材穩健度排序**：Markdown 表格/結構塊 > 簡單 Mermaid > 複雜 Mermaid > LLM 手寫 SVG（脆,後置且須驗證）。
- **結構化輸出 + 程式組版**,別讓 LLM freehand 排版。
- **不硬塞**：`none` 是一等公民。寧可不產圖,也不要產一張誤導/壞掉的圖。
- classify/render 一律走 `_complete_json` / `complete`,不經 answer_query 的文件樣板。

## 5. 建議順序
1. **① Router 骨幹**（classify → 表格 + 既有 flowchart + `none`）——先把「選型」這件事立起來,最小可用。
2. **加 Mermaid 多樣性**（mindmap / timeline / quadrant / concept_map）掛上 router。
3. **③ 論證圖** 作為旗艦 type（最高批判思考價值,重用最多既有抽取）。
4. `@ling-visualize` on-demand 指令 + 驗證後開 auto flag。

## 6. 待你拍板的開放問題
1. **Auto vs on-demand 先後**：先做 `@ling-visualize` 指令（安全、不動既有 synthesis 輸出),還是直接讓 synthesis/insight 自動附產物（影響面大、需 flag）?我傾向**先 on-demand**。
2. **論證圖渲染**：純結構化 Markdown（最穩）就夠,還是一定要 Mermaid graph?我傾向**先 Markdown,Mermaid 選配**。
3. **是否設為新的「Phase 6」軸**,還是當 backlog 批次逐步出貨?
