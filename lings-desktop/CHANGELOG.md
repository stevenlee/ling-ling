# Changelog

Ling-Ling 的逐項變更紀錄（新到舊）。架構層面的概覽見 [README.md](README.md) 的「架構演進」一節。

### 2026-06-14 Metacognition 層 · M4 數值自調（閉合自動改善弧線,唯一無人工閘）

整條「自動評估 → 自動改善」弧線閉合:M1 感覺 → M2 診斷 → M3 提案(人工閘)→ **M4 數值自調**。M4 是唯一不經人工閘的一相,所以護欄最厚。

- **`maintenance/autotune.py` + `services/autotune_store.py`**：把 Cortex 衰減校準那條單一自調迴路,推廣成**有護欄的通用數值自調器**。`Tunable` registry,每旋鈕綁一個 outcome metric。護欄:min-sample gate(無資料不動)、interval gate(不過頻)、damped ±20% step、hard bounds、**auto-rollback**(上次「調升」後指標進危險區→回退凍結;比衰減校準多出來的安全機制)。
- **不碰 config**：自調值寫進自有的 `autotune_state.json`;消費端走 `autotune_store.get_tuned(name, default)`,**只在 `AUTOTUNE_ENABLED` 開時**回覆寫值——關掉總開關即乾淨還原 config 預設,呼叫端零改動。
- **v1 綁定**：`CORTEX_GROUND_FRACTION` ↔ echo canary 的 novelty gap(cold − grounded)。gap 高(同溫層風險)→調降;gap ≤0(grounded 反而更新穎)→調升,bounded `[0.3, 0.85]`(永遠留 cold 對照組)。**這把 F1 的 canary 從「只告警」升級成「會動手」**。消費端 `insight_agent._should_ground` 改讀 `get_tuned`。
- 排程 `autotune_weekly`(dreaming window、idle)。Flag `AUTOTUNE_ENABLED`(預設 **false**)。
- **Live**：canary 目前 insufficient(F1 剛開、grounded 樣本未滿 5)→ 自調器正確回報「樣本不足,不調整」、不寫檔;AUTOTUNE 關閉時洞察路徑用 config 預設、零影響。框架由單元測試證明(升/降/維持/邊界/冷卻/回退)。+10 tests(1017 passed)。計劃見 [DesignDoc/SelfImprovement_metacognition_plan.md](System_Engine/DesignDoc/SelfImprovement_metacognition_plan.md)。

### 2026-06-14 Metacognition 層 · M3 生成改用結構化 find/replace 編輯（解決全檔改寫離題）

承上:M3 第一版「整檔改寫」在本機 gemma4:26b 會離題（複述 meta 指令、膨脹檔案,守門丟棄→產出率低）。**改為結構化分段編輯後解決**。

- **`maintenance/self_improve.py`**：改寫步驟改成要 LLM 回**一組 find/replace 編輯**（`_complete_json`,非整檔改寫）。每個 `find` 須在現行檔**逐字存在**才套用,對不上的自動丟棄（部分正確的回應仍取其可用編輯）;我們**確定性重建**修訂全文,其餘內容逐字保留。size 守門保留為 backstop(擋巨量 replace)。提案存下套用的編輯清單,`@ling-improve show` 會列出 find→replace + diff。
- **Live（真實 gemma4:26b）**：對 `agent_counter.md` 產出**兩個乾淨的針對性編輯**——加入 Negative Constraints + Self-Correction 步驟、收緊 schema 的 reasoning 須連結 quote——正是 M2 診斷所要,diff 最小、其餘逐字保留。全檔改寫的離題問題消失。+13 tests(1007 passed)。

### 2026-06-14 Metacognition 層 · M3 提案（自我改善,人工閘 + `@ling-improve`）

整條弧線第一次「改自己」,但全程人在迴路、永不靜默套用。

- **`services/improvement_store.py`**：修訂提案佇列(mirror Profiles `_pending`)。每份提案是一個 JSON(target_path、rationale、addressed_fixes、original_sha、original/revised content)。`approve` 三道守門:目標須在允許資產目錄內(`Templates/`、`Personas/`、`Guidelines/`,**不碰程式碼**;含 path-escape 防護)、目標檔自提案後**未被改動**才寫(不蓋使用者編輯)、原檔備份到 `_applied/`(一鍵回退)。
- **`maintenance/self_improve.py`（M3）**：把診斷出的「報告品質」問題映射到產生它的 prompt 檔(`lens_report`→`agent_counter.md` 等),載入現行全文 → LLM 改寫 → 存提案。**結構守門**:改寫須是針對性最小編輯(size 0.5×–2.5×、原結構保留 ≥35%),否則丟棄。其餘軸誠實標「需人工」。
- **`@ling-improve` 指令**(`agents/improve_agent.py`)：`generate`(跑 M1→M2→M3 產生提案)／`list`／`show <id>`(看 diff)／`approve <id>`／`reject <id>`。toranomaki 範例已附。
- Flag `SELF_IMPROVE_ENABLED`(預設 **false**);週任務在 `SELF_DIAGNOSIS_ENABLED`+`SELF_IMPROVE_ENABLED` 皆開時,接在 M2 後自動產生提案(仍不自動套用)。
- **Live 發現(重要)**：本機 gemma4:26b 在「整檔 prompt 改寫」這種巢狀提示上會**離題複述 meta 指令**(35 行→355 行)。安全設計如預期擋下——提案非自動套用,且結構守門把離題改寫**直接丟棄**(實測回報「暴增 >2.5×,跳過」),佇列不收垃圾。代價:此後端下 M3 常「不產生提案」而非產爛提案——**這是對的失敗方向**。瓶頸是本機模型對忠實全檔改寫的能力,非架構;未來改結構化/分段編輯或換更強改寫模型。+24 tests(1007 passed)。計劃見 [DesignDoc/SelfImprovement_metacognition_plan.md](System_Engine/DesignDoc/SelfImprovement_metacognition_plan.md)。

### 2026-06-14 Metacognition 層 · M2 診斷 + 趨勢分析

M1 之上接出診斷層,並讓自評有歷史記憶。

- **趨勢（M1 增強）**：`self_assessment` 每跑一次就把計分卡快照 append 到 `self_assessment_history.json`（capped `SELF_ASSESSMENT_HISTORY_MAX`,預設 180）。計算每軸相對前一次的趨勢箭頭（↑改善／↓惡化／→持平／•新）與 streak（連續同燈次數）。**慢性軸**（紅/黃連續 ≥3 次）會自動加一條「優先處理」觀察。計分卡報告新增「趨勢」欄。
- **`maintenance/self_diagnosis.py`（M2）**：`run_self_diagnosis(llm, assessment)`。對每個紅/黃軸蒐集 deterministic 上下文（最差報告型別、教條/薄證據主張原文、檢索趨勢…）→ 跑**一次精簡 LLM 呼叫**（`_complete_json`,非 answer_query 樣板）→ 結構化 `{root_cause, candidate_fixes[], confidence, needs}`。趨勢感知（慢性 vs 新）、**逐軸 fail-open**。報告明確框成「候選改善,尚未套用,需人工審核」——診斷與改動分離是反漂移核心條款。
- Flag `SELF_DIAGNOSIS_ENABLED`（預設 **false**,LLM-costed）。`self_assessment_weekly` 在 flag 開且有紅/黃軸時,接在 M1 後跑 M2。
- **Live（真實 LLM + 資料）**：M2 診斷品質很高且扣著架構——正確隔離 `lens_report`（50% 失敗,vs synthesis 0%）、獨立指出 embedder 語義天花板 + facet_lift=0（檢索停在 73%）、Cortex 缺證據多樣性與可證偽門檻並提出「promote 為主張前需 ≥2 來源」。+18 tests（994 passed）。
- 已知限制:M2 v1 尚未載入目標元件 prompt/template 原文,故偶與既有機制重複建議;載入原文 + 產 diff 是 M3。計劃見 [DesignDoc/SelfImprovement_metacognition_plan.md](System_Engine/DesignDoc/SelfImprovement_metacognition_plan.md)。

