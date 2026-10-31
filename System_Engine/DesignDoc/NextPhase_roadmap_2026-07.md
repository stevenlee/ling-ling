# 下一階段路線圖（2026-07-13 起）

> **目前執行入口（2026-10-30）**：[開發計畫重評與未完成工作](Development_Plan_2026-10-30.md)。
> 本文件保留歷史決策與逐輪驗證；舊段落的待辦請以新計畫的狀態／依賴／驗收條件核對。

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
  **→ 2026-07-24 觀察窗實測未降（79/80 薄、零 merge）→ 觸發條件成立，已建（falsifier-first、dry-run 形態）**：`maintenance/evidence_traceback.py`＋`evidence_traceback_daily` 任務——對薄證據 claim 先查 falsifier 情境再查主張本身（壓力測試優先、非找支持票），排除自我來源（Cortex/Insights/自家報告/原生 insight 與 sources）與遠距段落後，LLM 判 supports/contradicts/neutral，寫 `fromLingLing/✅EvidenceTrace-YYYY-MM-DD.md`。**dry-run only：不寫任何 Cortex 狀態**（僅擁有輪替 cursor `Database/evidence_traceback_state.json`）；失敗≠判定（error/unparseable 分開計，不折成 neutral）。knobs：`evidence_traceback: true`（已設）/`evidence_traceback_batch: 5`/`evidence_traceback_max_distance: 0.45`。**首次啟用需 daemon 重啟**（任務註冊在啟動時）。看幾晚命中率後再裁決 apply 路徑（+evidence/記 tension/強化）。
- ~~**A3 contradictions 恆 0**~~：**✅ 診斷完成（2026-07-13，`3945e3f`）**。結論——**不是裁決過鬆**：實測真 adjudicator 對兩組明確矛盾 2/2 判 contradicts、complementary 對照也對，判準準確。0 contradicts 是 claim 母體（跨域橋接主張）本就少對立命題，非 bug。「過鬆」疑慮排除、contradicts 不需改。診斷過程揪到真 bug 並修掉：adjudicate_claims 走 `_complete_json`，reasoning model 偶爾空 content → 靜默 fallback 成 unrelated（掉了 equivalent＝丟 merge，佔快取 ~3%）→ 改 reroll 重試至解析成功。**此 merge 修復直接餵 A2**（該併的主張現在會併）。
- ~~**A4 資料衛生**~~：**✅ 查證（2026-07-13）**。①NaN embedding＝**非問題**：實查 cortex_state.json 71 條 embedding **0 degenerate**（NaN 只出現在我先前的 `backfill --dry-run` shim、無快取，從未進持久狀態）。②0.975 近重複對（代理式智慧/實踐）＝真實但**不會靠 consolidation 自癒**：兩者從未被一起裁決過（不在 adjudication cache），且 consolidation 只裁決「新」claim vs 鄰居、不重比兩個既有 claim。要合併須讓其一以「新」身分重入（re-ingest 來源）再裁決，或寫一次性 force-adjudicate+merge。**決策：1 對近重複、cosmetic，不值得為它冒 daemon 單寫者風險手動併**；留待未來若近重複累積再做「Cortex de-dup pass」。觀察窗順帶看近重複有沒有變多。

### B. synthesis 品質閘——收尾
- ~~**B1 #1 型模型不合規**~~：**✅ 完成（`bb811db`）**。retry 迴圈新增 `_needs_retry`：「verdict None 但有 critique section」（閘跑了卻讀不到判定）也重試（bounded）；讀不到就出貨但記成 unparseable（可見、計入 bad），不再靜默放行。＋critique.md 把總體判定段設為 REQUIRED、禁用「總結」替代。無 section（critique 關閉/失敗）仍不重試。
- ~~**B2 prompt/parser 格式契約矛盾（2026-07-24 觀察窗重稽核頭條）**~~：**✅ 完成（2026-07-24，需 daemon 重啟）**。B1 的 critique.md 規定 header `**總體判定 (Overall Verdict)**`，但 parser 兩條 regex 都不認括號英文尾/無冒號形態→觀察窗 11/14 unparseable（詳見 `LingLing_Audit_ObsWindow_report_20260724.md` §2）。三修：①regex 加 `_VERDICT_HEADER_TAIL`（全半形括號尾）；②critique_text 空回應（reasoning-channel 空 content，同 A3 家族）套 reroll（attempts=3、retry 升溫），不再被當「critique off」無閘出貨；③`test_prompt_assets.py::TestCritiqueVerdictContract` 直接從 critique.md 抽 REQUIRED header 餵 parser——prompt 格式再漂移、suite 先紅。真實窗內 trace 驗證：16/22 可解析（其餘 6 筆為真 #1 型整篇無判定，誠實 unparseable 屬設計行為）。**殘留**：#1 型（模型完全不寫判定段，集中在大部頭數學書的賞析型輸出）仍靠 unparseable→NeedsReview 兜底。

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

## 3.5 N.2 觀察窗清單（2026-07-24 起，排程 `lingling-n2-obswindow-reaudit` 於 2026-07-31 09:00 提醒）

> N.1 重稽核（`LingLing_Audit_ObsWindow_report_20260724.md`）後又落了三個修法＋清了佇列，各自需要跨夜累積才驗得出。到期照這份清單重量測，重點仍是**觀察窗期間新產出**（date ≥ 2026-07-24），全庫平均會被存量稀釋。

