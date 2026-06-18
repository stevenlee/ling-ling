# 🖼️ @ling-visualize 範例指令

學習輔助視覺化——讀一篇筆記,判斷它的**認知結構**,自動產生最合適的學習產物
(不是一律畫流程圖)。幫你更快理解、組織、批判一份內容。

> [!TIP]
> **發動方式**：在 `toLingLing/` 建一個檔案,用下面任一格式。報告寫入 `fromLingLing/`。
> 內容若沒有清楚的結構,系統會明說「不產圖」而不是硬畫一張誤導的圖。

---

## 範例 1：自動選型(最常用)
```markdown
@ling-visualize [[某篇筆記]]
```
系統自己判斷該畫成比較表 / 流程圖 / 心智圖 / 時間軸 / 象限圖 / 概念圖 / 論證圖,還是不畫。

## 範例 2：指定類型(`as <type>`)
```markdown
@ling-visualize [[某篇文章]] as timeline
```

## 範例 3：把一篇論述拆成論證圖(批判性思考)
```markdown
@ling-visualize [[某篇評論]] as argument_map
```
輸出 Toulmin 骨架——主張、根據、**未明說的隱含前提**、適用條件、反駁,並標出「最弱的一環」。專門用來**找出隱藏邏輯**。

## 範例 4：斜線寫法
```markdown
/visualize [[筆記名]]
```

---

### 可用的 `as <type>`
| type               | 適合的內容             |
| ------------------ | ----------------- |
| `comparison_table` | 比較多個對象的多個維度       |
| `flowchart`        | 流程、因果序列、步驟        |
| `mindmap`          | 一個主題的階層分解         |
| `timeline`         | 時序、階段、歷史          |
| `quadrant`         | 物件落在兩個軸 / 取捨      |
| `concept_map`      | 概念間的網狀關係          |
| `argument_map`     | 論證(主張+根據+隱含前提+反駁) |

### 小提醒
- 不指定 type 就讓系統自動選——它也可能判定「沒有強結構」而不畫圖。
- 對象用 `[[WikiLink]]` 指定 `pages/` 或 `Notes/` 裡的筆記。
- 在 `Scripture/Scripture.md` 把 `visual_router: true` 打開,長文的 `(Synthesis)` 總結頁與**洞察報告**就會**自動**附上學習產物,不必每次手動下指令(改完即時生效,免重啟)。
- `argument_map` 預設只出結構化 Markdown;想額外要一張確定性的 Mermaid 論證圖,在 Scripture 開 `argument_map_mermaid: true`。
