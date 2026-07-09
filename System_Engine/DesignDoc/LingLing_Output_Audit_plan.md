# LingLing 產出品質稽核計劃（fromLingLing/ + Insights/）

> 撰寫：2026-07-07（Fable 預掃描）。更新：2026-07-09（納入干預成效驗證）。
> 執行者：Opus（日後由 Steven 觸發）。
> 目的：對 daydream / scheduler / research 等自動產出做一次系統性體檢，
> 判斷「累積觀察期」的產出是否值得留、管線訊號是否可信、內容是否同質化。
> 本計劃只做**讀取與評估**，不修檔、不動 daemon、不 reindex。
>
> ⚠️ **2026-07-09 起這份稽核多了第二個任務：驗證下列干預是否真的有效。**
> 預掃描（7/7）到執行之間，已針對發現的缺陷做了多項修復（見 §0.1 干預基線）。
> 因此稽核不只是「體檢現況」，更是「量測 before/after」——很多疑點的裁決
> 從「有沒有病」變成「治好了沒、有沒有治過頭」。新增 **G 軸**專責此事。

---

## 0. 語料快照（2026-07-07 預掃描結果，執行時請重跑確認）

- `lings-desktop/fromLingLing/`：**200 檔**，日期 2026-05-25 → 2026-07-07。
  - ✅ins 59（montecarlo 41 / full 11 / plan 4 / improve 3）
  - ✅sys 49（ctx-consol 15 / eval 6 / ctx-valid 6 / canary-echo 5 / memoir 4 / routing 3 / diag、admin、Tags、Patrol 各 2 / 總覽、DB Repair 各 1）
  - 💌re 34（research 21 / review 3 / blog 3 / 其餘零星）
  - ✅vis 16、✅ctx 11、✅blog 7、✅admin 5、✅lens 3、🎴複習卡 6、🚨alert 1、📄sw 2、雜項 ~5
- `lings-desktop/Insights/`：**56 檔**（insight-montecarlo 為主 + full-insight + 早期 🎐 檔），共 ~42k 行。

> 執行時重跑統計：語料在 7/7 之後持續累積（每日 1 篇、operation 輪替；
> 妙法蓮華經 7/7 重新 ingest）。務必以「執行當下」的檔數為準，並用
> daemon 重啟時間點（見 §0.1）切分「舊行為 vs 新行為」樣本。

### 0.1 干預基線（7/7 預掃描 → 執行之間已做的修改，稽核要驗證成效）

| 干預 | commit | 影響面 | 稽核該驗證什麼 |
|---|---|---|---|
| insight 訊號三症狀修復（巢狀 pages 解析 + sidecar 維度自癒） | 4a17afa | novelty/bridging/groundedness/refute 來源 | 見 S1–S3 |
| 存量 56 檔重簽（no-refute、時序重放） | 3d5eb9d | 全存量訊號值 | novelty null 0/56、bridging 46 檔有值；驗證重算正確 |
| daydream makeup gate + `daydream_spontaneous: false` | bcaee08 | **產出頻率：每日 2→1 篇** | 執行時確認 7/9 起每日僅 1 篇、無「一夜三篇」重演 |
| operation 輪替（montecarlo/counterfactual/analogy/dialogue/fable）+ 每技能分段溫度 | a0ff3c1 | insight 產出形態 | 見 G 軸；輪替後 novelty/SE 是否散開 |
| resynthesize 路徑式 wikilink 修復 | dbece90 | @ling-resynthesize | 抽查妙法蓮華經是否成功重 synthesis |
| **O0：Cortex 斷邊修復（link/merge 雙門檻 0.60/0.80 + pending 回訪 + 存量回填）** | a161b81 | Cortex 圖連通度、grounding 集中化 | 見 S4、C 軸、G 軸；連通分量 63→32 是否維持 |
| **SE：語義熵監測（self_assessment 第 7 軸）** | 1e2813c | 新監測軸 | 見 G 軸；insight_dim 11.2 vs cortex_dim 37.6 是否隨輪替上升 |

> daemon 已於 7/9 重啟載入上述引擎變更。輪替五日循環：7/8 montecarlo、
> 7/9 counterfactual、7/10 analogy、7/11 dialogue、7/12 fable。

### 預掃描已發現的疑點（執行時列為必查）