**優先觀察（本輪新修法的成效）：**
1. **A2 evidence_traceback dry-run 命中率**（最高優先，全新機制）：讀 `fromLingLing/✅EvidenceTrace-*.md`。看 ①supports/contradicts vs neutral 比例——全 neutral ⇒ 放寬 `evidence_traceback_max_distance`（0.45→0.55）或代表 vault 真無獨立佐證；②自我來源排除計數是否乾淨（supports 來源不該是變相同源）；③有沒有出現有據 contradicts（= Cortex 第一次有 tension 訊號）。約需 ~2 週掃完 79 條薄證據池一輪。**看過幾晚後裁決 apply 路徑（+evidence／記 tension／強化）是否開。**
2. **synthesis 品質閘 parser 修復生效**（B2，commit 3513ddc）：只取觀察窗 ts 的 synthesis artifact，`quality_verdict` 分佈——unparseable 應大幅下降、keep/revise 正常出現（修前窗內 11/14 unparseable）。若仍高 ⇒ header 又漂移新形態，看 critique trace。
3. **兩筆已套用 prompt 的效果**（commit 2afa9bf，熱載入）：①`agent_counter.md`（LingLens）→ lens_report 型別失敗率是否從 ~50% 降；②`agent_insight.md` → 新 insight 的 novelty 是否升、refute 存活率是否改善（注意殼改造/輪替也在同時作用，需分辨）。

**延續觀察（N.1 已在追、繼續看趨勢）：**
4. **Cortex thin-evidence 佔比 + merge 事件**：N.1 實測 79/80 薄、零 merge。A2 dry-run 不寫入，所以這輪佔比不會因 A2 而降（除非開 apply）；看的是 grounding/decay/merge 三被動 lever 有沒有任何自然變化。
5. **grounding 去集中化維持**：新 insight 的 grounded_on distinct / top-4 share（N.1 已達 0.159，看是否守住不回升）。
6. **SE insight_dim 趨勢**：`self_assessment_history.json`，N.1 三點 13.79→14.22，續看方向。
7. **產出頻率**：N.1 發現 7/15-18 三個夜間 insight slot 缺席，疑似被重 ingestion（Mathematics for CS 等）排擠。看重 ingestion 過後 slot 是否回歸每日一篇。

**待查 bug（非趨勢、可隨時查）：**
8. ~~**insight 生成逾時仍落盤**~~：**✅ 完成（2026-07-27）**。根因是 `LLMClient.answer_query` 的 `Error: ...` sentinel 被 insight pipeline 當正常內容，`daily_insight` 又無條件回 `succeeded`。現在 generation artifact boundary 會拒絕空內容／error sentinel、不計 signals、不寫 canonical 或 mirror；排程收到明確 `failed`，Daydream 也把失敗夜間 slot 視為仍 owed。Full insight 會跳過單一失敗 strategy，但全數失敗或 cross-synthesis 失敗時不出貨。
9. ~~**novelty null 2/11**~~：**✅ 完成（2026-07-27）**。7/22 fable 是已處理的失敗出貨；7/23 montecarlo 內容完整、groundedness/refute 正常，但該內容在 embedding cache 與 `insight_signals.json` 都沒有 commit，後續每日 novelty 又自行恢復，證據最符合單次 embedding transport failure（當時 exception 未持久化，無法事後精確還原）。修正兩條可重現缺口：①novelty embedding 對 raising transport failure 做 2 次 bounded retry；②sidecar 消費端清除 malformed/null、NaN/Inf、zero-norm、錯維度 entry，避免一筆長壽狀態毒死本輪計算。sidecar 同時改用同目錄唯一 temp＋fsync 的原子寫入；持續 outage 仍誠實 fail-open 為 null。

## 3.6 Code review hardening（2026-07-27）

- **Packaging**：setuptools 改為自動發現 `System_Engine` 下所有 package；wheel 安裝 smoke test 已驗證 `agents.insight`、`core.parsing`、`services.ingest/llm/rag/scout` 與 migrations 可匯入，避免 editable install 掩蓋缺包。
- **Evidence traceback 候選完整性**：同一來源跨 falsifier／claim query 的 hits 先聚合、保留最佳 distance，再做距離與獨立性 gate；同 title 不同來源保持獨立，NaN／Inf distance fail-closed。仍維持 dry-run，不改 Cortex。
- **Insight artifact commit point**：canonical report 與 Insights mirror 都改同目錄原子寫入；canonical 是 commit point，mirror 若失敗會明確回報「已提交、待修 mirror」，不再把 post-commit failure 說成未套用。
- **驗證**：`make check` exit 0（ruff clean、mypy clean、1796 passed／1 skipped）；另以實建 wheel 安裝到隔離目錄後做 nested-package import smoke test。

## 3.7 N.2 中期稽核（2026-07-27，觀察窗第 4 天）

- **evidence_traceback 已穩定跨夜**：7/24–7/27 共 4/4 日出報，覆蓋 20 個 unique claims、49 次判讀；raw 分佈 supports 11／contradicts 1／neutral 37，0 error、0 unparseable。尚未開 apply，這些數字只代表候選命中，不代表已強化 Cortex。
- **中期稽核抓到兩種同源灌票**：7/27 有 2 筆 claim 把自身 Cortex 頁判為 supports；另有同一 SOAR 文件的 Synthesis／Stitched view 同時算兩份證據。已補 metadata gate：vault-relative `Cortex/...` 也必須排除，claim-id title fail-closed；Part N／Stitched／Synthesis 則 canonicalize 成同一 source family，只留最佳距離候選。
- **SeedSampler 的 doc anchor 改採 metadata 白名單**：7/27 fable 抽到 Cortex claim text，導致 `refute_verdict: null`、bridging 0；根因是舊 filter 只看 title 是否以 `cortex-` 起頭。現在由 RAG 只列 `pages/`、`Notes/` 的 concrete source titles，Cortex／Insights／fromLingLing 不再進 seed pool；測試替身仍保留 legacy accessor fallback。
- **薄證據尚未自然改善**：Cortex 目前 total 80、active 61、fading 19；active 中 60/61 仍只有一筆 evidence，1/61 有兩筆，merge 仍為 0。這支持維持 N.2 的 A2 優先級，但在 source gate 修正後還需再累積乾淨樣本，現在不宜依 raw 11 supports 開 apply。
- **新 insight 訊號健康但樣本仍小**：7/24–7/27 四個每日 slot 全到；novelty 0.3014／0.3213／0.2660／0.3408（mean 0.3074、0 null），24 個 grounded refs／20 distinct，top-4 share 0.333。synthesis parser 修復後的新 synthesis sample 仍為 0；7/24 的 4 筆（2 unparseable、2 null）都早於修復 commit，不能據此判 regression。post-fix LLM calls 219/219 succeeded。
- **7/31 重稽核仍保留**：中期稽核用來提前修資料污染，不取代完整觀察窗。到期應從修正部署後重新切 evidence_traceback 樣本，核對 self-source=0、同 source-family 不重複，再決定 apply 契約（+evidence／tension／strengthening）；同時等待至少一筆 post-fix synthesis 才裁決 parser 趨勢。

