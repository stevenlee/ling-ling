# Ling-Ling Mentor / 玲玲小老師

Ling-Ling 是一個 file-based agent。你用 Obsidian 管理資料，Ling-Ling 監聽資料夾、讀寫 Markdown、呼叫本地或遠端 LLM，並把整理後的知識重新寫回 vault。

她的核心目標是把「閱讀、整理、查詢、洞察」變成可追蹤的檔案流程：人負責挑選材料與判斷，AI 負責清洗、拆解、交叉比對與產出初稿。

## 解決的問題

- **注意力不足**：長文可以先拆成 Parts，再產出忠實接合版與洞察型總結。
- **AI 難以精準駕馭**：角色、語言、創造力、記憶長度與搜尋深度集中在 `Scripture/Scripture.md`。
- **人機共筆麻煩**：把檔案丟進 `Consolidate/` 或 `toLingLing/`，daemon 會自動處理並歸檔。
- **知識管理困難**：本地 ChromaDB 與 Markdown vault 同步，支援 RAG 問答與語意搜尋。
- **保密與可控**：可接 Ollama、vLLM 或 Gemini；資料與 Obsidian vault 保留在本機專案內。
- **想像力素材不足**：`@ling-insight` 會從既有筆記生成跨領域洞察。

## 核心功能

- **Scripture-driven settings**：`Scripture/Scripture.md` 是可熱載入的行為設定，控制 persona、輸出語言、temperature、context window、RAG top-k 等參數。
- **Long-document ingestion pipeline**：長文進入 `Consolidate/` 後，`IngestionPipeline` 會切分文字、生成 Part notes、產生 structured digests、輸出 Stitched Article 與 Synthesis。
- **Source-grounded Parts**：每個 Part 會記錄原文 char/line range，Stitched Article 也會顯示 `Original range`，方便從分析結果回到原始段落。
- **Stitched Article**：`Title (Stitched).md` 保留各 Part 的主要正文，適合完整閱讀與校對。
- **Synthesis Note**：`Title (Synthesis).md` 使用 Part digests 進行總合成，並附上 Part Digest Appendix。
- **LingLens 概念透鏡**：`@ling-lens` 可用概念視角掃描文章，找出不能只靠 Ctrl+F 找到的語意實例；`@ling-count` 保留為 legacy alias。
- **Agentic command workflow**：在 `toLingLing/` 放入 `@ling-*` 指令檔，即可觸發巡邏、修復、洞察、合併、備份、還原等任務。
- **Markdown quality checker**：寫入 Obsidian 前會修復常見 Markdown/ Mermaid 問題，並在 metadata 留下 `quality_checker` 紀錄。
- **Knowledge dashboard**：`index.md` 會自動更新，支援自然排序與 Obsidian callouts。

## 快速開始

### 1. 必要條件

- Python 3.10+
- Obsidian
- 一個 LLM provider：Ollama、vLLM 或 Gemini API

### 2. 安裝

```bash
git clone <repo-url>
cd ling-ling
cp .env.example .env
vim .env
```

`.env` 主要設定：

- `LLM_PROVIDER=ollama|vllm|gemini`
- `OLLAMA_API_BASE` / `OLLAMA_MODEL`
- `VLLM_API_BASE` / `VLLM_MODEL`
- `GEMINI_API_KEY` / `GEMINI_MODEL`
- `CHUNK_SIZE` / `CHUNK_OVERLAP`

### 3. 啟動 daemon

```bash
./start.sh
```

啟動腳本會在第一次執行時建立 `venv` 並安裝 `requirements.txt`。

健康檢查：

```bash
./start.sh --check
```

## 使用方式

### 整理長文

1. 把 Markdown 放進 `lings-desktop/Consolidate/`。
2. Ling-Ling 會產生：
   - `pages/<Title>/<Title> (Part N).md`
   - `pages/<Title>/<Title> (Stitched).md`
   - `pages/<Title>/<Title> (Synthesis).md`
3. 原始檔會移到 `raw/consolidate/`。

如果 Markdown 有 sidecar images，目錄格式為：

```text
Consolidate/
├── Article.md
└── images/
    └── Article/
        └── page_1.jpeg
```

處理後 sidecar images 會一起歸檔到 `raw/consolidate/images/`。

### 下指令給 Ling-Ling

在 `lings-desktop/toLingLing/` 建立 Markdown 指令檔，例如：

```markdown
@ling-lens [[西遊記]]
Count: 幫助孫悟空的人、神、妖怪
Confidence: medium
```

完成後：

- 回覆會寫到 `fromLingLing/`
- 原始指令會移到 `raw/prompts/`

## 指令一覽