| # | 疑點 | 證據 |
|---|------|------|
| S1 | **novelty 大面積 null**：56 檔中 42 檔 `novelty: null`。**已追因並修復（4a17afa，2026-07-07）**：sidecar 殘留 768 維舊 embedding，換 bge-m3 後 np.dot 維度不合 throw。稽核改為驗證修復後新產出的 novelty 是否有值且分佈合理 | Insights frontmatter 統計；`services/insight_signals.py` |
| S2 | **bridging 幾乎恆為 0.0**（46 檔）＋ refute_verdict 恆 null。**已追因並修復（4a17afa）**：`_load_source_contents` 扁平路徑找不到巢狀 pages → 來源永遠載不到。稽核改為驗證修復後新產出 | 同上 |
| S3 | **groundedness 有 5 檔為 0.0** — 部分是同一扁平路徑缺陷的量測假象（已修）；稽核需判斷修復後仍為低值的檔案是否真的零接地、以及要不要加入庫閘門 | 同上 |

> ✅ 後續（2026-07-07）：daemon 已重啟；存量 56 檔已用 `scripts/backfill_insight_signals.py --force` 重簽完成（3d5eb9d，run_refute=False、時序重放、sidecar 重建）。重簽後分佈：novelty null 0/56（mean 0.143）、bridging 46 檔有值（mean 0.353，仍有 7 檔 0.0：4 檔 🎐 legacy + 3 檔來源無法解析）、groundedness mean 0.596（5 檔仍 0.0）。稽核時：A 軸改為驗證這些重算值 + 檢討 7 檔 bridging=0.0 與 5 檔 groundedness=0.0 個案；refute_verdict 仍全 null（重簽未跑 LLM refute），由新產出累積。novelty mean 僅 0.143 本身是 B 軸同質化的量化證據。
| S4 | **grounding 集中化**：42 檔有 `grounded_on`，但前 4 個 cortex id 出現 40/33/21/19 次 — 幾乎所有 insight 都掛在同 4 個節點上（同溫層）。**根因＝Cortex 斷邊，已由 O0 處理（a161b81）**：連通分量 63→32、最大分量 2→29。稽核改為驗證 **O0 之後新產出的 grounded_on 是否散開**（前 4 佔比是否下降），舊存量不變屬預期 | `grep grounded_on` 統計 |
| S5 | sys-eval 自己報告：Cortex 44 條主張中 **43 條薄證據（≤1 來源）、2 條教條**；insight 平均 novelty 僅 0.15。**薄證據部分由 O0 補邊改善**（related 非空 4/65→38/65）。稽核：重跑 cortex_tensions，比對 O0 前後薄證據/教條數；novelty 0.15 對照重簽後 mean 0.143 與 SE insight_dim | `✅sys-eval-20260629.md`、`services/cortex_tensions.py` |
| S6 | 🚨retrieval alert（2026-06-11）在 bge-m3 修復**之前** — 需確認後續 ctx-valid 是否回穩、alert 是否該歸檔 | `🚨sys-alert-retrieval-20260611-0000.md` |
| S7 | 55/56 Insight 檔含 mermaid — 已知 classDiagram/flowchart 缺陷家族可能存量存在 | doc_quality track |

---

## 1. 檢查軸線（七軸）

### A 軸：管線訊號可信度（機械檢查，先跑）

訊號是 M1–M4 自我改善弧線的地基；訊號壞掉，上層自評全是幻覺。

1. 全量抽出 Insights + fromLingLing ✅ins 的 frontmatter（signals / grounded_on / trace_ids / input_chars / output_chars），做分佈統計：
   - novelty null 比率、隨時間有無改善（是「早期沒算」還是「一直沒算」？對照 `engine_build` 欄位）。
   - bridging 是否恆 0；groundedness 的分佈與 0.0 檔案清單。
   - `refute_verdict` 是否從未有值（若是，M-arc 的證偽環節形同虛設）。
2. grounding 集中度：算每個 cortex id 的出現頻率、Gini/top-4 佔比；對照 Cortex/ 目錄實際節點數，判斷是 seed 選擇偏差還是 Cortex 本身太小。
3. `run_id`/`trace_ids` 抽 5 檔回查 Database/（若 trace 落地）驗證可追溯性是否真的可用。

**產出**：訊號健康表 + 「哪些訊號目前不可信、不應拿來做 M2/M3 決策」的結論。

### B 軸：Insight 內容品質與同質化（LLM 深讀，本次重點）

分層抽樣（見 §2），每檔評四項（1–5 分）：

