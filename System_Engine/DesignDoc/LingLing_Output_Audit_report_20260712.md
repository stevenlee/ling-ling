# LingLing 產出品質稽核報告（2026-07-12）

> 執行：Fable 5（Phase 1 機械統計＋C 軸本體執行；B/D 軸由 4 個深讀 subagent 完成）。
> 依據：[[LingLing_Output_Audit_plan]]（七軸＋S1-S7 疑點＋G 軸干預計分卡）。
> 界線遵守：純讀取，未刪檔、未改 frontmatter、未動 daemon、未 reindex；
> `backfill_cortex_edges.py` 只跑 `--dry-run --report`。
> 統計腳本與中間產物在 session scratchpad（`phase1_afg.py` 等），報告內所有數字可重跑復現。

---

## ① 執行摘要（四句話）

1. **訊號可信嗎？——可信了。** S1-S3 修復全數守住（novelty null 0/64、bridging 恆 0 已解、
   refute 已實跑且有 verdict），trace 可追溯性抽查 5/5 全中；訊號層已足以支撐 M2/M3 決策。
2. **內容值得嗎？——料進步中、殼仍是病灶。** montecarlo 殼的戰略建議同質化裁定**嚴重**
   （7 個模板聚類收攏 53/54 檔、55% 建議跨主題可互換），但 operation 輪替的 dialogue/fable
   是貨真價實的新形態（novelty 0.27-0.41 vs montecarlo 0.126）。
3. **自省弧線通嗎？——感覺層通、行動層斷。** 6 份 sys-eval 逐字重複同 2 條觀察、薄證據
   12→52 線性惡化、唯一 M3 提案 pending 28 天未審——M1 有感覺，M2/M3 沒消化。
4. **干預有效嗎？——6 項干預 5 項有效、1 項初步出現。** 頻率✓、novelty 散開✓、SE 上升✓、
   連通度✓、merge guard 無過頭（但有 1 對實錘漏合併）；grounding 去集中化在 7/12 排程檔
   首次實現（單點，待累積）。

---

## ② 七軸發現

### A 軸：管線訊號可信度 ✅

| 訊號 | 分佈（n=64） | 判定 |
|---|---|---|
| novelty | null 0、mean 0.150、median 0.124 | ✓ S1 修復守住 |
| bridging | null 14、zero 8、mean 0.349（有值者） | ✓ S2 已解；zero 8＝4 🎐legacy＋3 來源不可解析＋1 個案 |
| groundedness | mean 0.614、zero 5 | ✓ S3 降級為個案（4 legacy＋1 檔） |
| refute_verdict | None 58 / refuted 5 / survived 3 | ✓ 夜間已實跑；**校準問題見 B 軸** |

- 可追溯性：最新 5 檔的 trace_ids（15-16 條/檔）**全部**在 `llm_trace.sqlite` 的 `llm_calls`
  命中，run_id 在 `runs` 命中 5/5 —— 端到端可回放。
- refuted 的 insight 不會進 Cortex（`cortex_consolidation.py:581` `_is_candidate` 過濾）＋
  groundedness 閘門同在 —— 消毒管線成立。
- **grounding 集中（全量）**：50 檔有 `grounded_on` 但只掛過 **12 個** distinct cortex id，
  top-4 佔比 0.878（eb4d 48 次/84ee 41/b337 27/db2a 21）。根因與去向見 G5。

**結論：訊號健康表全綠，可拿來做 M2/M3 決策。**唯 refute 對創意型 operation 的校準要修（B 軸）。

### B 軸：Insight 內容品質與同質化（LLM 深讀 14 檔＋全量聚類）

**同質化聚類（54 節戰略建議全量、單一 context）：裁定「嚴重」。**

| 聚類（代表句型） | 檔數 | 佔比 |
|---|---|---|
| 建立量化指標／監控／稽核框架 | 23 | 43% |
| 種子庫擴展／配對策略優化（meta 建議） | 14 | 26% |
| 開發技術原型（PoC）驗證冠軍洞察 | 12 | 22% |
| 標準化類比模板／隱喻庫 | 10 | 19% |
| 邊界治理／負向定義 | 10 | 19% |

前三大聚類覆蓋 ~80% 檔案；~55% 檔案的建議把主題名詞替換後放到他檔仍成立；95% 條目是
「動詞＋『術語』＋框架/模板/庫」句型填空。同質化的病灶在 montecarlo **殼**不在**料**——
料（配對野心、接地精度）三期緩升，殼（骨架、建議節）三期零演化。
同質化並已滲入信念層：Cortex 主張標題大面積「從 X 轉向 Y」句型（見 G6 樣本）。

