# 精簡執行計畫 — 2026-11-04

使用者已確認以「簡單、強韌、可維護」收斂工作。本表取代先前 16 項主線加 8 項條件式
待辦的執行排序；舊清單保留在 Git 與 [原開發計畫](Development_Plan_2026-10-30.md) 供查考，
不再逐項開發或為缺少 live 樣本建立新驗證專案。

## 只保留三項工作

| 工作 | 本次處置 | 接續原則 |
|---|---|---|
| 收斂自動化 | 用既有設定關閉定期 M2／M3，A2 改報告模式；M1 保留 | 不另建自動診斷因果驗證平台；按需人工查核與修復 |
| 核對核心使用流程 | 先唯讀檢查佇列、索引追補與既有文件狀態 | 以實際需要的匯入／檢索／來源引用為驗收；沒有使用需求不新增 benchmark 或強迫重跑文件 |
| 修復已證實風險 | 只優先看仍 active 的可疑 evidence、確實卡住的核心工作 | 每次先有可重現問題，再做最小修正與必要回歸；保留人工編輯與原資料 |

## 設定與操作

- 本機 `.env`：`SELF_DIAGNOSIS_ENABLED=false`、`SELF_IMPROVE_ENABLED=false`。
  檔案含本機設定，不納入 Git；程式預設本來即為 false，無須改 runtime code。
- Scripture：`evidence_traceback=true`、`evidence_traceback_apply=false`。
  A2 繼續報告，不新增 evidence／counterpoint 或強化信念；既有寫入防線及修復能力保留。
- 使用者已授權此設定收斂；閒置時正常停止舊 daemon、釋放 PID lock，在互斥範圍內原子
  修改設定，沿用既有 `start.sh` 啟動。未新增服務管理器、排程、worker 或診斷框架。
- 原 `.env` 與 Scripture 已備份至本機 private tmp，設定修改時核對來源 hash 並保留權限；
  執行結果與載入確認見本輪交付摘要。模型、其他排程與資料不調整。

## 一次唯讀健康檢查

觀察時匯入佇列 Consolidate 與命令佇列 toLingLing 皆為空，索引 repair intent 為 0。
這表示目前沒有上述待辦，不等於全庫索引或所有內容已驗證健康。
Clippings 是使用者放置材料的目錄，不以其中檔案數當成 daemon 卡住的工作。

既有頁面的 learning-aids slot：558 generated、44 preserved、1 pending。唯一 pending
是數學書 Part 164 的空學習輔助 slot，attempts=1，無 quarantine 時間。這是選配圖表，
不是核心文件匯入失敗；本次不為單一選配產物增加恢復排程或重跑整本書。

Cortex 狀態為 19 active、14 fading、89 dormant。仍有效的舊 EvidenceTrace 補證中，
7 條 active 主張共 8 筆未附新版 audit 的補證，當中 3 條 confidence=0.75。
這些是較值得按使用情境核對的範圍；缺 audit 不等於 claim 為假，也不足以直接撤回或
回滾 confidence。本次不改 evidence、confidence、索引或原文，不對全部歷史存量啟動修復。

## 保留、結案與退出主線

- 保留：原子寫入、有限重試、去重／crash recovery、人工內容保護、支持範圍／引文／
  來源 gate；不為追求簡單而移除防止資料損壞的接縫。
- C01 rag 接線與 B04 缺判定警示已有修復及回歸；程式修復結案，正常使用中觀察即可。
- C04 已能識別載入 commit／session；保持現有能力，結案觀察，不繼續擴建版本平台。
- A01／A02 的案例與分歧保留為已知限制，不追逐未複核 gold 的 24/24；A2 目前不寫回。
  A03／A04 不做全量 provenance 回填，不在正式環境製造故障以補驗收。
- B03 15 份提案的[審閱與 diff](M3_pending_review_2026-11-04.md) 保留，佇列不變；沒有
  必要為清空 queue 做決策。需要時才逐筆依既有人工流程處理。
- 全面品質／效能稽核、SE 成效報告、門檻校準，以及本體論、planner learning、fan-out、
  新自調旋鈕與大規模重構均退出主線；有實際錯誤／需求才重新立案。

後續每項改動先回答：已發生什麼問題、既有機制為何不足、修改成本是否值得。
未驗證如實記錄，不自動轉為開發工作。集中驗證與必要重啟，避免每個小段都擾動 daemon。

## 2026-11-07 補記：推理 token 截斷與 M1 盲點

屬「修復已證實風險」。

**發生了什麼。** gemma4:26b 的推理 token 也計入 `max_tokens`。固定上限會讓回應停在
推理途中或 JSON 寫到一半，provider 回 `finish_reason=length`，但 trace 狀態仍記為
`succeeded`，不會拋例外。

- `extract_claims`（上限 1024）：至少自 2026-10-12 起約 96% 的呼叫被截斷。claim 抽取
  全數失敗，29 則洞察被隔離，Cortex 這段期間沒有新增 claim。先前稽核看到的
  「Cortex 薄證據／零 merge」主因即在此。
- `generate_part_digest`（上限 1800）：43 次 digest 中 22 次兩次嘗試都被截斷，頁面
  拿到 degraded fallback（沒有 highlights／evidence／terms、跳過 facet 索引）；
  facet backfill 有 20 頁因此達到 `max_attempts` 被永久隔離。

**已交付。**

- `26cb022`、`9a7aa29`：兩個呼叫改為 `max_tokens=None`，沿用既有的推理模型慣例，
  並附回歸測試。已合併並推送 main，daemon 已重啟載入。
- 實測恢復情況：
  - facet：touch 20 頁的 `pages/`／`Notes/` 副本（只改 mtime，內容 hash 不變）後，
    20/20 重新 digest 並寫入 facet；每次用量 2094–8557 token，全數超過舊上限。
  - Cortex：首次重試即產出 3 條新 claim、4/4 adjudications；同日隔離數 29→19，
    其餘在各自的 TTL 到期後陸續 half-open 重試。
- M1 補上盲點：
  - 「LLM 健康」原本只計 `status=failed`，看不到截斷。現在由 `llm_call_health`
    統計 `finish_reason=length`，逐 stage 判斷；樣本 ≥5 時，截斷率 ≥10% 亮黃燈、
    ≥30% 亮紅燈，並點名 stage。
  - 「Cortex 信念」新增 consolidation 被隔離的洞察數；≥3 則亮黃燈，>0 即列入觀察。
  - 以正式資料唯讀副本試跑：LLM 健康為紅燈，正確點名 `extract_claims` 97% 與
    `generate_part_digest` 66%。

**解讀注意。** 2026-10-12 至 11-07 期間的 A2 命中率、M1 的 Cortex 指標與 Cortex 規模，
都是在 Cortex 幾乎沒有新輸入的狀態下產生的，不能當成正常基線。M1 的 7 天窗格在
修正後一週內仍會因舊呼叫亮紅燈，屬預期現象。

**未做／待觀察。**

- 確認其餘被隔離的洞察是否全數恢復；仍失敗者才視為真正的 poison input，逐筆處理。
- 其他偶發截斷（`answer_query`、`adjudicate_claims`、`generate_entity_page`、
  `generate_synthesis` 各數次）尚未抽查；有實際傷害才立案。不全面移除 token 上限。
- 9 頁帶 `digest_degraded: true` 的書籍分頁尚未確認成因。
- 隔離／half-open 重試策略不變：這次正是靠它在修正後自動恢復。
