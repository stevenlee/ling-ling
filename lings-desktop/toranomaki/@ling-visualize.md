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
| `all`              | (輸出所有 19 種圖表)        |
| `comparison_table` | 比較多個對象的多個維度       |
| `flowchart`        | 流程、因果序列、步驟        |
| `mindmap`          | 一個主題的階層分解         |
| `timeline`         | 時序、階段、歷史          |
| `quadrant`         | 物件落在兩個軸 / 取捨      |
| `concept_map`      | 概念間的網狀關係          |
| `argument_map`     | 論證(主張+根據+隱含前提+反駁) |
| `sequence_diagram` | 實體間的對話、訊息傳遞或劇情順序 |
| `state_diagram`    | 狀態轉變與觸發條件         |
| `user_journey`     | 主角在不同階段的心境與體驗分數  |
| `gantt_chart`      | 歷史事件或專案排程的重疊關係    |
| `pie_chart`        | 整體中的比例、成分分配       |
| `sankey_diagram`   | 資金流向、資源分配、能量轉換等流量關係 |
| `xy_chart`         | 數值在時間序列或類別上的分佈與趨勢 |
| `block_diagram`    | 系統高階架構、硬體拓樸或立體方塊關係 |
| `c4_diagram`       | 軟體系統的 C4 架構 (Context / Container) |
| `class_diagram`    | 物件導向的類別、屬性與繼承關係 |
| `er_diagram`       | 資料庫的實體關聯表 (一對多、主外鍵) |
| `ontology`         | 領域本體：類別階層(is-a)、組成(part-of)、屬性與個體(instance-of) |

### 小提醒
- 不指定 type 就讓系統自動選——它也可能判定「沒有強結構」而不畫圖。
- 對象用 `[[WikiLink]]` 指定 `pages/` 或 `Notes/` 裡的筆記。
- 在 `Scripture/Scripture.md` 把 `visual_router: true` 打開,長文的 `(Synthesis)` 總結頁與**洞察報告**就會**自動**附上學習產物,不必每次手動下指令(改完即時生效,免重啟)。
- `argument_map` 預設只出結構化 Markdown;想額外要一張確定性的 Mermaid 論證圖,在 Scripture 開 `argument_map_mermaid: true`。
- 自動選型時偏好 `ontology`：只要關係能型別化 (is-a / part-of / instance-of) 就優先畫成本體論圖,而非鬆散的 `concept_map`。預設開啟,想回到中性分類在 Scripture 設 `ontology_bias: false`(熱載入,免重啟)。