**operation 輪替形態評分（7/9-7/12 各檔深讀）：**

| operation | 形態忠實度 | novelty | 判定 |
|---|---|---|---|
| dialogue（7/11 排程） | 5/5 真對話體 | 0.266 | 具名輪替/打斷/裁決/研究種子，survived |
| fable（7/10 手動） | 5/5 真寓言 | 0.413 | 敘事弧＋寓意＋脫戲服；「剪去自我的特徵」是真文學動作 |
| fable（7/12 排程） | — | 0.374 | survived、bridging 0.425、grounded_on 全新 6 id |
| analogy（7/10 排程） | 4/5 | 0.135 | 映射表＋撕裂線全兌現，但報告殼未脫、映射偏教科書 |
| counterfactual（7/9 排程） | 3/5 | 0.148 | 四成 seed 退回標準分析——殼同化力最強的一環 |

**refute 校準缺陷（本軸頭條）**：analogy 0710 是明確誤殺——groundedness 0.867（四檔最高）、
事實主張全對，refute 代理疑似把 exercise 明文要求的「撕裂線」（誠實標注類比失效處）當成
證偽證據——**懲罰認識論誠實**。counterfactual 半對（冠軍段確實無支撐，但反事實體天生產生
「被否定前提下的推演」，整檔級 verdict 過粗）。現況並自相矛盾：fable 豁免（null）、
dialogue 進閘門倖存、analogy/counterfactual 進閘門被殺。
→ 建議：refute 只評「可抽取的可轉移原則／最終建議」，或對 analogy/counterfactual 比照
fable 豁免＋降權進 Cortex。

**full-insight 三期**（0603/0613/0707）：接地精度緩升（3/5→4/5，SpaceX「收盤價高於發行價
30%」可在 Part 161 逐字對上）、模板骨架零演化；**「策略段 LLM 拒答原文照登」貫穿三期**
（「請現在貼上文章內容，我將立即為您開始工作！」、"no specific insights can-not be taken
from thess____" 這類 stub 直接進正式檔，每檔 2-3 個死維度）——critique 閘門在 insight
pipeline 的對應缺陷，與 doc_quality track 的 P1 同族。

**montecarlo 中期/近期 6 檔＋legacy 對照深讀（附錄 B′ 全文，此處摘要）：**
- 評分帶：新穎度 2-3/5、模板收斂 1-2/5（嚴重）、接地 2-4/5、配對 2-4/5。
- **趨勢反轉發現**：中期問題是幻覺（0613 黑格爾錯灌上下文、0625 把 P vs NP 整節誤讀成
  「即插即用 Plug-and-Play」——來源全庫 grep 零命中），近期問題轉為同質化＋
  「單一洞察攤薄成多節」（0626/0702 皆是一橋改寫三節）；接地紀律近期明顯改善
  （0626 groundedness 0.796、抽驗逐字命中）。
- **交叉污染**：7 檔中至少 5 檔引用非配對來源（cortex 檢索混入）——引用鏈可信度隱患。
- 吃字/控制字元存量殘留（範amen/垂直語蟻/sub語graph→直接害 mermaid fail）＝
  LaTeX-in-JSON 家族同源存量；0625 有兩張「開始→處理中→結束」佔位空殼 mermaid。
- 模板句型 top 3（原文）：①「從『X』轉向『Y』」（7/7 檔全中、單檔可 4 次）
  ②冠軍頒獎句「該洞察之所以脫穎而出…」③戰略建議三件套（動詞恆為建立/深化/強化/擴展）。
- legacy 對照：節構多樣但 96k chars 過長、自評膨脹（9/10）；montecarlo 固定評估殼
  佔 1/4 篇幅、幾乎零資訊增量。

### C 軸：系統自省層有效性 ⚠️

- **M-arc 斷點實錘**：6 份 sys-eval（0614→0706）「觀察」節**逐字相同**（2 條教條主張＋
  薄證據 N 條），薄證據 12→22→28→31→43→52 線性惡化、教條恆 2、已證偽恆 0。
  M1 每週產生同樣的感覺，M2/M3 從未把它變成提案；唯一的 M3 提案
  （`agent_counter-20260614`，lens_report 主題）與重複觀察無關，且 pending 28 天未審。
  **弧線斷在 M1→M2 的消化層＋人工 review 停滯兩處。**
