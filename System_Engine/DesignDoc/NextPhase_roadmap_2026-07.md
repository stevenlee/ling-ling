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
- ~~**A1 externalize Cortex prompts**~~：**✅ 完成（2026-07-13，`a16694a`）**。`extract_claims`/`assess_falsifiability` 兩 prompt 外部化到 `Templates/Prompts/cortex_extract_claims.md` / `cortex_falsifiability.md`（`_vault_prompt` 熱載入 + 缺檔 fallback，vault 與 fallback byte-identical、行為不變）。M3 `_resolve_target`：Cortex 信念 → cortex_falsifiability.md（M2 對 Cortex 唯一 prompt 可套的建議＝降教條化/反向壓力測試）。**M-arc 的 Cortex 半邊接通。**
- ~~**A2 thin-evidence 累積機制**~~：**評估後不另建任務（redundant）**。thin-evidence 要「變厚」的三個 lever 現已全數在線：①MMR grounding（本波，insight 接到更多元主張）②A3 merge parse-miss 修復（equivalent 不再靜默掉→該併的併、accrue evidence）③既有 decay pass（未強化主張的 S 自然衰減＝M2 建議 #2 的「降級」動作已由 decay 涵蓋）。專門的「證據追溯任務」會與這三者重疊、且無法憑空製造證據。**決策：不建冗餘任務,改在 N.1 觀察窗量 thin-evidence 趨勢**——若三 lever 到期仍未見改善,再建針對性任務。
- ~~**A3 contradictions 恆 0**~~：**✅ 診斷完成（2026-07-13，`3945e3f`）**。結論——**不是裁決過鬆**：實測真 adjudicator 對兩組明確矛盾 2/2 判 contradicts、complementary 對照也對，判準準確。0 contradicts 是 claim 母體（跨域橋接主張）本就少對立命題，非 bug。「過鬆」疑慮排除、contradicts 不需改。診斷過程揪到真 bug 並修掉：adjudicate_claims 走 `_complete_json`，reasoning model 偶爾空 content → 靜默 fallback 成 unrelated（掉了 equivalent＝丟 merge，佔快取 ~3%）→ 改 reroll 重試至解析成功。**此 merge 修復直接餵 A2**（該併的主張現在會併）。
- ~~**A4 資料衛生**~~：**✅ 查證（2026-07-13）**。①NaN embedding＝**非問題**：實查 cortex_state.json 71 條 embedding **0 degenerate**（NaN 只出現在我先前的 `backfill --dry-run` shim、無快取，從未進持久狀態）。②0.975 近重複對（代理式智慧/實踐）＝真實但**不會靠 consolidation 自癒**：兩者從未被一起裁決過（不在 adjudication cache），且 consolidation 只裁決「新」claim vs 鄰居、不重比兩個既有 claim。要合併須讓其一以「新」身分重入（re-ingest 來源）再裁決，或寫一次性 force-adjudicate+merge。**決策：1 對近重複、cosmetic，不值得為它冒 daemon 單寫者風險手動併**；留待未來若近重複累積再做「Cortex de-dup pass」。觀察窗順帶看近重複有沒有變多。

### B. synthesis 品質閘——收尾
- ~~**B1 #1 型模型不合規**~~：**✅ 完成（`bb811db`）**。retry 迴圈新增 `_needs_retry`：「verdict None 但有 critique section」（閘跑了卻讀不到判定）也重試（bounded）；讀不到就出貨但記成 unparseable（可見、計入 bad），不再靜默放行。＋critique.md 把總體判定段設為 REQUIRED、禁用「總結」替代。無 section（critique 關閉/失敗）仍不重試。

### C. 觀察與再稽核——驗證本波的趨勢效果
- 本波多數修法的 payoff 要**跨夜累積**才看得出（novelty 散開、grounding 去集中化、SE insight_dim 趨勢、M-arc 提案節奏、synthesis unparseable 率）。
- SE trend 本波才清檔重建,需數晚。
- 建議跑 ~1 週後重跑 audit 的 G 軸指標,用**趨勢**（非單點）確認,再決定加碼或回退。

