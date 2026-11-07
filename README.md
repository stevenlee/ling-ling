# Ling-Ling Mentor / 玲玲小老師

Ling-Ling 是一個 file-based agent。你用 Obsidian 管理資料，Ling-Ling 監聽資料夾、讀寫 Markdown、呼叫本地或遠端 LLM，並把整理後的知識重新寫回 vault。

她的核心目標是把「閱讀、整理、查詢、洞察」變成可追蹤的檔案流程：人負責挑選材料與判斷，AI 負責清洗、拆解、交叉比對與產出初稿。

> 📐 想了解系統**怎麼運作**（架構、資料流、設計取捨）？見 [`System_Engine/DesignDoc/SystemDesign.md`](System_Engine/DesignDoc/SystemDesign.md)。若已在 Obsidian 裡，請從 [Vault 使用入口](lings-desktop/README.md) 查找工作目錄與指令範例。

## 目錄

- [解決的問題](#解決的問題)
- [核心功能](#核心功能)
- [快速開始](#快速開始)
- [日常維護與排查](#日常維護與排查)
- [使用方式](#使用方式)
- [指令一覽](#指令一覽)
- [TUI cockpit（終端機操作台）](#tui-cockpit終端機操作台)
- [專案結構](#專案結構)
- [架構演進](#架構演進)
- [開發檢查](#開發檢查)
- [License](#license)

## 解決的問題

- **注意力不足**：長文可以先拆成 Parts，再產出忠實接合版與洞察型總結。
- **AI 難以精準駕馭**：角色、語言、創造力、記憶長度與搜尋深度集中在 `Scripture/Scripture.md`。
- **人機共筆麻煩**：把檔案丟進 `Consolidate/` 或 `toLingLing/`，daemon 會自動處理並歸檔。
- **知識管理困難**：本地 ChromaDB 與 Markdown vault 同步，支援 RAG 問答與語意搜尋。
- **保密與可控**：Vault 與索引儲存在本機。依 LLM 與 embedding 設定，指令或筆記片段可能送往所選服務。
- **想像力素材不足**：`@ling-insight` 會從既有筆記生成跨領域洞察。
- **夜間思考被忙碌打斷**：若 1–5am「做夢窗口」系統正忙，當天的深層思考（洞察生成、Cortex 鞏固）原本會整天被跳過、憑空蒸發；現在改由白天閒置時段低優先補做。

## 核心功能

- **Scripture-driven settings**：`Scripture/Scripture.md` 是可熱載入的行為設定，控制 persona、輸出語言、temperature、context window、RAG top-k 以及預設輸出模板 (`use_template`) 等參數。
- **Long-document ingestion pipeline**：長文進入 `Consolidate/` 後，`IngestionPipeline` 會切分文字、生成 Part notes、產生 structured digests、輸出 Stitched Article 與 Synthesis。
- **結構感知切分（ThoughtfulSplitter）**：開啟 `use_thoughtful_splitter` 後，依章節／標題邊界切 Parts、把短段落打包到大小預算、過長段落再遞迴細切；切之前先跑 `source_prep` 前處理（剝除 Gutenberg 授權／目錄、把純文字章節線索升格成 markdown 標題）。
- **可續跑的 Part 蒸餾（B1 resume）**：每個 Part note 的 frontmatter 會持久化 `pending_concepts` 與結構化 digest，所以幾百個 Parts 的長文中途被打斷後，重跑會略過已完成的 Part、不重複呼叫 LLM。
- **重點標注 + 學習輔助**：`highlight_spans` 讓每個 Part note 把關鍵句包進 `== ==`（無額外 LLM call、只標逐字命中、非破壞性）；`visual_router` 為 Synthesis／Insight 自動挑最合適的視覺化（比較表／流程圖／心智圖…）。
- **書評／報導發布track**：`@ling-review` 把筆記的 Synthesis 寫成報導者／書評人口吻的助學習稿，`@ling-blog` 再把核可稿轉成 Quartz 內容送進 kafu 數位花園（build/push 留在 kafu 端手動）。
- **Source-grounded Parts**：每個 Part 會記錄原文 char/line range，Stitched Article 也會顯示 `Original range`，方便從分析結果回到原始段落。
- **Stitched Article**：`Title (Stitched).md` 保留各 Part 的主要正文，適合完整閱讀與校對。
- **Synthesis Note**：`Title (Synthesis).md` 使用 Part digests 進行總合成，並附上 Part Digest Appendix。
- **Insight mirror reports**：`@ling-insight` 會同時寫入 canonical report 到 `fromLingLing/`，並在 `Insights/` 放一份 byte-identical copy，保留完整 YAML frontmatter 供 Obsidian/Dataview/indexing 使用。
- **LingLens 概念透鏡**：`@ling-lens` 可用概念視角掃描文章，找出不能只靠 Ctrl+F 找到的語意實例；`@ling-count` 保留為 legacy alias。
- **Agentic command workflow**：在 `toLingLing/` 放入 `@ling-*` 指令檔，即可觸發巡邏、修復、洞察、合併、備份、還原等任務。支援 `/template` 指令動態切換輸出格式。
- **Markdown quality checker**：寫入 Obsidian 前會修復常見 Markdown/Mermaid/LaTeX 問題，包含裸 Mermaid block、未關閉 fence、node label quote、body frontmatter 與 `\rightarrow` 這類 carriage-return 轉義錯誤，並在 metadata 留下 `quality_checker` 紀錄。
- **Knowledge dashboard**：`index.md` 會自動更新，支援自然排序與 Obsidian callouts；最上方有「🆕 最近新增」區塊列出最新文件（依 `date_created` 新→舊，數量由 `recent_count` 控制），下方字母清單照舊。
- **Daydream 白日夢補做**：夜間深睡（1–5am 排程）被忙碌跳過時，`DaydreamPump` 會在白天系統閒置時補做欠下的認知——最低優先、一次一小口、可被使用者工作隨時插隊（複用 facet-backfill 的讓步契約）。工作階梯由高到低：①消化未鞏固的 insight 積壓（`run_consolidation` 一次一條，可續做）②補做漏掉的每日洞察 ③真的沒有欠工作時，閒著也自發產生一條輕量反思。嚴格 idle-only、限白天（夜晚仍屬深睡）、每日有額度上限、不背 SLA——忙了一整天就整天跳過，當晚深睡照常清完積壓。所有旋鈕都在 `Scripture/Scripture.md` 熱載入（與 `dreaming_from/to` 並排）：`daydream` / `daydream_spontaneous` 開關，`daydream_*_budget` 設每日額度。

## 快速開始

### 1. 安裝與設定

準備 Python 3.12 以上、Bash，以及可連線的 Ollama、vLLM 或 Gemini。若使用 Obsidian，請將 `lings-desktop/` 開啟為 vault。

首次安裝時執行：

```bash
git clone https://github.com/stevenlee/ling-ling.git
cd ling-ling
python3 --version
cp .env.example .env
```

開啟專案根目錄的 `.env`，選擇 `LLM_PROVIDER` 並填入相應設定。範例中的 Ollama 和 vLLM 位址須換成你能連線的端點，模型名稱須與服務提供的名稱一致。

| 提供者 | 要核對的設定 |
|---|---|
| Ollama | `OLLAMA_API_BASE`（同機範例為 `http://localhost:11434/v1`）、`OLLAMA_MODEL` |
| vLLM | `VLLM_API_BASE`（OpenAI 相容的 `/v1` 端點）、`VLLM_MODEL`、`VLLM_API_KEY` |
| Gemini | `GEMINI_API_KEY`、`GEMINI_MODEL` |

API 金鑰留在本機的 `.env`。首次使用可保留 `EMBEDDING_PROVIDER=local`。若沿用既有索引，請使用建立索引時的 embedding 提供者與模型。切換前先備份，並安排重建索引。

### 2. 檢查與啟動

在專案根目錄執行：

```bash
./start.sh --check
./start.sh
```

首次執行會用 `python3` 建立 `venv/` 並安裝依賴。`--check` 會建立缺少的目錄，檢查 prompt 資產、agent 介面與 Markdown parser。實際模型呼叫與索引狀態需透過使用流程確認。`./start.sh` 在前景執行 daemon，可按 Ctrl+C 停止。

### 3. 試跑一份短文件

在 daemon 啟動後，將一份可交給所選模型處理的短 Markdown 放到 `lings-desktop/Consolidate/`。到 `lings-desktop/pages/` 確認文件產出，再到 `lings-desktop/raw/consolidate/` 確認原文已歸檔。

多語 reranker 可選裝。若需要，執行 `venv/bin/pip install -r requirements-reranker.txt`，並在 `.env` 設定 `RERANKER_ENABLED=true`。缺少依賴時系統會略過 reranker。

## 日常維護與排查

| 情況 | 先檢查 |
|---|---|
| 匯入或指令沒有產出 | 確認 daemon 在執行，再看檔案是否仍在 `Consolidate/` 或 `toLingLing/`。匯入產出在 `pages/`，指令回覆在 `fromLingLing/`。 |
| 模型呼叫失敗 | 查看啟動終端與 vault 裡的 `log.md`，核對 `.env` 的端點、模型與金鑰。修改 `.env` 後重啟 daemon。 |
| 匯入頁面存在，但搜尋不到 | 查看頁面 frontmatter。若 `ingest_status: pending_index`，代表頁面已寫入，索引提交尚未完成。查閱日誌確認失敗原因。 |
| 想查看背景維護 | 查看 `maintenance.log.md`，或用 `./tui.sh` 查看活動。可用 `@ling-assess` 產生唯讀品質記分卡。 |

模型連線與啟動參數見 [`.env.example`](.env.example)。角色、輸出與排程時段見 [Scripture](lings-desktop/Scripture/Scripture.md)。目前維護計畫保留 M1、停用定期 M2／M3，Evidence Traceback 採只報告模式。設定與待核對的範圍見[剩餘工作清單](System_Engine/DesignDoc/Remaining_Work_2026-11-04.md)。

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

### 一般指令

| 指令 | 說明 |
|---|---|
| `@ling-lens` | 概念透鏡。掃描文章中的語意實例，輸出 evidence、confidence、分析錨點與原文範圍。 |
| `@ling-count` | `@ling-lens` 的舊別名，仍可使用。 |
| `@ling-patrol` | 全庫健康檢查，找出死連結、孤兒頁面與資料庫不同步。 |
| `@ling-patrol-tags` | 標籤巡邏，找出未翻譯或格式錯誤的標籤。 |
| `@ling-repair-tags` | 根據巡邏清單批量修復標籤。 |
| `@ling-repair-db` | 資料庫修復與重新同步。 |
| `@ling-insight` | 從既有筆記生成跨領域洞察。 |
| `@ling-merge` | 合併筆記。內容包含 `[[A]]` 與 `[[B]]` 即可融合兩篇筆記。 |
| `@ling-plan` | 規劃多步工作流（Pipeline），產出腳本與預覽報告，不立即執行。 |
| `@ling-do` | 執行 `@ling-plan` 產出的工作流腳本。 |
| `@ling-profiles` | 管理文件路由 Profiles。`pending` 看待審草稿、`approve <名稱>` 一鍵生效。 |
| `@ling-cortex` | 對 Cortex 長期記憶層跑三層驗證（紅線/品質/檢索效益），報告含狀態分佈、矛盾對與人工抽查清單。 |
| `@ling-recall` | 回想——給一個主題，撈出 Cortex 中最相關的蒸餾主張（連同信心、可反駁性、反例、證據鏈與矛盾）。與 `@ling` 問答不同：問答從原始筆記答，recall 從**蒸餾過的信念**答。 |
| `@ling-tensions` | 知識張力掃描——攤開 Cortex 裡的矛盾對、教條（高信心低可反駁性）、證據單薄與已被推翻的主張。recall 的反面：不是「我相信什麼」，而是「我的信念在哪裡有問題」，對抗自我印證用。 |
| `@ling-visualize [[筆記]]` | 學習輔助視覺化——判斷內容的認知結構,自動產生最合適的學習產物（17種圖表：比較表 / 流程圖 / 心智圖 / 時間軸 / 象限圖 / 概念圖 / 論證圖 / 循序圖 / 狀態圖 / 使用者旅程 / 甘特圖 / 圓餅圖 / 桑基圖 / XY座標圖 / 方塊圖 / C4架構圖 / 類別圖 / ER關聯圖，沒有強結構就不硬畫）。可用 `as <type>` 指定類型，例如 `@ling-visualize [[X]] as timeline` 或 `as all`。 |
| `@ling-zip` / `@ling-unzip` | 備份與還原知識庫。 |
| `@ling-RESET` | 清除內容前會先強制備份。 |

### 大腦運作命令（手動觸發排程/鞏固，與排程器/白日夢共用同一套函式）

| 指令 | 說明 |
|---|---|
| `@ling-dream` | 立刻做一次反思（doc-anchored 洞察生成），不必等夜間做夢窗口。 |
| `@ling-consolidate` | 立刻把未鞏固的 insight 蒸餾進 Cortex（消化積壓）。 |
| `@ling-decay` | 跑一次 Cortex 衰減 / 強化。 |
| `@ling-ledger` | 跑一次 Cortex falsification 帳本。 |
| `@ling-assess` | 自我體檢（Metacognition M1）——彙整七軸品質訊號成健康記分卡（唯讀，無副作用）。 |
| `@ling-improve` | 自我改善提案佇列（M3）。`generate` 跑 M1→M2→M3 產出修訂提案，`list`／`show <id>` 檢視，`approve <id>`／`reject <id>` 人工核可。只提案不自動套用；核可才寫檔且原檔先備份。 |
| `@ling-resynthesize [[標題]]` | 把既有文件的原始檔重新投入 `Consolidate/`，重跑 synthesis（sidecar 圖片一併還原）。 |

### 發布track（書評／報導 → kafu 數位花園）

| 指令 | 說明 |
|---|---|
| `@ling-review [[標題]]` | 把一篇已有 Synthesis 的筆記，用 **報導者／書評人** 口吻寫成助學習的書評／報導，產出到 `fromLingLing/`（不回寫 `pages/`）。genre 可用 `as book\|explainer\|paper\|patent` 指定；省略時標題含專利號→`patent`，否則→`book`。 |
| `@ling-blog` | 把 `lings-desktop/Blog/` 裡核可的 review 轉成 Quartz 內容、送進 kafu repo 的 `content/`（純本機搬運，不 build、不 push）。上線在 kafu 端手動 `make publish`。 |

## TUI cockpit（終端機操作台）

除了 Obsidian，還可以用一個**終端機 TUI** 直接下命令、看狀態。它是一個**獨立的伴生程式**：

```bash
./tui.sh        # 首次會自動安裝 textual (requirements-tui.txt)
```

- **命令調色盤**：選一個命令、填好 `[[目標]]` 與選項，按 Enter 就把 `@ling-*.md` 投進 `toLingLing/`——跟 Obsidian 完全同一條管線（兩邊並存）。
- **狀態列**：忙碌/閒置（讀 `.kb_lock`）、當前正在跑的 run、provider/role/做夢窗口。
- **活動feed + 產出**：即時讀 `llm_trace.sqlite`（唯讀）、`maintenance.log.md` 與 `fromLingLing/`。

設計上 TUI **絕不**開 ChromaDB（單寫入安全）：所有「大腦運作」都是 daemon 在執行，TUI 只負責投命令與唯讀顯示。daemon 沒開時，投進去的命令會留在 `toLingLing/` 等它啟動後處理。

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
│   └── watchers/                # filesystem watchers
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

> 想知道這些模組彼此怎麼呼叫、資料怎麼流動，見 [`SystemDesign.md`](System_Engine/DesignDoc/SystemDesign.md)。

## 架構演進

逐項變更紀錄見 [CHANGELOG.md](lings-desktop/CHANGELOG.md)；架構細節見 [`SystemDesign.md`](System_Engine/DesignDoc/SystemDesign.md)。主要里程碑（新到舊）：

| 里程碑 | 時間 | 摘要 |
|---|---|---|
| 產出品質稽核 + 反同質化／自省接通 | 2026-07 | 七軸稽核（[audit report](System_Engine/DesignDoc/LingLing_Output_Audit_report_20260712.md)）→ 一連串硬化：refute 對創意型 operation 改 lenient（不再誤殺）、grounding 用 MMR 去集中化、montecarlo 殼改造去戰略建議模板（novelty ~翻倍）、M-arc 接通（M3 泛化到洞察品質軸 + 陳舊 pending 提醒）、synthesis 品質閘 verdict parser 補中文 header + 計量把 unparseable 算進 bad。全部 gated／default-off／行為保守，均經 live 驗證。 |
| Metacognition 自我改善弧線（M1–M4） | 2026-06 | 自動評估→自動改善的四階閉環，全數 flag-gated default-off：M1 七軸自評記分卡（`@ling-assess`，唯讀零 LLM）→ M2 診斷（每紅/黃軸一次 lean LLM 給根因+候選改善）→ M3 提案（`@ling-improve`，把診斷變 gated `_pending` 修訂提案，人工核可才寫檔）→ M4 數值自調（唯一無人工閘，限安全旋鈕 + damping + 自動回滾）。反漂移硬規則：評估/改善分離、改善走閘、多訊號交叉防 Goodhart。 |
| Parts 蒸餾翻修 | 2026-06 | 結構感知切分（ThoughtfulSplitter + `source_prep` 前處理）讓 Parts 沿章節邊界切；B1 resume 把 `pending_concepts`/digest 持久化進 frontmatter，長文中斷後可續跑。（曾試「每 Part 合併成一次 LLM call」的加速法，A/B 實測只有 1.03×——生成是 token-bound 不是 call-bound——已移除。） |
| 書評／報導發布track | 2026-06 | `@ling-review` 把 Synthesis 寫成助學習的書評／報導（book/explainer/paper/patent 四種 genre + `identifier_guard` 校正），`@ling-blog` 把核可稿轉成 Quartz 內容送進 kafu 數位花園（build/push 留在 kafu 端）。 |
| Daydream 白日夢補做 | 2026-06 | 夜間做夢窗口被忙碌跳過的工作改由白天閒置時段低優先補做（consolidation 積壓 → 漏掉的每日洞察 → 自發反思），複用 facet-backfill 的「一次一小口、可被插隊、衍生不追蹤、每日額度」讓步契約。 |
| 全模組稽核與硬化 | 2026-06 | 多代理程式碼稽核 + 逐項驗證修正——資料完整性、reasoning-channel JSON 防禦、watcher 並發、trace 索引。 |
| Cortex 長期記憶（Phase 1–4） | — | insight 品質訊號 → 夜間鞏固成原子主張 → 雙強度衰減 + 行為訊號 → 保守 falsified 主張帳本。 |
| Backlog 批次 | — | falsifiability 評分穩健化、每週記事、critique retry、Lens 引文驗證、新 Operations（compare/classify/outline/explain）。 |
| Profile 路由 | — | 具名 persona+template 配對取代 DocType.md，封閉式選擇 + 審核佇列 + 路由可觀測性。 |
| Facet 指標檢索 + 自我進步 bench | — | digest 句子作檢索指標、facet A/B lift、評測集隨 vault 自動生長。 |
| RAG 品質與成本堆疊 | — | content-hash skip、持久 embedding cache、MMR、cross-encoder reranker、hybrid BM25+RRF。 |
| Capability 層 + PipelineRunner/Planner | — | Operations/Skills capability metadata、adapter 註冊表、`@ling-plan` / `@ling-do`。 |
| Ingestion pipeline | — | 長文 map-reduce（Parts → digests → Stitched → Synthesis）、source-grounded char/line range、LingLens evidence grounding。 |

## 開發檢查

從專案根目錄建立開發環境並執行與 CI 相同的檢查：

```bash
python3 -m venv venv
venv/bin/pip install -e '.[tui,dev]'
make check
```

`make check` 依序執行 Ruff、mypy 和快速測試。全套測試用 `make test`，依功能挑選測試見[測試分層](System_Engine/DesignDoc/Test_Profiles.md)。測試須使用隔離的暫存資料。修改引擎前請閱讀[工程慣例](System_Engine/DesignDoc/Engineering_Conventions.md)。

## License

MIT License Copyright (c) 2026 [MH / Project Ling Ling]

Ling-Ling's Note: "Mom said you can use my code, but don't claim you wrote it yourself. That's plagiarism!"