- 檢索回穩 ✓：bench pass_rate 連續 1.0（7/3-7/11）；🚨alert(0611) 的失敗查詢已全數通過
  → alert 檔可歸檔。
- consolidation 有在長邊（maintenance.log 7/10 兩次 run 各 +2/+3 related）；但
  `_write_report`（`cortex_consolidation.py:809`）漏印 `related_links`/`pending_resolved`
  欄位——人讀報告看不到 O0 的邊成長，只有 log 有。順帶：7/11 dialogue 鞏固結果
  「1 insight → 0 new claims」——創作型產出貢獻較少主張，設計使然但值得追蹤。
- SE 軸自檢：實跑有數字（G3）；canary echo 樣本不足（grounded 1/5）暫不評斷，
  novelty grounded ≥ cold 方向健康。
- **附帶重大發現（已修）**：`tests/test_self_assessment.py` 的 `_paths()` 漏導 `history_file`，
  每跑一次測試就往正式 trend 檔塞 12 筆 fixture；HISTORY_MAX=180 剛好被 15 次測試 run 填滿，
  **真實夜間 trend 紀錄全數被擠出**——SE 燈號的「連 3 降」機制等於從未真正運作。
  已修（6ef2c51）＋清檔重新累積（污染備份在 scratchpad）。

### D 軸：互動型產出正確性 ✅（9 檔）

- **research 幻覺連結率 0/14 可驗連結**（arXiv 6＋Wikipedia 4＋專利抽 4 全部存在且標題
  逐字相符；「271 位從業者調查」「PoW 保護攔截金鑰」等易編造細節全屬實）。
  瑕疵：JEPA 檔（0628-1120）**全檔零連結**＋LeJEPA 年代錯置（誤植 2022-2023，實為
  2025-11 提交）＋「202模態」截斷。
- **RAG 忠實度 3/3**：引用宣稱、信心值（0.50）、falsifier 與 Cortex 信念檔逐字一致。
  但 `✅ctx-recall-Hibert-0614-0006` 第 19-316 行 **CoT 全文洩漏**（prompt 指令複述＋
  多輪 "Wait, let me re-check"＋5 版非法 mermaid 草稿），最終答案未輸出。
  全庫掃描僅此一檔（6/14 孤例，非家族）→ 清理候選。
- lens-DG：誠實空結果（`total_count: 0` 前後一致，無硬湊）。vis 2/2 正確
  （術語/數字全數在同名來源筆記落地；僅 `$open$` 數學定界符誤用＋「反駁欄」語義錯置小瑕）。

### E 軸：形式規範

| 項 | 結果 |
|---|---|
| E1 mermaid（mmdc 真 parse 全量） | **563 blocks、533 pass（94.7%）、30 fail 落在 ~24 檔**；fail 集中 graph/flowchart（quoted subgraph title 內特殊字元、sequenceDiagram `participant "AI 代理" as "Agent"` 非法）＝已知 deferred flowchart 家族；**classDiagram 家族 0 fail**（三 commit 修復有效） |
| E2 wikilink | 8484 條中 **2119 斷（25%）**；top 斷鏈全是長標題截斷/命名不一致家族（AI Tokenomics… 26 次、Safe Untrusted… 20 次、NLA Part 19 16 次） |
| E3 檔名 | 0 漏網 ✓（sanitize_filename 9584dee 有效）；無前綴漏網 3 檔（ontology-index.md、專利檢查-AI主題.md、[review] new profile - novel.md） |
| E4 語言 | 4 檔疑似簡體污染（0611 full-insight 20 個簡體字元實錘；另有越南文 đạt、緬文 စွာ 洩漏個案）；污損字元家族零星跨檔（楊立動/sub raph/V-JENA） |
| E5 emoji palette | **83 檔含 🧠**（LLM 自生「## 🧠 邏輯結構圖」標題，非程式模板）＋27 檔 ⚠️——palette 規則沒進 insight prompt |

### F 軸：量能與價值密度

- 週產出 W22-W28：每週 3-15 檔、out_chars 12-37 萬/週；Insights 總量 64 檔 ~163 萬 chars。
- 重複配對 0 ✓（ledger 防重有效）。
- 對照 B 軸分級：目前頻率（1 篇/天）合理；**該省的不是頻率是殼**——montecarlo 殼佔每檔
  約八成字數且是同質化主體，creative report_mode（19-24k vs 32k）已證明可砍。

### G 軸：干預計分卡（本次稽核第二任務）

