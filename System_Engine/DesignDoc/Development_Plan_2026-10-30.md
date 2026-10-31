# 開發計畫重評與未完成工作 — 2026-10-30

> 本文件是截至 2026-10-30 的執行排序入口。舊 roadmap／implementation plan 保留設計與
> 歷史決策；其 checkbox 或「待實作」不直接代表現在還沒做。後續狀態變更請更新本表，
> 每個工作以獨立可驗收的變更交付。本輪只做評估與文件，不啟動下列實作或 live 修復。

## 1. 評估結論

目前主要缺口是「證據與診斷能否支持自我修改」，不是缺少新架構。
A2 雖能持久化補證與追補索引，但歷史樣本證明 supports 的語義太寬；M2 的因果推論
仍需程式端隔離。應先建立可信的證據判定、可重播的觀察資料與修復流程，再討論
擴大自動化、本體論或 planner 自我調校。

優先順序採四個判準：錯誤寫入持久信念的風險、實際使用影響、是否阻塞後續判斷、
現有資料能否驗收。低資料量不以等待固定天數掩飾；沒有樣本就標為未驗證。

## 2. 查核依據與基線

評估以 [NextPhase roadmap](NextPhase_roadmap_2026-07.md)、
[A2 支持品質稽核](A2_support_quality_audit_2026-10-30.md)、
[Ingestion 效能與完整性計畫](Ingestion_Performance_Integrity_Plan.md)、
[M1–M4 計畫](SelfImprovement_metacognition_plan.md)、
[Ontology／SE 計畫](Ontology_SemanticEntropy_implementation_plan.md)、
[Phase 4.5 後續計畫](Roadmap_Phase4.5_onwards.md)、
[7 月重構計畫](Refactor-Roadmap-2026-07.md) 及相關程式／測試為準；
並唯讀查核現有 maintenance state、trace、報告與提案佇列。
這不是逐行重做全庫 code audit；未有足夠證據的舊待辦另列待確認，不當成已確認 bug。

| 項目 | 目前可確認的狀態 | 尚未證明的事 |
|---|---|---|
| 工程門檻 | `make check` exit 0，1934 passed、1 skipped，ruff／mypy clean；1 個既有 Chroma warning | 測試通過不代表長期生成品質已改善 |
| 分支交付 | `codex/a2-audit-hardening` 已推至 `fa53ca0`；Scout 正文修復 `a5339ce` | 尚未合併 main；是否合併是獨立交付決定 |
| A2 可靠性 | transport／parse 分類、真實距離 gate、持久化索引追補已實作及離線測試 | 重啟後新 A2 正常排程、實際修復樣本、contradicts apply 尚未驗收 |
| A2 語義品質 | 28 筆歷史 supports；8 筆完整 trace 複核為局部 5、類比 2、機制不符 1 | 不能推算 28 筆的錯誤率，也不能據此判 claim 為假 |
| M1／M2／M3 | active 分層及 Cortex 推論守衛已實跑；模型假設不能進 M3 | 模型本身的因果推論仍不可靠；其他軸沒有同等守衛 |
| 待審提案 | 15 筆內容未變；新版本防重複與退回理由機制已實作 | 舊競爭提案仍未完成逐筆審閱／整併 |
| Scout | 英文 `economy OR finance`；10/30 四來源成功、29 新項目；Google 4/4 原網址、2 正文／2 明確降級；Brookings 訂閱提示被新檢查排除 | heuristic 不保證排除所有站型／報價頁；不能以 4 則宣稱整體成功率 |
| 新長文 | `Anthropic, PBC - S-1 (Q2)` 229/229 Parts，document complete、archivable=true；230 筆 artifact timing | 完成不代表每個圖表／摘要內容正確，也不構成跨題材效能 A/B |
| 該份 synthesis | 10/30 00:16 出品，舊 engine_build `2026-08-08`，兩次 critique 皆 unparseable，NeedsReview | 不是新版 failure-status 契約的 live 驗收；目前 parser 重播兩次原輸出仍為 None |
| 近期其他訊號 | 檢索 bench 39/45（87%）；retained planner run 0 | 0 planner run 只指保留期內，不代表從未使用；不能立刻啟動學習迴圈 |