### 2026-06-14 Metacognition 層 · M1：統一自評器（自動評估 → 自動改善的感覺層）

往「系統能自動評估、自動改善」方向開工。盤點發現系統其實已有 6 條窄自調迴路（衰減校準、帳本嚴格度、bench 自長/回歸告警、同溫層金絲雀、證偽），但**訊號散落六個子系統、沒有統一視圖**,且**沒有任何迴路會根據累積 verdict 改善生成品質（prompt/template）**。遵循專案 Nervous-System-First 原則——自動「評估」是感覺層,必須先於自動「改善」（行動層）——先做評估器。

- **`maintenance/self_assessment.py`**：`run_self_assessment(trace_store, ...)`,**純讀、零 LLM**,把六軸訊號聚合成一份健康計分卡 + deterministic 觀察條目。六軸:報告品質（verdict 分佈）、LLM 健康（錯誤率/token）、檢索（bench pass_rate 趨勢）、Cortex 信念（tensions：矛盾/教條/薄證據/證偽）、記憶衰減（校準狀態）、洞察品質（novelty/groundedness/refute 存活、grounded vs cold）。每軸一個狀態燈（🔴🟡🟢⚪）由閾值規則決定;**逐軸 fail-open**（單一來源壞掉只降該軸為 unknown,不拖垮整份）。安靜週保持安靜:只有出現紅/黃燈或觀察條目才寫完整報告到 `fromLingLing/`,一行摘要恆進 `maintenance.log.md`。仿既有 `routing_report` 形狀。
- **唯讀聚合 helper**（`services/trace_store.py`）：新增 `query_all_artifacts(since_days)`（跨所有 type,免列舉脆弱字串）與 `llm_call_health(since_days)`（SQL GROUP BY 算各 stage 錯誤率/token）。皆新增,未動既有方法。
- 排程 `self_assessment_weekly`（週、idle、dreaming window;受 `MAINTENANCE_SCHEDULER_ENABLED` 總開關管,唯讀報告無需個別 flag）。
- **Live（真實 DB+Cortex+Insights）**：計分卡如實反映現況——檢索 🔴（pass_rate 73% < 80% 警戒線）、Cortex 🔴（2 條教條 + 12 條薄證據）、LLM 🟢（903 次呼叫 1% 失敗）、洞察 🟢（13 篇全 cold,呼應 F1 剛開尚無 grounded）。觀察條目即 M2 診斷的種子。+11 tests（986 passed）。
- **反漂移硬條款**（寫進計劃）：評估與改善分離;改是提案非靜默套用（M3 走 `_pending` 人工閘）;數值自調須阻尼+對照+回退（M4,沿用衰減校準模式）;不可用單一指標自我獎勵（多訊號交叉,防 Goodhart）。相位 M1→M4 與完整設計見 [DesignDoc/SelfImprovement_metacognition_plan.md](System_Engine/DesignDoc/SelfImprovement_metacognition_plan.md)。

### 2026-06-14 Cortex Phase 5 · F1 開啟協定啟動（grounded insight flag ON，進入觀察期）

五道防禦齊備後,依計劃啟動開啟協定:`CORTEX_GROUNDED_INSIGHT_ENABLED=true`(寫在 `.env`——這是**安全閘**,不是口味,屬部署期決定且需重啟生效,與 Phase 6 口味開關歸在 Scripture 不同)。本次只翻 flag + live 驗證,未改程式邏輯。

- **Live 驗證(真實 ollama + Cortex)**：(1) flag 載入為 True;(2) `_should_ground` 跨 500 idea 切出 72%(目標 70%),**cold 對照組 142 個確實保留**給金絲雀;(3) `_cortex_priors` 對「AI Agent 協作系統如何對抗熵增」這個 idea,經 recall 排序回傳 3 條最相關 prior(首條正是對應的「收斂機制」主張),`_grounding_block` 正確以「**請挑戰,不要附和**」辯證框架呈現,並附上 falsifier 當反例——反同溫層注入如設計運作。
- **基線 canary**：on-demand 跑 `run_echo_canary` → `insufficient`（grounded 0、cold 13;既有洞察都在開 flag 前產生）。執行無誤,正確回報樣本不足、暫不評斷。
- **觀察期**：`echo_canary_weekly` 已排程自動跑。協定要求累積 ≥5 條 grounded 洞察後,canary 才能判斷;以每晚 `insight_daily` × 0.7 grounding 比例,約需數晚。**無同溫層告警（grounded 組 novelty+falsifiability 未系統性偏低）才維持開啟**;若告警則回退關閉。計劃見 [DesignDoc/CortexPhase5_F1_grounded_insight_plan.md](System_Engine/DesignDoc/CortexPhase5_F1_grounded_insight_plan.md)。
- ⚠️ flag 在 `.env`,running daemon **需重啟**才會生效。

### 2026-06-14 設定歸位：Phase 6 學習輔助開關從 `.env` 移到 Scripture.md（hot-reload）

- **檢討**：`.env` 是**環境/部署**層的設定（endpoint、API key、provider/model 選擇、會動到成本或安全的子系統 gate）。`visual_router`（自動附學習產物）與 `argument_map_mermaid`（論證圖多附一張 Mermaid）兩個是**使用者輸出偏好**——純口味、無安全/架構含意——卻被我先塞進 `.env`,等於逼使用者改完要重啟 daemon 才生效。歸位到 `Scripture/Scripture.md` 的 YAML frontmatter,走既有 `DynamicSettings.reload()`,**改完即時生效**,使用者也不必碰程式環境。
- 從 `core/config.py` 的 env 區移除這兩個常數,改為 `DynamicSettings` 綁定（`VISUAL_ROUTER_ENABLED` / `ARGUMENT_MAP_MERMAID`,預設 False);呼叫端 `learning_artifacts.py` 改讀 `settings.*`（live 讀取,故熱更新有效）。Scripture.md 新增「🖼️ Learning Aids」說明段。+1 test（hot-reload 綁定回歸）。
- 原則釐清：config.py 既有註解「feature flag 一律 env」其實混了兩種東西——(a) 架構/安全 gate(該留 env,如 `CORTEX_GROUNDED_INSIGHT_ENABLED` 是必須關到防禦齊備的安全閘)與 (b) 使用者輸出偏好(該進 Scripture)。本次只搬 (b) 類的兩個,其餘 env gate 維持不動。

### 2026-06-14 Phase 6 學習輔助軸 · 收尾（auto-attach 全覆蓋 + per-type linter + 論證圖 Mermaid 選配）