| 干預 | 裁決 | 證據 |
|---|---|---|
| daydream 頻率修復（bcaee08） | **有效** | 排程 7/8-7/12 每日恰 1 篇；無一夜多篇重演（7/10 的 4 篇為 Steven 手動測試） |
| insight 訊號修復＋重簽（4a17afa/3d5eb9d） | **有效** | novelty null 0/64、bridging mean 0.349、refute 有 verdict |
| operation 輪替＋分段溫度（a0ff3c1）＋creative report_mode（a2028d3） | **有效（形態）/部分有效（novelty）** | dialogue/fable 真新形態（5/5）；novelty pre 0.142（n=57）→ post 0.235（n=8）；但 analogy 0.135/counterfactual 0.148 未散開，counterfactual 四成退化 |
| resynthesize wikilink 修復（dbece90） | 未抽查（妙法蓮華經重 synthesis 屬 doc_quality 範疇） | — |
| O0 Cortex 修邊（a161b81） | **有效、守住** | related 非空 42/71（59%）；夜間有長邊（+2/+3）；71 節點 35 分量（新增 6 主張後回彈 3 分量屬預期）；**contradictions 仍 0/71——裁決從未判過 contradicts，過鬆疑慮成立**；1 主張 embedding NaN（毒輸入佔位） |
| SE 語義熵（1e2813c） | **有效（指標）/趨勢重新累積** | insight_dim 11.2（7/9）→ **13.14**（7/12 現算）↑、cortex_dim 37.6→40.2；trend 歷史因測試污染歸零重來（6ef2c51 已修根因） |
| grounding 去集中化（O0 下游） | **初步出現、待累積** | post-O0 前 6 檔仍全掛舊 top-3（手動 [Vault] 檔主導樣本）；**7/12 排程 fable 的 6 個 grounded_on 與舊 top-4 零重疊**——首次真實散開。根因已定位：`_cortex_priors`（monte_carlo.py:248）用 recall 純相關性 top-3，可接地池實有 62 條但中心主張永遠贏；建議 ε-explore/MMR 多樣性注入 |
| merge guard 回歸副作用 | **無過頭、有 1 漏** | 70 主張僅 2 對 cosine≥0.80 未合併；0.975 那對（「代理式**智慧**」vs「代理式**實踐**」，fde7b686/7cf26e9a）一字之差該合沒合——個案實錘 |

## ③ S1-S7 疑點裁決

| # | 裁決 |
|---|---|
| S1 novelty null | **CLOSED**（null 0/64，修復守住） |
| S2 bridging 0/refute null | **CLOSED**（bridging mean 0.349；refute 已實跑）——新問題移交 B 軸：refute 對創意型過嚴 |
| S3 groundedness 0 | **降級個案**（剩 5 檔：4 🎐legacy＋1 檔 0623） |
| S4 grounding 集中 | **OPEN、根因移位**：不是斷邊（O0 已修），是 `_cortex_priors` 純相關性 top-3；7/12 首見散開 |
| S5 薄證據/教條 | **OPEN、升級**：52/53 薄證據、教條恆 2、已證偽恆 0，M-arc 斷點（C 軸） |
| S6 0611 alert | **歸檔**（bench 連續 1.0） |
| S7 mermaid 存量 | **量化完成**：30/563 fail（5.3%），flowchart/graph 家族，classDiagram 已清零 |

## ④ 檔案分級清單（只列清單，不執行刪除）

**清理候選（7）**
- `Insights/[20260613-064725][Vault][full-insight].md` — 污損最重（緬文/斷鏈 wikilink ×5+/殘破 stub）
- `Insights/[20260603-144003][…][full-insight].md` — 3 個死段落（LLM 拒答原文照登）
- `Insights/[20260625-063053][AgenticDB…+PNP問題][insight-montecarlo].md` — R1-2 整節建立在
  「P vs NP＝即插即用」幻覺解讀上＋自我配對複述＋2 張佔位空殼 mermaid
- `Insights/[20260702-043156][Agentic Auto-Scheduling+RL…][insight-montecarlo].md` — 同域拼接、
  三節互抄（R1-1 一節可摘存）
- `fromLingLing/✅ctx-recall-關於 Hibert 的所有記憶-20260614-0006.md` — CoT 全文洩漏、無最終答案
- `fromLingLing/🚨sys-alert-retrieval-20260611-0000.md` — 已回穩，歸檔
- `fromLingLing/💌re-research JEPA research-20260628-1120.md` — 零連結＋年代錯置（或重簽）