狀態用語：**待實作**、**已實作待驗收**、**待稽核／決策**、**條件式暫緩**。
「完成」需說明是程式、離線測試、實跑還是成效觀察完成，不能混用。

## 3. 立即工作：P0 證據可信度

下表是執行順序；規模 S／M／L 為相對工作量，不是工期承諾。

| ID | 狀態／規模 | 工作與交付物 | 依賴 | 驗收條件 |
|---|---|---|---|---|
| A01 | 基準已實作／標註待複核（10/31） | 建 A2 固定判讀評估集：本次 8 個可重播樣本的合成變體，另加真正直接支持、明確反證、neutral、複合 claim、規範建議及惡意引文案例；標出人工預期與理由 | 現有稽核 | 每個案例有可覆核標準；公開 fixture 不複製私有長段資料；negative 與 positive 對照齊全，不能靠全部判 neutral 過關 |
| A02 | 程式已實作、隔離模型評估待驗收（10/31）／M | 支持範圍契約：區分直接完整、局部、類比；要求支持引文、未支持部分及適用範圍；更新 parser／report／apply 並更正 `_is_independent` 的過期註解 | A01 | 只有範圍與資料有效的直接完整支持能新增完整 evidence／reinforce；局部與類比留報告；缺欄、壞 schema、不存在引文不得套用；contradicts、transport、parse 狀態不混同 |
| A03 | 程式與隔離故障驗證完成（10/31），live 待驗收／M | 可重播的 A2 audit record：claim／falsifier basis、送審段落快照或可恢復私有引用、hash、來源 family／path／範圍、contract／prompt／model 版本、trace 與判定 | 與 A02 一起定格式，獨立交付 | trace 到期後仍可覆核；套用前檢查 basis；稽核資料無法落盤時不提交不可稽核的補證；原子寫入、崩潰恢復、去重及保存政策有測試；私有內容不進公開 Git |
| A04 | 待實作／M | 原始來源 lineage 與獨立性：分開「不同顯示 title」與「不同原始證據」，追溯 Part／Stitched／Synthesis、原 insight 來源及 grounded_on | A03 | 同一原文件不同 view 不灌票；已知同源拒絕，無法確認獨立性明列未知、不冒充獨立佐證；缺資料不藉批量重生補齊 |
| A05 | 已實作部分、待驗收／S | A2 新版端到端驗收報告：正常排程結果、來源／距離／範圍排除、index intent 與 trace／state 一致性 | 現有可靠性可先唯讀觀察；新版語義驗收依 A02–A04 | 部分失敗如實呈現、成功不重複強化、空池仍可追補；反證寫入用隔離資料驗完整交易，live 無反證樣本時仍標缺口；不故意破壞 live index 來製造故障 |

A02 的字面引文匹配只能驗證「引用存在」，不能證明語義支持成立；必須再跑 A01
評估集及有限、隔離的真模型判讀。失敗時維持不能 apply，不用放寬距離／加信心補救。
新欄位的 schema version／舊紀錄相容策略須先定義；不把舊 supports 自動當成新版合格證據。

A05 不需要等待 A02 才觀察既有可靠性，但舊契約下的成功 run 不能取代新版語義驗收。
若要手動觸發 A2，另以小變更新增 daemon 命令，沿用 busy lock／trace／設定與頻率規則；
不從另一程序直接呼叫 live maintenance，也不改 state 時鐘強迫排程。

## 4. 下一批：P1 既有迴路閉環