把 Phase 6 三條軸的後續一次補齊（皆 flag-gated,預設行為不變）：

- **(A) Insight 自動附學習產物**：先前只有 synthesis 會 auto-attach,insight 報告沒有。`agents/insight_agent.py` 新增 `_maybe_artifact(content)`,在 `generate_insight`（單一/montecarlo）與 `generate_full_insight`（用跨維度綜合段）寫檔前附上「## 🖼️ 學習輔助」。共用 `maybe_artifact_section`，故 `VISUAL_ROUTER_ENABLED` OFF（預設）→ 零 LLM 呼叫、報告 byte-identical;fail-open（產圖失敗絕不擋洞察）。
- **(B) Mermaid per-type linter 強化**：live 觀察到 mindmap 語法常被模型寫鬆。`_render_mermaid` 現在注入 `_MERMAID_HINTS`（mindmap 用縮排非箭頭、timeline/quadrant/concept_map/flowchart 各自的語法提示）,並新增 `_validate_mermaid(block, kind)`——檢查產出區塊首行確實宣告了**要求的圖種**（抓「要 mindmap 卻回 flowchart」）且有 ≥1 行內容,不符就降級為不輸出（不放壞/錯種的圖）。
- **(C) 論證圖 Mermaid 選配版**：`render_argument_map(data, with_mermaid=False)` 可附一張**確定性**（非 LLM,不會漂移/幻覺）的 Toulmin graph——根據實線、隱含前提/反駁虛線並標籤,label 經 sanitize。Flag `ARGUMENT_MAP_MERMAID`（預設 OFF）;Markdown 仍是主輸出。
- 新增 `@ling-visualize` 的 toranomaki 使用範例（先前唯一缺的）。+13 tests（974 passed, 1 skipped）。

### 2026-06-14 Cortex Phase 5 · F2：記憶的「讀」side（`@ling-recall`）

- **關閉記憶迴路的第一步**：Cortex 長期記憶（Phase 1–4）一直只寫不讀——生長/衰減/falsify/驗證，但生成端從不主動讀。F2 加上讀側原語 `services/cortex_recall.py` 的 `recall_claims(rag, query, top_k)`：embed query + 每條 claim（走 RAG 既有 embedding cache，未變動的 claim 是 cache hit），cosine 排序，回傳結構化 CortexPage（連同 confidence/falsifiability/falsifier/contradictions/evidence，而非 RAG 文字 chunk）。
- **`@ling-recall` 指令**（`agents/recall_agent.py`）：給主題，撈最相關的蒸餾主張並渲染**連同其知識論**——刻意把反例與矛盾一起攤開（反同溫層透明度），不是只給結論。與 `@ling` 問答區分：問答從原始筆記答，recall 從蒸餾信念答。
- 這個 `recall_claims` 原語是 F1（Cortex-grounded insight）與 F3（張力摘要）共用的地基。F1 計劃見 [DesignDoc/CortexPhase5_F1_grounded_insight_plan.md](System_Engine/DesignDoc/CortexPhase5_F1_grounded_insight_plan.md)，其中把 provenance 防火牆與同溫層金絲雀列為硬條款。
- Flag `CORTEX_RECALL_TOP_K`（預設 8）。Live：真實 vault 查「用什麼文獻當蒙特卡羅種子」→ 對應主張 0.865 排第一。+7 tests。
- **改用 LLM-over-full-Cortex（第二輪 live 回饋後的架構修正）**：連 hybrid 都救不了「關於 Hibert 的所有記憶」——一字 typo（Hibert vs Hilbert）打爆 exact-token BM25，embedder 又弱到接不起，字面命中主張排最後。**根本問題是工具選錯**：語料只有 9 條，這種規模不該用「檢索」——直接把整個 Cortex 塞進 LLM context，讓它讀完選 + 綜述，typo／概念匹配／框架詞全部自然處理。recall 現在預設 LLM-over-corpus（`CORTEX_RECALL_LLM_MAX=150` 內全塞；超過才用 hybrid `recall_claims` 預篩 `CORTEX_RECALL_PREFILTER`）。Live（typo query）：正確對應 Hilbert、只引用相關的 [#4]、附自我批判（信心 0.50 + 反例）。**同時修一個 bug**：原用 `answer_query` 會經 `_build_system_prompt` 注入 Visualization/template 樣板，害模型狂追一個沒人要的 Mermaid 圖並吐出整段 chain-of-thought（16682 字）；改用新的精簡 `LLMClient.complete(system_prompt, user_msg)`（不帶 persona/template/viz 機制）+ 「只輸出最終綜述」指令後，輸出降為 836 字、乾淨。
- **Hybrid 融合（第一輪 live 回饋後，保留為大語料預篩）**：純向量在真實對話式查詢上回傳平帶 grab-bag——embedder（nomic-embed-text）對同語言文字的 cosine 擠在 0.53–0.69，連把 on-topic 主張排到 off-topic 之前都做不到（這是 R6 的同一個 embedder 天花板，memory loop 繼承了它）。recall 改為 **magnitude-aware hybrid**（cosine + BM25 字元級 CJK token，BM25 以自身 max 正規化後加權融合）。**刻意不用 RAG 層的 RRF**：RRF 是 rank-based、丟棄 BM25 的量級，且其 k=60 阻尼在 Cortex 這種小語料（~數十條）會把「字面命中得分 4× 次名」的尖峰訊號壓成「rank1 僅微幅勝 rank2」，被 embedder 平帶蓋過。實測：「構建知識圖譜的過程」查詢，字面命中主張從純向量的 **rank 9 → hybrid rank 1**。注意：BM25 在極小語料（≤2–3 頁）IDF 退化為 0；hybrid 的效益隨 Cortex 長大才完整。純概念、無詞彙重疊的查詢仍受 embedder 天花板限制（R6）。+1 test。

### 2026-06-14 Phase 6 學習輔助軸 · ③：論證圖（Toulmin,批判性思考）

router 的旗艦 type:把內容攤成論證骨架——主張 → 根據 → **隱含前提(warrant)** → 適用條件 → 反駁。

- **`services/argument_map.py`**:`build_argument_map`（`_complete_json` 抽 Toulmin 結構）+ `render_argument_map`（結構化 Markdown,先不畫 Mermaid——穩健優先）。value-add 是**未明說的隱含前提**:連結根據到主張、作者沒講出來的假設,正是「找出隱藏邏輯」;並標出「最弱的一環」(最可爭議的 warrant)。重用既有 claim/evidence/falsifier 抽取的同源思路。
- router `argument_map` type 不再是 stub,dispatch 到此模組;`@ling-visualize [[X]] as argument_map` 可用。
- Live（鮑莫爾成本病）:正確抽出主張、明說根據,並挖出三條**未明說的隱含前提**（勞動力跨部門流動逼漲薪資、服務業生產率瓶頸、全社會薪資基準上推),最弱一環=勞動力自由流動且「必須」跟漲的假設。+6 tests。

### 2026-06-14 Phase 6 學習輔助軸 · ①：學習產物 Router（`@ling-visualize`）

Ling-Ling 的使命是幫人**學**,但視覺輸出一直只有 Mermaid flowchart。重新框定:缺口不是圖種（Mermaid 本就支援約 10 種）,是「**內容認知結構 → 對的學習產物**」的對應。

