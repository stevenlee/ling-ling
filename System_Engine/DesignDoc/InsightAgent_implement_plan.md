# 實作計畫：大腦洞察代理人 (Insight Agent)

目前系統處於「被動攝入與檢索」模式。為了讓 Wiki 進化為「主動思考」的數位大腦，我們將實作 `InsightAgent`。它能根據預設策略或使用者指令，從 ChromaDB 中提取碎片並合成跨領域的深度洞察文件。

## Architecture & Concepts

### 1. 洞察合成引擎 (`InsightAgent`)
建立獨立模組 `System_Engine/insight_agent.py`，負責：
- **策略配置化 (JSON Mode)**：策略邏輯與 Prompts 完全抽離，存放於 `System_Engine/Strategies/` 目錄下。
- **多維度合成**：支援單一策略分析 (`generate_insight`) 與全方位掃描分析 (`generate_full_insight`)。

### 2. 雙模態啟動機制
- **指令驅動 (Manual)**：攔截 `@ling-insight.md` 指令。
    - 使用 `/strategy-` 前綴來精確打擊特定維度（如 `/strategy-meta`）。
    - 若無前綴，則預設執行「全方位掃描報告」。
- **排程守護 (Scheduled)**：每日凌晨 **02:00 ~ 07:00** 定時產出「全方位智慧洞察深潛報告」。

### 3. 環境感知與保護 (Busy-Aware)
- **旗標檢查**：增加 `is_busy` 全域旗標。當有剪報正在攝入或正在回答問題時，排程作業會自動避開，防止運算資源衝突。

## Proposed Changes

### [NEW] `System_Engine/Strategies/` (目錄)
存放所有的策略設定檔（`recency.json`, `islands.json`, `tag_cluster.json`, `meta_methods.json`）。

### [NEW] `System_Engine/insight_agent.py`
實作動態載入 JSON 策略與跨維度聚合報告的核心邏輯。

### [MODIFY] `System_Engine/rag_manager.py`
- 修改 `add_document`：在元數據（Metadata）中增加 `timestamp` 與 `tags`。

### [MODIFY] `System_Engine/auto_ingest.py`
- **整合 `InsightScheduler`**：改為產出全方位合成報告。
- **更新 `PromptHandler`**：新增 `/strategy_` 前綴識別，若無匹配則執行全方位分析。

### [NEW] [Insight目錄](file:///Users/stevenlee/projects/llm_wiki/lings-desktop/Insights/)
命名規範：
- 單一策略：`insight-YYYYMMDD-HHMMSS.md`
- 全方位報告：`full-insight-YYYYMMDD-HHMMSS.md`

## User Review Required / Open Questions

> [!IMPORTANT]
> **關於資料庫元數據補全**
> 為了讓「最近更新」策略生效，強烈建議執行 `init_rag.py` 重新索引舊資料。

## Verification Plan

1. **手動測試 (單一)**：建立 `@ling-insight.md` 包含 `/strategy_meta`，確認產出單維度報告。
2. **手動測試 (全案)**：建立空的 `@ling-insight.md`，確認產出 `full-insight` 報告。
3. **排程測試**：確認凌晨自動執行的是包含所有維度的全方位報告。