| ID | 狀態／規模 | 工作與交付物 | 依賴／先後 | 驗收條件 |
|---|---|---|---|---|
| B01 | 待稽核／M | 歷史 28 筆 supports 的逐筆處置清單，分保留、局部線索、需更多資料及建議撤回；另做歷史 Cortex↔RAG 唯讀對帳 | 可先盤點；正式重判依 A01–A04 | 無 trace 的 20 筆不偽造歷史段落；人工確認處置前不刪 evidence、不回滾 confidence／S、不批量重建索引；修復另有備份與冪等方案 |
| B02 | 待實作／M | M2 診斷輸入包含元件能力、相關任務執行結果與可覆核樣本；把必要的推論限制推廣到其他會進 M3 的軸 | A03、B04 的乾淨訊號；可先做一般能力對照 | 重播 10/30 的誤判與其他軸樣本；證據不足仍只給假設／查證；Cortex 不因多幾個數字或模型高信心就解除 gate；M3 消費端限制保留 |
| B03 | 待審閱／S | 整理 15 筆既有 M3 提案：同 target／basis 競爭組、schema 不符與能力不可能的修改、可保留候選及退回理由 | 不需等模型工作，可作 P0 期間獨立小批 | 交付逐筆建議及 diff；由使用者決定 approve／reject；不以清空佇列為目標。人工確認的退回理由進正式流程 |
| B04 | 待診斷／S–M | Synthesis 判定格式與新版狀態驗收：先檢查新取得的 229-Part 文件兩次原 critique，定位模型漏寫判定或 parser 缺口，再做最小修正 | 現有 parser 重播已確認仍 None | 同份回應可重現；合法格式不破、漏 verdict 仍 NeedsReview；保留 bounded retry；用一份代表性短長文驗證新版 status／history／artifact，不重跑 229 Parts 才驗一條 parser |
| B05 | 待稽核／M | 長文與 learning artifacts 的效能／品質收尾報告：Part 完整性、耗時分位數、實際 HTTP attempts、queue／wait、pending／deferred／quarantine 及至少 15 個 Parts 人工抽查 | 可在 A01–A04 間穿插；先查可用資料版本 | 229-Part 文件與數學書分開；不同題材不能當 routing A/B。若原數學書前後資料已過期，明示缺口，不補造比較；圖表須有內容正確性、互補性與 ownership 檢查 |
| B06 | 待評估、按欠帳決定／M | 檢查 artifact deferred／pending 是否長期無喚醒；只有證明存在可處理欠帳，再設計持久化恢復／排程接點 | B05 | 沿現有 slot ledger、TTL、basis 與衝突保護；不把 deferred 當永久 skipped，不重生人工區域。以現有 timer pump／busy lock 交付，不新增無管理 thread |

B01 的「歷史索引缺漏」與新 A2 index intent 是兩件事：新 intent 的零待辦不能證明
歷史索引全部健康。對帳避免直接開啟第二個 Chroma writer；先用 daemon 唯讀介面，
不足時再設計正式診斷介面，不以裸跑 RAG constructor 當安全檢查。

B05 優先利用已落盤的資料，不重新 ingestion 來取得漂亮的成效數字。soft artifact
budget 不是取消進行中 HTTP request 的硬上限，報告不能把 600 秒寫成每件工作必然截止。

## 5. P2 衡量與產品品質

| ID | 狀態／規模 | 工作與交付物 | 啟動條件／驗收 |
|---|---|---|---|
| C01 | 已確認小缺口，待修／S | 手動 M1 的 source baseline 對齊：`ImproveAgent._generate` 與 `CommandDispatcher._run_brain_op('assess')` 尚未傳入 rag，scheduler 已有 | 用注入的 rag 傳入 `run_self_assessment`，測試手動與排程資料契約一致。缺 baseline 不顯示成零；不因此重建 embedding |
| C02 | 待重稽核／M | 更新 N.2／SE／grounding／doc-anchored falsifiability 的成效報告 | 先 C01；依 operation、建立時間、backend／版本與 active cohort 分層。SE 的來源／產出比、固定母體增厚及 decay 分開；不以混合全庫 aggregate 證明干預有效 |
| C03 | 待觀察／S | Scout 英文經濟／財經來源品質：累積促銷頁、報價頁、付費牆與誤拒短新聞的案例，再決定篩選／readability 需要 | 保留 economy OR finance 與英文來源需求；不擅自縮成只看股市。先標註再調 300 字門檻，報告保留缺正文標記，來源解析失敗可見 |
| C04 | 待盤點／S | Build／配置／執行版本可觀測性及當前 feature branch 整合 | 用啟動與任務版本資料區分 stale module／新碼失敗；交付可覆核版本。完成 review 後由使用者決定合併 main；不得自行切 main 或部署 |
| C05 | 待數據／S | Lens 引文門檻校準（目前 0.8） | 先有翻譯／原文分組的人工作答基準與足夠樣本，衡量漏報／誤報；不為讓健康燈轉綠直接降門檻 |

