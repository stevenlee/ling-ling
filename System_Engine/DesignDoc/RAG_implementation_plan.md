# 實作計畫：RAG 記憶檢索擴充 (Long-term Memory)

此計畫目標是為 LLM Wiki 導入**檢索增強生成 (Retrieval-Augmented Generation, RAG)**，讓 `toLingLing` 管線在回答問題時，能精準搜尋並引用 `pages/` 裡面無數篇過去產出的筆記。

## Architecture & Concepts

為了保持專案輕量且無需額外部署，我們將採用 **ChromaDB** 作為本地向量資料庫，它會像 SQLite 一樣將向量資料直接儲存在專案內的 `chroma_db/` 資料夾中。

新 RAG 系統的運作將分為三個支柱：
1. **Embedding (向量化引擎)**：將文字轉化為高維度向量，讓系統可以計算問題與筆記之間的「語義相似度」。
2. **Indexing (索引更新)**：當有全新剪報被轉化為 Wiki 頁面時，同步將它切塊並存入資料庫。
3. **Retrieval (檢索應用)**：在 `toLingLing` 提問時，於背景根據問題查出 Top-3 高相關性的筆記，再餵給 LLM 作答。

## Proposed Changes

### [NEW] `rag_manager.py` (或是直接寫進 `auto_ingest.py`)
為保持 `auto_ingest.py` 乾淨，建議將 RAG 的初始化與查詢邏輯模組化。
- **功能**: 管理 ChromaDB 的 Collection 連線。
- **功能**: 把 Markdown 文章切分成 Chunk (例如以 `\n##` 或段落切分)，以保留語義完整性。
- **功能**: 提供 `query_similar_notes(query_text, top_k=3)` 介面。

### [NEW] `init_rag.py` (一次性腳本)
- 用於載入目前 `/pages/` 底下已經存在的所有舊筆記，這是讓大腦擁有「過去記憶」的第一步。

### [MODIFY] `auto_ingest.py`

#### 1. 綁定 `ClippingHandler`
在原本將產出的 Markdown 寫入 `pages/` 後，呼叫 `rag_manager.add_document()`，這樣系統只要有新知識，大腦也會立刻學會。

#### 2. 更新 `PromptHandler`
修改這一段暫時的邏輯：
```python
# 舊邏輯 (只抓 index 目錄)
wiki_context = self.index_file.read_text('utf-8') ...

# 新邏輯 (動態檢索)
relevant_chunks = rag_manager.query_similar_notes(query_content)
wiki_context = "\n---\n".join([chunk.text for chunk in relevant_chunks])
```

## User Review Required / Open Questions

> [!IMPORTANT]
> **選定你的 Embedding (向量化) 引擎**
> RAG 需要將文字轉為數字。目前你有兩種最無痛的選擇，請告訴我你偏好哪一種：
> 1. **Gemini API (`text-embedding-004`)**: 最簡單、速度極快、多語言支援棒，而且對程式碼改動最小 (你本來就有這個系統的 API Key，且它超級便宜)。
> 2. **完全本地端 (`sentence-transformers` 等開源模型)**: 隱私度最高。但需要下載 PyTorch (約 1-2GB) 以及載入模型權重，如果你的機器不是常駐記憶體很大的話，容易在查詢時造成延遲。

> [!TIP]
> 為了不干擾主程式，我們預計將會新增 `chromadb` 這支輕量級套件到你的 `requirements.txt`。

## Verification Plan
1. 修改並套用更新後程式碼，執行 `init_rag.py` 讓大腦閱讀舊筆記。
2. 啟動 `./start.sh`。
3. 在 `toLingLing/` 內提問一個「非常針對你先前的某些筆記細節」的問題（例如 Agentic Workflows 的特定功能）。
4. 觀察產出的 `fromLingLing` 回應，必須精巧地參照了該筆記的內容。
