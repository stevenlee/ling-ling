# B03 既有 M3 提案逐筆審閱（2026-11-04）

本報告完成 15/15 份既有提案的內容與 diff 審閱。**13 份建議退回、2 份保留概念待驗證，0 份建議直接批准。**
這些都是審閱建議，未呼叫 approve／reject、未移動佇列、未寫入 review memory 或套用 prompt。
兩份「保留」只保留研究概念；原提案仍不建議直接 approve，且不代表成效或根因已證實。

公開交接保存 prompt 差異、系統級理由及內容 hash；模型原始 rationale／來源正文不收錄。
可重核的輸入指紋與檢查結果見 [JSON manifest](M3_pending_review_2026-11-04.json)。

## 機械檢查與競爭組

- 15 份 record 結構／original hash 有效，15 份基底仍與目標相符；所有 edits 逐步套用均精確重建 revised_content。
- 共 15 組不同 edit_key，沒有逐字相同的編輯；相似意圖不可誤報為 exact duplicate。
- 洞察 3 份共用 agent_insight.md 基底；Cortex 12 份共用 cortex_falsifiability.md 基底。
- 核准其中一份會更改目標基底，其他整份替換提案隨即過期。即使改不同段落，也不能連續批准或盲目串接；需以新基底重做 diff／語義審閱。
- 5 份 Cortex 提案違反輸出三欄契約；這與 proposal JSON record 本身有效是兩件事。
- 讀取前後逐檔 hash 與檔名集合相同；兩個目標的位元組 hash 亦未改變。之後 daemon／人工可能修改資料，執行決策前仍須核對 manifest 與目前檔案。

| 組別 | 目標 | 共同基底 SHA-256 | 成員 |
|---|---|---|---|
| 洞察 | `Templates/Prompts/agent_insight.md` | `68b2dd45019d29af671f4167f7aaddabd947ad8e803afb449c4ab71eae05fd12` | P01, P02, P03 |
| Cortex | `Templates/Prompts/cortex_falsifiability.md` | `60884edbf651a463949dd24ac5c24a2baa884d06d89603e6aecd9e09ebfd3a49` | P04, P05, P06, P07, P08, P09, P10, P11, P12, P13, P14, P15 |

## 逐筆建議