C01 是程式比對確認的缺口，尚未實作；C02 不能只用 10/30 手動報告顯示「無來源基準」
就斷言整個 SE 模組不存在。B02、C01 和 B03 可獨立做，但不插隊延遲 A2 的 P0 可信度。

## 6. P3 條件式暫緩與待確認項

| 項目 | 決定 | 重新啟動條件 |
|---|---|---|
| O1–O4 概念本體層 | 暫緩；O0 修圖與 SE 已存在，不重做 | A2 的證據／來源與觀察資料可信，且有具體概念查詢用例不能由現有 claim 圖滿足；先寫用例與對照，再決定做到哪一層。薄證據率下降不是啟動本體論的充分條件 |
| Planner learning loop | 暫緩 | 有實際 planner 使用及可用 verdict／人工評價資料；目前 retained planner run=0，先做資料可用性確認；不建立無消費者的自調 loop |
| 多 server／LLM fan-out | 暫緩 | 同 request set、零失敗、backend benchmark ≥1.5× speedup，且 trace context／ownership／取消及成本語義已定義；不把切換 provider 名稱當成能並行的證明 |
| ThoughtfulSplitter 新演算法或調參 | 暫緩；原 P0–P6 已完成 | 新增人工標註 boundary／completeness corpus 與使用者可見的錯切案例後才改。已有結構測試 corpus 不等於已有語義邊界標準 |
| 擴充 M4 自調旋鈕 | 暫緩；M4 本身已實作 | 每個旋鈕有乾淨 outcome metric、min-sample、bounds、rollback／freeze 驗收；不拿失真的 supports 或混合品質率當目標 |
| 新的大規模 refactor／全庫 strict／async 化 | 不排主線 | 有可重現維護或效能痛點才開小批，不以模組行數單獨立案 |
| 7 月舊重構「7 個過期測試／IngestResult／tension renderer 死碼」 | 舊狀態不可照搬 | 現在 fast suite 僅 1 skipped，`IngestResult` 已有 typed 模組，`TensionAgent._render` 是現行呼叫路徑。除非新證據，不重列為待修；本輪未稽核所有 optional/live tests |
| 舊 R7-C-2／R7-D 小型架構債、translate_tags retry 一致化 | 待確認，低優先 | 先用當前呼叫點證明仍存在與影響，再決定是否開工；不照舊清單直接搬家 |
| 概念卡／外部 Anki 等整合 | 暫緩新整合 | 既有 spaced review 已有命令與排程；需使用者的學習流程需求才擴大成外部同步產品 |

## 7. 不重複開發的已落地項目

