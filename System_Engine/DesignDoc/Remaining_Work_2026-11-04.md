# 全部剩餘工作 — 2026-11-04

本表整合 [10/30 計畫](Development_Plan_2026-10-30.md) 與其後各段交接，取代只看舊表或
舊 checkbox 的狀態判斷。共 16 項主線仍有驗收／資料／決策欠項，另有 8 項條件式或
低優先工作；不是 24 項都需要立即寫程式。已實作的功能不重做，缺樣本仍保留未驗證。

## 主線：剩餘驗收、資料與決策

| 優先 | ID | 目前狀態 | 尚欠的具體工作 | 下一步／前提 |
|---|---|---|---|---|
| P0 | A01 | 固定 v2 案例與模型評估已完成，標註未獨立複核 | 24 案人工 rubric／標註複核，先處理 6 筆分歧及 partial／analogy／neutral 邊界 | 使用既有匿名案例與結果，不依模型答案倒改 gold，不擅自換模型 |
| P0 | A02 | 範圍契約已實作；參考分類一致 18/24，未達門檻 | A01 複核後決定是否演進判讀方案，再做有限隔離驗證 | 嚴格 schema／引文／apply gate 保留；目前小樣本零 unsafe_full_support 不等於 live 零錯誤 |
| P0 | A03 | durable audit／交易與故障測試完成 | 正常 live 判讀到 audit、evidence／counterpoint 引用、索引修復的端到端樣本 | 與 A05 合併收驗；不故意破壞 live index 製造故障 |
| P0 | A04 | lineage gate 與隔離測試完成 | 盤點並提出來源缺口修復；驗 shared／unknown／independent 在正常樣本的行為 | 既有盤點 12 條薄證據完整 closure 0/12；修資料前先有備份／basis／冪等方案，不放行 unknown |
| P0 | A05 | 11/01–11/04 四輪正常排程與負向排除已觀察 | live 合格送判、audit、補證／反證提交、不重複強化與恢復樣本；trace／state 對帳 | 四輪皆 0 判讀／套用；空 repair queue 不證明歷史索引健康，不調門檻製造樣本 |
| P1 | B01 | 待逐筆稽核 | 既有稽核的 28 筆歷史 supports 處置表；對缺原文 trace 的 20 筆明示資料缺口；另做 Cortex↔RAG 對帳 | 先重核當前 evidence，再提保留／局部／待資料／建議撤回；無 daemon 唯讀介面時先設計介面，不開第二個 Chroma writer |
| P1 | B02 | context／跨軸 gate 與隔離驗證完成 | 新維護結果／M1 快照／M2 報告的 live 驗收；若恢復自動 M3，另需經覆核因果樣本與放行契約 | 目前自動診斷只留假設／查證；模型信心與新計數不解鎖。載入新碼後再收正常樣本 |
| P1 | B03 | **本輪 15/15 逐筆審閱完成** | 使用者對 13 份建議退回、2 份概念保留作決策；確認後正式記錄理由並處置；兩概念另做固定案例驗證 | [完整理由與逐筆 diff](M3_pending_review_2026-11-04.md)／[hash manifest](M3_pending_review_2026-11-04.json)。0 份建議直接批准；佇列未改 |
| P1 | B04 | 原始漏判診斷、警示修正及隔離發布測試完成 | 固定模型的短長文生成與新版 status／history／artifact live 驗收；查核長 context 規則／來源容量 | 不重跑 229 Parts、不把讚語解析成 keep；原兩次 input token=32768 不能單獨證明截斷位置 |
| P1 | B05 | 待品質／效能收尾稽核 | 分別核對 229-Part 文件與數學書；完整性、耗時分位數、HTTP attempts、queue／wait、artifact pending／quarantine；至少 15 Parts 內容抽查 | 優先用已落盤資料，不重 ingestion 取數；缺舊比較資料則標缺口，不把不同題材當 A/B |
| P1 | B06 | 先診斷欠帳，再決定是否實作 | 是否有可處理的 deferred／pending 長期無喚醒；若證實才補持久化恢復／排程 | 依 B05；沿 slot ledger／TTL／ownership／busy lock，不能把 deferred 當永久 skipped |
| P2 | C01 | 手動 rag 傳遞已修、11 個隔離案例完成 | 手動體檢與改善入口的真實來源 baseline 驗收 | 先用載入版本佐證；不重建 embedding，缺來源仍 None／未知 |
| P2 | C02 | 待重稽核 | N.2／SE／grounding／doc-anchored falsifiability 成效報告 | 按 operation、建立時間、backend／版本、active cohort 分層；來源／產出比、固定母體增厚與 decay 分開；依 C01 |
| P2 | C03 | Scout 篩選修復已有，待持續樣本 | 促銷頁／報價頁／付費牆與誤拒短新聞的人工樣本，必要時調整正文抽取 | 保留英文 economy OR finance 與缺正文標記；先有標註再調 300 字門檻 |
| P2 | C04 | **本輪確認 status live 版本資訊**；實作／隔離驗證已完成 | 新 trace、maintenance state 的 runtime／session 一致性 live 驗收；辨識後續批次是否載入 | 觀察的 daemon clean snapshot=`8f5ee47`；啟動後尚無新 run／last_runtime，不能用現在 HEAD 替代載入版本 |
| P2 | C05 | 待標註與數據 | Lens 引文門檻 0.8 的翻譯／原文分組人工作答集與漏報／誤報衡量 | 不為健康燈轉綠直接降門檻 |

