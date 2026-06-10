# Profiles — 文件路由設定檔

這個資料夾的每一個 `.md` 檔就是一個 **Profile**：一組經過驗證的「persona + template」配對。
Ingestion 時系統選的是 Profile，而不是分別選 persona 和 template，所以兩者永遠不會配錯對。
（本系統取代了舊的 `Scripture/DocType.md` 對照表。）

## Profile 檔格式

檔名（不含 `.md`）就是 profile 的正式名稱。Frontmatter 宣告路由決策，內文是給人看的說明：

```yaml
---
persona: cookery-curator            # Scripture/Personas/ 下的檔名（必填）
template: cookery-recipe-card      # Templates/ 下的檔名（必填）
operations: [digest_sources]       # 選填，保留給未來 Planner 編排
description: 食譜與烹飪教學          # 給人看的簡述
applicable_when: Recipes, cooking instructions, kitchen techniques
---
```

> **`applicable_when` 是路由準確度的關鍵**：系統會把所有 profile 的這一行餵給 LLM
> 做封閉式選擇。寫得越具體（包含典型文件特徵、關鍵詞），選得越準。

## 路由順序（高到低）

1. **手動指定**：文件 frontmatter 寫 `profile: 名稱`，或直接寫
   `synthesis_persona:` / `synthesis_template:` 覆寫 — 完全跳過自動選擇。
2. **自動選擇**：`document_type:` 命中 profile 名稱直接採用；否則 LLM
   在已註冊的 profile 中挑一個（答不出就是 `none`）。
3. **預設**：落到 `default` profile；連 `default` 都沒有時用
   `Scripture.md` 的 `be_a` / `use_template`。

## `_pending/` 審核佇列

遇到無法分類的新文件類型時，Ling Ling 會自動草擬一組
persona / template / profile 放進 `_pending/<類型名>/`，並在 `fromLingLing/`
留審核通知。**草稿不會自動生效**。在生效之前，同類文件一律先用
`default` profile 處理。

審核通過後，兩種生效方式擇一：

- **一鍵生效**：在 `toLingLing/` 放入指令檔 `@ling-profiles approve <類型名>`，
  三個檔案會自動搬到正式位置並清掉審核通知。
- **手動**：把三個檔案各自搬到 `Scripture/Personas/`、`Templates/`、
  `Scripture/Profiles/`。

其他指令：`@ling-profiles`（總覽）、`@ling-profiles pending`（草稿明細）。

## 新增或修改 Profile

直接在這個資料夾新增/編輯 `.md` 檔即可，下一次 ingestion 就會生效（每次
ingestion 都重新掃描）。注意：

- 底線開頭的檔案（如本檔）與 `_pending/` 內容會被掃描略過。
- `persona` 與 `template` 缺一不可，缺了整個 profile 會被略過並記 warning。
- 語系變體（`foo.zh.md`）不會被當成獨立 profile。