---

## 4. 一句話總結
本波把 **insight 生成 → synthesis 品質閘 → 自省弧線** 三條品質迴路都硬化了；下一階段先**觀察趨勢**驗證成效,再往**最深的 Cortex 結構缺口**（薄證據累積、M3 接通 Cortex、裁決過鬆）推進,房務平行填空。

## 2026-10-29 Scout 財經來源切換

- Yahoo Finance `news/rssindex` 持續 404，改用 Google News 美國英文版搜尋
  `economy OR finance`（`hl=en-US&gl=US&ceid=US:en`），每天最多 10 則。
  來源語言改英文，日報輸出語言仍遵循原設定。
- RSS 解析仍使用既有 feed parser；正文抓取先取得 Google News 的 signature/timestamp，
  經 batchexecute 解析為媒體原網址，再走既有正文擷取。HTTP redirect 直接指向媒體時
  也可處理。報告使用原網址，去重 key 保留原 feed GUID。
- Google 解析參數缺失、RPC/格式錯誤或未取得有效媒體網址時 fail-soft，回報既有的
  缺正文標記；不把 Google 中轉／同意頁當正文。解析使用非官方介面，未來格式變動
  可能需維護；媒體的付費牆或拒絕抓取仍會降級為標題／摘要。
- 實測 RSS HTTP 200、100 則條目；前三則原網址解析 3/3 成功，California State
  Portal 與 Daily Northwestern 正文擷取成功，Reuters 未取得正文，保留降級行為。
- 新增解析成功、參數缺失、RPC timeout、錯誤 payload／非媒體 URL、HTTP redirect
  的 hermetic 回歸測試；`make check` exit 0（ruff/mypy clean、1817 passed、
  1 skipped；1 個既有 Chroma deprecation warning）。
- 啟用：Scout.md 目標隨下次執行讀取；daemon 需重啟以載入新的 Python 解析程式。
- daemon 重啟後手動 Scout 實跑成功：四來源成功、25 則新項目；Google News 10/10
  原網址解析成功，7 則取得實質內容。Reuters／Army.mil 缺正文有標示，Brookings
  訂閱提示仍被當內容。後續可改善正文充分性判定，以及搜尋混入報價頁／非市場財經
  內容的篩選；本次維持 `economy OR finance` 搜尋範圍。

## 2026-10-29 A2 apply 成效稽核與失敗／距離契約修正

### 稽核結果（8/8–10/29）

- 保存 67 份 apply 報告，335 claim-scans／87 不同 claim、559 次段落判讀：
  supports 28、contradicts 0、neutral 451、unparseable 80（14.3%）、報告 error 0。
  28 筆 evidence 對應 24 條增厚主張，全部仍可對回 Cortex，未見重複 marker 或
  同一 claim 多次新增。7 條目前 active、8 fading、9 dormant；17 條在 decay
  ledger 中可確認增厚後降級。active 共 16 條，thin 9／thick 7。
- 8/8 pool=54 → 10/29 pool=9 不是固定 cohort，不能直接歸因 A2；舊 claim
  大量淡出。期間無 apply counterpoint 樣本，無法以 live 結果驗收反證寫入。
- 10/15 的 13 unparseable 實為 13 claim/passage × 2 JSON attempts 共 26 筆
  `Connection error`，任務 trace 卻為 succeeded。其餘 67 unparseable 因 trace
  retention 無法還原，不能推定相同根因。期間有 16 日缺報，缺報本身不等於
  排程失敗。559 筆距離皆顯示 0.00；BM25-only 候選以 0.0 佔位的契約缺口
  可由程式確認，但報告未保留 provenance，不能推定全部判讀都來自 BM25。

### 本次修正契約

- `_complete_json` 新增 opt-in `propagate_errors`，A2 與 `raise_on_miss` 一起使用：
  transport 由既有 `_complete_text` 重試，耗盡後直接成 error，不疊加 content
  retry；內容解析失敗由 bounded `reroll` 重試，耗盡以
  `JSONCompletionParseError` 表達 unparseable。其他呼叫端保留原有預設契約。
- 任一 claim 的檢索／判讀不完整時不 apply（避免反例檢索失敗卻只依支持票強化）；
  同批完整 claim 仍可完成。任何 claim 判讀／檢索／寫入失敗，整輪回 failed，
  保存報告與明確摘要，scheduler state 和 SQLite run trace 同步反映失敗。
- `checked` cursor 仍記「最後嘗試」並輪替失敗 claim，不宣稱成功。daily 排程
  不在同日立即重跑，避免 outage／poison claim 耗盡預算或餓死其他 claim；下次
  輪到時重試。RAG `raise_on_error=True` 讓 A2 能區分檢索失敗與有效空集合，
  一般 RAG caller 的 fail-soft 預設不變。
- A2 依 `retrieval_breakdown.vector_distance` 使用實測正文距離，BM25-only、
  facet 代換後的正文距離未知、missing/malformed/NaN/Inf/負值不送裁決，也不
  套用；另列「缺少有效正文距離」計數。legacy 直接向量 provider 仍可用有效
  `distance`。真正量測為 0 可接受，但 lexical 佔位 0 不能勝過同源的實測
  候選。Chroma 未回傳距離時的 breakdown 改為 None，其他 caller 的 legacy
  distance 欄位保留。這是保守收窄，可能降低候選數；後續若需救回 lexical／facet
  候選，應計算 query-to-passage 距離，而非放寬 0.45 門檻。