## 條件式／低優先工作

| 項目 | 現況 | 重新啟動條件 |
|---|---|---|
| O1–O4 概念本體層 | 暫緩；O0 修圖與 SE 已有 | 證據／來源可靠且有現有 claim 圖不能滿足的具體查詢用例 |
| Planner learning loop | 暫緩 | 先確認實際使用與保留期內的 verdict／人工評價資料；舊 retained run=0 不等於從未使用 |
| 多 server／LLM fan-out | 暫緩 | 相同 request set、零失敗、≥1.5× speedup，加上 trace／ownership／取消／成本契約 |
| ThoughtfulSplitter 新演算法或調參 | 暫緩；既有 P0–P6 已完成 | 人工 boundary／completeness corpus 與使用者可見錯切樣本 |
| M4 新自調旋鈕 | 暫緩；M4 模組已存在 | 每旋鈕有可靠 outcome、min-sample、bounds、rollback／freeze；不採失真的 supports 或混合品質率 |
| 大規模 refactor／全庫 strict／async 化 | 不排主線 | 先有可重現維護或效能痛點，再切小批 |
| R7-C-2／R7-D 架構債、translate_tags retry 一致化 | 待確認、低優先 | 先用當前呼叫點證實仍存在且有影響，不直接搬舊清單 |
| 外部 Anki／概念卡同步 | 暫緩新整合；spaced review 已有 | 使用者具體學習與同步流程需求 |

舊「7 個過期測試／IngestResult／tension renderer 死碼」不列待辦：現況與舊清單不符。
Scout 已否決的跨來源空泛綜合分析亦不恢復。Ollama 服務更新已完成，模型維持原設定。

## 本輪完成與後續順序

本輪完成 B03 可審閱交付：15 份有效 record、15 個基底相符、15 組 edits 精確重建、
2 個競爭組、5 個 Cortex 輸出契約違反；13 份建議退回、2 份僅保留概念，沒有執行決策。
逐檔 proposal 與兩個目標的讀取前後 hash 不變。沒有觸發真模型、重跑 ingestion 或寫入
live vault／DB；文件與 diff 是對既有提案的審閱產物。

另外唯讀確認：daemon status 的 captured_at=`2026-11-04T15:21:49+00:00`、
commit=`8f5ee479acd434ee69d576382e53b3462de221bb`、engine_dirty=false。這是啟動 checkout
觀測，非逐模組 attestation；後來的 B04／B02 提交不能據此認作已載入。
該啟動後尚無新 run，maintenance state 沒有 last_runtime，兩項仍待樣本。

接著優先做可獨立唯讀推進的 B01 歷史處置清單與 B05 品質／欠帳盤點；A01 的獨立
標註複核與 B03 正式決策保留給覆核者／使用者，A03–A05 及新功能 live 驗收持續收正常
樣本。各段依既有授權完成後提交、合併 main 並推送，不因等待樣本停止所有離線工作。

本輪驗證：`make check` exit 0；ruff clean、mypy 174 source files clean，2155 passed、
1 skipped、1 個既有 Chroma warning。`git diff --check` exit 0；manifest、15 份 diff、
anchor／相對連結與來源 hash 已核對。此次交付只有文件與審閱產物，未改 runtime code。