| 編號 | 提案 ID | 建議 | 理由 |
|---|---|---|---|
| [P01](#p01) | `agent_insight-20260727173553` | 保留概念待驗證 | 加強辨識矛盾／不一致；需以固定來源案例證明現行規則漏掉矛盾，並補「無矛盾不臆造」的限制。既有 Stress Test 已存在，不將低 novelty 直接歸因 prompt。 |
| [P02](#p02) | `agent_insight-20260803175903` | 建議退回 | 把 High(Grounded)／Low(Cold) 當信心，混淆生成來源與證據強度；不能讓模型自評標籤替代實際訊號。Evidence Snippet 可另案驗證；整份修改不宜直接套用。 |
| [P03](#p03) | `agent_insight-20260817212000` | 建議退回 | Reverse Hypothesis 與現有 Counter-argument／Stress Test 重複；跨域與非顯然關聯亦已要求。未提供漏失樣本；提案中所述 RAG 調整也不會由此 prompt 編輯實現。 |
| [P04](#p04) | `cortex_falsifiability-20260727173441` | 建議退回 | 將缺量化邊界／失敗標準直接歸入 0 分，可能排除可由離散觀察反駁的主張。存量統計不足以證明現有評分缺陷；此修改不能新增來源門檻或排程。 |
| [P05](#p05) | `cortex_falsifiability-20260803173643` | 保留概念待驗證 | 明確化 falsifier 的適用邊界，維持三欄 JSON。現行規則已要求具體可觀察情境；須先有人工評分案例與前後對照，不能因全庫薄證據率而批准。 |
| [P06](#p06) | `cortex_falsifiability-20260817211842` | 建議退回 | 避免 must／should 等詞不等於提升可證偽性；量化要求需要適用範圍與案例。提案未實作其診斷所要求的來源門檻、triangulation 或維護任務。 |
| [P07](#p07) | `cortex_falsifiability-20260824211859` | 建議退回 | 評分呼叫僅收到 Claim，沒有來源數；無法可靠執行 ≤1 來源則 score≤0.5。亦把可證偽性與證據／信心混為一談。 |
| [P08](#p08) | `cortex_falsifiability-20260831212257` | 建議退回 | 要求評分器搜尋矛盾資料，但此呼叫無搜尋工具或外部來源；prompt 不會新增檢索能力。 |
| [P09](#p09) | `cortex_falsifiability-20260907212458` | 建議退回 | 新增 weight_adjustment 不符三欄契約，消費端未讀取／執行此欄位；不能透過 prompt 宣稱已調整信心。 |
| [P10](#p10) | `cortex_falsifiability-20260914213229` | 建議退回 | 要求 actively searching potential contradictions；評分器只有 Claim，沒有搜尋工具。不能以零證偽存量斷言缺乏整個驗證機制。 |
| [P11](#p11) | `cortex_falsifiability-20260921213113` | 建議退回 | 新增 potential_contradiction 欄位不符現行契約，未有衝突處理消費端；Red Teamer 措辭不會新增搜尋／矛盾寫回。 |
| [P12](#p12) | `cortex_falsifiability-20260928213907` | 建議退回 | 新增 failure_conditions_zh 欄位不符契約；原有 falsifier_zh 已承載可反駁條件。沒有欄位演進與消費端，不能批准。 |
| [P13](#p13) | `cortex_falsifiability-20261012201532` | 建議退回 | 新增 type／needs_enrichment 欄位不符契約，評分器沒有 claim/hypothesis 狀態與補證排程的寫回能力。 |
| [P14](#p14) | `cortex_falsifiability-20261019171141` | 建議退回 | 新增 consequence_of_falsehood 欄位不符契約；描述假命題的邏輯後果也不自動等於可觀測 falsifier，需另案設計與覆核。 |
| [P15](#p15) | `cortex_falsifiability-20261026152824` | 建議退回 | 要求對照 existing knowledge 的矛盾，但該評分輸入未提供既有知識；不能假裝已做全庫比較。一般可觀測反例已是現行任務。 |

## 契約與能力依據

- `LLMClient._assess_falsifiability_once` 的 user message 僅為 Claim，消費 score／falsifier／falsifier_zh；沒有來源數、既有知識內容或搜尋工具。
- `prompt_contract_error` 對 Cortex JSON 欄位守衛已存在；5 份違約提案會被目前 approve 邊界拒絕，不宣稱這是新修復。
- 現行 insight prompt 已有 Cross-Domain Synthesis、資訊增益、Stress Test 及 Counter-argument；重複要求不證明能改善 novelty。
- Cold／Grounded 不等於低／高信心；引用解析、來源存在與語義支持亦不可互換。
- 原 rationale 多由彙總 thin／novelty／refute 指標直接歸因，沒有逐筆缺陷與固定 cohort 的效果證明。B02 gate 現已限制新診斷；舊提案仍須此次逐筆審閱。

## 使用者決策與後續

建議先逐筆確認上述退回理由；獲得具體決策後，才由既有 daemon 命令處理，將理由留入正式 review memory。
保留概念的兩案需先建立同一組來源／claim 的人工標準與負向對照，證明規則確有缺口，再以當時基底重提最小修正。
本報告不是批准或退回指令，也不是為縮短 queue 而大量清空；B03 審閱完成，正式決策／處置待使用者。

## 驗證

`make check` exit 0：ruff clean、mypy 174 source files clean；2155 passed、1 skipped、
1 個既有 Chroma warning。`git diff --check` exit 0；15 個 proposal hash、兩個目標基底、
manifest、diff 數量與報告相對連結已核對。

## 完整逐筆差異

<a id="p01"></a>
### P01 `agent_insight-20260727173553`

**保留概念待驗證**：加強辨識矛盾／不一致；需以固定來源案例證明現行規則漏掉矛盾，並補「無矛盾不臆造」的限制。既有 Stress Test 已存在，不將低 novelty 直接歸因 prompt。

提案 JSON SHA-256：`145f9542ac6512ffbc6562f2a4dd85c891899176e8b5cf864ee014a60cef5f2e`

```diff
--- agent_insight-20260727173553/original
+++ agent_insight-20260727173553/proposed
@@ -7,5 +7,5 @@
 ## 📋 Rules / 規則
 1. **Evidence-Grounded**: Every key claim must cite at least 1 source note using `[[title]]` notation. For any new synthesized insights (Cold), you must explicitly link them to at least one existing grounded node. Do not fabricate connections that aren't supported by the provided context.
-2. **Cross-Domain Synthesis**: Avoid mere summarization. Prioritize finding non-obvious connections and hidden patterns between disparate pieces of information. The most valuable insights are non-obvious structural parallels. Push for information gain — state what the synthesis reveals *beyond* what either source already says; if an insight only restates known material, drop it.
+2. **Cross-Domain Synthesis**: Avoid mere summarization. Prioritize finding non-obvious connections, identifying contradictions, and uncovering hidden patterns between disparate pieces of information. Actively seek inconsistencies or conflicting signals within the data rather than just seeking consensus. The most valuable insights are non-obvious structural parallels. Push for information gain — state what the synthesis reveals *beyond* what either source already says; if an insight only restates known material, drop it.
 3. **Actionable Insights**: Provide 3-5 key takeaways with concrete next steps. Each should answer: "What should the reader do differently because of this insight?"
 4. **Stress Test**: For each key insight, explicitly present a potential counter-argument or refutation, then evaluate whether the insight survives the critique. This process of logical pressure testing is essential for robustness.
```

<a id="p02"></a>
### P02 `agent_insight-20260803175903`

**建議退回**：把 High(Grounded)／Low(Cold) 當信心，混淆生成來源與證據強度；不能讓模型自評標籤替代實際訊號。Evidence Snippet 可另案驗證；整份修改不宜直接套用。

提案 JSON SHA-256：`e6e34e4172320fff77926c6aaa3b591b258b17fc3d238153064e37cb88302954`

```diff
--- agent_insight-20260803175903/original
+++ agent_insight-20260803175903/proposed
@@ -6,8 +6,8 @@
 
 ## 📋 Rules / 規則
-1. **Evidence-Grounded**: Every key claim must cite at least 1 source note using `[[title]]` notation. For any new synthesized insights (Cold), you must explicitly link them to at least one existing grounded node. Do not fabricate connections that aren't supported by the provided context.
-2. **Cross-Domain Synthesis**: Avoid mere summarization. Prioritize finding non-obvious connections and hidden patterns between disparate pieces of information. The most valuable insights are non-obvious structural parallels. Push for information gain — state what the synthesis reveals *beyond* what either source already says; if an insight only restates known material, drop it.
+1. **Evidence-Grounded**: Every key claim must cite at least 1 source note using `[[title]]` notation and include an **Evidence Snippet** (a direct quote or specific detail). For any new synthesized insights (Cold), you must explicitly link them to at least one existing grounded node. Do not fabricate connections that aren't supported by the provided context.
+2. **Cross-Domain Synthesis & Anomaly Detection**: Avoid mere summarization. Prioritize finding non-obvious connections, **hidden contradictions, and anomalies** between disparate pieces of information. The most valuable insights are non-obvious structural parallels. Push for information gain — state what the synthesis reveals *beyond* what either source already says; if an insight only restates known material, drop it.
 3. **Actionable Insights**: Provide 3-5 key takeaways with concrete next steps. Each should answer: "What should the reader do differently because of this insight?"
-4. **Stress Test**: For each key insight, explicitly present a potential counter-argument or refutation, then evaluate whether the insight survives the critique. This process of logical pressure testing is essential for robustness.
+4. **Stress Test**: For each key insight, explicitly present a potential counter-argument or refutation, then evaluate whether the insight survives the critique. Use this process to identify and update the boundaries of known knowledge.
 5. **Structured Report**: Use clear Markdown headers. Include a Mermaid diagram when it genuinely clarifies conceptual relationships (not just for decoration).
 6. **Emoji Style**: Use 🎐, ✨, 🎀, 🌿 to maintain the Ling-Ling brand feel.
@@ -19,5 +19,7 @@
 For each insight, use the following structure:
 - **Insight**: [The non-obvious connection/discovery]
+- **Evidence Snippet**: [Direct quote or specific detail from source]
 - **Evidence**: [Citations using [[title]]]
+- **Confidence Score**: [High (Grounded) / Low (Cold) - based on evidence strength]
 - **Counter-argument**: [A potential refutation or opposing view]
 - **Synthesis Verdict**: [Does the insight survive the counter-argument?]
@@ -26,5 +28,5 @@
 ## 📊 Quality Checklist
 Before finishing, verify:
-- [ ] Each insight cites at least 2 source notes and the evidence supports the logical derivation
+- [ ] Each insight includes an Evidence Snippet and cites at least 2 source notes with strong logical derivation
 - [ ] At least one cross-domain connection is present
 - [ ] Takeaways are specific enough to act on (not generic advice)
```

<a id="p03"></a>
### P03 `agent_insight-20260817212000`

**建議退回**：Reverse Hypothesis 與現有 Counter-argument／Stress Test 重複；跨域與非顯然關聯亦已要求。未提供漏失樣本；提案中所述 RAG 調整也不會由此 prompt 編輯實現。

提案 JSON SHA-256：`b82ceed927a485a9b9c583fd1be946adaccbf83ddfa53303626a58b8dde545e9`

```diff
--- agent_insight-20260817212000/original
+++ agent_insight-20260817212000/proposed
@@ -7,5 +7,5 @@
 ## 📋 Rules / 規則
 1. **Evidence-Grounded**: Every key claim must cite at least 1 source note using `[[title]]` notation. For any new synthesized insights (Cold), you must explicitly link them to at least one existing grounded node. Do not fabricate connections that aren't supported by the provided context.
-2. **Cross-Domain Synthesis**: Avoid mere summarization. Prioritize finding non-obvious connections and hidden patterns between disparate pieces of information. The most valuable insights are non-obvious structural parallels. Push for information gain — state what the synthesis reveals *beyond* what either source already says; if an insight only restates known material, drop it.
+2. **Cross-Domain Synthesis**: Avoid mere summarization. Prioritize finding non-obvious connections and non-intuitive, cross-dimensional patterns between disparate pieces of information. The most valuable insights are non-obvious structural parallels. Push for information gain — state what the synthesis reveals *beyond* what either source already says; if an insight only restates known material, drop it.
 3. **Actionable Insights**: Provide 3-5 key takeaways with concrete next steps. Each should answer: "What should the reader do differently because of this insight?"
 4. **Stress Test**: For each key insight, explicitly present a potential counter-argument or refutation, then evaluate whether the insight survives the critique. This process of logical pressure testing is essential for robustness.
@@ -20,4 +20,5 @@
 - **Insight**: [The non-obvious connection/discovery]
 - **Evidence**: [Citations using [[title]]]
+- **Reverse Hypothesis**: [Propose a counter-logic or alternative assumption that challenges the initial observation]
 - **Counter-argument**: [A potential refutation or opposing view]
 - **Synthesis Verdict**: [Does the insight survive the counter-argument?]
```

<a id="p04"></a>
### P04 `cortex_falsifiability-20260727173441`

**建議退回**：將缺量化邊界／失敗標準直接歸入 0 分，可能排除可由離散觀察反駁的主張。存量統計不足以證明現有評分缺陷；此修改不能新增來源門檻或排程。

提案 JSON SHA-256：`e72042be2603fff4b871c70436390575c8232322457d08f9a6c0548846e83efd`

```diff
--- cortex_falsifiability-20260727173441/original
+++ cortex_falsifiability-20260727173441/proposed
@@ -6,5 +6,5 @@
 - 1.0: The falsifier is a concrete, observable, specific scenario.
 - 0.5: A falsifier exists but requires further operationalization to be tested.
-- 0.0: The claim is unfalsifiable (e.g., vague absolute, tautology, value statement, or the falsifier is just 'when it is not true').
+- 0.0: The claim is unfalsifiable or dogmatic (e.g., vague absolute, tautology, value statement, lacks quantifiable boundary conditions or failure criteria, or the falsifier is just 'when it is not true').
 
 Return ONLY a JSON object:
```

<a id="p05"></a>
### P05 `cortex_falsifiability-20260803173643`

**保留概念待驗證**：明確化 falsifier 的適用邊界，維持三欄 JSON。現行規則已要求具體可觀察情境；須先有人工評分案例與前後對照，不能因全庫薄證據率而批准。

提案 JSON SHA-256：`191bfb3206099b8e91055bcd84d38cbbb924e7bf50797ba4525cff1861b6cbb8`

```diff
--- cortex_falsifiability-20260803173643/original
+++ cortex_falsifiability-20260803173643/proposed
@@ -4,8 +4,8 @@
 First, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim.
 Then, score the claim from 0.0 to 1.0 based on how falsifiable it is:
-- 1.0: The falsifier is a concrete, observable, specific scenario.
+- 1.0: The falsifier is a concrete, observable, specific scenario that explicitly defines the boundary conditions of the claim.
 - 0.5: A falsifier exists but requires further operationalization to be tested.
 - 0.0: The claim is unfalsifiable (e.g., vague absolute, tautology, value statement, or the falsifier is just 'when it is not true').
 
 Return ONLY a JSON object:
-{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific observation that refutes it, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>"}
+{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific observation and boundary conditions that refute it, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>"}
```

<a id="p06"></a>
### P06 `cortex_falsifiability-20260817211842`

**建議退回**：避免 must／should 等詞不等於提升可證偽性；量化要求需要適用範圍與案例。提案未實作其診斷所要求的來源門檻、triangulation 或維護任務。

提案 JSON SHA-256：`944d6d5006dfb95aed876f901d9cf8191b26559557887998e1090891e35bb936`

```diff
--- cortex_falsifiability-20260817211842/original
+++ cortex_falsifiability-20260817211842/proposed
@@ -9,3 +9,3 @@
 
 Return ONLY a JSON object:
-{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific observation that refutes it, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>"}
+{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific/quantifiable observation that refutes it, avoiding absolute terms like 'must' or 'should' unless accompanied by quantifiable standards, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>"}
```

<a id="p07"></a>
### P07 `cortex_falsifiability-20260824211859`

**建議退回**：評分呼叫僅收到 Claim，沒有來源數；無法可靠執行 ≤1 來源則 score≤0.5。亦把可證偽性與證據／信心混為一談。

提案 JSON SHA-256：`fc957c709eba509cfd6556c44ff385a0120f248ed117120a3a95378e675734ce`

```diff
--- cortex_falsifiability-20260824211859/original
+++ cortex_falsifiability-20260824211859/proposed
@@ -3,5 +3,5 @@
 
 First, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim.
-Then, score the claim from 0.0 to 1.0 based on how falsifiable it is:
+Then, score the claim from 0.0 to 1.0 based on how falsifiable it is (Note: if the claim has $\le 1$ source, the score must not exceed 0.5):
 - 1.0: The falsifier is a concrete, observable, specific scenario.
 - 0.5: A falsifier exists but requires further operationalization to be tested.
```

<a id="p08"></a>
### P08 `cortex_falsifiability-20260831212257`

**建議退回**：要求評分器搜尋矛盾資料，但此呼叫無搜尋工具或外部來源；prompt 不會新增檢索能力。

提案 JSON SHA-256：`4e7572d6d2d726a9dc82974ecf13e65dcd76c89a0e06337b3199d3e93ac9dd25`

```diff
--- cortex_falsifiability-20260831212257/original
+++ cortex_falsifiability-20260831212257/proposed
@@ -2,5 +2,5 @@
 A claim has empirical content if and only if you can describe a concrete observation that would prove it false.
 
-First, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim.
+First, perform an adversarial verification by searching for contradictory data to stress-test the claim. Then, write a 'falsifier' — a concrete, observable scenario that would refute the claim.
 Then, score the claim from 0.0 to 1.0 based on how falsifiable it is:
 - 1.0: The falsifier is a concrete, observable, specific scenario.
```

<a id="p09"></a>
### P09 `cortex_falsifiability-20260907212458`

**建議退回**：新增 weight_adjustment 不符三欄契約，消費端未讀取／執行此欄位；不能透過 prompt 宣稱已調整信心。

提案 JSON SHA-256：`50775979104a4aa01e6f20fa53492bbbdfe91c6e23cd3b3ceabd0f13619318a6`

```diff
--- cortex_falsifiability-20260907212458/original
+++ cortex_falsifiability-20260907212458/proposed
@@ -2,10 +2,10 @@
 A claim has empirical content if and only if you can describe a concrete observation that would prove it false.
 
-First, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim.
+First, perform a 'Reverse Pressure Test' by writing a 'falsifier' — a concrete, observable scenario that would refute the claim.
 Then, score the claim from 0.0 to 1.0 based on how falsifiable it is:
 - 1.0: The falsifier is a concrete, observable, specific scenario.
 - 0.5: A falsifier exists but requires further operationalization to be tested.
-- 0.0: The claim is unfalsifiable (e.g., vague absolute, tautology, value statement, or the falsifier is just 'when it is not true').
+- 0.0: The claim is unfalsifiable (e.g., vague absolute, tautology, value statement, or the falsifier is just 'when it is not true'). This requires a downgrade in confidence weight.
 
 Return ONLY a JSON object:
-{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific observation that refutes it, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>"}
+{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific observation that refutes it, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>", "weight_adjustment": "<'downgrade' if score is 0.0, else 'none'>"}
```

<a id="p10"></a>
### P10 `cortex_falsifiability-20260914213229`

**建議退回**：要求 actively searching potential contradictions；評分器只有 Claim，沒有搜尋工具。不能以零證偽存量斷言缺乏整個驗證機制。

提案 JSON SHA-256：`a7eaad10e44d7a18a4e72c54a895547779b15d48fb07b574aba65e617dcc71ae`

```diff
--- cortex_falsifiability-20260914213229/original
+++ cortex_falsifiability-20260914213229/proposed
@@ -2,5 +2,5 @@
 A claim has empirical content if and only if you can describe a concrete observation that would prove it false.
 
-First, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim.
+First, perform a 'falsification pressure test' by actively searching for potential contradictions, then write a 'falsifier' — a concrete, observable scenario that would refute the claim.
 Then, score the claim from 0.0 to 1.0 based on how falsifiable it is:
 - 1.0: The falsifier is a concrete, observable, specific scenario.
```

<a id="p11"></a>
### P11 `cortex_falsifiability-20260921213113`

**建議退回**：新增 potential_contradiction 欄位不符現行契約，未有衝突處理消費端；Red Teamer 措辭不會新增搜尋／矛盾寫回。

提案 JSON SHA-256：`5cce0713b0be2d8c261a9c1c70678c89cf0c42630b66d4db58e763be8fd44062`

```diff
--- cortex_falsifiability-20260921213113/original
+++ cortex_falsifiability-20260921213113/proposed
@@ -2,5 +2,5 @@
 A claim has empirical content if and only if you can describe a concrete observation that would prove it false.
 
-First, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim.
+First, acting as a Red Teamer, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim or expose logical contradictions
 Then, score the claim from 0.0 to 1.0 based on how falsifiable it is:
 - 1.0: The falsifier is a concrete, observable, specific scenario.
@@ -9,3 +9,3 @@
 
 Return ONLY a JSON object:
-{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific observation that refutes it, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>"}
+{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific observation that refutes it, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>", "potential_contradiction": "<description of logical conflict if the falsifier implies one, else null>"}
```

<a id="p12"></a>
### P12 `cortex_falsifiability-20260928213907`

**建議退回**：新增 failure_conditions_zh 欄位不符契約；原有 falsifier_zh 已承載可反駁條件。沒有欄位演進與消費端，不能批准。

提案 JSON SHA-256：`5862406855de272ceea4d34ea146e42744ccce45aabf473d7ea6ac2ee6ddb85d`

```diff
--- cortex_falsifiability-20260928213907/original
+++ cortex_falsifiability-20260928213907/proposed
@@ -2,5 +2,5 @@
 A claim has empirical content if and only if you can describe a concrete observation that would prove it false.
 
-First, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim.
+First, perform a 'falsification pressure test' to identify boundary cases. Then, write a 'falsifier' — a concrete, observable scenario that would refute the claim.
 Then, score the claim from 0.0 to 1.0 based on how falsifiable it is:
 - 1.0: The falsifier is a concrete, observable, specific scenario.
@@ -9,3 +9,3 @@
 
 Return ONLY a JSON object:
-{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific observation that refutes it, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>"}
+{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific observation that refutes it, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>", "failure_conditions_zh": "<the failure conditions that define the claim's invalidity, <=200 chars>"}
```

<a id="p13"></a>
### P13 `cortex_falsifiability-20261012201532`

**建議退回**：新增 type／needs_enrichment 欄位不符契約，評分器沒有 claim/hypothesis 狀態與補證排程的寫回能力。

提案 JSON SHA-256：`96cda4b2a74d4922abf4b6acbb10f00631682f11a98ba2dbe555965c39caf939`

```diff
--- cortex_falsifiability-20261012201532/original
+++ cortex_falsifiability-20261012201532/proposed
@@ -2,5 +2,5 @@
 A claim has empirical content if and only if you can describe a concrete observation that would prove it false.
 
-First, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim.
+First, act as a red-teamer: identify at least one boundary condition or counter-example (a 'falsifier') — a concrete, observable scenario that would refute the claim.
 Then, score the claim from 0.0 to 1.0 based on how falsifiable it is:
 - 1.0: The falsifier is a concrete, observable, specific scenario.
@@ -9,3 +9,3 @@
 
 Return ONLY a JSON object:
-{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific observation that refutes it, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>"}
+{"type": "<'claim' or 'hypothesis'>", "score": <float 0.0, 0.5, or 1.0>, "falsifier": "<boundary condition or counter-example, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>", "needs_enrichment": <boolean>}
```

<a id="p14"></a>
### P14 `cortex_falsifiability-20261019171141`

**建議退回**：新增 consequence_of_falsehood 欄位不符契約；描述假命題的邏輯後果也不自動等於可觀測 falsifier，需另案設計與覆核。

提案 JSON SHA-256：`24d6bdd16427260362902df368906432376b5d2cce3044d88aef4c1564c43b0d`

```diff
--- cortex_falsifiability-20261019171141/original
+++ cortex_falsifiability-20261019171141/proposed
@@ -2,5 +2,5 @@
 A claim has empirical content if and only if you can describe a concrete observation that would prove it false.
 
-First, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim.
+First, perform counter-evidence generation: construct a logical scenario describing "what would happen if this claim were false" to test its falsifiability. Then, write a 'falsifier' — a concrete, observable scenario that would refute the claim.
 Then, score the claim from 0.0 to 1.0 based on how falsifiable it is:
 - 1.0: The falsifier is a concrete, observable, specific scenario.
@@ -9,3 +9,3 @@
 
 Return ONLY a JSON object:
-{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific observation that refutes it, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>"}
+{"score": <float 0.0, 0.5, or 1.0>, "consequence_of_falsehood": "<the logical scenario of what happens if the claim is false, <=200 chars>", "falsifier": "<specific observation that refutes it, <=200 chars>", "falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>"}
```

<a id="p15"></a>
### P15 `cortex_falsifiability-20261026152824`

**建議退回**：要求對照 existing knowledge 的矛盾，但該評分輸入未提供既有知識；不能假裝已做全庫比較。一般可觀測反例已是現行任務。

提案 JSON SHA-256：`bdd56741b0cf99e1cedd0dbe22fd16232f9dc8d49d25a77780c03356c11b7843`

```diff
--- cortex_falsifiability-20261026152824/original
+++ cortex_falsifiability-20261026152824/proposed
@@ -2,5 +2,5 @@
 A claim has empirical content if and only if you can describe a concrete observation that would prove it false.
 
-First, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim.
+First, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim or expose a contradiction with existing knowledge.
 Then, score the claim from 0.0 to 1.0 based on how falsifiable it is:
 - 1.0: The falsifier is a concrete, observable, specific scenario.
```