| 舊文件可能造成的誤解 | 本次處置／證據 |
|---|---|
| A2 仍只有 dry-run／索引失敗只記 warning | apply 與新 index repair 已存在；剩語義與 live 驗收，見 A01–A05 |
| M1–M4 還待建、Cortex M3 應直接啟用 | 都已有模組；現在刻意以 evidence gate 暫停不受支持的 Cortex prompt 修訂，不是接線沒做 |
| PipelineRunner／Planner Phase 4.5–6 未開始 | runner、adapter、planner／executor／readiness 及命令已存在；剩有資料前提的 learning loop，不重建 runner |
| LearningArtifacts 文件仍寫「規格待實作」 | `learning_artifacts.py`、artifact dispatcher／slot ledger／quality gate 與測試已存在；目前要 B05 品質／效能驗收與 B06 恢復盤點 |
| Synthesis critique、retry、專用生成與 resynthesize 命令還全待做 | 已有生成、critique loop、NeedsReview、history 及命令；舊 roadmap 的 richer source／prompt lineage 仍可依 A03／B04 實際需要補，不重做整套 |
| PromptSystem P3–P6 checkbox 未勾 | `pin_language`、prompt asset tests、架構文件均存在；未勾不作未實作證據，未逐項證實的細節留文件整理 |
| Refactor 工具鏈與 typed ingestion 全未做 | pyproject／ruff／mypy／pre-commit／make check、facades／composition root、typed result 已存在 |
| Scout 還需要回復空泛跨來源綜合分析 | 該功能是使用者判定無效後移除；維持逐項摘要、streak 與 RAG bridging，不把已否決方向當 backlog |

## 8. 實際執行批次與完成條件

1. **A01 → A02＋A03 的離線實作已完成**。標註獨立複核與隔離真模型評估仍欠，
   不把評分器／fault tests 通過當成語義品質驗收。詳見 §10–12；各邏輯修復獨立提交。
2. **下一批：A04 → A05**。確認 lineage／unknown 行為，再收新版觀察窗；既有 A2
   可靠性排程可在第一批期間唯讀追蹤，不必為等 live 樣本停止所有離線工作。
3. **第三批：B01＋B02**。用新版可覆核樣本訂歷史處置與 M2 輸入契約。
   B03、B04、B05 可在等待觀察窗時各做一個獨立交付，不同時修改共享 prompt／schema。
4. **第四批：按 B05 欠帳決定 B06；C01 → C02**，再依真實錯誤樣本安排 C03／C05。
   C04 的分支 review／整合隨交付節奏處理，不阻塞安全修正但也不默認已合併。
5. **最後：依重新啟動條件評估 P3**。沒有需求或資料就保留暫緩，不以日期到了自動開案。

各批次完成須具備：明確行為契約、必要故障路徑的 hermetic 測試、`make check` exit 0、
與變更相稱的實際樣本／隔離驗證、roadmap 交接與剩餘限制。實跑只驗管線，不自動證明成效。
模型輸出仍不可靠而兩輪 review 未收斂時，收窄問題或提升 deterministic guard，
不靠延長 prompt 或調高信心勉強過關。

## 9. 操作與交付邊界

- vault／Chroma 寫入須由 daemon 既有 busy lock 序列化；先離線實作，重啟與實跑
  配合使用者長工作窗口，不另啟 live writer。
- 不批量改寫歷史 evidence、prompt、索引或 229-Part 文件；任何修復先交可審閱清單、
  備份、basis／ownership 與 commit／rollback 語義。
- 本計畫不授權自動 approve／reject、main 合併、重啟、部署或付費長重跑；到具體動作
  時依當時使用者授權執行，不把計畫本身當操作指令。
- 以狀態與驗收條件排程，未在沒有排班／backend 量測的情況下承諾日曆工期。
- 本輪稽核文件已 commit／push 為 `fa53ca0`；本計畫與入口連結為其後新文件，
  尚未 commit／push。最後完整檢查仍為 `make check` exit 0：1934 passed、1 skipped、
  1 個既有 warning。本計畫只改文件，另驗 `git diff --check` 與相對連結。


## 10. 2026-10-31 A01 進度

- 已建立 [案例、標準與操作說明](../tests/fixtures/evidence_evaluation/README.md)：
  24 個短篇合成案例（full_support 6、partial_support 6、analogy 2、contradiction 4、neutral 6），
  含 8 個歷史過度支持模式與正反對照。不將私有 passage／trace 原文放進 Git。
- `annotation_status=assistant_authored_pending_review`：參考標註由助理編寫、尚未獨立
  複核。資料集與評分器已可用，不等於模型準確率或標註品質已通過獨立驗收。