**標註存疑／邊緣（5）**：counterfactual 0709 冠軍段（自由聯想當戰略建議）；0629 同日兩檔
（首條建議近乎重複）；montecarlo 0613/0626/0707（模板重或單橋攤薄，邊緣保留）。
**其餘**：保留。dialogue/fable 全數保留（novelty 與形態雙優）。

## ⑤ 管線改善建議（依 impact 排序）

1. **殼改造（同質化主因）**：montecarlo 報告殼（Scorecard/戰略建議三件套）是嚴重同質化的
   病灶且佔八成字數——把 creative report_mode 的「輕量殼」推廣到全部 operation，或至少
   讓「戰略建議」節 operation-aware（衝擊 B 軸 43% 最大聚類）。證據：聚類表＋三期骨架零演化。
2. **refute operation-aware 校準**：停止懲罰撕裂線；只證偽「可轉移原則/最終建議」。
   證據：analogy 0710 誤殺實錘。連動：refuted 不進 Cortex → 誤殺=白燒 token＋知識漏記。
3. **M-arc 接通**：讓 M2 diagnosis 消化「教條/薄證據」重複觀察（現成 52 條薄證據就是
   種子）；審掉 pending 28 天的 agent_counter 提案。證據：6 份 eval 逐字重複。
4. **_cortex_priors 多樣性注入**（ε-explore 或 MMR）：62 條可接地池不該只用 12 條。
   證據：top-4 佔比 0.878；7/12 散開單點證明池夠用。
5. **insight prompt 補 palette／語言約束**：83 檔 🧠 標題＋簡體/外文洩漏個案，一條 prompt
   規則可清（新增檔）；存量可留（非功能性）。
6. **引用交叉污染**：montecarlo 7 檔中 5 檔引用非配對來源（cortex/RAG 檢索混入正文引用）——
   引用鏈可信度隱患；建議 wikilink 引用限定白名單（本次配對來源＋grounded_on 主張）。
7. **小修**：consolidation 報告模板補 `related_links`/`pending` 欄位（809 行）；
   0.975 近重複主張對人工裁併；NaN embedding 主張（「The vitality of complex systems…」）
   重算；wikilink 25% 斷鏈家族（長標題截斷）併入 doc_quality deferred 處理。

---

## 附錄 B′：montecarlo/legacy 深讀評分表

評分約定：模板收斂 5＝無模板化、1＝嚴重模板化。

| 檔 | 新穎 | 模板 | 接地 | 配對 | 判定 | 關鍵證據 |
|---|---|---|---|---|---|---|
| 0613 簡立峰×Hardy | 3 | 2 | 3 | 3 | 保留（邊緣） | R1-1 自承「上下文僅含黑格爾文獻」仍硬寫；SOP→Skills 抽驗命中 |
| 0620 HBM×ClaudeCode | 3 | 2 | 4 | 4 | 保留 | TSV→LSP 逐層對映可操作；R1-3 同橋重寫 |
| 0625 AgenticDB×PNP | 2 | 2 | 2 | 2 | **清理候選** | 「P vs NP＝即插即用」幻覺整節；自我配對；佔位 mermaid ×2 |
| 0626 vibe×PDE | 3 | 2 | 4 | 3 | 保留（邊緣） | Babuška-Brezzi→驗證代理 σ 可證偽；一橋攤三節 |
| 0702 AutoSched×RL | 3 | 2 | 4 | 2 | **清理候選** | 「5% 有益特徵/53 OOD 評估」逐字命中；同域拼接三節互抄 |
| 0707 記憶毒化×BitterLesson | 2 | 1 | 4 | 3 | 保留（邊緣） | 元模式原句＝「從規則定義轉向計算驅動」模板本尊；npm postinstall 抽驗命中 |
| 🎐0527 legacy | 3 | 3 | 3 | 3 | 保留（基準樣本） | 節構多樣但 96k 過長、自評 9/10 膨脹；NIST GV-1.4-002 命中 |

深讀原始評語（含逐條證據與行號）存於稽核 session 紀錄；引用時以本表＋正文摘要為準。

## ⑥ 執行條件備註

- 稽核執行期間（7/12 凌晨）7/12 fable 於 03:21 落地，五日輪替樣本完整。
- SE trend 序列因測試污染於 7/12 歸零重建，G3 的趨勢燈號要再累積數晚才可信。
- B 軸 montecarlo 中期/近期 6 檔＋legacy 深讀因 session limit 中斷重跑，結果見附錄 B'。
