# 下一階段路線圖（2026-07-13 起）

> 承 2026-07-12 產出品質稽核（`LingLing_Output_Audit_report_20260712.md`）與其後一連串修復。
> 這份文件記錄:①本波做完了什麼 ②殘留工作(分類+排序) ③下一階段的分期計劃。

---

## 1. 本波已閉環（2026-07-12/13，10 個 commit，見 CHANGELOG）

| 主題 | commit | 狀態 |
|---|---|---|
| refute operation-aware（創意型不再誤殺） | 1e67ae0 | ✅ live 驗證（strict→refuted / lenient→survived） |
| grounding 去集中化（MMR priors） | 79f7a33 | ✅ live（top-4 88%→散開，新 insight 掛新主張） |
| 殼改造（analogy/counterfactual 輕量殼 + montecarlo 去戰略建議模板） | 6445088 | ✅ live（novelty ~翻倍） |
| 意圖 trigger 詞界（/count 不吞 /counterfactual） | 28d7fa6 | ✅ live |
| M-arc 接通（M3 泛化到洞察品質軸 + 陳舊 pending 提醒） | a6aa6d1 | ✅ live（@ling-improve 產出 agent_insight 提案 + 標出 28 天陳舊） |
| agent_insight 反同質化三改 | 3217e89 | ✅ 熱載入 |
| synthesis verdict parser 補中文 header | bffd6c9 | ✅ live（KD/Fine-tuning 重合成→keep，不再 unparseable） |
| 報告品質軸 unparseable 計量修正 | c949f6a | ✅（synthesis 31%→56% 如實跨 RED） |
| 測試污染修復（history_file） | 6ef2c51 | ✅ |

**共同性質**:全部 gated／default-off／行為保守；修法本身用單元測試＋trace 重跑確定性證明,live run 另證管線健康。

---

## 2. 殘留工作（backlog，依 impact 排序）

### A. Cortex 軸——最深的結構性缺口（M-arc 的另一半）
- **A1 externalize Cortex prompts**：`extract_claims` / `assess_falsifiability` 是 hardcoded Python（`llm_client.py`），M3 因此**碰不到 Cortex 軸**（只能誠實 skip）。外部化成 vault 模板後，M3 才能對 Cortex 診斷產出 prompt 提案，真正接通 M-arc 的 Cortex 半邊。
- **A2 thin-evidence 累積機制**：52/53 主張薄證據（≤1 來源）。MMR grounding（本波）改善了**分佈**，但主張要**變厚**需要 consolidation 更會跨 insight 合併/連結，或 M2 診斷 #2 的「證據追溯維護任務」（定期掃薄證據→找第二來源或降級為假設）。
- **A3 contradictions 恆 0**：71 主張零矛盾邊，裁決從未判過 contradicts——**過鬆疑慮成立**。查 `_adjudicate` 的 contradicts 判準/門檻是否形同虛設。
- **A4 資料衛生**：1 主張 embedding NaN（「The vitality of complex systems…」毒輸入佔位）需重算；0.975 近重複主張對（代理式智慧 vs 實踐）人工裁併。

### B. synthesis 品質閘——收尾
- ~~**B1 #1 型模型不合規**~~：**✅ 完成（`bb811db`）**。retry 迴圈新增 `_needs_retry`：「verdict None 但有 critique section」（閘跑了卻讀不到判定）也重試（bounded）；讀不到就出貨但記成 unparseable（可見、計入 bad），不再靜默放行。＋critique.md 把總體判定段設為 REQUIRED、禁用「總結」替代。無 section（critique 關閉/失敗）仍不重試。

### C. 觀察與再稽核——驗證本波的趨勢效果
- 本波多數修法的 payoff 要**跨夜累積**才看得出（novelty 散開、grounding 去集中化、SE insight_dim 趨勢、M-arc 提案節奏、synthesis unparseable 率）。
- SE trend 本波才清檔重建,需數晚。
- 建議跑 ~1 週後重跑 audit 的 G 軸指標,用**趨勢**（非單點）確認,再決定加碼或回退。