- 舊測試對「失敗仍 ok」的斷言隨上述狀態契約更新；不再將處理完一批等同成功。
  另補真實 `_complete_json` 的 transport／parse 路徑、部分成功不回滾、未完整
  claim 不套用、實際 hybrid RAG→A2 gate，以及 scheduler trace/state/busy-lock
  釋放與每日頻率的 hermetic 回歸測試。

### 還欠什麼

- Cortex 寫入後 RAG re-index 失敗仍只有 warning，需另做持久化追補；本次不宣稱
  報告「0 寫入失敗」能證明索引完全健康。
- 抽查 supports 是否僅為跨域類比／局部支持，以及累積 contradicts apply 樣本。
- daemon 需重啟載入新程式；不重寫歷史日報，不回滾既有 evidence，不改強化幅度。
- 驗證：`make check` exit 0；ruff clean、mypy 166 source files 無錯誤；
  1843 passed、1 skipped，1 個既有 Chroma deprecation warning。`git diff --check` clean。

## 2026-10-29 M3 防重複、退回記憶與 prompt 契約守衛

### 唯讀盤點

- pending 15 筆（agent_insight 3、cortex_falsifiability 12），最老 7/27，至 10/29
  為 94 個日曆日；13 筆至少 14 個日曆日。applied 2、rejected 4。
- 15 筆 original_sha 仍符合各自 target，沒有逐字相同的 edit fingerprint；主要
  問題為相同 target／相同 basis 不斷產生功能重疊的競爭方案，非單純檔名重複。
- Cortex 4 筆再次新增 consumer 不讀取的 JSON 欄位；另有要求來源數／搜尋反證的
  編輯，但該呼叫只收到 Claim、沒有搜尋工具。既有 rejected 有 2 筆手動保存
  rejected_reason，生成端卻未讀取；正式 reject 命令原先也不接受理由。

### 本次離線實作

- M3 生成前以 canonical target＋原文 SHA 比對 pending，有相同版本待審即跳過，
  不呼叫 LLM。同輪多 axis 對應同 target 也共用一個待審 slot。儲存前再讀 target
  和佇列，避免生成期間的編輯／新提案被忽略。舊版本 pending 不會永久阻擋新版。
- `_rejected` 同 target／同 basis 的 rationale＋fixes 相同時生成前跳過；其他
  診斷會參考最近 5 筆退回理由，再於生成後比對 revised content／find-replace。
  edit 比對不含 why、統一 CRLF，但保留編輯順序與有意義的空白；不使用模糊
  相似度直接否決。目標 basis 改變後可重新評估，沒有永久封鎖。
- `@ling-improve reject <id> [理由]` 會保存 rejected_reason，無理由與舊紀錄仍
  可用。退回紀錄先原子落盤，再清 pending；清除失敗回報「已退回、待清理」，
  重試時不遺失原理由。路由以第一個 subcommand 為準，避免理由裡的
  `approve <id>` 誤觸批准。
- Cortex prompt 的 JSON key 守衛同時放在生成與 approve 消費端：禁止新增程式
  不讀的欄位或刪除必要欄位；既有待審提案也需通過守衛。生成 context 明示只有
  Claim、無搜尋／來源數，不能靠 prompt 新增證據或調整信心。
- pending 改同目錄唯一 temp＋fsync 原子寫入，既有 ID 拒絕覆寫；讀取端驗證
  proposal 形狀與 original_sha，損壞資料留原檔並 logging.warning，不永久占住
  target。讀檔／儲存失敗逐 axis 可見並繼續其他項目。

### 驗證與後續

- M3 聚焦測試 42 passed；完整 `make check` exit 0，ruff clean、mypy 167 source
  files 無錯誤、1867 passed／1 skipped，1 個既有 Chroma deprecation warning。
  `git diff --check` clean。測試只用 tmp_path 與 FakeLLM，未呼叫外部模型。
- 本次不重啟 daemon、不操作 live generate/approve/reject、不變更既有 15 筆
  待審提案／prompt。新 Python 行為須待合適時間重啟載入。
- 待做：人工整併／審閱既有競爭提案，以及 M2 的 active/inactive 分層與 A2 成效
  上下文；本輪未實作 M2 指標調整。A2 re-index 持久化追補亦仍為獨立待辦。

## 2026-10-29 Synthesis 審查失敗不得誤標通過

### 稽核與問題

- 8/8 後未找到新完成的 synthesis；保留期內的 trace 也無 critique_text／synthesis
  artifact，因此沒有足夠新樣本裁決長期品質趨勢。8/7 AI Political Compass 仍是
  模型未寫判定段的 unparseable，維持 NeedsReview；不猜測或回寫歷史 verdict。
- 記憶體替身確認：啟用 critique 後若回例外、`Critique failed` sentinel 或空內容，
  舊介面回空 section＋None，與 disabled 相同，出版分支因而可能標 PerfectPitch。

### 修正後契約

- SynthesisCritiqueLoop 明確傳遞 critique_status：disabled／succeeded／unparseable／
  error，並帶 critique_error、每次 attempt 的 status/error history。tuple adapter
  保留給舊呼叫端；啟用但失敗時改回可見的「審查未完成」section，verdict 仍為 None。
- 啟用但審查失敗、空／非字串內容、缺少來源或 candidate 時，成品保留但標 NeedsReview，
  頁首警示「審查未完成」，metadata／artifact 的 quality_verdict 記為 error 狀態，
  不假造 keep／revise／reject，也不誤列為模型格式 unparseable。消費端遇到缺失或
  不一致的 outcome status 亦採相同保守判定。明確 disabled 保留原有出版行為，
  另記 critique_status=disabled，沒有 quality_verdict。
- 連線／呼叫失敗只使用既有 transport 重試，不因此重生整篇。若第一版已取得有效
  判定，但重生版本的審查失敗，保留原本較佳版本並停止後續整篇重試；失敗保存在
  history，不覆寫原本的內容判定。模型漏判定段的 bounded retry 與既有排序規則保留。
