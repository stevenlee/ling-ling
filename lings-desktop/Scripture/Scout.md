---
targets:
  - url: https://github.com/trending
    parser: github_trending
    cadence: daily
    max_items: 10
  - url: https://news.ycombinator.com/newest
    parser: hackernews
    cadence: daily
    max_items: 15
  - url: https://arxiv.org/list/cs.AI/recent
    parser: arxiv
    cadence: daily
    max_items: 10
language: ""
---

# 🔭 Scout 偵查目標

Scout 每天出門一次，把上面列表裡的目標爬回來、整理成
`fromLingLing/✅Scout-YYYY-MM-DD.md` 日報（概要＋綜合分析）。

## 使用方式

- **啟用**：在 `Scripture.md` frontmatter 加 `scout: true`（熱重載，免重啟）。
- **加目標**：在上面 `targets:` 加一條。欄位：
  - `url`（必填）
  - `parser`（可省略；`github_trending` / `hackernews` / `arxiv`，省略時由網址自動判斷）
  - `cadence`：`daily` 或 `weekly`（預設 daily）
  - `max_items`：這個目標最多取幾項（預設看 Scripture 的 `scout_max_items`）
- `language: ""` 表示日報語言跟隨 Scripture 的 `say`；也可以指定，例如
  `language: English`。
- 已看過的項目 30 天內不會重複進日報（去重狀態在 `Database/scout_state.json`）。
- 每條新項目會抓內文給 LLM 逐條分析（`scout_fetch_content: false` 可關掉，
  改用標題＋列表摘錄）。

## 備註

- 任意網址的通用抽取（部落格、新聞站）是 Phase 2，還沒好；目前只支援上面兩種 parser。
- 某個目標抓取失敗時，日報底部「🧹 抓取狀況」會註明原因，其他目標照常。
