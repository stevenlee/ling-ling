# 實作計畫：檔案型代理介面 (File-based IPC)

此計畫目標是在現有的 `auto_ingest.py` 架構上，平行增加一條「即時問答指令」的工作管線，並確保未來能平滑過渡到 RAG (檢索增強生成)。

## Proposed Changes

### [MODIFY] `auto_ingest.py`

#### 1. 初始化新資料夾
在腳本開頭或 Handler 內加上新的資料夾檢查機制：
- `toLingLing/`：使用者放入提問、指令 Markdown 檔案的入口。
- `fromLingLing/`：Daemon 將結果寫出的出口。
- `raw/prompts/`：處理完成的指令存放區（仿造目前的 Clippings 搬移邏輯，避免重複執行與遺失）。

#### 2. 擴充 `LLMWrapper`
目前 `generate_entity_page` 是強制綁定 JSON 輸出的 Schema 模式。我們需要新增一個通用介面：
```python
def answer_query(self, query_content: str, wiki_context: str) -> str:
    # 這裡將不會限制 JSON 格式，回傳純 Markdown 內容
    # 未來：wiki_context 這個參數可以從只塞 index.md，升級為向量資料庫檢索出來的 Top-K 筆記片段 (RAG)
```

#### 3. 建立 `PromptHandler` (繼承 watchdog)
建立與現有 `ClippingHandler` 完全平行的處理器：
- **觸發**: 監聽 `toLingLing/` 當中的 `.md` 或 `.txt`。
- **處理**: 
  1. 讀取檔案內的指令。
  2. 提取筆記本的長期記憶背景（目前為 `index.md` 加上 Schema 說明），與使用者指令一併送入 `answer_query` 模型。
  3. 取得文字產出後，將結果寫入 `fromLingLing/回應_{原始檔名}`。
  4. 將原本的提問檔搬移至 `raw/prompts/` 作為留存紀錄。

#### 4. 掛載多重監聽 (Observer)
在 `if __name__ == "__main__":` 區塊中：
- 註冊現有的 `path` (`Clippings/`) 給 `ClippingHandler`。
- 註冊新增的 `path_prompt` (`toLingLing/`) 給 `PromptHandler`。
- 讓 watchdog 同時觀察兩個信道。

---

## 放眼未來的設計：如何達成「筆記本即長期記憶」？

在目前的實作中，我們會在 `PromptHandler` 內傳遞 `index.md` 的內容作為給 LLM 的上下文 (Context)。
未來要實作 RAG 時，只需要把 `PromptHandler` 讀取 `index.md` 的那兩行程式碼，替換為：
1. 本地 ChromaDB / FAISS 相似度搜尋。
2. 透過 Obsidian 的 Tag 或雙向連結 (Backlink) 做圖資料檢索。
完全無須改動核心的 `fromLingLing` 與 `toLingLing` 放檔機制。

## User Review Required / Open Questions

> [!IMPORTANT]
> 關於回覆格式的細節討論：
> 1. 您希望輸出的檔案需要帶有 YAML Frontmatter 嗎（例如：`type: chat` 或是包含提問記錄）？還是單純只是 LLM 回覆的 Markdown 純文字即可？
> 2. 將執行完畢的檔案移到 `raw/prompts/` 作為封存，是否符合您目前的整理習慣？

## Verification Plan
1. 修改 `auto_ingest.py` 程式碼。
2. 重啟 `./start.sh`。
3. 建立一個包含 `幫我列出筆記本中所有與 AI 相關的實體名稱` 的檔案到 `toLingLing/` 內。
4. 檢查 `fromLingLing/` 是否迅速彈出對應的回應文件，並查驗 `raw/` 底下是否正確留存檔案。