- M1 將 error 納入品質閘失敗率，另列 gate_error，與 content_bad／unparseable 分開；
  by_type.bad 仍只計 revise/reject，避免 M3 把 provider／輸入錯誤當成生成 prompt 問題。
  趨勢 snapshot 同時保留這三種計數。
- 測試契約同步更新：僅 disabled 可回完全空審查；原本「失敗等同關閉」的斷言改為
  可見未完成，另補真正 disabled 的回歸案例。測試預設 TraceStore 導向 tmp_path，
  避免 LLMClient fixture 初始化 live SQLite；明確指定的測試 DB 不受影響。

### 驗證與交接

- `make check` exit 0：ruff clean、mypy 167 source files 無錯誤；1883 passed、
  1 skipped、1 個既有 Chroma deprecation warning。`git diff --check` clean。
- 覆蓋例外／sentinel／空與非字串回應、真實 LLMClient 的 bounded 空內容重試、
  transport 不疊加 content retry、重生後審查失敗保留原版、出版標記、artifact、M1。
- 本次未重啟 daemon、未觸發 live ingestion／LLM、未改既有頁面；待長時間工作完成後
  再安排重啟與新成品驗證。既有 A2／M3 未提交變更保留，A2 re-index 追補仍待實作。

## 2026-10-29 A2 vault commit 後索引失敗的持久化追補

### 行為與 commit point

- 新增 `maintenance/cortex_index_repair.py`。A2 apply 在存 Cortex 前先原子落盤索引意圖，
  位於 `Database/cortex_index_repairs/<relative-path-hash>.json`。意圖無法持久化即不
  提交頁面；Cortex 原子 replace 是信念變更的 commit point。Cortex store 同步改用
  既有 unique temp＋fsync 原子寫入工具，避免固定 temp 碰撞。
- commit 後正文／facet 索引或 intent 清除失敗，保留實際 added_evidence／reinforced
  結果，回報「Cortex 已生效但待索引」，不謊稱未套用。intent 在索引前先持久化
  attempt／next_retry_at，失敗寫 last_error；不重做證據判讀、append 或 reinforce。
- 現有 A2 daily task 的 apply 模式先追補，再選 thin pool。因此主張增厚後退出
  thin pool、甚至整個 pool 為空，仍可跨重啟恢復；沒有新增 thread／timer 或 LLM
  呼叫。整段沿用 scheduler 的 idle_required／try_set_busy ownership；測試確認
  使用者忙碌時不執行，追補執行時 busy lock 已持有。
- 追補只讀當下同一次頁面 snapshot，解析器新增可注入已讀 text 的介面；重新索引
  最新正文與 claim，不寫回 Cortex。寫入期間內容再改動即保留意圖；basis 改變重設
  retry budget。最新狀態為 dormant／falsified 時移除 facets，避免錯誤復活。
- 頁面已刪除則取消意圖，不以舊 snapshot 重建；RAG 刪除仍由既有 VaultWatcher／
  orphan sweep 擁有。損壞 intent 可由 path hash 對回現存頁面重建；無法對回者保留
  原檔並在報告／log 顯示，不消耗 embedding 批次額度、不阻擋其他有效工作。
- RAG 的正文、legacy 清理、facet 清理／upsert／移除新增或沿用 strict opt-in，
  讓失敗能向追補端傳遞；其他呼叫端預設 fail-soft。repair 正文使用 force=True，
  不會因先前部分 upsert 已寫 content_hash 就誤以為整頁完成。

### 頻率、觀察與限制

- `CORTEX_INDEX_REPAIR_BATCH` 預設 5；`CORTEX_INDEX_REPAIR_BACKOFF_SECONDS` 預設
  3600、`CORTEX_INDEX_REPAIR_MAX_BACKOFF_SECONDS` 預設 86400，皆由 env 設定。
  失敗採持久化指數退避，最多到設定上限，實際重試時機仍受每日 A2 排程限制。
  舊待辦優先，attempt 更新 slot 時間後讓其他未嘗試工作先走；不讓單筆故障壟斷。
- 仍有待索引意圖時，A2 result／scheduler state／trace 為 failed；報告列完成數、
  待處理數與失敗原因。dry-run 不處理或清除索引意圖；master switch／apply 關閉
  時不會透過此機制變異 RAG。
- write-ahead intent 若先落盤、vault 尚未 commit 就 crash，下次只索引當下真正
  存在的頁面，不將未提交的證據 snapshot 寫回。cleanup crash 可能重做索引，但
  strict cleanup＋deterministic IDs 維持冪等，不會再次強化信念。
- 本機制保護新 A2 apply；部署前沒有 intent 的歷史索引缺漏需另行唯讀對帳，
  不宣稱已追溯修復。重啟與 live 驗收仍等使用者的長時間工作結束。

### 驗證

- `make check` exit 0：ruff clean、mypy 168 source files 無錯誤；1911 passed、
  1 skipped、1 個既有 Chroma deprecation warning；`git diff --check` clean。
- 新增 28 個 hermetic 案例，涵蓋 intent／vault／索引／清除各 commit 階段的故障，
  跨呼叫恢復、空 thin pool、最新人工修改、刪頁、dormant、破損／越界 slot、
  退避／公平性／上限、busy lock，以及實際 RAG strict failure 接縫。
- 本次未重啟 daemon、未呼叫 live LLM 或修改 live Cortex／ChromaDB；與前述
  A2／M3／synthesis 修正一起保留在功能分支，等待獨立邏輯 commit 的交付授權。

## 2026-10-29 M1／M2 診斷依據的狀態與來源分離

- Cortex tension scan 在同一批頁面快照中產生 `by_status`：各狀態主張數、薄證據數、
  保留 A2 補證的主張數及證據筆數；只計 `active_evidence`，排除 superseded。
  A2 來源以現有 `[EvidenceTrace] ` provenance marker 辨認，包含 inactive 的歷史留存，
  不宣稱為近期增厚次數或因果成效。既有 tension buckets 與燈號判準不變。
