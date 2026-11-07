# Ling-Ling Vault / 玲玲知識庫

在 Obsidian 中將這個資料夾開啟為 vault。Ling-Ling daemon 從專案根目錄以 `./start.sh` 啟動。安裝、模型設定與排查見[專案 README](../README.md)。

| 想做的事 | 入口 | 到哪裡看結果 |
|---|---|---|
| 整理文件 | 將 Markdown 放進 `Consolidate/` | `pages/` 查看整理稿，`raw/consolidate/` 查看歸檔原文 |
| 交辦任務 | 將 Markdown 指令檔放進 `toLingLing/` | `fromLingLing/` 查看回覆，`raw/prompts/` 查看歸檔指令 |
| 瀏覽既有文件 | 開啟 [index.md](index.md) | 依索引連結進入筆記 |
| 修改角色或輸出設定 | 開啟 [Scripture](Scripture/Scripture.md) | 查看設定檔中的欄位與說明 |

第一次下指令時，可以參考 [@ling-assess](toranomaki/@ling-assess.md)，在 `toLingLing/` 建立一個只含 `@ling-assess` 的 Markdown 檔。Daemon 處理後，前往 `fromLingLing/` 查看健康記分卡。其他指令範例收在 [toranomaki/](toranomaki/)。

請把個人筆記放在 `Notes/`。新 checkout 啟動後會建立缺少的工作目錄。原文歸檔在 `raw/`，系統索引與執行狀態在 `Database/`。
