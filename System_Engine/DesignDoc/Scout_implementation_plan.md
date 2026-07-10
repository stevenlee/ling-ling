# Scout（定時爬蟲 → 日報）— Implementation Plan

> 目的：一個排程驅動的偵查工具。從 vault 內的目標列表檔讀取要爬的網站，
> 把新內容爬回來、以指定語言逐項摘要，再由 LLM 產出跨來源的綜合分析，
> 整理成一份日報 `✅Scout-YYYY-MM-DD.md`。撰於 2026-07-11。
>
> **R1（2026-07-11，同日修訂）**：使用者對首次試跑的三點回饋 →
> (1) per-item 分析必須**抓內文 grounding**（原本只餵標題＋列表摘錄）——
> `services/scout/content.py` 通用抽取提前自 Phase 2，每條新項目一次
> HTTP GET ＋一次 LLM 呼叫，knob `scout_fetch_content`（預設 on）；
> (2) 新增 **arxiv parser**（`/list/<cat>/recent` → 官方 API Atom XML，
> abstract 直接當內文，去重鍵去版本號）；(3) 批次摘要改為**逐條摘要**
> （per-item prose，不再要求編號行格式）。LLM 成本：每日約
> Σ(新項目數) 次 summarize ＋ 1 次 analyze。
>
> **R2（2026-07-11）**：真跑後使用者判定「綜合分析」三節（趨勢／跨來源
> 訊號／值得深入）缺乏價值 → **整節移除**。根因是結構性的：對單日、
> 少量、彼此無關的榜單快照做趨勢歸納，只能產出空泛填充。此節在 Phase 2
> 以**有根據的形態**回歸：跨日 streak 訊號（「連續 N 天上榜」）＋ vault
> RAG bridging（「與 [[某筆記]] 相關」）。日報現為：逐條分析清單＋抓取狀況。
>
> 命名：工具名 **Scout**（主動出門偵查、帶情報回來交報告，貼合 LingLing
> 好學生交作業的 persona 敘事）。報告 type=`Scout`，手動指令（Phase 3）
> `@ling-scout`。

## 0. 設計原則

- **不另起爐灶**：全部掛進既有基礎設施 —— `PoliteHttpClient`（節流 HTTP）、
  `MaintenanceScheduler`（每日排程）、`LLMClient.complete()`（LLM 呼叫）、
  `dump_markdown_with_metadata`（vault 輸出）、`DynamicSettings`（Scripture 熱重載）。
- **行為旋鈕進 Scripture，不進 .env**（config-in-Scripture 慣例）。
- **flag-gated，預設關閉**：`scout: true` 才啟用，與 M1-M4 等既有功能一致。
- **失敗隔離**：單一目標抓取失敗只在報告「抓取狀況」節註明，絕不讓整份日報掛掉。
- **摘要與翻譯合併成一次 LLM 呼叫**（直接以目標語言輸出概要），避免
  譯文污染（DocQuality track 的教訓），也省 token。

## 1. 架構

```
lings-desktop/Scripture/Scout.md (targets 檔, Obsidian 可熱編輯)
    │
MaintenanceTask "scout_daily" (daily, idle_required, 由 MaintenanceScheduler 觸發)
    │  gate: settings.SCOUT_ENABLED
    ▼
services/scout/digest.py::run_scout_digest(llm)
    ├─ targets.py   讀 targets → cadence 過濾（weekly 距上次 <6 天者跳過）
    ├─ parsers/     每目標一個 parser：fetch → list[CrawledItem]
    │     github_trending  (刮 HTML, bs4)
    │     hackernews       (Algolia API, JSON — 不刮 /newest 頁面)
    ├─ state.py     去重：seen URL-hash 滾動窗口（30 天），只留新項目
    ├─ LLM #1..N    每目標一次 complete()：逐項 2-3 句概要（目標語言）
    ├─ LLM #N+1     一次 complete()：跨來源綜合分析（趨勢/共同訊號/值得深入）
    └─ 報告         dump_markdown_with_metadata → fromLingLing/✅Scout-YYYY-MM-DD.md
```

