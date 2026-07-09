# 實作計劃：本體論學習機制 + 語義熵監測

> 撰寫 2026-07-09（Opus，接續 LingLing 產出稽核 track 的同質化調查）。
> 一句話動機：稽核量到 novelty mean 0.143（同質化），追因發現 **Cortex 信念圖
> 幾乎沒有邊**——65 個主張節點，`contradictions` 全空、`related` 僅 4 檔非空。
> 這同時是「本體論」缺席的根因，也是語義熵該監測的對象。本計劃分三段解決：
> O0 修圖的邊、SE 建語義熵監測、O1–O4 長出本體論層。
>
> **界線**：所有自動修改一律走 metacognition 的閘門提案制（見 [[SelfImprovement_metacognition_plan]]），
> 不自動改寫既有主張；新旗標一律 default-off；設定進 Scripture 非 .env。

---

## 0. 現況盤點（2026-07-09 實查）

| 元件 | 現況 | 檔案 |
|---|---|---|
| 「Ontology」概念圖 | 每篇 Synthesis 檔尾一張 mermaid（34 篇索引在 `fromLingLing/ontology-index.md`）；**視覺產物、看完即棄、不跨文件、不累積** | `services/learning_artifacts.py` |
| Cortex 信念圖 | 65 主張節點，帶 `confidence`/`falsifiability`/`S`(衰減)/`status`；裁決產生型別邊 `equivalent`/`entails`/`entailed_by`/`complementary`/`contradicts` | `services/cortex_store.py`、`maintenance/cortex_consolidation.py` |
| 邊的實況 | **65 檔 `contradictions` 全空；61/65 `related` 全空**——整張圖≈65 個孤島 | `lings-desktop/Cortex/*.md` |
| 語義訊號 | insight 已有 novelty（距歷史最近鄰距離）、bridging（來源間離散度）；`self_assessment` 已讀 `mean_novelty`/`mean_groundedness` | `services/insight_signals.py`、`maintenance/self_assessment.py:291` |
| 自評框架 | 6 軸、純讀零 LLM、夜間跑、RED/YELLOW 才寫報告 | `maintenance/self_assessment.py:337` |

### 斷圖的兩個精確根因（本計劃 O0 要解的）

1. **相似度門檻 0.80 太高，濾掉了跨域邊。** `_ingest_claim` 補邊時只對
   `sim ≥ CORTEX_NEIGHBOR_SIM_THRESHOLD`(=0.80) 的 top-K 鄰居做裁決
   （`cortex_consolidation.py:343`）。但 montecarlo insight 的主張本來就是
   跨域的，彼此 cosine 極少到 0.80 → `neighbors` 常為空 → 連裁決都不觸發 →
   零邊。這解釋了為什麼 `related`/`contradictions` 幾乎全空。
2. **配額耗盡的主張永不回訪。** 每晚配額 `DAYDREAM_BITE_ADJUDICATIONS=4`
   （夜間 scheduler 走 `CORTEX_MAX_ADJUDICATIONS_PER_NIGHT=20`）。補邊迴圈
   在配額用完時 `break`（`cortex_consolidation.py:354`），但該主張**仍會被
   寫入**（line 365「No equivalent found → a new claim enters」），帶著殘缺
   或零邊，而且 `adjudications_used` 每次 run 歸零、主張已標記完成，
   **沒有任何機制隔天回來補剩下的邊**。忙碌/配額耗盡夜進場的主張永久欠連。

> 這兩點是「本體論」與「語義熵」共同的前提：本體論若建在同一套挨餓的裁決上會
> 一樣斷；語義熵若不先修圖，量到的低熵有一部分是 bug 假象而非真同質化。

---

## 段一：O0 — 修 Cortex 的邊（前提，最高投報比）

**目標**：讓 65 個孤島連成圖，同時治好稽核 S4 的 grounding 集中化
（top-4 cortex id 掛在 40/33/21/19 篇 insight 上——不是那 4 個重要，是其餘
61 個沒邊、seed sampler 只好反覆抓有邊的那幾個）。

### O0.1 分離「跨域配對門檻」與「合併門檻」
- 現在 0.80 同時當「找鄰居」與「判等價合併」的門檻，過嚴。
- 拆成兩個 Scripture 旗標：
  - `cortex_link_threshold`（預設 0.55–0.60）：低於此不配對；**這條決定圖的稠密度**。
  - `cortex_merge_threshold`（維持 0.80+）：等價合併仍要高門檻，避免亂併。
- 找鄰居用低門檻取 top-K 候選，是否合併仍由 LLM 裁決 + 高門檻雙重把關。

### O0.2 配額耗盡主張的回訪佇列
- 新增 `Database/cortex_pending_edges.json`：記錄「已進場但補邊未完成」的
  claim_id + 尚未裁決的鄰居清單。
- consolidation 每次 run **先清 pending 佇列**再處理新主張——把「隔天回來補」
  從承諾變成機制。這是 O0 的核心修正。

### O0.3 存量回填（一次性腳本）
- `scripts/backfill_cortex_edges.py`：對現有 65 主張全量重跑補邊（低門檻），
  shim 模式不開 ChromaDB（沿用 [[LingLing 產出稽核 track]] 那支 backfill 的
  安全做法：ef 直連 Ollama、cache 關閉），LLM 裁決可分批跑或 `--dry-run` 先估量。
- 估算成本：65 主張 × top-K(擴大後~5) 鄰居，去重後約 150–250 次裁決 LLM call，
  一次性。跑前 `--dry-run` 報實際配對數。