- **`services/learning_artifacts.py`**:classify（用 `_complete_json`,不走 answer_query 樣板）→ render（per-type）→ validate（既有 markdown quality checker 修 Mermaid）。type 選單:`comparison_table`（Markdown 表）/ `flowchart` / `mindmap` / `timeline` / `quadrant` / `concept_map`（皆 Mermaid）/ `argument_map`（保留給 ③）/ **`none`**（沒有強結構就不硬畫,一等公民）。
- **`@ling-visualize [[筆記]]`** 指令（on-demand）:解析筆記 → 選型 → 產出;`as <type>` 可強制類型。
- 穩健性:壞 Mermaid 過不了 linter 就降級為「驗證失敗,不輸出壞圖」;classify/render 全走精簡 `complete`/`_complete_json`,避免 Visualization/template 樣板污染（recall 的教訓）。
- Live（真實 vault）:鮑莫爾成本病 synthesis → 自動選 **flowchart**（因果機制）並產出正確的因果圖;`as mindmap` → 正確產出階層心智圖。兩者都通過 linter。+10 tests。
- **Auto-attach（同批,flag-gated）**：`VISUAL_ROUTER_ENABLED=true` 時,長文 synthesis 的 Executive Summary 後自動附一段「## 🖼️ 學習輔助」（`maybe_artifact_section`,走 router 選型）。flag OFF（預設）→ 回空字串且**零 LLM 呼叫**,synthesis 輸出 byte-identical。on-demand `@ling-visualize` 不受此 flag 限制。計劃見 [DesignDoc/LearningArtifacts_plan.md](System_Engine/DesignDoc/LearningArtifacts_plan.md)。

### 2026-06-14 Cortex Phase 5 · F1：Cortex-grounded insight（五防禦俱全，flag OFF）

關閉記憶迴路的核心：讓累積的 Cortex 信念**參與**洞察生成,但**不製造自我印證/同溫層**。`CORTEX_GROUNDED_INSIGHT_ENABLED` 預設 **OFF**;五條反同溫層防禦全部落地後才安全開啟。

- **注入側**：`_expand_seed` 注入相關的 Cortex 主張當**辯證式**先驗（「請挑戰、找張力與反例,不要附和」,防禦②）,且只取 `falsifiability >= CORTEX_GROUND_MIN_FALSIFIABILITY` 的主張當錨（不可反駁的＝同溫層燃料,排除,防禦③）。只 ground `CORTEX_GROUND_FRACTION` 比例的 seed,其餘保持 cold 當 canary 控制組。洞察 frontmatter 記 `grounded_on=[claim_id...]` provenance。
- **Provenance 防火牆（防禦①,堵兩條循環）**：強化路徑——`cortex_consolidation._merge_into` 若 grounded 洞察附和自己的先驗,記 evidence 但**跳過 S/confidence 強化**（只有外部證據能升信心,防禦④）;falsification 路徑——`cortex_ledger` 的「≥2 獨立來源」計數**排除被 prompt 來挑戰該主張的洞察**,避免辯證 framing 製造反例殺自己的先驗。
- **同溫層金絲雀（防禦⑤）**：`maintenance/echo_canary.py` 比較 grounded vs cold 洞察的 novelty/groundedness,grounded novelty 系統性偏低 → alarm + 建議關閉。
- flags：`CORTEX_GROUNDED_INSIGHT_ENABLED`(False)/`_MIN_FALSIFIABILITY`(0.5)/`_TOP_K`(3)/`_FRACTION`(0.7)。+13 tests（注入/閘/firewall 兩路/canary）。計劃與 enable protocol 見 [DesignDoc/CortexPhase5_F1_grounded_insight_plan.md](System_Engine/DesignDoc/CortexPhase5_F1_grounded_insight_plan.md)。

### 2026-06-14 Cortex Phase 5 · F3：知識張力掃描（`@ling-tensions`）

- **記憶的反面**：recall 答「我相信什麼」，tensions 答「我的信念在哪裡有張力」——對抗自我印證的解藥（把異議攤開）。純掃描，無 LLM、無 embedding，所以不受檢索品質影響。四個 bucket：**矛盾對**（ledger 標記的衝突，A↔B 去重、id 解析成主張全文）、**教條**（高信心 ≥0.5 但低可反駁性 ≤0.25——「不可能錯」的主張只會自我強化，同溫層的結構性燃料）、**證據單薄**（≤1 來源）、**已被推翻**（status falsified，附死因 counterpoints，透明保留）。
- `services/cortex_tensions.py` 的 `scan_tensions(cortex_dir) -> TensionReport`（fail-open）+ `@ling-tensions` agent 渲染。Flag：`CORTEX_TENSION_DOGMATIC_FALS`(0.25) / `_DOGMATIC_CONF`(0.5) / `_THIN_EVIDENCE_MAX`(1)。
- Live（真實 12 頁 Cortex）：正確標出 2 條 falsifiability=0.0 的教條主張（「純粹顯現」「知識圖譜」那種規範性/不可反駁陳述）——正是該人工複審的同溫層風險。+7 tests。

### 2026-06-13 全模組稽核與硬化（audit R7）

對 System_Engine（~22k LOC）跑了一次多代理程式碼稽核（99 raw → 41 confirmed），逐項親手驗證後修正，不成立的記為「查證後不做」。完整脈絡見 [System_Engine/DesignDoc/SystemEngine_audit_20260613.md](System_Engine/DesignDoc/SystemEngine_audit_20260613.md) 與 [Roadmap R7 區](System_Engine/DesignDoc/Roadmap_Phase4.5_onwards.md)。

- **資料完整性（batch-A）**：`is_empty_json_literal` 取代子字串判斷（B1，修自己的迴歸）；lens RAG fallback 餵原文而非格式化 markdown（B2）；`add_document` legacy 清理改 scoped、只刪無 doc_id 的同名 chunk（A2）；短文件 `(Synthesis)` 命名確認為慣例、文件化（A1）。
- **Reasoning-channel 防禦統一（R4）**：`_complete_json` helper，6 個 JSON 呼叫端統一「解析失敗 re-roll 一次」。
- **獨立 correctness（R7-E）**：profile_manager 大小寫 key、maintenance_scheduler busy-lock 搶占、trace_store finally 遮蔽原例外、parser 空 mermaid label 被丟、frontmatter 無尾換行漏判。
- **效能（R7-F）**：trace_store 補時間窗索引（SCAN→SEARCH）。
- **ChromaDB／架構（R7-C/C2）**：facet deref 批次化（N get→1 `$in`）、`format_digest_for_prompt` 公開化、insight 8 處 `rag.collection` 直存收進 `all_chunks`/`chunks_by_title`。
- **並發（R7-G/G2）**：prompt_watcher 與 clipping_watcher 的處理移出 watchdog dispatch thread、改專屬 worker。
- **純清理（R7-D）**：insight signals／pair-key helper 抽取、targeted-pair fallback 尊重 exclude。
- **查證後不做**：R6 檢索漂移（ROI 不成立——監控指標非使用者問題）、R7-B LLM 並行（實測單 ollama 全串行、只 1.07×）、N+1 embedding（誤報，已快取）、load_all_pages 快取（~9 頁過早）。

### 2026-06-13 全模組稽核後續（batch-A 資料完整性）

