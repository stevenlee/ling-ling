# Blog/ — 發布佇列（策展閘門）

把**核可要公開發布**的 review 丟進這個資料夾，就等於「我同意發這篇」。

- 來源：`@ling-review` 的產出（在 `fromLingLing/`）。看過、滿意的，複製一份進這裡。
- 發布流程（`services/blog_transform.py`）只讀這個資料夾，**不會自動掃整個 vault**——這是隱私邊界。
- 轉換 pass 會：清掉引擎 frontmatter、產生乾淨的 Quartz frontmatter、套用 wikilink 規則
  （**指向其他已發布 review 的連結保留，其餘剝成純文字**）、輸出到獨立 Quartz repo 的 `content/`。
- `== ==` 高亮、Mermaid、callout 原封不動 —— Quartz 原生渲染。

> 這個資料夾不被 daemon 監看（watcher 只看 `toLingLing/`），放檔案不會觸發任何 ingestion。
> 檔名以 `_` 開頭的（像本檔）會被發布流程略過。