### O0 驗收
- `related`/`contradictions` 非空的主張比例從 ~6% 升到目標 >60%。
- 重跑稽核的 grounding 集中度：top-4 cortex id 佔比明顯下降。
- 圖的連通分量數大幅減少（附一個 `--report` 印連通分量統計）。

---

## 段二：SE — 語義熵監測（便宜、純讀、零風險，當 O0/輪替的效果尺）

**目標**：把「同質化」從一個孤點（novelty 0.143）變成可追蹤的趨勢訊號，
並區分「生成問題」與「閱讀食譜問題」。

### SE.1 指標定義（首選：有效維度，數學乾淨、免調 k）
- 對一群 embedding 算共變異數矩陣特徵值的 **participation ratio**
  `(Σλ)² / Σλ²`——反映知識實際攤在幾個維度上。塌縮 = 同質化。
- 次選叢集熵（群大小分佈 Shannon 熵）列為 detail 佐證，不當主指標（要調 k）。

### SE.2 對三個母體各算一次，重點是比值
| 母體 | 意義 | 來源 |
|---|---|---|
| insights | 產出多樣性 | novelty sidecar 的 embedding（現成） |
| Cortex 主張 | 信念多樣性（回音室偵測） | consolidation 已快取的 page embedding |
| 來源文件 | 閱讀食譜寬度（基準線） | ChromaDB Synthesis 向量 |

- **關鍵訊號 = 產出熵 ÷ 輸入熵**：輸入寬但產出塌 → 生成問題（調引擎/輪替）；
  輸入本身窄 → 閱讀食譜問題（靠 research 拓寬）。這個比值是 LingLing 特有的可行動訊號。

### SE.3 接入點：self_assessment 第 7 軸
- 新增 `_axis_semantic_entropy(...)`，塞進 `run_self_assessment` 的 `axes` 清單
  （`self_assessment.py:359`）。純讀、零 LLM、夜間跑，完美符合現有契約。
- 時序：沿用既有 `_persist_and_trend`，熵值連續下降 N 次 → 亮 YELLOW + 觀察條目。
- **接自我改善弧線**：熵下降的觀察條目是天然的 M2 診斷種子，可餵 M3 提案
  （「加大 seed sampler 的 ε」「加長 operation 輪替」），但仍走閘門、不自動執行。

### SE 驗收
- 自評報告出現第 7 軸，對 insights/cortex/sources 三母體各報有效維度 + 比值。
- 用它回測：上週 operation 輪替（[[Insight 反同質化 track]]）上線前後的產出熵有無上升。
- 人工塞一批刻意雷同的假 insight → 熵指標應下降並亮燈（單元測試可造這個 fixture）。

---

## 段三：O1–O4 — 本體論學習層（最重，前兩段結果決定要做到哪階）

**設計判斷**：不另建平行結構，**擴展 Cortex 慣例**。但認清節點型別不同：
Cortex 節點是「命題主張」，本體論要的是「概念實體」節點（HBM、蒙地卡羅法）
+ 概念間型別關係（is-a / part-of / instance-of / uses / causes）。
→ 概念層與 Cortex 並存，共用同一套 store / embedding / 裁決基礎設施。

- **O1 概念抽取**：ingest 到 Synthesis 時順手抽核心實體，**複用 learning_artifacts
  那次概念圖 LLM call**（不加成本）。
- **O2 實體消解**：新概念用 bge-m3 對既有本體去重（"HBM" ≡ "High Bandwidth
  Memory"）——複用 facet/embedding。
- **O3 關係定型**：複用 `_adjudicate` 模式（LLM 判型別 + cache + 配額 + O0 的
  pending 佇列），關係詞表換成本體型別。
- **O4 衰減與自我修正**：概念邊套 Cortex 的 `S` 衰減，久未佐證的關係淡出——
  讓它是「持續學習」而非「一次性建圖」。

### O1–O4 驗收
- 跨文件查詢「哪些概念是 X 的下位/組成」能回出真實子圖。
- 本體論支撐 seed sampler：可依「概念距離最遠」選配對（餵回反同質化）。
- 自動抽出的關係全進 `_pending` 審核佇列（沿用 [[Profile system decisions]] 的
  quality-over-immediacy），人工核可才生效。

---

## 建議執行順序與理由

1. **O0 先做**——同時治斷圖、grounding 集中、seed 多樣性；做完會改變對 O1–O4
   需求的判斷（可能一半需求自動滿足）。
2. **SE 次之**——便宜零風險，且提供客觀數字驗證 O0 與輪替到底有沒有效
   （現在只有 novelty 一個孤點，無趨勢）。SE 也是這類問題的早期預警：
   insight→cortex→ground 下一篇 insight 的迴路熵塌縮是漸進的，時序熵會在
   novelty 掉到 0.143 之前就報警。
3. **O1–O4 最後**——最重，且前兩步結果會告訴你需要做到哪一階。

## 開放問題（需 Steven 拍板）
> [!IMPORTANT]
> 1. **O0.1 的 `cortex_link_threshold` 預設值**：0.55 較激進（圖更密、裁決成本更高）
>    vs 0.60 較保守。建議先 0.60 跑一輪看連通分量，再決定要不要降。
> 2. **O3 關係詞表**：要不要沿用學術本體標準（SKOS/OWL 的 broader/narrower/related），
>    還是用貼近你知識庫的自訂詞表（is-a/uses/causes/contradicts）？後者較實用。
> 3. **O1–O4 值不值得做**：等 O0+SE 跑完再回來評估——很可能修好的 Cortex 圖
>    已經覆蓋你 80% 的本體論需求，O1–O4 只在你明確需要「概念層 vs 主張層」
>    分離時才啟動。
