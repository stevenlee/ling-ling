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

- **Scripture-driven settings**：`Scripture/Scripture.md` 是可熱載入的行為設定，控制 persona、輸出語言、temperature、context window、RAG top-k 以及預設輸出模板 (`use_template`) 等參數。
- **Long-document ingestion pipeline**：長文進入 `Consolidate/` 後，`IngestionPipeline` 會切分文字、生成 Part notes、產生 structured digests、輸出 Stitched Article 與 Synthesis。
- **Source-grounded Parts**：每個 Part 會記錄原文 char/line range，Stitched Article 也會顯示 `Original range`，方便從分析結果回到原始段落。
- **Stitched Article**：`Title (Stitched).md` 保留各 Part 的主要正文，適合完整閱讀與校對。
- **Synthesis Note**：`Title (Synthesis).md` 使用 Part digests 進行總合成，並附上 Part Digest Appendix。
- **Insight mirror reports**：`@ling-insight` 會同時寫入 canonical report 到 `fromLingLing/`，並在 `Insights/` 放一份 byte-identical copy，保留完整 YAML frontmatter 供 Obsidian/Dataview/indexing 使用。
- **LingLens 概念透鏡**：`@ling-lens` 可用概念視角掃描文章，找出不能只靠 Ctrl+F 找到的語意實例；`@ling-count` 保留為 legacy alias。
- **Agentic command workflow**：在 `toLingLing/` 放入 `@ling-*` 指令檔，即可觸發巡邏、修復、洞察、合併、備份、還原等任務。支援 `/template` 指令動態切換輸出格式。
- **Markdown quality checker**：寫入 Obsidian 前會修復常見 Markdown/Mermaid/LaTeX 問題，包含裸 Mermaid block、未關閉 fence、node label quote、body frontmatter 與 `\rightarrow` 這類 carriage-return 轉義錯誤，並在 metadata 留下 `quality_checker` 紀錄。
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
- `@ling-plan`：規劃多步工作流（Pipeline），產出腳本與預覽報告，不立即執行。
- `@ling-do`：執行 `@ling-plan` 產出的工作流腳本。
- `@ling-profiles`：管理文件路由 Profiles。`pending` 看待審草稿、`approve <名稱>` 一鍵生效。
- `@ling-cortex`：對 Cortex 長期記憶層跑三層驗證（紅線/品質/檢索效益），報告含狀態分佈、矛盾對與人工抽查清單。
- `@ling-recall`：回想——給一個主題，撈出 Cortex 中最相關的蒸餾主張（連同信心、可反駁性、反例、證據鏈與矛盾）。與 `@ling` 問答不同：問答從原始筆記答，recall 從**蒸餾過的信念**答。
- `@ling-tensions`：知識張力掃描——攤開 Cortex 裡的矛盾對、教條（高信心低可反駁性）、證據單薄與已被推翻的主張。recall 的反面：不是「我相信什麼」，而是「我的信念在哪裡有問題」，對抗自我印證用。
- `@ling-visualize [[筆記]]`：學習輔助視覺化——判斷內容的認知結構,自動產生最合適的學習產物（比較表 / 流程圖 / 心智圖 / 時間軸 / 象限圖 / 概念圖,沒有強結構就不硬畫）。可用 `as <type>` 指定類型，例如 `@ling-visualize [[X]] as timeline`。
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

## 架構演進

逐項變更紀錄見 [CHANGELOG.md](CHANGELOG.md)。主要里程碑（新到舊）：

- **全模組稽核與硬化**（2026-06）：多代理程式碼稽核 + 逐項驗證修正——資料完整性、reasoning-channel JSON 防禦、watcher 並發、trace 索引。
- **Cortex 長期記憶（Phase 1–4）**：insight 品質訊號 → 夜間鞏固成原子主張 → 雙強度衰減 + 行為訊號 → 保守 falsified 主張帳本。
- **Backlog 批次**：falsifiability 評分穩健化、每週記事、critique retry、Lens 引文驗證、新 Operations（compare/classify/outline/explain）。
- **Profile 路由**：具名 persona+template 配對取代 DocType.md，封閉式選擇 + 審核佇列 + 路由可觀測性。
- **Facet 指標檢索 + 自我進步 bench**：digest 句子作檢索指標、facet A/B lift、評測集隨 vault 自動生長。
- **RAG 品質與成本堆疊**：content-hash skip、持久 embedding cache、MMR、cross-encoder reranker、hybrid BM25+RRF。
- **Capability 層 + PipelineRunner/Planner**：Operations/Skills capability metadata、adapter 註冊表、`@ling-plan` / `@ling-do`。
- **Ingestion pipeline**：長文 map-reduce（Parts → digests → Stitched → Synthesis）、source-grounded char/line range、LingLens evidence grounding。


## 開發檢查

語法與 whitespace 檢查：

```bash
python3 -m compileall -q System_Engine
git diff --check
```

若使用 `venv`：

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

pytest regression suite（需要先安裝 pytest）：

```bash
venv/bin/pip install pytest
PYTHONPATH="$PWD/System_Engine" venv/bin/python -m pytest -q System_Engine/tests
```

日常開發可用較小的 test profile，避免每次都手動挑一長串測試。完整分層見
`System_Engine/DesignDoc/Test_Profiles.md`。例如 planner/executor 相關改動：

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python -m pytest -q \
  System_Engine/tests/test_prompt_watcher.py \
  System_Engine/tests/test_planner_service.py \
  System_Engine/tests/test_plan_readiness.py \
  System_Engine/tests/test_pipeline_runner.py \
  System_Engine/tests/test_planner_agent.py \
  System_Engine/tests/test_executor_agent.py \
  System_Engine/tests/test_insight_agent.py
```

封版或宣告 phase complete 前仍以 full suite 作為 gate。

Markdown/Mermaid smoke test：

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python System_Engine/scratch/test_markdown_quality.py
```

LingLens scratch regression：

```bash
PYTHONPATH="$PWD/System_Engine" venv/bin/python System_Engine/scratch/test_counter_agent.py
```

## License

MIT License Copyright (c) 2026 [MH / Project Ling Ling]

Ling-Ling's Note: "Mom said you can use my code, but don't claim you wrote it yourself. That's plagiarism!"
