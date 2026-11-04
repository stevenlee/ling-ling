# B04 Synthesis critique 判定與發布狀態查核

## 診斷結論

229-Part 文件的兩次原始 critique 都沒有明確 verdict。以目前 `parse_verdict` 重播，
皆為 None，符合既有契約：一般總結、讚語或後續建議不能替代 keep／revise／reject。
本次不放寬 parser、不更換模型、不修改 critique prompt 或擴增 retry budget。

唯讀查詢私有 trace SQLite（mode=ro），沒有實例化 live TraceStore 或第二個 Chroma
writer，也沒有重跑原始文件。公開交接僅留 ID、hash、計數與格式分類，不複製正文。

| 項目 | 第一次 critique | 第二次 critique |
|---|---|---|
| trace_id | `llm_121038abf41143099efafe8eb6c4fdd4` | `llm_6b86c8cf14ae46b8b56689672946206e` |
| 時間（Asia/Taipei） | 10/30 00:13:48 | 10/30 00:16:11 |
| provider／model | ollama／gemma4:26b | ollama／gemma4:26b |
| 輸出字元數 | 1818 | 1780 |
| 結尾形狀 | 總結建議／後續改善方向 | 總結／正向評語與延伸建議 |
| prompt 已含必要 verdict 規則 | 是 | 是 |
| token 記錄：input／output | 32768／1167 | 32768／1099 |
| parser 重播 | None | None |

response SHA-256（亦與原 trace response_hash 一致）：

- 第一次：`2400fbeeeaed15837bc6b9122a1dfeaa1f43c207e1dbcb5cad177e60073eab43`
- 第二次：`f436c55d920698234d0471f736372b7db33049baef986b81fe6164249aa09a01`

兩次 input token 皆為 32768，與已知服務 context 設定相同，是來源／規則可能受 context
容量影響的線索；沒有當時伺服器截斷位置的紀錄，不能據此斷言哪部分被丟棄或確定漏判
原因。trace 的 transport succeeded 不表示 critique 格式或來源判讀正確。

## 最小修正

`_write_synthesis` 對 unparseable 原本沿用 revise／reject 的「審查發現未解決的缺陷」
警示，會把缺判定的讚語也呈現成負面品質判斷。改為：

> 品質審查尚未取得明確判定，本文尚未通過審查；請對照來源與文末審查人工核對後再引用。

保留 NeedsReview／quality_verdict=unparseable、原 critique、實際 findings 與 retry
歷史。明示 revise／reject 仍顯示缺陷警示；error 仍顯示審查未完成；keep 才升級。
沒有自動改寫歷史 synthesis、索引或 evidence，原 229-Part 文件仍待人工審查。

## 隔離驗證與剩餘驗收

新增 5 個 hermetic 案例：

- 兩種缺 verdict 的合成摘要／正向評語：parser 仍為 None，頁首不捏造缺陷判斷。
- 兩個合成 Part 的真實 critique loop → 頁面寫入 → artifact trace → FakeRAG 邊界：
  分別驗 missing→missing、missing→keep、missing→transport error。
  預算仍是兩次 synthesis；選中候選、status、verdict/status/error history 與 artifact
  metadata 一致，只有明確 keep 升級，斷線保留原候選及錯誤歷史。

修前 2 failed／3 passed；修後 critique 測試 54 passed。`make check` exit 0：ruff clean、
mypy 174 source files clean，2133 passed、1 skipped、1 個既有 Chroma warning；
`git diff --check` exit 0。所有新增頁面與 trace 寫入均在 tmp_path，沒有真模型呼叫。

B04 的原始回應診斷、呈現修正與隔離狀態契約已驗；固定模型的短長文真實生成及新版
live status／history／artifact 仍待樣本，不能用 FakeLLM 測試代替模型語義品質驗收。
若後續查證長 context 漏規則，應另設有來源覆蓋證明的輸入預算方案；本段未截取來源
來換取合格 verdict，也未把上下文限制隱藏成 parser 成功。