1. **實質新穎度**：洞察是否超出兩篇來源各自內容的直接複述？「A×B 的橋接」是真橋還是萬用句型（例：什麼都能接到「從規則轉向計算驅動」「從內容過濾轉向架構隔離」這類 meta-pattern 模板）？
2. **模板收斂檢查**：跨檔比對「核心發現／語意關聯分析／戰略建議」三節的句型與結論。具體做法：抽出所有 montecarlo 檔的「戰略建議」條目，聚類看是否 N 篇收斂到同幾條建議。若同質化嚴重，這是 montecarlo pipeline 的溫度/seed 問題，不是內容問題。**以 SE 的有效維度當客觀輔證**（不用只靠人工印象）：對照 §G 的 insight_dim，並**分 operation 切**看輪替後（7/9 起）的檔是否比純 montecarlo 期（7/8 前）散開。
3. **接地正確性**：戰略建議與核心發現中的事實宣稱，抽查 2–3 條回 vault 來源文件驗證，標記幻覺。
4. **配對品質**：來源配對（如 Siddhartha × SecOrchestration）產生的是強行類比還是有效跨域？記錄「哪類配對值得、哪類是浪費 token」，作為未來 seed 策略的輸入。
5. **operation 產出形態差異（輪替後新增）**：counterfactual/analogy/dialogue/fable 是否真的產出「不同形狀」的東西，還是換湯不換藥？各抽 1 檔讀。**注意：fable/dialogue 的 groundedness 天生偏低是設計使然（它們刻意不逐點接地），不得當缺陷扣分**——改判其「創意品質」與「是否仍忠於來源機制」。

**產出**：分項評分表 + 「值得保留 / 建議清理」的檔案分級 + montecarlo/輪替參數建議（溫度、五日循環組成）。

### C 軸：系統自省層有效性（sys-* 家族）

1. **sys-eval 時間序**：6 份 eval 依日期排開，追蹤「觀察」條目 — 同一條觀察（教條主張、薄證據）是否週週重複出現而無收斂？若 M2/M3 從未消化 M1 的觀察，弧線斷在哪一層？（對照 metacognition memory：M3 有一條 proposal 至今未 review。）
2. **ctx-valid / canary-echo 時間序**：pass rate 是否在 2026-06-15 bge-m3 修復後回穩；🚨alert(0611) 的六條失敗查詢如今是否已通過（可對照 `Database/bench_history.json`）。
3. **ctx-consol 產能**：15 份鞏固報告的「處理 insights / 新增主張」數字加總，對照 Cortex 主張數 — 驗證帳是否對得起來；「合併 0、矛盾連結 0」若恆為零，reconsolidation 邏輯可能沒觸發過。**O0 後新增變數**：報告新增 `related link(s)` 與 `補回 pending 邊` 欄位——確認 O0 之後的夜間 consolidation 是否真的在長邊（related_links > 0），以及 pending 佇列有無正常排空。
4. **語義熵軸自檢（SE 新上線）**：self_assessment 現有第 7 軸「語義熵」。確認它每晚有產出數字、trend 有累積（需數次 run）；並人工核對 insight_dim/cortex_dim/source_dim 是否合理（別自己就壞了還拿來當 M2 依據）。

**產出**：自省層是否「有感覺沒行動」的判定 + 斷點定位。

### D 軸：互動型產出正確性（💌re / ✅ctx / ✅lens / ✅vis）

1. 💌re-research（21 份）抽 4 份：arXiv/URL 連結逐一驗證存在性與標題相符（幻覺連結是 research digest 的典型病）；摘要與論文 abstract 抽對 1–2 篇。
2. ✅ctx-recall / ✅lens 抽 3 份：答案是否忠於 vault 內容（這是 RAG explain 路徑的端到端檢驗）。
3. ✅vis 抽 2 份：mermaid/artifact 是否可 render、數字是否與來源筆記一致。

### E 軸：形式規範（機械檢查）

1. **Mermaid 存量健檢**：55 檔 Insight 的 mermaid block 全量跑 parse 驗證（用 `npx -y @mermaid-js/mermaid-cli` 或現有 repair pass 的 validator）；比對已知缺陷家族（classDiagram 成員行、statement 黏連、flowchart）。
2. **Wikilink 完整性**：抽出所有 `[[...]]`，對照 vault 檔名，統計斷鏈率（特別是 `(Synthesis)`/`(Part N)` 後綴目標）。
3. **檔名規範**：fromLingLing 檔名是否還有 sanitization 漏網（空格、括號、超長、`@` 開頭）；`ontology-index.md`、`專利檢查-AI主題.md`、`[review] new profile - novel.md` 這類無前綴檔是誰產的、該不該在這目錄。
4. **語言一致性**：OUTPUT_LANGUAGE 應為 zh-TW — 抽查有無英文/簡體污染（已知 P-family 譯文污染缺陷的存量）。
5. **Persona 表情符號**：內容型產出是否守 soft palette（🚨 只允許出現在 alert 類）。

### F 軸：量能與價值密度（統計）

1. 依週統計產出量與 token（input_chars/output_chars 加總）— montecarlo 佔 41/59，每檔 ~26k output chars；估算「每月 daydream token 成本 vs. B 軸判定值得保留的比率」。
2. 同一來源配對是否被重複抽中多次（浪費）；Vault full-insight 與 montecarlo 的價值比。

