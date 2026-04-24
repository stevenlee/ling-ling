# 實作計畫：lings-desktop 檔案變更主動同步 (Vault Sync Agent)

目前系統僅在「剪報建立」或「提問回答」時執行增量索引，但不會感應到使用者手動刪除或修改筆記的行為。這會導致 ChromaDB 出現已經消失的「幻覺記憶」。我們將建立一個 `VaultHandler` 來建立完整的檔案系統同步閉環。

## Architecture & Concepts

### 1. 全方位監聽 (Full Vault Watch)
我們將新增一個 `VaultHandler` 類別，專門負責監聽 `lings-desktop/pages` 與 `lings-desktop/Notes` 這兩個核心知識產出區。

### 2. 聯動邏輯
*   **刪除事件 (`on_deleted`)**: 
    - 當偵測到 `.md` 檔案消失，立即調用 `rag.delete_document(title)` 從資料庫抹除對應的語意向量。
*   **修改事件 (`on_modified`)**: 
    - 當偵測到使用者手動修改了 Obsidian 中的筆記，立即重新讀取內容並執行 `rag.add_document()`。由於我們使用 `upsert` 機制，新的內容會自動覆蓋舊的向量碎片。

## Proposed Changes

### [MODIFY] `System_Engine/auto_ingest.py`

#### [NEW] `VaultHandler(watchdog.events.FileSystemEventHandler)` 類別
```python
class VaultHandler(watchdog.events.FileSystemEventHandler):
    def __init__(self, rag):
        self.rag = rag
    
    def on_deleted(self, event):
        # 排除目錄及非 md 檔案
        # 提取檔名作為 Title
        # 執行 rag.delete_document(title)
        
    def on_modified(self, event):
        # 排除目錄及非 md 檔案
        # 讀取檔案
        # 執行 rag.add_document()
```

#### 修改 `__main__` 啟動邏輯
*   將 `pages_path` 與 `notes_path` 也納入 `observer.schedule` 的監管範圍。

## User Review Required / Open Questions

> [!IMPORTANT]
> **關於「重新命名」的處理**
> Watchdog 的「重新命名」通常會發送一個 `on_moved` 事件。目前計畫會先簡單處理：將重新命名視為「刪除舊的 + 建立新的」。
> 您是否常在 Obsidian 中大規模批次重命名檔案？若是，我們可能需要加入更有彈性的「防抖 (Debounce)」機制。

## Verification Plan

1. **刪除測試**：手動在 Obsidian 刪除一篇測試筆記，觀察終端機是否跳出 `Deleted '...' from RAG DB`。
2. **修改測試**：修改現有筆記內容，觀察是否觸發 `Added '...' to RAG DB` 重新索引。
3. **穩定性測試**：重啟 `./start.sh` 確保所有 Patch 正常運作。
