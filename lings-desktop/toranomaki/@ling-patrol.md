> [!TIP]
> **發動方式**：將此檔案拖入 `toLingLing/` 資料夾。
> **效果**：檢查 Wiki 健康狀況（死連結、孤立頁面、重複內容）。
> 
> **查看報告**：完成後請查看 `fromLingLing/` 下的 `✅patrol-rpt-xxxx.md`。
> *註：自動修復由 `.env` 的 `SELF_HEALING` 控制（**預設開啟**），會順手補齊缺失索引、清除過時殘留。想要只報告不動手，把 `SELF_HEALING=false`。針對性的向量庫修復請改用 `@ling-repair-db`。*