**產出**：一頁「值不值得繼續用目前頻率跑 daydream」的量化建議。

### G 軸：反同質化干預成效驗證（本次更新新增，對照 §0.1 干預基線）

這軸是「量測 before/after」——每項干預都要能用數據回答「有效／無效／過頭」。

1. **產出頻率（daydream 修復）**：以 `Insights/` 檔名時間戳，統計 7/9 之後是否
   每日恰 1 篇；確認無「一夜多篇」（比對 7/7 的三篇事件）。若仍多篇，
   `daydream_spontaneous: false` 或 makeup gate 沒生效 → daemon 未載新 code。
2. **novelty 散開（輪替 + 溫度）**：把 novelty 依 `date_created` 排時間序，
   對照 operation 輪替上線（7/9）。輪替後是否脫離 mean 0.143 的窪地？
   **分 operation 看**：fable/dialogue/counterfactual 是否比 montecarlo 高？
3. **語義熵趨勢（SE）**：讀 `Database/self_assessment_history.json` 的
   `語義熵.insight_dim` 序列，看是否隨輪替上升（11.2 是干預前基準）；
   以及產出÷輸入比（daemon 帶 rag 跑後才有 source_dim）。
4. **Cortex 連通度（O0）**：重算連通分量與最大分量
   （`scripts/backfill_cortex_edges.py --dry-run --report` 或直接算），
   確認 63→32、最大 2→29 維持或持續改善；`related`/`contradictions` 非空
   節點比例（4/65→38/65 是否守住）。0 contradiction 是否仍為零（裁決過鬆？）。
5. **grounding 去集中化（O0 下游）**：O0 之後**新產出**的 insight，其
   `grounded_on` 前 4 cortex id 佔比是否低於預掃描的 40/33/21/19（S4）。
6. **回歸副作用**：merge guard 會不會把該合併的也擋掉→ Cortex 出現大量近義
   重複主張？抽查 O0 後新增主張有無語義重複但未合併的個案。

**產出**：干預計分卡（每項：有效／無效／過頭／證據不足 + 數據），以及
「哪些該回退、哪些該加碼」的建議（例：0.60 是否再降 0.55、輪替組成調整）。

- **B 軸深讀**：montecarlo 41 檔 → 分三期各抽 3（早期 0525–0610 / 中期 0611–0625 / 近期 0626–0707），共 9 檔；full-insight 11 檔 → 抽 3（早中晚各 1）；同質化聚類則用**全量**「戰略建議」節（機械抽出後一次比對，成本低）。
- **D 軸**：如 §D 所列（4+3+2）。
- 抽樣用確定性規則（每期按日期排序取第 1、中位、最後一檔），避免 cherry-pick。

## 3. 執行順序與分工建議（給 Opus）

1. **Phase 1（機械，~30 分鐘）**：A + E + F + **G** 軸統計 — 全部可用 bash/python 腳本完成，先產出硬數據。G 軸的連通分量/novelty 時間序/SE 序列都是機械可算的。
2. **Phase 2（LLM 深讀）**：B 軸 9+3 檔（含各 operation 形態抽讀）+ D 軸 9 檔。可用 subagent 並行（每檔一個評分 agent + 統一 rubric），但**聚類/同質化判定必須單一 context 看全量**。
3. **Phase 3（綜合）**：對照 C 軸時間序 + G 軸干預計分卡，寫總報告。

## 4. 報告輸出

- 報告落地：`System_Engine/DesignDoc/LingLing_Output_Audit_report_<date>.md`
- 結構：①執行摘要（四句話：訊號可信嗎／內容值得嗎／自省弧線通嗎／**干預有效嗎**）②七軸發現（含 S1–S7 疑點裁決 + G 軸干預計分卡）③檔案分級清單（保留/清理候選 — **只列清單，不執行刪除**）④管線改善建議（依 impact 排序，各附證據錨點，含「該回退／該加碼」的干預調整建議）。
- 所有結論必附證據（檔名+行號或統計命令），禁止印象式評語。

## 5. 界線（hard constraints）

- 純讀取。不刪檔、不改 frontmatter、不動 `.env`、不重啟 daemon、不 reindex（reindex 有雙寫 segfault 前科，見 memory）。
- 清理建議一律進報告的候選清單，由 Steven 裁決。
- 若需跑 mermaid validator 等工具，寫到 scratchpad，不落在 vault 內。
- G 軸的 `backfill_cortex_edges.py` 只准 **`--dry-run --report`**（純讀，計算連通分量）；**不得**跑不帶 `--dry-run` 的版本（那會寫邊、動 Cortex），修改屬 O0 執行範疇、非稽核範疇。