- 新增純離線 `services/evidence_evaluation.py` 與
  `scripts/evaluate_evidence_cases.py`：匯出不含答案的輸入、綁定 dataset ID／SHA-256、
  逐類別 recall／confusion／過度支持與缺漏／error 計數；缺案例不移出分母，重複／
  非法輸入拒絕。現行 production A2 JSON 與 apply 完全未改。
- 全 neutral 控制為 6/24、支持／反證 recall 皆 0；全 full_support 控制有 18 筆
  過度支持，兩者均不通過。這些是評分器控制測試，不是模型實測。
- 新增 31 個 hermetic 測試，包含 CLI 以 Python `-S`（無 provider／DB 第三方依賴）
  執行、答案不洩漏、錯版資料、缺漏／失敗分母、異常 JSON 及 CLI exit code。
- `make check` exit 0：ruff clean、mypy 169 source files 無錯誤；1965 passed、
  1 skipped、1 個既有 Chroma warning；`git diff --check` clean。
- 下一步為 A02 的 schema／判讀／apply 支持範圍契約，與 A03 共同定義 basis／稽核格式；
  真模型的隔離評估及標註複核仍待完成，不自動放行 live apply。
- 本輪未重啟 daemon、未呼叫 LLM／RAG、未修改 live vault／DB、未 commit／push。
  本文件原有 10/30 baseline 仍保留；本節記錄新增進度與最新檢查結果。


## 11. 2026-10-31 A02 支持範圍契約

- 新判讀契約 `contract_version: 2`：`relation` 為 `full_support`、
  `partial_support`、`analogy`、`contradicts`、`neutral`；另必須有非空 `reason`、
  `applicability`，以及字串陣列 `quotes`、`unsupported`。A01 的 `contradiction`
  是評估標籤，對應 production 的 `contradicts`，不擅自改寫固定評估集。
- 非 neutral 必須有引文；引文逐字匹配實際送審的前 2000 字，不能引用截斷外內容。
  full_support 的 unsupported 必須為空；partial_support／analogy 必須明列缺口。
  缺欄、錯型別、錯版、舊 `supports` 或不存在引文都回 unparseable，不當成 neutral。
- 判讀改由 `LLMClient.judge_evidence_scope` 統一呼叫，方法論在版本控制的
  `Templates/Operations/judge_evidence_scope.md`，經 `_load_capability_body` 剝除
  frontmatter。缺 prompt 時明示 error；內容／schema 失敗最多兩次 reroll，各次
  trace 帶 contract_version／scope_attempt。transport 失敗不消耗另一層 content retry。
- apply 消費端重驗整批契約及傳入 page 的 claim_id／claim／falsifier；整批通過後才
  修改。只有 full_support 新增完整 evidence 並沿既有索引意圖交易強化一次；局部支持
  與類比只留報告，兩段局部支持不拼成完整支持。contradicts 只記 counterpoint，
  不改 confidence／S。既有重複來源刷新與索引追補故障語義保留。
- 報告列出適用範圍、引文與未支持部分。新 evidence 附 optional
  scope_contract_version／scope_relation／quotes／applicability；Cortex reader
  保留這些原始 metadata，後續 save 不丟失。這些欄位本身不是可信判定或套用授權；
  不為舊 evidence 自動補版本，不改 Cortex 固定標頭與既有 schema_version。
- `_is_independent` 過期的「不會寫狀態」註解已更正。其 title／path 排除仍是
  heuristic，沒有完成 A04 原始來源 lineage；引文存在也不能證立語義支持。
- A03 仍待獨立實作：目前記憶體 basis 對照及報告不是 durable audit record，也沒有
  落盤快照 hash／prompt hash／model lineage／保留政策或 apply 前重讀磁碟的保護。
  A03 應以本版六欄判定作 payload，另綁 claim／falsifier snapshot、實際送審 passage、
  source family／path／範圍、版本與 trace；audit 無法持久化時不可提交 evidence。
  其 write-ahead／vault commit／crash recovery 邊界要與既有 index intent 一起驗收。