- M1 增列 active 薄證據需求與當下 A2 enabled／apply 設定；設定不代表任務執行成功。
  M2 直接使用 M1 的分布與有界範例，不再重新掃描另一時間點的 vault；舊 assessment
  缺少新欄位時明示未知。明確要求 inactive 存量與 active 比率分開，不能從薄證據多或
  矛盾為零推論驗證／證偽機制不存在；執行成效仍需任務報告及失敗訊號佐證。
- M2 報告品質上下文加入 `gate_error`，與 revise/reject 及 unparseable 分列，
  避免把審查呼叫失敗當成內容品質差而提議改寫生成 prompt。
- 新增 4 個 hermetic 回歸案例：跨狀態及 superseded 補證計數、禁止 M2 重掃、
  舊評估未知值、審查執行失敗分類。首輪新 fixture 缺少必填 evidence 欄位，已修正。
- 最終 `make check` exit 0：ruff clean、mypy 168 source files 無錯誤；
  **1915 passed、1 skipped、1 個既有 Chroma deprecation warning**；`git diff --check` clean。
- 未重啟 daemon、未呼叫 live LLM、未修改 live vault／DB；尚未 commit 或 push。
  實際排程輸出與 live 效果驗收仍待允許重啟後進行。

## 2026-10-30 重啟後實際驗證（尚未全數驗收）

- 重新執行 `make check` exit 0：ruff clean、mypy 168 source files 無錯誤；
  1915 passed、1 skipped、1 個既有 Chroma warning。修改的核心模組可在新程序完整匯入。
- maintenance state 的 00:42 A2 失敗為找不到新增 `CORTEX_INDEX_REPAIR_DIR`；發生在
  PID 檔 08:25 更新之前，符合舊程序已載入 config、晚載入新 maintenance module 的
  時序。不將新程序可匯入等同 A2 live 修復驗收；尚無本次重啟後的新 A2 執行樣本。
- 透過正常 prompt queue 執行 `@ling-improve-verify-20261030.md`，daemon 在
  08:28:28–08:29:24 跑完 M1→M2→M3。run `run_c3163e4e599b4e3087a786ffe12231ac`
  succeeded，兩次 self_diagnosis 呼叫 succeeded，最後 busy=false，命令已歸檔。
- M1／M2 context 確認載入新碼：全庫 122／薄證據 97；active 19／thin 12、
  dormant 89／thin 78、fading 14／thin 7；有效 A2 補證分別 7／8、10／11、7／9
  （主張／證據）。M3 同 basis 防重複生效，pending 前後皆 15 筆，沒有新提案或套用。
- **M2 推論品質未通過**：即使 context 明示母體差異與禁止因果推定，模型仍以全庫
  97/122 斷言 A2 陷入低質量循環，並建議修改 prompt 強制搜尋多來源。現有輸入不足以
  支持此因果結論，且呼叫沒有搜尋工具。這輪只確認 context 傳遞正確，不能宣稱診斷
  誤判已解決。後續需補任務能力／實際執行訊號及可驗證的診斷輸出限制，再離線與實跑。
- A2 尚無手動 daemon 命令入口；未修改每日排程 state、另開寫入程序或故意製造
  索引故障。新 A2 與索引追補仍待正常排程驗收。Synthesis 亦待自然新成品。
- 報告：`fromLingLing/✅sys-eval-20261030.md`、`✅sys-diag-20261030.md`、
  `✅ins-improve-Improve-generate-20261030-0829.md`。本次未 commit／push。

## 2026-10-30 M2 Cortex 因果推論與 M3 消費端守衛

- 上輪實跑證明 context 提醒不足以阻止模型的因果誤判。本次不依賴模型自報信心或
  遵守文字限制：目前 Cortex 軸只有彙總存量／狀態／補證計數，程式固定將模型因果
  輸出保存為 `hypothesis`，主判定為「尚未確認根因」，清空 candidate_fixes、信心
  不作成立根因呈現，並設 `proposal_eligible=False`。模型 JSON 不能自行解除此限制。
- 報告分列判定、折疊的未驗證假設、資料缺口與尚未執行的查證項目。查證包含逐次
  A2 候選／裁決／失敗紀錄、固定 cohort 的補證與狀態遷移，以及實際 Claim／評分樣本。
  這些查證未自動執行，不宣稱已有因果證據。
- context 明示能力：A2 程式會檢索且已只選 active thin；裁決呼叫本身沒有搜尋工具。
  可證偽性評分只收到 Claim，改其 prompt 無法補證、調排程或新增反證檢索。
- M3 在找目標或呼叫改寫 LLM 前檢查 proposal_eligible，即使誤保留 fixes 也不能
  產生提案。其他軸與既有 Diagnosis 呼叫的預設介面保留；目前正常 Cortex M2 輸出
  會暫停自動產生 prompt 提案，未來必須另定含可覆核樣本的輸入契約才能放行。
  原有待審提案未清除或套用。這是明確的行為收窄，並非只改報告措辭。
- 解析途中異常（如非法 confidence）會清掉部分 fixes 並禁止入 M3，避免先解析到
  的建議在失敗後流出。provider 失敗仍不會偽裝成有效診斷。
- 新增 5 個 hermetic 測試，重播實跑的 97/122 因果誤判與搜尋建議、模型 99% 信心／
  自報放行、舊資料缺口、provider 與 malformed confidence 失敗，以及 M3 消費端拒絕。
  原「全部 flagged 軸都有 fixes」測試同步改為 Cortex 保留假設但禁止 fixes，其他軸不變。
- 最終 `make check` exit 0：ruff clean、mypy 168 source files 無錯誤；
  **1920 passed、1 skipped、1 個既有 Chroma warning**；`git diff --check` clean。
- 本次未重啟 daemon、未再發 live generate、未 commit／push。當前 daemon 已快取舊
  Python module，需下次重啟載入新守衛再驗收；A2 正常排程與新 synthesis 仍待樣本。

### 2026-10-30 08:35 新守衛實跑驗收

- 使用者確認再次重啟後，透過正常 prompt queue 執行
  `@ling-improve-verify-m2-gate-20261030.md`，08:34:38–08:35:28 完成。
  run `run_2e71fd4edd70462bbf5c1dbd5bf75f50` succeeded、兩次 self_diagnosis
  呼叫 succeeded，最後 busy=false。
