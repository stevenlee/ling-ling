# Ling-Ling Testing Strategy

> Current day-to-day test commands live in `System_Engine/DesignDoc/Test_Profiles.md`.
> This file keeps the longer-term quality model and integration ambitions.

# 🛡️ Ling-Ling 系統測試與品質保障方案

為了從目前的「表淺檢查」升級到「深層邏輯驗證」，我們在下個版本將引入以下測試體系。

## 1. 核心組件：Mock LLM 測試環境
目前所有的 Agent 都重度依賴 LLM。為了在不消耗 Token 的情況下測試邏輯，我們將實作 `MockLLMClient`。

### 測試案例範例：
- **邊界測試**：模擬 LLM 回傳空的 Tags 列表，檢查系統是否會崩潰。
- **格式壓力測試**：模擬 LLM 回傳損壞的 YAML 或不完整的 Markdown，測試 `_hybrid_parse` 的容錯能力。
- **異常處理**：模擬 API Timeout 或 429 錯誤，驗證系統的 Retry 機制。

## 2. 輸出品質驗證 (Markdown Schema Validation)
建立一套驗證規則，針對每個 Agent 的輸出檔案進行「內容審計」。

- **YAML 必填項檢查**：所有生成的頁面必須包含 `title`, `type`, `date_created`。
- **WikiLink 有效性**：檢查生成內容中的 `[[...]]` 是否產生了無意義的空連結。
- **Tags 規範化**：確保輸出的標籤已經過 `TagManager` 處理，不存在大寫或空格。

## 3. 集成測試流水線 (Integration Pipeline)
建立一個 `test_vault`（測試用的小型 Wiki 庫），執行以下自動化場景：

1. **Clipping 流水線**：
   - 丟入一個測試用的 `.md` 檔案。
   - 驗證是否正確生成了 `pages/` 下的實體頁面。
   - 驗證 RAG 資料庫中是否確實新增了對應的 Chunks。
2. **Merge 流水線**：
   - 指定兩個測試頁面進行合併。
   - 驗證原始檔案是否被正確刪除，且新檔案內容是否包含來源參考。
3. **Prompt 流水線**：
   - 觸發 `@ling-patrol`。
   - 驗證生成的報告檔案內容是否包含「死連結」或「孤兒頁面」的統計數據。

## 4. 併發與狀態檢查 (Concurrency Safety)
針對 `global_busy_state` 進行壓力測試：
- 同時丟入 5 個指令，驗證系統是否能正確執行「排隊」或「拒絕（Busy）」，避免多個 Agent 同時寫入同一個檔案。

---
> [!NOTE]
> 這些深層檢查將整合進 `./start.sh --check --deep` 指令中。
