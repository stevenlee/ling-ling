# A05 新版正常排程觀察（2026-11-02）

本報告僅保存彙總、run ID 與私有報告 hash，不複製查詢、主張或來源正文。
唯讀讀取已落盤的報告、scheduler state 與 trace SQLite；沒有開第二個 Chroma writer、
觸發 maintenance、呼叫模型或改動 live state。模型維持原設定。

## 正常排程樣本

兩份報告均含 v2 支持範圍與來源鏈章節。日期及執行時間採 Asia/Taipei；
時間來自 trace，而非 scheduler 的輪詢／排程時間戳。

| 報告日期 | 實際執行時間 | 薄證據池／掃描 | 檢索事件／原始 top-5 命中 | 判讀／新增 evidence／反證 | 索引追補完成／待辦 |
|---|---|---|---|---|---|
| 11/01 | 00:28:18–00:31:02 | 12／5 | 10／50 | 0／0／0 | 0／0 |
| 11/02 | 00:27:05–00:29:54 | 12／5 | 10／50 | 0／0／0 | 0／0 |

兩個 maintenance run 及 20 個檢索事件皆 succeeded；沒有 LLM call。兩天掃描合計
10 個不同主張。觀察時私有 evidence audit 與 index repair 目錄均無紀錄。
無判讀時沒有 audit 符合預期；零待辦不能證明歷史 Cortex↔RAG 索引全部健康。

| 日期 | Run ID | 私有 EvidenceTrace 報告 SHA-256 |
|---|---|---|
| 11/01 | `run_886bd523af4746ad8ca4471796231144` | `630ea6367c69ac8a2e593a59d3cb120418805fd87aa28d5996e1beecec94a925` |
| 11/02 | `run_fab1a801a08a42e8a48b6ebe731908b9` | `2610a2d58fc36ad64269400e0a244bb4ef5f04f2e148113019aee14fa50353cd` |

## 距離與來源排除

以下是 trace 保存的 top-5 命中，不是全部 RAG 候選，也不是唯一文件數。
BM25-only 的 41 筆皆標記 `passed_layers=[bm25, rerank]`，沒有 vector distance；
facet 的距離不能當解參照後正文的距離。兩者維持不准套用的既有規則。

| 日期 | BM25-only 無 vector distance | facet 距離不可用 | vector > 0.45 | vector ≤ 0.45 |
|---|---|---|---|---|
| 11/01 | 18 | 17 | 13 | 2 |
| 11/02 | 23 | 14 | 8 | 5 |
| 合計 | 41 | 31 | 21 | 7 |

family 合併後，報告的排除數另列如下；不能與原始命中數相加。

| 日期 | 超距離 | 缺可用正文距離 | 自我來源 | 通過送判 |
|---|---|---|---|---|
| 11/01 | 11 | 19 | 1 | 0 |
| 11/02 | 7 | 21 | 4 | 0 |
| 合計 | 18 | 40 | 5 | 0 |

這些樣本不足以證明距離接線故障，也不能推論整個檢索庫皆缺距離。
不為製造補證而調低門檻、重建 embedding 或強迫判讀。

## 驗收結論與剩餘缺口

A05 部分完成：新版正常排程、檢索、負向排除與零判讀報告已觀察。
尚無 live 合格判讀、durable audit、evidence／counterpoint 提交與索引故障恢復樣本；
這些交易已有隔離測試，仍不能冒充 live 成效。A01 的獨立標註複核及 A04 來源資料
缺口仍保留。兩個 trace 的 metadata 只有 task，不能釘住確切載入 commit；
新版報告章節只能佐證功能存在，C04 執行版本可觀測性仍待補。

觀察時 daemon 為 idle；最新 evidence traceback state 為 11/02 ok，摘要與報告的
掃描／判讀／套用計數一致。consolidation 最新為 11/02 succeeded、0 新 insight／claim、
3 quarantined；不據此解除隔離或判定其他維護任務已驗收。