### D. 房務（可平行、隨時）
- DocQuality deferred：mermaid statement 黏連 / meta-text 洩漏 / flowchart 家族（見 `DocQuality_CloudAct_implementation_plan.md`）。
- wikilink 25% 斷鏈（長標題截斷家族）。
- ~~palette prompt 規則~~：**✅ 完成（`795409a`）**。system_base.md Rule 4 補負向禁令（禁 🚨🔴⚠️❌🧠… + 邏輯結構圖標題用 📊/🌿 例），reviewer/coder 補 ⚠️ 三處一致。存量 🧠 檔非功能性可留。
- 稽核清理候選 7 檔（audit report §④）——待 Steven 裁決。

### E. 本體論 O1-O4（暫緩）
- 概念層。計劃書（`Ontology_SemanticEntropy_implementation_plan.md`）明訂等 O0+SE 結果評估;SE trend 剛重置,**尚未到評估點**。

---

## 3. 下一階段計劃（分期）

### Phase N.1 — 觀察窗（被動，~1 週）
先讓夜間排程把本波修法的效果累積成趨勢,不急著再改。到期重跑 audit G 軸:
- novelty 依 operation 的時序（輪替 + 殼改造後是否穩定散開）
- 新 insight 的 grounded_on top-4 佔比（MMR 是否持續去集中化）
- `self_assessment_history` 的 SE insight_dim / 報告品質 rate 趨勢（計量修正後）
- @ling-improve 提案節奏 + 陳舊佇列有沒有被消化
- synthesis unparseable 率（parser 修復後應趨零）

**產出**:一份「干預成效趨勢」小結,決定 N.2 要加碼哪條、回退哪條。

### Phase N.2 — Cortex 軸深化（主動，觀察窗結果決定深度）
依 §2.A 排序:
1. **A3 contradictions 診斷**（最便宜、最能揭露裁決是否過鬆）——先查再決定要不要調門檻/prompt。
2. **A1 externalize Cortex prompts**——接通 M-arc 的 Cortex 半邊（讓 M3 可對 Cortex 診斷提案）。
3. **A2 證據追溯維護任務**——thin-evidence 的 maintenance-side lever（走 gated 提案或 idle 補做契約）。
4. **A4 資料衛生**——順手清 NaN embedding + 裁併 0.975 對。

### Phase N.3 — 房務（平行，隨時插入）
§2.D（DocQuality/wikilink/palette/清理候選）。這些彼此獨立,適合當觀察窗期間的填空工作。（§2.B1 synthesis 品質閘收尾已於 2026-07-13 完成，`bb811db`。）

### 觀察窗基線（t=0，2026-07-13，全庫 aggregate；一週後比 NEW-only）
> 觀察窗自此開跑（daemon 跑最新 code，prompt 熱載入）。~1 週後重跑同指標,
> 重點看**觀察窗期間新產出**的值(非全庫平均——全庫含修法前存量會稀釋訊號)。

| 指標 | t=0 | 稽核前(pre-fix) | 一週後期望 |
|---|---|---|---|
| novelty by op | analogy 0.212 / counterfactual 0.203 / dialogue 0.229 / fable 0.314 / montecarlo 0.126 | analogy 0.135 / counterfactual 0.148 / montecarlo 0.126 | 創意型維持高、不回落 |
| grounding distinct ids / top-4 share | 39 / 0.662 | 12 / 0.878 | distinct 續升、top-4 share 續降 |
| SE insight_dim | 13.9（cortex 40.6） | 11.2（37.6） | trend 累積數點、續升或持平 |
| synthesis 7d verdicts | keep 8 / revise 5 / **unparseable 4** | — | **unparseable → ~0**（parser+B1；4 是修法前存量） |
| pending 提案 | 1（agent_counter 28d+） | — | 別再累積陳舊；@ling-improve 有人審 |

### 暫緩
本體論 O1-O4（§2.E）——等 N.1 的 SE 趨勢與 Cortex 圖狀況明朗再評估是否需要概念層。

---

## 4. 一句話總結
本波把 **insight 生成 → synthesis 品質閘 → 自省弧線** 三條品質迴路都硬化了；下一階段先**觀察趨勢**驗證成效,再往**最深的 Cortex 結構缺口**（薄證據累積、M3 接通 Cortex、裁決過鬆）推進,房務平行填空。
