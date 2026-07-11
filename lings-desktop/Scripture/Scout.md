---
language: ""
---

# 🔭 Scout 偵查目標

Scout 每天出門一次，把下表的目標爬回來、整理成
`fromLingLing/✅Scout-YYYY-MM-DD.md` 日報（逐條內文分析＋持續上榜訊號
＋vault 相關筆記連結），並鏡射到 `Notes/Scout/` 讓日報可被檢索。

## 目標清單

| url                                     | parser          | cadence | max_items |
| --------------------------------------- | --------------- | ------- | --------- |
| https://finance.yahoo.com/news/rssindex |                 | daily   | 10        |
| https://news.ycombinator.com/newest     | hackernews      | daily   | 15        |
| https://arxiv.org/list/cs.AI/recent     | arxiv           | daily   | 10        |
| https://github.com/trending             | github_trending | daily   | 10        |

## 使用方式

- **啟用**：在 `Scripture.md` frontmatter 設 `scout: true`（首次啟用需重啟
  daemon，之後熱切換）。
- **手動觸發**：在 `toLingLing/` 丟一個**檔名**含 `@ling-scout` 的筆記
  （或檔名隨意、內文寫 `/scout`——路由規則是 `@ling-` 看檔名、斜線看內文），
  Scout 立刻出動一趟（不受 `scout` 開關限制；去重狀態與夜間排程共用，
  白天手動跑過晚上就只剩少量新項目）。
- **加目標**：在上表加一列。只有 `url` 必填，其他欄留空用預設：
  - `parser`：`github_trending` / `hackernews` / `arxiv` / `feed`；留空自動判斷
  - `cadence`：`daily` 或 `weekly`（留空 = daily）
  - `max_items`：這個目標最多取幾項（留空看 Scripture 的 `scout_max_items`）
- 任意部落格/新聞站直接填網址即可——Scout 會自動找該站的 RSS/Atom feed
  （網址本身是 feed 也行）。找不到 feed 的站會在「抓取狀況」註明，
  這種情況請改填該站的 feed URL。
- frontmatter 的 `language: ""` 表示日報語言跟隨 Scripture 的 `say`；
  也可指定，例如 `language: English`。

## 備註

- 已看過的項目 30 天內不會重複進日報（去重狀態在 `Database/scout_state.json`）。
- 連續上榜 ≥3 天的項目不會重複列出，但會在該節以「🔁 持續上榜」標註天數。
- 每條項目若與 vault 既有筆記高度相關，行尾會附「相關筆記: [[筆記名]]」。
- 每條新項目會抓內文給 LLM 逐條分析（`scout_fetch_content: false` 可關掉，
  改用標題＋列表摘錄）。
- 某個目標抓取失敗時，日報底部「🧹 抓取狀況」會註明原因，其他目標照常。