## 2. targets 檔格式（`lings-desktop/Scripture/Scout.md`）

```yaml
---
targets:
  - url: https://github.com/trending
    parser: github_trending   # 可省略；由 URL 自動偵測
    cadence: daily             # daily | weekly，預設 daily
    max_items: 10              # 可省略；預設 scout_max_items
  - url: https://news.ycombinator.com/newest
    cadence: daily
language: ""                   # 報告語言；空字串 = 跟隨 Scripture 的 say（OUTPUT_LANGUAGE）
---
```

- frontmatter 用既有 `parse_markdown_metadata` 解析；body 可自由寫筆記，引擎不管。
- 壞掉的 target 條目（缺 url、cadence 打錯字）記 warning 跳過，不影響其他目標。

## 3. 模組與檔案

| 檔案 | 職責 |
|---|---|
| `services/scout/models.py` | `ScoutTarget` / `CrawledItem` / `TargetResult` dataclasses |
| `services/scout/targets.py` | 讀＋驗證 targets 檔 → `(list[ScoutTarget], language)` |
| `services/scout/parsers/__init__.py` | parser registry + URL 自動偵測（`resolve_parser`） |
| `services/scout/parsers/github_trending.py` | 刮 trending HTML（`article.Box-row`），支援 `/trending/<lang>?since=` 變體 |
| `services/scout/parsers/hackernews.py` | Algolia `search_by_date?tags=story` API |
| `services/scout/state.py` | `Database/scout_state.json`：seen hash（30 天窗口）＋各 target `last_crawled_at`；atomic write（沿用 maintenance_state 模式） |
| `services/scout/digest.py` | 主流程 + 兩層 LLM 呼叫 + 報告組裝/寫檔；入口 `run_scout_digest(llm)` |

新增 config（`core/config.py`）：

- 路徑（infra）：`SCOUT_TARGETS_FILE = SCRIPTURE_DIR / "Scout.md"`、
  `SCOUT_STATE_FILE = DATABASE_DIR / "scout_state.json"`
- DynamicSettings 綁定（Scripture 熱重載）：
  - `scout` → `SCOUT_ENABLED`（bool，預設 **False**）
  - `scout_language` → `SCOUT_LANGUAGE`（str，預設 `""` = 跟隨 `OUTPUT_LANGUAGE`）
  - `scout_max_items` → `SCOUT_MAX_ITEMS_PER_TARGET`（int，預設 10）

排程（`watchers/maintenance_scheduler.py::_default_tasks`）：

```python
MaintenanceTask(name="scout_daily", action=scout_digest, daily=True,
                idle_required=True, intent="maintenance.scout", agent="Scout")
```

closure 內先檢查 `settings.SCOUT_ENABLED`，關閉時回 `skipped`（與 cortex_* 任務同型）。
weekly cadence 由 digest 內部用 state 的 `last_crawled_at` 判斷（距上次 ≥6 天才跑），
scheduler 不需要新機制；未來加 `cadence: mon,thu` 也只動 digest。

## 4. LLM 呼叫細節（R1/R2 後）

走 `llm.complete()`（lean path，不注入 persona/template 機器）：

**逐條分析**（每條新項目一次呼叫）：user msg 含 title/url/stats/列表摘錄
＋抓回的頁面內文（`content.py` 抽取，8000 字上限；arXiv 用 abstract）。
要求 2-4 句 {language} prose、以內文為根據。空回覆或內文抓不到都逐條
degrade（fallback 列表摘錄）——LLM 失敗不會讓項目消失。

原第二層「綜合分析」已於 R2 移除（見檔頭），Phase 2 以 streak＋RAG
bridging 回歸。溫度用預設；`stage` 標 `scout_summarize` 以利 trace 查詢。

## 5. 報告格式