- 模型仍提出 A2 閉環／無法增厚的無充分依據因果推論，但報告只在「模型未驗證
  假設」折疊區保存；主判定顯示「尚未確認根因」，另列查證項目及資料缺口。
  因此驗收的是程式端隔離與 M3 守衛，不是宣稱模型推論已正確。
- M3 明示 Cortex 軸「診斷證據不足，須先查證，不能據此修訂 prompt」，未產生
  改寫呼叫或新提案。pending 15 筆，前後逐檔位元組比對完全相同。
- 報告 `fromLingLing/✅sys-diag-20261030.md` 與
  `✅ins-improve-Improve-generate-20261030-0835.md`；同日日報依既有命名覆寫。
  報告品質軸仍是模型候選診斷，這次 Cortex 守衛不等同所有軸因果推論皆已驗收。
- 本輪未改 Python code，最近完整檢查仍為 `make check` exit 0：1920 passed、
  1 skipped、1 個既有 warning；`git diff --check` clean。A2 排程／索引追補與
  新 synthesis 仍待自然執行樣本；未 commit／push。

### 2026-10-30 分批交付

- 依使用者授權 commit／push 至功能分支 `codex/a2-audit-hardening`，main 未變更。
- 邏輯提交：`5b3825e` 測試 trace 隔離、`f02b8a5` A2 失敗與索引恢復、
  `8e24b6d` M3 提案審閱、`591f7b2` synthesis 審查失敗、`43f4dd8` M1／M2 因果守衛。
- 提交前完整 `make check` exit 0：1920 passed、1 skipped、1 個既有 warning，
  ruff／mypy clean；分批提交的 pre-commit hooks 均通過。上述「尚未 commit」為歷次
  工作紀錄，本節更新最終交付狀態。A2／新 synthesis 的 live 驗收缺口仍保留。
- 下一工作：Scout 正文充分性與訂閱／付費牆提示辨識，先離線實作及測試，
  不重啟 daemon、不修改 live 既有報告。

## 2026-10-30 Google News 正文充分性（交付後續，尚未提交）

- 僅 Google News 包裝網址解析後的媒體頁啟用新聞正文檢查；其他 Scout 來源及 parser
  已提供的內容保留既有契約，避免將短 README 或 arXiv 摘要誤當失敗。
- 移除已知訂閱／cookie／dialog／隱藏區域及以存取提示開頭的文字區塊；若頁面無
  article 且主標題為訂閱／登入／newsletter，也不作正文。只在指定 UI 結構或提示
  句首判斷，保留報導中討論 paywall／subscription 的一般段落。
- 對輸出預算內去除重複行後的文字計算字母／數字數，至少
  `SCOUT_NEWS_MIN_CONTENT_CHARS`（env，預設 300，最小 1）才作可用正文。
  不足即回空字串，沿用現有 snippet grounding 與「未取得內文」標記；原媒體 URL
  與原 feed GUID 不變，失敗仍依解析後的媒體網域記 strike。
- 這是保守的 heuristic，會將短新聞／部分付費牆摘錄降級，也不能保證排除所有長篇
  boilerplate 或報價頁；未宣稱已完成新聞相關性篩選或通用 readability 引擎。
- 新增 14 個 hermetic 案例，包含訂閱頁、人機驗證、重複提示、隱藏文字、正文與 UI
  混合、正常訂閱議題報導、可設定門檻及 digest 的 prompt／report／domain strike
  整合路徑。既有 Google 原網址解析成功 fixture 改為實質長度正文；短正文改由新
  降級案例覆蓋，解析流程及 GUID 契約不變。
- `make check` exit 0：ruff clean、mypy 168 source files 無錯誤；
  **1934 passed、1 skipped、1 個既有 Chroma warning**；`git diff --check` clean。
- 前一批六個 commits 已推送至 origin 功能分支；本節的新 code／tests／docs 保留
  未提交狀態。本次未重啟 daemon、未抓 live 網頁或更動既有 Scout 報告；需後續載入
  新碼再抽查實際新聞頁。

### 2026-10-30 11:45 Scout 重啟後驗證

- 使用者確認重啟（PID 檔 11:35 更新），經正常 prompt queue 送出
  `@ling-scout-verify-content-20261030.md`，沿用 daemon busy lock 與原 Scout 設定。
  run `run_6d36269d171049cd96ee9c328e8dae4b` 在 11:35:36–11:45:39 succeeded；
  29 次 scout_summarize 呼叫均 succeeded，最後 busy=false，命令已歸檔。
- 四來源抓取皆成功，29 新項目（Google News 4、HN 15、arXiv 10、GitHub 0）。
  Google News 列表 10 項，因既有去重只分析 4 新項目：4/4 為原媒體網址，
  Yahoo Finance 與 Microsoft 取得正文，AP／UN 以既有缺正文標記降級。
- 兩則降級另做唯讀複查：AP HTTP 403；UN HTTP 200 但新舊擷取均 0 字，
  不將這兩例的缺正文歸因新門檻。沒有修改 seen／target 時鐘來強制重複分析。
- 額外唯讀抽查先前誤判的 Brookings 原文頁：HTTP 200，舊擷取 59 字
  （The Beijing Brief 節目／訂閱提示），新 news 判定為空正文，證實已知誤判
  被排除。此為獨立 HTTP＋純擷取比較，未呼叫 LLM、未重寫舊 Scout 報告；
  不宣稱它是本輪 daemon 的新項目。
- 日報 `fromLingLing/✅Scout-2026-10-30.md` 按既有同日命名更新；
  `Notes/Scout/✅Scout-2026-10-30.md` 與日報內容完全相同。seen 458→487，
  Google crawl clock 與本輪一致，沒有中轉網址被當作新聞連結。
- 本輪未修改 Python code；最近 `make check` exit 0 為 1934 passed、1 skipped、
  1 個既有 warning。`git diff --check` clean。Google News 新正文 heuristic 的
  離線與上述樣本驗證完成，仍不保證排除所有未知站型。這批新改進尚未 commit／push。

