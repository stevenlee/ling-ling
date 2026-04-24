# 實作計畫：Wiki 定期巡邏精靈 (Health Linter)

這是一個絕佳的點子！知識庫一旦龐大起來，最怕的就是出現**孤兒頁面 (Orphans)**、**死連結 (Broken Links)**、甚至是**邏輯矛盾與概念冗餘 (Redundancies)**。

我們規劃實作一支獨立的腳本 `wiki_linter.py`，作為 LLM Wiki 的「健康檢查醫生」。

## Architecture & Concepts

為了省去不必要的 API/算力消耗，我們將巡邏分成「**物理引擎層**」與「**語意大腦層**」兩個階段進行：

### 階段一：實體連結掃描 (Python Regex 圖論) - 速度極快、幾乎零成本
透過傳統 Python 讀取所有 Markdown，分析出你的知識庫網路結構：
1. **孤兒頁面 (Orphan Pages)**：找出存在於 `pages/` 裡面，卻從來沒有被任何一篇筆記 `[[引用]]` 過的邊緣人。
2. **死連結 (Broken Links)**：找出筆記中提到了 `[[某個概念]]`，但該 Markdown 檔案根本不存在的缺漏點。

### 階段二：大腦語意巡邏 (LLM 邏輯推演) - 深入除錯
1. **目錄冗餘檢查**：讓大腦閱讀整份 `index.md`。例如當下你的系統內其實同時存在了 `[[Agentic Workflows]]` 與 `[[GitHub Agentic Workflows]]`，大腦會主動抓出這類高度重疊的概念，建議你合併。
2. **隨機抽檢/深度矛盾對比**：大腦會抽出標籤高度重合的兩篇筆記（或互連的兩篇筆記），閱讀它們的內文，並揪出「敘述角度/邏輯」是否出現矛盾，或是應該要加上超連結卻漏加的狀況。

## Proposed Changes

### [NEW] `wiki_linter.py`

新增這支腳本，主要包含 `WikiLinter` 類別：
- `scan_graph()`: 負責解析 `[[ ]]` 並回報孤兒與死連結。
- `llm_patrol(llm_wrapper)`: 負責向你的 DGX Server (LLMWrapper) 發出合併與矛盾檢查請求。
- `generate_report()`: 將第一與第二階段的結果，統整成一篇極具美感的 Markdown 報告。

報告的輸出目的地，可以直接導向你剛建好的 `fromLingLing/` 資料夾，命名為 `大腦巡邏報告_YYYY-MM-DD.md`。

## User Review Required / Open Questions

> [!IMPORTANT]
> **排程觸發機制 (Trigger Mechanism)**
> 這支 `wiki_linter.py` 腳本寫好後，你希望它如何被觸發？
> 
> 1. **單純手動執行**：你想要打掃的時候，手動敲擊 `python3 wiki_linter.py`。
> 2. **指令檔觸發 (完美融入剛做的架構)**：如果你在 `toLingLing/` 裡面放入一個檔名為 `健康檢查.md` 的檔案，`auto_ingest.py` 的守護精靈就會攔截它，自動啟動巡邏並回傳報告到 `fromLingLing`！
> 
> 選項 2 聽起來最酷且最連貫，你覺得如何？

## Verification Plan
1. 完成 `wiki_linter.py` 撰寫。
2. （根據觸發機制）執行一次健康檢查。
3. 觀察產出的報告，看它是否能精準抓出目前目錄中 `Agentic Workflows` 重複的嫌疑，以及找出可能的孤兒連結。
