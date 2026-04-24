# 實作計畫：自動合併代理引擎 (Merge Agent)

這將會是讓 LLM Wiki 做到「自我進化與收斂」的關鍵功能。我們將設計一個 Merge Agent，負責自動讀取被點名的多篇筆記，交由大腦融合成全新筆記，並無縫清理舊有資料 (包含實體檔案、全域索引、以及 RAG 向量特徵)。

## Architecture & Concepts

### 1. 觸發條件 (Interceptor)
在 `toLingLing/` 進行自然語言發問時，一旦我們偵測到內文包含關鍵字「合併」，並且有明確的 `[[雙向實體連結]]`（例如：`請幫我合併 [[A]] 與 [[B]]`），精靈就會暫停一般的問答模式，轉而將指令移交給 Merge Agent。

### 2. 破壞與重建機制 (Destruction & Creation)
這是最為精密的一環，必須確保資料不遺失且索引不殘留：
1. **讀取 (Read)**: Merge Agent 檢查並抓取所有目標 `.md` 的內容。
2. **合成 (Synthesize)**: 將舊內容連接在一起，重新派發給現有的 `LLMWrapper.generate_entity_page`。由於我們先前已經把 System Prompt 寫得很強大，它能自動抽取出全新結構化的 JSON (Title, Tags, Content)。
3. **佈建 (Deploy)**: 建立合成後的新 Markdown 筆記。
4. **抹除 (Erase)**:
   - 刪除舊的 `.md` 實體檔案。
   - 將舊檔案從 `index.md` 目錄中根除。
   - 從 ChromaDB (`rag_manager`) 中徹底刪除舊的高維度向量特徵 (`delete_document`)。
5. **註冊 (Register)**: 將全新產出的文章寫入 `index.md` 與 ChromaDB 中。

## Proposed Changes

### [MODIFY] `rag_manager.py`
為因應抹除機制，我們必須在 RAG 核心中加入向量清除能力：
```python
def delete_document(self, title: str):
    # 利用 where={"title": title} 從 ChromaDB 徹底刪除指定文件的記憶 chunk
```

### [NEW] `merge_agent.py`
獨立的類別模組，負責封裝上述的檔案讀寫、合併、索引與後處理機制。執行完畢後會回傳一份詳細的 `合併手術報告` String。

### [MODIFY] `auto_ingest.py`
在 `PromptHandler.process_prompt` 內新增一條判定邏輯分支。利用 Regex 萃取使用者指令中的所有 `[[ ]]` 標籤，若超過 2 個且含有「合併」字眼，則呼叫 `MergeAgent`：
```python
# Pseudo Code
target_entities = re.findall(r'\[\[(.*?)\]\]', query_content)
if "合併" in query_content and len(target_entities) >= 2:
    report = merge_agent.execute(target_entities)
    # output report to fromLingLing/
```

## User Review Required / Open Questions

> [!WARNING]
> **舊連結斷鏈風險提示**
> 合併文章後（例如 A 與 B 合併為 C），如果過去有「其他筆記」引用了 `[[A]]`，那這些舊筆記內就會產生一個「死連結」。
> 目前這個版本先不會去全面盤查並修改所有的舊筆記內文（避免風險過高）。也就是說，如果發生這種情況，之後的 `wiki_linter.py` 巡邏時就會幫你抓出來那些產生死連結的舊筆記了，我們就仰賴 Linter 巡邏機制來輔助抓漏。
> 您同意這個折衷的漸進式設計嗎？

## Verification Plan
1. 完成 `rag_manager.py` 與 `merge_agent.py` 修改。
2. 重啟背景精靈 `./start.sh`。
3. 建立一份 `合併測試.md` 放入 `toLingLing/`，內容寫上：「請合併 `[[Agentic Workflows]]` 和 `[[GitHub Agentic Workflows 安全架構]]`」。
4. 觀察這兩個檔案是否確實從 `pages/` 中消失，確認 `index.md` 被更新，並產出一篇全新的、結構完美的綜合版 `Agentic Workflows` 筆記。
