# 🪷 @ling-blog 範例指令

發布——把 `lings-desktop/Blog/` 裡核可的 review 轉成 Quartz 內容，送進 **kafu**
數位花園的 `content/`。這是發布流程的 ling-ling「推送」半邊（對應 `make blog`）：
**純本機檔案搬運，不呼叫 LLM、不連網、不 build、也不 push**。

> [!TIP]
> **發動方式**：在 `toLingLing/` 建一個檔案，內容只要一行指令即可。回報寫入 `fromLingLing/`。
> **前置條件**：先把 [`@ling-review`](@ling-review.md) 產出、你核可要發布的稿子複製進 `lings-desktop/Blog/`。
> **目標 repo**：`$KAFU` 環境變數指定的路徑，沒設就用 `~/projects/kafu`。

---

## 範例 1：發布 Blog/ 裡全部的 review
```markdown
@ling-blog
```

## 範例 2：斜線寫法
```markdown
/blog
```

---

### 它做了什麼
- 掃 `lings-desktop/Blog/*.md`（以 `_` 開頭的檔案會跳過），跑 `blog_transform`，寫出 web-ready 的 Quartz markdown 到 `kafu/content/`。
- 回報列出寫了哪幾篇，並附上「下一步」指令。

### 接著做什麼（kafu 那邊，手動）
build + 上線刻意留在 kafu repo 手動執行——`@ling-blog` 絕不替你 push：
```bash
cd ~/projects/kafu && make publish     # build + 部署上線
# 或先在本機看一眼：
cd ~/projects/kafu && make preview
```

### 小提醒
- 找不到 kafu repo 時會直接回報，請確認路徑存在或設好 `KAFU` 環境變數。
- `Blog/` 沒東西時也會明白告訴你「沒有可發布的 review」，不會默默成功。
- 這條命令可重複跑：同名檔會覆蓋更新，不會重複堆積。
