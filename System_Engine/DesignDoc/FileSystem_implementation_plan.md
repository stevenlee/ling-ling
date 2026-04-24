# 實作計畫：目錄結構重構與前後台分離 (Refactoring)

完全沒問題！我們要把這個專案從一個「雜亂的腳本資料夾」，正式升級變成一個**「企業級的 Agentic Wiki 系統架構」**。
在這次改版中，我也會按照你的要求，在知識庫內補上 `Notes/` (個人手寫筆記) 的專屬目錄。

## Architecture & Concepts

### 最終藍圖的長相：

```text
llm_wiki/
├── 🎀 lings-desktop/           <-- 唯一用 Obsidian 打開的核心知識庫
│   ├── index.md             (自動產生的全域目錄)
│   ├── log.md               (歷史紀錄)
│   ├── Clippings/           (丟生肉)
│   ├── pages/               (LLM整理好的精美實體知識節點)
│   ├── Notes/               ★ (全新！你個人專用的手寫筆記區)
│   ├── Excalidraw/          (圖表)
│   ├── toLingLing/               (指令引爆與提問區)
│   ├── fromLingLing/             (報告輸出區)
│   └── Commands/            (放使用者自建的指令範本，例如「大腦巡邏.md」)
│
├── ⚙️ System_Engine/        <-- 大腦引擎區 (後台，Obsidian 完全看不見)
│   ├── python_scripts...    (包含 auto_ingest, merge_agent, hn 等)
│   ├── prompts/             (ai-assistant.txt 等系統人格設定)
│   └── DesignDoc/           (你我討論的這份開發架構計畫書)
│
├── 🗄️ Database/             <-- 資料特徵與歷史備份區 (後台)
│   ├── chroma_db/           (RAG 向量庫)
│   └── raw/                 (包含原始 clippings 與 prompts 的冷備份)
│
├── start.sh                 (主啟動腳本，路徑需更新)
├── requirements.txt
└── .env
```

## Proposed Changes

這是一項涉及「全系統路徑相依性」的巨大工程。為了保證運作如常，我需要執行以下搬遷邏輯：

1. **建立新大陸**：自動在根目錄建立 `lings-desktop`, `System_Engine`, `Database` 及其子目錄。
2. **遷移 `auto_ingest.py` 路徑**：因為 Python 腳本會被搬進 `System_Engine/` 中，它找尋 `pages/`、`toLingLing/` 等位置的計算方式必須從 `目前目錄` 變成 `上一層目錄 / lings-desktop / ...`。
3. **遷移 `rag_manager.py` 路徑**：將 `chroma_db` 的儲存路徑重新導向至 `Database/chroma_db`。
4. **遷移 `wiki_linter.py` 與 `merge_agent.py` 路徑**：更新所有的 `index.md` 與 `pages/` 及 `Excalidraw/` 的定址路徑。
   - ***擴充功能***：讓 `WikiLinter` 不只掃描 `pages/`，連你新開放的 `Notes/` 資料夾也一併納入「死連結與孤兒」的維安巡邏範圍！
5. **遷移實體資料夾**：透過 Bash Command 執行 `mv` 搬移檔案。
6. **更新 `start.sh`**：把啟動精靈的進入點改為 `./venv/bin/python System_Engine/auto_ingest.py`。

## User Review Required / Open Questions

> [!WARNING]
> 一旦我開始執行這個腳本，你的資料夾會發生**天翻地覆的板塊位移**。請務必確認：
> 1. 您目前已經在終端機內按下 `Ctrl+C` **終止了目前的背景執行程式**。如果在程式運作時強制搬移檔案，可能會讓 Watchdog 崩潰或產生錯誤！

請問現在背景程式已經被您徹底關閉了嗎？若是，我們隨時可以下達搬移指令！

## Verification Plan
1. 修改所有 Python 檔內的 `project_root` 及相對路徑宣告。
2. 透過 Bash Script，搬移原本的各種資料夾至新位置。
3. 重啟 `./start.sh` 進行測試。能夠正常啟動代表所有路徑修改完成。