- `@ling-lens`：概念透鏡。掃描文章中的語意實例，輸出 evidence、confidence、分析錨點與原文範圍。
- `@ling-count`：`@ling-lens` 的舊別名，仍可使用。
- `@ling-patrol`：全庫健康檢查，找出死連結、孤兒頁面與資料庫不同步。
- `@ling-patrol-tags`：標籤巡邏，找出未翻譯或格式錯誤的標籤。
- `@ling-repair-tags`：根據巡邏清單批量修復標籤。
- `@ling-repair-db`：資料庫修復與重新同步。
- `@ling-insight`：從既有筆記生成跨領域洞察。
- `@ling-merge`：合併筆記。內容包含 `[[A]]` 與 `[[B]]` 即可融合兩篇筆記。
- `@ling-zip` / `@ling-unzip`：備份與還原知識庫。
- `@ling-RESET`：清除內容前會先強制備份。

## 專案結構

```text
ling-ling/
├── Backups/                    # @ling-zip 與 RESET 前備份
├── System_Engine/              # Python daemon 與 agent 程式
│   ├── agents/                 # LingLens、Insight、Merge、Patrol 等 agents
│   ├── core/                   # config、parser、state、ui、tag manager
│   ├── maintenance/            # health check、DB repair、release helper
│   ├── services/               # LLM client、RAG、media、ingestion pipeline
│   ├── tests/                  # pytest-style unit tests
│   └── watchers/               # filesystem watchers
├── lings-desktop/              # Obsidian vault
├── requirements.txt
└── start.sh
```

```text
lings-desktop/
├── Assets/                     # 圖片與附件
├── Clippings/                  # 外部剪報暫存
├── Consolidate/                # 待整理資料入口
├── Database/                   # ChromaDB
├── fromLingLing/               # agent 產出
├── Insights/                   # insight 報告
├── Notes/                      # 人類筆記
├── pages/                      # AI 生成 wiki pages
├── raw/                        # 已處理原始檔與指令歸檔
├── Scripture/                  # persona、語言、系統設定
├── Skills/                     # 分析方法包
├── Templates/                  # 輸出模板與 agent prompts
├── toLingLing/                 # 指令入口
├── toranomaki/                 # @ling-* 指令範例
└── index.md                    # 自動維護的知識地圖
```

## Refactor Notes

### 2026-05-15 Ingestion Pipeline Refactor

- `ClippingWatcher` 現在只負責 filesystem events、busy state、檔案類型分流與歸檔。
- Markdown ingestion 主流程移到 `services/ingestion_pipeline.py`。
- 長文切分改用 `TextSplitter.split_text_with_spans()`，保留每個 chunk 對應的原文 char/line range。
- Part metadata 會寫入 `source_start_char`、`source_end_char`、`source_start_line`、`source_end_line`。
- Synthesis metadata 會寫入 `part_source_map`。
- Stitched Article 會在每個 Part 前顯示原文範圍，供 LingLens evidence link 使用。
- `extract_json_array()` 與 `extract_json_object()` 移到 `core.parser`，讓 LLM JSON parsing 共用。
- digest value formatting 移到 `core.utils.digest_value_to_text()`。
- `PromptWatcher` 改用 declarative intent routing table，`@ling-lens` 與 `/lens` 成為正式入口。
- `VaultWatcher` 復用 main process 的 `LLMClient` 翻譯新標籤，避免每次手動改檔都建立新的 client。

### 2026-05-13 LingLens Evidence Grounding

- 新增 `@ling-lens` / `/lens` 概念透鏡指令。
- `@ling-count` / `/count` 保留為 legacy alias。
- 多文章與多概念會輸出 cross-analysis matrix。
- evidence 會盡量連回 Stitched Part anchor 與原始檔。
- 若 Part metadata 有原文範圍，報告會顯示原文 line range。

### 2026-05-10 Synthesis Quality Upgrade

- 長文解析由 Part 第一行摘要升級為 structured digest synthesis。
- 每個 Part 會整理 `thesis`、`key_points`、`evidence`、`terms`、`open_questions`、`handoff`。
- Synthesis 會附上 Part Digest Appendix，方便檢查合成依據。
- Markdown quality checker 會修復裸 Mermaid、未關閉 Mermaid fence、body YAML frontmatter 等常見問題。

## 開發檢查

語法檢查：

```bash
venv/bin/python -m py_compile \
  System_Engine/services/ingestion_pipeline.py \
  System_Engine/watchers/clipping_watcher.py \
  System_Engine/agents/counter_agent.py \
  System_Engine/services/llm_client.py \
  System_Engine/watchers/prompt_watcher.py \
  System_Engine/watchers/vault_watcher.py \
  System_Engine/core/parser.py \
  System_Engine/core/utils.py \
  System_Engine/services/text_splitter.py
```

LingLens scratch regression：

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python System_Engine/scratch/test_counter_agent.py
```

## License

MIT License Copyright (c) 2026 [MH / Project Ling Ling]

Ling-Ling's Note: "Mom said you can use my code, but don't claim you wrote it yourself. That's plagiarism!"