### 2026-10-30 正文判定交付

- 使用者授權將上述 Scout 正文改善與實跑驗證紀錄 commit／push 至
  `codex/a2-audit-hardening`；上方「尚未提交」為實作時的歷史狀態。
- 提交前重新執行 `make check` exit 0：ruff／mypy clean，1934 passed、
  1 skipped、1 個既有 Chroma deprecation warning；`git diff --check` clean。

## 2026-10-30 A2 supports 範圍稽核（唯讀）

- 新增 `A2_support_quality_audit_2026-10-30.md`，盤點 28 筆 supports；只有 8 筆／
  7 條 active claim 有當次完整 supports trace。人工逐段分類為局部 5、類比 2、
  機制不相符 1，未找到足以無保留支持完整 claim 的單段樣本。這是 trace retention
  留下的偏樣本，不推算全部 28 筆的錯誤率，也不等於主張被反證。
- 具體缺口包括：NIST 通用控制移轉法律插件、CRM／衰減圖未涵蓋 Beta-Binomial
  與特徵選取、線上事務驗證被誤作 CoT 預編譯、政治根基類比物理零容錯結構、
  一般槓桿理論類比併購。API／Skills 兩來源各支持組件，不各算一次完整驗證。
- 後續優先：支持範圍（direct／partial／analogy）＋引文及內容 basis 的可覆核契約，
  僅完整直接支持可強化；部分支持保留探索用途。來源獨立性與原始 lineage 為另一
  缺口，空 sources 不能證明獨立或循環，不以不同 title 直接等同獨立證據。
- 未修改歷史 evidence、confidence、S 或索引，未調門檻／強化幅度、未呼叫 LLM。
  本輪為文件交付，未重跑 `make check`；`git diff --check` clean。
  新文件尚未 commit／push；前一批 Scout 已提交至 `a5339ce`。

### 2026-10-30 A2 品質稽核交付

- 依使用者授權提交並推送本輪稽核文件；提交前重新執行 `make check` exit 0：
  ruff／mypy clean，1934 passed、1 skipped、1 個既有 Chroma warning。
- 接續評估開發計畫，另建目前有效的未完成工作排序與驗收清單，避免把歷史階段
  的「未完成」文字當成現行 backlog。


## 2026-10-31 A01 固定判讀評估基準

- 依 [目前開發計畫](Development_Plan_2026-10-30.md) 建立 24 個合成案例與純離線評分器。
  [使用與標準](../tests/fixtures/evidence_evaluation/README.md) 明示 assistant-authored
  標註仍待獨立複核；未把歷史私有段落複製進 Git，也未宣稱已驗證真模型。
- 覆蓋完整／局部支持、類比、反證、中立，以及複合 claim、規範主張、錯誤機制、
  假引用與 prompt injection。只匯出輸入、不洩漏答案；結果綁內容 hash，錯版拒絕。
- 全 neutral 與全支持皆不能通過。缺漏／error 留在分母，不偽裝 neutral。
  當前 production A2 與 apply 契約不變，後續由 A02／A03 接入並做隔離模型評估。
- `make check` exit 0：ruff／mypy 169 source files clean，1965 passed、1 skipped、
  1 個既有 Chroma warning；`git diff --check` clean。未重啟或操作 live 資料。
- 本輪新碼、測試、文件及前輪未提交的開發計畫保留在功能分支，未 commit／push。


## 2026-10-31 A02 支持範圍契約（離線實作）

- 詳見 [目前開發計畫 §11](Development_Plan_2026-10-30.md#11-2026-10-31-a02-支持範圍契約)。
  新 v2 判讀區分完整支持、局部、類比、反證及 neutral，要求引文／未支持部分／適用範圍；
  舊 supports、缺欄、錯版及不存在引文不接受。局部／類比不新增完整 evidence 或強化。
- LLMClient 專用方法與 vault operation asset 接上兩次 bounded content reroll，transport
  不加碼重試。apply 邊界重驗整批及傳入 basis；scope metadata 在 Cortex 讀寫後保留。
- 新增 41 個隔離回歸案例，既有索引交易／故障恢復驗證保留。
  `make check` exit 0：ruff clean、mypy 170 source files clean；2006 passed、1 skipped、
  1 個既有 Chroma warning；`git diff --check` exit 0。
- 真模型語義評估／A01 標註複核、A03 durable audit 與 A04 lineage 仍待驗收或實作；
  不把字面引文匹配當語義支持。未重啟 daemon、未呼叫真模型／RAG、未修改 live evidence／DB，
  未 commit／push；先前 A01 與計畫文件的未提交變更保留。


## 2026-10-31 A03 可重播私有稽核與整合交付

- [稽核格式與 commit 契約](Evidence_Audit_Record_Contract.md) 已實作：判讀 basis／
  passage／prompt snapshots、hash、來源 family／path／範圍及模型／trace 資訊持久化；
  稽核無法落盤不得套用，提交前重讀 basis，保留競寫的人工編輯。
- audit 內容定址且不自動清除；evidence／counterpoint 引用 audit ID。崩潰前留下的
  preparation 不視為已套用；vault commit 後只追補索引，避免重複強化。
  snapshots 存於被 Git 忽略的私有 Database，不入 RAG；詳細限制見契約。
- 本輪新增 40 個隔離測試；`make check` exit 0：ruff／mypy 171 source files clean，
  2046 passed、1 skipped、1 個既有 Chroma warning；`git diff --check` exit 0。
- 使用者授權本段完成後 commit、合併 main 並 push。A01 `be6d59a`、A02 `8ece600`、
  A03 `8e25475` 已各自提交，交接文件另列一次提交；遠端整合狀態以本輪 Git 結果為準。
- A01 標註複核／真模型評估、A04 lineage 與 A05 live 驗收仍欠。使用者於本輪之前
  重啟 daemon，不代表之後新增的 A03 已載入；本輪未自行重啟或觸發 live 寫入。
