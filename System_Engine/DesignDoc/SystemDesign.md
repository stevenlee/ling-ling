# 🗺️ Autonomous LLM Wiki - 系統設計文件 (System Design)

此文件詳細記錄了 LLM Wiki 的核心架構、組件分工、資料流向以及四大自動化代理機制。本系統旨在建立一個具備「長期記憶」與「自我癒合」能力的自主型個人知識庫。

---

## 🏗️ 1. 系統架構概念 (High-Level Architecture)

系統採用 **「檔案型進程間通訊 (File-based IPC)」** 架構。使用者只需與 Markdown 檔案互動，背景精靈 (Auto-Ingest Daemon) 會透過檔案變更事件 (Watchdog) 驅動所有後台動作。

- **前端介面**: Obsidian (基於 `lings-desktop/` 目錄)
- **後端引擎**: Python 異步守護程序 (`System_Engine/`)
- **推理中心**: 
    - 遠端推論: DGX Spark (vLLM) 或 Google Gemini API
    - 本地推論: Ollama (Gemma2/Gemma4)
- **記憶中樞**: ChromaDB 本地向量資料庫 (`Database/`)

---

## 📂 2. 目錄結構分析 (Directory Structure)

重構後採用「前後台分離」體系：

- **`🎀 lings-desktop/`**: 知識庫前台。
    - `pages/`: AI 自動生成的實體頁面。
    - `Notes/`: 使用者個人手寫筆記。
    - `Clippings/`: 待處理的剪報「生肉」區。
    - `Insights/`: [NEW] 存放置自動化洞察報告。
    - `toLingLing/` & `fromLingLing/`: 人機互動指令發送與接收區。
- **`⚙️ System_Engine/`**: 後台引擎。
    - `auto_ingest.py`: 主管全局的 Daemon 管線。
    - `insight_agent.py`: [NEW] 主動思考與合成代理。
    - `Strategies/`: [NEW] 存放 JSON 格式的洞察策略設定。
    - `rag_manager.py`: 負責 RAG 檢索與 Metadata 管理。
    - `wiki_linter.py`: 健康檢查診斷程式。
    - `merge_agent.py`: 負責執行文章重組與合併。
- **`🗄️ Database/`**: 冷資料與記憶。
    - `chroma_db/`: 存放載有 `timestamp` 與 `tags` 的向量記憶。

---

## 🔄 3. 核心資料流 (Data Pipelines)

### A. 知識攝入管線 (Ingestion Pipeline)
1. **Source**: 使用者將 Markdown 丟入 `Clippings/`。
2. **Analysis**: LLM 根據環境變數中的 `AGENT_ROLE` 進行語意分析。
3. **Memory Registration**: 內容被 Chunked 並帶領時間戳記與標籤寫入 ChromaDB。

### B. 主動合成管線 (Insight Synthesis Pipeline)
1. **Trigger**: 定期排程 (02:00-07:00) 或手動指令 `/strategy-`。
2. **Sampling**: 從資料庫進行「隨機抖動抽樣 (Jitter Sampling)」，確保洞察多樣性。
3. **Synthesis**: LLM 根據指定策略（新聞風、偵探風、哲學風）合成跨領域報告。
4. **Delivery**: 在 `lings-desktop/Insights/` 產出 Markdown 文件。

---

## 🤖 4. 四大自動化代理程式 (Automation Agents)

### 🩺 1. Wiki 健康巡邏精靈 (Wiki Linter)
*   **任務**: 找出死連結 (Broken Links)、孤兒頁面 (Orphans) 與語意冗餘。

### 🔪 2. 自動合併手術代理 (Merge Agent)
*   **任務**: 執行文章深度融合、清理過時專體並更新索引。

### 📰 3. 資訊採集員 (HN Scraper)
*   **任務**: 定期抓取 Hacker News 趨勢並轉化為內部待讀列表。

### 🎀 4. 大腦洞察代理人 (Insight Agent) [NEW]
*   **任務**: 
    *   **排程反思**: 凌晨自動進行全方位知識掃描。
    *   **策略演化**: 透過掛載不同的 JSON 策略，實現「尋找孤島」、「方法論挖掘」等進階思考。

---

## 🔒 5. 安全與指令機制

系統設有嚴格的指令前綴攔截：
- **`/strategy-[ID]`**: 觸發特定的洞察策略。
- **`/patrol`**: 啟動全庫掃描。
- **Busy-State Protection**: 系統在執行 Ingestion 時會自動暫停排程作業，確保資源不衝突。

---

## 🛠️ 6. 技術棧 (Tech Stack)

- **Vector DB**: ChromaDB (本地 Embedding 模型)。
- **LLM Provider**: vLLM, Gemini API, **Ollama (Local)**。
- **Config**: python-dotenv (管理 .env)。
- **Frontend**: Obsidian (Markdown + Mermaid + Excalidraw)。

---
*文件更新日期: 2026-04-14*