### D. 房務（可平行、隨時）— ✅ 全數處理
- ~~DocQuality deferred（mermaid）~~：**✅ 完成（`bb0a61c`）**。全庫驗證 15594 blocks / 1245 fail（8%，graph 佔 1082）。逐一驗證後：**主流失敗族（`got STR`＝引號含空格 node id `"X"["label"]`，佔 graph 68%）寫時已被 `repair_mermaid_quoted_endpoint_labels` 修掉**（實測真檔 raw fail、跑現行品質閘後 pass）——1245 是修法前存量，非 live gap。MATH 也已被 `normalized_mermaid_math` 處理。唯一仍漏的 write-time gap＝PS 族（`A["(\"X\")"]`，70 例）→ 新增 `repair_mermaid_paren_escaped_label`。存量 cosmetic、pages/ 有索引，不 mass-backfill（resynthesize 自動修）。剩零星 long-tail（HTML tag ×2）不逐一設 pass。
- ~~wikilink 25% 斷鏈~~：**部分完成（`c64fd22`）**。精算後其實 6.6%：dangling_absent 2411（指向未 ingest 文件的截斷/亂碼標題，歷史 AI 報告的裝飾性斷鏈、改連結救不了）＋ truncation 237（逐案）＋ control_char ~39（唯一系統性 bug）。已修 control_char：新增 `repair_wikilink_newlines` 品質檢查 pass（收合 `[[...]]` 內換行），write-time 生效。dangling 為非功能性、不 mass-edit（且 pages/ 有索引，批改會索引漂移）——**視為已處理**。
- ~~palette prompt 規則~~：**✅ 完成（`795409a`）**。system_base.md Rule 4 補負向禁令（禁 🚨🔴⚠️❌🧠… + 邏輯結構圖標題用 📊/🌿 例），reviewer/coder 補 ⚠️ 三處一致。存量 🧠 檔非功能性可留。
- ~~稽核清理候選 7 檔~~：**✅ 完成（quarantine）**。7 檔（4 Insights + 3 fromLingLing，皆非索引目錄）移到 `Backups/audit-cleanup-20260713/`——可逆、非硬刪、無索引漂移。要永久刪或還原任一由 Steven 決定。（JEPA research 那份若想保留可 `@ling-resynthesize` 重簽。）

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
| Cortex thin-evidence / dogmatic / merges | 薄證據 ~52/53、教條 2、contradictions 0 | 薄證據 52 | 薄證據**佔比**下降（A2 三 lever：MMR grounding + merge 修復 + decay）；merge 事件 >0 |

### N.1 到期執行清單（≥2026-07-20，一週後跑這個）
時間到時,照這份 checklist 執行——不用重想:
1. **重跑指標**（純讀，同 t=0 基線的量法）:
   - novelty by op,**只取 `date_created >= 2026-07-13` 的 NEW insight**（避免存量稀釋）。
   - 新 insight 的 grounded_on distinct ids / top-4 佔比。
   - `self_assessment_history.json` 的 語義熵.insight_dim 與 報告品質 rate 時序（觀察窗累積了幾點、方向）。
   - **Cortex thin-evidence 佔比**（`@ling-cortex` 或掃 Cortex/*.md 的 ≤1 來源比例）＋ maintenance.log 的 merge 事件數（A2 三 lever 有沒有讓薄證據下降）。若仍未降→建針對性證據追溯任務。
   - synthesis quality_verdict 分佈,**只取觀察窗期間 ts** → unparseable 是否 →0（parser+B1 生效）。
   - `_pending` 提案數與最老 age（陳舊有沒有被消化 / 有沒有新提案）。
2. **比對** t=0 基線表的「一週後期望」欄,寫一段「干預成效趨勢」小結（有效／無效／過頭）。
3. **決策 N.2**:趨勢站得住 → 進 Cortex 深化（先 A3 contradictions 診斷 → A1 externalize prompts → A2 證據追溯任務）；若某修法回落 → 先修回落再說。
4. 若 SE 已累積足夠點且 Cortex 圖穩定,順帶評估 §2.E 本體論 O1-O4 是否啟動。

### 暫緩
本體論 O1-O4（§2.E）——等 N.1 的 SE 趨勢與 Cortex 圖狀況明朗再評估是否需要概念層。

---

## 4. 一句話總結
本波把 **insight 生成 → synthesis 品質閘 → 自省弧線** 三條品質迴路都硬化了；下一階段先**觀察趨勢**驗證成效,再往**最深的 Cortex 結構缺口**（薄證據累積、M3 接通 Cortex、裁決過鬆）推進,房務平行填空。