- 新增 41 個隔離回歸案例，覆蓋局部／類比不強化、schema 與引文拒絕、舊 supports
  不能繞過 apply、整批先驗後改、basis 改變、截斷外引文、prompt 缺失及 scope metadata
  讀寫往返。既有 transport／content retry、contradicts、索引失敗與恢復測試保留；
  原測試 fake 同步產生 v2 完整欄位，舊格式拒絕另有回歸測試。
- 本輪沒有重啟 daemon、沒有呼叫真模型或 RAG、沒有修改 live evidence／DB，未
  commit／push。A01 標註獨立複核、固定案例的隔離真模型評估及 A03／A04 尚待完成；
  不把 hermetic 測試通過宣稱為語義品質或新版 live apply 已驗收。
- 最新驗證：`make check` exit 0，ruff clean、mypy 170 source files 無錯誤；
  **2006 passed、1 skipped、1 個既有 Chroma DeprecationWarning**，20.27 秒。
  `git diff --check` exit 0。


## 12. 2026-10-31 A03 私有稽核與本批交付

- [A03 record／commit／保存契約](Evidence_Audit_Record_Contract.md) 與
  `services/evidence_audit.py` 已完成。§11 的「A03 仍待實作」是上一批狀態，本節取代。
  每個有效判定保存原始 claim basis、實際送審 passage、來源 path／family／chunk 範圍、
  hash、完整 prompt body／內容版本、provider／model identifier、trace run 與六欄判定。
  保存於 Git 忽略且不進 RAG 的私有 Database 目錄；本批提交不含私有來源快照。
- audit 內容定址、原子落盤且 file／directory fsync；同 payload 不覆寫，壞 slot 明示
  失敗。包括未引用 preparation 在內均不自動到期，trace TTL 不影響重播資料。
  model alias 不等於權重版本，來源 family 仍標示 unverified，不冒充 A04 已完成。
- audit 持久化失敗不得提交 evidence。apply 前與 intent 準備後重讀 raw snapshot，
  有變更則保留人工編輯並回報衝突。提交以 vault atomic replace 為準；audit 不是
  applied/done ledger，trace artifact 失敗也不推翻已持久化的紀錄。
- 新 evidence／counterpoint 帶 audit ID，Cortex 讀寫保留引用。commit 前崩潰只留下
  preparation，正常重試可去重；commit 後由既有 index intent 恢復，不再強化。
- 新增 40 個隔離測試：完整性／去重／永久保留、replace／directory fsync 故障、
  稽核失敗不套用、模型不可偽造 client provenance、basis 讀取失敗仍輪轉、人工編輯
  衝突、commit 前後崩潰及 trace artifact fail-soft。既有 A01/A02 與索引恢復測試保留。
- 最新 `make check` exit 0：ruff clean、mypy 171 source files 無錯誤；
  **2046 passed、1 skipped、1 個既有 Chroma warning**（15.80 秒）；
  `git diff --check` exit 0。
- 本輪使用者明確授權完成一段後 commit／merge main／push：A01 提交 `be6d59a`、
  A02 提交 `8ece600`、A03 提交 `8e25475`，另提交本交接文件後整合 main。
  先前 §9–11 的未提交說明保留為歷史紀錄；實際遠端交付以本輪 Git 結果為準。
- 使用者已先重啟 daemon；唯讀狀態檔顯示待命，但沒有載入版本資訊，不能據此判定
  A02 已實跑。本輪之後新增的 A03 要再次載入新版 daemon 才能確保生效；本輪未自行
  重啟、手動觸發 maintenance、呼叫真模型或開啟 live RAG／DB writer。
- 下一段為 A04 原始來源 lineage／unknown 契約；A01 標註複核與隔離真模型評估、
  A05 新版排程／端到端成效仍待完成。A03 的可稽核性不替代語義品質驗收。