- **B1（迴歸修正）**：`is_empty_json_literal(text, kind)` 取代「子字串含 `[]`/`{}`」的判斷——只有整段（去空白去 code fence 後）等於 `[]`/`{}` 才算真零、不 re-roll。修掉 `_complete_json`(R4) 與 lens extraction(batch-3) 會把 `{"items":[]}` 誤判成空集合、遮蔽 parse 失敗的問題。
- **B2**：LingLens 的 RAG fallback 改用 `query_notes` dict API，餵原始 chunk 文字 + metadata 真標題，不再把 `query_similar_notes` 的格式化 markdown（`### [來自筆記…]`）當原文污染 grounding。
- **A2**：`add_document` 的 legacy title 清理改為 scoped——只刪「同名 AND 無 doc_id」的舊 chunk（`_delete_legacy_title_chunks`），不再無差別 delete-by-title 而誤刪同名的不同文件。
- **A1（慣例，刻意不改）**：短文件的單一頁面命名為 `{stem} (Synthesis)` 是**正式慣例**，不是 bug。load_sources（`builtin_adapters`）以 `{title} (Synthesis).md` 找頁且無 `{title}.md` fallback、ReadingIndex（`vault_utils`）以此名連結、使用者 vault 可能有 `[[X (Synthesis)]]` wikilink。改名的 churn 與破壞性遠大於「名稱看起來怪」的收益，故保留並於 `ingestion_pipeline` 標注。

### 2026-06-13 R4：Reasoning-channel JSON 防禦統一

- **`LLMClient._complete_json` 新 helper**：統一「呼叫 → 解析 JSON → 解析失敗 re-roll 一次」的防禦。reasoning 模型（gemma via Ollama）偶爾把整段回覆塞進 reasoning channel、content 無可解析 JSON 又不報錯（就是 batch-3 默默把 LingLens extraction 歸零的同一個失敗）。literal `[]`/`{}` 視為「真的空答案」不 re-roll，避免把合法零當成解析失敗。
- 盤點後 6 個原本只解析一次、失敗即 fail-open 的呼叫改走它：`generate_part_digest`、`find_topic_shifts`、`summarize_for_context`、`extract_claims`、`adjudicate_claims`、`generate_persona_and_template`。
- 刻意保留 bespoke：`score_text_quality`（`reason` 需區分 transport error 與 parse miss，但仍補了 parse-miss re-roll）、`_assess_falsifiability_once`（re-roll 條件更嚴）。已知缺口：`translate_tags` 走 provider dispatch 不經 `_complete_text`，未納入（已設 `response_format=json_object`，曝險較低）。

### 2026-06-12 Cortex Memory (Phase 3+4 - 衰減、行為訊號、主張帳本) ＋ 生成端配比

- **生成端配比**：夜間 insight 改為 doc-anchored——SeedSampler 以「近期被檢索命中」做興趣加權、ε=0.2 保證探索（最久未被抽中的文件輪流上場），確定性選種子餵給 targeted montecarlo；全庫漫談降為每週任務。對症下藥：過閘 insights 斷鏈率 80% 全來自 Vault 型漫談。
- **Phase 3 雙強度衰減**（[services/cortex_decay.py](System_Engine/services/cortex_decay.py)）：S 只增不減、R = exp(−Δt·ln2/t½(S)) 現算不存；**spacing effect** ΔS = gain×(1−R)——同晚重複發現幾乎不增 S，快遺忘時被重新發現才大漲。狀態遲滯（降 0.5/0.2、升 0.6/0.3）防 facet index 震盪；dormant 移出 facets、復活自動歸隊。行為訊號：檢索命中（0.5/日）、使用者編輯（1.0，mtime 動了而 frontmatter `updated` 沒動＝只有人類這樣寫）。每晚再驗證 3 頁（fading 高 S 優先——睡眠優先重播重要記憶），失敗降 confidence。revival rate 阻尼校準（±20%、≥20 樣本、月節奏）。`decay_simulation.py` 回測：現有頁面太年輕無法區分網格，維持 21d/1.8 預設。
- **Phase 4 主張帳本**（[maintenance/cortex_ledger.py](System_Engine/maintenance/cortex_ledger.py)）：**保守擊殺**——≥2 個矛盾連結且追溯到**獨立** insights（單源圍攻不算）＋ LLM 反駁確認才轉 falsified（檔案保留、facets 移除、counterpoints 記死因；倖存者 14 天冷卻）。**un-merge 回饋**：快照偵測使用者拆頁，un-merge 率 ≥10% 自動進 strict mode（equivalent 裁決降級為連結），<5% 鬆綁。驗證報告新增「⚔️ 矛盾對」與「🪦 已 falsified」區。

### 2026-06-12 backlog batch-3：Lens 引文驗證

- **LingLens Quote Verification**: `_ground_tally_locations` 原本就會嘗試把每條引文定位回原文，但「定位失敗」這個負訊號被默默吞掉。現在報告尾端新增 `## 🔍 Quote Verification` 節：統計錨定比例、列出無法定位的引文（可能是改寫、翻譯或虛構，提示人工抽查）；metadata 寫入 `quotes_grounded`/`quotes_total` 與 `quality_verdict`（比例低於 `LENS_QUOTE_MIN_GROUNDED_RATIO`，預設 0.8 → `revise`），與 synthesis critique 共用同一套 verdict 詞彙，artifact 層可跨報告型態比較。停車場 D2 的 Insight 半邊由 Phase 2.5/3 的 groundedness/refute 訊號涵蓋，不另做。

### 2026-06-12 backlog batch-2

- **T1: Critique retry loop**: synthesis 的 critique postcheck 由「只記錄」升級為「行動」——verdict 為 revise/reject 時，帶著 critique 全文重生 synthesis（`SYNTHESIS_CRITIQUE_MAX_RETRIES` 控制，預設 1），重試結果 verdict 嚴格較好才採用。metadata 新增 `critique_attempts` 與（重試發生時）`quality_verdict_history`；`critique_feedback=None` 路徑的 prompt 與舊行為 byte-identical。
- **T2: 新 Operations 四件套**: `Templates/Operations/` 新增 `compare` / `classify` / `outline` / `explain` 四個 fixed-methodology 模板，CapabilityManager 自動拾取，零 Python 產品碼變動。
- **T3: select_profile 選單修復**: 選單原本只印 hint、從未給模型看 profile 名，模型答 hint 字樣被攔成 `none` → 路由靜默退化到 default profile（batch-1 live transcript 實測發現）。選單改為 `name: hint` 格式，並加一層「答案恰好包含一個合法名」的 salvage 解析。

### 2026-06-12 backlog batch-1

- **T1: Falsifiability 穩健化**: `assess_falsifiability` 改為支援多重取樣（由 `CORTEX_FALSIFY_SAMPLES` 控制）。當 `samples=1` 時維持 `byte-identical` 原生行為；偶數樣本時取中位數，並在 Falsifier 挑選時取最接近中位數者，避免產生不符實際推論的文本。
- **T2: 每週記事 (Weekly Memoir)**: 新增 `weekly_memoir` 工具並註冊至 `maintenance_scheduler`。匯總當週 `recent_query_texts` 歷史、Cortex 頁面動態與 Falsified 狀態更新。全模組採 fail-open 策略，確保個別資料源損毀時報告仍能產生（標註資料不可用），無資料的節點則自動省略。
- **T3: 結構化 LLM Token 上限解除**: `score_text_quality`, `find_topic_shifts`, `summarize_for_context` 等 P0 評分器，以及 `classify_document`, `select_profile` 等結構化路由呼叫的 `max_tokens` 上限由 `20/200` 等具體數字調整為 `None`。解決因 Reasoning 模型輸出過長而被中途截斷的問題，並透過實機測試（如 gemma 等模型）證明與既有的 `reasoning` / `reasoning_content` fallback 機制完全相容。

