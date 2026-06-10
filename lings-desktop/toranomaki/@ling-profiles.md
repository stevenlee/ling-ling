> [!TIP]
> **發動方式**：將此檔案拖入 `toLingLing/` 資料夾。
> **效果**：管理文件路由 Profiles（persona + template 配對），包含審核佇列的一鍵生效。
>
> **基本用法（總覽）**：
> - 直接拖入本檔，不需任何參數：`@ling-profiles`
> - 產出報告：所有已生效的 profile（persona / template / 適用情境）＋ 待審草稿清單。
>
> **查看待審草稿明細**：
> - `@ling-profiles pending`
> - 列出 `Scripture/Profiles/_pending/` 中每個草稿的 persona、template、檔案與生效指令。
>
> **一鍵生效（approve）**：
> - `@ling-profiles approve <草稿名稱>`（例如 `@ling-profiles approve diary`）
> - 系統會把草稿的三個檔案自動搬到正式位置：
>   1. persona → `Scripture/Personas/`
>   2. template → `Templates/`
>   3. profile → `Scripture/Profiles/`
> - 並清除 `fromLingLing/` 的審核通知。下一次 ingestion 即納入路由選項。
> - **安全保證**：永不覆寫既有檔案——若同名檔案已存在，會回報衝突並保持草稿原狀。
>
> **背景知識**：
> - 當 Ling Ling 遇到無法分類的新文件類型時，會自動草擬一組
>   persona / template / profile 放進 `_pending/`，並在 `fromLingLing/` 留審核通知。
>   草稿**不會自動生效**（品質優先於即時性）；在生效前，同類文件先以 `default` profile 處理。
> - 每週的路由健康報告也會列出待審草稿與 fallback 率，提醒您是否需要新增或調整 profile。
> - Profile 格式與路由規則詳見 `Scripture/Profiles/_README.md`。
