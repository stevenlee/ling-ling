# 📝 @ling-review 範例指令

書評／報導——把一篇已經跑完 ingestion 的筆記，用 **報導者／書評人** 的口吻寫成
一篇「助學習」的評論或報導。它讀的是該筆記的 **Synthesis**（不是原始長文），
產出可直接給人看的稿子。

> [!TIP]
> **發動方式**：在 `toLingLing/` 建一個檔案，內容用下面任一格式。稿子寫入 `fromLingLing/`。
> **前置條件**：目標筆記必須已經有 `pages/<標題>/<標題> (Synthesis).md`（先讓它走完 ingestion）。
> **產出去向**：只寫 `fromLingLing/`，**不**寫進 `pages/`，所以稿子不會再被 ingestion 吃回去。

---

## 範例 1：最簡單——讓它自己決定 genre
```markdown
@ling-review [[西遊記]]
```
> 省略 genre 時：標題含專利號 → `patent`，否則一律 `book`。

## 範例 2：指定 genre（`as <genre>`）
```markdown
@ling-review [[Attention Is All You Need]] as paper
```

## 範例 3：科普報導體
```markdown
@ling-review [[某篇技術長文]] as explainer
```

## 範例 4：專利說明書
```markdown
@ling-review [[US-12645742-B1 RAG-based product assistance]] as patent
```

## 範例 5：斜線寫法
```markdown
/review [[某本書]] as book
```

---

### genre 一覽
| genre | 用途 | 可接受的別名 |
|-------|------|--------------|
| `book` | 書評（預設） | `book` |
| `explainer` | 科普／報導體 | `report`、`topic` |
| `paper` | 論文導讀 | `research` |
| `patent` | 專利說明書解讀 | — |

### 輸出長什麼樣
- 一篇繁體中文的書評／報導，語氣是「幫讀者學會」，不是流水帳摘要。
- 每種 genre 套不同的 review 模板（`book-review` / `explainer-report` / `paper-review` / `patent-review`），共用 `reviewer` persona。
- 引用到的標題／專利號／識別碼會經 `identifier_guard` 校正回正規寫法，避免模型抄錯。

### 接著做什麼
核可的稿子複製進 `lings-desktop/Blog/`，再用 [`@ling-blog`](@ling-blog.md) 送上 kafu 數位花園。
