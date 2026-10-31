# A01／A02 隔離真模型評估 — 2026-10-31

## 結論

完整執行 24 個 v2 合成案例，與仍待獨立複核的參考分類一致 **18/24（75%）**；
error=0、missing=0、unsafe_full_support=0。六個完整支持、四個明確反證與六個 neutral
對照皆一致；partial_support 2/6、analogy 0/2。既定 passed 條件為 24/24，本輪 **未通過**。
這是對暫定標註的一致率，不是 live 準確率，也不將尚未獨立核定的標註當成真值。

[可離線重評的完整結果](../tests/fixtures/evidence_evaluation/results/gemma4_26b_2026-10-31.json)
保存 predictions、逐案 verdict／引文／範圍／耗時、參考結果、兩輪 prompt snapshots、
版本 hash 與實際 provider telemetry。資料全部來自本 repo 的合成案例及模型回應，
沒有將私有 vault passage、live evidence 或 trace DB 放進 Git。

## 執行與版本

- 使用目前配置的內網 Ollama，model identifier=`gemma4:26b`；這個 alias 不是權重 digest。
  temperature=0.1、max output=4096，沿 production `LLMClient.judge_evidence_scope`。
- 首輪：台北時間 08:55:40–08:59:26，v1 dataset／原 prompt；只嘗試 3 案，
  6 次 provider attempts 都有 JSON，但全部漏掉必填 contract_version。既有兩次
  content reroll 後記為 3 個 error，達預算停止；21 案 missing。沒有可比較的語義分數。
- 格式修正只在 operation prompt 補明確的六欄 JSON shape 與 integer version，
  parser／引文／schema／apply gate 完全未放寬。prompt 循既有 mtime 熱載入，
  下一次判讀可讀取新版；新增的隔離 runner 不需要插入 daemon 排程。
- 發現 v1 ID／合成 title 帶 analogy、direct 等間接提示，改為中性 case_001／
  Synthetic source 001，原名保留於不匯出的 provenance.case_name。v2 的 claim、
  falsifier、passage、gold label／rationale、tags 經逐案比對全部不變；資料 ID／hash 升版。
- 完整輪：台北時間 08:59:50–09:11:24，v2 dataset＋明確 shape。24 次 LLM calls、
  24 次 provider attempts 全成功；每案 scope_attempt=1，沒有 content 或 transport 重試。
  呼叫延遲總和 693.333 秒；provider usage 回報 prompt 16,373、completion 33,644、
  total 50,017 tokens。含首輪共 30 provider attempts，沒有第三輪追分重跑。
- 兩輪 dataset／prompt 都有變更，因此不是同一組輸入的受控 A/B；本次只確認實際
  格式接縫及這版匿名案例的分類結果，不以首輪失敗宣稱語義準確率改善。

Dataset ID：`a2-evidence-scope-v2`  
Dataset SHA-256：`1721b37303ae54fdd73aae6378719d61c9cf1b07e126390928ae4dc063b8887d`  
Prompt SHA-256：`07c25f4aeadeefbe64aa338c7b328da1ad0a1309fcf8d0d337cfbce52bbe3760`

## 分類結果與分歧

| 參考分類 | 一致／總數 | 其他輸出 |
|---|---:|---|
| full_support | 6/6 | 無 |
| partial_support | 2/6 | neutral 2、contradiction 2 |
| analogy | 0/2 | neutral 2 |
| contradiction | 4/4 | 無 |
| neutral | 6/6 | 無 |

八個歷史過度支持模式的合成變體皆未被判 full_support，且真正支持／反證對照有保留，
不是全 neutral 控制。本小樣本未觀察到過度完整支持，不足以估計 live 錯誤率或宣稱零風險。

| ID | 可讀案例名（不送模型） | 暫定參考 | 模型輸出 |
|---|---|---|---|
| case_001 | legal_plugin_transfer | partial_support | neutral |
| case_002 | risk_model_components | partial_support | contradiction |
| case_004 | political_mechanical_analogy | analogy | neutral |
| case_005 | growth_access_condition | partial_support | neutral |
| case_006 | api_only_component | partial_support | contradiction |
| case_008 | leverage_merger_analogy | analogy | neutral |

兩個 API／Skills 單組件案例使用同一 compound claim，模型分別給 contradiction 與
partial_support，顯示邊界不一致。對未陳述完整機制／未觀察結果的 passage，仍需複核
是否被過度解讀為反證。兩個 analogy 與其餘 partial／neutral 分歧也要先獨立複核 rubric：
一般治理組件、跨域相似或很弱的理論關聯，何時足以算 partial／analogy，需要明確標準。
不因模型選項不同而倒改 gold，不用加長 prompt 或提高信心追到 24/24。

## 隔離與驗收界線

`LLMClient` 新增 keyword-only trace_store 注入，未傳入時保留既有行為。runner 顯式用
run-local trace.sqlite，輸出拒絕位於 live vault 內、拒絕覆寫既有 run。每案先保存
started/error checkpoint；中斷／失敗留在分母，不偽裝 neutral，無自動 resume 或重播。

模型只收到 claim、falsifier、匿名 title 與 passage；case_id 只用於 trace metadata。
不傳 gold／rationale／tags／provenance。model 或 prompt context 與 manifest 不符即記 error。
有 caller cases／error budget，沒有疊加 production retry；daemon busy 或狀態不可讀時在
下一案前停止。狀態檔觀察不是跨程序鎖，也不是對 request 的硬牆鐘取消保證。

本輪沒有建構 live RAG／Chroma、手動觸發 maintenance、改排程時鐘、修改 live evidence／
confidence／S 或變更 apply 開關。A2 state 仍無本輪重啟後的新正常排程樣本，A05 仍待驗收；
A04 的原始來源缺口亦未因此補齊。pipeline 能執行與模型分類通過，是不同的驗收項。

新增 18 個 hermetic 測試，涵蓋不洩漏輸入標註、固定分母、錯誤／中斷 checkpoint、
無額外 retry、busy 停止、重跑目錄拒絕、live vault／trace 拒絕、版本漂移、contra label
映射、client trace 注入與 CLI 中斷狀態。`make check` exit 0：ruff clean、mypy 173 source
files 無錯誤；**2105 passed、1 skipped、1 個既有 Chroma warning**（16.11 秒）。

後續先由獨立審閱者核定 partial／analogy／neutral 的邊界及兩個反證分歧，再決定
是否需調整契約或選擇其他判讀方案；本輪沒有將標註改為 reviewed。來源修復與 A05
正常排程／完整交易仍各有前提，不以 75% 或零 unsafe_full_support 自動放行。