檔名 `fromLingLing/✅Scout-YYYY-MM-DD.md`（每天一份，日期即唯一鍵；同日重跑覆寫）。
frontmatter：`title: Scout-YYYY-MM-DD`、`type: Scout`、`date_created`、
`targets_ok` / `targets_failed`、`new_items`、`tags: [Scout]`。

```markdown
# 📓 Scout 日報 YYYY-MM-DD

## GitHub Trending
- [owner/repo](url) ⭐12,345 — 內文導向的 2-4 句分析…

## Hacker News (newest)
- [title](url)（💬 42）— …

## arXiv cs.AI (recent)
- [title](abs-url) 作者 — abstract 導向分析…

## 🧹 抓取狀況
- github.com/trending：10 項（3 新）
- news.ycombinator.com/newest：抓取失敗 — <原因>
```

（R2 起無「綜合分析」節；Phase 2 以 streak＋RAG bridging 的有根據形態回歸。）

- 全部目標都沒有新項目 → 不寫檔，task 回 `succeeded, no new items`。
- 已知限制：`fromLingLing/` 不在 VaultWatcher 索引範圍，日報不進 RAG
  （與其他 fromLingLing 產出一致）；要可檢索再走 Insights 式鏡射（Phase 2 決定）。

## 6. 禮貌與穩定性

- `PoliteHttpClient` 每來源節流：`github` 2.0s、`hackernews` 1.0s；共用
  描述性 UA。每目標單次抓取（HN 一個 API call、GH 一頁 HTML），量極小。
- GitHub Trending 無官方 API，HTML 結構變動 = parser 失敗 → 該目標進
  `targets_failed`，日報其餘照出。selector 集中在 parser 一處好修。
- HN 走 Algolia 公開 API（`hn.algolia.com/api/v1/search_by_date`），
  比刮 `/newest` 穩定；`url` 為空的 Ask HN 類貼文 fallback 連到 HN item 頁。

## 7. Phase 拆解

- **Phase 1（本計劃實作範圍）**：上述全部 —— targets 讀取、GH/HN 兩個
  parser、去重 state、兩層 LLM、daily 排程（含 weekly cadence gate）、
  報告輸出、單元測試。
- **Phase 2**：通用 parser（任意 URL 文章抽取，`trafilatura`）、報告鏡射
  進 RAG 可索引目錄、「連續上榜第 N 天」訊號、bridging RAG 關聯
  （「這跟你 vault 的 [[某筆記]] 有關」）。
- **Phase 3**：`@ling-scout` 手動指令（AgentRegistry）、註冊 AdapterRegistry
  capability 讓 Planner 可調用、`Templates/Operations/ScoutAnalysis.md`
  把分析 prompt 移進 Operations 第四軸。

## 8. 測試

- `test_scout_targets.py`：frontmatter 解析、壞條目跳過、預設值。
- `test_scout_parsers.py`：GH trending fixture HTML / HN fixture JSON →
  CrawledItem；結構變動（selector 全空）→ 拋 `ScoutParserError`；URL 自動偵測。
- `test_scout_state.py`：seen 去重、30 天窗口修剪、atomic write、壞檔容錯。
- `test_scout_digest.py`：fake client + fake llm —— weekly cadence gate、
  只有新項目進報告、單目標失敗隔離、無新項目不寫檔、概要 regex 回收
  與 fallback、報告 frontmatter/檔名。

## 9. 驗收

1. `pytest System_Engine/tests/test_scout_*.py` 全綠；ruff/mypy 零新錯
   （mypy 豁免清單不得新增）。
2. Scripture.md 加 `scout: true` ＋ 重啟 daemon → 隔日產出
   `fromLingLing/✅Scout-YYYY-MM-DD.md`，GH/HN 各自成節、含綜合分析。
3. 拔網路線／改壞一個 target URL 重跑 → 該目標列於抓取狀況失敗清單，
   其餘正常。