### 2026-06-11 Cortex Memory (Phase 2.5 - 可反駁性與抽取錨點)

- **第五訊號 falsifiability**：把 Popper 操作化——`assess_falsifiability` 要求 LLM「描述一個能推翻此主張的具體觀察」，寫不出＝低分（0/0.5/1 三檔，越界自動夾制）。建新 Cortex 頁時評一次（merge 路徑零額外成本），分數掛鉤初始 confidence（`0.3 + 0.4×score`）——不可反駁的主張以低信心進場而非被拒。緣起：首輪驗證發現「有道理＋無法反駁＋不知如何實施」的占星術組合，而模糊是對 refute 訊號的天然護甲。
- **抽取錨點 applies_when**：每條 claim 附「適用情境」，prompt 明文糾偏「原子 ≠ 無條件全稱」；頁面以 `> 適用情境：` blockquote 確定性編碼進 Core Claim 節（round-trip 有測試把關）。
- **證據鏈穿透**：claim 的 sources 除 insight frontmatter 外，追加解析 insight 內文的 `[[wikilink]]`（存在性過濾、上限 5）——「如何實施」的脈絡一跳可達。
- **量尺修缺**：斷鏈率只計過閘 insights（首輪 90% 是把被擋的 planner 文件算進去的假象，且閘門判定改為與鞏固管線共用同一函式）；新增 `refute_coverage` 與 falsifiability 分佈（mean < 0.4 黃線）；人工抽查清單每條附「證偽」提示。
- **回補基線**：`maintenance/backfill_falsifiability.py` 對既有頁面補測（只加測量、不改 confidence/S/時間戳——不破壞 reconsolidation 歷史）。

### 2026-06-11 Cortex Memory (Phase 2 - 夜間鞏固)

- **Insight → 原子主張 → Cortex 頁**：dreaming window 的每日任務把通過 Phase 1 訊號閘門的 insights（排除 refuted 與低 groundedness）蒸餾成原子主張（每份最多 3 條），鞏固進獨立的 `Cortex/` 目錄——**一頁一主張**，這是長期記憶的最高層。
- **只有雙向蘊涵才合併**：embedding 鄰居（≥0.80、top-3）交給 LLM 做六值裁決（equivalent / entails / entailed_by / complementary / contradicts / unrelated）；只有 `equivalent` 觸發合併（reconsolidation：證據鏈增長、S+1、confidence 上調 cap 0.9、變體區 cap 5），其餘建**型別化連結**；`contradicts` 雙方互記並下調 confidence（floor 0.1，不擊殺——falsified 是 Phase 4）。裁決結果內容定址快取、無 TTL。
- **頁面結構鎖死**：[services/cortex_store.py](System_Engine/services/cortex_store.py) 是唯一讀寫路徑——機器狀態（S / confidence / 證據鏈）在 frontmatter、內文固定四節，全部確定性程式碼操作，LLM 永不重寫整頁；`parse(render())` round-trip 有測試把關。
- **進檢索閉環**：Cortex 頁建立/更新即 `add_document` + facet；`Cortex/` 納入 vault watcher 與 orphan sweep——你刪頁面，chunks 自動清理。
- 配額：每晚 10 份 insight 抽取、20 次蘊涵裁決（`CORTEX_*` flags 可調）。

### 2026-06-10 Cortex Memory (Phase 1 - Insight Quality Signals)

- **四項品質雷達**：為所有 `@ling-insight` 產出加上 `groundedness` (引用存活率)、`novelty` (內容新穎度，Cosine Distance vs `insight_signals.json` 歷史)、`bridging` (跨領域融合度，Target embeddings 間的最大距離)，以及 `refute_verdict` (LLM Challenger 壓力測試)。
- **Fail-open 哲學**：所有計算皆包裹於 try-except，不干擾報告主流程；即使 LLM 失敗或 RAG 失聯，依然會寫入檔案（帶空訊號）。
- **Sidecar FIFO**：採用 atomic swap 更新 `Database/insight_signals.json`，並利用 UTC timestamp 滾動淘汰舊的 embedding 快取，將記憶體與磁碟佔用上限鎖定在 500 筆。

### 2026-06-10 Profile Routing（取代 DocType.md）

- **Profile 制**：`Scripture/Profiles/*.md` 每檔一個具名「persona + template」配對（frontmatter：`persona / template / operations / description / applicable_when`），路由器選 profile 而非分別選 persona/template，配對衝突從結構上消失。新的 [services/profile_manager.py](System_Engine/services/profile_manager.py) 負責掃描、`DocType.md` 一次性遷移與 `_pending/` 審核佇列。使用說明見 [Scripture/Profiles/_README.md](Scripture/Profiles/_README.md)。
- **三層解析**：文件 frontmatter 覆寫（`profile:` 或 `synthesis_persona`/`synthesis_template`）> 自動選擇（`document_type` 直接命中，否則 `LLMClient.select_profile` 在已註冊 profile 中做封閉式選擇）> `default` profile / Scripture 設定。封閉式選擇取代了舊的開放式分類＋查表，答案永遠可執行。
- **審核佇列**：無法分類的新類型會自動草擬 persona/template/profile 三件套進 `Scripture/Profiles/_pending/<類型>/` 並通知 `fromLingLing/`，審核搬檔後才生效；當次先用 `default` 處理（品質優先於即時性）。
- **統一資產契約**：Personas 與 Templates 改經 `_load_capability_body` 載入，frontmatter 一律剝除後才進 system prompt——之後可安全地替 persona/template 加上與 Operations 同款的 metadata。
- **併發加固**：vault 刪檔搶不到鎖改為排程重試（不再無鎖裸跑 RAG）；`DynamicSettings.reload()` 加鎖並改為先解析再原子套用；MaintenanceScheduler 狀態檔改 temp-file + rename 原子寫入；watcher 同步/處理失敗改為 `ui.error` 浮出。

### 2026-06-10 Routing Observability & Ops（Profile 制後續五件套）

- **路由決策追蹤**：每次 ingestion 寫一筆 `routing_decision` artifact（profile、解析層 `frontmatter_override/frontmatter_profile/llm_selection/default_profile/settings_fallback`、是否 fallback、是否送審）。TraceStore 新增 `query_artifacts()` / `query_llm_calls()` 分析介面。
- **路由健康報告**（[maintenance/routing_report.py](System_Engine/maintenance/routing_report.py)）：每週彙整 fallback 率、各層分佈、未被選用的 profile、待審草稿。摘要固定進 `maintenance.log.md`；有可行動項（fallback 超過 30%、有待審草稿、選擇器失敗）才寫完整報告到 `fromLingLing/`。
- **Template 版本化**：template frontmatter 可宣告 `version:`；生成頁面 frontmatter 蓋上 `template` / `template_version` 戳記。每週 [maintenance/template_audit.py](System_Engine/maintenance/template_audit.py) 稽核 `pages/`，列出用舊版 template 渲染的頁面（不自動重渲染，只追蹤）。
- **`@ling-profiles` 指令**（[agents/profiles_agent.py](System_Engine/agents/profiles_agent.py)）：`@ling-profiles` 總覽、`pending` 草稿明細、`approve <名稱>` 一鍵把審核通過的草稿搬入正式位置（拒絕覆寫既有檔案）。
- **Skill 前置條件強制檢查**：`Skills/*.md` 的 `applicable_when`（`database_populated` / `min_documents` / `has_tag_graph`）在執行前對照實際 vault 狀態驗證，不滿足就明確拒跑（例如知識庫只有 5 份文件時擋下 montecarlo）。RAG 失效時 fail-open，不會反過來癱瘓 insight。

### 2026-06-10 Facet Backfill Pump（閒置時低優先權回填）

- **行為**：系統閒置（busy→idle 後 180 秒 grace）時，一次回填一頁的 facets，步間隔 30 秒；`toLingLing/`、`Consolidate/` 有新檔（mtime < 10 分鐘）就讓路，使用者工作永遠優先（busy lock 仲裁 + idle callback 最後註冊）。陳舊的卡住檔案不會餓死回填。
- **零成本捷徑**：Part 頁面的 facets 直接解析既有的「Part Digest Appendix」，不花 LLM call；只有無 appendix 的頁面才花一次 digest call。
- **佇列從 DB 推導**（不持久化，與 orphan sweep 同哲學），優先序：Synthesis → 近 30 天被檢索命中（trace 查詢）→ 一般頁 → Part 頁。唯一持久狀態是失敗 ledger（`Database/facet_backfill_state.json`）：單頁失敗 3 次隔離（檔案改過自動解除）、連續多頁失敗視為服務中斷退避 1 小時、每日 LLM 預算 1000 calls。
- **補完即安靜**：佇列空了就什麼都不做（一次性完成通知），新 ingestion 自帶 facets，佇列只在異常事件後重新出現。
- 參數：`FACET_BACKFILL_ENABLED` / `_GRACE_SECONDS`(180) / `_STEP_GAP_SECONDS`(30) / `_DAILY_BUDGET`(1000) / `_MAX_ATTEMPTS`(3) / `_MIN_BYTES`(400)。

### 2026-06-10 Self-Improving Bench Loop（檢索品質自動進步迴路）

- **評測集自動生長**（[maintenance/bench_builder.py](System_Engine/maintenance/bench_builder.py)，週任務）：每篇有 facet 的未覆蓋頁面，LLM 把 thesis 改寫成自然問句（禁止逐字抄），且**當下系統答得對才收錄**（品質閘門）。哲學是 regression guard：auto case 鎖定今天可用的能力，未來改動讓它失敗＝退步。寫入獨立的 `scratch/retrieval_bench_auto.yml`，手寫 bench 檔永不被改寫，auto 檔可隨時刪除重來。上限 `BENCH_AUTO_MAX_CASES`（30）、每輪 `BENCH_AUTO_PER_RUN`（5）。
- **Facet A/B lift**：每日 retrieval bench 每條 case 跑兩次（`use_facets` 開/關），量化 facet 淨貢獻。lift 持續為負時告警會建議關閉 `FACET_INDEX_ENABLED`。
- **歷史趨勢＋退步告警**：每次 bench 結果存入 `Database/bench_history.json`（365 筆、原子寫入）；pass rate 低於上次 → status 升為 `regressed` 並寫 `fromLingLing/` 告警，列出失敗查詢。
- 迴路全貌：**ingest（facet 自動產生）→ bench builder（評測集跟著長）→ daily bench（A/B 測量）→ history（趨勢）→ alert（退步立即可見）**——vault 越大，評測越完整，系統越知道自己哪裡退步。

### 2026-06-10 Facet Index（摘要向量指標檢索，Phase A+B）

- **概念**：LLM digest 的 `thesis` 與 `key_points` 被 embed 成「facet」條目存入同一個 collection（`role: facet`，共用母文件 doc_id）。Facet 是**檢索指標不是內容**——query 命中 facet 後，rerank 之前就解參照回母頁面的真實 chunk，下游永遠拿到原文。解決 query（問句）與 chunk（散文）的語意落差與跨語言落差。
- **Phase A（零新增 LLM 成本）**：長文 pipeline 既有的 part digests 直接轉成 facets，每個 part 頁面一批。
- **Phase B（每篇短文 +1 次輕量 LLM call）**：短文 ingest 後補跑一次 `generate_part_digest`（1/1），同樣轉 facets。
- **安全設計**：facet 共用母文件 doc_id，刪除路徑與 orphan sweep 自動涵蓋；重新 ingest 會先清舊 facets（冪等）；母 chunk 已在候選池時以較高排名者優先、不重複；幻覺風險被「只當指標、不當內容」的設計隔離。
- **控制**：`FACET_INDEX_ENABLED`（預設 true）、`FACET_MAX_PER_DOC`（預設 8）。建議用 `scratch/retrieval_bench.yml` 前後對比驗證檢索品質。

### 2026-06-10 RAG Orphan Sweep（殘留 chunk 清理）

- **問題**：刪除整個文章資料夾（`pages/<標題>/`）只發 directory 事件，舊 watcher 直接忽略；改名/搬移完全沒有 handler——兩者都讓 chunks 永久殘留在 ChromaDB。
- **修復**：`RAGManager.prune_orphan_chunks()` 以檔案系統為真相來源——掃描 `pages/`+`Notes/` 算出有效 doc_id 集合，刪除其餘所有 chunks（含無 doc_id 的 legacy chunks）。觸發點三處：資料夾刪除事件（debounce 5s）、新增的 `on_moved` handler（改名＝刪舊+索引新）、每日 maintenance task `rag_orphan_sweep_daily`（涵蓋 daemon 關機期間的刪除）。

### 2026-05-24 Capability Layer & Lens Dual-Link (Phase 4)

- **Capability metadata**：`Templates/Operations/*.md` 與 `Skills/*.md` 加上 frontmatter（`type / expected_inputs / produces / cost_class / applicable_when`）。新的 [services/capability_manager.py](System_Engine/services/capability_manager.py) 在 daemon 啟動時掃描並建索引；`LLMClient._build_system_prompt` 改回傳 `(prompt, resolution)`，resolution 寫入 `llm_calls.metadata_json.capability_resolution`，**不會**注入到 system prompt。Operations 的 frontmatter 在組 system prompt 前由 `_load_capability_body` 剝除。
- **Lens dual-link**：lens 報告的 evidence 同時輸出 Obsidian wikilink 與 `file:///` 連結。`file:///` 連結帶 `#L<start>-L<end>` fragment——只有 VS Code / Cursor 系列編輯器會跳到指定行，Obsidian 點擊與系統 `open` 會開檔但忽略行號。Wikilink 走 Obsidian 原生導航，永遠可用。
- **PipelineRunner / Pipeline DSL** 暫緩，設計草案見 [DesignDoc/PipelineRunner_roadmap.md](System_Engine/DesignDoc/PipelineRunner_roadmap.md)。

### 2026-05-23 RAG Quality & Cost Stack

第二輪 ChromaDB 優化，疊在 embedding provider / mismatch guard / migrations 之上。所有功能都可獨立 toggle，預設行為與舊版兼容。

**Ingestion 端（無腦省錢）**
- **Content-hash skip**：每個 chunk 多存 `content_hash = sha256(text + tags + section_path)`。`add_document` 比對既有 hash，相符就直接 return，連 delete 都不做。在 Obsidian 反覆存檔但未真的修改內容的情況下，零 embedding call。
- **Persistent embedding cache**（[services/embedding_cache.py](System_Engine/services/embedding_cache.py)）：SQLite at `Database/embedding_cache.sqlite`，key = `sha256(model || text)`。Cache hit ≈ 0.1ms vs cold ≈ 70ms (local MiniLM)。跨 provider 切換、wipe + reindex 都從 cache 直接回。控制：`EMBEDDING_CACHE_ENABLED`。

**Retrieval 端（品質 pipeline）**
- **MMR diversity**：`query_notes(diversity: float)` 0~1。over-fetch top_k*3，cosine-MMR 選出 top_k，消除相鄰 chunk 霸佔 top-k 的問題。
- **Cross-encoder reranker**（[services/reranker.py](System_Engine/services/reranker.py)）：`rerank=True` 時 over-fetch top_k*5，用 cross-encoder（預設 `BAAI/bge-reranker-v2-m3`）重新打分。延遲導入，未啟用時零開銷；sentence-transformers 缺失會 graceful fallback 到向量檢索。控制：`RERANKER_ENABLED` + `RERANKER_MODEL` + `RERANKER_MULTIPLIER`。
- **Hybrid BM25 + RRF**（[services/bm25_index.py](System_Engine/services/bm25_index.py)）：`hybrid=True` 時並列查向量與 BM25，Reciprocal Rank Fusion 合併。BM25 lazy rebuild from collection，add/delete 觸發 dirty flag；對「@ling-lens」「XYZBLATZ」這類精確 token 查詢顯著回升 recall。控制：`HYBRID_RETRIEVAL_ENABLED` + `BM25_MULTIPLIER`。

三層 retrieval feature 可以自由組合：`hybrid → rerank → MMR` 依此順序套用，rerank 的分數會餵給 MMR 當 relevance 訊號，hybrid 的 RRF 分數同理。

**啟動成本**
- `_check_metadata_mismatch` 不再每次啟動都 probe 一次 embedding dimension。Provider+model name 已是 authoritative key，匹配時直接 return；只在空 collection 初始化 metadata 時才打一次模型。Gemini 用戶啟動時節省一次 paid API call。

**依賴**
- `rank_bm25` 已加入 `requirements.txt`（純 Python，~10KB）。
- `sentence-transformers` 列為選用依賴；只在需要 reranker 時手動 `pip install`。

### 2026-05-23 Monte Carlo Concept-Level Sampling

- `InsightAgent._get_all_documents` 改成兩階段抽樣：先 uniform 抽 Book，再從每本書抽多個 chunk，讓碰撞池呈現概念層級的多樣性，而不是每本書只露出一個代表 chunk。
- 每本書內的 tier 優先順序反轉為 **raw Parts > (Synthesis) > (Stitched)**，保留未經提煉的原始概念給 Monte Carlo 碰撞使用；distilled tier 只在沒有 Parts 時 fallback。
- 新增 strategy frontmatter 參數 `chunks_per_book`（預設 5）。設小會更廣（更多本書、每本少抽），設大會更深（少本書、每本多抽）。
- 同一本書的不同 chunk 仍可配對，允許跨章節的概念碰撞。
- 移除舊的 `_pick_representative_title`，由新的 `_docs_from_book` 取代。

### 2026-05-23 Refactor Follow-up

- `BaseAgent._write_report()` 現在回傳 `(path, full_markdown)`；第二個值是已寫入磁碟的完整文件（YAML frontmatter + body）。需要 mirror report 的 caller 應直接寫這份完整內容。
- `InsightAgent` 的 `Insights/` mirror copy 改為與 `fromLingLing/` canonical report byte-identical，避免 mirror 失去 `title`、`type`、`version`、`date_created`、`input_chars`、`output_chars` 等 metadata。
- Mermaid label repair 擴充到多種 node shape（`[]`, `()`, `{}`, `{{}}`, `[[ ]]`, `[()]`, `([ ])` 等），並修正 `A[Start] --> B[End]` 這類箭頭被誤判成 asymmetric node 的 regression。
- `TextSplitter` 預先計算 fenced-code regions，避免在每個 chunk boundary 重新掃描全部 fence，並保留 legacy helper API 供測試與外部呼叫。
- 新增 LLM-free regression tests，覆蓋 CounterAgent、IngestionPipeline、Insight mirror、LLMClient helper、Mermaid repair、TextSplitter fence protection。

### 2026-05-15 Ingestion Pipeline Refactor

- `ClippingWatcher` 現在只負責 filesystem events、busy state、檔案類型分流與歸檔。
- Markdown ingestion 主流程移到 `services/ingestion_pipeline.py`。
- 長文切分改用 `TextSplitter.split_text_with_spans()`，保留每個 chunk 對應的原文 char/line range。
- Part metadata 會寫入 `source_start_char`、`source_end_char`、`source_start_line`、`source_end_line`。
- Synthesis metadata 會寫入 `part_source_map`。
- Stitched Article 會在每個 Part 前顯示原文範圍，供 LingLens evidence link 使用。
- `extract_json_array()` 與 `extract_json_object()` 移到 `core.parser`，讓 LLM JSON parsing 共用。
- digest value formatting 移到 `core.utils.digest_value_to_text()`。
- `PromptWatcher` 改用 declarative intent routing table，`@ling-lens` 與 `/lens` 成為正式入口。
- `VaultWatcher` 復用 main process 的 `LLMClient` 翻譯新標籤，避免每次手動改檔都建立新的 client。
- **Unified Template Routing**: 實作四層級模板路由（指令 > Skill > Scripture > 系統預設），支援在指令中加註 `/template tech-rpt` 隨時切換格式。
- **Mermaid & YAML Hardening**: 修復 Mermaid 標籤自動加引號（支援中文字元與數字 ID）、修復 Mermaid fence 提前關閉的預判邏輯，以及防止 Markdown 水平線被誤判為 YAML frontmatter 的防呆機制。

### 2026-05-13 LingLens Evidence Grounding

- 新增 `@ling-lens` / `/lens` 概念透鏡指令。
- `@ling-count` / `/count` 保留為 legacy alias。
- 多文章與多概念會輸出 cross-analysis matrix。
- evidence 會盡量連回 Stitched Part anchor 與原始檔。
- 若 Part metadata 有原文範圍，報告會顯示原文 line range。

### 2026-05-10 Synthesis Quality Upgrade

- 長文解析由 Part 第一行摘要升級為 structured digest synthesis。
- 每個 Part 會整理 `thesis`、`key_points`、`evidence`、`terms`、`open_questions`、`handoff`。
- Synthesis 會附上 Part Digest Appendix，方便檢查合成依據。
- Markdown quality checker 會修復裸 Mermaid、未關閉 Mermaid fence、body YAML frontmatter 等常見問題。
